# Redteam 仕様書 v3 R1 独立検証 (Phase 1 C-2 dry-run R2)

作成: 2026-04-23 / 担当: Red Team 専任 agent（Claude Opus 4.7 別 session・CCF 内側・sycophancy 禁止モード）
対象: `data/specs/v3/common_spec_v3_20260422.md`（R1 改訂・B14b/B15/B16 新設）/ `atlas_spec_v3_20260422.md`（R1・B5 3 Type 分類）/ `chronos_spec_v3_20260422.md`（R1・B5 MFFU 契約）
前回: `data/governance/redteam_spec_v3_audit_20260422.md`（CRITICAL 11 / HIGH 14 / MEDIUM 7 / 盲点 5）

---

## 判定: CONDITIONAL-GO

前回 CRITICAL 11 件のうち **7 件完全解消・3 件部分解消（残存バグ温床あり）・1 件 spec 上は解消だが physical enforcement なし**。新設された B14b / B15 / B16 / 3 Type TacticEngine / MFFU 動的契約は方向性として正しいが、**新規 CRITICAL を 4 件導入**している。Phase 2 着手は可能だが、新 CRITICAL 4 件を Phase 2 最初のスプリントで潰すことを必修条件とする。

- common_v3 R1: CONDITIONAL-GO（B15/B16 で穴が残る）
- atlas_v3 R1: CONDITIONAL-GO（Type C/Type A 境界で新規 silent failure ベクトル）
- chronos_v3 R1: CONDITIONAL-GO（yaml 真実源が未作成・bootstrap 手順欠落）

---

## 前回 CRITICAL 11 件の解消状況

| ID | 内容 | R1 対応 | 検証結果 |
|---|---|---|---|
| C-01 | scaffold dir と spec DAG 衝突 | B6 で `common_v3/idempotency/store.py` 確定 | **部分解消**: path は確定したが、既存 `common/idempotency.py` の `check_and_register/make_key(signal_id,label)` と新 `check_and_mark/make_job_key(strategy,symbol,trigger_time)` の **API 非互換 migration 手順なし** |
| C-02 | kill_switch activate() 戻り値矛盾 | B9 で `-> bool`・FirmScopedKillSwitch 統合経路明記 | **部分解消**: 現 `common/kill_switch.py:187` は `-> None` / `FirmScopedKillSwitch.activate(firm, reason) -> None`。R1 は `FirmScopedKillSwitch(firm=...).__init__().activate(reason) -> bool`。**singleton → per-firm instance への設計変更 + 呼出元全面書換が必要な migration が spec 未記載** |
| C-03 | Warning 連動 Kill Switch 危険 | B3 で「Kill Switch と分離」明記 | **完全解消** |
| C-04 | MarketDataClient silent failure | B10 で `MarketDataResult[T]` + `MarketDataError` + `allow_stale` 明記 | **完全解消** |
| C-05 | pybreaker ベンダーロック | B14 で `CircuitBreakerBackend` 抽象 Protocol + auto_recovery=False | **部分解消**: 抽象化 OK だが `auto_recovery: bool = False` がパラメータで露出・Builder が `True` で迂回可能（physical enforcement なし） |
| C-06 | Deadman 既存 vs 新設 migration | B11 で 5 ステップ + 3 日 shadow 運用明記 | **名目解消・物理矛盾**: 既存 `scripts/dead_man_switch.py` は `data/ops/heartbeat/dead_man_ping.jsonl`（**JSONL 1 ファイル集約式**）、R1 は `data/state_v3/deadman/*.beacon`（**component 別ファイル式**）。形式が違うものの差分比較手順が未定義 → shadow 運用で何をもって「差分ゼロ」とするか不明 |
| C-07 | 10 戦術単一 Protocol 強制 | B5 で 3 Type（A/B/C）分類 | **部分解消**: 3 分類は正しい方向だが新規問題を導入（新 CRITICAL R-01 参照） |
| C-08 | percentile 固定引数 | B2 で `PercentileSelector` 外部注入 | **完全解消** |
| C-09 | SPX whitelist 責務所在不明 | B14b で `SymbolWhitelist` 独立化・`verify_at_startup()` | **完全解消** |
| C-10 | MFFU 動的値循環依存 | chronos B5 で yaml 真実源 + `MFFURuleMissingError` + 週次 scan | **部分解消**: 契約は明確だが `data/prop_rules/mffu_flex.yaml` が **未作成**。yaml スキーマ定義・初期 bootstrap 手順・既存ハードコード定数からの移行契約が spec 未記載 |
| C-11 | Part F 未確定抱えた凍結 | common Part F 5 項目中 3 解消・atlas/chronos Part F は「Phase 2 着手前 gate」に位置付け変更 | **完全解消** |

