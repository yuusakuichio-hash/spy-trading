"""tests/test_atlas_v3_bots_subprocess_20260425.py

atlas_v3/bots/ subprocess 境界 launcher テスト — 15 件以上。

Redteam 対案採択 (2026-04-25) — delegate 禁止 / subprocess 境界隔離 検証。

テスト分類
----------
[argv]       build_spy_bot_argv: --mode → spy_bot.py argv 変換正当性
[argparse]   build_parser: 各フラグ解析 / 無効値エラー
[launch]     launch_spy_bot: subprocess.Popen 引数構築 mock 検証
[sigterm]    SIGTERM forward: _setup_sigterm_forward が子プロセスに転送する
[exitcode]   main(): spy_bot の exit code を透過的に返す
[no_import]  spy_bot.py 書換 0 検証: atlas_v3.bots が spy_bot を import しない
[smoke]      main() mock smoke: Popen mock で main() が正常終了する
"""
from __future__ import annotations

import pathlib
import signal
import subprocess
import sys
import types
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# [argv] build_spy_bot_argv テスト
# ---------------------------------------------------------------------------

class TestBuildSpyBotArgv:
    """build_spy_bot_argv: --mode × フラグ → spy_bot argv 変換を網羅する。"""

    def _parse_and_build(self, cli_args: list[str]) -> list[str]:
        from atlas_v3.bots.main import build_parser, build_spy_bot_argv
        args = build_parser().parse_args(cli_args)
        return build_spy_bot_argv(args)

    def test_mode_paper_gives_paper_flag(self):
        """--mode paper → ['--paper']"""
        result = self._parse_and_build(["--mode", "paper"])
        assert result == ["--paper"]

    def test_mode_live_gives_empty(self):
        """--mode live → [] (spy_bot のデフォルトが live)"""
        result = self._parse_and_build(["--mode", "live"])
        assert result == []

    def test_mode_dry_gives_paper_and_dry_test(self):
        """--mode dry → ['--paper', '--dry-test']"""
        result = self._parse_and_build(["--mode", "dry"])
        assert "--paper" in result
        assert "--dry-test" in result

    def test_mode_test_connect_gives_paper_and_test_connect(self):
        """--mode test-connect → ['--paper', '--test-connect']"""
        result = self._parse_and_build(["--mode", "test-connect"])
        assert "--paper" in result
        assert "--test-connect" in result

    def test_dry_run_flag_appends_dry_test(self):
        """--mode paper --dry-run → ['--paper', '--dry-test']"""
        result = self._parse_and_build(["--mode", "paper", "--dry-run"])
        assert "--dry-test" in result
        assert "--paper" in result

    def test_no_orb_forwarded(self):
        """--no-orb が spy_argv に含まれる。"""
        result = self._parse_and_build(["--mode", "paper", "--no-orb"])
        assert "--no-orb" in result

    def test_no_calendar_forwarded(self):
        """--no-calendar が spy_argv に含まれる。"""
        result = self._parse_and_build(["--mode", "paper", "--no-calendar"])
        assert "--no-calendar" in result

    def test_no_multi_forwarded(self):
        """--no-multi が spy_argv に含まれる。"""
        result = self._parse_and_build(["--mode", "paper", "--no-multi"])
        assert "--no-multi" in result

    def test_mode_dry_plus_dry_run_no_duplicate_dry_test(self):
        """--mode dry --dry-run で --dry-test が重複しない。"""
        result = self._parse_and_build(["--mode", "dry", "--dry-run"])
        assert result.count("--dry-test") == 1

    def test_all_disable_flags_combined(self):
        """--no-orb --no-calendar --no-multi が全部含まれる。"""
        result = self._parse_and_build(
            ["--mode", "paper", "--no-orb", "--no-calendar", "--no-multi"]
        )
        assert "--no-orb" in result
        assert "--no-calendar" in result
        assert "--no-multi" in result


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


# ---------------------------------------------------------------------------
# [launch] launch_spy_bot subprocess mock 検証
# ---------------------------------------------------------------------------

