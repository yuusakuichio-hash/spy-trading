"""atlas_v3/ops/mass_verify_safe_runner.py — MassVerify TOCTOU race 根治 wrapper

Redteam aa60 CRITICAL #2:
  spy_bot.py MassVerify ループが `underlying_code` 共有属性を複数スレッドで
  同時書換するため、スレッド A が書いた値をスレッド B が誤読む TOCTOU race が発生。
  結果として誤銘柄のオプションチェーンを参照するリスク。

設計方針:
- `run_mass_verify_safe(entries, verify_fn)` が唯一の公開 API
- threading.Lock を per-symbol context manager で wrap し直列実行を保証
- 各エントリは独立した `VerifyContext` dataclass に分離（共有属性なし）
- verify_fn は `(VerifyContext) -> VerifyResult` の純粋関数 signature
- spy_bot.py 側: MassVerify ループを `run_mass_verify_safe` に差替で有効化

Interface 契約:
    VerifyContext: symbol / strike / expiry / option_type を保持（共有属性なし）
    VerifyResult:  success / reason / data を保持
    verify_fn:     (VerifyContext) -> VerifyResult（スレッドセーフ純粋関数）

Usage (将来統合時):
    from atlas_v3.ops.mass_verify_safe_runner import (
        VerifyContext, run_mass_verify_safe
    )
    results = run_mass_verify_safe(entries, my_verify_fn)
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Sequence

log = logging.getLogger(__name__)

# ── 公開例外 ──────────────────────────────────────────────────────────────────

class MassVerifyError(RuntimeError):
    """MassVerify 実行エラー"""


class SymbolLockTimeoutError(MassVerifyError):
    """per-symbol Lock の取得が timeout_secs 以内に完了しなかった"""


# ── データ型定義 ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VerifyContext:
    """単一エントリの検証コンテキスト（immutable・共有属性なし）。

    frozen=True により verify_fn 内での誤書換を物理防止する。
    """
    symbol: str             # 銘柄コード (例: "US.SPY")
    strike: float           # 行使価格
    expiry: str             # 満期日 YYYY-MM-DD
    option_type: str        # "C" or "P"
    qty: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("VerifyContext.symbol must be non-empty")
        if self.option_type not in ("C", "P"):
            raise ValueError(f"VerifyContext.option_type must be 'C' or 'P', got {self.option_type!r}")
        if self.strike <= 0:
            raise ValueError(f"VerifyContext.strike must be positive, got {self.strike}")


@dataclass
class VerifyResult:
    """単一エントリの検証結果。"""
    context: VerifyContext
    success: bool
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, ctx: VerifyContext, **data: Any) -> "VerifyResult":
        return cls(context=ctx, success=True, reason="ok", data=dict(data))

    @classmethod
    def fail(cls, ctx: VerifyContext, reason: str, **data: Any) -> "VerifyResult":
        return cls(context=ctx, success=False, reason=reason, data=dict(data))


# ── per-symbol Lock レジストリ（プロセス内シングルトン） ──────────────────────

_symbol_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _get_symbol_lock(symbol: str) -> threading.Lock:
    """symbol ごとの Lock を遅延生成して返す（スレッドセーフ）。"""
    with _registry_lock:
        if symbol not in _symbol_locks:
            _symbol_locks[symbol] = threading.Lock()
        return _symbol_locks[symbol]


@contextmanager
def _symbol_lock_ctx(
    symbol: str, timeout_secs: float = 10.0
) -> Generator[None, None, None]:
    """per-symbol Lock を context manager として提供する。

    timeout_secs 以内に取得できなければ SymbolLockTimeoutError を raise。
    """
    lock = _get_symbol_lock(symbol)
    acquired = lock.acquire(timeout=timeout_secs)
    if not acquired:
        raise SymbolLockTimeoutError(
            f"[MassVerify] Could not acquire lock for symbol={symbol!r} "
            f"within {timeout_secs}s. Possible deadlock or overloaded worker."
        )
    try:
        yield
    finally:
        lock.release()


# ── 公開 API ─────────────────────────────────────────────────────────────────

VerifyFn = Callable[[VerifyContext], VerifyResult]


def run_mass_verify_safe(
    entries: Sequence[VerifyContext],
    verify_fn: VerifyFn,
    *,
    lock_timeout_secs: float = 10.0,
    stop_on_first_error: bool = False,
) -> list[VerifyResult]:
    """77 エントリを per-symbol Lock で直列保証して実行する。

    同一 symbol を複数エントリが含む場合は Lock で直列化する。
    異なる symbol は並列実行可能だが、本実装は安全側に倒して全エントリ直列。
    （将来: symbol ごとのスレッドプールに切替可能な設計を維持）

    Args:
        entries:             VerifyContext のリスト（77 件想定）
        verify_fn:           (VerifyContext) -> VerifyResult の純粋関数
        lock_timeout_secs:   per-symbol Lock 取得タイムアウト秒数
        stop_on_first_error: True なら最初の失敗で残りをスキップ

    Returns:
        list[VerifyResult]: entries と同順の結果リスト

    Raises:
        MassVerifyError:           verify_fn が例外を raise した場合
        SymbolLockTimeoutError:    Lock 取得がタイムアウトした場合
    """
    if not entries:
        return []

    results: list[VerifyResult] = []
    errors: list[tuple[int, Exception]] = []

    for idx, ctx in enumerate(entries):
        with _symbol_lock_ctx(ctx.symbol, timeout_secs=lock_timeout_secs):
            try:
                result = verify_fn(ctx)
                results.append(result)
                if not result.success:
                    log.warning(
                        "[MassVerify] entry[%d] symbol=%s strike=%s failed: %s",
                        idx, ctx.symbol, ctx.strike, result.reason,
                    )
                    if stop_on_first_error:
                        log.error(
                            "[MassVerify] stop_on_first_error=True: aborting at entry[%d]", idx
                        )
                        break
            except Exception as exc:  # noqa: BLE001
                err_result = VerifyResult.fail(
                    ctx, reason=f"verify_fn exception: {exc}"
                )
                results.append(err_result)
                errors.append((idx, exc))
                log.error(
                    "[MassVerify] entry[%d] symbol=%s raised: %s",
                    idx, ctx.symbol, exc,
                )
                if stop_on_first_error:
                    break

    if errors and not stop_on_first_error:
        log.error(
            "[MassVerify] Completed with %d errors out of %d entries. "
            "Check logs for details.",
            len(errors), len(entries),
        )

    return results


def run_mass_verify_safe_with_summary(
    entries: Sequence[VerifyContext],
    verify_fn: VerifyFn,
    *,
    lock_timeout_secs: float = 10.0,
) -> tuple[list[VerifyResult], dict[str, Any]]:
    """run_mass_verify_safe + 結果サマリーを同時返却する便利 wrapper。

    Returns:
        (results, summary):
            summary = {
                "total": int,
                "success": int,
                "failed": int,
                "failed_symbols": list[str],
            }
    """
    results = run_mass_verify_safe(
        entries, verify_fn, lock_timeout_secs=lock_timeout_secs
    )
    failed = [r for r in results if not r.success]
    summary: dict[str, Any] = {
        "total": len(results),
        "success": len(results) - len(failed),
        "failed": len(failed),
        "failed_symbols": [r.context.symbol for r in failed],
    }
    return results, summary
