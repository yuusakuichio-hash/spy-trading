#!/usr/bin/env python3
"""
violation_daily_rollup.py  -- 違反ダッシュボード日次集計 + 自動エスカレーション
Phase C 施策2

毎朝06:00 JST に LaunchAgent から実行される。
集計対象:
  - data/logs/discipline_violations.log
  - data/logs/sns_truth_violations.log
  - data/logs/state_safety_violations.log
  - data/logs/pace_violations.log
  - data/logs/service_recommend_violations.log
  - data/violation_registry.jsonl

エスカレーション条件:
  - 前日比 +50% 以上 → Pushover P1 緊急
  - 単日 100 件超    → Pushover P1 緊急
  - 単日 50 件超     → Pushover P0 通常

出力:
  - data/governance/daily_violations_YYYYMMDD.json
  - data/reports/weekly_violations_YYYYWW.md (日曜日のみ生成)
"""

import glob
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "/Users/yuusakuichio/trading"
LOG_DIR = f"{BASE}/data/logs"
GOVERNANCE_DIR = f"{BASE}/data/governance"
REPORTS_DIR = f"{BASE}/data/reports"
REGISTRY_PATH = f"{BASE}/data/violation_registry.jsonl"
JST = timezone(timedelta(hours=9))

PUSHOVER_USER = "u2cevk8nktib3sr148rw2hs78ecvux"
PUSHOVER_TOKEN_ALERT = "au5xdx7oi91r275gaw6spfoojbqj5z"
PUSHOVER_TOKEN_REPORT = "afv2594jgkc4jvh2vgf7dnnyft1gdi"

LOG_TARGETS = [
    "discipline_violations.log",
    "sns_truth_violations.log",
    "state_safety_violations.log",
    "pace_violations.log",
    "service_recommend_violations.log",
    "peer_review.log",
]


def send_pushover(title: str, message: str, priority: int = 0, token: str = PUSHOVER_TOKEN_ALERT):
    try:
        payload = {
            "token": token,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
            "priority": str(priority),
        }
        if priority >= 2:
            payload["retry"] = "60"
            payload["expire"] = "3600"
        cmd = ["curl", "-s", "--form-string"]
        parts = [f"{k}={v}" for k, v in payload.items()]
        args = ["curl", "-s"]
        for k, v in payload.items():
            args += ["--form-string", f"{k}={v}"]
        args.append("https://api.pushover.net/1/messages.json")
        subprocess.run(args, capture_output=True, timeout=10)
    except Exception as e:
        print(f"[violation_rollup] Pushover error: {e}", file=sys.stderr)


def count_lines_today(log_path: str, today_str: str) -> int:
    """今日の日付文字列 (YYYY-MM-DD / YYYYMMDD / 2026-04-21 等) を含む行数を数える"""
    if not os.path.exists(log_path):
        return 0
    count = 0
    # 複数フォーマット対応
    patterns = [
        today_str,
        today_str.replace("-", ""),
        today_str.replace("-", "/"),
    ]
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(p in line for p in patterns):
                    count += 1
    except Exception:
        pass
    return count


def count_lines_yesterday(log_path: str, yesterday_str: str) -> int:
    return count_lines_today(log_path, yesterday_str)


def count_registry_today(today_str: str) -> int:
    if not os.path.exists(REGISTRY_PATH):
        return 0
    count = 0
    try:
        with open(REGISTRY_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("timestamp", rec.get("ts", ""))
                    if today_str in ts:
                        count += 1
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return count


def analyze_patterns(log_path: str, today_str: str) -> dict:
    """今日の違反ログから頻出パターンを上位5件抽出"""
    if not os.path.exists(log_path):
        return {}
    pattern_counts = defaultdict(int)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if today_str not in line:
                    continue
                # キーワード抽出（簡易）
                kws = re.findall(r"\[([A-Z_]{3,})\]", line)
                for kw in kws:
                    pattern_counts[kw] += 1
    except Exception:
        pass
    return dict(sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True)[:5])


def load_previous_total(yesterday_date: str) -> int:
    """昨日の集計JSONから合計件数を取得"""
    fname = f"{GOVERNANCE_DIR}/daily_violations_{yesterday_date.replace('-', '')}.json"
    if not os.path.exists(fname):
        return 0
    try:
        with open(fname) as f:
            data = json.load(f)
        return data.get("total_today", 0)
    except Exception:
        return 0


