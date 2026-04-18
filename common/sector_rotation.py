"""
common/sector_rotation.py — セクターローテーション分析モジュール

## 設計思想
優秀なトレーダーは毎朝、どのセクターが資金を集めているかを確認する。
強いセクター（leading）は個別銘柄のCS/IC売り向き、
弱いセクター（lagging）は銘柄選択から除外する。

## 分析指標
- 5日・20日リターン（短期・中期モメンタム）
- 相対強弱指数 (RSI様のセクター相対スコア)
- leading/lagging判定（ユニバース内分布から動的算出）

## 対象セクター (11 SPDR ETFs)
  XLK  — Technology
  XLF  — Financials
  XLE  — Energy
  XLV  — Health Care
  XLY  — Consumer Discretionary
  XLP  — Consumer Staples
  XLI  — Industrials
  XLU  — Utilities
  XLRE — Real Estate
  XLB  — Materials
  XLC  — Communication Services

## 出力
  get_sector_scores() → dict[str, SectorScore]
  get_leading_sectors() → list[str]  (上位 1/3)
  get_lagging_sectors() → list[str]  (下位 1/3)

## Graceful Degradation
  API失敗時 → 全セクターを neutral (score=0.5) で返す
  部分失敗 → 取得できたセクターのみで相対スコアを算出
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── セクターETF一覧 ───────────────────────────────────────────────────────────
SECTOR_ETFS: list[str] = [
    "XLK", "XLF", "XLE", "XLV", "XLY",
    "XLP", "XLI", "XLU", "XLRE", "XLB", "XLC",
]

# セクター名マッピング
SECTOR_NAMES: dict[str, str] = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Health Care",
    "XLY":  "Consumer Discretionary",
    "XLP":  "Consumer Staples",
    "XLI":  "Industrials",
    "XLU":  "Utilities",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
    "XLC":  "Communication Services",
}


@dataclass
class SectorScore:
    """1セクターのスコアリング結果。"""
    symbol: str
    name: str
    ret_5d: Optional[float]   # 5日リターン (0.05 = +5%)
    ret_20d: Optional[float]  # 20日リターン
    score_5d: float = 0.5     # ユニバース内相対スコア (0〜1)
    score_20d: float = 0.5    # ユニバース内相対スコア (0〜1)
    composite_score: float = 0.5  # 5d×0.6 + 20d×0.4 の重み付き
    regime: str = "neutral"   # "leading" / "neutral" / "lagging"
    data_available: bool = True

    def __repr__(self) -> str:
        return (
            f"SectorScore({self.symbol}/{self.name[:6]}, "
            f"score={self.composite_score:.3f}, regime={self.regime})"
        )


# ── データ取得レイヤー（フォールバック付き）────────────────────────────────────

def _fetch_prices_yahoo(symbols: list[str], period_days: int = 25) -> dict[str, list[float]]:
    """Yahoo Finance から日次終値を取得。失敗時は空dictを返す。

    Returns:
        dict: {symbol: [close_oldest, ..., close_latest]}
    """
    try:
        import requests
        import datetime

        end_ts   = int(time.time())
        start_ts = end_ts - period_days * 86400

        result: dict[str, list[float]] = {}
        for sym in symbols:
            try:
                url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                    f"?period1={start_ts}&period2={end_ts}&interval=1d"
                )
                resp = requests.get(url, timeout=10,
                                    headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    log.warning(f"[SectorRotation] Yahoo {sym}: HTTP {resp.status_code}")
                    continue
                data = resp.json()
                closes = (
                    data.get("chart", {})
                        .get("result", [{}])[0]
                        .get("indicators", {})
                        .get("quote", [{}])[0]
                        .get("close", [])
                )
                closes = [c for c in closes if c is not None]
                if closes:
                    result[sym] = closes
            except Exception as e:
                log.warning(f"[SectorRotation] Yahoo {sym} error: {e}")
                continue
        return result
    except Exception as e:
        log.warning(f"[SectorRotation] _fetch_prices_yahoo failed: {e}")
        return {}


def _fetch_prices_finnhub(symbols: list[str], period_days: int = 25,
                           api_key: str = "") -> dict[str, list[float]]:
    """Finnhub /stock/candle から日次終値を取得。

    Returns:
        dict: {symbol: [close_oldest, ..., close_latest]}
    """
    if not api_key:
        return {}
    try:
        import requests
        import datetime

        end_ts   = int(time.time())
        start_ts = end_ts - period_days * 86400

        result: dict[str, list[float]] = {}
        for sym in symbols:
            try:
                resp = requests.get(
                    "https://finnhub.io/api/v1/stock/candle",
                    params={
                        "symbol":     sym,
                        "resolution": "D",
                        "from":       start_ts,
                        "to":         end_ts,
                        "token":      api_key,
                    },
                    timeout=10,
                )
                data = resp.json()
                if data.get("s") != "ok":
                    log.warning(f"[SectorRotation] Finnhub {sym}: status={data.get('s')}")
                    continue
                closes = [float(c) for c in data.get("c", []) if c is not None]
                if closes:
                    result[sym] = closes
                time.sleep(0.05)  # レートリミット回避 (60req/min)
            except Exception as e:
                log.warning(f"[SectorRotation] Finnhub {sym} error: {e}")
                continue
        return result
    except Exception as e:
        log.warning(f"[SectorRotation] _fetch_prices_finnhub failed: {e}")
        return {}


def _compute_return(prices: list[float], lookback: int) -> Optional[float]:
    """N日前からの終値リターンを算出。データ不足はNone。"""
    if len(prices) < lookback + 1:
        return None
    return (prices[-1] - prices[-lookback - 1]) / prices[-lookback - 1]


# ── スコアリング ──────────────────────────────────────────────────────────────

def _normalize_universe(values: list[Optional[float]]) -> list[float]:
    """値リストをユニバース内の相対位置 (0〜1) にノーマライズ。
    Noneは中央値 (0.5) として扱う。固定閾値なし。
    """
    valid = [v for v in values if v is not None]
    if len(valid) < 2:
        return [0.5 if v is None else 0.5 for v in values]
    mn, mx = min(valid), max(valid)
    if mx == mn:
        return [0.5 for _ in values]
    result = []
    for v in values:
        if v is None:
            result.append(0.5)
        else:
            result.append((v - mn) / (mx - mn))
    return result


def _assign_regime(scores: list[float], threshold_top: float,
                   threshold_bottom: float) -> list[str]:
    """スコアからレジームを割り当て。

    threshold_top/bottom は固定値ではなく、呼び出し元が動的に算出して渡す。
    """
    regimes = []
    for s in scores:
        if s >= threshold_top:
            regimes.append("leading")
        elif s <= threshold_bottom:
            regimes.append("lagging")
        else:
            regimes.append("neutral")
    return regimes


# ── パブリック API ────────────────────────────────────────────────────────────

def get_sector_scores(
    symbols: Optional[list[str]] = None,
    api_key: str = "",
    price_data: Optional[dict[str, list[float]]] = None,
) -> dict[str, SectorScore]:
    """セクターETFをスコアリングして返す。

    Args:
        symbols:    分析対象ETFリスト (Noneで全11セクター)
        api_key:    Finnhub APIキー（Yahooフォールバックがあるので必須ではない）
        price_data: テスト用に外部から価格データを注入できる。指定時はAPI不使用。

    Returns:
        dict: {symbol: SectorScore}
    """
    if symbols is None:
        symbols = SECTOR_ETFS

    # 価格データ取得（外部注入 > Yahoo > Finnhub の優先順）
    if price_data is not None:
        prices = price_data
    else:
        prices = _fetch_prices_yahoo(symbols, period_days=25)
        if not prices:
            log.info("[SectorRotation] Yahoo failed, trying Finnhub")
            prices = _fetch_prices_finnhub(symbols, period_days=25, api_key=api_key)

    # 各セクターのリターン算出
    rets_5d:  list[Optional[float]] = []
    rets_20d: list[Optional[float]] = []
    for sym in symbols:
        p = prices.get(sym, [])
        rets_5d.append(_compute_return(p, 5))
        rets_20d.append(_compute_return(p, 20))

    # ユニバース内相対ノーマライズ（固定閾値なし）
    scores_5d  = _normalize_universe(rets_5d)
    scores_20d = _normalize_universe(rets_20d)

    # 複合スコア算出（短期重視: 5d 60%, 20d 40%）
    composites = [0.6 * s5 + 0.4 * s20 for s5, s20 in zip(scores_5d, scores_20d)]

    # レジーム判定: 上位1/3=leading, 下位1/3=lagging（動的閾値）
    # データが十分ある場合のみ percentile 算出
    valid_composites = [c for c in composites if c is not None]
    if len(valid_composites) >= 3:
        sorted_c = sorted(valid_composites)
        n = len(sorted_c)
        # 上位1/3の下限 / 下位1/3の上限
        threshold_top    = sorted_c[int(n * 2 / 3)]
        threshold_bottom = sorted_c[int(n * 1 / 3)]
    else:
        threshold_top, threshold_bottom = 0.67, 0.33

    regimes = _assign_regime(composites, threshold_top, threshold_bottom)

    # SectorScore組み立て
    result: dict[str, SectorScore] = {}
    for i, sym in enumerate(symbols):
        result[sym] = SectorScore(
            symbol=sym,
            name=SECTOR_NAMES.get(sym, sym),
            ret_5d=rets_5d[i],
            ret_20d=rets_20d[i],
            score_5d=scores_5d[i],
            score_20d=scores_20d[i],
            composite_score=composites[i],
            regime=regimes[i],
            data_available=bool(prices.get(sym)),
        )

    leading = [s for s in symbols if result[s].regime == "leading"]
    lagging = [s for s in symbols if result[s].regime == "lagging"]
    log.info(
        f"[SectorRotation] leading={leading} lagging={lagging} "
        f"threshold_top={threshold_top:.3f} threshold_bottom={threshold_bottom:.3f}"
    )
    return result


def get_leading_sectors(scores: dict[str, SectorScore]) -> list[str]:
    """leading レジームのセクターシンボルリストを返す。"""
    return [sym for sym, sc in scores.items() if sc.regime == "leading"]


def get_lagging_sectors(scores: dict[str, SectorScore]) -> list[str]:
    """lagging レジームのセクターシンボルリストを返す。"""
    return [sym for sym, sc in scores.items() if sc.regime == "lagging"]


def sector_signal_for_symbol(
    symbol: str,
    sector_scores: dict[str, SectorScore],
    sector_map: Optional[dict[str, str]] = None,
) -> str:
    """銘柄が属するセクターのレジームを返す。

    Args:
        symbol:        銘柄ティッカー (例: "AAPL")
        sector_scores: get_sector_scores() の返値
        sector_map:    {symbol: sector_etf} の対応表（未指定は内部デフォルト使用）

    Returns:
        "leading" / "neutral" / "lagging" / "unknown"
    """
    _default_map: dict[str, str] = {
        # Technology
        "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK",
        "META": "XLC", "GOOGL": "XLC", "AMZN": "XLY",
        "TSLA": "XLY",
        # ETF自体は自分のセクター
        **{sym: sym for sym in SECTOR_ETFS},
    }
    if sector_map is None:
        sector_map = _default_map

    sector_etf = sector_map.get(symbol)
    if sector_etf is None:
        return "unknown"
    sc = sector_scores.get(sector_etf)
    if sc is None:
        return "unknown"
    return sc.regime
