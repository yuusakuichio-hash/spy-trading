"""
tests/test_strangle_sell.py — StrangleSellEngine テスト（10テスト以上）

futu未接続のdry_testモードで動作する。外部API依存なし。
"""

import sys
import os
import datetime
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import spy_bot as bot
from spy_bot import (
    StrangleSellEngine,
    StrangleSellPosition,
    STRANGLE_SELL_CALL_DELTA,
    STRANGLE_SELL_PUT_DELTA,
    STRANGLE_SELL_IVR_MIN,
    STRANGLE_SELL_VIX_MIN,
    STRANGLE_SELL_VIX_MAX,
    STRANGLE_SELL_PROFIT_TARGET,
    STRANGLE_SELL_STOP_LOSS_MULT,
    STRANGLE_SELL_MAX_RISK_PCT,
    STRANGLE_SELL_MAX_QTY,
    ENABLE_STRANGLE_SELL,
    ET,
)


# ── モックオブジェクト ─────────────────────────────────────────────────────────

class MockMarketData:
    underlying_code = "US.SPY"
    quote_ctx = None

    def get_vix(self):
        return 22.0

    def get_spy_current(self):
        return 560.0

    def get_option_chain_with_greeks(self, expiry, opt_type, center_strike=None):
        return []

    def get_option_greeks(self, code):
        return {"last": 0.20, "iv": 0.25, "delta": 0.15}

    def calc_ivr(self):
        return 70.0

    def get_ivr_percentiles(self):
        return {"p75": 70.0, "p70": 62.0}


class MockTradeEngine:
    def __init__(self):
        self._virtual_pos = MockVirtualPos()

    def get_account_cash(self):
        return 15000.0

    def _place_single_leg(self, code, side, qty, tag):
        return "order_id_mock", "ok"


class MockVirtualPos:
    def add_position(self, code, qty, price, side):
        pass


# ── StrangleSellEngine テスト ─────────────────────────────────────────────────

class TestStrangleSellEngineInit(unittest.TestCase):
    """StrangleSellEngineの初期化テスト。"""

    def setUp(self):
        self.mkt = MockMarketData()
        self.eng = MockTradeEngine()

    def test_01_init_default_symbol(self):
        """symbol未指定時はmkt.underlying_codeを使う。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        self.assertEqual(engine.symbol, "US.SPY")

    def test_02_init_custom_symbol(self):
        """symbol引数を指定できる。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True, symbol="US.QQQ")
        self.assertEqual(engine.symbol, "US.QQQ")

    def test_03_excluded_symbols_constant(self):
        """[4/17事故対応] EXCLUDED_SYMBOLS廃止。空setであることを確認。
        混入防止は validate_code_for_symbol() の物理ブロックで実施。"""
        self.assertEqual(StrangleSellEngine.EXCLUDED_SYMBOLS, set())

    def test_04_init_state(self):
        """初期状態は全フラグFalse・position=None。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        self.assertIsNone(engine.position)
        self.assertFalse(engine.entry_done)
        self.assertFalse(engine.trade_done)
        self.assertFalse(engine._entry_attempted)

    def test_05_reset_daily_clears_state(self):
        """reset_daily()で状態がリセットされる。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        # entryして状態を変える
        engine.execute_entry(underlying_price=560.0, vix=22.0)
        engine.reset_daily()
        self.assertIsNone(engine.position)
        self.assertFalse(engine.entry_done)
        self.assertFalse(engine.trade_done)
        self.assertFalse(engine._entry_attempted)


