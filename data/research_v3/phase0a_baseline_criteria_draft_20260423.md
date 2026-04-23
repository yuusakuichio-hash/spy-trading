# Phase 0-A 基準固定 draft v2（27 項目 × 5 Level + 2 ルート合流プロトコル + マイクロマイルストーン）

**起票**: 2026-04-23 / ソラ draft v2 · ゆうさくさん承認待ち
**承認経緯**: 3 者検証 (Strategist + Gemini + o3) 2 回実施で CONDITIONAL-GO → 全指摘統合済
**関連**:
- `data/governance/concept_verify_{gemini,o3}_20260423_{160417,164932,171816}.md`
- `data/research_v3/phase0a_criteria_review_20260423.md`

---

## 変更サマリ（v1 → v2）

| 変更 | 内容 |
|---|---|
| 項目追加 | #25 最終出口戦略 / #26 物理時間考慮 / #27 OSS 依存管理（+ 既存項目への統合多数）|
| 番号整合 | 24 → 27 に更新・番号連続性確認済 |
| 期限数値化 | 各 Phase 着手目標日を明示 |
| rollback 粒度 | マイクロマイルストーン不合格時の戻し範囲を明記 |
| 判断要件拡張 | #13 ガバナンス / #23 境界 / #18 リソース配分 / #25 出口戦略 を追加 |
| 合流重み客観化 | 測定方法を定量ルール化 |
| 月 3-5 件保護 | Auditor 事前フィルタ → ゆうさくさん最終、の段階化 |
| gating フロー | Navigator / Auditor 介入点を明示 |
| Phase 0 特例 | 初期は週 5-10 件の意思決定必要（Gemini 指摘）|

---

## L1 最優先（法務・負け源・時間軸 — Phase 0-B 着手前 gating）

### 1. 法規制・コンプラ gating チェック
- 証券法（日本金商法 / 米証取法）
- 投資助言業登録要否（AI 自動売買・C2 除外と整合・`memory/project_c2_excluded.md`）
- CTA 登録要否
- KYC/AML 対策（moomoo / prop firm onboarding）
- prop firm T&C + API 利用規約
- 税務枠組（雑所得総合課税 vs 法人・`memory/project_atlas_tax_correction_20260420.md`）
- **LLM 自動レポートの prop 外部シグナル共有禁止条項抵触チェック**（o3）
- **CFTC AI 取引ガイドライン 2026Q1 パブコメ監視**（o3）

**ゆうさくさん判断要件**: 個人/法人/妻役員の構成・LLC 設立タイミング
**gating 基準**: NO-GO 出たら Phase 0-B 即停止

### 2. 負けパターン定義
- 虚偽完了（過去 8 件・`memory/feedback_false_completion_*`）
- silent failure / flag race / idempotency 不全 / prop 違反
- データライセンス違反（OPRA/CME 大量 DL 一発停止）
- レイテンシ死（研究 EV + → 実運用 EV −）
- AI 特有: モデル流出 / プロンプト注入 / 重学習攻撃

**設計制約化**: 各パターンに検知・自動停止経路を v3 設計に組込必須

### 3. 撤退条件・時間軸（KPI + 外生ショック統合）
- **KPI 軸**: 月次 DD 上限 / 連続赤字日数閾値 / ペーパー N 日不合格
- **外生ショック軸**（o3）: 法規制改正 / prop 規約改訂 / データ供給停止 / API 突然停止
- prop 違反で即停止
- **2026-10 月 60 万未達時の分岐**（ゆうさくさん判断要件）
- **Phase 0-B 着手目標**: 2026-04-25 まで Phase 0-A 確定 → 着手

### 4. データライセンス・コスト
- OPRA 大量 DL 禁止 / **Real-Time Display は別契約**（o3）
- CME 類似制約
- moomoo API rate limit / build 制限
- 月額予算上限（AI 推論 + データ・ゆうさくさん判断要件）

---

## L2 必須（KPI・成功基準・絞込 — Phase 0-B と並行可）

### 5. KPI 閾値
- Atlas / Chronos / Sora Lab 全体の勝率 / PF / DD / Sharpe / 月利（v6 数値: `memory/project_atlas_monthly_rate_v6.md`）
- ゆうさくさん承認必要

### 6. 成功基準
- ペーパー → 本番移行条件（N 日・KPI 閾値クリア・回帰ゼロ）
- 本番継続条件（月次 KPI 維持・DD 閾値内）
- パフォーマンス維持（30 日移動平均でドリフト検知）

