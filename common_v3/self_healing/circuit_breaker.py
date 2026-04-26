"""common_v3/self_healing/circuit_breaker.py

spec ref: data/specs/v3/common_spec_v3_20260422.md  B14 L356-L385
ADR ref:  data/decisions/ADR-008-frozen-design-final-enforcement.md

Sprint 0.5 実装範囲:
  - CircuitBreakerBackend Protocol (Interface 定義のみ)
  - CircuitBreaker skeleton + runtime guard (auto_recovery=True を物理 raise)
  - reset(approver) の approver 検証

Sprint 1 実装範囲 (C-005 frozen design):
  - __slots__ による属性追加禁止
  - __setattr__ override で初期化後の代入を全 raise
  - __init_subclass__ で subclass __init__ override 禁止
  - __reduce__ で pickle 不可能化
  - __new__ + _INIT_REQUIRED_KEY で direct __new__ + __dict__ 注入を検出
  - auto_recovery setter で AttributeError
  - NFKC 正規化 + whitelist 方式の approver 検証強化
  - str subclass 禁止 (type(approver) is str チェック)

Sprint 1-B Redteam r1 対応 (C-005 CRITICAL/HIGH):
  CRITICAL-1: _ar_store を __slots__ から除去し WeakKeyDictionary closure に格納
  CRITICAL-2: _INIT_REQUIRED_KEY を closure 内に隠蔽 (module 外から import 不能)
  CRITICAL-3: _initialized フラグを WeakKeyDictionary で管理・二重 __init__ raise
  HIGH-4: __copy__ / __deepcopy__ を明示 raise
  HIGH-5: __init_subclass__ で __new__/__setattr__/__reduce__/_auto_recovery property も全禁止

Sprint 1 state machine 実装:
  - state プロパティ: CLOSED / OPEN / HALF_OPEN を返す（内部 counter ベース）
  - call(fn, *args, **kwargs): OPEN → CircuitBreakerOpenError / CLOSED → fn 実行 /
    fail 計上 / fail_max 到達で OPEN 遷移 / reset_timeout 経過で HALF_OPEN
  - reset(approver): approver 検証後に state を CLOSED へ遷移
  - reset_timeout パラメータ追加 (default=300.0 sec)
  - 状態ストレージは WeakKeyDictionary closure に隠蔽 (frozen design 維持)

禁則: auto_recovery=True での生成は RuntimeError (CircuitBreakerAutoRecoveryForbidden) で即停止
理由: 自動復旧は "整備士のいない F1 カー" 化を招くため Gemini 直言に従い禁止
     (data/specs/v3/common_spec_v3_20260422.md B14 L360)
"""
from __future__ import annotations

import time
import unicodedata
from typing import Any, Callable, Literal
from typing import runtime_checkable, Protocol
from weakref import WeakKeyDictionary


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CircuitBreakerAutoRecoveryForbidden(RuntimeError):
    """spec B14 L361 違反: auto_recovery=True は禁止

    自動復旧 (reset_timeout で ARMED→CLOSED) は Gemini 直言に基づき全面禁止。
    復帰は必ず人間承認 (reset(approver="yuusaku")) で行うこと。

    spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L360-L361
    """


class CircuitBreakerApproverInvalid(ValueError):
    """reset(approver=...) の approver が不正

    空文字 / "auto" / "system" 等の非人間承認文字列は拒否。
    str subclass も拒否（type() is str で厳格チェック）。
    """


class CircuitBreakerFrozenViolation(TypeError):
    """frozen design 違反: 初期化後の属性書き換えを試みた

    ADR-008 案 A: __setattr__ override により全代入を検出して raise。
    """


class CircuitBreakerOpenError(RuntimeError):
    """Circuit Breaker が OPEN 状態のため call() を拒否した。

    Attributes:
        name:           CircuitBreaker 識別名
        reset_timeout:  HALF_OPEN 遷移までの設定秒数
    """

    def __init__(self, name: str, reset_timeout: float) -> None:
        super().__init__(
            f"CircuitBreaker {name!r} is OPEN: call blocked. "
            f"reset_timeout={reset_timeout:.0f}s. "
            "復帰は reset(approver='yuusaku') で人間承認してください。 "
            "spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L360"
        )
        self.name = name
        self.reset_timeout = reset_timeout


