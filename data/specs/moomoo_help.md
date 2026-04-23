# moomoo / futu OpenD — ペーパー取引・権限・接続設定

spy_bot.pyの設定をもとにした実態ベースのメモ。

---

## 接続設定

### OpenD（ローカルプロセス）
- ホスト: 127.0.0.1
- ポート: 11111（OPEND_PORT）
- Mac上で常時起動が必要（LaunchAgentで管理）

### OpenQuoteContext（相場データ）
```python
quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
```

### OpenSecTradeContext（取引）
```python
trade_ctx = OpenSecTradeContext(
    filter_trdmarket=TrdMarket.US,       # US市場のみ
    host='127.0.0.1',
    port=11111,
    security_firm=SecurityFirm.FUTUJP,   # Futu証券JP口座
)
```

SecurityFirm の選択肢:
- FUTUJP      — 日本法人（spy_bot.pyが使用）
- FUTUSECURITIES — 香港法人
- FUTUINC     — 米国法人
- FUTUSG / FUTUAU / FUTUCA / FUTUMY — 各国法人

---

## TrdEnv（取引環境）

| 値        | 意味           |
|-----------|----------------|
| REAL      | 本番口座       |
| SIMULATE  | ペーパー取引口座|

### ペーパー取引の起動
```bash
python3 spy_bot.py --paper
```
`--paper` 引数でTrdEnv.SIMULATEに切り替わる。

### アカウント解決ロジック（spy_bot.py内）
1. `get_acc_list()` でアカウント一覧取得
2. trd_env == 'REAL' の行を優先
3. REAL口座がなければ trd_env == 'SIMULATE' にフォールバック

---

## SIMULATE対応市場（TrdMarket）

TrdMarketの SIMULATE 系:
```
FUTURES_SIMULATE_HK  — 香港先物ペーパー
FUTURES_SIMULATE_US  — 米国先物ペーパー
FUTURES_SIMULATE_SG  — シンガポール先物ペーパー
FUTURES_SIMULATE_JP  — 日本先物ペーパー
```

証券（stock/options）のペーパーは filter_trdmarket=TrdMarket.US + TrdEnv.SIMULATE で動く。
上記 FUTURES_SIMULATE_* は先物専用。

---

## 権限（Quote権限）

### spy_bot.pyで判明している権限要件
- US.VIX の get_market_snapshot → VIX quote権限が必要
- US.SPY の get_market_snapshot → US Securities quote権限（us_qot_right）が必要
  → 権限なし時はFinnhubフォールバックで処理
- SPYオプションチェーン get_option_chain → US Options権限が必要
  → 権限なし時はオプション選択スキップ

### 権限確認方法
moomooアプリ → アカウント → 権限・プラン → 相場権限 で確認。

US Securities相場権限コード（内部識別）:
- `us_qot_right` — 米国株・ETFリアルタイム相場
- 米国オプション相場権限は別途必要（有料プランまたは申請）

---

## unlock_trade（取引パスワード解除）

```python
ret, data = trade_ctx.unlock_trade(password=TRADE_PASSWORD)
```

- REAL口座では毎セッション開始時に必要
- SIMULATE口座ではunlockが不要（spy_bot.pyで `self.unlock_ok = True` 直接セット）
- OpenDのGUI設定で「ソフトウェアでのアンロックを無効」にしている場合は
  `"unlock button"` / `"disabled in the GUI"` エラーが返るが、GUIアンロック済みなら取引可能

---

## futuオプションコード形式

```
US.{TICKER}{YY}{MM}{DD}{C|P}{STRIKE×1000（8桁ゼロ埋め）}
```

例:
```
US.SPY260413P668000
  → SPY, 2026-04-13, PUT, Strike $668.000

US.SPY260414C575000
  → SPY, 2026-04-14, CALL, Strike $575.000
```

STRIKE部分は小数点なし、1000倍、8桁ゼロ埋め。

---

## 注意事項
- OpenD の残り試行回数に注意（設定変更・ログイン操作は禁止）
- VPS上の OpenD には絶対触らない（Mac版と別インスタンス）
- SIMULATE口座は本番口座とacc_idが異なる。get_acc_listで動的取得すること
