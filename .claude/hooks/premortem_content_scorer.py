#!/usr/bin/env python3
"""premortem_content_scorer.py — PreToolUse(Agent) hook

premortem レポートの内容品質を regex でスコアリングし、
閾値未達なら block (exit 2) する。

評価軸:
  1. 文字数 (chars)           : 閾値 PREMORTEM_MIN_CHARS (デフォルト 400)
  2. シナリオ数 (scenarios)   : 閾値 PREMORTEM_MIN_SCENARIOS (デフォルト 5)
  3. mitigation 数 (mitigation): 閾値 PREMORTEM_MIN_MITIGATION (デフォルト 5)
  4. incident DB 参照数 (idb) : 閾値 PREMORTEM_MIN_INCIDENTDB (デフォルト 1)

入力: stdin から Claude hook JSON (tool_name / tool_input)
  - tool_name が "Agent" か "Task" でなければ即 pass
  - tool_input.prompt に premortem キーワードがなければ pass

Bypass: 環境変数 PREMORTEM_SCORER_BYPASS=1
無効化: PREMORTEM_SCORER_ENABLED != "1" で常時 pass

B16 asyncio 禁止: 非同期処理一切不使用。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# ── 設定 ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parents[3]  # trading/
LOG_FILE = BASE_DIR / "data" / "logs" / "premortem_content_scorer.log"
BYPASS_LOG = BASE_DIR / "data" / "governance" / "audit_bypass_log.jsonl"

MIN_CHARS = int(os.environ.get("PREMORTEM_MIN_CHARS", "400"))
MIN_SCENARIOS = int(os.environ.get("PREMORTEM_MIN_SCENARIOS", "5"))
MIN_MITIGATION = int(os.environ.get("PREMORTEM_MIN_MITIGATION", "5"))
MIN_INCIDENTDB = int(os.environ.get("PREMORTEM_MIN_INCIDENTDB", "1"))

TS = time.strftime("%Y-%m-%d %H:%M:%S JST")

# ── regex パターン ─────────────────────────────────────────────────────────────
# シナリオ: 「シナリオ N」「scenario N」「## N.」「- シナリオ:」等
RE_SCENARIO = re.compile(
    r"(?:シナリオ\s*\d+|scenario\s*\d+|##\s*\d+\.\s|\*\*scenario|\bscenario\b\s*[:\-])",
    re.IGNORECASE | re.MULTILINE,
)

# mitigation: 「対策」「mitigation」「緩和策」「対応策」等
RE_MITIGATION = re.compile(
    r"(?:対策|mitigation|緩和策|対応策|countermeasure|軽減|予防策)",
    re.IGNORECASE | re.MULTILINE,
)

# incident DB 参照: 「past incident」「障害事例」「過去事例」「data/postmortems」等
RE_INCIDENTDB = re.compile(
    r"(?:past\s+incident|障害事例|過去事例|data/postmortems|incident\s+db|incident_db|インシデントDB)",
    re.IGNORECASE | re.MULTILINE,
)


def _log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{TS}] {msg}\n")


def _bypass_log(reason: str) -> None:
    import datetime
    BYPASS_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "premortem_scorer_bypass",
        "reason": reason,
        "pid": os.getpid(),
    }
    with open(BYPASS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def score_text(text: str) -> dict[str, int]:
    return {
        "chars": len(text),
        "scenarios": len(RE_SCENARIO.findall(text)),
        "mitigation": len(RE_MITIGATION.findall(text)),
        "incidentdb": len(RE_INCIDENTDB.findall(text)),
    }


def check_thresholds(scores: dict[str, int]) -> list[str]:
    """閾値未達の項目リストを返す。空なら全通過。"""
    failures: list[str] = []
    if scores["chars"] < MIN_CHARS:
        failures.append(f"chars={scores['chars']} < {MIN_CHARS}")
    if scores["scenarios"] < MIN_SCENARIOS:
        failures.append(f"scenarios={scores['scenarios']} < {MIN_SCENARIOS}")
    if scores["mitigation"] < MIN_MITIGATION:
        failures.append(f"mitigation={scores['mitigation']} < {MIN_MITIGATION}")
    if scores["incidentdb"] < MIN_INCIDENTDB:
        failures.append(f"incidentdb={scores['incidentdb']} < {MIN_INCIDENTDB}")
    return failures


def main() -> int:
    # ── 無効化 ─────────────────────────────────────────────────────────────────
    if os.environ.get("PREMORTEM_SCORER_ENABLED", "0") != "1":
        return 0

    # ── bypass ─────────────────────────────────────────────────────────────────
    if os.environ.get("PREMORTEM_SCORER_BYPASS", "0") == "1":
        reason = os.environ.get("PREMORTEM_SCORER_BYPASS_REASON", "未指定")
        _bypass_log(reason)
        _log(f"BYPASS reason={reason}")
        return 0

    # ── stdin 読み込み ──────────────────────────────────────────────────────────
    raw = sys.stdin.read()
    if not raw.strip():
        return 0

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    tool_name = data.get("tool_name", "")
    if tool_name not in ("Agent", "Task"):
        return 0

    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return 0

    prompt = (
        tool_input.get("prompt", "")
        or tool_input.get("description", "")
        or tool_input.get("content", "")
        or ""
    )

    # premortem キーワードが含まれない Agent call はスキップ
    if "premortem" not in prompt.lower():
        return 0

    scores = score_text(prompt)
    failures = check_thresholds(scores)

    if not failures:
        _log(f"PASS tool={tool_name} scores={scores}")
        return 0

    _log(f"BLOCK tool={tool_name} scores={scores} failures={failures}")
    print(
        "\n"
        "[PREMORTEM_CONTENT_SCORER] premortem レポートが品質基準を満たしていません — block\n"
        "\n"
        f"  スコア結果: {scores}\n"
        f"  未達項目:   {', '.join(failures)}\n"
        "\n"
        "  必要な品質基準:\n"
        f"    文字数           >= {MIN_CHARS}  (env: PREMORTEM_MIN_CHARS)\n"
        f"    シナリオ数       >= {MIN_SCENARIOS}  (env: PREMORTEM_MIN_SCENARIOS)\n"
        f"    mitigation 数    >= {MIN_MITIGATION}  (env: PREMORTEM_MIN_MITIGATION)\n"
        f"    incident DB 参照 >= {MIN_INCIDENTDB}  (env: PREMORTEM_MIN_INCIDENTDB)\n"
        "\n"
        "  レポートを充実させてから再実行してください。\n"
        f"  log: {LOG_FILE}\n"
        "\n"
        "  一時 bypass: export PREMORTEM_SCORER_BYPASS=1 "
        "PREMORTEM_SCORER_BYPASS_REASON='<理由>'\n",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
