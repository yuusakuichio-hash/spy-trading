#!/usr/bin/env python3
"""
scripts/backtest_validator.py — Blinded Backtest Pre-registration Validator

バックテスト実行時に GitHub Issue の事前登録内容と照合し、
基準未達・サンプルサイズ不足・メトリック不一致を警告/エラーとして出力する。

使い方:
    python3 scripts/backtest_validator.py \\
        --prereg-issue 42 \\
        --results-file backtest_results.csv \\
        --primary-metric sharpe_ratio \\
        --threshold 1.2

    --prereg-issue が指定されていない場合でも --primary-metric / --threshold で
    ローカル検証のみ実行できる（GitHub APIなしモード）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ── 定数 ──────────────────────────────────────────────────────────────────────

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_WARN = 2   # 基準未達だがブロックしない警告レベル

METRIC_COLUMNS_REQUIRED = ["trade_date", "pnl"]   # results CSV の必須カラム
DEFAULT_MIN_TRADES = 30   # 最低トレード数の下限（事前登録で指定がない場合のデフォルト）


# ── GitHub Issue 取得 ─────────────────────────────────────────────────────────

def fetch_issue_body(issue_number: int, repo: Optional[str] = None) -> Optional[str]:
    """gh CLI を使って Issue 本文を取得する。"""
    try:
        cmd = ["gh", "issue", "view", str(issue_number), "--json", "body"]
        if repo:
            cmd += ["--repo", repo]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.warning(f"gh issue view failed: {result.stderr.strip()}")
            return None
        data = json.loads(result.stdout)
        return data.get("body", "")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        log.warning(f"Could not fetch issue: {e}")
        return None


def parse_prereg_from_body(body: str) -> dict:
    """Issue 本文からpre-registration フィールドを抽出する。"""
    fields: dict = {}

    # primary_metric: の行を探す
    m = re.search(r"\*\*primary_metric\*\*:\s*(.+)", body)
    if m:
        val = m.group(1).strip()
        if val and not val.startswith("<!--"):
            fields["primary_metric"] = val

    # success_threshold: の行を探す
    m = re.search(r"\*\*合格基準.*?\*\*:\s*(.+)", body)
    if m:
        val = m.group(1).strip()
        if val and not val.startswith("<!--"):
            fields["success_threshold_raw"] = val

    # min_trades: の行を探す
    m = re.search(r"\*\*最低トレード数.*?\*\*:\s*(.+)", body)
    if m:
        val = m.group(1).strip()
        digits = re.search(r"\d+", val)
        if digits:
            fields["min_trades"] = int(digits.group())

    return fields


def parse_threshold(raw: str) -> tuple[str, float]:
    """'sharpe_ratio >= 1.2' 形式の文字列をパースして (operator, value) を返す。"""
    m = re.search(r"(>=|<=|>|<|==)\s*([\d.]+)", raw)
    if not m:
        raise ValueError(f"Cannot parse threshold: '{raw}'")
    op  = m.group(1)
    val = float(m.group(2))
    return op, val


def check_threshold(actual: float, op: str, threshold: float) -> bool:
    ops = {">=": actual >= threshold, "<=": actual <= threshold,
           ">": actual > threshold,   "<": actual < threshold,
           "==": actual == threshold}
    return ops.get(op, False)


# ── 結果CSV 読み込み・メトリック計算 ─────────────────────────────────────────

def load_results(results_file: str) -> pd.DataFrame:
    path = Path(results_file)
    if not path.exists():
        raise FileNotFoundError(f"Results file not found: {results_file}")
    df = pd.read_csv(path)
    return df


def compute_metric(df: pd.DataFrame, metric: str) -> float:
    """DataFrameからメトリックを計算する。"""
    if "pnl" not in df.columns:
        raise ValueError(f"'pnl' column not found in results CSV. Columns: {list(df.columns)}")

    pnl = df["pnl"].astype(float)

    if metric == "sharpe_ratio":
        mean = pnl.mean()
        std  = pnl.std(ddof=1)
        if std == 0:
            return 0.0
        # 日次シャープ比（年率化: √252）
        return float((mean / std) * (252 ** 0.5))

    elif metric == "win_rate":
        return float((pnl > 0).sum() / len(pnl))

    elif metric == "profit_factor":
        gross_profit = pnl[pnl > 0].sum()
        gross_loss   = abs(pnl[pnl < 0].sum())
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 1.0
        return float(gross_profit / gross_loss)

    elif metric == "cagr":
        # 簡易CAGR: 累積リターンを日数ベースで年率化
        if "trade_date" not in df.columns:
            raise ValueError("'trade_date' column required for CAGR calculation")
        dates = pd.to_datetime(df["trade_date"])
        days  = (dates.max() - dates.min()).days
        if days <= 0:
            return 0.0
        cumulative = (1 + pnl).prod()
        return float(cumulative ** (365.0 / days) - 1)

    elif metric == "max_drawdown":
        equity = pnl.cumsum()
        rolling_max = equity.cummax()
        drawdown = (equity - rolling_max) / (rolling_max.abs() + 1e-10)
        return float(drawdown.min())  # 負の値（最大ドローダウン）

    elif metric == "total_pnl":
        return float(pnl.sum())

    else:
        # カラム名と一致する場合はその値の平均を返す
        if metric in df.columns:
            return float(df[metric].mean())
        raise ValueError(f"Unknown metric: '{metric}'. "
                         f"Supported: sharpe_ratio, win_rate, profit_factor, cagr, "
                         f"max_drawdown, total_pnl, or any CSV column name.")


# ── メイン検証ロジック ────────────────────────────────────────────────────────

def validate(
    results_file: str,
    primary_metric: str,
    threshold_raw: str,
    min_trades: int = DEFAULT_MIN_TRADES,
    prereg_issue: Optional[int] = None,
    repo: Optional[str] = None,
) -> int:
    """
    Returns:
        EXIT_PASS (0): 基準達成
        EXIT_WARN (2): 警告あり（サンプルサイズ不足等）
        EXIT_FAIL (1): 基準未達
    """
    warnings = []
    errors   = []

    # ── Step 1: 事前登録Issue との照合 ──────────────────────────────────────
    prereg_fields: dict = {}
    if prereg_issue is not None:
        log.info(f"Fetching pre-registration from Issue #{prereg_issue}...")
        body = fetch_issue_body(prereg_issue, repo)
        if body:
            prereg_fields = parse_prereg_from_body(body)
            log.info(f"Pre-registration fields parsed: {prereg_fields}")
        else:
            warnings.append(
                f"Could not fetch Issue #{prereg_issue}. "
                "Proceeding without pre-registration cross-check."
            )

        # primary_metric の一致確認
        if "primary_metric" in prereg_fields:
            if prereg_fields["primary_metric"] != primary_metric:
                errors.append(
                    f"[PREREG MISMATCH] primary_metric in Issue: "
                    f"'{prereg_fields['primary_metric']}' "
                    f"vs --primary-metric: '{primary_metric}'. "
                    "Using a different metric than pre-registered is prohibited."
                )

        # min_trades を事前登録値で上書き
        if "min_trades" in prereg_fields:
            min_trades = prereg_fields["min_trades"]
            log.info(f"min_trades from pre-registration: {min_trades}")

        # threshold の一致確認
        if "success_threshold_raw" in prereg_fields:
            prereg_threshold = prereg_fields["success_threshold_raw"]
            if threshold_raw not in prereg_threshold and prereg_threshold not in threshold_raw:
                warnings.append(
                    f"[THRESHOLD CHECK] Pre-registered threshold: '{prereg_threshold}' "
                    f"vs --threshold: '{threshold_raw}'. "
                    "Verify these match your pre-registration intent."
                )

    # ── Step 2: 結果CSV 読み込み ─────────────────────────────────────────────
    log.info(f"Loading results from: {results_file}")
    try:
        df = load_results(results_file)
    except FileNotFoundError as e:
        log.error(str(e))
        return EXIT_FAIL

    total_trades = len(df)
    log.info(f"Total trades in results: {total_trades}")

    # サンプルサイズ確認
    if total_trades < min_trades:
        warnings.append(
            f"[SAMPLE SIZE] {total_trades} trades < min_trades={min_trades}. "
            "Results may not be statistically meaningful."
        )

    # ── Step 3: メトリック計算 ───────────────────────────────────────────────
    log.info(f"Computing metric: {primary_metric}")
    try:
        actual_value = compute_metric(df, primary_metric)
    except (ValueError, KeyError) as e:
        log.error(f"Metric computation failed: {e}")
        return EXIT_FAIL

    log.info(f"{primary_metric} = {actual_value:.4f}")

    # ── Step 4: 閾値判定 ────────────────────────────────────────────────────
    try:
        op, threshold_val = parse_threshold(threshold_raw)
    except ValueError as e:
        log.error(str(e))
        return EXIT_FAIL

    passed = check_threshold(actual_value, op, threshold_val)

    # ── Step 5: 結果レポート出力 ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  BACKTEST VALIDATION REPORT")
    print("=" * 60)
    if prereg_issue:
        print(f"  Pre-registration Issue : #{prereg_issue}")
    print(f"  Results file           : {results_file}")
    print(f"  Total trades           : {total_trades}")
    print(f"  Primary metric         : {primary_metric}")
    print(f"  Threshold              : {threshold_raw}")
    print(f"  Actual value           : {actual_value:.4f}")
    print(f"  Result                 : {'PASS' if passed else 'FAIL'}")

    if warnings:
        print("\n  WARNINGS:")
        for w in warnings:
            print(f"    [WARN] {w}")

    if errors:
        print("\n  ERRORS (pre-registration mismatch):")
        for e in errors:
            print(f"    [ERROR] {e}")

    print("=" * 60 + "\n")

    if errors:
        return EXIT_FAIL
    if not passed:
        log.warning(f"Threshold not met: {primary_metric}={actual_value:.4f} (required {threshold_raw})")
        return EXIT_FAIL
    if warnings:
        return EXIT_WARN
    return EXIT_PASS


# ── CLI エントリポイント ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Validate backtest results against pre-registration criteria"
    )
    parser.add_argument(
        "--prereg-issue", type=int, default=None,
        help="GitHub Issue number of the pre-registration (optional)"
    )
    parser.add_argument(
        "--repo", type=str, default=None,
        help="GitHub repo (owner/name). Defaults to current repo."
    )
    parser.add_argument(
        "--results-file", type=str, required=True,
        help="Path to backtest results CSV (must have 'pnl' column)"
    )
    parser.add_argument(
        "--primary-metric", type=str, required=True,
        help="Primary metric to evaluate (sharpe_ratio, win_rate, profit_factor, cagr, etc.)"
    )
    parser.add_argument(
        "--threshold", type=str, required=True,
        help="Success threshold expression, e.g. '>= 1.2' or '> 0.55'"
    )
    parser.add_argument(
        "--min-trades", type=int, default=DEFAULT_MIN_TRADES,
        help=f"Minimum number of trades for statistical validity (default: {DEFAULT_MIN_TRADES})"
    )
    args = parser.parse_args()

    exit_code = validate(
        results_file=args.results_file,
        primary_metric=args.primary_metric,
        threshold_raw=args.threshold,
        min_trades=args.min_trades,
        prereg_issue=args.prereg_issue,
        repo=args.repo,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
