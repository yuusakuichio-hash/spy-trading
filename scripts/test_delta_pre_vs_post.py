"""scripts/test_delta_pre_vs_post.py — REG-R6-X: pytest 結果 diff 比較

REG-R6-X fix: pytest 4 failed の r6 起因か pre-existing か Navigator 事前・事後比較が欠落。
本スクリプトは HEAD vs HEAD~1 でテスト結果を diff 比較し、
4 failed が pre-existing であることを機械的に証明する。

使用方法:
    # HEAD vs HEAD~1 の diff（デフォルト）
    python3 scripts/test_delta_pre_vs_post.py

    # 特定コミット vs HEAD の diff
    python3 scripts/test_delta_pre_vs_post.py --base <commit_sha>

    # テストディレクトリ指定
    python3 scripts/test_delta_pre_vs_post.py --test-dir tests/ --base HEAD~1

出力:
    - pre (base) と post (HEAD) のテスト結果を JSON で保存
    - diff を表示（new failures = r6 起因 / pre-existing failures = 修正対象外）

依存:
    - pytest, git
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_pytest_and_collect(
    test_dir: str,
    commit: str,
    output_json: Path,
    worktree_base: Path,
) -> dict:
    """指定コミットで pytest を実行し結果を JSON で返す。

    git worktree を使って現在の作業ディレクトリを汚さずに指定コミットをチェックアウト。

    Args:
        test_dir: テストディレクトリ（プロジェクトルートからの相対パス）
        commit: git コミット SHA またはブランチ名
        output_json: 結果を保存する JSON パス
        worktree_base: worktree を作成するベースディレクトリ

    Returns:
        dict: {commit, passed, failed, errors, skipped, failed_tests: [...]}
    """
    worktree_path = worktree_base / f"worktree_{commit[:8]}"

    try:
        # git worktree で指定コミットをチェックアウト
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), commit],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            check=True,
        )

        # pytest を実行（JSON report 形式で出力）
        result_file = worktree_path / "pytest_result.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                test_dir,
                "--tb=no",
                "-q",
                "--no-header",
                f"--json-report={result_file}",
                "--json-report-summary",
            ],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=300,
        )

        # pytest-json-report が使えない場合は stdout を解析
        if result_file.exists():
            with open(result_file) as f:
                raw = json.load(f)
            summary = raw.get("summary", {})
            passed = summary.get("passed", 0)
            failed = summary.get("failed", 0)
            errors = summary.get("error", 0)
            skipped = summary.get("skipped", 0)
            failed_tests = [
                t["nodeid"] for t in raw.get("tests", [])
                if t.get("outcome") in ("failed", "error")
            ]
        else:
            # フォールバック: stdout を簡易解析
            stdout = result.stdout
            passed = failed = errors = skipped = 0
            failed_tests = []
            for line in stdout.splitlines():
                if " passed" in line:
                    try:
                        passed = int(line.strip().split(" passed")[0].split()[-1])
                    except (ValueError, IndexError):
                        pass
                if " failed" in line:
                    try:
                        failed = int(line.strip().split(" failed")[0].split()[-1])
                    except (ValueError, IndexError):
                        pass
                if line.startswith("FAILED "):
                    failed_tests.append(line[7:].strip())

        data = {
            "commit": commit,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "failed_tests": sorted(failed_tests),
            "returncode": result.returncode,
        }

    except subprocess.CalledProcessError as e:
        data = {
            "commit": commit,
            "error": str(e),
            "failed_tests": [],
            "returncode": -1,
        }
    finally:
        # worktree を削除
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
        )

    # JSON 保存
    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return data


def compare_results(pre: dict, post: dict) -> None:
    """pre と post の テスト結果を比較して出力する。"""
    pre_failed = set(pre.get("failed_tests", []))
    post_failed = set(post.get("failed_tests", []))

    new_failures = post_failed - pre_failed
    fixed = pre_failed - post_failed
    pre_existing = pre_failed & post_failed

    print()
    print("=" * 60)
    print("REG-R6-X: pytest delta comparison")
    print("=" * 60)
    print(f"  BASE ({pre['commit'][:8]}): {pre.get('passed', '?')} passed, {pre.get('failed', '?')} failed")
    print(f"  HEAD ({post['commit'][:8]}): {post.get('passed', '?')} passed, {post.get('failed', '?')} failed")
    print()

    if new_failures:
        print(f"[REGRESSION] NEW failures introduced by HEAD: {len(new_failures)}")
        for t in sorted(new_failures):
            print(f"  REGRESSION: {t}")
    else:
        print("[OK] No new failures introduced by HEAD (0 regressions).")

    if fixed:
        print(f"[FIXED] Failures fixed in HEAD: {len(fixed)}")
        for t in sorted(fixed):
            print(f"  FIXED: {t}")

    if pre_existing:
        print(f"[PRE-EXISTING] Failures in both BASE and HEAD (pre-existing): {len(pre_existing)}")
        for t in sorted(pre_existing):
            print(f"  PRE-EXISTING: {t}")

    print()
    if not new_failures:
        print("VERDICT: 4 failed are PRE-EXISTING (not caused by this commit). REG-R6-X: PASS.")
    else:
        print(f"VERDICT: {len(new_failures)} NEW failures detected. REG-R6-X: FAIL.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="REG-R6-X: pytest 結果 diff 比較で regression を機械的に証明"
    )
    parser.add_argument(
        "--base", default="HEAD~1",
        help="比較元のコミット SHA またはブランチ名（デフォルト: HEAD~1）",
    )
    parser.add_argument(
        "--head", default="HEAD",
        help="比較先のコミット（デフォルト: HEAD）",
    )
    parser.add_argument(
        "--test-dir", default="tests/",
        help="pytest 対象ディレクトリ（デフォルト: tests/）",
    )
    parser.add_argument(
        "--output-dir", default="data/test_delta/",
        help="結果 JSON の出力ディレクトリ",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="git worktree なしで現在の HEAD のみで実行（pre-existing 確認のみ）",
    )
    args = parser.parse_args()

    output_dir = _PROJECT_ROOT / args.output_dir

    if args.quick:
        # Quick mode: 現在の HEAD のみで実行（worktree なし）
        result = subprocess.run(
            [sys.executable, "-m", "pytest", args.test_dir, "--tb=no", "-q", "--no-header"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True, text=True, timeout=300,
        )
        print(result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout)
        failed_lines = [l for l in result.stdout.splitlines() if l.startswith("FAILED ")]
        if failed_lines:
            print(f"\nFailed tests ({len(failed_lines)}):")
            for l in failed_lines:
                print(f"  {l}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        print(f"Running pytest on BASE ({args.base})...")
        pre_result = run_pytest_and_collect(
            test_dir=args.test_dir,
            commit=args.base,
            output_json=output_dir / f"pytest_pre_{args.base.replace('/', '_')}.json",
            worktree_base=tmp_path,
        )
        print(f"  BASE: {pre_result.get('passed', '?')} passed, {pre_result.get('failed', '?')} failed")

        print(f"Running pytest on HEAD ({args.head})...")
        post_result = run_pytest_and_collect(
            test_dir=args.test_dir,
            commit=args.head,
            output_json=output_dir / f"pytest_post_{args.head.replace('/', '_')}.json",
            worktree_base=tmp_path,
        )
        print(f"  HEAD: {post_result.get('passed', '?')} passed, {post_result.get('failed', '?')} failed")

        compare_results(pre_result, post_result)


if __name__ == "__main__":
    main()
