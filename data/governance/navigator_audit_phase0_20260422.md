## Navigator 代替役 監査結果 — Phase 0 成果物総点検

**作成**: 2026-04-22 17:20 JST
**監査役**: Navigator 代替（本来の navigator agent type 未使用・Skill限定でソラ本体が代替実行）
**監査対象期間**: mtime 2026-04-22 00:00〜17:15 の全成果物
**監査方法**: `ls` / `find -newermt` / `Read` / `Grep` / `bash test` / 構文チェック / 動作 smoke test

---

### 概要
- 監査対象数: 23 件（memory 10 / hooks 6 / agents 2 / governance 5）
- 確認方法: ls + Grep + Read + Bash test (syntax / run)
- **CRITICAL 違反: 3 件**
- **HIGH 違反: 4 件**
- **MEDIUM 違反: 5 件**
- 動作確認済 / 未確認: 15 / 8

---

### CRITICAL（即修正必須・Phase 1 着手前にブロッキング）

#### C-1: `settings.local.json` L111 `matcher: "*"` が正規表現として不正
- 対象: `/Users/yuusakuichio/trading/.claude/settings.local.json` L102-118 の PreToolUse ブロック
- 違反: 該当箇所だけ `"matcher": "*"`（他は全部 `".*"`）
- 確認コマンド: `python3 -c "import re; re.match('*', 'Bash')"` → `nothing to repeat at position 0` エラー
- 影響: **`andon_multichannel.py --hook` の PreToolUse hook が正常動作しない可能性**。KILL_SWITCH が作成されていても tool 呼出を block できない → Andon 物理ガード骨抜き
- 推奨対処: `"*"` → `".*"` に修正

#### C-2: `common/kill_switch.py` と `andon_multichannel.py` の KILL_SWITCH 二重構造（設計衝突）
- 既存: `common/kill_switch.py` → `data/kill_switch.flag`（spy_bot / webhook_server が利用・TTL キャッシュ廃止済 hardening 版）
- 新規: `andon_multichannel.py` → `data/KILL_SWITCH`（別パス・別フォーマット JSON）
- 違反: どちらを正とするかの統合設計が未定義。Bot 側は `data/kill_switch.flag` で発動、Claude Code hook は `data/KILL_SWITCH` で block、相互連動なし
- 影響: Andon 発令時に Bot が止まらない / Bot 側 kill 時に Claude Code hook が block しない → 二重の真実
- 推奨対処: 一方を master 化、他方はシンボリックリンクまたは同期 hook で統一（`project_agent_organization_20260422.md` に設計図追加）

#### C-3: `data/research/integration_final_20260422.md` 実在しない（参照リンク切れ）
- 参照元:
  - `memory/feedback_navigator_mandatory_20260422.md` L77
  - `memory/project_session_20260422_major_redesign.md` L62「統合 F: `integration_final_20260422.md`」
- 確認: `ls data/research/integration_final_20260422.md` → No such file or directory
- 影響: 「R-14 三権分立物理化」の原典参照先が存在しない = 根拠消失
- 推奨対処: 実在ファイル名に修正（`org_redesign_v3_20260422.md` か）、または欠落分を生成

---

### HIGH（Phase 1 着手前に修正）

#### H-1: 新規 hook 4 件（auditor_required_gate / external_self_check / spec_premortem_required / estimate_historical_calibration）が `settings.local.json` 未登録
- 対象: 前 3 件は `registered_in_settings: false`（`data/governance/inventory/hook_deps.json` 参照済）
- 確認: Grep `auditor_required_gate|external_self_check|spec_premortem|estimate_historical_calibration` → settings.local.json 内ヒット 0
- 各 hook 自体は `*_ENABLED=1` or 環境変数で default OFF にはなっているので害は無い
- しかし「Phase 0 で整備済」という CURRENT_STATE / project_agent_organization の記述と**乖離**
- `CURRENT_STATE.md` L55「🔜 estimate_historical_calibration hook・auditor_required_gate・spec_premortem_required・deadman 等（Phase 1）」で「🔜」扱いなので整合はしているが、`project_agent_organization_20260422.md` L64-66 では「新規実装（2026-04-22）」として記載・有効化 OFF は注記あるものの **settings 未登録の事実が書かれていない**
- 推奨対処: Phase 1 有効化時に settings.local.json 追記

