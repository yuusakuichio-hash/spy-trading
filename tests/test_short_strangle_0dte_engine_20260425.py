"""tests/test_short_strangle_0dte_engine_20260425.py — ShortStrangle0DTEEngine テスト 15 件

観点:
  T-01: TacticBase ABC 継承確認
  T-02: tactic_type / tactic_name プロパティ
  T-03: is_0dte_expiry — 当日 → True
  T-04: is_0dte_expiry — 翌日 → False
  T-05: is_0dte_expiry — 空文字 → False
  T-06: VIX 15-30 範囲内 → is_vix_in_range=True
  T-07: VIX < 15 → False (低 VIX 除外)
  T-08: VIX > 30 → False (spike halt)
  T-09: VIX 境界値 15.0 / 30.0 → True
  T-10: エントリー窓内 (10:30 ET) → True / 窓外 (09:00 ET) → False
  T-11: 15:30 ET 以降 → force_close (should_exit)
  T-12: should_exit — 損切り (current_value >= 2x credit)
  T-13: should_exit — 利確 (current_value <= 30% of credit)
  T-14: 担保計算 calc_required_margin / is_margin_sufficient
  T-15: should_enter — 全条件 OK → should_enter=True, idempotency_key 設定済み
"""
from __future__ import annotations

import sys
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

# パス設定
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from atlas_v3.bots.engines.short_strangle_0dte import (
    DELTA_MAX,
    DELTA_MIN,
    FORCE_CLOSE_HOUR_ET,
    FORCE_CLOSE_MINUTE_ET,
    IVR_MIN,
    VIX_ENTRY_MAX,
    VIX_ENTRY_MIN,
    ShortStrangle0DTEConfig,
    ShortStrangle0DTEEngine,
    StranglePosition,
    calc_required_margin,
    is_0dte_expiry,
    is_force_close_time,
    is_in_entry_window,
    is_margin_sufficient,
    is_vix_in_range,
)
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# fixtures
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


def _env(
    vix: float = 22.0,
    symbol: str = "SPY",
    ivr: float = 70.0,
) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=1.5,
        gex=1.0,
        term_ratio=1.0,
        bias="neutral",
        ivr_by_symbol={symbol: ivr},
    )


def _et_now(hour: int, minute: int, date_str: str = "2026-04-25") -> datetime:
    """指定の ET 時刻を UTC で返す。"""
    naive = datetime.fromisoformat(f"{date_str}T{hour:02d}:{minute:02d}:00")
    et_dt = naive.replace(tzinfo=ET)
    return et_dt.astimezone(timezone.utc)


def _engine(config: ShortStrangle0DTEConfig | None = None) -> ShortStrangle0DTEEngine:
    return ShortStrangle0DTEEngine(config=config)


def _position(
    symbol: str = "SPY",
    initial_credit: float = 1.00,   # $1.00 per share = $100 per contract
    current_value: float = 0.30,
    quantity: int = 1,
) -> StranglePosition:
    return StranglePosition(
        symbol=symbol,
        quantity=quantity,
        initial_credit=initial_credit,
        current_value=current_value,
        call_strike=560.0,
        put_strike=540.0,
        expiry_date="2026-04-25",
        entry_time=datetime.now(timezone.utc),
        tactic_name="short_strangle_0dte",
    )


# ---------------------------------------------------------------------------
# T-01: TacticBase ABC 継承確認
# ---------------------------------------------------------------------------

def test_t01_tactic_base_inheritance():
    """ShortStrangle0DTEEngine は TacticBase を継承していること。"""
    engine = _engine()
    assert isinstance(engine, TacticBase), (
        "ShortStrangle0DTEEngine は TacticBase を継承していなければならない"
    )


# ---------------------------------------------------------------------------
# T-02: tactic_type / tactic_name プロパティ
# ---------------------------------------------------------------------------

def test_t02_tactic_properties():
    """tactic_type='enter_exit' / tactic_name='short_strangle_0dte' であること。"""
    engine = _engine()
    assert engine.tactic_type == "enter_exit"
    assert engine.tactic_name == "short_strangle_0dte"


# ---------------------------------------------------------------------------
# T-03: is_0dte_expiry — 当日 → True
# ---------------------------------------------------------------------------

def test_t03_is_0dte_expiry_today():
    """当日の expiry_date は True を返す。"""
    # 2026-04-25 11:00 ET（UTC 15:00）
    ref_utc = _et_now(11, 0, "2026-04-25")
    assert is_0dte_expiry("2026-04-25", ref_utc) is True


# ---------------------------------------------------------------------------
# T-04: is_0dte_expiry — 翌日 → False
# ---------------------------------------------------------------------------

