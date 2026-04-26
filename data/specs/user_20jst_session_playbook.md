# 20時セッション プレイブック (2026-04-21 改訂版)

**合計所要時間: 40分**
**方針: Track A (Tradovate Demo) + Track B (Pine Script) のみ**
**Tradeify 購入・MFFU Builder 購入: 保留**

---

## Track A-0: Tradovate Demo アカウント開設 (15分)

**優先度: 最高 — Demo 疎通なしに Track B・Track C は進めない**

| 時刻 (目安) | アクション |
|---|---|
| 20:00 | https://trader.tradovate.com/welcome を開く |
| 20:02 | 「Try Demo」または「Open Demo Account」をクリック |
| 20:04 | 氏名・メールアドレス・パスワードを入力して登録 |
| 20:08 | メール認証リンクをクリック |
| 20:10 | ダッシュボードにログイン → 「Demo」モードであることを確認 |
| 20:12 | API Access: Account ID をメモ (smoke test で使用) |
| 20:15 | 完了 |

**確認事項**:
- 口座モードが「Demo」表示であること (Live と混同しない)
- 証拠金残高が仮想値 ($10,000 など) であること

---

## Track A-1: .env に Demo credentials 追記 (5分)

**優先度: 高 — Track B・C の前提条件**

VPS の `/root/spxbot/.env` に以下を追記:

```
# Tradovate Demo 専用 (Live と完全分離)
TRADOVATE_DEMO_USERNAME=<登録メールアドレス>
TRADOVATE_DEMO_PASSWORD=<パスワード>
TRADOVATE_DEMO_ACCOUNT_ID=<ダッシュボードで確認した Account ID>

# Demo 専用 Webhook Secret (Live の CHRONOS_WEBHOOK_SECRET とは別の値)
CHRONOS_WEBHOOK_SECRET_DEMO=<64文字のランダム16進数>
```

**Demo secret 生成コマンド** (Mac ターミナルで実行):
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Track B: TradingView Pine Script + Alert 設定 (15分)

**優先度: 高 (Track A-0/A-1 完了後)**

### B-1. Pine Script アップロード (7分)

1. TradingView を開く → Pine Editor
2. 新規スクリプト作成
3. `strategies/pine/chronos_orb_mes_demo.pine` の内容をコピー&ペースト
4. 「Add to Chart」をクリック
5. strategy_id が `chronos_orb_mes_demo` になっていることを確認
6. チャート上に `[Demo]` ラベルが表示されることを確認

### B-2. webhook_secret 設定 (3分)

1. Pine Editor で `REPLACE_ME_64CHAR_HEX_DEMO` を Track A-1 で生成した Demo secret に置換
2. MFFU 本番の secret (`218f45b8762f8176f7d5568f7bcab9eb11fb76fee99242cb309a628ed276ec65`) とは別の値であること
3. スクリプトを「Private」で保存 (Public 保存は secret 漏洩のため禁止)

### B-3. Alert 設定 (5分)

TradingView で MES 5分足チャートを開き、以下 3 つの Alert を設定:

| Alert 名 | 条件 | Webhook URL |
|---|---|---|
| MES LONG Entry [Demo] | longSignal | https://difference-after-tend-patterns.trycloudflare.com/chronos/signal |
| MES SHORT Entry [Demo] | shortSignal | https://difference-after-tend-patterns.trycloudflare.com/chronos/signal |
| MES TIME STOP [Demo] | isTimeStop + position_size != 0 | https://difference-after-tend-patterns.trycloudflare.com/chronos/signal |

Alert の Message: `{{strategy.order.alert_message}}`

**確認事項**:
- `nonce` が `demo_` prefix で始まること (JSON プレビューで確認)
- `account_id: "tradovate_demo"` が含まれること
- `strategy_id: "chronos_orb_mes_demo"` であること

詳細: `data/specs/pine_script_setup_guide.md`

---

## Track C: 疎通 smoke test (5分)

**優先度: 中 (Track A/B 完了後)**

詳細コマンド: `data/specs/chronos_demo_smoke_test.md`

手順概要:
1. Health check → HTTP 200 確認
2. curl で手動シグナル送信 (BUY 1 MES Demo)
3. VPS ログで `demo_` prefix の nonce を確認
4. Tradovate Demo UI でエントリー表示を確認

---

## 完了チェックリスト

- [ ] Track A-0: Tradovate Demo アカウント開設完了
- [ ] Track A-0: Account ID メモ完了
- [ ] Track A-1: .env に Demo credentials 追記完了
- [ ] Track A-1: CHRONOS_WEBHOOK_SECRET_DEMO 生成・追記完了
- [ ] Track B: Pine Script アップロード + strategy_id `chronos_orb_mes_demo` 確認
- [ ] Track B: webhook_secret を Demo 専用値に置換・Private 保存
- [ ] Track B: 3 Alert 設定完了 + nonce `demo_` prefix 確認
- [ ] Track C: Health check HTTP 200
- [ ] Track C: 手動シグナル → VPS ログ → Tradovate Demo UI 約定確認

---

## Demo / MFFU 切替え手順 (MFFU Flex Pending 解除後)

MFFU から「Tradovate アカウント発行」通知が来たら:

| ステップ | 作業 |
|---|---|
| 1 | VPS `.env` に `TRADOVATE_MFFU_USERNAME` / `TRADOVATE_MFFU_PASSWORD` / `TRADOVATE_MFFU_ACCOUNT_ID` を追記 |
| 2 | TradingView の Demo Alert (chronos_orb_mes_demo) を **停止** |
| 3 | TradingView に `chronos_orb_mes.pine` をアップロード (既存 MFFU 用) |
| 4 | 既存 `strategies/pine/chronos_orb_mes.pine` の `REPLACE_ME_64CHAR_HEX` を MFFU 本番 secret に置換 |
| 5 | MFFU 用 Alert 3本を新規設定 (strategy_id = `chronos_orb_mes`) |
| 6 | smoke test: `chronos_orb_mes` で BUY 1 MES → MFFU Eval 口座に反映確認 |
| 7 | Eval 開始 (Consistency 50% で $3K target) |

**MFFU 本番 secret**: `218f45b8762f8176f7d5568f7bcab9eb11fb76fee99242cb309a628ed276ec65`
(保管場所: VPS `/root/spxbot/.env` の `CHRONOS_WEBHOOK_SECRET`)

---

## account_id routing まとめ

| account_id | 送信先 | strategy_id | secret 環境変数 |
|---|---|---|---|
| `tradovate_demo` | Tradovate Demo | chronos_orb_mes_demo | CHRONOS_WEBHOOK_SECRET_DEMO |
| `mffu_flex_A` (予定) | MFFU Flex Eval | chronos_orb_mes | CHRONOS_WEBHOOK_SECRET |
| `tradeify_F` (保留) | Tradeify Lightning | chronos_orb_mes_tradeify | CHRONOS_WEBHOOK_SECRET_TRADEIFY |
