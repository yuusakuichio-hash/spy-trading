"""
tests/test_prop_firm_rules.py — common/prop_firm_rules.py 各関数テスト

各チェック関数に 正常 / 境界 / 違反 の 3 パターン以上を実装。
統合チェック check_prop_firm_compliance() の PF-1 テスト 10 件を含む。
"""

import datetime
import pytest

from common.prop_firm_rules import (
    reload_rules,
    check_mll_breach,
    check_daily_loss_limit,
    check_consistency,
    check_max_contracts,
    check_hft_daily_count,
    check_microscalping,
    check_hedging,
    check_t1_news_blackout,
    check_dca_pattern,
    check_inactivity,
    check_payout_eligibility_with_freeze,
    check_prop_firm_compliance,
    get_plan_rules,
    is_rapid_enabled,
)


@pytest.fixture(autouse=True)
def _reload():
    reload_rules()
    yield


# ── check_mll_breach ─────────────────────────────────────────────────────────

class TestCheckMllBreach:
    def test_intraday_ok(self):
        ok, msg = check_mll_breach(51000, 52000, 2000, "intraday_trailing_4pct")
        assert ok

    def test_intraday_breach(self):
        """Intraday: peak 52000 → current 49999 → DD=2001 >= MLL=2000"""
        ok, msg = check_mll_breach(49999, 52000, 2000, "intraday_trailing_4pct")
        assert not ok
        assert "MLL超過" in msg

    def test_intraday_warning_80pct(self):
        """Intraday: DD=1600 = MLL*0.80 → 予兆ブロック"""
        ok, msg = check_mll_breach(50400, 52000, 2000, "intraday_trailing_4pct")
        assert not ok
        assert "予兆" in msg

    def test_eod_trailing_ok(self):
        ok, msg = check_mll_breach(50500, 51000, 1500, "eod_trailing_3pct")
        assert ok

    def test_eod_trailing_breach(self):
        """EOD: floor=51000-1500=49500. balance=49499 < floor"""
        ok, msg = check_mll_breach(49499, 51000, 1500, "eod_trailing_3pct")
        assert not ok

    def test_eod_trailing_warning_80pct(self):
        """EOD: floor=49500, buffer=1500*0.20=300 → balance < floor+300"""
        ok, msg = check_mll_breach(49700, 51000, 1500, "eod_trailing_3pct")
        assert not ok
        assert "予兆" in msg

    def test_eod_static_ok(self):
        ok, msg = check_mll_breach(48500, 50000, 2000, "eod_static")
        assert ok

    def test_eod_static_breach(self):
        ok, msg = check_mll_breach(47999, 50000, 2000, "eod_static")
        assert not ok


# ── check_daily_loss_limit ───────────────────────────────────────────────────

class TestCheckDailyLossLimit:
    def test_no_limit(self):
        ok, _ = check_daily_loss_limit(-999999, None)
        assert ok

    def test_zero_limit(self):
        ok, _ = check_daily_loss_limit(-999, 0)
        assert ok

    def test_not_reached(self):
        ok, _ = check_daily_loss_limit(-500, 1000)
        assert ok

    def test_at_limit(self):
        ok, msg = check_daily_loss_limit(-1000, 1000)
        assert not ok
        assert "DLL" in msg

    def test_beyond_limit(self):
        ok, msg = check_daily_loss_limit(-1200, 1000)
        assert not ok

    def test_warning_80pct(self):
        """$800 損失 = $1000 の 80% → 予兆ブロック"""
        ok, msg = check_daily_loss_limit(-800, 1000)
        assert not ok
        assert "予兆" in msg


# ── check_consistency ────────────────────────────────────────────────────────

