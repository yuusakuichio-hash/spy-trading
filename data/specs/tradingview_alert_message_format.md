# TradingView Alert Message Format — Chronos Webhook 契約

作成日: 2026-04-21
責任: Chronos strategist
対応サーバ: `chronos_webhook_server.py` v1.0.0
対応スキーマ: `data/specs/chronos_webhook_contract.json`
対応Pine: `strategies/pine/chronos_orb_{mes,mnq}.pine`

---

## 1. 概要

TradingView Pine Script 戦略の `alert()` 関数が生成する JSON body の正式仕様。
Sora Lab 側 FastAPI エンドポイント `POST /chronos/signal` がこの契約で検証する。

---

## 2. JSON スキーマ

### 2.1 フィールド一覧

| field | type | 制約 | 説明 |
|---|---|---|---|
| `timestamp` | integer | Unix epoch (秒) | Pine `timenow` を 1000 で割った値 |
| `nonce` | string | 1-128 文字 | `{syminfo.ticker}-{timenow_ms}` 形式。重複不可 |
| `symbol` | string | enum: `MES`, `MNQ`, `ES`, `NQ` | 固定文字列 (syminfo.ticker とは別) |
| `action` | string | enum: `BUY`, `SELL`, `CLOSE` | 発注方向 |
| `qty` | integer | 1-5 | 発注枚数 |
| `strategy_id` | string | 1-64 文字 | `chronos_orb_mes` / `chronos_orb_mnq` |
| `hmac` | string | 64 文字 16 進 | Pine 版では `webhook_secret` の平文を格納 |

### 2.2 実体例 (MES ロング)

```json
{
  "timestamp": 1745222400,
  "nonce": "MES1!-1745222400123",
  "symbol": "MES",
  "action": "BUY",
  "qty": 1,
  "strategy_id": "chronos_orb_mes",
  "hmac": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

---

## 3. サーバ側検証順序

`chronos_webhook_server.py::receive_signal` は以下の順で検証する:

1. **IP allowlist** — `common/tradingview_ips.py::TRADINGVIEW_IPS` 一致
2. **Pydantic 検証** — フィールド型・enum・長さ
3. **HMAC 検証** — `_verify_hmac()` 経由
4. **Timestamp 検証** — `|now - timestamp| <= 30 sec`
5. **Nonce 検証** — SQLite で重複排除 (1時間 TTL)
6. **JSONL 書込み** — `data/chronos_webhook_signals.jsonl`

---

## 4. HMAC 検証の二系統設計

### 4.1 標準モード (`HMAC_PINE_MODE` 未設定)

真の HMAC-SHA256 検証。クライアントは body から `hmac` フィールドを除外した上で
キーソート済みコンパクト JSON を再構築し、CHRONOS_WEBHOOK_SECRET で HMAC を計算する。

**対応クライアント**: Python / Node.js などネイティブ HMAC 対応環境。

### 4.2 Pine Script モード (`HMAC_PINE_MODE=1` ※サーバ側拡張予定)

Pine Script は HMAC-SHA256 をネイティブサポートしない。
代替として `webhook_secret` を `hmac` フィールドに平文埋め込み、サーバ側で
`hmac.compare_digest(payload_hmac, CHRONOS_WEBHOOK_SECRET.hex())` の完全一致検証に切り替える。

**二層防御**:
- TradingView IP allowlist (第一層)
- webhook_secret 平文完全一致 (第二層)

**リスク**:
- Pine Chart を public 共有した瞬間に secret が漏洩する
- → 緩和策: chart を必ず **Private** 保存・secret を定期ローテーション (月次)

**Phase 1A 現状**:
サーバ側は `HMAC_PINE_MODE` 未実装。Phase 2 拡張で追加予定。
それまでは mock HMAC 検証を通すため、Python 中継プロキシで正規 HMAC に再計算する選択肢もあり。

---

## 5. nonce 設計

### 5.1 Pine 側生成

```pine
nonce = syminfo.ticker + "-" + str.tostring(timenow)
// 例: "MES1!-1745222400123"
```

- `syminfo.ticker` は MES/MNQ 先物の current contract (例: `MES1!`)
- `timenow` はミリ秒 epoch (bar close 時刻)
- タイムフレーム 5 分足なら同一 bar で 2 回発火しても同じ nonce になる
  → Pine 側 `alert.freq_once_per_bar_close` で防御

### 5.2 サーバ側重複排除

SQLite `webhook_nonce_cache.sqlite` で 1 時間保持。
同一 nonce 再送は HTTP 409 (replay) で拒否。

---

## 6. action → Tradovate 変換 (Phase 2 仕様)

| action | 既存ポジションなし | ロング保有中 | ショート保有中 |
|---|---|---|---|
| `BUY` | 新規ロング | (無視) | 反対決済+新規ロング (※要確認) |
| `SELL` | 新規ショート | 反対決済+新規ショート (※要確認) | (無視) |
| `CLOSE` | (無視) | 成行クローズ | 成行クローズ |

※ Phase 2 実装時に「反転エントリー許可/禁止」設計を確定する。

---

## 7. 変更履歴

| 日付 | 変更 | 担当 |
|---|---|---|
| 2026-04-21 | 初版作成 | strategist |
