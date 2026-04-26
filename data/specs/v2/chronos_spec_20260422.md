# Chronos v2 完全仕様書（ゼロから作り直す版）

**作成日**: 2026-04-22
**対象**: ゆうさくさん（主読者）/ 担当エージェント（副読者）
**位置付け**: 現行 `chronos_bot.py` の設計を一度忘れ、マルチ銘柄マルチ戦術前提でゼロ設計する際の完全仕様書
**独立性**: Atlas / Common / Secretary と並列。相互参照ゼロ
**絶対条件**: 推測禁止・嘘禁止・不明は「未確定」と明記。金額・％は極力避ける。

---

## 目次

- Part A: ゆうさくさん向け（非エンジニア視点・読みもの）
  - A1. この Bot が何をするか（30秒サマリ）
  - A2. 扱う銘柄一覧（マルチ銘柄）
  - A3. 扱う戦術一覧（マルチ戦術）
  - A4. MFFU Flex プロップルール（最優先順守）
  - A5. 他プロップ対応（Tradeify / Apex / Bulenox / Topstep）
  - A6. 1 日の運用フロー（24時間市場なので時間帯別）
  - A7. リスクガード
  - A8. 期待される自律度
- Part B: 技術付録（実装者向け）
  - B1. 戦術別詳細仕様
  - B2. プロップルール yaml 駆動設計
  - B3. TradersPost webhook 経路仕様
  - B4. Tradovate demo 認証
  - B5. サイジング（Kelly + プロップ制約）
  - B6. 想定ボトルネック・仕様レベル対処

---

# Part A: ゆうさくさん向け

## A1. この Bot が何をするか（30秒サマリ）

### 目的
- シカゴ先物取引所（CME）の小口先物をプロップファーム（他人の資金を借りて運用する枠組み）経由で自動売買する
- 主目的：MFFU Flex 評価段階を通過し、Sim-Funded（模擬資金）フェーズで毎月 Payout（出金）を取り続ける
- 副目的：Tradeify / Apex / Bulenox の追加プロップを並行稼働し、合算月収を積み上げる

### 対象市場
| 項目 | 内容 |
|---|---|
| 取引所 | シカゴ・マーカンタイル取引所（CME Globex） |
| 商品種別 | E-mini 先物・Micro E-mini 先物 |
| 取引時間 | 月曜 07:00 JST 〜 土曜 06:00 JST（24時間）<br>毎日 06:00-07:00 JST は休止 |
| 週末 | 土曜 06:00 JST 〜 月曜 07:00 JST は全停止 |

（出典: `chronos_agent.py` L540-L569 / `common/market_calendar.py`）

### 対象銘柄（予定）
Micro E-mini 6銘柄 + Mini E-mini 4銘柄の計10銘柄。詳細は A2 参照。

### 扱う戦術（予定）
11戦術（実装済・未実装混在）。詳細は A3 参照。

### 1日の流れ（概要）
| 時間帯（JST） | フェーズ | 内容 |
|---|---|---|
| 07:00-17:00 | アジア時間 | レンジ戦術・VWAP逆張り（低ボラ帯） |
| 17:00-22:30 | 欧州時間 | トレンド追随・ロンドンブレイクアウト |
| 22:30-23:30 | US プレ | ギャップ確認・Asia レンジ終値記録 |
| 23:30-05:00 | US 通常 | ORB・ES-NQスプレッド・VIX期間構造 |
| 05:00-06:00 | US 引け後 | EOD反転・ポジション整理 |
| 06:00-07:00 | 休止 | CME Globex デイリー休止 |

---

## A2. 扱う銘柄一覧（マルチ銘柄）

### 現行（2026-04-22 時点 `chronos_accounts.yaml` L17-L136）
実際に稼働が想定されている銘柄は **MES / MNQ の2銘柄に限定** されている。

```
アカウントA (Flex)     : MES
アカウントB (Rapid)    : MES
アカウントC (Pro)      : MNQ
アカウントD (Core)     : MES  ※ enabled: false（Core廃止）
アカウントE (Builder)  : MES
```

### v2 設計の対象銘柄（拡張）

| 銘柄コード | 種類 | 基になる株価指数 | なぜ扱うか | 扱える戦術 | tick size | 倍率 |
|---|---|---|---|---|---|---|
| MES | Micro E-mini | S&P 500 | 流動性最高・Chronos 既存実装の中心 | 全11戦術 | 0.25pt | $1.25/tick |
| MNQ | Micro E-mini | NASDAQ-100 | テック寄り・ES との相関差が ES-NQ スプレッド戦術に必要 | 全11戦術 | 0.25pt | $0.50/tick |
| ES | E-mini | S&P 500 | MES の 10 倍サイズ。MFFU Eval で大きく動かす場合 | 全11戦術 | 0.25pt | $12.50/tick |
| NQ | E-mini | NASDAQ-100 | MNQ の 10 倍サイズ | 全11戦術 | 0.25pt | $5.00/tick |
| M2K | Micro E-mini | Russell 2000 | 小型株指数・SPY/IWM ギャップ戦術と相性良い | ORB / range break / gap fill | 0.10pt | $0.50/tick |
| MYM | Micro E-mini | Dow Jones 30 | 指数分散（SP500 とは銘柄構成差あり） | trend follow / session | 1.0pt | $0.50/tick |

（出典: `common/prop_firm_rules.yaml` L174-L178 の `same_product_pairs` に MES/ES, MNQ/NQ, MYM/YM, M2K/RTY が定義されている）

### ヘッジ禁止（重要）
MFFU / Tradeify / Apex 共通で、以下のペアの両建て（BUY と SELL 同時保有）は即違反となる：
- MES と ES（同じ S&P500 なので両建て不可）
- MNQ と NQ（同じ NASDAQ なので両建て不可）
- MYM と YM（同じ Dow Jones なので両建て不可）
- M2K と RTY（同じ Russell 2000 なので両建て不可）

（出典: `common/prop_firm_rules.py` `check_hedging()` L350-L388）

### 未確定項目
- M2K / MYM / NQ / ES / RTY / YM / CL（原油）等の本格実装は未着手
- 現行 Bot は MES/MNQ のみで動いている（`chronos_accounts.yaml` L29-L136）

---

## A3. 扱う戦術一覧（マルチ戦術）

**出典**: `chronos_strategy_selector.py` L48-L218 のインポート群に列挙された戦術モジュール

### 戦術一覧

