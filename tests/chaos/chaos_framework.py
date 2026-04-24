"""tests/chaos/chaos_framework.py — Chaos Engineering 基盤 (2026-04-25 新規)

OpenD random disconnect / network latency spike / pushover 429 を
context manager + decorator で注入する。

設計方針:
- 既存コード無変更: tests/ 配下のみで完結
- asyncio 禁止 (B16 規律): async def / await / import asyncio は使わない
- context manager (`with inject_*(): ...`) + decorator (`@chaos_*`) の両形式
- 注入はモンキーパッチ (unittest.mock) で実施・cleanup は __exit__ / finally で確実実行
- ChaosState singleton で注入中のフラグを共有 (スレッドセーフ)

公開 API:
    # context manager
    with opend_disconnect(probability=1.0): ...
    with network_latency(latency_ms=300): ...
    with pushover_429(retry_after=60): ...

    # decorator
    @chaos_opend_disconnect(probability=1.0)
    def my_test(): ...

    @chaos_network_latency(latency_ms=200)
    def my_test(): ...

    @chaos_pushover_429(retry_after=30)
    def my_test(): ...

    # 組み合わせ
    with combined_chaos(disconnect=True, latency_ms=200, pushover_429=True): ...
"""
from __future__ import annotations

import functools
import random
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Generator, TypeVar
from unittest.mock import MagicMock, patch

_F = TypeVar("_F", bound=Callable[..., Any])


# ── ChaosState (注入状態管理) ─────────────────────────────────────────────────

class ChaosState:
    """テスト中の注入状態を保持するスレッドセーフシングルトン。"""

    _lock: threading.Lock
    opend_disconnect_active: bool
    latency_ms: float
    pushover_429_active: bool
    pushover_retry_after: int
    inject_count: int

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.opend_disconnect_active = False
        self.latency_ms = 0.0
        self.pushover_429_active = False
        self.pushover_retry_after = 60
        self.inject_count = 0

    def reset(self) -> None:
        with self._lock:
            self.opend_disconnect_active = False
            self.latency_ms = 0.0
            self.pushover_429_active = False
            self.pushover_retry_after = 60
            self.inject_count = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "opend_disconnect_active": self.opend_disconnect_active,
                "latency_ms": self.latency_ms,
                "pushover_429_active": self.pushover_429_active,
                "pushover_retry_after": self.pushover_retry_after,
                "inject_count": self.inject_count,
            }


# プロセス内シングルトン
_chaos_state = ChaosState()


def get_chaos_state() -> ChaosState:
    """テストコードから現在の注入状態を取得するユーティリティ。"""
    return _chaos_state


# ── 例外 ─────────────────────────────────────────────────────────────────────

class ChaosInjectionError(RuntimeError):
    """chaos 注入によって発生する例外基底クラス。"""


class OpenDDisconnectError(ChaosInjectionError):
    """OpenD が切断された状況をシミュレートする例外。"""

    def __init__(self, symbol: str = "", attempt: int = 1) -> None:
        super().__init__(
            f"[Chaos] OpenD disconnect injected: symbol={symbol!r} attempt={attempt}"
        )
        self.symbol = symbol
        self.attempt = attempt


class NetworkLatencyExceededError(ChaosInjectionError):
    """ネットワーク遅延が許容値を超えた。"""

    def __init__(self, latency_ms: float) -> None:
        super().__init__(
            f"[Chaos] Network latency spike injected: {latency_ms:.0f}ms"
        )
        self.latency_ms = latency_ms


class Pushover429Error(ChaosInjectionError):
    """Pushover API が 429 Too Many Requests を返した状況をシミュレート。"""

    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(
            f"[Chaos] Pushover 429 injected: retry_after={retry_after}s"
        )
        self.retry_after = retry_after
        self.status_code = 429


# ── OpenD Disconnect Injector ─────────────────────────────────────────────────

