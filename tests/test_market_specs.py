"""tests/test_market_specs.py — market_specs.yaml / market_specs.py / market_calendar.py 統合テスト

テスト対象:
  1. market_specs.yaml ロード正常性
  2. get_session_jst() の値検証 (SPX / CME)
  3. get_daily_break_jst() の値検証
  4. is_in_session() の境界判定 (SPX / CME 先物)
  5. market_calendar.is_in_market_hours() との一致確認
  6. hook スクリプト存在・実行可能性テスト

既存テスト (test_watchdog_recovery.py / test_watchdog_recovery_integration.py) との
干渉なし: import のみで副作用なし。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

# ── パス設定 ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

JST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────────────────────
# 1. market_specs.yaml ロード正常性
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketSpecsYamlLoad:
    """market_specs.yaml が正しく読み込まれるか。"""

    def test_yaml_file_exists(self):
        """market_specs.yaml が存在する。"""
        path = PROJECT_ROOT / "common" / "market_specs.yaml"
        assert path.exists(), f"market_specs.yaml が見つからない: {path}"

    def test_yaml_loads_without_error(self):
        """yaml.safe_load で例外なく読み込める。"""
        import yaml
        path = PROJECT_ROOT / "common" / "market_specs.yaml"
        with path.open(encoding="utf-8") as f:
            specs = yaml.safe_load(f)
        assert specs is not None
        assert "markets" in specs

    def test_yaml_has_spx_options(self):
        """spx_options セクションが存在する。"""
        from common.market_specs import get_market_spec
        spec = get_market_spec("spx_options")
        assert "description" in spec
        assert "atlas" in spec["used_by"]

    def test_yaml_has_cme_futures_equity(self):
        """cme_futures_equity セクションが存在する。"""
        from common.market_specs import get_market_spec
        spec = get_market_spec("cme_futures_equity")
        assert "description" in spec
        assert "chronos" in spec["used_by"]

    def test_yaml_has_confusion_prevention(self):
        """confusion_prevention チェックリストが存在する。"""
        import yaml
        path = PROJECT_ROOT / "common" / "market_specs.yaml"
        with path.open(encoding="utf-8") as f:
            specs = yaml.safe_load(f)
        items = specs.get("confusion_prevention", [])
        assert len(items) >= 3, "混同防止チェックリストが不足している"

    def test_yaml_unknown_market_raises(self):
        """存在しない市場名は ValueError。"""
        from common.market_specs import get_market_spec
        with pytest.raises(ValueError, match="未知の market"):
            get_market_spec("unknown_market")


# ─────────────────────────────────────────────────────────────────────────────
# 2. get_session_jst() 値検証
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSessionJst:
    """get_session_jst() が期待値を返すか。"""

    def test_spx_options_edt_session(self):
        """SPX オプション夏時間セッション。"""
        from common.market_specs import get_session_jst
        sessions = get_session_jst("spx_options", dst=True)
        assert len(sessions) >= 1
        # open は 22:xx (夜) 付近
        open_str, close_str = sessions[0]
        assert "22" in open_str, f"SPX open が期待と異なる: {open_str}"

    def test_cme_futures_equity_edt_session(self):
        """CME 先物夏時間セッション。"""
        from common.market_specs import get_session_jst
        sessions = get_session_jst("cme_futures_equity", dst=True)
        assert len(sessions) >= 1
        open_str, close_str = sessions[0]
        # open_day: monday 07:00
        assert "monday" in open_str.lower() or "07" in open_str, (
            f"CME open_day が期待と異なる: {open_str}"
        )
        # close_day: saturday 06:00
        assert "saturday" in close_str.lower() or "06" in close_str, (
            f"CME close_day が期待と異なる: {close_str}"
        )

    def test_cme_futures_equity_est_session(self):
        """CME 先物冬時間セッション (1時間後ろ)。"""
        from common.market_specs import get_session_jst
        sessions = get_session_jst("cme_futures_equity", dst=False)
        assert len(sessions) >= 1
        open_str, _ = sessions[0]
        assert "monday" in open_str.lower() or "08" in open_str, (
            f"CME 冬時間 open_day が期待と異なる: {open_str}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. get_daily_break_jst() 値検証
# ─────────────────────────────────────────────────────────────────────────────

class TestGetDailyBreakJst:
    """get_daily_break_jst() の値を確認。"""

    def test_cme_break_edt(self):
        """CME デイリー休止 夏時間: 06:00-07:00 JST。"""
        from common.market_specs import get_daily_break_jst
        result = get_daily_break_jst("cme_futures_equity", dst=True)
        assert result is not None
        start_hm, end_hm = result
        assert start_hm == (6, 0), f"CME デイリー休止開始: {start_hm}"
        assert end_hm == (7, 0), f"CME デイリー休止終了: {end_hm}"

    def test_cme_break_est(self):
        """CME デイリー休止 冬時間: 07:00-08:00 JST。"""
        from common.market_specs import get_daily_break_jst
        result = get_daily_break_jst("cme_futures_equity", dst=False)
        assert result is not None
        start_hm, end_hm = result
        assert start_hm == (7, 0), f"CME 冬時間デイリー休止開始: {start_hm}"
        assert end_hm == (8, 0), f"CME 冬時間デイリー休止終了: {end_hm}"

    def test_spx_no_daily_break(self):
        """SPX オプションにはデイリー休止なし。"""
        from common.market_specs import get_daily_break_jst
        result = get_daily_break_jst("spx_options", dst=True)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. is_in_session() 境界判定
# ─────────────────────────────────────────────────────────────────────────────

def _jst(year, month, day, hour, minute=0):
    """JST の datetime を生成するヘルパー。"""
    return datetime(year, month, day, hour, minute, tzinfo=JST)


class TestIsInSessionSPX:
    """SPX オプション (Atlas) のセッション境界判定。"""

    # ── 平日・市場時間内 ────────────────────────────────────────────────────

    def test_spx_wednesday_2330_in_session(self):
        """水曜 23:30 JST は Atlas セッション内。"""
        from common.market_specs import is_in_session
        # 水曜 23:30 JST (夏時間)
        now = _jst(2026, 4, 22, 23, 30)  # 水曜
        assert is_in_session("spx_options", now) is True

    def test_spx_thursday_0200_in_session(self):
        """木曜 02:00 JST は Atlas セッション内 (日跨ぎ窓)。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 23, 2, 0)  # 木曜
        assert is_in_session("spx_options", now) is True

    # ── 平日・市場時間外 ────────────────────────────────────────────────────

    def test_spx_wednesday_1200_out_of_session(self):
        """水曜 12:00 JST は Atlas セッション外。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 22, 12, 0)
        assert is_in_session("spx_options", now) is False

    # ── 週末 ──────────────────────────────────────────────────────────────

    def test_spx_saturday_closed(self):
        """土曜は Atlas クローズ。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 25, 23, 30)  # 土曜 23:30
        assert is_in_session("spx_options", now) is False

    def test_spx_sunday_closed(self):
        """日曜は Atlas クローズ。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 26, 2, 0)  # 日曜 02:00
        assert is_in_session("spx_options", now) is False


class TestIsInSessionCME:
    """CME 先物 (Chronos) のセッション境界判定。"""

    # ── 開場中 ──────────────────────────────────────────────────────────────

    def test_cme_monday_1000_open(self):
        """月曜 10:00 JST は CME 開場中。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 20, 10, 0)  # 月曜
        assert is_in_session("cme_futures_equity", now) is True

    def test_cme_wednesday_0300_open(self):
        """水曜 03:00 JST (夜明け前) は CME 開場中。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 22, 3, 0)  # 水曜
        assert is_in_session("cme_futures_equity", now) is True

    def test_cme_friday_2200_open(self):
        """金曜 22:00 JST は CME 開場中 (土曜 06:00 前)。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 24, 22, 0)  # 金曜
        assert is_in_session("cme_futures_equity", now) is True

    # ── デイリー休止 ────────────────────────────────────────────────────────

    def test_cme_daily_break_0630_closed(self):
        """毎日 06:30 JST は CME デイリー休止中。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 22, 6, 30)  # 水曜 06:30
        assert is_in_session("cme_futures_equity", now) is False

    def test_cme_daily_break_0600_closed(self):
        """06:00 JST ちょうどは休止開始 → クローズ。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 22, 6, 0)  # 水曜 06:00
        assert is_in_session("cme_futures_equity", now) is False

    def test_cme_daily_break_0700_open(self):
        """07:00 JST ちょうどは休止終了 → 開場。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 22, 7, 0)  # 水曜 07:00
        assert is_in_session("cme_futures_equity", now) is True

    # ── 週末クローズ ────────────────────────────────────────────────────────

    def test_cme_saturday_0700_closed(self):
        """土曜 07:00 JST は CME 週末クローズ (土曜 06:00 以降)。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 25, 7, 0)  # 土曜
        assert is_in_session("cme_futures_equity", now) is False

    def test_cme_sunday_1200_closed(self):
        """日曜全日は CME クローズ。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 26, 12, 0)  # 日曜
        assert is_in_session("cme_futures_equity", now) is False

    def test_cme_monday_0630_closed(self):
        """月曜 06:30 JST (07:00 前) は CME クローズ。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 20, 6, 30)  # 月曜
        assert is_in_session("cme_futures_equity", now) is False

    def test_cme_monday_0700_open(self):
        """月曜 07:00 JST ちょうどは CME 開場。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 20, 7, 0)  # 月曜
        assert is_in_session("cme_futures_equity", now) is True

    # ── SPX と CME の差異確認 (混同防止の核心) ────────────────────────────

    def test_spx_vs_cme_difference_saturday_closed(self):
        """土曜 10:00: SPX はクローズ・CME もクローズ (一致)。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 25, 10, 0)  # 土曜
        assert is_in_session("spx_options", now) is False
        assert is_in_session("cme_futures_equity", now) is False

    def test_spx_vs_cme_difference_monday_0800(self):
        """月曜 08:00 JST: SPX はクローズ・CME は開場 (最重要差異)。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 20, 8, 0)  # 月曜 08:00
        # SPX は夜 22:20 から始まる → 月曜昼は閉場
        assert is_in_session("spx_options", now) is False
        # CME は月曜 07:00 から開場 → 08:00 は開場中
        assert is_in_session("cme_futures_equity", now) is True

    def test_spx_vs_cme_difference_daily_break(self):
        """06:30 JST: SPX はクローズ・CME もデイリー休止でクローズ。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 22, 6, 30)  # 水曜 06:30
        # SPX は夜間の監視窓なので 06:30 は範囲外 (05:10 以降はクローズ)
        assert is_in_session("spx_options", now) is False
        # CME は 06:00-07:00 のデイリー休止中
        assert is_in_session("cme_futures_equity", now) is False

    def test_spx_vs_cme_difference_wednesday_2300(self):
        """水曜 23:00 JST: SPX は開場・CME も開場 (共通開場時間)。"""
        from common.market_specs import is_in_session
        now = _jst(2026, 4, 22, 23, 0)  # 水曜 23:00
        assert is_in_session("spx_options", now) is True
        assert is_in_session("cme_futures_equity", now) is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. market_calendar.is_in_market_hours() との一致確認
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketCalendarConsistency:
    """market_specs.is_in_session と market_calendar.is_in_market_hours の結果一致確認。"""

    CASES = [
        # (year, month, day, hour, minute, expected_spx, expected_cme)
        (2026, 4, 20,  8,  0, False, True),   # 月曜 08:00: SPX閉・CME開
        (2026, 4, 20,  7,  0, False, True),   # 月曜 07:00: SPX閉・CME開
        (2026, 4, 20, 23,  0, True,  True),   # 月曜 23:00: 両方開
        (2026, 4, 22,  6, 30, False, False),  # 水曜 06:30: 両方閉
        (2026, 4, 25, 10,  0, False, False),  # 土曜 10:00: 両方閉
        (2026, 4, 26, 12,  0, False, False),  # 日曜 12:00: 両方閉
    ]

    @pytest.mark.parametrize("y,mo,d,h,mi,exp_spx,exp_cme", CASES)
    def test_consistency(self, y, mo, d, h, mi, exp_spx, exp_cme):
        """is_in_session と is_in_market_hours が同じ結果を返す。"""
        from common.market_specs import is_in_session
        from common.market_calendar import is_in_market_hours

        now = datetime(y, mo, d, h, mi, tzinfo=JST)

        # SPX
        specs_spx = is_in_session("spx_options", now)
        cal_spx   = is_in_market_hours("spx_options", now)
        assert specs_spx == cal_spx, (
            f"SPX 不一致 {y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}: "
            f"specs={specs_spx} cal={cal_spx}"
        )
        assert specs_spx == exp_spx, f"SPX 期待値不一致: {specs_spx} != {exp_spx}"

        # CME (market_specs は cme_futures_equity, calendar は cme_futures)
        specs_cme = is_in_session("cme_futures_equity", now)
        cal_cme   = is_in_market_hours("cme_futures", now)
        assert specs_cme == cal_cme, (
            f"CME 不一致 {y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}: "
            f"specs={specs_cme} cal={cal_cme}"
        )
        assert specs_cme == exp_cme, f"CME 期待値不一致: {specs_cme} != {exp_cme}"


