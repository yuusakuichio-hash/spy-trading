#!/usr/bin/env python3
"""
gmail_monitor.py — Gmail AI監視 for SPX Bot VPS
- 15分ごとに未読メール全件を取得
- Claude API (claude-3-5-haiku-20241022) で重要度判定
- 重要メール → Pushover転送
- 非重要メール → 自動アーカイブ
- 起動: python3 gmail_monitor.py
- 初回認証: python3 gmail_monitor.py --auth
"""

import os
import sys
import json
import time
import logging
import base64
import argparse
from pathlib import Path

# ── .env loader ───────────────────────────────────────────────────────────────
def _load_env():
    env_path = Path("/root/spxbot/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

# ── Config ────────────────────────────────────────────────────────────────────
LOG_DIR            = Path(os.environ.get("SPX_LOG_DIR", "/var/log/spx_bot"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIALS_FILE   = Path("/root/spxbot/credentials.json")
TOKEN_FILE         = Path("/root/spxbot/gmail_token.json")
PUSHOVER_TOKEN     = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER      = os.environ.get("PUSHOVER_USER",  "u2cevk8nktib3sr148rw2hs78ecvux")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
POLL_INTERVAL      = 900   # 15 minutes
GMAIL_SCOPES       = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
CLAUDE_MODEL       = "claude-3-5-haiku-20241022"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "gmail_monitor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("gmail_monitor")


# ── Pushover ──────────────────────────────────────────────────────────────────
def pushover(title: str, message: str, priority: int = 0) -> bool:
    import urllib.request, urllib.parse
    try:
        data = urllib.parse.urlencode({
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
            "priority": priority,
        }).encode()
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        log.error(f"Pushover failed: {e}")
        return False


# ── Gmail OAuth2 ──────────────────────────────────────────────────────────────
def get_gmail_service():
    """Build Gmail API service, refreshing/creating token as needed."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        log.error("Required: pip install google-auth google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error(f"credentials.json not found at {CREDENTIALS_FILE}. Run --auth first.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        log.info("Gmail token saved to gmail_token.json")

    return build("gmail", "v1", credentials=creds)


# ── Claude API importance judgment ────────────────────────────────────────────
def is_important(sender: str, subject: str, body_snippet: str) -> bool:
    """Use Claude API to judge if email is important (financial/account/urgent)."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — treating all email as non-important")
        return False

    import urllib.request, urllib.parse
    prompt = (
        f"送信元: {sender}\n"
        f"件名: {subject}\n"
        f"本文(抜粋): {body_snippet[:300]}\n\n"
        "これは金融・口座・緊急・要対応のメールか？YES/NOで答えよ（理由不要）"
    )
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        answer = resp.get("content", [{}])[0].get("text", "NO").strip().upper()
        result = answer.startswith("YES")
        log.info(f"Claude judgment: '{subject[:40]}' → {answer} → important={result}")
        return result
    except Exception as e:
        log.error(f"Claude API call failed: {e}")
        return False  # safe default: don't archive on error


# ── Get email body ────────────────────────────────────────────────────────────
def get_body_snippet(msg: dict) -> str:
    """Extract plain-text body snippet from Gmail message."""
    try:
        payload = msg.get("payload", {})
        # Check body directly
        body_data = payload.get("body", {}).get("data", "")
        if body_data:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")[:500]
        # Check parts
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")[:500]
    except Exception:
        pass
    return msg.get("snippet", "")[:300]


# ── Main polling loop ─────────────────────────────────────────────────────────
def run_monitor():
    """Main 15-minute polling loop."""
    log.info("Gmail monitor starting...")
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — Claude judgment disabled")

    service = get_gmail_service()
    log.info("Gmail service initialized")

    while True:
        try:
            poll_once(service)
        except Exception as e:
            log.error(f"Poll error: {e}")
            # Reconnect on auth errors
            try:
                service = get_gmail_service()
            except Exception:
                pass
        log.info(f"Sleeping {POLL_INTERVAL}s until next poll...")
        time.sleep(POLL_INTERVAL)


def poll_once(service):
    """One poll cycle: fetch unread → judge → forward or archive."""
    log.info("Polling Gmail for unread messages...")
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=50,
    ).execute()

    messages = result.get("messages", [])
    if not messages:
        log.info("No unread messages")
        return

    log.info(f"Found {len(messages)} unread message(s)")
    for msg_ref in messages:
        try:
            process_message(service, msg_ref["id"])
        except Exception as e:
            log.error(f"Error processing message {msg_ref['id']}: {e}")


def process_message(service, msg_id: str):
    """Fetch full message, judge importance, forward or archive."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    sender  = headers.get("from", "(不明)")
    subject = headers.get("subject", "(件名なし)")
    body    = get_body_snippet(msg)

    log.info(f"Checking: [{sender}] {subject[:60]}")

    important = is_important(sender, subject, body)

    if important:
        # Forward to Pushover
        title = f"📧 {sender[:20]}"
        message = f"{subject[:30]}"
        pushover(title, message, priority=0)
        log.info(f"Forwarded to Pushover: {subject[:60]}")
        # Mark as read (keep in inbox)
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    else:
        # Archive (remove from INBOX, mark as read)
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"removeLabelIds": ["INBOX", "UNREAD"]},
        ).execute()
        log.info(f"Archived (non-important): {subject[:60]}")


# ── Auth helper ───────────────────────────────────────────────────────────────
def run_auth():
    """Interactive OAuth2 authentication flow. Run once on VPS."""
    log.info("Starting Gmail OAuth2 authentication...")
    if not CREDENTIALS_FILE.exists():
        print(f"\nERROR: {CREDENTIALS_FILE} not found.")
        print("Please follow the setup instructions to obtain credentials.json first.\n")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Run: pip install google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
    creds = flow.run_local_server(port=8765)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"\n✅ Authentication successful! Token saved to {TOKEN_FILE}")
    print("You can now start the gmail_monitor service.")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail AI Monitor for SPX Bot")
    parser.add_argument("--auth", action="store_true",
                        help="Run OAuth2 authentication flow (required once)")
    args = parser.parse_args()

    if args.auth:
        run_auth()
    else:
        run_monitor()
