# ADR-004: 案 B 採用 — バグ根治手法 50 調査（バックグラウンド + 即採用 1-2 件）

**起票日**: 2026-04-23 06:18 JST
**起票者**: ソラ提案 → ゆうさくさん承認
**ステータス**: accepted（調査完了 / 即採用は次サイクル）
**関連**: `data/research/bug_root_cure_methods_50_20260423.md`

---

## コンテキスト

- ゆうさくさん指摘: 「実装フェーズで小さく作って動作点検する手法（アジャイル？）が世界中にある・我々に取り入れる価値があるもの 50 ほど調査して」
- 当初解釈ミス（小さく作る系のみ）→ 修正: 「成果物のバグを根治するための実装方法・点検方法 50」
- 既存方針との重複なきよう既知扱いリスト先送り

## 選択肢

| 案 | 内容 | バグ発生率 |
|---|---|---|
| A | 並列バックグラウンド調査・採否は調査完了後ゆうさく判断 | 極低 |
| **B** | **調査 + 即採用 1-2 件 hook 化まで進める** | 中（採用と Redteam 修正の並走で文脈衝突） |
| C | Redteam 修正完了後に着手 | 極低（進度遅） |

## 採用案

**採用**: B

**判断者**: ゆうさく承認

**理由**:
- 進度第一（goal acceleration）
- 調査と Redteam 修正は別 agent / 別 context なので物理的衝突は限定的
- 即採用 1-2 件で「学んだことを即生かす」を体得

## 想定結果（事前）

- 短期: 100→50 候補絞り込み + TOP10 即採用候補
- 中期: TOP10 から 1-2 件 hook 化

## 実結果（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- 調査完了: `data/research/bug_root_cure_methods_50_20260423.md` (38KB / 635 行)
- TOP10 提示: Idempotency Key 必須化 / Mutation Testing CI gate / PBT 拡張 / Feature Flag Lifecycle Lint / Canary 3段階 / **Circuit Breaker（task #3 と統合可）** / Approval LLM 出力 / Schemathesis API / CoVe builder 完了 / Golden Master legacy
- 即 hook 化 1-2 件は CircuitBreaker (task #3) と統合する判断（ADR-007）

## 振り返り（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- TOP10 のうち #6 Circuit Breaker が task #3 と完全一致 = 別途調査せず統合できた点が良かった
- premortem hook block を 1 度経験（prompt に「完了報告」を含めた誤検知）→ prompt 修正で再投入成功
- 学習: agent 投入 prompt にトリガーワード混入させない注意

## 関連証跡

- `data/research/bug_root_cure_methods_50_20260423.md`
- `data/premortem_reports/20260423_061807_*.md` (調査投入時)
- general-purpose agent: a3cfc96ad801602a1（完了）
