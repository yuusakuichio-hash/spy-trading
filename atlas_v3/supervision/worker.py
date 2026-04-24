"""atlas_v3/supervision/worker.py — Supervisor 配下 worker protocol / ABC

設計方針
--------
- worker は multiprocessing.Process として Supervisor から spawn される。
- WorkerBase を継承して main() を実装するだけで Supervisor に登録可能。
- healthcheck() は Supervisor が定期的に呼び出して生死確認に使う。
  デフォルト実装は Process.is_alive() を見るだけ。
- subprocess 境界で動く legacy bot（spy_bot.py 等）は
  SubprocessWorker を使って Process に包む（atlas_v3.bots.main パターン踏襲）。
"""
from __future__ import annotations

import abc
import logging
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

log = logging.getLogger("atlas.supervision.worker")


class WorkerBase(abc.ABC):
    """Supervisor 下で動く worker の共通 ABC。

    使い方
    ------
    1. WorkerBase を継承して main() を実装する。
    2. ChildSpec(entry_point=MyWorker, ...) で Supervisor に登録する。
    3. Supervisor が start() → main() → (crash) → restart を管理する。

    ライフサイクル
    --------------
    start()  → main() が実行される（multiprocessing.Process.run() から呼ばれる）
    stop()   → SIGTERM を送って graceful shutdown を促す
    healthcheck() → 生死判定（True: OK / False: dead/unhealthy）
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs
        self._log = logging.getLogger(f"atlas.supervision.worker.{name}")

    @abc.abstractmethod
    def main(self) -> None:
        """worker のメインループ。Supervisor はこれを Process.run() から呼ぶ。"""

    def healthcheck(self) -> bool:
        """worker が健全かどうかを返す。

        デフォルト実装: 常に True を返す（Process.is_alive() が真の前提）。
        長時間ループ系 worker はファイル heartbeat 等を使ってオーバーライド推奨。
        """
        return True

    def on_start(self) -> None:
        """start 直前フック（サブクラスでオーバーライド可）。"""

    def on_stop(self) -> None:
        """stop 直後フック（サブクラスでオーバーライド可）。"""


class SubprocessWorker(WorkerBase):
    """legacy bot を subprocess で包む汎用 worker。

    atlas_v3.bots.main の SIGTERM forward パターンを踏襲する。
    spy_bot.py のような既存スクリプトを Supervisor 配下に置く用途。

    Parameters
    ----------
    name : str
        worker 識別名
    cmd : list[str]
        実行コマンド（例: [sys.executable, "/path/to/spy_bot.py", "--paper"]）
    cwd : str | None
        subprocess の作業ディレクトリ。None のとき現在ディレクトリ。
    """

    def __init__(self, name: str, cmd: list[str], cwd: str | None = None, **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self.cmd = cmd
        self.cwd = cwd
        self._proc: subprocess.Popen | None = None

    def main(self) -> None:
        self._log.info("SubprocessWorker: cmd=%s cwd=%s", self.cmd, self.cwd)
        self._proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=os.environ.copy(),
        )

        # SIGTERM → 子プロセスに転送
        def _forward(signum: int, frame: object) -> None:
            if self._proc is not None:
                try:
                    self._proc.send_signal(signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

        signal.signal(signal.SIGTERM, _forward)

        rc = self._proc.wait()
        self._log.info("SubprocessWorker: subprocess exited rc=%d", rc)
        if rc != 0:
            sys.exit(rc)  # 非ゼロ終了で Process.exitcode を汚染 → Supervisor が crash 検知

    def healthcheck(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None
