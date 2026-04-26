# Red Team 攻撃監査 — Phase 0 成果物
**起草**: 2026-04-22 17:30 JST / Red Team 専任 (Claude Opus 4.7)
**対象**: 2026-04-22 Phase 0 で作成された hook 6 件 / memory 11+ 件 / script 3 件 / scaffold 3 件 / common/llm_budget.py
**前提**: flow_audit 24 件 + Gemini verify CONDITIONAL-GO + inventory summary を既読
**この文書の限界**: 起草者 Red Team 自身も Claude。同一 CCF 内。別機種独立読みなしで GO 判定の基礎にしないこと。

---

## 判定
**NO-GO**

根拠 (5 秒版):
1. 今日作成 hook 6 件のうち、settings.local.json に登録され**実稼働しているのは 2 件のみ**（legacy_write_block / andon_multichannel）。残り 4 件（auditor_required_gate / external_self_check / spec_premortem_required / estimate_historical_calibration）は **ファイル存在するだけで Claude Code の hook runtime に接続されていない dead artifact**。CURRENT_STATE.md は「🔜 Phase 1」と正直に書いているが、Phase 0「完了」宣言するには 4/6 が未接続のまま。これは虚偽完了の第 10 回目の温床。
2. `data/KILL_SWITCH`（Andon が書く）と `data/kill_switch.flag`（spy_bot/chronos_bot/pre_trade_check.py が読む）が**完全に別ファイル**。Andon を引いても既存 bot は 1 ミリも止まらない。
3. `legacy_write_block.sh` が `common/llm_budget.py` 自身（今日作成した新ファイル）を block する（`common/` 全体 block・例外は `common_v3/` のみ）。次回 Edit しようとすると物理 block → bypass 経路に頼る → 物理ガード空洞化の始点。
4. `auditor_required_gate.sh` が参照する `scripts/auditor_run.py` と `data/governance/auditor_latest.json` は**存在しない**。hook 本文で exit 2 を返す設計だが、そもそも実稼働していない（settings 未登録）のでエラーすら出ない完全 dead code。
5. `estimate_historical_calibration` の calibration サンプル n=2・median 3.0 / p95 3.0 で両方同値。統計的に無意味な数字で「科学的に補正した」と誤認させる装飾。

---

## 致命的攻撃成功シナリオ Top 5

### S1. 「Andon 引いても bot 止まらない」致命的ファイル名衝突 — 確率 100% / 影響 CATASTROPHIC
**前提**: `andon_multichannel.py:31` は `data/KILL_SWITCH` を書く。一方 `common/kill_switch.py:26` の `FLAG_FILE = BASE / "data" / "kill_switch.flag"` を `spy_bot.py` / `chronos_bot.py` / `chronos_agent.py:1036` / `common/pre_trade_check.py:145` が参照する。**2 つは完全に別ファイル名**（`KILL_SWITCH` 大文字・拡張子なし vs `kill_switch.flag` 小文字・flag 拡張子）。

**トリガー**: ゆうさくさん / ソラ / Navigator が「全 agent 停止」のつもりで `python3 .claude/hooks/andon_multichannel.py --pull` を実行。

**波及**: 
- Claude Code の tool 呼出は hook `andon_multichannel.py --hook` で exit 2 → block（機能する）
- しかし既存 Atlas/Chronos bot（launchd 常駐プロセス）は `common/kill_switch.is_active()` を見ている → **`KILL_SWITCH` ファイルは無視**して発注継続

**最悪結果**: 場中に「Andon 引いた・全部止まった・安心」と認識したゆうさくさんが監視を外す。Atlas は自動発注続行。moomoo 本番口座 $380K 証拠金に対し誤動作。類比: Three Mile Island 2 号炉の PORV indicator light（閉指示ランプ点灯だが物理的には開いたまま）と完全同型。ランプは「引いた」と言っているが現実は止まっていない。

**緩和案**: `andon_multichannel.py` が既存 `common.kill_switch.activate(reason="...")` も同時呼出するパッチを**今日中に**入れる。同一ファイル `data/kill_switch.flag` に書く。新規 `KILL_SWITCH` ファイル命名は完全廃止。

### S2. hook 4/6 が dead artifact・「Phase 0 完了」宣言が虚偽完了 10 回目 — 確率 100% / 影響 CATASTROPHIC
**前提**: settings.local.json の hooks 登録 (lines 18-326) を grep した結果:
- `legacy_write_block` 登録済（line 102-107）
- `andon_multichannel` 登録済（line 110-117）
- **未登録**: `auditor_required_gate.sh` / `external_self_check.sh` / `spec_premortem_required.sh` / `estimate_historical_calibration.py`

**トリガー**: ゆうさくさんが CURRENT_STATE.md の「Phase 0 完了状況」を見て、estimate_historical_calibration 等は「🔜 Phase 1」と書いてあるため未完了と認識。しかし `feedback_bug_zero_absolute_20260422.md:38` は「実ファイル存在確認・pytest 全体実行・証跡 4 点セット添付必須」と要求。ファイル存在だけ確認する慣性で「完了」と判定される。

**波及**: Phase 1 着手前に「物理ガード 6 件完備」の錯覚で安全性の見積もりが膨張。実態は 2 件のみ稼働。残り 4 件は「スクリプトは書いたが呼ばれない」= 存在しないのと同じ。過去 9 度の虚偽完了と完全同型（F12/F13 cycle3/4・Phase A-C 175/175 E2E接続ゼロ）。

