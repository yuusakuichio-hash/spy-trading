"""tests/test_atlas_trade_engine_native_20260425.py — TradeEngine v3 テスト

atlas_v3/core/trade_engine.py の全公開 API を 30+ ケースで検証する。
spy_bot.py は一切触れない。

テスト分類:
  - Unit: DRY_TEST=1 / futu 未利用環境で完結（VirtualPositionManager）
  - Integration: futu mock / CircuitBreaker / Bulkhead / PreTradeGate / KillSwitch
  - Schema contract: BreakerConfig / _VirtualPositionManager / _extract_symbol_from_code

環境:
  - FUTU_AVAILABLE=False 前提（futu SDK 未インストール環境）
  - conftest.py autouse により state_v3 は tmp_path に隔離済み
"""
from __future__ import annotations

import importlib
import os
import sys
import time
import types
import unittest.mock as mock
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# プロジェクトルートを sys.path に追加
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# ヘルパー: DRY_TEST 環境変数セット
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _dry_test_env(monkeypatch):
    """全テストで DRY_TEST=1 を設定し futu 依存経路をスキップする。"""
    monkeypatch.setenv("DRY_TEST", "1")
    # trade_engine モジュールを毎テストで再読込して env 変数を反映する
    mods_to_reload = [
        "atlas_v3.core.trade_engine",
    ]
    for mod in mods_to_reload:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    yield


def _fresh_engine(**kwargs):
    """テスト用 TradeEngine を生成する（毎回 fresh import）。"""
    from atlas_v3.core.trade_engine import TradeEngine
    return TradeEngine(**kwargs)


# ===========================================================================
# T-01 〜 T-10: TradeEngine 基本 / DRY_TEST モード
# ===========================================================================

class TestTradeEngineInit:
    def test_01_default_init(self):
        """T-01: デフォルト引数で初期化できる。"""
        te = _fresh_engine()
        assert te.paper is False
        assert te.account_id is None
        assert te.unlock_ok is False

    def test_02_paper_mode(self):
        """T-02: paper=True で初期化。"""
        te = _fresh_engine(paper=True)
        assert te.paper is True

    def test_03_connect_returns_false_without_futu(self, monkeypatch):
        """T-03: FUTU_AVAILABLE=False にパッチ → connect() は False を返す。"""
        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", False)
        from atlas_v3.core.trade_engine import TradeEngine
        te = TradeEngine(paper=False)
        result = te.connect()
        assert result is False

    def test_04_close_no_error_without_ctx(self):
        """T-04: trade_ctx が None のまま close() しても例外なし。"""
        te = _fresh_engine()
        te.close()  # 例外なし

    def test_05_is_alive_false_without_futu(self, monkeypatch):
        """T-05: FUTU_AVAILABLE=False にパッチ → is_alive() は False。"""
        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", False)
        from atlas_v3.core.trade_engine import TradeEngine
        te = TradeEngine(paper=False)
        assert te.is_alive() is False

    def test_06_get_account_cash_dry_test(self):
        """T-06: DRY_TEST=1 → get_account_cash() は 10000.0 を返す。"""
        te = _fresh_engine()
        cash = te.get_account_cash()
        assert cash == 10000.0

    def test_07_get_open_positions_dry_test_empty(self):
        """T-07: DRY_TEST=1 初期状態 → get_open_positions() は空リスト。"""
        te = _fresh_engine()
        assert te.get_open_positions() == []

    def test_08_get_margin_ratio_dry_test(self):
        """T-08: DRY_TEST=1 → get_margin_usage_ratio() は 0.0。"""
        te = _fresh_engine()
        ratio = te.get_margin_usage_ratio()
        assert ratio == 0.0

    def test_09_check_margin_and_alert_ok(self):
        """T-09: margin 0.0 → check_margin_and_alert() は True。"""
        te = _fresh_engine()
        assert te.check_margin_and_alert() is True

    def test_10_cancel_all_dry_test_returns_zero(self):
        """T-10: DRY_TEST=1 → cancel_all_open_orders() は 0 を返す。"""
        te = _fresh_engine()
        assert te.cancel_all_open_orders("test") == 0


