"""Risk Limits — Defense-in-Depth (原子力プラント思想)

Phase別パラメータ+4層防護の閾値管理。誤発注で一撃資本全損を阻止。
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False


@dataclass
class RiskLimits:
    """Phase別リスク閾値"""
    phase: str
    max_margin_pct_per_trade: float   # 単一発注最大証拠金 (資本%)
    daily_loss_pct: float             # 日次最大損失 (負値)
    weekly_loss_pct: float            # 週次最大損失
    monthly_loss_pct: float           # 月次最大損失 (Kill Switch連動)
    max_positions: int                # 同時ポジ上限
    max_concentration_pct: float      # 単一銘柄集中上限
    max_margin_pct_total: float       # 合計保有証拠金上限
    max_qty_per_order: int            # 単一発注最大枚数
    max_option_price: float           # 発注拒否するprice上限 (Deep ITM防御)
    max_bid_ask_spread_pct: float     # bid-ask過大判定
    orders_per_minute_limit: int      # 暴走判定
    symbol_whitelist: list[str]


# Phase別 default
DEFAULT_LIMITS: dict[str, RiskLimits] = {
    "P0_paper": RiskLimits(
        phase="P0_paper",
        max_margin_pct_per_trade=0.03,
        daily_loss_pct=-0.03,
        weekly_loss_pct=-0.06,
        monthly_loss_pct=-0.12,
        max_positions=15,
        max_concentration_pct=0.20,
        max_margin_pct_total=0.50,
        max_qty_per_order=50,
        max_option_price=50.0,
        max_bid_ask_spread_pct=0.10,
        orders_per_minute_limit=15,
        symbol_whitelist=[
            "US.SPY", "US.QQQ", "US.META", "US.SPXW",
            "US.TSLA", "US.NVDA", "US.AAPL", "US.MSFT",
            "US.AMZN", "US.GOOGL", "US.IWM",
        ],
    ),
    "P1_live_small": RiskLimits(  # $8K-$25K
        phase="P1_live_small",
        max_margin_pct_per_trade=0.05,
        daily_loss_pct=-0.05,
        weekly_loss_pct=-0.10,
        monthly_loss_pct=-0.20,
        max_positions=5,
        max_concentration_pct=0.30,
        max_margin_pct_total=0.50,
        max_qty_per_order=10,
        max_option_price=50.0,
        max_bid_ask_spread_pct=0.10,
        orders_per_minute_limit=5,
        symbol_whitelist=["US.SPY", "US.QQQ", "US.IWM"],  # 小資本時は高流動銘柄のみ
    ),
    "P2_live_mid": RiskLimits(  # $25K-$100K
        phase="P2_live_mid",
        max_margin_pct_per_trade=0.05,
        daily_loss_pct=-0.05,
        weekly_loss_pct=-0.10,
        monthly_loss_pct=-0.20,
        max_positions=15,
        max_concentration_pct=0.25,
        max_margin_pct_total=0.50,
        max_qty_per_order=30,
        max_option_price=50.0,
        max_bid_ask_spread_pct=0.10,
        orders_per_minute_limit=10,
        symbol_whitelist=[
            "US.SPY", "US.QQQ", "US.META", "US.SPXW",
            "US.TSLA", "US.NVDA", "US.AAPL", "US.IWM",
        ],
    ),
    "P3_live_large": RiskLimits(  # $100K-$500K
        phase="P3_live_large",
        max_margin_pct_per_trade=0.04,
        daily_loss_pct=-0.04,
        weekly_loss_pct=-0.08,
        monthly_loss_pct=-0.18,
        max_positions=20,
        max_concentration_pct=0.20,
        max_margin_pct_total=0.50,
        max_qty_per_order=50,
        max_option_price=75.0,
        max_bid_ask_spread_pct=0.10,
        orders_per_minute_limit=15,
        symbol_whitelist=[
            "US.SPY", "US.QQQ", "US.META", "US.SPXW",
            "US.TSLA", "US.NVDA", "US.AAPL", "US.MSFT",
            "US.AMZN", "US.GOOGL", "US.IWM",
        ],
    ),
    "P4_fund": RiskLimits(  # $1M+
        phase="P4_fund",
        max_margin_pct_per_trade=0.02,
        daily_loss_pct=-0.02,
        weekly_loss_pct=-0.05,
        monthly_loss_pct=-0.10,
        max_positions=30,
        max_concentration_pct=0.15,
        max_margin_pct_total=0.40,
        max_qty_per_order=100,
        max_option_price=100.0,
        max_bid_ask_spread_pct=0.08,
        orders_per_minute_limit=20,
        symbol_whitelist=[
            "US.SPY", "US.QQQ", "US.META", "US.SPXW",
            "US.TSLA", "US.NVDA", "US.AAPL", "US.MSFT",
            "US.AMZN", "US.GOOGL", "US.IWM",
        ],
    ),
}


def determine_phase(
    capital_usd: float,
    paper: bool = False,
    trade_count: int = 0,
    monthly_pnl_usd: float | None = None,
    max_dd_pct: float | None = None,
) -> str:
    """資本規模 + 実績条件からPhase判定する（自動遷移対応版）。

    CLAUDE.md §本番移行判断 (移行トリガー):
    - 20トレード完了
    - 月次プラス
    - DD < 20%
    を全て満たした場合のみ P1_live_small に昇格する。
    条件未達の場合は資本が $25K 超でも P0_paper に留まる。

    Args:
        capital_usd:      口座残高 (USD)
        paper:            明示的な paper モード override
        trade_count:      累計完了トレード数 (0 = 未集計)
        monthly_pnl_usd:  直近月次PnL (None = 未集計)
        max_dd_pct:       最大DD% (正値。例: 15.0 = 15%DD。None = 未集計)

    Returns:
        Phase文字列 "P0_paper" / "P1_live_small" / "P2_live_mid" /
                   "P3_live_large" / "P4_fund"
    """
    if paper:
        return "P0_paper"

    # P0→P1 昇格条件チェック（全条件必須）
    # 条件未集計 (None) は未達扱い（安全側）
    _trade_ok = trade_count >= 20
    _pnl_ok = (monthly_pnl_usd is not None and monthly_pnl_usd > 0)
    _dd_ok = (max_dd_pct is not None and max_dd_pct < 20.0)
    _promotion_ready = _trade_ok and _pnl_ok and _dd_ok

    if capital_usd < 25_000:
        # 資本 < $25K は条件関係なく P1_live_small（PDT制限下）
        return "P1_live_small"

    # 資本 >= $25K だが昇格条件未達 → P0_paper で留まる
    if not _promotion_ready:
        import logging as _log
        _log.getLogger(__name__).info(
            f"[PhaseTransition] P0→P1 昇格条件未達: "
            f"trades={trade_count}(需20), "
            f"monthly_pnl={monthly_pnl_usd}(要>0), "
            f"max_dd={max_dd_pct}%(要<20%) — P0_paper維持"
        )
        return "P0_paper"

    # 昇格条件達成 → 資本額でフェーズ決定
    if capital_usd < 100_000:
        return "P1_live_small"
    if capital_usd < 1_000_000:
        return "P2_live_mid"
    return "P3_live_large"


def load_limits(phase: str | None = None, capital_usd: float = 0, paper: bool = False) -> RiskLimits:
    """設定yamlがあれば優先、なければdefault"""
    if phase is None:
        phase = determine_phase(capital_usd, paper)

    # yaml上書き試行
    yaml_path = Path(__file__).resolve().parents[1] / "data" / "risk_limits.yaml"
    if _YAML_OK and yaml_path.exists():
        try:
            with open(yaml_path, "r") as f:
                data = yaml.safe_load(f) or {}
            if phase in data:
                d = {**DEFAULT_LIMITS[phase].__dict__, **data[phase]}
                return RiskLimits(**d)
        except Exception:
            pass

    return DEFAULT_LIMITS.get(phase, DEFAULT_LIMITS["P0_paper"])
