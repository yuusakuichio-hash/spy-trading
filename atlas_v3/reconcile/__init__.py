"""atlas_v3.reconcile — K8s Reconciliation Loop 相当モジュール

desired_state.yaml を読み込み、running state との diff を算出して
idempotent に修復する。

公開 API:
    DriftDetector   — running state と desired state の diff を算出
    Reconciler      — desired state YAML を読み込み・idempotent 修復を実行
    DriftItem       — 個別 diff 項目 dataclass
    ReconcileResult — 修復結果 dataclass
"""
from atlas_v3.reconcile.drift_detector import DriftDetector, DriftItem
from atlas_v3.reconcile.reconciler import ReconcileResult, Reconciler

__all__ = [
    "DriftDetector",
    "DriftItem",
    "ReconcileResult",
    "Reconciler",
]
