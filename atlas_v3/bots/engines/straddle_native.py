"""atlas_v3/bots/engines/straddle_native.py — StraddleNativeEngine + GammaScalpNativeEngine

spy_bot.StraddleEngine (L9498-L9709) + spy_bot.GammaScalpEngine (L9711-L9968) の
atlas_v3 native 一体移植。orb_native.py と同一方式。

GammaScalpNativeEngine は StraddleNativeEngine 参照を持つため 1 ファイルで同居する。

公開インターフェース（spy_bot と互換）:
    StraddleNativeEngine:
        reset_daily()
        should_enter_today(vix) -> bool
        execute_entry() -> Optional[StraddleNativePosition]
        close_straddle(pos, reason)
    GammaScalpNativeEngine:
        reset_daily()
        initialize_atr()
        update_price(spy_price)
        monitor_gamma_opportunity() -> Optional[str]
        execute_scalp(direction) -> bool
        check_stop_loss() -> bool
        check_and_hedge()   <- tick() の atlas_v3 向けリネーム
        tick()              <- spy_bot 互換エイリアス

依存置換:
    MarketData      -> MarketDataProtocol (Protocol / duck-typing)
    TradeEngine     -> TradeEngineProtocol (Protocol / duck-typing)
    futu TrdSide    -> 文字列 "BUY" / "SELL"（futu 非依存）
    pushover        -> common_v3 Pushover クライアント（lazy import）
    _gamma_scalp_append_pnl -> self._append_pnl() で内包化
    FUTU_AVAILABLE  -> TradeEngineProtocol.place_buy / place_sell 存在で判定

禁則:
    - spy_bot.py / chronos_bot.py への import 禁止
    - asyncio 禁止（sync_only 前提）
    - CC <= 20 規律
    - futu SDK 直接 import 禁止
"""
from __future__ import annotations

import datetime
import logging
import math
import os
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import requests

from atlas_v3.ops.symbol_aware_price import normalize_symbol
from atlas_v3.strategies.base import TacticBase, TacticType
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# 定数（spy_bot.py L9400-L9412 と同値・独立コピー）
# ---------------------------------------------------------------------------

GAMMA_SCALP_VIX_MIN: float           = 20.0
GAMMA_SCALP_ENTRY_H: int             = 9
GAMMA_SCALP_ENTRY_M: int             = 45
GAMMA_SCALP_CUTOFF_H: int            = 10
GAMMA_SCALP_CUTOFF_M: int            = 30
GAMMA_SCALP_ATR_TRIGGER: float       = 0.40
GAMMA_SCALP_MAX_PER_DAY: int         = 5
GAMMA_SCALP_STOP_LOSS_PCT: float     = 0.50
GAMMA_SCALP_FORCE_CLOSE_H: int       = 15
GAMMA_SCALP_FORCE_CLOSE_M: int       = 30
GAMMA_SCALP_MIN_INTERVAL_MIN: float  = 10.0

#: PDT: StraddleEngine は 0DTE ガンマスキャルプ目的。1DTE 化は設計ミスマッチ。
STRADDLE_SUPPORTS_1DTE: bool         = False

#: early-close 半日取引日（orb_native と同じ定義）
_EARLY_CLOSE_DAYS: dict[str, tuple[int, int]] = {
    "2026-11-27": (13, 0),
    "2026-12-24": (13, 0),
    "2027-07-03": (13, 0),
    "2027-11-26": (13, 0),
    "2027-12-24": (13, 0),
}

# PnL ファイルはプロセスごとに設定可能（デフォルトはホームディレクトリ下）
_DEFAULT_PNL_FILE = Path(os.environ.get(
    "STRADDLE_NATIVE_PNL_FILE",
    str(Path.home() / ".spxbot_data" / "straddle_native_pnl.json"),
))

# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------


def _is_past_entry_cutoff(dry_test: bool = False) -> bool:
    """15:30 ET を超えていれば True（H-T1 共通エントリーゲート）。"""
    if dry_test:
        return False
    try:
        now_et = datetime.datetime.now(ET)
        return (now_et.hour * 60 + now_et.minute) >= (15 * 60 + 30)
    except Exception:
        log.warning("[StraddleNative] ET 時刻取得失敗 → エントリー禁止（安全側）")
        return True


def _get_expiry_today() -> str:
    """今日の 0DTE expiry 文字列 (YYYY-MM-DD) を返す。"""
    return datetime.datetime.now(ET).strftime("%Y-%m-%d")


def _calc_atr14(closes: list[float]) -> Optional[float]:
    """日次終値リストから ATR(14) 近似値を返す。データ不足時は None。"""
    if len(closes) < 15:
        return None
    daily_ranges = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    recent = daily_ranges[-14:]
    return sum(recent) / len(recent)


