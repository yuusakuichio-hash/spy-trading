# 判断基準: 公式仕様確認済なら即実行

## 結論

「進めていい？」「どうしましょう？」と確認するな。**公式ドキュメントで仕様確認済 + 副作用 reversible** なら即実行。確認は **判断必要箇所のみ**(reversible でない・外部影響あり・複数択肢で trade-off ある場合)。

## 確認していい場面

1. **副作用 irreversible**: `rm -rf` `git push --force` `DROP TABLE` `gh pr merge` 等
2. **外部影響あり**: GitHub Issue 投稿・Slack 通知・本番デプロイ
3. **複数択肢 trade-off**: ライブラリ選定・アーキテクチャ判断
4. **secret/権限関連**: API key 取得・OAuth scope 拡張

## 確認しなくていい場面

1. ファイル新規作成(消せばいい)
2. ローカルでの実装変更(git で戻せる)
3. pytest 実行(read-only)
4. README 等のドキュメント追加
5. 公式 doc 確認済の API 呼び出し

## 物理ガード

- `discipline_guard.sh`: 「進めていい？」「GOなら」「どれからやる？」検知で violation