**集計**: 完全解消 5 件 / 部分解消 6 件 / 退行 0 件。

---

## 新規 CRITICAL（4 件・R1 で導入された新バグ温床）

### R-01 (atlas_v3 B5): Type A / Type C 境界の silent failure 新ベクトル
- `gamma_scalp` が Type A（EnterExit）に分類されるが、B16 自体が「gamma_scalp は秒単位 Greeks 再計算 + delta hedge で並列必要」と認めている
- Type A は single symbol かつ single shot のエントリー前提 → portfolio 全体の Greeks を反応的に監視する gamma_scalp は本来 Type B（Portfolio 反応型）と Type C（state 持ち）の hybrid
- **Boeing 737MAX MCAS 同型の退行**: 単一 interface 強制を 3 分類に緩めたが、戦術特性分類が誤っている
- 最悪: 場中 gamma 爆発時、Type A の `should_enter` が 1 symbol のみ評価 → portfolio 全体 delta 中立化遅延 → flash crash（2020/3 COVID 型）でヘッジ間に合わず口座吹き飛び
- Type A/B/C 間の共通基底未定義のため、Builder が Union 型で書き始めて type system 崩壊・silent failure 経路拡大
- 修復: gamma_scalp を Type B+C hybrid として B5 に追加、または Type D（Reactive-Stateful）新設。Type 間 migration / 併用プロトコル明記

### R-02 (common_v3 B16): TaskExecutor 抽象化の leaky abstraction
- `def submit(...) -> Future` で `Future` の型が `concurrent.futures.Future` か `asyncio.Future` か未指定
- 両者 API 非互換（`.result(timeout=)` vs `await future`・`.cancel()` semantics も異なる）
- `AsyncExecutor(TaskExecutor)` が Phase 3 で必要になった瞬間、全呼出元の `.result()` パターンが壊れる
- sync 実装内で `submit` された関数が他の `submit` を呼ぶ場合 → ThreadPool saturation → **Knight Capital 2012 型デッドロック**
- `map(func, items) -> list[Any]` は lazy evaluation 不可 → 11 銘柄 env_observer で全件完了待ち・1 銘柄の slow request がポートフォリオ全体ブロック
- 修復: `submit` 戻り値を `AwaitableResult` 独自型でラップ（`.result(timeout)` 統一）・ThreadPool 内部再入 detect・`map` を `Iterator` 返却に変更

### R-03 (common_v3 B16): 「Kill Switch 同期経路必須」の物理強制欠如
- spec: 「Circuit Breaker / Idempotency / Kill Switch 確認は**同期経路で必須実行**（並列化禁止）」と宣言
- しかし Builder が `executor.submit(lambda: check_kill_switch())` と書ける余地を技術的に消していない
- 規律違反は code review でしか検知できず → Normalization of Deviance（Challenger O-ring 1986）で見逃し累積
- 最悪: 並列化された kill switch check が race condition で `is_active()=False` を返す瞬間に発注通過 → Kill Switch が飾りに
- **Therac-25 (1985-87) 同型**: interlock を「規律で守る」が失敗する古典パターン
- 修復: AST 検査 hook（`common_v3/executor/no_concurrent_safety_check.py`）追加・`check_kill_switch()` 呼出が executor context 外であることを import-time assertion で検証

