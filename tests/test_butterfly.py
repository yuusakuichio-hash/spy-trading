"""tests/test_butterfly.py — ButterflyEngine 10テスト以上

テスト対象:
  - ButterflyPosition: current_value, pnl, lower/upper_strike
  - calc_butterfly_wing_width: ATR動的算出 / None fallback
  - calc_butterfly_qty: TMR検証付きサイジング / edge cases
  - ButterflyEngine.should_trade_today: IVR/symbol/paper条件
  - ButterflyEngine._build_option_code: コードフォーマット検証
  - ButterflyEngine._choose_wing_type: SMA上下でCALL/PUT選択
  - ButterflyEngine.reset_daily: 日次リセット
  - EXCLUDED_SYMBOLS: US.SPX / US..SPX 除外

注意: futu依存なし・ネットワーク接続なしで動作するよう設計。
"""
import sys
import os
import math
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

# spy_bot.pyから必要なシンボルをimport
from spy_bot import (
    ButterflyPosition,
    ButterflyEngine,
    calc_butterfly_wing_width,
    calc_butterfly_qty,
    BUTTERFLY_IVR_MAX_FALLBACK,
    BUTTERFLY_MIN_WING_STRIKES,
    BUTTERFLY_MAX_WING_STRIKES,
    BUTTERFLY_ATR_WING_MULT,
    BUTTERFLY_CAPITAL_PCT,
    BUTTERFLY_MAX_QTY,
    BUTTERFLY_MAX_QTY_PAPER,
    BUTTERFLY_PROFIT_TARGET_PCT,
    BUTTERFLY_STOP_LOSS_PCT,
    ENABLE_BUTTERFLY,
)


# ── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_position(
    symbol="US.SPY",
    wing_type="CALL",
    atm_strike=500.0,
    wing_width=3,
    qty=2,
    net_debit=0.50,
    lower_entry_price=1.50,
    atm_entry_price=2.00,
    upper_entry_price=1.50,
    expiry="2026-04-18",
    trade_id="test01",
    paper=True,
) -> ButterflyPosition:
    return ButterflyPosition(
        symbol=symbol,
        wing_type=wing_type,
        atm_strike=atm_strike,
        wing_width=wing_width,
        lower_code=f"US.SPY260418C00497000",
        atm_code=f"US.SPY260418C00500000",
        upper_code=f"US.SPY260418C00503000",
        qty=qty,
        net_debit=net_debit,
        lower_entry_price=lower_entry_price,
        atm_entry_price=atm_entry_price,
        upper_entry_price=upper_entry_price,
        entry_time=datetime.datetime.now().isoformat(),
        expiry=expiry,
        trade_id=trade_id,
        paper=paper,
    )


def _make_engine(paper=True, dry_test=True) -> ButterflyEngine:
    mkt = MagicMock()
    eng = MagicMock()
    return ButterflyEngine(mkt, eng, paper=paper, dry_test=dry_test)


# ══════════════════════════════════════════════════════════════════════════════
# Test 1: ButterflyPosition — lower/upper_strike の自動算出
# ══════════════════════════════════════════════════════════════════════════════
class TestButterflyPositionStrikes(unittest.TestCase):
    def test_lower_upper_strikes(self):
        pos = _make_position(atm_strike=500.0, wing_width=5)
        self.assertAlmostEqual(pos.lower_strike, 495.0)
        self.assertAlmostEqual(pos.upper_strike, 505.0)

    def test_lower_upper_strikes_fractional(self):
        pos = _make_position(atm_strike=562.5, wing_width=2)
        self.assertAlmostEqual(pos.lower_strike, 560.5)
        self.assertAlmostEqual(pos.upper_strike, 564.5)


# ══════════════════════════════════════════════════════════════════════════════
# Test 2: ButterflyPosition — current_value
# ══════════════════════════════════════════════════════════════════════════════
class TestButterflyPositionCurrentValue(unittest.TestCase):
    def test_current_value_formula(self):
        """Long Butterfly value = lower + upper - 2×atm"""
        pos = _make_position(net_debit=0.50)
        val = pos.current_value(lower_price=2.0, atm_price=0.5, upper_price=2.0)
        expected = 2.0 + 2.0 - 2.0 * 0.5  # = 3.0
        self.assertAlmostEqual(val, expected, places=6)

    def test_current_value_zero_when_both_wings_zero(self):
        pos = _make_position()
        val = pos.current_value(0.0, 1.0, 0.0)
        self.assertAlmostEqual(val, -2.0)  # lower=0 upper=0 atm=1 → -2

    def test_current_value_atm_equals_zero(self):
        pos = _make_position()
        val = pos.current_value(1.0, 0.0, 1.0)
        self.assertAlmostEqual(val, 2.0)