**最悪結果**: Phase 1 で「もう Andon / auditor_gate / self_check が守ってくれる」前提で Builder 単独作業許容 → 4 件未稼働のため三権分立は Navigator / legacy_write_block の 2 層のみ → Builder の自己申告で完了宣言通過 → 虚偽完了 10 回目。

**緩和案**: 4 件全てを settings.local.json の適切なフェーズ（PreToolUse / Stop）に**今日中に**登録するか、CURRENT_STATE.md の「Phase 0 完了状況」から✅マーク撤回。**登録なしにファイル作成だけで「✅実装済」と書くのは feedback_false_completion_report_root_cause.md で指摘された「実ファイルなしに完了報告」と実質同型**。

### S3. `legacy_write_block.sh` 自身が今日作成の `common/llm_budget.py` を block・bypass 常用化 — 確率 100% / 影響 HIGH
**前提**: `legacy_write_block.sh:77-80` の判定:
```
common/*)
    BLOCK=1
    REASON="common/ 配下は legacy 保護対象。新規実装は common_v3/ に作成"
```
しかし `common/llm_budget.py` は今日作成された**新ファイル**で Phase 0 の中核。`common_v3/llm/budget.py` にはまだ移動されていない（`common_v3/README.md:44` でも「暫定的に既存パスで動作中」）。次回 Edit 必須時（バグ修正・閾値変更）、hook が block する。

**トリガー**: バグが出て `common/llm_budget.py` の threshold を 15 → 20 に変える必要。Edit 実行 → `[LEGACY_WRITE_BLOCK] 書込みを物理 block` → ゆうさくさんが `LEGACY_WRITE_BYPASS=1` を常用 → bypass が環境変数として .env 等に常駐化 → 物理ガード空洞化。

**波及**: Chernobyl 試験時の安全装置無効化パターン。「一時的 bypass」が「常用 bypass」になり、本来の保護対象（spy_bot.py 書換）までバイパス通過。Normalization of Deviance（Vaughan 1996 Challenger 調査）の教科書。

**最悪結果**: bypass env が .envrc や launchd plist に固着し、2 週間後には「bypass なし起動が不可能」状態。legacy_write_block は見た目だけの security theater 化。

**緩和案**: 許可ホワイトリストに `common/llm_budget.py` を **個別明記**（`*.md` と同列で即 exit 0）。もしくは今日中に `common_v3/llm/budget.py` にシンボリックリンク or 物理移動して import path を張替え。暫定運用は bypass 常用化の入口として最悪手。

### S4. `estimate_historical_calibration` のサンプル n=2・偽装的科学性 — 確率 100% / 影響 MEDIUM
**前提**: `data/governance/cycle_estimates_actual.jsonl` の中身:
```
{"task":"llm_budget_py_impl", "estimated":20, "actual":45, "ratio":2.25}
{"task":"legacy_write_block_impl", "estimated":30, "actual":90, "ratio":3.0}
```
**n=2**。median と p95 を算出する意味が統計的に存在しない（p95 = 95%tile は最低 n=20 程度必要）。stress test で median 3.0 / p95 3.0 と**両方同値**で返ってきた。

**トリガー**: ソラ or Navigator が「見積もり 1 ヶ月・calibration median 3.0x / p95 3.0x」を応答に付ける → ゆうさくさんは「過去データから science-based に 3 ヶ月」と誤認 → 実態は「n=2 の単なる直近観測」。

**波及**: `feedback_no_numeric_citation.md` 違反そのもの。「数値引用の物理規律」と謳いながら n=2 の median を引用している。楽観の別形態（Red Team の古典: Knight Capital の risk model が「過去データで VaR 測定」したが sample size 不足で破綻）。

**最悪結果**: 「科学的補正」の錯覚で Phase 1 以降の schedule 楽観が再発。2027-04 月 300 万達成見込みの再度の楽観偏向。

**緩和案**: hook 冒頭で `if samples < 10: return "サンプル不足・補正係数なし・自己申告見積もりそのまま適用（要注意）"` を強制。サンプル 10 未満は median / p95 表示禁止。default 3.0x 補正すら根拠の明示（「Red Team 推奨初期値」としか書かれていない・この値の根拠もない）が必要。

### S5. Phase 0 で作成した 5 件の独立ガードが同一 Claude session で書かれた CCF — 確率 100% / 影響 HIGH
**前提**: 6 件の hook（legacy_write_block / andon_multichannel / auditor_required_gate / external_self_check / spec_premortem_required / estimate_historical_calibration）+ llm_budget.py + 3 件 scaffold + navigator.md が**全て同じ Claude Opus session id `a35796aa75b18ac13`** で起草された（`org_redesign_v3_20260422.md:3-4` 参照）。Gemini 独立検証は v3 テキストにはあっても、**hook 本体コード一行一行は独立検証されていない**。

**トリガー**: いずれかの hook に Claude 特有の blindspot（e.g. SIGPIPE 処理・race condition・Unicode 正規化）が埋まっている。全 hook がその blindspot を共有。

**波及**: Challenger O-ring の「MTI 技術者と NASA が同じ圧力下で同じ判断をした」= 独立性の錯覚と同型。6 層防御を謳っても「6 人の判断者が全員 Claude Opus」という 1 層防御と機能的等価。