| # | 戦術名 | 日本語での簡単な説明 | 実装ファイル | 実装状態 |
|---|---|---|---|---|
| 1 | ORB | オープニング・レンジ・ブレイクアウト（寄付き30分のレンジを抜けた方向に乗る） | `chronos_bot.py` 内 FuturesORBStrategy | 実装済 |
| 2 | VIX term structure | 恐怖指数 VIX の期先・期近の関係（コンタンゴ/バックワーデーション）で方向判定 | `futures_vix_term_structure.py` | 実装済 |
| 3 | ES-NQ spread | S&P500 と NASDAQ の相対強弱（片方が強いと期待される時にロング/ショートを組む） | `futures_es_nq_spread.py` | 実装済 |
| 4 | Session strategy | 時間帯（Asia / London / US Open / US Midday / US Close）別に適した戦術を切り替え | `futures_session_strategy.py` | 実装済 |
| 5 | Asia range fade | アジア時間の狭いレンジ上下限で逆張り（米国指数はアジア時間に動きにくい） | `futures_asia_range_fade.py` | 実装済 |
| 6 | Gap fill | 前日終値と今日の寄付きの差（ギャップ）が埋まる方向に乗る | `futures_gap_fill_advanced.py` | 実装済 |
| 7 | Volume profile | 価格別出来高（POC / VAH / VAL）の重要価格帯に反応する戦術 | `futures_volume_profile.py` | 実装済 |
| 8 | Economic event | 経済指標（雇用統計・CPI・FOMC）発表後の方向性に反応 | `futures_economic_event.py` | 実装済 |
| 9 | Range break improved | ドンチャンチャネル（過去 N 日の高値・安値）のブレイクで追随（改良版） | `futures_range_break_improved.py` | 実装済 |
| 10 | Cumulative delta | 買い出来高 - 売り出来高の累積値で方向バイアスを測る（F12） | `chronos_cumulative_delta.py` | 実装済（silent failure 監視中） |
| 11 | Liquidity sweep | ストップ狩り（直近高値/安値を一度抜いてすぐ戻る動き）を検出して逆張り（F13） | `chronos_liquidity_sweep.py` | 実装済（silent failure 監視中） |

### 各戦術の環境・手順・損切り/利確

#### 1. ORB（オープニング・レンジ・ブレイクアウト）
| 項目 | 内容 |
|---|---|
| 環境条件 | VIX が elevated（約20以上）かつ ORB窓（09:35-11:00 ET）内 |
| 手順 | 寄付き後30分のレンジ（最高値と最安値）を決定 → そのレンジを明確にブレイクした方向にエントリー |
| 損切り | レンジ反対端（ロング時は下限、ショート時は上限）を割れたら撤退 |
| 利確 | レンジ幅の 1.5〜2.0 倍の値幅、または引け前強制クローズ |
| 適合銘柄 | MES / MNQ / ES / NQ / M2K |
| 損益イメージ | 勝率 50-55%・リスクリワード 1:1.5 程度を想定（バックテスト `data/backtest_orb_1dte_20260418.md` 参照） |

#### 2. VIX term structure
| 項目 | 内容 |
|---|---|
| 環境条件 | VIX 先物の期先が期近より高い（コンタンゴ）= 平常時。逆転（バックワーデーション）= リスクオフ |
| 手順 | term_ratio を計算し、コンタンゴ時はロングバイアス、バックワーデーション時はショートバイアス |
| 損切り | ボラティリティ変化の反転 |
| 利確 | 期間構造の正常化 |
| 適合銘柄 | MES / ES |
| 損益イメージ | 未確定（本番実績なし） |

#### 3. ES-NQ spread
| 項目 | 内容 |
|---|---|
| 環境条件 | S&P500 と NASDAQ の相対強弱が極値（z-score |2| 超）に振れたとき |
| 手順 | 弱い方をロング・強い方をショートで両建て、相対値が平均回帰する動きに乗る |
| 損切り | スプレッドがさらに広がった場合 |
| 利確 | スプレッドの縮小 |
| 適合銘柄 | MES と MNQ のペア（または ES と NQ） |
| 損益イメージ | 未確定（ヘッジ扱いになる可能性要検証。MES/ES は同一プロダクト両建て禁止だが MES/MNQ は別プロダクト） |
| 注意 | MFFU / Tradeify / Apex の「相関ヘッジ禁止」に抵触しないか要確認（`check_hedging()` は MES/MNQ の両建ては同一プロダクト扱いしていないが公式文書で未確定） |

#### 4. Session strategy
| 項目 | 内容 |
|---|---|
| 環境条件 | 時間帯に応じた切替（Asia=レンジ、London=トレンド、US Open=ブレイクアウト、US Midday=平均回帰、US Close=反転） |
| 手順 | 現在セッションを `get_current_session()` で取得し、セッション別戦術を呼び出す |
| 損切り・利確 | 戦術ごと |
| 適合銘柄 | MES / MNQ |
| 損益イメージ | 未確定 |

#### 5. Asia range fade
| 項目 | 内容 |
|---|---|
| 環境条件 | アジア時間（JST 07:00-16:00）で ATR が小さい・範囲が狭い |
| 手順 | レンジ上限到達でショート、下限到達でロング |
| 損切り | レンジの外側（ATR 1.0倍） |
| 利確 | レンジ中央（midpoint） |
| 適合銘柄 | MES |
| 損益イメージ | 未確定 |

#### 6. Gap fill
| 項目 | 内容 |
|---|---|
| 環境条件 | 前日終値から今日の寄付きまで 0.3〜2.0% のギャップ |
| 手順 | ギャップ逆方向（窓埋め方向）にエントリー |
| 損切り | ギャップがさらに拡大 |
| 利確 | 前日終値到達 |
| 適合銘柄 | MES / MNQ / M2K |
| 損益イメージ | 未確定 |

#### 7. Volume profile
| 項目 | 内容 |
|---|---|
| 環境条件 | POC（Point of Control: 出来高最大価格）やVAH/VAL（Value Area の上下限）に価格が到達 |
| 手順 | POC 反発でカウンタートレード、VAH/VAL ブレイクで追随 |
| 損切り | 逆方向のVA外抜け |
| 利確 | 次の出来高ノード |
| 適合銘柄 | MES / MNQ |
| 損益イメージ | 未確定 |

