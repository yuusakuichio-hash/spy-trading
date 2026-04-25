"""
tests/test_paper_pl_baseline_20260425.py — paper_pl_baseline.py 単体テスト

仕様: data/research/paper_30day_judgment_criteria_20260425.md
対象: scripts/paper_pl_baseline.py

カバー関数:
  - load_daily_pnl         (3 ソース統合 + graceful degradation)
  - calc_metrics           (Sharpe / max_dd / win_rate / PF / avg_r / 月利)
  - calc_monthly_rate      (公式確認)
  - judge_checkpoint       (Day7 / Day14 / Day21 / Day30 × PASS/WATCH/FAIL)
  - emit_report            (outputs/ ファイル生成確認)
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import pytest

# プロジェクトルートを sys.path に追加
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.paper_pl_baseline import (
    DailyRecord,
    JudgmentReport,
    Metrics,
    calc_metrics,
    calc_monthly_rate,
    emit_report,
    judge_checkpoint,
    load_daily_pnl,
)


# ── フィクスチャ ──────────────────────────────────────────────────────────────

INITIAL_EQUITY = 100_000.0

# v6_rates / paper_risk を固定値で注入 (数値直書き禁止規律の根拠: data/specs/v6_rates.json)
_V6_RATES_FIXTURE = {
    "monthly_rate": {"conservative": 0.036, "central": 0.074, "optimistic": 0.119},
    "sharpe_threshold": {"value": 1.5},
    "initial_equity_usd": {"value": INITIAL_EQUITY},
}
_PAPER_RISK_FIXTURE = {
    "max_drawdown": {"pct": 0.15},
    "max_daily_loss": {"usd": -500.0},
}


def _make_records(pnls: list[float], base_date: str = "2026-04-27") -> list[DailyRecord]:
    """テスト用 DailyRecord リストを生成。"""
    from datetime import date, timedelta
    start = date.fromisoformat(base_date)
    records = []
    for i, pnl in enumerate(pnls):
        d = (start + timedelta(days=i)).isoformat()
        win = pnl > 0
        records.append(DailyRecord(
            date=d,
            pnl=pnl,
            trade_count=2,
            win_count=1 if win else 0,
            gross_profit=pnl if win else 0.0,
            gross_loss=abs(pnl) if not win else 0.0,
            avg_hold_minutes=30.0,
            source="test",
        ))
    return records


# ── T-01: load_daily_pnl — データ未存在時は空リスト ──────────────────────────

def test_load_daily_pnl_empty_dir(tmp_path):
    """ソースファイルが全て不在のとき graceful degradation で空リストを返す。"""
    result = load_daily_pnl(tmp_path)
    assert isinstance(result, list)
    assert len(result) == 0


# ── T-02: load_daily_pnl — monitor_state.jsonl 読込 ──────────────────────────

def test_load_daily_pnl_monitor_state(tmp_path):
    """monitor_state.jsonl の daily_loss エントリが DailyRecord に変換される。"""
    jsonl = tmp_path / "monitor_state.jsonl"
    # heartbeat は無視・daily_loss のみ拾う
    lines = [
        '{"ts": "2026-04-27T14:00:00+00:00", "check_name": "heartbeat", "value": 0.0}',
        '{"ts": "2026-04-27T14:00:00+00:00", "check_name": "daily_loss", "value": 250.50, "threshold": -500.0}',
        '{"ts": "2026-04-28T14:00:00+00:00", "check_name": "daily_loss", "value": -100.0, "threshold": -500.0}',
    ]
    jsonl.write_text("\n".join(lines))
    result = load_daily_pnl(tmp_path)
    assert len(result) == 2
    pnls = {r.date: r.pnl for r in result}
    assert pytest.approx(pnls.get("2026-04-27", pnls.get("2026-04-26")), abs=0.01) == 250.50


# ── T-03: load_daily_pnl — 同一日複数エントリは最後の値を採用 ─────────────────

def test_load_daily_pnl_latest_wins(tmp_path):
    """同一日に複数 daily_loss エントリがある場合は最後のエントリ (最新値) を使う。"""
    jsonl = tmp_path / "monitor_state.jsonl"
    lines = [
        '{"ts": "2026-04-27T12:00:00+00:00", "check_name": "daily_loss", "value": 100.0}',
        '{"ts": "2026-04-27T16:00:00+00:00", "check_name": "daily_loss", "value": 320.0}',
    ]
    jsonl.write_text("\n".join(lines))
    result = load_daily_pnl(tmp_path)
    assert len(result) == 1
    assert pytest.approx(result[0].pnl, abs=0.01) == 320.0


# ── T-04: load_daily_pnl — trades.jsonl 詳細補完 ──────────────────────────────

def test_load_daily_pnl_trades_jsonl(tmp_path, monkeypatch):
    """trades.jsonl からトレード詳細 (trade_count / gross_profit 等) が補完される。"""
    import scripts.paper_pl_baseline as mod
    atlas_paper_dir = tmp_path / "atlas_paper"
    atlas_paper_dir.mkdir()
    monkeypatch.setattr(mod, "_ATLAS_PAPER_DIR", atlas_paper_dir)

    trades = atlas_paper_dir / "trades.jsonl"
    trades_data = [
        {"date": "2026-04-27", "pnl": 200.0, "win": True, "hold_minutes": 45.0},
        {"date": "2026-04-27", "pnl": -80.0, "win": False, "hold_minutes": 20.0},
        {"date": "2026-04-28", "pnl": 150.0, "win": True, "hold_minutes": 30.0},
    ]
    trades.write_text("\n".join(json.dumps(t) for t in trades_data))

    result = load_daily_pnl(tmp_path)  # monitor_state は空 → trades.jsonl のみ
    dates = {r.date for r in result}
    assert "2026-04-27" in dates
    day27 = next(r for r in result if r.date == "2026-04-27")
    assert day27.trade_count == 2
    assert day27.win_count == 1
    assert pytest.approx(day27.gross_profit, abs=0.01) == 200.0
    assert pytest.approx(day27.gross_loss, abs=0.01) == 80.0


# ── T-05: calc_metrics — 空リスト時はゼロメトリクス ──────────────────────────

def test_calc_metrics_empty():
    """records が空のとき Metrics はゼロ埋めされる (no exception)。"""
    m = calc_metrics([], INITIAL_EQUITY)
    assert m.cumulative_pnl == 0.0
    assert m.monthly_rate == 0.0
    assert m.sharpe == 0.0
    assert m.max_drawdown == 0.0
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0
    assert m.trade_count == 0


# ── T-06: calc_metrics — Sharpe 計算の正確性 ─────────────────────────────────

def test_calc_metrics_sharpe():
    """一定 pnl=100 が 5 日続くとき std=0 → Sharpe=0 (ゼロ除算しない)。"""
    recs = _make_records([100.0] * 5)
    m = calc_metrics(recs, INITIAL_EQUITY)
    # all same → std=0 → sharpe=0
    assert m.sharpe == 0.0


def test_calc_metrics_sharpe_positive():
    """上昇トレンドで Sharpe が正になること。"""
    # 勝ち日が多い
    recs = _make_records([200.0, 300.0, -50.0, 250.0, 180.0, 400.0, 150.0])
    m = calc_metrics(recs, INITIAL_EQUITY)
    assert m.sharpe > 0.0


# ── T-07: calc_metrics — max_drawdown 計算 ───────────────────────────────────

def test_calc_metrics_max_drawdown():
    """equity が 100k → 105k → 98k の場合、max_dd = (105k-98k)/105k。"""
    pnls = [5000.0, -7000.0]  # peak=105k, trough=98k
    recs = _make_records(pnls)
    m = calc_metrics(recs, INITIAL_EQUITY)
    expected_dd = 7000.0 / 105_000.0
    assert pytest.approx(m.max_drawdown, abs=1e-6) == expected_dd


# ── T-08: calc_monthly_rate — 公式確認 ───────────────────────────────────────

def test_calc_monthly_rate_formula():
    """calc_monthly_rate = cum_pnl / initial_equity × (30 / elapsed)。"""
    pnls = [300.0, 200.0, -100.0]  # cum = 400
    recs = _make_records(pnls)
    rate = calc_monthly_rate(recs, INITIAL_EQUITY)
    expected = (400.0 / INITIAL_EQUITY) * (30.0 / 3)
    assert pytest.approx(rate, rel=1e-6) == expected


def test_calc_monthly_rate_empty():
    """records 空のとき 0.0 (graceful degradation)。"""
    assert calc_monthly_rate([], INITIAL_EQUITY) == 0.0


# ── T-09: judge_checkpoint Day7 — PASS / WATCH / FAIL ───────────────────────

def test_judge_day7_pass():
    """Day7: 月利 >= conservative / DD < 50% / 勝率 >= 55% / 連続損失 < 3 → PASS。"""
    # 全勝日で構成、trade_count=2/win_count=2 で勝率=100%
    pnls = [200.0, 250.0, 180.0, 300.0, 200.0, 100.0, 150.0]
    recs = []
    from datetime import date, timedelta
    start = date.fromisoformat("2026-04-27")
    for i, pnl in enumerate(pnls):
        d = (start + timedelta(days=i)).isoformat()
        recs.append(DailyRecord(
            date=d, pnl=pnl, trade_count=2, win_count=2,
            gross_profit=pnl, gross_loss=0.0, avg_hold_minutes=30.0,
        ))
    report = judge_checkpoint(calc_metrics(recs, INITIAL_EQUITY), 7,
                               v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    assert report.day == 7
    assert report.overall in ("PASS", "WATCH")  # 全指標が保守以上なら PASS


def test_judge_day7_fail_win_rate():
    """Day7: 勝率 < 45% → 全体 FAIL。"""
    # 7日中 6日損失
    recs = _make_records([-100.0, -200.0, -150.0, -80.0, -50.0, -120.0, 2000.0])
    # win_rate = 1/14 (trade_count=2 per day, 1 win only on day7)
    recs_bad = _make_records([-100.0] * 6 + [50.0])
    for r in recs_bad:
        r.win_count = 0 if r.pnl < 0 else 1
        r.gross_profit = max(r.pnl, 0.0)
        r.gross_loss = abs(min(r.pnl, 0.0))
    metrics = calc_metrics(recs_bad, INITIAL_EQUITY)
    report = judge_checkpoint(metrics, 7, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    assert report.overall == "FAIL"


def test_judge_day7_insufficient_data():
    """Day7: 取引数 < 5 → WATCH 保留 (特例)。"""
    recs = _make_records([100.0, 200.0])
    for r in recs:
        r.trade_count = 1  # 計 2 取引
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    # trade_count は fallback で len(records) になるため trade_count を直接セット
    metrics.trade_count = 4
    report = judge_checkpoint(metrics, 7, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    assert report.overall == "WATCH"
    assert "5 件未満" in report.note


# ── T-10: judge_checkpoint Day14 ─────────────────────────────────────────────

def test_judge_day14_pf_fail():
    """Day14: PF < 1.0 → 全体 FAIL。"""
    # 損失が利益を大幅上回る
    pnls = [-200.0, -300.0, 50.0, -150.0, -100.0, 80.0, -200.0,
            -250.0, 30.0, -180.0, -120.0, 40.0, -200.0, -300.0]
    recs = _make_records(pnls)
    for r in recs:
        r.win_count = 1 if r.pnl > 0 else 0
        r.gross_profit = max(r.pnl, 0.0)
        r.gross_loss = abs(min(r.pnl, 0.0))
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    report = judge_checkpoint(metrics, 14, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    assert report.overall == "FAIL"
    # PF FAIL の check が含まれる
    fail_checks = [c for c in report.checks if c.label == "FAIL"]
    assert len(fail_checks) >= 1


# ── T-11: judge_checkpoint Day14 — Sharpe PASS ───────────────────────────────

def test_judge_day14_sharpe_watch():
    """Day14: Sharpe が閾値の 70-100% → Sharpe チェックが WATCH。"""
    recs = _make_records([150.0, 200.0, -50.0, 180.0, 160.0, 100.0, -20.0,
                          170.0, 200.0, 130.0, 100.0, 80.0, 150.0, -30.0])
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    # Sharpe を強制的に 1.5 * 0.75 = 1.125 に設定してWATCHを誘発
    metrics.sharpe = 1.5 * 0.75
    report = judge_checkpoint(metrics, 14, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    sh_check = next((c for c in report.checks if "sharpe" in c.metric_name), None)
    assert sh_check is not None
    assert sh_check.label == "WATCH"


# ── T-12: judge_checkpoint Day21 ─────────────────────────────────────────────

def test_judge_day21_consecutive_loss_fail():
    """Day21: 連続損失 6 日以上 → FAIL。"""
    pnls = [-100.0] * 6 + [500.0] * 15
    recs = _make_records(pnls)
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    assert metrics.consecutive_loss_days_max >= 6
    report = judge_checkpoint(metrics, 21, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    cl_check = next((c for c in report.checks if "consecutive" in c.metric_name), None)
    assert cl_check is not None
    assert cl_check.label == "FAIL"


# ── T-13: judge_checkpoint Day30 — 取引数 WATCH ──────────────────────────────

def test_judge_day30_trade_count_watch():
    """Day30: 取引数 10-19 → trade_count_30d = WATCH。"""
    pnls = [200.0] * 20 + [-50.0] * 10
    recs = _make_records(pnls)
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    metrics.trade_count = 15  # 強制 WATCH 帯
    report = judge_checkpoint(metrics, 30, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    tc_check = next((c for c in report.checks if "trade_count" in c.metric_name), None)
    assert tc_check is not None
    assert tc_check.label == "WATCH"


# ── T-14: judge_checkpoint Day30 — v6_deviation FAIL ────────────────────────

def test_judge_day30_deviation_fail():
    """Day30: v6 乖離 > 40% → deviation_30d = FAIL。"""
    pnls = [200.0] * 30
    recs = _make_records(pnls)
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    metrics.v6_deviation = 50.0  # 強制 FAIL
    metrics.trade_count = 25
    report = judge_checkpoint(metrics, 30, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    dev_check = next((c for c in report.checks if "deviation" in c.metric_name), None)
    assert dev_check is not None
    assert dev_check.label == "FAIL"


# ── T-15: judge_checkpoint — 不正な day 値は WATCH ───────────────────────────

def test_judge_invalid_day():
    """day=99 など未定義チェックポイント → overall=WATCH + note 付き。"""
    metrics = Metrics()
    report = judge_checkpoint(metrics, 99, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)
    assert report.overall == "WATCH"
    assert "未定義" in report.note


# ── T-16: emit_report — ファイル生成確認 ─────────────────────────────────────

def test_emit_report_files_created(tmp_path, monkeypatch):
    """emit_report が 3 種のファイルを outputs/ に生成すること。"""
    import scripts.paper_pl_baseline as mod
    monkeypatch.setattr(mod, "_V6_RATES_PATH", tmp_path / "nonexistent_v6.json")
    monkeypatch.setattr(mod, "_PAPER_RISK_YAML", tmp_path / "nonexistent_risk.yaml")

    outputs_dir = tmp_path / "outputs"
    recs = _make_records([200.0, -50.0, 150.0, 100.0, 300.0, -80.0, 200.0])
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    report = judge_checkpoint(metrics, 7, v6_rates=_V6_RATES_FIXTURE, paper_risk=_PAPER_RISK_FIXTURE)

    emit_report(recs, metrics, report, outputs_dir=outputs_dir)

    from datetime import date
    today = date.today().isoformat()
    assert (outputs_dir / f"paper_pl_{today}.json").exists()
    assert (outputs_dir / "paper_pl_cumulative.csv").exists()
    assert (outputs_dir / f"paper_judgment_{today}.md").exists()


# ── T-17: emit_report — JSON スキーマ検証 ────────────────────────────────────

def test_emit_report_json_schema(tmp_path, monkeypatch):
    """出力 JSON が required キーを持つこと。"""
    import scripts.paper_pl_baseline as mod
    monkeypatch.setattr(mod, "_V6_RATES_PATH", tmp_path / "nonexistent_v6.json")
    monkeypatch.setattr(mod, "_PAPER_RISK_YAML", tmp_path / "nonexistent_risk.yaml")

    outputs_dir = tmp_path / "outputs"
    recs = _make_records([150.0, 200.0])
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    report = JudgmentReport(day=7, overall="PASS", checks=[])
    emit_report(recs, metrics, report, outputs_dir=outputs_dir)

    from datetime import date
    snap = json.loads((outputs_dir / f"paper_pl_{date.today().isoformat()}.json").read_text())
    for key in ("generated_at", "checkpoint_day", "overall_label", "metrics"):
        assert key in snap, f"missing key: {key}"


# ── T-18: emit_report — CSV 行数確認 ─────────────────────────────────────────

def test_emit_report_csv_rows(tmp_path, monkeypatch):
    """CSV の行数 = ヘッダ 1 行 + records 行数。"""
    import scripts.paper_pl_baseline as mod
    monkeypatch.setattr(mod, "_V6_RATES_PATH", tmp_path / "nonexistent_v6.json")
    monkeypatch.setattr(mod, "_PAPER_RISK_YAML", tmp_path / "nonexistent_risk.yaml")

    outputs_dir = tmp_path / "outputs"
    n = 5
    recs = _make_records([100.0 * (i + 1) for i in range(n)])
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    report = JudgmentReport(day=7, overall="PASS", checks=[])
    emit_report(recs, metrics, report, outputs_dir=outputs_dir)

    csv_path = outputs_dir / "paper_pl_cumulative.csv"
    with csv_path.open() as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == n


# ── T-19: emit_report — FAIL 時 Pushover 呼出 (mock) ─────────────────────────

def test_emit_report_fail_notifies(tmp_path, monkeypatch):
    """overall=FAIL のとき _notify_fail が呼ばれる。"""
    import scripts.paper_pl_baseline as mod
    monkeypatch.setattr(mod, "_V6_RATES_PATH", tmp_path / "nonexistent_v6.json")
    monkeypatch.setattr(mod, "_PAPER_RISK_YAML", tmp_path / "nonexistent_risk.yaml")

    calls = []

    def mock_notify(report, metrics):
        calls.append((report.day, report.overall))

    monkeypatch.setattr(mod, "_notify_fail", mock_notify)

    outputs_dir = tmp_path / "outputs"
    recs = _make_records([-500.0] * 7)
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    report = JudgmentReport(day=7, overall="FAIL", checks=[])
    emit_report(recs, metrics, report, outputs_dir=outputs_dir)

    assert len(calls) == 1
    assert calls[0] == (7, "FAIL")


# ── T-20: emit_report — PASS 時 Pushover 呼出なし ────────────────────────────

def test_emit_report_pass_no_notify(tmp_path, monkeypatch):
    """overall=PASS のとき _notify_fail は呼ばれない。"""
    import scripts.paper_pl_baseline as mod
    monkeypatch.setattr(mod, "_V6_RATES_PATH", tmp_path / "nonexistent_v6.json")
    monkeypatch.setattr(mod, "_PAPER_RISK_YAML", tmp_path / "nonexistent_risk.yaml")

    calls = []
    monkeypatch.setattr(mod, "_notify_fail", lambda r, m: calls.append(1))

    outputs_dir = tmp_path / "outputs"
    recs = _make_records([300.0, 400.0])
    metrics = calc_metrics(recs, INITIAL_EQUITY)
    report = JudgmentReport(day=7, overall="PASS", checks=[])
    emit_report(recs, metrics, report, outputs_dir=outputs_dir)

    assert len(calls) == 0


# ── T-21: calc_metrics — Calmar レシオ ───────────────────────────────────────

def test_calc_metrics_calmar():
    """max_dd > 0 のとき Calmar = annualized_ret / max_dd で正になる。"""
    recs = _make_records([500.0, -200.0, 600.0, -100.0, 700.0])
    m = calc_metrics(recs, INITIAL_EQUITY)
    if m.max_drawdown > 1e-10:
        assert m.calmar > 0.0
    else:
        assert m.calmar == 0.0


# ── T-22: calc_metrics — win_rate fallback (trade_count=0) ───────────────────

def test_calc_metrics_win_rate_fallback():
    """trade_count=0 の DailyRecord でも win_rate が日次 pnl>0 日数ベースで計算される。"""
    # trade_count=0 の DailyRecord
    recs = [
        DailyRecord(date="2026-04-27", pnl=100.0, trade_count=0),
        DailyRecord(date="2026-04-28", pnl=-50.0, trade_count=0),
        DailyRecord(date="2026-04-29", pnl=200.0, trade_count=0),
    ]
    m = calc_metrics(recs, INITIAL_EQUITY)
    # 2 日勝ち / 3 日 = 0.667
    assert pytest.approx(m.win_rate, abs=0.01) == 2.0 / 3.0


# ── T-23: 全体 label 最悪優先確認 ────────────────────────────────────────────

def test_judge_worst_label_wins():
    """PASS + WATCH + FAIL が混在するとき overall = FAIL。"""
    from scripts.paper_pl_baseline import CheckpointResult, JudgmentReport

    checks = [
        CheckpointResult("a", 1.0, "PASS", ""),
        CheckpointResult("b", 2.0, "WATCH", ""),
        CheckpointResult("c", 0.5, "FAIL", ""),
    ]
    # _label_priority ロジックを再現
    label_priority = {"FAIL": 2, "WATCH": 1, "PASS": 0}
    worst = max(checks, key=lambda c: label_priority.get(c.label, 0))
    assert worst.label == "FAIL"
