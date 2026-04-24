# ADR-014: moomoo MetricProvider Scope & Deployment（2026-04-24）

**Status**: **PROVISIONAL（Secretary 暫定採用・ゆうさくさん最終承認待ち）**
  - Strategist 第三者検証（agent a943891a86064487e, 2026-04-24）で Decision 1, 2 は CONDITIONAL-VALID 判定
  - Secretary 自動採用規律（feedback_bug_rate_auto_decide_no_question）は Decision 3 のみ適用妥当
  - Decision 1, 2 はプライベート領域（ゆうさくさんの生活サイクル）に踏み込むためゆうさくさん承認が必要
  - 詳細: `data/decisions/ADR-015-moomoo-provider-alternatives-20260424.md`

**Date**: 2026-04-24
**Context**: Sprint 2 最優先 C-017 moomoo paper API 実接続の着手前 3 判断

---

## Decision 1: OpenD 常駐場所 = **Mac mini（ゆうさくさんの開発機）**

### 選択肢
- A. Mac mini（開発機）
- B. VPS

### 採用 A の根拠（バグ発生率・リスク）

| 観点 | Mac mini | VPS |
|---|---|---|
| auth_budget max 3/24h 規律（CLAUDE.md 鉄則 #2） | 抵触なし | **抵触** |
| 個人利用 moomoo ライセンス | 範囲内 | グレー（商用利用判定リスク）|
| 市場時間 JST 22:20-05:10 のカバー | ゆうさくさん Mac 起動時間帯に収まる | 24h 常時カバー |
| Paper 段階での要件 | 十分 | 過剰 |
| Sprint 2 内の実装複雑度 | 低（既存環境）| 中（VPS セットアップ必要）|

**結論**: Paper 段階では Mac mini で十分・VPS は Sprint 3+ live 運用時に再検討。

---

## Decision 2: セッション期限切れ時 = **手動再ログイン + 期限検知 Pushover 通知**

### 選択肢
- A. 手動再ログイン（ゆうさくさんが moomoo アプリ起動 → login）
- B. AppleScript 等で自動ログイン GUI 操作

### 採用 A の根拠

| 観点 | 手動 | 自動化 |
|---|---|---|
| moomoo TOS 遵守 | 明確に遵守 | グレー領域（bot-like action 判定リスク）|
| 実装複雑度 | 低 | 高（GUI 操作 + パスワード管理）|
| セキュリティ | パスワード暗号保管のみ | 平文経路リスク |
| 対処頻度 | 24-48h ごと（許容範囲）| - |

**結論**: 手動再ログイン。ただし MonitorDaemon に認証失敗検知機構を組み込み、検知時に Pushover 発火 → ゆうさくさんに即通知。

### 実装仕様
- `moomoo_provider.get_metrics()` で認証失敗を検知したら `AuthenticationError` raise
- MonitorDaemon の `_fetch_metrics` で `AuthenticationError` を catch → `_send_alert` で Pushover 発火（priority=1 で EMERGENCY 扱い）
- 検知後は fail-closed（監視ゼロ回避のため即 KillSwitch 発動 or yfinance fallback）

---

## Decision 3: Sprint 2 C-017 スコープ = **read-only metrics のみ（発注 API は Sprint 3+ 分離）**

### 選択肢
- A. read-only metrics provider のみ
- B. 発注 API も含む（place_order / cancel_order / position）

### 採用 A の根拠（Strategist 調査・premortem F05）

| 観点 | A: read-only | B: 発注含む |
|---|---|---|
| 工数 | 2.5-3 日 | 5 日超 |
| bug 発生率 | **低** | 中〜高 |
| premortem F05 べき等性 | 自然担保（読取は副作用なし）| 要実装（client_order_id キー設計・重複発注リスク）|
| Sprint 2 期限内完遂 | 可能 | 不可能 |
| 代理 PnL 問題の解決 | **解決**（実 PnL 取得） | 解決 |

**結論**: read-only で Sprint 2 完結・paper 運用で実 PnL 監視可能になる段階で 4/27+α paper 開始可否を再判定。発注 API は Sprint 3+ 別 task。

---

## 実装指針（Sprint 2 Day 2）

### `atlas_v3/ops/moomoo_provider.py` 実装項目

1. **futu SDK import guard**（spy_bot.py パターン踏襲・読取のみ）
2. **認証接続**: `OpenSecTradeContext(host=127.0.0.1, port=11111)` + `unlock_trade(password=TRADE_PASSWORD)`
3. **smoke_test()**: startup 時に `get_acc_list()` で 401/unauth 検出
4. **get_metrics()**:
   - `accinfo_query(trd_env=TrdEnv.SIMULATE)` で paper 口座情報取得
   - `total_assets / realized_pl / unrealized_pl` から pnl_day_usd 算出
   - `drawdown_pct`: high_water_mark vs 現在 total_assets
   - `latency_ms`: API 呼出往復時間計測
5. **AuthenticationError** 例外で期限切れを区別
6. **socket timeout=5s + retry 3 exponential backoff**
7. **fail-closed**: provider 失敗は zero-fallback 禁止（RuntimeError raise）

### テスト方針

- mock test（futu SDK monkeypatch）10+ 件で実装検証
- 実 paper 接続 smoke test は**ゆうさくさん戻り後**に実行
- pytest tests/ 全件 0 regression 維持

### main.py 配線

- `--provider moomoo` を argparse に追加
- `_build_metric_provider("moomoo")` で `MoomooMetricProvider` を返却
- launchd plist の default を `--provider moomoo` に変更（`--provider yfinance` は emergency fallback）

---

## 関連 ADR / memory / doc

- ADR-013: v3 戦術選定（前 Sprint の決定）
- `memory/project_moomoo_opend_research_20260424.md`（Strategist 事前調査）
- `data/premortem_reports/20260424_081027_moomoo_OpenD_API_事前調査_*.md`
- `memory/feedback_bug_rate_auto_decide_no_question_20260423.md`（本 ADR の採用規律根拠）

## 撤回条件

- ゆうさくさんから明示的に別判断（VPS / 自動ログイン / 発注 API 含む）の指示があった場合、本 ADR は無効化し再判定。