def _fetch_closes_for_atr(symbol: str = "SPY", days: int = 20) -> list[float]:
    """Yahoo Finance からシンボルの日次終値を取得する（ATR 計算用）。"""
    try:
        end_ts   = int(_time.time())
        start_ts = end_ts - (days + 10) * 86400
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"period1": start_ts, "period2": end_ts, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        closes_raw = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        return [float(c) for c in closes_raw if c is not None]
    except Exception as exc:
        log.warning("[StraddleNative] _fetch_closes_for_atr(%s): %s", symbol, exc)
        return []


def _atomic_json_write(path: Path, data: Any) -> None:
    """アトミック JSON 書き込み（tmp → rename）。"""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Protocols — 依存先の型契約（duck-typing・futu 非依存）
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataProtocol(Protocol):
    """StraddleNativeEngine / GammaScalpNativeEngine が利用する最小 MarketData 型契約。"""

    @property
    def underlying_code(self) -> str: ...

    @underlying_code.setter
    def underlying_code(self, v: str) -> None: ...

    def get_vix(self) -> Optional[float]: ...

    def get_current_price(self, symbol: str) -> Optional[float]: ...

    def get_last_price(self, symbol: str) -> Optional[float]: ...

    def get_market_snapshot(self, codes: list[str]) -> tuple[Any, Any]: ...


@runtime_checkable
class TradeEngineProtocol(Protocol):
    """StraddleNativeEngine / GammaScalpNativeEngine が利用する最小 TradeEngine 型契約。"""

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
# StraddleNativePosition — StraddlePosition の atlas_v3 独立コピー
# ---------------------------------------------------------------------------


class StraddleNativePosition:
    """ATM CALL + PUT ロングポジション管理データクラス。

    spy_bot.StraddlePosition (L9468-L9495) と同一インターフェース。
    """

    def __init__(
        self,
        call_code: str,
        put_code: str,
        call_qty: int,
        put_qty: int,
        call_entry_price: float,
        put_entry_price: float,
        spy_price_at_entry: float,
        expiry: str,
    ) -> None:
        self.call_code          = call_code
        self.put_code           = put_code
        self.call_qty           = call_qty
        self.put_qty            = put_qty
        self.call_entry_price   = call_entry_price
        self.put_entry_price    = put_entry_price
        self.spy_price_at_entry = spy_price_at_entry
        self.expiry             = expiry
        self.entry_ts           = datetime.datetime.now(ET)
        self.total_cost         = (
            call_entry_price * call_qty + put_entry_price * put_qty
        ) * 100
        self.scalp_count: int   = 0

    @property
    def stop_loss_threshold(self) -> float:
        return self.total_cost * GAMMA_SCALP_STOP_LOSS_PCT

    def current_pnl(self, call_current: float, put_current: float) -> float:
        call_val = call_current * self.call_qty * 100
        put_val  = put_current  * self.put_qty  * 100
        return (call_val + put_val) - self.total_cost


# ---------------------------------------------------------------------------
# StraddleNativeEngine — TacticBase 継承・enter_exit タイプ
# ---------------------------------------------------------------------------


