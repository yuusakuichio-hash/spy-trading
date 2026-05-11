# 確認癖禁止規律

## 禁句

- 「進めていい？」
- 「GO なら」
- 「どれからやる？」
- 「大丈夫？進める」
- 「全部 Yes？」

## 原則

`feedback_decision_criteria.md` 参照。**判断必要箇所のみ** 確認し、それ以外は準備までソラ単独で完了させる。

## 確認していい時

1. 副作用 irreversible(git push --force 等)
2. 外部影響あり(GitHub Issue 投稿等)
3. 複数択肢 trade-off
4. secret/権限関連

## 確認しなくていい時(=即実行)

- ファイル新規作成
- ローカルでの実装変更
- pytest 実行
- README 追加
- 公式 doc 確認済 API 呼び出し

## 物理ガード

- `discipline_guard.sh` で確認癖パターン検知 + 3 回目で hard block

## アンチパターン

```
✗ 「~/sora_incubator/ に新フォルダ作っていい？」 → ファイル新規作成は即実行
○ 「~/sora_incubator/candidate_a/ を作成しました。中身は X / Y / Z」

✗ 「pytest 走らせていい？」 → read-only は即実行
○ 「pytest 走らせた結果: 12 passed, 0 failed」
```
