# 外部死活監視 セットアップ手順書

## 背景

2026-04-20 Pushover IP ban 事案で全通知経路が死亡した。
本手順書に従い **UptimeRobot + Healthchecks.io の2段構成** を構築することで、
Pushover と完全独立した外部監視を実現する。

```
[Bot メインループ]
    ↓ 2-5分毎に ping
[Healthchecks.io]          ← Tier 2 保険（Bot側から能動的に報告）
    ↓ ping 届かない時に通知
[Email / SMS]

[UptimeRobot]              ← Tier 1 主監視（外部から能動的に確認）
    ↓ 5分毎に /health をチェック
[health_server :8080]
    ↓ 問題あり（503）
[Email / SMS]
```

---

## Step 1: Healthchecks.io アカウント作成

### 1-1. アカウント登録

1. https://healthchecks.io にアクセス
2. 右上 "Sign up" をクリック
3. メールアドレスとパスワードを入力して登録
4. 認証メールのリンクをクリック

**料金:** 無料プランで 20チェックまで作成可能。今回は最大9チェック使用。

### 1-2. チェック（監視項目）を作成する

ダッシュボード右上の "Add Check" ボタンをクリック。
以下の設定で **計9個** のチェックを作成する。

| チェック名              | Period (期間) | Grace (猶予) | 対応Bot       |
|------------------------|--------------|-------------|--------------|
| chronos_agent          | 5 minutes    | 5 minutes   | Chronos CME先物 |
| chronos_watchdog       | 10 minutes   | 5 minutes   | Chronos CME先物 |
| chronos_bot            | 10 minutes   | 5 minutes   | Chronos CME先物 |
| atlas_agent            | 5 minutes    | 5 minutes   | Atlas SPXオプション |
| atlas_watchdog         | 10 minutes   | 5 minutes   | Atlas SPXオプション |
| spy_bot                | 10 minutes   | 5 minutes   | Atlas SPXオプション |
| sora_heartbeat_monitor | 10 minutes   | 5 minutes   | 共通監視インフラ |
| health_aggregator      | 15 minutes   | 5 minutes   | 集約ping      |

**設定値の意味:**
- Period: Botからこの間隔以内に ping が来なければ障害とみなす
- Grace: Period 経過後、さらにこの時間猶予を与えてから通知する

### 1-3. UUID の取得

作成したチェックをクリックして詳細画面を開く。
"Ping URL" に表示されている URL の末尾が UUID:
```
https://hc-ping.com/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                    これが UUID
```

各チェックのUUIDをメモしておく。

### 1-4. 通知先の設定（Email / SMS）

1. ダッシュボード右上のユーザーアイコン → "Integrations"
2. "Email" をクリックして通知先メールアドレスを追加
3. SMS を使う場合は "PagerDuty" または "Twilio" 経由で設定
   - 日本番号 (+81) は Twilio が対応

---

## Step 2: .env に UUID を設定する

`/Users/yuusakuichio/trading/.env` を開き、取得したUUIDを入力する:

```bash
# Chronos: CME先物Bot 系（Atlas と混同禁止）
HC_UUID_CHRONOS_AGENT=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
HC_UUID_CHRONOS_WATCHDOG=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
HC_UUID_CHRONOS_BOT=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Atlas: SPXオプションBot 系（Chronos と混同禁止）
HC_UUID_ATLAS_AGENT=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
HC_UUID_ATLAS_WATCHDOG=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
HC_UUID_SPY_BOT=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# 共通監視インフラ
HC_UUID_SORA_HEARTBEAT_MONITOR=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
HC_UUID_HEALTH_AGGREGATOR=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

設定確認（UUIDが読み込まれているか）:
```bash
cd /Users/yuusakuichio/trading
python3 -m common.external_health_ping --list
```

出力例:
```
コンポーネント UUID 設定状況:
  [OK] chronos_agent          env: HC_UUID_CHRONOS_AGENT
  [OK] chronos_watchdog       env: HC_UUID_CHRONOS_WATCHDOG
  [未設定] chronos_bot         env: HC_UUID_CHRONOS_BOT
  ...
