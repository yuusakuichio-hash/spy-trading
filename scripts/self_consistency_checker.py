#!/usr/bin/env python3
"""
self_consistency_checker.py
Self-Consistency (Wang Google 2022) minimal implementation

Critical claim 時に同質問を 3 sub-agent 独立実行→ 2/3 以上一致 OK、不一致は log
2026-04-21 導入
"""
from __future__ import annotations
import json
import pathlib
import datetime as _dt
import hashlib
import sys

LOG_DIR = pathlib.Path("/Users/yuusakuichio/trading/data/logs")
CONSISTENCY_LOG = LOG_DIR / "self_consistency.jsonl"


def record_claim_for_consistency_check(
    claim_id: str,
    claim_text: str,
    source: str,
) -> dict:
    """Record a critical claim that needs self-consistency verification.

    Actual 3-agent dispatch is separate infrastructure;
    this is the claim registry + result storage.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).isoformat()
    entry = {
        "ts": ts,
        "claim_id": claim_id,
        "claim_hash": hashlib.sha256(claim_text.encode()).hexdigest()[:16],
        "claim_text": claim_text[:500],
        "source": source,
        "status": "pending_verification",
    }
    with CONSISTENCY_LOG.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def record_verification_result(
    claim_id: str,
    agent_responses: list[str],
    threshold: float = 2 / 3,
) -> dict:
    """Record verification result from N agent runs.

    agent_responses: list of text responses from independent agent runs
    threshold: minimum agreement ratio (default 2/3 = Wang 2022)
    """
    from collections import Counter

    # normalize responses (lowercase, strip) for comparison
    normalized = [r.lower().strip()[:200] for r in agent_responses]
    counter = Counter(normalized)
    most_common, count = counter.most_common(1)[0]
    agreement_ratio = count / len(agent_responses)
    passed = agreement_ratio >= threshold

    ts = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).isoformat()
    result = {
        "ts": ts,
        "claim_id": claim_id,
        "agent_count": len(agent_responses),
        "majority_count": count,
        "agreement_ratio": round(agreement_ratio, 3),
        "threshold": threshold,
        "passed": passed,
        "disagreement_responses": [
            r[:200] for r in normalized if r != most_common
        ][:3]
        if not passed
        else [],
        "status": "verified" if passed else "inconsistent",
    }
    with CONSISTENCY_LOG.open("a") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        # smoke test
        r1 = record_claim_for_consistency_check(
            "test001", "Atlas monthly rate is 8.89%", "user_query"
        )
        print(json.dumps(r1, ensure_ascii=False, indent=2))

        r2 = record_verification_result(
            "test001",
            ["8.89%", "8.89%", "9.0%"],
        )
        print(json.dumps(r2, ensure_ascii=False, indent=2))
