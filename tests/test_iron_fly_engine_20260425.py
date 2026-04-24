"""tests/test_iron_fly_engine_20260425.py — IronFlyEngine 15 件テスト

テスト対象:
    atlas_v3/bots/engines/iron_fly.py

カバー範囲:
    T01  4 leg 発注順序の正確性（ATM short call / short put / OTM long call / long put）
    T02  IVR フィルタ: IVR <= ivr_min でエントリー拒否
    T03  VIX フィルタ: VIX >= vix_max でエントリー拒否
    T04  IVR + VIX 両フィルタ同時通過でエントリー許可
    T05  entry_window 外（08:00 ET）でエントリー拒否
    T06  entry_window 内（10:30 ET）でエントリー許可
    T07  entry_window 上限（11:30 ET）でエントリー許可
    T08  profit_target 25% 達成でエグジット
    T09  stop_loss 1.5x クレジット到達でエグジット
    T10  15:40 ET force close が発火
    T11  15:39 ET は force close 未発火（境界値）
    T12  Kill Switch ARMED で should_exit が kill_switch を返す
    T13  Kill Switch ARMED で preflight が False を返す
    T14  max_credit=0 のとき should_exit が holding を返す
    T15  NoOpTradeEngine.place_iron_fly が "DRY_ORDER_" prefix の order_id を返す

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

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from atlas_v3.bots.engines.iron_fly import (
    IronFlyConfig,
    IronFlyEngine,
    IronFlyLeg,
    IronFlyPosition,
    NoOpTradeEngine,
)
from atlas_v3.core.env_observer import MarketEnvironment

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _env(vix: float = 18.0, ivr: float = 75.0, symbol: str = "US.SPY") -> MarketEnvironment:
    """テスト用 MarketEnvironment を生成する。"""
    return MarketEnvironment(vix=vix, ivr_by_symbol={symbol: ivr})


def _engine(ivr_min: float = 70.0, vix_max: float = 25.0, wing: float = 5.0) -> IronFlyEngine:
    """テスト用 IronFlyEngine（NoOpTradeEngine 使用）を生成する。"""
    cfg = IronFlyConfig(ivr_min=ivr_min, vix_max=vix_max, wing_width_pts=wing)
    return IronFlyEngine(trade_engine=NoOpTradeEngine(), config=cfg)


def _window_time(hour: int, minute: int) -> datetime:
    """ET タイムゾーン付き datetime（テスト用・2026-04-25 固定日）を生成する。"""
    return datetime(2026, 4, 25, hour, minute, 0, tzinfo=ET)


def _position(
    symbol: str = "US.SPY",
    max_credit: float = 200.0,
    unrealized_pnl: float = 0.0,
) -> IronFlyPosition:
    """テスト用 IronFlyPosition を生成する。"""
    return IronFlyPosition(
        symbol=symbol,
        quantity=1,
        atm_strike=500.0,
        max_credit=max_credit,
        unrealized_pnl=unrealized_pnl,
    )


# ---------------------------------------------------------------------------
# T01: 4 leg 発注順序の正確性
# ---------------------------------------------------------------------------

class TestIronFlyLegOrder:
    def test_leg_order_and_types(self):
        """legs[0]=ATM short call / [1]=ATM short put / [2]=OTM long call / [3]=OTM long put."""
        eng = _engine()
        env = _env(vix=18.0, ivr=80.0)
        decision = eng.should_enter(
            env=env,
            symbol="US.SPY",
            atm_strike=500.0,
            max_credit=300.0,
            now_et=_window_time(10, 45),
        )
        assert decision.should_enter is True
        legs = decision.legs
        assert len(legs) == 4

        # leg[0]: ATM short call
        assert legs[0].strike == 500.0
        assert legs[0].option_type == "call"
        assert legs[0].side == "sell"

        # leg[1]: ATM short put
        assert legs[1].strike == 500.0
        assert legs[1].option_type == "put"
        assert legs[1].side == "sell"

        # leg[2]: OTM long call
        assert legs[2].strike == 505.0   # 500 + 5
        assert legs[2].option_type == "call"
        assert legs[2].side == "buy"

        # leg[3]: OTM long put
        assert legs[3].strike == 495.0   # 500 - 5
        assert legs[3].option_type == "put"
        assert legs[3].side == "buy"


# ---------------------------------------------------------------------------
# T02: IVR フィルタ
# ---------------------------------------------------------------------------

class TestIVRFilter:
    def test_ivr_below_min_rejects_entry(self):
        """IVR == ivr_min（境界値）はエントリー拒否。"""
        eng = _engine(ivr_min=70.0)
        env = _env(vix=18.0, ivr=70.0, symbol="US.SPY")
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(10, 45))
        assert d.should_enter is False
        assert "ivr_min" in d.reason.lower() or "IVR" in d.reason

    def test_ivr_strictly_above_min_allows_entry(self):
        """IVR > ivr_min はエントリー許可。"""
        eng = _engine(ivr_min=70.0)
        env = _env(vix=18.0, ivr=71.0, symbol="US.SPY")
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(10, 45))
        assert d.should_enter is True


# ---------------------------------------------------------------------------
# T03: VIX フィルタ
# ---------------------------------------------------------------------------

class TestVIXFilter:
    def test_vix_at_max_rejects_entry(self):
        """VIX == vix_max（境界値）はエントリー拒否。"""
        eng = _engine(vix_max=25.0)
        env = _env(vix=25.0, ivr=80.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(10, 45))
        assert d.should_enter is False
        assert "vix_max" in d.reason.lower() or "VIX" in d.reason

    def test_vix_above_max_rejects_entry(self):
        """VIX > vix_max はエントリー拒否。"""
        eng = _engine(vix_max=25.0)
        env = _env(vix=26.0, ivr=80.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(10, 45))
        assert d.should_enter is False

    def test_vix_below_max_allows_entry(self):
        """VIX < vix_max はフィルタ通過。"""
        eng = _engine(vix_max=25.0)
        env = _env(vix=24.9, ivr=80.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(10, 45))
        assert d.should_enter is True


# ---------------------------------------------------------------------------
# T04: IVR + VIX 両フィルタ同時通過
# ---------------------------------------------------------------------------

class TestBothFilters:
    def test_both_pass_allows_entry(self):
        """IVR > 70 かつ VIX < 25 の場合にエントリー許可。"""
        eng = _engine(ivr_min=70.0, vix_max=25.0)
        env = _env(vix=18.0, ivr=75.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=250.0,
                             now_et=_window_time(11, 0))
        assert d.should_enter is True
        assert d.atm_strike == 500.0
        assert d.max_credit == 250.0


# ---------------------------------------------------------------------------
# T05-T07: entry_window 判定
# ---------------------------------------------------------------------------

class TestEntryWindow:
    def test_before_window_08h_rejects(self):
        """08:00 ET は entry_window 前: エントリー拒否。"""
        eng = _engine()
        env = _env(vix=18.0, ivr=80.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(8, 0))
        assert d.should_enter is False
        assert "window" in d.reason.lower()

    def test_window_start_10h30_allows(self):
        """10:30 ET（window 開始時刻）: エントリー許可。"""
        eng = _engine()
        env = _env(vix=18.0, ivr=80.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(10, 30))
        assert d.should_enter is True

    def test_window_end_11h30_allows(self):
        """11:30 ET（window 終端境界値）: エントリー許可（inclusive）。"""
        eng = _engine()
        env = _env(vix=18.0, ivr=80.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(11, 30))
        assert d.should_enter is True

    def test_after_window_12h00_rejects(self):
        """12:00 ET（window 終了後）: エントリー拒否。"""
        eng = _engine()
        env = _env(vix=18.0, ivr=80.0)
        d = eng.should_enter(env, "US.SPY", atm_strike=500.0, max_credit=200.0,
                             now_et=_window_time(12, 0))
        assert d.should_enter is False


# ---------------------------------------------------------------------------
# T08: profit_target 25%
# ---------------------------------------------------------------------------

class TestProfitTarget:
    def test_profit_target_25pct_triggers_exit(self):
        """unrealized_pnl == max_credit * 0.25 で利確エグジット。"""
        eng = _engine()
        pos = _position(max_credit=200.0, unrealized_pnl=50.0)   # 200 * 0.25 = 50
        d = eng.should_exit(pos, _env(), now_et=_window_time(13, 0))
        assert d.should_exit is True
        assert d.exit_type == "profit_target"

    def test_profit_below_target_holds(self):
        """unrealized_pnl < 25% では保有継続。"""
        eng = _engine()
        pos = _position(max_credit=200.0, unrealized_pnl=49.9)
        d = eng.should_exit(pos, _env(), now_et=_window_time(13, 0))
        assert d.should_exit is False
        assert d.exit_type == "none"


# ---------------------------------------------------------------------------
# T09: stop_loss 1.5x
# ---------------------------------------------------------------------------

class TestStopLoss:
    def test_stop_loss_1_5x_triggers_exit(self):
        """unrealized_pnl == -(max_credit * 1.5) で損切りエグジット。"""
        eng = _engine()
        pos = _position(max_credit=200.0, unrealized_pnl=-300.0)  # -(200 * 1.5) = -300
        d = eng.should_exit(pos, _env(), now_et=_window_time(13, 0))
        assert d.should_exit is True
        assert d.exit_type == "stop_loss"

    def test_loss_below_threshold_holds(self):
        """損失が 1.5x 未満では保有継続。"""
        eng = _engine()
        pos = _position(max_credit=200.0, unrealized_pnl=-299.9)
        d = eng.should_exit(pos, _env(), now_et=_window_time(13, 0))
        assert d.should_exit is False


# ---------------------------------------------------------------------------
# T10-T11: force_close 15:40 ET
# ---------------------------------------------------------------------------

class TestForceClose:
    def test_force_close_at_15h40_triggers(self):
        """15:40 ET: force_close が発火する。"""
        eng = _engine()
        pos = _position(max_credit=200.0, unrealized_pnl=0.0)
        d = eng.should_exit(pos, _env(), now_et=_window_time(15, 40))
        assert d.should_exit is True
        assert d.exit_type == "force_close"

    def test_no_force_close_at_15h39(self):
        """15:39 ET: force_close は発火しない（profit/stop も達していない）。"""
        eng = _engine()
        pos = _position(max_credit=200.0, unrealized_pnl=5.0)  # 5 < 50 (25%)
        d = eng.should_exit(pos, _env(), now_et=_window_time(15, 39))
        assert d.should_exit is False
        assert d.exit_type == "none"


# ---------------------------------------------------------------------------
# T12: Kill Switch ARMED — should_exit
# ---------------------------------------------------------------------------

class TestKillSwitchExit:
    def test_kill_switch_forces_exit(self):
        """Kill Switch ARMED 時に should_exit が kill_switch exit_type を返す。"""
        eng = _engine()
        pos = _position(max_credit=200.0, unrealized_pnl=0.0)
        with patch("atlas_v3.bots.engines.iron_fly.kill_switch_is_active", return_value=True):
            d = eng.should_exit(pos, _env(), now_et=_window_time(11, 0))
        assert d.should_exit is True
        assert d.exit_type == "kill_switch"


# ---------------------------------------------------------------------------
# T13: Kill Switch ARMED — preflight
# ---------------------------------------------------------------------------

class TestKillSwitchPreflight:
    def test_kill_switch_disables_preflight(self):
        """Kill Switch ARMED 時に preflight が False を返す。"""
        eng = _engine()
        with patch("atlas_v3.bots.engines.iron_fly.kill_switch_is_active", return_value=True):
            ok = eng.preflight(_env())
        assert ok is False


# ---------------------------------------------------------------------------
# T14: max_credit=0 のとき should_exit は holding
# ---------------------------------------------------------------------------

class TestMaxCreditZero:
    def test_zero_max_credit_returns_holding(self):
        """max_credit=0 のときエグジット判定不能・should_exit=False。"""
        eng = _engine()
        pos = _position(max_credit=0.0, unrealized_pnl=999.0)
        d = eng.should_exit(pos, _env(), now_et=_window_time(11, 0))
        assert d.should_exit is False
        assert "max_credit" in d.reason.lower()


# ---------------------------------------------------------------------------
# T15: NoOpTradeEngine.place_iron_fly が DRY_ORDER_ prefix を返す
# ---------------------------------------------------------------------------

class TestNoOpTradeEngine:
    def test_place_iron_fly_returns_dry_order_id(self):
        """NoOpTradeEngine.place_iron_fly は 'DRY_ORDER_' プレフィックスの order_id を返す。"""
        eng_no_op = NoOpTradeEngine()
        legs = (
            IronFlyLeg(strike=500.0, option_type="call", side="sell", quantity=1),
            IronFlyLeg(strike=500.0, option_type="put",  side="sell", quantity=1),
            IronFlyLeg(strike=505.0, option_type="call", side="buy",  quantity=1),
            IronFlyLeg(strike=495.0, option_type="put",  side="buy",  quantity=1),
        )
        order_id = eng_no_op.place_iron_fly(
            symbol="US.SPY",
            legs=legs,
            quantity=1,
            idempotency_key="test_key_001",
        )
        assert order_id.startswith("DRY_ORDER_")
        assert len(order_id) > len("DRY_ORDER_")

    def test_place_order_calls_trade_engine(self):
        """IronFlyEngine.place_order が self._eng.place_iron_fly を呼び出す。"""
        mock_eng = MagicMock()
        mock_eng.place_iron_fly.return_value = "ORDER_12345"
        cfg = IronFlyConfig(ivr_min=70.0, vix_max=25.0)
        iron_fly_engine = IronFlyEngine(trade_engine=mock_eng, config=cfg)

        env = _env(vix=18.0, ivr=80.0)
        decision = iron_fly_engine.should_enter(
            env, "US.SPY", atm_strike=500.0, max_credit=300.0,
            now_et=_window_time(10, 50),
        )
        assert decision.should_enter is True

        order_id = iron_fly_engine.place_order(decision)
        mock_eng.place_iron_fly.assert_called_once()
        call_kwargs = mock_eng.place_iron_fly.call_args
        # symbol / legs / quantity が渡されていることを確認
        assert call_kwargs[1]["symbol"] == "US.SPY" or call_kwargs[0][0] == "US.SPY"
        assert order_id == "ORDER_12345"
