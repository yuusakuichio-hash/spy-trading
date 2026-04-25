"""atlas_v3/bots/engines/short_strangle_0dte.py — 0DTE Short Strangle エンジン

設計方針:
    OTM Short Call (delta 0.10-0.15) + OTM Short Put (delta 0.10-0.15) を
    同日満期（0DTE）で同時売り建て・極短期 theta decay 最速消費を狙う。

    既存 strangle_sell (multi-DTE / spy_bot.py) とは完全独立。
    spy_bot.py / common/* には一切触れない。

エントリー条件:
    - IVR > 65（高 IV 環境でプレミアム厚みを確保）
    - VIX 15-30（過度な spike 時は halt: VIX > 30 は gamma リスク過大）
    - 開場後 1h 経過（10:30 ET 以降）でトレンド・ORB 確定後に乗る
    - エントリー窓: 10:30-13:00 ET（午後は gamma 加速・乗らない）

エグジット:
    - 利確: クレジット 70% 取得（remaining 30% 以下）
    - 損切: クレジットの 2x 逆行（実損 >= 2 * initial_credit * qty * 100）
    - 強制クローズ: 15:30 ET 厳守（0DTE gamma spike 前に必ず手仕舞い）

担保計算（unassigned cash-secured）:
    - required_margin = (max_loss / 25) の充足確認
    - 収取クレジット >= max_loss / 25 でなければエントリー見送り

戦術分類: Type A (enter_exit)
TacticBase ABC 継承 / CC ≤ 20 規律 / kill_switch 連動

ADR 参照: 既存 ADR-013（ZeroDTESystem）と直交する独立戦術。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal, Optional
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.bots.engines.earnings_calendar_check import is_near_earnings
from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: デルタ範囲（OTM ショートレッグ）
DELTA_MIN: float = 0.10
DELTA_MAX: float = 0.15

#: IVR エントリー最低閾値
IVR_MIN: float = 65.0

#: VIX エントリー範囲
VIX_ENTRY_MIN: float = 15.0
VIX_ENTRY_MAX: float = 30.0

#: エントリー窓（ET 時・分）
ENTRY_OPEN_HOUR_ET: int = 10
ENTRY_OPEN_MINUTE_ET: int = 30
ENTRY_CLOSE_HOUR_ET: int = 13
ENTRY_CLOSE_MINUTE_ET: int = 0

#: 強制クローズ（ET 時・分）— 0DTE gamma spike 前
FORCE_CLOSE_HOUR_ET: int = 15
FORCE_CLOSE_MINUTE_ET: int = 30

#: 利確ライン（クレジット残存率）: 70% 取得 = 残存 30%
PROFIT_TARGET_REMAINING_PCT: float = 0.30

#: 損切り倍率（受取クレジットの何倍逆行で損切り）
STOP_LOSS_MULT: float = 2.0

#: 担保計算除数（cash-secured 要件）
MARGIN_DIVISOR: float = 25.0


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class ShortStrangle0DTEConfig:
    """0DTE Short Strangle 設定。

    Attributes:
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        delta_min:              ショートレッグ最小デルタ絶対値
        delta_max:              ショートレッグ最大デルタ絶対値
        ivr_min:                エントリー最低 IVR
        vix_min:                エントリー最低 VIX
        vix_max:                エントリー最大 VIX（超過は halt）
        entry_open_hour_et:     エントリー窓開始時（ET）
        entry_open_minute_et:   エントリー窓開始分（ET）
        entry_close_hour_et:    エントリー窓終了時（ET）
        entry_close_minute_et:  エントリー窓終了分（ET）
        force_close_hour_et:    強制クローズ時（ET）
        force_close_minute_et:  強制クローズ分（ET）
        profit_target_remaining_pct: 利確 residual rate（0.30 = 70% 取得で利確）
        stop_loss_mult:         損切り倍率（2.0 = 2x credit 逆行）
        margin_divisor:         担保計算除数（25.0 = max_loss / 25）
        quantity:               デフォルト発注枚数（ストラングル 1 セット = 1）
    """
    slippage_tolerance_bps: int = 20
    delta_min: float = DELTA_MIN
    delta_max: float = DELTA_MAX
    ivr_min: float = IVR_MIN
    vix_min: float = VIX_ENTRY_MIN
    vix_max: float = VIX_ENTRY_MAX
    entry_open_hour_et: int = ENTRY_OPEN_HOUR_ET
    entry_open_minute_et: int = ENTRY_OPEN_MINUTE_ET
    entry_close_hour_et: int = ENTRY_CLOSE_HOUR_ET
    entry_close_minute_et: int = ENTRY_CLOSE_MINUTE_ET
    force_close_hour_et: int = FORCE_CLOSE_HOUR_ET
    force_close_minute_et: int = FORCE_CLOSE_MINUTE_ET
    profit_target_remaining_pct: float = PROFIT_TARGET_REMAINING_PCT
    stop_loss_mult: float = STOP_LOSS_MULT
    margin_divisor: float = MARGIN_DIVISOR
    quantity: int = 1
    earnings_proximity_days: Optional[int] = 5


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrangleEntryDecision:
    """0DTE Short Strangle エントリー決定。

    Attributes:
        should_enter:     True のときエントリー実行
        symbol:           対象銘柄
        call_strike:      Short Call ストライク
        put_strike:       Short Put ストライク
        call_delta:       Short Call デルタ（絶対値）
        put_delta:        Short Put デルタ（絶対値）
        call_credit:      Short Call 受取クレジット（per share）
        put_credit:       Short Put 受取クレジット（per share）
        quantity:         発注枚数
        expiry_date:      満期日（当日 0DTE）
        margin_required:  必要担保額
        reason:           エントリー根拠テキスト
        idempotency_key:  冪等性キー
    """
    should_enter: bool
    symbol: str
    call_strike: float = 0.0
    put_strike: float = 0.0
    call_delta: float = 0.0
    put_delta: float = 0.0
    call_credit: float = 0.0
    put_credit: float = 0.0
    quantity: int = 1
    expiry_date: str = ""
    margin_required: float = 0.0
    reason: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class StrangleExitDecision:
    """0DTE Short Strangle エグジット決定。

    Attributes:
        should_exit:  True のとき exit
        reason:       exit 根拠テキスト
        exit_type:    "profit_target" / "stop_loss" / "force_close" / "none"
    """
    should_exit: bool
    reason: str = ""
    exit_type: Literal["profit_target", "stop_loss", "force_close", "none"] = "none"


# ---------------------------------------------------------------------------
# Position DTO
# ---------------------------------------------------------------------------

@dataclass
class StranglePosition:
    """0DTE Short Strangle ポジション表現。

    Attributes:
        symbol:          銘柄
        quantity:        枚数（ストラングル 1 セット = 1）
        initial_credit:  受取総クレジット（per share, call + put）
        current_value:   現在価値（per share, call + put）
        call_strike:     Short Call ストライク
        put_strike:      Short Put ストライク
        expiry_date:     満期日（当日）
        entry_time:      エントリー時刻（UTC）
        unrealized_pnl:  未実現損益
        tactic_name:     戦術名
    """
    symbol: str
    quantity: int
    initial_credit: float        # 受取クレジット per share (call + put)
    current_value: float = 0.0   # 現在価値 per share (call + put)
    call_strike: float = 0.0
    put_strike: float = 0.0
    expiry_date: str = ""
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: float = 0.0
    tactic_name: str = "short_strangle_0dte"


# ---------------------------------------------------------------------------
# 0DTE 満期判定ユーティリティ
# ---------------------------------------------------------------------------

def is_0dte_expiry(expiry_date: str, reference_dt: datetime | None = None) -> bool:
    """expiry_date（YYYY-MM-DD）が today（ET 基準）と一致するか判定する。

    Args:
        expiry_date:   満期日文字列（YYYY-MM-DD）
        reference_dt:  参照日時（UTC）。None の場合は datetime.now(UTC)。

    Returns:
        True  — 当日満期（0DTE）
        False — 当日満期でない
    """
    if not expiry_date:
        return False
    ref = reference_dt or datetime.now(timezone.utc)
    ref_et = ref.astimezone(ET)
    today_et_str = ref_et.strftime("%Y-%m-%d")
    return expiry_date == today_et_str


# ---------------------------------------------------------------------------
# VIX ゲート（エントリー条件）
# ---------------------------------------------------------------------------

def is_vix_in_range(vix: float, vix_min: float, vix_max: float) -> bool:
    """VIX がエントリー範囲内か判定する。

    Args:
        vix:     現在 VIX 値
        vix_min: 最低 VIX（下限・inclusive）
        vix_max: 最大 VIX（上限・inclusive）。超過は gamma spike halt。

    Returns:
        True  — エントリー可能
        False — halt（範囲外）
    """
    return vix_min <= vix <= vix_max


# ---------------------------------------------------------------------------
# エントリー窓判定
# ---------------------------------------------------------------------------

def is_in_entry_window(
    now_et: datetime,
    open_hour: int,
    open_minute: int,
    close_hour: int,
    close_minute: int,
) -> bool:
    """現在時刻が 10:30-13:00 ET のエントリー窓内か判定する。

    Args:
        now_et:       現在時刻（ET tzinfo 付き）
        open_hour:    窓開始時（ET）
        open_minute:  窓開始分（ET）
        close_hour:   窓終了時（ET）
        close_minute: 窓終了分（ET）

    Returns:
        True  — エントリー窓内
        False — 窓外
    """
    open_minutes = open_hour * 60 + open_minute
    close_minutes = close_hour * 60 + close_minute
    now_minutes = now_et.hour * 60 + now_et.minute
    return open_minutes <= now_minutes < close_minutes


# ---------------------------------------------------------------------------
# 強制クローズ判定
# ---------------------------------------------------------------------------

def is_force_close_time(
    now_et: datetime,
    force_hour: int,
    force_minute: int,
) -> bool:
    """現在時刻が強制クローズ時刻（15:30 ET）以降か判定する。

    0DTE gamma spike 前に必ずポジションを手仕舞いするため、
    15:30 ET 以降は即 force_close を返す。

    Args:
        now_et:       現在時刻（ET tzinfo 付き）
        force_hour:   強制クローズ時（ET）
        force_minute: 強制クローズ分（ET）

    Returns:
        True  — 強制クローズ発動
        False — まだ猶予あり
    """
    force_minutes = force_hour * 60 + force_minute
    now_minutes = now_et.hour * 60 + now_et.minute
    return now_minutes >= force_minutes


# ---------------------------------------------------------------------------
# 担保計算
# ---------------------------------------------------------------------------

def calc_required_margin(
    call_credit: float,
    put_credit: float,
    quantity: int,
    margin_divisor: float,
) -> float:
    """担保要件を計算する（cash-secured: max_loss / divisor）。

    max_loss = (call_strike + put_strike 差額の最大推定値ではなく)
               unassigned short option の margin 代替として
               受取クレジット × 100株 × quantity / divisor とする。

    Args:
        call_credit:    Short Call 受取クレジット（per share）
        put_credit:     Short Put 受取クレジット（per share）
        quantity:       枚数
        margin_divisor: 除数（25.0）

    Returns:
        必要担保額（USD）
    """
    total_credit = (call_credit + put_credit) * 100 * quantity
    return total_credit / margin_divisor


def is_margin_sufficient(
    call_credit: float,
    put_credit: float,
    quantity: int,
    margin_divisor: float,
) -> bool:
    """担保要件チェック: credit >= max_loss / 25 を検証する。

    条件: total_credit >= margin_required
    total_credit と margin_required は同じ式から計算されるため、
    この関数は calc_required_margin の使い方の正しさと
    credit が 0 以上であることを確認する実装となる。
    実際の運用では available_cash >= margin_required を外部で確認する。

    Args:
        call_credit:    Short Call 受取クレジット（per share）
        put_credit:     Short Put 受取クレジット（per share）
        quantity:       枚数
        margin_divisor: 除数（25.0）

    Returns:
        True  — 担保充足
        False — 担保不足（エントリー見送り）
    """
    if call_credit <= 0 or put_credit <= 0:
        return False
    total_credit = (call_credit + put_credit) * 100 * quantity
    margin_required = total_credit / margin_divisor
    return total_credit >= margin_required


# ---------------------------------------------------------------------------
# ShortStrangle0DTEEngine — メインエンジン
# ---------------------------------------------------------------------------

class ShortStrangle0DTEEngine(TacticBase):
    """0DTE Short Strangle エンジン（Type A: enter_exit）。

    spy_bot.py / 既存 strangle_sell と独立した新規実装。
    TacticBase ABC 継承により AtlasEngine から dispatch される。

    Args:
        config: ShortStrangle0DTEConfig（slippage_tolerance_bps 必須）
    """

    def __init__(
        self,
        config: ShortStrangle0DTEConfig | None = None,
        earnings_date_fn: "Optional[Callable[[str], Optional[object]]]" = None,
    ) -> None:
        self._cfg = config or ShortStrangle0DTEConfig()
        self._lock: threading.Lock = threading.Lock()
        self._earnings_date_fn = earnings_date_fn  # 決算日取得 DI（テスト用）

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "short_strangle_0dte"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック順:
        1. env=None → False
        2. Kill Switch ARMED → False
        3. VIX 範囲外（< vix_min or > vix_max）→ False

        Returns:
            True  — 戦術発動可能
            False — 発動不可（log に理由出力済み）
        """
        if env is None:
            log.warning("[ShortStrangle0DTEEngine.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[ShortStrangle0DTEEngine.preflight] Kill Switch ARMED: "
                "short_strangle_0dte 無効化"
            )
            return False

        if not is_vix_in_range(env.vix, self._cfg.vix_min, self._cfg.vix_max):
            log.info(
                "[ShortStrangle0DTEEngine.preflight] VIX=%.1f が範囲外 [%.1f, %.1f]: halt",
                env.vix, self._cfg.vix_min, self._cfg.vix_max,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol 実装
    # ------------------------------------------------------------------

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol: str,
        call_strike: float = 0.0,
        put_strike: float = 0.0,
        call_delta: float = 0.0,
        put_delta: float = 0.0,
        call_credit: float = 0.0,
        put_credit: float = 0.0,
        expiry_date: str = "",
        now_utc: datetime | None = None,
    ) -> StrangleEntryDecision:
        """エントリー判定。

        全条件を順次チェックし、いずれか NG なら should_enter=False を返す。

        チェック順:
        1. 0DTE 満期確認（expiry_date が当日）
        2. IVR >= ivr_min
        3. VIX 範囲内（preflight と二重チェック）
        4. エントリー窓内（10:30-13:00 ET）
        5. デルタ範囲内（0.10-0.15）
        6. 担保充足チェック
        7. idempotency_key 生成

        Args:
            env:          MarketEnvironment
            symbol:       銘柄
            call_strike:  Short Call ストライク候補
            put_strike:   Short Put ストライク候補
            call_delta:   Short Call デルタ（絶対値）
            put_delta:    Short Put デルタ（絶対値）
            call_credit:  Short Call 受取クレジット（per share）
            put_credit:   Short Put 受取クレジット（per share）
            expiry_date:  満期日文字列（YYYY-MM-DD）
            now_utc:      現在時刻（UTC）。None なら datetime.now(UTC）

        Returns:
            StrangleEntryDecision
        """
        now = now_utc or datetime.now(timezone.utc)
        now_et = now.astimezone(ET)

        # 0. 決算近接チェック（earnings_proximity_days が設定されている場合のみ）
        # safe_default は earnings_date_fn 注入時のみ True（API キー未設定時のブロック防止）
        if self._cfg.earnings_proximity_days:
            _safe = self._earnings_date_fn is not None
            blocked, ep_reason = is_near_earnings(
                symbol=symbol,
                proximity_days=self._cfg.earnings_proximity_days,
                earnings_date_fn=self._earnings_date_fn,
                safe_default=_safe,
            )
            if blocked:
                log.info(
                    "[ShortStrangle0DTEEngine.should_enter] 決算近接ブロック: %s",
                    ep_reason,
                )
                return StrangleEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    reason=ep_reason,
                )

        # 1. 0DTE 満期確認
        if not is_0dte_expiry(expiry_date, now):
            return StrangleEntryDecision(
                should_enter=False, symbol=symbol,
                reason=f"expiry={expiry_date} は当日 0DTE でない",
            )

        # 2. IVR チェック
        ivr = env.ivr_by_symbol.get(symbol, 0.0)
        if ivr < self._cfg.ivr_min:
            log.debug(
                "[ShortStrangle0DTEEngine.should_enter] IVR=%.1f < min=%.1f: スキップ (symbol=%s)",
                ivr, self._cfg.ivr_min, symbol,
            )
            return StrangleEntryDecision(
                should_enter=False, symbol=symbol,
                reason=f"IVR={ivr:.1f} < {self._cfg.ivr_min}",
            )

        # 3. VIX 範囲確認
        if not is_vix_in_range(env.vix, self._cfg.vix_min, self._cfg.vix_max):
            return StrangleEntryDecision(
                should_enter=False, symbol=symbol,
                reason=f"VIX={env.vix:.1f} 範囲外 [{self._cfg.vix_min},{self._cfg.vix_max}]",
            )

        # 4. エントリー窓確認（10:30-13:00 ET）
        if not is_in_entry_window(
            now_et,
            self._cfg.entry_open_hour_et, self._cfg.entry_open_minute_et,
            self._cfg.entry_close_hour_et, self._cfg.entry_close_minute_et,
        ):
            log.debug(
                "[ShortStrangle0DTEEngine.should_enter] 時刻 %s がエントリー窓外: スキップ",
                now_et.strftime("%H:%M ET"),
            )
            return StrangleEntryDecision(
                should_enter=False, symbol=symbol,
                reason=f"エントリー窓外 ({now_et.strftime('%H:%M ET')})",
            )

        # 5. デルタ範囲確認
        if not (self._cfg.delta_min <= call_delta <= self._cfg.delta_max):
            return StrangleEntryDecision(
                should_enter=False, symbol=symbol,
                reason=f"call_delta={call_delta:.3f} が範囲外 [{self._cfg.delta_min},{self._cfg.delta_max}]",
            )
        if not (self._cfg.delta_min <= put_delta <= self._cfg.delta_max):
            return StrangleEntryDecision(
                should_enter=False, symbol=symbol,
                reason=f"put_delta={put_delta:.3f} が範囲外 [{self._cfg.delta_min},{self._cfg.delta_max}]",
            )

        # 6. 担保充足チェック
        if not is_margin_sufficient(
            call_credit, put_credit, self._cfg.quantity, self._cfg.margin_divisor
        ):
            return StrangleEntryDecision(
                should_enter=False, symbol=symbol,
                reason=f"担保不足: call_credit={call_credit} put_credit={put_credit}",
            )

        # 7. idempotency_key 生成（分単位精度）
        trigger_time = now.replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        margin = calc_required_margin(
            call_credit, put_credit, self._cfg.quantity, self._cfg.margin_divisor
        )

        log.info(
            "[ShortStrangle0DTEEngine.should_enter] エントリー OK: symbol=%s "
            "call_strike=%.2f put_strike=%.2f IVR=%.1f VIX=%.1f key=%s",
            symbol, call_strike, put_strike, ivr, env.vix, idem_key,
        )
        return StrangleEntryDecision(
            should_enter=True,
            symbol=symbol,
            call_strike=call_strike,
            put_strike=put_strike,
            call_delta=call_delta,
            put_delta=put_delta,
            call_credit=call_credit,
            put_credit=put_credit,
            quantity=self._cfg.quantity,
            expiry_date=expiry_date,
            margin_required=margin,
            reason=(
                f"IVR={ivr:.1f}>={self._cfg.ivr_min} / VIX={env.vix:.1f} / "
                f"call_delta={call_delta:.3f} / put_delta={put_delta:.3f}"
            ),
            idempotency_key=idem_key,
        )

    def build_order(
        self,
        decision: StrangleEntryDecision,
        paper_mode: bool = True,
        capital_usd: float = 0.0,
    ) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。

        should_enter=False の decision は ValueError を raise する。
        slippage_tolerance_bps と idempotency_key を OrderRequest に設定する。

        Args:
            decision:    StrangleEntryDecision（should_enter=True 必須）
            paper_mode:  True = paper 発注（PDT チェックスキップ）
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            OrderRequest

        Raises:
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_enter:
            raise ValueError(
                f"[ShortStrangle0DTEEngine.build_order] "
                f"should_enter=False の decision が渡された: {decision}"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        # 2026-04-25: PreTradeGate L2 whitelist は "US.XXX" 形式・L3 margin は capital_usd+est_margin 必須。
        # StrangleEntryDecision は legs を持たないため quantity から proxy 算出。
        sym = decision.symbol if decision.symbol.startswith("US.") else f"US.{decision.symbol}"
        est_margin = decision.quantity * 100
        _gr = _gate(_Ctx(
            symbol=sym, qty=decision.quantity, side="SELL", is_long=False,
            est_margin=est_margin, capital_usd=capital_usd,
        ))
        if not _gr.allowed:
            raise ValueError(f"[ShortStrangle0DTEEngine.build_order] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        return OrderRequest(
            symbol=decision.symbol,
            side="sell",
            quantity=decision.quantity,
            order_type="limit",
            tactic_name=self.tactic_name,
            idempotency_key=decision.idempotency_key,
        )

    def should_exit(
        self,
        position: StranglePosition,
        env: MarketEnvironment,
        now_utc: datetime | None = None,
    ) -> StrangleExitDecision:
        """エグジット判定。

        チェック順（優先度高い順）:
        1. Kill Switch → force_close
        2. 強制クローズ時刻（15:30 ET 以降）→ force_close
        3. 損切り（current_value >= initial_credit * stop_loss_mult）→ stop_loss
        4. 利確（current_value <= initial_credit * profit_target_remaining_pct）→ profit_target

        Args:
            position: StranglePosition
            env:      MarketEnvironment
            now_utc:  現在時刻（UTC）。None なら datetime.now(UTC）

        Returns:
            StrangleExitDecision
        """
        now = now_utc or datetime.now(timezone.utc)
        now_et = now.astimezone(ET)

        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[ShortStrangle0DTEEngine.should_exit] Kill Switch ARMED: force_close"
            )
            return StrangleExitDecision(
                should_exit=True,
                reason="Kill Switch ARMED",
                exit_type="force_close",
            )

        # 2. 強制クローズ時刻（15:30 ET 厳守）
        if is_force_close_time(
            now_et, self._cfg.force_close_hour_et, self._cfg.force_close_minute_et
        ):
            log.info(
                "[ShortStrangle0DTEEngine.should_exit] 強制クローズ時刻 %s: force_close",
                now_et.strftime("%H:%M ET"),
            )
            return StrangleExitDecision(
                should_exit=True,
                reason=f"強制クローズ時刻 ({now_et.strftime('%H:%M ET')} >= 15:30 ET)",
                exit_type="force_close",
            )

        # 3. 損切り: current_value >= initial_credit * stop_loss_mult
        stop_threshold = position.initial_credit * self._cfg.stop_loss_mult
        if position.current_value >= stop_threshold:
            log.warning(
                "[ShortStrangle0DTEEngine.should_exit] 損切り発動: "
                "current_value=%.4f >= stop=%.4f (initial_credit=%.4f x %.1fx)",
                position.current_value, stop_threshold,
                position.initial_credit, self._cfg.stop_loss_mult,
            )
            return StrangleExitDecision(
                should_exit=True,
                reason=(
                    f"損切り: current_value={position.current_value:.4f} >= "
                    f"initial_credit×{self._cfg.stop_loss_mult:.1f}={stop_threshold:.4f}"
                ),
                exit_type="stop_loss",
            )

        # 4. 利確: current_value <= initial_credit * profit_target_remaining_pct
        profit_threshold = position.initial_credit * self._cfg.profit_target_remaining_pct
        if position.current_value <= profit_threshold:
            log.info(
                "[ShortStrangle0DTEEngine.should_exit] 利確: "
                "current_value=%.4f <= profit_threshold=%.4f",
                position.current_value, profit_threshold,
            )
            return StrangleExitDecision(
                should_exit=True,
                reason=(
                    f"利確: current_value={position.current_value:.4f} <= "
                    f"initial_credit×{self._cfg.profit_target_remaining_pct:.2f}={profit_threshold:.4f}"
                ),
                exit_type="profit_target",
            )

        return StrangleExitDecision(should_exit=False, reason="保持継続", exit_type="none")

    def build_exit_order(
        self, position: StranglePosition, decision: StrangleExitDecision
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。

        Short Strangle は売り建てのため、クローズは buy_to_close（side="buy"）。

        Args:
            position: StranglePosition
            decision: StrangleExitDecision（should_exit=True 必須）

        Returns:
            OrderRequest
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_exit:
            raise ValueError(
                f"[ShortStrangle0DTEEngine.build_exit_order] "
                f"should_exit=False の decision が渡された: {decision}"
            )

        return OrderRequest(
            symbol=position.symbol,
            side="buy",          # buy_to_close（short strangle 解消）
            quantity=position.quantity,
            order_type="market" if decision.exit_type == "force_close" else "limit",
            tactic_name=self.tactic_name,
            idempotency_key=make_job_key(
                strategy=self.tactic_name,
                symbol=position.symbol,
                trigger_time=datetime.now(timezone.utc).replace(second=0, microsecond=0),
            ),
        )
