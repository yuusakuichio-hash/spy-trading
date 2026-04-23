# ADR-013: Sora Lab v3 戦術選定 + Sora Lab 独自価値実装計画

**起票日**: 2026-04-23
**起票者**: ソラ（Phase 0-D シンセシス）
**ステータス**: accepted v2（ゆうさくさん 4 件 + 3 者監査後補正 A-縮小承認済・2026-04-23）
**関連**: ADR-001 / ADR-012 / Phase 0-A 基準・Phase 0-B 調査・Phase 0-C 比較検証

---

## コンテキスト

- 2026-04-22 大転換「v3 は一からやり直し」方針
- Sprint 1-B Phase B 着手後にゆうさくさん指摘: 既存結果流用でなく v3 として独立調査すべき
- Phase 0（概念検証 + 調査）を実施 → 3 者独立検証で v3 戦術選定 + 独自価値抽出
- 本 ADR で戦術選定 + 独自価値実装計画を確定し、Sprint 1-B Phase B を再構成

## Phase 0 経緯

| Phase | 内容 | 成果物 |
|---|---|---|
| 0-A | 基準固定（27 項目 × 5 Level + 2 ルート合流プロトコル + マイクロマイルストーン）| `data/research_v3/phase0a_baseline_criteria_draft_20260423.md` |
| 0-B1 | Bottom-up 調査（Atlas 6 + Chronos 6 ペルソナ・Gemini/o3 抽出）| `data/research_v3/results/phase0b1_*.md` |
| 0-B2 | Top-down 概念駆動（Sora Lab 独自価値 8 軸・Strategist 抽出）| Phase 0-D 統合済 |
| 0-C | 合流プロトコル適用・比較検証 | `data/research_v3/phase0c_comparison_synthesis_20260423.md` |
| 0-D | 本 ADR シンセシス | 本ファイル |

## 決定事項（ゆうさくさん 4 件承認済）

### 決定 1: Atlas v3 戦術 2 本で開始（Sprint 1-B Phase B 範囲）

v1 案（3 本・Wheel 含む）から **3 者監査後に A-縮小へ補正**:

| 順 | 戦術 | Phase 0-B 出典 | 合流スコア |
|---|---|---|---|
| 1 | **統計的プレミアム売却（ic_sell 拡張）** | Gemini A1 | Bot 化 5/5 |
| 2 | **0DTE システム・スキャルピング** | Gemini A2 | Bot 化 5/5 |

**Wheel (A5) は資金成長後に追加**: o3 指摘「米オプション Wheel は証拠金要件大・現元本規模では Put 売り成立困難」を反映。元本成長まで保留。

**根拠**: A-縮小案の合流スコア 730 点（Wheel 資金制約反映後に候補中最高）

### 決定 2: 既存 Builder 成果の扱い

| 成果物 | 判定 | 理由 |
|---|---|---|
| `atlas_v3/core/engine.py` | **継続** | 戦術 3 本を `register_tactic()` で load |
| `atlas_v3/core/strategy_selector.py` | **継続** | PercentileSelector 動的閾値対応 |
| `atlas_v3/strategies/ic_sell.py` | **継続 + A1 向けに拡張** | 既存 IC Sell + Put Spread 系追加 |
| `atlas_v3/strategies/earnings_iv_crush.py` | **破棄** | 合流スコア 700 点（破棄が最高）|
| `atlas_v3/strategies/orb_1dte_spy.py` | **Sprint 2 転用検討** | A2 0DTE システムの素材として部分転用可能か評価 |

**新規実装必要**:
- `atlas_v3/strategies/0dte_system.py`（A2 0DTE スキャルピング）
- `atlas_v3/strategies/wheel.py` は **資金成長後に追加**（ADR-013 v2 補正）

### 決定 3: Chronos v3 戦術（Sprint 2 着手時）

合流スコア上位 4 本を Sprint 2 主軸に:

| 順 | 戦術 | Phase 0-B 出典 | Bot 化 | Prop 整合 |
|---|---|---|---|---|
| 1 | **US-RTH ORB Trend-Rider** | o3 C1 | 4/5 | MFFU 中心 |
| 2 | **Liquidity Sweep & VWAP Reclaim** | o3 C2 | 5/5 | Tradeify 戦術分離と整合 |
| 3 | **Event-Driven Vol Breakout (FOMC/NFP)** | o3 C5 | 4/5 | MFFU Pro + Bulenox 補助 |
| 4 | **Asia-Overnight Range Scalper** | o3 C6 | 5/5 | 24h 独自価値実現 |

**Sprint 2 持越し or 破棄**:
- Footprint Micro-Scalper（C3・Bot 化 3/5・高速裁量依存）→ Sprint 2 検討
- VWAP Mean-Revert Midday Fader（C4・Bot 化 4/5）→ Sprint 2 補完

### 決定 4: Sora Lab 独自価値 6 項目（実装優先順・v2 補正）

**v2 補正**: 軸 4「感情排除」は独立項目から削除し、軸 1「人間×Bot ハイブリッド」の下位制約として統合（Strategist 指摘のスコア順不整合解消）

