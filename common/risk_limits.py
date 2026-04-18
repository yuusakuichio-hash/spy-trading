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


def determine_phase(capital_usd: float, paper: bool = False) -> str:
    """資本規模からPhase判定"""
    if paper:
        return "P0_paper"
    if capital_usd < 25_000:
        return "P1_live_small"
    if capital_usd < 100_000:
        return "P2_live_mid"
    if capital_usd < 1_000_000:
        return "P3_live_large"
    return "P4_fund"


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
