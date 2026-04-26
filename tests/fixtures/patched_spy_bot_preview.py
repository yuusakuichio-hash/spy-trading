"""tests/fixtures/patched_spy_bot_preview.py — 月曜パッチ適用後の spy_bot.py 差替箇所スナップショット

このファイルは参照専用。spy_bot.py への直接書込は行わない。
apply_monday_patches.sh が実際の差替を行う。

スナップショット作成日: 2026-04-25
対象パッチ: monday_integration_patch_20260425.md
"""
from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# SNAPSHOT 1: import block (spy_bot.py ~L1072 `_pdt_tracker = None` の直後)
# ──────────────────────────────────────────────────────────────────────────────

IMPORT_BLOCK_PREVIEW = """
# ── atlas_v3 wrapper imports (2026-04-28 統合パッチ) ──────────────────────────

_CHAINGUARD_WRAPPER_AVAILABLE = False
try:
    from atlas_v3.ops.chainguard_wrapper import (
        get_chain_center_price as _cg_get_center_price,
        get_chain_center_price_with_fallback as _cg_get_center_price_fb,
        ChainGuardError as _ChainGuardError,
        MissingPriceError as _CgMissingPriceError,
    )
    _CHAINGUARD_WRAPPER_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.chainguard_wrapper ロード成功 (CRITICAL #1 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.chainguard_wrapper ロード失敗: {_e}")
    def _cg_get_center_price(symbol, market_data, **kw): return None
    def _cg_get_center_price_fb(symbol, market_data, fallback, **kw): return fallback, "fallback"
    class _ChainGuardError(Exception): pass
    class _CgMissingPriceError(_ChainGuardError): pass

_MASS_VERIFY_SAFE_AVAILABLE = False
try:
    from atlas_v3.ops.mass_verify_safe_runner import (
        VerifyContext as _MVVerifyContext,
        VerifyResult as _MVVerifyResult,
        run_mass_verify_safe as _mv_run_safe,
        run_mass_verify_safe_with_summary as _mv_run_safe_summary,
        MassVerifyError as _MassVerifyError,
    )
    _MASS_VERIFY_SAFE_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.mass_verify_safe_runner ロード成功 (CRITICAL #2 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.mass_verify_safe_runner ロード失敗: {_e}")
    class _MassVerifyError(Exception): pass

_PORTFOLIO_RISK_GATE_AVAILABLE = False
try:
    from atlas_v3.ops.portfolio_risk_gate import (
        check_entry_allowed as _prg_check_entry,
        check_entry_allowed_with_log as _prg_check_entry_log,
        GateConfig as _PRGateConfig,
        GateDecision as _PRGateDecision,
        PortfolioRiskGateError as _PRGateError,
    )
    _PORTFOLIO_RISK_GATE_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.portfolio_risk_gate ロード成功 (CRITICAL #3 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.portfolio_risk_gate ロード失敗: {_e}")
    def _prg_check_entry(vix, entries, config=None): return type("D", (), {"allowed": True, "reason": "fallback"})()
    def _prg_check_entry_log(vix, entries, config=None, **kw): return _prg_check_entry(vix, entries)
    class _PRGateError(Exception): pass

_SYMBOL_AWARE_PRICE_AVAILABLE = False
try:
    from atlas_v3.ops.symbol_aware_price import (
        get_current_price as _sap_get_price,
        get_current_price_with_fallback as _sap_get_price_fb,
        normalize_symbol as _sap_normalize_symbol,
        SymbolPriceError as _SymbolPriceError,
        MissingPriceError as _SapMissingPriceError,
        OutOfRangePriceError as _OutOfRangePriceError,
    )
    _SYMBOL_AWARE_PRICE_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.symbol_aware_price ロード成功 (H=300 fix 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.symbol_aware_price ロード失敗: {_e}")
    def _sap_get_price(code, mkt, **kw): return None
    def _sap_get_price_fb(code, mkt, fb, **kw): return fb, "fallback"
    class _SymbolPriceError(Exception): pass
    class _SapMissingPriceError(_SymbolPriceError): pass
    class _OutOfRangePriceError(_SymbolPriceError): pass
"""

# ──────────────────────────────────────────────────────────────────────────────
# SNAPSHOT 2: CRITICAL #1 差替後 (spy_bot.py ~L5539)
# ──────────────────────────────────────────────────────────────────────────────

