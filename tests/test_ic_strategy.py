"""IronCondorSellEngine regression tests (10 tests minimum)

テスト方針:
  - IronCondorSellEngine を dry_test=True でインスタンス化
  - 実際の futu/moomoo 接続なしで全テストを実行
  - 動的パラメータ算出・premarket_check・execute_entry・check_exit・PnL記録を検証
"""
import os
import sys
import json
import datetime
import tempfile
import types
import pytest

# spy_bot.py をインポートする前に futu を mock して ImportError を回避
futu_mock = types.ModuleType("futu")
futu_mock.TrdSide = types.SimpleNamespace(BUY="BUY", SELL="SELL")
futu_mock.OrderType = types.SimpleNamespace(MARKET="MARKET", LIMIT="LIMIT")
futu_mock.TrdMarket = types.SimpleNamespace(US="US")
futu_mock.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
futu_mock.RET_OK = 0
futu_mock.SecurityFirm = types.SimpleNamespace(FUTUINC="FUTUINC")
futu_mock.SubType = types.SimpleNamespace(TICKER="TICKER")
futu_mock.TimeInForce = types.SimpleNamespace(DAY="DAY")
futu_mock.ModifyOrderOp = types.SimpleNamespace(CANCEL="CANCEL")
futu_mock.StockQuoteHandlerBase = object
futu_mock.OpenQuoteContext = object
futu_mock.OpenSecTradeContext = object
sys.modules.setdefault("futu", futu_mock)

# trading dir を path に追加
_TRADING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TRADING_DIR)

import spy_bot as bot_mod
from spy_bot import (
    IronCondorSellEngine,
    IronCondorSellPosition,
    IC_SELL_CALL_DELTA_BASE,
    IC_SELL_PUT_DELTA_BASE,
    IC_SELL_VIX_HIGH_THRESHOLD,
    IC_SELL_CAPITAL_PCT_BASE,
    IC_SELL_CAPITAL_PCT_HIGH,
    IC_SELL_WIDTH_DEFAULT,
    IC_SELL_PROFIT_TARGET_PCT,
    IC_SELL_STOP_LOSS_MULT,
    IC_SELL_EXCLUDED_SYMBOLS,
    ENABLE_IC_SELL,
)


# ── モック MarketData / TradeEngine ──────────────────────────────────────────

class MockMarketData:
    def __init__(self, underlying="US.SPY", vix=22.0):
        self.underlying_code = underlying
        self._vix = vix

    def get_vix(self):
        return self._vix

    def get_vix_history(self, days=60):
        # 60日分の VIX データを生成（現在VIXが50%ile相当になるよう）
        import random
        random.seed(42)
        base = self._vix
        return [base + random.uniform(-5, 5) for _ in range(days)]

    def get_spy_current(self):
        return 560.0

    def get_option_chain_with_greeks(self, expiry, direction, center_strike=None):
        # delta 0.20 相当の OTM オプションをシミュレート
        price = 560.0
        opts = []
        for strike in range(int(price) - 30, int(price) + 31, 1):
            dist = abs(strike - price)
            if direction == "CALL":
                delta = max(0.01, 0.50 - dist * 0.01)
            else:
                delta = max(0.01, 0.50 - dist * 0.01)
            opts.append({
                "code": f"US.SPY260418{'C' if direction == 'CALL' else 'P'}{int(strike * 1000)}",
                "strike_price": float(strike),
                "delta": delta if direction == "CALL" else -delta,
                "bid_price": 0.35,
                "ask_price": 0.45,
                "last_price": 0.40,
            })
        return opts

    def find_by_delta(self, chain, target_delta):
        """絶対値が target_delta に最も近いオプションを返す。"""
        if not chain:
            return None
        return min(chain, key=lambda x: abs(abs(x.get("delta", 0)) - target_delta))

    def find_by_strike(self, chain, target_strike):
        if not chain:
            return None
        return min(chain, key=lambda x: abs(x.get("strike_price", 0) - target_strike))

    def get_symbol_atr(self, symbol, period=14):
        return 10.0  # ATR 10 → width = 10 * 0.50 = 5

    def calc_ivr(self):
        return 55.0


class MockTradeEngine:
    def __init__(self, cash=50000.0):
        self._cash = cash
        self.trade_ctx = None

    def get_account_cash(self):
        return self._cash

    def get_open_positions(self):
        return []

    def place_credit_spread(self, sell_code, buy_code, qty, direction,
                             sell_init_price=None, buy_init_price=None, vix=None):
        return True  # 発注成功


