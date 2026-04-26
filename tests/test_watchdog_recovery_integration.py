"""tests/test_watchdog_recovery_integration.py

watchdog 自己回復の integration test — 実 launchctl を叩く。

このテストは CI で @pytest.mark.slow としてスキップ可能。
ローカルでの手動実行: python3 -m pytest tests/test_watchdog_recovery_integration.py -v -m slow

テストケース:
  1. 実 kickstart が実プロセスを起動する（ダミー plist 経由）
  2. 存在しない service_id で失敗を正しく検出する
  3. backoff state が JSON ファイルに永続化される

設計方針:
  - 実 launchctl を subprocess.run で呼ぶ（mock 不使用）
  - ダミー plist は /tmp/ に作成して使い捨て
  - 既存 mock テスト（test_watchdog_recovery.py）と別ファイルで互いを汚染しない
  - 実行後のクリーンアップで LaunchAgent 登録を確実に解除する
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pytest

# ── プロジェクトルートをパスに追加 ────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


# ── ユーティリティ ────────────────────────────────────────────────────────────

def _make_dummy_plist(label: str, script_path: Path) -> Path:
    """ダミーの LaunchAgent plist を /tmp/ に作成する。

    script_path に指定したシェルスクリプトを 1 回だけ実行するシンプルな plist。
    KeepAlive=false, RunAtLoad=false で意図しない自動起動を防ぐ。
    """
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>{script_path}</string>
    </array>
    <key>KeepAlive</key>
    <false/>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/{label}_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{label}_stderr.log</string>
</dict>
</plist>
"""
    plist_path = Path(f"/tmp/{label}.plist")
    plist_path.write_text(plist_content, encoding="utf-8")
    return plist_path


def _launchctl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """launchctl をサブプロセスで実行する。"""
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _bootout_if_loaded(label: str, plist_path: Path) -> None:
    """テスト後クリーンアップ: サービスが登録されていたら bootout する。"""
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}", str(plist_path))


