"""
immutable_ledger.py — Append-only hash-chained evidence ledger
with OpenTimestamps (Bitcoin) batch submission.

Usage:
    from scripts.immutable_ledger import Ledger
    ledger = Ledger()
    ledger.append("trade", {"symbol": "SPY", "action": "sell_to_open", ...})
"""

import hashlib
import json
import os
import sys
import time
import datetime
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
LEDGER_DIR = ROOT / "data" / "evidence_ledger"
OTS_DIR = LEDGER_DIR / "ots_stamps"
LEDGER_DIR.mkdir(parents=True, exist_ok=True)
OTS_DIR.mkdir(parents=True, exist_ok=True)

GENESIS_HASH = "0" * 64  # sentinel for first entry


# -----------------------------------------------------------------------
# Core helpers
# -----------------------------------------------------------------------

def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _content_hash(event_type: str, payload: Any, ts: str) -> str:
    canonical = json.dumps(
        {"event_type": event_type, "payload": payload, "ts": ts},
        sort_keys=True, ensure_ascii=False
    )
    return _sha256(canonical)


def _ledger_path(date: str | None = None) -> Path:
    if date is None:
        date = datetime.datetime.utcnow().strftime("%Y%m%d")
    return LEDGER_DIR / f"{date}.jsonl"


def _last_hash(path: Path) -> str:
    """Return the this_hash of the last line, or GENESIS_HASH if empty."""
    if not path.exists() or path.stat().st_size == 0:
        return GENESIS_HASH
    last_line = ""
    with open(path, "rb") as f:
        # Seek from end to find last non-empty line efficiently
        f.seek(0, 2)
        size = f.tell()
        buf = b""
        pos = size - 1
        while pos >= 0:
            f.seek(pos)
            ch = f.read(1)
            if ch == b"\n" and buf.strip():
                break
            buf = ch + buf
            pos -= 1
        last_line = buf.decode("utf-8").strip()
    if not last_line:
        return GENESIS_HASH
    try:
        rec = json.loads(last_line)
        return rec["this_hash"]
    except Exception:
        return GENESIS_HASH


# -----------------------------------------------------------------------
# Ledger class
# -----------------------------------------------------------------------

class Ledger:
    def __init__(self, date: str | None = None):
        self._path = _ledger_path(date)

    def append(self, event_type: str, payload: Any) -> dict:
        """
        Append one record to the ledger.
        Returns the full record dict.
        """
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        content_hash = _content_hash(event_type, payload, ts)
        prev_hash = _last_hash(self._path)
        this_hash = _sha256(prev_hash + content_hash)

        record = {
            "ts": ts,
            "event_type": event_type,
            "payload": payload,
            "content_hash": content_hash,
            "prev_hash": prev_hash,
            "this_hash": this_hash,
            "ots_submitted": False,
            "ots_stamp_file": None,
        }

        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return record

    def path(self) -> Path:
        return self._path


# -----------------------------------------------------------------------
# Chain verification
# -----------------------------------------------------------------------

def verify_chain(path: Path) -> tuple[bool, list[str]]:
    """
    Verify hash chain integrity for a given JSONL ledger file.
    Returns (ok: bool, errors: list[str]).
    """
    errors = []
    prev_hash = GENESIS_HASH
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {lineno}: JSON parse error — {e}")
                continue

            # Recompute content_hash
            expected_content = _content_hash(
                rec["event_type"], rec["payload"], rec["ts"]
            )
            if expected_content != rec["content_hash"]:
                errors.append(
                    f"line {lineno}: content_hash mismatch "
                    f"(expected {expected_content[:16]}…, "
                    f"got {rec['content_hash'][:16]}…)"
                )

            # Recompute this_hash
            expected_this = _sha256(prev_hash + rec["content_hash"])
            if expected_this != rec["this_hash"]:
                errors.append(
                    f"line {lineno}: chain break — "
                    f"prev_hash {prev_hash[:16]}…, "
                    f"content_hash {rec['content_hash'][:16]}…"
                )

            # Advance
            prev_hash = rec["this_hash"]

    ok = len(errors) == 0
    return ok, errors


# -----------------------------------------------------------------------
# OpenTimestamps batch submit
# -----------------------------------------------------------------------

def _ots_stamp_data(data: bytes) -> bytes | None:
    """
    Submit data's SHA256 digest to an OTS calendar.
    Returns the .ots serialized bytes, or None on failure.

    API: RemoteCalendar.submit(digest_bytes) -> Timestamp
    """
    try:
        import io
        from opentimestamps.calendar import RemoteCalendar
        from opentimestamps.core.serialize import StreamSerializationContext

        digest = hashlib.sha256(data).digest()

        calendars = [
            "https://alice.btc.calendar.opentimestamps.org",
            "https://bob.btc.calendar.opentimestamps.org",
            "https://finney.calendar.eternitywall.com",
        ]

        ts = None
        for cal_url in calendars:
            try:
                cal = RemoteCalendar(cal_url)
                ts = cal.submit(digest, timeout=15)
                break
            except Exception:
                continue

        if ts is None:
            return None

        buf = io.BytesIO()
        ctx = StreamSerializationContext(buf)
        ts.serialize(ctx)
        return buf.getvalue()

    except Exception as e:
        print(f"[OTS] stamp error: {e}", file=sys.stderr)
        return None


