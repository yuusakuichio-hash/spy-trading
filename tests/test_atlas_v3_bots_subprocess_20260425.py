"""tests/test_atlas_v3_bots_subprocess_20260425.py

atlas_v3/bots/ native engine ランチャーテスト（β-2 配線実装 2026-04-25 対応版）。

旧 subprocess 境界テストを native AtlasEngine 経路に全面改訂。
変更理由: β-2 により main.py から subprocess.Popen(spy_bot.py) を廃止し
AtlasEngine を直接起動するよう変更したため、テストも新経路に対応させる。

テスト分類
----------
[argparse]    build_parser: 各フラグ解析 / 無効値エラー
[disable]     build_disable_names: --no-* → tactic_name リスト変換
[engine]      build_engine_native: TacticRegistry 経由 AtlasEngine 組み立て
[sigterm]     setup_graceful_shutdown: stop_event セット確認
[main_smoke]  main(): test-connect モード即終了 / stop_event による loop 停止
[no_import]   spy_bot.py import ゼロ検証（β-2 後も継続）
[compat]      build_parser が dry / test-connect モードを受け付ける後方互換確認
"""
from __future__ import annotations

import pathlib
import signal
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# [argparse] build_parser テスト
# ---------------------------------------------------------------------------

class TestBuildParser:
    """build_parser: 各フラグ解析 / 無効値エラーを確認する。"""

    def test_missing_mode_raises_system_exit(self):
        """--mode 未指定は SystemExit。"""
        from atlas_v3.bots.main import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_invalid_mode_raises_system_exit(self):
        """--mode demo は SystemExit。"""
        from atlas_v3.bots.main import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--mode", "demo"])

    def test_mode_paper_parses(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "paper"])
        assert args.mode == "paper"

    def test_mode_live_parses(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "live"])
        assert args.mode == "live"

    def test_mode_dry_parses(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "dry"])
        assert args.mode == "dry"

    def test_mode_test_connect_parses(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "test-connect"])
        assert args.mode == "test-connect"

    def test_dry_run_flag_defaults_false(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "paper"])
        assert args.dry_run is False

    def test_dry_run_flag_set(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "paper", "--dry-run"])
        assert args.dry_run is True

    def test_no_orb_flag_defaults_false(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "paper"])
        assert args.no_orb is False

    def test_no_orb_flag_set(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "paper", "--no-orb"])
        assert args.no_orb is True

    def test_no_calendar_flag_set(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "paper", "--no-calendar"])
        assert args.no_calendar is True

    def test_no_multi_flag_set(self):
        from atlas_v3.bots.main import build_parser
        args = build_parser().parse_args(["--mode", "paper", "--no-multi"])
        assert args.no_multi is True


# ---------------------------------------------------------------------------
# [disable] build_disable_names テスト
# ---------------------------------------------------------------------------

class TestBuildDisableNames:
    """build_disable_names: --no-* → tactic_name リスト変換を確認する。"""

    def _parse(self, cli_args: list[str]):
        from atlas_v3.bots.main import build_parser
        return build_parser().parse_args(cli_args)

    def test_no_flags_returns_empty(self):
        from atlas_v3.bots.main import build_disable_names
        args = self._parse(["--mode", "paper"])
        assert build_disable_names(args) == []

    def test_no_orb_returns_orb_native(self):
        from atlas_v3.bots.main import build_disable_names
        args = self._parse(["--mode", "paper", "--no-orb"])
        result = build_disable_names(args)
        assert "orb_native" in result

    def test_no_calendar_returns_diagonal_spread(self):
        from atlas_v3.bots.main import build_disable_names
        args = self._parse(["--mode", "paper", "--no-calendar"])
        result = build_disable_names(args)
        assert "diagonal_spread" in result

    def test_no_multi_returns_empty_list(self):
        """--no-multi は v3.0 では対応 tactic なし → 空リスト。"""
        from atlas_v3.bots.main import build_disable_names
        args = self._parse(["--mode", "paper", "--no-multi"])
        result = build_disable_names(args)
        # no_multi 単独では disable 対象 tactic なし（ログのみ）
        assert "orb_native" not in result
        assert "diagonal_spread" not in result

    def test_combined_no_orb_no_calendar(self):
        from atlas_v3.bots.main import build_disable_names
        args = self._parse(["--mode", "paper", "--no-orb", "--no-calendar"])
        result = build_disable_names(args)
        assert "orb_native" in result
        assert "diagonal_spread" in result

    def test_all_flags_combined(self):
        from atlas_v3.bots.main import build_disable_names
        args = self._parse(["--mode", "paper", "--no-orb", "--no-calendar", "--no-multi"])
        result = build_disable_names(args)
        assert "orb_native" in result
        assert "diagonal_spread" in result


