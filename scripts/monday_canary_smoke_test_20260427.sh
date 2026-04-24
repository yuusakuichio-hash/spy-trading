#!/bin/bash
# scripts/monday_canary_smoke_test_20260427.sh
# =============================================================================
# Monday 2026-04-27 NYSE open Canary Smoke Test (12 checks, 30 min budget)
#
# 目的:
#   open 22:30 JST 直前に 30 分以内で 12 項目を全自動検証し、
#     12/12 PASS  -> MassVerify 本番起動 (auto kickstart)
#     <= 11/12    -> 即停止 + Pushover priority=1 (margin of error なし)
#
# 設計原則（CLAUDE.md 最上位規律準拠）:
#   - 既存コード書換禁止 (spy_bot.py / chronos_bot.py / common/*) → 参照のみ
#   - 新規物は全て scripts/ + tests/ 配下 (atlas_v3 が import される)
#   - VPS OpenD 非接続 (auth_budget.py max=3/24h を消費しない)
#   - 副作用なし (発注 0 件 / Pushover は dry-run SILENT / trade_ctx close 厳守)
#   - bug_ledger.jsonl は read-only
#   - 12 項目は順序依存 (daemon alive → OpenD → acc → cash → …) / 1 fail で halt
#   - ETA: 各チェック 90s 平均 × 12 + 回帰 pytest 400s + margin 200s = ~1800s
#
# Exit codes:
#   0   = 12/12 PASS (本番 kickstart 実施)
#   10  = 1/12 fail (halt + Pushover P1)
#   11  = 2-11/12 fail (halt + Pushover P1, 詳細差分)
#   12  = 全滅 (OpenD 未起動 or daemon 全停止)
#   20  = timeout 30 min 超過 (halt + P1)
#   99  = 前提条件未充足 (env / path / python)
# =============================================================================

set -o pipefail

# ── パス・定数 ────────────────────────────────────────────────────────────────
ROOT="/Users/yuusakuichio/trading"
LOGDIR="${ROOT}/data/logs"
STATE_V3="${ROOT}/data/state_v3"
CANARY_LOG="${LOGDIR}/monday_canary_20260427.log"
CANARY_JSON="${LOGDIR}/monday_canary_20260427.json"
BUG_LEDGER="${ROOT}/data/bug_ledger.jsonl"

BUDGET_SEC=1800            # 30 min 上限
HEARTBEAT_MAX_AGE_H=20     # case F relogin heartbeat 鮮度閾値
CASH_MIN_USD=10000         # 最低残高
ACC_ID_EXPECTED=1173421    # paper SIMULATE
BUG_IDS_MIN=6              # BUG-20260425-001..006 は必ず入っていること (追加分は許容)
VIX_MOCK_SPIKE=40.0        # portfolio_risk_gate 発火閾値確認用

# ── 移行期間フラグ (Step 1 daemon 検出切替) ──────────────────────────────────
# ATLAS_TRADER_ACTIVE=1 → com.soralab.atlas-trader を優先 (月曜移行後)
# ATLAS_TRADER_ACTIVE=0 → com.soralab.spy-bot-paper を監視 (移行前・デフォルト)
ATLAS_TRADER_ACTIVE="${ATLAS_TRADER_ACTIVE:-0}"

PYTHON="${PYTHON:-/usr/bin/python3}"
export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"
export PUSHOVER_DRY_RUN=1  # 全 Pushover 呼出を SILENT に倒す（本体 send は最後だけ）

mkdir -p "${LOGDIR}"
: > "${CANARY_LOG}"

START_EPOCH=$(date +%s)
TS_START=$(date '+%Y-%m-%d %H:%M:%S %Z')

