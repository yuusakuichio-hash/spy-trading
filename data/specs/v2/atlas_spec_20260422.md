---
title: Atlas 完全仕様書 v2 draft
subtitle: SPY/SPX オプション・moomoo経由 — ゆうさくさん向け + 技術付録
version: v2-draft
created: 2026-04-22
author: strategist agent (独立担当)
scope: Atlas のみ（Chronos/Common仕様書とは別領域・相互参照なし）
status: draft
source_of_truth:
  - /Users/yuusakuichio/trading/spy_bot.py
  - /Users/yuusakuichio/trading/common/
  - /Users/yuusakuichio/trading/atlas_rules.yaml
  - /Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory/CURRENT_STATE.md
  - /Users/yuusakuichio/trading/data/research/minimum_spec_20260422.md
---

# Atlas 完全仕様書 v2 draft

> **この仕様書は「Atlas を今のコード資産から分離して、どんなBotなのか・どう動くべきなのか」を整理したものです。**
> **前半（Part A）はゆうさくさんが最初に読む章です。技術用語は括弧書きで平易化しています。**
> **後半（Part B）は開発者（builder/ops）と redteam（独立検証）向けの付録です。**
> **ここに書いてある内容は、すべて既存コード・公式仕様・atlas_rules.yaml から抽出した事実です。推測・一般論は入っていません。**

---

# Part A ゆうさくさん向け（平易版）

## A1. このBot が何をするか（30秒サマリ）

| 項目 | 内容 |
|---|---|
| 目的 | 米国株式市場のオプション（株式・指数を買う権利・売る権利を売買する契約）を使って、日中に小さく勝ちを積み上げる |
| 対象市場 | 米国株（ニューヨーク証券取引所・NASDAQ・CBOE指数オプション） |
| 対象銘柄 | 高流動性のETF・指数・個別株10-11銘柄（詳細はA2） |
| 扱う戦術 | 環境に応じて最大10種類を自動切替（詳細はA3） |
| 1日の大まかな流れ | 朝に環境観測→ 戦術候補選定 → 市場オープン後にエントリー → 場中監視 → クローズ前に決済（詳細はA4） |
| 運用資金 | ペーパー口座（仮想・moomoo paper）と本番口座（moomoo 2756）の並行運用 |
| 税制 | SPX は Section 1256（税制優遇対象）・他は雑所得 |

**一言でいうと**: 「市場環境（ボラティリティ・方向性・流動性）を毎朝観測して、今日その瞬間に最も期待値が高い戦術と銘柄の組み合わせを自動選択するBot」です。固定戦術ではなく、環境に応じて別の戦術に切り替える「環境適応型」として設計されています。

**根拠**: `atlas_rules.yaml` の `symbol_meta.allowed_universe` / `tactics` 群・`CURRENT_STATE.md` の戦術別月利表 / `feedback_no_fixed_params.md`（固定パラメータ禁止規律）

---

## A2. 扱う銘柄一覧

### 銘柄表

| 銘柄 | 種類 | なぜ扱うか | 扱える戦術 | 制約・注意 |
|---|---|---|---|---|
| SPY | S&P500 ETFオプション | 流動性が世界最大クラス・スプレッド（売買価格差）が狭い・ペーパー口座でも扱える | ほぼ全戦術（CS/IC/Straddle/Butterfly/Calendar/Strangle/ORB1DTE） | ストライク刻みは1ドル単位 |
| SPX | S&P500 指数オプション | 現金決済（株の受渡しなし）・証拠金効率が高い・税制優遇（Section 1256） | CS/IC/Calendar/Strangle など売り系中心 | ストライク刻みは5ドル単位・moomoo paper 口座では扱いが未確定（本番のみ確認済み） |
| QQQ | NASDAQ100 ETF | テック比率高く動きがSPYと違うため分散 | CS/IC/ORB1DTE/Butterfly | ストライク刻み1ドル |
| IWM | ラッセル2000 ETF | 小型株・SPY/QQQと動きが違う | CS/IC/Butterfly | ORB1DTE は検証で不合格（除外中） |
| TSLA | 個別株 | ボラティリティ（値動きの激しさ）が高く決算狙いに向く | Straddle/IV Crush/ORB1DTE | ストライク刻み2.5ドル・決算日参戦のみ推奨 |
| NVDA | 個別株 | 半導体代表・決算影響大 | Straddle/IV Crush/ORB1DTE | 同上 |
| AAPL / MSFT / AMZN / META / GOOGL | 個別株（大型テック） | 決算カレンダー連動で IV Crush（決算後にボラが一気に下がる現象）を狙える | IV Crush/ORB1DTE | 各銘柄の過去IV低下実績を学習してサイズ決定 |

**重要な事実**:
- 「ユニバース」（取引候補全体）は `atlas_rules.yaml` の `symbol_meta.allowed_universe` で明示的に11銘柄に限定されています（根拠: `atlas_rules.yaml:419-431`）
- ブラックリスト方式（除外リスト）ではなくホワイトリスト方式（許可リスト）です。過去の誤注文事故（2026-04-17 SPX/SPY 混入）を受けて、混入を物理的に防ぐ設計に変更されました（根拠: `common/symbol_meta.py:3` 4/17事故対応）
- SPX のストライク刻みは5ドル単位のため、SPY のストライク（1ドル単位）を誤投入すると価格がズレます。`validate_code_for_symbol()` で物理ブロックされます

