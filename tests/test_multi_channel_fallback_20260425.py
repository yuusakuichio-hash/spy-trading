"""tests/test_multi_channel_fallback_20260425.py — #14 Pushover monthly_exceeded fallback

対象:
  common_v3/notify/multi_channel_sender.py の 3 段 fallback 動作を全経路 mock で検証する。

カバー範囲:
  F-01: Pushover 成功 → Slack/Gmail を呼ばない
  F-02: Pushover 429 → Slack へ fallback → Slack 成功
  F-03: Pushover monthly_exceeded → Slack へ fallback → Slack 成功
  F-04: Pushover 失敗 + Slack 失敗 → Gmail へ fallback → Gmail 成功
  F-05: 全チャンネル失敗 → ok=False / delivered_by=None
  F-06: Pushover token 未設定 → Slack → Gmail fallback
  F-07: Slack のみ設定 (Pushover/Gmail 未設定) → Slack 成功
  F-08: Gmail のみ設定 → Gmail 成功
  F-09: priority=2 が Slack に :rotating_light: prefix として渡る
  F-10: FallbackResult.attempted に試行チャンネルが順序通り記録される
  F-11: FallbackResult.errors に失敗理由が記録される
  F-12: Pushover 例外発生 → Slack へ fallback (例外が伝播しない)
  F-13: Slack 例外発生 → Gmail へ fallback (例外が伝播しない)
  F-14: Gmail 例外発生 → ok=False で graceful return (例外が伝播しない)
  F-15: channel_status() が env var 設定状態を正確に反映する
  F-16: Pushover "banned" レスポンス → Slack fallback
  F-17: send_with_fallback は app_tag を受け取り例外を上げない (smoke)
  F-18: monthly_exceeded キーワード (大文字小文字混在) → Slack fallback

実行: python3 -m pytest tests/test_multi_channel_fallback_20260425.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

TRADING_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TRADING_DIR))

from common_v3.notify.multi_channel_sender import (
    FallbackResult,
    MonthlyExceededError,
    _gmail_send,
    _pushover_send,
    _slack_send,
    channel_status,
    send_with_fallback,
)


# ── ヘルパー: requests モック生成 ──────────────────────────────────────────────

def _make_resp(status_code: int, text: str = "") -> MagicMock:
    """requests.Response 相当の MagicMock を返す。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.ok = 200 <= status_code < 300
    return resp


# ── F-01: Pushover 成功 ───────────────────────────────────────────────────────

class TestF01PushoverSuccess:
    def test_pushover_success_no_slack_or_gmail(self, monkeypatch):
        """Pushover 成功時は Slack/Gmail を呼ばない。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok123")
        monkeypatch.setenv("PUSHOVER_USER", "usr456")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.return_value = _make_resp(200, '{"status":1}')
            result = send_with_fallback("title", "msg", priority=0)

        assert result.ok is True
        assert result.delivered_by == "pushover"
        assert "pushover" in result.attempted
        assert "slack" not in result.attempted
        assert "gmail" not in result.attempted
        # post は Pushover の 1 回のみ
        assert mock_req.post.call_count == 1


# ── F-02: Pushover 429 → Slack fallback ──────────────────────────────────────

class TestF02Pushover429SlackFallback:
    def test_pushover_429_falls_back_to_slack(self, monkeypatch):
        """Pushover が 429 → Slack へ fallback して成功する。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        call_counter = {"n": 0}

        def fake_post(url, **kwargs):
            call_counter["n"] += 1
            if "pushover" in url:
                return _make_resp(429, "rate limit")
            # Slack
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            result = send_with_fallback("title", "msg", priority=1)

        assert result.ok is True
        assert result.delivered_by == "slack"
        assert result.attempted == ["pushover", "slack"]
        assert "pushover" in result.errors


# ── F-03: Pushover monthly_exceeded → Slack ───────────────────────────────────

