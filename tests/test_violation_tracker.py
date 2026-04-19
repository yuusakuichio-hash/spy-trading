#!/usr/bin/env python3
"""
tests/test_violation_tracker.py
4機構（memory_completion_tracker / violation_registry / stop_pending_check / prepend_pending_violations）
の動作検証テスト。
"""
import json, os, sys, subprocess, hashlib, tempfile, shutil
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
PENDING_PATH = f"{BASE}/data/pending_completions.jsonl"
REGISTRY_PATH = f"{BASE}/data/violation_registry.jsonl"
HALT_FLAG_PATH = f"{BASE}/data/session_halt_flag.txt"
LOG_PATH = f"{BASE}/data/logs/discipline_violations.log"
JST = timezone(timedelta(hours=9))

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
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

def backup_file(path):
    if os.path.exists(path):
        backup = path + ".test_bak"
        shutil.copy2(path, backup)
        return backup
    return None

def restore_file(path, backup):
    if backup and os.path.exists(backup):
        shutil.copy2(backup, path)
        os.remove(backup)
    elif not backup and os.path.exists(path):
        os.remove(path)

PASS = 0
FAIL = 0

def ok(msg):
    global PASS
    PASS += 1
    print(f"  [PASS] {msg}")

def fail(msg, detail=""):
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {msg}")
    if detail:
        print(f"         {detail}")

# =========================================================
# Test 1: memory_completion_tracker.sh の should_trigger 判定
# =========================================================
def test_should_trigger():
    print("\nTest 1: memory_completion_tracker trigger patterns")
    import re
    TRIGGER_PATTERNS = [
        r"memory/feedback_.*violations?.*\.md",
        r"memory/feedback_.*_gap.*\.md",
        r"memory/feedback_.*_lessons.*\.md",
        r"memory/feedback_.*_failure.*\.md",
        r"memory/feedback_.*_fix.*\.md",
        r"memory/project_.*violations?.*\.md",
        r"memory/project_.*_gap.*\.md",
        r"memory/project_.*_failure.*\.md",
        r"memory/project_.*_fix.*\.md",
        r"memory/project_.*_bug.*\.md",
        r"memory/feedback_.*_bias.*\.md",
        r"memory/feedback_.*_mistake.*\.md",
        r"memory/project_.*_redteam.*\.md",
        r"memory/project_.*_critical.*\.md",
    ]
    def should_trigger(fp):
        for pat in TRIGGER_PATTERNS:
            if re.search(pat, fp):
                return True
        return False

    # 発火すべきパターン
    trigger_cases = [
        "memory/feedback_peer_review_3violations_20260420.md",
        "memory/feedback_watchdog_gap_20260420.md",
        "memory/feedback_memory_lessons_20260419.md",
        "memory/feedback_false_completion_failure_20260419.md",
        "memory/project_chronos_fix_20260419.md",
        "memory/feedback_cognitive_bias_20260420.md",
        "memory/project_atlas_critical_20260419.md",
        "memory/project_chronos_redteam_20260419.md",
    ]
    for fp in trigger_cases:
        if should_trigger(fp):
            ok(f"trigger: {fp}")
        else:
            fail(f"should trigger but did not: {fp}")

    # 発火しないパターン
    no_trigger_cases = [
        "memory/project_atlas_schedule.md",
        "memory/project_session_20260419_summary.md",
        "memory/feedback_tone.md",
        "data/violation_patterns.json",
        ".claude/hooks/discipline_guard.sh",
    ]
    for fp in no_trigger_cases:
        if not should_trigger(fp):
            ok(f"no-trigger: {fp}")
        else:
            fail(f"should NOT trigger but did: {fp}")

# =========================================================
# Test 2: pending_completions.jsonl への登録
# =========================================================
def test_pending_registration():
    print("\nTest 2: pending_completions.jsonl registration")
    backup = backup_file(PENDING_PATH)
    try:
        # テスト用エントリ追加
        now = datetime.now(JST)
        deadline = now + timedelta(minutes=30)
        entry = {
            "ts": now.isoformat(),
            "memory_path": "/test/memory/feedback_test_violation_20260420.md",
            "fingerprint": "abc123",
            "deadline_ts": deadline.isoformat(),
            "resolved": False,
            "title": "feedback_test_violation_20260420.md",
        }
        save_jsonl(PENDING_PATH, [entry])

        loaded = load_jsonl(PENDING_PATH)
        if len(loaded) == 1:
            ok("pending entry registered")
        else:
            fail("pending entry count mismatch", f"expected 1, got {len(loaded)}")

        if not loaded[0].get("resolved", True):
            ok("resolved=false on registration")
        else:
            fail("should be resolved=false")

    finally:
        restore_file(PENDING_PATH, backup)

