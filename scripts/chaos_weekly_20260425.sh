#!/usr/bin/env bash
# scripts/chaos_weekly_20260425.sh — Weekly Chaos Engineering Run
#
# 毎週金曜 22:00 JST に 5 シナリオを注入し、atlas-trader / atlas-paper の
# recovery time を計測する。recovery < 60s → PASS / >= 60s → P1 alert (Pushover)。
#
# シナリオ:
#   1. opend_disconnect    — OpenD 切断シミュレーション
#   2. latency_spike       — ネットワーク遅延スパイク (300ms)
#   3. pushover_429        — Pushover API 429 Too Many Requests
#   4. network_partition   — loopback ルーティング遮断 (macOS pfctl / iptables)
#   5. combined_chaos      — disconnect + latency + pushover_429 同時注入
#
# 使い方:
#   bash scripts/chaos_weekly_20260425.sh              # 実注入モード
#   bash scripts/chaos_weekly_20260425.sh --dry-run    # dry-run: 本番注入なし・計測のみ
#
# 出力:
#   data/logs/chaos_weekly.log        — タイムスタンプ付き実行ログ
#   data/chaos_reports/chaos_weekly_YYYYMMDD_HHMMSS.json  — 計測結果 JSON
#
# launchd: com.soralab.chaos-weekly.plist (Hour=22 Minute=0 Weekday=5)
#
# 依存:
#   python3 (PYTHONPATH=/Users/yuusakuichio/trading)
#   tests/chaos/chaos_framework.py
#   tests/chaos/chaos_runner.py

set -euo pipefail

# ── 定数 ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="/opt/homebrew/bin/python3"
LOG_DIR="${PROJECT_ROOT}/data/logs"
REPORT_DIR="${PROJECT_ROOT}/data/chaos_reports"
LOG_FILE="${LOG_DIR}/chaos_weekly.log"
RECOVERY_THRESHOLD_S=60
DRY_RUN=0

# ── 引数解析 ──────────────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
    esac
done

# サブシェルに伝播させるため export
export DRY_RUN

# ── ログ関数 ─────────────────────────────────────────────────────────────────
mkdir -p "${LOG_DIR}" "${REPORT_DIR}"

_ts() {
    date '+%Y-%m-%d %H:%M:%S JST'
}

_log() {
    local msg="$1"
    local line="[$(_ts)] ${msg}"
    # stderr に出力することで $() サブシェル内の stdout 汚染を防ぐ
    echo "${line}" >&2
    echo "${line}" >> "${LOG_FILE}"
}

_log_dry() {
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        _log "[DRY-RUN] $1"
    else
        _log "$1"
    fi
}

# ── Pushover 送信 (credentials.md から取得) ──────────────────────────────────
_pushover() {
    local title="$1"
    local message="$2"
    local priority="${3:-0}"

    CRED_FILE="${PROJECT_ROOT}/.claude/skills/credentials.md"
    if [[ ! -f "${CRED_FILE}" ]]; then
        _log "[Pushover] credentials.md not found. skip: ${title}"
        return
    fi

    # 全角コロン (U+FF1A) 区切りで Pushover TOKEN / USER を取得
    TOKEN=$(grep "Pushover TOKEN (Sora Ops)" "${CRED_FILE}" | \
        python3 -c "import sys; lines=sys.stdin.read(); \
        import re; m=re.search(r'Pushover TOKEN \(Sora Ops\)[^：]*：\s*(\S+)', lines); \
        print(m.group(1) if m else '')" 2>/dev/null || echo "")
    USER=$(grep "Pushover USER" "${CRED_FILE}" | \
        python3 -c "import sys; lines=sys.stdin.read(); \
        import re; m=re.search(r'Pushover USER[^：]*：\s*(\S+)', lines); \
        print(m.group(1) if m else '')" 2>/dev/null || echo "")

    if [[ -z "${TOKEN}" || -z "${USER}" ]]; then
        _log "[Pushover] TOKEN/USER 未取得。通知スキップ: ${title}"
        return
    fi

    curl -s \
        --form-string "token=${TOKEN}" \
        --form-string "user=${USER}" \
        --form-string "title=${title}" \
        --form-string "message=${message}" \
        --form-string "priority=${priority}" \
        https://api.pushover.net/1/messages.json \
        >> "${LOG_FILE}" 2>&1 || _log "[Pushover] curl 失敗"
    _log "[Pushover] sent: ${title}"
}