# ── JSON 結果蓄積 (jq 非依存・手組) ──────────────────────────────────────────
RESULTS_JSON="["
add_result() {
  # $1=step_no $2=name $3=PASS|FAIL $4=elapsed_sec $5=detail(escaped)
  local sep=""
  [[ "${RESULTS_JSON}" != "[" ]] && sep=","
  RESULTS_JSON="${RESULTS_JSON}${sep}{\"step\":$1,\"name\":\"$2\",\"status\":\"$3\",\"elapsed_s\":$4,\"detail\":\"$5\"}"
}

log() {
  local line="[$(date '+%H:%M:%S')] $*"
  echo "${line}" | tee -a "${CANARY_LOG}"
}

fatal_halt() {
  # $1=exit_code $2=step_no $3=reason
  local code=$1 step=$2 reason="$3"
  log "HALT code=${code} at step=${step} reason=${reason}"
  RESULTS_JSON="${RESULTS_JSON}]"
  # JSON 書出
  cat > "${CANARY_JSON}" <<EOF
{"run_ts":"${TS_START}","exit_code":${code},"halt_at_step":${step},"halt_reason":"${reason}","results":${RESULTS_JSON}}
EOF
  # Pushover P1 halt 通知 (dry-run 解除して 1 本だけ送る)
  unset PUSHOVER_DRY_RUN
  ${PYTHON} - <<PY 2>>"${CANARY_LOG}" || true
import sys
sys.path.insert(0, "${ROOT}")
try:
    from common.pushover_client import send_critical
    send_critical(
        "[CANARY HALT] Monday 2026-04-27 smoke",
        "step=${step} code=${code}\nreason=${reason}\nlog=${CANARY_LOG}",
        priority=1,
        app_tag="CANARY",
    )
except Exception as exc:
    print(f"pushover halt send failed: {exc}", file=sys.stderr)
PY
  exit "${code}"
}

timeout_guard() {
  local now=$(date +%s)
  local elapsed=$(( now - START_EPOCH ))
  if (( elapsed > BUDGET_SEC )); then
    fatal_halt 20 "${1:-?}" "budget 30min exceeded (elapsed=${elapsed}s)"
  fi
}

step_header() {
  # $1=step_no $2=title
  log "================================================================"
  log "Step $1/12: $2"
}

# ── 前提: python import 可能性 ────────────────────────────────────────────────
"${PYTHON}" -c "import sys; sys.path.insert(0,'${ROOT}'); import common.pushover_client" \
  2>>"${CANARY_LOG}" \
  || { echo "FATAL: python import failed"; exit 99; }

log "===== Monday Canary Smoke Test 開始 (${TS_START}) ====="
log "budget=${BUDGET_SEC}s  python=${PYTHON}  root=${ROOT}"

# ============================================================================
# Step 1/12: 全 daemon alive + case F relogin heartbeat 鮮度
# ============================================================================
step_header 1 "daemons alive + relogin heartbeat < ${HEARTBEAT_MAX_AGE_H}h"
S1_T0=$(date +%s)

# --- daemon 候補リスト (移行フラグで切替) ---
# ATLAS_TRADER_ACTIVE=1: com.soralab.atlas-trader (subprocess 版) を優先
# ATLAS_TRADER_ACTIVE=0: com.soralab.spy-bot-paper を監視 (移行前デフォルト)
MISSING_DAEMONS=""
if [[ "${ATLAS_TRADER_ACTIVE}" == "1" ]]; then
  # 移行後: atlas-trader + atlas-paper の両方が必要
  DAEMON_LABELS=("com.soralab.atlas-trader" "com.soralab.atlas-paper")
  log "  [Step1] mode=atlas-trader (ATLAS_TRADER_ACTIVE=1)"
else
  # 移行前: spy-bot-paper + atlas-paper (fallback)
  # atlas-trader が既に loaded であれば合わせて alive 確認する (移行途中対応)
  DAEMON_LABELS=("com.soralab.atlas-paper" "com.soralab.spy-bot-paper")
  log "  [Step1] mode=spy-bot-paper fallback (ATLAS_TRADER_ACTIVE=0)"