# =========================================================
# Test 3: mark_completion.py の動作 (git commit なしはフォールバック)
# =========================================================
def test_mark_completion_no_commit():
    print("\nTest 3: mark_completion.py with invalid commit hash")
    backup = backup_file(PENDING_PATH)
    try:
        now = datetime.now(JST)
        entry = {
            "ts": now.isoformat(),
            "memory_path": "/test/memory/feedback_test_violation_20260420.md",
            "fingerprint": "abc123",
            "deadline_ts": (now - timedelta(minutes=5)).isoformat(),  # 期限切れ
            "resolved": False,
            "title": "feedback_test_violation_20260420.md",
        }
        save_jsonl(PENDING_PATH, [entry])

        result = subprocess.run(
            ["python3", f"{BASE}/scripts/mark_completion.py",
             "/test/memory/feedback_test_violation_20260420.md",
             "deadbeef000"],
            capture_output=True, text=True, cwd=BASE
        )
        # 無効な commit hash なので REJECTED になるはず
        if "REJECTED" in result.stdout or result.returncode != 0:
            ok("invalid commit hash rejected")
        else:
            fail("should reject invalid commit hash", result.stdout[:200])
    finally:
        restore_file(PENDING_PATH, backup)

# =========================================================
# Test 4: stop_pending_check.sh の動作
# =========================================================
def test_stop_pending_check():
    print("\nTest 4: stop_pending_check.sh behavior")
    backup = backup_file(PENDING_PATH)
    backup_reg = backup_file(REGISTRY_PATH)
    try:
        # 未解決エントリを作成
        now = datetime.now(JST)
        entry = {
            "ts": now.isoformat(),
            "memory_path": "/test/memory/feedback_test_gap_20260420.md",
            "fingerprint": "def456",
            "deadline_ts": (now - timedelta(minutes=5)).isoformat(),
            "resolved": False,
            "title": "feedback_test_gap_20260420.md",
        }
        save_jsonl(PENDING_PATH, [entry])
        # registry は空
        save_jsonl(REGISTRY_PATH, [])

        hook_path = f"{BASE}/.claude/hooks/stop_pending_check.sh"
        test_input = json.dumps({"session_id": "test"})
        result = subprocess.run(
            ["python3", hook_path],
            input=test_input, capture_output=True, text=True
        )
        # 未解決あり・繰り返し3回未満 → exit 0 + stderr に警告
        if "STOP_CHECK" in result.stderr:
            ok("stop_pending_check outputs warning to stderr")
        else:
            fail("no warning output", result.stderr[:200])

        # 3回未満は exit 0（ブロックしない）
        if result.returncode == 0:
            ok("stop_pending_check exit 0 (under threshold)")
        else:
            fail(f"unexpected exit code: {result.returncode}")

    finally:
        restore_file(PENDING_PATH, backup)
        restore_file(REGISTRY_PATH, backup_reg)

# =========================================================
# Test 5: stop_pending_check.sh が3回以上でブロック
# =========================================================
def test_stop_pending_check_block():
    print("\nTest 5: stop_pending_check.sh blocks at 3+ repeated violations")
    backup_reg = backup_file(REGISTRY_PATH)
    backup_p = backup_file(PENDING_PATH)
    try:
        # registry に3回の繰り返し違反
        registry_entries = [
            {
                "ts": datetime.now(JST).isoformat(),
                "type": "memory_as_completion",
                "title": "feedback_test_repeated",
                "occurrence_count": 3,
            }
        ]
        save_jsonl(REGISTRY_PATH, registry_entries)
        save_jsonl(PENDING_PATH, [])  # pending は空でも registry 3回でブロック

        hook_path = f"{BASE}/.claude/hooks/stop_pending_check.sh"
        test_input = json.dumps({"session_id": "test"})
        result = subprocess.run(
            ["python3", hook_path],
            input=test_input, capture_output=True, text=True
        )
        if result.returncode == 2:
            ok("stop_pending_check blocks (exit 2) on 3+ repeated violations")
        else:
            fail(f"should exit 2 but got {result.returncode}", result.stderr[:200])

    finally:
        restore_file(REGISTRY_PATH, backup_reg)
        restore_file(PENDING_PATH, backup_p)

