"""Model Router tests"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.model_router import (
    select_model, EscalationContext,
    MODEL_HAIKU, MODEL_SONNET, MODEL_OPUS,
)


def test_aar_normal_uses_haiku():
    ctx = EscalationContext(task_type="aar", pnl_pct=0.005, anomaly_count=2)
    model, _ = select_model(ctx)
    assert model == MODEL_HAIKU


def test_aar_big_loss_escalates_opus():
    ctx = EscalationContext(task_type="aar", pnl_pct=-0.03, anomaly_count=0)
    model, reason = select_model(ctx)
    assert model == MODEL_OPUS
    assert "AAR重大" in reason


def test_aar_many_anomalies_escalates():
    ctx = EscalationContext(task_type="aar", pnl_pct=0.01, anomaly_count=15)
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS


def test_premortem_new_feature_opus():
    ctx = EscalationContext(task_type="premortem", is_new_feature=True)
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS


def test_premortem_routine_haiku():
    ctx = EscalationContext(task_type="premortem")
    model, _ = select_model(ctx)
    assert model == MODEL_HAIKU


def test_kill_switch_always_opus():
    ctx = EscalationContext(task_type="aar", is_kill_switch_related=True)
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS


def test_destructive_always_opus():
    ctx = EscalationContext(task_type="peer_review", is_destructive=True)
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS


def test_red_team_fixed_opus():
    ctx = EscalationContext(task_type="red_team")
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS


def test_analyst_strategic_opus():
    ctx = EscalationContext(task_type="analyst", is_strategic=True)
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS


def test_analyst_routine_sonnet():
    ctx = EscalationContext(task_type="analyst")
    model, _ = select_model(ctx)
    assert model == MODEL_SONNET


def test_deviation_normalization_opus():
    ctx = EscalationContext(task_type="deviation", is_normalization_risk=True)
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS


def test_user_override():
    ctx = EscalationContext(task_type="aar", user_override="opus")
    model, _ = select_model(ctx)
    assert model == MODEL_OPUS
