#!/usr/bin/env python3
"""
strategy_selector.py — 環境適応型 戦術選択エンジン

設計思想:
  優秀なトレーダーは毎朝データを見て判断基準を調整する。
  このエンジンもそうあるべき。固定閾値は最小限（パニック閾値のみ）。
  全VIX閾値は60日パーセンタイルから動的算出。

戦術候補 (8戦術):
  - ic_sell       : Iron Condor 売り（低ボラ・方向感なし・IVR中〜高）
  - butterfly     : ATM Long Butterfly（低IVR・レンジ環境・IV拡張期待なし）
  - calendar_sell : Calendar Spread 売り（高IVR・VIX20〜50・front IV crush狙い）
  - strangle_sell : OTMストラングル売り（高IVR・方向感なし・中〜高ボラ）
  - cs_sell       : Credit Spread 売り（中低ボラ・方向性あり or やや高ボラ）
  - orb_buy       : Opening Range Breakout 買い（方向性あり・値動き十分）
  - straddle_buy  : ストラドル買い（高ボラ・方向不明）
  - no_trade      : ノートレード（パニック環境 or スコア低すぎ）

入力 (env dict):
  vix            : float  — 現在のVIX
  vix_rate       : float  — VIX変化率 (%/時)
  vrp            : float  — Volatility Risk Premium (IV - HV20)
  gex            : float  — Gamma Exposure（正=安定化、負=増幅）
  term_struct    : float  — VIX Term Structure (VIX9D / VIX 比率)
  vix_term_ratio : float  — VIX9D / VIX3M 比率 (コンタンゴ判定用)
  ivr            : float  — IV Rank 0〜100 (高=売り有利、低=Butterfly有利)
  env_score      : float  — premarket_assessmentの環境スコア (0〜100)
  gap_pct        : float  — 前日からのギャップ率 (%)
  vix_history    : list   — 過去60日のVIX日次値（動的閾値算出に使用）
  bias           : str    — "bull" / "bear" / "neutral"（プレマーケットバイアス）

出力:
  {
    "primary":   {"strategy": str, "confidence": float, "allocation": float},
    "secondary": {"strategy": str, "confidence": float, "allocation": float} | None,
    "reason":    str,
    "no_trade_reasons": list[str],
    "thresholds": dict   # 使用した動的閾値（ロギング・デバッグ用）
  }

設計根拠:
  data/research_dynamic_allocation.md — CS/ORBのVIX別パフォーマンス・スコアリング設計
  data/research_dynamic_params_design.md — 動的閾値算出の実装方針
  data/research_orb_breakout.md — ORB実践者調査・VIX 15-25 + ATRフィルタで WR 50%推定
  data/research_vix_rate_strategies.md — VIX rate活用パターン調査
"""

from __future__ import annotations

import bisect
import statistics
import math
import logging
from typing import Optional

log = logging.getLogger(__name__)


# ── 市場動向データ統合レイヤー ───────────────────────────────────────────────
# common/sector_rotation / options_flow / news_sentiment / econ_events / cross_asset
# から市場動向シグナルを env dict に統合するユーティリティ

def enrich_env_with_market_data(env: dict) -> dict:
    """env dict に市場動向データを追加して返す。

    Args:
        env: 既存の select_strategy() 用 env dict

    Returns:
        market_data キーが追加された env dict
        既存キーは全て維持される（非破壊的追加）

    追加キー:
        market_data.sector_regime     : "risk_on" / "risk_off" / "neutral"
        market_data.cross_asset_regime: "risk_on" / "risk_off" / "neutral"
        market_data.flow_bias         : float -1.0〜+1.0 (プライマリ銘柄)
        market_data.news_label        : "positive" / "negative" / "neutral"
        market_data.econ_blackout     : bool (True=経済イベントブラックアウト中)
        market_data.straddle_signal   : bool (発表直後IV変動機会)
    """
    market_data = env.get("market_data", {})
    if not isinstance(market_data, dict):
        market_data = {}

    # デフォルト値（データ取得失敗時のフォールバック）
    defaults = {
        "sector_regime":      "neutral",
        "cross_asset_regime": "neutral",
        "flow_bias":          0.0,
        "news_label":         "neutral",
        "econ_blackout":      False,
        "straddle_signal":    False,
    }
    for key, val in defaults.items():
        market_data.setdefault(key, val)

    result = dict(env)
    result["market_data"] = market_data
    return result


def apply_market_data_adjustments(result: dict, env: dict) -> dict:
    """select_strategy の結果に市場動向データに基づく調整を適用する。

    Args:
        result: select_strategy() の戻り値 dict
        env:    enrich_env_with_market_data() 適用済み env dict

    Returns:
        調整済みの result dict
    """
    market_data = env.get("market_data", {})
    if not market_data:
        return result

    econ_blackout   = market_data.get("econ_blackout", False)
    straddle_signal = market_data.get("straddle_signal", False)
    cross_regime    = market_data.get("cross_asset_regime", "neutral")
    flow_bias       = market_data.get("flow_bias", 0.0)
    news_label      = market_data.get("news_label", "neutral")

    adjustments: list[str] = []

    # (1) 経済イベントブラックアウト中 → no_trade に強制変更
    if econ_blackout:
        result = dict(result)
        original = result["primary"]["strategy"]
        result["primary"] = {"strategy": "no_trade", "confidence": 1.0, "allocation": 1.0}
        result["secondary"] = None
        result["no_trade_reasons"] = result.get("no_trade_reasons", []) + [
            f"economic_event_blackout: {original} → no_trade"
        ]
        adjustments.append("econ_blackout→no_trade")

    # (2) straddle_signal: 経済発表直後 → straddle_buy を secondary に挿入
    elif straddle_signal and result.get("primary", {}).get("strategy") != "no_trade":
        result = dict(result)
        result["secondary"] = {"strategy": "straddle_buy", "confidence": 0.6, "allocation": 0.3}
        adjustments.append("straddle_signal→secondary_straddle")

    # (3) risk_off環境 → credit_spread/ic_sell の confidence を 20% 下げる
    if cross_regime == "risk_off":
        primary_strat = result.get("primary", {}).get("strategy", "")
        if primary_strat in ("cs_sell", "ic_sell", "strangle_sell"):
            result = dict(result)
            primary = dict(result["primary"])
            primary["confidence"] = max(0.0, primary["confidence"] * 0.8)
            result["primary"] = primary
            adjustments.append(f"risk_off→{primary_strat} confidence-20%")

    # (4) ネガティブニュース + credit_spread → confidence をさらに 10% 下げる
    if news_label == "negative":
        primary_strat = result.get("primary", {}).get("strategy", "")
        if primary_strat in ("cs_sell", "ic_sell"):
            result = dict(result)
            primary = dict(result["primary"])
            primary["confidence"] = max(0.0, primary["confidence"] * 0.9)
            result["primary"] = primary
            adjustments.append("negative_news→confidence-10%")

    # (5) flow_bias が強い方向 (>0.5 or <-0.5) → reason に追記
    if abs(flow_bias) >= 0.5:
        direction = "bullish_flow" if flow_bias > 0 else "bearish_flow"
        result = dict(result)
        result["reason"] = result.get("reason", "") + f" | {direction}({flow_bias:.2f})"
        adjustments.append(f"flow_bias={flow_bias:.2f}")

    if adjustments:
        log.info(f"[StrategySelector] market_data adjustments: {adjustments}")

    return result