**根拠ファイル**: `/Users/yuusakuichio/trading/atlas_rules.yaml` `symbol_meta` / `/Users/yuusakuichio/trading/common/symbol_meta.py` / `/Users/yuusakuichio/trading/common/risk_limits.py:50-75`

---

## A3. 扱う戦術一覧

### 戦術カタログ表

| 戦術 | 使う市場環境 | 何をするか（平易説明） | 利確タイミング | 損切りタイミング | 銘柄別の向き |
|---|---|---|---|---|---|
| CS（クレジットスプレッド売り） | VIX（恐怖指数）が普通・方向性が弱い | 「現在価格から遠いストライクで売り・さらに遠いストライクで買い」の組で建て、プレミアム（オプション料）を受取る | 受取ったプレミアムの半分が残ったら利確 | 受取額の2倍の含み損で損切り | SPY/QQQ が中心 |
| IC（アイアンコンドル売り） | VIXが中くらい（18-40）・レンジ相場 | 上方向のCS + 下方向のCS を同時に組む（両サイドからプレミアム受取） | net_credit（受取額合計）の50%で利確 | net_credit × 2倍で損切り | SPY/QQQ/メガテック |
| Butterfly（バタフライ） | IVR（ボラの相対的な高さ）が低い・方向感なし | ATM（現在価格に最も近いストライク）を中心に「1枚買い・2枚売り・1枚買い」の3本構成 | デビット（支払額）の50%増で利確 | デビットの1.5倍損失で損切り | 個別株（SPX除く） |
| Calendar（カレンダー売り） | IVR が高くてVIXが20-50・Term Structure（先物曲線）がコンタンゴ（順ザヤ） | 当日満期を売り・7日後満期を買い（同ストライク・異期日） | 前脚（近い期日）のIVが10%下がったら利確 | 初期デビットの30%増で損切り | SPY/QQQ/SPX |
| Strangle（ストラングル売り） | IVR が高くて（P70以上）方向感なし | OTM（現在価格から離れた）コール売り + OTM プット売り | 受取ったクレジットの50%で利確 | クレジットの200%で損切り | SPY/QQQ/SPX/個別株 |
| Straddle買い | 高ボラ環境・方向不明・大きい動き予想 | ATMコール買い + ATMプット買い（両方向にベット） | 値動きで合計が規定幅に達したら | 時間経過で価値減少した時点 | 決算銘柄・FOMC前 |
| ORB 1DTE | 方向性のあるトレンド初期・ATR（平均的な値動き幅）が十分 | 朝9:30-9:45の高値・安値を基準に、ブレイク方向に翌日満期のオプションを買う | +30%で利確 | -50%で損切り | SPY/QQQ・IWMは除外中 |
| Gamma Scalp | IVR>50%・実現ボラ<想定ボラ・VIX>20 | ストラドル買いポジションの方向リスクを株でヘッジ（MVP実装） | ガンマ利益で調整 | 時間減価が大きくなったら | SPY中心 |
| Delta Hedge | ポートフォリオ全体のデルタ（方向リスク）が過大 | オプションか株でデルタを中立に戻す | デルタ < 0.15 で解除 | PDT枠（後述A5）消費注意 | SPY/QQQ |
| 決算 IV Crush | 銘柄の決算発表前 | 決算直前に Straddle 売りを建て、発表後のボラ急降下で利益 | 翌朝寄付でクローズ | 想定外の方向ブレでアボート | メガテック個別株 |

### 戦術の損益イメージ（定性表現）

| 戦術 | 1回あたり勝ちやすさ | 1回あたり損益の大きさ | 複利への寄与 |
|---|---|---|---|
| CS / IC | 勝ちやすいが勝ち幅は小さい | 小 | 高（回数が稼げる） |
| Butterfly | 中央に収まれば大きい | 小勝ち・中負け | 中 |
| Calendar | 時間減価で勝つ安定系 | 中 | 中 |
| Strangle | 勝ちやすいが尾リスク大 | 小勝ち・大負けも稀にあり | 高（環境選ぶ） |
| Straddle買い | 負けやすいが大当たりあり | 大 | 決算時のみ |
| ORB 1DTE | 勝率60%程度（SPY実測） | 中 | 中〜高 |
| Gamma Scalp | 安定系 | 小 | 中 |
| Delta Hedge | 損益目的でない（リスク調整） | 小 | 間接的 |
| 決算 IV Crush | 勝ちやすい | 小勝ち・中負け稀 | 中 |

**重要**: 具体的な金額・%は `CURRENT_STATE.md` の戦術別月利表に集約されています（参照してください）。この仕様書では定性表現に留めます。

**根拠ファイル**: `/Users/yuusakuichio/trading/atlas_rules.yaml` の `ic_sell` / `butterfly` / `calendar_sell` / `strangle_sell` / `orb_1dte` / `delta_hedge` / `earnings_engine` 各ブロック

---

## A4. 1日の運用フロー

### 時系列表（すべてJST = 日本時間・夏時間想定）

