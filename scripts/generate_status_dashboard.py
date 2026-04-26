#!/usr/bin/env python3
"""
scripts/generate_status_dashboard.py — Unified Status Dashboard 生成

5分毎 (com.soralab.status_dashboard.plist) に呼ばれ、
data/ops/unified_status.md を更新する。

表示内容:
  - 全 bot/monitor の heartbeat + log 状態
  - Pushover ban 状態
  - 直近 auto-remediation 5件
  - dead_man_switch 最新 ping
  - 直近 rescue_tracker 3件
  - 直近 ground_truth_reconciler 状態
  - 赤黄青 priority 色分け

Usage:
  python3 scripts/generate_status_dashboard.py   # 即時実行
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

def _load_env() -> None:
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

_TRADING_DIR  = Path(os.environ.get("SORA_TRADING_DIR", _PROJECT_ROOT))
OPS_DIR       = _TRADING_DIR / "data" / "ops"
HEARTBEAT_DIR = _TRADING_DIR / "data" / "heartbeats"
DASHBOARD_PATH = OPS_DIR / "unified_status.md"
REMEDIATION_LOG = OPS_DIR / "remediation" / "auto_remediation_log.jsonl"

OPS_DIR.mkdir(parents=True, exist_ok=True)


# ── コンポーネント定義 ────────────────────────────────────────────────────────
_COMPONENTS = [
    # Atlas系
    {"name": "atlas_agent",      "hb": "atlas_agent.json",      "log": _TRADING_DIR / "data/logs/atlas_agent.log",      "tier": "Atlas"},
    {"name": "atlas_watchdog",   "hb": "atlas_watchdog.json",   "log": _TRADING_DIR / "data/logs/atlas_watchdog.log",   "tier": "Atlas"},
    # Chronos系
    {"name": "chronos_agent",    "hb": "chronos_agent.json",    "log": _TRADING_DIR / "logs/chronos_agent.log",         "tier": "Chronos"},
    {"name": "chronos_watchdog", "hb": "chronos_watchdog.json", "log": _TRADING_DIR / "logs/mffu_bot.log",              "tier": "Chronos"},
    # 共通インフラ
    {"name": "heartbeat_monitor","hb": None,                    "log": _TRADING_DIR / "logs/sora_heartbeat_monitor.log","tier": "Infra"},
    {"name": "dead_man_switch",  "hb": None,                    "log": _TRADING_DIR / "logs/dead_man_switch.log",       "tier": "Infra"},
    {"name": "ground_truth",     "hb": None,                    "log": _TRADING_DIR / "logs/ground_truth_reconciler.log","tier":"Infra"},
    {"name": "failure_rescue",   "hb": None,                    "log": _TRADING_DIR / "logs/failure_to_rescue.log",     "tier": "Infra"},
    {"name": "autonomous_sentinel","hb":None,                   "log": _TRADING_DIR / "logs/autonomous_sentinel.log",   "tier": "Infra"},
]

_STALE_HB_SEC  = 300   # 5分
_STALE_LOG_SEC = 600   # 10分


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hb_status(hb_file: Optional[str]) -> tuple[str, Optional[float], Optional[int]]:
    """(status, age_sec, pid)"""
    if not hb_file:
        return "no_hb", None, None
    path = HEARTBEAT_DIR / hb_file
    if not path.exists():
        return "missing", None, None
    try:
        data = json.loads(path.read_text())
        ts = datetime.fromisoformat(data.get("ts", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (_now_utc() - ts).total_seconds()
        pid = data.get("pid")
        status = "ok" if age < _STALE_HB_SEC else "stale"
        return status, age, pid
    except Exception:
        return "error", None, None


def _log_status(log_path: Optional[Path]) -> tuple[str, Optional[float]]:
    """(status, age_sec)"""
    if log_path is None or not Path(str(log_path)).exists():
        return "no_log", None
    age = time.time() - Path(str(log_path)).stat().st_mtime
    status = "ok" if age < _STALE_LOG_SEC else "stale"
    return status, age


def _age_str(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec:.0f}秒"
    if sec < 3600:
        return f"{sec/60:.0f}分"
    return f"{sec/3600:.1f}時間"


def _priority_icon(hb_st: str, log_st: str) -> str:
    if "missing" in (hb_st, log_st) or hb_st == "error":
        return "🔴"
    if "stale" in (hb_st, log_st):
        return "🟡"
    return "🔵"


def build_dashboard() -> str:
    now_jst = datetime.now(timezone(timedelta(hours=9)))
    lines: list[str] = [
        "# Sora Lab Unified Status Dashboard",
        f"更新: {now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}",
        "",
    ]

    # ── Bot/Monitor 状態テーブル ────────────────────────────────────────────
    lines += [
        "## コンポーネント状態",
        "| 優先 | Tier | コンポーネント | HB状態 | HB経過 | LOG状態 | LOG経過 | PID |",
        "|------|------|--------------|--------|--------|---------|---------|-----|",
    ]
    for comp in _COMPONENTS:
        hb_st, hb_age, pid = _hb_status(comp["hb"])
        log_st, log_age    = _log_status(comp["log"])
        icon  = _priority_icon(hb_st, log_st)
        lines.append(
            f"| {icon} | {comp['tier']} | {comp['name']} "
            f"| {hb_st} | {_age_str(hb_age)} "
            f"| {log_st} | {_age_str(log_age)} "
            f"| {pid or '—'} |"
        )

    # ── Pushover 状態 ───────────────────────────────────────────────────────
    lines += ["", "## Pushover 状態"]
    pov_state = _TRADING_DIR / "data" / "pushover_client_state.json"
    if pov_state.exists():
        try:
            ps = json.loads(pov_state.read_text())
            backoff_until = ps.get("backoff_until", 0.0)
            c429 = ps.get("consecutive_429", 0)
            queue_path = _TRADING_DIR / "data" / "pushover_client_queue.jsonl"
            queue_count = 0
            if queue_path.exists():
                queue_count = sum(1 for l in queue_path.read_text().splitlines() if l.strip())
            if backoff_until > time.time():
                remain = int(backoff_until - time.time())
                lines.append(f"- 🔴 **BAN中** あと{remain}秒 | consecutive_429={c429} | queue={queue_count}件")
            else:
                lines.append(f"- 🔵 正常 | consecutive_429={c429} | queue={queue_count}件")
        except Exception as e:
            lines.append(f"- 🟡 読み込みエラー: {e}")
    else:
        lines.append("- 🟡 状態ファイルなし")

    # ── 直近 Auto-Remediation ───────────────────────────────────────────────
    lines += ["", "## 直近 Auto-Remediation (最新5件)"]
    if REMEDIATION_LOG.exists():
        rows = [
            json.loads(l) for l in REMEDIATION_LOG.read_text().splitlines() if l.strip()
        ][-5:]
        if rows:
            for r in reversed(rows):
                ts_str = r.get("ts", "")[:19]
                icon   = "✅" if r.get("result") == "ok" else "❌"
                lines.append(f"- `{ts_str}` {icon} {r.get('action')} → **{r.get('target')}** ({r.get('details','')})")
        else:
            lines.append("- ログなし")
    else:
        lines.append("- ログファイルなし")

    # ── Dead Man's Switch ───────────────────────────────────────────────────
    lines += ["", "## Dead Man's Switch (直近3件)"]
    dmp = _TRADING_DIR / "data" / "ops" / "heartbeat" / "dead_man_ping.jsonl"
    if dmp.exists():
        rows = [json.loads(l) for l in dmp.read_text().splitlines() if l.strip()][-3:]
        if rows:
            for r in reversed(rows):
                lines.append(f"- `{r.get('ts','')[:19]}` {r.get('component','?')}")
        else:
            lines.append("- pingなし")
    else:
        lines.append("- ファイルなし")

    # ── Rescue Tracker ──────────────────────────────────────────────────────
    lines += ["", "## Failure-to-Rescue 直近3件"]
    rt = _TRADING_DIR / "data" / "ops" / "rescue_tracker.jsonl"
    if rt.exists():
        rows = [json.loads(l) for l in rt.read_text().splitlines() if l.strip()][-3:]
        if rows:
            for r in reversed(rows):
                resolved = "✅" if r.get("resolved_at") else "⏳"
                lines.append(
                    f"- {resolved} `{r.get('detected_at','')[:19]}` "
                    f"{r.get('anomaly_id','?')}: {r.get('message','')[:50]}"
                )
        else:
            lines.append("- なし")
    else:
        lines.append("- ファイルなし")

    # ── Escalation Flag ─────────────────────────────────────────────────────
    flag = OPS_DIR / "ESCALATION_PENDING.flag"
    lines += ["", "## Escalation Flag"]
    if flag.exists():
        try:
            fp = json.loads(flag.read_text())
            lines.append(f"- 🔴 **PENDING** `{fp.get('ts','')[:19]}`: {fp.get('title','')}")
        except Exception:
            lines.append("- 🔴 flag 存在 (読み込みエラー)")
    else:
        lines.append("- 🔵 なし")

    lines += [
        "",
        "---",
        f"*自動生成 by `scripts/generate_status_dashboard.py`*",
        f"*sentinel: `scripts/autonomous_sentinel.py` 30分毎*",
        f"*閲覧: `cat ~/trading/data/ops/unified_status.md`*",
    ]
    return "\n".join(lines)


def main() -> None:
    dashboard = build_dashboard()
    DASHBOARD_PATH.write_text(dashboard, encoding="utf-8")
    print(f"Dashboard written ({len(dashboard)} chars): {DASHBOARD_PATH}")


if __name__ == "__main__":
    main()
