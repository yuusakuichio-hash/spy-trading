"""common/orderflow_analysis.py -- GEX/DIX analysis via free CBOE public API

## 設計思想
Gamma Exposure (GEX) とDark Indexプロキシ (DIX) を無料のCBOE公開データで計算し、
既存VIX/IVR/VRPバイアスに追加シグナルとして戦術選択精度を向上させる。

## GEXとは
- Gamma Exposure: 市場参加者が保有するオプションのガンマ合計
- 正GEX: コールOI優勢、市場メーカーがショートガンマ -> スポット変動を抑制
- 負GEX: ショートプット/コールが多く、市場メーカーがロングガンマ -> 変動を増幅
- GEX > 0: 低ボラ環境 -> IC/CS売り有利
- GEX < 0: 高ボラ環境 -> ORB買い/ストラドル有利

## DIXプロキシとは
- Dark Pool Index本来はダークプール出来高比率（有料データ必須）
- 無料代替: プット出来高 / 全出来高 = put_volume_ratio
  -> 低い（買い意欲）= 強気、高い（ヘッジ需要）= 弱気
- 本来のDIXとは異なるが、機関のヘッジ動向の概算として有用

## データソース
- CBOE公開API: https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json
- 15分遅延データ（無料・認証不要）
- 対応銘柄: SPX(_SPX), SPY, QQQ, IWM, AAPL, MSFT, TSLA, NVDA, AMZN, META, GOOGL

## Graceful Degradation
- API取得失敗 -> neutral GEX/DIX (gex_bias=0.0)
- 部分データ -> 取得できた分のみで算出
- タイムアウト5秒

## 使い方
    from common.orderflow_analysis import get_orderflow_signal, OrderflowSignal
    sig = get_orderflow_signal("SPY")
    print(sig.gex_bias, sig.dix_proxy, sig.combined_bias)
"""

from __future__ import annotations

import json
import logging
import math
import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# CBOE API設定
_CBOE_BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
_CBOE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.cboe.com/",
}
_REQUEST_TIMEOUT_SEC = 5

# CBOEシンボルマッピング（インデックスはアンダースコア付き）
_SYMBOL_MAP: dict[str, str] = {
    "SPX":  "_SPX",
    "SPXW": "_SPX",
    "SPY":  "SPY",
    "QQQ":  "QQQ",
    "IWM":  "IWM",
    "AAPL": "AAPL",
    "MSFT": "MSFT",
    "TSLA": "TSLA",
    "NVDA": "NVDA",
    "AMZN": "AMZN",
    "META": "META",
    "GOOGL": "GOOGL",
}

# GEX正規化基準値（銘柄別の典型的GEX絶対値）
# 銘柄スケールに合わせた初期値（tanh正規化でこの値付近で±0.76になる）
_GEX_NORM_DEFAULTS: dict[str, float] = {
    "_SPX": 5_000_000_000.0,   # SPXは規模が大きい (5B)
    "SPY":  2_000_000_000.0,   # SPY (2B)
    "QQQ":  1_000_000_000.0,   # QQQ (1B)
    "IWM":  500_000_000.0,     # IWM (500M)
}
_GEX_NORM_DEFAULT_FALLBACK = 1_000_000_000.0  # 未登録銘柄用

# 日次ログファイル
_ORDERFLOW_LOG_PATH = Path("/Users/yuusakuichio/trading/data/orderflow_daily.jsonl")


# -- データクラス ----------------------------------------------------------------

@dataclass
class OptionRecord:
    """CBOEから取得した1ストライク分のオプションデータ。"""
    symbol:        str
    expiry:        str      # "20260420" 形式
    strike:        float
    right:         str      # "C" or "P"
    volume:        float
    open_interest: float
    gamma:         float
    delta:         float


