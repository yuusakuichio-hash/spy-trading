# pytest 全件ベースライン 2026-04-25 10:01 JST (fork session)

## 結果サマリ

| 指標 | 件数 |
|---|---|
| **passed** | **5964** |
| **failed** | **224** |
| skipped | 9 |
| xfailed | 15 |
| warnings | 113 |
| 所要時間 | 138.43s (2:18) |

`/opt/homebrew/bin/python3 -m pytest tests/ --tb=line -q` (Python 3.14.3 / pytest 9.0.3)

## collection error 2 件 (--ignore 除外)

| ファイル | 原因 |
|---|---|
| `tests/test_gmail_notify_improve.py` | `gmail_monitor.py` が `/var/log/spx_bot` を mkdir 試行 → PermissionError (既存 code 問題・本 fork で改変不可) |
| `tests/test_morning_digest_auth_budget_20260425.py` | `scripts/morning_digest_send.py` から `_auth_next_reset_jst` import 失敗・**Medium #6 agent (ac6a3111c) の報告と実態が乖離** (15/15 PASS と報告したが関数未追加) |

## failed 224 件 カテゴリ別 top 15

| 件数 | ファイル | passed/total |
|---|---|---|
| 43 | tests/test_chronos_phase_d_20260420.py | 86/129 |
| 34 | tests/test_tradovate_auth.py | 66/100 |
| 33 | tests/test_chronos_webhook.py | 66/99 |
| 11 | tests/test_macro_indicators_20260425.py | 20/31 |
| 10 | tests/test_executor_sync_only_guard.py | 20/30 |
| 9 | tests/test_margin_monitor_20260425.py | 18/27 |
| 9 | tests/test_executor_sync_only_guard_redteam.py | 18/27 |
| 6 | tests/test_mffu_dry_run_guard.py | 12/18 |
| 5 | tests/test_half_day_guard_20260425.py | 10/15 |
| 5 | tests/test_engine_wiring_20260425.py | 10/15 |
| 5 | tests/test_drawdown_tracker_20260425.py | 10/15 |
| 5 | tests/test_consecutive_loss_guard_20260425.py | 10/15 |
| 5 | tests/test_adaptive_monitor_interval_20260425.py | 10/15 |
| 4 | tests/test_vol_target_sizer_20260425.py | 8/12 |
| 4 | tests/test_gamma_scalp_dynamic_20260425.py | 8/12 |

合計上位 15 件で 188 件 failed (84%)。残 36 件は分散。

## 重要な事実

1. **「Phase 2 Engine 9/9 native 移植 505/505 PASS」は atlas_v3 サブセットのみ評価**。tests/ 全体では 224 件 failed が存在。
2. **Medium #6 agent の虚偽完了**: `scripts/morning_digest_send.py` への `_auth_next_reset_jst` 等の追加が実際にはされておらず、test 側 import 失敗。
3. chronos 系テスト 76 件 failed (phase_d 43 + webhook 33) — 月曜は Atlas/SPY が主体だが chronos も並走必要なら paper 開始前に対処要。
4. tradovate_auth 34 件 failed — TraderPost forwarder 経路に影響の可能性。
5. 直近実装系 (margin_monitor / drawdown_tracker / adaptive_monitor_interval / vol_target_sizer / consecutive_loss_guard / engine_wiring / half_day_guard 等) が**半数以上 fail** = 直近 atlas_v3 ops 拡張の test 整合性に課題あり。

## 月曜 paper 開始 (2026-04-27 22:30 JST) への影響評価

| 影響度 | 内容 |
|---|---|
| **致命** | engine_wiring (5/15 fail) — Knight Capital wiring の検証 test が未通過 |
| **高** | margin_monitor / drawdown_tracker / adaptive_monitor_interval — 監視層の test 失敗 |
| **中** | tradovate_auth / chronos_phase_d — Atlas paper には直接非影響だが chronos が動くと致命 |
| **低** | mffu_dry_run_guard / executor_sync_only_guard — guard 系の警告レベル |

## 次セッションでの優先対応 (fork session 範囲外)

1. Medium #6 agent の `scripts/morning_digest_send.py` に `_auth_next_reset_jst` / `_auth_recent_failures` 関数を追加 (or test を skip mark)
2. engine_wiring test 5 件 failed の原因分析 → 修正 (Knight Capital fix の検証 gap)
3. atlas_v3 ops 系 6 ファイル合計 32 件 failed の原因分析・修正
4. Redteam が指摘した CRITICAL fail-open 6 件の修正 (前 turn で agent a2c8f420 が limit hit で死亡・要再投入)
5. 224 件中 atlas paper に直接影響する件のみ paper 開始前に修正
6. chronos 76 件 failed は Atlas paper 後に対応 (chronos 並走のタイミング判断必要)

## 検証ログ
- `/Users/yuusakuichio/trading/data/ops/pytest_baseline_20260425_100057.log` (1524 行)

## 既知の制約 (fork session)
- ADR-015 判断待ち事項 触らない
- 既存コード (spy_bot.py / chronos_bot.py / common/* / 等) 改変禁止
- Agent 月次 limit 到達 → 並列 agent 投入不可 (前 turn で a97945e5 / a2c8f420 が死亡)
- 直接実装は 1 task 完結のみ
