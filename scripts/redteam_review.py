#!/usr/bin/env python3
"""
redteam_review.py - Red Team 敵対的レビュー起動スクリプト

軍事Red Team（プロイセン Kriegsspiel 1812起源）文化の Atlas 適用版。
対象（ファイル / 提案テキスト）を Red Team エージェント (Opus) に渡し、
攻撃シナリオ・見逃しバグ・失敗モードを強制列挙させる。

Usage:
    python3 scripts/redteam_review.py --file /path/to/target.py
    python3 scripts/redteam_review.py --text "ORBの閾値を0.3%に固定する"
    python3 scripts/redteam_review.py --file A.py --file B.py --label "ORB修正"

Output:
    /Users/yuusakuichio/trading/data/redteam_reports/<timestamp>_<label>.md
    Pushover [SYS/REDTEAM] 通知（severity に応じて priority 調整）
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

BASE_DIR = Path("/Users/yuusakuichio/trading")
REPORT_DIR = BASE_DIR / "data" / "redteam_reports"
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER = "u2cevk8nktib3sr148rw2hs78ecvux"

MAX_CONTENT_CHARS = 60000  # 1ファイル最大


def read_target_files(paths: list[str]) -> str:
    """複数ターゲットファイルを連結して返す"""
    blocks = []
    for p in paths:
        fp = Path(p)
        if not fp.exists():
            blocks.append(f"### {p}\n[FILE NOT FOUND]\n")
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            blocks.append(f"### {p}\n[READ ERROR: {e}]\n")
            continue
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + f"\n...[truncated; total {len(content)} chars]"
        blocks.append(f"### {p}\n```\n{content}\n```\n")
    return "\n".join(blocks)


def build_redteam_prompt(target_text: str, label: str) -> str:
    """Red Team エージェントへの敵対的プロンプトを構築"""
    return f"""あなたは Sora Lab の Red Team 専任エージェントです。
軍事 Red Team 文化（プロイセン Kriegsspiel 1812起源）に基づき、以下の対象を敵対的・懐疑的・アンタゴニスティックに徹底破壊しに行ってください。

## 対象: {label}

{target_text}

## タスク
以下の観点を **全て網羅** し、具体的な攻撃シナリオ・見逃しバグ・失敗モードを強制列挙してください。
「問題ない」「良い」「妥当」等の追認的な表現は禁止です。必ず欠陥を探しに行ってください。

### A. Attack Scenarios（攻撃シナリオ）
- このシステム/提案を壊すには？（外部攻撃者視点）
- 悪意ある入力・API障害・ネットワーク分断・認証情報漏洩で何が起こる？

### B. Overlooked Bugs（見逃しパターン）
- このバグはどう本番まで見逃されるか？
- テストが green でも壊れているパターン
- エッジケース（週末/祝日/早終い/FOMC/SQ/DST/ET-JST変換）
- 時間処理の事故（naive datetime / ±30秒ズレ / 冬時間）

### C. Failure Modes（失敗モード）
- Silent failure / Partial failure / Cascading failure
- Retry storm / Rate limit / API ban リスク

### D. Operational Holes（運用上の穴）
- 誤操作時の挙動、復旧手順の有無、ログだけで原因特定可能か

### E. Strategic / Economic Risks
- Tail risk・Overfitting・Look-ahead bias・Survivorship bias
- 月300万円目標からの逆算で本当に必要か？
- 固定パラメータの残存（環境適応型規律違反）

### F. Contrarian View（皆が合意してても疑う）
- 「即実装」「OK」の前提を1つ以上明示的に反論してください

## 出力形式（厳守）
```
# Red Team Report: {label}
Date: <JST timestamp>

## Severity Assessment
Overall: [CRITICAL / HIGH / MEDIUM / LOW / NOISE]

## Attack Scenarios
1. <攻撃名>: <再現手順> → <被害>
2. ...

## Overlooked Bugs
1. ...

## Failure Modes
1. ...

## Operational Holes
1. ...

## Strategic / Economic Risks
1. ...

## Contrarian View
<皆が合意している前提への反論>

## Mitigation Priorities
P0: ...
P1: ...
P2: ...

## Conclusion
- Ship as-is? [NO / CONDITIONAL YES / YES-WITH-MITIGATIONS]
- 最も危険な単一リスク: <1行>
```