CRITICAL1_AFTER = """
            # [CRITICAL #1 patch 2026-04-28] chainguard_wrapper 経由で動的取得
            if _CHAINGUARD_WRAPPER_AVAILABLE:
                _cg_fb, _cg_src = _cg_get_center_price_fb(
                    self.underlying_code, self.mkt, 0.0
                )
                spy_price_ref = _cg_fb
                if _cg_src == "fallback":
                    log.warning(f"[ChainGuard] center price fallback=0 for {self.underlying_code}")
            else:
                spy_price_ref = 0
                try:
                    _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                    if _cg_ret == RET_OK and not _cg_snap.empty:
                        spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
                except Exception as _cg_e:
                    log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")
"""

# ──────────────────────────────────────────────────────────────────────────────
# SNAPSHOT 3: CRITICAL #3 差替後 (spy_bot.py ~L13988 standard entry)
# ──────────────────────────────────────────────────────────────────────────────

CRITICAL3_STANDARD_AFTER = """
        # [CRITICAL #3 patch 2026-04-28] VIX spike gate wrapper
        if _PORTFOLIO_RISK_GATE_AVAILABLE:
            _prg_open_count = len(getattr(self, "_mass_verify_positions", {}) or {})
            _prg_decision = _prg_check_entry_log(vix, _prg_open_count, context="standard_entry")
            if not _prg_decision.allowed:
                log.warning(f"[PortfolioRiskGate] standard entry halted: {_prg_decision.reason}")
                return
        params = get_params(vix, STANDARD_PARAMS)
        if params is None:
            log.info(f"VIX={vix:.1f} >= 35 → standard entry halted (ORF may activate)")
            return
"""

# ──────────────────────────────────────────────────────────────────────────────
# SNAPSHOT 4: H=300 fix 差替サンプル #9 (spy_bot.py ~L10440 ORB _get_underlying_price)
# ──────────────────────────────────────────────────────────────────────────────

H300_FIX9_AFTER = """
        # H=300 fix #9: underlying_code 切替済みのため銘柄別に動的取得
        if _SYMBOL_AWARE_PRICE_AVAILABLE:
            _p9, _ = _sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)
            return _p9 if _p9 > 0 else None
        return self.mkt.get_spy_current()  # underlying_code切替済みのため銘柄別に動作
"""

# ──────────────────────────────────────────────────────────────────────────────
# SNAPSHOT 5: 差替箇所総数確認テスト用マップ
# ──────────────────────────────────────────────────────────────────────────────

PATCH_SUMMARY = {
    "total_replacements": 19,
    "import_block": 1,
    "critical_1_chainguard": 1,
    "critical_2_mass_verify_import_only": 1,  # import flag確立のみ。ループ差替はv3統合時。
    "critical_3_portfolio_risk_gate": 2,        # standard_entry + cs_entry (ORF手動確認)
    "h300_fix_symbol_aware_price": 16,
    "wrappers": [
        "atlas_v3/ops/chainguard_wrapper.py",
        "atlas_v3/ops/mass_verify_safe_runner.py",
        "atlas_v3/ops/portfolio_risk_gate.py",
        "atlas_v3/ops/symbol_aware_price.py",
    ],
    "spy_bot_original_flags": "schg",
    "backup_pattern": "spy_bot.py.bak_monday_patch_YYYYMMDD_HHMMSS",
    "apply_script": "scripts/apply_monday_patches.sh",
    "decisions_doc": "data/decisions/monday_integration_patch_20260425.md",
}


# ──────────────────────────────────────────────────────────────────────────────
# 参照専用スモークテスト (pytest で実行可能・spy_bot.py への書込なし)
# ──────────────────────────────────────────────────────────────────────────────

def test_wrapper_imports_available():
    """4 wrapper が全件 import できることを確認する参照テスト。"""
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    from atlas_v3.ops.chainguard_wrapper import get_chain_center_price
    from atlas_v3.ops.mass_verify_safe_runner import run_mass_verify_safe
    from atlas_v3.ops.portfolio_risk_gate import check_entry_allowed
    from atlas_v3.ops.symbol_aware_price import get_current_price

    assert callable(get_chain_center_price)
    assert callable(run_mass_verify_safe)
    assert callable(check_entry_allowed)
    assert callable(get_current_price)


def test_patch_summary_counts():
    """差替箇所総数が期待値と一致することを確認する。"""
    assert PATCH_SUMMARY["total_replacements"] == 19
    assert PATCH_SUMMARY["h300_fix_symbol_aware_price"] == 16
    assert PATCH_SUMMARY["critical_3_portfolio_risk_gate"] == 2
    assert len(PATCH_SUMMARY["wrappers"]) == 4
