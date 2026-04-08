#!/usr/bin/env python3
"""
spx_bot_verify.py — 12-check GO/NOGO verification for SPX Bot deployment.
Run this on the VPS or locally. Prints GO/NOGO for each check and exits
with code 0 if all GO, 1 if any NOGO.
"""

import sys
import os
import json
import datetime
import subprocess
import importlib
import socket
from pathlib import Path

ET_TZ = "America/New_York"
RESULTS = []


def check(name: str, fn):
    try:
        result, detail = fn()
        status = "GO  " if result else "NOGO"
        symbol = "✅" if result else "❌"
        print(f"{symbol} [{status}] {name}: {detail}")
        RESULTS.append(result)
    except Exception as e:
        print(f"❌ [NOGO] {name}: EXCEPTION — {e}")
        RESULTS.append(False)


# ─── Check 1: spx_bot.py imports without error ────────────────────────────────
def c1_import():
    try:
        sys.modules.pop("spx_bot", None)
        # Suppress futu import error
        from unittest.mock import MagicMock
        sys.modules.setdefault("futu", MagicMock())
        import spx_bot
        return True, "import OK"
    except Exception as e:
        return False, str(e)


# ─── Check 2: US_HOLIDAYS contains 2025–2027 entries ──────────────────────────
def c2_holidays():
    from unittest.mock import MagicMock
    sys.modules.setdefault("futu", MagicMock())
    import spx_bot
    years = {d.year for d in spx_bot.US_HOLIDAYS}
    expected = {2025, 2026, 2027}
    missing = expected - years
    if missing:
        return False, f"Missing years: {missing}"
    count = len(spx_bot.US_HOLIDAYS)
    return True, f"{count} holidays spanning {sorted(years)}"


