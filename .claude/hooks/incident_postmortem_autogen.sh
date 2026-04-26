#!/usr/bin/env bash
# incident_postmortem_autogen.sh — PreToolUse hook
#
# 以下の「インシデント」を 24h 以内に検知した場合、
# data/postmortems/<incident_id>.md が未作成なら block し、
# テンプレートを自動生成する。
#
# インシデントトリガー (いずれか 1 件でも → gate 起動):
#   1. data/kill_switch.flag の mtime が 24h 以内
#   2. pytest red: data/logs/ 以下の pytest ログに FAILED が 24h 以内
#   3. Pushover P1 発報: data/pushover_client_queue.jsonl に priority=1 が 24h 以内
#
# Bypass: POSTMORTEM_BYPASS=1 (bypass log に記録)
# 無効化: POSTMORTEM_GATE_ENABLED != "1" で常時 pass
#
# B16 asyncio 禁止遵守: bash + python3 同期処理のみ。

set -uo pipefail

BASE_DIR="/Users/yuusakuichio/trading"
POSTMORTEM_DIR="${BASE_DIR}/data/postmortems"
LOG_FILE="${BASE_DIR}/data/logs/incident_postmortem_autogen.log"
BYPASS_LOG="${BASE_DIR}/data/governance/audit_bypass_log.jsonl"
WINDOW_SEC=86400

TS="$(date '+%Y-%m-%d %H:%M:%S JST')"

mkdir -p "$POSTMORTEM_DIR" "$(dirname "$LOG_FILE")" "$(dirname "$BYPASS_LOG")"

# ── 無効化フラグ ──────────────────────────────────────────────────────────────
if [[ "${POSTMORTEM_GATE_ENABLED:-0}" != "1" ]]; then
    exit 0
fi

# ── bypass ───────────────────────────────────────────────────────────────────
if [[ "${POSTMORTEM_BYPASS:-0}" == "1" ]]; then
    REASON="${POSTMORTEM_BYPASS_REASON:-未指定}"
    python3 - <<PYEOF 2>/dev/null
import json, datetime, os
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "event": "postmortem_bypass",
    "reason": "${REASON}",
    "pid": os.getpid(),
}
with open("${BYPASS_LOG}", "a") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PYEOF
    echo "[$TS] BYPASS reason=${REASON}" >> "$LOG_FILE"
    exit 0
fi

# ── インシデント検知 (Python) ─────────────────────────────────────────────────
DETECTION_RESULT="$(python3 - <<'PYEOF'
import json, glob, sys, time
from pathlib import Path

base = Path("/Users/yuusakuichio/trading")
window = 86400
now = time.time()
incidents = []

# 1. kill_switch.flag
ks_flag = base / "data" / "kill_switch.flag"
if ks_flag.exists():
    age = now - ks_flag.stat().st_mtime
    if age <= window:
        incidents.append({"type": "kill_switch", "file": str(ks_flag), "age_min": round(age/60,1)})

# 2. pytest red
for pattern in [str(base/"data"/"logs"/"*pytest*.log"), str(base/"data"/"logs"/"*test*.log")]:
    found_pytest_red = False
    for fpath in glob.glob(pattern):
        if found_pytest_red:
            break
        p = Path(fpath)
        try:
            age = now - p.stat().st_mtime
            if age > window:
                continue
            text = p.read_text(errors="replace")
            if "FAILED" in text or "ERROR" in text:
                incidents.append({"type": "pytest_red", "file": str(p), "age_min": round(age/60,1)})
                found_pytest_red = True
        except Exception:
            pass

# 3. Pushover P1
pq = base / "data" / "pushover_client_queue.jsonl"
if pq.exists():
    try:
        for line in pq.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            priority = d.get("priority", d.get("p", 0))
            if str(priority) == "1" or priority == 1:
                ts_val = d.get("ts", d.get("timestamp", ""))
                age = None
                try:
                    if isinstance(ts_val, (int, float)):
                        age = now - float(ts_val)
                    elif isinstance(ts_val, str) and ts_val:
                        import datetime, zoneinfo
                        dt = datetime.datetime.fromisoformat(ts_val)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))
                        age = now - dt.timestamp()
                except Exception:
                    age = None
                if age is not None and age <= window:
                    incidents.append({"type":"pushover_p1","file":str(pq),"age_min":round(age/60,1)})
                    break
                # age が None (ts 不明) または age > window の場合はスキップ
    except Exception:
        pass

print(json.dumps(incidents, ensure_ascii=False))
PYEOF
)"

