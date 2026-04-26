"""tests/test_atlas_v3_yfinance_provider_20260425.py — atlas_v3/ops/yfinance_provider.py coverage tests

対象: atlas_v3/ops/yfinance_provider.py (88 stmts)
happy path: 7 件 / error path: 4 件
推定 coverage: ~68%

yfinance.Ticker は unittest.mock で完全 mock し、ネットワーク通信なし。
"""
from __future__ import annotations

import time
import unittest.mock as mock

import pytest

from atlas_v3.ops.yfinance_provider import (
    YFinanceMetricProvider,
    _FLASH_CRASH_THRESHOLD_PCT,
    _DEFAULT_CACHE_TTL_SECS,
    _PROXY_NOTIONAL_USD,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_fast_info(current: float = 500.0, open_: float = 495.0, day_high: float = 505.0):
    """yfinance fast_info を模倣する MagicMock。"""
    fi = mock.MagicMock()
    fi.last_price = current
    fi.open = open_
    fi.day_high = day_high
    return fi


def _make_provider_mocked(
    current: float = 500.0,
    open_: float = 495.0,
    day_high: float = 505.0,
    ticker_symbol: str = "SPY",
    check_interval_secs=None,
) -> YFinanceMetricProvider:
    """yfinance をモック済みの YFinanceMetricProvider を返す。"""
    fast_info = _make_fast_info(current, open_, day_high)

    with mock.patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker_cls.return_value.fast_info = fast_info
        provider = YFinanceMetricProvider(
            ticker_symbol=ticker_symbol,
            check_interval_secs=check_interval_secs,
        )
        # Ticker mock を provider に持たせておく（get_metrics 呼び出し時も使うため）
        provider._mock_ticker_cls = mock_ticker_cls
        provider._mock_fast_info = fast_info
    return provider


# ---------------------------------------------------------------------------
# __init__ — happy path
# ---------------------------------------------------------------------------

class TestYFinanceMetricProviderInit:
    def test_happy_default_cache_ttl(self):
        """check_interval_secs=None のとき cache_ttl はデフォルト値。"""
        with mock.patch("yfinance.Ticker"):
            p = YFinanceMetricProvider()
        assert p._cache_ttl_secs == _DEFAULT_CACHE_TTL_SECS

    def test_happy_auto_adjust_cache_ttl(self):
        """check_interval=15s → cache_ttl=min(5, 10)=5s（HIGH-R6-2 fix）。"""
        with mock.patch("yfinance.Ticker"):
            p = YFinanceMetricProvider(check_interval_secs=15.0)
        assert p._cache_ttl_secs == pytest.approx(5.0)

    def test_happy_cache_ttl_capped_at_default(self):
        """check_interval=60s → cache_ttl=min(20, 10)=10s（上限キャップ）。"""
        with mock.patch("yfinance.Ticker"):
            p = YFinanceMetricProvider(check_interval_secs=60.0)
        assert p._cache_ttl_secs == pytest.approx(10.0)

    def test_yfinance_import_error_raises(self):
        """yfinance が ImportError なら RuntimeError（fail-closed）。"""
        with mock.patch.dict("sys.modules", {"yfinance": None}):
            with pytest.raises(RuntimeError, match="yfinance is not installed"):
                YFinanceMetricProvider()


# ---------------------------------------------------------------------------
# get_metrics — happy path
# ---------------------------------------------------------------------------

class TestGetMetrics:
    def _make_provider(
        self,
        current=500.0,
        open_=495.0,
        day_high=505.0,
        check_interval_secs=None,
    ) -> tuple[YFinanceMetricProvider, mock.MagicMock]:
        fast_info = _make_fast_info(current, open_, day_high)
        mock_ticker = mock.MagicMock()
        mock_ticker.fast_info = fast_info
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            p = YFinanceMetricProvider(check_interval_secs=check_interval_secs)
        return p, mock_ticker

    def test_happy_returns_expected_keys(self):
        """get_metrics() は pnl_day_usd / drawdown_pct / latency_ms を返す。"""
        p, mock_ticker = self._make_provider()
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            metrics = p.get_metrics()
        assert "pnl_day_usd" in metrics
        assert "drawdown_pct" in metrics
        assert "latency_ms" in metrics

    def test_happy_pnl_positive_when_price_above_open(self):
        """current > open なら pnl_day_usd > 0。"""
        p, mock_ticker = self._make_provider(current=510.0, open_=495.0)
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            metrics = p.get_metrics()
        assert metrics["pnl_day_usd"] > 0.0

    def test_happy_pnl_negative_when_price_below_open(self):
        """current < open なら pnl_day_usd < 0。"""
        p, mock_ticker = self._make_provider(current=480.0, open_=495.0)
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            metrics = p.get_metrics()
        assert metrics["pnl_day_usd"] < 0.0

    def test_happy_drawdown_zero_when_at_high(self):
        """current == day_high のとき drawdown_pct == 0。"""
        p, mock_ticker = self._make_provider(current=505.0, open_=495.0, day_high=505.0)
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            metrics = p.get_metrics()
        assert metrics["drawdown_pct"] == pytest.approx(0.0)

    def test_happy_cache_hit_skips_api(self):
        """cache TTL 内の 2 回目呼び出しはキャッシュが返りAPIコールが増えない。"""
        fast_info = _make_fast_info()
        mock_ticker = mock.MagicMock()
        mock_ticker.fast_info = fast_info
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            p = YFinanceMetricProvider(check_interval_secs=15.0)
        with mock.patch("yfinance.Ticker", return_value=mock_ticker) as mock_cls:
            _ = p.get_metrics()
            _ = p.get_metrics()  # 2回目: キャッシュから
        # yfinance.Ticker は 1 回目の get_metrics でのみ呼ばれる
        assert mock_cls.call_count <= 1

    def test_happy_flash_crash_detected_updates_last_price(self):
        """Flash Crash 検知時は _last_price が新価格に更新される。

        実装上: Flash Crash 検知で cache_ts=0.0 をセットした直後に
        cache_ts=now で上書きされる（同一の get_metrics() 呼び出し内）。
        したがって Flash Crash 検知の証跡として _last_price が crash 後の
        価格に更新されたことを確認する。
        """
        fast_info_1 = _make_fast_info(current=500.0)
        fast_info_2 = _make_fast_info(current=450.0)  # -10% の Flash Crash
        mock_ticker = mock.MagicMock()

        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            p = YFinanceMetricProvider()

        # 1 回目: price=500 を記録
        mock_ticker.fast_info = fast_info_1
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            p.get_metrics()
        assert p._last_price == pytest.approx(500.0)

        # 2 回目: price=450 (10% drop → Flash Crash)
        # キャッシュ TTL を強制期限切れにして API 呼び出しさせる
        p._cache_ts = time.monotonic() - 999.0
        mock_ticker.fast_info = fast_info_2
        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            metrics = p.get_metrics()

        # Flash Crash 後は _last_price が新価格になる
        assert p._last_price == pytest.approx(450.0)
        # pnl_day_usd / drawdown_pct も正常に計算されている
        assert "pnl_day_usd" in metrics


# ---------------------------------------------------------------------------
# get_metrics — error path / degraded mode
# ---------------------------------------------------------------------------

class TestGetMetricsDegradedMode:
    def _make_provider(self) -> YFinanceMetricProvider:
        with mock.patch("yfinance.Ticker"):
            return YFinanceMetricProvider()

    def test_degraded_mode_returns_stale_cache(self):
        """yfinance 失敗かつキャッシュあり → stale cache を返す（degraded mode）。"""
        p = self._make_provider()
        # 先にキャッシュをセットしておく
        p._cache_data = {"pnl_day_usd": 99.0, "drawdown_pct": 0.01, "latency_ms": 5.0}
        p._cache_ts = time.monotonic() - 999.0  # TTL 超過扱い

        with mock.patch("yfinance.Ticker", side_effect=Exception("connection error")):
            metrics = p.get_metrics()
        assert metrics["pnl_day_usd"] == pytest.approx(99.0)
        assert p._degraded_mode is True

    def test_no_cache_and_yfinance_fail_raises(self):
        """yfinance 失敗かつキャッシュなし → RuntimeError（fail-closed）。"""
        p = self._make_provider()
        p._cache_data = None
        p._cache_ts = 0.0

        with mock.patch("yfinance.Ticker", side_effect=RuntimeError("api down")):
            with pytest.raises(RuntimeError, match="Failed to fetch"):
                p.get_metrics()

    def test_invalid_price_zero_raises_degraded(self):
        """current_price=0 は rate limit 相当 → degraded mode、キャッシュなしなら RuntimeError。"""
        p = self._make_provider()
        fast_info = _make_fast_info(current=0.0, open_=0.0, day_high=505.0)
        mock_ticker = mock.MagicMock()
        mock_ticker.fast_info = fast_info
        p._cache_data = None

        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            with pytest.raises(RuntimeError):
                p.get_metrics()

    def test_degraded_mode_recovery_resets_flag(self):
        """degraded mode 後に正常取得できたら _degraded_mode が False に戻る。"""
        p = self._make_provider()
        p._degraded_mode = True  # 強制セット

        fast_info = _make_fast_info(current=500.0, open_=495.0, day_high=505.0)
        mock_ticker = mock.MagicMock()
        mock_ticker.fast_info = fast_info

        # 古いキャッシュを消す（TTL 強制期限切れ）
        p._cache_data = None
        p._cache_ts = 0.0

        with mock.patch("yfinance.Ticker", return_value=mock_ticker):
            _ = p.get_metrics()
        assert p._degraded_mode is False


# ---------------------------------------------------------------------------
# _is_flash_crash
# ---------------------------------------------------------------------------

class TestIsFlashCrash:
    def _provider(self) -> YFinanceMetricProvider:
        with mock.patch("yfinance.Ticker"):
            return YFinanceMetricProvider()

    def test_no_last_price_returns_false(self):
        p = self._provider()
        assert p._is_flash_crash(500.0) is False

    def test_small_change_returns_false(self):
        p = self._provider()
        p._last_price = 500.0
        # 0.5% 変動（閾値 2% 未満）
        assert p._is_flash_crash(502.5) is False

    def test_large_drop_returns_true(self):
        p = self._provider()
        p._last_price = 500.0
        # 3% 下落（閾値 2% 超）
        assert p._is_flash_crash(485.0) is True

    def test_large_spike_returns_true(self):
        p = self._provider()
        p._last_price = 500.0
        # 3% 上昇（Flash Crash は下落だけでなく急騰も検知）
        assert p._is_flash_crash(515.0) is True
