# Sprint 2 Builder Prompt Template（2026-04-24 策定）

**用途**: Sprint 2 以降の Builder dispatch 時に prompt のベースとして使用
**策定根拠**: Sprint 1-B Phase B r1-r7 累積教訓（Normalization of Deviance 再発防止）

---

## 1. 必須規律（全 Builder dispatch に含める）

### A. 既存コード改変禁止
- `common/` `spy_bot.py` `chronos_bot.py` `atlas_agent.py` `chronos_agent.py` は**絶対触らない**
- **Bash 経由の編集も禁止**: `sed -i`, `perl -i`, `python -c "open(...,'w').write"`, `echo >>`, `tee`, `dd`, `git apply`, `patch`, `install`, `ln -sf`, `vim -c ':w'` 等すべて使用禁止
- Sprint 2 冒頭の allowlist hook (C-018・`scripts/lock_legacy_files.sh lock`) 適用後は OS 層で書換不可
- 変更が必要な場合は **allowlist hook unlock → legitimate edit → 即 re-lock** の手順

### B. DoD 厳格
- CRITICAL 実害高 == 0
- HIGH == 0
- 回帰 == 0
- carryover 新規起票 == 0（技術的不可能な場合のみ例外・明示記録）
- Navigator PASS / Redteam PASS 両方必須

### C. テスト形式
- **AST inspection 型禁止**: `inspect.getsource()` + `assert "文字列" in source` は不可
- **実試行形式必須**:
  - Bash/subprocess で実コマンド実行
  - 実クラスインスタンス化して実挙動確認
  - mock 禁止ではないが、可能な限り実コード到達
- 攻撃ベクトルテストは実攻撃を subprocess で試行 + exit code / 副作用 assert

### D. 芋づる式 CIA 活用
- Edit/Write 時に `.claude/hooks/cia_reminder.sh` が自動発動
- CIA 出力に含まれる **類似パターン** を必ず確認し、同時修正
- 単発修正で済ませず、同じ書き方のバグを全件潰す（Defect Clustering）

### E. Regression Ledger 遵守
- `tests/test_regression_ledger_20260424.py` が pytest で 5/5 pass 維持
- 新規発見した regression は本 ledger に追記

---

## 2. 完遂宣言時の証跡 4 点セット

必ず以下を提出:

1. **grep**: 実装箇所の物理存在確認
2. **AST**: `ast.parse()` 成功
3. **pytest stdout**: `pytest tests/` 全件走行結果（passed / failed / skip の数値）
4. **mutation** or **実試行テスト**: AST inspection ではなく実挙動検証

失敗カウントは Builder 自己申告でなく、**Navigator / Redteam が独立実行で確認**した数値を最終判定とする。

---

## 3. Navigator 並走必須

- Builder dispatch と同時に Navigator dispatch（逐次運用禁止）
- Navigator は Builder の jsonl を 10 分間隔で解析
- CRITICAL 違反検出で Secretary 即報告
- Builder 完了時に最終再監査

Redteam は完了後に独立検証（敵対攻撃試行・AST inspection 禁止）。

---

## 4. Normalization of Deviance 警戒

Sprint 1-B Phase B r1-r7 で繰り返された失敗パターン:

| ❌ Anti-pattern | ✅ 正しい対処 |
|---|---|
| blacklist regex にパターン追加 | allowlist 設計へ転換 |
| isinstance 化だけ | isinstance + runtime invariant 検証 |
| hook にガード追加 | hook 自身の bypass 経路を潰す |
| テスト件数を増やす | 実攻撃試行テストの比率を上げる |
| symptom 対処 | root design 変更 |

「対処した」と主張する前に、**「新しい bypass 経路が開いていないか」を Redteam 視点でセルフチェック**。

---

## 5. Builder dispatch prompt 骨格

```
Sprint 2 [phase] Builder [round]: [task summary]

## 適用規律
- DoD 厳格: CRITICAL 0 / HIGH 0 / 回帰 0 / carryover 新規 0
- 既存コード改変絶対禁止（Bash 経由も）
- テスト実試行形式必須（AST inspection 禁止）
- 芋づる式 CIA 活用
- Regression ledger 5/5 維持
- Navigator [id] 並走監視中

## 修正対象
[前 round の指摘 N 件の具体修正方針]

## 完遂基準
- [N 件全件解消]
- 新規実試行テスト [M+] 件追加
- pytest tests/ 0 failed (pre-existing 除く)
- 既存コード改変 0（git diff HEAD -- 禁止対象で差分 0）

## Normalization of Deviance 警戒
以下は不可:
- [具体的な anti-pattern 列挙]

見積 [X-Y]h。並列化可能な修正は並列。3h ごと中間報告。
```

---

## 6. Secretary (ソラ) 側の規律

- dispatch 前に premortem 実行（`scripts/premortem.py`）
- monitor_target.txt を新 agent ID に更新
- agent_queue で Redteam/Navigator の次予定を push
- 応答末尾に「いかがしますか」「いいですか」「どうしますか」禁句
  （`memory/feedback_declaration_execution_unified_20260424.md`）
- commit 後は次 carryover に即遷移・停止禁止

---

## 関連ファイル
- `memory/feedback_bug_zero_absolute_20260422.md`
- `memory/feedback_navigator_mandatory_20260422.md`
- `memory/feedback_no_postpone_within_sprint_20260424.md`
- `memory/feedback_declaration_execution_unified_20260424.md`
- `memory/feedback_impact_analysis_dev_20260424.md`
- `data/governance/definition_of_done.md`
- `data/governance/redteam_r7_audit_20260424.md`
- `data/ops/post_incident_review_20260424.md`
- `data/specs/allowlist_hook_design_20260424.md`