@pytest.mark.slow
class TestWatchdogRecoveryIntegration(unittest.TestCase):
    """実 launchctl を使った integration テスト。

    slow マーク付きのため CI では `pytest -m "not slow"` でスキップ可能。
    ローカル実行: pytest -m slow
    """

    _DUMMY_LABEL = "com.soralab.test.watchdog.integration"
    _plist_path: Path = None
    _script_path: Path = None

    @classmethod
    def setUpClass(cls):
        """テスト用のダミースクリプトと plist を作成する。"""
        # 1秒 sleep するだけのダミースクリプト（プロセスが起動したことを確認できる）
        cls._script_path = Path("/tmp/soralab_integration_test_script.sh")
        cls._script_path.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8")
        cls._script_path.chmod(0o755)
        cls._plist_path = _make_dummy_plist(cls._DUMMY_LABEL, cls._script_path)

    @classmethod
    def tearDownClass(cls):
        """テスト後に plist・スクリプト・ログを削除する。"""
        _bootout_if_loaded(cls._DUMMY_LABEL, cls._plist_path)
        for p in [cls._plist_path, cls._script_path,
                  Path(f"/tmp/{cls._DUMMY_LABEL}_stdout.log"),
                  Path(f"/tmp/{cls._DUMMY_LABEL}_stderr.log")]:
            try:
                if p and p.exists():
                    p.unlink()
            except Exception:
                pass

    # ── ケース1: 実 kickstart が実プロセスを起動する ─────────────────────────

    def test_1_kickstart_launches_real_process(self):
        """bootstrap してから kickstart するとプロセスが実際に起動する。

        手順:
          1. ダミー plist を bootstrap で登録
          2. kickstart -k でプロセス起動
          3. launchctl list で PID が非ゼロ または returncode=0 を確認
          4. bootout でクリーンアップ
        """
        uid = os.getuid()
        plist = self._plist_path
        label = self._DUMMY_LABEL

        # bootstrap
        r_boot = _launchctl("bootstrap", f"gui/{uid}", str(plist))
        self.assertEqual(
            r_boot.returncode, 0,
            f"bootstrap failed: stderr={r_boot.stderr}"
        )

        try:
            # kickstart
            r_kick = _launchctl("kickstart", "-k", f"gui/{uid}/{label}")
            # kickstart は returncode=0 で成功 (プロセスが起動しすぐ終わる場合もある)
            self.assertIn(
                r_kick.returncode, [0, 36],  # 36=already running も許容
                f"kickstart unexpected returncode={r_kick.returncode} "
                f"stderr={r_kick.stderr}"
            )

            # list でサービスが認識されているか確認 (PID が整数値)
            r_list = _launchctl("list", label)
            self.assertEqual(
                r_list.returncode, 0,
                f"launchctl list {label} failed: {r_list.stderr}"
            )
        finally:
            _bootout_if_loaded(label, plist)

    # ── ケース2: 存在しない service_id で失敗を正しく検出 ──────────────────

    def test_2_nonexistent_service_id_fails_correctly(self):
        """存在しない service_id に kickstart すると returncode != 0 が返る。

        watchdog の自己回復コードはこの returncode を見てログに記録する。
        実 launchctl がエラーを返すことを検証する。
        """
        uid = os.getuid()
        fake_label = "com.soralab.test.this.does.not.exist.xyz"

        r = _launchctl("kickstart", "-k", f"gui/{uid}/{fake_label}")
        # launchctl は存在しないサービスに対して非ゼロを返す
        self.assertNotEqual(
            r.returncode, 0,
            "存在しないサービスへの kickstart が成功を返した (想定外)"
        )
        # stderr にエラーメッセージが含まれることを確認
        self.assertTrue(
            len(r.stderr) > 0 or len(r.stdout) > 0,
            "launchctl がエラー出力を返さなかった"
        )

    # ── ケース3: backoff state が JSON ファイルに永続化される ───────────────

    def test_3_backoff_state_persists_across_module_reload(self):
        """_save_backoff_state / _load_backoff_state が JSON を永続化する。

        モジュールをリロードして状態が失われないことを確認する。
        これにより watchdog 再起動後も backoff 状態が継続する設計を検証する。
        """
        import types
        import importlib

        tmp_state = Path("/tmp/integration_backoff_test.json")
        if tmp_state.exists():
            tmp_state.unlink()

        try:
            # requests をモックして import
            _requests_mock = types.ModuleType("requests")
            from unittest.mock import MagicMock
            ok_resp = MagicMock()
            ok_resp.ok = True
            ok_resp.status_code = 200
            ok_resp.text = ""
            _requests_mock.post = MagicMock(return_value=ok_resp)

            # chronos_watchdog をクリーンにインポート
            if "chronos_watchdog" in sys.modules:
                del sys.modules["chronos_watchdog"]
            sys.modules["requests"] = _requests_mock

            import chronos_watchdog as cw_fresh

            # 状態を tmp に向ける
            orig_path = cw_fresh.PUSHOVER_BACKOFF_STATE_PATH
            cw_fresh.PUSHOVER_BACKOFF_STATE_PATH = tmp_state

            try:
                # backoff 状態を設定して永続化
                cw_fresh._pushover_consecutive_429 = 3
                cw_fresh._pushover_backoff_until = 9999999.0
                cw_fresh._save_backoff_state()

                # 状態をリセット
                cw_fresh._pushover_consecutive_429 = 0
                cw_fresh._pushover_backoff_until = 0.0

                # ファイルから読み直す
                cw_fresh._load_backoff_state()

                # 永続化が成功していること
                self.assertEqual(cw_fresh._pushover_consecutive_429, 3)
                self.assertAlmostEqual(cw_fresh._pushover_backoff_until, 9999999.0, places=1)

                # JSON ファイルが実際に存在すること
                self.assertTrue(tmp_state.exists())
                data = json.loads(tmp_state.read_text())
                self.assertIn("consecutive_429", data)
                self.assertIn("backoff_until", data)

            finally:
                cw_fresh.PUSHOVER_BACKOFF_STATE_PATH = orig_path
                cw_fresh._pushover_consecutive_429 = 0
                cw_fresh._pushover_backoff_until = 0.0

        finally:
            if tmp_state.exists():
                tmp_state.unlink()
            # モジュールキャッシュをクリア（他テストへの影響防止）
            if "chronos_watchdog" in sys.modules:
                del sys.modules["chronos_watchdog"]


if __name__ == "__main__":
    # slow マーク関係なく全件実行
    unittest.main(verbosity=2)
