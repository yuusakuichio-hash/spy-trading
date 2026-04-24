#!/bin/bash
# scripts/monday_premarket_smoke_20260427.sh
# =============================================================================
# Monday 2026-04-27 Pre-Market Smoke (前段階 / canary より前)
#
# 目的:
#   月曜 09:00 JST から 15 分間隔で 12 時間 継続実行し (最大 48 cycle)、
#   canary smoke (monday_canary_smoke_test_20260427.sh) を安全に起動できる
#   前提条件を整え続ける。
#
#   各 cycle で以下を検証:
#     1. OpenD alive (TCP 127.0.0.1:11111 + launchctl)
#     2. relogin heartbeat 鮮度 < 1h
#     3. daemon 群 alive (atlas-paper / spy-bot-paper / relogin)
#     4. atlas-trader stdout log 正常 (CRITICAL/FATAL/Traceback ゼロ)
#     5. pytest 静的回帰 (新規 engine 10 件 + CRITICAL wrapper 3 本)
#     6. kill_switch.flag 検出 → 即停止 alert
#
# 時間帯: 09:00 JST (2026-04-27) から 12 時間 = 21:00 JST まで
#          canary は 22:30 JST (pre-market 30 分前) に別途起動
#
# 異常時:
#   - Pushover priority=1 + sentinel_watchdog 起動
#   - kill_switch.flag 検出で即停止 (exit 98)
#
# Exit codes:
#   0   = 12h 正常完了 (canary 起動待ち状態)
#   10  = 単一 cycle でチェック失敗 (Pushover P1 送信・次 cycle は継続)
#   20  = 致命的: kill_switch.flag アクティブ検出 (即停止)
#   21  = 致命的: OpenD 連続 3 cycle 停止 (即停止)
#   22  = 致命的: pytest 静的回帰 2 cycle 連続 FAIL (即停止)
#   99  = 前提条件未充足 (python / PYTHONPATH)
# =============================================================================

set -o pipefail

# ── パス・定数 ────────────────────────────────────────────────────────────────
ROOT="/Users/yuusakuichio/trading"
LOGDIR="${ROOT}/data/logs"
STATE_V3="${ROOT}/data/state_v3"
LOG_BASE="${LOGDIR}/monday_premarket_20260427"
CYCLE_LOG="${LOG_BASE}.log"
CYCLE_JSON="${LOG_BASE}.jsonl"       # 各 cycle を JSONL 1 行で追記

# 実行窓: 09:00 JST 起動 → 最大 12h = 43200s
# 15 分間隔 = 900s スリープ
TOTAL_WINDOW_SEC=43200
INTERVAL_SEC=900
RELOGIN_HB_MAX_AGE_H=1              # heartbeat 鮮度閾値 (1h)
OPEND_FAIL_HALT=3                    # OpenD 連続失敗でhalt
PYTEST_FAIL_HALT=2                   # pytest 静的回帰連続失敗でhalt

PYTHON="${PYTHON:-/usr/bin/python3}"
export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"
export PUSHOVER_DRY_RUN=1            # cycle 中は SILENT (halt 時のみ解除)

# 移行フラグ (canary と同じ値を引き継ぐ)
ATLAS_TRADER_ACTIVE="${ATLAS_TRADER_ACTIVE:-0}"

mkdir -p "${LOGDIR}"
: > "${CYCLE_LOG}"

START_EPOCH=$(date +%s)
TS_START=$(date '+%Y-%m-%d %H:%M:%S %Z')

# 連続失敗カウンタ
OPEND_CONSEC_FAIL=0
PYTEST_CONSEC_FAIL=0
CYCLE_NO=0

# ── ユーティリティ ────────────────────────────────────────────────────────────
log() {
  local line="[$(date '+%H:%M:%S')] $*"
  echo "${line}" | tee -a "${CYCLE_LOG}"
}

append_cycle_json() {
  # $1=cycle $2=status $3=elapsed_s $4=details
  printf '{"cycle":%d,"ts":"%s","status":"%s","elapsed_s":%d,"detail":"%s"}\n' \
    "$1" "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$2" "$3" "$4" \
    >> "${CYCLE_JSON}"
}

# Pushover P1 を 1 本 (dry-run 解除して送る)
pushover_p1() {
  local title="$1" msg="$2"
  unset PUSHOVER_DRY_RUN
  "${PYTHON}" - <<PY 2>>"${CYCLE_LOG}" || true
import sys
sys.path.insert(0, "${ROOT}")
try:
    from common.pushover_client import send_critical
    send_critical(
        "${title}",
        "${msg}",
        priority=1,
        app_tag="PREMARKET",
    )
except Exception as exc:
    print(f"pushover p1 send failed: {exc}", file=sys.stderr)
PY
  export PUSHOVER_DRY_RUN=1
}

