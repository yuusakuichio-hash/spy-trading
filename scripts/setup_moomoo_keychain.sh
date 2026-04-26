#!/bin/bash
# setup_moomoo_keychain.sh — 案 F 実装 Step 1/5
#
# moomoo credential を macOS Keychain に登録する初期セットアップスクリプト。
# これは運用開始時に**ゆうさくさんが 1 回だけ実行**する手順。
# 以降は atlas_v3/ops/moomoo_opend_relogin.py が自動で Keychain から取り出す。
#
# 規律:
#   - ps aux / log / plist に password が一切露出しない（redteam CRITICAL 回避）
#   - Keychain は macOS ユーザー認証 (Touch ID) で保護
#   - password は stdin/read -s で入力・履歴に残らない
#
# 使い方:
#   bash /Users/yuusakuichio/trading/scripts/setup_moomoo_keychain.sh
#
# 登録される service/account:
#   service=moomoo_opend account=<moomoo UserID or Email>

set -euo pipefail

SERVICE="moomoo_opend"

echo "=== moomoo Keychain credential setup ==="
echo ""
echo "この手順は 1 回だけ実行してください (password が変わった時のみ再実行)"
echo ""

read -rp "moomoo UserID / Email / Phone: " ACCOUNT
if [ -z "$ACCOUNT" ]; then
  echo "ERROR: account is empty" >&2
  exit 1
fi

# password は stdin 非表示入力
read -rsp "moomoo login password: " PASSWORD
echo ""
if [ -z "$PASSWORD" ]; then
  echo "ERROR: password is empty" >&2
  exit 1
fi

read -rsp "moomoo login password (確認): " PASSWORD_CONFIRM
echo ""
if [ "$PASSWORD" != "$PASSWORD_CONFIRM" ]; then
  echo "ERROR: password mismatch" >&2
  exit 1
fi

# 既存 entry があれば削除（update 用途）
security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" 2>/dev/null || true

# Keychain に登録
# -U: update if exists
# -T "": 他 application の access 禁止（login.keychain の ACL 制限）
security add-generic-password \
  -s "$SERVICE" \
  -a "$ACCOUNT" \
  -w "$PASSWORD" \
  -l "moomoo OpenD auto-relogin credential" \
  -j "sora lab - atlas_v3 preemptive relogin - created $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -U

# account 名を別キーに保存（relogin.py が account を引ける様に）
ACCOUNT_REF_SERVICE="moomoo_opend_account"
security delete-generic-password -s "$ACCOUNT_REF_SERVICE" 2>/dev/null || true
security add-generic-password \
  -s "$ACCOUNT_REF_SERVICE" \
  -a "sora_lab" \
  -w "$ACCOUNT" \
  -l "moomoo OpenD account reference" \
  -U

echo ""
echo "✓ Keychain entry created"
echo ""
echo "確認コマンド:"
echo "  security find-generic-password -s moomoo_opend"
echo "  security find-generic-password -s moomoo_opend_account -w"
echo ""
echo "次のステップ:"
echo "  atlas_v3/ops/moomoo_opend_relogin.py が Keychain から自動取得する"
echo "  launchd plist は ~/Library/LaunchAgents/com.soralab.moomoo-opend-relogin.plist"
