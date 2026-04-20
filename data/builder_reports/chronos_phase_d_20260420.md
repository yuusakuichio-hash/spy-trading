# Chronos Phase D: 戦術最適化層 実装報告
Builder Report — 2026-04-20

## 実装概要

Phase A-C（ガード層: 失効防止）に続く **Phase D（戦術最適化層: 期待値最大化）** を完了した。

---

## 実装ファイル一覧

| ファイル | 種別 | 内容 |
|---|---|---|
| `/common/kelly_sizer.py` | 新設 | プラン別動的Kelly係数算出エンジン |
| `/common/kelly_sizer.yaml` | 新設 | Kelly設定YAML（コード変更不要で上書き可能） |
| `/chronos_strategy_selector.py` | 追記 | Phase D全機能をファイル末尾に追加 |
| `/tests/test_chronos_phase_d_20260420.py` | 新設 | Phase Dテスト 61件 |

---

## D-1: プラン別戦術プロファイル (`_PLAN_TACTIC_PROFILES`)

### 設計判断
各プランの制約（Consistency%・DDタイプ・利益上限）を戦術選択に織り込む。`select_futures_strategy()` の env dict に `plan_id` を追加するだけで機能する後付け設計。

### 実装プラン一覧

| plan_id | ORBスケール | Consistency上限 | 日次目標 | 特記 |
|---|---|---|---|---|
| flex_eval | 0.80 | 50% | $500 | 均等日利益・Consistency管理 |
| rapid_eval | 0.90 | 50% | $600 | EOD DD + Consistency50% |
| rapid_sim | 0.70 | なし | $400 | Intraday Trailing対応・短期利確 |
| pro_eval | 1.00 | 50% | $600 | 標準EOD・全戦術フル |
| pro_sim | 1.00 | なし | $600 | Consistency制限なし |
| builder_funded | 0.85 | 50% | $500 | Add-On+Consistency50% |
| tradeify | 0.75 | 35% | $400 | Day1 Consistency35% |
| apex | 0.80 | 30% | $400 | DCA禁止+30%/50%段階制 |

### `apply_plan_tactic_profile()` 動作フロー
1. Consistency接近チェック（`cons_limit - 5%` で予防ブロック）
2. 日次目標90%達成チェック（no_trade返却）
3. ORBサイズスケール適用
4. 低頻度戦術 size 30%縮小

---

## D-2: 動的Kelly係数 (`common/kelly_sizer.py`)

### Kelly計算式
```
f* = (b*p - q) / b
  b = rr_ratio (reward/risk比)
  p = win_rate
  q = 1 - win_rate
```
Half Kelly (×0.5) をデフォルトとして実務標準を適用。

### プロファイル制約係数（全プラン）

| plan_id | max_kelly | dd_scale | consistency_penalty | intraday_penalty |
|---|---|---|---|---|
| flex_eval | 0.20 | 0.90 | 0.80 | 1.00 |
| rapid_sim | 0.18 | 0.85 | 1.00 | 0.80 |
| apex_safety_net | 0.15 | 0.85 | 0.65 | 0.85 |
| pro_eval | 0.25 | 0.90 | 0.82 | 1.00 |

### YAML設定
`common/kelly_sizer.yaml` の `plans:` セクションにキーを追記するだけでコード変更なし上書き可能。実績データ蓄積後に勝率・RR比を実測値に更新する設計。

---

## D-3: HFT 200件/日カウンタ戦術連動 (`check_hft_limit()`)

MFFU規則「200件/日超でHFT認定→失格」に対し、`common/prop_firm_rules.yaml` の `max_trades_per_day=180` を基準に3段階制御を実装。

| 件数 | 処置 |
|---|---|
| < 150件 | 変更なし |
| 150-174件 (warn帯) | 全戦術のsize_pctを線形縮小 (最大50%縮小) |
| >= 175件 (stop帯) | 新規エントリー完全停止 (no_trade返却) |

定数:
```python
HFT_DAILY_LIMIT  = 180   # prop_firm_rules.yaml 準拠
HFT_WARN_THRESH  = 150
HFT_STOP_THRESH  = 175
```

---

## D-4: DCA検知ロジック (`check_dca_violation()`)

**Apex 2026/4改定**: PA口座で損失ポジションへの追加発注 → 自動失格。

### 検知条件
- 同一銘柄 (`symbol`)
- 同一方向 (`direction`)
- 保有ポジションの `unrealized_pnl < 0`

