"""tests/test_atlas_v3_paper_ops.py — Sprint 1-B Phase B / Paper 運用要件 7 項目テスト

テスト対象:
    1. atlas_v3/ops/vault.py          — vault 暗号化・復号・load_from_env
    2. data/configs/atlas_paper_risk.yaml — config 読み込み・RiskConfig 変換
    3. atlas_v3/ops/monitor.py        — daemon alert・KillSwitch 連動
    4. atlas_v3/ops/latency_monitor.py — latency 閾値・backoff・HALT
    5. atlas_v3/ops/replay_bt.py      — replay 実行・walk-forward
    6. data/ops/runbook_atlas_paper_20260423.md — runbook 整合
    7. data/ops/compliance_checklist_20260423.md — compliance 項目

テスト数: >= 24
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# Item 1: vault.py テスト (5 tests)
# ===========================================================================

class TestVault:
    """atlas_v3/ops/vault.py のテスト。"""

    def test_load_from_env_valid(self, tmp_path: Path) -> None:
        """有効な .env ファイルから PaperCredentials を読み込める。"""
        env_file = tmp_path / "moomoo_paper.env"
        env_file.write_text(
            "MOOMOO_APP_ID=test_app_id_001\n"
            "MOOMOO_APP_SECRET=test_secret_abc123\n"
            "MOOMOO_HOST=127.0.0.1\n"
            "MOOMOO_PORT=11111\n"
            "MOOMOO_TRD_ENV=SIMULATE\n",
            encoding="utf-8",
        )
        # CRITICAL 2 修正: 0600 パーミッションが必要
        env_file.chmod(0o600)
        from atlas_v3.ops.vault import load_from_env
        creds = load_from_env(env_path=env_file)
        assert creds.app_id == "test_app_id_001"
        assert creds.app_secret == "test_secret_abc123"
        assert creds.host == "127.0.0.1"
        assert creds.port == 11111
        assert creds.trd_env == "SIMULATE"

    def test_load_from_env_missing_key_raises(self, tmp_path: Path) -> None:
        """MOOMOO_APP_ID が欠けている場合は VaultError を raise する。"""
        env_file = tmp_path / "empty.env"
        env_file.write_text("MOOMOO_APP_SECRET=secret\n", encoding="utf-8")
        # CRITICAL 2 修正: 0600 パーミッションが必要
        env_file.chmod(0o600)
        from atlas_v3.ops.vault import load_from_env, VaultError
        with pytest.raises(VaultError, match="MOOMOO_APP_ID"):
            load_from_env(env_path=env_file)

    def test_credentials_non_simulate_raises(self) -> None:
        """trd_env が SIMULATE 以外なら VaultError を raise する（Paper 専用保護）。"""
        from atlas_v3.ops.vault import PaperCredentials, VaultError
        with pytest.raises(VaultError, match="SIMULATE"):
            PaperCredentials(
                app_id="id",
                app_secret="secret",
                trd_env="REAL",
            )

    def test_credentials_repr_masks_secret(self) -> None:
        """__repr__ は app_secret をマスクする（ログ漏洩防止）。"""
        from atlas_v3.ops.vault import PaperCredentials
        creds = PaperCredentials(app_id="myid", app_secret="supersecretvalue")
        repr_str = repr(creds)
        assert "supersecretvalue" not in repr_str
        assert "****" in repr_str
        assert "myid" in repr_str

    def test_encrypt_decrypt_roundtrip(self, tmp_path: Path) -> None:
        """encrypt_to_disk -> decrypt_from_disk の往復が正しく動作する。"""
        pytest.importorskip("cryptography", reason="cryptography package required")
        from cryptography.fernet import Fernet
        from atlas_v3.ops.vault import (
            PaperCredentials, encrypt_to_disk, decrypt_from_disk
        )
        key = Fernet.generate_key().decode()
        vault_path = tmp_path / "vault_paper.enc"
        creds = PaperCredentials(app_id="roundtrip_id", app_secret="roundtrip_secret")

        saved_path = encrypt_to_disk(creds, vault_path=vault_path, master_key=key)
        assert saved_path.exists()

        restored = decrypt_from_disk(vault_path=vault_path, master_key=key)
        assert restored.app_id == creds.app_id
        assert restored.app_secret == creds.app_secret
        assert restored.trd_env == "SIMULATE"

    def test_decrypt_wrong_key_raises(self, tmp_path: Path) -> None:
        """間違ったキーで復号しようとすると VaultError を raise する。"""
        pytest.importorskip("cryptography", reason="cryptography package required")
        from cryptography.fernet import Fernet
        from atlas_v3.ops.vault import (
            PaperCredentials, encrypt_to_disk, decrypt_from_disk, VaultError
        )
        key1 = Fernet.generate_key().decode()
        key2 = Fernet.generate_key().decode()
        vault_path = tmp_path / "vault_bad.enc"
        creds = PaperCredentials(app_id="id", app_secret="secret")
        encrypt_to_disk(creds, vault_path=vault_path, master_key=key1)

        with pytest.raises(VaultError, match="[Dd]ecryption failed"):
            decrypt_from_disk(vault_path=vault_path, master_key=key2)

    def test_encrypt_no_master_key_raises(self, tmp_path: Path) -> None:
        """VAULT_MASTER_KEY が未設定なら encrypt_to_disk は VaultError を raise する。"""
        pytest.importorskip("cryptography", reason="cryptography package required")
        from atlas_v3.ops.vault import PaperCredentials, encrypt_to_disk, VaultError
        vault_path = tmp_path / "vault.enc"
        creds = PaperCredentials(app_id="id", app_secret="secret")
        # 環境変数未設定 + master_key=None → VaultError
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_MASTER_KEY", None)
            with pytest.raises(VaultError, match="VAULT_MASTER_KEY"):
                encrypt_to_disk(creds, vault_path=vault_path, master_key=None)


# ===========================================================================
# Item 2: atlas_paper_risk.yaml / RiskConfig テスト (4 tests)
# ===========================================================================

class TestRiskConfigLoader:
    """atlas_v3/ops/risk_config_loader.py のテスト。"""

    def test_load_paper_risk_config_returns_risk_config(self) -> None:
        """load_paper_risk_config() が RiskConfig インスタンスを返す。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import load_paper_risk_config
        from common_v3.risk.engine import RiskConfig
        config = load_paper_risk_config()
        assert isinstance(config, RiskConfig)

    def test_load_paper_risk_config_values(self) -> None:
        """load_paper_risk_config() の値が YAML と整合している。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import load_paper_risk_config
        config = load_paper_risk_config()
        # max_daily_loss_usd は負値のはず
        assert config.max_daily_loss_usd < 0
        # max_drawdown_pct は (0, 1] の範囲
        assert 0.0 < config.max_drawdown_pct <= 1.0
        # max_notional_usd は正値
        assert config.max_notional_usd > 0

    def test_load_paper_risk_config_missing_file(self, tmp_path: Path) -> None:
        """存在しないファイルパスを渡すと RiskConfigLoadError を raise する。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import load_paper_risk_config, RiskConfigLoadError
        with pytest.raises(RiskConfigLoadError, match="not found"):
            load_paper_risk_config(config_path=tmp_path / "nonexistent.yaml")

    def test_load_paper_risk_config_invalid_yaml(self, tmp_path: Path) -> None:
        """不正な YAML を渡すと RiskConfigLoadError を raise する。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import load_paper_risk_config, RiskConfigLoadError
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("max_notional: {usd: }", encoding="utf-8")
        with pytest.raises(RiskConfigLoadError):
            load_paper_risk_config(config_path=bad_yaml)

    def test_load_paper_risk_config_custom_values(self, tmp_path: Path) -> None:
        """カスタム YAML から RiskConfig が正しく構築される。"""
        pytest.importorskip("yaml", reason="PyYAML required")
        from atlas_v3.ops.risk_config_loader import load_paper_risk_config
        custom_yaml = tmp_path / "custom.yaml"
        custom_yaml.write_text(
            "mode: paper\n"
            "max_notional:\n  usd: 5000.0\n"
            "max_daily_loss:\n  usd: -200.0\n"
            "max_drawdown:\n  pct: 0.10\n"
            "max_var:\n  usd: 1000.0\n"
            "sizing:\n  method: FIXED\n  fixed_size_contracts: 1\n",
            encoding="utf-8",
        )
        config = load_paper_risk_config(config_path=custom_yaml)
        assert config.max_notional_usd == 5000.0
        assert config.max_daily_loss_usd == -200.0
        assert config.max_drawdown_pct == 0.10


# ===========================================================================
# Item 3: monitor.py テスト (4 tests)
# ===========================================================================

class TestMonitorDaemon:
    """atlas_v3/ops/monitor.py のテスト。"""

    def test_check_once_returns_health_checks(self, tmp_path: Path) -> None:
        """check_once() が HealthCheck のリストを返す。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, HealthCheck
        config = MonitorConfig(
            check_interval_secs=1.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        checks = daemon.check_once(pnl_day_usd=0.0, drawdown_pct=0.0, latency_ms=10.0)
        assert isinstance(checks, list)
        assert len(checks) == 4  # heartbeat + daily_loss + drawdown + latency
        assert all(isinstance(c, HealthCheck) for c in checks)

    def test_daily_loss_breach_triggers_alert(self, tmp_path: Path) -> None:
        """日次損失制限超過で CRITICAL/EMERGENCY アラートが発火される。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel
        alerts = []

        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=True,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config, pushover_send=lambda **kw: alerts.append(kw))
        # pnl_day_usd <= daily_loss_usd でアラート発火
        checks = daemon.check_once(pnl_day_usd=-450.0, drawdown_pct=0.0, latency_ms=10.0)
        daily_check = next(c for c in checks if c.check_name == "daily_loss")
        assert daily_check.level in (AlertLevel.CRITICAL, AlertLevel.EMERGENCY)
        assert len(alerts) >= 1

    def test_heartbeat_timeout_triggers_emergency(self, tmp_path: Path) -> None:
        """heartbeat timeout で EMERGENCY が発火される。"""
        import time
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig, AlertLevel
        config = MonitorConfig(
            heartbeat_timeout_secs=0.01,  # 10ms — ほぼ即タイムアウト
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        time.sleep(0.05)  # timeout を確実に超える
        checks = daemon.check_once()
        hb_check = next(c for c in checks if c.check_name == "heartbeat")
        assert hb_check.level == AlertLevel.EMERGENCY

    def test_monitor_start_stop(self, tmp_path: Path) -> None:
        """daemon が起動・停止できる。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig
        config = MonitorConfig(
            check_interval_secs=0.1,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=tmp_path / "monitor.jsonl",
        )
        daemon = MonitorDaemon(config)
        daemon.start()
        assert daemon.is_running()
        daemon.stop(timeout=2.0)
        assert not daemon.is_running()

    def test_monitor_log_written(self, tmp_path: Path) -> None:
        """check_once() 後に JSONL ログが書き込まれる。"""
        from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig
        log_path = tmp_path / "monitor.jsonl"
        config = MonitorConfig(
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            log_path=log_path,
        )
        daemon = MonitorDaemon(config)
        daemon.check_once(pnl_day_usd=0.0, drawdown_pct=0.0, latency_ms=10.0)
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
        assert len(lines) >= 1
        assert "check_name" in lines[0]


# ===========================================================================
# Item 4: latency_monitor.py テスト (5 tests)
# ===========================================================================

class TestLatencyMonitor:
    """atlas_v3/ops/latency_monitor.py のテスト。"""

    def test_record_allow_under_warn(self, tmp_path: Path) -> None:
        """p99 が warn 閾値未満なら ALLOW を返す。

        NEW-C-3 対応: window_size は _MIN_SAMPLES_FOR_P99=100 以上必須。
        旧テストの window_size=10 は禁止になったため 100 に変更。
        p99 判定には 100 サンプル以上必要なため 100件記録する。
        """
        from atlas_v3.ops.latency_monitor import LatencyMonitor, LatencyConfig, LatencyDecision
        config = LatencyConfig(
            window_size=100,
            p99_warn_ms=200.0,
            p99_halt_ms=1000.0,
            persist_samples=False,
        )
        m = LatencyMonitor(config)
        # NEW-C-3: _MIN_SAMPLES_FOR_P99=100 に達するまで cold-start → ALLOW
        # 100件記録して p99 を確定させる
        for _ in range(100):
            decision = m.record(latency_ms=50.0)
        assert decision == LatencyDecision.ALLOW

    def test_record_backoff_over_warn(self, tmp_path: Path) -> None:
        """p99 が warn 閾値以上なら BACKOFF を返す。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor, LatencyConfig, LatencyDecision
        config = LatencyConfig(
            window_size=500,
            p99_warn_ms=100.0,
            p99_halt_ms=500.0,
            persist_samples=False,
        )
        m = LatencyMonitor(config)
        # CRITICAL 3 修正: min_samples=100 必要なので 100 件追加
        for _ in range(100):
            m.record(latency_ms=200.0)
        assert m.decide() == LatencyDecision.BACKOFF

    def test_record_halt_over_halt_threshold(self) -> None:
        """p99 が halt 閾値以上なら HALT を返し halted フラグが立つ。"""
        from atlas_v3.ops.latency_monitor import (
            LatencyMonitor, LatencyConfig, LatencyDecision
        )
        ks_calls = []
        config = LatencyConfig(
            window_size=500,
            p99_warn_ms=100.0,
            p99_halt_ms=500.0,
            kill_switch_on_halt=True,
            persist_samples=False,
        )
        m = LatencyMonitor(
            config,
            kill_switch_activate=lambda **kw: ks_calls.append(kw),
        )
        # CRITICAL 3 修正: min_samples=100 必要なので 100 件追加
        for _ in range(100):
            m.record(latency_ms=1500.0)
        assert m.decide() == LatencyDecision.HALT
        assert len(ks_calls) >= 1

    def test_backoff_factor(self) -> None:
        """backoff_factor() が設定値を返す。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor, LatencyConfig
        config = LatencyConfig(
            p99_warn_ms=100.0, p99_halt_ms=500.0,
            backoff_multiplier=3.0,
            persist_samples=False,
        )
        m = LatencyMonitor(config)
        assert m.backoff_factor() == 3.0

    def test_reset_clears_samples(self) -> None:
        """reset() がサンプルを全て消去して halted フラグをクリアする。"""
        from atlas_v3.ops.latency_monitor import LatencyMonitor, LatencyConfig, LatencyDecision
        config = LatencyConfig(
            window_size=500, p99_warn_ms=50.0, p99_halt_ms=100.0,
            persist_samples=False, kill_switch_on_halt=False,
        )
        m = LatencyMonitor(config)
        # CRITICAL 3 修正: min_samples=100 必要なので 100 件追加
        for _ in range(100):
            m.record(latency_ms=200.0)
        assert m.decide() == LatencyDecision.HALT
        m.reset()
        assert m.sample_count() == 0
        assert m.decide() == LatencyDecision.ALLOW  # reset 後はサンプル不足で ALLOW

    def test_latency_timer_context_manager(self) -> None:
        """LatencyTimer context manager が経過時間を記録する。"""
        import time
        from atlas_v3.ops.latency_monitor import (
            LatencyMonitor, LatencyConfig, LatencyTimer
        )
        config = LatencyConfig(
            p99_warn_ms=10000.0, p99_halt_ms=20000.0,
            persist_samples=False,
        )
        m = LatencyMonitor(config)
        with LatencyTimer(m, source="test") as timer:
            time.sleep(0.005)
        assert timer.elapsed_ms >= 1.0  # 最低 1ms
        assert m.sample_count() == 1


# ===========================================================================
# Item 5: replay_bt.py テスト (4 tests)
# ===========================================================================

class TestReplayBacktest:
    """atlas_v3/ops/replay_bt.py のテスト。"""

    def _make_csv(self, tmp_path: Path, n_days: int = 60) -> Path:
        """テスト用 CSV データを生成する。"""
        csv_path = tmp_path / "trades.csv"
        rows = []
        base = datetime.date(2024, 1, 2)
        for i in range(n_days):
            d = base + datetime.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            rows.append({
                "date": d.isoformat(),
                "strategy": "CS",
                "dte": 1,
                "entry_credit": "0.20",
                "pnl": "0.15" if i % 3 != 0 else "-0.05",
                "exit_reason": "take_profit" if i % 3 != 0 else "stop_loss",
                "width": "3",
                "target_delta": "0.1",
                "stop_mult": "",
                "take_profit": "0.75",
                "vix_est": "17.5",
            })
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["date", "strategy", "dte", "entry_credit", "pnl",
                             "exit_reason", "width", "target_delta", "stop_mult",
                             "take_profit", "vix_est"],
            )
            writer.writeheader()
            writer.writerows(rows)
        return csv_path

    def test_run_returns_walk_forward_result(self, tmp_path: Path) -> None:
        """run() が WalkForwardResult を返す。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig, WalkForwardResult
        csv_path = self._make_csv(tmp_path, n_days=100)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "results",
            train_months=1,
            test_months=1,
        )
        bt = ReplayBacktest(config)
        result = bt.run()
        assert isinstance(result, WalkForwardResult)
        assert result.total_trades > 0

    def test_run_missing_data_raises(self, tmp_path: Path) -> None:
        """データファイルが存在しない場合は FileNotFoundError を raise する。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig
        config = ReplayConfig(data_path=tmp_path / "nonexistent.csv")
        bt = ReplayBacktest(config)
        with pytest.raises(FileNotFoundError):
            bt.run()

    def test_run_with_real_data(self) -> None:
        """実データ (1dte_trades_raw.csv) でバックテストが実行できる。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig
        real_csv = Path(__file__).resolve().parents[1] / "data" / "thetadata" / "1dte_trades_raw.csv"
        if not real_csv.exists():
            pytest.skip("1dte_trades_raw.csv not found")
        config = ReplayConfig(
            data_path=real_csv,
            train_months=3,
            test_months=1,
        )
        bt = ReplayBacktest(config)
        result = bt.run()
        assert result.total_trades > 0
        assert result.num_windows >= 1

    def test_save_creates_json_file(self, tmp_path: Path) -> None:
        """save() が JSON ファイルを作成する。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig
        csv_path = self._make_csv(tmp_path, n_days=80)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "results",
            train_months=1,
            test_months=1,
        )
        bt = ReplayBacktest(config)
        result = bt.run()
        saved = bt.save(result, label="test")
        assert saved.exists()
        data = json.loads(saved.read_text())
        assert "total_trades" in data
        assert "sharpe_ratio" in data

    def test_daily_loss_limit_halts_day(self, tmp_path: Path) -> None:
        """日次損失制限に達した日は halted=True になる。"""
        from atlas_v3.ops.replay_bt import ReplayBacktest, ReplayConfig

        # 十分な量の損失トレードを持つ CSV（train_months=1 → 約 20 営業日の学習期間）
        csv_path = tmp_path / "loss_trades.csv"
        rows = []
        base = datetime.date(2024, 1, 2)
        # 180 日分（約 6 ヶ月）生成 — train(1m) + test(1m) に十分
        for i in range(180):
            d = base + datetime.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            # 1 日に 4 件・各 -200 USD → 合計 -800 > 制限 -500
            for _ in range(4):
                rows.append({
                    "date": d.isoformat(),
                    "strategy": "CS",
                    "dte": "1",
                    "entry_credit": "0.10",
                    "pnl": "-200.0",
                    "exit_reason": "stop_loss",
                    "width": "3",
                    "target_delta": "0.1",
                    "stop_mult": "",
                    "take_profit": "0.75",
                    "vix_est": "20.0",
                })
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["date", "strategy", "dte", "entry_credit", "pnl",
                             "exit_reason", "width", "target_delta", "stop_mult",
                             "take_profit", "vix_est"],
            )
            writer.writeheader()
            writer.writerows(rows)

        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "results",
            max_daily_loss_usd=-500.0,
            train_months=1,
            test_months=1,
        )
        bt = ReplayBacktest(config)
        result = bt.run()
        assert result.halted_days > 0


