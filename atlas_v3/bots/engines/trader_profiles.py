"""atlas_v3/bots/engines/trader_profiles.py — 多軸トレーダープロファイル層

概要:
    VIX 単軸の dynamic_params.py に加え、「優秀トレーダー選定基準」の
    多軸パラメータ（Kelly 係数 / DD 上限 / IVR 下限 / VIX 上限 /
    Greeks 予算 / Term-structure フィルタ / 決算近接日数 / 勝率閾値）を
    プロファイルとして束ね、Engine Config への chain override を提供する。

プロファイル選定根拠 (data/trader_evaluation_framework.md より):
    - 勝率ベンチマーク 66-70% (Option Alpha 230k trade)
    - Sortino >2.0 / Calmar >3.0 / Return on Margin >10%
    - BP 使用率上限 50% / デイリーリスク 1-2%
    - Kelly fraction: 理論フル Kelly は破産確率が高い
      → half-Kelly (0.5) が実践的上限 (Vince, Tharp)
      → conservative は quarter-Kelly (0.25)

設計原則:
    - spy_bot.py / chronos_bot.py / common/* は一切 import しない
    - CC <= 20 規律を各関数で遵守
    - apply_dynamic_overrides (dynamic_params.py) と chain で使用可能
    - frozen dataclass = 不変プロファイル / deep copy 安全
"""
from __future__ import annotations

import dataclasses
import logging
import math
import statistics
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TraderProfile dataclass
# ---------------------------------------------------------------------------

ProfileName = Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE", "TOP100_TRADER"]

RegimeLabel = Literal["trending", "mean_reverting", "volatile", "unknown"]


@dataclass(frozen=True)
class TraderProfile:
    """多軸トレーダープロファイル。

    Attributes:
        name:                    プロファイル識別名
        kelly_fraction:          Kelly 係数（0.0 < x <= 0.5）
                                 0.25 = quarter-Kelly / 0.50 = half-Kelly
        drawdown_cap_pct:        1 トレード当たり最大 DD 上限（口座比 %）
                                 例: 0.01 = 1%
        ivr_min:                 エントリー許容最低 IVR
        vix_max:                 エントリー許容最大 VIX
        greeks_budget_delta:     ポートフォリオ net-delta 上限（絶対値）
        greeks_budget_gamma:     ポートフォリオ net-gamma 上限（絶対値）
        greeks_budget_vega:      ポートフォリオ net-vega 上限（絶対値）
        term_structure_filter:   term-structure 要件
                                 "contango" / "backwardation" / "any"
        earnings_proximity_days: 決算発表まで X 日以内はエントリー回避
        win_rate_threshold:      戦術採用最低勝率（0.0-1.0）
    """
    name: ProfileName
    kelly_fraction: float
    drawdown_cap_pct: float
    ivr_min: float
    vix_max: float
    greeks_budget_delta: float
    greeks_budget_gamma: float
    greeks_budget_vega: float
    term_structure_filter: Literal["contango", "backwardation", "any"]
    earnings_proximity_days: int
    win_rate_threshold: float


# ---------------------------------------------------------------------------
# 4 preset profiles
# ---------------------------------------------------------------------------

CONSERVATIVE = TraderProfile(
    name="CONSERVATIVE",
    kelly_fraction=0.25,          # quarter-Kelly — 最小リスク
    drawdown_cap_pct=0.005,       # 0.5% / trade — 厳格 DD 管理
    ivr_min=55.0,                 # 高 IVR 環境限定
    vix_max=20.0,                 # calm / normal 帯のみ
    greeks_budget_delta=0.10,     # 狭い net-delta 許容
    greeks_budget_gamma=0.03,     # gamma リスク最小化
    greeks_budget_vega=50.0,      # vega 曝露抑制
    term_structure_filter="contango",  # 順構造のみ
    earnings_proximity_days=7,    # 決算 7 日前回避
    win_rate_threshold=0.65,      # 65% 以上でなければ見送り
)

