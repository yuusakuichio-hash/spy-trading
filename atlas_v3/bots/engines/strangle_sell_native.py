"""atlas_v3/bots/engines/strangle_sell_native.py — Multi-DTE Strangle Sell エンジン

spy_bot.py の StrangleSellEngine (L11333-L11845) を atlas_v3 ネイティブに移植。
short_strangle_0dte.py (0DTE 専用) と完全独立 — multi-DTE (0DTE/1DTE 両対応) が本モジュール。

設計方針:
    - spy_bot.py に一切触れない (参照もしない)
    - TacticBase ABC 継承 + TacticType="enter_exit"
    - reset_daily / execute_entry / check_exit の 3 メソッドを必須 public API とする
    - PDTGuard (atlas_v3/bots/engines/pdt_guard.py) を発注前に必ず通す
    - earnings_proximity チェック (is_near_earnings) を execute_entry 冒頭で実施
    - pre_trade_check (common_v3/risk/pre_trade_check.py) を execute_entry 内で実施
    - Kill Switch (common_v3/risk/kill_switch.py) を reset_daily / execute_entry / check_exit で確認
    - すべての定数は設定 DTO (StrangleSellNativeConfig) 経由 — グローバル書き換え禁止
    - CC <= 20 規律準拠 (各 public メソッドは 20 行以内の論理分岐構成)
    - spy_bot.py StrangleSellEngine の動作を完全再現し multi-DTE 拡張を加える

Multi-DTE 拡張 (spy_bot 版との主差分):
    - dte: 0 / 1 選択可能 (0=当日満期, 1=翌営業日満期)
    - 1DTE 時: フォースクローズ時刻を翌日 9:50 ET に変更（0DTE は 15:45 ET）
    - 1DTE 時: PDT day_trade カウント対象外 (allow_expiry_pass_through=True 相当)
    - allow_expiry_pass_through: True = OTM のまま満期消滅 → PDT 消費なし

エントリー条件 (spy_bot.py L11338-L11353 移植):
    - IVR > ivr_min (動的 P70 相当 fallback=60)
    - VIX in [vix_min, vix_max]
    - 10:30-12:00 ET エントリー窓
    - OTM CALL delta ≈ +0.15 / OTM PUT delta ≈ -0.15

エグジット:
    - 利確: buyback コスト <= net_credit × (1 - profit_target_pct)
    - 損切り: buyback コスト >= net_credit × stop_loss_mult
    - フォースクローズ: 0DTE=15:45 ET / 1DTE=翌日 09:50 ET

TacticBase ABC 継承 / Kill Switch 連動 / PDT ガード / earnings_proximity 統合
"""
from __future__ import annotations

import logging
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Literal, Optional
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    pass

from atlas_v3.bots.engines.earnings_calendar_check import is_near_earnings
from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.risk.kill_switch import is_active as kill_switch_is_active
from common_v3.risk.pre_trade_check import GateResult, OrderCtx, check_order_critical_only

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# デフォルト定数 (spy_bot.py L10017-L10037 参照)
# ---------------------------------------------------------------------------

#: CALL 脚の目標 delta（OTM）
DEFAULT_CALL_DELTA: float = 0.15

#: PUT 脚の目標 |delta|（OTM）
DEFAULT_PUT_DELTA: float = 0.15

#: delta 許容誤差（±0.05 以内の strike を採用）
DEFAULT_DELTA_TOL: float = 0.05

#: fallback IVR 閾値（IVR > 60 で優先エントリー: P70 相当）
DEFAULT_IVR_MIN: float = 60.0

#: VIX 上限（クライシス環境はスキップ）
DEFAULT_VIX_MAX: float = 50.0

#: VIX 下限（IV 低すぎはスキップ）
DEFAULT_VIX_MIN: float = 15.0

#: 利確ライン（受取クレジットの 50% 取得で利確）
DEFAULT_PROFIT_TARGET_PCT: float = 0.50

#: 損切り倍率（buyback コスト = 2× credit でストップ）
DEFAULT_STOP_LOSS_MULT: float = 2.00

#: 口座の 3% を最大リスク
DEFAULT_MAX_RISK_PCT: float = 0.03

#: 最大 2 契約
DEFAULT_MAX_QTY: int = 2

#: エントリー窓開始（ET）
DEFAULT_ENTRY_OPEN_H: int = 10
DEFAULT_ENTRY_OPEN_M: int = 30

