# 一般論禁止規律

## 結論

「一般的には」「通常」「だいたい」「多くの場合」等の一般論で済ませない。**具体ファイル名・行番号・出典 URL・実測値** で語ること。

## 違反パターン

- 「テストはだいたい通っている」 → `pytest tests/ -v` 実行 + stdout 提示
- 「設定は適切にされています」 → `cat config.yaml` で内容提示
- 「セキュリティは問題ありません」 → 具体的な脆弱性チェック項目 + 各 verdict
- 「ユーザーの多くは X を望む」 → アンケート結果 / interview ログ / 検索 trend 数値

## 正しい記述

```
✗ 一般的には pytest を実行します
○ pytest tests/test_payment.py::test_refund 実行・stdout: "1 passed in 0.42s"

✗ 設定は正しく入っています
○ config.yaml:12 で api_key=$API_KEY 経由・実環境では .env から load 確認済(grep 結果添付)

✗ ユーザーは認証を望む
○ 検索インタビュー 5 人中 4 人が「ログイン省略したい」回答(interview_20260511.md 参照)
```

## 物理ガード

- `confidence_assertion_guard.sh` が evidence path 不在の断定を検知して block
- `claim_ledger_guard.py` が未検証 URL/価格を検知して block
