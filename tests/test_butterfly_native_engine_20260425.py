"""tests/test_butterfly_native_engine_20260425.py — ButterflyNativeEngine 25 件以上

テスト対象: atlas_v3/bots/engines/butterfly_native.py
    - SymmetricButterflyConfig: バリデーション
    - ButterflyNativePosition: current_value / pnl / lower_strike / upper_strike
    - ButterflyNativeEngine.preflight: env=None / Kill Switch ARMED
    - ButterflyNativeEngine.reset_daily: 状態クリア
    - ButterflyNativeEngine._build_option_code: CALL/PUT フォーマット
    - ButterflyNativeEngine._calc_wing_width: ATR 動的算出 / None fallback
    - ButterflyNativeEngine._calc_qty: サイジング / edge cases
    - ButterflyNativeEngine._choose_wing_type: SMA 上下で CALL/PUT / SMA=None -> None
    - ButterflyNativeEngine.execute_entry: Kill Switch / ウィンドウ外 / IVR 拒否 /
      dry_test エントリー / 価格取得失敗 / ネットデビット <= 0 / 発注失敗
    - ButterflyNativeEngine.check_exit: TP / SL / force_close / kill_switch /
      価格取得失敗+強制クローズ時刻 / 保有継続 / ポジションなし
    - ButterflyNativeEngine.is_active: エントリー前後

設計規律:
    - futu SDK 依存なし・ネットワーク接続なし
    - common_v3.risk.kill_switch は monkeypatch で制御
    - clock_fn で ET 時刻を固定注入
    - spy_bot.py への import ゼロ
"""
from __future__ import annotations

import datetime
import math
from datetime import time
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.bots.engines.butterfly_native import (
    ButterflyLeg,
    ButterflyNativeEngine,
    ButterflyNativePosition,
    NoOpMarketData,
    NoOpTradeEngine,
    SymmetricButterflyConfig,
    TACTIC_NAME,
    _IVR_MAX_FALLBACK,
    _MIN_WING_STRIKES,
    _MAX_WING_STRIKES,
    _PROFIT_TARGET_PCT,
    _STOP_LOSS_PCT,
)
from atlas_v3.core.env_observer import MarketEnvironment

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------

def _make_clock(h: int, m: int, s: int = 0) -> "() -> datetime.datetime":
    """固定 ET 時刻を返す clock_fn を生成する。"""
    def _fn() -> datetime.datetime:
        return datetime.datetime(2026, 4, 25, h, m, s, tzinfo=ET)
    return _fn


def _make_env(vix: float = 15.0, ivr: float = 20.0, symbol: str = "US.SPY") -> MarketEnvironment:
    return MarketEnvironment(vix=vix, ivr_by_symbol={symbol: ivr})


def _make_position(
    symbol: str = "US.SPY",
    wing_type: Literal["CALL", "PUT"] = "CALL",
    atm_strike: float = 560.0,
    wing_width: int = 2,
    qty: int = 1,
    net_debit: float = 0.50,
    lower_entry_price: float = 1.50,
    atm_entry_price: float = 2.50,
    upper_entry_price: float = 1.50,
    expiry: str = "2026-04-25",
    trade_id: str = "t001",
    paper: bool = True,
) -> ButterflyNativePosition:
    lower_code = f"US.SPY260425C{int((atm_strike - wing_width) * 1000):08d}"
    atm_code   = f"US.SPY260425C{int(atm_strike * 1000):08d}"
    upper_code = f"US.SPY260425C{int((atm_strike + wing_width) * 1000):08d}"
    return ButterflyNativePosition(
        symbol=symbol, wing_type=wing_type,
        atm_strike=atm_strike, wing_width=wing_width,
        lower_code=lower_code, atm_code=atm_code, upper_code=upper_code,
        qty=qty, net_debit=net_debit,
        lower_entry_price=lower_entry_price,
        atm_entry_price=atm_entry_price,
        upper_entry_price=upper_entry_price,
        entry_time="2026-04-25T11:00:00",
        expiry=expiry, trade_id=trade_id, paper=paper,
    )


