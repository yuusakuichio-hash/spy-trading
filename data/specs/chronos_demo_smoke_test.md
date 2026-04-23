# Chronos Demo 疎通 smoke test (2026-04-21)

20時セッションで実行する具体的 curl コマンド。
Track A-0/A-1/B 完了後に実施。所要時間: 5分。

---

## 事前準備

```bash
# Demo secret を変数にセット (Track A-1 で生成した値)
DEMO_SECRET="<Track A-1 で生成した 64 文字 hex>"

# Webhook URL (VPS reboot で変わる場合は下記で再確認)
# ssh -i ~/.ssh/deploy_key root@198.13.37.17 \
#   "grep -o 'https://.*trycloudflare.com' /root/logs/chronos_cloudflared.log | tail -1"
WEBHOOK_URL="https://difference-after-tend-patterns.trycloudflare.com"
```

---

## Step 1: Health check

```bash
curl -s "${WEBHOOK_URL}/chronos/health"
```

期待値:
```json
{"status":"ok","version":"2.0.0"}
```

NG の場合: VPS で `systemctl status chronos_webhook.service` を確認。

---

## Step 2: 手動シグナル送信 (BUY 1 MES Demo)

Mac ローカルから送信 (TradingView IP チェックを bypass するため VPS 内部経由で実行):

```bash
ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no root@198.13.37.17 python3 - << 'PYEOF'
import json, urllib.request, time, os

SECRET = os.environ.get("CHRONOS_WEBHOOK_SECRET_DEMO", "REPLACE_ME")
TS = int(time.time())
NONCE = f"demo_smoke_test_{TS}"

payload = json.dumps({
    "timestamp": TS,
    "nonce": NONCE,
    "symbol": "MES",
    "action": "BUY",
    "qty": 1,
    "strategy_id": "chronos_orb_mes_demo",
    "account_id": "tradovate_demo",
    "hmac": SECRET
}).encode()

req = urllib.request.Request(
    "http://localhost:8765/chronos/signal",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-Forwarded-For": "52.89.214.238"   # TradingView IP 偽装
    },
    method="POST"
)
with urllib.request.urlopen(req, timeout=10) as r:
    print("HTTP:", r.status)
    print("Body:", r.read().decode())
PYEOF
```

期待値:
```
HTTP: 200
Body: {"status":"accepted","signal_id":"..."}
```

NG パターン:
- HTTP 401: Demo secret が .env の CHRONOS_WEBHOOK_SECRET_DEMO と不一致
- HTTP 422: JSON スキーマエラー (symbol/qty の値を確認)
- HTTP 403: IP allowlist ブロック (X-Forwarded-For ヘッダーを確認)

---

## Step 3: VPS 実行ログ確認

```bash
ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no root@198.13.37.17 \
  "tail -5 /root/spxbot/data/chronos_webhook_executions.jsonl"
```

確認ポイント:
- `"nonce"` フィールドが `"demo_smoke_test_"` で始まること
- `"strategy_id": "chronos_orb_mes_demo"` であること
- `"account_id": "tradovate_demo"` であること
- `"status": "executed"` または `"pending"` であること

---

## Step 4: Tradovate Demo UI 約定確認

1. https://trader.tradovate.com にログイン (Demo モード)
2. 「Positions」または「Orders」タブを開く
3. MES の Long ポジション 1枚が表示されていること

**表示されない場合**:
- Chronos が Tradovate Demo API に接続していない可能性
- VPS `.env` の `TRADOVATE_DEMO_USERNAME` / `TRADOVATE_DEMO_PASSWORD` を確認
- `journalctl -u chronos_webhook -n 50` でエラーログを確認

---

## Step 5: クリーンアップ (ポジション手動クローズ)

smoke test で建てたポジションは手動でクローズ:

```bash
ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no root@198.13.37.17 python3 - << 'PYEOF'
import json, urllib.request, time, os

SECRET = os.environ.get("CHRONOS_WEBHOOK_SECRET_DEMO", "REPLACE_ME")
TS = int(time.time())

payload = json.dumps({
    "timestamp": TS,
    "nonce": f"demo_smoke_close_{TS}",
    "symbol": "MES",
    "action": "CLOSE",
    "qty": 1,
    "strategy_id": "chronos_orb_mes_demo",
    "account_id": "tradovate_demo",
    "hmac": SECRET
}).encode()

req = urllib.request.Request(
    "http://localhost:8765/chronos/signal",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-Forwarded-For": "52.89.214.238"
    },
    method="POST"
)
with urllib.request.urlopen(req, timeout=10) as r:
    print("HTTP:", r.status)
    print("Body:", r.read().decode())
PYEOF
```

---

## smoke test 合格基準

| チェック項目 | 期待値 | 結果 |
|---|---|---|
| Health check | HTTP 200 + `{"status":"ok"}` | [ ] |
| BUY シグナル受理 | HTTP 200 + `signal_id` 返却 | [ ] |
| nonce prefix | `demo_` で始まる | [ ] |
| VPS ログ | `strategy_id: chronos_orb_mes_demo` | [ ] |
| Tradovate Demo UI | MES Long 1枚表示 | [ ] |
| CLOSE シグナル受理 | HTTP 200 + ポジション消滅 | [ ] |

**全 6 項目合格 = smoke test PASS = MFFU 切替え可能状態**