class StraddleNativeEngine(TacticBase):
    """spy_bot.StraddleEngine の atlas_v3 native 移植。

    0DTE ATM Call + Put ロングエントリー・クローズを担当する。
    GammaScalpNativeEngine はポジション管理を担当する。

    Public interface (spy_bot 互換):
        reset_daily()
        should_enter_today(vix) -> bool
        execute_entry() -> Optional[StraddleNativePosition]
        close_straddle(pos, reason)

    TacticBase ABC:
        tactic_type  -> "enter_exit"
        tactic_name  -> "straddle_native"
        preflight(env) -> bool
    """

    supports_1dte: bool = STRADDLE_SUPPORTS_1DTE

    def __init__(
        self,
        mkt: Optional[Any] = None,
        eng: Optional[Any] = None,
        paper: bool = False,
        dry_test: bool = False,
        pnl_file: Optional[Path] = None,
    ) -> None:
        self.mkt      = mkt
        self.eng      = eng
        self.paper    = paper
        self.dry_test = dry_test
        self._pnl_file = pnl_file or _DEFAULT_PNL_FILE

        self.position:   Optional[StraddleNativePosition] = None
        self.entry_done: bool  = False
        self.today_vix:  Optional[float] = None

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "straddle_native"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。Kill Switch ARMED なら False。"""
        if env is None:
            log.warning("[StraddleNative.preflight] env=None → False")
            return False
        if kill_switch_is_active():
            log.warning("[StraddleNative.preflight] Kill Switch ARMED → False")
            return False
        return True

    # ------------------------------------------------------------------
    # 日次リセット
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """EOD または日付変わり時に日次状態をリセットする。"""
        self.position   = None
        self.entry_done = False
        self.today_vix  = None

    # ------------------------------------------------------------------
    # エントリー判断
    # ------------------------------------------------------------------

    def should_enter_today(self, vix: Optional[float]) -> bool:
        """当日ストラドルエントリーを実行すべきか判定する。

        paper モードは VIX 条件をバイパス（spy_bot と同一仕様）。
        """
        if kill_switch_is_active():
            log.warning("[StraddleNative] Kill Switch ARMED → skip")
            return False
        if vix is None:
            return False
        if self.paper:
            log.info("[StraddleNative][PAPER] VIX=%.2f → VIX 条件バイパス", vix)
            return True
        return vix > GAMMA_SCALP_VIX_MIN

    # ------------------------------------------------------------------
    # エントリー実行
    # ------------------------------------------------------------------

    def execute_entry(self) -> Optional[StraddleNativePosition]:
        """ATM ストラドルをエントリーする。

        dry_test モードはリアル発注なし・StraddleNativePosition を直接生成して返す。
        """
        if _is_past_entry_cutoff(dry_test=self.dry_test):
            log.info("[StraddleNative] execute_entry: 15:30 ET 以降 → エントリー中止")
            return None

        if kill_switch_is_active():
            log.warning("[StraddleNative] execute_entry: Kill Switch ARMED → 中止")
            return None

        vix = self._get_vix()
        if not self.should_enter_today(vix):
            log.info(
                "[StraddleNative] skip: VIX=%.2f <= %.2f",
                vix or 0, GAMMA_SCALP_VIX_MIN,
            )
            return None

        self.today_vix = vix
        ticker = self._get_ticker()
        spy_price = self._get_underlying_price(ticker)
        if spy_price is None or spy_price <= 0:
            log.warning("[StraddleNative] execute_entry: %s 価格取得失敗 → スキップ", ticker)
            return None

        # Pre-Trade Gate (critical only: Deep ITM + kill_switch)
        qty, est_premium_per_leg = self._calc_straddle_qty(spy_price, vix or GAMMA_SCALP_VIX_MIN)
        # B-1/B-2 fix (2026-04-25): est_premium_per_leg/cash/margin を渡し、
        # L1 Deep ITM + L3 margin の fail-closed で誤ブロックしないようにする。
        # est_premium_per_leg は株価ベース → 1 contract = 100 株分。
        # 2 leg (call+put) で qty 倍 = est_margin。
        cash = self._get_cash() or 0.0
        opt_price_per_contract = est_premium_per_leg * 100
        est_margin_total = opt_price_per_contract * qty * 2
        self._run_pre_trade_gate(
            ticker, qty,
            option_price=opt_price_per_contract,
            est_margin=est_margin_total,
            capital_usd=cash,
        )

        # PDT チェック（0DTE straddle は day_trade = 買い当日クローズ → PDT 計上対象）
        if not self.paper and not self.dry_test:
            guard = PDTGuard(paper_mode=False, capital_usd=self._get_cash() or 0.0)
            result = guard.check_can_trade(f"US.{ticker}")
            if not result.allowed:
                log.warning("[StraddleNative] PDT blocked: %s", result.reason)
                return None

        atm_strike  = round(spy_price)
        expiry      = _get_expiry_today()
        date_str    = datetime.datetime.now(ET).strftime("%y%m%d")
        call_code   = f"US.{ticker}{date_str}C{int(atm_strike * 1000)}"
        put_code    = f"US.{ticker}{date_str}P{int(atm_strike * 1000)}"

        log.info(
            "[StraddleNative] entry plan: %s=%.2f strike=%d vix=%.2f qty=%d est_prem/leg=%.4f",
            ticker, spy_price, atm_strike, vix or 0, qty, est_premium_per_leg,
        )

        if self.dry_test:
            return self._dry_entry(
                call_code, put_code, qty, est_premium_per_leg,
                spy_price, expiry, ticker, atm_strike,
            )

        return self._live_entry(
            call_code, put_code, qty, est_premium_per_leg,
            spy_price, expiry, ticker, atm_strike,
        )

    # ------------------------------------------------------------------
    # クローズ
    # ------------------------------------------------------------------

    def close_straddle(self, pos: StraddleNativePosition, reason: str) -> None:
        """ストラドルポジションをクローズする。"""
        if pos is None:
            return

        spy_price = self._get_underlying_price(self._get_ticker()) or 0.0

        if self.dry_test:
            log.info("[StraddleNative][DRY-TEST] straddle closed: reason=%s", reason)
            self._append_pnl({
                "event": "straddle_exit", "reason": reason,
                "spy_at_exit": spy_price,
                "scalp_count": pos.scalp_count,
                "dry_test": True,
            })
            self.position = None
            return

        if self.eng is None:
            log.warning("[StraddleNative] eng=None → クローズスキップ")
            return

        for code, qty in [(pos.call_code, pos.call_qty), (pos.put_code, pos.put_qty)]:
            if qty > 0:
                try:
                    self.eng.place_sell(code, qty, f"straddle_close_{reason}")
                except Exception as exc:
                    log.error("[StraddleNative] close_straddle place_sell: %s", exc)

        log.info("[StraddleNative] straddle closed: reason=%s spy=%.2f", reason, spy_price)
        self._append_pnl({
            "event": "straddle_exit", "reason": reason,
            "spy_at_exit": spy_price,
            "scalp_count": pos.scalp_count,
        })
        self._notify(
            "[StraddleNative] ストラドルクローズ",
            f"reason={reason} spy={spy_price:.2f} scalp_count={pos.scalp_count}",
        )
        self.position = None

    # ------------------------------------------------------------------
    # 内部ヘルパー: エントリー
    # ------------------------------------------------------------------

    def _dry_entry(
        self,
        call_code: str,
        put_code: str,
        qty: int,
        est_premium_per_leg: float,
        spy_price: float,
        expiry: str,
        ticker: str,
        atm_strike: int,
    ) -> StraddleNativePosition:
        call_price = max(est_premium_per_leg, 0.01)
        put_price  = max(est_premium_per_leg, 0.01)
        pos = StraddleNativePosition(
            call_code=call_code, put_code=put_code,
            call_qty=qty, put_qty=qty,
            call_entry_price=call_price, put_entry_price=put_price,
            spy_price_at_entry=spy_price, expiry=expiry,
        )
        self.position   = pos
        self.entry_done = True
        log.info(
            "[StraddleNative][DRY-TEST] straddle entered: CALL=%s PUT=%s qty=%d "
            "call_px=%.4f put_px=%.4f total_cost=%.0f",
            call_code, put_code, qty, call_price, put_price, pos.total_cost,
        )
        self._append_pnl({
            "event": "straddle_entry",
            "call_code": call_code, "put_code": put_code, "qty": qty,
            "call_entry_price": call_price, "put_entry_price": put_price,
            "spy_at_entry": spy_price,
            "vix": self.today_vix, "total_cost": pos.total_cost, "dry_test": True,
        })
        self._notify(
            "[StraddleNative] ストラドルエントリー(DRY-TEST)",
            f"{ticker} {atm_strike} qty={qty} コスト概算{pos.total_cost:.0f}",
        )
        return pos

    def _live_entry(
        self,
        call_code: str,
        put_code: str,
        qty: int,
        est_premium_per_leg: float,
        spy_price: float,
        expiry: str,
        ticker: str,
        atm_strike: int,
    ) -> Optional[StraddleNativePosition]:
        if self.eng is None:
            log.warning("[StraddleNative] eng=None → live entry スキップ")
            return None

        now_ts   = datetime.datetime.now(ET).strftime("%Y%m%d%H%M")
        sig_base = f"straddle_{ticker}_{now_ts}"

        call_order_id = self._safe_place_buy(
            call_code, qty, "straddle_call",
            mid_price=est_premium_per_leg, signal_id=f"{sig_base}_call",
        )
        if call_order_id is None:
            log.error("[StraddleNative] CALL 発注失敗 → ストラドルエントリー中止")
            return None

        put_order_id = self._safe_place_buy(
            put_code, qty, "straddle_put",
            mid_price=est_premium_per_leg, signal_id=f"{sig_base}_put",
        )
        if put_order_id is None:
            log.error("[StraddleNative] PUT 発注失敗 → CALL 脚のみ残留リスク")
            self._notify(
                "[StraddleNative] PUT 発注失敗",
                f"CALL={call_code} 約定済み。CALL 売却確認要。",
                priority=1,
            )
            return None

        call_fill = max(est_premium_per_leg, 0.01)
        put_fill  = max(est_premium_per_leg, 0.01)

        pos = StraddleNativePosition(
            call_code=call_code, put_code=put_code,
            call_qty=qty, put_qty=qty,
            call_entry_price=call_fill, put_entry_price=put_fill,
            spy_price_at_entry=spy_price, expiry=expiry,
        )
        self.position   = pos
        self.entry_done = True
        log.info(
            "[StraddleNative] straddle entered: CALL=%s@%.2f PUT=%s@%.2f qty=%d",
            call_code, call_fill, put_code, put_fill, qty,
        )
        self._append_pnl({
            "event": "straddle_entry",
            "call_code": call_code, "put_code": put_code, "qty": qty,
            "call_entry_price": call_fill, "put_entry_price": put_fill,
            "spy_at_entry": spy_price,
            "vix": self.today_vix, "total_cost": pos.total_cost,
        })
        self._notify(
            "[StraddleNative] ストラドルエントリー",
            f"{ticker} {atm_strike} qty={qty} コスト{pos.total_cost:.0f}",
        )
        return pos

    # ------------------------------------------------------------------
    # 内部ヘルパー: 価格 / サイズ
    # ------------------------------------------------------------------

    def _get_ticker(self) -> str:
        if self.mkt is not None:
            try:
                return normalize_symbol(self.mkt.underlying_code)
            except Exception:
                pass
        return "SPY"

    def _get_vix(self) -> Optional[float]:
        if self.dry_test:
            return 22.0
        if self.mkt is not None:
            try:
                return self.mkt.get_vix()
            except Exception:
                pass
        return None

    def _get_underlying_price(self, ticker: str) -> Optional[float]:
        if self.dry_test:
            return self._fetch_price_finnhub(ticker) or 560.0
        if self.mkt is not None:
            try:
                p = self.mkt.get_last_price(ticker)
                if p and p > 0:
                    return float(p)
            except Exception:
                pass
        return self._fetch_price_finnhub(ticker)

    def _fetch_price_finnhub(self, ticker: str) -> Optional[float]:
        try:
            token = os.environ.get("FINNHUB_API_KEY", "")
            resp  = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": token},
                timeout=5,
            )
            p = float(resp.json().get("c") or 0)
            return p if p > 0 else None
        except Exception as exc:
            log.debug("[StraddleNative] Finnhub price(%s): %s", ticker, exc)
            return None

    def _get_cash(self) -> float:
        if self.eng is not None:
            try:
                c = self.eng.get_account_cash()
                if c and c > 0:
                    return float(c)
            except Exception:
                pass
        return 10_000.0

    def _calc_straddle_qty(
        self, spy_price: float, vix: float
    ) -> tuple[int, float]:
        """(qty, est_premium_per_leg) を返す。spy_bot L9569-L9573 と同ロジック。"""
        T = 1.0 / 252.0
        est_premium_per_leg = spy_price * (vix / 100.0) / 16.0 * math.sqrt(T)
        est_cost_1lot = est_premium_per_leg * 2 * 100
        cash      = self._get_cash()
        # B-2/L3 整合 (2026-04-25): max_risk を 5% → 2.5% に下げ、
        # PreTradeGate L3 (max_margin_pct_per_trade=3%) を超えないようサイズ。
        # straddle は premium = margin なので両者を同一の cap で揃える必要がある。
        max_risk  = cash * 0.025
        qty = max(1, min(3, int(max_risk / est_cost_1lot))) if est_cost_1lot > 0 else 1
        return qty, est_premium_per_leg

    def _run_pre_trade_gate(
        self, ticker: str, qty: int,
        option_price: float = 0.0,
        est_margin: float = 0.0,
        capital_usd: float = 0.0,
    ) -> None:
        """critical-only PreTradeGate を通す（KILL + L1 Deep ITM + L3 margin + L4 qty）。

        Args:
            ticker: 銘柄コード (US. prefix なし)
            qty: 発注数量
            option_price: 1 contract のオプション価格 (USD) — 0 で L1 fail-closed
            est_margin: 推定必要証拠金 (USD) — 0 で L3 fail-closed
            capital_usd: 口座資本 (USD) — 0 で L3 fail-closed
        """
        try:
            from common_v3.risk.pre_trade_check import (
                OrderCtx as _Ctx,
                check_order_critical_only as _gate,
            )
            # is_long=False: straddle は ATM 2 leg ペア (call + put) で hedge 構造のため
            # L1 「Deep ITM 裸 LONG 拒否」(4/17 事故由来) の対象外。
            # spread 系として L1 skip するのが OrderCtx 設計意図 (line 124 docstring)。
            result = _gate(_Ctx(
                symbol=f"US.{ticker}", qty=qty, side="BUY", is_long=False,
                option_price=option_price,
                est_margin=est_margin,
                capital_usd=capital_usd,
            ))
            if not result.allowed:
                raise ValueError(
                    f"[StraddleNative] PreTradeGate BLOCKED: {result.reason}"
                )
        except (ImportError, ValueError):
            raise
        except Exception as exc:
            log.warning("[StraddleNative] _run_pre_trade_gate: %s", exc)

    def _safe_place_buy(
        self,
        code: str,
        qty: int,
        label: str,
        mid_price: float,
        signal_id: str,
    ) -> Optional[str]:
        try:
            return self.eng.place_buy(
                code=code,
                qty=qty,
                label=label,
                init_price=mid_price,
                use_limit=False,
                signal_id=signal_id,
            )
        except Exception as exc:
            log.error("[StraddleNative] place_buy(%s): %s", code, exc)
            return None

    # ------------------------------------------------------------------
    # PnL logging
    # ------------------------------------------------------------------

    def _append_pnl(self, record: dict) -> None:
        import json
        try:
            path = self._pnl_file
            path.parent.mkdir(parents=True, exist_ok=True)
            trades: list = []
            if path.exists():
                try:
                    trades = json.loads(path.read_text()).get("trades", [])
                except Exception:
                    trades = []
            now_et = datetime.datetime.now(ET)
            record.setdefault("date", now_et.strftime("%Y-%m-%d"))
            record.setdefault("ts", now_et.isoformat())
            record.setdefault("bot", "straddle_native")
            trades.append(record)
            _atomic_json_write(path, {"trades": trades})
        except Exception as exc:
            log.warning("[StraddleNative] _append_pnl: %s", exc)

    # ------------------------------------------------------------------
    # 通知ヘルパー
    # ------------------------------------------------------------------

    @staticmethod
    def _notify(title: str, body: str, priority: int = 0) -> None:
        try:
            from common.pushover_client import send as _pushover
            _pushover(title, body, priority=priority)
        except Exception as exc:
            log.debug("[StraddleNative] Pushover: %s", exc)


# ---------------------------------------------------------------------------
# GammaScalpNativeEngine — GammaScalpEngine の atlas_v3 native 移植
# ---------------------------------------------------------------------------


class GammaScalpNativeEngine:
    """spy_bot.GammaScalpEngine の atlas_v3 native 移植。

    StraddleNativeEngine でポジションを開いた後、毎 tick を監視し
    SPY 価格変動が ATR(14) × GAMMA_SCALP_ATR_TRIGGER を超えた時点で
    スキャルプを実行する（デルタリセット）。

    Public interface (spy_bot 互換):
        reset_daily()
        initialize_atr()
        update_price(spy_price)
        monitor_gamma_opportunity() -> Optional[str]
        execute_scalp(direction) -> bool
        check_stop_loss() -> bool
        check_and_hedge()    <- atlas_v3 向けリネーム（tick 相当）
        tick()               <- spy_bot 互換エイリアス

    check_and_hedge() は check_and_hedge という名前で atlas_v3 Engine から呼ばれる。
    """

    def __init__(
        self,
        straddle_eng: StraddleNativeEngine,
        mkt: Optional[Any] = None,
        eng: Optional[Any] = None,
        paper: bool = False,
        dry_test: bool = False,
    ) -> None:
        self.straddle_eng = straddle_eng
        self.mkt          = mkt
        self.eng          = eng
        self.paper        = paper
        self.dry_test     = dry_test

        self._spy_price_history: list[tuple[datetime.datetime, float]] = []
        self._atr14:             Optional[float] = None
        self._scalp_count_today: int   = 0
        self._last_scalp_ts:     Optional[datetime.datetime] = None
        self._min_scalp_interval_min: float = GAMMA_SCALP_MIN_INTERVAL_MIN

    # ------------------------------------------------------------------
    # 日次リセット
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        self._spy_price_history  = []
        self._atr14              = None
        self._scalp_count_today  = 0
        self._last_scalp_ts      = None

    # ------------------------------------------------------------------
    # ATR 初期化
    # ------------------------------------------------------------------

    def initialize_atr(self) -> None:
        """起動時に原資産の ATR(14) を計算して保持する。"""
        ticker  = self.straddle_eng._get_ticker()
        closes  = _fetch_closes_for_atr(ticker, days=20)
        self._atr14 = _calc_atr14(closes)
        if self._atr14 is not None:
            log.info("[GammaScalpNative] ATR(14)=%.2f (%s)", self._atr14, ticker)
        else:
            log.warning("[GammaScalpNative] ATR(14) 計算失敗 → スキャルプ無効 (%s)", ticker)

    # ------------------------------------------------------------------
    # 価格更新
    # ------------------------------------------------------------------

    def update_price(self, spy_price: float) -> None:
        """現在価格を履歴に追記する（直近 30 分のみ保持）。"""
        now    = datetime.datetime.now(ET)
        cutoff = now - datetime.timedelta(minutes=30)
        self._spy_price_history.append((now, spy_price))
        self._spy_price_history = [
            (ts, p) for ts, p in self._spy_price_history if ts >= cutoff
        ]

    # ------------------------------------------------------------------
    # ガンマスキャルプ機会監視
    # ------------------------------------------------------------------

    def monitor_gamma_opportunity(self) -> Optional[str]:
        """ガンマスキャルプ機会を監視。Returns "CALL" / "PUT" / None。"""
        pos = self.straddle_eng.position
        if pos is None:
            return None
        if self._scalp_count_today >= GAMMA_SCALP_MAX_PER_DAY:
            return None
        if self._last_scalp_ts is not None:
            elapsed_min = (
                datetime.datetime.now(ET) - self._last_scalp_ts
            ).total_seconds() / 60.0
            if elapsed_min < self._min_scalp_interval_min:
                return None
        if self._atr14 is None:
            return None

        move = self._get_5min_move()
        if move is None:
            return None

        threshold = self._atr14 * GAMMA_SCALP_ATR_TRIGGER
        if abs(move) < threshold:
            return None

        direction = "CALL" if move > 0 else "PUT"
        log.info(
            "[GammaScalpNative] opportunity: move=%+.3f thr=%.3f dir=%s "
            "count=%d/%d",
            move, threshold, direction,
            self._scalp_count_today, GAMMA_SCALP_MAX_PER_DAY,
        )
        return direction

    # ------------------------------------------------------------------
    # スキャルプ実行
    # ------------------------------------------------------------------

    def execute_scalp(self, direction: str) -> bool:
        """ガンマスキャルプを実行する。Returns True=成功 / False=失敗|スキップ。"""
        pos = self.straddle_eng.position
        if pos is None:
            return False

        if kill_switch_is_active():
            log.warning("[GammaScalpNative] execute_scalp: Kill Switch ARMED → スキップ")
            return False

        ticker    = self.straddle_eng._get_ticker()
        spy_price = self._get_spy_price(ticker)
        if spy_price is None or spy_price <= 0:
            return False

        new_atm_strike  = round(spy_price)
        now_et          = datetime.datetime.now(ET)
        expiry_date_str = now_et.strftime("%y%m%d")

        if direction == "CALL":
            close_code = pos.call_code
            close_qty  = pos.call_qty
            new_code   = f"US.{ticker}{expiry_date_str}C{int(new_atm_strike * 1000)}"
        else:
            close_code = pos.put_code
            close_qty  = pos.put_qty
            new_code   = f"US.{ticker}{expiry_date_str}P{int(new_atm_strike * 1000)}"

        log.info(
            "[GammaScalpNative] scalp: close %s qty=%d → open %s (spy=%.2f strike=%d)",
            close_code, close_qty, new_code, spy_price, new_atm_strike,
        )

        if self.dry_test:
            return self._dry_scalp(direction, pos, close_code, new_code, spy_price, new_atm_strike, now_et)

        return self._live_scalp(direction, pos, close_code, close_qty, new_code, spy_price, now_et)

    # ------------------------------------------------------------------
    # ストップロス確認
    # ------------------------------------------------------------------

    def check_stop_loss(self) -> bool:
        """ストラドルのストップロス条件を確認する。True=発動。"""
        pos = self.straddle_eng.position
        if pos is None or self.dry_test:
            return False
        if self.mkt is None:
            return False

        try:
            codes = [pos.call_code, pos.put_code]
            ret, snap_df = self.mkt.get_market_snapshot(codes)
            # ret == 0 が OK (futu 規約互換)
            if ret != 0 or snap_df is None:
                return False
            # snap_df は pandas.DataFrame or dict 互換を想定
            prices: dict[str, float] = {}
            try:
                for _, row in snap_df.iterrows():
                    code = row.get("code", "")
                    prices[code] = float(row.get("last_price", 0) or 0)
            except AttributeError:
                # snap_df が dict-of-lists 形式の場合
                for code, price in snap_df.items():
                    prices[str(code)] = float(price or 0)

            pnl = pos.current_pnl(
                prices.get(pos.call_code, 0.0),
                prices.get(pos.put_code, 0.0),
            )
            if pnl <= -pos.stop_loss_threshold:
                log.warning(
                    "[GammaScalpNative] STOP LOSS: pnl=%.2f <= -%.2f",
                    pnl, pos.stop_loss_threshold,
                )
                return True
        except Exception as exc:
            log.warning("[GammaScalpNative] check_stop_loss: %s", exc)
        return False

    # ------------------------------------------------------------------
    # check_and_hedge / tick — メインループ呼び出しエントリポイント
    # ------------------------------------------------------------------

    def check_and_hedge(self) -> None:
        """毎 tick (60 秒ごと) 呼ばれる。価格更新 → 強制クローズ → SL → スキャルプ。

        atlas_v3 Engine からの dispatch 名。spy_bot 互換エイリアスは tick()。
        """
        pos = self.straddle_eng.position
        if pos is None:
            return

        ticker    = self.straddle_eng._get_ticker()
        spy_price = self._get_spy_price(ticker)
        if spy_price and spy_price > 0:
            self.update_price(spy_price)

        now_et = datetime.datetime.now(ET)

        # 強制クローズ（0DTE ガンマリスク対策）
        if (now_et.hour > GAMMA_SCALP_FORCE_CLOSE_H or
                (now_et.hour == GAMMA_SCALP_FORCE_CLOSE_H
                 and now_et.minute >= GAMMA_SCALP_FORCE_CLOSE_M)):
            log.info(
                "[GammaScalpNative] force close at %s ET", now_et.strftime("%H:%M")
            )
            self.straddle_eng.close_straddle(pos, "force_close_time")
            return

        if self.check_stop_loss():
            self.straddle_eng.close_straddle(pos, "stop_loss")
            return

        direction = self.monitor_gamma_opportunity()
        if direction is not None:
            self.execute_scalp(direction)

    def tick(self) -> None:
        """spy_bot.GammaScalpEngine.tick() 互換エイリアス。check_and_hedge() を呼ぶ。"""
        self.check_and_hedge()

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _get_5min_move(self) -> Optional[float]:
        """直近 5 分間の価格変動（正=上昇 / 負=下落）を返す。"""
        now    = datetime.datetime.now(ET)
        cutoff = now - datetime.timedelta(minutes=5)
        past   = [(ts, p) for ts, p in self._spy_price_history if ts <= cutoff]
        if not past or not self._spy_price_history:
            return None
        return self._spy_price_history[-1][1] - past[-1][1]

    def _get_spy_price(self, ticker: str) -> Optional[float]:
        if self.dry_test:
            return self.straddle_eng._fetch_price_finnhub(ticker) or 560.0
        if self.mkt is not None:
            try:
                p = self.mkt.get_last_price(ticker)
                if p and p > 0:
                    return float(p)
            except Exception:
                pass
        return self.straddle_eng._fetch_price_finnhub(ticker)

    def _dry_scalp(
        self,
        direction: str,
        pos: StraddleNativePosition,
        close_code: str,
        new_code: str,
        spy_price: float,
        new_atm_strike: int,
        now_et: datetime.datetime,
    ) -> bool:
        self._scalp_count_today += 1
        pos.scalp_count         += 1
        self._last_scalp_ts      = now_et
        if direction == "CALL":
            pos.call_code = new_code
        else:
            pos.put_code  = new_code

        log.info(
            "[GammaScalpNative][DRY-TEST] scalp: dir=%s count=%d "
            "old=%s new=%s",
            direction, self._scalp_count_today, close_code, new_code,
        )
        self.straddle_eng._append_pnl({
            "event": "scalp", "direction": direction,
            "old_code": close_code, "new_code": new_code,
            "spy_price": spy_price, "new_strike": new_atm_strike,
            "scalp_count_today": self._scalp_count_today, "dry_test": True,
        })
        self.straddle_eng._notify(
            "[GammaScalpNative] スキャルプ(DRY-TEST)",
            f"{direction} scalp#{self._scalp_count_today} spy={spy_price:.2f}",
        )
        return True

    def _live_scalp(
        self,
        direction: str,
        pos: StraddleNativePosition,
        close_code: str,
        close_qty: int,
        new_code: str,
        spy_price: float,
        now_et: datetime.datetime,
    ) -> bool:
        if self.eng is None:
            log.warning("[GammaScalpNative] eng=None → スキャルプスキップ")
            return False

        try:
            sell_id = self.eng.place_sell(
                close_code, close_qty, f"gamma_scalp_close_{direction}"
            )
        except Exception as exc:
            log.warning("[GammaScalpNative] place_sell: %s", exc)
            sell_id = None

        if sell_id is None:
            log.warning("[GammaScalpNative] %s 売却失敗 → スキャルプ中止", direction)
            return False

        try:
            buy_id = self.eng.place_buy(
                code=new_code,
                qty=close_qty,
                label=f"gamma_scalp_open_{direction}",
            )
        except Exception as exc:
            log.warning("[GammaScalpNative] place_buy new: %s", exc)
            buy_id = None

        if buy_id is None:
            log.warning("[GammaScalpNative] 新 %s 購入失敗", direction)
            self.straddle_eng._notify(
                "[GammaScalpNative] 新オプション購入失敗",
                f"{direction} 売却済み・新 ATM 購入失敗。片脚残留リスク。手動確認要。",
                priority=1,
            )
            return False

        if direction == "CALL":
            pos.call_code = new_code
        else:
            pos.put_code  = new_code

        self._scalp_count_today += 1
        pos.scalp_count         += 1
        self._last_scalp_ts      = now_et
        log.info(
            "[GammaScalpNative] scalp OK: %s %s→%s count=%d spy=%.2f",
            direction, close_code, new_code, self._scalp_count_today, spy_price,
        )
        self.straddle_eng._append_pnl({
            "event": "scalp", "direction": direction,
            "old_code": close_code, "new_code": new_code,
            "spy_price": spy_price,
            "scalp_count_today": self._scalp_count_today,
        })
        self.straddle_eng._notify(
            "[GammaScalpNative] スキャルプ実行",
            f"{direction} scalp#{self._scalp_count_today} spy={spy_price:.2f}",
        )
        return True
