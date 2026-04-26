# Yahoo Finance 非公式API — spy_bot.py使用エンドポイント抜粋

Base URL: https://query1.finance.yahoo.com
認証: 不要（User-Agentヘッダーを付ける）
Rate limit: 非公式のため制限は不明。IP単位で突然BAN可能性あり（1分間に数十req程度なら安定）

注意: Yahoo Financeは公式APIを提供していない。以下はリバースエンジニアリングで判明した非公式エンドポイントのため、予告なく仕様変更・廃止の可能性あり。

---

## 1. GET /v8/finance/chart/{symbol}
VIX/SPY の日足ローソク足取得

spy_bot.pyは以下2つのシンボルで使用:
- `%5EVIX` (URL encode of `^VIX`) — VIX指数日足
- `SPY` — SPY ETF日足

### spy_bot.pyの実際の呼び出し（VIX、IVR計算用）
```python
resp = requests.get(
    "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
    params={
        "period1": start_ts,   # unix timestamp (int)
        "period2": end_ts,     # unix timestamp (int)
        "interval": "1d"
    },
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=10,
)
data = resp.json()
closes_raw = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
closes = [float(c) for c in closes_raw if c is not None]
```

### spy_bot.pyの実際の呼び出し（VIX現在値、futuフォールバック）
```python
resp = requests.get(
    "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=5,
)
data = resp.json()
v = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
```

### spy_bot.pyの実際の呼び出し（SPY日足、SMA20/VRP計算用）
```python
resp = requests.get(
    f"https://query1.finance.yahoo.com/v8/finance/chart/SPY",
    params={"period1": start_ts, "period2": end_ts, "interval": "1d"},
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=10,
)
data = resp.json()
closes = [float(c) for c in data["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
```

### クエリパラメータ
| name     | type   | 説明                                         |
|----------|--------|----------------------------------------------|
| period1  | int    | 開始 unix timestamp（省略時=直近1日）         |
| period2  | int    | 終了 unix timestamp（省略時=現在）            |
| interval | string | "1d"=日足, "1h"=時間足, "1m"=1分足           |

### レスポンス構造（JSONパス）
```
data["chart"]["result"][0]
  ├── "meta"
  │     ├── "regularMarketPrice"  float  // 最新値（VIX現在値取得に使用）
  │     └── "symbol"              str
  ├── "timestamp"                 list[int]  // unix timestamps
  └── "indicators"
        └── "quote"
              └── [0]
                    ├── "close"   list[float|None]  // 終値（Noneあり、要フィルタ）
                    ├── "open"    list[float|None]
                    ├── "high"    list[float|None]
                    └── "low"     list[float|None]
```

### spy_bot.pyが使うパス
```python
# 現在値（VIX）
current_vix = data["chart"]["result"][0]["meta"]["regularMarketPrice"]

# 日足終値リスト（VIX/SPY、252日）
closes_raw = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
closes = [float(c) for c in closes_raw if c is not None]
```

---

## 注意事項
- `close` リストに None が混在するため必ず `if c is not None` フィルタが必要
- IVR計算には252取引日（約1年）必要なので period1 を380日前に設定
- VIX現在値は `meta.regularMarketPrice` で取得（日足リクエストでもリアルタイム値が入る）
- User-Agentなしだと403が返ることがある。`"Mozilla/5.0"` を付けると安定
- `data["chart"]["result"]` が空リストになるケースあり（エラー時）→ インデックス[0]アクセス前に長さ確認推奨
