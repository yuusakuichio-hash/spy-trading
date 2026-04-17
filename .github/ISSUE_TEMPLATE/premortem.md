---
name: Premortem (pre-implementation risk analysis)
about: New feature / fix / refactor — fill before implementation (Gary Klein premortem + HAZOP + ACH)
title: "[PREMORTEM] <short task title>"
labels: ["premortem", "todo"]
assignees: []
---

<!--
Sora Lab 規律: 新機能・修正を出す前に必ず premortem を埋めること。
unittestは「想定済み」しかカバーしない。本テンプレートは「想定外」を体系探索するための枠。
自動生成する場合は:
  python3 scripts/premortem.py --task "..." --files a.py,b.py
  → data/premortem_reports/<timestamp>.md の内容を以下にコピー
-->

## 1. タスク概要
- **目的**:
- **対象ファイル/モジュール**:
- **期待される挙動**:
- **関連Issue/PR**:

## 2. Premortem 宣言
> 「このタスクをリリースしたら、3 日後に致命的な失敗が発覚した。何が原因か？」

### 致命的失敗シナリオ (最低 10 件)

| id  | title | prob (L/M/H) | impact (L/M/H/C) | detection | mitigation |
|-----|-------|--------------|------------------|-----------|------------|
| F01 |       |              |                  |           |            |
| F02 |       |              |                  |           |            |
| F03 |       |              |                  |           |            |
| F04 |       |              |                  |           |            |
| F05 |       |              |                  |           |            |
| F06 |       |              |                  |           |            |
| F07 |       |              |                  |           |            |
| F08 |       |              |                  |           |            |
| F09 |       |              |                  |           |            |
| F10 |       |              |                  |           |            |

## 3. HAZOP Guide Words (全 11 語必須)

| word                  | 当該タスクでの逸脱 | 対策 |
|-----------------------|--------------------|------|
| No / None             |                    |      |
| More                  |                    |      |
| Less                  |                    |      |
| As well as            |                    |      |
| Part of               |                    |      |
| Reverse               |                    |      |
| Other than / Instead  |                    |      |
| Early                 |                    |      |
| Late                  |                    |      |
| Before                |                    |      |
| After                 |                    |      |

## 4. Competing Hypotheses (ACH)
主仮説「このタスクは意図通り動く」に対し、反証可能な競合仮説を最低 3 件。

### H1.
- evidence_for:
- evidence_against:
- test:

### H2.
- evidence_for:
- evidence_against:
- test:

### H3.
- evidence_for:
- evidence_against:
- test:

## 5. 総合判定
- **overall_risk**: low / medium / high / critical
- **decision**: GO / CONDITIONAL_GO / NO_GO
- **top3 blockers**: F__, F__, F__
- **required gates (実装前に必ずクリア)**:
  - [ ] 事前バックアップ
  - [ ] smoke test 項目定義
  - [ ] roll-back 手順文書化
  - [ ] 依存サービス healthcheck
  - [ ]

## 6. 実装後 verify チェック
- [ ] smoke test pass
- [ ] 既存回帰テスト pass
- [ ] ログ異常なし (30 分観察)
- [ ] roll-back 手順を実機で試した

---
_Auto-generable via `scripts/premortem.py`. Gary Klein premortem + HAZOP (IEC 61882) + ACH (Heuer)._
