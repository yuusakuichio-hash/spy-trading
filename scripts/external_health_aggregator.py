#!/usr/bin/env python3
"""
scripts/external_health_aggregator.py — 外部死活監視 集約 ping スクリプト

役割:
    LaunchAgent (com.sora.external_health_check) から 10分毎に呼ばれる。
    全コンポーネントの heartbeat ファイルを確認し、
    Healthchecks.io の health_aggregator チェックに成功/失敗を報告する。

    health_aggregator ping が届かない場合:
      UptimeRobot / Healthchecks.io → Email/SMS で通知
      → Pushover が完全死亡していても外部から検知できる

設定:
    .env に HC_UUID_HEALTH_AGGREGATOR=<UUID> が必要
    docs/external_monitoring_setup.md 参照

対象コンポーネント:
    Chronos系 (CME先物Bot): chronos_agent / chronos_watchdog
    Atlas系 (SPXオプションBot): atlas_agent / atlas_watchdog
    共通: sora_heartbeat_monitor
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from common.external_health_ping import ping_healthchecks

# heartbeat ファイルディレクトリ（common/heartbeat.py と同じ）
_TRADING_DIR = Path(os.environ.get("SORA_TRADING_DIR", _PROJECT_ROOT))
_HEARTBEAT_DIR = _TRADING_DIR / "data" / "heartbeats"

# stale 閾値（秒）: 3分以上更新なし = stale
_STALE_SEC = 180

# 監視対象コンポーネント（Atlas/Chronos 混同防止のためコメント明示）
_COMPONENTS = [
    # Chronos: CME先物Bot 系（Atlas と混同禁止）
    "chronos_agent",
    "chronos_watchdog",
    # Atlas: SPXオプションBot 系（Chronos と混同禁止）
    "atlas_agent",
    "atlas_watchdog",
    # 共通監視インフラ
    "sora_heartbeat_monitor",
]


def check_component(comp: str) -> tuple[bool, float]:
    """コンポーネントの heartbeat ファイルが stale かどうかを確認する。

    Returns
    -------
    (is_stale, age_sec)
    """
    hb_file = _HEARTBEAT_DIR / f"{comp}.json"
    if not hb_file.exists():
        return True, float("inf")
    age_sec = time.time() - hb_file.stat().st_mtime
    return age_sec >= _STALE_SEC, age_sec


def main() -> int:
    stale_list: list[tuple[str, float]] = []
    healthy_list: list[str] = []

    for comp in _COMPONENTS:
        is_stale, age_sec = check_component(comp)
        if is_stale:
            age_str = f"{age_sec:.0f}s" if age_sec != float("inf") else "inf"
            stale_list.append((comp, age_sec))
            print(f"[STALE] {comp}: age={age_str}", flush=True)
        else:
            healthy_list.append(comp)
            print(f"[OK]    {comp}: age={age_sec:.0f}s", flush=True)

    if stale_list:
        # 1件でも stale → fail で報告
        payload_lines = ["stale components:"]
        for comp, age_sec in stale_list:
            age_str = f"{age_sec:.0f}s" if age_sec != float("inf") else "inf (no file)"
            payload_lines.append(f"  - {comp}: {age_str}")
        payload = "\n".join(payload_lines)
        print(f"\n[AGGREGATOR] FAIL: {len(stale_list)} stale components", flush=True)
        ping_healthchecks("health_aggregator", status="fail", payload=payload)
        return 1
    else:
        print(f"\n[AGGREGATOR] OK: all {len(healthy_list)} components healthy", flush=True)
        ping_healthchecks("health_aggregator", status="success")
        return 0


if __name__ == "__main__":
    sys.exit(main())
