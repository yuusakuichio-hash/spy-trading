"""
tests/test_sentinel_watchdog_20260425.py — sentinel_watchdog.py / self_monitor.py テスト

カバレッジ (15 件以上):
    SW-01: _is_dms_process_alive — pgrep 成功 (自 pid 以外あり) → True
    SW-02: _is_dms_process_alive — pgrep 成功 (自 pid のみ) → False
    SW-03: _is_dms_process_alive — pgrep returncode=1 → False
    SW-04: _is_dms_process_alive — TimeoutExpired → False
    SW-05: _is_dms_heartbeat_fresh — mtime 新しい → True
    SW-06: _is_dms_heartbeat_fresh — mtime 古い → False
    SW-07: _is_dms_heartbeat_fresh — ファイル不在 → False
    SW-08: _count_dms_beats_in_window — 5 分以内 3 件 → 3
    SW-09: _count_dms_beats_in_window — ファイル不在 → 0
    SW-10: check_dms_health — 全 OK → (True, "OK...")
    SW-11: check_dms_health — proc DEAD → (False, "proc=DEAD...")
    SW-12: check_dms_health — beats 不足 → (False, "beats_5m=...")
    SW-13: restart_dms — launchctl 成功 → True
    SW-14: restart_dms — launchctl 失敗 → False
    SW-15: run_check_cycle — 正常 → consecutive_failures=0 維持
    SW-16: run_check_cycle — 1 回異常 → consecutive_failures=1, restart呼出
    SW-17: run_check_cycle — 3 連続失敗 → P1 alert + KILL_SWITCH 発動
    SW-18: run_check_cycle — 回復後 consecutive_failures リセット
    SW-19: write_sentinel_heartbeat — JSONL に追記される
    SW-20: write_sentinel_heartbeat — SENTINEL_HEARTBEAT_INTERVAL 以内は再書き込みしない
    SM-01: check_sentinel_liveness — heartbeat 不在 → False
    SM-02: check_sentinel_liveness — mtime 新鮮 → True
    SM-03: check_sentinel_liveness — mtime 古い → False
    SM-04: recover_sentinel_if_needed — 正常 → False (何もしない)
    SM-05: recover_sentinel_if_needed — 異常 → P1 送信 + kickstart 発火 → True
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# sentinel_watchdog を import (プロジェクトルートへのパスは conftest/sys.path で解決済み)
import scripts.sentinel_watchdog as sw
import atlas_v3.supervision.self_monitor as sm


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_consecutive_failures():
    """各テスト前後に _consecutive_failures をリセットする。"""
    sw._consecutive_failures = 0
    yield
    sw._consecutive_failures = 0


@pytest.fixture()
def tmp_ping_file(tmp_path):
    """PING_FILE を tmp_path 内に向け直す。"""
    orig_dir = sw.PING_DIR
    orig_file = sw.PING_FILE
    new_dir = tmp_path / "heartbeat"
    new_dir.mkdir()
    new_file = new_dir / "dead_man_ping.jsonl"
    sw.PING_DIR = new_dir
    sw.PING_FILE = new_file
    yield new_file
    sw.PING_DIR = orig_dir
    sw.PING_FILE = orig_file


@pytest.fixture()
def tmp_sentinel_hb(tmp_path):
    """SENTINEL_HEARTBEAT_FILE を tmp_path 内に向け直す。"""
    orig = sw.SENTINEL_HEARTBEAT_FILE
    new_file = tmp_path / "sentinel_heartbeat.jsonl"
    sw.SENTINEL_HEARTBEAT_FILE = new_file
    yield new_file
    sw.SENTINEL_HEARTBEAT_FILE = orig


def _write_ping_record(ping_file: Path, component: str = "dead_man_switch",
                       offset_sec: float = 0.0) -> None:
    """テスト用 beacon レコードを JSONL に書き込む。"""
    ts = datetime.now(timezone.utc)
    if offset_sec != 0:
        import datetime as _dt
        ts = ts - _dt.timedelta(seconds=abs(offset_sec)) if offset_sec > 0 else ts
    record = {"ts": ts.isoformat(), "component": component}
    ping_file.parent.mkdir(parents=True, exist_ok=True)
    with ping_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# SW-01〜04: _is_dms_process_alive
# ─────────────────────────────────────────────────────────────────────────────

class TestIsDmsProcessAlive:
    def test_sw01_other_pid_found_returns_true(self):
        """SW-01: pgrep が自 pid 以外の pid を返す → True。"""
        own_pid = str(os.getpid())
        other_pid = str(int(own_pid) + 1000)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = f"{other_pid}\n".encode()
        with patch("scripts.sentinel_watchdog.subprocess.run", return_value=mock_result):
            assert sw._is_dms_process_alive() is True

    def test_sw02_only_own_pid_returns_false(self):
        """SW-02: pgrep が自 pid のみ返す → False。"""
        own_pid = str(os.getpid())
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = f"{own_pid}\n".encode()
        with patch("scripts.sentinel_watchdog.subprocess.run", return_value=mock_result):
            assert sw._is_dms_process_alive() is False

    def test_sw03_pgrep_nonzero_returns_false(self):
        """SW-03: pgrep returncode=1 → False。"""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""
        with patch("scripts.sentinel_watchdog.subprocess.run", return_value=mock_result):
            assert sw._is_dms_process_alive() is False

    def test_sw04_timeout_returns_false(self):
        """SW-04: TimeoutExpired → False (クラッシュしない)。"""
        with patch(
            "scripts.sentinel_watchdog.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5),
        ):
            assert sw._is_dms_process_alive() is False


# ─────────────────────────────────────────────────────────────────────────────
# SW-05〜07: _is_dms_heartbeat_fresh
# ─────────────────────────────────────────────────────────────────────────────

class TestIsDmsHeartbeatFresh:
    def test_sw05_fresh_mtime_returns_true(self, tmp_ping_file):
        """SW-05: mtime が HEARTBEAT_STALE_SEC より新しい → True。"""
        tmp_ping_file.write_text("x\n", encoding="utf-8")
        # mtime は現在時刻なので fresh
        assert sw._is_dms_heartbeat_fresh() is True

    def test_sw06_stale_mtime_returns_false(self, tmp_ping_file, monkeypatch):
        """SW-06: mtime が HEARTBEAT_STALE_SEC を超えている → False。"""
        tmp_ping_file.write_text("x\n", encoding="utf-8")
        # time.time() を未来にずらすことで古く見せる
        old_time = time.time() + sw.HEARTBEAT_STALE_SEC + 10
        monkeypatch.setattr("scripts.sentinel_watchdog.time.time", lambda: old_time)
        assert sw._is_dms_heartbeat_fresh() is False

    def test_sw07_file_absent_returns_false(self, tmp_ping_file):
        """SW-07: PING_FILE が存在しない → False。"""
        # tmp_ping_file は fixture で new_file を指しているが書かれていない
        assert not tmp_ping_file.exists()
        assert sw._is_dms_heartbeat_fresh() is False


# ─────────────────────────────────────────────────────────────────────────────
# SW-08〜09: _count_dms_beats_in_window
# ─────────────────────────────────────────────────────────────────────────────

class TestCountDmsBeatsInWindow:
    def test_sw08_three_beats_in_window(self, tmp_ping_file):
        """SW-08: 直近 5 分以内に 3 件の dead_man_switch beacon → 3 を返す。"""
        for _ in range(3):
            _write_ping_record(tmp_ping_file, component="dead_man_switch")
        result = sw._count_dms_beats_in_window(window_sec=300)
        assert result == 3

    def test_sw09_file_absent_returns_zero(self, tmp_ping_file):
        """SW-09: PING_FILE が存在しない → 0。"""
        assert not tmp_ping_file.exists()
        assert sw._count_dms_beats_in_window(window_sec=300) == 0


# ─────────────────────────────────────────────────────────────────────────────
# SW-10〜12: check_dms_health
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckDmsHealth:
    def test_sw10_all_ok_returns_true(self, tmp_ping_file):
        """SW-10: proc alive + mtime fresh + beats OK → (True, "OK...")。"""
        for _ in range(sw.MIN_BEATS_IN_5MIN):
            _write_ping_record(tmp_ping_file, component="dead_man_switch")
        with patch("scripts.sentinel_watchdog._is_dms_process_alive", return_value=True), \
             patch("scripts.sentinel_watchdog._is_dms_heartbeat_fresh", return_value=True):
            healthy, reason = sw.check_dms_health()
        assert healthy is True
        assert "OK" in reason

    def test_sw11_proc_dead_but_hb_ok_returns_true_oneshot(self, tmp_ping_file):
        """SW-11: proc dead (ワンショット終了後) + heartbeat fresh + beats OK → (True, ...)。

        dead_man_switch はワンショット設計なのでプロセスが終了していても
        heartbeat が新鮮なら正常と判断する。
        """
        for _ in range(sw.MIN_BEATS_IN_5MIN):
            _write_ping_record(tmp_ping_file, component="dead_man_switch")
        with patch("scripts.sentinel_watchdog._is_dms_process_alive", return_value=False), \
             patch("scripts.sentinel_watchdog._is_dms_heartbeat_fresh", return_value=True):
            healthy, reason = sw.check_dms_health()
        assert healthy is True
        assert "oneshot" in reason or "OK" in reason

    def test_sw12_beats_insufficient_returns_false(self, tmp_ping_file):
        """SW-12: beats 不足 → (False, reason に "beats_5m" 含む)。"""
        # MIN_BEATS_IN_5MIN 未満しか書かない
        _write_ping_record(tmp_ping_file, component="dead_man_switch")  # 1件のみ
        with patch("scripts.sentinel_watchdog._is_dms_process_alive", return_value=True), \
             patch("scripts.sentinel_watchdog._is_dms_heartbeat_fresh", return_value=True), \
             patch(
                 "scripts.sentinel_watchdog._count_dms_beats_in_window",
                 return_value=sw.MIN_BEATS_IN_5MIN - 1,
             ):
            healthy, reason = sw.check_dms_health()
        assert healthy is False
        assert "beats_5m" in reason


# ─────────────────────────────────────────────────────────────────────────────
# SW-13〜14: restart_dms
# ─────────────────────────────────────────────────────────────────────────────

class TestRestartDms:
    def test_sw13_launchctl_success_returns_true(self):
        """SW-13: launchctl kickstart 成功 → True。"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        with patch("scripts.sentinel_watchdog.subprocess.run", return_value=mock_result):
            assert sw.restart_dms() is True

    def test_sw14_launchctl_failure_returns_false(self):
        """SW-14: launchctl kickstart 失敗 → False。"""
        mock_result = MagicMock()
        mock_result.returncode = 113  # No such process
        mock_result.stderr = "No such process"
        with patch("scripts.sentinel_watchdog.subprocess.run", return_value=mock_result):
            assert sw.restart_dms() is False


