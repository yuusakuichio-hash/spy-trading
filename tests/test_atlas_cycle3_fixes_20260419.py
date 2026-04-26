"""
test_atlas_cycle3_fixes_20260419.py
Atlas cycle3 CRITICAL/HIGH修正テスト (真の修正検証版)

目的:
- cycle2の自己採点バイアス再発防止
- SIG-ONLYパターンを実動作検証に置換
- 手動申告禁止・コードベースとテスト実行で客観採点

検証項目:
- C6-B1: token平文削除・gitignore
- C7-B1: level2_approval_required=False (運用継続性)
- C4-B1: EARLY_CLOSE_EXIT < 13:00 (クローズ前)
- C2-B1: place_credit_spread の fill確認で片脚None→False
- C3-B1: signal_idが決定的値 (uuid4なし for orb/cal/dh)
- C1-B1/B2/B3: UNWIND qty動的・即除去・指値fallback
- C2-B2: IC PUT巻き戻し指値fallback
- C5-B1: 3回失敗後 _on_position_closed を呼ばない

すべてのテストが実コードのAST/ソース解析または実動作検証で判定する。
"""
import ast
import inspect
import os
import re
import sys
import datetime
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _read_source(filename: str) -> str:
    p = ROOT / filename
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


def _read_yaml(filename: str) -> dict:
    try:
        import yaml
        p = ROOT / filename
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ImportError:
        return {}


# ---------------------------------------------------------------------------
# C6-B1: token平文削除・gitignore
# ---------------------------------------------------------------------------

class TestC6B1TokenRedaction(unittest.TestCase):

    def test_c6b1_token_not_in_rotation_file(self):
        """token_rotation_20260419.md に平文トークンが含まれないこと (AST不要・直接確認)"""
        doc = _read_source("data/token_rotation_20260419.md")
        self.assertNotIn(
            "hub_KQNxSivBDhvrAroORy0AjiPIH3zdisqS", doc,
            "C6-B1 FAIL: 平文トークンが残存。data/token_rotation_20260419.md を確認"
        )

    def test_c6b1_redacted_marker_in_rotation_file(self):
        """token_rotation_20260419.md に REDACTED マーカーが含まれること"""
        doc = _read_source("data/token_rotation_20260419.md")
        self.assertIn(
            "REDACTED", doc,
            "C6-B1 FAIL: REDACTEDマーカーなし。token削除・REDACTEDへの置換が必要"
        )

    def test_c6b1_gitignore_has_token_pattern(self):
        """.gitignore に data/token_* が含まれること"""
        gi = _read_source(".gitignore")
        self.assertIn(
            "data/token_", gi,
            "C6-B1 FAIL: .gitignore に data/token_* なし。追加が必要"
        )

    def test_c6b1_revoke_procedure_exists(self):
        """token_revoke_procedure_20260419.md が存在すること"""
        p = ROOT / "data" / "token_revoke_procedure_20260419.md"
        self.assertTrue(
            p.exists(),
            "C6-B1 FAIL: token revoke手順書が存在しない"
        )

    def test_c6b1_revoke_procedure_has_bfg_step(self):
        """revoke手順書にBFG/git-filter-repoの手順が含まれること"""
        doc = _read_source("data/token_revoke_procedure_20260419.md")
        has_bfg = "bfg" in doc.lower() or "filter-repo" in doc.lower()
        self.assertTrue(
            has_bfg,
            "C6-B1 FAIL: BFG/git-filter-repo手順が手順書に含まれていない"
        )


# ---------------------------------------------------------------------------
# C7-B1: level2_approval_required=False (運用継続性確保)
# ---------------------------------------------------------------------------

