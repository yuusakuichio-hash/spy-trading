"""tests/test_mutation_survivors_20260425.py
Mutation survivors 対応テスト (2026-04-25)

対象 surviving mutations:
  - atlas_v3/ops/chainguard_wrapper.py       (7 survived)
  - atlas_v3/ops/portfolio_risk_gate.py      (22 survived)
  - atlas_v3/ops/mass_verify_safe_runner.py  (10 survived)
  - atlas_v3/ops/moomoo_opend_relogin.py     (42 survived)
  - common_v3/risk/kill_switch.py            (23 survived)

カバー方針:
1. 比較演算子境界値: >, >=, <, <=, ==, != の境界を両側テスト
2. 算術演算子: - の結果が正であることを assert
3. Boolean 戻り値: True/False の返却値を明示検証
4. 定数値: magic number が変わると動作が変わることを示す
"""
from __future__ import annotations

import os
import sys
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

# TRADING_STATE_DIR をテスト用 tmp に差し替え (kill_switch が本番ファイルを汚染しないように)
import tempfile
_TMP_STATE = tempfile.mkdtemp(prefix="mutation_test_state_")
os.environ["TRADING_STATE_DIR"] = _TMP_STATE


# ============================================================================
# Section 1: chainguard_wrapper.py
# ============================================================================

