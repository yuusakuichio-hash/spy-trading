#!/bin/bash
# auditor_required_gate.sh — Auditor 監査結果が欠損していたら NO-GO (P0-1)
#
# flow_audit C-01: Auditor (Gemini/o3) の「別 conversation」定義が曖昧で、
# `purple_cell_audit.py:204` の silent skip が継承されるリスクへの対応。
#
# 動作:
#   - Phase 1 以降の重大判断系 task（merge / deploy / kill_switch 解除等）の前に呼ばれる
#   - data/governance/auditor_latest.json の存在 + parse_error != true + verdict 妥当 + 1h 以内
#   - いずれか NG なら exit 2
#
# Bypass: AUDITOR_BYPASS=1（audit log に理由必須）

set -u

# bypass
if [ "${AUDITOR_BYPASS:-}" = "1" ]; then
    # bypass 履歴記録
    AUDIT_LOG="/Users/yuusakuichio/trading/data/governance/audit_bypass_log.jsonl"
    mkdir -p "$(dirname "$AUDIT_LOG")"
    BYPASS_REASON="${AUDITOR_BYPASS_REASON:-未指定}"
    python3 -c "
import json, datetime, os
rec = {
    'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'event': 'auditor_bypass',
    'reason': '$BYPASS_REASON',
    'pid': os.getpid(),
}
with open('$AUDIT_LOG', 'a') as f:
    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
" 2>/dev/null
    exit 0
fi

INPUT=$(cat 2>/dev/null || true)

# tool_name 取得（hook モードのみ）
TOOL_NAME=""
if [ -n "$INPUT" ]; then
    TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || echo "")
fi

# 監査必須対象 tool（Phase 1 以降で運用開始）
REQUIRES_AUDIT=0
case "$TOOL_NAME" in
    Bash)
        # bash command 内に重大キーワード含む場合のみ
        CMD=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command','')[:1000])" 2>/dev/null || echo "")
        case "$CMD" in
            *"git push origin main"*|*"git merge"*|*"launchctl bootstrap"*|*"launchctl kickstart"*|*"systemctl start"*|*"chronos_bot.py"*|*"spy_bot.py"*)
                REQUIRES_AUDIT=1
                ;;
        esac
        ;;
esac

# 運用開始前は require フラグでも skip（明示有効化まで）
if [ "${AUDITOR_GATE_ENABLED:-}" != "1" ]; then
    exit 0
fi

if [ "$REQUIRES_AUDIT" = "0" ]; then
    exit 0
fi

# Auditor 結果ファイル確認
AUDITOR_FILE="/Users/yuusakuichio/trading/data/governance/auditor_latest.json"

if [ ! -f "$AUDITOR_FILE" ]; then
    cat >&2 <<EOF
[AUDITOR_REQUIRED_GATE] Auditor 結果欠損で NO-GO:
  required for: $TOOL_NAME
  missing: $AUDITOR_FILE

重大判断には Auditor (Gemini/o3 異機種) の独立監査結果が必須です。
Auditor を起動してから再実行してください:
  python3 scripts/auditor_run.py --target <task>

緊急 bypass: AUDITOR_BYPASS=1 AUDITOR_BYPASS_REASON="<理由>"
（bypass 履歴は data/governance/audit_bypass_log.jsonl に記録されます）
EOF
    exit 2
fi

# JSON 健全性 + verdict + 鮮度 (1h) 確認
python3 <<EOF
import json, sys, time
from pathlib import Path
p = Path('$AUDITOR_FILE')
try:
    d = json.loads(p.read_text())
except Exception as e:
    print(f'[AUDITOR_REQUIRED_GATE] parse error: {e}', file=sys.stderr)
    sys.exit(2)

if d.get('parse_error') is True:
    print(f'[AUDITOR_REQUIRED_GATE] parse_error=true (silent skip 検出)', file=sys.stderr)
    sys.exit(2)

verdict = d.get('verdict', d.get('overall_verdict', ''))
if verdict not in ('GO', 'CONDITIONAL-GO', 'CONDITIONAL_GO', 'NO-GO', 'NO_GO'):
    print(f'[AUDITOR_REQUIRED_GATE] invalid verdict: {verdict!r}', file=sys.stderr)
    sys.exit(2)

if 'NO' in verdict.upper():
    print(f'[AUDITOR_REQUIRED_GATE] Auditor verdict NO-GO', file=sys.stderr)
    sys.exit(2)

# 鮮度: ファイル mtime 1h 以内
mtime = p.stat().st_mtime
age_sec = time.time() - mtime
if age_sec > 3600:
    age_h = age_sec / 3600
    print(f'[AUDITOR_REQUIRED_GATE] auditor result stale ({age_h:.1f}h old)', file=sys.stderr)
    sys.exit(2)

print(f'[AUDITOR_REQUIRED_GATE] OK verdict={verdict} age={int(age_sec)}s', file=sys.stderr)
sys.exit(0)
EOF
exit $?
