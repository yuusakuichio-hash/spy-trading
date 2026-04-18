"""common/symbol_meta.py — 銘柄別メタ情報の権威ある情報源

4/17事故の根本原因: SPY/SPX/SPXW の strike_interval・option_code_prefix・settlement
が散在していたため、chain取得時に混入が物理的に防げなかった。

このモジュールが全戦術から参照される唯一の情報源。
symbol_params.json の値よりこのモジュールが優先（静的定数として埋め込み済み）。

futuコードルール:
  - US.SPY260417C00710000  ... ETF/株のオプション
  - US.SPXW260417C05400000 ... SPX 0DTE/Weekly (SPXW prefix)
  - US.SPX260417C05400000  ... SPX Monthly (第3金曜)

underlying とオプションコード root の対応:
  underlying="US..SPX" -> option_root="SPXW" (0DTE/Weekly) or "SPX" (Monthly)
  0DTE戦略では常に SPXW を使う。
"""

from __future__ import annotations
from typing import Optional

# ── 銘柄メタ定義 ─────────────────────────────────────────────────────────────
# strike_interval: strike刻み幅（$）
# multiplier: オプション1枚あたりの株数（常に100）
# settlement: "physical" = 権利行使で株受け渡し / "cash_european" = 現金清算・欧州型
# option_root_0dte: 0DTE戦略で使うオプションコードのroot prefix
# center_strike_tolerance: center_strike±X%範囲外を混入と判定する閾値
#   SPY  ±20%: strike $140–$850 を許容（実用上十分）
#   SPX  ±10%: strike $4800–$5900 付近を許容（$710のSPY strikeが混入したら即検知）
#   個別株 ±25%: 個別株はボラが高いため少し広め

SYMBOL_META: dict[str, dict] = {
    "US.SPY": {
        "strike_interval":          1.0,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "SPY",
        "center_strike_tolerance":  0.20,
        "exchange":                 "NYSEArca",
        "type":                     "etf",
    },
    "US..SPX": {
        "strike_interval":          5.0,
        "multiplier":               100,
        "settlement":               "cash_european",
        "section_1256":             True,
        "option_root_0dte":         "SPXW",   # 0DTE/Weeklyは常にSPXW
        "option_root_monthly":      "SPX",    # 第3金曜montlyはSPX
        "center_strike_tolerance":  0.10,     # ±10%で SPY strike 混入を即検知
        "exchange":                 "CBOE",
        "type":                     "index",
    },
    "US.QQQ": {
        "strike_interval":          1.0,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "QQQ",
        "center_strike_tolerance":  0.20,
        "exchange":                 "NASDAQ",
        "type":                     "etf",
    },
    "US.IWM": {
        "strike_interval":          0.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "IWM",
        "center_strike_tolerance":  0.20,
        "exchange":                 "NYSEArca",
        "type":                     "etf",
    },
    "US.NVDA": {
        "strike_interval":          2.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "NVDA",
        "center_strike_tolerance":  0.25,
        "type":                     "stock",
    },
    "US.TSLA": {
        "strike_interval":          2.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "TSLA",
        "center_strike_tolerance":  0.25,
        "type":                     "stock",
    },
    "US.META": {
        "strike_interval":          2.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "META",
        "center_strike_tolerance":  0.25,
        "type":                     "stock",
    },
    "US.AMZN": {
        "strike_interval":          2.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "AMZN",
        "center_strike_tolerance":  0.25,
        "type":                     "stock",
    },
    "US.GOOGL": {
        "strike_interval":          2.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "GOOGL",
        "center_strike_tolerance":  0.25,
        "type":                     "stock",
    },
    "US.AAPL": {
        "strike_interval":          2.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "AAPL",
        "center_strike_tolerance":  0.25,
        "type":                     "stock",
    },
    "US.MSFT": {
        "strike_interval":          2.5,
        "multiplier":               100,
        "settlement":               "physical",
        "section_1256":             False,
        "option_root_0dte":         "MSFT",
        "center_strike_tolerance":  0.25,
        "type":                     "stock",
    },
}

# オプションコード root -> underlying の逆引きマップ
# SPXWのオプションコードから underlying=US..SPX を確定させる
_ROOT_TO_UNDERLYING: dict[str, str] = {
    "SPY":   "US.SPY",
    "QQQ":   "US.QQQ",
    "IWM":   "US.IWM",
    "SPXW":  "US..SPX",
    "SPX":   "US..SPX",
    "NVDA":  "US.NVDA",
    "TSLA":  "US.TSLA",
    "META":  "US.META",
    "AMZN":  "US.AMZN",
    "GOOGL": "US.GOOGL",
    "AAPL":  "US.AAPL",
    "MSFT":  "US.MSFT",
}

# WHITELIST: 全戦術で取引許可される銘柄。ここに含まれない銘柄はスキップ。
# blacklist方式（EXCLUDED_SYMBOLS）は廃止。漏れが致命的なため。
ALLOWED_SYMBOLS: frozenset[str] = frozenset(SYMBOL_META.keys())


def get_meta(symbol: str) -> dict:
    """銘柄のメタ情報を返す。不明銘柄は空dictを返す（エラーにしない）。"""
    return SYMBOL_META.get(symbol, {})


def get_strike_interval(symbol: str) -> float:
    """銘柄のstrike刻み幅を返す。不明銘柄は1.0。"""
    return float(SYMBOL_META.get(symbol, {}).get("strike_interval", 1.0))


def get_option_root(symbol: str, use_0dte: bool = True) -> str:
    """銘柄のオプションコード root prefix を返す。

    Args:
        symbol:    "US..SPX" 等の futu 銘柄コード
        use_0dte:  True = 0DTE/Weekly用 (SPXWなど) / False = Monthly用

    Returns:
        "SPXW" / "SPY" / "QQQ" 等。不明銘柄はシンボルのUS.除去部分。
    """
    meta = SYMBOL_META.get(symbol, {})
    if use_0dte:
        return meta.get("option_root_0dte", symbol.replace("US.", "").replace(".", ""))
    return meta.get("option_root_monthly",
                    meta.get("option_root_0dte",
                             symbol.replace("US.", "").replace(".", "")))


def get_center_strike_tolerance(symbol: str) -> float:
    """center_strike に対する許容乖離率（小数）を返す。

    SPX は 0.10 (±10%)、SPY/ETFは 0.20 (±20%)、個別株は 0.25 (±25%)。
    SPX取引中にSPY strike ($710付近) が混入したら ±10%フィルタで即検知できる。
    """
    return float(SYMBOL_META.get(symbol, {}).get("center_strike_tolerance", 0.20))


def underlying_from_option_root(root: str) -> Optional[str]:
    """オプションコードのroot prefix から underlying 銘柄コードを返す。

    例: "SPXW" -> "US..SPX", "SPY" -> "US.SPY"
    不明の場合は None。
    """
    return _ROOT_TO_UNDERLYING.get(root)


def is_allowed(symbol: str) -> bool:
    """銘柄が WHITELIST に含まれるか確認する。"""
    return symbol in ALLOWED_SYMBOLS


def is_cash_settled(symbol: str) -> bool:
    """現金清算（欧州型）かどうかを返す。SPXはTrue、他はFalse。"""
    return SYMBOL_META.get(symbol, {}).get("settlement") == "cash_european"


def is_section_1256(symbol: str) -> bool:
    """Section 1256 税制優遇対象かどうかを返す。SPXはTrue。"""
    return bool(SYMBOL_META.get(symbol, {}).get("section_1256", False))