### 7. 絞込基準（粒度 + 深度）
- **粒度定義**（o3 指摘）: 軸 A は「1 手法 = 1 概念・論文相当」で 1 パラメータ違いを同一扱い
- **軸 B 一次情報収集手段**（o3）: 文献 / 公開動画 / SNS 観察（インタビュー/DM/有料コースは後続 Phase）
- **深度レベル**: L0 文献 / L1 軽量コードレビュー / L2 実装+BT / L3 ペーパー
- フェーズゲート: 各 Level で合格した項目のみ次 Level へ
- **数**: 50 個 → **15-20 個に縮小**（3 者一致指摘）

### 8. 外部監査マイルストーン + 虚偽完了メタ化防止
- **5 項目ごとに Gemini + o3 クロスチェック**（3 者一致）
- **虚偽完了メタ化防止**（Gemini 最鋭）: 検証用偽装データ生成を検知する二重レビュー
  - 第一層: 調査結果 vs 原典 hash 突合
  - 第二層: 別 session の Claude で独立再調査 → 差分検知
- **rollback 粒度**: 5 項目中 1 件 NG → 該当項目のみ個別戻し（全件戻しは過剰）
- 監査ログは OpenTimestamps 刻印（改竄検知）

### 9. 法的実体タイミング
- LLC 設立（`memory/project_llc_establishment_20260419.md`）
- 妻役員化（`memory/project_wife_officer_incorporation_20260420.md`）
- prop 名義（個人 vs 法人・`memory/project_prop_firm_dual_layer_20260420.md`）
- 責任帰属（個人 / LLC / Sora Lab 組織）

---

## L3 セキュリティ・データ

### 10. セキュリティ（AI 特有 + 内部不正 + 権限昇格）
- 鍵管理: `.env` 一元管理
- GitHub: プライベートリポジトリ・edge 流出禁止
- 2FA: 取引口座 + prop firm + GitHub + cloud
- **AI 特有**: モデル流出 / プロンプト注入 / 重学習攻撃
- **内部不正対策**（o3）: 従業員/共同開発者アクセス監査ログ
- **権限昇格リスク**（o3）: Navigator→Builder 一時 root が戻らない経路の物理ブロック

### 11. データ整合性
- Garbage In Out 防止・ソース検証プロセス
- ライセンス遵守（#4 連動）
- 品質: ギャップ検出・欠損補完・異常値検出

### 12. インフラ堅牢性・可用性
- サーバ停止時挙動（Bot 自動停止・手動介入経路）
- API ダウン対応（Tradovate / moomoo outage 時）
- **BIA/DR**: 損失上限・復旧手順
- 人の離脱・病欠・ゆうさくさん不在シナリオ

---

## L4 ガバナンス・組織

### 13. ガバナンス境界
- **ゆうさくさん**: オーナー・最終決裁（Phase 0 初期は週 5-10 件の意思決定想定・Gemini 指摘）
- **ソラ**: Secretary・窓口・単独完了判定禁止
- **Navigator**: Builder 並走・実装差し戻し権
- **Auditor**（Gemini/o3）: 週次 M&M + 重大判断
- **Redteam**: 別 session 敵対レビュー
- 資本・収益分配・失敗時責任: ゆうさくさん / LLC 帰属

**ゆうさくさん判断要件**: 各ロールの決裁権境界（金額閾値 / 技術判断範囲）

### 14. エージェント調停 + デッドロック物理介入（Gemini）
- Builder vs Navigator vs Redteam 衝突時の調停プロトコル
- Captain van Zanten 構造（ソラ最終判断禁止）
- Auditor 物理化（ADR-008 + o3 マイクロマイルストーン）
- **デッドロック時のゆうさくさん物理介入トリガー**（Gemini）: 同一論点で 3 往復しても合意不達なら自動 escalation

### 15. 学習 AAR ループ + moving target 対策
- 失敗 → memory → hook 物理化の閉路
- メタ学習（`scripts/failure_to_rescue.py` 拡張）
- 自己修正（単なる Automation でなく Self-Correction・Gemini 指摘）
- **バックテスト一貫性**（o3）: 連続モデルアップデート時の過去パフォーマンス検証性を保持（snapshot + version tag）

