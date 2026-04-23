# CLAUDE.md 244 行 全項目分類リスト（圧縮 draft 用）

**作成**: 2026-04-22 / 目的: 244 → 80-100 行 圧縮の判断材料

## 分類軸
- **CORE**: 核規律・新 CLAUDE.md に残す（最上位規律 + 鉄則 + 4 層構造概要）
- **MEMORY**: feedback_*.md / project_*.md に退避し、CLAUDE.md からは参照リンクのみ
- **OUTDATED**: 2026-04-22 大転換で古くなった・削除候補
- **STALE**: 動的に変わる情報（Atlas 実装済み機能等）→ CURRENT_STATE.md に集約済・CLAUDE.md からは削除

## 行ごとの分類

| 行 | セクション | 分類 | 理由 |
|---|---|---|---|
| 1 | タイトル | CORE | 残す |
| 3-8 | 絶対守るべき禁句TOP5 | CORE | 最上位規律・残す（ただし「Bot 停止禁止」は新方針で見直し可能性） |
| 10-12 | オーナー | CORE | 残す（短く） |
| 14-18 | 収入構造 | MEMORY | `user_company_and_retirement.md` 等へ退避・参照のみ |
| 20-25 | 資金計画 | MEMORY | `project_fx_transfer_timing_final.md` 等へ退避・参照のみ |
| 27-81 | 戦略 A 4 層構造 | MEMORY | `project_strategy_bcd.md` へ既に統合済・参照リンクのみ |
| 83-85 | セッション開始時必須手順 | CORE | 残す（ただし新方針で `prev_session_summary.md` + `CURRENT_STATE.md` 両方読む） |
| 87-88 | 現在の構成 | STALE | `CURRENT_STATE.md` に集約・削除 |
| 90-109 | Atlas 実装済み機能 | STALE | 全コード書き直しで再構築・**OUTDATED**・削除（v3 で再記載） |
| 111-112 | VPS 移行時手順 | CORE | 鉄則として残す（auth_budget の物理強制） |
| 114-115 | 認証情報 | MEMORY | `.claude/skills/credentials.md` 参照（既に移行済） |
| 117-118 | 本番移行判断 | OUTDATED | 旧方針・新 Phase 0-3 ロードマップに置換 |
| 120-122 | ET↔JST 時間対応表 | MEMORY | `.claude/skills/timezone-rules.md` 参照（既に移行済） |
| 124-129 | 戦略と戦術の違い | MEMORY | `feedback_strategy_first_principles.md` へ退避・参照 |
| 131-133 | 戦術の結果が想定と違った場合のルール | MEMORY | 同上 |
| 135-139 | 思考の優先順位 | MEMORY | `feedback_thinking_discipline.md` 等へ退避・参照 |
| 141-149 | 環境適応型の設計規律 | MEMORY | `feedback_no_fixed_params.md` へ退避・参照 |
| 151-153 | 不必要な待ち時間を作らない | MEMORY | `feedback_no_unnecessary_wait.md` へ退避・参照 |
| 155-168 | 判断・実行の基準 | MEMORY | `feedback_decision_criteria.md` へ退避・参照 |
| 170-177 | 調査品質基準 | MEMORY | `feedback_no_general_claims.md` へ退避・参照 |
| 179-188 | 実装前 7 ステッププロセス | MEMORY | `feedback_implementation_process.md` へ退避・参照 |
| 190-198 | 鉄則 | CORE | 残す（ただし鉄則 8「test_bot_e2e.py」は新コード前提で更新） |
| 200-202 | 絶対禁止事項（/compact） | CORE | 残す |
| 204-205 | Bot 稼働後の検討リスト | OUTDATED | 削除・現状にそぐわない |
| 207-216 | エージェント組織（旧 6 体） | OUTDATED | 新組織（4 常時 + 5 on-demand）に置換 |
| 218-238 | オーケストレーションルール | MEMORY | 一部 CORE（VPS 安全規則）+ 一部 MEMORY（GitHub 運用は project_remote_control.md へ） |
| 240-241 | VPS 通信チャンネル | MEMORY | `.claude/skills/vps-channels.md` 参照（既に移行済） |
| 243-244 | 戦略会議決定事項 | MEMORY | `project_strategy_bcd.md` 参照（既に移行済） |

## 圧縮 draft の構造案（80-100 行目標）

