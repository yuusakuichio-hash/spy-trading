# MFFU Flex $50K 正式仕様 (2026-04-21 直接確認版)

**出典** (公式直読・2026-04-19/21 再確認):
- https://myfundedfutures.com/blog/myfundedfutures-mffu-flex-plan
- https://help.myfundedfutures.com/en/articles/8230009-news-trading-policy
- https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices
- https://help.myfundedfutures.com/en/articles/10771500-copy-trading-at-myfundedfutures

**購入状況** (2026-04-20):
- ORD-tjrhEgenMN / $127/month (Flex $50K・Tradovate/TradingView) / Pending中

---

## 1. Evaluation 段階 確定ルール

| 項目 | 値 | 備考 |
|---|---|---|
| Starting Balance | $50,000 | |
| **Profit Target** | **$3,000 (6%)** | 10%ではない・公式ブログ明記 |
| Max Loss Limit (MLL) | **$2,000 (4%)** | EOD Trailing (Intradayではない) |
| Daily Loss Limit | **なし** | Flex は DLL 無し |
| **DD方式** | **EOD Trailing** | 引け後のHWMからトレール・含み益失っても即失格にならない |
| Min Trading Days | **2 日** | 1日合格不可 (Builderのみ1日Pass可) |
| **Consistency Rule** | **50%** | 単日利益 ≤ 累計利益の50%・**Eval のみ適用** |
| Max Mini Contracts | 5 Mini (ES/NQ/RTY) | |
| Max Micro Contracts | **50 Micro** (MES/MNQ/M2K) | Pro (5枚) と異なる |
| **News Trading** | **許可** | Eval段階は全プラン許可 |
| **Overnight Hold** | **許可** | Builderのみ禁止 |
| **Weekend Hold** | **不可** | 金曜クローズ必須 |
| 料金 | $107 1回払い (定価) | Promo時 30-40% OFF あり |
| Activation Fee | $0 | |

### EOD Trailing の挙動 (重要)
- 日中の含み益は MLL を動かさない
- 引け時点の残高が HWM を更新する場合のみ MLL が上がる
- 翌営業日の朝に新MLLが反映
- **例**: 残高 $51,200 で引け → MLL = $51,200 - $2,000 = $49,200
- 翌日に残高 $52,000 で引け → MLL = $52,000 - $2,000 = $50,000

### Consistency Rule 50% の挙動
- 累計利益が $3,000 到達時点で単日最大利益/累計 が 50% 超だと Eval通過判定が却下
- 例: 1日目 $1,800 / 2日目 $1,200 → 合計 $3,000、最大日 1,800/3,000 = 60% → **違反**
- 対策: **毎日ほぼ均等にPnLを積む** / 大勝ち日があれば他の日で追いつく
- **1日で$3,000達成 → Eval通過不可** (Consistency自動違反)

---

## 2. Sim-Funded 段階 (Eval通過後)

| 項目 | 値 | 備考 |
|---|---|---|
| Initial Balance | $0 | -MLL までマイナス許容 |
| MLL (初回Payout前) | $2,000 | EOD Trailing 継続 |
| **MLL (初回Payout後)** | **$100 (static)** | Survival Mode |
| Daily Loss Limit | なし | |
| Consistency (Sim-Funded) | なし | Evalのみ |
| **T1 News Trading** | **許可** (Flex Sim-Funded Unrestricted) | Rapid/Pro は禁止 |
| Overnight Hold | 許可 | |
| Inactivity Rule | 7日未取引で口座閉鎖 | |
| Contract Table | 利益 $0〜: 2mini/20micro | 段階的スケーリング |
| Contract Table | 利益 $1,500〜: 3mini/30micro | |
| Contract Table | 利益 $2,000〜: 5mini/50micro | |

---

## 3. Payout (初回Payout条件)

| 項目 | 値 |
|---|---|
| Min Winning Days | 5 日 |
| Min Daily Profit | $150 (勝利日の定義) |
| Min Net Profit | $500 |
| Min Withdrawal | $250 |
| Max Withdrawal | 利益の 50% / 1cycle Cap $5,000 |
| Payout Cycle | 5勝利日 ＋ $500 純益 達成ごと |
| Profit Split | **80 / 20** (トレーダー/MFFU) |

### Path to Live (Live口座移行)
- 5回連続 Payout 承認、または
- 累計Sim-Funded資本 $10,000 到達

---

## 4. 自動化ルール (2025/07 公式更新)

**出典**: Fair Play Prohibited Practices

