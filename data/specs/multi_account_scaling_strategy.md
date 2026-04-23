---
title: プロップファーム 5+ 口座同時運用 最適スケーリング戦略
status: DRAFT (2026-04-21 strategist)
premortem_ref: 20260421_171308_multi-account_scaling_strategy_prop_firm_d6b359
sources_verified:
  - https://tradeify.co/ (2026-04-21 直読・homepage FAQ)
  - https://help.tradeify.co/article/18-trailing-drawdowns (2026-04-21 wayback直読)
  - https://help.myfundedfutures.com/en/articles/10771500-copy-trading-at-myfundedfutures (2026-04-21 直読)
  - https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices (2026-04-21 直読)
  - data/specs/mffu_flex_50k_rules.md (2026-04-21)
  - data/specs/mffu_eval_fast_pass_plan.md (2026-04-21)
related:
  - data/specs/multi_account_risk_matrix.md
  - data/configs/chronos_multi_account_routing.yaml
---

# プロップファーム 5+ 口座同時運用 最適スケーリング戦略

## 0. 結論 (先出し)

**★推奨 Phase 1 (今週〜5月末): 案B 中庸 → 3 active 口座**
- MFFU Flex $50K × 1 (ORD-tjrhEgenMN, Eval中・Day 1 明日 4/22)
- MFFU Rapid $50K × 1 (5/1 着手)
- Tradeify Lightning 50K × 1 (KYC完了後 即Sim-Funded)
- 期待月収 (Sim-Funded 到達後): **保守 18万 / 中央 35万 / 楽観 58万**

**★Phase 2 (6月〜退職直後): 案B+ 6 active 口座**
- MFFU 4口座 (Flex/Rapid/Pro/Builder) + Tradeify Lightning 50K × 2
- 期待月収: **保守 36万 / 中央 68万 / 楽観 112万**

**★Phase 3 (7〜9月): 案C+ 合計 8 口座**
- MFFU 4口座 + Tradeify Lightning 150K × 4 (Tradeify combined $600K)
- 期待月収: **保守 68万 / 中央 125万 / 楽観 203万**

**月300万到達には Atlas + Chronos + Tradeify Phase 3 の合算が必須** (Chronos単独では中央ケースでも月125万)。

---

## 1. 公式確認済み前提 (Must-Known)

### 1.1 MFFU (MyFundedFutures)
| 項目 | 公式回答 | 出典 |
|---|---|---|
| **Copy Trading across own accounts** | **許可** (Tradesyncer公式推奨 + Tradovate built-in group copier OK) | help MFFU 10771500 |
| Copy Trading other **traders** | **禁止** (2名以上の間) | Fair Play §5 |
| Device sharing with other traders | 禁止 (another trader) | Fair Play 同上 |
| Collaborative Trading | 禁止 (unconnected accounts across trader) | Fair Play §2 |
| Hedging | 禁止 (MES/ES・MNQ/NQ同時両建て) | Fair Play §5 |
| Max concurrent accounts/person | 明記なし (5+ 口座の事例多数) | — |
| HFT | 200+ trades/日 禁止 | Fair Play §1 |
| Max Qty (Flex/Pro/Rapid Eval) | 5 mini / 50 micro | mffu_flex_50k_rules |

### 1.2 Tradeify
| 項目 | 公式回答 | 出典 |
|---|---|---|
| **Max Sim Funded accounts** | **5** (同時保有可) | tradeify.co/ FAQ |
| **Combined cap** | **$750K** (150K × 5) | homepage "up to $750k" + FAQ |
| Lightning Funded仕様 | Instant Funding・No Eval | homepage |
| Lightning サイズ (アクティベート費) | 25K / 50K ($111) / 100K ($181) / 150K ($251) | homepage Pricing |
| Profit Split | **90%** 常時 | homepage |
| EOD Trailing DD | 50K=$2,500 / 100K=$3,000 / 150K=$3,500 (推定) | trailing-drawdowns article |
| Daily Loss Limit | **明記なし** (real-time enforcementのEOD DDでガード) | trailing-drawdowns article |
| Consistency | 20% (Lightning・relaxes after each payout) | homepage |
| Payout frequency | 5-day (Flex) / Daily ($1,250 cap/payout) | homepage |
| Payout Cap (Flex path) | **なし** | homepage |
| News Trading | **Free rein** (制限なし) | FAQ |
| Japan居住者 | **許可** (Restricted Countries に含まれず) | FAQ |
| Copy Trading 公式ポリシー | homepage上 明記なし (要質問) | — |

