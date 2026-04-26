"""tests/test_cs_1dte.py — cs_sell_1dte PDT回避切替ロジックのユニットテスト

テスト対象:
  - strategy_selector.choose_cs_variant()
  - strategy_selector.select_strategy() の cs_sell/cs_sell_1dte 分岐
  - CS_1DTE_PARAMS の定義値確認

設計根拠 (data/atlas_20pct_improvement_10measures_20260420.md 施策5):
  - 1DTE CS (w=3, d=0.20, sm=2x, tp=50%) BT月利+32.4%
  - VIX<25 × PDT残≤2 → 1DTE優先（稼働日数倍増・PDT物理回避）
  - VIX>25 → 0DTE優先（当日収束期待・BT根拠）
"""
from __future__ import annotations

import math
import sys
import os

import pytest

# strategy_selector をルートから import できるように
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy_selector import (
    choose_cs_variant,
    select_strategy,
    CS_1DTE_PARAMS,
    CS_1DTE_VIX_THRESHOLD,
    CS_1DTE_PDT_TRIGGER,
)

# テスト用 VIX 履歴（60日分の中程度ボラ環境）
_VIX_HIST = sorted([13.0 + i * 0.2 for i in range(60)])  # 13.0〜24.8


# ──────────────────────────────────────────────────────────────────────────────
# choose_cs_variant() 単体テスト
# ──────────────────────────────────────────────────────────────────────────────

class TestChooseCsVariant:
    """choose_cs_variant() の全分岐を網羅するテスト群。"""

    def _env(self, vix=18.0, pdt_remaining=float("inf"), account_equity=30_000.0) -> dict:
        return {
            "vix": vix,
            "pdt_remaining": pdt_remaining,
            "account_equity": account_equity,
        }

    def test_pdt_unlimited_returns_0dte(self):
        """$25K以上 (PDT制限なし) → cs_sell (0DTE) を返す。"""
        env = self._env(vix=18.0, pdt_remaining=float("inf"), account_equity=30_000.0)
        assert choose_cs_variant(env) == "cs_sell"

    def test_pdt_remaining_0_forces_1dte(self):
        """PDT残0 + $25K未満 → cs_sell_1dte 強制。"""
        env = self._env(vix=18.0, pdt_remaining=0, account_equity=8_000.0)
        assert choose_cs_variant(env) == "cs_sell_1dte"

    def test_pdt_remaining_2_vix_low_returns_1dte(self):
        """PDT残2 + VIX<=25 + $25K未満 → cs_sell_1dte。"""
        env = self._env(vix=20.0, pdt_remaining=2, account_equity=10_000.0)
        assert choose_cs_variant(env) == "cs_sell_1dte"

    def test_pdt_remaining_2_vix_high_returns_0dte(self):
        """PDT残2 + VIX>25 + $25K未満 → cs_sell (0DTE優先)。"""
        env = self._env(vix=26.0, pdt_remaining=2, account_equity=10_000.0)
        assert choose_cs_variant(env) == "cs_sell"

    def test_pdt_remaining_3_vix_low_returns_1dte(self):
        """PDT残3 + VIX<=25 + $25K未満 → cs_sell_1dte（BT月利優位）。

        残3本はトリガー閾値(2)を超えるが、$25K未満かつVIX<=25のケースでも
        choose_cs_variant は1DTE を返す（デフォルトブランチ）。
        """
        env = self._env(vix=15.0, pdt_remaining=3, account_equity=12_000.0)
        result = choose_cs_variant(env)
        # 残3本 > CS_1DTE_PDT_TRIGGER(2) → デフォルトブランチ → 1DTE
        assert result == "cs_sell_1dte"

    def test_equity_above_25k_ignores_pdt_remaining(self):
        """$25K以上ならPDT残にかかわらず0DTE。"""
        env = self._env(vix=18.0, pdt_remaining=0, account_equity=26_000.0)
        # $25K以上は早期リターン → cs_sell
        assert choose_cs_variant(env) == "cs_sell"


# ──────────────────────────────────────────────────────────────────────────────
# select_strategy() 統合テスト — cs_sell/cs_sell_1dte 分岐
# ──────────────────────────────────────────────────────────────────────────────

