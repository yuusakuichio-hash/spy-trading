"""Kill Switch — Emergency全停止機構

data/kill_switch.flag ファイル存在で全発注即停止。
webhook_server の /kill_switch/activate, /kill_switch/deactivate endpoint から制御可。
直接ファイル操作も後方互換でサポートするが、audit記録は必須。

CRITICAL-4修正 (2026-04-18):
  - activate() 時に PID/timestamp/reason/activator を JSONL追記記録
  - is_active() を5秒TTLキャッシュで race condition 軽減
  - deactivate() 時に Pushover priority=1 送信 + audit記録
  - webhook_server経由の発動・解除エンドポイント追加
"""
from __future__ import annotations
import datetime
import json
import os
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
FLAG_FILE  = BASE / "data" / "kill_switch.flag"
AUDIT_FILE = BASE / "data" / "kill_switch_audit.jsonl"

# ── TTLキャッシュ (5秒) ────────────────────────────────────────────────────────
_cache_value: bool = False
_cache_ts: float = 0.0
_CACHE_TTL: float = 5.0


def _pushover_kill_switch(title: str, message: str, priority: int = 1) -> None:
    """Pushover通知（kill_switch専用・importループ回避のため内部実装）"""
    try:
        import requests as _req
        _req.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":    os.environ.get("PUSHOVER_TOKEN", ""),
                "user":     os.environ.get("PUSHOVER_USER", ""),
                "title":    title,
                "message":  message[:1024],
                "priority": priority,
                # priority=1 の場合は retry/expire が必須
                **({"retry": 60, "expire": 300} if priority >= 1 else {}),
            },
            timeout=10,
        )
    except Exception:
        pass


def _write_audit(event: str, reason: str = "", activator: str = "unknown") -> None:
    """JSONL形式でaudit記録を追記する。"""
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":        datetime.datetime.now().isoformat(),
        "event":     event,
        "reason":    reason,
        "activator": activator,
        "pid":       os.getpid(),
    }
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def is_active() -> bool:
    """Kill Switchが発動中か（5秒TTLキャッシュ）"""
    global _cache_value, _cache_ts
    now = time.monotonic()
    if now - _cache_ts < _CACHE_TTL:
        return _cache_value
    _cache_value = FLAG_FILE.exists()
    _cache_ts = now
    return _cache_value


def activate(reason: str = "manual", activator: str = "unknown") -> None:
    """Kill Switch発動 — audit記録必須"""
    FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().isoformat()
    FLAG_FILE.write_text(
        f"activated_at={ts}\nreason={reason}\nactivator={activator}\npid={os.getpid()}\n",
        encoding="utf-8",
    )
    # キャッシュ即無効化
    global _cache_value, _cache_ts
    _cache_value = True
    _cache_ts = time.monotonic()

    _write_audit("activate", reason=reason, activator=activator)
    _pushover_kill_switch(
        "[Atlas/ALERT] Kill Switch 発動",
        f"reason={reason}\nactivator={activator}\npid={os.getpid()}\nts={ts}",
        priority=1,
    )


def deactivate(activator: str = "unknown") -> None:
    """Kill Switch解除（手動承認時のみ） — audit記録 + Pushover priority=1"""
    if FLAG_FILE.exists():
        FLAG_FILE.unlink()

    # キャッシュ即無効化
    global _cache_value, _cache_ts
    _cache_value = False
    _cache_ts = time.monotonic()

    _write_audit("deactivate", reason="manual_deactivate", activator=activator)
    _pushover_kill_switch(
        "[Atlas/ALERT] Kill Switch 解除",
        f"activator={activator}\npid={os.getpid()}\nts={datetime.datetime.now().isoformat()}",
        priority=1,
    )


def reason() -> str | None:
    """発動理由"""
    if not FLAG_FILE.exists():
        return None
    try:
        text = FLAG_FILE.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("reason="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return "unknown"
