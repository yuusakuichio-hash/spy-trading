# Chronos 先物トレーダー判定レポート

**採点日時**: 2026-04-19T21:48:34.412308
**対象コードベース**: `/Users/yuusakuichio/trading`
**対象ファイル**: 16 本 (chronos_bot.py, chronos_rules.yaml, chronos_mffu_rules.py, chronos_strategy_selector.py, chronos_pre_trade_check.py, chronos_symbol_meta.py, tradovate_client.py, futures_vix_mr.py...)

---

## 1. エグゼクティブサマリー

### 合計点: **80 / 80 点 (100.0%)**

**合格判定: EXCELLENT**

- 合格ライン (60点 / 75%): **達成**
- 優秀ライン (70点 / 87.5%): **達成**

### スコア分布

| ランク | 対象 |
|---|---|
| 5 マスター級 | F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12, F13, F14, F15, F16 |
| 4 高度実装 | (なし) |
| 3 基本実装 | (なし) |
| 2 部分実装 | (なし) |
| 1 部分実装(バグ) | (なし) |
| 0 未実装 | (なし) |

### 改善優先 TOP 3

---

## 2. 16項目 詳細採点

| ID | 項目 | 点数 | 判定 |
|---|---|---|---|
| F1 | ORBセットアップ規律 | 5/5 | EXCELLENT |
| F2 | VIX-MR タイミング | 5/5 | EXCELLENT |
| F3 | Max Loss遵守 | 5/5 | EXCELLENT |
| F4 | Consistency管理 | 5/5 | EXCELLENT |
| F5 | News Window回避 | 5/5 | EXCELLENT |
| F6 | Globex Maintenance Break | 5/5 | EXCELLENT |
| F7 | Hedging禁止遵守 | 5/5 | EXCELLENT |
| F8 | 連敗制御 | 5/5 | EXCELLENT |
| F9 | セッション認識 | 5/5 | EXCELLENT |
| F10 | ATR Regime適応 | 5/5 | EXCELLENT |
| F11 | VWAP使用 | 5/5 | EXCELLENT |
| F12 | Cumulative Delta | 5/5 | EXCELLENT |
| F13 | Liquidity Sweep認識 | 5/5 | EXCELLENT |
| F14 | Phase認識 | 5/5 | EXCELLENT |
| F15 | Rate-limit処理 | 5/5 | EXCELLENT |
| F16 | Risk-per-trade サイジング | 5/5 | EXCELLENT |

### F1. ORBセットアップ規律 — **5/5 点**

**評価根拠**: FuturesORBStrategy実装済み・5分OR確定・VIX>=20フィルタ・RR動的(STOP=OR×1.0, TP=OR×2.0)。

**実装エビデンス**:
- `chronos_bot.py:887` — `class FuturesORBStrategy:`
- `chronos_bot.py:942` — `def update_or_candle(self, high: float, low: float):`
- `chronos_bot.py:949` — `def finalize_or(self):`
- `chronos_bot.py:963` — `def check_breakout(`
- `chronos_rules.yaml:37` — `- orb      # Futures ORB (VIX>=20のみ) — バックテスト済み`

### F2. VIX-MR タイミング — **5/5 点**

**評価根拠**: VIX-MR完全実装: Z>=1.5で15:40-15:55 ETにentry, 5日hold, VIX帯別size調整(panic 0.5 / high 1.0 / other 0.7)

**実装エビデンス**:
- `futures_vix_mr.py:3` — `futures_vix_mr.py — Overnight VIX Mean Reversion 戦術`
- `chronos_rules.yaml:38` — `- vix_mr   # Overnight VIX Mean Reversion (Zscore>1.5) — バックテスト済み`
- `chronos_rules.yaml:47` — `# VIX-MR overnight エントリー: 15:40-15:55 ET（RTH終了前）`
- `chronos_rules.yaml:71` — `vix_mr_sl_pct: 0.015  # SL: エントリー価格 -1.5%`
- `chronos_rules.yaml:72` — `vix_mr_tp_pct: 0.010  # TP: エントリー価格 +1.0%`

### F3. Max Loss遵守 — **5/5 点**

