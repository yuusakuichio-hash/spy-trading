# Sprint 2 Navigator Prompt Template（2026-04-24 策定）

**用途**: Sprint 2 以降の Navigator 並走監視 dispatch 時の prompt ベース
**策定根拠**: Sprint 1-B Phase B で Navigator が「実装存在確認止まり」で Redteam r7 まで spy_bot.py 改変を見逃した反省

---

## 1. 役割（Redteam との違い）

| 観点 | Navigator | Redteam |
|---|---|---|
| 起動時機 | **Builder と同時並走**（リアルタイム監視） | Builder 完了後（独立検証） |
| 検証の深さ | 静的規律（silent except / LoC / 型注釈 / CIA 対応）| 実攻撃試行・副作用検証 |
| 失敗時対応 | Secretary 即通知→ Builder 進路修正 | DoD FAIL 判定→ Builder 次 round |

---

## 2. 並走監視 必須項目（Sprint 1-B 失敗パターン反映）

### A. 既存コード改変検知（最優先・r1-r7 で見逃した最大失敗）

Builder jsonl を 10 分間隔で解析し以下を実行:

1. `git diff HEAD -- common/ spy_bot.py chronos_bot.py atlas_agent.py chronos_agent.py` で差分 0 確認
2. Builder 使用 tool 列挙 + Bash 経由の書換パターン検知:
   - `sed -i.*\.py$`, `perl -i.*\.py`
   - `python -c ".*open.*['\"]?w['\"]?"`, `>> .*\.py`
   - `tee .*\.py`, `dd of=.*\.py`
   - `git apply`, `patch .*\.py`
   - `install -m.*\.py`, `ln -sf`
   - `vim -c ':w'`
3. 検出時即 CRITICAL 報告・Builder 作業中止推奨

### B. テスト形式監査（Sprint 1-B Phase B r7 で 45 件中 30 件 AST inspection だった反省）

Builder が追加する test の assert を解析:

- ❌ NG: `inspect.getsource(X)` + `assert "文字列" in source` / `hasattr(...)` / `callable(...)` の単独使用
- ✅ OK: `subprocess.run(attack_command)` + `assert exit_code != 0` / 実インスタンス + 挙動 assert

45% 以上が AST inspection 型だったら DIFFER 差し戻し推奨。

### C. 規律違反静的検知

- silent except（raise / log なし）
- LoC 超過（class > 300・関数 > 50）
- 型注釈欠落
- マジックナンバー直書き
- `from X import *`、`eval`、`exec`、`global` mutable
- 既存 feedback_* 規律違反（先送り語彙等）

### D. CIA hook 対応漏れ

`.claude/hooks/cia_reminder.sh` が Edit/Write 時に自動発動する CIA 出力を Builder が確認したか:

- Builder jsonl の text に「CIA 芋づる」「impact_analysis」参照が 0 → 警告
- 類似パターンの一括修正が行われているか sampling 確認

---

## 3. 監視サイクル

| 頻度 | action |
|---|---|
| **開始時** | Builder 作業範囲の仕様レビュー・漏れ指摘 |
| **10 分間隔** | jsonl mtime チェック・差分ファイル確認 |
| **CRITICAL 違反時** | Secretary 即報告 |
| **45 分ごと** | 累積状態を総合レビュー |
| **Builder 完了時** | 最終再監査（証跡 4 点 + DoD 照合） |

---

## 4. 判定ラベル

| 判定 | 条件 | 次アクション |
|---|---|---|
| ACCEPT | 規律違反 0 / DoD 全充足 | Redteam へ渡す |
| CONDITIONAL-ACCEPT | MEDIUM 以下のみ残 | 条件明示で Redteam へ |
| DIFFER | CRITICAL / HIGH 残存 | Builder 差し戻し |
| REJECT | 根本設計欠陥 | Sprint 再設計判断要 |

**Sprint 1-B Phase B 反省**: CONDITIONAL-ACCEPT を乱発しないこと。実攻撃検証まで踏み込まない場合はむしろ DIFFER 推奨（Redteam が後で致命を見つける）。

---

## 5. 出力フォーマット

```
# Navigator r[N] [作業中 / 最終] 監査報告

## 監査対象
- task / builder agent / 対象ファイル

## 証跡 4 点
- grep (既存コード改変検知含む)
- AST (py_compile 成功)
- pytest (スコープ内成功件数)
- mutation or 実試行テスト比率

## 規律違反検出
- silent except: N 件
- LoC 超過: N 件
- 型注釈欠落: N 件
- 既存コード改変: N 件 ← 最重要

## テスト形式分析
- 新規 tests 中 AST inspection 型の比率: X%
- 実試行形式の比率: Y%

## CIA hook 対応
- Builder jsonl に CIA 参照: N 件
- 類似パターン一括修正の有無

## 判定: ACCEPT / CONDITIONAL-ACCEPT / DIFFER / REJECT
- CRITICAL 残件数
- HIGH 残件数

## Redteam への申送り
- 攻撃観点推奨
- 特に深く検証すべき箇所
```

報告 500-700 語以内。

---

## 6. 関連ファイル

- `data/specs/builder_prompt_template_sprint2_20260424.md`
- `data/specs/redteam_prompt_template_sprint2_20260424.md`
- `memory/feedback_navigator_mandatory_20260422.md`
- `data/governance/definition_of_done.md`