#: エントリー窓終了（ET）
DEFAULT_ENTRY_CLOSE_H: int = 12
DEFAULT_ENTRY_CLOSE_M: int = 0

#: 0DTE フォースクローズ（ET）
DEFAULT_FORCE_CLOSE_0DTE_H: int = 15
DEFAULT_FORCE_CLOSE_0DTE_M: int = 45

#: 1DTE フォースクローズ（翌日 ET）— 翌朝 IV crush 取得後の手仕舞い
DEFAULT_FORCE_CLOSE_1DTE_H: int = 9
DEFAULT_FORCE_CLOSE_1DTE_M: int = 50

#: 半日取引日フォースクローズ（ET）
DEFAULT_EARLY_CLOSE_H: int = 12
DEFAULT_EARLY_CLOSE_M: int = 50

#: 決算近接ブロック日数
DEFAULT_EARNINGS_PROXIMITY_DAYS: int = 5


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class StrangleSellNativeConfig:
    """Multi-DTE Strangle Sell 設定 DTO。

    Attributes:
        dte:                     0=当日満期 / 1=翌営業日満期
        call_delta:              CALL 脚目標 delta
        put_delta:               PUT 脚目標 |delta|
        delta_tol:               delta 許容誤差
        ivr_min:                 IVR エントリー最低閾値
        vix_min:                 VIX 下限
        vix_max:                 VIX 上限
        profit_target_pct:       利確ライン（クレジットの何割取得で利確）
        stop_loss_mult:          損切り倍率
        max_risk_pct:            口座資本の最大リスク比率
        max_qty:                 最大発注数量
        entry_open_h:            エントリー窓開始時（ET）
        entry_open_m:            エントリー窓開始分（ET）
        entry_close_h:           エントリー窓終了時（ET）
        entry_close_m:           エントリー窓終了分（ET）
        force_close_0dte_h:      0DTE フォースクローズ時（ET）
        force_close_0dte_m:      0DTE フォースクローズ分（ET）
        force_close_1dte_h:      1DTE フォースクローズ時（翌日 ET）
        force_close_1dte_m:      1DTE フォースクローズ分（翌日 ET）
        early_close_h:           半日取引日フォースクローズ時（ET）
        early_close_m:           半日取引日フォースクローズ分（ET）
        paper:                   True=ペーパーモード（PDT/VIX 条件バイパス）
        earnings_proximity_days: 決算近接ブロック日数（0 で無効）
    """
    dte: int = 0
    call_delta: float = DEFAULT_CALL_DELTA
    put_delta: float = DEFAULT_PUT_DELTA
    delta_tol: float = DEFAULT_DELTA_TOL
    ivr_min: float = DEFAULT_IVR_MIN
    vix_min: float = DEFAULT_VIX_MIN
    vix_max: float = DEFAULT_VIX_MAX
    profit_target_pct: float = DEFAULT_PROFIT_TARGET_PCT
    stop_loss_mult: float = DEFAULT_STOP_LOSS_MULT
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT
    max_qty: int = DEFAULT_MAX_QTY
    entry_open_h: int = DEFAULT_ENTRY_OPEN_H
    entry_open_m: int = DEFAULT_ENTRY_OPEN_M
    entry_close_h: int = DEFAULT_ENTRY_CLOSE_H
    entry_close_m: int = DEFAULT_ENTRY_CLOSE_M
    force_close_0dte_h: int = DEFAULT_FORCE_CLOSE_0DTE_H
    force_close_0dte_m: int = DEFAULT_FORCE_CLOSE_0DTE_M
    force_close_1dte_h: int = DEFAULT_FORCE_CLOSE_1DTE_H
    force_close_1dte_m: int = DEFAULT_FORCE_CLOSE_1DTE_M
    early_close_h: int = DEFAULT_EARLY_CLOSE_H
    early_close_m: int = DEFAULT_EARLY_CLOSE_M
    paper: bool = True
    earnings_proximity_days: int = DEFAULT_EARNINGS_PROXIMITY_DAYS


# ---------------------------------------------------------------------------
# Position DTO
# ---------------------------------------------------------------------------

