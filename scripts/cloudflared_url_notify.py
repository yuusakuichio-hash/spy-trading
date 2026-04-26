#!/usr/bin/env python3
"""cloudflared quick tunnel URL 変更検知 + Pushover 通知。

2026-04-24 策定。cloudflared は再起動ごとに URL 変更される。
前回 URL と比較して変わっていたらゆうさくさんに Pushover 通知。

Usage:
  python3 scripts/cloudflared_url_notify.py         # 現 URL 確認・変更なら通知
  python3 scripts/cloudflared_url_notify.py --force # 強制通知（変更なしでも送信）

launchd 統合: com.soralab.cloudflared-notify.plist を 5 分ごと起動。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "data" / "logs" / "cloudflared_tunnel.log"
STATE_FILE = PROJECT_ROOT / "data" / "state_v3" / "cloudflared_last_url.txt"


def extract_current_url() -> str | None:
    if not LOG_FILE.exists():
        return None
    content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"https://[a-z-]+\.trycloudflare\.com", content)
    return matches[-1] if matches else None


def get_last_notified_url() -> str:
    if not STATE_FILE.exists():
        return ""
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def save_last_url(url: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(url, encoding="utf-8")


def send_notification(url: str) -> None:
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from common.pushover_client import send
        send(
            "[Sora] 外出先ダッシュボード URL 更新",
            f"Sora Lab Monitor 外出先 URL:\n{url}\n\nスマホ Safari で叩いてください。",
            priority=0,
        )
        print(f"[notify] Pushover sent: {url}")
    except Exception as exc:
        print(f"[notify] Pushover failed: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    current = extract_current_url()
    if current is None:
        print("[notify] cloudflared URL not found (tunnel not running?)", file=sys.stderr)
        return 1

    last = get_last_notified_url()
    if current != last or args.force:
        send_notification(current)
        save_last_url(current)
        return 0

    print(f"[notify] URL unchanged: {current}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