| 時刻（JST） | 時刻（ET） | フェーズ | やること |
|---|---|---|---|
| 22:00頃 | 09:00 | プレマーケット観測 | VIX・VIX9D・VIX3M・IVR・Term Ratio を観測。環境スコア算出。本日の戦術候補を3-5個に絞る |
| 22:25 | 09:25 | ウォッチドッグ起動 | `atlas_watchdog` が場中監視モードに移行（`atlas_rules.yaml:28` `market_window.start: 22:25`） |
| 22:30 | 09:30 | マーケットオープン | ORB 1DTE：最初の15分（3本の5分足）で高値・安値を記録 |
| 22:45-23:00 | 09:45-10:00 | ORB エントリー窓 | ORB1DTE のブレイクアウト確認 → 条件満たせば買いエントリー |
| 23:30-01:00 | 10:30-12:00 | 主戦術エントリー窓 | IC/Calendar/Strangle/Butterfly のエントリー。環境スコア55点以上・pre_trade_check 4層通過が条件 |
| 01:00-05:00 | 12:00-16:00 | 場中監視 | Profit Target / Stop Loss / Early Exit (ガンマ爆発検知等) 監視 |
| 04:45 | 15:45 | 強制決済時刻（売り系） | IC/Calendar/Strangle/CS は未決済ポジションを全て強制クローズ（`ic_sell.force_close_hour: 15, force_close_min: 45`） |
| 04:50 | 15:50 | Butterfly 強制決済 | Butterfly は15:50 ET クローズ |
| 05:00 | 16:00 | マーケットクローズ | Gamma/Delta Hedge の最終調整 |
| 05:05 | 16:05 | ウォッチドッグ停止 | `market_window.end: 05:05` で場中モード終了 |
| 05:05-22:25 | - | アイドル | 日次集計・翌日カレンダー更新・エラー分析 |

### 朝の環境観測フロー（詳細）

```
1. VIX・VIX9D・VIX3M・VIX6M を取得（Yahoo Finance フォールバック）
2. IVR（IVランク）を各候補銘柄で算出（過去52週のIVレンジ中の位置）
3. Term Ratio = VIX9D / VIX3M を算出
   → 0.85未満: コンタンゴ（CS売り有利）
   → 1.05超: バックワーデーション（Straddle買い有利）
4. セクターローテーション・オプションフロー・ニュースセンチメント・経済指標イベントを確認
5. 環境スコアを算出し、戦術候補を選定
6. Pushover通知：本日の戦術候補・サイズ係数を報告
```

**根拠**: `atlas_rules.yaml` `vix_term_structure` / `sector_rotation` / `options_flow` / `news_sentiment` / `econ_events` / `cross_asset` 各ブロック・`market_window` 定義

---

## A5. リスクガード（何を守って何をしないか）

### 守るルール（物理的に実装済み）

| ルール | 内容 | 実装場所 |
|---|---|---|
| 日次最大損失 | フェーズ別に資本の3-5%が上限（超えたら当日停止） | `common/risk_limits.py:22,40,60,80` |
| 週次最大損失 | フェーズ別に6-10%が上限 | 同上 |
| 月次最大損失 | フェーズ別に12-20%が上限（Kill Switch 発動連動） | 同上 |
| 同時ポジション上限 | フェーズ別5-15個 | 同上 |
| 単一銘柄集中上限 | 資本の20-30% | 同上 |
| 単一発注最大枚数 | P0ペーパーは50枚・本番は10枚 | `risk_limits.py:46,66` |
| 発注レート制限 | 1分あたり5-15件 | 同上 |
| 銘柄ホワイトリスト | 11銘柄のみ（それ以外は物理ブロック） | `risk_limits.py:50-75` |

### PDT制約（デイトレード4回ルール）

- 米国の法律で、証拠金口座が$25,000未満の場合、5営業日で4回以上の当日往復取引（PDT = Pattern Day Trader）をすると90日間デイトレ禁止
- Atlas は `common/pdt_tracker.py` で消費回数を管理。残り1回を下回ったら新規の0DTE戦術を自動的に1DTE（翌日満期）にフォールバック
- 満期放置（OTM で紙くず化）・ITM自動行使・現金決済（SPX）は往復取引ではないためPDT対象外（正しく区別される）

### 市場イベント回避

| イベント | ブラックアウト窓（前後分） | 理由 |
|---|---|---|
| FOMC（連邦公開市場委員会） | 前30分・後60分 | 金融政策発表で急変動 |
| CPI（消費者物価指数） | 前15分・後30分 | インフレ指標で急変動 |
| NFP（雇用統計） | 前15分・後30分 | 雇用データで急変動 |
| PPI / GDP / PCE | 前15分・後30分 | インフレ・成長指標 |
| JOLTS / ISM | 前10分・後20分 | 雇用・景況感 |

**根拠**: `atlas_rules.yaml` `econ_events.blackout_windows`（Lucca & Moench 2015 論文準拠の最小安全マージン）

### 決算日の扱い

- Finnhub API から当日・翌日決算銘柄を動的取得
- 決算前1時間にエントリーし、翌朝寄付でクローズ（IV Crush戦術）
- IV Crush 率が25%未満の銘柄はスキップ（プレミアム不足）
- 閉場90分前より遅い「寄引前発表」はスキップ（時間不足）

**根拠**: `atlas_rules.yaml:507-537` `earnings_engine`

### Kill Switch 発動条件

| 条件 | アクション |
|---|---|
| 月次損失が-12〜20%（フェーズ依存） | `data/kill_switch.flag` 自動生成 → 全発注停止 |
| 未捕捉例外が60秒以内に3件（Level3） | Bot 即停止 |
| 建玉数が設計上限を超えた | Bot 即停止 |
| 発注サイズが異常値（3桁以上の数量） | 発注キャンセル＋手動待ち（Level4） |
| 手動 webhook 呼び出し | 即停止 |