def _make_engine(
    h: int = 11, m: int = 0,
    paper: bool = True,
    ivr_ret: float = 20.0,
    price_ret: float = 560.0,
    atr_ret: float = 5.0,
    sma_ret: Optional[float] = 555.0,
    lower_mid: float = 2.50,   # lower wing (near-ATM) is more expensive -> positive debit
    atm_mid: float = 1.50,     # ATM sell price
    upper_mid: float = 2.50,   # upper wing price
    place_result: Optional[str] = "OID_TEST",
    early_close: bool = False,
    cash: float = 15_000.0,
) -> ButterflyNativeEngine:
    """テスト用エンジンを生成する（全 mock をインライン定義）。

    デフォルト価格設定: lower_mid=2.50 / atm_mid=1.50 / upper_mid=2.50
    -> net_debit = 2.50 + 2.50 - 2*1.50 = 2.0 (正値 = debit エントリー成立)
    """

    class _Mkt:
        def get_current_price(self, symbol):    return price_ret
        def get_ivr(self, symbol):              return ivr_ret
        def get_ivr_percentile_low(self, symbol): return 30.0
        def get_option_mid(self, code):
            # 558 系 -> lower_mid, 562 系 -> upper_mid, else -> atm_mid
            if "558" in code:
                return lower_mid
            if "562" in code:
                return upper_mid
            return atm_mid
        def get_sma(self, symbol, period=20):   return sma_ret
        def get_atr(self, symbol, period=14):   return atr_ret
        def get_strike_interval(self, symbol):  return 1.0
        def is_early_close_today(self):         return early_close
        def get_account_cash(self):             return cash

    class _Eng:
        def __init__(self):
            self.calls: list[dict] = []
        def place_butterfly_leg(self, option_code, side, quantity, label, signal_id=""):
            self.calls.append({"code": option_code, "side": side, "qty": quantity, "label": label})
            return place_result
        def get_open_positions(self):
            return []

    cfg = SymmetricButterflyConfig(paper_mode=paper)
    eng = _Eng()
    return ButterflyNativeEngine(
        market_data=_Mkt(),
        trade_engine=eng,
        config=cfg,
        clock_fn=_make_clock(h, m),
    ), eng


# ---------------------------------------------------------------------------
# 1. SymmetricButterflyConfig バリデーション
# ---------------------------------------------------------------------------

class TestSymmetricButterflyConfig:

    def test_defaults_are_valid(self):
        cfg = SymmetricButterflyConfig()
        assert cfg.ivr_low_threshold == _IVR_MAX_FALLBACK
        assert cfg.profit_target_pct == _PROFIT_TARGET_PCT
        assert cfg.stop_loss_pct     == _STOP_LOSS_PCT

    def test_invalid_ivr_threshold_raises(self):
        with pytest.raises(ValueError, match="ivr_low_threshold"):
            SymmetricButterflyConfig(ivr_low_threshold=0.0)

    def test_invalid_capital_pct_raises(self):
        with pytest.raises(ValueError, match="capital_pct"):
            SymmetricButterflyConfig(capital_pct=0.0)

    def test_invalid_min_wing_strikes_raises(self):
        with pytest.raises(ValueError, match="min_wing_strikes"):
            SymmetricButterflyConfig(min_wing_strikes=0)

    def test_max_less_than_min_wing_raises(self):
        with pytest.raises(ValueError, match="max_wing_strikes"):
            SymmetricButterflyConfig(min_wing_strikes=5, max_wing_strikes=2)

    def test_paper_mode_default_true(self):
        cfg = SymmetricButterflyConfig()
        assert cfg.paper_mode is True


# ---------------------------------------------------------------------------
# 2. ButterflyNativePosition
# ---------------------------------------------------------------------------

class TestButterflyNativePosition:

    def test_lower_upper_strike_derived(self):
        pos = _make_position(atm_strike=560.0, wing_width=3)
        assert pos.lower_strike == pytest.approx(557.0)
        assert pos.upper_strike == pytest.approx(563.0)

    def test_current_value_symmetric(self):
        pos = _make_position(net_debit=0.50)
        val = pos.current_value(1.80, 0.70, 1.80)
        assert val == pytest.approx(1.80 + 1.80 - 2 * 0.70)

    def test_pnl_positive_when_value_exceeds_debit(self):
        pos = _make_position(net_debit=0.50, qty=1)
        val = pos.current_value(1.80, 0.70, 1.80)   # = 2.20
        pnl = pos.pnl(1.80, 0.70, 1.80)
        assert pnl == pytest.approx((2.20 - 0.50) * 1 * 100.0)

    def test_pnl_negative_when_value_below_debit(self):
        pos = _make_position(net_debit=0.50, qty=1)
        pnl = pos.pnl(0.20, 1.50, 0.20)  # current_value = 0.20+0.20-3.0 < 0
        assert pnl < 0.0

    def test_repr_contains_symbol(self):
        pos = _make_position()
        assert "US.SPY" in repr(pos)

    def test_wing_width_zero_gives_same_lower_upper(self):
        pos = _make_position(atm_strike=500.0, wing_width=0)
        assert pos.lower_strike == 500.0
        assert pos.upper_strike == 500.0


