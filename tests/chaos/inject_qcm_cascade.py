"""inject_qcm_cascade.py — Quote Context 3回連続切断 (シナリオ3)

QCM が level 0→1→2→3 に段階遷移し、
level 3 で新規エントリーが停止することを確認。
"""
from __future__ import annotations
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from common.quote_context_manager import QuoteContextManager, set_global_manager
from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits


def run() -> dict:
    """3回連続切断 → level 3 → 新規エントリー停止"""
    mgr = QuoteContextManager()
    set_global_manager(mgr)

    transitions = []

    # 切断 3回注入
    for i in range(1, 4):
        mgr.on_disconnect()
        transitions.append({
            "disconnect_count": i,
            "level": mgr.get_level(),
            "allow_new_entry": mgr.allow_new_entry(),
            "margin_scale": mgr.margin_scale(),
        })

    # level 3 で発注試行 → QCM ブロック期待
    limits = load_limits(phase="P0_paper")
    ctx = OrderContext(
        symbol="US.SPY",
        strike=560.0,
        side="SELL",
        qty=1,
        option_price=1.00,
        bid=0.95,
        ask=1.05,
        est_margin=500.0,
        capital_usd=120_000.0,
        paper=True,
    )
    result = check_order(ctx, limits=limits)

    level3_blocked = (not result.allow) and "QCM" in result.layer
    level3_state = mgr.get_level() == 3

    # 後片付け: グローバルマネージャをリセット
    set_global_manager(None)

    return {
        "scenario": "qcm_3x_disconnect",
        "description": "Quote Context 3回連続切断 → level 3 → 新規エントリー停止",
        "expected": "level=3 でエントリー拒否 (QCM layer)",
        "transitions": transitions,
        "final_level": mgr.get_level(),
        "order_allow": result.allow,
        "order_layer": result.layer,
        "order_reason": result.reason,
        "pass": level3_blocked and level3_state,
        "severity": "CRITICAL" if not (level3_blocked and level3_state) else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
