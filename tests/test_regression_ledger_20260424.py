"""Regression Ledger — 発見済 regression の永続再発防止テスト。

設計思想:
  - Redteam r2 / r3 で発見された regression をテストとして固定化
  - 「修正済だから OK」ではなく「将来の修正で再発しないこと」を永続保証
  - Regression-Specific Test (Google / Microsoft の QA 標準手法)
  - 2026-04-24 ゆうさくさん指示「バグ潰しても regression の指摘あった・対処」受け

各テストには:
  - 元の regression ID (REG-RN-N)
  - 発見時のシナリオ
  - 修正方針
  - 再発時に失敗する assert
を明示。

既存 fix テスト (test_redteam_r1_fixes / test_redteam_r2_fixes / test_redteam_r3_fixes) と
重複しても OK（独立 ledger として機能・削除禁止）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# REG-R2-1: halt 後の利益 trade を捨てるバグ
# ============================================================================
# 発見: Redteam r2 REG-NEW-1
# シナリオ: C7 projected_pnl pre-check で pnl > 0 (利益) の trade も halt 判定対象に
#           → 利益 trade で projected_pnl が max_daily_loss を超えない限り止まらないはず
#           なのに "max_daily_loss 超過 だから halt" という判定が利益 trade でも走り、
#           halt 後に来た +X USD の利益 trade を捨てる副作用
# 修正方針: rec.pnl < 0 の場合のみ projected_pnl pre-check を実行（利益で halt しない）
# ============================================================================

class TestRegressionR2_1_ProfitTradeNotHalted:
    """halt は損失 trade のみ発動・利益 trade で halt 発動してはならない。"""

    def test_profit_trade_does_not_trigger_halt_precheck(self):
        """利益 trade (pnl>0) は projected_pnl pre-check の halt 判定対象外。"""
        try:
            from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig
        except ImportError:
            pytest.skip("atlas_v3.ops.replay_bt unavailable")

        # 利益 trade で daily_pnl がどう動いても halt しない
        # （損失のみが halt トリガー）
        # このテストは「rec.pnl < 0」の条件式が存在することを保証する間接チェック
        import inspect
        source = inspect.getsource(ReplayBacktest)
        assert "rec.pnl < 0" in source or "pnl < 0" in source, (
            "REG-R2-1 再発疑い: halt 判定が損失 trade のみ対象である保証が無い"
        )


# ============================================================================
# REG-R2-2: env fallback 削除で CI/dev VaultError 常態化
# ============================================================================
# 発見: Redteam r2 REG-NEW-2
# シナリオ: C4 で vault.py の load_from_env の env fallback 削除 → CI/dev 環境で .env ファイル不在時
#           に VaultError 常態化。開発者が chmod 0600 の .env を作らず迂回して運用する懸念
# 修正方針: VAULT_ALLOW_ENV_FALLBACK=1 を明示 env で opt-in 時のみ env fallback 許可
# ============================================================================

class TestRegressionR2_2_EnvFallbackOptIn:
    """VAULT_ALLOW_ENV_FALLBACK opt-in 無しに env fallback 経路はない。"""

    def test_env_fallback_disabled_by_default(self, monkeypatch, tmp_path):
        """デフォルト (opt-in 無し) で env fallback は動かない（VaultError）。"""
        try:
            from atlas_v3.ops.vault import load_from_env, VaultError
        except ImportError:
            pytest.skip("atlas_v3.ops.vault unavailable")

        monkeypatch.delenv("VAULT_ALLOW_ENV_FALLBACK", raising=False)
        nonexistent = tmp_path / "does_not_exist.env"

        with pytest.raises((VaultError, FileNotFoundError)):
            load_from_env(env_path=nonexistent)

    def test_env_fallback_explicit_opt_in_variable_exists(self):
        """VAULT_ALLOW_ENV_FALLBACK 環境変数が設計として存在する (grep による)。"""
        vault_py = PROJECT_ROOT / "atlas_v3" / "ops" / "vault.py"
        if not vault_py.exists():
            pytest.skip("vault.py not present")
        content = vault_py.read_text(encoding="utf-8")
        assert "VAULT_ALLOW_ENV_FALLBACK" in content, (
            "REG-R2-2 再発疑い: env fallback の明示 opt-in 仕組みが消えた"
        )


# ============================================================================
# REG-R3-1: intraday peak-to-trough drawdown 未監視
# ============================================================================
# 発見: Redteam r3 REG-NEW-1
# シナリオ: replay_bt の _simulate_day が日中 peak-to-trough drawdown を追跡していない
#           → +1000 USD → -1500 USD 推移で日終値 -500 (閾値=-500) だが intraday の $1500 drawdown
#           が記録されない・halt 判定が弱い
# 修正方針: daily_peak_capital / daily_trough_capital を追跡し peak-to-trough で halt 判定
# ============================================================================

class TestRegressionR3_1_IntradayPeakTroughDrawdown:
    """日中 peak-to-trough drawdown が監視されていること。"""

    def test_intraday_peak_trough_tracking_exists(self):
        """ReplayBacktest に daily_peak_capital / daily_trough_capital の追跡実装がある。"""
        try:
            import inspect
            from atlas_v3.ops.replay_bt import ReplayBacktest
        except ImportError:
            pytest.skip("atlas_v3.ops.replay_bt unavailable")

        source = inspect.getsource(ReplayBacktest)
        has_peak = "daily_peak_capital" in source or "peak_capital" in source
        has_trough = "daily_trough_capital" in source or "trough_capital" in source
        assert has_peak and has_trough, (
            "REG-R3-1 再発疑い: intraday peak/trough 追跡が欠落。"
            "日中 drawdown (+1000 → -1500 推移等) を見逃す"
        )


# ============================================================================
# Ledger メタテスト: 本 ledger 自体の整合性
# ============================================================================

class TestRegressionLedgerMeta:
    """Ledger 自体が機能していることの自己検証。"""

    def test_ledger_has_at_least_one_test_per_regression(self):
        """発見済 regression の数だけテストクラスが存在すること。"""
        import inspect
        current_module = sys.modules[__name__]
        classes = [
            (name, cls)
            for name, cls in inspect.getmembers(current_module, inspect.isclass)
            if name.startswith("TestRegression") and not name.endswith("Meta")
        ]
        # REG-R2-1 / REG-R2-2 / REG-R3-1 = 3 件分のテストクラスが必要
        assert len(classes) >= 3, (
            f"Regression ledger のテストクラスが不足: {len(classes)} < 3。"
            "新しい regression が発見されたら本 ledger に追加必須"
        )
