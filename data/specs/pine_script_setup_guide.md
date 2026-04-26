# Pine Script 設置手順書 — Chronos ORB (MES / MNQ)

作成日: 2026-04-21
対象: ゆうさくさん (TradingView 画面操作)
所要時間: 初回 30-40 分 / 2本目以降 10-15 分
前提: TradingView Premium プラン (webhook 機能必須) + MFFU Flex ($127/月) 契約済み

---

## 0. 事前準備チェックリスト

- [ ] TradingView Premium プラン有効 (webhook 機能・複数 alert 同時実行)
- [ ] MFFU Flex アカウント有効 (Tradovate + TradingView 連携済み)
- [ ] Sora Lab 側 `chronos_webhook_server.py` が Public IP で稼働中
  (または ngrok で一時的に公開)
- [ ] `CHRONOS_WEBHOOK_SECRET` 共有済み (64 文字 16 進)
- [ ] Webhook URL 確定 (例: `https://chronos.sora-lab.io/chronos/signal`)

---

## 1. Pine Script を TradingView に貼り付け (MES)

### 手順
1. TradingView ログイン → 左上検索窓で `MES1!` 入力 → 先物チャート開く
2. タイムフレームを **5m** に変更 (上部タブ)
3. 画面下部の **Pine Editor** タブを開く
4. 右上の **Open → New** → **Indicator** ではなく **Strategy** を選択
5. エディタの内容を全削除
6. `strategies/pine/chronos_orb_mes.pine` の全文をコピペ
7. 右上の **Save** → 名前 `Chronos ORB MES` で保存
8. **右上の錠前アイコンを確認 → Private (プライベート) であることを必須確認**
   (Public にすると webhook_secret が漏洩する)
9. **Add to chart** をクリック

### 入力パラメータ設定
チャート上部の戦略名 (歯車アイコン) をクリック:
- ORB 開始: 9:30 ET / ORB 終了: 9:45 ET
- タイムストップ: 15:30 ET
- ATR 期間: 14 / ATR 倍率: 1.5 / RR: 2.0
- 発注枚数: 1 (初期値・ペーパー検証中は 1 枚固定)
- strategy_id: `chronos_orb_mes`
- **webhook_secret: CHRONOS_WEBHOOK_SECRET の値を貼り付け** (64 文字 16 進)

---

## 2. Alert 作成 (MES)

### 手順
1. チャート上で戦略名右クリック → **Add alert on Chronos ORB MES**
   または右上のベルアイコン → **Create Alert**
2. **Condition**: `Chronos ORB MES` → `Any alert() function call`
   (これが最重要。`alertcondition` 経由だと `alert()` の動的 JSON が使えない)
3. **Options**:
   - Trigger: **Once Per Bar Close**
   - Expiration: **Open-ended** (最長 2 ヶ月・期限切れ前に再作成必要)
4. **Alert name**: `Chronos-MES-Webhook`
5. **Notifications タブ**:
   - ✅ **Webhook URL** にチェック
   - URL: `https://<your-server>/chronos/signal`
   - (Email/App 通知は任意。検証中は OFF 推奨でノイズ削減)
6. **Message 欄**: **空欄のまま**
   (`alert()` 関数が動的に JSON body を送信するため message は不要)
7. **Create** をクリック

---

## 3. MNQ も同じ手順で設定

1. 新規タブで `MNQ1!` チャート開く (5m 足)
2. Pine Editor で `chronos_orb_mnq.pine` を貼り付け → Save → Private 確認 → Add to chart
3. strategy_id を `chronos_orb_mnq` にすることを確認
4. Alert を同じ手順で作成 (名前: `Chronos-MNQ-Webhook`)

---

## 4. Tradovate 連携確認

Phase 1A の webhook は **発注せず受信のみ**。
Phase 2 で Tradovate REST API 呼び出しが有効化される。

ただし TradingView ↔ Tradovate の **broker 連携** (画面上部の取引パネル) は
別系統で、Pine 戦略のバックテスト表示と **実取引は切り離されている**。
Phase 2 まで TradingView 画面からの手動発注は行わない。

---

## 5. 動作確認 (Phase 1A)

### 5.1 Webhook 受信確認
Sora Lab 側で以下を監視:

```bash
tail -f /Users/yuusakuichio/trading/data/chronos_webhook_signals.jsonl
```

場中 ORB 成立 (9:45 ET = JST 22:45) 後のブレイクアウトで 1 行追記されれば OK。

### 5.2 サーバログ確認
```
signal accepted: signal_id=<uuid> symbol=MES action=BUY qty=1
```

### 5.3 エラー時
- `IP拒否` → Cloudflare / reverse proxy 経由時の IP allowlist 追加要
- `HMAC 不一致` → `HMAC_PINE_MODE` 未実装時は期待通り (Phase 2 で対応)
- `validation` → Pine 側 JSON テンプレート崩壊 (ダブルクォート欠落等)

---

## 6. トラブルシューティング (主要 3 項目)

| 症状 | 原因 | 対処 |
|---|---|---|
| Alert が発火しない | Condition が `Any alert() function call` でない | Alert 編集 → Condition 再設定 |
| Webhook 401/403 | IP allowlist or HMAC 不一致 | サーバログ確認 → 該当検証を調整 |
| Pine Editor コンパイルエラー | v5 構文と古い文法の混在 | `// @version=5` 行を確認 |

---

## 7. 運用上の 3 つの重要ポイント

1. **Chart は必ず Private 保存**
   webhook_secret が平文埋め込みされているため Public 化で即漏洩。
   Publish Script は **絶対に押さない**。

2. **Alert は 2 ヶ月で自動期限切れ**
   TradingView 仕様で Open-ended alert も 2 ヶ月で失効。
   毎月 1 日に再作成する運用を推奨 (LaunchAgent / カレンダーリマインダ)。

3. **Once Per Bar Close で発火設定**
   `Once Per Bar` にすると bar 途中の値動きで複数回発火し nonce 衝突で replay 扱いになる。
   `Once Per Bar Close` 固定。

---

## 8. 変更履歴

| 日付 | 変更 | 担当 |
|---|---|---|
| 2026-04-21 | 初版作成 | strategist |