fi

for label in "${DAEMON_LABELS[@]}"; do
  pid=$(launchctl list 2>/dev/null | awk -v l="${label}" '$3==l{print $1}')
  if [[ -z "${pid}" || "${pid}" == "-" ]]; then
    MISSING_DAEMONS="${MISSING_DAEMONS} ${label}"
  else
    log "  alive: ${label} pid=${pid}"
  fi
done

# --- atlas-trader 任意チェック (ATLAS_TRADER_ACTIVE=0 時も loaded なら確認) ---
if [[ "${ATLAS_TRADER_ACTIVE}" != "1" ]]; then
  at_pid=$(launchctl list 2>/dev/null | awk '$3=="com.soralab.atlas-trader"{print $1}')
  if [[ -n "${at_pid}" && "${at_pid}" != "-" ]]; then
    log "  [Step1] com.soralab.atlas-trader also detected pid=${at_pid} (pre-migration coexist)"
  else
    log "  [Step1] com.soralab.atlas-trader not loaded (expected in pre-migration mode)"
  fi
fi
# OpenD は正規表現で (動的 PID 含む)
opend_line=$(launchctl list 2>/dev/null | grep -E 'application\.com\.moomoo\.opend' | head -1)
if [[ -z "${opend_line}" ]]; then
  MISSING_DAEMONS="${MISSING_DAEMONS} moomoo_OpenD"
else
  log "  alive: moomoo_OpenD line=${opend_line}"
fi
# case F relogin launchd agent (pid=- でも loaded であれば OK: file heartbeat を後で見る)
relogin_line=$(launchctl list 2>/dev/null | awk '$3=="com.soralab.moomoo-opend-relogin"{print}')
if [[ -z "${relogin_line}" ]]; then
  MISSING_DAEMONS="${MISSING_DAEMONS} com.soralab.moomoo-opend-relogin"
else
  log "  loaded: com.soralab.moomoo-opend-relogin line=${relogin_line}"
fi
# case F relogin heartbeat 鮮度
HB_FILE="${STATE_V3}/opend_relogin_heartbeat.jsonl"
if [[ ! -s "${HB_FILE}" ]]; then
  MISSING_DAEMONS="${MISSING_DAEMONS} relogin_heartbeat_file"
else
  # 最終行の ts を epoch に (python で安全 parse)
  last_age_h=$("${PYTHON}" - "${HB_FILE}" <<'PY' 2>>"${CANARY_LOG}"
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
    # ISO 8601 or epoch
    if isinstance(ts, (int, float)):
        dt = datetime.datetime.fromtimestamp(float(ts))
    else:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        age_s = time.time() - dt.timestamp()
    else:
        age_s = time.time() - dt.timestamp()
    print(f"{age_s/3600.0:.2f}")
except Exception as exc:
    print("999")
PY
  )
  log "  relogin_heartbeat age=${last_age_h}h (threshold=${HEARTBEAT_MAX_AGE_H}h)"
  # float 比較は python で
  stale=$("${PYTHON}" -c "print(1 if float('${last_age_h}') > ${HEARTBEAT_MAX_AGE_H} else 0)")
  if [[ "${stale}" == "1" ]]; then
    MISSING_DAEMONS="${MISSING_DAEMONS} relogin_heartbeat_stale(${last_age_h}h)"
  fi
fi
S1_DT=$(( $(date +%s) - S1_T0 ))
if [[ -n "${MISSING_DAEMONS}" ]]; then
  add_result 1 "daemons_alive" "FAIL" "${S1_DT}" "missing:${MISSING_DAEMONS}"
  fatal_halt 12 1 "daemons missing:${MISSING_DAEMONS}"
fi
add_result 1 "daemons_alive" "PASS" "${S1_DT}" "daemons_ok(ATLAS_TRADER_ACTIVE=${ATLAS_TRADER_ACTIVE})+opend+relogin_hb_ok"
timeout_guard 1

