"""tests/test_monitor_self_heartbeat_20260424.py — S-6 regression: self-heartbeat

2026-04-24 事故:
    v3 kill_switch.flag が Monitor の heartbeat 判定 (300s threshold) で誤発動。
    真因: MonitorDaemon._run_loop が record_heartbeat() を自動で呼ばず、外部 Bot 層
    からの呼出に依存する設計だが、yfinance provider 単体 (atlas-paper paper mode)
    では Bot 層不在 → 300s 経過で Monitor 自身が「Bot dead」判定 → kill_switch。

修正 (S-6):
    _run_loop の check_once() 成功パスで self.record_heartbeat() を自動呼出。
    Monitor が生きて metrics 取得できている事実そのものを liveness 証明として heartbeat。
    Bot 層があればその record_heartbeat が上書きする設計のため互換性維持。

テスト:
    T-1: _run_loop 1 iteration 成功後、_last_heartbeat が更新される
    T-2: _fetch_metrics が例外を投げる場合は heartbeat 更新されない（fail-closed 保証）
    T-3: check_once() 自体の呼出は heartbeat を更新しない（loop path のみ）
"""
from __future__ import annotations

import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon


class _FakeProvider:
    def __init__(self, raise_on_call: bool = False):
        self.raise_on_call = raise_on_call
        self.call_count = 0

    def get_metrics(self) -> dict:
        self.call_count += 1
        if self.raise_on_call:
            raise RuntimeError("simulated provider failure")
        return {"pnl_day_usd": 0.0, "drawdown_pct": 0.0, "latency_ms": 10.0}


def _make_daemon(provider) -> MonitorDaemon:
    cfg = MonitorConfig(
        check_interval_secs=0.05,  # test を素早く回す
        heartbeat_timeout_secs=300.0,
        pushover_enabled=False,
        kill_switch_on_emergency=False,
        probe_on_consecutive_failure=False,
        metric_provider=provider.get_metrics,
    )
    return MonitorDaemon(cfg)


def test_run_loop_success_updates_self_heartbeat():
    """T-1: _run_loop の 1 iteration 成功で _last_heartbeat が更新されること。"""
    provider = _FakeProvider(raise_on_call=False)
    daemon = _make_daemon(provider)

    hb_before = daemon._last_heartbeat
    time.sleep(0.01)  # monotonic の精度を確保

    # _run_loop を内部で直接 1 iteration 分走らせる（start/stop で周回させる）
    daemon.start()
    try:
        # 1-2 iteration 走らせる時間
        time.sleep(0.3)
    finally:
        daemon.stop(timeout=2.0)

    assert provider.call_count >= 1, "provider.get_metrics が呼ばれていない"
    assert daemon._last_heartbeat > hb_before, (
        f"_last_heartbeat が更新されていない: before={hb_before}, after={daemon._last_heartbeat}"
    )


def test_run_loop_failure_does_not_update_heartbeat():
    """T-2: provider 例外時は heartbeat 更新されない（fail-closed 保証）。"""
    provider = _FakeProvider(raise_on_call=True)
    daemon = _make_daemon(provider)

    hb_before = daemon._last_heartbeat

    daemon.start()
    try:
        time.sleep(0.2)
    finally:
        daemon.stop(timeout=2.0)

    # provider は呼ばれたが raise したため heartbeat は更新されないはず
    assert provider.call_count >= 1
    assert daemon._last_heartbeat == hb_before, (
        f"失敗パスで heartbeat が更新されてしまった: before={hb_before}, after={daemon._last_heartbeat}"
    )


def test_check_once_does_not_update_heartbeat():
    """T-3: check_once() 単体呼出では heartbeat 更新されない（_run_loop path のみが source）。"""
    provider = _FakeProvider(raise_on_call=False)
    daemon = _make_daemon(provider)

    hb_before = daemon._last_heartbeat
    time.sleep(0.01)

    # check_once を直接呼ぶ（_run_loop 経由でない）
    daemon.check_once(pnl_day_usd=0.0, drawdown_pct=0.0, latency_ms=0.0)

    assert daemon._last_heartbeat == hb_before, (
        "check_once 単体で heartbeat が更新されてしまった（テスト目的の直接呼出を壊す）"
    )