# ===========================================================================
# T-11 〜 T-20: place_credit_spread DRY_TEST
# ===========================================================================

class TestPlaceCreditSpreadDryTest:
    def test_11_pcs_dry_test_returns_true(self):
        """T-11: DRY_TEST=1 で place_credit_spread は True を返す。"""
        te = _fresh_engine()
        ok = te.place_credit_spread(
            sell_code="US.SPYW260502P00560000",
            buy_code="US.SPYW260502P00555000",
            qty=1,
            direction="PUT",
        )
        assert ok is True

    def test_12_pcs_dry_test_adds_virtual_positions(self):
        """T-12: place_credit_spread 後に virtual positions が 2 件追加される。"""
        te = _fresh_engine()
        te.place_credit_spread(
            sell_code="US.SPYW260502P00560000",
            buy_code="US.SPYW260502P00555000",
            qty=2,
            direction="PUT",
        )
        positions = te.get_open_positions()
        assert len(positions) == 2

    def test_13_pcs_dry_test_position_sides(self):
        """T-13: virtual positions に SHORT / LONG が設定される。"""
        te = _fresh_engine()
        te.place_credit_spread(
            sell_code="US.SPYW260502C00580000",
            buy_code="US.SPYW260502C00585000",
            qty=1,
            direction="CALL",
        )
        positions = te.get_open_positions()
        sides = {p["position_side"] for p in positions}
        assert "SHORT" in sides
        assert "LONG" in sides

    def test_14_pcs_signal_id_auto_generated(self):
        """T-14: signal_id=None でも place_credit_spread は成功する。"""
        te = _fresh_engine()
        ok = te.place_credit_spread(
            sell_code="US.SPYW260502P00560000",
            buy_code="US.SPYW260502P00555000",
            qty=1,
            direction="PUT",
            signal_id=None,
        )
        assert ok is True

    def test_15_pcs_with_explicit_signal_id(self):
        """T-15: 明示 signal_id を渡しても動作する。"""
        te = _fresh_engine()
        ok = te.place_credit_spread(
            sell_code="US.SPYW260502P00560000",
            buy_code="US.SPYW260502P00555000",
            qty=1,
            direction="PUT",
            signal_id="test_signal_20260425",
        )
        assert ok is True

    def test_16_pcs_kill_switch_blocks(self, tmp_path, monkeypatch):
        """T-16: Kill Switch ARMED → place_credit_spread は False を返す。"""
        state_dir = tmp_path / "state_v3"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("TRADING_STATE_DIR", str(state_dir))

        import common_v3.risk.kill_switch as ks
        monkeypatch.setattr(ks, "_STATE_DIR", state_dir)
        monkeypatch.setattr(ks, "FLAG_FILE", state_dir / "kill_switch.flag")
        monkeypatch.setattr(ks, "AUDIT_FILE", state_dir / "kill_switch_audit.jsonl")

        ks.activate(reason="test", activator="yuusaku")
        try:
            te = _fresh_engine()
            ok = te.place_credit_spread(
                sell_code="US.SPYW260502P00560000",
                buy_code="US.SPYW260502P00555000",
                qty=1,
                direction="PUT",
            )
            assert ok is False
        finally:
            ks.deactivate(activator="yuusaku", reason="test_teardown")

    def test_17_pcs_multiple_spreads_accumulate(self):
        """T-17: 複数回 place_credit_spread → virtual positions が累積する。"""
        te = _fresh_engine()
        te.place_credit_spread("US.SPYW260502P00560000", "US.SPYW260502P00555000", 1, "PUT")
        te.place_credit_spread("US.SPYW260502C00580000", "US.SPYW260502C00585000", 1, "CALL")
        positions = te.get_open_positions()
        assert len(positions) == 4

    def test_18_pcs_qty_propagates_to_virtual_pos(self):
        """T-18: qty が virtual position に反映される。"""
        te = _fresh_engine()
        te.place_credit_spread(
            "US.SPYW260502P00560000", "US.SPYW260502P00555000", 5, "PUT"
        )
        positions = te.get_open_positions()
        qtys = [p["qty"] for p in positions]
        assert all(q == 5 for q in qtys)