**評価根拠**: Max Loss ガード完全実装 (Daily soft stop含む)

**実装エビデンス**:
- `chronos_bot.py:650` — `class MFFURuleGuard:`
- `chronos_mffu_rules.py:136` — `class MFFURules:`
- `chronos_mffu_rules.py:172` — `def trailing_drawdown_floor(self) -> float:`
- `chronos_bot.py:673` — `INTRADAY_STOP_PCT  = 0.90   # 90%消費で予防的停止（任意・保守的設定）`
- `chronos_mffu_rules.py:82` — `sim_max_loss_after_payout_usd: float = 100.0  # 初回ペイアウト後のMLL`

### F4. Consistency管理 — **5/5 点**

**評価根拠**: Consistency管理完全: is_consistency_applicable() Phase分岐 + Eval 50%ルール + 予防35% soft cap + Sim-Fundedスキップ + daily_pnl違反検知 + call_site統合すべて実装

**実装エビデンス**:
- `chronos_mffu_rules.py:225` — `def is_consistency_applicable(phase: str) -> bool:`
- `chronos_mffu_rules.py:18` — `- Consistency Rule: 1日の利益が全利益の50%以内（単日集中禁止）`
- `chronos_strategy_selector.py:16` — `- check_consistency_safety()      — Consistency Rule 35%予防ブロック`
- `chronos_rules.yaml:297` — `# 口座タイプ ("demo" \| "mffu_eval" \| "mffu_sim_funded" \| "mffu_sim_funded_after_payout")`
- `chronos_mffu_rules.py:269` — `def check_consistency(daily_pnl_list: list[float], phase: str,`

### F5. News Window回避 — **5/5 点**

**評価根拠**: NewsTradingFilter + 2分窓 + T1 events + ORB/Level経路でのNewsGuard統合・既存ポジhold許可

**実装エビデンス**:
- `chronos_bot.py:495` — `class NewsTradingFilter:`
- `chronos_rules.yaml:207` — `blackout_window_sec: 120`
- `chronos_rules.yaml:210` — `t1_events:`
- `chronos_rules.yaml:211` — `- "FOMC"`
- `chronos_bot.py:982` — `news_check = self.news_filter.is_blackout(now_et)`

### F6. Globex Maintenance Break — **5/5 点**

**評価根拠**: is_maintenance_break + 17:00-18:00 ET設定 + block_new_orders + run_forever経路統合すべて完備

**実装エビデンス**:
- `chronos_bot.py:1812` — `def _is_maintenance_break(self, now_et: Optional[datetime.datetime] = None) -> bool:`
- `chronos_rules.yaml:231` — `# CME Globex 毎日 ET 17:00-18:00 の清算窓（市場閉場）`
- `chronos_rules.yaml:240` — `block_new_orders: true`
- `chronos_bot.py:2679` — `if self._is_maintenance_break(now_et):`

### F7. Hedging禁止遵守 — **5/5 点**

**評価根拠**: check_hedging_violation + MES/ES / MNQ/NQ / MYM/YM / M2K/RTY 4ペアのテーブル + long/short 逆方向ロジック完備。ただし call_site が pre_trade_check に統合されていない場合は発注経路で呼ばれない

**実装エビデンス**:
- `chronos_pre_trade_check.py:235` — `def check_hedging_violation(`
- `chronos_pre_trade_check.py:50` — `# MES と ES は実質同一プロダクト (S&P500 先物) のため両建て禁止`
- `chronos_pre_trade_check.py:50` — `# MES と ES は実質同一プロダクト (S&P500 先物) のため両建て禁止`
- `chronos_bot.py:1051` — `# place_order 直前で check_hedging_violation() を必ず通過させる。`

### F8. 連敗制御 — **5/5 点**

**評価根拠**: consecutive_loss_guard完全実装: 2連敗50% / 3連敗25% / 5連敗停止 + _apply_loss_scaling + record_trade_result + 日次リセット

