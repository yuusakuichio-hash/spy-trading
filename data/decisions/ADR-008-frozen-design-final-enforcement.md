# ADR-008: frozen design + final class enforcement + 名義インスタンス module 化（CircuitBreaker 抜本対策）

**起票日**: 2026-04-23 07:25 JST
**起票者**: ソラ自律判断（ADR-007 振り返り + Redteam #3 audit Strat-3 反映）
**ステータス**: proposed（Sprint 1 で実装予定）
**関連**: ADR-003 / ADR-005 / ADR-007（学習元）/ Sprint 1 carryover C-005

---

## コンテキスト

ADR-003 / ADR-005 / ADR-007 の 3 連続失敗パターンが Redteam audit で確定:

| サイクル | 主防御 | 結果 |
|---|---|---|
| #7 (ADR-003) | AST 静的解析 | 限界 → runtime guard に逃げる |
| #8 (ADR-005) | AST 静的解析 | 限界 → runtime guard に逃げる |
| #3 (ADR-007) | runtime guard | **限界（__init__ 単一関所）** → 次は何に逃げる？ |

Redteam Strat-3 指摘:
> 真の根本対策はこの class を `final` にできない Python の言語特性そのもの。frozen dataclass + `__slots__` + property setter 全 raise + `__init_subclass__` 禁止 + module 内 monkey-patch 禁止チェックまでやらない限り、defense-in-depth は完成しない

これを Sprint 1 の設計指針として確立する。

## 選択肢

| 案 | 内容 | バグ発生率 | 工数 |
|---|---|---|---|
| A | frozen design 全面採用（全 critical class に `__slots__` + property setter + `__init_subclass__` 禁止 + pickle `__reduce__` override + sys.modules 監視） | 低（Python 動的性に対する真の防御） | Sprint 1 で 1-2 日 |
| B | runtime guard を多重化（`call`, `state`, `reset` 全メソッドで再検証）※ ADR-007 の延長 | 中（イタチごっこ 4 周目）| 半日 |
| C | mypy plugin + lint 強化のみ（実行時防御は諦める） | 高（実行時に止まらない）| 1 日 |

## 採用案

**採用**: A

**判断者**: ソラ自律（Sprint 1 着手時にゆうさく確認推奨）

**理由**:
- Redteam audit で 3 度同型失敗が確定 = 防御層の質的転換が必要
- B は「runtime guard 多重化」で根本同じ（誰かが `__setattr__` 経由で直接代入したら全部効かない）
- C は実行時防御を諦め = production で事故を runtime で止められない
- A は Python の動的性に対する「言語側の真の防御」（class final 化に最も近い）

**選択しなかった理由**:
- B 不採用: runtime guard の延長 = 同じ轍
- C 不採用: 実行時防御放棄 = 取引 Bot で許容できない

## frozen design 設計指針（A 案の具体案）

### 1. `__slots__` 必須化
- attribute 追加を禁止 → `cb._auto_recovery = True` で AttributeError
- 全 critical class（CircuitBreaker / KillSwitch / IdempotencyStore 等）に必須

### 2. property setter 全 raise
```python
@property
def auto_recovery(self) -> bool:
    return self._auto_recovery

@auto_recovery.setter
def auto_recovery(self, value: bool) -> None:
    raise AttributeError("auto_recovery is immutable")
```

### 3. `__init_subclass__` で subclass の __init__ override 禁止
```python
def __init_subclass__(cls, **kwargs):
    if "__init__" in cls.__dict__:
        # super().__init__() を呼ぶか static analysis
        raise TypeError("Subclass must call super().__init__()")
    super().__init_subclass__(**kwargs)
```

### 4. pickle `__reduce__` override
```python
def __reduce__(self):
    raise TypeError("CircuitBreaker is not picklable (security)")
```

### 5. `__new__` 監視
```python
_INIT_REQUIRED_KEY = object()

def __new__(cls, *args, **kwargs):
    instance = super().__new__(cls)
    instance.__dict__["_init_required"] = _INIT_REQUIRED_KEY
    return instance

def __init__(self, ...):
    if self.__dict__.get("_init_required") is not _INIT_REQUIRED_KEY:
        raise RuntimeError("__new__ must be followed by __init__")
    del self.__dict__["_init_required"]
    # 通常の __init__ 処理
```

### 6. 名義インスタンス module 化（spec B14 L382-L383 完全実装）
```python
# common_v3/self_healing/instances.py
from common_v3.self_healing.circuit_breaker import CircuitBreaker

tradovate_breaker = CircuitBreaker(name="tradovate", fail_max=3)
moomoo_breaker = CircuitBreaker(name="moomoo", fail_max=5)
```
- 利用側は `from common_v3.self_healing.instances import tradovate_breaker` のみ許可
- `CircuitBreaker(name=...)` 直接 instantiation を hook で WARN

### 7. sys.modules 監視（runtime sentinel）
```python
# common_v3/self_healing/sentinel.py
import sys
import hashlib

_TRUSTED_HASH = hashlib.sha256(open(__file__).read().encode()).hexdigest()

def verify_module_integrity():
    actual = sys.modules["common_v3.self_healing.circuit_breaker"]
    if actual.__file__ != __file__:
        raise RuntimeError("Module monkey-patched")
```

## 想定結果（事前）

- 短期（Sprint 1 冒頭）: CircuitBreaker / KillSwitch / IdempotencyStore に frozen design 適用
- 中期（Sprint 1 中盤）: Redteam audit で CRITICAL/HIGH 全消化
- 長期: 「3 度同型失敗」パターンの脱却 = ソラの設計能力の質的向上

## 実結果（事後追記）

**最終更新**: 未着手（Sprint 1 で更新）

## 振り返り（事後追記）

**最終更新**: 未着手

## 関連証跡

- ADR-003 / ADR-005 / ADR-007（同型失敗 3 件）
- `data/governance/redteam_audit_circuit_breaker_20260423.md` Strat-3
- `data/sprint1_carryovers.md` C-005（CircuitBreaker frozen design 適用）