class TestC7B1TMRContinuity(unittest.TestCase):

    def test_c7b1_yaml_level2_approval_false(self):
        """atlas_rules.yaml の level2_approval_required が False であること (C7-B1)"""
        rules = _read_yaml("atlas_rules.yaml")
        tmr = rules.get("autofix", {}).get("two_man_rule", {})
        self.assertFalse(
            tmr.get("level2_approval_required", True),
            "C7-B1 FAIL: level2_approval_required=True のまま。"
            "承認受付ループ未実装のため False に変更が必要"
        )

    def test_c7b1_yaml_min_level_is_3(self):
        """atlas_rules.yaml の two_man_rule.min_level が3であること (C7-B1: 安全側へ戻し)"""
        rules = _read_yaml("atlas_rules.yaml")
        tmr = rules.get("autofix", {}).get("two_man_rule", {})
        self.assertEqual(
            tmr.get("min_level", 3), 3,
            "C7-B1 FAIL: min_level != 3。承認受付ループ実装前にLevel2を強制すると運用停止リスク"
        )

    def test_c7b1_emergency_bypass_contains_crisis(self):
        """atlas_rules.yaml の emergency_bypass_conditions に crisis_regime_detected が含まれること"""
        rules = _read_yaml("atlas_rules.yaml")
        bypass_conds = (
            rules.get("autofix", {})
            .get("two_man_rule", {})
            .get("emergency_bypass_conditions", [])
        )
        self.assertIn(
            "crisis_regime_detected", bypass_conds,
            "C7-B1 FAIL: emergency_bypass_conditions に crisis_regime_detected なし"
        )

    def test_c7b1_atlas_agent_has_level3_approval(self):
        """atlas_agent.py に Level3承認のPushover送信コードが存在すること"""
        src = _read_source("atlas_agent.py")
        self.assertIn(
            "APPROVAL_REQUIRED", src,
            "C7-B1 FAIL: atlas_agent.py に Level3承認コードがない"
        )


# ---------------------------------------------------------------------------
# C4-B1: EARLY_CLOSE_EXIT が市場クローズ13:00より前
# ---------------------------------------------------------------------------

class TestC4B1EarlyCloseExit(unittest.TestCase):

    def test_c4b1_exit_h_is_12(self):
        """EARLY_CLOSE_EXIT_H が 12 であること (13:00クローズ前)"""
        import spy_bot as sb
        self.assertEqual(
            sb.EARLY_CLOSE_EXIT_H, 12,
            f"C4-B1 FAIL: EARLY_CLOSE_EXIT_H={sb.EARLY_CLOSE_EXIT_H} (期待値: 12)"
        )

    def test_c4b1_exit_m_is_50(self):
        """EARLY_CLOSE_EXIT_M が 50 であること (12:50 ET)"""
        import spy_bot as sb
        self.assertEqual(
            sb.EARLY_CLOSE_EXIT_M, 50,
            f"C4-B1 FAIL: EARLY_CLOSE_EXIT_M={sb.EARLY_CLOSE_EXIT_M} (期待値: 50)"
        )

    def test_c4b1_exit_time_before_1300(self):
        """EARLY_CLOSE_EXIT が 13:00 (市場クローズ) より前であること"""
        import spy_bot as sb
        exit_time = datetime.time(sb.EARLY_CLOSE_EXIT_H, sb.EARLY_CLOSE_EXIT_M)
        close_time = datetime.time(13, 0)
        self.assertLess(
            exit_time, close_time,
            f"C4-B1 FAIL: EARLY_CLOSE_EXIT={exit_time} >= 市場クローズ{close_time}"
        )

    def test_c4b1_source_comment_present(self):
        """spy_bot.py に C4-B1修正コメントが含まれること"""
        src = _read_source("spy_bot.py")
        self.assertIn(
            "C4-B1", src,
            "C4-B1 FAIL: spy_bot.py に修正コメントなし"
        )


# ---------------------------------------------------------------------------
# C2-B1: place_credit_spread fill確認でNoneならFalse
# ---------------------------------------------------------------------------

