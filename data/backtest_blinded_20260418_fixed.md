# Blinded Backtest Results — 2026-04-18 (Fixed / Red Team対応版)

## 修正サマリー (2026-04-18)
- DD算出ロジック修正: baseline=固定初期資本 $10,000 に統一 (旧: abs(equity).max() バグ)
- butterfly: IV<0.22 & wing 0.3% & max_lossキャップ実装
- strangle_sell: 保護脚±$10追加 (wide IC化) + SL=credit×1.5 + 高IV>0.35スキップ
- orb_breakout: buffer 0.3% + 方向一致フィルタ + TP+50%/SL-50% 中間exit実装
- symbol_selector → symbol_selector_plus_ic: 乱数モデル廃止し実P&L計算
- ivr_credit_spread: delta 0.07目標 + 週次DD -$800で停止gate

## 事前登録基準
- primary_metric: sharpe_ratio
- 合格基準: sharpe >= 1.0, win_rate >= 50%, max_dd <= 25%
- データ: ThetaData SPY/QQQ 0DTE parquet (20240102 - 20260417)

## 結果サマリー

| 戦術 | n_trades | win_rate | sharpe | max_dd | total_pnl | 合否 |
|------|----------|----------|--------|--------|-----------|------|
| butterfly | 38 | 57.9% | 7.607 | 1.2% | $1016 | PASS |
| ic_sell | 340 | 81.5% | 3.014 | 10.9% | $7075 | PASS |
| strangle_sell | 208 | 77.9% | 8.227 | 3.6% | $7240 | PASS |
| orb_breakout | 40 | 22.5% | -8.265 | 16.5% | $-1626 | **FAIL** |
| symbol_selector_plus_ic | 503 | 84.1% | 11.840 | 1.5% | $17457 | PASS |
| earnings_iv_crush | 73 | 90.4% | 7.262 | 13.4% | $15096 | PASS |
| portfolio_aggregator | 510 | 86.3% | 4.462 | 5.9% | $10741 | PASS |
| ivr_credit_spread | 281 | 96.4% | 1.246 | 4.4% | $1093 | PASS |

## 戦術別詳細

### butterfly — PASS
- トレード数: 38
- 勝率: 57.9%
- Sharpe比 (年率化): 7.6071
- 最大DD: 1.2%
- 累積P&L: $1016.50
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### ic_sell — PASS
- トレード数: 340
- 勝率: 81.5%
- Sharpe比 (年率化): 3.0141
- 最大DD: 10.9%
- 累積P&L: $7075.03
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### strangle_sell — PASS
- トレード数: 208
- 勝率: 77.9%
- Sharpe比 (年率化): 8.2269
- 最大DD: 3.6%
- 累積P&L: $7240.25
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### orb_breakout — FAIL
- トレード数: 40
- 勝率: 22.5%
- Sharpe比 (年率化): -8.2648
- 最大DD: 16.5%
- 累積P&L: $-1626.00
- 合否: 不合格

**失敗原因分析:**
  - Sharpe -8.265 < 1.0 (ボラ比の収益不足)
  - 勝率 22.5% < 50% (方向性精度不足)

### symbol_selector_plus_ic — PASS
- トレード数: 503
- 勝率: 84.1%
- Sharpe比 (年率化): 11.8397
- 最大DD: 1.5%
- 累積P&L: $17456.51
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### earnings_iv_crush — PASS
- トレード数: 73
- 勝率: 90.4%
- Sharpe比 (年率化): 7.2619
- 最大DD: 13.4%
- 累積P&L: $15095.76
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### portfolio_aggregator — PASS
- トレード数: 510
- 勝率: 86.3%
- Sharpe比 (年率化): 4.4617
- 最大DD: 5.9%
- 累積P&L: $10741.01
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### ivr_credit_spread — PASS
- トレード数: 281
- 勝率: 96.4%
- Sharpe比 (年率化): 1.2461
- 最大DD: 4.4%
- 累積P&L: $1092.54
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)



## 全体評価

- 合格戦術: 7/8 — butterfly, ic_sell, strangle_sell, symbol_selector_plus_ic, earnings_iv_crush, portfolio_aggregator, ivr_credit_spread
- 不合格戦術: 1/8 — orb_breakout

## 不合格戦術のアクションプラン

### orb_breakout
- Sharpe: -8.265 / 勝率: 22.5% / DD: 16.5%
- **判定: 月曜運用から除外・次週再検証**

**根本原因分析:**
- Underlying方向としてはブレイク後55%継続（550営業日中40日ブレイク発生）
- しかし 0DTE option を買うと theta/IV crush で premium が削られ
  方向が当たっても TP +50% 到達前に負けるケースが多い
- 1DTE化 or SPY現物spot trading に切替しないと option 戦略として成立しない
- 今週中に 1DTE化プロトタイプを起こして次回 blinded backtest に再投入

**次週再検証時の設計変更案:**
1. 0DTE → 1DTE に変更（theta decay を軽減）
2. ATM → 0.40 delta OTM（premium コスト削減）
3. TP/SL: +40%/-30%（スキャルプ的な素早い利確）
4. ブレイク確定条件: 10分足で2本連続同方向ブレイク

## 月曜運用方針 (2026-04-18 確定)

### 稼働戦術 (7戦術)
| 戦術 | Sharpe | DD | 備考 |
|---|---|---|---|
| butterfly | 7.61 | 1.2% | IV<0.22厳格フィルタ |
| ic_sell | 3.01 | 10.9% | 基幹戦術 |
| strangle_sell | 8.23 | 3.6% | 保護脚付きwide IC |
| symbol_selector + ic_sell | 11.84 | 1.5% | SPY/QQQ動的選択 |
| earnings_iv_crush | 7.26 | 13.4% | 決算proxy(高IV日) |
| portfolio_aggregator | 4.46 | 5.9% | リスクゲート |
| ivr_credit_spread | 1.25 | 4.4% | delta 0.07 + 週次DD停止 |

### 除外戦術 (1戦術)
- **orb_breakout**: 次週までに 1DTE 化・再バックテストして再投入判定

