"""common_v3/self_healing/breaker_config.py

3 upstream の CircuitBreaker 設定マッピングと @breaker デコレータ。

設定根拠:
  tradovate_auth : 認証失敗は最重要。3 回失敗で 1h 遮断（過剰 auth 試行を防ぐ）
  pushover       : 通知失敗は非クリティカル。5 回失敗で 5 分遮断（rate limit 余裕）
  moomoo_quote   : quote 取得失敗。5 回失敗で 1 分遮断（短期 retry を許容）

@breaker デコレータ:
  CircuitBreaker.call() は Sprint 1 で NotImplementedError のため、
  本モジュールが独立した軽量 state machine を実装する。
  - CLOSED  : 通常通りコール
  - OPEN    : CircuitOpenError を raise（reset_timeout 経過まで呼出不可）
  - HALF_OPEN: reset_timeout 経過後の次の 1 コールを試行
  state は関数オブジェクト単位で _BreakerState に格納（プロセス内 singleton）。

使用例:
    from common_v3.self_healing.breaker_config import breaker, UPSTREAM_CONFIGS

    @breaker("pushover")
    def send_notification(title: str, msg: str) -> bool:
        ...
"""
from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Literal, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 設定値
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UpstreamBreakerConfig:
    """1 upstream の Circuit Breaker パラメータ。

    Attributes:
        upstream_name:  upstream 識別子（ログ・エラーメッセージ用）
        fail_max:       OPEN に遷移するまでの連続失敗許容回数
        reset_timeout:  OPEN → HALF_OPEN に遷移するまでの秒数
    """
    upstream_name: str
    fail_max: int
    reset_timeout: float  # seconds


#: 3 upstream の設定マッピング（key = upstream_name）
UPSTREAM_CONFIGS: Dict[str, UpstreamBreakerConfig] = {
    "tradovate_auth": UpstreamBreakerConfig(
        upstream_name="tradovate_auth",
        fail_max=3,
        reset_timeout=3600.0,  # 1h — 認証失敗は過剰試行防止で長め
    ),
    "pushover": UpstreamBreakerConfig(
        upstream_name="pushover",
        fail_max=5,
        reset_timeout=300.0,  # 5min — rate limit 余裕を持って待機
    ),
    "moomoo_quote": UpstreamBreakerConfig(
        upstream_name="moomoo_quote",
        fail_max=5,
        reset_timeout=60.0,  # 1min — quote 一時失敗は短期 retry 許容
    ),
}


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

BreakerState = Literal["CLOSED", "OPEN", "HALF_OPEN"]


@dataclass
class _BreakerState:
    """1 upstream の breaker 状態（プロセス内 mutable singleton）。

    NOTE: frozen=False — state 遷移が必要なため mutable。
    """
    upstream_name: str
    fail_max: int
    reset_timeout: float
    _state: BreakerState = field(default="CLOSED", init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: Optional[float] = field(default=None, init=False)

    @property
    def state(self) -> BreakerState:
        """現在状態（CLOSED / OPEN / HALF_OPEN）を返す。

        OPEN かつ reset_timeout 経過時は自動的に HALF_OPEN に遷移する。
        """
        if self._state == "OPEN" and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.reset_timeout:
                self._state = "HALF_OPEN"
                log.info(
                    "[breaker:%s] OPEN → HALF_OPEN (elapsed=%.1fs >= reset_timeout=%.1fs)",
                    self.upstream_name, elapsed, self.reset_timeout,
                )
        return self._state

    def record_success(self) -> None:
        """成功を記録。HALF_OPEN / CLOSED どちらでも failure_count をリセット。"""
        if self._state in ("HALF_OPEN", "OPEN"):
            log.info(
                "[breaker:%s] %s → CLOSED (success recorded)",
                self.upstream_name, self._state,
            )
        self._state = "CLOSED"
        self._failure_count = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """失敗を記録。fail_max 到達で OPEN に遷移。"""
        self._failure_count += 1
        log.warning(
            "[breaker:%s] failure recorded (%d/%d)",
            self.upstream_name, self._failure_count, self.fail_max,
        )
        if self._failure_count >= self.fail_max and self._state != "OPEN":
            self._state = "OPEN"
            self._opened_at = time.monotonic()
            log.error(
                "[breaker:%s] CLOSED → OPEN (fail_max=%d reached). "
                "reset_timeout=%.1fs",
                self.upstream_name, self.fail_max, self.reset_timeout,
            )


# ---------------------------------------------------------------------------
# Global state registry（upstream_name → _BreakerState）
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, _BreakerState] = {}