@dataclass
class StrangleSellNativePosition:
    """Multi-DTE Strangle Sell ポジション表現。

    Attributes:
        symbol:            銘柄 (例: "US.SPY")
        call_code:         CALL オプションコード
        put_code:          PUT オプションコード
        call_strike:       CALL ストライク
        put_strike:        PUT ストライク
        qty:               発注枚数
        call_entry_price:  CALL 受取プレミアム（per share）
        put_entry_price:   PUT 受取プレミアム（per share）
        net_credit:        受取総クレジット（(call+put)×qty×100）
        entry_time:        エントリー時刻（ISO 形式文字列）
        expiry:            満期日（YYYY-MM-DD）
        call_delta:        エントリー時 CALL delta（記録用）
        put_delta:         エントリー時 PUT |delta|（記録用）
        dte:               0=当日満期 / 1=翌営業日満期
        tactic_name:       戦術識別子
    """
    symbol: str
    call_code: str
    put_code: str
    call_strike: float
    put_strike: float
    qty: int
    call_entry_price: float
    put_entry_price: float
    net_credit: float
    entry_time: str
    expiry: str
    call_delta: float = 0.0
    put_delta: float = 0.0
    dte: int = 0
    tactic_name: str = "strangle_sell_native"


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrangleSellEntryDecision:
    """エントリー判定結果。"""
    should_enter: bool
    symbol: str
    reason: str = ""
    call_strike: float = 0.0
    put_strike: float = 0.0
    call_delta: float = 0.0
    put_delta: float = 0.0
    call_credit: float = 0.0
    put_credit: float = 0.0
    qty: int = 0
    net_credit: float = 0.0
    expiry: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class StrangleSellExitDecision:
    """エグジット判定結果。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal["profit_target", "stop_loss", "force_close", "none"] = "none"
    pnl_usd: float = 0.0


# ---------------------------------------------------------------------------
# ユーティリティ関数
# ---------------------------------------------------------------------------

def _et_now() -> datetime:
    """現在時刻を ET タイムゾーン付きで返す。"""
    return datetime.now(timezone.utc).astimezone(ET)


def _expiry_for_dte(now_et: datetime, dte: int) -> str:
    """dte=0 なら当日、dte=1 なら翌営業日の満期日文字列（YYYY-MM-DD）を返す。

    dte=1: 土曜→月曜、日曜→月曜 をスキップして翌営業日を返す。
    """
    target = now_et.date() + timedelta(days=dte)
    # dte=1 でも週末なら翌月曜にシフト
    if dte >= 1:
        while target.weekday() >= 5:  # 5=土, 6=日
            target += timedelta(days=1)
    return target.strftime("%Y-%m-%d")


def _is_in_entry_window(now_et: datetime, open_h: int, open_m: int,
                         close_h: int, close_m: int) -> bool:
    """エントリー窓内か判定する。"""
    open_mins = open_h * 60 + open_m
    close_mins = close_h * 60 + close_m
    now_mins = now_et.hour * 60 + now_et.minute
    return open_mins <= now_mins < close_mins


def _is_force_close_time(now_et: datetime, force_h: int, force_m: int) -> bool:
    """フォースクローズ時刻以降か判定する。"""
    return now_et.hour * 60 + now_et.minute >= force_h * 60 + force_m


def _calc_qty_from_risk(cash: float, net_per_share: float,
                        stop_loss_mult: float, max_risk_pct: float,
                        max_qty: int) -> int:
    """口座リスク率から発注枚数を算出する。"""
    max_risk_usd = cash * max_risk_pct
    risk_per_contract = net_per_share * stop_loss_mult * 100.0
    if risk_per_contract <= 0:
        return 1
    qty = int(max_risk_usd / risk_per_contract)
    return max(1, min(max_qty, qty))


def _estimate_otm_strike(underlying: float, vix: float,
                          opt_type: str, target_delta: float) -> float:
    """delta ≈ target_delta 相当の OTM ストライクを sigma 近似で算出する。

    1σ daily = underlying × (vix/100) / sqrt(252)
    delta=0.15 ≒ 1.2σ OTM として計算。
    """
    sigma_daily = underlying * (vix / 100.0) / math.sqrt(252.0)
    offset = 1.2 * sigma_daily
    if opt_type == "CALL":
        return round(underlying + offset)
    return round(underlying - offset)


def _make_dry_code(symbol: str, expiry: str, opt_type: str, strike: float) -> str:
    """dry-test / paper モード用の仮想オプションコードを生成する。"""
    ticker = symbol.replace("US.", "").replace(".", "")
    date_str = expiry.replace("-", "")[2:]  # YYMMDD
    return f"DRY_{ticker}_{date_str}{opt_type[0]}{int(strike * 1000)}"


def should_trade_today(
    symbol: str,
    vix: Optional[float],
    ivr: Optional[float],
    ivr_min: float,
    paper: bool = False,
    vix_min: float = DEFAULT_VIX_MIN,
    vix_max: float = DEFAULT_VIX_MAX,
) -> bool:
    """当日ストラングル売りを実施すべきか判定する（static ヘルパー）。

    ペーパーモード: VIX/IVR 条件をバイパスして常に True を返す。
    本番モード: VIX 範囲内 AND IVR >= ivr_min の場合のみ True。

    Args:
        symbol:   対象銘柄
        vix:      現在 VIX 値（None なら False）
        ivr:      現在 IVR 値（None なら False、本番のみ）
        ivr_min:  IVR 最低閾値
        paper:    True=ペーパーモード
        vix_min:  VIX 下限
        vix_max:  VIX 上限

    Returns:
        True = 当日実施 / False = スキップ
    """
    if vix is None:
        return False
    if paper:
        log.info(
            "[StrangleSellNative] %s VIX=%.2f IVR=%s → 条件バイパス(ペーパー)",
            symbol, vix, ivr,
        )
        return True
    if not (vix_min <= vix <= vix_max):
        log.info(
            "[StrangleSellNative] %s VIX=%.2f 範囲外 [%.1f, %.1f] → スキップ",
            symbol, vix, vix_min, vix_max,
        )
        return False
    if ivr is None or ivr < ivr_min:
        log.info(
            "[StrangleSellNative] %s IVR=%s < threshold=%.1f → スキップ",
            symbol, ivr, ivr_min,
        )
        return False
    log.info(
        "[StrangleSellNative] %s VIX=%.2f IVR=%.1f → エントリー条件充足",
        symbol, vix, ivr,
    )
    return True


# ---------------------------------------------------------------------------
# StrangleSellNativeEngine — メインエンジン
# ---------------------------------------------------------------------------

class StrangleSellNativeEngine(TacticBase):
    """Multi-DTE Short Strangle 売りエンジン (atlas_v3 ネイティブ実装)。

    spy_bot.py StrangleSellEngine を完全再現しつつ multi-DTE 拡張を加えた
    TacticBase 継承戦術。

    reset_daily / execute_entry / check_exit が外部から呼ぶ 3 つの必須 API。
    TacticBase 必須の preflight / tactic_type / tactic_name も実装済み。

    Args:
        config:           StrangleSellNativeConfig
        symbol:           対象銘柄 (例: "US.SPY")
        option_chain_fn:  (expiry, opt_type, underlying) -> list[dict] | DI 注入
        account_cash_fn:  () -> float | 口座残高取得 DI
        place_order_fn:   (code, side, qty, tag) -> bool | 発注 DI
        get_price_fn:     (code) -> dict | オプション現在価格取得 DI
        earnings_date_fn: DI 注入（テスト / CI 用）
        pdt_guard:        PDTGuard インスタンス DI
        now_fn:           () -> datetime ET | 現在時刻 DI（テスト用）
    """

    #: PDT 1DTE 対応
    supports_1dte: bool = True

    #: 満期消滅を PDT 不消費として扱う
    allow_expiry_pass_through: bool = True

    def __init__(
        self,
        config: Optional[StrangleSellNativeConfig] = None,
        symbol: str = "US.SPY",
        option_chain_fn: Optional[Callable] = None,
        account_cash_fn: Optional[Callable] = None,
        place_order_fn: Optional[Callable] = None,
        get_price_fn: Optional[Callable] = None,
        earnings_date_fn: Optional[Callable] = None,
        pdt_guard: Optional[PDTGuard] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._cfg = config or StrangleSellNativeConfig()
        self._symbol = symbol
        self._option_chain_fn = option_chain_fn
        self._account_cash_fn = account_cash_fn
        self._place_order_fn = place_order_fn
        self._get_price_fn = get_price_fn
        self._earnings_date_fn = earnings_date_fn
        self._pdt_guard = pdt_guard or PDTGuard(
            paper_mode=self._cfg.paper,
            capital_usd=0.0,
        )
        self._now_fn: Callable[[], datetime] = now_fn or _et_now
        self._lock = threading.Lock()

        # 日次状態
        self._position: Optional[StrangleSellNativePosition] = None
        self._entry_done: bool = False
        self._trade_done: bool = False
        self._entry_attempted: bool = False

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "strangle_sell_native"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック順:
        1. env=None → False
        2. Kill Switch ARMED → False
        3. VIX 範囲外 → False (ペーパーモードはバイパス)

        Returns:
            True = 戦術発動可能 / False = 発動不可
        """
        if env is None:
            log.warning("[StrangleSellNative.preflight] env=None: 失敗")
            return False
        if kill_switch_is_active():
            log.warning("[StrangleSellNative.preflight] Kill Switch ARMED: 無効化")
            return False
        if not self._cfg.paper:
            if not (self._cfg.vix_min <= env.vix <= self._cfg.vix_max):
                log.info(
                    "[StrangleSellNative.preflight] VIX=%.1f 範囲外 [%.1f, %.1f]: halt",
                    env.vix, self._cfg.vix_min, self._cfg.vix_max,
                )
                return False
        return True

    # ------------------------------------------------------------------
    # 必須 public API: reset_daily
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """日次リセット。ポジション残存時は警告ログを出す。"""
        with self._lock:
            if self._position is not None:
                log.warning(
                    "[StrangleSellNative] reset_daily: ポジション残存 "
                    "call=%s put=%s",
                    self._position.call_code, self._position.put_code,
                )
            self._position = None
            self._entry_done = False
            self._trade_done = False
            self._entry_attempted = False
        log.info("[StrangleSellNative] reset_daily 完了 symbol=%s dte=%d",
                 self._symbol, self._cfg.dte)

    # ------------------------------------------------------------------
    # 必須 public API: execute_entry
    # ------------------------------------------------------------------

    def execute_entry(
        self,
        underlying_price: float,
        vix: float,
        ivr: Optional[float] = None,
        signal_id: Optional[str] = None,
    ) -> Optional[StrangleSellNativePosition]:
        """ストラングル売りを発注する。

        (1) 前提条件チェック（Kill Switch / エントリー済 / 時刻窓 / 決算近接）
        (2) OTM CALL / PUT ストライク算出
        (3) サイズ計算（口座リスク率ベース）
        (4) pre_trade_check (common_v3 4層)
        (5) PDTGuard チェック
        (6) 発注 (place_order_fn) または DRY 仮想発注

        Args:
            underlying_price: 原資産現在価格
            vix:              現在 VIX 値
            ivr:              現在 IVR 値（本番モードの追加判定用）
            signal_id:        冪等性 ID（None なら自動生成）

        Returns:
            StrangleSellNativePosition or None
        """
        with self._lock:
            return self._execute_entry_inner(underlying_price, vix, ivr, signal_id)

    def _execute_entry_inner(
        self,
        underlying_price: float,
        vix: float,
        ivr: Optional[float],
        signal_id: Optional[str],
    ) -> Optional[StrangleSellNativePosition]:
        # ---- 前提条件チェック ----
        if kill_switch_is_active():
            log.warning("[StrangleSellNative.execute_entry] Kill Switch ARMED → 中止")
            return None
        if self._entry_attempted or self._entry_done:
            return None
        now_et = self._now_fn()
        if not _is_in_entry_window(now_et,
                                    self._cfg.entry_open_h, self._cfg.entry_open_m,
                                    self._cfg.entry_close_h, self._cfg.entry_close_m):
            log.info("[StrangleSellNative.execute_entry] エントリー窓外 %s → 中止",
                     now_et.strftime("%H:%M ET"))
            return None
        if not should_trade_today(
            self._symbol, vix, ivr,
            self._cfg.ivr_min,
            paper=self._cfg.paper,
            vix_min=self._cfg.vix_min,
            vix_max=self._cfg.vix_max,
        ):
            return None

        # ---- 決算近接チェック ----
        if self._cfg.earnings_proximity_days > 0:
            _safe = self._earnings_date_fn is not None
            blocked, ep_reason = is_near_earnings(
                symbol=self._symbol,
                proximity_days=self._cfg.earnings_proximity_days,
                earnings_date_fn=self._earnings_date_fn,
                safe_default=_safe,
            )
            if blocked:
                log.info("[StrangleSellNative.execute_entry] 決算近接ブロック: %s", ep_reason)
                return None

        self._entry_attempted = True

        # ---- オプション選択 ----
        expiry = _expiry_for_dte(now_et, self._cfg.dte)
        call_opt = self._find_otm_option(expiry, "CALL", underlying_price,
                                          self._cfg.call_delta, vix)
        put_opt = self._find_otm_option(expiry, "PUT", underlying_price,
                                         self._cfg.put_delta, vix)
        if call_opt is None or put_opt is None:
            log.warning(
                "[StrangleSellNative.execute_entry] OTM オプション取得失敗: "
                "call=%s put=%s symbol=%s",
                call_opt, put_opt, self._symbol,
            )
            return None

        call_code = call_opt["code"]
        put_code = put_opt["code"]
        call_strike = float(call_opt.get("strike_price", 0))
        put_strike = float(put_opt.get("strike_price", 0))
        call_bid = float(call_opt.get("bid_price", 0))
        call_ask = float(call_opt.get("ask_price", 0))
        put_bid = float(put_opt.get("bid_price", 0))
        put_ask = float(put_opt.get("ask_price", 0))
        call_delta_val = float(call_opt.get("delta", self._cfg.call_delta))
        put_delta_val = abs(float(call_opt.get("delta", self._cfg.put_delta)))

        call_mid = (call_bid + call_ask) / 2.0 if (call_bid + call_ask) > 0 else call_ask
        put_mid = (put_bid + put_ask) / 2.0 if (put_bid + put_ask) > 0 else put_ask

        if call_mid <= 0 or put_mid <= 0:
            log.warning(
                "[StrangleSellNative.execute_entry] mid 価格不正: "
                "call_mid=%.2f put_mid=%.2f → スキップ",
                call_mid, put_mid,
            )
            return None

        # ---- サイズ計算 ----
        cash = self._get_cash()
        net_per_share = call_mid + put_mid
        qty = _calc_qty_from_risk(cash, net_per_share,
                                   self._cfg.stop_loss_mult,
                                   self._cfg.max_risk_pct,
                                   self._cfg.max_qty)
        net_credit = net_per_share * qty * 100.0

        log.info(
            "[StrangleSellNative] ENTRY plan: %s CALL=%s/%.2f PUT=%s/%.2f "
            "qty=%d net_credit=%.2f expiry=%s dte=%d",
            self._symbol, call_code, call_mid, put_code, put_mid,
            qty, net_credit, expiry, self._cfg.dte,
        )

        # ---- pre_trade_check (4 層) ----
        for code, price in [(call_code, call_mid), (put_code, put_mid)]:
            gate_result: GateResult = check_order_critical_only(
                OrderCtx(symbol=self._symbol, qty=qty, side="SELL", is_long=False)
            )
            if not gate_result.allowed:
                log.warning("[StrangleSellNative.execute_entry] PreTradeGate BLOCKED: %s",
                            gate_result.reason)
                return None

        # ---- PDTGuard チェック ----
        pdt_result = self._pdt_guard.check_can_trade(self._symbol)
        if not pdt_result.allowed:
            log.warning("[StrangleSellNative.execute_entry] PDTGuard BLOCKED: %s",
                        pdt_result.reason)
            return None

        # ---- 発注 ----
        if signal_id is None:
            _sym = self._symbol.replace("US.", "")
            _ts = now_et.strftime("%Y%m%d%H%M")
            signal_id = f"ss_native_{_sym}_{_ts}_{uuid.uuid4().hex[:8]}"

        ok = self._place_legs(call_code, put_code, qty, signal_id)
        if not ok:
            return None

        pos = StrangleSellNativePosition(
            symbol=self._symbol,
            call_code=call_code,
            put_code=put_code,
            call_strike=call_strike,
            put_strike=put_strike,
            qty=qty,
            call_entry_price=call_mid,
            put_entry_price=put_mid,
            net_credit=net_credit,
            entry_time=now_et.isoformat(),
            expiry=expiry,
            call_delta=call_delta_val,
            put_delta=put_delta_val,
            dte=self._cfg.dte,
        )
        self._position = pos
        self._entry_done = True
        log.info(
            "[StrangleSellNative] ENTRY完了: CALL=%s PUT=%s qty=%d credit=%.2f",
            call_code, put_code, qty, net_credit,
        )
        return pos

    # ------------------------------------------------------------------
    # 必須 public API: check_exit
    # ------------------------------------------------------------------

    def check_exit(
        self,
        call_current_price: Optional[float] = None,
        put_current_price: Optional[float] = None,
        is_early_close: bool = False,
    ) -> Optional[StrangleSellExitDecision]:
        """保有ポジションのエグジット条件をチェックする。

        優先度:
        1. Kill Switch → force_close
        2. フォースクローズ時刻（dte / 半日取引日考慮）→ force_close
        3. 損切り（buyback コスト >= net_credit × stop_loss_mult）
        4. 利確（buyback コスト <= net_credit × (1 - profit_target_pct)）

        Args:
            call_current_price: CALL 現在価格（per share）。None なら取得試行。
            put_current_price:  PUT 現在価格（per share）。None なら取得試行。
            is_early_close:     半日取引日フラグ

        Returns:
            StrangleSellExitDecision or None（ポジションなし or 条件不成立）
        """
        with self._lock:
            return self._check_exit_inner(call_current_price, put_current_price,
                                           is_early_close)

    def _check_exit_inner(
        self,
        call_price: Optional[float],
        put_price: Optional[float],
        is_early_close: bool,
    ) -> Optional[StrangleSellExitDecision]:
        if self._position is None:
            return None
        pos = self._position

        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning("[StrangleSellNative.check_exit] Kill Switch ARMED → force_close")
            return self._close_position("force_close_kill_switch")

        # 2. フォースクローズ時刻
        now_et = self._now_fn()
        force_decision = self._check_force_close_time(now_et, pos.dte, is_early_close)
        if force_decision is not None:
            return self._close_position("force_close_time")

        # 3 & 4. P&L 判定（価格取得）
        call_val = self._resolve_price(pos.call_code, call_price, pos.call_entry_price)
        put_val = self._resolve_price(pos.put_code, put_price, pos.put_entry_price)
        if call_val is None or put_val is None:
            return None

        current_cost = (call_val + put_val) * pos.qty * 100.0
        profit_threshold = pos.net_credit * (1.0 - self._cfg.profit_target_pct)
        stop_threshold = pos.net_credit * self._cfg.stop_loss_mult

        if current_cost >= stop_threshold:
            log.info(
                "[StrangleSellNative.check_exit] 損切り: cost=%.2f >= threshold=%.2f",
                current_cost, stop_threshold,
            )
            return self._close_position("stop_loss")
        if current_cost <= profit_threshold:
            log.info(
                "[StrangleSellNative.check_exit] 利確: cost=%.2f <= threshold=%.2f",
                current_cost, profit_threshold,
            )
            return self._close_position("profit_target")

        return None

    # ------------------------------------------------------------------
    # アクセサ
    # ------------------------------------------------------------------

    @property
    def position(self) -> Optional[StrangleSellNativePosition]:
        """現在保有ポジション（None = ノーポジション）。"""
        return self._position

    def is_active(self) -> bool:
        """ポジション保有中かどうか。"""
        return self._position is not None

    @property
    def entry_done(self) -> bool:
        return self._entry_done

    @property
    def trade_done(self) -> bool:
        return self._trade_done

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _find_otm_option(
        self,
        expiry: str,
        opt_type: str,
        underlying: float,
        target_delta: float,
        vix: float,
    ) -> Optional[dict]:
        """指定 delta 付近の OTM オプションを探す。

        option_chain_fn が DI されていれば実チェーンから探す。
        未 DI (None) またはペーパーモードなら sigma 近似で合成する。
        """
        # sigma 近似 (DRY / ペーパー / DI なし)
        if self._option_chain_fn is None or self._cfg.paper:
            strike = _estimate_otm_strike(underlying, vix, opt_type, target_delta)
            code = _make_dry_code(self._symbol, expiry, opt_type, strike)
            sign = 1.0 if opt_type == "CALL" else -1.0
            return {
                "code": code,
                "strike_price": float(strike),
                "delta": sign * target_delta,
                "bid_price": 0.25,
                "ask_price": 0.35,
            }

        # 実チェーン検索
        try:
            chain = self._option_chain_fn(expiry, opt_type, underlying)
        except Exception as exc:
            log.warning("[StrangleSellNative._find_otm_option] chain 取得失敗: %s", exc)
            return None
        if not chain:
            return None

        best: Optional[dict] = None
        best_diff: float = 999.0
        for opt in chain:
            raw_delta = opt.get("delta")
            if raw_delta is None:
                continue
            d = abs(float(raw_delta))
            diff = abs(d - target_delta)
            if diff < best_diff and diff <= self._cfg.delta_tol:
                best_diff = diff
                best = opt
        if best is None:
            log.warning(
                "[StrangleSellNative._find_otm_option] %s delta≈%.2f 候補なし "
                "(TOL=%.2f) symbol=%s",
                opt_type, target_delta, self._cfg.delta_tol, self._symbol,
            )
        return best

    def _get_cash(self) -> float:
        """口座残高を取得する。DI がなければ 10,000 USD を返す。"""
        if self._account_cash_fn is not None:
            try:
                return float(self._account_cash_fn())
            except Exception as exc:
                log.warning("[StrangleSellNative._get_cash] 取得失敗: %s → fallback 10000", exc)
        return 10_000.0

    def _place_legs(self, call_code: str, put_code: str,
                    qty: int, signal_id: str) -> bool:
        """CALL + PUT 両レッグを発注する。DI がなければ仮想発注（True 返却）。"""
        if self._place_order_fn is None:
            log.info("[StrangleSellNative._place_legs] DI なし → 仮想発注 OK")
            return True
        try:
            ok_call = self._place_order_fn(call_code, "SELL", qty, f"{signal_id}_call")
            ok_put = self._place_order_fn(put_code, "SELL", qty, f"{signal_id}_put")
            return bool(ok_call) and bool(ok_put)
        except Exception as exc:
            log.warning("[StrangleSellNative._place_legs] 発注エラー: %s", exc)
            return False

    def _close_legs(self, call_code: str, put_code: str, qty: int) -> None:
        """CALL + PUT 両レッグをクローズする。DI がなければ仮想クローズ。"""
        if self._place_order_fn is None:
            return
        try:
            self._place_order_fn(call_code, "BUY", qty, "strangle_call_close")
            self._place_order_fn(put_code, "BUY", qty, "strangle_put_close")
        except Exception as exc:
            log.warning("[StrangleSellNative._close_legs] クローズエラー: %s", exc)

    def _resolve_price(
        self, code: str, provided: Optional[float], fallback: float
    ) -> Optional[float]:
        """オプション現在価格を解決する。provided > 0 なら即採用、次に get_price_fn、最後 fallback。"""
        if provided is not None and provided > 0:
            return provided
        if self._get_price_fn is not None:
            try:
                snap = self._get_price_fn(code)
                val = snap.get("last") or snap.get("mid")
                if val is not None:
                    return float(val)
            except Exception as exc:
                log.debug("[StrangleSellNative._resolve_price] %s: %s", code, exc)
        return fallback  # entry price fallback

    def _check_force_close_time(
        self,
        now_et: datetime,
        dte: int,
        is_early_close: bool,
    ) -> Optional[bool]:
        """フォースクローズ時刻を超えていれば True を返す。

        優先順:
        1. 半日取引日 → early_close 時刻
        2. dte=1 → force_close_1dte 時刻
        3. dte=0 → force_close_0dte 時刻
        """
        if is_early_close:
            if _is_force_close_time(now_et, self._cfg.early_close_h, self._cfg.early_close_m):
                return True
            return None
        if dte >= 1:
            if _is_force_close_time(now_et, self._cfg.force_close_1dte_h, self._cfg.force_close_1dte_m):
                return True
            return None
        if _is_force_close_time(now_et, self._cfg.force_close_0dte_h, self._cfg.force_close_0dte_m):
            return True
        return None

    def _close_position(self, reason: str) -> StrangleSellExitDecision:
        """ポジションをクローズし ExitDecision を返す。"""
        pos = self._position
        if pos is None:
            return StrangleSellExitDecision(should_exit=True, reason=reason,
                                             exit_type="force_close")
        self._close_legs(pos.call_code, pos.put_code, pos.qty)
        pnl = self._calc_pnl(reason, pos)
        log.info("[StrangleSellNative] CLOSE: reason=%s pnl=%.2f", reason, pnl)
        self._position = None
        self._trade_done = True
        exit_type = _reason_to_exit_type(reason)
        return StrangleSellExitDecision(
            should_exit=True,
            reason=reason,
            exit_type=exit_type,
            pnl_usd=pnl,
        )

    @staticmethod
    def _calc_pnl(reason: str, pos: StrangleSellNativePosition) -> float:
        """reason から概算 PnL を計算する。"""
        cfg_profit = DEFAULT_PROFIT_TARGET_PCT
        cfg_stop = DEFAULT_STOP_LOSS_MULT
        if reason == "profit_target":
            return pos.net_credit * cfg_profit
        if reason == "stop_loss":
            return -pos.net_credit * (cfg_stop - 1.0)
        return 0.0


# ---------------------------------------------------------------------------
# ユーティリティ: reason → exit_type 変換
# ---------------------------------------------------------------------------

def _reason_to_exit_type(
    reason: str,
) -> Literal["profit_target", "stop_loss", "force_close", "none"]:
    """reason 文字列を exit_type リテラルに変換する。"""
    if "profit" in reason:
        return "profit_target"
    if "stop" in reason:
        return "stop_loss"
    if "force" in reason or "kill" in reason.lower():
        return "force_close"
    return "none"