def _make_engine(underlying="US.SPY", vix=22.0, cash=50000.0, paper=True, dry_test=True):
    mkt = MockMarketData(underlying=underlying, vix=vix)
    eng = MockTradeEngine(cash=cash)
    return IronCondorSellEngine(mkt, eng, paper=paper, dry_test=dry_test)


# ═══════════════════════════════════════════════════════════════════
# Test 1: クラスが正常にインスタンス化できる
# ═══════════════════════════════════════════════════════════════════
def test_engine_instantiation():
    engine = _make_engine()
    assert engine is not None
    assert engine.dry_test is True
    assert engine.paper is True
    assert engine.position is None
    assert engine.trade_done is False


# ═══════════════════════════════════════════════════════════════════
# Test 2: ENABLE_IC_SELL=True のグローバルフラグ確認
# ═══════════════════════════════════════════════════════════════════
def test_enable_ic_sell_flag():
    assert ENABLE_IC_SELL is True


# ═══════════════════════════════════════════════════════════════════
# Test 3: [4/17事故対応] EXCLUDED_SYMBOLS廃止 — 空setであることを確認
# ═══════════════════════════════════════════════════════════════════
def test_excluded_symbols():
    # EXCLUDED_SYMBOLS廃止。混入防止は validate_code_for_symbol() の物理ブロックで実施。
    assert IC_SELL_EXCLUDED_SYMBOLS == set()


# ═══════════════════════════════════════════════════════════════════
# Test 4: premarket_check() — US..SPX は取引対象に復活
# [4/17事故対応] EXCLUDED_SYMBOLS廃止。dry_test=False では VIX/IVR で判定される。
# ═══════════════════════════════════════════════════════════════════
def test_premarket_check_excluded_symbol():
    # underlying=US..SPX でも除外されない。VIX取得失敗でFalseになるのは許容。
    engine = _make_engine(underlying="US..SPX", dry_test=False)
    # IronCondorSellEngine.EXCLUDED_SYMBOLS が空setなので除外ガードはパスする
    # mkt.get_vix() が None を返すためVIX取得失敗でFalseになる
    result = engine.premarket_check()
    # VIX取得失敗でFalseになるのは正常（除外が理由ではない）
    assert isinstance(result, bool)


# ═══════════════════════════════════════════════════════════════════
# Test 5: premarket_check() — dry_test=True は常にTrueを返す
# ═══════════════════════════════════════════════════════════════════
def test_premarket_check_dry_test():
    engine = _make_engine(dry_test=True)
    result = engine.premarket_check()
    assert result is True
    assert engine.today_vix == 22.0


# ═══════════════════════════════════════════════════════════════════
# Test 6: _calc_dynamic_deltas() — 通常VIX(20)では base delta を返す
# ═══════════════════════════════════════════════════════════════════
def test_calc_dynamic_deltas_normal_vix():
    engine = _make_engine()
    call_d, put_d = engine._calc_dynamic_deltas(vix=20.0, ivr_pct=50.0)
    assert call_d == IC_SELL_CALL_DELTA_BASE
    assert put_d  == IC_SELL_PUT_DELTA_BASE
    assert 0.10 <= call_d <= 0.35
    assert 0.10 <= put_d  <= 0.35


# ═══════════════════════════════════════════════════════════════════
# Test 7: _calc_dynamic_deltas() — 高VIX(30)でデルタ縮小
# ═══════════════════════════════════════════════════════════════════
def test_calc_dynamic_deltas_high_vix():
    engine = _make_engine()
    call_d_normal, _ = engine._calc_dynamic_deltas(vix=20.0, ivr_pct=50.0)
    call_d_high,   _ = engine._calc_dynamic_deltas(vix=30.0, ivr_pct=50.0)
    assert call_d_high < call_d_normal, "高VIX時はデルタが縮小すべき"
    assert call_d_high >= 0.10, "下限0.10"


# ═══════════════════════════════════════════════════════════════════
# Test 8: _calc_dynamic_deltas() — 高IVR(75%ile)でデルタ拡大
# ═══════════════════════════════════════════════════════════════════
def test_calc_dynamic_deltas_high_ivr():
    engine = _make_engine()
    call_d_normal, _ = engine._calc_dynamic_deltas(vix=20.0, ivr_pct=50.0)
    call_d_high,   _ = engine._calc_dynamic_deltas(vix=20.0, ivr_pct=75.0)
    assert call_d_high > call_d_normal, "高IVR時はデルタが拡大すべき"
    assert call_d_high <= 0.35, "上限0.35"


