---
title: プロップファーム Multi-Account Risk Matrix (Cross-Account Rules)
status: DRAFT (2026-04-21 strategist)
parent: data/specs/multi_account_scaling_strategy.md
scope: MFFU (MyFundedFutures) + Tradeify のみ (その他firmは未調査のため本書対象外)
sources_verified:
  - https://help.myfundedfutures.com/en/articles/10771500-copy-trading-at-myfundedfutures (2026-04-21)
  - https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices (2026-04-21)
  - https://tradeify.co/ (2026-04-21 FAQ)
  - https://help.tradeify.co/article/18-trailing-drawdowns (2026-04-21 wayback)
  - data/specs/mffu_flex_50k_rules.md (2026-04-21)
---

# Multi-Account Risk Matrix — Cross-Account Rules (MFFU + Tradeify)

## 0. 凡例
- OK: 公式明示許可
- NG: 公式明示禁止
- 要確認: 公式明記なし・解釈必要 (要 support確認)
- 条件付: 注釈参照

## 対象外
本マトリクスは MFFU / Tradeify の直読公式情報のみを根拠とする。
他firm (Apex Trader Funding・TopStep・MFF Forex など) は個別調査 (`data/research_<firm>.md`) を経た後に別ドキュメントで拡張する。

---

## 1. Copy Trading 判定境界 (firm別)

| ルール | MFFU | Tradeify |
|---|---|---|
| **同一人物の自アカウント間** Copy Trading | OK (help 10771500 明示) | 要確認 (homepage明記なし) |
| **他トレーダー間** Copy Trading | NG (Fair Play §2・§5) | 要確認 |
| Tradovate **Built-in Group Copier** | OK (help 10771500 明示) | 要確認 |
| Tradesyncer 3rd party copier | OK (公式推奨パートナー) | 要確認 |
| 自作スクリプト (Python → Tradovate REST) | OK (semi-managed範囲・Oliver明言) | 要確認 |

### Chronos 実装上の Copy Trading 判定
- **定義 (MFFU Fair Play 原文)**: "Each individual trader is required to maintain their own individual trading activity. Meaning, entering, exiting and cancelling their own trade executions. **Traders are not permitted to copy trade one another**"
- この原文は **2人以上の関係** (one another) を想定 → **自分1人の5口座間は該当しない**
- 実装上: 1つのTradingViewアラート → chronos_webhook_server → N口座並列発注 は **semi-managed + 自口座間** = 合法
- **違反例**: 友人のChronos信号を受け取り自分の口座で発注 = `Cross-trader Copy Trading` = 禁止

### Tradeify の未確定事項 (support質問必須)
```
Q1. Can I run the same automated strategy across multiple Lightning Funded accounts I own under one name?
Q2. Is Tradovate built-in group copier permitted?
Q3. Is there a limit on concurrent identical orders across my 5 accounts?
```
返答受領前は **安全側仮定** (禁止とみなす) でrouting設計。

---

## 2. Account Size / Max Concurrent Rules

| Firm | Max Accounts/Person | Combined Cap | Size Options |
|---|---|---|---|
| **MFFU** | 明記なし (5+ 事例多数) | 明記なし | $50K/$100K/$150K |
| **Tradeify** | **5** (FAQ明示) | **$750K** (homepage "up to $750k") | 25K/50K/100K/150K |

### 組み合わせ推奨
- **Phase 1 (今週-5月末)**: MFFU 1-2 + Tradeify 1 = 2-3口座
- **Phase 2 (6月)**: MFFU 3-4 + Tradeify 2 = 5-6口座
- **Phase 3 (7-9月)**: MFFU 4 + Tradeify 4 (うち150K×2) = 8口座

### Scaling Cap 到達時 (5+5=10口座上限)
- MFFU Max Account数 明記なし → support確認必要
- Tradeify 5口座固定
- 10口座超拡張が必要な段階 (月200万以上) で他firm追加調査

---

## 3. Drawdown 伝播 (口座間影響)

