# Chronos Webhook 接続情報 (2026-04-21)

## Webhook URL

```
https://difference-after-tend-patterns.trycloudflare.com/chronos/signal
```

> 注意: trycloudflare.com のURLは VPS reboot や chronos_cloudflared.service 再起動で変わります。
> 変わった場合は `grep -o 'https://.*trycloudflare.com' /root/logs/chronos_cloudflared.log | tail -1` で確認。

## HMAC Secret (Pine Script の `webhook_secret` input に入れる値)

```
218f45b8762f8176f7d5568f7bcab9eb11fb76fee99242cb309a628ed276ec65
```

保管場所: VPS `/root/spxbot/.env` の `CHRONOS_WEBHOOK_SECRET`

## 動作確認 curl コマンド

### health check (IP制限なし)
```bash
curl -s -X POST https://difference-after-tend-patterns.trycloudflare.com/chronos/health
# 期待値: {"status":"ok","version":"2.0.0"}
```

### 有効シグナル (VPS内部から実行・TradingView IP偽装)
```bash
ssh -i ~/.ssh/deploy_key root@198.13.37.17
python3 - << 'EOF'
import json, urllib.request, time
SECRET = "218f45b8762f8176f7d5568f7bcab9eb11fb76fee99242cb309a628ed276ec65"
TS = int(time.time())
payload = json.dumps({
    "timestamp": TS,
    "nonce": f"test-{TS}",
    "symbol": "MES",
    "action": "BUY",
    "qty": 1,
    "strategy_id": "manual_test",
    "hmac": SECRET
}).encode()
req = urllib.request.Request(
    "http://localhost:8765/chronos/signal",
    data=payload,
    headers={"Content-Type": "application/json", "X-Forwarded-For": "52.89.214.238"},
    method="POST"
)
with urllib.request.urlopen(req) as r:
    print(r.status, r.read().decode())
EOF
# 期待値: 200 {"status":"accepted","signal_id":"..."}
```

## TradingView Alert Message テンプレート

TradingView の Alert > Message フィールドに以下を貼る:

```json
{
  "timestamp": {{timenow}},
  "nonce": "{{strategy.order.id}}-{{timenow}}",
  "symbol": "MES",
  "action": "{{strategy.order.action}}",
  "qty": {{strategy.position_size}},
  "strategy_id": "chronos_v1",
  "hmac": "218f45b8762f8176f7d5568f7bcab9eb11fb76fee99242cb309a628ed276ec65"
}
```

> `action` は TradingView から `buy` または `sell` で来ます。
> サーバーは `BUY`/`SELL`/`CLOSE` を要求するため、
> Pine Script 側で `str.upper()` するか、strategy.order.action を大文字化してください。

## TradingView Alert 設定時の注意点3つ

1. **`hmac` フィールドに secret を平文で埋め込む (pine_compat モード)**
   - TradingView はカスタムヘッダーが送れないため、本サーバーは `HMAC_PINE_MODE=pine_compat` で起動しています。
   - `hmac` フィールドの値がサーバー側の `CHRONOS_WEBHOOK_SECRET` と一致するかどうかを `hmac.compare_digest` で検証します。
   - secret は Alert Message の JSON に直接書いてください。

2. **Webhook URL は HTTPS 必須・HTTP 不可**
   - TradingView は HTTPS の URL しか受け付けません。
   - 現在の URL: `https://difference-after-tend-patterns.trycloudflare.com/chronos/signal`
   - URL が変わった場合は Alert を再設定する必要があります。

3. **`symbol` フィールドは `MES`/`MNQ`/`ES`/`NQ` のいずれかに固定**
   - それ以外の値はスキーマエラー(422)で拒否されます。
   - `qty` は 1〜5 の整数のみ有効 (MFFU Flex $50K Eval の上限)。

## VPS サービス状態確認

```bash
ssh -i ~/.ssh/deploy_key root@198.13.37.17
systemctl status chronos_webhook.service      # uvicorn プロセス
systemctl status chronos_cloudflared.service  # Cloudflare Tunnel
journalctl -u chronos_webhook -n 30           # ログ確認
cat /root/spxbot/data/chronos_webhook_signals.jsonl  # 受信シグナル一覧
```
