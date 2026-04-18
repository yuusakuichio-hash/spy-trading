"""common/option_code.py — オプションコードの生成・解析・バリデーション

4/17事故の根本: SPX取引中にSPY用のATM strike($710)が混入し、コードバリデーションが
なかったためそのまま発注されてしまった。

このモジュールが全発注前のバリデーションゲートとなる。
validate_code_for_symbol() が False を返したら即発注ブロック。

futu オプションコード形式:
  US.SPY260417C00710000
  ^--- underlying prefix (US.SPY / US.SPXW / US.QQQ 等)
        ^----- YYMMDD expiry
               ^ C or P
                ^-------- strike * 1000, ゼロ埋め8桁

SPX/SPXW の注意:
  - underlying (moomoo API) = "US..SPX"
  - option code prefix = "SPXW" (0DTE/Weekly) or "SPX" (Monthly)
  - "US..SPX" を _build_option_code に渡すと "US..SPX..." になるため
    build_option_code() 内で option_root を参照して "US.SPXW..." に変換する
"""

from __future__ import annotations
import re
from typing import Optional

from common.symbol_meta import (
    SYMBOL_META,
    _ROOT_TO_UNDERLYING,
    get_option_root,
    get_strike_interval,
    underlying_from_option_root,
)

# コードパターン: US.ROOT YYMMDD [C|P] STRIKE(8桁)
# ROOT は英大文字1文字以上（例: SPY / SPXW / QQQ）
_CODE_PATTERN = re.compile(
    r"^(US\.)([A-Z\.]+?)(\d{6})([CP])(\d{8})$"
)


def parse_option_code(code: str) -> Optional[dict]:
    """futu オプションコードをパースして dict を返す。

    Args:
        code: "US.SPXW260417C05400000" 等

    Returns:
        {
          "prefix":     "US.",
          "root":       "SPXW",
          "expiry":     "2026-04-17",
          "side":       "C",
          "strike":     5400.0,
          "underlying": "US..SPX",  # _ROOT_TO_UNDERLYING から解決
        }
        パース失敗時は None。
    """
    if not code:
        return None
    m = _CODE_PATTERN.match(code)
    if not m:
        return None

    prefix   = m.group(1)   # "US."
    root     = m.group(2)   # "SPXW" / "SPY" / "QQQ"
    exp_str  = m.group(3)   # "260417"
    side     = m.group(4)   # "C" or "P"
    raw      = m.group(5)   # "05400000"

    strike = int(raw) / 1000.0

    # YYMMDD -> YYYY-MM-DD
    try:
        yy = int(exp_str[0:2])
        mm = int(exp_str[2:4])
        dd = int(exp_str[4:6])
        yyyy = 2000 + yy
        expiry = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
    except ValueError:
        expiry = ""

    underlying = underlying_from_option_root(root)

    return {
        "prefix":     prefix,
        "root":       root,
        "expiry":     expiry,
        "side":       side,
        "strike":     strike,
        "underlying": underlying,
    }


def validate_code_for_symbol(code: str, expected_symbol: str) -> bool:
    """オプションコードが期待する underlying 銘柄と一致するか検証する。

    4/17事故防止の最終ゲート。place_order 直前に必ず呼ぶ。

    Args:
        code:            "US.SPXW260417C05400000" 等
        expected_symbol: "US..SPX" 等（trade intent の銘柄）

    Returns:
        True:  コードが expected_symbol のオプションである
        False: 銘柄混入を検知（発注ブロックすべき）

    Examples:
        validate_code_for_symbol("US.SPXW260417C05400000", "US..SPX") -> True
        validate_code_for_symbol("US.SPY260417C00710000",  "US..SPX") -> False  # 4/17事故シナリオ
        validate_code_for_symbol("US.SPY260417C00710000",  "US.SPY")  -> True
    """
    if not code or not expected_symbol:
        return False

    parsed = parse_option_code(code)
    if parsed is None:
        # パース失敗 = フォーマット不正 -> ブロック
        return False

    underlying = parsed.get("underlying")
    if underlying is None:
        # 未知の root -> ブロック
        return False

    return underlying == expected_symbol


def build_option_code(symbol: str, expiry: str, strike: float,
                      opt_type: str, use_0dte: bool = True) -> str:
    """futu オプションコードを生成する。

    4/17事故対応: symbol が "US..SPX" の場合、root を "SPXW" に変換して
    "US.SPXW260418C05400000" 形式を生成する。
    従来の _build_option_code は "US..SPX..." になりfutuが受け付けなかった。

    Args:
        symbol:   "US..SPX" / "US.SPY" 等 (futu underlying code)
        expiry:   "2026-04-18" (YYYY-MM-DD)
        strike:   5400.0
        opt_type: "CALL" or "PUT"
        use_0dte: True = SPXW / False = SPX monthly

    Returns:
        "US.SPXW260418C05400000" 等
    """
    root     = get_option_root(symbol, use_0dte=use_0dte)
    yy_mm_dd = expiry.replace("-", "")[2:]   # "2026-04-18" -> "260418"
    cp       = "C" if opt_type.upper() in ("CALL", "C") else "P"

    # SPXはstrike $5000 -> 5000.000 -> 5000000 / 1000 = 5000.0
    # futu形式: strike * 1000 -> 8桁ゼロ埋め
    strike_i = int(round(strike * 1000))
    return f"US.{root}{yy_mm_dd}{cp}{strike_i:08d}"


def round_strike(symbol: str, price: float) -> float:
    """銘柄のstrike_intervalに従って価格をATMに丸める。

    Args:
        symbol: "US..SPX" 等
        price:  原資産価格

    Returns:
        最近傍のstrike価格

    Examples:
        round_strike("US.SPY", 561.3) -> 561.0
        round_strike("US..SPX", 5412.7) -> 5415.0   # $5刻み
    """
    interval = get_strike_interval(symbol)
    if interval <= 0:
        interval = 1.0
    return round(price / interval) * interval