**根拠**: `common/kill_switch.py:25-27` / `atlas_rules.yaml:274-322` `rules` の Level3/Level4

---

## A6. 期待される自律度

### 完全自動の範囲

| 項目 | 自動化レベル |
|---|---|
| 朝の環境観測 | 完全自動（Yahoo/Finnhub/moomoo API） |
| 戦術候補の選定 | 完全自動（StrategySelector） |
| 銘柄の選定 | 完全自動（SymbolSelector） |
| エントリー発注 | 完全自動（pre_trade_check 4層通過後） |
| 場中監視・早期決済 | 完全自動（IntradayMonitor） |
| 強制クローズ | 完全自動（時刻トリガー） |
| 日次集計 | 完全自動 |
| 異常検知 | 完全自動（atlas_watchdog 25ルール） |
| Level 1/2 の自動対応（INFO通知・再起動） | 自動 |

### ゆうさくさん承認が必要な場面（月に数件以内を目指す）

| 場面 | 理由 |
|---|---|
| Level 3 の異常検知（Bot停止レベル） | Two-Man Rule（2人制）で誤停止を防ぐ・Pushover priority=1 で通知 |
| Level 4 の発注キャンセル（サイズ異常） | 桁誤りの可能性あり・手動確認必須 |
| 戦略パラメータの変更提案 | バックテストで検証済みでも本番適用は承認経由 |
| 資金フェーズ移行（P0→P1→P2） | リスク閾値が変わるため |
| OpenD ログイン残回数消費 | 残4回（現状・永久BAN回避で物理3回制限） |

**承認フロー**:
1. Atlas または watchdog が Pushover priority=1 で通知
2. ゆうさくさんが応答（5分タイムアウトで自動キャンセル・安全側）
3. 承認後のみアクション実行

**根拠**: `atlas_rules.yaml:324-351` `autofix.two_man_rule`

---

# Part B 技術付録（開発者・redteam用）

## B1. 戦術別の詳細仕様

### B1.1 IC Sell (IronCondorSellEngine)

**実装**: `spy_bot.py:12454` `class IronCondorSellEngine` (約790行)

**エントリー条件**（すべて論理積 AND）:
```yaml
vix_min: 18.0          # これ未満はプレミアム不足でスキップ
vix_max: 40.0          # これ超は方向性リスク大
ivr_min_percentile: 40.0   # IVR が過去60日の40%ile以上
env_score_min: 55.0    # 環境スコア55点以上
entry_window: 10:30-14:00 ET
```

**デルタ目標（動的算出）**:
- base: call_delta=0.20 / put_delta=0.20
- VIX > 28.0: delta を -0.03 補正（守り）
- IVR > 70: delta を +0.03 補正（積極）

**スプレッド幅（ATR連動）**:
- `spread_width = ATR_14 * 0.50`
- min: 1 strike・default: 5（ATR取得失敗時）

**資本配分**:
- VIX < 28: `capital_pct_base: 0.40`（call/put 合算 max_loss ベース）
- VIX ≥ 28: `capital_pct_high_vix: 0.30`

**Exit 条件**:
- `profit_target_pct: 0.50` → net_credit の50%でTP
- `stop_loss_mult: 2.0` → net_credit × 2.0でSL
- `force_close: 15:45 ET`

**発注手順（legs）**:
1. Call side short（売り）leg
2. Call side long（買い）leg = short + spread_width
3. Put side short leg
4. Put side long leg = short - spread_width
5. net_credit = 受取プレミアム合計

**失敗時ロールバック**:
- 4レッグのうち一部のみ約定した場合: 全残余 leg を市場価格で即クローズ
- Pushover priority=1で通知

**根拠**: `atlas_rules.yaml:576-625` `ic_sell`

---

### B1.2 Butterfly (ButterflyEngine)

**実装**: `spy_bot.py:13401` `class ButterflyEngine` (約600行)

**エントリー条件**:
- IVR < 30.0 (fallback) または動的P30未満
- entry_window: 10:30-14:00 ET
- SPX 除外（個別株・SPY/QQQ/IWM のみ）

**Wing 幅算出（動的）**:
- `wing_width = ATR_14 * 0.40`（ストライク刻みに丸め）
- min_wing: 1 strikes / max_wing: 10 strikes

**Wing タイプ選択**:
- `wing_type: dynamic` → SMAトレンドで自動選択（上昇→Call Butterfly / 下降→Put Butterfly）
- 固定設定可: "call" / "put"

**Exit 条件**:
- `profit_target_pct: 0.50` → ネットデビットの50%増で利確
- `stop_loss_pct: 1.50` → デビットの150%損失で損切り
- force_close: 15:50 ET

**サイジング**:
- `capital_pct: 0.02`（2%をネットデビット=リスクとして使用）
- max_qty: 3 / max_qty_paper: 10
- 推定証拠金 fallback: $200/contract

**根拠**: `atlas_rules.yaml:458-500` `butterfly`

---

### B1.3 Calendar Sell (CalendarEngine)

**実装**: `spy_bot.py:9090` `class CalendarEngine` (約610行)

**エントリー条件**:
- IVR > P75（動的）
- VIX 20-50
- VIX5日EMA が下降傾向（Term Structure チェック）
- entry_window: 10:30-12:00 ET

