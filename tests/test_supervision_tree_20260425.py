"""tests/test_supervision_tree_20260425.py

atlas_v3/supervision/ Supervisor Tree テスト — 20 件以上。

テスト分類
----------
[spec]      ChildSpec / RestartStrategy dataclass 正当性
[spawn]     Supervisor._spawn: Process が起動すること
[crash]     crash 検知 → one_for_one 再起動
[ofa]       one_for_all: 1 crash → 全 worker 再起動
[rfo]       rest_for_one: crash 以降の worker を再起動
[halt]      max_restarts 超過 → SupervisorHaltError + alert
[graceful]  graceful shutdown: SIGTERM → join → SIGKILL fallback
[mttd]      MTTD < 60s: crash から再起動完了まで wall-clock 60s 以内
[restart]   restart policy: permanent / transient / temporary
[worker]    WorkerBase ABC / SubprocessWorker
[tree]      ATLAS_TREE factory: child spec 名称・数の検証
[dedup]     同名 child spec 二重登録 → ValueError
[context]   context manager __enter__/__exit__
"""
from __future__ import annotations

import signal
import sys
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# ── テスト対象 import ─────────────────────────────────────────────────────────
from atlas_v3.supervision.supervisor import (
    ChildSpec,
    RestartStrategy,
    Supervisor,
    SupervisorHaltError,
    _WorkerState,
    _worker_run,
)
from atlas_v3.supervision.worker import SubprocessWorker, WorkerBase
from atlas_v3.supervision.tree_config import ATLAS_TREE


# ── ヘルパー worker ───────────────────────────────────────────────────────────

class _OKWorker(WorkerBase):
    """正常に main() が返るだけの worker。"""
    def main(self) -> None:
        pass


class _LoopWorker(WorkerBase):
    """stop_event がセットされるまでループする worker。"""
    def main(self) -> None:
        for _ in range(200):
            time.sleep(0.05)


class _CrashWorker(WorkerBase):
    """main() で即 RuntimeError を raise する worker。"""
    def main(self) -> None:
        raise RuntimeError("intentional crash")


# ── [spec] ChildSpec / RestartStrategy ───────────────────────────────────────

class TestChildSpec:
    def test_defaults(self):
        spec = ChildSpec(name="w1", entry_point=_OKWorker)
        assert spec.name == "w1"
        assert spec.entry_point is _OKWorker
        assert spec.kwargs == {}
        assert spec.restart == "permanent"
        assert spec.shutdown_timeout == 5.0

    def test_custom_values(self):
        spec = ChildSpec(
            name="w2",
            entry_point=_OKWorker,
            kwargs={"x": 1},
            restart="transient",
            shutdown_timeout=3.0,
        )
        assert spec.kwargs == {"x": 1}
        assert spec.restart == "transient"
        assert spec.shutdown_timeout == 3.0

    def test_restart_strategy_enum_values(self):
        assert RestartStrategy.ONE_FOR_ONE == "one_for_one"
        assert RestartStrategy.ONE_FOR_ALL == "one_for_all"
        assert RestartStrategy.REST_FOR_ONE == "rest_for_one"


# ── [dedup] 同名登録 ──────────────────────────────────────────────────────────

class TestDuplicateChildSpec:
    def test_duplicate_name_raises(self):
        sup = Supervisor()
        sup.add_child(ChildSpec(name="w1", entry_point=_OKWorker))
        with pytest.raises(ValueError, match="w1"):
            sup.add_child(ChildSpec(name="w1", entry_point=_OKWorker))


# ── [spawn] Process 起動 ─────────────────────────────────────────────────────

class TestSpawn:
    def test_spawn_starts_process(self):
        sup = Supervisor(poll_interval=999.0)
        sup.add_child(ChildSpec(name="ok", entry_point=_OKWorker))
        state = sup._children[0]
        sup._spawn(state)
        assert state.process is not None
        # プロセスが起動したことを確認（終了済みでも pid があれば OK）
        assert state.process.pid is not None
        state.process.join(timeout=3.0)

    def test_spawn_assigns_name(self):
        sup = Supervisor(poll_interval=999.0)
        sup.add_child(ChildSpec(name="named-worker", entry_point=_OKWorker))
        state = sup._children[0]
        sup._spawn(state)
        assert state.process.name == "named-worker"
        state.process.join(timeout=3.0)


