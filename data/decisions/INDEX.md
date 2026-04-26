# Decision Log Index

**目的**: 刷新プロジェクトでの全判断を「あとで見返せる・どこで間違えたか分かる」形で残す
**起票**: 2026-04-23（ゆうさくさん指示）
**形式**: ADR (Architecture Decision Record)
**運用**: 重要判断は ADR 起票必須・テンプレートは `_TEMPLATE.md`

---

## 索引（時系列・新しい順）

| ADR | 日時 | タイトル | 判断者 | 結果 |
|---|---|---|---|---|
| [ADR-008](ADR-008-frozen-design-final-enforcement.md) | 2026-04-23 07:25 | frozen design + final class enforcement（CircuitBreaker 抜本対策） | ソラ自律（Sprint 1 着手時にゆうさく確認推奨） | proposed |
| [ADR-007](ADR-007-circuit-breaker-runtime-guard-first.md) | 2026-04-23 06:55 | CircuitBreaker は最初から runtime guard 主防御 | ソラ自律 | **判断ミス確定（ADR-008 で抜本対策）** |
| [ADR-006](ADR-006-atlas-chronos-stop-all-plist.md) | 2026-04-23 06:36 | Atlas/Chronos 全 launchd plist 退避 | ゆうさく承認 | 完了 |
| [ADR-005](ADR-005-mffu-dry-run-guard-conditional-pass.md) | 2026-04-23 06:48 | #8 mffu_dry_run_guard CONDITIONAL-PASS 受領（B' 採用） | ゆうさく承認 | 完了 |
| [ADR-004](ADR-004-research-50-methods-bg-investigation.md) | 2026-04-23 06:18 | バグ根治手法 50 調査・調査+即採用1-2件 | ゆうさく承認 | 完了 |
| [ADR-003](ADR-003-executor-sync-guard-runtime-guard-sprint1.md) | 2026-04-23 06:30 | #7 executor_sync_only_guard CONDITIONAL-PASS 受領（B 採用） | ゆうさく承認 | 完了 |
| [ADR-002](ADR-002-redteam-78-parallel-f1.md) | 2026-04-23 06:14 | F1 採用: Redteam #7/#8 並列投入後に #3 | ゆうさく承認 | 完了 |
| [ADR-001](ADR-001-phase2-sprint05-plan-A-prime.md) | 2026-04-23 03:30 | 案 A' 採用: Phase 2 Sprint 0.5（3 日・物理化のみ・取引ロジック禁止） | ゆうさく承認 | 進行中 |

---

## 索引（テーマ別）

### Phase 計画
- ADR-001: Phase 2 Sprint 0.5 計画

### Sprint 0.5 hook 修正
- ADR-002: Redteam #7/#8 投入順序
- ADR-003: #7 hook CONDITIONAL-PASS（runtime guard 持ち越し）
- ADR-005: #8 hook CONDITIONAL-PASS（runtime guard 持ち越し）
- ADR-007: #3 CircuitBreaker 設計（最初から runtime guard）

### 既存運用停止
- ADR-006: Atlas/Chronos 停止（全 plist 退避）

### 調査
- ADR-004: バグ根治手法 50 調査

---

## 振り返り済み（判断ミス検出）

| ADR | 判断ミスの内容 | 学習 |
|---|---|---|
| [ADR-007](ADR-007-circuit-breaker-runtime-guard-first.md) | runtime guard 主防御の核心崩壊（__init__ 単一関所依存）= Boeing 737MAX MCAS 型の単一センサ依存と同型 | ADR-008 で frozen design 抜本対策へ |

---

## 運用規律

1. **重要判断は ADR 起票必須**（ゆうさくさん判断を要した時 / ソラ自律で重要選択した時）
2. **想定結果 vs 実結果の対比**を事後追記（毎週または該当判断の影響発現時）
3. **判断ミス検出時は ADR に振り返り追記** + memory/feedback_*.md に学習転記
4. **新 ADR 起票時は INDEX.md 索引も更新**

## 検索コマンド

```bash
# 過去判断検索
grep -lE "判断者.*ゆうさく" data/decisions/ADR-*.md
grep -lE "バグ発生率.*高" data/decisions/ADR-*.md

# 振り返り済み検索
grep -lE "判断ミス" data/decisions/ADR-*.md
```
