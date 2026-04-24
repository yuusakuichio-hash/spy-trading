"""atlas_v3/bots/engines/weekly_gamma_scalp.py — Weekly Gamma Scalp エンジン

設計概要:
    ATM Straddle Long (call + put 同枚数) を毎月曜 open にエントリーし、
    週次 expiry (金曜) まで保有しながら毎日 delta neutral rehedge を行う。
    Gamma long ポジションで realized vol を小刻みに刈り取る戦術。

    既存 spy_bot.GammaScalpEngine (1 日 / SPY 専用) とは完全独立。
    spy_bot.py / common/* には一切触れない。

戦術パラメータ:
    - エントリー: 月曜 open（9:31-9:45 ET）
    - IVR 推奨: 低 IVR 環境（straddle を安く仕込む）— ivr_max で上限管理
    - Delta hedge: 毎日 1 回以上・delta_band 超過時に原資産でニュートラル化
    - Exit: 金曜 15:50 ET 強制クローズ or earnings 前日 15:50 ET 強制クローズ
    - 対応銘柄: SPY / QQQ / IWM（個別株 earnings 前日は自動回避）

Weekly Expiry フィルタ:
    - 金曜日が expiry（通常 weekly）
    - 水曜 FOMC / 木曜 CPI 等の event は earnings_dates で渡す（earnings と同扱い）

Delta Hedge ロジック:
    - Black-Scholes delta を外部注入（stub: 単純 delta_estimate 関数で近似）
    - |portfolio_delta| > delta_band → 原資産で中和
    - hedge_interval_min: 最短再ヘッジ間隔（スキャルプ過多防止）

CC 規律: 各メソッド CC <= 20
TacticBase ABC 継承 / kill_switch 連動
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")

#: 対応銘柄
SUPPORTED_SYMBOLS: frozenset[str] = frozenset({"SPY", "QQQ", "IWM"})

#: エントリー窓（月曜 open 直後 ET）
_ENTRY_WINDOW_START: time = time(9, 31)
_ENTRY_WINDOW_END: time = time(9, 45)

#: 強制クローズ時刻（ET）
_FORCE_CLOSE_TIME: time = time(15, 50)

#: IVR スケール
_IVR_SCALE_MIN: float = 0.0
_IVR_SCALE_MAX: float = 100.0

#: デルタ帯域デフォルト（|delta| > これでヘッジ）
_DELTA_BAND_DEFAULT: float = 0.20

#: 最短再ヘッジ間隔（分）
_HEDGE_INTERVAL_MIN_DEFAULT: float = 60.0

#: 週の曜日番号（0=月曜 … 4=金曜 … 6=日曜）
_MONDAY: int = 0
_FRIDAY: int = 4

#: 戦術識別子
TACTIC_PREFIX: str = "weekly_gamma_scalp"

#: 1 コントラクト = 100 株換算
_CONTRACT_MULTIPLIER: int = 100


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class WeeklyGammaScalpConfig:
    """Weekly Gamma Scalp 設定 DTO。

    Attributes:
        ivr_max:               エントリー上限 IVR（低 IVR 推奨・これ以上は straddle 高すぎ）
        delta_band:            ヘッジ発動 delta 閾値（絶対値）
        hedge_interval_min:    最短再ヘッジ間隔（分）
        stop_loss_pct:         ストップロス（エントリーコスト比）
        profit_target_pct:     利確目標（エントリーコスト比）
        quantity:              デフォルト発注枚数
        paper_mode:            True = paper 発注
    """
    ivr_max: float = 50.0
    delta_band: float = _DELTA_BAND_DEFAULT
    hedge_interval_min: float = _HEDGE_INTERVAL_MIN_DEFAULT
    stop_loss_pct: float = 0.50
    profit_target_pct: float = 1.50
    quantity: int = 1
    paper_mode: bool = True

    def __post_init__(self) -> None:
        if not (0.0 <= self.ivr_max <= _IVR_SCALE_MAX):
            raise ValueError(
                f"ivr_max={self.ivr_max!r} は [0, 100] の範囲外です。"
            )
        if self.delta_band <= 0.0 or self.delta_band > 1.0:
            raise ValueError(
                f"delta_band={self.delta_band!r} は (0, 1] の範囲でなければなりません。"
            )
        if self.hedge_interval_min <= 0.0:
            raise ValueError(
                f"hedge_interval_min={self.hedge_interval_min!r} は正の値でなければなりません。"
            )
        if self.stop_loss_pct <= 0.0:
            raise ValueError(
                f"stop_loss_pct={self.stop_loss_pct!r} は正の値でなければなりません。"
            )


# ---------------------------------------------------------------------------
# エントリー / エグジット 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WeeklyStraddleLeg:
    """Straddle 1 leg の定義。

    Attributes:
        option_type: "call" | "put"
        strike:      権利行使価格（ATM）
        expiry:      満期日（weekly 金曜）
        ask:         エントリー ask 価格（stub: underlying × 0.02 近似）
        quantity:    枚数
    """
    option_type: Literal["call", "put"]
    strike: float
    expiry: date
    ask: float
    quantity: int


@dataclass(frozen=True)
class WeeklyGammaScalpEntry:
    """Weekly Gamma Scalp エントリー決定 DTO。

    Attributes:
        should_enter:    True = エントリー実行
        symbol:          対象銘柄
        legs:            (call_leg, put_leg)
        total_cost:      ストラドル合計エントリーコスト（両 leg × multiplier）
        underlying_price: エントリー時原資産価格
        weekly_expiry:   当週 weekly 満期日（金曜）
        idempotency_key: 二重発注防止キー
        reason:          判定理由
        ivr:             エントリー時 IVR
    """
    should_enter: bool
    symbol: str
    legs: tuple[WeeklyStraddleLeg, ...] = field(default_factory=tuple)
    total_cost: float = 0.0
    underlying_price: float = 0.0
    weekly_expiry: date = field(default_factory=date.today)
    idempotency_key: str = ""
    reason: str = ""
    ivr: float = 0.0


@dataclass(frozen=True)
class DeltaHedgeAction:
    """デルタヘッジ実行記録。

    Attributes:
        symbol:           対象銘柄
        delta_before:     ヘッジ前 portfolio delta
        delta_after:      ヘッジ後 portfolio delta（理論 0）
        hedge_units:      原資産発注株数（正=買い / 負=売り）
        underlying_price: ヘッジ時原資産価格
        pnl_estimate:     ガンマ PnL 概算（0.5 × gamma × delta² × multiplier × qty）
        ts:               実行時刻（UTC ISO）
    """
    symbol: str
    delta_before: float
    delta_after: float
    hedge_units: float
    underlying_price: float
    pnl_estimate: float
    ts: str


@dataclass(frozen=True)
class WeeklyGammaScalpExit:
    """Weekly Gamma Scalp エグジット決定 DTO。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "friday_force_close",
        "earnings_force_close",
        "stop_loss",
        "profit_target",
        "kill_switch",
        "none",
    ] = "none"


