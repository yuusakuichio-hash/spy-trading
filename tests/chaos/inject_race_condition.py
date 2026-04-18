"""inject_race_condition.py — race condition 同strike×3連続発注 (シナリオ7)

同一 (symbol, strike, side) を 3回連続発注 → L4 重複発注疑い拒否確認。
pre_trade_check の _recent_keys deque が 3件以上で L4 ブロック。
"""
from __future__ import annotations
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

import common.pre_trade_check as ptc
from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits


def run() -> dict:
    """同 strike×3連続発注 → L4 重複発注拒否期待"""
    limits = load_limits(phase="P0_paper")

    # テスト前に _recent_keys をクリア（他テストの汚染防止）
    ptc._recent_orders.clear()
    ptc._recent_keys.clear()

    symbol = "US.SPY"
    strike = 565.0
    side = "SELL"

    order_results = []
    for attempt in range(1, 5):  # 4回試行 → 3回目以降は拒否期待
        ctx = OrderContext(
            symbol=symbol,
            strike=strike,
            side=side,
            qty=1,
            option_price=1.00,
            bid=0.95,
            ask=1.05,
            est_margin=500.0,
            capital_usd=120_000.0,
            paper=True,
        )
        r = check_order(ctx, limits=limits)
        order_results.append({
            "attempt": attempt,
            "allow": r.allow,
            "layer": r.layer,
            "reason": r.reason,
        })

    # 最初の2回は通過、3回目以降は L4 ブロック期待
    # duplicate_count >= 3 の条件なので 4回目の発注で拒否
    fourth_blocked = (
        not order_results[3]["allow"] and
        "L4" in order_results[3]["layer"]
    )

    # 後片付け
    ptc._recent_orders.clear()
    ptc._recent_keys.clear()

    return {
        "scenario": "race_condition_same_strike",
        "description": f"({symbol}, {strike}, {side}) × 4回連続発注 → L4 重複拒否確認",
        "expected": "4回目発注で L4 重複発注疑い拒否",
        "order_results": order_results,
        "pass": fourth_blocked,
        "severity": "CRITICAL" if not fourth_blocked else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
