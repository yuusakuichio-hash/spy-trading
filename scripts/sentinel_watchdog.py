#!/usr/bin/env python3
"""
scripts/sentinel_watchdog.py — Sentinel Watchdog (supervisor of supervisors)

役割:
    dead_man_switch.py を監視する独立プロセス。
    dead_man_switch 自身が停止 / ハング / heartbeat 途絶した場合に
    launchctl 経由で再起動し、3 連続失敗時は Pushover P1 + KILL_SWITCH 発動。

設計原則:
    - dead_man_switch.py とは完全独立のプロセス空間で動作する
    - launchd KeepAlive=true でコンテナ自身も常駐
    - 外部依存は pushover_client と kill_switch のみ（I/O は heartbeat JSONL）
    - sudo 不要: launchctl kickstart は同一 user session 内なら sudo 不要

監視ロジック:
    1. pgrep で dead_man_switch プロセスが存在するか確認
    2. heartbeat file の mtime が HEARTBEAT_STALE_SEC (180s) 以内か確認
    3. heartbeat JSONL の直近 5 分以内のレコードが MIN_BEATS_IN_5MIN (3) 件以上か確認
    4. 問題検出 → launchctl kickstart -k com.soralab.dead-man-switch で再起動
    5. 3 連続失敗 → Pushover P1 + KILL_SWITCH activate

定数:
    CHECK_INTERVAL_SEC = 30     # チェック間隔
    HEARTBEAT_STALE_SEC = 180   # heartbeat mtime 鮮度 (3 分)
    MIN_BEATS_IN_5MIN = 3       # 直近 5 分の最低 beacon 件数
    MAX_CONSECUTIVE_FAILURES = 3  # P1 + KILL_SWITCH 発動閾値
    SENTINEL_HEARTBEAT_INTERVAL = 30  # Sentinel 自身の heartbeat 書き込み間隔

LaunchAgent: com.soralab.sentinel-watchdog (KeepAlive=true)
呼び出し: python3 scripts/sentinel_watchdog.py [--once | --daemon]
    --once   : 1 回だけチェックして終了 (テスト・手動確認用)
    --daemon : 連続ループ (LaunchAgent から呼ぶ)
    引数なし : --daemon と同等
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ── パス設定 ──────────────────────────────────────────────────────────────────
_TRADING_DIR = Path(os.environ.get("SORA_TRADING_DIR", _PROJECT_ROOT))

# dead_man_switch の heartbeat 先 (dead_man_switch.py と同じパスを参照)
PING_DIR = _TRADING_DIR / "data" / "ops" / "heartbeat"
PING_FILE = PING_DIR / "dead_man_ping.jsonl"

# Sentinel 自身の heartbeat ファイル (atlas_v3/supervision/self_monitor.py が参照する)
SENTINEL_HEARTBEAT_FILE = _TRADING_DIR / "data" / "ops" / "heartbeat" / "sentinel_heartbeat.jsonl"

# launchd 再起動ログ
LOG_DIR = _TRADING_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── 閾値 ──────────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SEC: int = int(os.environ.get("SENTINEL_CHECK_INTERVAL", "30"))
HEARTBEAT_STALE_SEC: int = int(os.environ.get("SENTINEL_HB_STALE_SEC", "180"))
MIN_BEATS_IN_5MIN: int = int(os.environ.get("SENTINEL_MIN_BEATS", "3"))
MAX_CONSECUTIVE_FAILURES: int = int(os.environ.get("SENTINEL_MAX_FAILURES", "3"))
SENTINEL_HEARTBEAT_INTERVAL: int = int(os.environ.get("SENTINEL_HB_INTERVAL", "30"))

# launchd job ラベル (dead_man_switch の job)
DMS_LAUNCHD_LABEL: str = os.environ.get(
    "SENTINEL_DMS_LABEL", "com.soralab.dead-man-switch"
)
# dead_man_switch のプロセス名 (pgrep 検索キーワード)
DMS_PROCESS_KEYWORD: str = "dead_man_switch"

# ── ロガー ───────────────────────────────────────────────────────────────────
log = logging.getLogger("sentinel_watchdog")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] sentinel_watchdog: %(message)s")
    )
    log.addHandler(_h)
    log.setLevel(logging.INFO)

_fh = logging.FileHandler(LOG_DIR / "sentinel_watchdog.log")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)


# ─────────────────────────────────────────────────────────────────────────────
# 内部状態 (プロセス内ステート)
# ─────────────────────────────────────────────────────────────────────────────
_consecutive_failures: int = 0
_last_sentinel_hb_write: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Sentinel 自身の heartbeat 書き込み
# ─────────────────────────────────────────────────────────────────────────────

def write_sentinel_heartbeat() -> None:
    """Sentinel が生存していることを JSONL に記録する。

    atlas_v3/supervision/self_monitor.py がこのファイルの mtime を監視する。
    """
    global _last_sentinel_hb_write
    now = time.monotonic()
    if now - _last_sentinel_hb_write < SENTINEL_HEARTBEAT_INTERVAL:
        return
    PING_DIR.mkdir(parents=True, exist_ok=True)
    ts_iso = datetime.now(timezone.utc).isoformat()
    record = {"ts": ts_iso, "component": "sentinel_watchdog", "status": "alive"}
    try:
        with SENTINEL_HEARTBEAT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _last_sentinel_hb_write = now
        log.debug("sentinel heartbeat written ts=%s", ts_iso)
    except OSError as exc:
        log.warning("sentinel heartbeat write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# dead_man_switch 死活チェック
# ─────────────────────────────────────────────────────────────────────────────

def _is_dms_process_alive() -> bool:
    """pgrep で dead_man_switch プロセスが存在するか確認する。

    dead_man_switch.py は LaunchAgent から python3 として起動されるため
    プロセス名ではなく引数文字列で検索する。
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", DMS_PROCESS_KEYWORD],
            capture_output=True,
            timeout=5,
        )
        # pgrep 自身を除外 (sentinel_watchdog が DMS_PROCESS_KEYWORD を含む場合)
        if result.returncode == 0:
            pids = result.stdout.decode().strip().split()
            own_pid = str(os.getpid())
            # sentinel 自身の pid を除外した pid が残れば alive
            other_pids = [p for p in pids if p != own_pid]
            return len(other_pids) > 0
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