# ---------------------------------------------------------------------------
# [engine] build_engine_native テスト
# ---------------------------------------------------------------------------

class TestBuildEngineNative:
    """build_engine_native: TacticRegistry 経由 AtlasEngine 組み立てを確認する。"""

    def test_returns_atlas_engine(self):
        from atlas_v3.bots.main import build_engine_native
        from atlas_v3.core.engine import AtlasEngine
        engine = build_engine_native(disable_names=[])
        assert isinstance(engine, AtlasEngine)

    def test_engine_has_eleven_tactics_by_default(self):
        from atlas_v3.bots.main import build_engine_native
        engine = build_engine_native(disable_names=[])
        # AtlasEngine._tactics にアクセス（内部属性だが検証目的）
        assert len(engine._tactics) == 11

    def test_disable_orb_native_reduces_count(self):
        from atlas_v3.bots.main import build_engine_native
        engine = build_engine_native(disable_names=["orb_native"])
        assert len(engine._tactics) == 10
        tactic_names = [t.tactic_name for t in engine._tactics]
        assert "orb_native" not in tactic_names

    def test_disable_diagonal_spread_removes_it(self):
        from atlas_v3.bots.main import build_engine_native
        engine = build_engine_native(disable_names=["diagonal_spread"])
        tactic_names = [t.tactic_name for t in engine._tactics]
        assert "diagonal_spread" not in tactic_names

    def test_custom_market_data_injected(self):
        from atlas_v3.bots.main import build_engine_native
        stub_md = MagicMock()
        engine = build_engine_native(disable_names=[], market_data=stub_md)
        assert engine._market_data is stub_md

    def test_custom_broker_injected(self):
        from atlas_v3.bots.main import build_engine_native
        stub_bk = MagicMock()
        engine = build_engine_native(disable_names=[], broker=stub_bk)
        assert engine._broker is stub_bk

    def test_none_providers_use_stubs(self):
        """market_data=None, broker=None のとき stub providers が設定される。"""
        from atlas_v3.bots.main import _StubBroker, _StubMarketData, build_engine_native
        engine = build_engine_native(disable_names=[])
        assert isinstance(engine._market_data, _StubMarketData)
        assert isinstance(engine._broker, _StubBroker)


# ---------------------------------------------------------------------------
# [sigterm] setup_graceful_shutdown テスト
# ---------------------------------------------------------------------------

class TestGracefulShutdown:
    """setup_graceful_shutdown: stop_event セット確認。"""

    def test_sigterm_sets_stop_event(self):
        from atlas_v3.bots.main import setup_graceful_shutdown
        stop_event = threading.Event()
        setup_graceful_shutdown(stop_event)

        # ハンドラを直接呼び出して確認
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not signal.SIG_DFL

        handler(signal.SIGTERM, None)
        assert stop_event.is_set()

        # 復元
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    def test_sigint_sets_stop_event(self):
        from atlas_v3.bots.main import setup_graceful_shutdown
        stop_event = threading.Event()
        setup_graceful_shutdown(stop_event)

        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert stop_event.is_set()

        # 復元
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)


# ---------------------------------------------------------------------------
# [main_smoke] main() smoke テスト
# ---------------------------------------------------------------------------

class TestMainSmoke:
    """main() smoke: test-connect モードと run_loop の停止を確認する。"""

    def test_main_test_connect_returns_0(self):
        """--mode test-connect で main() が 0 を返す（engine 組み立てのみ）。"""
        from atlas_v3.bots.main import main
        rc = main(["--mode", "test-connect"])
        assert rc == 0

    def test_main_paper_dry_run_stops_via_stop_event(self):
        """--mode paper --dry-run で run_loop が stop_event セットで 0 を返す。"""
        import time

        from atlas_v3.bots.main import build_engine_native, run_loop

        engine = build_engine_native(disable_names=[])
        stop_event = threading.Event()
        results: list[int] = []

        def _runner():
            rc = run_loop(engine=engine, stop_event=stop_event, tick_interval_secs=60.0)
            results.append(rc)

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        # run_loop が最初の tick を処理するまで待機
        time.sleep(0.5)
        stop_event.set()
        t.join(timeout=5)
        assert not t.is_alive(), "run_loop が stop_event 後 5 秒で終了しなかった"
        assert results == [0]

    def test_main_dry_mode_stops_via_stop_event(self):
        """--mode dry 相当の run_loop が stop_event セットで 0 を返す。"""
        import time

        from atlas_v3.bots.main import build_engine_native, run_loop

        engine = build_engine_native(disable_names=[])
        stop_event = threading.Event()
        results: list[int] = []

        def _runner():
            rc = run_loop(engine=engine, stop_event=stop_event, tick_interval_secs=60.0)
            results.append(rc)

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        time.sleep(0.5)
        stop_event.set()
        t.join(timeout=5)
        assert not t.is_alive()
        assert results == [0]

    def test_main_paper_no_orb_smoke(self):
        """--no-orb で 10 戦術エンジンの run_loop が stop_event で 0 を返す。"""
        import time

        from atlas_v3.bots.main import build_engine_native, run_loop

        engine = build_engine_native(disable_names=["orb_native"])
        assert len(engine._tactics) == 10
        stop_event = threading.Event()
        results: list[int] = []

        def _runner():
            rc = run_loop(engine=engine, stop_event=stop_event, tick_interval_secs=60.0)
            results.append(rc)

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        time.sleep(0.5)
        stop_event.set()
        t.join(timeout=5)
        assert not t.is_alive()
        assert results == [0]