class TestCheckConsistency:
    def test_empty_cycle_ok(self):
        ok, _ = check_consistency([], 0.40, 0)
        assert ok

    def test_below_limit_ok(self):
        """3日: 300, 200, 100 → 合計600 → max=300/600=50% > 40% 予兆発動"""
        # 50% が 40%×0.9=36% 超なので False
        ok, _ = check_consistency([300.0, 200.0, 100.0], 0.40, 0)
        assert not ok

    def test_well_below_limit_ok(self):
        """均等分布: 5日 × 200 → 各日 20% → OK"""
        ok, _ = check_consistency([200.0, 200.0, 200.0, 200.0, 200.0], 0.40, 0)
        assert ok

    def test_breach_warning_90pct(self):
        """1日 200, 他は小額 → max_day/total が 40%×0.9 を超える"""
        ok, msg = check_consistency([200.0, 10.0, 10.0, 10.0, 10.0], 0.40, 0)
        assert not ok
        assert "Consistency" in msg

    def test_tradeify_35pct(self):
        """Tradeify 35%: 1日 200 out of 500 → 40% > 35%×0.9=31.5%"""
        ok, _ = check_consistency([200.0, 300.0], 0.35, 0)
        assert not ok


# ── check_max_contracts ──────────────────────────────────────────────────────

class TestCheckMaxContracts:
    def test_core_mini_ok(self):
        rules = get_plan_rules("mffu", "core_50k")
        ok, _ = check_max_contracts(5, rules, 50000, "mini")
        assert ok

    def test_core_mini_exceed(self):
        rules = get_plan_rules("mffu", "core_50k")
        ok, msg = check_max_contracts(6, rules, 50000, "mini")
        assert not ok
        assert "枚数上限" in msg

    def test_core_micro_scaled(self):
        rules = get_plan_rules("mffu", "core_50k")
        ok, _ = check_max_contracts(50, rules, 50000, "micro")
        assert ok

    def test_core_micro_exceed(self):
        rules = get_plan_rules("mffu", "core_50k")
        ok, msg = check_max_contracts(51, rules, 50000, "micro")
        assert not ok

    def test_flex_funded_tiers_low_balance(self):
        """Flex: balance < $1500 → max=2"""
        rules = get_plan_rules("mffu", "flex_50k")
        ok, msg = check_max_contracts(3, rules, 1000, "mini")
        assert not ok
        assert "残高連動" in msg

    def test_flex_funded_tiers_mid_balance(self):
        """Flex: balance=$1600 → max=3"""
        rules = get_plan_rules("mffu", "flex_50k")
        ok, _ = check_max_contracts(3, rules, 1600, "mini")
        assert ok

    def test_flex_funded_tiers_high_balance(self):
        """Flex: balance=$2100 → max=5"""
        rules = get_plan_rules("mffu", "flex_50k")
        ok, _ = check_max_contracts(5, rules, 2100, "mini")
        assert ok

    def test_builder_50k_max_4(self):
        rules = get_plan_rules("mffu", "builder_50k")
        ok, _ = check_max_contracts(4, rules, 50000, "mini")
        assert ok
        ok2, _ = check_max_contracts(5, rules, 50000, "mini")
        assert not ok2


# ── check_hft_daily_count ────────────────────────────────────────────────────

class TestCheckHftDailyCount:
    def test_ok_below_limit(self):
        ok, _ = check_hft_daily_count(100)
        assert ok

    def test_at_limit_blocked(self):
        ok, msg = check_hft_daily_count(180)
        assert not ok
        assert "HFT" in msg

    def test_over_limit_blocked(self):
        ok, _ = check_hft_daily_count(181)
        assert not ok

    def test_zero_ok(self):
        ok, _ = check_hft_daily_count(0)
        assert ok


# ── check_microscalping ──────────────────────────────────────────────────────