### R-04 (chronos_v3 B5): yaml 真実源 bootstrap 契約欠落
- R1 spec: 「`data/prop_rules/mffu_flex.yaml` を**唯一の真実源**とする」明記
- しかし `data/prop_rules/` ディレクトリも `mffu_flex.yaml` も**現時点で未作成**（`ls` で不在確認済）
- 既存ハードコード定数 `_EVAL_PROFIT_TARGET_USD=3_000.0`（`chronos_rules_plugin/mffu_flex.py:40`）から yaml への移行契約が spec 未記載
  - yaml schema 定義（必須 key 一覧・optional key・validation rule）
  - yaml 初期値の**由来証跡**（MFFU 公式 URL + scrape 日時 + 確認者）
  - yaml 欠損時の fallback（spec は `MFFURuleMissingError` raise と書くが、**bootstrap 時に raise されたら Chronos 起動不能**）
- 最悪: Phase 2 Builder が yaml 作成忘れ → Chronos 起動失敗 → 場中に気付く → MFFU eval 期限内に間に合わず evaluation 失敗 → Flex plan 没収
- 修復: Phase 2 実装順 DAG の先頭に「`data/prop_rules/mffu_flex.yaml` 作成（schema + 初期値 + 由来証跡）」を追加・schema JSON Schema で厳密化

---

## 新規 HIGH（6 件）

### H-R01 (common_v3 B6): IdempotencyStore API 非互換 migration 欠落
- 既存: `check_and_register(key) -> bool` / `make_key(signal_id, label)`
- R1: `check_and_mark(key, ttl_sec=300) -> bool` / `make_job_key(strategy, symbol, trigger_time)`
- key 生成規則が別 → 旧 key と新 key が共存時に重複検知失敗
- 既存呼出元（Atlas/Chronos 全域）の置換プロトコル未定義

### H-R02 (common_v3 B9): FirmScopedKillSwitch singleton vs per-firm instance 設計変更
- 既存: `get_firm_kill_switch()` singleton + `firm_ks.activate(firm, reason)` 
- R1: `FirmScopedKillSwitch(firm=...)` per-firm instance + `.activate(reason) -> bool`
- 複数モジュール（chronos_bot.py / intraday_monitor / etc）から同一 firm 参照時の state 共有経路未定義
- per-firm instance 化で singleton 的 state の責任所在が Builder に投げられる

### H-R03 (common_v3 B15): SqliteStorage / JsonlStorage / HybridStorage の trade-off 未明記
- どの method が SQLite で、どの method が JSONL か spec 決定（`HybridStorage` docstring のみ）
- 並行書込時のロック戦略（SQLite WAL mode / file lock / fcntl）未定義
- 多プロセス（Atlas + Chronos 同時 idempotency 書込）時の整合性未保証
- SQLite file corruption 時の fallback 未定義

### H-R04 (common_v3 B11 Deadman): path 同一の物理矛盾
- R1 spec: 「同一 path 使用（`data/state_v3/deadman/`）」と明記
- 既存実体は `data/ops/heartbeat/dead_man_ping.jsonl`
- 「同一 path」と spec で書くなら既存 path を使うべき（`data/ops/heartbeat/`）、または既存を migrate するなら migration 期間の「どっちに書くか」明記必要

### H-R05 (common_v3 B14): CircuitBreaker auto_recovery パラメータ露出
- `auto_recovery: bool = False` がコンストラクタ公開パラメータ
- Builder が `auto_recovery=True` で迂回可能・Self-healing 再発リスク
- 既定 False だけでは不十分・「True 指定は禁止」を physical enforcement 必要

### H-R06 (atlas_v3 B5): 3 Type 間の共通基底型未定義
- Type A/B/C のいずれも `should_exit(position, env) -> ExitDecision` や `build_exit_order(...)` を共有
- 共通基底 `TacticBase` を spec で定義しないと Builder が Protocol 継承 or ABC で揺れる
- AtlasEngine.tick() で `list[TacticBase]` として扱う場合の type system 一貫性確保が不明

---

## 新規 MEDIUM（4 件）