#### 8. Economic event
| 項目 | 内容 |
|---|---|
| 環境条件 | 経済イベント（CPI / NFP / FOMC）の前後 5-30 分。T1 ニュースは前後 2 分ブラックアウト |
| 手順 | 発表後の初期反応方向に追随、または反転を狙う |
| 損切り | 反応方向の逆戻り |
| 利確 | 30 分以内に決済 |
| 適合銘柄 | MES / MNQ / ES / NQ |
| 損益イメージ | 未確定 |
| 注意 | T1 ニュース前後 2 分は MFFU Flex 以外は発注禁止（後述 A4） |

#### 9. Range break improved
| 項目 | 内容 |
|---|---|
| 環境条件 | ドンチャンチャネル（動的 N 日期間）の上下限ブレイク |
| 手順 | チャネル抜けでブレイク方向にエントリー・ATR ベースのストップ |
| 損切り | チャネル内側（ATR 1.0-1.5倍） |
| 利確 | ATR 2.0-3.0倍 |
| 適合銘柄 | MES / MNQ / M2K |
| 損益イメージ | 未確定 |

#### 10. Cumulative delta（F12）
| 項目 | 内容 |
|---|---|
| 環境条件 | Bid/Ask 出来高の累積差（買い優勢/売り優勢）を判定する |
| 手順 | 他の戦術シグナルが出たとき、cumulative delta が逆方向なら発注を抑制 |
| 損切り | 主戦術に依存 |
| 利確 | 主戦術に依存 |
| 適合銘柄 | 全銘柄のフィルターとして適用 |
| 損益イメージ | 単独戦術ではなく他戦術のフィルター |
| 注意 | silent failure 監視中（`check_level4_f12_f13_silent_failure()` in `chronos_agent.py` L908-L964） |

#### 11. Liquidity sweep（F13）
| 項目 | 内容 |
|---|---|
| 環境条件 | 直近の高値/安値を一時的に抜いてすぐ反発する動き |
| 手順 | ストップ狩り完了方向の逆にエントリー |
| 損切り | ストップ狩り高安の外側 |
| 利確 | 前回の swing 安高 |
| 適合銘柄 | MES / MNQ |
| 損益イメージ | 未確定 |
| 注意 | silent failure 監視中（同上） |

---

## A4. MFFU Flex プロップルール（最優先順守）

**出典**: `data/specs/mffu_flex_50k_rules.md`（2026-04-21 確定版）/ `common/prop_firm_rules.yaml` L84-L100 / `chronos_rules_plugin/mffu_flex.py`

### Evaluation 段階（評価フェーズ）

| 項目 | 内容 |
|---|---|
| Profit Target（利益目標） | 口座規模の6%分 |
| Max Loss Limit（最大損失上限・MLL） | 口座規模の4%分・EOD Trailing（引け後のHWMからトレール） |
| Daily Loss Limit（日次損失上限・DLL） | なし（Flex の強み） |
| Min Trading Days（最低取引日数） | 2 日 |
| Consistency Rule | 50%（単日利益が累計利益の50%以内・Eval のみ適用） |
| Max Mini Contracts | 5 mini（ES/NQ/RTY） |
| Max Micro Contracts | 50 micro（MES/MNQ/M2K） |
| Weekend Hold | 禁止（金曜 16:00 ET 全決済必須） |
| Overnight Hold | 許可（Builder以外） |
| News Trading | Eval / Sim-Funded 両方許可 |

### Sim-Funded 段階（評価通過後・模擬資金フェーズ）

| 項目 | 内容 |
|---|---|
| MLL（初回Payout前） | $2,000（EOD Trailing継続） |
| MLL（初回Payout後） | **$100（static）・Survival Mode**（初回Payout直後の最大罠） |
| Daily Loss Limit | なし |
| Consistency | なし（Sim-Funded は免除） |
| T1 News Trading | 許可（Flex Sim-Funded Unrestricted・他プランより強み） |
| Inactivity Rule | 7日間取引なしで口座閉鎖 |
| Contract Table | 利益$0-$1,500: 2 mini / 20 micro |
| | 利益$1,500-$2,000: 3 mini / 30 micro |
| | 利益$2,000以上: 5 mini / 50 micro |

### Payout 初回条件

| 項目 | 内容 |
|---|---|
| Min Winning Days（勝利日数） | 5 日 |
| Min Daily Profit（勝利日定義） | $150 以上 |
| Min Net Profit | $500 以上 |
| Min Withdrawal | $250 |
| Max Withdrawal | 利益の 50% / 1サイクル上限 $5,000 |
| Payout Cycle | 5勝利日＋$500純益達成ごと |
| Profit Split | 80 / 20（トレーダー / MFFU） |

### 自動化ルール（2026-04-21 Oliver 直接回答）

| 項目 | 状態 |
|---|---|
| Semi-automated（EAやカスタムスクリプト・人間監督前提） | **許可** |
| Fully automated（実市場戦略限定） | 許可 |
| TradingView Pine Script → webhook → Python server 経路 | **"semi-managed" として正式許可**（Oliver 回答 4/21） |
| HFT（1日200件以上） | 禁止 |
| AI-driven 完全無人（機械学習） | 禁止 |
| Copy Trading across own accounts（自口座間） | 許可（公式明記） |
| Cross-trader Copy Trading（他人との共有） | 禁止 |
| Hedging（同銘柄同時両建て・相関商品ペア） | 禁止 |
| Device Sharing with other traders | 禁止 |

### T1 ニュース時間窓

| イベント | 対応 |
|---|---|
| FOMC会合・FOMC議事録・雇用統計（NFP）・CPI | イベント時刻の前2分〜後2分は全ポジションFlat必須 |
| 例: 8:30 ET CPI → 8:28:00までに全クローズ → 8:32:00以降再エントリー可 |

### Weekend Hold

| 時刻 | 対応 |
|---|---|
| 金曜 16:00 ET | 全ポジション決済必須 |
| 土曜 06:00 JST〜月曜 07:00 JST | 全停止 |

---

## A5. 他プロップ対応（Tradeify / Apex / Bulenox / Topstep）

**出典**: `common/prop_firm_rules.yaml` L123-L165 / `data/tradeify_full_spec_20260420.md` / `CURRENT_STATE.md` L60-L104

### プロップ別ルール差分

