"""tests/test_redteam_r1_fixes_20260424.py — Redteam r1 指摘 15 件修正検証テスト

Sprint 1-B Phase B の CRITICAL 7 件 + HIGH 8 件修正を網羅的に検証する。
各 CRITICAL/HIGH に境界値・負例テスト最低 1 件を含む。

目標: 新規テスト 25+ 件（34 existing + 25 new >= 55+ 目標）
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# CRITICAL 1: _check_daily_loss 符号境界漏れ修正テスト
# threshold=-400 の境界値: -399/-400/-500/-600/-601 各々で期待 level 検証
# ===========================================================================

class TestCritical1DailyLossThreshold:
    """CRITICAL 1: _check_daily_loss の符号境界修正検証。"""

    def _make_daemon(self, tmp_path: Path):
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        return MonitorDaemon(config)

    def test_pnl_minus_399_is_info(self, tmp_path: Path) -> None:
        """pnl=-399 は閾値内 → INFO（損失が threshold=-400 に未達）。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(tmp_path)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = daemon._check_daily_loss(ts, pnl_day_usd=-399.0)
        assert chk.level == AlertLevel.INFO, f"Expected INFO, got {chk.level}"

    def test_pnl_minus_400_is_critical(self, tmp_path: Path) -> None:
        """pnl=-400 は閾値ちょうど → CRITICAL（threshold*1.5=-600 より大きい）。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(tmp_path)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = daemon._check_daily_loss(ts, pnl_day_usd=-400.0)
        assert chk.level == AlertLevel.CRITICAL, f"Expected CRITICAL, got {chk.level}"

    def test_pnl_minus_500_is_critical(self, tmp_path: Path) -> None:
        """pnl=-500 は -400〜-600 の間 → CRITICAL（KillSwitch 非発動）。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(tmp_path)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = daemon._check_daily_loss(ts, pnl_day_usd=-500.0)
        assert chk.level == AlertLevel.CRITICAL, f"Expected CRITICAL, got {chk.level}"

    def test_pnl_minus_600_is_emergency(self, tmp_path: Path) -> None:
        """pnl=-600 は threshold*1.5=-600 ちょうど → EMERGENCY。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(tmp_path)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = daemon._check_daily_loss(ts, pnl_day_usd=-600.0)
        assert chk.level == AlertLevel.EMERGENCY, f"Expected EMERGENCY, got {chk.level}"

    def test_pnl_minus_601_is_emergency(self, tmp_path: Path) -> None:
        """pnl=-601 は threshold*1.5=-600 を超える → EMERGENCY。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(tmp_path)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = daemon._check_daily_loss(ts, pnl_day_usd=-601.0)
        assert chk.level == AlertLevel.EMERGENCY, f"Expected EMERGENCY, got {chk.level}"

    def test_pnl_zero_is_info(self, tmp_path: Path) -> None:
        """pnl=0 は正常 → INFO。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(tmp_path)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = daemon._check_daily_loss(ts, pnl_day_usd=0.0)
        assert chk.level == AlertLevel.INFO


# ===========================================================================
# CRITICAL 2: vault.py パーミッション検査テスト
# ===========================================================================

class TestCritical2VaultPermissions:
    """CRITICAL 2: .env.d パーミッション検査修正検証。"""

    def test_load_from_env_world_readable_raises(self, tmp_path: Path) -> None:
        """world-readable (0644) な env ファイルは VaultError を raise する。"""
        from atlas_v3.ops.vault import load_from_env, VaultError
        env_file = tmp_path / "moomoo_paper.env"
        env_file.write_text(
            "MOOMOO_APP_ID=test_id\n"
            "MOOMOO_APP_SECRET=test_secret\n"
            "MOOMOO_TRD_ENV=SIMULATE\n",
            encoding="utf-8",
        )
        env_file.chmod(0o644)  # world-readable — 危険
        with pytest.raises(VaultError, match="[Ii]nsecure"):
            load_from_env(env_path=env_file)

    def test_load_from_env_0600_passes(self, tmp_path: Path) -> None:
        """0600 パーミッションの env ファイルは正常にロードできる。"""
        from atlas_v3.ops.vault import load_from_env
        env_file = tmp_path / "moomoo_paper.env"
        env_file.write_text(
            "MOOMOO_APP_ID=test_id\n"
            "MOOMOO_APP_SECRET=test_secret\n"
            "MOOMOO_TRD_ENV=SIMULATE\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)  # 安全
        creds = load_from_env(env_path=env_file)
        assert creds.app_id == "test_id"

    def test_load_from_env_file_not_found_raises_vault_error(self, tmp_path: Path) -> None:
        """ファイルが存在しない場合は VaultError を raise する（env fallback 無効）。"""
        from atlas_v3.ops.vault import load_from_env, VaultError
        nonexistent = tmp_path / "nonexistent.env"
        # MOOMOO_APP_ID を環境変数に設定してもファイルがないなら VaultError
        with patch.dict(os.environ, {"MOOMOO_APP_ID": "env_id", "MOOMOO_APP_SECRET": "env_secret"}):
            with pytest.raises(VaultError, match="[Nn]ot found|not found"):
                load_from_env(env_path=nonexistent)

    def test_real_env_cannot_leak_via_fallback(self, tmp_path: Path) -> None:
        """MOOMOO_TRD_ENV=REAL が環境変数にあっても、ファイル不在なら VaultError。"""
        from atlas_v3.ops.vault import load_from_env, VaultError
        nonexistent = tmp_path / "not_here.env"
        with patch.dict(os.environ, {"MOOMOO_TRD_ENV": "REAL", "MOOMOO_APP_ID": "x", "MOOMOO_APP_SECRET": "y"}):
            with pytest.raises(VaultError):
                load_from_env(env_path=nonexistent)


# ===========================================================================
# CRITICAL 3: latency_monitor.py cold-start (window < 100) テスト
# ===========================================================================

class TestCritical3LatencyColdStart:
    """CRITICAL 3: window < 100 で p99 が None / ALLOW 返却テスト。"""

    def _make_monitor(self, **kwargs):
        from atlas_v3.ops.latency_monitor import LatencyMonitor, LatencyConfig
        config = LatencyConfig(
            window_size=500,
            p99_warn_ms=100.0,
            p99_halt_ms=500.0,
            kill_switch_on_halt=False,
            persist_samples=False,
            **kwargs,
        )
        return LatencyMonitor(config)

    def test_cold_start_under_100_returns_allow(self) -> None:
        """99 サンプル時点では ALLOW を返す（cold-start）。"""
        from atlas_v3.ops.latency_monitor import LatencyDecision
        m = self._make_monitor()
        for _ in range(99):
            decision = m.record(latency_ms=2000.0)  # halt 閾値の 4 倍でも ALLOW
        assert decision == LatencyDecision.ALLOW

    def test_cold_start_p99_none_under_100(self) -> None:
        """99 サンプル時点では p99_ms() が None を返す。"""
        m = self._make_monitor()
        for _ in range(99):
            m.record(latency_ms=200.0)
        assert m.p99_ms() is None

    def test_at_100_samples_p99_is_not_none(self) -> None:
        """100 サンプル以上では p99_ms() が None でなくなる。"""
        m = self._make_monitor()
        for _ in range(100):
            m.record(latency_ms=50.0)
        assert m.p99_ms() is not None

    def test_at_100_high_latency_triggers_halt(self) -> None:
        """100 サンプル以上で高レイテンシなら HALT になる。"""
        from atlas_v3.ops.latency_monitor import LatencyDecision
        m = self._make_monitor()
        for _ in range(100):
            m.record(latency_ms=2000.0)  # halt 閾値 500ms の 4 倍
        assert m.decide() == LatencyDecision.HALT


# ===========================================================================
# CRITICAL 4: vault.py trd_env=REAL 漏入防止テスト（env fallback 削除確認）
# ===========================================================================

class TestCritical4VaultEnvFallbackRemoved:
    """CRITICAL 4: env fallback 削除で REAL 漏入が不可能なことを確認。"""

    def test_file_with_real_trd_env_raises(self, tmp_path: Path) -> None:
        """ファイルに MOOMOO_TRD_ENV=REAL が含まれている場合は VaultError。"""
        from atlas_v3.ops.vault import load_from_env, VaultError
        env_file = tmp_path / "bad.env"
        env_file.write_text(
            "MOOMOO_APP_ID=id\nMOOMOO_APP_SECRET=secret\nMOOMOO_TRD_ENV=REAL\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        with pytest.raises(VaultError, match="SIMULATE"):
            load_from_env(env_path=env_file)

    def test_missing_file_no_env_fallback(self, tmp_path: Path) -> None:
        """ファイル不在 + 環境変数あっても VaultError（fallback なし）。"""
        from atlas_v3.ops.vault import load_from_env, VaultError
        with patch.dict(os.environ, {"MOOMOO_APP_ID": "id", "MOOMOO_APP_SECRET": "sec"}):
            with pytest.raises(VaultError, match="[Nn]ot found|not found"):
                load_from_env(env_path=tmp_path / "nothere.env")


# ===========================================================================
# CRITICAL 5: ntfy Priority が数値 int であることを確認
# ===========================================================================

class TestCritical5NtfyPriorityIsInt:
    """CRITICAL 5: _send_ntfy_fallback の Priority ヘッダが数値文字列か確認。"""

    def test_ntfy_priority_header_is_numeric(self, tmp_path: Path) -> None:
        """ntfy に送信される Priority ヘッダが数値文字列（'3'/'4'/'5'）であること。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel

        captured_headers = {}

        def mock_urlopen(req, timeout=None):
            captured_headers.update(req.headers)
            return MagicMock()

        config = MonitorConfig(
            pushover_enabled=False,  # Pushover はスキップ
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        import urllib.request
        with patch.object(urllib.request, "urlopen", mock_urlopen):
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            from atlas_v3.ops.monitor import HealthCheck
            chk = HealthCheck(ts=ts, level=AlertLevel.CRITICAL, check_name="test", message="test")
            daemon._send_ntfy_fallback("title", "message", AlertLevel.CRITICAL)

        priority = captured_headers.get("Priority")
        assert priority is not None, "Priority header not set"
        assert priority.isdigit(), f"Priority must be numeric digit, got {priority!r}"
        assert int(priority) in (1, 2, 3, 4, 5), f"Priority must be 1-5, got {priority}"

    def test_ntfy_critical_priority_is_4(self, tmp_path: Path) -> None:
        """CRITICAL → Priority=4 (high) であること。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel

        captured_headers = {}

        def mock_urlopen(req, timeout=None):
            captured_headers.update(req.headers)
            return MagicMock()

        config = MonitorConfig(
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        import urllib.request
        with patch.object(urllib.request, "urlopen", mock_urlopen):
            daemon._send_ntfy_fallback("t", "m", AlertLevel.CRITICAL)

        assert captured_headers.get("Priority") == "4"

    def test_ntfy_emergency_priority_is_5(self, tmp_path: Path) -> None:
        """EMERGENCY → Priority=5 (urgent) であること。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel

        captured_headers = {}

        def mock_urlopen(req, timeout=None):
            captured_headers.update(req.headers)
            return MagicMock()

        config = MonitorConfig(
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        import urllib.request
        with patch.object(urllib.request, "urlopen", mock_urlopen):
            daemon._send_ntfy_fallback("t", "m", AlertLevel.EMERGENCY)

        assert captured_headers.get("Priority") == "5"

    def test_ntfy_warning_priority_is_3(self, tmp_path: Path) -> None:
        """WARNING → Priority=3 (default) であること。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel

        captured_headers = {}

        def mock_urlopen(req, timeout=None):
            captured_headers.update(req.headers)
            return MagicMock()

        config = MonitorConfig(
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        import urllib.request
        with patch.object(urllib.request, "urlopen", mock_urlopen):
            daemon._send_ntfy_fallback("t", "m", AlertLevel.WARNING)

        assert captured_headers.get("Priority") == "3"


# ===========================================================================
# CRITICAL 6: ntfy が Pushover 失敗時のみ発火することを確認
# ===========================================================================

class TestCritical6NtfyFallbackOnly:
    """CRITICAL 6: _send_alert が Pushover 成功時は ntfy を発火しないことを確認。"""

    def test_ntfy_not_called_when_pushover_succeeds(self, tmp_path: Path) -> None:
        """Pushover 成功時は ntfy を呼ばない。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel, HealthCheck

        ntfy_calls = []
        pushover_calls = []

        config = MonitorConfig(
            pushover_enabled=False,  # テスト注入で制御するため False
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        # Pushover は成功（True 返却）
        daemon._send_pushover = lambda title, message, priority: (pushover_calls.append(True) or True)
        # ntfy は呼ばれるか監視
        daemon._send_ntfy_fallback = lambda title, message, level: ntfy_calls.append(True)

        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = HealthCheck(ts=ts, level=AlertLevel.CRITICAL, check_name="daily_loss", message="test")

        # pushover_enabled=True にして _send_alert を呼ぶ
        object.__setattr__(daemon._config, 'pushover_enabled', True)
        daemon._send_alert(chk)

        assert len(pushover_calls) == 1, "Pushover should be called once"
        assert len(ntfy_calls) == 0, "ntfy should NOT be called when Pushover succeeds"

    def test_ntfy_called_when_pushover_fails(self, tmp_path: Path) -> None:
        """Pushover 失敗時は ntfy を呼ぶ。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel, HealthCheck

        ntfy_calls = []

        config = MonitorConfig(
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        # Pushover は失敗（False 返却）
        daemon._send_pushover = lambda title, message, priority: False
        daemon._send_ntfy_fallback = lambda title, message, level: ntfy_calls.append(True)

        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = HealthCheck(ts=ts, level=AlertLevel.CRITICAL, check_name="test", message="test")

        object.__setattr__(daemon._config, 'pushover_enabled', True)
        daemon._send_alert(chk)

        assert len(ntfy_calls) == 1, "ntfy should be called when Pushover fails"


# ===========================================================================
# CRITICAL 7: replay_bt.py 加算前 pre-check テスト
# ===========================================================================

class TestCritical7ReplayPreCheck:
    """CRITICAL 7: _simulate_day の加算前 pre-check テスト。"""

    def test_pnl_does_not_exceed_limit_after_precheck(self, tmp_path: Path) -> None:
        """加算前チェックにより daily_pnl が max_daily_loss_usd を超えない。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, TradeRecord

        config = ReplayConfig(
            data_path=tmp_path / "dummy.csv",  # 直接 _simulate_day を呼ぶ
            max_daily_loss_usd=-100.0,
        )
        bt = ReplayBacktest(config)

        records = [
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.1, pnl=-60.0, exit_reason="stop", vix_est=18.0),
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.1, pnl=-60.0, exit_reason="stop", vix_est=18.0),
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.1, pnl=-60.0, exit_reason="stop", vix_est=18.0),
        ]
        summary = bt._simulate_day("2024-01-02", records, capital=10000.0)

        # 加算前チェックがあれば 2 件目(-60) で projected=-120 < -100 → 止まるはず
        # pnl は最初の -60 のみカウント
        assert summary.pnl_usd >= config.max_daily_loss_usd, (
            f"pnl_usd={summary.pnl_usd} is below limit={config.max_daily_loss_usd}"
        )
        assert summary.halted, "Should be halted"

    def test_halt_stops_before_exceeding(self, tmp_path: Path) -> None:
        """制限を超える取引の前で halt になり、daily_pnl が制限以上に留まる。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, TradeRecord

        config = ReplayConfig(
            data_path=tmp_path / "dummy.csv",
            max_daily_loss_usd=-500.0,
        )
        bt = ReplayBacktest(config)

        # 各 -200 の取引 3 件: -200→OK, -400→OK, -600→pre-check で止まる
        records = [
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.1, pnl=-200.0, exit_reason="stop", vix_est=18.0),
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.1, pnl=-200.0, exit_reason="stop", vix_est=18.0),
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.1, pnl=-200.0, exit_reason="stop", vix_est=18.0),
        ]
        summary = bt._simulate_day("2024-01-02", records, capital=10000.0)

        # 3 件目を加算すると -600 < -500 なので pre-check で止まる
        # pnl_usd は -400 以上に留まるはず
        assert summary.pnl_usd >= config.max_daily_loss_usd, (
            f"Pre-check failed: pnl_usd={summary.pnl_usd} exceeded limit={config.max_daily_loss_usd}"
        )


# ===========================================================================
# HIGH 1: vault.py keyring 優先 テスト
# ===========================================================================

class TestHigh1VaultKeyring:
    """H-1: VAULT_MASTER_KEY の keyring 優先解決テスト。"""

    def test_keyring_takes_priority_over_env(self, tmp_path: Path) -> None:
        """keyring にキーがある場合は env 変数より優先して使用される。"""
        pytest.importorskip("cryptography", reason="cryptography required")
        from cryptography.fernet import Fernet
        from atlas_v3.ops.vault import _resolve_master_key

        keyring_key = Fernet.generate_key().decode()
        env_key = Fernet.generate_key().decode()

        class MockKeyring:
            def get_password(self, service, username):
                if service == "atlas_v3_vault" and username == "VAULT_MASTER_KEY":
                    return keyring_key
                return None

        with patch.dict(os.environ, {"VAULT_MASTER_KEY": env_key}):
            with patch.dict("sys.modules", {"keyring": MockKeyring()}):
                resolved = _resolve_master_key()

        assert resolved == keyring_key, "keyring key should take priority over env"

    def test_env_fallback_when_no_keyring(self, tmp_path: Path) -> None:
        """keyring が利用不可の場合は VAULT_ALLOW_ENV_FALLBACK=1 で env にフォールバックする。

        RT-R2-REG2 修正: env fallback は VAULT_ALLOW_ENV_FALLBACK=1 の明示 opt-in が必要。
        （r1 の仕様変更: サイレント退行から明示 opt-in への変更）
        """
        pytest.importorskip("cryptography", reason="cryptography required")
        from cryptography.fernet import Fernet
        from atlas_v3.ops.vault import _resolve_master_key

        env_key = Fernet.generate_key().decode()

        with patch.dict(os.environ, {
            "VAULT_MASTER_KEY": env_key,
            "VAULT_ALLOW_ENV_FALLBACK": "1",  # RT-R2-REG2: 明示 opt-in 必須
        }):
            # keyring モジュールが ImportError になるケース
            with patch.dict("sys.modules", {"keyring": None}):
                resolved = _resolve_master_key()

        assert resolved == env_key, "Should fall back to env when keyring unavailable and VAULT_ALLOW_ENV_FALLBACK=1"

    def test_no_key_anywhere_raises_vault_error(self) -> None:
        """keyring も env も空なら VaultError を raise する。"""
        from atlas_v3.ops.vault import VaultError, _resolve_master_key
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k != "VAULT_MASTER_KEY"}
            env.pop("VAULT_MASTER_KEY", None)
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(VaultError, match="VAULT_MASTER_KEY"):
                    _resolve_master_key()


# ===========================================================================
# HIGH 2: drawdown CRITICAL で KillSwitch 発動テスト
# ===========================================================================

class TestHigh2DrawdownKillSwitch:
    """H-2: drawdown CRITICAL で kill_switch_on_drawdown_breach=True 時 KillSwitch 発動。"""

    def test_drawdown_critical_activates_kill_switch_when_enabled(self, tmp_path: Path) -> None:
        """drawdown CRITICAL + kill_switch_on_drawdown_breach=True → KillSwitch 発動。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel, HealthCheck

        ks_calls = []

        config = MonitorConfig(
            drawdown_pct=0.12,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=True,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        daemon._activate_kill_switch = lambda chk: ks_calls.append(chk)

        # drawdown=0.15 > threshold=0.12 → CRITICAL drawdown
        checks = daemon.check_once(pnl_day_usd=0.0, drawdown_pct=0.15, latency_ms=10.0)
        dd_check = next(c for c in checks if c.check_name == "drawdown")
        assert dd_check.level == AlertLevel.CRITICAL

        assert len(ks_calls) >= 1, "KillSwitch should be activated on drawdown CRITICAL"

    def test_drawdown_critical_does_not_activate_when_disabled(self, tmp_path: Path) -> None:
        """kill_switch_on_drawdown_breach=False なら drawdown CRITICAL でも KillSwitch 非発動。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel

        ks_calls = []

        config = MonitorConfig(
            drawdown_pct=0.12,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,  # 無効
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        daemon._activate_kill_switch = lambda chk: ks_calls.append(chk)

        daemon.check_once(pnl_day_usd=0.0, drawdown_pct=0.15, latency_ms=10.0)
        assert len(ks_calls) == 0, "KillSwitch should NOT be activated when option is disabled"


# ===========================================================================
# HIGH 3: heartbeat file touch テスト
# ===========================================================================

class TestHigh3HeartbeatFile:
    """H-3: daemon が heartbeat ファイルに定期 touch することを確認。"""

    def test_heartbeat_file_touched_on_start(self, tmp_path: Path) -> None:
        """_touch_heartbeat_file() 呼び出しで heartbeat ファイルが作成される。"""
        import time
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig

        hb_file = tmp_path / "daemon_heartbeat"
        config = MonitorConfig(
            check_interval_secs=0.05,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            heartbeat_file=hb_file,
            heartbeat_write_interval_secs=0.01,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        daemon._touch_heartbeat_file()
        assert hb_file.exists(), "Heartbeat file should be created"
        content = hb_file.read_text(encoding="utf-8")
        assert "T" in content, "Heartbeat file should contain ISO timestamp"

    def test_heartbeat_file_updated_in_run_loop(self, tmp_path: Path) -> None:
        """daemon ループ中に heartbeat ファイルが更新される。"""
        import time
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig

        hb_file = tmp_path / "daemon_heartbeat"
        config = MonitorConfig(
            check_interval_secs=0.05,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            heartbeat_file=hb_file,
            heartbeat_write_interval_secs=0.01,  # 10ms ごとに touch
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        daemon.start()
        time.sleep(0.15)  # 3 サイクル待つ
        daemon.stop(timeout=2.0)
        assert hb_file.exists(), "Heartbeat file should exist after loop"


# ===========================================================================
# HIGH 4: replay_bt.py 必須列欠損 ReplayConfigError テスト
# ===========================================================================

class TestHigh4ReplayMissingColumns:
    """H-4: 必須列欠損で ReplayConfigError が raise されることを確認。"""

    def test_missing_date_column_raises(self, tmp_path: Path) -> None:
        """'date' 列がない CSV は ReplayConfigError を raise する。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, ReplayConfigError

        csv_path = tmp_path / "bad.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["pnl", "strategy"])
            writer.writeheader()
            writer.writerow({"pnl": "-100.0", "strategy": "CS"})

        config = ReplayConfig(data_path=csv_path)
        bt = ReplayBacktest(config)
        with pytest.raises(ReplayConfigError, match="date"):
            bt._load_records()

    def test_missing_pnl_column_raises(self, tmp_path: Path) -> None:
        """'pnl' 列がない CSV は ReplayConfigError を raise する。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, ReplayConfigError

        csv_path = tmp_path / "bad.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["date", "strategy"])
            writer.writeheader()
            writer.writerow({"date": "2024-01-02", "strategy": "CS"})

        config = ReplayConfig(data_path=csv_path)
        bt = ReplayBacktest(config)
        with pytest.raises(ReplayConfigError, match="pnl"):
            bt._load_records()


# ===========================================================================
# HIGH 5: replay_bt.py データ不足で ReplayConfigError テスト
# ===========================================================================

class TestHigh5ReplayInsufficientData:
    """H-5: データ不足時にシングル split ではなく ReplayConfigError を raise する。"""

    def test_insufficient_data_raises_replay_config_error(self, tmp_path: Path) -> None:
        """train_months=6 に対して少ないデータは ReplayConfigError を raise する。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, ReplayConfigError

        # 5 日分しかないデータ（train_months=6 × 20 = 120 日に対して不足）
        csv_path = tmp_path / "tiny.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["date", "strategy", "dte", "entry_credit",
                                "pnl", "exit_reason", "vix_est"]
            )
            writer.writeheader()
            for i in range(5):
                d = datetime.date(2024, 1, 2) + datetime.timedelta(days=i)
                writer.writerow({
                    "date": d.isoformat(), "strategy": "CS", "dte": "1",
                    "entry_credit": "0.1", "pnl": "0.05",
                    "exit_reason": "tp", "vix_est": "18",
                })

        config = ReplayConfig(data_path=csv_path, train_months=6, test_months=1)
        bt = ReplayBacktest(config)
        with pytest.raises(ReplayConfigError, match="[Ii]nsufficient"):
            bt.run()


