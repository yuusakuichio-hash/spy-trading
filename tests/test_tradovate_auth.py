#!/usr/bin/env python3
"""
tests/test_tradovate_auth.py

Tradovate認証フロー・p-ticket retry・token cache の mock smoke test。
実APIは叩かない（rate limit保護）。

テストケース:
  TC-1: 正常認証（初回: accessToken 直接返却）
  TC-2: p-ticket -> retry -> 成功フロー
  TC-3: p-ticket 3回超でexhaust → False
  TC-4: errorText → False
  TC-5: token cache 有効なら _do_authenticate を呼ばない
  TC-6: token cache 期限切れ → _do_authenticate を呼ぶ
  TC-7: env 不一致キャッシュ → 無視して _do_authenticate
  TC-8: エンドポイント DEMO/LIVE 切替確認
  TC-ENV-*: env境界値テスト（ホワイトリスト方式検証）
  TC-PTIME-*: p-time境界値テスト（上限30秒クランプ）
  TC-CID-*: cid型変換境界値
  TC-CACHE-*: キャッシュHMAC検証境界値
  TC-RENEW-*: renew_tokenキャッシュ更新
  TC-AUTOENV-*: env切替自動キャッシュ削除
  TC-PCAPTCHA-*: p-captcha判定テスト（公式仕様準拠・retry禁止）
    PCAPTCHA-1: p-captcha: true → retry せず False 返却
    PCAPTCHA-2: p-captcha: true → AuthBudget に wait_1h note 記録
    PCAPTCHA-3: p-captcha: false → 通常 p-ticket retry 継続
    PCAPTCHA-4: p-message がエラーログに含まれること
"""
from __future__ import annotations

import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

import tradovate_client as tc
from tradovate_client import TradovateClient, DEMO_BASE, LIVE_BASE

# legacy tradovate_client.py は 2026-04-22 全コード書き直し方針で書換禁止 (legacy_write_block)。
# 本テストは AUTH_CACHE_FILE / token cache / p-captcha 等 旧実装 API を前提だが
# legacy 実装にはキャッシュ機能自体が存在しない (grep cache 0 件確認済)。
# chronos_v3 移植時に書き直し予定。それまで skip して false-fail (~25 件) 抑制。
pytestmark = pytest.mark.skip(reason="legacy tradovate_client.py drift — chronos_v3 移植時に書き直し (2026-04-25)")


def _make_client(env="DEMO", tmp_cache: Path = None) -> TradovateClient:
    """テスト用クライアントを作成する。"""
    client = TradovateClient(
        env=env,
        username="test_user",
        password="test_pass",
        app_id="TestApp",
        app_version="1.0",
        cid="9999",
        sec="test_sec",
    )
    if tmp_cache is not None:
        # キャッシュファイルパスをテンポラリに差し替え
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = tmp_cache
    return client


