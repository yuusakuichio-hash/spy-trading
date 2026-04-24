"""atlas_v3.bots.engines — 個別 engine パッケージ。

公開シンボル（10 戦術 + 設定 DTO）
------------------------------------
IronFlyEngine / IronFlyConfig
    Iron Fly (tight Iron Condor) エントリー / エグジット戦術エンジン。
WeeklyGammaScalpTactic / WeeklyGammaScalpConfig
    Weekly ATM Straddle Long + delta hedge 戦術エンジン（SPY/QQQ/IWM）。
ORBNativeEngine / ORBNativePosition
    Opening Range Breakout エンジン（spy_bot.ORBEngine の atlas_v3 native 移植）。
ShortStrangle0DTEEngine / ShortStrangle0DTEConfig
    0DTE Short Strangle エンジン（OTM delta 0.10-0.15 の pair 売り）。
BrokenWingButterflyEngine / BrokenWingButterflyConfig
    Broken Wing Butterfly エンジン（4 leg / 片翼ゼロコスト）。
DiagonalSpreadTactic / DiagonalSpreadConfig
    Diagonal Spread エンジン（long back-month + short front-month）。
EarningsStraddleBuyTactic / StraddleBuyConfig
    Earnings IV crush buy エンジン（state_carrying / 決算前 ATM straddle long）。
JadeLizardTactic / JadeLizardConfig
    Jade Lizard エンジン（short put + short call spread）。
PMCCTactic / PMCCConfig
    Poor Man's Covered Call エンジン（deep ITM long LEAP + short OTM call）。
RatioSpreadEngine / RatioSpreadConfig
    Ratio Spread エンジン（1 long / 2 short の非対称スプレッド）。
VixTailHedgeEngine / VixTailHedgeConfig
    VIX テールヘッジエンジン（VIX call long / spike 保険）。
"""
from atlas_v3.bots.engines.broken_wing_butterfly import (
    BrokenWingButterflyConfig,
    BrokenWingButterflyEngine,
)
from atlas_v3.bots.engines.diagonal_spread import (
    DiagonalSpreadConfig,
    DiagonalSpreadTactic,
)
from atlas_v3.bots.engines.earnings_straddle_buy import (
    EarningsStraddleBuyTactic,
    StraddleBuyConfig,
)
from atlas_v3.bots.engines.iron_fly import IronFlyConfig, IronFlyEngine
from atlas_v3.bots.engines.jade_lizard import JadeLizardConfig, JadeLizardTactic
from atlas_v3.bots.engines.orb_native import ORBNativeEngine, ORBNativePosition
from atlas_v3.bots.engines.pmcc import PMCCConfig, PMCCTactic
from atlas_v3.bots.engines.ratio_spread import RatioSpreadConfig, RatioSpreadEngine
from atlas_v3.bots.engines.short_strangle_0dte import (
    ShortStrangle0DTEConfig,
    ShortStrangle0DTEEngine,
)
from atlas_v3.bots.engines.vix_tail_hedge import VixTailHedgeConfig, VixTailHedgeEngine
from atlas_v3.bots.engines.weekly_gamma_scalp import (
    WeeklyGammaScalpConfig,
    WeeklyGammaScalpTactic,
)

__all__ = [
    # Iron Fly
    "IronFlyConfig",
    "IronFlyEngine",
    # Weekly Gamma Scalp
    "WeeklyGammaScalpConfig",
    "WeeklyGammaScalpTactic",
    # ORB Native
    "ORBNativeEngine",
    "ORBNativePosition",
    # Short Strangle 0DTE
    "ShortStrangle0DTEConfig",
    "ShortStrangle0DTEEngine",
    # Broken Wing Butterfly
    "BrokenWingButterflyConfig",
    "BrokenWingButterflyEngine",
    # Diagonal Spread
    "DiagonalSpreadConfig",
    "DiagonalSpreadTactic",
    # Earnings Straddle Buy
    "EarningsStraddleBuyTactic",
    "StraddleBuyConfig",
    # Jade Lizard
    "JadeLizardConfig",
    "JadeLizardTactic",
    # PMCC
    "PMCCConfig",
    "PMCCTactic",
    # Ratio Spread
    "RatioSpreadConfig",
    "RatioSpreadEngine",
    # VIX Tail Hedge
    "VixTailHedgeConfig",
    "VixTailHedgeEngine",
]
