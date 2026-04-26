"""
tests/test_dead_man_infra.py — scripts/dead_man_switch.py の OpenD + atlas-paper 死活監視テスト

カバレッジ:
    1. _is_opend_alive() - pgrep 失敗 → False
    2. _is_opend_alive() - pgrep 成功 + ポート closed → False
    3. _is_opend_alive() - pgrep 成功 + ポート open → True
    4. _is_opend_alive() - subprocess.TimeoutExpired → False
    5. _is_atlas_paper_alive() - launchctl returncode != 0 → False
    6. _is_atlas_paper_alive() - plist 形式 PID あり → True
    7. _is_atlas_paper_alive() - plist 形式 PID なし → False
    8. _is_atlas_paper_alive() - テーブル形式 PID あり → True
    9. _is_atlas_paper_alive() - テーブル形式 PID = "-" → False
   10. _is_atlas_paper_alive() - TimeoutExpired → False
   11. _check_infra() - OpenD dead → P1 alert 呼出
   12. _check_infra() - atlas-paper dead → P1 alert 呼出
   13. _check_infra() - 両方 alive → alert なし
   14. _check_infra() - Pushover ImportError → クラッシュせず fallback_log のみ
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

import scripts.dead_man_switch as dms


# ─────────────────────────────────────────────────────────────────────────────
# Helper: subprocess.CompletedProcess ファクトリ
# ─────────────────────────────────────────────────────────────────────────────

def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    cp: subprocess.CompletedProcess = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )
    return cp


# ─────────────────────────────────────────────────────────────────────────────
# 1-4: _is_opend_alive()
# ─────────────────────────────────────────────────────────────────────────────

class TestIsOpendAlive:
    def test_pgrep_fails_returns_false(self) -> None:
        """pgrep が returncode=1 (プロセス不在) → False。"""
        with patch("scripts.dead_man_switch.subprocess.run", return_value=_cp(returncode=1)):
            assert dms._is_opend_alive() is False

    def test_pgrep_ok_port_closed_returns_false(self) -> None:
        """pgrep 成功 + ポート接続失敗 → False。"""
        import socket as _socket
        with patch("scripts.dead_man_switch.subprocess.run", return_value=_cp(returncode=0)):
            with patch("scripts.dead_man_switch.socket.create_connection",
                       side_effect=OSError("Connection refused")):
                assert dms._is_opend_alive() is False

    def test_pgrep_ok_port_open_returns_true(self) -> None:
        """pgrep 成功 + ポート接続成功 → True。"""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("scripts.dead_man_switch.subprocess.run", return_value=_cp(returncode=0)):
            with patch("scripts.dead_man_switch.socket.create_connection",
                       return_value=mock_conn):
                assert dms._is_opend_alive() is True

    def test_subprocess_timeout_returns_false(self) -> None:
        """subprocess.TimeoutExpired → False (クラッシュしない)。"""
        with patch("scripts.dead_man_switch.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5)):
            assert dms._is_opend_alive() is False


# ─────────────────────────────────────────────────────────────────────────────
# 5-10: _is_atlas_paper_alive()
# ─────────────────────────────────────────────────────────────────────────────

_PLIST_WITH_PID = """\
{
\t"StandardOutPath" = "/dev/null";
\t"Label" = "com.soralab.atlas-paper";
\t"LastExitStatus" = 0;
\t"PID" = 12345;
}
"""

_PLIST_WITHOUT_PID = """\
{
\t"StandardOutPath" = "/dev/null";
\t"Label" = "com.soralab.atlas-paper";
\t"LastExitStatus" = 0;
}
"""

_TABLE_WITH_PID = "12345\t0\tcom.soralab.atlas-paper\n"
_TABLE_NO_PID   = "-\t0\tcom.soralab.atlas-paper\n"


class TestIsAtlasPaperAlive:
    def test_launchctl_nonzero_returns_false(self) -> None:
        """launchctl list が returncode=1 → False。"""
        with patch("scripts.dead_man_switch.subprocess.run",
                   return_value=_cp(returncode=1, stdout="")):
            assert dms._is_atlas_paper_alive() is False

    def test_plist_with_pid_returns_true(self) -> None:
        """plist 形式で PID フィールドに数値がある → True。"""
        with patch("scripts.dead_man_switch.subprocess.run",
                   return_value=_cp(returncode=0, stdout=_PLIST_WITH_PID)):
            assert dms._is_atlas_paper_alive() is True

    def test_plist_without_pid_returns_false(self) -> None:
        """plist 形式で PID フィールドがない → False。"""
        with patch("scripts.dead_man_switch.subprocess.run",
                   return_value=_cp(returncode=0, stdout=_PLIST_WITHOUT_PID)):
            assert dms._is_atlas_paper_alive() is False

    def test_table_with_pid_returns_true(self) -> None:
        """テーブル形式 PID=数値 → True。"""
        with patch("scripts.dead_man_switch.subprocess.run",
                   return_value=_cp(returncode=0, stdout=_TABLE_WITH_PID)):
            assert dms._is_atlas_paper_alive() is True

    def test_table_no_pid_returns_false(self) -> None:
        """テーブル形式 PID="-" → False。"""
        with patch("scripts.dead_man_switch.subprocess.run",
                   return_value=_cp(returncode=0, stdout=_TABLE_NO_PID)):
            assert dms._is_atlas_paper_alive() is False

    def test_launchctl_timeout_returns_false(self) -> None:
        """launchctl が TimeoutExpired → False (クラッシュしない)。"""
        with patch("scripts.dead_man_switch.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="launchctl", timeout=10)):
            assert dms._is_atlas_paper_alive() is False


# ─────────────────────────────────────────────────────────────────────────────
# 11-14: _check_infra()
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckInfra:
    """_check_infra() が正しく alert / no-alert を振り分けるかを検証する。"""

    def test_opend_dead_triggers_p1_alert(self, tmp_path, monkeypatch) -> None:
        """OpenD が dead のとき P1 (priority=1) で Pushover を呼ぶ。"""
        pushover_mock = MagicMock()
        pushover_module = MagicMock()
        pushover_module.send = pushover_mock

        monkeypatch.setattr(dms, "LOG_DIR", tmp_path)
        with patch("scripts.dead_man_switch._is_opend_alive", return_value=False), \
             patch("scripts.dead_man_switch._is_atlas_paper_alive", return_value=True), \
             patch.dict("sys.modules", {"common.pushover_client": pushover_module}):
            dms._check_infra()

        pushover_mock.assert_called_once()
        call_kwargs = pushover_mock.call_args
        assert call_kwargs.kwargs.get("priority") == 1 or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] == 1
        ), f"priority=1 が渡されていない: {call_kwargs}"

    def test_atlas_paper_dead_triggers_p1_alert(self, tmp_path, monkeypatch) -> None:
        """atlas-paper が dead のとき P1 で Pushover を呼ぶ。"""
        pushover_mock = MagicMock()
        pushover_module = MagicMock()
        pushover_module.send = pushover_mock

        monkeypatch.setattr(dms, "LOG_DIR", tmp_path)
        with patch("scripts.dead_man_switch._is_opend_alive", return_value=True), \
             patch("scripts.dead_man_switch._is_atlas_paper_alive", return_value=False), \
             patch.dict("sys.modules", {"common.pushover_client": pushover_module}):
            dms._check_infra()

        pushover_mock.assert_called_once()
        call_kwargs = pushover_mock.call_args
        assert call_kwargs.kwargs.get("priority") == 1 or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] == 1
        ), f"priority=1 が渡されていない: {call_kwargs}"

    def test_both_alive_no_alert(self, tmp_path, monkeypatch) -> None:
        """両方 alive → Pushover を呼ばない。"""
        pushover_mock = MagicMock()
        pushover_module = MagicMock()
        pushover_module.send = pushover_mock

        monkeypatch.setattr(dms, "LOG_DIR", tmp_path)
        with patch("scripts.dead_man_switch._is_opend_alive", return_value=True), \
             patch("scripts.dead_man_switch._is_atlas_paper_alive", return_value=True), \
             patch.dict("sys.modules", {"common.pushover_client": pushover_module}):
            dms._check_infra()

        pushover_mock.assert_not_called()

    def test_pushover_import_error_no_crash(self, tmp_path, monkeypatch) -> None:
        """Pushover import 失敗でも例外をスローせず fallback_log だけ記録する。"""
        monkeypatch.setattr(dms, "LOG_DIR", tmp_path)

        # common.pushover_client を import 失敗にする
        import sys
        saved = sys.modules.pop("common.pushover_client", None)
        try:
            with patch("scripts.dead_man_switch._is_opend_alive", return_value=False), \
                 patch("scripts.dead_man_switch._is_atlas_paper_alive", return_value=False), \
                 patch.dict("sys.modules", {"common.pushover_client": None}):  # type: ignore[dict-item]
                # None を差し込むと import で ImportError が発生する
                try:
                    dms._check_infra()  # クラッシュしないこと
                except Exception as exc:  # noqa: BLE001
                    pytest.fail(f"_check_infra() が例外をスローした: {exc}")
        finally:
            if saved is not None:
                sys.modules["common.pushover_client"] = saved
            elif "common.pushover_client" in sys.modules:
                del sys.modules["common.pushover_client"]

        # fallback_log が記録されていること
        fallback = tmp_path / "dead_man_fallback.log"
        assert fallback.exists(), "fallback log が作成されていない"
        content = fallback.read_text(encoding="utf-8")
        assert "OpenD" in content or "atlas-paper" in content