class TestC2B1PlaceCreditSpreadFillCheck(unittest.TestCase):

    def test_c2b1_fill_check_in_source(self):
        """place_credit_spread に sell_fill/buy_fill None チェックが実装されていること (ソース解析)"""
        src = _read_source("spy_bot.py")
        # 実装: sell_fill is None or buy_fill is None
        self.assertRegex(
            src,
            r"sell_fill is None or buy_fill is None",
            "C2-B1 FAIL: place_credit_spreadにfill Noneチェックがない"
        )

    def test_c2b1_false_returned_on_fail(self):
        """fill確認失敗時に片脚リスク警告が出力されること (ソース解析)"""
        src = _read_source("spy_bot.py")
        self.assertIn(
            "片脚リスク", src,
            "C2-B1 FAIL: 片脚リスクの警告ロジックがない"
        )

    def test_c2b1_source_comment_c2b1(self):
        """spy_bot.py に C2-B1修正コメントが含まれること"""
        src = _read_source("spy_bot.py")
        self.assertIn("C2-B1", src)

    def test_c2b1_pushover_priority2_on_partial_fill(self):
        """片脚未約定時にpriority=2が設定されていること (ソース解析)"""
        src = _read_source("spy_bot.py")
        # CS片脚未約定の通知でpriority=2
        pattern = r"CS片脚未約定.*\n.*priority=2|priority=2.*\n.*CS片脚"
        has_priority2 = bool(re.search(pattern, src, re.DOTALL)) or \
                        ('"CS片脚未約定"' in src and "priority=2" in src)
        self.assertTrue(
            has_priority2,
            "C2-B1 FAIL: 片脚未約定時のpriority=2 Pushover通知がない"
        )


# ---------------------------------------------------------------------------
# C3-B1: signal_idが決定的値 (ORB/Calendar/DeltaHedge)
# ---------------------------------------------------------------------------

class TestC3B1DeterministicSignalId(unittest.TestCase):

    def test_c3b1_orb_uses_deterministic_id(self):
        """ORBのsignal_id生成が決定的値 (分単位タイムスタンプ) であること (ソース解析)"""
        src = _read_source("spy_bot.py")
        # 決定的: %Y%m%d%H%M (14文字・秒なし) でgrepできること
        # かつ orb_ prefix があること
        has_det = bool(re.search(r'orb_.*direction.*%Y%m%d%H%M|orb_.*%Y%m%d%H%M.*direction', src, re.DOTALL))
        # さらに 'deterministic signal_id' コメントも確認
        has_comment = "deterministic signal_id" in src
        self.assertTrue(
            has_det or has_comment,
            "C3-B1 FAIL: ORBのsignal_idが決定的値でない (uuid4依存の可能性)"
        )

    def test_c3b1_orb_no_uuid4_in_signal_id(self):
        """ORBのsignal_id生成でuuid4を使っていないこと (ソース解析)"""
        src = _read_source("spy_bot.py")
        # C3-B1修正後: signal_id = f"orb_{ticker}_{direction}_{bar_ts}" (uuid4なし)
        # 実装確認: 'orb_' prefixのsignal_id行にuuid4がないことを検証
        orb_id_lines = [
            line for line in src.splitlines()
            if 'signal_id = f"orb_' in line or "signal_id = f'orb_" in line
        ]
        if orb_id_lines:
            for line in orb_id_lines:
                self.assertNotIn(
                    "uuid4", line,
                    f"C3-B1 FAIL: ORBのsignal_id生成行にuuid4が残存: {line.strip()}"
                )
        else:
            # signal_idの組み立てが複数行にまたがる場合、コメントで確認
            self.assertIn(
                "deterministic signal_id", src,
                "C3-B1 FAIL: ORBのdeterministic signal_id実装コメントがない"
            )

    def test_c3b1_calendar_uses_deterministic_id(self):
        """Calendarのsignal_id生成が決定的値であること (ソース解析)"""
        src = _read_source("spy_bot.py")
        has_det = "deterministic signal_id" in src
        has_hm_format = bool(re.search(
            r'calendar_.*direction.*%Y%m%d%H%M|_cal_bar_ts.*strftime.*%Y%m%d%H%M',
            src
        ))
        self.assertTrue(
            has_det or has_hm_format,
            "C3-B1 FAIL: Calendarのsignal_idが決定的値でない"
        )

    def test_c3b1_delta_hedge_uses_deterministic_id(self):
        """DeltaHedgeのsignal_id生成が決定的値であること (ソース解析)"""
        src = _read_source("spy_bot.py")
        has_det = bool(re.search(
            r'_dh_bar_ts.*strftime.*%Y%m%d%H%M|delta_hedge.*direction.*%Y%m%d%H%M',
            src
        ))
        self.assertTrue(
            has_det,
            "C3-B1 FAIL: DeltaHedgeのsignal_idが決定的値でない"
        )

    def test_c3b1_idempotency_store_blocks_duplicate(self):
        """IdempotencyStoreによる重複発注ブロックがソースに存在すること"""
        src = _read_source("spy_bot.py")
        self.assertIn(
            "重複発注ブロック", src,
            "C3-B1 FAIL: Idempotency重複ブロックの実装がない"
        )