BALANCED = TraderProfile(
    name="BALANCED",
    kelly_fraction=0.35,          # 35% Kelly — 中庸
    drawdown_cap_pct=0.010,       # 1.0% / trade
    ivr_min=40.0,                 # 中程度の IVR も許容
    vix_max=25.0,                 # elevated まで許容
    greeks_budget_delta=0.20,
    greeks_budget_gamma=0.06,
    greeks_budget_vega=100.0,
    term_structure_filter="any",
    earnings_proximity_days=5,
    win_rate_threshold=0.55,
)

AGGRESSIVE = TraderProfile(
    name="AGGRESSIVE",
    kelly_fraction=0.50,          # half-Kelly — 実践的最大
    drawdown_cap_pct=0.020,       # 2.0% / trade
    ivr_min=25.0,                 # 低 IVR でもエントリー
    vix_max=35.0,                 # high / crisis 帯まで許容
    greeks_budget_delta=0.40,
    greeks_budget_gamma=0.12,
    greeks_budget_vega=200.0,
    term_structure_filter="any",
    earnings_proximity_days=2,
    win_rate_threshold=0.45,
)

TOP100_TRADER = TraderProfile(
    name="TOP100_TRADER",
    kelly_fraction=0.40,          # 40% Kelly — ドローダウン制御重視
    drawdown_cap_pct=0.015,       # 1.5% / trade (プロ水準)
    ivr_min=50.0,                 # 高 IV 環境選別
    vix_max=30.0,                 # high 帯まで — ただし Greeks 予算で制御
    greeks_budget_delta=0.25,
    greeks_budget_gamma=0.08,
    greeks_budget_vega=150.0,
    term_structure_filter="contango",  # term-structure に厳格
    earnings_proximity_days=5,
    win_rate_threshold=0.60,      # 60% 勝率 — Theta Profits ベンチマーク準拠
)

_PROFILE_REGISTRY: dict[ProfileName, TraderProfile] = {
    "CONSERVATIVE": CONSERVATIVE,
    "BALANCED": BALANCED,
    "AGGRESSIVE": AGGRESSIVE,
    "TOP100_TRADER": TOP100_TRADER,
}


# ---------------------------------------------------------------------------
# profile_selector
# ---------------------------------------------------------------------------

def profile_selector(name: str) -> TraderProfile:
    """プロファイル名から TraderProfile を返す。

    Args:
        name: "CONSERVATIVE" / "BALANCED" / "AGGRESSIVE" / "TOP100_TRADER"

    Returns:
        対応する TraderProfile

    Raises:
        KeyError: 未登録名の場合
    """
    key = name.upper()
    if key not in _PROFILE_REGISTRY:
        available = list(_PROFILE_REGISTRY.keys())
        raise KeyError(f"TraderProfile '{name}' は未登録です。利用可能: {available}")
    return _PROFILE_REGISTRY[key]  # type: ignore[index]


# ---------------------------------------------------------------------------
# apply_trader_profile — Engine Config への override 層
# ---------------------------------------------------------------------------

