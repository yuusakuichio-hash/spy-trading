"""tests/test_preflight_compliance_check_20260424.py — regression test for backtick false-positive

2026-04-24 事故:
    scripts/preflight_compliance_check.py の regex が checklist 凡例行のバックティック内テキストにもマッチし、
    paper モードで CRITICAL 誤検知 → exit 1 → atlas-paper launchd crash loop を引き起こした。
    ops agent が regex に negative lookbehind/lookahead `(?<!`)` / `(?!`)` を追加して修正。

再発防止テスト:
    T-1: バックティック外の [PENDING_OWNER_APPROVAL_LIVE] は検出（CRITICAL）
    T-2: バックティック内の `[PENDING_OWNER_APPROVAL_LIVE]` は非検出
    T-3: バックティック外の [PENDING_OWNER_APPROVAL_PAPER] は paper モードで WARN
    T-4: バックティック内の `[PENDING_OWNER_APPROVAL_PAPER]` は非検出
    T-5: 凡例行（legend line）の複数タグをバックティックで囲んでも誤検知しない
    T-6: legacy [PENDING_OWNER_APPROVAL] は paper モードで WARN / live モードで CRITICAL
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.preflight_compliance_check import (
    _PATTERN_CRITICAL_LIVE,
    _PATTERN_WARN_PAPER,
    _PATTERN_LEGACY,
    _find_pending_items,
)


def test_live_tag_outside_backtick_matches():
    """バックティック外の LIVE タグは検出される。"""
    line = "- [ ] 本番口座の確認 [PENDING_OWNER_APPROVAL_LIVE]"
    assert _PATTERN_CRITICAL_LIVE.search(line) is not None


def test_live_tag_inside_backtick_not_match():
    """バックティック内の LIVE タグは非検出（凡例行を誤検知しない）。"""
    line = "凡例: `[PENDING_OWNER_APPROVAL_LIVE]` は live モードで CRITICAL"
    assert _PATTERN_CRITICAL_LIVE.search(line) is None


def test_paper_tag_outside_backtick_matches():
    """バックティック外の PAPER タグは検出される。"""
    line = "- [ ] Paper 口座設定 [PENDING_OWNER_APPROVAL_PAPER]"
    assert _PATTERN_WARN_PAPER.search(line) is not None


def test_paper_tag_inside_backtick_not_match():
    """バックティック内の PAPER タグは非検出。"""
    line = "凡例: `[PENDING_OWNER_APPROVAL_PAPER]` は paper モードで WARN"
    assert _PATTERN_WARN_PAPER.search(line) is None


def test_legend_line_multi_tag_with_backticks(tmp_path):
    """凡例行で複数タグをバックティックで囲んだ場合も誤検知しない（事故の再現）。"""
    checklist = tmp_path / "checklist.md"
    checklist.write_text(
        "# 凡例\n"
        "\n"
        "- `[PENDING_OWNER_APPROVAL_LIVE]` — live モード CRITICAL\n"
        "- `[PENDING_OWNER_APPROVAL_PAPER]` — paper モード WARN のみ\n"
        "- `[PENDING_OWNER_APPROVAL]` — 後方互換タグ\n"
        "\n"
        "# 本文\n"
        "- [x] 全クリア項目\n",
        encoding="utf-8",
    )

    items_paper = _find_pending_items(checklist, mode="paper")
    items_live = _find_pending_items(checklist, mode="live")
    assert items_paper == [], (
        f"paper モードで凡例行を誤検知した（事故再発）: {items_paper}"
    )
    assert items_live == [], (
        f"live モードで凡例行を誤検知した: {items_live}"
    )


def test_legacy_tag_paper_mode_is_warn(tmp_path):
    """legacy [PENDING_OWNER_APPROVAL] は paper モードで WARN。"""
    checklist = tmp_path / "checklist.md"
    checklist.write_text(
        "- [ ] 旧タグ項目 [PENDING_OWNER_APPROVAL]\n",
        encoding="utf-8",
    )
    items = _find_pending_items(checklist, mode="paper")
    assert len(items) == 1
    assert items[0].severity == "WARN"


def test_legacy_tag_live_mode_is_critical(tmp_path):
    """legacy [PENDING_OWNER_APPROVAL] は live モードで CRITICAL。"""
    checklist = tmp_path / "checklist.md"
    checklist.write_text(
        "- [ ] 旧タグ項目 [PENDING_OWNER_APPROVAL]\n",
        encoding="utf-8",
    )
    items = _find_pending_items(checklist, mode="live")
    assert len(items) == 1
    assert items[0].severity == "CRITICAL"


def test_real_file_no_false_positive():
    """実ファイル (data/ops/compliance_checklist_20260423.md) で paper モード起動 block しないことを確認。"""
    repo = Path(__file__).resolve().parents[1]
    checklist = repo / "data" / "ops" / "compliance_checklist_20260423.md"
    if not checklist.exists():
        pytest.skip("checklist 本体が無い環境（CI 等）")
    items_paper = _find_pending_items(checklist, mode="paper")
    criticals = [i for i in items_paper if i.severity == "CRITICAL"]
    assert criticals == [], (
        f"paper モードで CRITICAL 検知（atlas-paper crash loop 再発リスク）: "
        f"{[(i.line_num, i.line[:80]) for i in criticals]}"
    )
