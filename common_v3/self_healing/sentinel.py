"""common_v3/self_healing/sentinel.py

Module integrity 検証 sentinel (C-005 ADR-008 案 A §6)

目的:
  - sys.modules への monkey-patch / 差替えを検出して raise
  - common_v3.self_healing.circuit_breaker が差し替えられていないか確認
  - CircuitBreaker class が期待するクラスオブジェクトと同一か確認

ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md
"""
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any


class ModuleIntegrityError(RuntimeError):
    """module monkey-patch / 差替えを検出した場合の例外"""


# ---------------------------------------------------------------------------
# _ModuleProxy: monkey-patch 検出ラッパー
# ---------------------------------------------------------------------------

class _IntegrityGuardedModule:
    """sys.modules 内の module を監視し属性書き換えを検出するラッパー。

    通常の module アクセスは透過的に委譲する。
    __setattr__ は全て raise して monkey-patch を防ぐ。
    """

    __slots__ = ("_wrapped",)

    def __init__(self, module: ModuleType) -> None:
        object.__setattr__(self, "_wrapped", module)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_wrapped"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_wrapped":
            object.__setattr__(self, name, value)
            return
        raise ModuleIntegrityError(
            f"Module monkey-patch を検出しました: {name!r} への代入は禁止です。 "
            "circuit_breaker module の integrity が破壊されます。 "
            "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
        )

    def __repr__(self) -> str:
        wrapped = object.__getattribute__(self, "_wrapped")
        return f"<IntegrityGuardedModule wrapping {wrapped!r}>"


# ---------------------------------------------------------------------------
# verify_module_integrity: 呼び出し元が任意タイミングで検証可能
# ---------------------------------------------------------------------------

def verify_module_integrity() -> None:
    """common_v3.self_healing.circuit_breaker の module integrity を検証する。

    以下を確認する:
      1. sys.modules に circuit_breaker が存在する
      2. circuit_breaker.CircuitBreaker が期待するクラス名を持つ
      3. circuit_breaker.CircuitBreaker に __slots__ が定義されている

    Raises:
        ModuleIntegrityError: 差替え / monkey-patch が検出された場合
    """
    module_key = "common_v3.self_healing.circuit_breaker"

    if module_key not in sys.modules:
        raise ModuleIntegrityError(
            f"'{module_key}' が sys.modules に存在しません。 "
            "module が削除または差し替えられた可能性があります。"
        )

    module = sys.modules[module_key]

    # CircuitBreaker class の存在確認
    cb_class = getattr(module, "CircuitBreaker", None)
    if cb_class is None:
        raise ModuleIntegrityError(
            f"'{module_key}.CircuitBreaker' が存在しません。 "
            "monkey-patch で削除または差し替えられた可能性があります。"
        )

    # クラス名確認
    if cb_class.__name__ != "CircuitBreaker":
        raise ModuleIntegrityError(
            f"CircuitBreaker のクラス名が期待値と異なります: "
            f"got {cb_class.__name__!r}, expected 'CircuitBreaker'. "
            "差し替えが検出されました。"
        )

    # __slots__ の存在確認（frozen design の証拠）
    if not hasattr(cb_class, "__slots__"):
        raise ModuleIntegrityError(
            "CircuitBreaker に __slots__ が存在しません。 "
            "frozen design が破壊されています。"
        )

    # __slots__ に必須属性が含まれているか確認
    # Sprint 1-B CRITICAL-1: _ar_store は WeakKeyDictionary に移動したため slots から除外
    required_slots = {"_name", "_fail_max"}
    actual_slots = set(cb_class.__slots__)
    missing = required_slots - actual_slots
    if missing:
        raise ModuleIntegrityError(
            f"CircuitBreaker.__slots__ に必須属性が不足しています: {sorted(missing)!r}. "
            "frozen design が改ざんされています。"
        )


def install_monkey_patch_guard() -> None:
    """sys.modules の circuit_breaker module を監視ラッパーで置き換える。

    注意: この関数を呼ぶと以降の module 属性への直接代入が raise される。
    テスト環境での使用を推奨。プロダクションでは verify_module_integrity() を定期呼び出しする方式を推奨。
    """
    module_key = "common_v3.self_healing.circuit_breaker"
    if module_key in sys.modules:
        original = sys.modules[module_key]
        if not isinstance(original, _IntegrityGuardedModule):
            sys.modules[module_key] = _IntegrityGuardedModule(original)  # type: ignore[assignment]
