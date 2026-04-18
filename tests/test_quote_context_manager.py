"""Quote Context Manager tests"""
import os
import sys
import time
import datetime
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


# ── M-6: cache鮮度チェック ────────────────────────────────────────────────────

def test_m6_stale_cache_blocks_entry():
    """M-6: cacheが5分超の場合はallow_new_entry()がFalseになる。"""
    m = QuoteContextManager()
    # 3回切断でlevel=3, active_source="cache"
    for _ in range(3):
        m.on_disconnect()
    assert m.get_level() == 3
    assert m.allow_new_entry() is False  # level 3 already blocks

    # 別のケース: level=1でcacheを強制選択しても鮮度チェックが効く
    m2 = QuoteContextManager()
    m2.on_disconnect()
    # 鮮度切れを模擬: last_disconnect_atを7分前に設定
    m2.state.last_disconnect_at = datetime.datetime.now() - datetime.timedelta(seconds=700)
    # _pick_fallback_sourceを再度呼ぶ（内部でcacheを選んでstale判定させる）
    # cacheが選ばれる=source_chain[level]でlevel>=len(chain)の時
    m2.state.level = 10  # len(source_chain)=4より大きくしてcacheを強制
    m2._pick_fallback_source()
    assert m2.state.active_source == "stale_cache"
    assert m2.allow_new_entry() is False


def test_m6_fresh_cache_allows_entry():
    """M-6: cacheが新鮮（5分以内）の場合はstale_cacheにならない。"""
    m = QuoteContextManager()
    m.on_disconnect()
    # 新鮮なdisconnect（直近）
    m.state.last_disconnect_at = datetime.datetime.now() - datetime.timedelta(seconds=30)
    m.state.level = 10  # cacheを強制選択
    m._pick_fallback_source()
    assert m.state.active_source == "cache"


# ── M-7: try_reconnect TOCTOU修正 ────────────────────────────────────────────

def test_m7_reconnect_attempts_atomic():
    """M-7: 再接続試行中にattempts countが正確に更新される。"""
    attempt_log = []

    def reconnect_fn():
        # 試行時のattempt countを記録
        attempt_log.append(1)
        return True

    m = QuoteContextManager(reconnect_fn=reconnect_fn)
    m.on_disconnect()

    import common.quote_context_manager as qcm
    original = qcm._BACKOFF_SEQUENCE
    qcm._BACKOFF_SEQUENCE = [0.01, 0.01, 0.01]
    try:
        ok = m.try_reconnect()
    finally:
        qcm._BACKOFF_SEQUENCE = original

    assert ok is True
    assert len(attempt_log) == 1
    # 成功後はattempts=0にリセットされる
    with m._lock:
        assert m.state.reconnect_attempts == 0


def test_m7_failed_reconnect_preserves_attempts():
    """M-7: 再接続失敗時はattempt countが保持される。"""
    m = QuoteContextManager(reconnect_fn=lambda: False)
    m.on_disconnect()

    import common.quote_context_manager as qcm
    original = qcm._BACKOFF_SEQUENCE
    qcm._BACKOFF_SEQUENCE = [0.01, 0.01, 0.01]
    try:
        ok = m.try_reconnect()
    finally:
        qcm._BACKOFF_SEQUENCE = original

    assert ok is False
    # 失敗時はattemptsが増加したまま（リセットしない）
    with m._lock:
        assert m.state.reconnect_attempts == 1
