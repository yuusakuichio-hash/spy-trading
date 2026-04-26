# Redteam 仕様書 v3 独立検証 (Phase 1 C-2 dry-run)

作成: 2026-04-22 / 担当: Red Team 専任 agent（Claude Opus 4.7・CCF内側・sycophancy禁止モード）
対象: `data/specs/v3/common_spec_v3_20260422.md` / `atlas_spec_v3_20260422.md` / `chronos_spec_v3_20260422.md`

## 判定: NO-GO

3本の仕様書はいずれも「Phase 2実装に耐える凍結水準」に達していない。凍結を謳いながら、実装者が一義的に書き下ろせない曖昧さが多数残り、既存コードとの型衝突・シグネチャ不一致、scaffold ディレクトリ構造と仕様DAGの齟齬、外部依存（pybreaker / healthchecks.io / ntfy.sh / Finnhub / Yahoo）の契約未記載が重なっている。

## 各仕様書の判定
- common_v3: NO-GO（14 Interfaceのうち実装一義性を満たすのは4本のみ）
- atlas_v3: NO-GO（v2にあった重要契約が情報損失で消失、固定閾値禁止規律違反あり）
- chronos_v3: NO-GO（v2の25ボトルネック対処が15%しか継承されていない・MFFU動的値参照が循環依存）

---

## CRITICAL 違反（11件）

### C-01 (common_v3 B6): scaffoldディレクトリと仕様DAGの物理衝突
`common_v3/idempotency/__init__.py` と `common_v3/order/__init__.py` が両方存在（scaffold済）。
しかし common_spec_v3 B6 は `common_v3/order/idempotency.py` と記載。
Builderがどちらに実装すべきか一義不能。import path 破綻のリスク。
修復: scaffold 削除 or 仕様書を `common_v3/idempotency/store.py` に書き直し。

### C-02 (common_v3 B9 kill_switch): 既存実装との戻り値型シグネチャ矛盾
v3: `activate(..., scope) -> None`
v2 spec: `-> bool`（True=新規発動/False=冪等スキップ）
既存実装 `common/kill_switch.py:187`: `-> None`
v2で発見された冪等性修正（audit重複/Pushover二重送信防止）が v3 で消失。
FirmScopedKillSwitch との統合経路も未定義。
最悪: フラッシュクラッシュ時に同時多発 activate → Pushover quota 一瞬枯渇 → Andon Cord 経路喪失。

### C-03 (common_v3 B3 EICAS): Warning通知の KILL_SWITCH 連動が危険
B3: `Warning: Pushover priority=2 + ntfy + KILL_SWITCH`
Warningで自動 Kill Switch 発動は EICAS 航空原則に反する（Boeing 777以降、Warning=人間判断喚起・自動停止しない）。
Therac-25 (1985-87) 同型リスク: 誤った interlock で医療事故。
最悪: Finnhub 一時断絶 → Warning → 自動 Kill Switch → 手動解除まで全停止。
修復: Warning と Kill Switch 分離。Kill Switch 発動は MLL/DLL/portfolio DD 超過のみ。

### C-04 (common_v3 B10 MarketDataClient): silent failure 誘発契約
v2 B6: 「取得失敗時 stale=True で返す」明記
v3 B10: エラー経路一切なし
CLAUDE.md「silent except 禁止」「silent failure 禁止」絶対規律違反。
最悪: 2020/3 コロナショック再現時、stale VIX=15 で戦術が低ボラ前提発注継続（LTCM 1998型）。
修復: 全メソッドに error 契約明記（raise/stale/default の選択と呼出元責務）。

### C-05 (common_v3 B14 Circuit Breaker): pybreaker 単一ベンダーロック
B14: `from pybreaker import CircuitBreaker` と package 名を仕様書に直接記載。
pybreaker は 2024 以降メンテ頻度低。SolarWinds 型 supply chain attack 耐性なし。
修復: CircuitBreaker 抽象 interface を定義、実装は差し替え可能と明記。

### C-06 (common_v3 B11 Deadman): 既存 scripts/dead_man_switch.py との役割分担不明
既存 LaunchAgent 登録済み・COMPONENTS 7つ監視中。v3 は library 化を謳うが migration 手順なし。
最悪: 新旧両方が並行動作 → beacon path 不一致 → 両方 silent 死。
修復: 既存 scripts を deprecated にする migration 明記。

