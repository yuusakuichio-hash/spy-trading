"""
tests/test_self_monitor_wiring_20260425.py — self_monitor 配線テスト

カバレッジ (12 件):
    WR-01: launchd plist 存在確認
    WR-02: plist Label = com.soralab.self-monitor
    WR-03: plist StartInterval = 30
    WR-04: plist ProgramArguments = python3 -m atlas_v3.supervision.self_monitor
    WR-05: plist WorkingDirectory = /Users/yuusakuichio/trading
    WR-06: plist EnvironmentVariables に SELF_MONITOR_STALE_SEC あり
    WR-07: python -m atlas_v3.supervision.self_monitor として起動可能（main entry point）
    WR-08: main() — sentinel 正常 → sys.exit(0)
    WR-09: main() — sentinel 停滞 → sys.exit(1)
    WR-10: recover_sentinel_if_needed — 停滞検知 → kickstart 発火
    WR-11: MonitorDaemon._run_loop — self_monitor_enabled=True で recover 呼出
    WR-12: MonitorDaemon._run_loop — self_monitor_enabled=False で recover 呼出なし
"""

from __future__ import annotations

import plistlib
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import atlas_v3.supervision.self_monitor as sm
from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon

# ---------------------------------------------------------------------------
# パス定数
# ---------------------------------------------------------------------------

_PLIST_PATH = Path(
    "/Users/yuusakuichio/Library/LaunchAgents/com.soralab.self-monitor.plist"
)

# ---------------------------------------------------------------------------
# WR-01〜WR-06: plist 検査
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def plist_data() -> dict:
    assert _PLIST_PATH.exists(), f"plist が存在しない: {_PLIST_PATH}"
    with _PLIST_PATH.open("rb") as f:
        return plistlib.load(f)


def test_wr01_plist_exists():
    """WR-01: launchd plist ファイルが存在する。"""
    assert _PLIST_PATH.exists()


def test_wr02_plist_label(plist_data):
    """WR-02: Label = com.soralab.self-monitor"""
    assert plist_data["Label"] == "com.soralab.self-monitor"


def test_wr03_plist_start_interval(plist_data):
    """WR-03: StartInterval = 30 (30秒ごとの one-shot 起動)"""
    assert plist_data["StartInterval"] == 30


def test_wr04_plist_program_arguments(plist_data):
    """WR-04: ProgramArguments に -m atlas_v3.supervision.self_monitor が含まれる。"""
    args = plist_data["ProgramArguments"]
    assert "-m" in args
    assert "atlas_v3.supervision.self_monitor" in args


def test_wr05_plist_working_directory(plist_data):
    """WR-05: WorkingDirectory = /Users/yuusakuichio/trading"""
    assert plist_data["WorkingDirectory"] == "/Users/yuusakuichio/trading"


def test_wr06_plist_env_stale_sec(plist_data):
    """WR-06: EnvironmentVariables に SELF_MONITOR_STALE_SEC が設定されている。"""
    env = plist_data.get("EnvironmentVariables", {})
    assert "SELF_MONITOR_STALE_SEC" in env
    # 値が数値文字列であること
    assert int(env["SELF_MONITOR_STALE_SEC"]) > 0


# ---------------------------------------------------------------------------
# WR-07: entry point import 可能
# ---------------------------------------------------------------------------

def test_wr07_main_entry_point_importable():
    """WR-07: atlas_v3.supervision.self_monitor に main callable が存在する。"""
    assert callable(getattr(sm, "main", None)), "main() が self_monitor に未定義"


# ---------------------------------------------------------------------------
# WR-08〜WR-09: main() の exit code
# ---------------------------------------------------------------------------

def test_wr08_main_exit0_when_healthy(tmp_path):
    """WR-08: sentinel が正常 → main() は sys.exit(0) を呼ぶ。"""
    with patch.object(sm, "recover_sentinel_if_needed", return_value=False) as mock_rec:
        with pytest.raises(SystemExit) as exc_info:
            sm.main()
        assert exc_info.value.code == 0
        mock_rec.assert_called_once()


def test_wr09_main_exit1_when_stale():
    """WR-09: sentinel が停滞 → main() は sys.exit(1) を呼ぶ。"""
    with patch.object(sm, "recover_sentinel_if_needed", return_value=True):
        with pytest.raises(SystemExit) as exc_info:
            sm.main()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# WR-10: recover_sentinel_if_needed — 停滞時 kickstart 発火
# ---------------------------------------------------------------------------

def test_wr10_recover_fires_kickstart_when_stale(tmp_path):
    """WR-10: sentinel 停滞検知時に _kickstart_sentinel() が呼ばれる。"""
    # check_sentinel_liveness が停滞を返すように patch
    with patch.object(sm, "check_sentinel_liveness", return_value=(False, "mtime stale")):
        with patch.object(sm, "_kickstart_sentinel", return_value=True) as mock_kick:
            with patch.object(sm, "_send_p1") as mock_p1:
                result = sm.recover_sentinel_if_needed(stale_sec=90)
    assert result is True
    mock_kick.assert_called_once()
    mock_p1.assert_called_once()


# ---------------------------------------------------------------------------
# WR-11〜WR-12: MonitorDaemon._run_loop 配線
# ---------------------------------------------------------------------------

def _make_daemon(self_monitor_enabled: bool) -> MonitorDaemon:
    """テスト用 MonitorDaemon を生成するヘルパー。"""
    cfg = MonitorConfig(
        check_interval_secs=0.05,
        daily_loss_usd=-400.0,
        pushover_enabled=False,
        kill_switch_on_emergency=False,
        kill_switch_on_drawdown_breach=False,
        log_rotate_enabled=False,
        self_monitor_enabled=self_monitor_enabled,
    )
    daemon = MonitorDaemon(config=cfg)
    return daemon


def test_wr11_run_loop_calls_recover_when_enabled():
    """WR-11: self_monitor_enabled=True の場合 _run_loop が recover_sentinel_if_needed を呼ぶ。"""
    daemon = _make_daemon(self_monitor_enabled=True)
    called_event = threading.Event()

    def fake_recover(stale_sec=sm.SENTINEL_DEFAULT_STALE_SEC):
        called_event.set()
        return False

    with patch(
        "atlas_v3.supervision.self_monitor.recover_sentinel_if_needed",
        side_effect=fake_recover,
    ):
        daemon.start()
        called = called_event.wait(timeout=2.0)
        daemon.stop()

    assert called, "self_monitor_enabled=True なのに recover_sentinel_if_needed が呼ばれなかった"


def test_wr12_run_loop_no_recover_when_disabled():
    """WR-12: self_monitor_enabled=False の場合 recover_sentinel_if_needed は呼ばれない。"""
    daemon = _make_daemon(self_monitor_enabled=False)
    recover_called = []

    def fake_recover(stale_sec=sm.SENTINEL_DEFAULT_STALE_SEC):
        recover_called.append(True)
        return False

    with patch(
        "atlas_v3.supervision.self_monitor.recover_sentinel_if_needed",
        side_effect=fake_recover,
    ):
        daemon.start()
        # 3 tick 分待つ
        time.sleep(0.3)
        daemon.stop()

    assert len(recover_called) == 0, (
        f"self_monitor_enabled=False なのに recover が {len(recover_called)} 回呼ばれた"
    )