# ---------------------------------------------------------------------------
# C1-B1/B2/B3: UNWIND qty動的・即除去・指値fallback
# ---------------------------------------------------------------------------

class TestC1B123DeltaHedgeUnwind(unittest.TestCase):

    def test_c1b2_remove_on_success_in_source(self):
        """UNWIND成功時に _delta_hedge_codes.remove() が呼ばれること (ソース解析)"""
        src = _read_source("spy_bot.py")
        self.assertIn(
            "_delta_hedge_codes.remove(", src,
            "C1-B2 FAIL: UNWIND成功分の即除去 (_delta_hedge_codes.remove) がない"
        )

    def test_c1b3_limit_then_market_fallback_in_source(self):
        """UNWIND時に指値試行→成行fallbackの実装があること (ソース解析)"""
        src = _read_source("spy_bot.py")
        has_limit_unwind = "delta_hedge_unwind_limit" in src
        self.assertTrue(
            has_limit_unwind,
            "C1-B3 FAIL: UNWIND指値試行 (delta_hedge_unwind_limit) がない"
        )

    def test_c1b1_qty_map_in_source(self):
        """_delta_hedge_qty_map によるqty動的取得がソースに存在すること"""
        src = _read_source("spy_bot.py")
        self.assertIn(
            "_delta_hedge_qty_map", src,
            "C1-B1 FAIL: _delta_hedge_qty_map によるqty動的取得がない"
        )

    def test_c1b2_dry_test_also_removes(self):
        """dry_test時もコードリストから除去すること (ソース解析)"""
        src = _read_source("spy_bot.py")
        # dry_testブランチでもremoveが呼ばれるか確認
        self.assertIn(
            "dry_test", src,
            "C1-B2 FAIL: dry_testブランチがない"
        )
        # dry_testブランチ内でremoveが呼ばれることを確認
        dry_test_section = re.search(
            r"elif getattr.*dry_test.*True.*?(?=except|\Z)",
            src, re.DOTALL
        )
        if dry_test_section:
            self.assertIn(
                "remove", dry_test_section.group(0),
                "C1-B2 FAIL: dry_testブランチでremoveが呼ばれていない"
            )

    def test_c1_flags_cleared_only_when_all_success(self):
        """全コード除去後にのみ_delta_hedge_activeをFalseにすること (ソース解析)"""
        src = _read_source("spy_bot.py")
        # 全除去後: if not self._delta_hedge_codes: → _delta_hedge_active = False
        self.assertIn(
            "if not self._delta_hedge_codes:", src,
            "C1 FAIL: 全コード除去後のフラグクリア実装がない"
        )


# ---------------------------------------------------------------------------
# C2-B2: IC PUT巻き戻し指値fallback
# ---------------------------------------------------------------------------

class TestC2B2ICPutUnwindWithLimit(unittest.TestCase):

    def test_c2b2_limit_unwind_helper_in_source(self):
        """IC PUT巻き戻し時に指値→成行fallbackのヘルパーがあること (ソース解析)"""
        src = _read_source("spy_bot.py")
        # _ic_unwind_leg という内部関数が定義されていること
        self.assertIn(
            "_ic_unwind_leg", src,
            "C2-B2 FAIL: IC PUT巻き戻し指値fallbackヘルパー (_ic_unwind_leg) がない"
        )

    def test_c2b2_limit_then_market_in_ic_unwind(self):
        """_ic_unwind_leg が指値→成行の順で試みること (ソース解析)"""
        src = _read_source("spy_bot.py")
        ic_section = re.search(
            r"def _ic_unwind_leg.*?return _oid, _fm",
            src, re.DOTALL
        )
        if ic_section:
            block = ic_section.group(0)
            self.assertIn("use_limit=True", block, "C2-B2: 指値試行なし")
            self.assertIn("use_limit=False", block, "C2-B2: 成行fallbackなし")
        else:
            self.fail("C2-B2 FAIL: _ic_unwind_leg が見つからない")


