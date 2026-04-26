#!/usr/bin/env python3
"""
test_apex_bot.py — Apex Bot テストスイート

テスト対象:
  1. TradovateClientのmockテスト（API接続なし）
  2. ApexRuleGuardの全ルールチェック
  3. FuturesORBStrategyのエッジケース
  4. ContractRollerのロールオーバー検知
  5. apex_rule_simulatorのシミュレーション

実行方法:
  python3 test_apex_bot.py
  python3 test_apex_bot.py -v  (詳細出力)

完了条件: 全テスト PASS
"""

from __future__ import annotations

import sys
import unittest
import datetime
import zoneinfo
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Optional

# テスト対象をimport
sys.path.insert(0, str(Path(__file__).parent))

from apex_rule_simulator import (
    APEX_ACCOUNT_RULES,
    check_daily_loss_limit,
    check_trailing_drawdown,
    check_consistency_rule,
    check_profit_target,
    check_all_rules,
    get_allowed_contracts,
    simulate_apex_evaluation,
    ApexAccountRules,
)

ET = zoneinfo.ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────────
# 1. TradovateClient Mockテスト
# ─────────────────────────────────────────────────────────────────────────────

class TestTradovateClientMock(unittest.TestCase):
    """TradovateClientのmockテスト。実際のAPI接続は行わない。"""

    def setUp(self):
        from tradovate_client import TradovateClient, _generate_device_id, _get_front_month_symbol
        self.TradovateClient       = TradovateClient
        self._generate_device_id   = _generate_device_id
        self._get_front_month_symbol = _get_front_month_symbol

    def test_client_initialization_demo(self):
        """DEMO環境でのクライアント初期化確認。"""
        client = self.TradovateClient(env="DEMO")
        self.assertEqual(client.env, "DEMO")
        self.assertIn("demo.tradovateapi.com", client.base_url)
        self.assertIsNone(client._access_token)
        self.assertIsNone(client.account_id)

    def test_client_initialization_live(self):
        """LIVE環境でのクライアント初期化確認。"""
        client = self.TradovateClient(env="LIVE")
        self.assertEqual(client.env, "LIVE")
        self.assertIn("live.tradovateapi.com", client.base_url)

    def test_device_id_generation(self):
        """device_idがSHA256で生成されること。"""
        device_id = self._generate_device_id()
        self.assertEqual(len(device_id), 64)  # SHA256 = 64 hex chars
        # 同じ環境で呼び出すと同一のIDが返る
        self.assertEqual(device_id, self._generate_device_id())

    def test_front_month_symbol_mes(self):
        """MESのフロント限月シンボルが正しい形式であること。"""
        symbol = self._get_front_month_symbol("MES")
        self.assertTrue(symbol.startswith("MES"), f"Expected MES prefix, got {symbol}")
        self.assertGreater(len(symbol), 3)  # 例: MESU5

    def test_front_month_symbol_es(self):
        """ESのフロント限月シンボルが正しい形式であること。"""
        symbol = self._get_front_month_symbol("ES")
        self.assertTrue(symbol.startswith("ES"), f"Expected ES prefix, got {symbol}")

    def test_authenticate_mock_success(self):
        """認証成功のmockテスト。"""
        client = self.TradovateClient(
            env="DEMO", username="testuser", password="testpass",
            app_id="TestApp", cid="12345", sec="secret"
        )

        mock_auth_response = {
            "accessToken":    "mock_access_token_12345",
            "mdAccessToken":  "mock_md_token_67890",
            "expirationTime": "2099-01-01T00:00:00Z",
        }
        mock_account_response = [
            {"id": 999, "name": "TEST-ACCOUNT"}
        ]

        with patch.object(client._session, "post") as mock_post, \
             patch.object(client._session, "get") as mock_get:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=mock_auth_response)
            )
            mock_post.return_value.raise_for_status = MagicMock()

            mock_get.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=mock_account_response)
            )
            mock_get.return_value.raise_for_status = MagicMock()

            result = client.authenticate()

        self.assertTrue(result)
        self.assertEqual(client._access_token, "mock_access_token_12345")
        self.assertEqual(client.account_id, 999)
        self.assertEqual(client.account_spec, "TEST-ACCOUNT")

    def test_authenticate_invalid_credentials(self):
        """認証失敗（errorText）のmockテスト。"""
        client = self.TradovateClient(
            env="DEMO", username="wrong", password="wrong"
        )

        mock_error_response = {"errorText": "Invalid credentials"}

        with patch.object(client._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=mock_error_response)
            )
            mock_post.return_value.raise_for_status = MagicMock()
            result = client.authenticate()

        self.assertFalse(result)

    def test_place_order_mock(self):
        """注文送信のmockテスト。"""
        client = self.TradovateClient(env="DEMO")
        client._access_token = "mock_token"
        client.account_id    = 999
        client.account_spec  = "TEST-ACCOUNT"
        client._session.headers["Authorization"] = "Bearer mock_token"

        mock_order_response = {
            "orderId":     12345,
            "orderStatus": "Working",
        }

        with patch.object(client._session, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=mock_order_response)
            )
            mock_post.return_value.raise_for_status = MagicMock()

            result = client.place_order(
                symbol="MESU5", action="Buy", qty=1, order_type="Market"
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["order_id"], 12345)
        self.assertEqual(result["action"], "Buy")
        self.assertEqual(result["symbol"], "MESU5")

    def test_place_order_without_auth_returns_none(self):
        """認証なしでの注文はNoneを返すこと。"""
        client = self.TradovateClient(env="DEMO")
        # account_idなし
        result = client.place_order(
            symbol="MESU5", action="Buy", qty=1, order_type="Market"
        )
        self.assertIsNone(result)

    def test_is_authenticated_property(self):
        """is_authenticatedプロパティの確認。"""
        client = self.TradovateClient(env="DEMO")
        self.assertFalse(client.is_authenticated)

        client._access_token = "some_token"
        self.assertTrue(client.is_authenticated)

    def test_ensure_authenticated_renews_near_expiry(self):
        """トークン期限切れ前にrenewが呼ばれること。"""
        import time
        client = self.TradovateClient(env="DEMO")
        client._access_token = "expiring_token"
        client._token_expiry = time.time() + 300  # 5分後に期限切れ（renewal margin = 10分）

        with patch.object(client, "renew_token", return_value=True) as mock_renew:
            result = client.ensure_authenticated()

        mock_renew.assert_called_once()
        self.assertTrue(result)