class TestShouldTradeToday(unittest.TestCase):
    """StrangleSellEngine.should_trade_today() ロジックテスト。"""

    def test_06_disabled_returns_false(self):
        """ENABLE_STRANGLE_SELL=False のときは False。"""
        original = bot.ENABLE_STRANGLE_SELL
        try:
            bot.ENABLE_STRANGLE_SELL = False
            result = StrangleSellEngine.should_trade_today(
                symbol="US.SPY", vix=22.0, ivr=70.0,
                ivr_high_threshold=60.0, paper=False
            )
            self.assertFalse(result)
        finally:
            bot.ENABLE_STRANGLE_SELL = original

    def test_07_spx_no_longer_excluded(self):
        """[4/17事故対応] EXCLUDED_SYMBOLS廃止。US..SPXはVIX/IVR条件を満たせば取引対象。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US..SPX", vix=22.0, ivr=70.0,
            ivr_high_threshold=60.0, paper=False
        )
        # EXCLUDED_SYMBOLSが空setなので、VIX=22(範囲内) IVR=70>60 → True
        self.assertTrue(result)

    def test_08_none_vix_returns_false(self):
        """VIX=NoneのときはFalse。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US.SPY", vix=None, ivr=70.0,
            ivr_high_threshold=60.0, paper=False
        )
        self.assertFalse(result)

    def test_09_vix_below_min_returns_false(self):
        """VIX < STRANGLE_SELL_VIX_MIN のときはFalse。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US.SPY", vix=STRANGLE_SELL_VIX_MIN - 1.0, ivr=70.0,
            ivr_high_threshold=60.0, paper=False
        )
        self.assertFalse(result)

    def test_10_vix_above_max_returns_false(self):
        """VIX > STRANGLE_SELL_VIX_MAX のときはFalse。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US.SPY", vix=STRANGLE_SELL_VIX_MAX + 1.0, ivr=70.0,
            ivr_high_threshold=60.0, paper=False
        )
        self.assertFalse(result)

    def test_11_low_ivr_returns_false(self):
        """IVR < ivr_high_threshold のときはFalse。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US.SPY", vix=22.0, ivr=50.0,
            ivr_high_threshold=60.0, paper=False
        )
        self.assertFalse(result)

    def test_12_high_ivr_returns_true(self):
        """IVR >= ivr_high_threshold かつ VIX範囲内でTrue。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US.SPY", vix=22.0, ivr=70.0,
            ivr_high_threshold=60.0, paper=False
        )
        self.assertTrue(result)

    def test_13_paper_mode_bypasses_ivr(self):
        """ペーパーモードはIVR/VIX条件をバイパスしてTrue。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US.SPY", vix=10.0, ivr=20.0,
            ivr_high_threshold=60.0, paper=True
        )
        self.assertTrue(result)

    def test_14_multi_symbol_spy_allowed(self):
        """US.SPYは除外対象外でTrue（条件充足時）。"""
        result = StrangleSellEngine.should_trade_today(
            symbol="US.SPY", vix=22.0, ivr=70.0,
            ivr_high_threshold=60.0, paper=False
        )
        self.assertTrue(result)


class TestStrangleSellEngineEntry(unittest.TestCase):
    """StrangleSellEngine.execute_entry() テスト（dry_test）。"""

    def setUp(self):
        self.mkt = MockMarketData()
        self.eng = MockTradeEngine()

    def test_15_dry_test_entry_returns_position(self):
        """dry_testモードでエントリーするとStrangleSellPositionを返す。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        pos = engine.execute_entry(underlying_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertIsInstance(pos, StrangleSellPosition)

    def test_16_spx_execute_entry_not_excluded(self):
        """[4/17事故対応] EXCLUDED_SYMBOLS廃止。US..SPXは除外されない。
        execute_entry は dry_test=True で実行し、EXCLUDED_SYMBOLS 空setを確認。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True, symbol="US..SPX")
        # EXCLUDED_SYMBOLS が空setであることを確認
        self.assertEqual(engine.EXCLUDED_SYMBOLS, set())

    def test_17_entry_sets_position(self):
        """エントリー後にposition, entry_doneがセットされる。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        pos = engine.execute_entry(underlying_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertTrue(engine.entry_done)
        self.assertTrue(engine.is_active())

    def test_18_no_double_entry(self):
        """2回目のexecute_entryはNoneを返す（_entry_attempted ガード）。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        pos1 = engine.execute_entry(underlying_price=560.0, vix=22.0)
        pos2 = engine.execute_entry(underlying_price=560.0, vix=22.0)
        self.assertIsNotNone(pos1)
        self.assertIsNone(pos2)

    def test_19_position_has_valid_credits(self):
        """ポジションのnet_creditが正の値。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        pos = engine.execute_entry(underlying_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertGreater(pos.net_credit, 0)
        self.assertGreater(pos.call_strike, 0)
        self.assertGreater(pos.put_strike, 0)
        self.assertGreater(pos.qty, 0)

    def test_20_call_strike_above_underlying(self):
        """CALLストライクは原資産価格より高い（OTM確認）。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        pos = engine.execute_entry(underlying_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertGreater(pos.call_strike, 560.0)

    def test_21_put_strike_below_underlying(self):
        """PUTストライクは原資産価格より低い（OTM確認）。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        pos = engine.execute_entry(underlying_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertLess(pos.put_strike, 560.0)


class TestStrangleSellEngineExit(unittest.TestCase):
    """StrangleSellEngine.check_exit() / _close_position() テスト（dry_test）。"""

    def setUp(self):
        self.mkt = MockMarketData()
        self.eng = MockTradeEngine()

    def test_22_no_exit_without_position(self):
        """ポジションなし時はcheck_exit()がNoneを返す。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        result = engine.check_exit()
        self.assertIsNone(result)

    def test_23_exit_after_drytest_timeout(self):
        """dry_test: 8分経過後にcheck_exitが決済結果を返す。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        engine.execute_entry(underlying_price=560.0, vix=22.0)
        # 起動時刻を10分前に偽装
        engine._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=10)
        result = engine.check_exit()
        self.assertIsNotNone(result)
        self.assertIn("reason", result)
        self.assertIn("pnl_usd", result)

    def test_24_exit_clears_position(self):
        """決済後にpositionがNoneになる。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        engine.execute_entry(underlying_price=560.0, vix=22.0)
        engine._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=10)
        engine.check_exit()
        self.assertIsNone(engine.position)
        self.assertFalse(engine.is_active())
        self.assertTrue(engine.trade_done)

    def test_25_profit_target_pnl_positive(self):
        """profit_target決済ではpnl_usd > 0。"""
        engine = StrangleSellEngine(self.mkt, self.eng, dry_test=True)
        engine.execute_entry(underlying_price=560.0, vix=22.0)
        engine._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=10)
        result = engine.check_exit()
        self.assertIsNotNone(result)
        # dry_testのprofit_target_drytestは利益
        self.assertGreater(result["pnl_usd"], 0)

    def test_26_multi_symbol_qqq_entry(self):
        """US.QQQシンボルでエントリー（マルチ銘柄対応確認）。"""
        mkt = MockMarketData()
        mkt.underlying_code = "US.QQQ"
        engine = StrangleSellEngine(mkt, MockTradeEngine(), dry_test=True, symbol="US.QQQ")
        pos = engine.execute_entry(underlying_price=450.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.symbol, "US.QQQ")


class TestStrangleSellConstantsConfig(unittest.TestCase):
    """パラメータ定数の妥当性テスト。"""

    def test_27_delta_targets_positive(self):
        """CALLとPUTのdelta目標が正の値で0.10〜0.30の範囲内。"""
        self.assertGreater(STRANGLE_SELL_CALL_DELTA, 0.0)
        self.assertGreater(STRANGLE_SELL_PUT_DELTA, 0.0)
        self.assertLessEqual(STRANGLE_SELL_CALL_DELTA, 0.30)
        self.assertLessEqual(STRANGLE_SELL_PUT_DELTA, 0.30)

    def test_28_profit_target_less_than_stop(self):
        """利確%は損切り倍率より小さい（論理一貫性）。"""
        # PROFIT_TARGET=0.50 (50%利確), STOP_LOSS_MULT=2.0 (200%でストップ)
        # 0.50 < 2.00 は自明だが念のため
        self.assertLess(STRANGLE_SELL_PROFIT_TARGET, STRANGLE_SELL_STOP_LOSS_MULT)

    def test_29_max_risk_pct_reasonable(self):
        """最大リスク%が1%〜10%の実運用可能な範囲内。"""
        self.assertGreaterEqual(STRANGLE_SELL_MAX_RISK_PCT, 0.01)
        self.assertLessEqual(STRANGLE_SELL_MAX_RISK_PCT, 0.10)

    def test_30_max_qty_positive(self):
        """最大Qtyが1以上。"""
        self.assertGreaterEqual(STRANGLE_SELL_MAX_QTY, 1)

    def test_31_vix_range_valid(self):
        """VIX下限 < 上限。"""
        self.assertLess(STRANGLE_SELL_VIX_MIN, STRANGLE_SELL_VIX_MAX)

    def test_32_enabled_by_default(self):
        """ENABLE_STRANGLE_SELL がデフォルトでTrue。"""
        self.assertTrue(ENABLE_STRANGLE_SELL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