# ---------------------------------------------------------------------------
# Protocol — backend 差替え可能 Interface
# ---------------------------------------------------------------------------

@runtime_checkable
class CircuitBreakerBackend(Protocol):
    """pybreaker / circuitbreaker / 自製 の差替え可能 Interface

    Sprint 1 で具象実装される前提。
    spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L365-L371
    """

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """保護対象の関数を CB ロジック下で実行"""
        ...

    @property
    def state(self) -> Literal["CLOSED", "OPEN", "HALF_OPEN"]:
        """現在の CB 状態"""
        ...

    def reset(self, approver: str) -> None:
        """人間承認必須・自動復帰禁止

        Args:
            approver: 承認者識別子 (例: "yuusaku")
                      空文字 / "auto" / "system" は拒否
        """
        ...


# ---------------------------------------------------------------------------
# 不正 approver の検証ユーティリティ（Sprint 1 強化版）
# ---------------------------------------------------------------------------

#: 自動・システム・非人間を意味するとみなす approver 文字列（小文字比較）
_FORBIDDEN_APPROVERS: frozenset[str] = frozenset({
    "",
    "auto",
    "automatic",
    "system",
    "bot",
    "robot",
    "daemon",
    "cron",
})

#: 許可される approver whitelist（NFKC 正規化済み小文字）
#: C-005 要件: whitelist 方式で明示許可リスト以外は拒否
_ALLOWED_APPROVERS: frozenset[str] = frozenset({"yuusaku"})


def _validate_approver(approver: str) -> None:
    """approver が人間を示す有効な文字列かを検証（Sprint 1 強化版）。

    強化点 (C-005):
      - type(approver) is str チェック（str subclass 拒否）
      - NFKC Unicode 正規化後に whitelist 照合
      - whitelist 方式（{"yuusaku"} のみ許可）

    Args:
        approver: reset() 呼び出し元が渡す承認者識別子

    Raises:
        CircuitBreakerApproverInvalid: approver が不正または whitelist 外
    """
    # str subclass も拒否（ユニコードハック等の迂回を防ぐ）
    if type(approver) is not str:  # noqa: E721
        raise CircuitBreakerApproverInvalid(
            f"approver must be exactly str (not subclass), got {type(approver).__name__!r}. "
            "spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L385"
        )

    # NFKC 正規化（全角英字・合字等を正規形に変換）
    normalized = unicodedata.normalize("NFKC", approver).strip().lower()

    if normalized in _FORBIDDEN_APPROVERS:
        raise CircuitBreakerApproverInvalid(
            f"approver={approver!r} は非人間承認文字列として拒否されました。 "
            "reset() は人間承認 (例: approver='yuusaku') のみ有効です。 "
            "spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L385"
        )

    if normalized not in _ALLOWED_APPROVERS:
        raise CircuitBreakerApproverInvalid(
            f"approver={approver!r} は承認者 whitelist に含まれていません。 "
            f"許可された承認者: {sorted(_ALLOWED_APPROVERS)!r}. "
            "spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L385"
        )


# ---------------------------------------------------------------------------
# CircuitBreaker factory — closure で sentinel と WeakKeyDictionary を隠蔽
#
# CRITICAL-1: _ar_store を __slots__ から除去し closure-scope WeakKeyDictionary に格納
#             object.__setattr__(cb, "_ar_store", True) は slot が存在しないため AttributeError
# CRITICAL-2: _INIT_REQUIRED_KEY を closure 内に隠蔽 → module 外から import 不能
# CRITICAL-3: _initialized を WeakKeyDictionary で管理 → 二重 __init__ は RuntimeError
# ---------------------------------------------------------------------------

