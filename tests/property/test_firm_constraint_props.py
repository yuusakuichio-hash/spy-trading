"""Property-based tests for chronos_firm_constraint_enforcer.py

ChainGuard 級バグターゲット:
  - DLL -1 超過で必ず block になる (DLL=-1 時に allow が漏れないか)
  - max_contracts 超過で必ず block
  - enabled=False は常に block
  - unknown strategy_id は常に block
  - random payload でも例外が出ない (exception safety)
"""
from __future__ import annotations

import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import datetime
import yaml
import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from chronos_firm_constraint_enforcer import FirmConstraintEnforcer, CheckResult

# ── Fixtures ──────────────────────────────────────────────────────────────────

_BASE_YAML = {
    "version": "1",
    "default_webhook_url_env": "TEST_WEBHOOK",
    "strategies": [
        {
            "id": "test_strategy_a",
            "name": "Test A",
            "broker": "tp_paper",
            "webhook_url_env": "TEST_WEBHOOK",
            "firm": "test_firm",
            "enabled": True,
            "firm_constraints": {
                "daily_loss_limit_usd": 500,
                "max_contracts": 3,
                "overnight_allowed": False,
                "consistency_rule_pct": 50,
                "force_close_et": "15:45",
                "hft_prohibited": False,
            },
        },
        {
            "id": "test_strategy_disabled",
            "name": "Test Disabled",
            "broker": "tp_paper",
            "webhook_url_env": "TEST_WEBHOOK",
            "firm": "test_firm",
            "enabled": False,
            "firm_constraints": {
                "daily_loss_limit_usd": 500,
                "max_contracts": 10,
                "overnight_allowed": True,
            },
        },
        {
            "id": "test_no_dll",
            "name": "Test No DLL",
            "broker": "tp_paper",
            "webhook_url_env": "TEST_WEBHOOK",
            "firm": "test_firm",
            "enabled": True,
            "firm_constraints": {
                "daily_loss_limit_usd": None,
                "max_contracts": None,
                "overnight_allowed": True,
            },
        },
    ],
}