# ===========================================================================
# HIGH 7: sharpe_ratio が returns 率ベースか確認
# ===========================================================================

class TestHigh7SharpeRatioReturns:
    """H-7: sharpe_ratio が USD 絶対値ではなく returns 率ベースであることを確認。"""

    def test_sharpe_is_scale_invariant(self) -> None:
        """初期資本が異なっても同一比率なら Sharpe 比は同じになる。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest

        # 同じ比率のリターン（capital_a の 1% と capital_b の 1%）
        capital_a = 10000.0
        capital_b = 50000.0

        pnls_a = [100.0, -50.0, 80.0, -30.0, 60.0]   # capital_a の 1%/0.5% 相当
        pnls_b = [500.0, -250.0, 400.0, -150.0, 300.0]  # capital_b の 1%/0.5% 相当

        sharpe_a = ReplayBacktest._compute_sharpe(pnls_a, initial_capital=capital_a)
        sharpe_b = ReplayBacktest._compute_sharpe(pnls_b, initial_capital=capital_b)

        assert abs(sharpe_a - sharpe_b) < 1e-6, (
            f"Sharpe should be scale-invariant: a={sharpe_a:.6f}, b={sharpe_b:.6f}"
        )

    def test_sharpe_usd_absolute_vs_returns_differ(self) -> None:
        """修正前（USD 絶対値）と修正後（returns 率）で Sharpe 比の値が変わることを確認。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest

        pnls = [100.0, -50.0, 80.0, -30.0, 60.0]
        capital = 10000.0

        sharpe_returns = ReplayBacktest._compute_sharpe(pnls, initial_capital=capital)
        # USD 絶対値は initial_capital=1.0 で計算した場合と同等（旧実装相当）
        sharpe_usd = ReplayBacktest._compute_sharpe(pnls, initial_capital=1.0)

        # returns 化すると absolute より小さくなるはず（pnl/capital < pnl/1.0）
        assert sharpe_returns < sharpe_usd, (
            f"Returns-based sharpe should be smaller than USD-absolute: "
            f"returns={sharpe_returns:.4f}, usd={sharpe_usd:.4f}"
        )


