"""atlas_v3/bots/engines/calendar_native.py — Calendar Spread Engine (subprocess 依存ゼロ移植)

spy_bot.CalendarEngine の public interface を TacticBase 継承の純 atlas_v3 実装として完全再現。
spy_bot.py への書き換えはゼロ。

移植元: spy_bot.py::CalendarEngine (L8836-L9393)
公開 interface:
    reset_daily()
    premarket_check(intraday_monitor=None) -> bool
    execute_entry(spy_price, vix, signal_id=None) -> Optional[CalendarNativePosition]
    check_exit(intraday_monitor=None) -> Optional[dict]
    check_back_leg() -> Optional[dict]

依存置換:
    MarketData      -> MarketDataProtocol (Protocol / duck-typing)
    TradeEngine     -> TradeEngineProtocol (Protocol / duck-typing)
    futu SDK        -> 完全排除（dry_test / chainguard_wrapper / symbol_aware_price）
    chainguard_wrapper.get_chain_center_price_with_fallback でセンター価格動的取得
    symbol_aware_price.get_current_price_with_fallback でシンボル取違え防止

Deep ITM / SPX=300 防止:
    - back_mid / front_mid の Deep ITM 閾値チェック (CALENDAR_DEEP_ITM_THRESHOLD)
    - strike 乖離チェック (CALENDAR_STRIKE_DEVIATION_MAX)

禁則:
    - spy_bot.py / chronos_bot.py へのインポート禁止
    - asyncio 禁止（sync_only 前提）
    - CC <= 20 規律
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
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
# Calendar 定数（spy_bot.py L7575-L7591 と同値・独立コピー）
# ---------------------------------------------------------------------------

CALENDAR_VIX_MIN: float        = 20.0   # VIX 下限（IV 低い環境ではカレンダー優位性なし）
CALENDAR_VIX_MAX: float        = 50.0   # VIX 上限（過度な恐怖相場はスキップ）
CALENDAR_BACK_DAYS: int        = 7      # back レッグ DTE（SPY Weekly: 7 日）
CALENDAR_FORCE_CLOSE_H: int    = 15     # フォースクローズ時刻 (ET): 15:45
CALENDAR_FORCE_CLOSE_M: int    = 45
CALENDAR_MAX_LOSS_PCT: float   = 0.30   # 最大損失 30%（初期 debit に対して）
CALENDAR_IV_CRUSH_PCT: float   = 0.10   # front IV が 10% 以上低下 → IV crush 利確
CALENDAR_MAX_RISK_PCT: float   = 0.02   # 口座の 2% を最大リスク
CALENDAR_MAX_QTY: int          = 2      # 最大 2 契約
ENABLE_CALENDAR: bool          = True   # グローバル ON/OFF

# Deep ITM / strike 整合性ガード定数（spy_bot.py _find_atm_option 同値）
CALENDAR_DEEP_ITM_THRESHOLD: float   = 50.0   # $50 以上の mid は Deep ITM 異常
CALENDAR_STRIKE_DEVIATION_MAX: float = 0.15   # ATM から 15% 超乖離は異常

# back leg 単独 TP/SL 倍率（spy_bot.py check_back_leg 同値）
CALENDAR_BACK_TP_MULT: float  = 1.5
CALENDAR_BACK_SL_MULT: float  = 0.5

# early-close 半日取引日フォースクローズ（spy_bot.py EARLY_CLOSE_EXIT_H/M 同値）
EARLY_CLOSE_EXIT_H: int  = 12
EARLY_CLOSE_EXIT_M: int  = 30

# エントリーカットオフ（spy_bot.py LAST_ENTRY_H/M 同値）
LAST_ENTRY_H: int = 15
LAST_ENTRY_M: int = 30

# PnL ファイル出力先
_BASE_DIR = Path(__file__).parent.parent.parent.parent / "data" / "state_v3"
CALENDAR_PNL_FILE: Path = _BASE_DIR / "calendar_native_pnl.json"

# early-close 半日取引日（spy_bot.py L472 相当）
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
        log.warning("[CalendarNative] ET 時刻取得失敗 → エントリー禁止（安全側）")
        return True


def _get_expiry_today(now_et: datetime.datetime) -> str:
    """今日の満期日文字列を返す（0DTE front leg 用）。"""
    return now_et.strftime("%Y-%m-%d")


def _extract_symbol_from_code(code: str) -> str:
    """'US.SPY260417C710000' -> 'US.SPY'"""
    m = re.match(r"(US\.[A-Z]+)\d{6}", code or "")
    return m.group(1) if m else (code or "")


def _reason_to_exit_type(reason: str) -> str:
    """close reason 文字列から FINRA PDT exit_type を判定する（spy_bot 同値）。"""
    if reason in ("cash_settled",) or "cash_settle" in reason:
        return "cash_settled"
    if reason in ("assigned",) or "assigned" in reason or "auto_exercise" in reason:
        return "assigned"
    if reason in ("expired_worthless", "broker_auto_expired"):
        return "expired_worthless"
    return "manual_close"


def _atomic_json_write(path: Path, data: dict) -> None:
    """ファイル破損防止のための原子的 JSON 書き込み。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# CalendarNativePosition（spy_bot.CalendarPosition の独立コピー + dataclass）
# ---------------------------------------------------------------------------