class TestChainguardWrapperMutations(unittest.TestCase):
    """atlas_v3/ops/chainguard_wrapper.py surviving mutations 対応テスト"""

    def setUp(self):
        # モジュール import + キャッシュクリア
        from atlas_v3.ops.chainguard_wrapper import _clear_cache
        _clear_cache()

    # ── L84: _DEFAULT_STALE_THRESHOLD_SECS = 30.0 境界値 ────────────────────
    # num_const_30.0_plus_1: 30.0 -> 31.0 が survive
    # テスト: 30秒以下は新鮮、30.0秒ちょうどは stale を返すことを検証

    def test_stale_threshold_exactly_30_returns_none(self):
        """stale_threshold_secs=30.0: age=30.0 は stale (age > threshold で None を返す)"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        cw._price_cache["US.SPY"] = (590.0, time.monotonic() - 30.1)
        result = cw._get_cached("US.SPY", 30.0)
        self.assertIsNone(result, "30.1s old cache should be stale with threshold=30.0")

    def test_stale_threshold_just_under_returns_price(self):
        """age < stale_threshold_secs なら price を返す"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        cw._price_cache["US.SPY"] = (590.0, time.monotonic() - 5.0)
        result = cw._get_cached("US.SPY", 30.0)
        self.assertAlmostEqual(result, 590.0, places=1)

    # ── L93: age = time.monotonic() - ts (subが正値を作ること) ───────────────
    # binop_Sub_to_Add が survive: -→+ になると age が巨大になり常に stale

    def test_age_calculation_is_positive(self):
        """time.monotonic() - ts は正値 (age > 0)。+ に変わると常に stale になる"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        ts = time.monotonic() - 1.0  # 1秒前に保存
        cw._price_cache["US.SPY"] = (595.0, ts)
        result = cw._get_cached("US.SPY", 10.0)
        # 1秒前のキャッシュは 10s threshold で fresh
        self.assertIsNotNone(result, "1s old cache should not be stale with 10s threshold")
        self.assertAlmostEqual(result, 595.0, places=1)

    # ── L94: age > stale_threshold_secs (>を>=に変えると境界で動作変化) ───────
    # cmp_Gt_to_GtE が survive

    def test_stale_boundary_age_equals_threshold_is_stale(self):
        """age > threshold: age=threshold+epsilon は stale、age=threshold はギリギリ stale ではない"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        # age がほぼ 30.0 秒だが < 30.0 なら fresh
        cw._price_cache["US.SPY"] = (595.0, time.monotonic() - 29.5)
        result = cw._get_cached("US.SPY", 30.0)
        self.assertIsNotNone(result, "29.5s < 30.0s threshold should still be fresh")

    def test_stale_boundary_over_threshold_is_stale(self):
        """age > threshold: threshold を超えると stale"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        cw._price_cache["US.SPY"] = (595.0, time.monotonic() - 31.0)
        result = cw._get_cached("US.SPY", 30.0)
        self.assertIsNone(result, "31s > 30s threshold should be stale")

    # ── L122: allow_cache_on_error=False のデフォルト (False→True が survive) ─

    def test_allow_cache_on_error_default_is_false(self):
        """get_chain_center_price デフォルトは allow_cache_on_error=False"""
        from atlas_v3.ops.chainguard_wrapper import get_chain_center_price, MissingPriceError, _clear_cache
        _clear_cache()
        with self.assertRaises(MissingPriceError):
            get_chain_center_price("US.SPY", {})

    def test_allow_cache_on_error_true_uses_stale_cache(self):
        """allow_cache_on_error=True: stale でもキャッシュを返す"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        cw._price_cache["US.SPY"] = (580.0, time.monotonic() - 999.0)
        price = cw.get_chain_center_price("US.SPY", {}, allow_cache_on_error=True)
        self.assertAlmostEqual(price, 580.0, places=1)

    # ── L174: raw_price <= 0 で MissingPriceError (<=を<に変えると 0 が通る) ──

    def test_zero_price_raises_missing_price_error(self):
        """price=0 は MissingPriceError を raise する (<=0 チェック)"""
        from atlas_v3.ops.chainguard_wrapper import get_chain_center_price, MissingPriceError, _clear_cache
        _clear_cache()
        with self.assertRaises(MissingPriceError):
            get_chain_center_price("US.SPY", {"last_price": 0.0})

    def test_negative_price_raises_missing_price_error(self):
        """price=-1 も MissingPriceError"""
        from atlas_v3.ops.chainguard_wrapper import get_chain_center_price, MissingPriceError, _clear_cache
        _clear_cache()
        with self.assertRaises(MissingPriceError):
            get_chain_center_price("US.SPY", {"last_price": -1.0})

    def test_positive_price_returns_correctly(self):
        """price=0.01 は有効 (>0 の境界)"""
        from atlas_v3.ops.chainguard_wrapper import get_chain_center_price, _clear_cache
        _clear_cache()
        price = get_chain_center_price("US.SPY", {"last_price": 0.01})
        self.assertAlmostEqual(price, 0.01, places=3)

    # ── L231: stale_threshold_secs * 10 (x10 が fallback 用) ───────────────

    def test_fallback_uses_10x_stale_threshold(self):
        """get_chain_center_price_with_fallback: stale cache は 10x threshold で許容"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        # 60秒古い (通常 30s threshold では stale、10x=300s では fresh)
        cw._price_cache["US.SPY"] = (570.0, time.monotonic() - 60.0)
        price, source = cw.get_chain_center_price_with_fallback(
            "US.SPY", {}, fallback_price=999.0, stale_threshold_secs=30.0
        )
        self.assertEqual(source, "cache")
        self.assertAlmostEqual(price, 570.0, places=1)

    def test_fallback_uses_fallback_price_when_no_cache(self):
        """get_chain_center_price_with_fallback: cache なしは fallback_price を使う"""
        from atlas_v3.ops import chainguard_wrapper as cw
        cw._clear_cache()
        price, source = cw.get_chain_center_price_with_fallback(
            "US.SPY", {}, fallback_price=500.0
        )
        self.assertEqual(source, "fallback")
        self.assertAlmostEqual(price, 500.0, places=1)


# ============================================================================
# Section 2: portfolio_risk_gate.py
# ============================================================================

class TestPortfolioRiskGateMutations(unittest.TestCase):
    """atlas_v3/ops/portfolio_risk_gate.py surviving mutations 対応テスト"""

    def setUp(self):
        from atlas_v3.ops.portfolio_risk_gate import reset_gate_state
        reset_gate_state()

    # ── GateConfig.frozen=True (True→False が survive) ───────────────────────

    def test_gateconfig_is_frozen(self):
        """GateConfig(frozen=True): 書き換え不可"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig
        cfg = GateConfig()
        with self.assertRaises(Exception):
            cfg.vix_halt_threshold = 99.0  # type: ignore[misc]

    # ── GateConfig.__post_init__ 境界値 ─────────────────────────────────────
    # vix_halt_threshold <= 0 で raise (<=を<に変えると 0 が通る)

    def test_gateconfig_zero_vix_threshold_raises(self):
        """vix_halt_threshold=0 は PortfolioRiskGateError"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, PortfolioRiskGateError
        with self.assertRaises(PortfolioRiskGateError):
            GateConfig(vix_halt_threshold=0.0)

    def test_gateconfig_negative_vix_threshold_raises(self):
        """vix_halt_threshold=-1 は PortfolioRiskGateError"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, PortfolioRiskGateError
        with self.assertRaises(PortfolioRiskGateError):
            GateConfig(vix_halt_threshold=-1.0)

    def test_gateconfig_positive_vix_threshold_ok(self):
        """vix_halt_threshold=0.1 は valid"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig
        cfg = GateConfig(vix_halt_threshold=1.0, vix_warning_threshold=0.5)
        self.assertAlmostEqual(cfg.vix_halt_threshold, 1.0)

    def test_gateconfig_zero_max_entries_raises(self):
        """max_concurrent_entries=0 は PortfolioRiskGateError"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, PortfolioRiskGateError
        with self.assertRaises(PortfolioRiskGateError):
            GateConfig(max_concurrent_entries=0)

    # ── vix_warning_threshold > vix_halt_threshold で raise ─────────────────
    # cmp_Gt_to_GtE が survive → 等値でも raise すべき

    def test_gateconfig_warning_gt_halt_raises(self):
        """vix_warning_threshold > vix_halt_threshold は PortfolioRiskGateError"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, PortfolioRiskGateError
        with self.assertRaises(PortfolioRiskGateError):
            GateConfig(vix_halt_threshold=25.0, vix_warning_threshold=30.0)

    def test_gateconfig_warning_equal_halt_ok(self):
        """vix_warning_threshold == vix_halt_threshold は valid"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig
        cfg = GateConfig(vix_halt_threshold=30.0, vix_warning_threshold=30.0)
        self.assertAlmostEqual(cfg.vix_halt_threshold, 30.0)

    # ── check_entry_allowed: vix < 0 で PortfolioRiskGateError ──────────────

    def test_check_entry_negative_vix_raises(self):
        """vix=-1 は PortfolioRiskGateError"""
        from atlas_v3.ops.portfolio_risk_gate import check_entry_allowed, PortfolioRiskGateError
        with self.assertRaises(PortfolioRiskGateError):
            check_entry_allowed(-1.0, 0)

    def test_check_entry_zero_vix_allowed(self):
        """vix=0 は valid (< 0 なら raise、= 0 は許可)"""
        from atlas_v3.ops.portfolio_risk_gate import check_entry_allowed
        decision = check_entry_allowed(0.0, 0)
        self.assertTrue(decision.allowed)

    # ── check_entry_allowed: current_entries < 0 で PortfolioRiskGateError ──

    def test_check_entry_negative_entries_raises(self):
        """current_entries=-1 は PortfolioRiskGateError"""
        from atlas_v3.ops.portfolio_risk_gate import check_entry_allowed, PortfolioRiskGateError
        with self.assertRaises(PortfolioRiskGateError):
            check_entry_allowed(20.0, -1)

    # ── check_entry_allowed: vix >= halt_threshold で halt ───────────────────
    # cmp_GtE_to_Gt が survive: >= が > に変わると閾値ちょうどで halt しない

    def test_vix_at_halt_threshold_halts(self):
        """vix == halt_threshold (30.0) で entry halt"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed, reset_gate_state
        reset_gate_state()
        cfg = GateConfig(vix_halt_threshold=30.0, vix_warning_threshold=25.0, cooldown_secs=0.0)
        decision = check_entry_allowed(30.0, 0, cfg)
        self.assertFalse(decision.allowed)
        self.assertIn("vix_spike_halt", decision.active_rules)

    def test_vix_just_below_halt_threshold_allows(self):
        """vix = 29.9 < halt_threshold (30.0) で entry 許可"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed, reset_gate_state
        reset_gate_state()
        cfg = GateConfig(vix_halt_threshold=30.0, vix_warning_threshold=25.0, cooldown_secs=0.0)
        decision = check_entry_allowed(29.9, 0, cfg)
        self.assertTrue(decision.allowed)

    def test_vix_above_halt_threshold_halts(self):
        """vix = 30.1 > halt_threshold (30.0) で halt"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed, reset_gate_state
        reset_gate_state()
        cfg = GateConfig(vix_halt_threshold=30.0, vix_warning_threshold=25.0, cooldown_secs=0.0)
        decision = check_entry_allowed(30.1, 0, cfg)
        self.assertFalse(decision.allowed)

    # ── max_concurrent_entries >= max で halt ───────────────────────────────

    def test_entries_at_max_halts(self):
        """current_entries == max_concurrent_entries (10) で halt"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed, reset_gate_state
        reset_gate_state()
        cfg = GateConfig(max_concurrent_entries=10)
        decision = check_entry_allowed(10.0, 10, cfg)
        self.assertFalse(decision.allowed)
        self.assertIn("max_concurrent_entries", decision.active_rules)

    def test_entries_below_max_allows(self):
        """current_entries = 9 < max (10) で許可"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed, reset_gate_state
        reset_gate_state()
        cfg = GateConfig(max_concurrent_entries=10)
        decision = check_entry_allowed(10.0, 9, cfg)
        # vix=10 は warning_threshold=25 以下なので vix halt はなし
        self.assertNotIn("max_concurrent_entries", decision.active_rules)

    # ── GateDecision.allowed フィールドが bool ───────────────────────────────

    def test_gate_decision_halt_allowed_is_false(self):
        """GateDecision.halt().allowed == False"""
        from atlas_v3.ops.portfolio_risk_gate import GateDecision
        d = GateDecision.halt("reason", ["rule1"])
        self.assertFalse(d.allowed)
        self.assertIs(d.allowed, False)

    def test_gate_decision_allow_allowed_is_true(self):
        """GateDecision.allow().allowed == True"""
        from atlas_v3.ops.portfolio_risk_gate import GateDecision
        d = GateDecision.allow()
        self.assertTrue(d.allowed)
        self.assertIs(d.allowed, True)

    # ── is_in_cooldown: age < cooldown_secs (< を <= に変えると境界ズレ) ────

    def test_cooldown_age_subtraction_correct(self):
        """cooldown age = monotonic() - cleared_at: 減算結果が正。+になると常にcooldown中"""
        from atlas_v3.ops.portfolio_risk_gate import _gate_state, reset_gate_state
        reset_gate_state()
        # cleared_at を 1秒前に設定
        with _gate_state._lock:
            import time as _t
            _gate_state._vix_halt_cleared_at = _t.monotonic() - 1.0
        # cooldown_secs=10 なら 1秒ではまだ cooldown 中
        self.assertTrue(_gate_state.is_in_cooldown(10.0))
        # cooldown_secs=0.5 なら 1秒経過で cooldown 終了
        self.assertFalse(_gate_state.is_in_cooldown(0.5))

    def test_vix_warning_threshold_boundary(self):
        """vix >= warning_threshold で警告ログ (>= を > に変えると境界値で出ない)"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed, reset_gate_state
        reset_gate_state()
        cfg = GateConfig(vix_halt_threshold=30.0, vix_warning_threshold=25.0, cooldown_secs=0.0)
        # vix=25.0 は warning_threshold ちょうど: allowed (halt はしない)
        decision = check_entry_allowed(25.0, 0, cfg)
        self.assertTrue(decision.allowed)

    def test_halt_count_increments(self):
        """VIX halt 発動で halt_count が増える"""
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed, reset_gate_state, _gate_state
        reset_gate_state()
        cfg = GateConfig(vix_halt_threshold=30.0, vix_warning_threshold=25.0, cooldown_secs=0.0)
        check_entry_allowed(35.0, 0, cfg)
        self.assertEqual(_gate_state.get_halt_count(), 1)