| 項目 | MFFU Flex | Tradeify Lightning $50K | Apex $50K |
|---|---|---|---|
| 購入形態 | $127/月 | $295 one-time | $17-90（割引時） |
| Evaluation | 2日〜 | **なし（Instant Funded）** | あり |
| DLL | なし | $1,250 | なし |
| MLL | $2,000 | $2,500（EOD Trailing） | $2,500 |
| Drawdown Lock | なし | balance > $50,100 で DD ロック | なし |
| Consistency | Eval のみ 50% | 20%/25%/30%（payout 1-3回目+ tier） | Safety Net中 30%・Payout後 50% |
| Max Mini Contracts | 5 | 4 | 情報不足 |
| Max Micro Contracts | 50 | 40 | 情報不足 |
| Mini/Micro 併用 | 可 | 禁止 | 情報不足 |
| Profit Split | 80/20 | 90/10 常時 | 90/10（$25K 閾値後） |
| News Trading | 許可 | 制限なし | 情報不足 |
| HFT | 200+/日 禁止 | 10秒未満保有50%+ 禁止 | 情報不足 |
| DCA（損失ポジ追加） | 制限なし | 制限なし | PA口座で自動失効 |
| Payout Cap | $5,000/cycle | $1,000/cycle | 情報不足 |
| Max Concurrent Funded | 未確定 | 5（combined $750K cap） | 20 |
| Reset 可否 | 新規購入扱い | **なし（違反即口座消滅）** | 情報不足 |
| 日本ユーザー | 登録可 | 登録可・VPN禁止 | 情報不足 |
| 法人契約 | FFFレベルKYB・要書面承認 | 可（Articles+EIN+Operating Agreement+Beneficial Ownership） | 可（EIN 必須・Form SS-4） |

### 4/21 時点の最新仕様変更

| Firm | 変更内容 |
|---|---|
| MFFU | 料金改定 $107 → $127/月（2026-04-21 時点の確定値） |
| MFFU Rapid | 旧記載「intraday_trailing_4pct」は誤り。Eval は EOD、Sim Funded のみ Intraday Trailing（2026-04-20 修正） |
| Tradeify Lightning | profit_split 常時 90%（threshold $15K 撤回）・activation fee $0 |
| Tradeify Lightning | consistency 20%/25%/30% tier 実装必須・max contracts 4 mini/40 micro |

### Bulenox / Topstep
| Firm | 状態 |
|---|---|
| Bulenox | 未登録・情報不足（`CURRENT_STATE.md` L57） |
| Topstep | US-based LLC 限定で日本 GK 不可・個人契約必須（`CURRENT_STATE.md` L139） |
| FundedNext / Elite / Earn2Trade | 検討中・情報不足 |

### 法人契約可否（4/21 確定）

| 区分 | Firm | 備考 |
|---|---|---|
| 法人契約可 | Apex / Tradeify / Bulenox | 明示許可 |
| 法人契約可（要書面承認） | MFFU | FFFレベル KYB |
| 個人契約必須 | Topstep | US-based LLC 限定で日本 GK 不可 |

---

## A6. 1 日の運用フロー（24時間市場なので時間帯別）

**出典**: `futures_session_strategy.py` / `chronos_strategy_selector.py` L237-L348 / `common/market_calendar.py`

### JST 時系列フロー

| 時刻（JST） | 時刻（ET） | セッション | 主戦術 | 注意事項 |
|---|---|---|---|---|
| 07:00 | 17:00 前日 | CME再開（取引日カウント切替） | - | Globex Open |
| 07:00-16:00 | 17:00-02:00 | アジア時間 | Asia range fade / VWAP reversion | 低ボラ・レンジ戦術優先 |
| 16:00-21:30 | 02:00-07:30 | 欧州時間 | Trend follow / London breakout | 欧州指標発表に注意 |
| 21:30-22:30 | 07:30-08:30 | US プレ | Gap fill 準備 / Asia レンジ終値記録 | 寄付き前静粛 |
| 22:30-23:30 | 08:30-09:30 | US プレ（経済指標） | Economic event | 8:30 ET T1 指標（CPI/NFP）前後 2分ブラックアウト |
| 23:30-00:05 | 09:30-10:05 | US 寄付き 30分 | ORB レンジ決定 | **新規エントリー原則回避**（レンジ確定中） |
| 00:05-01:00 | 10:05-11:00 | US オープン窓 | ORB ブレイク / Level trading | ORB A+ セットアップ帯 |
| 01:00-04:00 | 11:00-14:00 | US ミッドデー | Volume profile / Range trade | 低ボラ・控えめサイズ |
| 04:00-04:55 | 14:00-14:55 | US 引け前 | Session break / Gap fill 手仕舞い | 引け向けポジション整理 |
| 04:55-05:00 | 14:55-15:00 | 引け前 5 分 | **Builder 全決済ライン** | Builder プラン 15:55 ET 強制クローズ |
| 05:00-05:05 | 15:00-15:05 | US 引け | EOD reversal | 引け値確定 |
| 05:05-06:00 | 15:05-16:00 | US 引け後 | Overnight entry 判断 | 翌日持ち越し（Builder 以外） |
| 05:00 金曜 | 15:00 金曜 | 週末クローズ準備 | **全プロップで週末持ち越し禁止** | 金曜 16:00 ET 全決済 |
| 06:00-07:00 | 16:00-17:00 | CME デイリー休止 | - | 取引不可・再開 07:00 JST |
| 土 06:00 JST 〜 月 07:00 JST | - | 週末全停止 | - | 取引不可 |

### ET で重要な境界

| 境界 | 意味 |
|---|---|
| 17:00 ET | CME の取引日カウンタが切替（HFT カウントのリセット基準） |
| 16:00 ET 金曜 | MFFU 全プラン全ポジション決済ライン（Weekend Hold 禁止） |
| 15:55 ET 毎日 | Builder プラン強制クローズライン |
| 16:00 ET 毎日 | 通常の「引け」（EOD Trailing の更新タイミング） |

---

## A7. リスクガード

### 1. 銘柄別 kill switch

| レイヤー | 内容 | 実装先 |
|---|---|---|
| 戦術別 | 特定戦術の連敗で当該戦術停止 | `common/kill_switch.py` |
| 銘柄別 | 特定銘柄で異常変動検知時に当該銘柄停止 | `common/kill_switch.py`（銘柄キー拡張必要） |
| アカウント別 | 個別口座のMLL接近で当該口座のみ停止 | `chronos_agent.py` L638-L652 / `check_level1_bot_alive()` |
| 全体 | 合算DDライン突破で全口座停止 | `chronos_accounts.yaml` L139-L153 `combined_rules` |

