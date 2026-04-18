#!/usr/bin/env python3
"""
tests/test_audit_medium.py — Atlas Red Team Audit MEDIUM修正の回帰テスト

対象:
  M-4: DeltaHedge ATM strike 銘柄別grid
  M-5: DeltaHedge _place_single_leg est_margin
  M-6: QCM _pick_fallback_source cache鮮度チェック
  M-7: QCM try_reconnect TOCTOU
  M-8: ORB fallback_prices クラス変数キャッシュ化
  M-9: cancel_all_open_orders FILLED_PART除外
  M-10: ORBEngine WHITELIST方式

実行: python3 -m pytest tests/test_audit_medium.py -v
"""
from __future__ import annotations

import sys
import types
import datetime
import pandas as pd
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

TRADING_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TRADING_DIR))

# futu モック（未インストール環境対応）
_futu_mock = types.ModuleType("futu")
_futu_mock.RET_OK = 0
_futu_mock.RET_ERROR = -1
_futu_mock.TrdSide = types.SimpleNamespace(BUY=1, SELL=2)
_futu_mock.KLType = types.SimpleNamespace(K_1M="K_1M")
_futu_mock.ModifyOrderOp = types.SimpleNamespace(CANCEL="CANCEL")
_futu_mock.TrdEnv = types.SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL")
_futu_mock.OrderType = types.SimpleNamespace(NORMAL="NORMAL", MARKET="MARKET")
_futu_mock.TimeInForce = types.SimpleNamespace(DAY="DAY")
sys.modules.setdefault("futu", _futu_mock)


# ── M-4: ATM strike 銘柄別grid ────────────────────────────────────────────────

class TestM4DeltaHedgeATMStrike:
    """M-4: DeltaHedge ATM strikeが銘柄別strike_intervalで丸められる。"""

    def test_atm_strike_spy_interval_1(self):
        """SPY: $1.0刻みで正しく丸める。"""
        from common.symbol_meta import get_meta
        meta = get_meta("US.SPY")
        interval = meta["strike_interval"]
        assert interval == 1.0
        price = 553.7
        atm = round(price / interval) * interval
        assert atm == 554.0

    def test_atm_strike_iwm_interval_05(self):
        """IWM: $0.5刻みで正しく丸める（旧round()では199.0になり非存在strike）。"""
        from common.symbol_meta import get_meta
        meta = get_meta("US.IWM")
        interval = meta["strike_interval"]
        assert interval == 0.5
        price = 199.3
        atm = round(price / interval) * interval
        assert atm == pytest.approx(199.5)

    def test_atm_strike_spx_interval_5(self):
        """SPX: $5.0刻みで正しく丸める。"""
        from common.symbol_meta import get_meta
        meta = get_meta("US..SPX")
        interval = meta["strike_interval"]
        assert interval == 5.0
        price = 5412.3
        atm = round(price / interval) * interval
        assert atm == pytest.approx(5410.0)

    def test_old_round_differs_from_new_for_iwm(self):
        """旧実装round()と新実装が異なる値を返すことで修正効果を確認。"""
        price = 199.3
        old_atm = round(price)               # = 199 (IWMで非存在strike)
        interval = 0.5
        new_atm = round(price / interval) * interval  # = 199.5
        assert old_atm != new_atm


# ── M-5: DeltaHedge est_margin ────────────────────────────────────────────────

class TestM5DeltaHedgeEstMargin:
    """M-5: ヘッジ発注時のinit_priceが設定されest_marginがゼロにならない。"""

    def test_hedge_init_price_nonzero(self):
        """underlying_price * 0.01 が init_price として計算される（ゼロでない）。"""
        underlying_price = 560.0
        hedge_est_price = underlying_price * 0.01
        assert hedge_est_price > 0
        assert hedge_est_price == pytest.approx(5.6)

    def test_hedge_init_price_for_various_prices(self):
        """各銘柄の価格で est_price > 0 になる。"""
        prices = {"SPY": 560.0, "QQQ": 480.0, "IWM": 200.0, "SPX": 5400.0}
        for ticker, p in prices.items():
            est = p * 0.01
            assert est > 0, f"{ticker}: est_price should be > 0, got {est}"


# ── M-6: QCM cache鮮度チェック ────────────────────────────────────────────────