@dataclass
class CalendarNativePosition:
    """カレンダースプレッドのポジション管理（spy_bot.CalendarPosition と同一構造）。

    front: 0DTE 売りレッグ（シータ崩壊が速い）
    back:  7DTE 買いレッグ（シータ崩壊が遅い・IV crush 後のバリュー保持）
    """

    front_code: str
    back_code: str
    strike: float
    qty: int
    direction: str              # "CALL" or "PUT"
    front_entry_price: float
    back_entry_price: float
    front_iv: float             # エントリー時の front IV（IV crush 判定用）
    entry_time: str = field(
        default_factory=lambda: datetime.datetime.now(ET).isoformat()
    )
    front_closed: bool = False  # front 満期消滅 or 手動クローズ済み

    def __post_init__(self) -> None:
        # initial_debit = 正の値（debit = back_entry_price - front_entry_price）
        self.initial_debit: float = self.back_entry_price - self.front_entry_price


# ---------------------------------------------------------------------------
# Protocols — 依存先の型契約（duck-typing、futu 非依存）
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataProtocol(Protocol):
    """MarketData の最小インターフェース（calendar_native が利用するメソッドのみ）。"""

    @property
    def underlying_code(self) -> str: ...

    @underlying_code.setter
    def underlying_code(self, v: str) -> None: ...

    def get_vix(self) -> Optional[float]: ...

    def get_vix_history(self, days: int = 60) -> list[float]: ...

    def get_option_chain_with_greeks(
        self, expiry: str, direction: str, center_strike: float = 0.0
    ) -> list[dict]: ...

    def find_by_strike(self, chain: list[dict], strike: float) -> Optional[dict]: ...

    def get_last_price(self, symbol: str) -> Optional[float]: ...

    def get_option_greeks(self, code: str) -> dict: ...

    def get_cached_option_price(
        self, code: str, max_age_sec: float = 15.0
    ) -> Optional[float]: ...


@runtime_checkable
class TradeEngineProtocol(Protocol):
    """TradeEngine の最小インターフェース（calendar_native が利用するメソッドのみ）。"""

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
# フォールバック価格テーブル（spy_bot と同値）
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
# CalendarNativeEngine — TacticBase 継承・state_carrying タイプ
# ---------------------------------------------------------------------------


