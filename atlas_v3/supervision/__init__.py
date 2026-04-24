"""atlas_v3.supervision — Erlang OTP 風 Supervisor Tree

公開 API:
    Supervisor      — worker プロセス群を監視・再起動する supervisor
    WorkerBase      — supervisor 下で動く worker の ABC
    ChildSpec       — child 登録仕様 dataclass
    RestartStrategy — one_for_one / one_for_all / rest_for_one
    ATLAS_TREE      — atlas-trader / atlas-paper / moomoo-opend-relogin の宣言的 config
"""
from atlas_v3.supervision.supervisor import (
    ChildSpec,
    RestartStrategy,
    Supervisor,
    SupervisorHaltError,
)
from atlas_v3.supervision.worker import WorkerBase
from atlas_v3.supervision.tree_config import ATLAS_TREE

__all__ = [
    "ChildSpec",
    "RestartStrategy",
    "Supervisor",
    "SupervisorHaltError",
    "WorkerBase",
    "ATLAS_TREE",
]
