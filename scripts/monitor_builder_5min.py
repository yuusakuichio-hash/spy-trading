#!/usr/bin/env python3
"""Builder 進捗を 5 分ごと Pushover 配信（Sora Lab スピード基準）。

使い方:
  python3 scripts/monitor_builder_5min.py <agent_id>
  python3 scripts/monitor_builder_5min.py a57defe4fbf70f409

動作:
  対象 agent JSONL の末尾から新規 message を抽出し、
  前回時刻以降の差分を要約して Pushover 送信。
  state file: data/builder_monitor_state.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

STATE_FILE = PROJECT_ROOT / "data" / "builder_monitor_state.json"

JSONL_CANDIDATES = [
    Path.home() / ".claude" / "projects" / "-Users-yuusakuichio-trading",
]


def find_jsonl(agent_id: str) -> Path | None:
    for base in JSONL_CANDIDATES:
        for p in base.rglob(f"agent-{agent_id}.jsonl"):
            return p
        for p in base.rglob(f"*{agent_id}*.jsonl"):
            return p
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def parse_messages(jsonl_path: Path) -> list[dict]:
    messages = []
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                messages.append(rec)
    except FileNotFoundError:
        return []
    return messages


def extract_summary(messages: list[dict], since_ts: str | None) -> tuple[str, int, str]:
    new_texts: list[str] = []
    latest_ts = since_ts or ""
    tool_uses = 0

    for m in messages:
        ts = m.get("timestamp", "")
        if since_ts and ts <= since_ts:
            continue
        if ts > latest_ts:
            latest_ts = ts
        msg_block = m.get("message", {})
        content = msg_block.get("content", [])
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    t = item.get("text", "").strip()
                    if t:
                        new_texts.append(t)
                elif item.get("type") == "tool_use":
                    tool_uses += 1

    if not new_texts:
        return "", tool_uses, latest_ts

    last_text = new_texts[-1]
    if len(last_text) > 300:
        last_text = last_text[:300] + "..."
    summary = last_text
    return summary, tool_uses, latest_ts


def send_macos_notification(title: str, message: str) -> bool:
    """macOS 通知センター経由（quiet hours bypass・手元ゆうさくさん向け即通知）。"""
    import subprocess
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'").replace("\n", " / ")[:200]
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"'],
            check=False, timeout=5,
        )
        return True
    except Exception as exc:
        print(f"[osascript error] {exc}", file=sys.stderr)
        return False


def log_to_file(title: str, message: str) -> None:
    log = PROJECT_ROOT / "data" / "logs" / "builder_monitor_5min.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {title}\n{message}\n---\n")


def send_pushover(title: str, message: str) -> bool:
    """2026-04-24 ゆうさくさん指示: Pushover/macOS 通知削除・log file のみ保持。
    ダッシュボード (scripts/sora_live_status.sh) が別窓 Terminal で表示する方式に移行。
    """
    log_to_file(title, message)
    return True


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        agent_id = sys.argv[1].strip()
    else:
        target_file = PROJECT_ROOT / "data" / "monitor_target.txt"
        if target_file.exists():
            agent_id = target_file.read_text(encoding="utf-8").strip()
        else:
            print("usage: monitor_builder_5min.py <agent_id>", file=sys.stderr)
            return 1
        if not agent_id:
            print("monitor_target.txt empty — no agent to monitor", file=sys.stderr)
            return 0

    jsonl_path = find_jsonl(agent_id)
    if jsonl_path is None:
        send_pushover(
            f"[Sora] Builder {agent_id[:8]} monitor",
            f"JSONL not found — agent might have not started yet or already completed.",
        )
        return 0

    state = load_state()
    since_ts = state.get(agent_id, {}).get("last_ts")

    messages = parse_messages(jsonl_path)
    summary, tool_uses_delta, latest_ts = extract_summary(messages, since_ts)

    total_messages = len(messages)
    prev_count = state.get(agent_id, {}).get("message_count", 0)
    delta = total_messages - prev_count

    now_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%H:%M")

    if delta == 0 and not summary:
        title = f"[Sora 5min] {now_jst} Builder 進捗なし"
        body = f"agent {agent_id[:8]} / msgs={total_messages} 変化なし（前回比 +0）"
    else:
        title = f"[Sora 5min] {now_jst} Builder 作業中"
        body = (
            f"agent {agent_id[:8]}\n"
            f"msgs: {prev_count} → {total_messages} (+{delta})\n"
            f"tools +{tool_uses_delta}\n"
            f"---\n"
            f"{summary if summary else '(tool use のみ・text なし)'}"
        )
        if len(body) > 900:
            body = body[:900] + "..."

    send_pushover(title, body)

    state[agent_id] = {
        "last_ts": latest_ts,
        "message_count": total_messages,
        "last_check": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