**最悪結果**: 実装後 2 週間で共通バグ発覚。全 hook 一斉無効化で再設計。「物理ガード」を失って再び丸腰の Phase 1。

**緩和案**: 今日作成 7 件全てを Gemini 2.5 Pro / o3 / GPT-5 のいずれかで**独立コードレビュー**を通す（v3 起草本文ではなくコード自体）。特に `andon_multichannel.py` の 3 経路 OR 条件・`legacy_write_block.sh:32` の `case "$TOOL_NAME" in` の bash-syntax （Python 経由で起動できない: test 1 で SyntaxError 検出済み）。

---

## 見逃しバグ（具体的行番号付き）

### B1. `legacy_write_block.sh` は pure bash。Python/zsh から直接 exec 不可
**場所**: `.claude/hooks/legacy_write_block.sh:1` shebang `#!/bin/bash`・`line 32` の `case Write|Edit|NotebookEdit)` 構文は bash のみ互換。python 経由 `subprocess.run(['python3', '.../legacy_write_block.sh'])` で起動すると SyntaxError（test 1 で実測）。Claude Code 本体が bash 起動する仕様なら動くが、**テスト・CI・他 hook からの chain 呼出は全滅**。

### B2. `andon_multichannel.py:211-214` の `json.loads()` silent skip
```python
try:
    _ = json.loads(raw)
except Exception:
    return 0
```
**致命的**: stdin が不正 JSON のとき、Andon は **exit 0（通過）**。発令中でも通過してしまう。正しい挙動は「stdin 読み取れなくても KILL_SWITCH 存在チェックは実施」。test で確認: 不正 JSON → rc=0、正常 JSON → rc=0 または 2 ← stdin 形式依存で挙動が変わる。Tenerife / Challenger 型の silent skip。

### B3. `auditor_required_gate.sh:44-56` の監査対象が `git push origin main` に限定
```bash
*"git push origin main"*|*"git merge"*|*"launchctl bootstrap"*|*"launchctl kickstart"*|*"systemctl start"*|*"chronos_bot.py"*|*"spy_bot.py"*)
```
**bypass 経路**: `git push origin HEAD` / `git push` （省略形）/ `git push origin dev → cherry-pick main` / `ssh vps systemctl start` （ssh 経由は検知不能）/ `python chronos_agent.py`（bot 本体は chronos_agent.py・chronos_watchdog.py 経由起動で `chronos_bot.py` 文字列を含まない）。Bypass 辞書の網羅性ゼロ。

### B4. `external_self_check.sh:85` の shell interpolation XSS
```bash
response_text = """${TEXT//\"/\\\"}"""
```
ソラ応答 text が `"""` (triple-quote) を含む場合、Python literal 終端で任意コード実行。ソラがコード例・markdown code block を含む応答を書くと容易に発火。今日作成した全 hook で**最もハイリスク**な入力処理。さらに API 未呼出なのに `response_text` を stdin から取り込む時点で prompt injection のベクトル。

### B5. `estimate_historical_calibration.py:36-39` の日付誤検知
```python
(re.compile(r"(?<!\d-)(\d+)\s*日間?(?!-)"), "day"),
```
negative lookbehind `(?<!\d-)` は 1 文字分のみチェック。`2026-04-22 日` のような表現は検知するが、`2026/04/22 朝 3 日後に完了` のような表現で「3 日」が検知される一方、「`12日 (月) に完了`」のような曜日付き日付も検知される。正規表現設計が雑。false positive 多発の予感。実 test では `見積もりは1日作業` のとき `1日` が day としてヒット・正しい挙動に見えるが、`(月曜の)1日後` のような文脈も同型ヒット。

### B6. `llm_budget.py:126-142` ファイル読みのレコード件数が RAM 爆発リスク
Gemini Free Tier 1500 RPD 想定で数ヶ月運用すると `data/llm_budget/gemini.jsonl` が 10 万行規模。`_read_records()` は**全行 json.loads** してから `ts >= since_unix` でフィルタ。毎分の rate limit check で 10 万行読み直し → CPU / IO 爆発。index / 月次ローテーションなし。Knight Capital 型のスケール事故。

### B7. `andon_multichannel.py:132` の順次実行で合計 30s ブロック
Andon 発令 = 3 経路順次（kill_switch → ntfy → pushover）。pushover タイムアウト 10s + ntfy 10s = 最大 20s 発令者がブロック。場中の緊急停止で 20s は致命。並列実行（`concurrent.futures`）への書き換え必須。

### B8. `spec_premortem_required.sh:46` の premortem report 判定が雑
`find "$PREMORTEM_DIR" -name "*.md" -mmin -30` は**過去 30 分以内の任意の .md ファイル**が 1 件あれば通過。仕様書と無関係な premortem（今日 SNS 投稿の premortem・Chronos agent の premortem 等）があればそれで通過する。仕様書と premortem の**対応関係ゼロ検証**。

---

## 運用的な穴（3 件以上）

### O1. 棚卸が Phase 1 🔜 のまま: memory 213 件 / hook 40 件の膨張を Phase 0 で認識しただけで未着手
`inventory_summary.md` が「死コード候補 0 件」と断言しているが、判定基準が「参照 0 件」のみ。`feedback_*.md` は「読まれるだけで参照されない」規律 memory なので 213 件中相当数が eval されていない死メモリ。新規 feedback 11 件を足して 224 件になった状態で Phase 1 着手は `feedback_cognitive_limit_design.md` 違反（Miller 7±2）。