レポート本体のみを出力してください。前置き・後書き不要。
"""


def call_claude_opus(prompt: str, timeout: int = 600) -> tuple[str, int]:
    """Claude Opus を Red Team エージェントとして起動"""
    try:
        proc = subprocess.run(
            [
                "claude", "-p",
                "--model", "claude-opus-4-5",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout or "", proc.returncode
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]", 124
    except FileNotFoundError:
        # claude CLI が無い環境用のフォールバック
        return "[claude CLI not found; skipping LLM call]", 127
    except Exception as e:
        return f"[ERROR: {e}]", 1


def parse_severity(report: str) -> str:
    """レポートから Overall severity を抽出"""
    m = re.search(r"Overall:\s*\[?\s*(CRITICAL|HIGH|MEDIUM|LOW|NOISE)", report, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return "UNKNOWN"


def parse_conclusion_line(report: str) -> str:
    """最も危険な単一リスク行を抽出"""
    m = re.search(r"最も危険な単一リスク:\s*(.+)", report)
    if m:
        return m.group(1).strip()[:200]
    return "(no explicit conclusion)"


def send_pushover(title: str, message: str, priority: int = 0) -> None:
    """Pushover通知"""
    try:
        subprocess.run(
            [
                "curl", "-s",
                "--form-string", f"token={PUSHOVER_TOKEN}",
                "--form-string", f"user={PUSHOVER_USER}",
                "--form-string", f"title={title}",
                "--form-string", f"message={message}",
                "--form-string", f"priority={priority}",
                *(["--form-string", "retry=60", "--form-string", "expire=3600"] if priority >= 1 else []),
                "https://api.pushover.net/1/messages.json",
            ],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass


def save_report(report: str, label: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^\w\-]+", "_", label)[:60]
    fname = f"{ts}_{safe_label}.md"
    fp = REPORT_DIR / fname
    fp.write_text(report, encoding="utf-8")
    return fp


def main() -> int:
    ap = argparse.ArgumentParser(description="Red Team adversarial review")
    ap.add_argument("--file", action="append", default=[], help="Target file path (multiple allowed)")
    ap.add_argument("--text", default="", help="Inline proposal text to review")
    ap.add_argument("--label", default="", help="Label for the report filename")
    ap.add_argument("--dry-run", action="store_true", help="Build prompt only; do not call Claude")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    if not args.file and not args.text:
        print("ERROR: --file or --text required", file=sys.stderr)
        return 2

    label = args.label or (Path(args.file[0]).name if args.file else "inline_text")

    body_parts = []
    if args.file:
        body_parts.append(read_target_files(args.file))
    if args.text:
        body_parts.append(f"### Inline Proposal\n```\n{args.text}\n```\n")
    target_block = "\n".join(body_parts)

    prompt = build_redteam_prompt(target_block, label)

    if args.dry_run:
        print(prompt)
        return 0

    print(f"[redteam] invoking Claude Opus on '{label}' ({len(prompt)} chars)...", file=sys.stderr)
    report, rc = call_claude_opus(prompt, timeout=args.timeout)

    if rc != 0 or len(report.strip()) < 50:
        err_report = (
            f"# Red Team Report: {label}\n"
            f"Date: {datetime.datetime.now(JST).isoformat()}\n\n"
            f"## Severity Assessment\nOverall: UNKNOWN (LLM call failed rc={rc})\n\n"
            f"## Raw Output\n```\n{report}\n```\n"
        )
        fp = save_report(err_report, label)
        send_pushover(
            f"[SYS/REDTEAM] FAIL {label}",
            f"Red Team LLM call failed rc={rc}. See {fp.name}",
            priority=1,
        )
        print(f"[redteam] FAILED rc={rc}. report={fp}", file=sys.stderr)
        return 1

    fp = save_report(report, label)
    severity = parse_severity(report)
    top_risk = parse_conclusion_line(report)

    priority = 1 if severity in {"CRITICAL", "HIGH"} else 0
    send_pushover(
        f"[SYS/REDTEAM] {severity} {label}",
        f"{severity} / {top_risk}\nreport: {fp.name}",
        priority=priority,
    )

    print(f"[redteam] done severity={severity} report={fp}", file=sys.stderr)
    print(str(fp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
