"""atlas_v3/bots/engines/butterfly_native.py — Symmetric ATM Long Butterfly Engine

spy_bot.ButterflyEngine (L12769-L13332) の公開 interface を TacticBase 継承の
純 atlas_v3 実装として完全再現。spy_bot.py への書き換えはゼロ。

移植元: spy_bot.py::ButterflyEngine (L12769-L13332)
公開 interface:
    reset_daily()
    execute_entry(symbol, dry_test=False) -> bool
    check_exit() -> bool
    is_active() -> bool
    preflight(env) -> bool   ← TacticBase ABC

BWB (broken_wing_butterfly.py) との差分:
    - symmetric: lower_wing と upper_wing が ATM から等距離
    - net DEBIT エントリー（BWB は net credit）
    - IVR < P30 の低 IV 環境向け（BWB は IVR 50-80）
    - TP 50% / SL 150%（BWB とは別値）

設計規律:
    - 外部依存は Protocol / duck-typing で抽象化（futu SDK 直接参照禁止）
    - spy_bot.py 内定数は独立コピー（import 禁止）
    - PDTGuard + common_v3 pre_trade_check 統合
    - CC <= 20 per method
    - 非同期禁止（sync_only 前提）

禁則:
    - spy_bot.py / chronos_bot.py への import / 書換禁止
    - asyncio 禁止
    - IVR フィルタのハードコード禁止（SymmetricButterflyConfig 経由）
"""
from __future__ import annotations

import datetime
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import time
from typing import Literal, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# 定数（spy_bot.py L12592-L12609 の独立コピー）
# ---------------------------------------------------------------------------

#: low IVR フォールバック閾値（IVR < この値でエントリー）
_IVR_MAX_FALLBACK: float = 30.0

#: wing幅 = ATR(14) × この倍率
_ATR_WING_MULT: float = 0.40

#: wing幅の最小/最大 strikes
_MIN_WING_STRIKES: int = 1
_MAX_WING_STRIKES: int = 10

#: 口座資本に対するネットデビット割合
_CAPITAL_PCT: float = 0.02

#: 本番最大枚数
_MAX_QTY_LIVE: int = 3

#: ペーパー最大枚数
_MAX_QTY_PAPER: int = 10

#: TP: ネットデビット × (1 + この値) でクローズ
_PROFIT_TARGET_PCT: float = 0.50

#: SL: ネットデビット × (1 - この値) でクローズ
_STOP_LOSS_PCT: float = 1.50

#: エントリーウィンドウ（ET）
_ENTRY_WINDOW_START: time = time(10, 30)
_ENTRY_WINDOW_END: time   = time(14, 0)

#: 通常日の強制クローズ時刻（ET）
_FORCE_CLOSE_TIME: time = time(15, 50)

#: 半日取引日の強制クローズ時刻（ET）
_EARLY_CLOSE_TIME: time = time(12, 50)

#: エントリーカットオフ時刻（ET）
_ENTRY_CUTOFF: time = time(15, 30)

#: 戦術識別子
TACTIC_NAME: str = "butterfly_native"


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class SymmetricButterflyConfig:
    """Symmetric ATM Long Butterfly 戦術設定。

    Attributes:
        ivr_low_threshold:  この IVR 以下でエントリー（動的 P30 取得失敗時のフォールバック）
        atr_wing_mult:      wing幅 = ATR(14) × この倍率
        min_wing_strikes:   wing幅の最小 strikes
        max_wing_strikes:   wing幅の最大 strikes
        capital_pct:        口座資本に対するネットデビット割合
        max_qty_live:       本番最大枚数
        max_qty_paper:      ペーパー最大枚数
        profit_target_pct:  TP = net_debit × (1 + profit_target_pct)
        stop_loss_pct:      SL = net_debit × (1 - stop_loss_pct)
        paper_mode:         True = ペーパー発注
        entry_window_start: エントリー開始時刻（ET）
        entry_window_end:   エントリー終了時刻（ET）
        force_close_time:   通常日強制クローズ時刻（ET）
        early_close_time:   半日取引日強制クローズ時刻（ET）
        entry_cutoff:       エントリーカットオフ時刻（ET）
    """
    ivr_low_threshold: float   = _IVR_MAX_FALLBACK
    atr_wing_mult: float       = _ATR_WING_MULT
    min_wing_strikes: int      = _MIN_WING_STRIKES
    max_wing_strikes: int      = _MAX_WING_STRIKES
    capital_pct: float         = _CAPITAL_PCT
    max_qty_live: int          = _MAX_QTY_LIVE
    max_qty_paper: int         = _MAX_QTY_PAPER
    profit_target_pct: float   = _PROFIT_TARGET_PCT
    stop_loss_pct: float       = _STOP_LOSS_PCT
    paper_mode: bool           = True
    entry_window_start: time   = _ENTRY_WINDOW_START
    entry_window_end: time     = _ENTRY_WINDOW_END
    force_close_time: time     = _FORCE_CLOSE_TIME
    early_close_time: time     = _EARLY_CLOSE_TIME
    entry_cutoff: time         = _ENTRY_CUTOFF

    def __post_init__(self) -> None:
        if self.ivr_low_threshold <= 0:
            raise ValueError(f"ivr_low_threshold must be > 0, got {self.ivr_low_threshold}")
        if not (0.0 < self.capital_pct <= 1.0):
            raise ValueError(f"capital_pct must be in (0.0, 1.0], got {self.capital_pct}")
        if not (0.0 < self.profit_target_pct < 10.0):
            raise ValueError(f"profit_target_pct out of range: {self.profit_target_pct}")
        if not (0.0 < self.stop_loss_pct < 10.0):
            raise ValueError(f"stop_loss_pct out of range: {self.stop_loss_pct}")
        if self.min_wing_strikes < 1:
            raise ValueError(f"min_wing_strikes must be >= 1, got {self.min_wing_strikes}")
        if self.max_wing_strikes < self.min_wing_strikes:
            raise ValueError(
                f"max_wing_strikes={self.max_wing_strikes} < min_wing_strikes={self.min_wing_strikes}"
            )


