#!/usr/bin/env python3
"""
estimate_historical_calibration.py — 見積もり甘さの物理防御 (P0-6)

flow_audit 指摘: ソラが「1 ヶ月」と言うものが実務 2-3 時間という過小評価の常習化。
本 hook は ソラが応答に時間見積もりを書いた瞬間、過去実績からの calibration を
強制注記させる。

ゆうさくさん 2026-04-22 指摘:
> 「あなたの1ヶ月報告は実務2、3時間レベルだから、いつもね」

動作モード:
  --check: 応答 text を stdin で受け取り、時間表現があれば calibration notice を
           stdout に出力（PostToolUse or UserPromptSubmit hook 想定）
  --record: 実測時間を data/governance/cycle_estimates_actual.jsonl に追記

重要: LLM Budget と同様、LLM を呼ばない機械的 hook。
"""
import argparse
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

RECORD_PATH = Path(__file__).parent.parent.parent / "data" / "governance" / "cycle_estimates_actual.jsonl"
RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)

# 時間表現の検出パターン（日本語・英語）
TIME_PATTERNS = [
    # 「X ヶ月」「X カ月」
    (re.compile(r"(\d+)\s*[ヶか]月"), "month"),
    # 「X 週間」
    (re.compile(r"(\d+)\s*週間?"), "week"),
    # 「X 日間?」（ただし日付 2026-04-22 にマッチしないよう注意）
    (re.compile(r"(?<!\d-)(\d+)\s*日間?(?!-)"), "day"),
    # 「X 時間」
    (re.compile(r"(\d+)\s*時間"), "hour"),
    # 「X 分(間?)」
    (re.compile(r"(\d+)\s*分間?(?![a-z])"), "minute"),
    # 英語
    (re.compile(r"(\d+)\s*months?", re.I), "month"),
    (re.compile(r"(\d+)\s*weeks?", re.I), "week"),
    (re.compile(r"(\d+)\s*days?", re.I), "day"),
    (re.compile(r"(\d+)\s*hours?", re.I), "hour"),
    (re.compile(r"(\d+)\s*minutes?", re.I), "minute"),
]

# 見積もり文脈のキーワード（これが近辺にあれば「見積もり」と判定）
ESTIMATE_KEYWORDS = [
    "所要", "かかる", "完了", "実装", "見積", "想定", "予定",
    "takes", "estimate", "complete", "implement",
]


def detect_estimates(text: str) -> list[dict]:
    """text から時間見積もり表現を抽出"""
    if not text:
        return []
    hits = []
    for pattern, unit in TIME_PATTERNS:
        for m in pattern.finditer(text):
            # 前後 30 文字の文脈
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            context = text[start:end]
            # 見積もり文脈キーワードが近傍にあるか
            has_estimate_keyword = any(kw in context for kw in ESTIMATE_KEYWORDS)
            if has_estimate_keyword:
                hits.append({
                    "value": int(m.group(1)),
                    "unit": unit,
                    "match": m.group(0),
                    "context": context,
                })
    return hits


def get_historical_calibration() -> dict:
    """過去実測データから calibration ratio を算出"""
    if not RECORD_PATH.exists():
        return {
            "samples": 0,
            "median_ratio": 3.0,  # Red Team 推奨初期値
            "p95_ratio": 5.0,
            "note": "過去実測データなし・デフォルト 3.0x 補正（Red Team 推奨初期値）",
        }
    ratios = []
    try:
        with RECORD_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    est = rec.get("estimated_minutes", 0)
                    actual = rec.get("actual_minutes", 0)
                    if est > 0 and actual > 0:
                        ratios.append(actual / est)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    if not ratios:
        return {
            "samples": 0,
            "median_ratio": 3.0,
            "p95_ratio": 5.0,
            "note": "過去実測データなし・デフォルト 3.0x 補正",
        }

    ratios_sorted = sorted(ratios)
    n = len(ratios_sorted)
    median = ratios_sorted[n // 2]
    p95_idx = max(0, min(n - 1, int(n * 0.95)))
    p95 = ratios_sorted[p95_idx]

    return {
        "samples": n,
        "median_ratio": round(median, 2),
        "p95_ratio": round(p95, 2),
        "note": f"過去 {n} サンプルの実測比",
    }


def format_calibration_notice(estimates: list[dict], calib: dict) -> str:
    """見積もり + calibration の注記文を生成"""
    if not estimates:
        return ""
    lines = [
        "",
        "---",
        "### 📏 historical calibration（見積もり甘さ物理防御）",
        "",
        f"検出した時間見積もり: {len(estimates)} 件",
    ]
    for e in estimates[:5]:
        lines.append(f"- `{e['match']}`（{e['unit']}）")
    lines.append("")
    lines.append(
        f"**過去実測 calibration**: median {calib['median_ratio']}x / p95 {calib['p95_ratio']}x"
        f"（{calib['note']}）"
    )
    lines.append("")
    lines.append(
        f"**補正後の目安**: 表示見積もりの {calib['median_ratio']} 倍（中央値）〜"
        f"{calib['p95_ratio']} 倍（95%tile）を現実ラインとして参照してください。"
    )
    return "\n".join(lines)


def check_mode() -> int:
    """stdin から応答 text を受け取り、calibration notice を stdout に出力"""
    try:
        text = sys.stdin.read()
    except Exception:
        return 0
    if not text:
        return 0

    estimates = detect_estimates(text)
    if not estimates:
        return 0

    calib = get_historical_calibration()
    notice = format_calibration_notice(estimates, calib)
    print(notice)
    return 0


def record_mode(estimated_minutes: int, actual_minutes: int, task: str, note: str) -> int:
    """実測時間を記録"""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task": task,
        "estimated_minutes": estimated_minutes,
        "actual_minutes": actual_minutes,
        "ratio": round(actual_minutes / estimated_minutes, 3) if estimated_minutes > 0 else None,
        "note": note,
    }
    with RECORD_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"recorded: {json.dumps(rec, ensure_ascii=False)}")
    return 0


def summary_mode() -> int:
    """calibration summary を表示"""
    calib = get_historical_calibration()
    print(json.dumps(calib, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="historical calibration hook")
    parser.add_argument("--check", action="store_true",
                        help="stdin text をチェックして calibration notice を出力")
    parser.add_argument("--record", action="store_true",
                        help="実測データ記録モード")
    parser.add_argument("--summary", action="store_true",
                        help="現在の calibration 係数を表示")
    parser.add_argument("--estimated", type=int, default=0,
                        help="（record モード）見積もり分")
    parser.add_argument("--actual", type=int, default=0,
                        help="（record モード）実測分")
    parser.add_argument("--task", default="", help="（record モード）task 名")
    parser.add_argument("--note", default="", help="（record モード）補足")
    args = parser.parse_args()

    if args.check:
        sys.exit(check_mode())
    if args.record:
        if args.estimated <= 0 or args.actual <= 0:
            print("ERROR: --estimated と --actual の両方必須", file=sys.stderr)
            sys.exit(1)
        sys.exit(record_mode(args.estimated, args.actual, args.task, args.note))
    if args.summary:
        sys.exit(summary_mode())

    parser.print_help()


if __name__ == "__main__":
    main()
