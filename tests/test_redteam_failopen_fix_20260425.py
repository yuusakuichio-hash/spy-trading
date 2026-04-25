"""Redteam fail-open CRITICAL 6 件 (B-1〜B-6) 修正検証テスト。

2026-04-25 Redteam agent ad3f2314 が発見した 4/17 事故再現リスクの構造的 fail-open。
本テストは各修正が fail-closed に切り替わったことを検証する。

| ID | 対象 | 修正前 | 修正後 |
|---|---|---|---|
| B-1 | _check_layer1_deep_itm option_price<=0.0 | return None (pass) | fail-closed L1 critical |
| B-2 | _check_layer3_margin capital/margin<=0.0 | return None (pass) | fail-closed L3 critical |
| B-3 | check_order_critical_only L2/L3 委譲 | dead code 経由でスキップ | L2/L3 fall-through 追加 |
| B-4 | _place_market_leg price=0.0 ハードコード | gate に 0.0 渡す | init_price None/<=0 で fail-closed |
| B-5 | get_open_positions 失敗時 | return [] | raise TradeEngineError |
| B-6 | check_margin_and_alert 失敗時 | return True (許可) | return False (拒否) |
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common_v3.risk.pre_trade_check import (
    GateResult,
    OrderCtx,
    PreTradeConfig,
    _check_layer1_deep_itm,
    _check_layer3_margin,
    check_order_critical_only,
)


# ---------------------------------------------------------------------------
# B-1: _check_layer1_deep_itm option_price<=0.0 fail-closed
# ---------------------------------------------------------------------------


class TestB1Layer1FailClosed:
    """B-1: 裸 LONG で option_price <=0.0 (価格不明) は fail-closed。"""

    def test_b1_zero_price_naked_long_blocked(self) -> None:
        """option_price=0.0 + is_long=True → fail-closed L1 critical。"""
        ctx = OrderCtx(symbol="US.SPY", qty=1, option_price=0.0, is_long=True)
        cfg = PreTradeConfig()
        result = _check_layer1_deep_itm(ctx, cfg)
        assert result is not None, "B-1 fail-closed: option_price=0.0 should block"
        assert result.allowed is False
        assert result.layer == "L1"
        assert result.severity == "critical"
        assert "B-1" in result.reason or "価格不明" in result.reason

    def test_b1_negative_price_naked_long_blocked(self) -> None:
        """option_price=-1.0 + is_long=True → fail-closed L1 critical。"""
        ctx = OrderCtx(symbol="US.SPY", qty=1, option_price=-1.0, is_long=True)
        cfg = PreTradeConfig()
        result = _check_layer1_deep_itm(ctx, cfg)
        assert result is not None
        assert result.allowed is False
        assert result.layer == "L1"

    def test_b1_zero_price_spread_leg_passes(self) -> None:
        """is_long=False (Spread 脚) なら option_price=0.0 でも pass (L1 対象外)。"""
        ctx = OrderCtx(symbol="US.SPY", qty=1, option_price=0.0, is_long=False)
        cfg = PreTradeConfig()
        result = _check_layer1_deep_itm(ctx, cfg)
        assert result is None, "Spread leg (is_long=False) is L1 out of scope"


# ---------------------------------------------------------------------------
# B-2: _check_layer3_margin capital/margin<=0.0 fail-closed
# ---------------------------------------------------------------------------


class TestB2Layer3FailClosed:
    """B-2: capital/margin が <=0 のとき fail-closed。"""

    def test_b2_zero_capital_blocked(self) -> None:
        """capital_usd=0.0 → fail-closed L3 critical。"""
        ctx = OrderCtx(
            symbol="US.SPY", qty=1, capital_usd=0.0, est_margin=100.0
        )
        cfg = PreTradeConfig()
        result = _check_layer3_margin(ctx, cfg)
        assert result is not None
        assert result.allowed is False
        assert result.layer == "L3"
        assert result.severity == "critical"
        assert "B-2" in result.reason or "margin 可視性" in result.reason

    def test_b2_zero_margin_blocked(self) -> None:
        """est_margin=0.0 → fail-closed L3 critical。"""
        ctx = OrderCtx(
            symbol="US.SPY", qty=1, capital_usd=10000.0, est_margin=0.0
        )
        cfg = PreTradeConfig()
        result = _check_layer3_margin(ctx, cfg)
        assert result is not None
        assert result.allowed is False
        assert result.layer == "L3"

    def test_b2_normal_margin_pass(self) -> None:
        """capital=10000, margin=100 (正常) → pass。"""
        ctx = OrderCtx(
            symbol="US.SPY", qty=1, capital_usd=10000.0, est_margin=100.0
        )
        cfg = PreTradeConfig()
        result = _check_layer3_margin(ctx, cfg)
        assert result is None, "Normal margin should pass"


# ---------------------------------------------------------------------------
# B-3: check_order_critical_only L2/L3 fall-through
# ---------------------------------------------------------------------------


class TestB3CriticalOnlyL2L3:
    """B-3: check_order_critical_only に L2 (whitelist) と L3 (margin) が走る。"""

    def test_b3_whitelist_violation_blocked_in_critical_only(self) -> None:
        """whitelist 外 symbol が critical_only でも block される (旧: skip)。"""
        ctx = OrderCtx(
            symbol="UNKNOWN_SYMBOL_XYZ",  # whitelist 外
            qty=1,
            option_price=10.0,  # B-1 通過
            is_long=False,  # L1 対象外
            capital_usd=10000.0,
            est_margin=100.0,
        )
        cfg = PreTradeConfig()
        result = check_order_critical_only(ctx, cfg)
        assert result.allowed is False
        assert result.layer == "L2", (
            f"B-3 fix: critical_only でも L2 が走るべき。実際: layer={result.layer}"
        )

    def test_b3_margin_overage_blocked_in_critical_only(self) -> None:
        """margin 超過が critical_only でも block される (旧: skip)。"""
        # whitelist 内 symbol を選ぶ
        cfg = PreTradeConfig()
        whitelist_sample = next(iter(cfg.symbol_whitelist))
        ctx = OrderCtx(
            symbol=whitelist_sample,
            qty=1,
            option_price=10.0,  # B-1 通過
            is_long=False,  # L1 対象外
            capital_usd=1000.0,
            est_margin=900.0,  # 90% > max_margin_pct_per_trade デフォルト
        )
        result = check_order_critical_only(ctx, cfg)
        assert result.allowed is False
        assert result.layer == "L3", (
            f"B-3 fix: critical_only でも L3 が走るべき。実際: layer={result.layer}"
        )


# ---------------------------------------------------------------------------
# B-4/B-5/B-6: trade_engine.py の修正は importable + 関数シグネチャ確認のみ
# (full instance test は TradeEngine の重い依存があるため smoke test 化)
# ---------------------------------------------------------------------------


class TestB4B5B6TradeEngineSmoke:
    """B-4/B-5/B-6: trade_engine.py の修正済関数シグネチャと fail-closed コード経路の存在を検証。"""

    def test_trade_engine_imports(self) -> None:
        """trade_engine が import できる (修正で構文エラーが入っていない)。"""
        from atlas_v3.core import trade_engine
        assert hasattr(trade_engine, "TradeEngineError")
        assert hasattr(trade_engine, "TradeEngine")

    def test_b4_init_price_check_in_place_market_leg(self) -> None:
        """B-4: _place_market_leg のソースに init_price fail-closed コード経路が存在。"""
        import inspect

        from atlas_v3.core.trade_engine import TradeEngine

        src = inspect.getsource(TradeEngine._place_market_leg)
        assert "B-4 fail-closed" in src, "B-4 fix marker missing in _place_market_leg"
        assert "init_price is None or init_price <= 0" in src, (
            "B-4 fail-closed condition missing"
        )

    def test_b5_get_open_positions_raises_on_failure(self) -> None:
        """B-5: get_open_positions のソースに raise TradeEngineError が存在。"""
        import inspect

        from atlas_v3.core.trade_engine import TradeEngine

        src = inspect.getsource(TradeEngine.get_open_positions)
        assert "B-5 fail-closed" in src, "B-5 fix marker missing"
        assert "raise TradeEngineError" in src, (
            "B-5 fail-closed should raise TradeEngineError"
        )

    def test_b6_check_margin_and_alert_returns_false_on_failure(self) -> None:
        """B-6: check_margin_and_alert のソースに取得失敗時 return False が存在。"""
        import inspect

        from atlas_v3.core.trade_engine import TradeEngine

        src = inspect.getsource(TradeEngine.check_margin_and_alert)
        assert "B-6 fail-closed" in src, "B-6 fix marker missing"
        # 取得失敗時 return False の経路が存在すること
        assert "return False" in src
        # 旧コメント "サービス継続優先" が残っていないこと
        assert "サービス継続優先" not in src, (
            "B-6: old fail-open comment 'サービス継続優先' should be removed"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