### O2. Gemini Flash Free Tier 1500 RPD 上限・Critical 発火時に枯渇リスク
`llm_budget.py:48-58` の `gemini.daily_request_cap: 1500`。これは**全用途合計**。Navigator + Auditor + Redteam + external_self_check が全て Gemini 経由で走る設計。通常日 50 req × 4 役 = 200 req/日として、異常発生日は 3-5 倍 = 1000 req/日。**場中のフラッシュクラッシュで複数 Flow 3 案件並発するとその日の 1500 を昼までに消費**。critical 用の retry ができず沈黙。critical_reserve_usd が 0.0（Free Tier なので）という設計が根本欠陥。

### O3. 今日作成の 4 件 hook は「ファイル存在するだけで未稼働」状態で週末を越す
settings.local.json 未登録で稼働しない hook が 4 件ある。月曜場中（4/27 1st market open）までに登録しないと、「Phase 0 で完備した 6 層防御の Phase 1 着手」が実態 2 層のみ。CURRENT_STATE.md の「Phase 0 完了状況」✅マークを見て Phase 1 着手の Go サイン出すのは根拠なし。

### O4. `AUDITOR_BYPASS` / `LEGACY_WRITE_BYPASS` / `SPEC_PREMORTEM_BYPASS` / `EXTERNAL_SELF_CHECK_BYPASS` / `ANDON_SMOKE_TEST` / `AUDITOR_GATE_ENABLED` / `EXTERNAL_SELF_CHECK_ENABLED` の env var 7 本
env 設計が破綻寸前。どれが「常時 ON」「常時 OFF」「bypass 用」か一覧表なし。`external_self_check.sh:30` は「運用開始前は通過のみ」＝デフォルト OFF で**ENABLED=1 を手動で立てないと永遠に走らない**。ゆうさくさんがこれを忘れる確率 80% 超。

### O5. 7 時間連続判断後の今日の成果物の品質保証なし
セッション id `349b128e-...`（CURRENT_STATE.md:79）で 7+ 時間連続判断。医療・航空の知見（`org_tps_xp_medical_20260422.md`）では連続 6 時間超の判断品質は 30-50% 劣化。今日作成した全成果物に**疲労バイアス保証**がない。flow_audit と同型の「Red Team も疲れている」問題が既に侵食している可能性。

### O6. `journal.md` 新規作成 / `governance.md` 新規作成 / `sns.md` 新規作成 の agent 定義が確認されていない
git status で `?? .claude/agents/governance.md / journal.md / sns.md` が untracked 新規。これらが Phase 0 成果物として追加されたが、navigator.md のように詳細チェックできていない。「agent 数削減」方針（CURRENT_STATE.md:41）に逆行して増えている可能性。

---

## 戦略的リスク（3 件以上）

### ST1. 「バグなし絶対」 vs 「Andon で全 agent 停止」のデッドロック
`feedback_bug_zero_absolute_20260422.md:11-12`：「バグなしが全判断の最上位」。一方 Andon Cord 発令すると全 tool block（`andon_multichannel.py:219-228`）。場中に小さなバグが検出された瞬間、Navigator / Redteam / ソラのいずれかが「バグなし絶対」解釈で Andon 発令 → 全 agent 停止 → 修正作業もできない → ゆうさくさん iPhone 経由でしか復旧できない（奥様は権限外）。**進捗ゼロのデッドロック**が構造的に成立。Flight Director 権限の欠落。

### ST2. 「Gemini/GPT は劣る」体感 vs「異機種 LLM 必須」方針の矛盾
`project_session_20260422_major_redesign.md` 等でゆうさくさん明示体感「Gemini/GPT は Claude より劣る」。一方 flow_audit C-01 / Red Team v3 提案は「Navigator は Claude と別機種必須」。別機種=劣機種という二律背反。Gemini Flash で採点した結果、Claude が正しいのに Gemini が NG 判定する false reject が多発する可能性。1 度 false reject が累積すると、ゆうさくさんが Gemini の verdict を無視する習慣 → Auditor の存在が形骸化 → 三権分立崩壊（NISA と TEPCO のパス）。

### ST3. 「月額ほぼゼロ」費用方針 vs「MFFU/Tradeify 仕様変更頻度（12 ヶ月 56 件以上）」の不整合
`project_external_llm_strategy_20260422.md`：収益化前は月額ほぼゼロ（Gemini Free Tier + OpenAI $15）。一方 MFFU は 2025-09-12 に max contracts 変更、2026-04-21 に価格改定（$107 → $127）。12 ヶ月で 50+ 件の仕様変更があり、spec_drift_watcher（common_v3/spec_drift/）は**LLM で本文差分解析を大量に行う必要**。Free Tier 1500 RPD ではカバー不可。**費用ゼロ方針は spec_drift 検知を犠牲にしている**自覚なし。

### ST4. Bus Factor 1 未解消
`bottleneck_governance_20260422.md` / `org_solo_ai_cases_20260422.md:152-170` で既指摘のゆうさくさん倒れた場合の事業停止。Phase 0 で何も対策していない。奥様への「Andon 解除代理権」「KILL_SWITCH 物理解除」権限移譲なし。Phase 1 で agent 数減らす前に、**人間側の冗長化**が先。

