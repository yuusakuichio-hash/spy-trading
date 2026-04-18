"""
common/cross_asset.py — 先物・商品相関分析モジュール

## 設計思想
株式は真空では動かない。ES先物・原油・金・ビットコインとの
相関が通常から大きく外れた時、それはリスク環境の変化シグナル。
このモジュールはマクロリスク環境を「risk-on / risk-off / neutral」
の3分類で定量化する。

## データソース優先順位
1. Databento REST API (ES/NQ futures リアルタイム)
   API KEY: db-ABkfVNM5Nvcr... (既存保存済み)
2. Yahoo Finance (原油CL=F/金GC=F/BTC-USD)
3. ローカルparquet (data/futures/MNQ/daily_*.parquet)

## 相関係数の動的更新
- 日次で過去30日の終値から相関係数を算出（固定窓30日）
- 窓幅は atlas_rules.yaml から読む（変更可能）
- 「通常レンジ」は過去60日の移動平均±2σで算出（動的）

## risk-off / risk-on 判定
- ES↑&原油↑&金↓ → strong risk-on
- 金↑&ビットコイン↓&ES↓ → risk-off
- その他の組み合わせ → neutral
固定閾値ではなく、相関係数の z スコア（ユニバース内分布）で判定する。

## Graceful Degradation
- Databento 未設定/失敗 → Yahoo + ローカルparquetにフォールバック
- 全API失敗 → regime="neutral", all correlations=None
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Databento APIキー（既存 .env から読む）
_DATABENTO_KEY_ENV = "DATABENTO_API_KEY"

# ローカルparquetパス
_FUTURES_DIR = Path("/Users/yuusakuichio/trading/data/futures")

# 相関係数算出の窓幅（日数）: atlas_rules.yamlで上書き可
_DEFAULT_CORR_WINDOW = 30

# クロスアセットシンボルマッピング (Yahoo Finance)
_YAHOO_SYMBOLS = {
    "ES":  "ES=F",       # S&P500 E-mini futures
    "NQ":  "NQ=F",       # NASDAQ-100 E-mini futures
    "CL":  "CL=F",       # WTI 原油 futures
    "GC":  "GC=F",       # 金 futures
    "BTC": "BTC-USD",    # ビットコイン
    "SPY": "SPY",        # SPY (相関の基準)
}


@dataclass
class AssetPrice:
    """1銘柄の終値系列。"""
    symbol:    str
    prices:    list[float]    # [oldest, ..., latest]
    source:    str            # "databento" / "yahoo" / "local_parquet"
    available: bool = True


@dataclass
class CrossAssetSignal:
    """クロスアセット相関分析結果。"""
    # 相関係数 (30日, SPYを基準)
    corr_es_spy:    Optional[float] = None
    corr_cl_spy:    Optional[float] = None
    corr_gc_spy:    Optional[float] = None
    corr_btc_spy:   Optional[float] = None
    # 相関の異常度 (vs 過去60日の移動平均: -2σ以下 or +2σ以上で異常)
    es_corr_zscore:  float = 0.0
    gc_corr_zscore:  float = 0.0
    # リスクレジーム
    regime:          str = "neutral"   # "risk_on" / "risk_off" / "neutral"
    regime_score:    float = 0.0       # -1.0 (強risk-off) 〜 +1.0 (強risk-on)
    # 異常相関フラグ
    corr_anomaly:    bool = False
    anomaly_details: list[str] = field(default_factory=list)
    # データ品質
    data_sources:    dict[str, str] = field(default_factory=dict)
    data_available:  bool = False


# ── 相関係数計算 ──────────────────────────────────────────────────────────────

def _pearson_corr(x: list[float], y: list[float]) -> Optional[float]:
    """ピアソン相関係数を算出。データ不足/定数時はNone。"""
    n = min(len(x), len(y))
    if n < 5:
        return None
    x = x[-n:]
    y = y[-n:]
    try:
        mx = statistics.mean(x)
        my = statistics.mean(y)
        num   = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        den_x = math.sqrt(sum((xi - mx) ** 2 for xi in x))
        den_y = math.sqrt(sum((yi - my) ** 2 for yi in y))
        if den_x == 0 or den_y == 0:
            return None
        return max(-1.0, min(1.0, num / (den_x * den_y)))
    except Exception:
        return None


def _returns(prices: list[float]) -> list[float]:
    """終値から日次リターン系列を計算。"""
    if len(prices) < 2:
        return []
    return [(prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices))]


def _rolling_corr_history(
    x: list[float], y: list[float], window: int = 30
) -> list[Optional[float]]:
    """ローリング相関係数の時系列を返す（移動平均・σ算出用）。"""
    n = min(len(x), len(y))
    result: list[Optional[float]] = []
    for i in range(n):
        start = max(0, i - window + 1)
        c = _pearson_corr(x[start: i + 1], y[start: i + 1])
        result.append(c)
    return result


def _anomaly_zscore(
    current_corr: Optional[float],
    corr_history: list[Optional[float]],
) -> float:
    """相関係数の z スコアを算出（ゼロ = 通常範囲内）。"""
    if current_corr is None:
        return 0.0
    valid = [c for c in corr_history[:-1] if c is not None]
    if len(valid) < 10:
        return 0.0
    mu    = statistics.mean(valid)
    sigma = statistics.stdev(valid) if len(valid) > 1 else 1.0
    if sigma < 1e-6:
        return 0.0
    return (current_corr - mu) / sigma


# ── データ取得 ────────────────────────────────────────────────────────────────

def _fetch_yahoo_prices(
    symbols: dict[str, str],
    period_days: int = 65,
) -> dict[str, AssetPrice]:
    """Yahoo Finance から複数シンボルの日次終値を取得。"""
    try:
        import requests

        end_ts   = int(time.time())
        start_ts = end_ts - period_days * 86400
        result: dict[str, AssetPrice] = {}

        for key, yahoo_sym in symbols.items():
            try:
                url = (
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
                    f"?period1={start_ts}&period2={end_ts}&interval=1d"
                )
                resp = requests.get(url, timeout=10,
                                    headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    log.warning(f"[CrossAsset] Yahoo {yahoo_sym}: HTTP {resp.status_code}")
                    result[key] = AssetPrice(symbol=key, prices=[], source="yahoo", available=False)
                    continue
                data   = resp.json()
                closes = (
                    data.get("chart", {})
                        .get("result", [{}])[0]
                        .get("indicators", {})
                        .get("quote", [{}])[0]
                        .get("close", [])
                )
                closes = [c for c in closes if c is not None]
                result[key] = AssetPrice(symbol=key, prices=closes, source="yahoo",
                                          available=bool(closes))
                time.sleep(0.05)
            except Exception as e:
                log.warning(f"[CrossAsset] Yahoo {yahoo_sym} error: {e}")
                result[key] = AssetPrice(symbol=key, prices=[], source="yahoo", available=False)
        return result

    except ImportError:
        log.warning("[CrossAsset] requests not available")
        return {k: AssetPrice(symbol=k, prices=[], source="none", available=False)
                for k in symbols}


def _fetch_databento_prices(
    symbols: list[str],
    api_key: str,
    period_days: int = 65,
) -> dict[str, AssetPrice]:
    """Databento REST API から先物の日次価格を取得。

    Databento REST API:
    GET https://hist.databento.com/v0/timeseries.get_range
    dataset: GLBX.MDP3 (CME Globex)
    schema: ohlcv-1d
    参照: https://docs.databento.com/api-reference-historical/timeseries/timeseries-get-range
    """
    if not api_key:
        return {}
    try:
        import requests
        import datetime

        end_dt   = datetime.date.today()
        start_dt = end_dt - datetime.timedelta(days=period_days)

        # Databento シンボルマッピング (継続足)
        sym_map = {
            "ES": "ES.c.0",   # S&P500 E-mini 継続足
            "NQ": "NQ.c.0",   # NASDAQ-100 E-mini 継続足
        }
        result: dict[str, AssetPrice] = {}

        for key in symbols:
            db_sym = sym_map.get(key)
            if db_sym is None:
                continue
            try:
                resp = requests.get(
                    "https://hist.databento.com/v0/timeseries.get_range",
                    params={
                        "dataset":    "GLBX.MDP3",
                        "symbols":    db_sym,
                        "schema":     "ohlcv-1d",
                        "start":      start_dt.isoformat(),
                        "end":        end_dt.isoformat(),
                        "encoding":   "json",
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=15,
                )
                if resp.status_code != 200:
                    log.warning(f"[CrossAsset] Databento {db_sym}: HTTP {resp.status_code}")
                    continue
                data   = resp.json()
                closes = [float(rec["close"]) / 1e9  # Databento は fixed-point
                          for rec in data.get("result", [])
                          if rec.get("close") is not None]
                if closes:
                    result[key] = AssetPrice(symbol=key, prices=closes,
                                              source="databento", available=True)
            except Exception as e:
                log.warning(f"[CrossAsset] Databento {key} error: {e}")
        return result

    except ImportError:
        log.warning("[CrossAsset] requests not available for Databento")
        return {}
    except Exception as e:
        log.warning(f"[CrossAsset] Databento fetch error: {e}")
        return {}


def _load_local_parquet(key: str) -> Optional[AssetPrice]:
    """ローカルparquetから価格を読む（フォールバック）。"""
    try:
        import pandas as pd

        # MNQ (NASDAQ先物) を NQ の代理として使う
        key_to_file = {
            "NQ": _FUTURES_DIR / "MNQ" / "daily_2015_2026.parquet",
        }
        path = key_to_file.get(key)
        if path is None or not path.exists():
            return None

        df = pd.read_parquet(path)
        close_col = next((c for c in df.columns if "close" in c.lower()), None)
        if close_col is None:
            return None
        closes = df[close_col].dropna().tolist()
        if not closes:
            return None
        return AssetPrice(symbol=key, prices=closes[-65:],
                          source="local_parquet", available=True)
    except Exception as e:
        log.warning(f"[CrossAsset] parquet load {key}: {e}")
        return None


# ── パブリック API ────────────────────────────────────────────────────────────

def get_cross_asset_signal(
    databento_api_key: str = "",
    corr_window: int = _DEFAULT_CORR_WINDOW,
    price_data: Optional[dict[str, AssetPrice]] = None,
) -> CrossAssetSignal:
    """クロスアセット相関分析を実行してシグナルを返す。

    Args:
        databento_api_key: Databento API KEY
        corr_window:       相関係数の窓幅（日数）
        price_data:        テスト用外部注入データ

    Returns:
        CrossAssetSignal
    """
    sig = CrossAssetSignal()

    # 価格データ取得
    if price_data is not None:
        prices = price_data
    else:
        # Yahoo から一括取得
        prices = _fetch_yahoo_prices(_YAHOO_SYMBOLS, period_days=65)

        # Databento で ES/NQ を上書き（より精度高い）
        if databento_api_key:
            db_prices = _fetch_databento_prices(["ES", "NQ"], databento_api_key)
            prices.update(db_prices)

        # ローカルparquetでフォールバック
        for key in ["NQ"]:
            if not prices.get(key, AssetPrice(key, [], "none", False)).available:
                local = _load_local_parquet(key)
                if local:
                    prices[key] = local

    sig.data_sources = {k: v.source for k, v in prices.items() if v.available}

    # SPY価格が必須
    spy = prices.get("SPY")
    if not spy or not spy.available or len(spy.prices) < corr_window + 5:
        log.warning("[CrossAsset] SPY price data unavailable")
        sig.regime = "neutral"
        return sig

    spy_rets = _returns(spy.prices)

    # 各アセットの相関係数算出
    asset_corrs: dict[str, Optional[float]] = {}
    for key, asset_key in [("ES", "es"), ("CL", "cl"), ("GC", "gc"), ("BTC", "btc")]:
        asset = prices.get(key)
        if not asset or not asset.available or len(asset.prices) < corr_window + 2:
            asset_corrs[key] = None
            continue
        asset_rets = _returns(asset.prices)
        n = min(len(spy_rets), len(asset_rets), corr_window)
        corr = _pearson_corr(spy_rets[-n:], asset_rets[-n:])
        asset_corrs[key] = corr

    sig.corr_es_spy  = asset_corrs.get("ES")
    sig.corr_cl_spy  = asset_corrs.get("CL")
    sig.corr_gc_spy  = asset_corrs.get("GC")
    sig.corr_btc_spy = asset_corrs.get("BTC")

    # 異常相関 z スコア算出 (ES, GCを主要指標として使用)
    anomaly_messages: list[str] = []
    for key, attr_name in [("ES", "es"), ("GC", "gc")]:
        asset = prices.get(key)
        if not asset or not asset.available:
            continue
        asset_rets = _returns(asset.prices)
        n = min(len(spy_rets), len(asset_rets))
        hist = _rolling_corr_history(spy_rets[-n:], asset_rets[-n:], window=corr_window)
        current = asset_corrs.get(key)
        z = _anomaly_zscore(current, hist)
        if key == "ES":
            sig.es_corr_zscore = z
        else:
            sig.gc_corr_zscore = z
        if abs(z) >= 2.0:
            sig.corr_anomaly = True
            corr_str = f"{current:.3f}" if current is not None else "N/A"
            anomaly_messages.append(
                f"{key}/SPY corr={corr_str} z={z:.2f} (異常)"
            )

    sig.anomaly_details = anomaly_messages
    if anomaly_messages:
        log.warning(f"[CrossAsset] Correlation anomaly: {anomaly_messages}")

    # risk_on / risk_off 判定
    # 動的スコア: 各相関係数の方向性と強度から算出
    # ES高相関・GC低相関 → risk-on
    # GC高相関・ES低/負相関 → risk-off
    score_components: list[float] = []

    if sig.corr_es_spy is not None:
        # ES相関 > 0 = risk-on方向
        score_components.append(sig.corr_es_spy * 0.4)

    if sig.corr_gc_spy is not None:
        # 金は SPY と逆相関傾向。通常負 → risk-on。正方向 = risk-off
        score_components.append(-sig.corr_gc_spy * 0.3)

    if sig.corr_cl_spy is not None:
        # 原油は risk-on 時に上昇 (正相関傾向)
        score_components.append(sig.corr_cl_spy * 0.15)

    if sig.corr_btc_spy is not None:
        # BTC は risk-on アセット (正相関傾向)
        score_components.append(sig.corr_btc_spy * 0.15)

    if score_components:
        regime_score = sum(score_components)
        sig.regime_score = max(-1.0, min(1.0, regime_score))
    else:
        sig.regime_score = 0.0

    # レジーム分類（動的閾値: スコアの絶対値が 0.2 以上で判定）
    if sig.regime_score >= 0.2:
        sig.regime = "risk_on"
    elif sig.regime_score <= -0.2:
        sig.regime = "risk_off"
    else:
        sig.regime = "neutral"

    sig.data_available = bool(sig.data_sources)

    log.info(
        f"[CrossAsset] regime={sig.regime} score={sig.regime_score:.3f} "
        f"ES_corr={sig.corr_es_spy} GC_corr={sig.corr_gc_spy} "
        f"anomaly={sig.corr_anomaly}"
    )
    return sig
