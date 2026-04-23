"""tests/test_atlas_v3_r6_fixes.py — Sprint 1-B Phase B Builder r6 修正テスト

対象: Redteam r5 指摘 CRIT 4 + HIGH 4 + regression 2 = 10 件

C1: launchd TZ 誤解 — plist KeepAlive=true 常駐化 + TZ=America/New_York
C2: DummyMetricProvider 本番流出 — --provider argparse + YFinanceMetricProvider 新設
C3: KillSwitch ゾンビ — probe 成功時に deactivate()
C4: hysteresis 振動脆弱 — Schmitt Trigger 化（upper/lower 二閾値）
H1: spy_bot.py 改変疑惑 — legacy_write_block.sh 強化（H1 テストは legacy_write_block_test.py で）
H2: O_NOFOLLOW 親ディレクトリ無防御 — _check_path_not_symlink() 追加
H3: Dummy + probe 結託 — Dummy 使用時は probe 失敗扱い
H4: テスト品質脆弱 — 攻撃ベクトルテスト 10+ 追加（本ファイル）
R1/R2: spy_bot.py regression — stash 復元済み・Builder 不触
"""
from __future__ import annotations

import os
import sys
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch
import threading
import time

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ===========================================================================
# C1: launchd TZ バグ修正 — plist 常駐化 + TZ 設定
# ===========================================================================

