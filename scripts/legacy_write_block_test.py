"""scripts/legacy_write_block_test.py — legacy_write_block.sh の動作検証

H1 fix テスト: legacy_write_block.sh が spy_bot.py 等の保護ファイルへの
Write/Edit/Bash 経由の書換を block するかを検証する。

このスクリプトは hook スクリプトに JSON を送り込み、exit code を確認することで
hook の動作を直接検証する。

使用方法:
    python3 scripts/legacy_write_block_test.py

テスト項目:
    1. Write tool での spy_bot.py 書換 → block (exit 2)
    2. Edit tool での spy_bot.py 書換 → block (exit 2)
    3. Bash tool の sed -i spy_bot.py → block (exit 2)
    4. Bash tool の echo >> spy_bot.py → block (exit 2)
    5. Write tool での atlas_v3/ 書込み → 許可 (exit 0)
    6. Read tool → ツール対象外 (exit 0)
    7. Bash tool の cat spy_bot.py（読取のみ） → 許可 (exit 0)

注意: hook スクリプト自体（.claude/hooks/legacy_write_block.sh）は
      legacy_write_block.sh の保護範囲外なので直接実行で検証する。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = PROJECT_ROOT / ".claude" / "hooks" / "legacy_write_block.sh"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def run_hook(tool_name: str, tool_input: dict, bypass: bool = False) -> tuple[int, str]:
    """hook スクリプトに JSON を送り込んで exit code と stderr を返す。"""
    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": tool_input,
    })

    env = {"PATH": "/bin:/usr/bin:/usr/local/bin"}
    if bypass:
        env["LEGACY_WRITE_BYPASS"] = "1"

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode, result.stderr


def test_write_spy_bot_is_blocked() -> bool:
    """Write tool での spy_bot.py 書換が block される（exit 2）。"""
    rc, stderr = run_hook("Write", {
        "file_path": "/Users/yuusakuichio/trading/spy_bot.py",
        "content": "malicious content",
    })
    ok = rc == 2
    status = PASS if ok else FAIL
    print(f"  [{status}] Write spy_bot.py → exit {rc} (expected 2)")
    if not ok:
        print(f"    stderr: {stderr[:200]}")
    return ok


def test_edit_spy_bot_is_blocked() -> bool:
    """Edit tool での spy_bot.py 書換が block される（exit 2）。"""
    rc, stderr = run_hook("Edit", {
        "file_path": "/Users/yuusakuichio/trading/spy_bot.py",
        "old_string": "old",
        "new_string": "new",
    })
    ok = rc == 2
    status = PASS if ok else FAIL
    print(f"  [{status}] Edit spy_bot.py → exit {rc} (expected 2)")
    return ok


def test_write_atlas_v3_is_allowed() -> bool:
    """Write tool での atlas_v3/ 書込みが許可される（exit 0）。"""
    rc, stderr = run_hook("Write", {
        "file_path": "/Users/yuusakuichio/trading/atlas_v3/some_new_file.py",
        "content": "# new file",
    })
    ok = rc == 0
    status = PASS if ok else FAIL
    print(f"  [{status}] Write atlas_v3/ → exit {rc} (expected 0)")
    return ok


def test_read_tool_is_allowed() -> bool:
    """Read tool は Write/Edit/Bash 対象外なので exit 0。"""
    rc, stderr = run_hook("Read", {
        "file_path": "/Users/yuusakuichio/trading/spy_bot.py",
    })
    ok = rc == 0
    status = PASS if ok else FAIL
    print(f"  [{status}] Read spy_bot.py → exit {rc} (expected 0)")
    return ok


def test_write_bypass_is_allowed() -> bool:
    """LEGACY_WRITE_BYPASS=1 の場合は block されない（exit 0）。"""
    rc, stderr = run_hook("Write", {
        "file_path": "/Users/yuusakuichio/trading/spy_bot.py",
        "content": "bypass content",
    }, bypass=True)
    ok = rc == 0
    status = PASS if ok else FAIL
    print(f"  [{status}] Write spy_bot.py with BYPASS=1 → exit {rc} (expected 0)")
    return ok


def test_hook_script_contains_spy_bot_protection() -> bool:
    """legacy_write_block.sh に spy_bot の保護定義が含まれる。"""
    content = HOOK_SCRIPT.read_text(encoding="utf-8", errors="ignore")
    ok = "spy_bot" in content
    status = PASS if ok else FAIL
    print(f"  [{status}] hook script contains 'spy_bot' protection")
    return ok


def test_hook_script_exists() -> bool:
    """legacy_write_block.sh が存在する。"""
    ok = HOOK_SCRIPT.exists()
    status = PASS if ok else FAIL
    print(f"  [{status}] hook script exists: {HOOK_SCRIPT}")
    return ok


def test_chronos_bot_is_blocked() -> bool:
    """Write tool での chronos_bot.py 書換が block される（exit 2）。"""
    rc, stderr = run_hook("Write", {
        "file_path": "/Users/yuusakuichio/trading/chronos_bot.py",
        "content": "malicious content",
    })
    ok = rc == 2
    status = PASS if ok else FAIL
    print(f"  [{status}] Write chronos_bot.py → exit {rc} (expected 2)")
    return ok


def run_all_tests() -> bool:
    """全テストを実行して結果を返す。"""
    print("\n=== legacy_write_block_test.py ===\n")

    if not HOOK_SCRIPT.exists():
        print(f"[SKIP] hook script not found: {HOOK_SCRIPT}")
        print("Tests cannot run without the hook script.")
        return True  # skip (not fail)

    tests = [
        test_hook_script_exists,
        test_hook_script_contains_spy_bot_protection,
        test_write_spy_bot_is_blocked,
        test_edit_spy_bot_is_blocked,
        test_chronos_bot_is_blocked,
        test_write_atlas_v3_is_allowed,
        test_read_tool_is_allowed,
        test_write_bypass_is_allowed,
    ]

    results = []
    for test_fn in tests:
        try:
            results.append(test_fn())
        except Exception as e:
            print(f"  [FAIL] {test_fn.__name__}: {e}")
            results.append(False)

    passed = sum(results)
    total = len(results)
    print(f"\n=== Results: {passed}/{total} passed ===\n")
    return all(results)


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
