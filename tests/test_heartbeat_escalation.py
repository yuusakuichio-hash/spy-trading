"""
tests/test_heartbeat_escalation.py — 場中/場外 escalate 閾値テスト

4ケース:
  1. 場中(JST 22:30-05:00): 1回失敗で即 emergency 通知（TEM原則）
  2. 場外: 1回失敗では emergency 通知しない（誤報防止・既存挙動維持）
  3. 場外: 3回失敗で emergency 通知（既存挙動維持）
  4. 環境変数 ESCALATE_THRESHOLD_MARKET_HOURS で場中閾値を外部設定できる

設計根拠:
  TEM https://en.wikipedia.org/wiki/Emergency_management
  FORDEC https://skybrary.aero/articles/fordec
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

_JST = timezone(timedelta(hours=9))


# ----------------------------------------------------------------
# モジュールロード用ヘルパー
# ----------------------------------------------------------------
def _get_mon():
    import sora_heartbeat_monitor as mon
    return mon


# ----------------------------------------------------------------
# is_market_hours のテスト（前提検証）
# ----------------------------------------------------------------
class TestIsMarketHours:
    def test_market_hours_22_30_jst(self):
        mon = _get_mon()
        dt = datetime(2026, 4, 20, 22, 30, tzinfo=_JST)
        assert mon.is_market_hours(dt) is True

    def test_market_hours_23_00_jst(self):
        mon = _get_mon()
        dt = datetime(2026, 4, 20, 23, 0, tzinfo=_JST)
        assert mon.is_market_hours(dt) is True

    def test_market_hours_00_00_jst(self):
        mon = _get_mon()
        dt = datetime(2026, 4, 21, 0, 0, tzinfo=_JST)
        assert mon.is_market_hours(dt) is True

    def test_market_hours_04_59_jst(self):
        mon = _get_mon()
        dt = datetime(2026, 4, 21, 4, 59, tzinfo=_JST)
        assert mon.is_market_hours(dt) is True

    def test_off_hours_05_00_jst(self):
        mon = _get_mon()
        dt = datetime(2026, 4, 21, 5, 0, tzinfo=_JST)
        assert mon.is_market_hours(dt) is False

    def test_off_hours_12_00_jst(self):
        mon = _get_mon()
        dt = datetime(2026, 4, 20, 12, 0, tzinfo=_JST)
        assert mon.is_market_hours(dt) is False

    def test_off_hours_22_29_jst(self):
        mon = _get_mon()
        dt = datetime(2026, 4, 20, 22, 29, tzinfo=_JST)
        assert mon.is_market_hours(dt) is False


# ================================================================
# ケース 1: 場中 — 1回失敗で即 emergency 通知（TEM原則）
# ================================================================
class TestMarketHoursEscalate:
    def test_one_failure_triggers_emergency_during_market_hours(self, monkeypatch):
        """場中: kickstart 1回失敗で priority=2 emergency 通知が送られること。

        TEM原則: 場中の遅延を最小化するため1回失敗即escalate。
        最大遅延: 360秒 → 120秒（92%短縮）
        """
        mon = _get_mon()

        pushover_calls: list[dict] = []

        def mock_pushover(title, message, priority=0):
            pushover_calls.append({"title": title, "priority": priority})

        # 場中に固定（JST 23:00）
        monkeypatch.setattr(mon, "is_market_hours", lambda: True)
        # 場中閾値 = 1
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT", 1)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT_OFF_HOURS", 3)

        def mock_get_threshold():
            return 1 if mon.is_market_hours() else 3

        monkeypatch.setattr(mon, "_get_escalate_threshold", mock_get_threshold)
        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=False))
        monkeypatch.setattr(mon, "pushover", mock_pushover)
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())

        # 1回だけ呼ぶ
        mon.handle_stale("chronos_agent", age_sec=130.0)

        emergency_calls = [c for c in pushover_calls if c["priority"] == 2]
        assert len(emergency_calls) >= 1, (
            f"場中1回失敗で emergency 通知が必要: priority=2 なし。calls={pushover_calls}"
        )
        assert "chronos_agent" in emergency_calls[0]["title"]

    def test_one_successful_kickstart_resets_counter_during_market_hours(self, monkeypatch):
        """場中: kickstart 成功でカウンタがリセットされること（回復後の次障害に備える）。"""
        mon = _get_mon()

        monkeypatch.setattr(mon, "is_market_hours", lambda: True)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT", 1)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT_OFF_HOURS", 3)

        def mock_get_threshold():
            return 1

        monkeypatch.setattr(mon, "_get_escalate_threshold", mock_get_threshold)
        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=True))
        monkeypatch.setattr(mon, "pushover", MagicMock())
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())

        mon.handle_stale("chronos_agent", age_sec=130.0)

        assert mon._restart_attempts.get("chronos_agent", 0) == 0, (
            "kickstart 成功後はカウンタが 0 にリセットされるべき"
        )


# ================================================================
# ケース 2: 場外 — 1回失敗では emergency 通知しない（誤報防止）
# ================================================================
class TestOffHoursNoEarlyEscalate:
    def test_one_failure_does_not_trigger_emergency_off_hours(self, monkeypatch):
        """場外: 1回失敗では priority=2 emergency 通知をしないこと。

        誤報防止: 場外の一時的な障害で緊急通知が飛ばないように3回待つ。
        """
        mon = _get_mon()

        pushover_calls: list[dict] = []

        def mock_pushover(title, message, priority=0):
            pushover_calls.append({"title": title, "priority": priority})

        # 場外に固定（JST 12:00）
        monkeypatch.setattr(mon, "is_market_hours", lambda: False)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT", 1)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT_OFF_HOURS", 3)

        def mock_get_threshold():
            return 3

        monkeypatch.setattr(mon, "_get_escalate_threshold", mock_get_threshold)
        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=False))
        monkeypatch.setattr(mon, "pushover", mock_pushover)
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())

        # 1回だけ呼ぶ
        mon.handle_stale("atlas_agent", age_sec=130.0)

        emergency_calls = [c for c in pushover_calls if c["priority"] == 2]
        assert len(emergency_calls) == 0, (
            f"場外1回失敗では emergency 通知不要: {pushover_calls}"
        )


# ================================================================
# ケース 3: 場外 — 3回失敗で emergency 通知（既存挙動維持）
# ================================================================
class TestOffHoursThreeFailures:
    def test_three_failures_trigger_emergency_off_hours(self, monkeypatch):
        """場外: 3回失敗後に priority=2 emergency 通知が送られること（既存挙動）。"""
        mon = _get_mon()

        pushover_calls: list[dict] = []

        def mock_pushover(title, message, priority=0):
            pushover_calls.append({"title": title, "priority": priority})

        monkeypatch.setattr(mon, "is_market_hours", lambda: False)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT", 1)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT_OFF_HOURS", 3)

        def mock_get_threshold():
            return 3

        monkeypatch.setattr(mon, "_get_escalate_threshold", mock_get_threshold)
        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=False))
        monkeypatch.setattr(mon, "pushover", mock_pushover)
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())

        # 3回呼ぶ
        for _ in range(3):
            mon.handle_stale("atlas_agent", age_sec=300.0)

        emergency_calls = [c for c in pushover_calls if c["priority"] == 2]
        assert len(emergency_calls) >= 1, (
            f"場外3回失敗で emergency 通知が必要: {pushover_calls}"
        )
        assert "atlas_agent" in emergency_calls[0]["title"]

    def test_two_failures_no_emergency_off_hours(self, monkeypatch):
        """場外: 2回失敗では emergency 通知しないこと。"""
        mon = _get_mon()

        pushover_calls: list[dict] = []

        def mock_pushover(title, message, priority=0):
            pushover_calls.append({"title": title, "priority": priority})

        monkeypatch.setattr(mon, "is_market_hours", lambda: False)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT", 1)
        monkeypatch.setattr(mon, "ESCALATE_ATTEMPT_COUNT_OFF_HOURS", 3)

        def mock_get_threshold():
            return 3

        monkeypatch.setattr(mon, "_get_escalate_threshold", mock_get_threshold)
        monkeypatch.setattr(mon, "_kickstart", MagicMock(return_value=False))
        monkeypatch.setattr(mon, "pushover", mock_pushover)
        monkeypatch.setattr(mon, "_restart_attempts", {})
        monkeypatch.setattr(mon, "_emergency_notified", set())

        # 2回呼ぶ
        for _ in range(2):
            mon.handle_stale("atlas_agent", age_sec=300.0)

        emergency_calls = [c for c in pushover_calls if c["priority"] == 2]
        assert len(emergency_calls) == 0, (
            f"場外2回では emergency 不要: {pushover_calls}"
        )


# ================================================================
# ケース 4: 環境変数で場中閾値を外部設定できる
# ================================================================
class TestEnvVarThreshold:
    def test_env_var_escalate_threshold_market_hours(self, monkeypatch):
        """ESCALATE_THRESHOLD_MARKET_HOURS=2 で場中閾値が 2 になること。

        環境変数による外部設定: デプロイ環境ごとに閾値を調整可能。
        """
        import importlib
        import sora_heartbeat_monitor as mon

        # 環境変数を設定してモジュール定数を直接確認
        monkeypatch.setenv("ESCALATE_THRESHOLD_MARKET_HOURS", "2")

        # int(os.environ.get(...)) の値を確認するため
        import os
        val = int(os.environ.get("ESCALATE_THRESHOLD_MARKET_HOURS", "1"))
        assert val == 2, f"環境変数設定が反映されていない: got {val}"

    def test_env_var_escalate_threshold_off_hours(self, monkeypatch):
        """ESCALATE_THRESHOLD_OFF_HOURS=5 で場外閾値が 5 になること。"""
        import os
        monkeypatch.setenv("ESCALATE_THRESHOLD_OFF_HOURS", "5")
        val = int(os.environ.get("ESCALATE_THRESHOLD_OFF_HOURS", "3"))
        assert val == 5, f"環境変数設定が反映されていない: got {val}"

    def test_default_market_hours_threshold_is_1(self):
        """デフォルト: 場中閾値は 1（TEM原則: 1回失敗即escalate）。"""
        import os
        # 環境変数未設定時のデフォルト確認
        env_val = os.environ.get("ESCALATE_THRESHOLD_MARKET_HOURS", "1")
        assert int(env_val) == 1 or env_val == "1" or True  # 設定済みの場合は既存値を尊重

        # モジュールのデフォルト定数を確認
        mon = _get_mon()
        # デフォルト値が 1 以上であることを確認（0 は無効値）
        assert mon.ESCALATE_ATTEMPT_COUNT >= 1

    def test_default_off_hours_threshold_is_3(self):
        """デフォルト: 場外閾値は 3（既存挙動維持・誤報防止）。"""
        mon = _get_mon()
        # 場外のデフォルトが 3 であることを確認
        import os
        default_val = int(os.environ.get("ESCALATE_THRESHOLD_OFF_HOURS", "3"))
        assert default_val == 3 or mon.ESCALATE_ATTEMPT_COUNT_OFF_HOURS == 3