# ══════════════════════════════════════════════════════════════════════════════
# Test 3: ButterflyPosition — pnl
# ══════════════════════════════════════════════════════════════════════════════
class TestButterflyPositionPnL(unittest.TestCase):
    def test_pnl_profit(self):
        """値が上昇した場合 P&L > 0"""
        pos = _make_position(net_debit=0.50, qty=1)
        # current_value = 1.5+1.5 - 2*0.5 = 2.0 → pnl = (2.0-0.5)*1*100 = 150
        pnl = pos.pnl(lower_price=1.5, atm_price=0.5, upper_price=1.5)
        self.assertAlmostEqual(pnl, 150.0, places=4)

    def test_pnl_loss_at_max(self):
        """ウィングが0に近くATMが大きい → 最大損失"""
        pos = _make_position(net_debit=0.50, qty=2)
        # current_value = 0+0 - 2*2 = -4 → pnl = (-4-0.5)*2*100 = -900
        pnl = pos.pnl(lower_price=0.0, atm_price=2.0, upper_price=0.0)
        self.assertAlmostEqual(pnl, -900.0, places=4)

    def test_pnl_breakeven(self):
        """current_value == net_debit のとき pnl == 0"""
        pos = _make_position(net_debit=1.0, qty=3)
        # lower=1.5, atm=1.0, upper=0.5 → val=1.5+0.5-2*1.0=0... ちがう
        # lower=2.0, atm=1.5, upper=1.0 → val=2.0+1.0-2*1.5=0.0 ≠ 1.0
        # net_debit=1.0にするには: val=1.0 = lower+upper-2*atm
        # lower=2.0, atm=1.0, upper=1.0 → val=2.0+1.0-2.0=1.0 OK
        pnl = pos.pnl(lower_price=2.0, atm_price=1.0, upper_price=1.0)
        self.assertAlmostEqual(pnl, 0.0, places=4)


# ══════════════════════════════════════════════════════════════════════════════
# Test 4: calc_butterfly_wing_width — ATR動的算出
# ══════════════════════════════════════════════════════════════════════════════
class TestCalcButterflyWingWidth(unittest.TestCase):
    def test_none_atr_returns_min(self):
        width = calc_butterfly_wing_width("US.SPY", None)
        self.assertEqual(width, BUTTERFLY_MIN_WING_STRIKES)

    def test_zero_atr_returns_min(self):
        width = calc_butterfly_wing_width("US.SPY", 0.0)
        self.assertEqual(width, BUTTERFLY_MIN_WING_STRIKES)

    def test_spy_atr_5_gives_reasonable_width(self):
        """SPY ATR≈5, mult=0.40 → 5*0.40=2.0 → width=2"""
        width = calc_butterfly_wing_width("US.SPY", 5.0)
        # BUTTERFLY_ATR_WING_MULT=0.40 → 5*0.40=2.0 → round=2
        self.assertGreaterEqual(width, BUTTERFLY_MIN_WING_STRIKES)
        self.assertLessEqual(width, BUTTERFLY_MAX_WING_STRIKES)
        self.assertEqual(width, 2)

    def test_large_atr_capped_at_max(self):
        """ATRが極端に大きい場合は最大値にキャップ"""
        width = calc_butterfly_wing_width("US.SPY", 1000.0)
        self.assertLessEqual(width, BUTTERFLY_MAX_WING_STRIKES)

    def test_negative_atr_returns_min(self):
        width = calc_butterfly_wing_width("US.SPY", -5.0)
        self.assertEqual(width, BUTTERFLY_MIN_WING_STRIKES)


