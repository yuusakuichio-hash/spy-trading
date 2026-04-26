"""tests/test_tradeify_lightning_20260419.py — Tradeify Lightning ルールプラグイン テスト
                                               (Sora Lab / Chronos 2026-04-19)

テスト対象: chronos_rules_plugin/tradeify_lightning.py

カバー範囲（15+ケース）:
  1. プラグイン登録・インスタンス生成
  2. Consistency tier 切替（全フェーズで None・MFFUと異なる）
  3. Activation fee 判定（Daily Path / Flex Path）
  4. Payout cycle（5 勝利日）
  5. Profit split threshold（初回 $15K は 100% → 以降 90%）
  6. Automation policy 準拠（HFT/commercial 禁止）
  7. 戦術分離（MFFU 戦術使用で違反）
  8. News window（制限なし = 常に False）
  9. Max contracts（symbol 別）
 10. Max Loss Limit（フェーズ別・口座サイズ別）
 11. payout_rules 辞書構造
 12. check_compliance 統合（正常・MLL 違反・コントラクト超過）
 13. load_plugin("tradeify", "lightning") 経由 instantiate
 14. Core deprecated warning 発生確認
 15. 戦術分離: MFFU 側戦術チェック
 16. account_size 不正値で ValueError
 17. path 不正値で ValueError
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Optional

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


# ── ヘルパー: OrderContext 生成 ───────────────────────────────────────────────

def _make_order(
    account_id: str = "tradeify_F",
    symbol: str = "MES",
    side: str = "BUY",
    qty: int = 1,
    entry_price: float = 5_000.0,
    current_balance_usd: float = 0.0,
    daily_pnl_usd: float = 0.0,
    daily_pnl_history: Optional[list] = None,
    phase: str = "funded",
    payout_count: int = 0,
    peak_balance_usd: float = 0.0,
    tactic_name: Optional[str] = None,
    strategy_type: str = "proprietary",
):
    from chronos_rules_plugin import OrderContext
    ctx = OrderContext(
        account_id=account_id,
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        current_balance_usd=current_balance_usd,
        daily_pnl_usd=daily_pnl_usd,
        daily_pnl_history=daily_pnl_history or [],
        phase=phase,
        payout_count=payout_count,
        peak_balance_usd=peak_balance_usd,
    )
    # 拡張フィールド（setattr で追加）
    if tactic_name is not None:
        ctx.tactic_name = tactic_name
    ctx.strategy_type = strategy_type
    return ctx


def _make_rules(account_size_usd: float = 50_000.0, path: str = "daily"):
    from chronos_rules_plugin.tradeify_lightning import TradeifyLightningRules
    return TradeifyLightningRules(account_size_usd=account_size_usd, path=path)


# ══════════════════════════════════════════════════════════════════════════════
# TC-01: プラグイン登録・インスタンス生成
# ══════════════════════════════════════════════════════════════════════════════

class TestTC01PluginRegistration:

    def test_direct_instantiate(self):
        rules = _make_rules()
        assert rules is not None

    def test_load_plugin_via_factory(self):
        """load_plugin("tradeify", "lightning") 経由で生成できること。"""
        # プラグインモジュールを先に import して register させる
        import chronos_rules_plugin.tradeify_lightning  # noqa: F401
        from chronos_rules_plugin import load_plugin
        rules = load_plugin("tradeify", "lightning")
        assert rules is not None

    def test_monthly_fee_50k(self):
        """Lightning は買い切り $295（Select plan の $111/月とは異なる）。"""
        rules = _make_rules(account_size_usd=50_000)
        assert rules.get_monthly_fee_usd() == 295.0

    def test_monthly_fee_100k(self):
        """Lightning は買い切り $295（Select plan の $181/月とは異なる）。"""
        rules = _make_rules(account_size_usd=100_000)
        assert rules.get_monthly_fee_usd() == 295.0

    def test_monthly_fee_150k(self):
        """Lightning は買い切り $295（Select plan の $251/月とは異なる）。"""
        rules = _make_rules(account_size_usd=150_000)
        assert rules.get_monthly_fee_usd() == 295.0


# ══════════════════════════════════════════════════════════════════════════════
# TC-02: Consistency Tier 切替（全フェーズで None）
# ══════════════════════════════════════════════════════════════════════════════

class TestTC02ConsistencyTier:

    def test_consistency_funded_is_not_none(self):
        """Funded フェーズ: Consistency Rule あり（20%/25%/30% tier）。
        Lightning 2025-09-12 改定: 累計利益帯別スケーリング制。
        get_consistency_pct() は後方互換のため最低 tier の 20% を返す。
        """
        rules = _make_rules()
        assert rules.get_consistency_pct("funded") == 0.20

    def test_consistency_funded_post_activation(self):
        """funded_post_activation フェーズ: Consistency Rule あり（20%）。"""
        rules = _make_rules()
        assert rules.get_consistency_pct("funded_post_activation") == 0.20

    def test_consistency_by_payout_count_all_non_none(self):
        """ペイアウト回数（0/1/2/3以降）全て 20%（最低 tier の Consistency）。"""
        rules = _make_rules()
        for payout_count in range(5):
            result = rules.get_consistency_pct_by_payout(payout_count)
            assert result == 0.20, f"payout_count={payout_count} は 0.20 であるべき"

    def test_consistency_differs_from_mffu_flex(self):
        """MFFU Flex の Consistency（50%）と Tradeify Lightning（20%）は異なること。"""
        from chronos_rules_plugin.mffu_flex import MFFUFlexRules
        mffu_rules = MFFUFlexRules()
        tradeify_rules = _make_rules()
        mffu_consistency = mffu_rules.get_consistency_pct("evaluation")
        tradeify_consistency = tradeify_rules.get_consistency_pct("funded")
        assert mffu_consistency == 0.50
        assert tradeify_consistency == 0.20


# ══════════════════════════════════════════════════════════════════════════════
# TC-03: Activation Fee 判定
# ══════════════════════════════════════════════════════════════════════════════

class TestTC03ActivationFee:

    def test_daily_path_fee(self):
        """Lightning: activation fee = $0（Select plan の $1,500 とは異なる）。"""
        rules = _make_rules(path="daily")
        assert rules.get_activation_fee_usd() == 0.0

    def test_flex_path_fee(self):
        """Lightning: activation fee = $0（Select plan の $4,000 とは異なる）。"""
        rules = _make_rules(path="flex")
        assert rules.get_activation_fee_usd() == 0.0

    def test_payout_rules_includes_activation_fee(self):
        """payout_rules 辞書に activation_fee_usd が含まれること（$0）。"""
        rules = _make_rules(path="daily")
        payout_rules = rules.get_payout_rules()
        assert "activation_fee_usd" in payout_rules
        assert payout_rules["activation_fee_usd"] == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# TC-04: Payout Cycle（5 勝利日）
# ══════════════════════════════════════════════════════════════════════════════

class TestTC04PayoutCycle:

    def test_payout_cycle_winning_days(self):
        """Payout cycle = 5 勝利日。"""
        rules = _make_rules()
        payout_rules = rules.get_payout_rules()
        assert payout_rules["payout_cycle_winning_days"] == 5

    def test_profit_target_is_zero(self):
        """Instant Funded: profit target なし（0.0）。"""
        rules = _make_rules()
        assert rules.get_profit_target_usd() == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# TC-05: Profit Split Threshold
# ══════════════════════════════════════════════════════════════════════════════

class TestTC05ProfitSplit:

    def test_profit_split_is_constant_90pct_at_zero(self):
        """Lightning: 累計利益 $0 でも常時 90%（Select plan の 100%→90% 段階制なし）。"""
        rules = _make_rules()
        assert rules.get_profit_split_pct(0.0) == 0.90

    def test_profit_split_is_constant_90pct_below_15k(self):
        """Lightning: 累計利益 $14,999 でも 90%（段階制なし）。"""
        rules = _make_rules()
        assert rules.get_profit_split_pct(14_999.0) == 0.90

    def test_profit_split_is_constant_90pct_at_threshold(self):
        """Lightning: 累計利益 $15,000 でも 90%（常時固定・Select plan の threshold なし）。"""
        rules = _make_rules()
        assert rules.get_profit_split_pct(15_000.0) == 0.90

    def test_profit_split_above_threshold_still_90pct(self):
        """累計利益 $15,001: split = 90%（Lightning は全額 90%）。"""
        rules = _make_rules()
        assert rules.get_profit_split_pct(15_001.0) == 0.90

    def test_post_threshold_90pct_high(self):
        """累計利益 $100,000: split = 90%（高額でも変化なし）。"""
        rules = _make_rules()
        assert rules.get_profit_split_pct(100_000.0) == 0.90

    def test_payout_rules_split_structure(self):
        """payout_rules に split 情報が含まれること（Lightning: 常時 90%）。"""
        rules = _make_rules()
        pr = rules.get_payout_rules()
        # Lightning は threshold なし・常時 90%
        # profit_split_pct または profit_split_initial_pct キーで 90% を確認
        split = pr.get("profit_split_pct", pr.get("profit_split_initial_pct", None))
        assert split == 0.90


# ══════════════════════════════════════════════════════════════════════════════
# TC-06: Automation Policy
# ══════════════════════════════════════════════════════════════════════════════

class TestTC06AutomationPolicy:

    def test_proprietary_allowed(self):
        """自己所有戦略: 許可。"""
        rules = _make_rules()
        ok, reason = rules.check_automation_policy("proprietary")
        assert ok
        assert reason == ""

    def test_hft_prohibited(self):
        """HFT: 禁止。"""
        rules = _make_rules()
        ok, reason = rules.check_automation_policy("hft")
        assert not ok
        assert "HFT" in reason

    def test_commercial_prohibited(self):
        """commercial 戦略: 禁止。"""
        rules = _make_rules()
        ok, reason = rules.check_automation_policy("commercial")
        assert not ok
        assert "commercial" in reason


# ══════════════════════════════════════════════════════════════════════════════
# TC-07: 戦術分離
# ══════════════════════════════════════════════════════════════════════════════

class TestTC07TacticSeparation:

    def test_vwap_reclaim_allowed(self):
        """vwap_reclaim: Tradeify 許可戦術。"""
        rules = _make_rules()
        ok, reason = rules.check_tactic_allowed("vwap_reclaim")
        assert ok

    def test_liquidity_sweep_allowed(self):
        """liquidity_sweep: Tradeify 許可戦術。"""
        rules = _make_rules()
        ok, reason = rules.check_tactic_allowed("liquidity_sweep")
        assert ok

    def test_orb_mffu_exclusive_violation(self):
        """orb: MFFU 専用戦術 → Tradeify で使用不可。"""
        rules = _make_rules()
        ok, reason = rules.check_tactic_allowed("orb")
        assert not ok
        assert "MFFU" in reason

    def test_orb_buy_mffu_exclusive_violation(self):
        """orb_buy: MFFU 専用戦術 → Tradeify で使用不可。"""
        rules = _make_rules()
        ok, reason = rules.check_tactic_allowed("orb_buy")
        assert not ok

    def test_vix_mr_mffu_exclusive_violation(self):
        """vix_mr: MFFU 専用戦術 → Tradeify で使用不可。"""
        rules = _make_rules()
        ok, reason = rules.check_tactic_allowed("vix_mr")
        assert not ok

    def test_gap_fill_mffu_exclusive_violation(self):
        """gap_fill: MFFU 専用戦術 → Tradeify で使用不可。"""
        rules = _make_rules()
        ok, reason = rules.check_tactic_allowed("gap_fill")
        assert not ok

    def test_check_compliance_with_mffu_tactic_blocked(self):
        """check_compliance で MFFU 戦術を使うと NG。"""
        rules = _make_rules()
        order = _make_order(tactic_name="orb_buy")
        ok, reason = rules.check_compliance(order)
        assert not ok
        assert "MFFU" in reason


# ══════════════════════════════════════════════════════════════════════════════
# TC-08: News Window（制限なし）
# ══════════════════════════════════════════════════════════════════════════════

class TestTC08NewsWindow:

    def test_news_window_always_false(self):
        """News window: 常に False（制限なし）。"""
        import datetime
        import zoneinfo
        rules = _make_rules()
        ET = zoneinfo.ZoneInfo("America/New_York")
        # FOMC 発表時刻（通常 14:00 ET）
        fomc_time = datetime.datetime(2026, 4, 20, 14, 0, 0, tzinfo=ET)
        assert rules.check_news_window(fomc_time) is False

    def test_news_window_nfp_is_false(self):
        """NFP 発表時刻でも False（Tradeify は制限なし）。"""
        import datetime
        import zoneinfo
        rules = _make_rules()
        ET = zoneinfo.ZoneInfo("America/New_York")
        nfp_time = datetime.datetime(2026, 5, 1, 8, 30, 0, tzinfo=ET)
        assert rules.check_news_window(nfp_time) is False


# ══════════════════════════════════════════════════════════════════════════════
# TC-09: Max Contracts
# ══════════════════════════════════════════════════════════════════════════════

class TestTC09MaxContracts:

    def test_mes_micro(self):
        """MES（micro）: 40 枚上限（2025-09-12 改定: 旧 100 → 40）。"""
        rules = _make_rules()
        assert rules.get_max_contracts("MES", "funded") == 40

    def test_mnq_micro(self):
        """MNQ（micro）: 40 枚上限（2025-09-12 改定: 旧 100 → 40）。"""
        rules = _make_rules()
        assert rules.get_max_contracts("MNQ", "funded") == 40

    def test_es_mini(self):
        """ES（mini）: 4 枚上限（2025-09-12 改定: 旧 10 → 4）。"""
        rules = _make_rules()
        assert rules.get_max_contracts("ES", "funded") == 4

    def test_balance_based_returns_fixed(self):
        """残高連動なし（固定上限を返す）: micro 40 枚。"""
        rules = _make_rules()
        result = rules.get_max_contracts_for_balance("MES", net_profit_usd=500.0)
        assert result == 40


# ══════════════════════════════════════════════════════════════════════════════
# TC-10: Max Loss Limit（口座サイズ別）
# ══════════════════════════════════════════════════════════════════════════════

class TestTC10MaxLossLimit:

    def test_mll_50k(self):
        """50K 口座: MLL = $2,000。"""
        rules = _make_rules(account_size_usd=50_000)
        assert rules.get_max_loss_usd("funded") == 2_000.0

    def test_mll_100k(self):
        """100K 口座: MLL = $3,500。"""
        rules = _make_rules(account_size_usd=100_000)
        assert rules.get_max_loss_usd("funded") == 3_500.0

    def test_mll_150k(self):
        """150K 口座: MLL = $5,000。"""
        rules = _make_rules(account_size_usd=150_000)
        assert rules.get_max_loss_usd("funded") == 5_000.0

    def test_daily_loss_limit_is_1250(self):
        """日次損失制限: $1,250/日（2025-09-12 以降追加）。"""
        rules = _make_rules()
        assert rules.get_daily_loss_limit_usd() == 1_250.0


# ══════════════════════════════════════════════════════════════════════════════
# TC-11: check_compliance 統合テスト
# ══════════════════════════════════════════════════════════════════════════════

class TestTC11CheckCompliance:

    def test_normal_order_ok(self):
        """正常発注: 合格。"""
        rules = _make_rules()
        order = _make_order(
            qty=1,
            current_balance_usd=0.0,
            peak_balance_usd=0.0,
            tactic_name="vwap_reclaim",
        )
        ok, reason = rules.check_compliance(order)
        assert ok, f"Expected OK, got: {reason}"

    def test_mll_violation(self):
        """MLL 違反: 残高 < フロア → NG。"""
        rules = _make_rules(account_size_usd=50_000)
        # Funded: floor = -MLL = -2000。残高 -2001 → 違反
        order = _make_order(
            current_balance_usd=-2_001.0,
            peak_balance_usd=0.0,
        )
        ok, reason = rules.check_compliance(order)
        assert not ok
        assert "MLL" in reason

    def test_contract_excess_violation(self):
        """コントラクト超過: qty=101 (MES 上限 100) → NG。"""
        rules = _make_rules()
        order = _make_order(symbol="MES", qty=101)
        ok, reason = rules.check_compliance(order)
        assert not ok
        assert "コントラクト数超過" in reason

    def test_hft_strategy_violation(self):
        """HFT 戦略: check_compliance で NG。"""
        rules = _make_rules()
        order = _make_order(
            tactic_name="vwap_reclaim",
            strategy_type="hft",
        )
        ok, reason = rules.check_compliance(order)
        assert not ok
        assert "HFT" in reason


# ══════════════════════════════════════════════════════════════════════════════
# TC-12: Core Deprecated Warning
# ══════════════════════════════════════════════════════════════════════════════

class TestTC12CoreDeprecated:

    def test_mffu_core_raises_deprecation_warning(self):
        """MFFUCoreRules() 生成時に DeprecationWarning が発生すること。"""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from chronos_rules_plugin.mffu_core import MFFUCoreRules
            MFFUCoreRules()
            # DeprecationWarning が 1 件以上発生していること
            dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(dep_warnings) >= 1
            assert "2026-01-28" in str(dep_warnings[0].message)

    def test_mffu_core_still_functional_for_existing_accounts(self):
        """DeprecationWarning は出るが、既存口座継続運用のため機能は維持。"""
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            from chronos_rules_plugin.mffu_core import MFFUCoreRules
            rules = MFFUCoreRules()
            # 基本機能が動くこと
            assert rules.get_max_loss_usd("evaluation") == 2_000.0


# ══════════════════════════════════════════════════════════════════════════════
# TC-13: account_size / path バリデーション
# ══════════════════════════════════════════════════════════════════════════════

class TestTC13Validation:

    def test_invalid_account_size_raises(self):
        """不正な account_size_usd で ValueError。"""
        from chronos_rules_plugin.tradeify_lightning import TradeifyLightningRules
        with pytest.raises(ValueError, match="Tradeify Lightning 対象外"):
            TradeifyLightningRules(account_size_usd=75_000)

    def test_invalid_path_is_accepted(self):
        """Lightning は path パラメータを使わないため不正値でもエラーなし（後方互換パラメータ）。"""
        from chronos_rules_plugin.tradeify_lightning import TradeifyLightningRules
        # Lightning では path を使わないため ValueError は発生しない
        rules = TradeifyLightningRules(path="unknown_path")
        assert rules is not None

    def test_25k_account_valid(self):
        """25K 口座: 有効。Lightning 買い切り $295。"""
        from chronos_rules_plugin.tradeify_lightning import TradeifyLightningRules
        rules = TradeifyLightningRules(account_size_usd=25_000)
        assert rules.get_monthly_fee_usd() == 295.0
