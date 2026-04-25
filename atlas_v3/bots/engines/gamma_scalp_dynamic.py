"""atlas_v3/bots/engines/gamma_scalp_dynamic.py — GammaScalp 動的 ATR / interval 層

VIX バンドに応じて GammaScalpNativeEngine の ATR 乗数 (atr_multiplier) と
ヘッジ間隔 (hedge_interval_min) を動的に算出する。

VIX 対応表（project_orb_qqq_iwm_gamma_mvp_20260421 仕様）:
    calm     (VIX < 15)          : interval_min=15  / atr_multiplier=0.3
    normal   (15 <= VIX < 20)    : interval_min=10  / atr_multiplier=0.5
    elevated (20 <= VIX < 25)    : interval_min= 5  / atr_multiplier=0.7
    high     (25 <= VIX < 30)    : interval_min= 3  / atr_multiplier=1.0
    crisis   (VIX >= 30)         : interval_min= 1  / atr_multiplier=1.5

設計方針:
    - dynamic_params.py の get_vix_band / VixBand を再利用（cross-import 可・同 atlas_v3 内）
    - GammaScalpNativeEngine には initialize_atr_with_vix() / update_vix() を追加
    - 各関数は VIX 不正時に static fallback（normal バンド値）を返す
    - spy_bot.py / chronos_bot.py への import 禁止
    - CC <= 20 規律
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from atlas_v3.bots.engines.dynamic_params import VixBand, get_vix_band, _is_valid_vix

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VIX 対応表定数
# ---------------------------------------------------------------------------

#: fallback バンド（VIX 不正時）
_FALLBACK_BAND: VixBand = "normal"


@dataclass(frozen=True)
class GammaScalpBandParams:
    """VIX バンド 1 段の GammaScalp パラメータ。

    Attributes:
        interval_min:    最短ヘッジ間隔（分）
        atr_multiplier:  ATR に掛けるトリガー乗数
    """
    interval_min: float
    atr_multiplier: float


#: VIX バンド → GammaScalp パラメータのルックアップ表
_BAND_TABLE: dict[VixBand, GammaScalpBandParams] = {
    "calm":     GammaScalpBandParams(interval_min=15.0, atr_multiplier=0.3),
    "normal":   GammaScalpBandParams(interval_min=10.0, atr_multiplier=0.5),
    "elevated": GammaScalpBandParams(interval_min=5.0,  atr_multiplier=0.7),
    "high":     GammaScalpBandParams(interval_min=3.0,  atr_multiplier=1.0),
    "crisis":   GammaScalpBandParams(interval_min=1.0,  atr_multiplier=1.5),
    "unknown":  GammaScalpBandParams(interval_min=10.0, atr_multiplier=0.5),  # fallback
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_gamma_scalp_params(vix: float) -> GammaScalpBandParams:
    """VIX 値から GammaScalp の動的パラメータ (interval_min, atr_multiplier) を返す。

    VIX が不正（None / inf / 負）の場合は normal バンドの値を fallback として返す。

    Args:
        vix: VIX 現在値

    Returns:
        GammaScalpBandParams: interval_min と atr_multiplier

    Examples:
        >>> get_gamma_scalp_params(12.0)   # calm
        GammaScalpBandParams(interval_min=15.0, atr_multiplier=0.3)
        >>> get_gamma_scalp_params(22.0)   # elevated
        GammaScalpBandParams(interval_min=5.0, atr_multiplier=0.7)
    """
    if not _is_valid_vix(vix):
        log.debug(
            "[GammaScalpDynamic.get_params] VIX invalid (%.4f): fallback normal band",
            vix if isinstance(vix, float) else 0.0,
        )
        return _BAND_TABLE[_FALLBACK_BAND]

    band = get_vix_band(vix)
    params = _BAND_TABLE[band]
    log.debug(
        "[GammaScalpDynamic.get_params] VIX=%.1f band=%s "
        "interval_min=%.0f atr_mult=%.1f",
        vix, band, params.interval_min, params.atr_multiplier,
    )
    return params


def get_dynamic_interval_min(vix: float) -> float:
    """VIX 値から動的ヘッジ間隔（分）を返す。

    Args:
        vix: VIX 現在値

    Returns:
        interval_min（float）: calm=15 / normal=10 / elevated=5 / high=3 / crisis=1
    """
    return get_gamma_scalp_params(vix).interval_min


def get_dynamic_atr_multiplier(vix: float) -> float:
    """VIX 値から動的 ATR 乗数を返す。

    Args:
        vix: VIX 現在値

    Returns:
        atr_multiplier（float）: calm=0.3 / normal=0.5 / elevated=0.7 / high=1.0 / crisis=1.5
    """
    return get_gamma_scalp_params(vix).atr_multiplier


def apply_vix_to_gamma_engine(engine: object, vix: float) -> bool:
    """GammaScalpNativeEngine インスタンスに VIX 由来の動的パラメータを適用する。

    engine が initialize_atr_with_vix() / update_vix() を持つ場合は
    それを優先して呼び出す。持たない場合は直接フィールドを書き換える。

    Args:
        engine: GammaScalpNativeEngine インスタンス（または互換 duck-type）
        vix:    VIX 現在値

    Returns:
        True  = 適用成功
        False = 適用失敗（engine が None など）
    """
    if engine is None:
        log.warning("[GammaScalpDynamic.apply_vix] engine=None → スキップ")
        return False

    params = get_gamma_scalp_params(vix)

    # initialize_atr_with_vix() が存在すれば優先呼び出し
    if hasattr(engine, "initialize_atr_with_vix") and callable(
        getattr(engine, "initialize_atr_with_vix")
    ):
        engine.initialize_atr_with_vix(vix)
        return True

    # フォールバック: 直接フィールドを書き換え
    _apply_fields_direct(engine, params, vix)
    return True


def _apply_fields_direct(engine: object, params: GammaScalpBandParams, vix: float) -> None:
    """engine のフィールドを直接書き換えて動的パラメータを適用する。

    対象フィールド:
        _min_scalp_interval_min  (GammaScalpNativeEngine)
        _atr_multiplier          (将来拡張用)

    Args:
        engine: 対象 engine インスタンス
        params: 適用するパラメータ
        vix:    VIX 現在値（ログ用）
    """
    if hasattr(engine, "_min_scalp_interval_min"):
        old_val = getattr(engine, "_min_scalp_interval_min", None)
        engine._min_scalp_interval_min = params.interval_min  # type: ignore[attr-defined]
        log.info(
            "[GammaScalpDynamic] VIX=%.1f: interval_min %s → %.0f",
            vix, old_val, params.interval_min,
        )

    if hasattr(engine, "_atr_multiplier"):
        engine._atr_multiplier = params.atr_multiplier  # type: ignore[attr-defined]
        log.debug(
            "[GammaScalpDynamic] VIX=%.1f: atr_multiplier → %.1f",
            vix, params.atr_multiplier,
        )
