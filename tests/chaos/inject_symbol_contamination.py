"""inject_symbol_contamination.py — 銘柄混入攻撃 (シナリオ2)

underlying_code 切替中に別銘柄 chain を注入し、
whitelist フィルタ (L1) が拒否することを確認。
SPY → GME に銘柄が混入したシミュレーション。
"""
from __future__ import annotations
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits


def run() -> dict:
    """whitelist 外銘柄 GME が注入された発注試行"""
    limits = load_limits(phase="P0_paper")

    # P0_paper whitelist に GME は存在しない
    contaminated_symbol = "US.GME"
    ctx = OrderContext(
        symbol=contaminated_symbol,
        strike=20.0,
        side="SELL",
        qty=1,
        option_price=2.50,
        bid=2.40,
        ask=2.60,
        est_margin=2500.0,
        capital_usd=120_000.0,
        paper=True,
    )
    result = check_order(ctx, limits=limits)
    # 期待: whitelist 拒否 (L1)
    blocked = (not result.allow) and "L1" in result.layer
    return {
        "scenario": "symbol_contamination",
        "description": f"underlying 切替中に {contaminated_symbol} chain が混入 → L1 whitelist 拒否期待",
        "expected": "L1 拒否 (symbol_whitelist)",
        "actual_allow": result.allow,
        "actual_layer": result.layer,
        "actual_reason": result.reason,
        "pass": blocked,
        "severity": "CRITICAL" if not blocked else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
