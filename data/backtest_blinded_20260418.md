# Blinded Backtest Results — 2026-04-18

## 事前登録基準
- primary_metric: sharpe_ratio
- 合格基準: sharpe >= 1.0, win_rate >= 50%, max_dd <= 25%
- データ: ThetaData SPY/QQQ 0DTE parquet (20240102 - 20260417)

## 結果サマリー

| 戦術 | n_trades | win_rate | sharpe | max_dd | total_pnl | 合否 |
|------|----------|----------|--------|--------|-----------|------|
| butterfly | 337 | 46.9% | -0.450 | 186.2% | $-982 | **FAIL** |
| ic_sell | 340 | 81.5% | 3.014 | 17.9% | $7075 | PASS |
| strangle_sell | 524 | 79.8% | 1.588 | 31.0% | $9613 | **FAIL** |
| orb_breakout | 86 | 40.7% | 1.943 | 20.5% | $5146 | **FAIL** |
| symbol_selector | 517 | 67.3% | 0.180 | 126.6% | $268 | **FAIL** |
| earnings_iv_crush | 73 | 90.4% | 7.262 | 18.1% | $15096 | PASS |
| portfolio_aggregator | 510 | 86.3% | 4.462 | 8.1% | $10741 | PASS |
| ivr_credit_spread | 216 | 95.8% | 2.015 | 25.8% | $1738 | **FAIL** |

## 戦術別詳細

### butterfly — FAIL
- トレード数: 337
- 勝率: 46.9%
- Sharpe比 (年率化): -0.4503
- 最大DD: 186.2%
- 累積P&L: $-981.50
- 合否: 不合格

**失敗原因分析:**
  - Sharpe -0.450 < 1.0 (ボラ比の収益不足)
  - 勝率 46.9% < 50% (方向性精度不足)
  - DD 186.2% > 25% (リスク管理要改善)

### ic_sell — PASS
- トレード数: 340
- 勝率: 81.5%
- Sharpe比 (年率化): 3.0141
- 最大DD: 17.9%
- 累積P&L: $7075.03
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### strangle_sell — FAIL
- トレード数: 524
- 勝率: 79.8%
- Sharpe比 (年率化): 1.5883
- 最大DD: 31.0%
- 累積P&L: $9613.05
- 合否: 不合格

**失敗原因分析:**
  - DD 31.0% > 25% (リスク管理要改善)

### orb_breakout — FAIL
- トレード数: 86
- 勝率: 40.7%
- Sharpe比 (年率化): 1.9429
- 最大DD: 20.5%
- 累積P&L: $5146.00
- 合否: 不合格

**失敗原因分析:**
  - 勝率 40.7% < 50% (方向性精度不足)

### symbol_selector — FAIL
- トレード数: 517
- 勝率: 67.3%
- Sharpe比 (年率化): 0.1804
- 最大DD: 126.6%
- 累積P&L: $267.50
- 合否: 不合格

**失敗原因分析:**
  - Sharpe 0.180 < 1.0 (ボラ比の収益不足)
  - DD 126.6% > 25% (リスク管理要改善)

### earnings_iv_crush — PASS
- トレード数: 73
- 勝率: 90.4%
- Sharpe比 (年率化): 7.2619
- 最大DD: 18.1%
- 累積P&L: $15095.76
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### portfolio_aggregator — PASS
- トレード数: 510
- 勝率: 86.3%
- Sharpe比 (年率化): 4.4617
- 最大DD: 8.1%
- 累積P&L: $10741.01
- 合否: 合格 (sharpe>=1.0, win>=50%, dd<=25%)


### ivr_credit_spread — FAIL
- トレード数: 216
- 勝率: 95.8%
- Sharpe比 (年率化): 2.0149
- 最大DD: 25.8%
- 累積P&L: $1737.50
- 合否: 不合格

**失敗原因分析:**
  - DD 25.7% > 25% (リスク管理要改善)


## 全体評価

- 合格戦術: 3/8 — ic_sell, earnings_iv_crush, portfolio_aggregator
- 不合格戦術: 5/8 — butterfly, strangle_sell, orb_breakout, symbol_selector, ivr_credit_spread

## 不合格戦術のアクションプラン

### butterfly
- Sharpe: -0.450 / 勝率: 46.9% / DD: 186.2%
- 推奨: パラメータ再設計（delta幅・IVR閾値・TP/SL比率）

### strangle_sell
- Sharpe: 1.588 / 勝率: 79.8% / DD: 31.0%
- 推奨: パラメータ再設計（delta幅・IVR閾値・TP/SL比率）

### orb_breakout
- Sharpe: 1.943 / 勝率: 40.7% / DD: 20.5%
- 推奨: パラメータ再設計（delta幅・IVR閾値・TP/SL比率）

### symbol_selector
- Sharpe: 0.180 / 勝率: 67.3% / DD: 126.6%
- 推奨: パラメータ再設計（delta幅・IVR閾値・TP/SL比率）

### ivr_credit_spread
- Sharpe: 2.015 / 勝率: 95.8% / DD: 25.7%
- 推奨: パラメータ再設計（delta幅・IVR閾値・TP/SL比率）