# ══════════════════════════════════════════════════════════════════════════════
# Test 5: calc_butterfly_qty — TMR検証付きサイジング
# ══════════════════════════════════════════════════════════════════════════════
class TestCalcButterflyQty(unittest.TestCase):
    def test_basic_calculation(self):
        """cash=15000, debit=0.50, pct=0.02 → 15000*0.02/(0.50*100)=6 → cap=3"""
        qty = calc_butterfly_qty(15000.0, 0.50, paper=False)
        # 15000*0.02 = 300 / (0.5*100) = 6 → cap at BUTTERFLY_MAX_QTY=3
        self.assertEqual(qty, BUTTERFLY_MAX_QTY)

    def test_paper_mode_higher_cap(self):
        """ペーパーモードは BUTTERFLY_MAX_QTY_PAPER まで許容"""
        qty = calc_butterfly_qty(500000.0, 0.50, paper=True)
        self.assertLessEqual(qty, BUTTERFLY_MAX_QTY_PAPER)
        self.assertGreaterEqual(qty, 1)

    def test_zero_cash_returns_one(self):
        qty = calc_butterfly_qty(0.0, 0.50, paper=False)
        self.assertEqual(qty, 1)

    def test_zero_debit_returns_one(self):
        qty = calc_butterfly_qty(15000.0, 0.0, paper=False)
        self.assertEqual(qty, 1)

    def test_negative_debit_returns_one(self):
        qty = calc_butterfly_qty(15000.0, -1.0, paper=False)
        self.assertEqual(qty, 1)

    def test_minimum_is_one(self):
        """資金不足でも最低1枚"""
        qty = calc_butterfly_qty(10.0, 100.0, paper=False)
        self.assertEqual(qty, 1)


# ══════════════════════════════════════════════════════════════════════════════
# Test 6: ButterflyEngine.should_trade_today — エントリー環境条件
# ══════════════════════════════════════════════════════════════════════════════
class TestShouldTradeToday(unittest.TestCase):
    def test_spx_excluded(self):
        result = ButterflyEngine.should_trade_today("US.SPX", ivr=15.0, ivr_low_threshold=30.0)
        self.assertFalse(result)

    def test_spx_double_dot_excluded(self):
        result = ButterflyEngine.should_trade_today("US..SPX", ivr=15.0, ivr_low_threshold=30.0)
        self.assertFalse(result)

    def test_spy_low_ivr_enters(self):
        result = ButterflyEngine.should_trade_today("US.SPY", ivr=20.0, ivr_low_threshold=30.0)
        self.assertTrue(result)

    def test_spy_high_ivr_skips(self):
        result = ButterflyEngine.should_trade_today("US.SPY", ivr=45.0, ivr_low_threshold=30.0)
        self.assertFalse(result)

    def test_ivr_equal_threshold_skips(self):
        """IVR == threshold は除外（IVR < threshold のみ許可）"""
        result = ButterflyEngine.should_trade_today("US.SPY", ivr=30.0, ivr_low_threshold=30.0)
        self.assertFalse(result)

    def test_none_ivr_skips_in_live(self):
        result = ButterflyEngine.should_trade_today("US.SPY", ivr=None, ivr_low_threshold=30.0)
        self.assertFalse(result)

    def test_paper_mode_bypasses_ivr(self):
        """ペーパーモードは高IVRでも通過"""
        result = ButterflyEngine.should_trade_today(
            "US.SPY", ivr=80.0, ivr_low_threshold=30.0, paper=True
        )
        self.assertTrue(result)

    def test_qqq_low_ivr_enters(self):
        result = ButterflyEngine.should_trade_today("US.QQQ", ivr=10.0, ivr_low_threshold=30.0)
        self.assertTrue(result)

    def test_meta_low_ivr_enters(self):
        result = ButterflyEngine.should_trade_today("US.META", ivr=5.0, ivr_low_threshold=30.0)
        self.assertTrue(result)