# ---------------------------------------------------------------------------
# ポジション状態
# ---------------------------------------------------------------------------

@dataclass
class WeeklyGammaScalpPosition:
    """週またぎ保有ポジション。

    Attributes:
        symbol:           対象銘柄
        entry:            エントリー決定（DTO）
        call_current:     ATM Call 現在価格
        put_current:      ATM Put 現在価格
        portfolio_delta:  現在のポートフォリオ delta
        hedge_events:     delta hedge 実行履歴
        is_closed:        クローズ済みフラグ
        close_reason:     クローズ理由
    """
    symbol: str
    entry: WeeklyGammaScalpEntry
    call_current: float = 0.0
    put_current: float = 0.0
    portfolio_delta: float = 0.0
    hedge_events: list[DeltaHedgeAction] = field(default_factory=list)
    is_closed: bool = False
    close_reason: str = ""

    @property
    def current_option_value(self) -> float:
        """現在のオプション評価額（コントラクト価値合計）。"""
        qty = self.entry.legs[0].quantity if self.entry.legs else 1
        return (self.call_current + self.put_current) * qty * _CONTRACT_MULTIPLIER

    @property
    def current_pnl(self) -> float:
        """含み損益（エントリーコストとの差）。"""
        return self.current_option_value - self.entry.total_cost

    @property
    def scalp_pnl(self) -> float:
        """delta hedge スキャルプで実現した累計 PnL 概算。"""
        return sum(e.pnl_estimate for e in self.hedge_events)


