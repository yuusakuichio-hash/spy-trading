#!/usr/bin/env python3
"""
tests/test_guard_hooks.py
Unit tests for Layer 1 pre-commit hooks:
  - selective_test_detector.sh
  - false_claim_detector.sh
  - time_estimate_sanity.sh
"""

import json
import os
import subprocess
import tempfile
import textwrap
import pytest

HOOKS_DIR = "/Users/yuusakuichio/trading/.claude/hooks"
SELECTIVE_HOOK = os.path.join(HOOKS_DIR, "selective_test_detector.sh")
FALSE_CLAIM_HOOK = os.path.join(HOOKS_DIR, "false_claim_detector.sh")
TIME_ESTIMATE_HOOK = os.path.join(HOOKS_DIR, "time_estimate_sanity.sh")


def run_hook(hook_path: str, stdin_data: dict) -> subprocess.CompletedProcess:
    """hookスクリプトをsubprocessで実行してCompletedProcessを返す。"""
    return subprocess.run(
        ["bash", hook_path],
        input=json.dumps(stdin_data).encode(),
        capture_output=True,
        timeout=10,
    )


def make_jsonl_with_events(events: list[dict]) -> str:
    """イベントリストからJSONLファイルを一時ファイルに書き込みパスを返す。"""
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for e in events:
        tf.write(json.dumps(e, ensure_ascii=False) + "\n")
    tf.close()
    return tf.name


# ─────────────────────────────────────────────────────────────────────────────
# Hook 1: selective_test_detector.sh
# ─────────────────────────────────────────────────────────────────────────────