class TestCheckMicroscalping:
    def _make_trades(self, n, hold_sec):
        base = datetime.datetime(2026, 4, 20, 10, 0, 0)
        trades = []
        for i in range(n):
            entry = base + datetime.timedelta(minutes=i)
            exit_ = entry + datetime.timedelta(seconds=hold_sec)
            trades.append({"entry_ts": entry, "exit_ts": exit_})
        return trades

    def test_few_trades_ok(self):
        trades = self._make_trades(3, 5)  # <5 件: スキップ
        ok, _ = check_microscalping(trades)
        assert ok

    def test_all_long_holds_ok(self):
        trades = self._make_trades(20, 30)  # 全て 30 秒
        ok, _ = check_microscalping(trades)
        assert ok

    def test_50pct_short_blocked(self):
        """20 件中 10 件が 5 秒 → 50% > 40% でブロック"""
        long_trades = self._make_trades(10, 30)
        short_trades = self._make_trades(10, 5)
        trades = long_trades + short_trades
        ok, msg = check_microscalping(trades)
        assert not ok
        assert "Microscalping" in msg

    def test_exactly_40pct_ok(self):
        """20 件中 8 件が 5 秒 → 40% = 上限ちょうど → OK（>ではなく>=で判定）"""
        long_trades = self._make_trades(12, 30)
        short_trades = self._make_trades(8, 5)
        trades = long_trades + short_trades
        ok, _ = check_microscalping(trades)
        assert ok

    def test_open_positions_excluded(self):
        """exit_ts なし（オープン中）のトレードは除外される"""
        base = datetime.datetime(2026, 4, 20, 10, 0, 0)
        trades = [{"entry_ts": base + datetime.timedelta(minutes=i), "exit_ts": None} for i in range(20)]
        ok, _ = check_microscalping(trades)
        assert ok  # closed < 5 件 → スキップ


# ── check_hedging ────────────────────────────────────────────────────────────

class TestCheckHedging:
    def test_no_positions_ok(self):
        ok, _ = check_hedging("MES", "BUY", [])
        assert ok

    def test_same_symbol_opposite_side_blocked(self):
        ok, msg = check_hedging("MES", "BUY", [{"symbol": "MES", "side": "SELL"}])
        assert not ok
        assert "両建て" in msg

    def test_correlated_pair_blocked(self):
        """MES long + ES short → 相関ヘッジ禁止"""
        ok, msg = check_hedging("MES", "BUY", [{"symbol": "ES", "side": "SELL"}])
        assert not ok
        assert "相関ヘッジ禁止" in msg

    def test_same_direction_ok(self):
        ok, _ = check_hedging("MES", "BUY", [{"symbol": "MES", "side": "BUY"}])
        assert ok

    def test_different_product_ok(self):
        """MES と NQ は相関ペアではない → OK"""
        ok, _ = check_hedging("MES", "BUY", [{"symbol": "NQ", "side": "SELL"}])
        assert ok

    def test_mnq_nq_pair_blocked(self):
        ok, msg = check_hedging("MNQ", "BUY", [{"symbol": "NQ", "side": "SELL"}])
        assert not ok


# ── check_t1_news_blackout ───────────────────────────────────────────────────

class TestCheckT1NewsBlackout:
    def _make_event(self, delta_sec, tier=1, name="FOMC"):
        ts = datetime.datetime.now() + datetime.timedelta(seconds=delta_sec)
        return {"tier": tier, "ts": ts, "name": name}

    def test_no_events_ok(self):
        ok, _ = check_t1_news_blackout(datetime.datetime.now(), [], "evaluation", {})
        assert ok

    def test_within_blackout_blocked(self):
        events = [self._make_event(60)]  # 60 秒後 < 120 秒
        ok, msg = check_t1_news_blackout(datetime.datetime.now(), events, "evaluation", {})
        assert not ok
        assert "blackout" in msg

    def test_outside_blackout_ok(self):
        events = [self._make_event(200)]  # 200 秒後 > 120 秒
        ok, _ = check_t1_news_blackout(datetime.datetime.now(), events, "evaluation", {})
        assert ok

    def test_flex_funded_t1_allowed(self):
        """Flex funded は T1 ニュース中取引許可"""
        events = [self._make_event(30)]
        ok, _ = check_t1_news_blackout(
            datetime.datetime.now(), events, "funded",
            {"t1_news_funded_allowed": True},
        )
        assert ok

    def test_tier2_not_blocked(self):
        """Tier 2 ニュースはブロック対象外"""
        events = [{"tier": 2, "ts": datetime.datetime.now() + datetime.timedelta(seconds=10), "name": "ISM"}]
        ok, _ = check_t1_news_blackout(datetime.datetime.now(), events, "evaluation", {})
        assert ok


# ── check_dca_pattern ────────────────────────────────────────────────────────

