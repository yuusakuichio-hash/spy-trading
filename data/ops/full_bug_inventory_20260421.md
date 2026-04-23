# Full Bug Inventory — 2026-04-21
生成: 実ログ・pytestの失敗・コード解析に基づく。推測なし。

---

## サマリー

| severity | 件数 |
|---|---|
| CRITICAL | 8 |
| HIGH | 9 |
| MEDIUM | 5 |
| LOW | 3 |
| **合計** | **25** |

---

## CRITICAL (8件)

### BUG-001
- **severity**: CRITICAL
- **場所**: `common/pushover_client.py:669` + `tests/test_pushover_client.py` (全テスト)
- **再現条件**: テスト実行時刻がJST 22:00-4:00 (現在常時該当)
- **症状**: `_is_quiet_hours()` が True を返し `LEVEL_CRITICAL` でも HTTP送信をスキップしてモーニングキューへ保留。7テストが失敗 (`test_1_send_success`, `test_2_single_429_no_backoff`, `test_3`, `test_4`, `test_5`, `test_success_resets_consecutive_counter`, `test_token_override`)
- **影響**: テストが全て偽PASSになる時間帯が存在する。本番では夜間CRITICAL通知が届かず機会損失・損失拡大。
- **修正提案**: `_setup_paths()` 内で `pc._is_quiet_hours = lambda: False` をモンキーパッチ。teardown で復元。
- **工数**: 5分

### BUG-002
- **severity**: CRITICAL
- **場所**: `tests/test_pushover_dedup.py` 全4テスト
- **再現条件**: JST 22:00-4:00 (BUG-001と同根)
- **症状**: `dedup_suppresses_second_send` / `dedup_skipped_for_priority2` / `dedup_expires_after_one_hour` / `dedup_works_in_batched_level` が全て失敗。送信前に quiet_hours ガードで返るため dedup ロジックに到達しない。
- **影響**: dedup 機能の回帰テストが無効化されている
- **修正提案**: BUG-001と同様に `_is_quiet_hours` をモック
- **工数**: 5分 (BUG-001と同時修正可)

### BUG-003
- **severity**: CRITICAL
- **場所**: `tests/test_pushover_priority_protection.py::test_p2_bypasses_dedup`
- **再現条件**: JST 22:00-4:00 (BUG-001と同根)
- **症状**: priority=2がdedupをバイパスすることを確認するテストが、quiet_hoursに先に引っかかって失敗
- **影響**: priority=2の緊急通知保護が検証されない
- **修正提案**: BUG-001と同様
- **工数**: BUG-001と同時

### BUG-004
- **severity**: CRITICAL
- **場所**: `scripts/morning_digest_send.py:93`
- **再現条件**: import時常時
- **症状**: `SyntaxError: unterminated string literal` — 行93に未終端文字列リテラル `return "認証試行:`
- **影響**: morning_digest_send.py が import 不可。テスト3件失敗 (`test_categorization_auto_fix`, `test_empty_queue_returns_true`, `test_sends_digest_and_clears_queue`)。朝のダイジェスト送信が毎日クラッシュ。
- **修正提案**: L93の文字列を閉じる (`"` 追加または f-string化)
- **工数**: 2分

### BUG-005
- **severity**: CRITICAL
- **場所**: `common/chronos_traderspost_forwarder.py:380-413`
- **再現条件**: `chronos_traderspost_routing.yaml` が存在し `strategy_id="chronos_test"` のシグナルが来た時
- **症状**: `FirmEnforcer` が routing.yaml に `chronos_test` strategy が存在しないため全シグナルを BLOCK。テスト6件失敗。本番では `chronos_orb_mes_demo` 以外の全 strategy が発注不可能になる。
- **影響**: テスト用 strategy_id が routing.yaml に未登録。実装とテストのスキーマ不整合。
- **修正提案**: テスト側で `strategy_id="chronos_orb_mes_demo"` を使うか、テスト fixture でモック routing を注入する。
- **工数**: 15分

### BUG-006
- **severity**: CRITICAL
- **場所**: `common/pdt_tracker.py:245-249` + `tests/test_pdt_1dte_handling.py`
- **再現条件**: テスト実行日が2026-04-21 (テストデータの日付2026-04-14から8日以上経過)
- **症状**: `count_day_trades_rolling()` が `reference=None` の時「今日のET日付」からローリング5営業日を計算。テストデータの日付(2026-04-14)が窓外になりカウント=0になる。`test_0dte_blocked_when_pdt_exhausted` / `test_strategy_selector_0dte_to_1dte_fallback` / `test_0dte_passes_when_pdt_remaining` / `test_strategy_selector_no_trade_for_unsupported_1dte` / `test_check_pdt_layer_0dte_exhausted_denies` の5テストが失敗。
- **影響**: PDT制御の根幹ロジックのテストが無効化されている。本番でPDTブロックが効かない可能性。
- **修正提案**: テスト側で `check_pdt_layer(..., now_et=_make_et("2026-04-14",...))` を引数に渡すか、`pdt_tracker.count_day_trades_rolling(reference=today)` を明示的に使うよう修正。
- **工数**: 20分

