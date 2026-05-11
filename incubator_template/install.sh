#!/usr/bin/env bash
# install.sh — sora_incubator から新規候補プロジェクトを materialize
#
# 使い方:
#   bash incubator_template/install.sh ~/sora_incubator/candidate_a
#   bash incubator_template/install.sh ~/sora_incubator/candidate_a "SNS収益化"
#
# 動作:
#   1. <target_dir> に _template/ をコピー
#   2. CLAUDE.md の {{PROJECT_NAME}} を $2 (or basename of $1) で置換
#   3. .env.example を .env にコピー(secret 未設定状態)
#   4. git init + 初回 commit
#   5. hook 実行権限を付与

set -euo pipefail

if [ $# -lt 1 ]; then
    cat >&2 <<EOF
Usage: bash install.sh <target_dir> [project_name]
Example:
  bash install.sh ~/sora_incubator/candidate_a "SNS収益化"
  bash install.sh ~/sora-monetize
EOF
    exit 1
fi

TARGET="$1"
PROJECT_NAME="${2:-$(basename "$TARGET")}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/_template"

if [ ! -d "$TEMPLATE_DIR" ]; then
    echo "[install.sh] Error: $TEMPLATE_DIR not found" >&2
    exit 1
fi

if [ -e "$TARGET" ]; then
    echo "[install.sh] Error: $TARGET already exists. Refusing to overwrite." >&2
    exit 1
fi

echo "[install.sh] Materializing template to: $TARGET"
echo "[install.sh] Project name: $PROJECT_NAME"

# Step 1: コピー(隠しファイル含む)
mkdir -p "$TARGET"
cp -r "$TEMPLATE_DIR"/. "$TARGET"/

# Step 2: CLAUDE.md placeholder 置換
CLAUDE_MD="$TARGET/CLAUDE.md"
if [ -f "$CLAUDE_MD" ]; then
    # Linux/macOS 両対応の sed in-place
    if sed --version >/dev/null 2>&1; then
        # GNU sed (Linux)
        sed -i "s/{{PROJECT_NAME}}/$PROJECT_NAME/g" "$CLAUDE_MD"
    else
        # BSD sed (macOS)
        sed -i '' "s/{{PROJECT_NAME}}/$PROJECT_NAME/g" "$CLAUDE_MD"
    fi
    echo "[install.sh] CLAUDE.md project name set"
fi

# Step 3: .env.example → .env (secret 未設定)
if [ -f "$TARGET/.env.example" ] && [ ! -f "$TARGET/.env" ]; then
    cp "$TARGET/.env.example" "$TARGET/.env"
    echo "[install.sh] .env created from .env.example (secret は手動で入れる)"
fi

# Step 4: hook 実行権限付与
chmod +x "$TARGET"/.claude/hooks/*.sh 2>/dev/null || true
chmod +x "$TARGET"/.claude/hooks/*.py 2>/dev/null || true
echo "[install.sh] hooks 実行権限付与済"

# Step 5: git init(commit 失敗は許容・後で手動 commit 可能)
if command -v git >/dev/null 2>&1; then
    if [ ! -d "$TARGET/.git" ]; then
        (cd "$TARGET" && git init -q && git add . && \
            git commit -q -m "init: bootstrap from sora_incubator template

Project: $PROJECT_NAME
Source: incubator_template/_template/" 2>/dev/null) \
            && echo "[install.sh] git init + 初回 commit 完了" \
            || echo "[install.sh] git init 完了(commit は手動で・gpg 等の制約があれば設定後 git commit)"
    fi
else
    echo "[install.sh] git 未インストール・skip"
fi

cat <<EOF

[install.sh] 完了

次のステップ:
  cd $TARGET
  # .env に Pushover 認証情報(任意)を設定
  vim .env
  # Claude Code を起動
  claude

このプロジェクトの hook はすべて \$CLAUDE_PROJECT_DIR 基準で動作し、
他のプロジェクト(spy-trading 含む)に影響しません。
EOF