# ---------------------------------------------------------------------------
# 3. preflight
# ---------------------------------------------------------------------------

class TestPreflight:

    def test_preflight_returns_false_when_env_none(self):
        engine, _ = _make_engine()
        assert engine.preflight(None) is False  # type: ignore[arg-type]

    def test_preflight_returns_false_when_kill_switch(self, monkeypatch):
        engine, _ = _make_engine()
        monkeypatch.setattr(
            "atlas_v3.bots.engines.butterfly_native.kill_switch_is_active",
            lambda: True,
        )
        env = _make_env()
        assert engine.preflight(env) is False

    def test_preflight_returns_true_when_clear(self, monkeypatch):
        engine, _ = _make_engine()
        monkeypatch.setattr(
            "atlas_v3.bots.engines.butterfly_native.kill_switch_is_active",
            lambda: False,
        )
        env = _make_env()
        assert engine.preflight(env) is True


# ---------------------------------------------------------------------------
# 4. tactic_type / tactic_name
# ---------------------------------------------------------------------------

class TestTacticProperties:

    def test_tactic_type(self):
        engine, _ = _make_engine()
        assert engine.tactic_type == "enter_exit"

    def test_tactic_name(self):
        engine, _ = _make_engine()
        assert engine.tactic_name == TACTIC_NAME


# ---------------------------------------------------------------------------
# 5. reset_daily
# ---------------------------------------------------------------------------

class TestResetDaily:

    def test_reset_clears_position_and_flags(self):
        engine, _ = _make_engine()
        engine.position   = _make_position()
        engine.entry_done = True
        engine.trade_done = True

        engine.reset_daily()

        assert engine.position   is None
        assert engine.entry_done is False
        assert engine.trade_done is False


# ---------------------------------------------------------------------------
# 6. _build_option_code
# ---------------------------------------------------------------------------

class TestBuildOptionCode:

    def test_call_code_format(self):
        engine, _ = _make_engine()
        code = engine._build_option_code("US.SPY", "2026-04-25", 560.0, "CALL")
        # strike=560.0 -> int(560.0 * 1000) = 560000 -> %08d = "00560000"
        assert code == "US.SPY260425C00560000"

    def test_put_code_format(self):
        engine, _ = _make_engine()
        code = engine._build_option_code("US.SPY", "2026-04-25", 560.0, "PUT")
        assert code == "US.SPY260425P00560000"

    def test_fractional_strike(self):
        engine, _ = _make_engine()
        code = engine._build_option_code("US.QQQ", "2026-04-25", 445.5, "CALL")
        assert "445500" in code

    def test_us_prefix_stripped(self):
        engine, _ = _make_engine()
        code = engine._build_option_code("US.META", "2026-04-25", 500.0, "CALL")
        assert code.startswith("US.META")


# ---------------------------------------------------------------------------
# 7. _calc_wing_width
# ---------------------------------------------------------------------------

class TestCalcWingWidth:

    def test_atr_none_returns_min(self):
        engine, _ = _make_engine(atr_ret=None)
        w = engine._calc_wing_width("US.SPY")
        assert w == engine._cfg.min_wing_strikes

    def test_atr_zero_returns_min(self):
        engine, _ = _make_engine(atr_ret=0.0)
        w = engine._calc_wing_width("US.SPY")
        assert w == engine._cfg.min_wing_strikes

    def test_atr_large_caps_at_max(self):
        engine, _ = _make_engine(atr_ret=100.0)
        w = engine._calc_wing_width("US.SPY")
        assert w <= engine._cfg.max_wing_strikes

    def test_normal_atr_gives_reasonable_width(self):
        engine, _ = _make_engine(atr_ret=5.0)
        w = engine._calc_wing_width("US.SPY")
        # 5.0 × 0.40 = 2.0 -> width=2
        assert w == 2


# ---------------------------------------------------------------------------
# 8. _calc_qty
# ---------------------------------------------------------------------------