**構成**:
- front（近い期日）: 0DTE ATM 売り
- back（遠い期日）: 7DTE ATM 買い（同ストライク）

**Exit 条件**:
- `profit_target_iv_crush_pct: 0.10` → front IVが10%以上低下→利確
- `stop_loss_pct: 0.30` → 初期 debit 比30%増で損切り
- force_close: 15:45 ET

**サイジング**:
- `max_risk_pct: 0.02`（口座の2%）
- max_qty: 2

**根拠**: `atlas_rules.yaml:363-381` `calendar_sell`

---

### B1.4 Strangle Sell (StrangleSellEngine)

**実装**: `spy_bot.py:11871` `class StrangleSellEngine` (約580行)

**エントリー条件**:
- IVR > P70（動的算出）または fallback 60
- VIX 15-50
- entry_window: 10:30-12:00 ET

**デルタ目標**:
- call_target_delta: 0.15
- put_target_delta: 0.15
- delta_tolerance: 0.05

**Exit 条件**:
- profit_target: クレジットの50%
- stop_loss: クレジット × 2.0
- force_close: 15:45 ET

**サイジング**:
- max_risk_pct: 0.03（3%）・max_qty: 2

**根拠**: `atlas_rules.yaml:387-407` `strangle_sell`

---

### B1.5 ORB 1DTE (ORB_1DTEEngine)

**実装**: 0DTE 版 ORB が Blinded Backtest で不合格 → 1DTE化で再設計（合格）

**エントリー条件**:
- ORB window: 9:30-9:45 ET の5分足3本で高値・安値確定
- breakout buffer: `ATR × 0.5`（固定%ではなく動的）
- consec_bars: 2本連続ブレイクで確定（フェイク除外）
- SMA20 方向フィルタ

**デルタ目標**:
- min: 0.35 / max: 0.45 / center: 0.40

**Exit 条件**:
- TP: +30%
- SL: -50%（early-stop 回避のため緩め）

**満期**:
- dte_target: 1（翌営業日）
- dte_max: 3（祝日明け許容）

**対象銘柄（ホワイトリスト）**:
- SPY / QQQ / AAPL / AMZN / GOOGL / META / MSFT / NVDA / TSLA
- IWM は Backtest FAIL で除外（2026-04-22 D11 fix）

**バックテスト実績**（ThetaData SPY 1DTE 135日分）:
- Trades: 70 / Win rate: 60.0% / Sharpe: 2.49 / Max DD: 5.2% / PnL +$1735
- 合格基準（Sharpe≥1.0, WR≥50%, DD≤25%）を全てクリア

**根拠**: `atlas_rules.yaml:701-737` `orb_1dte` / `data/backtest_orb_1dte_20260418.md`

---

### B1.6 Delta Hedge (IntradayMonitor._try_delta_hedge)

**実装**: `spy_bot.py` 内 IntradayMonitor クラス内

**トリガー**:
- 9:30-14:30 ET: |portfolio_delta| > 0.30
- 14:30以降（ガンマ爆発帯）: > 0.40
- unwind: |delta| < 0.15 で解除
- emergency: |delta| > 0.50 は PDT枠消費してでもヘッジ

**制約**:
- daily_max_count: 3（過剰ヘッジ防止）
- weekly_pdt_budget: 3（FINRA PDTルール）
- contract_delta: 0.50（ATM near想定）

**根拠**: `atlas_rules.yaml:631-666` `delta_hedge`

---

### B1.7 Earnings IV Crush (EarningsEngine)

**実装**: `common/earnings_engine.py` (902行)

**エントリー条件**:
- `entry_before_earnings_min: 60`
- `entry_cutoff_before_close_min: 90`
- `min_iv_crush_rate: 0.25`
- Finnhub earnings calendar API で動的取得（固定銘柄リスト禁止）

**サイズ係数（動的）**:
- crush_rate ≥ 0.38: ×1.2
- crush_rate ≥ 0.30: ×1.0
- crush_rate < 0.30: ×0.7

**銘柄別 default crush rate**:
| 銘柄 | crush rate |
|---|---|
| NVDA | 0.40 |
| META | 0.38 / NFLX | 0.38 |
| AMD | 0.36 |
| TSLA | 0.35 |
| CRM | 0.34 |
| AMZN | 0.33 |
| GOOGL | 0.32 |
| AAPL | 0.30 |
| MSFT | 0.28 |

**実績3件以上で自動的に中央値に切り替わる**（動的学習）

**根拠**: `atlas_rules.yaml:507-537` `earnings_engine`

---

### B1.8 Gamma Scalp / Straddle Buy / CS Sell

| 戦術 | 実装 | 主条件 |
|---|---|---|
| GammaScalpEngine | `spy_bot.py:10130` | IVR>50% + RV<IV + VIX>20 でMVP実装 |
| StraddleBuyEngine | `spy_bot.py:10631` | 高ボラ・方向不明・ATM両建て |
| TradeEngine (CS) | `spy_bot.py:4532` | 方向性スコア + VIX低中 + IVR中高 |

---

## B2. 環境観測スコア体系

### 指標一覧