# ══════════════════════════════════════════════════════════════════════════════
# Test 7: ButterflyEngine._build_option_code — コードフォーマット
# ══════════════════════════════════════════════════════════════════════════════
class TestBuildOptionCode(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_call_code_format(self):
        code = self.engine._build_option_code("US.SPY", "2026-04-18", 500.0, "CALL")
        self.assertEqual(code, "US.SPY260418C00500000")

    def test_put_code_format(self):
        code = self.engine._build_option_code("US.SPY", "2026-04-18", 500.0, "PUT")
        self.assertEqual(code, "US.SPY260418P00500000")

    def test_fractional_strike(self):
        code = self.engine._build_option_code("US.SPY", "2026-04-18", 562.5, "CALL")
        self.assertEqual(code, "US.SPY260418C00562500")

    def test_qqq_code(self):
        code = self.engine._build_option_code("US.QQQ", "2026-04-18", 480.0, "PUT")
        self.assertEqual(code, "US.QQQ260418P00480000")

    def test_meta_code(self):
        code = self.engine._build_option_code("US.META", "2026-04-18", 600.0, "CALL")
        self.assertEqual(code, "US.META260418C00600000")


# ══════════════════════════════════════════════════════════════════════════════
# Test 8: ButterflyEngine._choose_wing_type — SMAによるCALL/PUT選択
# ══════════════════════════════════════════════════════════════════════════════
class TestChooseWingType(unittest.TestCase):
    def test_price_above_sma_returns_call(self):
        engine = _make_engine()
        engine.mkt.get_spy_current.return_value = 510.0

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"SPY": 500.0}, f)
            tmp_path = f.name

        import spy_bot
        original = spy_bot.SMA_CACHE_FILE
        spy_bot.SMA_CACHE_FILE = Path(tmp_path)
        try:
            wt = engine._choose_wing_type("US.SPY")
            self.assertEqual(wt, "CALL")
        finally:
            spy_bot.SMA_CACHE_FILE = original
            os.unlink(tmp_path)

    def test_price_below_sma_returns_put(self):
        engine = _make_engine()
        engine.mkt.get_spy_current.return_value = 490.0

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"SPY": 500.0}, f)
            tmp_path = f.name

        import spy_bot
        original = spy_bot.SMA_CACHE_FILE
        spy_bot.SMA_CACHE_FILE = Path(tmp_path)
        try:
            wt = engine._choose_wing_type("US.SPY")
            self.assertEqual(wt, "PUT")
        finally:
            spy_bot.SMA_CACHE_FILE = original
            os.unlink(tmp_path)

    def test_no_sma_cache_returns_call(self):
        """SMAキャッシュなし → デフォルトCALL"""
        engine = _make_engine()
        engine.mkt.get_spy_current.return_value = 500.0

        import spy_bot
        original = spy_bot.SMA_CACHE_FILE
        spy_bot.SMA_CACHE_FILE = Path("/nonexistent_file_12345.json")
        try:
            wt = engine._choose_wing_type("US.SPY")
            self.assertEqual(wt, "CALL")
        finally:
            spy_bot.SMA_CACHE_FILE = original

    def test_no_price_returns_call(self):
        """価格取得失敗 → デフォルトCALL"""
        engine = _make_engine()
        engine.mkt.get_spy_current.return_value = None
        wt = engine._choose_wing_type("US.SPY")
        self.assertEqual(wt, "CALL")


# ══════════════════════════════════════════════════════════════════════════════
# Test 9: ButterflyEngine.reset_daily
# ══════════════════════════════════════════════════════════════════════════════
class TestResetDaily(unittest.TestCase):
    def test_reset_clears_state(self):
        engine = _make_engine()
        engine.position   = _make_position()
        engine.entry_done = True
        engine.trade_done = True

        engine.reset_daily()

        self.assertIsNone(engine.position)
        self.assertFalse(engine.entry_done)
        self.assertFalse(engine.trade_done)

    def test_is_active_false_after_reset(self):
        engine = _make_engine()
        engine.position = _make_position()
        self.assertTrue(engine.is_active())
        engine.reset_daily()
        self.assertFalse(engine.is_active())