### 2. プロップルール違反の物理ガード（Layer PF-1 〜 PF-4）

**出典**: `common/prop_firm_rules.py` / `data/prop_firm_countermeasures_design.md`

| Layer | 内容 | 実装関数 |
|---|---|---|
| PF-1 Pre-Trade | 発注前に10項目チェック（MLL / DLL / Consistency / 枚数 / HFT / Microscalp / Hedge / T1 News / DCA / Inactivity） | `check_prop_firm_compliance()` L540-L707 |
| PF-2 Post-Fill | 約定後のポジション整合チェック | 未確定 |
| PF-3 EOD | 日次引け時にEOD Drawdown再計算 | 未確定 |
| PF-4 Payout Freeze | Consistency 予兆（目標の90%到達）で追加エントリー停止 | `check_payout_eligibility_with_freeze()` L506-L535 |

### 3. Cross-account guard（複数アカウント間の重複トレード防止）

**出典**: `chronos_accounts.yaml` L137-L153

| ガード | 値 | 目的 |
|---|---|---|
| timing_jitter_s | 各アカウント個別（A:0-30秒 / B:60-120秒 / C:90-180秒 / E:30-90秒） | 同一タイミング発注回避（Copy trading判定回避） |
| inter_account_timing_gap_s | 10秒 | アカウント間発注最低間隔 |
| max_concurrent_orders | 10 | 全アカウント合算の同時発注上限 |
| combined_weekly_dd_usd | 5,000 | 合算週次DD上限（自主規律・MFFU未公開） |
| vps_ip | 各アカウントpre-assigned | デバイス共有禁止対応識別子 |

### 4. 人間監視（Human Oversight）

**出典**: `chronos_accounts.yaml` L155-L174

| 項目 | 値 | 目的 |
|---|---|---|
| 朝承認 | 09:20 ET | マーケットオープン前 |
| 昼承認 | 12:00 ET | ランチ帯 |
| 引け前承認 | 15:30 ET | 引け前判断 |
| Max Unapproved Hours | 6時間 | 未承認で発注停止 |
| Pushover 承認リクエスト | 有効 | MFFU「完全無人AI禁止」対応 |

### 5. Kill Switch 優先順位

**出典**: `chronos_agent.py` L1036-L1047

1. `common.kill_switch.is_active()` → 全停止
2. `chronos_agent_state.json.manual_halt` → Level1（生死監視）のみ継続
3. 通常サイクル → 全 Level1-4 チェック実行

---

## A8. 期待される自律度

### 自律でできること

| 項目 | 内容 |
|---|---|
| 戦術選択 | VIX / IVR / セッション / 残高 から動的に最適戦術を選ぶ |
| サイジング | Kelly ベースの分率算出（`common/kelly_sizer.py`）+ プロップ枚数制約 |
| プロップルール遵守 | PF-1 の10項目を発注前に自動チェック |
| 市場時間判定 | CME Globex の 24時間 + 週末休止を自動認識 |
| 自己回復 | Bot死亡検知 → 自動再起動（Level2 AUTOFIX） |
| 違反検知 | News Window / HFT / Survival Mode / F12/F13 silent failure |
| Pushover 通知 | CRITICAL / BATCHED / SILENT の3段階ゲート |
| 合算監視 | 複数アカウントを横断で監視し、合算DD超過で全停止 |

### 人間判断に依存すること

| 項目 | 頻度 | 理由 |
|---|---|---|
| 承認ウィンドウ内の確認 | 1日3回（09:20 / 12:00 / 15:30 ET） | MFFU「完全無人AI禁止」対応 |
| プロップ規約変更の反映 | 規約改定都度 | 公式文書の直接確認が必須 |
| 本番移行判断 | 2サイクル完走毎 | Red Team NO-GO 条件を人間が評価 |
| manual_halt 解除 | Level4 HALT 発生時 | `python3 chronos_agent.py --unhalt` |
| 新 Firm 追加 | 新規契約都度 | KYC / 税務 / 法人判断が必要 |

### 期待できない自律度

| 項目 | 理由 |
|---|---|
| 完全無人運転 | MFFU が禁止（AI-driven完全無人禁止） |
| 機械学習による戦術選択 | MFFU が禁止（ルールベースのみ許可） |
| 新戦術の自己発見 | ゆうさくさんの承認が必要 |
| 料金改定の追随 | 公式サイトの人手確認が必要 |

---

# Part B: 技術付録

## B1. 戦術別詳細仕様（11戦術）

### 実装ファイル対応表

| # | 戦術 | ファイル | 主要クラス・関数 |
|---|---|---|---|
| 1 | ORB | `chronos_bot.py` | FuturesORBStrategy（apex_bot.py より流用） |
| 2 | VIX term structure | `futures_vix_term_structure.py` | VIXTermStructureStrategy |
| 3 | ES-NQ spread | `futures_es_nq_spread.py` | ESNQSpreadStrategy |
| 4 | Session strategy | `futures_session_strategy.py` | SessionBasedStrategy / `get_current_session()` / `select_mffu_strategies()` |
| 5 | Asia range fade | `futures_asia_range_fade.py` | AsiaRangeFadeStrategy / `is_asia_session()` |
| 6 | Gap fill advanced | `futures_gap_fill_advanced.py` | GapFillAdvancedStrategy / `check_gap_fill_entry()` |
| 7 | Volume profile | `futures_volume_profile.py` | VolumeProfileStrategy / `calc_volume_profile()` |
| 8 | Economic event | `futures_economic_event.py` | EconomicEventStrategy |
| 9 | Range break improved | `futures_range_break_improved.py` | RangeBreakImprovedStrategy / `calc_donchian_channel()` / `calc_dynamic_donchian_period()` |
| 10 | Cumulative delta | `chronos_cumulative_delta.py` | CumulativeDelta / `calc_bid_ask_delta()` / `calc_volume_ratio()` |
| 11 | Liquidity sweep | `chronos_liquidity_sweep.py` | LiquiditySweepDetector / SweepSignal |

### Selector 統合ポイント

**出典**: `chronos_strategy_selector.py` L389-L末尾（`select_futures_strategy()`）

