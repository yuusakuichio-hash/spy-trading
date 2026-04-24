"""atlas_v3/supervision/tree_config.py — Atlas Supervisor Tree 宣言的 config

登録 worker
-----------
1. atlas-trader   : 本番 spy_bot.py を subprocess で包む SubprocessWorker
2. atlas-paper    : ペーパー spy_bot.py を subprocess で包む SubprocessWorker
3. moomoo-opend-relogin : moomoo OpenD preemptive relogin スクリプトを包む worker

設計方針
--------
- spy_bot.py は atlas_v3/bots/main.py と同じ subprocess 境界パターンで worker 化する。
- 各 worker は SubprocessWorker を使い、entry_point の cmd に実行コマンドを渡す。
- ATLAS_TREE は Supervisor インスタンスを返す factory 関数。
  テストや本番起動は ATLAS_TREE() を呼んで supervisor.start() する。
- moomoo-opend-relogin は restart="transient" (正常終了は再起動しない)。
  launchd com.soralab.moomoo-opend-relogin と役割が被るが、
  Supervisor 直下でも管理することで MTTD を短縮する。
"""
from __future__ import annotations

import pathlib
import sys

from atlas_v3.supervision.supervisor import ChildSpec, RestartStrategy, Supervisor
from atlas_v3.supervision.worker import SubprocessWorker

_TRADING_ROOT = pathlib.Path(__file__).resolve().parents[2]  # atlas_v3/supervision → trading/
_SPY_BOT = _TRADING_ROOT / "spy_bot.py"
_RELOGIN_SCRIPT = _TRADING_ROOT / "scripts" / "moomoo_opend_relogin.py"


def ATLAS_TREE(
    strategy: RestartStrategy = RestartStrategy.ONE_FOR_ONE,
    max_restarts: int = 3,
    within: float = 60.0,
    poll_interval: float = 10.0,
    enable_live: bool = False,
) -> Supervisor:
    """Atlas Supervisor Tree インスタンスを生成して返す。

    Parameters
    ----------
    strategy : RestartStrategy
        再起動戦略 (デフォルト: one_for_one)
    max_restarts : int
        intensity 制限 (デフォルト: 3)
    within : float
        intensity ウィンドウ秒 (デフォルト: 60)
    poll_interval : float
        crash 検知ポーリング間隔秒 (デフォルト: 10)
    enable_live : bool
        True のとき atlas-trader (本番モード) を登録する。
        False のとき atlas-paper のみ登録する（デフォルト・安全側）。

    Returns
    -------
    Supervisor
        start() を呼ぶと全 worker が起動する。
    """
    sup = Supervisor(
        strategy=strategy,
        max_restarts=max_restarts,
        within=within,
        poll_interval=poll_interval,
    )

    # ── atlas-paper (常時登録) ──────────────────────────────────────────────
    sup.add_child(ChildSpec(
        name="atlas-paper",
        entry_point=SubprocessWorker,
        kwargs={
            "name": "atlas-paper",
            "cmd": [sys.executable, str(_SPY_BOT), "--paper"],
            "cwd": str(_TRADING_ROOT),
        },
        restart="permanent",
        shutdown_timeout=10.0,
    ))

    # ── atlas-trader (本番モード・明示有効化時のみ) ────────────────────────
    if enable_live:
        sup.add_child(ChildSpec(
            name="atlas-trader",
            entry_point=SubprocessWorker,
            kwargs={
                "name": "atlas-trader",
                "cmd": [sys.executable, str(_SPY_BOT)],
                "cwd": str(_TRADING_ROOT),
            },
            restart="permanent",
            shutdown_timeout=10.0,
        ))

    # ── moomoo-opend-relogin (transient: 正常終了は再起動しない) ───────────
    sup.add_child(ChildSpec(
        name="moomoo-opend-relogin",
        entry_point=SubprocessWorker,
        kwargs={
            "name": "moomoo-opend-relogin",
            "cmd": [sys.executable, str(_RELOGIN_SCRIPT)],
            "cwd": str(_TRADING_ROOT),
        },
        restart="transient",
        shutdown_timeout=5.0,
    ))

    return sup
