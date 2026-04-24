"""
atlas_v3/supervision/self_monitor.py — Sentinel 自身の最後の砦

役割:
    sentinel_watchdog.py が crash / ハングした場合に「Sentinel が沈黙している」を
    検出し、次の launchd 起動サイクルまでの空白を最小化するための
    最終防衛線。

設計:
    - sentinel_heartbeat.jsonl の mtime を監視
    - mtime が SENTINEL_STALE_SEC を超えた場合は Pushover P1 送信
    - launchctl kickstart で sentinel_watchdog 自体を再起動
    - 本モジュールは atlas_v3 MonitorDaemon の定期チェックから呼ばれる想定
      (atlas_v3/ops/monitor_daemon.py などから import)

公開 API:
    check_sentinel_liveness(stale_sec: int = 90) -> tuple[bool, str]
        sentinel が生存しているか確認し (healthy, reason) を返す

    recover_sentinel_if_needed(stale_sec: int = 90) -> bool
        異常なら launchctl kickstart + Pushover P1 を発火し True を返す
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_TRADING_DIR = Path(os.environ.get("SORA_TRADING_DIR", _PROJECT_ROOT))

SENTINEL_HEARTBEAT_FILE = (
    _TRADING_DIR / "data" / "ops" / "heartbeat" / "sentinel_heartbeat.jsonl"
)

SENTINEL_LAUNCHD_LABEL: str = os.environ.get(
    "SELF_MONITOR_SENTINEL_LABEL", "com.soralab.sentinel-watchdog"
)

# sentinel の heartbeat が何秒以上途絶えたら異常とみなすか
# デフォルト 90s = CHECK_INTERVAL_SEC(30) * 3
SENTINEL_DEFAULT_STALE_SEC: int = int(
    os.environ.get("SELF_MONITOR_STALE_SEC", "90")
)

log = logging.getLogger("atlas_v3.supervision.self_monitor")


def check_sentinel_liveness(
    stale_sec: int = SENTINEL_DEFAULT_STALE_SEC,
) -> tuple[bool, str]:
    """sentinel_watchdog の heartbeat ファイルを確認し生存状態を返す。

    Args:
        stale_sec: heartbeat の許容停滞秒数

    Returns:
        (healthy: bool, reason: str)
    """
    if not SENTINEL_HEARTBEAT_FILE.exists():
        return False, "sentinel_heartbeat.jsonl が存在しない (起動前 or crash)"

    try:
        mtime = SENTINEL_HEARTBEAT_FILE.stat().st_mtime
        age_sec = time.time() - mtime
    except OSError as exc:
        return False, f"stat エラー: {exc}"

    if age_sec > stale_sec:
        return False, f"heartbeat が {age_sec:.0f}s 更新なし (閾値={stale_sec}s)"

    # JSONL 最終行も確認
    try:
        text = SENTINEL_HEARTBEAT_FILE.read_text(encoding="utf-8")
        last_line = ""
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line:
                last_line = line
                break
        if not last_line:
            return False, "heartbeat JSONL が空"
        rec = json.loads(last_line)
        last_ts = rec.get("ts", "")
        return True, f"alive (last_ts={last_ts}, age={age_sec:.0f}s)"
    except Exception as exc:  # noqa: BLE001
        # ファイルが存在して mtime が新しければ生存とみなす
        return True, f"mtime OK (JSONL parse warn: {exc})"


def recover_sentinel_if_needed(
    stale_sec: int = SENTINEL_DEFAULT_STALE_SEC,
) -> bool:
    """sentinel が停滞していれば launchctl kickstart + Pushover P1 を発火する。

    Returns:
        True: 異常検知して回復を試みた
        False: 正常 (何もしなかった)
    """
    healthy, reason = check_sentinel_liveness(stale_sec=stale_sec)
    if healthy:
        log.debug("self_monitor: sentinel OK reason=%s", reason)
        return False

    log.error("self_monitor: sentinel STALE reason=%s", reason)

    # Pushover P1
    _send_p1(reason)

    # launchctl kickstart
    restarted = _kickstart_sentinel()
    log.warning("self_monitor: kickstart_sentinel result=%s", restarted)
    return True


def _send_p1(reason: str) -> None:
    title = "[SELF_MONITOR] sentinel_watchdog 停止検知"
    msg = f"sentinel_watchdog が応答していません。理由: {reason} launchctl 再起動を試みました。"
    log.critical("SELF_MONITOR P1: %s | %s", title, msg)
    try:
        from common.pushover_client import send as pushover_send  # noqa: PLC0415
        pushover_send(title, msg, priority=1)
    except Exception as exc:  # noqa: BLE001
        log.error("self_monitor Pushover P1 failed: %s", exc)


def _kickstart_sentinel() -> bool:
    """launchctl kickstart -k com.soralab.sentinel-watchdog を発行する。"""
    uid = os.getuid()
    target = f"gui/{uid}/{SENTINEL_LAUNCHD_LABEL}"
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            log.info("self_monitor: kickstart OK target=%s", target)
            return True
        log.warning(
            "self_monitor: kickstart FAILED rc=%d stderr=%s",
            result.returncode,
            result.stderr.strip(),
        )
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("self_monitor: kickstart exception: %s", exc)
        return False


# ---------------------------------------------------------------------------
# CLI entry point — python -m atlas_v3.supervision.self_monitor
# ---------------------------------------------------------------------------

def main() -> None:  # pragma: no cover — launchd 30s one-shot
    """launchd plist (com.soralab.self-monitor) から 30s ごとに呼ばれる one-shot entry。

    StartInterval=30 で繰り返し起動されるため、プロセスは 1 回チェックして終了する。
    exit code: 0=正常 or 回復不要, 1=sentinel 停滞を検知して kickstart を試みた。
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stale_sec = int(os.environ.get("SELF_MONITOR_STALE_SEC", str(SENTINEL_DEFAULT_STALE_SEC)))
    acted = recover_sentinel_if_needed(stale_sec=stale_sec)
    sys.exit(1 if acted else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