def _is_dms_heartbeat_fresh() -> bool:
    """dead_man_switch の heartbeat JSONL の mtime が HEARTBEAT_STALE_SEC 以内か確認する。"""
    if not PING_FILE.exists():
        return False
    try:
        mtime = PING_FILE.stat().st_mtime
        age_sec = time.time() - mtime
        return age_sec < HEARTBEAT_STALE_SEC
    except OSError:
        return False


def _count_dms_beats_in_window(window_sec: int = 300) -> int:
    """PING_FILE の直近 window_sec 秒以内の dead_man_switch beacon レコード数を返す。"""
    if not PING_FILE.exists():
        return 0
    cutoff = time.time() - window_sec
    count = 0
    try:
        lines = PING_FILE.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("component") != "dead_man_switch":
                    continue
                ts = datetime.fromisoformat(rec["ts"]).timestamp()
                if ts >= cutoff:
                    count += 1
                else:
                    # 時系列逆順なので、これより古い行はすべて対象外
                    break
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    except OSError:
        pass
    return count


def check_dms_health() -> tuple[bool, str]:
    """dead_man_switch の総合健全性チェック。

    dead_man_switch はワンショット設計（実行して終了する）のため、
    プロセスが「今走っているか」ではなく「最近正常に実行された痕跡があるか」で
    判断する。判定基準は heartbeat mtime の鮮度 + 直近ウィンドウの beacon 件数。
    proc チェックは補助情報として reason に含めるが healthy 判定には使わない。

    Returns:
        (healthy: bool, reason: str)
    """
    proc_alive = _is_dms_process_alive()
    hb_fresh = _is_dms_heartbeat_fresh()
    beat_count = _count_dms_beats_in_window(300)
    beats_ok = beat_count >= MIN_BEATS_IN_5MIN

    # ワンショット設計: heartbeat が新鮮 かつ beats が十分なら正常
    if hb_fresh and beats_ok:
        proc_note = "proc=alive" if proc_alive else "proc=idle(oneshot)"
        return True, f"OK ({proc_note}, mtime_fresh, beats_5m={beat_count})"

    reasons = []
    if not proc_alive:
        reasons.append("proc=DEAD")
    if not hb_fresh:
        reasons.append("heartbeat=STALE")
    if not beats_ok:
        reasons.append(f"beats_5m={beat_count}<{MIN_BEATS_IN_5MIN}")
    return False, " | ".join(reasons)


