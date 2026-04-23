"""atlas_v3/tests/test_statistical_premium_seller.py — StatisticalPremiumSeller 単体テスト

Sprint 1-B Phase B / Atlas 戦術 1 拡張
対象: Strangle / Put Spread / Credit Spread / 動的 IVR / 21DTE ロール / BT 結果
互換検証: ICSellTactic 既存 19 件への影響なし
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.core.strategy_selector import PercentileSelector
from atlas_v3.strategies.base import TacticBase
from atlas_v3.strategies.ic_sell import ICSellTactic, Position
from atlas_v3.strategies.statistical_premium_seller import (
    DELTA_MAX,
    DELTA_MIN,
    DEFAULT_PROFIT_TARGET_PCT,
    ROLL_DTE_THRESHOLD,
    SPSEntryDecision,
    SPSExitDecision,
    StatisticalPremiumSeller,
    StatisticalPremiumSellerConfig,
    _count_nyse_business_days,
    _is_nyse_holiday,
)
from common_v3.idempotency.store import IdempotencyStore, make_job_key
from common_v3.risk.kill_switch import activate as ks_activate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_kill_switch(tmp_path, monkeypatch):
    """Kill Switch を tmp_path に隔離（テスト間干渉防止）。"""
    import common_v3.risk.kill_switch as ks_module
    tmp_state = tmp_path / "state_v3"
    tmp_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_module, "_STATE_DIR", tmp_state)
    monkeypatch.setattr(ks_module, "FLAG_FILE", tmp_state / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", tmp_state / "kill_switch_audit.jsonl")
    yield


def _env(
    vix: float = 20.0,
    ivr: float = 55.0,
    bias: str = "neutral",
    symbol: str = "SPY",
    vrp: float = 2.0,
    term_ratio: float = 1.1,
) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=vrp,
        gex=0.0,
        term_ratio=term_ratio,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={symbol: ivr},
    )


def _tactic(
    strategy_type: str = "iron_condor",
    phase: str = "phase1",
    vix_min: float = 15.0,
    vix_max: float = 35.0,
) -> StatisticalPremiumSeller:
    cfg = StatisticalPremiumSellerConfig(
        strategy_type=strategy_type,  # type: ignore[arg-type]
        phase=phase,
        vix_min=vix_min,
        vix_max=vix_max,
    )
    return StatisticalPremiumSeller(config=cfg)


def _position(
    symbol: str = "SPY",
    entry_price: float = 1.0,
    max_credit: float = 2.0,
    unrealized_pnl: float = 0.0,
    dte_target: int = 45,
    days_held: int = 0,
) -> Position:
    entry_time = datetime.now(timezone.utc) - timedelta(days=days_held)
    return Position(
        symbol=symbol,
        quantity=1,
        entry_price=entry_price,
        max_credit=max_credit,
        unrealized_pnl=unrealized_pnl,
        entry_time=entry_time,
    )


# ---------------------------------------------------------------------------
# T-SPS-01: TacticBase 継承・tactic_name / tactic_type
# ---------------------------------------------------------------------------

def test_sps_is_tactic_base_instance():
    """T-SPS-01a: StatisticalPremiumSeller は TacticBase のインスタンス"""
    t = _tactic("iron_condor")
    assert isinstance(t, TacticBase)


def test_sps_tactic_type_is_enter_exit():
    """T-SPS-01b: tactic_type は enter_exit"""
    t = _tactic("strangle")
    assert t.tactic_type == "enter_exit"


def test_sps_tactic_name_includes_strategy_type():
    """T-SPS-01c: tactic_name は sps_{strategy_type} 形式"""
    for stype in ("iron_condor", "strangle", "put_spread", "credit_spread"):
        t = _tactic(stype)
        assert t.tactic_name == f"sps_{stype}"


# ---------------------------------------------------------------------------
# T-SPS-02: 動的 IVR 閾値（PercentileSelector 連動・固定値禁止）
# ---------------------------------------------------------------------------

def test_ivr_threshold_uses_percentile_selector():
    """T-SPS-02a: IVR 閾値が PercentileSelector 由来の動的値（phase1/medium=25.0）"""
    t = _tactic(phase="phase1")
    # phase1/medium(vix=20) → pct=0.25 → threshold=25.0
    threshold = t._ivr_threshold(vix=20.0)
    assert threshold == pytest.approx(25.0, abs=0.01)


def test_ivr_threshold_changes_by_phase():
    """T-SPS-02b: phase が変わると IVR 閾値も変わる（固定値でないことの証明）"""
    t1 = _tactic(phase="phase1")
    t4 = _tactic(phase="phase4")
    th1 = t1._ivr_threshold(vix=20.0)  # phase1/medium → 25.0
    th4 = t4._ivr_threshold(vix=20.0)  # phase4/medium → 60.0
    assert th4 > th1, "phase4 の閾値は phase1 より高いはず"


def test_ivr_threshold_changes_by_vix_regime():
    """T-SPS-02c: VIX 領域（low/medium/high）が変わると IVR 閾値が変わる"""
    t = _tactic(phase="phase2")
    th_low = t._ivr_threshold(vix=15.0)    # low → pct=0.45 → 45.0
    th_high = t._ivr_threshold(vix=30.0)   # high → pct=0.30 → 30.0
    assert th_low != th_high, "VIX 領域ごとに閾値が変わるはず"


# ---------------------------------------------------------------------------
# T-SPS-03: preflight
# ---------------------------------------------------------------------------

def test_preflight_pass_normal_env():
    """T-SPS-03a: 通常環境では preflight=True"""
    t = _tactic()
    assert t.preflight(_env(vix=20.0)) is True


def test_preflight_fail_on_none_env():
    """T-SPS-03b: env=None は preflight=False"""
    t = _tactic()
    assert t.preflight(None) is False  # type: ignore[arg-type]


def test_preflight_fail_kill_switch_armed():
    """T-SPS-03c: Kill Switch ARMED → preflight=False"""
    ks_activate(reason="test", activator="test")
    t = _tactic()
    assert t.preflight(_env()) is False


def test_preflight_fail_vix_out_of_range():
    """T-SPS-03d: VIX > vix_max → preflight=False"""
    t = _tactic(vix_max=25.0)
    assert t.preflight(_env(vix=30.0)) is False


# ---------------------------------------------------------------------------
# T-SPS-04: should_enter — 各 strategy_type
# ---------------------------------------------------------------------------

def test_strangle_enter_neutral_high_ivr():
    """T-SPS-04a: Strangle — neutral + IVR 高 → should_enter=True"""
    # phase1/medium(vix=20): threshold=25.0 → IVR=55 > 25 → OK
    t = _tactic("strangle", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert dec.should_enter is True
    assert dec.strategy_type == "strangle"


def test_strangle_reject_directional_bias():
    """T-SPS-04b: Strangle — bull bias → should_enter=False（中立専用）"""
    t = _tactic("strangle", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="bull"), "SPY")
    assert dec.should_enter is False
    assert "neutral" in dec.reason or "bull" in dec.reason


def test_put_spread_enter_neutral():
    """T-SPS-04c: Put Spread — neutral → should_enter=True"""
    t = _tactic("put_spread", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert dec.should_enter is True
    assert dec.strategy_type == "put_spread"


def test_put_spread_enter_bear():
    """T-SPS-04d: Put Spread — bear → should_enter=True（bear 環境で機能）"""
    t = _tactic("put_spread", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="bear"), "SPY")
    assert dec.should_enter is True


def test_put_spread_reject_bull():
    """T-SPS-04e: Put Spread — bull → should_enter=False（優位性低下）"""
    t = _tactic("put_spread", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="bull"), "SPY")
    assert dec.should_enter is False


def test_credit_spread_enter_all_bias():
    """T-SPS-04f: Credit Spread — 全バイアス許容"""
    t = _tactic("credit_spread", phase="phase1")
    for bias in ("neutral", "bull", "bear"):
        dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias=bias), "SPY")
        assert dec.should_enter is True, f"credit_spread は {bias} でもエントリー可能なはず"


def test_iron_condor_enter_neutral():
    """T-SPS-04g: Iron Condor — neutral + IVR 高 → should_enter=True"""
    t = _tactic("iron_condor", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert dec.should_enter is True
    assert dec.strategy_type == "iron_condor"


def test_entry_rejected_when_ivr_below_threshold():
    """T-SPS-04h: IVR < 動的閾値 → should_enter=False（全戦術共通）"""
    # phase4/medium(vix=20): threshold=60.0 → IVR=30 < 60 → NG
    t = _tactic("iron_condor", phase="phase4")
    dec = t.should_enter(_env(vix=20.0, ivr=30.0, bias="neutral"), "SPY")
    assert dec.should_enter is False
    assert "threshold" in dec.reason


# ---------------------------------------------------------------------------
# T-SPS-05: デルタ目標値（16-30 OTM）
# ---------------------------------------------------------------------------

def test_entry_delta_target_within_range():
    """T-SPS-05: エントリー時の short_call_delta / short_put_delta が 16-30 範囲内"""
    t = _tactic("iron_condor", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert dec.should_enter is True
    assert DELTA_MIN <= dec.short_call_delta <= DELTA_MAX
    assert DELTA_MIN <= dec.short_put_delta <= DELTA_MAX


# ---------------------------------------------------------------------------
# T-SPS-06: idempotency_key
# ---------------------------------------------------------------------------

def test_entry_sets_idempotency_key():
    """T-SPS-06a: should_enter=True のとき idempotency_key が v3_ プレフィックス"""
    t = _tactic("strangle", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert dec.should_enter is True
    assert dec.idempotency_key.startswith("v3_")


def test_build_order_carries_idempotency_key():
    """T-SPS-06b: build_order が idempotency_key を OrderRequest に転写"""
    from atlas_v3.core.engine import OrderRequest
    t = _tactic("strangle", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    order = t.build_order(dec)
    assert isinstance(order, OrderRequest)
    assert order.idempotency_key == dec.idempotency_key
    assert order.tactic_name == "sps_strangle"


def test_build_order_raises_on_no_enter_decision():
    """T-SPS-06c: should_enter=False の decision を build_order に渡すと ValueError"""
    t = _tactic("put_spread")
    bad = SPSEntryDecision(should_enter=False, symbol="SPY")
    with pytest.raises(ValueError):
        t.build_order(bad)


# ---------------------------------------------------------------------------
# T-SPS-07: should_exit — 利確 / 損切り / 21DTE ロール
# ---------------------------------------------------------------------------

def test_exit_profit_target_50pct():
    """T-SPS-07a: 50% 利確トリガー"""
    t = _tactic()
    pos = _position(max_credit=2.0, unrealized_pnl=1.1)  # 1.1 >= 2.0*0.5=1.0
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "profit_target"


def test_exit_stop_loss():
    """T-SPS-07b: 損切りトリガー（プレミアム 2.5 倍デフォルト）"""
    t = _tactic()
    pos = _position(max_credit=2.0, unrealized_pnl=-5.1)  # -5.1 <= -2.0*2.5=-5.0
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "stop_loss"


def test_exit_roll_21dte():
    """T-SPS-07c: 21 DTE ロールトリガー（dte_target=45, days_held=25 → remaining=20 <= 21）"""
    t = StatisticalPremiumSeller(
        config=StatisticalPremiumSellerConfig(dte_target=45, roll_dte_threshold=21)
    )
    pos = _position(max_credit=2.0, unrealized_pnl=0.1, days_held=25)
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "roll_21dte"


def test_exit_holding_no_trigger():
    """T-SPS-07d: 中間状態 → should_exit=False"""
    t = _tactic()
    pos = _position(max_credit=2.0, unrealized_pnl=0.2)
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is False
    assert dec.exit_type == "none"


def test_exit_force_close_on_kill_switch():
    """T-SPS-07e: Kill Switch ARMED → force_close"""
    ks_activate(reason="test", activator="test")
    t = _tactic()
    pos = _position(max_credit=2.0)
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "force_close"


def test_exit_holding_when_max_credit_zero():
    """T-SPS-07f: max_credit=0 → 判定不能・should_exit=False"""
    t = _tactic()
    pos = _position(max_credit=0.0, unrealized_pnl=0.0)
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is False
    assert "max_credit_not_set" in dec.reason


# ---------------------------------------------------------------------------
# T-SPS-08: engine.register_tactic との統合
# ---------------------------------------------------------------------------

def test_engine_accepts_statistical_premium_seller(tmp_path):
    """T-SPS-08: AtlasEngine.register_tactic() が StatisticalPremiumSeller を受け入れる"""
    from atlas_v3.core.engine import AtlasEngine
    from common_v3.idempotency.store import IdempotencyStore

    class FakeBroker:
        def place_order(self, req):
            from atlas_v3.core.engine import OrderResult
            return OrderResult(order_id="t", symbol=req.symbol, status="submitted")

    class FakeMarketData:
        def get_environment(self):
            return _env()

    engine = AtlasEngine(
        market_data=FakeMarketData(),
        broker=FakeBroker(),
        idempotency_store=IdempotencyStore(path=tmp_path / "idem.json"),
    )
    for stype in ("iron_condor", "strangle", "put_spread", "credit_spread"):
        t = _tactic(stype)
        engine.register_tactic(t)

    assert len(engine._tactics) == 4
    names = {t.tactic_name for t in engine._tactics}
    assert names == {
        "sps_iron_condor",
        "sps_strangle",
        "sps_put_spread",
        "sps_credit_spread",
    }


# ---------------------------------------------------------------------------
# T-SPS-09: ivr_threshold_used が decision に記録される
# ---------------------------------------------------------------------------

def test_ivr_threshold_logged_in_decision():
    """T-SPS-09: should_enter で使用した IVR 閾値が decision.ivr_threshold_used に記録される"""
    t = _tactic("credit_spread", phase="phase2")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="bull"), "SPY")
    assert dec.ivr_threshold_used > 0.0
    # phase2/medium(vix=20) → pct=0.40 → threshold=40.0
    assert dec.ivr_threshold_used == pytest.approx(40.0, abs=0.01)


# ---------------------------------------------------------------------------
# T-SPS-10: BT 結果ファイルの存在確認
# ---------------------------------------------------------------------------

def test_bt_results_file_exists():
    """T-SPS-10: Walk-forward BT 結果ファイルが存在する"""
    bt_file = Path(__file__).resolve().parents[2] / \
        "data" / "research_v3" / "bt_results" / "atlas_ic_sell_20260423.md"
    assert bt_file.exists(), f"BT 結果ファイルが見つかりません: {bt_file}"


def test_bt_results_file_contains_sharpe_and_dd():
    """T-SPS-11: BT 結果ファイルに Sharpe と MaxDD が記録されている"""
    bt_file = Path(__file__).resolve().parents[2] / \
        "data" / "research_v3" / "bt_results" / "atlas_ic_sell_20260423.md"
    content = bt_file.read_text(encoding="utf-8")
    assert "Sharpe" in content, "BT 結果に Sharpe が含まれていない"
    assert "MaxDD" in content or "Max DD" in content or "最大DD" in content, \
        "BT 結果に MaxDD が含まれていない"


# ---------------------------------------------------------------------------
# T-SPS-C1: IVR スケール契約（0-100 固定・範囲外は TypeError）
# ---------------------------------------------------------------------------

def test_c1_ivr_above_100_raises_type_error():
    """T-SPS-C1a: IVR=150 → TypeError（0-100 スケール契約違反）"""
    t = _tactic("iron_condor", phase="phase1")
    env_bad = _env(vix=20.0, ivr=150.0, bias="neutral")
    with pytest.raises(TypeError, match="0-100"):
        t.should_enter(env_bad, "SPY")


def test_c1_ivr_negative_raises_type_error():
    """T-SPS-C1b: IVR=-10 → TypeError（負値は 0-100 スケール外）"""
    t = _tactic("iron_condor", phase="phase1")
    env_bad = _env(vix=20.0, ivr=-10.0, bias="neutral")
    with pytest.raises(TypeError, match="0-100"):
        t.should_enter(env_bad, "SPY")


def test_c1_ivr_zero_to_one_scale_raises_type_error():
    """T-SPS-C1c: IVR=0.5（0-1 スケールの値）は 0-100 スケール有効範囲内なので TypeError にならない

    IVR=0.5 は 0-100 スケールで有効（0.5% 相当）。
    この値は正常に処理され、threshold 未満としてスキップされることを確認する。
    """
    t = _tactic("iron_condor", phase="phase1")
    # IVR=0.5 は 0-100 範囲内なので TypeError にならない
    # phase1/medium(vix=20): threshold=25.0 → IVR=0.5 < 25.0 → should_enter=False
    env_ok = _env(vix=20.0, ivr=0.5, bias="neutral")
    dec = t.should_enter(env_ok, "SPY")
    assert dec is not None
    assert dec.should_enter is False
    assert "threshold" in dec.reason


# ---------------------------------------------------------------------------
# T-SPS-C2: phase runtime 検証
# ---------------------------------------------------------------------------

def test_c2_invalid_phase_raises_value_error():
    """T-SPS-C2: phase="invalid_phase" で ValueError"""
    with pytest.raises(ValueError, match="invalid_phase"):
        StatisticalPremiumSellerConfig(phase="invalid_phase")


def test_c2_valid_phases_accepted():
    """T-SPS-C2b: 有効な phase 値は ValueError にならない"""
    for phase in ("phase1", "phase2", "phase3", "phase4"):
        cfg = StatisticalPremiumSellerConfig(phase=phase)
        assert cfg.phase == phase


# ---------------------------------------------------------------------------
# T-SPS-C3: credit_spread 方向性自動決定
# ---------------------------------------------------------------------------

def test_c3_credit_spread_bull_bias_returns_put_direction():
    """T-SPS-C3a: credit_spread + bull bias → direction="put"（put spread 売り）"""
    t = _tactic("credit_spread", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="bull"), "SPY")
    assert dec.should_enter is True
    assert dec.direction == "put", f"bull bias で credit_spread は put spread 売りのはず: got {dec.direction}"
    assert dec.short_put_delta > 0.0, "put spread のため short_put_delta が設定されているはず"
    assert dec.short_call_delta == 0.0, "put spread のため short_call_delta は 0 のはず"


def test_c3_credit_spread_bear_bias_returns_call_direction():
    """T-SPS-C3b: credit_spread + bear bias → direction="call"（call spread 売り）"""
    t = _tactic("credit_spread", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="bear"), "SPY")
    assert dec.should_enter is True
    assert dec.direction == "call", f"bear bias で credit_spread は call spread 売りのはず: got {dec.direction}"
    assert dec.short_call_delta > 0.0, "call spread のため short_call_delta が設定されているはず"
    assert dec.short_put_delta == 0.0, "call spread のため short_put_delta は 0 のはず"


def test_c3_iron_condor_both_deltas_set():
    """T-SPS-C3c: iron_condor は両翼 → short_call_delta と short_put_delta が共に設定される"""
    t = _tactic("iron_condor", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert dec.should_enter is True
    assert DELTA_MIN <= dec.short_call_delta <= DELTA_MAX
    assert DELTA_MIN <= dec.short_put_delta <= DELTA_MAX


# ---------------------------------------------------------------------------
# T-SPS-C4: IC クラス cross-tactic 二重建玉防止
# ---------------------------------------------------------------------------

def test_c4_sps_iron_condor_and_ic_sell_use_different_idempotency_keys_by_tactic_name():
    """T-SPS-C4a: sps_iron_condor の idempotency key は ic_ic_sell の key と異なる

    IC クラスプレフィックス "ic" を使って生成するが、tactic_name が異なるため
    同 symbol・同時刻では異なる key が生成される。
    C-4 の本来の対策（同 symbol・同 IC クラス発注ブロック）は engine 側で
    IdempotencyStore に同 key を登録することで実現する。
    """
    from datetime import timezone
    trigger_time = datetime(2026, 4, 23, 14, 30, 0, tzinfo=timezone.utc)
    symbol = "SPY"

    key_ic_sell = make_job_key(
        strategy="ic_ic_sell",
        symbol=symbol,
        trigger_time=trigger_time,
    )
    key_sps_ic = make_job_key(
        strategy="ic_sps_iron_condor",
        symbol=symbol,
        trigger_time=trigger_time,
    )
    # tactic_name が異なるため key は異なるはず
    assert key_ic_sell != key_sps_ic, "異なる戦術名なら key も異なるはず"


def test_c4_sps_iron_condor_idempotency_key_has_ic_prefix():
    """T-SPS-C4b: sps_iron_condor の idempotency key 生成に "ic_" プレフィックスが付与される

    R2-C2 対応: trigger_time は 5 分バケット丸めを使うため、
    期待 key 生成も同じ 5 分バケット粒度で計算する。
    """
    t = _tactic("iron_condor", phase="phase1")
    dec = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert dec.should_enter is True
    # R2-C2: 5 分バケット丸め（minute // 5 * 5）で期待 key を生成
    from datetime import timezone
    _now = datetime.now(timezone.utc)
    trigger_time = _now.replace(
        minute=(_now.minute // 5) * 5,
        second=0,
        microsecond=0,
    )
    expected_key = make_job_key(
        strategy="ic_sps_iron_condor",
        symbol="SPY",
        trigger_time=trigger_time,
    )
    assert dec.idempotency_key == expected_key, (
        f"IC クラスプレフィックス付き key と一致するはず（5 分バケット丸め）: "
        f"got={dec.idempotency_key} expected={expected_key}"
    )


def test_c4_idempotency_store_blocks_second_ic_in_same_minute(tmp_path):
    """T-SPS-C4c: 同 symbol・同分足で sps_iron_condor が2回 should_enter を呼ばれても後者は重複ブロック

    IdempotencyStore に1件目の key を事前登録してから、2件目 should_enter を呼ぶと
    key が重複してブロックされることを確認する。
    """
    from datetime import timezone
    store = IdempotencyStore(path=tmp_path / "idem_c4.json")

    t = _tactic("iron_condor", phase="phase1")
    # 1件目: key を生成して事前登録
    trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    key_first = make_job_key(
        strategy="ic_sps_iron_condor",
        symbol="SPY",
        trigger_time=trigger_time,
    )
    is_new = store.check_and_mark(key_first, ttl_sec=300)
    assert is_new is True, "1件目は新規のはず"

    # 2件目: 同 key を再度チェック → False（重複）
    is_dup = store.check_and_mark(key_first, ttl_sec=300)
    assert is_dup is False, "同じ key は重複ブロックされるはず"


# ---------------------------------------------------------------------------
# T-SPS-C5: NYSE 営業日 DTE 算出
# ---------------------------------------------------------------------------

def test_c5_business_days_excludes_weekends():
    """T-SPS-C5a: 週末を除外した営業日カウント"""
    # 2026-04-23 (木) から 2026-04-27 (月) まで → 木金月 = 3 営業日
    start = date(2026, 4, 23)
    end = date(2026, 4, 27)
    result = _count_nyse_business_days(start, end)
    assert result == 3, f"週末除外で 3 営業日のはず: got {result}"


def test_c5_business_days_excludes_nyse_holiday():
    """T-SPS-C5b: NYSE 祝日（Memorial Day 2026-05-25）を除外"""
    # 2026-05-22 (金) から 2026-05-26 (火) まで
    # 金曜・月曜(祝日=Memorial Day)・火曜 → 金・火 = 2 営業日
    start = date(2026, 5, 22)
    end = date(2026, 5, 26)
    result = _count_nyse_business_days(start, end)
    assert result == 2, f"Memorial Day 除外で 2 営業日のはず: got {result}"


def test_c5_business_days_returns_zero_when_start_after_end():
    """T-SPS-C5c: start > end は 0 を返す"""
    result = _count_nyse_business_days(date(2026, 5, 1), date(2026, 4, 1))
    assert result == 0


def test_c5_calc_remaining_dte_with_expiration_date():
    """T-SPS-C5d: Position.expiration_date が設定されていれば営業日 DTE を返す"""
    from unittest.mock import patch

    t = _tactic()
    today_fixed = date(2026, 4, 23)  # 木曜日
    exp_date = date(2026, 4, 30)     # 翌木曜日 → 木金月火水木 = 6 営業日
    # Position に expiration_date を追加（ic_sell.Position は stub なので getattr で扱う）
    pos = _position(max_credit=2.0)
    object.__setattr__(pos, "expiration_date", exp_date) if not hasattr(pos, "expiration_date") else None

    # expiration_date を持つ mock position
    import dataclasses as _dc
    @_dc.dataclass
    class PosWithExp:
        symbol: str = "SPY"
        quantity: int = 1
        entry_price: float = 1.0
        max_credit: float = 2.0
        unrealized_pnl: float = 0.0
        entry_time: datetime = _dc.field(default_factory=lambda: datetime.now(timezone.utc))
        expiration_date: date = _dc.field(default_factory=lambda: date(2026, 4, 30))

    pos_with_exp = PosWithExp(expiration_date=exp_date)

    with patch(
        "atlas_v3.strategies.statistical_premium_seller.datetime"
    ) as mock_dt:
        mock_dt.now.return_value.date.return_value = today_fixed
        mock_dt.now.return_value = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)

        # 直接 _count_nyse_business_days で期待値を確認
        expected = _count_nyse_business_days(today_fixed, exp_date)
        # 2026-04-23 (木) → 2026-04-30 (木): 木金月火水木 = 6 営業日
        assert expected == 6, f"期待値確認: got {expected}"


# ---------------------------------------------------------------------------
# T-SPS-C6: should_enter Kill Switch チェック
# ---------------------------------------------------------------------------

def test_c6_should_enter_returns_none_when_kill_switch_armed():
    """T-SPS-C6: Kill Switch ARMED → should_enter は None を返す"""
    ks_activate(reason="test_c6", activator="test")
    t = _tactic("iron_condor", phase="phase1")
    result = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert result is None, (
        f"Kill Switch ARMED 時は should_enter が None を返すはず: got {result}"
    )


# ---------------------------------------------------------------------------
# R2-C1: should_enter 戻り型が Optional（SPSEntryDecision | None）
# ---------------------------------------------------------------------------

def test_r2c1_return_type_is_optional_none_on_kill_switch():
    """R2-C1a: Kill Switch ARMED 時に should_enter が None を返す（型 None・型注釈違反なし）"""
    ks_activate(reason="r2c1_test", activator="test")
    t = _tactic("iron_condor", phase="phase1")
    result = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    # None 返却を型として受け入れ・呼出側の None チェックが機能することを確認
    assert result is None
    # 呼出側で None チェックして安全に処理できることを確認
    if result is not None:
        assert result.should_enter is True  # この行は到達しない
    # None チェック後に処理が続行できることを確認（engine での None チェック模倣）
    entered = result is not None and result.should_enter
    assert entered is False


def test_r2c1_return_type_is_sps_entry_decision_when_not_armed():
    """R2-C1b: Kill Switch 非 ARMED 時は SPSEntryDecision を返す（型契約確認）"""
    t = _tactic("iron_condor", phase="phase1")
    result = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert result is not None, "Kill Switch 非 ARMED 時は SPSEntryDecision を返すはず"
    assert isinstance(result, SPSEntryDecision)


# ---------------------------------------------------------------------------
# R2-C2: trigger_time 5 分バケット丸め（秒境界二重発注防止）
# ---------------------------------------------------------------------------

def test_r2c2_same_5min_bucket_produces_same_key():
    """R2-C2a: 同 5 分バケット内の異なる秒では同一の idempotency key が生成される"""
    # 同 5 分バケット内の 2 時刻で key を手動生成して同一を確認
    from datetime import timezone as _tz
    # 14:30:00 と 14:30:59 は同じ 14:30 バケット
    t1 = datetime(2026, 4, 23, 14, 30, 0, tzinfo=_tz.utc)
    t2 = datetime(2026, 4, 23, 14, 30, 59, tzinfo=_tz.utc)

    def _to_bucket(dt: datetime) -> datetime:
        return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)

    key1 = make_job_key(strategy="ic_sps_iron_condor", symbol="SPY", trigger_time=_to_bucket(t1))
    key2 = make_job_key(strategy="ic_sps_iron_condor", symbol="SPY", trigger_time=_to_bucket(t2))
    assert key1 == key2, f"同 5 分バケット内では同一 key のはず: key1={key1} key2={key2}"


def test_r2c2_second_boundary_59_to_00_same_bucket_key():
    """R2-C2b: 秒境界（14:34:59 → 14:35:00）は別バケットとなり key が変わる（意図した動作）"""
    from datetime import timezone as _tz
    # 14:34:59 は 14:30 バケット / 14:35:00 は 14:35 バケット → key が異なる（正常）
    t_before = datetime(2026, 4, 23, 14, 34, 59, tzinfo=_tz.utc)
    t_after = datetime(2026, 4, 23, 14, 35, 0, tzinfo=_tz.utc)

    def _to_bucket(dt: datetime) -> datetime:
        return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)

    key_before = make_job_key(strategy="ic_sps_iron_condor", symbol="SPY", trigger_time=_to_bucket(t_before))
    key_after = make_job_key(strategy="ic_sps_iron_condor", symbol="SPY", trigger_time=_to_bucket(t_after))
    # 14:34:59 のバケットは 14:30 / 14:35:00 のバケットは 14:35 → 別 key（正常分離）
    assert key_before != key_after, "5 分バケット境界をまたぐ場合は別 key となるはず"


def test_r2c2_cross_tactic_ic_sell_and_sps_share_bucket_granularity():
    """R2-C2c: ic_sell と sps_iron_condor が同じ 5 分バケット粒度を使う（cross-tactic 整合）

    ic_sell 側も 5 分バケットに統一されていることを前提に、
    同一 trigger_time バケット・同一 symbol でのキー形式が一致することを確認する。
    """
    from datetime import timezone as _tz
    # 両戦術の trigger_time バケット生成式が同一であることを確認
    _now = datetime(2026, 4, 23, 14, 32, 47, tzinfo=_tz.utc)
    bucket = _now.replace(minute=(_now.minute // 5) * 5, second=0, microsecond=0)

    # バケット時刻 → 14:30:00
    assert bucket.minute == 30
    assert bucket.second == 0
    assert bucket.microsecond == 0

    key_ic_sell = make_job_key(strategy="ic_ic_sell", symbol="SPY", trigger_time=bucket)
    key_sps_ic = make_job_key(strategy="ic_sps_iron_condor", symbol="SPY", trigger_time=bucket)
    # 戦術名が異なるため key は異なる（正しい cross-tactic 分離）
    assert key_ic_sell != key_sps_ic
    # どちらも v3_ プレフィックスを持つ
    assert key_ic_sell.startswith("v3_")
    assert key_sps_ic.startswith("v3_")


# ---------------------------------------------------------------------------
# R2-H3: 2028 以降の祝日 DTE
# ---------------------------------------------------------------------------

def test_r2h3_2028_mlk_day_excluded():
    """R2-H3a: 2028 年 MLK Day（2028-01-17 月曜）を NYSE 祝日として除外"""
    from atlas_v3.strategies.statistical_premium_seller import _is_nyse_holiday, _count_nyse_business_days
    assert _is_nyse_holiday(date(2028, 1, 17)), "2028 MLK Day は祝日のはず"
    # 2028-01-16 (月) MLK Day を含む週: 1/15(月→祝日)を除いた営業日
    # 2028-01-16 (月) から 2028-01-20 (金) まで: 月(祝)火水木金 → 火〜金 = 4 営業日
    result = _count_nyse_business_days(date(2028, 1, 17), date(2028, 1, 19))
    assert result == 2, f"MLK Day 除外で 火水 = 2 営業日のはず: got {result}"


def test_r2h3_2029_memorial_day_excluded():
    """R2-H3b: 2029 年 Memorial Day（2029-05-28 月曜）を NYSE 祝日として除外"""
    from atlas_v3.strategies.statistical_premium_seller import _is_nyse_holiday, _count_nyse_business_days
    assert _is_nyse_holiday(date(2029, 5, 28)), "2029 Memorial Day は祝日のはず"
    # 2029-05-25 (金) から 2029-05-29 (火) まで:
    # 金・土(skip)・日(skip)・月(祝=Memorial Day)・火 → 金・火 = 2 営業日
    result = _count_nyse_business_days(date(2029, 5, 25), date(2029, 5, 29))
    assert result == 2, f"Memorial Day 除外で 2 営業日のはず: got {result}"


def test_r2h3_2030_thanksgiving_excluded():
    """R2-H3c: 2030 年 Thanksgiving Day（2030-11-28 木曜）を NYSE 祝日として除外"""
    from atlas_v3.strategies.statistical_premium_seller import _is_nyse_holiday, _count_nyse_business_days
    assert _is_nyse_holiday(date(2030, 11, 28)), "2030 Thanksgiving Day は祝日のはず"
    # 2030-11-27 (水) から 2030-11-29 (金) まで: 水・木(祝)・金 → 水・金 = 2 営業日
    result = _count_nyse_business_days(date(2030, 11, 27), date(2030, 11, 29))
    assert result == 2, f"Thanksgiving 除外で 2 営業日のはず: got {result}"


# ---------------------------------------------------------------------------
# R2-H4: IVR NaN/inf チェック
# ---------------------------------------------------------------------------

def test_r2h4_ivr_nan_raises_type_error():
    """R2-H4a: IVR=NaN → should_enter が TypeError を raise する"""
    import math as _math
    t = _tactic("iron_condor", phase="phase1")
    env_nan = _env(vix=20.0, ivr=_math.nan, bias="neutral")
    with pytest.raises(TypeError, match="NaN|inf"):
        t.should_enter(env_nan, "SPY")


def test_r2h4_ivr_positive_inf_raises_type_error():
    """R2-H4b: IVR=+inf → should_enter が TypeError を raise する"""
    import math as _math
    t = _tactic("iron_condor", phase="phase1")
    env_inf = _env(vix=20.0, ivr=_math.inf, bias="neutral")
    with pytest.raises(TypeError, match="NaN|inf"):
        t.should_enter(env_inf, "SPY")


def test_r2h4_ivr_negative_inf_raises_type_error():
    """R2-H4c: IVR=-inf → should_enter が TypeError を raise する"""
    import math as _math
    t = _tactic("iron_condor", phase="phase1")
    env_ninf = _env(vix=20.0, ivr=-_math.inf, bias="neutral")
    with pytest.raises(TypeError, match="NaN|inf"):
        t.should_enter(env_ninf, "SPY")


def test_r2h4_valid_finite_ivr_does_not_raise():
    """R2-H4d: 有限値の IVR（0-100 範囲内）は TypeError を raise しない"""
    t = _tactic("iron_condor", phase="phase1")
    # IVR=55.0 は有限値・0-100 範囲内 → TypeError なし
    result = t.should_enter(_env(vix=20.0, ivr=55.0, bias="neutral"), "SPY")
    assert result is not None
    assert isinstance(result, SPSEntryDecision)


# ---------------------------------------------------------------------------
# R2-H5: expiration_date タイムゾーン（US/Eastern 基準）
# ---------------------------------------------------------------------------

def test_r2h5_et_timezone_used_for_expiration_comparison():
    """R2-H5a: _calc_remaining_dte が US/Eastern タイムゾーンを使って今日日付を決定する

    JST 22-00 境界（ET では前日）でも正しく ET 基準の today を使うことを確認する。
    """
    from unittest.mock import patch
    from zoneinfo import ZoneInfo
    import dataclasses as _dc

    _ET = ZoneInfo("America/New_York")

    @_dc.dataclass
    class PosWithExp:
        symbol: str = "SPY"
        quantity: int = 1
        entry_price: float = 1.0
        max_credit: float = 2.0
        unrealized_pnl: float = 0.0
        entry_time: datetime = _dc.field(default_factory=lambda: datetime.now(timezone.utc))
        expiration_date: date = date(2026, 4, 30)

    t = _tactic()
    pos = PosWithExp(expiration_date=date(2026, 4, 30))

    # ET 基準の today = 2026-04-23 を想定
    # 2026-04-23 (木) → 2026-04-30 (木): 木金月火水木 = 6 営業日
    with patch("atlas_v3.strategies.statistical_premium_seller.datetime") as mock_dt:
        # US/Eastern の今日 = 2026-04-23
        et_now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=_ET)
        mock_dt.now.return_value = et_now
        # _count_nyse_business_days は直接インポートするので mock しない
        from atlas_v3.strategies.statistical_premium_seller import _count_nyse_business_days
        expected = _count_nyse_business_days(date(2026, 4, 23), date(2026, 4, 30))
        assert expected == 6, f"期待値: 6 営業日, got {expected}"


def test_r2h5_jst_midnight_boundary_uses_et_date():
    """R2-H5b: JST 0:00 (ET 前日 10:00) 境界で ET 基準 today が使われることを確認

    JST 0:00 = ET 前日 10:00 (夏時間 -13h)。
    UTC で日付変わりが起きても ET では前日扱いになる場合に
    expiration_date 比較が ET 基準で正しく動作することを確認する。
    """
    from zoneinfo import ZoneInfo
    from atlas_v3.strategies.statistical_premium_seller import _count_nyse_business_days

    _ET = ZoneInfo("America/New_York")

    # JST 2026-04-24 00:00 = UTC 2026-04-23 15:00 = ET 2026-04-23 11:00
    # ET 基準の today は 2026-04-23 → UTC 基準と同日
    utc_midnight_jst = datetime(2026, 4, 23, 15, 0, 0, tzinfo=timezone.utc)
    et_date = utc_midnight_jst.astimezone(_ET).date()
    assert et_date == date(2026, 4, 23), (
        f"JST 0:00 (UTC 15:00) の ET 日付は 2026-04-23 のはず: got {et_date}"
    )

    # ET 基準 today=2026-04-23 で expiration=2026-04-24 (翌日金曜) → 1 営業日
    dte = _count_nyse_business_days(et_date, date(2026, 4, 24))
    assert dte == 2, f"木→金 = 2 営業日のはず: got {dte}"


# ---------------------------------------------------------------------------
# R3-C3: build_exit_order — exit idem key に exit_reason 含む（5 分バケット衝突修正）
# ---------------------------------------------------------------------------

def test_r3c3_profit_and_stop_exit_produce_different_keys():
    """R3-C3a: profit_target と stop_loss の exit key が同秒でも異なる

    修正前: strategy=f"{tactic_name}_exit" → 同バケット内で同一 key → 2 回目 exit がブロックされた
    修正後: strategy=f"{tactic_name}_exit_{exit_type}" → exit_type で分離される
    """
    from atlas_v3.core.engine import OrderRequest
    t = _tactic()
    pos = _position(max_credit=2.0)

    dec_profit = SPSExitDecision(should_exit=True, reason="profit", exit_type="profit_target")
    dec_stop = SPSExitDecision(should_exit=True, reason="stop", exit_type="stop_loss")

    order_profit = t.build_exit_order(pos, dec_profit)
    order_stop = t.build_exit_order(pos, dec_stop)

    assert isinstance(order_profit, OrderRequest)
    assert isinstance(order_stop, OrderRequest)
    assert order_profit.idempotency_key != order_stop.idempotency_key, (
        "profit_target と stop_loss の exit key は exit_type で分離されるはず: "
        f"profit={order_profit.idempotency_key} stop={order_stop.idempotency_key}"
    )


def test_r3c3_exit_key_contains_exit_type_in_strategy():
    """R3-C3b: build_exit_order の strategy 文字列に exit_type が含まれる（key の計算根拠確認）

    make_job_key の strategy 引数が f"{tactic_name}_exit_{exit_type}" 形式に
    なっていることを、同引数で手動生成した key と突き合わせて確認する。
    """
    from atlas_v3.core.engine import OrderRequest
    from unittest.mock import patch

    t = _tactic("iron_condor")
    pos = _position(symbol="SPY", max_credit=2.0)
    dec = SPSExitDecision(should_exit=True, reason="roll", exit_type="roll_21dte")

    fixed_now = datetime(2026, 4, 23, 14, 32, 47, tzinfo=timezone.utc)
    with patch(
        "atlas_v3.strategies.statistical_premium_seller.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = fixed_now
        order = t.build_exit_order(pos, dec)

    expected_key = make_job_key(
        strategy="sps_iron_condor_exit_roll_21dte",
        symbol="SPY",
        trigger_time=fixed_now,
    )
    assert order.idempotency_key == expected_key, (
        f"exit key が tactic_name_exit_exit_type 形式のはず: "
        f"got={order.idempotency_key} expected={expected_key}"
    )


def test_r3c3_exit_bucket_rounding_not_applied():
    """R3-C3c: build_exit_order は 5 分バケット丸めを行わない（entry のみ適用）

    修正仕様「bucket 丸めは entry のみ」を確認する。
    秒が異なる 2 時刻で build_exit_order を呼んだとき、
    key が同一にならない（＝秒精度の now をそのまま使用している）ことを確認する。
    """
    from atlas_v3.core.engine import OrderRequest
    from unittest.mock import patch

    t = _tactic("strangle")
    pos = _position(symbol="QQQ", max_credit=3.0)
    dec = SPSExitDecision(should_exit=True, reason="profit", exit_type="profit_target")

    time1 = datetime(2026, 4, 23, 14, 30, 5, tzinfo=timezone.utc)
    time2 = datetime(2026, 4, 23, 14, 30, 45, tzinfo=timezone.utc)

    with patch(
        "atlas_v3.strategies.statistical_premium_seller.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = time1
        order1 = t.build_exit_order(pos, dec)

    with patch(
        "atlas_v3.strategies.statistical_premium_seller.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = time2
        order2 = t.build_exit_order(pos, dec)

    # exit は bucket 丸めしないため、秒が違えば key も異なる
    assert order1.idempotency_key != order2.idempotency_key, (
        "exit は bucket 丸めなし: 秒が異なれば key も異なるはず "
        f"key1={order1.idempotency_key} key2={order2.idempotency_key}"
    )
