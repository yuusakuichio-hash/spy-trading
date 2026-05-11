#!/usr/bin/env python3
"""claim_ledger_guard.py — 未検証 claim ブロッカー(ドメイン非依存版)

Stop hook: URL/価格/仕様の未検証 claim を user 出力前に block
ROOT は $CLAUDE_PROJECT_DIR で各プロジェクトに閉じる
Pushover token/user は env var 経由(.env)・未設定なら通知 skip

設計方針:
  1. HARD BLOCK (exit 2) - 未検証 URL/価格を user 出力前に阻止
  2. CLAIM_LEDGER_BYPASS=1 で緊急回避
  3. Bash tool 結果 (curl 200 等) を自動 ledger 更新 (verified_at 付き)
  4. 直近 assistant 応答から claim 抽出 -> ledger 突合 -> 未検証なら violation 記録

出典:
  - Poka-Yoke 6原則 (Shingo 1960s, Toyota): detection 層
  - WHO Surgical Safety Checklist (NEJM 2009, n=7688): 死亡40%減
  - Chain-of-Verification CoVe (arXiv 2309.11495)
  - Reflexion (arXiv 2303.11366)

Ledger スキーマ (1行1 claim JSONL):
  {claim_id, type, value, normalized, source, source_detail,
   verified_at, verified_by, ttl_hours, hash}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
LEDGER = ROOT / "data" / "claim_ledger.jsonl"
LOG_DIR = ROOT / "data" / "logs"
VIOLATIONS_LOG = LOG_DIR / "claim_ledger_violations.log"
PENDING_PATH = LOG_DIR / "pending_proposal_violations.md"

JST = timezone(timedelta(hours=9))

TTL_HOURS = {
    "url": 24,
    "price": 1,
    "spec": 72,
    "date": 24,
    "procedure": 24,
}

INTERNAL_URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)"
)
TRUSTED_HOSTS = re.compile(
    r"https?://(?:ntfy\.sh|api\.pushover\.net|api\.github\.com|"
    r"raw\.githubusercontent\.com|pypi\.org|files\.pythonhosted\.org)"
)

URL_RE = re.compile(r"https?://[A-Za-z0-9./\-?#_%=&:+]+")
PRICE_RE = re.compile(
    r"(?:\$\s?\d[\d,]*(?:\.\d+)?[KMB]?"
    r"|\d[\d,]*\s?(?:USD|円|万円|億円))"
)

# Pushover は .env から(未設定なら通知 skip)
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")


def _load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def sha12(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:12]


def _ensure_paths() -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not LEDGER.exists():
        LEDGER.touch()


def load_ledger() -> list[dict[str, Any]]:
    _ensure_paths()
    items: list[dict[str, Any]] = []
    for line in LEDGER.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def append_ledger(entry: dict[str, Any]) -> None:
    _ensure_paths()
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def is_fresh(entry: dict[str, Any], now: datetime) -> bool:
    try:
        va = datetime.fromisoformat(entry["verified_at"])
    except (KeyError, ValueError):
        return False
    ttl = TTL_HOURS.get(entry.get("type", ""), 24)
    return (now - va) < timedelta(hours=ttl)


def index_verified(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    now = datetime.now(JST)
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for e in items:
        if not is_fresh(e, now):
            continue
        key = (e.get("type", ""), e.get("value", ""))
        cur = idx.get(key)
        if cur is None or e.get("verified_at", "") > cur.get("verified_at", ""):
            idx[key] = e
    return idx


def read_transcript(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def last_assistant_text(events: list[dict[str, Any]]) -> str:
    for ev in reversed(events):
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)[:16000]
    return ""


def recent_bash_results(events: list[dict[str, Any]], limit: int = 40) -> list[tuple[str, str]]:
    cmd_by_id: dict[str, str] = {}
    for ev in events:
        if ev.get("type") == "assistant":
            for b in ev.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                    cmd_by_id[b.get("id", "")] = b.get("input", {}).get("command", "")
    pairs: list[tuple[str, str]] = []
    for ev in events:
        if ev.get("type") == "user":
            for b in ev.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id", "")
                    cmd = cmd_by_id.get(tid, "")
                    content = b.get("content", "")
                    if isinstance(content, list):
                        content = "".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    if cmd:
                        pairs.append((cmd, str(content)))
    return pairs[-limit:]


def harvest_from_bash(pairs: list[tuple[str, str]]) -> int:
    added = 0
    for cmd, out in pairs:
        m = re.search(r"curl\b.*?(https?://[^\s'\"]+)", cmd)
        if m:
            url = m.group(1).rstrip(".,)")
            if out and ("200" in out[:200] or len(out) > 50):
                append_ledger(
                    {
                        "claim_id": sha12(url),
                        "type": "url",
                        "value": url,
                        "normalized": url,
                        "source": "curl",
                        "source_detail": f"bash curl (stdout_len={len(out)})",
                        "verified_at": now_jst_iso(),
                        "verified_by": "hook_auto",
                        "ttl_hours": TTL_HOURS["url"],
                        "hash": sha12(url + "curl"),
                    }
                )
                added += 1
    return added


def extract_claims(text: str) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for m in URL_RE.finditer(text):
        v = m.group(0).rstrip(".,)、。」")
        if INTERNAL_URL_RE.match(v) or TRUSTED_HOSTS.match(v):
            continue
        k = ("url", v)
        if k not in seen:
            seen.add(k)
            out.append(k)
    for m in PRICE_RE.finditer(text):
        k = ("price", m.group(0).strip())
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def record_violation(unverified: list[tuple[str, str]]) -> None:
    ts = now_jst_iso()
    with VIOLATIONS_LOG.open("a", encoding="utf-8") as f:
        for typ, val in unverified:
            f.write(f"[{ts}] UNVERIFIED {typ} | {val}\n")

    with PENDING_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\n## [{ts}] Claim Ledger 未検証 claim 検知\n")
        for typ, val in unverified:
            f.write(f"- [{typ}] {val}\n")
        f.write(
            "\n→ curl / 公式 doc で verify してから再送。"
            "緊急回避: CLAIM_LEDGER_BYPASS=1\n"
        )

    # Pushover (認証情報あり・JST 5-22 のみ)
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        return
    hour = datetime.now(JST).hour
    if 5 <= hour < 22:
        try:
            import urllib.parse
            import urllib.request

            msg = "Unverified claims in response:\n" + "\n".join(
                f"- [{t}] {v[:80]}" for t, v in unverified[:5]
            )
            data = urllib.parse.urlencode(
                {
                    "token": PUSHOVER_TOKEN,
                    "user": PUSHOVER_USER,
                    "title": "[SYS/ALERT] Claim Ledger unverified",
                    "message": msg[:900],
                    "priority": "0",
                }
            ).encode()
            req = urllib.request.Request(
                "https://api.pushover.net/1/messages.json", data=data
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass


def main() -> int:
    if os.environ.get("CLAIM_LEDGER_BYPASS") == "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    transcript_path = payload.get("transcript_path", "") or ""
    if not transcript_path:
        return 0

    events = read_transcript(transcript_path)
    if not events:
        return 0

    _ensure_paths()

    bash_pairs = recent_bash_results(events)
    harvest_from_bash(bash_pairs)

    text = last_assistant_text(events)
    if not text:
        return 0
    claims = extract_claims(text)
    if not claims:
        return 0

    verified_idx = index_verified(load_ledger())
    unverified = [c for c in claims if c not in verified_idx]

    if unverified:
        record_violation(unverified)
        print(
            f"[CLAIM_LEDGER_GUARD] BLOCKED: {len(unverified)} unverified claim(s) detected.\n"
            + "\n".join(f"  - [{t}] {v[:120]}" for t, v in unverified[:5])
            + "\nVerify with curl/docs first, or set CLAIM_LEDGER_BYPASS=1 to override.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
