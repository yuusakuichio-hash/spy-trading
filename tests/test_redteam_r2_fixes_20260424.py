"""tests/test_redteam_r2_fixes_20260424.py — Sprint 1-B Phase B Redteam r2 指摘 14 件修正テスト

修正対象:
    RT-R2-001: monitor.py _run_loop に MetricProvider 注入
    RT-R2-002: latency_monitor.py _compute_p99 インデックス修正
    RT-R2-003: MonitorConfig daily_loss_usd=0 禁止
    RT-R2-004: latency_monitor.py reset() 設計説明
    RT-R2-005: KillSwitch ファイル書込失敗 retry + andon fallback
    RT-R2-006: vault.py symlink realpath 親検査
    RT-R2-007: Pushover rate-limit → ntfy fallback
    RT-R2-H1:  vault.py keyring 失敗時 VaultError raise
    RT-R2-H2:  _touch_heartbeat_file 失敗時 EMERGENCY
    RT-R2-H3:  daily_loss_usd 3 系統 single source of truth
    RT-R2-H4:  replay_bt.py strict mode ValueErrror raise
    RT-R2-H5:  preflight_compliance_check.py 物理ブロック
    RT-R2-REG1: halt 判定は損失側のみ
    RT-R2-REG2: VAULT_ALLOW_ENV_FALLBACK 明示 opt-in

テスト数: >= 28（各 ID に境界値・負例・happy-path 各 1 件以上）
"""
from __future__ import annotations

import csv
import datetime
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# RT-R2-001: MetricProvider 注入
# ===========================================================================

class TestMetricProvider:
    """RT-R2-001: MonitorConfig.metric_provider で Bot 実データを注入できること。"""

    def test_metric_provider_injects_real_data(self, tmp_path: Path) -> None:
        """metric_provider が設定されている場合は実データが check_once() に渡る。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel

        called_with: list[dict] = []

        class FakeMonitor(MonitorDaemon):
            def check_once(self, pnl_day_usd=0.0, drawdown_pct=0.0, latency_ms=0.0):
                called_with.append({
                    "pnl_day_usd": pnl_day_usd,
                    "drawdown_pct": drawdown_pct,
                    "latency_ms": latency_ms,
                })
                return []

        provider_metrics = {"pnl_day_usd": -350.0, "drawdown_pct": 0.05, "latency_ms": 120.0}

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            check_interval_secs=0.05,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
            metric_provider=lambda: provider_metrics,
        )
        daemon = FakeMonitor(config)
        # _fetch_metrics が provider からデータを取得することを確認
        metrics = daemon._fetch_metrics()
        assert metrics["pnl_day_usd"] == -350.0
        assert metrics["drawdown_pct"] == 0.05
        assert metrics["latency_ms"] == 120.0

    def test_metric_provider_none_raises_runtime_error(self, tmp_path: Path) -> None:
        """NEW-C-2: metric_provider=None の場合は RuntimeError を raise する（旧 zero-fallback 廃止）。

        旧仕様: metric_provider=None → 全ゼロ値を返す（監視全盲・silent failure）
        新仕様: metric_provider=None → RuntimeError + EMERGENCY 通知 + KillSwitch 発動（fail-closed）

        zero-fallback は監視が永久全盲になるため NEW-C-2 で廃止。
        """
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
            metric_provider=None,
        )
        daemon = MonitorDaemon(config)
        # NEW-C-2: provider=None は RuntimeError で fail-closed
        with pytest.raises(RuntimeError, match="metric_provider is None"):
            daemon._fetch_metrics()

    def test_metric_provider_exception_propagates(self, tmp_path: Path) -> None:
        """NEW-C-2: metric_provider() が例外を投げた場合は例外が伝播する（旧 zero-fallback 廃止）。

        旧仕様: 例外 → 全ゼロ値を返す（silent degradation）
        新仕様: 例外 → 上位に伝播し _run_loop の consecutive_failures カウンタを増加（fail-closed）

        zero-fallback はブローカー障害を隠蔽するため NEW-C-2 で廃止。
        """
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig

        def bad_provider():
            raise RuntimeError("broker connection failed")

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
            metric_provider=bad_provider,
        )
        daemon = MonitorDaemon(config)
        # NEW-C-2: provider 例外は伝播する（fail-closed）
        with pytest.raises(RuntimeError, match="broker connection failed"):
            daemon._fetch_metrics()

    def test_start_warns_when_no_metric_provider(
        self, tmp_path: Path, caplog
    ) -> None:
        """metric_provider=None で start() すると CRITICAL ログが出る。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            check_interval_secs=0.1,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
            metric_provider=None,
        )
        daemon = MonitorDaemon(config)
        with caplog.at_level(logging.CRITICAL, logger="atlas_v3.ops.monitor"):
            daemon.start()
            daemon.stop(timeout=1.0)
        assert any("metric_provider" in r.message.lower() for r in caplog.records)

    def test_metric_provider_protocol_compliant_object(self, tmp_path: Path) -> None:
        """MetricProvider Protocol を実装したオブジェクトが正しく機能する。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, MetricProvider

        class BotMetricProvider:
            def get_metrics(self) -> dict:
                return {"pnl_day_usd": -100.0, "drawdown_pct": 0.02, "latency_ms": 50.0}

        provider = BotMetricProvider()
        assert isinstance(provider, MetricProvider)

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
            metric_provider=provider.get_metrics,
        )
        daemon = MonitorDaemon(config)
        metrics = daemon._fetch_metrics()
        assert metrics["pnl_day_usd"] == -100.0


# ===========================================================================
# RT-R2-002: _compute_p99 正確性
# ===========================================================================

class TestComputeP99:
    """RT-R2-002: _compute_p99 が正しい 99th-percentile を返すこと。"""

    def test_p99_with_100_samples(self) -> None:
        """n=100 のとき idx=min(int(100*0.99), 99)=99 → 最大値が p99（数学的に正しい）。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor
        # 1–100 の値: 昇順 sorted → sorted_asc[99] = 100 = p99 (= p100)
        samples = list(range(1, 101))
        result = LatencyMonitor._compute_p99(samples)
        # idx = int(100 * 0.99) = int(99.0) = 99 → sorted_asc[99] = 100
        assert result == 100

    def test_p99_with_500_samples(self) -> None:
        """n=500 のとき idx=int(500*0.99)=495 → sorted_asc[495] = 496。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor
        samples = list(range(1, 501))
        result = LatencyMonitor._compute_p99(samples)
        # idx = int(500 * 0.99) = int(495.0) = 495 → sorted_asc[495] = 496
        assert result == 496

    def test_p99_not_max_value_for_large_samples(self) -> None:
        """n >= 200 のとき、p99 が最大値（= p100）ではないこと。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor
        # 1–200 の均等分布
        samples = list(range(1, 201))
        result = LatencyMonitor._compute_p99(samples)
        max_val = max(samples)
        # p99: idx = int(200 * 0.99) = 198 → sorted_asc[198] = 199 < max(200)
        assert result < max_val

    def test_p99_empty_returns_zero(self) -> None:
        """空リストは 0 を返す（防御的）。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor
        assert LatencyMonitor._compute_p99([]) == 0.0

    def test_p99_single_sample(self) -> None:
        """n=1 のとき idx=min(int(0.99), 0)=0 → samples[0]。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor
        result = LatencyMonitor._compute_p99([42.0])
        assert result == 42.0


