"""inject_fat_finger_qty.py — fat finger qty 9999枚発注試行 (シナリオ5)

L1 max_qty_per_order ガードが 9999枚を拒否することを確認。
P0_paper の max_qty_per_order = 50。
"""
from __future__ import annotations
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits


def run() -> dict:
    """9999枚発注 → L1 qty 拒否期待"""
    limits = load_limits(phase="P0_paper")

    # max_qty_per_order = 50 を超える
    fat_qty = 9999
    ctx = OrderContext(
        symbol="US.SPY",
        strike=560.0,
        side="SELL",
        qty=fat_qty,
        option_price=1.00,
        bid=0.95,
        ask=1.05,
        est_margin=fat_qty * 100.0,
        capital_usd=120_000.0,
        paper=True,
    )
    result = check_order(ctx, limits=limits)
    blocked = (not result.allow) and "L1" in result.layer

    return {
        "scenario": "fat_finger_qty",
        "description": f"qty={fat_qty}枚発注試行 → L1 max_qty_per_order={limits.max_qty_per_order} 拒否期待",
        "expected": f"L1 拒否 (qty {fat_qty} > {limits.max_qty_per_order})",
        "actual_allow": result.allow,
        "actual_layer": result.layer,
        "actual_reason": result.reason,
        "pass": blocked,
        "severity": "CRITICAL" if not blocked else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
