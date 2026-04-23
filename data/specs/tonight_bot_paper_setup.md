# 今夜 Bot Paper 稼働セットアップ手順（ゆうさくさんアクション）

- 対象時間帯: 2026-04-21（火）21:30 - 22:30 JST
- 絶対期限: **23:00 JST**
- 選択ルート: **ルート A（TradersPost Free tier + TP Paper 口座）** を第一推奨
- 選択理由: Tradovate 2FA 承認メール待ちの最大ボトルネックを完全回避、40 分で完結
- 並行実装: Chronos 側 webhook POST クライアントは Sora Lab（builder）が 21:30 起動で並列開発

---

## 全体タイムライン

| 時刻 | 担当 | タスク | 所要 |
|---|---|---|---|
| 21:30-21:33 | ゆうさくさん | TP 登録 | 3 分 |
| 21:33-21:35 | ゆうさくさん | Paper broker connection | 2 分 |
| 21:33-22:00 | Sora Lab builder（並行） | Chronos webhook client 実装 | 25 分 |
| 21:35-21:40 | ゆうさくさん | Strategy 作成・Webhook URL 取得 | 5 分 |
| 21:40-21:45 | ゆうさくさん | Webhook URL を .env に設定・共有 | 5 分 |
| 22:00-22:10 | Sora Lab + ゆうさくさん | curl 疎通テスト | 10 分 |
| 22:10-22:30 | Sora Lab | Chronos paper モード起動 | 20 分 |
| 22:30-23:00 | 監視 | 初回シグナル発注・TP で約定確認 | バッファ 30 分 |

---

## STEP 1: TradersPost 登録（3 分・21:30-21:33）

### やること
1. https://traderspost.io/ にアクセス
2. 右上 "Get Started" または "Sign Up" をクリック
3. Email（yuusakuichio@gmail.com 推奨、Google SSO があればそれを使用）とパスワードで登録
4. 確認メールが届いたら認証リンククリック

### つまずきポイント
- Google SSO があればそちらが最速。Apple SSO の可否は非確認
- クレカ要求画面が出たら Free tier 選択を再確認（Free は公式に「No credit card required」明記）
- 確認メールが Gmail プロモーションタブに入る可能性 → 直接検索 "traderspost" で確認

---

## STEP 2: Paper broker connection 作成（2 分・21:33-21:35）

### やること
1. TP ダッシュボード → 左メニュー "Brokers" または "Connections"
2. "Add Connection" → 一覧から **"TradersPost Paper"** を選択
3. 口座名（例: `chronos_paper_v1`）を入力 → Save
4. Status が "Connected" になることを確認

### つまずきポイント
- Tradovate を選ばないこと（今夜は Paper のみ）
- 口座名は後で Strategy と紐付けるので Chronos/Atlas 判別可能な名前にする

---

## STEP 3: Strategy 作成 & Webhook URL 取得（5 分・21:35-21:40）

### やること
1. TP ダッシュボード → "Strategies" → "New Strategy"
2. Strategy 名（例: `chronos_mnq_paper`）入力
3. Broker connection に STEP 2 で作った Paper 口座を指定
4. Strategy 作成後、"Webhook" タブを開く
5. Webhook URL をコピー（形式: `https://webhooks.traderspost.io/trading/webhook/{uuid}/{password}`）

### つまずきポイント
- Webhook URL は **per-strategy** で発行される。Strategy ごとに異なる URL
- URL の後半 `{uuid}/{password}` が認証情報。**Git コミット禁止・環境変数管理必須**
- Regenerate 機能あり。漏洩時はすぐ regenerate

---

## STEP 4: Webhook URL を環境変数に設定（5 分・21:40-21:45）

### やること
1. Webhook URL を `.env` ファイル（Git 対象外）に追記:
   ```
   TRADERSPOST_WEBHOOK_URL_CHRONOS=https://webhooks.traderspost.io/trading/webhook/xxxx/yyyy
   ```
2. `.gitignore` に `.env` が含まれているか確認（既に含まれているはず）
3. Sora Lab（builder）に Webhook URL を **Pushover DM または Pushover priority=0 通知** で共有

### つまずきポイント
- Slack / GitHub コメントに貼らない（public 流出リスク）
- 複数 Strategy 作る場合は `_CHRONOS` `_ATLAS` 等でサフィックス分ける

---

## STEP 5: curl 疎通テスト（10 分・22:00-22:10）

### やること
ゆうさくさんのターミナルで以下を実行（環境変数展開前提）:
```bash
curl -X POST "$TRADERSPOST_WEBHOOK_URL_CHRONOS" \
  -H "Content-Type: application/json" \
  -d '{
    "ticker": "AAPL",
    "action": "buy",
    "quantity": 1,
    "orderType": "market",
    "sentiment": "bullish",
    "extras": {"test": "tonight_setup_smoke"}
  }'
```

