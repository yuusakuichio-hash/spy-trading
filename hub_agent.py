#!/usr/bin/env python3
"""
hub_agent.py — GitHub Issue-driven command executor for VPS.
Polls every 60s for Issues labeled 'hub-command', executes body as bash,
comments result, closes issue, notifies via Pushover.
"""
import os
import sys
import subprocess
import time
import logging
import pathlib
import datetime
import requests

# ── Load .env ────────────────────────────────────────────────────────────────
_ENV_FILE = pathlib.Path("/root/spxbot/.env")
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Constants ─────────────────────────────────────────────────────────────────
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
REPO           = os.environ.get("GITHUB_REPO", "yuusakuichio-hash/spy-trading")
LABEL          = "hub-command"
POLL_INTERVAL  = 60   # seconds
CMD_TIMEOUT    = 300  # seconds per command

PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER  = "u2cevk8nktib3sr148rw2hs78ecvux"

# ── Logging ───────────────────────────────────────────────────────────────────
pathlib.Path("/root/logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/logs/hub_agent.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("hub_agent")


# ── GitHub API ────────────────────────────────────────────────────────────────
def _gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh(method, path, **kwargs):
    url = f"https://api.github.com/repos/{REPO}/{path}"
    try:
        r = getattr(requests, method)(url, headers=_gh_headers(), timeout=15, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        log.warning(f"GitHub API {method.upper()} {path} failed: {e}")
        return None


def get_open_commands():
    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN not set — polling disabled")
        return []
    result = gh("get", "issues", params={"labels": LABEL, "state": "open", "per_page": 10})
    return result if isinstance(result, list) else []


def post_comment(number, body):
    gh("post", f"issues/{number}/comments", json={"body": body})


def close_issue(number):
    gh("patch", f"issues/{number}", json={"state": "closed"})


# ── Pushover ──────────────────────────────────────────────────────────────────
def pushover(title, message, priority=0):
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":    PUSHOVER_TOKEN,
                "user":     PUSHOVER_USER,
                "title":    title,
                "message":  message[:1024],
                "priority": priority,
            },
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Pushover failed: {e}")


# ── Command execution ─────────────────────────────────────────────────────────
def execute_command(issue):
    number = issue["number"]
    title  = issue.get("title", "")
    body   = (issue.get("body") or "").strip()

    log.info(f"Issue #{number}: {title!r}")

    if not body:
        post_comment(number, "⚠️ Issue body is empty — nothing to execute.")
        close_issue(number)
        pushover("hub-command ⚠️", f"#{number} empty body")
        return

    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    log.info(f"Running: {body[:120]}")

    try:
        result = subprocess.run(
            body, shell=True, capture_output=True, text=True,
            timeout=CMD_TIMEOUT, executable="/bin/bash",
        )
        rc = result.returncode
        output = (result.stdout + result.stderr).strip() or "(no output)"
        status = "✅ 成功" if rc == 0 else f"❌ 失敗 (rc={rc})"
    except subprocess.TimeoutExpired:
        rc, output, status = 124, f"タイムアウト ({CMD_TIMEOUT}s)", "❌ タイムアウト"
    except Exception as e:
        rc, output, status = 1, str(e), "❌ エラー"

    # Truncate for GitHub (max ~3000 chars in comment code block)
    out_disp = output if len(output) <= 3000 else output[:1500] + "\n…(truncated)…\n" + output[-1500:]

    comment = (
        f"## hub-command {status}\n\n"
        f"**Command:**\n```bash\n{body}\n```\n\n"
        f"**Exit code:** `{rc}`\n\n"
        f"**Output:**\n```\n{out_disp}\n```\n\n"
        f"*{ts} | VPS 198.13.37.17*"
    )
    post_comment(number, comment)
    close_issue(number)

    priority = 0 if rc == 0 else 1
    pushover(
        f"hub-command {status}",
        f"#{number} {title[:50]}\nrc={rc}\n{output[:200]}",
        priority=priority,
    )
    log.info(f"Issue #{number} closed: {status}")


# ── Main loop ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"hub_agent started | repo={REPO} | poll={POLL_INTERVAL}s")
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN not set — add to /root/spxbot/.env and restart")

    while True:
        try:
            issues = get_open_commands()
            if issues:
                log.info(f"Found {len(issues)} hub-command issue(s)")
                for issue in issues:
                    execute_command(issue)
        except Exception as e:
            log.error(f"Poll loop error: {e}")
        time.sleep(POLL_INTERVAL)