# ===========================================================================
# T-19 〜 T-25: close_all_positions DRY_TEST
# ===========================================================================

class TestCloseAllPositionsDryTest:
    def test_19_close_no_positions_returns_true(self):
        """T-19: ポジションなし → close_all_positions は True を返す。"""
        te = _fresh_engine()
        assert te.close_all_positions("test") is True

    def test_20_close_after_spread_clears_positions(self):
        """T-20: place_credit_spread 後に close_all_positions → positions 空になる。"""
        te = _fresh_engine()
        te.place_credit_spread("US.SPYW260502P00560000", "US.SPYW260502P00555000", 1, "PUT")
        assert len(te.get_open_positions()) == 2
        ok = te.close_all_positions("test_close")
        assert ok is True
        assert te.get_open_positions() == []

    def test_21_close_returns_true_on_success(self):
        """T-21: close_all_positions は成功時 True を返す。"""
        te = _fresh_engine()
        te.place_credit_spread("US.SPYW260502P00560000", "US.SPYW260502P00555000", 2, "PUT")
        result = te.close_all_positions()
        assert result is True

    def test_22_close_kill_switch_blocks(self, tmp_path, monkeypatch):
        """T-22: Kill Switch ARMED → close_all_positions は False を返す。"""
        state_dir = tmp_path / "state_v3_close"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("TRADING_STATE_DIR", str(state_dir))

        import common_v3.risk.kill_switch as ks
        monkeypatch.setattr(ks, "_STATE_DIR", state_dir)
        monkeypatch.setattr(ks, "FLAG_FILE", state_dir / "kill_switch.flag")
        monkeypatch.setattr(ks, "AUDIT_FILE", state_dir / "kill_switch_audit.jsonl")

        ks.activate(reason="test_close", activator="yuusaku")
        try:
            te = _fresh_engine()
            result = te.close_all_positions("force")
            assert result is False
        finally:
            ks.deactivate(activator="yuusaku", reason="teardown")

    def test_23_pending_close_initially_empty(self):
        """T-23: 初期状態で _pending_close は空リスト。close 後も空のまま。"""
        te = _fresh_engine()
        assert te._pending_close == []
        te.place_credit_spread("US.SPYW260502P00560000", "US.SPYW260502P00555000", 1, "PUT")
        result = te.close_all_positions("test")
        assert result is True
        # DRY_TEST パスでは _pending_close は手動設定しない限り空
        assert te._pending_close == []


# ===========================================================================
# T-24 〜 T-30: ヘルパー / BreakerConfig / PreTradeGate / schema
# ===========================================================================