class TestLaunchSpyBot:
    """launch_spy_bot: Popen に渡す cmd / cwd / env を確認する。"""

    def test_popen_cmd_contains_sys_executable(self, tmp_path):
        """cmd[0] が sys.executable になる。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=9999)
            from atlas_v3.bots.main import launch_spy_bot
            launch_spy_bot(["--paper"], spy_bot_path=fake_spy, inherit_stdio=False)

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == sys.executable

    def test_popen_cmd_contains_spy_bot_path(self, tmp_path):
        """cmd[1] が spy_bot.py の絶対パスになる。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=9999)
            from atlas_v3.bots.main import launch_spy_bot
            launch_spy_bot(["--paper"], spy_bot_path=fake_spy, inherit_stdio=False)

        cmd = mock_popen.call_args[0][0]
        assert cmd[1] == str(fake_spy)

    def test_popen_cmd_contains_spy_argv(self, tmp_path):
        """spy_argv が cmd に追加される。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=9999)
            from atlas_v3.bots.main import launch_spy_bot
            launch_spy_bot(["--paper", "--no-orb"], spy_bot_path=fake_spy, inherit_stdio=False)

        cmd = mock_popen.call_args[0][0]
        assert "--paper" in cmd
        assert "--no-orb" in cmd

    def test_popen_cwd_is_spy_bot_parent(self, tmp_path):
        """cwd が spy_bot.py の親ディレクトリになる。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=9999)
            from atlas_v3.bots.main import launch_spy_bot
            launch_spy_bot(["--paper"], spy_bot_path=fake_spy, inherit_stdio=False)

        kwargs = mock_popen.call_args[1]
        assert kwargs["cwd"] == str(tmp_path)

    def test_popen_pipe_when_not_inherit_stdio(self, tmp_path):
        """inherit_stdio=False のとき stdout/stderr が PIPE になる。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=9999)
            from atlas_v3.bots.main import launch_spy_bot
            launch_spy_bot(["--paper"], spy_bot_path=fake_spy, inherit_stdio=False)

        kwargs = mock_popen.call_args[1]
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE

    def test_popen_inherit_none_when_inherit_stdio(self, tmp_path):
        """inherit_stdio=True のとき stdout/stderr が None (継承) になる。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=9999)
            from atlas_v3.bots.main import launch_spy_bot
            launch_spy_bot(["--paper"], spy_bot_path=fake_spy, inherit_stdio=True)

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("stdout") is None
        assert kwargs.get("stderr") is None


# ---------------------------------------------------------------------------
# [sigterm] SIGTERM forward テスト
# ---------------------------------------------------------------------------

class TestSigtermForward:
    """_setup_sigterm_forward: SIGTERM 受信時に子プロセスへ転送する。"""

    def test_sigterm_handler_registered(self):
        """_setup_sigterm_forward 呼び出し後に SIGTERM ハンドラが変わる。"""
        from atlas_v3.bots.main import _setup_sigterm_forward

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        _setup_sigterm_forward(mock_proc)

        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not signal.SIG_DFL

        # 復元
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_sigterm_handler_calls_send_signal(self):
        """SIGTERM 受信時に proc.send_signal(SIGTERM) が呼ばれる。"""
        from atlas_v3.bots.main import _setup_sigterm_forward

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        _setup_sigterm_forward(mock_proc)

        # ハンドラを直接呼び出してテスト
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)

        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)

        # 復元
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_sigterm_handler_tolerates_process_lookup_error(self):
        """proc.send_signal が ProcessLookupError を上げても例外が伝播しない。"""
        from atlas_v3.bots.main import _setup_sigterm_forward

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.send_signal.side_effect = ProcessLookupError

        _setup_sigterm_forward(mock_proc)
        handler = signal.getsignal(signal.SIGTERM)

        # 例外が伝播しないことを確認
        handler(signal.SIGTERM, None)  # ProcessLookupError を飲み込む

        # 復元
        signal.signal(signal.SIGTERM, signal.SIG_DFL)


# ---------------------------------------------------------------------------
# [exitcode] exit code 透過テスト
# ---------------------------------------------------------------------------