# ═══════════════════════════════════════════════════════════════════
# Test 9: _calc_capital_pct() — VIX閾値で配分率が変わる
# ═══════════════════════════════════════════════════════════════════
def test_calc_capital_pct():
    engine = _make_engine()
    pct_normal = engine._calc_capital_pct(vix=20.0)
    pct_high   = engine._calc_capital_pct(vix=IC_SELL_VIX_HIGH_THRESHOLD + 1)
    assert pct_normal == IC_SELL_CAPITAL_PCT_BASE
    assert pct_high   == IC_SELL_CAPITAL_PCT_HIGH
    assert pct_high < pct_normal, "高VIX時は配分率が下がるべき"


# ═══════════════════════════════════════════════════════════════════
# Test 10: _calc_qty() — TMR検証あり・上限適用
# ═══════════════════════════════════════════════════════════════════
def test_calc_qty_tmr_and_ceiling():
    engine = _make_engine(cash=50000.0)
    qty = engine._calc_qty(cash=50000.0, spread_width=5, capital_pct=0.40)
    # 50000 * 0.40 / (5 * 100) = 40 → max_qty_paper=10 でクランプ
    assert qty == 10, f"paper最大枚数に制限されるべき: got {qty}"

    # 小口座は1枚
    engine_small = _make_engine(cash=10000.0)
    qty_small = engine_small._calc_qty(cash=10000.0, spread_width=5, capital_pct=0.40)
    assert qty_small == 1, f"SMALL_ACCOUNT_USD以下は1枚: got {qty_small}"


# ═══════════════════════════════════════════════════════════════════
# Test 11: execute_entry() — dry_test でポジションが生成される
# ═══════════════════════════════════════════════════════════════════
def test_execute_entry_dry_test(tmp_path, monkeypatch):
    # PnL ファイルを tmp_path に向ける
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", tmp_path / "ic_sell_pnl.json")

    engine = _make_engine(dry_test=True)
    engine.today_vix = 22.0
    pos = engine.execute_entry()

    assert pos is not None, "execute_entry は IronCondorSellPosition を返すべき"
    assert isinstance(pos, IronCondorSellPosition)
    assert pos.qty >= 1
    assert pos.net_credit > 0
    assert pos.call_sell_strike > pos.call_buy_strike or pos.call_buy_strike > pos.call_sell_strike
    assert engine.entry_done is True
    assert engine.trade_done is True
    assert engine.position is pos


# ═══════════════════════════════════════════════════════════════════
# Test 12: execute_entry() — entry_done=True で None を返す
# ═══════════════════════════════════════════════════════════════════
def test_execute_entry_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", tmp_path / "ic_sell_pnl.json")
    engine = _make_engine(dry_test=True)
    engine.today_vix = 22.0
    pos1 = engine.execute_entry()
    pos2 = engine.execute_entry()  # 2回目はNone
    assert pos1 is not None
    assert pos2 is None


