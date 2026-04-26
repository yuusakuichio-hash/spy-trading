# MFFU Flex Activation Plan (2026-04-21)

MFFU Flex $50K Pending 解除 (Tradovate アカウント発行) 通知が来たときの切替え手順。

---

## トリガー条件

- MFFU からメール「Your Tradovate account is ready」が届いた
- または MFFU ダッシュボードで Eval アカウントステータスが「Active」になった

---

## Step 1: MFFU Tradovate credentials 取得 (5分)

1. MFFU ダッシュボード → 「My Accounts」→ Flex $50K を開く
2. 以下の値をメモ:
   - Tradovate Username (メールアドレス形式)
   - Tradovate Password (初回ログイン時に設定)
   - Account ID (数値 例: 123456)
3. https://trader.tradovate.com でログイン確認 (Live モード表示であること)

---

## Step 2: VPS .env に MFFU credentials 追記 (3分)

```bash
ssh -i ~/.ssh/deploy_key root@198.13.37.17

# .env に追記
cat >> /root/spxbot/.env << 'EOF'
# MFFU Flex Eval (2026-04-21 発行)
TRADOVATE_MFFU_USERNAME=<MFFUダッシュボードで確認>
TRADOVATE_MFFU_PASSWORD=<同上>
TRADOVATE_MFFU_ACCOUNT_ID=<同上>
EOF

# 確認
grep TRADOVATE_MFFU /root/spxbot/.env
```

---

## Step 3: TradingView Alert を Demo から MFFU に切替え (10分)

### 3-1. Demo Alert を停止

TradingView → Alerts パネル → 以下 3 本を「Pause」または「Delete」:
- MES LONG Entry [Demo]
- MES SHORT Entry [Demo]
- MES TIME STOP [Demo]

### 3-2. MFFU 用 Pine Script をアップロード

1. Pine Editor → 新規スクリプト
2. `strategies/pine/chronos_orb_mes.pine` の内容をコピー&ペースト
3. `REPLACE_ME_64CHAR_HEX` を MFFU 本番 secret に置換:
   ```
   218f45b8762f8176f7d5568f7bcab9eb11fb76fee99242cb309a628ed276ec65
   ```
   (VPS `.env` の `CHRONOS_WEBHOOK_SECRET` と同一値)
4. strategy_id が `chronos_orb_mes` (Demo suffix なし) であることを確認
5. スクリプトを「Private」で保存

### 3-3. MFFU 用 Alert 3本を設定

| Alert 名 | 条件 | Webhook URL |
|---|---|---|
| MES LONG Entry [MFFU] | longSignal | https://difference-after-tend-patterns.trycloudflare.com/chronos/signal |
| MES SHORT Entry [MFFU] | shortSignal | 同上 |
| MES TIME STOP [MFFU] | isTimeStop + position | 同上 |

Alert Message: `{{strategy.order.alert_message}}`

確認事項:
- `nonce` に `demo_` prefix がないこと
- `account_id: "mffu_flex_A"` (またはMFFU発行のアカウントID)
- `strategy_id: "chronos_orb_mes"` であること

---

## Step 4: MFFU Eval smoke test (5分)

```bash
ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no root@198.13.37.17 python3 - << 'PYEOF'
import json, urllib.request, time, os

SECRET = os.environ.get("CHRONOS_WEBHOOK_SECRET", "REPLACE_ME")
TS = int(time.time())

payload = json.dumps({
    "timestamp": TS,
    "nonce": f"mffu_smoke_{TS}",
    "symbol": "MES",
    "action": "BUY",
    "qty": 1,
    "strategy_id": "chronos_orb_mes",
    "account_id": "mffu_flex_A",
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

MFFU Tradovate ダッシュボードで MES Long 1枚を確認後、手動クローズ。

---

## Step 5: Eval 開始確認

- [ ] MFFU ダッシュボードで Eval カウンター開始を確認
- [ ] 1日目: 1トレード実行・P&L 記録
- [ ] Consistency ルール (50% 上限): 1日のP&Lが全体P&Lの50%を超えないように注意
- [ ] Target: $3,000 profit (Flex $50K Eval)
- [ ] Max Daily Loss: $2,000 (厳守)
- [ ] Max Drawdown: $4,000 (厳守)

---

## Demo → MFFU 切替え後の状態

| 項目 | 切替え前 | 切替え後 |
|---|---|---|
| アクティブ Alert | Demo (chronos_orb_mes_demo) | MFFU (chronos_orb_mes) |
| account_id routing | tradovate_demo | mffu_flex_A |
| 実資金 | なし | MFFU Eval 仮想資金 $50K |
| Chronos ログ nonce | `demo_` prefix | prefix なし |

---

## ロールバック手順 (MFFU で問題発生時)

```bash
# 1. TradingView MFFU Alert を即停止 (Pause)
# 2. Demo Alert を再開
# 3. VPS で MFFU secret を無効化
ssh -i ~/.ssh/deploy_key root@198.13.37.17
systemctl restart chronos_webhook.service
```
