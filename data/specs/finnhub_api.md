# Finnhub API — spy_bot.py使用エンドポイント抜粋

Base URL: https://finnhub.io/api/v1
認証: クエリパラメータ `token=FINNHUB_API_KEY`
Rate limit: 無料プラン 60 req/min（公式: https://finnhub.io/docs/api/rate-limit）

---

## 1. GET /v1/quote
SPY現在価格取得（futu US Securities権限なし時のフォールバック）

### spy_bot.pyの実際の呼び出し
```python
resp = requests.get(
    "https://finnhub.io/api/v1/quote",
    params={"symbol": "SPY", "token": FINNHUB_API_KEY},
    timeout=10,
)
```

### パラメータ
| name   | type   | 説明              |
|--------|--------|-------------------|
| symbol | string | ティッカー "SPY"  |
| token  | string | APIキー            |

### レスポンス (JSON)
```json
{
  "c":  567.23,   // current price
  "h":  570.10,   // high of the day
  "l":  564.50,   // low of the day
  "o":  566.00,   // open price
  "pc": 565.80,   // previous close
  "t":  1713100000 // timestamp (unix)
}
```

### spy_bot.pyが使うフィールド
```python
data = resp.json()
price   = data["c"]          # 現在値
open_px = data["o"]          # 当日始値
high    = data["h"]
low     = data["l"]
prev_cl = data["pc"]
```

---

## 2. GET /v1/stock/candle
SPY日足ローソク足取得（yahoo fallback の次のフォールバック）

### spy_bot.pyの実際の呼び出し
```python
resp = requests.get(
    "https://finnhub.io/api/v1/stock/candle",
    params={
        "symbol": "SPY",
        "resolution": "D",
        "from": start_ts,   # unix timestamp (int)
        "to":   end_ts,     # unix timestamp (int)
        "token": FINNHUB_API_KEY
    },
    timeout=10,
)
data = resp.json()
closes = [float(c) for c in data.get("c", [])]
```

### パラメータ
| name       | type   | 説明                                    |
|------------|--------|-----------------------------------------|
| symbol     | string | "SPY"                                   |
| resolution | string | "D"=日足, "W"=週足, "M"=月足, "1"=1分足 |
| from       | int    | 開始 unix timestamp                     |
| to         | int    | 終了 unix timestamp                     |
| token      | string | APIキー                                 |

### レスポンス (JSON)
```json
{
  "c": [562.1, 563.4, ...],  // close prices
  "h": [...],                 // high
  "l": [...],                 // low
  "o": [...],                 // open
  "s": "ok",                  // status: "ok" | "no_data"
  "t": [1712880000, ...],     // timestamps
  "v": [...]                  // volume
}
```

### spy_bot.pyが使うフィールド
```python
closes = [float(c) for c in data.get("c", [])]  # 終値リスト（SMA20・VRP計算用）
```

---

## 3. GET /v1/forex/rates
USD/JPY レート取得（月次P&Lを円換算する際に使用）

### spy_bot.pyの実際の呼び出し
```python
resp = requests.get(
    "https://finnhub.io/api/v1/forex/rates",
    params={"base": "USD", "token": FINNHUB_API_KEY},
    timeout=5,
)
rates = resp.json().get("quote", {})
jpy   = float(rates.get("JPY", 0))
```

### パラメータ
| name  | type   | 説明           |
|-------|--------|----------------|
| base  | string | "USD"          |
| token | string | APIキー         |

### レスポンス (JSON)
```json
{
  "base": "USD",
  "quote": {
    "JPY": 153.42,
    "EUR": 0.926,
    ...
  }
}
```

### spy_bot.pyが使うフィールド
```python
usdjpy = float(rates.get("JPY", 150.0))  # デフォルト 150.0
```

---

## 注意事項
- 無料プランではリアルタイムではなく15分遅延データの可能性あり（US株）
- `/v1/quote` の `c` フィールドはリアルタイムまたは直近約定値
- status `"no_data"` の場合 `c` フィールドが空配列になるので `data.get("c", [])` で安全処理
