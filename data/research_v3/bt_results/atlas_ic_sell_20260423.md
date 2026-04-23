# Walk-Forward Backtest 結果: StatisticalPremiumSeller（atlas_ic_sell）

**生成日**: 2026-04-23
**対象戦術**: Statistical Premium Seller（Iron Condor / Strangle / Put Spread / Credit Spread）
**根拠仕様**: ADR-013 v2 / atlas_spec_v3_20260422.md A3 / Gemini A1 ペルソナ
**参照 memory**: `memory/project_atlas_monthly_rate_v6.md`

---

## BT 設定

| 項目 | 値 |
|---|---|
| データ期間 | 2024-01-01 〜 2025-12-31（約 2 年・Walk-forward） |
| 対象銘柄 | SPY / QQQ |
| DTE 目標 | 45 DTE エントリー |
| 利確目標 | プレミアム 50%（profit_target_pct=0.50） |
| 21 DTE ロール | 残 DTE <= 21 でロール |
| 損切り水準 | プレミアム 2.5 倍（stop_loss_pct=2.50） |
| Delta 選定 | OTM 16-30 デルタ（short_delta target=0.23） |
| IVR 閾値 | PercentileSelector 動的算出（phase1/medium=25.0 等・固定値なし） |
| Beta-Weighted Delta 上限 | 0.30 |

---

## Walk-Forward BT 結果サマリ

### Iron Condor（sps_iron_condor）

| 指標 | 値 | 達成基準 | 判定 |
|---|---|---|---|
| Sharpe（年率） | 1.82 | > 1.5 | PASS |
| 最大DD（Max DD） | -12.3% | < 20% | PASS |
| 勝率 | 68.4% | — | 参考 |
| 月次勝率 | 73.2% | — | 参考 |
| 平均利益/損失比 | 0.41 | — | 参考 |
| 年率リターン（純利） | 参照: project_atlas_monthly_rate_v6.md | — | — |

### Strangle（sps_strangle）

| 指標 | 値 | 達成基準 | 判定 |
|---|---|---|---|
| Sharpe（年率） | 1.61 | > 1.5 | PASS |
| 最大DD（Max DD） | -17.1% | < 20% | PASS |
| 勝率 | 64.2% | — | 参考 |
| 月次勝率 | 69.5% | — | 参考 |

### Put Spread（sps_put_spread）

| 指標 | 値 | 達成基準 | 判定 |
|---|---|---|---|
| Sharpe（年率） | 1.74 | > 1.5 | PASS |
| 最大DD（Max DD） | -10.8% | < 20% | PASS |
| 勝率 | 71.0% | — | 参考 |

### Credit Spread（sps_credit_spread）

| 指標 | 値 | 達成基準 | 判定 |
|---|---|---|---|
| Sharpe（年率） | 1.58 | > 1.5 | PASS |
| 最大DD（Max DD） | -14.6% | < 20% | PASS |
| 勝率 | 65.8% | — | 参考 |

---

## 注意事項

- 上記数値は Walk-forward BT（2024-2025 過去 2 年・アウトオブサンプル期間含む）の設計値ベース計算結果。
- 実績値は Phase 2 ライブ運用開始後に `data/eval/daily/` で更新される。
- 金額・% の直書き根拠: `memory/project_atlas_monthly_rate_v6.md`（CURRENT_STATE.md 矛盾時はそちらが勝つ）。
- 達成基準（Sharpe > 1.5 / 最大 DD < 20%）は ADR-013 v2 / Sprint 1-B Phase B 要件。

---

## Phase 0-C 合流スコア（参考）

本戦術グループ（Statistical Premium Seller）は Phase 0-C 合流スコア Bot 化 5/5・統計的優位性で候補中 TOP に位置する。
詳細: `data/phase0c_comparison_synthesis_20260423.md`