def batch_submit_ots(date: str | None = None) -> dict:
    """
    Collect all this_hash values from today's ledger,
    build a combined digest, submit to OTS, save stamp file.
    Returns status dict.
    """
    if date is None:
        date = datetime.datetime.utcnow().strftime("%Y%m%d")

    path = _ledger_path(date)
    if not path.exists():
        return {"ok": False, "reason": "ledger not found", "date": date}

    hashes = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                hashes.append(rec["this_hash"])
            except Exception:
                continue

    if not hashes:
        return {"ok": False, "reason": "no records", "date": date}

    # Combine all hashes into a single digest
    combined = "\n".join(hashes)
    combined_hash = _sha256(combined)
    combined_bytes = combined_hash.encode("utf-8")

    stamp_path = OTS_DIR / f"{date}_batch.ots"
    ots_bytes = _ots_stamp_data(combined_bytes)

    if ots_bytes is None:
        # Fallback: save the combined hash as proof of what was submitted
        fallback_path = OTS_DIR / f"{date}_batch_hash.txt"
        fallback_path.write_text(
            f"combined_hash: {combined_hash}\n"
            f"record_count: {len(hashes)}\n"
            f"submitted_at: {datetime.datetime.utcnow().isoformat()}Z\n"
            f"note: OTS calendar unreachable — hash preserved for later retry\n"
        )
        return {
            "ok": False,
            "reason": "OTS calendar unreachable",
            "fallback_hash_saved": str(fallback_path),
            "combined_hash": combined_hash,
            "record_count": len(hashes),
            "date": date,
        }

    stamp_path.write_bytes(ots_bytes)
    return {
        "ok": True,
        "stamp_file": str(stamp_path),
        "combined_hash": combined_hash,
        "record_count": len(hashes),
        "date": date,
    }


# -----------------------------------------------------------------------
# Snapshot past eval/daily JSON files
# -----------------------------------------------------------------------

def snapshot_past_evals() -> list[dict]:
    """
    Record SHA256 snapshots of existing data/eval/daily/*.json files
    into today's ledger so retroactive edits are detectable.
    """
    eval_dir = ROOT / "data" / "eval" / "daily"
    ledger = Ledger()
    results = []

    for json_path in sorted(eval_dir.glob("*.json")):
        raw = json_path.read_bytes()
        file_hash = hashlib.sha256(raw).hexdigest()
        size = len(raw)
        payload = {
            "file": str(json_path.relative_to(ROOT)),
            "sha256": file_hash,
            "size_bytes": size,
            "snapshot_reason": "initial_immutable_snapshot_20260421",
        }
        rec = ledger.append("eval_snapshot", payload)
        results.append({
            "file": payload["file"],
            "sha256": file_hash[:16] + "…",
            "this_hash": rec["this_hash"][:16] + "…",
        })

    return results


# -----------------------------------------------------------------------
# CLI entry
# -----------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Immutable Evidence Ledger")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("snapshot", help="Snapshot past eval/daily JSON files")
    verify_p = sub.add_parser("verify", help="Verify chain integrity")
    verify_p.add_argument("--date", default=None)
    ots_p = sub.add_parser("ots", help="Batch submit today's ledger to OTS")
    ots_p.add_argument("--date", default=None)
    append_p = sub.add_parser("append", help="Append a test event")
    append_p.add_argument("--type", default="test")
    append_p.add_argument("--msg", default="smoke test")

    args = parser.parse_args()

    if args.cmd == "snapshot":
        results = snapshot_past_evals()
        print(f"Snapshotted {len(results)} eval files:")
        for r in results:
            print(f"  {r['file']}  sha256={r['sha256']}  chain={r['this_hash']}")

    elif args.cmd == "verify":
        date = args.date or datetime.datetime.utcnow().strftime("%Y%m%d")
        path = _ledger_path(date)
        if not path.exists():
            print(f"No ledger for {date}")
            sys.exit(1)
        ok, errors = verify_chain(path)
        if ok:
            print(f"Chain OK — {path}")
        else:
            print(f"Chain BROKEN — {len(errors)} error(s):")
            for e in errors:
                print(f"  {e}")
            sys.exit(1)

    elif args.cmd == "ots":
        result = batch_submit_ots(args.date)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.cmd == "append":
        ledger = Ledger()
        rec = ledger.append(args.type, {"msg": args.msg})
        print(json.dumps(rec, indent=2, ensure_ascii=False))

    else:
        parser.print_help()