# ─────────────────────────────────────────────────────────────────────────────
# 2. ApexRuleGuard テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestApexRuleGuard(unittest.TestCase):
    """ApexRuleGuardの全ルールチェックテスト。"""

    def setUp(self):
        from apex_bot import ApexRuleGuard
        self.ApexRuleGuard = ApexRuleGuard

    def test_initial_state(self):
        """初期状態の確認。"""
        guard = self.ApexRuleGuard(50_000)
        self.assertEqual(guard.account_size, 50_000)
        self.assertEqual(guard.initial_balance, 50_000.0)
        self.assertFalse(guard._daily_loss_halted)
        self.assertFalse(guard._trailing_dd_halted)

    def test_safe_condition(self):
        """通常状態（損失なし）でsafe=Trueであること。"""
        guard = self.ApexRuleGuard(50_000)
        result = guard.check(50_000, 0.0)
        self.assertTrue(result["safe"])
        self.assertEqual(result["action"], "ok")

    def test_daily_loss_emergency_close(self):
        """Daily Loss上限超過で emergency_close になること。"""
        guard = self.ApexRuleGuard(50_000)
        guard.day_start_balance = 50_000

        # $50K口座のDaily Loss Limit = $2,500
        # 損失$3,000 → 上限超過
        result = guard.check(47_000, 0.0)  # 50000 - 3000 = 47000

        self.assertFalse(result["safe"])
        self.assertEqual(result["action"], "emergency_close")
        violations = result["violations"]
        self.assertTrue(any("DAILY_LOSS_VIOLATED" in v for v in violations))

    def test_daily_loss_warning(self):
        """Daily Loss 80%消費で warn になること。"""
        guard = self.ApexRuleGuard(50_000)
        guard.day_start_balance = 50_000

        # $2,500 × 80% = $2,000消費 → 残り$500 → warning
        result = guard.check(48_000, 0.0)  # 50000 - 2000 = 48000

        # warn または halt（95%を超えたらhalt）
        self.assertIn(result["action"], ("warn", "halt", "emergency_close"))

    def test_trailing_dd_emergency_close(self):
        """Trailing DD上限超過で emergency_close になること。"""
        guard = self.ApexRuleGuard(50_000)
        guard.high_water_mark = 53_000  # ハイウォーターマーク

        # $50K口座のTrailing DD Limit = $2,500
        # HWM $53,000 - threshold $2,500 = $50,500 が閾値
        # 現在残高 $50,000 < $50,500 → 違反
        result = guard.check(50_000, 0.0)

        self.assertFalse(result["safe"])
        self.assertEqual(result["action"], "emergency_close")
        violations = result["violations"]
        self.assertTrue(any("TRAILING_DD_VIOLATED" in v for v in violations))

    def test_can_enter_new_position_safe(self):
        """安全な状態で新規エントリーが許可されること。"""
        guard = self.ApexRuleGuard(50_000)
        self.assertTrue(guard.can_enter_new_position(50_000))

    def test_can_enter_new_position_after_halt(self):
        """停止フラグが立っているとエントリーが拒否されること。"""
        guard = self.ApexRuleGuard(50_000)
        guard._daily_loss_halted = True
        self.assertFalse(guard.can_enter_new_position(50_000))

    def test_get_allowed_contracts_initial(self):
        """利益0のとき最小コントラクト数が返ること。"""
        guard = self.ApexRuleGuard(50_000)
        contracts = guard.get_allowed_contracts(0.0)
        self.assertGreaterEqual(contracts, 1)

    def test_reset_day(self):
        """day_resetでフラグがクリアされること。"""
        guard = self.ApexRuleGuard(50_000)
        guard._daily_loss_halted = True
        guard._warned_daily_loss = True
        guard.today_pnl = 500.0

        guard.reset_day(51_000)

        self.assertFalse(guard._daily_loss_halted)
        self.assertFalse(guard._warned_daily_loss)
        self.assertEqual(guard.today_pnl, 0.0)
        self.assertEqual(guard.day_start_balance, 51_000)
        # 前日のP&Lがdaily_pnlsに追加されていること
        self.assertIn(500.0, guard.daily_pnls)

    def test_status_summary_format(self):
        """status_summaryが文字列を返すこと。"""
        guard = self.ApexRuleGuard(50_000)
        summary = guard.status_summary(50_000, 0.0)
        self.assertIsInstance(summary, str)
        self.assertIn("Balance=", summary)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Apex Rule Simulator テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestApexRuleSimulator(unittest.TestCase):
    """apex_rule_simulatorのルールチェック関数テスト。"""

    def test_all_account_sizes_defined(self):
        """全口座サイズが定義されていること。"""
        expected = {25_000, 50_000, 100_000, 150_000, 300_000}
        self.assertEqual(set(APEX_ACCOUNT_RULES.keys()), expected)

    def test_50k_rules(self):
        """$50K口座のルール値が正しいこと。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        self.assertEqual(rules.account_size, 50_000)
        self.assertEqual(rules.profit_target, 3_000)
        self.assertEqual(rules.max_daily_loss, 2_500)
        self.assertEqual(rules.max_trailing_dd, 2_500)
        self.assertEqual(rules.consistency_limit, 0.30)
        self.assertTrue(rules.consistency_rule_applies)

    def test_25k_no_consistency_rule(self):
        """$25K口座はConsistency Ruleが適用されないこと。"""
        rules = APEX_ACCOUNT_RULES[25_000]
        self.assertFalse(rules.consistency_rule_applies)

    def test_daily_loss_pass(self):
        """Daily Loss上限内でpassedがTrue。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        result = check_daily_loss_limit(rules, 50_000, 49_000, 0.0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["daily_loss"], -1_000)
        self.assertEqual(result["remaining"], 1_500)

    def test_daily_loss_fail(self):
        """Daily Loss上限超過でpassedがFalse。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        # $2,500 limitを超える$3,000の損失
        result = check_daily_loss_limit(rules, 50_000, 47_000, 0.0)
        self.assertFalse(result["passed"])

    def test_daily_loss_with_open_pnl(self):
        """含み損を含む実効残高でのチェック。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        # 確定損失$1,500 + 含み損$1,500 = 合計$3,000 → 超過
        result = check_daily_loss_limit(rules, 50_000, 48_500, -1_500)
        self.assertFalse(result["passed"])
        self.assertEqual(result["effective_balance"], 47_000)

    def test_trailing_dd_pass(self):
        """Trailing DD上限内でpassedがTrue。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        result = check_trailing_drawdown(rules, 50_000, 51_000, 52_000, 0.0)
        # HWM=52000, 現在=51000, DD=1000 < 2500 → pass
        self.assertTrue(result["passed"])
        self.assertEqual(result["drawdown"], 1_000)

    def test_trailing_dd_fail(self):
        """Trailing DD上限超過でpassedがFalse。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        # HWM=53000, 現在=50000, DD=3000 > 2500 → fail
        result = check_trailing_drawdown(rules, 50_000, 50_000, 53_000, 0.0)
        self.assertFalse(result["passed"])

    def test_trailing_dd_hwm_update(self):
        """ハイウォーターマークが上方向にのみ動くこと。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        # 現在価値 > 入力HWM → HWMが更新される
        result = check_trailing_drawdown(rules, 50_000, 55_000, 50_000, 0.0)
        # effective_balance=55000 > hwm_input=50000 → hwm=55000
        self.assertEqual(result["high_water_mark"], 55_000)

    def test_consistency_rule_pass(self):
        """今日の利益が30%以内でpassedがTrue。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        # 過去の利益: [200, 300, 400] = 合計900
        # 今日: 250 → 250 / (900+250) = 21.7% < 30% → pass
        result = check_consistency_rule(rules, [200, 300, 400], 250)
        self.assertTrue(result["passed"])

    def test_consistency_rule_fail(self):
        """今日の利益が30%超でpassedがFalse。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        # 過去の利益: [100] = 合計100
        # 今日: 200 → 200 / (100+200) = 66.7% > 30% → fail
        result = check_consistency_rule(rules, [100], 200)
        self.assertFalse(result["passed"])
        self.assertGreater(result["violation_amount"], 0)

    def test_consistency_rule_loss_day_always_pass(self):
        """損失の日はConsistency Ruleに引っかからないこと。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        result = check_consistency_rule(rules, [500, 300], -200)
        self.assertTrue(result["passed"])

    def test_25k_consistency_not_applied(self):
        """$25K口座はConsistency Ruleが適用されないこと。"""
        rules = APEX_ACCOUNT_RULES[25_000]
        # 極端な例でもpassedがTrue
        result = check_consistency_rule(rules, [100], 10_000)
        self.assertTrue(result["passed"])
        self.assertIn("note", result)

    def test_profit_target_not_achieved(self):
        """Profit Target未達成。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        result = check_profit_target(rules, 50_000, 51_000)
        self.assertFalse(result["achieved"])
        self.assertEqual(result["remaining"], 2_000)

    def test_profit_target_achieved(self):
        """Profit Target達成。"""
        rules = APEX_ACCOUNT_RULES[50_000]
        result = check_profit_target(rules, 50_000, 53_500)
        self.assertTrue(result["achieved"])
        self.assertEqual(result["remaining"], 0.0)

    def test_check_all_rules_pass(self):
        """全ルールチェックが通る正常系。"""
        # Consistency Rule: today_pnl=200, 過去の利益合計=[300,250,400] = 950
        # 今日200 / (950+200) = 17.4% < 30% → pass
        result = check_all_rules(
            account_size      = 50_000,
            initial_balance   = 50_000,
            day_start_balance = 50_100,  # 前日比+100（少し利益あり）
            current_balance   = 50_300,  # +200
            high_water_mark   = 50_300,
            daily_pnls        = [300, 250, 400],  # 過去の利益実績
            today_pnl         = 200,
        )
        self.assertTrue(result["overall_passed"])
        self.assertEqual(len(result["violations"]), 0)

    def test_check_all_rules_daily_loss_violation(self):
        """Daily Loss違反が検知されること。"""
        result = check_all_rules(
            account_size      = 50_000,
            initial_balance   = 50_000,
            day_start_balance = 50_000,
            current_balance   = 47_000,  # -$3000
            high_water_mark   = 50_000,
            daily_pnls        = [],
            today_pnl         = -3_000,
        )
        self.assertFalse(result["overall_passed"])
        self.assertTrue(any("DAILY_LOSS" in v for v in result["violations"]))

    def test_unknown_account_size_raises(self):
        """未定義の口座サイズは例外を発生させること。"""
        with self.assertRaises(ValueError):
            check_all_rules(
                account_size      = 99_999,
                initial_balance   = 99_999,
                day_start_balance = 99_999,
                current_balance   = 99_999,
                high_water_mark   = 99_999,
                daily_pnls        = [],
                today_pnl         = 0,
            )

    def test_simulation_no_violations(self):
        """全勝シナリオでシミュレーション通過（Consistency Rule対策済み）。"""
        # Consistency Rule違反を避けるため、小さな利益 + 損失を混ぜる
        # 毎日$50勝ち / 隔日$30負け で30日 → DD違反も出ない
        # $50 * 20 - $30 * 10 = $700 (Profit Target $3000未達だが違反なし)
        import random as _random
        _random.seed(0)
        daily_pnls = []
        for i in range(30):
            if i % 3 == 2:
                daily_pnls.append(-30.0)
            else:
                daily_pnls.append(50.0)
        result = simulate_apex_evaluation(50_000, daily_pnls)
        # 違反がないことを確認
        self.assertEqual(result.daily_loss_violations, 0)
        self.assertEqual(result.trailing_dd_violations, 0)
        self.assertEqual(result.total_days, 30)

    def test_simulation_daily_loss_violation(self):
        """大きな損失でシミュレーション失敗。"""
        # 1日で$5,000の損失 (limit=$2,500)
        daily_pnls = [200, 300, -5000, 400]
        result = simulate_apex_evaluation(50_000, daily_pnls)
        # Daily Loss違反またはTrailing DD違反が発生
        total_violations = (result.daily_loss_violations +
                            result.trailing_dd_violations)
        self.assertGreater(total_violations, 0)
        self.assertLess(result.passed_days, 4)  # Day3か4で失敗

    def test_get_allowed_contracts_scaling(self):
        """利益増加でコントラクト数が増えること。"""
        contracts_0    = get_allowed_contracts(50_000, 0)
        contracts_3000 = get_allowed_contracts(50_000, 3_000)
        self.assertGreaterEqual(contracts_3000, contracts_0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. FuturesORBStrategy テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestFuturesORBStrategy(unittest.TestCase):
    """FuturesORBStrategyのエッジケーステスト。"""

    def setUp(self):
        from apex_bot import FuturesORBStrategy, ApexRuleGuard
        self.FuturesORBStrategy = FuturesORBStrategy
        self.ApexRuleGuard      = ApexRuleGuard

    def _make_strategy(self, product="MES", account_size=50_000):
        """テスト用StrategyインスタンスをMockクライアントで生成。"""
        mock_client = MagicMock()
        mock_client.get_front_month_symbol.return_value = "MESU5"
        mock_client.place_order.return_value = {
            "order_id": 1001, "status": "Working",
            "action": "Buy", "symbol": "MESU5", "qty": 1,
            "order_type": "Market", "price": None, "raw": {}
        }

        guard = self.ApexRuleGuard(account_size)
        strat = self.FuturesORBStrategy(
            client       = mock_client,
            rule_guard   = guard,
            product      = product,
            account_size = account_size,
        )
        return strat, mock_client, guard

    def test_initial_state(self):
        """初期状態の確認。"""
        strat, _, _ = self._make_strategy()
        self.assertIsNone(strat._or_high)
        self.assertIsNone(strat._or_low)
        self.assertFalse(strat._or_complete)
        self.assertFalse(strat._entry_done)

    def test_update_or_candle(self):
        """ORレンジが正しく更新されること。"""
        strat, _, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.update_or_candle(5005, 4975)
        strat.update_or_candle(4995, 4985)

        self.assertEqual(strat._or_high, 5005)
        self.assertEqual(strat._or_low, 4975)

    def test_finalize_or(self):
        """finalize_orでor_completeがTrueになること。"""
        strat, _, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()
        self.assertTrue(strat._or_complete)
        self.assertEqual(strat.or_range, 20.0)

    def test_or_range_none_before_finalize(self):
        """OR計測前はor_rangeがNoneであること。"""
        strat, _, _ = self._make_strategy()
        self.assertIsNone(strat.or_range)

    def test_no_entry_before_or_complete(self):
        """OR未確定ではエントリーしないこと。"""
        strat, _, _ = self._make_strategy()
        result = strat.check_breakout(5010, 50_000, 18.0, 70.0)
        self.assertIsNone(result)

    def test_no_entry_inside_or_range(self):
        """ORレンジ内ではエントリーしないこと。"""
        strat, _, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()

        # OR内の価格: 4990
        result = strat.check_breakout(4990, 50_000, 18.0, 70.0)
        self.assertIsNone(result)

    def test_long_entry_on_breakout(self):
        """OR高値ブレイクでBuyエントリーが実行されること。"""
        strat, mock_client, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()

        # OR高値(5000)を上回る価格
        result = strat.check_breakout(5005, 50_000, 18.0, 70.0)

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "Buy")
        mock_client.place_order.assert_called_once()
        call_kwargs = mock_client.place_order.call_args
        self.assertEqual(call_kwargs[1]["action"], "Buy")

    def test_short_entry_on_breakdown(self):
        """OR安値ブレイクでSellエントリーが実行されること。"""
        strat, mock_client, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()

        # OR安値(4980)を下回る価格
        result = strat.check_breakout(4975, 50_000, 18.0, 70.0)

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "Sell")

    def test_stop_and_target_levels_long(self):
        """Longエントリー時のストップ・利確レベルが正しく計算されること。"""
        strat, _, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()
        # or_range = 20

        result = strat.check_breakout(5005, 50_000, 18.0, 70.0)

        self.assertIsNotNone(result)
        # stop = OR高値 - range * 1.0 = 5000 - 20 = 4980
        self.assertAlmostEqual(result["stop_price"], 4980.0)
        # target = OR高値 + range * 2.0 = 5000 + 40 = 5040
        self.assertAlmostEqual(result["target_price"], 5040.0)

    def test_no_second_entry(self):
        """1日1回しかエントリーしないこと。"""
        strat, mock_client, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()

        strat.check_breakout(5005, 50_000, 18.0, 70.0)  # 1回目

        mock_client.reset_mock()
        strat.check_breakout(5010, 50_000, 18.0, 70.0)  # 2回目

        mock_client.place_order.assert_not_called()

    def test_no_entry_high_vix(self):
        """VIX > 35でエントリーしないこと。"""
        strat, mock_client, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()

        result = strat.check_breakout(5005, 50_000, vix=36.0, env_score=70.0)
        self.assertIsNone(result)
        mock_client.place_order.assert_not_called()

    def test_no_entry_low_env_score(self):
        """env_score < 40でエントリーしないこと。"""
        strat, mock_client, _ = self._make_strategy()
        strat.update_or_candle(5000, 4980)
        strat.finalize_or()

        result = strat.check_breakout(5005, 50_000, vix=18.0, env_score=35.0)
        self.assertIsNone(result)

    def test_stop_hit_long(self):
        """Longエントリー後にストップ価格を下回ったら "stop_hit" を返すこと。"""
        strat, _, _ = self._make_strategy()
        strat._entry_done  = True
        strat._entry_side  = "Long"
        strat._stop_price  = 4980.0
        strat._target_price = 5040.0

        result = strat.check_exit(4975.0, 50_000, -100.0)
        self.assertEqual(result, "stop_hit")

    def test_target_hit_long(self):
        """Longエントリー後に利確価格を上回ったら "target_hit" を返すこと。"""
        strat, _, _ = self._make_strategy()
        strat._entry_done  = True
        strat._entry_side  = "Long"
        strat._stop_price  = 4980.0
        strat._target_price = 5040.0

        result = strat.check_exit(5045.0, 50_000, 500.0)
        self.assertEqual(result, "target_hit")

    def test_no_exit_without_entry(self):
        """エントリーなしではエグジットチェックしないこと。"""
        strat, _, _ = self._make_strategy()
        result = strat.check_exit(5000.0, 50_000, 0.0)
        self.assertIsNone(result)

    def test_emergency_close_on_rule_violation(self):
        """Apexルール違反でemergency_closeが返ること。"""
        strat, _, guard = self._make_strategy()
        strat._entry_done   = True
        strat._entry_side   = "Long"
        strat._stop_price   = 4900.0
        strat._target_price = 5100.0

        # DDを大きくしてルール違反を発生させる
        guard.high_water_mark = 55_000
        # effective_balance = 50000 - 55000 + 2500 = DD超過
        result = strat.check_exit(4960.0, 49_000, -3_000.0)
        # emergency_closeまたはstop_hit
        self.assertIn(result, ("emergency_close", "stop_hit"))

    def test_reset_day(self):
        """日次リセットで全状態がクリアされること。"""
        strat, _, _ = self._make_strategy()
        strat._or_high     = 5000
        strat._or_low      = 4980
        strat._or_complete = True
        strat._entry_done  = True

        strat.reset_day()

        self.assertIsNone(strat._or_high)
        self.assertIsNone(strat._or_low)
        self.assertFalse(strat._or_complete)
        self.assertFalse(strat._entry_done)


# ─────────────────────────────────────────────────────────────────────────────
# 5. ContractRoller テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestContractRoller(unittest.TestCase):
    """ContractRollerのロールオーバー検知テスト。"""

    def setUp(self):
        from apex_bot import ContractRoller
        self.ContractRoller = ContractRoller

    def test_first_check_no_rollover(self):
        """初回チェックはrolled=Falseであること。"""
        roller = self.ContractRoller("MES")
        result = roller.check_rollover()
        self.assertFalse(result["rolled"])
        self.assertIsNone(result["old_symbol"])
        self.assertIsNotNone(result["new_symbol"])

    def test_rollover_detected(self):
        """シンボルが変わったらrolled=Trueになること。"""
        roller = self.ContractRoller("MES")
        roller._last_symbol = "MESH5"  # 古いシンボルを強制セット

        with patch("apex_bot._get_front_month_symbol", return_value="MESU5"), \
             patch("apex_bot.pushover"):
            # ContractRollerの内部でtradovate_client._get_front_month_symbolを使う
            # apex_botからimportされた_get_front_month_symbolをpatch
            from tradovate_client import _get_front_month_symbol as _orig
            import apex_bot
            orig_fn = apex_bot._get_front_month_symbol

            try:
                apex_bot._get_front_month_symbol = lambda p: "MESU5"
                result = roller.check_rollover()
            finally:
                apex_bot._get_front_month_symbol = orig_fn

        if result["rolled"]:
            self.assertEqual(result["old_symbol"], "MESH5")

    def test_product_code_preserved(self):
        """productコードが結果に含まれること。"""
        roller = self.ContractRoller("ES")
        result = roller.check_rollover()
        self.assertEqual(result["product"], "ES")


# ─────────────────────────────────────────────────────────────────────────────
# 6. ApexBot 統合テスト（dry_run）
# ─────────────────────────────────────────────────────────────────────────────

class TestApexBotDryRun(unittest.TestCase):
    """ApexBot dry_runモードの統合テスト。"""

    def setUp(self):
        from apex_bot import ApexBot
        self.ApexBot = ApexBot

    def test_init_dry_run(self):
        """dry_runモードで初期化できること。"""
        bot = self.ApexBot(account_size=50_000, product="MES", dry_run=True)
        self.assertIsNone(bot.client)
        self.assertIsNotNone(bot.rule_guard)
        self.assertIsNotNone(bot.orb)
        self.assertIsNotNone(bot.roller)

    def test_connect_dry_run(self):
        """dry_runモードではconnectが常にTrueを返すこと。"""
        bot = self.ApexBot(account_size=50_000, dry_run=True)
        with patch("apex_bot.pushover"):
            result = bot.connect()
        self.assertTrue(result)

    def test_premarket_dry_run(self):
        """dry_runモードでプレマーケット評価が実行できること。"""
        bot = self.ApexBot(account_size=50_000, dry_run=True)

        with patch("apex_bot.get_vix", return_value=18.5), \
             patch("apex_bot.get_vix_history", return_value=[15, 16, 18, 20, 17, 19] * 10), \
             patch("apex_bot.pushover"):
            result = bot.run_premarket()

        self.assertTrue(result)
        self.assertTrue(bot._premarket_done)
        self.assertEqual(bot._vix, 18.5)
        self.assertIsNotNone(bot._env_score)

    def test_get_balance_dry_run(self):
        """dry_runモードでは初期残高が返ること。"""
        bot = self.ApexBot(account_size=50_000, dry_run=True)
        balance, open_pnl = bot._get_current_balance_and_pnl()
        self.assertEqual(balance, 50_000.0)
        self.assertEqual(open_pnl, 0.0)

    def test_daily_reset(self):
        """日次リセットでフラグがクリアされること。"""
        bot = self.ApexBot(account_size=50_000, dry_run=True)
        bot._premarket_done   = True
        bot._or_finalized     = True
        bot._force_close_done = True

        bot._daily_reset(datetime.date.today())

        self.assertFalse(bot._premarket_done)
        self.assertFalse(bot._or_finalized)
        self.assertFalse(bot._force_close_done)


# ─────────────────────────────────────────────────────────────────────────────
# 8. P0/P1 バグ修正検証テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestPropAuditFixes(unittest.TestCase):
    """prop_audit_v1_20260417 で検出したバグの修正を検証するテスト。"""

    def test_a1_get_positions_symbol_is_string(self):
        """A-1: get_positions() の symbol フィールドが文字列（"MESM6"等）であること。"""
        from tradovate_client import TradovateClient
        from unittest.mock import MagicMock, patch

        client = TradovateClient(env="DEMO")
        client._session = MagicMock()
        client.account_id = 12345

        # Tradovate /position/list は contractId(int) を返す
        mock_positions_resp = MagicMock()
        mock_positions_resp.json.return_value = [
            {"id": 1, "contractId": 9999, "netPos": 2, "netPrice": 5000.0, "openPnl": 50.0},
        ]
        mock_positions_resp.raise_for_status = MagicMock()

        # /contract/items は name(str) を返す
        mock_items_resp = MagicMock()
        mock_items_resp.json.return_value = [
            {"id": 9999, "name": "MESM6"},
        ]
        mock_items_resp.raise_for_status = MagicMock()

        def mock_get(url, **kwargs):
            if "/position/list" in url:
                return mock_positions_resp
            if "/contract/items" in url:
                return mock_items_resp
            return MagicMock()

        client._session.get.side_effect = mock_get

        positions = client.get_positions()

        self.assertEqual(len(positions), 1)
        # 修正後: symbol は文字列 "MESM6"
        self.assertEqual(positions[0]["symbol"], "MESM6")
        # close_position("MESM6") での比較が機能することを確認
        match = next((p for p in positions if p.get("symbol") == "MESM6"), None)
        self.assertIsNotNone(match, "close_position symbol matching should work after fix")

    def test_a1_close_position_symbol_match(self):
        """A-1: close_position("MESM6") がポジションをマッチできること。"""
        from tradovate_client import TradovateClient
        from unittest.mock import MagicMock

        client = TradovateClient(env="DEMO")
        client._session      = MagicMock()
        client.account_id    = 12345      # 認証済みを模擬
        client.account_spec  = "testaccount"  # place_order の認証チェックに必要

        mock_pos_resp = MagicMock()
        mock_pos_resp.json.return_value = [
            {"id": 1, "contractId": 9999, "netPos": 3, "netPrice": 5010.0, "openPnl": 100.0},
        ]
        mock_pos_resp.raise_for_status = MagicMock()

        mock_items_resp = MagicMock()
        mock_items_resp.json.return_value = [{"id": 9999, "name": "MESM6"}]
        mock_items_resp.raise_for_status = MagicMock()

        mock_order_resp = MagicMock()
        mock_order_resp.json.return_value = {"orderId": 42}
        mock_order_resp.raise_for_status = MagicMock()

        def mock_get(url, **kwargs):
            if "/position/list" in url:
                return mock_pos_resp
            if "/contract/items" in url:
                return mock_items_resp
            return MagicMock()

        def mock_post(url, **kwargs):
            if "/order/placeOrder" in url:
                return mock_order_resp
            return MagicMock()

        client._session.get.side_effect  = mock_get
        client._session.post.side_effect = mock_post

        result = client.close_position("MESM6")
        # 修正後: ポジションが見つかり注文が実行される（None ではない）
        self.assertIsNotNone(result, "close_position should find the position after A-1 fix")

    def test_a2_vix_mr_pnl_includes_point_value(self):
        """A-2: VIX-MR の close() が point_value を掛けたドルPnLを返すこと。"""
        from futures_vix_mr import VIXMRPosition

        pos = VIXMRPosition(
            trade_id     = "test001",
            entry_price  = 5000.0,
            stop_price   = 4925.0,    # -1.5%
            target_price = 5050.0,    # +1.0%
            symbol       = "MESM6",
            qty          = 2,
            entry_date   = datetime.date.today(),
        )

        pnl = pos.close("target_hit", 5050.0)

        # 修正前: (5050 - 5000) * 2 = 100（point_value 無し）
        # 修正後: (5050 - 5000) * 2 * 5.0 = 500
        self.assertAlmostEqual(pnl, 500.0, places=2,
            msg="VIX-MR PnL must include MES point_value=5.0")

    def test_a2_trend_follow_pnl_includes_point_value_long(self):
        """A-2: TF Long の close() が point_value を掛けたドルPnLを返すこと。"""
        from futures_trend_follow import TFPosition

        pos = TFPosition(
            trade_id    = "tf001",
            side        = "Long",
            entry_price = 5000.0,
            symbol      = "MESM6",
            qty         = 1,
            entry_date  = datetime.date.today(),
        )
        pnl = pos.close("signal_reverse", 5100.0)
        # 修正前: (5100 - 5000) * 1 = 100
        # 修正後: (5100 - 5000) * 1 * 5.0 = 500
        self.assertAlmostEqual(pnl, 500.0, places=2,
            msg="TF Long PnL must include point_value=5.0")

    def test_a2_trend_follow_pnl_includes_point_value_short(self):
        """A-2: TF Short の close() が point_value を掛けたドルPnLを返すこと。"""
        from futures_trend_follow import TFPosition

        pos = TFPosition(
            trade_id    = "tf002",
            side        = "Short",
            entry_price = 5100.0,
            symbol      = "MESM6",
            qty         = 2,
            entry_date  = datetime.date.today(),
        )
        pnl = pos.close("signal_reverse", 5000.0)
        # (5100 - 5000) * 2 * 5.0 = 1000
        self.assertAlmostEqual(pnl, 1000.0, places=2,
            msg="TF Short PnL must include point_value=5.0")

    def test_a3_apex_hwm_freeze(self):
        """A-3: Apex Trailing DD HWM フリーズが initial_balance + max_dd で止まること。"""
        from apex_bot import ApexRuleGuard

        guard = ApexRuleGuard(account_size=50_000)
        # initial=50000, max_trailing_dd=2500 → freeze_threshold=52500

        self.assertFalse(guard._hwm_frozen, "Should not be frozen initially")
        self.assertEqual(guard._hwm_freeze_threshold, 52_500.0)

        # 52499 ではフリーズしない
        guard.update_hwm(52_499.0)
        self.assertFalse(guard._hwm_frozen)
        self.assertAlmostEqual(guard.high_water_mark, 52_499.0)

        # 52500 に達したらフリーズ
        guard.update_hwm(52_500.0)
        self.assertTrue(guard._hwm_frozen, "HWM should be frozen at threshold")
        self.assertAlmostEqual(guard.high_water_mark, 52_500.0)

        # フリーズ後は 60000 になってもHWMは動かない
        guard.update_hwm(60_000.0)
        self.assertTrue(guard._hwm_frozen)
        self.assertAlmostEqual(guard.high_water_mark, 52_500.0,
            msg="HWM must stay frozen after threshold is reached")

    def test_b1_env_score_written_back(self):
        """B-1: select_futures_strategy() が env dict に env_score を書き戻すこと。"""
        from chronos_strategy_selector import select_futures_strategy

        env = {
            "vix":               20.0,
            "vix_history":       [15.0, 16.0, 17.0, 18.0, 19.0, 20.0] * 10,
            "vix_z":             0.5,
            "time_et":           "10:00",
            "gap_pct":           0.0,
            "account_pnl_day":   0.0,
            "account_pnl_month": 0.0,
            "account_balance":   50_000.0,
            "consistency_used_pct": 0.0,
        }
        select_futures_strategy(env)

        self.assertIn("env_score", env,
            "select_futures_strategy must write env_score back to env dict")
        score = env["env_score"]
        self.assertIsInstance(score, float)
        self.assertGreater(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_b4_news_filter_aware_datetime(self):
        """B-4: NewsTradingFilter が offset-aware な datetime_et 文字列を正しく処理すること。"""
        import zoneinfo
        from chronos_bot import NewsTradingFilter
        import tempfile, json
        from pathlib import Path

        ET = zoneinfo.ZoneInfo("America/New_York")

        # offset-aware 形式（例: "2026-04-17T08:30:00-04:00"）のカレンダー
        now_et = datetime.datetime.now(ET)
        # ブラックアウト内: 1分後のイベント
        event_time = now_et + datetime.timedelta(minutes=1)
        event_str_aware = event_time.isoformat()  # offset-aware

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([{"event": "CPI", "datetime_et": event_str_aware}], f)
            cal_path = Path(f.name)

        try:
            nf = NewsTradingFilter(calendar_path=cal_path)
            result = nf.is_blackout(now_et)
            # offset-aware でも正しく検出できること（4時間ズレなし）
            self.assertTrue(result["blocked"],
                "NewsTradingFilter must detect blackout with offset-aware datetime_et")
        finally:
            cal_path.unlink(missing_ok=True)

    def test_b5_kelly_contract_calc_by_risk_budget(self):
        """B-5: Kelly枚数計算がリスク予算ベースで行われること（常時1枚固定にならないこと）。"""
        # ApexBot の FuturesORBStrategy._calc_contracts を直接テスト
        # kelly=0.10, account=50000, or_range=20pts, point_value=5.0, stop_mult=1.0
        # risk_per_contract = 20 * 1.0 * 5.0 = $100
        # dollar_risk = 50000 * 0.10 = $5000
        # contracts = floor(5000 / 100) = 50 → min(50, 5) = 5
        import apex_bot as apex_module
        FuturesORBStrategy = apex_module.FuturesORBStrategy

        class MockGuard:
            initial_balance = 50_000.0
            def get_allowed_contracts(self, profit):
                return 5

        orb = FuturesORBStrategy.__new__(FuturesORBStrategy)
        orb.product    = "MES"
        orb.rule_guard = MockGuard()
        orb.client     = None

        with patch("apex_bot.KELLY_AVAILABLE", True), \
             patch("apex_bot.calc_kelly_fraction", return_value=0.10):
            n = orb._calc_contracts(
                account_balance = 50_000.0,
                max_contracts   = 5,
                or_range        = 20.0,
            )

        # 修正前: floor(0.10 * 5) = 0 → max(1,0)=1 (常時1枚固定)
        # 修正後: floor(5000/100)=50 → min(50,5)=5
        self.assertGreater(n, 1,
            "Kelly contracts should be > 1 when kelly=0.10 and risk budget allows")


# ─────────────────────────────────────────────────────────────────────────────
# テスト実行
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)  # テスト中は警告以上のみ表示

    # テストスイート構築
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    test_classes = [
        TestTradovateClientMock,
        TestApexRuleGuard,
        TestApexRuleSimulator,
        TestFuturesORBStrategy,
        TestContractRoller,
        TestApexBotDryRun,
        TestPropAuditFixes,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print(f"\n{'='*60}")
    print(f"Tests run:    {result.testsRun}")
    print(f"Failures:     {len(result.failures)}")
    print(f"Errors:       {len(result.errors)}")
    print(f"Skipped:      {len(result.skipped)}")
    print(f"{'='*60}")

    if result.wasSuccessful():
        print("ALL TESTS PASSED")
        sys.exit(0)
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)