class TestF03MonthlyExceededSlack:
    def test_monthly_exceeded_body_falls_back_to_slack(self, monkeypatch):
        """Pushover レスポンスに monthly_exceeded が含まれる → Slack fallback。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                # 200 だが本文に monthly_exceeded を含む
                return _make_resp(200, '{"status":0,"errors":["monthly_exceeded"]}')
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "slack"
        assert result.errors.get("pushover") == "429_or_monthly_exceeded"

    def test_app_limit_exceeded_falls_back(self, monkeypatch):
        """'app limit exceeded' テキストが monthly_exceeded と同様に扱われる。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                return _make_resp(429, "app limit exceeded for this user")
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "slack"


# ── F-04: Pushover + Slack 失敗 → Gmail ──────────────────────────────────────

class TestF04PushoverSlackFailGmailSuccess:
    def test_both_fail_falls_back_to_gmail(self, monkeypatch):
        """Pushover + Slack 失敗 → Gmail で成功する。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "xxxx xxxx xxxx xxxx")
        monkeypatch.setenv("GMAIL_FROM", "test@gmail.com")
        monkeypatch.setenv("GMAIL_TO", "test@gmail.com")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                return _make_resp(429, "rate limit")
            # Slack 失敗
            return _make_resp(500, "error")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            with patch("smtplib.SMTP_SSL") as mock_smtp:
                smtp_instance = MagicMock()
                mock_smtp.return_value.__enter__ = MagicMock(return_value=smtp_instance)
                mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
                result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "gmail"
        assert result.attempted == ["pushover", "slack", "gmail"]


# ── F-05: 全チャンネル失敗 ────────────────────────────────────────────────────

class TestF05AllChannelsFail:
    def test_all_channels_fail_returns_not_ok(self, monkeypatch):
        """Pushover/Slack/Gmail 全て失敗 → ok=False / delivered_by=None。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
        monkeypatch.setenv("GMAIL_FROM", "test@gmail.com")
        monkeypatch.setenv("GMAIL_TO", "test@gmail.com")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.return_value = _make_resp(500, "error")
            with patch("smtplib.SMTP_SSL", side_effect=OSError("connection refused")):
                result = send_with_fallback("title", "msg")

        assert result.ok is False
        assert result.delivered_by is None
        assert len(result.attempted) == 3

    def test_no_channels_configured(self, monkeypatch):
        """全 env var 未設定 → ok=False で graceful return。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        result = send_with_fallback("title", "msg")
        assert result.ok is False
        assert result.delivered_by is None
        # 例外が上がらないこと (graceful)


# ── F-06: Pushover token 未設定 ───────────────────────────────────────────────

class TestF06PushoverTokenMissing:
    def test_missing_token_fallback_to_slack(self, monkeypatch):
        """PUSHOVER_OPS_TOKEN 未設定 → Slack へ fallback。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.return_value = _make_resp(200, "ok")
            result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "slack"


# ── F-07: Slack のみ設定 ─────────────────────────────────────────────────────

class TestF07SlackOnly:
    def test_slack_only_configured_delivers(self, monkeypatch):
        """Pushover/Gmail 未設定・Slack のみ設定 → Slack 成功。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.return_value = _make_resp(200, "ok")
            result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "slack"


# ── F-08: Gmail のみ設定 ─────────────────────────────────────────────────────

class TestF08GmailOnly:
    def test_gmail_only_configured_delivers(self, monkeypatch):
        """Pushover/Slack 未設定・Gmail のみ設定 → Gmail 成功。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
        monkeypatch.setenv("GMAIL_FROM", "test@gmail.com")
        monkeypatch.setenv("GMAIL_TO", "test@gmail.com")

        with patch("smtplib.SMTP_SSL") as mock_smtp:
            smtp_instance = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=smtp_instance)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "gmail"


# ── F-09: priority=2 → Slack :rotating_light: prefix ────────────────────────