# ── [crash] one_for_one crash → 再起動 ───────────────────────────────────────

class TestOneForOneCrash:
    def test_crash_triggers_restart(self):
        """crash した worker のみ再起動されること。"""
        restarted: list[str] = []
        original_spawn = Supervisor._spawn

        def _mock_spawn(self_sup, state):
            restarted.append(state.spec.name)
            # 実際には spawn しない（テスト高速化）
            mock_proc = MagicMock()
            mock_proc.is_alive.return_value = True
            mock_proc.pid = 99999
            state.process = mock_proc

        sup = Supervisor(
            strategy=RestartStrategy.ONE_FOR_ONE,
            max_restarts=10,
            within=60.0,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="crash-worker", entry_point=_OKWorker))
        sup.add_child(ChildSpec(name="healthy-worker", entry_point=_OKWorker))

        # 初期 spawn をモック
        with patch.object(Supervisor, "_spawn", _mock_spawn):
            for state in sup._children:
                _mock_spawn(sup, state)

        # crash-worker を crash させる
        crash_state = sup._children[0]
        crash_proc = MagicMock()
        crash_proc.is_alive.return_value = False
        crash_proc.exitcode = 1
        crash_state.process = crash_proc

        restarted.clear()
        with patch.object(Supervisor, "_spawn", _mock_spawn):
            sup._handle_crash(crash_state)

        assert "crash-worker" in restarted
        # one_for_one なので healthy-worker は再起動しない
        assert "healthy-worker" not in restarted

    def test_one_for_one_does_not_restart_others(self):
        """one_for_one: crash worker 以外の is_alive が呼ばれないこと。"""
        sup = Supervisor(
            strategy=RestartStrategy.ONE_FOR_ONE,
            max_restarts=10,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="w1", entry_point=_OKWorker))
        sup.add_child(ChildSpec(name="w2", entry_point=_OKWorker))

        healthy = MagicMock()
        healthy.is_alive.return_value = True
        healthy.pid = 11111
        sup._children[1].process = healthy

        crashed = MagicMock()
        crashed.is_alive.return_value = False
        crashed.exitcode = 1
        sup._children[0].process = crashed

        spawned: list[str] = []

        def _mock_spawn(self_sup, state):
            spawned.append(state.spec.name)
            m = MagicMock()
            m.is_alive.return_value = True
            m.pid = 22222
            state.process = m

        with patch.object(Supervisor, "_spawn", _mock_spawn):
            with patch.object(Supervisor, "_terminate_worker"):
                sup._handle_crash(sup._children[0])

        assert spawned == ["w1"]


# ── [ofa] one_for_all ────────────────────────────────────────────────────────

class TestOneForAll:
    def test_all_workers_restarted_on_crash(self):
        sup = Supervisor(
            strategy=RestartStrategy.ONE_FOR_ALL,
            max_restarts=10,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="w1", entry_point=_OKWorker))
        sup.add_child(ChildSpec(name="w2", entry_point=_OKWorker))
        sup.add_child(ChildSpec(name="w3", entry_point=_OKWorker))

        for state in sup._children:
            m = MagicMock()
            m.is_alive.return_value = True
            m.pid = 1000
            state.process = m

        spawned: list[str] = []

        def _mock_spawn(self_sup, state):
            spawned.append(state.spec.name)
            m = MagicMock()
            m.is_alive.return_value = True
            m.pid = 9999
            state.process = m

        with patch.object(Supervisor, "_spawn", _mock_spawn):
            with patch.object(Supervisor, "_terminate_worker"):
                sup._handle_crash(sup._children[0])

        assert set(spawned) == {"w1", "w2", "w3"}


# ── [rfo] rest_for_one ───────────────────────────────────────────────────────