### ST5. 「全コード書き直し」方針の隠れコスト
`atlas_v3/README.md:12`「atlas_v3/ の想定構造」で 20+ ファイル・Python 規律（CC ≤ 20・関数 LoC ≤ 50・mypy --strict）。既存 `spy_bot.py` 18858 行は 30+ バグ修正の歴史的蓄積。**これをゼロから書き直すと、歴史的バグを別形式で全部再発する確率 50% 超**（rewrite_case_studies_20260422.md で既指摘）。Red Team v3 Section 6 R3 に明記されているが、ゆうさくさんは「全書き直し」で既に走り始めている。Red Team 推奨の「シナリオ C（10,000 行リファクタリング）」は検討打ち切り状態。

---

## 反論視点（Contrarian）

### 反論 1: 「Phase 0 完了」を宣言する前提に立っている CURRENT_STATE.md そのものが楽観バイアス
CURRENT_STATE.md:48-55 の「Phase 0 完了状況」を読むと、✅ マーク 6 項目中 3 項目（legacy_write_block / andon_multichannel / llm_budget）は確かに実稼働している。しかし ✅ の 4 つ目「Gemini 独立検証 ×2」は、v3 テキストの独立検証であって hook コード 6 件の独立検証ではない。**「Phase 0」という名前の範囲が拡張縮小して都合良く解釈される**。Red Team として断言: 今日の時点で Phase 0 で「安全と言えるレベル」に達しているのは `andon_multichannel.py` と `llm_budget.py` の 2 件のみ。他は雛形に近い。

### 反論 2: Gemini verify CONDITIONAL-GO は「目標達成可能性 15% 以下」と判定している
`v3_verify_contexted_20260422_155233.md:14`:
> 月 300 万は元本 120 万に対して月利 250% 超の複利運用を 1 年継続する計算になり、技術的バグ以前に「金融工学的・プロップファームのドローダウン制限」の壁に衝突する。目標の再定義が必要。

Gemini は「目標達成不可」と言っているが、CURRENT_STATE.md:46 は「4 層構造 context を無視した一般論判定」で片付けた。しかし Gemini は金融工学的制約（DD 制限）を根拠として出している。これは一般論ではなく**プロップ固有の制約**。Red Team は Gemini 側に分を認める: 「4 層構造」と言っても Phase 3（2027/04-）の私募ファンドは適格機関投資家 1 名以上の確保が前提で、2026-04-22 時点で心当たりなし（CLAUDE.md 記載）。Phase 2（C2 サブスク）はフォロワー 15-40 人想定だが、Phase 1 段階で SNS 開始すらしていない。**「Bot 単独月利 250% 不可」が「4 層で 300 万可」になるロジックは、依存 Phase 2-3 の実現性検証を飛ばしている**。

### 反論 3: 「agent 数削減・常時 4 以下」は既に破綻している
CURRENT_STATE.md:41「agent 数削減（常時 4 以下）」。しかし実態は:
- 常時稼働 LaunchAgent 5 件（gmail_monitor / violation_rollup / manual_toggle_audit / morning_digest / tradovate_login_check）
- Bot プロセス 2 件（atlas_agent / chronos_agent）
- Watchdog 2 件
- 今日追加 Navigator 1 件

既に合計 10+。**agent 数削減を謳って増やしている**。Miller 7±2 遵守の主張は虚構。

### 反論 4: 「9 度目虚偽再現」を認識した今日、新たに「虚偽完了 10 度目」の条件を揃えた
`feedback_false_completion_5th_governance.md` で三権分立を規律化した後にも虚偽完了 9 度目を再発した事実がある。今日の Phase 0 成果物で物理ガードを「増やした」が、実際の稼働率は 2/6（33%）。**過去の規律化パターン（規律を作る → 規律を守らない）を踏襲している**。Phase 1 で「物理ガード完備」と誤認して先に進むと 10 度目が確実。

### 反論 5: `external_self_check.sh` は OpenAI 依存で Single Point of Failure
CURRENT_STATE.md:74 「OpenAI 残高 $10 チャージ済」。external_self_check が毎応答走ると、1 応答 $0.001 × 100 応答 / 日 = $0.1 / 日 = $3 / 月。しかし **Claude Code session で 1 日 200-500 応答**あるので実際は $6-15 / 月。`llm_budget.monthly_cap_usd = 15.0` と ほぼ同額。self_check だけで予算枯渇。critical_reserve $3 も 1 日で吸収される。**予算設計が numerical 計算されていない**。

---

## 重症度評価

