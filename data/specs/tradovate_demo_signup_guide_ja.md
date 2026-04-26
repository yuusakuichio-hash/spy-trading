# Tradovate Demo 口座開設ガイド（日本人向け・step-by-step）

| 項目 | 値 |
|---|---|
| 作成日 | 2026-04-21 |
| 対象 | ゆうさくさん（20時セッションでChronos Bot疎通テスト用） |
| 所要時間 | 約 8〜15 分（メール認証と初回ログイン含む） |
| 費用 | 無料（クレカ登録不要） |
| KYC | **不要**（Demoのみ。Live口座は別途SSN/パスポート等必要） |
| 公式ソース | https://www.tradovate.com/ / https://info.tradovate.com/simulated-trading / https://api.tradovate.com/ / https://community.tradovate.com/ |

---

## ★ 最初に確認すべき重要ポイント（目を通す）

1. **Demo は 14 日間無料**。以降は延長申請 or 有料プラン、もしくは新規Demo再取得で継続可（複数アカウント可）。
2. **初期残高は $50,000 仮想資金**。画面から任意額へ増減可能（$500 〜 $1,000,000）。
3. **market data は real-time**（delayed ではない・CME Group 先物全カバー）。
4. **API アクセス（cid / sec）は Demo 口座でも取得可能**だが、**API Access add-on 月額 $25 の購入が必要**。これは Live/Demo 共通のルール（公式フォーラム・support記事で確認済）。
5. **月額 $25 を今すぐ払いたくない場合** → **username / password だけでREST `/auth/accessTokenRequest` は叩ける**（cid/sec は optional。公式 OpenAPI schema 確認済）。まずは疎通テストのみならこのルートで十分。
6. **日本からの Demo 登録は可能**（Tradovate は "many countries" を受け付ける公式表明あり。Demo はそもそも実取引資金を扱わないので国籍制限は事実上なし）。
7. **Chronos Bot で使う base URL**：`https://demo.tradovateapi.com/v1`（既に tradovate_client.py にハードコード済み）。

---

## Step 1: 公式サイトアクセス（所要 1 分）

1. ブラウザで **https://www.tradovate.com/** を開く
2. 画面上部ヘッダー中央右寄りに紺色の **「OPEN ACCOUNT」** ボタン、その左隣に緑系の **「TRY TRADOVATE NOW」** ボタンがある
3. **「TRY TRADOVATE NOW」** をクリック（または直接 https://www.tradovate.com/trial/ へ）
4. トライアルランディングページに遷移 → ページ中央に **「Sign-Up Now for Your Free Trial >>」** という大きな CTA ボタンが表示される
5. これをクリック → 登録フォーム画面（https://trader.tradovate.com/try-it もしくは /register）へ

※ **「Start Trading」「Start Free Trial」** というラベルも同等のリンク。いずれも最終的に同じ登録フォームに到達する。

---

## Step 2: アカウント登録フォーム（所要 2 分）

登録フォームで入力する項目（公式フォーラム・PickMyTradeガイド確認済）：

| フィールド | 入力内容 | 備考 |
|---|---|---|
| First Name | ローマ字（例: `Yusaku`） | 本名でなくてもDemoでは可。ただしLive口座に将来アップグレードする場合は整合性必要 |
| Last Name | ローマ字（例: `Sora`） | 同上 |
| Email | 有効なGmail等 | メール認証リンクが届く。`yuusakuichio@gmail.com` でよい |
| Password | 8文字以上・大小英数字記号推奨 | **Tradovate ログイン用パスワード**。後でAPI用パスワードを別途設定推奨 |
| Country / Region | **Japan** を選択 | ドロップダウン・アルファベット順・「J」まで下ると Japan が出現 |
| Phone（任意・ある場合） | +81-xx-xxxx-xxxx | 省略可能なケースあり |
| Agree to Terms | チェックボックス | 利用規約・個人情報方針に同意 |

**「Create Account」/「Sign Up」** ボタンクリック → 登録送信

### KYC について（最重要）
- **Demo 登録では身分証提出・SSN・住所証明は求められない**（公式表明・複数実践者確認済）
- Live 口座へのアップグレード時に別途 KYC フロー（パスポート/住所証明/W-8BEN）
- **Demo はメール+パスワードのみで完結する**

---

## Step 3: メール認証（所要 2〜3 分）

1. 登録送信後、画面に **「Check your email to verify」** 的なメッセージ表示
2. Gmail を開く → `noreply@tradovate.com` または `support@tradovate.com` からのメール受信（迷惑メールフォルダも確認）
3. メール本文の **「Verify Email」/「Confirm Your Account」** ボタンをクリック
4. 自動的にブラウザで認証完了画面 → **即時ログイン可能**（activation 待ちゼロ。24-48h の承認プロセスは Live 口座のみ）

※ メールが届かない場合: 5 分待っても来なければ「Resend Verification Email」リンクをフォーム下部から再送。ドメイン `tradovate.com` をホワイトリストへ。

---

## Step 4: Demo 環境の選択（所要 1 分）