def apply_trader_profile(config: object, profile: TraderProfile) -> object:
    """TraderProfile を Engine Config dataclass へ適用して新インスタンスを返す。

    dynamic_params.apply_dynamic_overrides() と chain して使用する。
    本関数は VIX 非依存の静的プロファイル override を担当する。

    対応フィールド（存在しない場合はスキップ）:
        ivr_min          → profile.ivr_min (より保守的な側を採用)
        vix_max          → profile.vix_max (より保守的な側を採用)

    数値競合ポリシー:
        ivr_min: max(config.ivr_min, profile.ivr_min)  — 高い方を採用（厳格化）
        vix_max: min(config.vix_max, profile.vix_max)  — 低い方を採用（保守化）

    Args:
        config:  各エンジンの Config dataclass インスタンス
        profile: 適用する TraderProfile

    Returns:
        override 済みの新 Config インスタンス（型は入力と同一）
    """
    if not dataclasses.is_dataclass(config):
        log.warning("[TraderProfile.apply] config は dataclass ではない: no-op")
        return config

    changes: dict[str, object] = {}
    fields = {f.name for f in dataclasses.fields(config)}  # type: ignore[arg-type]

    if "ivr_min" in fields:
        current = float(getattr(config, "ivr_min"))
        new_val = max(current, profile.ivr_min)
        if new_val != current:
            changes["ivr_min"] = new_val

    if "vix_max" in fields:
        current = float(getattr(config, "vix_max"))
        new_val = min(current, profile.vix_max)
        if new_val != current:
            changes["vix_max"] = new_val

    if not changes:
        return config

    try:
        new_cfg = dataclasses.replace(config, **changes)  # type: ignore[type-var]
        log.info(
            "[TraderProfile.apply] %s + profile=%s: overrides=%s",
            type(config).__name__, profile.name, list(changes.keys()),
        )
        return new_cfg
    except Exception as exc:  # pragma: no cover
        log.warning("[TraderProfile.apply] replace 失敗 (%s): 元 config を返す", exc)
        return config


# ---------------------------------------------------------------------------
# 追加軸 getter 群
# ---------------------------------------------------------------------------

def get_kelly_sizing(
    win_rate: float,
    payoff_ratio: float,
    cap: float,
    profile: TraderProfile,
) -> float:
    """Kelly 基準サイジング（口座比率）を計算して返す。

    Kelly 公式: f* = (win_rate * payoff_ratio - loss_rate) / payoff_ratio
    profile.kelly_fraction で上限制御（half-Kelly / quarter-Kelly）。
    profile.drawdown_cap_pct でさらに上限キャップ。

    Args:
        win_rate:      期待勝率（0.0 < x < 1.0）
        payoff_ratio:  avg_win / avg_loss（> 0）
        cap:           外部指定の上限係数（0.0 < x <= 1.0）
        profile:       適用 TraderProfile

    Returns:
        実効リスク比率（0.0 ~ min(cap, profile.drawdown_cap_pct * 10)）
        負の Kelly（期待値 <= 0）は 0.0 を返す（no-trade シグナル）
    """
    if not (0.0 < win_rate < 1.0) or payoff_ratio <= 0.0 or cap <= 0.0:
        log.debug("[TraderProfile.kelly] 不正入力: 0.0 を返す")
        return 0.0

    loss_rate = 1.0 - win_rate
    full_kelly = (win_rate * payoff_ratio - loss_rate) / payoff_ratio

    if full_kelly <= 0.0:
        log.debug("[TraderProfile.kelly] 期待値 <= 0: f*=%.4f → 0.0", full_kelly)
        return 0.0

    sized = full_kelly * profile.kelly_fraction
    upper = min(cap, profile.drawdown_cap_pct * 10.0)
    result = round(min(sized, upper), 6)
    log.debug(
        "[TraderProfile.kelly] win=%.3f payoff=%.2f f*=%.4f "
        "×%.2f=%.4f cap=%.4f → %.6f",
        win_rate, payoff_ratio, full_kelly, profile.kelly_fraction, sized, upper, result,
    )
    return result


