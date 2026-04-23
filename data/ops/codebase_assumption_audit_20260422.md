# Codebase 全 regex/filter/guard assumption 監査レポート

作成: 2026-04-22 03:25 JST
対象: common/*.py / spy_bot.py / atlas_agent.py / chronos_*.py / scripts/*.py / .claude/hooks/*.{py,sh} / atlas_rules.yaml / common/risk_limits.py
手法: re.(compile|match|search|findall|sub|fullmatch) 全抽出 -> 実 log (data/logs/condor.log / atlas_agent.log) 突合 -> pass/fail 判定

## 全数集計

| 区分 | 件数 |
|---|---|
| re.* 呼出し (本番コード、backup/mutants 除外) | 約 100 件 |
| def check_/filter_/validate_/verify_ 関数 | 約 150 件 (含 bot class methods) |
| atlas_rules.yaml 行 pattern | 24 件 |
| ガード hook (.claude/hooks/*.py, *.sh) | 29 ファイル |

## TOP10 hidden bug 候補 — 全件 grep/log 実データで再現確認済

### 1. [CRITICAL] .claude/hooks/claim_ledger_guard.py:67,71 INTERNAL_URL_RE / TRUSTED_HOSTS 全滅
- pattern: `r"https?://(?:localhost|127\\.0\\.0\\.1|198\\.13\\.37\\.17|0\\.0\\.0\\.0)"`
- 実データ検証: `re.search(p, 'http://127.0.0.1:8080')` -> None / `http://198.13.37.17/` -> None / `https://api.pushover.net/...` -> None
- 根本原因: raw string 内で `\\.` と書くと regex engine が `\\` (literal backslash) + `.` (any) として解釈。ドット escape が効いていない。
- 影響: 内部 URL / 信頼ホスト判定が全件 False → すべて「未検証URL」扱い。TTL キャッシュが全く機能していない。
- 修正: raw string で `\.` に直す。`r"https?://(?:localhost|127\.0\.0\.1|198\.13\.37\.17|0\.0\.0\.0)"`

### 2. [CRITICAL] common/risk_limits.py:50-125 symbol_whitelist が US.SPX を含まない
- 実データ: condor.log に `[PreTradeCheck/L1] 発注拒否: Symbol not in whitelist: US.SPX (code=US.SPX260421C00705000 ...)` 16件 / `Leg delta_hedge_CALL market pre_trade_check reject` 多数
- 根本原因: whitelist は `US.SPXW` / `US..SPX` だが delta_hedge は `US.SPX` を投げている (さらに strike=705 は SPY 価格帯 → 銘柄混入疑いも併発)
- 影響: delta_hedge CALL が全件リジェクト。ポートフォリオΔ>0.30 時のヘッジ戦術が事実上停止。
- 修正: whitelist に `"US.SPX"` 追加 + delta_hedge 側で symbol 正規化 (`US.SPX` -> `US..SPX` or `US.SPXW`)

### 3. [HIGH] atlas_rules.yaml:47/55/63/71 IC_SELL/Butterfly/Calendar/Strangle entry rule が永久に fire しない
- pattern: `\[IC_SELL\].*(entry|エントリー).*(success|ok)|IronCondor.*placed`
- 実データ: condor.log 実ログは `[IC_SELL] premarket_check OK` / `IronCondorSellEngine initialized` 形式のみ。`entry success` / `placed` 文字列は 0 件。
- 影響: 新戦術 5種の正常系監視ルールが sunday 以来すべて dead。Level1 通知が 1件も届いていない(誤解検出盲点)。
- 修正: pattern を `\[IC_SELL\].*(premarket_check|place_order|entry).*OK` 等、実ログに合わせる

### 4. [HIGH] atlas_rules.yaml:87/159/209 `\[EarningsEngine\|IVCrush\]` は literal 文字列マッチで fire 不能
- pattern: `\[EarningsEngine\|IVCrush\].*(entry|エントリー).*(success|ok)`
- 解析: 正規表現的には `[EarningsEngine|IVCrush]` という literal 文字列を探しに行く (`\|` は literal `|` としか解釈されない)
- 実データ: spy_bot.py ログ出力は `[IVCrush]` 単独 or `[EarningsEngine]` 単独で `|` を含まない
- 影響: 決算エンジン系ルール 3本が永久 dead
- 修正: `(\[EarningsEngine\]|\[IVCrush\]).*(entry|...)`

### 5. [HIGH] .claude/hooks/auth_budget_guard.py:30 pattern 過剰マッチで read-only 操作も HARD BLOCK
- pattern: `tradovate_client\.py`
- 実データ: data/logs/auth_budget_guard.log に `[HARD BLOCK] services=['tradovate_demo'] cmd=head -80 .../tradovate_client.py` / `grep ... tradovate_client` / `scp .../tradovate_client` が多数
- 影響: ファイル閲覧すら予算消費扱いで即ブロック。調査タスクが軒並み詰まる (この監査中にも発生・確認済み)
- 修正: pattern を `python3\s+.*tradovate_client\.py|\./tradovate_client\.py\s` (実行形態のみ) に狭める

### 6. [HIGH] daily_trade_analysis.py:197 entry_logs が `(standard|orf)` 2戦術のみ認識
- pattern: `\[(standard|orf)\] (\w+) CS: expiry=(\S+) qty=(\d+) VIX=([\d.]+) delta=([\d.]+) width=(\d+)`
- 実データ: condor.log で `[standard]/[orf]` = 52 件、一方 `[IC_SELL]/[Butterfly]/[CalendarEngine]/[StrangleSell]/IronCondor` = 1006 件
- 影響: AAR / 日次レポートで新戦術 5種のエントリーが完全に欠落。月次集計・戦術別勝率の誤評価。
- 修正: pattern を戦術ごとに分岐 or tactic 汎用パーサに統合

### 7. [HIGH] common/itm_risk_check.py:43 ITM_WARNING_DISTANCE_USD が全銘柄 $0.50 固定
- `ITM_WARNING_DISTANCE_USD = 0.50` は SPY($500 帯) では 0.1% だが SPX($5400) では 0.009% = ノイズ未満
- 影響: SPX/NVDA/TSLA 等高値銘柄で ITM 接近警告が実質発動しない。ChainGuard (\d{8} 全銘柄均一) と同じ独立性欠如パターン。
- 修正: symbol_meta の strike_interval などから比率ベース `distance_pct * underlying` へ

### 8. [MEDIUM] chronos_watchdog.py:258 `kill.?switch.*activated` pattern が test ログで誤発火
- pattern: `kill.?switch.*activated|kill.*switch.*active`
- 実データ: atlas_agent.log に `[Two-Man Rule] Level3 emergency_bypass発動: R_TEST_L3 matched=kill_switch_activated` 176件
- 影響: chronos_watchdog は chronos.log しか見ていないので今は無害だが、将来 atlas_agent.log も読むようになった途端に test 文字列で誤発火する種
- 修正: 文脈限定 `(?<!matched=)kill_switch_activated` または `KILL_SWITCH actual activate` 等

### 9. [MEDIUM] .claude/hooks/state_safety_guard.py:74,75 `_enabled/_disabled` 過検知
- pattern: `_enabled` / `_disabled` 単体マッチ
- 実データ未突合だが `feature_enabled`, `paper_enabled`, `trade_enabled` など正常フィールド含むあらゆる state.json で MEDIUM 警告発火し、hook ログ(`state_safety_violations.log`)がノイズ化、本当に重要な CRITICAL を埋める危険
- 修正: `^\s*"(kill|override|force|pdt)_enabled"\s*:` など接頭辞限定

### 10. [MEDIUM] common/account_schema.py:37 capital_usd < 100 で ValidationError
- `if v < 100: raise ValueError(...)` — futu API 初期 polling 中や一時 0 返却時に必ず例外。fallback 層がここでクラッシュする。
- 影響: AccountState 生成時に try/except で受けていない箇所で bot が突然死するレース
- 修正: warning log + フォールバック値 or この strict 判定は呼び出し側にする

## 即修正推奨件数

CRITICAL 2 件 (#1 claim_ledger_guard・#2 risk_limits) は 24h 以内対応推奨。
HIGH 5 件 (#3 #4 #5 #6 #7) は週内対応推奨。
MEDIUM 3 件 (#8 #9 #10) は今週レビュー。

## 備考 — 本監査で確認できなかった領域
- C2/SNS 系 regex は対象外 (プロダクション機能ではない)
- .sh hook 内 regex は目視確認のみ (grep -E 部分): discipline_guard.sh / peer_review.sh / 等。個別深掘り未
- Grep による追加発見の可能性: 今回 re.* 中心、`in string` 部分文字列 filter は別監査推奨