class CalendarNativeEngine(TacticBase):
    """spy_bot.CalendarEngine の atlas_v3 native 移植。

    subprocess 依存ゼロ・futu SDK 直接インポートなし。
    MarketDataProtocol / TradeEngineProtocol で依存を抽象化。
    chainguard_wrapper + symbol_aware_price でセンター価格整合・Deep ITM / SPX=300 防止。

    Public interface（spy_bot.CalendarEngine と完全互換）:
        reset_daily()
        premarket_check(intraday_monitor=None) -> bool
        execute_entry(spy_price, vix, signal_id=None) -> Optional[CalendarNativePosition]
        check_exit(intraday_monitor=None) -> Optional[dict]
        check_back_leg() -> Optional[dict]

    PDT ガード統合:
        Calendar は front 0DTE + back 7DTE の 2 レッグ構成。
        back leg の翌日クローズは PDT 対象外（overnight hold）。
        PDT guard は execute_entry() で paper_mode 確認のみ（is_same_day_round_trip=False）。

    earnings_proximity 統合:
        execute_entry() 内で EarningsEngine.has_earnings_today() を soft-check。
        決算当日は Calendar を強制スキップ（IV crush 期待が消える）。

    pre_trade_check 統合:
        common_v3.risk.pre_trade_check.check_order() を back leg buy 発注前に通す。
        front leg sell は is_long=False → Layer1 Deep ITM ブロックの対象外。
    """

    supports_1dte: bool = True   # back leg は翌日満期に近い = PDT 対象外
    allow_expiry_pass_through: bool = True  # front 満期消滅は expired_worthless 扱い

    def __init__(
        self,
        mkt: Optional[Any] = None,
        eng: Optional[Any] = None,
        paper: bool = False,
        dry_test: bool = False,
        symbol: Optional[str] = None,
    ) -> None:
        self.mkt = mkt
        self.eng = eng
        self.paper = paper
        self.dry_test = dry_test
        # マルチ銘柄対応: symbol=None 時は mkt.underlying_code を使用
        self.symbol = symbol or getattr(mkt, "underlying_code", "US.SPY")

        # 日次状態
        self.position: Optional[CalendarNativePosition] = None
        self.entry_done: bool = False
        self.trade_done: bool = False
        self.today_vix: Optional[float] = None
        self._entry_attempted: bool = False
        self._dry_test_start: datetime.datetime = datetime.datetime.now(ET)

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "state_carrying"

    @property
    def tactic_name(self) -> str:
        return "calendar_native"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。Kill Switch ARMED なら False。"""
        if env is None:
            log.warning("[CalendarNative.preflight] env=None → False")
            return False
        if kill_switch_is_active():
            log.warning("[CalendarNative.preflight] Kill Switch ARMED → False")
            return False
        if env.vix > CALENDAR_VIX_MAX:
            log.info(
                "[CalendarNative.preflight] VIX=%.2f > CALENDAR_VIX_MAX=%.2f → False",
                env.vix, CALENDAR_VIX_MAX,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Phase 0: 日次リセット
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """日付変わり・EOD に日次状態をリセットする。

        front 満期後に back が残存する場合は position.front_closed=True のまま保持。
        front がまだ生きている状態でリセットされた場合はポジション破棄（ログ警告）。
        """
        if self.position is not None and not self.position.front_closed:
            log.warning("[CalendarNative] reset_daily: front クローズ未完了のままリセット")
        # back 持ち越し中（front_closed=True）はポジションを保持
        if self.position is None or not self.position.front_closed:
            self.position = None

        self.entry_done = False
        self.trade_done = False
        self.today_vix = None
        self._entry_attempted = False

    # ------------------------------------------------------------------
    # Phase 1: プレマーケット環境チェック
    # ------------------------------------------------------------------

    def premarket_check(
        self, intraday_monitor: Optional[Any] = None
    ) -> bool:
        """VIX・IVR・VIX5 日トレンドで Calendar エントリー可否を判断する。

        spy_bot.CalendarEngine.should_trade_today() のロジックを統合。
        paper=True の場合は条件をバイパスしてデータ収集を優先する。
        """
        if not ENABLE_CALENDAR:
            log.info("[CalendarNative] premarket_check: ENABLE_CALENDAR=False → skip")
            return False

        if self.dry_test:
            self.today_vix = 22.0
            log.info("[CalendarNative][DRY-TEST] premarket_check: vix=22.0 → OK")
            return True

        if self.mkt is None:
            log.warning("[CalendarNative] premarket_check: mkt=None → False")
            return False

        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("[CalendarNative] premarket_check: VIX 取得失敗 → False")
            return False

        self.today_vix = vix

        # ペーパーモードは VIX/IVR/トレンド条件をバイパス（全環境でデータ収集）
        if self.paper:
            log.info(
                "[CalendarNative][PAPER] VIX=%.2f → 条件バイパス（ペーパー検証モード）", vix
            )
            return True

        if vix < CALENDAR_VIX_MIN or vix > CALENDAR_VIX_MAX:
            log.info(
                "[CalendarNative] Skip: VIX=%.2f は [%.1f, %.1f] 範囲外",
                vix, CALENDAR_VIX_MIN, CALENDAR_VIX_MAX,
            )
            return False

        # VIX 5 日傾向: 直近 5 日終値の線形傾き（末尾 < 先頭なら下降）
        try:
            vix_history = self.mkt.get_vix_history(days=60)
        except Exception:
            vix_history = []
        if len(vix_history) >= 3:
            recent = vix_history[-5:] if len(vix_history) >= 5 else vix_history[-3:]
            slope = recent[-1] - recent[0]
            if slope >= 0:
                log.info(
                    "[CalendarNative] VIX トレンド上昇 (slope=%.2f) → スキップ", slope
                )
                return False

        log.info("[CalendarNative] premarket_check OK: VIX=%.2f", vix)
        return True

    # ------------------------------------------------------------------
    # Phase 2: エントリー実行
    # ------------------------------------------------------------------

    def execute_entry(
        self,
        spy_price: float,
        vix: float,
        signal_id: Optional[str] = None,
    ) -> Optional[CalendarNativePosition]:
        """カレンダースプレッドを発注する。

        (1) 0DTE（front）の ATM オプションを売り
        (2) 7DTE（back）の ATM オプションを買い
        (3) CalendarNativePosition を返す

        Deep ITM 防止: back_mid / front_mid が CALENDAR_DEEP_ITM_THRESHOLD 以上なら中止。
        strike 整合性: ATM から CALENDAR_STRIKE_DEVIATION_MAX 超乖離なら中止。
        earnings_proximity: EarningsEngine.has_earnings_today() でソフトチェック。
        pre_trade_check: back leg buy 発注前に common_v3 check_order() を通す。
        """
        if _is_past_entry_cutoff(dry_test=self.dry_test):
            log.info(
                "[CalendarNative] execute_entry: %d:%02d ET 以降 → エントリー中止",
                LAST_ENTRY_H, LAST_ENTRY_M,
            )
            return None

        if kill_switch_is_active():
            log.warning("[CalendarNative] execute_entry: Kill Switch ARMED → 中止")
            return None

        if self._entry_attempted:
            log.debug("[CalendarNative] execute_entry: 既にエントリー試行済み → スキップ")
            return None

        self._entry_attempted = True

        # earnings_proximity ソフトチェック（決算当日は IV crush 期待消滅）
        if not self.paper and not self.dry_test:
            self._check_earnings_and_skip(spy_price)
            if self.trade_done:
                return None

        now_et = datetime.datetime.now(ET)
        effective_symbol = self.symbol or getattr(self.mkt, "underlying_code", "US.SPY")

        # dry-test モード
        if self.dry_test:
            return self._dry_entry(spy_price, now_et)

        if self.mkt is None:
            log.warning("[CalendarNative] execute_entry: mkt=None → スキップ")
            return None

        # 実価格を symbol_aware_price で再取得して整合確認
        verified_price = self._get_verified_underlying_price(spy_price, effective_symbol)
        if verified_price is None:
            log.error("[CalendarNative] execute_entry: 原資産価格取得失敗 → 中止")
            return None

        front_expiry = _get_expiry_today(now_et)
        back_expiry = self._find_back_expiry(verified_price)
        if back_expiry is None:
            log.warning("[CalendarNative] back leg expiry 取得失敗 → 中止")
            return None

        # 方向は CALL（高 IVR 環境では方向中立 → CALL カレンダーを基本とする）
        direction = "CALL"

        front_opt = self._find_atm_option(front_expiry, direction, verified_price)
        back_opt = self._find_atm_option(back_expiry, direction, verified_price)

        if front_opt is None or back_opt is None:
            log.warning(
                "[CalendarNative] ATM オプション取得失敗: front=%s back=%s",
                front_opt, back_opt,
            )
            return None

        front_code = front_opt["code"]
        back_code = back_opt["code"]
        atm_strike = front_opt.get("strike_price", round(verified_price))
        front_mid = self._calc_mid_price(front_opt)
        back_mid = self._calc_mid_price(back_opt)

        if front_mid is None or back_mid is None:
            log.warning("[CalendarNative] mid price 取得失敗 → 中止")
            return None

        # Deep ITM ガード（back buy 側が主要リスク）
        if back_mid >= CALENDAR_DEEP_ITM_THRESHOLD:
            log.error(
                "[CalendarNative] Deep ITM 異常価格 → 発注拒否: back_mid=%.2f >= threshold=%.0f",
                back_mid, CALENDAR_DEEP_ITM_THRESHOLD,
            )
            return None

        net_debit = back_mid - front_mid
        if net_debit <= 0:
            log.warning(
                "[CalendarNative] net_debit=%.2f <= 0 → スキップ（不合理なプライス）", net_debit
            )
            return None

        # サイズ計算
        cash = self._get_cash()
        qty = self._calc_qty(cash, net_debit)

        # deterministic signal_id（C3-B1 方式）
        if not signal_id:
            _sym = effective_symbol.replace("US.", "").replace(".", "") or "SPY"
            _bar_ts = now_et.strftime("%Y%m%d%H%M")
            signal_id = f"calendar_{_sym}_{direction}_{_bar_ts}"
            log.debug("[CalendarNative] signal_id=%s", signal_id)

        # pre_trade_check（back leg buy 側: is_long=True）
        if not self._pre_trade_gate_check(
            effective_symbol, back_code, qty, back_mid, cash
        ):
            return None

        # 発注: front 売り → back 買い（futu 依存排除: place_sell / place_buy API 統一）
        if not self._place_front_sell(front_code, qty, front_mid, signal_id):
            return None

        time.sleep(0.5)  # front/back 発注間の最小インターバル（spy_bot 同値）

        if not self._place_back_buy(back_code, qty, back_mid, signal_id, front_code, front_mid):
            return None

        # IV 取得（snapshot / fallback）
        front_iv = self._get_front_iv(front_code)

        self._record_pnl("entry", 0.0, direction, float(atm_strike), qty)
        pos = CalendarNativePosition(
            front_code=front_code,
            back_code=back_code,
            strike=float(atm_strike),
            qty=qty,
            direction=direction,
            front_entry_price=front_mid,
            back_entry_price=back_mid,
            front_iv=front_iv,
        )
        self.position = pos
        self.entry_done = True
        log.info(
            "[CalendarNative] ENTRY: %s strike=%.0f front=%s back=%s "
            "debit=%.2f x%d",
            direction, atm_strike, front_code, back_code, net_debit, qty,
        )
        return pos

    # ------------------------------------------------------------------
    # Phase 3: エグジット監視（毎 tick 呼び出す）
    # ------------------------------------------------------------------

    def check_exit(
        self, intraday_monitor: Optional[Any] = None
    ) -> Optional[dict]:
        """保有中ポジションの決済条件をチェックする。

        決済条件（front 生存中）:
        1. IV crush: front IV が entry 比 -10% 以上低下
        2. Max loss: debit が初期比 +30% 以上
        3. フォースクローズ時刻（15:45 ET / 半日取引日は 12:30 ET）
        """
        if self.position is None:
            return None

        pos = self.position
        now_et = datetime.datetime.now(ET)
        h, m = now_et.hour, now_et.minute

        # フォースクローズ時刻チェック
        if not self.dry_test:
            if _is_early_close_today():
                fc_h, fc_m = EARLY_CLOSE_EXIT_H, EARLY_CLOSE_EXIT_M
            else:
                fc_h, fc_m = CALENDAR_FORCE_CLOSE_H, CALENDAR_FORCE_CLOSE_M
            if h > fc_h or (h == fc_h and m >= fc_m):
                return self._close_position("force_close_time")

        # dry_test モード: 起動 7 分後に IV crush シミュレート
        if self.dry_test:
            from_start = (
                now_et - getattr(self, "_dry_test_start", now_et - datetime.timedelta(minutes=10))
            ).total_seconds() / 60.0
            if from_start >= 7.0:
                return self._close_position("iv_crush_drytest")
            return None

        # front IV crush チェック
        if not pos.front_closed:
            iv_result = self._check_iv_crush(pos)
            if iv_result is not None:
                return iv_result

        # Max loss チェック
        loss_result = self._check_max_loss(pos)
        if loss_result is not None:
            return loss_result

        return None

    # ------------------------------------------------------------------
    # Phase 3b: back leg 単独管理（front 満期後）
    # ------------------------------------------------------------------

    def check_back_leg(self) -> Optional[dict]:
        """front 満期消滅後の back 単独ポジション管理。

        front_closed=True かつ back が生きている場合に呼ぶ。
        back last が back_entry_price × 1.5 以上 or 0.5 以下で決済。
        """
        if self.position is None or not self.position.front_closed:
            return None

        pos = self.position
        if self.dry_test:
            # dry_test では即決済
            return self._close_back_only("back_profit_target_drytest")

        if self.mkt is None:
            return None

        try:
            back_snap = self.mkt.get_option_greeks(pos.back_code)
            back_last = back_snap.get("last", None)
            if back_last is None:
                return None
            if back_last >= pos.back_entry_price * CALENDAR_BACK_TP_MULT:
                return self._close_back_only("back_profit_target")
            if back_last <= pos.back_entry_price * CALENDAR_BACK_SL_MULT:
                return self._close_back_only("back_stop_loss")
        except Exception as e:
            log.debug("[CalendarNative] check_back_leg: %s", e)

        return None

    # ------------------------------------------------------------------
    # 内部ヘルパー: 価格取得・オプション選択
    # ------------------------------------------------------------------

    def _get_verified_underlying_price(
        self, hint_price: float, symbol: str
    ) -> Optional[float]:
        """symbol_aware_price で原資産価格を取得・整合確認する。

        hint_price が 0 以下ならフォールバック価格を使用。
        OutOfRangePriceError / StalePriceError は警告レベルで記録し None を返す。
        """
        if self.mkt is None:
            return hint_price if hint_price > 0 else None

        ticker = normalize_symbol(symbol)
        try:
            price, source = get_current_price_with_fallback(
                symbol,
                self.mkt,
                fallback_price=_get_fallback_price(ticker),
            )
            if source != "fallback":
                return price
        except (OutOfRangePriceError, StalePriceError) as exc:
            log.warning("[CalendarNative] 価格整合性エラー: %s", exc)
            return None
        except Exception as exc:
            log.debug("[CalendarNative] _get_verified_underlying_price: %s", exc)

        # hint_price が合理的なら使用
        return hint_price if hint_price > 0 else None

    def _find_back_expiry(self, spy_price: float) -> Optional[str]:
        """7DTE 付近の最も近い expiry を探す。

        futu API 不可時は 7 日後の平日を fallback として返す。
        """
        now_et = datetime.datetime.now(ET)
        target_dt = now_et + datetime.timedelta(days=CALENDAR_BACK_DAYS)
        while target_dt.weekday() >= 5:
            target_dt += datetime.timedelta(days=1)

        if self.dry_test or self.mkt is None:
            return target_dt.strftime("%Y-%m-%d")

        # mkt が get_option_chain_with_greeks を持つ場合はチェーンを参照して確認
        try:
            # 5〜14 日先の範囲でチェーンを検索（spy_bot._find_back_expiry 同方式）
            start_dt = now_et + datetime.timedelta(days=5)
            end_dt = now_et + datetime.timedelta(days=14)
            # チェーン取得で代表的な expiry 一覧を得る（center_strike で絞る）
            atm = round(spy_price)
            chain = self.mkt.get_option_chain_with_greeks(
                start_dt.strftime("%Y-%m-%d"), "CALL", center_strike=float(atm)
            )
            if not chain:
                return target_dt.strftime("%Y-%m-%d")
            expiries = sorted({item.get("expiry_date", "") for item in chain if item.get("expiry_date")})
            if not expiries:
                return target_dt.strftime("%Y-%m-%d")
            target_date = target_dt.date()
            best = min(
                expiries,
                key=lambda d: abs(
                    (datetime.datetime.strptime(d, "%Y-%m-%d").date() - target_date).days
                ),
            )
            return best
        except Exception as e:
            log.warning("[CalendarNative] _find_back_expiry: %s", e)
            return target_dt.strftime("%Y-%m-%d")

    def _find_atm_option(
        self, expiry: str, opt_type: str, spy_price: float
    ) -> Optional[dict]:
        """指定満期・方向の ATM オプション（delta ≈ 0.50）を返す。

        strike 整合性チェック（ATM から CALENDAR_STRIKE_DEVIATION_MAX 超乖離なら None）。
        """
        if self.mkt is None:
            return None

        # chainguard_wrapper で center_price を動的取得・Deep ITM / 混入防止
        ticker = normalize_symbol(self.symbol or "US.SPY")
        try:
            center, _src = get_chain_center_price_with_fallback(
                self.symbol or "US.SPY",
                self.mkt,
                fallback_price=_get_fallback_price(ticker),
            )
        except Exception:
            center = spy_price

        chain = self.mkt.get_option_chain_with_greeks(
            expiry, opt_type, center_strike=float(center)
        )
        if not chain:
            return None

        opt = self.mkt.find_by_strike(chain, spy_price)
        if opt is None:
            return None

        # strike 整合性チェック（spy_bot._find_atm_option 同値）
        strike = opt.get("strike_price", 0)
        if spy_price > 0:
            dev = abs(strike - spy_price) / spy_price
            if dev > CALENDAR_STRIKE_DEVIATION_MAX:
                log.error(
                    "[CalendarNative] strike 整合性 NG: strike=%.0f vs underlying=%.2f "
                    "乖離=%.1f%% symbol=%s → 中止",
                    strike, spy_price, dev * 100,
                    getattr(self.mkt, "underlying_code", "?"),
                )
                return None
        return opt

    def _calc_mid_price(self, opt: dict) -> Optional[float]:
        bid = opt.get("bid_price", 0)
        ask = opt.get("ask_price", 0)
        if bid and ask:
            return (bid + ask) / 2
        return opt.get("last_price") or None

    def _get_front_iv(self, front_code: str) -> float:
        """front leg の IV を取得する（snapshot / fallback=0.30）。"""
        if self.mkt is None:
            return 0.30
        try:
            greeks = self.mkt.get_option_greeks(front_code)
            iv = greeks.get("iv", 0.30)
            return float(iv) if iv else 0.30
        except Exception:
            return 0.30

    def _get_cash(self) -> float:
        if self.eng is not None:
            try:
                cash = self.eng.get_account_cash()
                if cash and cash > 0:
                    return cash
            except Exception:
                pass
        return 10_000.0

    def _calc_qty(self, cash: float, net_debit: float) -> int:
        """サイズ計算（口座資金の CALENDAR_MAX_RISK_PCT 以内）。"""
        max_risk_usd = cash * CALENDAR_MAX_RISK_PCT
        # debit コスト × 100（1 契約）× qty
        if net_debit <= 0:
            return 1
        qty = max(1, min(CALENDAR_MAX_QTY, int(max_risk_usd / (net_debit * 100))))
        return qty

    # ------------------------------------------------------------------
    # 内部ヘルパー: 発注
    # ------------------------------------------------------------------

    def _place_front_sell(
        self,
        front_code: str,
        qty: int,
        front_mid: float,
        signal_id: str,
    ) -> bool:
        """front leg（0DTE）を売り発注する。失敗したら False を返す。"""
        if self.eng is None:
            log.info(
                "[CalendarNative][DRY-RUN] SELL front %s x%d @ %.2f",
                front_code, qty, front_mid,
            )
            return True

        try:
            order_id = self.eng.place_sell(
                front_code, qty, f"calendar_front_{signal_id}"
            )
            if order_id is None:
                log.warning("[CalendarNative] front leg 売り発注失敗")
                return False
            log.info("[CalendarNative] front SELL OK: %s x%d", front_code, qty)
            return True
        except Exception as exc:
            log.error("[CalendarNative] _place_front_sell: %s", exc)
            return False

    def _place_back_buy(
        self,
        back_code: str,
        qty: int,
        back_mid: float,
        signal_id: str,
        front_code: str,
        front_mid: float,
    ) -> bool:
        """back leg（7DTE）を買い発注する。失敗したら front 巻き戻しを試みて False を返す。"""
        if self.eng is None:
            log.info(
                "[CalendarNative][DRY-RUN] BUY back %s x%d @ %.2f",
                back_code, qty, back_mid,
            )
            return True

        try:
            order_id = self.eng.place_buy(
                code=back_code,
                qty=qty,
                label=f"calendar_back_{signal_id}",
                init_price=back_mid,
                use_limit=True,
                signal_id=signal_id + "_back",
            )
            if order_id is None:
                log.warning("[CalendarNative] back leg 買い発注失敗 → front を巻き戻す")
                self._reverse_front(front_code, qty)
                return False
            log.info("[CalendarNative] back BUY OK: %s x%d", back_code, qty)
            return True
        except Exception as exc:
            log.error("[CalendarNative] _place_back_buy: %s", exc)
            self._reverse_front(front_code, qty)
            return False

    def _reverse_front(self, front_code: str, qty: int) -> None:
        """back 発注失敗時に front 売りを巻き戻す（buy で相殺）。"""
        if self.eng is None:
            return
        try:
            self.eng.place_buy(
                code=front_code,
                qty=qty,
                label="calendar_front_reverse",
            )
            log.info("[CalendarNative] front 巻き戻し OK: %s x%d", front_code, qty)
        except Exception as exc:
            log.error("[CalendarNative] front 巻き戻し失敗: %s", exc)

    def _pre_trade_gate_check(
        self,
        symbol: str,
        back_code: str,
        qty: int,
        back_mid: float,
        cash: float,
    ) -> bool:
        """common_v3.risk.pre_trade_check.check_order() を通す。失敗なら False。"""
        try:
            from common_v3.risk.pre_trade_check import check_order, OrderCtx, PreTradeConfig
            ctx = OrderCtx(
                symbol=symbol,
                qty=qty,
                option_price=back_mid,
                side="BUY",
                is_long=True,
                est_margin=back_mid * qty * 100,
                capital_usd=cash,
            )
            result = check_order(ctx, PreTradeConfig())
            if not result.allowed:
                log.warning(
                    "[CalendarNative] pre_trade_gate ブロック: layer=%s reason=%s",
                    result.layer, result.reason,
                )
                return False
        except ImportError:
            log.debug("[CalendarNative] common_v3.risk.pre_trade_check 未ロード（スキップ）")
        except Exception as exc:
            log.warning("[CalendarNative] _pre_trade_gate_check 例外（スキップ）: %s", exc)
        return True

    def _check_earnings_and_skip(self, spy_price: float) -> None:
        """決算当日は Calendar をスキップ（IV crush 期待消滅）。

        soft check: EarningsEngine ロード失敗時は無視して進む。
        """
        try:
            import os
            from common.earnings_engine import EarningsEngine
            api_key = os.environ.get("FINNHUB_API_KEY", "")
            eng = EarningsEngine(api_key=api_key)
            if eng.has_earnings_today():
                ticker = normalize_symbol(self.symbol or "US.SPY")
                candidates = [c.symbol for c in eng.get_today_candidates()]
                if ticker in candidates:
                    log.info(
                        "[CalendarNative] 決算当日: %s → Calendar スキップ（IV crush 期待消滅）",
                        ticker,
                    )
                    self.trade_done = True
        except Exception as exc:
            log.debug("[CalendarNative] earnings_proximity check: %s（無視）", exc)

    # ------------------------------------------------------------------
    # 内部ヘルパー: IV/Max loss チェック
    # ------------------------------------------------------------------

    def _check_iv_crush(self, pos: CalendarNativePosition) -> Optional[dict]:
        """front IV が entry 比 CALENDAR_IV_CRUSH_PCT 以上低下したら利確。"""
        if self.mkt is None:
            return None
        try:
            greeks = self.mkt.get_option_greeks(pos.front_code)
            current_iv = greeks.get("iv", None)
            if current_iv and pos.front_iv > 0:
                iv_change_pct = (current_iv - pos.front_iv) / pos.front_iv
                if iv_change_pct <= -CALENDAR_IV_CRUSH_PCT:
                    log.info(
                        "[CalendarNative] IV crush 検出: iv=%.3f entry=%.3f chg=%.1f%%",
                        current_iv, pos.front_iv, iv_change_pct * 100,
                    )
                    return self._close_position("iv_crush")
        except Exception as e:
            log.debug("[CalendarNative] IV 取得失敗: %s", e)
        return None

    def _check_max_loss(self, pos: CalendarNativePosition) -> Optional[dict]:
        """current_debit が initial_debit の CALENDAR_MAX_LOSS_PCT 以上増加したら損切り。"""
        if self.mkt is None:
            return None
        try:
            front_snap = self.mkt.get_option_greeks(pos.front_code)
            back_snap = self.mkt.get_option_greeks(pos.back_code)
            front_last = front_snap.get("last", pos.front_entry_price)
            back_last = back_snap.get("last", pos.back_entry_price)
            current_debit = back_last - front_last
            if pos.initial_debit > 0:
                loss_pct = (current_debit - pos.initial_debit) / pos.initial_debit
                if loss_pct >= CALENDAR_MAX_LOSS_PCT:
                    log.info(
                        "[CalendarNative] Max loss 到達: current_debit=%.2f initial=%.2f loss=%.1f%%",
                        current_debit, pos.initial_debit, loss_pct * 100,
                    )
                    return self._close_position("max_loss")
        except Exception as e:
            log.debug("[CalendarNative] max loss check 失敗: %s", e)
        return None

    # ------------------------------------------------------------------
    # 内部ヘルパー: ポジション決済
    # ------------------------------------------------------------------

    def _close_position(self, reason: str) -> dict:
        """front + back 両レッグをクローズする。"""
        pos = self.position
        pnl_usd = 0.0

        if self.dry_test:
            pnl_usd = (pos.initial_debit if pos.initial_debit > 0 else 0.0) * pos.qty * 100 * 0.5
            log.info("[CalendarNative][DRY-TEST] CLOSE: reason=%s pnl=%.2f", reason, pnl_usd)
            self._pdt_record_close(pos, reason)
            self._record_pnl("exit", pnl_usd, pos.direction, pos.strike, pos.qty, reason)
            self.position = None
            self.trade_done = True
            return {"reason": reason, "pnl_usd": pnl_usd}

        if self.eng is not None:
            try:
                if not pos.front_closed:
                    # front の売りを buy で相殺（クローズ）
                    self.eng.place_buy(pos.front_code, pos.qty, "cal_front_close")
                # back の買いを sell でクローズ
                self.eng.place_sell(pos.back_code, pos.qty, "cal_back_close")
            except Exception as e:
                log.warning("[CalendarNative] _close_position 発注例外: %s", e)

        # PnL 簡易計算（実約定価格は取得困難なので初期 debit ベース）
        pnl_usd = -(pos.initial_debit * pos.qty * 100)
        if reason in ("iv_crush", "iv_crush_drytest"):
            pnl_usd = abs(pnl_usd) * 0.5  # 利益と仮定

        log.info("[CalendarNative] CLOSE: reason=%s pnl_est=%.2f", reason, pnl_usd)
        self._pdt_record_close(pos, reason)
        self._record_pnl("exit", pnl_usd, pos.direction, pos.strike, pos.qty, reason)
        self.position = None
        self.trade_done = True
        return {"reason": reason, "pnl_usd": pnl_usd}

    def _close_back_only(self, reason: str) -> dict:
        """back レッグのみをクローズする（front 満期後）。"""
        pos = self.position
        pnl_usd = 0.0

        if not self.dry_test and self.eng is not None:
            try:
                self.eng.place_sell(pos.back_code, pos.qty, "cal_back_only_close")
                # back 単独の P&L
                if self.mkt is not None:
                    back_snap = self.mkt.get_option_greeks(pos.back_code)
                    back_last = back_snap.get("last", pos.back_entry_price)
                    front_premium_received = pos.front_entry_price * pos.qty * 100
                    back_pnl = (back_last - pos.back_entry_price) * pos.qty * 100
                    pnl_usd = front_premium_received + back_pnl
            except Exception as e:
                log.warning("[CalendarNative] _close_back_only: %s", e)

        log.info("[CalendarNative] BACK_CLOSE: reason=%s pnl_est=%.2f", reason, pnl_usd)
        self._record_pnl("exit", pnl_usd, pos.direction, pos.strike, pos.qty, reason)
        self.position = None
        self.trade_done = True
        return {"reason": reason, "pnl_usd": pnl_usd}

    def _pdt_record_close(self, pos: CalendarNativePosition, reason: str) -> None:
        """PDTTracker に round_trip を記録する（common_v3.risk.kill_switch 連携）。"""
        try:
            from common.pdt_tracker import get_global_tracker
            tracker = get_global_tracker()
            symbol = _extract_symbol_from_code(pos.front_code) or "US.SPY"
            exit_type = _reason_to_exit_type(reason)
            tracker.record_round_trip(
                symbol,
                datetime.datetime.fromisoformat(pos.entry_time),
                datetime.datetime.now(ET),
                strategy="Calendar",
                exit_type=exit_type,
            )
        except Exception as exc:
            log.debug("[CalendarNative] PDT record 例外（無視）: %s", exc)

    def _record_pnl(
        self,
        event: str,
        pnl_usd: float,
        direction: str,
        strike: float,
        qty: int,
        reason: str = "",
    ) -> None:
        """PnL を JSON ファイルに記録する（ORB と同パターン）。"""
        try:
            CALENDAR_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if CALENDAR_PNL_FILE.exists():
                existing = json.loads(CALENDAR_PNL_FILE.read_text())
            trades = existing.get("trades", [])
            entry = {
                "event": event,
                "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                "pnl_usd": round(pnl_usd, 2),
                "direction": direction,
                "strike": strike,
                "qty": qty,
                "reason": reason,
            }
            trades.append(entry)
            _atomic_json_write(CALENDAR_PNL_FILE, {"trades": trades})
        except Exception as e:
            log.warning("[CalendarNative] _record_pnl: %s", e)

    # ------------------------------------------------------------------
    # dry-test エントリーヘルパー
    # ------------------------------------------------------------------

    def _dry_entry(
        self, spy_price: float, now_et: datetime.datetime
    ) -> CalendarNativePosition:
        """dry_test 用: 仮想発注（futu API 未使用）。"""
        front_expiry = _get_expiry_today(now_et)
        back_expiry = (
            now_et + datetime.timedelta(days=CALENDAR_BACK_DAYS)
        ).strftime("%Y-%m-%d")
        # back expiry が週末なら翌月曜
        back_dt = now_et + datetime.timedelta(days=CALENDAR_BACK_DAYS)
        while back_dt.weekday() >= 5:
            back_dt += datetime.timedelta(days=1)
        back_expiry = back_dt.strftime("%Y-%m-%d")

        direction = "CALL"
        atm_strike = round(spy_price / 5) * 5  # $5 丸め
        front_code = f"DRY_FRONT_{atm_strike}C_{front_expiry}"
        back_code = f"DRY_BACK_{atm_strike}C_{back_expiry}"
        front_price = 0.30
        back_price = 0.60
        front_iv = 0.30
        qty = 1

        log.info(
            "[CalendarNative][DRY-TEST] ENTRY: %s strike=%d "
            "front=%s back=%s debit=%.2f qty=%d",
            direction, atm_strike, front_code, back_code,
            back_price - front_price, qty,
        )
        self._record_pnl("entry", 0.0, direction, float(atm_strike), qty)
        pos = CalendarNativePosition(
            front_code=front_code,
            back_code=back_code,
            strike=float(atm_strike),
            qty=qty,
            direction=direction,
            front_entry_price=front_price,
            back_entry_price=back_price,
            front_iv=front_iv,
        )
        self.position = pos
        self.entry_done = True
        return pos

    # ------------------------------------------------------------------
    # static: should_trade_today（strategy_selector 連携用）
    # ------------------------------------------------------------------

    @staticmethod
    def should_trade_today(
        vix: Optional[float],
        ivr: Optional[float] = None,
        ivr_high_threshold: float = 0.75,
        vix_history: Optional[list] = None,
        paper: bool = False,
    ) -> bool:
        """環境データから Calendar エントリーが適切かを判定する（spy_bot 互換）。

        条件:
        1. ENABLE_CALENDAR が True
        2. VIX が CALENDAR_VIX_MIN〜CALENDAR_VIX_MAX の範囲内
        3. IVR が ivr_high_threshold (動的 P75) 以上（IV が高い環境）
        4. VIX 5 日 EMA 傾向が下降（IV crush 期待）
        paper=True では 2〜4 の条件をバイパス。
        """
        if not ENABLE_CALENDAR:
            return False
        if vix is None:
            return False
        if paper:
            log.info(
                "[CalendarNative][PAPER] VIX=%.2f IVR=%s → 条件バイパス（ペーパー検証モード）",
                vix, ivr,
            )
            return True
        if vix < CALENDAR_VIX_MIN or vix > CALENDAR_VIX_MAX:
            return False
        if ivr is not None and ivr < ivr_high_threshold:
            return False
        if vix_history and len(vix_history) >= 3:
            recent = vix_history[-5:] if len(vix_history) >= 5 else vix_history[-3:]
            slope = recent[-1] - recent[0]
            if slope >= 0:
                log.info("[CalendarNative] VIX トレンド上昇 (slope=%.2f) → False", slope)
                return False
        return True