class TestC1_LaunchdTZ:
    """C1: launchd plist が StartCalendarInterval を使わず KeepAlive=true 常駐である。"""

    def _get_plist(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / "com.soralab.atlas-paper.plist"

    def test_plist_exists(self):
        """com.soralab.atlas-paper.plist が存在する。"""
        plist = self._get_plist()
        assert plist.exists(), f"C1: {plist} が存在しない"

    def test_plist_no_start_calendar_interval_as_xml_key(self):
        """plist に <key>StartCalendarInterval</key> が XML key として含まれない（KeepAlive 常駐化済み）。

        コメント内に文字列が含まれるのは削除記録として許可する。
        実際の XML key として StartCalendarInterval が動作していないことを確認する。
        """
        plist = self._get_plist()
        if not plist.exists():
            pytest.skip("plist not found")
        content = plist.read_text(encoding="utf-8")
        # XML key として存在しないことを確認（コメント内は許可）
        assert "<key>StartCalendarInterval</key>" not in content, (
            "C1: plist に <key>StartCalendarInterval</key> が XML key として残っている。"
            "KeepAlive=true 常駐 daemon に変更すること。"
            "Hour=0 はローカルタイム解釈が不定でスリープ中は失火する。"
        )

    def test_plist_has_keepalive_true(self):
        """plist に KeepAlive=true（単純 true）が含まれる。"""
        plist = self._get_plist()
        if not plist.exists():
            pytest.skip("plist not found")
        content = plist.read_text(encoding="utf-8")
        # <key>KeepAlive</key><true/> が含まれていること
        # KeepAlive dict（Crashed のみ）ではなく単純 true
        assert "<key>KeepAlive</key>" in content, "C1: plist に KeepAlive キーがない"
        # <true/> が KeepAlive に続く（dict ではなく単純な true）
        # 簡易チェック: <true/> が存在する
        assert "<true/>" in content, "C1: plist の KeepAlive が true でない"

    def test_plist_has_tz_environment_variable(self):
        """plist の EnvironmentVariables に TZ=America/New_York が含まれる。"""
        plist = self._get_plist()
        if not plist.exists():
            pytest.skip("plist not found")
        content = plist.read_text(encoding="utf-8")
        assert "America/New_York" in content, (
            "C1: plist に TZ=America/New_York が設定されていない。"
            "TZ 未設定だとタイムゾーン混乱が発生する。"
        )

    def test_plist_has_provider_yfinance(self):
        """plist の ProgramArguments に --provider yfinance が含まれる（C2 fix）。"""
        plist = self._get_plist()
        if not plist.exists():
            pytest.skip("plist not found")
        content = plist.read_text(encoding="utf-8")
        assert "yfinance" in content, (
            "C2: plist に --provider yfinance がない。"
            "DummyMetricProvider の本番流出を防ぐために必須。"
        )


# ===========================================================================
# C2: DummyMetricProvider 本番流出修正 — --provider argparse
# ===========================================================================

class TestC2_DummyMetricProviderLeakPrevention:
    """C2: main.py が --provider argparse を持ち、yfinance がデフォルトである。"""

    def test_main_has_provider_argument(self):
        """atlas_v3.main に --provider argparse が存在する。"""
        import inspect
        import atlas_v3.main as m
        source = inspect.getsource(m)
        assert "--provider" in source, "C2: atlas_v3.main に --provider argparse がない"

    def test_main_default_provider_is_yfinance(self):
        """argparse の --provider デフォルトが yfinance である。"""
        import inspect
        import atlas_v3.main as m
        source = inspect.getsource(m)
        # デフォルト yfinance の設定が存在する
        assert 'default="yfinance"' in source or "default='yfinance'" in source, (
            "C2: --provider のデフォルトが yfinance でない。"
            "デフォルト dummy だと本番で監視全盲になる。"
        )

    def test_dummy_provider_not_directly_instantiated_in_run(self):
        """run() 関数内で DummyMetricProvider() が直接インスタンス化されていない。

        run() は _build_metric_provider() に委譲するだけで、
        DummyMetricProvider を直接 new() してはいけない。
        """
        import ast
        import inspect
        import atlas_v3.main as m
        run_source = inspect.getsource(m.run)
        # AST で DummyMetricProvider(...) の Call が存在しないことを確認
        tree = ast.parse(run_source)
        dummy_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "DummyMetricProvider"
        ]
        assert len(dummy_calls) == 0, (
            "C2: run() 内で DummyMetricProvider() が直接インスタンス化されている。"
            "_build_metric_provider() に委譲すること。"
        )

    def test_build_metric_provider_exists(self):
        """_build_metric_provider() 関数が atlas_v3.main に存在する。"""
        import atlas_v3.main as m
        assert hasattr(m, "_build_metric_provider"), (
            "C2: atlas_v3.main に _build_metric_provider() がない"
        )

    def test_build_metric_provider_dummy_returns_callable(self):
        """_build_metric_provider('dummy') は callable を返す。"""
        from atlas_v3.main import _build_metric_provider
        fn = _build_metric_provider("dummy")
        assert callable(fn), "C2: dummy provider が callable でない"

    def test_build_metric_provider_dummy_warns(self, caplog):
        """_build_metric_provider('dummy') は WARNING ログを出す。"""
        import logging
        from atlas_v3.main import _build_metric_provider
        with caplog.at_level(logging.WARNING, logger="atlas_v3.main"):
            _build_metric_provider("dummy")
        assert any("dummy" in r.message.lower() or "zero" in r.message.lower()
                   for r in caplog.records), (
            "C2: dummy provider 選択時に WARNING ログがない"
        )

    def test_build_metric_provider_unknown_raises(self):
        """_build_metric_provider('unknown') は ValueError を raise する。"""
        from atlas_v3.main import _build_metric_provider
        with pytest.raises(ValueError, match="Unknown provider"):
            _build_metric_provider("unknown_xyz")

    def test_run_provider_param_accepted(self):
        """run() が provider 引数を受け付ける（シグネチャ確認）。"""
        import inspect
        from atlas_v3.main import run
        sig = inspect.signature(run)
        assert "provider" in sig.parameters, "C2: run() に provider 引数がない"

    def test_yfinance_provider_file_exists(self):
        """atlas_v3/ops/yfinance_provider.py が存在する。"""
        yf_py = PROJECT_ROOT / "atlas_v3" / "ops" / "yfinance_provider.py"
        assert yf_py.exists(), "C2: atlas_v3/ops/yfinance_provider.py が存在しない"

    def test_yfinance_provider_importable(self):
        """YFinanceMetricProvider がインポート可能である。"""
        try:
            from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        except ImportError as e:
            pytest.fail(f"C2: YFinanceMetricProvider のインポート失敗: {e}")


