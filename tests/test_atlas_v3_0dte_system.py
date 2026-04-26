"""tests/test_atlas_v3_0dte_system.py — ZeroDTESystemTactic 単体テスト（≥12 件）

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 / ADR-013 v2 / Gemini A2
観点:
  1. TacticBase ABC 継承 / register_tactic 成功
  2. preflight: kill_switch / VIX / daily_stop 各条件
  3. ストラクチャー選択: iron_fly / credit_spread / butterfly / none
  4. ORB 転用: ブレイクアウト方向（call_spread / put_spread / none）
  5. should_enter: ORB 未確定 / VIX 高過ぎ / 正常エントリー
  6. should_exit: kill_switch / daily_stop / force_close 時刻 / stop_loss / profit_target
  7. build_order / build_exit_order
  8. observe: ORB + Gamma state 更新
  9. persist_state / restore_state
 10. update_daily_pnl / reset_daily_pnl
 11. iv_crush_mode / shadow_live_mode フラグ
 12. engine.register_tactic 登録可能性
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Literal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.core.engine import AtlasEngine, OrderRequest
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase
from atlas_v3.strategies.orb_1dte_spy import ORBRange
from atlas_v3.strategies.zero_dte_system import (
    ZeroDTEConfig,
    ZeroDTEEntryDecision,
    ZeroDTEExitDecision,
    ZeroDTEPosition,
    ZeroDTESystemTactic,
)
from common_v3.risk.kill_switch import activate as ks_activate


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
    vix: float = 18.0,
    gex: float = 1.0,
    bias: str = "bull",
) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=1.5,
        gex=gex,
        term_ratio=1.0,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={"SPX": 45.0},
    )


def _confirmed_orb(high: float = 520.0, low: float = 515.0, symbol: str = "SPX") -> ORBRange:
    return ORBRange(
        high=high,
        low=low,
        is_confirmed=True,
        observed_at=datetime.now(timezone.utc),
        symbol=symbol,
    )


def _tactic(config: ZeroDTEConfig | None = None) -> ZeroDTESystemTactic:
    return ZeroDTESystemTactic(config=config)


def _position(
    symbol: str = "SPX",
    entry_price: float = 5.0,
    unrealized_pnl: float = 0.0,
    max_credit: float = 0.0,
) -> ZeroDTEPosition:
    return ZeroDTEPosition(
        symbol=symbol,
        quantity=1,
        entry_price=entry_price,
        current_price=entry_price,
        tactic_name="0dte_system",
        entry_time=datetime.now(timezone.utc),
        unrealized_pnl=unrealized_pnl,
        max_credit=max_credit,
    )


# ---------------------------------------------------------------------------
# T-01: TacticBase ABC 継承・tactic_type・tactic_name
# ---------------------------------------------------------------------------

def test_tactic_is_tacticbase_subclass():
    t = _tactic()
    assert isinstance(t, TacticBase)


def test_tactic_type_and_name():
    t = _tactic()
    assert t.tactic_type == "state_carrying"
    assert t.tactic_name == "0dte_system"


# ---------------------------------------------------------------------------
# T-02: register_tactic — AtlasEngine に登録できること
# ---------------------------------------------------------------------------

def test_register_tactic_succeeds(tmp_path):
    """AtlasEngine.register_tactic が TypeError を raise しないこと。"""
    md = MagicMock()
    broker = MagicMock()
    engine = AtlasEngine(market_data=md, broker=broker)
    t = _tactic()
    engine.register_tactic(t)   # should not raise
    assert t in engine._tactics


# ---------------------------------------------------------------------------
# T-03: preflight — kill_switch ARMED → False
# ---------------------------------------------------------------------------

def test_preflight_kill_switch_returns_false():
    t = _tactic()
    ks_activate(reason="test")
    assert t.preflight(_env()) is False


# ---------------------------------------------------------------------------
# T-04: preflight — VIX 過大 → False
# ---------------------------------------------------------------------------

def test_preflight_vix_too_high_returns_false():
    cfg = ZeroDTEConfig(vix_max=30.0)
    t = _tactic(cfg)
    assert t.preflight(_env(vix=35.0)) is False


# ---------------------------------------------------------------------------
# T-05: preflight — daily_stop 発動 → False
# ---------------------------------------------------------------------------

def test_preflight_daily_stop_returns_false():
    cfg = ZeroDTEConfig(daily_stop_loss=-500.0)
    t = _tactic(cfg)
    t.update_daily_pnl(-600.0)   # daily_pnl = -600 <= -500
    assert t.preflight(_env()) is False


# ---------------------------------------------------------------------------
# T-06: preflight — 正常環境 → True
# ---------------------------------------------------------------------------

def test_preflight_normal_env_returns_true():
    t = _tactic()
    assert t.preflight(_env(vix=18.0)) is True


# ---------------------------------------------------------------------------
# T-07: ストラクチャー選択 — butterfly（VIX<15 / neutral bias）
# ---------------------------------------------------------------------------

def test_structure_selection_butterfly():
    t = _tactic()
    env = _env(vix=12.0, bias="neutral")
    struct = t._select_structure(env, gamma_level=0.5)
    assert struct == "butterfly"


# ---------------------------------------------------------------------------
# T-08: ストラクチャー選択 — iron_fly（VIX<=25 / GEX>=0.5）
# ---------------------------------------------------------------------------

def test_structure_selection_iron_fly():
    t = _tactic()
    env = _env(vix=20.0, bias="bull")
    struct = t._select_structure(env, gamma_level=1.2)
    assert struct == "iron_fly"


# ---------------------------------------------------------------------------
# T-09: ストラクチャー選択 — credit_spread（VIX<=35 / GEX低）
# ---------------------------------------------------------------------------

def test_structure_selection_credit_spread():
    cfg = ZeroDTEConfig(vix_iron_fly_max=25.0, vix_credit_spread_max=35.0)
    t = _tactic(cfg)
    env = _env(vix=28.0, bias="bull")
    struct = t._select_structure(env, gamma_level=0.1)   # |GEX| < 0.5
    assert struct == "credit_spread"


# ---------------------------------------------------------------------------
# T-10: ORB 転用 — upper breakout → call_spread
# ---------------------------------------------------------------------------

def test_orb_breakout_direction_no_price_phase1_compat():
    """C-1 修正: current_price=None（価格未取得）のとき direction=none / Phase 1 互換メッセージ。"""
    t = _tactic()
    orb = ORBRange(high=100.0, low=90.0, is_confirmed=True,
                   observed_at=datetime.now(timezone.utc), symbol="SPX")
    direction, reason = t._orb_breakout_direction(orb, current_price=None)
    assert direction == "none"
    assert "Phase 1" in reason


def test_orb_breakout_direction_call_spread_via_low_high():
    """high=0 は無効 → none を返すこと。"""
    t = _tactic()
    orb = ORBRange(high=0.0, low=0.0, is_confirmed=True,
                   observed_at=datetime.now(timezone.utc), symbol="SPX")
    direction, reason = t._orb_breakout_direction(orb)
    assert direction == "none"
    assert "0" in reason


# ---------------------------------------------------------------------------
# T-11: should_enter — ORB 未確定 → should_enter=False
# ---------------------------------------------------------------------------

def test_should_enter_orb_not_confirmed():
    t = _tactic()
    t._orb_ranges["SPX"] = ORBRange(
        high=520.0, low=515.0, is_confirmed=False,
        observed_at=datetime.now(timezone.utc), symbol="SPX",
    )
    t._gamma_levels["SPX"] = 1.0
    env = _env()
    decisions = t.should_enter(env, ["SPX"])
    assert len(decisions) == 1
    assert decisions[0].should_enter is False
    assert "ORB 未確定" in decisions[0].reason


# ---------------------------------------------------------------------------
# T-12: should_enter — ORB confirmed + VIX ok → should_enter=True
# ---------------------------------------------------------------------------

def test_should_enter_normal_entry():
    t = _tactic()
    t._orb_ranges["SPX"] = _confirmed_orb()
    t._gamma_levels["SPX"] = 1.5   # iron_fly 条件
    env = _env(vix=20.0)
    decisions = t.should_enter(env, ["SPX"])
    assert len(decisions) == 1
    d = decisions[0]
    assert d.should_enter is True
    assert d.symbol == "SPX"
    assert d.structure in ("iron_fly", "credit_spread", "butterfly")
    assert d.idempotency_key.startswith("v3_")


# ---------------------------------------------------------------------------
# T-13: should_exit — kill_switch → force_close
# ---------------------------------------------------------------------------

def test_should_exit_kill_switch():
    t = _tactic()
    ks_activate(reason="test")
    result = t.should_exit(_position(), _env())
    assert result.should_exit is True
    assert result.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-14: should_exit — daily_stop → daily_stop
# ---------------------------------------------------------------------------

def test_should_exit_daily_stop():
    cfg = ZeroDTEConfig(daily_stop_loss=-500.0)
    t = _tactic(cfg)
    t.update_daily_pnl(-600.0)
    result = t.should_exit(_position(), _env())
    assert result.should_exit is True
    assert result.exit_type == "daily_stop"


# ---------------------------------------------------------------------------
# T-15: should_exit — 15:30 ET 強制クローズ
# ---------------------------------------------------------------------------

def test_should_exit_force_close_time():
    t = _tactic()
    result = t.should_exit(
        _position(), _env(),
        current_et_hour=15, current_et_minute=30,
    )
    assert result.should_exit is True
    assert result.exit_type == "eod_close"


def test_should_exit_before_force_close_time():
    t = _tactic()
    result = t.should_exit(
        _position(), _env(),
        current_et_hour=15, current_et_minute=29,
    )
    # entry_price=5, max_credit=0 → long exit check, pnl=0 → holding
    assert result.should_exit is False


# ---------------------------------------------------------------------------
# T-16: should_exit — stop_loss（credit structure・50% premium 逆行）
# ---------------------------------------------------------------------------

def test_should_exit_stop_loss_credit():
    t = _tactic()
    pos = _position(max_credit=200.0, unrealized_pnl=-101.0)
    result = t.should_exit(pos, _env())
    assert result.should_exit is True
    assert result.exit_type == "stop_loss"


# ---------------------------------------------------------------------------
# T-17: should_exit — profit_target（credit structure）
# ---------------------------------------------------------------------------

def test_should_exit_profit_target_credit():
    t = _tactic()
    pos = _position(max_credit=200.0, unrealized_pnl=101.0)
    result = t.should_exit(pos, _env())
    assert result.should_exit is True
    assert result.exit_type == "profit_target"


# ---------------------------------------------------------------------------
# T-18: build_order — should_enter=False は ValueError
# ---------------------------------------------------------------------------

def test_build_order_raises_on_no_entry():
    t = _tactic()
    bad_decision = ZeroDTEEntryDecision(should_enter=False, symbol="SPX")
    with pytest.raises(ValueError):
        t.build_order(bad_decision)


# ---------------------------------------------------------------------------
# T-19: build_order — 正常 OrderRequest 生成
# ---------------------------------------------------------------------------

def test_build_order_normal():
    t = _tactic()
    t._orb_ranges["SPX"] = _confirmed_orb()
    t._gamma_levels["SPX"] = 1.5
    env = _env(vix=20.0)
    decisions = t.should_enter(env, ["SPX"])
    d = next(x for x in decisions if x.should_enter)
    order = t.build_order(d)
    assert isinstance(order, OrderRequest)
    assert order.symbol == "SPX"
    assert order.tactic_name == "0dte_system"
    assert order.idempotency_key.startswith("v3_")


# ---------------------------------------------------------------------------
# T-20: build_exit_order — should_exit=False は ValueError
# ---------------------------------------------------------------------------

def test_build_exit_order_raises_on_no_exit():
    t = _tactic()
    bad = ZeroDTEExitDecision(should_exit=False, exit_type="none")
    with pytest.raises(ValueError):
        t.build_exit_order(_position(), bad)


# ---------------------------------------------------------------------------
# T-21: build_exit_order — 正常 OrderRequest 生成
# ---------------------------------------------------------------------------

def test_build_exit_order_normal():
    t = _tactic()
    ks_activate(reason="test")
    exit_d = t.should_exit(_position(), _env())
    order = t.build_exit_order(_position(), exit_d)
    assert isinstance(order, OrderRequest)
    assert order.side == "buy"
    assert order.tactic_name == "0dte_system"


# ---------------------------------------------------------------------------
# T-22: observe — get_orb_range + get_gex がある market_data で state 更新
# ---------------------------------------------------------------------------

def test_observe_updates_orb_and_gamma():
    t = _tactic()
    md = MagicMock()
    md.tracked_symbols = ["SPX"]
    md.get_orb_range.return_value = {
        "high": 525.0, "low": 518.0, "is_confirmed": True,
    }
    md.get_gex.return_value = 2.3

    env = _env()
    t.observe(env, md)

    assert "SPX" in t._orb_ranges
    assert t._orb_ranges["SPX"].high == 525.0
    assert t._orb_ranges["SPX"].is_confirmed is True
    assert t._gamma_levels["SPX"] == pytest.approx(2.3)


# ---------------------------------------------------------------------------
# T-23: observe — get_orb_range 未実装のとき既存 state 保持
# ---------------------------------------------------------------------------

def test_observe_no_get_orb_range_preserves_state():
    t = _tactic()
    t._orb_ranges["SPX"] = _confirmed_orb(high=510.0)
    md = MagicMock(spec=[])  # get_orb_range を持たない
    t.observe(_env(), md)
    # 既存 state が保持されること
    assert t._orb_ranges["SPX"].high == 510.0


# ---------------------------------------------------------------------------
# T-24: persist_state / restore_state
# ---------------------------------------------------------------------------

def test_persist_and_restore_state():
    """state 永続化後に新規インスタンスで復元できること。"""
    t1 = _tactic()
    t1._orb_ranges["SPX"] = _confirmed_orb(high=530.0, low=522.0)
    t1._gamma_levels["SPX"] = 3.1
    t1.update_daily_pnl(-150.0)

    store: dict = {}

    class MemStorage:
        def save(self, key: str, data: dict) -> None:
            store[key] = data
        def load(self, key: str) -> dict | None:
            return store.get(key)

    storage = MemStorage()
    t1.persist_state(storage)

    t2 = _tactic()
    t2.restore_state(storage)

    assert "SPX" in t2._orb_ranges
    assert t2._orb_ranges["SPX"].high == 530.0
    assert t2._orb_ranges["SPX"].is_confirmed is True
    assert t2._gamma_levels["SPX"] == pytest.approx(3.1)
    assert t2._daily_pnl == pytest.approx(-150.0)


# ---------------------------------------------------------------------------
# T-25: update_daily_pnl / reset_daily_pnl
# ---------------------------------------------------------------------------

def test_update_and_reset_daily_pnl():
    t = _tactic()
    t.update_daily_pnl(-100.0)
    t.update_daily_pnl(-50.0)
    assert t._daily_pnl == pytest.approx(-150.0)

    t.reset_daily_pnl()
    assert t._daily_pnl == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# T-26: shadow_live_mode フラグが decision に反映されること
# ---------------------------------------------------------------------------

def test_shadow_live_mode_flag_in_decision():
    cfg = ZeroDTEConfig(shadow_live_mode=True)
    t = _tactic(cfg)
    t._orb_ranges["SPX"] = _confirmed_orb()
    t._gamma_levels["SPX"] = 1.5
    env = _env(vix=20.0)
    decisions = t.should_enter(env, ["SPX"])
    # shadow_live=True を持つ decision が存在すること
    entering = [d for d in decisions if d.should_enter]
    assert all(d.shadow_live is True for d in entering)


# ---------------------------------------------------------------------------
# T-27: iv_crush_mode — credit_spread 以外の long premium 系ストラクチャーは
#        IV Crush モード時でも credit 構造選択に影響しないこと（設定フラグ保持）
# ---------------------------------------------------------------------------

def test_iv_crush_mode_flag_stored_in_config():
    cfg = ZeroDTEConfig(iv_crush_mode=True)
    t = _tactic(cfg)
    assert t._cfg.iv_crush_mode is True


# ---------------------------------------------------------------------------
# T-28: force_close_time — 16:00 ET は RTH 後（C-6 修正後は False）
# ---------------------------------------------------------------------------

def test_force_close_time_after_1630():
    """C-6 修正: 16:00 ET はアフターアワーなので RTH 強制クローズを発動しない。

    修正前は hour > 15 が 16:00-23:59 すべて True だったが、
    修正後は 15:30-15:59 ET のみ RTH 強制クローズ対象とする。
    """
    t = _tactic()
    result = t.should_exit(
        _position(), _env(),
        current_et_hour=16, current_et_minute=0,
    )
    # 16:00 ET はアフターアワー → force_close 不発動
    assert result.should_exit is False


# ---------------------------------------------------------------------------
# ヘルパー: current_price_by_symbol 付き env（C-1 用）
# ---------------------------------------------------------------------------

def _env_with_price(
    vix: float = 18.0,
    gex: float = 1.0,
    bias: str = "bull",
    price_by_symbol: dict | None = None,
) -> MagicMock:
    """current_price_by_symbol を持つ環境モック（C-1 テスト用）。"""
    env = MagicMock(spec=None)
    env.vix = vix
    env.vrp = 1.5
    env.gex = gex
    env.term_ratio = 1.0
    env.bias = bias
    env.ivr_by_symbol = {"SPX": 45.0}
    env.current_price_by_symbol = price_by_symbol or {}
    return env


# ---------------------------------------------------------------------------
# C-1 追加テスト: ORB breakout direction — 実 current_price 変化で bull/bear/none
# ---------------------------------------------------------------------------

def test_c1_orb_breakout_upper_real_price():
    """C-1: current_price が ORB high + buffer を超えたとき call_spread を返す。"""
    t = _tactic()
    orb = ORBRange(high=100.0, low=90.0, is_confirmed=True,
                   observed_at=datetime.now(timezone.utc), symbol="SPX")
    # high=100, buffer=0.1 → 100.101 でブレイク
    direction, reason = t._orb_breakout_direction(orb, current_price=100.2)
    assert direction == "call_spread"
    assert "upper_breakout" in reason


def test_c1_orb_breakout_lower_real_price():
    """C-1: current_price が ORB low - buffer を下回ったとき put_spread を返す。"""
    t = _tactic()
    orb = ORBRange(high=100.0, low=90.0, is_confirmed=True,
                   observed_at=datetime.now(timezone.utc), symbol="SPX")
    # low=90, buffer=0.1 → 89.89 でブレイク
    direction, reason = t._orb_breakout_direction(orb, current_price=89.8)
    assert direction == "put_spread"
    assert "lower_breakout" in reason


def test_c1_orb_breakout_range_bound_real_price():
    """C-1: current_price が ORB レンジ内のとき none / range_bound。"""
    t = _tactic()
    orb = ORBRange(high=100.0, low=90.0, is_confirmed=True,
                   observed_at=datetime.now(timezone.utc), symbol="SPX")
    direction, reason = t._orb_breakout_direction(orb, current_price=95.0)
    assert direction == "none"
    assert "range_bound" in reason


def test_c1_should_enter_with_current_price_bull_breakout():
    """C-1: env.current_price_by_symbol で upper breakout → credit_spread / call_spread エントリー。"""
    cfg = ZeroDTEConfig(vix_iron_fly_max=25.0, vix_credit_spread_max=35.0)
    t = _tactic(cfg)
    # credit_spread 条件: VIX=28（iron_fly_max=25 超え）, GEX=0.1（|GEX|<0.5）
    # ORB high=100, buffer=0.1: price=100.5 → call_spread
    t._orb_ranges["SPX"] = ORBRange(high=100.0, low=90.0, is_confirmed=True,
                                     observed_at=datetime.now(timezone.utc), symbol="SPX")
    t._gamma_levels["SPX"] = 0.1
    env = _env_with_price(vix=28.0, gex=0.1, bias="bull",
                          price_by_symbol={"SPX": 100.5})
    decisions = t.should_enter(env, ["SPX"])
    assert len(decisions) == 1
    d = decisions[0]
    assert d.should_enter is True
    assert d.direction == "call_spread"
    assert d.structure == "credit_spread"


# ---------------------------------------------------------------------------
# C-2 追加テスト: _is_force_close_time — UTC/ET 混在・DST 境界
# ---------------------------------------------------------------------------

def _mock_et_now(hour: int, minute: int, tzinfo=None):
    """指定 ET 時刻を返す datetime モック。"""
    tz = tzinfo or ZoneInfo("America/New_York")
    return datetime(2026, 6, 1, hour, minute, 0, tzinfo=tz)


def test_c2_force_close_uses_et_when_args_none():
    """C-2: hour=None/minute=None のとき内部 ET 取得で 15:30 ET 強制クローズが動く。"""
    t = _tactic()
    # 15:35 ET（hour=None で内部取得）でクローズされること
    with patch("atlas_v3.strategies.zero_dte_system.datetime") as mock_dt:
        mock_dt.now.return_value = _mock_et_now(15, 35)
        result = t._is_force_close_time(None, None)
    assert result is True


def test_c2_utc_1530_does_not_close_early():
    """C-2: UTC 15:30 渡し（= ET 11:30 夏時間）では強制クローズしないこと。

    修正前は呼び出し側が UTC h=15, m=30 を渡せば誤クローズしていた。
    修正後は引数がある場合はその値を使うが、引数 None で ET 取得する経路が正しく動く。
    ここでは 11:30 ET = UTC 15:30 相当を args で渡して False を確認する。
    """
    t = _tactic()
    # ET 11:30 → 強制クローズしない
    result = t._is_force_close_time(11, 30)
    assert result is False


def test_c2_dst_boundary_march_2026_et():
    """C-2: 2026-03-08 DST 移行日（夏時間開始）の 15:30 ET でクローズされること。"""
    t = _tactic()
    # 2026-03-08 15:30 ET (EDT, UTC-4)
    et_tz = ZoneInfo("America/New_York")
    dst_dt = datetime(2026, 3, 8, 15, 30, 0, tzinfo=et_tz)
    with patch("atlas_v3.strategies.zero_dte_system.datetime") as mock_dt:
        mock_dt.now.return_value = dst_dt
        result = t._is_force_close_time(None, None)
    assert result is True


def test_c2_dst_boundary_november_2026_et():
    """C-2: 2026-11-01 DST 移行日（標準時復帰）の 15:29 ET ではクローズしないこと。"""
    t = _tactic()
    et_tz = ZoneInfo("America/New_York")
    dst_dt = datetime(2026, 11, 1, 15, 29, 0, tzinfo=et_tz)
    with patch("atlas_v3.strategies.zero_dte_system.datetime") as mock_dt:
        mock_dt.now.return_value = dst_dt
        result = t._is_force_close_time(None, None)
    assert result is False


# ---------------------------------------------------------------------------
# C-3 追加テスト: max_credit=0 / entry_price=0 → invalid_state 即 exit
# ---------------------------------------------------------------------------

def test_c3_invalid_state_max_credit_zero_entry_price_zero():
    """C-3: max_credit=0 かつ entry_price=0 で should_exit=True / invalid_state。"""
    t = _tactic()
    pos = ZeroDTEPosition(
        symbol="SPX",
        quantity=1,
        entry_price=0.0,
        current_price=0.0,
        max_credit=0.0,
    )
    result = t.should_exit(pos, _env())
    assert result.should_exit is True
    assert result.exit_type == "force_close"
    assert "invalid_state" in result.reason


def test_c3_invalid_state_exit_reason_text():
    """C-3: invalid_state の reason に max_credit と entry_price の情報が含まれること。"""
    t = _tactic()
    pos = ZeroDTEPosition(
        symbol="QQQ",
        quantity=2,
        entry_price=0.0,
        max_credit=0.0,
    )
    result = t._check_pnl_exit(pos)
    assert result.should_exit is True
    assert "invalid_state" in result.reason
    assert result.exit_type == "force_close"


def test_c3_valid_entry_price_nonzero_not_invalid_state():
    """C-3: entry_price > 0（max_credit=0）の場合は invalid_state にならない。"""
    t = _tactic()
    pos = _position(entry_price=5.0, max_credit=0.0, unrealized_pnl=0.0)
    result = t._check_pnl_exit(pos)
    # holding (pnl=0 < profit_target, > stop_loss) → should_exit=False
    assert result.should_exit is False
    assert result.exit_type == "none"