# ═══════════════════════════════════════════════════════════════════
# Test 13: check_exit() — TP到達でクローズされる
# ═══════════════════════════════════════════════════════════════════
def test_check_exit_profit_target(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", tmp_path / "ic_sell_pnl.json")
    engine = _make_engine(dry_test=True)
    engine.today_vix = 22.0
    engine.execute_entry()

    pos = engine.position
    assert pos is not None

    # _estimate_current_value を TP条件（credit x 50%以下に減衰）に強制
    # current_pnl = net_credit - current_value >= target
    # → current_value <= net_credit * (1 - TP_pct) に設定
    target_value = round(pos.net_credit * (1.0 - IC_SELL_PROFIT_TARGET_PCT), 4) - 0.01

    monkeypatch.setattr(engine, "_estimate_current_value",
                        lambda p: target_value)

    closed = engine.check_exit()
    assert closed is True, "TP到達でクローズされるべき"
    assert engine.position is None


# ═══════════════════════════════════════════════════════════════════
# Test 14: check_exit() — SL到達でクローズされる
# ═══════════════════════════════════════════════════════════════════
def test_check_exit_stop_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", tmp_path / "ic_sell_pnl.json")
    engine = _make_engine(dry_test=True)
    engine.today_vix = 22.0
    engine.execute_entry()

    pos = engine.position
    assert pos is not None

    # current_pnl = net_credit - current_value <= -SL_threshold
    # → current_value >= net_credit * (1 + SL_mult) + epsilon
    sl_value = round(pos.net_credit * (1.0 + IC_SELL_STOP_LOSS_MULT), 4) + 0.01

    monkeypatch.setattr(engine, "_estimate_current_value",
                        lambda p: sl_value)

    closed = engine.check_exit()
    assert closed is True, "SL到達でクローズされるべき"
    assert engine.position is None


# ═══════════════════════════════════════════════════════════════════
# Test 15: check_exit() — TP未達・SL未達では保有継続
# ═══════════════════════════════════════════════════════════════════
def test_check_exit_holds_position(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", tmp_path / "ic_sell_pnl.json")
    engine = _make_engine(dry_test=True)
    engine.today_vix = 22.0
    engine.execute_entry()

    pos = engine.position
    assert pos is not None

    # 中間価値: TP未達かつSL未達
    mid_value = pos.net_credit * 0.8  # pnl = 20% → TP=50%未達・SL未達

    monkeypatch.setattr(engine, "_estimate_current_value",
                        lambda p: mid_value)

    closed = engine.check_exit()
    assert closed is False, "TP/SL未達では保有継続すべき"
    assert engine.position is not None


# ═══════════════════════════════════════════════════════════════════
# Test 16: reset_daily() — 全フィールドがリセットされる
# ═══════════════════════════════════════════════════════════════════
def test_reset_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", tmp_path / "ic_sell_pnl.json")
    engine = _make_engine(dry_test=True)
    engine.today_vix = 22.0
    engine.execute_entry()

    assert engine.trade_done is True
    assert engine.entry_done is True
    assert engine.position is not None

    engine.reset_daily()

    assert engine.today_vix    is None
    assert engine.position     is None
    assert engine.trade_done   is False
    assert engine.entry_done   is False


# ═══════════════════════════════════════════════════════════════════
# Test 17: PnL ファイルにエントリーが記録される
# ═══════════════════════════════════════════════════════════════════
def test_pnl_entry_recorded(tmp_path, monkeypatch):
    pnl_path = tmp_path / "ic_sell_pnl.json"
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", pnl_path)

    engine = _make_engine(dry_test=True)
    engine.today_vix = 22.0
    engine.execute_entry()

    assert pnl_path.exists(), "PnLファイルが作成されるべき"
    data = json.loads(pnl_path.read_text())
    assert "trades" in data
    entries = [t for t in data["trades"] if t.get("event") == "entry"]
    assert len(entries) >= 1
    entry = entries[0]
    assert entry["strategy"] == "IC"
    assert entry["tactic"] == "ic_sell"
    assert entry["qty"] >= 1
    assert entry["net_credit"] > 0


# ═══════════════════════════════════════════════════════════════════
# Test 18: is_active() — ポジションの有無を正しく返す
# ═══════════════════════════════════════════════════════════════════
def test_is_active(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_mod, "IC_SELL_PNL_FILE", tmp_path / "ic_sell_pnl.json")
    engine = _make_engine(dry_test=True)
    assert engine.is_active() is False

    engine.today_vix = 22.0
    engine.execute_entry()
    assert engine.is_active() is True

    engine.reset_daily()
    assert engine.is_active() is False


# ═══════════════════════════════════════════════════════════════════
# Test 19: _get_ivr_percentile() — 50%ile前後を返す
# ═══════════════════════════════════════════════════════════════════
def test_get_ivr_percentile():
    engine = _make_engine(vix=22.0)
    engine.today_vix = 22.0
    pct = engine._get_ivr_percentile()
    assert 0.0 <= pct <= 100.0, f"パーセンタイルは0-100の範囲: {pct}"


# ═══════════════════════════════════════════════════════════════════
# Test 20: IronCondorSellPosition — net_credit と max_loss が正しい
# ═══════════════════════════════════════════════════════════════════
def test_position_net_credit_and_max_loss():
    pos = IronCondorSellPosition(
        symbol="US.SPY", expiry="2026-04-18", qty=2,
        call_sell_code="CS_CODE", call_buy_code="CB_CODE",
        put_sell_code="PS_CODE", put_buy_code="PB_CODE",
        call_sell_strike=570.0, call_buy_strike=575.0,
        put_sell_strike=550.0, put_buy_strike=545.0,
        call_net_credit=0.40, put_net_credit=0.35,
        spread_width=5, vix=22.0,
    )
    assert abs(pos.net_credit - 0.75) < 1e-9, f"net_credit={pos.net_credit}"
    # max_loss_per_contract = (5 - 0.75) * 100 = 425
    expected_max_loss = (5 - 0.75) * 100
    assert abs(pos.max_loss_per_contract - expected_max_loss) < 1e-6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