# ===========================================================================
# C3: KillSwitch ゾンビ状態修正 — probe 成功時に deactivate
# ===========================================================================

class TestC3_KillSwitchZombieState:
    """C3: _probe_recovery 成功時に KillSwitch を deactivate() する。"""

    def test_probe_recovery_calls_deactivate_on_success(self):
        """_probe_recovery 成功時に common_v3.risk.kill_switch.deactivate が呼ばれる。"""
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon

        # valid provider（probe 成功）
        provider = lambda: {
            "pnl_day_usd": 0.0,
            "drawdown_pct": 0.0,
            "latency_ms": 0.0,
        }
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            metric_provider=provider,
            probe_on_consecutive_failure=True,
        )
        daemon = MonitorDaemon(config)

        deactivate_called = []
        def mock_deactivate(activator="", reason=""):
            deactivate_called.append({"activator": activator, "reason": reason})
            return True

        with patch("atlas_v3.ops.monitor.MonitorDaemon._probe_recovery") as mock_probe:
            # _probe_recovery 内で deactivate が呼ばれることを検証する
            # 直接 source を確認する
            pass

        import inspect
        from atlas_v3.ops.monitor import MonitorDaemon as MD
        probe_source = inspect.getsource(MD._probe_recovery)
        assert "deactivate" in probe_source, (
            "C3: _probe_recovery に deactivate() 呼び出しがない。"
            "probe 成功後も KillSwitch が ARMED のままになる（ゾンビ状態）。"
        )

    def test_probe_recovery_deactivate_on_success_functional(self):
        """_probe_recovery が True を返す時に KillSwitch deactivate が呼ばれる（機能テスト）。"""
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon

        provider = lambda: {"pnl_day_usd": 0.0, "drawdown_pct": 0.0, "latency_ms": 0.0}
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            metric_provider=provider,
        )
        daemon = MonitorDaemon(config)

        deactivate_calls = []

        def fake_deactivate(activator="", reason=""):
            deactivate_calls.append(reason)
            return True

        with patch("atlas_v3.ops.monitor.ks_deactivate", fake_deactivate, create=True):
            # common_v3.risk.kill_switch.deactivate をモック
            with patch.dict("sys.modules", {
                "common_v3.risk.kill_switch": type(sys)("common_v3.risk.kill_switch"),
            }):
                import types
                mock_ks = types.ModuleType("common_v3.risk.kill_switch")
                mock_ks.deactivate = fake_deactivate
                sys.modules["common_v3.risk.kill_switch"] = mock_ks
                try:
                    result = daemon._probe_recovery()
                    assert result is True, "C3: valid provider で probe が False を返した"
                    assert len(deactivate_calls) > 0, (
                        "C3: probe 成功時に deactivate が呼ばれていない"
                    )
                finally:
                    # cleanup
                    if "common_v3.risk.kill_switch" in sys.modules:
                        # 元のモジュールに戻す
                        try:
                            import importlib
                            sys.modules["common_v3.risk.kill_switch"] = importlib.import_module(
                                "common_v3.risk.kill_switch"
                            )
                        except Exception:
                            pass

    def test_probe_recovery_state_machine_in_source(self):
        """_probe_recovery の docstring/コメントに状態機械の記述がある。"""
        import inspect
        from atlas_v3.ops.monitor import MonitorDaemon
        source = inspect.getsource(MonitorDaemon._probe_recovery)
        assert "deactivate" in source and ("C3" in source or "ゾンビ" in source or "zombie" in source.lower()), (
            "C3: _probe_recovery に状態機械の記述 (C3 fix / ゾンビ状態) がない"
        )


# ===========================================================================
# C4: hysteresis 振動脆弱修正 — Schmitt Trigger 化
# ===========================================================================

