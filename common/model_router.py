"""Model Router — Haiku/Sonnet/Opus の自動エスカレーション

コスト配分 + 品質確保の両立。
- Haiku: 定型・低コスト（Daily AAR基本提案等）
- Sonnet: 中規模実装・分析
- Opus: 深い洞察・原因分析・戦略判断・Red Team

エスカレーション条件に達したら自動でOpus呼び出し。
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Optional

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# モデル識別子（最新・公式）
MODEL_HAIKU = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS = "claude-opus-4-6"


@dataclass
class EscalationContext:
    """エスカレーション判定に必要な情報"""
    task_type: str               # 'aar' / 'premortem' / 'tabletop' / 'deviation' / 'peer_review' / 'analyst'
    # 量的シグナル
    pnl_pct: float = 0           # 当日P&L変動率（-0.05 = -5%）
    anomaly_count: int = 0       # 異常事案数
    # 質的シグナル
    is_strategic: bool = False   # 戦略判断含む
    is_destructive: bool = False # 破壊的操作
    is_new_feature: bool = False # 新戦術・新機能
    is_normalization_risk: bool = False  # 常態化リスク
    is_kill_switch_related: bool = False # Kill Switch関連
    # メタ
    user_override: Optional[str] = None  # "opus" / "sonnet" / "haiku"


def select_model(ctx: EscalationContext) -> tuple[str, str]:
    """
    Returns: (model_id, reason)
    """
    # ユーザー明示指定優先
    if ctx.user_override:
        mapping = {"haiku": MODEL_HAIKU, "sonnet": MODEL_SONNET, "opus": MODEL_OPUS}
        if ctx.user_override in mapping:
            return mapping[ctx.user_override], f"user_override={ctx.user_override}"

    # Opus必須条件（品質優先）
    if ctx.is_kill_switch_related:
        return MODEL_OPUS, "kill_switch関連=重要度最大"
    if ctx.is_destructive:
        return MODEL_OPUS, "破壊的操作=慎重判断"

    # task_type別デフォルト + 条件エスカレーション
    if ctx.task_type == "aar":
        if ctx.pnl_pct <= -0.02 or ctx.anomaly_count >= 10 or ctx.is_strategic:
            return MODEL_OPUS, f"AAR重大(pnl={ctx.pnl_pct:.1%}/異常={ctx.anomaly_count})"
        return MODEL_HAIKU, "AAR定型"

    if ctx.task_type == "premortem":
        if ctx.is_new_feature or ctx.is_destructive:
            return MODEL_OPUS, "premortem重要タスク"
        return MODEL_HAIKU, "premortem定型"

    if ctx.task_type == "tabletop":
        if ctx.is_normalization_risk or ctx.anomaly_count >= 5:
            return MODEL_OPUS, "tabletop実データ兆候あり"
        return MODEL_HAIKU, "tabletop定型"

    if ctx.task_type == "deviation":
        if ctx.is_normalization_risk:
            return MODEL_OPUS, "deviation常態化検知"
        return MODEL_HAIKU, "deviation集計のみ"

    if ctx.task_type == "peer_review":
        if ctx.is_strategic or ctx.is_kill_switch_related:
            return MODEL_OPUS, "peer_review戦略判断"
        return MODEL_HAIKU, "peer_review定型"

    if ctx.task_type == "analyst":
        if ctx.is_strategic or ctx.pnl_pct <= -0.03:
            return MODEL_OPUS, "analyst原因分析"
        return MODEL_SONNET, "analyst通常レポート"

    if ctx.task_type == "red_team":
        return MODEL_OPUS, "red_team常時Opus"

    if ctx.task_type == "strategy_selector":
        if ctx.is_normalization_risk or ctx.is_new_feature:
            return MODEL_OPUS, "strategy_selector異常レジーム"
        return MODEL_SONNET, "strategy_selector標準"

    # default
    return MODEL_HAIKU, "default_haiku"


def build_escalation_from_aar(pnl_pct: float, anomaly_count: int,
                              gap_has_strategic: bool = False) -> EscalationContext:
    """AAR用の convenience builder"""
    return EscalationContext(
        task_type="aar",
        pnl_pct=pnl_pct,
        anomaly_count=anomaly_count,
        is_strategic=gap_has_strategic,
    )
