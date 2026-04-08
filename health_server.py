#!/usr/bin/env python3
"""
Lightweight HTTP health server for SPX Bot VPS (port 8080).
Returns JSON status of spxbot/opend/xvfb services and system metrics.
"""

import json
import subprocess
import datetime
import os
import time
import http.server
import socketserver
import logging
from pathlib import Path

PORT = 8080
_START_TIME = time.time()
LOG_DIR = Path("/var/log/spx_bot")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "health.log"), logging.StreamHandler()],
)
log = logging.getLogger("health_server")

SERVICES = ["spxbot", "opend", "xvfb"]


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
        all_active = all(v == "active" for v in services.values())
        mem = get_memory_mb()
        disk_pct = get_disk_usage_pct()

        # Memory warning threshold: >90%
        mem_warn = mem["total_mb"] > 0 and (mem["used_mb"] / mem["total_mb"]) > 0.90

        status_code = 200 if (all_active and not mem_warn) else 503

        payload = {
            "status": "ok" if status_code == 200 else "degraded",
            "uptime": int(time.time() - _START_TIME),
            "bot": "running" if services.get("spxbot") == "active" else "stopped",
            "opend": "connected" if services.get("opend") == "active" else "disconnected",
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
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