**実装エビデンス**:
- `chronos_rules.yaml:247` — `consecutive_loss_guard:`
- `chronos_bot.py:1425` — `self._consecutive_losses:   int  = 0       # 連続負け数（日次リセット）`
- `chronos_bot.py:1934` — `def record_trade_result(self, pnl: float) -> None:`
- `chronos_bot.py:1424` — `# 2連敗→50%, 3連敗→25%, 5連敗→当日停止`
- `chronos_bot.py:3402` — `# 連敗カウンタ日次リセット (consecutive_loss_guard: daily_reset_et="09:00")`

### F9. セッション認識 — **5/5 点**

**評価根拠**: 5セッション分類 + Time-of-Day Bias + selector統合 + Asia Range Fade / London Breakout まで完備

**実装エビデンス**:
- `futures_session_strategy.py:126` — `def get_current_session(et_time: Optional[str] = None) -> str:`
- `futures_session_strategy.py:38` — `"asia": {`
- `futures_session_strategy.py:45` — `"london": {`
- `futures_time_of_day_bias.py:3` — `futures_time_of_day_bias.py — 時間帯別サイズ重み係数（Time-of-Day Bias）`
- `chronos_strategy_selector.py:20` — `セッション統合 (futures_session_strategy.py):`

### F10. ATR Regime適応 — **5/5 点**

**評価根拠**: ATR Regime 完全実装 (yaml + 分類関数 + fetch + size_mult + TP/SL比例)

**実装エビデンス**:
- `chronos_rules.yaml:272` — `# get_atr_regime() の分類乗数設定`
- `chronos_strategy_selector.py:254` — `def get_atr_regime(atr_14d: float, atr_history_60d: list[float]) -> str:`
- `chronos_bot.py:1770` — `gf_atr_5d: float = 10.0  # フォールバック値`
- `chronos_bot.py:1781` — `gf_atr_5d = sum(ranges) / len(ranges)`
- `chronos_rules.yaml:283` — `# レジーム別の atr_size_multiplier（size_pct × atr_size_mult）`

### F11. VWAP使用 — **5/5 点**

**評価根拠**: VWAP 完全実装: calc_vwap + update_vwap + Reclaim/Rejection + bot統合 + Anchored対応

**実装エビデンス**:
- `futures_level_trading.py:107` — `def calc_vwap(prices: list[float], volumes: list[float]) -> Optional[float]:`
- `futures_level_trading.py:382` — `self.vwap          = None`
- `futures_level_trading.py:335` — `VWAP_REVERT_SIGMA     = 0.5     # VWAP乖離シグナル閾値（IB Range × 0.5）`
- `chronos_bot.py:1632` — `self.vwap = None`
- `chronos_bot.py:161` — `get_anchored_vwap_set,      # Anchored VWAP (前日高・前日安・FOMC) 3アンカー`

### F12. Cumulative Delta — **5/5 点**

**評価根拠**: Cumulative Delta 完全実装: AST確認済みCumulativeDelta / import成功 / テスト合格 / bot daily_reset連動 / selector統合

**実装エビデンス**:
- `chronos_cumulative_delta.py:0` — `[AST] CumulativeDelta found: methods_implemented=['__init__', 'update', 'update_from_bar', '_maybe_flush_bucket', '_flush_bucket', 'daily_reset', 'get_current_delta', 'get_current_bucket_delta', 'get_total_delta', 'get_bucket_delta', 'get_buckets', 'detect_divergence', 'get_strategy_bias']`
- `chronos_cumulative_delta.py:0` — `[IMPORT] chronos_cumulative_delta import: OK`
- `test_f12_f13_critical_fixes_20260419.py:0` — `[PYTEST] F12 tests: passed=33 failed=0`
- `chronos_bot.py:3375` — `self.cumulative_delta.daily_reset()`
- `chronos_bot.py:1647` — `env_dict["cumulative_delta_bias"] = _cd_bias`

### F13. Liquidity Sweep認識 — **5/5 点**

**評価根拠**: Liquidity Sweep 完全実装: AST確認済みLiquiditySweepDetector / import成功 / C5 ATRフィルタ修正 / テスト合格