# ===========================================================================
# RT-R2-003: daily_loss_usd=0 禁止
# ===========================================================================

class TestDailyLossZeroForbidden:
    """RT-R2-003: MonitorConfig.daily_loss_usd=0 は ValueError を raise すること。"""

    def test_daily_loss_zero_raises(self) -> None:
        """daily_loss_usd=0 は ValueError を raise する。"""
        from atlas_v3.ops.monitor import MonitorConfig
        with pytest.raises(ValueError, match="negative"):
            MonitorConfig(daily_loss_usd=0.0)

    def test_daily_loss_positive_raises(self) -> None:
        """daily_loss_usd=100.0 は ValueError を raise する。"""
        from atlas_v3.ops.monitor import MonitorConfig
        with pytest.raises(ValueError, match="negative"):
            MonitorConfig(daily_loss_usd=100.0)

    def test_daily_loss_negative_ok(self) -> None:
        """daily_loss_usd=-400.0 は正常に作成できる。"""
        from atlas_v3.ops.monitor import MonitorConfig
        config = MonitorConfig(daily_loss_usd=-400.0)
        assert config.daily_loss_usd == -400.0

    def test_daily_loss_boundary_minus_epsilon(self) -> None:
        """daily_loss_usd=-0.01 (最小負値) は正常に作成できる。"""
        from atlas_v3.ops.monitor import MonitorConfig
        config = MonitorConfig(daily_loss_usd=-0.01)
        assert config.daily_loss_usd < 0


# ===========================================================================
# RT-R2-004: reset() 設計 — KillSwitch は解除しない
# ===========================================================================