#### H-2: `.claude/agents/builder.md` が新規律（バグなし絶対 / Navigator 必須）を反映していない
- 対象: `/Users/yuusakuichio/trading/.claude/agents/builder.md`
- 確認: `grep -E "Navigator|navigator|バグなし" builder.md` → **0 ヒット**
- mtime は 2026-04-22 03:43 だが内容は 2026-04-20 R-1 規律まで
- 影響: builder agent 起動時に Navigator 並走規律・`feedback_bug_zero_absolute_20260422.md` を参照しない
- 推奨対処: builder.md に「Navigator 並走必須・単独作業禁止・`feedback_bug_zero_absolute_20260422.md` 必読」追記

#### H-3: navigator.md の model 指定と memory 記述の乖離
- `.claude/agents/navigator.md` L4 `model: sonnet`（Claude Sonnet）
- `project_agent_organization_20260422.md` L24 「Claude Sonnet（Phase 1 で Gemini Flash へ）」
- 設計意図として Phase 1 で切替は明記されているが、**現時点では Claude 同系列で self-preference bias が完全には除去されない**（flow_audit 指摘の CCF）
- 嘘ではないが「三権分立物理化」の名の下で Claude 同系列で運用することの不完全性を応答内で言明すべき
- 推奨対処: navigator.md に「現状は Claude Sonnet で Phase 1 限定運用・Gemini 切替で本格稼働」と注記

#### H-4: `atlas_v3/tools/ast_antipattern.py` 未実装だが「機械検証層 L3」として記載
- `project_agent_organization_20260422.md` L54 「L3 AST 検査 | atlas_v3/tools/ast_antipattern.py（新規） | silent except / CC>20 / LoC>500」
- 確認: `ls atlas_v3/tools/` → `No such file or directory`
- 影響: Phase 1 着手前提が整っていない（新実装の順序が詰められていない）
- 推奨対処: Phase 1 タスクリストに明記（既に「🔜」扱いなら OK だが L54 の書き方は「存在する」ように読める）

---

### MEDIUM（時間あれば・Phase 1 中に修正）

#### M-1: `feedback_time_awareness_20260422.md` と `feedback_no_private_life_intrusion_20260422.md` の境界表現が誤読を招く
- time_awareness L32 「休息優先で OK」「明日以降でも問題ありません」 = 指示
- no_private_life_intrusion L20 「今日は休んでください」「休息日を設定すべき」= 禁止
- time_awareness 自体の L40「区別」で両立説明はあるが、本文の「休息優先で OK」は私→ゆうさくさんへの**推奨形式**に読める
- 推奨対処: time_awareness の「休息優先で OK」を「急ぐ必然性はない」等の判断材料提示へ書換

#### M-2: `project_session_20260422_major_redesign.md` L22「memory 物理刻印済」表現が自己違反の芽
- 同ファイル L126 で「memory に書いた = 対策完了」は禁句 TOP 5 #5 違反と明記
- L22 の「memory 物理刻印済」は「刻印 = 書いた = 対策完了」と誤解される表現
- 推奨対処: 「memory 記載 + 対応 hook 未実装（Phase 1 にて）」等、hook 実装有無を明示

#### M-3: `time_estimate_sanity.sh`（既存）と `estimate_historical_calibration.py`（新規）の機能境界未整理
- 両者とも時間見積もり検知
- 既存: 警告のみ（BLOCK しない）・UserPromptSubmit
- 新規: calibration 係数注記追加・mode は check / record / summary
- 重複はしないが、役割分担が memory で明文化されていない
- 推奨対処: `project_agent_organization_20260422.md` か新規 feedback に役割分担を記述

#### M-4: `spec_premortem_required.sh` の premortem 紐付けがザル
- L44 「直近 30 分以内の premortem report 存在確認」が単純 mtime 検索
- 実際: 今回監査起動時に premortem.py が回って recent report 作成済だったので、全く別の task の premortem でも spec v3 書込を通過する
- 影響: Phase 1 実運用時に premortem bypass 事故の温床
- 推奨対処: premortem report の front-matter に対象 task 名を記録・hook 側で file_path と target task の突合必要

#### M-5: CURRENT_STATE.md L55「deadman」と既存 `scripts/dead_man_switch.py` の命名揺れ
- CURRENT_STATE は「deadman hook」と書いているが、実体は `scripts/dead_man_switch.py`（LaunchAgent 登録済 既存）
- hook ではなく script なので、CURRENT_STATE の書き方は誤解を招く
- 推奨対処: 「Phase 1 で dead_man_switch 拡張（beacon 多様化）」等の正確な記述へ

---

### 既存資産との関係マトリクス

