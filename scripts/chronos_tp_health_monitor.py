#!/usr/bin/env python3
"""
scripts/chronos_tp_health_monitor.py — Chronos TradersPost Forwarder 監視スクリプト

10分ごとに LaunchAgent から起動される。
- VPS chronos_traderspost_forwarder.service の稼働確認
- 直近 execution log の成功率集計
- 異常時 Pushover アラート

環境変数:
    CHRONOS_TP_EXEC_LOG_REMOTE  — VPS上のexec log パス (デフォルト /root/spxbot/data/chronos_traderspost_executions.jsonl)
    CHRONOS_TP_HEALTH_WINDOW    — 成功率チェック窓 (件数, デフォルト 20)
    CHRONOS_TP_ALERT_THRESHOLD  — 成功率異常閾値 (0.0-1.0, デフォルト 0.8)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))

# 設定
VPS_HOST = "root@198.13.37.17"
VPS_KEY = os.path.expanduser("~/.ssh/deploy_key")
EXEC_LOG_REMOTE = os.environ.get(
    "CHRONOS_TP_EXEC_LOG_REMOTE",
    "/root/spxbot/data/chronos_traderspost_executions.jsonl",
)
HEALTH_WINDOW = int(os.environ.get("CHRONOS_TP_HEALTH_WINDOW", "20"))
ALERT_THRESHOLD = float(os.environ.get("CHRONOS_TP_ALERT_THRESHOLD", "0.8"))


def _notify(title: str, message: str, priority: int = 0) -> None:
    # .env からロード
    env_path = _HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    try:
        from common.pushover_client import send  # noqa: PLC0415
        send(title, message, priority=priority)
    except Exception as e:
        print(f"[Pushover] 通知失敗: {e}", file=sys.stderr)


def check_vps_service() -> tuple[bool, str]:
    """VPS で chronos_traderspost_forwarder.service が active かチェックする。"""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", VPS_KEY,
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                VPS_HOST,
                "systemctl is-active chronos_traderspost_forwarder.service",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        status = result.stdout.strip()
        return status == "active", status
    except Exception as e:
        return False, f"ssh error: {e}"


def fetch_recent_executions(n: int = HEALTH_WINDOW) -> list[dict]:
    """VPS の executions.jsonl から直近 n 件を取得する。"""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", VPS_KEY,
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                VPS_HOST,
                f"tail -n {n} {EXEC_LOG_REMOTE} 2>/dev/null || echo ''",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries
    except Exception as e:
        print(f"[Monitor] exec log 取得失敗: {e}", file=sys.stderr)
        return []


def calc_success_rate(entries: list[dict]) -> float:
    """executions の成功率を計算する (error=None が成功)。"""
    if not entries:
        return 1.0  # データなし = 異常なし
    success = sum(1 for e in entries if e.get("error") is None)
    return success / len(entries)


def main() -> None:
    # .env ロード
    env_path = _HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] chronos_tp_health_monitor 開始")

    # 1. サービス稼働確認
    service_ok, service_status = check_vps_service()
    print(f"[Monitor] service status: {service_status}")

    if not service_ok:
        msg = (
            f"[Chronos/ALERT] TradersPost Forwarder サービス停止\n"
            f"status={service_status}\n"
            f"VPS: {VPS_HOST}\n"
            f"確認: ssh -i ~/.ssh/deploy_key {VPS_HOST} 'systemctl status chronos_traderspost_forwarder'"
        )
        print(f"[Monitor] ALERT: {msg}", file=sys.stderr)
        _notify("[Chronos/ALERT] TP Forwarder 停止", msg, priority=1)
        sys.exit(1)

    # 2. 成功率チェック
    entries = fetch_recent_executions(HEALTH_WINDOW)
    rate = calc_success_rate(entries)
    total = len(entries)
    success = sum(1 for e in entries if e.get("error") is None)

    print(f"[Monitor] 直近{total}件 成功率={rate:.1%} ({success}/{total})")

    if total > 0 and rate < ALERT_THRESHOLD:
        msg = (
            f"[Chronos/ALERT] TradersPost 転送成功率低下\n"
            f"直近{total}件 成功率={rate:.1%} (閾値{ALERT_THRESHOLD:.0%})\n"
            f"失敗: {total - success}件\n"
            f"exec log: {EXEC_LOG_REMOTE}"
        )
        print(f"[Monitor] ALERT: {msg}", file=sys.stderr)
        _notify("[Chronos/ALERT] TP 転送成功率低下", msg, priority=1)
        sys.exit(1)

    print(f"[Monitor] OK: service=active success_rate={rate:.1%}")


if __name__ == "__main__":
    main()
