#!/usr/bin/env python3
"""Test Impact Analysis (TIA) — 変更ファイルに紐付くテストだけ列挙。

用途:
  開発中の r1→r2 サイクル高速化。Builder 修正直後の feedback loop。

規律（最上位）:
  最終判定は常に pytest tests/ 全件走行（feedback_no_selective_testing.md）。
  本スクリプトで絞った走行で「done」主張は禁止。

使い方:
  python3 scripts/test_impact.py atlas_v3/ops/monitor.py atlas_v3/ops/vault.py
  python3 scripts/test_impact.py --since HEAD~1  # git diff ベース

参考:
  Google / Microsoft CI での TIA 採用例。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def files_from_git_diff(since: str) -> list[Path]:
    cmd = ["git", "diff", "--name-only", since]
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        PROJECT_ROOT / line
        for line in result.stdout.strip().splitlines()
        if line.endswith(".py") and (PROJECT_ROOT / line).exists()
    ]


def related_tests_for(file_path: Path) -> list[str]:
    stem = file_path.stem
    tests: set[str] = set()

    for pattern in [f"tests/test_{stem}.py", f"tests/test_{stem}_*.py"]:
        for p in PROJECT_ROOT.glob(pattern):
            tests.add(str(p.relative_to(PROJECT_ROOT)))

    try:
        rel = file_path.relative_to(PROJECT_ROOT)
    except ValueError:
        return sorted(tests)

    module_name = ".".join(rel.with_suffix("").parts)
    cmd = ["git", "grep", "-l", module_name, "--", "tests/*.py", "tests/**/*.py"]
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.strip().splitlines():
        if line:
            tests.add(line)

    return sorted(tests)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Impact Analysis (TIA)")
    parser.add_argument("files", nargs="*", help="変更ファイル（複数可）")
    parser.add_argument("--since", help="git diff 基準 (例: HEAD~1, main)")
    parser.add_argument(
        "--pytest-cmd",
        action="store_true",
        help="pytest コマンド形式で出力",
    )
    args = parser.parse_args()

    targets: list[Path] = []
    if args.since:
        targets.extend(files_from_git_diff(args.since))
    for f in args.files:
        p = Path(f)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.exists():
            targets.append(p)

    if not targets:
        print("No target files provided. Use --since or pass files as args.", file=sys.stderr)
        return 1

    all_tests: set[str] = set()
    for t in targets:
        tests = related_tests_for(t)
        all_tests.update(tests)
        print(f"[{t.relative_to(PROJECT_ROOT)}] {len(tests)} related test file(s)", file=sys.stderr)

    sorted_tests = sorted(all_tests)

    if args.pytest_cmd:
        if sorted_tests:
            print("pytest " + " ".join(sorted_tests))
        else:
            print("pytest tests/")
    else:
        for t in sorted_tests:
            print(t)

    print(
        "\n"
        "# REMINDER: done 宣言時は必ず pytest tests/ 全件走行すること\n"
        "# (feedback_no_selective_testing.md / DoD #1)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