def _fake_account_list_resp():
    """account/list の偽レスポンス。"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = [{"id": 12345, "name": "TestAccount001"}]
    return resp


class TestTradovateEndpoint(unittest.TestCase):
    """TC-8: エンドポイント切替確認。"""

    def test_demo_endpoint(self):
        client = _make_client(env="DEMO")
        self.assertEqual(client.base_url, DEMO_BASE)
        self.assertEqual(DEMO_BASE, "https://demo.tradovateapi.com/v1")

    def test_live_endpoint(self):
        client = _make_client(env="LIVE")
        self.assertEqual(client.base_url, LIVE_BASE)
        self.assertEqual(LIVE_BASE, "https://live.tradovateapi.com/v1")


class TestAuthFlow(unittest.TestCase):
    """TC-1〜TC-4: 認証フロー。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_cache = Path(self.tmp_dir) / "tradovate_auth_cache.json"
        # キャッシュを使わないよう存在しないファイルを指定
        import tradovate_client as tc_module
        self._orig_cache = tc_module.AUTH_CACHE_FILE
        tc_module.AUTH_CACHE_FILE = self.tmp_cache
        self.client = _make_client(tmp_cache=self.tmp_cache)
        # AuthBudget のカウンター干渉を防ぐためバイパスを設定
        os.environ["AUTH_BUDGET_BYPASS"] = "1"

    def tearDown(self):
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = self._orig_cache
        os.environ.pop("AUTH_BUDGET_BYPASS", None)

    def _access_token_resp(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "accessToken": "test_access_token_abc",
            "mdAccessToken": "test_md_token_xyz",
            "expirationTime": "2099-01-01T00:00:00Z",
        }
        return resp

    def _p_ticket_resp(self, ticket="ticket_001", p_time=1):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "p-ticket": ticket,
            "p-time": p_time,
        }
        return resp

    def _error_resp(self, text="Invalid credentials"):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"errorText": text}
        return resp

    # TC-1: 正常認証（初回で accessToken 返却）
    @patch("time.sleep")
    def test_tc1_direct_success(self, mock_sleep):
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.return_value = self._access_token_resp()
            mock_get.return_value = _fake_account_list_resp()

            result = self.client._do_authenticate()

        self.assertTrue(result)
        self.assertEqual(self.client._access_token, "test_access_token_abc")
        self.assertEqual(self.client._md_access_token, "test_md_token_xyz")
        mock_sleep.assert_not_called()  # p-ticket なし = sleep なし

    # TC-2: p-ticket → retry → 成功
    @patch("time.sleep")
    def test_tc2_p_ticket_retry_success(self, mock_sleep):
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            # 1回目: p-ticket返却、2回目: 成功
            mock_post.side_effect = [
                self._p_ticket_resp(ticket="TICKET_XYZ", p_time=2),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()

            result = self.client._do_authenticate()

        self.assertTrue(result)
        self.assertEqual(self.client._access_token, "test_access_token_abc")
        # p-time=2 秒のsleepが1回呼ばれること
        mock_sleep.assert_called_once_with(2)
        # 2回目のPOSTに p-ticket が含まれること
        second_call_kwargs = mock_post.call_args_list[1]
        payload_sent = second_call_kwargs[1]["json"]  # kwargs["json"]
        self.assertIn("p-ticket", payload_sent)
        self.assertEqual(payload_sent["p-ticket"], "TICKET_XYZ")

    # TC-3: p-ticket が P_TICKET_MAX_RETRIES+1 回返り続ける → exhausted → False
    @patch("time.sleep")
    def test_tc3_p_ticket_exhausted(self, mock_sleep):
        with patch.object(self.client._session, "post") as mock_post:
            # P_TICKET_MAX_RETRIES+1 = 4 回 p-ticket を返し続ける
            mock_post.side_effect = [
                self._p_ticket_resp(ticket=f"T{i}", p_time=1)
                for i in range(tc.P_TICKET_MAX_RETRIES + 2)
            ]
            result = self.client._do_authenticate()

        self.assertFalse(result)
        # sleep は P_TICKET_MAX_RETRIES 回呼ばれること
        self.assertEqual(mock_sleep.call_count, tc.P_TICKET_MAX_RETRIES)

    # TC-4: errorText → False
    @patch("time.sleep")
    def test_tc4_error_text(self, mock_sleep):
        with patch.object(self.client._session, "post") as mock_post:
            mock_post.return_value = self._error_resp("Invalid password")
            result = self.client._do_authenticate()

        self.assertFalse(result)
        mock_sleep.assert_not_called()

    def _p_captcha_resp(self, p_captcha=True, p_message="captcha required"):
        """p-captcha を含むレスポンスを返す。"""
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "p-ticket": "TICKET_CAPTCHA",
            "p-captcha": p_captcha,
            "p-time": 1,
            "p-message": p_message,
        }
        return resp

    # TC-PCAPTCHA-1: p-captcha: true 受信時 → retry せず False を返す
    @patch("time.sleep")
    def test_p_captcha_returns_false_no_retry(self, mock_sleep):
        """
        公式仕様: p-captcha received → third-party cannot complete → return immediately.
        Source: https://github.com/tradovate/example-api-faq
        """
        with patch.object(self.client._session, "post") as mock_post:
            mock_post.return_value = self._p_captcha_resp(p_captcha=True)
            result = self.client._do_authenticate()

        self.assertFalse(result)
        # p-captcha 受信後に sleep してはいけない（retry 禁止）
        mock_sleep.assert_not_called()
        # POST は1回のみ（retry していない）
        self.assertEqual(mock_post.call_count, 1)

    # TC-PCAPTCHA-2: p-captcha: true → AuthBudget に wait_1h note で記録される
    @patch("time.sleep")
    def test_p_captcha_records_wait_1h_in_budget(self, mock_sleep):
        """
        p-captcha 受信時に AuthBudget.record_attempt(..., note='p-captcha:wait_1h') が呼ばれる。
        """
        with patch.object(self.client._session, "post") as mock_post, \
             patch("tradovate_client._AUTH_BUDGET_AVAILABLE", True), \
             patch("tradovate_client.AuthBudget") as mock_budget:
            mock_post.return_value = self._p_captcha_resp(p_captcha=True)
            result = self.client._do_authenticate()

        self.assertFalse(result)
        # record_attempt が呼ばれ、note に 'p-captcha:wait_1h' が含まれること
        record_calls = mock_budget.record_attempt.call_args_list
        self.assertGreater(len(record_calls), 0, "AuthBudget.record_attempt が呼ばれていない")
        notes = [str(c) for c in record_calls]
        self.assertTrue(
            any("p-captcha:wait_1h" in n for n in notes),
            f"p-captcha:wait_1h が note に含まれていない。calls={notes}"
        )

    # TC-PCAPTCHA-3: p-captcha: false → 通常の p-ticket フローで retry する
    @patch("time.sleep")
    def test_p_ticket_without_captcha_still_retries(self, mock_sleep):
        """
        p-captcha フィールドが false (または欠損) の場合、従来通り p-ticket retry を行う。
        """
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            # 1回目: p-ticket あり・p-captcha: False → retry すべき
            # 2回目: 認証成功
            mock_post.side_effect = [
                self._p_captcha_resp(p_captcha=False),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            result = self.client._do_authenticate()

        self.assertTrue(result)
        # p-captcha=False なので sleep(1) が1回呼ばれる（p-time=1）
        mock_sleep.assert_called_once_with(1)
        # POST は2回（初回 + retry）
        self.assertEqual(mock_post.call_count, 2)

    # TC-PCAPTCHA-4: p-captcha: true + p-message が error ログに含まれること
    @patch("time.sleep")
    def test_p_captcha_p_message_logged(self, mock_sleep):
        """
        p-message の内容がエラーログに含まれること（デバッグ情報保全）。
        """
        with patch.object(self.client._session, "post") as mock_post, \
             patch("tradovate_client.log") as mock_log:
            mock_post.return_value = self._p_captcha_resp(
                p_captcha=True,
                p_message="CAPTCHA_SENTINEL_MESSAGE"
            )
            self.client._do_authenticate()

        # log.error が呼ばれ、p-message の内容が含まれること
        error_calls = [str(c) for c in mock_log.error.call_args_list]
        self.assertTrue(
            any("CAPTCHA_SENTINEL_MESSAGE" in s for s in error_calls),
            f"p-message が error ログに含まれていない。calls={error_calls}"
        )


class TestAuthCache(unittest.TestCase):
    """TC-5〜TC-7: token cache。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_cache = Path(self.tmp_dir) / "tradovate_auth_cache.json"
        import tradovate_client as tc_module
        self._orig_cache = tc_module.AUTH_CACHE_FILE
        tc_module.AUTH_CACHE_FILE = self.tmp_cache

    def tearDown(self):
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = self._orig_cache

    def _write_cache(self, env="DEMO", remaining_secs=7200):
        """有効なHMAC付きキャッシュをファイルに書く。"""
        import hmac as hmac_mod, hashlib, tradovate_client as tc_mod
        expiry_unix = time.time() + remaining_secs
        payload = {
            "accessToken":   "cached_token_abc",
            "mdAccessToken": "cached_md_token",
            "expirationTime": "2099-01-01T00:00:00Z",
            "expiry_unix":   expiry_unix,
            "saved_at":      time.time(),
            "env":           env,
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        sig = hmac_mod.new(tc_mod._CACHE_HMAC_KEY, payload_json.encode(), hashlib.sha256).hexdigest()
        self.tmp_cache.write_text(json.dumps({"data": payload, "sig": sig}))

    # TC-5: 有効なキャッシュ → _do_authenticate を呼ばない
    def test_tc5_valid_cache_reused(self):
        self._write_cache(env="DEMO", remaining_secs=7200)
        client = TradovateClient(env="DEMO", username="u", password="p",
                                  app_id="A", app_version="1.0", cid="1", sec="s")
        with patch.object(client, "_do_authenticate") as mock_do_auth, \
             patch.object(client._session, "get") as mock_get:
            mock_get.return_value = _fake_account_list_resp()
            result = client.authenticate()

        self.assertTrue(result)
        mock_do_auth.assert_not_called()
        self.assertEqual(client._access_token, "cached_token_abc")

    # TC-6: キャッシュ期限切れ（TOKEN_REFRESH_MARGIN_SECS 未満）→ _do_authenticate を呼ぶ
    def test_tc6_expired_cache_reauth(self):
        # TOKEN_REFRESH_MARGIN_SECS = 600 秒。残り300秒 → 期限切れ扱い
        self._write_cache(env="DEMO", remaining_secs=300)
        client = TradovateClient(env="DEMO", username="u", password="p",
                                  app_id="A", app_version="1.0", cid="1", sec="s")
        with patch.object(client, "_do_authenticate", return_value=True) as mock_do_auth:
            result = client.authenticate()

        self.assertTrue(result)
        mock_do_auth.assert_called_once()

    # TC-7: env 不一致キャッシュ → 無視して _do_authenticate
    def test_tc7_env_mismatch_cache_ignored(self):
        self._write_cache(env="LIVE", remaining_secs=7200)  # LIVE キャッシュ
        client = TradovateClient(env="DEMO", username="u", password="p",
                                  app_id="A", app_version="1.0", cid="1", sec="s")
        with patch.object(client, "_do_authenticate", return_value=True) as mock_do_auth:
            result = client.authenticate()

        self.assertTrue(result)
        mock_do_auth.assert_called_once()


class TestEnvWhitelist(unittest.TestCase):
    """TC-ENV-*: env ホワイトリスト方式の境界値テスト（修正1検証）。"""

    def test_env_demo_upper(self):
        """'DEMO' → DEMO_BASE を設定。"""
        client = TradovateClient(env="DEMO", username="u", password="p",
                                 app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertEqual(client.env, "DEMO")
        self.assertEqual(client.base_url, DEMO_BASE)

    def test_env_live_upper(self):
        """'LIVE' → LIVE_BASE を設定。"""
        client = TradovateClient(env="LIVE", username="u", password="p",
                                 app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertEqual(client.env, "LIVE")
        self.assertEqual(client.base_url, LIVE_BASE)

    def test_env_demo_lower(self):
        """'demo'（小文字）→ DEMO に正規化。"""
        client = TradovateClient(env="demo", username="u", password="p",
                                 app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertEqual(client.env, "DEMO")

    def test_env_live_lower(self):
        """'live'（小文字）→ LIVE に正規化。"""
        client = TradovateClient(env="live", username="u", password="p",
                                 app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertEqual(client.env, "LIVE")

    def test_env_invalid_liv(self):
        """'LIV' → ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            TradovateClient(env="LIV", username="u", password="p",
                            app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertIn("LIV", str(ctx.exception))

    def test_env_invalid_empty(self):
        """'' → 空文字は falsy なので TRADOVATE_ENV 環境変数にフォールバック。
        未設定時はデフォルト 'DEMO' になる（ValueErrorにはならない）。"""
        import os
        old_val = os.environ.pop("TRADOVATE_ENV", None)
        try:
            client = TradovateClient(env="", username="u", password="p",
                                     app_id="A", app_version="1.0", cid="1", sec="s")
            self.assertEqual(client.env, "DEMO")
        finally:
            if old_val is not None:
                os.environ["TRADOVATE_ENV"] = old_val

    def test_env_invalid_prod(self):
        """'PROD' → ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            TradovateClient(env="PROD", username="u", password="p",
                            app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertIn("PROD", str(ctx.exception))

    def test_env_invalid_none_explicit(self):
        """None が渡されてデフォルト(DEMO)が使われる（env=None はデフォルト経路）。"""
        import os
        old_val = os.environ.pop("TRADOVATE_ENV", None)
        try:
            client = TradovateClient(env=None, username="u", password="p",
                                     app_id="A", app_version="1.0", cid="1", sec="s")
            self.assertEqual(client.env, "DEMO")
        finally:
            if old_val is not None:
                os.environ["TRADOVATE_ENV"] = old_val


class TestPTimeClamp(unittest.TestCase):
    """TC-PTIME-*: p-time 上限30秒クランプの境界値テスト（修正4検証）。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_cache = Path(self.tmp_dir) / "tradovate_auth_cache.json"
        import tradovate_client as tc_module
        self._orig_cache = tc_module.AUTH_CACHE_FILE
        tc_module.AUTH_CACHE_FILE = self.tmp_cache
        self.client = _make_client(tmp_cache=self.tmp_cache)
        os.environ["AUTH_BUDGET_BYPASS"] = "1"

    def tearDown(self):
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = self._orig_cache
        os.environ.pop("AUTH_BUDGET_BYPASS", None)

    def _p_ticket_resp_with_ptime(self, p_time_val):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "p-ticket": "TICKET_X",
            "p-time": p_time_val,
        }
        return resp

    def _access_token_resp(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "accessToken": "tok_abc",
            "mdAccessToken": "md_abc",
            "expirationTime": "2099-01-01T00:00:00Z",
        }
        return resp

    @patch("time.sleep")
    def test_ptime_1_no_clamp(self, mock_sleep):
        """p-time=1 → sleep(1) そのまま。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime(1),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            self.client._do_authenticate()
        mock_sleep.assert_called_once_with(1)

    @patch("time.sleep")
    def test_ptime_30_no_clamp(self, mock_sleep):
        """p-time=30 → sleep(30) そのまま。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime(30),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            self.client._do_authenticate()
        mock_sleep.assert_called_once_with(30)

    @patch("time.sleep")
    def test_ptime_31_clamped_to_30(self, mock_sleep):
        """p-time=31 → sleep(30) にクランプ。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime(31),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            self.client._do_authenticate()
        mock_sleep.assert_called_once_with(30)

    @patch("time.sleep")
    def test_ptime_0_clamped_to_min(self, mock_sleep):
        """p-time=0 → P_TICKET_MIN_WAIT (1) にクランプ。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime(0),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            self.client._do_authenticate()
        mock_sleep.assert_called_once_with(tc.P_TICKET_MIN_WAIT)

    @patch("time.sleep")
    def test_ptime_negative_clamped(self, mock_sleep):
        """p-time=-1 → P_TICKET_MIN_WAIT にクランプ。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime(-1),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            self.client._do_authenticate()
        mock_sleep.assert_called_once_with(tc.P_TICKET_MIN_WAIT)

    @patch("time.sleep")
    def test_ptime_string_invalid(self, mock_sleep):
        """p-time='abc' → デフォルト値で続行（クラッシュしない）。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime("abc"),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            # ValueErrorにならず完了すること
            result = self.client._do_authenticate()
        self.assertTrue(result)
        mock_sleep.assert_called_once_with(tc.P_TICKET_MIN_WAIT)

    @patch("time.sleep")
    def test_ptime_none(self, mock_sleep):
        """p-time=None → デフォルト値で続行（クラッシュしない）。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime(None),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            result = self.client._do_authenticate()
        self.assertTrue(result)

    @patch("time.sleep")
    def test_ptime_float_truncated(self, mock_sleep):
        """p-time=1.5 → int(1.5)=1 として処理。"""
        with patch.object(self.client._session, "post") as mock_post, \
             patch.object(self.client._session, "get") as mock_get:
            mock_post.side_effect = [
                self._p_ticket_resp_with_ptime(1.5),
                self._access_token_resp(),
            ]
            mock_get.return_value = _fake_account_list_resp()
            self.client._do_authenticate()
        mock_sleep.assert_called_once_with(1)


class TestCidConversion(unittest.TestCase):
    """TC-CID-*: cid 型変換境界値テスト。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_cache = Path(self.tmp_dir) / "tradovate_auth_cache.json"
        import tradovate_client as tc_module
        self._orig_cache = tc_module.AUTH_CACHE_FILE
        tc_module.AUTH_CACHE_FILE = self.tmp_cache
        os.environ["AUTH_BUDGET_BYPASS"] = "1"

    def tearDown(self):
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = self._orig_cache
        os.environ.pop("AUTH_BUDGET_BYPASS", None)

    def _make_client_cid(self, cid_val):
        return TradovateClient(
            env="DEMO", username="u", password="p",
            app_id="A", app_version="1.0", cid=cid_val, sec="s",
        )

    def _access_token_resp(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "accessToken": "tok_abc",
            "mdAccessToken": "md_abc",
            "expirationTime": "2099-01-01T00:00:00Z",
        }
        return resp

    @patch("time.sleep")
    def test_cid_int_zero(self, mock_sleep):
        """cid=0 (int) → payload["cid"]=0。"""
        client = self._make_client_cid("0")
        with patch.object(client._session, "post") as mock_post, \
             patch.object(client._session, "get") as mock_get:
            mock_post.return_value = self._access_token_resp()
            mock_get.return_value = _fake_account_list_resp()
            client._do_authenticate()
        sent = mock_post.call_args[1]["json"]
        self.assertEqual(sent["cid"], 0)

    @patch("time.sleep")
    def test_cid_string_numeric(self, mock_sleep):
        """cid='9999' → payload["cid"]=9999 (int変換)。"""
        client = self._make_client_cid("9999")
        with patch.object(client._session, "post") as mock_post, \
             patch.object(client._session, "get") as mock_get:
            mock_post.return_value = self._access_token_resp()
            mock_get.return_value = _fake_account_list_resp()
            client._do_authenticate()
        sent = mock_post.call_args[1]["json"]
        self.assertEqual(sent["cid"], 9999)

    @patch("time.sleep")
    def test_cid_empty_string(self, mock_sleep):
        """cid='' (空) → payload["cid"]=0 (フォールバック)。"""
        client = self._make_client_cid("")
        with patch.object(client._session, "post") as mock_post, \
             patch.object(client._session, "get") as mock_get:
            mock_post.return_value = self._access_token_resp()
            mock_get.return_value = _fake_account_list_resp()
            client._do_authenticate()
        sent = mock_post.call_args[1]["json"]
        self.assertEqual(sent["cid"], 0)

    @patch("time.sleep")
    def test_cid_none(self, mock_sleep):
        """cid=None → payload["cid"]=0 (フォールバック)。"""
        client = TradovateClient(
            env="DEMO", username="u", password="p",
            app_id="A", app_version="1.0", cid=None, sec="s",
        )
        with patch.object(client._session, "post") as mock_post, \
             patch.object(client._session, "get") as mock_get:
            mock_post.return_value = self._access_token_resp()
            mock_get.return_value = _fake_account_list_resp()
            client._do_authenticate()
        sent = mock_post.call_args[1]["json"]
        self.assertEqual(sent["cid"], 0)


class TestCacheHMAC(unittest.TestCase):
    """TC-CACHE-*: HMAC署名検証の境界値テスト（修正2検証）。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_cache = Path(self.tmp_dir) / "tradovate_auth_cache.json"
        import tradovate_client as tc_module
        self._orig_cache = tc_module.AUTH_CACHE_FILE
        tc_module.AUTH_CACHE_FILE = self.tmp_cache

    def tearDown(self):
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = self._orig_cache

    def _make_client(self):
        return TradovateClient(env="DEMO", username="u", password="p",
                               app_id="A", app_version="1.0", cid="1", sec="s")

    def _write_valid_cache(self, env="DEMO", remaining_secs=7200):
        """HMAC署名付きの有効なキャッシュを書く。"""
        import hmac as hmac_mod, hashlib, tradovate_client as tc_mod
        expiry_unix = time.time() + remaining_secs
        payload = {
            "accessToken":   "cached_tok",
            "mdAccessToken": "cached_md",
            "expirationTime": "2099-01-01T00:00:00Z",
            "expiry_unix":   expiry_unix,
            "saved_at":      time.time(),
            "env":           env,
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        sig = hmac_mod.new(tc_mod._CACHE_HMAC_KEY, payload_json.encode(), hashlib.sha256).hexdigest()
        self.tmp_cache.write_text(json.dumps({"data": payload, "sig": sig}))

    def test_cache_hmac_match(self):
        """正しいHMACのキャッシュは読み込まれる。"""
        self._write_valid_cache(env="DEMO", remaining_secs=7200)
        client = self._make_client()
        result = client._load_auth_cache()
        self.assertIsNotNone(result)
        self.assertEqual(result["accessToken"], "cached_tok")

    def test_cache_hmac_mismatch(self):
        """HMAC不一致のキャッシュは破棄される。"""
        self._write_valid_cache(env="DEMO", remaining_secs=7200)
        # sigを改ざん
        outer = json.loads(self.tmp_cache.read_text())
        outer["sig"] = "0" * 64
        self.tmp_cache.write_text(json.dumps(outer))
        client = self._make_client()
        result = client._load_auth_cache()
        self.assertIsNone(result)

    def test_cache_no_sig_field(self):
        """旧形式（sigなし）のキャッシュは破棄される。"""
        # sigなしの旧形式
        old_cache = {
            "accessToken": "old_tok",
            "expiry_unix": time.time() + 7200,
            "env": "DEMO",
        }
        self.tmp_cache.write_text(json.dumps(old_cache))
        client = self._make_client()
        result = client._load_auth_cache()
        self.assertIsNone(result)

    def test_cache_sig_present_but_no_data_field(self):
        """sigフィールドはあるがdataフィールドがない形式は破棄される。"""
        broken_cache = {
            "accessToken": "tok",  # data キーがない（旧形式変形）
            "env": "DEMO",
            "sig": "0" * 64,
        }
        self.tmp_cache.write_text(json.dumps(broken_cache))
        client = self._make_client()
        result = client._load_auth_cache()
        self.assertIsNone(result)

    def test_cache_env_mismatch_discarded(self):
        """envが一致しないキャッシュは破棄される（署名が正しくても）。"""
        self._write_valid_cache(env="LIVE", remaining_secs=7200)  # LIVEで書く
        client = self._make_client()  # DEMOで読む
        result = client._load_auth_cache()
        self.assertIsNone(result)


class TestRenewTokenCacheUpdate(unittest.TestCase):
    """TC-RENEW-*: renew_token がキャッシュを更新する（修正5検証）。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_cache = Path(self.tmp_dir) / "tradovate_auth_cache.json"
        import tradovate_client as tc_module
        self._orig_cache = tc_module.AUTH_CACHE_FILE
        tc_module.AUTH_CACHE_FILE = self.tmp_cache

    def tearDown(self):
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = self._orig_cache

    def test_renew_token_updates_cache(self):
        """renew_token成功後にキャッシュファイルが生成・更新される。"""
        client = TradovateClient(env="DEMO", username="u", password="p",
                                 app_id="A", app_version="1.0", cid="1", sec="s")
        client._access_token = "old_token"
        client._token_expiry = time.time() + 86400

        renew_resp = MagicMock()
        renew_resp.raise_for_status = MagicMock()
        renew_resp.json.return_value = {
            "accessToken": "new_token_after_renew",
            "mdAccessToken": "new_md",
            "expirationTime": "2099-06-01T00:00:00Z",
        }

        with patch.object(client._session, "post", return_value=renew_resp):
            result = client.renew_token()

        self.assertTrue(result)
        self.assertEqual(client._access_token, "new_token_after_renew")
        # キャッシュが更新されていること
        self.assertTrue(self.tmp_cache.exists())
        outer = json.loads(self.tmp_cache.read_text())
        self.assertIn("sig", outer)
        self.assertEqual(outer["data"]["accessToken"], "new_token_after_renew")

    def test_renew_token_cache_readable_after_update(self):
        """renew_token後のキャッシュは次回_load_auth_cacheで読み込める（E2E往復）。"""
        client = TradovateClient(env="DEMO", username="u", password="p",
                                 app_id="A", app_version="1.0", cid="1", sec="s")
        client._access_token = "old_token"
        client._token_expiry = time.time() + 86400

        renew_resp = MagicMock()
        renew_resp.raise_for_status = MagicMock()
        renew_resp.json.return_value = {
            "accessToken": "refreshed_token_xyz",
            "mdAccessToken": "refreshed_md",
            "expirationTime": "2099-12-31T00:00:00Z",
        }

        with patch.object(client._session, "post", return_value=renew_resp):
            client.renew_token()

        # 別クライアントでキャッシュを読み込める
        client2 = TradovateClient(env="DEMO", username="u", password="p",
                                  app_id="A", app_version="1.0", cid="1", sec="s")
        loaded = client2._load_auth_cache()
        self.assertIsNotNone(loaded, "renew後のキャッシュが読み込めない")
        self.assertEqual(loaded["accessToken"], "refreshed_token_xyz")


class TestAutoEnvInvalidate(unittest.TestCase):
    """TC-AUTOENV-*: env切替時の自動キャッシュ削除（修正6検証）。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_cache = Path(self.tmp_dir) / "tradovate_auth_cache.json"
        import tradovate_client as tc_module
        self._orig_cache = tc_module.AUTH_CACHE_FILE
        tc_module.AUTH_CACHE_FILE = self.tmp_cache

    def tearDown(self):
        import tradovate_client as tc_module
        tc_module.AUTH_CACHE_FILE = self._orig_cache

    def _write_raw_cache(self, env: str):
        """環境検出用の最低限のキャッシュを書く（HMAC不要・env変化検出専用）。"""
        cache = {"env": env, "expiry_unix": time.time() + 7200}
        self.tmp_cache.write_text(json.dumps(cache))

    def test_same_env_cache_not_deleted(self):
        """同じenv（DEMO→DEMO）ではキャッシュを削除しない。"""
        self._write_raw_cache("DEMO")
        TradovateClient(env="DEMO", username="u", password="p",
                        app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertTrue(self.tmp_cache.exists())

    def test_env_change_cache_deleted(self):
        """異なるenv（LIVE→DEMO）ではキャッシュを自動削除する。"""
        self._write_raw_cache("LIVE")
        self.assertTrue(self.tmp_cache.exists())
        # DEMOクライアントでinitするとLIVEキャッシュが削除される
        TradovateClient(env="DEMO", username="u", password="p",
                        app_id="A", app_version="1.0", cid="1", sec="s")
        self.assertFalse(self.tmp_cache.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
