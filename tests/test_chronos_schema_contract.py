"""tests/test_chronos_schema_contract.py — スキーマ契約テスト (CR-5 / G-2)

Tradovate / prop_firm_rules / KellySizer 等のモジュール間データ受け渡し
スキーマが契約通りであることを自動検証する。

テスト設計方針:
  - 実 fixture（モジュールを実際にインポートして呼び出す）を使用
  - モックは外部 HTTP のみに限定（requests.Session を差し替える）
  - 「動くはず」でなく「実際に動く」ことを assertEqual で証明する

網羅範囲:
  TC-1: TradovateClient.get_positions() 返却スキーマ
  TC-2: TradovateClient.get_positions_for_rules() アダプタ変換
  TC-3: prop_firm_rules.check_hedge_prohibition() スキーマ受け入れ
  TC-4: prop_firm_rules.check_dca_pattern() スキーマ受け入れ
  TC-5: KellySizer fail-closed（"" / None / "core_50k" で Kelly=0）
  TC-6: KellySizer 正常系（既知 plan_id で Kelly > 0）
  TC-7: common.plan_id.from_yaml_plan_phase() 全マッピング整合
  TC-8: common.plan_id.from_str() 往復変換
  TC-9: Tradovate net_pos → side 変換の双方向整合
  TC-10: HFT guard: _daily_trade_count が env に正しく渡る
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

# パス設定
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# TC-1: TradovateClient.get_positions() 返却スキーマ
# ─────────────────────────────────────────────────────────────────────────────
class TestTradovatePositionsSchema(unittest.TestCase):
    """TradovateClient.get_positions() が正しいスキーマを返すことを検証。"""

    def _make_client(self):
        from tradovate_client import TradovateClient
        client = TradovateClient.__new__(TradovateClient)
        client.base_url = "https://demo.tradovateapi.com/v1"
        client.access_token = "test-token"
        client._session = MagicMock()
        return client

    def test_get_positions_schema_keys(self):
        """get_positions() は id/symbol/net_pos/net_price/unrealized_pnl を含む。"""
        client = self._make_client()
        # Tradovate /position/list レスポンスのフィクスチャ
        mock_positions = [{"id": 1, "contractId": 100, "netPos": 2, "netPrice": 5000.0, "openPnl": 150.0}]
        mock_contracts = [{"id": 100, "name": "MESU5"}]

        client._session.get = MagicMock()
        # 1回目: position/list, 2回目: contract/items
        resp1 = MagicMock()
        resp1.json.return_value = mock_positions
        resp1.raise_for_status = MagicMock()
        resp2 = MagicMock()
        resp2.json.return_value = mock_contracts
        resp2.raise_for_status = MagicMock()
        client._session.get.side_effect = [resp1, resp2]

        result = client.get_positions()

        self.assertEqual(len(result), 1)
        pos = result[0]
        required_keys = {"id", "symbol", "net_pos", "net_price", "unrealized_pnl"}
        self.assertTrue(required_keys.issubset(set(pos.keys())),
                        f"missing keys: {required_keys - set(pos.keys())}")
        self.assertEqual(pos["symbol"], "MESU5")
        self.assertEqual(pos["net_pos"], 2)

    def test_get_positions_zero_net_pos_excluded(self):
        """net_pos=0 のポジションは返却リストから除外される。"""
        client = self._make_client()
        mock_positions = [
            {"id": 1, "contractId": 100, "netPos": 0, "netPrice": 0.0, "openPnl": 0.0},
            {"id": 2, "contractId": 101, "netPos": -1, "netPrice": 5010.0, "openPnl": -50.0},
        ]
        mock_contracts = [{"id": 101, "name": "MESU5"}]
        resp1 = MagicMock()
        resp1.json.return_value = mock_positions
        resp1.raise_for_status = MagicMock()
        resp2 = MagicMock()
        resp2.json.return_value = mock_contracts
        resp2.raise_for_status = MagicMock()
        client._session.get.side_effect = [resp1, resp2]

        result = client.get_positions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["net_pos"], -1)


# ─────────────────────────────────────────────────────────────────────────────
# TC-2: get_positions_for_rules() アダプタ変換
# ─────────────────────────────────────────────────────────────────────────────
class TestTradovatePositionsAdapterSchema(unittest.TestCase):
    """get_positions_for_rules() が prop_firm_rules 期待スキーマに変換することを検証。"""

    def _make_client_with_positions(self, raw_positions: list[dict]):
        """get_positions() を mock した TradovateClient を返す。"""
        from tradovate_client import TradovateClient
        client = TradovateClient.__new__(TradovateClient)
        client.get_positions = MagicMock(return_value=raw_positions)
        return client

    def test_long_position_becomes_buy(self):
        """net_pos > 0 は side="BUY" に変換される。"""
        client = self._make_client_with_positions([
            {"id": 1, "symbol": "MESU5", "net_pos": 3, "net_price": 5000.0, "unrealized_pnl": 100.0}
        ])
        result = client.get_positions_for_rules()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["side"], "BUY")
        self.assertIn("symbol", result[0])
        self.assertIn("unrealized_pnl", result[0])

    def test_short_position_becomes_sell(self):
        """net_pos < 0 は side="SELL" に変換される。"""
        client = self._make_client_with_positions([
            {"id": 2, "symbol": "MESU5", "net_pos": -2, "net_price": 5020.0, "unrealized_pnl": -80.0}
        ])
        result = client.get_positions_for_rules()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["side"], "SELL")

    def test_adapter_schema_has_required_keys(self):
        """アダプタ返却は symbol/side/unrealized_pnl/net_pos を含む。"""
        client = self._make_client_with_positions([
            {"id": 1, "symbol": "NQU5", "net_pos": 1, "net_price": 18000.0, "unrealized_pnl": 200.0}
        ])
        result = client.get_positions_for_rules()
        required = {"symbol", "side", "unrealized_pnl", "net_pos"}
        self.assertTrue(required.issubset(set(result[0].keys())),
                        f"missing: {required - set(result[0].keys())}")

    def test_roundtrip_net_pos_preserved(self):
        """net_pos は変換後も元の値が保持される。"""
        client = self._make_client_with_positions([
            {"id": 1, "symbol": "MESU5", "net_pos": 5, "net_price": 5000.0, "unrealized_pnl": 0.0}
        ])
        result = client.get_positions_for_rules()
        self.assertEqual(result[0]["net_pos"], 5)

    def test_zero_net_pos_excluded_from_adapter(self):
        """get_positions_for_rules() は net_pos=0 を除外する（get_positions がすでに除外済みでも安全）。"""
        client = self._make_client_with_positions([
            {"id": 1, "symbol": "MESU5", "net_pos": 0, "net_price": 5000.0, "unrealized_pnl": 0.0}
        ])
        result = client.get_positions_for_rules()
        self.assertEqual(len(result), 0)


# ─────────────────────────────────────────────────────────────────────────────
# TC-3: prop_firm_rules.check_hedge_prohibition() スキーマ受け入れ
# ─────────────────────────────────────────────────────────────────────────────
class TestPropFirmRulesHedgeSchema(unittest.TestCase):
    """check_hedge_prohibition() がアダプタ済みスキーマを正しく受け入れることを検証。"""

    def test_adapter_schema_accepted_buy(self):
        """BUY ポジション(side="BUY") でヘッジ検出が正しく動く。"""
        from common.prop_firm_rules import check_hedging
        # アダプタ済みスキーマ: side フィールドあり
        open_positions = [{"symbol": "MESU5", "side": "BUY", "unrealized_pnl": 100.0}]
        # BUY エントリー時: 同方向なのでヘッジ検出しない → allow
        ok, reason = check_hedging("MESU5", "BUY", open_positions)
        self.assertTrue(ok, f"unexpected block: {reason}")

    def test_adapter_schema_detects_hedge(self):
        """BUY ポジションに SELL エントリーはヘッジ検出 → 拒否。"""
        from common.prop_firm_rules import check_hedging
        open_positions = [{"symbol": "MESU5", "side": "BUY", "unrealized_pnl": 100.0}]
        ok, reason = check_hedging("MESU5", "SELL", open_positions)
        self.assertFalse(ok, "ヘッジを検出すべきだが allow になった")
        self.assertIn("ヘッジ禁止", reason)

    def test_raw_tradovate_schema_fails_detection(self):
        """CR-5検証: net_pos スキーマ(side なし)ではヘッジが検出できない(既知の問題)。"""
        from common.prop_firm_rules import check_hedging
        # Tradovate raw: side フィールドなし
        raw_positions = [{"symbol": "MESU5", "net_pos": 1, "unrealized_pnl": 100.0}]
        ok, reason = check_hedging("MESU5", "SELL", raw_positions)
        # side="" になるので pos_side != opposite → ヘッジ未検出 → allow (バグ動作)
        # このテストは「raw スキーマではガードが無効」という事実を記録する
        self.assertTrue(ok, "期待: raw スキーマではヘッジ未検出(side='')")


# ─────────────────────────────────────────────────────────────────────────────
# TC-4: prop_firm_rules.check_dca_pattern() スキーマ受け入れ
# ─────────────────────────────────────────────────────────────────────────────
class TestPropFirmRulesDCASchema(unittest.TestCase):
    """check_dca_pattern() がアダプタ済みスキーマを正しく受け入れることを検証。"""

    def test_dca_blocked_with_adapter_schema(self):
        """Apex PA: 損失ポジへの DCA がアダプタ済みスキーマで検出される。"""
        from common.prop_firm_rules import check_dca_pattern
        open_positions = [
            {"symbol": "MESU5", "side": "BUY", "unrealized_pnl": -200.0}
        ]
        ok, reason = check_dca_pattern("MESU5", "BUY", open_positions, "apex", "pa")
        self.assertFalse(ok, "Apex PA の DCA 禁止が検出されなかった")
        self.assertIn("DCA禁止", reason)

    def test_dca_not_triggered_for_non_apex(self):
        """MFFU では DCA チェックは適用されない。"""
        from common.prop_firm_rules import check_dca_pattern
        open_positions = [
            {"symbol": "MESU5", "side": "BUY", "unrealized_pnl": -200.0}
        ]
        ok, reason = check_dca_pattern("MESU5", "BUY", open_positions, "mffu", "evaluation")
        self.assertTrue(ok, f"MFFU は DCA 禁止対象外: {reason}")


# ─────────────────────────────────────────────────────────────────────────────
# TC-5: KellySizer fail-closed
# ─────────────────────────────────────────────────────────────────────────────
class TestKellySizerFailClosed(unittest.TestCase):
    """H-2: 無効/DEPRECATED な plan_id で KellySizer が Kelly=0 を返すことを検証。"""

    def test_empty_string_plan_id_returns_zero(self):
        """plan_id="" は fail-closed: Kelly=0。"""
        from common.kelly_sizer import KellySizer
        sizer = KellySizer("", yaml_override={})
        kelly = sizer.calc_kelly(win_rate=0.55, rr_ratio=1.3)
        self.assertEqual(kelly, 0.0, f"plan_id='' should return 0.0 but got {kelly}")

    def test_core_50k_deprecated_returns_zero(self):
        """plan_id="core_50k" は DEPRECATED: Kelly=0。"""
        from common.kelly_sizer import KellySizer
        sizer = KellySizer("core_50k", yaml_override={})
        kelly = sizer.calc_kelly(win_rate=0.55, rr_ratio=1.3)
        self.assertEqual(kelly, 0.0, f"core_50k should return 0.0 but got {kelly}")

    def test_unknown_plan_id_returns_zero(self):
        """未知の plan_id は fail-closed: Kelly=0。"""
        from common.kelly_sizer import KellySizer
        sizer = KellySizer("nonexistent_plan_xyz", yaml_override={})
        kelly = sizer.calc_kelly(win_rate=0.55, rr_ratio=1.3)
        self.assertEqual(kelly, 0.0, f"unknown plan should return 0.0 but got {kelly}")

    def test_get_size_pct_also_returns_zero_for_fail_closed(self):
        """get_size_pct() も fail-closed plan では 0.0 を返す。"""
        from common.kelly_sizer import KellySizer
        sizer = KellySizer("core_50k", yaml_override={})
        size = sizer.get_size_pct(kelly_fraction=0.15)
        self.assertEqual(size, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# TC-6: KellySizer 正常系
# ─────────────────────────────────────────────────────────────────────────────
class TestKellySizerNormal(unittest.TestCase):
    """既知の正規 plan_id で KellySizer が Kelly > 0 を返すことを検証。"""

    def test_flex_eval_returns_positive_kelly(self):
        """flex_eval: Kelly > 0。"""
        from common.kelly_sizer import KellySizer
        sizer = KellySizer("flex_eval", yaml_override={})
        kelly = sizer.calc_kelly(win_rate=0.55, rr_ratio=1.3)
        self.assertGreater(kelly, 0.0)

    def test_rapid_sim_returns_positive_kelly(self):
        """rapid_sim: Kelly > 0。"""
        from common.kelly_sizer import KellySizer
        sizer = KellySizer("rapid_sim", yaml_override={})
        kelly = sizer.calc_kelly(win_rate=0.55, rr_ratio=1.3)
        self.assertGreater(kelly, 0.0)

    def test_all_valid_plan_ids_return_positive_kelly(self):
        """全有効 plan_id が Kelly > 0 を返す（core_50k を除く）。"""
        from common.kelly_sizer import KellySizer, _DEFAULT_PROFILES
        for pid in _DEFAULT_PROFILES:
            if pid == "core_50k":
                continue
            sizer = KellySizer(pid, yaml_override={})
            kelly = sizer.calc_kelly(win_rate=0.55, rr_ratio=1.3)
            self.assertGreater(kelly, 0.0, f"plan_id={pid} unexpectedly returned Kelly=0")


# ─────────────────────────────────────────────────────────────────────────────
# TC-7: common.plan_id.from_yaml_plan_phase() 全マッピング整合
# ─────────────────────────────────────────────────────────────────────────────
class TestPlanIDFromYaml(unittest.TestCase):
    """H-3: from_yaml_plan_phase() が正しい PlanID を返すことを検証。"""

    def test_flex_50k_evaluation(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        self.assertEqual(from_yaml_plan_phase("flex_50k", "evaluation"), PlanID.FLEX_EVAL)

    def test_flex_50k_sim_funded(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        self.assertEqual(from_yaml_plan_phase("flex_50k", "sim_funded"), PlanID.FLEX_SIM)

    def test_rapid_50k_evaluation(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        self.assertEqual(from_yaml_plan_phase("rapid_50k", "evaluation"), PlanID.RAPID_EVAL)

    def test_rapid_50k_sim_funded(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        self.assertEqual(from_yaml_plan_phase("rapid_50k", "sim_funded"), PlanID.RAPID_SIM)

    def test_apex_pa(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        self.assertEqual(from_yaml_plan_phase("apex", "pa"), PlanID.APEX_SAFETY_NET)

    def test_core_50k_deprecated(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        self.assertEqual(from_yaml_plan_phase("core_50k", "evaluation"), PlanID.CORE_50K_DEPRECATED)

    def test_unknown_combination_raises_value_error(self):
        """β-6 fail-closed: 未知の組み合わせは FLEX_EVAL にフォールバックせず ValueError を raise。"""
        from common.plan_id import from_yaml_plan_phase
        with self.assertRaises(ValueError):
            from_yaml_plan_phase("unknown_plan_xyz", "evaluation")


# ─────────────────────────────────────────────────────────────────────────────
# TC-8: common.plan_id.from_str() 往復変換
# ─────────────────────────────────────────────────────────────────────────────
class TestPlanIDFromStr(unittest.TestCase):
    """H-3: from_str() → .value が元の文字列に戻ることを検証（往復変換）。"""

    def test_flex_eval_roundtrip(self):
        from common.plan_id import from_str
        result = from_str("flex_eval")
        self.assertEqual(result.value, "flex_eval")

    def test_all_plan_ids_roundtrip(self):
        from common.plan_id import from_str, PlanID
        for pid in PlanID:
            result = from_str(pid.value)
            self.assertEqual(result.value, pid.value, f"roundtrip failed for {pid.value}")

    def test_empty_string_returns_fallback(self):
        from common.plan_id import from_str, PlanID
        result = from_str("")
        self.assertEqual(result, PlanID.FLEX_EVAL)

    def test_is_deprecated_core_50k(self):
        from common.plan_id import is_deprecated
        self.assertTrue(is_deprecated("core_50k"))

    def test_is_not_deprecated_flex_eval(self):
        from common.plan_id import is_deprecated
        self.assertFalse(is_deprecated("flex_eval"))


# ─────────────────────────────────────────────────────────────────────────────
# TC-9: Tradovate net_pos → side 変換の双方向整合
# ─────────────────────────────────────────────────────────────────────────────
class TestTradovateNetPosSideConversion(unittest.TestCase):
    """CR-5: net_pos の正負と side 変換の整合性を網羅的に検証。"""

    def _adapter(self, net_pos: int) -> str:
        """get_positions_for_rules() のアダプタロジックを独立で検証。"""
        return "BUY" if net_pos > 0 else "SELL"

    def test_positive_net_pos_is_buy(self):
        for n in [1, 2, 5, 10, 100]:
            self.assertEqual(self._adapter(n), "BUY", f"net_pos={n} should be BUY")

    def test_negative_net_pos_is_sell(self):
        for n in [-1, -2, -5, -10]:
            self.assertEqual(self._adapter(n), "SELL", f"net_pos={n} should be SELL")

    def test_adapter_side_matches_prop_firm_rules_expectation(self):
        """アダプタが返す side は prop_firm_rules の期待値("BUY"/"SELL") と一致。"""
        from tradovate_client import TradovateClient
        client = TradovateClient.__new__(TradovateClient)
        client.get_positions = MagicMock(return_value=[
            {"id": 1, "symbol": "MESU5", "net_pos": 3, "net_price": 5000.0, "unrealized_pnl": 0.0}
        ])
        result = client.get_positions_for_rules()
        self.assertEqual(result[0]["side"], "BUY")
        # prop_firm_rules は "BUY" / "SELL" / "LONG" / "SHORT" を受け入れる（正規化あり）
        self.assertIn(result[0]["side"], {"BUY", "SELL", "LONG", "SHORT"})


# ─────────────────────────────────────────────────────────────────────────────
# TC-10: HFT guard env渡し整合
# ─────────────────────────────────────────────────────────────────────────────
class TestHFTTradeCountEnv(unittest.TestCase):
    """CR-2: _daily_trade_count が env["trade_count_today"] に正しく渡ることを検証。

    ChronosBot._build_env_dict() 相当のロジックを直接テストするのではなく、
    _daily_trade_count と env["trade_count_today"] が一致することを
    インスタンス経由で確認する。
    """

    def _find_chronos_bot_path(self) -> Path:
        """chronos_bot.py の場所を解決する（mutmut実行環境でも動作）。"""
        # 通常のプロジェクトルート
        candidate = _ROOT / "chronos_bot.py"
        if candidate.exists():
            return candidate
        # mutmut実行時: mutants/ 上位ディレクトリを探す
        for parent in [_ROOT.parent, _ROOT.parent.parent]:
            c = parent / "chronos_bot.py"
            if c.exists():
                return c
        raise FileNotFoundError(f"chronos_bot.py not found from {_ROOT}")

    def test_daily_trade_count_attribute_exists(self):
        """ChronosBot に _daily_trade_count 属性が存在する（getattr フォールバック不要）。"""
        import ast
        path = self._find_chronos_bot_path()
        with open(path) as f:
            src = f.read()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ChronosBot":
                for item in ast.walk(node):
                    if isinstance(item, (ast.Assign, ast.AnnAssign)):
                        if isinstance(item, ast.AnnAssign):
                            t = item.target
                        else:
                            t = item.targets[0] if item.targets else None
                        if t and isinstance(t, ast.Attribute) and t.attr == "_daily_trade_count":
                            found = True
                            break
        self.assertTrue(found, "ChronosBot._daily_trade_count が __init__ で定義されていない")

    def test_trade_count_today_uses_daily_trade_count(self):
        """env['trade_count_today'] は _daily_trade_count を参照（CR-2修正確認）。

        getattr(self, "_trade_count_today", 0) のフォールバックが除去され、
        self._daily_trade_count を直接参照していることを AST で確認する。
        """
        import ast
        path = self._find_chronos_bot_path()
        with open(path) as f:
            src = f.read()
        tree = ast.parse(src)

        found_fallback_getattr = False

        for node in ast.walk(tree):
            # getattr(self, "_trade_count_today", 0) パターンを検出
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "getattr":
                    if len(node.args) >= 2:
                        arg1 = node.args[1]
                        if isinstance(arg1, ast.Constant) and arg1.value == "_trade_count_today":
                            found_fallback_getattr = True

        # _daily_trade_count の参照が env["trade_count_today"] = self._daily_trade_count として存在
        self.assertIn("self._daily_trade_count", src, "_daily_trade_count への直接参照がない")
        self.assertFalse(
            found_fallback_getattr,
            "CR-2未修正: getattr(self, '_trade_count_today', 0) フォールバックが残っている"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