### BUG-007
- **severity**: CRITICAL
- **場所**: `sora_heartbeat_monitor.py:296-329` (`handle_stale` 関数)
- **再現条件**: `_kickstart` 成功かつ `attempt > threshold` 条件が同時に成立する場合 (閾値=1、attempt=2 で初回は threshold 超過→emergency→returnでリセット前に抜ける)
- **症状**: `test_successful_kickstart_resets_counter` FAILED。`attempt > threshold` 時は early return するためカウンタがリセットされない。attempt=2 (previous=1 + 今回+1) で threshold=1 を超え emergency パスへ進み `_restart_attempts[component]=0` に到達しない。
- **影響**: kickstart成功しても `_restart_attempts` が増加し続けるため、次の stale 検知で emergency 誤発報。
- **修正提案**: L300の `if attempt > threshold:` ブランチでも `_kickstart` 成功時は `_restart_attempts[component]=0` を実行する。
- **工数**: 5分

### BUG-008
- **severity**: CRITICAL
- **場所**: `common/earnings_engine.py:205-210` (`get_today_candidates` 関数)
- **再現条件**: `_fetch_earnings_calendar` のモックで `date=today_et()` のデータを返す場合でも `_now_et()` の戻り値が実際の今日(2026-04-21)になるとフィルタで除外される可能性。テスト `test_filters_by_date` FAILED。
- **症状**: `today = now_et.date()` がテスト実行時の実際の今日を返すため、モックで設定した `today` 文字列と一致せず NVDA が candidates に含まれない。
- **影響**: 決算トレードの候補銘柄が毎日正しく取得できないリスク。
- **修正提案**: テストで `_now_et` をモックして固定日付を返すようにする。
- **工数**: 10分

---

## HIGH (9件)

### BUG-009
- **severity**: HIGH
- **場所**: `spy_bot.py:5196` (`close_all_positions` 内)
- **再現条件**: `code` に期限切れ日付が含まれる場合 (US.SPY260420P00550000)
- **症状**: `test_short_leg_failure_sets_pending_close_and_returns_false` FAILED。期限切れオプション (`_option_is_expired` が True) は `close_all_positions` 内でスキップされ `place_order` が呼ばれない。従って SHORT buyback 失敗を模擬しても `result=True` が返る。
- **影響**: 本番でショートレグの決済が静かにスキップされる。_pending_close への記録・False返却も発生しない。
- **修正提案**: テストで期限切れにならない日付のコード (US.SPY261231P00550000) を使うか、`_option_is_expired` のモックを追加する。または期限切れスキップ時も False を返すよう修正。
- **工数**: 15分

### BUG-010
- **severity**: HIGH
- **場所**: `data/logs/atlas_watchdog_stdout.log` / `atlas_watchdog.py`
- **再現条件**: `com.atlas.agent` launchd サービスが未登録 (2026-04-20以降継続)
- **症状**: `[RECOVERY] attempt=N: 回復不可 — 人間介入要求 service=com.atlas.agent` が2026-04-20〜2026-04-21に40+回以上繰り返されている。launchctl kickstart が rc=113 で失敗。
- **影響**: atlas_agent の自動復旧が全て失敗。夜間を含め長時間停止している可能性あり。
- **修正提案**: `launchctl bootstrap gui/501 /path/to/com.atlas.agent.plist` でサービス登録。または COMPONENT_LAUNCHD_LABEL を正しいラベルに修正。
- **工数**: 10分

### BUG-011
- **severity**: HIGH
- **場所**: `data/logs/chronos_watchdog_stdout.log`
- **再現条件**: `com.soralab.chronos_agent` サービス停止状態 (2026-04-20 07:11〜継続)
- **症状**: `attempt=N: 回復不可` が2026-04-20に70+回以上継続。chronos_agent が長時間停止。
- **影響**: Chronos 本番取引が全停止している可能性。
- **修正提案**: BUG-010と同様にサービス再登録。
- **工数**: 10分

### BUG-012
- **severity**: HIGH
- **場所**: `data/ops/unified_status.md` — Pushover BAN
- **再現条件**: 現在進行中
- **症状**: Pushover が `{ status: 0, ip: "banned" }` を返し続けている。queue=197件が蓄積中。atlas_watchdog_stdout.log に定期的に `HTTP 429: banned` が記録されている。
- **影響**: 全Pushover通知が届かない。緊急通知も含めてサイレント状態。
- **修正提案**: Pushover管理コンソールでIP/トークンのBAN解除。または別トークンへのフォールバック。
- **工数**: 5分 (手動操作)

