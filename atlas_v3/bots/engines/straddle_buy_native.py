"""atlas_v3/bots/engines/straddle_buy_native.py — Straddle Buy Engine (subprocess 依存ゼロ移植)

spy_bot.StraddleBuyEngine の public interface を TacticBase 継承の純 atlas_v3 実装として完全再現。
spy_bot.py への書き換えはゼロ。

移植元: spy_bot.py::StraddleBuyEngine (L10154-L10757)
公開 interface:
    reset_daily()
    premarket_check() -> bool
    execute_entry() -> Optional[StraddleBuyNativePosition]
    check_exit() -> Optional[dict]
    check_hedge() -> bool

依存置換:
    MarketData      -> MarketDataProtocol (Protocol / duck-typing)
    TradeEngine     -> TradeEngineProtocol (Protocol / duck-typing)
    futu SDK 直接参照なし
    chainguard_wrapper / symbol_aware_price 経由で価格取得

設計規律:
    - spy_bot.py / chronos_bot.py / common/* への書き換え禁止
    - asyncio 禁止（sync_only 前提）
    - CC <= 20 per method
    - TacticBase ABC 継承必須
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import requests

from atlas_v3.ops.symbol_aware_price import (
    get_current_price_with_fallback,
    normalize_symbol,
)
from atlas_v3.strategies.base import TacticBase, TacticType
from atlas_v3.core.env_observer import MarketEnvironment
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# 定数（spy_bot.py L9975-L9991 と同値・独立コピー）
# ---------------------------------------------------------------------------

STRADDLE_BUY_TP_PCT: float             = 0.40   # 利確: +40%
STRADDLE_BUY_SL_PCT: float             = -0.25  # 損切: -25%
STRADDLE_BUY_VIX_SPIKE_PCT: float      = 10.0   # VIX急騰(>10%/h)+含み益で即利確
STRADDLE_BUY_MAX_RISK_PCT: float       = 0.02   # 口座の2%を最大リスク
STRADDLE_BUY_MAX_QTY: int              = 3      # 最大3契約
STRADDLE_BUY_SMALL_ACCOUNT_USD: float  = 15_000.0  # この金額以下は1契約まで
STRADDLE_BUY_MAX_HEDGE_COUNT: int      = 5      # 1日最大ヘッジ回数
STRADDLE_BUY_EXIT_H: int               = 15     # タイムストップ 15:50 ET
STRADDLE_BUY_EXIT_M: int               = 50
STRADDLE_BUY_MIN_ENV_SCORE: float      = 60.0   # 環境スコア最低ライン
STRADDLE_BUY_STRIKE_DEV_MAX: float     = 0.15   # strike 整合性 NG 閾値

# デルタヘッジバンド（VIXで動的算出。VIX高→バンド狭く）
STRADDLE_BUY_HEDGE_BAND_LOW: float     = 0.25   # VIX < 15
STRADDLE_BUY_HEDGE_BAND_MID: float     = 0.20   # VIX 15-20
STRADDLE_BUY_HEDGE_BAND_HIGH: float    = 0.15   # VIX 20-25
STRADDLE_BUY_HEDGE_BAND_CRISIS: float  = 0.10   # VIX > 25

# Early-close 半日取引日
EARLY_CLOSE_EXIT_H: int = 12
EARLY_CLOSE_EXIT_M: int = 50
LAST_ENTRY_H: int = 15
LAST_ENTRY_M: int = 30

_EARLY_CLOSE_DAYS: dict[str, tuple[int, int]] = {
    "2026-11-27": (13, 0),
    "2026-12-24": (13, 0),
    "2027-07-03": (13, 0),
    "2027-11-26": (13, 0),
    "2027-12-24": (13, 0),
}

_FALLBACK_PRICE_DEFAULTS: dict[str, float] = {
    "SPY": 560.0, "QQQ": 480.0, "IWM": 200.0,
    "TSLA": 250.0, "NVDA": 900.0, "AAPL": 200.0,
    "MSFT": 420.0, "AMZN": 200.0, "META": 600.0,
    "GOOGL": 170.0,
}

# ---------------------------------------------------------------------------
# ヘルパー
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
        log.warning("[StraddleBuyNative] ET 時刻取得失敗 → エントリー禁止（安全側）")
        return True


def _get_fallback_price(ticker: str) -> float:
    return _FALLBACK_PRICE_DEFAULTS.get(ticker, 300.0)


# ---------------------------------------------------------------------------
# StraddleBuyNativePosition（spy_bot.StraddleBuyPosition の独立コピー）
# ---------------------------------------------------------------------------


@dataclass
class StraddleBuyNativePosition:
    """ATM Long Straddle (CALL + PUT) のポジション（spy_bot.StraddleBuyPosition 互換）。

    Attributes:
        call_code:   CALL オプションコード
        put_code:    PUT オプションコード
        qty:         契約数
        call_price:  エントリー時 CALL mid 価格
        put_price:   エントリー時 PUT mid 価格
        strike:      ATM strike 値
        entry_time:  エントリー時刻 ISO 文字列
        hedge_count: ヘッジ発動回数（最大 STRADDLE_BUY_MAX_HEDGE_COUNT）
        hedge_legs:  {option_code: qty} ヘッジ追加 leg 管理
    """

    call_code: str
    put_code: str
    qty: int
    call_price: float
    put_price: float
    strike: float
    entry_time: str = field(
        default_factory=lambda: datetime.datetime.now(ET).isoformat()
    )
    hedge_count: int = 0
    hedge_legs: dict = field(default_factory=dict)

    @property
    def entry_price_per_unit(self) -> float:
        """1 ユニット（CALL + PUT）のエントリー価格合計。"""
        return self.call_price + self.put_price

    @property
    def entry_cost(self) -> float:
        """全契約分のエントリーコスト（1 契約 = 100 株）。"""
        return self.entry_price_per_unit * self.qty * 100


# ---------------------------------------------------------------------------
# Protocols — 依存先の型契約（duck-typing・futu 非依存）
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataProtocol(Protocol):
    """StraddleBuyNativeEngine が利用する MarketData 最小 interface。"""

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
    """StraddleBuyNativeEngine が利用する TradeEngine 最小 interface。"""

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


# ---------------------------------------------------------------------------
# StraddleBuyNativeEngine — TacticBase 継承
# ---------------------------------------------------------------------------


class StraddleBuyNativeEngine(TacticBase):
    """spy_bot.StraddleBuyEngine の atlas_v3 native 移植。

    subprocess 依存ゼロ・futu SDK 直接インポートなし。
    MarketDataProtocol / TradeEngineProtocol で依存を抽象化。

    設計:
        - Low-IV 環境（IVR < P25）に ATM 0DTE CALL + PUT 同時買い
        - VIX 急騰（+10%/h 以上）で即利確
        - シンセティックデルタヘッジ: ポートフォリオデルタ超過時にオプション追加買い

    Public interface（spy_bot.StraddleBuyEngine と完全互換）:
        reset_daily()
        premarket_check() -> bool
        execute_entry() -> Optional[StraddleBuyNativePosition]
        check_exit() -> Optional[dict]
        check_hedge() -> bool

    TacticBase 追加 interface:
        tactic_type  -> "enter_exit"
        tactic_name  -> "straddle_buy_native"
        preflight(env) -> bool

    Flags（strategy_selector 連携）:
        supports_1dte = False   （シータ崩壊で翌日保有は逆効果）
        allow_expiry_pass_through = False （買い戦術: 満期放置 NG）
    """

    supports_1dte: bool = False
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
        self.today_vix: Optional[float] = None
        self.position: Optional[StraddleBuyNativePosition] = None
        self.trade_done: bool = False
        self.entry_done: bool = False
        self._assessment: Optional[dict] = None
        self._kelly_fraction: Optional[float] = None
        self._vix_prev: Optional[float] = None
        self._vix_check_ts: Optional[datetime.datetime] = None

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "straddle_buy_native"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。Kill Switch ARMED / VIX > 40 なら False。"""
        if env is None:
            log.warning("[StraddleBuyNative.preflight] env=None → False")
            return False
        if kill_switch_is_active():
            log.warning("[StraddleBuyNative.preflight] Kill Switch ARMED → False")
            return False
        if env.vix >= 40.0:
            log.info(
                "[StraddleBuyNative.preflight] VIX=%.2f >= 40.0 (高ボラ: straddle 高コスト) → False",
                env.vix,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Phase 0: 日次リセット
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """EOD または日付変わり時に日次状態をリセットする。"""
        self.today_vix = None
        self.position = None
        self.trade_done = False
        self.entry_done = False
        self._assessment = None
        self._kelly_fraction = None
        self._vix_prev = None
        self._vix_check_ts = None

    # ------------------------------------------------------------------
    # Phase 1: プレマーケット環境チェック
    # ------------------------------------------------------------------

    def premarket_check(self) -> bool:
        """IVR (VIX が60日P25以下) + env_score でエントリー可否を判断する。

        Returns:
            True: Low-IV 環境・エントリー可
            False: 高IVまたは環境スコア不足
        """
        if self.dry_test:
            self.today_vix = 16.0
            log.info("[StraddleBuyNative][DRY-TEST] premarket_check OK: vix=16.0")
            return True

        if self.mkt is None:
            log.warning("[StraddleBuyNative] premarket_check: mkt=None → False")
            return False

        if kill_switch_is_active():
            log.warning("[StraddleBuyNative] premarket_check: Kill Switch ARMED → False")
            return False

        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("[StraddleBuyNative] premarket_check: VIX 取得失敗 → False")
            return False

        self.today_vix = vix
        self._vix_prev = vix
        self._vix_check_ts = datetime.datetime.now(ET)

        # IVR 条件: VIX が60日履歴の P25 以下
        if not self.paper:
            vix_history = self.mkt.get_vix_history(days=60)
            if len(vix_history) >= 20:
                sorted_h = sorted(vix_history)
                p25 = sorted_h[int(0.25 * (len(sorted_h) - 1))]
                ivr_ok = vix <= p25
            else:
                ivr_ok = vix < 18.0  # フォールバック
            if not ivr_ok:
                log.info(
                    "[StraddleBuyNative] Skip: VIX=%.2f > P25 (IVR高すぎ → コスト高)", vix
                )
                return False
        else:
            log.info("[StraddleBuyNative][PAPER] VIX=%.2f → IVR 条件バイパス", vix)

        # 環境スコアチェック
        if self._assessment:
            score = self._assessment.get("score", 100.0)
            if score < STRADDLE_BUY_MIN_ENV_SCORE:
                log.info(
                    "[StraddleBuyNative] Skip: env_score=%.1f < %.1f",
                    score, STRADDLE_BUY_MIN_ENV_SCORE,
                )
                return False

        log.info("[StraddleBuyNative] premarket_check OK: VIX=%.2f (P25以下)", vix)
        return True

    # ------------------------------------------------------------------
    # Phase 2: エントリー実行
    # ------------------------------------------------------------------

    def execute_entry(self) -> Optional[StraddleBuyNativePosition]:
        """ATM 0DTE CALL + PUT を同時買い発注する。

        Returns:
            StraddleBuyNativePosition — 発注成功
            None — 条件未達・発注失敗
        """
        if _is_past_entry_cutoff(dry_test=self.dry_test):
            log.info(
                "[StraddleBuyNative] execute_entry: %d:%02d ET 以降 → エントリー中止",
                LAST_ENTRY_H, LAST_ENTRY_M,
            )
            return None

        if kill_switch_is_active():
            log.warning("[StraddleBuyNative] execute_entry: Kill Switch ARMED → 中止")
            return None

        ticker = self._get_ticker()
        underlying_price = self._get_underlying_price()
        if not underlying_price or underlying_price <= 0:
            log.error("[StraddleBuyNative] execute_entry: %s 価格取得失敗", ticker)
            return None

        atm_strike = round(underlying_price)
        today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        log.info(
            "[StraddleBuyNative] Entry: %s=%.2f ATM=%d",
            ticker, underlying_price, atm_strike,
        )

        if self.dry_test:
            return self._dry_entry(ticker, atm_strike)

        # オプションチェーン取得
        call_chain = self.mkt.get_option_chain_with_greeks(
            today_str, "CALL", center_strike=float(atm_strike)
        )
        put_chain = self.mkt.get_option_chain_with_greeks(
            today_str, "PUT", center_strike=float(atm_strike)
        )
        if not call_chain or not put_chain:
            log.error("[StraddleBuyNative] オプションチェーン取得失敗")
            return None

        call_opt = (
            self.mkt.find_by_delta(call_chain, 0.50)
            or self.mkt.find_by_strike(call_chain, float(atm_strike))
        )
        put_opt = (
            self.mkt.find_by_delta(put_chain, 0.50)
            or self.mkt.find_by_strike(put_chain, float(atm_strike))
        )
        if call_opt is None or put_opt is None:
            log.error("[StraddleBuyNative] ATM オプション選択失敗")
            return None

        # strike 整合性チェック（ATM基準 ±15% 超は異常）
        for tag, opt in [("CALL", call_opt), ("PUT", put_opt)]:
            strike_val = opt.get("strike_price", 0)
            dev = abs(strike_val - float(atm_strike)) / max(float(atm_strike), 1.0)
            if dev > STRADDLE_BUY_STRIKE_DEV_MAX:
                log.error(
                    "[StraddleBuyNative] %s strike 整合性NG: %.0f vs ATM %d 乖離=%.1f%%",
                    tag, strike_val, atm_strike, dev * 100,
                )
                return None

        call_code = call_opt["code"]
        put_code = put_opt["code"]
        call_mid = self._calc_mid_price(call_opt)
        put_mid = self._calc_mid_price(put_opt)

        if not call_mid or not put_mid or call_mid <= 0 or put_mid <= 0:
            log.error(
                "[StraddleBuyNative] 価格取得失敗 CALL=%.2f PUT=%.2f",
                call_mid or 0, put_mid or 0,
            )
            return None

        cash = self._get_cash()
        qty = self._calc_qty(cash, call_mid + put_mid)

        vix = self.today_vix or 20.0
        use_limit = vix <= 30.0

        _ts = datetime.datetime.now(ET).strftime("%Y%m%d%H%M")
        signal_id_base = f"straddlebuy_{ticker}_{_ts}"

        call_oid = self._place_buy(
            call_code, qty, "STRADDLE_BUY_CALL",
            mid_price=call_mid if use_limit else None,
            use_limit=use_limit,
            signal_id=f"{signal_id_base}_call",
        )
        if call_oid is None:
            log.error("[StraddleBuyNative] CALL 発注失敗")
            return None

        put_oid = self._place_buy(
            put_code, qty, "STRADDLE_BUY_PUT",
            mid_price=put_mid if use_limit else None,
            use_limit=use_limit,
            signal_id=f"{signal_id_base}_put",
        )
        if put_oid is None:
            log.error("[StraddleBuyNative] PUT 発注失敗（CALL 約定済）")
            return None

        log.info(
            "[StraddleBuyNative] 発注OK: CALL=%s PUT=%s x%d C$%.2f+P$%.2f",
            call_oid, put_oid, qty, call_mid, put_mid,
        )
        pos = StraddleBuyNativePosition(
            call_code=call_code,
            put_code=put_code,
            qty=qty,
            call_price=call_mid,
            put_price=put_mid,
            strike=float(atm_strike),
        )
        self.entry_done = True
        return pos

    # ------------------------------------------------------------------
    # Phase 3: エグジット監視（毎 tick）
    # ------------------------------------------------------------------

    def check_exit(self) -> Optional[dict]:
        """TP / SL / タイムストップ / VIX急騰を毎 tick チェックする。

        Returns:
            決済完了時: {"reason": str, "exit_value": float, "pnl_usd": float}
            継続中: None
        """
        if self.position is None:
            return None

        pos = self.position
        now_et_time = datetime.datetime.now(ET).time()

        if _is_early_close_today():
            time_stop = datetime.time(EARLY_CLOSE_EXIT_H, EARLY_CLOSE_EXIT_M)
        else:
            time_stop = datetime.time(STRADDLE_BUY_EXIT_H, STRADDLE_BUY_EXIT_M)

        if not self.dry_test and now_et_time >= time_stop:
            cv = self._get_straddle_value(pos) or pos.entry_price_per_unit * 0.3
            log.info("[StraddleBuyNative] タイムストップ: %s ET", time_stop)
            return self._close_position(pos, cv, "time_stop")

        cv = self._get_straddle_value(pos)
        if not cv or cv <= 0:
            return None

        pnl_pct = (cv - pos.entry_price_per_unit) / pos.entry_price_per_unit

        # VIX 急騰時即利確
        if not self.dry_test and self._vix_prev and self._vix_check_ts:
            vix_now = self.mkt.get_vix() if self.mkt else None
            if vix_now:
                elapsed_h = (
                    (datetime.datetime.now(ET) - self._vix_check_ts).total_seconds() / 3600.0
                )
                if elapsed_h > 0:
                    vix_chg = (vix_now - self._vix_prev) / self._vix_prev * 100.0 / elapsed_h
                    if vix_chg > STRADDLE_BUY_VIX_SPIKE_PCT and pnl_pct > 0:
                        log.warning(
                            "[StraddleBuyNative] VIX 急騰(%.1f%%/h) → 即利確", vix_chg
                        )
                        return self._close_position(pos, cv, "vix_spike_profit_take")
                if elapsed_h >= 1.0:
                    self._vix_prev = vix_now
                    self._vix_check_ts = datetime.datetime.now(ET)

        if pnl_pct >= STRADDLE_BUY_TP_PCT:
            return self._close_position(pos, cv, "profit_target")
        if pnl_pct <= STRADDLE_BUY_SL_PCT:
            return self._close_position(pos, cv, "stop_loss")

        return None

    # ------------------------------------------------------------------
    # Phase 4: シンセティックデルタヘッジ（毎 tick）
    # ------------------------------------------------------------------

    def check_hedge(self) -> bool:
        """デルタが ±HEDGE_BAND を超えたら ATM オプション追加発注でデルタ調整する。

        Returns:
            True: ヘッジ発動（または dry_test 模擬発動）
            False: 発動なし
        """
        if self.position is None:
            return False

        pos = self.position
        if pos.hedge_count >= STRADDLE_BUY_MAX_HEDGE_COUNT:
            return False

        vix = self.today_vix or 20.0
        if not self.dry_test and self.mkt:
            vix = self.mkt.get_vix() or vix

        hedge_band = self._calc_hedge_band(vix)
        portfolio_delta = self._get_portfolio_delta(pos)
        if portfolio_delta is None:
            return False
        if abs(portfolio_delta) <= hedge_band:
            return False

        direction = "PUT" if portfolio_delta > 0 else "CALL"
        log.info(
            "[StraddleBuyNative][HEDGE] delta=%+.3f band=%.2f → 追加%s (%d/%d)",
            portfolio_delta, hedge_band, direction,
            pos.hedge_count + 1, STRADDLE_BUY_MAX_HEDGE_COUNT,
        )

        if self.dry_test:
            pos.hedge_count += 1
            log.info(
                "[StraddleBuyNative][DRY-TEST][HEDGE] 追加%s delta=%+.3f",
                direction, portfolio_delta,
            )
            return True

        today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        spy_price = self._get_underlying_price() or pos.strike
        chain = self.mkt.get_option_chain_with_greeks(
            today_str, direction, center_strike=float(spy_price)
        )
        if not chain:
            log.warning("[StraddleBuyNative][HEDGE] %s チェーン取得失敗", direction)
            return False

        hedge_opt = (
            self.mkt.find_by_delta(chain, 0.50)
            or self.mkt.find_by_strike(chain, spy_price)
        )
        if hedge_opt is None:
            log.warning("[StraddleBuyNative][HEDGE] ヘッジオプション選択失敗")
            return False

        # strike 整合性チェック
        hs = hedge_opt.get("strike_price", 0)
        if spy_price > 0 and abs(hs - spy_price) / spy_price > STRADDLE_BUY_STRIKE_DEV_MAX:
            log.error(
                "[StraddleBuyNative][HEDGE] strike 整合性NG: %.0f vs underlying=%.2f",
                hs, spy_price,
            )
            return False

        h_code = hedge_opt["code"]
        h_mid = self._calc_mid_price(hedge_opt)
        if not h_mid or h_mid <= 0:
            log.warning("[StraddleBuyNative][HEDGE] ヘッジ価格取得失敗")
            return False

        oid = self._place_buy(
            h_code, 1, f"STRADDLE_BUY_HEDGE_{direction}",
            mid_price=h_mid, use_limit=True,
            signal_id=None,
        )
        if oid is None:
            log.warning("[StraddleBuyNative][HEDGE] 発注失敗: %s", h_code)
            return False

        pos.hedge_count += 1
        pos.hedge_legs[h_code] = pos.hedge_legs.get(h_code, 0) + 1
        log.info(
            "[StraddleBuyNative][HEDGE] 完了: %s %s x1 @ $%.2f 回数=%d",
            direction, h_code, h_mid, pos.hedge_count,
        )
        return True

    # ------------------------------------------------------------------
    # static: should_trade_today
    # ------------------------------------------------------------------

    @staticmethod
    def should_trade_today(
        vix: Optional[float],
        assessment: Optional[dict] = None,
        paper: bool = False,
    ) -> bool:
        """Low-IV ストラドルエントリーが適切な環境かを判定する（spy_bot 互換）。

        条件: VIX < 25 (IVが安い帯) + env_score >= MIN
        paper=True 時は VIX 上限をバイパスして全環境で検証データを収集する。
        """
        if vix is None:
            return False
        if not paper and vix >= 25.0:
            return False
        if assessment:
            if assessment.get("score", 100.0) < STRADDLE_BUY_MIN_ENV_SCORE:
                return False
        return True

    # ------------------------------------------------------------------
    # 内部ヘルパー: ポジション決済
    # ------------------------------------------------------------------

    def _close_position(
        self,
        pos: StraddleBuyNativePosition,
        exit_value: float,
        reason: str,
    ) -> dict:
        pnl_usd = (exit_value - pos.entry_price_per_unit) * pos.qty * 100
        pnl_pct = (
            (exit_value - pos.entry_price_per_unit) / pos.entry_price_per_unit
            if pos.entry_price_per_unit
            else 0.0
        )
        log.info(
            "[StraddleBuyNative] 決済(%s): %d枚 @ $%.2f P&L=$%+.2f (%+.1f%%)",
            reason, pos.qty, exit_value, pnl_usd, pnl_pct * 100,
        )

        if self.eng is not None and not self.dry_test:
            for code, label in [
                (pos.call_code, "straddle_call_close"),
                (pos.put_code, "straddle_put_close"),
            ]:
                try:
                    oid = self.eng.place_sell(code, pos.qty, label)
                    if oid is None:
                        log.error("[StraddleBuyNative] %s 決済失敗", label)
                except Exception as exc:
                    log.error("[StraddleBuyNative] %s 決済例外: %s", label, exc)

            # ヘッジ leg 決済
            for h_code, h_qty in pos.hedge_legs.items():
                if h_qty <= 0:
                    continue
                try:
                    oid = self.eng.place_sell(h_code, abs(h_qty), "straddle_hedge_close")
                    if oid is None:
                        log.error("[StraddleBuyNative] ヘッジ leg 決済失敗: %s", h_code)
                except Exception as exc:
                    log.error("[StraddleBuyNative] ヘッジ leg 決済例外 %s: %s", h_code, exc)

        self.position = None
        self.trade_done = True
        return {"reason": reason, "exit_value": exit_value, "pnl_usd": pnl_usd}

    # ------------------------------------------------------------------
    # 内部ヘルパー: 価格取得
    # ------------------------------------------------------------------

    def _get_ticker(self) -> str:
        code = "US.SPY"
        if self.mkt is not None:
            try:
                code = self.mkt.underlying_code
            except Exception:
                pass
        return normalize_symbol(code)

    def _get_underlying_price(self) -> Optional[float]:
        """mkt → symbol_aware_price → Finnhub fallback の順で現在価格を取得。"""
        ticker = self._get_ticker()
        underlying_code = "US.SPY" if self.mkt is None else self.mkt.underlying_code

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
                log.debug("[StraddleBuyNative] symbol_aware_price: %s", exc)

        return self._fetch_price_finnhub(ticker)

    def _fetch_price_dry(self, ticker: str) -> float:
        """dry_test 用: Finnhub → フォールバック。"""
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
            log.debug("[StraddleBuyNative] Finnhub price: %s", exc)
            return None

    def _get_straddle_value(
        self, pos: StraddleBuyNativePosition
    ) -> Optional[float]:
        """ストラドルの現在価値（CALL + PUT 合計・1 枚あたり）を取得する。"""
        if self.dry_test:
            now_et = datetime.datetime.now(ET)
            session_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            session_end = now_et.replace(hour=15, minute=30, second=0, microsecond=0)
            total_secs = (session_end - session_start).total_seconds()
            elapsed = max(0.0, (now_et - session_start).total_seconds())
            decay = min(elapsed / total_secs, 1.0) if total_secs > 0 else 0.5
            entry_unit = pos.entry_price_per_unit
            return round(max(entry_unit * (1.3 - decay), entry_unit * 0.1), 4)

        if self.mkt is None:
            return None

        try:
            call_p = self.mkt.get_cached_option_price(pos.call_code, max_age_sec=15.0)
            put_p = self.mkt.get_cached_option_price(pos.put_code, max_age_sec=15.0)
            if call_p and put_p and call_p > 0 and put_p > 0:
                return call_p + put_p
        except Exception:
            pass

        return None

    def _get_portfolio_delta(
        self, pos: StraddleBuyNativePosition
    ) -> Optional[float]:
        """ポートフォリオデルタを取得する。

        dry_test 時は 0.22（PUT 方向ヘッジを発動させる値）を返す。
        """
        if self.dry_test:
            return 0.22

        # mkt が get_option_greeks を持っていれば使用する（duck-typing）
        if self.mkt is not None:
            try:
                snap_fn = getattr(self.mkt, "get_option_greeks", None)
                if snap_fn:
                    total_delta = 0.0
                    for code in [pos.call_code, pos.put_code]:
                        g = snap_fn(code)
                        if g and "delta" in g:
                            total_delta += float(g["delta"]) * pos.qty
                    for h_code, h_qty in pos.hedge_legs.items():
                        g = snap_fn(h_code)
                        if g and "delta" in g:
                            total_delta += float(g["delta"]) * h_qty
                    return total_delta
            except Exception as exc:
                log.debug("[StraddleBuyNative] _get_portfolio_delta: %s", exc)

        return None

    # ------------------------------------------------------------------
    # 内部ヘルパー: サイズ計算
    # ------------------------------------------------------------------

    def _calc_qty(self, cash: float, straddle_cost: float) -> int:
        """Kelly / max_risk で契約数を算出する。"""
        risk_pct = (
            self._kelly_fraction
            if (self._kelly_fraction and self._kelly_fraction > 0)
            else STRADDLE_BUY_MAX_RISK_PCT
        )
        max_loss = straddle_cost * 100
        if max_loss <= 0:
            return 1
        qty = max(1, int(cash * risk_pct / max_loss))
        qty = min(qty, STRADDLE_BUY_MAX_QTY)
        if cash < STRADDLE_BUY_SMALL_ACCOUNT_USD:
            qty = min(qty, 1)
        return qty

    def _calc_hedge_band(self, vix: float) -> float:
        """VIX 水準からヘッジバンド幅を動的算出する（VIX 高 → バンド狭く）。"""
        if vix < 15.0:
            return STRADDLE_BUY_HEDGE_BAND_LOW
        if vix < 20.0:
            return STRADDLE_BUY_HEDGE_BAND_MID
        if vix < 25.0:
            return STRADDLE_BUY_HEDGE_BAND_HIGH
        return STRADDLE_BUY_HEDGE_BAND_CRISIS

    def _get_cash(self) -> float:
        if self.eng is not None:
            try:
                cash = self.eng.get_account_cash()
                if cash and cash > 0:
                    return cash
            except Exception:
                pass
        return 10_000.0

    def _calc_mid_price(self, opt: dict) -> Optional[float]:
        bid = opt.get("bid_price", 0)
        ask = opt.get("ask_price", 0)
        if bid and ask:
            return (bid + ask) / 2
        return opt.get("last_price") or None

    # ------------------------------------------------------------------
    # 内部ヘルパー: 発注
    # ------------------------------------------------------------------

    def _place_buy(
        self,
        code: str,
        qty: int,
        label: str,
        mid_price: Optional[float],
        use_limit: bool,
        signal_id: Optional[str],
    ) -> Optional[str]:
        if self.eng is None:
            log.info("[StraddleBuyNative][DRY-RUN] BUY %s x%d", code, qty)
            return "DRY_ORDER"
        try:
            return self.eng.place_buy(
                code=code,
                qty=qty,
                label=label,
                init_price=mid_price if use_limit else None,
                use_limit=use_limit,
                signal_id=signal_id,
            )
        except Exception as exc:
            log.error("[StraddleBuyNative] place_buy 失敗: %s", exc)
            return None

    # ------------------------------------------------------------------
    # dry-test エントリーヘルパー
    # ------------------------------------------------------------------

    def _dry_entry(
        self, ticker: str, atm_strike: int
    ) -> StraddleBuyNativePosition:
        call_price = 1.80
        put_price = 1.80
        dt_str = datetime.datetime.now(ET).strftime("%y%m%d")
        virtual_call = f"US.{ticker}{dt_str}C{int(atm_strike * 1000)}"
        virtual_put = f"US.{ticker}{dt_str}P{int(atm_strike * 1000)}"
        cash = self._get_cash()
        qty = self._calc_qty(cash, call_price + put_price)
        log.info(
            "[StraddleBuyNative][DRY-TEST] %s/%s x%d C$%.2f+P$%.2f",
            virtual_call, virtual_put, qty, call_price, put_price,
        )
        self.entry_done = True
        return StraddleBuyNativePosition(
            call_code=virtual_call,
            put_code=virtual_put,
            qty=qty,
            call_price=call_price,
            put_price=put_price,
            strike=float(atm_strike),
        )

    # ------------------------------------------------------------------
    # Finnhub トークン取得
    # ------------------------------------------------------------------

    @staticmethod
    def _finnhub_token() -> str:
        import os
        return os.environ.get("FINNHUB_API_KEY", "")