def test_t04_is_0dte_expiry_tomorrow():
    """翌日 expiry は False を返す。"""
    ref_utc = _et_now(11, 0, "2026-04-25")
    assert is_0dte_expiry("2026-04-26", ref_utc) is False


# ---------------------------------------------------------------------------
# T-05: is_0dte_expiry — 空文字 → False
# ---------------------------------------------------------------------------

def test_t05_is_0dte_expiry_empty():
    """expiry_date が空文字の場合は False を返す。"""
    assert is_0dte_expiry("") is False
    assert is_0dte_expiry("", datetime.now(timezone.utc)) is False


# ---------------------------------------------------------------------------
# T-06: VIX 15-30 範囲内 → True
# ---------------------------------------------------------------------------

def test_t06_vix_in_range_nominal():
    """VIX=22 は 15-30 範囲内で True。"""
    assert is_vix_in_range(22.0, VIX_ENTRY_MIN, VIX_ENTRY_MAX) is True


# ---------------------------------------------------------------------------
# T-07: VIX < 15 → False
# ---------------------------------------------------------------------------

def test_t07_vix_too_low():
    """VIX=12 は範囲外 (< 15) で False。プレミアム薄い環境は除外。"""
    assert is_vix_in_range(12.0, VIX_ENTRY_MIN, VIX_ENTRY_MAX) is False


# ---------------------------------------------------------------------------
# T-08: VIX > 30 → False (spike halt)
# ---------------------------------------------------------------------------

def test_t08_vix_spike_halt():
    """VIX=35 は 30 超過で False（gamma spike halt）。"""
    assert is_vix_in_range(35.0, VIX_ENTRY_MIN, VIX_ENTRY_MAX) is False


# ---------------------------------------------------------------------------
# T-09: VIX 境界値 15.0 / 30.0 → True
# ---------------------------------------------------------------------------

def test_t09_vix_boundary_values():
    """VIX=15.0 と VIX=30.0 は境界値 inclusive で True。"""
    assert is_vix_in_range(15.0, VIX_ENTRY_MIN, VIX_ENTRY_MAX) is True
    assert is_vix_in_range(30.0, VIX_ENTRY_MIN, VIX_ENTRY_MAX) is True


# ---------------------------------------------------------------------------
# T-10: エントリー窓 10:30 ET → True / 09:00 ET → False
# ---------------------------------------------------------------------------

def test_t10_entry_window():
    """10:30 ET はエントリー窓内・09:00 ET は窓外。"""
    # 10:30 ET — 窓内
    dt_1030 = _et_now(10, 30, "2026-04-25").astimezone(ET)
    assert is_in_entry_window(dt_1030, 10, 30, 13, 0) is True

    # 09:00 ET — 窓外（1h 経過前）
    dt_0900 = _et_now(9, 0, "2026-04-25").astimezone(ET)
    assert is_in_entry_window(dt_0900, 10, 30, 13, 0) is False

    # 13:00 ET — 窓終了（closed）
    dt_1300 = _et_now(13, 0, "2026-04-25").astimezone(ET)
    assert is_in_entry_window(dt_1300, 10, 30, 13, 0) is False

    # 12:59 ET — まだ窓内
    dt_1259 = _et_now(12, 59, "2026-04-25").astimezone(ET)
    assert is_in_entry_window(dt_1259, 10, 30, 13, 0) is True


# ---------------------------------------------------------------------------
# T-11: 15:30 ET 以降 → force_close
# ---------------------------------------------------------------------------

def test_t11_force_close_at_1530():
    """15:30 ET 以降は should_exit=True / exit_type='force_close'。"""
    engine = _engine()
    pos = _position(initial_credit=1.00, current_value=0.50)  # 通常保持中

    # 15:30 ET — 強制クローズ発動
    now_1530 = _et_now(15, 30, "2026-04-25")
    decision = engine.should_exit(pos, _env(), now_utc=now_1530)
    assert decision.should_exit is True
    assert decision.exit_type == "force_close", f"expected force_close, got {decision.exit_type}"

    # 15:29 ET — まだ発動しない（損切り・利確条件を外した値を使う）
    now_1529 = _et_now(15, 29, "2026-04-25")
    pos_mid = _position(initial_credit=1.00, current_value=0.50)
    decision_mid = engine.should_exit(pos_mid, _env(), now_utc=now_1529)
    # 0.50 は初期クレジット 1.00 の 50% → 利確 (30%) でも損切り (200%) でもない → 保持
    assert decision_mid.should_exit is False

    # 16:00 ET — 強制クローズ引き続き発動
    now_1600 = _et_now(16, 0, "2026-04-25")
    decision_late = engine.should_exit(pos, _env(), now_utc=now_1600)
    assert decision_late.should_exit is True
    assert decision_late.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-12: should_exit — 損切り (current_value >= 2x credit)