ログイン後、アカウント選択画面が表示される：

1. 画面左側に **「Simulation / Demo」** タブ、右側に **「Live」** タブ
2. Demo は **緑色のバッジ**、Live は **赤色のバッジ** で区別される
3. **Simulation** を選択（Live はそもそも未アクティベートで選べない）
4. **「Enter Simulation」/「Launch Demo」** ボタンをクリック

### Demo vs Live の切替方法（参考）
- 画面右上のユーザーアイコン → Dropdown → **「Switch Account」** で Live/Demo を往復可能
- Demo と Live は完全に独立した残高・注文履歴（混ざらない）

---

## Step 5: Platform（取引プラットフォーム）の選択（所要 1 分）

Tradovate は複数の取引画面を提供。Bot 疎通テスト目的では以下推奨：

| Platform | URL / 起動方法 | 推奨度 | 用途 |
|---|---|---|---|
| **Tradovate Web Trader** | https://trader.tradovate.com/ （ブラウザのみ・インストール不要） | ★★★ | 約定UI確認・残高確認 |
| TradingView 連携 | TradingView 上の「Trading Panel」→ Tradovate 選択 | ★★ | Chart+発注を統合したい場合 |
| Desktop App | Windows/Mac アプリDL | ★ | 重装備。今回は不要 |
| Mobile App | iOS/Android | ★ | 外出時確認用 |

**今回は Tradovate Web Trader（ブラウザ版）のみで十分**。Chronos Bot は REST API 経由で発注するので取引画面は約定確認用途のみ。

---

## Step 6: API / Webhook credentials の取得（所要 3〜5 分）

### 6-A. 最小ルート（疎通テストのみ・$25 不要）

**まず username / password だけで REST API を叩けるか試す**。公式 OpenAPI schema では `/auth/accessTokenRequest` の `cid` / `sec` は **optional** フィールド。Demo 環境では username/password のみで accessToken が返るケースが実践者報告にあり（要実測）。

必要情報：
- `name` = Step 2 で登録した email もしくは username（ログイン画面の入力欄に従う）
- `password` = Step 2 で設定したパスワード
- `appId` = 任意文字列（例: `ChronosBot`）
- `appVersion` = 任意文字列（例: `1.0`）
- `deviceId` = SHA256(platform + arch + username) — tradovate_client.py が自動生成

### 6-B. 正規ルート（API Access add-on $25/月・cid/sec 取得）

1. Tradovate Web Trader ログイン状態で、画面右上のユーザーアイコン → **「Application Settings」**
2. 左メニュー **「Add-Ons」** タブ
3. **「API Access」** 項目をクリック → **「Subscribe ($25/month)」** ボタン
4. クレジットカード登録（Demo でも add-on 購入にはカードが必要）
5. 購入完了後、同画面に **「API Access」** タブが新たに出現
6. **「Generate API Key」** ボタン → `cid`（Client ID / 数値）と `sec`（Secret / 長文字列）が表示される
7. **この瞬間のみ表示される**ので必ず両方コピーして安全な場所へ保管（紛失時は再生成・旧キーは無効化）
8. **推奨**: 同画面で **API 専用パスワード** を別途設定（通常ログインパスワードと分離することで漏洩リスク低減）

### 今回の判断（推奨）

**Step 6-A（$25 課金なしルート）を先に試す**。username/password で `/auth/accessTokenRequest` が成功すれば Chronos 疎通テストは完了。失敗したら Step 6-B に進む。理由：

- 疎通テストのみなら cid/sec 不要で済む可能性大（公式schema で optional）
- $25 は Flex プロップ本採用が見えてから払っても遅くない
- Demo アカウント単体で月額 $25 は機会損失

---

## Step 7: 環境変数設定（所要 2 分）

`/Users/yuusakuichio/trading/.env` に以下を追記：

```bash
# ── Tradovate Demo (Chronos Bot 疎通テスト用) ──────────────────
# 既存の tradovate_client.py は TRADOVATE_ENV=DEMO|LIVE で分岐。
# ユーザー指定の命名を尊重しつつ、既存コード互換のため両方書く。
TRADOVATE_MODE=demo

# Demo 専用credentials（ユーザー指定命名）
TRADOVATE_USERNAME_DEMO=<Step 2 で登録した username または email>
TRADOVATE_PASSWORD_DEMO=<Step 2 で設定した password>
TRADOVATE_CID_DEMO=0
TRADOVATE_SEC_DEMO=

# 既存 tradovate_client.py が読む変数（DEMO時はこちらに同値を入れる）
TRADOVATE_ENV=DEMO
TRADOVATE_USERNAME=<同上 username または email>
TRADOVATE_PASSWORD=<同上 password>
TRADOVATE_APP_ID=ChronosBot
TRADOVATE_APP_VERSION=1.0
TRADOVATE_CID=0
TRADOVATE_SEC=
```