# sentinel_watchdog を単発起動 (バックグラウンド)
kick_sentinel() {
  local reason="$1"
  log "  [SENTINEL] kick reason=${reason}"
  "${PYTHON}" "${ROOT}/scripts/sentinel_watchdog.py" \
    >>"${CYCLE_LOG}" 2>&1 &
}

# 前提: python import 可能性
"${PYTHON}" -c "import sys; sys.path.insert(0,'${ROOT}'); import common.pushover_client" \
  2>>"${CYCLE_LOG}" \
  || { echo "FATAL: python import failed"; exit 99; }

log "===== Pre-Market Smoke 開始 (${TS_START}) ====="
log "window=${TOTAL_WINDOW_SEC}s interval=${INTERVAL_SEC}s root=${ROOT}"
log "ATLAS_TRADER_ACTIVE=${ATLAS_TRADER_ACTIVE}"

# ============================================================================
# メインループ: 15 分間隔 × 12h
# ============================================================================
while true; do
  CYCLE_NO=$(( CYCLE_NO + 1 ))
  CYCLE_T0=$(date +%s)
  ELAPSED=$(( CYCLE_T0 - START_EPOCH ))

  # 12h 窓終了チェック
  if (( ELAPSED >= TOTAL_WINDOW_SEC )); then
    log "===== 12h 窓完了 (cycle=${CYCLE_NO} elapsed=${ELAPSED}s) → canary 起動待ち ====="
    append_cycle_json "${CYCLE_NO}" "WINDOW_COMPLETE" "${ELAPSED}" "12h_window_expired_normal"
    exit 0
  fi

  log "---- Cycle ${CYCLE_NO} (elapsed=${ELAPSED}s / ${TOTAL_WINDOW_SEC}s) ----"
  CYCLE_FAIL=""

  # ── Check A: kill_switch.flag ─────────────────────────────────────────────
  ks_active=$("${PYTHON}" -c "
import sys; sys.path.insert(0,'${ROOT}')
from common.kill_switch import is_active
print(1 if is_active() else 0)
" 2>>"${CYCLE_LOG}")
  if [[ "${ks_active}" == "1" ]]; then
    log "  [A] CRITICAL: kill_switch.flag active → 即停止"
    pushover_p1 "[PREMARKET HALT] kill_switch active" \
      "cycle=${CYCLE_NO} elapsed=${ELAPSED}s\nkill_switch.flag detected\nlog=${CYCLE_LOG}"
    kick_sentinel "kill_switch_active"
    append_cycle_json "${CYCLE_NO}" "HALT_KILL_SWITCH" "${ELAPSED}" \
      "kill_switch.flag active - immediate stop"
    exit 20
  fi
  log "  [A] kill_switch.flag: clear"

  # ── Check B: OpenD alive ──────────────────────────────────────────────────
  opend_ok=0
  opend_line=$(launchctl list 2>/dev/null | grep -E 'application\.com\.moomoo\.opend' | head -1)
  if [[ -n "${opend_line}" ]]; then
    # TCP reachability
    tcp_ok=$("${PYTHON}" -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3.0)
try:
    s.connect(('127.0.0.1', 11111))
    s.close()
    print(1)
except Exception:
    print(0)
" 2>>"${CYCLE_LOG}")
    if [[ "${tcp_ok}" == "1" ]]; then
      opend_ok=1
      OPEND_CONSEC_FAIL=0
      log "  [B] OpenD: alive (TCP ok)"
    else
      log "  [B] OpenD: launchctl loaded but TCP FAIL"
    fi
  else
    log "  [B] OpenD: NOT in launchctl"
  fi

  if [[ "${opend_ok}" != "1" ]]; then
    OPEND_CONSEC_FAIL=$(( OPEND_CONSEC_FAIL + 1 ))
    log "  [B] OpenD fail (consec=${OPEND_CONSEC_FAIL}/${OPEND_FAIL_HALT})"
    CYCLE_FAIL="${CYCLE_FAIL}|OpenD_FAIL(consec=${OPEND_CONSEC_FAIL})"
    if (( OPEND_CONSEC_FAIL >= OPEND_FAIL_HALT )); then
      pushover_p1 "[PREMARKET HALT] OpenD consecutive ${OPEND_FAIL_HALT} fail" \
        "cycle=${CYCLE_NO} consec=${OPEND_CONSEC_FAIL}\nlog=${CYCLE_LOG}"
      kick_sentinel "opend_consecutive_fail"
      append_cycle_json "${CYCLE_NO}" "HALT_OPEND" "${ELAPSED}" \
        "OpenD_consecutive_fail=${OPEND_CONSEC_FAIL}"
      exit 21
    fi
  fi

  # ── Check C: relogin heartbeat 鮮度 < 1h ─────────────────────────────────
  HB_FILE="${STATE_V3}/opend_relogin_heartbeat.jsonl"
  relogin_ok=0
  if [[ -s "${HB_FILE}" ]]; then
    age_h=$("${PYTHON}" - "${HB_FILE}" <<'PY' 2>>"${CYCLE_LOG}"
import json, sys, time, datetime, pathlib
p = pathlib.Path(sys.argv[1])
last = None
with p.open() as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except Exception:
            pass
if not last or "ts" not in last:
    print("999")
    sys.exit(0)
ts = last["ts"]
try:
    if isinstance(ts, (int, float)):
        dt = datetime.datetime.fromtimestamp(float(ts))
    else:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    age_s = time.time() - dt.timestamp()
    print(f"{age_s/3600.0:.2f}")
except Exception:
    print("999")
PY
    )
    stale=$("${PYTHON}" -c "print(1 if float('${age_h:-999}') > ${RELOGIN_HB_MAX_AGE_H} else 0)")
    if [[ "${stale}" == "0" ]]; then
      relogin_ok=1
      log "  [C] relogin heartbeat: fresh (age=${age_h}h)"
    else
      log "  [C] relogin heartbeat: STALE (age=${age_h}h > ${RELOGIN_HB_MAX_AGE_H}h)"
      CYCLE_FAIL="${CYCLE_FAIL}|relogin_HB_STALE(age=${age_h}h)"
    fi
  else
    log "  [C] relogin heartbeat file missing: ${HB_FILE}"
    CYCLE_FAIL="${CYCLE_FAIL}|relogin_HB_MISSING"
  fi

  # ── Check D: daemon 群 alive ──────────────────────────────────────────────
  MISSING_D=""
  if [[ "${ATLAS_TRADER_ACTIVE}" == "1" ]]; then
    CHECK_LABELS=("com.soralab.atlas-trader" "com.soralab.atlas-paper")
  else
    CHECK_LABELS=("com.soralab.atlas-paper" "com.soralab.spy-bot-paper")
  fi
  CHECK_LABELS+=("com.soralab.moomoo-opend-relogin")

  for label in "${CHECK_LABELS[@]}"; do
    pid=$(launchctl list 2>/dev/null | awk -v l="${label}" '$3==l{print $1}')
    if [[ -z "${pid}" || "${pid}" == "-" ]]; then
      MISSING_D="${MISSING_D} ${label}"
      log "  [D] missing: ${label}"
    else
      log "  [D] alive: ${label} pid=${pid}"
    fi
  done
  if [[ -n "${MISSING_D}" ]]; then
    CYCLE_FAIL="${CYCLE_FAIL}|daemon_missing:${MISSING_D}"
  fi

  # ── Check E: atlas-trader stdout log 正常 ────────────────────────────────
  # atlas-trader または spy-bot-paper の直近ログを検査
  ATLAS_LOG="${LOGDIR}/atlas_trader.log"
  SPYBOT_LOG="${LOGDIR}/spy_bot_paper.log"
  TARGET_LOG=""
  [[ -f "${ATLAS_LOG}" ]] && TARGET_LOG="${ATLAS_LOG}"
  [[ -z "${TARGET_LOG}" && -f "${SPYBOT_LOG}" ]] && TARGET_LOG="${SPYBOT_LOG}"

  log_clean=1
  if [[ -n "${TARGET_LOG}" && -s "${TARGET_LOG}" ]]; then
    # 直近 200 行を検査
    if tail -200 "${TARGET_LOG}" 2>/dev/null \
        | grep -qE "(CRITICAL|FATAL|Traceback \(most recent call|RuntimeError|SystemExit)"; then
      match=$(tail -200 "${TARGET_LOG}" \
              | grep -E "(CRITICAL|FATAL|Traceback \(most recent call|RuntimeError|SystemExit)" \
              | head -3 | tr '\n' '|')
      log "  [E] WARN: log anomaly detected: ${match:0:200}"
      CYCLE_FAIL="${CYCLE_FAIL}|log_anomaly:${match:0:80}"
      log_clean=0
    else
      log "  [E] log: clean (${TARGET_LOG})"
    fi
  else
    log "  [E] log file not found (pre-market / daemon not yet started) — SKIP"
  fi

  # ── Check F: pytest 静的回帰 (新規 engine 10 件 + CRITICAL wrapper 3 本) ─
  PYTEST_STATIC_TARGETS=(
    "tests/test_chainguard_wrapper.py"
    "tests/test_portfolio_risk_gate.py"
    "tests/test_mass_verify_safe_runner.py"
  )
  # 新規 engine テストを加える (存在するもののみ)
  ENGINE_CANDIDATES=(
    "tests/test_premarket_smoke_20260425.py"
    "tests/test_canary_smoke_20260425.py"
  )
  for ec in "${ENGINE_CANDIDATES[@]}"; do
    [[ -f "${ROOT}/${ec}" ]] && PYTEST_STATIC_TARGETS+=("${ec}")
  done

  cd "${ROOT}" || { log "cd root failed"; exit 99; }
  pytest_out=$("${PYTHON}" -m pytest -x --tb=line -q \
    "${PYTEST_STATIC_TARGETS[@]}" 2>&1)
  pytest_rc=$?
  pytest_summary=$(echo "${pytest_out}" | grep -E "^(=+.*(passed|failed|error))" | tail -1)
  log "  [F] pytest: rc=${pytest_rc} summary=${pytest_summary}"

  if [[ ${pytest_rc} -ne 0 ]]; then
    PYTEST_CONSEC_FAIL=$(( PYTEST_CONSEC_FAIL + 1 ))
    log "  [F] pytest FAIL (consec=${PYTEST_CONSEC_FAIL}/${PYTEST_FAIL_HALT})"
    CYCLE_FAIL="${CYCLE_FAIL}|pytest_FAIL(consec=${PYTEST_CONSEC_FAIL})"
    if (( PYTEST_CONSEC_FAIL >= PYTEST_FAIL_HALT )); then
      pushover_p1 "[PREMARKET HALT] pytest static regression consecutive ${PYTEST_FAIL_HALT} fail" \
        "cycle=${CYCLE_NO} consec=${PYTEST_CONSEC_FAIL}\nsummary=${pytest_summary}\nlog=${CYCLE_LOG}"
      kick_sentinel "pytest_consecutive_fail"
      append_cycle_json "${CYCLE_NO}" "HALT_PYTEST" "${ELAPSED}" \
        "pytest_consecutive_fail=${PYTEST_CONSEC_FAIL}"
      exit 22
    fi
  else
    PYTEST_CONSEC_FAIL=0
    log "  [F] pytest: PASS"
  fi

  # ── cycle 結果集計 ────────────────────────────────────────────────────────
  CYCLE_DT=$(( $(date +%s) - CYCLE_T0 ))
  if [[ -n "${CYCLE_FAIL}" ]]; then
    log "  CYCLE ${CYCLE_NO}: WARN (${CYCLE_FAIL:1}) dt=${CYCLE_DT}s"
    append_cycle_json "${CYCLE_NO}" "WARN" "${CYCLE_DT}" "${CYCLE_FAIL:1}"
    # 単一 cycle の warn は P1 を 1 本送って次 cycle へ継続
    pushover_p1 "[PREMARKET WARN] cycle=${CYCLE_NO}" \
      "issues=${CYCLE_FAIL:1}\nelapsed=${ELAPSED}s\nlog=${CYCLE_LOG}"
  else
    log "  CYCLE ${CYCLE_NO}: OK dt=${CYCLE_DT}s"
    append_cycle_json "${CYCLE_NO}" "OK" "${CYCLE_DT}" "all_checks_pass"
  fi

  # ── 次 cycle まで待機 (15 分) ─────────────────────────────────────────────
  NEXT_ELAPSED=$(( $(date +%s) - START_EPOCH + INTERVAL_SEC ))
  if (( NEXT_ELAPSED < TOTAL_WINDOW_SEC )); then
    log "  sleep ${INTERVAL_SEC}s until next cycle..."
    sleep "${INTERVAL_SEC}"
  else
    log "  last cycle reached, exiting normal"
    append_cycle_json "${CYCLE_NO}" "WINDOW_COMPLETE" "${ELAPSED}" "last_cycle"
    exit 0
  fi
done
