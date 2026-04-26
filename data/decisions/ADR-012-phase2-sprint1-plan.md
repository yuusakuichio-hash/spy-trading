# ADR-012: Phase 2 Sprint 1 計画（Bot 取引ロジック + runtime guard 化 + carryover 対応）

**起票日**: 2026-04-23 11:10 JST
**起票者**: ソラ自律（Sprint 0.5 完了直後・ADR-001 計画線上）
**ステータス**: accepted（案 B 採用・2026-04-23 11:30 JST）
**o3 レビュー**: NO-GO（CRITICAL 5 件）→ 案 B で範囲絞りと見積修正
**o3 レビュー全文**: `data/governance/o3_review_sprint1_20260423_115854.md`
**関連**: ADR-001（Sprint 0.5 完了）/ ADR-003/005/007/008（carryover 設計指針）/ `data/sprint1_carryovers.md`

---

## コンテキスト

- Phase 2 Sprint 0.5（3 日計画 / 実測 7h）完了・P0 8 件全件達成（2026-04-23）
- ペーパー実走開始前に **Bot 取引ロジック v3 実装 + Sprint 0.5 carryover C-001〜C-011 対応**
- Sprint 0.5 の見積ズレ（3 日→7h）反映で Sprint 1 も実測ベース短縮見積
- ADR-001 計画: Sprint 0.5 末 o3 先行レビュー → Sprint 1 着手

## 採用案

### 範囲（案 B 採用・o3 NO-GO 指摘を反映し Sprint 1 は段階絞り込み）

**Sprint 1 本体（1-2 週間想定）**:

| 項目 | 出典 | 工数 |
|---|---|---|
| **C-001** executor_sync_only_guard runtime 化（`@sync_only` + threading 検査・**asyncio 衝突検証必須**）| carryover | 1 日 |
| **C-004** mffu_dry_run_guard runtime 化（`MFFUFlexRules.__init__` raise）| carryover | 1 日 |
| **C-005** CircuitBreaker frozen design（ADR-008 方式）| carryover | 1-2 日 |
| Atlas v3 Bot 取引ロジック（`atlas_v3/strategies/` TacticBase 派生・**BT 結果参照 + スリッページ耐性検証**）| - | 2-3 日 |
| **ペーパー運用要件 7 項目**（paper API key vault / risk params config / 24h 監視 / Runbook / コンプラ / Latency モニタ / 長時間 replay BT）| o3 指摘 | 2-3 日 |
| 統合 E2E テスト（Atlas + C-001/004/005 guard 連動・asyncio 整合）| - | 半日〜1 日 |

**合計**: 7-10 開発日 / カレンダー 1-2 週間（並列化は C-001/004/005 の範囲のみ・Atlas v3 は後続）

**Sprint 2 に持ち越し**（Sprint 1 完遂 + ペーパー実測 30 日後に着手）:
- Chronos v3 Bot 取引ロジック（MFFU 5 プラン連携・BT 結果ベース設計）
- C-006 Deadman hardening（HIGH 4 件）
- C-007 Deadman spec path 乖離解消
- C-008 Idempotency HIGH 4 件 + B15 経由化
- C-009 earnings test fixture 化
- C-010 KillSwitch audit log B15 経由化
- C-011 KillSwitch HIGH 4 件
- B15 StorageBackend 実装

### 実行方針

1. **段階並列** — Phase A（C-001/004/005 同時並列）→ Phase B（Atlas v3 + 運用要件・asyncio 整合確認後）→ Phase C（統合 E2E）
2. **Navigator 並走監視** — Builder ごとに Navigator ペアリング（Sprint 0.5 で確立）
3. **Redteam サイクル** — 各完了時に Redteam 1 巡・CRITICAL は即差し戻し
4. **規律 #8 遵守** — 各完了は pytest 全件 + Navigator + Redteam PASS
5. **Brook's law 回避** — 並列は最大 3 Builder 同時まで・Redteam レビュー待ち行列に注意

### Sprint 1 完了基準

- C-001/C-004/C-005 runtime guard が全 CLOSED
- Atlas v3 Bot が paper モードで 1 サイクル E2E 通る
- ペーパー運用要件 7 項目（API key vault / risk params / 24h 監視 / Runbook / コンプラ / Latency / 長時間 replay BT）全 CLOSED
- Redteam CRITICAL ゼロ（Sprint 2 持越しは HIGH のみ）
- `@sync_only` と既存 asyncio `market_stream_v2` の衝突解消確認

## 選択肢比較（バグ発生率）

| 案 | 内容 | バグ発生率 | 工数 |
|---|---|---|---|
| A | Sprint 1 で 13 項目一括対応・並列 Builder 3-4 名・2-3 日見積 | **高**（o3 NO-GO: 工数過小・統合リスク・asyncio 衝突未検討）| 2-3 日（架空）|
| **★ B** | **Sprint 1 を C-001/004/005 + Atlas v3 + 運用要件に絞る（Chronos と残 carryover は Sprint 2）**| 低（工数確度高・段階的）| **7-10 日 / カレンダー 1-2 週** |
| C | 現状 ADR-012 初稿のまま強行 | 高（CRITICAL 5 件未対応）| - |

**採用: B**

**判断者**: ゆうさく承認（2026-04-23 11:30 JST）

**理由**:
- o3 レビュー NO-GO・CRITICAL 5 件指摘で A 案の工数 2-3 日は過小と判明
- Sprint 0.5 の 7h 実績は「scaffold 生成」であり、今回必要な「実装・結合・障害注入テスト」とは複雑度が桁違い（o3 指摘）
- `@sync_only` と既存 asyncio の衝突（Atlas 起動不能リスク）を先に解消すべき
- ペーパー運用要件 7 項目（API key vault / 24h 監視 / Runbook 等）を Sprint 1 内に組込 → ペーパー開始の実運用可能性を確保
- C（Chronos / C-006〜C-011 / B15）は Sprint 2 に回し、Sprint 1 は確実に完遂

## 想定結果（事前・案 B ベース）

- Sprint 1-B 完遂: 2026-04-23〜2026-05-06 頃（1-2 週間）
- ペーパー実走開始: **2026-05-07 頃**
- ペーパー実測月利確定: 2026-06-06 頃
- 複利運用開始: **2026-06-08 頃**
- デッドライン 2026-10 まで: 約 4-5 ヶ月複利余地あり

## 関連証跡（計画時）

- `data/sprint1_carryovers.md` C-001〜C-011
- `memory/CURRENT_STATE.md`
- `data/specs/v3/common_spec_v3_20260422.md`
- `data/decisions/ADR-001-phase2-sprint05-plan-A-prime.md`

## 実結果（事後追記）

（Sprint 1 完了後に追記）

## 振り返り（事後追記）

（Sprint 1 完了後に追記）
