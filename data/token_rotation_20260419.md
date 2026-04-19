# Token Rotation 手順書 (C6修正 2026-04-19)

## 背景
- OPS-1: Bearer token が vps-channels.md に平文保存されていた
- .gitignore に .claude/skills/ を追加して今後のgit追跡を防止済み

## 現在のトークン状況
- webhook Bearer token: `hub_KQNxSivBDhvrAroORy0AjiPIH3zdisqS`
- 場所: .claude/skills/vps-channels.md (gitignore済み)
- VPS上のwebook_server.py にハードコードされている可能性あり

## Revoke手順（次回rotation時）
```bash
# 1. VPS上で新トークンを生成
ssh -i ~/.ssh/deploy_key root@198.13.37.17 \
  "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"

# 2. webhook_server.py のトークンを更新
ssh -i ~/.ssh/deploy_key root@198.13.37.17 \
  "grep -n 'BEARER\|bearer\|hub_K' /root/spxbot/webhook_server.py | head -5"

# 3. サービス再起動
ssh -i ~/.ssh/deploy_key root@198.13.37.17 \
  "systemctl restart webhook_server"

# 4. 旧トークンで疎通確認が失敗することを確認
# 5. 新トークンを .claude/skills/credentials.md に保存
# 6. 旧トークンの記録を削除
```

## セキュリティ対策状況
- [x] .gitignore に .claude/skills/ 追加
- [ ] webhook token の VPS側更新 (次回maintenance window で実施)
- [ ] credentials.md → OS keychain または Vault への移行 (Phase 3)

## 優先度
- 現状: VPSはSSH key認証 + プライベートリポジトリ → 緊急度は低
- 次回メンテ時に token rotation を実施
