#!/bin/bash
# Sora 死活ダッシュボードを別窓 Terminal で開く
# 使い方: bash scripts/open_sora_dashboard.sh
# 効果: 新 Terminal 窓 × watch -n 5 で自動更新表示
# セッション跨ぎ対応: Claude Code と独立動作

set -u

PROJ_ROOT="/Users/yuusakuichio/trading"
SCRIPT="${PROJ_ROOT}/scripts/sora_live_status.sh"

if [ ! -x "${SCRIPT}" ]; then
    chmod +x "${SCRIPT}"
fi

# Terminal.app で新窓開いて while ループ実行（macOS に watch がないため）
osascript <<EOF
tell application "Terminal"
    activate
    do script "cd ${PROJ_ROOT} && while true; do clear; bash scripts/sora_live_status.sh; sleep 5; done"
end tell
EOF

echo "✓ 新 Terminal 窓でダッシュボードを開きました (5 秒ごと自動更新)"
echo "  閉じる時は Cmd+W or Ctrl+C"