# ─────────────────────────────────────────────────────────────────────────────
# SW-15〜18: run_check_cycle
# ─────────────────────────────────────────────────────────────────────────────

class TestRunCheckCycle:
    def test_sw15_healthy_keeps_failures_zero(self):
        """SW-15: dms 正常 → consecutive_failures は 0 のまま。"""
        with patch("scripts.sentinel_watchdog.check_dms_health", return_value=(True, "OK")), \
             patch("scripts.sentinel_watchdog.write_sentinel_heartbeat"):
            sw.run_check_cycle()
        assert sw._consecutive_failures == 0

    def test_sw16_one_failure_increments_and_restarts(self):
        """SW-16: 1 回異常 → consecutive_failures=1 + restart_dms 呼出。"""
        restart_mock = MagicMock(return_value=True)
        with patch("scripts.sentinel_watchdog.check_dms_health",
                   return_value=(False, "proc=DEAD")), \
             patch("scripts.sentinel_watchdog.restart_dms", restart_mock), \
             patch("scripts.sentinel_watchdog._send_p1_alert"), \
             patch("scripts.sentinel_watchdog._activate_kill_switch"), \
             patch("scripts.sentinel_watchdog.write_sentinel_heartbeat"):
            sw.run_check_cycle()
        assert sw._consecutive_failures == 1
        restart_mock.assert_called_once()

    def test_sw17_three_failures_triggers_p1_and_kill_switch(self):
        """SW-17: 3 連続失敗 → Pushover P1 + KILL_SWITCH 発動。"""
        p1_mock = MagicMock()
        ks_mock = MagicMock()
        sw._consecutive_failures = sw.MAX_CONSECUTIVE_FAILURES - 1  # 2回失敗済み

        with patch("scripts.sentinel_watchdog.check_dms_health",
                   return_value=(False, "heartbeat=STALE")), \
             patch("scripts.sentinel_watchdog.restart_dms", return_value=False), \
             patch("scripts.sentinel_watchdog._send_p1_alert", p1_mock), \
             patch("scripts.sentinel_watchdog._activate_kill_switch", ks_mock), \
             patch("scripts.sentinel_watchdog.write_sentinel_heartbeat"):
            sw.run_check_cycle()

        assert sw._consecutive_failures == sw.MAX_CONSECUTIVE_FAILURES
        p1_mock.assert_called_once()
        ks_mock.assert_called_once()
        # KILL_SWITCH 引数に連続失敗数が含まれる
        ks_args = ks_mock.call_args[0][0]
        assert "consecutive_failures" in ks_args

    def test_sw18_recovery_resets_failures(self):
        """SW-18: 異常後に正常 → consecutive_failures がリセットされる。"""
        sw._consecutive_failures = 2  # 既に 2 回失敗

        with patch("scripts.sentinel_watchdog.check_dms_health", return_value=(True, "OK")), \
             patch("scripts.sentinel_watchdog.write_sentinel_heartbeat"):
            sw.run_check_cycle()

        assert sw._consecutive_failures == 0


