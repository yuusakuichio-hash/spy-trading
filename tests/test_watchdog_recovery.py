"""tests/test_watchdog_recovery.py

watchdog 自己回復 + Pushover backoff のテスト。

テストケース:
  (a) 正常時 — 全ファイル新鮮 → _reset_recovery_state 呼び出し
  (b) 1回目kickstart成功 — attempt=0→1 で kickstart が呼ばれる
  (c) 3回失敗で人間通知 — attempt>=3 で priority=2 通知
  (d) backoff中にqueue追加 — 429 × 3回 → backoff → 次の send がキューへ
  (e) 時間帯ゲート: 窓内stale → recovery発動
  (f) 時間帯ゲート: 窓外stale → skip（recovery発動しない・通知しない）
  (g) 時間帯ゲート: 境界時刻ハンドリング（22:25 JST は窓内）

Atlas 版 (atlas_watchdog) も同等のテストを実施する。
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── プロジェクトルートをパスに追加 ────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# ── requests をモック（ネットワーク不要） ─────────────────────────────────────
_requests_mock = types.ModuleType("requests")
_ok_resp = MagicMock()
_ok_resp.ok = True
_ok_resp.status_code = 200
_ok_resp.text = ""
_requests_mock.post = MagicMock(return_value=_ok_resp)
sys.modules.setdefault("requests", _requests_mock)

import chronos_watchdog as cw
import atlas_watchdog as aw


# ─────────────────────────────────────────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────────────────────────────────────────
def _reset_cw_globals():
    """テスト間でモジュールレベル状態をリセットする（chronos_watchdog）。"""
    cw._pushover_consecutive_429 = 0
    cw._pushover_backoff_until = 0.0
    cw._last_health_alert = 0.0
    cw._last_health_check = 0.0


def _reset_aw_globals():
    """テスト間でモジュールレベル状態をリセットする（atlas_watchdog）。"""
    aw._pushover_consecutive_429 = 0
    aw._pushover_backoff_until = 0.0
    aw._last_health_alert = 0.0
    aw._last_health_check = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# chronos_watchdog テスト
# ─────────────────────────────────────────────────────────────────────────────
class TestChronosRecovery(unittest.TestCase):
    """chronos_watchdog._attempt_self_recovery のテスト。"""

    def setUp(self):
        _reset_cw_globals()
        # RECOVERY_STATE_PATH が一時ディレクトリを向くようにする
        self._orig_recovery_path = cw.RECOVERY_STATE_PATH
        self._tmp_state = Path("/tmp/cw_recovery_state_test.json")
        if self._tmp_state.exists():
            self._tmp_state.unlink()
        cw.RECOVERY_STATE_PATH = self._tmp_state

    def tearDown(self):
        cw.RECOVERY_STATE_PATH = self._orig_recovery_path
        if self._tmp_state.exists():
            self._tmp_state.unlink()

    # ── (a) 正常時: _reset_recovery_state が呼ばれること ─────────────────────
    def test_a_healthy_resets_recovery_state(self):
        """run_health_check で全ファイル正常 → recovered=True に reset。"""
        with (
            patch("chronos_watchdog.pushover_send") as mock_push,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.stat") as mock_stat,
        ):
            st = MagicMock()
            st.st_size = 1000
            st.st_mtime = time.time()  # 現在時刻 → age < 600
            mock_stat.return_value = st

            cw.run_health_check([Path("/fake/chronos.log")])

            state = json.loads(self._tmp_state.read_text())
            self.assertEqual(state["attempt"], 0)
            self.assertTrue(state["recovered"])
            # 正常時は pushover を呼ばない
            mock_push.assert_not_called()

    # ── (b) 1回目kickstart ────────────────────────────────────────────────────
    def test_b_first_stale_calls_kickstart(self):
        """更新停止 1回目検知 → launchctl kickstart が実行される。"""
        with (
            patch("chronos_watchdog.pushover_send") as mock_push,
            patch("subprocess.run") as mock_sub,
        ):
            mock_sub.return_value = MagicMock(returncode=0, stdout="", stderr="")

            cw._attempt_self_recovery("更新停止(700秒): chronos.log")

            # subprocess.run が ["launchctl", "kickstart", ...] で呼ばれたか
            args_list = mock_sub.call_args_list
            self.assertTrue(len(args_list) >= 1)
            first_call_args = args_list[0][0][0]  # positional arg[0] = command list
            self.assertIn("kickstart", first_call_args)

            # attempt=1 の pushover が送信されたか
            mock_push.assert_called_once()
            title = mock_push.call_args[0][0]
            self.assertIn("attempt=1", title)

            # state が保存されていること
            state = json.loads(self._tmp_state.read_text())
            self.assertEqual(state["attempt"], 1)

    # ── (c) 3回失敗で人間通知 (priority=2) ───────────────────────────────────
    def test_c_third_attempt_escalates_to_human(self):
        """3回目の attempt で priority=2 の人間介入通知が送信される。"""
        # 既に 2 回試みた状態にセット（クールダウン済み）
        past = time.time() - 700  # 700秒前 > RECOVERY_COOLDOWN_SEC(600秒)
        cw._save_recovery_state({
            "attempt": 2,
            "last_attempt_ts": past,
            "recovered": False,
        })

        with (
            patch("chronos_watchdog.pushover_send") as mock_push,
            patch("subprocess.run"),
        ):
            cw._attempt_self_recovery("更新停止(2000秒): chronos.log")

            state = json.loads(self._tmp_state.read_text())
            self.assertEqual(state["attempt"], 3)

            # priority=2 の呼び出しが存在する
            calls_priority2 = [
                c for c in mock_push.call_args_list
                if c[1].get("priority", c[0][2] if len(c[0]) > 2 else 1) == 2
                or (len(c[0]) > 2 and c[0][2] == 2)
            ]
            # 直接 priority キーワード or positional で 2 が渡っていること
            found_p2 = any(
                (c.kwargs.get("priority") == 2 or
                 (len(c.args) > 2 and c.args[2] == 2))
                for c in mock_push.call_args_list
            )
            self.assertTrue(found_p2, "priority=2 の通知が送信されていない")

    # ── クールダウン中は再試行しない ─────────────────────────────────────────
    def test_cooldown_prevents_retry(self):
        """前回試行から 10分未満なら再試行しない。"""
        recent = time.time() - 60  # 60秒前 < RECOVERY_COOLDOWN_SEC(600秒)
        cw._save_recovery_state({
            "attempt": 1,
            "last_attempt_ts": recent,
            "recovered": False,
        })

        with patch("subprocess.run") as mock_sub:
            cw._attempt_self_recovery("更新停止(700秒): chronos.log")
            mock_sub.assert_not_called()

        state = json.loads(self._tmp_state.read_text())
        # attempt は変化しない
        self.assertEqual(state["attempt"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# chronos_watchdog Pushover backoff テスト
# NOTE: 共通クライアント導入後、backoff は common.pushover_client が一元管理する。
#       テストは「フォールバックパス（_PC_AVAILABLE=False）」で旧ロジックを検証する。
#       共通クライアント経由の backoff テストは tests/test_pushover_client.py で実施。
# ─────────────────────────────────────────────────────────────────────────────
class TestChronosPushoverBackoff(unittest.TestCase):
    """chronos_watchdog.pushover_send のフォールバックパス 429 backoff テスト。"""

    def setUp(self):
        _reset_cw_globals()
        # 共通クライアントを無効化してフォールバックパスを通す
        self._orig_pc_available = cw._PC_AVAILABLE
        cw._PC_AVAILABLE = False
        # フォールバックパスは PUSHOVER_TOKEN/USER が必要
        self._orig_token = cw.PUSHOVER_TOKEN
        self._orig_user  = cw.PUSHOVER_USER
        cw.PUSHOVER_TOKEN = "test_token_fallback"
        cw.PUSHOVER_USER  = "test_user_fallback"
        self._orig_backoff_path = cw.PUSHOVER_BACKOFF_STATE_PATH
        self._orig_queue_path   = cw.PUSHOVER_QUEUE_PATH
        self._tmp_backoff = Path("/tmp/cw_backoff_state_test.json")
        self._tmp_queue   = Path("/tmp/cw_pushover_queue_test.jsonl")
        for p in [self._tmp_backoff, self._tmp_queue]:
            if p.exists():
                p.unlink()
        cw.PUSHOVER_BACKOFF_STATE_PATH = self._tmp_backoff
        cw.PUSHOVER_QUEUE_PATH         = self._tmp_queue

    def tearDown(self):
        cw._PC_AVAILABLE               = self._orig_pc_available
        cw.PUSHOVER_TOKEN              = self._orig_token
        cw.PUSHOVER_USER               = self._orig_user
        cw.PUSHOVER_BACKOFF_STATE_PATH = self._orig_backoff_path
        cw.PUSHOVER_QUEUE_PATH         = self._orig_queue_path
        for p in [self._tmp_backoff, self._tmp_queue]:
            if p.exists():
                p.unlink()
        _reset_cw_globals()

    # ── (d) 429 × 3回 → backoff → 次の send がキューへ ──────────────────────
    def test_d_backoff_after_three_429_queues_next(self):
        """フォールバックパス: 429 を 3 回受信 → backoff → 次の通知がキューに追記される。

        _PC_AVAILABLE=False のフォールバックパスを検証。
        cw.requests.post を直接パッチすることで他テストのモック差し替えと干渉しない。
        """
        self.assertFalse(cw._PC_AVAILABLE, "_PC_AVAILABLE=False のフォールバックパスのテスト")

        resp_429 = MagicMock()
        resp_429.ok = False
        resp_429.status_code = 429
        resp_429.text = "rate limited"

        # cw.requests.post を直接パッチ（他テストのモック差し替えとの干渉を回避）
        with patch.object(cw.requests, "post", return_value=resp_429):
            for _ in range(cw.PUSHOVER_429_MAX_CONSECUTIVE):
                cw.pushover_send("test title", "test message", priority=1)

        # consecutive_429 が上限に達したことを確認
        self.assertGreaterEqual(
            cw._pushover_consecutive_429,
            cw.PUSHOVER_429_MAX_CONSECUTIVE,
            "consecutive_429 が最大値に達していない",
        )
        # キューに少なくとも 1 件追記されていること
        self.assertTrue(self._tmp_queue.exists(), "キューファイルが作成されていない")
        lines = [l for l in self._tmp_queue.read_text().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 1, "キューにエントリが追記されていない")
        entry = json.loads(lines[-1])
        self.assertIn("test title", entry.get("title", ""))

    def test_normal_send_resets_consecutive_counter(self):
        """正常送信後は consecutive_429 カウンタがリセットされる。"""
        cw._pushover_consecutive_429 = 2

        resp_ok = MagicMock()
        resp_ok.ok = True
        resp_ok.status_code = 200

        with patch.object(_requests_mock, "post", return_value=resp_ok):
            cw.pushover_send("ok title", "ok msg", priority=0)

        self.assertEqual(cw._pushover_consecutive_429, 0)

    def test_backoff_active_skips_http(self):
        """backoff 期間中は HTTP を叩かずキューに追記する。"""
        cw._pushover_backoff_until = time.time() + 1000  # 未来

        with patch.object(_requests_mock, "post") as mock_post:
            cw.pushover_send("skip title", "skip msg", priority=1)
            mock_post.assert_not_called()

        lines = [l for l in self._tmp_queue.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)


# ─────────────────────────────────────────────────────────────────────────────
# atlas_watchdog テスト（chronos のミラー検証）
# ─────────────────────────────────────────────────────────────────────────────
class TestAtlasRecovery(unittest.TestCase):
    """atlas_watchdog._attempt_self_recovery のテスト。"""

    def setUp(self):
        _reset_aw_globals()
        self._orig_recovery_path = aw.RECOVERY_STATE_PATH
        self._tmp_state = Path("/tmp/aw_recovery_state_test.json")
        if self._tmp_state.exists():
            self._tmp_state.unlink()
        aw.RECOVERY_STATE_PATH = self._tmp_state

    def tearDown(self):
        aw.RECOVERY_STATE_PATH = self._orig_recovery_path
        if self._tmp_state.exists():
            self._tmp_state.unlink()

    def test_a_healthy_resets_recovery_state(self):
        """run_health_check で全ファイル正常 → recovered=True に reset。"""
        with (
            patch("atlas_watchdog.pushover_send") as mock_push,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.stat") as mock_stat,
        ):
            st = MagicMock()
            st.st_size = 500
            st.st_mtime = time.time()
            mock_stat.return_value = st

            aw.run_health_check(["/fake/condor.log"])

            state = json.loads(self._tmp_state.read_text())
            self.assertEqual(state["attempt"], 0)
            self.assertTrue(state["recovered"])
            mock_push.assert_not_called()

    def test_b_first_stale_calls_kickstart(self):
        """更新停止 1回目検知 → launchctl kickstart が実行される。"""
        with (
            patch("atlas_watchdog.pushover_send") as mock_push,
            patch("subprocess.run") as mock_sub,
        ):
            mock_sub.return_value = MagicMock(returncode=0, stdout="", stderr="")

            aw._attempt_self_recovery("更新停止(700秒): condor.log")

            args_list = mock_sub.call_args_list
            self.assertTrue(len(args_list) >= 1)
            first_cmd = args_list[0][0][0]
            self.assertIn("kickstart", first_cmd)

            mock_push.assert_called_once()
            title = mock_push.call_args[0][0]
            self.assertIn("attempt=1", title)

            state = json.loads(self._tmp_state.read_text())
            self.assertEqual(state["attempt"], 1)

    def test_c_third_attempt_escalates_to_human(self):
        """3回目の attempt で priority=2 の人間介入通知。"""
        past = time.time() - 700
        aw._save_recovery_state({
            "attempt": 2,
            "last_attempt_ts": past,
            "recovered": False,
        })

        with (
            patch("atlas_watchdog.pushover_send") as mock_push,
            patch("subprocess.run"),
        ):
            aw._attempt_self_recovery("更新停止(2000秒): condor.log")

            state = json.loads(self._tmp_state.read_text())
            self.assertEqual(state["attempt"], 3)

            found_p2 = any(
                (c.kwargs.get("priority") == 2 or
                 (len(c.args) > 2 and c.args[2] == 2))
                for c in mock_push.call_args_list
            )
            self.assertTrue(found_p2, "priority=2 の通知が送信されていない")

    def test_cooldown_prevents_retry(self):
        """前回試行から 10分未満なら再試行しない。"""
        recent = time.time() - 60
        aw._save_recovery_state({
            "attempt": 1,
            "last_attempt_ts": recent,
            "recovered": False,
        })

        with patch("subprocess.run") as mock_sub:
            aw._attempt_self_recovery("更新停止(700秒): condor.log")
            mock_sub.assert_not_called()

        state = json.loads(self._tmp_state.read_text())
        self.assertEqual(state["attempt"], 1)


class TestAtlasPushoverBackoff(unittest.TestCase):
    """atlas_watchdog.pushover_send のフォールバックパス 429 backoff テスト。

    NOTE: 共通クライアント導入後、backoff は common.pushover_client が一元管理する。
          テストは _PC_AVAILABLE=False でフォールバックパスを検証する。
    """

    def setUp(self):
        _reset_aw_globals()
        # 共通クライアントを無効化してフォールバックパスを通す
        self._orig_pc_available = aw._PC_AVAILABLE
        aw._PC_AVAILABLE = False
        # フォールバックパスは PUSHOVER_TOKEN/USER が必要
        self._orig_token = aw.PUSHOVER_TOKEN
        self._orig_user  = aw.PUSHOVER_USER
        aw.PUSHOVER_TOKEN = "test_token_fallback"
        aw.PUSHOVER_USER  = "test_user_fallback"
        self._orig_backoff_path = aw.PUSHOVER_BACKOFF_STATE_PATH
        self._orig_queue_path   = aw.PUSHOVER_QUEUE_PATH
        self._tmp_backoff = Path("/tmp/aw_backoff_state_test.json")
        self._tmp_queue   = Path("/tmp/aw_pushover_queue_test.jsonl")
        for p in [self._tmp_backoff, self._tmp_queue]:
            if p.exists():
                p.unlink()
        aw.PUSHOVER_BACKOFF_STATE_PATH = self._tmp_backoff
        aw.PUSHOVER_QUEUE_PATH         = self._tmp_queue

    def tearDown(self):
        aw._PC_AVAILABLE               = self._orig_pc_available
        aw.PUSHOVER_TOKEN              = self._orig_token
        aw.PUSHOVER_USER               = self._orig_user
        aw.PUSHOVER_BACKOFF_STATE_PATH = self._orig_backoff_path
        aw.PUSHOVER_QUEUE_PATH         = self._orig_queue_path
        for p in [self._tmp_backoff, self._tmp_queue]:
            if p.exists():
                p.unlink()
        _reset_aw_globals()

    def test_d_backoff_after_three_429_queues_next(self):
        """フォールバックパス: 429 を 3 回受信 → backoff → キューに追記される。"""
        self.assertFalse(aw._PC_AVAILABLE, "_PC_AVAILABLE=False のフォールバックパスのテスト")

        resp_429 = MagicMock()
        resp_429.ok = False
        resp_429.status_code = 429
        resp_429.text = "rate limited"

        # aw.requests.post を直接パッチ（他テストのモック差し替えとの干渉を回避）
        with patch.object(aw.requests, "post", return_value=resp_429):
            for _ in range(aw.PUSHOVER_429_MAX_CONSECUTIVE):
                aw.pushover_send("test title", "test message", priority=1)

        # consecutive_429 が上限に達したことを確認
        self.assertGreaterEqual(
            aw._pushover_consecutive_429,
            aw.PUSHOVER_429_MAX_CONSECUTIVE,
            "consecutive_429 が最大値に達していない",
        )
        # キューに少なくとも 1 件追記されていること
        self.assertTrue(self._tmp_queue.exists(), "キューファイルが作成されていない")
        lines = [l for l in self._tmp_queue.read_text().splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 1, "キューにエントリが追記されていない")
        entry = json.loads(lines[-1])
        self.assertIn("test title", entry.get("title", ""))

    def test_backoff_active_skips_http(self):
        """backoff 期間中は HTTP を叩かない。"""
        aw._pushover_backoff_until = time.time() + 1000

        with patch.object(_requests_mock, "post") as mock_post:
            aw.pushover_send("skip", "msg", priority=1)
            mock_post.assert_not_called()

        lines = [l for l in self._tmp_queue.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)


# ─────────────────────────────────────────────────────────────────────────────
# _load_backoff_state 永続化テスト
# ─────────────────────────────────────────────────────────────────────────────
class TestBackoffStatePersistence(unittest.TestCase):
    """backoff_state.json の読み書きが正しく機能すること。"""

    def test_chronos_backoff_state_roundtrip(self):
        tmp = Path("/tmp/cw_backoff_roundtrip.json")
        try:
            _reset_cw_globals()
            cw.PUSHOVER_BACKOFF_STATE_PATH = tmp
            cw._pushover_consecutive_429   = 2
            cw._pushover_backoff_until     = 9999999.0
            cw._save_backoff_state()

            # リセット後に読み込む
            cw._pushover_consecutive_429 = 0
            cw._pushover_backoff_until   = 0.0
            cw._load_backoff_state()

            self.assertEqual(cw._pushover_consecutive_429, 2)
            self.assertAlmostEqual(cw._pushover_backoff_until, 9999999.0, places=1)
        finally:
            if tmp.exists():
                tmp.unlink()

    def test_atlas_backoff_state_roundtrip(self):
        tmp = Path("/tmp/aw_backoff_roundtrip.json")
        try:
            _reset_aw_globals()
            aw.PUSHOVER_BACKOFF_STATE_PATH = tmp
            aw._pushover_consecutive_429   = 3
            aw._pushover_backoff_until     = 8888888.0
            aw._save_backoff_state()

            aw._pushover_consecutive_429 = 0
            aw._pushover_backoff_until   = 0.0
            aw._load_backoff_state()

            self.assertEqual(aw._pushover_consecutive_429, 3)
            self.assertAlmostEqual(aw._pushover_backoff_until, 8888888.0, places=1)
        finally:
            if tmp.exists():
                tmp.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# 時間帯ゲートテスト（3件追加）
# ─────────────────────────────────────────────────────────────────────────────
class TestWatchWindowGate(unittest.TestCase):
    """chronos_watchdog / atlas_watchdog の時間帯ゲート (_is_in_watch_window) テスト。

    (e) 窓内時間 + stale → recovery発動
    (f) 窓外時間 + stale → skip（recovery発動しない・通知しない）
    (g) 境界時刻ハンドリング（22:25 JST は 22:20〜05:10 窓内）
    """

    def setUp(self):
        _reset_cw_globals()
        _reset_aw_globals()
        self._orig_cw_recovery_path = cw.RECOVERY_STATE_PATH
        self._orig_aw_recovery_path = aw.RECOVERY_STATE_PATH
        self._cw_tmp = Path("/tmp/cw_gate_recovery_test.json")
        self._aw_tmp = Path("/tmp/aw_gate_recovery_test.json")
        for p in [self._cw_tmp, self._aw_tmp]:
            if p.exists():
                p.unlink()
        cw.RECOVERY_STATE_PATH = self._cw_tmp
        aw.RECOVERY_STATE_PATH = self._aw_tmp

    def tearDown(self):
        cw.RECOVERY_STATE_PATH = self._orig_cw_recovery_path
        aw.RECOVERY_STATE_PATH = self._orig_aw_recovery_path
        for p in [self._cw_tmp, self._aw_tmp]:
            if p.exists():
                p.unlink()

    # ── (e) 窓内時間 + stale → recovery発動 ─────────────────────────────────
    def test_e_window_inside_stale_triggers_recovery(self):
        """市場時間内(JST 23:00)で stale ファイルを検知 → _attempt_self_recovery が呼ばれる。"""
        # JST 23:00 = 窓内 ("22:20" → "05:10")
        from datetime import datetime, timezone, timedelta
        fake_jst = datetime(2026, 4, 20, 23, 0, 0, tzinfo=timezone(timedelta(hours=9)))

        with (
            patch("chronos_watchdog.datetime") as mock_dt,
            patch("chronos_watchdog.pushover_send"),
            patch("chronos_watchdog._attempt_self_recovery") as mock_recovery,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.stat") as mock_stat,
        ):
            mock_dt.now.return_value = fake_jst
            st = MagicMock()
            st.st_size = 100
            st.st_mtime = time.time() - 700  # 700秒前 = stale
            mock_stat.return_value = st

            # chronos.log は市場時間帯のみ監視
            cw.run_health_check([Path("/fake/chronos.log")])

            # _attempt_self_recovery が呼ばれたことを確認
            mock_recovery.assert_called_once()
            call_label = mock_recovery.call_args[0][0]
            self.assertIn("更新停止", call_label)

    # ── (f) 窓外時間 + stale → skip ──────────────────────────────────────────
    def test_f_window_outside_stale_skips_recovery(self):
        """CME先物クローズ時間（土曜 10:00 JST）で stale → recovery もアラートも発動しない。

        旧テスト: JST 07:41 月曜 = SPX オプション窓外。
        新テスト: 土曜 10:00 JST = CME 先物週末クローズ（窓外）。
          月曜 07:41 JST は CME 先物開場中のため、先物仕様では窓内となる。
        """
        from datetime import datetime, timezone, timedelta
        # 土曜 10:00 JST = CME 先物週末クローズ（土曜 06:00 以降はクローズ）
        fake_jst = datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone(timedelta(hours=9)))  # 2026-04-25 土曜

        with (
            patch("chronos_watchdog.datetime") as mock_dt,
            patch("chronos_watchdog.pushover_send") as mock_push,
            patch("chronos_watchdog._attempt_self_recovery") as mock_recovery,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.stat") as mock_stat,
        ):
            mock_dt.now.return_value = fake_jst
            st = MagicMock()
            st.st_size = 100
            st.st_mtime = time.time() - 115200  # 32時間前 = stale
            mock_stat.return_value = st

            # chronos.log エントリを WATCH_TARGETS から取得して path を合わせる
            chronos_path = cw.WATCH_TARGETS[0]["path"]
            cw.run_health_check([chronos_path])

            # recovery も通知も発動しないこと
            mock_recovery.assert_not_called()
            mock_push.assert_not_called()

    # ── (g) 境界時刻ハンドリング: 22:25 JST は窓内 ──────────────────────────
    def test_g_boundary_22_25_jst_is_inside_window(self):
        """22:25 JST は ("22:20", "05:10") 窓内と判定されること。"""
        from datetime import datetime, timezone, timedelta

        # _is_in_watch_window を直接テスト（モジュールのユーティリティ関数）
        fake_jst = datetime(2026, 4, 20, 22, 25, 0, tzinfo=timezone(timedelta(hours=9)))
        windows = [("22:20", "05:10")]

        with patch("chronos_watchdog.datetime") as mock_dt:
            mock_dt.now.return_value = fake_jst
            result = cw._is_in_watch_window(windows)

        self.assertTrue(result, "22:25 JST は市場時間帯窓内のはず")

        # 05:15 JST は窓外のはず
        fake_outside = datetime(2026, 4, 20, 5, 15, 0, tzinfo=timezone(timedelta(hours=9)))
        with patch("chronos_watchdog.datetime") as mock_dt:
            mock_dt.now.return_value = fake_outside
            result_outside = cw._is_in_watch_window(windows)

        self.assertFalse(result_outside, "05:15 JST は市場時間帯窓外のはず")


# ─────────────────────────────────────────────────────────────────────────────
# CME先物 時間帯ゲートテスト（7件）
# 正しい先物時間帯: 月曜 07:00 JST 〜 土曜 06:00 JST / デイリー休止 06:00-07:00 JST
# ─────────────────────────────────────────────────────────────────────────────
class TestCMEFuturesWindowGate(unittest.TestCase):
    """CME E-mini 先物 Globex 時間帯ゲート (_is_in_futures_window_jst) の 7ケーステスト。

    テスト設計根拠 (CME公式):
      Sunday 6:00 PM ET – Friday 5:00 PM ET (Globex)
      Daily break: 5:00-6:00 PM ET each day
      JST換算 (夏時間 EDT+13h):
        開場: 月曜 07:00 JST
        閉場: 土曜 06:00 JST
        デイリー休止: 毎日 06:00-07:00 JST
    """

    def _make_jst(self, year, month, day, hour, minute):
        """JST datetime を生成するヘルパー。"""
        from datetime import datetime, timezone, timedelta
        return datetime(year, month, day, hour, minute, 0,
                        tzinfo=timezone(timedelta(hours=9)))

    def test_1_monday_07_01_jst_is_open(self):
        """(1) 月曜 07:01 JST → 窓内（週オープン直後）。"""
        # 2026-04-20 は月曜
        now = self._make_jst(2026, 4, 20, 7, 1)
        result = cw._is_in_futures_window_jst(now)
        self.assertTrue(result, "月曜 07:01 JST は CME 先物窓内のはず")

    def test_2_saturday_05_59_jst_is_open(self):
        """(2) 土曜 05:59 JST → 窓内（週クローズ直前）。"""
        # 2026-04-25 は土曜
        now = self._make_jst(2026, 4, 25, 5, 59)
        result = cw._is_in_futures_window_jst(now)
        self.assertTrue(result, "土曜 05:59 JST は CME 先物窓内のはず")

    def test_3_saturday_06_01_jst_is_closed(self):
        """(3) 土曜 06:01 JST → 窓外（週末クローズ）。"""
        # 2026-04-25 は土曜
        now = self._make_jst(2026, 4, 25, 6, 1)
        result = cw._is_in_futures_window_jst(now)
        self.assertFalse(result, "土曜 06:01 JST は週末クローズ（窓外）のはず")

    def test_4_sunday_12_00_jst_is_closed(self):
        """(4) 日曜 12:00 JST → 窓外（日曜は全日クローズ）。"""
        # 2026-04-26 は日曜
        now = self._make_jst(2026, 4, 26, 12, 0)
        result = cw._is_in_futures_window_jst(now)
        self.assertFalse(result, "日曜 12:00 JST は全日クローズ（窓外）のはず")

    def test_5_tuesday_06_30_jst_is_daily_break(self):
        """(5) 火曜 06:30 JST → 窓外（デイリー休止 06:00-07:00）。"""
        # 2026-04-21 は火曜
        now = self._make_jst(2026, 4, 21, 6, 30)
        result = cw._is_in_futures_window_jst(now)
        self.assertFalse(result, "火曜 06:30 JST はデイリー休止中（窓外）のはず")

    def test_6_tuesday_07_01_jst_is_open_after_break(self):
        """(6) 火曜 07:01 JST → 窓内（デイリー休止明け）。"""
        # 2026-04-21 は火曜
        now = self._make_jst(2026, 4, 21, 7, 1)
        result = cw._is_in_futures_window_jst(now)
        self.assertTrue(result, "火曜 07:01 JST はデイリー休止明け（窓内）のはず")

    def test_7_monday_06_59_jst_is_closed_before_open(self):
        """(7) 月曜 06:59 JST → 窓外（週オープン前 かつ デイリー休止内）。"""
        # 2026-04-20 は月曜
        now = self._make_jst(2026, 4, 20, 6, 59)
        result = cw._is_in_futures_window_jst(now)
        self.assertFalse(result, "月曜 06:59 JST は週オープン前（窓外）のはず")

    def test_boundary_saturday_06_00_is_closed(self):
        """土曜 06:00 JST ちょうど → 窓外（境界値: 閉場開始）。"""
        now = self._make_jst(2026, 4, 25, 6, 0)
        result = cw._is_in_futures_window_jst(now)
        self.assertFalse(result, "土曜 06:00 JST ちょうどは週末クローズ開始（窓外）のはず")

    def test_boundary_monday_07_00_is_open(self):
        """月曜 07:00 JST ちょうど → 窓内（境界値: 週オープン）。"""
        now = self._make_jst(2026, 4, 20, 7, 0)
        result = cw._is_in_futures_window_jst(now)
        self.assertTrue(result, "月曜 07:00 JST ちょうどは週オープン（窓内）のはず")

    def test_wednesday_midday_is_open(self):
        """水曜昼間 (12:00 JST) → 窓内（通常取引中）。"""
        now = self._make_jst(2026, 4, 22, 12, 0)
        result = cw._is_in_futures_window_jst(now)
        self.assertTrue(result, "水曜 12:00 JST は通常取引中（窓内）のはず")

    def test_daily_break_start_06_00_is_closed(self):
        """デイリー休止開始境界: 水曜 06:00 JST → 窓外。"""
        now = self._make_jst(2026, 4, 22, 6, 0)
        result = cw._is_in_futures_window_jst(now)
        self.assertFalse(result, "水曜 06:00 JST はデイリー休止開始（窓外）のはず")

    def test_daily_break_end_06_59_is_closed(self):
        """デイリー休止中: 水曜 06:59 JST → 窓外。"""
        now = self._make_jst(2026, 4, 22, 6, 59)
        result = cw._is_in_futures_window_jst(now)
        self.assertFalse(result, "水曜 06:59 JST はデイリー休止中（窓外）のはず")


# ─────────────────────────────────────────────────────────────────────────────
# market_calendar モジュール単体テスト
# ─────────────────────────────────────────────────────────────────────────────
class TestMarketCalendarCMEFutures(unittest.TestCase):
    """common.market_calendar.is_in_market_hours("cme_futures", ...) の単体テスト。"""

    def setUp(self):
        """market_calendar をインポートできない場合はスキップ。"""
        try:
            from common.market_calendar import is_in_market_hours
            self.is_in_market_hours = is_in_market_hours
        except ImportError:
            self.skipTest("common.market_calendar が利用不可")

    def _make_jst(self, year, month, day, hour, minute):
        from datetime import datetime, timezone, timedelta
        return datetime(year, month, day, hour, minute, 0,
                        tzinfo=timezone(timedelta(hours=9)))

    def test_monday_open(self):
        """月曜 08:00 JST → 開場。"""
        now = self._make_jst(2026, 4, 20, 8, 0)
        self.assertTrue(self.is_in_market_hours("cme_futures", now))

    def test_saturday_closed(self):
        """土曜 10:00 JST → 週末クローズ。"""
        now = self._make_jst(2026, 4, 25, 10, 0)
        self.assertFalse(self.is_in_market_hours("cme_futures", now))

    def test_daily_break(self):
        """火曜 06:30 JST → デイリー休止。"""
        now = self._make_jst(2026, 4, 21, 6, 30)
        self.assertFalse(self.is_in_market_hours("cme_futures", now))

    def test_friday_evening_open(self):
        """金曜 18:00 JST → 開場（閉場は土曜 06:00）。"""
        now = self._make_jst(2026, 4, 24, 18, 0)
        self.assertTrue(self.is_in_market_hours("cme_futures", now))

    def test_spx_options_market(self):
        """SPX オプション: 水曜 23:00 JST → 開場窓内。"""
        now = self._make_jst(2026, 4, 22, 23, 0)
        self.assertTrue(self.is_in_market_hours("spx_options", now))

    def test_invalid_market_raises(self):
        """未知の market 文字列は ValueError を発生させること。"""
        now = self._make_jst(2026, 4, 20, 10, 0)
        with self.assertRaises(ValueError):
            self.is_in_market_hours("unknown_market", now)


if __name__ == "__main__":
    unittest.main(verbosity=2)