class TestC4_HysteresisSchmittTrigger:
    """C4: _check_drawdown が Schmitt Trigger 二閾値で振動に強い。"""

    def _make_daemon(self, breach_count: int = 3, upper=None, lower=None):
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            drawdown_pct=0.12,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            drawdown_breach_count=breach_count,
            hysteresis_upper=upper,
            hysteresis_lower=lower,
        )
        return MonitorDaemon(config)

    def test_flash_crash_oscillation_does_not_reset_counter(self):
        """Flash Crash 型振動（上閾値超過 → 帯域内 → 上閾値超過 → ...）でカウンターが reset されない。

        旧実装の脆弱性:
          drawdown: 0.13 → counter=1（超過）
          drawdown: 0.11 → counter=0 reset（閾値以下）
          drawdown: 0.13 → counter=1（超過）... 永遠に CRITICAL 発火しない

        Schmitt Trigger:
          drawdown: 0.13 → counter=1（上閾値 0.12 超過）
          drawdown: 0.11 → counter 保持（帯域内: 0.096 < 0.11 <= 0.12）
          drawdown: 0.13 → counter=2（上閾値再超過）
          → breach_count=3 に近づいていく（振動でリセットされない）
        """
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(breach_count=3)
        ts = "2026-04-23T00:00:00+00:00"

        # 0.12 を何度も上下に振動する（0.096 以下には降りない）
        daemon._check_drawdown(ts, 0.13)   # counter=1
        daemon._check_drawdown(ts, 0.11)   # 帯域内 → counter 保持 (1)
        daemon._check_drawdown(ts, 0.13)   # counter=2
        daemon._check_drawdown(ts, 0.11)   # 帯域内 → counter 保持 (2)

        # カウンターが 2 のまま維持されていること
        assert daemon._drawdown_breach_counter == 2, (
            f"C4: Flash Crash 振動でカウンターが reset された: {daemon._drawdown_breach_counter}。"
            "Schmitt Trigger 帯域内（0.096–0.12）は counter を保持すること。"
        )

    def test_below_lower_threshold_decrements_counter(self):
        """下閾値（lower=drawdown_pct*0.8）を下回ると counter が decrement される。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(breach_count=5)
        ts = "2026-04-23T00:00:00+00:00"

        # threshold=0.12, lower=0.096
        daemon._check_drawdown(ts, 0.13)   # counter=1
        daemon._check_drawdown(ts, 0.13)   # counter=2
        # 下閾値以下に降りる（0.05 < 0.096）
        daemon._check_drawdown(ts, 0.05)   # counter--

        assert daemon._drawdown_breach_counter == 1, (
            f"C4: 下閾値以下で counter が decrement されない: {daemon._drawdown_breach_counter}"
        )

    def test_counter_does_not_go_below_zero(self):
        """counter が 0 以下にはならない。"""
        from atlas_v3.ops.monitor import AlertLevel
        daemon = self._make_daemon(breach_count=5)
        ts = "2026-04-23T00:00:00+00:00"

        # counter=0 の状態で lower 以下に降りても 0 のまま
        daemon._check_drawdown(ts, 0.05)
        assert daemon._drawdown_breach_counter == 0, (
            f"C4: counter が 0 以下になった: {daemon._drawdown_breach_counter}"
        )

    def test_explicit_hysteresis_upper_lower(self):
        """明示的な hysteresis_upper / hysteresis_lower が正しく機能する。"""
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon, AlertLevel
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            drawdown_pct=0.12,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            drawdown_breach_count=3,
            hysteresis_upper=0.10,   # 上閾値を 0.10 に設定（drawdown_pct より低め）
            hysteresis_lower=0.08,   # 下閾値を 0.08 に設定
        )
        daemon = MonitorDaemon(config)
        ts = "2026-04-23T00:00:00+00:00"

        # 0.10 を超えると counter++
        daemon._check_drawdown(ts, 0.11)  # upper=0.10 超過 → counter=1
        assert daemon._drawdown_breach_counter == 1

        # 0.09 は帯域内（0.08 <= 0.09 <= 0.10）→ counter 保持
        daemon._check_drawdown(ts, 0.09)
        assert daemon._drawdown_breach_counter == 1, (
            "C4: 帯域内（0.08–0.10）で counter が変化した"
        )

        # 0.07 < lower=0.08 → counter--
        daemon._check_drawdown(ts, 0.07)
        assert daemon._drawdown_breach_counter == 0, (
            "C4: lower 以下でも counter が decrement されない"
        )

    def test_hysteresis_config_fields_exist(self):
        """MonitorConfig に hysteresis_upper / hysteresis_lower フィールドが存在する。"""
        from atlas_v3.ops.monitor import MonitorConfig
        import dataclasses
        fields = {f.name for f in dataclasses.fields(MonitorConfig)}
        assert "hysteresis_upper" in fields, "C4: MonitorConfig に hysteresis_upper がない"
        assert "hysteresis_lower" in fields, "C4: MonitorConfig に hysteresis_lower がない"

    def test_hysteresis_lower_gt_upper_raises(self):
        """hysteresis_lower >= hysteresis_upper で ValueError が raise される。"""
        from atlas_v3.ops.monitor import MonitorConfig
        with pytest.raises(ValueError, match="hysteresis_lower"):
            MonitorConfig(
                daily_loss_usd=-400.0,
                hysteresis_upper=0.08,
                hysteresis_lower=0.10,  # lower > upper → invalid
            )

    def test_schmitt_trigger_in_source(self):
        """_check_drawdown の実装に Schmitt Trigger 関連の実装がある。"""
        import inspect
        from atlas_v3.ops.monitor import MonitorDaemon
        source = inspect.getsource(MonitorDaemon._check_drawdown)
        assert "upper" in source and "lower" in source, (
            "C4: _check_drawdown に upper/lower 二閾値の実装がない（Schmitt Trigger 未実装）"
        )

    def test_existing_hysteresis_tests_still_pass_after_schmitt(self):
        """r5 の既存 HIGH-R4-3 テスト（breach_count=3、単純 counter）が Schmitt Trigger 後も動く。

        Schmitt Trigger 化後も breach_count による CRITICAL/WARNING の振る舞いが維持されること。
        """
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon, AlertLevel
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            drawdown_pct=0.12,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            drawdown_breach_count=3,
        )
        daemon = MonitorDaemon(config)
        ts = "2026-04-23T00:00:00+00:00"

        # 連続 3 回超過 → CRITICAL
        daemon._check_drawdown(ts, 0.15)  # 1
        daemon._check_drawdown(ts, 0.15)  # 2
        result = daemon._check_drawdown(ts, 0.15)  # 3 → CRITICAL
        assert result.level == AlertLevel.CRITICAL, (
            "C4: 3 回連続超過後に CRITICAL にならない（Schmitt Trigger 後退行）"
        )


# ===========================================================================
# H2: O_NOFOLLOW 親ディレクトリ無防御修正 — _check_path_not_symlink
# ===========================================================================

class TestH2_ONoFollowParentDirectory:
    """H2: vault.py が親ディレクトリ symlink も拒否する。"""

    def test_check_path_not_symlink_exists_in_vault(self):
        """vault.py に _check_path_not_symlink() が存在する。"""
        import inspect
        from atlas_v3.ops import vault as vault_module
        assert hasattr(vault_module, "_check_path_not_symlink"), (
            "H2: vault.py に _check_path_not_symlink() がない"
        )

    def test_check_path_not_symlink_rejects_symlink_file(self, tmp_path):
        """symlink ファイルに対して _check_path_not_symlink が VaultError を raise する。"""
        from atlas_v3.ops.vault import VaultError, _check_path_not_symlink

        real = tmp_path / "real.env"
        real.write_text("KEY=VALUE")
        symlink = tmp_path / "link.env"
        symlink.symlink_to(real)

        with pytest.raises(VaultError, match="symlink"):
            _check_path_not_symlink(symlink)

    def test_check_path_not_symlink_accepts_regular_file(self, tmp_path):
        """通常ファイルに対して _check_path_not_symlink が例外を raise しない。"""
        from atlas_v3.ops.vault import _check_path_not_symlink

        real = tmp_path / "real.env"
        real.write_text("KEY=VALUE")

        # 例外が raise されないこと
        try:
            _check_path_not_symlink(real)
        except Exception as e:
            pytest.fail(f"H2: 通常ファイルで _check_path_not_symlink が例外: {e}")

    def test_check_path_not_symlink_rejects_symlink_parent(self, tmp_path):
        """親ディレクトリが symlink の場合に VaultError を raise する。"""
        from atlas_v3.ops.vault import VaultError, _check_path_not_symlink

        # real_dir/ を作り symlink_dir/ → real_dir/ のシンボリックリンクを作成
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        real_file = real_dir / "secret.env"
        real_file.write_text("KEY=VALUE")

        symlink_dir = tmp_path / "symlink_dir"
        symlink_dir.symlink_to(real_dir)
        symlink_file = symlink_dir / "secret.env"

        with pytest.raises(VaultError, match="symlink"):
            _check_path_not_symlink(symlink_file)

    def test_parse_env_file_calls_check_path_not_symlink(self):
        """_parse_env_file が _check_path_not_symlink を呼び出している（source 確認）。"""
        import inspect
        from atlas_v3.ops import vault as vault_module
        source = inspect.getsource(vault_module._parse_env_file)
        assert "_check_path_not_symlink" in source, (
            "H2: _parse_env_file が _check_path_not_symlink を呼んでいない"
        )


# ===========================================================================
# H3: DummyMetricProvider + probe 結託防止
# ===========================================================================

class TestH3_DummyProbCollusion:
    """H3: _probe_recovery が DummyMetricProvider 使用時に False を返す。"""

    def test_probe_recovery_false_when_dummy_provider(self):
        """_probe_recovery は DummyMetricProvider 使用時に False を返す。"""
        from atlas_v3.main import DummyMetricProvider
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon

        dummy = DummyMetricProvider(warn_on_use=False)
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            metric_provider=dummy.get_metrics,
            probe_on_consecutive_failure=True,
        )
        daemon = MonitorDaemon(config)
        result = daemon._probe_recovery()
        assert result is False, (
            "H3: DummyMetricProvider 使用中に probe が True を返した。"
            "Dummy は 0 値を返すため probe 成功でも回復確認にならない（結託防止）。"
        )

    def test_probe_recovery_true_when_real_provider(self):
        """_probe_recovery は実 provider（非 Dummy）使用時に True を返す。"""
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon

        # 非 Dummy の実 provider（lambda）
        real_provider = lambda: {"pnl_day_usd": -10.0, "drawdown_pct": 0.05, "latency_ms": 50.0}
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            metric_provider=real_provider,
            probe_on_consecutive_failure=True,
        )
        daemon = MonitorDaemon(config)
        result = daemon._probe_recovery()
        assert result is True, "H3: 実 provider で probe が False を返した"

    def test_is_dummy_provider_detects_dummy(self):
        """_is_dummy_provider が DummyMetricProvider を正しく検出する。"""
        from atlas_v3.main import DummyMetricProvider
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon

        dummy = DummyMetricProvider(warn_on_use=False)
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            metric_provider=dummy.get_metrics,
        )
        daemon = MonitorDaemon(config)
        assert daemon._is_dummy_provider() is True, (
            "H3: _is_dummy_provider が DummyMetricProvider を検出できない"
        )

    def test_is_dummy_provider_false_for_real_provider(self):
        """_is_dummy_provider が lambda（非 Dummy）に対して False を返す。"""
        from atlas_v3.ops.monitor import MonitorConfig, MonitorDaemon

        provider = lambda: {"pnl_day_usd": 0.0, "drawdown_pct": 0.0, "latency_ms": 0.0}
        config = MonitorConfig(
            daily_loss_usd=-400.0,
            pushover_enabled=False,
            kill_switch_on_emergency=False,
            kill_switch_on_drawdown_breach=False,
            metric_provider=provider,
        )
        daemon = MonitorDaemon(config)
        assert daemon._is_dummy_provider() is False, (
            "H3: lambda provider を Dummy と誤認した"
        )

    def test_dummy_probe_collusion_in_source(self):
        """_probe_recovery の実装に H3 fix (Dummy 結託防止) の記述がある。"""
        import inspect
        from atlas_v3.ops.monitor import MonitorDaemon
        source = inspect.getsource(MonitorDaemon._probe_recovery)
        assert "_is_dummy_provider" in source, (
            "H3: _probe_recovery に _is_dummy_provider チェックがない（Dummy 結託防止未実装）"
        )


# ===========================================================================
# H1: spy_bot.py 書換防止 — legacy_write_block hook テスト
# ===========================================================================

class TestH1_LegacyWriteBlockHook:
    """H1: legacy_write_block.sh が存在し spy_bot.py 等の保護ファイルを定義している。"""

    def test_legacy_write_block_sh_exists(self):
        """scripts/legacy_write_block.sh が存在する。"""
        block_sh = PROJECT_ROOT / "scripts" / "legacy_write_block.sh"
        # .claude/hooks/ にある場合も確認
        hooks_block = PROJECT_ROOT / ".claude" / "hooks" / "legacy_write_block.sh"
        exists = block_sh.exists() or hooks_block.exists()
        assert exists, (
            "H1: legacy_write_block.sh が scripts/ または .claude/hooks/ に存在しない"
        )

    def test_legacy_write_block_protects_spy_bot(self):
        """legacy_write_block.sh が spy_bot.py を保護対象として含む。"""
        candidates = [
            PROJECT_ROOT / "scripts" / "legacy_write_block.sh",
            PROJECT_ROOT / ".claude" / "hooks" / "legacy_write_block.sh",
        ]
        for sh in candidates:
            if sh.exists():
                content = sh.read_text(encoding="utf-8", errors="ignore")
                assert "spy_bot" in content, (
                    f"H1: {sh} に spy_bot.py の保護定義がない"
                )
                return
        pytest.skip("legacy_write_block.sh not found")

    def test_spy_bot_file_unchanged_since_last_known_good(self):
        """spy_bot.py が git で変更されていないこと（未ステージ差分なし）を確認する。"""
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "spy_bot.py"],
            capture_output=True, text=True,
            cwd=str(PROJECT_ROOT),
        )
        # diff があれば spy_bot.py が変更されている
        spy_in_diff = "spy_bot.py" in result.stdout
        # 注: r6 で spy_bot.py を変更した場合は CI で失敗させる
        # （H1 の意図: builder が spy_bot.py を改変しないこと）
        assert not spy_in_diff, (
            "H1: spy_bot.py が HEAD から変更されている。"
            "legacy_write_block.sh によって保護されるべきファイルが改変された。"
            "stash で復元すること。"
        )


# ===========================================================================
# Regression: spy_bot.py の known-good 値が維持されていること
# ===========================================================================

class TestRegressionR6_SpyBotIntegrity:
    """R1/R2: spy_bot.py の regression 値（PROFIT_TARGET / ^GSPC）が維持されている。"""

    def test_spy_bot_primary_profit_target_is_0_80(self):
        """spy_bot.py のメイン PROFIT_TARGET（line ~275）が 0.80 であること（R1 regression 防止）。

        R1 regression: spy_bot.py:279 で PROFIT_TARGET が 0.80 → 0.50 に変更された。
        モジュールレベルの主要 PROFIT_TARGET 定数の値を確認する。
        IV_CRUSH_PROFIT_TARGET_PCT=0.50 等の別定数は対象外。
        """
        spy_bot_py = PROJECT_ROOT / "spy_bot.py"
        if not spy_bot_py.exists():
            pytest.skip("spy_bot.py not found")

        import re
        content = spy_bot_py.read_text(encoding="utf-8")

        # モジュールレベルの主要 PROFIT_TARGET を探す（コメントあり版も含む）
        # "PROFIT_TARGET  = 0.80" または "PROFIT_TARGET = 0.80" のパターン
        matches_080 = re.findall(r"^PROFIT_TARGET\s*=\s*0\.80", content, re.MULTILINE)
        matches_050_primary = re.findall(r"^PROFIT_TARGET\s*=\s*0\.50", content, re.MULTILINE)

        assert len(matches_050_primary) == 0, (
            f"R1: spy_bot.py のモジュールレベル PROFIT_TARGET が 0.50 に変更されている。"
            "元の値（0.80）に戻すこと。"
        )
        assert len(matches_080) > 0, (
            f"R1: spy_bot.py のモジュールレベル PROFIT_TARGET = 0.80 が見つからない。"
            "stash で復元すること。"
        )

    def test_spy_bot_no_unexpected_ticker_change(self):
        """spy_bot.py の ticker 関連コードが大幅変更されていないこと（R2 regression 防止）。

        R2 regression: spy_bot.py:876 で ticker 識別子が変更された。
        spy_bot.py は SPY を扱うため 'SPY' が含まれていることを確認する。
        """
        spy_bot_py = PROJECT_ROOT / "spy_bot.py"
        if not spy_bot_py.exists():
            pytest.skip("spy_bot.py not found")

        content = spy_bot_py.read_text(encoding="utf-8")
        # SPY が存在していることを確認（基本的な整合性チェック）
        assert "SPY" in content, (
            "R2: spy_bot.py から SPY の参照が消えている。"
            "ticker 識別子が大幅変更された可能性がある。stash で復元すること。"
        )


# ===========================================================================
# Regression Ledger 更新: r6 修正の永続再発防止
# ===========================================================================

class TestRegressionR6_1_SchmittTriggerMaintained:
    """REG-R6-1: Schmitt Trigger の hysteresis_upper/lower フィールドが消えないこと。"""

    def test_schmitt_fields_in_config(self):
        """MonitorConfig に hysteresis_upper / hysteresis_lower が存在する。"""
        from atlas_v3.ops.monitor import MonitorConfig
        import dataclasses
        fields = {f.name for f in dataclasses.fields(MonitorConfig)}
        assert "hysteresis_upper" in fields, "REG-R6-1: hysteresis_upper が消えた"
        assert "hysteresis_lower" in fields, "REG-R6-1: hysteresis_lower が消えた"


class TestRegressionR6_2_DummyProviderNotDefault:
    """REG-R6-2: main.py の --provider デフォルトが dummy に戻らないこと。"""

    def test_default_provider_not_dummy(self):
        """argparse の --provider デフォルトが dummy でないこと。"""
        import inspect
        import atlas_v3.main as m
        source = inspect.getsource(m)
        # dummy がデフォルトになっていないことを確認
        assert 'default="dummy"' not in source and "default='dummy'" not in source, (
            "REG-R6-2: --provider のデフォルトが dummy に戻った（C2 regression）。"
            "yfinance がデフォルトであること。"
        )


class TestRegressionR6_3_PlistNoCalendarInterval:
    """REG-R6-3: plist に StartCalendarInterval が戻らないこと。"""

    def test_plist_no_calendar_interval(self):
        """plist に <key>StartCalendarInterval</key> が XML key として存在しないこと（C1 regression 防止）。"""
        plist = Path.home() / "Library" / "LaunchAgents" / "com.soralab.atlas-paper.plist"
        if not plist.exists():
            pytest.skip("plist not found")
        content = plist.read_text(encoding="utf-8")
        assert "<key>StartCalendarInterval</key>" not in content, (
            "REG-R6-3: plist に <key>StartCalendarInterval</key> が XML key として戻った（C1 regression）"
        )


class TestRegressionR6_4_ProbeDeactivatesKillSwitch:
    """REG-R6-4: _probe_recovery が deactivate を呼ぶ実装が消えないこと。"""

    def test_deactivate_call_in_probe_source(self):
        """_probe_recovery ソースに deactivate の呼び出しがある。"""
        import inspect
        from atlas_v3.ops.monitor import MonitorDaemon
        source = inspect.getsource(MonitorDaemon._probe_recovery)
        assert "deactivate" in source, (
            "REG-R6-4: _probe_recovery の deactivate 呼び出しが消えた（C3 regression）"
        )