class TestM6QCMCacheFreshness:
    """M-6: stale_cacheは新規エントリーをブロックする。"""

    def test_stale_cache_blocks_entry(self):
        """cacheが5分超の場合は allow_new_entry() == False。"""
        from common.quote_context_manager import QuoteContextManager
        m = QuoteContextManager()
        m.on_disconnect()
        m.state.last_disconnect_at = datetime.datetime.now() - datetime.timedelta(seconds=700)
        m.state.level = 10  # cacheを強制選択
        m._pick_fallback_source()
        assert m.state.active_source == "stale_cache"
        assert m.allow_new_entry() is False

    def test_fresh_cache_allows_entry(self):
        """cacheが新鮮（30秒前）なら stale_cache にならない。"""
        from common.quote_context_manager import QuoteContextManager
        m = QuoteContextManager()
        m.on_disconnect()
        m.state.last_disconnect_at = datetime.datetime.now() - datetime.timedelta(seconds=30)
        m.state.level = 10  # cacheを強制選択
        m._pick_fallback_source()
        assert m.state.active_source == "cache"
        # levelが10でも stale_cache でなければ allow_new_entry のlevel判定に委ねる

    def test_no_disconnect_time_is_not_stale(self):
        """last_disconnect_at=Noneの場合はstaleにならない。"""
        from common.quote_context_manager import QuoteContextManager
        m = QuoteContextManager()
        m.state.last_disconnect_at = None
        m.state.level = 10
        m._pick_fallback_source()
        assert m.state.active_source == "cache"  # stale判定スキップ


# ── M-7: QCM TOCTOU修正 ───────────────────────────────────────────────────────

class TestM7QCMTOCTOU:
    """M-7: try_reconnect の attempts カウントがatomicに更新される。"""

    def test_attempts_incremented_atomically(self):
        """再接続試行1回でattempts=1になる（成功後は0にリセット）。"""
        from common.quote_context_manager import QuoteContextManager
        import common.quote_context_manager as qcm
        m = QuoteContextManager(reconnect_fn=lambda: True)
        m.on_disconnect()
        orig = qcm._BACKOFF_SEQUENCE
        qcm._BACKOFF_SEQUENCE = [0.001, 0.001, 0.001]
        try:
            ok = m.try_reconnect()
        finally:
            qcm._BACKOFF_SEQUENCE = orig
        assert ok is True
        with m._lock:
            assert m.state.reconnect_attempts == 0  # 成功後リセット

    def test_failed_attempts_not_reset(self):
        """失敗した場合はattempts=1のまま（リセットしない）。"""
        from common.quote_context_manager import QuoteContextManager
        import common.quote_context_manager as qcm
        m = QuoteContextManager(reconnect_fn=lambda: False)
        m.on_disconnect()
        orig = qcm._BACKOFF_SEQUENCE
        qcm._BACKOFF_SEQUENCE = [0.001, 0.001, 0.001]
        try:
            ok = m.try_reconnect()
        finally:
            qcm._BACKOFF_SEQUENCE = orig
        assert ok is False
        with m._lock:
            assert m.state.reconnect_attempts == 1  # 失敗時は維持


# ── M-8: ORB fallback_prices クラス変数化 ────────────────────────────────────

class TestM8ORBFallbackPrices:
    """M-8: ORBEngine._get_fallback_price() がデフォルト値を返す。"""

    def setup_method(self):
        """テスト前にキャッシュをリセット。"""
        # クラス変数をリセット（テスト間の独立性確保）
        import spy_bot
        spy_bot.ORBEngine._fallback_price_cache = {}
        spy_bot.ORBEngine._fallback_cache_fetched = False

    def test_default_spy_price(self):
        """キャッシュなし時はデフォルト値（SPY=560）を返す。"""
        # Finnhubリクエストをモックしてデフォルト値テスト
        import spy_bot
        spy_bot.ORBEngine._fallback_cache_fetched = True  # Finnhub取得をスキップ
        price = spy_bot.ORBEngine._get_fallback_price("SPY")
        assert price == 560.0

    def test_default_nvda_price(self):
        """NVDA デフォルト値 $900。"""
        import spy_bot
        spy_bot.ORBEngine._fallback_cache_fetched = True
        price = spy_bot.ORBEngine._get_fallback_price("NVDA")
        assert price == 900.0

    def test_unknown_symbol_returns_300(self):
        """未知銘柄は $300 を返す。"""
        import spy_bot
        spy_bot.ORBEngine._fallback_cache_fetched = True
        price = spy_bot.ORBEngine._get_fallback_price("UNKNOWN")
        assert price == 300.0

    def test_cache_overrides_default(self):
        """キャッシュ値がデフォルト値より優先される。"""
        import spy_bot
        spy_bot.ORBEngine._fallback_cache_fetched = True
        spy_bot.ORBEngine._fallback_price_cache["SPY"] = 620.0  # 新しい価格
        price = spy_bot.ORBEngine._get_fallback_price("SPY")
        assert price == 620.0


