"""inject_kill_switch.py — Kill Switch 発動 + 全発注停止確認 (シナリオ4)

日次 -5% 損失到達で Kill Switch を手動発動し、
以後の全発注試行がブロックされることを確認。
"""
from __future__ import annotations
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from common import kill_switch
from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits


def run() -> dict:
    """Kill Switch 発動 → 発注ブロック確認"""
    # 前回テスト残りのフラグがあれば先にクリア
    kill_switch.deactivate()

    # Kill Switch 発動 (日次 -5% 到達シミュレーション)
    kill_switch.activate("chaos_inject: daily_loss=-5.0% triggered")

    activated = kill_switch.is_active()
    ks_reason = kill_switch.reason()

    # 通常の発注試行 → KILL layer で全拒否期待
    limits = load_limits(phase="P0_paper")
    orders_blocked = []
    test_cases = [
        ("US.SPY",  560.0, "SELL", 1, 1.00),
        ("US.SPXW", 5500.0, "SELL", 2, 0.50),
        ("US.QQQ",  450.0, "BUY",  1, 3.00),
    ]
    for sym, strike, side, qty, price in test_cases:
        ctx = OrderContext(
            symbol=sym, strike=strike, side=side, qty=qty,
            option_price=price, est_margin=500.0,
            capital_usd=120_000.0, paper=True,
        )
        r = check_order(ctx, limits=limits)
        orders_blocked.append({
            "symbol": sym,
            "allow": r.allow,
            "layer": r.layer,
            "blocked_by_kill": (not r.allow) and r.layer == "KILL",
        })

    all_blocked = all(o["blocked_by_kill"] for o in orders_blocked)

    # 後片付け
    kill_switch.deactivate()

    return {
        "scenario": "kill_switch_activation",
        "description": "日次 -5% 損失 → Kill Switch 発動 → 全発注停止確認",
        "expected": "全発注が KILL layer でブロック",
        "kill_switch_activated": activated,
        "kill_switch_reason": ks_reason,
        "orders": orders_blocked,
        "pass": activated and all_blocked,
        "severity": "CRITICAL" if not (activated and all_blocked) else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