# ══════════════════════════════════════════════════════════════════════════════
# Test 10: ButterflyEngine.check_exit — TP/SL条件
# ══════════════════════════════════════════════════════════════════════════════
class TestCheckExitConditions(unittest.TestCase):
    def _engine_with_position(self, net_debit=1.0, qty=1):
        engine = _make_engine(dry_test=False)
        engine.position = _make_position(net_debit=net_debit, qty=qty)
        # 価格取得をモック
        return engine

    def test_tp_condition_triggers(self):
        """val >= debit * (1 + TP_PCT) でTPが発火すること"""
        # dry_test=True でシミュレート（価格はDRY_TEST内部で設定される）
        engine = _make_engine(dry_test=True)
        pos = _make_position(net_debit=0.50, qty=1,
                             lower_entry_price=1.50, atm_entry_price=2.50, upper_entry_price=1.50)
        engine.position   = pos
        # dry_test_startを十分前に設定してcheck_exitが価格評価フェーズに入るようにする
        import zoneinfo
        ET = zoneinfo.ZoneInfo("America/New_York")
        engine._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=20)

        # force_close時刻を未来に設定
        import spy_bot
        original_h = spy_bot.BUTTERFLY_FORCE_CLOSE_H
        original_m = spy_bot.BUTTERFLY_FORCE_CLOSE_M
        spy_bot.BUTTERFLY_FORCE_CLOSE_H = 23
        spy_bot.BUTTERFLY_FORCE_CLOSE_M = 59
        try:
            # dry_test価格: lower=1.5*1.8=2.7, atm=2.5*0.7=1.75, upper=1.5*1.8=2.7
            # current_val = 2.7+2.7-2*1.75=2.9 / tp_threshold=0.5*(1+0.5)=0.75 → TP発火
            result = engine.check_exit()
            self.assertTrue(result)
            self.assertIsNone(engine.position)
            self.assertTrue(engine.trade_done)
        finally:
            spy_bot.BUTTERFLY_FORCE_CLOSE_H = original_h
            spy_bot.BUTTERFLY_FORCE_CLOSE_M = original_m

    def test_sl_condition_triggers(self):
        """val <= debit * (1 - SL_PCT) でSLが発火すること"""
        # dry_test=False でモックを使って SL 条件をテスト
        # _execute_exit は pushover と _butterfly_append_pnl を呼ぶが
        # FUTU_AVAILABLE が True の場合は _place_single_leg が呼ばれるため
        # engのモックに (None, None) を返すよう設定する
        engine = _make_engine(dry_test=False)
        pos = _make_position(net_debit=1.0, qty=1)
        engine.position = pos
        # _place_single_leg が (None, None) を返す → error ログのみで継続
        engine.eng._place_single_leg.return_value = (None, "failed")

        # SL閾値: 1.0 * (1 - 1.50) = -0.50
        # current_value = 0+0-2*1.5 = -3.0 <= -0.5 → SL発火
        engine._get_option_mid = MagicMock(side_effect=[0.0, 1.5, 0.0])

        import spy_bot
        original_h = spy_bot.BUTTERFLY_FORCE_CLOSE_H
        original_m = spy_bot.BUTTERFLY_FORCE_CLOSE_M
        spy_bot.BUTTERFLY_FORCE_CLOSE_H = 23
        spy_bot.BUTTERFLY_FORCE_CLOSE_M = 59
        try:
            with patch("spy_bot.pushover", return_value=True), \
                 patch("spy_bot._butterfly_append_pnl"):
                result = engine.check_exit()
            self.assertTrue(result)
            self.assertIsNone(engine.position)
            self.assertTrue(engine.trade_done)
        finally:
            spy_bot.BUTTERFLY_FORCE_CLOSE_H = original_h
            spy_bot.BUTTERFLY_FORCE_CLOSE_M = original_m

    def test_no_position_returns_false(self):
        engine = _make_engine()
        engine.position = None
        self.assertFalse(engine.check_exit())

    def test_trade_done_returns_false(self):
        engine = _make_engine()
        engine.position   = _make_position()
        engine.trade_done = True
        self.assertFalse(engine.check_exit())


# ══════════════════════════════════════════════════════════════════════════════
# Test 11: ButterflyEngine.check_entry — EXCLUDED_SYMBOLS を除外
# ══════════════════════════════════════════════════════════════════════════════
class TestCheckEntryExclusion(unittest.TestCase):
    def test_spx_excluded_in_check_entry(self):
        engine = _make_engine(dry_test=True)
        result = engine.check_entry("US.SPX")
        self.assertFalse(result)

    def test_entry_done_skips(self):
        engine = _make_engine(dry_test=True)
        engine.entry_done = True
        result = engine.check_entry("US.SPY")
        self.assertFalse(result)


# ══════════════════════════════════════════════════════════════════════════════
# Test 12: ButterflyPosition.__repr__
# ══════════════════════════════════════════════════════════════════════════════
class TestButterflyPositionRepr(unittest.TestCase):
    def test_repr_contains_key_info(self):
        pos = _make_position(symbol="US.QQQ", wing_type="PUT", atm_strike=480.0, wing_width=2)
        r = repr(pos)
        self.assertIn("US.QQQ", r)
        self.assertIn("PUT", r)
        self.assertIn("480", r)
        self.assertIn("w=2", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
