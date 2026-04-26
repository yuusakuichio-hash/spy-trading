#!/usr/bin/env bash
# test_auto_resume.sh — auto_resume インフラの dry-run 検証スクリプト
# 実行: bash scripts/test_auto_resume.sh

set -euo pipefail

TRADING_DIR="/Users/yuusakuichio/trading"
PLIST="$HOME/Library/LaunchAgents/com.sora.auto_resume.plist"
RESUME_SCRIPT="${TRADING_DIR}/scripts/auto_resume.sh"
WORK_QUEUE="${TRADING_DIR}/data/work_queue.md"

PASS=0
FAIL=0

check() {
    local label="$1"
    local result="$2"
    if [[ "${result}" == "ok" ]]; then
        echo "  [PASS] ${label}"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] ${label}: ${result}"
        FAIL=$((FAIL+1))
    fi
}

echo "=========================================="
echo "  auto_resume dry-run テスト"
echo "  $(date '+%Y-%m-%d %H:%M:%S JST')"
echo "=========================================="

echo ""
echo "--- 1. 依存ツール確認 ---"
which claude > /dev/null 2>&1 && check "claude CLI" "ok" || check "claude CLI" "missing"
which caffeinate > /dev/null 2>&1 && check "caffeinate" "ok" || check "caffeinate" "missing"
which osascript > /dev/null 2>&1 && check "osascript" "ok" || check "osascript" "missing"
which plutil > /dev/null 2>&1 && check "plutil" "ok" || check "plutil" "missing"
which curl > /dev/null 2>&1 && check "curl" "ok" || check "curl" "missing"

echo ""
echo "--- 2. plist 構文検証 (plutil -lint) ---"
if plutil -lint "${PLIST}" > /dev/null 2>&1; then
    check "plist lint" "ok"
else
    check "plist lint" "$(plutil -lint "${PLIST}" 2>&1)"
fi

echo ""
echo "--- 3. plist 内容確認 ---"
HOUR=$(plutil -extract StartCalendarInterval.Hour raw "${PLIST}" 2>/dev/null || echo "ERR")
MINUTE=$(plutil -extract StartCalendarInterval.Minute raw "${PLIST}" 2>/dev/null || echo "ERR")
RUN_AT_LOAD=$(plutil -extract RunAtLoad raw "${PLIST}" 2>/dev/null || echo "ERR")
check "StartCalendarInterval Hour=2" "$([ "${HOUR}" == "2" ] && echo ok || echo "got=${HOUR}")"
check "StartCalendarInterval Minute=10" "$([ "${MINUTE}" == "10" ] && echo ok || echo "got=${MINUTE}")"
check "RunAtLoad=false" "$([ "${RUN_AT_LOAD}" == "false" ] && echo ok || echo "got=${RUN_AT_LOAD}")"

echo ""
echo "--- 4. スクリプトファイル確認 ---"
[[ -f "${RESUME_SCRIPT}" ]] && check "auto_resume.sh 存在" "ok" || check "auto_resume.sh 存在" "missing"
[[ -x "${RESUME_SCRIPT}" ]] && check "auto_resume.sh 実行権限" "ok" || check "auto_resume.sh 実行権限" "not executable (chmod +x してください)"
[[ -f "${WORK_QUEUE}" ]] && check "work_queue.md 存在" "ok" || check "work_queue.md 存在" "missing"

echo ""
echo "--- 5. work_queue ACTIVE TASKS 確認 ---"
if [[ -f "${WORK_QUEUE}" ]]; then
    TASK_COUNT=$(grep -c "^###" "${WORK_QUEUE}" 2>/dev/null || echo "0")
    check "ACTIVE TASKSエントリ数=${TASK_COUNT}" "ok"
    echo "     タスク一覧:"
    grep "^### " "${WORK_QUEUE}" | while IFS= read -r line; do
        echo "       ${line}"
    done
fi

echo ""
echo "--- 6. auto_resume.sh --dry-run 実行 ---"
if bash "${RESUME_SCRIPT}" --dry-run; then
    check "dry-run 実行" "ok"
else
    check "dry-run 実行" "exit code $?"
fi

echo ""
echo "--- 7. launchctl 登録確認 ---"
# pipefail環境ではlaunchctl list|grepがfalseを返すケースがあるため一時無効化
set +o pipefail
LAUNCHCTL_COUNT=$(launchctl list 2>/dev/null | grep -c "sora.auto_resume" || echo 0)
set -o pipefail
if [[ "${LAUNCHCTL_COUNT}" -ge 1 ]]; then
    check "launchctl 登録済み" "ok"
    set +o pipefail
    launchctl list 2>/dev/null | grep "sora.auto_resume" | while IFS= read -r line; do
        echo "     ${line}"
    done
    set -o pipefail
else
    check "launchctl 登録" "未登録 — 要実行: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sora.auto_resume.plist"
fi

echo ""
echo "--- 8. night_batch との時刻衝突確認 ---"
NIGHT_HOUR=$(plutil -extract StartCalendarInterval.Hour raw "$HOME/Library/LaunchAgents/com.spybot.nightbatch.plist" 2>/dev/null || echo "N/A")
NIGHT_MIN=$(plutil -extract StartCalendarInterval.Minute raw "$HOME/Library/LaunchAgents/com.spybot.nightbatch.plist" 2>/dev/null || echo "N/A")
echo "     night_batch: ${NIGHT_HOUR}:${NIGHT_MIN} JST / auto_resume: 2:10 JST"
check "night_batch衝突なし" "$([ "${NIGHT_HOUR}" != "2" ] && echo ok || echo "CONFLICT: ${NIGHT_HOUR}:${NIGHT_MIN}")"

echo ""
echo "=========================================="
echo "  結果: PASS=${PASS} / FAIL=${FAIL}"
echo "=========================================="

if [[ ${FAIL} -gt 0 ]]; then
    exit 1
fi
exit 0
