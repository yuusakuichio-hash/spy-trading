#!/bin/bash
# install_atlas_paper_daemon.sh — CRIT-R6-4: plist インストール + launchctl 起動確認
#
# 修正内容: 旧実装は plist を書いただけで launchctl load を実行しておらず
#          "Could not find service" が発生していた。
# 本 script は:
#   1. plist を ~/Library/LaunchAgents/ にコピー
#   2. launchctl bootstrap gui/$UID <plist>
#   3. 30 秒待機
#   4. launchctl list com.soralab.atlas-paper で起動確認
#   5. stderr.log の crash 監視
#   6. python3 -m atlas_v3.main --verify-daemon-alive で最終確認
#
# 使用方法:
#   bash scripts/install_atlas_paper_daemon.sh
#   bash scripts/install_atlas_paper_daemon.sh --uninstall  # アンインストール
#
# 前提:
#   - ~/Library/LaunchAgents/com.soralab.atlas-paper.plist が存在すること
#     (trading ディレクトリ内の plist からコピー、または直接配置)
#   - python3 -m atlas_v3.main が実行可能なこと

set -euo pipefail

LABEL="com.soralab.atlas-paper"
PLIST_SRC="/Users/yuusakuichio/trading/scripts/com.soralab.atlas-paper.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
WORKING_DIR="/Users/yuusakuichio/trading"
STDERR_LOG="${WORKING_DIR}/data/state_v3/atlas-paper-stderr.log"
WAIT_SECS=30

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S')] $*"
}

error() {
    echo "[ERROR] $*" >&2
    exit 1
}

# --uninstall オプション
if [[ "${1:-}" == "--uninstall" ]]; then
    log "Uninstalling ${LABEL}..."
    launchctl bootout "gui/${UID}/${LABEL}" 2>/dev/null || true
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
    log "Uninstalled. plist file not removed: ${PLIST_DEST}"
    exit 0
fi

log "=== Installing Atlas Paper Daemon (CRIT-R6-4 fix) ==="

# Step 1: plist の存在確認
if [ ! -f "${PLIST_DEST}" ]; then
    if [ -f "${PLIST_SRC}" ]; then
        log "Copying plist from ${PLIST_SRC} to ${PLIST_DEST}"
        cp "${PLIST_SRC}" "${PLIST_DEST}"
        chmod 644 "${PLIST_DEST}"
    else
        error "plist not found at ${PLIST_DEST} or ${PLIST_SRC}. " \
              "Create ~/Library/LaunchAgents/${LABEL}.plist first."
    fi
else
    log "plist already exists: ${PLIST_DEST}"
fi

# Step 2: 既存 daemon を停止してから起動（べき等）
log "Attempting to bootout existing daemon (idempotent)..."
launchctl bootout "gui/${UID}/${LABEL}" 2>/dev/null && log "  Booted out existing daemon." || true
launchctl unload "${PLIST_DEST}" 2>/dev/null || true

# Step 3: launchctl bootstrap で起動
log "Bootstrapping ${LABEL} via launchctl..."
if launchctl bootstrap "gui/${UID}" "${PLIST_DEST}"; then
    log "  bootstrap succeeded."
else
    # bootstrap が失敗した場合は旧 load で試みる
    log "  bootstrap failed, trying legacy launchctl load..."
    launchctl load "${PLIST_DEST}" || error "launchctl load also failed."
fi

# Step 4: 起動待機
log "Waiting ${WAIT_SECS}s for daemon to start..."
sleep "${WAIT_SECS}"

# Step 5: launchctl list で起動確認
log "Checking launchctl list ${LABEL}..."
if launchctl list "${LABEL}" 2>&1 | grep -q "PID\|pid"; then
    log "  PASS: ${LABEL} is running (PID found)."
else
    LAUNCHCTL_OUT=$(launchctl list "${LABEL}" 2>&1)
    log "  WARNING: PID not found in launchctl output:"
    log "  ${LAUNCHCTL_OUT}"
fi

# Step 6: stderr.log の crash 確認（5 行 確認）
if [ -f "${STDERR_LOG}" ]; then
    RECENT_ERRORS=$(tail -5 "${STDERR_LOG}" 2>/dev/null || true)
    if echo "${RECENT_ERRORS}" | grep -qi "error\|exception\|crash\|traceback"; then
        log "  WARNING: Recent errors in stderr.log:"
        echo "${RECENT_ERRORS}" | head -10
    else
        log "  PASS: No recent errors in stderr.log."
    fi
else
    log "  INFO: stderr.log not yet created (daemon may still be starting)."
fi

# Step 7: python3 --verify-daemon-alive で最終確認
log "Running --verify-daemon-alive check..."
PYTHON3="/opt/homebrew/bin/python3"
if [ ! -f "${PYTHON3}" ]; then
    PYTHON3=$(which python3 2>/dev/null || echo "python3")
fi

if cd "${WORKING_DIR}" && "${PYTHON3}" -m atlas_v3.main --verify-daemon-alive --skip-preflight; then
    log "  PASS: --verify-daemon-alive succeeded."
else
    log "  WARNING: --verify-daemon-alive returned non-zero. Check daemon status manually:"
    log "    launchctl list ${LABEL}"
    log "    tail -50 ${STDERR_LOG}"
fi

log "=== Install complete. ==="
log "  Start: launchctl start ${LABEL}"
log "  Stop:  launchctl stop ${LABEL}"
log "  Log:   tail -f ${STDERR_LOG}"
