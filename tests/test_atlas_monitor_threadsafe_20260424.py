"""tests/test_atlas_monitor_threadsafe_20260424.py — regression: sync-only violation crash loop

2026-04-24 事故:
    atlas_v3/ops/monitor.py:805 と latency_monitor.py:352 の ks_activate() が thread から
    呼ばれて @sync_only guard violation → activation FAILED → KILL_SWITCH file 永続ブロック
    → launchd crash loop で hook 全 tool 停止。復旧まで約 4 時間。

再発防止:
    T-1: monitor.py が _activate_raw 経路を使用していること（ソース検査）
    T-2: latency_monitor.py が _activate_raw 経路を使用していること（ソース検査）
    T-3: _activate_raw は non-main thread から呼べる（regression）
    T-4: guard された activate() は non-main thread から呼ぶと RuntimeError が出る（guard 健在確認）
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from common_v3.risk import kill_switch

_REPO = Path(__file__).resolve().parents[1]


def _strip_docstrings(src: str) -> str:
    """docstring / コメントを除いたコード本体を返す（文字列検索用）。"""
    no_triple_dq = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
    no_triple_sq = re.sub(r"'''.*?'''", '', no_triple_dq, flags=re.DOTALL)
    return no_triple_sq


def test_monitor_uses_activate_raw_in_thread_context():
    """atlas_v3/ops/monitor.py の MonitorDaemon が _activate_raw 経路を使用していることを確認。"""
    src = (_REPO / "atlas_v3" / "ops" / "monitor.py").read_text()
    body = _strip_docstrings(src)
    assert "from common_v3.risk.kill_switch import activate as ks_activate" not in body, (
        "monitor.py must not import @sync_only guarded activate() directly from thread context. "
        "Use _activate_raw instead (see async_impl.py for rationale)."
    )
    assert "from common_v3.risk.kill_switch import _activate_raw" in body, (
        "monitor.py must import _activate_raw for thread-safe activation."
    )


def test_monitor_uses_deactivate_raw_in_thread_context():
    """MonitorDaemon の probe_recovery も _deactivate_raw 経路を使用していることを確認。"""
    src = (_REPO / "atlas_v3" / "ops" / "monitor.py").read_text()
    body = _strip_docstrings(src)
    assert "from common_v3.risk.kill_switch import deactivate as ks_deactivate" not in body, (
        "monitor.py must not import @sync_only guarded deactivate() from thread context."
    )
    assert "from common_v3.risk.kill_switch import _deactivate_raw" in body


def test_latency_monitor_uses_activate_raw_in_thread_context():
    """atlas_v3/ops/latency_monitor.py も _activate_raw 経路を使用していることを確認。"""
    src = (_REPO / "atlas_v3" / "ops" / "latency_monitor.py").read_text()
    body = _strip_docstrings(src)
    assert "from common_v3.risk.kill_switch import activate as ks_activate" not in body, (
        "latency_monitor.py must not import @sync_only guarded activate() from thread context."
    )
    assert "from common_v3.risk.kill_switch import _activate_raw" in body


def test_activate_raw_safe_from_non_main_thread(tmp_path, monkeypatch):
    """_activate_raw が non-main thread から RuntimeError なく動作することを確認（regression）。"""
    flag_path = tmp_path / "kill_switch.flag"
    lock_path = tmp_path / "kill_switch.lock"

    monkeypatch.setattr(kill_switch, "FLAG_FILE", flag_path)
    monkeypatch.setattr(kill_switch, "_get_lock_path", lambda: lock_path)
    monkeypatch.setattr(kill_switch, "_ensure_dirs", lambda: None)
    monkeypatch.setattr(kill_switch, "_write_audit", lambda **kw: None)
    monkeypatch.setattr(kill_switch, "_write_flag", lambda p, d: p.write_text("test"))

    result: dict = {}

    def worker():
        try:
            result["activated"] = kill_switch._activate_raw(
                reason="regression_test_thread_activate",
                activator="test_worker_20260424",
            )
        except Exception as e:
            result["error"] = repr(e)

    t = threading.Thread(target=worker, name="non_main_thread_test")
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), "thread must finish within 5s"
    assert "error" not in result, (
        f"_activate_raw must not raise from non-main thread, got: {result.get('error')}"
    )
    assert result.get("activated") is True
    assert flag_path.exists()


def test_sync_only_guard_still_blocks_non_main_thread():
    """guard されたactivate() は non-main thread から呼ぶと RuntimeError が出ることを確認（guard 健在）。"""
    result: dict = {}

    def worker():
        try:
            kill_switch.activate(reason="should_fail", activator="test_worker")
            result["exc"] = None
        except RuntimeError as e:
            result["exc"] = str(e)

    t = threading.Thread(target=worker, name="guarded_thread_test")
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), "thread must finish within 5s"
    exc_msg = result.get("exc") or ""
    assert "sync-only contract violation" in exc_msg, (
        f"guard must raise sync-only violation from non-main thread, got: {exc_msg!r}"
    )