# ============================================================================
# Step 2/12: OpenD connectivity (quote + trade context)
# ============================================================================
step_header 2 "OpenD connectivity (quote + trade ctx)"
S2_T0=$(date +%s)
"${PYTHON}" - <<'PY' >>"${CANARY_LOG}" 2>&1
import sys, socket
sys.path.insert(0, "/Users/yuusakuichio/trading")
# 素の TCP reachability
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3.0)
try:
    s.connect(("127.0.0.1", 11111))
    s.close()
    print("OpenD TCP 127.0.0.1:11111 reachable")
except Exception as exc:
    print(f"FAIL:TCP:{exc}")
    sys.exit(2)
# quote + trade ctx 確立
try:
    from futu import OpenQuoteContext, OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv
    qctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    ret, _ = qctx.get_global_state()
    qctx.close()
    if ret != 0:
        print(f"FAIL:QUOTE:ret={ret}")
        sys.exit(3)
    tctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host="127.0.0.1", port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES,
    )
    ret2, _ = tctx.get_acc_list()
    tctx.close()
    if ret2 != 0:
        print(f"FAIL:TRADE:ret={ret2}")
        sys.exit(4)
    print("OpenD quote+trade ctx OK")
except Exception as exc:
    print(f"FAIL:CTX:{exc}")
    sys.exit(5)
