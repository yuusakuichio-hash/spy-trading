# Phase 1 完了条件定義（Phase 2 着手 gate）

**作成**: 2026-04-22 / Phase 1 → Phase 2 移行判定基準

## 目的
Phase 1「組織再編完遂」を**虚偽完了なく**締めくくり、Phase 2「新コード実装」に着手してよいかを客観的に判定する gate。

過去 9 度の虚偽完了パターンを防ぐため、**検証可能・定量的**な条件のみで構成する。

---

## A. 機能的完了条件（全件 GREEN 必須）

### A-1. Navigator agent 正式稼働
- 判定: 次セッション or それ以降で `subagent_type='navigator'` が Claude Code に認識される
- 検証: Agent tool で navigator 起動 → エラーなし応答
- 失敗時: Claude Code 再起動 / agents/navigator.md format 再確認

### A-2. 物理 hook の実稼働確認
- 判定: 今日登録済 hook 全件が PreToolUse / Stop で実行される
- 検証: 各 hook の log file（data/logs/ 配下）に直近 24h の実行記録あり
- 特に検証: `andon_multichannel --hook` / `legacy_write_block.sh` / `pronoun_guard.sh` / `estimate_historical_calibration.py`

### A-3. Andon Cord → 既存 bot 停止の完全経路
- 判定: Andon 発令 → `common.kill_switch.is_active() == True` → 既存 bot 6 箇所（`spy_bot.py` / `chronos_bot.py:1259,1701` / `chronos_agent.py:104,1036` / `pre_trade_check.py:145`）が発注停止
- 検証: E2E テスト（Phase 0 で 1 回成功・再現性確認）

### A-4. 外部 LLM 有効化フラグ判定完了
- 判定: `external_self_check_ENABLED` / `AUDITOR_GATE_ENABLED` の ON / OFF がゆうさくさん判断で確定
- 検証: 運用開始日を CURRENT_STATE.md に明記

### A-5. llm_budget 実稼働
- 判定: 月間上限内で収まる
- 検証: `python3 common/llm_budget.py summary` で OpenAI/Gemini の monthly_spent が cap 未満

### A-6. memory 整合性維持
- 判定: CURRENT_STATE.md が直近 3 日以内更新・リンク切れゼロ
- 検証: `inventory_dependency_map.py` 再実行で死コード候補 ≤ 5 件

---

## B. 規律的完了条件（全件 GREEN 必須）

### B-1. 虚偽完了候補の全解消
- 判定: 「実装した」「保存した」と宣言したもの全件が実在＆動作
- 検証: Phase 0 監査で発覚した 10 度目候補 4 件（Andon kill_switch 二重 / hook 未登録 / matcher 不正 / リンク切れ）全て修正済 ✅
- ペンディング項目の正直開示: pronoun_guard（実装済）・atlas_v3/tools/ast_antipattern.py（未実装 → Phase 2 で）

### B-2. 未実装宣言の整合性
- 判定: CURRENT_STATE.md「🔜 未着手」に記載の項目は全て未着手のまま（勝手に完了扱いしてない）
- 検証: grep で宣言と実態の一致確認

### B-3. Phase 1 中の虚偽完了件数
- 判定: Phase 1 期間中の新規虚偽完了事象 = **0 件**
- 検証: Navigator + Redteam ログで完了宣言と実態の乖離ゼロ

---

## C. 運用的完了条件

### C-1. ゆうさくさんの認知負荷許容内
- 判定: 1 日の承認判断件数が **月 3-5 件想定の範囲内**（Phase 1 中は準備期間のため多少多め可）
- 検証: ゆうさくさん自己申告（疲弊していないか）

### C-2. 新体制で仕様書 1 本を通せる dry-run
- 判定: Phase 2 仕様書 v3（Common or Atlas or Chronos）1 本を以下のフローで通せる
  1. Builder draft
  2. Navigator 並走監視（規律違反ゼロ）
  3. Redteam 独立検証
  4. Auditor（Gemini）三権外監査
  5. ソラ統合
  6. ゆうさくさん最終承認
- 検証: 実際に 1 本完走して全 pass

### C-3. Free Tier 制限内運用
- 判定: Gemini Free Tier（1500 RPD・15 RPM）を超過しない
- 検証: llm_budget の daily_request_count が cap 内

---

## D. Phase 2 着手 NO-GO 条件（どれか 1 つでも該当 → 着手禁止）

- ❌ 虚偽完了パターンが残存（解消されていない指摘あり）
- ❌ Andon → 既存 bot 停止の経路不確実
- ❌ Navigator agent 未稼働（subagent_type 認識せず）
- ❌ 物理 hook の実稼働が未確認
- ❌ 月 300 万目標達成への道筋が不明
- ❌ ゆうさくさんの認知負荷が限界（休息不足の自覚）
- ❌ Phase 1 中の新規虚偽完了 >= 1 件

---

## E. 段階的着手（partial GO 許容）

全件 GREEN 待ちで Phase 2 が永遠に始まらないリスク回避のため、以下は **partial GO** で着手可:

- C-1 ゆうさくさん認知負荷: 本人自己判断
- A-4 外部 LLM 有効化フラグ: デフォルト OFF のまま Phase 2 着手可（運用開始は Phase 2 途中でも可）
- A-2 hook 稼働確認: 主要 4 hook（legacy_write / andon / llm_budget / historical_calibration）動作確認で partial GO

ただし以下は **必須 GREEN**:
- A-1 Navigator 稼働
- A-3 Andon → 既存 bot 停止の完全経路
- B-1 虚偽完了候補全解消
- B-3 Phase 1 中虚偽完了ゼロ
- C-2 dry-run 成功

---

## F. 判定プロセス

1. Phase 1 終盤でソラが全条件を自己評価（A/B/C/D/E）
2. Navigator + Redteam + Auditor で独立検証
3. ゆうさくさんに結果提示・最終承認
4. 承認後 Phase 2 着手（CURRENT_STATE.md に記録）

## G. 虚偽完了防止の追加規律

- Phase 1 完了宣言時は **証跡 4 点セット**（grep / AST / pytest stdout / mutation）必須
- すべての条件に「検証方法」を明記
- 「だいたい完了」は禁止（PASS か DIFFER の二値判定）

---

## 関連
- `memory/CURRENT_STATE.md`（Phase 0 完了状況）
- `data/research/flow_audit_20260422.md`（Phase 0 監査結果）
- `data/governance/redteam_audit_phase0_20260422.md`
- `memory/feedback_bug_zero_absolute_20260422.md`（最上位規律）
- `memory/feedback_navigator_mandatory_20260422.md`