### C-07 (atlas_v3 B5 TacticEngine Protocol): 10戦術の共通 Interface が現実に合わない
- delta_hedge: 単独戦術でなく portfolio 反応型。should_enter(env, symbol) に合わない
- earnings_iv_crush: Finnhub 取得銘柄リスト必要・単一 symbol 形式と噛み合わない
- orb_1dte: 09:30-09:45 ET range 決定後 breakout 判定（state 持ち必要）
無理やり Protocol に合わせると stub/unused parameter 続出 → silent failure の温床。
Boeing 737MAX MCAS と同型（単一 interface で全状況対応）。
修復: 少なくとも 3種（enter/exit型・portfolio反応型・state持ち型）に分類。

### C-08 (atlas_v3 B2 EnvObserver): 固定閾値禁止規律との構造矛盾
`get_dynamic_threshold(metric, percentile)` で percentile が固定引数。
CLAUDE.md「IVR<30%でスキップ は環境を見ているように見えるが30%が固定である時点で半分しか適応できていない」明記の規律違反を構造化。
修復: percentile 自体を資金フェーズ/VIX 領域から動的算出する PercentileSelector 追加。

### C-09 (atlas_v3 B8 MoomooClient): SPX whitelist 問題がまた消失
v2 B5 で C-4 fix 記録済みだった「US.SPX が risk_limits.py 長期欠落→トレード0件事故」が、v3 では `# SPX whitelist 登録済` コメントに矮小化。実装責務所在不明。
最悪: 2026-04-17 事故の再発。
修復: `common_v3/risk/symbol_whitelist.py` として独立 Interface 化。

### C-10 (chronos_v3 B5 MFFUFlexRules): 動的値参照が循環依存
A4「Profit target: 動的（chronos_rules_plugin/mffu_flex.py 参照）」
しかし mffu_flex.py L38-58 はハードコード定数（_EVAL_PROFIT_TARGET_USD = 3_000.0）。
「動的」と宣言しながら実装は固定値参照 → 仕様と実装の齟齬。
最悪: MFFU 改定未反映 → Bot が旧 target 到達 → 新規 entry 継続 → MLL 違反 → 口座失効。
修復: 取得元（YAML/API/HTML scrape）・更新頻度・確認ゲートを契約に明記。

### C-11 (全spec Part F): 未確定事項を抱えたまま凍結宣言は規律違反
atlas_v3 Part F: moomoo paper SPX/個別7銘柄 earnings/gamma_scalp MVP 全て未確定
chronos_v3 Part F: MFFU Flex 最新 Profit Target/Max Loss 未確定
common_v3 Part F: Navigator 代替役精査・別 session Redteam 検証 未完了
Challenger O-ring (1986) 同型: 未確定寒冷下挙動を抱えたまま launch。
修復: Part F が空になるまで凍結禁止。

---

## HIGH 違反（14件）

H-01: common_v3 A4 legacy_write_block 実装責務・場所未記載
H-02: common_v3 B2 LLMBudget 複数agent並行呼出時の fair-sharing 未定義
H-03: common_v3 B5 Leg option_type default "STOCK" がマルチレッグ戦術で混在検証を pass させてしまう
H-04: common_v3 B5 OrderRequest request_id 生成責務未記載（default factory なし）
H-05: common_v3 B6 with_idempotency signature 変更（store 引数消失、singleton 強制）
H-06: common_v3 B7 reconcile の Diff 型未定義
H-07: common_v3 B12 HealthCheck -> bool 返却（HealthResult message 消失で silent failure 誘発）
H-08: common_v3 B13 spec_drift の Drift/Patch 型未定義
H-09: common_v3 Part E テスト要件の数値が達成困難（mutation 75%+ / cov 85%+ / integration 20%+）、--ignore 偽装の誘惑
H-10: atlas_v3 B1 AtlasEngine tick() 60秒固定は動的化余地を Interface で封じる
H-11: atlas_v3 Part C DAG 10戦術並列で共通 data model 未決定（MarketEnvironment field 衝突リスク）
H-12: chronos_v3 A3 F12/F13 の silent failure 監視要件が v2 から消失（情報損失）
H-13: chronos_v3 B5 MFFUFlexRules 4メソッド分割の根拠なし（統合 check_all() の有無不明）
H-14: chronos_v3 B7/B8 TradersPost/Tradovate の rate limit 枯渇時 fallback 未定義

---

## MEDIUM 違反（7件）

M-01: 「凍結宣言」のエスケープハッチ（Flow 3 再審議）が強すぎ、F3-C01 と連鎖
M-02: common_v3 B10 cache path が scaffold 未作成・race condition リスク
M-03: atlas_v3 A2 moomoo paper/本番 の動作差を Interface で吸収する経路未定義
M-04: atlas_v3 B6 KellySizer win_rate 取得 window 未定義
M-05: atlas_v3 B7 PDTGuard record_day_trade() が exit_type 取らない（v2 区別が消失）
M-06: chronos_v3 A7 5層 vs atlas_v3 A5 4層（構造不一致）
M-07: Part E Hypothesis boundary case の適用範囲未定義

