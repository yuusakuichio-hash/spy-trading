"""tests/test_ratio_spread_engine_20260425.py — RatioSpreadEngine 15 件テスト

対象: atlas_v3/bots/engines/ratio_spread.py

テスト一覧（15 件）:
    TC-01: 3 leg 発注確認 — long 1 ATM + short 2 OTM の 1:2 ratio
    TC-02: leg 順序・label 確認
    TC-03: OTM short strike = atm_strike + otm_offset_pts
    TC-04: IVR 40-70 フィルタ — IVR=55（境界内）はエントリー許可
    TC-05: IVR 40-70 フィルタ — IVR=39（下限外）はエントリー拒否
    TC-06: IVR 40-70 フィルタ — IVR=71（上限外）はエントリー拒否
    TC-07: IVR 境界値 — IVR=40 はエントリー許可
    TC-08: IVR 境界値 — IVR=70 はエントリー許可
    TC-09: VIX 範囲外（VIX=26）はエントリー拒否
    TC-10: short_otm_call_1 発注失敗 → rollback 発動
    TC-11: short_otm_call_2 発注失敗 → rollback 発動（long + short_1 cancel）
    TC-12: profit 40% 利確判定 — unrealized_pnl = net_credit × 0.40
    TC-13: stop 1.5x 損切り判定 — unrealized_pnl = -(net_credit × 1.5)
    TC-14: 15:40 force close 判定
    TC-15: entry window 外（09:59 ET）はエントリー拒否
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.bots.engines.ratio_spread import (
    NoOpTradeEngine,
    RatioSpreadConfig,
    RatioSpreadEngine,
    RatioSpreadPosition,
)
from atlas_v3.core.env_observer import MarketEnvironment

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _env(ivr: float = 55.0, vix: float = 20.0, symbol: str = "QQQ") -> MarketEnvironment:
    """テスト用 MarketEnvironment を生成する。"""
    return MarketEnvironment(
        vix=vix,
        vrp=10.0,
        ivr_by_symbol={symbol: ivr},
    )


def _clock(h: int, m: int) -> "() -> datetime":
    """指定 ET 時刻を返すクロック関数を生成する。"""
    def fn() -> datetime:
        return datetime.now(_ET).replace(hour=h, minute=m, second=0, microsecond=0)
    return fn


# ---------------------------------------------------------------------------
# TC-01: 3 leg 発注確認 — 1:2 ratio
# ---------------------------------------------------------------------------

class TestRatio12Order:
    """TC-01: long 1 ATM + short 2 OTM の 1:2 ratio 発注確認。"""

    def test_three_legs_total(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=55.0, vix=20.0)
        decision = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)

        assert decision.should_enter is True
        assert len(decision.legs) == 3

    def test_one_long_two_short(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=55.0, vix=20.0)
        decision = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)

        buy_legs = [l for l in decision.legs if l.side == "buy"]
        sell_legs = [l for l in decision.legs if l.side == "sell"]
        assert len(buy_legs) == 1
        assert len(sell_legs) == 2


# ---------------------------------------------------------------------------
# TC-02: leg 順序・label 確認
# ---------------------------------------------------------------------------

class TestLegOrderAndLabel:
    """TC-02: 発注順序 [long_atm_call, short_otm_call_1, short_otm_call_2]。"""

    def test_leg_labels_in_order(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env()
        decision = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)

        labels = [l.label for l in decision.legs]
        assert labels == ["long_atm_call", "short_otm_call_1", "short_otm_call_2"]

    def test_long_leg_is_buy(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env()
        decision = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)

        assert decision.legs[0].side == "buy"
        assert decision.legs[1].side == "sell"
        assert decision.legs[2].side == "sell"


# ---------------------------------------------------------------------------
# TC-03: OTM short strike = atm + otm_offset
# ---------------------------------------------------------------------------

class TestOtmStrike:
    """TC-03: OTM strike = ATM + otm_offset_pts。"""

    def test_otm_strike_value(self) -> None:
        cfg = RatioSpreadConfig(otm_offset_pts=5.0)
        eng = RatioSpreadEngine(config=cfg, clock_fn=_clock(11, 0))
        env = _env()
        decision = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)

        assert decision.legs[1].strike == 455.0
        assert decision.legs[2].strike == 455.0

    def test_atm_long_strike_equals_atm(self) -> None:
        cfg = RatioSpreadConfig(otm_offset_pts=7.0)
        eng = RatioSpreadEngine(config=cfg, clock_fn=_clock(11, 0))
        env = _env()
        decision = eng.should_enter(env, "QQQ", atm_strike=500.0, net_credit=2.0)

        assert decision.legs[0].strike == 500.0
        assert decision.legs[1].strike == 507.0


# ---------------------------------------------------------------------------
# TC-04 - TC-08: IVR フィルタ
# ---------------------------------------------------------------------------

class TestIvrFilter:
    """TC-04 ~ TC-08: IVR 40-70 フィルタ検証。"""

    def test_ivr_in_range_allows_entry(self) -> None:
        """TC-04: IVR=55 → エントリー許可。"""
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=55.0)
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is True

    def test_ivr_below_min_blocks_entry(self) -> None:
        """TC-05: IVR=39 → エントリー拒否。"""
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=39.0)
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is False
        assert "IVR" in dec.reason

    def test_ivr_above_max_blocks_entry(self) -> None:
        """TC-06: IVR=71 → エントリー拒否。"""
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=71.0)
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is False
        assert "IVR" in dec.reason

    def test_ivr_lower_boundary_allows_entry(self) -> None:
        """TC-07: IVR=40（下限値）→ エントリー許可。"""
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=40.0)
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is True

    def test_ivr_upper_boundary_allows_entry(self) -> None:
        """TC-08: IVR=70（上限値）→ エントリー許可。"""
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=70.0)
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is True


# ---------------------------------------------------------------------------
# TC-09: VIX 範囲外
# ---------------------------------------------------------------------------

class TestVixFilter:
    """TC-09: VIX=26 → エントリー拒否。"""

    def test_vix_above_max_blocks_entry(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env(ivr=55.0, vix=26.0)
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is False
        assert "VIX" in dec.reason


# ---------------------------------------------------------------------------
# TC-10 - TC-11: rollback
# ---------------------------------------------------------------------------

class TestRollback:
    """TC-10 ~ TC-11: short leg 発注失敗時の rollback 確認。"""

    def test_short_otm_call_1_fail_triggers_rollback(self) -> None:
        """TC-10: short_otm_call_1 発注失敗 → rollback_triggered=True。"""
        stub = NoOpTradeEngine(fail_on_short_leg=1)
        eng = RatioSpreadEngine(trade_engine=stub, clock_fn=_clock(11, 0))
        env = _env()
        decision = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert decision.should_enter is True

        order_id, result_decision = eng.place_order(decision)
        assert order_id == ""
        assert result_decision.rollback_triggered is True

    def test_short_otm_call_2_fail_triggers_rollback_and_cancels_previous(self) -> None:
        """TC-11: short_otm_call_2 発注失敗 → long + short_1 cancel・rollback_triggered=True。"""
        stub = NoOpTradeEngine(fail_on_short_leg=2)
        eng = RatioSpreadEngine(trade_engine=stub, clock_fn=_clock(11, 0))
        env = _env()
        decision = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert decision.should_enter is True

        order_id, result_decision = eng.place_order(decision)
        assert order_id == ""
        assert result_decision.rollback_triggered is True
        # rollback 後は placed_orders が空になっているはず
        assert len(stub._placed_orders) == 0


# ---------------------------------------------------------------------------
# TC-12: profit 40% 利確
# ---------------------------------------------------------------------------

class TestProfitTarget:
    """TC-12: profit 40% 利確判定。"""

    def test_profit_40pct_triggers_exit(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env()
        pos = RatioSpreadPosition(
            symbol="QQQ",
            quantity=1,
            atm_strike=450.0,
            net_credit=200.0,
            unrealized_pnl=80.0,  # 200 × 0.40
        )
        dec = eng.should_exit(pos, env)
        assert dec.should_exit is True
        assert dec.exit_type == "profit_target"

    def test_profit_below_40pct_holds(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env()
        pos = RatioSpreadPosition(
            symbol="QQQ",
            quantity=1,
            atm_strike=450.0,
            net_credit=200.0,
            unrealized_pnl=79.0,  # 200 × 0.395 < 40%
        )
        dec = eng.should_exit(pos, env)
        assert dec.should_exit is False


# ---------------------------------------------------------------------------
# TC-13: stop 1.5x 損切り
# ---------------------------------------------------------------------------

class TestStopLoss:
    """TC-13: stop 1.5x 損切り判定。"""

    def test_stop_loss_1_5x_triggers_exit(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env()
        pos = RatioSpreadPosition(
            symbol="QQQ",
            quantity=1,
            atm_strike=450.0,
            net_credit=200.0,
            unrealized_pnl=-300.0,  # -(200 × 1.5)
        )
        dec = eng.should_exit(pos, env)
        assert dec.should_exit is True
        assert dec.exit_type == "stop_loss"

    def test_loss_below_stop_holds(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(11, 0))
        env = _env()
        pos = RatioSpreadPosition(
            symbol="QQQ",
            quantity=1,
            atm_strike=450.0,
            net_credit=200.0,
            unrealized_pnl=-299.0,  # < 1.5x → まだ stop に未達
        )
        dec = eng.should_exit(pos, env)
        assert dec.should_exit is False


# ---------------------------------------------------------------------------
# TC-14: 15:40 force close
# ---------------------------------------------------------------------------

class TestForceClose:
    """TC-14: 15:40 ET force close 判定。"""

    def test_force_close_at_1540(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(15, 40))
        env = _env()
        pos = RatioSpreadPosition(
            symbol="QQQ",
            quantity=1,
            atm_strike=450.0,
            net_credit=200.0,
            unrealized_pnl=0.0,
        )
        dec = eng.should_exit(pos, env)
        assert dec.should_exit is True
        assert dec.exit_type == "force_close"

    def test_before_force_close_holds(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(15, 39))
        env = _env()
        pos = RatioSpreadPosition(
            symbol="QQQ",
            quantity=1,
            atm_strike=450.0,
            net_credit=200.0,
            unrealized_pnl=0.0,
        )
        dec = eng.should_exit(pos, env)
        assert dec.should_exit is False


# ---------------------------------------------------------------------------
# TC-15: entry window 外
# ---------------------------------------------------------------------------

class TestEntryWindow:
    """TC-15: entry window 外（09:59 ET）はエントリー拒否。"""

    def test_before_entry_window_blocks_entry(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(9, 59))
        env = _env()
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is False
        assert "entry_window" in dec.reason

    def test_after_entry_window_blocks_entry(self) -> None:
        eng = RatioSpreadEngine(clock_fn=_clock(12, 1))
        env = _env()
        dec = eng.should_enter(env, "QQQ", atm_strike=450.0, net_credit=1.5)
        assert dec.should_enter is False
        assert "entry_window" in dec.reason