```
入力: env dict
  - vix: float
  - vix_history: list[float] (60日)
  - vix_z: float (20日Z-score)
  - time_et: HH:MM
  - gap_pct: float
  - account_pnl_day: float
  - account_pnl_month: float
  - session: "asia" | "london" | "us_open" | "us_midday" | "us_close"
  - atr_14d: float
  - atr_history_60d: list[float]
  - consistency_ratio: float (今日/月間)

出力: list[dict]
  [{"strategy": str, "size_pct": float, "reason": str}, ...]
```

### 環境適応規律

**出典**: `CLAUDE.md` 「固定パラメータは環境適応ではない」/ `feedback_no_fixed_params.md`

- VIX 閾値は `compute_dynamic_vix_thresholds()`（過去60日分位数）から算出
- ATR レジームは `get_atr_regime()`（過去60日のP25/P75/P90）から分類
- ドンチャン期間は `calc_dynamic_donchian_period()` で動的算出
- Consistency Safety は 35%（40%ルールに5%バッファ）固定だが規律値なので許容

### 実装状態と silent failure 監視

- F12 / F13（Cumulative delta / Liquidity sweep）は silent failure 疑い時に `chronos_agent.py` L908-L964 が検知
- state.json の `f12_cumulative_delta_bias` / `f13_liquidity_sweep_signal` フィールドが None の場合に警告
- `chronos_rules.yaml` の `cumulative_delta.enabled` / `liquidity_sweep.enabled` で無効化可能

---

## B2. プロップルール yaml 駆動設計

### 設計原則

**出典**: `common/prop_firm_rules.yaml` コメント L5

```
全プロップファーム契約ルールの単一管理 YAML
改定時はこの YAML のみ更新（コード変更不要）
```

### ファイル構造

```yaml
meta:
  version: str
  last_updated: date
  fx_rate_jpy_per_usd: int
  rapid_enabled: bool  # Phase A 完了まで Rapid 起動禁止

firms:
  {firm_name}:
    {plan_name}:
      account_size: int
      mll: int
      drawdown_type: str  # eod_trailing | eod_static | intraday_trailing_4pct | eod_trailing_3pct
      daily_loss_limit: int | null
      min_trading_days: int
      consistency_eval_pct: float | null
      consistency_funded_pct: float | null
      max_contracts_mini: int
      max_contracts_micro: int
      profit_target: int
      profit_split: float
      payout_*: ...
      t1_news_funded_allowed: bool
      # Flex固有
      mll_after_first_payout: int
      max_contracts_mini_funded_tiers: list
      inactivity_max_days: int

common_prohibited:
  hft:
    max_trades_per_day: int
    microscalping_min_hold_sec: int
    microscalping_10sec_ratio_max: float
  hedging:
    same_product_pairs: list of [sym, sym]
    max_overlap_sec: int
  collusion:
    cross_account_min_delay_sec: int
    identical_strategy_detection: bool
  news:
    t1_events: list
    blackout_window_sec: int
  device_sharing:
    machine_fingerprint_required: bool
    multiple_user_same_ip_prohibited: bool
```

### プラグイン構造

**出典**: `chronos_rules_plugin/` ディレクトリ

```
chronos_rules_plugin/
  __init__.py              # PropFirmRules 抽象クラス / register_plugin
  mffu_flex.py             # MFFU Flex 実装
  mffu_core.py             # MFFU Core 実装
  mffu_pro.py              # MFFU Pro 実装
  mffu_rapid.py            # MFFU Rapid 実装
  mffu_builder.py          # MFFU Builder 実装
  tradeify_lightning.py    # Tradeify Lightning 実装
  # 追加予定: apex_pa.py / bulenox_instant.py
```

### 統合チェック関数

**出典**: `common/prop_firm_rules.py` L540-L707

```python
check_prop_firm_compliance(
    firm: str,        # "mffu" | "tradeify" | "apex"
    plan: str,        # "flex_50k" | "lightning_50k" | "apex_50k"
    phase: str,       # "evaluation" | "sim_funded" | "funded" | "pa" | "live"
    account_state: dict,
    order_ctx: dict,
) -> tuple[bool, str, str]
    # returns (allow, layer, reason)
    # layer: "PF-1-PASS" | "PF-1-MLL" | "PF-1-DLL" | "PF-1-CON" |
    #        "PF-1-QTY" | "PF-1-HFT" | "PF-1-MSC" | "PF-1-HEDGE" |
    #        "PF-1-NEWS" | "PF-1-DCA" | "PF-1-INACT"
```

### 予兆ブロック設計

| 項目 | 予兆閾値 | 実装関数 |
|---|---|---|
| MLL | 80%到達で発注停止 | `check_mll_breach()` L74-L129 |
| DLL | 80%到達で発注停止 | `check_daily_loss_limit()` L132-L152 |
| Consistency | 目標の90%到達で Payout Freeze | `check_consistency()` L155-L207 / `check_payout_eligibility_with_freeze()` L506-L535 |
| HFT | 180件到達でブロック（200の90%） | `check_hft_daily_count()` L288-L308 |
| Microscalping | 40%到達で警告（Tradeify 50%の80%） | `check_microscalping()` L311-L347 |
| Inactivity | 6日目でアラート | `check_inactivity()` L476-L503 |

---

## B3. TradersPost webhook 経路仕様

**出典**: `data/specs/chronos_webhook_contract.json` / `data/specs/chronos_webhook_connection_info.md` / `memory/project_session_20260421_night_complete.md`

### 稼働実績（2026-04-21 21:44 JST）

```
signal: curl → TradersPost webhook → Paper broker → 138ms Filled
trade: Buy @ $7175.00 MESM2026 Completed
```

### Webhook URL 構造

**出典**: `memory/project_session_20260421_night_complete.md` L50-L53

```
Webhook URL auth: {uuid}/{password} URL 内埋込 (HMAC 不要)
Strategy uuid: 8d125cd0-1fe5-45d6-ba72-d6a51fe95c5e
環境変数: .env TRADERSPOST_WEBHOOK_URL_PAPER 保存済
```

### 経路の正当性（2026-04-21 Oliver 回答）

**出典**: `memory/project_mffu_webhook_architecture_approved_20260421.md`

> "TradingView Pine Script → webhook → Python server → Python server からTradingView経由発注・Human daily monitoring" = **"semi-managed"として正式許可**

- MFFU Flex 公式 integration partner
- Paper broker で MES/MNQ 先物 paper 発注可
- Free tier = $0 forever・クレカ不要

### 経路の制約

