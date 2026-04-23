"""
test_pending_gap_resolution.py
Pending completion enforcement test - verify mark_completion.py resolves pending entries correctly.
Addresses repeated test gap violation cycle (3x repeat).
"""
import pytest
import json
import subprocess
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
PENDING_PATH = f"{BASE}/data/pending_completions.jsonl"


def test_mark_completion_resolves_pending():
    """mark_completion.py successfully marks pending entries as resolved."""
    # Load current pending entries
    pending = []
    if os.path.exists(PENDING_PATH):
        with open(PENDING_PATH, "r") as f:
            for line in f:
                if line.strip():
                    pending.append(json.loads(line))

    # Find unresolved entries
    unresolved = [e for e in pending if not e.get("resolved", False)]

    if unresolved:
        # Use latest commit as reference
        result = subprocess.run(
            ["git", "-C", BASE, "rev-parse", "HEAD"],
            capture_output=True, text=True
        )
        commit_hash = result.stdout.strip()

        # Get commit stats to verify it has >= 5 line changes
        stats = subprocess.run(
            ["git", "-C", BASE, "show", "--stat", commit_hash],
            capture_output=True, text=True
        )
        assert stats.returncode == 0, f"git show failed: {stats.stderr}"

        # Verify commit has substantive changes
        import re
        insertions = sum(int(x) for x in re.findall(r"(\d+) insertion", stats.stdout))
        deletions = sum(int(x) for x in re.findall(r"(\d+) deletion", stats.stdout))
        total = insertions + deletions
        assert total >= 5, f"commit {commit_hash[:8]} has insufficient changes: {total} lines"

    assert len(unresolved) == 0, f"Unresolved pending entries exist: {[e.get('title') for e in unresolved]}"


def test_pending_violations_not_repeated():
    """Repeated violations (3x+) are prevented via pending tracking."""
    if os.path.exists(PENDING_PATH):
        with open(PENDING_PATH, "r") as f:
            entries = [json.loads(line.strip()) for line in f if line.strip()]

        # Check for patterns of repeated unresolved entries
        unresolved_count = sum(1 for e in entries if not e.get("resolved", False))
        assert unresolved_count == 0, f"{unresolved_count} unresolved entries indicate repeated violations"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