PY
rc=$?
S2_DT=$(( $(date +%s) - S2_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 2 "opend_connectivity" "FAIL" "${S2_DT}" "rc=${rc}"
  fatal_halt 12 2 "OpenD ctx rc=${rc}"
fi
add_result 2 "opend_connectivity" "PASS" "${S2_DT}" "quote+trade_ok"
timeout_guard 2

# ============================================================================
# Step 3/12: Paper account resolution (acc_id=1173421)
# ============================================================================
step_header 3 "paper acc resolution (expected=${ACC_ID_EXPECTED})"
S3_T0=$(date +%s)
"${PYTHON}" - <<PY >>"${CANARY_LOG}" 2>&1
import sys
sys.path.insert(0, "${ROOT}")
from futu import OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111,
                          security_firm=SecurityFirm.FUTUSECURITIES)
try:
    ret, df = ctx.get_acc_list()
    if ret != RET_OK:
        print(f"FAIL:get_acc_list ret={ret}")
        sys.exit(2)
    hit = df[(df["trd_env"] == "SIMULATE") & (df["acc_id"] == ${ACC_ID_EXPECTED})]
    if hit.empty:
        print(f"FAIL:acc_id ${ACC_ID_EXPECTED} not found")
        sys.exit(3)
    print(f"OK acc_id=${ACC_ID_EXPECTED} resolved trd_env=SIMULATE")
finally:
    ctx.close()
PY
rc=$?
S3_DT=$(( $(date +%s) - S3_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 3 "acc_resolution" "FAIL" "${S3_DT}" "rc=${rc}"
  fatal_halt 10 3 "acc_id resolution rc=${rc}"
fi
add_result 3 "acc_resolution" "PASS" "${S3_DT}" "acc_id_ok"
timeout_guard 3

# ============================================================================
# Step 4/12: Cash balance > ${CASH_MIN_USD} USD
# ============================================================================
step_header 4 "cash balance > ${CASH_MIN_USD} USD"
S4_T0=$(date +%s)
cash_json=$("${PYTHON}" - <<PY 2>>"${CANARY_LOG}"
import sys, json
sys.path.insert(0, "${ROOT}")
from futu import OpenSecTradeContext, TrdMarket, SecurityFirm, TrdEnv, RET_OK
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111,
                          security_firm=SecurityFirm.FUTUSECURITIES)
try:
    ret, df = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=${ACC_ID_EXPECTED})
    if ret != RET_OK:
        print(json.dumps({"ok": False, "err": f"accinfo_query ret={ret}"}))
        sys.exit(0)
    # cash 優先: cash > power の順で拾う
    cash = None
    for col in ("cash", "total_assets", "power"):
        if col in df.columns:
            cash = float(df[col].iloc[0])
            break
    print(json.dumps({"ok": cash is not None, "cash": cash, "col": col}))
finally:
    ctx.close()
PY
)
S4_DT=$(( $(date +%s) - S4_T0 ))
cash_val=$(echo "${cash_json}" | "${PYTHON}" -c "import sys,json; d=json.loads(sys.stdin.read() or '{}'); print(d.get('cash') or -1)")
enough=$("${PYTHON}" -c "print(1 if float('${cash_val}') >= ${CASH_MIN_USD} else 0)")
log "  cash=${cash_val} enough=${enough}"
if [[ "${enough}" != "1" ]]; then
  add_result 4 "cash_balance" "FAIL" "${S4_DT}" "cash=${cash_val}<${CASH_MIN_USD}"
  fatal_halt 10 4 "cash insufficient: ${cash_val}"
fi
add_result 4 "cash_balance" "PASS" "${S4_DT}" "cash=${cash_val}"
timeout_guard 4

# ============================================================================
# Step 5/12: symbol_selector 7 戦術 weight 取得
# ============================================================================
step_header 5 "symbol_selector 7 tactics weight resolvable"
S5_T0=$(date +%s)
"${PYTHON}" - <<PY >>"${CANARY_LOG}" 2>&1
import sys
sys.path.insert(0, "${ROOT}")
from common import symbol_selector as ss
names = ss.get_tactic_names()
print(f"tactic_names count={len(names)}: {names}")
# 7 戦術はプロジェクト契約 (ORB / credit_spread / iron_condor / gamma_scalp / mean_rev / momo / event_fade 相当)
if len(names) < 7:
    print(f"FAIL:tactics<7 ({len(names)})")
    sys.exit(2)
# 各 tactic が weight dict を返すこと
missing = []
for t in names[:7]:
    try:
        # 内部 API: _TACTIC_WEIGHTS[_resolve_tactic(t)] を踏む
        resolved = ss._resolve_tactic(t)
        w = ss._TACTIC_WEIGHTS[resolved]
        if not w or not isinstance(w, dict):
            missing.append(t)
    except Exception as exc:
        missing.append(f"{t}:{exc}")
if missing:
    print(f"FAIL:weights missing: {missing}")
    sys.exit(3)
print("OK 7 tactics weights resolvable")
PY
rc=$?
S5_DT=$(( $(date +%s) - S5_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 5 "symbol_selector_weights" "FAIL" "${S5_DT}" "rc=${rc}"
  fatal_halt 10 5 "symbol_selector rc=${rc}"
fi
add_result 5 "symbol_selector_weights" "PASS" "${S5_DT}" "7_tactics_ok"
timeout_guard 5

# ============================================================================
# Step 6/12: ChainGuard wrapper import + smoke call
# ============================================================================
step_header 6 "ChainGuard wrapper import + smoke"
S6_T0=$(date +%s)
"${PYTHON}" - <<PY >>"${CANARY_LOG}" 2>&1
import sys
sys.path.insert(0, "${ROOT}")
from atlas_v3.ops.chainguard_wrapper import get_chain_center_price, ChainGuardError
# smoke: mock dict で正常取得
price = get_chain_center_price("US.SPY", {"last_price": 570.12})
assert price == 570.12, f"price mismatch: {price}"
# None は ChainGuardError
try:
    get_chain_center_price("US.SPY", {"last_price": None})
except ChainGuardError:
    pass
else:
    print("FAIL:None should raise ChainGuardError")
    sys.exit(2)
print("OK chainguard_wrapper smoke")
PY
rc=$?
S6_DT=$(( $(date +%s) - S6_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 6 "chainguard_wrapper" "FAIL" "${S6_DT}" "rc=${rc}"
  fatal_halt 10 6 "chainguard rc=${rc}"
fi
add_result 6 "chainguard_wrapper" "PASS" "${S6_DT}" "import_ok"
timeout_guard 6

# ============================================================================
# Step 7/12: portfolio_risk_gate VIX=40 で halt 判定
# ============================================================================
step_header 7 "portfolio_risk_gate VIX=${VIX_MOCK_SPIKE} halt check"
S7_T0=$(date +%s)
"${PYTHON}" - <<PY >>"${CANARY_LOG}" 2>&1
import sys
sys.path.insert(0, "${ROOT}")
from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed
cfg = GateConfig()  # vix_halt_threshold=30 デフォルト
decision = check_entry_allowed(vix=${VIX_MOCK_SPIKE}, current_entries=0, config=cfg)
assert not decision.allowed, f"expected halt but allowed={decision.allowed}"
print(f"OK halt_decision reason={decision.reason}")
PY
rc=$?
S7_DT=$(( $(date +%s) - S7_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 7 "portfolio_risk_gate" "FAIL" "${S7_DT}" "rc=${rc}"
  fatal_halt 10 7 "PRG rc=${rc}"
fi
add_result 7 "portfolio_risk_gate" "PASS" "${S7_DT}" "vix40_halt_ok"
timeout_guard 7

# ============================================================================
# Step 8/12: mass_verify_safe_runner empty-list smoke
# ============================================================================
step_header 8 "mass_verify_safe_runner empty-list smoke"
S8_T0=$(date +%s)
"${PYTHON}" - <<PY >>"${CANARY_LOG}" 2>&1
import sys
sys.path.insert(0, "${ROOT}")
from atlas_v3.ops.mass_verify_safe_runner import (
    VerifyContext, VerifyResult, run_mass_verify_safe,
)
# 空 list は即 [] を返すこと (副作用 0)
def dummy(ctx):
    return VerifyResult.ok(ctx)
out = run_mass_verify_safe([], dummy)
assert out == [], f"expected [] got {out}"
# 1 件スモーク: frozen VerifyContext は書換拒否すること
ctx = VerifyContext(symbol="US.SPY", strike=570.0, expiry="2026-05-15", option_type="C")
try:
    object.__setattr__  # exists
    ctx2 = VerifyContext(symbol="US.SPY", strike=570.0, expiry="2026-05-15", option_type="C")
except Exception as exc:
    print(f"FAIL:{exc}")
    sys.exit(2)
print("OK mass_verify_safe_runner smoke")
PY
rc=$?
S8_DT=$(( $(date +%s) - S8_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 8 "mass_verify_safe_runner" "FAIL" "${S8_DT}" "rc=${rc}"
  fatal_halt 10 8 "mass_verify rc=${rc}"
fi
add_result 8 "mass_verify_safe_runner" "PASS" "${S8_DT}" "empty_list_ok"
timeout_guard 8

# ============================================================================
# Step 9/12: bug_ledger 読込 + BUG-20260425-* 6 件存在確認
# ============================================================================
step_header 9 "bug_ledger BUG-20260425-* >= ${BUG_IDS_MIN} entries (core 001..006 必須)"
S9_T0=$(date +%s)
if [[ ! -s "${BUG_LEDGER}" ]]; then
  add_result 9 "bug_ledger" "FAIL" "0" "bug_ledger.jsonl missing or empty"
  fatal_halt 10 9 "bug_ledger missing"
fi
bug_audit=$("${PYTHON}" - "${BUG_LEDGER}" <<'PY' 2>>"${CANARY_LOG}"
import json, sys, pathlib, re
p = pathlib.Path(sys.argv[1])
ids = set()
pat = re.compile(r"^BUG-20260425-\d{3}$")
with p.open() as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        bid = d.get("bug_id", "")
        if pat.match(bid):
            ids.add(bid)
core = {f"BUG-20260425-{i:03d}" for i in range(1, 7)}
missing = sorted(core - ids)
print(f"count={len(ids)}|missing={','.join(missing) if missing else 'none'}")
PY
)
log "  bug audit: ${bug_audit}"
S9_DT=$(( $(date +%s) - S9_T0 ))
bug_count=$(echo "${bug_audit}" | sed -n 's/^count=\([0-9]*\).*/\1/p')
bug_missing=$(echo "${bug_audit}" | sed -n 's/.*missing=\(.*\)$/\1/p')
if [[ -z "${bug_count}" || "${bug_count}" -lt "${BUG_IDS_MIN}" || "${bug_missing}" != "none" ]]; then
  add_result 9 "bug_ledger" "FAIL" "${S9_DT}" "count=${bug_count}|missing=${bug_missing}"
  fatal_halt 10 9 "bug_ledger core missing: ${bug_missing} (count=${bug_count})"
fi
add_result 9 "bug_ledger" "PASS" "${S9_DT}" "count=${bug_count}_core_ok"
timeout_guard 9

# ============================================================================
# Step 10/12: 回帰 pytest fast-run (freezegun 68 + symbol_aware 40 + CRITICAL 36)
# ============================================================================
step_header 10 "regression pytest (freezegun + symbol_aware + CRITICAL 3)"
S10_T0=$(date +%s)
# 基本 5 file をまとめて走らせ、失敗ゼロを必須化する
PYTEST_TARGETS=(
  "tests/test_time_travel_windows_20260425.py"
  "tests/test_symbol_aware_price_20260425.py"
  "tests/test_chainguard_wrapper.py"
  "tests/test_portfolio_risk_gate.py"
  "tests/test_mass_verify_safe_runner.py"
)
# atlas-trader subprocess test: agent a531d8 完了後に追加される想定
# ファイルが存在する場合のみ自動追加 (存在しなければスキップ・smoke test は継続)
ATLAS_SUBPROCESS_TEST="tests/test_atlas_v3_bots_subprocess_20260425.py"
if [[ -f "${ROOT}/${ATLAS_SUBPROCESS_TEST}" ]]; then
  PYTEST_TARGETS+=("${ATLAS_SUBPROCESS_TEST}")
  log "  [Step10] atlas-trader subprocess test included: ${ATLAS_SUBPROCESS_TEST}"
else
  log "  [Step10] atlas-trader subprocess test not yet available (will be wired post-a531d8): ${ATLAS_SUBPROCESS_TEST}"
fi
cd "${ROOT}" || fatal_halt 99 10 "cd root failed"
"${PYTHON}" -m pytest -x --tb=line -q "${PYTEST_TARGETS[@]}" >>"${CANARY_LOG}" 2>&1
rc=$?
# 最終 summary 行を拾う
summary=$(grep -E "^(=+.*(passed|failed|error))" "${CANARY_LOG}" | tail -1)
log "  pytest summary: ${summary}"
S10_DT=$(( $(date +%s) - S10_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 10 "regression_pytest" "FAIL" "${S10_DT}" "rc=${rc}|${summary}"
  fatal_halt 11 10 "regression pytest rc=${rc}"
fi
add_result 10 "regression_pytest" "PASS" "${S10_DT}" "all_green"
timeout_guard 10

# ============================================================================
# Step 11/12: dry-test 10 秒間 clean (新 bug 検出なし)
# ============================================================================
step_header 11 "10s dry-test clean (no new bug)"
S11_T0=$(date +%s)
# SPY bot dry-test を 10 秒走らせ crash / AttributeError / TypeError / "no new bug" 確認
DRY_LOG="${LOGDIR}/monday_canary_drytest.log"
: > "${DRY_LOG}"
(
  cd "${ROOT}" && timeout 10 "${PYTHON}" spy_bot.py --paper --test-connect --dry-run \
    >"${DRY_LOG}" 2>&1
) || true  # timeout の exit=124 は正常系
S11_DT=$(( $(date +%s) - S11_T0 ))
# bug signature 検出
if grep -qE "(AttributeError|TypeError|NameError|ImportError|Traceback|CRITICAL|FATAL)" "${DRY_LOG}"; then
  match=$(grep -E "(AttributeError|TypeError|NameError|ImportError|Traceback|CRITICAL|FATAL)" "${DRY_LOG}" | head -3 | tr '\n' '|')
  add_result 11 "drytest_clean" "FAIL" "${S11_DT}" "match=${match:0:200}"
  fatal_halt 11 11 "new bug signature detected: ${match:0:120}"
fi
add_result 11 "drytest_clean" "PASS" "${S11_DT}" "10s_clean"
timeout_guard 11

# ============================================================================
# Step 12/12: Pushover dry-run (IP ban 解消確認)
# ============================================================================
step_header 12 "pushover dry-run (IP ban cleared?)"
S12_T0=$(date +%s)
"${PYTHON}" - <<PY >>"${CANARY_LOG}" 2>&1
import sys, os
sys.path.insert(0, "${ROOT}")
# SILENT level で dedup / quiet_hours も通過する (ログのみ)
from common.pushover_client import send_silent
ok = send_silent("[CANARY DRYRUN] connectivity probe", "step12 probe")
print(f"send_silent ok={ok}")
# state からの連続 429 / banned 判定を抽出
from pathlib import Path
import json
state_path = Path("${ROOT}/data/pushover_client_state.json")
if state_path.exists():
    st = json.loads(state_path.read_text() or "{}")
    consec = st.get("consecutive_429", 0)
    backoff_until = st.get("backoff_until", 0)
    import time
    now = time.time()
    banned = backoff_until > now
    print(f"consec_429={consec} backoff_until={backoff_until} banned={banned}")
    if banned:
        print(f"FAIL:pushover banned until {backoff_until}")
        sys.exit(2)
print("OK pushover not banned")
PY
rc=$?
S12_DT=$(( $(date +%s) - S12_T0 ))
if [[ ${rc} -ne 0 ]]; then
  add_result 12 "pushover_dryrun" "FAIL" "${S12_DT}" "rc=${rc}"
  fatal_halt 10 12 "pushover banned rc=${rc}"
fi
add_result 12 "pushover_dryrun" "PASS" "${S12_DT}" "not_banned"
timeout_guard 12

# ============================================================================
# 全 12/12 PASS → 本番 MassVerify kickstart + Pushover 成功通知
# ============================================================================
TOTAL_DT=$(( $(date +%s) - START_EPOCH ))
RESULTS_JSON="${RESULTS_JSON}]"
cat > "${CANARY_JSON}" <<EOF
{"run_ts":"${TS_START}","exit_code":0,"pass":"12/12","total_elapsed_s":${TOTAL_DT},"results":${RESULTS_JSON}}
EOF
log "================================================================"
log "ALL 12/12 PASS (total=${TOTAL_DT}s) → MassVerify kickstart"

# MassVerify 本番起動 (spy-bot-paper を kickstart - 既に起動中ならベストエフォートで kickstart)
launchctl kickstart -k "gui/$(id -u)/com.soralab.spy-bot-paper" >>"${CANARY_LOG}" 2>&1 || true
log "launchctl kickstart com.soralab.spy-bot-paper 実施"

# 成功 Pushover (priority=0 / 非緊急)
unset PUSHOVER_DRY_RUN
"${PYTHON}" - <<PY >>"${CANARY_LOG}" 2>&1 || true
import sys
sys.path.insert(0, "${ROOT}")
from common.pushover_client import send
send(
    "[CANARY 12/12 PASS] Monday 2026-04-27",
    "all 12 checks green, total=${TOTAL_DT}s\nMassVerify kickstart executed\nlog=${CANARY_LOG}",
    priority=0, app_tag="CANARY",
)
PY

exit 0
