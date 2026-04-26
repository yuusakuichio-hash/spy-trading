"""atlas_v3/supervision/supervisor.py — Erlang OTP 風 Supervisor

設計方針
--------
Erlang/OTP の Supervisor behavior を Python multiprocessing で再現する。

Restart strategy
~~~~~~~~~~~~~~~~
- one_for_one   : crash した worker のみ再起動する
- one_for_all   : 1 つでも crash したら全 worker を再起動する
- rest_for_one  : crash した worker + それ以降に登録された worker を再起動する

Intensity limit
~~~~~~~~~~~~~~~
max_restarts 回以上の再起動が within 秒以内に発生したら SupervisorHaltError を
raise して tree 全体を停止し、Pushover 通知を試みる。

Graceful shutdown
~~~~~~~~~~~~~~~~~
stop() は各 worker に SIGTERM を送り shutdown_timeout 秒待つ。
タイムアウトしたら SIGKILL で強制終了。

MTTD 目標
~~~~~~~~~
crash 検知ループは poll_interval (デフォルト 10s) 毎に全 worker の
is_alive() を確認する。MTTD = poll_interval + 再起動処理時間 ≒ 15s 以内。
"""
from __future__ import annotations

import dataclasses
import enum
import logging
import multiprocessing
import multiprocessing.context
import os
import signal
import time
from collections import deque
from typing import Any, Callable, Optional, Type

log = logging.getLogger("atlas.supervision.supervisor")


class RestartStrategy(str, enum.Enum):
    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    REST_FOR_ONE = "rest_for_one"


class SupervisorHaltError(RuntimeError):
    """max_restarts 超過で Supervisor が停止するときに raise される。"""


@dataclasses.dataclass
class ChildSpec:
    """child worker の登録仕様。

    Parameters
    ----------
    name : str
        一意な worker 識別名
    entry_point : type
        WorkerBase のサブクラス（インスタンス化して .main() を呼ぶ）
    kwargs : dict
        entry_point のコンストラクタ引数
    restart : str
        "permanent" (常に再起動) / "transient" (非ゼロ終了のみ再起動) /
        "temporary" (再起動しない)
    shutdown_timeout : float
        graceful shutdown の SIGTERM→SIGKILL タイムアウト秒
    """
    name: str
    entry_point: type
    kwargs: dict = dataclasses.field(default_factory=dict)
    restart: str = "permanent"          # permanent | transient | temporary
    shutdown_timeout: float = 5.0


@dataclasses.dataclass
class _WorkerState:
    """内部管理用 worker 実行状態。"""
    spec: ChildSpec
    process: Optional[multiprocessing.Process] = None
    restart_times: deque = dataclasses.field(default_factory=lambda: deque(maxlen=100))


def _worker_run(entry_point: type, kwargs: dict) -> None:
    """multiprocessing.Process.target — worker を instantiate して main() を呼ぶ。"""
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    worker = entry_point(**kwargs)
    worker.on_start()
    try:
        worker.main()
    finally:
        worker.on_stop()