class TestCalcQty:

    def test_zero_debit_returns_one(self):
        engine, _ = _make_engine()
        assert engine._calc_qty(15_000, 0.0) == 1

    def test_zero_cash_returns_one(self):
        engine, _ = _make_engine()
        assert engine._calc_qty(0.0, 0.50) == 1

    def test_paper_qty_caps_at_paper_max(self):
        engine, _ = _make_engine(paper=True)
        qty = engine._calc_qty(10_000_000, 0.01)
        assert qty <= engine._cfg.max_qty_paper

    def test_live_qty_caps_at_live_max(self):
        engine, _ = _make_engine(paper=False)
        qty = engine._calc_qty(10_000_000, 0.01)
        assert qty <= engine._cfg.max_qty_live

    def test_normal_qty_at_least_one(self):
        engine, _ = _make_engine()
        qty = engine._calc_qty(15_000, 0.50)
        assert qty >= 1


# ---------------------------------------------------------------------------
# 9. _choose_wing_type
# ---------------------------------------------------------------------------

class TestChooseWingType:

    def test_price_above_sma_returns_call(self):
        engine, _ = _make_engine(price_ret=560.0, sma_ret=555.0)
        result = engine._choose_wing_type("US.SPY", 560.0)
        assert result == "CALL"

    def test_price_below_sma_returns_put(self):
        engine, _ = _make_engine(price_ret=550.0, sma_ret=555.0)
        result = engine._choose_wing_type("US.SPY", 550.0)
        assert result == "PUT"

    def test_price_equals_sma_returns_call(self):
        engine, _ = _make_engine(price_ret=555.0, sma_ret=555.0)
        result = engine._choose_wing_type("US.SPY", 555.0)
        assert result == "CALL"

    def test_sma_none_returns_none(self):
        engine, _ = _make_engine(sma_ret=None)
        result = engine._choose_wing_type("US.SPY", 560.0)
        assert result is None


# ---------------------------------------------------------------------------
# 10. execute_entry — Kill Switch
# ---------------------------------------------------------------------------

class TestExecuteEntryKillSwitch:

    def test_kill_switch_armed_blocks_entry(self, monkeypatch):
        engine, _ = _make_engine()
        monkeypatch.setattr(
            "atlas_v3.bots.engines.butterfly_native.kill_switch_is_active",
            lambda: True,
        )
        assert engine.execute_entry("US.SPY") is False

    def test_entry_done_skips(self):
        engine, _ = _make_engine()
        engine.entry_done = True
        assert engine.execute_entry("US.SPY") is False

    def test_trade_done_skips(self):
        engine, _ = _make_engine()
        engine.trade_done = True
        assert engine.execute_entry("US.SPY") is False


# ---------------------------------------------------------------------------
# 11. execute_entry — ウィンドウ / カットオフ
# ---------------------------------------------------------------------------

class TestExecuteEntryWindow:

    def test_before_entry_window_blocks(self):
        engine, _ = _make_engine(h=9, m=0)
        assert engine.execute_entry("US.SPY") is False

    def test_after_entry_window_blocks(self):
        engine, _ = _make_engine(h=14, m=30)
        assert engine.execute_entry("US.SPY") is False

    def test_past_cutoff_blocks(self):
        engine, _ = _make_engine(h=15, m=35)
        assert engine.execute_entry("US.SPY") is False

    def test_inside_window_proceeds_to_ivr_check(self):
        # IVR = 25 < threshold 30 -> エントリー成功
        engine, _ = _make_engine(h=11, m=0, ivr_ret=25.0)
        result = engine.execute_entry("US.SPY")
        assert result is True


# ---------------------------------------------------------------------------
# 12. execute_entry — IVR フィルタ
# ---------------------------------------------------------------------------

class TestExecuteEntryIVR:

    def test_ivr_at_threshold_blocks(self):
        engine, _ = _make_engine(h=11, m=0, ivr_ret=30.0)
        assert engine.execute_entry("US.SPY") is False

    def test_ivr_above_threshold_blocks(self):
        engine, _ = _make_engine(h=11, m=0, ivr_ret=50.0)
        assert engine.execute_entry("US.SPY") is False

    def test_ivr_below_threshold_allows(self):
        engine, _ = _make_engine(h=11, m=0, ivr_ret=15.0)
        assert engine.execute_entry("US.SPY") is True


# ---------------------------------------------------------------------------
# 13. execute_entry — dry_test
# ---------------------------------------------------------------------------