| 新規成果物 | 既存重複 | 補完関係 | 衝突 |
|---|---|---|---|
| andon_multichannel.py | なし | kill_switch.py（Bot 側）と補完すべき | **あり（C-2: KILL_SWITCH パス違い）** |
| legacy_write_block.sh | なし | chronos_edit_spec_guard.sh と補完 | なし |
| auditor_required_gate.sh | なし | （Phase 1 有効化） | なし |
| external_self_check.sh | sycophancy_detector.sh（既存）と方向性近い | 既存 5 軸 + 別機種採点 | なし |
| spec_premortem_required.sh | premortem_gate.sh（既存）と重複の可能性 | 両方 PreToolUse・対象が違う | 要確認（今回は未比較） |
| estimate_historical_calibration.py | time_estimate_sanity.sh（既存） | 警告 vs calibration で役割分担可能 | なし・ただし M-3 境界未整理 |
| navigator.md | redteam.md / governance.md と役割重複の可能性 | 三権分立で補完 | なし |
| feedback_time_awareness_20260422.md | feedback_effort_estimate_2_3h.md / feedback_builder_time_estimate_minutes.md | 補完 | なし・ただし M-1 境界曖昧 |
| feedback_no_private_life_intrusion_20260422.md | なし（新規領域） | | なし |
| feedback_bug_zero_absolute_20260422.md | feedback_false_completion_5th_governance.md / feedback_implementation_process.md | 最上位規律として補完 | なし |
| feedback_navigator_mandatory_20260422.md | feedback_false_completion_5th_governance.md（三権分立の既存版） | Navigator 役割を追加 | なし（補強） |
| feedback_no_numeric_citation.md | claim_ledger_guard.py（既存） | 物理 guard と補完 | なし |
| feedback_market_24h_vs_session_confusion.md | 既存 `feedback_market_schedule.md` / `feedback_timezone_rule.md` | Chronos 24h を追加 | なし |
| project_agent_organization_20260422.md | `feedback_agent_naming.md` / 旧 CLAUDE.md L207-216 | 新組織で上書き | 旧 CLAUDE.md の agent 組織と形式的衝突（classification で OUTDATED 指定済） |

---

### 動作確認テスト結果

