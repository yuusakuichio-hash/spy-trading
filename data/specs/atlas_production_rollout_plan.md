# Atlas 本番 段階的スケールアップ計画

作成日: 2026-04-21  
前提: analystによる2サイクルGO判定取得済み  
初回稼働: 4/24 (木) ET

---

## フェーズ概要

| フェーズ | 期間 | size | 日次目標トレード数 | 戦術 | 備考 |
|---|---|---|---|---|---|
| W1 | 4/24-4/30 | 1枚 | 1-2 | cs_sell + orb_buy | 慣らし運転 |
| W2 | 5/1-5/7 | 1枚 | 3-5 | cs_sell + orb_buy | 頻度上げ |
| W3 | 5/8-5/14 | 検討 2枚 | 3-5 | cs_sell + orb_buy | 実績次第 |
| M2+ | 5/15- | 2-3枚 | 5-8 | +戦術追加検討 | PDT回避後 |

---

## Week 1: 4/24 - 4/30 「慣らし運転」

**目標**: エラーなし稼働・手動介入ゼロ・日次損失0件  
**設定**: atlas_production_small.yaml そのまま (size=1, 日次最大1万円損失)

### Go 基準 (Week 1 → Week 2 移行)
- [ ] 5営業日中エラー停止ゼロ (kill_switch未発動)
- [ ] Pushover通知がスムーズに届いていること
- [ ] 日次P&L の記録が data/pnl.jsonl に正常蓄積されていること
- [ ] ペーパーとの発注乖離レポートを目視確認 (許容範囲: ±10%)

### No-Go 基準 (Week 2 移行を見送る)
- 3日以上連続でBot異常停止
- kill_switch が意図せず発動
- OpenD接続エラーが複数回発生

---

## Week 2: 5/1 - 5/7 「頻度拡大」

**目標**: 日次3-5トレードを安定実行・win rate > 50%  
**変更点**: ATLAS_MAX_SYMBOLS=2 → 3 に引き上げ (銘柄追加)

### 変更コマンド:
```bash
# launchctl setenv で更新 or plist 修正
launchctl setenv ATLAS_MAX_SYMBOLS 3
```

### Go 基準 (Week 2 → Week 3 移行)
- [ ] 10トレード以上実施
- [ ] 累計 P&L がプラスまたは ±0 (損益分岐点)
- [ ] 最大ドローダウンが証拠金の 5% 以内
- [ ] 3連敗 (halting) ゼロ

### No-Go 基準
- 累計損失が 3万円以上
- 日次 kill_switch 発動が2回以上

---

## Week 3: 5/8 - 5/14 「サイズ拡大検討」

**目標**: size=2 枚での稼働判断  
**前提条件**: W1+W2 合計15トレード以上・累計P&Lプラス

### サイズ拡大の判断基準
```
W1+W2 実績:
  トレード数 >= 15
  勝率 >= 50%
  最大ドローダウン < 5%
  Sharpe (簡易計算) >= 0.5
```

全条件クリアで ATLAS_MAX_QTY=2 に変更。  
いずれか未達の場合は W3 もサイズ1継続・原因分析を実施。

### 変更コマンド (条件クリア時):
```bash
launchctl setenv ATLAS_MAX_QTY 2
```

---

## Month 2+: 5/15 以降 「PDT回避後スケールアップ」

**前提**: 口座残高が $25,000 (375万円) 到達で PDT制限解除  
または: 1DTE戦略を徹底してPDTカウント消費ゼロ設計に移行

### 追加する戦術と優先順序

| 追加戦術 | 追加条件 | 期待寄与 |
|---|---|---|
| ic_sell | W2終了時にGO基準クリアで即追加 | VIX18-40帯でCS補完 |
| straddle_buy | 高ボラ発生時に追加 | 方向感ない局面の収益 |
| delta_hedge | ポートフォリオδ>0.30で自動発動 | リスク低減 |

上記は実績確認後に条件クリアで即追加。条件を満たしている戦術は全て同時稼働。

### スケールアップ上限目標
- M2 (5月): size=2-3枚・日次5-8トレード
- M3 (6月): size=3-5枚 (LLC設立後・法人口座検討)
- M4 (7月): PDT解除後フル稼働・月利8-12%目標

---

## 累計リスク計算 (参考)

| フェーズ | max daily loss | max cumulative | 備考 |
|---|---|---|
| W1 | 1万円 | 5万円 | size=1 の絶対上限 |
| W2 | 1万円 | 8万円 | 銘柄追加でリスク微増 |
| W3 | 2万円 | 12万円 | size=2 想定 |
| M2 | 3万円 | 18万円 | size=3 想定 |

120万円元本に対してフルスケール時 (M2) の最大ドローダウンは 18万円 = 15%。  
ゆうさくさん設定の DD<20% 条件内に収まる。

---

## 本番稼働の起動コマンド (承認後に使用)

```bash
# 本番小額モード起動 (ペーパーのplistとは別のplistを使う)
# plist例: ~/Library/LaunchAgents/com.spybot.live.plist

ATLAS_MODE=production_small \
ATLAS_MAX_QTY=1 \
ATLAS_MAX_SYMBOLS=2 \
ATLAS_DAILY_LOSS_PCT=0.033 \
python3 /Users/yuusakuichio/trading/spy_bot.py
```

既存のペーパーplist (`com.spybot.paper.plist`) は unload してから起動すること。  
ペーパーと本番の同時稼働は「重複発注」ではないが、Pushover通知が混在して判断しにくい。

---

## 緊急連絡系統

| 状況 | 対応 |
|---|---|
| 想定外の大ロス (1回で2万円以上) | 即 `touch data/kill_switch.flag` → moomooでポジクローズ |
| OpenD切断 | spy_bot は接続エラー → 自動retry → Pushover通知 |
| VIX 40超え突入 | cs_sell はStrategySelector がスキップ。ORBも停止。通常は自動対応 |
| 3連敗 halt | 当日停止 (自動)。翌日自動再開。追加介入不要 |
| kill_switch 誤発動 | `python3 -c "from common import kill_switch; kill_switch.deactivate()"` |

---

*最終更新: 2026-04-21 / builder (Sora Lab)*
