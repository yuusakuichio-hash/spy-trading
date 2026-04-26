---
name: governance
description: Sora Lab 三権分立・完了判定ガバナンス規約（2026-04-20 R-3新設）
---

# Sora Lab 三権分立ガバナンス

虚偽完了パターン5回連続発生（F12/F13 / Chronos agent/watchdog / auto_resume Guard / Phase A-C前回 / Phase A-C/D今回）を受け、Blue Team自己採点体制を廃止し三権分立を導入する。

## 三権

### builder（実装権）
- コード・インフラ・設定を実装する
- **自己完了宣言権なし**
- 「完了」と書けるのは証拠4点セット（実grep・AST・fixture・mutation）が揃った時のみ
- 自己テストは一次検証扱い・採点材料にならない

### redteam（採点権）
- builderの実装を敵対視点で検証する
- grep/AST/fixture/mutation の4点で採点
- 自作テストではなく`tests/redteam_fixtures/`共有fixtureで検証
- GO/NO-GO判定を明示する
- 「多分OK」「おそらく」禁止・実証のみ

### secretary（判定権・オーケストレーション）
- redteam採点結果でGO/NO-GO判定する
- ゆうさくさんへの報告・次アクション決定
- builderの自己完了宣言を鵜呑みにしない
- NO-GO時は即次サイクル投入（「明日」「2-3日」禁句）

## フロー

```
ゆうさくさん指示
    ↓
secretary（計画立案）
    ↓
builder（実装・証拠4点提出）
    ↓
redteam（独立採点）
    ↓
secretary（GO/NO-GO判定）
    ↓
GO: ゆうさくさん報告 / NO-GO: 即builder差し戻し
```

## 禁止事項

- builder自身が「完了」と宣言すること
- redteamがbuilder作成テストだけで採点すること
- secretaryがbuilder宣言を鵜呑みにしてゆうさくさんに報告すること
- 同一session内のbuilder+redteamで癒着採点すること（独立session必須）

## 虚偽完了検出メカニズム

1. **.claude/hooks/blue_team_bias_detector.sh** — builder完了宣言時に自動grep検証
2. **CI mutation testing** — mutation score<50%はmerge block
3. **tests/redteam_fixtures/** — 外部API実スキーマfixture共有
4. **過去事案メモリ** — project_false_completion_*.md で系譜追跡

## メモリ参照
- feedback_false_completion_report_root_cause.md
- feedback_independent_verification_mandatory.md
- feedback_schema_contract_test_mandatory.md
- project_false_completion_4th_20260420.md
- feedback_false_completion_5th_governance.md
