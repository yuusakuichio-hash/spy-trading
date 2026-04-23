"""
verify_ledger.py — Verify hash-chain integrity + OTS stamp status.

Usage:
    python3 scripts/verify_ledger.py [--date YYYYMMDD] [--all] [--ots]
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.immutable_ledger import (
    LEDGER_DIR,
    OTS_DIR,
    verify_chain,
    _ledger_path,
    batch_submit_ots,
)


def verify_date(date: str, check_ots: bool = False) -> dict:
    path = _ledger_path(date)
    result = {"date": date, "chain_ok": False, "record_count": 0, "errors": []}

    if not path.exists():
        result["errors"].append("ledger file not found")
        return result

    # Count records
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    result["record_count"] = count

    ok, errors = verify_chain(path)
    result["chain_ok"] = ok
    result["errors"] = errors

    # OTS stamp check
    stamp_path = OTS_DIR / f"{date}_batch.ots"
    hash_path = OTS_DIR / f"{date}_batch_hash.txt"
    if stamp_path.exists():
        result["ots_stamp"] = str(stamp_path)
        result["ots_status"] = "STAMPED"
    elif hash_path.exists():
        result["ots_stamp"] = str(hash_path)
        result["ots_status"] = "HASH_ONLY (calendar was unreachable)"
    else:
        result["ots_status"] = "NOT_SUBMITTED"

    if check_ots and result["ots_status"] == "NOT_SUBMITTED" and count > 0:
        print(f"  Submitting OTS batch for {date}…")
        ots_result = batch_submit_ots(date)
        result["ots_submit_result"] = ots_result

    return result


def main():
    parser = argparse.ArgumentParser(description="Ledger Verifier")
    parser.add_argument("--date", default=None, help="YYYYMMDD (default: today)")
    parser.add_argument("--all", action="store_true", help="Verify all ledger files")
    parser.add_argument("--ots", action="store_true", help="Submit missing OTS stamps")
    args = parser.parse_args()

    if args.all:
        dates = sorted(
            p.stem for p in LEDGER_DIR.glob("*.jsonl")
        )
    else:
        date = args.date or datetime.datetime.utcnow().strftime("%Y%m%d")
        dates = [date]

    all_ok = True
    for date in dates:
        result = verify_date(date, check_ots=args.ots)
        status = "OK" if result["chain_ok"] else "FAIL"
        ots = result.get("ots_status", "N/A")
        print(
            f"[{date}] chain={status} records={result['record_count']} "
            f"ots={ots}"
        )
        if result["errors"]:
            for e in result["errors"]:
                print(f"    ERROR: {e}")
            all_ok = False

    if not all_ok:
        print("\nCHAIN INTEGRITY FAILURE — alert required")
        sys.exit(1)
    else:
        print("\nAll chains intact.")


if __name__ == "__main__":
    main()