```markdown
# Sora Lab — Atlas/Chronos 自動売買 Bot プロジェクト
**最上位参照**: memory/CURRENT_STATE.md（数値・状態の全権威）

## 最上位規律（2026-04-22 確定・違反禁止）
1. バグなし絶対最優先（feedback_bug_zero_absolute_20260422.md）
2. Navigator（管理者）必須・builder 単独作業禁止（feedback_navigator_mandatory_20260422.md）
3. プライベート領域への指示禁止（feedback_no_private_life_intrusion_20260422.md）
4. 時間感覚規律（feedback_time_awareness_20260422.md）
5. 数値引用物理規律（feedback_no_numeric_citation.md）

## 絶対守るべき禁句 TOP5
1. 先延ばし語彙（後日・週末・本番移行前 等）禁止
2. 不要な確認禁止（明確に判断が必要な箇所のみ確認）
3. 場中バグで早期 Bot 停止提案禁止
4. 戦術を銘柄固定化禁止（マルチ銘柄マルチ戦術前提）
5. 「メモリに保存した」で対策完了扱い禁止（hook 物理化まで）

## オーナー & 目標
- ゆうさくさん（奈良・会社員・家族持ち・非エンジニア）
- 2026-10: 月 60 万達成（退職判定）
- 2027-04: 月 300 万達成（4 層構造）

## セッション開始時必須手順
1. memory/CURRENT_STATE.md を読む（最優先）
2. data/prev_session_summary.md を読む
3. memory/MEMORY.md インデックスから関連 memory を引く

## 鉄則（違反禁止）
1. 実装前に公式ドキュメント確認
2. VPS OpenD には触らない（auth_budget 物理強制）
3. ゆうさくさんへの確認は判断が必要な箇所のみ・準備までソラ単独
4. 通知は問題発生時のみ（EICAS 3 層分離・feedback_notification_policy.md）
5. プライベート時間に干渉しない
6. 市場・スケジュール言及前に common/market_specs.yaml + date 確認
7. 実装前 7 ステップ順守（feedback_implementation_process.md）
8. 新コードは pytest 全件 + Navigator + Redteam pass で初めて完了

## 絶対禁止事項
- /compact 禁止
- 既存コード（spy_bot.py / chronos_bot.py / common/）への書き込み禁止（legacy_write_block.sh で物理ガード）

## エージェント組織（2026-04-22 確定）
| 役割 | model | 起動 |
|---|---|---|
| Builder | Claude Opus | 実装時 |
| Navigator | Claude Sonnet（→ Phase 1 で Gemini Flash） | builder 並走 |
| Redteam | Claude Opus 別 session（+ GPT-5 補助） | 完了宣言時 |
| Auditor | Gemini Flash（重大時 OpenAI o3） | 三権外監査 |
| Secretary（ソラ）| Claude Opus | 窓口 |
| Ops | Claude Opus | 監視 |
| その他（strategist/analyst/sns/journal/governance）| Claude | on-demand |

## 詳細参照
- 戦略 4 層: memory/project_strategy_bcd.md
- 資金計画: memory/project_fx_transfer_timing_final.md
- 思考規律: memory/feedback_thinking_discipline.md / feedback_no_fixed_params.md / feedback_implementation_process.md / feedback_no_general_claims.md / feedback_decision_criteria.md
- 認証情報: .claude/skills/credentials.md
- 時間規律: .claude/skills/timezone-rules.md
- VPS 通信: .claude/skills/vps-channels.md

## 物理ガード hook（2026-04-22 整備）
- legacy_write_block.sh: 既存コード書換禁止
- andon_multichannel.py: Andon 3 経路（Pushover + ntfy + KILL_SWITCH）
- llm_budget.py: LLM コスト Hard cap
- estimate_historical_calibration.py: 見積もり甘さ物理防御
- claim_ledger_guard.py: 数値引用検証
- discipline_guard.sh: 先延ばし語彙検知
- 他: data/governance/inventory/hook_deps.json 参照
```

**目標**: 約 80 行（現 244 行から 67% 削減）

## 削除する情報の退避先確認

| 削除元 | 退避先 |
|---|---|
| 戦略 A 4 層 | project_strategy_bcd.md（既存・確認） |
| 資金計画 | project_fx_transfer_timing_final.md（既存・確認） |
| Atlas 実装済み機能 | atlas_v3/README.md（新規・追記必要）|
| エージェント組織旧版 | feedback_agent_naming.md / project_agent_organization_20260422.md（新規） |
| 思考規律詳細 | 既存 feedback_*.md（追記なし・参照のみ） |
| Bot 稼働後検討リスト | project_todo_20260412.md（既存・確認）|

## バグなし観点の確認事項

- [ ] 退避先の memory が全て存在するか確認
- [ ] 圧縮 draft で参照しているファイルが全て実在するか確認
- [ ] 削除した規律が誰も読まなくなる構造になっていないか
- [ ] 新セッション開始時の動作確認（CURRENT_STATE.md + 圧縮 CLAUDE.md で必要情報が揃うか）

**ゆうさくさん最終承認**: 圧縮 draft を見せて、「この内容で OK」を確認してから差し替え実行。