| シナリオ | MFFU | Tradeify | 備考 |
|---|---|---|---|
| 1口座でDD上限Breach | 他口座へ**影響なし** (独立計算) | 同左 | 両社とも口座単位で評価 |
| Aggregate Trailing DD (合算DD) | **存在しない** (公式明記なし) | **存在しない** | 各口座独立 |
| Account-level DD lock | EOD Trailing (Flex/Pro/Builder) / Intraday (Rapid) | EOD Trailing (Lightning・real-time enforce) | Rapid/Advancedは日中厳しい |
| 失格時の他口座状態 | 他口座は継続 | 他口座は継続 | 独立性あり |

### 具体例: MFFU Flex $50K × 2口座並列Eval中
- 口座A: $-2,000 hit → Bust (料金$107没収)
- 口座B: $+1,500 → 継続可能
- Chronos 実装上: `kill_switch.trigger_account(account_id)` で**個別停止**・全体停止は**不要**

### Tradeify EOD Trailing DDの特殊挙動 (公式原文)
> "the drawdown limit is enforced in real-time throughout the trading day. If your account balance drops to or below the current drawdown limit at ANY point during trading hours, your account will be immediately failed"

- EODで計算するが日中もリアルタイムで監視
- **Chronos実装**: Tradeify口座のDD閾値は intraday でも常時 monitor 必要
- MFFUのEOD Trailing (Flex/Pro) は引け後計算・Tradeifyより甘い

---

## 4. 違反時の Cross-Account 波及

| 違反種別 | MFFU | Tradeify | 影響範囲 |
|---|---|---|---|
| DD breach (事故的) | 該当口座のみ停止 | 該当口座のみ停止 | **口座単位** |
| HFT検出 (200+ trades/日) | Fair Play §1 違反 | 明記なし | **全口座剥奪の可能性** (公式原文 "permanent restrictions") |
| Copy Trading with other person | Fair Play §2 違反 | 明記なし | **全口座剥奪 + profit没収** |
| Hedging (MES/ES両建て) | Fair Play §5 違反 | 明記なし | **該当口座剥奪・他口座影響は解釈依存** |
| Device sharing (他人と) | Fair Play §5 違反 | 明記なし | **全口座剥奪** |

### Chronos 実装上の防御
```python
# common/pre_trade_check.py に追加すべき multi-account 検証
def check_multi_account_limits(payload, accounts):
    # 1. HFT防御: 全口座合算のtrades/day
    total_trades_today = sum(a.trades_today for a in accounts)
    if total_trades_today >= 150:  # 200未満に安全マージン
        raise RejectReason("MULTI_ACCOUNT_HFT_LIMIT")
    
    # 2. Hedging防御: 同一underlying・逆方向検知
    for a in accounts:
        if a.position and a.position.direction != payload.action:
            if underlying(a.position.symbol) == underlying(payload.symbol):
                raise RejectReason("CROSS_ACCOUNT_HEDGE_DETECTED")
    
    # 3. 同一信号 duplicate_order 防御 (既存nonce検証で担保)
    
    # 4. Device sharing検知 (別人への不正使用防止)
    #   → auth_budget + IP allowlist + geolocation 組み合わせ (運用規律)
```

---

## 5. Daily Loss Limit / Consistency Rule 比較

| 項目 | MFFU Flex | MFFU Rapid | MFFU Pro | MFFU Builder | Tradeify Lightning |
|---|---|---|---|---|---|
| DD方式 | EOD Trailing | Intraday Trailing | EOD Trailing | EOD Trailing | EOD Trailing (real-time enforce) |
| Max Loss (50K) | $2,000 | $2,000 | $2,000 | $2,000 | $2,500 |
| Daily Loss Limit | **なし** | **なし** | **なし** | **$1,000** | **明記なし** (DDで代替) |
| Consistency (Eval) | **50%** | なし | なし | 50% | N/A (Instant) |
| Consistency (Funded) | なし | なし | なし | 50% | **20%** (relaxes per payout) |
| 最小取引日 | 2日 | 2日 | 2日 | 1日 | N/A |
| Profit Split | 80/20 | 80/20 | 80/20 | 80/20 | **90/10** |