- M-R01: B14b SymbolWhitelist.verify_at_startup() 失敗時の「起動中断」が具体的 exit code / restart プロトコル未定義
- M-R02: B16 TaskExecutor `map` が `list[Any]` 返却のため 11 銘柄 env_observer で中間失敗時 partial result 扱い未定義
- M-R03: chronos B5 `verify_yaml_freshness` が 30 日固定閾値 → CLAUDE.md 固定閾値禁止規律違反の疑い
- M-R04: common B15 `save_position_snapshot` の snapshot 頻度・retention policy 未定義（無限肥大の潜在）

---

## 案B（ExecutorProvider 抽象）への集中攻撃

### 攻撃面 1: sync/async 切替境界の race condition
- 現状 spec は「asyncio は `common_v3/executor/async_impl.py` 内のみ許可」とするが、SyncExecutor の ThreadPoolExecutor 内から async 関数を呼ぶ場合（例: async 対応 broker SDK 追加時）、event loop 衝突が発生
- Python の `asyncio.run()` を thread 内で呼ぶと RuntimeError（loop already running）・または nested loop で silent 挙動

### 攻撃面 2: ThreadPoolExecutor saturation
- SyncExecutor が内部で ThreadPoolExecutor を使う場合、worker 数が固定なら gamma_scalp（秒単位）+ delta_hedge（portfolio）+ 11 銘柄 env_observer が同時起動で thread starvation
- spec は worker 数・queue 長・reject policy 未定義

### 攻撃面 3: Kill Switch / Idempotency 同期経路の物理強制欠落
- R-03 の再掲。AST 検査なしでは規律違反を検知できない
- Navigator（Gemini Flash）や Redteam review に全面依存 → human factor（Normalization of Deviance）

### 攻撃面 4: Future cancellation semantics 不一致
- `concurrent.futures.Future.cancel()` は実行中の task を中断できない
- `asyncio.Future.cancel()` は CancelledError を投げる
- Kill Switch 発動時に進行中の戦術計算を止める経路が executor 種類で異なる → 「3 秒以内に全 task 停止」の SLA が実装で揺れる

### 攻撃面 5: テストの複雑性爆発
- sync / async 両方の実装を test するには fixture を 2 倍必要
- mutation testing 75%+ 目標が達成困難（Part E H-09 の懸念が拡大）

---

## 既存コードとの衝突（R1 で新規発生・合計 7 件）

1. `common/kill_switch.py:187` `activate(reason, activator) -> None` vs R1 `-> bool` + `scope: dict`
2. `common/kill_switch.py:295` `FirmScopedKillSwitch.activate(firm, reason) -> None` vs R1 `activate(reason) -> bool`（per-firm instance 化）
3. `common/kill_switch.py:397` `get_firm_kill_switch()` singleton vs R1 設計の per-firm instance
4. `common/idempotency.py:95` `check_and_register(key) -> bool` vs R1 `check_and_mark(key, ttl_sec)`
5. `common/idempotency.py:74` `make_key(signal_id, label)` vs R1 `make_job_key(strategy, symbol, trigger_time)`
6. `scripts/dead_man_switch.py:46` PING_FILE `data/ops/heartbeat/dead_man_ping.jsonl` vs R1 `data/state_v3/deadman/*.beacon`
7. `chronos_rules_plugin/mffu_flex.py:40-58` ハードコード定数 vs R1 yaml 真実源（yaml 未作成）

---

## Part F 未確定事項の gate 機能評価

| spec | Part F 項目 | gate 機能として機能するか |
|---|---|---|
| common_v3 | F-01〜F-05 | 5 項目中 3 解消・残 2 項目（F-03 案B 再検証 / F-05 Self-healing）は R1 で実質対応済 → **機能する** |
| atlas_v3 | moomoo paper SPX / 個別 7 銘柄 earnings / gamma_scalp MVP | 「Phase 2 着手前 Builder 調査タスク」とされたが、**調査タスクの owner / deadline / success criteria が未定義** → 先送りリスク |
| chronos_v3 | MFFU 最新値 / TradersPost 8 日挙動 / 他プロップ自動化 | MFFU は B5 契約で対応済だが yaml 未作成（R-04）・TradersPost 8 日は現時点で既に 4/23 → **期限切れ情報の扱い未定義** |