class TestExecuteEntryDryTest:

    def test_dry_test_sets_position(self):
        engine, _ = _make_engine()
        result = engine.execute_entry("US.SPY", dry_test=True)
        assert result is True
        assert engine.position is not None
        assert engine.entry_done is True

    def test_dry_test_net_debit_positive(self):
        engine, _ = _make_engine()
        engine.execute_entry("US.SPY", dry_test=True)
        assert engine.position.net_debit > 0

    def test_dry_test_qty_at_least_one(self):
        engine, _ = _make_engine()
        engine.execute_entry("US.SPY", dry_test=True)
        assert engine.position.qty >= 1

    def test_dry_test_bypasses_window(self):
        # 22:00 ET = ウィンドウ外でも dry_test は通る
        engine, _ = _make_engine(h=22, m=0)
        result = engine.execute_entry("US.SPY", dry_test=True)
        assert result is True


# ---------------------------------------------------------------------------
# 14. execute_entry — 発注失敗
# ---------------------------------------------------------------------------

class TestExecuteEntryOrderFail:

    def test_order_failure_returns_false(self):
        engine, _ = _make_engine(h=11, m=0, ivr_ret=20.0, place_result=None)
        result = engine.execute_entry("US.SPY")
        assert result is False
        assert engine.position is None
        assert engine.entry_done is False

    def test_net_debit_zero_blocks(self):
        # lower_mid + upper_mid - 2*atm_mid = 1.0 + 1.0 - 4.0 = -2.0 -> 拒否
        engine, _ = _make_engine(h=11, m=0, ivr_ret=20.0,
                                  lower_mid=1.0, atm_mid=2.0, upper_mid=1.0)
        result = engine.execute_entry("US.SPY")
        assert result is False


# ---------------------------------------------------------------------------
# 15. check_exit — ポジションなし
# ---------------------------------------------------------------------------

class TestCheckExitNoPosition:

    def test_no_position_returns_false(self):
        engine, _ = _make_engine()
        assert engine.check_exit() is False

    def test_trade_done_returns_false(self):
        engine, _ = _make_engine()
        engine.position   = _make_position()
        engine.trade_done = True
        assert engine.check_exit() is False


# ---------------------------------------------------------------------------
# 16. check_exit — Kill Switch
# ---------------------------------------------------------------------------

class TestCheckExitKillSwitch:

    def test_kill_switch_forces_exit(self, monkeypatch):
        engine, _ = _make_engine()
        engine.position = _make_position()
        monkeypatch.setattr(
            "atlas_v3.bots.engines.butterfly_native.kill_switch_is_active",
            lambda: True,
        )
        result = engine.check_exit()
        assert result is True
        assert engine.trade_done is True


# ---------------------------------------------------------------------------
# 17. check_exit — TP / SL / force_close / holding
# ---------------------------------------------------------------------------

class TestCheckExitTPSL:

    def test_tp_trigger(self):
        # net_debit=0.50, TP threshold = 0.50 × 1.50 = 0.75
        # current_value = 1.80 + 1.80 - 2*0.70 = 2.20 > 0.75 -> TP
        engine, _ = _make_engine(h=12, m=0, lower_mid=1.80, atm_mid=0.70, upper_mid=1.80)
        engine.position = _make_position(net_debit=0.50)
        result = engine.check_exit()
        assert result is True
        assert engine.trade_done is True

    def test_sl_trigger(self):
        # net_debit=0.50, SL threshold = 0.50 × (1 - 1.50) = -0.25
        # current_value = 0.05 + 0.05 - 2*2.0 = -3.90 <= -0.25 -> SL
        engine, _ = _make_engine(h=12, m=0, lower_mid=0.05, atm_mid=2.0, upper_mid=0.05)
        engine.position = _make_position(net_debit=0.50)
        result = engine.check_exit()
        assert result is True
        assert engine.trade_done is True

    def test_force_close_at_cutoff(self):
        # 15:50 ET = force_close_time
        engine, _ = _make_engine(h=15, m=50, lower_mid=1.50, atm_mid=2.50, upper_mid=1.50)
        engine.position = _make_position(net_debit=0.50)
        result = engine.check_exit()
        assert result is True
        assert engine.trade_done is True

    def test_holding_no_tp_sl(self):
        # val = 1.50 + 1.50 - 2*2.50 = -2.0
        # TP threshold = 0.50 × 1.50 = 0.75 -> -2.0 < 0.75: no TP
        # SL threshold = 0.50 × (1-1.50) = -0.25 -> -2.0 <= -0.25: SL!
        # Use tighter debit that prevents SL: debit=5.0 -> SL at 5*(1-1.5)=-2.5 > -2.0: hold
        engine, _ = _make_engine(h=12, m=0, lower_mid=1.50, atm_mid=2.50, upper_mid=1.50)
        engine.position = _make_position(net_debit=5.0)
        result = engine.check_exit()
        assert result is False

    def test_early_close_uses_early_time(self):
        # early_close=True, time=12:55 -> past early_close_time(12:50) -> force close
        engine, _ = _make_engine(h=12, m=55, early_close=True,
                                  lower_mid=1.50, atm_mid=2.50, upper_mid=1.50)
        engine.position = _make_position(net_debit=0.50)
        result = engine.check_exit()
        assert result is True
        assert engine.trade_done is True


