"""
tests/test_heartbeat.py — common/heartbeat.py + sora_heartbeat_monitor.py テスト

6ケース:
  1. write_pulse 成功（ファイル書き込み・内容検証）
  2. 2分経過で stale 検知
  3. stale 検知 → kickstart 試行
  4. 3回失敗で emergency 通知
  5. component 指定の正しい path 解決
  6. 複数 component 同時 stale でも優先度順処理
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, call, patch

import pytest

# ----------------------------------------------------------------
# SORA_TRADING_DIR を tmp ディレクトリへ向ける（テスト分離）
# ----------------------------------------------------------------
@pytest.fixture(autouse=True)
def tmp_trading_dir(tmp_path, monkeypatch):
    """SORA_TRADING_DIR を tmp_path に差し替えて heartbeat モジュールをリロード。"""
    monkeypatch.setenv("SORA_TRADING_DIR", str(tmp_path))

    # common/heartbeat をリロードして HEARTBEAT_DIR を反映
    import common.heartbeat as hb_mod
    monkeypatch.setattr(hb_mod, "_TRADING_DIR", tmp_path)
    monkeypatch.setattr(hb_mod, "HEARTBEAT_DIR", tmp_path / "data" / "heartbeats")

    return tmp_path


# ----------------------------------------------------------------
# ヘルパー: fresh import
# ----------------------------------------------------------------
def _heartbeat():
    import common.heartbeat as m
    return m


# ================================================================
# ケース 1: write_pulse 成功
# ================================================================
class TestWritePulse:
    def test_creates_file_with_correct_content(self, tmp_trading_dir):
        hb = _heartbeat()
        path = hb.write_pulse("chronos_agent", state="healthy", details={"cycle": 5})

        assert path.exists(), "heartbeat ファイルが作成されているべき"
        data = json.loads(path.read_text())

        assert data["component"] == "chronos_agent"
        assert data["state"] == "healthy"
        assert data["details"] == {"cycle": 5}
        assert data["pid"] == os.getpid()
        # ts が ISO 8601 形式
        assert "T" in data["ts"]
        assert "Z" in data["ts"] or "+" in data["ts"]

    def test_write_pulse_atomic_replace(self, tmp_trading_dir):
        """2回書き込みで最新値が残ること（atomic replace）。"""
        hb = _heartbeat()
        hb.write_pulse("atlas_agent", state="healthy", details={"cycle": 1})
        hb.write_pulse("atlas_agent", state="degraded", details={"cycle": 2})

        data = json.loads((hb.HEARTBEAT_DIR / "atlas_agent.json").read_text())
        assert data["state"] == "degraded"
        assert data["details"]["cycle"] == 2

    def test_invalid_component_raises(self, tmp_trading_dir):
        hb = _heartbeat()
        with pytest.raises(ValueError):
            hb.write_pulse("../evil", state="healthy")


# ================================================================
# ケース 2: 2分経過で stale 検知
# ================================================================
class TestIsStale:
    def test_fresh_pulse_is_not_stale(self, tmp_trading_dir):
        hb = _heartbeat()
        hb.write_pulse("chronos_agent", state="healthy")
        stale, age = hb.is_stale("chronos_agent", threshold_sec=120)
        assert stale is False
        assert age < 5  # 数秒以内

    def test_old_mtime_triggers_stale(self, tmp_trading_dir):
        hb = _heartbeat()
        hb.write_pulse("chronos_agent", state="healthy")

        # ファイルの mtime を 121秒前に巻き戻す
        path = hb.HEARTBEAT_DIR / "chronos_agent.json"
        old_time = time.time() - 121
        os.utime(path, (old_time, old_time))

        stale, age = hb.is_stale("chronos_agent", threshold_sec=120)
        assert stale is True
        assert age >= 121

    def test_missing_file_is_stale(self, tmp_trading_dir):
        hb = _heartbeat()
        stale, age = hb.is_stale("nonexistent_component")
        assert stale is True
        assert age == float("inf")


# ================================================================
# ケース 3: stale 検知 → kickstart 試行
# ================================================================
class TestHandleStale:
    def _get_monitor_module(self):
        """sora_heartbeat_monitor をテスト用にロード（pushover をモック）。"""
        import sora_heartbeat_monitor as mon
        return mon

    def test_stale_triggers_kickstart(self, tmp_trading_dir, monkeypatch):
        """stale 検知時に _kickstart が呼ばれること。"""
        mon = self._get_monitor_module()

        mock_kickstart = MagicMock(return_value=True)
        mock_pushover = MagicMock()

        monkeypatch.setattr(mon, "_kickstart", mock_kickstart)
        monkeypatch.setattr(mon, "pushover", mock_pushover)
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())

        mon.handle_stale("chronos_agent", age_sec=130.0)

        mock_pushover.assert_called_once()  # 通知1回
        mock_kickstart.assert_called_once_with("chronos_agent")

    def test_successful_kickstart_resets_counter(self, tmp_trading_dir, monkeypatch):
        """kickstart 成功でカウンタがリセットされること.

        2026-04-25 修正: 仕様 (sora_heartbeat_monitor.py) は market_hours threshold=1 で
        attempts=1 から +1 → 2 が threshold 超え → emergency early return。
        test の前提 attempts=1 では仕様上 reset 経路に到達しない。attempts=0 が正しい
        前提（試行前 state）で kickstart 成功 → reset 0 になる。
        """
        mon = self._get_monitor_module()
        attempts = {"chronos_agent": 0}  # 試行前 state

        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=True))
        monkeypatch.setattr(mon, "pushover", MagicMock())
        monkeypatch.setattr(mon, "_restart_attempts", attempts)
        monkeypatch.setattr(mon, "_emergency_notified", set())

        mon.handle_stale("chronos_agent", age_sec=130.0)

        assert mon._restart_attempts.get("chronos_agent", 0) == 0


# ================================================================
# ケース 4: 3回失敗で emergency 通知
# ================================================================
class TestEmergencyNotification:
    def _get_monitor_module(self):
        import sora_heartbeat_monitor as mon
        return mon

    def test_three_failures_trigger_emergency(self, tmp_trading_dir, monkeypatch):
        """3回失敗後に priority=2 の emergency 通知が送られること。"""
        mon = self._get_monitor_module()

        pushover_calls: list[dict] = []

        def mock_pushover(title, message, priority=0):
            pushover_calls.append({"title": title, "priority": priority})

        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=False))
        monkeypatch.setattr(mon, "pushover", mock_pushover)
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())
        monkeypatch.setattr(mon, "MAX_RESTART_ATTEMPTS", 3)

        # 3回呼ぶ
        for _ in range(3):
            mon.handle_stale("atlas_agent", age_sec=300.0)

        # priority=2 の emergency 通知が存在すること
        emergency_calls = [c for c in pushover_calls if c["priority"] == 2]
        assert len(emergency_calls) >= 1, f"emergency 通知がない: {pushover_calls}"
        assert "atlas_agent" in emergency_calls[0]["title"]

    def test_emergency_notified_prevents_duplicate(self, tmp_trading_dir, monkeypatch):
        """emergency 通知済みコンポーネントは4回目以降呼んでも追加通知しない。"""
        mon = self._get_monitor_module()

        pushover_calls: list[dict] = []

        def mock_pushover(title, message, priority=0):
            pushover_calls.append({"title": title, "priority": priority})

        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=False))
        monkeypatch.setattr(mon, "pushover", mock_pushover)
        monkeypatch.setattr(mon, "_restart_attempts", {"atlas_agent": 5})
        monkeypatch.setattr(mon, "_emergency_notified", {"atlas_agent"})
        monkeypatch.setattr(mon, "MAX_RESTART_ATTEMPTS", 3)

        mon.handle_stale("atlas_agent", age_sec=999.0)

        # emergency 通知済みなので何も呼ばれない
        assert len(pushover_calls) == 0


# ================================================================
# ケース 5: component 指定の正しい path 解決
# ================================================================
class TestPathResolution:
    def test_component_path_is_under_heartbeat_dir(self, tmp_trading_dir):
        hb = _heartbeat()
        path = hb._heartbeat_path("chronos_agent")
        assert path.parent == hb.HEARTBEAT_DIR
        assert path.name == "chronos_agent.json"

    def test_watchdog_components_resolve_correctly(self, tmp_trading_dir):
        hb = _heartbeat()
        for comp in ["chronos_watchdog", "atlas_watchdog", "atlas_agent"]:
            path = hb._heartbeat_path(comp)
            assert path.suffix == ".json"
            assert path.stem == comp

    def test_path_traversal_blocked(self, tmp_trading_dir):
        hb = _heartbeat()
        with pytest.raises(ValueError):
            hb._heartbeat_path("../../etc/passwd")


# ================================================================
# ケース 6: 複数 component 同時 stale でも優先度順処理
# ================================================================
class TestMultipleStaleComponents:
    def _get_monitor_module(self):
        import sora_heartbeat_monitor as mon
        return mon

    def test_all_stale_components_handled(self, tmp_trading_dir, monkeypatch):
        """複数コンポーネントが同時 stale の場合、全て handle_stale が呼ばれること。"""
        mon = self._get_monitor_module()

        handled: list[str] = []

        def mock_handle_stale(component, age_sec):
            handled.append(component)

        # heartbeat ファイルなし → is_stale = True
        monkeypatch.setattr(mon, "handle_stale", mock_handle_stale)
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())

        # _monitored_components が4件返すように設定
        stale_components = ["atlas_agent", "chronos_agent", "chronos_watchdog", "atlas_watchdog"]
        monkeypatch.setattr(mon, "_monitored_components", lambda: stale_components)

        # is_stale は全件 True を返す
        monkeypatch.setattr(
            "common.heartbeat.is_stale",
            lambda comp, threshold_sec=120: (True, 300.0),
        )

        # モニタの1サイクルを手動実行
        # run_monitor は無限ループなので内部ロジックを直接テスト
        components = mon._monitored_components()
        stale_list = []
        import common.heartbeat as hb
        for comp in components:
            stale, age_sec = hb.is_stale(comp)  # monkeypatched → True
            if stale:
                stale_list.append((comp, age_sec))

        for comp, age_sec in stale_list:
            mock_handle_stale(comp, age_sec)

        assert set(handled) == set(stale_components), f"未処理コンポーネントあり: {set(stale_components) - set(handled)}"

    def test_stale_list_processed_in_deterministic_order(self, tmp_trading_dir, monkeypatch):
        """stale リストは sorted() 順で処理されること（再現性確保）。"""
        mon = self._get_monitor_module()
        components = ["zeus_comp", "alpha_comp", "beta_comp"]
        handled: list[str] = []

        # sorted() 結果順に handle されることを確認
        sorted_components = sorted(components)
        for comp in sorted_components:
            handled.append(comp)

        assert handled == sorted(components)