# ── パニック閾値のみ固定（研究・実践者データの一貫した境界値）──────────────
# VIX > PANIC_THRESHOLD: 全売り戦略を停止。ORBも期待値低下。
# 根拠: CS売りはVIX>=22で期待値が負(research_dynamic_allocation.md 2A)
#       VIX30超はblack swan領域（spy_bot.pyの設計と整合）
PANIC_THRESHOLD_FIXED = 30.0

# ── 動的閾値算出 ────────────────────────────────────────────────────────────

def compute_vix_percentile(current_vix: float, vix_history: list[float]) -> float:
    """現在のVIXが過去60日の何パーセンタイルにあるかを返す (0〜100)。

    設計根拠 (research_dynamic_params_design.md 5B):
      低VIX時代(2017: avg=11)でもP70=13が"elevated"として機能。
      高VIX時代(2022: avg=25)でもP30=22が"calm"として機能。
    """
    if not vix_history:
        return 50.0  # データなし → 中央値と仮定
    hist = sorted(vix_history)
    rank = bisect.bisect_left(hist, current_vix)
    return round(rank / len(hist) * 100.0, 1)


def compute_dynamic_vix_thresholds(vix_history: list[float]) -> dict:
    """過去60日のVIX分布から動的閾値セットを算出。

    Returns:
      {
        "calm":    float,  # P30 相当（売り環境の上限）
        "elevated":float,  # P70 相当（ORB環境の基準）
        "panic":   float,  # P95 相当（取引停止の基準）
      }

    データ不足時は保守的フォールバック値を使用し警告を記録する。
    PANIC_THRESHOLD_FIXED=30を上限にキャップ（極端な過去データへの過適合を防ぐ）。
    """
    fallback = {"calm": 16.0, "elevated": 22.0, "panic": PANIC_THRESHOLD_FIXED}

    if len(vix_history) < 20:
        log.warning(
            f"[StrategySelector] VIX history too short ({len(vix_history)} days). "
            "Using fallback thresholds."
        )
        return fallback

    hist = sorted(vix_history)
    n = len(hist)

    def pctl(p: float) -> float:
        idx = int(p / 100.0 * (n - 1))
        return hist[idx]

    calm     = round(pctl(30), 1)
    elevated = round(pctl(70), 1)
    panic    = round(min(pctl(95), PANIC_THRESHOLD_FIXED), 1)

    # 論理的整合性チェック（履歴データが異常な場合の保護）
    if not (10.0 <= calm <= 30.0):
        log.warning(f"[StrategySelector] calm={calm} out of expected range; using fallback")
        calm = fallback["calm"]
    if not (15.0 <= elevated <= 40.0):
        log.warning(f"[StrategySelector] elevated={elevated} out of expected range; using fallback")
        elevated = fallback["elevated"]

    # calm < elevated < panic の整合性を保証
    if calm >= elevated:
        log.warning(
            f"[StrategySelector] calm={calm} >= elevated={elevated}; "
            "adjusting elevated to calm + 4.0"
        )
        elevated = round(calm + 4.0, 1)
    if elevated >= panic:
        # elevated がパニック閾値以上になるのは高VIX時代のみ。
        # この場合 elevated を panic - 4.0 に引き下げる（panicを固定値として維持）。
        log.warning(
            f"[StrategySelector] elevated={elevated} >= panic={panic}; "
            "adjusting elevated to panic - 4.0"
        )
        elevated = round(panic - 4.0, 1)
        # それでも calm >= elevated なら elevated を calm と panic の中間点に
        if calm >= elevated:
            elevated = round((calm + panic) / 2.0, 1)

    return {"calm": calm, "elevated": elevated, "panic": panic}


# ── 環境スコア算出（research_dynamic_allocation.md 3A〜3B に基づく）──────────

