"""tests/test_circuit_breaker_upstream_20260425.py

Medium #3: common_v3/self_healing/breaker_config.py upstream mapping テスト

カバレッジ:
  1. UPSTREAM_CONFIGS の 3 upstream 設定値確認（fail_max / reset_timeout）
  2. get_state() — 初回生成・キャッシュ・未知 upstream で KeyError
  3. _BreakerState state machine: CLOSED→OPEN→HALF_OPEN→CLOSED
  4. @breaker デコレータ: CLOSED/OPEN/HALF_OPEN 各状態の動作
  5. CircuitOpenError 属性確認
  6. yfinance_provider.get_metrics に @breaker 適用済み確認
  7. moomoo_provider.get_metrics に @breaker 適用済み確認
"""
from __future__ import annotations

import inspect
import time
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 共通 fixture: 各テスト前に _REGISTRY をクリア
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """各テスト前に breaker_config._REGISTRY を空にする。"""
    from common_v3.self_healing import breaker_config as bc
    bc._REGISTRY.clear()
    yield
    bc._REGISTRY.clear()


# ===========================================================================
# 1. UPSTREAM_CONFIGS 設定値検証
# ===========================================================================

class TestUpstreamConfigs:
    """UPSTREAM_CONFIGS に 3 upstream が正しい値で登録されている。"""

    def test_tradovate_auth_exists(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        assert "tradovate_auth" in UPSTREAM_CONFIGS

    def test_pushover_exists(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        assert "pushover" in UPSTREAM_CONFIGS

    def test_moomoo_quote_exists(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        assert "moomoo_quote" in UPSTREAM_CONFIGS

    def test_tradovate_auth_fail_max(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        assert UPSTREAM_CONFIGS["tradovate_auth"].fail_max == 3

    def test_tradovate_auth_reset_timeout(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        # 1h = 3600s
        assert UPSTREAM_CONFIGS["tradovate_auth"].reset_timeout == 3600.0

    def test_pushover_fail_max(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        assert UPSTREAM_CONFIGS["pushover"].fail_max == 5

    def test_pushover_reset_timeout(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        # 5min = 300s
        assert UPSTREAM_CONFIGS["pushover"].reset_timeout == 300.0

    def test_moomoo_quote_fail_max(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        assert UPSTREAM_CONFIGS["moomoo_quote"].fail_max == 5

    def test_moomoo_quote_reset_timeout(self):
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        # 1min = 60s
        assert UPSTREAM_CONFIGS["moomoo_quote"].reset_timeout == 60.0

    def test_config_is_frozen(self):
        """UpstreamBreakerConfig は frozen=True で変更不可。"""
        from common_v3.self_healing.breaker_config import UPSTREAM_CONFIGS
        cfg = UPSTREAM_CONFIGS["pushover"]
        with pytest.raises((AttributeError, TypeError)):
            cfg.fail_max = 999  # type: ignore[misc]


# ===========================================================================
# 2. get_state / reset_state
# ===========================================================================

class TestGetState:
    """get_state() の初回生成・キャッシュ・未知 upstream。"""

    def test_get_state_creates_on_first_call(self):
        from common_v3.self_healing.breaker_config import get_state
        bs = get_state("pushover")
        assert bs is not None

    def test_get_state_returns_same_object(self):
        """2 回呼んでも同じ _BreakerState インスタンスを返す。"""
        from common_v3.self_healing.breaker_config import get_state
        bs1 = get_state("pushover")
        bs2 = get_state("pushover")
        assert bs1 is bs2

    def test_get_state_unknown_upstream_raises_keyerror(self):
        from common_v3.self_healing.breaker_config import get_state
        with pytest.raises(KeyError):
            get_state("nonexistent_upstream_xyz")

    def test_reset_state_sets_closed(self):
        from common_v3.self_healing.breaker_config import get_state, reset_state
        bs = get_state("pushover")
        # 手動で OPEN にする
        for _ in range(5):
            bs.record_failure()
        assert bs._state == "OPEN"
        reset_state("pushover")
        assert bs.state == "CLOSED"

    def test_reset_state_unknown_name_is_noop(self):
        """登録前の upstream を reset しても例外なし。"""
        from common_v3.self_healing.breaker_config import reset_state
        reset_state("unknown_noop")  # should not raise


# ===========================================================================
# 3. _BreakerState state machine
# ===========================================================================

class TestBreakerStateMachine:
    """_BreakerState の CLOSED→OPEN→HALF_OPEN→CLOSED 遷移。"""

    def _make_state(self, fail_max: int = 3, reset_timeout: float = 60.0) -> object:
        from common_v3.self_healing.breaker_config import _BreakerState
        return _BreakerState(
            upstream_name="test_upstream",
            fail_max=fail_max,
            reset_timeout=reset_timeout,
        )

    def test_initial_state_is_closed(self):
        bs = self._make_state()
        assert bs.state == "CLOSED"

    def test_failure_increments_count(self):
        bs = self._make_state(fail_max=3)
        bs.record_failure()
        assert bs._failure_count == 1

    def test_fail_max_triggers_open(self):
        bs = self._make_state(fail_max=3)
        for _ in range(3):
            bs.record_failure()
        assert bs._state == "OPEN"

    def test_below_fail_max_stays_closed(self):
        bs = self._make_state(fail_max=3)
        for _ in range(2):
            bs.record_failure()
        assert bs._state == "CLOSED"

    def test_open_to_half_open_after_timeout(self):
        """reset_timeout 経過後に state プロパティが HALF_OPEN を返す。"""
        bs = self._make_state(fail_max=1, reset_timeout=0.01)
        bs.record_failure()
        assert bs._state == "OPEN"
        time.sleep(0.05)
        # プロパティアクセスで遷移評価
        assert bs.state == "HALF_OPEN"

    def test_success_resets_to_closed(self):
        bs = self._make_state(fail_max=3)
        for _ in range(3):
            bs.record_failure()
        assert bs._state == "OPEN"
        bs.record_success()
        assert bs._state == "CLOSED"
        assert bs._failure_count == 0

    def test_success_clears_failure_count(self):
        bs = self._make_state(fail_max=3)
        bs.record_failure()
        bs.record_failure()
        bs.record_success()
        assert bs._failure_count == 0


# ===========================================================================
# 4. @breaker デコレータ
# ===========================================================================

class TestBreakerDecorator:
    """@breaker の CLOSED/OPEN/HALF_OPEN 各状態での動作確認。"""

    def test_unknown_upstream_raises_keyerror(self):
        from common_v3.self_healing.breaker_config import breaker
        with pytest.raises(KeyError):
            breaker("no_such_upstream")

    def test_closed_passes_through(self):
        from common_v3.self_healing.breaker_config import breaker

        @breaker("pushover")
        def _ok() -> str:
            return "ok"

        assert _ok() == "ok"

    def test_failure_recorded_on_exception(self):
        from common_v3.self_healing.breaker_config import breaker, get_state

        @breaker("pushover")
        def _fail() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            _fail()

        bs = get_state("pushover")
        assert bs._failure_count == 1

    def test_open_raises_circuit_open_error(self):
        from common_v3.self_healing.breaker_config import breaker, get_state, CircuitOpenError

        @breaker("pushover")
        def _fail() -> None:
            raise RuntimeError("boom")

        # fail_max=5 まで失敗させる
        for _ in range(5):
            with pytest.raises(RuntimeError):
                _fail()

        bs = get_state("pushover")
        assert bs._state == "OPEN"

        # OPEN 状態では CircuitOpenError
        with pytest.raises(CircuitOpenError):
            _fail()

    def test_half_open_success_closes(self):
        """HALF_OPEN 中に成功 → CLOSED に戻る。"""
        from common_v3.self_healing.breaker_config import breaker, get_state

        call_count = {"n": 0}

        @breaker("moomoo_quote")
        def _conditional() -> str:
            call_count["n"] += 1
            if call_count["n"] <= 5:
                raise RuntimeError("fail")
            return "recovered"

        # まず OPEN にする
        for _ in range(5):
            with pytest.raises(RuntimeError):
                _conditional()

        bs = get_state("moomoo_quote")
        assert bs._state == "OPEN"

        # reset_timeout を 0 に強制して HALF_OPEN に遷移させる
        bs._opened_at = time.monotonic() - 999.0
        assert bs.state == "HALF_OPEN"

        # HALF_OPEN 中の成功
        result = _conditional()
        assert result == "recovered"
        assert bs._state == "CLOSED"

    def test_functools_wraps_preserves_name(self):
        """@breaker 後も __name__ が保持される。"""
        from common_v3.self_healing.breaker_config import breaker

        @breaker("pushover")
        def my_function() -> None:
            pass

        assert my_function.__name__ == "my_function"


# ===========================================================================
# 5. CircuitOpenError 属性確認
# ===========================================================================

class TestCircuitOpenError:
    """CircuitOpenError の属性と継承。"""

    def test_is_runtime_error(self):
        from common_v3.self_healing.breaker_config import CircuitOpenError
        err = CircuitOpenError("pushover", 300.0, time.monotonic())
        assert isinstance(err, RuntimeError)

    def test_upstream_name_attribute(self):
        from common_v3.self_healing.breaker_config import CircuitOpenError
        err = CircuitOpenError("tradovate_auth", 3600.0, time.monotonic())
        assert err.upstream_name == "tradovate_auth"

    def test_remaining_secs_non_negative(self):
        from common_v3.self_healing.breaker_config import CircuitOpenError
        err = CircuitOpenError("pushover", 300.0, time.monotonic())
        assert err.remaining_secs >= 0.0


# ===========================================================================
# 6. yfinance_provider.get_metrics に @breaker 適用済み確認
# ===========================================================================

class TestYFinanceProviderBreakerApplied:
    """YFinanceMetricProvider.get_metrics が moomoo_quote breaker でラップされている。"""

    @pytest.mark.xfail(reason="β-2 配下: YFinanceProvider への CB wrap 未実装 — C-017 moomoo provider 統合時に対応")
    def test_get_metrics_wrapped(self):
        """get_metrics の __wrapped__ 属性 or breaker state が存在すること。"""
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        # @breaker は functools.wraps を使うので __wrapped__ を持つ
        assert hasattr(YFinanceMetricProvider.get_metrics, "__wrapped__")

    @pytest.mark.xfail(reason="β-2 配下: YFinanceProvider への CB wrap 未実装 — C-017 moomoo provider 統合時に対応")
    def test_get_metrics_failure_increments_breaker_count(self):
        """get_metrics が RuntimeError を raise したとき moomoo_quote の failure_count が増える。

        yfinance の fast_info を patch して ConnectionError を発生させる。
        キャッシュなし → degraded mode → RuntimeError (fail-closed) → breaker カウント増加。
        """
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        from common_v3.self_healing.breaker_config import get_state

        provider = YFinanceMetricProvider.__new__(YFinanceMetricProvider)
        provider._ticker_symbol = "SPY"
        provider._yf = None
        provider._cache_ttl_secs = 10.0
        provider._cache_ts = 0.0
        provider._cache_data = None  # キャッシュなし: degraded mode で fail-closed
        provider._last_price = None
        provider._degraded_mode = False
        provider._degraded_since = 0.0

        # yfinance Ticker.fast_info が ConnectionError を raise するよう patch
        mock_ticker = MagicMock()
        mock_ticker.fast_info.__get__ = MagicMock(
            side_effect=ConnectionError("yfinance network error")
        )
        type(mock_ticker).fast_info = property(
            fget=lambda self: (_ for _ in ()).throw(ConnectionError("yfinance network error"))
        )

        with patch("yfinance.Ticker", return_value=mock_ticker):
            with pytest.raises(RuntimeError):
                provider.get_metrics()

        bs = get_state("moomoo_quote")
        assert bs._failure_count >= 1


# ===========================================================================
# 7. moomoo_provider.get_metrics に @breaker 適用済み確認
# ===========================================================================

class TestMoomooProviderBreakerApplied:
    """MoomooMetricProvider.get_metrics が moomoo_quote breaker でラップされている。"""

    def test_get_metrics_wrapped(self):
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        assert hasattr(MoomooMetricProvider.get_metrics, "__wrapped__")

    def test_get_metrics_open_raises_circuit_open_error(self):
        """moomoo_quote breaker が OPEN のとき get_metrics は CircuitOpenError を raise する。"""
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        from common_v3.self_healing.breaker_config import get_state, CircuitOpenError

        # breaker を直接 OPEN 状態にする
        bs = get_state("moomoo_quote")
        for _ in range(5):
            bs.record_failure()
        assert bs._state == "OPEN"

        provider = MoomooMetricProvider.__new__(MoomooMetricProvider)
        # 最低限の属性を設定
        provider._opend_host = "127.0.0.1"
        provider._opend_port = 11111
        provider._socket_timeout_secs = 5.0
        provider._retry_max = 3
        provider._trade_password = None
        provider._security_firm = None
        provider._trd_market = None
        provider._trade_ctx = None
        provider._high_water_mark_usd = 0.0
        provider._last_request_ts = 0.0
        provider._paper_acc_id = None

        with pytest.raises(CircuitOpenError) as exc_info:
            provider.get_metrics()

        assert exc_info.value.upstream_name == "moomoo_quote"
