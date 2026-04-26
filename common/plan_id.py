"""common/plan_id.py — Chronos plan_id 正規化モジュール

H-3修正: plan_id 命名が YAML / Kelly / Tactic / runtime の4系統でバラバラだった問題を解消する。
全系統が同一の PlanID enum を参照し、文字列変換は to_str() / from_str() で一元管理する。

命名規則:
  <plan_short>_<phase_suffix>
  例: flex_eval, rapid_sim, pro_funded, apex_safety_net

正規マッピング:
  firm "mffu" + plan "flex_50k"  + phase "evaluation"  → "flex_eval"
  firm "mffu" + plan "rapid_50k" + phase "sim_funded"   → "rapid_sim"
  firm "apex" + plan "apex_pa"   + phase "pa"           → "apex_safety_net"
"""
from __future__ import annotations

from enum import Enum, unique
import logging

log = logging.getLogger(__name__)


@unique
class PlanID(str, Enum):
    """Chronos 全プランの正規 ID 列挙。

    値は Kelly / Tactic / state.json で共通して使用する文字列。
    DEPRECATED プランは末尾に _DEPRECATED を付ける（Kelly fail-closed 対象）。
    """
    # ── MFFU ─────────────────────────────────────────────────────────────────
    FLEX_EVAL       = "flex_eval"
    FLEX_SIM        = "flex_sim"
    RAPID_EVAL      = "rapid_eval"
    RAPID_SIM       = "rapid_sim"
    PRO_EVAL        = "pro_eval"
    PRO_SIM         = "pro_sim"
    BUILDER_EVAL    = "builder_eval"
    BUILDER_FUNDED  = "builder_funded"
    # ── Tradeify ─────────────────────────────────────────────────────────────
    TRADEIFY_EVAL   = "tradeify_eval"
    TRADEIFY_FUNDED = "tradeify_funded"
    # ── Apex ─────────────────────────────────────────────────────────────────
    APEX_SAFETY_NET = "apex_safety_net"
    APEX_POST_PAYOUT= "apex_post_payout"
    # ── DEPRECATED ───────────────────────────────────────────────────────────
    # core_50k は廃止。Kelly fail-closed 対象。
    CORE_50K_DEPRECATED = "core_50k"


# ── YAML plan key → PlanID ────────────────────────────────────────────────────
# chronos_rules.yaml の prop_firm.plan に書かれる値を PlanID に変換するマッピング。
# phase は ChronosBot._phase_for_prop を参照して決定する。

_YAML_PLAN_PHASE_TO_PLAN_ID: dict[tuple[str, str], PlanID] = {
    # (yaml_plan, phase) → PlanID
    ("flex_50k",    "evaluation"):  PlanID.FLEX_EVAL,
    ("flex_50k",    "sim_funded"):  PlanID.FLEX_SIM,
    ("flex_100k",   "evaluation"):  PlanID.FLEX_EVAL,
    ("flex_100k",   "sim_funded"):  PlanID.FLEX_SIM,
    ("rapid_50k",   "evaluation"):  PlanID.RAPID_EVAL,
    ("rapid_50k",   "sim_funded"):  PlanID.RAPID_SIM,
    ("rapid_25k",   "evaluation"):  PlanID.RAPID_EVAL,
    ("rapid_25k",   "sim_funded"):  PlanID.RAPID_SIM,
    ("pro_50k",     "evaluation"):  PlanID.PRO_EVAL,
    ("pro_50k",     "sim_funded"):  PlanID.PRO_SIM,
    ("pro_100k",    "evaluation"):  PlanID.PRO_EVAL,
    ("pro_100k",    "sim_funded"):  PlanID.PRO_SIM,
    ("builder_25k", "evaluation"):  PlanID.BUILDER_EVAL,
    ("builder_25k", "funded"):      PlanID.BUILDER_FUNDED,
    ("builder_50k", "evaluation"):  PlanID.BUILDER_EVAL,
    ("builder_50k", "funded"):      PlanID.BUILDER_FUNDED,
    ("tradeify",    "evaluation"):  PlanID.TRADEIFY_EVAL,
    ("tradeify",    "funded"):      PlanID.TRADEIFY_FUNDED,
    ("apex",        "pa"):          PlanID.APEX_SAFETY_NET,
    ("apex",        "funded"):      PlanID.APEX_POST_PAYOUT,
    # DEPRECATED
    ("core_50k",    "evaluation"):  PlanID.CORE_50K_DEPRECATED,
    ("core_50k",    "funded"):      PlanID.CORE_50K_DEPRECATED,
}

# ChronosBot._plan_id プロパティが生成する文字列 → PlanID（前方互換）
_STR_TO_PLAN_ID: dict[str, PlanID] = {p.value: p for p in PlanID}

# DEPRECATED セット（Kelly fail-closed 対象）
DEPRECATED_PLAN_IDS: frozenset[str] = frozenset({PlanID.CORE_50K_DEPRECATED.value})


def from_yaml_plan_phase(yaml_plan: str, phase: str) -> PlanID:
    """YAML plan + phase から PlanID を返す。

    Args:
        yaml_plan: chronos_rules.yaml の prop_firm.plan 値（例: "flex_50k"）
        phase:     ChronosBot._phase_for_prop の値（例: "evaluation"）

    Returns:
        PlanID

    Raises:
        ValueError: β-6 fail-closed — 未知の組み合わせはフォールバックせず即 raise。
                    意図しない設定で取引が継続するのを防ぐ。
    """
    key = (yaml_plan.lower(), phase.lower())
    result = _YAML_PLAN_PHASE_TO_PLAN_ID.get(key)
    if result is None:
        raise ValueError(
            f"[PlanID] from_yaml_plan_phase: 未知の組み合わせ yaml_plan={yaml_plan!r} "
            f"phase={phase!r} — fail-closed (β-6). "
            f"有効な組み合わせ: {list(_YAML_PLAN_PHASE_TO_PLAN_ID.keys())}"
        )
    return result


def from_str(plan_id_str: str) -> PlanID:
    """文字列 plan_id から PlanID を返す。

    Args:
        plan_id_str: "flex_eval" 等の文字列

    Returns:
        PlanID（未知の文字列は FLEX_EVAL にフォールバックして ERROR ログ）
    """
    if not plan_id_str:
        log.error("[PlanID] from_str: 空文字列 → FLEX_EVAL にフォールバック（H-3）")
        return PlanID.FLEX_EVAL
    result = _STR_TO_PLAN_ID.get(plan_id_str.lower())
    if result is None:
        log.error(
            "[PlanID] from_str: 未知の plan_id=%r → FLEX_EVAL にフォールバック（H-3）",
            plan_id_str,
        )
        return PlanID.FLEX_EVAL
    return result


def is_deprecated(plan_id_str: str) -> bool:
    """plan_id が DEPRECATED プランか判定する。

    DEPRECATED プランは KellySizer の fail-closed 対象。

    Args:
        plan_id_str: 文字列 plan_id

    Returns:
        True = DEPRECATED
    """
    return plan_id_str.lower() in DEPRECATED_PLAN_IDS