class _OpenDDisconnectInjector:
    """OpenD disconnect を確率的に注入する context manager。

    probability=1.0 で必ず disconnect を模倣する。
    注入先: atlas_v3.ops モジュールの futu 接続関数を MagicMock で差替。
    """

    def __init__(
        self,
        probability: float = 1.0,
        symbol: str = "US.SPY",
        max_attempts: int = 3,
    ) -> None:
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f"probability must be in [0, 1], got {probability}")
        self.probability = probability
        self.symbol = symbol
        self.max_attempts = max_attempts
        self._patches: list = []
        self._triggered = False

    def _make_disconnect_side_effect(self) -> Callable:
        symbol = self.symbol
        probability = self.probability
        call_count = [0]

        def side_effect(*args: Any, **kwargs: Any) -> Any:
            call_count[0] += 1
            if random.random() < probability:
                with _chaos_state._lock:
                    _chaos_state.opend_disconnect_active = True
                    _chaos_state.inject_count += 1
                raise OpenDDisconnectError(
                    symbol=symbol, attempt=call_count[0]
                )
            return MagicMock()

        return side_effect

    def __enter__(self) -> "_OpenDDisconnectInjector":
        # futu SysConfig / OpenQuoteContext / OpenTradeContext の接続系を patch
        # atlas_v3.ops.moomoo_provider がインポート済みでなくてもよいよう
        # try/except で静かに skip する
        targets = [
            "futu.OpenQuoteContext",
            "futu.OpenTradeContext",
        ]
        se = self._make_disconnect_side_effect()
        for t in targets:
            try:
                p = patch(t, side_effect=se)
                p.start()
                self._patches.append(p)
            except (ModuleNotFoundError, AttributeError):
                pass

        _chaos_state.opend_disconnect_active = True
        self._triggered = True
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        _chaos_state.opend_disconnect_active = False
        return False  # 例外を再 raise させる

    @property
    def triggered(self) -> bool:
        return self._triggered


# ── Network Latency Spike Injector ────────────────────────────────────────────

class _NetworkLatencyInjector:
    """ネットワーク遅延スパイクを注入する context manager。

    socket.create_connection をモンキーパッチして latency_ms 分の sleep を挿入する。
    timeout_threshold_ms を超えた場合は NetworkLatencyExceededError を raise する。
    """

    def __init__(
        self,
        latency_ms: float = 300.0,
        timeout_threshold_ms: float | None = None,
        probability: float = 1.0,
    ) -> None:
        self.latency_ms = latency_ms
        self.timeout_threshold_ms = timeout_threshold_ms
        self.probability = probability
        self._patches: list = []
        self._original_create_connection: Any = None

    def _make_latency_side_effect(self, original: Callable) -> Callable:
        latency_ms = self.latency_ms
        threshold = self.timeout_threshold_ms
        probability = self.probability

        def side_effect(*args: Any, **kwargs: Any) -> Any:
            if random.random() < probability:
                with _chaos_state._lock:
                    _chaos_state.latency_ms = latency_ms
                    _chaos_state.inject_count += 1
                time.sleep(latency_ms / 1000.0)
                if threshold is not None and latency_ms > threshold:
                    raise NetworkLatencyExceededError(latency_ms)
            return original(*args, **kwargs)

        return side_effect

    def __enter__(self) -> "_NetworkLatencyInjector":
        import socket as _socket

        original = _socket.create_connection
        self._original_create_connection = original

        se = self._make_latency_side_effect(original)
        p = patch("socket.create_connection", side_effect=se)
        p.start()
        self._patches.append(p)

        with _chaos_state._lock:
            _chaos_state.latency_ms = self.latency_ms
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        with _chaos_state._lock:
            _chaos_state.latency_ms = 0.0
        return False


# ── Pushover 429 Injector ─────────────────────────────────────────────────────

class _Pushover429Injector:
    """Pushover API の 429 Too Many Requests をシミュレートする context manager。

    common.pushover_client.send / urllib.request.urlopen を patch して
    status_code=429 / Retry-After ヘッダを返す MagicMock に差替える。
    """

    def __init__(
        self,
        retry_after: int = 60,
        fail_count: int = 3,
        probability: float = 1.0,
    ) -> None:
        self.retry_after = retry_after
        self.fail_count = fail_count
        self.probability = probability
        self._patches: list = []
        self._call_count = 0

    def _make_429_side_effect(self) -> Callable:
        retry_after = self.retry_after
        fail_count = self.fail_count
        probability = self.probability
        call_count = [0]

        def side_effect(*args: Any, **kwargs: Any) -> Any:
            call_count[0] += 1
            if call_count[0] <= fail_count and random.random() < probability:
                with _chaos_state._lock:
                    _chaos_state.pushover_429_active = True
                    _chaos_state.pushover_retry_after = retry_after
                    _chaos_state.inject_count += 1
                raise Pushover429Error(retry_after=retry_after)
            with _chaos_state._lock:
                _chaos_state.pushover_429_active = False
            return MagicMock()

        return side_effect

    def __enter__(self) -> "_Pushover429Injector":
        se = self._make_429_side_effect()

        # common.pushover_client.send を patch
        try:
            p = patch("common.pushover_client.send", side_effect=se)
            p.start()
            self._patches.append(p)
        except (ModuleNotFoundError, AttributeError):
            pass

        with _chaos_state._lock:
            _chaos_state.pushover_429_active = True
            _chaos_state.pushover_retry_after = self.retry_after
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass
        with _chaos_state._lock:
            _chaos_state.pushover_429_active = False
        return False


