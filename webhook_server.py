#!/usr/bin/env python3
"""
webhook_server.py — HTTP webhook for HUB chat direct commands.

POST /command
  Header: Authorization: Bearer <WEBHOOK_TOKEN>
  Body:   {"command": "bash command..."}
  Returns: {"ok": true/false, "rc": 0, "output": "..."}

GET /health  -> {"ok": true}

Port: 9999
"""
import os
import sys
import json
import subprocess
import logging
import pathlib
import datetime
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Load .env ─────────────────────────────────────────────────────────────────
# .envの値で環境変数を上書きする（setdefaultではなく明示的上書きで.envを優先, V2-H2対応）
_ENV_FILE = pathlib.Path("/root/spxbot/.env")
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()

# ── Config ────────────────────────────────────────────────────────────────────
PORT          = int(os.environ.get("WEBHOOK_PORT", "9999"))
SECRET_TOKEN  = os.environ.get("WEBHOOK_TOKEN", "")
CMD_TIMEOUT   = 300

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER",  "u2cevk8nktib3sr148rw2hs78ecvux")

# ── Logging ───────────────────────────────────────────────────────────────────
pathlib.Path("/root/logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/logs/webhook_server.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("webhook_server")


def pushover(title, message) -> bool:
    """Pushover通知を送信する。成功時True、失敗時Falseを返す（V2-M3対応）。"""
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   PUSHOVER_TOKEN,
                "user":    PUSHOVER_USER,
                "title":   title,
                "message": message[:1024],
            },
            timeout=10,
        )
        if not resp.ok:
            log.warning(f"Pushover HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        log.warning(f"Pushover failed: {e}")
        return False


class WebhookHandler(BaseHTTPRequestHandler):

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "ts": datetime.datetime.utcnow().isoformat()})
        else:
            self._json(404, {"error": "not found"})

    def _auth(self) -> bool:
        """Bearer token認証。成功時True、失敗時401を返してFalse。"""
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {SECRET_TOKEN}"
        if not SECRET_TOKEN or auth != expected:
            log.warning(f"Auth failed from {self.address_string()}")
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def do_POST(self):
        # ── Kill Switch endpoints ─────────────────────────────────────────────
        if self.path in ("/kill_switch/activate", "/kill_switch/deactivate"):
            if not self._auth():
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw    = self.rfile.read(length)
                data   = json.loads(raw) if raw else {}
            except Exception:
                data = {}

            import sys as _sys
            _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
            try:
                from common.kill_switch import activate as _ks_activate, deactivate as _ks_deactivate
                if self.path == "/kill_switch/activate":
                    _reason    = (data.get("reason") or "webhook_trigger").strip()
                    _activator = self.address_string()
                    _ks_activate(reason=_reason, activator=_activator)
                    log.warning(f"[KillSwitch] ACTIVATED via webhook: reason={_reason}")
                    self._json(200, {"ok": True, "status": "activated", "reason": _reason})
                else:
                    _activator = self.address_string()
                    _ks_deactivate(activator=_activator)
                    log.info(f"[KillSwitch] DEACTIVATED via webhook by {_activator}")
                    self._json(200, {"ok": True, "status": "deactivated"})
            except Exception as e:
                log.error(f"[KillSwitch] endpoint error: {e}")
                self._json(500, {"error": str(e)})
            return

        if self.path != "/command":
            self._json(404, {"error": "not found"})
            return

        # ── Auth ──────────────────────────────────────────────────────────────
        if not self._auth():
            return

        # ── Parse body ────────────────────────────────────────────────────────
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw)
        except Exception as e:
            self._json(400, {"error": f"invalid json: {e}"})
            return

        command = (data.get("command") or "").strip()
        if not command:
            self._json(400, {"error": "command is required"})
            return

        log.info(f"CMD from {self.address_string()}: {command[:120]}")

        # ── Execute ───────────────────────────────────────────────────────────
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=CMD_TIMEOUT, executable="/bin/bash",
            )
            rc     = result.returncode
            output = (result.stdout + result.stderr).strip() or "(no output)"
        except subprocess.TimeoutExpired:
            rc, output = 124, f"Timeout ({CMD_TIMEOUT}s)"
        except Exception as e:
            rc, output = 1, str(e)

        ok = rc == 0
        log.info(f"rc={rc} ok={ok}")

        # Truncate for response
        out_disp = output if len(output) <= 4000 else output[:2000] + "\n…(truncated)…\n" + output[-2000:]

        self._json(200, {"ok": ok, "rc": rc, "output": out_disp})

    def log_message(self, fmt, *args):
        log.info(f"{self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    if not SECRET_TOKEN:
        log.error("WEBHOOK_TOKEN not set in /root/spxbot/.env — refusing to start insecurely")
        sys.exit(1)

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log.info(f"webhook_server listening on 0.0.0.0:{PORT}")
    pushover("webhook_server 起動", f"VPS 198.13.37.17:{PORT} POST /command ready")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