### BUG-013
- **severity**: HIGH
- **場所**: `common/pdt_tracker.py` — `count_day_trades_rolling` のデフォルト reference
- **再現条件**: テスト実行が記録日付から5営業日以上後
- **症状**: BUG-006の根本原因。`reference=None` 時に `datetime.datetime.now(ET).date()` を使う設計のため、テストが時間経過で自然に壊れる。
- **影響**: 全PDTテストが時限爆弾。新しくテストを書いても同様に壊れる。
- **修正提案**: `check_pdt_layer` に `now_et` 引数があるが `pdt_tracker.count_day_trades_rolling` に渡していない。 `pdt_tracker.remaining_allowed` も `now_et` を引数に取り `count_day_trades_rolling(reference=now_et.date())` を呼ぶよう修正。
- **工数**: 30分

### BUG-014
- **severity**: HIGH
- **場所**: `chronos_traderspost_forwarder.py` テストフィクスチャ
- **再現条件**: `chronos_traderspost_routing.yaml` が存在する全テスト環境
- **症状**: BUG-005の根本原因。テストが `_make_queue_row(signal_id=..., strategy_id="chronos_test")` を使っているが `routing.yaml` に `chronos_test` が未登録。`FirmEnforcer` が全シグナルをブロックし本来のロジックが一切実行されない。
- **影響**: TradersPost連携ロジック (retry, kill_switch, exec_log) が全く検証されていない。
- **修正提案**: テストで `chronos_orb_mes_demo` を使うか、`FirmEnforcer` をモックして `check()` が pass を返すようにする。
- **工数**: 20分

### BUG-015
- **severity**: HIGH
- **場所**: `data/ops/unified_status.md` — `heartbeat_monitor` no_hb, dead_man_switch stale
- **再現条件**: 現在
- **症状**: `heartbeat_monitor: no_hb / no_log` — heartbeat_monitor 自体が停止中またはHB未書込み。`dead_man_switch: stale (10分)` — デッドマンスイッチが10分間更新されていない。
- **影響**: heartbeat_monitor が停止するとatlas_agent/chronos_agentのstaleness検知が機能しない。
- **修正提案**: heartbeat_monitor の LaunchAgent 登録確認・再起動。
- **工数**: 10分

### BUG-016
- **severity**: HIGH
- **場所**: `data/logs/heartbeat_monitor.log` — launchd ラベル不整合
- **再現条件**: 常時
- **症状**: heartbeat_monitor が `com.soralab.atlas_agent` → 失敗 (旧ラベル) と `com.atlas.agent` → 失敗 (新ラベル) の両方で kickstart を試みている。どちらも rc=113で失敗。
- **影響**: launchd ラベルが `COMPONENT_LAUNCHD_LABEL` dict と実際の plist で一致していない。
- **修正提案**: `sora_heartbeat_monitor.py` の `COMPONENT_LAUNCHD_LABEL` と実 plist のラベルを統一。
- **工数**: 15分

### BUG-017
- **severity**: HIGH
- **場所**: `data/pending_completions.jsonl`
- **再現条件**: 常時
- **症状**: `project_tradovate_pcaptcha_fix_20260421.md` が `resolved=false` のまま deadline (2026-04-21T13:50) を過ぎている。
- **影響**: Tradovate CAPTCHA修正が未完了のまま放置されている可能性。
- **修正提案**: 対象メモリファイルを確認し resolved=true に更新するか、再実施。
- **工数**: 調査に30分

---

## MEDIUM (5件)

### BUG-018
- **severity**: MEDIUM
- **場所**: `common/orderflow_analysis.py:459`
- **再現条件**: `get_orderflow_signal` 呼び出し時常時
- **症状**: `datetime.datetime.utcnow()` (deprecated in Python 3.12+) を使用。`DeprecationWarning` が毎回出力される。
- **影響**: Python 3.16で削除予定。テスト実行時に警告が多発。
- **修正提案**: `datetime.datetime.now(datetime.UTC)` に変更。
- **工数**: 5分

### BUG-019
- **severity**: MEDIUM
- **場所**: `data/logs/atlas_watchdog_stdout.log` — `[STRADDLE_BUY] オプションチェーン取得失敗`
- **再現条件**: ペーパー稼働中 (2026-04-21 03:29〜継続)
- **症状**: STRADDLE_BUY がオプションチェーン取得に毎回失敗。10回/バッチで Watchdog がエラー検知しているが自動回復なし。
- **影響**: STRADDLE_BUY 戦術が実質無効状態。ペーパーでの検証ができていない。
- **修正提案**: `spy_bot.py` のオプションチェーン取得ロジックのフェイルオーバー確認・修正。
- **工数**: 30分