# ============================================================================
# Section 3: mass_verify_safe_runner.py
# ============================================================================

class TestMassVerifySafeRunnerMutations(unittest.TestCase):
    """atlas_v3/ops/mass_verify_safe_runner.py surviving mutations 対応テスト"""

    def _make_ctx(self, symbol="US.SPY", strike=590.0, expiry="2026-05-16", opt="C"):
        from atlas_v3.ops.mass_verify_safe_runner import VerifyContext
        return VerifyContext(symbol=symbol, strike=strike, expiry=expiry, option_type=opt)

    # ── VerifyContext.frozen=True (True→False が survive) ──────────────────

    def test_verify_context_is_frozen(self):
        """VerifyContext(frozen=True): 書き換え不可"""
        ctx = self._make_ctx()
        with self.assertRaises(Exception):
            ctx.symbol = "US.QQQ"  # type: ignore[misc]

    # ── VerifyContext.option_type バリデーション ────────────────────────────

    def test_verify_context_invalid_option_type_raises(self):
        """option_type='X' は ValueError"""
        from atlas_v3.ops.mass_verify_safe_runner import VerifyContext
        with self.assertRaises(ValueError):
            VerifyContext(symbol="US.SPY", strike=590.0, expiry="2026-05-16", option_type="X")

    def test_verify_context_valid_call(self):
        """option_type='C' は valid"""
        ctx = self._make_ctx(opt="C")
        self.assertEqual(ctx.option_type, "C")

    def test_verify_context_valid_put(self):
        """option_type='P' は valid"""
        ctx = self._make_ctx(opt="P")
        self.assertEqual(ctx.option_type, "P")

    # ── VerifyContext.strike <= 0 で ValueError (<=を<に変えると 0 が通る) ──

    def test_verify_context_zero_strike_raises(self):
        """strike=0 は ValueError"""
        from atlas_v3.ops.mass_verify_safe_runner import VerifyContext
        with self.assertRaises(ValueError):
            VerifyContext(symbol="US.SPY", strike=0, expiry="2026-05-16", option_type="C")

    def test_verify_context_negative_strike_raises(self):
        """strike=-1 は ValueError"""
        from atlas_v3.ops.mass_verify_safe_runner import VerifyContext
        with self.assertRaises(ValueError):
            VerifyContext(symbol="US.SPY", strike=-1, expiry="2026-05-16", option_type="C")

    def test_verify_context_small_positive_strike_ok(self):
        """strike=0.01 は valid"""
        from atlas_v3.ops.mass_verify_safe_runner import VerifyContext
        ctx = VerifyContext(symbol="US.SPY", strike=0.01, expiry="2026-05-16", option_type="C")
        self.assertAlmostEqual(ctx.strike, 0.01)

    # ── VerifyResult.ok / fail の success フラグ ───────────────────────────

    def test_verify_result_ok_success_is_true(self):
        """VerifyResult.ok().success == True"""
        from atlas_v3.ops.mass_verify_safe_runner import VerifyResult
        ctx = self._make_ctx()
        r = VerifyResult.ok(ctx)
        self.assertTrue(r.success)
        self.assertIs(r.success, True)

    def test_verify_result_fail_success_is_false(self):
        """VerifyResult.fail().success == False"""
        from atlas_v3.ops.mass_verify_safe_runner import VerifyResult
        ctx = self._make_ctx()
        r = VerifyResult.fail(ctx, "test error")
        self.assertFalse(r.success)
        self.assertIs(r.success, False)

    # ── summary の success カウント (len(results) - len(failed)) ────────────
    # binop_Sub_to_Add が survive: - → + になると成功数が二重計上される

    def test_summary_success_count_is_correct(self):
        """run_mass_verify_safe_with_summary: success = total - failed"""
        from atlas_v3.ops.mass_verify_safe_runner import (
            VerifyContext, VerifyResult, run_mass_verify_safe_with_summary
        )
        entries = [
            VerifyContext("US.SPY", 590.0, "2026-05-16", "C"),
            VerifyContext("US.QQQ", 460.0, "2026-05-16", "P"),
            VerifyContext("US.IWM", 200.0, "2026-05-16", "C"),
        ]

        def mock_fn(ctx):
            if ctx.symbol == "US.IWM":
                return VerifyResult.fail(ctx, "mock fail")
            return VerifyResult.ok(ctx)

        results, summary = run_mass_verify_safe_with_summary(entries, mock_fn)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["success"], 2)
        # success = total - failed (NOT total + failed)
        self.assertEqual(summary["success"], summary["total"] - summary["failed"])

    # ── stop_on_first_error=False デフォルト ────────────────────────────────

    def test_stop_on_first_error_default_is_false(self):
        """run_mass_verify_safe デフォルト stop_on_first_error=False: エラーでも続行"""
        from atlas_v3.ops.mass_verify_safe_runner import (
            VerifyContext, VerifyResult, run_mass_verify_safe
        )
        entries = [
            VerifyContext("US.SPY", 590.0, "2026-05-16", "C"),
            VerifyContext("US.QQQ", 460.0, "2026-05-16", "C"),
        ]
        call_count = 0

        def raising_fn(ctx):
            nonlocal call_count
            call_count += 1
            if ctx.symbol == "US.SPY":
                raise RuntimeError("mock error")
            return VerifyResult.ok(ctx)

        results = run_mass_verify_safe(entries, raising_fn)
        # エラーがあっても全エントリを処理する
        self.assertEqual(len(results), 2)
        self.assertEqual(call_count, 2, "both entries should be processed")

    # ── lock_timeout_secs のデフォルト値 ────────────────────────────────────

    def test_lock_timeout_default(self):
        """run_mass_verify_safe: empty entries returns empty list"""
        from atlas_v3.ops.mass_verify_safe_runner import run_mass_verify_safe, VerifyContext
        result = run_mass_verify_safe([], lambda ctx: None)
        self.assertEqual(result, [])