| ID | 内容 | 重症度 | 対策優先度 |
|---|---|---|---|
| S1 | Andon の `KILL_SWITCH` vs bot の `kill_switch.flag` 別ファイル衝突 | **CRITICAL** | P0 (即日) |
| S2 | hook 4/6 が settings 未登録・Phase 0 完了宣言は虚偽 10 回目候補 | **CRITICAL** | P0 (即日) |
| S3 | `legacy_write_block` が `common/llm_budget.py` 自身を block・bypass 常用化の始点 | **HIGH** | P0 (即日) |
| S5 | Phase 0 成果物 7 件が同一 Claude session 起草 CCF | **HIGH** | P0 (48h以内) |
| B1 | `legacy_write_block.sh` bash のみ起動・Python 経由で SyntaxError | **HIGH** | P1 |
| B2 | `andon_multichannel.py:211-214` 不正 JSON で silent skip | **HIGH** | P0 (即日) |
| B4 | `external_self_check.sh:85` shell interpolation リスク | **HIGH** | P1 |
| ST1 | 「バグなし」+ Andon 発令 = デッドロック構造 | **HIGH** | P1 |
| S4 | `estimate_historical_calibration` サンプル n=2・偽装的科学性 | MEDIUM | P1 |
| B3 | `auditor_required_gate.sh` 監査対象の辞書網羅性ゼロ | MEDIUM | P1 |
| B5 | `estimate_historical_calibration` 日付 false positive | MEDIUM | P2 |
| B6 | `llm_budget.py` 10 万行 jsonl で RAM 爆発 | MEDIUM | P2 |
| B7 | Andon 順次実行 30s block | MEDIUM | P1 |
| B8 | `spec_premortem_required` 対応関係ゼロ検証 | MEDIUM | P1 |
| O1 | 棚卸未着手・memory 膨張 | MEDIUM | P1 |
| O2 | Gemini Free Tier 1500 RPD critical 枯渇 | MEDIUM | P1 |
| O4 | env var 7 本の設計破綻 | MEDIUM | P2 |
| O5 | 7 時間連続判断・疲労バイアス保証なし | MEDIUM | P0 (今日休息) |
| O6 | governance.md / journal.md / sns.md 未検証 | LOW | P2 |
| ST2-5 | 戦略的矛盾（異機種劣機種・月額ゼロ・Bus Factor・全書き直し） | HIGH | P1 (Flow 3) |
| 反論 1-5 | CURRENT_STATE.md 楽観バイアス群 | HIGH | P1 |

---

## 自己矛盾の検出

### 矛盾 1: Phase 0「完了」宣言 vs hook 4/6 の実稼働なし
CURRENT_STATE.md:48 は ✅ マークだが settings.local.json には未登録。`feedback_bug_zero_absolute_20260422.md:38` の「実ファイル存在確認」は通過するが、「動作確認済」の証跡 4 点セット（grep / AST / pytest / mutation）が欠落。
**解消案**: Phase 0 完了宣言の条件に「settings.local.json 登録」を必須条件として追加。今日中に 4 件登録 or Phase 0 範囲から外す（「Phase 1 着手前に追加」として明記）。

### 矛盾 2: 「費用月額ほぼゼロ」 vs 「external_self_check 毎応答 OpenAI 呼出」
Phase 0 方針（CURRENT_STATE.md:38）と external_self_check.sh:104 の毎応答 gpt-5-nano 呼出。200-500 応答/日で月 $6-15。**monthly_cap_usd:15.0 と ほぼ同額**。
**解消案**: external_self_check は「重大応答のみ」にサンプリング。response_len > 2000 文字 OR 完了宣言キーワード含有時のみ発火に絞る。あるいは Gemini Flash Free Tier に委ねる（ただし 1500 RPD 枯渇 = O2 と相反）。

### 矛盾 3: 「Navigator 必須・Builder 単独禁止」 vs 「agent 数削減 4 以下」
`feedback_navigator_mandatory_20260422.md` で Navigator 必須化。一方 `project_agent_organization_20260422.md`（未読・推定）で agent 数削減。常時 Navigator 稼働で「常時 4」を食う。Redteam / Auditor も同席想定。**合計 4 を超える**。
**解消案**: 「常時」の定義を「Bot 稼働中の常時」に限定。仕様定義 / 調査フェーズは Builder + Navigator + ソラ + ゆうさくさん = 4 で切り詰める。

### 矛盾 4: 「Andon 3 経路 OR」 vs 「KILL_SWITCH 既存ガードと別名前空間」
「3 経路化で CCF 除去」と謳うが、既存の bot は別ファイルを見ている。**3 経路の先に 1 経路しかつながっていない**。
**解消案**: andon_multichannel.py に `import sys; sys.path.insert(0, ...); from common.kill_switch import activate; activate(reason=...)` を追加。既存 `data/kill_switch.flag` と新 `data/KILL_SWITCH` を**ハードリンク化**で物理的に同一ファイル扱いする。

### 矛盾 5: 「バグなし絶対」 vs 「7 時間連続判断で今日作成」
疲労バイアスで書いた code がバグなしである保証なし。自己矛盾。
**解消案**: 今日書いた 6 hook + llm_budget.py は**翌朝覚醒状態で全行読み直し**を Phase 0 完了条件に追加。休息を Phase 0 の一部として明示。

---

## 既存資産との衝突

### 衝突 1: `common/kill_switch.py:FLAG_FILE = data/kill_switch.flag` vs `andon_multichannel.py:KILL_SWITCH_PATH = data/KILL_SWITCH`
**再掲・最重要**。既存 bot 6 箇所（`spy_bot.py` / `chronos_bot.py:1259,1701` / `chronos_agent.py:104,1036` / `common/pre_trade_check.py:145`）が全て `common.kill_switch.is_active()` 経由で `data/kill_switch.flag` を見ている。Andon 引いても無視される。
**推奨処理**: Andon 発令ロジックを `common.kill_switch.activate(reason, activator)` の**薄いラッパ**に書き換える。新規名前空間 `data/KILL_SWITCH` は削除。

