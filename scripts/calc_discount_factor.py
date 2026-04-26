#!/usr/bin/env python3
"""割引率フィードバック計算スクリプト

data/discount_factor_log.jsonl を読み込み、
戦術別・全体の実測割引率（actual_pnl / bt_expected_pnl の中央値）を算出する。

10件 / 50件 / 100件 到達マイルストーンで Pushover 通知を送信する。

使い方:
    python3 scripts/calc_discount_factor.py          # 全件集計
    python3 scripts/calc_discount_factor.py --json   # JSON出力
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse, request

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
DISCOUNT_LOG_FILE = DATA / "discount_factor_log.jsonl"

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER",  "u2cevk8nktib3sr148rw2hs78ecvux")

# 通知トリガーのマイルストーン件数
MILESTONES = (10, 50, 100)

# 上方修正通知閾値（割引率 >= 0.55 → 月利上方修正余地）
THRESHOLD_UPPER = 0.55
# 下方修正警告閾値（割引率 <= 0.35 → 月利下方修正警告）
THRESHOLD_LOWER = 0.35


def send_pushover(title: str, message: str, priority: int = 0) -> bool:
    """Pushover 送信。失敗しても例外を上げない。"""
    try:
        payload = parse.urlencode({
            "token":    PUSHOVER_TOKEN,
            "user":     PUSHOVER_USER,
            "title":    title,
            "message":  message,
            "priority": priority,
        }).encode()
        req = request.Request(
            "https://api.pushover.net/1/messages.json",
            data=payload,
            method="POST",
        )
        with request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[Pushover] 送信失敗: {e}")
        return False


def load_records() -> list[dict]:
    """discount_factor_log.jsonl を全件読み込む。"""
    if not DISCOUNT_LOG_FILE.exists():
        return []
    rows = []
    with DISCOUNT_LOG_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def calc_stats(rows: list[dict]) -> dict:
    """全体・戦術別の割引率統計を返す。

    Returns:
        {
            "total_count": int,
            "overall": {"median": float, "mean": float, "min": float, "max": float},
            "by_strategy": {
                "<tactic>": {"count": int, "median": float, "mean": float}
            }
        }
    """
    if not rows:
        return {"total_count": 0, "overall": None, "by_strategy": {}}

    ratios = [r["ratio"] for r in rows if r.get("ratio") is not None]
    by_strategy: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("ratio") is not None and r.get("strategy"):
            by_strategy[r["strategy"]].append(r["ratio"])

    def _stats(vals: list[float]) -> dict:
        if not vals:
            return {}
        return {
            "median": round(statistics.median(vals), 4),
            "mean":   round(statistics.mean(vals),   4),
            "min":    round(min(vals), 4),
            "max":    round(max(vals), 4),
            "count":  len(vals),
        }

    return {
        "total_count": len(ratios),
        "overall": _stats(ratios),
        "by_strategy": {k: _stats(v) for k, v in by_strategy.items()},
    }


def check_milestones(total: int, overall_median: float) -> None:
    """マイルストーン到達時と閾値超過時に Pushover 通知。"""
    # マイルストーン通知
    if total in MILESTONES:
        _notify_milestone(total, overall_median)

    # 閾値通知（マイルストーン未到達でも発火）
    if overall_median >= THRESHOLD_UPPER:
        send_pushover(
            "[Atlas] 割引率: 月利上方修正余地",
            f"実測割引率 {overall_median:.2f} >= {THRESHOLD_UPPER}\n"
            f"サンプル数: {total}件\n"
            f"バックテスト中央シナリオ8% → 実績補正後 {8 * overall_median / 0.50:.1f}% 見込み\n"
            f"月利シナリオ見直しを検討してください。",
            priority=0,
        )
    elif overall_median <= THRESHOLD_LOWER:
        send_pushover(
            "[Atlas] 割引率: 月利下方修正警告",
            f"実測割引率 {overall_median:.2f} <= {THRESHOLD_LOWER}\n"
            f"サンプル数: {total}件\n"
            f"バックテスト中央シナリオ8% → 実績補正後 {8 * overall_median / 0.50:.1f}% 見込み\n"
            f"戦術パラメータの見直しを推奨します。",
            priority=1,
        )


def _notify_milestone(total: int, median: float) -> None:
    """マイルストーン件数到達時の Pushover 通知。"""
    corrected_mid = round(8 * median / 0.50, 1)
    msg = (
        f"割引率マイルストーン {total}件到達\n"
        f"実測割引率（中央値）: {median:.4f}\n"
        f"基準割引率（仮定）: 0.50\n"
        f"補正後中央月利: {corrected_mid}%（バックテスト8%ベース）\n"
        f"{'上方修正余地あり' if median >= THRESHOLD_UPPER else '下方修正警告' if median <= THRESHOLD_LOWER else '仮定値と概ね整合'}"
    )
    send_pushover("[Atlas] 割引率フィードバック", msg, priority=0)


def print_report(stats: dict, output_json: bool = False) -> None:
    """集計結果を標準出力へ。"""
    if output_json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    total = stats["total_count"]
    print(f"=== 割引率フィードバック集計 ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}) ===")
    print(f"総サンプル数: {total}件")

    if total == 0:
        print("記録なし。bt_expected_pnl 付きの exit ログが溜まるのを待ちます。")
        return

    ov = stats["overall"]
    print(f"\n[全体]")
    print(f"  中央値: {ov['median']:.4f}  平均: {ov['mean']:.4f}  "
          f"最小: {ov['min']:.4f}  最大: {ov['max']:.4f}")

    if total >= 10:
        corrected = round(8 * ov['median'] / 0.50, 1)
        print(f"  → 補正後中央月利（バックテスト8%ベース）: {corrected}%")

    print(f"\n[戦術別]")
    for tactic, s in sorted(stats["by_strategy"].items()):
        print(f"  {tactic:<20} n={s['count']:>3}  中央値={s['median']:.4f}  平均={s['mean']:.4f}")

    next_ms = next((m for m in MILESTONES if m > total), None)
    if next_ms:
        print(f"\n次マイルストーン: {next_ms}件（あと{next_ms - total}件）")
    else:
        print(f"\n全マイルストーン到達済み")


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas 割引率フィードバック計算")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    parser.add_argument("--notify", action="store_true",
                        help="マイルストーン・閾値チェックを行い Pushover 通知")
    args = parser.parse_args()

    rows = load_records()
    stats = calc_stats(rows)
    print_report(stats, output_json=args.json)

    if args.notify and stats["total_count"] > 0 and stats["overall"]:
        check_milestones(stats["total_count"], stats["overall"]["median"])


if __name__ == "__main__":
    main()