# ── Combined Chaos ────────────────────────────────────────────────────────────

class _CombinedChaosInjector:
    """複数の chaos injector を同時に適用する context manager。"""

    def __init__(
        self,
        disconnect: bool = False,
        latency_ms: float = 0.0,
        pushover_429: bool = False,
        disconnect_probability: float = 1.0,
        pushover_retry_after: int = 60,
    ) -> None:
        self._injectors: list = []
        if disconnect:
            self._injectors.append(
                _OpenDDisconnectInjector(probability=disconnect_probability)
            )
        if latency_ms > 0:
            self._injectors.append(
                _NetworkLatencyInjector(latency_ms=latency_ms)
            )
        if pushover_429:
            self._injectors.append(
                _Pushover429Injector(retry_after=pushover_retry_after)
            )

    def __enter__(self) -> "_CombinedChaosInjector":
        for inj in self._injectors:
            inj.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        for inj in reversed(self._injectors):
            try:
                inj.__exit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass
        return False


# ── 公開 context manager factory ─────────────────────────────────────────────

def opend_disconnect(
    probability: float = 1.0,
    symbol: str = "US.SPY",
    max_attempts: int = 3,
) -> _OpenDDisconnectInjector:
    """OpenD disconnect 注入 context manager を返す。"""
    return _OpenDDisconnectInjector(
        probability=probability, symbol=symbol, max_attempts=max_attempts
    )


def network_latency(
    latency_ms: float = 300.0,
    timeout_threshold_ms: float | None = None,
    probability: float = 1.0,
) -> _NetworkLatencyInjector:
    """Network latency spike 注入 context manager を返す。"""
    return _NetworkLatencyInjector(
        latency_ms=latency_ms,
        timeout_threshold_ms=timeout_threshold_ms,
        probability=probability,
    )


def pushover_429(
    retry_after: int = 60,
    fail_count: int = 3,
    probability: float = 1.0,
) -> _Pushover429Injector:
    """Pushover 429 注入 context manager を返す。"""
    return _Pushover429Injector(
        retry_after=retry_after, fail_count=fail_count, probability=probability
    )


def combined_chaos(
    disconnect: bool = False,
    latency_ms: float = 0.0,
    pushover_429: bool = False,
    disconnect_probability: float = 1.0,
    pushover_retry_after: int = 60,
) -> _CombinedChaosInjector:
    """複数 chaos を同時適用する context manager を返す。"""
    return _CombinedChaosInjector(
        disconnect=disconnect,
        latency_ms=latency_ms,
        pushover_429=pushover_429,
        disconnect_probability=disconnect_probability,
        pushover_retry_after=pushover_retry_after,
    )


# ── デコレータ ────────────────────────────────────────────────────────────────

def chaos_opend_disconnect(
    probability: float = 1.0,
    symbol: str = "US.SPY",
    max_attempts: int = 3,
) -> Callable[[_F], _F]:
    """OpenD disconnect 注入デコレータ。"""
    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with opend_disconnect(
                probability=probability,
                symbol=symbol,
                max_attempts=max_attempts,
            ):
                return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


def chaos_network_latency(
    latency_ms: float = 300.0,
    timeout_threshold_ms: float | None = None,
    probability: float = 1.0,
) -> Callable[[_F], _F]:
    """Network latency spike 注入デコレータ。"""
    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with network_latency(
                latency_ms=latency_ms,
                timeout_threshold_ms=timeout_threshold_ms,
                probability=probability,
            ):
                return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


def chaos_pushover_429(
    retry_after: int = 60,
    fail_count: int = 3,
    probability: float = 1.0,
) -> Callable[[_F], _F]:
    """Pushover 429 注入デコレータ。"""
    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with pushover_429(
                retry_after=retry_after,
                fail_count=fail_count,
                probability=probability,
            ):
                return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


# ── pytest fixture ────────────────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture(autouse=False)
    def reset_chaos_state():
        """各テスト前後に ChaosState を初期化する fixture。"""
        _chaos_state.reset()
        yield
        _chaos_state.reset()

except ImportError:
    pass