| hook/script | テストコマンド | 期待 | 実際 | 判定 |
|---|---|---|---|---|
| legacy_write_block.sh (common/*) | `echo '{...common/test_file.py...}' \| sh` | exit 2 + block msg | exit 2 + block msg | **PASS** |
| legacy_write_block.sh (common_v3/*) | `echo '{...common_v3/new.py...}' \| sh` | exit 0 | exit 0 | **PASS** |
| legacy_write_block.sh (spy_bot.py) | `echo '{...spy_bot.py...}' \| sh` | exit 2 + block | exit 2 + block | **PASS** |
| auditor_required_gate.sh (default) | `echo '{...git push...}' \| sh` | exit 0 (OFF) | exit 0 | **PASS** |
| auditor_required_gate.sh (ENABLED=1) | `AUDITOR_GATE_ENABLED=1 ...` | exit 2 + NO-GO | exit 2 + NO-GO | **PASS** |
| spec_premortem_required.sh (v3/*.md) | `echo '{...v3/atlas.md...}' \| sh` | premortem 無ければ exit 2 | exit 0（recent premortem 存在） | **SKIP**（M-4 参照） |
| spec_premortem_required.sh (v2/*.md) | `echo '{...v2/atlas.md...}' \| sh` | exit 0（対象外） | exit 0 | **PASS** |
| estimate_historical_calibration.py --check | 「2週間かかる見積もり」 stdin | notice 出力 | notice 出力（detect 2 件・median 3.0x） | **PASS** |
| external_self_check.sh (default) | 300 文字超 stdin | exit 0 (OFF) | exit 0 | **PASS** |
| andon_multichannel.py --check | CLI 実行 | INACTIVE | INACTIVE | **PASS** |
| andon_multichannel.py --hook | via settings `matcher: "*"` | block at KILL_SWITCH active | **C-1 で動作疑義あり** | **FAIL（matcher 異常）** |
| 全 hook 構文チェック | bash -n / python ast.parse | SYNTAX_OK | 全 6 件 OK | **PASS** |

### 嘘の検出

| 主張 | 実態 | 訂正 |
|---|---|---|
| `project_session_20260422_major_redesign.md` L62「統合 F: `integration_final_20260422.md`」 | 実ファイルなし | C-3 参照・修正要 |
| `feedback_navigator_mandatory_20260422.md` L77「`data/research/integration_final_20260422.md`」 | 実ファイルなし | C-3 参照・修正要 |
| `builder.md` mtime 2026-04-22 だが内容は 2026-04-20 規律まで | 実質未更新 | H-2 参照・追記要 |
| `project_agent_organization_20260422.md` L61 「新規実装（2026-04-22）」として 6 hook 列挙 | うち 4 件は settings 未登録・事実上 dead hook | H-1 参照・登録 OR 記述修正 |
| `CURRENT_STATE.md` L52「`.claude/hooks/andon_multichannel.py` 実装（...settings 登録済）」 | settings 登録は `matcher: "*"` で不正（C-1）・実行されない疑義 | C-1 参照・修正要 |
| `project_agent_organization_20260422.md` L54 「`atlas_v3/tools/ast_antipattern.py`（新規）」 | 実ファイルなし | H-4 参照・未実装明記要 |

### 参照リンク切れ

| 参照元 | 参照先 | 実在 |
|---|---|---|
| `feedback_navigator_mandatory_20260422.md` L77 | `data/research/integration_final_20260422.md` | **❌ MISSING** |
| `project_session_20260422_major_redesign.md` L62 | `data/research/integration_final_20260422.md` | **❌ MISSING** |
| `project_agent_organization_20260422.md` L54 | `atlas_v3/tools/ast_antipattern.py` | **❌ MISSING（未実装・記述誤り）** |
| 他 15 件の参照 | 各種 `data/research/*.md` `memory/feedback_*.md` | ✅ 全て実在 |

### 動作確認未完了（Phase 1 着手前に確認必要）

1. `spec_premortem_required.sh` で対象 task 紐付け動作（現状は単純 mtime だけ・誤通過検証要）
2. `external_self_check.sh` 実運用時の GPT-5 Nano API 疎通（OPENAI_API_KEY 実在だが API 呼出は未テスト）
3. `auditor_required_gate.sh` ENABLED + auditor_latest.json 存在ケース（JSON 健全性 + verdict + 鮮度 3 判定）
4. `andon_multichannel.py` の 3 経路全失敗時の挙動（現状 1 経路成功でOK だが、全失敗で何をすべきか未定義・docstring L14「要別実装」）
5. legacy_write_block.sh の bypass 環境変数（LEGACY_WRITE_BYPASS=1）動作
6. settings.local.json の hook order 依存性（discipline_guard → premortem_gate → ... → legacy_write_block → andon）の実効順序
7. `memory_completion_tracker.sh` と新規 memory との互換
8. builder.md 実際に起動した時の memory reload 挙動

---

### Navigator 代替役 判定

**DIFFER（差し戻し）**

Phase 1 着手 **NO-GO**。以下 CRITICAL 3 件と HIGH 2 件（H-1 / H-2）の修正後に再監査。

#### 差し戻し詳細
- **C-1（matcher "*"）**: Phase 1 前に必須修正。andon の物理ガードが効かないまま組織再編は危険。1 分で修正可能。
- **C-2（KILL_SWITCH 二重構造）**: 設計文書に統合方式を 1 行でも追記すれば差し戻し解除可。実装は Phase 2。
- **C-3（integration_final リンク切れ）**: ファイル名訂正 or 欠落分を代替ファイル名で更新。5 分で修正可能。
- **H-1（hook 未登録）**: `project_agent_organization_20260422.md` に「Phase 1 有効化時に settings 登録」と明記修正。または Phase 0 完了宣言の範囲を縮小。
- **H-2（builder.md 未更新）**: Phase 1 で Navigator 並走が必須になる以上、builder.md の新規律反映は**必須**（builder 起動時の実読箇所）。

#### Phase 1 着手 GO 条件
1. C-1 修正（matcher "*" → ".*"）
2. C-3 修正（リンク切れ解消）
3. H-2 修正（builder.md に Navigator 並走・バグなし絶対を追記）
4. C-2 / H-1 / H-3 / H-4 は**記述訂正で暫定 GO 可**（実装は Phase 1 内で継続）
5. MEDIUM 5 件は Phase 1 中の対応で可

#### 評価で留意した事項
- Phase 0 の成果物として「default OFF 設計」は sensible（運用前に副作用を起こさない）
- 新規 memory の分量は過剰ではなく・既存 MEMORY.md index への統合も済
- inventory スクリプト（`scripts/inventory_dependency_map.py`）・claudemd_classification は基準が明確で棚卸に有用
- Navigator 役（本監査）も Claude Opus 4.7 で実行している点で self-preference bias の可能性あり・Gemini or redteam との double-check 推奨
