#!/usr/bin/env python3
"""condor_pnl.json の重複 exit レコードをマージする one-shot クリーンアップ。

発動背景（2026-04-17）:
    SPXW_260417 の gamma_early_exit イベントが 10 秒おきに 23 件（=23 tick 分）
    誤って記録されていた。pnl_usd / legs / entry_credit がすべて同一である
    ため、物理的には 1 回の手仕舞いを 23 回カウントしてしまっている。
    最初の tick のみ残し、以降を除去する。

安全装置:
    1. 事前に .bak_<ts> 形式でバックアップを取る。
    2. 同一キー (spread_key, reason, pnl_usd, legs, entry_credit) のうち
       最も早い ts のみを残す。
    3. 実行前に --dry-run で差分表示。
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("/Users/yuusakuichio/trading/data/condor_pnl.json")


def dedup_key(trade: dict[str, Any]) -> tuple:
    """重複判定用のキー。exit イベントのみを対象とする。"""
    if trade.get("event") != "exit":
        return ("__keep__", id(trade))  # exit 以外は全保持
    return (
        "exit",
        trade.get("spread_key"),
        trade.get("reason"),
        trade.get("pnl_usd"),
        tuple(trade.get("legs") or []),
        trade.get("entry_credit"),
        trade.get("date"),
    )


def clean(path: Path, dry_run: bool = False) -> None:
    data = json.loads(path.read_text())
    trades = data.get("trades", [])

    seen: dict[tuple, dict] = {}
    removed: list[dict] = []
    kept_order: list[dict] = []
    for t in trades:
        k = dedup_key(t)
        if k[0] == "__keep__":
            kept_order.append(t)
            continue
        prev = seen.get(k)
        if prev is None:
            seen[k] = t
            kept_order.append(t)
        else:
            # 最初に見つかった ts（=最古）を優先して残す
            if t.get("ts", "") < prev.get("ts", ""):
                # より古いものが後から来たら入れ替え
                idx = kept_order.index(prev)
                kept_order[idx] = t
                removed.append(prev)
                seen[k] = t
            else:
                removed.append(t)

    print(f"[condor_pnl cleanup] total={len(trades)} kept={len(kept_order)} removed={len(removed)}")
    if removed:
        # 除去されたものの内訳
        from collections import Counter
        by_key = Counter(
            (r.get("spread_key"), r.get("reason")) for r in removed
        )
        for (sk, rsn), n in by_key.most_common():
            print(f"  removed dup: spread_key={sk} reason={rsn} count={n}")

    if dry_run:
        print("[dry-run] no file mutation")
        return

    # backup
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak_{ts}_dedup")
    shutil.copy2(path, backup)
    print(f"[backup] {backup}")

    data["trades"] = kept_order
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"[write] {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    clean(args.path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