@dataclass
class OrderflowSignal:
    """GEX/DIX分析結果。strategy_selectorに渡すバイアス値を含む。"""
    symbol: str

    # GEX (Gamma Exposure)
    gex_total:     float = 0.0   # Sigma(OI x gamma x 100 x spot): 正=低ボラ環境
    gex_call:      float = 0.0   # コールのみのGEX
    gex_put:       float = 0.0   # プットのみのGEX（符号反転前）
    gex_bias:      float = 0.0   # -1.0(負GEX=高ボラ) ~ +1.0(正GEX=低ボラ)

    # DIXプロキシ（プット出来高比率）
    call_volume:   float = 0.0
    put_volume:    float = 0.0
    dix_proxy:     float = 0.5   # put_volume / total_volume: 高い=弱気
    dix_bias:      float = 0.0   # -1.0(弱気) ~ +1.0(強気)

    # 0DIV (zero-strike volume): ATM近辺の出来高集中度
    zero_strike_vol:   float = 0.0   # ATMの+-2%以内のストライク出来高合計
    zero_strike_ratio: float = 0.0   # total_volumeに対する比率

    # 総合バイアス（GEX + DIX加重平均）
    combined_bias: float = 0.0   # -1.0(弱気/高ボラ) ~ +1.0(強気/低ボラ)
    confidence:    float = 0.0   # 0.0~1.0
    data_available: bool = False

    # メタ情報
    spot_price:    float = 0.0
    strikes_used:  int = 0
    timestamp_utc: str = ""

    def volatility_regime(self) -> str:
        """GEXからボラティリティ環境を返す。"""
        if self.gex_total > 0:
            return "low_vol"   # 正GEX: マーケットメーカーがスポットを固定
        elif self.gex_total < 0:
            return "high_vol"  # 負GEX: 変動増幅
        return "neutral"

    def market_direction(self) -> str:
        """DIXプロキシから市場方向性バイアスを返す。"""
        if self.dix_proxy < 0.40:
            return "bullish"   # プット出来高少ない = 強気
        elif self.dix_proxy > 0.60:
            return "bearish"   # プット出来高多い = 弱気/ヘッジ活発
        return "neutral"

    def strategy_hint(self) -> str:
        """GEX/DIXの組み合わせから推奨戦術ヒントを返す。"""
        vol = self.volatility_regime()
        direction = self.market_direction()
        if vol == "low_vol" and direction == "bullish":
            return "ic_sell"        # 低ボラ+強気 -> アイアンコンドル売り
        elif vol == "low_vol" and direction == "bearish":
            return "cs_sell_put"    # 低ボラ+弱気 -> プットCS売り
        elif vol == "high_vol" and direction == "bullish":
            return "orb_buy"        # 高ボラ+強気 -> ORB買い方向
        elif vol == "high_vol" and direction == "bearish":
            return "straddle_buy"   # 高ボラ+弱気 -> ストラドル買い
        return "no_preference"


# -- CBOE APIクライアント --------------------------------------------------------