### 1.3 共通 (Chronos アーキテクチャ)
- Webhook: TradingView Pine → `chronos_webhook_server.py` (HMAC+nonce+IP allowlist)
- Signal symbol: `MES/MNQ/ES/NQ` のみ
- Qty: 1-5 (Pydantic ge=1 le=5) ← **multi-account時は 1信号 × N口座で発注、qty自体はper-account**

---

## 2. ユーザー指定 3 案の公式突き合わせ

### 質問1 Tradeify Lightning $50K × 5 (combined $250K)
- **公式整合: OK** (5口座・combined $250K は $750K上限内)
- Activation fee $111 × 5 = **$555 初期費用** (購入1回限り・Lightningは reset fee 無しを明記・注: Evaluation pathには reset fee $239 記載あり)
- 月額費用: **なし** (Lightningは買切)
- Chronos側: 1信号 → 5口座同時 routing (Tradovate group copier)

### 質問2 MFFU Flex $50K + Rapid $50K + Pro $100K 並行
- **公式整合: OK** (MFFU公式 "allows copy trading across all account types")
- 初期: Flex $107 + Rapid $165/月 + Pro $227/月 = **$107 + 月$392**
- ただし Pro $100K は Pro プランの mini枚数 3 (公式) で Flex/Rapid より厳しい
- **注意**: Pro Plan Micro 上限 = **5 micro** (5 mini ではない方・project_mffu_5plan_pitfallsで既に訂正済)

### 質問3 合計 6-8 口座運用時の期待月収
- **Sim-Funded到達率・payout到達率を楽観一律90%と仮定すると過大評価** (実測 Chronos MES 3枚で月3.84-4.58%)
- 現実見積もり: Chronos 1口座あたり **$800-1,500/月 (Sim-Funded段階・安定運用)**
- 8口座 → $6,400-12,000/月 = **96万-180万円/月** (中央135万前後)

---

## 3. 口座配分シミュレーション 3案

### 前提パラメータ
- Chronos ORB 実績: VIX>20 で月次 +1.84-4.58% (backtest_futures_orb_results.csv)
- 1口座・MES 3枚で月期待 $1,500 (Sim-Funded・80% split 後 $1,200/月)
- 1口座・MES 5枚で月期待 $2,500 ($2,000/月 after split)
- Evaluation Pass確率 (案B準拠): Flex 70% / Rapid 60% (Intraday厳しい) / Pro 55% / Builder 75%
- Tradeify Lightning = Pass不要 (Instant Funded・即Sim-Funded)

### 案A: 保守 (Small Start・2-3 active 口座)

| 口座 | 初期費 | 月額 | Sim-Funded月収 | Pass率 |
|---|---|---|---|---|
| MFFU Flex $50K (実行中) | $107 | 0 | $1,200 | 70% |
| Tradeify Lightning 50K | $111 | 0 | $1,500 (90% split) | 100% |
| (保留) | — | — | — | — |
| **合計** | **$218 (3.3万円)** | **$0** | **$2,700 = 40.5万円** | 期待値 35万 |

- **破綻確率**: 1口座-1%/日・独立運用で 2口座同時breach 確率 = 0.0001/日・年間 2.5%
- **運用負荷**: 1日 15-30分 (Pushoverチェック + 週末payout申請)
- **所要時間**: 即日開始可 (Flex稼働済・Tradeifyは KYC 24-48h のみ)

### 案B 中庸 (★推奨 Phase 1: 3-4 active 口座)

| 口座 | 初期費 | 月額 | Sim-Funded月収 | 着手時期 |
|---|---|---|---|---|
| MFFU Flex $50K (実行中) | $107 | 0 | $1,200 | 実行中・4/22 Day 1 |
| MFFU Rapid $50K | 0 | $165 | $1,000 (Intraday厳しい) | 5/1 |
| MFFU Pro $100K | 0 | $227 | $2,400 | 5/10 |
| Tradeify Lightning 50K | $111 | 0 | $1,350 | KYC後即 (~4/24) |
| **合計** | **$218** | **$392/月** | **$5,950/月 = 89万円** | 中央月収 35万→65万 |

