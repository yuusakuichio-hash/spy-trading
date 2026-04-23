#!/usr/bin/env python3
"""
navigator_antipattern_detector.py — Layer 2 builder behavior monitor
PreToolUse hook: builderのtool_useシーケンスを見てantipatternをリアルタイム検出

Reflexion (Shinn 2023) + Aviation Two-Person Rule 融合設計
- NAVIGATOR_MODE=warn  (default): stderr出力のみ・exit 0
- NAVIGATOR_MODE=block : stderr出力 + exit 2 (hard block)

検出パターン:
  P1: Write後にpytest tests/test_X.pyのみ実行 (全体pytestなし) x3連続
  P2: 同一ファイルへのEdit x3連続・間にRead/Bashなし
  P3: Write x5連続・間にRead/Bashなし
  P4: 仕様書未確認で新規コードWrite
  P5: Bash(スクリプト実行)後ログ確認なしで次のWriteへ進む
"""

import json
import os
import sys
import re
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

LOG_DIR = Path("/Users/yuusakuichio/trading/data/logs")
LOG_FILE = LOG_DIR / "navigator_antipattern.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

NAVIGATOR_MODE = os.environ.get("NAVIGATOR_MODE", "warn").lower()
# warn: stderr only, exit 0
# block: stderr + exit 2


@dataclass
class Detection:
    pattern_id: str
    name: str
    message: str
    reflexion_note: str
    recommended_action: str


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def _extract_tool_uses(transcript_path: str, n: int = 30) -> list:
    """セッションJSONLから最新N件のtool_useエントリを抽出する。"""
    path = Path(transcript_path)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    tool_uses = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        content = d.get("message", {}).get("content", [])
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_uses.append({
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                })

    return tool_uses[-n:]


def _current_tool(event: dict) -> dict:
    """現在のPreToolUseイベントをtool_use形式に変換する。"""
    return {
        "name": event.get("tool_name", ""),
        "input": event.get("tool_input", {}),
    }


def _combine_sequence(history: list, current: dict) -> list:
    """履歴 + 今回のツールを結合したシーケンスを返す。"""
    return history + [current]


def detect_p1_write_then_selective_pytest(seq: list) -> Optional[Detection]:
    """
    P1: Write後にpytest tests/test_X.pyのみ実行 (全体pytestなし) が3回連続
    選択的テストで品質チェックをごまかすパターン
    """
    consecutive_violations = 0
    i = len(seq) - 1
    while i >= 1:
        cur = seq[i]
        prev = seq[i - 1]
        if cur["name"] == "Bash" and prev["name"] == "Write":
            cmd = cur["input"].get("command", "")
            has_selective = bool(re.search(r"\bpytest\s+tests/test_\w+\.py", cmd))
            has_full = bool(re.search(
                r"\bpytest\s+(tests/\s*($|-v|\s)|tests/\s+-|\.\s+-|\.\s*$)",
                cmd
            ))
            if has_selective and not has_full:
                consecutive_violations += 1
                i -= 2
                continue
        break

    if consecutive_violations >= 3:
        return Detection(
            pattern_id="P1",
            name="Write->selective pytest (全体なし) x3連続",
            message=f"Write後に pytest tests/test_X.py のみ実行が{consecutive_violations}回連続。全体テストなし。",
            reflexion_note="Reflexion 2023: 部分検証は隠れた失敗を見逃す。全体回帰なしの宣言は虚偽完了の温床。",
            recommended_action="pytest tests/ -v を実行してから次のWriteに進んでください",
        )
    return None


def detect_p2_edit_without_read(seq: list) -> Optional[Detection]:
    """
    P2: 同一ファイルへのEdit x3連続・間にRead/Bashなし
    ファイル内容を把握せずに盲目editするパターン
    """
    if len(seq) < 3:
        return None

    tail = seq[-3:]
    if not all(t["name"] == "Edit" for t in tail):
        return None

    file_paths = [t["input"].get("file_path", "") for t in tail]
    if len(set(file_paths)) != 1 or not file_paths[0]:
        return None

    target_file = file_paths[0]

    prior = seq[:-3]
    has_read_of_file = any(
        t["name"] == "Read" and t["input"].get("file_path", "") == target_file
        for t in prior[-10:]
    )
    has_bash_between = any(t["name"] == "Bash" for t in prior[-5:])

    if not has_read_of_file and not has_bash_between:
        return Detection(
            pattern_id="P2",
            name="同一ファイルEdit x3連続 Read/Bashなし",
            message=f"'{target_file}' への Edit が3回連続 Read/Bashなし。ファイル内容未把握の盲目editパターン。",
            reflexion_note="Aviation Two-Person Rule: 変更前に独立した現状確認が必要。Read->確認->Editが正しいシーケンス。",
            recommended_action=f"先に Read {target_file} でファイル内容を確認してください",
        )
    return None


def detect_p3_write_burst(seq: list) -> Optional[Detection]:
    """
    P3: Write x5連続・間にRead/Bashなし
    大量ファイル一気出しで品質低下するパターン
    """
    if len(seq) < 5:
        return None

    tail = seq[-5:]
    if not all(t["name"] == "Write" for t in tail):
        return None

    return Detection(
        pattern_id="P3",
        name="Write x5連続バースト (Read/Bashなし)",
        message="Write tool が5回連続・間にRead/Bashなし。大量ファイル一気出しは品質低下パターン。",
        reflexion_note="Reflexion 2023: 各実装後の即検証なしは複合バグを生む。Write->Bash->Write のサイクルが正しい。",
        recommended_action="Bash で構文チェック/テスト実行してから次のWriteに進んでください",
    )


