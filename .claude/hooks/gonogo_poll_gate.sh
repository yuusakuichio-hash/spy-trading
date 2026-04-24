#!/usr/bin/env bash
# gonogo_poll_gate.sh — Stop hook
# 4 役 (secretary/navigator/redteam/auditor) の GO sign が
# data/governance/gonogo/<task_id>_<role>.json に揃わないと block する。
#
# sign ファイル形式:
#   { "role": "navigator", "task_id": "T-001", "verdict": "GO", "reason": "...", "ts": "..." }
#
# verdict: GO | NOGO | CONDITIONAL
# 全 4 役が "GO" or "CONDITIONAL" ならパス。
# いずれか 1 役でも "NOGO" または sign ファイル欠損 → exit 2 (block)
#
# Bypass: GONOGO_BYPASS=1 (bypass log に自動記録)
# 無効化: GONOGO_GATE_ENABLED != "1" のとき常時 pass (段階的ロールアウト用)
#
# 呼び出し方: Stop hook として settings.local.json に登録。
# stdin には Stop hook の JSON が渡されるが、このゲートは task_id を
# 環境変数 GONOGO_TASK_ID から受け取る（未設定時は直近 STALE_MIN 分の sign を集計）。

set -uo pipefail

GONOGO_DIR="/Users/yuusakuichio/trading/data/governance/gonogo"
LOG_FILE="/Users/yuusakuichio/trading/data/logs/gonogo_poll_gate.log"
BYPASS_LOG="/Users/yuusakuichio/trading/data/governance/audit_bypass_log.jsonl"
ROLES=("secretary" "navigator" "redteam" "auditor")
STALE_MIN="${GONOGO_STALE_MIN:-60}"

mkdir -p "$GONOGO_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$BYPASS_LOG")"
TS="$(date '+%Y-%m-%d %H:%M:%S JST')"

# ── 無効化フラグ ──────────────────────────────────────────────────────────────
if [[ "${GONOGO_GATE_ENABLED:-0}" != "1" ]]; then
    exit 0
fi

# ── bypass ───────────────────────────────────────────────────────────────────
if [[ "${GONOGO_BYPASS:-0}" == "1" ]]; then
    REASON="${GONOGO_BYPASS_REASON:-未指定}"
    python3 - <<PYEOF 2>/dev/null
import json, datetime, os
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "event": "gonogo_bypass",
    "reason": "${REASON}",
    "pid": os.getpid(),
}
with open("${BYPASS_LOG}", "a") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PYEOF
    echo "[$TS] BYPASS reason=${REASON}" >> "$LOG_FILE"
    exit 0
fi

# ── task_id 解決 ──────────────────────────────────────────────────────────────
# 環境変数優先。未設定なら直近 STALE_MIN 分の sign ファイルを収集。
TASK_ID="${GONOGO_TASK_ID:-}"

if [[ -n "$TASK_ID" ]]; then
    collect_mode="task"
else
    collect_mode="recent"
fi

# ── sign ファイル評価 (Python) ─────────────────────────────────────────────
RESULT="$(python3 - <<PYEOF 2>&1
import json, os, sys, time
from pathlib import Path

gonogo_dir = Path("${GONOGO_DIR}")
roles = ["secretary", "navigator", "redteam", "auditor"]
stale_sec = int("${STALE_MIN}") * 60
collect_mode = "${collect_mode}"
task_id_env = "${TASK_ID}"
now = time.time()

def load_sign(path):
    try:
        d = json.loads(path.read_text())
        return d
    except Exception as e:
        return {"_error": str(e)}

def evaluate_signs(task_id):
    results = {}
    for role in roles:
        pattern = f"{task_id}_{role}.json"
        cands = list(gonogo_dir.glob(pattern))
        if not cands:
            results[role] = {"status": "MISSING"}
            continue
        p = max(cands, key=lambda x: x.stat().st_mtime)
        age = now - p.stat().st_mtime
        if age > stale_sec:
            results[role] = {"status": "STALE", "age_min": round(age/60, 1), "file": str(p)}
            continue
        d = load_sign(p)
        if "_error" in d:
            results[role] = {"status": "PARSE_ERROR", "error": d["_error"]}
            continue
        verdict = d.get("verdict", "").upper()
        if verdict not in ("GO", "NOGO", "CONDITIONAL"):
            results[role] = {"status": "INVALID_VERDICT", "verdict": verdict}
            continue
        results[role] = {"status": "OK", "verdict": verdict, "reason": d.get("reason", ""), "file": str(p)}
    return results

if collect_mode == "task":
    tasks = {task_id_env: evaluate_signs(task_id_env)}
else:
    recent_files = [
        f for f in gonogo_dir.glob("*.json")
        if (now - f.stat().st_mtime) <= stale_sec
    ]
    task_ids = set()
    for f in recent_files:
        stem = f.stem
        for role in roles:
            if stem.endswith(f"_{role}"):
                task_ids.add(stem[: -(len(role) + 1)])
                break
    if not task_ids:
        print("NO_SIGNS_FOUND")
        sys.exit(0)
    tasks = {tid: evaluate_signs(tid) for tid in sorted(task_ids)}

all_pass = True
blocked_tasks = []
for tid, signs in tasks.items():
    nogo_roles = []
    missing_roles = []
    for role, info in signs.items():
        st = info["status"]
        if st == "OK" and info["verdict"] == "NOGO":
            nogo_roles.append(role)
        elif st != "OK":
            missing_roles.append(f"{role}({st})")
    if nogo_roles or missing_roles:
        all_pass = False
        blocked_tasks.append({
            "task_id": tid,
            "nogo": nogo_roles,
            "not_ok": missing_roles,
            "signs": signs,
        })

if all_pass:
    print("PASS")
else:
    import json as _j
    print("BLOCK:" + _j.dumps(blocked_tasks, ensure_ascii=False))
PYEOF
)"

EXIT_CODE=$?

if [[ "$RESULT" == "NO_SIGNS_FOUND" ]]; then
    echo "[$TS] NO_SIGNS no recent sign files, pass" >> "$LOG_FILE"
    exit 0
fi

if [[ "$RESULT" == "PASS" ]]; then
    echo "[$TS] PASS task=${TASK_ID:-recent}" >> "$LOG_FILE"
    exit 0
fi

if [[ $EXIT_CODE -ne 0 ]] || [[ "$RESULT" == BLOCK:* ]]; then
    DETAIL="${RESULT#BLOCK:}"
    echo "[$TS] BLOCK task=${TASK_ID:-recent} detail=${DETAIL}" >> "$LOG_FILE"
    cat >&2 <<EOF

[GONOGO_POLL_GATE] 4 役の GO sign が揃っていません — block

  必要な sign ファイル: ${GONOGO_DIR}/<task_id>_<role>.json
  対象役割: secretary / navigator / redteam / auditor
  有効期限: 直近 ${STALE_MIN} 分以内

  sign ファイル形式 (JSON):
    {
      "role":    "navigator",
      "task_id": "T-001",
      "verdict": "GO",        -- GO | NOGO | CONDITIONAL
      "reason":  "確認済み",
      "ts":      "2026-04-24T10:00:00+09:00"
    }

  ブロック詳細:
  ${DETAIL}

  一時 bypass: export GONOGO_BYPASS=1 GONOGO_BYPASS_REASON="<理由>"
  log: ${LOG_FILE}

EOF
    exit 2
fi

echo "[$TS] PASS (fallthrough)" >> "$LOG_FILE"
exit 0