class TestSelectiveTestDetector:
    """Hook 1 の陽性・陰性テスト。"""

    def _make_transcript(self, bash_cmds: list[str], assistant_texts: list[str]) -> str:
        """Bash コマンドとアシスタントテキストを含む最小限の JSONL を作る。"""
        events = []
        for cmd in bash_cmds:
            events.append({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
                    ]
                }
            })
        for txt in assistant_texts:
            events.append({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": txt}]}
            })
        return make_jsonl_with_events(events)

    def test_positive_selective_pytest_with_claim(self):
        """陽性: selective pytest + 全合格宣言 → exit 2 (HARD BLOCK)"""
        transcript = self._make_transcript(
            bash_cmds=["pytest tests/test_chronos_bot.py -v"],
            assistant_texts=["テスト全合格。実装完了です。"],
        )
        try:
            result = run_hook(
                SELECTIVE_HOOK,
                {"transcript_path": transcript, "tool_input": {}},
            )
            assert result.returncode == 2, (
                f"Expected exit 2 (BLOCK), got {result.returncode}\n"
                f"stderr: {result.stderr.decode()}"
            )
            assert b"SELECTIVE TEST GUARD" in result.stderr
        finally:
            os.unlink(transcript)

    def test_no_transcript_mode_fail_open(self):
        """Redteam r3 C-1: transcript 不在時は scope 判定不能のため fail-open (exit 0)。
        新方針: no-transcript fallback で raw prompt を BLOCK しない
        (claim_ledger_guard / discipline_guard 等で別途検出される)。
        検証コメント: grep result で fail-open 挙動を確認。"""
        data = {
            "tool_input": {
                "prompt": "pytest tests/test_chronos_bot.py -v all tests passed"
            }
        }
        result = run_hook(SELECTIVE_HOOK, data)
        assert result.returncode == 0, (
            f"Expected exit 0 (fail-open), got {result.returncode}\n"
            f"stderr: {result.stderr.decode()}"
        )

    def test_negative_full_pytest_with_claim(self):
        """陰性: 全体 pytest tests/ + 全合格宣言 → exit 0 (通過)"""
        transcript = self._make_transcript(
            bash_cmds=["pytest tests/ -v"],
            assistant_texts=["全テスト合格を確認しました。"],
        )
        try:
            result = run_hook(
                SELECTIVE_HOOK,
                {"transcript_path": transcript, "tool_input": {}},
            )
            assert result.returncode == 0, (
                f"Expected exit 0 (PASS), got {result.returncode}\n"
                f"stderr: {result.stderr.decode()}"
            )
        finally:
            os.unlink(transcript)

    def test_negative_selective_pytest_no_claim(self):
        """陰性: selective pytest だが「全合格」宣言なし → exit 0"""
        transcript = self._make_transcript(
            bash_cmds=["pytest tests/test_chronos_bot.py -v"],
            assistant_texts=["テスト実行しました。"],
        )
        try:
            result = run_hook(
                SELECTIVE_HOOK,
                {"transcript_path": transcript, "tool_input": {}},
            )
            assert result.returncode == 0, (
                f"Expected exit 0, got {result.returncode}"
            )
        finally:
            os.unlink(transcript)

    def test_negative_empty_input(self):
        """陰性: 空 JSON → exit 0 (クラッシュしない)"""
        result = run_hook(SELECTIVE_HOOK, {})
        assert result.returncode == 0

    def test_boundary_beyond_200_lines_still_narrows(self):
        """Redteam r3 C-2: 境界が末尾 200 行より前でも新ロジックは全域境界探索。
        旧: recent=lines[-200:] 後に境界探索 → 境界消失で scope 退化。
        新: 全範囲 (最大 10000 行) で境界探索 → 境界後全域を scope 化。"""
        events = []
        events.append({"type": "user", "message": {"content": "real user prompt here"}})
        events.append({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "pytest tests/test_z.py -v"}}
            ]},
        })
        events.append({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "all tests passed"}]},
        })
        for _ in range(300):
            events.append({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            })
        transcript = make_jsonl_with_events(events)
        try:
            result = run_hook(
                SELECTIVE_HOOK,
                {"transcript_path": transcript, "tool_input": {}},
            )
            assert result.returncode == 2, (
                f"Expected BLOCK for selective+claim past 200-line window.\n"
                f"got {result.returncode}\nstderr: {result.stderr.decode()}"
            )
        finally:
            os.unlink(transcript)

    def test_empty_tooluseresult_key_still_skipped(self):
        """Redteam r3 C-3: toolUseResult キー存在判定（空 dict など falsy でも
        schema 上 tool_result user event はキー存在で識別する）。
        空 {} でも境界に使わず、周囲の assistant 証跡が scope に残って BLOCK される。"""
        events = [
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "pytest tests/test_foo.py -v"}}
                ]},
            },
            {
                "type": "user",
                "toolUseResult": {},
                "message": {"content": "ignore"},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "all tests passed"}]},
            },
        ]
        transcript = make_jsonl_with_events(events)
        try:
            result = run_hook(
                SELECTIVE_HOOK,
                {"transcript_path": transcript, "tool_input": {}},
            )
            assert result.returncode == 2, (
                f"Expected BLOCK. Empty toolUseResult must not act as boundary.\n"
                f"got {result.returncode}\nstderr: {result.stderr.decode()}"
            )
        finally:
            os.unlink(transcript)

    def test_positive_tool_result_only_user_does_not_narrow_scope(self):
        """案 E': tool_result のみの user event は境界にしない。
        同ターン内の selective + claim が scope に残り BLOCK される。"""
        events = [
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "pytest tests/test_foo.py -v"}}
                ]},
            },
            {
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
                ]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "all tests passed"}]},
            },
        ]
        transcript = make_jsonl_with_events(events)
        try:
            result = run_hook(
                SELECTIVE_HOOK,
                {"transcript_path": transcript, "tool_input": {}},
            )
            assert result.returncode == 2, (
                f"Expected exit 2 (BLOCK). tool_result user should not narrow scope.\n"
                f"got {result.returncode}\nstderr: {result.stderr.decode()}"
            )
        finally:
            os.unlink(transcript)

    def test_negative_past_turn_not_leaking_across_user_msg(self):
        """回帰: 過去ターンの selective pytest + claim があっても、
        その後の raw user message 以降に新しい根拠がなければ PASS。
        (2026-04-23 修正: 古い状態で新規プロンプトを永続 BLOCK 誤作動)"""
        events = [
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "pytest tests/test_foo.py -v"}}
                ]},
            },
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "all tests passed"}
                ]},
            },
            {
                "type": "user",
                "message": {"content": "are you stuck?"},
            },
        ]
        transcript = make_jsonl_with_events(events)
        try:
            result = run_hook(
                SELECTIVE_HOOK,
                {"transcript_path": transcript, "tool_input": {}},
            )
            assert result.returncode == 0, (
                f"Expected exit 0 (PASS), got {result.returncode}\n"
                f"stderr: {result.stderr.decode()}"
            )
        finally:
            os.unlink(transcript)


# ─────────────────────────────────────────────────────────────────────────────
# Hook 2: false_claim_detector.sh
# ─────────────────────────────────────────────────────────────────────────────


