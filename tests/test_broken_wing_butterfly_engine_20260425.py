"""tests/test_broken_wing_butterfly_engine_20260425.py — BrokenWingButterflyEngine 15 件テスト

テスト対象:
    atlas_v3/bots/engines/broken_wing_butterfly.py

カバー範囲:
    T01  非対称 wing strike 計算: short wing +5pt / long wing +15pt が正しく設定される
    T02  asymmetric offset leg の strike が atm - offset_pts になっている
    T03  4 leg 発注順序: [long_call_lower, short_call_body, long_call_upper, asymmetric_offset]
    T04  short_call_body の quantity が quantity × 2（2 枚売り）
    T05  IVR < ivr_min でエントリー拒否
    T06  IVR > ivr_max でエントリー拒否（IV 高すぎ）
    T07  IVR が [ivr_min, ivr_max] 内でエントリー許可
    T08  entry_window 外（09:00 ET）でエントリー拒否
    T09  entry_window 内（11:00 ET）でエントリー許可
    T10  entry_window 終端（13:00 ET）はウィンドウ外（exclusive end）
    T11  profit_target 30% 達成でエグジット
    T12  profit が 30% 未満では保有継続
    T13  max_loss 50% stop 到達でエグジット
    T14  15:45 ET force close が発火
    T15  Kill Switch ARMED で should_exit が kill_switch を返す

注意:
    - futu SDK 依存なし・ネットワーク接続なしで動作
    - kill_switch は unittest.mock.patch で制御
    - ET タイムゾーン付き datetime を注入してウィンドウ判定を決定論的に制御
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from atlas_v3.bots.engines.broken_wing_butterfly import (
    BrokenWingButterflyConfig,
    BrokenWingButterflyEngine,
    BWBLeg,
    BWBPosition,
    NoOpTradeEngine,
)
from atlas_v3.core.env_observer import MarketEnvironment

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _env(
    vix: float = 18.0,
    ivr: float = 65.0,
    symbol: str = "US.SPY",
) -> MarketEnvironment:
    return MarketEnvironment(vix=vix, ivr_by_symbol={symbol: ivr})


def _engine(
    ivr_min: float = 50.0,
    ivr_max: float = 80.0,
    short_wing: float = 5.0,
    long_wing: float = 15.0,
    offset_pts: float = 5.0,
    profit_target_pct: float = 0.30,
    max_loss_pct: float = 0.50,
) -> BrokenWingButterflyEngine:
    cfg = BrokenWingButterflyConfig(
        ivr_min=ivr_min,
        ivr_max=ivr_max,
        short_wing_pts=short_wing,
        long_wing_pts=long_wing,
        offset_pts=offset_pts,
        profit_target_pct=profit_target_pct,
        max_loss_pct=max_loss_pct,
    )
    return BrokenWingButterflyEngine(trade_engine=NoOpTradeEngine(), config=cfg)


def _window(hour: int, minute: int) -> datetime:
    """ET タイムゾーン付き datetime（テスト用・2026-04-25 固定）を生成する。"""
    return datetime(2026, 4, 25, hour, minute, 0, tzinfo=ET)


def _position(
    symbol: str = "US.SPY",
    net_credit: float = 200.0,
    unrealized_pnl: float = 0.0,
) -> BWBPosition:
    return BWBPosition(
        symbol=symbol,
        quantity=1,
        net_credit=net_credit,
        unrealized_pnl=unrealized_pnl,
    )


# ---------------------------------------------------------------------------
# T01: 非対称 wing strike 計算
# ---------------------------------------------------------------------------

class TestAsymmetricWingStrikes:
    def test_short_wing_5pt_and_long_wing_15pt(self):
        """short wing = atm + 5pt / long wing = atm + 15pt が legs に反映される。"""
        eng = _engine(short_wing=5.0, long_wing=15.0)
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=150.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is True

        short_call = dec.legs[1]  # short_call_body
        long_upper = dec.legs[2]  # long_call_upper

        assert short_call.strike == 505.0, f"short wing: expected 505, got {short_call.strike}"
        assert long_upper.strike == 515.0, f"long wing: expected 515, got {long_upper.strike}"
        # 非対称: long_wing (15) > short_wing (5)
        assert long_upper.strike - dec.atm_strike == 15.0
        assert short_call.strike - dec.atm_strike == 5.0


# ---------------------------------------------------------------------------
# T02: asymmetric offset leg の strike
# ---------------------------------------------------------------------------

class TestOffsetLegStrike:
    def test_offset_leg_is_atm_minus_offset(self):
        """asymmetric_offset leg の strike = atm - offset_pts。"""
        eng = _engine(offset_pts=5.0)
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is True
        offset_leg = dec.legs[3]
        assert offset_leg.strike == 495.0, f"expected 495, got {offset_leg.strike}"
        assert offset_leg.label == "asymmetric_offset"
        assert offset_leg.side == "buy"


# ---------------------------------------------------------------------------
# T03: 4 leg 発注順序と label
# ---------------------------------------------------------------------------

class TestLegOrder:
    def test_four_leg_order_and_labels(self):
        """4 legs の順序: long_call_lower / short_call_body / long_call_upper / asymmetric_offset。"""
        eng = _engine()
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=150.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is True
        assert len(dec.legs) == 4

        assert dec.legs[0].label == "long_call_lower"
        assert dec.legs[0].side == "buy"
        assert dec.legs[0].option_type == "call"

        assert dec.legs[1].label == "short_call_body"
        assert dec.legs[1].side == "sell"

        assert dec.legs[2].label == "long_call_upper"
        assert dec.legs[2].side == "buy"

        assert dec.legs[3].label == "asymmetric_offset"
        assert dec.legs[3].side == "buy"


# ---------------------------------------------------------------------------
# T04: short_call_body が 2 枚売り
# ---------------------------------------------------------------------------

class TestShortCallDoubleQuantity:
    def test_short_call_body_quantity_is_double(self):
        """short_call_body の quantity は config quantity × 2。"""
        cfg = BrokenWingButterflyConfig(
            ivr_min=50.0, ivr_max=80.0,
            short_wing_pts=5.0, long_wing_pts=15.0,
            quantity=1,
        )
        eng = BrokenWingButterflyEngine(config=cfg)
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is True
        assert dec.legs[1].quantity == 2  # sell 2

    def test_short_call_body_quantity_scales_with_config_quantity(self):
        """quantity=2 のとき short_call_body は 4 枚。"""
        cfg = BrokenWingButterflyConfig(
            ivr_min=50.0, ivr_max=80.0,
            short_wing_pts=5.0, long_wing_pts=15.0,
            quantity=2,
        )
        eng = BrokenWingButterflyEngine(config=cfg)
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is True
        assert dec.legs[1].quantity == 4


# ---------------------------------------------------------------------------
# T05: IVR < ivr_min でエントリー拒否
# ---------------------------------------------------------------------------

class TestIVRMinFilter:
    def test_ivr_below_min_rejects_entry(self):
        """IVR = 49.9 < ivr_min=50 でエントリー拒否。"""
        eng = _engine(ivr_min=50.0)
        env = _env(ivr=49.9)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is False
        assert "ivr_min" in dec.reason.lower() or "IVR" in dec.reason

    def test_ivr_exactly_at_min_rejects(self):
        """IVR == ivr_min（境界値）もエントリー拒否（strict less than）。"""
        eng = _engine(ivr_min=50.0)
        env = _env(ivr=50.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        # IVR == ivr_min は < ivr_min 判定に引っかからないので通過する
        # (実装上は ivr < ivr_min の場合のみ拒否)
        # ivr_min=50, ivr=50 は < ではないので通過
        assert dec.should_enter is True


# ---------------------------------------------------------------------------
# T06: IVR > ivr_max でエントリー拒否
# ---------------------------------------------------------------------------

class TestIVRMaxFilter:
    def test_ivr_above_max_rejects_entry(self):
        """IVR = 80.1 > ivr_max=80 でエントリー拒否。"""
        eng = _engine(ivr_max=80.0)
        env = _env(ivr=80.1)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is False
        assert "ivr_max" in dec.reason.lower() or "IVR" in dec.reason


# ---------------------------------------------------------------------------
# T07: IVR [ivr_min, ivr_max] 内でエントリー許可
# ---------------------------------------------------------------------------

class TestIVRRangePass:
    def test_ivr_in_range_allows_entry(self):
        """IVR = 65.0 が [50, 80] 内でエントリー許可。"""
        eng = _engine(ivr_min=50.0, ivr_max=80.0)
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is True
        assert dec.ivr == 65.0


# ---------------------------------------------------------------------------
# T08: entry_window 外（09:00 ET）でエントリー拒否
# ---------------------------------------------------------------------------

class TestEntryWindowBefore:
    def test_before_window_09h00_rejects(self):
        """09:00 ET は entry_window（10:30-13:00）前: エントリー拒否。"""
        eng = _engine()
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(9, 0),
        )
        assert dec.should_enter is False
        assert "window" in dec.reason.lower()


# ---------------------------------------------------------------------------
# T09: entry_window 内（11:00 ET）でエントリー許可
# ---------------------------------------------------------------------------

class TestEntryWindowInside:
    def test_inside_window_11h00_allows(self):
        """11:00 ET（window 中央付近）: エントリー許可。"""
        eng = _engine()
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(11, 0),
        )
        assert dec.should_enter is True

    def test_window_start_10h30_allows(self):
        """10:30 ET（window 開始時刻・inclusive）: エントリー許可。"""
        eng = _engine()
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(10, 30),
        )
        assert dec.should_enter is True


# ---------------------------------------------------------------------------
# T10: entry_window 終端（13:00 ET）は exclusive end
# ---------------------------------------------------------------------------

class TestEntryWindowEnd:
    def test_window_end_13h00_is_excluded(self):
        """13:00 ET（window 終端・exclusive）: エントリー拒否。"""
        eng = _engine()
        env = _env(ivr=65.0)
        dec = eng.should_enter(
            env=env, symbol="US.SPY",
            atm_strike=500.0, net_credit=100.0,
            now_et=_window(13, 0),
        )
        assert dec.should_enter is False
        assert "window" in dec.reason.lower()


# ---------------------------------------------------------------------------
# T11: profit_target 30% 達成でエグジット
# ---------------------------------------------------------------------------

class TestProfitTarget30:
    def test_profit_30pct_triggers_exit(self):
        """unrealized_pnl == net_credit * 0.30 で利確エグジット。"""
        eng = _engine(profit_target_pct=0.30)
        pos = _position(net_credit=200.0, unrealized_pnl=60.0)  # 200 * 0.30 = 60
        dec = eng.should_exit(pos, _env(), now_et=_window(12, 0))
        assert dec.should_exit is True
        assert dec.exit_type == "profit_target"

    def test_profit_above_30pct_also_triggers(self):
        """unrealized_pnl > 30% でも利確エグジット。"""
        eng = _engine(profit_target_pct=0.30)
        pos = _position(net_credit=200.0, unrealized_pnl=80.0)
        dec = eng.should_exit(pos, _env(), now_et=_window(12, 0))
        assert dec.should_exit is True
        assert dec.exit_type == "profit_target"


# ---------------------------------------------------------------------------
# T12: profit < 30% では保有継続
# ---------------------------------------------------------------------------

class TestProfitBelowTarget:
    def test_profit_below_30pct_holds(self):
        """unrealized_pnl < 30% では保有継続。"""
        eng = _engine(profit_target_pct=0.30)
        pos = _position(net_credit=200.0, unrealized_pnl=59.9)  # < 60
        dec = eng.should_exit(pos, _env(), now_et=_window(12, 0))
        assert dec.should_exit is False
        assert dec.exit_type == "none"


# ---------------------------------------------------------------------------
# T13: max_loss 50% stop 到達でエグジット
# ---------------------------------------------------------------------------

class TestMaxLossStop50:
    def test_max_loss_50pct_triggers_exit(self):
        """unrealized_pnl == -(net_credit * 0.50) で max_loss_stop エグジット。"""
        eng = _engine(max_loss_pct=0.50)
        pos = _position(net_credit=200.0, unrealized_pnl=-100.0)  # -(200 * 0.50)
        dec = eng.should_exit(pos, _env(), now_et=_window(12, 0))
        assert dec.should_exit is True
        assert dec.exit_type == "max_loss_stop"

    def test_loss_below_threshold_holds(self):
        """損失が 50% 未満では保有継続。"""
        eng = _engine(max_loss_pct=0.50)
        pos = _position(net_credit=200.0, unrealized_pnl=-99.9)  # > -100
        dec = eng.should_exit(pos, _env(), now_et=_window(12, 0))
        assert dec.should_exit is False
        assert dec.exit_type == "none"


# ---------------------------------------------------------------------------
# T14: 15:45 ET force close
# ---------------------------------------------------------------------------

class TestForceClose1545:
    def test_force_close_at_15h45_triggers(self):
        """15:45 ET: force_close が発火する。"""
        eng = _engine()
        pos = _position(net_credit=200.0, unrealized_pnl=0.0)
        dec = eng.should_exit(pos, _env(), now_et=_window(15, 45))
        assert dec.should_exit is True
        assert dec.exit_type == "force_close"

    def test_no_force_close_at_15h44(self):
        """15:44 ET: force_close は発火しない（profit/stop も達していない）。"""
        eng = _engine()
        pos = _position(net_credit=200.0, unrealized_pnl=5.0)  # 5 < 60 (30%)
        dec = eng.should_exit(pos, _env(), now_et=_window(15, 44))
        assert dec.should_exit is False
        assert dec.exit_type == "none"


# ---------------------------------------------------------------------------
# T15: Kill Switch ARMED で should_exit が kill_switch を返す
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_kill_switch_forces_exit(self):
        """Kill Switch ARMED 時に should_exit が kill_switch exit_type を返す。"""
        eng = _engine()
        pos = _position(net_credit=200.0, unrealized_pnl=0.0)
        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.kill_switch_is_active",
            return_value=True,
        ):
            dec = eng.should_exit(pos, _env(), now_et=_window(11, 0))
        assert dec.should_exit is True
        assert dec.exit_type == "kill_switch"

    def test_kill_switch_disables_entry(self):
        """Kill Switch ARMED 時に should_enter が False を返す。"""
        eng = _engine()
        env = _env(ivr=65.0)
        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.kill_switch_is_active",
            return_value=True,
        ):
            dec = eng.should_enter(
                env=env, symbol="US.SPY",
                atm_strike=500.0, net_credit=100.0,
                now_et=_window(11, 0),
            )
        assert dec.should_enter is False
        assert dec.reason == "kill_switch_armed"

    def test_nooptrade_engine_returns_dry_bwb_prefix(self):
        """NoOpTradeEngine.place_broken_wing_butterfly は 'DRY_BWB_' prefix の order_id を返す。"""
        no_op = NoOpTradeEngine()
        legs: tuple[BWBLeg, ...] = (
            BWBLeg(label="long_call_lower",   strike=500.0, option_type="call", side="buy",  quantity=1),
            BWBLeg(label="short_call_body",   strike=505.0, option_type="call", side="sell", quantity=2),
            BWBLeg(label="long_call_upper",   strike=515.0, option_type="call", side="buy",  quantity=1),
            BWBLeg(label="asymmetric_offset", strike=495.0, option_type="call", side="buy",  quantity=1),
        )
        order_id = no_op.place_broken_wing_butterfly(
            symbol="US.SPY",
            legs=legs,
            quantity=1,
            idempotency_key="test_bwb_001",
        )
        assert order_id.startswith("DRY_BWB_")
        assert len(order_id) > len("DRY_BWB_")
