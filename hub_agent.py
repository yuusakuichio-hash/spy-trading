#!/usr/bin/env python3
"""
HUB Command Agent — polls GitHub Issues for hub-command label and executes them.
Reads GITHUB_TOKEN, PUSHOVER_TOKEN, PUSHOVER_USER from environment / .env.
"""
import requests
import subprocess
import time
import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/logs/hub_agent.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
REPO           = "yuusakuichio-hash/spy-trading"
PUSHOVER_TOKEN = os.getenv("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER  = os.getenv("PUSHOVER_USER",  "u2cevk8nktib3sr148rw2hs78ecvux")
POLL_INTERVAL  = 60  # seconds

GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def gh(method, path, **kwargs):
    url = f"https://api.github.com/repos/{REPO}/{path}"
    try:
        r = getattr(requests, method)(url, headers=GH_HEADERS, timeout=15, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}
    except Exception as e:
        log.warning(f"GitHub API {method.upper()} {path} failed: {e}")
        return None


def pushover(title, message, priority=0):
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
    except Exception as e:
        log.warning(f"Pushover failed: {e}")


def get_open_commands():
    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN not set — polling disabled")
        return []
    result = gh("get", "issues", params={"labels": "hub-command", "state": "open"})
    return result if isinstance(result, list) else []


def execute_command(issue):
    number = issue["number"]
    title  = issue.get("title", "")
    body   = (issue.get("body") or "").strip()

    if not body:
        log.info(f"Issue #{number} has empty body — closing without execution")
        gh("patch", f"issues/{number}", json={"state": "closed"})
        return

    log.info(f"Executing Issue #{number}: {body[:80]}")
    try:
        result = subprocess.run(
            body, shell=True, capture_output=True, text=True, timeout=300
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        output = (stdout or stderr)[-1000:]
        status = "✅ 成功" if result.returncode == 0 else f"❌ 失敗 (rc={result.returncode})"
    except subprocess.TimeoutExpired:
        output = "タイムアウト (300s)"
        status = "❌ タイムアウト"
    except Exception as e:
        output = str(e)
        status = "❌ エラー"

    # Comment on issue
    comment_body = f"**{status}**\n```\n{output}\n```"
    gh("post", f"issues/{number}/comments", json={"body": comment_body})

    # Close issue
    gh("patch", f"issues/{number}", json={"state": "closed", "labels": ["hub-command", "hub-done"]})

    # Pushover
    priority = 0 if "成功" in status else 1
    pushover(
        f"HUBコマンド {status}",
        f"#{number} {title[:50]}\n{output[:200]}",
        priority=priority,
    )
    log.info(f"Issue #{number} done: {status}")


if __name__ == "__main__":
    log.info(f"hub_agent started. Repo={REPO} interval={POLL_INTERVAL}s")
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN is not set. Add it to /root/spxbot/.env and restart.")

    while True:
        try:
            issues = get_open_commands()
            for issue in issues:
                execute_command(issue)
        except Exception as e:
            log.error(f"Poll loop error: {e}")
        time.sleep(POLL_INTERVAL)
