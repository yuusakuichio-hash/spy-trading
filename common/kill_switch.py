"""Kill Switch — Emergency全停止機構

data/kill_switch.flag ファイル存在で全発注即停止。
webhook_server の /emergency_stop endpoint から制御可。
"""
from __future__ import annotations
import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
FLAG_FILE = BASE / "data" / "kill_switch.flag"


def is_active() -> bool:
    """Kill Switchが発動中か"""
    return FLAG_FILE.exists()


def activate(reason: str = "manual") -> None:
    """Kill Switch発動"""
    FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().isoformat()
    FLAG_FILE.write_text(f"activated_at={ts}\nreason={reason}\n", encoding="utf-8")


def deactivate() -> None:
    """Kill Switch解除（手動承認時のみ）"""
    if FLAG_FILE.exists():
        FLAG_FILE.unlink()


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