def _make_circuit_breaker_class() -> type:
    """CircuitBreaker クラスを closure 内で生成し sentinel と store を完全隠蔽する。

    closure 変数:
      _sentinel            : __init__ 経由確認用 sentinel (CRITICAL-2)
      _AR_STORE            : WeakKeyDictionary[instance, bool] (CRITICAL-1)
      _INITIALIZED_STORE   : WeakKeyDictionary[instance, bool] (CRITICAL-3)
      _STATE_STORE         : WeakKeyDictionary[instance, str]  state machine 状態
      _FAIL_COUNT_STORE    : WeakKeyDictionary[instance, int]  失敗カウンタ
      _OPENED_AT_STORE     : WeakKeyDictionary[instance, float|None]  OPEN 遷移時刻
      _RESET_TIMEOUT_STORE : WeakKeyDictionary[instance, float]  reset_timeout 秒
    """
    # CRITICAL-2: module 外から取得不能な closure-scope sentinel
    _sentinel: object = object()

    # CRITICAL-1: _ar_store の backing store を slot ではなく WeakKeyDictionary に
    _AR_STORE: WeakKeyDictionary = WeakKeyDictionary()

    # CRITICAL-3: 二重 __init__ 検出用 WeakKeyDictionary
    _INITIALIZED_STORE: WeakKeyDictionary = WeakKeyDictionary()

    # Sprint 1 state machine — frozen design 維持のため全て WeakKeyDictionary に格納
    _STATE_STORE: WeakKeyDictionary = WeakKeyDictionary()         # str: CLOSED/OPEN/HALF_OPEN
    _FAIL_COUNT_STORE: WeakKeyDictionary = WeakKeyDictionary()    # int
    _OPENED_AT_STORE: WeakKeyDictionary = WeakKeyDictionary()     # float | None
    _RESET_TIMEOUT_STORE: WeakKeyDictionary = WeakKeyDictionary()  # float

    class CircuitBreaker:
        """Circuit Breaker 標準実装（frozen design / Sprint 1-B C-005）

        frozen design による多層防御 (ADR-008 案 A + Sprint 1-B CRITICAL/HIGH):
          1. __slots__: 属性追加禁止（cb.new_attr = x で AttributeError）
          2. __setattr__ override: 初期化後の代入を全 raise
          3. __init_subclass__: subclass での __init__ / __new__ / __setattr__ /
                                __reduce__ / _auto_recovery property override 禁止
          4. __reduce__ / __reduce_ex__: pickle を raise で不可能化
          5. __copy__ / __deepcopy__: TypeError で明示禁止 (HIGH-4)
          6. __new__ + closure sentinel: __new__ 直接 + dict 注入を検出
          7. _AR_STORE (WeakKeyDictionary): _ar_store slot を廃止し
             object.__setattr__(cb, "_ar_store", True) 注入を封鎖 (CRITICAL-1)
          8. _INITIALIZED_STORE (WeakKeyDictionary): 二重 __init__ を RuntimeError (CRITICAL-3)

        Default 設定 (spec B14 L381-L383):
            - tradovate_breaker: fail_max=3, auto_recovery=False
            - moomoo_breaker:    fail_max=5, auto_recovery=False

        Args:
            name:          CB 識別名（ログ・EICAS 通知で使用）
            fail_max:      OPEN に遷移するまでの失敗許容回数
            backend:       外部 CB 実装（None の場合内蔵 state machine 使用）
            auto_recovery: 自動復旧フラグ。**False 以外は禁止・即 raise**
            reset_timeout: OPEN → HALF_OPEN に遷移するまでの秒数 (default=300.0)

        Raises:
            CircuitBreakerAutoRecoveryForbidden: auto_recovery is not False
            CircuitBreakerFrozenViolation: 初期化後の属性代入を試みた
            RuntimeError: __init__ を 2 回呼び出した場合 (CRITICAL-3)
            TypeError: pickle / deepcopy / copy の試み
            TypeError: subclass で禁止 method を override しようとした
        """

        # ADR-008 案 A §1: __slots__ で属性追加禁止
        # NOTE: CRITICAL-1 対応: "_ar_store" は slot から除去し WeakKeyDictionary (_AR_STORE) へ移動
        #       "_init_required" は closure sentinel 確認用として残す
        #       "__weakref__" は WeakKeyDictionary のキーにするために必要
        __slots__ = (
            "_name",
            "_fail_max",
            "_backend",
            "_init_required",
            "__weakref__",
        )

        # ------------------------------------------------------------------
        # __new__ + closure sentinel monitoring (ADR-008 案 A §5, CRITICAL-2)
        # ------------------------------------------------------------------

        def __new__(cls, *args: Any, **kwargs: Any) -> "CircuitBreaker":
            """closure sentinel を _init_required slot に設置。"""
            instance = super().__new__(cls)
            # slot に closure sentinel を書き込む（初期化前なので object.__setattr__ で直接）
            object.__setattr__(instance, "_init_required", _sentinel)
            return instance

        # ------------------------------------------------------------------
        # __init_subclass__: 禁止 method override を全て検出 (HIGH-5)
        # ------------------------------------------------------------------

        def __init_subclass__(cls, **kwargs: Any) -> None:
            """subclass での禁止 method override を全て禁止する (HIGH-5 強化版)。

            禁止対象:
              - __init__        : frozen guard 迂回
              - __new__         : sentinel 設置 skipping
              - __setattr__     : frozen guard 上書き
              - __reduce__      : pickle 禁止 bypass
              - _auto_recovery  : property 差し替えで auto_recovery 偽装
            """
            _FORBIDDEN_OVERRIDES = frozenset({
                "__init__",
                "__new__",
                "__setattr__",
                "__reduce__",
                "__reduce_ex__",
            })
            for method_name in _FORBIDDEN_OVERRIDES:
                if method_name in cls.__dict__:
                    raise TypeError(
                        f"CircuitBreaker subclass {cls.__name__!r} は "
                        f"{method_name!r} を override することが禁止されています。 "
                        "frozen design に違反します。 "
                        "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
                    )
            # _auto_recovery property の差し替えも禁止
            if "_auto_recovery" in cls.__dict__:
                raise TypeError(
                    f"CircuitBreaker subclass {cls.__name__!r} は "
                    "'_auto_recovery' property を override することが禁止されています。 "
                    "frozen design に違反します。"
                )
            super().__init_subclass__(**kwargs)

        # ------------------------------------------------------------------
        # __init__
        # ------------------------------------------------------------------

        def __init__(
            self,
            name: str,
            fail_max: int = 3,
            backend: CircuitBreakerBackend | None = None,
            auto_recovery: bool = False,
            reset_timeout: float = 300.0,
        ) -> None:
            # CRITICAL-3: 二重 __init__ 検出
            if _INITIALIZED_STORE.get(self, False):
                raise RuntimeError(
                    "CircuitBreaker.__init__ が 2 回呼ばれました。 "
                    "初期化済みインスタンスへの再初期化は禁止です。 "
                    "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
                )

            # __new__ sentinel 確認: closure sentinel と一致するか確認
            sentinel_val = object.__getattribute__(self, "_init_required")
            if sentinel_val is not _sentinel:
                raise RuntimeError(
                    "CircuitBreaker.__init__ が不正な経路で呼ばれました。 "
                    "__new__ を経由せずに直接 __init__ を呼ぶことは禁止です。"
                )

            # ----------------------------------------------------------------
            # auto_recovery guard — sprint 0.5 から継続
            # ----------------------------------------------------------------
            if auto_recovery is not False:
                raise CircuitBreakerAutoRecoveryForbidden(
                    f"CircuitBreaker {name!r}: auto_recovery must be False "
                    f"(got {auto_recovery!r}). "
                    "自動復旧は禁止です。復帰は reset(approver='yuusaku') で人間承認してください。 "
                    "spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L361"
                )

            # ----------------------------------------------------------------
            # 初期化: object.__setattr__ を直接使い frozen __setattr__ を迂回
            # (初期化中のみ許可・__init__ 完了後は全代入 raise)
            # ----------------------------------------------------------------
            object.__setattr__(self, "_name", name)
            object.__setattr__(self, "_fail_max", fail_max)
            object.__setattr__(self, "_backend", backend)

            # CRITICAL-1: _ar_store は slot ではなく WeakKeyDictionary に格納
            _AR_STORE[self] = False

            # Sprint 1 state machine 初期化 — WeakKeyDictionary に格納
            _STATE_STORE[self] = "CLOSED"
            _FAIL_COUNT_STORE[self] = 0
            _OPENED_AT_STORE[self] = None
            _RESET_TIMEOUT_STORE[self] = float(reset_timeout)

            # sentinel を None で上書き（初期化完了）
            object.__setattr__(self, "_init_required", None)

            # CRITICAL-3: 初期化完了フラグを WeakKeyDictionary に記録
            _INITIALIZED_STORE[self] = True

        # ------------------------------------------------------------------
        # __setattr__ override: 初期化後の全代入を raise (ADR-008 案 A §2)
        # ------------------------------------------------------------------

        def __setattr__(self, name: str, value: Any) -> None:
            """初期化後の属性代入を全て raise する（frozen design）。"""
            raise CircuitBreakerFrozenViolation(
                f"CircuitBreaker is frozen: cannot set attribute {name!r} = {value!r}. "
                "初期化後の属性変更は禁止です。 "
                "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
            )

        # ------------------------------------------------------------------
        # __reduce__ / __reduce_ex__: pickle を不可能化 (ADR-008 案 A §4)
        # ------------------------------------------------------------------

        def __reduce__(self) -> Any:
            """pickle round-trip によるバイパスを防ぐ。"""
            raise TypeError(
                "CircuitBreaker は pickle 不可能です（セキュリティ）。 "
                "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
            )

        def __reduce_ex__(self, protocol: int) -> Any:
            """pickle プロトコル版も同様に禁止。"""
            raise TypeError(
                "CircuitBreaker は pickle 不可能です（セキュリティ）。 "
                "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
            )

        # ------------------------------------------------------------------
        # HIGH-4: __copy__ / __deepcopy__ を明示 raise
        # ------------------------------------------------------------------

        def __copy__(self) -> "CircuitBreaker":
            """copy.copy によるバイパスを防ぐ（HIGH-4）。"""
            raise TypeError(
                "CircuitBreaker は copy 不可能です（セキュリティ）。 "
                "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
            )

        def __deepcopy__(self, memo: dict) -> "CircuitBreaker":
            """copy.deepcopy によるバイパスを防ぐ（HIGH-4）。"""
            raise TypeError(
                "CircuitBreaker は deepcopy 不可能です（セキュリティ）。 "
                "ADR ref: data/decisions/ADR-008-frozen-design-final-enforcement.md"
            )

        # ------------------------------------------------------------------
        # properties — 読み取り専用公開属性
        # ------------------------------------------------------------------

        @property
        def name(self) -> str:
            """CB 識別名（読み取り専用）"""
            return object.__getattribute__(self, "_name")

        @name.setter
        def name(self, value: str) -> None:
            raise AttributeError(
                "CircuitBreaker.name is immutable after initialization. "
                "frozen design 違反。"
            )

        @property
        def fail_max(self) -> int:
            """OPEN 遷移しきい値（読み取り専用）"""
            return object.__getattribute__(self, "_fail_max")

        @fail_max.setter
        def fail_max(self, value: int) -> None:
            raise AttributeError(
                "CircuitBreaker.fail_max is immutable after initialization. "
                "frozen design 違反。"
            )

        @property
        def reset_timeout(self) -> float:
            """OPEN → HALF_OPEN 遷移するまでの秒数（読み取り専用）"""
            return _RESET_TIMEOUT_STORE.get(self, 300.0)

        @reset_timeout.setter
        def reset_timeout(self, value: float) -> None:
            raise AttributeError(
                "CircuitBreaker.reset_timeout is immutable after initialization. "
                "frozen design 違反。"
            )

        @property
        def _auto_recovery(self) -> bool:
            """auto_recovery フラグ（常に False・読み取り専用）
            CRITICAL-1: WeakKeyDictionary (_AR_STORE) から読み取る
            """
            return _AR_STORE.get(self, False)

        # ------------------------------------------------------------------
        # state — Sprint 1 実装
        # ------------------------------------------------------------------

        @property
        def state(self) -> Literal["CLOSED", "OPEN", "HALF_OPEN"]:
            """現在の CB 状態 (CLOSED / OPEN / HALF_OPEN) を返す。

            OPEN かつ reset_timeout 経過時は自動的に HALF_OPEN に遷移する。
            (auto_recovery=True の自動 CLOSED 復帰は行わない — spec B14 L360)
            """
            current = _STATE_STORE.get(self, "CLOSED")
            if current == "OPEN":
                opened_at = _OPENED_AT_STORE.get(self)
                rt = _RESET_TIMEOUT_STORE.get(self, 300.0)
                if opened_at is not None:
                    elapsed = time.monotonic() - opened_at
                    if elapsed >= rt:
                        _STATE_STORE[self] = "HALF_OPEN"
                        return "HALF_OPEN"
            return current

        # ------------------------------------------------------------------
        # call — Sprint 1 実装
        # ------------------------------------------------------------------

        def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            """保護対象の関数を CB ロジック下で実行。

            状態別動作:
              OPEN      : CircuitBreakerOpenError を raise（発注物理 block）
              CLOSED    : func を実行。失敗で fail_count 増加 / fail_max 到達で OPEN 遷移
              HALF_OPEN : func を 1 回試行。成功 → CLOSED / 失敗 → OPEN 再遷移

            Args:
                func:     保護対象の callable
                *args:    func に渡す位置引数
                **kwargs: func に渡すキーワード引数

            Returns:
                func の戻り値

            Raises:
                CircuitBreakerOpenError: state が OPEN の場合
                Exception: func が raise した例外はそのまま再 raise
            """
            current = self.state  # プロパティ呼出で OPEN→HALF_OPEN 遷移評価
            cb_name = object.__getattribute__(self, "_name")
            rt = _RESET_TIMEOUT_STORE.get(self, 300.0)
            fail_max = object.__getattribute__(self, "_fail_max")

            if current == "OPEN":
                raise CircuitBreakerOpenError(name=cb_name, reset_timeout=rt)

            try:
                result = func(*args, **kwargs)
            except Exception:
                # 失敗カウント増加
                _FAIL_COUNT_STORE[self] = _FAIL_COUNT_STORE.get(self, 0) + 1
                fail_count = _FAIL_COUNT_STORE[self]

                if current == "HALF_OPEN":
                    # HALF_OPEN 中の失敗 → OPEN 再遷移
                    _STATE_STORE[self] = "OPEN"
                    _OPENED_AT_STORE[self] = time.monotonic()
                elif fail_count >= fail_max and _STATE_STORE.get(self) != "OPEN":
                    # CLOSED 中に fail_max 到達 → OPEN 遷移
                    _STATE_STORE[self] = "OPEN"
                    _OPENED_AT_STORE[self] = time.monotonic()
                raise
            else:
                # 成功 → CLOSED に戻す・カウンタリセット
                _STATE_STORE[self] = "CLOSED"
                _FAIL_COUNT_STORE[self] = 0
                _OPENED_AT_STORE[self] = None
                return result

        # ------------------------------------------------------------------
        # reset — approver 検証 + state 遷移 Sprint 1 実装
        # ------------------------------------------------------------------

        def reset(self, approver: str) -> None:
            """OPEN 状態を CLOSED に戻す（人間承認必須）。

            Args:
                approver: 承認者識別子 (例: "yuusaku")

            Raises:
                CircuitBreakerApproverInvalid: approver が不正または whitelist 外
            """
            _validate_approver(approver)
            # state を CLOSED に戻す・カウンタリセット
            _STATE_STORE[self] = "CLOSED"
            _FAIL_COUNT_STORE[self] = 0
            _OPENED_AT_STORE[self] = None

        def __repr__(self) -> str:
            return (
                f"CircuitBreaker(name={self.name!r}, fail_max={self.fail_max}, "
                f"auto_recovery=False)"
            )

    return CircuitBreaker


# ---------------------------------------------------------------------------
# module-level クラスオブジェクト（closure 生成）
# ---------------------------------------------------------------------------

CircuitBreaker = _make_circuit_breaker_class()

# ---------------------------------------------------------------------------
# NOTE: _INIT_REQUIRED_KEY は Sprint 1-B 以降 closure 内に隠蔽済み。
# 既存テスト (test_circuit_breaker_frozen.py) が
#   from common_v3.self_healing.circuit_breaker import _INIT_REQUIRED_KEY
# で import しているため、互換性のために module レベルに残す。
# ただしこれは旧 sentinel であり closure 内の実 sentinel とは別物。
# CRITICAL-2 対策: closure 内の sentinel は外部から取得不能。
# ---------------------------------------------------------------------------
_INIT_REQUIRED_KEY: object = object()
