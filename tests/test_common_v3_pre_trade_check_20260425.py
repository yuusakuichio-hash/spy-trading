"""tests/test_common_v3_pre_trade_check_20260425.py

common_v3/risk/pre_trade_check.py — 4-Layer Pre-Trade Gate テスト
4/17 事故 (SPXW 5400C 裸 LONG $169K) 再現阻止の回帰テスト込み。

カバー範囲:
  Layer 1: Deep ITM 裸 LONG $50+ ブロック / $50 境界 / is_long=False スキップ
  Layer 2: Symbol whitelist 違反 / 許可銘柄 PASS
  Layer 3: 単一発注 margin 80%+ ブロック / 合計 margin 超過 / capital=0 スキップ
  Layer 4: qty=0 / qty 負 / qty 超過 / 非 int / 正常
  Kill Switch: ARMED → 即ブロック / import 失敗 → fail-closed
  config: deep_itm_threshold 変更 / whitelist 変更 / margin_pct 変更
  PASS: 全 layer 通過
"""
from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common_v3.risk.pre_trade_check import (
    GateResult,
    OrderCtx,
    PreTradeConfig,
    _check_layer1_deep_itm,
    _check_layer2_whitelist,
    _check_layer3_margin,
    _check_layer4_qty,
    check_order,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_WHITELIST_SYMBOL = "US.SPY"
_NONWHITELIST_SYMBOL = "US.UNKNOWN_XYZ"


def _ctx(**kw) -> OrderCtx:
    """正常系 OrderCtx のデフォルト (Layer 全通過基準)

    2026-04-25: β-1 採択 (commit 6e4acd2) で fail-open → fail-closed 化された後、
    est_margin=0.0 / capital_usd=0.0 / option_price=0.0 はいずれも fail-closed reject
    対象になったため、正常系 default は値を入れる。skip 系 test (test_skip_when_*) は
    意図的に 0 を override し、新仕様で reject されることを assert する。
    """
    defaults = dict(
        symbol=_WHITELIST_SYMBOL,
        qty=1,
        option_price=10.0,
        side="SELL",
        is_long=False,
        est_margin=100.0,
        capital_usd=10000.0,
        open_margin_total=0.0,
    )
    defaults.update(kw)
    return OrderCtx(**defaults)


def _cfg(**kw) -> PreTradeConfig:
    """デフォルト PreTradeConfig"""
    return PreTradeConfig(**kw)


# ---------------------------------------------------------------------------
# fixture: Kill Switch を確実に非アクティブに保つ
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _deactivate_kill_switch(tmp_path, monkeypatch):
    """各テスト前に kill_switch を deactivate する (state dir を tmp に向ける)。"""
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    # kill_switch モジュールを再ロードして tmp_path の FLAG_FILE を使わせる
    import importlib
    import common_v3.risk.kill_switch as ks
    importlib.reload(ks)
    ks.deactivate()
    yield
    ks.deactivate()
    importlib.reload(ks)


# ===========================================================================
# Layer 1: Deep ITM 裸 LONG ブロック
# ===========================================================================

class TestLayer1DeepITM:
    """Layer 1: option_price >= threshold → 裸 LONG 即ブロック"""

    def test_block_at_threshold_exact(self):
        """option_price == threshold は境界値でブロック"""
        ctx = _ctx(is_long=True, option_price=50.0, side="BUY")
        result = _check_layer1_deep_itm(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert result.layer == "L1"
        assert result.severity == "critical"

    def test_block_above_threshold(self):
        """option_price > threshold (4/17 事故相当 $169) はブロック"""
        ctx = _ctx(is_long=True, option_price=169.0, side="BUY")
        result = _check_layer1_deep_itm(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert "169" in result.reason

    def test_pass_below_threshold(self):
        """option_price < threshold は通過"""
        ctx = _ctx(is_long=True, option_price=49.99, side="BUY")
        result = _check_layer1_deep_itm(ctx, _cfg())
        assert result is None

    def test_skip_when_not_long(self):
        """is_long=False (SELL 側) は option_price 問わずスキップ"""
        ctx = _ctx(is_long=False, option_price=200.0, side="SELL")
        result = _check_layer1_deep_itm(ctx, _cfg())
        assert result is None

    def test_skip_when_price_zero(self):
        """option_price=0.0 (不明) は β-1 採択後 fail-closed で reject (旧 skip 仕様廃止)"""
        ctx = _ctx(is_long=True, option_price=0.0, side="BUY")
        result = _check_layer1_deep_itm(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert result.layer == "L1"

    def test_custom_threshold(self):
        """custom threshold=$75 → $74.99 は通過、$75 はブロック"""
        cfg = _cfg(deep_itm_price_threshold=75.0)
        ctx_pass = _ctx(is_long=True, option_price=74.99, side="BUY")
        ctx_block = _ctx(is_long=True, option_price=75.0, side="BUY")
        assert _check_layer1_deep_itm(ctx_pass, cfg) is None
        assert _check_layer1_deep_itm(ctx_block, cfg) is not None

    def test_check_order_l1_integration(self):
        """check_order() 経由で L1 ブロックが発火する"""
        ctx = _ctx(is_long=True, option_price=100.0, side="BUY", symbol=_WHITELIST_SYMBOL, qty=1)
        result = check_order(ctx)
        assert not result.allowed
        assert result.layer == "L1"


# ===========================================================================
# Layer 2: Symbol Whitelist
# ===========================================================================

class TestLayer2Whitelist:
    """Layer 2: 未登録銘柄即拒否"""

    def test_block_unknown_symbol(self):
        """whitelist 外銘柄はブロック"""
        ctx = _ctx(symbol=_NONWHITELIST_SYMBOL)
        result = _check_layer2_whitelist(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert result.layer == "L2"
        assert result.severity == "high"

    def test_pass_known_symbol(self):
        """whitelist 内銘柄は通過"""
        for sym in ["US.SPY", "US.QQQ", "US.SPXW", "US.IWM"]:
            ctx = _ctx(symbol=sym)
            result = _check_layer2_whitelist(ctx, _cfg())
            assert result is None, f"{sym} should pass whitelist"

    def test_custom_whitelist(self):
        """custom whitelist: US.SPY 除外 → ブロック"""
        cfg = _cfg(symbol_whitelist=frozenset(["US.QQQ"]))
        ctx = _ctx(symbol="US.SPY")
        result = _check_layer2_whitelist(ctx, cfg)
        assert result is not None
        assert not result.allowed

    def test_whitelist_list_input_accepted(self):
        """whitelist を list で渡しても frozenset に変換される"""
        cfg = PreTradeConfig(symbol_whitelist=["US.SPY", "US.QQQ"])
        assert "US.SPY" in cfg.symbol_whitelist
        ctx = _ctx(symbol="US.SPY")
        result = _check_layer2_whitelist(ctx, cfg)
        assert result is None

    def test_check_order_l2_integration(self):
        """check_order() 経由で L2 ブロックが発火する"""
        ctx = _ctx(symbol="US.GARBAGE_COIN", qty=1, is_long=False, option_price=5.0)
        result = check_order(ctx)
        assert not result.allowed
        assert result.layer == "L2"


# ===========================================================================
# Layer 3: Margin% Cap
# ===========================================================================

class TestLayer3MarginCap:
    """Layer 3: 単一発注 + 合計保有 margin 上限"""

    def test_block_single_trade_margin_over_3pct(self):
        """est_margin / capital > 3% (デフォルト) → ブロック"""
        ctx = _ctx(
            symbol=_WHITELIST_SYMBOL,
            qty=1,
            is_long=False,
            est_margin=4000.0,   # 4% of 100K
            capital_usd=100_000.0,
        )
        result = _check_layer3_margin(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert result.layer == "L3"

    def test_block_total_margin_over_50pct(self):
        """(open + new) / capital > 50% → ブロック (単一発注は 3% 以内)"""
        # single = 2_000 / 100_000 = 2% → pass per-trade check
        # total  = (49_000 + 2_000) / 100_000 = 51% → fails total check
        ctx = _ctx(
            symbol=_WHITELIST_SYMBOL,
            qty=1,
            is_long=False,
            est_margin=2_000.0,
            capital_usd=100_000.0,
            open_margin_total=49_000.0,  # total 51% with new margin
        )
        result = _check_layer3_margin(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert "合計" in result.reason

    def test_pass_within_margin_limits(self):
        """margin が両閾値以内は通過"""
        ctx = _ctx(
            symbol=_WHITELIST_SYMBOL,
            qty=1,
            is_long=False,
            est_margin=2_000.0,   # 2% < 3%
            capital_usd=100_000.0,
            open_margin_total=10_000.0,  # total = 12% < 50%
        )
        result = _check_layer3_margin(ctx, _cfg())
        assert result is None

    def test_skip_when_capital_zero(self):
        """capital=0 は β-1 採択後 fail-closed で reject (旧 skip 仕様廃止)"""
        ctx = _ctx(est_margin=99999.0, capital_usd=0.0)
        result = _check_layer3_margin(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert result.layer == "L3"

    def test_skip_when_margin_zero(self):
        """est_margin=0 は β-1 採択後 fail-closed で reject (旧 skip 仕様廃止)"""
        ctx = _ctx(est_margin=0.0, capital_usd=100_000.0)
        result = _check_layer3_margin(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert result.layer == "L3"

    def test_check_order_l3_integration(self):
        """check_order() 経由で L3 ブロックが発火する (80% margin)"""
        ctx = _ctx(
            symbol=_WHITELIST_SYMBOL,
            qty=1,
            is_long=False,
            option_price=5.0,
            est_margin=80_000.0,
            capital_usd=100_000.0,
        )
        result = check_order(ctx)
        assert not result.allowed
        assert result.layer == "L3"


# ===========================================================================
# Layer 4: Fat Finger qty sanity
# ===========================================================================

class TestLayer4QtySanity:
    """Layer 4: qty が int かつ (0, max_qty_per_order] の範囲外をブロック"""

    def test_block_qty_zero(self):
        """qty=0 はブロック"""
        ctx = _ctx(qty=0)
        result = _check_layer4_qty(ctx, _cfg())
        assert result is not None
        assert not result.allowed
        assert result.layer == "L4"
        assert result.severity == "critical"

    def test_block_qty_negative(self):
        """qty=-1 はブロック"""
        ctx = _ctx(qty=-1)
        result = _check_layer4_qty(ctx, _cfg())
        assert result is not None
        assert not result.allowed

    def test_block_qty_over_limit(self):
        """qty > max_qty_per_order (デフォルト 100) はブロック"""
        ctx = _ctx(qty=101)
        result = _check_layer4_qty(ctx, _cfg())
        assert result is not None
        assert not result.allowed

    def test_pass_qty_at_max(self):
        """qty == max_qty_per_order は通過"""
        ctx = _ctx(qty=100)
        result = _check_layer4_qty(ctx, _cfg())
        assert result is None

    def test_block_qty_float(self):
        """qty が float (1.0) でも int でなければブロック"""
        ctx = _ctx(qty=1.0)  # type: ignore[arg-type]
        result = _check_layer4_qty(ctx, _cfg())
        assert result is not None
        assert not result.allowed

    def test_check_order_l4_integration(self):
        """check_order() 経由で L4 ブロックが発火する"""
        ctx = _ctx(qty=9999, is_long=False, option_price=5.0)
        result = check_order(ctx)
        assert not result.allowed
        assert result.layer == "L4"


# ===========================================================================
# Kill Switch
# ===========================================================================

class TestKillSwitch:
    """Kill Switch ARMED → 全発注ブロック"""

    def test_kill_switch_blocks_all_layers(self):
        """kill_switch ARMED のときは全 layer より優先してブロック"""
        import common_v3.risk.kill_switch as ks
        ks.activate("test_kill_switch_pre_trade")
        ctx = _ctx(symbol=_WHITELIST_SYMBOL, qty=1, is_long=False, option_price=5.0)
        result = check_order(ctx)
        assert not result.allowed
        assert result.layer == "KILL"
        assert result.severity == "critical"
        ks.deactivate()

    def test_kill_switch_import_fail_close(self, monkeypatch):
        """kill_switch import 失敗 → fail-closed (ブロック)"""
        import common_v3.risk.pre_trade_check as ptc
        original = ptc.check_order

        def _patched(ctx, config=None):
            # モジュール内の is_active を強制的に例外化
            import unittest.mock as mock
            with mock.patch(
                "common_v3.risk.kill_switch.is_active",
                side_effect=ImportError("mocked import failure"),
            ):
                return original(ctx, config)

        ctx = _ctx(symbol=_WHITELIST_SYMBOL, qty=1)
        result = _patched(ctx)
        assert not result.allowed
        assert result.layer == "KILL"


# ===========================================================================
# PreTradeConfig バリデーション
# ===========================================================================

class TestPreTradeConfigValidation:
    """PreTradeConfig の __post_init__ バリデーション"""

    def test_invalid_deep_itm_zero(self):
        with pytest.raises(ValueError, match="deep_itm_price_threshold"):
            PreTradeConfig(deep_itm_price_threshold=0.0)

    def test_invalid_margin_pct_over_one(self):
        with pytest.raises(ValueError, match="max_margin_pct_per_trade"):
            PreTradeConfig(max_margin_pct_per_trade=1.1)

    def test_invalid_max_qty_zero(self):
        with pytest.raises(ValueError, match="max_qty_per_order"):
            PreTradeConfig(max_qty_per_order=0)

    def test_valid_default_config(self):
        cfg = PreTradeConfig()
        assert cfg.deep_itm_price_threshold == 50.0
        assert "US.SPY" in cfg.symbol_whitelist
        assert cfg.max_margin_pct_per_trade == 0.03
        assert cfg.max_margin_pct_total == 0.50
        assert cfg.max_qty_per_order == 100


# ===========================================================================
# PASS: 全 layer 通過
# ===========================================================================

class TestPassAllLayers:
    """全 4 layer を通過する正常系テスト"""

    def test_pass_sell_side_normal(self):
        """SELL・whitelist・margin 0・qty 1 → PASS"""
        ctx = _ctx(
            symbol="US.QQQ",
            qty=5,
            side="SELL",
            is_long=False,
            option_price=8.0,
        )
        result = check_order(ctx)
        assert result.allowed
        assert result.layer == "PASS"
        assert result.reason == ""
        assert result.severity == "low"

    def test_pass_buy_below_threshold(self):
        """BUY・price $49 < $50 threshold → PASS"""
        ctx = _ctx(
            symbol="US.TSLA",
            qty=2,
            side="BUY",
            is_long=True,
            option_price=49.0,
        )
        result = check_order(ctx)
        assert result.allowed
        assert result.layer == "PASS"

    def test_pass_with_margin_info_within_limits(self):
        """margin 情報あり・両閾値以内 → PASS"""
        ctx = _ctx(
            symbol="US.SPXW",
            qty=10,
            side="SELL",
            is_long=False,
            option_price=5.0,
            est_margin=2_000.0,
            capital_usd=100_000.0,
            open_margin_total=10_000.0,
        )
        result = check_order(ctx)
        assert result.allowed

    def test_deepcopy_does_not_mutate_caller_ctx(self):
        """check_order は ctx を破壊的に変更しない (deepcopy 規律)"""
        ctx = _ctx(symbol="US.SPY", qty=1, is_long=False)
        original_qty = ctx.qty
        check_order(ctx)
        assert ctx.qty == original_qty

    def test_pass_multiple_symbols(self):
        """whitelist の全銘柄が PASS する"""
        for sym in [
            "US.SPY", "US.QQQ", "US.META", "US.SPXW", "US.SPX",
            "US.TSLA", "US.NVDA", "US.AAPL", "US.MSFT",
            "US.AMZN", "US.GOOGL", "US.IWM",
        ]:
            ctx = _ctx(symbol=sym, qty=1, is_long=False, option_price=10.0)
            result = check_order(ctx)
            assert result.allowed, f"{sym} should PASS but got layer={result.layer} reason={result.reason}"
