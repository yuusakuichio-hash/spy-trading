#!/usr/bin/env python3
"""
sora_heartbeat_monitor.py — Sora Lab 能動 Heartbeat 監視デーモン

2分毎に全 heartbeat ファイルをチェックし、stale（最終更新から2分以上経過）を
検知したら対応アクション（Pushover通知 + launchctl 再起動試行）を実行する。

対応アクション:
  1. stale 検知 → Pushover 通知（priority=1）
  2. launchctl kickstart で該当コンポーネント再起動試行
  3. 3回失敗で priority=2 エマージェンシー通知

LaunchAgent: com.sora.heartbeat_monitor.plist
  - 常駐デーモン（KeepAlive=true）
  - StartInterval なし（プロセス内でループ）
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# プロジェクトルートを sys.path に追加
_TRADING_DIR = Path(__file__).parent
sys.path.insert(0, str(_TRADING_DIR))

from common.heartbeat import STALE_THRESHOLD_SEC, is_stale, list_components

# ----------------------------------------------------------------
# Pushover クライアント（共通実装があれば優先、なければ直接 requests）
# ----------------------------------------------------------------
try:
    from common.pushover_client import send as _pushover_send  # type: ignore[import]

    def pushover(title: str, message: str, priority: int = 0) -> None:
        _pushover_send(title=title, message=message, priority=priority)

except ImportError:
    import requests

    PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "u2cevk8nktib3sr148rw2hs78ecvux")
    PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "aj9f1fk3ae2o6azif17kjyn698remc")

    def pushover(title: str, message: str, priority: int = 0) -> None:  # type: ignore[misc]
        try:
            requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": PUSHOVER_TOKEN,
                    "user": PUSHOVER_USER,
                    "title": title,
                    "message": message,
                    "priority": priority,
                },
                timeout=10,
            )
        except Exception as exc:
            log.error("[PUSHOVER_FAIL] %s", exc)


# ----------------------------------------------------------------
# ロギング設定
# ----------------------------------------------------------------
LOG_DIR = _TRADING_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "heartbeat_monitor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("heartbeat_monitor")

# ----------------------------------------------------------------
# 定数
# ----------------------------------------------------------------
CHECK_INTERVAL_SEC: int = 120  # 2分毎チェック

# コンポーネント → LaunchAgent ラベルのマッピング
COMPONENT_LAUNCHD_LABEL: dict[str, str] = {
    "chronos_agent": "com.soralab.chronos_agent",
    "atlas_agent": "com.soralab.atlas_agent",
    "chronos_watchdog": "com.soralab.chronos_watchdog",
    "atlas_watchdog": "com.soralab.atlas_watchdog",
}

# 再起動試行回数の上限（3回超えで emergency）
MAX_RESTART_ATTEMPTS: int = 3

# 試行回数を追跡: component → count
_restart_attempts: dict[str, int] = {}

# 既に emergency 通知済みのコンポーネント（重複抑制）
_emergency_notified: set[str] = set()


# ----------------------------------------------------------------
# コンポーネント再起動
# ----------------------------------------------------------------
def _kickstart(component: str) -> bool:
    """launchctl kickstart で再起動を試みる。

    Returns
    -------
    bool
        True = 成功（exit 0）, False = 失敗
    """
    label = COMPONENT_LAUNCHD_LABEL.get(component)
    if not label:
        log.warning("[KICKSTART] unknown component=%s, no launchd label", component)
        return False

    cmd = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
    log.info("[KICKSTART] cmd=%s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            log.info("[KICKSTART] success: component=%s", component)
            return True
        else:
            log.error(
                "[KICKSTART] failed: component=%s, rc=%d, stderr=%s",
                component,
                result.returncode,
                result.stderr.strip(),
            )
            return False
    except subprocess.TimeoutExpired:
        log.error("[KICKSTART] timeout: component=%s", component)
        return False
    except Exception as exc:
        log.error("[KICKSTART] exception: component=%s, err=%s", component, exc)
        return False


# ----------------------------------------------------------------
# stale 検知 → アクション
# ----------------------------------------------------------------
def handle_stale(component: str, age_sec: float) -> None:
    """stale コンポーネントへの対応アクション。

    1. Pushover 通知（priority=1）
    2. launchctl kickstart 試行
    3. 3回失敗で priority=2 エマージェンシー通知
    """
    age_str = f"{age_sec:.0f}s" if age_sec != float("inf") else "∞ (ファイルなし)"
    log.warning("[STALE] component=%s age=%s", component, age_str)

    # エマージェンシー通知済みはスキップ（過剰通知抑制）
    if component in _emergency_notified:
        log.info("[STALE] emergency already notified for %s, skip", component)
        return

    # 初回 stale 検知通知
    pushover(
        title=f"[SYS] Heartbeat STALE: {component}",
        message=f"コンポーネント {component} のハートビートが停止しています\n経過: {age_str}\n再起動を試みます...",
        priority=1,
    )

    # 再起動試行
    _restart_attempts.setdefault(component, 0)
    _restart_attempts[component] += 1
    attempt = _restart_attempts[component]

    if attempt > MAX_RESTART_ATTEMPTS:
        # 3回超え → emergency
        log.error("[EMERGENCY] component=%s exceeded max restart attempts=%d", component, MAX_RESTART_ATTEMPTS)
        pushover(
            title=f"[SYS] EMERGENCY: {component} restart FAILED",
            message=(
                f"コンポーネント {component} が {MAX_RESTART_ATTEMPTS} 回再起動失敗しました。\n"
                f"手動介入が必要です。\n経過時間: {age_str}"
            ),
            priority=2,
        )
        _emergency_notified.add(component)
        return

    log.info("[RESTART] attempting kickstart: component=%s (attempt=%d/%d)", component, attempt, MAX_RESTART_ATTEMPTS)
    success = _kickstart(component)

    if success:
        log.info("[RESTART] success: component=%s (attempt=%d)", component, attempt)
        # 成功したらカウンタをリセット
        _restart_attempts[component] = 0
    else:
        log.warning("[RESTART] failed: component=%s (attempt=%d/%d)", component, attempt, MAX_RESTART_ATTEMPTS)
        if attempt >= MAX_RESTART_ATTEMPTS:
            pushover(
                title=f"[SYS] EMERGENCY: {component} restart FAILED",
                message=(
                    f"コンポーネント {component} が {MAX_RESTART_ATTEMPTS} 回再起動失敗しました。\n"
                    f"手動介入が必要です。\n経過時間: {age_str}"
                ),
                priority=2,
            )
            _emergency_notified.add(component)


# ----------------------------------------------------------------
# 既知コンポーネント一覧（heartbeat 登録済み + 設定済み）
# ----------------------------------------------------------------
def _monitored_components() -> list[str]:
    """監視対象コンポーネントのリストを返す。

    heartbeat ファイルが存在するものに加え、COMPONENT_LAUNCHD_LABEL に
    登録されているコンポーネントも含める（未 pulse でも stale 扱い）。
    """
    from_files = set(list_components())
    from_config = set(COMPONENT_LAUNCHD_LABEL.keys())
    return sorted(from_files | from_config)


# ----------------------------------------------------------------
# メイン監視ループ
# ----------------------------------------------------------------
def run_monitor() -> None:
    log.info("[HeartbeatMonitor] 起動: check_interval=%ds, stale_threshold=%ds", CHECK_INTERVAL_SEC, STALE_THRESHOLD_SEC)
    pushover(
        title="[SYS] HeartbeatMonitor 起動",
        message=f"Sora Lab 能動監視デーモン開始\n監視間隔: {CHECK_INTERVAL_SEC}秒\nStale閾値: {STALE_THRESHOLD_SEC}秒",
        priority=0,
    )

    while True:
        try:
            components = _monitored_components()
            stale_list: list[tuple[str, float]] = []

            for comp in components:
                stale, age_sec = is_stale(comp)
                if stale:
                    stale_list.append((comp, age_sec))
                else:
                    # stale でなければカウンタリセット（回復）
                    if comp in _restart_attempts and _restart_attempts[comp] > 0:
                        log.info("[RECOVER] component=%s recovered (age=%.0fs)", comp, age_sec)
                        _restart_attempts[comp] = 0
                        _emergency_notified.discard(comp)

            if stale_list:
                log.warning("[MONITOR] stale_components=%d: %s", len(stale_list), [c for c, _ in stale_list])
                for comp, age_sec in stale_list:
                    handle_stale(comp, age_sec)
            else:
                log.info("[MONITOR] all_healthy: components=%s", components)

        except Exception as exc:
            log.error("[MONITOR_ERR] %s", exc, exc_info=True)

        time.sleep(CHECK_INTERVAL_SEC)


def main() -> None:
    run_monitor()


if __name__ == "__main__":
    main()