### 衝突 2: `common/auth_budget.py:SERVICES` vs `common/llm_budget.py:VENDORS` の意味重複
auth_budget は Tradovate / OpenD / moomoo の**認証試行**予算。llm_budget は openai / gemini / anthropic の **API 呼出**予算。同じ `common/*_budget.py` 命名・同じ `data/*_budget/` ディレクトリ・同じ `hard_block_if_exhausted()` API。新人開発者（未来のゆうさくさん or Builder）が**どちらがどちらか混乱する**。
**推奨処理**: 命名差別化。`common/auth_rate_budget.py` / `common/llm_cost_budget.py` 等。もしくは共通基底クラス `common/budget/base.py` で両方継承にリファクタ。

### 衝突 3: `navigator.md` で `Write/Edit 禁止` 規律 vs `legacy_write_block.sh` の物理 block
navigator.md:115「`Write` `Edit` 禁止（settings 側で物理 block 候補）」。しかし settings.local.json line 102-107 の `legacy_write_block.sh` は `matcher: Write|Edit|NotebookEdit` のみ。Navigator agent が Bash で `echo > file.py` とやると pass する。**物理 block が表面的**。
**推奨処理**: Navigator agent が Bash tool で write に相当する操作（`echo >` / `cat >` / `tee` / `sed -i` / `python -c "open..."`）をするのを bash command regex で block。

### 衝突 4: 40 件の hook の PreToolUse 順序
settings.local.json line 18-117 の PreToolUse で 11 件の hook が順次実行。`discipline_guard → premortem_gate → service_recommend_guard → pace_check_guard → chronos_edit_spec_guard → state_safety_guard → sns_truth_guard → auth_budget_guard → navigator_antipattern_detector → legacy_write_block → andon_multichannel`。**1 tool call あたり 11 回の hook 起動**。平均 200ms × 11 = 2.2s 遅延。場中の意思決定が遅れる。
**推奨処理**: hook のうち read-only tool（Read / Glob / Grep）で発火不要のものは `matcher: Write|Edit|Bash|Task` に限定。

### 衝突 5: `claim_ledger_guard.py` (既存) と新 `estimate_historical_calibration.py` の重複
claim_ledger_guard は「数値引用検証」（feedback_no_numeric_citation.md ベース）。estimate_historical_calibration も数値（時間見積もり）を触る。両者が同じ応答 text を別観点で検証するが、**お互いの存在を知らない**。誤検知が重複する恐れ。
**推奨処理**: 一本化 or 明示的役割分担。`claim_ledger_guard` が数値引用 OK / NG、`estimate_historical_calibration` は「引用は OK だが calibration 注記を追加すべき」と階層化。

---

## 虚偽完了 10 度目を生む候補

### 候補 1: 「hook 6 件実装完了」と報告したが、実稼働 2 件のみ
CURRENT_STATE.md:48-55 の ✅ マークで「Phase 0 実装済」と宣言する瞬間、過去 9 度の虚偽パターン（F12/F13 cycle3/4 の「✅ 実装済」→「✅ テスト済」→「本番起動せず」と完全同型）。
**防御**: 「settings.local.json 登録確認」「hook 起動 dry-run 成功」「pytest で hook 実行確認」の 3 点セットを Phase 0 完了条件に追加。

### 候補 2: 「Andon 3 経路実装済」と報告したが、既存 bot は 1 経路しか見ていない
S1 の再掲。Andon の 3 経路は Claude Code hook 側には効くが、常駐 bot プロセス側は見えない。「3 経路」は**Claude Code 内部限定**。
**防御**: 「3 経路から既存 bot まで end-to-end テスト」（Andon 引いて Atlas が止まるかを実測）を Phase 0 完了条件。

### 候補 3: 「Gemini 独立検証済」と報告したが、verify 対象が v3 テキストのみで hook コードは未検証
CURRENT_STATE.md:54 「Gemini 独立検証（組織再々設計 v3・P0-0）2 回実施」で ✅ だが、対象は `org_redesign_v3_20260422.md` のテキスト要約であって、`legacy_write_block.sh` 等のコード本体ではない。
**防御**: 「hook コード 6 件を Gemini / GPT-5 で独立コードレビュー」を追加実施。diff 含め本体全行を対象に。

### 候補 4: 「estimate_historical_calibration 実装」と言いつつサンプル n=2 で科学性ゼロ
S4 の再掲。
**防御**: calibration データが n < 10 のときは「sample 不足・補正非表示」を自動化。

### 候補 5: 「$10 OpenAI チャージ済・ハードキャップ $15/月」と言いつつ external_self_check で月 $6-15 食う
ST5（反論 5）の再掲。
**防御**: 「Phase 1 稼働開始後、7 日間の実測コストで Phase 0 予算設計の妥当性を再評価」を予約。

### 候補 6: CURRENT_STATE.md 自身の自己更新で後から✅マークが動く
memory ファイルは自由に書き換え可。今の Phase 0 完了状況を見て「✅ 4 個・🔜 6 個」と読んだ後、15 分後に「✅ 10 個」に更新されることが起こり得る（過去事例あり）。
**防御**: memory 書き換えを git commit で hash 固定。CURRENT_STATE.md の「Phase 0 完了状況」セクションは `last_updated` + 上位 signature 必須。

