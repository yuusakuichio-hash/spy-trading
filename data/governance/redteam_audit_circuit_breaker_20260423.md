# Redteam Audit Report: Sprint 0.5 P0 #3 CircuitBreaker auto_recovery=False 物理強制

**監査日時**: 2026-04-23 JST
**監査対象**:
- `common_v3/self_healing/__init__.py`
- `common_v3/self_healing/circuit_breaker.py`
- `.claude/hooks/circuit_breaker_no_auto_recovery_guard.sh`
- `tests/test_circuit_breaker_no_auto_recovery.py`
- 仕様: `data/specs/v3/common_spec_v3_20260422.md` B14 L356-L385
- ADR-007: `data/decisions/ADR-007-circuit-breaker-runtime-guard-first.md`

**監査者**: Redteam（独立 session・builder/navigator 報告は鵜呑みにせず再検証）
**判定**: **FAIL**

---

## 1. 独立再現結果

| 項目 | builder/navigator 報告 | Redteam 独立実測 | 一致 |
|---|---|---|---|
| pytest 件数 | 35 PASS + 2 xfailed + 1 xpassed | 同上（38 collected → 35/2/1）| OK |
| 既存テスト失敗 | 4+1 件「無関係」 | **7 件 FAIL（うち 2 件は単独 PASS = テスト間順序依存）** | 部分乖離 |
| AST hook 動作 | 直接 True 検出 OK | OK | OK |
| runtime guard 動作 | __init__ 経由は全て raise | **__init__ 経由のみ。__init__ 非経由は全 BYPASS** | 重大乖離 |

---

## 2. 攻撃シナリオ（11 件発見・うち CRITICAL 2 / HIGH 4 / MEDIUM 3 / LOW 2）

### CRITICAL-1: `__new__` + `__dict__` 直接挿入で runtime guard を完全 bypass
```python
cb = CircuitBreaker.__new__(CircuitBreaker)
cb.__dict__["_auto_recovery"] = True  # __init__ をskip → guard 不発
```
**実測結果**: BYPASSED（_auto_recovery=True が成立）
**最悪結果**: Tradovate/moomoo 障害時に CB が無人で復旧 → Self-healing 誤作動でバグ隠蔽 → Boeing 737MAX MCAS 型「単一防御点失陥」

### CRITICAL-2: post-init 直接代入で runtime guard 完全 bypass
```python
cb = CircuitBreaker(name="x")  # __init__ は通る
cb._auto_recovery = True        # 後から書き換え可能
```
**実測結果**: BYPASSED
**歴史的類例**: Therac-25（変数の race condition で安全装置が無効化）

### HIGH-1: pickle round-trip で _auto_recovery=True を復元可能
```python
cb._auto_recovery = True
cb2 = pickle.loads(pickle.dumps(cb))  # __init__ は呼ばれない
```
**実測結果**: BYPASSED
**歴史**: Knight Capital 2012 — デプロイ不整合で旧コードフラグが復活

### HIGH-2: copy.deepcopy で _auto_recovery=True を保持
**実測結果**: BYPASSED

### HIGH-3: subclass で `__init__` を完全 override
```python
class FakeCB(CircuitBreaker):
    def __init__(self, **kw):
        self._auto_recovery = True  # super().__init__() を呼ばない
```
**実測結果**: BYPASSED

### HIGH-4: モジュール monkey-patch / sys.modules 差替えで guard 完全消失
**実測結果**: BYPASSED

### MEDIUM-1: approver 検証が Unicode normalization を考慮していない
- `"ａｕｔｏ"` (fullwidth) → BYPASS
- `"a​uto"` (zero-width space 挿入) → BYPASS
- 任意のランダム文字列 `"evilbot"` `"autopilot"` `"0"` → 全て BYPASS

### MEDIUM-2: str サブクラスで `lower()` / `strip()` をオーバーライド可能
**実測結果**: BYPASSED

### MEDIUM-3: AST hook が repo 外の絶対パスを silent ALLOW
**実測結果**: BYPASSED

### LOW-1: alias import (`as` 経由) は AST hook 通過
### LOW-2: 不正 JSON / 空 tool_input で hook が silent exit 0

---

## 3. 見逃しバグ（コード行番号付き）

### Bug-1: `circuit_breaker.py` L170 — `_auto_recovery` を public attribute として保持
- Python の `_` prefix は規約のみ。`__setattr__` override / `__slots__` / property setter による read-only 化なし
- CRITICAL-2 / HIGH-1〜3 の根本原因

### Bug-2: `circuit_breaker.py` L94-113 — `_validate_approver` の検証が exact-match のみ
- NFKC 正規化なし、whitelist 方式でない、second_factor なし
- MEDIUM-1 / MEDIUM-2 の根本

### Bug-3: `circuit_breaker_no_auto_recovery_guard.sh` L99-100 — SyntaxError silent ALLOW
- 攻撃者が意図的に SyntaxError を含めて hook を通過できる

### Bug-4: hook L77-80 — repo 外パス silent skip
- MEDIUM-3 の根本

### Bug-5: test L386 `test_lambda_bypasses_ast` の xfail マーカー誤り
- xpassed = builder の前提誤り（ast.walk は lambda 内部も traverse）
- xfail マーカー削除 or strict=True 必須

### Bug-6: `circuit_breaker.py` L211-214 — `reset()` が approver 検証通過後 NotImplementedError
- 「auto_recovery=False 物理強制だけが成立し、CB 本体は不在」という空虚な防御

