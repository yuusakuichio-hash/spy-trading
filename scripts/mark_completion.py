#!/usr/bin/env python3
"""
mark_completion.py <memory_path> <commit_hash>
指定されたメモリパスの pending エントリを resolved=true にマークする。
commit_hash に 5行以上の変更が含まれることを git log --stat で確認する。
fake resolve 防止のため commit 内容の最小変更量チェックを行う。
"""
import sys, json, os, subprocess
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
PENDING_PATH = f"{BASE}/data/pending_completions.jsonl"
LOG_PATH = f"{BASE}/data/logs/discipline_violations.log"
JST = timezone(timedelta(hours=9))

def log_event(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    with open(LOG_PATH, "a") as f:
        f.write(f"[MARK_COMPLETION] {ts} {msg}\n")

def load_jsonl(path):
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries

def save_jsonl(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

def verify_commit(commit_hash):
    """commit が 5行以上の変更を含むか確認"""
    try:
        result = subprocess.run(
            ["git", "-C", BASE, "show", "--stat", commit_hash],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return False, f"git show failed: {result.stderr[:200]}"
        output = result.stdout
        # "X insertions" or "X deletions" を探す
        import re
        insertions = sum(int(x) for x in re.findall(r"(\d+) insertion", output))
        deletions = sum(int(x) for x in re.findall(r"(\d+) deletion", output))
        total_changes = insertions + deletions
        if total_changes < 5:
            return False, f"変更行数が少なすぎます: {total_changes}行 (最低5行必要)"
        return True, f"変更確認OK: +{insertions}/-{deletions} lines"
    except Exception as e:
        return False, f"検証エラー: {e}"

def main():
    if len(sys.argv) < 3:
        print("Usage: mark_completion.py <memory_path> <commit_hash>")
        sys.exit(1)

    memory_path = sys.argv[1]
    commit_hash = sys.argv[2]

    # commit 検証
    ok, reason = verify_commit(commit_hash)
    if not ok:
        print(f"[MARK_COMPLETION] REJECTED: {reason}")
        log_event(f"REJECTED: memory={memory_path} commit={commit_hash} reason={reason}")
        sys.exit(1)

    pending = load_jsonl(PENDING_PATH)
    found = False
    for e in pending:
        if e.get("memory_path") == memory_path and not e.get("resolved", False):
            e["resolved"] = True
            e["resolved_ts"] = datetime.now(JST).isoformat()
            e["resolved_commit"] = commit_hash
            e["resolved_reason"] = reason
            found = True
            break

    if not found:
        # パスの部分一致でも検索
        for e in pending:
            if memory_path in e.get("memory_path", "") and not e.get("resolved", False):
                e["resolved"] = True
                e["resolved_ts"] = datetime.now(JST).isoformat()
                e["resolved_commit"] = commit_hash
                e["resolved_reason"] = reason
                found = True
                break

    if not found:
        print(f"[MARK_COMPLETION] NOT FOUND: {memory_path} (未解決エントリなし)")
        log_event(f"NOT_FOUND: memory={memory_path}")
        sys.exit(1)

    save_jsonl(PENDING_PATH, pending)
    print(f"[MARK_COMPLETION] RESOLVED: {memory_path}")
    print(f"[MARK_COMPLETION] Commit: {commit_hash} ({reason})")
    log_event(f"RESOLVED: memory={memory_path} commit={commit_hash}")

if __name__ == "__main__":
    main()
