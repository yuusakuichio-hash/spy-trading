"""
common_v3/observability/deadman.py — Dead Man's Switch ライブラリ公開 API

beacon path は既存 data/ops/heartbeat/dead_man_ping.jsonl を継承する。
(spec L320 の data/state_v3/deadman/ への移設は Sprint 1 で spec 側を現実追従修正予定)

公開 API:
    write_beacon(component: str) -> None
    check_and_alert() -> dict
    get_last_ping(component: str) -> float | None
    list_components() -> list[str]

閾値: WARN=30min / CRIT=60min (既存 dead_man_switch.py と同値)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ── パス設定 ─────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_TRADING_DIR = Path(os.environ.get("SORA_TRADING_DIR", str(_PROJECT_ROOT)))
PING_DIR = _TRADING_DIR / "data" / "ops" / "heartbeat"
PING_FILE = PING_DIR / "dead_man_ping.jsonl"

# ── 閾値 (既存踏襲) ───────────────────────────────────────────────────────────
WARN_SEC: int = 30 * 60   # 30分
CRIT_SEC: int = 60 * 60   # 60分

# ── 監視対象コンポーネント (既存踏襲) ─────────────────────────────────────────
COMPONENTS: list[str] = [
    "spy_bot",
    "atlas_agent",
    "chronos_webhook_server",
    "chronos_traderspost_forwarder",
    # HIGH 9 fix (2026-04-22): Chronos コアコンポーネント追加
    "chronos_agent",
    "chronos_bot",
    "chronos_webhook_queue_reader",
]

# ── ロガー ───────────────────────────────────────────────────────────────────
log = logging.getLogger("common_v3.observability.deadman")


# ── 内部ヘルパー ──────────────────────────────────────────────────────────────

def _make_hash(ts_iso: str, component: str) -> str:
    raw = f"{ts_iso}:{component}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# ── 公開 API ──────────────────────────────────────────────────────────────────

def write_beacon(component: str) -> None:
    """ping レコードを JSONL に追記する。

    Args:
        component: ビーコンを書き込むコンポーネント名。
    """
    PING_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ts_iso = now.isoformat()
    record = {
        "ts": ts_iso,
        "component": component,
        "hash": _make_hash(ts_iso, component),
    }
    with PING_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info("beacon written: component=%s ts=%s", component, ts_iso)


def get_last_ping(component: str) -> float | None:
    """component の直近 ping timestamp (epoch float) を返す。なければ None。

    Args:
        component: 検索対象のコンポーネント名。

    Returns:
        最終 ping の UNIX timestamp。ファイル不在・レコード不在の場合は None。
    """
    if not PING_FILE.exists():
        return None
    last_ts: float | None = None
    try:
        lines = PING_FILE.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("component") == component:
                    last_ts = datetime.fromisoformat(rec["ts"]).timestamp()
                    break
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    except OSError:
        pass
    return last_ts


def list_components() -> list[str]:
    """監視対象コンポーネント名の一覧を返す。

    Returns:
        COMPONENTS リストのコピー。
    """
    return list(COMPONENTS)


def check_and_alert() -> dict:
    """全 COMPONENTS の ping を確認し、timeout があれば Pushover P2 を送信する。

    既存 dead_man_switch.py の check_and_alert() をライブラリ化したもの。
    Pushover 送信失敗は警告ログのみ（例外を外に漏らさない）。

    Returns:
        {
            "ok": bool,           # 問題なし = True
            "warn": list[str],    # WARN 対象コンポーネント名
            "crit": list[str],    # CRIT 対象コンポーネント名
            "checked_at": float,  # チェック実施時刻 (epoch)
        }
    """
    now = time.time()
    warn_list: list[str] = []
    crit_list: list[str] = []

    for comp in COMPONENTS:
        last = get_last_ping(comp)
        age_sec = (now - last) if last is not None else float("inf")

        if age_sec >= CRIT_SEC:
            crit_list.append(comp)
            log.warning("CRITICAL: %s beacon absent %.0f min", comp, age_sec / 60)
        elif age_sec >= WARN_SEC:
            warn_list.append(comp)
            log.warning("WARN: %s beacon absent %.0f min", comp, age_sec / 60)

    result: dict = {
        "ok": len(warn_list) == 0 and len(crit_list) == 0,
        "warn": warn_list,
        "crit": crit_list,
        "checked_at": now,
    }

    if not result["ok"]:
        _send_alert(warn_list, crit_list)

    return result


def _send_alert(warn_list: list[str], crit_list: list[str]) -> None:
    """アラート送信（Pushover + fallback log）。送信失敗は握り潰してログのみ。"""
    try:
        from common.pushover_client import send as pushover_send  # type: ignore[import]
    except ImportError:
        log.error("pushover_client import failed; alert dropped")
        return

    if crit_list:
        title = "[SYS] ALL_BOTS_DOWN CRITICAL Dead Man"
        msg = f"60分以上 beacon 途絶 (rescue 要): {', '.join(crit_list)}"
    else:
        title = "[SYS] ALL_BOTS_DOWN WARN Dead Man"
        msg = f"30分以上 beacon 途絶: {', '.join(warn_list)}"

    log.error("ALERT: %s | %s", title, msg)
    try:
        pushover_send(title, msg, priority=2)
    except Exception as exc:  # noqa: BLE001
        log.error("Pushover send failed: %s", exc)

    _fallback_log(title, msg)


def _fallback_log(title: str, msg: str) -> None:
    log_dir = _TRADING_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fallback = log_dir / "dead_man_fallback.log"
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with fallback.open("a", encoding="utf-8") as f:
            f.write(f"{now_iso} | {title} | {msg}\n")
    except OSError as exc:
        log.error("fallback log write failed: %s", exc)
