---
name: Deploy Approval (Two-Man Rule)
about: 本番デプロイ承認フォーム。builder + ops の両者が承認するまでデプロイはブロックされる。
title: "[DEPLOY APPROVAL] commit: <sha>"
labels: deploy-approval
assignees: ""
---

## デプロイ承認チェックリスト

<!-- builder と ops それぞれが確認後、下記チェックボックスにチェックを入れてコメントを追加してください -->

### builder 承認
- [ ] コードレビュー完了（構文・ロジック・副作用の確認）
- [ ] バックテスト結果またはユニットテスト結果を確認済み
- [ ] 承認コメント投稿済み: `BUILDER_APPROVED`

### ops 承認
- [ ] VPS現状確認済み（ディスク・メモリ・サービス稼働状態）
- [ ] ロールバック手順が明確であることを確認
- [ ] 承認コメント投稿済み: `OPS_APPROVED`

---

## デプロイ情報

| 項目 | 値 |
|---|---|
| commit | |
| branch | |
| 変更ファイル | |
| 実施予定時刻 (JST) | |
| 影響サービス | atlas.service |

## 承認手順

1. builder 担当者: 上記 builder セクションを確認し、Issue コメントに `BUILDER_APPROVED` を含むコメントを投稿する
2. ops 担当者: 上記 ops セクションを確認し、Issue コメントに `OPS_APPROVED` を含むコメントを投稿する
3. 両承認が揃うと `scripts/two_man_check.py` が exit 0 を返しデプロイが進行する
4. どちらか一方でも欠ける場合、デプロイは exit 2 でブロックされる
