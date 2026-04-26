#!/usr/bin/env bash
# auto_resume.sh — Claude Code セッション自動再開スクリプト
# 起動: 30分毎ポーリング (LaunchAgent: com.sora.auto_resume, StartInterval=1800)
# 方式A: Claude CLI -p/--print 非対話モード
# 変更: 2026-04-19 固定時刻(2:10 JST)→30分毎ポーリング方式へ改修
# 変更: 2026-04-20 Guard 1 をプロセス存在→セッション活性度判定へ改修（A+Bハイブリッド）
#   - B案: Guard 1 前に rate_limit を先行チェック → rate_limited なら Guard 1 bypass
#   - A案: セッションファイル mtime が 30分以上古い場合はプロセス存在でも介入
# premortem対策: F01依存確認済み / F08バックアップ済み / F10 rollback記録済み

set -euo pipefail

SCRIPT_START=$(date +%s)
TRADING_DIR="/Users/yuusakuichio/trading"
WORK_QUEUE="${TRADING_DIR}/data/work_queue.md"
LOG_DIR="${TRADING_DIR}/data/logs"
LOG_FILE="${LOG_DIR}/auto_resume.log"

# セッションファイルディレクトリ
SESSION_DIR="${HOME}/.claude/projects/-Users-yuusakuichio-trading"

# mtime ベース活性度しきい値（秒）: 30分 = 1800秒
ACTIVE_SESSION_THRESHOLD=1800

# Pushover 認証情報 (Sora Ops トークン)
PUSHOVER_USER="u2cevk8nktib3sr148rw2hs78ecvux"
PUSHOVER_TOKEN="aj9f1fk3ae2o6azif17kjyn698remc"

# dry-run モード判定
DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# --force-check: Guard 1（セッション活性度判定）を bypass する
# token_reset_trigger.sh から呼び出される固定時刻トリガー用オプション
# Guard 2-3 は通常通り動作する
FORCE_CHECK=false
if [[ "${1:-}" == "--force-check" ]]; then
    FORCE_CHECK=true
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S JST')] $*" | tee -a "${LOG_FILE}"
}

# 軽量ログ: ポーリング毎に1行だけ記録（詳細ログと区別）
log_poll() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S JST')] [POLL] $*" >> "${LOG_FILE}"
}

pushover() {
    local message="$1"
    local priority="${2:-0}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[DRY-RUN] Pushover: ${message}"
        return 0
    fi
    curl -s \
        --form-string "token=${PUSHOVER_TOKEN}" \
        --form-string "user=${PUSHOVER_USER}" \
        --form-string "message=${message}" \
        --form-string "priority=${priority}" \
        https://api.pushover.net/1/messages.json \
        -o /dev/null
}