class TestExitCodeTransparency:
    """main(): spy_bot の終了コードをそのまま返す。"""

    def _make_mock_proc(self, returncode: int) -> MagicMock:
        proc = MagicMock()
        proc.pid = 42
        proc.returncode = returncode
        proc.wait.return_value = returncode
        return proc

    def test_exit_code_0_propagated(self, tmp_path):
        """spy_bot が rc=0 で終了したとき main() が 0 を返す。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = self._make_mock_proc(0)
        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc), \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            rc = main(["--mode", "paper"])

        assert rc == 0

    def test_exit_code_1_propagated(self, tmp_path):
        """spy_bot が rc=1 で終了したとき main() が 1 を返す。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = self._make_mock_proc(1)
        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc), \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            rc = main(["--mode", "paper"])

        assert rc == 1

    def test_exit_code_2_propagated(self, tmp_path):
        """spy_bot が rc=2 で終了したとき main() が 2 を返す。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = self._make_mock_proc(2)
        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc), \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            rc = main(["--mode", "paper"])

        assert rc == 2


# ---------------------------------------------------------------------------
# [no_import] spy_bot.py 書換 0 検証
# ---------------------------------------------------------------------------

class TestNoSpyBotImport:
    """atlas_v3.bots が spy_bot を import しないことを検証する。"""

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

    def test_trader_py_removed_or_no_spy_bot_import(self):
        """trader.py が残っている場合、spy_bot を import していないこと。
        (trader.py は delegate 実装のため存在しても spy_bot import 禁止)"""
        bots_dir = pathlib.Path(__file__).parents[1] / "atlas_v3" / "bots"
        trader_path = bots_dir / "trader.py"
        if not trader_path.exists():
            pytest.skip("trader.py は削除済み — 問題なし")
        src = trader_path.read_text(encoding="utf-8")
        assert "import spy_bot" not in src, (
            "trader.py が spy_bot を直接 import している — delegate 禁止違反"
        )


# ---------------------------------------------------------------------------
# [smoke] main() mock smoke テスト
# ---------------------------------------------------------------------------

class TestMainSmoke:
    """main() mock smoke: Popen mock で main() が正常に動作する。"""

    def test_main_paper_smoke(self, tmp_path):
        """--mode paper で main() が 0 を返す。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = MagicMock(pid=1001, returncode=0)
        mock_proc.wait.return_value = 0

        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc), \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            rc = main(["--mode", "paper"])

        assert rc == 0

    def test_main_live_smoke(self, tmp_path):
        """--mode live で main() が 0 を返す。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = MagicMock(pid=1002, returncode=0)
        mock_proc.wait.return_value = 0

        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc), \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            rc = main(["--mode", "live"])

        assert rc == 0

    def test_main_dry_smoke(self, tmp_path):
        """--mode dry で main() が 0 を返す。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = MagicMock(pid=1003, returncode=0)
        mock_proc.wait.return_value = 0

        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc), \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            rc = main(["--mode", "dry"])

        assert rc == 0

    def test_main_launches_once(self, tmp_path):
        """main() が launch_spy_bot を 1 回だけ呼ぶ。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = MagicMock(pid=1004, returncode=0)
        mock_proc.wait.return_value = 0

        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc) as mock_launch, \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            main(["--mode", "paper"])

        assert mock_launch.call_count == 1

    def test_main_keyboard_interrupt_sends_sigterm(self, tmp_path):
        """KeyboardInterrupt 時に proc.send_signal(SIGTERM) が呼ばれる。"""
        fake_spy = tmp_path / "spy_bot.py"
        fake_spy.write_text("# stub")

        mock_proc = MagicMock(pid=1005, returncode=0)
        mock_proc.wait.side_effect = [KeyboardInterrupt, None]

        with patch("atlas_v3.bots.main.launch_spy_bot", return_value=mock_proc), \
             patch("atlas_v3.bots.main._SPY_BOT_PATH", fake_spy), \
             patch("atlas_v3.bots.main._setup_sigterm_forward"):
            from atlas_v3.bots.main import main
            main(["--mode", "paper"])

        mock_proc.send_signal.assert_called_with(signal.SIGTERM)
