#!/usr/bin/env python3
"""
scripts/trade_reason_analyzer.py — Trade Entry/Exit Reason Analyzer

Usage:
  python3 scripts/trade_reason_analyzer.py [--bot atlas|chronos|both] [--n 50]
  python3 scripts/trade_reason_analyzer.py --grep "calendar_sell" --bot atlas
  python3 scripts/trade_reason_analyzer.py --engine ORBEngine --date 2026-04-21

Shows:
  - Engine × symbol × reason_text breakdown of recent N trades
  - Constraint bypass summary (paper bypass / dll_ok / etc.)
  - Daily entry count per engine
  - Chronos: strategy_id × symbol × action breakdown
  - Grep: filter by reason_text substring
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

_BASE_DIR = Path(__file__).parent.parent / "data"

ATLAS_REASONS_FILE   = _BASE_DIR / "atlas_trade_reasons.jsonl"
CHRONOS_REASONS_FILE = _BASE_DIR / "chronos_trade_reasons.jsonl"


def load_records(path: Path, n: int) -> List[Dict[str, Any]]:
    if not path.exists():
        print(f"[WARN] file not found: {path}")
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    records = []
    for line in lines[-n:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def grep_records(
    records: List[Dict[str, Any]],
    query: str,
    engine_filter: str = "",
    date_filter: str = "",
) -> List[Dict[str, Any]]:
    """Filter records by reason_text substring, engine name, and/or date prefix."""
    out = []
    query_l = query.lower()
    for r in records:
        ts = r.get("ts", "")
        if date_filter and not ts.startswith(date_filter):
            continue
        if engine_filter and r.get("engine", "") != engine_filter:
            continue
        reason_text = r.get("entry_reason", {}).get("reason_text", "") or \
                      r.get("exit_reason", {}).get("reason_text", "") or ""
        if query_l and query_l not in reason_text.lower() and query_l not in str(r.get("engine", "")).lower():
            continue
        out.append(r)
    return out


def summarize_chronos(records: List[Dict[str, Any]]) -> None:
    """Chronos 固有: strategy_id × symbol × action 集計。"""
    entries = [r for r in records if r.get("event") == "entry"]
    if not entries:
        return

    # strategy_id 別集計
    strategy_counts: Counter = Counter(
        r.get("entry_reason", {}).get("strategy_id", "(none)") for r in entries
    )
    print("\n[Chronos strategy_id breakdown]")
    for sid, cnt in strategy_counts.most_common():
        print(f"  {sid:<40} {cnt:>4} entries")

    # action 別集計
    action_counts: Counter = Counter(r.get("action", "?") for r in entries)
    print("\n[Chronos action breakdown]")
    for act, cnt in action_counts.most_common():
        print(f"  {act:<20} {cnt:>4} entries")

    # firm_constraint_result 集計（TradersPostForwarder 経由）
    firm_results: Counter = Counter(
        r.get("entry_reason", {}).get("firm_constraint_result", "(none)")
        for r in entries
        if r.get("engine") == "TradersPostForwarder"
    )
    if firm_results:
        print("\n[Chronos firm_constraint_result (TradersPostForwarder)]")
        for fr, cnt in firm_results.most_common():
            print(f"  {fr:<20} {cnt:>4}")


def summarize(records: List[Dict[str, Any]], bot: str) -> None:
    entries = [r for r in records if r.get("event") == "entry"]
    exits   = [r for r in records if r.get("event") == "exit"]

    print(f"\n{'='*60}")
    print(f" Bot: {bot.upper()}  |  total={len(records)}  entry={len(entries)}  exit={len(exits)}")
    print(f"{'='*60}")

    if not records:
        print("  (no records)")
        return

    # Engine breakdown
    engine_counts: Counter = Counter(r.get("engine", "?") for r in entries)
    print("\n[Engine breakdown - entries]")
    for eng, cnt in engine_counts.most_common():
        print(f"  {eng:<30} {cnt:>4} entries")

    # Symbol breakdown
    symbol_counts: Counter = Counter(r.get("symbol", "?") for r in entries)
    print("\n[Symbol breakdown - entries]")
    for sym, cnt in symbol_counts.most_common():
        print(f"  {sym:<20} {cnt:>4} entries")

    # Reason text breakdown
    reason_texts: Counter = Counter()
    for r in entries:
        rt = r.get("entry_reason", {}).get("reason_text", "(none)")
        reason_texts[rt] += 1
    print("\n[Entry reason_text (top 10)]")
    for txt, cnt in reason_texts.most_common(10):
        print(f"  [{cnt:>3}] {txt[:80]}")

    # Constraint bypass summary
    bypass_counts: Dict[str, Counter] = defaultdict(Counter)
    for r in entries:
        for k, v in r.get("constraints_checked", {}).items():
            bypass_counts[k][str(v)] += 1
    if bypass_counts:
        print("\n[Constraints checked]")
        for k, vc in sorted(bypass_counts.items()):
            vals = "  ".join(f"{v}:{c}" for v, c in vc.most_common())
            print(f"  {k:<30} {vals}")

    # VIX distribution
    vix_vals = [
        r.get("entry_reason", {}).get("vix")
        for r in entries
        if r.get("entry_reason", {}).get("vix") is not None
    ]
    if vix_vals:
        avg_vix = sum(vix_vals) / len(vix_vals)
        print(f"\n[VIX at entry]  n={len(vix_vals)}  avg={avg_vix:.2f}  "
              f"min={min(vix_vals):.2f}  max={max(vix_vals):.2f}")

    # P&L summary on exits
    pnl_vals = [r.get("pnl") for r in exits if r.get("pnl") is not None]
    if pnl_vals:
        total_pnl = sum(pnl_vals)
        win_count = sum(1 for p in pnl_vals if p > 0)
        print(f"\n[P&L summary]  n={len(pnl_vals)}  total={total_pnl:.2f}  "
              f"win_rate={win_count/len(pnl_vals)*100:.1f}%  "
              f"avg={total_pnl/len(pnl_vals):.2f}")

    # Date breakdown
    date_engine: Dict[str, Counter] = defaultdict(Counter)
    for r in entries:
        ts = r.get("ts", "")
        date = ts[:10] if len(ts) >= 10 else "?"
        date_engine[date][r.get("engine", "?")] += 1
    print("\n[Daily entry count by date × engine]")
    for dt in sorted(date_engine)[-7:]:
        row = "  ".join(f"{e}:{c}" for e, c in date_engine[dt].most_common())
        print(f"  {dt}  {row}")

    if bot == "chronos":
        summarize_chronos(records)

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade reason analyzer")
    parser.add_argument("--bot", choices=["atlas", "chronos", "both"], default="both")
    parser.add_argument("--n", type=int, default=100, help="Recent N records to analyze")
    parser.add_argument("--grep", default="", help="Filter by reason_text or engine substring")
    parser.add_argument("--engine", default="", help="Filter by exact engine name")
    parser.add_argument("--date", default="", help="Filter by date prefix (e.g. 2026-04-21)")
    args = parser.parse_args()

    bots_to_show = ["atlas", "chronos"] if args.bot == "both" else [args.bot]
    path_map = {"atlas": ATLAS_REASONS_FILE, "chronos": CHRONOS_REASONS_FILE}

    for bot in bots_to_show:
        records = load_records(path_map[bot], args.n)
        if args.grep or args.engine or args.date:
            records = grep_records(records, args.grep, args.engine, args.date)
            print(f"\n[GREP filter: query={args.grep!r} engine={args.engine!r} date={args.date!r}]")
            print(f"  matched {len(records)} records")
            for r in records[:20]:
                ts = r.get("ts", "")[:19]
                eng = r.get("engine", "?")
                sym = r.get("symbol", "?")
                act = r.get("action", "?")
                qty = r.get("qty", 0)
                rt = (r.get("entry_reason") or r.get("exit_reason") or {}).get("reason_text", "")
                print(f"  {ts} | {eng:<28} | {sym:<12} | {act:<20} | x{qty} | {rt[:60]}")
        else:
            summarize(records, bot)


if __name__ == "__main__":
    main()
