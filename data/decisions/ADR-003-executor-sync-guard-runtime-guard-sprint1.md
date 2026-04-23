# ADR-003: 案 B 採用 — #7 executor_sync_only_guard CONDITIONAL-PASS / Sprint 1 で runtime guard

**起票日**: 2026-04-23 06:30 JST
**起票者**: ソラ提案 → ゆうさくさん承認
**ステータス**: accepted（完了 / Sprint 1 持ち越し記録: `data/sprint1_carryovers.md` C-001）
**関連**: ADR-002 / Sprint 0.5 P0 #7

---

## コンテキスト

- Redteam #7 r1: FAIL（10 件 bypass・lambda/変数経由/getattr/wrapper/partial/dict lookup/`__import__` 等）
- builder 修正: C-09/C-10 + C-01/C-06/C-08A/C-08B Best-effort + C-02〜C-05/C-07 を xfail 永続化
- navigator: PASS（申し送り 2 件）
- Redteam r2: **FAIL**（NotebookEdit 完全素通り / C-09 半分修正 / lambda body 等で 14+ 新規 bypass）
- AST 静的解析の限界に到達（Redteam r2 自身が「runtime guard が抜本対策」と指摘）

## 選択肢

| 案 | 内容 | バグ発生率 | 工数 |
|---|---|---|---|
| A | builder 再修正で AST 補強（イタチごっこ 3 回目） | 中（また穴出る可能性） | 1-2h |
| **B** | **Phase 2 Sprint 1 で `@sync_only` デコレータ + runtime guard に切替**。AST hook は補助に格下げ・CONDITIONAL-PASS で先進める | 低（runtime で必ず止まる） | Sprint 1 で半日 |
| C | 現状を「Sprint 1 持ち越し」明示で受領・先進む | 中（穴ある状態で他作業） | 0 |

## 採用案

**採用**: B

**判断者**: ゆうさく承認

**理由**:
- AST 静的解析は dataflow / alias / 動的属性アクセスを原理的に追えない
- runtime guard（threading.current_thread() is main_thread() 検証）なら lambda/partial/getattr 全パス共通で必ず止まる
- Redteam r2 自身の S-01 指摘と一致

## 想定結果（事前）

- 短期: 現 hook を CONDITIONAL-PASS 扱いで先進む
- 中期（Sprint 1）: `@sync_only` デコレータ + runtime guard 実装で抜本解決

## 実結果（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- Sprint 1 持ち越し記録: `data/sprint1_carryovers.md` C-001 に物理記載済（忘却防止）
- Task #1 completed

## 振り返り（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- 学習: 物理強制の設計初手で AST hook を選んだのが遠回りだった
- ADR-007 (CircuitBreaker) で「最初から runtime guard 主防御」の設計判断を反映済み
- 学習転記候補: `memory/feedback_runtime_guard_over_ast_hook.md`

## 関連証跡

- `data/governance/redteam_audit_executor_sync_guard_20260423.md` (r1)
- `data/governance/navigator_audit_executor_sync_guard_20260423.md`
- `data/governance/redteam_audit_executor_sync_guard_20260423_r2.md` (r2)
- `data/sprint1_carryovers.md` C-001