class Supervisor:
    """Erlang OTP 風 Supervisor。

    Parameters
    ----------
    strategy : RestartStrategy
        再起動戦略
    max_restarts : int
        intensity 制限: within 秒内でこの回数を超えたら tree 停止
    within : float
        intensity 計測ウィンドウ秒
    poll_interval : float
        crash 検知ポーリング間隔秒（MTTD に直結）
    alert_fn : callable | None
        tree 停止時に呼ぶアラート関数。None のとき Pushover を試みる。
    """

    def __init__(
        self,
        strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE,
        max_restarts: int = 3,
        within: float = 60.0,
        poll_interval: float = 10.0,
        alert_fn: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.strategy = strategy
        self.max_restarts = max_restarts
        self.within = within
        self.poll_interval = poll_interval
        self.alert_fn = alert_fn or self._default_alert

        self._children: list[_WorkerState] = []
        self._running: bool = False
        # tree 全体の restart カウント (intensity 計測用)
        self._global_restart_times: deque = deque(maxlen=1000)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_child(self, spec: ChildSpec) -> None:
        """child spec を登録する。start() 前に呼ぶこと。"""
        if any(s.spec.name == spec.name for s in self._children):
            raise ValueError(f"ChildSpec name '{spec.name}' is already registered")
        self._children.append(_WorkerState(spec=spec))

    def start(self) -> None:
        """全 child を起動してポーリングループを開始する。

        このメソッドはブロッキング。Supervisor を別スレッド/プロセスで動かす場合は
        threading.Thread(target=supervisor.start).start() で包む。
        """
        self._running = True
        log.info("Supervisor starting: strategy=%s max_restarts=%d within=%.0fs",
                 self.strategy, self.max_restarts, self.within)

        for state in self._children:
            self._spawn(state)

        self._run_loop()

    def stop(self) -> None:
        """全 child に SIGTERM を送り graceful shutdown する。"""
        self._running = False
        log.info("Supervisor stop requested")
        for state in self._children:
            self._terminate_worker(state)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """クラッシュ検知 + 再起動ポーリングループ。"""
        while self._running:
            time.sleep(self.poll_interval)
            if not self._running:
                break

            crashed: list[_WorkerState] = [
                s for s in self._children
                if s.process is not None and not s.process.is_alive()
            ]
            if not crashed:
                continue

            for state in crashed:
                rc = state.process.exitcode if state.process else None
                log.warning(
                    "Worker '%s' crashed (exitcode=%s)", state.spec.name, rc
                )
                self._handle_crash(state)

    def _handle_crash(self, crashed_state: _WorkerState) -> None:
        """戦略に応じて再起動対象を決定して実行する。

        restart policy チェックを行い、再起動不要と判断した場合は早期リターン。
        """
        proc = crashed_state.process
        rc = proc.exitcode if proc is not None else None

        # restart policy
        if crashed_state.spec.restart == "temporary":
            log.info("Worker '%s' is temporary — not restarting", crashed_state.spec.name)
            return
        if crashed_state.spec.restart == "transient" and rc == 0:
            log.info("Worker '%s' exited cleanly (transient) — not restarting", crashed_state.spec.name)
            return

        now = time.monotonic()
        self._global_restart_times.append(now)

        # intensity check
        recent = [t for t in self._global_restart_times if now - t <= self.within]
        if len(recent) > self.max_restarts:
            msg = (
                f"Supervisor halt: max_restarts={self.max_restarts} exceeded "
                f"within {self.within}s (recent={len(recent)})"
            )
            log.critical(msg)
            self._running = False
            self.alert_fn("[SYS] Supervisor HALT", msg)
            self._stop_all()
            raise SupervisorHaltError(msg)

        if self.strategy == RestartStrategy.ONE_FOR_ONE:
            targets = [crashed_state]
        elif self.strategy == RestartStrategy.ONE_FOR_ALL:
            targets = list(self._children)
        elif self.strategy == RestartStrategy.REST_FOR_ONE:
            idx = self._children.index(crashed_state)
            targets = self._children[idx:]
        else:
            targets = [crashed_state]

        for state in targets:
            self._terminate_worker(state)  # 念のため既存プロセスを停止
            self._spawn(state)

    def _spawn(self, state: _WorkerState) -> None:
        """worker プロセスを起動する。"""
        spec = state.spec
        proc = multiprocessing.Process(
            target=_worker_run,
            args=(spec.entry_point, spec.kwargs),
            name=spec.name,
            daemon=True,
        )
        proc.start()
        state.process = proc
        log.info("Worker '%s' started (pid=%d)", spec.name, proc.pid or -1)

    def _terminate_worker(self, state: _WorkerState) -> None:
        """worker に SIGTERM を送り shutdown_timeout 後に SIGKILL。"""
        proc = state.process
        if proc is None or not proc.is_alive():
            return
        timeout = state.spec.shutdown_timeout
        log.info("Terminating worker '%s' (pid=%d, timeout=%.1fs)",
                 state.spec.name, proc.pid or -1, timeout)
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        proc.join(timeout=timeout)
        if proc.is_alive():
            log.warning("Worker '%s' did not stop after %.1fs — SIGKILL", state.spec.name, timeout)
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            proc.join(timeout=2.0)

    def _stop_all(self) -> None:
        """全 worker を強制停止する（halt 時に呼ばれる）。"""
        for state in self._children:
            self._terminate_worker(state)

    @staticmethod
    def _default_alert(title: str, msg: str) -> None:
        """デフォルトアラート: Pushover を試みてログにフォールバック。"""
        log.critical("ALERT: %s | %s", title, msg)
        try:
            from common.pushover_client import send as _pushover  # type: ignore[import]
            _pushover(title, msg, priority=1)
        except Exception as exc:  # noqa: BLE001
            log.error("Pushover send failed: %s", exc)

    # ------------------------------------------------------------------
    # Convenience: context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "Supervisor":
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()
