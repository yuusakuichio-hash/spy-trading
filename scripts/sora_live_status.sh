#!/bin/bash
# Sora Lab 死活監視ダッシュボード（単発実行）
# 用途: Terminal 別窓で watch -n 5 で回して使う
# セッション跨ぎ対応: Claude Code 本体と独立動作
# 2026-04-24 制定（ゆうさくさん指示: プッシュ通知不要・別窓ダッシュボード方式）

set -u

PROJ_ROOT="/Users/yuusakuichio/trading"
SESS_DIR="/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading"
MONITOR_TARGET_FILE="${PROJ_ROOT}/data/monitor_target.txt"

# 色定義
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; C='\033[0;36m'; B='\033[1m'; N='\033[0m'

now_ts=$(date +%s)
now_jst=$(date '+%Y-%m-%d %H:%M:%S')

echo -e "${B}=== Sora Lab 死活監視 === ${N}${C}${now_jst} JST${N}"
echo ""

# --- 1. Claude Code 本体セッション（ソラ本人）の生存確認 ---
echo -e "${B}[1] ソラ本人（Claude Code メインセッション）${N}"

# 最新の jsonl（mtime で選ぶ）
latest_jsonl=$(ls -t "${SESS_DIR}"/*.jsonl 2>/dev/null | head -1)
if [ -z "${latest_jsonl}" ]; then
    echo -e "  ${R}✗ セッション jsonl なし${N}"
else
    mtime=$(stat -f %m "${latest_jsonl}")
    elapsed=$((now_ts - mtime))

    # 最終 message の timestamp 取得
    last_msg_ts=$(tail -200 "${latest_jsonl}" 2>/dev/null | python3 -c "
import sys, json
last = ''
for line in sys.stdin:
    try:
        d = json.loads(line)
        ts = d.get('timestamp', '')
        if ts > last:
            last = ts
    except Exception:
        pass
print(last)
" 2>/dev/null)

    last_role=$(tail -200 "${latest_jsonl}" 2>/dev/null | python3 -c "
import sys, json
role = 'unknown'
last = ''
for line in sys.stdin:
    try:
        d = json.loads(line)
        ts = d.get('timestamp', '')
        if ts > last:
            last = ts
            role = d.get('type', 'unknown')
    except Exception:
        pass
print(role)
" 2>/dev/null)

    basename_jsonl=$(basename "${latest_jsonl}")
    short_name="${basename_jsonl:0:8}"

    if [ "${elapsed}" -lt 60 ]; then
        status="${G}ALIVE${N}"
    elif [ "${elapsed}" -lt 300 ]; then
        status="${Y}IDLE${N}"
    elif [ "${elapsed}" -lt 900 ]; then
        status="${Y}STALE${N}"
    else
        status="${R}DEAD?${N}"
    fi

    echo -e "  session: ${short_name}..."
    echo -e "  status:  ${status}  最終活動 ${elapsed}s 前"
    echo -e "  last:    role=${last_role}  ts=${last_msg_ts}"
fi
echo ""

# --- 2. 監視対象 Builder agent の進捗 ---
echo -e "${B}[2] 監視対象 Builder${N}"

if [ ! -f "${MONITOR_TARGET_FILE}" ]; then
    echo -e "  ${Y}monitor_target.txt なし${N}"
else
    target=$(cat "${MONITOR_TARGET_FILE}" | head -1 | tr -d '[:space:]')
    if [ -z "${target}" ]; then
        echo -e "  ${Y}target 未設定（Builder 稼働なし）${N}"
    else
        agent_jsonl=$(find "${SESS_DIR}" -name "agent-${target}.jsonl" 2>/dev/null | head -1)
        if [ -z "${agent_jsonl}" ]; then
            agent_jsonl=$(find "${SESS_DIR}" -name "*${target}*.jsonl" 2>/dev/null | head -1)
        fi

        if [ -z "${agent_jsonl}" ]; then
            echo -e "  target:  ${target:0:12}..."
            echo -e "  ${Y}jsonl 未検出（まだ起動前 or 既に完了）${N}"
        else
            a_mtime=$(stat -f %m "${agent_jsonl}")
            a_elapsed=$((now_ts - a_mtime))
            a_lines=$(wc -l < "${agent_jsonl}")

            if [ "${a_elapsed}" -lt 60 ]; then
                a_status="${G}WORKING${N}"
            elif [ "${a_elapsed}" -lt 300 ]; then
                a_status="${Y}PAUSE${N}"
            elif [ "${a_elapsed}" -lt 900 ]; then
                a_status="${Y}STALE${N}"
            else
                a_status="${R}STOPPED?${N}"
            fi

            last_agent_text=$(tail -100 "${agent_jsonl}" 2>/dev/null | python3 -c "
import sys, json
last_text = ''
for line in sys.stdin:
    try:
        d = json.loads(line)
        msg = d.get('message', {})
        content = msg.get('content', [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    t = item.get('text', '').strip()
                    if t:
                        last_text = t[:100]
    except Exception:
        pass
print(last_text)
" 2>/dev/null)

            echo -e "  target:  ${target:0:12}..."
            echo -e "  status:  ${a_status}  最終活動 ${a_elapsed}s 前 / 行数 ${a_lines}"
            echo -e "  last:    ${last_agent_text}"
        fi
    fi
fi
echo ""

# --- 3. Builder 進捗ログ（launchd 側の 5 分記録） ---
echo -e "${B}[3] 直近 5 分レポート（launchd 記録）${N}"
LOG_5MIN="${PROJ_ROOT}/data/logs/builder_monitor_5min.log"
if [ -f "${LOG_5MIN}" ]; then
    tail -8 "${LOG_5MIN}" | sed 's/^/  /'
else
    echo "  ログなし"
fi
echo ""

# --- 4. Task 状態 ---
echo -e "${B}[4] 進行中タスク${N}"
if [ -f "${PROJ_ROOT}/data/pending_completions.jsonl" ]; then
    grep -h '"in_progress"' "${PROJ_ROOT}/data/pending_completions.jsonl" 2>/dev/null | tail -5 | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line)
        print(f\"  #{d.get('id', '?')}: {d.get('subject', '')[:60]}\")
    except Exception:
        pass
" 2>/dev/null || echo "  (ログ parse 失敗)"
else
    echo "  (pending_completions.jsonl なし)"
fi
echo ""
echo -e "${C}更新: watch -n 5 ${0}${N}"
