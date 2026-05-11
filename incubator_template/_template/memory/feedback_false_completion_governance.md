# 虚偽完了禁止・証跡 4 点セット規律

## 結論

「完了」「実装完了」「全テスト pass」等の宣言時は、以下 4 点セットの証跡を **同じ応答内に明示** すること。証跡なしの完了宣言は虚偽完了パターンであり、hook が検知して block する。

## 証跡 4 点セット

1. **grep 結果**: 該当機能の実装ファイル・行番号(`file_path:line_number`)
2. **AST/構文検証**: 意図した構造になっているかの形式チェック
3. **pytest stdout**: 全件実行結果(`N passed, M failed` 行を含む)
4. **mutation score** または **coverage 数値**: 単なる pass ではなく品質値

## 物理ガード

- `.claude/hooks/false_claim_detector.sh`: 完了宣言検知 + pytest 証跡なしで警告
- `.claude/hooks/discipline_guard.sh`: 「全合格」「全PASS」「完了宣言」検知 + 証跡パターンなしで violation 記録(3回目で hard block)
- `.claude/hooks/confidence_assertion_guard.sh`: 「X%確実」「完全稼働」検知 + evidence path なしで hard block

## 緊急 bypass(audit log 必要)

- `DISCIPLINE_GUARD_BYPASS=1` `CONFIDENCE_GUARD_BYPASS=1` 等

## 背景

CLAUDE が「動きました」「実装しました」と報告するが実際にはテスト未実行・部分実装・別ファイル書換等の虚偽パターンが繰り返し発生。記憶ではなく hook で物理ブロックする。
