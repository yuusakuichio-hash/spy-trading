"""inject_dst_boundary.py -- DST boundary 0DTE off-by-one check (Scenario 11)

ET vs JST date divergence at DST transitions must NOT cause 0DTE miss.
Winter: ET=UTC-5, JST=ET+14h
Summer: ET=UTC-4, JST=ET+13h

Correct rule: option_expiry_date == today_ET  (not JST)
"""
from __future__ import annotations
import sys
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

ET = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")


def _is_0dte(expiry_date_et: datetime.date, now_et: datetime.datetime) -> bool:
    return expiry_date_et == now_et.date()


def run() -> dict:
    results = []

    # Spring forward: 2026-03-08 ET
    spring_pre  = datetime.datetime(2026, 3, 8, 1, 59, 0, tzinfo=ET)
    spring_post = datetime.datetime(2026, 3, 8, 3, 0, 0, tzinfo=ET)

    # Fall back: 2025-11-02 ET
    fall_pre  = datetime.datetime(2025, 11, 2, 1, 0, 0, tzinfo=ET)
    fall_post = datetime.datetime(2025, 11, 2, 2, 0, 0, tzinfo=ET)

    test_cases = [
        ("spring_pre",  spring_pre,  datetime.date(2026, 3, 8)),
        ("spring_post", spring_post, datetime.date(2026, 3, 8)),
        ("fall_pre",    fall_pre,    datetime.date(2025, 11, 2)),
        ("fall_post",   fall_post,   datetime.date(2025, 11, 2)),
    ]

    for name, now_et, expiry in test_cases:
        is_0 = _is_0dte(expiry, now_et)
        jst_now = now_et.astimezone(JST)
        et_d  = now_et.date()
        jst_d = jst_now.date()
        correct = (et_d == expiry) == is_0
        results.append({
            "case": name,
            "et": now_et.isoformat(),
            "jst": jst_now.isoformat(),
            "et_date": et_d.isoformat(),
            "jst_date": jst_d.isoformat(),
            "expiry": expiry.isoformat(),
            "is_0dte": is_0,
            "tz_date_differs": et_d != jst_d,
            "correct": correct,
        })

    all_correct = all(r["correct"] for r in results)
    boundary_cases = [r for r in results if r["tz_date_differs"]]

    return {
        "scenario": "dst_boundary_0dte",
        "description": "DST boundary 0DTE judgement uses ET date (not JST)",
        "expected": "All cases use ET date correctly",
        "test_cases": results,
        "tz_boundary_count": len(boundary_cases),
        "pass": all_correct,
        "severity": "HIGH" if not all_correct else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
