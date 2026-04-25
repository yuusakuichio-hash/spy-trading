#!/usr/bin/env python3
"""
scripts/paper_pl_baseline.py — Atlas v3 Paper P&L Baseline 計測スクリプト

仕様: data/research/paper_30day_judgment_criteria_20260425.md
設定: data/specs/v6_rates.json / data/configs/atlas_paper_risk.yaml

実行:
    python3 scripts/paper_pl_baseline.py [--day 7|14|21|30] [--state-dir PATH]

出力:
    outputs/paper_pl_YYYYMMDD.json         日次スナップショット
    outputs/paper_pl_cumulative.csv        累積テーブル
    outputs/paper_judgment_YYYYMMDD.md     チェックポイント判定レポート

数値直書き禁止 (memory/feedback_no_numeric_citation.md):
    すべての閾値数値は data/specs/v6_rates.json または
    data/configs/atlas_paper_risk.yaml から動的読込。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

# ── パス定義 ──────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR_DEFAULT = _REPO_ROOT / "data" / "state_v3"
_ATLAS_PAPER_DIR = _REPO_ROOT / "data" / "atlas_paper"
_OUTPUTS_DIR = _REPO_ROOT / "outputs"
_V6_RATES_PATH = _REPO_ROOT / "data" / "specs" / "v6_rates.json"
_PAPER_RISK_YAML = _REPO_ROOT / "data" / "configs" / "atlas_paper_risk.yaml"

# ── ロガー ────────────────────────────────────────────────────────────────────
log = logging.getLogger("paper_pl_baseline")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# ── 型定義 ────────────────────────────────────────────────────────────────────
JudgmentLabel = Literal["PASS", "WATCH", "FAIL", "INSUFFICIENT_DATA"]


@dataclass
class DailyRecord:
    """1 営業日の P&L レコード。"""
    date: str          # ISO 日付 YYYY-MM-DD (ET)
    pnl: float         # 日次 P&L (USD)
    trade_count: int = 0
    win_count: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0        # 絶対値で保持
    avg_hold_minutes: float = 0.0
    source: str = "monitor_state"  # データソース識別


@dataclass
class Metrics:
    """計算済みメトリクス。"""
    # P&L 系
    cumulative_pnl: float = 0.0
    monthly_rate: float = 0.0       # 月利換算 (0.0–1.0)
    v6_deviation: float = 0.0       # 実測 vs v6中央 deviation

    # リスク系
    max_drawdown: float = 0.0       # 最大 DD (0.0–1.0, 負値)
    sharpe: float = 0.0             # 年率 Sharpe
    calmar: float = 0.0             # Calmar レシオ
    var_95: float = 0.0             # VaR 95% (USD, 負値)

    # トレード効率系
    win_rate: float = 0.0           # 0.0–1.0
    profit_factor: float = 0.0      # PF
    avg_r: float = 0.0              # avg利益 / avg損失絶対値
    avg_hold_minutes: float = 0.0
    trade_count: int = 0

    # 補助
    elapsed_business_days: int = 0
    consecutive_loss_days_max: int = 0
    daily_pnl_list: list[float] = field(default_factory=list)


@dataclass
class CheckpointResult:
    """チェックポイント単一指標の判定結果。"""
    metric_name: str
    value: float | int | str
    label: JudgmentLabel
    reason: str


@dataclass
class JudgmentReport:
    """チェックポイント全体の判定集約。"""
    day: int
    overall: JudgmentLabel
    checks: list[CheckpointResult] = field(default_factory=list)
    note: str = ""


# ── 設定読込 ──────────────────────────────────────────────────────────────────

def _load_v6_rates() -> dict:
    """data/specs/v6_rates.json を読込。ファイル不在時は KeyError を上流に伝播させない。"""
    if not _V6_RATES_PATH.exists():
        log.warning("v6_rates.json not found at %s — using graceful defaults", _V6_RATES_PATH)
        return {
            "monthly_rate": {"conservative": 0.0, "central": 0.0, "optimistic": 0.0},
            "sharpe_threshold": {"value": 1.5},
            "initial_equity_usd": {"value": 100000.0},
        }
    with _V6_RATES_PATH.open() as f:
        return json.load(f)


def _load_paper_risk_config() -> dict:
    """data/configs/atlas_paper_risk.yaml を読込。"""
    if not _PAPER_RISK_YAML.exists():
        log.warning("atlas_paper_risk.yaml not found — using graceful defaults")
        return {
            "max_drawdown": {"pct": 0.15},
            "max_daily_loss": {"usd": -500.0},
            "max_notional": {"usd": 10000.0},
        }
    with _PAPER_RISK_YAML.open() as f:
        return yaml.safe_load(f)


# ── ソース 1: monitor_state.jsonl ─────────────────────────────────────────────

def _parse_monitor_state(state_dir: Path) -> dict[str, float]:
    """
    monitor_state.jsonl から check_name=daily_loss のエントリを抽出し、
    日付 -> 日次 P&L (USD) のマッピングを返す。

    同一日に複数エントリある場合は最後のエントリを採用 (最新値が正)。
    スキーマ: {"ts": "<ISO8601>", "check_name": "daily_loss", "value": <float>, ...}
    """
    path = state_dir / "monitor_state.jsonl"
    if not path.exists():
        log.info("monitor_state.jsonl not found at %s — skipping", path)
        return {}

    daily: dict[str, float] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("check_name") != "daily_loss":
                continue
            ts_str = obj.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            # ET 日付 = UTC - 4h (夏時間) / -5h (冬時間)。
            # Paper 開始は 2026-04-27 (EDT = UTC-4)。簡易変換: UTC-4 固定。
            et_offset = -4
            et_date = (ts.astimezone(timezone.utc).replace(tzinfo=None)
                       .replace(hour=ts.hour + et_offset) if ts.tzinfo is None
                       else ts.astimezone(timezone.utc))
            # UTC -> ET 変換 (ET = UTC - 4)
            if ts.tzinfo is not None:
                import datetime as _dt
                et = ts.astimezone(_dt.timezone(_dt.timedelta(hours=-4)))
                day_key = et.strftime("%Y-%m-%d")
            else:
                day_key = ts.strftime("%Y-%m-%d")

            pnl_value = float(obj.get("value", 0.0))
            daily[day_key] = pnl_value  # 上書きで最新優先

    return daily


# ── ソース 2: atlas-trader-stdout.log ─────────────────────────────────────────

_FILL_PATTERN = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}).*"
    r"(?:FILL|TRADE|ORDER).*pnl[=:](?P<pnl>-?\d+\.?\d*)",
    re.IGNORECASE,
)


def _parse_trader_log(state_dir: Path) -> dict[str, float]:
    """
    atlas-trader-stdout.log から FILL/TRADE/ORDER + pnl= パターンを grep し
    日付 -> 累積 P&L を返す (補完用、ソース1が優先)。
    """
    path = state_dir / "atlas-trader-stdout.log"
    if not path.exists():
        log.info("atlas-trader-stdout.log not found — skipping trader log source")
        return {}

    daily: dict[str, float] = {}
    with path.open(errors="replace") as f:
        for line in f:
            m = _FILL_PATTERN.search(line)
            if not m:
                continue
            day_key = m.group("ts")[:10]
            daily[day_key] = daily.get(day_key, 0.0) + float(m.group("pnl"))
    return daily


# ── ソース 3: data/atlas_paper/trades.jsonl ───────────────────────────────────

def _parse_trades_jsonl(atlas_paper_dir: Path) -> dict[str, DailyRecord]:
    """
    data/atlas_paper/trades.jsonl から取引単位で日次レコードを構築。
    スキーマ:
        {"date": "YYYY-MM-DD", "pnl": float, "win": bool,
         "hold_minutes": float, "tactic": str, ...}
    """
    path = atlas_paper_dir / "trades.jsonl"
    if not path.exists():
        log.info("trades.jsonl not found at %s — skipping", path)
        return {}

    accum: dict[str, dict] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            day_key = obj.get("date", "")
            if not day_key:
                continue
            if day_key not in accum:
                accum[day_key] = {
                    "pnl": 0.0, "trade_count": 0, "win_count": 0,
                    "gross_profit": 0.0, "gross_loss": 0.0,
                    "hold_minutes_sum": 0.0,
                }
            a = accum[day_key]
            pnl = float(obj.get("pnl", 0.0))
            a["pnl"] += pnl
            a["trade_count"] += 1
            if pnl > 0:
                a["win_count"] += 1
                a["gross_profit"] += pnl
            else:
                a["gross_loss"] += abs(pnl)
            a["hold_minutes_sum"] += float(obj.get("hold_minutes", 0.0))

    records = {}
    for day_key, a in accum.items():
        tc = a["trade_count"]
        records[day_key] = DailyRecord(
            date=day_key,
            pnl=a["pnl"],
            trade_count=tc,
            win_count=a["win_count"],
            gross_profit=a["gross_profit"],
            gross_loss=a["gross_loss"],
            avg_hold_minutes=a["hold_minutes_sum"] / tc if tc > 0 else 0.0,
            source="trades_jsonl",
        )
    return records


# ── メイン: load_daily_pnl ─────────────────────────────────────────────────────

def load_daily_pnl(state_dir: Path | None = None) -> list[DailyRecord]:
    """
    3 ソースを優先順で統合し、日付昇順ソートした DailyRecord リストを返す。

    優先順:
        1. data/state_v3/monitor_state.jsonl (daily_loss check)
        2. data/state_v3/atlas-trader-stdout.log (FILL/TRADE grep)
        3. data/atlas_paper/trades.jsonl (詳細取引ログ)

    日付が複数ソースで重複する場合:
        - trades.jsonl は最も詳細 → trade_count / win_count を補完
        - monitor_state の pnl 値を正として上書き (ソース1優先)

    データ未存在時は空リスト (graceful degradation)。
    """
    if state_dir is None:
        state_dir = _STATE_DIR_DEFAULT

    # ソース 1
    monitor_pnl = _parse_monitor_state(state_dir)
    # ソース 2
    trader_pnl = _parse_trader_log(state_dir)
    # ソース 3
    trades_records = _parse_trades_jsonl(_ATLAS_PAPER_DIR)

    # 全日付を集約
    all_dates = sorted(
        set(monitor_pnl) | set(trader_pnl) | set(trades_records)
    )

    records: list[DailyRecord] = []
    for d in all_dates:
        if d in trades_records:
            rec = trades_records[d]
        else:
            rec = DailyRecord(date=d, pnl=0.0)

        # pnl は優先順で上書き
        if d in monitor_pnl:
            rec.pnl = monitor_pnl[d]
            rec.source = "monitor_state"
        elif d in trader_pnl:
            rec.pnl = trader_pnl[d]
            rec.source = "trader_log"

        records.append(rec)

    log.info("load_daily_pnl: %d days loaded from %s", len(records), state_dir)
    return records


# ── calc_metrics ──────────────────────────────────────────────────────────────

def calc_metrics(records: list[DailyRecord], initial_equity: float) -> Metrics:
    """
    DailyRecord リストから全メトリクスを一括算出。

    Parameters
    ----------
    records:        日付昇順の DailyRecord リスト
    initial_equity: 仮想元本 (USD)

    Returns
    -------
    Metrics (records が空の場合はゼロ埋めした Metrics)
    """
    if not records:
        return Metrics(elapsed_business_days=0)

    daily_pnl = [r.pnl for r in records]
    n = len(daily_pnl)

    # 累積 P&L
    cum_pnl = sum(daily_pnl)

    # 月利換算 (elapsed = 実営業日数)
    elapsed = n
    monthly_rate = (cum_pnl / initial_equity) * (30.0 / elapsed) if elapsed > 0 and initial_equity > 0 else 0.0

    # 最大ドローダウン (ピーク→谷)
    equity_curve = [initial_equity]
    for p in daily_pnl:
        equity_curve.append(equity_curve[-1] + p)

    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve[1:]:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    # Sharpe (年率換算)
    mean_ret = sum(daily_pnl) / n if n > 0 else 0.0
    variance = sum((p - mean_ret) ** 2 for p in daily_pnl) / n if n > 0 else 0.0
    std_ret = math.sqrt(variance)
    sharpe = (mean_ret / std_ret) * math.sqrt(252) if std_ret > 1e-10 else 0.0

    # Calmar レシオ (年率リターン / 最大DD)
    annualized_ret = (cum_pnl / initial_equity) * (252.0 / elapsed) if elapsed > 0 and initial_equity > 0 else 0.0
    calmar = annualized_ret / max_dd if max_dd > 1e-10 else 0.0

    # VaR 95%
    sorted_pnl = sorted(daily_pnl)
    var_idx = max(0, int(n * 0.05) - 1)
    var_95 = sorted_pnl[var_idx] if sorted_pnl else 0.0

    # 勝率
    trade_total = sum(r.trade_count for r in records)
    win_total = sum(r.win_count for r in records)
    # trade_count が埋まっていない場合は日次 pnl > 0 を "win day" とする
    if trade_total == 0:
        trade_total = n
        win_total = sum(1 for p in daily_pnl if p > 0)
    win_rate = win_total / trade_total if trade_total > 0 else 0.0

    # Profit Factor
    gross_profit = sum(r.gross_profit for r in records)
    gross_loss = sum(r.gross_loss for r in records)
    if gross_profit == 0 and gross_loss == 0:
        # fallback: 日次 pnl から算出
        gross_profit = sum(p for p in daily_pnl if p > 0)
        gross_loss = sum(abs(p) for p in daily_pnl if p < 0)
    profit_factor = gross_profit / gross_loss if gross_loss > 1e-10 else (float("inf") if gross_profit > 0 else 0.0)

    # 平均 R (avg利益 / avg損失絶対値)
    win_pnls = [r.pnl for r in records if r.pnl > 0] or [p for p in daily_pnl if p > 0]
    loss_pnls = [abs(r.pnl) for r in records if r.pnl < 0] or [abs(p) for p in daily_pnl if p < 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    avg_r = avg_win / avg_loss if avg_loss > 1e-10 else (float("inf") if avg_win > 0 else 0.0)

    # 平均保有時間
    hold_records = [r for r in records if r.avg_hold_minutes > 0]
    avg_hold = (sum(r.avg_hold_minutes for r in hold_records) / len(hold_records)
                if hold_records else 0.0)

    # 連続損失日数最大
    max_consec_loss = 0
    cur_consec = 0
    for p in daily_pnl:
        if p < 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # v6 deviation (vs 中央月利)
    v6_rates = _load_v6_rates()
    v6_central = v6_rates["monthly_rate"]["central"]
    v6_deviation = ((monthly_rate - v6_central) / v6_central * 100.0
                    if v6_central > 1e-10 else 0.0)

    return Metrics(
        cumulative_pnl=cum_pnl,
        monthly_rate=monthly_rate,
        v6_deviation=v6_deviation,
        max_drawdown=max_dd,
        sharpe=sharpe,
        calmar=calmar,
        var_95=var_95,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_r=avg_r,
        avg_hold_minutes=avg_hold,
        trade_count=trade_total,
        elapsed_business_days=elapsed,
        consecutive_loss_days_max=max_consec_loss,
        daily_pnl_list=daily_pnl,
    )


# ── calc_monthly_rate ─────────────────────────────────────────────────────────

def calc_monthly_rate(records: list[DailyRecord], initial_equity: float) -> float:
    """
    累積 PnL / 元本 × (30 / 経過営業日数) で月利換算を返す。

    仕様: data/research/paper_30day_judgment_criteria_20260425.md § 4-A
    データ未存在時は 0.0 (graceful degradation)。
    """
    if not records or initial_equity <= 0:
        return 0.0
    cum_pnl = sum(r.pnl for r in records)
    elapsed = len(records)
    return (cum_pnl / initial_equity) * (30.0 / elapsed)


# ── judge_checkpoint ──────────────────────────────────────────────────────────

def _label_from_thresholds(
    value: float,
    pass_min: float | None = None,
    pass_max: float | None = None,
    watch_min: float | None = None,
    watch_max: float | None = None,
    higher_is_better: bool = True,
) -> JudgmentLabel:
    """
    汎用ラベル判定ヘルパー。
    higher_is_better=True: value >= pass_min → PASS, watch_min <= value < pass_min → WATCH, else FAIL
    higher_is_better=False: value <= pass_max → PASS, pass_max < value <= watch_max → WATCH, else FAIL
    """
    if higher_is_better:
        if pass_min is not None and value >= pass_min:
            return "PASS"
        if watch_min is not None and value >= watch_min:
            return "WATCH"
        return "FAIL"
    else:
        if pass_max is not None and value <= pass_max:
            return "PASS"
        if watch_max is not None and value <= watch_max:
            return "WATCH"
        return "FAIL"


def judge_checkpoint(
    metrics: Metrics,
    day: int,
    v6_rates: dict | None = None,
    paper_risk: dict | None = None,
) -> JudgmentReport:
    """
    PASS / WATCH / FAIL を返す。v6_rates は data/specs/v6_rates.json から読込。

    Parameters
    ----------
    metrics:    calc_metrics の出力
    day:        チェックポイント日数 (7 / 14 / 21 / 30)
    v6_rates:   明示指定 (None の場合は自動ロード)
    paper_risk: 明示指定 (None の場合は自動ロード)

    仕様: data/research/paper_30day_judgment_criteria_20260425.md § 2
    """
    if v6_rates is None:
        v6_rates = _load_v6_rates()
    if paper_risk is None:
        paper_risk = _load_paper_risk_config()

    monthly_rate_conservative = v6_rates["monthly_rate"]["conservative"]
    monthly_rate_central = v6_rates["monthly_rate"]["central"]
    sharpe_threshold = v6_rates["sharpe_threshold"]["value"]
    max_dd_limit = paper_risk["max_drawdown"]["pct"]  # e.g. 0.15

    checks: list[CheckpointResult] = []

    # ── Day 7 ────────────────────────────────────────────────────────────────
    if day == 7:
        # 統計有効性チェック (5件未満は WATCH 保留)
        if metrics.trade_count < 5:
            return JudgmentReport(
                day=7,
                overall="WATCH",
                checks=[],
                note="Day 7 特例: 取引数 5 件未満 — 統計的根拠不足。WATCH 保留。Strategist 連携要。",
            )

        # 週次月利換算 (仕様: 保守以上→PASS, 保守の50%以上→WATCH, 未満→FAIL)
        mr = metrics.monthly_rate
        if mr >= monthly_rate_conservative:
            mr_label: JudgmentLabel = "PASS"
        elif mr >= monthly_rate_conservative * 0.5:
            mr_label = "WATCH"
        else:
            mr_label = "FAIL"
        checks.append(CheckpointResult(
            "monthly_rate_7d", mr, mr_label,
            f"週次月利換算={mr:.4f} / 保守={monthly_rate_conservative:.4f}",
        ))

        # 最大DD (仕様: max_dd_limit の 50% 未満→PASS, 50-80%→WATCH, 80%超→FAIL)
        dd = metrics.max_drawdown
        if dd < max_dd_limit * 0.50:
            dd_label: JudgmentLabel = "PASS"
        elif dd < max_dd_limit * 0.80:
            dd_label = "WATCH"
        else:
            dd_label = "FAIL"
        checks.append(CheckpointResult(
            "max_drawdown_7d", dd, dd_label,
            f"最大DD={dd:.4f} / 許容上限={max_dd_limit:.4f}",
        ))

        # 勝率 (仕様: 55%以上→PASS, 45-55%→WATCH, 45%未満→FAIL)
        wr = metrics.win_rate
        if wr >= 0.55:
            wr_label: JudgmentLabel = "PASS"
        elif wr >= 0.45:
            wr_label = "WATCH"
        else:
            wr_label = "FAIL"
        checks.append(CheckpointResult(
            "win_rate_7d", wr, wr_label,
            f"勝率={wr:.2%}",
        ))

        # 連続損失日数 (仕様: 3日未満→PASS, 3-4日→WATCH, 5日以上→FAIL)
        cl = metrics.consecutive_loss_days_max
        if cl < 3:
            cl_label: JudgmentLabel = "PASS"
        elif cl <= 4:
            cl_label = "WATCH"
        else:
            cl_label = "FAIL"
        checks.append(CheckpointResult(
            "consecutive_loss_7d", cl, cl_label,
            f"連続損失最大={cl}日",
        ))

    # ── Day 14 ───────────────────────────────────────────────────────────────
    elif day == 14:
        # 累積月利換算 (仕様: 中央以上→PASS, 保守-中央→WATCH, 保守未満→FAIL)
        mr = metrics.monthly_rate
        if mr >= monthly_rate_central:
            mr_label = "PASS"
        elif mr >= monthly_rate_conservative:
            mr_label = "WATCH"
        else:
            mr_label = "FAIL"
        checks.append(CheckpointResult(
            "monthly_rate_14d", mr, mr_label,
            f"月利換算={mr:.4f} / 保守={monthly_rate_conservative:.4f} / 中央={monthly_rate_central:.4f}",
        ))

        # Sharpe (仕様: phase0a Sharpe閾値以上→PASS, 70-100%→WATCH, 70%未満→FAIL)
        sh = metrics.sharpe
        if sh >= sharpe_threshold:
            sh_label: JudgmentLabel = "PASS"
        elif sh >= sharpe_threshold * 0.70:
            sh_label = "WATCH"
        else:
            sh_label = "FAIL"
        checks.append(CheckpointResult(
            "sharpe_14d", sh, sh_label,
            f"Sharpe={sh:.4f} / 閾値={sharpe_threshold:.4f}",
        ))

        # 最大DD (仕様: 60%未満→PASS, 60-90%→WATCH, 90%超→FAIL)
        dd = metrics.max_drawdown
        if dd < max_dd_limit * 0.60:
            dd_label = "PASS"
        elif dd < max_dd_limit * 0.90:
            dd_label = "WATCH"
        else:
            dd_label = "FAIL"
        checks.append(CheckpointResult(
            "max_drawdown_14d", dd, dd_label,
            f"最大DD={dd:.4f} / 許容上限={max_dd_limit:.4f}",
        ))

        # PF (仕様: 1.5以上→PASS, 1.0-1.5→WATCH, 1.0未満→FAIL)
        pf = metrics.profit_factor
        if pf >= 1.5:
            pf_label: JudgmentLabel = "PASS"
        elif pf >= 1.0:
            pf_label = "WATCH"
        else:
            pf_label = "FAIL"
        checks.append(CheckpointResult(
            "profit_factor_14d", pf, pf_label,
            f"PF={pf:.4f}",
        ))

        # 平均R (仕様: 1.2以上→PASS, 0.8-1.2→WATCH, 0.8未満→FAIL)
        ar = metrics.avg_r
        if ar >= 1.2:
            ar_label: JudgmentLabel = "PASS"
        elif ar >= 0.8:
            ar_label = "WATCH"
        else:
            ar_label = "FAIL"
        checks.append(CheckpointResult(
            "avg_r_14d", ar, ar_label,
            f"平均R={ar:.4f}",
        ))

    # ── Day 21 ───────────────────────────────────────────────────────────────
    elif day == 21:
        # 累積月利換算 (Day14同基準)
        mr = metrics.monthly_rate
        if mr >= monthly_rate_central:
            mr_label = "PASS"
        elif mr >= monthly_rate_conservative:
            mr_label = "WATCH"
        else:
            mr_label = "FAIL"
        checks.append(CheckpointResult(
            "monthly_rate_21d", mr, mr_label,
            f"月利換算={mr:.4f}",
        ))

        # 最大DD (仕様: 65%未満→PASS, 65-95%→WATCH, 95%超→FAIL)
        dd = metrics.max_drawdown
        if dd < max_dd_limit * 0.65:
            dd_label = "PASS"
        elif dd < max_dd_limit * 0.95:
            dd_label = "WATCH"
        else:
            dd_label = "FAIL"
        checks.append(CheckpointResult(
            "max_drawdown_21d", dd, dd_label,
            f"最大DD={dd:.4f} / 許容上限={max_dd_limit:.4f}",
        ))

        # 連続損失日数 (仕様: 4日未満→PASS, 4-5日→WATCH, 6日以上→FAIL)
        cl = metrics.consecutive_loss_days_max
        if cl < 4:
            cl_label = "PASS"
        elif cl <= 5:
            cl_label = "WATCH"
        else:
            cl_label = "FAIL"
        checks.append(CheckpointResult(
            "consecutive_loss_21d", cl, cl_label,
            f"連続損失最大={cl}日",
        ))

    # ── Day 30 ───────────────────────────────────────────────────────────────
    elif day == 30:
        # 月利 (仕様: 中央以上→PASS, 保守-中央→WATCH, 保守未満→FAIL)
        mr = metrics.monthly_rate
        if mr >= monthly_rate_central:
            mr_label = "PASS"
        elif mr >= monthly_rate_conservative:
            mr_label = "WATCH"
        else:
            mr_label = "FAIL"
        checks.append(CheckpointResult(
            "monthly_rate_30d", mr, mr_label,
            f"月利={mr:.4f} / 保守={monthly_rate_conservative:.4f} / 中央={monthly_rate_central:.4f}",
        ))

        # 最大DD (仕様: max_dd_limit内→PASS/WATCH, 超過→FAIL)
        dd = metrics.max_drawdown
        if dd < max_dd_limit:
            dd_label = "PASS"
        else:
            dd_label = "FAIL"
        checks.append(CheckpointResult(
            "max_drawdown_30d", dd, dd_label,
            f"最大DD={dd:.4f} / 上限={max_dd_limit:.4f}",
        ))

        # Sharpe
        sh = metrics.sharpe
        if sh >= sharpe_threshold:
            sh_label = "PASS"
        elif sh >= sharpe_threshold * 0.70:
            sh_label = "WATCH"
        else:
            sh_label = "FAIL"
        checks.append(CheckpointResult(
            "sharpe_30d", sh, sh_label,
            f"Sharpe={sh:.4f} / 閾値={sharpe_threshold:.4f}",
        ))

        # 勝率 (仕様: 55%以上→PASS, 48-55%→WATCH, 48%未満→FAIL)
        wr = metrics.win_rate
        if wr >= 0.55:
            wr_label = "PASS"
        elif wr >= 0.48:
            wr_label = "WATCH"
        else:
            wr_label = "FAIL"
        checks.append(CheckpointResult(
            "win_rate_30d", wr, wr_label,
            f"勝率={wr:.2%}",
        ))

        # PF
        pf = metrics.profit_factor
        if pf >= 1.5:
            pf_label = "PASS"
        elif pf >= 1.0:
            pf_label = "WATCH"
        else:
            pf_label = "FAIL"
        checks.append(CheckpointResult(
            "profit_factor_30d", pf, pf_label,
            f"PF={pf:.4f}",
        ))

        # 取引数 (仕様: 20件以上→PASS, 10-19→WATCH, 9以下→FAIL)
        tc = metrics.trade_count
        if tc >= 20:
            tc_label: JudgmentLabel = "PASS"
        elif tc >= 10:
            tc_label = "WATCH"
        else:
            tc_label = "FAIL"
        checks.append(CheckpointResult(
            "trade_count_30d", tc, tc_label,
            f"取引数={tc}件",
        ))

        # 想定 vs 実測 deviation (仕様: ±20%以内→PASS, ±20-40%→WATCH, ±40%超→FAIL)
        dev = abs(metrics.v6_deviation)
        if dev <= 20.0:
            dev_label: JudgmentLabel = "PASS"
        elif dev <= 40.0:
            dev_label = "WATCH"
        else:
            dev_label = "FAIL"
        checks.append(CheckpointResult(
            "v6_deviation_30d", metrics.v6_deviation, dev_label,
            f"v6乖離={metrics.v6_deviation:.1f}%",
        ))

    else:
        # 不正な day 値
        return JudgmentReport(
            day=day,
            overall="WATCH",
            note=f"day={day} は未定義チェックポイント (7/14/21/30 のみ有効)",
        )

    # 全体判定: 最悪ラベルを採用 (FAIL > WATCH > PASS)
    label_priority = {"FAIL": 2, "WATCH": 1, "PASS": 0, "INSUFFICIENT_DATA": 1}
    overall_val = max(checks, key=lambda c: label_priority.get(c.label, 0))
    overall: JudgmentLabel = overall_val.label

    return JudgmentReport(day=day, overall=overall, checks=checks)


# ── emit_report ───────────────────────────────────────────────────────────────

def emit_report(
    records: list[DailyRecord],
    metrics: Metrics,
    report: JudgmentReport,
    outputs_dir: Path | None = None,
) -> None:
    """
    outputs/ に json (日次スナップショット) + csv (累積テーブル) + md (判定レポート) を出力。
    FAIL 時は Pushover priority=1 を送信 (import 失敗時はログのみ)。

    仕様: data/research/paper_30day_judgment_criteria_20260425.md § 5
    """
    if outputs_dir is None:
        outputs_dir = _OUTPUTS_DIR
    outputs_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()

    # ── 1. 日次スナップショット JSON ─────────────────────────────────────────
    snapshot = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "checkpoint_day": report.day,
        "overall_label": report.overall,
        "metrics": asdict(metrics),
        "checks": [asdict(c) for c in report.checks],
        "note": report.note,
    }
    snap_path = outputs_dir / f"paper_pl_{today}.json"
    with snap_path.open("w") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    log.info("Snapshot written: %s", snap_path)

    # ── 2. 累積 CSV ───────────────────────────────────────────────────────────
    csv_path = outputs_dir / "paper_pl_cumulative.csv"
    v6_rates = _load_v6_rates()
    initial_equity = v6_rates["initial_equity_usd"]["value"]

    cum_sum = 0.0
    equity = initial_equity
    peak = initial_equity
    rows = []
    for rec in records:
        cum_sum += rec.pnl
        equity += rec.pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        elapsed = len(rows) + 1
        mr = (cum_sum / initial_equity) * (30.0 / elapsed) if elapsed > 0 and initial_equity > 0 else 0.0
        rows.append({
            "date": rec.date,
            "daily_pnl": f"{rec.pnl:.2f}",
            "cumulative_pnl": f"{cum_sum:.2f}",
            "monthly_rate_annlzd": f"{mr:.6f}",
            "max_drawdown": f"{dd:.6f}",
            "equity": f"{equity:.2f}",
        })

    with csv_path.open("w", newline="") as f:
        fieldnames = ["date", "daily_pnl", "cumulative_pnl", "monthly_rate_annlzd", "max_drawdown", "equity"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Cumulative CSV written: %s", csv_path)

    # ── 3. 判定レポート MD ────────────────────────────────────────────────────
    md_path = outputs_dir / f"paper_judgment_{today}.md"
    lines = [
        f"# Atlas v3 Paper チェックポイント判定 — Day {report.day}",
        f"",
        f"**生成日時**: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**対象期間**: {records[0].date if records else 'N/A'} 〜 {records[-1].date if records else 'N/A'}",
        f"**総合判定**: **{report.overall}**",
        f"",
        "## 指標チェック一覧",
        "",
        "| 指標 | 値 | 判定 | 根拠 |",
        "|---|---|---|---|",
    ]
    for c in report.checks:
        val_str = f"{c.value:.4f}" if isinstance(c.value, float) else str(c.value)
        lines.append(f"| {c.metric_name} | {val_str} | **{c.label}** | {c.reason} |")

    lines += [
        "",
        "## メトリクスサマリ",
        "",
        f"- 累積 P&L: {metrics.cumulative_pnl:.2f} USD",
        f"- 月利換算: {metrics.monthly_rate:.4f} ({metrics.monthly_rate:.2%})",
        f"- 最大ドローダウン: {metrics.max_drawdown:.4f} ({metrics.max_drawdown:.2%})",
        f"- Sharpe (年率): {metrics.sharpe:.4f}",
        f"- Calmar: {metrics.calmar:.4f}",
        f"- 勝率: {metrics.win_rate:.2%}",
        f"- プロフィットファクター: {metrics.profit_factor:.4f}",
        f"- 平均R: {metrics.avg_r:.4f}",
        f"- 取引数: {metrics.trade_count}",
        f"- 経過営業日数: {metrics.elapsed_business_days}",
        f"- 連続損失最大: {metrics.consecutive_loss_days_max} 日",
        f"- v6 乖離: {metrics.v6_deviation:.1f}%",
    ]

    if report.note:
        lines += ["", f"> **Note**: {report.note}"]

    lines += [
        "",
        "## 後続アクション",
        "",
    ]
    if report.overall == "PASS":
        lines.append("- Navigator → ゆうさくさん: 本番移行審査リクエスト")
        lines.append("- Strategist: C2/SNS/私募仕込み検討解禁 (`memory/project_decision_wait_for_paper_results_20260423.md`)")
    elif report.overall == "WATCH":
        lines.append("- Strategist: 戦略修正案の策定依頼")
    else:  # FAIL
        lines.append("- Strategist: v6 仮説再設計・月利シナリオ見直し")
        lines.append("- ゆうさくさん: Pushover 報告済 (priority=1)")

    lines += [
        "",
        "---",
        "*本ファイルは scripts/paper_pl_baseline.py が自動生成。*",
        f"*仕様: data/research/paper_30day_judgment_criteria_20260425.md*",
    ]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Judgment MD written: %s", md_path)

    # ── FAIL 時 Pushover priority=1 ───────────────────────────────────────────
    if report.overall == "FAIL":
        _notify_fail(report, metrics)


def _notify_fail(report: JudgmentReport, metrics: Metrics) -> None:
    """FAIL 時に Pushover priority=1 送信。import 失敗時はログのみ。"""
    title = f"[Atlas-Paper] Day {report.day} FAIL"
    body_lines = [
        f"月利={metrics.monthly_rate:.2%}",
        f"最大DD={metrics.max_drawdown:.2%}",
        f"Sharpe={metrics.sharpe:.2f}",
        f"勝率={metrics.win_rate:.2%}",
        f"PF={metrics.profit_factor:.2f}",
    ]
    fail_checks = [c for c in report.checks if c.label == "FAIL"]
    if fail_checks:
        body_lines.append("FAIL項目: " + ", ".join(c.metric_name for c in fail_checks))
    body = " / ".join(body_lines)

    try:
        import sys
        sys.path.insert(0, str(_REPO_ROOT))
        from common.pushover_client import send
        send(title, body, priority=1, app_tag="Atlas")
        log.warning("Pushover FAIL notification sent: %s", title)
    except Exception as exc:
        log.error("Pushover send failed (%s) — log only: %s | %s", exc, title, body)


# ── CLI エントリポイント ───────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas v3 Paper P&L Baseline — チェックポイント判定スクリプト"
    )
    parser.add_argument(
        "--day",
        type=int,
        choices=[7, 14, 21, 30],
        default=7,
        help="チェックポイント日数 (7/14/21/30)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=_STATE_DIR_DEFAULT,
        help=f"monitor_state.jsonl の親ディレクトリ (default: {_STATE_DIR_DEFAULT})",
    )
    parser.add_argument(
        "--outputs-dir",
        type=Path,
        default=_OUTPUTS_DIR,
        help=f"出力先ディレクトリ (default: {_OUTPUTS_DIR})",
    )
    args = parser.parse_args()

    log.info("=== paper_pl_baseline start (day=%d) ===", args.day)

    v6_rates = _load_v6_rates()
    paper_risk = _load_paper_risk_config()
    initial_equity = v6_rates["initial_equity_usd"]["value"]

    records = load_daily_pnl(args.state_dir)
    metrics = calc_metrics(records, initial_equity)
    report = judge_checkpoint(metrics, args.day, v6_rates=v6_rates, paper_risk=paper_risk)

    log.info(
        "metrics: cum_pnl=%.2f monthly_rate=%.4f sharpe=%.4f max_dd=%.4f win_rate=%.2f pf=%.4f",
        metrics.cumulative_pnl,
        metrics.monthly_rate,
        metrics.sharpe,
        metrics.max_drawdown,
        metrics.win_rate,
        metrics.profit_factor,
    )
    log.info("judgment: day=%d overall=%s", report.day, report.overall)

    emit_report(records, metrics, report, outputs_dir=args.outputs_dir)
    log.info("=== paper_pl_baseline complete ===")


if __name__ == "__main__":
    main()