```

---

## Step 3: UptimeRobot アカウント作成

### 3-1. アカウント登録

1. https://uptimerobot.com にアクセス
2. "Register for FREE" をクリック
3. メールアドレスとパスワードで登録
4. 認証メールのリンクをクリック

**料金:** 無料プランで50監視・5分毎チェック可能。

### 3-2. Monitor 追加

ダッシュボード左上 "+ Add New Monitor" をクリック。

**設定値:**

| 項目 | 値 |
|---|---|
| Monitor Type | HTTP(s) |
| Friendly Name | Sora Lab Health Server |
| URL | http://198.13.37.17:8080/health |
| Monitoring Interval | 5 minutes |
| HTTP Method | GET |
| Expected Status Codes | 200 |

"Create Monitor" をクリックして保存。

### 3-3. 通知先の設定

1. ダッシュボード右上 "My Settings" → "Alert Contacts"
2. "+ Add Alert Contact" をクリック
3. Contact Type: Email → メールアドレスを入力
4. SMS（日本番号）: Type を "SMS" に変更 → 電話番号に "+81" 形式で入力

---

## Step 4: 集約 ping LaunchAgent の有効化

UUID を .env に設定した後:

```bash
# LaunchAgent をロード（UUID設定完了後に実行）
launchctl load /Users/yuusakuichio/trading/LaunchAgents/com.sora.external_health_check.plist

# 手動実行テスト
python3 /Users/yuusakuichio/trading/scripts/external_health_aggregator.py
```

出力例:
```
[OK]    chronos_agent: age=45s
[OK]    atlas_agent: age=32s
[STALE] atlas_watchdog: age=inf
[AGGREGATOR] FAIL: 1 stale components
```

---

## Step 5: 動作確認（検証テスト）

### 5-1. ping 送信テスト（各コンポーネント）

```bash
cd /Users/yuusakuichio/trading

# chronos_agent の ping 成功テスト
python3 -m common.external_health_ping chronos_agent --status success

# atlas_agent の fail ping テスト
python3 -m common.external_health_ping atlas_agent --status fail

# 全コンポーネントの設定確認
python3 -m common.external_health_ping --list
```

### 5-2. Healthchecks.io ダッシュボードで確認

- ping 送信後、ダッシュボードのチェックが "Up" になること
- fail 送信後、"Down" になること
- 5〜10分 ping しないと "Late" → "Down" になること

### 5-3. UptimeRobot ダッシュボードで確認

- /health エンドポイントが 200 を返していること
- health_server が起動していない場合は "Down" 通知が来ること

### 5-4. 疑似障害テスト（任意）

```bash
# health_server を停止してUptimeRobotが検知するか確認
# (テスト後は再起動すること)

# Healthchecks.io: ping を5〜10分停止して通知が来るか確認
```

---

## トラブルシューティング

### ping が送信されない

```bash
# UUID が設定されているか確認
python3 -m common.external_health_ping --list

# requests ライブラリが入っているか確認
python3 -c "import requests; print('OK')"
```

### UptimeRobot が /health に到達できない

- VPS (198.13.37.17) のポート 8080 が開いているか確認:
  ```bash
  ssh -i ~/.ssh/deploy_key root@198.13.37.17 "ss -tlnp | grep 8080"
  ```
- ファイアウォール設定（ufw / iptables）でポート 8080 を許可しているか確認

### Healthchecks.io が "Late" のまま

- Botのメインループで `ping_healthchecks()` が呼ばれているか確認
- `data/logs/` にエラーログがないか確認

---

## コンポーネント別 ping 頻度

| コンポーネント | ping 頻度 | HC チェック Period |
|---|---|---|
| chronos_agent | 2分毎 | 5分 |
| atlas_agent | 2分毎 | 5分 |
| chronos_watchdog | 5分毎 | 10分 |
| atlas_watchdog | 5分毎 | 10分 |
| sora_heartbeat_monitor | 5分毎 | 10分 |
| health_aggregator (集約) | 10分毎 | 15分 |

---

## 月額コスト

| サービス | プラン | 月額 |
|---|---|---|
| Healthchecks.io | 無料 (Hobby) | 0円 |
| UptimeRobot | 無料 | 0円 |
| 合計 | | **0円** |

---

*最終更新: 2026-04-20 / Sora Lab builder*
