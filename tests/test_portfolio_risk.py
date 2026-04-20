"""Bot間リスク統合 + 週次/月次DD管理 テスト (15テスト)

カバー範囲:
  - portfolio_aggregator: Bot別PnL集計 / 全Bot合算 / ポジション集計
  - check_loss_gates: 日次/週次/月次ゲート
  - check_cross_bot_limits: Bot間合算ポジション・証拠金
  - pre_trade_check L3: 月次DD超過時のKill Switch自動発動
  - pre_trade_check L3B: cross_bot_limits によるブロック
"""
import datetime
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import kill_switch, portfolio_aggregator
from common.portfolio_aggregator import (
    BotRiskSnapshot,
    PortfolioRiskSummary,
    aggregate_portfolio_risk,
    bot_pnl_by_period,
    bot_portfolio_risk,
    check_cross_bot_limits,
    check_loss_gates,
    daily_pnl,
    monthly_pnl,
    weekly_pnl,
)
from common.pre_trade_check import OrderContext, check_order
from common.risk_limits import DEFAULT_LIMITS, RiskLimits


# ─────────────────────────────────────────────────────────────────────────────
# フィクスチャ
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_kill_switch():
    kill_switch.deactivate()
    yield
    kill_switch.deactivate()


@pytest.fixture()
def tmp_data_dir(tmp_path, monkeypatch):
    """portfolio_aggregator が参照するファイルパスを一時ディレクトリに向ける"""
    monkeypatch.setattr(portfolio_aggregator, "PNL_FILE", tmp_path / "condor_pnl.json")
    monkeypatch.setattr(portfolio_aggregator, "PORTFOLIO_PNL_FILE", tmp_path / "portfolio_pnl.json")
    monkeypatch.setattr(portfolio_aggregator, "POSITIONS_FILE", tmp_path / "portfolio_positions.json")
    return tmp_path


def _write_portfolio_pnl(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps(records), encoding="utf-8")


def _write_positions(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_condor_pnl(path: Path, trades: list[dict]) -> None:
    path.write_text(json.dumps({"trades": trades}), encoding="utf-8")


def _limits() -> RiskLimits:
    return DEFAULT_LIMITS["P0_paper"]


def _today_str() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: 空データで全損失関数がゼロを返す
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_data_returns_zero(tmp_data_dir):
    today = datetime.date.today()
    assert daily_pnl(today) == 0.0
    assert weekly_pnl(today) == 0.0
    assert monthly_pnl(today) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Bot別PnLが正しく期間フィルタされる (日次)
# ─────────────────────────────────────────────────────────────────────────────

def test_bot_pnl_by_period_daily(tmp_data_dir):
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    records = [
        {"date": today.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -50.0},
        {"date": today.strftime("%Y-%m-%d"), "bot": "momentum_bot", "pnl_usd": 30.0},
        {"date": yesterday.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -100.0},
    ]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)

    assert bot_pnl_by_period("spy_bot", "daily", today) == -50.0
    assert bot_pnl_by_period("momentum_bot", "daily", today) == 30.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: 週次P&Lが今週分のみ合算される
# ─────────────────────────────────────────────────────────────────────────────

def test_weekly_pnl_accumulates(tmp_data_dir):
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    before_week = week_start - datetime.timedelta(days=1)
    records = [
        {"date": week_start.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -200.0},
        {"date": today.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -100.0},
        {"date": before_week.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -9999.0},
    ]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)
    assert weekly_pnl(today) == pytest.approx(-300.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: 月次P&Lが月初から当日まで合算される
# ─────────────────────────────────────────────────────────────────────────────

def test_monthly_pnl_accumulates(tmp_data_dir):
    today = datetime.date.today()
    month_start = today.replace(day=1)
    before_month = month_start - datetime.timedelta(days=1)
    records = [
        {"date": month_start.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -500.0},
        {"date": today.strftime("%Y-%m-%d"), "bot": "momentum_bot", "pnl_usd": 100.0},
        {"date": before_month.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -9999.0},
    ]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)
    assert monthly_pnl(today) == pytest.approx(-400.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: check_loss_gates — 日次ゲートブロック
# ─────────────────────────────────────────────────────────────────────────────

def test_check_loss_gates_daily_block(tmp_data_dir):
    today = datetime.date.today()
    capital = 10_000.0
    # daily_loss_pct=-0.05 → -500以下でブロック
    records = [{"date": today.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -500.0}]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)

    allow, reason = check_loss_gates(capital, _limits(), today)
    assert allow is False
    assert "daily_loss_gate" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: check_loss_gates — 週次ゲートブロック
