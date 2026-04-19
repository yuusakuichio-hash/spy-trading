# Token Revoke / Git History Sanitization 手順書 (C6-B1)

## 緊急度
HIGH — git commit 76f424c で hub_agent トークン平文がリポジトリ履歴に記録済み

## 対象トークン
- `hub_KQNxSivBDhvrAroORy0AjiPIH3zdisqS` (VPS webhook Bearer token)
- 記録場所: `data/token_rotation_20260419.md` (現在は REDACTED 済み)

## ステップ1: VPS側でトークンを即時無効化 (最優先)

```bash
# 新トークンを生成
NEW_TOKEN=$(ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no root@198.13.37.17 \
  "python3 -c 'import secrets; print(secrets.token_hex(32))'")

# webhook_server.py のトークンを更新
ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no root@198.13.37.17 \
  "grep -n 'BEARER\|bearer\|hub_K' /root/spxbot/webhook_server.py"

# 更新後にサービス再起動
ssh -i ~/.ssh/deploy_key -o StrictHostKeyChecking=no root@198.13.37.17 \
  "systemctl restart webhook_server && systemctl status webhook_server | head -5"
```

## ステップ2: git-filter-repo で履歴から文字列を消去

### 前提: git-filter-repo のインストール
```bash
pip3 install git-filter-repo
```

### 実行 (リポジトリルートで)
```bash
# バックアップを先に作成 (必須)
cp -r . /tmp/trading_repo_backup_$(date +%Y%m%d_%H%M%S)

# 対象文字列を履歴から消去
git filter-repo \
  --replace-text <(echo "hub_KQNxSivBDhvrAroORy0AjiPIH3zdisqS==>REDACTED_HUB_TOKEN") \
  --force

# 確認: 漏洩文字列が消えているか
git log --all -p | grep -c "hub_KQNxSivBDhvrAroORy0AjiPIH3zdisqS"
# → 0 が正解
```

### BFG Repo-Cleaner を使う場合 (代替)
```bash
# BFG をダウンロード
curl -Lo /tmp/bfg.jar https://repo1.maven.org/maven2/com/madgag/bfg/1.14.0/bfg-1.14.0.jar

# 消去対象文字列ファイルを作成
echo "hub_KQNxSivBDhvrAroORy0AjiPIH3zdisqS" > /tmp/bad_tokens.txt

# 実行
java -jar /tmp/bfg.jar --replace-text /tmp/bad_tokens.txt .

# 参照を整理
git reflog expire --expire=now --all
git gc --prune=now --aggressive
```

## ステップ3: GitHub への force push
```bash
# プライベートリポジトリであること確認後
git push origin --all --force
git push origin --tags --force
```

## ステップ4: .gitignore の確認
以下が追加済みであること:
```
data/token_*
data/credentials_*
```

## ステップ5: 旧トークンでの疎通がNGになることを確認
```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer hub_KQNxSivBDhvrAroORy0AjiPIH3zdisqS" \
  http://198.13.37.17:9999/command
# → 401 が正解 (旧トークンが無効化されている)
```

## リスク評価
- リポジトリはプライベート設定
- VPSはSSH鍵認証 + ポート9999 はファイアウォールで制限
- 緊急度: 旧トークンが有効な間は中リスク / 無効化後は低リスク

## 完了チェックリスト
- [ ] VPS側トークンを新トークンに更新
- [ ] webhook_server.py の新トークンで疎通確認
- [ ] git-filter-repo / BFG で履歴消去
- [ ] GitHub force push
- [ ] 旧トークンで401確認
- [ ] .claude/skills/credentials.md を新トークンに更新