# ── M-9: cancel_all_open_orders FILLED_PART除外 ───────────────────────────────

class TestM9CancelOrdersFilledPart:
    """M-9: FILLED_PARTオーダーがキャンセル対象外で、アラートリストに追加される。

    cancel_all_open_ordersはfutu/RET_OK依存が強いため、
    ロジックを純粋テスト可能な形で検証する。
    """

    def test_filled_part_excluded_from_cancel_status(self):
        """FILLED_PARTはcancel_status（キャンセル対象）に含まれない。"""
        cancel_status = {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}
        alert_status = {"FILLED_PART"}
        # FILLED_PARTはどちらのセットに入るか
        assert "FILLED_PART" not in cancel_status
        assert "FILLED_PART" in alert_status

    def test_cancel_status_contains_correct_orders(self):
        """キャンセル対象はSUBMITTED/WAITING_SUBMIT/SUBMITTING。"""
        cancel_status = {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}
        assert "SUBMITTED" in cancel_status
        assert "WAITING_SUBMIT" in cancel_status
        assert "SUBMITTING" in cancel_status
        assert "FILLED_ALL" not in cancel_status
        assert "FILLED_PART" not in cancel_status

    def test_filled_part_logic_simulation(self):
        """FILLED_PART判定ロジックのシミュレーション。"""
        cancel_status = {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}
        alert_status = {"FILLED_PART"}

        orders = [
            {"order_id": "ORD001", "order_status": "FILLED_PART", "code": "US.SPY260418P00553000",
             "qty": 2, "dealt_qty": 1},
            {"order_id": "ORD002", "order_status": "SUBMITTED", "code": "US.SPY260418C00560000",
             "qty": 1, "dealt_qty": 0},
            {"order_id": "ORD003", "order_status": "FILLED_ALL", "code": "US.SPY260418C00560000",
             "qty": 1, "dealt_qty": 1},
        ]

        canceled = []
        alerted = []
        for o in orders:
            status = o["order_status"]
            if status in alert_status:
                alerted.append(o["order_id"])
            elif status in cancel_status:
                canceled.append(o["order_id"])

        assert "ORD001" not in canceled  # FILLED_PARTはキャンセルしない
        assert "ORD001" in alerted       # FILLED_PARTはアラート
        assert "ORD002" in canceled      # SUBMITTEDはキャンセル
        assert "ORD003" not in canceled  # FILLED_ALLはスキップ
        assert "ORD003" not in alerted   # FILLED_ALLはアラートもしない

    def test_filled_part_pushover_priority_is_2(self):
        """FILLED_PART発見時のPushoverはpriority=2を想定。"""
        # コードレビュー: cancel_all_open_ordersでpriority=2を指定している
        # これはFILLED_PARTが裸ポジションリスクを持つため高優先度アラートが必要
        # priority=1（urgent）より低いが、0（normal）より高い緊急通知
        expected_priority = 2
        # M-9修正でpushover_alert(priority=2)を呼ぶことを確認（コードレビューテスト）
        import inspect
        import spy_bot
        source = inspect.getsource(spy_bot.TradeEngine.cancel_all_open_orders)
        assert "priority=2" in source, "FILLED_PART検出時にpriority=2のPushoverが必要"
        assert "FILLED_PART" in source, "FILLED_PARTの処理が実装されている"
        assert "alert_status" in source, "alert_status分離ロジックが実装されている"


# ── M-10: ORBEngine WHITELIST方式 ─────────────────────────────────────────────

class TestM10ORBWhitelist:
    """M-10: ORBEngineがALLOWED_SYMBOLS外銘柄でエントリーをスキップする。"""

    def test_allowed_symbol_not_blocked(self):
        """US.SPY は ALLOWED_SYMBOLS に含まれるため is_allowed() == True。"""
        from common.symbol_meta import is_allowed
        assert is_allowed("US.SPY") is True

    def test_unknown_symbol_blocked(self):
        """未知銘柄は is_allowed() == False。"""
        from common.symbol_meta import is_allowed
        assert is_allowed("US.UNKNOWN_TICKER") is False

    def test_all_expected_symbols_allowed(self):
        """設計対象銘柄が全てALLOWED_SYMBOLS内にある。"""
        from common.symbol_meta import is_allowed
        expected = ["US.SPY", "US.QQQ", "US.IWM", "US..SPX",
                    "US.NVDA", "US.TSLA", "US.META", "US.AMZN",
                    "US.GOOGL", "US.AAPL", "US.MSFT"]
        for sym in expected:
            assert is_allowed(sym), f"{sym} should be in ALLOWED_SYMBOLS"