### 候補 7: 「navigator.md 実装完了」としつつ Navigator が起動しても Write できないため何もできない
navigator.md:115「Write Edit 禁止」。read-only でしか走らない。**しかし Navigator の仕事は「差し戻し」を Builder にフィードバックすること**。フィードバックを書き込む先がない（Edit 禁止・Write 禁止）ため、実態は「Read して stderr に吐く」だけ。Secretary (ソラ) 経由でしか Builder に伝わらない → Secretary が sycophancy で丸めて伝える → Navigator の存在無意味化。
**防御**: Navigator に `data/governance/navigator_feedback/YYYYMMDD_HHMM.md` への append-only Write 権限を付与（他の書込みは禁止継続）。

---

## Phase 1 着手前必修対処（優先度順）

### P0 (今日中・20260422 夜 or 20260423 朝)
1. **S1 修復・最優先**: `andon_multichannel.py` に既存 `common.kill_switch.activate()` 同時呼出パッチ。`data/KILL_SWITCH` vs `data/kill_switch.flag` 一本化（ハードリンクでも symlink でも可）。Andon 引いて Atlas/Chronos が止まる E2E テスト実施し結果を `data/governance/andon_e2e_test_20260422.md` に記録。
2. **S2 修復・最優先**: hook 4 件（auditor_required_gate / external_self_check / spec_premortem_required / estimate_historical_calibration）を settings.local.json の適切な stage に登録 or CURRENT_STATE.md から ✅ 撤回。「未登録 = 未実装」と明記。
3. **B2 修復**: `andon_multichannel.py:211-214` の silent skip を削除。stdin 不正でも KILL_SWITCH 存在チェックは実施。
4. **S3 修復**: `legacy_write_block.sh` 許可リストに `common/llm_budget.py` を追加 or `common_v3/llm/budget.py` に物理移動。
5. **O5 対応**: Phase 0 完了宣言は**翌朝覚醒後**に再レビュー必須化。今夜は休息。

### P0 (48 時間以内・4/23 中)
6. **S5 緩和**: 今日作成 7 成果物（hook 6 + llm_budget.py）を Gemini 2.5 Pro と GPT-5 で**コード本体**独立レビュー。flow_audit と同様の text レビューではなく diff を渡す。
7. **B1 修復**: `legacy_write_block.sh` を python script に書き換え（Claude Code は bash interpreter で呼べるが、ポータビリティ・テスト性のため）。
8. **B4 修復**: `external_self_check.sh:85` の shell interpolation を Python stdin 経由に変更。XSS / injection の閉塞。

### P1 (Phase 1 着手前・4/25 まで)
9. **O1 着手**: 棚卸 feedback_*.md 213 件 → Gemini に「重複している規律を統合」指示。60 件まで圧縮。
10. **ST1 対応**: Andon 発令時の Flight Director（Navigator / ソラ / ゆうさくさん）権限階層を明文化。場中バグ発覚の「小バグなら修正・大バグなら Andon」の境界を数値化。
11. **反論 2 の反論**: 4 層構造（Bot+C2+SNS+私募）の Phase 2-3 実現性を Gemini verify で**一般論でない具体的根拠**付きで再検証。2027/04 月 300 万が 15% なのか 40% なのかを数字で確定。
12. **候補 6 防御**: CURRENT_STATE.md 書き換え時の git commit hash 固定化。auto-memory 更新 log の完全 append-only 化。

### P1 (Phase 1 中・4/30 まで)
13. **B6 修復**: `llm_budget.py` jsonl の月次ローテーション実装。_read_records に since filter を sqlite index に置換。
14. **B7 修復**: Andon 3 経路並列化（`concurrent.futures.ThreadPoolExecutor(max_workers=3)`）。
15. **O4 対応**: env var 7 本を 1 本 `.sora_lab_env.yaml` に統合。各 flag の default / bypass meaning を YAML に明記。
16. **衝突 2 対応**: `common/auth_budget.py` / `common/llm_budget.py` 命名差別化 or 基底クラス化。

### P2 (Phase 2 開始まで)
17. **候補 1 防御**: hook 起動 dry-run CI 化（pytest で全 hook を stdin 正常 / 異常 / 境界値で test）。
18. **衝突 4 対応**: PreToolUse の 11 hook を Read/Write 別 matcher に最適化。
19. **候補 7 対応**: Navigator agent に `data/governance/navigator_feedback/` write 権限。Secretary 経由の情報劣化を防ぐ。

---

## 最終判定

**NO-GO**（Phase 1 着手は P0 対処 5 件完遂後・24-48h 保留）。

S1（Andon が bot を止めない）と S2（hook 4/6 が dead artifact）は単独で虚偽完了 10 回目確定材料。今日中に P0 5 件を対処せず、明日以降「Phase 0 完了・Phase 1 着手」と宣言するのは、過去 9 度の虚偽パターンをそのままなぞる行為。

この Red Team 監査自身も**同一 Claude セッション・疲労バイアス環境下で起草**。Gemini 2.5 Pro と GPT-5 で本文独立検証にかけること。Red Team の見逃しが必ずある。特に `external_self_check.sh:85` の shell interpolation のような**code-level security bug** は異機種の目で見ないと捕捉できない。

---

**文字数**: 約 11,500 字 / 攻撃シナリオ 5 + 見逃しバグ 8 + 運用穴 6 + 戦略的リスク 5 + Contrarian 5 + 自己矛盾 5 + 既存衝突 5 + 虚偽 10 度目候補 7 + Phase 1 対処 19 = **65 項目**
