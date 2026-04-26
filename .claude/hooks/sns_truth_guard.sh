#!/usr/bin/env python3
"""
sns_truth_guard: SNS投稿に本番稼働示唆の嘘を混入させないHARD BLOCK

設置方法（.claude/ 配下書き込み拒否のためこちらに草案を置いた）:
  cp /Users/yuusakuichio/trading/data/sns/sns_truth_guard.proposed.sh \
     /Users/yuusakuichio/trading/.claude/hooks/sns_truth_guard.sh
  chmod +x /Users/yuusakuichio/trading/.claude/hooks/sns_truth_guard.sh

settings.local.json の PreToolUse に追加:
  {
    "matcher": ".*",
    "hooks": [
      {
        "type": "command",
        "command": "/Users/yuusakuichio/trading/.claude/hooks/sns_truth_guard.sh"
      }
    ]
  }

発動条件:
- Write|Edit|MultiEdit で data/sns/ 配下
  (daily_*.txt / pending_*.json / weekly_*.md / pillar_*.json / intro.txt)
- Bash で sns_daily_post.py / sns_weekly_post*.py 実行
- payload テキストから NG パターン検出 -> exit 2 (HARD BLOCK)

NG: 勝/負/利益/稼ぎ/配当/運用益/月利/年利/収益/実戦/本番/リアルマネー/
    $金額/円金額/勝率/累計/プラスX%

Bypass:
- SAFE_MARKERS (ペーパー/検証/デモ/シミュレーション/実績はこれから/
  まだ実績なし/エントリーなし) が同一 payload 内
- env SNS_TRUTH_BYPASS=1
- memory/research/docs/hooks/agents/journal ファイル編集
"""
import sys, json, re, os
from datetime import datetime

LOG_FILE = "/Users/yuusakuichio/trading/data/logs/sns_truth_violations.log"

if os.environ.get("SNS_TRUTH_BYPASS") == "1":
    sys.exit(0)

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {}) or {}

if tool_name in ("Read", "Grep", "Glob"):
    sys.exit(0)

path = tool_input.get("file_path", "") or tool_input.get("path", "") or ""
cmd = tool_input.get("command", "") or ""

target = False
if tool_name in ("Write", "Edit", "MultiEdit"):
    if "/data/sns/" in path and (
        re.search(r"daily_\d+\.txt$", path)
        or re.search(r"pending_.+\.json$", path)
        or re.search(r"weekly_.+\.md$", path)
        or re.search(r"pillar_.+\.json$", path)
        or path.endswith("intro.txt")
    ):
        target = True

if tool_name == "Bash":
    if (re.search(r"sns_(daily|weekly)_post.*\.py", cmd)
            and "test" not in cmd.lower()
            and "--dry" not in cmd):
        target = True

if not target:
    sys.exit(0)

bypass_paths = [
    "memory/", "research_", "docs/", ".claude/hooks/", ".claude/agents/",
    "premortem_reports/", "redteam_reports/", "/data/journal_",
    "sns_truth_violations.log",
    ".corrected.txt", ".proposed.sh",
]
if any(p in path for p in bypass_paths):
    sys.exit(0)

payload_text = ""
if tool_name == "Write":
    payload_text = tool_input.get("content", "") or ""
elif tool_name == "Edit":
    payload_text = tool_input.get("new_string", "") or ""
elif tool_name == "MultiEdit":
    edits = tool_input.get("edits", []) or []
    payload_text = "\n".join((e.get("new_string") or "") for e in edits)
elif tool_name == "Bash":
    sns_dir = "/Users/yuusakuichio/trading/data/sns"
    if os.path.isdir(sns_dir):
        recent = []
        for fn in os.listdir(sns_dir):
            if re.match(r"(daily_|pending_|weekly_)", fn) and ".corrected." not in fn:
                full = os.path.join(sns_dir, fn)
                try:
                    recent.append((os.path.getmtime(full), full))
                except Exception:
                    pass
        recent.sort(reverse=True)
        for _, full in recent[:3]:
            try:
                with open(full, "r", encoding="utf-8") as f:
                    payload_text += "\n" + f.read()
            except Exception:
                pass