| 順 | 項目 | 独自性 | 実装 Phase |
|---|---|---|---|
| 1 | OpenTimestamps 刻印自動化 | 10/10 | **Phase 0-D 完了時点で実施** |
| 2 | Multi-Agent 組織（Auditor 物理化）| 9/10 | Sprint 1-B Phase A 継続 |
| 3 | 人間 × Bot ハイブリッド（Pushover 書換 API 署名必須・感情物理隔離含む）| 9/10 | Sprint 1-B Phase B |
| 4 | メタ学習閉路（failure_to_rescue 拡張 + snapshot/version tag）| 8/10 | Sprint 1-B Phase B |
| 5 | 24h カバー（Sentinel daemon 強化）| 7/10 | Sprint 2（Chronos 着手時）|
| 6 | 95% 自動化（Auditor 事前フィルタ）| 6/10 | Sprint 1-B Phase B（2 完了後）|

### 決定 5: moomoo ペーパー初回銘柄

**SPY + QQQ**（合流スコア 980 点）

**理由**:
- 流動性最高・指数 ETF で SPY 類似
- earnings 依存最小（earnings 戦術破棄と整合）
- 動的閾値実装容易

### 決定 6: 2026-10 月 60 万未達時の分岐（Phase 0-A 承認再確認）

- B 採用: 4 層加速（元本増 + 戦略追加）
- **追加事項**: 別収益構造の戦略検討余地あり（戦略 B/C/D 新規・Task #17 起票済）

### 決定 7: LLC 設立タイミング

- B 採用: ペーパー 1 日稼働確認後（2026-05 頃）

### 決定 8: 月額予算上限

- B 採用: 月 5,000 円程度（o3 回数増 + OPRA 最小契約検討）

---

## Sprint 1-B Phase B 再構成計画

### 着手順序（3 者監査後の改訂見積・AI 作業時間 + バッファ）

| # | タスク | 実測見積 |
|---|---|---|
| 1 | **独自価値 1**: OpenTimestamps 刻印自動化 | 1-2h |
| 2 | **共通 Risk Engine**（o3 推奨・Plug-in 化・max_DD/VaR/assignment）| 2-3h |
| 3 | **Atlas 戦術 1**: ic_sell 拡張（A1 統計売り対応で Put Spread 系追加 + BT 2 年分）| 3-4h |
| 4 | **Atlas 戦術 2**: 0DTE システム新規実装（orb_1dte_spy 転用判定 + BT 2 年分）| 4-6h |
| 5 | **独自価値 3**: Pushover Bot パラメータ書換 API（署名必須・感情物理隔離含む）| 1-2h |
| 6 | **独自価値 4**: failure_to_rescue 拡張（snapshot + version tag）| 1-2h |
| 7 | **Atlas 戦術破棄**: earnings_iv_crush.py 削除 + orb_1dte_spy Sprint 2 判定 | 30 min |
| 8 | **Shadow Live 段階**（o3 推奨・発注 send → cancel で乖離測定）| 1-2h |
| 9 | **統合 E2E**: Atlas 戦術 2 本 + 独自価値 3 項目 + Risk Engine | 2-3h |
| 10 | **ペーパー運用要件 7 項目**（o3 指摘を ADR 内で展開）| 3-4h |
| **合計** | | **約 19-29h**（3 者指摘の工数改訂反映）|

### ペーパー運用要件 7 項目（Sprint 1-B Phase B 内で実装）

1. Paper API キー/シークレット取得 + vault 格納
2. リスクパラメータ（max_notional, max_daily_loss）Config 化
3. 24h 監視 + オンコール体制
4. Latency モニタ + バックオフ
5. 長時間 replay BT
6. 運用 Runbook + ロールバック手順
7. 法的コンプライアンス最終確認

### Phase B 再構成の承認点

1. 既存 Builder 成果の部分継続（engine/selector/ic_sell は使用）
2. 戦術 2 本差替（earnings 破棄・0DTE 新規・Wheel 新規）
3. Sora Lab 独自価値 4 項目の Phase B 内実装
4. Phase A 完了項目（C-001/C-004/C-005）は継続採用

---

## 想定結果（事前）

- Sprint 1-B Phase B 完了: 本 ADR 承認後 1-2 日（AI 実測）
- ペーパー実走開始: 2026-04-25 頃
- ペーパー実測月利確定: 2026-05-25 頃
- 複利運用開始: 2026-05-27 頃
- 2026-10 月 60 万達成目標（デッドライン）

---

## 関連証跡

- `data/research_v3/phase0a_baseline_criteria_draft_20260423.md`
- `data/research_v3/phase0c_comparison_synthesis_20260423.md`
- `data/research_v3/results/phase0b1_atlas_personas_input_20260423__gemini_*.md`
- `data/research_v3/results/phase0b1_chronos_personas_input_20260423__o3_*.md`
- Strategist Phase 0-B2 分析（会話ログ保持）
- `data/governance/concept_verify_*.md` 3 セット（Gemini/o3）
- `memory/project_atlas_monthly_rate_v6.md`
- `memory/project_mffu_5plan_pitfalls_20260419.md`
- `data/tradeify_full_spec_20260420.md`

---

## 実結果（事後追記）

（Sprint 1-B Phase B 再構成完了後に追記）

## 振り返り（事後追記）

（Sprint 1-B Phase B 再構成完了後に追記）
