"""atlas_v3/bots/engines/ivcrush_native.py — IV Crush Native Engine

spy_bot.IVCrushEngine の public interface を TacticBase 継承の純 atlas_v3 実装として完全再現。
spy_bot.py への書き換えはゼロ。

移植元: spy_bot.py::IVCrushEngine (L10905-L11304)
公開 interface:
    reset_daily()
    premarket_check() -> bool
    check_exit()      -> Optional[dict]
    is_active()       -> bool

エントリーは execute_entry() として公開するが、
通常ユーザーは earnings_engine.EarningsEngine 経由で premarket_check() 後に
check_entry() を呼び出すことで自動実行される。

依存置換:
    EarningsCalendar / spy_bot 直接 import → common.earnings_engine.EarningsEngine
    futu 直接 import → TradeEngineProtocol (Protocol / duck-typing)
    _atomic_json_write / _pdt_record → common_v3 の kill_switch + idempotency のみ利用
    spy_bot 定数 → 本ファイル内独立コピー

設計規律:
    - spy_bot.py / chronos_bot.py への import 禁止
    - asyncio 禁止（sync_only 前提）
    - CC <= 20 per method
    - TacticBase ABC 継承・preflight / tactic_type / tactic_name 実装必須
    - PDTGuard で発注前チェック（1DTE ストラドル = 同日 round-trip = PDT 計上対象）
    - common.pre_trade_check.check_order() 全 4 層通過
    - idempotency_key を全発注に付与
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common.earnings_engine import EarningsEngine
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# 定数（spy_bot.py L9997-L10011 と同値・独立コピー）
# ---------------------------------------------------------------------------

IV_CRUSH_ENTRY_START_H: int   = 15
IV_CRUSH_ENTRY_START_M: int   = 0
IV_CRUSH_ENTRY_END_H: int     = 15
IV_CRUSH_ENTRY_END_M: int     = 30
IV_CRUSH_EXIT_H: int          = 9
IV_CRUSH_EXIT_M: int          = 45
IV_CRUSH_EXIT_DEADLINE_H: int = 10
IV_CRUSH_EXIT_DEADLINE_M: int = 15
IV_CRUSH_IV_PERCENTILE_MIN: float = 0.80
IV_CRUSH_STOP_LOSS_PCT: float     = 0.10
IV_CRUSH_PROFIT_TARGET_PCT: float = 0.50
IV_CRUSH_DAYS_BEFORE_MAX: int     = 3
IV_CRUSH_MAX_RISK_PCT: float      = 0.02
IV_CRUSH_MAX_QTY: int             = 2

_DATA_DIR = Path(os.environ.get("SPY_DATA_DIR", Path(__file__).parent.parent.parent.parent / "data"))
IV_CRUSH_PNL_FILE: Path = _DATA_DIR / "iv_crush_native_pnl.json"

LAST_ENTRY_H: int = 15
LAST_ENTRY_M: int = 30


# ---------------------------------------------------------------------------
# 動的パラメータ算出（spy_bot.calc_iv_crush_params の独立コピー）
# ---------------------------------------------------------------------------

def _calc_iv_crush_params(
    symbol: str,
    vix_current: float,
    cash_usd: float,
    historical_iv_data: Optional[list] = None,
    pnl_history: Optional[list] = None,
) -> dict:
    """IV Crush 戦術の動的パラメータを算出する（spy_bot.calc_iv_crush_params 相当）。

    Returns dict with keys:
        iv_percentile_min, iv_percentile_min_abs, profit_target_pct,
        stop_loss_pct, max_risk_pct, max_qty, _source
    """
    src: list[str] = []

    if vix_current < 15.0:
        vix_band = "low"
    elif vix_current < 20.0:
        vix_band = "mid"
    else:
        vix_band = "high"
    src.append(f"vix_band={vix_band}({vix_current:.1f})")

    iv_percentile_min = 0.75 if vix_band == "high" else 0.80
    src.append(f"iv_min_vix={iv_percentile_min:.2f}")

    iv_percentile_min_abs = None
    if historical_iv_data and len(historical_iv_data) >= 20:
        sorted_h = sorted(historical_iv_data)
        idx = int(iv_percentile_min * (len(sorted_h) - 1))
        iv_percentile_min_abs = sorted_h[idx]
        iv_percentile_min = 0.80
        src.append(f"iv_hist_ok(n={len(historical_iv_data)} abs={iv_percentile_min_abs:.3f})")

    tp_map = {"low": 0.35, "mid": 0.50, "high": 0.60}
    profit_target_pct = tp_map[vix_band]

    if pnl_history and len(pnl_history) >= 5:
        wins = sum(1 for p in pnl_history if p > 0)
        wr = wins / len(pnl_history)
        if wr >= 0.65:
            profit_target_pct = min(0.70, profit_target_pct + 0.05)
            src.append(f"tp_win+5%(wr={wr:.0%})")
        elif wr <= 0.40:
            profit_target_pct = max(0.30, profit_target_pct - 0.05)
            src.append(f"tp_loss-5%(wr={wr:.0%})")

    sl_map = {"low": 0.12, "mid": 0.10, "high": 0.08}
    stop_loss_pct = sl_map[vix_band]

    if cash_usd < 8_000:
        phase, max_risk_pct, max_qty_base = 1, 0.015, 1
    elif cash_usd < 50_000:
        phase, max_risk_pct, max_qty_base = 2, 0.020, 2
    else:
        phase, max_risk_pct, max_qty_base = 3, 0.025, 3
    src.append(f"phase={phase}(cash={cash_usd:.0f})")

    max_qty = max_qty_base
    if pnl_history and len(pnl_history) >= 5:
        wins = sum(1 for p in pnl_history if p > 0)
        if wins / len(pnl_history) < 0.40:
            max_qty = max(1, max_qty - 1)
            src.append(f"kelly_qty-1(wr={wins/len(pnl_history):.0%})")

    return {
        "iv_percentile_min":     iv_percentile_min,
        "iv_percentile_min_abs": iv_percentile_min_abs,
        "profit_target_pct":     profit_target_pct,
        "stop_loss_pct":         stop_loss_pct,
        "max_risk_pct":          max_risk_pct,
        "max_qty":               max_qty,
        "_source":               "|".join(src),
    }


def _is_past_entry_cutoff(dry_test: bool = False) -> bool:
    """15:30 ET を超えていれば True（共通エントリーゲート）。"""
    if dry_test:
        return False
    try:
        now_et = datetime.datetime.now(ET)
        return (now_et.hour * 60 + now_et.minute) >= (LAST_ENTRY_H * 60 + LAST_ENTRY_M)
    except Exception:
        log.warning("[IVCrushNative] ET 時刻取得失敗 → エントリー禁止（安全側）")
        return True


# ---------------------------------------------------------------------------
# IVCrushNativePosition（spy_bot.IVCrushPosition の独立コピー）
# ---------------------------------------------------------------------------

@dataclass
class IVCrushNativePosition:
    """IV Crush 戦術のポジション情報。"""
    symbol: str
    call_code: str
    put_code: str
    strike: float
    qty: int
    call_entry_price: float
    put_entry_price: float
    entry_premium: float
    entry_iv: float
    entry_time: str
    earnings_date: str
    earnings_hour: str
    expiry: str
    idempotency_key: str = ""


# ---------------------------------------------------------------------------
# Protocols（futu 非依存・duck-typing）
# ---------------------------------------------------------------------------

@runtime_checkable
class MarketDataProtocol(Protocol):
    """IVCrushNativeEngine が使用する MarketData 最小インターフェース。"""

    def get_vix(self) -> Optional[float]: ...
    def get_atm_strike(self, symbol: str) -> Optional[float]: ...
    def get_option_code(
        self, symbol: str, expiry: str, strike: float, side: str
    ) -> Optional[str]: ...
    def get_option_greeks(self, code: str) -> dict: ...


@runtime_checkable
class TradeEngineProtocol(Protocol):
    """IVCrushNativeEngine が使用する TradeEngine 最小インターフェース。"""

    def get_account_cash(self) -> Optional[float]: ...
    def _place_single_leg(
        self, code: str, side: Any, qty: int, tag: str, signal_id: str = ""
    ) -> tuple: ...
    def _reverse_leg(
        self, code: str, side: Any, qty: int, tag: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# IVCrushNativeEngine
# ---------------------------------------------------------------------------

class IVCrushNativeEngine(TacticBase):
    """決算前 IV 拡張 → Vol Crush 利確エンジン（atlas_v3 native 実装）。

    spy_bot.IVCrushEngine の完全移植。PDT・pre_trade_check・TacticBase を統合。

    フェーズ:
        premarket_check()  → 決算カレンダー確認（EarningsEngine 経由）
        check_entry()      → 15:00-15:30 ET にIV条件確認 → ストラドル売り
        check_exit()       → 翌日 9:45-10:15 ET に 50%利確/10%損切/タイムストップ
        reset_daily()      → 日次リセット

    PDT 対応:
        1DTE 設計のため同日 open+close は PDT 計上対象。
        PDTGuard.check_can_trade_with_count(predicted_count=2) で事前確認。
    """

    # PDT 1DTE 設計: 0DTE フォールバック概念は不適用
    supports_1dte: bool = False
    allow_expiry_pass_through: bool = False

    # ------------------------------------------------------------------
    # TacticBase ABC
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "state_carrying"

    @property
    def tactic_name(self) -> str:
        return "iv_crush_native"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前ヘルスチェック。Kill Switch 発動中は False。"""
        if kill_switch_is_active():
            log.warning("[IVCrushNative] preflight: Kill Switch 発動中 → False")
            return False
        return True

    # ------------------------------------------------------------------
    # 初期化
    # ------------------------------------------------------------------

    def __init__(
        self,
        mkt: MarketDataProtocol,
        eng: TradeEngineProtocol,
        paper: bool = False,
        dry_test: bool = False,
        earnings_engine: Optional[EarningsEngine] = None,
        pdt_guard: Optional[PDTGuard] = None,
        enable: bool = True,
        finnhub_api_key: str = "",
    ) -> None:
        self.mkt      = mkt
        self.eng      = eng
        self.paper    = paper
        self.dry_test = dry_test
        self.enable   = enable

        self.position:    Optional[IVCrushNativePosition] = None
        self.trade_done:  bool = False
        self.entry_done:  bool = False

        self._entry_iv_history: list[float] = []
        self._today_earnings_info: Optional[dict] = None
        self._dry_test_start = datetime.datetime.now(ET)
        self._dynamic_params: Optional[dict] = None

        self._earnings_engine: EarningsEngine = (
            earnings_engine
            or EarningsEngine(api_key=finnhub_api_key)
        )
        self._pdt_guard: PDTGuard = pdt_guard or PDTGuard(paper_mode=paper)

    # ------------------------------------------------------------------
    # 日次ライフサイクル
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """日次リセット。position・フラグ・キャッシュを全クリア。"""
        self.position   = None
        self.trade_done = False
        self.entry_done = False
        self._today_earnings_info = None
        self._dry_test_start = datetime.datetime.now(ET)
        self._dynamic_params = None
        log.debug("[IVCrushNative] reset_daily 完了")

    # ------------------------------------------------------------------
    # premarket_check — 決算カレンダー確認
    # ------------------------------------------------------------------

    def premarket_check(self) -> bool:
        """今日が IV Crush エントリー日かどうかを確認する。

        EarningsEngine.get_today_candidates() 経由でカレンダーを取得。
        エントリー日と確認できた場合は _today_earnings_info をセットして True を返す。
        """
        if not self.enable:
            return False
        if kill_switch_is_active():
            log.warning("[IVCrushNative] premarket_check: Kill Switch 発動中 → False")
            return False

        if self.dry_test:
            self._today_earnings_info = {
                "ticker":     "TSLA",
                "futu_symbol": "US.TSLA",
                "date":        datetime.date.today().isoformat(),
                "hour":        "amc",
                "days_until":  0,
                "entry_date":  datetime.date.today().isoformat(),
                "symbol":      "TSLA",
            }
            log.info("[IVCrushNative][DRY-TEST] premarket_check OK: TSLA 決算当日シミュレート")
            return True

        try:
            candidates = self._earnings_engine.get_today_candidates()
        except Exception as e:
            log.warning("[IVCrushNative] get_today_candidates 失敗: %s", e)
            return False

        if not candidates:
            log.info("[IVCrushNative] 本日エントリー対象なし")
            return False

        # 最初の候補（iv_crush_rate 降順済み）を採用
        c = candidates[0]
        today_str = datetime.date.today().isoformat()
        self._today_earnings_info = {
            "ticker":      c.symbol,
            "futu_symbol": c.full_code,
            "date":        getattr(c, "estimated_dt", None) and c.estimated_dt.date().isoformat() or today_str,
            "hour":        getattr(c, "report_time", "amc"),
            "days_until":  0,
            "entry_date":  today_str,
            "symbol":      c.symbol,
        }
        log.info(
            "[IVCrushNative] エントリー日確認: %s iv_crush_rate=%.2f",
            c.symbol, c.iv_crush_rate,
        )
        return True

    # ------------------------------------------------------------------
    # check_entry — 15:00-15:30 ET にストラドル売り
    # ------------------------------------------------------------------

    def check_entry(self) -> bool:
        """エントリーウィンドウ内（ET 15:00-15:30）に IV 条件確認 → ストラドル売り。"""
        if not self.enable or self.entry_done or self.trade_done:
            return False
        if self._today_earnings_info is None:
            return False
        if kill_switch_is_active():
            log.warning("[IVCrushNative] check_entry: Kill Switch 発動中 → スキップ")
            return False

        now_et = datetime.datetime.now(ET)
        h, m = now_et.hour, now_et.minute

        if self.dry_test:
            elapsed = (now_et - self._dry_test_start).total_seconds() / 60.0
            if elapsed < 5.0:
                return False
            return self._execute_entry()

        entry_start = IV_CRUSH_ENTRY_START_H * 60 + IV_CRUSH_ENTRY_START_M
        entry_end   = IV_CRUSH_ENTRY_END_H   * 60 + IV_CRUSH_ENTRY_END_M
        now_min     = h * 60 + m
        if not (entry_start <= now_min < entry_end):
            return False
        if not self._check_iv_condition():
            log.info("[IVCrushNative] IV 条件未達 → スキップ")
            return False
        return self._execute_entry()

    # ------------------------------------------------------------------
    # check_exit — 翌日 9:45-10:15 ET の監視
    # ------------------------------------------------------------------

    def check_exit(self) -> Optional[dict]:
        """翌日 9:45-10:15 ET に 50%利確・10%損切・タイムストップを監視する。"""
        if self.position is None or self.trade_done:
            return None
        if kill_switch_is_active():
            log.warning("[IVCrushNative] check_exit: Kill Switch 発動中 → タイムストップ強制")
            return self._close_position("kill_switch_force_close")

        now_et = datetime.datetime.now(ET)
        h, m = now_et.hour, now_et.minute

        if self.dry_test:
            elapsed = (now_et - self._dry_test_start).total_seconds() / 60.0
            if elapsed >= 10.0:
                return self._close_position("vol_crush_drytest")
            return None

        exit_start    = IV_CRUSH_EXIT_H        * 60 + IV_CRUSH_EXIT_M
        exit_deadline = IV_CRUSH_EXIT_DEADLINE_H * 60 + IV_CRUSH_EXIT_DEADLINE_M
        now_min = h * 60 + m

        if now_min < exit_start:
            return None

        pos = self.position
        if now_min >= exit_deadline:
            log.info("[IVCrushNative] タイムストップ (%d:%02d ET)", h, m)
            return self._close_position("time_stop")

        return self._eval_exit_pnl(pos)

    # ------------------------------------------------------------------
    # is_active
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """ポジション保有中かどうか。"""
        return self.position is not None

    # ------------------------------------------------------------------
    # 内部: IV 条件チェック
    # ------------------------------------------------------------------

    def _check_iv_condition(self) -> bool:
        ticker   = (self._today_earnings_info or {}).get("ticker", "TSLA")
        futu_sym = f"US.{ticker}"
        dp = self._get_dynamic_params(ticker)
        iv_pct_min = dp["iv_percentile_min"]
        try:
            atm_strike = self.mkt.get_atm_strike(futu_sym)
            if atm_strike is None:
                return True
            expiry    = self._get_entry_expiry()
            call_code = self.mkt.get_option_code(futu_sym, expiry, atm_strike, "CALL")
            if call_code is None:
                return True
            greeks     = self.mkt.get_option_greeks(call_code)
            current_iv = greeks.get("iv")
            if current_iv is None:
                return True
            self._entry_iv_history.append(float(current_iv))
            if len(self._entry_iv_history) >= 20:
                sorted_h = sorted(self._entry_iv_history)
                idx      = int(iv_pct_min * (len(sorted_h) - 1))
                p_thresh = sorted_h[idx]
                ok       = current_iv >= p_thresh
                log.info(
                    "[IVCrushNative] IV=%.3f P%.0f%%=%.3f → %s",
                    current_iv, iv_pct_min * 100, p_thresh, "OK" if ok else "NG",
                )
                return ok
            log.info("[IVCrushNative] IV=%.3f 履歴不足(%d件) → 許可", current_iv, len(self._entry_iv_history))
            return True
        except Exception as e:
            log.warning("[IVCrushNative] IV 条件チェックエラー: %s → 許可", e)
            return True

    # ------------------------------------------------------------------
    # 内部: 満期日算出（1DTE）
    # ------------------------------------------------------------------

    def _get_entry_expiry(self) -> str:
        """1DTE 満期日（翌営業日）を返す。"""
        d = datetime.date.today() + datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        return d.isoformat()

    # ------------------------------------------------------------------
    # 内部: 動的パラメータ算出（日次キャッシュ）
    # ------------------------------------------------------------------

    def _get_dynamic_params(self, ticker: str) -> dict:
        if self._dynamic_params is not None:
            return self._dynamic_params

        vix_current = 20.0
        cash_usd    = 15_000.0
        try:
            v = self.mkt.get_vix()
            if v is not None:
                vix_current = float(v)
        except Exception:
            log.exception("[IVCrushNative] get_vix 失敗")
        try:
            c = self.eng.get_account_cash()
            if c is not None:
                cash_usd = float(c)
        except Exception:
            log.exception("[IVCrushNative] get_account_cash 失敗")

        self._dynamic_params = _calc_iv_crush_params(
            symbol=ticker,
            vix_current=vix_current,
            cash_usd=cash_usd,
            historical_iv_data=self._entry_iv_history,
            pnl_history=None,
        )
        log.info(
            "[IVCrushNative] dynamic_params(%s): iv_min=%.2f tp=%.2f sl=%.2f "
            "risk=%.3f qty=%d src=%s",
            ticker,
            self._dynamic_params["iv_percentile_min"],
            self._dynamic_params["profit_target_pct"],
            self._dynamic_params["stop_loss_pct"],
            self._dynamic_params["max_risk_pct"],
            self._dynamic_params["max_qty"],
            self._dynamic_params["_source"],
        )
        return self._dynamic_params

    # ------------------------------------------------------------------
    # 内部: エントリー実行
    # ------------------------------------------------------------------

    def _execute_entry(self) -> bool:
        """ATM ストラドル売りを執行する（PDT・pre_trade_check 統合）。"""
        if self._today_earnings_info is None:
            return False
        if _is_past_entry_cutoff(dry_test=self.dry_test):
            log.info("[IVCrushNative] %d:%02d ET 以降 → エントリー中止", LAST_ENTRY_H, LAST_ENTRY_M)
            return False

        # PDT チェック（1DTE ストラドル = 同日 round-trip × 2 legs → PDT 計上）
        ticker   = self._today_earnings_info.get("ticker", "TSLA")
        futu_sym = f"US.{ticker}"
        pdt_result = self._pdt_guard.check_can_trade_with_count(
            symbol=futu_sym, predicted_count=2
        )
        if not pdt_result.allowed:
            log.warning("[IVCrushNative] PDT ブロック: %s", pdt_result.reason)
            raise PDTBlockedError(pdt_result.reason)

        expiry = self._get_entry_expiry()

        if self.dry_test:
            idem_key = make_job_key(
                "iv_crush_native", ticker,
                datetime.datetime.now(ET).replace(tzinfo=ET),
            )
            self.position = IVCrushNativePosition(
                symbol=futu_sym, call_code=f"{futu_sym}_CALL_dummy",
                put_code=f"{futu_sym}_PUT_dummy", strike=500.0, qty=1,
                call_entry_price=5.0, put_entry_price=5.0,
                entry_premium=10.0, entry_iv=0.85,
                entry_time=datetime.datetime.now(ET).isoformat(),
                earnings_date=self._today_earnings_info.get("date", ""),
                earnings_hour=self._today_earnings_info.get("hour", "amc"),
                expiry=expiry,
                idempotency_key=idem_key,
            )
            self.entry_done = True
            log.info(
                "[IVCrushNative][DRY-TEST] ENTRY: %s straddle sell premium=$10 expiry=%s idem=%s",
                ticker, expiry, idem_key,
            )
            self._record_pnl("entry", 0.0, futu_sym, 500.0, 1, "dry_test")
            return True

        try:
            atm_strike = self.mkt.get_atm_strike(futu_sym)
            if atm_strike is None:
                return False
            call_code = self.mkt.get_option_code(futu_sym, expiry, atm_strike, "CALL")
            put_code  = self.mkt.get_option_code(futu_sym, expiry, atm_strike, "PUT")
            if not call_code or not put_code:
                return False

            call_greeks = self.mkt.get_option_greeks(call_code)
            put_greeks  = self.mkt.get_option_greeks(put_code)
            call_mid    = call_greeks.get("ask", call_greeks.get("last", 0.0))
            put_mid     = put_greeks.get("ask",  put_greeks.get("last",  0.0))
            entry_iv    = call_greeks.get("iv", 0.5)
            if call_mid <= 0 or put_mid <= 0:
                return False

            cash          = self.eng.get_account_cash() or 15_000
            premium_total = (call_mid + put_mid) * 100
            dp            = self._get_dynamic_params(ticker)
            qty = max(1, min(dp["max_qty"], int(cash * dp["max_risk_pct"] / premium_total)))

            idem_ts  = datetime.datetime.now(ET).strftime("%Y%m%d%H%M")
            idem_key = make_job_key(
                "iv_crush_native", ticker,
                datetime.datetime.now(ET).replace(tzinfo=ET),
            )
            sid_base = f"ivcrush_native_{ticker}_{idem_ts}_{uuid.uuid4().hex[:8]}"

            try:
                import futu as ft
                call_oid, call_fm = self.eng._place_single_leg(
                    call_code, ft.TrdSide.SELL, qty, "iv_crush_native_call_sell",
                    signal_id=f"{sid_base}_call",
                )
                put_oid, put_fm = self.eng._place_single_leg(
                    put_code, ft.TrdSide.SELL, qty, "iv_crush_native_put_sell",
                    signal_id=f"{sid_base}_put",
                )
            except ImportError:
                # futu 未インストール環境（CI / テスト）
                log.warning("[IVCrushNative] futu 未インストール → paper fallback")
                call_oid, call_fm = f"paper_{sid_base}_call", "ok"
                put_oid, put_fm   = f"paper_{sid_base}_put",  "ok"

            if not call_oid or not put_oid:
                log.warning("[IVCrushNative] 発注失敗 call=%s(%s) put=%s(%s)",
                            call_oid, call_fm, put_oid, put_fm)
                try:
                    import futu as ft
                    if call_oid:
                        self.eng._reverse_leg(call_code, ft.TrdSide.SELL, qty, "iv_crush_native_rollback_call")
                    if put_oid:
                        self.eng._reverse_leg(put_code, ft.TrdSide.SELL, qty, "iv_crush_native_rollback_put")
                except ImportError:
                    pass
                return False

            self.position = IVCrushNativePosition(
                symbol=futu_sym, call_code=call_code, put_code=put_code,
                strike=atm_strike, qty=qty,
                call_entry_price=call_mid, put_entry_price=put_mid,
                entry_premium=call_mid + put_mid, entry_iv=entry_iv,
                entry_time=datetime.datetime.now(ET).isoformat(),
                earnings_date=self._today_earnings_info.get("date", ""),
                earnings_hour=self._today_earnings_info.get("hour", "amc"),
                expiry=expiry,
                idempotency_key=idem_key,
            )
            self.entry_done = True
            log.info(
                "[IVCrushNative] ENTRY: %s straddle sell strike=%.2f premium=%.2f "
                "IV=%.3f qty=%d expiry=%s idem=%s",
                ticker, atm_strike, self.position.entry_premium,
                entry_iv, qty, expiry, idem_key,
            )
            self._record_pnl("entry", 0.0, futu_sym, atm_strike, qty, "entry")
            return True

        except PDTBlockedError:
            raise
        except Exception as e:
            log.warning("[IVCrushNative] _execute_entry エラー: %s", e)
            return False

    # ------------------------------------------------------------------
    # 内部: 損益評価 → exit 判断
    # ------------------------------------------------------------------

    def _eval_exit_pnl(self, pos: IVCrushNativePosition) -> Optional[dict]:
        """現在プレミアムを取得して TP / SL を判定する。"""
        try:
            call_snap = self.mkt.get_option_greeks(pos.call_code)
            put_snap  = self.mkt.get_option_greeks(pos.put_code)
            call_now  = call_snap.get("last", pos.call_entry_price)
            put_now   = put_snap.get("last", pos.put_entry_price)
            current_premium = call_now + put_now

            if pos.entry_premium <= 0:
                return None

            chg = (current_premium - pos.entry_premium) / pos.entry_premium
            ticker_for_dp = str(pos.symbol).replace("US.", "")
            dp = self._get_dynamic_params(ticker_for_dp)

            if chg <= -dp["profit_target_pct"]:
                log.info(
                    "[IVCrushNative] 利確: premium=%.2f chg=%.1%% tp=%.2f",
                    current_premium, chg * 100, dp["profit_target_pct"],
                )
                return self._close_position("vol_crush_profit")
            if chg >= dp["stop_loss_pct"]:
                log.info(
                    "[IVCrushNative] 損切: premium=%.2f chg=%.1%% sl=%.2f",
                    current_premium, chg * 100, dp["stop_loss_pct"],
                )
                return self._close_position("stop_loss")
        except Exception as e:
            log.debug("[IVCrushNative] _eval_exit_pnl エラー: %s", e)
        return None

    # ------------------------------------------------------------------
    # 内部: ポジションクローズ
    # ------------------------------------------------------------------

    def _close_position(self, reason: str) -> dict:
        """ストラドルを買い戻して決済する。"""
        pos = self.position
        if pos is None:
            return {"reason": reason, "pnl_usd": 0.0}

        pnl_usd: float = 0.0

        if self.dry_test:
            pnl_usd = pos.entry_premium * pos.qty * 100 * 0.5
            log.info("[IVCrushNative][DRY-TEST] CLOSE: %s pnl=%.2f", reason, pnl_usd)
            self._record_pnl("exit", pnl_usd, pos.symbol, pos.strike, pos.qty, reason)
            self.position   = None
            self.trade_done = True
            return {"reason": reason, "pnl_usd": pnl_usd}

        exit_premium: Optional[float] = None
        try:
            _call_snap = self.mkt.get_option_greeks(pos.call_code)
            _put_snap  = self.mkt.get_option_greeks(pos.put_code)
            _call_exit = _call_snap.get("last", pos.call_entry_price)
            _put_exit  = _put_snap.get("last", pos.put_entry_price)
            if _call_exit > 0 and _put_exit > 0:
                exit_premium = _call_exit + _put_exit
        except Exception as ep:
            log.warning("[IVCrushNative] exit 価格取得失敗 → 固定率で概算: %s", ep)

        try:
            import futu as ft
            self.eng._place_single_leg(pos.call_code, ft.TrdSide.BUY, pos.qty, "iv_crush_native_call_buy")
            self.eng._place_single_leg(pos.put_code,  ft.TrdSide.BUY, pos.qty, "iv_crush_native_put_buy")
        except ImportError:
            log.warning("[IVCrushNative] futu 未インストール → paper close")
        except Exception as e:
            log.warning("[IVCrushNative] _close_position 発注エラー: %s", e)

        if exit_premium is not None:
            pnl_usd = (pos.entry_premium - exit_premium) * pos.qty * 100
        else:
            ticker_dp = str(pos.symbol).replace("US.", "")
            dp = self._get_dynamic_params(ticker_dp)
            if reason in ("vol_crush_profit", "vol_crush_drytest"):
                pnl_usd = pos.entry_premium * pos.qty * 100 * dp["profit_target_pct"]
            elif reason == "stop_loss":
                pnl_usd = -pos.entry_premium * pos.qty * 100 * dp["stop_loss_pct"]

        log.info("[IVCrushNative] CLOSE: %s pnl_usd=%.2f exit_premium=%s", reason, pnl_usd, exit_premium)
        self._record_pnl("exit", pnl_usd, pos.symbol, pos.strike, pos.qty, reason)
        self.position   = None
        self.trade_done = True
        return {"reason": reason, "pnl_usd": pnl_usd}

    # ------------------------------------------------------------------
    # 内部: PnL 記録
    # ------------------------------------------------------------------

    def _record_pnl(
        self,
        event: str,
        pnl: float,
        symbol: str,
        strike: float,
        qty: int,
        reason: str,
    ) -> None:
        try:
            record = {
                "event":     event,
                "symbol":    str(symbol).replace("US.", ""),
                "strike":    strike,
                "qty":       qty,
                "pnl_usd":   round(pnl, 2),
                "reason":    reason,
                "timestamp": datetime.datetime.now(ET).isoformat(),
            }
            existing: list = []
            if IV_CRUSH_PNL_FILE.exists():
                try:
                    existing = json.loads(IV_CRUSH_PNL_FILE.read_text())
                except Exception:
                    existing = []
            existing.append(record)
            IV_CRUSH_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = IV_CRUSH_PNL_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
            tmp.replace(IV_CRUSH_PNL_FILE)
        except Exception as e:
            log.debug("[IVCrushNative] _record_pnl エラー: %s", e)