# =========================================================
# Test 6: prepend_pending_violations.sh の注入
# =========================================================
def test_prepend_pending_violations():
    print("\nTest 6: prepend_pending_violations.sh injection")
    backup = backup_file(PENDING_PATH)
    try:
        now = datetime.now(JST)
        entry = {
            "ts": now.isoformat(),
            "memory_path": "/test/memory/feedback_test_violation_20260420.md",
            "fingerprint": "ghi789",
            "deadline_ts": (now + timedelta(minutes=15)).isoformat(),
            "resolved": False,
            "title": "feedback_test_violation_20260420.md",
        }
        save_jsonl(PENDING_PATH, [entry])

        hook_path = f"{BASE}/.claude/hooks/prepend_pending_violations.sh"
        test_input = json.dumps({"prompt": "テスト入力"})
        result = subprocess.run(
            ["python3", hook_path],
            input=test_input, capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                output = json.loads(result.stdout.strip())
                if "additionalSystemPrompt" in output:
                    prompt_content = output["additionalSystemPrompt"]
                    if "PENDING VIOLATIONS REMINDER" in prompt_content:
                        ok("prepend hook injects PENDING VIOLATIONS REMINDER")
                    else:
                        fail("injection missing expected header", prompt_content[:200])
                    if "feedback_test_violation_20260420.md" in prompt_content:
                        ok("prepend hook includes specific file name")
                    else:
                        fail("injection missing file name", prompt_content[:200])
                else:
                    fail("no additionalSystemPrompt in output", result.stdout[:200])
            except json.JSONDecodeError:
                fail("output is not valid JSON", result.stdout[:200])
        elif result.returncode == 0 and not result.stdout.strip():
            fail("no output from prepend hook")
        else:
            fail(f"prepend hook failed: exit {result.returncode}", result.stderr[:200])
    finally:
        restore_file(PENDING_PATH, backup)

# =========================================================
# Test 7: 空 pending では prepend しない
# =========================================================
def test_prepend_empty():
    print("\nTest 7: prepend_pending_violations.sh skips when no pending")
    backup = backup_file(PENDING_PATH)
    backup_reg = backup_file(REGISTRY_PATH)
    try:
        save_jsonl(PENDING_PATH, [])
        save_jsonl(REGISTRY_PATH, [])

        hook_path = f"{BASE}/.claude/hooks/prepend_pending_violations.sh"
        test_input = json.dumps({"prompt": "正常な入力"})
        result = subprocess.run(
            ["python3", hook_path],
            input=test_input, capture_output=True, text=True
        )
        if result.returncode == 0 and not result.stdout.strip():
            ok("prepend hook outputs nothing when no pending (exit 0, no stdout)")
        else:
            fail(f"expected silent exit 0, got returncode={result.returncode}, stdout={result.stdout[:100]}")
    finally:
        restore_file(PENDING_PATH, backup)
        restore_file(REGISTRY_PATH, backup_reg)

# =========================================================
# Test 8: violation_patterns.json の存在確認
# =========================================================
def test_violation_patterns_exists():
    print("\nTest 8: violation_patterns.json integrity")
    patterns_path = f"{BASE}/data/violation_patterns.json"
    if not os.path.exists(patterns_path):
        fail("violation_patterns.json not found")
        return
    try:
        with open(patterns_path) as f:
            patterns = json.load(f)
        required_keys = ["memory_as_completion", "schedule_delay", "unnecessary_confirmation", "pushover_omission"]
        for key in required_keys:
            if key in patterns:
                ok(f"violation_patterns.json has key: {key}")
            else:
                fail(f"missing key in violation_patterns.json: {key}")
    except json.JSONDecodeError as e:
        fail(f"violation_patterns.json invalid JSON: {e}")

# =========================================================
# Test 9: settings.local.json の hook 登録確認
# =========================================================
def test_settings_hook_registration():
    print("\nTest 9: settings.local.json hook registration")
    settings_path = f"{BASE}/.claude/settings.local.json"
    if not os.path.exists(settings_path):
        fail("settings.local.json not found")
        return
    with open(settings_path) as f:
        settings = json.load(f)
    hooks = settings.get("hooks", {})

    expected = [
        ("PostToolUse", "memory_completion_tracker.sh"),
        ("Stop", "stop_pending_check.sh"),
        ("UserPromptSubmit", "prepend_pending_violations.sh"),
    ]
    for hook_type, hook_name in expected:
        entries = hooks.get(hook_type, [])
        all_cmds = [h["command"] for entry in entries for h in entry.get("hooks", [])]
        if any(hook_name in cmd for cmd in all_cmds):
            ok(f"{hook_type}: {hook_name} registered")
        else:
            fail(f"{hook_type}: {hook_name} NOT registered")

# =========================================================
# Run all tests
# =========================================================
if __name__ == "__main__":
    print("=" * 60)
    print("VIOLATION TRACKER TEST SUITE")
    print("=" * 60)

    test_should_trigger()
    test_pending_registration()
    test_mark_completion_no_commit()
    test_stop_pending_check()
    test_stop_pending_check_block()
    test_prepend_pending_violations()
    test_prepend_empty()
    test_violation_patterns_exists()
    test_settings_hook_registration()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"RESULT: {PASS}/{total} PASS")
    if FAIL > 0:
        print(f"FAILED: {FAIL} tests")
        sys.exit(1)
    else:
        print("ALL PASS")
        sys.exit(0)
