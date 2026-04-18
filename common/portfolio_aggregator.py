"""Portfolio Aggregator — Daily/Weekly/Monthly P&L集計

condor_pnl.json からloss_gate判定に必要な累積P&L計算。
"""
from __future__ import annotations
import datetime
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
PNL_FILE = BASE / "data" / "condor_pnl.json"


def _load_pnl() -> list:
    if not PNL_FILE.exists():
        return []
    try:
        with open(PNL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("trades", []) if isinstance(data, dict) else data
    except Exception:
        return []


def _sum_pnl(trades: list, since: datetime.date) -> float:
    total = 0.0
    since_str = since.strftime("%Y-%m-%d")
    for t in trades:
        if t.get("event") != "exit":
            continue
        d = t.get("date", "")
        if d >= since_str:
            total += float(t.get("pnl_usd", 0) or 0)
    return total


def daily_pnl(today: datetime.date | None = None) -> float:
    today = today or datetime.date.today()
    return _sum_pnl(_load_pnl(), today)


def weekly_pnl(today: datetime.date | None = None) -> float:
    today = today or datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    return _sum_pnl(_load_pnl(), week_start)


def monthly_pnl(today: datetime.date | None = None) -> float:
    today = today or datetime.date.today()
    month_start = today.replace(day=1)
    return _sum_pnl(_load_pnl(), month_start)


def check_loss_gates(capital_usd: float, limits) -> tuple[bool, str]:
    """Returns (allow_new_entry, reason). 違反時(False, reason)"""
    dp = daily_pnl()
    wp = weekly_pnl()
    mp = monthly_pnl()
    if capital_usd <= 0:
        return True, "no capital ref"
    if dp / capital_usd <= limits.daily_loss_pct:
        return False, f"daily_loss_gate: ${dp:.0f} ({dp/capital_usd:.1%})"
    if wp / capital_usd <= limits.weekly_loss_pct:
        return False, f"weekly_loss_gate: ${wp:.0f} ({wp/capital_usd:.1%})"
    if mp / capital_usd <= limits.monthly_loss_pct:
        return False, f"monthly_loss_gate: ${mp:.0f} ({mp/capital_usd:.1%}) → Kill Switch推奨"
    return True, "ok"