### 許可
- Semi-automated (EA・カスタムスクリプト)・人間監督前提
- **Fully automated (実市場戦略限定)**
- 対応プラットフォーム: Tradovate / Quantower / Rithmic / Sierra / ATAS / Jigsaw 等
- **Copy Trading across own accounts** (同一人物の自口座間・公式明記)

### 禁止
- **HFT**: 200+ trades/日 (Chronos 1日5-20 trades で問題なし)
- **AI-driven完全無人** (Chronos はルールベースで機械学習不使用・該当しない)
- **Hedging** (同銘柄同時両建て・MNQ/NQは同一underlying扱いで両建て禁止)
- **Slippage/Bracket/Gap exploit**
- **Cross-trader Copy Trading** (他人との共有禁止)
- **Device Sharing with other traders** (別人との共用禁止・自身のみはOK)

### Chronos のルール適合 (2026-04-20 Oliver回答整合)
- HFT: 該当しない (環境判定 + ORB なので1日数回)
- AI: ルールベースで非該当
- Hedging: 単一銘柄単一方向のみ
- isAutomated flag: Tradovate側実装・評価段階では問題報告なし

---

## 5. ニュース規制 (T1 Events)

### T1イベント定義
- FOMC会合
- FOMC議事録
- 雇用統計 (NFP)
- **CPI** (2026-04-10 既発表/2026-05予定)

### 時間窓
- イベント時刻の **前2分〜後2分** は全ポジションFlat必須
- 例: 8:30 ET CPI → 8:28:00までに全クローズ → 8:32:00以降再エントリー可

### Flex 50K の適用
- Eval: 許可 (ただし前後2分は回避推奨)
- **Sim-Funded: 許可 (Unrestricted)** ← Flex 50Kの強み

---

## 6. 週末・祝日ルール

- **金曜 16:00 ET クローズまでに全ポジション決済必須**
- 週末持ち越し禁止 (全プラン共通)
- 祝日前半日取引日も同様

---

## 7. 失敗時ポリシー

### Eval失敗 (MLL違反)
- 料金没収 ($107)
- 即再挑戦可 (新規購入)
- Reset機能: 公式で明確な言及なし・実質「新規購入でReset」
- Post-breach Cooldown: Flexは特に明記なし (Builder Live breachのみ21日)

### 再挑戦コスト (Flex)
| 試行回数 | 累計コスト | 円換算 (150円/$) |
|---|---|---|
| 1発合格 | $107 | 16,050円 |
| 2回挑戦 | $214 | 32,100円 |
| 3回挑戦 | $321 | 48,150円 |

**Promo時**: Flex は定期的に30-40% OFFクーポン配布 → 実コストは70-90ドル程度

---

## 8. 未確定事項 (公式未明記・要サポート確認)

| # | 項目 | 現状判断 | 確認優先度 |
|---|---|---|---|
| 1 | Reset Fee の有無 | 公式言及なし・新規購入扱いと推定 | 低 |
| 2 | Weekly Profit Target | **なし** (公式明記なし) | 確定 |
| 3 | Min Win Rate 要件 | **なし** (公式明記なし) | 確定 |
| 4 | Micro/Mini 混在発注 | 両建てでなければ可と推定 | 中 |
| 5 | isAutomated flag の MFFU側扱い | 不明・実測必要 | 低 (非重要) |
| 6 | Max Active Accounts | 公式明記なし・複数可と推定 | 低 |
| 7 | Evaluation 有効期限 | 公式明記なし (30日制限なし) | 低 |

---

## 9. プレミアム情報 (公式以外・要警戒)

- Trustpilot評価: **4.9/5 (17K reviews)** — Payout実績高い
- 日本語サポート: なし (英語のみ)
- 日本居住者: Restricted Country に日本含まず
- KYC: パスポート + 住所証明 + 銀行口座証明
- 送金: RiseWorks経由 / Wise受取推奨 / 手数料$15/回

---

## 10. このルールから導ける戦術的含意

1. **1日で$3,000狙い禁止** (Consistency違反確定) → 最低2日に分散
2. **EOD Trailing なので含み益を持ち越せる** → 引け間近に含み益を決済する必要なし
3. **Overnight許可** → 翌日ギャップを狙える (Builder との差別化)
4. **News許可 (Flex) なので CPI/FOMC も稼働可** → ただしvolatility高い時は回避推奨
5. **50 Micro上限** → MES 5-10枚で十分・本番スケールには余裕
6. **Consistency 50%逆算** → 目標$3,000なら単日最大$1,500以下に抑える

---

**以上、MFFU Flex $50K Eval 確定仕様。次ファイル `mffu_eval_fast_pass_plan.md` で具体戦術化。**