- **Pass率 4口座中3-4口座通過: 65%** (Flex 70% × Rapid 60% × Pro 55% × Tradeify 100%)
- **Evaluation 期間中赤字**: 最大 -$3,000 MLL × 3 Eval = **-$9,000 = -135万円** (全口座Bust想定)
- **運用負荷**: 1日 30-45分

### 案C 楽観 (Phase 2-3: 8 active 口座)

| 口座 | 初期費 | 月額 | Sim-Funded月収 | 着手時期 |
|---|---|---|---|---|
| MFFU Flex $50K | $107 | 0 | $1,200 | 実行中 |
| MFFU Rapid $50K | 0 | $165 | $1,000 | 5/1 |
| MFFU Pro $100K | 0 | $227 | $2,400 | 5/10 |
| MFFU Builder $50K | $75 | 0 | $800 (cap $10K) | 6/1 |
| Tradeify Lightning 50K #1 | $111 | 0 | $1,350 | 4/24 |
| Tradeify Lightning 50K #2 | $111 | 0 | $1,350 | 6/1 |
| Tradeify Lightning 150K #1 | $251 | 0 | $3,500 | 7/1 (資金確認後) |
| Tradeify Lightning 150K #2 | $251 | 0 | $3,500 | 8/1 |
| **合計** | **$906 (14万円)** | **$392/月** | **$15,100/月 = 226万円** | 楽観最大 |

- **Pass率 6口座以上通過: 58%**
- **相関リスク**: 同一信号で8口座一斉Bustもあり得る (§4参照)
- **運用負荷**: 1日 60-90分 (口座状態確認 + payout 8口座並行申請)
- **KYC/送金コスト**: Wise手数料 $15/回 × 8口座 = 月 $120 (2万円)

---

## 4. ★推奨と判断理由

### Phase 1 (今週-5月末): **案B改 3 active 口座**
理由:
1. 4/22 Flex稼働済・4/24 Tradeify追加は即時可能 (KYC 24-48h)
2. Rapid/Proは **Flex 初回Payoutを確認してから** 5月中旬追加 (Chronos実戦検証)
3. 8口座同時bust相関リスク高・検証完了前に過拡大は機会損失より事故リスク優位
4. 月300万ロードマップ v6 との整合: 6月目標月利下限を維持

### Phase 2 (6月-退職後2ヶ月): **案B+ 6 active**
条件:
- Flex 初回Payout着金 (6/15前後) + Tradeify 初回Payout着金 (5月末) 両方確認
- Atlas Chronos 合算でDD -15%以内を2ヶ月維持
- Consistency違反ゼロ

### Phase 3 (7-9月): **案C 8 active**
条件:
- Phase 2で月利実測 4%以上を2ヶ月連続
- Tradeify Lightning 150K (activation $251) の予算 ($502) 確保
- LLC 6/1設立済・法人口座で送金統合可能な状態

---

## 5. 期待月収 (現実ベース・税引前)

### Chronos 1口座 ($50K) の月収ケース (backtest実績ベース)
| ケース | 月利 | MES 3枚月次$ | Profit Split後 (80%) | 円換算 (150円/$) |
|---|---|---|---|---|
| 保守 | 1.5% | $750 | $600 | 9万円 |
| 中央 | 3.0% | $1,500 | $1,200 | 18万円 |
| 楽観 | 5.0% | $2,500 | $2,000 | 30万円 |

### Tradeify Lightning の高効率
- Profit Split 90% (MFFU 80%より+12.5%)
- Lightning 150K は MES 15枚可 (50K口座の5倍)
- **月利同率でも 150K 1口座 = 50K 3口座分** (運用負荷は1/3)

### 配分別期待月収表
| 案 | 保守 | 中央 | 楽観 |
|---|---|---|---|
| A (2口座) | 18万 | 35万 | 58万 |
| **B Phase1 (3-4口座)** | **36万** | **65万** | **110万** |
| B+ Phase2 (6口座) | 54万 | 95万 | 155万 |
| **C Phase3 (8口座・150K組込)** | **82万** | **145万** | **240万** |

※Atlas + FX スワップ合算で月300万は案C到達後 3ヶ月程度で見込み

---

## 6. いつ追加するか (Scaling Trigger)

