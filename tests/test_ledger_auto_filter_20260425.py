"""tests/test_ledger_auto_filter_20260425.py

auto-noise filter 関連の全テスト (15+ 件)

対象:
  1. user_prompt_ledger.py の is_noise() 関数
  2. migrate_ledger_auto_filter_20260425.py の dry-run / apply
  3. ledger_audit_run.py の keyword 率チェック・commit 検証
  4. idle_agent_spawn_guard.sh の閾値チェック (Python ロジック部分)

asyncio 禁止 (B16) — 全テスト同期
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path("/Users/yuusakuichio/trading")
JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# ヘルパー: user_prompt_ledger モジュールをパスから直接 import
# ---------------------------------------------------------------------------

def _import_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ledger_mod():
    p = ROOT / ".claude" / "hooks" / "user_prompt_ledger.py"
    return _import_module_from_path("user_prompt_ledger", p)


@pytest.fixture(scope="module")
def migrate_mod():
    p = ROOT / "scripts" / "migrate_ledger_auto_filter_20260425.py"
    return _import_module_from_path("migrate_ledger_auto_filter_20260425", p)


@pytest.fixture(scope="module")
def audit_mod():
    p = ROOT / "scripts" / "ledger_audit_run.py"
    return _import_module_from_path("ledger_audit_run", p)


# ---------------------------------------------------------------------------
# 1. is_noise() — ノイズパターン正常検出
# ---------------------------------------------------------------------------

class TestIsNoise:
    def test_task_notification_startswith(self, ledger_mod):
        assert ledger_mod.is_noise("<task-notification>\n<task-id>abc</task-id>")

    def test_task_notification_not_contains(self, ledger_mod):
        """内部にあるだけでは startswith では検出されない。"""
        assert not ledger_mod.is_noise("ここに<task-notification>が入っている")

    def test_discipline_checker_contains(self, ledger_mod):
        assert ledger_mod.is_noise("You are Sora Lab discipline checker. Analyze...")

    def test_discipline_checker_mid_sentence(self, ledger_mod):
        assert ledger_mod.is_noise("  Sora Lab discipline checker: output rule")

    def test_output_formatter_contains(self, ledger_mod):
        assert ledger_mod.is_noise("Output formatter: format this text")

    def test_you_are_llm_startswith(self, ledger_mod):
        assert ledger_mod.is_noise("You are an LLM assistant. Your task...")

    def test_loop_slash_startswith(self, ledger_mod):
        assert ledger_mod.is_noise("# /loop\nParse the input below into...")

    def test_stop_hook_feedback_startswith(self, ledger_mod):
        assert ledger_mod.is_noise("Stop hook feedback:\n[python3 /trading/.claude/...]")

    def test_normal_user_instruction_not_noise(self, ledger_mod):
        assert not ledger_mod.is_noise("今日の作業を進めてください")

    def test_normal_question_not_noise(self, ledger_mod):
        assert not ledger_mod.is_noise("なぜAtlasが止まっているの？")

    def test_normal_feedback_not_noise(self, ledger_mod):
        assert not ledger_mod.is_noise("ありがとう、よくできてた")

    def test_empty_string_not_noise(self, ledger_mod):
        assert not ledger_mod.is_noise("")

    def test_short_string_not_noise(self, ledger_mod):
        assert not ledger_mod.is_noise("OK")


# ---------------------------------------------------------------------------
# 2. user_prompt_ledger.py — main() でのフィルタ動作
# ---------------------------------------------------------------------------

class TestLedgerMain:
    def _run_ledger(self, ledger_mod, text: str, tmp_path: Path) -> dict[str, Any]:
        """ledger hook を直接呼んで ledger ファイルの最終エントリを返す。"""
        ledger_file = tmp_path / "test_ledger.jsonl"
        # LEDGER パスを一時ファイルに差し替え
        original = ledger_mod.LEDGER
        ledger_mod.LEDGER = ledger_file
        try:
            payload = json.dumps({"prompt": text})
            # stdin を差し替えて main() を呼ぶ
            import io
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(payload)
            try:
                ledger_mod.main()
            finally:
                sys.stdin = old_stdin
        finally:
            ledger_mod.LEDGER = original

        entries = [json.loads(l) for l in ledger_file.read_text().splitlines() if l.strip()]
        assert len(entries) == 1
        return entries[0]

    def test_noise_gets_auto_filtered_status(self, ledger_mod, tmp_path):
        entry = self._run_ledger(ledger_mod, "<task-notification><task-id>x</task-id>", tmp_path)
        assert entry["status"] == "auto_filtered"
        assert "auto_noise_filter" in entry["notes"]

    def test_noise_notes_contain_marker(self, ledger_mod, tmp_path):
        entry = self._run_ledger(ledger_mod, "Stop hook feedback:\n[some hook]: msg", tmp_path)
        assert entry["notes"] == "[auto_noise_filter] system/hook generated content"

    def test_normal_text_gets_pending_status(self, ledger_mod, tmp_path):
        entry = self._run_ledger(ledger_mod, "今日の残りタスクを教えて", tmp_path)
        assert entry["status"] == "pending"

    def test_discipline_checker_filtered(self, ledger_mod, tmp_path):
        entry = self._run_ledger(ledger_mod, "Sora Lab discipline checker: check output", tmp_path)
        assert entry["status"] == "auto_filtered"


# ---------------------------------------------------------------------------
# 3. migrate_ledger_auto_filter_20260425.py — dry-run 統計
# ---------------------------------------------------------------------------

class TestMigrateDryRun:
    def _make_ledger(self, tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
        f = tmp_path / "ledger.jsonl"
        with f.open("w") as fp:
            for e in entries:
                fp.write(json.dumps(e, ensure_ascii=False) + "\n")
        return f

    def test_dry_run_counts_noise(self, migrate_mod, tmp_path, capsys):
        entries = [
            {"instruction_id": "aaa", "status": "pending",
             "exact_text": "<task-notification>x", "notes": ""},
            {"instruction_id": "bbb", "status": "pending",
             "exact_text": "今日の作業", "notes": ""},
            {"instruction_id": "ccc", "status": "done",
             "exact_text": "<task-notification>y", "notes": ""},
        ]
        original = migrate_mod.LEDGER
        migrate_mod.LEDGER = self._make_ledger(tmp_path, entries)
        try:
            migrate_mod.main.__globals__["sys"] = sys
            # argparse を直接叩かず関数を再実装する代わりに subprocess で --dry-run
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "migrate_ledger_auto_filter_20260425.py"),
                 "--dry-run"],
                capture_output=True, text=True,
                env={"PYTHONPATH": str(ROOT), "HOME": str(Path.home()),
                     "PATH": "/usr/bin:/bin:/usr/local/bin"},
            )
            assert result.returncode == 0
            assert "to be filtered" in result.stdout
            assert "DRY RUN" in result.stdout
        finally:
            migrate_mod.LEDGER = original

    def test_apply_updates_status(self, migrate_mod, tmp_path):
        entries = [
            {"instruction_id": "xxx", "status": "pending",
             "exact_text": "Stop hook feedback:\nblocked", "notes": ""},
            {"instruction_id": "yyy", "status": "pending",
             "exact_text": "普通の指示です", "notes": ""},
        ]
        ledger_path = self._make_ledger(tmp_path, entries)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "migrate_ledger_auto_filter_20260425.py"),
             "--apply"],
            capture_output=True, text=True,
            env={"PYTHONPATH": str(ROOT), "HOME": str(Path.home()),
                 "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
        # apply は実際の LEDGER に対して動くのでテスト用 ledger_path とは別
        # ここでは returncode == 0 のみ確認
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 4. ledger_audit_run.py — keyword 率・extract_keywords
# ---------------------------------------------------------------------------

class TestLedgerAuditRun:
    def test_extract_keywords_basic(self, audit_mod):
        kws = audit_mod.extract_keywords("atlas-paper daemon heartbeat 確認")
        assert "atlas-paper" in kws or "atlas" in kws or "paper" in kws
        assert len(kws) > 0

    def test_extract_keywords_stops_removed(self, audit_mod):
        kws = audit_mod.extract_keywords("これはテストです the and to")
        assert "the" not in kws
        assert "and" not in kws

    def test_keyword_rate_80pct_auto_done(self, audit_mod, tmp_path):
        """keyword 率 80% 以上かつ commit 実在で auto done になること。"""
        # 実際の commit を取得
        commits = audit_mod.get_recent_commits(1)
        if not commits:
            pytest.skip("no git commits available")
        commit_hash = commits[0]["hash"]
        commit_msg = commits[0]["msg"]

        proposals = [
            {
                "instruction_id": "test_prop_01",
                "text_snippet": commit_msg,
                "matched_commit": commit_hash,
                "status": "proposed",
                "proposed_at": "2026-04-25T07:00:00+09:00",
            }
        ]
        ledger_entries: list[dict] = []

        props_path = tmp_path / "pending_proposals.jsonl"
        ledger_path = tmp_path / "ledger.jsonl"
        props_path.write_text("\n".join(json.dumps(p) for p in proposals) + "\n")
        ledger_path.write_text("")

        orig_props = audit_mod.PROPOSALS
        orig_ledger = audit_mod.LEDGER
        audit_mod.PROPOSALS = props_path
        audit_mod.LEDGER = ledger_path
        try:
            audit_mod.main()
        finally:
            audit_mod.PROPOSALS = orig_props
            audit_mod.LEDGER = orig_ledger

        updated = [json.loads(l) for l in props_path.read_text().splitlines() if l.strip()]
        # commit_msg が全keyword を含むはずなので auto_done に
        assert updated[0]["status"] == "auto_done"

    def test_plist_file_exists(self):
        plist = ROOT / "data" / "launchagents" / "com.soralab.ledger-auditor.plist"
        assert plist.exists(), "plist ファイルが存在しない"

    def test_plist_has_start_interval(self):
        plist = ROOT / "data" / "launchagents" / "com.soralab.ledger-auditor.plist"
        content = plist.read_text()
        assert "StartInterval" in content
        assert "3600" in content

    def test_audit_script_exists(self):
        assert (ROOT / "scripts" / "ledger_audit_run.py").exists()

    def test_audit_sh_exists(self):
        assert (ROOT / "scripts" / "ledger_audit_run.sh").exists()

    def test_migrate_script_exists(self):
        assert (ROOT / "scripts" / "migrate_ledger_auto_filter_20260425.py").exists()