**実装エビデンス**:
- `chronos_liquidity_sweep.py:0` — `[AST] LiquiditySweepDetector found: methods_implemented=['__init__', '_build_levels', 'update_levels', 'check_sweep', '_check_level_sweep', 'is_reversal_confirmed', 'get_entry_signal', 'has_pending_sweep', 'get_pending_sweep', 'clear_pending', 'is_sweep_expired', 'get_levels']`
- `chronos_liquidity_sweep.py:0` — `[IMPORT] chronos_liquidity_sweep import: OK`
- `chronos_liquidity_sweep.py:61` — `return self.volume_ratio >= 2.0 and self.atr_breach > 0.0`
- `test_f12_f13_critical_fixes_20260419.py:0` — `[PYTEST] F13 tests: passed=33 failed=0`

### F14. Phase認識 — **5/5 点**

**評価根拠**: Phase認識完全: 4 Phase定数 + account_type分岐 + on_payout_received遷移 + survival_mode + _get_active_phase_config

**実装エビデンス**:
- `chronos_mffu_rules.py:53` — `PHASE_EVALUATION = "evaluation"`
- `chronos_mffu_rules.py:54` — `PHASE_SIM_FUNDED = "sim_funded"`
- `chronos_bot.py:1428` — `# ── MVP追加: Phase / account_type 管理 ──────────────────────────────────`
- `chronos_bot.py:2230` — `def on_payout_received(self) -> bool:`
- `chronos_bot.py:1451` — `# MFFU: 初回ペイアウト後 MLL $100 → survival_mode_after_payout 適用`

### F15. Rate-limit処理 — **5/5 点**

**評価根拠**: Rate-limit完全実装: _request_with_backoff + 指数backoff + 連続halt + p-ticket検知

**実装エビデンス**:
- `tradovate_client.py:342` — `def _request_with_backoff(`
- `tradovate_client.py:59` — `RATE_LIMIT_STATUS_CODE        = 429   # HTTP Too Many Requests`
- `tradovate_client.py:213` — `self._rate_limit_halted:      bool = False  # 当日取引停止フラグ`
- `tradovate_client.py:250` — `if "p-ticket" in data:`

### F16. Risk-per-trade サイジング — **5/5 点**

**評価根拠**: Risk-per-trade 完全実装: Kelly + OR幅risk + Consistency-cap + max_contracts + 口座%制限

**実装エビデンス**:
- `chronos_bot.py:21` — `spy_bot.calc_kelly_fraction() — 流用済み（strategy_filter対応済み）`
- `chronos_bot.py:1151` — `contracts = floor(dollar_risk / risk_per_contract)`
- `chronos_bot.py:1192` — `max_daily_pnl = max(monthly_realized_pnl, monthly_target) * 0.35`
- `chronos_mffu_rules.py:200` — `def get_max_mini_contracts(self) -> int:`
- `chronos_rules.yaml:86` — `max_loss_per_trade_pct: null     # バックテスト後に設定`

---

## 3. 合格ラインへのギャップ分析

- 現在点: 80/80
- 合格まで: **0点**
- 優秀まで: **0点**

**優秀ライン達成。私募ファンド運用水準に到達。**

---

## 4. 次回採点予定

- **Week 1 (MFFU Eval開始直後)**: ペーパー5日稼働後に再採点。F8 連敗制御・F15 rate-limit の実動作確認 (実トレード駆動)
- **Week 2-3 (Eval通過前)**: Red Team対応を含む改善完了後に再採点。F10 ATR Regime を実装して +2-3点
- **Month 1 (Sim-Funded移行後)**: Phase切替の実動作確認 + F4 Consistency自動遷移の検証
- **Month 3 (ThetaData Pro契約時)**: F12/F13 (Cumulative Delta / Liquidity Sweep) 実装で +6-8点・優秀ライン到達を目標

---

## 5. 参考資料

- `data/research_mes_trader_day_20260419.md` — 起点調査 (16項目ドラフト§5 / Chronos翻訳表§4)
- `data/futures_trader_evaluation_framework.md` — 本FWの設計書
- `scripts/trader_evaluation.py` — Atlas版 (0DTE 15指標) 同型スクリプト
- `data/eval/trader_eval_20260418.md` — Atlas採点前例

*Generated by scripts/futures_trader_evaluation.py (Sora Lab / Chronos) — 2026-04-19 21:48 JST*
