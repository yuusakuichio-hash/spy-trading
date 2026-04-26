"""atlas_v3/bots/engines/orb_native.py — ORB Engine (subprocess 依存ゼロ移植)

spy_bot.ORBEngine の public interface を TacticBase 継承の純 atlas_v3 実装として完全再現。
spy_bot.py への書き換えはゼロ。

移植元: spy_bot.py::ORBEngine (L7680-L8798)
公開 interface:
    reset_daily()
    premarket_check(intraday_monitor=None) -> bool
    record_opening_range() -> bool
    check_breakout() -> Optional[str]
    execute_entry(direction, signal_id=None) -> Optional[ORBNativePosition]
    execute_entry_1dte(direction, signal_id=None) -> Optional[ORBNativePosition]
    check_exit(intraday_monitor=None) -> Optional[dict]

依存置換:
    MarketData      -> MarketDataProtocol (Protocol / duck-typing)
    TradeEngine     -> TradeEngineProtocol (Protocol / duck-typing)
    IntradayMonitor -> IntradayMonitorProtocol (Protocol / duck-typing)
    Finnhub / Yahoo / futu price 取得 -> atlas_v3.ops.symbol_aware_price.get_current_price
    chainguard_wrapper との同一例外体系

禁則:
    - spy_bot.py / chronos_bot.py へのインポート禁止
    - asyncio 禁止（sync_only 前提）
    - CC <= 20 規律
"""
from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import requests

from atlas_v3.ops.chainguard_wrapper import get_chain_center_price_with_fallback
from atlas_v3.ops.symbol_aware_price import (
    MissingPriceError,
    OutOfRangePriceError,
    StalePriceError,
    get_current_price_with_fallback,
    normalize_symbol,
)
from atlas_v3.strategies.base import TacticBase, TacticType
from atlas_v3.core.env_observer import MarketEnvironment
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# ORB 定数（spy_bot.py L7547-L7572 と同値・独立コピー）
# ---------------------------------------------------------------------------

ORB_PERIOD_MIN: int           = 5
ORB_BREAKOUT_CUTOFF_H: int    = 11
ORB_BREAKOUT_CUTOFF_M: int    = 0
ORB_EXIT_TIME_H: int          = 15
ORB_EXIT_TIME_M: int          = 30
ORB_TP_PCT: float             = 1.00    # +100%
ORB_SL_PCT: float             = -0.50  # -50%
ORB_MAX_RISK_PCT: float       = 0.02
ORB_MAX_QTY: int              = 3
ORB_MAX_CONSECUTIVE_LOSSES: int = 3
ORB_SMALL_ACCOUNT_USD: float  = 15_000.0
ORB_VIX_MIN: float            = 20.0
ORB_VIX_MAX: float            = 40.0
ORB_GAP_THRESHOLD_PCT: float  = 2.0
ORB_GAP_BULL_SIZE_BOOST: float = 1.3
ORB_PRIME_END_H: int          = 11
ORB_PRIME_END_M: int          = 0
ORB_LATE_FACTOR: float        = 0.7
DYNAMIC_ENTRY_MIN_ENV_SCORE: float = 60.0
LAST_ENTRY_H: int             = 15
LAST_ENTRY_M: int             = 30
EARLY_CLOSE_EXIT_H: int       = 12
EARLY_CLOSE_EXIT_M: int       = 50
DEEP_ITM_THRESHOLD_0DTE: float = 50.0
DEEP_ITM_THRESHOLD_1DTE: float = 60.0
STRIKE_DEVIATION_MAX_0DTE: float = 0.15
STRIKE_DEVIATION_MAX_1DTE: float = 0.20
ORB1DTE_DELTA_TARGET: float   = 0.40
ORB1DTE_DELTA_MIN: float      = 0.35
ORB1DTE_DELTA_MAX: float      = 0.45

# early-close 半日取引日（spy_bot.py L472 と同値）
_EARLY_CLOSE_DAYS: dict[str, tuple[int, int]] = {
    "2026-11-27": (13, 0),
    "2026-12-24": (13, 0),
    "2027-07-03": (13, 0),
    "2027-11-26": (13, 0),
    "2027-12-24": (13, 0),
}

# ---------------------------------------------------------------------------
# ヘルパー関数（内部使用）
# ---------------------------------------------------------------------------

def _is_early_close_today() -> bool:
    today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
    return today in _EARLY_CLOSE_DAYS


def _is_past_entry_cutoff(dry_test: bool = False) -> bool:
    """15:30 ET を超えていれば True（H-T1 共通エントリーゲート）。"""
    if dry_test:
        return False
    try:
        now_et = datetime.datetime.now(ET)
        return (now_et.hour * 60 + now_et.minute) >= (LAST_ENTRY_H * 60 + LAST_ENTRY_M)
    except Exception:
        log.warning("[ORBNative] ET 時刻取得失敗 → エントリー禁止（安全側）")
        return True


# ---------------------------------------------------------------------------
# ORBNativePosition（spy_bot.ORBPosition の独立コピー）
# ---------------------------------------------------------------------------

