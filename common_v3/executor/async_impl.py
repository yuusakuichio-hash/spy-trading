"""common_v3/executor/async_impl.py — async wrappers for @sync_only functions.

Use these from asyncio context instead of calling @sync_only functions directly.

背景:
- kill_switch / idempotency 等の @sync_only 関数は asyncio event loop 内から
  直接呼ぶと RuntimeError("sync-only contract violation") を送出する。
- @sync_only は executor スレッド (to_thread のワーカー) も non-main thread として
  弾くため、ラップされた関数を to_thread に渡しても RuntimeError になる。
- このモジュールは @sync_only ラッパーをバイパスして fn.__wrapped__ (デコレート前の
  生関数) を asyncio.to_thread に渡す唯一の公式経路である。
- fn.__wrapped__ は functools.wraps が設定する属性。

警告:
- deactivate_async / activate_async / firm_activate_async / firm_deactivate_async は
  audit log 副作用があるため、複数箇所から同時呼出すると競合が生じる可能性がある。
  通常は sync コード (main thread) から直接呼ぶことを推奨する。
  やむを得ず async context から呼ぶ場合は排他制御 (asyncio.Lock) を呼出側で担保すること。
"""
from __future__ import annotations

import asyncio
from typing import Any

import common_v3.risk.kill_switch as _ks
from common_v3.risk.kill_switch import FirmScopedKillSwitch


def _unwrap(fn: Any) -> Any:
    """@sync_only でラップされた関数から生関数 (__wrapped__) を取り出す。

    @sync_only (functools.wraps) は __wrapped__ 属性に元関数を保持する。
    asyncio.to_thread に渡す際は生関数を使うことで executor スレッド上での
    RuntimeError を回避する。

    H-1 fix: __sync_only__ 属性がない関数は assertion error を送出する。
    これにより @sync_only 未適用の関数が silent 通過するのを防ぐ。
    """
    assert getattr(fn, "__sync_only__", False), (
        f"{fn!r} is not decorated with @sync_only — "
        "_unwrap は @sync_only 関数にのみ使用すること"
    )
    return getattr(fn, "__wrapped__", fn)


# ── グローバル KillSwitch async ラッパー ───────────────────────────────────────
# CRITICAL-1 fix: _unwrap 経由ではなく _raw 関数を直接 to_thread に渡す。
# _raw 関数は @sync_only guard を持たないため executor スレッドで安全に動作する。
# _unwrap は bound method の __wrapped__ が unbound になる問題 + CRITICAL-1 の
# 再帰 guard 発火リスクがあるため、_raw への直接参照に統一する。

async def is_active_async() -> bool:
    """グローバル Kill Switch 発動中かを asyncio.to_thread 経由で確認する。

    _is_active_raw を直接 to_thread に渡す唯一の公式経路。
    """
    return await asyncio.to_thread(_ks._is_active_raw)


async def get_state_async() -> dict | None:
    """グローバル Kill Switch 状態を asyncio.to_thread 経由で取得する。

    _unwrap 経由: get_state は _raw を持たないため引き続き __wrapped__ 経由。
    """
    return await asyncio.to_thread(_unwrap(_ks.get_state))


async def activate_async(
    reason: str = "manual",
    activator: str = "unknown",
    scope: dict | None = None,
) -> bool:
    """グローバル Kill Switch を asyncio.to_thread 経由で発動する。

    CRITICAL-1 fix: _activate_raw を直接 to_thread に渡す。
    _activate_raw は @sync_only guard を持たないため executor スレッドで安全。

    警告: audit log 副作用があるため複数 coroutine から同時に呼ばないこと。
    """
    return await asyncio.to_thread(_ks._activate_raw, reason, activator, scope)


async def deactivate_async(activator: str = "unknown", reason: str = "") -> bool:
    """グローバル Kill Switch を asyncio.to_thread 経由で解除する。

    CRITICAL-1 fix: _deactivate_raw を直接 to_thread に渡す。

    警告: audit log 副作用があるため複数 coroutine から同時に呼ばないこと。
    """
    return await asyncio.to_thread(_ks._deactivate_raw, activator, reason)


# ── FirmScopedKillSwitch async ラッパー ───────────────────────────────────────

async def firm_is_active_async(fks: FirmScopedKillSwitch) -> bool:
    """FirmScopedKillSwitch.is_active() を asyncio.to_thread 経由で確認する。

    CRITICAL-1 fix: _unwrap 経由の unbound 呼出から _raw lambda に変更。
    fks._is_active_raw 相当の処理を直接 to_thread に渡す。

    Args:
        fks: FirmScopedKillSwitch インスタンス
    """
    return await asyncio.to_thread(fks._flag_path.exists)


async def firm_activate_async(
    fks: FirmScopedKillSwitch,
    reason: str,
    activator: str = "unknown",
) -> bool:
    """FirmScopedKillSwitch.activate() を asyncio.to_thread 経由で発動する。

    CRITICAL-1 fix: FirmScopedKillSwitch.activate の内部実装は _activate_raw を
    呼ぶため、executor スレッドから呼んでも RuntimeError は発生しない。
    ただし bound method は @sync_only でラップされているため _unwrap で
    __wrapped__ を取り出し to_thread に渡す。

    警告: audit log 副作用があるため複数 coroutine から同時に呼ばないこと。
    """
    raw_fn = _unwrap(fks.activate)
    return await asyncio.to_thread(raw_fn, fks, reason, activator)


async def firm_deactivate_async(
    fks: FirmScopedKillSwitch,
    activator: str = "unknown",
) -> bool:
    """FirmScopedKillSwitch.deactivate() を asyncio.to_thread 経由で解除する。

    bound method の __wrapped__ を to_thread に渡す。
    deactivate 内部は global _deactivate_raw を呼ばない（per-firm flag のみ）ため
    CRITICAL-1 問題は発生しない。

    警告: audit log 副作用があるため複数 coroutine から同時に呼ばないこと。
    """
    raw_fn = _unwrap(fks.deactivate)
    return await asyncio.to_thread(raw_fn, fks, activator)
