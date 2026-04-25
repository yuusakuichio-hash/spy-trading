"""tests/test_discipline_compliance_20260425.py — 規律違反検出 test (新規・自動回帰)

ゆうさくさん 2026-04-25 指摘「事前テストが足りてない」を受けて、各 memory 規律
が実装で守られているかを自動検出する test 群。

検出対象規律:
- feedback_no_fixed_params.md: 動的パラメータ化遵守
  → 各 engine が dynamic_params を import / 利用しているか
- feedback_implementation_process.md: 実装前 7 ステップ
  → 各 engine に preflight / should_enter / build_order を持つか
- legacy_write_block 規律: spy_bot.py 等への参照禁止
  → 新規 engine が spy_bot を import していないか
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


# ===========================================================================
# IVR フィルタを持つ engine の dynamic_params 利用検証
# ===========================================================================

class TestDynamicParamsCompliance:
    """各 engine が IVR 閾値判定で dynamic_params を使っているか検証.

    IVR 閾値判定を持つ engine が `self._cfg.ivr_min` のみを使い
    `get_dynamic_ivr_threshold` を経由していない場合、動的パラメータ化規律違反。
    """

    # IVR フィルタを持つ engine 一覧 (ivr_min config + env.ivr_by_symbol 参照あり)
    IVR_USING_ENGINES = [
        "iron_fly",
        "short_strangle_0dte",
        "diagonal_spread",
        "ratio_spread",
        "broken_wing_butterfly",
        "pmcc",
        "jade_lizard",
    ]

    @pytest.mark.parametrize("engine_name", IVR_USING_ENGINES)
    def test_engine_uses_dynamic_ivr_threshold(self, engine_name):
        """各 IVR-using engine が get_dynamic_ivr_threshold を import / 利用."""
        engine_path = Path(f"atlas_v3/bots/engines/{engine_name}.py")
        assert engine_path.exists(), f"{engine_path} 不在"
        text = engine_path.read_text()
        assert "get_dynamic_ivr_threshold" in text, (
            f"{engine_name}: get_dynamic_ivr_threshold を import / 利用していない "
            f"(動的パラメータ規律違反・feedback_no_fixed_params.md)"
        )


# ===========================================================================
# legacy 参照禁止 (spy_bot.py / chronos_bot.py への直接 import なし)
# ===========================================================================

class TestNoLegacyImports:
    """atlas_v3 配下の engine / ops が legacy spy_bot / chronos_bot を直接 import していない."""

    LEGACY_FORBIDDEN = ["spy_bot", "chronos_bot"]
    SCAN_DIRS = [
        "atlas_v3/bots/engines",
        "atlas_v3/ops",
        "atlas_v3/core",
        "common_v3",
    ]

    def _gather_atlas_v3_files(self) -> list[Path]:
        files = []
        for d in self.SCAN_DIRS:
            base = Path(d)
            if not base.exists():
                continue
            files.extend(base.rglob("*.py"))
        return [
            f for f in files
            if "__pycache__" not in str(f)
            and "archive" not in str(f)
            and not f.name.startswith("_")
        ]

    def test_no_direct_legacy_import(self):
        """atlas_v3 / common_v3 配下のファイルが spy_bot / chronos_bot を直接 import していない."""
        violations = []
        for f in self._gather_atlas_v3_files():
            text = f.read_text()
            for legacy in self.LEGACY_FORBIDDEN:
                if (
                    f"from {legacy}" in text
                    or f"import {legacy}" in text
                ):
                    violations.append(f"{f}: imports {legacy}")
        assert not violations, (
            f"legacy 直接 import 検出 (規律違反・既存コード書換禁止):\n"
            + "\n".join(violations)
        )


# ===========================================================================
# Engine が標準 interface を持つ (preflight / should_enter / build_order or place_order)
# ===========================================================================

class TestEngineInterface:
    """11 戦術 engine が標準 interface を実装している."""

    ENGINES_REQUIRED_IFACE = {
        "iron_fly": ["preflight", "should_enter", "place_order"],
        "weekly_gamma_scalp": ["preflight", "should_enter", "build_orders"],
        "short_strangle_0dte": ["preflight", "should_enter", "build_order"],
        "broken_wing_butterfly": ["preflight", "should_enter", "place_order"],
        "diagonal_spread": ["preflight", "should_enter", "build_order"],
        "earnings_straddle_buy": ["preflight", "should_enter", "build_order"],
        "jade_lizard": ["preflight", "should_enter", "build_orders"],
        "pmcc": ["preflight", "should_enter", "build_orders"],
        "ratio_spread": ["preflight", "should_enter", "place_order"],
        "vix_tail_hedge": ["preflight", "should_enter", "build_order"],
    }

    @pytest.mark.parametrize("engine_name,methods", list(ENGINES_REQUIRED_IFACE.items()))
    def test_engine_has_required_methods(self, engine_name, methods):
        """各 engine module ファイルに必須 method が定義されている."""
        engine_path = Path(f"atlas_v3/bots/engines/{engine_name}.py")
        assert engine_path.exists(), f"{engine_path} 不在"
        text = engine_path.read_text()
        for m in methods:
            assert f"def {m}" in text, (
                f"{engine_name}: required method '{m}' 未実装"
            )


# ===========================================================================
# 必須 module 存在検証 (β-2 配線完成度の自動 check)
# ===========================================================================

class TestRequiredModulesExist:
    """β-2 完成基準: 主要 module が存在し import 可能."""

    REQUIRED_MODULES = [
        "atlas_v3.core.engine",
        "atlas_v3.bots.engines.registry",
        "atlas_v3.bots.engines.dynamic_params",
        "atlas_v3.ops.vix_estimator",
        "atlas_v3.ops.realized_volatility",
        "atlas_v3.ops.gex_estimator",
        "atlas_v3.ops.market_data_adapter",
        "atlas_v3.broker.moomoo_paper",
        "atlas_v3.risk",
        "common_v3.order",
        "common_v3.auth",
        "common_v3.position",
    ]

    @pytest.mark.parametrize("module_name", REQUIRED_MODULES)
    def test_required_module_importable(self, module_name):
        """各 module が import 可能."""
        try:
            mod = importlib.import_module(module_name)
            assert mod is not None
        except ImportError as e:
            pytest.fail(f"{module_name}: import 失敗 ({e})")
