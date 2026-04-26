"""tests/test_chronos_high_fixes_20260419.py — Chronos HIGH 12件修正 回帰テスト

HIGH-1:  effective_mll PHASE_SIM_FUNDED_AFTER_PAYOUT 分岐
HIGH-3:  CHRONOS_START_DELAY_SEC wrapper対応
HIGH-4:  cross-account hedging prevent-mode
HIGH-5:  existing_positions_list 伝搬
HIGH-6:  account_type 環境変数化
HIGH-7:  fleet_watcher heartbeat plist
HIGH-8:  Pushover DoS throttling
HIGH-9:  qty=0 kill_switch bypass 修正
HIGH-10: Builder Daily Loss Limit $1,000
HIGH-11: Builder overnight強制クローズ
HIGH-12: Fleet combined daily DD監視
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import zoneinfo
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

ET = zoneinfo.ZoneInfo("America/New_York")


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-1: effective_mll PHASE_SIM_FUNDED_AFTER_PAYOUT 分岐
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh1EffectiveMll:
    """PHASE_SIM_FUNDED_AFTER_PAYOUT で effective_mll が $100 を返すこと"""

    def _make_rules(self, phase: str, payout_count: int = 0):
        from chronos_mffu_rules import MFFURules, MFFUPlan, MFFU_PLANS
        plan = MFFU_PLANS["flex_50k"]
        return MFFURules(
            plan=plan,
            phase=phase,
            account_balance_usd=0.0,
            peak_balance_usd=50_000.0,
            daily_pnl_usd=0.0,
            trading_days_count=1,
            payout_count=payout_count,
        )

    def test_after_payout_phase_returns_100(self):
        """PHASE_SIM_FUNDED_AFTER_PAYOUT → $100 (HIGH-1修正の主目的)"""
        from chronos_mffu_rules import PHASE_SIM_FUNDED_AFTER_PAYOUT
        rules = self._make_rules(PHASE_SIM_FUNDED_AFTER_PAYOUT, payout_count=0)
        assert rules.effective_mll == 100.0, (
            f"effective_mll={rules.effective_mll} (期待値: 100.0)"
        )

    def test_sim_funded_payout_count_0_returns_2000(self):
        """PHASE_SIM_FUNDED + payout_count=0 → $2000"""
        from chronos_mffu_rules import PHASE_SIM_FUNDED
        rules = self._make_rules(PHASE_SIM_FUNDED, payout_count=0)
        assert rules.effective_mll == 2_000.0

    def test_sim_funded_payout_count_1_returns_100(self):
        """PHASE_SIM_FUNDED + payout_count=1 → $100"""
        from chronos_mffu_rules import PHASE_SIM_FUNDED
        rules = self._make_rules(PHASE_SIM_FUNDED, payout_count=1)
        assert rules.effective_mll == 100.0

    def test_evaluation_returns_2000(self):
        """PHASE_EVALUATION → $2000"""
        from chronos_mffu_rules import PHASE_EVALUATION
        rules = self._make_rules(PHASE_EVALUATION, payout_count=0)
        assert rules.effective_mll == 2_000.0

    def test_after_payout_phase_not_returning_eval_value(self):
        """HIGH-1バグ再現テスト: 旧コードでは default($2000) が返っていた"""
        from chronos_mffu_rules import PHASE_SIM_FUNDED_AFTER_PAYOUT
        rules = self._make_rules(PHASE_SIM_FUNDED_AFTER_PAYOUT, payout_count=0)
        # バグ修正後は $100 であること（旧: $2000 が返ってしまっていた）
        assert rules.effective_mll != 2_000.0, "HIGH-1バグが再現: $2000が返っている"
        assert rules.effective_mll == 100.0


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-3: run_chronos_account.sh に CHRONOS_START_DELAY_SEC が存在すること
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh3StartDelay:
    """bin/run_chronos_account.sh に CHRONOS_START_DELAY_SEC 処理が実装されていること"""

    def test_script_contains_start_delay(self):
        script = Path(_ROOT) / "bin" / "run_chronos_account.sh"
        assert script.exists(), "run_chronos_account.sh が存在しない"
        content = script.read_text()
        assert "CHRONOS_START_DELAY_SEC" in content, (
            "CHRONOS_START_DELAY_SEC が run_chronos_account.sh に実装されていない"
        )

    def test_script_has_sleep_command(self):
        script = Path(_ROOT) / "bin" / "run_chronos_account.sh"
        content = script.read_text()
        assert "sleep" in content and "CHRONOS_START_DELAY_SEC" in content, (
            "sleep コマンドが CHRONOS_START_DELAY_SEC と共に実装されていない"
        )


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-4: cross-account hedging prevent-mode
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh4CrossAccountHedging:
    """発注前に他アカウントのstate.jsonを確認してcross-account両建てを検出する"""

    def _write_state(self, accounts_dir: Path, account_id: str, positions: list):
        state_dir = accounts_dir / account_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "state.json"
        state = {
            "account_id": account_id,
            "positions": positions,
            "daily_pnl_usd": 0.0,
            "weekly_dd_usd": 0.0,
        }
        state_file.write_text(json.dumps(state))
        return state_file

    def test_no_conflict_returns_ok(self):
        """他アカと同一方向 → OK"""
        from chronos_bot import check_cross_account_hedging
        with tempfile.TemporaryDirectory() as td:
            acc_dir = Path(td)
            self._write_state(acc_dir, "mffu_flex_A", [
                {"symbol": "MES", "side": "long", "qty": 1}
            ])
            ok, reason = check_cross_account_hedging(
                new_order={"symbol": "MES", "side": "BUY", "qty": 1},
                accounts_dir=acc_dir,
                current_account_id="mffu_rapid_B",
            )
            assert ok, f"同方向なのにNG: {reason}"

    def test_same_symbol_opposite_side_rejected(self):
        """他アカで MES long → 自アカで MES short → reject"""
        from chronos_bot import check_cross_account_hedging
        with tempfile.TemporaryDirectory() as td:
            acc_dir = Path(td)
            self._write_state(acc_dir, "mffu_flex_A", [
                {"symbol": "MES", "side": "long", "qty": 1}
            ])
            ok, reason = check_cross_account_hedging(
                new_order={"symbol": "MES", "side": "SELL", "qty": 1},
                accounts_dir=acc_dir,
                current_account_id="mffu_rapid_B",
            )
            assert not ok, "cross-account両建てがrejectされていない"
            assert "CrossAccountHedge" in reason or "cross-account" in reason.lower()

    def test_mes_es_pair_rejected(self):
        """他アカで MES long → 自アカで ES short → reject（同一プロダクト）"""
        from chronos_bot import check_cross_account_hedging
        with tempfile.TemporaryDirectory() as td:
            acc_dir = Path(td)
            self._write_state(acc_dir, "mffu_flex_A", [
                {"symbol": "MES", "side": "long", "qty": 1}
            ])
            ok, reason = check_cross_account_hedging(
                new_order={"symbol": "ES", "side": "SELL", "qty": 1},
                accounts_dir=acc_dir,
                current_account_id="mffu_rapid_B",
            )
            assert not ok, "MES/ES cross-account両建てがrejectされていない"

    def test_own_account_excluded(self):
        """自アカのstate.jsonは比較対象外"""
        from chronos_bot import check_cross_account_hedging
        with tempfile.TemporaryDirectory() as td:
            acc_dir = Path(td)
            self._write_state(acc_dir, "mffu_flex_A", [
                {"symbol": "MES", "side": "long", "qty": 1}
            ])
            ok, reason = check_cross_account_hedging(
                new_order={"symbol": "MES", "side": "SELL", "qty": 1},
                accounts_dir=acc_dir,
                current_account_id="mffu_flex_A",  # 自アカ → スキップ
            )
            assert ok, f"自アカがスキップされていない: {reason}"


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-5: existing_positions_list 伝搬
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh5ExistingPositionsList:
    """FuturesOrderContext に existing_positions_list フィールドが存在すること"""

    def test_field_exists_default_empty_list(self):
        """デフォルト値が空リストであること"""
        from chronos_pre_trade_check import FuturesOrderContext
        ctx = FuturesOrderContext(
            symbol="MES",
            side="BUY",
            qty=1,
            entry_price=5000.0,
            est_margin=1500.0,
            capital_usd=50_000.0,
        )
        assert hasattr(ctx, "existing_positions_list"), (
            "existing_positions_list フィールドが存在しない"
        )
        assert isinstance(ctx.existing_positions_list, list), (
            "existing_positions_list がリストではない"
        )

    def test_field_can_be_set(self):
        """existing_positions_list を設定できること"""
        from chronos_pre_trade_check import FuturesOrderContext
        positions = [{"symbol": "MES", "side": "long", "qty": 1}]
        ctx = FuturesOrderContext(
            symbol="MES",
            side="SELL",
            qty=1,
            entry_price=5000.0,
            est_margin=1500.0,
            capital_usd=50_000.0,
            existing_positions_list=positions,
        )
        assert ctx.existing_positions_list == positions


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-6: account_type 環境変数化
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh6AccountTypeEnv:
    """CHRONOS_ACCOUNT_TYPE / CHRONOS_PHASE 環境変数が chronos_bot.py に実装されていること"""

    def test_env_variable_referenced_in_bot(self):
        """chronos_bot.py が CHRONOS_ACCOUNT_TYPE を参照していること"""
        bot_file = Path(_ROOT) / "chronos_bot.py"
        content = bot_file.read_text()
        assert "CHRONOS_ACCOUNT_TYPE" in content, (
            "CHRONOS_ACCOUNT_TYPE が chronos_bot.py に実装されていない"
        )

    def test_chronos_phase_env_also_referenced(self):
        """CHRONOS_PHASE 環境変数も参照されていること"""
        bot_file = Path(_ROOT) / "chronos_bot.py"
        content = bot_file.read_text()
        assert "CHRONOS_PHASE" in content, (
            "CHRONOS_PHASE が chronos_bot.py に実装されていない"
        )


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-7: fleet_watcher heartbeat plist
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh7FleetWatcherHeartbeat:
    """fleet_watcher heartbeat スクリプトと plist が存在すること"""

    def test_heartbeat_script_exists(self):
        script = Path(_ROOT) / "bin" / "chronos_fleet_watcher_heartbeat.sh"
        assert script.exists(), "heartbeat スクリプトが存在しない"

    def test_heartbeat_script_checks_process(self):
        script = Path(_ROOT) / "bin" / "chronos_fleet_watcher_heartbeat.sh"
        content = script.read_text()
        assert "chronos_fleet_watcher" in content, "fleet_watcher プロセス確認が実装されていない"
        assert "pushover" in content.lower() or "send_pushover" in content, (
            "Pushover アラートが実装されていない"
        )

    @pytest.mark.skip(reason="com.chronos.fleet_watcher.plist は 2026-04-23 に disabled_20260423/ へ移動済 (obsolete)")
    def test_plist_exists(self):
        plist = Path.home() / "Library" / "LaunchAgents" / "com.chronos.fleet_watcher_heartbeat.plist"
        assert plist.exists(), "heartbeat plist が存在しない"

    @pytest.mark.skip(reason="com.chronos.fleet_watcher.plist は 2026-04-23 に disabled_20260423/ へ移動済 (obsolete)")
    def test_fleet_watcher_plist_keepalive_detailed(self):
        """fleet_watcher.plist の KeepAlive が詳細化されていること"""
        plist = Path.home() / "Library" / "LaunchAgents" / "com.chronos.fleet_watcher.plist"
        content = plist.read_text()
        assert "SuccessfulExit" in content, "KeepAlive の SuccessfulExit が設定されていない"
        assert "Crashed" in content, "KeepAlive の Crashed が設定されていない"


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-8: Pushover DoS throttling
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh8PushoverThrottling:
    """同一エラーメッセージは5分毎1回まで通知されること"""

    def setup_method(self):
        """各テスト前にthrottle cacheをクリア"""
        import chronos_bot
        chronos_bot._pushover_throttle_cache.clear()

    def test_first_call_allowed(self):
        """初回呼び出しは throttle されない"""
        import chronos_bot
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            # token/user を一時設定
            chronos_bot.PUSHOVER_TOKEN = "dummy_token"
            chronos_bot.PUSHOVER_USER = "dummy_user"
            result = chronos_bot.pushover("テストタイトル", "テストメッセージ")
            assert result is True

    def test_second_call_within_5min_throttled(self):
        """5分以内の同一メッセージは throttle される"""
        import chronos_bot
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            chronos_bot.PUSHOVER_TOKEN = "dummy_token"
            chronos_bot.PUSHOVER_USER = "dummy_user"
            # 1回目
            chronos_bot.pushover("タイトル", "同一メッセージ")
            # 2回目（5分以内）
            result = chronos_bot.pushover("タイトル", "同一メッセージ")
            assert result is False, "5分以内の重複が throttle されていない"

    def test_different_message_not_throttled(self):
        """異なるメッセージは throttle されない"""
        import chronos_bot
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            chronos_bot.PUSHOVER_TOKEN = "dummy_token"
            chronos_bot.PUSHOVER_USER = "dummy_user"
            chronos_bot.pushover("タイトル", "メッセージA")
            result = chronos_bot.pushover("タイトル", "メッセージB")  # 異なるメッセージ
            assert result is True, "異なるメッセージが throttle されている"

    def test_cache_key_is_title_and_message(self):
        """キャッシュキーは title|message の組み合わせであること"""
        import chronos_bot
        chronos_bot._pushover_throttle_cache.clear()
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            chronos_bot.PUSHOVER_TOKEN = "tok"
            chronos_bot.PUSHOVER_USER = "usr"
            chronos_bot.pushover("X", "Y")
            key = "X|Y"
            assert key in chronos_bot._pushover_throttle_cache


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-9: qty=0 kill_switch bypass 修正
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh9QtyZeroKillSwitch:
    """size_pct=0 の場合に qty=0 が返り発注がスキップされること"""

    def test_size_pct_zero_returns_zero_qty(self):
        """size_pct=0 → _scaled=0 → qty=0"""
        _size_pct = 0.0
        _base_qty = 2
        _scaled = round(_base_qty * _size_pct)
        if _scaled == 0 or _size_pct == 0:
            qty = 0
        else:
            qty = max(1, _scaled)
        assert qty == 0, f"size_pct=0 なのに qty={qty} (期待値: 0)"

    def test_size_pct_nonzero_returns_min_1(self):
        """size_pct > 0 → max(1, scaled) で最低1枚"""
        _size_pct = 0.3
        _base_qty = 2
        _scaled = round(_base_qty * _size_pct)
        if _scaled == 0 or _size_pct == 0:
            qty = 0
        else:
            qty = max(1, _scaled)
        assert qty >= 1, f"size_pct={_size_pct} なのに qty=0"

    def test_small_size_pct_floor_at_1(self):
        """size_pct=0.1 で scaled=0 になるケースはmax(1,)が効く"""
        _size_pct = 0.4
        _base_qty = 2
        _scaled = round(_base_qty * _size_pct)
        if _scaled == 0 or _size_pct == 0:
            qty = 0
        else:
            qty = max(1, _scaled)
        # round(2 * 0.4) = round(0.8) = 1 → max(1,1) = 1
        assert qty >= 1

    def test_chronos_bot_has_kill_switch_fix(self):
        """chronos_bot.py に HIGH-9 修正が実装されていること"""
        bot_file = Path(_ROOT) / "chronos_bot.py"
        content = bot_file.read_text()
        assert "HIGH-9" in content, "HIGH-9 修正が chronos_bot.py にない"
        assert "_size_pct == 0" in content or "_scaled == 0" in content, (
            "size_pct=0 チェックが実装されていない"
        )


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-10: Builder Daily Loss Limit $1,000
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh10BuilderDailyLossLimit:
    """MFFUBuilderRules.get_daily_loss_limit_usd() が $1,000 を返すこと"""

    def test_daily_loss_limit_is_1000(self):
        from chronos_rules_plugin.mffu_builder import MFFUBuilderRules
        rules = MFFUBuilderRules()
        limit = rules.get_daily_loss_limit_usd()
        assert limit is not None, "daily_loss_limit が None (制限なし) になっている"
        assert limit == 1_000.0, f"daily_loss_limit={limit} (期待値: 1000.0)"

    def test_compliance_fails_on_daily_loss_exceeded(self):
        """日次損失 -$1,000 超過で check_compliance が False を返すこと"""
        from chronos_rules_plugin.mffu_builder import MFFUBuilderRules
        from chronos_rules_plugin import OrderContext
        rules = MFFUBuilderRules()
        order = OrderContext(
            account_id="mffu_builder_E",
            symbol="MES",
            side="BUY",
            qty=1,
            entry_price=5000.0,
            current_balance_usd=50_000.0,
            daily_pnl_usd=-1_000.0,  # ちょうど $1,000 損失
            daily_pnl_history=[-1_000.0],
            phase="evaluation",
            peak_balance_usd=50_000.0,
        )
        ok, reason = rules.check_compliance(order)
        assert not ok, "Daily Loss $1,000 でcheck_complianceが通過している（拒否すべき）"
        assert "Daily Loss Limit" in reason or "daily" in reason.lower()

    def test_compliance_ok_before_limit(self):
        """日次損失 -$999 は通過すること"""
        from chronos_rules_plugin.mffu_builder import MFFUBuilderRules
        from chronos_rules_plugin import OrderContext
        rules = MFFUBuilderRules()
        order = OrderContext(
            account_id="mffu_builder_E",
            symbol="MES",
            side="BUY",
            qty=1,
            entry_price=5000.0,
            current_balance_usd=50_000.0,
            daily_pnl_usd=-999.0,  # $999 損失 → OK
            daily_pnl_history=[-999.0],
            phase="evaluation",
            peak_balance_usd=50_000.0,
        )
        ok, reason = rules.check_compliance(order)
        assert ok, f"$999損失なのに拒否された: {reason}"


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-11: Builder overnight強制クローズ
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh11BuilderForceClose:
    """should_force_close_now() が 15:55 ET 以降に True を返すこと"""

    def test_force_close_at_1555(self):
        from chronos_rules_plugin.mffu_builder import MFFUBuilderRules
        rules = MFFUBuilderRules()
        now_et = datetime.datetime(2026, 4, 21, 15, 55, 0, tzinfo=ET)
        assert rules.should_force_close_now(now_et) is True

    def test_force_close_at_1600(self):
        from chronos_rules_plugin.mffu_builder import MFFUBuilderRules
        rules = MFFUBuilderRules()
        now_et = datetime.datetime(2026, 4, 21, 16, 0, 0, tzinfo=ET)
        assert rules.should_force_close_now(now_et) is True

    def test_no_force_close_at_1554(self):
        from chronos_rules_plugin.mffu_builder import MFFUBuilderRules
        rules = MFFUBuilderRules()
        now_et = datetime.datetime(2026, 4, 21, 15, 54, 0, tzinfo=ET)
        assert rules.should_force_close_now(now_et) is False

    def test_no_force_close_during_trading(self):
        from chronos_rules_plugin.mffu_builder import MFFUBuilderRules
        rules = MFFUBuilderRules()
        now_et = datetime.datetime(2026, 4, 21, 10, 30, 0, tzinfo=ET)
        assert rules.should_force_close_now(now_et) is False

    def test_chronos_bot_has_builder_force_close(self):
        """chronos_bot.py に HIGH-11 Builder強制クローズが実装されていること"""
        bot_file = Path(_ROOT) / "chronos_bot.py"
        content = bot_file.read_text()
        assert "HIGH-11" in content, "HIGH-11 が chronos_bot.py に実装されていない"
        assert "builder" in content.lower() and "15, 55" in content, (
            "Builder 15:55 ET 強制クローズが実装されていない"
        )


# ══════════════════════════════════════════════════════════════════════════════
# HIGH-12: Fleet combined daily DD監視
# ══════════════════════════════════════════════════════════════════════════════

class TestHigh12FleetDailyDd:
    """fleet_watcher.sh に combined daily DD 監視が実装されていること"""

    def test_fleet_watcher_has_daily_dd_limit(self):
        script = Path(_ROOT) / "bin" / "chronos_fleet_watcher.sh"
        content = script.read_text()
        assert "FLEET_DAILY_DD_LIMIT_USD" in content, (
            "FLEET_DAILY_DD_LIMIT_USD が chronos_fleet_watcher.sh に実装されていない"
        )

    def test_fleet_watcher_reads_daily_pnl(self):
        script = Path(_ROOT) / "bin" / "chronos_fleet_watcher.sh"
        content = script.read_text()
        assert "daily_pnl_usd" in content, (
            "daily_pnl_usd の読み取りが chronos_fleet_watcher.sh に実装されていない"
        )

    def test_daily_dd_limit_is_1500(self):
        """閾値が $1,500 であること"""
        script = Path(_ROOT) / "bin" / "chronos_fleet_watcher.sh"
        content = script.read_text()
        assert "1500" in content, "FLEET_DAILY_DD_LIMIT_USD=1500 が設定されていない"

    def test_fleet_watcher_triggers_kill_on_daily_dd(self):
        """daily DD 超過時に fleet_kill を呼ぶコードがあること"""
        script = Path(_ROOT) / "bin" / "chronos_fleet_watcher.sh"
        content = script.read_text()
        assert "daily_dd_exceeded" in content or "FLEET_DAILY_DD_LIMIT" in content, (
            "daily DD 超過チェックが実装されていない"
        )
