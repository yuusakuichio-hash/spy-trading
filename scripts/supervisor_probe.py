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

# Fork session auto-spawn 制御
# SORA_AUTO_FORK=1 env で有効化・default off（cost 対策）
AUTO_FORK_ENABLED = os.environ.get("SORA_AUTO_FORK", "0") == "1"
MAX_FORKS_PER_DAY = 6  # 1 日最大 6 回 fork（cost 上限）

# Fork prompt（carryover の安全 task のみに限定）
FORK_PROMPT = """あなたは Sora Lab の auto-continue fork session です。
main Claude session が idle で止まっているため、fork で安全な継続作業を進めます。

厳守制約:
- ADR-015 判断待ち事項（ゆうさくさん承認要）には一切触らない
- 既存コード (spy_bot.py / chronos_bot.py / common/ 等) 改変禁止
- paper 口座の発注・send_pushover 等 side-effect ある操作禁止
- data/sprint1_carryovers.md の未完 carryover で「ゆうさくさん判断不要」のもののみ着手
- 作業内容は 1 件に絞る・実行 → test → commit まで完結
- 完遂できない task は触らない（半端な状態で放置しない）
- 完了後は「このターンで終了します」と明記して exit

着手推奨（優先度順）:
1. C-025 assert 残関数（check_var / _check_kill_switch 等）
2. memory の古い archive 整理
3. pytest 全件走行結果の最終確認
4. ダッシュボード改善の小項目

止まらず 1 task 完遂して終了。"""


def auto_fork_spawn(reason: str) -> bool:
    """SORA_AUTO_FORK=1 なら claude -p --fork-session で別プロセス spawn。"""
    if not AUTO_FORK_ENABLED:
        return False
    log_path = PROJECT_ROOT / "data" / "logs" / f"auto_fork_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    cmd = [
        "/opt/homebrew/bin/claude",
        "-p",
        "--continue",
        "--fork-session",
        "--dangerously-skip-permissions",
        FORK_PROMPT,
    ]
    try:
        with log_path.open("w") as logf:
            proc = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=logf,
                cwd=str(PROJECT_ROOT),
                start_new_session=True,
            )
        print(f"[supervisor] auto_fork spawned PID {proc.pid} → {log_path}")
        return True
    except Exception as exc:
        print(f"[supervisor] auto_fork failed: {exc}", file=sys.stderr)
        return False


def count_forks_today() -> int:
    """data/logs/auto_fork_YYYYMMDD_*.log で本日 fork 回数を数える。"""
    today = datetime.now().strftime("%Y%m%d")
    log_dir = PROJECT_ROOT / "data" / "logs"
    if not log_dir.exists():
        return 0
    return len(list(log_dir.glob(f"auto_fork_{today}_*.log")))


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

    # idle 検知 → 通知 + 可能なら auto_fork
    elapsed_m = int(elapsed / 60)

    # Auto-fork：ゆうさくさん介入なしで fork session を spawn
    fork_spawned = False
    forks_today = count_forks_today()
    if AUTO_FORK_ENABLED and forks_today < MAX_FORKS_PER_DAY:
        fork_spawned = auto_fork_spawn(f"idle {elapsed_m}m")

    title = "[Sora Supervisor] Sora idle 検知"
    base_msg = f"セッション {jsonl.stem[:8]} / {elapsed_m}分 idle / tool_use=0"
    if fork_spawned:
        message = (
            f"{base_msg}\n"
            f"→ AUTO_FORK で別 session spawn 済 (本日 {forks_today + 1}/{MAX_FORKS_PER_DAY})\n"
            "進捗: data/logs/auto_fork_*.log"
        )
    else:
        reason = "AUTO_FORK 無効" if not AUTO_FORK_ENABLED else f"本日 {forks_today}/{MAX_FORKS_PER_DAY} 上限"
        message = (
            f"{base_msg}\n"
            f"→ 手動介入推奨（{reason}）。\n"
            "「続けて」等 1 言送ってください。\n"
            "ダッシュボード: http://192.168.10.123:8765/"
        )

    print(f"[supervisor] IDLE DETECTED → {('auto_fork' if fork_spawned else 'notify only')}")
    send_macos_notification(title, message)
    send_pushover(title, message, priority=1)

    state["last_kick_ts"] = now.isoformat()
    state["last_kick_reason"] = f"idle {elapsed_m}m / tool_use 0"
    state["last_kick_fork"] = fork_spawned
    save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
