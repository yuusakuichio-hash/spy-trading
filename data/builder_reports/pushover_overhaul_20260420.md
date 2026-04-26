# Pushover 通知抜本改修 完了報告

実施日: 2026-04-20
担当: builder (claude-sonnet-4-6)
作業時間目安: 2-3時間

---

## 作業内容サマリー

### 変更ファイル

| ファイル | 変更内容 |
|---------|---------|
| common/pushover_client.py | ゲートレイヤー(3レベル)・バッチキュー機構・send_silent/send_batched/send_critical追加 |
| atlas_agent.py | pushover()にlevelパラメータ追加・Level1 INFO→SILENT・BOOT→SILENT・Level2 AUTOFIX→BATCHED |
| chronos_agent.py | pushover()/pushover_alert()にゲートレイヤー組み込み・起動→SILENT |
| atlas_watchdog.py | pushover_send()にゲートレイヤー・回復不可→CRITICAL・起動→SILENT |
| chronos_watchdog.py | pushover_send()にゲートレイヤー・CRITICAL/MFFU/KillSwitch→CRITICAL |
| sora_heartbeat_monitor.py | pushover()にゲートレイヤー・EMERGENCY→CRITICAL・STALE初回→BATCHED |
| morning_briefing.py | 全送信をBATCHED（デイリーサマリー報告は即断不要） |

### 新規ファイル

| ファイル | 内容 |
|---------|------|
| pushover_batch_flush.py | バッチフラッシュデーモン本体 |
| ~/Library/LaunchAgents/com.sora.pushover_batch_flush.plist | 30分毎自動実行のLaunchAgent |
| data/notification_policy_design.md | 脳科学ベース設計書 |

---

## 通知レベル定義

| レベル | 定数 | 動作 |
|--------|------|------|
| silent | LEVEL_SILENT | ログのみ。Pushover送信なし |
| batched | LEVEL_BATCHED | pushover_batch_queue.jsonlに追記。30分毎まとめ送信 |
| critical | LEVEL_CRITICAL | 即時送信。4系統のみ |

## Critical 4系統

1. 資金毀損: 日次損失上限超過・証拠金追加・Level4 HALT
2. アカウント停止: KillSwitch・MFFU違反・Tradovate接続断
3. 本番異常: 自己回復3回失敗・乖離検知停止フラグ・Level4 HALT
4. 市場機会喪失: Bot停止(場中)・Level3+ 承認要求

---

## 通知件数改善予測

| 区分 | 改修前 | 改修後 |
|------|--------|--------|
| 起動通知（各サービス） | 5-8件/日 | 0件 |
| Level1 INFO | 10-20件/日 | 0件 |
| Level2 AUTOFIX | 5-10件/日 | バッチ1件 |
| 監視系BATCHED | 多数 | バッチ1-2件/30分 |
| Critical（4系統） | 埋もれる | 1日0-5件のみ |
| **合計** | **30-50件** | **5件以下** |

---

## テスト結果

### ドライテスト（全PASS）

```
ALL DRY TESTS PASSED
- silent → ログのみ (True返却)
- batched → キュー追記 (True返却)
- send_silent → ログのみ
- send_batched → キュー追記
- classify_level(is_fund_loss=True) → LEVEL_CRITICAL
- classify_level() → LEVEL_BATCHED
- batch_queue_count確認
```

### Redteamテスト（全PASS）

Atlas level判定: 8/8件 OK
Chronos level判定: 7/7件 OK
flush_batch_queue動作確認: OK

### LaunchAgent確認

```
com.sora.pushover_batch_flush: loaded (StartInterval=1800)
```

---

## 既知の制限

- 旧来スクリプト（spy_bot.py・spx_bot.py・chronos_bot.py等）はまだ独自pushover()を持つ
  これらはアクティブなペーパー稼働スクリプトで、変更時に全回帰テストが必要。
  本改修のスコープ外（アクティブな5ファイルに絞った）。
- バッチフラッシュは token/user 環境変数が設定されている本番環境でのみ実際に送信する。

---

## 設計書参照先

data/notification_policy_design.md
- Decision Fatigue / Attention Residue / Ultradian Rhythm の根拠
- 通知レベル定義・Critical 4系統
- 通知件数目標

---

完了報告はbatched扱い（判断不要）のためPushover通知なし。
