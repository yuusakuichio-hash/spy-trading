#!/usr/bin/env python3
"""Deviation Scanner — Challenger O-ring教訓適用

condor.log から WARNING/ERROR/ALERT を集計し、同じパターンが常態化していないかを検知。
Normalization of Deviance（Diane Vaughan 1996）対策。

使い方:
    python3 scripts/deviation_scanner.py [--days N] [--threshold N]

出力:
    - stdout: 頻度×期間マトリクス
    - data/deviation_dashboard.md: Markdown形式ダッシュボード
    - exit code: 0 (正常) / 1 (常態化検知)
"""
from __future__ import annotations
import argparse
import collections
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
LOG_DIR = BASE / "data" / "logs"
DASHBOARD = BASE / "data" / "deviation_dashboard.md"

# 逸脱パターン定義（正規表現 + カテゴリ）
PATTERNS = [
    (r"strike不整合", "strike_mismatch"),
    (r"チェーン取得失敗", "chain_fetch_fail"),
    (r"エグジット確認例外", "exit_verify_exception"),
    (r"決済確認中", "settle_pending"),
    (r"Quote context\s*.*?切断", "quote_context_disconnect"),
    (r"gamma_early_exit", "gamma_early_exit"),
    (r"エントリー中止", "entry_aborted"),
    (r"ロード失敗", "module_load_fail"),
    (r"Not enough positions", "no_positions"),
    (r"Insufficient buying power", "insufficient_margin"),
    (r"POSITIONS STILL OPEN", "close_incomplete"),
    (r"ORB.*?→ SPY固定適用", "orb_spy_override"),
    (r"\[CRITICAL\]|FATAL", "critical"),
    (r"Bid-Ask.*?過大", "spread_too_wide"),
    (r"SMA20.*?取得失敗|IVR.*?取得失敗|VRP.*?取得失敗", "indicator_fail"),
]


