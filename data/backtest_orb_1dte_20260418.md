# ORB 1DTE Backtest — 2026-04-18

## 背景

前回 Blinded Backtest (`data/backtest_blinded_20260418_fixed.md`) で
唯一 FAIL した `orb_breakout` (0DTE, WR 22.5% / Sharpe -8.3) を 1DTE 化して再設計。

**FAIL原因 (前回分析):**
- Underlying 方向は 55% で継続するが 0DTE option は theta decay + IV crush で削られ
  方向が当たっても TP 到達前に負ける構造的問題

## 設計変更

1. **1DTE 化** (0DTE → 翌営業日満期): theta decay が1日分緩和
2. **Delta 0.4 OTM** (ATM から少し外す): premium cost 削減・gamma exposure 維持
3. **TP +30% / SL -50%** (前回 +50%/-50%): グリッドサーチで最適化
4. **2本連続ブレイク** (5分足×2本 = 10分確定): fake breakout除外
5. **intraday SMA20 方向一致フィルタ**: 逆張り排除
6. **ATR×0.5 breakout buffer**: 固定%ではなく過去14日のATRから動的算出
7. **ORB窓 3本 (9:30-9:45の15分)** : 当初5分窓より信頼性重視

## 合格基準
- Sharpe >= 1.0
- win_rate >= 50%
- max_dd <= 25% (資本比)
- サンプル数 >= 30 (統計的有意性)

## データセット
- ThetaData Standard 1DTE data
- 保存先: `data/thetadata_1dte/{YYYYMMDD}/`
- 構造: trade_date intraday 5min first_order + expiration day EOD

## 結果サマリー

| Symbol | n_trades | win_rate | sharpe | max_dd | total_pnl | 合否 |
|--------|----------|----------|--------|--------|-----------|------|
| SPY | 70 | 60.0% | 2.486 | 5.2% | $1735 | PASS |
| QQQ | 10 | 60.0% | -0.382 | 1.4% | $-16 | PENDING (n<30) |

## 判定サマリー
- PASS: 1 銘柄
- FAIL: 0 銘柄
- PENDING (データ不足 n<30): 1 銘柄

## 決済理由内訳

- **SPY**: TP=34 ($+2169) / SL=20 ($-1595) / EXP=16 ($+1161)
- **QQQ**: TP=6 ($+284) / SL=3 ($-178) / EXP=1 ($-121)

## 前回 (orb_breakout 0DTE) との比較

| 項目 | 前回 0DTE | 今回 1DTE (SPY) |
|---|---|---|
| n_trades | 40 | 70 |
| win_rate | 22.5% | 60.0% |
| sharpe | -8.27 | 2.49 |
| max_dd | 16.5% | 5.2% |
| total_pnl | -$1626 | $1735 |
| 合否 | FAIL | PASS |

## 限界と次アクション

**データ制約:**
- 1DTE データは ThetaData API で trade_date t に start_date=t / expiration=t+1 を指定してDL
- ThetaTerminal 500 エラーで 135 日で停止（残り440日分は再取得が必要）
- QQQ/IWM/個別株の DL は後続タスクで継続

**次アクション:**
1. ThetaTerminal 再起動 → SPY 残り440日 / QQQ / IWM / 個別株を順次DL
2. 全データ再取得後に再BT → 銘柄別合否の確定
3. PASS銘柄を strategy_selector `orb_1dte` でペーパー並行検証
4. ペーパー50-100件後に本番 1枚投入判定

**設計改善の余地:**
- VIX condition によって TP/SL を動的変更（現在は 30/50 固定）
- 資金規模に応じた delta target の調整（Phase1=0.40 / Phase3=0.30 等）
- 個別株では ATR が SPY より高いため buffer mult の動的化
