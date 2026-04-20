"""tests/test_chronos_plan_configs.py — Chronos CRITICAL修正 2026-04-20 回帰テスト

CRITICAL 1: Tradeify Lightning plugin 6件誤値修正
  - profit_split: 常時 90%（threshold ロジックなし）
  - activation_fee: $0
  - consistency: 20%/25%/30% scaling
  - max_contracts: 4 mini / 40 micro
  - daily_loss_limit: $1,250/日
  - fee: $295 buy-once

CRITICAL 2: Builder pro_rush 戦術置換
  - accounts.yaml の E アカウントに orb_breakout/level_trading/range_break_long/range_break_short
  - force_close_et / daily_loss_limit_usd / consistency_max_pct / payout_cap_per_cycle_usd / sim_total_cap_usd 追加
  - chronos_strategy_selector.py に orb / level_trading / range_break_long / range_break_short が存在
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


# ══════════════════════════════════════════════════════════════════════════════
# CRITICAL 1: Tradeify Lightning plugin 6件修正
# ══════════════════════════════════════════════════════════════════════════════

class TestTradeifyLightningCriticalFixes:
    """Tradeify Lightning plugin の 6件誤値修正を検証する。"""

    @pytest.fixture
    def rules(self):
        from chronos_rules_plugin.tradeify_lightning import TradeifyLightningRules
        return TradeifyLightningRules(account_size_usd=50_000)

    # ── Fix 1: Profit Split 常時 90% ──────────────────────────────────────────

    def test_profit_split_is_always_90_pct_at_zero(self, rules):
        """累計 $0 でも profit split は 90%（旧: 100%）。"""
        assert rules.get_profit_split_pct(0.0) == pytest.approx(0.90)

    def test_profit_split_is_always_90_pct_at_5000(self, rules):
        """累計 $5,000（旧 threshold $15K 未満）でも 90%。threshold ロジックなし。"""
        assert rules.get_profit_split_pct(5_000.0) == pytest.approx(0.90)

    def test_profit_split_is_always_90_pct_at_15000(self, rules):
        """累計 $15,000 ちょうどでも 90%（threshold ロジック削除済み）。"""
        assert rules.get_profit_split_pct(15_000.0) == pytest.approx(0.90)

    def test_profit_split_is_always_90_pct_above_15000(self, rules):
        """累計 $15,000 超でも 90%（select plan との混同なし）。"""
        assert rules.get_profit_split_pct(20_000.0) == pytest.approx(0.90)

    def test_payout_rules_profit_split_pct(self, rules):
        """get_payout_rules() も profit_split_pct を返す（threshold 系キーなし）。"""
        payout = rules.get_payout_rules()
        assert "profit_split_pct" in payout
        assert payout["profit_split_pct"] == pytest.approx(0.90)
        # 旧 Select plan 由来のキーが残っていないこと
        assert "profit_split_threshold_usd" not in payout
        assert "profit_split_initial_pct" not in payout

    # ── Fix 2: Activation Fee $0 ─────────────────────────────────────────────

    def test_activation_fee_is_zero(self, rules):
        """Lightning activation fee は $0（旧: $1,500/$4,000 は Select plan の値）。"""
        assert rules.get_activation_fee_usd() == pytest.approx(0.0)

    def test_payout_rules_activation_fee_is_zero(self, rules):
        """get_payout_rules() の activation_fee_usd が $0。"""
        assert rules.get_payout_rules()["activation_fee_usd"] == pytest.approx(0.0)

    # ── Fix 3: Consistency Rule 20%/25%/30% scaling ──────────────────────────

    def test_consistency_tier_0_to_3k(self, rules):
        """累計利益 $0–$3K 帯: Consistency 上限 20%。"""
        assert rules.get_consistency_pct_for_profit(0.0) == pytest.approx(0.20)
        assert rules.get_consistency_pct_for_profit(1_500.0) == pytest.approx(0.20)
        assert rules.get_consistency_pct_for_profit(2_999.99) == pytest.approx(0.20)

    def test_consistency_tier_3k_to_10k(self, rules):
        """累計利益 $3K–$10K 帯: Consistency 上限 25%。"""
        assert rules.get_consistency_pct_for_profit(3_000.0) == pytest.approx(0.25)
        assert rules.get_consistency_pct_for_profit(5_000.0) == pytest.approx(0.25)
        assert rules.get_consistency_pct_for_profit(9_999.99) == pytest.approx(0.25)

    def test_consistency_tier_above_10k(self, rules):
        """累計利益 $10K 超: Consistency 上限 30%。"""
        assert rules.get_consistency_pct_for_profit(10_000.0) == pytest.approx(0.30)
        assert rules.get_consistency_pct_for_profit(50_000.0) == pytest.approx(0.30)

    def test_consistency_pct_legacy_returns_conservative(self, rules):
        """後方互換 get_consistency_pct() は保守的な 20% を返す。"""
        result = rules.get_consistency_pct(phase="funded")
        assert result == pytest.approx(0.20)

    # ── Fix 4: Max Contracts 4 mini / 40 micro ───────────────────────────────

    def test_max_mini_contracts_is_4(self, rules):
        """mini contract 上限が 4（旧: 10）。"""
        assert rules.get_max_contracts("ES", phase="funded") == 4

    def test_max_micro_contracts_is_40(self, rules):
        """micro contract 上限が 40（旧: 100）。"""
        assert rules.get_max_contracts("MES", phase="funded") == 40

    def test_max_mini_contracts_mnq_is_4(self, rules):
        """NQ mini も 4。"""
        assert rules.get_max_contracts("NQ", phase="funded") == 4

    def test_max_micro_contracts_mnq_is_40(self, rules):
        """MNQ micro も 40。"""
        assert rules.get_max_contracts("MNQ", phase="funded") == 40

    def test_payout_rules_max_contracts(self, rules):
        """get_payout_rules() にも max_mini/micro_contracts が正しく入っている。"""
        payout = rules.get_payout_rules()
        assert payout["max_mini_contracts"] == 4
        assert payout["max_micro_contracts"] == 40

    # ── Fix 5: Daily Loss Limit $1,250 ───────────────────────────────────────

    def test_daily_loss_limit_is_1250(self, rules):
        """Daily Loss Limit が $1,250（旧: None）。"""
        assert rules.get_daily_loss_limit_usd() == pytest.approx(1_250.0)

    def test_payout_rules_daily_loss_limit(self, rules):
        """get_payout_rules() の daily_loss_limit_usd が $1,250。"""
        assert rules.get_payout_rules()["daily_loss_limit_usd"] == pytest.approx(1_250.0)

    # ── Fix 6: Fee $295 buy-once ──────────────────────────────────────────────

    def test_fee_buyonce_is_295(self, rules):
        """Lightning fee が $295 買い切り（旧: $111/月は Select plan の値）。"""
        assert rules.get_fee_buyonce_usd() == pytest.approx(295.0)

    def test_monthly_fee_backward_compat_is_295(self, rules):
        """後方互換 get_monthly_fee_usd() も $295 を返す。"""
        assert rules.get_monthly_fee_usd() == pytest.approx(295.0)

    def test_payout_rules_fee_buyonce(self, rules):
        """get_payout_rules() の fee_buyonce_usd が $295。"""
        assert rules.get_payout_rules()["fee_buyonce_usd"] == pytest.approx(295.0)

    # ── check_compliance: daily loss limit 発動 ───────────────────────────────

    def test_compliance_blocks_when_daily_loss_exceeds_1250(self, rules):
        """daily_pnl_usd が -$1,250 以下のとき check_compliance が False を返す。"""
        from chronos_rules_plugin import OrderContext
        order = OrderContext(
            account_id="tradeify_F",
            symbol="MES",
            side="BUY",
            qty=1,
            entry_price=5000.0,
            current_balance_usd=50_000.0,
            daily_pnl_usd=-1_250.0,
            daily_pnl_history=[],
            phase="funded",
            peak_balance_usd=50_000.0,
        )
        ok, reason = rules.check_compliance(order)
        assert not ok
        assert "Daily Loss Limit" in reason

    def test_compliance_passes_when_daily_loss_below_limit(self, rules):
        """daily_pnl_usd が -$1,249（制限内）のとき check_compliance が True を返す。"""
        from chronos_rules_plugin import OrderContext
        order = OrderContext(
            account_id="tradeify_F",
            symbol="MES",
            side="BUY",
            qty=1,
            entry_price=5000.0,
            current_balance_usd=50_000.0,
            daily_pnl_usd=-1_249.0,
            daily_pnl_history=[],
            phase="funded",
            peak_balance_usd=50_000.0,
        )
        ok, _ = rules.check_compliance(order)
        assert ok

    def test_compliance_blocks_when_contracts_exceed_4_mini(self, rules):
        """mini contract が 5 を超えたとき check_compliance が False を返す（旧 10 は許可されていた）。"""
        from chronos_rules_plugin import OrderContext
        order = OrderContext(
            account_id="tradeify_F",
            symbol="ES",
            side="BUY",
            qty=5,
            entry_price=5000.0,
            current_balance_usd=50_000.0,
            daily_pnl_usd=0.0,
            daily_pnl_history=[],
            phase="funded",
            peak_balance_usd=50_000.0,
        )
        ok, reason = rules.check_compliance(order)
        assert not ok
        assert "コントラクト数超過" in reason


# ══════════════════════════════════════════════════════════════════════════════
# CRITICAL 2: Builder pro_rush 戦術置換
# ══════════════════════════════════════════════════════════════════════════════

class TestBuilderStrategyReplacement:
    """Builder pro_rush → 4戦術置換の検証。"""

    @pytest.fixture
    def accounts_yaml(self):
        yaml_path = _ROOT / "chronos_accounts.yaml"
        with open(yaml_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    @pytest.fixture
    def builder_account(self, accounts_yaml):
        accounts = accounts_yaml["accounts"]
        builder = [a for a in accounts if a["id"] == "mffu_builder_E"]
        assert len(builder) == 1, "mffu_builder_E が chronos_accounts.yaml に存在しない"
        return builder[0]

    # ── pro_rush が存在しないこと ──────────────────────────────────────────────

    def test_pro_rush_not_in_builder_strategies(self, builder_account):
        """mffu_builder_E の strategies に pro_rush が含まれていないこと。"""
        strategies = builder_account["strategies"]
        assert "pro_rush" not in strategies, \
            f"pro_rush が残っている: {strategies}"

    # ── 4戦術が全て存在すること ────────────────────────────────────────────────

    def test_orb_breakout_in_builder_strategies(self, builder_account):
        """orb_breakout が mffu_builder_E の strategies に含まれる。"""
        assert "orb_breakout" in builder_account["strategies"]

    def test_level_trading_in_builder_strategies(self, builder_account):
        """level_trading が mffu_builder_E の strategies に含まれる。"""
        assert "level_trading" in builder_account["strategies"]

    def test_range_break_long_in_builder_strategies(self, builder_account):
        """range_break_long が mffu_builder_E の strategies に含まれる。"""
        assert "range_break_long" in builder_account["strategies"]

    def test_range_break_short_in_builder_strategies(self, builder_account):
        """range_break_short が mffu_builder_E の strategies に含まれる。"""
        assert "range_break_short" in builder_account["strategies"]

    # ── Builder 制約フィールドが正しく追加されていること ──────────────────────

    def test_force_close_et_is_1555(self, builder_account):
        """force_close_et が '15:55' に設定されている（overnight禁止対応）。"""
        assert builder_account.get("force_close_et") == "15:55", \
            f"force_close_et: {builder_account.get('force_close_et')}"

    def test_daily_loss_limit_usd_is_1000(self, builder_account):
        """daily_loss_limit_usd が 1000 に設定されている（Builder 公式制約）。"""
        assert builder_account.get("daily_loss_limit_usd") == 1000, \
            f"daily_loss_limit_usd: {builder_account.get('daily_loss_limit_usd')}"

    def test_consistency_max_pct_is_050(self, builder_account):
        """consistency_max_pct が 0.50 に設定されている（Builder Consistency Rule）。"""
        assert builder_account.get("consistency_max_pct") == pytest.approx(0.50), \
            f"consistency_max_pct: {builder_account.get('consistency_max_pct')}"

    def test_payout_cap_per_cycle_usd(self, builder_account):
        """payout_cap_per_cycle_usd が設定されている。"""
        assert "payout_cap_per_cycle_usd" in builder_account

    def test_sim_total_cap_usd_is_10000(self, builder_account):
        """sim_total_cap_usd が 10000 に設定されている（Builder Payout Cap $10K）。"""
        assert builder_account.get("sim_total_cap_usd") == 10_000, \
            f"sim_total_cap_usd: {builder_account.get('sim_total_cap_usd')}"


class TestBuilderStrategiesExistInSelector:
    """Builder 4戦術が chronos_strategy_selector.py に実装済みであることを確認。"""

    def test_orb_strategy_exists_in_selector(self):
        """ORB 戦術（'orb'）が strategy selector で選択可能であること。"""
        import chronos_strategy_selector as sel
        # KNOWN_STRATEGIES は selector の内部リストで all_strategy_names() 等から取得
        # 実際のセレクター実装に合わせて動的確認
        source = Path(sel.__file__).read_text(encoding="utf-8")
        assert "'orb'" in source or '"orb"' in source, \
            "orb が chronos_strategy_selector.py に存在しない"

    def test_level_trading_strategy_exists_in_selector(self):
        """level_trading 戦術が strategy selector に存在すること。"""
        import chronos_strategy_selector as sel
        source = Path(sel.__file__).read_text(encoding="utf-8")
        assert "level_trading" in source, \
            "level_trading が chronos_strategy_selector.py に存在しない"

    def test_range_break_long_strategy_exists_in_selector(self):
        """range_break_long 戦術が strategy selector に存在すること。"""
        import chronos_strategy_selector as sel
        source = Path(sel.__file__).read_text(encoding="utf-8")
        assert "range_break_long" in source, \
            "range_break_long が chronos_strategy_selector.py に存在しない"

    def test_range_break_short_strategy_exists_in_selector(self):
        """range_break_short 戦術が strategy selector に存在すること。"""
        import chronos_strategy_selector as sel
        source = Path(sel.__file__).read_text(encoding="utf-8")
        assert "range_break_short" in source, \
            "range_break_short が chronos_strategy_selector.py に存在しない"

    def test_pro_rush_not_in_selector(self):
        """pro_rush が strategy selector に存在しないこと（廃止戦術）。"""
        import chronos_strategy_selector as sel
        source = Path(sel.__file__).read_text(encoding="utf-8")
        assert "pro_rush" not in source, \
            "pro_rush が chronos_strategy_selector.py に残っている（廃止済みのため削除要）"


# ══════════════════════════════════════════════════════════════════════════════
# Tradeify YAML 設定ファイルの整合性確認
# ══════════════════════════════════════════════════════════════════════════════

class TestTradeifyYamlConfig:
    """data/chronos_configs/chronos_config_tradeify_lightning.yaml の値を検証。"""

    @pytest.fixture
    def yaml_config(self):
        yaml_path = _ROOT / "data/chronos_configs/chronos_config_tradeify_lightning.yaml"
        with open(yaml_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_fee_buyonce_is_295(self, yaml_config):
        """fee_buyonce_usd が $295 に設定されている。"""
        account = yaml_config["tradeify_compliance"]["account"]
        assert account["fee_buyonce_usd"] == pytest.approx(295.0)

    def test_activation_fee_is_zero(self, yaml_config):
        """activation_fee_usd が $0 に設定されている。"""
        account = yaml_config["tradeify_compliance"]["account"]
        assert account["activation_fee_usd"] == pytest.approx(0.0)

    def test_profit_split_pct_is_090(self, yaml_config):
        """payout.profit_split_pct が 0.90（常時 90%）。"""
        payout = yaml_config["tradeify_compliance"]["payout"]
        assert payout["profit_split_pct"] == pytest.approx(0.90)

    def test_profit_split_threshold_removed(self, yaml_config):
        """旧 profit_split_threshold_usd キーが削除されている。"""
        payout = yaml_config["tradeify_compliance"]["payout"]
        assert "profit_split_threshold_usd" not in payout

    def test_max_mini_contracts_is_4(self, yaml_config):
        """max_mini_contracts が 4（旧: 10）。"""
        funded = yaml_config["tradeify_compliance"]["funded"]
        assert funded["max_mini_contracts"] == 4

    def test_max_micro_contracts_is_40(self, yaml_config):
        """max_micro_contracts が 40（旧: 100）。"""
        funded = yaml_config["tradeify_compliance"]["funded"]
        assert funded["max_micro_contracts"] == 40

    def test_daily_loss_limit_is_1250(self, yaml_config):
        """daily_loss_limit_usd が $1,250。"""
        funded = yaml_config["tradeify_compliance"]["funded"]
        assert funded["daily_loss_limit_usd"] == pytest.approx(1_250.0)

    def test_consistency_rule_tiers_exist(self, yaml_config):
        """consistency_rule に tiers が設定されている（旧: null）。"""
        funded = yaml_config["tradeify_compliance"]["funded"]
        assert funded["consistency_rule"] is not None
        assert "tiers" in funded["consistency_rule"]
        tiers = funded["consistency_rule"]["tiers"]
        assert len(tiers) == 3

    def test_pro_rush_not_in_excluded_tactics(self, yaml_config):
        """excluded_tactics に pro_rush が含まれていないこと（Builder 廃止済み）。"""
        excluded = yaml_config["tradeify_compliance"]["excluded_tactics"]
        assert "pro_rush" not in excluded