### 16. 依存資産インベントリ + 爆発半径制御
- 既存 common/* 再利用可否判定
- 書換禁止規律との整合（`legacy_write_block.sh`）
- v3 で何を破棄・何を継承（sunk cost 判断含む）
- **Atlas/Chronos で共通 common_v3 のバグ道連れ制御**（Gemini）: test boundary + blast radius test

---

## L5 運用・リソース

### 17. ユーザ心理・規律（設計制約化）
- 非エンジニア判断帯域（通常月 3-5 件・Phase 0 初期は週 5-10 件）
- プライベート領域保護（`memory/feedback_no_private_life_intrusion_20260422.md`）
- 休憩差し込み提案禁止（`memory/feedback_no_break_suggestion_20260423.md`）

### 18. 人的資源配分 + 非金融シナジー（Gemini/o3 統合）
- 戦略 B/C/D とのゆうさくさん時間競合管理
- カレンダーブロック方式
- **金融 DD 時の非金融プロジェクト縮退アルゴリズム**（Gemini）
- **非金融（SNS/音楽）優先順位変更フロー**（o3）

**ゆうさくさん判断要件**: 金融 vs 非金融の月次リソース配分比率

### 19. 段階的 MVP + 評価ベンチマーク
- 1 市場・1 手法・1 prop firm で β 検証 → 拡張
- Atlas: SPY + 1 銘柄から開始
- Chronos: MFFU Flex 1 プランから開始
- 評価ベンチマーク標準化（共通データセット・標準指標）

### 20. 資金・Treasury 設計 + ポジション重複検知
- 自己資金 → Atlas 複利 → Chronos 拡張
- 口座分散（moomoo + prop × N）
- Atlas/Chronos 併用リスクシェア
- **Atlas/Chronos 間リスク相関・ポジション重複検知**（o3）: 同一インデックス先物 vs ETF オプション衝突

**ゆうさくさん判断要件**: Atlas→Chronos 資金移動トリガー（複利額閾値）

### 21. 観測可能性（Observability）
- telemetry: 注文 / 約定 / 建玉 / PnL / DD / Greek
- log schema: 統一 JSONL
- dashboard SLO: Atlas / Chronos / 統合
- SRE Golden Signals

### 22. kill-switch + 24h 監視フェイルオーバー（o3）
- 手動停止経路（既 `common_v3/risk/kill_switch.py`）
- escalation: ゆうさくさん Pushover → Auditor / Navigator
- 復帰プロトコル（approver 検証 + audit log）
- **Chronos 24h 体制**: 深夜-早朝の kill-switch 押下責務（自動 escalation + 多経路通知）

### 23. Atlas / Chronos / Sora Lab 境界責務
- Atlas: 米オプション / 平日 EDT / 自己資金
- Chronos: 米先物 / 24h / prop firm
- Sora Lab: 全組織 / 金融以外包含 / ゆうさくさん目標達成

**ゆうさくさん判断要件**: 将来 Atlas/Chronos に追加する市場・商品の判定基準

### 24. リスク管理モデル
- Position sizing（Kelly / 固定 % / VIX 連動）
- ファットテール対策（VaR でなく ES / CVaR）
- ブラックスワンシナリオ（flash crash / data outage / order-reject storm）
- Redteam 毎ビルド再実行 CI 統合

### 25. 最終出口戦略（o3 新規）
- Sora Lab 全体の終了条件（目標達成 or 長期失敗）
- 選択肢: 売却 / クローズ / OSS 化 / 継続運用
- OpenTimestamps 1st mover 刻印（既実施）との整合

**ゆうさくさん判断要件**: 出口シナリオと条件（「月 300 万持続 3 年で」等）

### 26. 物理時間考慮（Gemini/o3 新規）
- KYC 承認待ち（prop firm 平均 1-2 週・法人案件はさらに延長）
- 送金ラグ（国際送金 2-5 営業日）
- API キー発行待ち
- データライセンス契約（OPRA 等）の締結時間
- Phase 計画に物理時間バッファを含める

### 27. OSS 依存管理（o3 新規）
- prop firm API wrapper OSS の作者削除リスク
- ミラーフォーク戦略（critical OSS は fork + internal mirror）
- ライセンス継承プラン
- dependency audit CI

---

## 2 ルート合流プロトコル（重み + 客観測定方法）

### 比較軸と重み（客観測定ルール明記）

| 軸 | 重み | 測定方法（定量） |
|---|---|---|
| Sora Lab 独自性 | 30% | Top-down ルートのみで抽出され Bottom-up になかった項目数 / 候補総数 |
| 再現性 | 25% | L1-L3 検証で合格した項目数 / 候補総数 |
| リスク耐性 | 20% | #2 負けパターンでブロックされない確率（逆数）|
| 実装速度 | 15% | L0-L3 通過までの推定工数（小さいほど高評価）|
| ゆうさくさん工数負荷 | 10% | 月 3-5 件（Phase 0 は週 5-10 件）想定内に収まるか |

**Auditor 判定基準**: 上記 5 軸を Gemini/o3 で独立採点 → 平均スコア算出

### 採用ルール（月 3-5 件枠保護）

| 状況 | 判定 |
|---|---|
| 両ルートで重み top 3 | **自動採用**（ゆうさくさん承認不要）|
| 片方のみ top 3 | **Auditor 事前判断 → ゆうさくさん週次まとめで最終** |
| 両ルートで低位 | **自動破棄** |
| 片方で top 1 + 独自性 > 50% | **Sora Lab 独自価値として採用**（ゆうさくさん通知のみ）|

---

## マイクロマイルストーン方式（rollback 粒度明記）

- Phase 0-B1 / 0-B2 の調査結果は **5 項目ごとに切って Gemini + o3 クロスチェック**
- **rollback 粒度**: 5 項目中 1 件 NG → **該当項目のみ個別戻し**（全件戻しは過剰）
- 不合格時の処置: 再調査 / アイスボックス保管（o3 推奨・将来市場変化時に再利用）/ 完全破棄 から選択
- 検証 prompt は `scripts/concept_verify_20260423.py` 流用
- 結果は `data/governance/phase0b*_verify_*.md` 保存

---

## gating フロー（Navigator/Auditor 介入点明記）

```
Phase 0-A (本 draft v2)
  ↓ ゆうさくさん承認
Phase 0-A 確定（27 項目 Level 分け）
  ↓
L1 項目で gating 判定（法規制/負け源/時間軸/データライセンス）
  ← Auditor 事前レビュー
  ↓ NO-GO 出たら Phase 0-B 中止・再設計
Phase 0-B1（Bottom-up）+ Phase 0-B2（Top-down）並行
  ← Navigator 並走監視（進捗レポート毎日）
  ↓ 5 項目ごと Gemini+o3 検証（マイクロマイルストーン + 虚偽完了メタ化防止）
  ← Auditor 事前フィルタ
Phase 0-C 比較検証（合流プロトコル適用）
  ← 3 者独立再検証
  ↓
Phase 0-D シンセシス + ADR-013 + 3 者最終検証 + ゆうさくさん最終承認
```

---

## ゆうさくさん判断要件リスト（draft v2 承認時に確認）

| # | 項目 | 判断必要内容 |
|---|---|---|
| 1 | 法的実体 | LLC 設立タイミング / 妻役員化 / prop 名義 / 月額予算上限 |
| 3 | 撤退条件 | 月次 DD 上限 / 連続赤字閾値 / 2026-10 月 60 万未達分岐 |
| 5 | KPI 閾値 | Atlas/Chronos 各々の勝率/PF/DD/Sharpe/月利の最低ライン |
| 13 | ガバナンス境界 | 各ロールの決裁権境界（金額閾値 / 技術判断範囲） |
| 18 | リソース配分 | 金融 vs 非金融（SNS/音楽）の月次リソース配分比率 |
| 20 | Treasury | Atlas → Chronos 資金移動トリガー（複利額閾値） |
| 23 | 境界責務 | 将来 Atlas/Chronos に追加する市場・商品の判定基準 |
| 25 | 出口戦略 | Sora Lab 終了条件（月 300 万持続 3 年で完了等）|

---

## 次アクション

1. 本 draft v2 を ゆうさくさん承認
2. 承認後 Phase 0-B1（Bottom-up）+ 0-B2（Top-down）着手
3. 5 項目ごとマイクロマイルストーン検証
4. Phase 0-C 比較検証 → Phase 0-D シンセシス → ADR-013

---

## ゆうさくさん判断確定（2026-04-23）

| # | 判断項目 | 確定 | 補足 |
|---|---|---|---|
| 1 | LLC 設立タイミング | **B**: ペーパー 1 日稼働確認後（2026-05 頃）| - |
| 2 | 2026-10 未達分岐 | **B**: 4 層加速（元本増 or 戦略追加）| **別収益構造を戦略から考える可能性あり**（戦略 B/C/D 新規検討余地を残す）|
| 3 | KPI 閾値 | **推奨採用**（Atlas 月利 v6 保守以上 / Sharpe 1.5+ / 最大 DD 20%）| - |
| 4 | 月額予算 | **B**: 月 5,000 円程度（o3 回数増 + OPRA 最小契約検討）| - |
| 5 | draft v2 本体 | **問題なし** | 27 項目 + 2 ルート合流 + マイクロマイルストーン OK |

### 後続判断（Phase 0 進行中に順次確認）
- #13 ガバナンス境界 / #18 リソース配分 / #20 Treasury / #23 境界責務 / #25 出口戦略