**判定**: gate として「名目上機能するが、Phase 2 着手前に必ず raise されるとは限らない構造」。ceremonial gate 化のリスク（Challenger 同型: 未解消のまま launch 圧力で通過）。

---

## Phase 2 着手前必修対処 P0（4 件・新規 CRITICAL に対応）

| 優先 | ID | 必修対処 | 想定工数 |
|---|---|---|---|
| P0 | R-01 | gamma_scalp の Type 再分類（Type B+C hybrid または Type D 新設） | 0.5h（spec 修正のみ） |
| P0 | R-02 | TaskExecutor Future 型統一（AwaitableResult 独自型）・map を Iterator 化 | 1h（spec 修正） |
| P0 | R-03 | Kill Switch / Idempotency 同期経路の AST 検査 hook 追加を spec に明記 | 0.5h（spec 修正） |
| P0 | R-04 | `data/prop_rules/mffu_flex.yaml` schema 定義 + 初期値 + bootstrap 手順を chronos spec B5 に追記 | 1h（spec 修正 + yaml 作成） |

**Phase 2 第 1 スプリントに組込必修の残件（既存 C 系部分解消）**:
- C-01 部分残: idempotency API migration プロトコル明記
- C-02 部分残: kill_switch migration プロトコル明記（singleton → per-firm instance）
- C-05 部分残: auto_recovery パラメータ physical enforcement
- C-06 名目解消: deadman shadow 運用の差分比較手順定義
- C-10 部分残: mffu_flex.yaml 実ファイル作成

---

## 反論視点（Blue Team 主張への直接反論）

### Blue Team 主張 1: 「CRITICAL 11 件のうち 7 件完全解消・3 件部分解消で進捗良好」
**反論**: 部分解消 3 件は全て「spec 上は書いたが既存実装との migration プロトコル欠落」。migration こそ Knight Capital 2012 同型事故の典型的発生源。spec だけ書いて実装に投げるのは「virtual fix」。

### Blue Team 主張 2: 「Gemini と Redteam 両方の指摘を反映した」
**反論**: Gemini Fix 1-3 は良い指摘だが、Gemini 自身が「実機検証なしには完全に見抜けない」と限界開示している。同一 CCF 内側 Claude Opus + 同一 project context Gemini Flash の両方ともに見逃したベクトルが存在する可能性 → 外部独立レビュー（人間 SRE / OpenAI o3 有償）未実施。

### Blue Team 主張 3: 「Part F 残存項目を Phase 2 着手前 gate として位置付けた」
**反論**: gate の owner / deadline / success criteria が未定義のまま「着手前に調査」と書くのは Challenger 同型の先送り。実運用では launch 圧力で gate が ceremonial 化する。

### Blue Team 主張 4: 「sync 既定 + ExecutorProvider 抽象で案 B 完全対応」
**反論**: 抽象 interface は設計上正しいが、Future 型統一・sync/async 境界・ThreadPool saturation・cancellation semantics・Kill Switch 同期経路物理強制、5 つ全てで穴が残る。「抽象化した」だけで「安全になった」わけではない。

### Blue Team 主張 5: 「MFFU 動的値契約で循環依存解消」
**反論**: yaml 真実源とする契約は正しいが、yaml 自体が未作成。契約書は書いたが帳簿そのものがまだ無い状態（Gemini「銀行の帳簿をどこに書くか決めていない」の指摘を表面的にしか解消していない）。

---

## Red Team 自己限界（CCF 内側・同一 Claude Opus）