class TestCheckDcaPattern:
    def test_non_apex_ok(self):
        """MFFU は DCA チェック対象外"""
        ok, _ = check_dca_pattern(
            "MES", "BUY",
            [{"symbol": "MES", "side": "BUY", "unrealized_pnl": -500}],
            "mffu", "evaluation",
        )
        assert ok

    def test_apex_non_pa_ok(self):
        ok, _ = check_dca_pattern(
            "MES", "BUY",
            [{"symbol": "MES", "side": "BUY", "unrealized_pnl": -500}],
            "apex", "evaluation",
        )
        assert ok

    def test_apex_pa_loss_position_blocked(self):
        ok, msg = check_dca_pattern(
            "MES", "BUY",
            [{"symbol": "MES", "side": "BUY", "unrealized_pnl": -100}],
            "apex", "pa",
        )
        assert not ok
        assert "DCA" in msg

    def test_apex_pa_profit_position_ok(self):
        """含み益ポジへの追加は許可"""
        ok, _ = check_dca_pattern(
            "MES", "BUY",
            [{"symbol": "MES", "side": "BUY", "unrealized_pnl": 100}],
            "apex", "pa",
        )
        assert ok


# ── check_inactivity ─────────────────────────────────────────────────────────

class TestCheckInactivity:
    def test_none_ok(self):
        ok, _ = check_inactivity(None)
        assert ok

    @staticmethod
    def _et_today():
        try:
            import zoneinfo as _zi
            _tz = _zi.ZoneInfo("America/New_York")
        except ImportError:
            import pytz as _pytz
            _tz = _pytz.timezone("America/New_York")
        return datetime.datetime.now(tz=_tz).date()

    def test_recent_ok(self):
        ok, _ = check_inactivity(self._et_today() - datetime.timedelta(days=3))
        assert ok

    def test_day_6_warning(self):
        ok, msg = check_inactivity(self._et_today() - datetime.timedelta(days=6))
        assert not ok
        assert "予兆" in msg

    def test_day_7_expired(self):
        ok, msg = check_inactivity(self._et_today() - datetime.timedelta(days=7))
        assert not ok
        assert "失効" in msg

    def test_day_8_expired(self):
        ok, msg = check_inactivity(self._et_today() - datetime.timedelta(days=8))
        assert not ok


# ── check_prop_firm_compliance（統合: PF-1 テスト 10 件） ──────────────────────

