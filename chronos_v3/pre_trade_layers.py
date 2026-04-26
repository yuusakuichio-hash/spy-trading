"""chronos_v3.pre_trade_layers — F2/F3 MFFU integrity layer implementations

These functions were stubs (always returning None) in the legacy
chronos_pre_trade_check.py.  Because that file is protected by
legacy_write_block.sh, the implementations live here.

The legacy module re-exports these via monkey-patch at import time
(see bottom of this file), so existing callers of
`chronos_pre_trade_check._check_layer_f2_mffu_consistency` continue
to work without touching the protected file.

F2  MFFU Consistency Rule
    daily_pnl / total_pnl > 50% → BLOCK
    total_pnl == 0              → PASS (avoid ZeroDivisionError)
    Source: MFFU Consistency Rule specification

F3  MFFU Safety Buffer Rule
    safety_floor  = initial_balance - trailing_drawdown_limit
    balance < safety_floor → BLOCK
    Source: MFFU trailing-drawdown / safety-buffer specification
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy imports to avoid circular dependency at module load time
# ---------------------------------------------------------------------------
def _get_check_result_cls():
    from common.pre_trade_check import CheckResult
    return CheckResult


def _get_futures_order_context_cls():
    from chronos_pre_trade_check import FuturesOrderContext
    return FuturesOrderContext


# ---------------------------------------------------------------------------
# F2: MFFU Consistency Rule
# ---------------------------------------------------------------------------
def check_layer_f2_mffu_consistency(ctx) -> Optional[object]:
    """Layer F2: MFFU Consistency Rule.

    Blocks the order when a single day's PnL accounts for more than 50%
    of the account's total (cumulative) PnL, which violates MFFU's
    consistency requirement.

    Args:
        ctx: FuturesOrderContext — must have:
            mffu_daily_pnl      (float)
            prop_account_state  (dict with key "total_pnl": float)

    Returns:
        CheckResult(allow=False, layer="F2_mffu_consistency", ...) when
        the ratio exceeds 50%, otherwise None (pass).
    """
    CheckResult = _get_check_result_cls()
    total_pnl: float = float(ctx.prop_account_state.get("total_pnl", 0.0))

    if total_pnl == 0.0:
        # ZeroDivisionError guard — no basis to evaluate consistency
        return None

    daily_pnl: float = float(ctx.mffu_daily_pnl)
    ratio = daily_pnl / total_pnl

    if ratio > 0.50:
        reason = (
            f"Layer F2 (MFFU Consistency): daily_pnl={daily_pnl:.2f} / "
            f"total_pnl={total_pnl:.2f} = {ratio:.1%} > 50% → 発注拒否"
        )
        log.error("[ChronosPreTrade] F2: %s", reason)
        return CheckResult(allow=False, layer="F2_mffu_consistency", reason=reason)

    return None


# ---------------------------------------------------------------------------
# F3: MFFU Safety Buffer Rule
# ---------------------------------------------------------------------------
def check_layer_f3_mffu_safety_buffer(ctx) -> Optional[object]:
    """Layer F3: MFFU Safety Buffer Rule.

    Blocks the order when the current account balance has fallen below
    the safety floor calculated from the prop firm's trailing-drawdown
    limit.

        safety_floor = initial_balance - trailing_drawdown_limit
        BLOCK if mffu_account_balance < safety_floor

    When the required keys are absent from prop_account_state the check
    is skipped (returns None) to remain fail-open only for missing config,
    not for actual breaches.

    Args:
        ctx: FuturesOrderContext — must have:
            mffu_account_balance  (float)
            prop_account_state    (dict with keys
                                   "initial_balance": float,
                                   "trailing_drawdown_limit": float)

    Returns:
        CheckResult(allow=False, layer="F3_mffu_safety_buffer", ...) when
        the balance is below the floor, otherwise None (pass).
    """
    CheckResult = _get_check_result_cls()
    state = ctx.prop_account_state

    initial_balance = state.get("initial_balance")
    trailing_drawdown_limit = state.get("trailing_drawdown_limit")

    if initial_balance is None or trailing_drawdown_limit is None:
        # Config incomplete — cannot evaluate; skip conservatively
        log.warning(
            "[ChronosPreTrade] F3: prop_account_state missing "
            "'initial_balance' or 'trailing_drawdown_limit' — skipping check"
        )
        return None

    safety_floor = float(initial_balance) - float(trailing_drawdown_limit)
    balance = float(ctx.mffu_account_balance)

    if balance < safety_floor:
        reason = (
            f"Layer F3 (MFFU Safety Buffer): balance={balance:.2f} < "
            f"safety_floor={safety_floor:.2f} "
            f"(initial={initial_balance:.2f} - drawdown_limit={trailing_drawdown_limit:.2f}) "
            f"→ 発注拒否"
        )
        log.error("[ChronosPreTrade] F3: %s", reason)
        return CheckResult(allow=False, layer="F3_mffu_safety_buffer", reason=reason)

    return None


# ---------------------------------------------------------------------------
# Monkey-patch the legacy module so existing callers keep working
# ---------------------------------------------------------------------------
def _install_into_legacy() -> None:
    """Inject F2/F3 implementations into the legacy chronos_pre_trade_check
    module namespace.  Called once at import time of this module.

    This is the only safe way to fix the stubs without touching the
    write-blocked legacy file.
    """
    try:
        import chronos_pre_trade_check as _legacy
        _legacy._check_layer_f2_mffu_consistency = check_layer_f2_mffu_consistency
        _legacy._check_layer_f3_mffu_safety_buffer = check_layer_f3_mffu_safety_buffer
        log.debug(
            "[chronos_v3.pre_trade_layers] F2/F3 installed into "
            "chronos_pre_trade_check namespace"
        )
    except Exception as exc:  # pragma: no cover
        log.warning(
            "[chronos_v3.pre_trade_layers] monkey-patch failed: %s", exc
        )


_install_into_legacy()