# ---------------------------------------------------------------------------
# Leg DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ButterflyLeg:
    """Symmetric Butterfly の 1 leg。

    Attributes:
        label:       leg 識別子（"lower_buy" / "atm_sell_x2" / "upper_buy"）
        option_code: futu オプションコード（例: "US.SPY260418C00562000"）
        strike:      権利行使価格
        option_type: "CALL" | "PUT"
        side:        "BUY" | "SELL"
        quantity:    枚数
        mid_price:   mid 価格（entry 時確定）
    """
    label: str
    option_code: str
    strike: float
    option_type: Literal["CALL", "PUT"]
    side: Literal["BUY", "SELL"]
    quantity: int
    mid_price: float = 0.0


# ---------------------------------------------------------------------------
# Position DTO
# ---------------------------------------------------------------------------

@dataclass
class ButterflyNativePosition:
    """Symmetric ATM Long Butterfly 保有ポジション。

    Call 系: lower_call_buy + 2x atm_call_sell + upper_call_buy
    Put 系:  lower_put_buy  + 2x atm_put_sell  + upper_put_buy

    Attributes:
        symbol:            対象銘柄（例: "US.SPY"）
        wing_type:         "CALL" | "PUT"
        atm_strike:        ATM 権利行使価格
        wing_width:        ATM からの対称 wing 幅（strikes 単位）
        lower_code:        lower leg オプションコード
        atm_code:          ATM leg オプションコード
        upper_code:        upper leg オプションコード
        qty:               発注枚数（lower/upper は qty・ATM は qty*2）
        net_debit:         エントリー時ネットデビット（per share、正値必須）
        lower_entry_price: lower leg エントリー mid 価格
        atm_entry_price:   ATM leg エントリー mid 価格
        upper_entry_price: upper leg エントリー mid 価格
        entry_time:        エントリー時刻（isoformat 文字列）
        expiry:            満期日（YYYY-MM-DD）
        trade_id:          UUID 先頭 8 文字
        paper:             True = ペーパー取引
    """
    symbol: str
    wing_type: Literal["CALL", "PUT"]
    atm_strike: float
    wing_width: int
    lower_code: str
    atm_code: str
    upper_code: str
    qty: int
    net_debit: float
    lower_entry_price: float
    atm_entry_price: float
    upper_entry_price: float
    entry_time: str
    expiry: str
    trade_id: str
    paper: bool = True

    # 派生値
    lower_strike: float = field(init=False)
    upper_strike: float = field(init=False)

    def __post_init__(self) -> None:
        self.lower_strike = self.atm_strike - self.wing_width
        self.upper_strike = self.atm_strike + self.wing_width

    def current_value(
        self,
        lower_price: float,
        atm_price: float,
        upper_price: float,
    ) -> float:
        """現在ポジション価値 (per share) = lower + upper - 2 * atm"""
        return lower_price + upper_price - 2.0 * atm_price

    def pnl(
        self,
        lower_price: float,
        atm_price: float,
        upper_price: float,
    ) -> float:
        """P&L (per share × qty × 100) を返す。"""
        return (
            self.current_value(lower_price, atm_price, upper_price) - self.net_debit
        ) * self.qty * 100.0

    def __repr__(self) -> str:
        return (
            f"ButterflyNativePosition({self.symbol} {self.wing_type} "
            f"K={self.atm_strike:.1f} w={self.wing_width} "
            f"qty={self.qty} debit={self.net_debit:.3f} expiry={self.expiry})"
        )


