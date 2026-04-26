#!/usr/bin/env python3
"""市場時間中Atlas稼働監視・停止検知→即アラート+自動再起動"""
import os
import subprocess
import time
from datetime import datetime
import pytz

LOG = "/Users/yuusakuichio/trading/data/logs/market_hours_atlas_monitor.log"
ALERT = "/Users/yuusakuichio/trading/data/logs/emergency_alerts.log"
LABEL = "com.spybot.paper"
AGENT_LABEL = "com.atlas.agent"
CHECK_INTERVAL = 300  # 5分


def log(msg: str) -> None:
    ts = datetime.now().isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def notify(title: str, msg: str) -> None:
    with open(ALERT, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] [{title}] {msg}\n")
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{msg}" with title "{title}" sound name "Glass"',
            ],
            check=False,
            timeout=5,
        )
    except Exception as e:
        log(f"notify failed: {e}")


def is_market_open() -> bool:
    """ET 9:30-16:00 (DST自動判定)"""
    et = datetime.now(pytz.timezone("America/New_York"))
    if et.weekday() >= 5:  # 土日
        return False
    h, m = et.hour, et.minute
    # 9:30以降 かつ 16:00未満
    if (h, m) < (9, 30):
        return False
    if (h, m) >= (16, 0):
        return False
    return True


def get_pid(label: str) -> str:
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], text=True, timeout=10
        )
    except Exception as e:
        log(f"launchctl list failed: {e}")
        return "-"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            return parts[0]
    return "-"


def kickstart(label: str) -> None:
    uid = os.getuid()
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            check=False,
            timeout=15,
        )
        log(f"kickstart issued: {label}")
    except Exception as e:
        log(f"kickstart failed {label}: {e}")


def check_once() -> None:
    if not is_market_open():
        log("market closed → skip check")
        return

    spy_pid = get_pid(LABEL)
    atlas_pid = get_pid(AGENT_LABEL)

    log(f"market OPEN / spy_bot={spy_pid} / atlas_agent={atlas_pid}")

    alerts = []
    if spy_pid == "-":
        alerts.append(f"{LABEL} DOWN")
        kickstart(LABEL)
    if atlas_pid == "-":
        alerts.append(f"{AGENT_LABEL} DOWN")
        kickstart(AGENT_LABEL)

    if alerts:
        notify(
            "Atlas Market-Hours Monitor",
            "市場時間中の停止検知: " + ", ".join(alerts) + " / auto-kickstart実施",
        )


def main() -> None:
    log("market_hours_atlas_monitor started")
    while True:
        try:
            check_once()
        except Exception as e:
            log(f"check_once failed: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