@dataclass
class ORBNativePosition:
    """ORB ロングオプションポジション（spy_bot.ORBPosition と同一構造）。"""

    code: str
    qty: int
    entry_price: float
    direction: str          # "CALL" or "PUT"
    orb_high: float
    orb_low: float
    entry_time: str = field(
        default_factory=lambda: datetime.datetime.now(ET).isoformat()
    )
    partial_closed: int = 0
    _is_1dte: bool = False

    def __post_init__(self) -> None:
        self.orb_range: float = self.orb_high - self.orb_low

    @property
    def sl_price(self) -> float:
        return self.entry_price * (1 + ORB_SL_PCT)

    @property
    def tp_price(self) -> float:
        return self.entry_price * (1 + ORB_TP_PCT)

    def check_exit(self, current_price: float) -> Optional[str]:
        """TP/SL 到達でエグジット理由を返す。継続中は None。"""
        pnl_pct = (current_price - self.entry_price) / self.entry_price
        tp_pct = 0.30 if self._is_1dte else ORB_TP_PCT
        if pnl_pct >= tp_pct:
            return "profit_target"
        if pnl_pct <= -0.50:
            return "stop_loss"
        return None


# ---------------------------------------------------------------------------
# Protocols — 依存先の型契約（duck-typing、futu 非依存）
# ---------------------------------------------------------------------------

@runtime_checkable
class MarketDataProtocol(Protocol):
    """MarketData の最小インターフェース（orb_native が利用するメソッドのみ）。"""

    @property
    def underlying_code(self) -> str: ...

    @underlying_code.setter
    def underlying_code(self, v: str) -> None: ...

    def get_vix(self) -> Optional[float]: ...

    def get_vix_history(self, days: int = 60) -> list[float]: ...

    def get_option_chain_with_greeks(
        self, expiry: str, direction: str, center_strike: float = 0.0
    ) -> list[dict]: ...

    def find_by_delta(self, chain: list[dict], delta: float) -> Optional[dict]: ...

    def find_by_strike(self, chain: list[dict], strike: float) -> Optional[dict]: ...

    def get_last_price(self, symbol: str) -> Optional[float]: ...

    def get_cached_option_price(
        self, code: str, max_age_sec: float = 15.0
    ) -> Optional[float]: ...


@runtime_checkable
class TradeEngineProtocol(Protocol):
    """TradeEngine の最小インターフェース（orb_native が利用するメソッドのみ）。"""

    def get_account_cash(self) -> Optional[float]: ...

    def place_buy(
        self,
        code: str,
        qty: int,
        label: str,
        init_price: Optional[float] = None,
        use_limit: bool = False,
        signal_id: Optional[str] = None,
    ) -> Optional[str]: ...

    def place_sell(
        self,
        code: str,
        qty: int,
        label: str,
    ) -> Optional[str]: ...


@runtime_checkable
class IntradayMonitorProtocol(Protocol):
    """IntradayMonitor の最小インターフェース（regime 参照のみ）。"""

    @property
    def current_regime(self) -> str: ...


# ---------------------------------------------------------------------------
# フォールバック価格テーブル（spy_bot.ORBEngine._FALLBACK_PRICE_DEFAULTS と同値）
# ---------------------------------------------------------------------------

_FALLBACK_PRICE_DEFAULTS: dict[str, float] = {
    "SPY": 560.0, "QQQ": 480.0, "IWM": 200.0,
    "TSLA": 250.0, "NVDA": 900.0, "AAPL": 200.0,
    "MSFT": 420.0, "AMZN": 200.0, "META": 600.0,
    "GOOGL": 170.0,
}


def _get_fallback_price(ticker: str) -> float:
    return _FALLBACK_PRICE_DEFAULTS.get(ticker, 300.0)


# ---------------------------------------------------------------------------
# ORBNativeEngine — TacticBase 継承・enter_exit タイプ
# ---------------------------------------------------------------------------

