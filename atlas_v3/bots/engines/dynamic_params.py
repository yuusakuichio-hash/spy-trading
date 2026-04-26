"""atlas_v3/bots/engines/dynamic_params.py — Adaptive parameter generator

VIX / IVR / ATR / gap に基づく動的パラメータ層。
spy_bot.py の calc_dynamic_profit_target / calc_dynamic_width / get_vix_band /
apply_vix_band_overrides パターンを踏襲し、atlas_v3 の 10 戦術 Config に
apply できる override layer として設計する。

バンド定義（spy_bot.get_vix_band と同一境界値）:
    calm      : VIX < 15
    normal    : 15 <= VIX < 20
    elevated  : 20 <= VIX < 25
    high      : 25 <= VIX < 30
    crisis    : VIX >= 30

各 getter の設計根拠:
    - get_dynamic_ivr_threshold  : VIX 高 → 相場全体の IV が上昇しているため ivr_min を緩め entry を増やす
    - get_dynamic_delta_range    : VIX 高 → テール・リスク増大・delta 範囲を広めて OTM 寄りに調整
    - get_dynamic_profit_target  : VIX 高 → プレミアム大 → 早期利確で期待値最大化 (spy_bot 踏襲)
    - get_dynamic_stop_loss      : VIX 高 → 急変リスク大 → stop 乗数を縮小して最大損失を抑制
    - get_dynamic_entry_window   : VIX spike (>= elevated) → OP 直後ノイズが大きい → 開始を遅らせる
    - get_dynamic_qty_sizing     : VIX 高 → ボラティリティ上昇でリスク量が増大 → size を縮小

全関数は static fallback（VIX 入力が不正な場合 base_* をそのまま返す）を備える。
spy_bot.py / common/kill_switch.py は一切 import しない。
CC <= 20 規律を各関数で遵守する。
"""
from __future__ import annotations

import logging
import math
from typing import Literal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VIX バンド境界（spy_bot.get_vix_band と同一）
# ---------------------------------------------------------------------------

VIX_CALM_MAX: float = 15.0
VIX_NORMAL_MAX: float = 20.0
VIX_ELEVATED_MAX: float = 25.0
VIX_HIGH_MAX: float = 30.0

VixBand = Literal["calm", "normal", "elevated", "high", "crisis", "unknown"]


# ---------------------------------------------------------------------------
# 内部ユーティリティ
# ---------------------------------------------------------------------------

def _is_valid_vix(vix: float) -> bool:
    """VIX が有限かつ正の値かどうか検証する。"""
    return math.isfinite(vix) and vix > 0.0


def get_vix_band(vix: float) -> VixBand:
    """VIX 値から VIX 帯名を返す。

    spy_bot.get_vix_band と同一ロジック（atlas_v3 内で再定義・cross-import 回避）。

    Args:
        vix: VIX 現在値

    Returns:
        "calm" / "normal" / "elevated" / "high" / "crisis" / "unknown"
    """
    if not _is_valid_vix(vix):
        return "unknown"
    if vix < VIX_CALM_MAX:
        return "calm"
    if vix < VIX_NORMAL_MAX:
        return "normal"
    if vix < VIX_ELEVATED_MAX:
        return "elevated"
    if vix < VIX_HIGH_MAX:
        return "high"
    return "crisis"


# ---------------------------------------------------------------------------
# 動的パラメータ getter 群
# ---------------------------------------------------------------------------