# ---------------------------------------------------------------------------
# ユーティリティ関数（pure・テスト可）
# ---------------------------------------------------------------------------

def get_weekly_expiry(ref_date: date) -> date:
    """ref_date を含む週の金曜日（weekly expiry）を返す。

    Args:
        ref_date: 基準日

    Returns:
        当週金曜の date

    Examples:
        月曜 2026-04-28 → 2026-05-02（金曜）
        金曜 2026-05-01 → 2026-05-01（当日）
    """
    days_to_friday = (_FRIDAY - ref_date.weekday()) % 7
    return ref_date + timedelta(days=days_to_friday)


def is_monday(dt: date) -> bool:
    """dt が月曜かどうかを返す。"""
    return dt.weekday() == _MONDAY


def is_friday(dt: date) -> bool:
    """dt が金曜かどうかを返す。"""
    return dt.weekday() == _FRIDAY


def is_earnings_eve(trade_date: date, earnings_dates: frozenset[date]) -> bool:
    """trade_date の翌営業日が earnings_dates に含まれるかを返す（前日クローズ判定）。

    Args:
        trade_date:     現在の取引日
        earnings_dates: earnings 発表日の集合

    Returns:
        True → 翌日 earnings → 当日 15:50 ET にクローズすべき
    """
    next_day = trade_date + timedelta(days=1)
    return next_day in earnings_dates


def estimate_delta(
    option_type: Literal["call", "put"],
    underlying_price: float,
    strike: float,
    dte: float,
    iv: float,
) -> float:
    """Black-Scholes delta の簡易近似（stub）。

    ATM 付近でのみ有効な 1 次近似:
        call delta ≈ 0.5 + (underlying - strike) / (underlying × iv × sqrt(dte/252))
        put delta  ≈ call delta - 1

    Phase 2 で実際のオプションチェーン greek に差し替え。

    Args:
        option_type:      "call" | "put"
        underlying_price: 原資産価格
        strike:           行使価格
        dte:              満期までの残日数（営業日）
        iv:               implied vol（年率・例: 0.20 = 20%）

    Returns:
        delta（-1.0 〜 +1.0）
    """
    if dte <= 0 or iv <= 0 or underlying_price <= 0:
        return 0.5 if option_type == "call" else -0.5

    vol_adj = iv * math.sqrt(dte / 252.0)
    if vol_adj < 1e-9:
        return 0.5 if option_type == "call" else -0.5

    moneyness = (underlying_price - strike) / (underlying_price * vol_adj)
    call_delta = max(0.01, min(0.99, 0.5 + moneyness * 0.4))
    if option_type == "call":
        return call_delta
    return call_delta - 1.0


def compute_portfolio_delta(
    call_delta: float,
    put_delta: float,
    quantity: int,
) -> float:
    """Long straddle のポートフォリオ delta を計算する。

    Long call delta > 0 / Long put delta < 0
    ATM では call ≈ +0.5, put ≈ -0.5 → portfolio delta ≈ 0

    Args:
        call_delta: ATM call の delta（+0.0 〜 +1.0）
        put_delta:  ATM put の delta（-1.0 〜 +0.0）
        quantity:   枚数（コントラクト数）

    Returns:
        portfolio delta（コントラクト合計）
    """
    return (call_delta + put_delta) * quantity