### BUG-020
- **severity**: MEDIUM
- **場所**: `common/pushover_client.py` — quiet_hours 中の `LEVEL_CRITICAL` 扱い
- **再現条件**: JST 22:00-4:00 に非夜間緊急の `LEVEL_CRITICAL` 送信
- **症状**: `_is_night_emergency()` が False の場合、`LEVEL_CRITICAL` 指定でも `_enqueue_morning_digest` に回される。テスト設計の前提（CRITICAL=即時送信）と実装が乖離している。
- **影響**: 夜間の CRITICAL 通知（例: 大損失）が朝まで届かない。
- **修正提案**: `LEVEL_CRITICAL` は quiet_hours でも常に即時送信するよう修正。現在の `_is_night_emergency` チェックを `LEVEL_CRITICAL` は skip する。
- **工数**: 10分

### BUG-021
- **severity**: MEDIUM
- **場所**: `data/logs/atlas_watchdog_stdout.log` — `Quote context 死活確認失敗`
- **再現条件**: 2026-04-21 03:45 頃
- **症状**: Quote context (OpenD接続) の死活確認が1回失敗している。その後回復しているが記録は残っている。
- **影響**: 短時間のデータ欠落が発生した可能性。
- **修正提案**: フェイルオーバー Level 0→1 の自動切替が動作したか確認。
- **工数**: 調査10分

### BUG-022
- **severity**: MEDIUM
- **場所**: `data/logs/apex_bot.log`
- **再現条件**: apex_bot 起動時
- **症状**: `[TradovateClient] authenticate failed: Invalid credentials` および `place_order: not authenticated` (2026-04-17)。
- **影響**: Apex bot が認証失敗で取引できていない。
- **修正提案**: Tradovate認証情報の確認・更新。BUG-017のCAPTCHA修正と関連。
- **工数**: 調査20分

---

## LOW (3件)

### BUG-023
- **severity**: LOW
- **場所**: `futu/common/pb/Qot_Common_pb2.py` (外部ライブラリ)
- **再現条件**: pytest実行時常時
- **症状**: `DeprecationWarning: Call to deprecated create function EnumDescriptor()` が大量出力（100+件）
- **影響**: テスト出力ノイズ。外部ライブラリのため直接修正不可。
- **修正提案**: `pytest.ini` に `filterwarnings = ignore::DeprecationWarning:futu` を追加
- **工数**: 2分

### BUG-024
- **severity**: LOW
- **場所**: `tests/test_chronos_intraday_monitor.py:289`
- **再現条件**: pytest実行時
- **症状**: `asyncio.iscoroutinefunction` deprecated (Python 3.16 で削除予定)
- **影響**: 将来のPython更新でテスト壊れる
- **修正提案**: `inspect.iscoroutinefunction()` に変更
- **工数**: 2分

### BUG-025
- **severity**: LOW
- **場所**: `tests/test_chronos_webhook.py`
- **再現条件**: pytest実行時
- **症状**: `HTTP_422_UNPROCESSABLE_ENTITY` deprecated (FastAPI)
- **影響**: FastAPI更新でテスト壊れる
- **修正提案**: `HTTP_422_UNPROCESSABLE_CONTENT` に変更
- **工数**: 2分

---

## 修正優先順位

| 優先度 | BUG-ID | 理由 |
|---|---|---|
| P0 即時 | BUG-004 | SyntaxErrorで毎朝ダイジェスト送信クラッシュ |
| P0 即時 | BUG-012 | Pushover BAN中・緊急通知全停止 |
| P0 即時 | BUG-010, BUG-011 | atlas_agent/chronos_agent 自動復旧失敗継続 |
| P0 即時 | BUG-016 | launchd ラベル不整合・heartbeat kickstart全失敗 |
| P1 本番前 | BUG-001〜003 | テスト時間帯で全Pushoverテスト無効化 |
| P1 本番前 | BUG-005, BUG-014 | TradersPost forwarder テスト全無効 |
| P1 本番前 | BUG-006, BUG-013 | PDTテスト時限爆弾（既に全失敗） |
| P1 本番前 | BUG-007 | heartbeat kickstart成功でもカウンタ増加 |
| P1 本番前 | BUG-008 | earnings_engine テスト日付依存 |
| P1 本番前 | BUG-009 | close_all_positions 期限切れスキップ時False未返却 |
| P2 早期 | BUG-020 | LEVEL_CRITICAL が夜間キューに入る設計欠陥 |
| P2 早期 | BUG-019 | STRADDLE_BUY 連続失敗・戦術無効 |
| P3 計画的 | BUG-015, BUG-017 | heartbeat_monitor停止・pending未解決 |
| P4 技術的負債 | BUG-018, BUG-022〜025 | deprecation警告・外部認証失敗 |

---

*生成方法: pytest --tb=long 全テスト実行 + data/logs/ ERRORgrep + コード静的解析 + pending_completions.jsonl確認*