class TestHelpers:
    def test_24_extract_symbol_spy(self):
        """T-24: futu コード "US.SPYW260502C00570000" → "SPY" を返す。"""
        from atlas_v3.core.trade_engine import _extract_symbol_from_code
        assert _extract_symbol_from_code("US.SPYW260502C00570000") == "SPY"

    def test_25_extract_symbol_qqq(self):
        """T-25: futu コード "US.QQQW260502P00450000" → "QQQ" を返す。"""
        from atlas_v3.core.trade_engine import _extract_symbol_from_code
        assert _extract_symbol_from_code("US.QQQW260502P00450000") == "QQQ"

    def test_26_extract_symbol_unknown_returns_input(self):
        """T-26: フォーマット不明コードはそのまま返す。"""
        from atlas_v3.core.trade_engine import _extract_symbol_from_code
        assert _extract_symbol_from_code("UNKNOWN") == "UNKNOWN"

    def test_27_extract_symbol_no_prefix(self):
        """T-27: プレフィックスなしでも先頭アルファ部分を返す。"""
        from atlas_v3.core.trade_engine import _extract_symbol_from_code
        result = _extract_symbol_from_code("NVDA20260502C00900000")
        assert result == "NVDA"

    def test_28_virtual_position_manager_add_get(self):
        """T-28: VirtualPositionManager add / get_positions 動作確認。"""
        from atlas_v3.core.trade_engine import _VirtualPositionManager
        vpm = _VirtualPositionManager()
        vpm.add_position("US.SPYW260502P00560000", 3, 0.50, "SHORT")
        pos = vpm.get_positions()
        assert len(pos) == 1
        assert pos[0]["code"] == "US.SPYW260502P00560000"
        assert pos[0]["qty"] == 3
        assert pos[0]["position_side"] == "SHORT"

    def test_29_virtual_position_manager_remove_all(self):
        """T-29: VirtualPositionManager remove_all 後は空リスト。"""
        from atlas_v3.core.trade_engine import _VirtualPositionManager
        vpm = _VirtualPositionManager()
        vpm.add_position("US.SPYW260502P00560000", 1, 0.50, "SHORT")
        vpm.remove_all()
        assert vpm.get_positions() == []

    def test_30_breaker_config_accepts_breaker(self):
        """T-30: BreakerConfig に任意の breaker を渡せる。"""
        from atlas_v3.core.trade_engine import BreakerConfig, TradeEngine
        from common_v3.self_healing.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(name="test_engine", fail_max=3)
        cfg = BreakerConfig(breaker=cb)
        te = TradeEngine(paper=True, breaker_config=cfg)
        assert te._breaker is cb

    def test_31_breaker_config_none_uses_shared_moomoo_breaker(self):
        """T-31: breaker_config=None → 共有 moomoo_breaker を使用する。"""
        from atlas_v3.core.trade_engine import TradeEngine
        from common_v3.self_healing.instances import moomoo_breaker
        te = TradeEngine(paper=False, breaker_config=None)
        assert te._breaker is moomoo_breaker

    def test_32_pre_trade_cfg_custom(self):
        """T-32: カスタム PreTradeConfig を渡せる。"""
        from atlas_v3.core.trade_engine import TradeEngine
        from common_v3.risk.pre_trade_check import PreTradeConfig
        cfg = PreTradeConfig(max_qty_per_order=50)
        te = TradeEngine(pre_trade_cfg=cfg)
        assert te._pre_trade_cfg.max_qty_per_order == 50

    def test_33_get_account_cash_raises_on_zero_capital_mock(self, monkeypatch):
        """T-33: futu ctx あり・net_assets=0・cash=0 → AccountCashError raise。"""
        from atlas_v3.core.trade_engine import TradeEngine, AccountCashError
        te = TradeEngine(paper=True)
        # 擬似的に futu が利用可能かつ ctx があるように見せる
        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", True)
        monkeypatch.setattr(te_mod, "DRY_TEST", False)

        mock_ctx = MagicMock()
        mock_row = {"net_assets": 0, "cash": 0}
        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.iloc = [mock_row]
        mock_ctx.accinfo_query.return_value = (0, mock_df)
        te.trade_ctx = mock_ctx
        te.account_id = "12345"
        te.trade_env = "SIMULATE"

        with pytest.raises(AccountCashError):
            te.get_account_cash()

    def test_34_place_credit_spread_dry_run_no_futu(self, monkeypatch):
        """T-34: DRY_TEST=0 かつ FUTU_AVAILABLE=False → dry-run ログのみで True。"""
        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "DRY_TEST", False)
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", False)
        from atlas_v3.core.trade_engine import TradeEngine
        te = TradeEngine(paper=True)
        ok = te.place_credit_spread(
            "US.SPYW260502P00560000", "US.SPYW260502P00555000", 1, "PUT"
        )
        assert ok is True

    def test_35_cancel_all_dry_run_no_futu(self, monkeypatch):
        """T-35: DRY_TEST=0 かつ FUTU_AVAILABLE=False → cancel 0 件。"""
        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "DRY_TEST", False)
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", False)
        from atlas_v3.core.trade_engine import TradeEngine
        te = TradeEngine(paper=True)
        assert te.cancel_all_open_orders("test") == 0


