"""inject_whitelist_bypass.py — whitelist 外銘柄 (GME/AMC) 発注試行 (シナリオ6)

L1 symbol_whitelist が GME / AMC を拒否することを確認。
ミームストック混入攻撃シミュレーション。
"""
from __future__ import annotations
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits


def run() -> dict:
    """GME / AMC → L1 whitelist 拒否期待"""
    limits = load_limits(phase="P0_paper")

    meme_stocks = [
        ("US.GME", 20.0, 5.00),
        ("US.AMC", 5.0, 0.50),
        ("US.BBBY", 2.0, 0.30),   # 廃業済みミームストック
    ]

    results = []
    for sym, strike, price in meme_stocks:
        ctx = OrderContext(
            symbol=sym,
            strike=strike,
            side="SELL",
            qty=1,
            option_price=price,
            bid=price * 0.95,
            ask=price * 1.05,
            est_margin=strike * 100.0,
            capital_usd=120_000.0,
            paper=True,
        )
        r = check_order(ctx, limits=limits)
        blocked = (not r.allow) and "L1" in r.layer
        results.append({
            "symbol": sym,
            "allow": r.allow,
            "layer": r.layer,
            "blocked": blocked,
        })

    all_blocked = all(r["blocked"] for r in results)

    return {
        "scenario": "whitelist_bypass_attempt",
        "description": "GME/AMC/BBBY whitelist 外銘柄 → L1 拒否確認",
        "expected": "全銘柄 L1 拒否",
        "tested_symbols": results,
        "pass": all_blocked,
        "severity": "CRITICAL" if not all_blocked else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