# ─────────────────────────────────────────────────────────────────────────────

def test_check_loss_gates_weekly_block(tmp_data_dir):
    """週次損失がブロック閾値を超えた場合に allow=False が返ることを確認。

    注意: 週次と日次は独立したチェック。日次 -3% 未満 + 週次 -6% 超でのみ
    "weekly_loss_gate" になるが、今日が月曜（week_start == today）の場合は
    同じデータが日次にも反映され日次でブロックされる場合がある。

    このテストでは「大きな週次損失で allow=False になること」の検証に留め、
    reason の具体的なキーは日付依存のため確認しない。
    代わりに別途 reason に gate 関連文字列が含まれることを確認する。
    """
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    capital = 10_000.0
    # 今週月曜に -700 を記録（-7% → weekly_loss_pct=-6% を超える）
    # 月曜の場合は今日と同じになり日次でもブロックされる（どちらでも allow=False が返る）
    records = [
        {"date": week_start.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -700.0},
    ]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)

    allow, reason = check_loss_gates(capital, _limits(), today)
    assert allow is False
    # "weekly_loss_gate" または "daily_loss_gate" のどちらかが含まれること
    assert "loss_gate" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: check_loss_gates — 月次ゲートブロック
# ─────────────────────────────────────────────────────────────────────────────

def test_check_loss_gates_monthly_block(tmp_data_dir):
    today = datetime.date.today()
    month_start = today.replace(day=1)
    capital = 10_000.0
    # monthly_loss_pct=-0.12 → -1200以下。週次・日次はクリア
    old_date = month_start.strftime("%Y-%m-%d")
    records = [
        {"date": old_date, "bot": "spy_bot", "pnl_usd": -1300.0},
    ]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)

    allow, reason = check_loss_gates(capital, _limits(), today)
    assert allow is False
    assert "monthly_loss_gate" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: check_loss_gates — 全てクリア時はTrue
# ─────────────────────────────────────────────────────────────────────────────

def test_check_loss_gates_all_ok(tmp_data_dir):
    today = datetime.date.today()
    capital = 100_000.0
    records = [{"date": today.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -100.0}]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)

    allow, reason = check_loss_gates(capital, _limits(), today)
    assert allow is True
    assert reason == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: pre_trade_check L3 — 月次DD超過でKill Switch自動発動
# ─────────────────────────────────────────────────────────────────────────────

def test_pre_trade_check_monthly_dd_activates_kill_switch(tmp_data_dir):
    today = datetime.date.today()
    capital = 10_000.0
    month_start = today.replace(day=1)
    # load_limits() は data/risk_limits.yaml を参照するため
    # P0_paper monthly_loss_pct は yaml 値 -0.20 になる可能性がある。
    # -2100 / 10000 = -21% → -20% ゲートを確実に超える
    records = [
        {"date": month_start.strftime("%Y-%m-%d"), "bot": "spy_bot", "pnl_usd": -2100.0},
    ]
    _write_portfolio_pnl(tmp_data_dir / "portfolio_pnl.json", records)

    assert not kill_switch.is_active()

    ctx = OrderContext(
        symbol="US.SPY",
        strike=710,
        side="SELL",
        qty=1,
        option_price=0.30,
        bid=0.29,
        ask=0.31,
        est_margin=500,
        capital_usd=capital,
        open_positions=1,
        open_margin_total=500,
        symbol_margin=500,
        paper=True,
    )
    result = check_order(ctx)
    assert result.allow is False
    assert result.layer == "L3"
    assert kill_switch.is_active()
    assert "monthly" in kill_switch.reason().lower()


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: aggregate_portfolio_risk — 複数Bot合算
# ─────────────────────────────────────────────────────────────────────────────

def test_aggregate_portfolio_risk(tmp_data_dir):
    positions_data = {
        "spy_bot": {
            "positions": [
                {"symbol": "US.SPY", "qty": 1, "delta": -0.3},
                {"symbol": "US.SPY", "qty": 1, "delta": -0.3},
            ],
            "total_risk": 1000.0,
            "updated_at": "2026-04-18T09:00:00",
        },
        "momentum_bot": {
            "positions": [
                {"symbol": "US.QQQ", "qty": 2, "delta": 0.5},
            ],
            "total_risk": 800.0,
            "updated_at": "2026-04-18T09:01:00",
        },
        "stop_loss_events": [],  # 非Botキー → 除外されるべき
    }
    _write_positions(tmp_data_dir / "portfolio_positions.json", positions_data)

    summary = aggregate_portfolio_risk()
    assert isinstance(summary, PortfolioRiskSummary)
    assert summary.total_positions == 3
    assert summary.total_risk_usd == pytest.approx(1800.0)
    assert summary.total_delta == pytest.approx(-0.3 - 0.3 + 0.5)
    assert len(summary.bots) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: check_cross_bot_limits — 合計ポジション超過でブロック
