# Phase 0-C 比較検証・合流プロトコル適用結果

**起票**: 2026-04-23 / 3 者 (Strategist/Gemini/o3) 並列結果の統合

**入力**:
- Bottom-up Atlas: `data/research_v3/results/phase0b1_atlas_personas_input_20260423__gemini_*.md`（6 ペルソナ）
- Bottom-up Chronos: `data/research_v3/results/phase0b1_chronos_personas_input_20260423__o3_*.md`（6 ペルソナ）
- Top-down Sora Lab: Strategist 分析（8 軸・独自性スコア済）
- Phase 0-A 基準: `data/research_v3/phase0a_baseline_criteria_draft_20260423.md`

---

## 1. Bottom-up (ルート 1) 12 ペルソナ一覧

### Atlas（オプション・Gemini 抽出）

| # | ペルソナ | 代表 | Bot 化 | 推奨優先 |
|---|---|---|---|---|
| A1 | 統計的プレミアム売却 (TastyTrade 流) | Sosnoff / Battista / Butler | 5/5 | ★ 推奨主軸 |
| A2 | 0DTE システム・スキャルピング | Chambless / Sun / DiscordPrp | 5/5 | ★ 推奨ハイブリッド |
| A3 | ボラ・レラティブ・バリュー (Sinclair 型) | Sinclair / Eifert / Natenberg | 4/5 | 補完 |
| A4 | イベント駆動・バイナリー予測 | Golden (VIX) / 決算専門 | 3/5 | Sprint 2 |
| A5 | 系統的インカム・ホイール (The Wheel) | Heitkoetter 他 | 4/5 | 補完（長期） |
| A6 | テールリスク・ヘッジ (Taleb 型) | Taleb / Spitznagel / Cole | 2/5 | 低優先 |

**Gemini 推奨**: A1 + A2 ハイブリッド・動的閾値（IVR パーセンタイル + VIX 乖離）・moomoo 銘柄は **QQQ または TSLA**

### Chronos（先物・o3 抽出）

| # | ペルソナ | 代表 | Bot 化 | 推奨優先 |
|---|---|---|---|---|
| C1 | US-RTH ORB Trend-Rider | Diamond / Kell / Mando | 4/5 | ★ 推奨主軸 |
| C2 | Liquidity Sweep & VWAP Reclaim | ICT / Huddleston / Tradeify | 5/5 | ★ 推奨 (Tradeify 整合) |
| C3 | Footprint Order-Flow Micro-Scalper | Grady / Pulcini | 3/5 | Sprint 2 |
| C4 | VWAP Mean-Revert Midday Fader | FuturesTrader71 / Barrett | 4/5 | 補完 |
| C5 | Event-Driven Vol Breakout (FOMC/NFP) | Newsquawk / DeltaOne | 4/5 | ★ 推奨 |
| C6 | Asia-Overnight Range Scalper | OzarkTrader / MES_Kiwi | 5/5 | 補完 (24h 活用) |

**o3 推奨**: C1 + C2 + C5 主軸、C3/C4/C6 で PnL 平滑化

---

## 2. Top-down (ルート 2) Sora Lab 独自価値 8 軸（Strategist 抽出）

| 軸 | 名前 | 独自性 | 採用判定 |
|---|---|---|---|
| 8 | OpenTimestamps 1st mover | 10/10 | **自動採用**（最高独自性）|
| 6 | Multi-Agent 組織 | 9/10 | **自動採用**（全軸基盤）|
| 1 | 人間 × Bot ハイブリッド | 9/10 | **自動採用**（軸 4/7 統合）|
| 5 | メタ学習 (Self-Correction 閉路) | 8/10 | **自動採用**（リスク耐性）|
| 2 | 24h 全カバー | 7/10 | 条件付採用（Chronos 24h と整合）|
| 3 | 大量並列 (異質 alpha) | 6/10 | 条件付採用（Bottom-up 合流判定）|
| 7 | 95% 自動化 | 6/10 | 軸 1 配下統合（独立非採用）|
| 4 | 感情排除 | 4/10 | 軸 1 配下統合（独立非採用）|

---

## 3. 合流プロトコル適用結果

### 採用ルール（Phase 0-A 定義）

| 状況 | 判定 |
|---|---|
| 両ルートで top 3 | 自動採用 |
| 片方のみ top 3 | Auditor 事前判断 → ゆうさくさん週次まとめ |
| 両ルート低位 | 自動破棄 |
| 片方で top 1 + 独自性 > 50% | Sora Lab 独自価値として採用 |

### Phase 0-C 判定マトリクス

| 項目 | Bottom-up | Top-down | 判定 | 理由 |
|---|---|---|---|---|
| **Atlas 主戦術**: 統計売り + 0DTE（A1 + A2）| top 1-2 両方 | 軸 1 ハイブリッドと整合 | **自動採用** | Bot 化 5/5・推奨一致 |
| **Chronos 主戦術**: ORB + VWAP + FOMC（C1/C2/C5）| top 1/2/5 | 軸 2 24h と整合 | **自動採用** | Bot 化 4-5/5・Tradeify 戦術分離 (VWAP Reclaim) と整合 |
| **Atlas 補完**: Sinclair 型 (A3)・Wheel 型 (A5) | top 3-5 | 軸 3 異質並列と整合 | 条件付採用 | Sprint 2 検討 |
| **Chronos 補完**: Asia Overnight (C6) | top 6 | 軸 2 24h 直結 | **採用** | Bot 化 5/5・夜間カバー独自性 |
| **Multi-Agent 組織**（Builder/Nav/Redteam/Auditor）| なし | 軸 6・9/10 | **Sora Lab 独自採用** | Bottom-up に類似事例なし（Strategist 確認）|
| **OpenTimestamps 1st mover** | なし | 軸 8・10/10 | **Sora Lab 独自採用** | 業界先行例未確認 |
| **メタ学習閉路**（失敗→memory→hook 物理化）| なし | 軸 5・8/10 | **Sora Lab 独自採用** | RenTec 等も非公開・公開閉路は固有 |
| **人間感情の物理隔離**（sora_journal/ 隔離）| なし | 軸 4（軸 1 下位） | **Sora Lab 独自採用** | 規律 #7 整合 |
| **Atlas テールリスク (A6)** | 低位 2/5 | なし | **破棄 or Sprint 2+** | Bot 化低・精神コスト高 |
| **Chronos Footprint (C3)** | 低位 3/5 | なし | **Sprint 2** | 高速裁量依存・Bot 化困難 |