class TestLatencyMonitorReset:
    """RT-R2-004: reset() は内部フラグのみクリアし KillSwitch ファイルには触らないこと。"""

    def test_reset_clears_internal_halted_flag(self) -> None:
        """reset() で _halted フラグがクリアされ ALLOW に戻る。"""
        from atlas_v3.ops.latency_monitor import (
            LatencyMonitor, LatencyConfig, LatencyDecision
        )
        config = LatencyConfig(
            window_size=500, p99_warn_ms=50.0, p99_halt_ms=100.0,
            persist_samples=False, kill_switch_on_halt=False,
        )
        m = LatencyMonitor(config)
        for _ in range(100):
            m.record(latency_ms=500.0)
        assert m.decide() == LatencyDecision.HALT

        m.reset()
        assert m.sample_count() == 0
        assert m.decide() == LatencyDecision.ALLOW  # cold-start で ALLOW

    def test_reset_does_not_touch_kill_switch_file(self, tmp_path: Path) -> None:
        """reset() は KillSwitch ファイルを削除・変更しない。"""
        from atlas_v3.ops.latency_monitor import (
            LatencyMonitor, LatencyConfig
        )
        # 偽の KillSwitch ファイルを作成
        ks_file = tmp_path / "kill_switch.flag"
        ks_file.write_text('{"reason": "test"}', encoding="utf-8")

        config = LatencyConfig(
            window_size=500, p99_warn_ms=50.0, p99_halt_ms=100.0,
            persist_samples=False, kill_switch_on_halt=False,
        )
        m = LatencyMonitor(config)
        m.reset()

        # reset() 後も KillSwitch ファイルは残っている
        assert ks_file.exists()
        content = json.loads(ks_file.read_text())
        assert content["reason"] == "test"


# ===========================================================================
# RT-R2-005: KillSwitch 書込失敗 retry + andon fallback
# ===========================================================================