---

## Claude 起草者が見えない盲点（5件）

B-01: Pythonic Type Hints 依存バイアス（runtime validation の責務未定義）
B-02: spy_bot.py 18858行/silent except 31.4%/MI 0.00 の根本原因未分析、「継承しない」宣言だけでは再発
B-03: Part A ゆうさくさん向け制約が Part B Builder Interface に落ちていない（paper/prod 分岐等）
B-04: 同一 LLM + 同一 CCF 内側の仕様起草・検証。Gemini/GPT-5/人間レビュー で独立読解せず凍結は Boeing 737MAX 同型
B-05: 「interface 凍結」用語が spec_drift の正当な更新を心理的に阻害

---

## Phase 2 着手前の必修対処（P0）

| 優先 | ID | 必修対処 |
|---|---|---|
| P0 | C-01 | scaffold 削除 or B6 を `common_v3/idempotency/store.py` に書き直し |
| P0 | C-02 | activate() -> bool 戻し・FirmScopedKillSwitch 統合経路明記 |
| P0 | C-03 | EICAS Warning と Kill Switch 分離 |
| P0 | C-04 | MarketDataClient 全メソッドに error 契約明記 |
| P0 | C-05 | CircuitBreaker 抽象 interface 定義 |
| P0 | C-06 | Deadman migration 手順明記 |
| P0 | C-07 | 10戦術を3種以上の Protocol に分類 |
| P0 | C-08 | percentile 動的化経路追加 |
| P0 | C-09 | common_v3/risk/symbol_whitelist.py 独立 Interface 化 |
| P0 | C-10 | MFFU 動的値の取得元・頻度・確認ゲート明記 |
| P0 | C-11 | Part F を空にしてから凍結 |

---

## 既存資産との衝突（ファイル・行番号付き）

1. common_v3/idempotency/__init__.py (scaffold) vs common_spec_v3 B6 `common_v3/order/idempotency.py` — path 衝突
2. common_v3/order/__init__.py (scaffold) — 中身未確定
3. common/kill_switch.py:187 activate() -> None vs v2 spec bool vs v3 spec None — 3者不一致
4. common/llm_budget.py:48 VENDORS 定数 vs common_v3 B2 Interface — 継承明示なし
5. common/auth_budget.py:44-86 SERVICES 定数 — common_v3 B1 継承記載あるが Part C で扱いなし
6. scripts/dead_man_switch.py LaunchAgent 登録済 vs common_v3 B11 library 化 — migration 手順なし
7. .claude/hooks/andon_multichannel.py 既存 vs common_v3 B4 library 化 — pull_andon 関数名不一致の可能性
8. chronos_rules_plugin/mffu_flex.py:38-58 ハードコード定数 vs chronos_v3 A4 動的記載 — 宣言と実装の齟齬
9. common/idempotency.py:38-118 IdempotencyStore vs common_v3 B6 with_idempotency — signature 変更（store 引数消失）
10. common/portfolio_aggregator.py 既存 vs common_v3 B8 PortfolioSnapshot — 統合経路不明
11. common/pre_trade_check.py 既存 check_order(ctx,limits) — v2 C4 変更禁止リスト記載あったが v3 で扱い不明

---

## Red Team 自己限界

1. 同じ Claude Opus 4.7 で起草・検証している（Self-preference bias arXiv:2410.21819 定量測定済）
2. CCF 内側（同一 fs / CLAUDE.md / MEMORY.md / project context）
3. 仕様書自体の「正しさ」は未検証（Boeing 737MAX MCAS 単一 AoA センサ型リスク）
4. 外部依存（MFFU 規約・moomoo paper 挙動・Finnhub・pybreaker）の現実確認未実施
5. 楽観バイアス残存（Phase 1 C-2 を「通したい」prompt 側圧力で CONDITIONAL に傾く瞬間が複数あった）

## flow_audit 連鎖

flow_audit_20260422.md で既に NO-GO 判定。P0対処 6件（Auditor 欠損/Navigator 別機種化/Spec Pre-Mortem/Andon 3経路化/Self-Check 外部化/見積もり calibration）未完のまま仕様書凍結に進むのは二重の誤判断。本 Red Team の C-01〜C-11 を解消しても、flow_audit P0 未解決なら依然 NO-GO。

---

CRITICAL 11 / HIGH 14 / MEDIUM 7 / 盲点 5 / 既存衝突 11項目 / P0 修復 11項目