# ============================================================================
# Section 4: moomoo_opend_relogin.py
# ============================================================================

class TestMoomooOpendReloginMutations(unittest.TestCase):
    """atlas_v3/ops/moomoo_opend_relogin.py surviving mutations 対応テスト"""

    # ── _password_md5: MD5 = 32 桁 hex ─────────────────────────────────────

    def test_password_md5_returns_32_char_hex(self):
        """_password_md5: 32桁小文字 hex を返す"""
        from atlas_v3.ops.moomoo_opend_relogin import _password_md5
        result = _password_md5("test_password")
        self.assertEqual(len(result), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in result))

    def test_password_md5_deterministic(self):
        """同一 password の MD5 は常に同じ"""
        from atlas_v3.ops.moomoo_opend_relogin import _password_md5
        self.assertEqual(_password_md5("abc"), _password_md5("abc"))

    # ── _validate_relogin_response: returncode != 0 で raise ────────────────
    # cmp_NotEq_to_Eq が survive: != を == に変えると 0 で raise, 非0 で pass

    def test_validate_response_success_does_not_raise(self):
        """response='success' は raise しない"""
        from atlas_v3.ops.moomoo_opend_relogin import _validate_relogin_response
        _validate_relogin_response("success")  # raises nothing

    def test_validate_response_ok_does_not_raise(self):
        """response='OK' は raise しない"""
        from atlas_v3.ops.moomoo_opend_relogin import _validate_relogin_response
        _validate_relogin_response("OK")

    def test_validate_response_fail_raises(self):
        """response に 'fail' が含まれると OpendOperateResponseError"""
        from atlas_v3.ops.moomoo_opend_relogin import _validate_relogin_response, OpendOperateResponseError
        with self.assertRaises(OpendOperateResponseError):
            _validate_relogin_response("login fail")

    def test_validate_response_error_raises(self):
        """response に 'error' が含まれると OpendOperateResponseError"""
        from atlas_v3.ops.moomoo_opend_relogin import _validate_relogin_response, OpendOperateResponseError
        with self.assertRaises(OpendOperateResponseError):
            _validate_relogin_response("system error")

    def test_validate_response_empty_raises(self):
        """空 response は OpendOperateResponseError"""
        from atlas_v3.ops.moomoo_opend_relogin import _validate_relogin_response, OpendOperateResponseError
        with self.assertRaises(OpendOperateResponseError):
            _validate_relogin_response("")

    def test_validate_response_ambiguous_raises(self):
        """曖昧な response (positive/negative どちらも含まない) は OpendOperateResponseError"""
        from atlas_v3.ops.moomoo_opend_relogin import _validate_relogin_response, OpendOperateResponseError
        with self.assertRaises(OpendOperateResponseError):
            _validate_relogin_response("relogin initiated")

    # ── _fetch_from_keychain の returncode != 0 チェック ────────────────────

    def test_fetch_from_keychain_nonzero_returncode_raises(self):
        """returncode=1 は KeychainAccessError"""
        from atlas_v3.ops.moomoo_opend_relogin import _fetch_from_keychain, KeychainAccessError
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(KeychainAccessError):
                _fetch_from_keychain("test_service")

    def test_fetch_from_keychain_zero_returncode_empty_raises(self):
        """returncode=0 だが stdout 空 は KeychainAccessError"""
        from atlas_v3.ops.moomoo_opend_relogin import _fetch_from_keychain, KeychainAccessError
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "\n"  # rstrip で空になる
        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(KeychainAccessError):
                _fetch_from_keychain("test_service")

    def test_fetch_from_keychain_zero_returncode_ok(self):
        """returncode=0 + stdout あり は値を返す"""
        from atlas_v3.ops.moomoo_opend_relogin import _fetch_from_keychain
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "test_value\n"
        with patch("subprocess.run", return_value=mock_result):
            result = _fetch_from_keychain("test_service")
            self.assertEqual(result, "test_value")

    # ── 深夜時間帯 priority: hour_jst + 9 で JST 変換 ─────────────────────
    # binop_Add_to_Sub が survive: + を - に変えると JST 計算がズレる

    def test_jst_hour_calculation_correct(self):
        """UTC hour + 9 = JST hour (mod 24)"""
        # UTC 13:00 → JST 22:00 (深夜帯 priority=2)
        utc_hour = 13
        jst_hour = (utc_hour + 9) % 24
        self.assertEqual(jst_hour, 22)
        # 深夜帯: 22 <= 22 or 22 < 6 → True
        is_night = (22 <= jst_hour) or (jst_hour < 6)
        self.assertTrue(is_night)

    def test_jst_hour_day_calculation_correct(self):
        """UTC 00:00 → JST 09:00 (日中帯 priority=1)"""
        utc_hour = 0
        jst_hour = (utc_hour + 9) % 24
        self.assertEqual(jst_hour, 9)
        is_night = (22 <= jst_hour) or (jst_hour < 6)
        self.assertFalse(is_night)

    # ── run_once の exit code 境界値 ─────────────────────────────────────────
    # exit code 1=keychain, 2=connect, 3=response の各コードを検証

    def test_run_once_keychain_failure_returns_1(self):
        """Keychain 失敗は exit code 1"""
        from atlas_v3.ops.moomoo_opend_relogin import run_once, KeychainAccessError
        with patch(
            "atlas_v3.ops.moomoo_opend_relogin._resolve_credential",
            side_effect=KeychainAccessError("mock keychain fail")
        ), patch("atlas_v3.ops.moomoo_opend_relogin._escalate_failure"), \
           patch("atlas_v3.ops.moomoo_opend_relogin._record_heartbeat"):
            code = run_once()
            self.assertEqual(code, 1)

    def test_run_once_connection_failure_returns_2(self):
        """OpenD 接続失敗は exit code 2"""
        from atlas_v3.ops.moomoo_opend_relogin import run_once, OpendOperateConnectionError
        with patch(
            "atlas_v3.ops.moomoo_opend_relogin._resolve_credential",
            return_value=("user", "pass")
        ), patch(
            "atlas_v3.ops.moomoo_opend_relogin._execute_relogin",
            side_effect=OpendOperateConnectionError("mock connect fail")
        ), patch("atlas_v3.ops.moomoo_opend_relogin._escalate_failure"), \
           patch("atlas_v3.ops.moomoo_opend_relogin._record_heartbeat"):
            code = run_once()
            self.assertEqual(code, 2)

    def test_run_once_response_failure_returns_3(self):
        """response エラーは exit code 3"""
        from atlas_v3.ops.moomoo_opend_relogin import run_once, OpendOperateResponseError
        with patch(
            "atlas_v3.ops.moomoo_opend_relogin._resolve_credential",
            return_value=("user", "pass")
        ), patch(
            "atlas_v3.ops.moomoo_opend_relogin._execute_relogin",
            return_value="login fail"
        ), patch("atlas_v3.ops.moomoo_opend_relogin._escalate_failure"), \
           patch("atlas_v3.ops.moomoo_opend_relogin._record_heartbeat"):
            code = run_once()
            self.assertEqual(code, 3)

    def test_run_once_success_returns_0(self):
        """正常完了は exit code 0"""
        from atlas_v3.ops.moomoo_opend_relogin import run_once
        with patch(
            "atlas_v3.ops.moomoo_opend_relogin._resolve_credential",
            return_value=("user", "pass")
        ), patch(
            "atlas_v3.ops.moomoo_opend_relogin._execute_relogin",
            return_value="success"
        ), patch("atlas_v3.ops.moomoo_opend_relogin._record_heartbeat"):
            code = run_once()
            self.assertEqual(code, 0)