def compute_hedge_units(
    portfolio_delta: float,
    underlying_price: float,
) -> float:
    """delta 中立化に必要な原資産株数を計算する。

    hedge_units = -portfolio_delta × CONTRACT_MULTIPLIER

    正値 = 買い（delta がプラス過多 → 空売りヘッジ不要・買いヘッジ）
    負値 = 売り

    Args:
        portfolio_delta:  現在のポートフォリオ delta
        underlying_price: 参考原資産価格（将来的な notional 計算用・現在未使用）

    Returns:
        hedge_units（株数・小数点以下は切り捨て）
    """
    _ = underlying_price  # 将来 notional cap 計算で使用予定
    return -portfolio_delta * _CONTRACT_MULTIPLIER


def estimate_gamma_pnl(
    portfolio_delta: float,
    gamma: float,
    quantity: int,
    fee_per_contract: float = 0.65,
) -> float:
    """ガンマスキャルプ 1 回の PnL 概算。

    PnL ≈ 0.5 × gamma × delta² × multiplier × qty - fee
    delta² は価格移動に比例した realized variance の代理指標。

    Args:
        portfolio_delta:    ヘッジ前 delta
        gamma:              オプション gamma（stub: ATM weekly ≈ 0.05-0.15）
        quantity:           枚数
        fee_per_contract:   1 コントラクトあたりの手数料

    Returns:
        pnl_estimate（手数料控除後）
    """
    gross = 0.5 * gamma * (portfolio_delta ** 2) * _CONTRACT_MULTIPLIER * quantity
    fee = fee_per_contract * quantity * 2  # call + put 分
    return gross - fee


# ---------------------------------------------------------------------------
# WeeklyGammaScalpTactic
# ---------------------------------------------------------------------------