| 項目 | 内容 |
|---|---|
| MFFU Flex API 直接 | **不可**（Oliver 公式回答 "no API access will be available"） |
| Tradovate API 直接 | Prop firm 口座では不可（業界標準） |
| TradingView Paper Broker 自動化 | 公式不可（全 tier） |
| TradersPost 経由 | 稼働確定 |

### 8日目以降の挙動

**出典**: `memory/project_session_20260421_night_complete.md` L54

> 8日目以降 paper auto-submit 挙動未検証

**未確定**: Free tier の auto-submit が 8日目以降に制限される可能性あり。2026-04-28 以降に要検証。

---

## B4. Tradovate demo 認証

**出典**: `CURRENT_STATE.md` L249-L254 / `data/specs/tradovate_demo_signup_guide_ja.md` / `data/research/tradovate_mffu_eval_api_20260421.md`

### 認証仕様

| 項目 | 内容 |
|---|---|
| Demo CID | 0 |
| Demo SEC | 空文字列 |
| Live CID | 発行必要（$25/月 add-on・LIVE口座+$1K必須・Demo 不可） |
| Prop firm 口座 API 直接 | 不可（業界標準） |

### リトライ仕様

| 項目 | 内容 |
|---|---|
| p-ticket retry | 実装済（`tradovate_client.py`） |
| p-captcha 検知 | retry 禁止・1h待機 |
| token cache | 30分前から有効（`data/tradovate_auth_cache.json`） |
| renew cache | 実装済 |
| auto invalidate | 実装済 |

### auth_budget 物理制限

**出典**: `common/auth_budget.py` / `CLAUDE.md` 鉄則2

| 項目 | 内容 |
|---|---|
| 上限 | 5/hour（Tradovate）・3/24h（OpenD） |
| 緊急解除 | `AUTH_BUDGET_BYPASS=1`（OpenD）/ `TRADOVATE_AUTH_BYPASS=1`（要確認） |
| 失敗時 | Pushover 即通知・1h 自動待機 |

### 42テスト PASS 状況

**出典**: `CURRENT_STATE.md` L254-L256

- mutation 100%
- env whitelist / HMAC / p-time clamp / renew cache / auto invalidate / .gitignore（38テスト PASS）
- Webhook Server Phase 1A+2 実装完了

---

## B5. サイジング（Kelly + プロップ制約）

### Kelly 分率

**出典**: `common/kelly_sizer.py` / `chronos_bot.py` L202-L230

```python
get_kelly_fraction(
    plan_id: str,        # "mffu_flex_50k" | "tradeify_lightning_50k" 等
    win_rate: float = 0.55,
    rr_ratio: float = 1.30,
    half_kelly: bool = True,
) -> float
```

| 項目 | 内容 |
|---|---|
| Kelly 公式 | f = (bp - q) / b = (win_rate×rr - loss_rate) / rr |
| Half Kelly | デフォルト有効（リスク半減） |
| Plan別キャリブレーション | プランごとの過去実績で win_rate / rr を調整 |

### プロップ枚数上限の適用順

**出典**: `common/prop_firm_rules.py` `check_max_contracts()` L210-L248

1. Kelly 分率 × account_size = 理論サイズ（USD）
2. 理論サイズ / 単契約証拠金 = 理論枚数
3. プラン別 max_contracts_mini / max_contracts_micro でクリップ
4. Flex Sim-Funded のみ: 残高連動テーブル（max_contracts_mini_funded_tiers）で更にクリップ
5. Builder プランのみ: daily_loss_limit_soft_pause / payout_cap_per_cycle でクリップ

### ATR レジーム調整

**出典**: `chronos_strategy_selector.py` `apply_atr_regime_to_size()` L312-L338

| regime | 乗数 | 意味 |
|---|---|---|
| low | 0.8 | 低ボラ: 機会が少ない → 縮小 |
| normal | 1.0 | 通常: 変化なし |
| high | 1.2 | 高ボラ: ORBトレンド環境 → 拡大 |
| extreme | 0.5 | 超高ボラ: 逆張り抑制・損失リスク急増 → 大幅縮小 |

### Consistency Safety 予防ブロック

**出典**: `chronos_strategy_selector.py` L351-L386 / `CONSISTENCY_SAFETY_PCT = 0.35`

- 今日の利益 / 月間累積利益 が 35% を超えそうなら発注停止
- MFFU Consistency Rule 50%（Flex Eval）に対して 15% のバッファ

---

## B6. 想定ボトルネック・仕様レベル対処

### 優先度順リスト

| # | ボトルネック | 現状 | 仕様レベル対処 |
|---|---|---|---|
| 1 | Tradovate auth budget 5/hour | 物理実装済（`common/auth_budget.py`） | token cache 30分 + retry 1h待機 |
| 2 | Pushover 月10,000件枠超過 | 4,560件/月に圧縮済 | dedup + batch 3層 + Quiet Hours（22:00-04:00 JST） |
| 3 | MFFU プロップルール動的変更 | YAML 駆動 | `common/prop_firm_rules.yaml` 1ファイル更新でコード変更なし |
| 4 | TradersPost 8日目以降 auto-submit 挙動 | 未検証 | 2026-04-28 以降に手動検証・必要なら Essential tier $29/月 |
| 5 | MFFU Flex API 不可 | 確定（Oliver回答） | TradersPost webhook 経路に一本化 |
| 6 | Tradovate API Prop口座不可 | 確定（業界標準） | TradersPost 経路に一本化 |
| 7 | TradingView Paper 自動化不可 | 確定 | TradersPost Paper broker に一本化 |
| 8 | F12/F13 silent failure | 監視実装済 | `chronos_agent.py` L908 で state フィールド None 検知 |
| 9 | Chronos Bot 多重起動 | PID lock 実装済 | `data/accounts/<id>/pid.lock` + `os.kill(pid, 0)` 生存確認 |
| 10 | LaunchAgent JST Hour ズレ | 既知 | `feedback_launchd_jst.md` 規律で Hour 値は JST で記述 |
| 11 | Copy trading 判定 | timing_jitter + inter_account_gap で回避 | `chronos_accounts.yaml` の timing_jitter_s 個別設定 |
| 12 | Device sharing 禁止（MFFU） | vps_ip 個別識別子で対応 | 各アカウント pre-assigned 識別子 |
| 13 | HFT 200件/日超過 | 180件でブロック | `check_hft_daily_count()` + CME 17:00 ET境界でカウンタリセット |
| 14 | Microscalping 検知 | 40%で警告 | `check_microscalping()` + entry_ts/exit_ts 型バリデーション |
| 15 | T1 ニュース窓 | 前後2分ブロック | `check_t1_news_blackout()` + abs() 廃止（未来/過去分離） |
| 16 | Consistency Rule 違反 | 35%で予防ブロック | `check_consistency_safety()` + `check_payout_eligibility_with_freeze()` |
| 17 | Inactivity 7日失効（Flex Sim-Funded） | 6日目でアラート | `check_inactivity()` + ET日付基準 |
| 18 | Sim-Funded 初回Payout後 $100 Survival Mode | Level4 検知実装済 | `check_level4_sim_funded_payout_mode()` |
| 19 | 承認ウィンドウ未遵守 | 6時間で発注停止 | `human_oversight.max_unapproved_hours: 6` |
| 20 | state.json 改ざん | 起動時再集計 | `common/chronos_state_rebuild.py` + trade_log/audit log を真実の源とする |
| 21 | DST 切替（2026/11/1 冬時間） | ハードコード（夏時間優先） | `common/market_calendar.py` 更新必要 |
| 22 | Kill Switch 優先順位 | 実装済 | `common.kill_switch` → `manual_halt` → 通常サイクル |
| 23 | 合算DDライン監視 | $5,000（自主規律） | `combined_rules.combined_weekly_dd_usd: 5000` |
| 24 | fleet_watcher 役割分担 | 実装済 | chronos_agent: 生死・規約 / fleet_watcher: 合算DD・hedging |
| 25 | 外部死活監視（Pushover 独立） | 実装済 | `common/external_health_ping` + Healthchecks.io 2分毎 |