if not payload_text.strip():
    sys.exit(0)

SAFE_MARKERS = [
    "ペーパー", "検証", "実績はこれから", "まだ実績なし", "デモ",
    "シミュレーション", "simulation", "paper", "PAPER", "仮想",
    "実績ゼロ", "実績0", "エントリーなし", "トレードなし", "まだ動かして",
]
has_safe = any(m in payload_text for m in SAFE_MARKERS)

NG_PATTERNS = [
    (r"勝っ", "win-claim"),
    (r"負け", "loss-claim"),
    (r"利益", "profit-claim"),
    (r"稼い", "earn-claim"),
    (r"配当", "dividend"),
    (r"運用益", "return-claim"),
    (r"月利\s*\d", "monthly-yield"),
    (r"年利\s*\d", "yearly-yield"),
    (r"トータル\s*[\+\-]?\s*\$?\d", "total-pnl"),
    (r"収益\s*[\+\-]?\s*\$?\d", "revenue-number"),
    (r"実戦", "live-word"),
    (r"本番(?!移行|前|稼働)", "production-word"),
    (r"リアルマネー", "real-money"),
    (r"実弾", "real-ammo"),
    (r"[\+\-]?\s*\$\s*\d", "usd-amount"),
    (r"[\+\-]?\s*\d[\d,]+\s*円", "jpy-amount"),
    (r"\d[\d,]*\s*万円", "manen-amount"),
    (r"\d+\s*勝\s*\d+\s*敗", "win-loss-record"),
    (r"勝率\s*\d", "win-rate"),
    (r"累計\s*[\+\-]?\s*\$?\d", "cumulative-pnl"),
    (r"\d+\s*回\s*(勝率|WR)", "count-winrate"),
    (r"[＋\+]\s*\d+(\.\d+)?\s*%", "plus-percent"),
    (r"月\s*\d+\s*%", "monthly-percent"),
]

hits = []
for pat, label in NG_PATTERNS:
    m = re.search(pat, payload_text)
    if m:
        s = max(0, m.start() - 20)
        e = min(len(payload_text), m.end() + 20)
        snippet = payload_text[s:e].replace("\n", " ")
        hits.append((label, m.group(0), snippet))

if hits and not has_safe:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("=== SNS TRUTH VIOLATION ===\n")
        f.write(f"Timestamp: {ts}\n")
        f.write(f"Tool: {tool_name}\n")
        f.write(f"Path: {path}\n")
        f.write(f"Command: {cmd[:200]}\n")
        f.write("Hits:\n")
        for label, match, snippet in hits:
            f.write(f"  - [{label}] '{match}' ... {snippet}\n")
        f.write("---\n")
    sys.stderr.write("\n[SNS TRUTH GUARD] blocked: production-like claims detected\n\n")
    for label, match, snippet in hits[:8]:
        sys.stderr.write(f"  NG[{label}] '{match}'\n    ...{snippet}...\n")
    sys.stderr.write("\n[SNS TRUTH GUARD] Atlas/Chronos are PAPER-only. production-implying expressions forbidden.\n")
    sys.stderr.write("[SNS TRUTH GUARD] To pass:\n")
    sys.stderr.write("  1. Include one of: ペーパー/検証/デモ/シミュレーション/実績はこれから\n")
    sys.stderr.write("  2. Remove monetary/win-rate numbers; keep narrative only\n")
    sys.stderr.write("  3. Emergency bypass: env SNS_TRUTH_BYPASS=1 (user approval required)\n")
    sys.stderr.write(f"[SNS TRUTH GUARD] violation log: {LOG_FILE}\n\n")
    sys.exit(2)

sys.exit(0)
