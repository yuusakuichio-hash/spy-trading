"""tests/test_ntfy_backoff_20260424.py — ntfy 429 自動 backoff 検証

2026-04-24 22:33 JST 事故時、atlas_monitor が 15 秒毎に emergency
heartbeat 警告を連射し ntfy.sh も連射 → 429 Too Many Requests で全通知
失敗した。本 test は pushover_client と同設計の自動沈黙機構が正しく
動作することを検証する。

要件:
- R1: 3 連続 429 で backoff_until が 30 分先に設定される
- R2: backoff 中は urlopen を呼ばない (skip)
- R3: 成功後は consecutive_429 が 0 にリセット
- R4: backoff 期間終了で通知再開
- R5: 非 429 エラー (network error 等) は counter を増やさない
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    """module-level state を reset してから import。"""
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    import importlib
    import atlas_v3.ops.monitor as _m
    importlib.reload(_m)
    # module-level state reset
    _m._NTFY_CONSECUTIVE_429 = 0
    _m._NTFY_BACKOFF_UNTIL = 0.0
    yield _m
    _m._NTFY_CONSECUTIVE_429 = 0
    _m._NTFY_BACKOFF_UNTIL = 0.0


def _make_daemon(mod):
    """MonitorDaemon の最小 instance (dependency を mock)。"""
    from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig
    config = MonitorConfig(
        max_latency_ms=500.0,
        daily_loss_usd=-500.0,
        drawdown_pct=0.15,
        pushover_enabled=False,
        kill_switch_on_emergency=False,
    )
    return MonitorDaemon(config=config)


class TestNtfyBackoff:
    def test_429_three_consecutive_arms_backoff(self, mod):
        """3 連続 429 で backoff_until が 30 分先に設定される。"""
        daemon = _make_daemon(mod)
        err = Exception("HTTP Error 429: Too Many Requests")
        with patch("urllib.request.urlopen", side_effect=err):
            for _ in range(3):
                daemon._send_ntfy_fallback(
                    "t", "m", mod.AlertLevel.EMERGENCY,
                )

        assert mod._NTFY_BACKOFF_UNTIL > time.time() + 1700
        # counter は arming 後に reset される (無限増加防止)
        assert mod._NTFY_CONSECUTIVE_429 == 0

    def test_backoff_skips_urlopen(self, mod):
        """backoff 中は urlopen を呼ばない。"""
        daemon = _make_daemon(mod)
        mod._NTFY_BACKOFF_UNTIL = time.time() + 600  # 10 分先

        with patch("urllib.request.urlopen") as mock_urlopen:
            daemon._send_ntfy_fallback("t", "m", mod.AlertLevel.WARNING)
        mock_urlopen.assert_not_called()

    def test_success_resets_counter(self, mod):
        """成功送信後に consecutive_429 = 0 に reset。"""
        daemon = _make_daemon(mod)
        mod._NTFY_CONSECUTIVE_429 = 2  # 既に 2 連続 429 状態

        with patch("urllib.request.urlopen", return_value=MagicMock()):
            daemon._send_ntfy_fallback("t", "m", mod.AlertLevel.WARNING)

        assert mod._NTFY_CONSECUTIVE_429 == 0

    def test_backoff_expires_after_window(self, mod):
        """backoff 期間終了後は urlopen が再度呼ばれる。"""
        daemon = _make_daemon(mod)
        mod._NTFY_BACKOFF_UNTIL = time.time() - 1  # 1 秒前に expire

        with patch("urllib.request.urlopen") as mock_urlopen:
            daemon._send_ntfy_fallback("t", "m", mod.AlertLevel.CRITICAL)
        mock_urlopen.assert_called_once()

    def test_non_429_error_does_not_increment_counter(self, mod):
        """network error 等の非 429 エラーは counter を増やさない。"""
        daemon = _make_daemon(mod)
        assert mod._NTFY_CONSECUTIVE_429 == 0

        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            for _ in range(5):
                daemon._send_ntfy_fallback("t", "m", mod.AlertLevel.WARNING)

        assert mod._NTFY_CONSECUTIVE_429 == 0
        assert mod._NTFY_BACKOFF_UNTIL == 0.0

    def test_too_many_keyword_also_detected(self, mod):
        """'too many' 文字列も 429 判定される。"""
        daemon = _make_daemon(mod)
        err = Exception("Server returned 'Too Many Requests' error")
        with patch("urllib.request.urlopen", side_effect=err):
            for _ in range(3):
                daemon._send_ntfy_fallback("t", "m", mod.AlertLevel.EMERGENCY)

        assert mod._NTFY_BACKOFF_UNTIL > time.time() + 1700