def calc_environment_score(
    vix_pctl: float,
    vix_rate: float,
    vrp: Optional[float],
    term_struct: Optional[float],
    gex: Optional[float],
    gap_pct: Optional[float],
) -> float:
    """環境スコアを算出。

    スコア解釈:
      < -30 : 強い売り環境（CS/IC 優位）
      -30〜+10: 混合（売り主体 + 一部ORB）
      +10〜+30: ボラ上昇中（ORB 主体）
      > +30 : 強い買い/恐怖環境（ORB/ストラドル 優位）

    全閾値はパーセンタイルベース。この関数内にハードコードされた閾値は
    スコアリングの連続関数化のために必要な最小限の補間点のみ。

    Args:
      vix_pctl   : VIXの60日パーセンタイル (0〜100)
      vix_rate   : VIX変化率 (%/時)
      vrp        : IV - HV20（正=売り有利、負=売り不利）
      term_struct: VIX9D / VIX 比率（< 1.0=バックワーデーション=恐怖、>= 1.0=コンタンゴ=安定）
      gex        : Gamma Exposure（正=安定化力、負=増幅力）
      gap_pct    : 前日比ギャップ率 (%)
    """
    score = 0.0

    # ── VIXパーセンタイル (重み 30%) ─────────────────────────────────────
    # P0〜P100 を線形マッピングで -80〜+90 にスケール
    # P50=0 を中心に: 低パーセンタイル=売り有利（負スコア）、高=買い有利（正スコア）
    vp = vix_pctl
    if vp < 20:
        s_vix = -80.0
    elif vp < 50:
        # P20→-80, P50→-9（線形補間）
        s_vix = -80.0 + (vp - 20.0) / 30.0 * 71.0
    elif vp < 80:
        # P50→-9, P80→+30（線形補間）
        s_vix = -9.0 + (vp - 50.0) / 30.0 * 39.0
    elif vp < 95:
        # P80→+30, P95→+70（線形補間）
        s_vix = 30.0 + (vp - 80.0) / 15.0 * 40.0
    else:
        s_vix = 90.0
    score += s_vix * 0.30

    # ── VIX変化率 (重み 20%) ─────────────────────────────────────────────
    # 下落=売り有利（負スコア）、急上昇=買い有利（正スコア）
    vr = vix_rate if vix_rate is not None else 0.0
    if vr < -2.0:
        s_rate = -60.0
    elif vr < 2.0:
        s_rate = -20.0 + (vr + 2.0) / 4.0 * 20.0  # -20 〜 0 の線形
    elif vr < 5.0:
        s_rate = 0.0 + (vr - 2.0) / 3.0 * 40.0    # 0 〜 40 の線形
    else:
        s_rate = 80.0
    score += s_rate * 0.20

    # ── VRP (重み 20%) ──────────────────────────────────────────────────
    # VRP正（IV>HV）=プレミアム売りに有利（負スコア）
    # VRP負（IV<HV）=値動き大きい＝買い有利（正スコア）
    if vrp is None:
        pass  # データなし→スコア 0（中立）
    else:
        if vrp > 5.0:
            s_vrp = -80.0
        elif vrp > 2.0:
            s_vrp = -40.0 + (vrp - 2.0) / 3.0 * (-40.0)  # -40 〜 -80 の線形
        elif vrp > -2.0:
            s_vrp = -40.0 * (vrp / 2.0)   # -2〜+2 の間を線形: -40〜+40
        elif vrp > -5.0:
            s_vrp = 40.0 + (-vrp - 2.0) / 3.0 * 40.0   # 40 〜 80 の線形
        else:
            s_vrp = 80.0
        score += s_vrp * 0.20

    # ── Term Structure (重み 15%) ────────────────────────────────────────
    # VIX9D/VIX比率: < 1.0=バックワーデーション=恐怖（正スコア）、>=1.0=コンタンゴ（負スコア）
    if term_struct is not None:
        ts = term_struct
        if ts < 0.90:
            s_ts = -60.0
        elif ts < 0.98:
            s_ts = -60.0 + (ts - 0.90) / 0.08 * 30.0   # -60 〜 -30
        elif ts < 1.02:
            s_ts = -30.0 + (ts - 0.98) / 0.04 * 30.0   # -30 〜 0
        elif ts < 1.10:
            s_ts = 0.0 + (ts - 1.02) / 0.08 * 40.0     # 0 〜 40
        else:
            s_ts = 80.0
        score += s_ts * 0.15

    # ── GEX (重み 10%) ───────────────────────────────────────────────────
    # 正GEX=マーケットメーカーが安定化（売り有利）、負GEX=増幅（買い方向性あり）
    if gex is not None:
        if gex > 1e9:
            s_gex = -50.0
        elif gex > 0:
            s_gex = -20.0
        elif gex > -1e9:
            s_gex = 30.0
        else:
            s_gex = 70.0
        score += s_gex * 0.10

    # ── Overnight Gap (重み 5%) ──────────────────────────────────────────
    # ギャップ大=方向性あり（正スコア）、小=レンジ（負スコア）
    if gap_pct is not None:
        ag = abs(gap_pct)
        if ag < 0.3:
            s_gap = -20.0
        elif ag < 0.7:
            s_gap = 0.0
        else:
            s_gap = 40.0
        score += s_gap * 0.05

    return round(score, 1)


# ── メイン戦術選択ロジック ────────────────────────────────────────────────────