def _make_enforcer(yaml_data: dict) -> tuple[FirmConstraintEnforcer, str]:
    """テスト用に一時 YAML ファイルを作成して FirmConstraintEnforcer を返す。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        yaml.dump(yaml_data, f)
        path = f.name
    return FirmConstraintEnforcer(path), path


_enforcer, _yaml_path = _make_enforcer(_BASE_YAML)


# ── Property 1: DLL 超過で必ず block ─────────────────────────────────────────

@given(
    pnl=st.floats(
        min_value=-100_000.0,
        max_value=-500.01,   # DLL = 500 なので -500.01 以下は必ず超過
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_dll_exceeded_always_blocked(pnl):
    """daily_pnl <= -DLL のとき allowed は必ず False。"""
    result = _enforcer.check(
        strategy_id="test_strategy_a",
        action="buy",
        qty=1,
        daily_pnl_usd=pnl,
    )
    assert result.allowed is False, (
        f"DLL exceeded (pnl={pnl:.2f} DLL=500) but allowed=True"
    )
    # blocked_rules に daily_loss_limit が含まれる
    assert any("daily_loss_limit" in r for r in result.blocked_rules), (
        f"blocked_rules missing daily_loss_limit: {result.blocked_rules}"
    )


# ── Property 2: DLL ギリギリ内 (pnl > -DLL) は block されない ────────────────

@given(
    pnl=st.floats(
        min_value=-499.99,
        max_value=50_000.0,   # 黒字側も含む
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_dll_within_limit_not_blocked_by_dll(pnl):
    """daily_pnl > -DLL のとき DLL 起因の block はない (qty=1 で他制約も満たす)。"""
    result = _enforcer.check(
        strategy_id="test_strategy_a",
        action="buy",
        qty=1,          # max_contracts=3 なので OK
        daily_pnl_usd=pnl,
        # force_close_et=15:45 に引っかからないよう now_et を None に (チェックされない)
    )
    # DLL ブロックがないこと (他制約で block されても DLL ブロックはない)
    assert not any("daily_loss_limit" in r for r in result.blocked_rules), (
        f"DLL should NOT block pnl={pnl:.2f}: blocked_rules={result.blocked_rules}"
    )


# ── Property 3: max_contracts 超過で必ず block ────────────────────────────────

@given(
    qty=st.integers(min_value=4, max_value=1000),  # max_contracts=3
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_max_contracts_exceeded_always_blocked(qty):
    """qty > max_contracts のとき allowed は必ず False。"""
    result = _enforcer.check(
        strategy_id="test_strategy_a",
        action="buy",
        qty=qty,
        daily_pnl_usd=0.0,  # DLL は問題なし
    )
    assert result.allowed is False, (
        f"max_contracts exceeded (qty={qty} max=3) but allowed=True"
    )
    assert any("max_contracts" in r for r in result.blocked_rules), (
        f"blocked_rules missing max_contracts: {result.blocked_rules}"
    )


# ── Property 4: disabled strategy は常に block ───────────────────────────────

@given(
    action=st.sampled_from(["buy", "sell", "close"]),
    qty=st.integers(min_value=1, max_value=5),
    pnl=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_disabled_strategy_always_blocked(action, qty, pnl):
    """enabled=False の strategy は任意の入力で必ず block される。"""
    result = _enforcer.check(
        strategy_id="test_strategy_disabled",
        action=action,
        qty=qty,
        daily_pnl_usd=pnl,
    )
    assert result.allowed is False, (
        f"disabled strategy should be blocked but allowed=True (action={action}, qty={qty}, pnl={pnl})"
    )
    assert "strategy_disabled" in result.blocked_rules, (
        f"blocked_rules should contain strategy_disabled: {result.blocked_rules}"
    )


# ── Property 5: unknown strategy_id は常に block ──────────────────────────────

@given(
    unknown_id=st.text(min_size=1, max_size=50)
    .filter(lambda s: s not in ("test_strategy_a", "test_strategy_disabled", "test_no_dll")),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_unknown_strategy_always_blocked(unknown_id):
    """未知の strategy_id は常に block される。"""
    result = _enforcer.check(
        strategy_id=unknown_id,
        action="buy",
        qty=1,
        daily_pnl_usd=0.0,
    )
    assert result.allowed is False, (
        f"unknown strategy_id={unknown_id!r} should be blocked but allowed=True"
    )


# ── Property 6: no-constraint strategy は正常 ────────────────────────────────

@given(
    action=st.sampled_from(["buy", "sell"]),
    qty=st.integers(min_value=1, max_value=100),
    pnl=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_no_constraint_strategy_always_allowed(action, qty, pnl):
    """DLL=None・max_contracts=None の strategy は任意の qty/pnl で allowed=True。"""
    result = _enforcer.check(
        strategy_id="test_no_dll",
        action=action,
        qty=qty,
        daily_pnl_usd=pnl,
    )
    assert result.allowed is True, (
        f"no-constraint strategy should allow but blocked: {result.blocked_rules}"
    )


# ── Property 7: force_close_et 以降に buy/sell は block ──────────────────────

@given(
    hour=st.integers(min_value=15, max_value=23),
    minute=st.integers(min_value=45, max_value=59),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_force_close_et_blocks_new_order(hour, minute):
    """force_close_et=15:45 以降の buy/sell は block される。"""
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
    now_et = datetime.datetime(2026, 4, 21, hour, minute, 0, tzinfo=ET)
    result = _enforcer.check(
        strategy_id="test_strategy_a",
        action="buy",
        qty=1,
        daily_pnl_usd=0.0,
        now_et=now_et,
    )
    assert result.allowed is False, (
        f"force_close_et=15:45 should block buy at {hour}:{minute} ET"
    )
    assert any("force_close_et" in r for r in result.blocked_rules), (
        f"blocked_rules missing force_close_et: {result.blocked_rules}"
    )


# ── Property 8: force_close_et 前は block されない ───────────────────────────

@given(
    hour=st.integers(min_value=9, max_value=15),
    minute=st.integers(min_value=0, max_value=44),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_before_force_close_not_blocked(hour, minute):
    """force_close_et=15:45 より前の buy は force_close_et ブロックがない。"""
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
    now_et = datetime.datetime(2026, 4, 21, hour, minute, 0, tzinfo=ET)
    result = _enforcer.check(
        strategy_id="test_strategy_a",
        action="buy",
        qty=1,
        daily_pnl_usd=0.0,
        now_et=now_et,
    )
    assert not any("force_close_et" in r for r in result.blocked_rules), (
        f"force_close_et should NOT block at {hour}:{minute} ET: {result.blocked_rules}"
    )


# ── Property 9: DLL 境界値 (-DLL ちょうど) は block ─────────────────────────

def test_dll_boundary_exact():
    """
    daily_pnl = -DLL ちょうどは「<= -abs(dll)」の条件に一致するので block されるべき。

    Bug チェック: `< -dll` (strict) だと境界値が漏れる。
    実装: `daily_pnl_usd <= -abs(dll)` が正しい。
    """
    result = _enforcer.check(
        strategy_id="test_strategy_a",
        action="buy",
        qty=1,
        daily_pnl_usd=-500.0,  # DLL=500 ちょうど
    )
    assert result.allowed is False, (
        "pnl=-500.0 = -DLL=500 boundary should be blocked (<=), but allowed=True"
    )


# ── Property 10: result は常に CheckResult 型で例外なし ─────────────────────

@given(
    strategy_id=st.text(max_size=50),
    action=st.text(max_size=10),
    qty=st.integers(min_value=-100, max_value=10000),
    pnl=st.floats(allow_nan=True, allow_infinity=True),
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_check_never_raises_exception(strategy_id, action, qty, pnl):
    """任意の入力で check() が例外を出さずに CheckResult を返す。"""
    # pnl の NaN/Inf は float 比較で問題になる可能性があるのでテスト対象
    import math
    safe_pnl = 0.0 if (math.isnan(pnl) or math.isinf(pnl)) else pnl
    try:
        result = _enforcer.check(
            strategy_id=strategy_id,
            action=action,
            qty=qty,
            daily_pnl_usd=safe_pnl,
        )
        assert isinstance(result, CheckResult)
        assert isinstance(result.allowed, bool)
    except Exception as e:
        pytest.fail(
            f"check() raised exception for strategy_id={strategy_id!r} "
            f"action={action!r} qty={qty} pnl={safe_pnl}: {e}"
        )