class TestCheckPropFirmCompliance:
    def _base_state(self):
        return {
            "balance": 50000,
            "peak_balance": 50000,
            "daily_pnl": 0,
            "cycle_daily_pnl": [],
            "trades_today": 0,
            "recent_trades": [],
            "open_positions": [],
            "last_trade_date": datetime.date.today(),
            "payout_count": 0,
        }

    def _base_order(self):
        return {
            "symbol": "MES",
            "side": "BUY",
            "qty": 1,
            "contract_type": "mini",
            "est_pnl": 0,
            "upcoming_events": [],
        }

    def test_pf1_rapid_disabled_blocked(self):
        """Rapid は Phase A 完了まで起動禁止"""
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "rapid_50k", "evaluation",
            self._base_state(), self._base_order(),
        )
        assert not allow
        assert "RAPID-DISABLED" in layer

    def test_pf1_mll_breach_blocked(self):
        """MLL 超過で PF-1-MLL"""
        state = self._base_state()
        state["balance"] = 48000  # peak=50000, balance=48000, DD=2000 >= MLL=1500
        state["peak_balance"] = 50000
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "core_50k", "evaluation",
            state, self._base_order(),
        )
        assert not allow
        assert "MLL" in layer

    def test_pf1_dll_breach_blocked(self):
        """Builder DLL soft pause"""
        state = self._base_state()
        state["daily_pnl"] = -1000
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "builder_50k", "funded",
            state, self._base_order(),
        )
        assert not allow
        assert "DLL" in layer

    def test_pf1_consistency_blocked(self):
        """Core Funded Consistency 40% 違反予兆"""
        state = self._base_state()
        state["cycle_daily_pnl"] = [500.0, 100.0, 100.0]  # max=500, total=700 → 71%
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "core_50k", "funded",
            state, self._base_order(),
        )
        assert not allow
        assert "CON" in layer

    def test_pf1_qty_exceeded_blocked(self):
        """枚数上限超過"""
        state = self._base_state()
        order = self._base_order()
        order["qty"] = 6  # Core max=5
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "core_50k", "evaluation",
            state, order,
        )
        assert not allow
        assert "QTY" in layer

    def test_pf1_hft_blocked(self):
        """HFT 180 件上限"""
        state = self._base_state()
        state["trades_today"] = 180
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "core_50k", "evaluation",
            state, self._base_order(),
        )
        assert not allow
        assert "HFT" in layer

    def test_pf1_hedging_blocked(self):
        """ヘッジ禁止"""
        state = self._base_state()
        state["open_positions"] = [{"symbol": "MES", "side": "SELL", "unrealized_pnl": 0}]
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "core_50k", "evaluation",
            state, self._base_order(),  # BUY MES with existing SELL MES
        )
        assert not allow
        assert "HEDGE" in layer

    def test_pf1_t1_news_blocked(self):
        """T1 ニュース 30 秒前ブロック"""
        state = self._base_state()
        order = self._base_order()
        # β-1修正: check_order 内の now が ET-aware になったため ev_ts も ET-aware にする
        try:
            import zoneinfo as _zi
            _et = _zi.ZoneInfo("America/New_York")
        except ImportError:
            import pytz as _pytz
            _et = _pytz.timezone("America/New_York")
        order["upcoming_events"] = [
            {"tier": 1, "ts": datetime.datetime.now(tz=_et) + datetime.timedelta(seconds=30), "name": "FOMC"}
        ]
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "core_50k", "evaluation",
            state, order,
        )
        assert not allow
        assert "NEWS" in layer

    def test_pf1_dca_blocked(self):
        """Apex PA DCA ブロック"""
        state = self._base_state()
        state["open_positions"] = [{"symbol": "MES", "side": "BUY", "unrealized_pnl": -200}]
        allow, layer, reason = check_prop_firm_compliance(
            "apex", "apex_50k", "pa",
            state, self._base_order(),
        )
        assert not allow
        assert "DCA" in layer

    def test_pf1_inactivity_blocked(self):
        """Flex 7 日 inactivity 失効"""
        state = self._base_state()
        state["last_trade_date"] = datetime.date.today() - datetime.timedelta(days=7)
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "flex_50k", "sim_funded",
            state, self._base_order(),
        )
        assert not allow
        assert "INACT" in layer

    def test_pf1_all_pass(self):
        """全チェック合格パス"""
        allow, layer, reason = check_prop_firm_compliance(
            "mffu", "core_50k", "evaluation",
            self._base_state(), self._base_order(),
        )
        assert allow
        assert layer == "PF-1-PASS"

    def test_pf1_unknown_firm_rejected(self):
        allow, layer, _ = check_prop_firm_compliance(
            "unknown_firm", "core_50k", "evaluation",
            self._base_state(), self._base_order(),
        )
        assert not allow
        assert "CONFIG" in layer


# ── check_payout_eligibility_with_freeze ─────────────────────────────────────

class TestPayoutFreeze:
    def test_empty_cycle_ok(self):
        ok, _ = check_payout_eligibility_with_freeze({"cycle_daily_pnl": []}, {})
        assert ok

    def test_no_target_pct_ok(self):
        ok, _ = check_payout_eligibility_with_freeze(
            {"cycle_daily_pnl": [1000, 100]}, {"no_consistency": True}
        )
        assert ok

    def test_freeze_triggered(self):
        """最大日が 40%×0.9=36% に到達 → freeze"""
        rules = {"consistency_funded_pct": 0.40}
        state = {"cycle_daily_pnl": [500.0, 100.0, 100.0]}  # max=500/700=71%
        ok, msg = check_payout_eligibility_with_freeze(state, rules)
        assert not ok
        assert "freeze" in msg.lower() or "Freeze" in msg

    def test_below_freeze_threshold_ok(self):
        """均等分布なら freeze なし"""
        rules = {"consistency_funded_pct": 0.40}
        state = {"cycle_daily_pnl": [200.0, 200.0, 200.0]}
        ok, _ = check_payout_eligibility_with_freeze(state, rules)
        assert ok
