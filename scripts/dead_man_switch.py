#!/usr/bin/env python3
"""
scripts/dead_man_switch.py — Prometheus 式 Dead Man's Switch

役割:
    15分毎に beacon を data/ops/heartbeat/dead_man_ping.jsonl へ記録する。
    Bot 自身の heartbeat とは独立した check process として動作する。

    直近 ping から:
      30分途絶 → Pushover P2 (ALL_BOTS_DOWN キーワード付き・quiet hour 回避)
      60分途絶 → fallback log + P2 継続送信

監視対象 (beacon components):
    - spy_bot
    - atlas_agent
    - chronos_webhook_server
    - chronos_traderspost_forwarder

LaunchAgent: com.soralab.dead_man_switch (15分間隔)
呼び出し: python3 scripts/dead_man_switch.py [--beacon | --check]
    --beacon  : beacon 書き込みのみ (Bot 自身が定期実行)
    --check   : ping ファイルを読んで timeout 判定 (LaunchAgent から呼ぶ)
    引数なし  : --check と同等
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from common.pushover_client import send as pushover_send

# ── パス設定 ──────────────────────────────────────────────────────────────────
_TRADING_DIR = Path(os.environ.get("SORA_TRADING_DIR", _PROJECT_ROOT))
PING_DIR = _TRADING_DIR / "data" / "ops" / "heartbeat"
PING_FILE = PING_DIR / "dead_man_ping.jsonl"

# ── 閾値 ──────────────────────────────────────────────────────────────────────
WARN_SEC = 30 * 60    # 30分: P2 alert
CRIT_SEC = 60 * 60    # 60分: fallback log + P2 継続

# ── 監視対象コンポーネント ────────────────────────────────────────────────────
COMPONENTS = [
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
log = logging.getLogger("dead_man_switch")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] dead_man_switch: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

LOG_DIR = _TRADING_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_fh = logging.FileHandler(LOG_DIR / "dead_man_switch.log")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)


# ── ping レコード hash ────────────────────────────────────────────────────────

def _make_hash(ts_iso: str, component: str) -> str:
    raw = f"{ts_iso}:{component}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# ── beacon 書き込み ───────────────────────────────────────────────────────────

def write_beacon(component: str = "dead_man_switch") -> None:
    """ping レコードを JSONL に追記する。"""
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


def write_all_beacons() -> None:
    """全 COMPONENTS + 自身の beacon を書き込む。"""
    for comp in COMPONENTS:
        write_beacon(comp)
    write_beacon("dead_man_switch")


# ── 直近 ping 読み取り ────────────────────────────────────────────────────────

def _read_last_ping(component: str) -> float | None:
    """component の直近 ping timestamp (epoch) を返す。なければ None。"""
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


# ── check ループ ─────────────────────────────────────────────────────────────

def check_and_alert() -> None:
    """全 COMPONENTS の ping を確認し、timeout 時は Pushover P2 を送信する。

    実装は common_v3.observability.deadman.check_and_alert() に委譲する。
    戻り値 dict は本スクリプトでは使用しないが、ライブラリ側は dict を返す。
    """
    from common_v3.observability.deadman import check_and_alert as _lib_check  # noqa: PLC0415
    result = _lib_check()
    if result["ok"]:
        log.info("all beacons OK")
    else:
        log.error("ALERT: warn=%s crit=%s", result["warn"], result["crit"])


def _fallback_log(title: str, msg: str) -> None:
    fallback = LOG_DIR / "dead_man_fallback.log"
    now_iso = datetime.now(timezone.utc).isoformat()
    with fallback.open("a", encoding="utf-8") as f:
        f.write(f"{now_iso} | {title} | {msg}\n")


# ── JSONL ローテーション (7日以上古い行を削除) ───────────────────────────────

def _rotate_ping_file() -> None:
    if not PING_FILE.exists():
        return
    cutoff = time.time() - 7 * 86400
    kept: list[str] = []
    try:
        for line in PING_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"]).timestamp()
                if ts >= cutoff:
                    kept.append(line)
            except Exception:
                kept.append(line)  # パース失敗行は保持
        PING_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass


# ── エントリポイント ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Dead Man's Switch")
    parser.add_argument("--beacon", action="store_true", help="beacon 書き込みのみ")
    parser.add_argument("--check", action="store_true", help="timeout 判定のみ")
    parser.add_argument("--component", default=None, help="--beacon 時に使うコンポーネント名")
    args = parser.parse_args()

    if args.beacon:
        comp = args.component or "dead_man_switch"
        write_beacon(comp)
    else:
        # --check または引数なし: beacon を書いてから check
        write_all_beacons()
        _rotate_ping_file()
        check_and_alert()


if __name__ == "__main__":
    main()
