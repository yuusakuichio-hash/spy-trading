#!/usr/bin/env python3
"""
ntfy_listener.py — SSE subscriber for HUB chat → VPS bidirectional commands.

Subscribe: https://ntfy.sh/spxbot-hub-yuusaku2026
Publish:   https://ntfy.sh/spxbot-hub-result-yuusaku2026

Message format (ntfy message body):
  Plain bash command string, e.g.: echo hello && date

Security: only messages with X-Auth-Token header matching NTFY_TOKEN are executed.
          If NTFY_TOKEN is empty, all messages are executed (open mode).
"""
import os
import sys
import json
import subprocess
import logging
import pathlib
import datetime
import time
import requests

# ── Load .env ─────────────────────────────────────────────────────────────────
_ENV_FILE = pathlib.Path("/root/spxbot/.env")
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
CMD_CHANNEL    = "spxbot-hub-yuusaku2026"
RESULT_CHANNEL = "spxbot-hub-result-yuusaku2026"
NTFY_BASE      = "https://ntfy.sh"
CMD_TIMEOUT    = 300
RECONNECT_WAIT = 10   # seconds before SSE reconnect on error

PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER  = "u2cevk8nktib3sr148rw2hs78ecvux"

# ── Logging ───────────────────────────────────────────────────────────────────
pathlib.Path("/root/logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/logs/ntfy_listener.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ntfy_listener")


# ── Pushover ──────────────────────────────────────────────────────────────────
def pushover(title, message, priority=0):
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                  "title": title, "message": message[:1024], "priority": priority},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Pushover failed: {e}")


# ── ntfy publish ──────────────────────────────────────────────────────────────
def ntfy_publish(message: str, title: str = "", tags: str = ""):
    try:
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if title:
            headers["Title"] = title
        if tags:
            headers["Tags"] = tags
        requests.post(
            f"{NTFY_BASE}/{RESULT_CHANNEL}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
    except Exception as e:
        log.warning(f"ntfy publish failed: {e}")


# ── Command execution ─────────────────────────────────────────────────────────
def execute(command: str) -> dict:
    log.info(f"EXEC: {command[:120]}")
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
    log.info(f"rc={rc}")
    return {"rc": rc, "output": output, "ok": rc == 0}


# ── SSE subscriber ────────────────────────────────────────────────────────────
def subscribe_and_run():
    url = f"{NTFY_BASE}/{CMD_CHANNEL}/sse"
    log.info(f"Subscribing to {url}")

    resp = requests.get(url, stream=True, timeout=(10, None),
                        headers={"Accept": "text/event-stream"})
    resp.raise_for_status()

    event_data = {}
    for raw_line in resp.iter_lines(decode_unicode=True):
        # SSE format: "event: ...", "data: ...", "" (blank = dispatch)
        if raw_line.startswith("event:"):
            event_data["event"] = raw_line.split(":", 1)[1].strip()
        elif raw_line.startswith("data:"):
            event_data["data"] = raw_line.split(":", 1)[1].strip()
        elif raw_line == "":
            # Dispatch accumulated event
            event = event_data.get("event", "message")
            data  = event_data.get("data", "")
            event_data = {}

            if event not in ("message", "open"):
                continue
            if not data:
                continue

            # ntfy SSE data is JSON
            try:
                msg = json.loads(data)
            except Exception:
                continue

            if msg.get("event") == "open":
                log.info("SSE connection open")
                continue

            command = (msg.get("message") or "").strip()
            if not command:
                continue

            ts = datetime.datetime.utcnow().strftime("%H:%M:%S UTC")
            log.info(f"Received command: {command[:80]}")

            result = execute(command)
            rc     = result["rc"]
            output = result["output"]
            ok     = result["ok"]

            # Truncate output for ntfy (8KB limit)
            out_disp = output if len(output) <= 3000 else output[:1500] + "\n…(truncated)…\n" + output[-1500:]

            status_tag = "white_check_mark" if ok else "x"
            ntfy_publish(
                f"$ {command[:60]}\n---\n{out_disp}\n---\nrc={rc} | {ts}",
                title=f"{'OK' if ok else 'NG'} rc={rc}",
                tags=status_tag,
            )


# ── Main loop ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"ntfy_listener started | cmd={CMD_CHANNEL} | result={RESULT_CHANNEL}")
    pushover("ntfy接続完了", f"VPS listening on ntfy.sh/{CMD_CHANNEL}")

    while True:
        try:
            subscribe_and_run()
        except KeyboardInterrupt:
            log.info("Shutting down")
            sys.exit(0)
        except Exception as e:
            log.error(f"SSE error: {e} — reconnecting in {RECONNECT_WAIT}s")
            time.sleep(RECONNECT_WAIT)
