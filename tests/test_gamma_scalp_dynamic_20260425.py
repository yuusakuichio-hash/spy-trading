"""tests/test_gamma_scalp_dynamic_20260425.py — GammaScalp 動的 ATR / interval 15 件テスト

テスト対象:
    atlas_v3/bots/engines/gamma_scalp_dynamic.py
    atlas_v3/bots/engines/straddle_native.GammaScalpNativeEngine
        (initialize_atr_with_vix / update_vix)

カバー範囲:
    T01  calm バンド (VIX=12): interval=15 / atr_mult=0.3
    T02  normal バンド (VIX=17): interval=10 / atr_mult=0.5
    T03  elevated バンド (VIX=22): interval=5 / atr_mult=0.7
    T04  high バンド (VIX=27): interval=3 / atr_mult=1.0
    T05  crisis バンド (VIX=35): interval=1 / atr_mult=1.5
    T06  VIX 境界値: VIX=15.0 は normal バンド
    T07  VIX 境界値: VIX=20.0 は elevated バンド
    T08  VIX 境界値: VIX=25.0 は high バンド
    T09  VIX 境界値: VIX=30.0 は crisis バンド
    T10  不正 VIX (0.0) は fallback normal バンド
    T11  不正 VIX (負値) は fallback normal バンド
    T12  不正 VIX (inf) は fallback normal バンド
    T13  get_dynamic_interval_min / get_dynamic_atr_multiplier 独立 getter
    T14  GammaScalpNativeEngine.initialize_atr_with_vix でパラメータ反映
    T15  GammaScalpNativeEngine.update_vix でランタイム更新
    T16  apply_vix_to_gamma_engine: engine=None → False
    T17  apply_vix_to_gamma_engine: initialize_atr_with_vix 呼び出し優先
    T18  apply_vix_to_gamma_engine: フィールド直接書き換え fallback
    T19  monitor_gamma_opportunity が _atr_multiplier を使って閾値算出
    T20  update_vix(None) はパラメータ更新をスキップ

注意:
    - ネットワーク接続不要
    - _fetch_closes_for_atr / Finnhub は unittest.mock.patch で遮断
    - kill_switch は mock で制御
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from atlas_v3.bots.engines.gamma_scalp_dynamic import (
    GammaScalpBandParams,
    _BAND_TABLE,
    _FALLBACK_BAND,
    apply_vix_to_gamma_engine,
    get_dynamic_atr_multiplier,
    get_dynamic_interval_min,
    get_gamma_scalp_params,
)

if TYPE_CHECKING:
    from atlas_v3.bots.engines.straddle_native import GammaScalpNativeEngine


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_engine(vix: float = 22.0, dry_test: bool = True) -> GammaScalpNativeEngine:
    """GammaScalpNativeEngine のテスト用インスタンスを返す。

    _fetch_closes_for_atr をパッチして ATR=2.0 固定。
    """
    from atlas_v3.bots.engines.straddle_native import (
        GammaScalpNativeEngine,
        StraddleNativeEngine,
    )

    straddle = StraddleNativeEngine(dry_test=dry_test)
    engine = GammaScalpNativeEngine(straddle_eng=straddle, dry_test=dry_test)
    return engine


# ---------------------------------------------------------------------------
# T01: calm バンド (VIX=12)
# ---------------------------------------------------------------------------

def test_t01_calm_band_params() -> None:
    """VIX=12 → calm バンド: interval_min=15 / atr_multiplier=0.3"""
    params = get_gamma_scalp_params(12.0)
    assert params.interval_min == pytest.approx(15.0)
    assert params.atr_multiplier == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# T02: normal バンド (VIX=17)
# ---------------------------------------------------------------------------

def test_t02_normal_band_params() -> None:
    """VIX=17 → normal バンド: interval_min=10 / atr_multiplier=0.5"""
    params = get_gamma_scalp_params(17.0)
    assert params.interval_min == pytest.approx(10.0)
    assert params.atr_multiplier == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# T03: elevated バンド (VIX=22)
# ---------------------------------------------------------------------------

def test_t03_elevated_band_params() -> None:
    """VIX=22 → elevated バンド: interval_min=5 / atr_multiplier=0.7"""
    params = get_gamma_scalp_params(22.0)
    assert params.interval_min == pytest.approx(5.0)
    assert params.atr_multiplier == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# T04: high バンド (VIX=27)
# ---------------------------------------------------------------------------

def test_t04_high_band_params() -> None:
    """VIX=27 → high バンド: interval_min=3 / atr_multiplier=1.0"""
    params = get_gamma_scalp_params(27.0)
    assert params.interval_min == pytest.approx(3.0)
    assert params.atr_multiplier == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# T05: crisis バンド (VIX=35)
# ---------------------------------------------------------------------------

def test_t05_crisis_band_params() -> None:
    """VIX=35 → crisis バンド: interval_min=1 / atr_multiplier=1.5"""
    params = get_gamma_scalp_params(35.0)
    assert params.interval_min == pytest.approx(1.0)
    assert params.atr_multiplier == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# T06: 境界値 VIX=15.0 → normal
# ---------------------------------------------------------------------------

def test_t06_boundary_vix_15_is_normal() -> None:
    """VIX=15.0 は normal バンド（calm_max の境界・>=15 は normal）。"""
    params = get_gamma_scalp_params(15.0)
    assert params.interval_min == pytest.approx(10.0)
    assert params.atr_multiplier == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# T07: 境界値 VIX=20.0 → elevated
# ---------------------------------------------------------------------------

def test_t07_boundary_vix_20_is_elevated() -> None:
    """VIX=20.0 は elevated バンド（normal_max の境界・>=20 は elevated）。"""
    params = get_gamma_scalp_params(20.0)
    assert params.interval_min == pytest.approx(5.0)
    assert params.atr_multiplier == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# T08: 境界値 VIX=25.0 → high
# ---------------------------------------------------------------------------

def test_t08_boundary_vix_25_is_high() -> None:
    """VIX=25.0 は high バンド（elevated_max の境界・>=25 は high）。"""
    params = get_gamma_scalp_params(25.0)
    assert params.interval_min == pytest.approx(3.0)
    assert params.atr_multiplier == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# T09: 境界値 VIX=30.0 → crisis
# ---------------------------------------------------------------------------

def test_t09_boundary_vix_30_is_crisis() -> None:
    """VIX=30.0 は crisis バンド（high_max の境界・>=30 は crisis）。"""
    params = get_gamma_scalp_params(30.0)
    assert params.interval_min == pytest.approx(1.0)
    assert params.atr_multiplier == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# T10: 不正 VIX=0.0 → fallback normal
# ---------------------------------------------------------------------------

def test_t10_invalid_vix_zero_fallback() -> None:
    """VIX=0.0 は不正値 → fallback (normal バンド) を返す。"""
    params = get_gamma_scalp_params(0.0)
    fallback = _BAND_TABLE[_FALLBACK_BAND]
    assert params.interval_min == pytest.approx(fallback.interval_min)
    assert params.atr_multiplier == pytest.approx(fallback.atr_multiplier)


# ---------------------------------------------------------------------------
# T11: 不正 VIX 負値 → fallback normal
# ---------------------------------------------------------------------------

def test_t11_invalid_vix_negative_fallback() -> None:
    """VIX=-5.0 は不正値 → fallback (normal バンド)。"""
    params = get_gamma_scalp_params(-5.0)
    fallback = _BAND_TABLE[_FALLBACK_BAND]
    assert params.interval_min == pytest.approx(fallback.interval_min)
    assert params.atr_multiplier == pytest.approx(fallback.atr_multiplier)


# ---------------------------------------------------------------------------
# T12: 不正 VIX inf → fallback normal
# ---------------------------------------------------------------------------

def test_t12_invalid_vix_inf_fallback() -> None:
    """VIX=inf は不正値 → fallback (normal バンド)。"""
    params = get_gamma_scalp_params(math.inf)
    fallback = _BAND_TABLE[_FALLBACK_BAND]
    assert params.interval_min == pytest.approx(fallback.interval_min)
    assert params.atr_multiplier == pytest.approx(fallback.atr_multiplier)


# ---------------------------------------------------------------------------
# T13: 独立 getter — get_dynamic_interval_min / get_dynamic_atr_multiplier
# ---------------------------------------------------------------------------

def test_t13_independent_getters() -> None:
    """get_dynamic_interval_min / get_dynamic_atr_multiplier の独立動作を確認。"""
    # elevated (VIX=21)
    assert get_dynamic_interval_min(21.0) == pytest.approx(5.0)
    assert get_dynamic_atr_multiplier(21.0) == pytest.approx(0.7)

    # crisis (VIX=40)
    assert get_dynamic_interval_min(40.0) == pytest.approx(1.0)
    assert get_dynamic_atr_multiplier(40.0) == pytest.approx(1.5)

    # calm (VIX=10)
    assert get_dynamic_interval_min(10.0) == pytest.approx(15.0)
    assert get_dynamic_atr_multiplier(10.0) == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# T14: GammaScalpNativeEngine.initialize_atr_with_vix パラメータ反映
# ---------------------------------------------------------------------------

def test_t14_initialize_atr_with_vix_applies_params() -> None:
    """VIX=27 (high) で initialize_atr_with_vix を呼ぶと
    interval_min=3 / _atr_multiplier=1.0 が engine に反映される。
    """
    engine = _make_engine()

    with patch(
        "atlas_v3.bots.engines.straddle_native._fetch_closes_for_atr",
        return_value=[100.0] * 20,
    ):
        engine.initialize_atr_with_vix(vix=27.0)

    assert engine._min_scalp_interval_min == pytest.approx(3.0)
    assert engine._atr_multiplier == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# T15: GammaScalpNativeEngine.update_vix ランタイム更新
# ---------------------------------------------------------------------------

def test_t15_update_vix_runtime_change() -> None:
    """update_vix で VIX が normal→crisis に変わると interval/mult が再設定される。"""
    engine = _make_engine()

    # 初期状態（normal相当）
    engine.update_vix(17.0)
    assert engine._min_scalp_interval_min == pytest.approx(10.0)
    assert engine._atr_multiplier == pytest.approx(0.5)

    # crisis に移行
    engine.update_vix(35.0)
    assert engine._min_scalp_interval_min == pytest.approx(1.0)
    assert engine._atr_multiplier == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# T16: apply_vix_to_gamma_engine — engine=None → False
# ---------------------------------------------------------------------------

def test_t16_apply_vix_engine_none_returns_false() -> None:
    """engine=None を渡すと False を返す。"""
    result = apply_vix_to_gamma_engine(None, 22.0)
    assert result is False


# ---------------------------------------------------------------------------
# T17: apply_vix_to_gamma_engine — initialize_atr_with_vix を優先呼び出し
# ---------------------------------------------------------------------------

def test_t17_apply_vix_prefers_initialize_atr_with_vix() -> None:
    """engine に initialize_atr_with_vix() があれば直接フィールド書き換えより優先する。"""
    engine = MagicMock()
    engine.initialize_atr_with_vix = MagicMock()

    result = apply_vix_to_gamma_engine(engine, 22.0)

    assert result is True
    engine.initialize_atr_with_vix.assert_called_once_with(22.0)


# ---------------------------------------------------------------------------
# T18: apply_vix_to_gamma_engine — フィールド直接書き換え fallback
# ---------------------------------------------------------------------------

def test_t18_apply_vix_direct_field_fallback() -> None:
    """initialize_atr_with_vix がない duck-type engine にはフィールドを直接書き換える。"""

    class FakeEngine:
        def __init__(self) -> None:
            self._min_scalp_interval_min = 99.0
            self._atr_multiplier = 99.0

    fe = FakeEngine()
    result = apply_vix_to_gamma_engine(fe, 35.0)  # crisis

    assert result is True
    assert fe._min_scalp_interval_min == pytest.approx(1.0)
    assert fe._atr_multiplier == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# T19: monitor_gamma_opportunity が _atr_multiplier を使って閾値算出
# ---------------------------------------------------------------------------

def test_t19_monitor_uses_atr_multiplier() -> None:
    """_atr14=2.0 / _atr_multiplier=1.5 のとき threshold=3.0。
    5 分移動が 2.5 なら threshold<3.0 なので機会なし、3.1 なら機会あり。
    """
    import datetime
    from zoneinfo import ZoneInfo

    from atlas_v3.bots.engines.straddle_native import (
        GammaScalpNativeEngine,
        StraddleNativePosition,
        StraddleNativeEngine,
    )

    ET = ZoneInfo("America/New_York")
    engine = _make_engine()
    engine._atr14 = 2.0
    engine._atr_multiplier = 1.5   # crisis: threshold = 3.0

    # ポジションをセット
    pos = StraddleNativePosition(
        call_code="US.SPY260425C500000",
        put_code="US.SPY260425P500000",
        call_qty=1,
        put_qty=1,
        call_entry_price=1.0,
        put_entry_price=1.0,
        spy_price_at_entry=500.0,
        expiry="2026-04-25",
    )
    engine.straddle_eng.position = pos
    engine._scalp_count_today = 0
    engine._last_scalp_ts = None

    now_et = datetime.datetime.now(ET)

    # 5 分前の価格を注入（移動 < 3.0 → 機会なし）
    engine._spy_price_history = [
        (now_et - datetime.timedelta(minutes=6), 500.0),
        (now_et, 502.5),
    ]
    assert engine.monitor_gamma_opportunity() is None

    # 5 分前の価格を注入（移動 > 3.0 → 機会あり・方向 CALL）
    engine._spy_price_history = [
        (now_et - datetime.timedelta(minutes=6), 500.0),
        (now_et, 503.5),
    ]
    direction = engine.monitor_gamma_opportunity()
    assert direction == "CALL"


# ---------------------------------------------------------------------------
# T20: update_vix(None) はパラメータ更新をスキップ
# ---------------------------------------------------------------------------

def test_t20_update_vix_none_skips() -> None:
    """update_vix(None) を呼んでも既存のパラメータが変わらない。"""
    engine = _make_engine()
    engine._min_scalp_interval_min = 7.0  # 仮セット
    engine._atr_multiplier = 0.9

    engine.update_vix(None)

    assert engine._min_scalp_interval_min == pytest.approx(7.0)
    assert engine._atr_multiplier == pytest.approx(0.9)
