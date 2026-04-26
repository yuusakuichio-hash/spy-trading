#!/usr/bin/env python3
"""claim_ledger_guard.py

Stop hook (最小版・Poka-Yoke detection 層)
提案ファイル: 手動で .claude/hooks/ へコピー + settings.local.json 登録必須

設置手順:
  1) cp /Users/yuusakuichio/trading/data/hooks_proposal/claim_ledger_guard.py \
        /Users/yuusakuichio/trading/.claude/hooks/claim_ledger_guard.py
  2) chmod +x /Users/yuusakuichio/trading/.claude/hooks/claim_ledger_guard.py
  3) .claude/settings.local.json の "hooks.Stop" 配列に追記:
     {"type":"command","command":"/Users/yuusakuichio/trading/.claude/hooks/claim_ledger_guard.py"}
  4) テスト: echo '{"transcript_path":""}' | python3 .claude/hooks/claim_ledger_guard.py

背景: URL / 価格 / プロップ仕様を verify せず user に提示するパターン再発
      (2026-04-21 CLAUDE.md 禁句TOP5 / SNS truth guard 等と同系列)

設計方針:
  1. HARD BLOCK (exit 2) - 未検証 URL/価格を user 出力前に阻止
  2. CLAIM_LEDGER_BYPASS=1 で緊急回避
  3. Bash tool 結果 (curl 200 等) を自動 ledger 更新 (verified_at 付き)
  4. 直近 assistant 応答から claim 抽出 -> ledger 突合 -> 未検証なら violation 記録

出典:
  - Poka-Yoke 6原則 (Shingo 1960s, Toyota): detection 層 = 下位優先
  - WHO Surgical Safety Checklist (NEJM 2009, n=7688): 死亡40%減・記憶ではなく読み上げ
  - Chain-of-Verification CoVe (arXiv 2309.11495): claim 独立検証
  - Reflexion (arXiv 2303.11366): episodic memory 継承

制御:
  CLAIM_LEDGER_BYPASS=1 で無効化

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

# --- 定数 ---------------------------------------------------------------
ROOT = Path("/Users/yuusakuichio/trading")
LEDGER = ROOT / "data" / "claim_ledger.jsonl"
LOG_DIR = ROOT / "data" / "logs"
VIOLATIONS_LOG = LOG_DIR / "claim_ledger_violations.log"
PENDING_PATH = LOG_DIR / "pending_proposal_violations.md"

JST = timezone(timedelta(hours=9))

# Claim TTL (hour) - 出典: research md 2-2
TTL_HOURS = {
    "url": 24,
    "price": 1,
    "spec": 72,
    "date": 24,
    "procedure": 24,
}

# 内部 URL (localhost / 127.0.0.1 / VPS IP) は検証不要
INTERNAL_URL_RE = re.compile(
    r"https?://(?:localhost|127\\.0\\.0\\.1|198\\.13\\.37\\.17|0\\.0\\.0\\.0)"
)
# ntfy / pushover / github API は自動許可
TRUSTED_HOSTS = re.compile(
    r"https?://(?:ntfy\\.sh|api\\.pushover\\.net|api\\.github\\.com|"
    r"raw\\.githubusercontent\\.com|pypi\\.org|files\\.pythonhosted\\.org)"
)

# --- 抽出パターン ------------------------------------------------------
URL_RE = re.compile(r"https?://[A-Za-z0-9./\-?#_%=&:+]+")
# 価格: $1,234 / $50K / 127 USD / 50000円 / 10万円
PRICE_RE = re.compile(
    r"(?:\$\s?\d[\d,]*(?:\.\d+)?[KMB]?"
    r"|\d[\d,]*\s?(?:USD|円|万円|億円))"
)

# Pushover
PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER = "u2cevk8nktib3sr148rw2hs78ecvux"


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def sha12(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:12]


# --- Ledger CRUD -------------------------------------------------------
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
    """(type, value) -> latest fresh entry."""
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


# --- Transcript 解析 ---------------------------------------------------
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
    """直近 bash 実行の (command, stdout) を返す。"""
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


# --- 自動 ledger 更新（Bash 結果から） ---------------------------------
def harvest_from_bash(pairs: list[tuple[str, str]]) -> int:
    """curl/gh 等の結果から verified claim を自動追加。返り値: 追加件数"""
    added = 0
    for cmd, out in pairs:
        m = re.search(r"curl\b.*?(https?://[^\s'\"]+)", cmd)
        if m:
            url = m.group(1).rstrip(".,)")
            # 成功判定: stdout 空じゃない or "200" が含まれる (簡易)
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


# --- Claim 抽出 --------------------------------------------------------
def extract_claims(text: str) -> list[tuple[str, str]]:
    """(type, value) list。重複除去。"""
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


# --- 違反記録 ----------------------------------------------------------
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

    # Pushover (JST 5-22 のみ)
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


# --- main --------------------------------------------------------------
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

    # 1. Bash 結果から ledger 自動更新
    bash_pairs = recent_bash_results(events)
    harvest_from_bash(bash_pairs)

    # 2. 直近応答から claim 抽出
    text = last_assistant_text(events)
    if not text:
        return 0
    claims = extract_claims(text)
    if not claims:
        return 0

    # 3. ledger と突合
    verified_idx = index_verified(load_ledger())
    unverified = [c for c in claims if c not in verified_idx]

    if unverified:
        record_violation(unverified)
        print(
            f"[CLAIM_LEDGER_GUARD] BLOCKED: {len(unverified)} unverified claim(s) detected.\n"
            + "\n".join(f"  - [{t}] {v[:120]}" for t, v in unverified[:5])
            + "\nVerify with curl/docs first, or set CLAIM_LEDGER_BYPASS=1 to override.",
            file=__import__("sys").stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