def get_greeks_budget_check(
    portfolio_greeks: dict[str, float],
    profile: TraderProfile,
) -> tuple[bool, str]:
    """ポートフォリオ Greeks が予算内かを検証する。

    Args:
        portfolio_greeks: {"delta": float, "gamma": float, "vega": float}
                          各値は net 値（絶対値で評価）
        profile:          適用 TraderProfile

    Returns:
        (ok: bool, reason: str)
        ok=True   → 予算内 / エントリー許可
        ok=False  → 予算超過 / エントリー禁止 + reason に超過項目
    """
    violations: list[str] = []

    delta = abs(portfolio_greeks.get("delta", 0.0))
    gamma = abs(portfolio_greeks.get("gamma", 0.0))
    vega = abs(portfolio_greeks.get("vega", 0.0))

    if delta > profile.greeks_budget_delta:
        violations.append(
            f"delta={delta:.4f} > budget={profile.greeks_budget_delta:.4f}"
        )
    if gamma > profile.greeks_budget_gamma:
        violations.append(
            f"gamma={gamma:.4f} > budget={profile.greeks_budget_gamma:.4f}"
        )
    if vega > profile.greeks_budget_vega:
        violations.append(
            f"vega={vega:.2f} > budget={profile.greeks_budget_vega:.2f}"
        )

    if violations:
        reason = "Greeks 予算超過: " + "; ".join(violations)
        log.debug("[TraderProfile.greeks] %s", reason)
        return (False, reason)

    return (True, "Greeks 予算内")


def get_sharpe_adjusted_threshold(profile: TraderProfile) -> float:
    """プロファイルに応じた Sharpe ベース エントリー閾値を返す。

    勝率閾値と Kelly 係数から期待 Sharpe 比の下限を導出する。
    低 Kelly (保守) → 高閾値 / 高 Kelly (積極) → 低閾値。

    導出式:
        base_sharpe = win_rate_threshold / (1.0 - win_rate_threshold)
            → odds ratio をそのまま pseudo-Sharpe として利用
        scale = 1.0 / profile.kelly_fraction
            → Kelly が低い（= 保守）ほど閾値を引き上げる
        result = round(base_sharpe * scale, 4)  clamp [0.5, 5.0]

    Returns:
        float — entry 可否判定に使う Sharpe 比下限（>= この値で entry）
    """
    wt = profile.win_rate_threshold
    if not (0.0 < wt < 1.0):
        return 1.0

    base = wt / (1.0 - wt)
    scale = 1.0 / max(profile.kelly_fraction, 0.01)
    raw = base * scale
    result = round(max(0.5, min(5.0, raw)), 4)
    log.debug(
        "[TraderProfile.sharpe_thresh] profile=%s base=%.4f scale=%.2f → %.4f",
        profile.name, base, scale, result,
    )
    return result


def get_regime_filter(
    recent_price_series: list[float],
    profile: TraderProfile,
) -> RegimeLabel:
    """直近価格系列からレジームを判定して返す。

    アルゴリズム:
        1. 系列が短すぎる (< 10) → "unknown"
        2. 標準偏差 / 平均（変動係数 CV） > 0.015 → "volatile"
        3. 最初 5 本 vs 最後 5 本の平均差の絶対値 > 1σ → "trending"
        4. それ以外 → "mean_reverting"

    profile による出力補正:
        - CONSERVATIVE: volatile 時は常に "volatile" を返す（no-trade トリガー想定）
        - AGGRESSIVE: volatile でも vix_max が高いため補正なし

    Args:
        recent_price_series: 直近 N 本の終値リスト（古い順）
        profile:             適用 TraderProfile

    Returns:
        "trending" | "mean_reverting" | "volatile" | "unknown"
    """
    n = len(recent_price_series)
    if n < 10:
        return "unknown"

    mean = statistics.mean(recent_price_series)
    if mean == 0.0:
        return "unknown"

    stdev = statistics.stdev(recent_price_series)
    cv = stdev / abs(mean)

    volatile_thresh = 0.015
    if profile.name == "CONSERVATIVE":
        volatile_thresh = 0.010  # 保守プロファイルは感度を高める

    if cv > volatile_thresh:
        log.debug("[TraderProfile.regime] CV=%.4f > %.3f → volatile", cv, volatile_thresh)
        return "volatile"

    half = n // 2
    early_mean = statistics.mean(recent_price_series[:half])
    late_mean = statistics.mean(recent_price_series[half:])
    diff = abs(late_mean - early_mean)

    if diff > stdev:
        log.debug(
            "[TraderProfile.regime] diff=%.4f > σ=%.4f → trending", diff, stdev
        )
        return "trending"

    return "mean_reverting"
