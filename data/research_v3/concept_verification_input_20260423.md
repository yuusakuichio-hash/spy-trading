# Sora Lab / Atlas / Chronos 概念修正版 + 進め方（2026-04-23）

## 背景
- 2026-04-22 大転換日: 全コード書き直し + v3 は一からやり直し方針
- ゆうさくさんから「既存結果流用でなく v3 として真に一から再調査せよ」と指摘受領
- その後、概念定義自体が浅い + 進め方が不十分と再指摘・今回検証依頼

---

## 概念修正版

### 🏢 Sora Lab（プロジェクト組織全体）
- **ゆうさくさんの目標達成のための自律型 AI チーム**
- 活動領域: チームビルディング / 戦略会議 / 調査・分析 / コード実装 / 運用 / SNS / 人的資源管理 等 **全方位**
- 現在の主対象: 金融（Atlas / Chronos）= 目標達成手段の 1 つに過ぎない
- 将来対象: 戦略 B/C/D（音楽・AI 技術・金融外収益柱）等に拡張
- 構成: ゆうさくさん（オーナー）+ ソラ（AI 秘書・窓口）+ サブエージェント群（Builder / Navigator / Redteam / Auditor 等）

### 🌐 Atlas（Bot #1 · オプション）
- **優秀なオプショントレーダーの行動 × BOT 運用の強み = 最強トレーダー BOT**
- 強みの源泉: 人間の判断規律（discipline）+ Bot の実行力（速度・24h・感情排除・大量データ処理）
- 戦場: moomoo オプション（自己資金）・マルチ銘柄 × マルチ戦術 × 環境適応型
- ペーパー初回: SPX 取扱不可 → SPY + 調査結果で選定する別銘柄 1-2 種

### ⏰ Chronos（Bot #2 · 先物）
- **優秀な先物トレーダー × プロップファームトレーダーの行動 × BOT 運用の強み = 最強トレーダー BOT**
- 戦場: 各種プロップファーム（MFFU 4 プラン + Tradeify + 他自動化対応 prop）
- 強みの源泉: 人間の判断規律 + prop firm ルール遵守の精密性 + Bot の実行力 + 24h 市場カバー
- マルチ戦術 × 環境適応型（Atlas と同じ設計思想）

---

## 進め方（軸 A + 軸 B 並列）

### 軸 A: 概念 → 実装への落とし込み方法論の調査（50 個）
- ソフトウェア設計: DDD / Clean Architecture / Hexagonal / Event Sourcing / CQRS / TDD / BDD 等
- プロセス: OODA / PDCA / Build-Measure-Learn / Event Storming / C4 Model
- 運用: SRE / Observability / Chaos Engineering / Canary / 12-Factor App
- 耐障害性: Circuit Breaker / Idempotency / Saga / Retry / DLQ
- AI / Agent: Multi-Agent / Agent-Oriented SE / RAG / Reflexion / CoT
- 金融 Bot 特有: Walk-forward / Monte Carlo / Paper Trading Protocol / Slippage modeling / Fill simulation / VWAP/TWAP / Market Microstructure / Position sizing
- 設計パターン: GoF 23 / Repository / Specification / State Machine

各項目について: **概念 / Sora Lab での有用性 / 実装方法 / 実装時の注意点** を抽出

### 軸 B: 優秀トレーダー調査（30-50 人）
- Atlas（オプション）: 起床〜就寝フェーズ / エントリー判断 / サイズ決定 / 損切り規律 / 対象銘柄選び / 戦術種類 / 決算イベント対応 / ポートフォリオ運営 / データソース / 生存者バイアス自己検証
- Chronos（先物 + prop firm）: 6 フェーズ / エントリーパターン / 時間帯別 / prop firm ルール / 隠れ制約 / リスク管理 / 銘柄選定 / データソース

### 両軸のシンセシス
軸 A（どう実装するか）× 軸 B（何を実装するか）→ v3 設計の必要項目確定

### 工数見積
- Phase 0-1 概念確定: 30 分
- Phase 0-2 軸 A 50 個調査: 1 日
- Phase 0-3 軸 B 優秀トレーダー調査: 1-2 日
- Phase 0-4 両軸シンセシス + 必要項目確定: 半日
- Phase 0-5 v3 設計 ADR-013 起票: 半日
- 合計: 3-4 日

---

## 検証依頼

1. **概念修正版の抜け**: Sora Lab / Atlas / Chronos の定義で見落としがあるか
2. **進め方の問題**: 軸 A + 軸 B 並列 + シンセシスの流れに欠陥があるか
3. **他に考慮すべき観点**: 起草者（Claude）が気づけない盲点
4. **優先順位**: 軸 A / 軸 B どちらを先にすべきか、並列の妥当性
5. **工数妥当性**: 3-4 日で完遂可能か、もっと短く/長くすべきか