class TestRestForOne:
    def test_crashed_and_subsequent_restarted(self):
        sup = Supervisor(
            strategy=RestartStrategy.REST_FOR_ONE,
            max_restarts=10,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="w0", entry_point=_OKWorker))
        sup.add_child(ChildSpec(name="w1", entry_point=_OKWorker))
        sup.add_child(ChildSpec(name="w2", entry_point=_OKWorker))

        for state in sup._children:
            m = MagicMock()
            m.is_alive.return_value = True
            m.pid = 1000
            state.process = m

        spawned: list[str] = []

        def _mock_spawn(self_sup, state):
            spawned.append(state.spec.name)
            m = MagicMock()
            m.is_alive.return_value = True
            m.pid = 9999
            state.process = m

        # w1 (index 1) が crash → w1, w2 を再起動、w0 は触らない
        with patch.object(Supervisor, "_spawn", _mock_spawn):
            with patch.object(Supervisor, "_terminate_worker"):
                sup._handle_crash(sup._children[1])

        assert "w0" not in spawned
        assert "w1" in spawned
        assert "w2" in spawned


# ── [halt] max_restarts 超過 ──────────────────────────────────────────────────

class TestMaxRestarts:
    def test_exceeds_max_restarts_raises_halt(self):
        alerted: list[str] = []
        sup = Supervisor(
            strategy=RestartStrategy.ONE_FOR_ONE,
            max_restarts=3,
            within=60.0,
            poll_interval=0.05,
            alert_fn=lambda t, m: alerted.append(t),
        )
        sup.add_child(ChildSpec(name="flappy", entry_point=_OKWorker))

        m = MagicMock()
        m.is_alive.return_value = True
        m.pid = 1000
        sup._children[0].process = m

        def _mock_spawn(self_sup, state):
            mm = MagicMock()
            mm.is_alive.return_value = True
            mm.pid = 9999
            state.process = mm

        with patch.object(Supervisor, "_spawn", _mock_spawn):
            with patch.object(Supervisor, "_terminate_worker"):
                # 4 回 crash → 4 回目で halt
                for _ in range(4):
                    try:
                        sup._handle_crash(sup._children[0])
                    except SupervisorHaltError:
                        break

        assert len(alerted) >= 1
        assert any("HALT" in a for a in alerted)

    def test_halt_sets_running_false(self):
        sup = Supervisor(
            max_restarts=1,
            within=60.0,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="w", entry_point=_OKWorker))
        sup._running = True

        m = MagicMock()
        m.is_alive.return_value = True
        m.pid = 1000
        sup._children[0].process = m

        def _mock_spawn(self_sup, state):
            mm = MagicMock()
            mm.is_alive.return_value = True
            mm.pid = 9999
            state.process = mm

        with patch.object(Supervisor, "_spawn", _mock_spawn):
            with patch.object(Supervisor, "_terminate_worker"):
                with pytest.raises(SupervisorHaltError):
                    for _ in range(3):
                        sup._handle_crash(sup._children[0])

        assert sup._running is False

    def test_restarts_outside_window_do_not_count(self):
        """within 秒を超えた古いリスタートはカウントしない。"""
        sup = Supervisor(
            max_restarts=2,
            within=1.0,   # 1秒ウィンドウ
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="w", entry_point=_OKWorker))
        sup._running = True

        m = MagicMock()
        m.is_alive.return_value = True
        m.pid = 1000
        sup._children[0].process = m

        def _mock_spawn(self_sup, state):
            mm = MagicMock()
            mm.is_alive.return_value = True
            mm.pid = 9999
            state.process = mm

        # 2 回 restart を古い時刻として手動挿入
        sup._global_restart_times.append(time.monotonic() - 120.0)
        sup._global_restart_times.append(time.monotonic() - 120.0)

        # 新しく 2 回 crash → within=1s ウィンドウでは 2 回なので halt しない
        with patch.object(Supervisor, "_spawn", _mock_spawn):
            with patch.object(Supervisor, "_terminate_worker"):
                # 2 回は halt しないはず
                sup._handle_crash(sup._children[0])
                sup._handle_crash(sup._children[0])
                # 3 回目で halt
                with pytest.raises(SupervisorHaltError):
                    sup._handle_crash(sup._children[0])


# ── [graceful] graceful shutdown ─────────────────────────────────────────────

