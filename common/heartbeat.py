"""
common/heartbeat.py — Sora Lab 能動 Heartbeat Pulse 機構

各コンポーネント（chronos_agent / atlas_agent / chronos_watchdog / atlas_watchdog）が
1分毎に data/heartbeats/{component}.json へ pulse を書き込む共通ヘルパー。

使い方:
    from common.heartbeat import write_pulse

    # メインループ内で呼ぶ（60秒毎）
    write_pulse("chronos_agent", state="healthy", details={"cycle": 42})
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# heartbeat ファイルを格納するディレクトリ
# 環境変数 SORA_TRADING_DIR で上書き可能（テスト用）
_TRADING_DIR = Path(os.environ.get("SORA_TRADING_DIR", Path(__file__).parent.parent))
HEARTBEAT_DIR = _TRADING_DIR / "data" / "heartbeats"

# stale 判定しきい値（秒）: sora_heartbeat_monitor.py が参照する
STALE_THRESHOLD_SEC: int = 120  # 2分


def _heartbeat_path(component: str) -> Path:
    """コンポーネント名から heartbeat ファイルのパスを返す。

    component に path separator が含まれる場合は ValueError を送出する（インジェクション防止）。
    """
    if "/" in component or "\\" in component or ".." in component:
        raise ValueError(f"Invalid component name: {component!r}")
    return HEARTBEAT_DIR / f"{component}.json"


def write_pulse(
    component: str,
    state: str = "healthy",
    details: dict[str, Any] | None = None,
) -> Path:
    """heartbeat pulse を書き込む。

    Parameters
    ----------
    component:
        コンポーネント識別子。例: "chronos_agent", "atlas_watchdog"
    state:
        "healthy" | "degraded" | "critical"
    details:
        追加情報（任意）。例: {"cycle": 42, "positions": 3}

    Returns
    -------
    Path
        書き込んだファイルのパス。

    Raises
    ------
    ValueError
        component 名が不正な場合。
    """
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)

    pulse = {
        "component": component,
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "state": state,
        "details": details or {},
    }

    path = _heartbeat_path(component)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pulse, ensure_ascii=False, indent=2))
    tmp.replace(path)  # atomic rename

    return path


def read_pulse(component: str) -> dict[str, Any] | None:
    """heartbeat ファイルを読み込む。存在しない場合は None を返す。"""
    path = _heartbeat_path(component)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def is_stale(component: str, threshold_sec: int = STALE_THRESHOLD_SEC) -> tuple[bool, float]:
    """コンポーネントの heartbeat が stale かどうかを判定する。

    Returns
    -------
    (stale, age_sec):
        stale: True = stale（異常）
        age_sec: 最終 pulse からの経過秒数。ファイルなし時は inf。
    """
    path = _heartbeat_path(component)
    if not path.exists():
        return True, float("inf")

    # ファイル mtime で判定（pulse 書き込み時刻の近似）
    mtime = path.stat().st_mtime
    age_sec = time.time() - mtime
    return age_sec >= threshold_sec, age_sec


def list_components() -> list[str]:
    """HEARTBEAT_DIR 内の全コンポーネント名リストを返す。"""
    if not HEARTBEAT_DIR.exists():
        return []
    return [p.stem for p in sorted(HEARTBEAT_DIR.glob("*.json"))]