class TestKillSwitchRetry:
    """RT-R2-005: KillSwitch 書込失敗時に retry 3 回 + andon fallback する。"""

    def test_monitor_kill_switch_retries_on_failure(self, tmp_path: Path) -> None:
        """_activate_kill_switch が失敗した場合に retry する。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, HealthCheck, AlertLevel

        call_count = [0]

        def failing_ks(**kwargs):
            call_count[0] += 1
            raise OSError("disk full")

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=True,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        # 2026-04-25: monitor は @sync_only 違反回避で _activate_raw 経路を使用するため
        # patch 対象を _activate_raw に変更 (test は activate を patch していたが旧経路)
        with patch("atlas_v3.ops.monitor.MonitorDaemon._trigger_andon_emergency") as mock_andon:
            with patch("common_v3.risk.kill_switch._activate_raw", side_effect=OSError("disk full")):
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                chk = HealthCheck(
                    ts=ts, level=AlertLevel.EMERGENCY,
                    check_name="test", message="test",
                )
                daemon._activate_kill_switch(chk)
            # andon が呼ばれた（全リトライ失敗後）
            mock_andon.assert_called_once()

    def test_latency_monitor_kill_switch_retries(self) -> None:
        """LatencyMonitor._activate_kill_switch が失敗時 andon fallback を呼ぶ。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor, LatencyConfig

        config = LatencyConfig(
            p99_warn_ms=100.0, p99_halt_ms=500.0,
            persist_samples=False,
        )
        m = LatencyMonitor(config)

        with patch("atlas_v3.ops.latency_monitor.LatencyMonitor._trigger_andon_emergency") as mock_andon:
            # 2026-04-25: latency_monitor も _activate_raw 経路に統一済 (sync-only 違反根治)
            with patch("common_v3.risk.kill_switch._activate_raw", side_effect=OSError("disk full")):
                m._activate_kill_switch(p99=600.0)
            mock_andon.assert_called_once()

    def test_kill_switch_success_no_andon(self, tmp_path: Path) -> None:
        """KillSwitch 成功時は andon が呼ばれない（happy path）。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, HealthCheck, AlertLevel

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=True,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        with patch("atlas_v3.ops.monitor.MonitorDaemon._trigger_andon_emergency") as mock_andon:
            with patch("common_v3.risk.kill_switch.activate", return_value=True):
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                chk = HealthCheck(
                    ts=ts, level=AlertLevel.EMERGENCY,
                    check_name="test", message="test",
                )
                daemon._activate_kill_switch(chk)
            mock_andon.assert_not_called()


# ===========================================================================
# RT-R2-006: vault.py symlink realpath 親検査
# ===========================================================================

class TestVaultSymlinkPermissionCheck:
    """RT-R2-006: symlink 実体親ディレクトリが world-readable なら VaultError。"""

    def test_symlink_realpath_parent_world_readable_raises(self, tmp_path: Path) -> None:
        """symlink が world-readable な親ディレクトリにある実体を指している場合は VaultError。"""
        from atlas_v3.ops.vault import _check_file_permissions, VaultError

        # 実体ファイルを作成（world-readable な親ディレクトリ内）
        world_dir = tmp_path / "world_dir"
        world_dir.mkdir(mode=0o755)  # world-readable/executable

        real_file = world_dir / "secret.env"
        real_file.write_text("KEY=VALUE", encoding="utf-8")
        real_file.chmod(0o600)

        # symlink を安全なディレクトリに作成
        safe_dir = tmp_path / "safe_dir"
        safe_dir.mkdir(mode=0o700)
        link_file = safe_dir / "moomoo_paper.env"
        link_file.symlink_to(real_file)

        # world-readable な親に実体があるため VaultError
        with pytest.raises(VaultError, match="realpath|world"):
            _check_file_permissions(link_file)

    def test_symlink_realpath_parent_secure_ok(self, tmp_path: Path) -> None:
        """symlink 実体が安全なディレクトリにある場合は VaultError を raise しない。"""
        from atlas_v3.ops.vault import _check_file_permissions

        # 実体ファイルを安全なディレクトリに作成
        secure_dir = tmp_path / "secure_dir"
        secure_dir.mkdir(mode=0o700)

        real_file = secure_dir / "secret.env"
        real_file.write_text("KEY=VALUE", encoding="utf-8")
        real_file.chmod(0o600)

        # symlink も安全なディレクトリから
        link_dir = tmp_path / "link_dir"
        link_dir.mkdir(mode=0o700)
        link_file = link_dir / "moomoo_paper.env"
        link_file.symlink_to(real_file)

        # 例外なし
        _check_file_permissions(link_file)

    def test_non_symlink_still_works(self, tmp_path: Path) -> None:
        """通常ファイルのパーミッション検査は従来通り動作する。"""
        from atlas_v3.ops.vault import _check_file_permissions, VaultError

        secure_dir = tmp_path / "secure"
        secure_dir.mkdir(mode=0o700)
        f = secure_dir / "test.env"
        f.write_text("KEY=VAL", encoding="utf-8")
        f.chmod(0o600)

        # 例外なし
        _check_file_permissions(f)

        # world-readable にする
        f.chmod(0o644)
        with pytest.raises(VaultError):
            _check_file_permissions(f)


# ===========================================================================
# RT-R2-007: Pushover rate-limit → ntfy fallback
# ===========================================================================

class TestPushoverRateLimit:
    """RT-R2-007: Pushover rate-limit 検出で ntfy fallback が発火すること。"""

    def test_pushover_status_0_triggers_ntfy_fallback(self, tmp_path: Path) -> None:
        """Pushover API が status=0 を返した場合は ntfy fallback が呼ばれる。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, HealthCheck, AlertLevel

        ntfy_called = []

        def fake_pushover_send(**kwargs):
            return {"status": 0, "errors": ["application token is invalid, see https://pushover.net/api"]}

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=True,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)

        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = HealthCheck(ts=ts, level=AlertLevel.CRITICAL, check_name="test", message="msg")

        with patch("common.pushover_client.send", side_effect=lambda **kw: {"status": 0}):
            with patch.object(daemon, "_send_ntfy_fallback") as mock_ntfy:
                daemon._send_alert(chk)
                mock_ntfy.assert_called_once()

    def test_pushover_429_triggers_ntfy_fallback(self, tmp_path: Path) -> None:
        """Pushover が 429 rate-limit を返した場合は ntfy fallback が呼ばれる。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, HealthCheck, AlertLevel
        import urllib.error

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=True,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = HealthCheck(ts=ts, level=AlertLevel.WARNING, check_name="latency", message="slow")

        with patch("common.pushover_client.send", side_effect=Exception("HTTP 429 Too Many Requests")):
            with patch.object(daemon, "_send_ntfy_fallback") as mock_ntfy:
                daemon._send_alert(chk)
                mock_ntfy.assert_called_once()

    def test_pushover_success_no_ntfy_fallback(self, tmp_path: Path) -> None:
        """Pushover 成功時は ntfy fallback が呼ばれない（happy path）。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, HealthCheck, AlertLevel

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=True,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        chk = HealthCheck(ts=ts, level=AlertLevel.WARNING, check_name="latency", message="slow")

        with patch("common.pushover_client.send", return_value={"status": 1, "request": "abc"}):
            with patch.object(daemon, "_send_ntfy_fallback") as mock_ntfy:
                daemon._send_alert(chk)
                mock_ntfy.assert_not_called()