| 指標 | 算出方法 | 根拠 |
|---|---|---|
| VIX | CBOE S&P500 ボラ指数（リアルタイム・Yahoo fallback） | 公知 |
| VIX9D | 9日先 | 同上 |
| VIX3M | 3ヶ月先 | 同上 |
| VIX6M | 6ヶ月先 | `atlas_rules.yaml:558-561` |
| VVIX | VIX のボラ | 公知 |
| IVR | (IV - IV_min_52w) / (IV_max_52w - IV_min_52w) × 100 | 52週レンジ相対位置 |
| Term Ratio | VIX9D / VIX3M | 環境適応判定 |
| Spot Ratio | VIX9D / VIX | 補助チェック |
| VRP | IV - RV（実現ボラとの差） | プレミアム過不足 |
| GEX | Gamma Exposure（市場全体のディーラー在庫推定） | 外部API or 概算 |
| Bias | bull/bear/neutral（SMA20/SMA50 クロス） | トレンド判定 |

### 環境判定（term_ratio ベース）

| term_ratio 範囲 | レジーム | CS売り size | Straddle買い size |
|---|---|---|---|
| < 0.85 | コンタンゴ | 1.0 | 控えめ |
| 0.85-1.05 | ニュートラル | 0.9 | 通常 |
| > 1.05 | バックワーデーション | 0.8 | 1.0（有利） |

**スコア加算**:
- コンタンゴ: +5 点（CS売り有利）
- バックワーデーション: -10 点（リスク環境）
- VIX6M > 25: size_factor × 0.9 追加縮小

### 固定閾値禁止規律（最重要）

- 「IVR < 30%でスキップ」は環境を見ているように見えるが30%が固定である時点で半分しか適応できていない
- 正しい設計: IVR のパーセンタイル（P30/P70/P75）を動的算出する
- 資金フェーズ（120万→400万→1億）でもパラメータは変わる。自動フェーズ移行を設計する

**根拠**: `CLAUDE.md` 環境適応型の設計規律 / `feedback_no_fixed_params.md`

---

## B3. サイジング

### Kelly Criterion 実装

**公式**: `f* = (bp - q) / b`
- b = 利益/損失比（reward/risk）
- p = 勝率
- q = 1 - p

**実装**: `common/kelly_sizer.py` (499行)

**YAML設定**: `common/kelly_sizer.yaml`（上書き可能）

**プラン別特性**（Chronos関連は別担当なので割愛）:
- Atlas は Kelly × 0.25（Quarter Kelly）で開始
- 連敗時の自動縮小（3連敗で 0.5 倍）

### Phase 別リスクパラメータ

| Phase | 資本レンジ | max_margin/trade | daily_loss | max_positions | max_qty/order |
|---|---|---|---|---|---|
| P0_paper | ペーパー | 3% | -3% | 15 | 50 |
| P1_live_small | $8K-$25K | 5% | -5% | 5 | 10 |
| P2_live_mid | $25K-$100K | 5% | -5% | - | - |
| P3_live_large | $100K+ | 4% | -4% | - | - |
| P4_prod_mature | - | 2% | -2% | - | - |

**根拠**: `common/risk_limits.py:36-120` DEFAULT_LIMITS

### PDT Tracker 連動

- `common/pdt_tracker.py` が append-only JSONL でラウンドトリップを記録
- `tracker.remaining_allowed(capital_usd)` で残回数取得
- capital ≥ $25K: 無制限 / < $25K: 5営業日で3回まで（4回目で違反）
- exit_type で PDT対象判定: manual_close は対象・expired_worthless/assigned/cash_settled は対象外

---

## B4. リスクガード実装

### Kill Switch

**実装**: `common/kill_switch.py` (408行)

**仕様**:
- `data/kill_switch.flag` 存在で全発注即停止
- TTLキャッシュ廃止（2026-04-21 hardening）→ 毎回ファイル確認（race condition 修正）
- ファイル削除検知: 予期せず消えた場合に自動再発動
- 解除は `deactivate()` CLI経由のみ（直接rmでバイパス不可）
- audit JSONL 全操作記録

**冪等性欠陥と対策**:
- 旧実装はTTLキャッシュにより `is_active()` の戻り値がrace conditionでズレる問題あり → 廃止済み
- 新設計では「キャッシュなし・毎回ファイル stat」で一貫性優先

**通知**: 多段 fallback (Pushover → ntfy.sh → Discord → Gmail → audit JSONL)

### Pre-Trade Check 4層

**実装**: `common/pre_trade_check.py`

**4層防護**:
1. kill_switch チェック
2. symbol_whitelist チェック
3. 資本配分・集中度チェック
4. 発注レート・重複チェック（recent_orders.json / 120秒TTL / Mac-VPS 共有）

### 決算日回避（Finnhub 連動）

**実装**: `common/earnings_engine.py` / `common/symbol_selector.py`

- SymbolSelector に `earnings_exclude=True` オプション
- Finnhub API から 当日 ±1営業日の決算銘柄を取得
- CS/IC 等のプレミアム売り戦術で自動除外

---

## B5. moomoo / futu API 接続

### 必要な API 呼び出し一覧

| API | 用途 | 頻度 |
|---|---|---|
| OpenD login | 初期認証 | 起動時のみ |
| get_snapshot | 原資産価格 | 30秒間隔 |
| get_option_chain | オプション板取得 | エントリー判断時 |
| get_rt_data | ティック | subscribe |
| subscribe / unsubscribe | ストリーミング | ポジション保有中 |
| place_order / modify_order / cancel_order | 発注 | 戦術実行時 |
| get_position_list | 建玉確認 | 5秒間隔 |
| get_order_list | 注文確認 | 同上 |
| get_deal_list | 約定確認 | EOD |