### 成功判定
- HTTP 200 応答
- TP ダッシュボード → Strategy 詳細 → "Signals" タブに記録
- TP ダッシュボード → Paper 口座 → "Orders" に AAPL buy 注文が記録（市場クローズ中なら pending）

### つまずきポイント
- 401/403: Webhook URL の uuid/password 間違い → STEP 3 で再コピー
- 422: JSON 形式エラー（trailing comma, smart quote）→ curl の `-d` 内を再確認
- 429: rate limit（60 req/min）→ 60 秒待機後リトライ
- ticker が TP 側で認識されない → `ticker` 形式を確認（SPY、MNQU2025 等、ブローカー記法に合わせる）

---

## STEP 6: Chronos paper モード起動（20 分・22:10-22:30 / Sora Lab 担当）

### Sora Lab が並行でやること（ゆうさくさんはモニタリングのみ）
1. `common/traderspost_client.py` 新規作成（pushover_client と同様の POST client、backoff 付き）
2. Chronos 発注ロジックに traderspost_client を paper モードで組込（`PAPER_TRADING_ENABLED=1` 時に発火）
3. 初回疎通スモーク実行（AAPL ダミー signal）
4. Chronos を paper モードで起動

### ゆうさくさんが見るもの
- Pushover 通知「[Chronos] paper signal sent: AAPL buy qty=1」
- TP ダッシュボードで同一 signal が着弾していること

### つまずきポイント
- Chronos 側のレート制限実装忘れ → 60 req/min 超過で 429 連発リスク
- 23:00 時点で signal 未発火なら、paper 口座への市場クローズ影響有無を確認（Tradovate sim なら 24h 対応、TP Paper は要確認）

---

## 監視フェーズ（22:30-23:00 / 30 分バッファ）

### ゆうさくさん確認項目
1. Pushover が Chronos から届いている
2. TP ダッシュボードの Signals tab にエントリが増えている
3. Paper 口座の Orders/Positions に注文が載っている
4. エラー通知が来ていない

### 問題発生時の判断
- 22:45 時点で未稼働 → Sora Lab に **Pushover priority=1 で即通知**、原因切り分け
- 23:00 までに稼働不可 → 今夜は **STEP 5 の curl 疎通成功のみで着地**（=「TP 経由で paper 口座に発注できる経路確立」の実績は残る）、Chronos 組込は翌朝継続

---

## 事前に知っておくリスク

| リスク | 影響 | 対策 |
|---|---|---|
| Free tier 8 日目以降の auto-submit 挙動が公式非明示 | 4/28 以降停止の可能性 | 7 日以内に Starter ($41.65/月) 昇格判断、またはそれまでに挙動実測 |
| Webhook URL 漏洩 | 発注乗っ取り | `.env` のみ・Git 禁止・regenerate 手順確認 |
| TP 側 rate limit 60/min | Chronos 連射で 429 | client に min_interval_seconds 実装（既存 pushover_client 流用） |
| TP Paper 口座の先物対応範囲が不明 | MNQ ticker が通らない可能性 | curl テストで早期検証、通らなければルート B（Tradovate sim）に切替 |
| MFFU Flex ルール検証は今夜未実施 | 月曜本番判断遅延 | 明朝にルート B を実施（2FA 承認込みで 60-80 分確保） |

---

## 今夜の "Done" 定義

### 最低ライン（必達）
- TP アカウント作成済
- Paper broker connection 作成済
- Strategy 作成・Webhook URL 発行済
- curl で疎通 200 OK 確認済

### 目標ライン
- 上記 + Chronos から実 signal を TP に送信・paper 口座で約定（または pending）確認済

### ストレッチ
- 上記 + Atlas 側も並行接続（strategy 2 本目）

---

## 翌朝タスク（今夜やらない）

- Tradovate broker connection 作成（MFFU Flex sim 実接続）
- 2FA 承認メール処理
- MFFU Flex ルール（DD limit, trailing stop, consistency）の TP 側反映
- Starter プラン昇格判断（Free tier 挙動実測後）

---

## 参考: Webhook JSON テンプレ（Chronos 実装用・builder 参考）

```python
# common/traderspost_client.py 想定シグネチャ
def send_signal(
    webhook_url: str,
    ticker: str,
    action: str,  # buy/sell/exit/cancel/add
    quantity: int = 1,
    order_type: str = "market",
    limit_price: float | None = None,
    stop_loss: dict | None = None,  # {"type": "stop", "amount": 30}
    take_profit: dict | None = None,  # {"amount": 60}
    sentiment: str | None = None,  # bullish/bearish/flat
    extras: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """POST signal to TradersPost webhook. Returns response JSON or raises."""
```

- 必須: ticker, action
- 既存の `common/pushover_client.py` の backoff/rate-limit パターン流用
- `dry_run=True` 時は POST せず JSON のみ返す（テスト用）
- レート制限: 60/min, 500/hour を client 内で enforce
