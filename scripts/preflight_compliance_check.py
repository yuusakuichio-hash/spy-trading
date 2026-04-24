#!/usr/bin/env python3
"""scripts/preflight_compliance_check.py — Paper/Live 起動前 compliance 物理チェック

責務:
- data/ops/compliance_checklist_*.md を走査し PENDING_OWNER_APPROVAL タグが残存していれば exit 1
- runbook の必須コマンドから呼び出す（物理ブロック機構）

判断 2 (Sprint 1-B Phase B): preflight タグ分割
- 旧タグ: PENDING_OWNER_APPROVAL (全体 CRITICAL・起動 block)
- 新タグ分割:
  - PENDING_OWNER_APPROVAL_PAPER: paper モードでは WARN のみ（exit 0・起動継続）
  - PENDING_OWNER_APPROVAL_LIVE:  live モードでは CRITICAL（exit 1・起動 block）
  - PENDING_OWNER_APPROVAL:       後方互換タグ。--mode paper では WARN / --mode live では CRITICAL
- --mode paper: PENDING_OWNER_APPROVAL_PAPER + PENDING_OWNER_APPROVAL を WARN 扱い（exit 0）
- --mode live:  PENDING_OWNER_APPROVAL_LIVE + PENDING_OWNER_APPROVAL を CRITICAL 扱い（exit 1）

使用方法:
    # Paper 起動（PENDING_PAPER は WARN のみ・継続）
    python3 scripts/preflight_compliance_check.py --all --mode paper

    # Live 起動（PENDING_LIVE / PENDING は CRITICAL・ブロック）
    python3 scripts/preflight_compliance_check.py --all --mode live

    # 特定のチェックリストファイルを指定
    python3 scripts/preflight_compliance_check.py --checklist data/ops/compliance_checklist_20260423.md --mode paper

終了コード:
    0: CRITICAL 残存なし → 起動 OK（WARN 件数はログ出力のみ）
    1: CRITICAL 残存あり → 起動ブロック
    2: チェックリストファイルが存在しない → 起動ブロック（安全側）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parents[1]
_OPS_DIR = _BASE / "data" / "ops"
_DEFAULT_CHECKLIST = _OPS_DIR / "compliance_checklist_20260423.md"

# ---------------------------------------------------------------------------
# タグ検出パターン（判断 2: paper/live 分割）
# ---------------------------------------------------------------------------

# CRITICAL タグ（全モードでブロック）: PENDING_OWNER_APPROVAL_LIVE
_PATTERN_CRITICAL_LIVE = re.compile(r"(?<!`)\[PENDING_OWNER_APPROVAL_LIVE\](?!`)", re.IGNORECASE)

# WARN タグ（paper のみ許容）: PENDING_OWNER_APPROVAL_PAPER
_PATTERN_WARN_PAPER = re.compile(r"(?<!`)\[PENDING_OWNER_APPROVAL_PAPER\](?!`)", re.IGNORECASE)

# 後方互換タグ: PENDING_OWNER_APPROVAL（分割タグのどちらでもない場合）
# paper モード: WARN / live モード: CRITICAL
# S-4 fix 2026-04-24: ops agent が _CRITICAL_LIVE / _WARN_PAPER にバックティック除外を
# 入れたが LEGACY パターンは漏れていた。凡例行の `[PENDING_OWNER_APPROVAL]` も誤検知
# するため同様の (?<!`) ... (?!`) を追加（atlas-paper crash loop 再発防止）。
_PATTERN_LEGACY = re.compile(
    r"(?<!`)\[PENDING_OWNER_APPROVAL\](?!_PAPER)(?!_LIVE)(?!`)",
    re.IGNORECASE,
)

# チェックボックス [ ] + PENDING_OWNER_APPROVAL (スペース形式)
_PATTERN_CHECKBOX = re.compile(
    r"^[-*]\s+\[\s+\].*PENDING_OWNER",
    re.MULTILINE | re.IGNORECASE,
)


class PendingItem(NamedTuple):
    """検出された PENDING 項目。"""
    line_num: int
    line: str
    severity: str  # "CRITICAL" または "WARN"


def _find_pending_items(checklist_path: Path, mode: str = "live") -> list[PendingItem]:
    """チェックリストファイルから PENDING_OWNER_APPROVAL 項目を抽出する。

    Args:
        checklist_path: チェックリストファイルパス
        mode: "paper" / "live"（判断 2: タグの severity 分類に使用）

    Returns:
        PendingItem のリスト。空なら全クリア。
    """
    if not checklist_path.exists():
        return []

    items: list[PendingItem] = []
    content = checklist_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    for line_num, line in enumerate(lines, start=1):
        # CRITICAL: _LIVE タグ（全モードで CRITICAL）
        if _PATTERN_CRITICAL_LIVE.search(line):
            items.append(PendingItem(line_num, line.strip(), "CRITICAL"))
            continue
        # WARN: _PAPER タグ（paper モードでは WARN / live モードでも WARN）
        if _PATTERN_WARN_PAPER.search(line):
            items.append(PendingItem(line_num, line.strip(), "WARN"))
            continue
        # 後方互換: PENDING_OWNER_APPROVAL（タグ分割前の旧形式）
        # paper モードでは WARN / live モードでは CRITICAL
        if _PATTERN_LEGACY.search(line) or _PATTERN_CHECKBOX.search(line):
            severity = "WARN" if mode == "paper" else "CRITICAL"
            items.append(PendingItem(line_num, line.strip(), severity))
            continue

    return items


def _run_check(
    checklist_path: Path,
    verbose: bool = True,
    mode: str = "live",
) -> int:
    """単一チェックリストを検査する。

    判断 2: CRITICAL 件数 > 0 なら exit 1（ブロック）/ WARN のみなら exit 0（継続）

    Args:
        checklist_path: チェックリストファイルパス
        verbose: True なら成功時も出力する
        mode: "paper" / "live"

    Returns:
        0: CRITICAL なし → 起動 OK（WARN は出力のみ）
        1: CRITICAL 残存 → 起動ブロック
        2: ファイル不在（安全側・起動ブロック）
    """
    if not checklist_path.exists():
        print(
            f"[PREFLIGHT ERROR] Compliance checklist not found: {checklist_path}\n"
            f"  起動前に必ず compliance_checklist を作成してください。\n"
            f"  安全側: exit 2 で起動をブロックします。",
            file=sys.stderr,
        )
        return 2

    pending_items = _find_pending_items(checklist_path, mode=mode)

    critical_items = [p for p in pending_items if p.severity == "CRITICAL"]
    warn_items = [p for p in pending_items if p.severity == "WARN"]

    # WARN 項目をログ出力（ブロックはしない）
    if warn_items:
        print(
            f"[PREFLIGHT WARN] {checklist_path.name} (mode={mode}): "
            f"{len(warn_items)} 件の PENDING_OWNER_APPROVAL_PAPER が残存。"
            "起動は継続しますが、ゆうさくさんが確認するまで本番移行禁止。",
        )
        for item in warn_items:
            print(f"  [WARN] Line {item.line_num:4d}: {item.line}")

    if not critical_items:
        if verbose and not warn_items:
            print(
                f"[PREFLIGHT OK] {checklist_path.name} (mode={mode}): "
                "PENDING_OWNER_APPROVAL (CRITICAL) なし — 起動チェック合格"
            )
        elif verbose:
            print(
                f"[PREFLIGHT OK] {checklist_path.name} (mode={mode}): "
                f"CRITICAL 0 件 / WARN {len(warn_items)} 件 — 起動継続"
            )
        return 0

    # CRITICAL 項目 → 起動ブロック
    print(
        f"\n[PREFLIGHT BLOCK] {checklist_path.name} (mode={mode}):\n"
        f"  PENDING_OWNER_APPROVAL (CRITICAL) が {len(critical_items)} 件残存しています。\n"
        f"  ゆうさくさんが全項目を確認・承認するまで起動をブロックします。\n",
        file=sys.stderr,
    )
    for item in critical_items:
        print(f"  [CRITICAL] Line {item.line_num:4d}: {item.line}", file=sys.stderr)

    print(
        f"\n  解除方法: checklist の各 [PENDING_OWNER_APPROVAL] / [PENDING_OWNER_APPROVAL_LIVE] "
        "項目を確認し、\n"
        f"  確認済みの場合は [x] または「確認済」等に書き換えてください。\n"
        f"  または paper 専用項目は [PENDING_OWNER_APPROVAL_PAPER] に変更してください "
        "(paper モードでは WARN のみ)。\n"
        f"  その後このスクリプトを再実行して exit 0 になることを確認してください。",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Paper/Live 起動前 compliance checklist 物理チェック (RT-R2-H5 + 判断 2)"
    )
    parser.add_argument(
        "--checklist",
        type=Path,
        default=None,
        help=f"チェックリストファイルパス（デフォルト: {_DEFAULT_CHECKLIST}）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="data/ops/ 配下の全 compliance_checklist_*.md を走査する",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="成功時の出力を抑制する",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="live",
        help=(
            "起動モード。paper: PENDING_OWNER_APPROVAL_PAPER を WARN 扱い（起動継続）。"
            "live: PENDING_OWNER_APPROVAL_LIVE + 旧タグを CRITICAL 扱い（起動ブロック）。"
            "デフォルト: live（安全側）"
        ),
    )
    args = parser.parse_args()

    if args.all:
        # 全チェックリストをスキャン
        checklists = sorted(_OPS_DIR.glob("compliance_checklist_*.md"))
        if not checklists:
            print(
                f"[PREFLIGHT ERROR] No compliance_checklist_*.md found in {_OPS_DIR}",
                file=sys.stderr,
            )
            sys.exit(2)

        worst_exit = 0
        for cl in checklists:
            rc = _run_check(cl, verbose=not args.quiet, mode=args.mode)
            if rc > worst_exit:
                worst_exit = rc
        sys.exit(worst_exit)

    else:
        # 単一ファイル
        target = args.checklist or _DEFAULT_CHECKLIST
        rc = _run_check(target, verbose=not args.quiet, mode=args.mode)
        sys.exit(rc)


if __name__ == "__main__":
    main()
