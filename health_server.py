#!/usr/bin/env python3
"""
Lightweight HTTP health server — Sora Lab 全Bot 対応 (port 8080).

対応Bot:
  - Atlas: SPXオプションBot (atlas_agent / atlas_watchdog / spy_bot)
  - Chronos: CME先物Bot (chronos_agent / chronos_watchdog / chronos_bot)
  - sora_heartbeat_monitor: 内部heartbeat監視デーモン

エンドポイント:
  GET /health  → JSON {"status": "healthy|degraded|critical", "components": {...}, "ts": "..."}
  GET /healthz → 同上（UptimeRobot 互換）
  GET /        → 同上

UptimeRobot 設定:
  - Monitor Type: HTTP(s)
  - URL: http://<VPS IP>:8080/health
  - 期待HTTP status: 200 = healthy / 503 = degraded
  - 5分毎監視推奨

設計:
  - Pushover と完全独立（別認証・別ネットワーク経路・別ベンダー）
  - Tier 1 主監視（UptimeRobot がここを見る）
  - heartbeat ファイル（data/heartbeats/）を参照して各Botの生死を判定
"""

import json
import subprocess
import datetime
import os
import time
import http.server
import socketserver
import logging
import sys
from pathlib import Path

PORT = int(os.environ.get("HEALTH_SERVER_PORT", "8080"))
_START_TIME = time.time()

# ローカル実行（Mac）と VPS（Linux）で log ディレクトリを分ける
# 環境変数 HEALTH_SERVER_LOG_DIR が設定されていればそちらを優先
# VPS では /var/log/spx_bot（root権限で作成済み）、Mac では data/logs
_default_log_dir = (
    "/var/log/spx_bot"
    if os.path.isdir("/var/log/spx_bot") or (sys.platform == "linux" and os.getuid() == 0)
    else str(Path(__file__).parent / "data" / "logs")
)
LOG_DIR = Path(os.environ.get("HEALTH_SERVER_LOG_DIR", _default_log_dir))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "health.log"), logging.StreamHandler()],
)
log = logging.getLogger("health_server")

# VPS 上の systemd サービス名（VPS デプロイ後に確認・調整すること）
SERVICES = ["spxbot", "opend", "xvfb"]

# heartbeat ファイルを格納するディレクトリ（common/heartbeat.py と同じパス）
_TRADING_DIR = Path(__file__).parent
_HEARTBEAT_DIR = _TRADING_DIR / "data" / "heartbeats"

# heartbeat stale 閾値（秒）
_STALE_SEC = 180  # 3分以上更新なし = stale

# コンポーネント一覧（Atlas/Chronos 混同防止のためコメント明示）
_COMPONENTS = {
    # Atlas: SPXオプションBot 系
    "atlas_agent":            "Atlas",
    "atlas_watchdog":         "Atlas",
    "spy_bot":                "Atlas",
    # Chronos: CME先物Bot 系
    "chronos_agent":          "Chronos",
    "chronos_watchdog":       "Chronos",
    "chronos_bot":            "Chronos",
    # 共通監視インフラ
    "sora_heartbeat_monitor": "SYS",
}


def get_service_status(name: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def get_memory_mb() -> dict:
    """Return used/total memory in MB."""
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0) // 1024
        free = info.get("MemAvailable", 0) // 1024
        used = total - free
        return {"total_mb": total, "used_mb": used, "free_mb": free}
    except Exception:
        return {"total_mb": 0, "used_mb": 0, "free_mb": 0}


def get_disk_usage_pct() -> float:
    try:
        result = subprocess.run(
            ["df", "-h", "/"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                return float(parts[4].rstrip("%"))
    except Exception:
        pass
    return -1.0


def get_last_bot_log_line() -> str:
    log_file = LOG_DIR / "bot.log"
    if not log_file.exists():
        return ""
    try:
        result = subprocess.run(
            ["tail", "-1", str(log_file)],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()[-200:]
    except Exception:
        return ""


def get_heartbeat_status() -> dict[str, dict]:
    """data/heartbeats/ を参照して全コンポーネントの生死を返す。

    Returns
    -------
    dict[component_name, {"state": str, "age_sec": float, "stale": bool, "bot": str}]
    """
    result: dict[str, dict] = {}
    now = time.time()

    for comp, bot_label in _COMPONENTS.items():
        hb_file = _HEARTBEAT_DIR / f"{comp}.json"
        if not hb_file.exists():
            result[comp] = {
                "state": "unknown",
                "age_sec": float("inf"),
                "stale": True,
                "bot": bot_label,
                "ts": None,
            }
            continue
        try:
            data = json.loads(hb_file.read_text())
            mtime = hb_file.stat().st_mtime
            age_sec = now - mtime
            result[comp] = {
                "state": data.get("state", "unknown"),
                "age_sec": round(age_sec, 1),
                "stale": age_sec >= _STALE_SEC,
                "bot": bot_label,
                "ts": data.get("ts"),
            }
        except Exception as e:
            result[comp] = {
                "state": "error",
                "age_sec": float("inf"),
                "stale": True,
                "bot": bot_label,
                "ts": None,
                "error": str(e),
            }
    return result


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access logs

    def do_GET(self):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return

        services = {s: get_service_status(s) for s in SERVICES}
        mem = get_memory_mb()
        disk_pct = get_disk_usage_pct()
        components = get_heartbeat_status()

        # 全コンポーネントが healthy かどうか判定
        stale_comps = [k for k, v in components.items() if v["stale"]]
        mem_warn = mem["total_mb"] > 0 and (mem["used_mb"] / mem["total_mb"]) > 0.90

        if stale_comps and mem_warn:
            overall = "critical"
            status_code = 503
        elif stale_comps or mem_warn:
            overall = "degraded"
            status_code = 503
        else:
            overall = "healthy"
            status_code = 200

        payload = {
            "status": overall,
            "uptime": int(time.time() - _START_TIME),
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "components": components,
            "stale_components": stale_comps,
            "services": services,
            "memory": mem,
            "disk_pct": disk_pct,
            "last_log": get_last_bot_log_line(),
        }

        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    with ReusableTCPServer(("0.0.0.0", PORT), HealthHandler) as httpd:
        log.warning(f"Health server listening on :{PORT}")
        httpd.serve_forever()
