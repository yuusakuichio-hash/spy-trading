"""inject_deep_itm.py — 裸ショート/深ITM発注試行 (シナリオ1)

L1 max_option_price ガードが $1697 を拒否することを確認。
"""
from __future__ import annotations
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits


def run() -> dict:
    """Deep ITM 発注試行 → L1 拒否を期待"""
    limits = load_limits(phase="P0_paper")
    ctx = OrderContext(
        symbol="US.SPXW",
        strike=5400.0,
        side="BUY",
        qty=1,
        option_price=1697.30,   # Deep ITM 異常価格
        bid=1697.00,
        ask=1697.60,
        est_margin=169730.0,
        capital_usd=120_000.0,
        paper=True,
    )
    result = check_order(ctx, limits=limits)
    blocked = (not result.allow) and ("L1" in result.layer or "Deep ITM" in result.reason or "Deep ITM価格発注拒否" in result.reason)
    return {
        "scenario": "deep_itm_naked_long",
        "description": "SPXW 5400C @$1697.30 Deep ITM 発注試行",
        "expected": "L1 拒否 (max_option_price=50.0)",
        "actual_allow": result.allow,
        "actual_layer": result.layer,
        "actual_reason": result.reason,
        "pass": blocked,
        "severity": "CRITICAL" if not blocked else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