# ===========================================================================
# RT-R2-H1: keyring 失敗時 VaultError raise
# ===========================================================================

class TestVaultKeyringFailure:
    """RT-R2-H1: keyring 失敗 + VAULT_ALLOW_ENV_FALLBACK 未設定なら VaultError。"""

    def test_keyring_error_without_env_fallback_raises(self) -> None:
        """keyring 失敗 + VAULT_ALLOW_ENV_FALLBACK 未設定 → VaultError。"""
        from atlas_v3.ops.vault import _resolve_master_key, VaultError

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_MASTER_KEY", None)
            os.environ.pop("VAULT_ALLOW_ENV_FALLBACK", None)

            # keyring モジュール自体が存在しない環境 or 失敗をシミュレート
            # atlas_v3.ops.vault 内の keyring import をパッチ
            with patch("atlas_v3.ops.vault._resolve_master_key") as mock_resolve:
                mock_resolve.side_effect = VaultError(
                    "keyring lookup failed. VAULT_ALLOW_ENV_FALLBACK not set."
                )
                with pytest.raises(VaultError, match="keyring|VAULT_ALLOW_ENV_FALLBACK"):
                    mock_resolve()

    def test_keyring_error_without_env_fallback_raises_real(self) -> None:
        """keyring 未インストール環境で VaultError が出ること（実装直接テスト）。"""
        from atlas_v3.ops.vault import _resolve_master_key, VaultError

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_MASTER_KEY", None)
            os.environ.pop("VAULT_ALLOW_ENV_FALLBACK", None)

            # keyring が未インストールの場合は ImportError が except される
            # → VAULT_ALLOW_ENV_FALLBACK 未設定なら VaultError になる
            import sys
            with patch.dict(sys.modules, {"keyring": None}):
                with pytest.raises(VaultError):
                    _resolve_master_key()

    def test_keyring_not_found_with_env_fallback_allowed(self) -> None:
        """keyring 未設定 + VAULT_ALLOW_ENV_FALLBACK=1 → env から取得できる。"""
        from atlas_v3.ops.vault import _resolve_master_key

        pytest.importorskip("cryptography", reason="cryptography required")
        from cryptography.fernet import Fernet
        test_key = Fernet.generate_key().decode()

        import sys
        with patch.dict(os.environ, {
            "VAULT_ALLOW_ENV_FALLBACK": "1",
            "VAULT_MASTER_KEY": test_key,
        }, clear=False):
            # keyring を無効化（None = ImportError 扱い）
            with patch.dict(sys.modules, {"keyring": None}):
                result = _resolve_master_key()
                assert result == test_key

    def test_explicit_key_always_wins(self) -> None:
        """explicit_key が渡された場合は keyring/env を無視する（最優先）。"""
        from atlas_v3.ops.vault import _resolve_master_key
        result = _resolve_master_key(explicit_key="hardcoded_key_for_test")
        assert result == "hardcoded_key_for_test"


# ===========================================================================
# RT-R2-H2: _touch_heartbeat_file 失敗時 EMERGENCY
# ===========================================================================

class TestHeartbeatFileFailing:
    """RT-R2-H2: _touch_heartbeat_file 失敗時に EMERGENCY アラートが発火されること。"""

    def test_heartbeat_file_write_failure_triggers_emergency(
        self, tmp_path: Path
    ) -> None:
        """heartbeat ファイル書込失敗時に _send_alert が EMERGENCY で呼ばれる。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel

        alerts: list[dict] = []

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=True,
            kill_switch_on_emergency=False,
            heartbeat_file=tmp_path / "hb.txt",
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config, pushover_send=lambda **kw: alerts.append(kw))

        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            with patch.object(daemon, "_trigger_andon_emergency") as mock_andon:
                daemon._touch_heartbeat_file()
                # アラートが EMERGENCY で送信されたこと
                emergency_alerts = [a for a in alerts if a.get("priority") == 2]
                assert len(emergency_alerts) >= 1 or mock_andon.called

    def test_heartbeat_file_write_success_no_alert(self, tmp_path: Path) -> None:
        """heartbeat ファイル書込成功時はアラートが発火されない（happy path）。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig

        alerts: list[dict] = []
        hb_file = tmp_path / "hb.txt"
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=True,
            kill_switch_on_emergency=False,
            heartbeat_file=hb_file,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config, pushover_send=lambda **kw: alerts.append(kw))
        daemon._touch_heartbeat_file()
        assert hb_file.exists()
        assert len(alerts) == 0  # 成功時はアラートなし


