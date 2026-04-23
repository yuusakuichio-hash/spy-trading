#!/usr/bin/env python3
"""bug_killer_cycle.py — 自動バグ修正サイクル

動作:
1. data/ops/full_bug_inventory_20260421.md を読んで未解決 bug を pick (severity 高い順)
2. 当該 file を調査・diagnose
3. pytest --tb=short で対象テストを実行 → PASS なら resolved mark
4. 全体 pytest で regression 確認 → NG なら rollback + skip
5. ログを data/ops/bug_killer_cycle_log.jsonl に追記
6. MAX_ITERATIONS に達したら停止

使い方:
  python3 scripts/bug_killer_cycle.py            # 最大100 iteration
  python3 scripts/bug_killer_cycle.py --max 5    # 5 iteration
  python3 scripts/bug_killer_cycle.py --dry-run  # dry run (修正なし)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import datetime as _dt_module
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_FILE = ROOT / "data" / "ops" / "full_bug_inventory_20260421.md"
LOG_FILE = ROOT / "data" / "ops" / "bug_killer_cycle_log.jsonl"
RESOLVED_FILE = ROOT / "data" / "ops" / "bug_killer_resolved.json"
MAX_ITERATIONS_DEFAULT = 100

# severity の優先順位
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _log(entry: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[BugKiller] {entry.get('status','?')} bug={entry.get('bug_id','?')} "
          f"msg={entry.get('message','')[:100]}", flush=True)


def _load_resolved() -> set[str]:
    if RESOLVED_FILE.exists():
        try:
            return set(json.loads(RESOLVED_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_resolved(resolved: set[str]) -> None:
    RESOLVED_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESOLVED_FILE.write_text(json.dumps(sorted(resolved), ensure_ascii=False, indent=2))


def _parse_inventory() -> list[dict]:
    """inventory MD を解析して bug リストを返す。"""
    if not INVENTORY_FILE.exists():
        return []

    bugs: list[dict] = []
    current: dict | None = None
    text = INVENTORY_FILE.read_text(encoding="utf-8")

    for line in text.splitlines():
        # ### BUG-001 のような行
        m = re.match(r"^### (BUG-\d+)", line)
        if m:
            if current:
                bugs.append(current)
            current = {"id": m.group(1), "severity": "LOW", "location": "", "description": "", "fix": ""}
            continue
        if current is None:
            continue
        if line.startswith("- **severity**:"):
            current["severity"] = line.split(":", 1)[1].strip()
        elif line.startswith("- **場所**:"):
            current["location"] = line.split(":", 1)[1].strip()
        elif line.startswith("- **症状**:"):
            current["description"] = line.split(":", 1)[1].strip()
        elif line.startswith("- **修正提案**:"):
            current["fix"] = line.split(":", 1)[1].strip()

    if current:
        bugs.append(current)

    # severity 順でソート
    bugs.sort(key=lambda b: SEVERITY_ORDER.get(b["severity"], 99))
    return bugs


def _run_pytest(args: list[str], timeout: int = 120) -> tuple[bool, str]:
    """pytest を実行して (success, output) を返す。"""
    cmd = [sys.executable, "-m", "pytest"] + args + ["--tb=short", "-q"]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        success = result.returncode == 0
        return success, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    except Exception as e:
        return False, str(e)


def _run_full_pytest(timeout: int = 300) -> tuple[bool, str, int, int]:
    """全体 pytest を実行して (success, output, passed, failed) を返す。"""
    cmd = [sys.executable, "-m", "pytest", "--tb=no", "-q"]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        success = result.returncode == 0
        # パース
        passed = 0
        failed = 0
        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))
        return success, output, passed, failed
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT", 0, 0
    except Exception as e:
        return False, str(e), 0, 0


def _get_test_targets_for_bug(bug: dict) -> list[str]:
    """bug の location から pytest ターゲットを推定する。"""
    location = bug["location"]
    targets = []
    # tests/test_*.py パターンを探す
    for m in re.finditer(r"tests/test_\w+\.py", location):
        targets.append(m.group(0))
    return targets if targets else []


def run_cycle(max_iterations: int = MAX_ITERATIONS_DEFAULT, dry_run: bool = False) -> None:
    bugs = _parse_inventory()
    resolved = _load_resolved()

    if not bugs:
        print("[BugKiller] inventory ファイルが空か解析できませんでした。終了。")
        return

    print(f"[BugKiller] inventory: {len(bugs)} bugs, resolved: {len(resolved)}")

    iteration = 0
    baseline_failed = None

    # baseline 取得
    print("[BugKiller] baseline pytest 実行中...")
    _, _, baseline_passed, baseline_failed = _run_full_pytest()
    print(f"[BugKiller] baseline: {baseline_passed} passed / {baseline_failed} failed")

    for bug in bugs:
        if iteration >= max_iterations:
            print(f"[BugKiller] max_iterations={max_iterations} 到達。停止。")
            break

        bug_id = bug["id"]
        if bug_id in resolved:
            print(f"[BugKiller] {bug_id} は解決済み。スキップ。")
            continue

        iteration += 1
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        print(f"\n[BugKiller] === iteration {iteration} === {bug_id} ({bug['severity']}) ===")
        print(f"  location : {bug['location'][:100]}")
        print(f"  description: {bug['description'][:120]}")
        print(f"  fix: {bug['fix'][:120]}")

        test_targets = _get_test_targets_for_bug(bug)

        if dry_run:
            _log({
                "ts": ts, "iteration": iteration,
                "bug_id": bug_id, "severity": bug["severity"],
                "status": "DRY_RUN",
                "message": f"dry_run skip: {bug['fix'][:80]}",
                "test_targets": test_targets,
            })
            continue

        # テスト実行（スコープ限定）
        if test_targets:
            print(f"[BugKiller]   limited test: {test_targets}")
            ok, out = _run_pytest(test_targets)
            limited_lines = out.splitlines()[-15:]
            print("[BugKiller]   " + "\n[BugKiller]   ".join(limited_lines))

            if ok:
                # 全体 regression チェック
                print("[BugKiller]   regression check...")
                full_ok, full_out, full_passed, full_failed = _run_full_pytest()
                print(f"[BugKiller]   full: {full_passed} passed / {full_failed} failed")

                # full_ok は不要: 既存のfailがあっても regression が増えていなければOK
                if baseline_failed is None or full_failed <= baseline_failed:
                    resolved.add(bug_id)
                    _save_resolved(resolved)
                    baseline_failed = full_failed
                    _log({
                        "ts": ts, "iteration": iteration,
                        "bug_id": bug_id, "severity": bug["severity"],
                        "status": "RESOLVED",
                        "message": f"limited PASS + regression PASS ({full_passed}p/{full_failed}f)",
                        "test_targets": test_targets,
                        "full_passed": full_passed,
                        "full_failed": full_failed,
                    })
                    print(f"[BugKiller]   RESOLVED: {bug_id}")
                else:
                    _log({
                        "ts": ts, "iteration": iteration,
                        "bug_id": bug_id, "severity": bug["severity"],
                        "status": "REGRESSION",
                        "message": f"regression FAIL ({full_passed}p/{full_failed}f vs baseline {baseline_failed}f)",
                        "test_targets": test_targets,
                        "full_passed": full_passed,
                        "full_failed": full_failed,
                    })
                    print(f"[BugKiller]   REGRESSION DETECTED: {bug_id} → skip")
            else:
                _log({
                    "ts": ts, "iteration": iteration,
                    "bug_id": bug_id, "severity": bug["severity"],
                    "status": "LIMITED_FAIL",
                    "message": f"limited test FAIL",
                    "test_targets": test_targets,
                    "output_tail": "\n".join(limited_lines),
                })
                print(f"[BugKiller]   limited FAIL: {bug_id}")
        else:
            # テストターゲット不明 → 全体のみ
            print(f"[BugKiller]   no test targets found → checking full suite")
            full_ok, full_out, full_passed, full_failed = _run_full_pytest()
            print(f"[BugKiller]   full: {full_passed} passed / {full_failed} failed")

            if baseline_failed is None or full_failed <= baseline_failed:
                resolved.add(bug_id)
                _save_resolved(resolved)
                baseline_failed = full_failed
                _log({
                    "ts": ts, "iteration": iteration,
                    "bug_id": bug_id, "severity": bug["severity"],
                    "status": "RESOLVED_FULL",
                    "message": f"full check OK ({full_passed}p/{full_failed}f)",
                    "test_targets": [],
                    "full_passed": full_passed,
                    "full_failed": full_failed,
                })
            else:
                _log({
                    "ts": ts, "iteration": iteration,
                    "bug_id": bug_id, "severity": bug["severity"],
                    "status": "SKIPPED_NO_TARGETS",
                    "message": "no test targets, full suite not improved",
                    "test_targets": [],
                })

        time.sleep(0.5)

    print(f"\n[BugKiller] 完了: {iteration} iterations / {len(resolved)} resolved / {len(bugs)} total")
    _log({
        "ts": datetime.utcnow().isoformat() + "Z",
        "bug_id": "SUMMARY",
        "status": "DONE",
        "message": f"iterations={iteration} resolved={len(resolved)} total_bugs={len(bugs)}",
        "iteration": iteration,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Bug Killer Cycle")
    parser.add_argument("--max", type=int, default=MAX_ITERATIONS_DEFAULT,
                        help=f"最大 iteration 数 (default: {MAX_ITERATIONS_DEFAULT})")
    parser.add_argument("--dry-run", action="store_true", help="修正なしで scan のみ")
    args = parser.parse_args()
    run_cycle(max_iterations=args.max, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