# ===========================================================================
# HIGH 8: _run_loop 連続失敗でdaemon停止テスト
# ===========================================================================

class TestHigh8DaemonConsecutiveFailure:
    """H-8: check_once 連続失敗 3 回で daemon が停止することを確認。"""

    def test_consecutive_failures_stop_daemon(self, tmp_path: Path) -> None:
        """check_once が max_consecutive_failures 回連続で例外を投げると daemon が停止する。

        NEW-C-2 対応: metric_provider=None は _fetch_metrics で RuntimeError になるため、
        check_once の呼出カウントを取るには metric_provider を明示設定する必要がある。
        """
        import time
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig

        def dummy_provider():
            return {"pnl_day_usd": 0.0, "drawdown_pct": 0.0, "latency_ms": 0.0}

        config = MonitorConfig(
            check_interval_secs=0.01,
            max_consecutive_failures=3,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
            metric_provider=dummy_provider,  # NEW-C-2: 明示設定必須
        )
        daemon = MonitorDaemon(config)

        call_count = [0]
        original_check_once = daemon.check_once

        def always_fail(*args, **kwargs):
            call_count[0] += 1
            raise RuntimeError("simulated check failure")

        daemon.check_once = always_fail
        daemon.start()
        time.sleep(0.5)  # 停止するのを待つ

        assert not daemon.is_running(), (
            "Daemon should have stopped after consecutive failures"
        )
        assert call_count[0] >= 3, (
            f"check_once should have been called at least 3 times, got {call_count[0]}"
        )

    def test_max_consecutive_failures_zero_raises(self) -> None:
        """max_consecutive_failures=0 は ValueError を raise する。"""
        from atlas_v3.ops.monitor import MonitorConfig
        with pytest.raises(ValueError, match="max_consecutive_failures"):
            MonitorConfig(max_consecutive_failures=0)