---

## 4. v3 設計の必要項目（Phase 0-D シンセシス入力）

### 4.1 戦術実装範囲（Sprint 1-B Phase B 再構成）

#### Atlas v3 戦術（既存 Builder 成果との照合）

| 既存実装 | Phase 0-C 判定 | 対応 |
|---|---|---|
| ic_sell (IC Sell・Bottom-up A1 の IC 版) | **採用継続** | Builder 成果使用可 |
| earnings_iv_crush (Bottom-up A4 相当) | **降格**（Bot 化 3/5） | Sprint 2 持越し候補 |
| orb_1dte_spy (Bottom-up なし・Top-down 軸 2 でも Atlas 範囲外) | **要再検討** | 0DTE スキャルピングと統合 or 別実装 |

**Phase 0-C 推奨の Atlas 戦術 3 本**:
1. 統計的売り (A1 IC/Strangle/Put Spread) → 既存 ic_sell 拡張
2. **0DTE システム (A2)** → 新規実装（既存 orb_1dte_spy と重複整理）
3. Wheel (A5) → 新規実装（長期安定・元本増用）

#### Chronos v3 戦術（Sprint 2 着手時）

1. ORB Trend-Rider (C1) → MFFU 中心
2. Liquidity Sweep + VWAP Reclaim (C2) → Tradeify 戦術分離と整合
3. Event-Driven Vol Breakout (C5) → MFFU Pro + Bulenox 補助
4. Asia Overnight (C6) → 24h 独自価値実現

### 4.2 Sora Lab 独自価値項目（v3 設計必須）

| 項目 | 実装必要内容 |
|---|---|
| Multi-Agent 組織 | Auditor 物理化 (ADR-008 継続)・デッドロック escalation・通信ログ改竄検知 |
| OpenTimestamps | Phase 0-B 監査ログ刻印自動化・出口戦略時活用 |
| 人間 × Bot ハイブリッド | Pushover → Bot パラメータ書換 API（署名必須）・月次 M&M 裁量反映・OpenTimestamps 刻印 |
| メタ学習閉路 | failure_to_rescue.py 拡張・snapshot + version tag 強制・memory→hook 自動生成パイプライン |
| 24h カバー | Sentinel non-LLM daemon・quiet hours・起床時サマリ自動・ゆうさくさん不在時 Auditor 暫定権限 |
| 感情排除物理化 | journal 隔離 hook（既存）・personal 流入検知 linter |
| 95% 自動化 | Auditor 事前フィルタ・月次メトリクス（5% 判断帯域内か）|

### 4.3 共通基盤（Sprint 0.5 + 1-B Phase A で既実装）

- common_v3/risk/kill_switch.py
- common_v3/idempotency/store.py
- common_v3/observability/deadman.py
- common_v3/self_healing/circuit_breaker.py
- common_v3/executor/sync_guard.py
- chronos_v3/prop/mffu_flex.py

### 4.4 既存 Builder 成果の扱い

| 成果物 | 判定 |
|---|---|
| atlas_v3/core/engine.py + strategy_selector.py | **継続** (Phase 0-C 推奨戦術 3 本を register_tactic で load) |
| atlas_v3/strategies/ic_sell.py | **継続** (A1 統計売りの一部) |
| atlas_v3/strategies/earnings_iv_crush.py | **降格**・Sprint 2 持越し or 破棄判断 |
| atlas_v3/strategies/orb_1dte_spy.py | **要再検討**・0DTE システム (A2) と統合判断 |

---

## 5. 次 Phase 0-D シンセシス + ADR-013 入力

### Phase 0-D 作業内容

1. 上記 4 項目を統合した **ADR-013** 起票（v3 戦術選定 + 独自価値実装計画）
2. 3 者再検証（Strategist + Gemini + o3）で最終チェック
3. ゆうさくさん最終承認
4. Sprint 1-B Phase B 再開（戦術 3 本の再構成）

### ADR-013 ドラフト項目

- 戦術選定結果（Atlas 3 本 / Chronos Sprint 2・計 7 本）
- Sora Lab 独自価値実装計画（7 項目）
- 既存 Builder 成果の採否
- Sprint 1-B Phase B 再構成手順

---

## 6. ゆうさくさん承認要件

| # | 項目 | 判断 |
|---|---|---|
| 1 | Atlas 戦術 3 本: ic_sell 拡張 + 0DTE システム + Wheel | OK / 変更希望 |
| 2 | earnings_iv_crush + orb_1dte_spy を Sprint 2 or 破棄 | どちら |
| 3 | Sora Lab 独自価値 7 項目の優先実装順 | 推奨に従う / 指定 |
| 4 | moomoo 銘柄: SPY + QQQ か SPY + TSLA | どちら（Gemini 推奨）|
