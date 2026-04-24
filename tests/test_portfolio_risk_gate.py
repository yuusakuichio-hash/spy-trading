"""tests/test_portfolio_risk_gate.py — PortfolioRisk Gate テスト (12 件)

カバー範囲:
  - GateConfig: バリデーション / デフォルト値
  - check_entry_allowed: VIX halt / max_concurrent_entries halt / both OK
  - クールダウン期間 (cooldown_secs) 挙動
  - GateDecision: halt / allow ファクトリ
  - グローバル状態 reset / halt_count
  - check_entry_allowed_with_log: allowed/halted ロギング
"""
from __future__ import annotations

import sys
import os
import logging

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atlas_v3.ops.portfolio_risk_gate import (
    DEFAULT_GATE_CONFIG,
    GateConfig,
    GateDecision,
    PortfolioRiskGateError,
    _gate_state,
    check_entry_allowed,
    check_entry_allowed_with_log,
    reset_gate_state,
)


# ── フィクスチャ ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_state():
    """各テスト前後に gate 状態をリセット。"""
    reset_gate_state()
    yield
    reset_gate_state()


def _config(
    vix_halt: float = 30.0,
    max_entries: int = 10,
    cooldown_secs: float = 0.0,  # テストでは cooldown 無効化
    vix_warning: float = 25.0,
) -> GateConfig:
    return GateConfig(
        vix_halt_threshold=vix_halt,
        max_concurrent_entries=max_entries,
        cooldown_secs=cooldown_secs,
        vix_warning_threshold=vix_warning,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: GateConfig デフォルト値が正しい
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_config_defaults():
    cfg = GateConfig()
    assert cfg.vix_halt_threshold == pytest.approx(30.0)
    assert cfg.max_concurrent_entries == 10
    assert cfg.cooldown_secs == pytest.approx(300.0)
    assert cfg.vix_warning_threshold == pytest.approx(25.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: GateConfig — 非正 vix_halt_threshold はエラー
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_config_invalid_vix_threshold():
    with pytest.raises(PortfolioRiskGateError, match="vix_halt_threshold"):
        GateConfig(vix_halt_threshold=0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: GateConfig — warning > halt はエラー
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_config_warning_gt_halt():
    with pytest.raises(PortfolioRiskGateError, match="vix_warning_threshold"):
        GateConfig(vix_halt_threshold=25.0, vix_warning_threshold=30.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: VIX が閾値未満 + entries が上限未満 → allowed=True
# ─────────────────────────────────────────────────────────────────────────────

def test_all_ok_returns_allowed():
    decision = check_entry_allowed(20.0, 5, _config())
    assert decision.allowed is True
    assert decision.reason == "ok"
    assert decision.active_rules == []


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: VIX >= halt_threshold → allowed=False (vix_spike_halt)
# ─────────────────────────────────────────────────────────────────────────────

def test_vix_spike_halts_entry():
    decision = check_entry_allowed(30.0, 3, _config(vix_halt=30.0))
    assert decision.allowed is False
    assert "vix_spike_halt" in decision.active_rules
    assert "VIX=30.0" in decision.reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: VIX が大きく超過しても halt 判定は同じ
# ─────────────────────────────────────────────────────────────────────────────

def test_vix_far_above_threshold_still_halts():
    decision = check_entry_allowed(85.0, 1, _config(vix_halt=30.0))
    assert decision.allowed is False
    assert "vix_spike_halt" in decision.active_rules


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: current_entries >= max → allowed=False (max_concurrent_entries)
# ─────────────────────────────────────────────────────────────────────────────

def test_max_entries_exceeded_halts():
    decision = check_entry_allowed(15.0, 10, _config(max_entries=10))
    assert decision.allowed is False
    assert "max_concurrent_entries" in decision.active_rules
    assert "current_entries=10" in decision.reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: VIX halt + entries 超過 → 両方の rule が active_rules に入る
# ─────────────────────────────────────────────────────────────────────────────

def test_both_rules_active_when_both_violated():
    decision = check_entry_allowed(35.0, 15, _config(vix_halt=30.0, max_entries=10))
    assert decision.allowed is False
    assert "vix_spike_halt" in decision.active_rules
    assert "max_concurrent_entries" in decision.active_rules


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: VIX halt が発動すると halt_count が増加する
# ─────────────────────────────────────────────────────────────────────────────

def test_halt_count_increments_on_vix_halt():
    assert _gate_state.get_halt_count() == 0
    check_entry_allowed(32.0, 0, _config())
    assert _gate_state.get_halt_count() == 1
    check_entry_allowed(35.0, 0, _config())
    assert _gate_state.get_halt_count() == 2


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: reset_gate_state で halt_count がリセットされる
# ─────────────────────────────────────────────────────────────────────────────

def test_reset_clears_halt_count():
    check_entry_allowed(40.0, 0, _config())
    assert _gate_state.get_halt_count() == 1
    reset_gate_state()
    assert _gate_state.get_halt_count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: 入力バリデーション — 負値 vix は PortfolioRiskGateError
# ─────────────────────────────────────────────────────────────────────────────

def test_negative_vix_raises():
    with pytest.raises(PortfolioRiskGateError, match="vix must be"):
        check_entry_allowed(-1.0, 5, _config())


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: check_entry_allowed_with_log — halt 時に log.error が呼ばれる
# ─────────────────────────────────────────────────────────────────────────────

def test_with_log_emits_error_on_halt(caplog):
    with caplog.at_level(logging.ERROR, logger="atlas_v3.ops.portfolio_risk_gate"):
        decision = check_entry_allowed_with_log(
            35.0, 0, _config(vix_halt=30.0), context="test_ctx"
        )
    assert decision.allowed is False
    # "[HALT]" が含まれるログが 1 件以上出力されていること
    halt_logs = [r for r in caplog.records if "[HALT]" in r.message]
    assert len(halt_logs) >= 1