def get_dynamic_ivr_threshold(vix: float, base_ivr: float) -> float:
    """VIX に応じた動的 IVR 閾値を返す。

    VIX が高い環境では市場全体の IV が上昇しているため、ivr_min を緩めることで
    エントリー機会を増やす（高ボラ環境でプレミアム売り戦術の期待値が上昇する）。

    調整式:
        calm      : base_ivr + 5.0  (低ボラ → 厳格フィルタ)
        normal    : base_ivr + 0.0  (基準)
        elevated  : base_ivr - 5.0
        high      : base_ivr - 10.0
        crisis    : base_ivr - 15.0
        unknown   : base_ivr (fallback)

    Floor = 20.0, Cap = base_ivr + 10.0

    Args:
        vix:      VIX 現在値
        base_ivr: 設定ファイル上のベース IVR 閾値（0-100）

    Returns:
        調整後 IVR 閾値（float, [20.0, base_ivr+10.0]）
    """
    if not _is_valid_vix(vix):
        log.debug("[DynParams.ivr_threshold] VIX invalid (%.2f): fallback base_ivr=%.1f", vix, base_ivr)
        return base_ivr

    band = get_vix_band(vix)
    offsets: dict[VixBand, float] = {
        "calm": +5.0,
        "normal": 0.0,
        "elevated": -5.0,
        "high": -10.0,
        "crisis": -15.0,
        "unknown": 0.0,
    }
    adj = round(base_ivr + offsets[band], 2)
    result = max(20.0, min(base_ivr + 10.0, adj))
    log.debug("[DynParams.ivr_threshold] VIX=%.1f band=%s base=%.1f → %.1f", vix, band, base_ivr, result)
    return result


def get_dynamic_delta_range(vix: float, base_delta: float) -> tuple[float, float]:
    """VIX に応じた動的 delta 範囲 (min, max) を返す。

    VIX が高い環境ではテールリスクが増大するため、delta 範囲を広めて
    ストライクを OTM 側へシフトする。

    調整式（base_delta を midpoint として ±half_width を広げる）:
        calm      : half_width = 0.02 (狭め)
        normal    : half_width = 0.025
        elevated  : half_width = 0.03
        high      : half_width = 0.04
        crisis    : half_width = 0.05 (広め)

    Args:
        vix:        VIX 現在値
        base_delta: 設定 delta 中央値（例: short_put_delta_min + max の平均）

    Returns:
        (delta_min, delta_max): 各 floor=0.05 / cap=0.50 でクランプ
    """
    if not _is_valid_vix(vix):
        log.debug("[DynParams.delta_range] VIX invalid: fallback symmetric ±0.025")
        half = 0.025
        return (max(0.05, round(base_delta - half, 4)), min(0.50, round(base_delta + half, 4)))

    band = get_vix_band(vix)
    half_widths: dict[VixBand, float] = {
        "calm": 0.020,
        "normal": 0.025,
        "elevated": 0.030,
        "high": 0.040,
        "crisis": 0.050,
        "unknown": 0.025,
    }
    half = half_widths[band]
    d_min = max(0.05, round(base_delta - half, 4))
    d_max = min(0.50, round(base_delta + half, 4))
    log.debug("[DynParams.delta_range] VIX=%.1f band=%s base=%.3f → (%.3f, %.3f)", vix, band, base_delta, d_min, d_max)
    return (d_min, d_max)


def get_dynamic_profit_target(vix: float, base_pct: float) -> float:
    """VIX に応じた動的利確目標 (0.0-1.0) を返す。

    VIX が高いほどプレミアムが大きく・早期利確の期待値が高い。
    spy_bot.calc_dynamic_profit_target の VIX 係数 (0.01/pt) を踏襲する。

    調整式:
        adjusted = base_pct + (vix - 20.0) * (-0.005)
        → VIX 20 基準。VIX 上昇で利確目標を下げ（早めに確定）。
        Floor = 0.20, Cap = min(0.80, base_pct + 0.15)

    Args:
        vix:      VIX 現在値
        base_pct: ベース利確目標（0.0 < base_pct < 1.0）

    Returns:
        調整後 profit_target_pct（float）
    """
    if not _is_valid_vix(vix):
        return base_pct

    adjusted = base_pct + (vix - 20.0) * (-0.005)
    floor = 0.20
    cap = min(0.80, base_pct + 0.15)
    result = round(max(floor, min(cap, adjusted)), 4)
    log.debug("[DynParams.profit_target] VIX=%.1f base=%.2f → %.4f", vix, base_pct, result)
    return result