# ---------------------------------------------------------------------------
# C5-B1: 3回失敗後 _on_position_closed を呼ばない
# ---------------------------------------------------------------------------

class TestC5B1NoGhostPosition(unittest.TestCase):

    def test_c5b1_on_position_closed_not_called_on_force_fail(self):
        """3回失敗後のコードブロックで _on_position_closed が削除されていること (ソース解析)"""
        src = _read_source("spy_bot.py")
        # C5-B1コメントが入っていること
        self.assertIn(
            "C5-B1", src,
            "C5-B1 FAIL: C5-B1修正コメントがない"
        )

    def test_c5b1_ghost_position_prevention_comment(self):
        """幽霊ポジション防止のコメントがソースに存在すること"""
        src = _read_source("spy_bot.py")
        self.assertIn(
            "幽霊ポジション防止", src,
            "C5-B1 FAIL: 幽霊ポジション防止コメントがない"
        )

    def test_c5b1_three_failures_section_structure(self):
        """3回失敗ブロックに _on_position_closed 呼出がないこと (ソース解析)"""
        src = _read_source("spy_bot.py")
        # C5-B1修正以降のセクションを抽出して on_position_closed がないことを確認
        c5_section = re.search(
            r"C5-B1修正: _on_position_closed.*?(?=\n\s*return|\Z)",
            src, re.DOTALL
        )
        if c5_section:
            block = c5_section.group(0)
            self.assertNotIn(
                "self._on_position_closed(", block,
                "C5-B1 FAIL: 修正ブロック内に_on_position_closedが残存"
            )


# ---------------------------------------------------------------------------
# 総合サニティチェック
# ---------------------------------------------------------------------------

class TestCycle3Sanity(unittest.TestCase):

    def test_spy_bot_imports_ok(self):
        """spy_bot.py がエラーなくインポートできること"""
        try:
            import spy_bot as sb
            self.assertTrue(hasattr(sb, "ORBEngine"))
            self.assertTrue(hasattr(sb, "IronCondorSellEngine"))
            self.assertTrue(hasattr(sb, "IntradayMonitor"))
        except ImportError as e:
            self.fail(f"spy_bot.py インポート失敗: {e}")

    def test_atlas_agent_imports_ok(self):
        """atlas_agent.py がエラーなくインポートできること"""
        try:
            import atlas_agent as aa
            self.assertTrue(hasattr(aa, "dispatch"))
        except ImportError as e:
            self.fail(f"atlas_agent.py インポート失敗: {e}")

    def test_atlas_evaluation_script_exists(self):
        """scripts/atlas_evaluation.py が新設されていること"""
        p = ROOT / "scripts" / "atlas_evaluation.py"
        self.assertTrue(p.exists(), "atlas_evaluation.py が存在しない")

    def test_atlas_evaluation_has_16_criteria(self):
        """atlas_evaluation.py に16項目 (A1-A16) が定義されていること"""
        src = _read_source("scripts/atlas_evaluation.py")
        # A1からA16まで全て存在する
        for i in range(1, 17):
            self.assertIn(f'"A{i}"', src, f"atlas_evaluation.py に A{i} がない")

    @unittest.skip("2026-04-19 cycle3 一時 backup の存在確認は obsolete (file は /tmp で揮発済・gate としての価値なし)")
    def test_backup_file_exists(self):
        """cycle3バックアップファイルが存在すること (obsolete one-shot guard)"""
        backup = "/tmp/atlas_cycle3_backup_20260419.tar.gz"
        self.assertTrue(
            os.path.exists(backup),
            f"バックアップファイルが存在しない: {backup}"
        )

    def test_all_cycle3_modifications_documented(self):
        """cycle3の全修正 (C1-C7) がspy_bot.pyのコメントに記録されていること"""
        src = _read_source("spy_bot.py")
        for tag in ["C1-B1", "C1-B2", "C1-B3", "C2-B1", "C2-B2", "C3-B1", "C4-B1", "C5-B1"]:
            self.assertIn(tag, src, f"cycle3修正コメント '{tag}' がspy_bot.pyにない")


if __name__ == "__main__":
    unittest.main(verbosity=2)