def detect_p4_write_without_spec_check(seq: list) -> Optional[Detection]:
    """
    P4: 仕様書未確認で新規コードWrite
    data/specs/, docs/ のReadなしで新規コードファイルを作成するパターン
    """
    if not seq:
        return None

    current = seq[-1]
    if current["name"] != "Write":
        return None

    prior = seq[:-1]
    spec_patterns = [
        r"data/specs/",
        r"/docs/",
        r"CLAUDE\.md",
        r"\.claude/agents/",
        r"data/research_",
    ]
    has_spec_read = any(
        t["name"] in ("Read", "Glob", "Grep") and any(
            re.search(p, str(t["input"]), re.IGNORECASE)
            for p in spec_patterns
        )
        for t in prior[-20:]
    )

    if has_spec_read:
        return None

    file_path = current["input"].get("file_path", "")
    is_new_code_file = bool(re.search(r"\.(py|sh)$", file_path))
    is_excluded = bool(re.search(
        r"(data/logs|data/.*\.json|data/.*\.jsonl|\.claude/projects|__pycache__)",
        file_path
    ))

    if is_new_code_file and not is_excluded:
        prior5 = prior[-5:] if len(prior) >= 5 else prior
        reads_in_prior5 = sum(1 for t in prior5 if t["name"] in ("Read", "Glob", "Grep", "Bash"))
        if reads_in_prior5 == 0:
            return Detection(
                pattern_id="P4",
                name="仕様書未確認で新規コードWrite",
                message=f"'{file_path}' を新規作成する前に data/specs/ や docs/ の仕様確認がなし。",
                reflexion_note="CLAUDE.md鉄則: 実装前に必ず公式ドキュメントを調べる。",
                recommended_action="Glob/Read で仕様・既存実装を確認してから実装してください",
            )
    return None


def detect_p5_bash_without_log_check(seq: list) -> Optional[Detection]:
    """
    P5: Bash(スクリプト実行)後にログ確認なしで次のWriteへ進む
    「動作する」を確認せず次の変更へ進むパターン
    """
    if len(seq) < 3:
        return None

    if seq[-1]["name"] != "Write":
        return None

    second_last = seq[-2]
    if second_last["name"] != "Bash":
        return None

    cmd = second_last["input"].get("command", "")
    is_script_run = bool(re.search(
        r"python3?\s+\S+\.py|bash\s+\S+\.sh|pytest\s|\.\/[a-zA-Z]",
        cmd
    ))
    if not is_script_run:
        return None

    if len(seq) >= 3:
        third_last = seq[-3]
        if third_last["name"] == "Read":
            return None

    has_log_read = bool(re.search(r"(cat|tail|head)\s+.*log|/logs/", cmd))
    if has_log_read:
        return None

    return Detection(
        pattern_id="P5",
        name="Bash実行後ログ確認なしで次Write",
        message=f"スクリプト実行: '{cmd[:80]}' 後にログ/出力確認なしで Write へ進もうとしている。",
        reflexion_note="Aviation Two-Person Rule: 実行後の結果確認は独立したステップ。'動くはず'で次に進むな。",
        recommended_action="Bash で出力/ログを確認 (tail data/logs/xxx.log) してから Write してください",
    )


def _emit_detection(det: Detection) -> None:
    """検出結果をstderrに出力し、ログに記録する。"""
    border = "=" * 65
    sys.stderr.write(f"\n{border}\n")
    sys.stderr.write(f"[NAVIGATOR] Antipattern detected: {det.pattern_id} -- {det.name}\n")
    sys.stderr.write(f"{border}\n")
    sys.stderr.write(f"  検出内容: {det.message}\n")
    sys.stderr.write(f"  世界の知見 (Reflexion 2023 / Aviation Two-Person Rule):\n")
    sys.stderr.write(f"    {det.reflexion_note}\n")
    sys.stderr.write(f"  推奨対策: {det.recommended_action}\n")
    mode_str = "WARN (block無効)" if NAVIGATOR_MODE == "warn" else "BLOCK (ツール実行停止)"
    sys.stderr.write(f"  モード: {mode_str}  (NAVIGATOR_MODE={NAVIGATOR_MODE})\n")
    sys.stderr.write(f"{border}\n\n")
    _log(f"[{det.pattern_id}] {det.name} | mode={NAVIGATOR_MODE}")


DETECTORS = [
    detect_p1_write_then_selective_pytest,
    detect_p2_edit_without_read,
    detect_p3_write_burst,
    detect_p4_write_without_spec_check,
    detect_p5_bash_without_log_check,
]


def main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    transcript_path = event.get("transcript_path", "")
    if not transcript_path:
        sys.exit(0)

    history = _extract_tool_uses(transcript_path, n=30)
    current = _current_tool(event)
    seq = _combine_sequence(history, current)

    detected_any = False
    for detector in DETECTORS:
        try:
            result = detector(seq)
        except Exception as e:
            _log(f"[WARN] detector {detector.__name__} raised: {e}")
            continue
        if result is not None:
            _emit_detection(result)
            detected_any = True
            break  # 最初の検出のみ報告 (複数同時ブロックは混乱を招く)

    if detected_any and NAVIGATOR_MODE == "block":
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