この3条件が全て成立したときDCA違反と判定し `(True, reason_str)` を返す。

### 非DCA判定
- 逆方向エントリー（ヘッジ的）: 違反なし
- 異なる銘柄: 違反なし
- 利益中のポジションへの追加: 違反なし

Apex (`plan_id="apex"`) では `strict=True` のログを出力して追跡可能にする。

---

## D-5: 1日利益上限制御 (`check_daily_profit_cap()`)

### Tradeify Day1 Consistency 35%対応
日次利益が累積の 35% に接近したら新規エントリーを停止。

安全マージン: `cons_limit - 5%` でブロック（例: 35% → 30% で停止）。

### 統合ラッパー `select_futures_strategy_with_plan()`

```python
# 1. HFTカウンタチェック
strategies = check_hft_limit(trade_count_today, strategies)
# 2. 日次利益上限制御
can_enter, reason = check_daily_profit_cap(daily_pnl, cumulative_pnl, plan_id)
# 3. プラン別戦術プロファイル適用
strategies = apply_plan_tactic_profile(strategies, plan_id, ...)
```

`env` dict に `plan_id` / `trade_count_today` / `open_positions` を含めるだけで自動取得する設計。

---

## テスト結果

| 項目 | 件数 |
|---|---|
| Phase D 新規テスト | **61件** |
| 既存テスト (Phase D前ベースライン) | 1,911件 |
| Phase D 導入後 合計 | **1,972件** |
| Phase D 新規失敗 | **0件** |
| 既存失敗 (変化なし) | 34件（Phase D前から存在） |

### テストクラス構成

| クラス | 件数 | 検証内容 |
|---|---|---|
| TestKellySizerCore | 10 | Kelly計算・プラン別制約・境界値 |
| TestKellySizerSizePct | 5 | 日次目標・HFT近接ペナルティ |
| TestKellySizerConvenienceFunctions | 3 | 便利関数・未知plan_id耐性 |
| TestPlanTacticProfiles | 9 | プロファイル取得・Consistency guard |
| TestHFTLimitGuard | 6 | HFT件数別動作・境界値 |
| TestDCADetection | 7 | DCA違反判定・非違反ケース |
| TestDailyProfitCap | 8 | Tradeify/Flex/rapid_sim各プラン |
| TestSelectFuturesStrategyWithPlan | 7 | 統合ラッパー E2E動作 |
| TestPlanProfileCompleteness | 6 | 全プロファイル構造整合性 |

---

## 呼び出し方（chronos_bot.py組込み用）

Phase D の全機能を使う最小呼び出しパターン:

```python
from chronos_strategy_selector import select_futures_strategy_with_plan

env = build_env_dict(...)
env["plan_id"]           = "flex_eval"     # プランID
env["trade_count_today"] = trade_counter   # 今日の発注件数
env["open_positions"]    = open_pos_list   # 保有ポジション (DCA検知用)

strategies = select_futures_strategy_with_plan(env)
```

既存の `select_futures_strategy(env)` 呼び出しを `select_futures_strategy_with_plan(env, plan_id=...)` に置き換えるだけで移行完了。

Phase A-C 組込み作業 (Task 13) 完了後、同タスク担当 builder が Phase D の呼び出し追加を実施する。

---

## 残課題・注意事項

1. **Kelly係数の勝率・RR比**: 現時点の設定は `win_rate=0.55 / rr_ratio=1.30` (MES実績ベース推定値)。ペーパー50件以上蓄積後に実測値で `kelly_sizer.yaml` を更新する。

2. **DCA検知の `open_positions` フォーマット**: `chronos_bot.py` 側で `{'symbol', 'direction', 'unrealized_pnl'}` の形式でリストを渡す実装が必要。

3. **Apex の Consistency段階切替**: Safety Net期間 (30%) → Payout後 (50%) の切替は、`chronos_bot.py` 側で `plan_id` を `"apex_safety_net"` / `"apex_post_payout"` と動的に変更することで対応する。

---

## 規律確認

- Blue Team / builder自己採点禁止: 本レポートはBlue Teamによる自己評価ではなく実装記録。
  Red Teamによる独立検証を別セッションで実施すること。
- 固定パラメータ禁止: 全Consistency%・ORBスケールは `_PLAN_TACTIC_PROFILES` の参照値。
  ハードコード禁止規律を遵守。
- YAML config化: Kelly係数設定は `common/kelly_sizer.yaml` で上書き可能。
