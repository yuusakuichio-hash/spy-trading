#!/bin/bash
# セッション開始時に Sora Lab 常駐 LaunchAgent の稼働確認
# 落ちていたら stdout に警告を流し context に注入・再ロードを促す
# 2026-04-24 制定（「気をつけます」禁止・仕組みで担保）

MARKER_DIR="/tmp/claude_session_markers"
mkdir -p "$MARKER_DIR"
MARKER_FILE="$MARKER_DIR/launchd_check_$(date +%Y%m%d%H)"

# 1 時間ごとに再チェック（頻度過剰回避）
if [ -f "$MARKER_FILE" ]; then
    exit 0
fi
touch "$MARKER_FILE"

REQUIRED_AGENTS=(
    "com.soralab.builder-monitor-5min"
    "com.soralab.status-server"
)

MISSING=()
for agent in "${REQUIRED_AGENTS[@]}"; do
    if ! launchctl list | grep -q "$agent"; then
        MISSING+=("$agent")
    fi
done

if [ ${#MISSING[@]} -eq 0 ]; then
    exit 0
fi

cat <<EOF

=== LaunchAgent 稼働警告（Sora Lab 永続 job）===

以下の LaunchAgent が停止しています。仕組みで担保していた 5 分報告や監視が動いていません。

EOF

for agent in "${MISSING[@]}"; do
    plist="$HOME/Library/LaunchAgents/${agent}.plist"
    if [ -f "$plist" ]; then
        echo "  - $agent (plist 存在 / unload 状態)"
        echo "    復旧: launchctl bootstrap gui/\$(id -u) $plist"
    else
        echo "  - $agent (plist 不在 / 未インストール)"
        echo "    復旧: 直近セッションで plist 作成してから bootstrap"
    fi
done

cat <<EOF

対応指示:
- 最重要タスク進行中なら即 launchctl bootstrap で復旧
- 監視対象が既に完了していれば monitor_target.txt を空にする運用で停止扱い

=== check 完了 ===

EOF

exit 0
