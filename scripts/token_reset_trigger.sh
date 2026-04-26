#!/usr/bin/env bash
# token_reset_trigger.sh — 02:01 JST 固定時刻トリガー
#
# LaunchAgent: com.sora.token_reset_trigger.plist
#   StartCalendarInterval: Hour=2, Minute=1 (JST)
#   KeepAlive=false
#
# 動作:
#   1. rate_limit 解消チェック
#   2. 解消済み → work_queue.md の ACTIVE TASKS を新セッションで投入
#      （auto_resume.sh --force-check を呼び出す）
#   3. rate_limit 継続中 → Pushover 通知のみ（次の30分ポーリングに委ねる）
#
# auto_resume.sh の --force-check オプション:
#   - Guard 1（セッション活性度判定）を bypass し、work_queue チェックへ直行
#   - Guard 2-3 は通常通り動作

set -euo pipefail

TRADING_DIR="/Users/yuusakuichio/trading"
AUTO_RESUME="${TRADING_DIR}/scripts/auto_resume.sh"
LOG_DIR="${TRADING_DIR}/data/logs"
LOG_FILE="${LOG_DIR}/token_reset_trigger.log"
WORK_QUEUE="${TRADING_DIR}/data/work_queue.md"

# Pushover 認証情報
PUSHOVER_USER="${PUSHOVER_USER:-u2cevk8nktib3sr148rw2hs78ecvux}"
PUSHOVER_TOKEN="${PUSHOVER_TOKEN:-aj9f1fk3ae2o6azif17kjyn698remc}"

# ----------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S JST')] [token_reset_trigger] $*" | tee -a "${LOG_FILE}"
}

pushover() {
    local message="$1"
    local priority="${2:-0}"
    curl -s \
        --form-string "token=${PUSHOVER_TOKEN}" \
        --form-string "user=${PUSHOVER_USER}" \
        --form-string "title=[SYS] token_reset_trigger" \
        --form-string "message=${message}" \
        --form-string "priority=${priority}" \
        https://api.pushover.net/1/messages.json \
        -o /dev/null || true
}

# ----------------------------------------------------------------
# ログディレクトリ確保
# ----------------------------------------------------------------
mkdir -p "${LOG_DIR}"

log "=== 02:01 JST 固定時刻トリガー起動 ==="

# ----------------------------------------------------------------
# 現在の claude プロセス PID を記録
# ----------------------------------------------------------------
CLAUDE_PIDS=$(pgrep -f "claude" 2>/dev/null | tr '\n' ',' | sed 's/,$//' || echo "none")
log "claude_pids: ${CLAUDE_PIDS}"

# ----------------------------------------------------------------
# rate_limit 状態チェック
# ----------------------------------------------------------------
log "rate_limit チェック中..."

LIMIT_CHECK=""
RATE_LIMITED=false

# dry-run モード（--dry-run 引数で呼び出し可能）
if [[ "${1:-}" == "--dry-run" ]]; then
    log "[DRY-RUN] rate_limit チェックをスキップ → rate_limited=false と仮定"
    RATE_LIMITED=false
else
    LIMIT_CHECK=$(claude -p "echo ok" --output-format text 2>&1 | head -c 500 || true)
    if echo "${LIMIT_CHECK}" | grep -qiE "limit|rate|429|overloaded|capacity"; then
        RATE_LIMITED=true
        log "rate_limit 継続中: response_preview=$(echo "${LIMIT_CHECK}" | head -c 100)"
    else
        log "rate_limit 解消確認: response_preview=$(echo "${LIMIT_CHECK}" | head -c 100)"
    fi
fi

# ----------------------------------------------------------------
# 分岐: rate_limit 継続中
# ----------------------------------------------------------------
if [[ "${RATE_LIMITED}" == "true" ]]; then
    log "rate_limit 継続中 → Pushover 通知のみ（次の30分ポーリングに委ねる）"
    pushover "02:01 JST 起動: rate_limit まだ継続中。次の30分ポーリングで再チェックします。" 0
    log "=== token_reset_trigger 終了 (rate_limited) ==="
    exit 0
fi

# ----------------------------------------------------------------
# rate_limit 解消済み → work_queue 確認
# ----------------------------------------------------------------
if [[ ! -f "${WORK_QUEUE}" ]]; then
    log "work_queue.md が存在しない → スキップ"
    log "=== token_reset_trigger 終了 (no_queue_file) ==="
    exit 0
fi

if ! grep -q "^### \[TASK-" "${WORK_QUEUE}" 2>/dev/null; then
    log "ACTIVE TASKS なし → スキップ"
    log "=== token_reset_trigger 終了 (no_active_tasks) ==="
    exit 0
fi

TASK_COUNT=$(grep -c "^### \[TASK-" "${WORK_QUEUE}" 2>/dev/null || echo "0")
log "rate_limit 解消 + ACTIVE TASKS ${TASK_COUNT}件 → auto_resume.sh --force-check 呼び出し"
pushover "02:01 JST: rate_limit 解消。ACTIVE TASKS ${TASK_COUNT}件 → auto_resume 投入中..." 0

# ----------------------------------------------------------------
# auto_resume.sh --force-check を呼び出す
# Guard 1 を bypass して work_queue 投入へ直行
# ----------------------------------------------------------------
if [[ "${1:-}" == "--dry-run" ]]; then
    log "[DRY-RUN] auto_resume.sh --force-check の呼び出しをスキップ"
    log "=== token_reset_trigger 終了 (dry-run) ==="
    exit 0
fi

if [[ ! -x "${AUTO_RESUME}" ]]; then
    log "auto_resume.sh が存在しないか実行権限なし: ${AUTO_RESUME}"
    pushover "02:01 JST: auto_resume.sh が見つかりません。手動確認してください。" 1
    exit 1
fi

exec "${AUTO_RESUME}" --force-check