class ORBNativeEngine(TacticBase):
    """spy_bot.ORBEngine の atlas_v3 native 移植。

    subprocess 依存ゼロ・futu SDK 直接インポートなし。
    MarketDataProtocol / TradeEngineProtocol で依存を抽象化。

    Public interface（spy_bot.ORBEngine と完全互換）:
        reset_daily()
        premarket_check(intraday_monitor=None) -> bool
        record_opening_range() -> bool
        check_breakout() -> Optional[str]
        execute_entry(direction, signal_id=None) -> Optional[ORBNativePosition]
        execute_entry_1dte(direction, signal_id=None) -> Optional[ORBNativePosition]
        check_exit(intraday_monitor=None) -> Optional[dict]
    """

    supports_1dte: bool = True
    allow_expiry_pass_through: bool = False

    def __init__(
        self,
        mkt: Optional[Any] = None,
        eng: Optional[Any] = None,
        paper: bool = False,
        dry_test: bool = False,
    ) -> None:
        self.mkt = mkt
        self.eng = eng
        self.paper = paper
        self.dry_test = dry_test

        # 日次状態
        self.orb_high: Optional[float] = None
        self.orb_low: Optional[float] = None
        self.orb_range: Optional[float] = None
        self.today_vix: Optional[float] = None
        self.position: Optional[ORBNativePosition] = None
        self.trade_done: bool = False
        self.orb_checked: bool = False
        self.breakout_direction: Optional[str] = None
        self.entry_done: bool = False
        self._daily_loss_halted: bool = False

        # サイズ係数
        self._assessment: Optional[dict] = None
        self._kelly_fraction: Optional[float] = None
        self._vix9d_vvix_factor: float = 1.0
        self._gap_pct: Optional[float] = None
        self._gap_size_factor: float = 1.0
        self._time_zone_factor: float = 1.0

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "orb_native"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。Kill Switch ARMED なら False。"""
        if env is None:
            log.warning("[ORBNative.preflight] env=None → False")
            return False
        if kill_switch_is_active():
            log.warning("[ORBNative.preflight] Kill Switch ARMED → False")
            return False
        if env.vix > ORB_VIX_MAX:
            log.info(
                "[ORBNative.preflight] VIX=%.2f > ORB_VIX_MAX=%.2f → False",
                env.vix, ORB_VIX_MAX,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Phase 0: 日次リセット
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """EOD または日付変わり時に日次状態をリセットする。"""
        self.orb_high = None
        self.orb_low = None
        self.orb_range = None
        self.today_vix = None
        self.position = None
        self.trade_done = False
        self.orb_checked = False
        self.breakout_direction = None
        self.entry_done = False
        self._daily_loss_halted = False
        self._assessment = None
        self._kelly_fraction = None
        self._vix9d_vvix_factor = 1.0
        self._gap_pct = None
        self._gap_size_factor = 1.0
        self._time_zone_factor = 1.0

    # ------------------------------------------------------------------
    # Phase 1: プレマーケット環境チェック
    # ------------------------------------------------------------------

    def premarket_check(
        self, intraday_monitor: Optional[Any] = None
    ) -> bool:
        """VIX・環境スコアで ORB エントリー可否を判断する。"""
        if self.dry_test:
            self.today_vix = 22.0
            log.info("[ORBNative][DRY-TEST] premarket_check: vix=22.0 → OK")
            return True

        if self.mkt is None:
            log.warning("[ORBNative] premarket_check: mkt=None → False")
            return False

        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("[ORBNative] premarket_check: VIX 取得失敗 → False")
            return False

        self.today_vix = vix

        if not self.paper:
            if vix < ORB_VIX_MIN:
                log.info(
                    "[ORBNative] Skip: VIX=%.2f < %.2f (値動き不足)", vix, ORB_VIX_MIN
                )
                return False
            if vix > ORB_VIX_MAX:
                log.info(
                    "[ORBNative] Skip: VIX=%.2f > %.2f (過度な恐怖相場)", vix, ORB_VIX_MAX
                )
                return False
        else:
            log.info("[ORBNative][PAPER] VIX=%.2f → VIX 条件バイパス", vix)

        if self._assessment:
            score = self._assessment.get("score", 100.0)
            gap_pct = self._assessment.get("gap_pct")
            if gap_pct is not None and abs(gap_pct) >= ORB_GAP_THRESHOLD_PCT:
                score += 20.0
            if score < DYNAMIC_ENTRY_MIN_ENV_SCORE:
                log.info("[ORBNative] Skip: 環境スコア=%.1f < %.1f", score, DYNAMIC_ENTRY_MIN_ENV_SCORE)
                return False
            self._gap_pct = gap_pct
            self._vix9d_vvix_factor = self._assessment.get("vix9d_vvix_size_factor", 1.0)

        log.info("[ORBNative] premarket_check OK: VIX=%.2f", vix)
        return True

    # ------------------------------------------------------------------
    # Phase 2: ORB 記録（9:35 ET に呼び出す）
    # ------------------------------------------------------------------

    def record_opening_range(self) -> bool:
        """9:30-9:35 の 5 分間高値/安値を記録する。"""
        ticker = self._get_ticker()

        if self.dry_test:
            underlying_price = self._fetch_price_dry(ticker)
            self.orb_high = underlying_price + 0.5
            self.orb_low = underlying_price - 0.5
            self.orb_range = 1.0
            self.orb_checked = True
            log.info(
                "[ORBNative][DRY-TEST] %s ORB: H=%.2f L=%.2f",
                ticker, self.orb_high, self.orb_low,
            )
            return True

        bars = self._get_1min_bars(minutes=10)
        if not bars:
            log.warning("[ORBNative] 1 分足データ取得失敗")
            return False

        now_et = datetime.datetime.now(ET)
        orb_bars = [
            b for b in bars
            if b["time"].astimezone(ET).replace(
                hour=9, minute=30, second=0, microsecond=0
            ) <= b["time"].astimezone(ET) < b["time"].astimezone(ET).replace(
                hour=9, minute=35, second=0, microsecond=0
            )
        ]
        if not orb_bars:
            orb_bars = bars[-5:] if len(bars) >= 5 else bars
        if not orb_bars:
            return False

        self.orb_high = max(b["high"] for b in orb_bars)
        self.orb_low = min(b["low"] for b in orb_bars)
        self.orb_range = self.orb_high - self.orb_low
        self.orb_checked = True
        log.info(
            "[ORBNative] Opening Range: H=%.2f L=%.2f Range=%.2f",
            self.orb_high, self.orb_low, self.orb_range,
        )
        return True

    # ------------------------------------------------------------------
    # Phase 3: ブレイクアウトチェック（毎 tick 呼び出す）
    # ------------------------------------------------------------------

    def check_breakout(self) -> Optional[str]:
        """現在価格で ORB ブレイクアウトを判定する。Returns: "CALL" / "PUT" / None。"""
        if not self.orb_checked or self.orb_high is None:
            return None
        if self.entry_done or self.trade_done:
            return None

        if not self.dry_test:
            now_et = datetime.datetime.now(ET)
            if (now_et.hour > ORB_BREAKOUT_CUTOFF_H or
                    (now_et.hour == ORB_BREAKOUT_CUTOFF_H
                     and now_et.minute >= ORB_BREAKOUT_CUTOFF_M)):
                return None

        price = self._get_underlying_price()
        if not price or price <= 0:
            return None

        ticker = self._get_ticker()
        if price > self.orb_high:
            log.info(
                "[ORBNative] CALL ブレイク: %s=%.2f > H=%.2f",
                ticker, price, self.orb_high,
            )
            return "CALL"
        if price < self.orb_low:
            log.info(
                "[ORBNative] PUT ブレイク: %s=%.2f < L=%.2f",
                ticker, price, self.orb_low,
            )
            return "PUT"
        return None

    # ------------------------------------------------------------------
    # Phase 4: エントリー実行（0DTE）
    # ------------------------------------------------------------------

    def execute_entry(
        self,
        direction: str,
        signal_id: Optional[str] = None,
    ) -> Optional[ORBNativePosition]:
        """ブレイクアウト確認後に ATM 0DTE オプションを買い注文する。"""
        orig_underlying = self._underlying_code()
        try:
            return self._execute_entry_impl(direction, signal_id=signal_id)
        finally:
            if self.mkt is not None:
                try:
                    if self.mkt.underlying_code != orig_underlying:
                        log.info(
                            "[ORBNative] underlying_code 復元: %s → %s",
                            self.mkt.underlying_code, orig_underlying,
                        )
                        self.mkt.underlying_code = orig_underlying
                except Exception:
                    pass

    def _execute_entry_impl(
        self, direction: str, signal_id: Optional[str] = None
    ) -> Optional[ORBNativePosition]:
        if _is_past_entry_cutoff(dry_test=self.dry_test):
            log.info("[ORBNative] execute_entry: 15:30 ET 以降 → エントリー中止")
            self.trade_done = True
            return None

        if kill_switch_is_active():
            log.warning("[ORBNative] execute_entry: Kill Switch ARMED → 中止")
            self.trade_done = True
            return None

        underlying_code = self._underlying_code()
        ticker = self._get_ticker()

        if not self.dry_test:
            now_et = datetime.datetime.now(ET)
            if now_et.hour >= 16:
                log.info("[ORBNative] execute_entry: 16:00 ET 以降 → エントリー中止")
                self.trade_done = True
                return None

        underlying_price = self._get_underlying_price()
        if not underlying_price or underlying_price <= 0:
            log.error("[ORBNative] execute_entry: %s 価格取得失敗", ticker)
            return None

        atm_strike = round(underlying_price)
        self._update_time_zone_factor()
        self._update_gap_size_factor(direction)

        today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")

        # dry-test モード
        if self.dry_test:
            return self._dry_entry_0dte(direction, ticker, atm_strike, signal_id)

        vix = self.today_vix or 20.0
        target_delta = self._calc_target_delta(vix)

        chain = self.mkt.get_option_chain_with_greeks(
            today_str, direction, center_strike=float(atm_strike)
        )
        if not chain:
            log.error("[ORBNative] オプションチェーン取得失敗 (%s %s)", direction, today_str)
            return None

        opt = self.mkt.find_by_delta(chain, target_delta)
        if opt is None:
            opt = self.mkt.find_by_strike(chain, float(atm_strike))
        if opt is None:
            log.error("[ORBNative] オプション選択失敗")
            return None

        if not self._check_strike_deviation(
            opt.get("strike_price", 0), atm_strike, STRIKE_DEVIATION_MAX_0DTE, underlying_code
        ):
            return None

        option_code = opt["code"]
        option_strike = opt["strike_price"]
        option_price = self._calc_mid_price(opt)

        if not option_price or option_price <= 0:
            log.error("[ORBNative] オプション価格取得失敗: %s", option_code)
            return None

        if option_price >= DEEP_ITM_THRESHOLD_0DTE:
            log.error(
                "[ORBNative] deep ITM 異常価格 → 発注拒否: $%.2f (threshold=$%.0f)",
                option_price, DEEP_ITM_THRESHOLD_0DTE,
            )
            return None

        cash = self._get_cash()
        qty = self._calc_qty(cash, option_price)
        signal_id = signal_id or self._make_signal_id(ticker, direction, "orb")

        order_id = self._place_buy(
            option_code, qty, f"ORB_{direction}",
            mid_price=self._calc_mid_price(opt),
            vix=vix,
            signal_id=signal_id,
        )
        if order_id is None and not self.dry_test:
            log.error("[ORBNative] 発注失敗")
            return None

        pos = ORBNativePosition(
            code=option_code,
            qty=qty,
            entry_price=option_price,
            direction=direction,
            orb_high=self.orb_high,
            orb_low=self.orb_low,
        )
        log.info(
            "[ORBNative] 発注OK: %s direction=%s strike=%s x%d @ $%.2f",
            option_code, direction, option_strike, qty, option_price,
        )
        return pos

    # ------------------------------------------------------------------
    # Phase 4b: エントリー実行（1DTE）
    # ------------------------------------------------------------------

    def execute_entry_1dte(
        self,
        direction: str,
        signal_id: Optional[str] = None,
    ) -> Optional[ORBNativePosition]:
        """翌営業日満期 (1DTE) ORB エントリー。"""
        orig_underlying = self._underlying_code()
        try:
            return self._execute_entry_1dte_impl(direction, signal_id=signal_id)
        finally:
            if self.mkt is not None:
                try:
                    if self.mkt.underlying_code != orig_underlying:
                        log.info(
                            "[ORBNative] 1DTE underlying_code 復元: %s → %s",
                            self.mkt.underlying_code, orig_underlying,
                        )
                        self.mkt.underlying_code = orig_underlying
                except Exception:
                    pass

    def _execute_entry_1dte_impl(
        self, direction: str, signal_id: Optional[str] = None
    ) -> Optional[ORBNativePosition]:
        if _is_past_entry_cutoff(dry_test=self.dry_test):
            log.info("[ORBNative] execute_entry_1dte: 15:30 ET 以降 → エントリー中止")
            self.trade_done = True
            return None

        if kill_switch_is_active():
            log.warning("[ORBNative] execute_entry_1dte: Kill Switch ARMED → 中止")
            self.trade_done = True
            return None

        underlying_code = self._underlying_code()
        ticker = self._get_ticker()

        now_et = datetime.datetime.now(ET)
        next_day = now_et + datetime.timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += datetime.timedelta(days=1)
        expiry_1dte = next_day.strftime("%Y-%m-%d")

        underlying_price = self._get_underlying_price()
        if not underlying_price or underlying_price <= 0:
            log.error("[ORBNative] 1DTE: %s 価格取得失敗", ticker)
            return None

        atm_strike = round(underlying_price)
        vix = self.today_vix or 20.0

        if self.dry_test:
            return self._dry_entry_1dte(direction, ticker, atm_strike, next_day, expiry_1dte, signal_id)

        chain = self.mkt.get_option_chain_with_greeks(
            expiry_1dte, direction, center_strike=float(atm_strike)
        )
        if not chain:
            log.error("[ORBNative] 1DTE チェーン取得失敗 (%s %s)", direction, expiry_1dte)
            return None

        opt = self._find_1dte_option(chain, direction, atm_strike)
        if opt is None:
            log.error("[ORBNative] 1DTE オプション選択失敗")
            return None

        if not self._check_strike_deviation(
            opt.get("strike_price", 0), atm_strike, STRIKE_DEVIATION_MAX_1DTE, underlying_code
        ):
            return None

        option_code = opt["code"]
        option_strike = opt["strike_price"]
        option_price = self._calc_mid_price(opt)

        if not option_price or option_price <= 0:
            log.error("[ORBNative] 1DTE オプション価格取得失敗: %s", option_code)
            return None

        if option_price >= DEEP_ITM_THRESHOLD_1DTE:
            log.error("[ORBNative] 1DTE deep ITM 異常価格 → 発注拒否: $%.2f", option_price)
            return None

        cash = self._get_cash()
        qty = self._calc_qty_1dte(cash, option_price)
        signal_id = signal_id or self._make_signal_id(ticker, direction, "orb1dte")

        order_id = self._place_buy(
            option_code, qty, f"ORB1DTE_{direction}",
            mid_price=self._calc_mid_price(opt),
            vix=vix,
            signal_id=signal_id,
        )
        if order_id is None and not self.dry_test:
            log.error("[ORBNative] 1DTE 発注失敗")
            return None

        pos = ORBNativePosition(
            code=option_code,
            qty=qty,
            entry_price=option_price,
            direction=direction,
            orb_high=self.orb_high or underlying_price,
            orb_low=self.orb_low or underlying_price,
            _is_1dte=True,
        )
        log.info(
            "[ORBNative] 1DTE 発注OK: %s direction=%s exp=%s x%d @ $%.2f",
            option_code, direction, expiry_1dte, qty, option_price,
        )
        return pos

    # ------------------------------------------------------------------
    # Phase 5: エグジット監視（毎 tick 呼び出す）
    # ------------------------------------------------------------------

    def check_exit(
        self, intraday_monitor: Optional[Any] = None
    ) -> Optional[dict]:
        """保有ポジションの TP/SL/タイムストップを毎 tick チェックする。

        Returns:
            決済完了時: {"reason": str, "exit_price": float, "pnl_usd": float}
            継続中: None
        """
        if self.position is None:
            return None

        pos = self.position
        now_et_time = datetime.datetime.now(ET).time()

        if _is_early_close_today():
            time_stop = datetime.time(EARLY_CLOSE_EXIT_H, EARLY_CLOSE_EXIT_M)
        else:
            time_stop = datetime.time(ORB_EXIT_TIME_H, ORB_EXIT_TIME_M)

        if not self.dry_test and now_et_time >= time_stop:
            exit_price = self._get_option_price(pos) or pos.entry_price * 0.3
            log.info("[ORBNative] タイムストップ: %s", time_stop)
            return self._close_position(pos, exit_price, "time_stop")

        current_price = self._get_option_price(pos)
        if not current_price or current_price <= 0:
            return None

        pnl_pct = (current_price - pos.entry_price) / pos.entry_price

        if intraday_monitor is not None and not self.dry_test:
            try:
                regime = intraday_monitor.current_regime
                if regime == "crisis" and pnl_pct > 0:
                    log.warning(
                        "[ORBNative] Crisis regime: 含み益%.1f%% → 即利確", pnl_pct * 100
                    )
                    return self._close_position(pos, current_price, "crisis_profit_take")
            except Exception:
                log.debug("[ORBNative] intraday_monitor.current_regime 取得失敗（無視）")

        reason = pos.check_exit(current_price)
        if reason:
            return self._close_position(pos, current_price, reason)
        return None

    # ------------------------------------------------------------------
    # 内部ヘルパー: 価格取得
    # ------------------------------------------------------------------

    def _underlying_code(self) -> str:
        if self.mkt is not None:
            try:
                return self.mkt.underlying_code
            except Exception:
                pass
        return "US.SPY"

    def _get_ticker(self) -> str:
        code = self._underlying_code()
        return normalize_symbol(code)

    def _get_underlying_price(self) -> Optional[float]:
        """mkt.get_last_price() → symbol_aware_price → Finnhub fallback の順で取得。"""
        underlying_code = self._underlying_code()
        ticker = normalize_symbol(underlying_code)

        if self.dry_test:
            return self._fetch_price_dry(ticker)

        if self.mkt is not None:
            try:
                price, source = get_current_price_with_fallback(
                    underlying_code,
                    self.mkt,
                    fallback_price=_get_fallback_price(ticker),
                )
                if source != "fallback":
                    return price
            except Exception as exc:
                log.debug("[ORBNative] get_current_price_with_fallback: %s", exc)

            # symbol_aware_price 経由で取得できなかった場合は Finnhub 直接
            return self._fetch_price_finnhub(ticker)

        return self._fetch_price_finnhub(ticker)

    def _fetch_price_dry(self, ticker: str) -> float:
        """dry_test 用: Finnhub 取得→失敗時フォールバック。"""
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": self._finnhub_token()},
                timeout=5,
            )
            p = float(resp.json().get("c") or 0)
            return p if p > 0 else _get_fallback_price(ticker)
        except Exception:
            return _get_fallback_price(ticker)

    def _fetch_price_finnhub(self, ticker: str) -> Optional[float]:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": self._finnhub_token()},
                timeout=5,
            )
            p = float(resp.json().get("c") or 0)
            return p if p > 0 else None
        except Exception as exc:
            log.debug("[ORBNative] Finnhub price fetch: %s", exc)
            return None

    def _get_option_price(self, pos: ORBNativePosition) -> Optional[float]:
        """保有オプション現在価格を取得する。"""
        if self.dry_test:
            now_et = datetime.datetime.now(ET)
            session_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            session_end = now_et.replace(hour=15, minute=30, second=0, microsecond=0)
            total_secs = (session_end - session_start).total_seconds()
            elapsed = max(0.0, (now_et - session_start).total_seconds())
            decay = min(elapsed / total_secs, 1.0) if total_secs > 0 else 0.5
            return round(max(pos.entry_price * (1.5 - decay), pos.entry_price * 0.1), 4)

        if self.mkt is None:
            return None
        try:
            cached = self.mkt.get_cached_option_price(pos.code, max_age_sec=15.0)
            if cached and cached > 0:
                return cached
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 内部ヘルパー: 1 分足取得
    # ------------------------------------------------------------------

    def _get_1min_bars(self, minutes: int = 10) -> list[dict]:
        """Yahoo Finance → Finnhub の順で 1 分足を取得する。"""
        ticker = self._get_ticker()
        yahoo_ticker = ticker

        try:
            end_ts = int(time.time())
            start_ts = end_ts - 3600
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}",
                params={"period1": start_ts, "period2": end_ts, "interval": "1m"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            data = resp.json()
            result_data = data["chart"]["result"][0]
            timestamps = result_data["timestamp"]
            quotes = result_data["indicators"]["quote"][0]
            bars: list[dict] = []
            for i, ts in enumerate(timestamps):
                o = quotes.get("open", [None] * len(timestamps))[i]
                h = quotes.get("high", [None] * len(timestamps))[i]
                lo = quotes.get("low", [None] * len(timestamps))[i]
                c = quotes.get("close", [None] * len(timestamps))[i]
                if None in (o, h, lo, c):
                    continue
                bars.append({
                    "time": datetime.datetime.fromtimestamp(ts, tz=ET),
                    "open": float(o), "high": float(h),
                    "low": float(lo), "close": float(c),
                })
            if bars:
                return bars[-minutes:] if len(bars) > minutes else bars
        except Exception as exc:
            log.debug("[ORBNative] 1min Yahoo %s: %s", ticker, exc)

        try:
            end_ts = int(time.time())
            start_ts = end_ts - 3600
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/candle",
                params={
                    "symbol": ticker, "resolution": "1",
                    "from": start_ts, "to": end_ts,
                    "token": self._finnhub_token(),
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("s") == "no_data":
                return []
            bars = []
            for i, ts in enumerate(data.get("t", [])):
                bars.append({
                    "time": datetime.datetime.fromtimestamp(ts, tz=ET),
                    "open": float(data["o"][i]), "high": float(data["h"][i]),
                    "low": float(data["l"][i]), "close": float(data["c"][i]),
                })
            return bars[-minutes:] if len(bars) > minutes else bars
        except Exception as exc:
            log.debug("[ORBNative] 1min Finnhub %s: %s", ticker, exc)

        return []

    # ------------------------------------------------------------------
    # 内部ヘルパー: サイズ計算
    # ------------------------------------------------------------------

    def _calc_target_delta(self, vix: float) -> float:
        """VIX 履歴パーセンタイルで delta を動的決定する（spy_bot と同一ロジック）。"""
        if self.mkt is None:
            return 0.50
        try:
            vix_history = self.mkt.get_vix_history(days=60)
        except Exception:
            return 0.50
        if len(vix_history) < 20:
            return 0.50
        sorted_h = sorted(vix_history)
        n = len(sorted_h)
        p50 = sorted_h[int(0.50 * (n - 1))]
        p80 = sorted_h[int(0.80 * (n - 1))]
        if vix < p50:
            return 0.50
        if vix < p80:
            return 0.60
        return 0.70

    def _calc_qty(self, cash: float, option_price: float) -> int:
        """0DTE サイズ計算（Kelly / VIX9D / Gap / 時間帯係数）。"""
        risk_pct = (self._kelly_fraction
                    if (self._kelly_fraction and self._kelly_fraction > 0)
                    else ORB_MAX_RISK_PCT)
        risk = cash * risk_pct * self._vix9d_vvix_factor * self._time_zone_factor
        max_loss = option_price * abs(ORB_SL_PCT) * 100
        if max_loss <= 0:
            return 1
        qty = max(1, int(risk / max_loss))
        qty = min(qty, ORB_MAX_QTY)
        if cash < ORB_SMALL_ACCOUNT_USD:
            qty = min(qty, 1)
        gap_factor = self._gap_size_factor
        if gap_factor > 1.0:
            qty = min(int(qty * gap_factor), ORB_MAX_QTY)
        return qty

    def _calc_qty_1dte(self, cash: float, option_price: float) -> int:
        """1DTE サイズ計算（SL=-50%・1DTE BT 最適値）。"""
        risk_pct = (self._kelly_fraction
                    if (self._kelly_fraction and self._kelly_fraction > 0)
                    else ORB_MAX_RISK_PCT)
        risk = cash * risk_pct * self._vix9d_vvix_factor * self._time_zone_factor
        max_loss = option_price * 0.50 * 100
        if max_loss <= 0:
            return 1
        qty = max(1, int(risk / max_loss))
        qty = min(qty, ORB_MAX_QTY)
        if cash < ORB_SMALL_ACCOUNT_USD:
            qty = min(qty, 1)
        return qty

    def _get_cash(self) -> float:
        if self.eng is not None:
            try:
                cash = self.eng.get_account_cash()
                if cash and cash > 0:
                    return cash
            except Exception:
                pass
        return 10_000.0

    # ------------------------------------------------------------------
    # 内部ヘルパー: エントリー補助
    # ------------------------------------------------------------------

    def _update_time_zone_factor(self) -> None:
        entry_time = datetime.datetime.now(ET).time()
        prime_end = datetime.time(ORB_PRIME_END_H, ORB_PRIME_END_M)
        self._time_zone_factor = ORB_LATE_FACTOR if entry_time >= prime_end else 1.0

    def _update_gap_size_factor(self, direction: str) -> None:
        if self._gap_pct is not None and abs(self._gap_pct) >= ORB_GAP_THRESHOLD_PCT:
            gap_up = self._gap_pct > 0
            is_call = direction == "CALL"
            self._gap_size_factor = (
                ORB_GAP_BULL_SIZE_BOOST
                if (gap_up and is_call) or (not gap_up and not is_call)
                else 1.0
            )

    def _check_strike_deviation(
        self,
        opt_strike: float,
        atm_strike: float,
        max_dev: float,
        underlying_code: str,
    ) -> bool:
        dev = abs(opt_strike - float(atm_strike)) / max(float(atm_strike), 1.0)
        if dev > max_dev:
            log.error(
                "[ORBNative] strike 整合性 NG: option_strike=%.0f vs atm=%.0f "
                "乖離=%.1f%% underlying=%s → エントリー中止",
                opt_strike, atm_strike, dev * 100, underlying_code,
            )
            return False
        return True

    def _calc_mid_price(self, opt: dict) -> Optional[float]:
        bid = opt.get("bid_price", 0)
        ask = opt.get("ask_price", 0)
        if bid and ask:
            return (bid + ask) / 2
        return opt.get("last_price") or None

    def _find_1dte_option(
        self, chain: list[dict], direction: str, atm_strike: float
    ) -> Optional[dict]:
        """delta 0.40 OTM に最も近い 1DTE オプションを選択する。"""
        best: Optional[dict] = None
        best_dd = 999.0
        for item in chain:
            d = item.get("delta", 0)
            if direction == "CALL":
                if not (ORB1DTE_DELTA_MIN <= d <= ORB1DTE_DELTA_MAX):
                    continue
            else:
                if not (-ORB1DTE_DELTA_MAX <= d <= -ORB1DTE_DELTA_MIN):
                    continue
            dd = abs(abs(d) - ORB1DTE_DELTA_TARGET)
            if dd < best_dd:
                best_dd = dd
                best = item
        if best is None and self.mkt is not None:
            best = self.mkt.find_by_strike(chain, float(atm_strike))
        return best

    def _make_signal_id(self, ticker: str, direction: str, prefix: str) -> str:
        bar_ts = datetime.datetime.now(ET).strftime("%Y%m%d%H%M")
        return f"{prefix}_{ticker}_{direction}_{bar_ts}"

    def _place_buy(
        self,
        code: str,
        qty: int,
        label: str,
        mid_price: Optional[float],
        vix: float,
        signal_id: str,
    ) -> Optional[str]:
        if self.eng is None:
            log.info("[ORBNative][DRY-RUN] BUY %s x%d", code, qty)
            return "DRY_ORDER"
        try:
            high_vix = vix > 30
            use_limit = not high_vix and mid_price is not None
            return self.eng.place_buy(
                code=code,
                qty=qty,
                label=label,
                init_price=mid_price if use_limit else None,
                use_limit=use_limit,
                signal_id=signal_id,
            )
        except Exception as exc:
            log.error("[ORBNative] place_buy 失敗: %s", exc)
            return None

    def _close_position(
        self, pos: ORBNativePosition, exit_price: float, reason: str
    ) -> dict:
        """ポジション決済・PnL 計算・状態更新。"""
        remaining_qty = pos.qty - pos.partial_closed
        pnl_usd = (exit_price - pos.entry_price) * remaining_qty * 100
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0

        log.info(
            "[ORBNative] 決済(%s): %s %d枚 @ $%.2f P&L=$%.2f (%.1f%%)",
            reason, pos.direction, remaining_qty, exit_price, pnl_usd, pnl_pct * 100,
        )

        if self.eng is not None and not self.dry_test:
            try:
                oid = self.eng.place_sell(pos.code, remaining_qty, "orb_close")
                if oid is None:
                    log.error("[ORBNative] 決済注文失敗")
            except Exception as exc:
                log.error("[ORBNative] 決済注文例外: %s", exc)

        self.position = None
        self.trade_done = True
        return {"reason": reason, "exit_price": exit_price, "pnl_usd": pnl_usd}

    # ------------------------------------------------------------------
    # dry-test エントリーヘルパー
    # ------------------------------------------------------------------

    def _dry_entry_0dte(
        self,
        direction: str,
        ticker: str,
        atm_strike: int,
        signal_id: Optional[str],
    ) -> ORBNativePosition:
        virtual_price = 1.50
        virtual_code = (
            f"US.{ticker}{datetime.datetime.now(ET).strftime('%y%m%d')}"
            f"{'C' if direction == 'CALL' else 'P'}{int(atm_strike * 1000)}"
        )
        qty = self._calc_qty(10_000.0, virtual_price)
        log.info(
            "[ORBNative][DRY-TEST] %s Entry: %s %s x%d @ $%.2f",
            ticker, direction, virtual_code, qty, virtual_price,
        )
        return ORBNativePosition(
            code=virtual_code,
            qty=qty,
            entry_price=virtual_price,
            direction=direction,
            orb_high=self.orb_high or float(atm_strike),
            orb_low=self.orb_low or float(atm_strike),
        )

    def _dry_entry_1dte(
        self,
        direction: str,
        ticker: str,
        atm_strike: int,
        next_day: datetime.datetime,
        expiry_1dte: str,
        signal_id: Optional[str],
    ) -> ORBNativePosition:
        virtual_price = 2.00
        virtual_code = (
            f"US.{ticker}{next_day.strftime('%y%m%d')}"
            f"{'C' if direction == 'CALL' else 'P'}{int(atm_strike * 1000)}"
        )
        qty = self._calc_qty_1dte(10_000.0, virtual_price)
        log.info(
            "[ORBNative][DRY-TEST] %s 1DTE Entry: %s %s x%d @ $%.2f exp=%s",
            ticker, direction, virtual_code, qty, virtual_price, expiry_1dte,
        )
        pos = ORBNativePosition(
            code=virtual_code,
            qty=qty,
            entry_price=virtual_price,
            direction=direction,
            orb_high=self.orb_high or float(atm_strike),
            orb_low=self.orb_low or float(atm_strike),
            _is_1dte=True,
        )
        return pos

    # ------------------------------------------------------------------
    # Finnhub トークン取得
    # ------------------------------------------------------------------

    @staticmethod
    def _finnhub_token() -> str:
        """環境変数 FINNHUB_API_KEY から token を取得する。未設定時は空文字。"""
        import os
        return os.environ.get("FINNHUB_API_KEY", "")

    # ------------------------------------------------------------------
    # static: should_trade_today（strategy_selector 連携用）
    # ------------------------------------------------------------------

    @staticmethod
    def should_trade_today(
        vix: Optional[float],
        assessment: Optional[dict] = None,
        paper: bool = False,
    ) -> bool:
        """環境データから ORB エントリーが適切かを判定する（spy_bot 互換）。"""
        if vix is None:
            return False
        if not paper:
            if vix < ORB_VIX_MIN or vix > ORB_VIX_MAX:
                log.info(
                    "[ORBNative] Skip: VIX=%.2f out of range [%.1f, %.1f]",
                    vix, ORB_VIX_MIN, ORB_VIX_MAX,
                )
                return False
        if assessment:
            score = assessment.get("score", 100.0)
            gap_pct = assessment.get("gap_pct")
            if gap_pct is not None and abs(gap_pct) >= ORB_GAP_THRESHOLD_PCT:
                score += 20.0
            if score < DYNAMIC_ENTRY_MIN_ENV_SCORE:
                return False
        return True