# ===========================================================================
# T-36 〜 T-42: CircuitBreaker / Kill Switch 統合
# ===========================================================================

class TestCircuitBreakerIntegration:
    def test_36_breaker_open_blocks_place_single_leg(self, monkeypatch):
        """T-36: moomoo_breaker OPEN 状態 → _place_single_leg は BrokerUnavailableError。"""
        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "DRY_TEST", False)
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", True)

        from atlas_v3.core.trade_engine import TradeEngine, BrokerUnavailableError
        from common_v3.self_healing.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(name="test_open_36", fail_max=1)
        # fail_max=1 で 1 回失敗させて OPEN にする
        try:
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("force open")))
        except (RuntimeError, Exception):
            pass  # CircuitBreaker に failure を記録させる

        # force OPEN: fail_max=1 なので 1 回 fail で OPEN になるはず
        # state が OPEN でなければ再試行
        if cb.state != "OPEN":
            pytest.skip("CircuitBreaker が OPEN にならなかった (実装依存)")

        from atlas_v3.core.trade_engine import BreakerConfig
        cfg = BreakerConfig(breaker=cb)

        te = TradeEngine(paper=True, breaker_config=cfg)
        te.trade_ctx = MagicMock()
        te.account_id = "12345"
        te.trade_env = "SIMULATE"

        # DRY_TEST=False FUTU_AVAILABLE=True 状態でダミー TrdSide を用意
        mock_side = MagicMock()
        mock_side.__eq__ = lambda self, other: False  # SELL でない

        with pytest.raises(BrokerUnavailableError):
            te._place_single_leg("US.SPYW260502P00560000", mock_side, 1, "test_leg")

    def test_37_kill_switch_blocks_cancel_all(self, tmp_path, monkeypatch):
        """T-37: Kill Switch ARMED → cancel_all_open_orders は 0 を返す。"""
        state_dir = tmp_path / "state_v3_cancel"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("TRADING_STATE_DIR", str(state_dir))

        import common_v3.risk.kill_switch as ks
        monkeypatch.setattr(ks, "_STATE_DIR", state_dir)
        monkeypatch.setattr(ks, "FLAG_FILE", state_dir / "kill_switch.flag")
        monkeypatch.setattr(ks, "AUDIT_FILE", state_dir / "kill_switch_audit.jsonl")

        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "DRY_TEST", False)
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", False)

        ks.activate(reason="test_cancel", activator="yuusaku")
        try:
            from atlas_v3.core.trade_engine import TradeEngine
            te = TradeEngine(paper=True)
            result = te.cancel_all_open_orders("sweep")
            assert result == 0
        finally:
            ks.deactivate(activator="yuusaku", reason="teardown")

    def test_38_kill_switch_blocks_place_single_leg(self, tmp_path, monkeypatch):
        """T-38: Kill Switch ARMED → _place_single_leg は (None, "failed") を返す。"""
        state_dir = tmp_path / "state_v3_psl"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("TRADING_STATE_DIR", str(state_dir))

        import common_v3.risk.kill_switch as ks
        monkeypatch.setattr(ks, "_STATE_DIR", state_dir)
        monkeypatch.setattr(ks, "FLAG_FILE", state_dir / "kill_switch.flag")
        monkeypatch.setattr(ks, "AUDIT_FILE", state_dir / "kill_switch_audit.jsonl")

        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "DRY_TEST", False)
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", False)

        ks.activate(reason="test_psl", activator="yuusaku")
        try:
            from atlas_v3.core.trade_engine import TradeEngine
            te = TradeEngine(paper=True)
            oid, method = te._place_single_leg(
                "US.SPYW260502P00560000", None, 1, "test"
            )
            assert oid is None
            assert method == "failed"
        finally:
            ks.deactivate(activator="yuusaku", reason="teardown")


