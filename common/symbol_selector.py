"""
common/symbol_selector.py — 銘柄選択エンジン (Atlas マルチ銘柄対応)

## 設計思想
優秀なトレーダーは毎朝、各銘柄のIV環境・流動性・モメンタムを横断比較して
「今日どの銘柄で何の戦術を使うか」を決める。このモジュールはその判断を動的に
スコアリングで再現する。固定銘柄リスト・固定閾値は持たない。

## スコアリング指標（各0.0〜1.0 normalizeしてから重み付き加算）
  1. IVR (Implied Volatility Rank)       — 過去52週のIVの相対位置
  2. Volume Spike Ratio                  — 直近出来高 / 20日平均出来高
  3. Gap Magnitude                       — |前日比| / 過去20日のgap標準偏差
  4. Bid-Ask Spread Ratio                — (ask-bid)/mid × -1 (小さいほど高スコア)
  5. VIX Correlation                     — 対VIXの30日相関係数の絶対値

## 戦術別銘柄選好
  credit_spread  : 高IVR・低ガップ・高流動性を優先（プレミアム売り環境）
  straddle       : 高IVR・大きなgap・volume spike優先（ボラ買い環境）
  butterfly      : 低IVR・小さなgap・高流動性優先（レンジ圧縮環境）
  iron_condor    : credit_spreadと同系統だが流動性ウェイトを追加

## 決算期除外
  earnings_exclude=True で決算日 ±1営業日の銘柄を自動除外

## 出力
  select_symbols() → list[SymbolScore] (スコア降順)
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── デフォルト候補銘柄 ────────────────────────────────────────────────────────
# atlas_rules.yaml に symbol_universe キーがあればそちらを使う
# なければこのリストをフォールバックとして使う
_DEFAULT_UNIVERSE: list[str] = [
    "SPY", "QQQ", "IWM",
    "META", "AMZN", "GOOGL", "NVDA", "TSLA", "AAPL",
]

# ── 戦術別の重みプロファイル ─────────────────────────────────────────────────
# キー: 指標名、値: ウェイト (合計が1.0になるように正規化される)
_TACTIC_WEIGHTS: dict[str, dict[str, float]] = {
    "credit_spread": {
        "ivr":        0.40,
        "volume":     0.20,
        "gap":       -0.15,   # 負=gap大→スコアDOWN
        "liquidity":  0.15,
        "vix_corr":   0.10,
    },
    "iron_condor": {
        "ivr":        0.35,
        "volume":     0.15,
        "gap":       -0.20,
        "liquidity":  0.20,
        "vix_corr":   0.10,
    },
    "straddle": {
        "ivr":        0.25,
        "volume":     0.30,
        "gap":        0.25,   # 正=gap大→スコアUP
        "liquidity":  0.10,
        "vix_corr":   0.10,
    },
    "butterfly": {
        "ivr":       -0.20,   # 負=IVR低い方が好ましい
        "volume":     0.10,
        "gap":       -0.35,
        "liquidity":  0.25,
        "vix_corr":   0.10,
    },
}

# フォールバック: 未知の戦術名
_DEFAULT_WEIGHTS: dict[str, float] = {
    "ivr":       0.30,
    "volume":    0.20,
    "gap":       0.20,
    "liquidity": 0.20,
    "vix_corr":  0.10,
}


# ── データクラス ──────────────────────────────────────────────────────────────

@dataclass
class SymbolMetrics:
    """1銘柄の生指標データ。Noneは「取得不可」を表す。"""
    symbol: str
    ivr: Optional[float] = None              # 0〜100 (%)
    volume_spike_ratio: Optional[float] = None  # 直近出来高 / 20日平均出来高
    gap_abs_pct: Optional[float] = None     # |前日比| (0.0 = 変化なし, 0.05 = 5%)
    bid_ask_spread_pct: Optional[float] = None  # (ask-bid)/mid (0.01 = 1%スプレッド)
    vix_correlation: Optional[float] = None    # -1〜+1
    near_earnings: bool = False              # True=決算日 ±1営業日
    hist_gaps: list[float] = field(default_factory=list)  # 過去Nバーのgap絶対値


@dataclass
class SymbolScore:
    """スコアリング結果。select_symbols() の戻り値。"""
    symbol: str
    score: float                             # 重み付き合計スコア (正規化後)
    raw_scores: dict[str, float]             # 各指標の正規化スコア
    metrics: SymbolMetrics
    excluded: bool = False
    exclude_reason: str = ""

    def __repr__(self) -> str:
        return (
            f"SymbolScore({self.symbol}, score={self.score:.4f}, "
            f"excluded={self.excluded})"
        )


# ── ノーマライズ関数 ──────────────────────────────────────────────────────────

def _normalize_ivr(ivr: Optional[float], universe_ivrs: list[float]) -> float:
    """IVRをユニバース内での相対位置 (0.0〜1.0) にノーマライズ。

    固定閾値ゼロ。ユニバース全体のIVR分布からsoftmax様に算出する。
    データなし→0.5 (ニュートラル)
    """
    if ivr is None:
        return 0.5
    valid = [v for v in universe_ivrs if v is not None]
    if len(valid) < 2:
        # ユニバースデータ不足→生のIVRを0〜100基準で正規化
        return max(0.0, min(1.0, ivr / 100.0))
    mn, mx = min(valid), max(valid)
    if mx == mn:
        return 0.5
    return (ivr - mn) / (mx - mn)


def _normalize_volume(ratio: Optional[float], universe_ratios: list[float]) -> float:
    """Volume spike ratioをユニバース内相対位置にノーマライズ。"""
    if ratio is None:
        return 0.5
    valid = [v for v in universe_ratios if v is not None]
    if len(valid) < 2:
        # 最低ラインの1.0 (平均並み) を0.5とする対数スケール
        return max(0.0, min(1.0, math.log1p(max(0.0, ratio)) / math.log1p(5.0)))
    mn, mx = min(valid), max(valid)
    if mx == mn:
        return 0.5
    return (ratio - mn) / (mx - mn)


def _normalize_gap(gap_abs_pct: Optional[float], hist_gaps: list[float],
                   universe_gaps: list[float]) -> float:
    """Gap絶対値を正規化。

    hist_gapsが十分あれば自銘柄の過去標準偏差でzスコア化し、
    その後ユニバース内のzスコア分布でノーマライズ。
    """
    if gap_abs_pct is None:
        return 0.5
    # 自銘柄のzスコア
    valid_hist = [g for g in hist_gaps if g is not None and not math.isnan(g)]
    if len(valid_hist) >= 5:
        mu = statistics.mean(valid_hist)
        sigma = statistics.stdev(valid_hist) if len(valid_hist) > 1 else 1.0
        if sigma == 0:
            sigma = 1.0
        z = (gap_abs_pct - mu) / sigma
    else:
        # 過去データ不足→生の比率を使う
        z = gap_abs_pct

    # ユニバース内での相対位置
    valid_u = [v for v in universe_gaps if v is not None]
    if len(valid_u) < 2:
        # tanh で (-∞,+∞)→(-1,+1)→(0,1) に変換
        return (math.tanh(z / 2.0) + 1.0) / 2.0
    mn, mx = min(valid_u), max(valid_u)
    if mx == mn:
        return 0.5
    return max(0.0, min(1.0, (z - mn) / (mx - mn)))


def _normalize_liquidity(spread_pct: Optional[float],
                          universe_spreads: list[float]) -> float:
    """bid-ask spreadを流動性スコア (spread小ほど高スコア) にノーマライズ。"""
    if spread_pct is None:
        return 0.5
    valid = [v for v in universe_spreads if v is not None]
    if len(valid) < 2:
        # spread 0.5%→0.0, spread 0%→1.0 の線形近似
        return max(0.0, min(1.0, 1.0 - spread_pct * 2.0))
    mn, mx = min(valid), max(valid)
    if mx == mn:
        return 0.5
    # spreadは小さい方が高スコアなので反転
    return 1.0 - (spread_pct - mn) / (mx - mn)


def _normalize_vix_corr(corr: Optional[float]) -> float:
    """VIX相関係数の絶対値を0〜1にノーマライズ。"""
    if corr is None:
        return 0.5
    return max(0.0, min(1.0, abs(corr)))


# ── スコアリングコア ──────────────────────────────────────────────────────────

def _compute_raw_scores(
    m: SymbolMetrics,
    universe: list[SymbolMetrics],
) -> dict[str, float]:
    """1銘柄の各指標を0.0〜1.0にノーマライズしてdictで返す。"""
    ivrs          = [x.ivr for x in universe]
    vol_ratios    = [x.volume_spike_ratio for x in universe]
    gap_zscores   = []  # ユニバース全体のgap zスコア (本銘柄込み)

    # gap zスコアはユニバース分布算出のために先にraw値を集める
    raw_gaps: list[float] = []
    for x in universe:
        if x.gap_abs_pct is not None:
            hist = [g for g in x.hist_gaps if g is not None and not math.isnan(g)]
            if len(hist) >= 5:
                mu = statistics.mean(hist)
                sigma = statistics.stdev(hist) if len(hist) > 1 else 1.0
                if sigma == 0:
                    sigma = 1.0
                raw_gaps.append((x.gap_abs_pct - mu) / sigma)
            else:
                raw_gaps.append(x.gap_abs_pct)
        else:
            raw_gaps.append(None)

    spreads = [x.bid_ask_spread_pct for x in universe]

    return {
        "ivr":       _normalize_ivr(m.ivr, ivrs),
        "volume":    _normalize_volume(m.volume_spike_ratio, vol_ratios),
        "gap":       _normalize_gap(m.gap_abs_pct, m.hist_gaps, raw_gaps),
        "liquidity": _normalize_liquidity(m.bid_ask_spread_pct, spreads),
        "vix_corr":  _normalize_vix_corr(m.vix_correlation),
    }


def _weighted_score(raw: dict[str, float], weights: dict[str, float]) -> float:
    """重みを使って加重平均スコアを算出。

    マイナスウェイトの指標は「その指標が高いほどスコアが下がる」ことを意味する。
    最終スコアは [0.0, 1.0] にクランプする。
    """
    # 正と負のウェイトを分けて処理
    pos_weight_total = sum(w for w in weights.values() if w > 0)
    neg_weight_total = sum(abs(w) for w in weights.values() if w < 0)
    total_weight = pos_weight_total + neg_weight_total
    if total_weight == 0:
        return 0.5

    score = 0.0
    for key, w in weights.items():
        val = raw.get(key, 0.5)
        if w >= 0:
            score += w * val
        else:
            # 負ウェイト: 指標が高い→その分スコアを引く
            score += abs(w) * (1.0 - val)

    return max(0.0, min(1.0, score / total_weight))


# ── パブリック API ────────────────────────────────────────────────────────────

def score_symbols(
    metrics_list: list[SymbolMetrics],
    tactic: str = "credit_spread",
    earnings_exclude: bool = True,
) -> list[SymbolScore]:
    """銘柄リストをスコアリングして SymbolScore リスト (降順) を返す。

    Args:
        metrics_list:      各銘柄の SymbolMetrics リスト
        tactic:            戦術名 ("credit_spread" / "straddle" / "butterfly" / "iron_condor")
        earnings_exclude:  True で near_earnings==True の銘柄を除外

    Returns:
        SymbolScore のリスト (score 降順)。除外銘柄は末尾に追加。
    """
    if not metrics_list:
        return []

    weights = _TACTIC_WEIGHTS.get(tactic, _DEFAULT_WEIGHTS)
    results: list[SymbolScore] = []

    # 除外後のユニバースのみでノーマライズ計算する
    active = [m for m in metrics_list
              if not (earnings_exclude and m.near_earnings)]
    excluded_metrics = [m for m in metrics_list
                        if earnings_exclude and m.near_earnings]

    for m in active:
        raw = _compute_raw_scores(m, active)
        s = _weighted_score(raw, weights)
        results.append(SymbolScore(
            symbol=m.symbol,
            score=s,
            raw_scores=raw,
            metrics=m,
        ))

    # スコア降順
    results.sort(key=lambda x: x.score, reverse=True)

    # 除外銘柄を末尾に追加（スコア=0）
    for m in excluded_metrics:
        results.append(SymbolScore(
            symbol=m.symbol,
            score=0.0,
            raw_scores={},
            metrics=m,
            excluded=True,
            exclude_reason="near_earnings",
        ))

    log.info(
        f"[SymbolSelector] tactic={tactic} ranked: "
        + ", ".join(f"{r.symbol}({r.score:.3f})" for r in results[:5])
    )
    return results


def select_symbols(
    metrics_list: list[SymbolMetrics],
    tactic: str = "credit_spread",
    top_n: int = 3,
    earnings_exclude: bool = True,
) -> list[SymbolScore]:
    """上位N銘柄を選択して返す。

    Args:
        metrics_list:      各銘柄の SymbolMetrics リスト
        tactic:            戦術名
        top_n:             返す上位銘柄数 (0以下なら全件)
        earnings_exclude:  True で決算近傍銘柄を除外

    Returns:
        上位N件の SymbolScore リスト (score 降順)
    """
    ranked = score_symbols(metrics_list, tactic=tactic,
                           earnings_exclude=earnings_exclude)
    active = [r for r in ranked if not r.excluded]
    if top_n > 0:
        active = active[:top_n]
    log.info(
        f"[SymbolSelector] select_symbols top_{top_n}: "
        + ", ".join(r.symbol for r in active)
    )
    return active


def get_default_universe() -> list[str]:
    """デフォルト候補銘柄リストを返す。atlas_rules.yaml統合時はここを拡張する。"""
    return list(_DEFAULT_UNIVERSE)


def get_tactic_names() -> list[str]:
    """サポートしている戦術名一覧を返す。"""
    return list(_TACTIC_WEIGHTS.keys())


# ── 市場動向データを使ったフィルタリング統合 ─────────────────────────────────

def apply_market_filters(
    ranked: list[SymbolScore],
    sector_scores: Optional[dict] = None,
    flow_signals: Optional[dict] = None,
    news_sentiments: Optional[dict] = None,
    tactic: str = "credit_spread",
    sector_map: Optional[dict] = None,
) -> list[SymbolScore]:
    """ランキング済みシンボルリストに市場動向フィルタを適用する。

    Args:
        ranked:          score_symbols() の戻り値
        sector_scores:   {symbol: SectorScore} (sector_rotation モジュール出力)
        flow_signals:    {symbol: FlowSignal} (options_flow モジュール出力)
        news_sentiments: {symbol: NewsSentiment} (news_sentiment モジュール出力)
        tactic:          戦術名
        sector_map:      {銘柄: セクターETF} の対応表

    Returns:
        フィルタ適用済みの SymbolScore リスト
        除外された銘柄は excluded=True になって末尾に移動する。
    """
    if not ranked:
        return ranked

    active:   list[SymbolScore] = []
    excluded: list[SymbolScore] = []

    for sym_score in ranked:
        if sym_score.excluded:
            excluded.append(sym_score)
            continue

        sym = sym_score.symbol
        reasons: list[str] = []

        # ── セクターフィルタ: lagging セクター銘柄を除外 ───────────────────
        if sector_scores is not None and tactic in ("credit_spread", "iron_condor"):
            _sector_map = sector_map or {}
            from common.sector_rotation import sector_signal_for_symbol
            regime = sector_signal_for_symbol(sym, sector_scores, sector_map=_sector_map)
            if regime == "lagging":
                reasons.append(f"sector=lagging")

        # ── ニュースフィルタ: ネガティブニュース銘柄を除外 ──────────────────
        if news_sentiments is not None:
            from common.news_sentiment import filter_by_news
            # 単一銘柄のフィルタ
            allowed, _ = filter_by_news([sym], news_sentiments, tactic=tactic)
            if sym not in allowed:
                reasons.append("news=negative")

        if reasons:
            sym_score = SymbolScore(
                symbol=sym_score.symbol,
                score=sym_score.score,
                raw_scores=sym_score.raw_scores,
                metrics=sym_score.metrics,
                excluded=True,
                exclude_reason=" | ".join(reasons),
            )
            excluded.append(sym_score)
        else:
            active.append(sym_score)

    # flow_bias を考慮してスコアを微調整（除外はしない）
    if flow_signals is not None:
        for sym_score in active:
            flow = flow_signals.get(sym_score.symbol)
            if flow is not None and flow.confidence >= 0.3:
                # flow_bias が戦術と一致する方向ならスコアを小幅 UP
                flow_boost = 0.0
                if tactic in ("cs_sell", "ic_sell") and flow.flow_bias > 0:
                    flow_boost = 0.03 * flow.confidence  # 最大 +3%
                elif tactic == "straddle_buy" and abs(flow.flow_bias) > 0.3:
                    flow_boost = 0.02 * flow.confidence  # 大口フロー = ボラ予感
                sym_score.score = min(1.0, sym_score.score + flow_boost)

    log.info(
        f"[SymbolSelector] market_filter tactic={tactic}: "
        f"active={[r.symbol for r in active][:5]} "
        f"excluded={[r.symbol for r in excluded][:5]}"
    )
    return active + excluded
