"""tests/test_atlas_v3_replay_bt_20260425.py — atlas_v3/ops/replay_bt.py coverage tests

対象: atlas_v3/ops/replay_bt.py (254 stmts)
happy path: 10 件 / error path: 6 件
推定 coverage: ~65%
"""
from __future__ import annotations

import csv
import datetime
import json
from pathlib import Path
from typing import List

import pytest

from atlas_v3.ops.replay_bt import (
    ReplayBacktest,
    ReplayConfig,
    ReplayConfigError,
    TradeSummary,
    TradeRecord,
    WalkForwardResult,
    _add_months,
    run_replay,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: List[dict]) -> None:
    """テスト用 CSV を書き出す。"""
    fieldnames = ["date", "strategy", "dte", "entry_credit", "pnl", "exit_reason", "vix_est"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _make_daily_rows(
    start_date: datetime.date,
    n_days: int,
    pnl: float = 10.0,
    strategy: str = "CS",
) -> List[dict]:
    """n_days 分の取引行を生成する。"""
    rows = []
    for i in range(n_days):
        d = start_date + datetime.timedelta(days=i)
        rows.append({
            "date": d.isoformat(),
            "strategy": strategy,
            "dte": 1,
            "entry_credit": 1.0,
            "pnl": pnl,
            "exit_reason": "target",
            "vix_est": 20.0,
        })
    return rows


# ---------------------------------------------------------------------------
# ReplayConfig — validation
# ---------------------------------------------------------------------------

class TestReplayConfig:
    def test_happy_defaults(self):
        c = ReplayConfig()
        assert c.train_months == 6
        assert c.test_months == 1
        assert c.initial_capital == 10000.0

    def test_happy_custom_params(self):
        c = ReplayConfig(train_months=3, test_months=2, initial_capital=25000.0)
        assert c.train_months == 3
        assert c.initial_capital == 25000.0

    def test_train_months_zero_raises(self):
        with pytest.raises(ValueError, match="train_months must be >= 1"):
            ReplayConfig(train_months=0)

    def test_initial_capital_zero_raises(self):
        with pytest.raises(ValueError, match="initial_capital must be > 0"):
            ReplayConfig(initial_capital=0)

    def test_max_daily_loss_positive_raises(self):
        with pytest.raises(ValueError, match="max_daily_loss_usd must be <= 0"):
            ReplayConfig(max_daily_loss_usd=100.0)

    def test_max_drawdown_zero_raises(self):
        with pytest.raises(ValueError, match="max_drawdown_pct must be in"):
            ReplayConfig(max_drawdown_pct=0.0)


# ---------------------------------------------------------------------------
# TradeRecord.from_row
# ---------------------------------------------------------------------------

class TestTradeRecord:
    def test_happy_full_row(self):
        row = {
            "date": "2025-01-02",
            "strategy": "IC",
            "dte": "1",
            "entry_credit": "1.5",
            "pnl": "20.0",
            "exit_reason": "target",
            "vix_est": "18.0",
        }
        rec = TradeRecord.from_row(row)
        assert rec.date == "2025-01-02"
        assert rec.pnl == 20.0
        assert rec.strategy == "IC"

    def test_happy_minimal_row(self):
        """date / pnl だけあれば残りはデフォルト。"""
        row = {"date": "2025-01-02", "pnl": "5.0"}
        rec = TradeRecord.from_row(row)
        assert rec.strategy == "CS"
        assert rec.vix_est == 20.0

    def test_missing_required_date_raises(self):
        with pytest.raises(ReplayConfigError, match="Required columns missing"):
            TradeRecord.from_row({"pnl": "5.0"})

    def test_nan_pnl_raises(self):
        with pytest.raises(ValueError, match="pnl value is invalid"):
            TradeRecord.from_row({"date": "2025-01-02", "pnl": "nan"})

    def test_inf_pnl_raises(self):
        with pytest.raises(ValueError, match="pnl value is invalid"):
            TradeRecord.from_row({"date": "2025-01-02", "pnl": "inf"})


# ---------------------------------------------------------------------------
# ReplayBacktest.run() — happy path
# ---------------------------------------------------------------------------

class TestReplayBacktestRun:
    def _make_data(self, tmp_path: Path, n_days: int = 200, pnl: float = 10.0) -> Path:
        """train_months=6(≈120 days) を満たす CSV を生成。"""
        rows = _make_daily_rows(datetime.date(2023, 1, 2), n_days, pnl=pnl)
        csv_path = tmp_path / "trades.csv"
        _write_csv(csv_path, rows)
        return csv_path

    def test_happy_basic_run(self, tmp_path):
        """基本的な walk-forward が成功し WalkForwardResult が返る。"""
        csv_path = self._make_data(tmp_path, n_days=250)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "results",
            train_months=6,
            test_months=1,
        )
        bt = ReplayBacktest(config)
        result = bt.run()
        assert isinstance(result, WalkForwardResult)
        assert result.total_trades > 0
        assert result.num_windows >= 1

    def test_happy_positive_pnl_accumulates(self, tmp_path):
        """pnl > 0 のみのデータで final_capital > initial_capital。"""
        csv_path = self._make_data(tmp_path, n_days=250, pnl=10.0)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
            train_months=6,
            test_months=1,
        )
        result = ReplayBacktest(config).run()
        assert result.final_capital > config.initial_capital

    def test_happy_win_rate_all_wins(self, tmp_path):
        """全取引 pnl > 0 なら win_rate == 1.0。"""
        csv_path = self._make_data(tmp_path, n_days=250, pnl=5.0)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
            train_months=6,
            test_months=1,
        )
        result = ReplayBacktest(config).run()
        assert result.win_rate == pytest.approx(1.0)

    def test_happy_strategy_filter(self, tmp_path):
        """strategies フィルタで特定戦略のみ使用できる。

        CS と IC の両戦略を混在させたデータで CS のみフィルタした場合、
        CS のレコードだけで run() が完走し daily_summaries が全て CS になる。
        250 日分を全て CS にして IC を別名で混ぜても walk-forward が回るよう、
        CS データを十分（300 日）用意する。
        """
        # CS 300 日 + IC 50 日（IC はフィルタで除外される）
        rows = (
            _make_daily_rows(datetime.date(2022, 1, 3), 300, strategy="CS")
            + _make_daily_rows(datetime.date(2023, 3, 1), 50, strategy="IC")
        )
        csv_path = tmp_path / "trades.csv"
        _write_csv(csv_path, rows)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
            train_months=6,
            test_months=1,
            strategies=("CS",),
        )
        result = ReplayBacktest(config).run()
        for s in result.daily_summaries:
            assert s.strategy == "CS"

    def test_happy_halt_on_daily_loss_limit(self, tmp_path):
        """日次損失制限 -50 で大損失日が halted になる。"""
        rows = []
        start = datetime.date(2023, 1, 2)
        for i in range(250):
            d = start + datetime.timedelta(days=i)
            # 偶数日は大損失
            pnl = -200.0 if i % 2 == 0 else 5.0
            rows.append({
                "date": d.isoformat(),
                "strategy": "CS",
                "dte": 1,
                "entry_credit": 1.0,
                "pnl": pnl,
                "exit_reason": "sl",
                "vix_est": 20.0,
            })
        csv_path = tmp_path / "trades.csv"
        _write_csv(csv_path, rows)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
            train_months=6,
            test_months=1,
            max_daily_loss_usd=-50.0,
        )
        result = ReplayBacktest(config).run()
        assert result.halted_days > 0

    def test_happy_save_creates_json(self, tmp_path):
        """save() が JSON ファイルを生成し、内容が WalkForwardResult と一致する。"""
        csv_path = self._make_data(tmp_path, n_days=250)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "results",
            train_months=6,
            test_months=1,
        )
        bt = ReplayBacktest(config)
        result = bt.run()
        saved_path = bt.save(result, label="test")
        assert saved_path.exists()
        data = json.loads(saved_path.read_text(encoding="utf-8"))
        assert data["total_trades"] == result.total_trades
        assert "daily_summaries" in data

    def test_happy_walk_forward_result_to_dict(self, tmp_path):
        """WalkForwardResult.to_dict() が serializable な dict を返す。"""
        csv_path = self._make_data(tmp_path, n_days=250)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
            train_months=6,
            test_months=1,
        )
        result = ReplayBacktest(config).run()
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "start_date" in d
        assert "final_capital" in d

    def test_happy_sharpe_zero_for_single_sample(self):
        """サンプル数 < 2 の場合 Sharpe は 0.0。"""
        sharpe = ReplayBacktest._compute_sharpe([100.0], initial_capital=10000.0)
        assert sharpe == 0.0

    def test_happy_sharpe_positive_for_varied_positive(self):
        """平均 > 0 かつ分散 > 0 のリターン列なら Sharpe > 0。

        全値が同一だと std=0 となり Sharpe=0.0 になる（仕様）。
        意図的に微小な変動を持たせて std > 0 を保証する。
        """
        import math
        # 10.0, 11.0, 9.0, ... と交互に変動させて std > 0 を保証
        daily_pnls = [10.0 + (i % 3 - 1) * 2.0 for i in range(30)]
        sharpe = ReplayBacktest._compute_sharpe(daily_pnls, initial_capital=10000.0)
        assert sharpe > 0.0

    # --- error path ---

    def test_data_file_not_found_raises(self, tmp_path):
        """データファイルが存在しない場合 FileNotFoundError。"""
        config = ReplayConfig(
            data_path=tmp_path / "no_such.csv",
            results_dir=tmp_path / "r",
        )
        with pytest.raises(FileNotFoundError):
            ReplayBacktest(config).run()

    def test_empty_csv_raises(self, tmp_path):
        """データが 0 件なら ValueError。"""
        csv_path = tmp_path / "empty.csv"
        _write_csv(csv_path, [])
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
        )
        with pytest.raises(ValueError, match="No trade records loaded"):
            ReplayBacktest(config).run()

    def test_insufficient_data_for_windows_raises(self, tmp_path):
        """train_months=6 に満たない日数では ReplayConfigError。"""
        rows = _make_daily_rows(datetime.date(2023, 1, 2), n_days=30)
        csv_path = tmp_path / "trades.csv"
        _write_csv(csv_path, rows)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
            train_months=6,
        )
        with pytest.raises(ReplayConfigError, match="Insufficient data"):
            ReplayBacktest(config).run()

    def test_strategy_filter_no_match_raises(self, tmp_path):
        """フィルタ後に 0 件になると ValueError。"""
        csv_path = self._make_data(tmp_path, n_days=250)
        config = ReplayConfig(
            data_path=csv_path,
            results_dir=tmp_path / "r",
            train_months=6,
            test_months=1,
            strategies=("NO_SUCH_STRATEGY",),
        )
        with pytest.raises(ValueError, match="No records after strategy filter"):
            ReplayBacktest(config).run()

    def test_nan_pnl_row_strict_mode_raises(self, tmp_path):
        """strict=True（デフォルト）で pnl=nan の行が含まれる場合 ReplayConfigError。"""
        rows = _make_daily_rows(datetime.date(2023, 1, 2), n_days=5)
        rows.append({"date": "2023-01-10", "strategy": "CS", "dte": "1",
                     "entry_credit": "1.0", "pnl": "nan", "exit_reason": "", "vix_est": "20"})
        csv_path = tmp_path / "nan.csv"
        _write_csv(csv_path, rows)
        config = ReplayConfig(data_path=csv_path, results_dir=tmp_path / "r")
        bt = ReplayBacktest(config)
        with pytest.raises(ReplayConfigError):
            bt._load_records(strict=True)

    def test_nan_pnl_row_non_strict_skips(self, tmp_path):
        """strict=False では pnl=nan の行はスキップされる。"""
        rows = _make_daily_rows(datetime.date(2023, 1, 2), n_days=5)
        rows.append({"date": "2023-01-10", "strategy": "CS", "dte": "1",
                     "entry_credit": "1.0", "pnl": "nan", "exit_reason": "", "vix_est": "20"})
        csv_path = tmp_path / "nan.csv"
        _write_csv(csv_path, rows)
        config = ReplayConfig(data_path=csv_path, results_dir=tmp_path / "r")
        bt = ReplayBacktest(config)
        records = bt._load_records(strict=False)
        assert len(records) == 5  # nan 行はスキップ


# ---------------------------------------------------------------------------
# _add_months — utility
# ---------------------------------------------------------------------------

class TestAddMonths:
    def test_basic(self):
        d = datetime.date(2023, 1, 15)
        assert _add_months(d, 1) == datetime.date(2023, 2, 15)

    def test_year_boundary(self):
        d = datetime.date(2023, 11, 1)
        assert _add_months(d, 2) == datetime.date(2024, 1, 1)

    def test_month_end_clamp(self):
        """月末補正: 1/31 + 1 ヶ月 = 2/28（閏年でない場合）。"""
        d = datetime.date(2023, 1, 31)
        result = _add_months(d, 1)
        assert result == datetime.date(2023, 2, 28)


# ---------------------------------------------------------------------------
# run_replay — entry point
# ---------------------------------------------------------------------------

class TestRunReplay:
    def test_happy_run_replay(self, tmp_path):
        """run_replay() が WalkForwardResult を返す。"""
        rows = _make_daily_rows(datetime.date(2023, 1, 2), n_days=250)
        csv_path = tmp_path / "t.csv"
        _write_csv(csv_path, rows)
        # save_result=False でファイル書込をスキップ
        result = run_replay(data_path=csv_path, save_result=False)
        assert isinstance(result, WalkForwardResult)