# ─────────────────────────────────────────────────────────────────────────────
# SW-19〜20: write_sentinel_heartbeat
# ─────────────────────────────────────────────────────────────────────────────

class TestWriteSentinelHeartbeat:
    def test_sw19_writes_jsonl_record(self, tmp_path, monkeypatch):
        """SW-19: write_sentinel_heartbeat() が JSONL レコードを追記する。"""
        hb_file = tmp_path / "sentinel_heartbeat.jsonl"
        monkeypatch.setattr(sw, "SENTINEL_HEARTBEAT_FILE", hb_file)
        monkeypatch.setattr(sw, "PING_DIR", tmp_path)
        # 強制書き込みのため _last_sentinel_hb_write をリセット
        sw._last_sentinel_hb_write = 0.0

        sw.write_sentinel_heartbeat()

        assert hb_file.exists()
        lines = [l for l in hb_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) >= 1
        rec = json.loads(lines[-1])
        assert rec["component"] == "sentinel_watchdog"
        assert rec["status"] == "alive"

    def test_sw20_no_duplicate_within_interval(self, tmp_path, monkeypatch):
        """SW-20: interval 以内の再呼出しは追記しない。"""
        hb_file = tmp_path / "sentinel_heartbeat.jsonl"
        monkeypatch.setattr(sw, "SENTINEL_HEARTBEAT_FILE", hb_file)
        monkeypatch.setattr(sw, "PING_DIR", tmp_path)
        sw._last_sentinel_hb_write = 0.0

        sw.write_sentinel_heartbeat()  # 1回目: 書く
        sw.write_sentinel_heartbeat()  # 2回目: interval 以内 → 書かない

        lines = [l for l in hb_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 1


# ─────────────────────────────────────────────────────────────────────────────
# SM-01〜05: atlas_v3.supervision.self_monitor
# ─────────────────────────────────────────────────────────────────────────────

class TestSelfMonitor:
    def test_sm01_heartbeat_absent_returns_false(self, tmp_path, monkeypatch):
        """SM-01: sentinel_heartbeat.jsonl が存在しない → (False, ...)。"""
        hb_file = tmp_path / "sentinel_heartbeat.jsonl"
        monkeypatch.setattr(sm, "SENTINEL_HEARTBEAT_FILE", hb_file)
        healthy, reason = sm.check_sentinel_liveness(stale_sec=90)
        assert healthy is False
        assert "存在しない" in reason

    def test_sm02_fresh_mtime_returns_true(self, tmp_path, monkeypatch):
        """SM-02: mtime 新鮮 → (True, ...)。"""
        hb_file = tmp_path / "sentinel_heartbeat.jsonl"
        ts = datetime.now(timezone.utc).isoformat()
        hb_file.write_text(
            json.dumps({"ts": ts, "component": "sentinel_watchdog", "status": "alive"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sm, "SENTINEL_HEARTBEAT_FILE", hb_file)
        healthy, reason = sm.check_sentinel_liveness(stale_sec=90)
        assert healthy is True

    def test_sm03_stale_mtime_returns_false(self, tmp_path, monkeypatch):
        """SM-03: mtime が stale_sec を超えている → (False, ...)。"""
        hb_file = tmp_path / "sentinel_heartbeat.jsonl"
        hb_file.write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(sm, "SENTINEL_HEARTBEAT_FILE", hb_file)
        # time.time() を未来にずらして古く見せる
        future_time = time.time() + 200  # stale_sec=90 を超える
        with patch("atlas_v3.supervision.self_monitor.time.time", return_value=future_time):
            healthy, reason = sm.check_sentinel_liveness(stale_sec=90)
        assert healthy is False
        assert "更新なし" in reason

    def test_sm04_healthy_sentinel_does_nothing(self, tmp_path, monkeypatch):
        """SM-04: sentinel 正常 → recover_sentinel_if_needed が False を返し何もしない。"""
        hb_file = tmp_path / "sentinel_heartbeat.jsonl"
        ts = datetime.now(timezone.utc).isoformat()
        hb_file.write_text(
            json.dumps({"ts": ts, "component": "sentinel_watchdog", "status": "alive"}) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sm, "SENTINEL_HEARTBEAT_FILE", hb_file)
        pushover_mock = MagicMock()
        with patch("atlas_v3.supervision.self_monitor._send_p1", pushover_mock):
            result = sm.recover_sentinel_if_needed(stale_sec=90)
        assert result is False
        pushover_mock.assert_not_called()

    def test_sm05_stale_sentinel_triggers_recovery(self, tmp_path, monkeypatch):
        """SM-05: sentinel 停滞 → P1 送信 + kickstart 発火 → True 返す。"""
        hb_file = tmp_path / "sentinel_heartbeat.jsonl"
        hb_file.write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(sm, "SENTINEL_HEARTBEAT_FILE", hb_file)

        p1_mock = MagicMock()
        kickstart_mock = MagicMock(return_value=True)
        future_time = time.time() + 200
        with patch("atlas_v3.supervision.self_monitor.time.time", return_value=future_time), \
             patch("atlas_v3.supervision.self_monitor._send_p1", p1_mock), \
             patch("atlas_v3.supervision.self_monitor._kickstart_sentinel", kickstart_mock):
            result = sm.recover_sentinel_if_needed(stale_sec=90)

        assert result is True
        p1_mock.assert_called_once()
        kickstart_mock.assert_called_once()
