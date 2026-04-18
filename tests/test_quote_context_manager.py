"""Quote Context Manager tests"""
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.quote_context_manager import QuoteContextManager


def test_initial_state():
    m = QuoteContextManager()
    assert m.get_level() == 0
    assert m.allow_new_entry() is True
    assert m.margin_scale() == 1.0
    assert m.state.active_source == "primary"


def test_disconnect_level1():
    m = QuoteContextManager()
    m.on_disconnect()
    assert m.get_level() == 1
    assert m.allow_new_entry() is True  # level 1 は継続
    assert m.margin_scale() == 0.8
    assert m.state.active_source == "finnhub"


def test_disconnect_level2():
    m = QuoteContextManager()
    m.on_disconnect()
    m.on_disconnect()
    assert m.get_level() == 2
    assert m.allow_new_entry() is True  # 保守化は継続
    assert m.margin_scale() == 0.5
    assert m.state.active_source == "yahoo"


def test_disconnect_level3_blocks_entry():
    m = QuoteContextManager()
    for _ in range(3):
        m.on_disconnect()
    assert m.get_level() == 3
    assert m.allow_new_entry() is False
    assert m.margin_scale() == 0.0
    assert m.state.active_source == "cache"


def test_reconnect_reset():
    m = QuoteContextManager()
    for _ in range(3):
        m.on_disconnect()
    assert m.get_level() == 3
    m.on_reconnect_success()
    assert m.get_level() == 0
    assert m.state.disconnect_count == 0
    assert m.state.active_source == "primary"
    assert m.allow_new_entry() is True


def test_try_reconnect_success():
    called = []

    def reconnect_fn():
        called.append(1)
        return True

    m = QuoteContextManager(reconnect_fn=reconnect_fn)
    m.on_disconnect()

    # backoff は最初5秒。短くするためmonkeypatch風にstate上書き
    # 実際はbackoffで5s待つので、tests は attempts=5 を2にしてから呼ぶ
    import common.quote_context_manager as qcm
    original = qcm._BACKOFF_SEQUENCE
    qcm._BACKOFF_SEQUENCE = [0.01, 0.01, 0.01]
    try:
        ok = m.try_reconnect()
    finally:
        qcm._BACKOFF_SEQUENCE = original

    assert ok is True
    assert m.get_level() == 0
    assert called == [1]


def test_notify_only_when_level3():
    notifications = []

    def notify_fn(title, msg, priority):
        notifications.append((title, msg, priority))

    m = QuoteContextManager(notify_fn=notify_fn)

    # level 1: 通知しない
    m.on_disconnect()
    m.notify_if_escalated()
    assert len(notifications) == 0

    # level 2: 通知しない
    m.on_disconnect()
    m.notify_if_escalated()
    assert len(notifications) == 0

    # level 3: 通知する
    m.on_disconnect()
    m.notify_if_escalated()
    assert len(notifications) == 1
    assert notifications[0][2] == 1  # priority=1


def test_status_summary():
    m = QuoteContextManager()
    m.on_disconnect()
    s = m.status_summary()
    assert s["level"] == 1
    assert s["disconnect_count"] == 1
    assert s["active_source"] == "finnhub"
    assert s["allow_new_entry"] is True
    assert s["margin_scale"] == 0.8
