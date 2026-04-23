# ADR-001: 案 A' 採用 — Phase 2 Sprint 0.5（3 日・物理化のみ・取引ロジック禁止）

**起票日**: 2026-04-23 03:30 JST
**起票者**: ソラ提案 → ゆうさくさん承認
**ステータス**: accepted（進行中）
**関連**: `memory/CURRENT_STATE.md` L11-L23 / `data/specs/v3/`

---

## コンテキスト

- Phase 1 C-2 dry-run R2b 完了
- 仕様書 v3 R1→R2→R2a→R2b 4 サイクル経て両 agent（Gemini/Redteam）が「3 サイクルで非収束 = 変質・CCF 内側盲点残存」と自己警告
- 仕様策定だけで時間消費・実装に進めない懸念
- 一方で Phase 2 Sprint 1 で Builder 着手すると取引ロジックの設計判断と物理化が並走して context 過剰

## 選択肢

| 案 | 内容 | バグ発生率 | 工数 |
|---|---|---|---|
| A | Sprint 1 直接着手（取引ロジック含む） | 高（context 過剰） | 1ヶ月+ |
| **A'** | **Sprint 0.5（3 日・物理化のみ・取引ロジック禁止）→ o3 先行レビュー → Sprint 1** | 低 | 3日 + Sprint 1 |
| B | 仕様 R3 でさらに精緻化 | 極低（進度ゼロ） | 1週間+ |

## 採用案

**採用**: A'

**判断者**: ゆうさく承認

**理由**:
- 3 サイクル非収束 = 仕様だけでは解決しない・実装で初めて見える盲点がある
- 物理化のみに限定すれば取引ロジック設計と分離・各々の context 軽量化
- o3 先行レビューで Sprint 1 着手前に独立検証

**選択しなかった理由**:
- A: context 過剰でバグ発生率高
- B: 進度ゼロ・無限の精緻化に陥る

## 想定結果（事前）

- 短期（3 日）: P0 8 件物理化完遂
- 中期（Sprint 1）: o3 承認済ベースラインから取引ロジック実装

## 実結果（事後追記）

**最終更新**: 2026-04-23 06:55 JST（Sprint 0.5 Day 1 進行中）

- P0 8 件のうち、現時点で実装着手:
  - #5 mffu_flex.yaml（CONDITIONAL-PASS / Sprint 1 持ち越し記録あり）
  - #6 TacticBase ABC（Builder/Navigator/Redteam 一巡）
  - #7 executor_sync_only_guard（CONDITIONAL-PASS / runtime guard Sprint 1 持ち越し）
  - #8 mffu_dry_run_guard（CONDITIONAL-PASS / runtime guard Sprint 1 持ち越し）
- 残: #1 Idempotency / #2 KillSwitch singleton / #3 CircuitBreaker / #4 Deadman path

## 振り返り（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- 想定外: AST hook の限界が #7/#8 で露呈（builder 修正→navigator PASS→redteam r2 FAIL のループ 2 回）
- 学習: 物理強制を「AST hook 主防御」で考えていたが、実装した結果「runtime guard 主防御 + AST hook 補助」が必要と判明
- 学習転記候補: `memory/feedback_runtime_guard_over_ast_hook.md`（新規）

## 関連証跡

- `data/governance/redteam_audit_phase0_20260422.md`
- `data/governance/gemini_verify/v3_verify_contexted_20260422_155233.md`
- session ID: 349b128e-47ab-447e-8bad-257466d1d7b8（前セッション）/ 21d2b139（現セッション）
