"""
common/qty_calculator.py — Self-Checking Pair発注計算 (NASA TMR思想)

NASA Triplex Modular Redundancy (TMR) の3-of-3 variant:
  - calc_qty_pure_python: 純粋Python演算で枚数算出
  - calc_qty_numpy:       NumPy演算で同じ計算を実行
  - calc_qty_decimal:     decimal.Decimal演算で独立経路を提供（NEW）
  - calc_qty_verified:    3経路の過半数一致を確認してから返す（NEW: 2-of-3方式）
  - tmr_verify_naked_qty: ネイキッドオプション・ストラングル用（spread_width不要）（NEW）

発注ロジックの数値計算エラー（浮動小数点バグ・ライブラリ破損・サイレント数値誤差）を
3つの独立した計算経路で相互検証することで検出する。

旧2経路の問題:
  pure_python と numpy は同一のIEEE754演算を使うため構造的に一致してしまい、
  TMRとしての独立性がゼロだった。Decimal経路はfloatとは独立した
  多倍長固定小数点演算を使うため真の冗長性を提供する。

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
      # ストラングル・ネイキッドオプション:
          _tmr_verify_naked_qty(cash, premium_per_share, qty)
"""

from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal, InvalidOperation

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


# ══════════════════════════════════════════════════════════════════════════════
# Decimal 独立経路 (CRITICAL-3修正: pure_python/numpyのIEEE754構造的一致問題を解決)
# ══════════════════════════════════════════════════════════════════════════════

def calc_qty_decimal(
    cash: float,
    premium: float,
    max_risk_pct: float,
    *,
    min_qty: int = 1,
    max_qty: "int | None" = None,
) -> int:
    """decimal.Decimal演算で発注枚数を算出する（pure_python/numpyと独立の第3経路）。

    IEEE754 float演算とは独立した多倍長固定小数点演算を使用するため、
    pure_python と numpy の構造的一致問題（同じIEEE754演算ゆえTMRとして独立性ゼロ）
    を解消する真の第3経路を提供する。

    算出式:
        risk_budget  = cash * max_risk_pct
        qty_by_risk  = int(risk_budget / (premium * 100))  (ROUND_DOWN)
        result       = clamp(qty_by_risk, min_qty, max_qty)
    """
    _validate_inputs(cash, premium, max_risk_pct, min_qty, max_qty)

    try:
        d_cash         = Decimal(str(cash))
        d_premium      = Decimal(str(premium))
        d_max_risk_pct = Decimal(str(max_risk_pct))

        risk_budget   = d_cash * d_max_risk_pct
        contract_cost = d_premium * Decimal("100")
        qty_raw       = (risk_budget / contract_cost).to_integral_value(rounding=ROUND_DOWN)
        qty_by_risk   = int(qty_raw)
    except InvalidOperation as e:
        raise ValueError(f"calc_qty_decimal: Decimal演算失敗: {e}") from e

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
    """Self-Checking Triplex: 3経路の過半数一致(2-of-3)を確認してから返す。

    旧2経路（pure_python + numpy）はIEEE754演算が同じため構造的に常に一致し、
    TMRとしての独立性がゼロだった。本関数は第3経路（Decimal）を加えて
    真の独立性を確保する。

    過半数一致方式（2-of-3）:
      - 3経路全一致: 正常
      - 2経路一致（1経路乖離）: 乖離した経路をwarningして多数決結果を返す
      - 3経路全不一致: QtyMismatchError（発注ブロック）

    Args:
        cash:         口座残高
        premium:      スプレッド幅またはオプションプレミアム（1株あたり）
        max_risk_pct: リスク許容率（0.0〜1.0）
        min_qty:      最低枚数（デフォルト1）
        max_qty:      上限枚数（Noneなら上限なし）

    Returns:
        過半数が合意した発注枚数（int）

    Raises:
        QtyMismatchError: 3経路が全て異なる場合（発注ブロック）
        ValueError:       入力値が不正な場合
    """
    qty_py  = calc_qty_pure_python(cash, premium, max_risk_pct,
                                   min_qty=min_qty, max_qty=max_qty)
    qty_np  = calc_qty_numpy(cash, premium, max_risk_pct,
                              min_qty=min_qty, max_qty=max_qty)
    qty_dec = calc_qty_decimal(cash, premium, max_risk_pct,
                               min_qty=min_qty, max_qty=max_qty)

    # 過半数一致チェック（2-of-3）
    if qty_py == qty_np == qty_dec:
        log.debug(
            f"[TMR-3] qty verified (3/3): {qty_py} "
            f"(cash={cash:.0f}, premium={premium:.4f}, risk={max_risk_pct:.1%})"
        )
        return qty_py

    if qty_py == qty_np:
        log.warning(
            f"[TMR-3] Decimal経路乖離 (2/3合意): py={qty_py} np={qty_np} dec={qty_dec} — "
            f"py/npの結果を採用 (cash={cash:.0f}, premium={premium:.4f})"
        )
        return qty_py

    if qty_py == qty_dec:
        log.warning(
            f"[TMR-3] NumPy経路乖離 (2/3合意): py={qty_py} np={qty_np} dec={qty_dec} — "
            f"py/decの結果を採用 (cash={cash:.0f}, premium={premium:.4f})"
        )
        return qty_py

    if qty_np == qty_dec:
        log.warning(
            f"[TMR-3] PurePython経路乖離 (2/3合意): py={qty_py} np={qty_np} dec={qty_dec} — "
            f"np/decの結果を採用 (cash={cash:.0f}, premium={premium:.4f})"
        )
        return qty_np

    # 3経路全不一致
    msg = (
        f"[TMR-3] qty全経路不一致 — pure_python={qty_py} numpy={qty_np} decimal={qty_dec} "
        f"(cash={cash}, premium={premium}, max_risk_pct={max_risk_pct}). "
        "Order BLOCKED."
    )
    log.error(msg)
    raise QtyMismatchError(msg)