def get_dynamic_stop_loss(vix: float, base_mult: float) -> float:
    """VIX に応じた動的ストップロス乗数を返す。

    VIX が高いほど急変リスクが増大するため、stop 乗数を縮小して最大損失を抑制する。
    spy_bot.calc_dynamic_stop_loss の設計（VIX 上昇 → SL 締め）を踏襲する。

    調整式:
        adjusted = base_mult - (vix - 20.0) * 0.02
        Floor = 0.80, Cap = base_mult + 0.50

    Args:
        vix:       VIX 現在値
        base_mult: ベースのストップロス乗数（>= 1.0 を推奨）

    Returns:
        調整後 stop_loss 乗数（float）
    """
    if not _is_valid_vix(vix):
        return base_mult

    adjusted = base_mult - (vix - 20.0) * 0.02
    result = round(max(0.80, min(base_mult + 0.50, adjusted)), 4)
    log.debug("[DynParams.stop_loss] VIX=%.1f base=%.2f → %.4f", vix, base_mult, result)
    return result


def get_dynamic_entry_window(
    vix: float,
    base_start: int,
    base_end: int,
) -> tuple[int, int]:
    """VIX に応じた動的エントリーウィンドウ（ET 時刻・hour 単位）を返す。

    VIX >= elevated (>=20) のとき、オープニング直後のノイズが大きいため
    エントリー開始時刻を遅らせる。終了時刻は変更しない（クローズ猶予を確保）。

    遅延量:
        calm / normal : +0h  (変更なし)
        elevated      : +1h
        high          : +1h
        crisis        : +2h

    エントリー開始が終了を超える場合は (base_end - 1, base_end) にフォールバック。

    Args:
        vix:        VIX 現在値
        base_start: ベース開始時刻（ET 時）
        base_end:   ベース終了時刻（ET 時）

    Returns:
        (start_hour, end_hour) — ET 時
    """
    if not _is_valid_vix(vix):
        return (base_start, base_end)

    band = get_vix_band(vix)
    delays: dict[VixBand, int] = {
        "calm": 0,
        "normal": 0,
        "elevated": 1,
        "high": 1,
        "crisis": 2,
        "unknown": 0,
    }
    delay = delays[band]
    new_start = base_start + delay
    if new_start >= base_end:
        new_start = max(base_start, base_end - 1)
    log.debug(
        "[DynParams.entry_window] VIX=%.1f band=%s start=%d→%d end=%d",
        vix, band, base_start, new_start, base_end,
    )
    return (new_start, base_end)


def get_dynamic_qty_sizing(
    vix: float,
    cash: float,
    base_risk_pct: float,
) -> float:
    """VIX に応じた動的リスク予算（cash × 実効 risk_pct）を返す。

    VIX が高いほどポジション当たりのリスク量が増大するため、
    risk_pct を縮小して size を抑制する。

    縮小係数 (size_factor):
        calm      : 1.00
        normal    : 1.00
        elevated  : 0.80
        high      : 0.60
        crisis    : 0.40

    Args:
        vix:           VIX 現在値
        cash:          口座利用可能残高（正の値）
        base_risk_pct: ベースのリスク比率（例: 0.02 = 2%）

    Returns:
        リスク予算金額 = cash × base_risk_pct × size_factor
    """
    if not _is_valid_vix(vix) or cash <= 0.0 or base_risk_pct <= 0.0:
        log.debug("[DynParams.qty_sizing] invalid input: fallback base")
        return max(0.0, cash * base_risk_pct)

    band = get_vix_band(vix)
    factors: dict[VixBand, float] = {
        "calm": 1.00,
        "normal": 1.00,
        "elevated": 0.80,
        "high": 0.60,
        "crisis": 0.40,
        "unknown": 1.00,
    }
    factor = factors[band]
    budget = round(cash * base_risk_pct * factor, 4)
    log.debug(
        "[DynParams.qty_sizing] VIX=%.1f band=%s cash=%.2f risk_pct=%.4f factor=%.2f → %.4f",
        vix, band, cash, base_risk_pct, factor, budget,
    )
    return budget