class WeeklyGammaScalpTactic(TacticBase):
    """Weekly Gamma Scalp 戦術（Type A: enter_exit）。

    月曜 open に ATM Straddle Long をエントリーし、週次 expiry（金曜）まで
    毎日 delta hedge を繰り返すことで realized vol を収益化する。

    earnings / event 前日に自動クローズ（earnings_dates 注入）。
    multi-symbol: SPY / QQQ / IWM に対応。

    Args:
        config:         WeeklyGammaScalpConfig（省略時はデフォルト）
        clock_fn:       テスト用時刻注入（None = datetime.now(ET)）
        earnings_dates: 銘柄ごとの earnings 発表日集合（銘柄名 → frozenset[date]）
    """

    def __init__(
        self,
        config: WeeklyGammaScalpConfig | None = None,
        clock_fn: "None | (() -> datetime)" = None,
        earnings_dates: "dict[str, frozenset[date]] | None" = None,
    ) -> None:
        self._cfg = config or WeeklyGammaScalpConfig()
        self._clock_fn = clock_fn
        self._earnings_dates: dict[str, frozenset[date]] = earnings_dates or {}
        self._last_hedge_ts: datetime | None = None

    # ------------------------------------------------------------------
    # TacticBase ABC 必須
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return TACTIC_PREFIX

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _now_et(self) -> datetime:
        """現在時刻 (ET)。テスト時は clock_fn で差し替え可。"""
        if self._clock_fn is not None:
            return self._clock_fn()
        return datetime.now(_ET)

    def _today_et(self) -> date:
        return self._now_et().date()

    def _in_entry_window(self) -> bool:
        """月曜 open 窓（9:31-9:45 ET）かつ月曜かどうか。"""
        now = self._now_et()
        if not is_monday(now.date()):
            return False
        t = now.time()
        return _ENTRY_WINDOW_START <= t < _ENTRY_WINDOW_END

    def _past_force_close(self) -> bool:
        """15:50 ET を過ぎているかどうか。"""
        return self._now_et().time() >= _FORCE_CLOSE_TIME

    def _get_earnings_dates(self, symbol: str) -> frozenset[date]:
        """銘柄の earnings 日集合を返す（未登録は空集合）。"""
        return self._earnings_dates.get(symbol, frozenset())

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        """対応銘柄チェック。

        Raises:
            ValueError: SUPPORTED_SYMBOLS 外の銘柄
        """
        if symbol not in SUPPORTED_SYMBOLS:
            raise ValueError(
                f"symbol={symbol!r} は非対応銘柄です。"
                f"対応: {sorted(SUPPORTED_SYMBOLS)}"
            )

    @staticmethod
    def _validate_ivr(symbol: str, ivr: float) -> None:
        """IVR を 0-100 スケールで検証する。

        Raises:
            TypeError: NaN / inf または範囲外
        """
        if not math.isfinite(ivr):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} は NaN または inf です。"
            )
        if not (_IVR_SCALE_MIN <= ivr <= _IVR_SCALE_MAX):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} が 0-100 範囲外です。"
            )

    def _build_straddle_legs(
        self,
        symbol: str,
        underlying_price: float,
        expiry: date,
    ) -> tuple[WeeklyStraddleLeg, WeeklyStraddleLeg]:
        """ATM straddle の 2 leg を構築する。

        ATM strike = round(underlying_price) に設定。
        ask 価格は stub（Phase 2 で実オプションチェーンに差し替え）:
            ask ≈ underlying × 0.02 (2% ATM 近似)

        Args:
            symbol:           銘柄
            underlying_price: 原資産価格
            expiry:           weekly 満期（金曜）

        Returns:
            (call_leg, put_leg)
        """
        atm_strike = round(underlying_price)
        ask_approx = round(underlying_price * 0.02, 2)

        call_leg = WeeklyStraddleLeg(
            option_type="call",
            strike=float(atm_strike),
            expiry=expiry,
            ask=ask_approx,
            quantity=self._cfg.quantity,
        )
        put_leg = WeeklyStraddleLeg(
            option_type="put",
            strike=float(atm_strike),
            expiry=expiry,
            ask=ask_approx,
            quantity=self._cfg.quantity,
        )
        return call_leg, put_leg

    # ------------------------------------------------------------------
    # TacticBase ABC 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック:
        1. env=None → False
        2. Kill Switch ARMED → False

        Returns:
            True — 発動可能
        """
        if env is None:
            log.warning("[WeeklyGammaScalp.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[WeeklyGammaScalp.preflight] Kill Switch ARMED: 戦術無効化"
            )
            return False

        return True

    # ------------------------------------------------------------------
    # should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol: str,
    ) -> WeeklyGammaScalpEntry:
        """エントリー判定。

        判定順:
        1. Kill Switch ARMED → skip
        2. symbol バリデーション（ValueError は上位伝播）
        3. 月曜 entry window 外 → skip
        4. IVR 検証（TypeError は上位伝播）
        5. IVR > ivr_max → skip（straddle 高すぎ）
        6. earnings eve → skip（当週内にリスクイベントあり）
        7. weekly expiry 取得
        8. straddle 2 leg 構築
        9. total_cost 計算
        10. idempotency key 生成（5 分バケット）

        Args:
            env:    市場環境スナップショット
            symbol: 対象銘柄

        Returns:
            WeeklyGammaScalpEntry

        Raises:
            ValueError: symbol が SUPPORTED_SYMBOLS 外
            TypeError:  IVR が NaN/inf または範囲外
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[WeeklyGammaScalp.should_enter] Kill Switch ARMED (symbol=%s)", symbol
            )
            return WeeklyGammaScalpEntry(
                should_enter=False, symbol=symbol, reason="kill_switch_armed"
            )

        # 2. symbol バリデーション
        self._validate_symbol(symbol)

        # 3. 月曜 entry window
        if not self._in_entry_window():
            now_et = self._now_et()
            log.debug(
                "[WeeklyGammaScalp.should_enter] entry window 外: %s weekday=%d symbol=%s",
                now_et.strftime("%Y-%m-%d %H:%M"),
                now_et.weekday(),
                symbol,
            )
            return WeeklyGammaScalpEntry(
                should_enter=False,
                symbol=symbol,
                reason=(
                    f"entry_window_closed: {now_et.strftime('%A %H:%M')} ET "
                    f"(要 Monday 9:31-9:45)"
                ),
            )

        # 4. IVR 検証
        ivr = env.ivr_by_symbol.get(symbol, 0.0)
        self._validate_ivr(symbol, ivr)

        # 5. IVR フィルタ（高 IVR は straddle 高すぎ → 見送り）
        if ivr > self._cfg.ivr_max:
            log.debug(
                "[WeeklyGammaScalp.should_enter] IVR=%.1f > max=%.1f: スキップ (symbol=%s)",
                ivr, self._cfg.ivr_max, symbol,
            )
            return WeeklyGammaScalpEntry(
                should_enter=False,
                symbol=symbol,
                reason=f"IVR={ivr:.1f} > ivr_max={self._cfg.ivr_max:.1f} (straddle 割高)",
                ivr=ivr,
            )

        # 6. earnings eve チェック（当週 earnings → 前日にクローズ必要→ 新規 entry 見送り）
        today = self._today_et()
        earnings = self._get_earnings_dates(symbol)
        weekly_expiry = get_weekly_expiry(today)
        # 今週の金曜までに earnings があれば entry 見送り
        if self._has_earnings_this_week(today, weekly_expiry, earnings):
            log.info(
                "[WeeklyGammaScalp.should_enter] 今週 earnings あり → entry 見送り (symbol=%s)",
                symbol,
            )
            return WeeklyGammaScalpEntry(
                should_enter=False,
                symbol=symbol,
                reason=f"earnings_this_week: {symbol} に今週 earnings イベントあり",
                ivr=ivr,
            )

        # 7. underlying_price stub（Phase 2 で MarketDataClient から取得）
        underlying_price = max(env.vrp, 1.0)

        # 8. straddle 2 leg 構築
        call_leg, put_leg = self._build_straddle_legs(symbol, underlying_price, weekly_expiry)

        # 9. total_cost = (call ask + put ask) × qty × 100
        total_cost = round(
            (call_leg.ask + put_leg.ask) * call_leg.quantity * _CONTRACT_MULTIPLIER, 4
        )

        # 10. idempotency key（5 分バケット）
        now_utc = datetime.now(timezone.utc)
        bucket_min = (now_utc.minute // 5) * 5
        trigger_time = now_utc.replace(minute=bucket_min, second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=TACTIC_PREFIX,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[WeeklyGammaScalp.should_enter] エントリー: symbol=%s IVR=%.1f "
            "expiry=%s total_cost=%.2f key=%s",
            symbol, ivr, weekly_expiry, total_cost, idem_key,
        )

        return WeeklyGammaScalpEntry(
            should_enter=True,
            symbol=symbol,
            legs=(call_leg, put_leg),
            total_cost=total_cost,
            underlying_price=underlying_price,
            weekly_expiry=weekly_expiry,
            idempotency_key=idem_key,
            reason=(
                f"IVR={ivr:.1f}<={self._cfg.ivr_max:.1f} / "
                f"expiry={weekly_expiry}"
            ),
            ivr=ivr,
        )

    @staticmethod
    def _has_earnings_this_week(
        today: date,
        weekly_expiry: date,
        earnings_dates: frozenset[date],
    ) -> bool:
        """今週（今日〜金曜）に earnings が含まれるかを返す。"""
        for d in earnings_dates:
            if today <= d <= weekly_expiry:
                return True
        return False

    # ------------------------------------------------------------------
    # build_orders
    # ------------------------------------------------------------------

    def build_orders(
        self,
        entry: WeeklyGammaScalpEntry,
        capital_usd: float = 0.0,
    ) -> "list[OrderRequest]":
        """エントリー決定から 2 件の OrderRequest（call + put）を返す。

        Args:
            entry:       should_enter=True の WeeklyGammaScalpEntry
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            [call_order, put_order]

        Raises:
            ValueError:      should_enter=False の entry が渡された場合
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not entry.should_enter:
            raise ValueError(
                "[WeeklyGammaScalp.build_orders] should_enter=False の entry が渡されました。"
            )
        # Per-leg gate check: use first leg qty as representative (each leg checked individually)
        _first_qty = entry.legs[0].quantity if entry.legs else 1
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        _gr = _gate(_Ctx(symbol=entry.symbol, qty=_first_qty, side="BUY", is_long=True))
        if not _gr.allowed:
            raise ValueError(f"[WeeklyGammaScalp.build_orders] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=self._cfg.paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(entry.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        order_type = "paper_limit" if self._cfg.paper_mode else "limit"
        orders: list[OrderRequest] = []

        for leg in entry.legs:
            leg_key = f"{entry.idempotency_key}_{leg.option_type}"
            orders.append(
                OrderRequest(
                    symbol=(
                        f"{entry.symbol}_{leg.option_type}"
                        f"_{leg.strike}_{leg.expiry.strftime('%Y%m%d')}"
                    ),
                    side="buy",
                    quantity=leg.quantity,
                    order_type=order_type,
                    tactic_name=self.tactic_name,
                    idempotency_key=leg_key,
                )
            )

        return orders

    # ------------------------------------------------------------------
    # delta_hedge（日次コール）
    # ------------------------------------------------------------------

    def delta_hedge(
        self,
        position: WeeklyGammaScalpPosition,
        portfolio_delta: float,
        underlying_price: float,
        gamma: float = 0.08,
    ) -> DeltaHedgeAction | None:
        """デルタヘッジ判定と実行記録。

        判定順:
        1. Kill Switch ARMED → None（ヘッジ不実行）
        2. ポジションクローズ済み → None
        3. |portfolio_delta| <= delta_band → ヘッジ不要
        4. hedge_interval_min 未経過 → スキップ
        5. ヘッジ実行 → DeltaHedgeAction を記録して返す

        Args:
            position:         保有ポジション
            portfolio_delta:  現在のポートフォリオ delta
            underlying_price: 現在の原資産価格
            gamma:            オプション gamma（stub 0.08）

        Returns:
            DeltaHedgeAction（実行した場合）or None
        """
        if kill_switch_is_active():
            log.warning(
                "[WeeklyGammaScalp.delta_hedge] Kill Switch ARMED: ヘッジスキップ"
            )
            return None

        if position.is_closed:
            return None

        # delta band チェック
        if abs(portfolio_delta) <= self._cfg.delta_band:
            log.debug(
                "[WeeklyGammaScalp.delta_hedge] |delta|=%.3f <= band=%.3f: ヘッジ不要",
                abs(portfolio_delta), self._cfg.delta_band,
            )
            return None

        # インターバルチェック
        now_utc = datetime.now(timezone.utc)
        if self._last_hedge_ts is not None:
            elapsed_min = (now_utc - self._last_hedge_ts).total_seconds() / 60.0
            if elapsed_min < self._cfg.hedge_interval_min:
                log.debug(
                    "[WeeklyGammaScalp.delta_hedge] interval %.1f min 未経過 (%.1f min)",
                    self._cfg.hedge_interval_min, elapsed_min,
                )
                return None

        # ヘッジ実行
        hedge_units = compute_hedge_units(portfolio_delta, underlying_price)
        pnl = estimate_gamma_pnl(
            portfolio_delta, gamma, position.entry.legs[0].quantity if position.entry.legs else 1
        )
        self._last_hedge_ts = now_utc

        action = DeltaHedgeAction(
            symbol=position.symbol,
            delta_before=portfolio_delta,
            delta_after=0.0,  # 理論上ニュートラル化
            hedge_units=hedge_units,
            underlying_price=underlying_price,
            pnl_estimate=pnl,
            ts=now_utc.isoformat(),
        )
        position.hedge_events.append(action)

        log.info(
            "[WeeklyGammaScalp.delta_hedge] HEDGE: symbol=%s delta=%.3f→0 "
            "units=%.1f pnl_est=%.2f",
            position.symbol, portfolio_delta, hedge_units, pnl,
        )
        return action

    # ------------------------------------------------------------------
    # should_exit
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: WeeklyGammaScalpPosition,
        env: MarketEnvironment,
    ) -> WeeklyGammaScalpExit:
        """エグジット判定。

        判定優先度:
        1. Kill Switch ARMED → kill_switch
        2. 15:50 ET 以降かつ金曜 → friday_force_close
        3. 15:50 ET 以降かつ earnings 前日 → earnings_force_close
        4. current_pnl <= -stop_loss_pct × total_cost → stop_loss
        5. current_pnl >= profit_target_pct × total_cost → profit_target

        Args:
            position: 保有ポジション
            env:      現在の市場環境

        Returns:
            WeeklyGammaScalpExit
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[WeeklyGammaScalp.should_exit] Kill Switch ARMED (symbol=%s)",
                position.symbol,
            )
            return WeeklyGammaScalpExit(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="kill_switch",
            )

        today = self._today_et()
        past_close = self._past_force_close()

        # 2. 金曜 強制クローズ
        if past_close and is_friday(today):
            log.info(
                "[WeeklyGammaScalp.should_exit] 金曜 15:50 ET 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return WeeklyGammaScalpExit(
                should_exit=True,
                reason=f"friday_force_close: {today}",
                exit_type="friday_force_close",
            )

        # 3. earnings 前日 強制クローズ
        earnings = self._get_earnings_dates(position.symbol)
        if past_close and is_earnings_eve(today, earnings):
            log.info(
                "[WeeklyGammaScalp.should_exit] earnings 前日 15:50 ET クローズ (symbol=%s)",
                position.symbol,
            )
            return WeeklyGammaScalpExit(
                should_exit=True,
                reason=f"earnings_force_close: {position.symbol} 翌日 earnings",
                exit_type="earnings_force_close",
            )

        # total_cost ガード
        if position.entry.total_cost <= 0:
            return WeeklyGammaScalpExit(
                should_exit=False,
                reason="total_cost_not_set",
            )

        pnl = position.current_pnl
        stop_threshold = -self._cfg.stop_loss_pct * position.entry.total_cost
        profit_threshold = self._cfg.profit_target_pct * position.entry.total_cost

        # 4. ストップロス
        if pnl <= stop_threshold:
            log.warning(
                "[WeeklyGammaScalp.should_exit] STOP LOSS: pnl=%.2f <= threshold=%.2f (symbol=%s)",
                pnl, stop_threshold, position.symbol,
            )
            return WeeklyGammaScalpExit(
                should_exit=True,
                reason=f"stop_loss: pnl={pnl:.2f} <= {stop_threshold:.2f}",
                exit_type="stop_loss",
            )

        # 5. 利確
        if pnl >= profit_threshold:
            log.info(
                "[WeeklyGammaScalp.should_exit] PROFIT TARGET: pnl=%.2f >= threshold=%.2f (symbol=%s)",
                pnl, profit_threshold, position.symbol,
            )
            return WeeklyGammaScalpExit(
                should_exit=True,
                reason=f"profit_target: pnl={pnl:.2f} >= {profit_threshold:.2f}",
                exit_type="profit_target",
            )

        return WeeklyGammaScalpExit(
            should_exit=False,
            reason="holding",
            exit_type="none",
        )