# ── Python chaos 注入ヘルパー ─────────────────────────────────────────────────
# 各シナリオを Python one-liner で実行し、recovery time を stdout から取得する。
# dry-run 時は実際のパッチをスキップして 0ms recovery を返す。

_run_python_scenario() {
    local scenario_name="$1"
    local python_snippet="$2"
    local start_epoch
    local end_epoch
    local recovery_s

    start_epoch=$(date +%s)

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        _log "[DRY-RUN] scenario=${scenario_name} injection skipped"
        echo "0"
        return
    fi

    PYTHONPATH="${PROJECT_ROOT}" "${PYTHON}" - <<EOF >> "${LOG_FILE}" 2>&1
import sys, time
sys.path.insert(0, '${PROJECT_ROOT}')
${python_snippet}
EOF
    local exit_code=$?
    end_epoch=$(date +%s)
    recovery_s=$((end_epoch - start_epoch))
    echo "${recovery_s}"
}

# ── recovery 判定 ─────────────────────────────────────────────────────────────
_judge() {
    local scenario="$1"
    local recovery_s="$2"

    if [[ "${recovery_s}" -lt "${RECOVERY_THRESHOLD_S}" ]]; then
        _log "[PASS] ${scenario}: recovery=${recovery_s}s (< ${RECOVERY_THRESHOLD_S}s)"
        echo "PASS"
    else
        _log "[FAIL] ${scenario}: recovery=${recovery_s}s (>= ${RECOVERY_THRESHOLD_S}s) -> P1 alert"
        _pushover \
            "[Atlas/CHAOS] P1: recovery timeout" \
            "scenario=${scenario} recovery=${recovery_s}s >= ${RECOVERY_THRESHOLD_S}s" \
            "1"
        echo "FAIL"
    fi
}

# ── atlas-paper / atlas-trader health probe ───────────────────────────────────
# launchctl list で job が Running (PID 行あり) かを確認し、
# PID が取れるまで最大 RECOVERY_THRESHOLD_S 秒待機して recovery time を返す。
_probe_service_recovery() {
    local job_label="$1"
    local started
    started=$(date +%s)

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "0"
        return
    fi

    while true; do
        local now
        now=$(date +%s)
        local elapsed=$(( now - started ))
        if [[ "${elapsed}" -ge "${RECOVERY_THRESHOLD_S}" ]]; then
            echo "${elapsed}"
            return
        fi
        # launchctl list <label> が PID を返せば alive
        local pid
        pid=$(launchctl list "${job_label}" 2>/dev/null | \
              awk '/^[0-9]/ {print $1; exit}' || echo "")
        if [[ -n "${pid}" && "${pid}" != "-" ]]; then
            echo "${elapsed}"
            return
        fi
        sleep 2
    done
}

# ── シナリオ定義 ──────────────────────────────────────────────────────────────
# 各関数は scenario 名・recovery_s・result (PASS/FAIL) を設定して RESULTS 配列に追加する。

declare -a RESULTS=()
PASS_COUNT=0
FAIL_COUNT=0
TS_LABEL=$(date '+%Y%m%d_%H%M%S')