def _fetch_cboe_options(symbol: str) -> tuple[float, list[dict]]:
    """CBOEから遅延オプションチェーンを取得する。

    Args:
        symbol: "SPY", "SPX", "QQQ" 等

    Returns:
        (spot_price, list of option dicts)
        失敗時は (0.0, [])
    """
    try:
        import requests

        cboe_sym = _SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        url = _CBOE_BASE_URL.format(symbol=cboe_sym)
        resp = requests.get(
            url,
            headers=_CBOE_HEADERS,
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            log.debug(f"[Orderflow] CBOE {symbol}: HTTP {resp.status_code}")
            return 0.0, []

        payload = resp.json()
        data = payload.get("data", {})
        spot = float(data.get("current_price", 0.0) or 0.0)
        options = data.get("options", [])
        log.info(f"[Orderflow] CBOE {symbol}: spot={spot:.2f} options={len(options)}")
        return spot, options

    except Exception as e:
        log.debug(f"[Orderflow] CBOE fetch error for {symbol}: {e}")
        return 0.0, []


def _parse_option_code(code: str) -> tuple[str, str, float]:
    """オプションコード文字列から満期・right・ストライクを解析する。

    例: "SPX260515C00200000" -> ("20260515", "C", 2000.00)
    例: "SPY260420C00500000" -> ("20260420", "C", 500.00)

    Args:
        code: CBOEオプションシンボル文字列

    Returns:
        (expiry "YYYYMMDD", right "C"/"P", strike float)
        解析失敗時は ("", "", 0.0)
    """
    # パターン: <ROOT><YY><MM><DD><C/P><STRIKE8桁>
    m = re.match(r"^[A-Z_]+(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", code)
    if not m:
        return "", "", 0.0
    yy, mm, dd, right, strike_raw = m.groups()
    expiry = f"20{yy}{mm}{dd}"
    strike = int(strike_raw) / 1000.0
    return expiry, right, strike


def _parse_options(raw_options: list[dict], spot: float) -> list[OptionRecord]:
    """CBOEの生オプションリストをOptionRecordリストに変換する。

    Args:
        raw_options: CBOEのoptions配列
        spot: 原資産スポット価格

    Returns:
        list[OptionRecord]
    """
    records: list[OptionRecord] = []
    symbol_root = ""

    for opt in raw_options:
        try:
            code = opt.get("option", "")
            if not code:
                continue

            # シンボルルート抽出（最初の1件から）
            if not symbol_root:
                m = re.match(r"^(_?[A-Z]+)\d{6}[CP]", code)
                if m:
                    symbol_root = m.group(1).lstrip("_")

            expiry, right, strike = _parse_option_code(code)
            if not expiry or right not in ("C", "P") or strike <= 0:
                continue

            volume = float(opt.get("volume", 0.0) or 0.0)
            oi     = float(opt.get("open_interest", 0.0) or 0.0)
            gamma  = float(opt.get("gamma", 0.0) or 0.0)
            delta  = float(opt.get("delta", 0.0) or 0.0)

            records.append(OptionRecord(
                symbol=symbol_root or "?",
                expiry=expiry,
                strike=strike,
                right=right,
                volume=volume,
                open_interest=oi,
                gamma=gamma,
                delta=delta,
            ))
        except Exception:
            continue

    return records


# -- GEX計算 --------------------------------------------------------------------

def _compute_gex(records: list[OptionRecord], spot: float) -> tuple[float, float, float]:
    """GEX（Gamma Exposure）を計算する。

    GEX = Sigma_call(OI x gamma x 100 x spot) - Sigma_put(OI x gamma x 100 x spot)

    コールのGEXは正（マーケットメーカーがデルタヘッジで買い支え）、
    プットのGEXは負（マーケットメーカーがデルタヘッジで売り圧力）。

    Args:
        records: OptionRecordリスト
        spot:    スポット価格

    Returns:
        (gex_total, gex_call, gex_put_raw)
        gex_put_rawは符号反転前の絶対値
    """
    gex_call = 0.0
    gex_put  = 0.0
    multiplier = 100.0  # 1コントラクト = 100株

    for r in records:
        if r.open_interest <= 0 or r.gamma <= 0:
            continue
        gex_contrib = r.open_interest * r.gamma * multiplier * spot
        if r.right == "C":
            gex_call += gex_contrib
        elif r.right == "P":
            gex_put += gex_contrib  # 符号反転は gex_total 計算時

    gex_total = gex_call - gex_put
    return gex_total, gex_call, gex_put


def _compute_gex_bias(gex_total: float, cboe_symbol: str) -> float:
    """GEX絶対値をバイアス値 [-1.0, +1.0] に正規化する。

    正GEX = 低ボラ = バイアス +1 方向
    負GEX = 高ボラ = バイアス -1 方向

    Args:
        gex_total: GEX合計値
        cboe_symbol: CBOEシンボル名

    Returns:
        float in [-1.0, +1.0]
    """
    norm = _GEX_NORM_DEFAULTS.get(cboe_symbol, _GEX_NORM_DEFAULT_FALLBACK)
    # tanh正規化: 絶対値が norm を超えたあたりで飽和
    bias = math.tanh(gex_total / norm)
    return max(-1.0, min(1.0, bias))


# -- DIXプロキシ計算 ------------------------------------------------------------

def _compute_dix_proxy(records: list[OptionRecord]) -> tuple[float, float, float]:
    """DIXプロキシ（プット出来高比率）を計算する。

    put_volume_ratio = put_volume / (call_volume + put_volume)

    本来のDIXはダークプール出来高比率だが、無料代替として
    プット出来高比率を使用。ヘッジ需要の増減を捉える。

    Returns:
        (call_volume, put_volume, put_ratio)
    """
    call_vol = sum(r.volume for r in records if r.right == "C")
    put_vol  = sum(r.volume for r in records if r.right == "P")
    total    = call_vol + put_vol
    if total <= 0:
        return 0.0, 0.0, 0.5
    ratio = put_vol / total
    return call_vol, put_vol, ratio


def _compute_dix_bias(put_ratio: float) -> float:
    """put_ratio をバイアス [-1.0, +1.0] に変換する。

    put_ratio = 0.50 -> neutral (0.0)
    put_ratio = 0.30 -> bullish (+0.4)
    put_ratio = 0.70 -> bearish (-0.4)

    Args:
        put_ratio: 0.0~1.0

    Returns:
        float in [-1.0, +1.0]
    """
    # 0.5基準で線形変換: (0.5 - ratio) x 2 で [-1, +1] に
    bias = (0.5 - put_ratio) * 2.0
    return max(-1.0, min(1.0, bias))


# -- 0DIV（ゼロストライクボリューム）計算 ----------------------------------------

def _compute_zero_strike_vol(
    records: list[OptionRecord],
    spot: float,
    range_pct: float = 0.02,
) -> tuple[float, float]:
    """ATM近辺+-2%のゼロストライク出来高を計算する。

    ATM付近の出来高急増はボラ急変の先行指標になる。

    Args:
        records:   OptionRecordリスト
        spot:      スポット価格
        range_pct: ATM+-何%以内を「ゼロストライク」とするか

    Returns:
        (zero_strike_vol, zero_strike_ratio)
    """
    if spot <= 0:
        return 0.0, 0.0

    lower = spot * (1.0 - range_pct)
    upper = spot * (1.0 + range_pct)
    near_atm = [r for r in records if lower <= r.strike <= upper]

    zero_vol = sum(r.volume for r in near_atm)
    total_vol = sum(r.volume for r in records)
    ratio = zero_vol / total_vol if total_vol > 0 else 0.0
    return zero_vol, ratio


# -- ログ書き込み ---------------------------------------------------------------

def _log_to_file(sig: OrderflowSignal) -> None:
    """日次ログファイルに1行JSONとして追記する。"""
    try:
        _ORDERFLOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts":                sig.timestamp_utc,
            "symbol":            sig.symbol,
            "spot":              round(sig.spot_price, 4),
            "gex_total":         round(sig.gex_total, 2),
            "gex_call":          round(sig.gex_call, 2),
            "gex_put":           round(sig.gex_put, 2),
            "gex_bias":          round(sig.gex_bias, 4),
            "call_volume":       round(sig.call_volume, 0),
            "put_volume":        round(sig.put_volume, 0),
            "dix_proxy":         round(sig.dix_proxy, 4),
            "dix_bias":          round(sig.dix_bias, 4),
            "zero_strike_vol":   round(sig.zero_strike_vol, 0),
            "zero_strike_ratio": round(sig.zero_strike_ratio, 4),
            "combined_bias":     round(sig.combined_bias, 4),
            "confidence":        round(sig.confidence, 4),
            "strikes_used":      sig.strikes_used,
            "vol_regime":        sig.volatility_regime(),
            "mkt_direction":     sig.market_direction(),
            "strategy_hint":     sig.strategy_hint(),
        }
        with open(_ORDERFLOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning(f"[Orderflow] log write failed: {e}")


# -- パブリック API -------------------------------------------------------------

def get_orderflow_signal(
    symbol: str,
    log_to_file: bool = True,
    _raw_options: Optional[list[dict]] = None,
    _spot_override: Optional[float] = None,
) -> OrderflowSignal:
    """GEX/DIX分析シグナルを返す。

    Args:
        symbol:         銘柄ティッカー ("SPX", "SPY", "QQQ" 等)
        log_to_file:    True でdata/orderflow_daily.jsonlに追記
        _raw_options:   テスト用外部注入データ。指定時はCBOE APIをスキップ
        _spot_override: テスト用スポット価格オーバーライド

    Returns:
        OrderflowSignal
    """
    cboe_sym = _SYMBOL_MAP.get(symbol.upper(), symbol.upper())
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    neutral = OrderflowSignal(
        symbol=symbol,
        data_available=False,
        timestamp_utc=ts,
    )

    # データ取得
    if _raw_options is not None:
        # テスト用: 外部注入データを使用
        spot = _spot_override or 500.0
        raw = _raw_options
    else:
        spot, raw = _fetch_cboe_options(symbol)

    if not raw or spot <= 0:
        log.info(f"[Orderflow] {symbol}: no data -> neutral signal")
        return neutral

    # パース
    records = _parse_options(raw, spot)
    if not records:
        log.info(f"[Orderflow] {symbol}: parse yielded 0 records -> neutral")
        return neutral

    # GEX計算
    gex_total, gex_call, gex_put = _compute_gex(records, spot)
    gex_bias = _compute_gex_bias(gex_total, cboe_sym)

    # DIXプロキシ計算
    call_vol, put_vol, dix_proxy = _compute_dix_proxy(records)
    dix_bias = _compute_dix_bias(dix_proxy)

    # 0DIV計算
    zero_vol, zero_ratio = _compute_zero_strike_vol(records, spot)

    # 総合バイアス: GEX 0.6 + DIX 0.4 加重平均
    combined = gex_bias * 0.6 + dix_bias * 0.4
    combined = max(-1.0, min(1.0, combined))

    # 信頼度: ストライク数に比例 (500以上で 1.0)
    confidence = min(1.0, len(records) / 500.0)

    sig = OrderflowSignal(
        symbol=symbol,
        gex_total=gex_total,
        gex_call=gex_call,
        gex_put=gex_put,
        gex_bias=gex_bias,
        call_volume=call_vol,
        put_volume=put_vol,
        dix_proxy=dix_proxy,
        dix_bias=dix_bias,
        zero_strike_vol=zero_vol,
        zero_strike_ratio=zero_ratio,
        combined_bias=combined,
        confidence=confidence,
        data_available=True,
        spot_price=spot,
        strikes_used=len(records),
        timestamp_utc=ts,
    )

    log.info(
        f"[Orderflow] {symbol}: spot={spot:.2f} "
        f"GEX={gex_total:.2e} gex_bias={gex_bias:.3f} "
        f"DIX={dix_proxy:.3f} dix_bias={dix_bias:.3f} "
        f"combined={combined:.3f} conf={confidence:.2f} "
        f"hint={sig.strategy_hint()}"
    )

    if log_to_file:
        _log_to_file(sig)

    return sig


def get_orderflow_signals(
    symbols: list[str],
    log_to_file: bool = True,
) -> dict[str, OrderflowSignal]:
    """複数銘柄のOrderflowSignalをまとめて返す。"""
    return {sym: get_orderflow_signal(sym, log_to_file=log_to_file) for sym in symbols}


def orderflow_to_bias(sig: OrderflowSignal) -> dict[str, float]:
    """OrderflowSignalをstrategy_selectorのバイアス辞書形式に変換する。

    strategy_selectorに渡す際のアダプター関数。

    Returns:
        {
            "gex_bias":       float,  # GEXバイアス
            "dix_bias":       float,  # DIXプロキシバイアス
            "orderflow_bias": float,  # 総合バイアス
            "vol_regime":     str,    # "low_vol" / "high_vol" / "neutral"
        }
    """
    return {
        "gex_bias":       sig.gex_bias,
        "dix_bias":       sig.dix_bias,
        "orderflow_bias": sig.combined_bias,
        "vol_regime":     sig.volatility_regime(),
    }