1. **Self-preference bias**（arXiv:2410.21819 定量測定済）: Claude Opus が起草した spec を Claude Opus が検証している。同族が見逃す構造的盲点は必ず存在
2. **CCF 内側**: 同一 filesystem / CLAUDE.md / MEMORY.md / project context を共有 → 起草時前提を検証時にも無意識採用
3. **前回検証と同一 agent persona**: 2026-04-22 前回検証と同一の Red Team agent profile で動作 → 同じ攻撃パターンに偏る risk
4. **外部依存の実機検証未実施**: MFFU 規約最新値 / moomoo SPX paper 挙動 / Tradovate rate limit 実測 / pybreaker 2024+ メンテ状況 / SQLite 多プロセス整合性 / ThreadPoolExecutor saturation 実測、いずれも spec 読解のみで実機なし
5. **15 分時間制約下の楽観バイアス**: 時間切れを避けるため「部分解消」判定を拡大解釈する誘惑が存在。CONDITIONAL-GO も R-01〜R-04 を「部分解消群と同列」に軽く扱う risk
6. **Bootstrap paradox**: spec が完全でないことを検知するが、spec が完全になった状態を想像することでしか検証できない → 未知の未知（Rumsfeld）に盲目

---

## flow_audit 連鎖（前回未解消）

前回監査で指摘した flow_audit_20260422.md の P0 対処 6 件（Auditor 欠損 / Navigator 別機種化 / Spec Pre-Mortem / Andon 3 経路化 / Self-Check 外部化 / 見積もり calibration）のうち、本 R1 改訂で解消されたのは:
- Andon 3 経路化: 既存 `andon_multichannel.py` で解消済
- Navigator 別機種化: Gemini Flash 運用で部分対応

残る 4 項目（Auditor / Spec Pre-Mortem / Self-Check 外部化 / 見積もり calibration）は未解消のまま。本 R1 仕様書の CONDITIONAL-GO は flow_audit 前提と独立しているため、**flow_audit P0 未解決でも Phase 2 仕様書レベルは進めるが、Phase 2 実装中に flow_audit 未解決リスクが顕在化する可能性あり**。

---

## 最終集計

- 前回 CRITICAL 解消: 完全 5 / 部分 6 / 退行 0
- 新規 CRITICAL: 4 件（R-01 〜 R-04）
- 新規 HIGH: 6 件（H-R01 〜 H-R06）
- 新規 MEDIUM: 4 件
- 既存コード衝突: 7 件（R1 で新規または未解消）
- Phase 2 前 P0 必修: 4 件（想定工数 3h・spec 修正中心）
- Red Team 自己限界: 6 件（外部独立レビュー未実施を含む）

**判定**: **CONDITIONAL-GO**（R-01〜R-04 の spec 修正 + Phase 2 第 1 スプリント必修込み）

**条件成立時のみ**:
- ゆうさくさん最終承認
- Phase 2 Builder 着手
- 外部独立レビュー（人間 SRE or OpenAI o3）を Phase 2 中に追加実施

---

## 関連ファイル（絶対パス）

- `/Users/yuusakuichio/trading/data/specs/v3/common_spec_v3_20260422.md`（R1）
- `/Users/yuusakuichio/trading/data/specs/v3/atlas_spec_v3_20260422.md`（R1）
- `/Users/yuusakuichio/trading/data/specs/v3/chronos_spec_v3_20260422.md`（R1）
- `/Users/yuusakuichio/trading/data/governance/redteam_spec_v3_audit_20260422.md`（前回）
- `/Users/yuusakuichio/trading/data/governance/spec_v3_fix_plan_20260423.md`（Gemini 結果反映 draft）
- `/Users/yuusakuichio/trading/data/governance/gemini_verify/spec_v3_verdict_20260423_023223.md`
- `/Users/yuusakuichio/trading/common/kill_switch.py`（L187 / L244 / L397 衝突）
- `/Users/yuusakuichio/trading/common/idempotency.py`（L38-118 API 衝突）
- `/Users/yuusakuichio/trading/scripts/dead_man_switch.py`（L46 path 衝突）
- `/Users/yuusakuichio/trading/chronos_rules_plugin/mffu_flex.py`（L40-58 ハードコード vs yaml）
- `/Users/yuusakuichio/trading/.claude/hooks/andon_multichannel.py`（OK 一致）
