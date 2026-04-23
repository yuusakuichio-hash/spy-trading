# Atlas 0DTE System BT 結果メモ（2026-04-23）

## 概要

ADR-013 v2 / Gemini A2 ペルソナ準拠の 0DTE System 戦術（`zero_dte_system.py`）の
実装完了時点での BT 方針・Shadow Live 設計記録。

実際の Walk-forward BT は Phase 2 で ThetaData / moomoo 分足データ接続後に実施する。

---

## Shadow Live 設計（o3 警告対応）

o3 による審査で指摘された 0DTE Paper vs 実弾 fill 乖離問題への対応として、
`ZeroDTEConfig.shadow_live_mode=True` のとき `ZeroDTEEntryDecision.shadow_live=True`
フラグを decision に付与する経路を設計上確保した。

Phase 2 での BrokerClient 接続時に以下を実装する:
- paper 発注と live 発注を同時実行
- fill 価格差（乖離）を `data/research_v3/bt_results/fill_divergence_log.jsonl` に記録
- 乖離が設定閾値を超えた場合は Pushover EICAS Advisory を発出

---

## BT 対象期間（Phase 2 実施予定）

- 対象: 2024-01-01 〜 2025-12-31（過去 2 年分 0DTE データ）
- データソース: ThetaData（`project_thetadata.md` 参照）
- 手法: Walk-forward BT（6 ヶ月 in-sample / 3 ヶ月 out-of-sample）
- 評価指標: Sharpe / Max Drawdown / Win Rate / Avg PnL per trade

---

## ストラクチャー選択ルール（実装済み）

| ストラクチャー | 条件 | 備考 |
|---|---|---|
| butterfly | VIX < 15 かつ bias == neutral | 超低 VIX・レンジ特化 |
| iron_fly   | VIX <= 25 かつ \|GEX\| >= 0.5 | Gamma Level 反発・pin risk |
| credit_spread | VIX <= 35 | 方向性バイアス + ORB ブレイクアウト |
| none | 上記以外 | エントリーしない |

---

## 損切り・強制クローズルール（実装済み）

- プレミアム 50% 逆行（credit: max_credit * 0.50 / long: entry_price * 0.50）
- 強制クローズ: 15:30 ET（`force_close_hour_et=15`, `force_close_minute_et=30`）
- Daily Stop: `daily_stop_loss`（デフォルト -2000 円相当）

---

## 実装ファイル

- `atlas_v3/strategies/zero_dte_system.py`（本体）
- `tests/test_atlas_v3_0dte_system.py`（テスト 31 件・全 PASS 確認済み）

---

## Phase 2 BT 実施時の注意事項

1. 0DTE は bid/ask スプレッドが広いため slippage_tolerance_bps を保守的に設定すること
2. IV Crush モード（CPI/FOMC 発表日）は long premium を禁止する実装が必要（現在フラグのみ）
3. fill 乖離が大きい場合は Shadow Live モードで十分なサンプルを積んでから実弾移行すること