def parse_log_lines(days: int = 7):
    """指定日数の log から逸脱イベント抽出"""
    cutoff = datetime.now() - timedelta(days=days)
    events: list[tuple[datetime, str, str]] = []  # (ts, category, raw)
    for logf in sorted(LOG_DIR.glob("*.log")):
        try:
            with open(logf, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.match(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", line)
                    if not m:
                        continue
                    try:
                        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    for pat, cat in PATTERNS:
                        if re.search(pat, line):
                            events.append((ts, cat, line.strip()[:200]))
                            break
        except Exception as e:
            print(f"[warn] {logf}: {e}", file=sys.stderr)
    return events


def analyze(events, threshold: int = 10):
    """頻度×期間マトリクス生成 + 3段階判定（急増/当日累積/常態化）

    Sora Labの改善ペース（1日で複数commit）に合わせた階層化:
    - surge (急増): 1時間で10件以上 → 即日対応
    - daily (当日累積): 24時間で30件以上 → 翌朝AAR強調
    - normalized (常態化): 2日連続 → 週次レビュー
    """
    import datetime as _dt
    now = _dt.datetime.now()

    by_cat = collections.defaultdict(list)
    for ts, cat, raw in events:
        by_cat[cat].append((ts, raw))

    report = []
    normalized = []
    surging = []      # 1時間急増
    daily_high = []   # 当日累積高
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        evs = by_cat[cat]
        cnt = len(evs)
        if cnt == 0:
            continue
        first = evs[0][0]
        last = evs[-1][0]
        span_hours = max(1, (last - first).total_seconds() / 3600)
        rate = cnt / span_hours
        days_span = (last - first).days

        # 3段階判定
        last_1h_cutoff = now - _dt.timedelta(hours=1)
        last_24h_cutoff = now - _dt.timedelta(hours=24)
        count_1h = sum(1 for ts, _ in evs if ts >= last_1h_cutoff)
        count_24h = sum(1 for ts, _ in evs if ts >= last_24h_cutoff)

        is_surging = count_1h >= 10            # 急増（即日対応）
        is_daily_high = count_24h >= 30        # 当日累積高（AAR強調）
        is_normalized = cnt >= threshold and days_span >= 2  # 常態化（週次）

        if is_surging:
            surging.append(cat)
        if is_daily_high:
            daily_high.append(cat)
        if is_normalized:
            normalized.append(cat)

        report.append({
            "category": cat,
            "count": cnt,
            "count_1h": count_1h,
            "count_24h": count_24h,
            "first": first,
            "last": last,
            "span_hours": span_hours,
            "rate_per_hour": rate,
            "days_span": days_span,
            "surging": is_surging,
            "daily_high": is_daily_high,
            "normalized": is_normalized,
            "sample": evs[0][1],
        })
    return report, normalized, surging, daily_high


def render_dashboard(report, normalized, days: int, surging=None, daily_high=None):
    if surging is None:
        surging = []
    if daily_high is None:
        daily_high = []
    return _render_dashboard_impl(report, normalized, days, surging, daily_high)


def _render_dashboard_impl(report, normalized, days, surging, daily_high):
    now = datetime.now().strftime("%Y-%m-%d %H:%M JST")
    lines = [
        "# Atlas Deviation Dashboard",
        "",
        f"**生成日時**: {now}  ",
        f"**対象期間**: 直近{days}日  ",
        f"**🚨 急増検知（1h ≥10件・即日対応）**: {len(surging)}件  ",
        f"**🟠 当日累積（24h ≥30件・AAR強調）**: {len(daily_high)}件  ",
        f"**🔴 常態化検知（2日連続・週次レビュー）**: {len(normalized)}件  ",
        "",
        "## 理論的背景",
        "",
        "Challenger O-ring事故（1986）の教訓: 同じ異常が繰り返されても「今までも大丈夫だったから」"
        "と正常扱いされる現象（Normalization of Deviance・Diane Vaughan 1996）。",
        "",
        "**Sora Labの改善ペースに合わせ3段階で検知**（急増/当日/常態化）。",
        "",
        "## 🚨 急増検知（1時間で10件超・即日対応必須）",
        "",
    ]
    if surging:
        lines.append("| カテゴリ | 直近1h件数 | 直近24h件数 | rate/h |")
        lines.append("|---|---:|---:|---:|")
        for r in report:
            if r.get("surging"):
                lines.append(
                    f"| `{r['category']}` | **{r['count_1h']}** | "
                    f"{r['count_24h']} | {r['rate_per_hour']:.1f} |"
                )
    else:
        lines.append("_急増検知なし_")
    lines.extend([
        "",
        "## 🟠 当日累積（24hで30件超・翌AARで強調）",
        "",
    ])
    if daily_high:
        lines.append("| カテゴリ | 直近24h件数 | 累計 |")
        lines.append("|---|---:|---:|")
        for r in report:
            if r.get("daily_high") and not r.get("surging"):
                lines.append(f"| `{r['category']}` | {r['count_24h']} | {r['count']} |")
    else:
        lines.append("_当日累積高なし_")
    lines.extend([
        "",
        "## 🔴 常態化検知（2日連続・要中期対処）",
        "",
    ])
    if normalized:
        lines.append("| カテゴリ | 件数 | 期間 | rate/h |")
        lines.append("|---|---:|---|---:|")
        for r in report:
            if r["normalized"]:
                lines.append(
                    f"| `{r['category']}` | {r['count']} | "
                    f"{r['days_span']}日 | {r['rate_per_hour']:.1f} |"
                )
    else:
        lines.append("_常態化検知なし_")
    lines.extend([
        "",
        "## 全逸脱カテゴリ（頻度降順）",
        "",
        "| カテゴリ | 件数 | 初回 | 最終 | rate/h | 常態化 |",
        "|---|---:|---|---|---:|---|",
    ])
    for r in report:
        flag = "🔴" if r["normalized"] else "🟢"
        lines.append(
            f"| `{r['category']}` | {r['count']} | "
            f"{r['first'].strftime('%m/%d %H:%M')} | "
            f"{r['last'].strftime('%m/%d %H:%M')} | "
            f"{r['rate_per_hour']:.1f} | {flag} |"
        )
    lines.extend([
        "",
        "## サンプルログ",
        "",
    ])
    for r in report[:5]:
        lines.append(f"### `{r['category']}`")
        lines.append("```")
        lines.append(r["sample"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--threshold", type=int, default=10)
    ap.add_argument("--dashboard", action="store_true", help="dashboard生成")
    args = ap.parse_args()

    events = parse_log_lines(args.days)
    report, normalized, surging, daily_high = analyze(events, args.threshold)

    # stdout
    print(f"[deviation_scanner] 直近{args.days}日・{len(events)}件の逸脱イベント")
    print(f"[deviation_scanner] 🚨急増: {len(surging)} / 🟠当日: {len(daily_high)} / 🔴常態化: {len(normalized)}")
    for cat in normalized:
        r = next(x for x in report if x["category"] == cat)
        print(f"  🔴 {cat}: {r['count']}件 / {r['days_span']}日 / {r['rate_per_hour']:.1f}回/h")

    # dashboard
    if args.dashboard:
        DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
        with open(DASHBOARD, "w", encoding="utf-8") as f:
            f.write(render_dashboard(report, normalized, args.days, surging, daily_high))
        print(f"[deviation_scanner] dashboard → {DASHBOARD}")

    # exit code: 急増 or 常態化 どちらか発生でnon-zero
    sys.exit(1 if (surging or normalized) else 0)


if __name__ == "__main__":
    main()