# ===========================================================================
# T-39 〜 T-42: PreTradeGate 統合
# ===========================================================================

class TestPreTradeGateIntegration:
    def test_39_pre_trade_gate_zero_capital_blocks(self, monkeypatch):
        """T-39: capital_usd=0 (get_account_cash=0) → _place_single_leg は (None, "failed")。"""
        import atlas_v3.core.trade_engine as te_mod
        monkeypatch.setattr(te_mod, "DRY_TEST", False)
        monkeypatch.setattr(te_mod, "FUTU_AVAILABLE", True)

        from atlas_v3.core.trade_engine import TradeEngine
        te = TradeEngine(paper=True)

        # get_account_cash をゼロ返しにモック
        monkeypatch.setattr(te, "get_account_cash", lambda: 0.0)

        mock_side = MagicMock()
        mock_side.__eq__ = lambda self, other: False

        oid, method = te._place_single_leg(
            "US.SPYW260502P00560000", mock_side, 1, "test_zero_capital"
        )
        assert oid is None
        assert method == "failed"

    def test_40_build_pre_trade_ctx_symbol_prefix(self):
        """T-40: _build_pre_trade_ctx が symbol に "US." プレフィックスを付与する。"""
        te = _fresh_engine()
        ctx = te._build_pre_trade_ctx(
            code="US.SPYW260502P00560000",
            qty=1,
            price=1.5,
            side_str="SELL",
            capital_usd=10000.0,
            est_margin=150.0,
            open_margin_total=0.0,
            is_long=False,
        )
        assert ctx.symbol.startswith("US.")
        assert ctx.qty == 1
        assert ctx.side == "SELL"

    def test_41_run_pre_trade_gate_blocked_by_whitelist(self):
        """T-41: 未登録銘柄 → _run_pre_trade_gate は (False, ...) を返す。"""
        from common_v3.risk.pre_trade_check import PreTradeConfig
        # whitelist から "UNKNOWN" を除いた設定
        cfg = PreTradeConfig(
            symbol_whitelist=frozenset(["US.SPY"]),
        )
        te = _fresh_engine(pre_trade_cfg=cfg)
        allowed, reason = te._run_pre_trade_gate(
            code="US.UNKNOWN123456789",
            qty=1,
            price=0.5,
            side_str="SELL",
            capital_usd=10000.0,
            est_margin=50.0,
            open_margin_total=0.0,
            is_long=False,
            label="test_whitelist",
        )
        assert allowed is False
        assert "whitelist" in reason.lower() or "L2" in reason

    def test_42_run_pre_trade_gate_pass(self):
        """T-42: 正常な発注コンテキスト (whitelist 内シンボル) → _run_pre_trade_gate は (True, "") を返す。

        futu 週次 option コード "US.SPYW..." → symbol="SPY" → whitelist "US.SPY" にマッチ。
        """
        te = _fresh_engine()
        # SPY 週次オプション: _extract_symbol_from_code が "W" suffix を除去 → "US.SPY" にマッチ
        allowed, reason = te._run_pre_trade_gate(
            code="US.SPYW260502P00560000",
            qty=1,
            price=0.5,
            side_str="SELL",
            capital_usd=10000.0,
            est_margin=50.0,
            open_margin_total=0.0,
            is_long=False,
            label="test_pass",
        )
        assert allowed is True, f"PreTradeGate ブロック: {reason}"
        assert reason == ""