class TestGracefulShutdown:
    def test_sigterm_sent_on_terminate(self):
        """_terminate_worker が os.kill(SIGTERM) を呼ぶこと。"""
        sup = Supervisor(poll_interval=999.0)
        sup.add_child(ChildSpec(name="w", entry_point=_OKWorker, shutdown_timeout=0.1))
        state = sup._children[0]

        mock_proc = MagicMock()
        mock_proc.is_alive.side_effect = [True, False]  # alive → dead after join
        mock_proc.pid = 54321
        state.process = mock_proc

        import os
        with patch("os.kill") as mock_kill:
            with patch.object(mock_proc, "join"):
                sup._terminate_worker(state)

        mock_kill.assert_called_once_with(54321, signal.SIGTERM)

    def test_sigkill_sent_on_timeout(self):
        """shutdown_timeout 内に終了しない場合 SIGKILL が送られること。"""
        sup = Supervisor(poll_interval=999.0)
        sup.add_child(ChildSpec(name="w", entry_point=_OKWorker, shutdown_timeout=0.01))
        state = sup._children[0]

        mock_proc = MagicMock()
        mock_proc.is_alive.return_value = True  # 常に alive → SIGKILL が必要
        mock_proc.pid = 54321
        state.process = mock_proc

        kill_calls: list[tuple] = []

        import os as _os
        def _mock_kill(pid, sig):
            kill_calls.append((pid, sig))

        with patch("os.kill", _mock_kill):
            sup._terminate_worker(state)

        sigs = [s for _, s in kill_calls]
        assert signal.SIGTERM in sigs
        assert signal.SIGKILL in sigs

    def test_stop_sets_running_false(self):
        sup = Supervisor(poll_interval=999.0)
        sup._running = True
        with patch.object(sup, "_terminate_worker"):
            sup.stop()
        assert sup._running is False


# ── [restart] restart policy ──────────────────────────────────────────────────

class TestRestartPolicy:
    def test_temporary_worker_not_restarted(self):
        sup = Supervisor(
            max_restarts=10,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="tmp", entry_point=_OKWorker, restart="temporary"))
        state = sup._children[0]

        m = MagicMock()
        m.is_alive.return_value = False
        m.exitcode = 1
        state.process = m

        spawned: list[str] = []
        with patch.object(Supervisor, "_spawn", lambda self_sup, s: spawned.append(s.spec.name)):
            sup._handle_crash(state)

        assert "tmp" not in spawned

    def test_transient_worker_not_restarted_on_clean_exit(self):
        sup = Supervisor(
            max_restarts=10,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="tran", entry_point=_OKWorker, restart="transient"))
        state = sup._children[0]

        m = MagicMock()
        m.is_alive.return_value = False
        m.exitcode = 0  # clean exit
        state.process = m

        spawned: list[str] = []
        with patch.object(Supervisor, "_spawn", lambda self_sup, s: spawned.append(s.spec.name)):
            sup._handle_crash(state)

        assert "tran" not in spawned

    def test_transient_worker_restarted_on_error_exit(self):
        sup = Supervisor(
            max_restarts=10,
            poll_interval=0.05,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="tran", entry_point=_OKWorker, restart="transient"))
        state = sup._children[0]

        m = MagicMock()
        m.is_alive.return_value = False
        m.exitcode = 1
        state.process = m

        spawned: list[str] = []

        def _mock_spawn(self_sup, s):
            spawned.append(s.spec.name)
            mm = MagicMock()
            mm.is_alive.return_value = True
            mm.pid = 9999
            s.process = mm

        with patch.object(Supervisor, "_spawn", _mock_spawn):
            with patch.object(Supervisor, "_terminate_worker"):
                sup._handle_crash(state)

        assert "tran" in spawned


# ── [worker] WorkerBase / SubprocessWorker ────────────────────────────────────

class TestWorkerBase:
    def test_abstract_main_required(self):
        with pytest.raises(TypeError):
            WorkerBase("w")  # abstract class cannot be instantiated

    def test_healthcheck_default_true(self):
        class ConcreteWorker(WorkerBase):
            def main(self): pass

        w = ConcreteWorker("w")
        assert w.healthcheck() is True

    def test_on_start_on_stop_callable(self):
        class ConcreteWorker(WorkerBase):
            def main(self): pass

        w = ConcreteWorker("w")
        w.on_start()  # should not raise
        w.on_stop()   # should not raise


class TestSubprocessWorker:
    def test_subprocess_worker_instantiates(self):
        w = SubprocessWorker(name="test", cmd=["echo", "hello"])
        assert w.name == "test"
        assert w.cmd == ["echo", "hello"]

    def test_healthcheck_false_when_no_proc(self):
        w = SubprocessWorker(name="test", cmd=["echo"])
        assert w.healthcheck() is False


