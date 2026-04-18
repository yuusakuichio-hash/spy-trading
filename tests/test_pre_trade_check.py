"""Pre-trade check regression tests"""
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.pre_trade_check import check_order, OrderContext
from common import kill_switch


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    kill_switch.deactivate()
    yield
    kill_switch.deactivate()


def _normal_ctx(**overrides):
    defaults = dict(
        symbol="US.SPY",
        strike=710,
        side="SELL",
        qty=1,
        option_price=0.30,
        bid=0.29,
        ask=0.31,
        est_margin=500,
        capital_usd=100_000,
        open_positions=5,
        open_margin_total=15_000,
        symbol_margin=3_000,
        paper=True,
    )
    defaults.update(overrides)
    return OrderContext(**defaults)


def test_normal_pass():
    r = check_order(_normal_ctx())
    assert r.allow is True
    assert r.layer == "PASS"


def test_deep_itm_block():
    """今朝のSPXW 5400C $1697 裸ロング事例"""
    r = check_order(_normal_ctx(
        symbol="US.SPXW", strike=5400, option_price=1697.30,
        est_margin=170_000, capital_usd=420_000, paper=True,
    ))
    assert r.allow is False
    assert r.layer == "L1"
    assert "Deep ITM" in r.reason


def test_whitelist():
    r = check_order(_normal_ctx(symbol="US.GME"))
    assert r.allow is False
    assert r.layer == "L1"


def test_qty_fat_finger():
    r = check_order(_normal_ctx(qty=9999))
    assert r.allow is False
    assert r.layer == "L1"


def test_qty_zero():
    r = check_order(_normal_ctx(qty=0))
    assert r.allow is False


def test_margin_over():
    r = check_order(_normal_ctx(est_margin=50_000))
    assert r.allow is False
    assert r.layer == "L1"


def test_spread_too_wide():
    r = check_order(_normal_ctx(bid=0.10, ask=0.50))  # 80% spread
    assert r.allow is False


def test_position_count_over():
    r = check_order(_normal_ctx(open_positions=20))
    assert r.allow is False
    assert r.layer == "L2"


def test_concentration_over():
    r = check_order(_normal_ctx(symbol_margin=25_000, est_margin=1_000))
    assert r.allow is False
    assert r.layer == "L2"


def test_kill_switch():
    kill_switch.activate("test")
    r = check_order(_normal_ctx())
    assert r.allow is False
    assert r.layer == "KILL"


def test_frequency_limit():
    # 連続15件で閾値超えを引き起こす
    ctx = _normal_ctx()
    results = [check_order(ctx) for _ in range(20)]
    # 最後の方は必ずL4で止まる
    blocked = [r for r in results if r.layer == "L4"]
    assert len(blocked) > 0