# ----------------------------------------------------------------
# ヘルパー: 最新セッションファイルの更新時刻からの経過秒数を返す
# セッションファイルが存在しない場合は 99999 を返す（=古いと判定）
# ----------------------------------------------------------------
session_stale_seconds() {
    local latest_mtime
    local now
    now=$(date +%s)

    # ~/.claude/projects/ 配下の最新 .jsonl を取得
    local latest_file
    latest_file=$(ls -t "${SESSION_DIR}"/*.jsonl 2>/dev/null | head -1 || true)

    if [[ -z "${latest_file}" ]]; then
        echo 99999
        return
    fi

    # macOS: stat -f %m / Linux: stat -c %Y
    if stat -f %m "${latest_file}" > /dev/null 2>&1; then
        latest_mtime=$(stat -f %m "${latest_file}")
    else
        latest_mtime=$(stat -c %Y "${latest_file}")
    fi

    echo $((now - latest_mtime))
}

# ----------------------------------------------------------------
# ヘルパー: claude CLI プロセスが存在するか
# ----------------------------------------------------------------
claude_process_exists() {
    pgrep -f "claude.*cli" > /dev/null 2>&1 \
        || pgrep -f "claude-cli" > /dev/null 2>&1 \
        || pgrep -f "/opt/homebrew/bin/claude" > /dev/null 2>&1 \
        || pgrep -x "claude" > /dev/null 2>&1
}

# ----------------------------------------------------------------
# Guard B (先行): rate_limit 状態を能動検知
#   "limit/rate/429/overloaded/capacity" 検知時は Guard A をバイパス
#   → auto_resume が強制介入モードへ進む
#
# --force-check フラグが立っている場合もこのチェックをスキップ
# （token_reset_trigger.sh 側で既にチェック済みのため）
# ----------------------------------------------------------------
RATE_LIMITED=false

if [[ "${DRY_RUN}" == "false" ]] && [[ "${FORCE_CHECK}" == "false" ]]; then
    LIMIT_CHECK=$(claude -p "echo ok" --output-format text 2>&1 | head -c 500 || true)
    if echo "${LIMIT_CHECK}" | grep -qiE "limit|rate|429|overloaded|capacity"; then
        RATE_LIMITED=true
        log_poll "state=rate_limited → Guard A bypass, proceeding to active-task check"
    fi
fi

# ----------------------------------------------------------------
# Guard A: セッション活性度判定（A+Bハイブリッド）
#
#  条件 | claudeプロセス | mtime経過 | rate_limited | force_check | 判定
#  1    | あり           | 30分未満  | false        | false       | skip
#  2    | あり           | 30分以上  | false        | false       | 介入
#  3    | あり/なし      | 任意      | true         | false       | 介入
#  4    | なし           | 任意      | false        | false       | 介入
#  5    | 任意           | 任意      | 任意         | true        | Guard 1 bypass → 直接 Guard 2へ
# ----------------------------------------------------------------
if [[ "${FORCE_CHECK}" == "true" ]]; then
    # ケース5: --force-check → Guard 1 を完全 bypass
    log_poll "state=force_check_bypass (Guard 1 bypassed by token_reset_trigger) → proceeding to Guard 2"
elif [[ "${RATE_LIMITED}" == "false" ]]; then
    if claude_process_exists; then
        STALE_SECS=$(session_stale_seconds)
        if [[ "${STALE_SECS}" -lt "${ACTIVE_SESSION_THRESHOLD}" ]]; then
            # ケース1: 最近アクティブ → 介入しない
            log_poll "state=active_session_recent_activity (mtime_age=${STALE_SECS}s < ${ACTIVE_SESSION_THRESHOLD}s) → skip, next_check=30min"
            exit 0
        else
            # ケース2: プロセスあるが 30分以上無更新 → フリーズ検知、介入
            log_poll "state=stale_session_frozen_detected (mtime_age=${STALE_SECS}s >= ${ACTIVE_SESSION_THRESHOLD}s) → proceeding"
        fi
    else
        # ケース4: プロセスなし → 通常の再開処理へ
        log_poll "state=no_claude_process → proceeding"
    fi
else
    # ケース3: rate_limited → Guard A bypass済み（上で log_poll 済み）
    :
fi

# ----------------------------------------------------------------
# Guard 2: work_queue.md 存在確認
# ----------------------------------------------------------------
if [[ ! -f "${WORK_QUEUE}" ]]; then
    log_poll "state=no_queue_file → skip, next_check=30min"
    exit 0
fi

# ACTIVE TASKS が空かチェック（### [TASK- で始まる行）
if ! grep -q "^### \[TASK-" "${WORK_QUEUE}" 2>/dev/null; then
    log_poll "state=no_active_tasks → skip, next_check=30min"
    exit 0
fi

# ----------------------------------------------------------------
# Guard 3: rate limit チェック（Guard B で未検知の場合の念のため確認）
# rate_limited=true の場合はすでにチェック済みのため再チェック不要
# ----------------------------------------------------------------
if [[ "${DRY_RUN}" == "false" ]] && [[ "${RATE_LIMITED}" == "false" ]]; then
    # Guard B と同じチェックを再度実行（Guard B は早期判定のため変数を再利用）
    LIMIT_CHECK2=$(claude -p "echo ok" --output-format text 2>&1 | head -c 500 || true)
    if echo "${LIMIT_CHECK2}" | grep -qiE "limit|rate|429|overloaded|capacity"; then
        log_poll "state=rate_limited (guard3_recheck) → skip, next_check=30min"
        exit 0
    fi
fi

# ----------------------------------------------------------------
# ここまで通過 = limit解除済み & アクティブタスクあり → 投入
# ----------------------------------------------------------------
TASK_COUNT=$(grep -c "^### \[TASK-" "${WORK_QUEUE}" 2>/dev/null || echo "0")
STALE_SECS_FINAL=$(session_stale_seconds)

# ----------------------------------------------------------------
# dry-run 分岐: ここまでで全チェック確認完了
# ----------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    log "=== DRY-RUN: auto_resume.sh 全チェック通過 ==="
    log "[DRY-RUN] Guard B (rate_limit_precheck): RATE_LIMITED=${RATE_LIMITED}"
    log "[DRY-RUN] Guard A (session_activity): session_mtime_age=${STALE_SECS_FINAL}s, threshold=${ACTIVE_SESSION_THRESHOLD}s"
    log "[DRY-RUN] Guard 2 (work_queue exists): PASS"
    log "[DRY-RUN] Guard 2 (active_tasks): PASS — タスク数=${TASK_COUNT}"
    log "[DRY-RUN] Guard 3 (rate_limit recheck): SKIPPED in dry-run"
    log "[DRY-RUN] work_queue.md 先頭20行:"
    head -20 "${WORK_QUEUE}" | while IFS= read -r line; do log "  ${line}"; done
    log "[DRY-RUN] → 実際の投入はスキップ。全分岐通過確認完了"
    exit 0
fi

# ----------------------------------------------------------------
# 投入通知（タスク投入時のみ — 静音ポーリング中は通知しない）
# ----------------------------------------------------------------
RESUME_REASON="normal"
if [[ "${RATE_LIMITED}" == "true" ]]; then
    RESUME_REASON="rate_limit_cleared"
elif [[ "${STALE_SECS_FINAL}" -ge "${ACTIVE_SESSION_THRESHOLD}" ]]; then
    RESUME_REASON="stale_session_recovered"
fi

log "=== auto_resume.sh 起動: ${TASK_COUNT}タスク検出, reason=${RESUME_REASON}, mtime_age=${STALE_SECS_FINAL}s → 投入 ==="
pushover "[SYS] auto_resume dispatched ${TASK_COUNT} tasks (reason=${RESUME_REASON}): Claude CLI 起動中..."

# ----------------------------------------------------------------
# 方式A: Claude CLI -p (非対話モード) 実行
# caffeinate -i でスリープ抑止
# ----------------------------------------------------------------
PROMPT=$(cat "${WORK_QUEUE}")
PROMPT="あなたはSora LabのClaude Codeエージェントです。以下はwork_queue.mdの内容です。ACTIVE TASKSを優先度順に実行してください。完了したタスクはCOMPLETEDに移動し、data/work_queue.mdを更新してください。

${PROMPT}"

SESSION_OUTPUT="${LOG_DIR}/auto_resume_session_$(date +%Y%m%d_%H%M%S).log"

caffeinate -i claude \
    -p "${PROMPT}" \
    --output-format text \
    --dangerously-skip-permissions \
    --add-dir "${TRADING_DIR}" \
    2>&1 | tee -a "${SESSION_OUTPUT}" "${LOG_FILE}"

EXIT_CODE=${PIPESTATUS[0]}
SCRIPT_END=$(date +%s)
DURATION=$((SCRIPT_END - SCRIPT_START))

# ----------------------------------------------------------------
# 完了通知（タスク完了時のみ — 通常ポーリングは静音）
# ----------------------------------------------------------------
if [[ ${EXIT_CODE} -eq 0 ]]; then
    log "Claude セッション正常終了 (duration=${DURATION}s, tasks=${TASK_COUNT}, reason=${RESUME_REASON})"
    pushover "[SYS] auto_resume session ended (duration=${DURATION}s, tasks_done=${TASK_COUNT}, reason=${RESUME_REASON})"
else
    log "Claude セッション異常終了 (exit=${EXIT_CODE}, duration=${DURATION}s, reason=${RESUME_REASON})"
    pushover "[SYS] auto_resume 異常終了 (exit=${EXIT_CODE}, duration=${DURATION}s)" 1
fi

log "=== auto_resume.sh 終了 ==="
exit ${EXIT_CODE}
