"""Portfolio Aggregator — Bot間リスク統合 + 週次/月次DD管理

condor_pnl.json (spy_bot単体) と portfolio_pnl.json (複数Bot) を統合集計。
portfolio_positions.json から合計ポジション・証拠金・デルタを集計。

Bot間リスク統合:
  - aggregate_portfolio_risk()  : 全Bot合算ポジション/証拠金/デルタ
  - bot_portfolio_risk(bot)     : 特定Bot単体のリスク情報
  - check_cross_bot_limits()    : 合計が limits を超えていないか

損失ゲート:
  - daily_pnl()   / weekly_pnl()   / monthly_pnl()   : 全Bot合算
  - bot_pnl_by_period(bot, period) : Bot別期間P&L
  - check_loss_gates()             : 逸脱時(False, reason)
"""
from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parents[1]
PNL_FILE = BASE / "data" / "condor_pnl.json"          # spy_bot単体 (旧来)
PORTFOLIO_PNL_FILE = BASE / "data" / "portfolio_pnl.json"   # Bot別統合
POSITIONS_FILE = BASE / "data" / "portfolio_positions.json"  # Bot別ポジション


# ─────────────────────────────────────────────────────────────────────────────
# データロード
# ─────────────────────────────────────────────────────────────────────────────

def _load_condor_pnl() -> list:
    """旧来の condor_pnl.json (spy_bot単体)"""
    if not PNL_FILE.exists():
        return []
    try:
        with open(PNL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("trades", []) if isinstance(data, dict) else data
    except Exception:
        return []


def _load_portfolio_pnl() -> list[dict]:
    """portfolio_pnl.json — Bot別PnLレコード [{date, bot, pnl_usd, ...}]"""
    if not PORTFOLIO_PNL_FILE.exists():
        return []
    try:
        with open(PORTFOLIO_PNL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _load_positions() -> dict:
    """portfolio_positions.json — Bot別ポジション/total_risk"""
    if not POSITIONS_FILE.exists():
        return {}
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# 旧来インターフェース (condor_pnl.json ベース・後方互換)
# ─────────────────────────────────────────────────────────────────────────────

def _sum_pnl(trades: list, since: datetime.date) -> float:
    total = 0.0
    since_str = since.strftime("%Y-%m-%d")
    for t in trades:
        if t.get("event") != "exit":
            continue
        d = t.get("date", "")
        if d >= since_str:
            total += float(t.get("pnl_usd", 0) or 0)
    return total


def daily_pnl(today: datetime.date | None = None) -> float:
    """全Botの日次P&L合算 (condor_pnl + portfolio_pnl)"""
    today = today or datetime.date.today()
    legacy = _sum_pnl(_load_condor_pnl(), today)
    portfolio = _sum_portfolio_pnl(since=today)
    return legacy + portfolio


def weekly_pnl(today: datetime.date | None = None) -> float:
    """全Botの週次P&L合算"""
    today = today or datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    legacy = _sum_pnl(_load_condor_pnl(), week_start)
    portfolio = _sum_portfolio_pnl(since=week_start)
    return legacy + portfolio


def monthly_pnl(today: datetime.date | None = None) -> float:
    """全Botの月次P&L合算"""
    today = today or datetime.date.today()
    month_start = today.replace(day=1)
    legacy = _sum_pnl(_load_condor_pnl(), month_start)
    portfolio = _sum_portfolio_pnl(since=month_start)
    return legacy + portfolio


# ─────────────────────────────────────────────────────────────────────────────
# Bot別P&L集計 (portfolio_pnl.json ベース)
# ─────────────────────────────────────────────────────────────────────────────

def _sum_portfolio_pnl(
    since: datetime.date,
    until: datetime.date | None = None,
    bot_name: str | None = None,
) -> float:
    """portfolio_pnl.json を期間・Bot名でフィルタして合算"""
    records = _load_portfolio_pnl()
    since_str = since.strftime("%Y-%m-%d")
    until_str = (until or datetime.date.today()).strftime("%Y-%m-%d")
    total = 0.0
    for rec in records:
        d = rec.get("date", "")
        if d < since_str or d > until_str:
            continue
        if bot_name is not None and rec.get("bot") != bot_name:
            continue
        total += float(rec.get("pnl_usd", 0) or 0)
    return total


def bot_pnl_by_period(
    bot_name: str,
    period: str,
    today: datetime.date | None = None,
) -> float:
    """特定Botの期間P&L。period: 'daily' / 'weekly' / 'monthly'"""
    today = today or datetime.date.today()
    if period == "daily":
        since = today
    elif period == "weekly":
        since = today - datetime.timedelta(days=today.weekday())
    elif period == "monthly":
        since = today.replace(day=1)
    else:
        raise ValueError(f"Unknown period: {period!r}. Use 'daily'/'weekly'/'monthly'")
    return _sum_portfolio_pnl(since=since, bot_name=bot_name)


# ─────────────────────────────────────────────────────────────────────────────
# Bot間リスク統合 (portfolio_positions.json ベース)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BotRiskSnapshot:
    """単一Botのリスク情報"""
    bot_name: str
    position_count: int
    total_risk: float     # 総証拠金リスク (USD)
    delta_total: float    # デルタ合計 (ポジションにdeltaフィールドがある場合)
    updated_at: str


@dataclass
class PortfolioRiskSummary:
    """全Bot合算リスクサマリ"""
    bots: list[BotRiskSnapshot]
    total_positions: int
    total_risk_usd: float
    total_delta: float
    snapshot_at: str


def bot_portfolio_risk(bot_name: str) -> BotRiskSnapshot:
    """特定Botのリスクスナップショットを返す"""
    positions_data = _load_positions()
    bot_data = positions_data.get(bot_name, {})

    positions = bot_data.get("positions", [])
    total_risk = float(bot_data.get("total_risk", 0) or 0)
    delta_total = sum(float(p.get("delta", 0) or 0) for p in positions)
    updated_at = bot_data.get("updated_at", "")

    return BotRiskSnapshot(
        bot_name=bot_name,
        position_count=len(positions),
        total_risk=total_risk,
        delta_total=delta_total,
        updated_at=updated_at,
    )


def aggregate_portfolio_risk(
    bot_names: list[str] | None = None,
) -> PortfolioRiskSummary:
    """全Bot (または指定Bot) の合算リスクを集計する。

    Args:
        bot_names: Noneの場合は portfolio_positions.json に含まれる全Botを対象
    """
    positions_data = _load_positions()

    if bot_names is None:
        # stop_loss_events 等の非Bot keyを除外
        bot_names = [
            k for k in positions_data.keys()
            if isinstance(positions_data[k], dict) and "positions" in positions_data[k]
        ]

    snapshots: list[BotRiskSnapshot] = []
    for bn in bot_names:
        snapshots.append(bot_portfolio_risk(bn))

    total_positions = sum(s.position_count for s in snapshots)
    total_risk_usd = sum(s.total_risk for s in snapshots)
    total_delta = sum(s.delta_total for s in snapshots)

    return PortfolioRiskSummary(
        bots=snapshots,
        total_positions=total_positions,
        total_risk_usd=total_risk_usd,
        total_delta=total_delta,
        snapshot_at=datetime.datetime.now().isoformat(),
    )


def check_cross_bot_limits(
    capital_usd: float,
    limits,
    bot_names: list[str] | None = None,
) -> tuple[bool, str]:
    """全Bot合算のポジション数・証拠金が limits を超えていないか確認する。

    Returns:
        (allow_new_entry, reason)
    """
    if capital_usd <= 0:
        return True, "no capital ref"

    summary = aggregate_portfolio_risk(bot_names=bot_names)

    if summary.total_positions >= limits.max_positions:
        return False, (
            f"cross_bot_position_limit: 合計{summary.total_positions}ポジ "
            f">= max={limits.max_positions} "
            f"(bots={[s.bot_name for s in summary.bots]})"
        )

    total_margin_pct = summary.total_risk_usd / capital_usd
    if total_margin_pct > limits.max_margin_pct_total:
        return False, (
            f"cross_bot_margin_limit: 合計証拠金{summary.total_risk_usd:.0f}USD "
            f"({total_margin_pct:.1%}) > max={limits.max_margin_pct_total:.0%}"
        )

    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# 損失ゲート (全Bot合算・Phase別動的閾値)
# ─────────────────────────────────────────────────────────────────────────────

def check_loss_gates(
    capital_usd: float,
    limits,
    today: datetime.date | None = None,
) -> tuple[bool, str]:
    """全Bot合算の損失ゲートチェック。違反時 (False, reason) を返す。

    月次DD超過は Kill Switch 自動発動対象（呼び出し元で対応すること）。
    """
    if capital_usd <= 0:
        return True, "no capital ref"

    dp = daily_pnl(today)
    wp = weekly_pnl(today)
    mp = monthly_pnl(today)

    # 日次
    if dp / capital_usd <= limits.daily_loss_pct:
        return False, (
            f"daily_loss_gate: ${dp:.0f} "
            f"({dp/capital_usd:.1%} <= {limits.daily_loss_pct:.0%})"
        )

    # 週次
    if wp / capital_usd <= limits.weekly_loss_pct:
        return False, (
            f"weekly_loss_gate: ${wp:.0f} "
            f"({wp/capital_usd:.1%} <= {limits.weekly_loss_pct:.0%})"
        )

    # 月次 — Kill Switch自動発動トリガー
    if mp / capital_usd <= limits.monthly_loss_pct:
        return False, (
            f"monthly_loss_gate: ${mp:.0f} "
            f"({mp/capital_usd:.1%} <= {limits.monthly_loss_pct:.0%}) "
            "→ Kill Switch発動済み"
        )

    return True, "ok"