def generate_weekly_report(week_str: str, days_data: list):
    """週次レポート Markdown を生成"""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = f"{REPORTS_DIR}/weekly_violations_{week_str}.md"
    lines = [
        f"# 週次違反レポート {week_str}",
        "",
        f"生成日時: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}",
        "",
        "## 日別サマリー",
        "",
        "| 日付 | 合計 | 前日比 | 最多パターン |",
        "|------|------|--------|------------|",
    ]
    for day in days_data:
        date = day.get("date", "")
        total = day.get("total_today", 0)
        delta = day.get("delta_vs_yesterday", 0)
        top_pattern = day.get("top_pattern", "-")
        delta_str = f"+{delta}" if delta >= 0 else str(delta)
        lines.append(f"| {date} | {total} | {delta_str} | {top_pattern} |")
    lines += ["", "## 注記", "", "- 100件超の日は緊急エスカレーション済み", ""]
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def main():
    now = datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")
    today_compact = now.strftime("%Y%m%d")
    yesterday = now - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")

    os.makedirs(GOVERNANCE_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)

    print(f"[violation_rollup] START {now.isoformat()}")

    # 各ログファイルのカウント
    counts = {}
    patterns = {}
    total_today = 0
    for log_name in LOG_TARGETS:
        log_path = f"{LOG_DIR}/{log_name}"
        c = count_lines_today(log_path, today_str)
        counts[log_name] = c
        total_today += c
        if c > 0:
            patterns[log_name] = analyze_patterns(log_path, today_str)

    # registry カウント
    registry_count = count_registry_today(today_str)
    counts["violation_registry.jsonl"] = registry_count
    total_today += registry_count

    # 前日比
    yesterday_total = load_previous_total(yesterday_str)
    delta = total_today - yesterday_total
    delta_ratio = (delta / yesterday_total) if yesterday_total > 0 else float("inf")

    # 最多パターン
    all_patterns = defaultdict(int)
    for p in patterns.values():
        for k, v in p.items():
            all_patterns[k] += v
    top_pattern = max(all_patterns, key=all_patterns.get) if all_patterns else "-"

    result = {
        "date": today_str,
        "timestamp": now.isoformat(),
        "total_today": total_today,
        "yesterday_total": yesterday_total,
        "delta_vs_yesterday": delta,
        "delta_ratio": round(delta_ratio, 2) if delta_ratio != float("inf") else None,
        "counts_by_log": counts,
        "top_patterns": dict(sorted(all_patterns.items(), key=lambda x: x[1], reverse=True)[:5]),
        "top_pattern": top_pattern,
        "escalated": False,
        "escalation_reason": None,
    }

    # エスカレーション判定
    escalated = False
    escalation_reason = None

    if total_today > 100:
        escalated = True
        escalation_reason = f"単日{total_today}件超(閾値100)"
        msg = (
            f"[SYS] 違反件数緊急: {today_str} 合計{total_today}件\n"
            f"前日: {yesterday_total}件 | 増加: +{delta}件\n"
            f"最多パターン: {top_pattern}"
        )
        send_pushover("[SYS] 違反急増 緊急", msg, priority=1)
    elif delta_ratio != float("inf") and delta_ratio >= 0.5 and delta > 10:
        escalated = True
        escalation_reason = f"前日比+{round(delta_ratio*100)}%(閾値+50%)"
        msg = (
            f"[SYS] 違反前日比急増: {today_str}\n"
            f"今日: {total_today}件 / 前日: {yesterday_total}件\n"
            f"増加率: +{round(delta_ratio*100)}% | パターン: {top_pattern}"
        )
        send_pushover("[SYS] 違反前日比急増", msg, priority=0)
    elif total_today > 50:
        escalated = True
        escalation_reason = f"単日{total_today}件超(閾値50)"
        msg = f"[SYS] 違反{total_today}件 ({today_str}) | 最多: {top_pattern}"
        send_pushover("[SYS] 違反件数注意", msg, priority=0)

    result["escalated"] = escalated
    result["escalation_reason"] = escalation_reason

    # JSON保存
    out_path = f"{GOVERNANCE_DIR}/daily_violations_{today_compact}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[violation_rollup] total={total_today} yesterday={yesterday_total} delta={delta:+d}")
    print(f"[violation_rollup] escalated={escalated} reason={escalation_reason}")
    print(f"[violation_rollup] saved: {out_path}")

    # 日曜日なら週次レポート生成
    if now.weekday() == 6:  # Sunday
        week_str = now.strftime("%GW%V")
        # 直近7日分のdaily jsonを読む
        days_data = []
        for i in range(7):
            d = now - timedelta(days=i)
            fname = f"{GOVERNANCE_DIR}/daily_violations_{d.strftime('%Y%m%d')}.json"
            if os.path.exists(fname):
                try:
                    with open(fname) as f:
                        days_data.append(json.load(f))
                except Exception:
                    pass
        days_data.reverse()
        report_path = generate_weekly_report(week_str, days_data)
        print(f"[violation_rollup] weekly report: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