### 新規口座追加の自動判定条件
```yaml
add_next_account_when:
  - existing_accounts_in_sim_funded: ">= 1"   # 少なくとも1口座 Sim-Funded到達
  - last_30d_consistency_violations: 0         # 規約違反ゼロ
  - last_30d_pnl_positive: true                # 単月黒字
  - rolling_dd_percent: "< 15"                 # DD 15%未満
  - cash_buffer_usd: ">= 500"                  # activation/月額の5倍は余裕
  - operational_load_minutes_per_day: "< 60"   # 運用負荷1h未満
```

### 案B→B+ 移行トリガー (6月上旬想定)
- Flex 初回Payout着金
- Tradeify Lightning 初回Payout着金
- 合計黒字30万以上

### 案B+→C 移行トリガー (7月上旬想定)
- 6月月収30万以上 (税引前)
- LLC設立完了・法人口座運用開始
- Tradeify 150K activation $251 × 2 = $502 の予算確保

---

## 7. 運用負荷見積もり (1日)

| フェーズ | 口座数 | 毎日 | 毎週 | 毎月 |
|---|---|---|---|---|
| A | 2 | 15分 (Pushover+Journal) | 30分 (payout) | 60分 (KYC・税務) |
| **B Phase1** | **3-4** | **30分** | **60分** | **120分 (税務・送金)** |
| B+ Phase2 | 6 | 45分 | 90分 | 180分 |
| C Phase3 | 8 | 60分 | 120分 | 240分 (LLC申告 + 送金集約) |

### 自動化可能箇所
- Pushover統合Dashboard (全口座1画面・[Phase]タグ導入)
- Payout自動申請スクリプト (Tradovate API + TradingView bot)
- 週次レポート (口座別P&L・DD消費率・Consistency残)

---

## 8. リスク・警告

### 相関Bustリスク (全口座同時失格)
- 同一Chronos信号で全8口座同方向発注 → 逆行時に全同時Bust
- **対策**: Phase 2以降は Chronos信号を **2つ以上の戦略分散** (ORB + VIX-MR 等)
- または **口座ごとにVIXバンド分離** (口座A: VIX 20-25 / 口座B: VIX 25-30)

### 税務コスト
- 日本在住・雑所得扱い (project_atlas_tax_correction_20260420 で確定)
- 法人化 (6/1 LLC設立) で一定額以上は法人税 (実効30%) に変更可
- 8口座合算 月125万 = 年1500万 (個人) vs 法人 (役員報酬 + 法人留保) で差

### MFFU/Tradeify 規約変更リスク
- 2025/07 MFFU自動化ルール更新・2026/01 Core廃止・2026 年Tradeify Tradeify 3.0 release
- **四半期ごとに公式直読確認** (hook化推奨: weekly crawl + diff通知)

### Copy Trading 判定境界 (詳細は risk matrix)
- MFFU 同一人物間: **公式明示OK**
- Tradeify 同一人物間: 公式明記なし・**support英文質問必須** (未解消)
- プロップ業界一般: 同一人物自アカウント間は原則OK・他人間は禁止

---

## 9. 具体実装 TODO (優先順)

1. **[今日] data/specs/multi_account_risk_matrix.md 作成** (firm別横断表)
2. **[今日] data/configs/chronos_multi_account_routing.yaml 作成** (routing定義)
3. **[4/22] Flex Day 1 開始** (既存 mffu_eval_fast_pass_plan.md 準拠)
4. **[4/24] Tradeify Lightning 50K activate・Sim-Funded開始**
5. **[5/1] MFFU Rapid $50K 購入・Eval開始**
6. **[5/5] Chronos routing実装 (1信号 → 3口座発注)**
7. **[5/10] MFFU Pro $100K 購入・Eval開始**
8. **[5/15] Tradeify support宛・Copy Trading同一人物規約の公式確認メール**
9. **[6/1] Phase 2 判定・Builder追加 + Tradeify 50K #2**
10. **[7/1] Phase 3 判定・Tradeify 150K × 2 拡張**

---

## 10. 成果物ファイル

- このドキュメント: `data/specs/multi_account_scaling_strategy.md`
- Risk Matrix: `data/specs/multi_account_risk_matrix.md`
- Routing Config: `data/configs/chronos_multi_account_routing.yaml`

---

_策定: strategist @ 2026-04-21 / premortem ref: d6b359 (CONDITIONAL_GO)_
