"""inject_tmr_mismatch.py -- TMR qty mismatch -> QtyMismatchError (Scenario 12)

calc_qty_pure_python と calc_qty_numpy の計算結果が乖離した場合に
QtyMismatchError が送出されることを確認。

通常の入力では一致するため、モンキーパッチで numpy パスを改ざんして乖離を注入する。
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from common.qty_calculator import (
    calc_qty_verified,
    calc_qty_pure_python,
    QtyMismatchError,
)


def run() -> dict:
    cash = 120_000.0
    premium = 10.0
    max_risk_pct = 0.05

    # 正常系: 一致することを確認
    try:
        normal_qty = calc_qty_verified(cash, premium, max_risk_pct)
        normal_ok = True
        normal_result = normal_qty
    except QtyMismatchError as e:
        normal_ok = False
        normal_result = str(e)

    # 異常系: numpy パスを改ざんして乖離を注入
    # calc_qty_numpy が +999 を返すように偽装
    def fake_numpy(c, p, r, *, min_qty=1, max_qty=None):
        real = calc_qty_pure_python(c, p, r, min_qty=min_qty, max_qty=max_qty)
        return real + 999   # 意図的な乖離

    mismatch_raised = False
    mismatch_msg = ""
    with patch("common.qty_calculator.calc_qty_numpy", side_effect=fake_numpy):
        try:
            calc_qty_verified(cash, premium, max_risk_pct)
        except QtyMismatchError as e:
            mismatch_raised = True
            mismatch_msg = str(e)

    passed = normal_ok and mismatch_raised

    return {
        "scenario": "tmr_qty_mismatch",
        "description": "calc_qty_pure_python vs calc_qty_numpy 乖離 -> QtyMismatchError 送出確認",
        "expected": "正常入力: 一致/通過。改ざんあり: QtyMismatchError 送出",
        "normal_pass": normal_ok,
        "normal_qty": normal_result,
        "mismatch_raised": mismatch_raised,
        "mismatch_msg": mismatch_msg,
        "pass": passed,
        "severity": "CRITICAL" if not passed else "OK",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