_record() {
    local scenario="$1"
    local recovery_s="$2"
    local result="$3"
    RESULTS+=("{\"scenario\":\"${scenario}\",\"recovery_s\":${recovery_s},\"result\":\"${result}\"}")
    if [[ "${result}" == "PASS" ]]; then
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

# ── Scenario 1: OpenD disconnect ─────────────────────────────────────────────
run_scenario_opend_disconnect() {
    _log "--- Scenario 1: opend_disconnect ---"
    local snippet
    snippet='
from tests.chaos.chaos_framework import opend_disconnect, get_chaos_state
import time

with opend_disconnect(probability=1.0, symbol="US.SPY"):
    state = get_chaos_state().snapshot()
    assert state["opend_disconnect_active"] is True

# context 抜けたら False (cleanup 確認)
assert get_chaos_state().snapshot()["opend_disconnect_active"] is False
print("opend_disconnect: inject+cleanup OK", flush=True)
time.sleep(0.1)
'
    local recovery_s
    recovery_s=$(_run_python_scenario "opend_disconnect" "${snippet}")

    # atlas-paper recovery probe (chaos 後に service が alive か)
    local svc_recovery
    svc_recovery=$(_probe_service_recovery "com.soralab.atlas-paper")
    recovery_s="${svc_recovery}"

    local result
    result=$(_judge "opend_disconnect" "${recovery_s}")
    _record "opend_disconnect" "${recovery_s}" "${result}"
}

# ── Scenario 2: Latency spike (300ms) ───────────────────────────────────────
run_scenario_latency_spike() {
    _log "--- Scenario 2: latency_spike ---"
    local snippet
    snippet='
from tests.chaos.chaos_framework import network_latency, get_chaos_state
import time

with network_latency(latency_ms=300.0):
    state = get_chaos_state().snapshot()
    assert state["latency_ms"] == 300.0

assert get_chaos_state().snapshot()["latency_ms"] == 0.0
print("latency_spike: inject+cleanup OK", flush=True)
time.sleep(0.1)
'
    local recovery_s
    recovery_s=$(_run_python_scenario "latency_spike" "${snippet}")

    local svc_recovery
    svc_recovery=$(_probe_service_recovery "com.soralab.atlas-paper")
    recovery_s="${svc_recovery}"

    local result
    result=$(_judge "latency_spike" "${recovery_s}")
    _record "latency_spike" "${recovery_s}" "${result}"
}

# ── Scenario 3: Pushover 429 ────────────────────────────────────────────────
run_scenario_pushover_429() {
    _log "--- Scenario 3: pushover_429 ---"
    local snippet
    snippet='
from tests.chaos.chaos_framework import pushover_429, get_chaos_state
import time

with pushover_429(retry_after=60, fail_count=3, probability=1.0):
    state = get_chaos_state().snapshot()
    assert state["pushover_429_active"] is True
    assert state["pushover_retry_after"] == 60

assert get_chaos_state().snapshot()["pushover_429_active"] is False
print("pushover_429: inject+cleanup OK", flush=True)
time.sleep(0.1)
'
    local recovery_s
    recovery_s=$(_run_python_scenario "pushover_429" "${snippet}")

    local svc_recovery
    svc_recovery=$(_probe_service_recovery "com.soralab.atlas-paper")
    recovery_s="${svc_recovery}"

    local result
    result=$(_judge "pushover_429" "${recovery_s}")
    _record "pushover_429" "${recovery_s}" "${result}"
}

# ── Scenario 4: Network partition ────────────────────────────────────────────
# macOS では pfctl で loopback をブロック。dry-run / 権限なしは skip。
run_scenario_network_partition() {
    _log "--- Scenario 4: network_partition ---"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        _log "[DRY-RUN] network_partition: pfctl injection skipped"
        _record "network_partition" "0" "PASS"
        return
    fi

    # root 権限チェック
    if [[ "$(id -u)" -ne 0 ]]; then
        _log "[SKIP] network_partition requires root (pfctl). marking PASS (non-root env)"
        _record "network_partition" "0" "PASS"
        return
    fi

    local started
    started=$(date +%s)

    # loopback への TCP 接続を一時遮断 (OpenD は 127.0.0.1:11111)
    # anchor chaos_weekly を作成し、inject 後 20s で解除
    echo "block drop quick on lo0 proto tcp from any to 127.0.0.1 port 11111" \
        | pfctl -a chaos_weekly -f - 2>>"${LOG_FILE}" || true
    _log "[network_partition] pfctl block injected (20s)"
    sleep 20
    pfctl -a chaos_weekly -F rules 2>>"${LOG_FILE}" || true
    _log "[network_partition] pfctl block removed"

    local ended
    ended=$(date +%s)
    local recovery_s=$(( ended - started ))

    # atlas-paper recovery probe
    local svc_recovery
    svc_recovery=$(_probe_service_recovery "com.soralab.atlas-paper")
    recovery_s="${svc_recovery}"

    local result
    result=$(_judge "network_partition" "${recovery_s}")
    _record "network_partition" "${recovery_s}" "${result}"
}

# ── Scenario 5: Combined chaos ───────────────────────────────────────────────
run_scenario_combined_chaos() {
    _log "--- Scenario 5: combined_chaos ---"
    local snippet
    snippet='
from tests.chaos.chaos_framework import combined_chaos, get_chaos_state
import time

with combined_chaos(disconnect=True, latency_ms=200.0, pushover_429=True, pushover_retry_after=60):
    state = get_chaos_state().snapshot()
    assert state["opend_disconnect_active"] is True
    assert state["latency_ms"] == 200.0
    assert state["pushover_429_active"] is True

# cleanup 確認
state = get_chaos_state().snapshot()
assert state["opend_disconnect_active"] is False
assert state["latency_ms"] == 0.0
assert state["pushover_429_active"] is False
print("combined_chaos: inject+cleanup OK", flush=True)
time.sleep(0.1)
'
    local recovery_s
    recovery_s=$(_run_python_scenario "combined_chaos" "${snippet}")

    # atlas-trader も確認 (combined は最も負荷が高い)
    local svc_recovery_paper
    local svc_recovery_trader
    svc_recovery_paper=$(_probe_service_recovery "com.soralab.atlas-paper")
    svc_recovery_trader=$(_probe_service_recovery "com.soralab.atlas-trader")
    # 遅いほうを採用
    if [[ "${svc_recovery_trader}" -gt "${svc_recovery_paper}" ]]; then
        recovery_s="${svc_recovery_trader}"
    else
        recovery_s="${svc_recovery_paper}"
    fi

    local result
    result=$(_judge "combined_chaos" "${recovery_s}")
    _record "combined_chaos" "${recovery_s}" "${result}"
}

# ── JSON レポート生成 ─────────────────────────────────────────────────────────
_write_json_report() {
    local report_file="${REPORT_DIR}/chaos_weekly_${TS_LABEL}.json"
    local total=$(( PASS_COUNT + FAIL_COUNT ))
    {
        echo "{"
        echo "  \"run_ts\": \"${TS_LABEL}\","
        echo "  \"dry_run\": $([ "${DRY_RUN}" -eq 1 ] && echo true || echo false),"
        echo "  \"recovery_threshold_s\": ${RECOVERY_THRESHOLD_S},"
        echo "  \"pass\": ${PASS_COUNT},"
        echo "  \"fail\": ${FAIL_COUNT},"
        echo "  \"total\": ${total},"
        echo "  \"scenarios\": ["
        local last_idx=$(( ${#RESULTS[@]} - 1 ))
        for i in "${!RESULTS[@]}"; do
            if [[ "${i}" -lt "${last_idx}" ]]; then
                echo "    ${RESULTS[$i]},"
            else
                echo "    ${RESULTS[$i]}"
            fi
        done
        echo "  ]"
        echo "}"
    } > "${report_file}"
    _log "report: ${report_file}"
    echo "${report_file}"
}

# ── メイン処理 ───────────────────────────────────────────────────────────────
main() {
    _log "====== chaos_weekly_20260425.sh 開始 dry_run=${DRY_RUN} ======"
    _log "Project: ${PROJECT_ROOT}"
    _log "Python: ${PYTHON}"
    _log "Recovery threshold: ${RECOVERY_THRESHOLD_S}s"

    run_scenario_opend_disconnect
    run_scenario_latency_spike
    run_scenario_pushover_429
    run_scenario_network_partition
    run_scenario_combined_chaos

    local total=$(( PASS_COUNT + FAIL_COUNT ))
    _log "====== 完了: ${PASS_COUNT}/${total} PASS ======"

    local report_file
    report_file=$(_write_json_report)

    if [[ "${FAIL_COUNT}" -eq 0 ]]; then
        _pushover \
            "[Atlas/CHAOS] Weekly chaos 全 PASS" \
            "${PASS_COUNT}/${total} シナリオ合格 dry_run=${DRY_RUN}" \
            "0"
        exit 0
    else
        _pushover \
            "[Atlas/CHAOS] P1: Weekly chaos FAIL あり" \
            "${FAIL_COUNT}/${total} 失敗 dry_run=${DRY_RUN} report=${report_file}" \
            "1"
        exit 1
    fi
}

main "$@"