### レート制限

- moomoo API: 保守的設定 `max: 3 / window: 3600sec`（`common/auth_budget.py:65-71`）
- OpenD: 残4回制限 → 3で停止（永久BAN回避）
- 公式制限を下回る余裕を持たせた設定

### エラー応答分類・リトライ方針

| エラー種別 | 分類 | リトライ |
|---|---|---|
| ErrCode 一時的（timeout/rate） | transient | 3回・指数バックオフ |
| ErrCode 認証（-9/-11） | auth_error | auth_budget 消費・1回のみ |
| ErrCode 入力（invalid code） | permanent | リトライせず Pushover |
| ErrCode 市場（closed/halted） | transient | 次tickまで待機 |

### SPX Whitelist 問題の対処

**問題**: `common/risk_limits.py` の `symbol_whitelist` に `US.SPX` が長期欠落 → SPX 戦術が pre_trade_check でブロックされてトレード0件になる事故

**対処**: 2026-04-22 C-4 fix で `US.SPX` / `US.SPXW` 両方を全Phase の whitelist に追加済み（`common/risk_limits.py:50-75`）

**根拠**: `data/research/bottleneck_datadriven_20260422.md:35,189` M-6 項目

### オプションコード体系（事故防止）

| 銘柄 | underlying code | option root | strike刻み |
|---|---|---|---|
| SPY | US.SPY | SPY | 1.0 |
| SPX (0DTE/Weekly) | US..SPX | SPXW | 5.0 |
| SPX (Monthly) | US..SPX | SPX | 5.0 |
| QQQ | US.QQQ | QQQ | 1.0 |
| IWM | US.IWM | IWM | 0.5 |
| 個別株 | US.TICKER | TICKER | 2.5（大半） |

**ガード**: `validate_code_for_symbol()` でセンターストライク±許容範囲外の混入を物理検知

---

## B6. 想定ボトルネック・仕様レベル対処

| # | ボトルネック | 仕様レベル対処 | 根拠 |
|---|---|---|---|
| 1 | OpenDログイン残4回 | `common/auth_budget.py` で max=3/24h 物理強制。`AUTH_BUDGET_BYPASS=1` で緊急解除 | `CLAUDE.md:VPS移行時の必須手順` |
| 2 | moomoo paper の SPX 対応未確定 | symbol_selector で paper モード時は SPX を candidate から除外（要ゆうさくさん実機検証） | 未確定事項として記録 |
| 3 | PDT 4回制限 | `pdt_tracker` 連動で 0DTE→1DTE 自動フォールバック（`strategy_selector.py`） | `common/pdt_1dte_utils.py` |
| 4 | オプション板の薄さ | 流動性フィルタ: `bid-ask_spread_pct < 10%` で除外（`risk_limits.max_bid_ask_spread_pct`） | `risk_limits.py:48` |
| 5 | SPX whitelist 欠落事故 | 全Phaseで SPX/SPXW を symbol_whitelist に追加（2026-04-22修正済） | `risk_limits.py:50-75` |
| 6 | SPX vs SPY strike 混入事故（2026-04-17） | center_strike_tolerance_map で SPX は ±10% 厳格・validate_code_for_symbol で物理ブロック | `atlas_rules.yaml:446-452` |
| 7 | Finnhub レート制限 | earnings_cache.json で当日分キャッシュ再利用・weekly auto_update | `common/earnings_engine.py:51` |
| 8 | Pushover 月10,000通枠 | quiet hours（JST 22:00-04:00）・dedup/batch 3層で月32,100→4,560件に圧縮済 | `CURRENT_STATE.md` 通知ポリシー |
| 9 | VIX index futu 非対応 | Yahoo Finance にフォールバック（`spy_bot.py:3118`） | 実コード確認済 |
| 10 | OpenD プロセスstale | `atlas_watchdog` が `stale_log_sec: 180` で検知・Level3 で restart_bot | `atlas_rules.yaml:24` |
| 11 | 建玉数上限超過 | position_count_anomaly ルール Level3 で stop_bot + create_issue | `atlas_rules.yaml:287-297` |
| 12 | 発注サイズ桁誤り | order_size_anomaly ルール Level4 で halt_and_wait（手動確認必須） | `atlas_rules.yaml:312-322` |
| 13 | 未捕捉例外連鎖 | unhandled_exception_cascade ルール Level3（60秒で3件→stop_bot） | `atlas_rules.yaml:275-285` |
| 14 | Gamma Early Exit 無限ループ | Level2 で restart_bot（`window:120 threshold:10`） | `atlas_rules.yaml:233-245` |
| 15 | API認証失効 | api_rejection_cluster Level2 で notify_only + post_verify | `atlas_rules.yaml:262-272` |
| 16 | チェーン取得連続失敗 | ic_sell_chain_fetch_loop Level2（5回/300秒でnotify） | `atlas_rules.yaml:94-105` |
| 17 | Butterfly wing計算エラー | Level3 で stop_bot（ATR 取得 None 参照対策） | `atlas_rules.yaml:183-193` |
| 18 | Delta Hedge PDT枠枯渇 | Level3 notify_only + create_issue（週次budget=3超過検知） | `atlas_rules.yaml:194-204` |
| 19 | 決算発表時刻計算バグ | earnings_abnormal_exit_loop Level3 で stop_bot | `atlas_rules.yaml:206-216` |
| 20 | selector 重複（root vs common） | 最小版仕様で common/ 1本に統合予定（-2,534行削減） | `minimum_spec_20260422.md:404` |
| 21 | mass_verify 本番混入 | 76件言及を tests/ 移動予定（-600行） | 同上 |

