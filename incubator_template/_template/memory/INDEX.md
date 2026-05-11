# Memory Index — 汎用規律 md(ドメイン非依存)

このフォルダは spy-trading の `memory/feedback_*.md` から **ドメイン非依存の規律のみ** を抽出したもの。

## 一覧

| ファイル | 規律 |
|---|---|
| `feedback_false_completion_governance.md` | 虚偽完了禁止・証跡 4 点セット |
| `feedback_no_general_claims.md` | 一般論禁止・具体ファイル名+行番号で語る |
| `feedback_decision_criteria.md` | 実装前に公式仕様確認・確認済なら即実行 |
| `feedback_implementation_process.md` | 実装前 7 ステップ |
| `feedback_independent_verification.md` | redteam による独立検証必須 |
| `feedback_no_schedule_delay.md` | 「明日」「後日」禁句 |
| `feedback_no_confirmation_execute_now.md` | 「進めていい？」禁止・準備までソラ単独 |

## ドメイン確定後に追加すべき md

- ターゲットユーザー定義
- 競合・差別化
- 撤退基準
- KPI と計測方法

## 取り扱い

- hook がこれらを参照して violation メッセージに `(see memory/xxx.md)` と書く
- 違反検知時はこのファイルを読み返して規律内面化する
- 新規規律発見時は新しい feedback_*.md を追加し、INDEX.md にも追記