### 注意点
- **既存 tradovate_client.py は `TRADOVATE_USERNAME` / `TRADOVATE_PASSWORD` / `TRADOVATE_ENV` を直接読む**。`_DEMO` 接尾辞は自動解釈されない。
- ユーザー指定 `TRADOVATE_USERNAME_DEMO` 等を活かすには `tradovate_client.py` 側に `if TRADOVATE_MODE==demo: fallback to *_DEMO` 的ロジック追加が別タスクで必要（Chronos 側で切替える設計なら既存命名のまま TRADOVATE_ENV 切替でもOK）。
- **.env はコミット禁止**（既に .gitignore 済み確認推奨）。

---

## Step 8: 疎通確認方法（所要 2〜3 分）

### 8-A. REST 認証疎通テスト（curl）

```bash
# Sora Lab 側で実行する curl サンプル（実行は Sora Lab が担当）
curl -sS -X POST "https://demo.tradovateapi.com/v1/auth/accessTokenRequest" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<TRADOVATE_USERNAME_DEMO>",
    "password": "<TRADOVATE_PASSWORD_DEMO>",
    "appId": "ChronosBot",
    "appVersion": "1.0",
    "deviceId": "<SHA256(platform+arch+username)>",
    "cid": 0,
    "sec": ""
  }' | python3 -m json.tool
```

**期待レスポンス（成功時）**:
```json
{
  "accessToken": "eyJhbGc...",
  "mdAccessToken": "eyJhbGc...",
  "expirationTime": "2026-04-22T11:30:00.000Z",
  "userId": 123456,
  "name": "<username>",
  "userStatus": "Active",
  "hasLive": false
}
```

**失敗パターン別対処**:
| エラー | 原因 | 対処 |
|---|---|---|
| `p-ticket`+`p-time` 返却 | captcha 的待機要求 | tradovate_client.py が自動 retry（P_TICKET_MAX_RETRIES=3） |
| `401 Unauthorized` | password 誤り | Step 2 のパスワード再確認 |
| `"errorText": "API access..."` | cid/sec 必須メッセージ | Step 6-B の add-on 購入が必要 |
| `429 Too Many Requests` | rate limit / 認証試行予算超過 | `common/auth_budget.py` で自動抑制。15分待機 |

### 8-B. Tradovate Demo UI で約定確認

Bot が order を発注した後：
1. **https://trader.tradovate.com/** にログイン → Simulation モード選択
2. 画面右側 **「Orders」** タブ → 送信した注文のステータス（Working / Filled / Rejected）確認
3. 画面右側 **「Positions」** タブ → 約定後のポジション確認
4. 画面右側 **「Account Info」** → 残高変動・P&L確認
5. 画面左 **「Alerts」** → reject 時のエラーメッセージ確認

---

## 調査結果サマリー（公式ソース基準）

| 項目 | 結果 | ソース |
|---|---|---|
| KYC 要否 | **不要**（Demoのみ。Liveは必要） | tradovate.com/open-your-trading-account, 複数実践者報告 |
| 費用 | **無料**（cid/sec 取得には別途 $25/月 add-on） | info.tradovate.com/simulated-trading, support記事 |
| 有効期限 | **14日間**（延長はsupport問い合わせ or 有料化） | info.tradovate.com/simulated-trading |
| 初期残高 | **$50,000**（画面から任意増減可） | support.tradovate.com 「Adjusting Your Demo Account Balance」 |
| market data | **real-time**（CME Group 先物全カバー） | info.tradovate.com/simulated-trading |
| 対応銘柄 | Index(ES/MES/NQ/MNQ) / Forex / Crypto / Metal / Energy | 同上 |
| 複数Demo作成 | **可能**（期限切れ後の新規Demoで継続運用する実践者多数） | 実践者報告 |
| Demo→Live 切替 | Web Trader 右上 → Switch Account。Live は別途申請 | 公式UI |
| activation 待ち | **即時**（メール認証後ログイン可） | PickMyTrade等実践者ガイド |
| API アクセス | **可能**（username/pw のみで疎通試行可、cid/sec は $25/月 add-on で取得） | api.tradovate.com, support記事, フォーラム |
| 日本人登録 | **可能**（Demo は国籍制限事実上なし） | tradovate.zendesk.com 国際口座記事 |
| Chronos 用 base URL | `https://demo.tradovateapi.com/v1`（既にハードコード済み） | tradovate_client.py L78 |

---

## 完了チェックリスト（ユーザー作業）

- [ ] Step 1: www.tradovate.com → TRY TRADOVATE NOW クリック
- [ ] Step 2: First/Last/Email/Password/Country=Japan 入力 → Create Account
- [ ] Step 3: Gmail 受信メールの Verify Email クリック
- [ ] Step 4: ログイン → Simulation / Demo 選択
- [ ] Step 5: Web Trader 画面が開くことを確認
- [ ] Step 6: (まずは 6-A の最小ルートで試す) username/password を控える
- [ ] Step 7: 控えた credentials を .env に追記（Sora Lab に渡す）
- [ ] Step 8: Sora Lab が curl 疎通テスト → 成功なら Chronos Bot 発注テスト へ移行

**ここまでで Chronos Bot の Demo 疎通テスト準備が完了する。**