class TestF09Priority2SlackPrefix:
    def test_priority2_slack_payload_has_rotating_light(self, monkeypatch):
        """priority=2 で Slack 送信時、ペイロードに :rotating_light: が含まれる。"""
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        captured: list[dict] = []

        def fake_post(url, json=None, **kwargs):
            captured.append(json or {})
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            _slack_send("urgent title", "urgent message", priority=2)

        assert len(captured) == 1
        assert ":rotating_light:" in captured[0].get("text", "")

    def test_priority0_slack_no_rotating_light(self, monkeypatch):
        """priority=0 では :rotating_light: が含まれない。"""
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        captured: list[dict] = []

        def fake_post(url, json=None, **kwargs):
            captured.append(json or {})
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            _slack_send("normal title", "normal message", priority=0)

        assert len(captured) == 1
        assert ":rotating_light:" not in captured[0].get("text", "")


# ── F-10: attempted リスト順序 ────────────────────────────────────────────────

class TestF10AttemptedOrder:
    def test_attempted_order_pushover_slack_gmail(self, monkeypatch):
        """全チャンネル失敗時の attempted は ['pushover', 'slack', 'gmail'] の順。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
        monkeypatch.setenv("GMAIL_FROM", "test@gmail.com")
        monkeypatch.setenv("GMAIL_TO", "test@gmail.com")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.return_value = _make_resp(429, "rate limit")
            with patch("smtplib.SMTP_SSL", side_effect=OSError("fail")):
                result = send_with_fallback("title", "msg")

        assert result.attempted == ["pushover", "slack", "gmail"]

    def test_attempted_stops_at_first_success(self, monkeypatch):
        """Slack 成功時は gmail は attempted に含まれない。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                return _make_resp(429, "rate")
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            result = send_with_fallback("title", "msg")

        assert result.attempted == ["pushover", "slack"]
        assert "gmail" not in result.attempted


# ── F-11: errors dict ────────────────────────────────────────────────────────

class TestF11ErrorsDict:
    def test_errors_dict_populated_on_failure(self, monkeypatch):
        """失敗したチャンネルは errors dict に記録される。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                return _make_resp(429, "rate limit")
            # Slack 成功
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            result = send_with_fallback("title", "msg")

        assert "pushover" in result.errors
        assert result.errors["pushover"] == "429_or_monthly_exceeded"
        assert "slack" not in result.errors  # slack は成功したので errors に含まれない


# ── F-12: Pushover 例外 → Slack fallback ─────────────────────────────────────

class TestF12PushoverExceptionFallback:
    def test_pushover_exception_falls_back_to_slack(self, monkeypatch):
        """Pushover で例外発生 → Slack へ fallback (例外が send_with_fallback に伝播しない)。

        _pushover_send は内部 try/except で ConnectionError を吸収して (False, False) を返す。
        send_with_fallback は send_failed として扱い Slack へ進む。
        """
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                raise ConnectionError("network error")
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            # 例外が上がらないことを確認
            result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "slack"
        # _pushover_send が内部で例外を吸収するため errors["pushover"] は "send_failed"
        assert "pushover" in result.errors


# ── F-13: Slack 例外 → Gmail fallback ────────────────────────────────────────

class TestF13SlackExceptionGmailFallback:
    def test_slack_exception_falls_back_to_gmail(self, monkeypatch):
        """Slack で例外発生 → Gmail へ fallback (例外が伝播しない)。

        _slack_send は内部 try/except で TimeoutError を吸収して False を返す。
        send_with_fallback は send_failed として扱い Gmail へ進む。
        """
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
        monkeypatch.setenv("GMAIL_FROM", "test@gmail.com")
        monkeypatch.setenv("GMAIL_TO", "test@gmail.com")

        call_n = {"n": 0}

        def fake_post(url, **kwargs):
            call_n["n"] += 1
            if "pushover" in url:
                return _make_resp(429, "rate")
            raise TimeoutError("slack timeout")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            with patch("smtplib.SMTP_SSL") as mock_smtp:
                smtp_instance = MagicMock()
                mock_smtp.return_value.__enter__ = MagicMock(return_value=smtp_instance)
                mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
                result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "gmail"
        # _slack_send が内部で例外を吸収するため errors["slack"] は "send_failed_or_not_configured"
        assert "slack" in result.errors


# ── F-14: Gmail 例外 → graceful return ───────────────────────────────────────

class TestF14GmailExceptionGraceful:
    def test_gmail_exception_graceful_return(self, monkeypatch):
        """Gmail で例外発生 → ok=False で graceful return (例外が伝播しない)。

        _gmail_send は内部 try/except で OSError を吸収して False を返す。
        send_with_fallback は send_failed として扱い ok=False で終了する。
        """
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")
        monkeypatch.setenv("GMAIL_FROM", "test@gmail.com")
        monkeypatch.setenv("GMAIL_TO", "test@gmail.com")

        with patch("smtplib.SMTP_SSL", side_effect=OSError("SMTP connection refused")):
            result = send_with_fallback("title", "msg")

        assert result.ok is False
        assert result.delivered_by is None
        # _gmail_send が内部で例外を吸収するため errors["gmail"] は "send_failed_or_not_configured"
        assert "gmail" in result.errors


# ── F-15: channel_status() ───────────────────────────────────────────────────

class TestF15ChannelStatus:
    def test_all_set(self, monkeypatch):
        """全 env var 設定済み → 全チャンネル True。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "pass")

        status = channel_status()
        assert status["pushover"] is True
        assert status["slack"] is True
        assert status["gmail"] is True

    def test_none_set(self, monkeypatch):
        """全 env var 未設定 → 全チャンネル False。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        status = channel_status()
        assert status["pushover"] is False
        assert status["slack"] is False
        assert status["gmail"] is False

    def test_partial_pushover(self, monkeypatch):
        """PUSHOVER_USER のみ設定 (TOKEN 未設定) → pushover=False。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.setenv("PUSHOVER_USER", "usr")

        status = channel_status()
        assert status["pushover"] is False