def get_state(upstream_name: str) -> _BreakerState:
    """upstream_name の _BreakerState を返す（なければ UPSTREAM_CONFIGS から生成）。

    Raises:
        KeyError: upstream_name が UPSTREAM_CONFIGS に存在しない場合
    """
    if upstream_name not in _REGISTRY:
        cfg = UPSTREAM_CONFIGS[upstream_name]  # KeyError propagation 意図的
        _REGISTRY[upstream_name] = _BreakerState(
            upstream_name=cfg.upstream_name,
            fail_max=cfg.fail_max,
            reset_timeout=cfg.reset_timeout,
        )
    return _REGISTRY[upstream_name]


def reset_state(upstream_name: str) -> None:
    """テスト・手動復帰用: 指定 upstream の state を CLOSED にリセット。

    approver 検証は行わない（テストユーティリティ用途）。
    本番の human-approval reset は CircuitBreaker.reset(approver=...) を経由すること。
    """
    if upstream_name in _REGISTRY:
        _REGISTRY[upstream_name].record_success()
        log.info("[breaker:%s] state forcibly reset to CLOSED", upstream_name)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class CircuitOpenError(RuntimeError):
    """Circuit Breaker が OPEN 状態のため呼出を拒否した。

    Attributes:
        upstream_name: 遮断された upstream 識別子
        reset_timeout: HALF_OPEN 遷移までの残り秒数（近似）
    """
    def __init__(self, upstream_name: str, reset_timeout: float, opened_at: float) -> None:
        remaining = max(0.0, reset_timeout - (time.monotonic() - opened_at))
        super().__init__(
            f"CircuitBreaker OPEN: upstream={upstream_name!r} is unavailable. "
            f"Retry after ~{remaining:.0f}s. "
            "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
        )
        self.upstream_name = upstream_name
        self.remaining_secs = remaining


# ---------------------------------------------------------------------------
# @breaker decorator factory
# ---------------------------------------------------------------------------

def breaker(upstream_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """upstream_name に対応する Circuit Breaker デコレータを返す。

    UPSTREAM_CONFIGS に登録された upstream_name のみ受け付ける。
    - CLOSED  : 関数を通常呼出。成功で failure_count リセット・失敗でカウント増加。
    - OPEN    : CircuitOpenError を raise（reset_timeout 経過まで）。
    - HALF_OPEN: 1 回試行。成功で CLOSED へ。失敗で OPEN に戻る（opened_at 更新）。

    Args:
        upstream_name: UPSTREAM_CONFIGS のキー
            ("tradovate_auth" / "pushover" / "moomoo_quote")

    Raises:
        KeyError: upstream_name が UPSTREAM_CONFIGS に存在しない

    Example:
        @breaker("pushover")
        def notify(title: str, msg: str) -> bool:
            return common.pushover_client.send(title, msg)
    """
    if upstream_name not in UPSTREAM_CONFIGS:
        raise KeyError(
            f"breaker: unknown upstream_name={upstream_name!r}. "
            f"Known: {sorted(UPSTREAM_CONFIGS)}"
        )

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            bs = get_state(upstream_name)
            current = bs.state  # プロパティ呼出で OPEN→HALF_OPEN 遷移を評価

            if current == "OPEN":
                raise CircuitOpenError(
                    upstream_name=upstream_name,
                    reset_timeout=bs.reset_timeout,
                    opened_at=bs._opened_at or time.monotonic(),
                )

            try:
                result = func(*args, **kwargs)
            except Exception:
                bs.record_failure()
                if bs.state == "HALF_OPEN":
                    # HALF_OPEN 中に失敗 → OPEN に戻す
                    bs._state = "OPEN"
                    bs._opened_at = time.monotonic()
                    log.error(
                        "[breaker:%s] HALF_OPEN → OPEN (probe failed)",
                        upstream_name,
                    )
                raise
            else:
                bs.record_success()
                return result

        return wrapper
    return decorator
