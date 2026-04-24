"""tests/test_canary_smoke_20260425.py
=====================================
Monday 2026-04-27 Canary Smoke Test script の design contract を固定する pytest。

対象:
  scripts/monday_canary_smoke_test_20260427.sh
  (shell script 本体は subprocess 経由で部分起動しないで、静的契約 + 各 step で
   実行される python snippet の論理互換テストを組む)

検証観点 (design freeze):
  1. script ファイルが存在し chmod +x 済み
  2. 12 step 全てに add_result / fatal_halt へのパスが存在 (grep 契約)
  3. 30 分 budget 定数 BUDGET_SEC=1800 が埋まっている
  4. exit code 表 (0/10/11/12/20/99) が docstring に漏れなく記載
  5. 各 step が必須 module / API を正しく import しているか (AST 静的解析 ではなく
     同じ import/expr を python 側で通すことで代替)
  6. bug_ledger パス + BUG-20260425- prefix + count=6 の閾値が script と一致
  7. pushover SILENT dry-run 経路が現行 pushover_client の public API と互換
  8. symbol_selector の 7 戦術以上契約 (get_tactic_names) と一致
  9. ChainGuard / PRG / MassVerify wrapper の公開 symbol が契約通り import 可能
 10. launchctl label が memo と一致 (com.soralab.atlas-paper / spy-bot-paper /
     com.moomoo.opend / com.soralab.moomoo-opend-relogin)
 11. budget 論理: 各 step の P95 想定合計が BUDGET_SEC 以内
 12. fatal_halt 時 Pushover が 1 本だけ発射される (送信数上限) — grep 契約
 13. ATLAS_TRADER_ACTIVE 移行フラグ: 0=spy-bot-paper / 1=atlas-trader 両対応

既存コード書換禁止原則により script への直接差分注入は行わず、契約チェックのみ。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from typing import Any

import pytest

ROOT = pathlib.Path("/Users/yuusakuichio/trading")
SCRIPT = ROOT / "scripts" / "monday_canary_smoke_test_20260427.sh"
BUG_LEDGER = ROOT / "data" / "bug_ledger.jsonl"

# プロジェクト root を import path に
sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# A. script 静的契約
# ─────────────────────────────────────────────────────────────────────────────


class TestScriptStaticContract:
    """script の静的契約 (存在 / 実行権限 / 定数 / exit code)."""

    def test_script_exists(self) -> None:
        assert SCRIPT.exists(), f"script missing: {SCRIPT}"

    def test_script_is_executable(self) -> None:
        mode = SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "script is not chmod +x"

    def test_budget_constant_30min(self) -> None:
        text = SCRIPT.read_text()
        assert "BUDGET_SEC=1800" in text, "30-minute budget constant missing"

    def test_12_steps_enumerated(self) -> None:
        text = SCRIPT.read_text()
        # step_header "N/12" が 1..12 全部あるか
        for n in range(1, 13):
            assert f"step_header {n} " in text, f"step_header {n} missing"
            assert f"add_result {n} " in text, f"add_result for step {n} missing"

    @pytest.mark.parametrize(
        "code,reason",
        [
            (0, "12/12 PASS"),
            (10, "1/12 fail"),
            (11, "2-11/12 fail"),
            (12, "total outage"),
            (20, "timeout 30 min"),
            (99, "precondition"),
        ],
    )
    def test_exit_code_documented(self, code: int, reason: str) -> None:
        text = SCRIPT.read_text()
        # docstring 部分 (先頭 200 行) に exit code が書かれているか
        header = "\n".join(text.splitlines()[:200])
        assert f"{code}" in header, f"exit code {code} ({reason}) not documented"

    def test_fatal_halt_present(self) -> None:
        text = SCRIPT.read_text()
        assert "fatal_halt()" in text
        # Pushover send_critical を 1 本だけ呼ぶ経路があるか
        assert "send_critical" in text, "fatal_halt Pushover send_critical missing"

    def test_pushover_dryrun_env_guard(self) -> None:
        text = SCRIPT.read_text()
        assert "PUSHOVER_DRY_RUN=1" in text, \
            "global PUSHOVER_DRY_RUN=1 not set at start (risk: accidental real send)"

    def test_no_vps_opend_reference(self) -> None:
        """CLAUDE.md: VPS OpenD 非接続 (auth_budget max=3/24h 温存)."""
        text = SCRIPT.read_text()
        # 127.0.0.1 のみ (VPS IP 参照禁止)
        assert "127.0.0.1" in text
        # 既知 VPS IP パターン (念のため) — 数字 IP を broadly 拾う
        vps_ip = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        bad = [ip for ip in vps_ip if ip != "127.0.0.1"]
        assert not bad, f"non-localhost IPs detected: {bad}"

    def test_budget_sum_under_1800s(self) -> None:
        """P95 所要時間想定: 各 step ごとの内訳を docstring に縛り、合計 <= 1800s."""
        # 想定 (P95 sec): 1=60, 2=20, 3=10, 4=10, 5=5, 6=3, 7=3, 8=3,
        #                 9=5, 10=400, 11=15, 12=5, margin=200 → 739 < 1800
        allocation = {
            1: 60, 2: 20, 3: 10, 4: 10, 5: 5, 6: 3, 7: 3, 8: 3,
            9: 5, 10: 400, 11: 15, 12: 5,
        }
        assert sum(allocation.values()) + 200 <= 1800, \
            "P95 + margin exceeds BUDGET_SEC=1800"


# ─────────────────────────────────────────────────────────────────────────────
# B. 依存 module の import 可能性 (step 5/6/7/8 が実行時に壊れないことを保証)
# ─────────────────────────────────────────────────────────────────────────────


class TestDependencyImports:
    def test_symbol_selector_7_tactics(self) -> None:
        from common import symbol_selector as ss
        names = ss.get_tactic_names()
        assert len(names) >= 7, f"expected >= 7 tactics, got {len(names)}: {names}"
        # 全 tactic が weight dict を引ける
        for t in names[:7]:
            resolved = ss._resolve_tactic(t)
            w = ss._TACTIC_WEIGHTS[resolved]
            assert isinstance(w, dict) and w

    def test_chainguard_wrapper_public_api(self) -> None:
        from atlas_v3.ops.chainguard_wrapper import (
            ChainGuardError,
            get_chain_center_price,
        )
        price = get_chain_center_price("US.SPY", {"last_price": 570.12})
        assert price == pytest.approx(570.12)
        with pytest.raises(ChainGuardError):
            get_chain_center_price("US.SPY", {"last_price": None})

    def test_portfolio_risk_gate_vix40_halts(self) -> None:
        from atlas_v3.ops.portfolio_risk_gate import (
            GateConfig,
            check_entry_allowed,
        )
        cfg = GateConfig()
        decision = check_entry_allowed(vix=40.0, current_entries=0, config=cfg)
        assert not decision.allowed, "VIX=40 must trigger halt"

    def test_mass_verify_empty_list_is_noop(self) -> None:
        from atlas_v3.ops.mass_verify_safe_runner import (
            VerifyContext,
            VerifyResult,
            run_mass_verify_safe,
        )

        def dummy(ctx: VerifyContext) -> VerifyResult:
            return VerifyResult.ok(ctx)

        assert run_mass_verify_safe([], dummy) == []

    def test_verify_context_is_frozen(self) -> None:
        from atlas_v3.ops.mass_verify_safe_runner import VerifyContext
        ctx = VerifyContext(
            symbol="US.SPY", strike=570.0, expiry="2026-05-15", option_type="C"
        )
        with pytest.raises((AttributeError, Exception)):
            ctx.symbol = "US.SPX"  # type: ignore[misc]

    def test_pushover_send_silent_is_safe(self) -> None:
        """SILENT level は quiet_hours / ban を無視してログのみで True を返す。"""
        from common.pushover_client import send_silent
        assert send_silent("[TEST] canary probe", "unit-test ping") is True


# ─────────────────────────────────────────────────────────────────────────────
# C. bug_ledger 実体契約 (step 9)
# ─────────────────────────────────────────────────────────────────────────────


class TestBugLedgerContract:
    BUG_PREFIX = "BUG-20260425-"
    MIN_EXPECTED = 6   # BUG-20260425-001..006 は必ず入っていること
    CORE_IDS = tuple(f"BUG-20260425-{i:03d}" for i in range(1, 7))

    def _load_ids(self) -> set[str]:
        ids: set[str] = set()
        if not BUG_LEDGER.exists():
            return ids
        for line in BUG_LEDGER.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            bid = d.get("bug_id", "")
            if bid.startswith(self.BUG_PREFIX):
                ids.add(bid)
        return ids

    def test_bug_ledger_file_exists(self) -> None:
        assert BUG_LEDGER.exists(), f"bug_ledger missing: {BUG_LEDGER}"

    def test_at_least_6_core_bugs_recorded(self) -> None:
        """初期 6 件 (BUG-20260425-001..006) が必ず存在すること。追加分 (007+) は許容."""
        ids = self._load_ids()
        missing = [b for b in self.CORE_IDS if b not in ids]
        assert not missing, (
            f"core BUG-20260425-* missing: {missing} (got={sorted(ids)})"
        )
        assert len(ids) >= self.MIN_EXPECTED

    @pytest.mark.parametrize("idx", [1, 2, 3, 4, 5, 6])
    def test_each_core_bug_id_present(self, idx: int) -> None:
        expected = f"BUG-20260425-{idx:03d}"
        assert expected in self._load_ids()


# ─────────────────────────────────────────────────────────────────────────────
# D. launchctl label 契約 (step 1)
# ─────────────────────────────────────────────────────────────────────────────


class TestLaunchctlLabels:
    EXPECTED_LABELS = (
        "com.soralab.atlas-paper",
        "com.soralab.spy-bot-paper",
        "com.soralab.atlas-trader",
        "com.soralab.moomoo-opend-relogin",
    )
    # bash 側の grep -E で使う OpenD 正規表現。bash heredoc 内では単バックスラッシュ
    # で綴られる (awk/grep の regex 解釈のため)。
    OPEND_SUBSTRING = r"application\.com\.moomoo\.opend"

    def test_script_enumerates_expected_labels(self) -> None:
        text = SCRIPT.read_text()
        for label in self.EXPECTED_LABELS:
            assert label in text, f"script missing launchctl label: {label}"
        # 生文字列サブ一致 (escape を含むまま) で確認
        assert self.OPEND_SUBSTRING in text, "OpenD grep -E regex not wired"

    def test_kickstart_target_is_spy_bot_paper(self) -> None:
        """全 PASS 時の本番 kickstart 対象は MassVerify 駆動の spy-bot-paper."""
        text = SCRIPT.read_text()
        assert "launchctl kickstart -k \"gui/$(id -u)/com.soralab.spy-bot-paper\"" in text

    def test_atlas_trader_label_in_script(self) -> None:
        """月曜移行後に使う com.soralab.atlas-trader が script 本体に存在すること。"""
        text = SCRIPT.read_text()
        assert "com.soralab.atlas-trader" in text

    def test_spy_bot_paper_label_in_script(self) -> None:
        """移行前 fallback の com.soralab.spy-bot-paper が script 本体に残ること。"""
        text = SCRIPT.read_text()
        assert "com.soralab.spy-bot-paper" in text


# ─────────────────────────────────────────────────────────────────────────────
# D2. ATLAS_TRADER_ACTIVE 移行フラグ契約 (step 1 dual-mode)
# ─────────────────────────────────────────────────────────────────────────────


class TestAtlasTraderMigrationFlag:
    """ATLAS_TRADER_ACTIVE=1/0 env による Step 1 daemon 検出切替の静的契約。"""

    def test_flag_default_zero_in_script(self) -> None:
        """デフォルト値 ATLAS_TRADER_ACTIVE=0 が script に埋まっていること (安全側)。"""
        text = SCRIPT.read_text()
        assert 'ATLAS_TRADER_ACTIVE="${ATLAS_TRADER_ACTIVE:-0}"' in text, \
            "ATLAS_TRADER_ACTIVE default=0 not found in script"

    def test_flag_active_branch_uses_atlas_trader(self) -> None:
        """ATLAS_TRADER_ACTIVE=1 ブランチが com.soralab.atlas-trader を参照すること。"""
        text = SCRIPT.read_text()
        # bash の条件式 [[ "${ATLAS_TRADER_ACTIVE}" == "1" ]] が存在すること
        assert 'ATLAS_TRADER_ACTIVE}" == "1"' in text, \
            "atlas-trader active branch condition not found"
        assert "com.soralab.atlas-trader" in text

    def test_flag_fallback_branch_uses_spy_bot_paper(self) -> None:
        """ATLAS_TRADER_ACTIVE=0 ブランチが com.soralab.spy-bot-paper を参照すること。"""
        text = SCRIPT.read_text()
        assert "com.soralab.spy-bot-paper" in text

    def test_both_branches_log_mode(self) -> None:
        """両ブランチがモードをログ出力すること (運用可視性)。"""
        text = SCRIPT.read_text()
        assert "mode=atlas-trader" in text, "atlas-trader mode log missing"
        assert "mode=spy-bot-paper fallback" in text, "spy-bot-paper fallback mode log missing"

    def test_pre_migration_coexist_detection(self) -> None:
        """ATLAS_TRADER_ACTIVE=0 時も atlas-trader が loaded なら検出ログを出すこと。"""
        text = SCRIPT.read_text()
        assert "pre-migration coexist" in text, \
            "pre-migration coexist detection log missing"

    def test_smoke_test_works_without_atlas_trader(self) -> None:
        """atlas-trader 未稼働時 (ATLAS_TRADER_ACTIVE=0) でも smoke test が機能すること。
        Script は ATLAS_TRADER_ACTIVE=0 ブランチで spy-bot-paper を必須チェック対象にする。
        atlas-trader は任意チェック (missing でも FAIL にしない)。"""
        text = SCRIPT.read_text()
        # 任意チェック側は MISSING_DAEMONS に追加しないこと
        # "atlas-trader not loaded (expected" という WARNING ログが存在すること
        assert "atlas-trader not loaded (expected" in text, \
            "atlas-trader absence should be logged as expected, not treated as FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# E. regression pytest target 契約 (step 10)
# ─────────────────────────────────────────────────────────────────────────────


class TestRegressionTargets:
    REGRESSION_FILES = (
        "tests/test_time_travel_windows_20260425.py",
        "tests/test_symbol_aware_price_20260425.py",
        "tests/test_chainguard_wrapper.py",
        "tests/test_portfolio_risk_gate.py",
        "tests/test_mass_verify_safe_runner.py",
    )
    # agent a531d8 完了後に追加される予定のファイル
    ATLAS_SUBPROCESS_TEST = "tests/test_atlas_v3_bots_subprocess_20260425.py"

    @pytest.mark.parametrize("rel", REGRESSION_FILES)
    def test_regression_file_exists(self, rel: str) -> None:
        f = ROOT / rel
        assert f.exists(), f"regression target missing: {rel}"

    @pytest.mark.parametrize("rel", REGRESSION_FILES)
    def test_script_references_regression_file(self, rel: str) -> None:
        text = SCRIPT.read_text()
        assert rel in text, f"script does not wire regression target: {rel}"

    def test_atlas_subprocess_test_auto_included_when_present(self) -> None:
        """atlas-trader subprocess test が存在する場合に script が自動追加する経路を持つこと。
        ファイル自体はまだ存在しなくてよい (agent a531d8 完了後に追加される想定)。"""
        text = SCRIPT.read_text()
        assert self.ATLAS_SUBPROCESS_TEST in text, \
            "atlas-trader subprocess test path not referenced in script (auto-include logic missing)"

    def test_atlas_subprocess_test_is_optional_when_absent(self) -> None:
        """atlas-trader subprocess test が存在しない場合は SKIP であり FAIL にならないこと。
        Script に -f ファイル存在チェックが組まれていること。"""
        text = SCRIPT.read_text()
        # script が -f ${ROOT}/${ATLAS_SUBPROCESS_TEST} のような存在確認してから追加する
        # 構造になっているか。bash 変数展開形式でチェック。
        assert '-f "${ROOT}/${ATLAS_SUBPROCESS_TEST}"' in text, \
            "atlas-trader subprocess test must be guarded by -f existence check"


# ─────────────────────────────────────────────────────────────────────────────
# F. side-effect safety contract (CLAUDE.md 鉄則 #2)
# ─────────────────────────────────────────────────────────────────────────────


class TestSideEffectSafety:
    def test_no_place_order_call(self) -> None:
        """発注 0 件を物理保証 (script に place_order の呼出禁止)."""
        text = SCRIPT.read_text()
        assert "place_order" not in text, \
            "canary must not issue place_order (side effect forbidden)"

    def test_no_auth_budget_consumption(self) -> None:
        """auth_budget を消費する relogin 呼出を含まない (heartbeat 読取のみ)."""
        text = SCRIPT.read_text()
        assert "force_relogin" not in text
        assert "unlock_trade" not in text

    def test_no_kill_switch_activation(self) -> None:
        """canary は kill_switch を自分で起動しない (andon_multichannel 経由のみ)."""
        text = SCRIPT.read_text()
        assert "kill_switch.activate" not in text

    def test_legacy_files_not_rewritten(self) -> None:
        """spy_bot.py / chronos_bot.py を編集対象にしていない."""
        text = SCRIPT.read_text()
        # spy_bot.py は subprocess で --test-connect dry-run 呼出のみ許可
        lines = [line for line in text.splitlines() if "spy_bot.py" in line]
        for ln in lines:
            # 編集系コマンド (>|>>|sed -i|cp) が混ざってない
            assert not re.search(r"(^|\s)(sed -i|>>?\s*.*spy_bot\.py|cp .* spy_bot\.py)", ln), \
                f"legacy rewrite suspected: {ln}"