INCIDENT_COUNT="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(len(d))" "$DETECTION_RESULT" 2>/dev/null || echo 0)"

if [[ "$INCIDENT_COUNT" == "0" ]]; then
    echo "[$TS] PASS no_incidents" >> "$LOG_FILE"
    exit 0
fi

# ── postmortem ファイル確認 ───────────────────────────────────────────────────
POSTMORTEM_CHECK="$(python3 - "$DETECTION_RESULT" <<'PYEOF'
import json, sys, time
from pathlib import Path

incidents = json.loads(sys.argv[1])
postmortem_dir = Path("/Users/yuusakuichio/trading/data/postmortems")
now = time.time()
window = 86400

missing = []
for inc in incidents:
    inc_type = inc["type"]
    found = False
    for pm in postmortem_dir.glob("*.md"):
        if (now - pm.stat().st_mtime) <= window:
            try:
                content = pm.read_text(errors="replace").lower()
                if inc_type.split("_")[0] in content or inc_type in content:
                    found = True
                    break
            except Exception:
                pass
    if not found:
        missing.append(inc)

print(json.dumps(missing, ensure_ascii=False))
PYEOF
)"

MISSING_COUNT="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(len(d))" "$POSTMORTEM_CHECK" 2>/dev/null || echo 0)"

if [[ "$MISSING_COUNT" == "0" ]]; then
    echo "[$TS] PASS all postmortems present incidents=$INCIDENT_COUNT" >> "$LOG_FILE"
    exit 0
fi

# ── テンプレート自動生成 ──────────────────────────────────────────────────────
GENERATED="$(python3 - "$POSTMORTEM_CHECK" <<'PYEOF'
import json, datetime, sys
from pathlib import Path

missing_incidents = json.loads(sys.argv[1])
postmortem_dir = Path("/Users/yuusakuichio/trading/data/postmortems")
postmortem_dir.mkdir(parents=True, exist_ok=True)

now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
now_disp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
generated = []

for inc in missing_incidents:
    inc_type = inc["type"]
    incident_id = f"{inc_type}_{now_str}"
    pm_path = postmortem_dir / f"{incident_id}.md"
    if pm_path.exists():
        generated.append(str(pm_path))
        continue
    age_min = inc.get("age_min", -1)
    age_display = f"{age_min:.1f} 分前" if age_min >= 0 else "不明"
    template = f"""# Postmortem: {incident_id}

**インシデント種別**: {inc_type}
**検知時刻 (JST)**: {now_disp}
**発生元ファイル**: {inc.get("file", "不明")}
**経過時間**: {age_display}
**自動生成**: incident_postmortem_autogen.sh

---

## 1. 概要 (Summary)
<!-- 何が起きたか 2〜3 行で記述 -->

## 2. 影響範囲 (Impact)
- システム:
- ポジション:
- 時間帯:

## 3. 原因分析 (Root Cause)
- 直接原因:
- 根本原因:
- 寄与因子:

## 4. タイムライン (Timeline JST)
| 時刻 | 出来事 |
|------|--------|
|      |        |

## 5. 対応内容 (Response)

## 6. 再発防止策 (Prevention)
- [ ] 対策 1:
- [ ] 対策 2:
- [ ] 対策 3:

## 7. 学習事項 (Lessons Learned)

## 8. 承認 (Sign-off)
- Navigator:
- Auditor:
- Date:
"""
    pm_path.write_text(template, encoding="utf-8")
    generated.append(str(pm_path))

print(json.dumps(generated, ensure_ascii=False))
PYEOF
)"

echo "[$TS] BLOCK missing=$MISSING_COUNT incidents=$INCIDENT_COUNT generated=${GENERATED}" >> "$LOG_FILE"

cat >&2 <<EOF

[INCIDENT_POSTMORTEM_AUTOGEN] 24h 以内のインシデントに対する postmortem が未作成です — block

  検知インシデント: ${DETECTION_RESULT}
  未対応:          ${POSTMORTEM_CHECK}
  自動生成済み:    ${GENERATED}

  テンプレートに記入・保存してから再実行してください。
  ディレクトリ: ${POSTMORTEM_DIR}/

  必須セクション:
    1. 概要  2. 影響範囲  3. 原因分析  4. タイムライン
    5. 対応内容  6. 再発防止策  7. 学習事項  8. 承認

  一時 bypass: export POSTMORTEM_BYPASS=1 POSTMORTEM_BYPASS_REASON="<理由>"
  log: ${LOG_FILE}

EOF

exit 2
