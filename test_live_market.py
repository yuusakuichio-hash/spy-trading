#!/usr/bin/env python3
"""
Live market test script — runs on VPS during US market hours.
Tests: options chain, account info, SIMULATE trade readiness.
"""
import sys
import os
import datetime
import json

sys.path.insert(0, "/root/spxbot")
os.environ.setdefault("SPX_LOG_DIR", "/var/log/spx_bot")

PASS = []
FAIL = []


def report(name, ok, detail=""):
    sym = "PASS" if ok else "FAIL"
    msg = f"[{sym}] {name}: {detail}"
    print(msg)
    (PASS if ok else FAIL).append(name)


# ── 1. futu import ─────────────────────────────────────────────────────────────
try:
    from futu import (
        OpenQuoteContext, OpenSecTradeContext,
        TrdMarket, TrdEnv, SecurityFirm, RET_OK
    )
    report("futu import", True, "OK")
except ImportError as e:
    report("futu import", False, str(e))
    print(f"\nFATAL: futu-api not available: {e}")
    sys.exit(1)

# ── 2. OpenD quote connection ──────────────────────────────────────────────────
try:
    qctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    report("quote connection", True, "OpenD reachable")
except Exception as e:
    report("quote connection", False, str(e))
    print(f"\nFATAL: Cannot connect to OpenD: {e}")
    sys.exit(1)

# ── 3. SPY price ───────────────────────────────────────────────────────────────
try:
    ret, snap = qctx.get_market_snapshot(["US.SPY"])
    if ret == RET_OK and not snap.empty:
        spy_price = float(snap.iloc[0]["last_price"])
        report("SPY price", True, f"${spy_price:.2f}")
    else:
        report("SPY price", False, str(snap)[:100])
        spy_price = 0.0
except Exception as e:
    report("SPY price", False, str(e))
    spy_price = 0.0

# ── 4. VIX ─────────────────────────────────────────────────────────────────────
try:
    ret, snap = qctx.get_market_snapshot(["US.VIX"])
    if ret == RET_OK and not snap.empty:
        vix = float(snap.iloc[0]["last_price"])
        report("VIX", True, f"{vix:.2f}")
    else:
        report("VIX", False, str(snap)[:100])
        vix = 0.0
except Exception as e:
    report("VIX", False, str(e))
    vix = 0.0

# ── 5. SPX option expiration dates ────────────────────────────────────────────
try:
    ret, exp_data = qctx.get_option_expiration_date(code="US.SPX")
    if ret == RET_OK and not exp_data.empty:
        dates = exp_data["time"].tolist()[:3]
        report("SPX expiry dates", True, str(dates))
    else:
        # fallback: try SPY
        ret2, exp_data2 = qctx.get_option_expiration_date(code="US.SPY")
        if ret2 == RET_OK and not exp_data2.empty:
            dates = exp_data2["time"].tolist()[:3]
            report("SPY expiry dates (fallback)", True, str(dates))
        else:
            report("option expiry dates", False, str(exp_data)[:100])
except Exception as e:
    report("option expiry dates", False, str(e))

# ── 6. SPY option chain (nearest expiry) ──────────────────────────────────────
try:
    import futu as ft
    today = datetime.date.today().strftime("%Y-%m-%d")
    ret, chain = qctx.get_option_chain(
        "US.SPY",
        index_option_type=ft.IndexOptionType.ETF,
        start=today, end=today
    )
    if ret == RET_OK and not chain.empty:
        put_chain = chain[chain["option_type"] == "PUT"]
        report("SPY option chain", True, f"{len(put_chain)} puts today")
    else:
        report("SPY option chain", False, str(chain)[:100])
except Exception as e:
    report("SPY option chain", False, str(e))

qctx.close()

# ── 7. Trade context ──────────────────────────────────────────────────────────
try:
    tctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host="127.0.0.1", port=11111,
        security_firm=SecurityFirm.FUTUJP
    )
    report("trade connection", True, "OK")
except Exception as e:
    report("trade connection", False, str(e))
    tctx = None

# ── 8. Account list ───────────────────────────────────────────────────────────
if tctx:
    try:
        ret, accs = tctx.get_acc_list()
        if ret == RET_OK and not accs.empty:
            acc_summary = accs[["acc_id", "trd_env", "acc_type"]].to_dict("records")
            report("account list", True, json.dumps(acc_summary, ensure_ascii=False)[:200])
        else:
            report("account list", False, str(accs)[:100])
            accs = None
    except Exception as e:
        report("account list", False, str(e))
        accs = None

# ── 9. SIMULATE account balance ──────────────────────────────────────────────
if tctx and accs is not None and ret == RET_OK:
    try:
        import pandas as pd
        sim_rows = accs[accs["trd_env"] == "SIMULATE"]
        if not sim_rows.empty:
            acc_id = int(sim_rows.iloc[0]["acc_id"])
            ret2, funds = tctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
            if ret2 == RET_OK and not funds.empty:
                cash = float(funds.iloc[0].get("cash", 0))
                net = float(funds.iloc[0].get("net_assets", 0))
                report("SIMULATE balance", True, f"cash=${cash:,.0f} net=${net:,.0f}")
            else:
                report("SIMULATE balance", False, str(funds)[:100])
        else:
            report("SIMULATE balance", False, "no SIMULATE account found")
    except Exception as e:
        report("SIMULATE balance", False, str(e))

# ── 10. REAL account balance (read-only) ─────────────────────────────────────
if tctx and accs is not None and ret == RET_OK:
    try:
        real_rows = accs[accs["trd_env"] == "REAL"]
        if not real_rows.empty:
            acc_id = int(real_rows.iloc[0]["acc_id"])
            ret3, funds = tctx.accinfo_query(trd_env=TrdEnv.REAL, acc_id=acc_id)
            if ret3 == RET_OK and not funds.empty:
                net = float(funds.iloc[0].get("net_assets", 0))
                cash = float(funds.iloc[0].get("cash", 0))
                report("REAL balance", True, f"net_assets=${net:,.0f} cash=${cash:,.0f}")
            else:
                report("REAL balance", False, str(funds)[:100])
        else:
            report("REAL balance", False, "no REAL account")
    except Exception as e:
        report("REAL balance", False, str(e))

if tctx:
    tctx.close()

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 50)
print(f"LIVE TEST: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print(f"FAILED: {FAIL}")
print("=" * 50)

sys.exit(0 if not FAIL else 1)
