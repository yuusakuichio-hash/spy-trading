#!/usr/bin/env python3
"""Supervisor: Sora idle 検知 + 再開トリガー。

2026-04-24 ゆうさくさん指示「違反で止まっても外部から再開させる仕組み」

launchd で 5 分ごと起動し、Sora メインセッションの jsonl を監視:
1. 直近 assistant response の tool_use 数確認
2. 経過時間が threshold 超 + tool_use 0 件 = idle 判定
3. 判定時の action:
   - Pushover 通知（ゆうさくさんに「ソラ止まってます」）
   - macOS notification（ローカル）
   - osascript で claude CLI に prompt 投入（完全自動再開）

macOS 通知 + Pushover + osascript 3 段構え。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = Path.home() / ".claude" / "projects" / "-Users-yuusakuichio-trading"
STATE_FILE = PROJECT_ROOT / "data" / "state_v3" / "supervisor_last_kick.json"

# しきい値
IDLE_THRESHOLD_SECS = 600  # 10 分以上 idle で検知
KICK_COOLDOWN_SECS = 1800  # 一度 kick したら 30 分は再 kick しない（連打防止）


def find_main_session_jsonl() -> Path | None:
    """直接 trading/ 直下の最新 session jsonl を返す（subagents/ は対象外）。"""
    if not SESSION_DIR.exists():
        return None
    candidates = sorted(
        SESSION_DIR.glob("*.jsonl"),  # 直下のみ・subagents/ 配下除外
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def analyze_last_turn(jsonl_path: Path) -> dict:
    """直近 assistant turn の情報を抽出。"""
    lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tool_use_count = 0
    last_assistant_ts = ""
    last_user_ts = ""
    last_role = "unknown"
    in_last_assistant = False
    for line in reversed(lines[-500:]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = rec.get("type", "")
        ts = rec.get("timestamp", "")
        if role == "user":
            if not last_user_ts:
                last_user_ts = ts
            if in_last_assistant:
                break
            continue
        if role != "assistant":
            continue
        if not last_assistant_ts:
            last_assistant_ts = ts
            last_role = role
        in_last_assistant = True
        msg = rec.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tool_use_count += 1
    return {
        "tool_use_count": tool_use_count,
        "last_assistant_ts": last_assistant_ts,
        "last_user_ts": last_user_ts,
        "last_role": last_role,
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def send_pushover(title: str, message: str, priority: int = 0) -> None:
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from common.pushover_client import send
        send(title, message, priority=priority)
    except Exception as exc:
        print(f"[supervisor] Pushover failed: {exc}", file=sys.stderr)


def send_macos_notification(title: str, message: str) -> None:
    safe_t = title.replace('"', "'")
    safe_m = message.replace('"', "'").replace("\n", " / ")[:200]
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_m}" with title "{safe_t}" sound name "Ping"'],
            check=False, timeout=5,
        )
    except Exception:
        pass


def main() -> int:
    jsonl = find_main_session_jsonl()
    if jsonl is None:
        print("[supervisor] no session jsonl found")
        return 0

    info = analyze_last_turn(jsonl)
    now = datetime.now(timezone.utc)
    file_mtime = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
    elapsed = (now - file_mtime).total_seconds()

    # idle 判定
    # - 最終書き込み (mtime) が IDLE_THRESHOLD_SECS 以上前
    # - 最新 role が user でない（user 発話後ソラ応答中は stop hook 責務・supervisor 介入しない）
    # - tool_use 0 件の assistant turn で終わっている
    last_role = info.get("last_role", "")
    tool_use = info.get("tool_use_count", -1)

    is_idle = (
        elapsed >= IDLE_THRESHOLD_SECS
        and last_role == "assistant"
        and tool_use == 0
    )

    state = load_state()
    last_kick_str = state.get("last_kick_ts", "")
    if last_kick_str:
        try:
            last_kick = datetime.fromisoformat(last_kick_str)
            since_last_kick = (now - last_kick).total_seconds()
            if since_last_kick < KICK_COOLDOWN_SECS:
                print(f"[supervisor] cooldown active ({since_last_kick:.0f}s < {KICK_COOLDOWN_SECS}s)")
                return 0
        except Exception:
            pass

    if not is_idle:
        elapsed_m = int(elapsed / 60)
        print(f"[supervisor] active: {jsonl.stem[:8]} / role={last_role} / tool_use={tool_use} / elapsed={elapsed_m}m")
        return 0

    # idle 検知 → kick
    elapsed_m = int(elapsed / 60)
    title = "[Sora Supervisor] Sora が止まっています"
    message = (
        f"セッション {jsonl.stem[:8]} / {elapsed_m}分 idle / tool_use=0\n"
        f"最新 role: {last_role}\n"
        "→ 「続けて」「次 task 進めて」等 1 言送ってください。\n"
        "ダッシュボード: http://192.168.10.123:8765/"
    )
    print(f"[supervisor] IDLE DETECTED → notifying: {title}")
    send_macos_notification(title, message)
    send_pushover(title, message, priority=1)

    state["last_kick_ts"] = now.isoformat()
    state["last_kick_reason"] = f"idle {elapsed_m}m / tool_use 0"
    save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