# ===========================================================================
# Item 6: runbook 整合テスト (2 tests)
# ===========================================================================

class TestRunbook:
    """data/ops/runbook_atlas_paper_20260423.md の整合テスト。"""

    _RUNBOOK = (
        Path(__file__).resolve().parents[1]
        / "data" / "ops" / "runbook_atlas_paper_20260423.md"
    )

    def test_runbook_exists(self) -> None:
        """Runbook ファイルが存在する。"""
        assert self._RUNBOOK.exists(), f"Runbook not found: {self._RUNBOOK}"

    def test_runbook_contains_required_sections(self) -> None:
        """Runbook に必須セクションが含まれている。"""
        content = self._RUNBOOK.read_text(encoding="utf-8")
        required = [
            "起動手順",
            "障害対応",
            "ロールバック",
            "Kill Switch",
        ]
        for section in required:
            assert section in content, f"Missing section: {section}"


# ===========================================================================
# Item 7: compliance checklist テスト (2 tests)
# ===========================================================================

class TestComplianceChecklist:
    """data/ops/compliance_checklist_20260423.md の整合テスト。"""

    _CHECKLIST = (
        Path(__file__).resolve().parents[1]
        / "data" / "ops" / "compliance_checklist_20260423.md"
    )

    def test_checklist_exists(self) -> None:
        """Compliance checklist ファイルが存在する。"""
        assert self._CHECKLIST.exists(), f"Checklist not found: {self._CHECKLIST}"

    def test_checklist_contains_required_items(self) -> None:
        """Checklist に必須項目が含まれている。"""
        content = self._CHECKLIST.read_text(encoding="utf-8")
        required = [
            "moomoo",
            "金商法",
            "C2",
            "税務",
            "Kill Switch",
            "SIMULATE",
        ]
        for item in required:
            assert item in content, f"Missing compliance item: {item}"


# ===========================================================================
# 追加: MonitorConfig / LatencyConfig の境界値テスト (2 tests)
# ===========================================================================

class TestConfigValidation:
    """MonitorConfig / LatencyConfig の入力バリデーションテスト。"""

    def test_monitor_config_invalid_daily_loss(self) -> None:
        """MonitorConfig に正値の daily_loss_usd を渡すと ValueError。"""
        from atlas_v3.ops.monitor import MonitorConfig
        with pytest.raises(ValueError, match="daily_loss_usd"):
            MonitorConfig(daily_loss_usd=100.0)

    def test_latency_config_halt_less_than_warn_raises(self) -> None:
        """p99_halt_ms <= p99_warn_ms なら ValueError。"""
        from atlas_v3.ops.latency_monitor import LatencyConfig
        with pytest.raises(ValueError, match="p99_halt_ms"):
            LatencyConfig(p99_warn_ms=500.0, p99_halt_ms=100.0)