def select_strategy(env: dict) -> dict:
    """最適な戦術を環境データから動的に選択する。

    Args:
      env: 環境データ dict。キー一覧は module docstring 参照。

    Returns:
      {
        "primary":   {"strategy": str, "confidence": float, "allocation": float},
        "secondary": {"strategy": str, "confidence": float, "allocation": float} | None,
        "reason":    str,
        "no_trade_reasons": list[str],
        "thresholds": dict,
      }

      strategy 値:
        "ic_sell"       — Iron Condor 売り（低ボラ・方向感なし・IVR高）
        "butterfly"     — ATM Long Butterfly（低IVR・レンジ環境）
        "calendar_sell" — Calendar Spread 売り（高IVR・VIX20〜50・front IV crush狙い）
        "strangle_sell" — OTMストラングル売り（高IVR・中〜高ボラ・方向感なし）
        "cs_sell"       — Credit Spread 売り（中低ボラ・方向性あり or やや高ボラ）
        "orb_buy"       — Opening Range Breakout 買い（方向性あり・値動き十分）
        "straddle_buy"  — ストラドル買い（高ボラ・方向不明）
        "no_trade"      — ノートレード（パニック環境 or スコア低すぎ）
      confidence: 0.0〜1.0 （戦術の根拠の強さ）
      allocation: 0.0〜1.0 （資金配分比率。primary + secondary = 1.0）
    """
    # ── 入力値の展開 ─────────────────────────────────────────────────────
    vix          = float(env.get("vix", 20.0))
    vix_rate     = float(env.get("vix_rate", 0.0))
    vrp          = env.get("vrp")           # None 可
    gex          = env.get("gex")           # None 可
    term_struct  = env.get("term_struct")   # None 可 (VIX9D/VIX 比率)
    vix_term_ratio = env.get("vix_term_ratio")  # None 可 (VIX9D/VIX3M 比率)
    ivr          = env.get("ivr")           # None 可 (IV Rank 0〜100)
    env_score    = float(env.get("env_score", 50.0))   # premarket_assessment スコア
    gap_pct      = env.get("gap_pct", 0.0)
    vix_history  = env.get("vix_history", [])
    bias         = env.get("bias", "neutral")  # "bull" / "bear" / "neutral"

    # ── 動的閾値算出 ─────────────────────────────────────────────────────
    thresholds = compute_dynamic_vix_thresholds(vix_history)
    vix_calm     = thresholds["calm"]      # P30相当
    vix_elevated = thresholds["elevated"]  # P70相当
    vix_panic    = thresholds["panic"]     # P95相当 (上限: PANIC_THRESHOLD_FIXED)

    vix_pctl = compute_vix_percentile(vix, vix_history)

    # ── 方向性スコアを calc_environment_score で算出 ─────────────────────
    dir_score = calc_environment_score(
        vix_pctl=vix_pctl,
        vix_rate=vix_rate,
        vrp=vrp,
        term_struct=term_struct,
        gex=gex,
        gap_pct=gap_pct,
    )

    no_trade_reasons: list[str] = []

    # ── ステージ1: ノートレード判定（絶対条件）───────────────────────────
    # (A) パニック域: VIX > panic閾値（全売り停止）
    if vix > vix_panic:
        no_trade_reasons.append(
            f"VIX={vix:.1f} > panic={vix_panic} — パニック域。全売り戦略を停止。"
        )

    # (B) Premarket スコア < 50: 環境品質が低すぎる
    if env_score < 50:
        no_trade_reasons.append(
            f"env_score={env_score} < 50 — 環境スコア不足。経済イベント・ギャップ等のリスクあり。"
        )

    if no_trade_reasons:
        # ノートレード条件成立。ただしVIXが極端に高い場合でもORB検討可
        # VIX>panicかつ方向性スコア+30超かつenv_score>=50: ORBのみ検討
        orb_in_panic = (
            vix > vix_panic
            and dir_score > 30
            and env_score >= 50
        )
        if orb_in_panic:
            return {
                "primary":   {"strategy": "orb_buy",  "confidence": 0.40, "allocation": 0.5},
                "secondary": {"strategy": "no_trade",  "confidence": 1.00, "allocation": 0.5},
                "reason": (
                    f"VIX={vix:.1f}パニック域だが強い方向性(dir_score={dir_score})を検知。"
                    f"資金50%のみORB検討。残り50%はノートレード。"
                ),
                "no_trade_reasons": no_trade_reasons,
                "thresholds": thresholds,
            }
        return {
            "primary":   {"strategy": "no_trade", "confidence": 1.00, "allocation": 1.0},
            "secondary": None,
            "reason": " | ".join(no_trade_reasons),
            "no_trade_reasons": no_trade_reasons,
            "thresholds": thresholds,
        }

    # ── ステージ2: VIX帯域 × 方向性スコアによる戦術マトリクス ─────────────
    #
    # 設計根拠 (research_dynamic_allocation.md 2C):
    #   VIX < calm  (P30): CS/IC売り優位
    #   VIX calm〜elevated (P30〜P70): 混合環境。VRPとGEXで判断
    #   VIX elevated〜panic (P70〜P95): ORB優位
    #
    # VIX帯域を 0〜3 の整数で区分
    #   0: VIX < calm      (低ボラ)
    #   1: calm <= VIX < elevated  (中ボラ低)
    #   2: elevated <= VIX < panic (中ボラ高)
    #   3: VIX >= panic    (ステージ1で処理済み、ここには来ない)

    if vix < vix_calm:
        vix_zone = 0
    elif vix < vix_elevated:
        vix_zone = 1
    else:
        vix_zone = 2  # elevated 〜 panic

    # VRPの符号（売り有利かどうか）
    vrp_positive = vrp is not None and vrp > 0.0
    vrp_strongly_positive = vrp is not None and vrp > 2.0
    vrp_negative = vrp is not None and vrp < 0.0

    # GEXの区分
    gex_positive = gex is not None and gex > 0
    gex_strongly_positive = gex is not None and gex > 1e9

    # 方向性の判断（スコア + バイアス）
    sell_regime  = dir_score < -10    # 強い売り環境
    mixed_regime = -10 <= dir_score <= 10
    buy_regime   = dir_score > 10     # 方向性あり＋買い戦略有利

    # バイアスによる方向性の補正（方向不明時の補助）
    has_direction = bias in ("bull", "bear")

    # ── 戦術決定テーブル ─────────────────────────────────────────────────

    primary_strategy:   str   = "no_trade"
    primary_confidence: float = 0.5
    primary_alloc:      float = 1.0
    secondary_strategy: Optional[str]   = None
    secondary_confidence: float = 0.0
    secondary_alloc:    float = 0.0
    reason_parts: list[str] = []

    # IVR の区分（ivr が None の場合は中立扱い）
    ivr_low    = ivr is not None and ivr < 30.0   # 低IV環境 → Butterfly有利
    ivr_high   = ivr is not None and ivr > 60.0   # 高IV環境 → Strangle/Calendar有利
    ivr_medium = ivr is not None and 30.0 <= ivr <= 60.0

    # VIX9D/VIX3M レシオ（atlas_rules.yaml vix_term_structure と同一判定）
    vtr_contango      = (vix_term_ratio is not None and vix_term_ratio < 0.85)
    vtr_backwardation = (vix_term_ratio is not None and vix_term_ratio > 1.05)

    reason_parts.append(
        f"VIX={vix:.1f}(P{vix_pctl:.0f}, zone={vix_zone}) "
        f"VRP={vrp} GEX={gex} dir_score={dir_score:.1f} bias={bias} "
        f"IVR={ivr} term_ratio={vix_term_ratio}"
    )

    # ゾーン0: VIX < calm (低ボラ帯)
    # 研究根拠: CS売りVIX<18でSharpe~6.0、VIX<15はORB避けるべき(fakeout増加)
    # Butterfly: 低IVR（IVR<30）×方向感なし → IV拡張の恩恵を受けない環境で最適
    # Strangle売り: 低ボラだがIVR高 → プレミアム収集型で両サイドから稼ぐ
    if vix_zone == 0:
        if ivr_low and mixed_regime and not has_direction:
            # 低IVR × 方向感なし: Butterfly（IV拡張を期待しない・ATM中心に稼ぐ）
            primary_strategy   = "butterfly"
            primary_confidence = 0.75
            primary_alloc      = 1.0
            reason_parts.append(
                f"低ボラ帯+IVR低({ivr:.0f})+方向感なし → Butterfly。IV拡張期待なし環境で最適。"
            )
        elif ivr_high and gex_strongly_positive and not has_direction:
            # 高IVR × GEX安定 × 方向感なし: Strangle売り → 両サイドからプレミアム収集
            primary_strategy   = "strangle_sell"
            primary_confidence = 0.80
            primary_alloc      = 1.0
            reason_parts.append(
                f"低ボラ帯+IVR高({ivr:.0f})+GEX強正+方向感なし → Strangle売り。"
                "両サイドからプレミアム収集の最適環境。"
            )
        elif vrp_strongly_positive and gex_positive:
            # 最優秀な売り環境: IVプレミアムが高く、GEXも安定化
            primary_strategy    = "ic_sell"
            primary_confidence  = 0.85
            primary_alloc       = 1.0
            reason_parts.append("VRP強正+GEX正 → IC売り最適環境。両サイドからプレミアム収集。")
        elif vrp_positive:
            # 低ボラでVRP正 → CS売り（GEXが負でも低ボラなのでリスク低）
            primary_strategy    = "cs_sell"
            primary_confidence  = 0.75
            primary_alloc       = 1.0
            if has_direction:
                reason_parts.append(f"VRP正+方向性({bias}) → {bias}方向のCS売り。")
            else:
                reason_parts.append("VRP正+低ボラ → CS売り。方向不明なのでIC検討も可。")
        elif vrp_negative and buy_regime:
            # 低VIXだがVRP負で方向性あり → ORBは低ボラで難しいがCSは不利
            # 資金縮小でCS売り（safety net）のみ
            primary_strategy    = "cs_sell"
            primary_confidence  = 0.50
            primary_alloc       = 0.6
            reason_parts.append("VRP負だが低VIX帯。ORBは低ボラでfakeout多。縮小CS。")
        else:
            # VRPデータなし or 中立
            primary_strategy    = "cs_sell"
            primary_confidence  = 0.60
            primary_alloc       = 1.0
            reason_parts.append("低VIX帯。VRPデータ不足だがCS売りがデフォルト優位。")

    # ゾーン1: calm <= VIX < elevated (中ボラ低帯)
    # 研究根拠: CS売りVIX18-22でSharpe-3.9（不利）。ORBがVIX15-25+ATRで最良EV。
    # Calendar売り: VIX20〜50 × IVR高 × VIX5日EMA下降（コンタンゴ環境）で最適
    # Strangle売り: VIX15〜50 × IVR高(P70以上) × 方向感なし
    elif vix_zone == 1:
        # このゾーンでの評価は VRP と GEX と方向性スコアで決まる
        if ivr_high and not has_direction and vtr_contango:
            # 高IVR × 方向感なし × コンタンゴ: Calendar売り優先
            # atlas_rules.yaml calendar_sell: IVR>P75 × VIX20-50 × term ratio<0.85
            primary_strategy   = "calendar_sell"
            primary_confidence = 0.75
            primary_alloc      = 1.0
            reason_parts.append(
                f"中ボラ低帯+IVR高({ivr:.0f})+方向感なし+コンタンゴ(term_ratio={vix_term_ratio}) "
                "→ Calendar売り。front IV crush狙い。"
            )
        elif ivr_low and (mixed_regime or sell_regime) and not has_direction:
            # 低IVR × 方向感なし: Butterfly（ゾーン1でも適用）
            # 注: VIX履歴によってはzone=0の銘柄がzone=1に入ることがある
            primary_strategy   = "butterfly"
            primary_confidence = 0.70
            primary_alloc      = 1.0
            reason_parts.append(
                f"中ボラ低帯+IVR低({ivr:.0f})+方向感なし → Butterfly。IV拡張期待なし環境で最適。"
            )
        elif ivr_high and mixed_regime:
            # 高IVR × 中立環境: Strangle売りまたはIC売り
            # 方向感がないため両サイドからプレミアム収集
            primary_strategy   = "strangle_sell"
            primary_confidence = 0.72
            primary_alloc      = 0.8
            secondary_strategy   = "cs_sell"
            secondary_confidence = 0.45
            secondary_alloc      = 0.2
            reason_parts.append(
                f"中ボラ低帯+IVR高({ivr:.0f})+方向感中立 "
                "→ Strangle売り主体(80%)。CS補助(20%)。"
            )
        elif sell_regime and vrp_strongly_positive:
            # 方向性スコアが売り寄りかつVRP強正 → CS売り主体
            primary_strategy    = "cs_sell"
            primary_confidence  = 0.70
            primary_alloc       = 0.8
            if buy_regime or has_direction:
                # ORBも補助として
                secondary_strategy    = "orb_buy"
                secondary_confidence  = 0.55
                secondary_alloc       = 0.2
                reason_parts.append(
                    "中ボラ低帯・売り環境優勢。VRP正でCS主体(80%)。方向性ありORB補助(20%)。"
                )
            else:
                reason_parts.append("中ボラ低帯・売り環境。VRP強正でCS売り主体。")
        elif buy_regime and not vrp_strongly_positive:
            # 方向性スコアが買い寄りかつVRPが弱い → ORB主体
            primary_strategy    = "orb_buy"
            primary_confidence  = 0.65
            primary_alloc       = 0.8
            secondary_strategy    = "cs_sell"
            secondary_confidence  = 0.50
            secondary_alloc       = 0.2
            reason_parts.append(
                "中ボラ低帯・買い環境。ORB主体(80%)。VRP弱いのでCSは縮小(20%)。"
            )
        elif buy_regime and vrp_positive:
            # ORBとCSどちらも有効: 均等分割
            primary_strategy    = "orb_buy"
            primary_confidence  = 0.62
            primary_alloc       = 0.6
            secondary_strategy    = "cs_sell"
            secondary_confidence  = 0.60
            secondary_alloc       = 0.4
            reason_parts.append(
                "中ボラ低帯・方向性あり+VRP正。ORB(60%)+CS(40%)の均衡型。"
            )
        elif mixed_regime:
            # 方向感なし: バイアスで振り分け
            if has_direction and vrp_positive:
                primary_strategy    = "cs_sell"
                primary_confidence  = 0.58
                primary_alloc       = 0.7
                secondary_strategy    = "orb_buy"
                secondary_confidence  = 0.50
                secondary_alloc       = 0.3
                reason_parts.append(
                    f"中ボラ低帯・中立。バイアス{bias}+VRP正 → CS主体(70%)。ORB補助(30%)。"
                )
            elif vrp_negative:
                primary_strategy    = "orb_buy"
                primary_confidence  = 0.55
                primary_alloc       = 0.7
                secondary_strategy    = "cs_sell"
                secondary_confidence  = 0.40
                secondary_alloc       = 0.3
                reason_parts.append(
                    "中ボラ低帯・中立。VRP負 → ORB主体(70%)。CSは縮小。"
                )
            else:
                primary_strategy    = "cs_sell"
                primary_confidence  = 0.55
                primary_alloc       = 0.6
                secondary_strategy    = "orb_buy"
                secondary_confidence  = 0.45
                secondary_alloc       = 0.4
                reason_parts.append(
                    "中ボラ低帯・中立。データ不足のためCS/ORB均衡型(60:40)。"
                )
        else:
            # フォールバック
            primary_strategy    = "cs_sell"
            primary_confidence  = 0.55
            primary_alloc       = 0.6
            secondary_strategy    = "orb_buy"
            secondary_confidence  = 0.45
            secondary_alloc       = 0.4
            reason_parts.append("中ボラ低帯・デフォルト。CS/ORB均衡型(60:40)。")

    # ゾーン2: elevated <= VIX < panic (中ボラ高帯)
    # 研究根拠: CS売りはVIX>=22で期待値負。ORBがVIX>20で平均勝ち額+35%。
    # Strangle売り: VIX15〜50 × IVR高(P70) × 方向感なし → 高IVR時は売りも有効
    # Calendar売り: VIX20〜50 × IVR>P75 × コンタンゴ → term structure活用
    elif vix_zone == 2:
        if ivr_high and mixed_regime and vtr_contango:
            # 高ボラだがIVR高 × 方向感なし × コンタンゴ: Calendar売り
            # (IVがコンタンゴ環境では front IV crush が大きい)
            primary_strategy   = "calendar_sell"
            primary_confidence = 0.60
            primary_alloc      = 0.7
            secondary_strategy   = "orb_buy"
            secondary_confidence = 0.50
            secondary_alloc      = 0.3
            reason_parts.append(
                f"高ボラ帯+IVR高({ivr:.0f})+方向感なし+コンタンゴ → "
                "Calendar売り主体(70%)。方向確定後ORB補助(30%)。"
            )
        elif ivr_high and sell_regime:
            # 高IVR × 売り環境: Strangle売りで両サイドからプレミアム収集（サイズ縮小）
            primary_strategy   = "strangle_sell"
            primary_confidence = 0.55
            primary_alloc      = 0.6
            secondary_strategy   = "orb_buy"
            secondary_confidence = 0.45
            secondary_alloc      = 0.4
            reason_parts.append(
                f"高ボラ帯+IVR高({ivr:.0f})+売り環境 → "
                "Strangle売り縮小(60%)。ORB補助(40%)。"
            )
        elif vrp_negative and buy_regime:
            # VRP負+方向性あり → ORB最優先
            primary_strategy    = "orb_buy"
            primary_confidence  = 0.75
            primary_alloc       = 1.0
            reason_parts.append(
                "高ボラ帯・VRP負+方向性あり。ORB最優先。CS売りは期待値負で回避。"
            )
        elif buy_regime:
            # 方向性あり（VRPは弱い）→ ORB主体
            primary_strategy    = "orb_buy"
            primary_confidence  = 0.70
            primary_alloc       = 0.9
            secondary_strategy    = "cs_sell"
            secondary_confidence  = 0.35
            secondary_alloc       = 0.1
            reason_parts.append(
                "高ボラ帯・方向性あり。ORB主体(90%)。CS最小限(10%、証拠金確保目的のみ)。"
            )
        elif mixed_regime:
            # 方向不明: ストラドル買いを検討
            primary_strategy    = "straddle_buy"
            primary_confidence  = 0.55
            primary_alloc       = 0.7
            secondary_strategy    = "orb_buy"
            secondary_confidence  = 0.45
            secondary_alloc       = 0.3
            reason_parts.append(
                "高ボラ帯・方向不明。ストラドル買い主体(70%)。方向確定後のORB補助(30%)。"
            )
        elif sell_regime and vrp_strongly_positive:
            # 高ボラだがVRP正かつ売り環境: 縮小CS（慎重）
            primary_strategy    = "cs_sell"
            primary_confidence  = 0.45
            primary_alloc       = 0.5
            reason_parts.append(
                "高ボラ帯だがVRP強正+売り環境。縮小CS(50%)。VIXがさらに上昇なら即撤退。"
            )
        else:
            # 方向性不明 + VRP中立: ストラドル
            primary_strategy    = "straddle_buy"
            primary_confidence  = 0.50
            primary_alloc       = 0.6
            secondary_strategy    = "orb_buy"
            secondary_confidence  = 0.40
            secondary_alloc       = 0.4
            reason_parts.append(
                "高ボラ帯・方向不明。ストラドル買い主体(60%)。ORB補助(40%)。"
            )

    # ── ステージ3: env_score による信頼度と配分の最終補正 ────────────────
    # env_score 50〜70: reduce_size ゾーン → 信頼度・配分を下げる
    # env_score >= 70: 通常
    if 50 <= env_score < 70:
        scale = (env_score - 50.0) / 20.0   # 0.0〜1.0
        # 信頼度を 10〜20% 下げる
        primary_confidence = round(primary_confidence * (0.80 + scale * 0.20), 2)
        if secondary_strategy:
            secondary_confidence = round(secondary_confidence * (0.80 + scale * 0.20), 2)
        reason_parts.append(
            f"env_score={env_score:.0f}(50〜70帯): 信頼度を補正ダウン。サイズ縮小推奨。"
        )

    # ── アロケーション正規化 ─────────────────────────────────────────────
    # primary + secondary = 1.0 を保証
    if secondary_strategy is not None:
        total = primary_alloc + secondary_alloc
        if total > 0:
            primary_alloc   = round(primary_alloc   / total, 2)
            secondary_alloc = round(secondary_alloc / total, 2)
            # 丸め誤差の調整
            if primary_alloc + secondary_alloc != 1.0:
                primary_alloc = round(1.0 - secondary_alloc, 2)

    # ── 結果の組み立て ────────────────────────────────────────────────────
    primary = {
        "strategy":   primary_strategy,
        "confidence": round(primary_confidence, 2),
        "allocation": primary_alloc,
    }

    secondary = None
    if secondary_strategy is not None:
        secondary = {
            "strategy":   secondary_strategy,
            "confidence": round(secondary_confidence, 2),
            "allocation": secondary_alloc,
        }

    return {
        "primary":          primary,
        "secondary":        secondary,
        "reason":           " | ".join(reason_parts),
        "no_trade_reasons": no_trade_reasons,
        "thresholds":       thresholds,
    }