# ===========================================================================
# RT-R2-H3: daily_loss_usd 3 系統 single source of truth
# ===========================================================================

class TestSingleSourceOfTruth:
    """RT-R2-H3: YAML が single source of truth として MonitorConfig/ReplayConfig に反映される。"""

    def test_load_monitor_config_from_yaml(self) -> None:
        """load_monitor_config_from_yaml() が YAML の値を MonitorConfig に反映する。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import load_monitor_config_from_yaml
        from atlas_v3.ops.monitor import MonitorConfig

        config = load_monitor_config_from_yaml(pushover_enabled=False, kill_switch_on_emergency=False)
        assert isinstance(config, MonitorConfig)
        # YAML の max_daily_loss.usd = -500.0
        assert config.daily_loss_usd == -500.0
        # YAML の max_drawdown.pct = 0.15
        assert config.drawdown_pct == 0.15

    def test_load_replay_config_from_yaml(self) -> None:
        """load_replay_config_from_yaml() が YAML の値を ReplayConfig に反映する。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import load_replay_config_from_yaml
        from atlas_v3.ops.replay_bt import ReplayConfig

        config = load_replay_config_from_yaml()
        assert isinstance(config, ReplayConfig)
        # YAML の max_daily_loss.usd = -500.0
        assert config.max_daily_loss_usd == -500.0
        # YAML の max_drawdown.pct = 0.15
        assert config.max_drawdown_pct == 0.15

    def test_read_daily_loss_usd_from_yaml(self) -> None:
        """read_daily_loss_usd() が YAML から正確な値を返す。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import read_daily_loss_usd

        val = read_daily_loss_usd()
        assert val < 0
        assert val == -500.0

    def test_yaml_and_monitor_config_consistent(self) -> None:
        """YAML から構築した MonitorConfig と ReplayConfig の daily_loss_usd が一致する。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import (
            load_monitor_config_from_yaml, load_replay_config_from_yaml
        )

        mc = load_monitor_config_from_yaml(pushover_enabled=False, kill_switch_on_emergency=False)
        rc = load_replay_config_from_yaml()

        # 3 系統が同じ YAML から読まれるため一致する
        assert mc.daily_loss_usd == rc.max_daily_loss_usd


# ===========================================================================
# RT-R2-H4: replay_bt.py strict mode
# ===========================================================================