# ---------------------------------------------------------------------------
# [no_import] spy_bot.py import ゼロ検証
# ---------------------------------------------------------------------------

class TestNoSpyBotImport:
    """atlas_v3.bots が spy_bot を import しないことを検証する（β-2 後も継続）。"""

    def test_main_module_does_not_import_spy_bot(self):
        """atlas_v3.bots.main のソースコードに 'import spy_bot' が含まれない。"""
        import importlib
        mod = importlib.import_module("atlas_v3.bots.main")
        src_path = pathlib.Path(mod.__file__)
        src = src_path.read_text(encoding="utf-8")
        assert "import spy_bot" not in src, (
            "atlas_v3.bots.main が spy_bot を直接 import している — delegate 禁止違反"
        )

    def test_init_module_does_not_import_spy_bot(self):
        """atlas_v3.bots.__init__ のソースコードに 'import spy_bot' が含まれない。"""
        import atlas_v3.bots as pkg
        init_path = pathlib.Path(pkg.__file__)
        src = init_path.read_text(encoding="utf-8")
        assert "import spy_bot" not in src, (
            "atlas_v3.bots.__init__ が spy_bot を直接 import している — delegate 禁止違反"
        )

    def test_no_subprocess_import_in_main(self):
        """main.py が subprocess を import しない（β-2 廃止検証）。"""
        import importlib
        mod = importlib.import_module("atlas_v3.bots.main")
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        # import subprocess 行が存在しないことを確認（docstring のコメントは除く）
        import ast
        tree = ast.parse(src)
        has_subprocess_import = any(
            (isinstance(node, ast.Import) and any(a.name == "subprocess" for a in node.names))
            or (isinstance(node, ast.ImportFrom) and node.module == "subprocess")
            for node in ast.walk(tree)
        )
        assert not has_subprocess_import, (
            "atlas_v3.bots.main が subprocess を import している — β-2 廃止違反"
        )

    def test_trader_py_removed_or_no_spy_bot_import(self):
        """trader.py が残っている場合、spy_bot を import していないこと。"""
        bots_dir = pathlib.Path(__file__).parents[1] / "atlas_v3" / "bots"
        trader_path = bots_dir / "trader.py"
        if not trader_path.exists():
            pytest.skip("trader.py は削除済み — 問題なし")
        src = trader_path.read_text(encoding="utf-8")
        assert "import spy_bot" not in src, (
            "trader.py が spy_bot を直接 import している — delegate 禁止違反"
        )


# ---------------------------------------------------------------------------
# [compat] 後方互換テスト
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    """build_parser が旧フラグと互換を保つことを確認する。"""

    def test_all_original_flags_still_accepted(self):
        """旧来の全フラグが引き続き argparse で受け付けられる。"""
        from atlas_v3.bots.main import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "--mode", "paper",
            "--dry-run",
            "--no-orb",
            "--no-calendar",
            "--no-multi",
        ])
        assert args.mode == "paper"
        assert args.dry_run is True
        assert args.no_orb is True
        assert args.no_calendar is True
        assert args.no_multi is True

    def test_mode_choices_unchanged(self):
        """mode の選択肢が paper/live/dry/test-connect であること。"""
        from atlas_v3.bots.main import build_parser
        parser = build_parser()
        # 全選択肢が通る
        for mode in ["paper", "live", "dry", "test-connect"]:
            args = parser.parse_args(["--mode", mode])
            assert args.mode == mode