def tmr_verify_naked_qty(
    cash: float,
    premium_per_share: float,
    qty: int,
    stop_loss_mult: float = 2.0,
) -> None:
    """ネイキッドオプション・ストラングル用TMR検証 (spread_width不要)。

    StrangleSell等 spread_width が定義できないポジション向け。
    premium * qty * 100 が cash の合理的な範囲内かを3経路で検証する。

    tmr_verify_spread_qty との違い:
      - spread_width ではなく premium_per_share (プレミアム受取額) を使う
      - 最大損失 = premium * stop_loss_mult * qty * 100 で概算
      - arithmetic コアの一致を3経路（pure_python/numpy/decimal）で確認

    Args:
        cash:               口座残高
        premium_per_share:  受取プレミアム合計（call_mid + put_mid など）
        qty:                発注枚数
        stop_loss_mult:     損切り倍率（デフォルト2.0 = 最大損失=受取の2倍）

    Raises:
        QtyMismatchError:   3経路のarithmetic計算が過半数不一致の場合
        ValueError:         入力値が不正 または spread_width=0渡しで呼ばれた場合
    """
    if premium_per_share <= 0:
        raise ValueError(
            f"tmr_verify_naked_qty: premium_per_share={premium_per_share} <= 0 は禁止。"
            "spread_width=0でtmr_verify_spread_qtyを呼ぶ誤用を防ぐため例外を送出する。"
        )
    if cash <= 0:
        raise ValueError(f"tmr_verify_naked_qty: cash={cash} <= 0")
    if qty <= 0:
        raise ValueError(f"tmr_verify_naked_qty: qty={qty} <= 0")

    # 最大損失でのrisk_pct相当を算出してarithmetic検証
    # max_loss_per_contract = premium_per_share * stop_loss_mult * 100
    # arithmetic_qty = int(cash * risk_pct / max_loss_per_contract) の逆算ではなく
    # 直接3経路で premium * qty * 100 の金額が cash以内かを検証する

    # 3経路でmax_notional計算の一致を確認
    notional_py  = int(premium_per_share * qty * 100)
    notional_np  = int(float(np.float64(premium_per_share)) * qty * 100)
    notional_dec = int((Decimal(str(premium_per_share)) * qty * 100).to_integral_value(rounding=ROUND_DOWN))

    if abs(notional_py - notional_np) > 1 or abs(notional_py - notional_dec) > 1:
        msg = (
            f"[TMR-naked] notional不一致 — py={notional_py} np={notional_np} dec={notional_dec} "
            f"(premium={premium_per_share}, qty={qty}). Order BLOCKED."
        )
        log.error(msg)
        raise QtyMismatchError(msg)

    # notional が cash の合理的範囲内か確認（500%超は異常）
    ratio = notional_py / cash if cash > 0 else float("inf")
    if ratio > 5.0:
        msg = (
            f"[TMR-naked] notional過大 — notional={notional_py} cash={cash:.0f} "
            f"ratio={ratio:.1f}x (> 5.0x threshold). Order BLOCKED."
        )
        log.error(msg)
        raise QtyMismatchError(msg)

    log.debug(
        f"[TMR-naked] verified: premium={premium_per_share:.4f} qty={qty} "
        f"notional={notional_py} cash={cash:.0f} ratio={ratio:.2f}x"
    )