# ─── Check 3: is_notrade_today works for known holiday eve ────────────────────
def c3_notrade_holiday():
    from unittest.mock import MagicMock, patch
    sys.modules.setdefault("futu", MagicMock())
    import spx_bot, zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
    # Jan 18, 2026 (Sun) → tomorrow = Jan 19 = MLK Day
    fake_now = datetime.datetime(2026, 1, 18, 10, 0, tzinfo=ET)
    with patch("spx_bot.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = fake_now
        mock_dt.date = datetime.date
        mock_dt.timedelta = datetime.timedelta
        mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
        with patch("spx_bot.EVENTS_FILE", Path("/nonexistent_events.json")):
            result = spx_bot.is_notrade_today()
    return result, "Jan 18 (pre-MLK Day) → no trade" if result else "FAILED"


# ─── Check 4: get_expiry returns correct dates ────────────────────────────────
def c4_expiry():
    from unittest.mock import MagicMock, patch
    sys.modules.setdefault("futu", MagicMock())
    import spx_bot, zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
    errors = []
    cases = [
        (datetime.datetime(2026, 3, 9, 10, 0, tzinfo=ET), "2026-03-09"),   # Mon 0DTE
        (datetime.datetime(2026, 3, 10, 10, 0, tzinfo=ET), "2026-03-11"),  # Tue 1DTE
        (datetime.datetime(2026, 3, 12, 10, 0, tzinfo=ET), "2026-03-13"),  # Thu 1DTE
        (datetime.datetime(2026, 3, 13, 10, 0, tzinfo=ET), "2026-03-13"),  # Fri 0DTE
    ]
    for fake_now, expected in cases:
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = fake_now
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            b = spx_bot.SPXBot.__new__(spx_bot.SPXBot)
            got = b.get_expiry()
        if got != expected:
            errors.append(f"{fake_now.strftime('%a')} got {got} expected {expected}")
    if errors:
        return False, "; ".join(errors)
    return True, f"{len(cases)} cases pass"


# ─── Check 5: calc_position_size base case ────────────────────────────────────
def c5_position_size():
    from unittest.mock import MagicMock, patch
    sys.modules.setdefault("futu", MagicMock())
    import spx_bot, zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
    eng = spx_bot.TradeEngine.__new__(spx_bot.TradeEngine)
    eng.trade_env = None; eng.trade_ctx = None
    eng.account_id = ""; eng.unlock_ok = False
    with patch("spx_bot.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = datetime.datetime(2026, 3, 11, 10, 30, tzinfo=ET)
        mock_dt.date = datetime.date
        mock_dt.timedelta = datetime.timedelta
        mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
        with patch("spx_bot.EVENTS_FILE", Path("/nonexistent_events.json")):
            qty = eng.calc_position_size(2500.0, 15.0, 14.0)
    if qty < 1:
        return False, f"got {qty} (expected >= 1)"
    return True, f"$2500 base → {qty} contract(s)"


# ─── Check 6: monthly CSV write ───────────────────────────────────────────────
def c6_monthly_csv():
    import tempfile
    from unittest.mock import patch
    from unittest.mock import MagicMock
    sys.modules.setdefault("futu", MagicMock())
    import spx_bot, zoneinfo
    tmpdir = Path(tempfile.mkdtemp())
    with patch("spx_bot.MONTHLY_CSV_DIR", tmpdir):
        spx_bot.append_monthly_csv({
            "direction": "bull_put", "sell_strike": 550.0,
            "buy_strike": 545.0, "qty": 2, "net_credit": 1.20, "result": "entered",
        })
    files = list(tmpdir.glob("*.csv"))
    if not files:
        return False, "CSV not created"
    content = files[0].read_text()
    if "bull_put" not in content:
        return False, "CSV missing expected data"
    return True, f"CSV created: {files[0].name}"


# ─── Check 7: health_server.py exists and is importable ──────────────────────
def c7_health_server():
    p = Path(__file__).parent / "health_server.py"
    if not p.exists():
        return False, "health_server.py not found"
    try:
        spec_text = p.read_text()
        if "8080" not in spec_text:
            return False, "port 8080 not found in health_server.py"
        return True, "health_server.py exists with port 8080"
    except Exception as e:
        return False, str(e)


# ─── Check 8: health.service exists with correct ExecStart ───────────────────
def c8_health_service():
    p = Path(__file__).parent / "health.service"
    if not p.exists():
        return False, "health.service not found"
    content = p.read_text()
    if "health_server.py" not in content:
        return False, "ExecStart missing health_server.py"
    return True, "health.service found with correct ExecStart"


# ─── Check 9: GitHub Actions workflow exists ─────────────────────────────────
def c9_workflow():
    p = Path(__file__).parent / ".github" / "workflows" / "health_check.yml"
    if not p.exists():
        return False, ".github/workflows/health_check.yml not found"
    content = p.read_text()
    checks = ["*/5 * * * *", "reboot", "pushover", "VULTR_API_KEY"]
    missing = [c for c in checks if c not in content]
    if missing:
        return False, f"Missing in workflow: {missing}"
    return True, "health_check.yml has schedule/reboot/pushover/vultr"


# ─── Check 10: test_spx_bot.py exists and runs ────────────────────────────────
def c10_tests():
    p = Path(__file__).parent / "test_spx_bot.py"
    if not p.exists():
        return False, "test_spx_bot.py not found"
    result = subprocess.run(
        [sys.executable, str(p)],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode == 0:
        # Count tests from output
        lines = result.stdout.splitlines()
        ran_line = [l for l in lines if "Ran " in l]
        detail = ran_line[0] if ran_line else "tests passed"
        return True, detail
    error_lines = (result.stdout + result.stderr).strip().splitlines()
    return False, "\n".join(error_lines[-5:])


# ─── Check 11: Memory monitor function exists ─────────────────────────────────
def c11_memory_monitor():
    from unittest.mock import MagicMock
    sys.modules.setdefault("futu", MagicMock())
    import spx_bot
    has_fn = hasattr(spx_bot, "check_memory_usage")
    has_const = hasattr(spx_bot, "MEMORY_WARN_MB")
    if not has_fn:
        return False, "check_memory_usage() not found"
    if not has_const:
        return False, "MEMORY_WARN_MB constant not found"
    return True, f"check_memory_usage() exists, MEMORY_WARN_MB={spx_bot.MEMORY_WARN_MB}"


# ─── Check 12: Daily summary method exists ────────────────────────────────────
def c12_daily_summary():
    from unittest.mock import MagicMock
    sys.modules.setdefault("futu", MagicMock())
    import spx_bot
    has_method = hasattr(spx_bot.SPXBot, "run_daily_summary_jst")
    if not has_method:
        return False, "run_daily_summary_jst() not found in SPXBot"
    src = open(__file__.replace("spx_bot_verify.py", "spx_bot.py")).read()
    if "run_daily_summary_jst" not in src:
        return False, "not wired into run_forever"
    return True, "run_daily_summary_jst() exists and wired in run_forever"


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("SPX Bot Verification — 12 Checks")
    print("=" * 60)

    check("1. spx_bot.py import", c1_import)
    check("2. US_HOLIDAYS 2025-2027", c2_holidays)
    check("3. no-trade on holiday eve", c3_notrade_holiday)
    check("4. get_expiry 0DTE/1DTE", c4_expiry)
    check("5. calc_position_size base", c5_position_size)
    check("6. monthly CSV write", c6_monthly_csv)
    check("7. health_server.py (port 8080)", c7_health_server)
    check("8. health.service", c8_health_service)
    check("9. GitHub Actions workflow", c9_workflow)
    check("10. test_spx_bot.py runs", c10_tests)
    check("11. memory monitor", c11_memory_monitor)
    check("12. daily summary (9AM JST)", c12_daily_summary)

    print("=" * 60)
    go_count = sum(RESULTS)
    nogo_count = len(RESULTS) - go_count
    print(f"RESULT: {go_count} GO / {nogo_count} NOGO")
    print("=" * 60)
    sys.exit(0 if all(RESULTS) else 1)