class TestSelectStrategyCs1Dte:
    """select_strategy() がPDT残を受け取り cs_sell_1dte を返すことを確認。"""

    def _base_env(self, **kwargs) -> dict:
        # VRP=1.5（弱正）・GEX=None でcs_sell環境を作る（ic_sellトリガーはVRP強正+GEX強正）
        base = {
            "vix": 15.0,
            "vix_rate": -0.5,
            "vrp": 1.5,    # 弱正 → ic_sell条件(vrp_strongly_positive + gex_positive)不成立
            "gex": None,   # GEXデータなし → vrp_positiveブランチ(cs_sell系)に落ちる
            "term_struct": 0.95,
            "vix_term_ratio": 0.90,
            "ivr": 45.0,
            "env_score": 80,
            "gap_pct": 0.2,
            "vix_history": _VIX_HIST,
            "bias": "bull",  # 方向性ありでcs_sellを選びやすくする
            "pdt_remaining": float("inf"),
            "account_equity": 30_000.0,
        }
        base.update(kwargs)
        return base

    def test_pdt_constrained_low_vix_selects_1dte(self):
        """PDT残2 + $25K未満 + VIX<=25 → primary が cs_sell_1dte。

        IC売りではなくCS売り環境（vrp弱正・GEXなし）でテスト。
        PDT残≤2 かつ VIX<=25 → choose_cs_variant が 1DTE を返す。
        """
        env = self._base_env(
            pdt_remaining=2,
            account_equity=8_000.0,
            vix=15.0,
        )
        result = select_strategy(env)
        primary = result["primary"]["strategy"]
        # choose_cs_variant がcs_sell_1dteを返す → primary is cs_sell_1dte
        assert primary == "cs_sell_1dte", (
            f"期待 cs_sell_1dte、実際 {primary}\n"
            f"reason: {result['reason']}"
        )

    def test_pdt_unlimited_low_vix_selects_0dte(self):
        """PDT無制限 + $25K以上 + VIX15 → primary が cs_sell (0DTE)。

        $25K以上はchoose_cs_variantがcs_sellを返すため0DTE。
        """
        env = self._base_env(
            pdt_remaining=float("inf"),
            account_equity=30_000.0,
            vix=15.0,
        )
        result = select_strategy(env)
        primary = result["primary"]["strategy"]
        assert primary == "cs_sell", (
            f"期待 cs_sell、実際 {primary}\n"
            f"reason: {result['reason']}"
        )

    def test_pdt_remaining_0_forces_1dte_in_select(self):
        """PDT残0 → select_strategy も cs_sell_1dte を返す。"""
        env = self._base_env(
            pdt_remaining=0,
            account_equity=9_000.0,
            vix=18.0,
        )
        result = select_strategy(env)
        primary = result["primary"]["strategy"]
        assert primary == "cs_sell_1dte", f"期待 cs_sell_1dte、実際 {primary}"


# ──────────────────────────────────────────────────────────────────────────────
# CS_1DTE_PARAMS 定義値確認
# ──────────────────────────────────────────────────────────────────────────────

class TestCs1DteParams:
    """CS_1DTE_PARAMS の必須キーと値域を確認するテスト。"""

    def test_required_keys_present(self):
        """必須パラメータが全て定義されている。"""
        required = {"width", "delta", "size_mult", "take_profit", "dte"}
        assert required.issubset(CS_1DTE_PARAMS.keys()), (
            f"不足キー: {required - set(CS_1DTE_PARAMS.keys())}"
        )

    def test_dte_is_1(self):
        """DTE=1 (翌営業日満期) であること。"""
        assert CS_1DTE_PARAMS["dte"] == 1

    def test_delta_range(self):
        """delta は 0.10〜0.30 の保守的範囲内。"""
        d = CS_1DTE_PARAMS["delta"]
        assert 0.10 <= d <= 0.30, f"delta={d} が許容範囲外"

    def test_take_profit_50pct(self):
        """TP=50%（BTパラメータ）。"""
        assert CS_1DTE_PARAMS["take_profit"] == 0.50

    def test_vix_threshold_definition(self):
        """CS_1DTE_VIX_THRESHOLD が 20〜30 の合理的範囲。"""
        assert 20.0 <= CS_1DTE_VIX_THRESHOLD <= 30.0

    def test_pdt_trigger_definition(self):
        """CS_1DTE_PDT_TRIGGER が 1〜3 の合理的範囲。"""
        assert 1 <= CS_1DTE_PDT_TRIGGER <= 3
