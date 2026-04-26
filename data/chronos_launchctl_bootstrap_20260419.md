# Chronos LaunchAgent Bootstrap 手順書

作成: 2026-04-19 cycle2

## 概要

Chronos Bot (MFFU先物) を Mac の LaunchAgent として起動する手順。
全 plist を `launchctl bootstrap` でロードする順序と、エラー時のトラブルシューティングを明示する。

## 前提条件

- macOS (Ventura 以降推奨)
- Python 3.11+
- `~/Library/LaunchAgents/` に plist ファイルが配置済み
- `data/accounts/<account_id>/state.json` が事前に存在すること（後述）

## ロード順序

以下の順序でロードすること。依存関係あり。

### Step 1: 基盤サービス（依存なし・並列ロード可）

```bash
# user ID 取得
export UID_VAL=$(id -u)

# Cloudflared トンネル（外部通信基盤）
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.cloudflared.plist

# ntfy リスナー（コマンド受信）
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.ntfy_listener.plist

# webhook サーバー（ポート 9999）
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.webhook_server.plist
```

### Step 2: Chronos Bot（アカウント別）

MFFU_ACCOUNT_ID をアカウントごとに設定する。
5アカウント構成の場合は以下5回実行:

```bash
# アカウント A
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.chronos.bot.acc_a.plist

# アカウント B
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.chronos.bot.acc_b.plist

# （以下同様）
```

plist の EnvironmentVariables に MFFU_ACCOUNT_ID を設定すること:
```xml
<key>EnvironmentVariables</key>
<dict>
    <key>MFFU_ACCOUNT_ID</key>
    <string>acc_a</string>
</dict>
```

### Step 3: Chronos Agent（Bot 起動後）

Bot が pid.lock を生成してから Agent をロードする（起動後 10 秒待機推奨）:

```bash
sleep 10
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.chronos_agent.plist
```

### Step 4: Chronos Watchdog（Agent 起動後）

```bash
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.chronos_watchdog.plist
```

## state.json 事前作成手順

Bot 起動前に各アカウントの state.json を事前作成する:

```bash
# アカウントディレクトリ作成
mkdir -p data/accounts/acc_a
mkdir -p data/accounts/acc_b

# state.json 初期化（最小構成）
python3 -c "
import json, datetime, zoneinfo
state = {
    'account_id': 'acc_a',
    'timestamp': datetime.datetime.now(zoneinfo.ZoneInfo('America/New_York')).isoformat(),
    'save_reason': 'init',
    'account_type': 'evaluation',
    'positions': [],
    'weekly_dd_usd': 0.0,
    'daily_pnl_usd': 0.0,
    'consecutive_losses': 0,
    'best_single_day_profit_usd': 0.0,
    'total_profit_usd': 0.0,
    'winning_days_count': 0,
    'daily_trade_count': 0,
    'phase_flags': {
        'survival_mode': False,
        'kill_switch_day': False,
        'daily_halt': False,
        'daily_soft_stop_active': False,
        'news_window_violation': False,
    },
}
with open('data/accounts/acc_a/state.json', 'w') as f:
    json.dump(state, f, indent=2, ensure_ascii=False)
print('state.json created')
"
```

## 起動確認

```bash
# 全 LaunchAgent のステータス確認
launchctl list | grep -E "chronos|soralab"

# pid.lock 確認
ls data/accounts/*/pid.lock

# Bot プロセス確認
ps aux | grep chronos_bot

# Agent プロセス確認
ps aux | grep chronos_agent
```

## Chronos Watchdog LaunchAgent 化（OPS-2）

現在 PID 1065 で裸プロセス起動中の場合の移行手順:

```bash
# 1. 既存裸プロセスを停止
kill 1065

# 2. plist を配置（com.soralab.chronos_watchdog.plist が未作成の場合）
cat > ~/Library/LaunchAgents/com.soralab.chronos_watchdog.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.soralab.chronos_watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/python3</string>
        <string>/Users/yuusakuichio/trading/chronos_watchdog.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/yuusakuichio/trading</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/yuusakuichio/trading/data/logs/chronos_watchdog.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/yuusakuichio/trading/data/logs/chronos_watchdog.stderr.log</string>
</dict>
</plist>
EOF

# 3. bootstrap でロード
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.soralab.chronos_watchdog.plist

# 4. 起動確認
launchctl list | grep chronos_watchdog
```

## アンロード手順（Rollback）

```bash
export UID_VAL=$(id -u)
launchctl bootout gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.chronos_watchdog.plist
launchctl bootout gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.chronos_agent.plist
launchctl bootout gui/${UID_VAL} ~/Library/LaunchAgents/com.chronos.bot.acc_a.plist
# ... 各アカウント同様
```

## トラブルシューティング

### "already bootstrapped" エラー

```bash
# 一度 bootout してから再 bootstrap
launchctl bootout gui/${UID_VAL}/com.soralab.chronos_agent
launchctl bootstrap gui/${UID_VAL} ~/Library/LaunchAgents/com.soralab.chronos_agent.plist
```

### pid.lock が残っている場合

```bash
# プロセスが死んでいるのに pid.lock が残っている場合は削除
rm data/accounts/acc_a/pid.lock
```

### state.json がない場合の Agent エラー

Agent は state.json がなくても起動するが、Level2/3/4 チェックは空を返す。
上記「state.json 事前作成手順」で初期化してから Bot を起動すること。

## manual_halt 解除手順

Level4 HALT が発生した場合の復旧:

```bash
# 解除コマンド（chronos_agent.py --unhalt）
python3 /Users/yuusakuichio/trading/chronos_agent.py --unhalt
```

Pushover に解除通知が届き、次の監視サイクルから全チェックが再開される。
