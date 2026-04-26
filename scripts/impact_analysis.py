#!/usr/bin/env python3
"""Change Impact Analysis (CIA) — 変更ファイルから影響箇所を列挙。

用途:
  Builder 修正の漏れ検出・Redteam 攻撃経路の特定補助。

手法:
  Python AST ベースの簡易 call graph 解析 +
  grep ベースの参照箇所探索。

使い方:
  python3 scripts/impact_analysis.py atlas_v3/ops/monitor.py
  python3 scripts/impact_analysis.py --symbol _check_daily_loss atlas_v3/ops/monitor.py

規律:
  最終判定は常に pytest tests/ 全件走行（feedback_no_selective_testing.md）。
  本スクリプトは開発中の絞り込みツールとして使用。
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def extract_symbols(file_path: Path) -> list[str]:
    """ファイル内の関数・クラス・メソッド名を抽出。"""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, FileNotFoundError) as exc:
        print(f"[WARN] {file_path}: {exc}", file=sys.stderr)
        return []

    symbols: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(node.name)
    return symbols


def find_references(symbol: str, exclude: Path | None = None) -> list[str]:
    """シンボルを参照している箇所を grep で列挙。"""
    cmd = [
        "git",
        "grep",
        "-n",
        "--word-regexp",
        symbol,
        "--",
        "*.py",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    lines = result.stdout.strip().splitlines()
    if exclude:
        exclude_str = str(exclude.relative_to(PROJECT_ROOT))
        lines = [line for line in lines if not line.startswith(exclude_str)]
    return lines


def find_related_tests(file_path: Path) -> list[str]:
    """変更ファイルに紐付いたテストファイルを推定。"""
    stem = file_path.stem
    test_patterns = [
        f"tests/test_{stem}.py",
        f"tests/test_{stem}_*.py",
        f"tests/**/test_*{stem}*.py",
    ]
    found: list[str] = []
    for pattern in test_patterns:
        for p in PROJECT_ROOT.glob(pattern):
            if p.is_file():
                found.append(str(p.relative_to(PROJECT_ROOT)))

    module_name = ".".join(file_path.relative_to(PROJECT_ROOT).with_suffix("").parts)
    cmd = ["git", "grep", "-l", module_name, "--", "tests/*.py", "tests/**/*.py"]
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        for line in result.stdout.strip().splitlines():
            if line and line not in found:
                found.append(line)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return sorted(set(found))


def analyze(file_path: Path, symbol: str | None = None) -> dict:
    """影響分析を実行。"""
    report: dict = {
        "target_file": str(file_path.relative_to(PROJECT_ROOT)),
        "symbols_defined": [],
        "references": {},
        "related_tests": [],
    }

    if not file_path.exists():
        report["error"] = f"File not found: {file_path}"
        return report

    symbols = [symbol] if symbol else extract_symbols(file_path)
    report["symbols_defined"] = symbols

    for sym in symbols:
        if sym.startswith("_") and not sym.startswith("__"):
            report["references"][sym] = find_references(sym, exclude=file_path)
        elif not sym.startswith("_"):
            report["references"][sym] = find_references(sym, exclude=file_path)

    report["related_tests"] = find_related_tests(file_path)
    return report


def format_report(report: dict) -> str:
    lines = [
        "=" * 70,
        "Change Impact Analysis (CIA) Report",
        "=" * 70,
        f"Target: {report['target_file']}",
        "",
    ]

    if "error" in report:
        lines.append(f"ERROR: {report['error']}")
        return "\n".join(lines)

    lines.append(f"Defined symbols ({len(report['symbols_defined'])}):")
    for sym in report["symbols_defined"]:
        refs = report["references"].get(sym, [])
        lines.append(f"  - {sym}: {len(refs)} external reference(s)")
        for ref in refs[:5]:
            lines.append(f"      {ref}")
        if len(refs) > 5:
            lines.append(f"      ... ({len(refs) - 5} more)")

    lines.append("")
    lines.append(f"Related tests ({len(report['related_tests'])}):")
    for test in report["related_tests"]:
        lines.append(f"  - {test}")

    lines.append("")
    lines.append("-" * 70)
    lines.append("NOTE: 最終判定は常に pytest tests/ 全件走行（feedback_no_selective_testing.md）")
    lines.append("      本レポートは開発中の絞り込みツール。関連テストのみの走行で done 宣言不可。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Change Impact Analysis (CIA)")
    parser.add_argument("file", help="解析対象ファイル（プロジェクトルートからの相対 or 絶対パス）")
    parser.add_argument("--symbol", help="特定シンボルのみ解析")
    args = parser.parse_args()

    target = Path(args.file)
    if not target.is_absolute():
        target = PROJECT_ROOT / target

    report = analyze(target, symbol=args.symbol)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
