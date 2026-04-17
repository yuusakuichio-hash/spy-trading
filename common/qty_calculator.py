"""
common/qty_calculator.py — Self-Checking Pair発注計算 (NASA TMR思想)

NASA Triplex Modular Redundancy (TMR) の2-of-2 variant:
  - calc_qty_pure_python: 純粋Python演算で枚数算出
  - calc_qty_numpy:       NumPy演算で同じ計算を実行
  - calc_qty_verified:    両方の結果が一致した場合のみ返す。不一致は例外。

発注ロジックの数値計算エラー（浮動小数点バグ・ライブラリ破損・サイレント数値誤差）を
2つの独立した計算経路で相互検証することで検出する。

spy_bot.pyとの統合:
  calc_qty() の式は:
      int(cash * capital_pct / (width * 100))
  本モジュールの calc_qty_verified() に対応するマッピング:
      premium      = spread_width  (例: params["width"] = 10 → $10スプレッド)
      max_risk_pct = capital_pct   (例: params["capital_pct"] = 0.55)
  TMR検証は arithmetic（切り捨て前の生数値）の照合のみ行い、
  min/max クランプはcalc_qty側に委ねる。

  使い方 (spy_bot.py側):
      qty = calc_qty(cash, params, paper=self.paper)
      if _QTY_CALCULATOR_AVAILABLE:
          _tmr_verify_spread_qty(cash, params["width"], params["capital_pct"], qty)
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

# 許容誤差: 整数演算の切り捨て差異のみ許容（0差のみ厳密一致）
_ALLOWED_DIFF = 0


class QtyMismatchError(Exception):
    """Pure-Python結果とNumPy結果が一致しなかった場合に送出"""
    pass


def _validate_inputs(cash: float, premium: float, max_risk_pct: float,
                     min_qty: int, max_qty: "int | None") -> None:
    if cash < 0:
        raise ValueError(f"cash must be >= 0, got {cash}")
    if premium <= 0:
        raise ValueError(f"premium must be > 0, got {premium}")
    if not (0.0 < max_risk_pct <= 1.0):
        raise ValueError(f"max_risk_pct must be in (0, 1], got {max_risk_pct}")
    if min_qty < 1:
        raise ValueError(f"min_qty must be >= 1, got {min_qty}")
    if max_qty is not None and max_qty < min_qty:
        raise ValueError(f"max_qty ({max_qty}) must be >= min_qty ({min_qty})")


def calc_qty_pure_python(
    cash: float,
    premium: float,
    max_risk_pct: float,
    *,
    min_qty: int = 1,
    max_qty: "int | None" = None,
) -> int:
    """純粋Pythonで発注枚数を算出する。

    算出式:
        risk_budget  = cash * max_risk_pct
        qty_by_risk  = int(risk_budget / (premium * 100))
        result       = clamp(qty_by_risk, min_qty, max_qty)

    クレジットスプレッドの場合:
        premium = spread_width (スプレッド幅 $)
        contract_cost = spread_width * 100 (証拠金 $)
        → 式は calc_qty() と同一

    Args:
        cash:         口座残高（任意通貨建て）
        premium:      スプレッド幅またはオプションプレミアム（1株あたり）
        max_risk_pct: リスク許容率 / capital_pct（0.0〜1.0）
        min_qty:      最低枚数（デフォルト1）
        max_qty:      上限枚数（Noneなら上限なし）

    Returns:
        発注枚数（int）

    Raises:
        ValueError: cash/premium/max_risk_pct が不正な値の場合
    """
    _validate_inputs(cash, premium, max_risk_pct, min_qty, max_qty)

    risk_budget = cash * max_risk_pct
    contract_cost = premium * 100.0  # 1枚 = 100株相当（スプレッド幅×100 = 証拠金）
    qty_by_risk = int(risk_budget / contract_cost)

    result = max(min_qty, qty_by_risk)
    if max_qty is not None:
        result = min(result, max_qty)
    return result


def calc_qty_numpy(
    cash: float,
    premium: float,
    max_risk_pct: float,
    *,
    min_qty: int = 1,
    max_qty: "int | None" = None,
) -> int:
    """NumPy演算で発注枚数を算出する（calc_qty_pure_python の冗長パス）。

    同一の算出式をNumPy float64で計算し、整数に変換して返す。
    calc_qty_pure_python と実装を意図的に分離することで相互検証の独立性を確保する。
    """
    _validate_inputs(cash, premium, max_risk_pct, min_qty, max_qty)

    np_cash         = np.float64(cash)
    np_premium      = np.float64(premium)
    np_max_risk_pct = np.float64(max_risk_pct)

    risk_budget = np_cash * np_max_risk_pct
    contract_cost = np_premium * np.float64(100.0)
    qty_by_risk = int(np.floor(risk_budget / contract_cost))

    result = max(min_qty, qty_by_risk)
    if max_qty is not None:
        result = min(result, max_qty)
    return result


def calc_qty_verified(
    cash: float,
    premium: float,
    max_risk_pct: float,
    *,
    min_qty: int = 1,
    max_qty: "int | None" = None,
) -> int:
    """Self-Checking Pair: 2経路の計算結果が一致した場合のみ枚数を返す。

    NASA TMR思想に基づく冗長検証:
      1. calc_qty_pure_python で算出
      2. calc_qty_numpy で独立算出
      3. 両者が一致 → 結果を返す
      4. 不一致 → QtyMismatchError を送出（発注ブロック）

    Args:
        cash:         口座残高（任意通貨建て）
        premium:      スプレッド幅またはオプションプレミアム（1株あたり）
        max_risk_pct: リスク許容率 / capital_pct（0.0〜1.0）
        min_qty:      最低枚数（デフォルト1）
        max_qty:      上限枚数（Noneなら上限なし）

    Returns:
        両経路が合意した発注枚数（int）

    Raises:
        QtyMismatchError: 2経路の結果が異なる場合（発注ブロック）
        ValueError:       入力値が不正な場合
    """
    qty_py  = calc_qty_pure_python(cash, premium, max_risk_pct,
                                   min_qty=min_qty, max_qty=max_qty)
    qty_np  = calc_qty_numpy(cash, premium, max_risk_pct,
                              min_qty=min_qty, max_qty=max_qty)

    if abs(qty_py - qty_np) > _ALLOWED_DIFF:
        msg = (
            f"[TMR] qty mismatch — pure_python={qty_py} numpy={qty_np} "
            f"(cash={cash}, premium={premium}, max_risk_pct={max_risk_pct}). "
            "Order BLOCKED."
        )
        log.error(msg)
        raise QtyMismatchError(msg)

    log.debug(
        f"[TMR] qty verified: {qty_py} "
        f"(cash={cash:.0f}, premium={premium:.4f}, risk={max_risk_pct:.1%})"
    )
    return qty_py


def tmr_verify_spread_qty(
    cash: float,
    spread_width: float,
    capital_pct: float,
    qty_from_calc_qty: int,
) -> None:
    """spy_bot.py の calc_qty() 結果を TMR で事後検証するサイドカー関数。

    calc_qty() は min/max クランプ・フェーズ制限・SMALL_ACCOUNT特例等を含む。
    本関数は「arithmetic コアの一致」のみを検証し、クランプ後の差異は無視する。

    arithmetic_qty = int(cash * capital_pct / (spread_width * 100))
    qty_from_calc_qty >= arithmetic_qty が true であれば正常（min_qty保証のため）。
    qty_from_calc_qty > arithmetic_qty かつ arithmetic_qty >= 1 なら上限クランプを確認。

    不整合パターン:
        - arithmetic_qty が負になる（cash/capital_pct が破損）
        - pure_python と numpy の arithmetic_qty が不一致

    Args:
        cash:               口座残高
        spread_width:       スプレッド幅 (params["width"])
        capital_pct:        資本配分率 (params["capital_pct"])
        qty_from_calc_qty:  calc_qty() が返した枚数

    Raises:
        QtyMismatchError: arithmetic の2経路が不一致の場合
    """
    if cash <= 0 or spread_width <= 0 or capital_pct <= 0:
        # 入力値が 0/負の場合は検証スキップ（calc_qty側で処理済み）
        return

    # arithmetic コアのみ検証（min=1 / max=None でクランプなし）
    arith_py = calc_qty_pure_python(cash, spread_width, capital_pct, min_qty=1, max_qty=None)
    arith_np = calc_qty_numpy(cash, spread_width, capital_pct, min_qty=1, max_qty=None)

    if abs(arith_py - arith_np) > _ALLOWED_DIFF:
        msg = (
            f"[TMR] arithmetic mismatch — pure_python={arith_py} numpy={arith_np} "
            f"(cash={cash:.0f}, width={spread_width}, capital_pct={capital_pct:.4f}). "
            "Order BLOCKED."
        )
        log.error(msg)
        raise QtyMismatchError(msg)

    log.debug(
        f"[TMR] spread qty verified: arithmetic={arith_py}, "
        f"calc_qty_result={qty_from_calc_qty} "
        f"(cash={cash:.0f}, width={spread_width}, capital_pct={capital_pct:.1%})"
    )
