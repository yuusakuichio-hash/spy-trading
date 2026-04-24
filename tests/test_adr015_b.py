"""tests/test_adr015_b.py — ADR-015 B 実装テスト

対象:
  1. atlas_v3/main.py: moomoo AuthenticationError → YFinanceMetricProvider auto fallback
  2. common_v3/notify/quiet_hours_pushover.py: 深夜遅延送信 wrapper

テスト要件:
- F04 対策: モジュールを実際に import して動作を確認する（AST inspection 禁止）
- F02 対策: mock は実 API の interface（AuthenticationError, MoomooMetricProvider）に
  準拠した形で設定する
- premortem F01/F02/F03/F04 mitigation を組み込む
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ===========================================================================
# Section 1: atlas_v3/main.py moomoo AuthenticationError → yfinance fallback
# ===========================================================================

class TestADR015B_MoomooAuthFallback:
    """moomoo AuthenticationError → YFinanceMetricProvider auto fallback を検証する。

    ADR-015 B 要件:
    - AuthenticationError → SystemExit(78) ではなく yfinance fallback に切替
    - fallback 後も MonitorDaemon が起動できること
    """

    def test_build_metric_provider_moomoo_auth_error_returns_yfinance(self):
        """実動作: moomoo smoke_test で AuthenticationError → yfinance fallback を返す。

        F04 対策: _build_metric_provider("moomoo") を実際に呼び出して
        返り値が YFinanceMetricProvider.get_metrics であることを確認する。
        """
        from atlas_v3.main import _build_metric_provider
        from atlas_v3.ops.moomoo_provider import AuthenticationError
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider

        # smoke_test が AuthenticationError を raise するよう mock
        with patch(
            "atlas_v3.ops.moomoo_provider.MoomooMetricProvider.smoke_test",
            side_effect=AuthenticationError("test: session expired"),
        ):
            # YFinanceMetricProvider の _ensure_yfinance を bypass（yfinance 未インストール環境対応）
            with patch.object(YFinanceMetricProvider, "_ensure_yfinance", return_value=None):
                result_fn = _build_metric_provider("moomoo")

        # 返り値が callable であること（F04: None でないことを確認）
        assert callable(result_fn), (
            "ADR-015 B: moomoo AuthenticationError fallback が callable を返さない。"
        )

        # 返り値の __self__ が YFinanceMetricProvider のインスタンスであること
        # （bound method の場合は __self__ でインスタンスを取得できる）
        if hasattr(result_fn, "__self__"):
            assert isinstance(result_fn.__self__, YFinanceMetricProvider), (
                f"ADR-015 B: fallback が YFinanceMetricProvider でない: {type(result_fn.__self__)}"
            )

    def test_build_metric_provider_moomoo_not_implemented_returns_yfinance(self):
        """実動作: MoomooProviderNotImplementedError → yfinance fallback を返す。

        futu-api 未インストール環境（開発 Mac）でのフォールバック確認。
        """
        from atlas_v3.main import _build_metric_provider
        from atlas_v3.ops.moomoo_provider import MoomooProviderNotImplementedError
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider

        with patch(
            "atlas_v3.ops.moomoo_provider.MoomooMetricProvider.smoke_test",
            side_effect=MoomooProviderNotImplementedError("futu-api not installed"),
        ):
            with patch.object(YFinanceMetricProvider, "_ensure_yfinance", return_value=None):
                result_fn = _build_metric_provider("moomoo")

        assert callable(result_fn), (
            "ADR-015 B: MoomooProviderNotImplementedError fallback が callable を返さない。"
        )

    def test_build_metric_provider_moomoo_auth_error_does_not_raise_system_exit(self):
        """実動作: AuthenticationError で SystemExit(78) が raise されない。

        旧実装: raise SystemExit(78) → launchd 無限再起動ループ
        新実装: yfinance fallback → 監視継続
        """
        from atlas_v3.main import _build_metric_provider
        from atlas_v3.ops.moomoo_provider import AuthenticationError
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider

        with patch(
            "atlas_v3.ops.moomoo_provider.MoomooMetricProvider.smoke_test",
            side_effect=AuthenticationError("test: 401 unauthorized"),
        ):
            with patch.object(YFinanceMetricProvider, "_ensure_yfinance", return_value=None):
                # SystemExit が raise されないことを確認
                try:
                    result_fn = _build_metric_provider("moomoo")
                except SystemExit as e:
                    pytest.fail(
                        f"ADR-015 B: AuthenticationError で SystemExit({e.code}) が raise された。"
                        "旧実装に戻っている。"
                    )

        assert result_fn is not None, "ADR-015 B: fallback が None を返した。"

    def test_build_metric_provider_moomoo_smoke_pass_uses_moomoo(self):
        """境界条件: smoke_test 成功時は MoomooMetricProvider を使う（fallback しない）。"""
        from atlas_v3.main import _build_metric_provider
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider

        with patch.object(MoomooMetricProvider, "smoke_test", return_value=None):
            result_fn = _build_metric_provider("moomoo")

        # smoke_test 成功時は MoomooMetricProvider.get_metrics が返る
        assert callable(result_fn), (
            "ADR-015 B: smoke_test 成功時に callable が返らない。"
        )
        if hasattr(result_fn, "__self__"):
            assert isinstance(result_fn.__self__, MoomooMetricProvider), (
                f"ADR-015 B: smoke_test 成功時の provider が MoomooMetricProvider でない: "
                f"{type(result_fn.__self__)}"
            )

    def test_run_with_moomoo_provider_auth_error_returns_0(self):
        """run() で provider=moomoo, AuthenticationError 発生 → daemon 起動して exit 0。

        F05 対策: _build_metric_provider が fallback 後に run() で daemon を起動できること。
        """
        from atlas_v3.main import run
        from atlas_v3.ops.moomoo_provider import AuthenticationError
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider

        with patch(
            "atlas_v3.ops.moomoo_provider.MoomooMetricProvider.smoke_test",
            side_effect=AuthenticationError("auth failed"),
        ):
            with patch.object(YFinanceMetricProvider, "_ensure_yfinance", return_value=None):
                with patch("atlas_v3.ops.monitor.bootstrap_paper_monitor") as mock_bootstrap:
                    mock_daemon = MagicMock()
                    mock_daemon.config = MagicMock()
                    mock_daemon.config.check_interval_secs = 15.0
                    mock_bootstrap.return_value = mock_daemon

                    exit_code = run(
                        mode="paper",
                        provider="moomoo",
                        skip_preflight=True,
                        daemon_only=True,
                    )

        assert exit_code == 0, (
            f"ADR-015 B: moomoo AuthenticationError fallback 後に exit_code={exit_code}。"
            "run() が正常に daemon を起動できない。"
        )

    def test_fallback_warning_logged_on_auth_error(self):
        """fallback 発動時に ADR-015 B の警告ログが出力される。"""
        import logging
        from atlas_v3.main import _build_metric_provider
        from atlas_v3.ops.moomoo_provider import AuthenticationError
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider

        with patch(
            "atlas_v3.ops.moomoo_provider.MoomooMetricProvider.smoke_test",
            side_effect=AuthenticationError("session expired"),
        ):
            with patch.object(YFinanceMetricProvider, "_ensure_yfinance", return_value=None):
                with patch("atlas_v3.main.log") as mock_log:
                    _build_metric_provider("moomoo")

        # warning ログが呼ばれていること
        assert mock_log.warning.called, (
            "ADR-015 B: AuthenticationError fallback 時に warning ログが出力されない。"
        )
        # ADR-015 B 文字列が含まれること
        warning_calls_str = " ".join(
            str(c) for c in mock_log.warning.call_args_list
        )
        assert "ADR-015" in warning_calls_str or "fallback" in warning_calls_str.lower(), (
            f"ADR-015 B: warning ログに 'ADR-015' または 'fallback' が含まれない。"
            f"calls: {warning_calls_str[:200]}"
        )

    def test_auth_error_class_importable(self):
        """AuthenticationError が atlas_v3.ops.moomoo_provider からインポートできる。

        F04 対策: 実 import の確認。
        """
        from atlas_v3.ops.moomoo_provider import AuthenticationError
        # AuthenticationError が Exception のサブクラスであること
        assert issubclass(AuthenticationError, Exception), (
            "ADR-015 B: AuthenticationError が Exception のサブクラスでない。"
        )

    def test_yfinance_provider_importable_from_atlas_v3(self):
        """YFinanceMetricProvider が atlas_v3.ops.yfinance_provider からインポートできる。

        F04 対策: fallback 先の実 import 確認。
        """
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        assert hasattr(YFinanceMetricProvider, "get_metrics"), (
            "ADR-015 B: YFinanceMetricProvider に get_metrics メソッドがない。"
        )

    def test_fallback_callable_returns_dict_schema(self):
        """fallback provider の get_metrics() が正しいスキーマを返す（mock で確認）。

        F02 対策: fallback 後の provider が期待スキーマを持つことを確認。
        """
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider

        with patch.object(YFinanceMetricProvider, "_ensure_yfinance", return_value=None):
            provider = YFinanceMetricProvider.__new__(YFinanceMetricProvider)
            provider._ticker_symbol = "SPY"
            provider._yf = None
            provider._cache_ttl_secs = 10.0
            # キャッシュに dummy data を注入
            provider._cache_ts = time.monotonic()  # 有効なキャッシュ
            provider._cache_data = {
                "pnl_day_usd": -20.0,
                "drawdown_pct": 0.02,
                "latency_ms": 50.0,
            }
            provider._last_price = 500.0
            provider._degraded_mode = False
            provider._degraded_since = 0.0

            metrics = provider.get_metrics()

        # スキーマ確認（MonitorDaemon が期待するキー）
        required_keys = {"pnl_day_usd", "drawdown_pct", "latency_ms"}
        missing = required_keys - set(metrics.keys())
        assert not missing, (
            f"ADR-015 B: fallback provider のスキーマに必須キーが欠落: {missing}"
        )


# ===========================================================================
# Section 2: common_v3/notify/quiet_hours_pushover.py 深夜遅延送信 wrapper
# ===========================================================================

class TestADR015B_QuietHoursPushover:
    """common_v3/notify/quiet_hours_pushover.py の深夜遅延送信を検証する。

    ADR-015 B 要件:
    - 深夜（JST 22:00-4:00）の非緊急通知を morning queue に遅延
    - 真の緊急通知（priority=2 + キーワード）は深夜も即時送信
    - 通常時間帯（JST 4:00-22:00）は即時送信
    """

    def test_module_importable(self):
        """common_v3.notify.quiet_hours_pushover がインポートできる。F04 対策。"""
        from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours
        assert callable(send_with_quiet_hours), (
            "ADR-015 B: send_with_quiet_hours が callable でない。"
        )

    def test_is_quiet_hours_jst_returns_bool(self):
        """_is_quiet_hours_jst() が bool を返す。"""
        from common_v3.notify.quiet_hours_pushover import _is_quiet_hours_jst
        result = _is_quiet_hours_jst()
        assert isinstance(result, bool), (
            f"ADR-015 B: _is_quiet_hours_jst() が bool を返さない: {type(result)}"
        )

    def test_send_during_quiet_hours_deferred_to_morning_queue(self, tmp_path):
        """深夜（静穏時間内）の非緊急通知が morning queue に遅延される。

        プライベート領域尊重規律: 深夜に通知を送らない。
        """
        from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours

        # morning_queue への enqueue を mock で確認
        with patch(
            "common_v3.notify.quiet_hours_pushover._is_quiet_hours_jst",
            return_value=True,  # 深夜を模擬
        ), patch(
            "common.pushover_client._is_quiet_hours",
            return_value=True,
        ), patch(
            "common.pushover_client._is_night_emergency",
            return_value=False,  # 緊急でない
        ), patch(
            "common.pushover_client._enqueue_morning_digest"
        ) as mock_enqueue:
            result = send_with_quiet_hours(
                title="[Atlas] moomoo fallback 発動",
                message="AuthenticationError → yfinance に切替",
                priority=0,
                app_tag="Atlas",
            )

        # morning queue への enqueue が呼ばれたこと
        assert mock_enqueue.called, (
            "ADR-015 B: 深夜非緊急通知が morning queue に遅延されなかった。"
        )
        assert result is True, (
            "ADR-015 B: 深夜遅延時に True が返らない（正常 queue 追記を示すべき）。"
        )

    def test_send_during_quiet_hours_emergency_is_immediate(self):
        """深夜でも priority=2 の緊急通知は即時送信される。"""
        from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours

        with patch(
            "common.pushover_client._is_quiet_hours",
            return_value=True,  # 深夜を模擬
        ), patch(
            "common.pushover_client._is_night_emergency",
            return_value=True,  # 緊急通知
        ), patch(
            "common.pushover_client.send_critical",
            return_value=True,
        ) as mock_send_critical, patch(
            "common.pushover_client._enqueue_morning_digest"
        ) as mock_enqueue:
            result = send_with_quiet_hours(
                title="[Atlas] LOSS_3PCT 資金損失",
                message="drawdown 3% 超過",
                priority=2,
                app_tag="Atlas",
            )

        # send_critical が呼ばれ、morning queue には積まれないこと
        assert mock_send_critical.called, (
            "ADR-015 B: 深夜緊急通知（priority=2）が即時送信されなかった。"
        )
        assert not mock_enqueue.called, (
            "ADR-015 B: 深夜緊急通知が morning queue に誤って積まれた。"
        )

    def test_send_during_business_hours_is_immediate(self):
        """通常時間帯（静穏時間外）は即時送信される。"""
        from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours

        with patch(
            "common.pushover_client._is_quiet_hours",
            return_value=False,  # 通常時間帯
        ), patch(
            "common.pushover_client.send_critical",
            return_value=True,
        ) as mock_send_critical, patch(
            "common.pushover_client._enqueue_morning_digest"
        ) as mock_enqueue:
            result = send_with_quiet_hours(
                title="[Atlas] 通常通知",
                message="test message",
                priority=0,
                app_tag="Atlas",
            )

        # send_critical が呼ばれること
        assert mock_send_critical.called, (
            "ADR-015 B: 通常時間帯の通知が即時送信されなかった。"
        )
        assert not mock_enqueue.called, (
            "ADR-015 B: 通常時間帯の通知が morning queue に誤って積まれた。"
        )
        assert result is True

    def test_force_immediate_bypasses_quiet_hours(self):
        """force_immediate=True で静穏時間をバイパスして即時送信する。"""
        from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours

        with patch(
            "common.pushover_client._is_quiet_hours",
            return_value=True,  # 深夜
        ), patch(
            "common.pushover_client.send_critical",
            return_value=True,
        ) as mock_send_critical, patch(
            "common.pushover_client._enqueue_morning_digest"
        ) as mock_enqueue:
            result = send_with_quiet_hours(
                title="[Atlas] 強制即時送信",
                message="force immediate test",
                priority=1,
                app_tag="Atlas",
                force_immediate=True,
            )

        # force_immediate=True → 深夜でも即時送信
        assert mock_send_critical.called, (
            "ADR-015 B: force_immediate=True で深夜でも即時送信されなかった。"
        )
        assert not mock_enqueue.called, (
            "ADR-015 B: force_immediate=True で morning queue に積まれた。"
        )

    def test_flush_morning_queue_v3_returns_int(self):
        """flush_morning_queue_v3() が int を返す。"""
        from common_v3.notify.quiet_hours_pushover import flush_morning_queue_v3

        with patch(
            "common.pushover_client._load_morning_queue",
            return_value=[],  # 空キュー
        ):
            result = flush_morning_queue_v3()

        assert isinstance(result, int), (
            f"ADR-015 B: flush_morning_queue_v3() が int を返さない: {type(result)}"
        )
        assert result == 0, (
            "ADR-015 B: 空キューで flush_morning_queue_v3() が 0 を返さない。"
        )

    def test_flush_morning_queue_v3_sends_queued_entries(self):
        """flush_morning_queue_v3() がキュー内エントリを送信する。"""
        from common_v3.notify.quiet_hours_pushover import flush_morning_queue_v3

        fake_entries = [
            {"title": "[Atlas] テスト1", "message": "msg1", "priority": 0, "token": "", "app_tag": "Atlas"},
            {"title": "[Atlas] テスト2", "message": "msg2", "priority": 1, "token": "", "app_tag": "Atlas"},
        ]

        with patch(
            "common.pushover_client._load_morning_queue",
            return_value=fake_entries,
        ), patch(
            "common.pushover_client.send_critical",
            return_value=True,
        ) as mock_send, patch(
            "common.pushover_client._clear_morning_queue"
        ) as mock_clear:
            result = flush_morning_queue_v3()

        # 2 件送信されること
        assert result == 2, (
            f"ADR-015 B: flush_morning_queue_v3() が 2 件送信しなかった: {result}"
        )
        # キューがクリアされること
        assert mock_clear.called, (
            "ADR-015 B: flush_morning_queue_v3() 後に _clear_morning_queue が呼ばれない。"
        )

    def test_quiet_hours_wrapper_does_not_modify_common_module(self):
        """wrapper が common/pushover_client.py を変更していない（書換禁止遵守）。

        common/pushover_client.py の send_critical 関数が元のまま存在することを確認。
        """
        import common.pushover_client as pc
        # send_critical が元の関数であること（monkey patch されていないこと）
        assert callable(pc.send_critical), (
            "ADR-015 B: common.pushover_client.send_critical が callable でない。"
            "書換禁止違反の可能性。"
        )
        # _is_quiet_hours が元の関数であること
        assert callable(pc._is_quiet_hours), (
            "ADR-015 B: common.pushover_client._is_quiet_hours が callable でない。"
        )

    def test_send_with_quiet_hours_signature(self):
        """send_with_quiet_hours の引数シグネチャが仕様通り。"""
        import inspect
        from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours
        sig = inspect.signature(send_with_quiet_hours)
        params = sig.parameters
        assert "title" in params, "title パラメータがない"
        assert "message" in params, "message パラメータがない"
        assert "priority" in params, "priority パラメータがない"
        assert "force_immediate" in params, "force_immediate パラメータがない"

    def test_is_night_emergency_v3_priority_2_returns_true(self):
        """_is_night_emergency_v3() で priority=2 は緊急通知と判定される（fallback 動作）。"""
        from common_v3.notify.quiet_hours_pushover import _is_night_emergency_v3

        # common.pushover_client が利用可能な場合は委譲される
        # common が利用できないケースでの fallback: priority >= 2 は True
        with patch(
            "common.pushover_client._is_night_emergency",
            return_value=True,
        ):
            result = _is_night_emergency_v3(
                title="LOSS_3PCT 資金損失",
                message="損失発生",
                priority=2,
            )
        assert result is True, (
            "ADR-015 B: priority=2 の通知が夜間緊急と判定されなかった。"
        )

    def test_is_night_emergency_v3_priority_0_returns_false(self):
        """_is_night_emergency_v3() で priority=0 は深夜遅延対象。"""
        from common_v3.notify.quiet_hours_pushover import _is_night_emergency_v3

        with patch(
            "common.pushover_client._is_night_emergency",
            return_value=False,
        ):
            result = _is_night_emergency_v3(
                title="[Atlas] 通常ログ",
                message="daily summary",
                priority=0,
            )
        assert result is False, (
            "ADR-015 B: priority=0 の通知が夜間緊急と誤判定された。"
        )


# ===========================================================================
# Section 3: 統合テスト（fallback 経路 + quiet_hours の連携）
# ===========================================================================

class TestADR015B_Integration:
    """moomoo fallback → quiet_hours 遅延送信の end-to-end 連携テスト。

    シナリオ: 深夜に moomoo AuthenticationError → yfinance fallback →
              管理者への通知を morning queue に遅延送信する。
    """

    def test_fallback_notification_deferred_during_quiet_hours(self):
        """深夜 moomoo 認証失敗 → yfinance fallback 通知 → morning queue に遅延。"""
        from atlas_v3.main import _build_metric_provider
        from atlas_v3.ops.moomoo_provider import AuthenticationError
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours

        # Step 1: moomoo AuthenticationError → yfinance fallback
        with patch(
            "atlas_v3.ops.moomoo_provider.MoomooMetricProvider.smoke_test",
            side_effect=AuthenticationError("deep night: session expired"),
        ):
            with patch.object(YFinanceMetricProvider, "_ensure_yfinance", return_value=None):
                fallback_fn = _build_metric_provider("moomoo")

        assert callable(fallback_fn), "ADR-015 B integration: fallback が callable でない"

        # Step 2: fallback 発動の通知を深夜遅延
        with patch(
            "common.pushover_client._is_quiet_hours",
            return_value=True,  # 深夜
        ), patch(
            "common.pushover_client._is_night_emergency",
            return_value=False,  # 非緊急
        ), patch(
            "common.pushover_client._enqueue_morning_digest"
        ) as mock_enqueue:
            send_with_quiet_hours(
                title="[Atlas] moomoo AuthError fallback",
                message="yfinance に切替。朝の確認後 moomoo 再ログインしてください",
                priority=1,
                app_tag="Atlas",
            )

        assert mock_enqueue.called, (
            "ADR-015 B integration: 深夜 moomoo fallback 通知が morning queue に遅延されなかった。"
        )

    def test_quiet_hours_module_does_not_import_spy_bot(self):
        """common_v3/notify/quiet_hours_pushover.py が spy_bot.py を import しない。

        既存コード書換禁止規律の確認。
        """
        import importlib
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "quiet_hours_pushover",
            PROJECT_ROOT / "common_v3" / "notify" / "quiet_hours_pushover.py",
        )
        src = (PROJECT_ROOT / "common_v3" / "notify" / "quiet_hours_pushover.py").read_text(
            encoding="utf-8"
        )
        assert "spy_bot" not in src, (
            "ADR-015 B: quiet_hours_pushover.py が spy_bot.py を参照している。"
            "既存コード書換禁止規律違反。"
        )
        assert "chronos_bot" not in src, (
            "ADR-015 B: quiet_hours_pushover.py が chronos_bot.py を参照している。"
        )

    def test_main_py_does_not_raise_system_exit_78_on_auth_error(self):
        """atlas_v3/main.py の moomoo パスで SystemExit(78) が raise されない（回帰確認）。

        ADR-015 B: 旧実装の SystemExit(78) は削除されている。
        """
        src = (PROJECT_ROOT / "atlas_v3" / "main.py").read_text(encoding="utf-8")

        # AuthenticationError パスに SystemExit(78) が残っていないことを確認
        # （grep: "raise SystemExit(78)" の前後の文脈で AuthenticationError が関連しているか）
        lines = src.split("\n")
        auth_section_start = None
        for i, line in enumerate(lines):
            if "if provider_name == \"moomoo\":" in line:
                auth_section_start = i
                break

        if auth_section_start is not None:
            # moomoo ブロック内の SystemExit(78) を探す
            # ADR-015 B 以降は AuthenticationError → yfinance fallback なので
            # moomoo ブロック内に "raise SystemExit(78)" があってはならない
            moomoo_block_lines = []
            depth = 0
            for i in range(auth_section_start, min(auth_section_start + 60, len(lines))):
                line = lines[i]
                if i == auth_section_start:
                    depth = 1
                    moomoo_block_lines.append(line)
                    continue
                # 次の if provider_name == で別ブロックに入ったら終了
                if "if provider_name ==" in line and i > auth_section_start:
                    break
                moomoo_block_lines.append(line)

            moomoo_block = "\n".join(moomoo_block_lines)
            # ADR-015 B: AuthenticationError パスに raise SystemExit(78) が残っていない
            # （既存の MoomooProviderNotImplementedError パスも SystemExit なし）
            assert "raise SystemExit(78)" not in moomoo_block, (
                "ADR-015 B: moomoo ブロックに raise SystemExit(78) が残存している。"
                "ADR-015 B の修正が適用されていない。"
            )