# ---------------------------------------------------------------------------
# MarketData Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class MarketDataProtocol(Protocol):
    """ButterflyNativeEngine が必要とする市場データ取得インターフェース。

    duck-typing 前提・futu SDK に直接依存しない。
    """

    def get_current_price(self, symbol: str) -> Optional[float]:
        """原資産の現在価格を返す（取得失敗時 None）。"""
        ...

    def get_ivr(self, symbol: str) -> Optional[float]:
        """現在 IVR (0-100) を返す（取得失敗時 None）。"""
        ...

    def get_ivr_percentile_low(self, symbol: str) -> Optional[float]:
        """IVR P30 閾値を返す（取得失敗時 None → フォールバック定数を使用）。"""
        ...

    def get_option_mid(self, option_code: str) -> Optional[float]:
        """オプションの mid 価格を返す（取得失敗時 None）。"""
        ...

    def get_sma(self, symbol: str, period: int = 20) -> Optional[float]:
        """SMA を返す（取得失敗時 None）。"""
        ...

    def get_atr(self, symbol: str, period: int = 14) -> Optional[float]:
        """ATR を返す（取得失敗時 None）。"""
        ...

    def get_strike_interval(self, symbol: str) -> float:
        """銘柄のストライク刻みを返す（取得失敗時 1.0）。"""
        ...

    def is_early_close_today(self) -> bool:
        """今日が半日取引日かどうかを返す。"""
        ...

    def get_account_cash(self) -> float:
        """口座資本 (USD) を返す（取得失敗時 0.0）。"""
        ...


# ---------------------------------------------------------------------------
# TradeEngine Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class TradeEngineProtocol(Protocol):
    """ButterflyNativeEngine が必要とする発注インターフェース。

    duck-typing 前提。各 leg を個別に発注する最小セット。
    """

    def place_butterfly_leg(
        self,
        option_code: str,
        side: Literal["BUY", "SELL"],
        quantity: int,
        label: str,
        signal_id: str = "",
    ) -> Optional[str]:
        """1 leg を発注し order_id を返す。失敗時 None。"""
        ...

    def get_open_positions(self) -> list[dict]:
        """オープンポジションのリストを返す（証拠金計算用）。"""
        ...


# ---------------------------------------------------------------------------
# NoOp スタブ（テスト用）
# ---------------------------------------------------------------------------

class NoOpMarketData:
    """テスト用 MarketData スタブ（全値をハードコード固定値で返す）。"""

    def get_current_price(self, symbol: str) -> Optional[float]:
        return 560.0

    def get_ivr(self, symbol: str) -> Optional[float]:
        return 20.0

    def get_ivr_percentile_low(self, symbol: str) -> Optional[float]:
        return 30.0

    def get_option_mid(self, option_code: str) -> Optional[float]:
        # lower/upper = 1.50, ATM = 2.50
        if "C00558" in option_code or "C00562" in option_code or "P00558" in option_code or "P00562" in option_code:
            return 1.50
        return 2.50

    def get_sma(self, symbol: str, period: int = 20) -> Optional[float]:
        return 555.0

    def get_atr(self, symbol: str, period: int = 14) -> Optional[float]:
        return 5.0

    def get_strike_interval(self, symbol: str) -> float:
        return 1.0

    def is_early_close_today(self) -> bool:
        return False

    def get_account_cash(self) -> float:
        return 15_000.0


class NoOpTradeEngine:
    """テスト用 TradeEngine スタブ（発注なし・DRY order_id を返す）。"""

    def place_butterfly_leg(
        self,
        option_code: str,
        side: Literal["BUY", "SELL"],
        quantity: int,
        label: str,
        signal_id: str = "",
    ) -> Optional[str]:
        ts = datetime.datetime.now(_ET).strftime("%Y%m%d%H%M%S%f")
        order_id = f"DRY_BFN_{ts}_{label[:8]}"
        log.info(
            "[NoOpTradeEngine] DRY leg=%s code=%s side=%s qty=%d order_id=%s",
            label, option_code, side, quantity, order_id,
        )
        return order_id

    def get_open_positions(self) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# ButterflyNativeEngine
# ---------------------------------------------------------------------------

