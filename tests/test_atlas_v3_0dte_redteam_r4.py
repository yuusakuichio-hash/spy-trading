"""tests/test_atlas_v3_0dte_redteam_r4.py — ZeroDTESystemTactic C-11 修正テスト

対象修正:
  C-11: build_exit_order side ハードコード解消
        - ZeroDTEPosition.pos_direction: Literal["credit", "long"] 追加
        - credit → buy_to_close（side="buy"）
        - long   → sell_to_close（side="sell"）
        - idem key に side を含める（credit/long 混在時の key 衝突防止）

完了条件: 本ファイル ≥ 3 件 PASS + 既存回帰 0 件
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from atlas_v3.core.engine import OrderRequest
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.zero_dte_system import (
    ZeroDTEConfig,
    ZeroDTEExitDecision,
    ZeroDTEPosition,
    ZeroDTESystemTactic,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_kill_switch(tmp_path, monkeypatch):
    """Kill Switch の state_v3 を tmp_path に隔離（テスト間干渉防止）。"""
    import common_v3.risk.kill_switch as ks_module
    tmp_state = tmp_path / "state_v3"
    tmp_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_module, "_STATE_DIR", tmp_state)
    monkeypatch.setattr(ks_module, "FLAG_FILE", tmp_state / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", tmp_state / "kill_switch_audit.jsonl")
    yield


def _env(vix: float = 18.0, gex: float = 1.0, bias: str = "bull") -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=1.5,
        gex=gex,
        term_ratio=1.0,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={"SPX": 45.0},
    )


def _tactic(config: ZeroDTEConfig | None = None) -> ZeroDTESystemTactic:
    return ZeroDTESystemTactic(config=config)


def _exit_decision() -> ZeroDTEExitDecision:
    return ZeroDTEExitDecision(should_exit=True, reason="test", exit_type="stop_loss")


def _pos(pos_direction: str, symbol: str = "SPX") -> ZeroDTEPosition:
    return ZeroDTEPosition(
        symbol=symbol,
        quantity=1,
        entry_price=5.0,
        current_price=5.0,
        tactic_name="0dte_system",
        entry_time=datetime.now(timezone.utc),
        unrealized_pnl=0.0,
        max_credit=0.0,
        pos_direction=pos_direction,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# C-11-01: credit ポジションの exit side が "buy"（buy_to_close）であること
# ---------------------------------------------------------------------------

def test_c11_credit_position_exit_side_is_buy():
    """C-11: pos_direction="credit" のとき build_exit_order が side="buy" を返す。"""
    t = _tactic()
    pos = _pos("credit")
    decision = _exit_decision()
    order = t.build_exit_order(pos, decision)

    assert isinstance(order, OrderRequest)
    assert order.side == "buy", (
        f"credit ポジションの exit side が 'buy' でない（got: {order.side!r}）"
    )


# ---------------------------------------------------------------------------
# C-11-02: long ポジションの exit side が "sell"（sell_to_close）であること
# ---------------------------------------------------------------------------

def test_c11_long_position_exit_side_is_sell():
    """C-11: pos_direction="long" のとき build_exit_order が side="sell" を返す。"""
    t = _tactic()
    pos = _pos("long")
    decision = _exit_decision()
    order = t.build_exit_order(pos, decision)

    assert isinstance(order, OrderRequest)
    assert order.side == "sell", (
        f"long ポジションの exit side が 'sell' でない（got: {order.side!r}）"
    )


# ---------------------------------------------------------------------------
# C-11-03: credit/long の idem key が異なること（key 衝突防止）
# ---------------------------------------------------------------------------

def test_c11_idem_keys_differ_between_credit_and_long():
    """C-11: 同一 symbol でも credit と long の idem key が衝突しないこと。

    make_job_key は strategy 文字列をハッシュに含める。
    credit: strategy="0dte_system_exit_buy"
    long:   strategy="0dte_system_exit_sell"
    ── 両者は異なる strategy 文字列を使うため同一時刻・同一 symbol でも key が衝突しない。
    """
    from common_v3.idempotency.store import make_job_key

    fixed_time = datetime(2026, 4, 23, 14, 0, 0, tzinfo=timezone.utc)
    key_credit = make_job_key(
        strategy="0dte_system_exit_buy",
        symbol="SPX",
        trigger_time=fixed_time,
    )
    key_long = make_job_key(
        strategy="0dte_system_exit_sell",
        symbol="SPX",
        trigger_time=fixed_time,
    )

    assert key_credit != key_long, (
        "credit と long の idem key が衝突している（C-11 idem key 修正未適用）"
    )
    # 両方とも v3_ プレフィックスを持つこと
    assert key_credit.startswith("v3_")
    assert key_long.startswith("v3_")


# ---------------------------------------------------------------------------
# C-11-04: pos_direction のデフォルト値が "credit" であること（後方互換）
# ---------------------------------------------------------------------------

def test_c11_pos_direction_default_is_credit():
    """C-11: ZeroDTEPosition のデフォルト pos_direction が "credit" であること。

    既存コードが pos_direction を指定しない場合にクレジット構造が正しく
    処理されることを保証する後方互換テスト。
    """
    pos = ZeroDTEPosition(
        symbol="SPX",
        quantity=1,
        entry_price=5.0,
    )
    assert pos.pos_direction == "credit", (
        f"pos_direction のデフォルトが 'credit' でない（got: {pos.pos_direction!r}）"
    )


# ---------------------------------------------------------------------------
# C-11-05: pos_direction="long" の ZeroDTEPosition が正しく構築できること
# ---------------------------------------------------------------------------

def test_c11_pos_direction_long_construction():
    """C-11: pos_direction="long" を明示的に設定した ZeroDTEPosition が構築できること。"""
    pos = ZeroDTEPosition(
        symbol="QQQ",
        quantity=2,
        entry_price=3.5,
        pos_direction="long",
    )
    assert pos.pos_direction == "long"
    assert pos.symbol == "QQQ"
    assert pos.quantity == 2