# ─────────────────────────────────────────────────────────────────────────────
# dead_man_switch 再起動
# ─────────────────────────────────────────────────────────────────────────────

def restart_dms() -> bool:
    """launchctl kickstart -k でdead_man_switch を再起動する。

    sudo 不要: 同一 user の launchd domain (gui/<uid>) 内なら権限なしで実行できる。

    Returns:
        True: 再起動コマンド発行成功
        False: コマンド失敗 or タイムアウト
    """
    uid = os.getuid()
    target = f"gui/{uid}/{DMS_LAUNCHD_LABEL}"
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            log.info("restart_dms: launchctl kickstart OK target=%s", target)
            return True
        log.warning(
            "restart_dms: launchctl kickstart FAILED rc=%d stderr=%s",
            result.returncode,
            result.stderr.strip(),
        )
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("restart_dms: exception %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Pushover / KILL_SWITCH
# ─────────────────────────────────────────────────────────────────────────────

def _send_p1_alert(title: str, msg: str) -> None:
    """Pushover P1 を送信する。送信失敗はログのみで握り潰さない (fallback log)。"""
    log.error("SENTINEL P1 ALERT: %s | %s", title, msg)
    # fallback log (Pushover 失敗時も残す)
    fallback = LOG_DIR / "sentinel_fallback.log"
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with fallback.open("a", encoding="utf-8") as f:
            f.write(f"{ts} | {title} | {msg}\n")
    except OSError:
        pass
    try:
        from common.pushover_client import send as pushover_send  # noqa: PLC0415
        pushover_send(title, msg, priority=1)
    except Exception as exc:  # noqa: BLE001
        log.error("Pushover P1 send failed: %s", exc)


def _activate_kill_switch(reason: str) -> None:
    """KILL_SWITCH を発動する。"""
    log.critical("SENTINEL: activating KILL_SWITCH reason=%s", reason)
    try:
        from common.kill_switch import activate  # noqa: PLC0415
        activate(reason=f"sentinel_watchdog: {reason}")
    except Exception as exc:  # noqa: BLE001
        log.error("KILL_SWITCH activate failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# メインチェックサイクル
# ─────────────────────────────────────────────────────────────────────────────

def run_check_cycle() -> None:
    """1 回のチェックサイクルを実行する。

    - 正常: consecutive_failures をリセット
    - 異常: restart_dms() → consecutive_failures 加算
    - MAX_CONSECUTIVE_FAILURES 到達: P1 + KILL_SWITCH
    """
    global _consecutive_failures

    write_sentinel_heartbeat()

    healthy, reason = check_dms_health()
    if healthy:
        if _consecutive_failures > 0:
            log.info("dms recovered after %d failures", _consecutive_failures)
        _consecutive_failures = 0
        log.debug("dms health OK: %s", reason)
        return

    # 異常検知
    _consecutive_failures += 1
    log.warning(
        "dms unhealthy (consecutive=%d): %s", _consecutive_failures, reason
    )

    # 再起動試行
    restarted = restart_dms()
    log.info("restart_dms result=%s", restarted)

    if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        msg = (
            f"dead_man_switch が {_consecutive_failures} 回連続で不健全です。"
            f" 理由: {reason}"
            f" 再起動試行: {restarted}"
            " KILL_SWITCH を発動しました。"
        )
        _send_p1_alert("[SENTINEL] dead_man_switch 連続失敗 KILL_SWITCH 発動", msg)
        _activate_kill_switch(f"consecutive_failures={_consecutive_failures} reason={reason}")


# ─────────────────────────────────────────────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Watchdog — supervisor of supervisors")
    parser.add_argument(
        "--once",
        action="store_true",
        help="1 回だけチェックして終了 (テスト用)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="連続ループ (LaunchAgent から使用)",
    )
    args = parser.parse_args()

    if args.once:
        run_check_cycle()
        return

    # --daemon または引数なし: 連続ループ
    log.info(
        "sentinel_watchdog starting daemon loop"
        " interval=%ds stale=%ds min_beats=%d max_failures=%d",
        CHECK_INTERVAL_SEC,
        HEARTBEAT_STALE_SEC,
        MIN_BEATS_IN_5MIN,
        MAX_CONSECUTIVE_FAILURES,
    )
    while True:
        try:
            run_check_cycle()
        except Exception as exc:  # noqa: BLE001
            log.error("run_check_cycle unexpected error: %s", exc, exc_info=True)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