### Bug-7: spec B14 L382-L383 で定義された `tradovate_breaker` / `moomoo_breaker` 名義インスタンスがコード上に存在しない
- production で「自動的に正しい設定で生成される CB」が存在しない

---

## 4. 運用的な穴（4 件）

### Op-1: 既存テスト 4+1 件失敗の Navigator 評価が浅い
- Navigator 報告: 「既存 4+1 件 = 無関係」
- Redteam 実測: full pytest で 7 件 FAIL。うち 2 件（test_known_symbol_tsla / test_record_outcome_updates_history）は単独実行で PASS = テスト間順序依存
- Navigator は full pytest を回していない可能性、または件数を誤記

### Op-2: Sprint 0.5 / Sprint 1 の責務分離が曖昧
- 「auto_recovery=False 物理強制」は完了したが、CB 本体は NotImplementedError
- spec B14 L382-L383 のデフォルト名義インスタンスは未定義
- 「P0 #3 完了」と宣言してもこの CB は何も守っていない

### Op-3: Hook の bypass log が無音 (rc=0) パターンを記録しない
### Op-4: ADR-007 の事後追記欄が空

---

## 5. 戦略的リスク（3 件）

### Strat-1: 「runtime guard 主防御」の主張が崩壊
ADR-007 の核心:
> runtime guard なら lambda/partial/getattr 全パス共通で必ず止まる

**Redteam 実測**: __init__ 経由なら止まる。だが __init__ を経由しない経路（CRITICAL-1, 2, HIGH-1, 2, 3, 4）が 6 つ存在。**ADR-007 の主張は __init__ という「単一の関所」依存であり、737MAX MCAS の単一センサ依存と同型の脆弱性**。

### Strat-2: 「premortem CONDITIONAL_GO・risk=medium」評価の過小化
本 audit で CRITICAL 2 件 + HIGH 4 件発見。Challenger O-ring（1986）と同じ楽観バイアス。

### Strat-3: 「3 度同型失敗」パターン
- #7 #8: AST 静的解析の限界 → runtime guard に逃げる
- #3: runtime guard の限界（__init__ 単一関所依存・属性 mutability・class 自体の差替え可能性）

**真の根本対策はこの class を `final` にできない Python の言語特性そのもの**。frozen dataclass + `__slots__` + property setter 全 raise + `__init_subclass__` 禁止 + module 内 monkey-patch 禁止チェックまでやらない限り、defense-in-depth は完成しない。

---

## 6. Navigator 申し送り 4 件の Redteam 評価

| # | Navigator 申し送り | Redteam 判定 |
|---|---|---|
| 1 | hook L61-62 silent except SyntaxError ALLOW | **修正必要（HIGH）**: Sprint 0.5 内で SyntaxError 時 string match fallback 必須 |
| 2 | xfail_strict 未設定 | **修正必要（MEDIUM）**: xfail マーカー自体が誤り。strict=True で test fail 顕在化 |
| 3 | tradovate_breaker / moomoo_breaker 未定義 | **修正必要（HIGH）**: spec B14 L382-L383 完全未実装 = Sprint 0.5 spec 未達成 |
| 4 | 既存 4+1 件失敗無関係 | **不正確（要再検証）**: 実測 7 件失敗 |

---

## 7. 重症度評価サマリ

| Severity | 件数 | 対応緊急度 |
|---|---|---|
| **CRITICAL** | 2（__new__ bypass / post-init bypass） | Sprint 0.5 or Sprint 1 で frozen design 必須 |
| **HIGH** | 6（pickle / deepcopy / subclass override / module patch / hook SyntaxError / 名義インスタンス未定義） | Sprint 0.5 内 or Sprint 1 冒頭 |
| **MEDIUM** | 4 | Sprint 0.5 内 |
| **LOW** | 2 | Sprint 1 |

---

## 8. ADR-007 への反証

ADR-007 採用案 B の前提:
> 同じパターンを 3 回繰り返す愚を回避

**反証**: 結果として「3 回目の同型失敗」を別の形で繰り返している。`__new__` / pickle / monkey-patch は Python の標準的な動的言語機能であり、想定すべきだった。

**真の対策は ADR-008（仮）で「frozen design + final class enforcement + 名義インスタンス module 化」を導入すること**。

---

## 9. 推奨アクション（優先度順）

### Sprint 0.5 内で必須（FAIL 解除条件 / D 案採用時）:
- Bug-7: `tradovate_breaker` / `moomoo_breaker` を `common_v3/self_healing/instances.py` で定義（spec 完全実装）
- Bug-3: hook SyntaxError 時 string match fallback
- Bug-5: xfail マーカー strict=True or 削除

### Sprint 1 で（ADR-008 として）:
- CRITICAL-1/2 + HIGH-1〜4: frozen design + `__slots__` + property setter + `__init_subclass__` + pickle `__reduce__` override
- MEDIUM-1, 2: approver NFKC + whitelist + str subclass 禁止
- Op-3: bypass log silent-allow パターン記録

---

## 10. 最終判定

**判定**: **FAIL**

**根拠（200字以内）**:
ADR-007 核心「runtime guard で全 bypass 経路を塞ぐ」が崩壊。`__new__`/pickle/post-init mutation/subclass `__init__` override/module monkey-patch の 6 経路で BYPASS 実測（CRITICAL 2 / HIGH 4）。approver 検証も exact-match のみで Unicode・任意文字列・str subclass で bypass。spec B14 L382-L383 の名義インスタンス未定義。xfail マーカー誤り。Navigator の regression 0 主張は full pytest で 7 件失敗。Sprint 0.5 内 修正必須（最低 4 件）。