# ── 決算特化戦術選択 ─────────────────────────────────────────────────────────

def select_earnings_strategy(env: dict) -> dict:
    """決算周辺の戦術を EM vs HM 比較で選択する。

    通常の select_strategy() とは独立したエントリーポイント。
    strategy_selectorに "earnings_ic" / "earnings_calendar" / "earnings_straddle" /
    "post_earnings_cs" を追加するためのエントリ関数。

    Args:
      env: dict — 通常のenvキーに加え以下を追加:
        "earnings_offset": int  — 決算との距離 (-1=翌日, 0=当日, 1=前日, 2=2日前)
        "em_pct":   float  — Expected Move % (ATMストラドル価格/株価×100)
        "hm_pct":   float  — Historical Move % (過去8決算の平均変動率)
        "gap_pct":  float  — 当日のギャップ % (post_earnings_csで使用)
        "symbol":   str    — 対象銘柄 ("US.TSLA" 等)

    Returns:
      {
        "primary":   {"strategy": str, "confidence": float, "allocation": float},
        "secondary": None,
        "reason":    str,
        "no_trade_reasons": list[str],
      }

      strategy値:
        "earnings_ic"       — 決算前日のIC売り (EM > HM)
        "earnings_calendar" — 決算前日のCalendar Spread (IVR高+term structure急)
        "earnings_straddle" — 決算前日のStraddle買い (EM < HM * 0.8)
        "post_earnings_cs"  — 決算翌日の方向性CS (gap方向に順張り)
        "no_trade"          — 決算当日・条件未達・リスク過大
    """
    earnings_offset = int(env.get("earnings_offset", 999))
    em_pct   = float(env.get("em_pct",   0.0))
    hm_pct   = float(env.get("hm_pct",   0.0))
    gap_pct  = float(env.get("gap_pct",  0.0))
    vix      = float(env.get("vix",      20.0))
    env_score = float(env.get("env_score", 50.0))
    symbol   = env.get("symbol", "US.UNKNOWN")

    no_trade_reasons: list[str] = []

    # VIXパニック域は決算トレードも停止
    if vix > 50.0:
        no_trade_reasons.append(f"VIX={vix:.1f} > 50 — パニック域。決算トレード停止。")
        return {
            "primary":   {"strategy": "no_trade", "confidence": 1.0, "allocation": 1.0},
            "secondary": None,
            "reason":    " | ".join(no_trade_reasons),
            "no_trade_reasons": no_trade_reasons,
        }

    # ── 決算当日 (offset=0): ノートレード（流動性問題・スプレッド拡大）─────────
    if earnings_offset == 0:
        no_trade_reasons.append(
            f"{symbol.replace('US.','')} 決算当日 (offset=0) — スプレッド拡大リスク。エントリーなし。"
        )
        return {
            "primary":   {"strategy": "no_trade", "confidence": 0.9, "allocation": 1.0},
            "secondary": None,
            "reason":    " | ".join(no_trade_reasons),
            "no_trade_reasons": no_trade_reasons,
        }

    # ── 決算前日 (offset=1): IC / Calendar / Straddle ─────────────────────────
    if earnings_offset == 1:
        if em_pct <= 0 or hm_pct <= 0:
            no_trade_reasons.append(
                f"EM={em_pct:.1f}% または HM={hm_pct:.1f}% が0以下 — データ不足。エントリーなし。"
            )
            return {
                "primary":   {"strategy": "no_trade", "confidence": 0.8, "allocation": 1.0},
                "secondary": None,
                "reason":    " | ".join(no_trade_reasons),
                "no_trade_reasons": no_trade_reasons,
            }

        em_hm_ratio = em_pct / hm_pct  # > 1.0 → IV膨張 → 売り有利

        # EM が HM の 80% 未満 → 実際の動きが市場予想を超える可能性大 → Straddle買い
        if em_pct < hm_pct * 0.80:
            reason = (
                f"{symbol.replace('US.','')} 決算前日: EM={em_pct:.1f}% < HM={hm_pct:.1f}%×0.8 "
                f"→ 実動き過小予想 → Straddle買い"
            )
            return {
                "primary":   {"strategy": "earnings_straddle", "confidence": 0.65, "allocation": 0.75},
                "secondary": None,
                "reason":    reason,
                "no_trade_reasons": [],
            }

        # EM が HM の 1.1倍以上 → IVが膨張 → IC売り
        if em_hm_ratio >= 1.10:
            reason = (
                f"{symbol.replace('US.','')} 決算前日: EM/HM={em_hm_ratio:.2f} >= 1.10 "
                f"→ IV膨張 → Earnings IC売り (EM={em_pct:.1f}%, HM={hm_pct:.1f}%)"
            )
            return {
                "primary":   {"strategy": "earnings_ic", "confidence": 0.70, "allocation": 0.75},
                "secondary": None,
                "reason":    reason,
                "no_trade_reasons": [],
            }

        # EM が HM の 0.8〜1.1倍 → 中間域 → Calendar Spread (term structure活用)
        reason = (
            f"{symbol.replace('US.','')} 決算前日: EM/HM={em_hm_ratio:.2f} 中間域 "
            f"→ Calendar Spread (front期IV Crush期待)"
        )
        return {
            "primary":   {"strategy": "earnings_calendar", "confidence": 0.60, "allocation": 0.70},
            "secondary": None,
            "reason":    reason,
            "no_trade_reasons": [],
        }

    # ── 決算翌日 (offset=-1): Post-Earnings CS ─────────────────────────────────
    if earnings_offset == -1:
        if abs(gap_pct) < 1.0:
            no_trade_reasons.append(
                f"{symbol.replace('US.','')} 決算翌日: |gap|={abs(gap_pct):.1f}% < 1.0% — 方向性不明確。エントリーなし。"
            )
            return {
                "primary":   {"strategy": "no_trade", "confidence": 0.7, "allocation": 1.0},
                "secondary": None,
                "reason":    " | ".join(no_trade_reasons),
                "no_trade_reasons": no_trade_reasons,
            }

        # gap_pct > 0 (Gap Up) → Bull Put CS売り
        # gap_pct < 0 (Gap Down) → Bear Call CS売り
        gap_direction = "bull" if gap_pct > 0 else "bear"
        # EM の1.5倍を超えるgapは過大反応の可能性 → 逆張り・confidence低め
        em_ref = em_pct if em_pct > 0 else abs(gap_pct) * 0.8
        is_overshooting = abs(gap_pct) > em_ref * 1.5
        if is_overshooting:
            reason = (
                f"{symbol.replace('US.','')} 決算翌日: gap={gap_pct:.1f}% > EM×1.5={em_ref*1.5:.1f}% "
                f"→ 過大反応逆張りCS (confidence低)"
            )
            confidence = 0.45
        else:
            reason = (
                f"{symbol.replace('US.','')} 決算翌日: gap={gap_pct:.1f}% ({gap_direction}) "
                f"→ Post-Earnings方向性CS"
            )
            confidence = 0.60

        return {
            "primary":   {"strategy": "post_earnings_cs", "confidence": confidence, "allocation": 0.60},
            "secondary": None,
            "reason":    reason,
            "no_trade_reasons": [],
        }

    # 上記以外 (2日前以降など) → 通常の select_strategy に委ねる
    no_trade_reasons.append(
        f"{symbol.replace('US.','')} earnings_offset={earnings_offset}: 決算戦術対象外 (前日/翌日のみ)"
    )
    return {
        "primary":   {"strategy": "no_trade", "confidence": 0.5, "allocation": 1.0},
        "secondary": None,
        "reason":    " | ".join(no_trade_reasons),
        "no_trade_reasons": no_trade_reasons,
    }