# ---------------------------------------------------------------------------
# Config override layer — apply_dynamic_overrides()
# ---------------------------------------------------------------------------

def apply_dynamic_overrides(config: object, vix: float) -> object:
    """Config dataclass に動的パラメータ override を適用して新インスタンスを返す。

    各エンジンの Config は frozen=True のため、dataclasses.replace() で
    override した新インスタンスを生成する。

    対応フィールド（存在しない場合はスキップ）:
        ivr_min                  → get_dynamic_ivr_threshold
        profit_target_pct        → get_dynamic_profit_target
        profit_target_remaining_pct → get_dynamic_profit_target (0DTE strangle)
        profit_target_ratio      → get_dynamic_profit_target (diagonal)
        stop_loss_multiplier     → get_dynamic_stop_loss (jade_lizard)
        stop_loss_mult           → get_dynamic_stop_loss (0DTE strangle)
        stop_loss_credit_x       → get_dynamic_stop_loss (iron_fly, ratio_spread)
        stop_loss_ratio          → get_dynamic_stop_loss (pmcc, diagonal)
        stop_loss_pct            → get_dynamic_stop_loss (earnings_straddle, weekly_gamma)
        entry_window_start_et    → get_dynamic_entry_window (pmcc, diagonal)
        vix_max                  → 高 VIX 環境では vix_max を引き上げてフィルタを通す特例なし
                                   (vix_tail_hedge は VIX 上昇でむしろ entry したいため対象外)

    Args:
        config: 各エンジンの Config dataclass インスタンス（frozen=True / False 両対応）
        vix:    現在の VIX 値

    Returns:
        override 済みの新 Config インスタンス（型は入力と同一）
    """
    import dataclasses

    if not dataclasses.is_dataclass(config):
        log.warning("[DynParams.apply_overrides] config is not a dataclass: no-op")
        return config

    if not _is_valid_vix(vix):
        log.debug("[DynParams.apply_overrides] VIX invalid: no-op")
        return config

    changes: dict[str, object] = {}
    fields = {f.name for f in dataclasses.fields(config)}  # type: ignore[arg-type]

    # IVR threshold
    if "ivr_min" in fields:
        base = float(getattr(config, "ivr_min"))  # type: ignore[arg-type]
        changes["ivr_min"] = get_dynamic_ivr_threshold(vix, base)

    # Profit target
    for field_name in ("profit_target_pct", "profit_target_remaining_pct", "profit_target_ratio"):
        if field_name in fields:
            base = float(getattr(config, field_name))  # type: ignore[arg-type]
            changes[field_name] = get_dynamic_profit_target(vix, base)
            break  # 最初に見つかったフィールドのみ処理

    # Stop loss
    for field_name in (
        "stop_loss_multiplier", "stop_loss_mult", "stop_loss_credit_x",
        "stop_loss_ratio", "stop_loss_pct",
    ):
        if field_name in fields:
            base = float(getattr(config, field_name))  # type: ignore[arg-type]
            changes[field_name] = get_dynamic_stop_loss(vix, base)
            break  # 最初に見つかったフィールドのみ処理

    # Entry window start (hour-unit fields)
    if "entry_window_start_et" in fields and "entry_window_end_et" in fields:
        base_s = int(getattr(config, "entry_window_start_et"))  # type: ignore[arg-type]
        base_e = int(getattr(config, "entry_window_end_et"))  # type: ignore[arg-type]
        new_s, _ = get_dynamic_entry_window(vix, base_s, base_e)
        changes["entry_window_start_et"] = new_s

    if changes:
        try:
            new_cfg = dataclasses.replace(config, **changes)  # type: ignore[type-var]
            log.info(
                "[DynParams.apply_overrides] %s: VIX=%.1f applied %s",
                type(config).__name__, vix, list(changes.keys()),
            )
            return new_cfg
        except Exception as exc:  # pragma: no cover
            log.warning("[DynParams.apply_overrides] replace failed (%s): returning original", exc)
            return config

    return config
