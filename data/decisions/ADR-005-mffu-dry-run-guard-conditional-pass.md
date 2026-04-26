# ADR-005: 案 B' 採用 — #8 mffu_dry_run_guard CONDITIONAL-PASS / Sprint 1 で runtime guard

**起票日**: 2026-04-23 06:48 JST
**起票者**: ソラ提案 → ゆうさくさん承認（「それでいいよ」）
**ステータス**: accepted（完了 / Sprint 1 持ち越し記録: `data/sprint1_carryovers.md` C-004）
**関連**: ADR-003（#7 と同型判断）

---

## コンテキスト

- Redteam #8 r1: FAIL（17 件中 12 件素通り）
- builder 7 件修正（C-1 絶対パス / C-2 部分 / C-4 部分 / H-2 nested / H-3 positional / C-5 contract test 31 ケース / M-1 bypass log）
- navigator: PASS（申し送り 2 件: xfail strict / NotebookEdit）
- Redteam r2: **FAIL**（fake repo root 攻撃 / AST 14+ 新規 bypass / spec L190 起動時 guard 未実装）
- #7 と同じ AST 静的解析の限界

## 選択肢

| 案 | 内容 | バグ発生率 | 工数 |
|---|---|---|---|
| **B'** | **#7 と同じ判断: Sprint 1 で runtime guard 切替（spec L190 の MFFUFlexRules 起動時 guard を Phase 2 で実装）。AST hook は CONDITIONAL-PASS 扱い** | 低（runtime で必ず止まる） | 0（先進める） |
| C | 軽い修正だけ Sprint 0.5 内で着手（fake repo root + bypass log permission error の 2 件）→ 再 redteam | 中（また何か出る可能性） | 30-60min |
| D | builder 再修正で 7 項目フル対応 → 再 redteam（イタチごっこ 4 周目） | 中-高 | 1-2h |

## 採用案

**採用**: B'

**判断者**: ゆうさく承認

**理由**:
- #7 と完全に同型問題（AST hook 限界）
- spec L190 で MFFUFlexRules 起動時 guard が既に明記されている = Phase 2 で MFFUFlexRules 実装する時に必ず作る = 二度手間回避
- 今 AST に積み上げても Sprint 1 で runtime guard 入れた瞬間に大半が冗長になる
- builder #8 修正自体は Sprint 1 への足掛かりとして有用

## 想定結果（事前）

- 短期: 現 hook を CONDITIONAL-PASS 扱いで先進む
- 中期（Sprint 1）: `chronos_v3/prop/mffu_flex.py` 実装時に起動時 runtime guard 実装

## 実結果（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- Sprint 1 持ち越し記録: `data/sprint1_carryovers.md` C-004 に物理記載済
- navigator audit 報告ファイル「物理不在」問題を Redteam r2 が指摘 → 私が代理書き出し（navigator agent に Write 権限なし・指示ミス）
- Task #2 completed

## 振り返り（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- 学習 1: navigator agent に Write 権限がないこと知らず指示書いた → agent definition の権限確認を投入前にすべき
- 学習 2: ADR-003 と同じパターン繰り返し = AST hook 主防御の限界が確定的
- ADR-007 で「最初から runtime guard 主防御」の設計判断を反映

## 関連証跡

- `data/governance/redteam_audit_mffu_dry_run_guard_20260423.md` (r1)
- `data/governance/navigator_audit_mffu_dry_run_guard_20260423.md`（私が代理書き出し）
- `data/governance/redteam_audit_mffu_dry_run_guard_20260423_r2.md` (r2)
- `data/sprint1_carryovers.md` C-004