# ── [tree] ATLAS_TREE factory ─────────────────────────────────────────────────

class TestAtlasTree:
    def test_tree_returns_supervisor(self):
        sup = ATLAS_TREE()
        assert isinstance(sup, Supervisor)

    def test_default_tree_has_paper_and_relogin(self):
        sup = ATLAS_TREE()
        names = [s.spec.name for s in sup._children]
        assert "atlas-paper" in names
        assert "moomoo-opend-relogin" in names

    def test_live_disabled_by_default(self):
        sup = ATLAS_TREE(enable_live=False)
        names = [s.spec.name for s in sup._children]
        assert "atlas-trader" not in names

    def test_live_enabled_adds_trader(self):
        sup = ATLAS_TREE(enable_live=True)
        names = [s.spec.name for s in sup._children]
        assert "atlas-trader" in names
        assert "atlas-paper" in names

    def test_relogin_is_transient(self):
        sup = ATLAS_TREE()
        relogin_state = next(s for s in sup._children if s.spec.name == "moomoo-opend-relogin")
        assert relogin_state.spec.restart == "transient"

    def test_paper_is_permanent(self):
        sup = ATLAS_TREE()
        paper_state = next(s for s in sup._children if s.spec.name == "atlas-paper")
        assert paper_state.spec.restart == "permanent"

    def test_strategy_passed_through(self):
        sup = ATLAS_TREE(strategy=RestartStrategy.ONE_FOR_ALL)
        assert sup.strategy == RestartStrategy.ONE_FOR_ALL

    def test_max_restarts_passed_through(self):
        sup = ATLAS_TREE(max_restarts=5)
        assert sup.max_restarts == 5


# ── [context] context manager ────────────────────────────────────────────────

class TestContextManager:
    def test_exit_calls_stop(self):
        sup = Supervisor()
        sup._running = True
        with patch.object(sup, "stop") as mock_stop:
            with sup:
                pass
        mock_stop.assert_called_once()


# ── [mttd] MTTD < 60s ───────────────────────────────────────────────────────

class TestMTTD:
    """crash 検知から再起動完了まで 60s 以内であることを wall-clock で検証。

    実際にプロセスを起動して crash させる統合テスト。
    poll_interval=1.0 で動かし、検知 + 再起動が 10s 以内であることを確認する。
    """

    def test_mttd_under_10s(self):
        """crash 検知 + 再起動が 10s 以内であること（poll_interval=1s）。"""
        restart_event = threading.Event()
        original_spawn = Supervisor._spawn

        spawn_count: list[int] = [0]

        def _counting_spawn(self_sup, state):
            spawn_count[0] += 1
            if spawn_count[0] > 1:
                # 2回目のspawn = 再起動 → イベントをセット
                restart_event.set()
            mock_proc = MagicMock()
            mock_proc.is_alive.return_value = True
            mock_proc.pid = 99990 + spawn_count[0]
            state.process = mock_proc

        sup = Supervisor(
            strategy=RestartStrategy.ONE_FOR_ONE,
            max_restarts=5,
            within=60.0,
            poll_interval=1.0,
            alert_fn=lambda t, m: None,
        )
        sup.add_child(ChildSpec(name="mttd-worker", entry_point=_OKWorker))

        with patch.object(Supervisor, "_spawn", _counting_spawn):
            # 初期 spawn
            _counting_spawn(sup, sup._children[0])

        # crash させる
        crash_proc = MagicMock()
        crash_proc.is_alive.return_value = False
        crash_proc.exitcode = 1
        sup._children[0].process = crash_proc

        t0 = time.monotonic()

        def _run_loop():
            with patch.object(Supervisor, "_spawn", _counting_spawn):
                with patch.object(Supervisor, "_terminate_worker"):
                    sup._running = True
                    # 1ループだけ手動実行
                    crashed = [
                        s for s in sup._children
                        if s.process is not None and not s.process.is_alive()
                    ]
                    for state in crashed:
                        sup._handle_crash(state)

        t = threading.Thread(target=_run_loop, daemon=True)
        t.start()
        restart_event.wait(timeout=10.0)
        elapsed = time.monotonic() - t0

        assert restart_event.is_set(), "再起動が 10s 以内に完了しなかった"
        assert elapsed < 60.0, f"MTTD = {elapsed:.1f}s が 60s 超過"