# ─────────────────────────────────────────────────────────────────────────────

def test_check_cross_bot_limits_positions_over(tmp_data_dir):
    positions_data = {
        "spy_bot": {
            "positions": [{"symbol": "US.SPY"}] * 10,
            "total_risk": 5000.0,
        },
        "momentum_bot": {
            "positions": [{"symbol": "US.QQQ"}] * 8,
            "total_risk": 4000.0,
        },
    }
    _write_positions(tmp_data_dir / "portfolio_positions.json", positions_data)

    allow, reason = check_cross_bot_limits(capital_usd=100_000, limits=_limits())
    assert allow is False
    assert "cross_bot_position_limit" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: check_cross_bot_limits — 合計証拠金超過でブロック
# ─────────────────────────────────────────────────────────────────────────────

def test_check_cross_bot_limits_margin_over(tmp_data_dir):
    positions_data = {
        "spy_bot": {
            "positions": [{"symbol": "US.SPY"}],
            "total_risk": 30_000.0,
        },
        "momentum_bot": {
            "positions": [{"symbol": "US.QQQ"}],
            "total_risk": 25_000.0,  # 合計 55% > 50%
        },
    }
    _write_positions(tmp_data_dir / "portfolio_positions.json", positions_data)

    allow, reason = check_cross_bot_limits(capital_usd=100_000.0, limits=_limits())
    assert allow is False
    assert "cross_bot_margin_limit" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: check_cross_bot_limits — 問題なし時はTrueを返す
# ─────────────────────────────────────────────────────────────────────────────

def test_check_cross_bot_limits_ok(tmp_data_dir):
    positions_data = {
        "spy_bot": {
            "positions": [{"symbol": "US.SPY"}] * 3,
            "total_risk": 5_000.0,
        },
        "momentum_bot": {
            "positions": [{"symbol": "US.QQQ"}] * 2,
            "total_risk": 3_000.0,
        },
    }
    _write_positions(tmp_data_dir / "portfolio_positions.json", positions_data)

    allow, reason = check_cross_bot_limits(capital_usd=100_000.0, limits=_limits())
    assert allow is True
    assert reason == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: pre_trade_check L3B — cross_bot_limits 超過でブロック
# ─────────────────────────────────────────────────────────────────────────────

def test_pre_trade_check_l3b_cross_bot_block(tmp_data_dir):
    # load_limits() は risk_limits.yaml を参照し max_positions=20 (P0_paper yaml値)
    # 合計22ポジ >= 20 でL3Bブロックを確実に発生させる
    positions_data = {
        "spy_bot": {
            "positions": [{"symbol": "US.SPY"}] * 13,
            "total_risk": 10_000.0,
        },
        "momentum_bot": {
            "positions": [{"symbol": "US.QQQ"}] * 9,
            "total_risk": 8_000.0,
        },
    }
    _write_positions(tmp_data_dir / "portfolio_positions.json", positions_data)

    ctx = OrderContext(
        symbol="US.SPY",
        strike=710,
        side="SELL",
        qty=1,
        option_price=0.30,
        bid=0.29,
        ask=0.31,
        est_margin=500,
        capital_usd=100_000.0,
        open_positions=5,
        open_margin_total=10_000,
        symbol_margin=3_000,
        paper=True,
    )
    result = check_order(ctx)
    # 合計22ポジ >= max_positions(yaml=20) でL3Bブロック
    assert result.allow is False
    assert result.layer == "L3B"


# ─────────────────────────────────────────────────────────────────────────────
# Test 15: condor_pnl (旧来) + portfolio_pnl が合算される
# ─────────────────────────────────────────────────────────────────────────────

def test_legacy_and_portfolio_pnl_combined(tmp_data_dir):
    today = datetime.date.today()
    _write_condor_pnl(
        tmp_data_dir / "condor_pnl.json",
        [{"event": "exit", "date": today.strftime("%Y-%m-%d"), "pnl_usd": -100.0}],
    )
    _write_portfolio_pnl(
        tmp_data_dir / "portfolio_pnl.json",
        [{"date": today.strftime("%Y-%m-%d"), "bot": "momentum_bot", "pnl_usd": -200.0}],
    )
    result = daily_pnl(today)
    assert result == pytest.approx(-300.0)