### Two-Man Rule（緊急時例外）

- Level 3/4 は原則 Pushover 承認待機（5分タイムアウトで自動キャンセル）
- 例外（即時実行許可）:
  - `crisis_regime_detected`（市場クラッシュ検知）
  - `kill_switch_activated`

**根拠**: `atlas_rules.yaml:333-348` `autofix.two_man_rule`

---

## B7. 未確定事項（redteam 要調査）

以下は既存コード・yaml から事実が確認できなかった項目。redteam で再調査が必要:

| # | 項目 | 確認先候補 |
|---|---|---|
| 1 | moomoo paper が SPX/SPXW を実際に発注受理するか | 実機テスト必要（OpenD paper接続でSPXオプションを1枚試験発注） |
| 2 | Finnhub API free tier の earnings calendar 日次取得上限 | Finnhub公式ドキュメント |
| 3 | VIX9D/VIX3M/VIX6M の Yahoo Finance ティッカー正確性 | 公式ティッカー確認（%5EVIX9D etc） |
| 4 | GEX データソース（外部API or 自前計算） | 現状コード中での算出経路不明 |
| 5 | Phase P2/P3/P4 の具体的な移行トリガー数値 | `risk_limits.py` DEFAULT_LIMITS の phase切替ロジック確認 |

---

## B8. 設計原則チェックリスト（実装 review 用）

- [ ] 固定パラメータなし（全閾値は動的算出 or YAML上書き可能）
- [ ] pre_trade_check 4層すべて通過後のみ発注
- [ ] kill_switch チェックが発注パスの最初にある
- [ ] symbol_whitelist で物理ブロック（ブラックリストでなくホワイトリスト）
- [ ] option_code は validate_code_for_symbol() を必ず通す
- [ ] PDT残数チェック後のみ 0DTE 発注
- [ ] 約定失敗時の leg ロールバック経路が全戦術に実装されている
- [ ] Pushover 多段fallback（ntfy/Discord/Gmail）
- [ ] atlas_watchdog の25ルールすべてが場中 22:25-05:05 JST で動く
- [ ] Two-Man Rule（Level3/4 は5分承認待機）
- [ ] auth_budget 消費が記録される（Tradovate/OpenD/moomoo/Gmail/MFFU）

---

## B9. 参考: 行数内訳と最小版ターゲット

現状の spy_bot.py (Atlas実装): 18,858行。最小版での目標サイズ:

| モジュール | 最小版行数 |
|---|---:|
| 市場時間ユーティリティ | 200 |
| 環境観測（MarketSnapshot） | 300 |
| 銘柄・戦術セレクタ | 250 |
| 戦術エンジン × 8 | 2,000 |
| サイジング | 150 |
| リスクガード | 300 |
| Exit条件 | 200 |
| 執行（BrokerClient） | 400 |
| メインループ | 200 |
| **Atlas合計** | **3,000** |

**根拠**: `data/research/minimum_spec_20260422.md:223-234`

---

## Appendix. 一次根拠ファイル一覧

| 役割 | ファイルパス |
|---|---|
| Atlas 実装本体 | /Users/yuusakuichio/trading/spy_bot.py |
| 戦術パラメータ全集 | /Users/yuusakuichio/trading/atlas_rules.yaml |
| 銘柄ホワイトリスト | /Users/yuusakuichio/trading/common/risk_limits.py |
| 銘柄メタ・option code | /Users/yuusakuichio/trading/common/symbol_meta.py |
| 銘柄選択 | /Users/yuusakuichio/trading/common/symbol_selector.py |
| 戦術選択 | /Users/yuusakuichio/trading/common/strategy_selector.py |
| Kill Switch | /Users/yuusakuichio/trading/common/kill_switch.py |
| Pre-Trade Check | /Users/yuusakuichio/trading/common/pre_trade_check.py |
| PDT Tracker | /Users/yuusakuichio/trading/common/pdt_tracker.py |
| Auth Budget | /Users/yuusakuichio/trading/common/auth_budget.py |
| Kelly Sizer | /Users/yuusakuichio/trading/common/kelly_sizer.py |
| Earnings Engine | /Users/yuusakuichio/trading/common/earnings_engine.py |
| Watchdog ルール | /Users/yuusakuichio/trading/atlas_rules.yaml（rules セクション） |
| 最新状態スナップショット | /Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory/CURRENT_STATE.md |
| Atlas プロジェクトメモ | /Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory/project_atlas.md |
| 最小版見積 | /Users/yuusakuichio/trading/data/research/minimum_spec_20260422.md |
| ボトルネック分析 | /Users/yuusakuichio/trading/data/research/bottleneck_datadriven_20260422.md |

---

**本仕様書の適用範囲**: Atlas（SPY/SPX オプション・moomoo経由）のみ。Chronos（MFFU先物・CME E-mini）と Common モジュール横断仕様は別 agent が作成（相互参照なし）。

**最終更新**: 2026-04-22（draft 初版）
**次回review**: データ駆動での戦術削除判断（30日PnLデータ揃い次第）後に v3 へ