# ── F-16: Pushover "banned" → Slack fallback ─────────────────────────────────

class TestF16PushoverBannedFallback:
    def test_pushover_banned_falls_back_to_slack(self, monkeypatch):
        """Pushover レスポンスに 'banned' が含まれる → Slack へ fallback。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                return _make_resp(200, '{"status":0,"errors":["banned"]}')
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            result = send_with_fallback("title", "msg")

        assert result.ok is True
        assert result.delivered_by == "slack"


# ── F-17: send_with_fallback smoke ───────────────────────────────────────────

class TestF17SendWithFallbackSmoke:
    def test_app_tag_accepted_no_exception(self, monkeypatch):
        """app_tag を指定しても例外が上がらない。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        # 例外が上がらないこと (全チャンネル未設定でも graceful)
        result = send_with_fallback("smoke title", "smoke msg", priority=1, app_tag="Atlas")
        assert isinstance(result, FallbackResult)
        assert result.ok is False

    def test_returns_fallback_result_type(self, monkeypatch):
        """戻り値が FallbackResult 型であること。"""
        monkeypatch.delenv("PUSHOVER_OPS_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        monkeypatch.delenv("PUSHOVER_USER", raising=False)
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        result = send_with_fallback("t", "m")
        assert isinstance(result, FallbackResult)
        assert isinstance(result.attempted, list)
        assert isinstance(result.errors, dict)


# ── F-18: monthly_exceeded 大文字小文字混在 ──────────────────────────────────

class TestF18MonthlyExceededCaseInsensitive:
    def test_monthly_exceeded_mixed_case_falls_back(self, monkeypatch):
        """'Monthly_Exceeded' (大文字小文字混在) も fallback トリガーになる。"""
        monkeypatch.setenv("PUSHOVER_OPS_TOKEN", "tok")
        monkeypatch.setenv("PUSHOVER_USER", "usr")
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

        def fake_post(url, **kwargs):
            if "pushover" in url:
                # 大文字混在
                return _make_resp(200, '{"status":0,"errors":["Monthly_Exceeded"]}')
            return _make_resp(200, "ok")

        with patch("common_v3.notify.multi_channel_sender._requests") as mock_req:
            mock_req.post.side_effect = fake_post
            result = send_with_fallback("title", "msg")

        # monthly_exceeded キーワード検出は body.lower() で行うため大文字小文字を問わない
        assert result.ok is True
        assert result.delivered_by == "slack"