class TestFalseClaimDetector:
    """Hook 2 の陽性・陰性テスト。ブロックしない（exit 0 だが WARN ログ）。"""

    def _make_transcript(self, assistant_texts: list[str], bash_cmds: list[str] = None) -> str:
        events = []
        for cmd in (bash_cmds or []):
            events.append({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
                    ]
                }
            })
        for txt in assistant_texts:
            events.append({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": txt}]}
            })
        return make_jsonl_with_events(events)

    def test_positive_completion_claim_no_pytest(self, tmp_path):
        """陽性: 完了宣言あり・pytest 証跡なし → WARN ログ記録 (exit 0)"""
        transcript = self._make_transcript(
            assistant_texts=["実装完了しました。全て正常動作を確認しました。"],
        )
        try:
            result = run_hook(
                FALSE_CLAIM_HOOK,
                {"transcript_path": transcript},
            )
            assert result.returncode == 0, "false_claim_detector must not BLOCK (exit 0)"
            # stderr に WARN メッセージが含まれるか
            assert b"FALSE CLAIM DETECTOR" in result.stderr, (
                f"Expected warning in stderr, got: {result.stderr.decode()}"
            )
        finally:
            os.unlink(transcript)

    def test_negative_no_completion_claim(self):
        """陰性: 完了宣言なし → WARN しない"""
        transcript = self._make_transcript(
            assistant_texts=["デバッグ中です。"],
        )
        try:
            result = run_hook(
                FALSE_CLAIM_HOOK,
                {"transcript_path": transcript},
            )
            assert result.returncode == 0
            assert b"FALSE CLAIM DETECTOR" not in result.stderr
        finally:
            os.unlink(transcript)

    def test_negative_completion_claim_with_pytest_output(self):
        """陰性: 完了宣言あり・pytest stdout あり → 警告なし"""
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/ -v"}}
                    ]
                }
            },
            {
                "type": "user",
                "toolUseResult": {
                    "stdout": "====== 42 passed, 0 failed in 5.23s ======",
                    "stderr": ""
                }
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "全合格を確認し実装完了です。"}]}
            }
        ]
        transcript = make_jsonl_with_events(events)
        try:
            result = run_hook(
                FALSE_CLAIM_HOOK,
                {"transcript_path": transcript},
            )
            assert result.returncode == 0
            assert b"FALSE CLAIM DETECTOR" not in result.stderr, (
                f"Should not warn when pytest output exists: {result.stderr.decode()}"
            )
        finally:
            os.unlink(transcript)

    def test_negative_no_transcript(self):
        """陰性: transcript なし → exit 0 (静かにスルー)"""
        result = run_hook(FALSE_CLAIM_HOOK, {"transcript_path": "/nonexistent/path.jsonl"})
        assert result.returncode == 0


# ─────────────────────────────────────────────────────────────────────────────
# Hook 3: time_estimate_sanity.sh
# ─────────────────────────────────────────────────────────────────────────────


class TestTimeEstimateSanity:
    """Hook 3 の陽性・陰性テスト。stdout に警告テキスト出力（exit 0）。"""

    def _make_input(self, prompt: str) -> dict:
        return {"tool_input": {"prompt": prompt}}

    def test_positive_2_3_hours(self):
        """陽性: '2-3時間' 見積もり → stdout に警告"""
        result = run_hook(
            TIME_ESTIMATE_HOOK,
            self._make_input("このタスクは2-3時間かかります。"),
        )
        assert result.returncode == 0
        assert b"TIME ESTIMATE SANITY" in result.stdout, (
            f"Expected TIME ESTIMATE SANITY in stdout, got: {result.stdout.decode()}"
        )

    def test_positive_4_hours(self):
        """陽性: '4時間' 見積もり → stdout に警告"""
        result = run_hook(
            TIME_ESTIMATE_HOOK,
            self._make_input("実装に4時間見積もっています。"),
        )
        assert result.returncode == 0
        assert b"TIME ESTIMATE SANITY" in result.stdout

    def test_positive_half_day(self):
        """陽性: '半日' → stdout に警告"""
        result = run_hook(
            TIME_ESTIMATE_HOOK,
            self._make_input("半日の作業が必要です。"),
        )
        assert result.returncode == 0
        assert b"TIME ESTIMATE SANITY" in result.stdout

    def test_negative_30_minutes(self):
        """陰性: '30分' → 警告なし"""
        result = run_hook(
            TIME_ESTIMATE_HOOK,
            self._make_input("このタスクは30分で完了できます。"),
        )
        assert result.returncode == 0
        assert b"TIME ESTIMATE SANITY" not in result.stdout

    def test_negative_1_hour(self):
        """陰性: '1時間' (< 2) → 警告なし"""
        result = run_hook(
            TIME_ESTIMATE_HOOK,
            self._make_input("1時間程度で終わります。"),
        )
        assert result.returncode == 0
        assert b"TIME ESTIMATE SANITY" not in result.stdout

    def test_negative_empty_prompt(self):
        """陰性: 空 prompt → exit 0 クラッシュなし"""
        result = run_hook(TIME_ESTIMATE_HOOK, {})
        assert result.returncode == 0