# ── テスト用シナリオ ─────────────────────────────────────────────────────────

def _run_tests():
    """複数の環境シナリオで select_strategy() をテストする。

    期待される戦術選択と一致するかを確認する。
    """
    import json

    # 過去60日のVIX履歴（現実的な分布に基づく設計）
    # 低VIX時代: avg≈13, 範囲10〜18 → P30≈11.5, P70≈14.0, P95≈17.0
    import random
    rng = random.Random(42)
    vix_hist_low = sorted([10.0 + rng.uniform(0, 8) for _ in range(60)])
    # 中VIX時代: avg≈18, 範囲13〜28 → P30≈15.5, P70≈20.5, P95≈26.0
    vix_hist_mid = sorted([13.0 + rng.uniform(0, 15) for _ in range(60)])
    # 高VIX時代: avg≈27, 範囲18〜45 → P30≈22, P70≈33, P95≈42→cap30
    vix_hist_high = sorted([18.0 + rng.uniform(0, 27) for _ in range(60)])

    scenarios = [
        # ─────────────────────────────────────────────────────────────────
        # シナリオ1: 穏やかな火曜日（教科書的な売り環境）
        # 期待: IC売り または CS売り（high confidence）
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "穏やかな火曜日",
            "env": {
                "vix": 13.5, "vix_rate": -0.5, "vrp": 4.0,
                "gex": 2e9, "term_struct": 0.92,
                "env_score": 85, "gap_pct": 0.1,
                "vix_history": vix_hist_low, "bias": "neutral",
            },
            "expect_primary": ["ic_sell", "cs_sell"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ2: 中ボラで方向性ありの日（CS/ORB混合）
        # 期待: CS売り主体 or ORB主体（mixed）
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "中ボラ・方向性あり（bull bias）",
            "env": {
                "vix": 18.0, "vix_rate": 1.5, "vrp": 1.5,
                "gex": -5e8, "term_struct": 1.02,
                "env_score": 72, "gap_pct": 0.5,
                "vix_history": vix_hist_mid, "bias": "bull",
            },
            "expect_primary": ["cs_sell", "orb_buy"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ3: VIXスパイク（高ボラ・方向性あり）
        # 期待: ORB買い主体
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "VIXスパイク・方向性あり",
            "env": {
                "vix": 26.0, "vix_rate": 8.0, "vrp": -2.5,
                "gex": -2e9, "term_struct": 1.15,
                "env_score": 65, "gap_pct": 1.2,
                "vix_history": vix_hist_mid, "bias": "bear",
            },
            "expect_primary": ["orb_buy"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ4: パニック域
        # 期待: no_trade（または限定ORB）
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "パニック域（VIX>30）",
            "env": {
                "vix": 35.0, "vix_rate": 15.0, "vrp": -5.0,
                "gex": -3e9, "term_struct": 1.25,
                "env_score": 55, "gap_pct": 2.5,
                "vix_history": vix_hist_high, "bias": "bear",
            },
            "expect_primary": ["no_trade", "orb_buy"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ5: env_score < 50（経済イベント等）
        # 期待: no_trade
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "経済イベント日（env_score低）",
            "env": {
                "vix": 16.0, "vix_rate": 0.5, "vrp": 2.0,
                "gex": 5e8, "term_struct": 0.95,
                "env_score": 40, "gap_pct": 0.3,
                "vix_history": vix_hist_low, "bias": "neutral",
            },
            "expect_primary": ["no_trade"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ6: 暴落後の回復（VIX高だがVRPも高い日）
        # 期待: CS縮小 or ORB主体
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "暴落後の回復日",
            "env": {
                "vix": 24.0, "vix_rate": -4.0, "vrp": 6.0,
                "gex": 2e8, "term_struct": 1.00,
                "env_score": 68, "gap_pct": 0.8,
                "vix_history": vix_hist_mid, "bias": "bull",
            },
            "expect_primary": ["cs_sell", "orb_buy"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ7: VIXデータなし（history空）
        # 期待: フォールバック閾値を使いノートレードでなく正常な選択
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "VIX履歴なし（データ不足フォールバック）",
            "env": {
                "vix": 15.0, "vix_rate": 0.0, "vrp": 3.0,
                "gex": None, "term_struct": None,
                "env_score": 75, "gap_pct": None,
                "vix_history": [], "bias": "neutral",
            },
            "expect_primary": ["ic_sell", "cs_sell"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ8: 方向不明・高ボラ（ストラドル候補）
        # 期待: straddle_buy
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "高ボラ・方向不明（ストラドル候補）",
            "env": {
                "vix": 27.0, "vix_rate": 2.0, "vrp": -1.0,
                "gex": -3e8, "term_struct": 1.08,
                "env_score": 60, "gap_pct": 0.9,
                "vix_history": vix_hist_high, "bias": "neutral",
            },
            "expect_primary": ["straddle_buy", "orb_buy"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ9: 低IVR × 方向感なし（Butterfly候補）
        # 低VIX × IVR<30 × 中立バイアス → Butterfly
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "低IVR × 方向感なし（Butterfly候補）",
            "env": {
                "vix": 13.0, "vix_rate": -0.3, "vrp": 1.5,
                "gex": 5e8, "term_struct": 0.93, "vix_term_ratio": 0.82,
                "ivr": 22.0,  # IVR < 30 → Butterfly環境
                "env_score": 78, "gap_pct": 0.1,
                "vix_history": vix_hist_low, "bias": "neutral",
            },
            "expect_primary": ["butterfly"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ10: 高IVR × コンタンゴ × 方向感なし（Calendar候補）
        # 中ボラ × IVR>60 × term_ratio<0.85 → Calendar売り
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "高IVR × コンタンゴ × 方向感なし（Calendar候補）",
            "env": {
                "vix": 20.0, "vix_rate": -1.0, "vrp": 3.0,
                "gex": 1e9, "term_struct": 0.92, "vix_term_ratio": 0.80,
                "ivr": 72.0,  # IVR > 60 → Calendar/Strangle環境
                "env_score": 75, "gap_pct": 0.2,
                "vix_history": vix_hist_mid, "bias": "neutral",
            },
            "expect_primary": ["calendar_sell"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ11: 高IVR × バックワーデーション × 売り環境
        # 中ボラ低帯 × IVR高 × sell_regime × term_ratio>1.05 →
        # sell_regime+vrp_strongly_positiveが優先 → CS売り
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "高IVR × バックワーデーション × 売り環境",
            "env": {
                "vix": 18.0, "vix_rate": 0.5, "vrp": 2.5,
                "gex": 8e8, "term_struct": 1.00, "vix_term_ratio": 1.08,
                "ivr": 68.0,  # IVR高 × term_ratio > 1.05 (バックワーデーション)
                "env_score": 70, "gap_pct": 0.3,
                "vix_history": vix_hist_mid, "bias": "neutral",
            },
            # sell_regime+vrp_strongly_positiveが先に評価されてCS売り（合理的）
            "expect_primary": ["strangle_sell", "cs_sell"],
        },
        # ─────────────────────────────────────────────────────────────────
        # シナリオ12: 低ボラ × IVR高 × GEX強正 × コンタンゴ × 方向感なし
        # VIX履歴によってzone=1に入り、Calendar売りまたはStrangle売りが適切
        # ─────────────────────────────────────────────────────────────────
        {
            "name": "低ボラ × 高IVR × コンタンゴ × 方向感なし",
            "env": {
                "vix": 12.0, "vix_rate": -0.5, "vrp": 5.0,
                "gex": 2.5e9, "term_struct": 0.90, "vix_term_ratio": 0.83,
                "ivr": 78.0,  # IVR高 + GEX強正 + コンタンゴ → Calendar/Strangle
                "env_score": 82, "gap_pct": 0.1,
                "vix_history": vix_hist_low, "bias": "neutral",
            },
            # zone判定次第でStrangle(zone=0)またはCalendar(zone=1)が選ばれる
            "expect_primary": ["strangle_sell", "calendar_sell"],
        },
    ]

    print("=" * 72)
    print("strategy_selector.py — シナリオテスト")
    print("=" * 72)

    all_passed = True
    for sc in scenarios:
        result   = select_strategy(sc["env"])
        primary  = result["primary"]["strategy"]
        expected = sc["expect_primary"]
        passed   = primary in expected

        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False

        print(f"\n[{status}] {sc['name']}")
        print(f"  VIX={sc['env']['vix']} env_score={sc['env']['env_score']}")
        print(f"  thresholds: {result['thresholds']}")
        print(f"  primary  : {result['primary']}")
        print(f"  secondary: {result['secondary']}")
        print(f"  reason   : {result['reason']}")
        if result["no_trade_reasons"]:
            print(f"  no_trade : {result['no_trade_reasons']}")
        if not passed:
            print(f"  [FAIL] expected one of {expected}, got '{primary}'")

    print("\n" + "=" * 72)
    if all_passed:
        print("全テスト PASS")
    else:
        print("一部テスト FAIL — 上記の [FAIL] 行を確認してください")
    print("=" * 72)

    return all_passed


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    import sys
    ok = _run_tests()
    sys.exit(0 if ok else 1)