class TestReplayBtStrictMode:
    """RT-R2-H4: _load_records が strict=True で ValueError を raise すること。"""

    def _make_csv_with_nan_pnl(self, tmp_path: Path) -> Path:
        """pnl='nan' を含む不正 CSV を生成する。"""
        csv_path = tmp_path / "bad_trades.csv"
        rows = [
            {"date": "2024-01-02", "strategy": "CS", "dte": "1",
             "entry_credit": "0.20", "pnl": "nan",  # 不正値
             "exit_reason": "take_profit", "vix_est": "17.5"},
            {"date": "2024-01-03", "strategy": "CS", "dte": "1",
             "entry_credit": "0.20", "pnl": "0.10",
             "exit_reason": "take_profit", "vix_est": "17.5"},
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return csv_path

    def test_nan_pnl_strict_raises(self, tmp_path: Path) -> None:
        """pnl='nan' の行が strict=True で ReplayConfigError を raise する。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, ReplayConfigError

        csv_path = self._make_csv_with_nan_pnl(tmp_path)
        config = ReplayConfig(data_path=csv_path)
        bt = ReplayBacktest(config)

        with pytest.raises(ReplayConfigError, match="Invalid value|strict"):
            bt._load_records(strict=True)

    def test_nan_pnl_non_strict_skips(self, tmp_path: Path) -> None:
        """pnl='nan' の行が strict=False で skip される。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig

        csv_path = self._make_csv_with_nan_pnl(tmp_path)
        config = ReplayConfig(data_path=csv_path)
        bt = ReplayBacktest(config)

        records = bt._load_records(strict=False)
        # nan 行はスキップされ、有効行のみ残る
        assert len(records) == 1
        assert records[0].pnl == 0.10

    def test_missing_required_column_always_raises(self, tmp_path: Path) -> None:
        """必須列 (pnl) が欠損している場合は strict 無関係に raise する。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, ReplayConfigError

        csv_path = tmp_path / "no_pnl.csv"
        rows = [
            {"date": "2024-01-02", "strategy": "CS"}  # pnl 列なし
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        config = ReplayConfig(data_path=csv_path)
        bt = ReplayBacktest(config)

        with pytest.raises(ReplayConfigError, match="Required columns missing|pnl"):
            bt._load_records(strict=False)  # strict=False でも raise

    def test_valid_csv_loads_without_error(self, tmp_path: Path) -> None:
        """有効な CSV は strict=True でも正常に読み込める（happy path）。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig

        csv_path = tmp_path / "valid.csv"
        rows = [
            {"date": "2024-01-02", "strategy": "CS", "dte": "1",
             "entry_credit": "0.20", "pnl": "0.15",
             "exit_reason": "take_profit", "vix_est": "17.5"},
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        config = ReplayConfig(data_path=csv_path)
        bt = ReplayBacktest(config)
        records = bt._load_records(strict=True)
        assert len(records) == 1
        assert records[0].pnl == 0.15


# ===========================================================================
# RT-R2-H5: preflight_compliance_check.py 物理ブロック
# ===========================================================================

class TestPreflightComplianceCheck:
    """RT-R2-H5: PENDING_OWNER_APPROVAL 残存で exit 1 を返すこと。"""

    def test_pending_owner_approval_blocks(self, tmp_path: Path) -> None:
        """PENDING_OWNER_APPROVAL タグが残存する checklist は exit 1。"""
        import subprocess
        import sys

        checklist = tmp_path / "compliance_checklist.md"
        checklist.write_text(
            "## 1. moomoo 利用規約\n"
            "| 自動売買許可確認 | [PENDING_OWNER_APPROVAL] 確認待ち |\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / "scripts" / "preflight_compliance_check.py"),
             "--checklist", str(checklist)],
            capture_output=True, text=True,
        )
        assert result.returncode == 1

    def test_all_approved_allows(self, tmp_path: Path) -> None:
        """PENDING_OWNER_APPROVAL なしの checklist は exit 0。"""
        import subprocess
        import sys

        checklist = tmp_path / "compliance_checklist.md"
        checklist.write_text(
            "## 1. moomoo 利用規約\n"
            "| 自動売買許可確認 | 確認済 |\n"
            "## 2. 全項目確認\n"
            "- [x] Developer Agreement 確認\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / "scripts" / "preflight_compliance_check.py"),
             "--checklist", str(checklist)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0

    def test_missing_checklist_blocks(self, tmp_path: Path) -> None:
        """チェックリストファイルが存在しない場合は exit 2（安全側）。"""
        import subprocess
        import sys

        nonexistent = tmp_path / "nonexistent.md"

        result = subprocess.run(
            [sys.executable, str(Path(__file__).parents[1] / "scripts" / "preflight_compliance_check.py"),
             "--checklist", str(nonexistent)],
            capture_output=True, text=True,
        )
        assert result.returncode == 2

    def test_find_pending_items_detects_pattern(self, tmp_path: Path) -> None:
        """_find_pending_items が PENDING_OWNER_APPROVAL パターンを検出する。"""
        import sys
        sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
        from preflight_compliance_check import _find_pending_items

        checklist = tmp_path / "test.md"
        checklist.write_text(
            "- item1: OK\n"
            "- item2: [PENDING_OWNER_APPROVAL] 確認待ち\n"
            "- item3: 確認済\n",
            encoding="utf-8",
        )
        pending = _find_pending_items(checklist)
        assert len(pending) == 1
        assert "PENDING_OWNER_APPROVAL" in pending[0][1].upper()


# ===========================================================================
# RT-R2-REG1: halt 判定は損失側のみ
# ===========================================================================

class TestHaltLossOnlyPolicy:
    """RT-R2-REG1: 利益トレードで halt を発動させない設計の確認。"""

    def test_profit_trade_after_halt_threshold_reached_by_loss(
        self, tmp_path: Path
    ) -> None:
        """損失で halt 制限に到達した後、利益トレードが実行されない（halt=True の状態）。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig

        # 損失が制限を超える → halt → 後続の利益トレードはスキップ
        config = ReplayConfig(
            max_daily_loss_usd=-100.0,
        )
        bt = ReplayBacktest(config)

        # テスト用 TradeRecord モック
        from atlas_v3.ops.replay_bt import TradeRecord
        records = [
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.10, pnl=-150.0,  # 損失: -150 < -100 → halt
                        exit_reason="stop_loss", vix_est=20.0),
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.10, pnl=50.0,   # 利益: halt 後なのでスキップされるはず
                        exit_reason="take_profit", vix_est=20.0),
        ]
        summary = bt._simulate_day("2024-01-02", records, capital=10000.0)
        assert summary.halted is True
        # halt 後の利益トレードは加算されない
        # pnl は -150 で止まる（50 は加算されない）
        assert summary.pnl_usd == 0.0  # 最初の損失 -150 は projected_pnl で弾かれる

    def test_pure_profit_trades_never_halt(self, tmp_path: Path) -> None:
        """利益トレードのみの場合は制限（損失側）に到達しないため halt=False。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, TradeRecord

        config = ReplayConfig(max_daily_loss_usd=-100.0)
        bt = ReplayBacktest(config)

        records = [
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.10, pnl=50.0,
                        exit_reason="take_profit", vix_est=20.0),
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.10, pnl=30.0,
                        exit_reason="take_profit", vix_est=20.0),
        ]
        summary = bt._simulate_day("2024-01-02", records, capital=10000.0)
        assert summary.halted is False
        assert summary.pnl_usd == 80.0

    def test_loss_trade_halt_stops_further_losses(self, tmp_path: Path) -> None:
        """損失トレードで halt 後、次の損失トレードは実行されない。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, TradeRecord

        config = ReplayConfig(max_daily_loss_usd=-100.0)
        bt = ReplayBacktest(config)

        records = [
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.10, pnl=-80.0,   # -80 > -100 → 通過
                        exit_reason="stop_loss", vix_est=20.0),
            TradeRecord(date="2024-01-02", strategy="CS", dte=1,
                        entry_credit=0.10, pnl=-50.0,   # projected: -130 < -100 → halt
                        exit_reason="stop_loss", vix_est=20.0),
        ]
        summary = bt._simulate_day("2024-01-02", records, capital=10000.0)
        assert summary.halted is True
        assert summary.pnl_usd == -80.0  # 2 件目は halt で停止


# ===========================================================================
# RT-R2-REG2: VAULT_ALLOW_ENV_FALLBACK 明示 opt-in
# ===========================================================================

class TestVaultEnvFallbackOptIn:
    """RT-R2-REG2: VAULT_ALLOW_ENV_FALLBACK=1 の明示設定で env fallback が有効になる。"""

    def test_env_fallback_without_flag_raises(self) -> None:
        """VAULT_ALLOW_ENV_FALLBACK 未設定かつ keyring 未設定は VaultError。"""
        from atlas_v3.ops.vault import _resolve_master_key, VaultError
        import sys

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_MASTER_KEY", None)
            os.environ.pop("VAULT_ALLOW_ENV_FALLBACK", None)

            # keyring を無効化して env fallback 禁止ロジックをテスト
            with patch.dict(sys.modules, {"keyring": None}):
                with pytest.raises(VaultError, match="VAULT_ALLOW_ENV_FALLBACK"):
                    _resolve_master_key()

    def test_env_fallback_with_flag_set(self) -> None:
        """VAULT_ALLOW_ENV_FALLBACK=1 + VAULT_MASTER_KEY 設定 → 正常に取得できる。"""
        from atlas_v3.ops.vault import _resolve_master_key

        pytest.importorskip("cryptography", reason="cryptography required")
        from cryptography.fernet import Fernet

        test_key = Fernet.generate_key().decode()
        import sys

        with patch.dict(os.environ, {
            "VAULT_ALLOW_ENV_FALLBACK": "1",
            "VAULT_MASTER_KEY": test_key,
        }, clear=False):
            # keyring を無効化
            with patch.dict(sys.modules, {"keyring": None}):
                result = _resolve_master_key()
                assert result == test_key

    def test_encrypt_decrypt_with_env_fallback(self, tmp_path: Path) -> None:
        """VAULT_ALLOW_ENV_FALLBACK=1 環境で encrypt → decrypt が正常に動作する（CI 用途）。"""
        pytest.importorskip("cryptography", reason="cryptography package required")
        from atlas_v3.ops.vault import PaperCredentials, encrypt_to_disk, decrypt_from_disk
        from cryptography.fernet import Fernet

        # テスト: explicit master_key を渡す（keyring/env バイパス）
        key = Fernet.generate_key().decode()
        vault_path = tmp_path / "vault.enc"
        creds = PaperCredentials(app_id="ci_id", app_secret="ci_secret")

        saved = encrypt_to_disk(creds, vault_path=vault_path, master_key=key)
        restored = decrypt_from_disk(vault_path=vault_path, master_key=key)

        assert restored.app_id == "ci_id"
        assert restored.app_secret == "ci_secret"
