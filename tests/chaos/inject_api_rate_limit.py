"""inject_api_rate_limit.py — API rate limit 過負荷 → graceful degradation (シナリオ10)

L4 orders_per_minute_limit を連続発注で超過させ、
rate limit 到達後に拒否されることを確認。
P0_paper: orders_per_minute_limit = 15
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
    """orders_per_minute_limit を超える連続発注 → L4 拒否"""
    limits = load_limits(phase="P0_paper")
    rate_limit = limits.orders_per_minute_limit  # 15

    # テスト前クリア
    ptc._recent_orders.clear()
    ptc._recent_keys.clear()

    # rate limit + 1 回発注して最後が拒否されることを確認
    # 各発注は異なる strike にして duplicate フィルタを回避
    order_results = []
    for i in range(rate_limit + 2):
        ctx = OrderContext(
            symbol="US.SPY",
            strike=500.0 + i,    # strike を変化させて duplicate 回避
            side="SELL",
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
            "attempt": i + 1,
            "allow": r.allow,
            "layer": r.layer,
        })

    # rate_limit 件目以降は L4 拒否期待
    blocked_correctly = any(
        not r["allow"] and "L4" in r["layer"]
        for r in order_results[rate_limit:]
    )
    passed_before_limit = sum(
        1 for r in order_results[:rate_limit] if r["allow"]
    ) == rate_limit

    # 後片付け
    ptc._recent_orders.clear()
    ptc._recent_keys.clear()

    return {
        "scenario": "api_rate_limit_overload",
        "description": f"1分間に {rate_limit + 2} 回発注試行 → {rate_limit} 件超過で L4 拒否",
        "expected": f"最初 {rate_limit} 件通過 → それ以降 L4 拒否",
        "rate_limit": rate_limit,
        "total_attempts": len(order_results),
        "passed_before_limit": passed_before_limit,
        "blocked_after_limit": blocked_correctly,
        "order_results": order_results,
        "pass": blocked_correctly and passed_before_limit,
        "severity": "CRITICAL" if not (blocked_correctly and passed_before_limit) else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