# ─────────────────────────────────────────────────────────────────────────────
# 6. hook スクリプト存在・実行可能性テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestHookScripts:
    """hook スクリプトが正しく配置・設定されているか。"""

    HOOKS_DIR = PROJECT_ROOT / ".claude" / "hooks"

    def test_chronos_edit_spec_guard_exists(self):
        """chronos_edit_spec_guard.sh が存在する。"""
        path = self.HOOKS_DIR / "chronos_edit_spec_guard.sh"
        assert path.exists(), f"hook が見つからない: {path}"

    def test_chronos_edit_spec_guard_executable(self):
        """chronos_edit_spec_guard.sh が実行可能。"""
        path = self.HOOKS_DIR / "chronos_edit_spec_guard.sh"
        assert os.access(str(path), os.X_OK), f"hook が実行可能でない: {path}"

    def test_session_start_market_specs_reload_exists(self):
        """session_start_market_specs_reload.sh が存在する。"""
        path = self.HOOKS_DIR / "session_start_market_specs_reload.sh"
        assert path.exists(), f"hook が見つからない: {path}"

    def test_session_start_market_specs_reload_executable(self):
        """session_start_market_specs_reload.sh が実行可能。"""
        path = self.HOOKS_DIR / "session_start_market_specs_reload.sh"
        assert os.access(str(path), os.X_OK), f"hook が実行可能でない: {path}"

    def test_session_start_hook_output_contains_chronos(self):
        """session_start hook を実行すると Chronos の時間帯が表示される。"""
        path = self.HOOKS_DIR / "session_start_market_specs_reload.sh"
        result = subprocess.run(
            ["bash", str(path)],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        assert "Chronos" in output, "Chronos の表示がない"
        assert "07:00" in output, "CME 開場時刻 07:00 の表示がない"

    def test_session_start_hook_output_contains_atlas(self):
        """session_start hook を実行すると Atlas の時間帯が表示される。"""
        path = self.HOOKS_DIR / "session_start_market_specs_reload.sh"
        result = subprocess.run(
            ["bash", str(path)],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr
        assert "Atlas" in output, "Atlas の表示がない"
        assert "22:20" in output, "SPX 開場時刻 22:20 の表示がない"

    def test_chronos_guard_hook_no_trigger_for_non_target(self):
        """chronos_edit_spec_guard は非対象ファイルをスキップする。"""
        import json
        path = self.HOOKS_DIR / "chronos_edit_spec_guard.sh"
        # spy_bot.py ではない別ファイルへの Edit
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/some_other_file.py",
                "old_string": "hello",
                "new_string": "world"
            }
        })
        result = subprocess.run(
            ["bash", str(path)],
            input=payload,
            capture_output=True, text=True, timeout=10
        )
        # 警告が stderr に出ていないこと (対象外なのでスキップ)
        assert "MARKET SPEC GUARD" not in result.stderr

    def test_chronos_guard_hook_triggers_for_chronos_with_time(self):
        """chronos_edit_spec_guard は chronos_*.py + 時間文字列で警告を出す。"""
        import json
        path = self.HOOKS_DIR / "chronos_edit_spec_guard.sh"
        # chronos_bot.py への Edit で 22:20 という SPX 時間をコピーしようとしている
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/Users/yuusakuichio/trading/chronos_bot.py",
                "old_string": "old",
                "new_string": "SESSION_START = (22, 20)  # SPX 時間をコピー"
            }
        })
        result = subprocess.run(
            ["bash", str(path)],
            input=payload,
            capture_output=True, text=True, timeout=10
        )
        # 警告が stderr に出ること
        assert "MARKET SPEC GUARD" in result.stderr, (
            f"警告が表示されなかった。stderr: {result.stderr}"
        )
        assert "Chronos" in result.stderr

    def test_chronos_guard_hook_bypass_with_env(self):
        """MARKET_GUARD_BYPASS=1 で警告をスキップできる。"""
        import json
        path = self.HOOKS_DIR / "chronos_edit_spec_guard.sh"
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/Users/yuusakuichio/trading/chronos_bot.py",
                "old_string": "old",
                "new_string": "START = (22, 20)"
            }
        })
        env = os.environ.copy()
        env["MARKET_GUARD_BYPASS"] = "1"
        result = subprocess.run(
            ["bash", str(path)],
            input=payload,
            capture_output=True, text=True, timeout=10,
            env=env
        )
        # バイパス時は警告なし
        assert "MARKET SPEC GUARD" not in result.stderr

    def test_settings_json_registers_chronos_guard(self):
        """settings.local.json に chronos_edit_spec_guard.sh が登録されている。"""
        import json
        settings_path = PROJECT_ROOT / ".claude" / "settings.local.json"
        assert settings_path.exists(), f"settings.local.json が見つからない: {settings_path}"
        with settings_path.open(encoding="utf-8") as f:
            settings = json.load(f)
        hooks = settings.get("hooks", {})
        pre_tool = hooks.get("PreToolUse", [])
        commands = []
        for item in pre_tool:
            for h in item.get("hooks", []):
                commands.append(h.get("command", ""))
        assert any("chronos_edit_spec_guard" in c for c in commands), (
            "chronos_edit_spec_guard.sh が PreToolUse に登録されていない"
            + "登録済みコマンド: " + str(commands)
        )

    def test_settings_json_registers_session_start_hook(self):
        """settings.local.json に session_start_market_specs_reload.sh が登録されている。"""
        import json
        settings_path = PROJECT_ROOT / ".claude" / "settings.local.json"
        with settings_path.open(encoding="utf-8") as f:
            settings = json.load(f)
        hooks = settings.get("hooks", {})
        session_hooks = hooks.get("SessionStart", [])
        commands = []
        for item in session_hooks:
            for h in item.get("hooks", []):
                commands.append(h.get("command", ""))
        assert any("session_start_market_specs_reload" in c for c in commands), (
            "session_start_market_specs_reload.sh が SessionStart に登録されていない" + "登録済みコマンド: " + str(commands)
        )