# ---------------------------------------------------------------------------

def test_t12_stop_loss():
    """current_value が initial_credit の 2x 以上で stop_loss exit。"""
    engine = _engine()
    pos = _position(initial_credit=1.00, current_value=2.00)  # 2x = 損切り境界
    now = _et_now(11, 0, "2026-04-25")

    decision = engine.should_exit(pos, _env(), now_utc=now)
    assert decision.should_exit is True
    assert decision.exit_type == "stop_loss", f"expected stop_loss, got {decision.exit_type}"

    # 2x 未満はまだ損切りしない
    pos_safe = _position(initial_credit=1.00, current_value=1.99)
    decision_safe = engine.should_exit(pos_safe, _env(), now_utc=now)
    # 0.30 threshold (profit) は 1.99 > 0.30 で利確でもない → 保持
    assert decision_safe.should_exit is False


# ---------------------------------------------------------------------------
# T-13: should_exit — 利確 (current_value <= 30% of credit)
# ---------------------------------------------------------------------------

def test_t13_profit_target():
    """current_value が initial_credit の 30% 以下で profit_target exit。"""
    engine = _engine()
    pos = _position(initial_credit=1.00, current_value=0.30)  # 30% = 利確境界
    now = _et_now(11, 0, "2026-04-25")

    decision = engine.should_exit(pos, _env(), now_utc=now)
    assert decision.should_exit is True
    assert decision.exit_type == "profit_target", f"expected profit_target, got {decision.exit_type}"

    # 30% 超過はまだ利確しない
    pos_hold = _position(initial_credit=1.00, current_value=0.31)
    decision_hold = engine.should_exit(pos_hold, _env(), now_utc=now)
    assert decision_hold.should_exit is False


# ---------------------------------------------------------------------------
# T-14: 担保計算 calc_required_margin / is_margin_sufficient
# ---------------------------------------------------------------------------

def test_t14_margin_calculation():
    """担保計算が仕様通りに動作すること。"""
    # call_credit=0.50, put_credit=0.50 → total_credit = 1.00 * 100 = 100
    # margin_required = 100 / 25 = 4.0
    margin = calc_required_margin(
        call_credit=0.50, put_credit=0.50, quantity=1, margin_divisor=25.0
    )
    assert margin == pytest.approx(4.0), f"expected 4.0, got {margin}"

    # 2 contracts
    margin_2 = calc_required_margin(
        call_credit=0.50, put_credit=0.50, quantity=2, margin_divisor=25.0
    )
    assert margin_2 == pytest.approx(8.0)

    # is_margin_sufficient: credit > 0 なら True（設計上常に充足）
    assert is_margin_sufficient(0.50, 0.50, 1, 25.0) is True

    # credit=0 は担保不足 → False
    assert is_margin_sufficient(0.0, 0.50, 1, 25.0) is False
    assert is_margin_sufficient(0.50, 0.0, 1, 25.0) is False


# ---------------------------------------------------------------------------
# T-15: should_enter — 全条件 OK → should_enter=True, idempotency_key 設定済み
# ---------------------------------------------------------------------------

def test_t15_should_enter_all_conditions_ok():
    """全エントリー条件充足 → should_enter=True かつ idempotency_key 非空。"""
    engine = _engine()
    env = _env(vix=22.0, symbol="SPY", ivr=70.0)
    now_utc = _et_now(11, 0, "2026-04-25")  # 11:00 ET — 窓内

    decision = engine.should_enter(
        env=env,
        symbol="SPY",
        call_strike=565.0,
        put_strike=540.0,
        call_delta=0.12,
        put_delta=0.12,
        call_credit=0.60,
        put_credit=0.60,
        expiry_date="2026-04-25",
        now_utc=now_utc,
    )

    assert decision.should_enter is True, f"should_enter=False: {decision.reason}"
    assert decision.symbol == "SPY"
    assert decision.call_strike == 565.0
    assert decision.put_strike == 540.0
    assert decision.idempotency_key != "", "idempotency_key が空"
    assert decision.margin_required > 0.0

    # 全条件 NG — IVR 低すぎ
    env_low_ivr = _env(vix=22.0, symbol="SPY", ivr=50.0)
    decision_ng = engine.should_enter(
        env=env_low_ivr,
        symbol="SPY",
        call_delta=0.12, put_delta=0.12,
        call_credit=0.60, put_credit=0.60,
        expiry_date="2026-04-25",
        now_utc=now_utc,
    )
    assert decision_ng.should_enter is False
    assert "IVR" in decision_ng.reason
