# Redteam 監査結果: atlas_v3/strategies/base.py TacticBase ABC

**監査日**: 2026-04-23
**対象**: `atlas_v3/strategies/base.py`（42 行・P0 #6 成果物）
**仕様書**: `data/specs/v3/atlas_spec_v3_20260422.md` B5 L134-L154
**Redteam 判定**: 受領不可（CRITICAL 5・HIGH 8・MEDIUM 3・LOW 1）

---

## Sprint 0.5 Day 1 対応済（P1）

| # | 項目 | 対応ファイル |
|---|---|---|
| F-01 | decorator 順序 silent 破壊 | `atlas_v3/tests/test_tactic_base_contract.py`（`__abstractmethods__` 空検証） |
| F-02 | Literal runtime 非検証 | 同上（subclass 値が `get_args(TacticType)` メンバー検証） |
| F-04 | preflight None 戻り値 | 同上（bool 型 assertion） |
| F-08 | tactic_name 重複衝突 | 同上（異なる class の unique 検証） |
| F-12 | MarketEnvironment 未実装 | `atlas_v3/core/env_observer.py` stub 先出し |

---

## Phase 2 着手前に必須対応（持ち越し 8 件）

### CRITICAL（Phase 2 Builder 着手日に最優先実装）

#### F-05: preflight silent skip の docstring 頼み
- **発火条件**: Builder が False だけ返して EICAS 発出忘れる
- **影響**: 場中「戦術が理由不明で走らない」無症状故障・月 60 万直撃
- **Phase 2 実装要求**:
  ```python
  # TacticBase に template method 追加
  @final
  def _preflight_check(self, env) -> bool:
      result = self.preflight(env)
      assert isinstance(result, bool), f"preflight must return bool, got {type(result).__name__}"
      if not result:
          reason = getattr(self, "last_preflight_reason", None)
          assert reason, "preflight returning False must set last_preflight_reason"
          EICAS.caution("tactic disabled", reason, source=self.tactic_name)
      return result
  ```

#### F-08: tactic_name 重複衝突（registry 不在）
- **発火条件**: 新戦術で既存と同じ `tactic_name` 誤返却
- **影響**: AAR 損益合算・kill_switch v1 が v2 として復活稼働
- **Phase 2 実装要求**:
  - `atlas_v3/strategies/registry.py` に `TacticRegistry` 追加
  - `@TacticRegistry.register` decorator 経由でのみ Engine に載る
  - 重複 `tactic_name` は import 時 `ImportError`

#### F-09: Builder TacticBase 継承忘れ
- **発火条件**: 新戦術 class で継承宣言なし
- **影響**: isinstance check 失敗で silent skip
- **Phase 2 実装要求**:
  - `atlas_v3/strategies/__init__.py` で全 `.py` 列挙・`issubclass(cls, TacticBase)` assert
  - `.claude/hooks/tactic_base_inheritance_guard.sh`（linter hook）

#### F-17: kill_switch 統合未規定
- **発火条件**: preflight 失敗で Pushover だけ発火・KILL_SWITCH ファイル未作成
- **影響**: 次 bar で戦術再起動試行
- **Phase 2 実装要求**:
  - ABC に `_emergency_disable(reason: str) -> None` concrete method 追加
  - preflight False 前に必ず呼ぶ template method パターン

### HIGH（Phase 2 Sprint 1 着手前に実装）

- **F-02** 追加強化: `__init_subclass__` で `cls().tactic_type in get_args(TacticType)` 強制 or `tactic_type` を Enum 昇格
- **F-07** `TacticBase.register()` 仮想継承: `__subclasshook__` 上書きで virtual subclass 禁止
- **F-11** preflight 副作用: docstring に「純関数・net I/O 禁止」明記 + monkeypatch contract test
- **F-13** ABC/Protocol 二重契約: Protocol 廃止して全 ABC に統合・または各 Type の Protocol メソッドも `@abstractmethod` 宣言
- **F-14** `__init__.py` 空: 全戦術を明示 import + `__all__` 列挙
- **F-16** hybrid 責務曖昧: `abstract composite` として複数 protocol 全メソッド abstract 強制

### MEDIUM（Phase 2 Sprint 1 中に完遂）

- **F-03** class attribute 被覆: `isinstance(type(t).tactic_type, property)` assert
- **F-06** TYPE_CHECKING NameError: `get_type_hints(TacticBase.preflight)` が動く CI assert
- **F-15** pickle 互換: multiprocessing 採用時の契約テスト

### LOW（Phase 2 ~ Phase 3）

- **F-10** setter 許容: `__setattr__` override で immutable 化

---

## 戦略的指摘（Phase 2 着手時に Builder / Navigator / Redteam で再読必須）

1. **Redteam R2-02「silent AttributeError 封鎖」の主張が未達成**
   - ABC を置いてもそれ以外の silent 失敗経路（F-01/F-02/F-04/F-05/F-07/F-11）が残存
   - ABC は「shape のみ」を強制し、値域・副作用・idempotency は未強制

2. **構造先行・実装後追いの Atlas v3 病**
   - env_observer.py stub だけ置いた状態・Engine 未実装のまま ABC だけ存在
   - 「ABC 入れたから安全」の思い込み禁止

3. **Protocol + ABC 二重契約**
   - Phase 2 Builder の認知負荷爆増
   - どちらが primary 権威か明確化必須（推奨: Protocol 廃止して全 ABC）

---

## 関連ファイル

- `atlas_v3/strategies/base.py` （本件対象）
- `atlas_v3/core/env_observer.py`（F-12 対応 stub）
- `atlas_v3/tests/test_tactic_base_contract.py`（F-01/F-02/F-04/F-08/F-12 contract test）
- `atlas_v3/strategies/__init__.py`（F-14 持ち越し）
- `data/specs/v3/atlas_spec_v3_20260422.md` B5
- Phase 2 Builder 着手時に `memory/project_session_20260423_*.md` と共に必読