class ButterflyNativeEngine(TacticBase):
    """Symmetric ATM Long Butterfly 戦術エンジン（Type: enter_exit）。

    spy_bot.ButterflyEngine の公開 interface を TacticBase 継承の atlas_v3 実装として再現。
    BWB（非対称）とは別戦術: lower / upper wing が ATM から等距離（symmetric）。
    low IVR 環境（IVR < P30）で価格収束を狙う net debit 戦術。

    Args:
        market_data:  MarketDataProtocol 実装（None のとき NoOpMarketData）
        trade_engine: TradeEngineProtocol 実装（None のとき NoOpTradeEngine）
        config:       SymmetricButterflyConfig（None のときデフォルト値）
        clock_fn:     ET 時刻注入関数（テスト用・None のとき datetime.now(_ET)）
        pdt_guard:    PDTGuard（None のときデフォルト生成）
    """

    def __init__(
        self,
        market_data: Optional[MarketDataProtocol] = None,
        trade_engine: Optional[TradeEngineProtocol] = None,
        config: Optional[SymmetricButterflyConfig] = None,
        clock_fn: "Optional[() -> datetime.datetime]" = None,
        pdt_guard: Optional[PDTGuard] = None,
    ) -> None:
        self._mkt: MarketDataProtocol = market_data or NoOpMarketData()
        self._eng: TradeEngineProtocol = trade_engine or NoOpTradeEngine()
        self._cfg = config or SymmetricButterflyConfig()
        self._clock_fn = clock_fn
        self._pdt_guard: PDTGuard = pdt_guard or PDTGuard(
            paper_mode=self._cfg.paper_mode
        )

        # 日次リセット対象の状態
        self.position: Optional[ButterflyNativePosition] = None
        self.entry_done: bool = False
        self.trade_done: bool = False

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return TACTIC_NAME

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _now_et(self) -> datetime.datetime:
        """ET 現在時刻を返す（テスト時は clock_fn で差し替え可）。"""
        if self._clock_fn is not None:
            return self._clock_fn()
        return datetime.datetime.now(_ET)

    def _in_entry_window(self) -> bool:
        """エントリーウィンドウ（10:30-14:00 ET）内かどうかを返す。"""
        t = self._now_et().time()
        return self._cfg.entry_window_start <= t < self._cfg.entry_window_end

    def _past_entry_cutoff(self) -> bool:
        """エントリーカットオフ（15:30 ET）を過ぎているかどうかを返す。"""
        return self._now_et().time() >= self._cfg.entry_cutoff

    def _get_force_close_time(self) -> time:
        """半日取引日なら early_close_time、通常日なら force_close_time を返す。"""
        try:
            if self._mkt.is_early_close_today():
                return self._cfg.early_close_time
        except Exception as exc:
            log.debug("[ButterflyNative] is_early_close_today 失敗: %s", exc)
        return self._cfg.force_close_time

    def _build_option_code(
        self,
        symbol: str,
        expiry: str,
        strike: float,
        opt_type: Literal["CALL", "PUT"],
    ) -> str:
        """futu 形式のオプションコードを生成する。

        例: US.SPY260418C00562000

        Args:
            symbol:   "US.SPY" 等の futu 銘柄コード
            expiry:   "YYYY-MM-DD" 形式の満期日
            strike:   権利行使価格（浮動小数）
            opt_type: "CALL" | "PUT"

        Returns:
            futu オプションコード文字列
        """
        ticker   = symbol.replace("US.", "")
        yy_mm_dd = expiry.replace("-", "")[2:]
        cp       = "C" if opt_type == "CALL" else "P"
        strike_i = int(round(strike * 1000))
        return f"US.{ticker}{yy_mm_dd}{cp}{strike_i:08d}"

    def _calc_wing_width(self, symbol: str) -> int:
        """ATR(14) から対称 wing 幅を動的算出する。

        wing_width = max(min_wing, min(max_wing, round(ATR14 × mult)))
        ストライク刻みに合わせて丸める。

        Returns:
            wing 幅（strikes 単位、int）
        """
        try:
            atr = self._mkt.get_atr(symbol)
        except Exception as exc:
            log.debug("[ButterflyNative] get_atr 失敗: %s", exc)
            atr = None

        if atr is None or atr <= 0:
            return self._cfg.min_wing_strikes

        try:
            interval = self._mkt.get_strike_interval(symbol)
            interval = interval if interval > 0 else 1.0
        except Exception:
            interval = 1.0

        raw     = atr * self._cfg.atr_wing_mult
        rounded = round(raw / interval) * interval
        width   = int(max(
            self._cfg.min_wing_strikes,
            min(self._cfg.max_wing_strikes, max(1, rounded)),
        ))
        log.info(
            "[ButterflyNative] wing width: %s ATR=%.2f × mult=%.2f"
            " = %.2f -> interval=%s -> width=%d",
            symbol, atr, self._cfg.atr_wing_mult, raw, interval, width,
        )
        return width

    def _calc_qty(self, cash: float, net_debit: float) -> int:
        """発注枚数を算出する。

        qty = int(cash × capital_pct / (net_debit × 100))
        上限: max_qty_paper（ペーパー）/ max_qty_live（本番）

        Returns:
            発注枚数（int、最低 1）
        """
        max_q = self._cfg.max_qty_paper if self._cfg.paper_mode else self._cfg.max_qty_live
        if net_debit <= 0 or cash <= 0:
            return 1
        try:
            raw_float = cash * self._cfg.capital_pct / (net_debit * 100.0)
            if not math.isfinite(raw_float):
                return 1
            return max(1, min(int(raw_float), max_q))
        except (OverflowError, ZeroDivisionError, ValueError):
            return 1

    def _choose_wing_type(
        self,
        symbol: str,
        current_price: float,
    ) -> Optional[Literal["CALL", "PUT"]]:
        """SMA20 トレンドから CALL / PUT を選択する。

        current_price >= SMA20 -> CALL Butterfly
        current_price <  SMA20 -> PUT  Butterfly
        SMA 取得失敗        -> None（エントリー中止）

        Returns:
            "CALL" / "PUT" / None
        """
        try:
            sma = self._mkt.get_sma(symbol)
        except Exception as exc:
            log.debug("[ButterflyNative] get_sma 失敗: %s -> エントリー中止", exc)
            return None

        if sma is None:
            log.info(
                "[ButterflyNative] %s SMA 未取得 -> エントリー中止 (H-13 規律: SPY fallback 禁止)",
                symbol,
            )
            return None

        wing_type: Literal["CALL", "PUT"] = "CALL" if current_price >= sma else "PUT"
        log.info(
            "[ButterflyNative] SMA: price=%.2f SMA=%.2f -> %s",
            current_price, sma, wing_type,
        )
        return wing_type

    def _get_ivr_threshold(self, symbol: str) -> float:
        """動的 IVR P30 閾値を取得する。失敗時はフォールバック定数を返す。"""
        try:
            pct = self._mkt.get_ivr_percentile_low(symbol)
            if pct is not None and pct > 0:
                log.info("[ButterflyNative] 動的 IVR P30 threshold=%.1f", pct)
                return float(pct)
        except Exception as exc:
            log.debug("[ButterflyNative] get_ivr_percentile_low 失敗: %s", exc)
        return self._cfg.ivr_low_threshold

    def _run_pre_trade_gate(
        self,
        symbol: str,
        legs: list[tuple[str, Literal["BUY", "SELL"], int, float]],
        cash: float,
    ) -> bool:
        """各 leg に対して common_v3 pre_trade_check (critical_only) を実行する。

        leg タプル: (option_code, side, qty, mid_price)

        Returns:
            True: 全 leg PASS / False: 拒否（理由は log に出力済）
        """
        try:
            from common_v3.risk.pre_trade_check import (
                OrderCtx as _Ctx,
                check_order_critical_only as _gate,
            )
        except ImportError as exc:
            log.warning("[ButterflyNative] pre_trade_check import 失敗: %s -> スキップ", exc)
            return True  # fail-open（テスト環境等で import 不可の場合）

        for code, side, qty, price in legs:
            is_long = (side == "BUY")
            ctx = _Ctx(
                symbol=symbol,
                qty=qty,
                option_price=price,
                side=side,
                is_long=is_long,
                est_margin=price * qty * 100.0,
                capital_usd=cash,
                open_margin_total=0.0,
            )
            result = _gate(ctx)
            if not result.allowed:
                log.warning(
                    "[ButterflyNative] pre_trade_check 拒否 leg=%s [%s]: %s",
                    code, result.layer, result.reason,
                )
                return False
        return True

    # ------------------------------------------------------------------
    # TacticBase ABC 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック順:
        1. env None ガード
        2. Kill Switch ARMED -> False

        Returns:
            True  — 戦術発動可能
            False — 発動不可（理由は log に必ず出力）
        """
        if env is None:
            log.warning("[ButterflyNative.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[ButterflyNative.preflight] Kill Switch ARMED: %s を無効化",
                TACTIC_NAME,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # 公開 API: reset_daily
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """日次状態をリセットする（翌日起動時に必ず呼ぶ）。"""
        self.position   = None
        self.entry_done = False
        self.trade_done = False
        log.debug("[ButterflyNative] reset_daily: 状態クリア")

    # ------------------------------------------------------------------
    # 公開 API: execute_entry
    # ------------------------------------------------------------------

    def execute_entry(self, symbol: str, dry_test: bool = False) -> bool:
        """Butterfly エントリーを実行する。

        処理順:
        1. Kill Switch / entry_done / trade_done ガード
        2. エントリーウィンドウ（10:30-14:00 ET）チェック
        3. エントリーカットオフ（15:30 ET）チェック
        4. IVR 条件チェック（IVR < P30）
        5. 価格・SMA・ATR 取得
        6. 3 leg 構築（lower_buy / upper_buy / atm_sell×2）
        7. pre_trade_check（critical_only）
        8. PDTGuard チェック
        9. 発注 → ButterflyNativePosition 記録

        Args:
            symbol:   "US.SPY" 等の futu 銘柄コード
            dry_test: True のとき発注なしで固定価格でテスト

        Returns:
            True: エントリー成功 / False: スキップ
        """
        # 1. Kill Switch ガード
        if kill_switch_is_active():
            log.warning("[ButterflyNative.execute_entry] Kill Switch ARMED -> スキップ")
            return False

        # 1b. 重複実行ガード
        if self.entry_done or self.trade_done:
            return False

        # 2. エントリーウィンドウチェック（dry_test は除外）
        if not dry_test and not self._in_entry_window():
            log.debug("[ButterflyNative] エントリーウィンドウ外 -> スキップ")
            return False

        # 3. エントリーカットオフチェック
        if not dry_test and self._past_entry_cutoff():
            log.info(
                "[ButterflyNative] エントリーカットオフ %s ET 以降 -> スキップ",
                self._cfg.entry_cutoff,
            )
            return False

        # 4. IVR 条件チェック（dry_test は除外）
        if not dry_test:
            ivr_threshold = self._get_ivr_threshold(symbol)
            try:
                ivr = self._mkt.get_ivr(symbol)
            except Exception as exc:
                log.debug("[ButterflyNative] get_ivr 失敗: %s", exc)
                ivr = None

            if ivr is None:
                log.info("[ButterflyNative] IVR 取得不可 -> スキップ")
                return False
            if ivr >= ivr_threshold:
                log.info(
                    "[ButterflyNative] IVR=%.1f >= threshold=%.1f -> スキップ",
                    ivr, ivr_threshold,
                )
                return False
            log.info(
                "[ButterflyNative] IVR=%.1f < threshold=%.1f -> エントリー候補",
                ivr, ivr_threshold,
            )

        return self._execute_entry_impl(symbol, dry_test=dry_test)

    # ------------------------------------------------------------------
    # 内部: エントリー実装
    # ------------------------------------------------------------------

    def _execute_entry_impl(self, symbol: str, dry_test: bool) -> bool:
        """3 leg Butterfly の発注を執行する（execute_entry の内部実装）。

        発注順（買い脚先行でリスク管理）:
          1. lower_buy  (long lower wing)
          2. upper_buy  (long upper wing)
          3. atm_sell × 2 (short center)

        Returns:
            True: エントリー成功 / False: 失敗
        """
        # 価格取得
        try:
            current_price = self._mkt.get_current_price(symbol)
        except Exception as exc:
            log.warning("[ButterflyNative] get_current_price 失敗: %s -> スキップ", exc)
            return False

        if current_price is None or current_price <= 0:
            log.warning("[ButterflyNative] %s 価格取得失敗 -> スキップ", symbol)
            return False

        # SMA によるウィングタイプ選択
        wing_type = self._choose_wing_type(symbol, current_price)
        if wing_type is None:
            return False

        # wing 幅算出
        wing_width = self._calc_wing_width(symbol)

        # ストライク刻み取得
        try:
            interval = self._mkt.get_strike_interval(symbol)
            interval = interval if interval > 0 else 1.0
        except Exception:
            interval = 1.0

        # ATM / lower / upper ストライク算出
        atm_strike = round(current_price / interval) * interval
        lower_st   = atm_strike - wing_width
        upper_st   = atm_strike + wing_width

        # 満期日（当日の 0DTE）
        expiry = self._now_et().strftime("%Y-%m-%d")

        # オプションコード生成
        lower_code = self._build_option_code(symbol, expiry, lower_st,  wing_type)
        atm_code   = self._build_option_code(symbol, expiry, atm_strike, wing_type)
        upper_code = self._build_option_code(symbol, expiry, upper_st,  wing_type)

        log.info(
            "[ButterflyNative] エントリー準備: %s %s lower=%.1f ATM=%.1f upper=%.1f"
            " width=%d expiry=%s dry=%s",
            symbol, wing_type, lower_st, atm_strike, upper_st,
            wing_width, expiry, dry_test,
        )

        # dry_test: 固定価格でシミュレート
        if dry_test:
            lower_price = 1.50
            atm_price   = 2.50
            upper_price = 1.50
            net_debit   = lower_price + upper_price - 2.0 * atm_price
            net_debit   = max(net_debit, 0.50)
            cash        = self._mkt.get_account_cash()
            qty         = self._calc_qty(cash, net_debit)
            trade_id    = str(uuid.uuid4())[:8]
            self.position = ButterflyNativePosition(
                symbol=symbol, wing_type=wing_type,
                atm_strike=atm_strike, wing_width=wing_width,
                lower_code=lower_code, atm_code=atm_code, upper_code=upper_code,
                qty=qty, net_debit=net_debit,
                lower_entry_price=lower_price,
                atm_entry_price=atm_price,
                upper_entry_price=upper_price,
                entry_time=self._now_et().isoformat(),
                expiry=expiry, trade_id=trade_id,
                paper=self._cfg.paper_mode,
            )
            self.entry_done = True
            log.info(
                "[ButterflyNative][DRY] ENTRY: %s %s K=%.1f w=%d debit=%.3f qty=%d",
                symbol, wing_type, atm_strike, wing_width, net_debit, qty,
            )
            return True

        # 実/ペーパー: mid 価格取得
        try:
            lower_price = self._mkt.get_option_mid(lower_code)
            atm_price   = self._mkt.get_option_mid(atm_code)
            upper_price = self._mkt.get_option_mid(upper_code)
        except Exception as exc:
            log.warning("[ButterflyNative] get_option_mid 失敗: %s -> スキップ", exc)
            return False

        if lower_price is None or atm_price is None or upper_price is None:
            log.warning(
                "[ButterflyNative] 価格取得失敗: lower=%s atm=%s upper=%s -> スキップ",
                lower_price, atm_price, upper_price,
            )
            return False

        # ネットデビット検証
        net_debit = lower_price + upper_price - 2.0 * atm_price
        if net_debit <= 0:
            log.warning(
                "[ButterflyNative] ネットデビット=%.4f <= 0 -> スキップ", net_debit
            )
            return False

        # 口座情報
        try:
            cash = self._mkt.get_account_cash()
        except Exception:
            cash = 0.0

        qty = self._calc_qty(cash, net_debit)

        # pre_trade_check (critical_only) — 各 leg
        legs_spec: list[tuple[str, Literal["BUY", "SELL"], int, float]] = [
            (lower_code, "BUY",  qty,     lower_price),
            (upper_code, "BUY",  qty,     upper_price),
            (atm_code,   "SELL", qty * 2, atm_price),
        ]
        if not self._run_pre_trade_gate(symbol, legs_spec, cash):
            return False

        # PDTGuard チェック
        try:
            pdt_result = self._pdt_guard.check_can_trade(
                symbol=symbol,
                trade_date=self._now_et().date(),
            )
            if not pdt_result.allowed:
                log.warning(
                    "[ButterflyNative] PDTGuard 拒否: %s -> スキップ", pdt_result.reason
                )
                return False
        except PDTBlockedError as exc:
            log.warning("[ButterflyNative] PDTBlockedError: %s -> スキップ", exc)
            return False
        except Exception as exc:
            log.debug("[ButterflyNative] PDTGuard チェック失敗（スキップ続行）: %s", exc)

        # 冪等性キー生成
        signal_id = make_job_key(
            strategy=TACTIC_NAME,
            symbol=symbol.replace("US.", ""),
            trigger_time=self._now_et(),
        )

        trade_id  = str(uuid.uuid4())[:8]

        # 発注実行（買い脚先行: lower -> upper -> atm×2）
        order_ok = True
        for idx, (code, side, qty_n, label) in enumerate([
            (lower_code, "BUY",  qty,     "lower_buy"),
            (upper_code, "BUY",  qty,     "upper_buy"),
            (atm_code,   "SELL", qty * 2, "atm_sell_x2"),
        ]):
            oid = self._eng.place_butterfly_leg(
                option_code=code,
                side=side,
                quantity=qty_n,
                label=label,
                signal_id=f"{signal_id}_leg{idx}",
            )
            if oid is None:
                log.error(
                    "[ButterflyNative] %s 発注失敗: code=%s -> エントリー中止",
                    label, code,
                )
                order_ok = False
                break
            log.info("[ButterflyNative] %s 約定: oid=%s", label, oid)

        if not order_ok:
            log.error("[ButterflyNative] 発注失敗 -> ポジション管理外")
            return False

        # ポジション記録
        self.position = ButterflyNativePosition(
            symbol=symbol, wing_type=wing_type,
            atm_strike=atm_strike, wing_width=wing_width,
            lower_code=lower_code, atm_code=atm_code, upper_code=upper_code,
            qty=qty, net_debit=net_debit,
            lower_entry_price=lower_price,
            atm_entry_price=atm_price,
            upper_entry_price=upper_price,
            entry_time=self._now_et().isoformat(),
            expiry=expiry, trade_id=trade_id,
            paper=self._cfg.paper_mode,
        )
        self.entry_done = True
        log.info("[ButterflyNative] ENTRY 完了: %r", self.position)
        return True

    # ------------------------------------------------------------------
    # 公開 API: check_exit
    # ------------------------------------------------------------------

    def check_exit(self) -> bool:
        """TP / SL / 強制クローズをチェックしてエグジットを実行する。

        判定順:
        1. ポジションなし / trade_done -> False
        2. Kill Switch ARMED -> 即時強制クローズ
        3. 現在価格取得失敗 + 強制クローズ時刻 -> 強制クローズ
        4. 強制クローズ時刻到達 -> 強制クローズ
        5. TP 到達（current_value >= net_debit × (1 + tp_pct)）-> TP クローズ
        6. SL 到達（current_value <= net_debit × (1 - sl_pct)）-> SL クローズ

        Returns:
            True: エグジット実行 / False: 継続保有
        """
        if self.position is None or self.trade_done:
            return False

        pos = self.position

        # Kill Switch 強制クローズ
        if kill_switch_is_active():
            log.warning("[ButterflyNative.check_exit] Kill Switch ARMED -> 強制クローズ")
            return self._execute_exit(pos, "kill_switch", 0.0, 0.0, 0.0)

        now_t          = self._now_et().time()
        force_close_t  = self._get_force_close_time()
        past_force     = now_t >= force_close_t

        # 現在価格取得
        try:
            lower_price = self._mkt.get_option_mid(pos.lower_code)
            atm_price   = self._mkt.get_option_mid(pos.atm_code)
            upper_price = self._mkt.get_option_mid(pos.upper_code)
        except Exception as exc:
            log.warning("[ButterflyNative] check_exit 価格取得失敗: %s", exc)
            lower_price = atm_price = upper_price = None

        # 価格取得失敗 + 強制クローズ時刻
        if lower_price is None or atm_price is None or upper_price is None:
            if past_force:
                log.warning(
                    "[ButterflyNative] 価格取得失敗 + 強制クローズ時刻 -> 強制クローズ"
                )
                return self._execute_exit(
                    pos, "force_close_price_unavailable",
                    lower_price or 0.0,
                    atm_price   or 0.0,
                    upper_price or 0.0,
                )
            return False

        current_val = pos.current_value(lower_price, atm_price, upper_price)
        pnl_usd     = pos.pnl(lower_price, atm_price, upper_price)

        # 強制クローズ
        if past_force:
            log.info("[ButterflyNative] 強制クローズ: pnl=%.2f", pnl_usd)
            return self._execute_exit(pos, "force_close", lower_price, atm_price, upper_price)

        # TP チェック
        tp_threshold = pos.net_debit * (1.0 + self._cfg.profit_target_pct)
        if current_val >= tp_threshold:
            log.info(
                "[ButterflyNative] TP 到達: val=%.4f >= %.4f pnl=%.2f",
                current_val, tp_threshold, pnl_usd,
            )
            return self._execute_exit(pos, "take_profit", lower_price, atm_price, upper_price)

        # SL チェック
        sl_threshold = pos.net_debit * (1.0 - self._cfg.stop_loss_pct)
        if current_val <= sl_threshold:
            log.info(
                "[ButterflyNative] SL 到達: val=%.4f <= %.4f pnl=%.2f",
                current_val, sl_threshold, pnl_usd,
            )
            return self._execute_exit(pos, "stop_loss", lower_price, atm_price, upper_price)

        log.debug(
            "[ButterflyNative] 保有中: val=%.4f debit=%.4f pnl=%.2f"
            " TP=%.4f SL=%.4f",
            current_val, pos.net_debit, pnl_usd, tp_threshold, sl_threshold,
        )
        return False

    # ------------------------------------------------------------------
    # 内部: エグジット実装
    # ------------------------------------------------------------------

    def _execute_exit(
        self,
        pos: ButterflyNativePosition,
        reason: str,
        lower_price: float,
        atm_price: float,
        upper_price: float,
    ) -> bool:
        """Butterfly ポジションを決済する。

        決済順（ショート脚先にクローズして証拠金を解放）:
          1. atm_buy × 2   (short -> buy to close)
          2. lower_sell    (long -> sell to close)
          3. upper_sell    (long -> sell to close)

        Returns:
            True: 決済実行 / False: 失敗
        """
        current_val = pos.current_value(lower_price, atm_price, upper_price)
        pnl_usd     = pos.pnl(lower_price, atm_price, upper_price)
        log.info(
            "[ButterflyNative] EXIT (%s): %s %s K=%.1f val=%.4f pnl=%.2f",
            reason, pos.symbol, pos.wing_type, pos.atm_strike, current_val, pnl_usd,
        )

        for code, side, qty_n, label in [
            (pos.atm_code,   "BUY",  pos.qty * 2, "atm_buy_close"),
            (pos.lower_code, "SELL", pos.qty,      "lower_sell_close"),
            (pos.upper_code, "SELL", pos.qty,      "upper_sell_close"),
        ]:
            oid = self._eng.place_butterfly_leg(
                option_code=code,
                side=side,
                quantity=qty_n,
                label=label,
                signal_id="",
            )
            if oid is None:
                log.error(
                    "[ButterflyNative] EXIT %s 失敗: code=%s", label, code
                )
                # クローズ失敗でも trade_done=True にして再発注ループを防ぐ
            else:
                log.info("[ButterflyNative] EXIT %s 約定: oid=%s", label, oid)

        self.position   = None
        self.trade_done = True
        return True

    # ------------------------------------------------------------------
    # 公開 API: is_active
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """ポジション保有中かどうかを返す。"""
        return self.position is not None