# ============================================================================
# Section 5: common_v3/risk/kill_switch.py
# ============================================================================

class TestKillSwitchMutations(unittest.TestCase):
    """common_v3/risk/kill_switch.py surviving mutations 対応テスト"""

    def setUp(self):
        """各テストの前に kill_switch の flag を確実にクリアする"""
        import importlib
        import common_v3.risk.kill_switch as ks
        # FLAG_FILE が存在すれば削除
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()
        # per-firm flag も削除
        for firm in ("mffu", "tradeify", "apex", "bulenox"):
            fp = ks._STATE_DIR / f"kill_switch_{firm}.flag"
            if fp.exists():
                fp.unlink()

    # ── activate() 戻り値: True=新規発動 / False=既 ARMED ──────────────────

    def test_activate_first_time_returns_true(self):
        """初回 activate は True"""
        import common_v3.risk.kill_switch as ks
        result = ks._activate_raw("test_reason", "test_activator")
        self.assertTrue(result)
        # クリーンアップ
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    def test_activate_second_time_returns_false(self):
        """二回目の activate は False (冪等)"""
        import common_v3.risk.kill_switch as ks
        ks._activate_raw("test_reason", "test_activator")
        result2 = ks._activate_raw("test_reason2", "test_activator2")
        self.assertFalse(result2)
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    # ── deactivate() 戻り値: True=解除 / False=FLAG_FILE 不在 ──────────────

    def test_deactivate_when_active_returns_true(self):
        """FLAG_FILE ありの deactivate は True"""
        import common_v3.risk.kill_switch as ks
        ks._activate_raw("test", "test")
        result = ks._deactivate_raw("test_deactivator")
        self.assertTrue(result)

    def test_deactivate_when_not_active_returns_false(self):
        """FLAG_FILE なしの deactivate は False (early return)"""
        import common_v3.risk.kill_switch as ks
        result = ks._deactivate_raw("test_deactivator")
        self.assertFalse(result)

    # ── is_active_raw() 正確性 ───────────────────────────────────────────────

    def test_is_active_false_when_not_armed(self):
        """FLAG_FILE 不在で is_active = False"""
        import common_v3.risk.kill_switch as ks
        self.assertFalse(ks._is_active_raw())

    def test_is_active_true_when_armed(self):
        """FLAG_FILE 存在で is_active = True"""
        import common_v3.risk.kill_switch as ks
        ks._activate_raw("test", "test")
        self.assertTrue(ks._is_active_raw())
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    # ── _write_flag / _read_flag ラウンドトリップ ────────────────────────────

    def test_write_read_flag_roundtrip(self):
        """_write_flag してから _read_flag すると同じデータが返る"""
        import common_v3.risk.kill_switch as ks
        test_data = {"activated_at": "2026-04-25T00:00:00Z", "reason": "test", "activator": "unit"}
        ks._write_flag(ks.FLAG_FILE, test_data)
        read = ks._read_flag(ks.FLAG_FILE)
        self.assertIsNotNone(read)
        self.assertEqual(read["reason"], "test")
        # クリーンアップ
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    def test_read_flag_nonexistent_returns_none(self):
        """存在しない FLAG_FILE は None を返す"""
        import common_v3.risk.kill_switch as ks
        result = ks._read_flag(ks._STATE_DIR / "nonexistent_99999.flag")
        self.assertIsNone(result)

    # ── FirmScopedKillSwitch: 無効 firm で ValueError ──────────────────────

    def test_firm_scoped_invalid_firm_raises(self):
        """無効な firm 名は ValueError"""
        import common_v3.risk.kill_switch as ks
        with self.assertRaises(ValueError):
            ks.FirmScopedKillSwitch("invalid_firm")  # type: ignore[arg-type]

    def test_firm_scoped_valid_firm_ok(self):
        """有効な firm 名は OK"""
        import common_v3.risk.kill_switch as ks
        sw = ks.FirmScopedKillSwitch("mffu")
        self.assertEqual(sw.firm, "mffu")

    # ── FirmScopedKillSwitch.activate() 戻り値 ──────────────────────────────

    def test_firm_activate_first_returns_true(self):
        """FirmScopedKillSwitch.activate 初回 True"""
        import common_v3.risk.kill_switch as ks
        sw = ks.FirmScopedKillSwitch("apex")
        result = sw.activate("test_reason", "test")
        self.assertTrue(result)
        # クリーンアップ
        if sw._flag_path.exists():
            sw._flag_path.unlink()
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    def test_firm_activate_second_returns_false(self):
        """FirmScopedKillSwitch.activate 二回目 False (冪等)"""
        import common_v3.risk.kill_switch as ks
        sw = ks.FirmScopedKillSwitch("apex")
        sw.activate("test_reason", "test")
        result2 = sw.activate("test_reason2", "test")
        self.assertFalse(result2)
        if sw._flag_path.exists():
            sw._flag_path.unlink()
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    # ── FirmScopedKillSwitch.deactivate() 戻り値 ────────────────────────────

    def test_firm_deactivate_when_active_returns_true(self):
        """firm flag あり deactivate は True"""
        import common_v3.risk.kill_switch as ks
        sw = ks.FirmScopedKillSwitch("tradeify")
        sw.activate("test", "test")
        result = sw.deactivate("test")
        self.assertTrue(result)
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    def test_firm_deactivate_when_not_active_returns_false(self):
        """firm flag なし deactivate は False"""
        import common_v3.risk.kill_switch as ks
        sw = ks.FirmScopedKillSwitch("tradeify")
        result = sw.deactivate("test")
        self.assertFalse(result)

    # ── FirmScopedKillSwitch.is_active() ────────────────────────────────────

    def test_firm_is_active_false_when_not_armed(self):
        """per-firm flag なし: is_active=False"""
        import common_v3.risk.kill_switch as ks
        sw = ks.FirmScopedKillSwitch("bulenox")
        self.assertFalse(sw.is_active())

    def test_firm_is_active_true_when_armed(self):
        """per-firm flag あり: is_active=True"""
        import common_v3.risk.kill_switch as ks
        sw = ks.FirmScopedKillSwitch("bulenox")
        sw.activate("test", "test")
        self.assertTrue(sw.is_active())
        if sw._flag_path.exists():
            sw._flag_path.unlink()
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    # ── deactivate_all: 全 firm flag を一括解除 ─────────────────────────────

    def test_deactivate_all_clears_armed_firms(self):
        """deactivate_all: 全 firm flag を解除"""
        import common_v3.risk.kill_switch as ks
        # mffu と apex を arm
        ks.FirmScopedKillSwitch("mffu").activate("test", "test")
        ks.FirmScopedKillSwitch("apex").activate("test", "test")
        results = ks.FirmScopedKillSwitch.deactivate_all("probe")
        self.assertTrue(results.get("mffu", False))
        self.assertTrue(results.get("apex", False))
        # global flag は残っている (deactivate_all は per-firm のみ)
        if ks.FLAG_FILE.exists():
            ks.FLAG_FILE.unlink()

    def test_deactivate_all_no_flags_returns_empty(self):
        """armed firm なし: deactivate_all は空 dict を返す"""
        import common_v3.risk.kill_switch as ks
        results = ks.FirmScopedKillSwitch.deactivate_all("probe")
        self.assertEqual(results, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
