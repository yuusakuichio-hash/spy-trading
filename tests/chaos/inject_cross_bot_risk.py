"""inject_cross_bot_risk.py — Bot間リスク越境 (シナリオ8)

spy_bot 単体 50% 証拠金 + momentum_bot 想定 20% = 合計 70% で
L3B cross_bot_margin_limit 拒否確認。
portfolio_positions.json を一時書き換えてシミュレーション。
"""
from __future__ import annotations
import json
import sys
import tempfile
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

import common.portfolio_aggregator as pa
from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import load_limits

POSITIONS_FILE = BASE / "data" / "portfolio_positions.json"


def run() -> dict:
    """spy_bot 50% + momentum_bot 20% = 70% → L3B 拒否期待"""
    capital = 120_000.0
    limits = load_limits(phase="P0_paper")
    # P0_paper max_margin_pct_total = 0.50

    # 既存ファイルバックアップ
    backup = None
    if POSITIONS_FILE.exists():
        backup = POSITIONS_FILE.read_text(encoding="utf-8")

    try:
        # 前テストの Kill Switch 残留を解除
        from common import kill_switch as _ks
        _ks.deactivate()
        # QCM グローバルをリセット
        from common.quote_context_manager import set_global_manager
        set_global_manager(None)

        # spy_bot: 50% 証拠金 (60,000 USD), momentum_bot: 20% (24,000 USD) 注入
        fake_positions = {
            "spy_bot": {
                "positions": [{"symbol": "US.SPY", "qty": 10, "delta": -0.3}],
                "total_risk": capital * 0.50,   # 60,000
                "updated_at": "2026-04-18T10:00:00",
            },
            "momentum_bot": {
                "positions": [{"symbol": "US.QQQ", "qty": 5, "delta": 0.2}],
                "total_risk": capital * 0.20,   # 24,000
                "updated_at": "2026-04-18T10:00:00",
            },
        }
        POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        POSITIONS_FILE.write_text(
            json.dumps(fake_positions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 新規発注試行 (est_margin は小さい値だが cross_bot 合計が超過)
        ctx = OrderContext(
            symbol="US.SPY",
            strike=560.0,
            side="SELL",
            qty=1,
            option_price=1.00,
            bid=0.95,
            ask=1.05,
            est_margin=100.0,       # 個別は小さい
            capital_usd=capital,
            open_margin_total=capital * 0.50,   # spy_bot 分
            paper=True,
        )
        result = check_order(ctx, limits=limits)

        # cross_bot チェックで拒否期待 (L3B)
        cross_blocked = (not result.allow) and "L3B" in result.layer

        # 合計証拠金を直接確認
        summary = pa.aggregate_portfolio_risk()
        total_pct = summary.total_risk_usd / capital

    finally:
        # 後片付け: バックアップを復元
        if backup is not None:
            POSITIONS_FILE.write_text(backup, encoding="utf-8")
        elif POSITIONS_FILE.exists():
            POSITIONS_FILE.unlink()

    return {
        "scenario": "cross_bot_margin_overflow",
        "description": "spy_bot 50% + momentum_bot 20% = 70% 合計 → L3B cross_bot 拒否確認",
        "expected": "L3B 拒否 (合計証拠金 70% > max 50%)",
        "total_risk_pct": f"{total_pct:.1%}",
        "order_allow": result.allow,
        "order_layer": result.layer,
        "order_reason": result.reason,
        "pass": cross_blocked,
        "severity": "CRITICAL" if not cross_blocked else "OK",
    }


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(run(), ensure_ascii=False, indent=2))
