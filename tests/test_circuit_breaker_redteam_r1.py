"""tests/test_circuit_breaker_redteam_r1.py

Sprint 1-B Phase A / C-005 Redteam r1 対応テスト

対象:
  CRITICAL-1: object.__setattr__(cb, "_ar_store", ...) が slot 消滅で AttributeError
  CRITICAL-2: closure 内 sentinel は module 外から取得不能（compat dummy では __init__ 通過不可）
  CRITICAL-3: __init__ 二重呼び出しで RuntimeError
  HIGH-4:     __copy__ / __deepcopy__ が TypeError
  HIGH-5:     subclass での __new__ / __setattr__ / __reduce__ / _auto_recovery property override が TypeError

完了基準: 既存 90 件 + 本ファイル 6 件以上 = 全件 PASS
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from common_v3.self_healing.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerFrozenViolation,
    _INIT_REQUIRED_KEY,  # これは module-level compat dummy (closure 内実 sentinel とは別物)
)


# ===========================================================================
# CRITICAL-1: object.__setattr__(cb, "_ar_store", True) が slot 不存在で AttributeError
# ===========================================================================

class TestCritical1ArStoreSlotsGone:
    """_ar_store slot を削除したため object.__setattr__ 注入が AttributeError"""

    def test_object_setattr_ar_store_raises(self):
        """object.__setattr__(cb, '_ar_store', True) が AttributeError を raise する。

        Sprint 1-B CRITICAL-1 対応:
          _ar_store slot は __slots__ から除去済み。
          slot が存在しないため object.__setattr__ は AttributeError。
          これにより auto_recovery=True 注入経路を物理的に封鎖する。
        """
        cb = CircuitBreaker(name="critical1_test")
        with pytest.raises(AttributeError):
            object.__setattr__(cb, "_ar_store", True)

    def test_ar_store_not_in_slots(self):
        """_ar_store が __slots__ に含まれていないことを確認"""
        assert "_ar_store" not in CircuitBreaker.__slots__

    def test_auto_recovery_still_returns_false(self):
        """slot 削除後も _auto_recovery property は False を返す"""
        cb = CircuitBreaker(name="ar_false_check")
        assert cb._auto_recovery is False


# ===========================================================================
# CRITICAL-2: module-level compat sentinel では __init__ を通過できない
# ===========================================================================

class TestCritical2SentinelNotImportable:
    """closure 内 sentinel は module 外から取得不能・compat dummy では bypass 不可"""

    def test_sentinel_import_gives_dummy_not_real(self):
        """import できる _INIT_REQUIRED_KEY は closure 内の実 sentinel とは別物。

        Sprint 1-B CRITICAL-2 対応:
          module 外から import できる _INIT_REQUIRED_KEY はダミー。
          実 sentinel は _make_circuit_breaker_class() の closure 内に隠蔽されており、
          外部コードが取得することは不可能。
        """
        # compat dummy が取得できること自体は問題ないが、
        # それを使って __init__ を bypass しようとすると RuntimeError になるべき
        dummy_sentinel = _INIT_REQUIRED_KEY

        # __new__ を skipping して object.__new__ で生成し、
        # compat dummy を _init_required に注入して __init__ を呼ぼうとする
        cb_raw = object.__new__(CircuitBreaker)
        object.__setattr__(cb_raw, "_init_required", dummy_sentinel)

        # compat dummy は closure 内実 sentinel と is 比較で不一致 → RuntimeError
        with pytest.raises(RuntimeError, match="不正な経路"):
            cb_raw.__init__("evil_bypass")

    def test_normal_construction_still_works(self):
        """正常な CircuitBreaker() 生成は引き続き成功する"""
        cb = CircuitBreaker(name="normal_ok")
        assert cb.name == "normal_ok"
        assert cb._auto_recovery is False


# ===========================================================================
# CRITICAL-3: __init__ 二重呼び出しで RuntimeError
# ===========================================================================

class TestCritical3DoubleInitRaises:
    """初期化済みインスタンスへの __init__ 再呼び出しを RuntimeError で封鎖"""

    def test_double_init_raises(self):
        """cb.__init__(...) を 2 回呼ぶと RuntimeError。

        Sprint 1-B CRITICAL-3 対応:
          _INITIALIZED_STORE (WeakKeyDictionary) で初期化済みフラグを管理。
          2 回目の __init__ は RuntimeError を raise する。
        """
        cb = CircuitBreaker(name="double_init_test")
        with pytest.raises(RuntimeError, match="2 回呼ばれました"):
            cb.__init__("double_init_test", 3, None, False)

    def test_double_init_with_different_name_raises(self):
        """名前を変えて 2 回目 __init__ しても同様に RuntimeError"""
        cb = CircuitBreaker(name="original_name")
        with pytest.raises(RuntimeError):
            cb.__init__("hijacked_name", 5, None, False)

    def test_single_init_is_fine(self):
        """1 回だけの __init__ は正常動作"""
        cb = CircuitBreaker(name="single_ok", fail_max=7)
        assert cb.name == "single_ok"
        assert cb.fail_max == 7


# ===========================================================================
# HIGH-4: __copy__ / __deepcopy__ が明示 TypeError
# ===========================================================================

class TestHigh4CopyForbidden:
    """copy.copy / copy.deepcopy が TypeError を raise する"""

    def test_copy_copy_raises(self):
        """copy.copy(cb) が TypeError を raise する（HIGH-4）。

        Sprint 1-B HIGH-4 対応:
          __copy__ を明示的に raise TypeError で定義。
          以前は __reduce__ 経由で間接的に raise されていたが、
          copy.copy は __reduce__ を呼ばないケースがあるため明示 override が必要。
        """
        cb = CircuitBreaker(name="copy_test")
        with pytest.raises(TypeError):
            copy.copy(cb)

    def test_copy_deepcopy_raises(self):
        """copy.deepcopy(cb) が TypeError を raise する（HIGH-4）。

        Sprint 1-B HIGH-4 対応:
          __deepcopy__ を明示的に raise TypeError で定義。
        """
        cb = CircuitBreaker(name="deepcopy_test")
        with pytest.raises(TypeError):
            copy.deepcopy(cb)


# ===========================================================================
# HIGH-5: __init_subclass__ で __new__ / __setattr__ / __reduce__ /
#         _auto_recovery property override も全禁止
# ===========================================================================

class TestHigh5SubclassOverridesForbidden:
    """__init_subclass__ が禁止 method override を class 定義時点で TypeError"""

    def test_subclass_new_override_forbidden(self):
        """subclass で __new__ を override すると class 定義時に TypeError（HIGH-5）。

        __new__ override は closure sentinel の設置を skipping するための攻撃経路。
        """
        with pytest.raises(TypeError, match="__new__"):
            class EvilNew(CircuitBreaker):
                def __new__(cls, *args: Any, **kwargs: Any) -> "CircuitBreaker":
                    return object.__new__(cls)

    def test_subclass_setattr_override_forbidden(self):
        """subclass で __setattr__ を override すると class 定義時に TypeError（HIGH-5）。

        __setattr__ override は frozen guard を上書きするための攻撃経路。
        """
        with pytest.raises(TypeError, match="__setattr__"):
            class EvilSetattr(CircuitBreaker):
                def __setattr__(self, name: str, value: Any) -> None:
                    object.__setattr__(self, name, value)

    def test_subclass_reduce_override_forbidden(self):
        """subclass で __reduce__ を override すると class 定義時に TypeError（HIGH-5）。

        __reduce__ override は pickle 禁止を bypass するための攻撃経路。
        """
        with pytest.raises(TypeError, match="__reduce__"):
            class EvilReduce(CircuitBreaker):
                def __reduce__(self) -> Any:
                    return (CircuitBreaker, ("restored",))

    def test_subclass_auto_recovery_property_override_forbidden(self):
        """subclass で _auto_recovery property を override すると TypeError（HIGH-5）。

        _auto_recovery property 差し替えは auto_recovery=True を偽装する攻撃経路。
        """
        with pytest.raises(TypeError, match="_auto_recovery"):
            class EvilProp(CircuitBreaker):
                @property
                def _auto_recovery(self) -> bool:
                    return True  # type: ignore[override]

    def test_subclass_without_forbidden_overrides_works(self):
        """禁止 method を override しない subclass は問題なく定義・instantiation 可能"""
        class SafeSub(CircuitBreaker):
            pass

        cb = SafeSub(name="safe_sub", fail_max=4)
        assert cb.name == "safe_sub"
        assert cb.fail_max == 4
        assert cb._auto_recovery is False