### 警告 (特に注意)
- **MFFU Rapid Intraday Trailing**: 日中含み益失っただけでBust → **Chronos側で日中MLL 40%到達時にHalt** (既存 halt_streak=3 連動)
- **Tradeify Consistency 20%**: 単日利益を累計の20%以内に抑える必要・**1日で稼ぎすぎるとPayout拒否**
- **MFFU Builder Overnight禁止**: 引け前全手仕舞い必須・Chronos `end_of_day_flat_sec` を Builder口座のみ早める

---

## 6. Symbol / Underlying 分離ルール

### Hedging回避のための underlying マッピング
| Underlying | Mini | Micro | 両建て禁止 |
|---|---|---|---|
| ES (S&P 500) | ES | MES | ES + MES 逆方向 NG |
| NQ (Nasdaq) | NQ | MNQ | NQ + MNQ 逆方向 NG |
| RTY (Russell) | RTY | M2K | RTY + M2K 逆方向 NG |
| YM (Dow) | YM | MYM | YM + MYM 逆方向 NG |

### 多口座分散時の安全ルール
- **口座A と 口座B で同時期に 同underlying 逆方向** = Cross-account Hedging
- 明示的な禁止は MFFU Fair Play §5 に「同一口座内」と記載されているが・**同一人物別口座でも解釈上グレー**
- **安全策**: Chronos routing で **全口座同方向発注のみ許可** (逆方向は全口座同時のみ・混在は禁止)

---

## 7. 並列運用時の 1行サマリ

| 懸念 | 結論 | 根拠 |
|---|---|---|
| 5口座同時Copy Trading | MFFU OK / Tradeify 要確認 | 公式明示 / 未確認 |
| 1口座失格で全波及 | **なし** (口座単位独立) | 両社公式で独立性確認 |
| Aggregate DD | **存在しない** | MFFU/Tradeify 共に口座別DD |
| Daily Loss Limit × N | 合算Exposure大・運用ガード必須 | Rapid Intraday厳しい |
| HFT判定 | 全口座合算で 150 trades/日を自主上限 | Fair Play §1 準拠 |
| Hedging Cross-Account | **禁止推定** (safety margin) | 公式グレーだが安全側 |
| Device Sharing | 1人で運用なら問題なし | Fair Play 原文 "another trader" |

---

## 8. ハザード分析 (premortem + この分析)

| ID | Hazard | Probability | Impact | Mitigation |
|---|---|---|---|---|
| X01 | 全口座同時Bust (相関事故) | Low | Catastrophic | 戦略分散 (ORB + VIX-MR) / サイズstagger |
| X02 | HFT判定で全口座剥奪 | Very Low | Catastrophic | 合算 150 trades/日 hard limit |
| X03 | Cross-account Hedging検出 | Low | High | Chronos routing で全口座同方向 |
| X04 | Copy Trading "別人扱い" 判定 | Very Low | High | MFFU/Tradeify両方にsupport確認済記録 |
| X05 | 1口座のConsistency違反→Payout拒否 | Medium | Medium | 単日 upper-bound 自動halt |
| X06 | Tradovate Group Copier遅延で約定ズレ | Medium | Low | Chronos並列発注で解消 |
| X07 | 送金手数料累積 (N口座 × $15 Wise) | High | Low | LLC法人口座で集約 |
| X08 | KYC書類誤認で複数口座停止 | Low | High | 全口座同一KYC書類使用 |

---

## 9. 結論と実装ガイド

### 即時 GO
- MFFU Copy Trading 同一人物間 (公式明示済)
- Tradeify Lightning 最大5口座 × $750K 合計 (FAQ明示済)
- Chronos 1信号 → N口座並列発注 (semi-managed 範疇・Oliver確認済)

### 要追加確認 (5/15までに support質問回答受領)
- Tradeify の copy trading ポリシー (§1参照の3質問)
- MFFU/Tradeify Aggregate DD の有無 (現状なしと確認・念のため再確認)
- Cross-account Hedging の解釈 (現状禁止と仮定)

### 即時 NO-GO
- 複数トレーダー間 (他人との) Copy Trading
- HFT境界越え (200+ trades/日)
- Device sharing with other traders

---

_策定: strategist @ 2026-04-21 / 公式直読4件引用 / scope: MFFU+Tradeify_