### 未確定・将来検証項目

| # | 項目 | 検証予定 |
|---|---|---|
| U1 | ES/NQ ペアでの相関ヘッジ判定 | 公式 support 質問が必要 |
| U2 | MES/MNQ 異プロダクト両建ての扱い | 公式 support 質問が必要 |
| U3 | TradersPost Free tier 長期安定性 | 2026-05 以降に実測 |
| U4 | 法人契約時の MFFU KYB 所要日数 | 申請時実測 |
| U5 | Tradeify Lightning の consistency 20%/25%/30% tier の実装精度 | 購入後検証 |

---

## 付録: ファイル・コードパス一覧

### 実装ファイル（既存）

| ファイル | 役割 |
|---|---|
| `/Users/yuusakuichio/trading/chronos_bot.py` | メインBot（約33K tokens） |
| `/Users/yuusakuichio/trading/chronos_agent.py` | 常駐監視エージェント（1211行） |
| `/Users/yuusakuichio/trading/chronos_watchdog.py` | 最小版watchdog |
| `/Users/yuusakuichio/trading/chronos_strategy_selector.py` | 戦術選択エンジン |
| `/Users/yuusakuichio/trading/chronos_pre_trade_check.py` | Pre-trade チェック |
| `/Users/yuusakuichio/trading/chronos_rule_simulator.py` | MFFUルール本体 |
| `/Users/yuusakuichio/trading/chronos_accounts.yaml` | 5アカウント設定 |
| `/Users/yuusakuichio/trading/chronos_rules.yaml` | Agentルール + MFFU設定 |
| `/Users/yuusakuichio/trading/common/prop_firm_rules.py` | PF-1 Pre-Trade 統合チェック |
| `/Users/yuusakuichio/trading/common/prop_firm_rules.yaml` | 全プロップルール YAML |
| `/Users/yuusakuichio/trading/common/kelly_sizer.py` | Kelly 分率計算 |
| `/Users/yuusakuichio/trading/common/kill_switch.py` | Kill Switch |
| `/Users/yuusakuichio/trading/common/market_calendar.py` | 市場時間判定 |
| `/Users/yuusakuichio/trading/common/chronos_state_rebuild.py` | state.json 再集計 |
| `/Users/yuusakuichio/trading/chronos_rules_plugin/mffu_flex.py` | MFFU Flex プラグイン |
| `/Users/yuusakuichio/trading/chronos_rules_plugin/mffu_builder.py` | MFFU Builder プラグイン |
| `/Users/yuusakuichio/trading/chronos_rules_plugin/tradeify_lightning.py` | Tradeify Lightning プラグイン |
| `/Users/yuusakuichio/trading/futures_*.py` | 13戦術モジュール |
| `/Users/yuusakuichio/trading/tradovate_client.py` | Tradovate API クライアント |

### ドキュメント

| ファイル | 役割 |
|---|---|
| `/Users/yuusakuichio/trading/data/specs/mffu_flex_50k_rules.md` | MFFU Flex 公式仕様（確定版） |
| `/Users/yuusakuichio/trading/data/specs/chronos_webhook_contract.json` | Webhook 契約 |
| `/Users/yuusakuichio/trading/data/specs/pine_script_setup_guide.md` | Pine Script 手順 |
| `/Users/yuusakuichio/trading/data/specs/tradovate_demo_signup_guide_ja.md` | Tradovate demo 手順 |
| `/Users/yuusakuichio/trading/data/prop_firm_countermeasures_design.md` | PF-1〜PF-4 設計書 |
| `/Users/yuusakuichio/trading/data/research/tradovate_mffu_eval_api_20260421.md` | API認証調査 |
| `/Users/yuusakuichio/trading/CURRENT_STATE.md`（memory） | 今日の真実スナップショット |

### 禁止事項・規律

| 規律 | 出典 |
|---|---|
| 固定パラメータ禁止（動的算出必須） | `feedback_no_fixed_params.md` |
| 実装前7ステッププロセス | `feedback_implementation_process.md` |
| E2E テスト必須 | `feedback_bot_integration_test.md` |
| 選択的テスト禁止（全体pytest） | `feedback_no_selective_testing.md` |
| 独立検証必須（Blue Team自己採点禁止） | `feedback_independent_verification_mandatory.md` |
| Pushover タグ規約（[Chronos] プレフィックス） | `project_pushover_tag_convention.md` |

---

## 作成メモ

- 本ファイルは v2 draft。本番適用前に Atlas / Common agent の並列成果物とマージ判断が必要
- 未確定項目は明示（A3 の損益イメージ / B6 の U1-U5 など）
- 金額・% は極力避けたが、プロップルール（MLL $2,000・Consistency 50% 等）は公式値のため記載
- 実装時は「7ステッププロセス」を飛ばさない（CLAUDE.md 鉄則7）