# ---------------------------------------------------------------------------
# 18. check_exit — 価格取得失敗 + 強制クローズ時刻
# ---------------------------------------------------------------------------

class TestCheckExitPriceUnavailable:

    def test_price_unavailable_before_force_close_holds(self):
        engine, _ = _make_engine(h=12, m=0, lower_mid=None, atm_mid=None, upper_mid=None)
        engine.position = _make_position()
        result = engine.check_exit()
        assert result is False

    def test_price_unavailable_at_force_close_exits(self):
        engine, _ = _make_engine(h=15, m=50, lower_mid=None, atm_mid=None, upper_mid=None)
        engine.position = _make_position()
        result = engine.check_exit()
        assert result is True
        assert engine.trade_done is True


# ---------------------------------------------------------------------------
# 19. is_active
# ---------------------------------------------------------------------------

class TestIsActive:

    def test_not_active_before_entry(self):
        engine, _ = _make_engine()
        assert engine.is_active() is False

    def test_active_after_dry_entry(self):
        engine, _ = _make_engine()
        engine.execute_entry("US.SPY", dry_test=True)
        assert engine.is_active() is True

    def test_not_active_after_exit(self):
        engine, _ = _make_engine(h=15, m=50, lower_mid=1.50, atm_mid=2.50, upper_mid=1.50)
        engine.execute_entry("US.SPY", dry_test=True)
        engine.check_exit()
        assert engine.is_active() is False


# ---------------------------------------------------------------------------
# 20. execute_entry -> check_exit 連携フロー
# ---------------------------------------------------------------------------

class TestEntryExitFlow:

    def test_full_flow_dry_test_tp(self):
        """dry_test エントリー後に TP 到達でクローズ。"""
        engine, _ = _make_engine(h=12, m=0, lower_mid=1.80, atm_mid=0.70, upper_mid=1.80)
        assert engine.execute_entry("US.SPY", dry_test=True) is True
        assert engine.position is not None
        # val = 1.80 + 1.80 - 2*0.70 = 2.20 > TP threshold
        result = engine.check_exit()
        assert result is True
        assert engine.is_active() is False

    def test_reset_daily_after_cycle_allows_re_entry(self):
        engine, _ = _make_engine()
        engine.execute_entry("US.SPY", dry_test=True)
        engine.reset_daily()
        # reset 後に再度 dry_test エントリー可能
        result = engine.execute_entry("US.SPY", dry_test=True)
        assert result is True

    def test_no_double_entry(self):
        engine, _ = _make_engine()
        r1 = engine.execute_entry("US.SPY", dry_test=True)
        r2 = engine.execute_entry("US.SPY", dry_test=True)
        assert r1 is True
        assert r2 is False  # entry_done=True でスキップ

    def test_exit_leg_order_atm_first(self):
        """EXIT は atm_buy_close -> lower_sell_close -> upper_sell_close の順。"""
        engine, trade_eng = _make_engine(
            h=15, m=50, lower_mid=1.50, atm_mid=2.50, upper_mid=1.50
        )
        engine.execute_entry("US.SPY", dry_test=True)
        engine.check_exit()
        labels = [c["label"] for c in trade_eng.calls]
        # exit calls は末尾 3 件
        exit_labels = labels[-3:]
        assert exit_labels[0] == "atm_buy_close"
        assert exit_labels[1] == "lower_sell_close"
        assert exit_labels[2] == "upper_sell_close"
