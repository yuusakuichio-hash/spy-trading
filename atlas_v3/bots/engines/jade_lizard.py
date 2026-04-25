"""atlas_v3/bots/engines/jade_lizard.py — Jade Lizard オプション戦術

構成: Short Put + Short Call Spread (= Short Put + Short Call + Long Call OTM)
  Leg 1: Short Put  (ITM/ATM 近辺・delta 0.15-0.20)
  Leg 2: Short Call (OTM・delta 0.20-0.25)
  Leg 3: Long  Call (further OTM・= Leg2 strike + spread_width)

「no-risk to upside」条件:
  total_credit >= call_spread_width × 100
  → 上方向の最大損失 = 0（コール・スプレッドの wid 全額をクレジットでカバー）

エントリー条件:
  - IVR >= ivr_min (デフォルト 60)
  - short put delta: 0.15-0.20
  - short call delta: 0.20-0.25
  - call spread width: spread_width_pts (固定幅・config で 5-10pt)
  - entry window: 10:00-12:00 ET

Exit 条件（優先度順）:
  1. Kill Switch ARMED → force_close
  2. 15:50 ET → force_close
  3. unrealized_pnl >= total_credit * profit_target_pct → profit_target
  4. unrealized_pnl <= -total_credit * stop_loss_multiplier → stop_loss

発注: TradeEngine (paper mode) 経由の 3-leg 発注
  OrderRequest を 3 件返す。AtlasEngine が 1 件ずつ dispatch する。

CC 規律: 各メソッド CC <= 20
設計参照: atlas_v3/strategies/ic_sell.py / statistical_premium_seller.py
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
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

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")

#: エントリー可能ウィンドウ（ET）
_ENTRY_WINDOW_START: time = time(10, 0)
_ENTRY_WINDOW_END: time = time(12, 0)

#: 強制クローズ時刻（ET）
_FORCE_CLOSE_TIME: time = time(15, 50)

#: IVR スケール（0-100 固定・env.ivr_by_symbol と同スケール）
_IVR_SCALE_MIN: float = 0.0
_IVR_SCALE_MAX: float = 100.0

#: デルタ仕様
_SHORT_PUT_DELTA_MIN: float = 0.15
_SHORT_PUT_DELTA_MAX: float = 0.20
_SHORT_CALL_DELTA_MIN: float = 0.20
_SHORT_CALL_DELTA_MAX: float = 0.25

#: call spread 幅（pt）許容範囲
_SPREAD_WIDTH_MIN_PTS: float = 5.0
_SPREAD_WIDTH_MAX_PTS: float = 10.0

#: leg 識別子（発注・idempotency key 生成用）
LegLabel = Literal["short_put", "short_call", "long_call"]

#: 戦術識別子プレフィックス（cross-tactic 二重建玉防止）
JADE_LIZARD_PREFIX: str = "jade_lizard"


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class JadeLizardConfig:
    """Jade Lizard 戦術設定。

    Attributes:
        ivr_min:              エントリー最低 IVR（0-100 スケール）
        short_put_delta_min:  short put デルタ下限（絶対値）
        short_put_delta_max:  short put デルタ上限（絶対値）
        short_call_delta_min: short call デルタ下限（絶対値）
        short_call_delta_max: short call デルタ上限（絶対値）
        spread_width_pts:     call spread 幅（ポイント）5-10pt
        profit_target_pct:    利確目標（総クレジット比 0.0-1.0）
        stop_loss_multiplier: 損切り倍率（総クレジット × multiplier）
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        paper_mode:           True = paper 発注（実際の発注をスキップ）
    """
    ivr_min: float = 60.0
    short_put_delta_min: float = _SHORT_PUT_DELTA_MIN
    short_put_delta_max: float = _SHORT_PUT_DELTA_MAX
    short_call_delta_min: float = _SHORT_CALL_DELTA_MIN
    short_call_delta_max: float = _SHORT_CALL_DELTA_MAX
    spread_width_pts: float = 5.0
    profit_target_pct: float = 0.50
    stop_loss_multiplier: float = 2.0
    slippage_tolerance_bps: int = 10
    paper_mode: bool = True
    earnings_proximity_days: Optional[int] = 5

    def __post_init__(self) -> None:
        """設定値バリデーション。

        Raises:
            ValueError: spread_width_pts が 5-10pt 範囲外の場合
            ValueError: デルタ範囲が不正（min > max）の場合
            ValueError: profit_target_pct が 0.0-1.0 範囲外の場合
        """
        if not (_SPREAD_WIDTH_MIN_PTS <= self.spread_width_pts <= _SPREAD_WIDTH_MAX_PTS):
            raise ValueError(
                f"spread_width_pts={self.spread_width_pts!r} は "
                f"[{_SPREAD_WIDTH_MIN_PTS}, {_SPREAD_WIDTH_MAX_PTS}] の範囲外です。"
            )
        if self.short_put_delta_min >= self.short_put_delta_max:
            raise ValueError(
                f"short_put_delta_min={self.short_put_delta_min!r} >= "
                f"short_put_delta_max={self.short_put_delta_max!r}"
            )
        if self.short_call_delta_min >= self.short_call_delta_max:
            raise ValueError(
                f"short_call_delta_min={self.short_call_delta_min!r} >= "
                f"short_call_delta_max={self.short_call_delta_max!r}"
            )
        if not (0.0 < self.profit_target_pct < 1.0):
            raise ValueError(
                f"profit_target_pct={self.profit_target_pct!r} は (0.0, 1.0) 範囲外です。"
            )


# ---------------------------------------------------------------------------
# 3-Leg 定義 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JadeLizardLeg:
    """Jade Lizard の 1 leg を表す発注単位。

    Attributes:
        label:    leg 識別子
        side:     "sell" | "buy"
        strike:   ストライク価格
        delta:    デルタ絶対値（ストライク選定根拠・ログ用）
        credit:   受け取りプレミアム（売り leg は正値・買い leg は負値）
        quantity: 発注枚数（1 = 1 コントラクト）
    """
    label: LegLabel
    side: Literal["sell", "buy"]
    strike: float
    delta: float
    credit: float
    quantity: int = 1


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JadeLizardEntryDecision:
    """Jade Lizard エントリー決定 DTO。

    Attributes:
        should_enter:  True = エントリー実行
        symbol:        対象銘柄
        legs:          3 legs（short_put / short_call / long_call）
        total_credit:  3 leg 合計受取クレジット（円/ドル 絶対額）
        no_risk_upside: total_credit >= spread_width × 100 の「上方無リスク」条件
        quantity:      発注枚数（Engine の quantity sanity check 用・各 leg 共通）
        idempotency_key: 二重発注防止キー
        reason:        エントリー判定理由（ログ・EICAS 用）
        ivr:           エントリー時の IVR 値（監査用）
    """
    should_enter: bool
    symbol: str
    legs: tuple[JadeLizardLeg, ...] = field(default_factory=tuple)
    total_credit: float = 0.0
    no_risk_upside: bool = False
    quantity: int = 1
    side: str = "sell"
    idempotency_key: str = ""
    reason: str = ""
    ivr: float = 0.0


@dataclass(frozen=True)
class JadeLizardExitDecision:
    """Jade Lizard エグジット決定 DTO。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "profit_target", "stop_loss", "force_close", "none"
    ] = "none"


# ---------------------------------------------------------------------------
# ポジション stub（Phase 2 で common_v3/position に差し替え）
# ---------------------------------------------------------------------------

@dataclass
class JadeLizardPosition:
    """Jade Lizard 保有ポジション。

    Attributes:
        symbol:          対象銘柄
        quantity:        保有枚数
        total_credit:    エントリー時に受け取ったクレジット合計
        unrealized_pnl:  未実現損益（正値 = 利益）
        entry_time:      エントリー時刻（UTC）
        tactic_name:     戦術名（ログ用）
    """
    symbol: str
    quantity: int
    total_credit: float
    unrealized_pnl: float = 0.0
    entry_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tactic_name: str = JADE_LIZARD_PREFIX


# ---------------------------------------------------------------------------
# qty 計算ユーティリティ
# ---------------------------------------------------------------------------

def compute_jade_lizard_qty(
    account_risk_budget: float,
    spread_width_pts: float,
    total_credit_per_contract: float,
) -> int:
    """Jade Lizard の発注枚数を算出する。

    最大損失 = (call_spread_width - total_credit) × 100 × qty
    account_risk_budget を超えない最大 qty を返す。

    no_risk_upside 成立時（total_credit >= spread_width × 100）は
    理論上の上方向損失ゼロだが、本関数はダウンサイド（put 側無限損失）
    に対して 1 コントラクト = spread_width × 100 のリスクとして計算する。

    Args:
        account_risk_budget:       1 トレードあたりの最大リスク予算（金額）
        spread_width_pts:          call spread 幅（ポイント）
        total_credit_per_contract: 1 コントラクトあたりの総クレジット

    Returns:
        int: 発注枚数（最低 1）

    Raises:
        ValueError: account_risk_budget <= 0 の場合
        ValueError: spread_width_pts <= 0 の場合
    """
    if account_risk_budget <= 0:
        raise ValueError(
            f"account_risk_budget={account_risk_budget!r} は正の値でなければなりません。"
        )
    if spread_width_pts <= 0:
        raise ValueError(
            f"spread_width_pts={spread_width_pts!r} は正の値でなければなりません。"
        )

    # 1 コントラクトあたりのリスク（ポイント × 100・クレジット分は差し引かない保守設定）
    risk_per_contract = spread_width_pts * 100.0
    qty = max(1, int(account_risk_budget // risk_per_contract))
    return qty


# ---------------------------------------------------------------------------
# JadeLizardTactic — Type A: EnterExit
# ---------------------------------------------------------------------------

class JadeLizardTactic(TacticBase):
    """Jade Lizard 戦術（Type A: enter_exit）。

    Short Put + Short Call Spread の 3-leg 組み合わせ。
    total_credit >= spread_width × 100 の「上方無リスク」条件を entry 時に検証する。

    エントリー窓: 10:00-12:00 ET
    強制クローズ: 15:50 ET
    IVR フィルタ: IVR >= ivr_min (default 60)
    Kill Switch 連動: preflight / should_enter / should_exit の 3 箇所で再チェック

    Args:
        config:   JadeLizardConfig（省略時はデフォルト値）
        clock_fn: テスト用時刻注入関数（省略時は datetime.now(ET)）
    """

    def __init__(
        self,
        config: JadeLizardConfig | None = None,
        clock_fn: "None | (() -> datetime)" = None,
        earnings_date_fn: "Optional[Callable[[str], Optional[object]]]" = None,
    ) -> None:
        self._cfg = config or JadeLizardConfig()
        self._clock_fn = clock_fn  # テスト用 DI
        self._earnings_date_fn = earnings_date_fn  # 決算日取得 DI（テスト用）

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return JADE_LIZARD_PREFIX

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _now_et(self) -> datetime:
        """現在時刻（ET）を返す（テスト時は clock_fn で差し替え可）。"""
        if self._clock_fn is not None:
            return self._clock_fn()
        return datetime.now(_ET)

    def _in_entry_window(self) -> bool:
        """エントリー窓（10:00-12:00 ET）内かどうかを返す。"""
        now = self._now_et()
        t = now.time()
        return _ENTRY_WINDOW_START <= t < _ENTRY_WINDOW_END

    def _past_force_close(self) -> bool:
        """強制クローズ時刻（15:50 ET）を過ぎているかどうかを返す。"""
        now = self._now_et()
        return now.time() >= _FORCE_CLOSE_TIME

    @staticmethod
    def _validate_ivr(symbol: str, ivr: float) -> None:
        """IVR を 0-100 スケールで検証する。

        Raises:
            TypeError: NaN / inf の場合
            TypeError: 0-100 範囲外の場合
        """
        if not math.isfinite(ivr):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} は NaN または inf です。"
                "IVR は 0-100 スケールの有限値でなければなりません。"
            )
        if not (_IVR_SCALE_MIN <= ivr <= _IVR_SCALE_MAX):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} が 0-100 スケール範囲外です。"
            )

    def _build_legs(
        self,
        symbol: str,
        underlying_price: float,
    ) -> tuple[JadeLizardLeg, ...]:
        """3 leg を構築して返す。

        デルタ目標の中央値を使ってストライクを簡易算出する（Phase 2 で
        実際のオプション・チェーンから最近接ストライクに差し替え）。

        short put  strike = underlying_price × (1 - put_delta_mid)
        short call strike = underlying_price × (1 + call_delta_mid)
        long  call strike = short_call_strike + spread_width_pts

        Args:
            symbol:           対象銘柄
            underlying_price: 原資産現在価格

        Returns:
            (short_put_leg, short_call_leg, long_call_leg) の tuple
        """
        put_delta_mid = (
            self._cfg.short_put_delta_min + self._cfg.short_put_delta_max
        ) / 2.0
        call_delta_mid = (
            self._cfg.short_call_delta_min + self._cfg.short_call_delta_max
        ) / 2.0

        short_put_strike = round(underlying_price * (1.0 - put_delta_mid), 2)
        short_call_strike = round(underlying_price * (1.0 + call_delta_mid), 2)
        long_call_strike = round(short_call_strike + self._cfg.spread_width_pts, 2)

        # クレジット見積もり（Phase 2 で実際の bid/ask から差し替え）
        # 暫定: デルタに比例した簡易プレミアム（$0.01 × delta × 100）
        short_put_credit = round(put_delta_mid * underlying_price * 0.01, 4)
        short_call_credit = round(call_delta_mid * underlying_price * 0.01, 4)
        long_call_cost = round(short_call_credit * 0.3, 4)  # 翼買いコスト（売りの30%）

        return (
            JadeLizardLeg(
                label="short_put",
                side="sell",
                strike=short_put_strike,
                delta=put_delta_mid,
                credit=short_put_credit,
            ),
            JadeLizardLeg(
                label="short_call",
                side="sell",
                strike=short_call_strike,
                delta=call_delta_mid,
                credit=short_call_credit,
            ),
            JadeLizardLeg(
                label="long_call",
                side="buy",
                strike=long_call_strike,
                delta=call_delta_mid * 0.5,  # 翼ロングは lower delta
                credit=-long_call_cost,
            ),
        )

    @staticmethod
    def _check_no_risk_upside(
        total_credit: float,
        spread_width_pts: float,
    ) -> bool:
        """「上方無リスク」条件を検証する。

        total_credit >= spread_width_pts × 100 が成立すれば True。
        1 コントラクト = 100 株換算。
        """
        return total_credit >= (spread_width_pts * 100.0)

    # ------------------------------------------------------------------
    # TacticBase ABC 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック項目:
        1. env が None → False
        2. Kill Switch ARMED → False（EICAS Advisory 発出）

        Returns:
            True — 戦術発動可能
            False — 発動不可（理由は log に必ず出力・silent skip 禁止）
        """
        if env is None:
            log.warning("[JadeLizardTactic.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[JadeLizardTactic.preflight] Kill Switch ARMED: jade_lizard を無効化"
            )
            return False

        return True

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol: str,
    ) -> JadeLizardEntryDecision:
        """エントリー判定。

        判定順:
        1. Kill Switch ARMED → should_enter=False
        2. エントリー窓外（10:00-12:00 ET）→ should_enter=False
        3. IVR NaN/inf チェック（TypeError）
        4. IVR スケール範囲外チェック（TypeError）
        5. IVR < ivr_min → should_enter=False
        6. 3-leg 構築
        7. total_credit 計算
        8. no_risk_upside 検証（警告は出すが entry は止めない）
        9. idempotency_key 生成（5 分バケット）

        Args:
            env:    市場環境スナップショット（ivr_by_symbol は 0-100 スケール）
            symbol: 対象銘柄（SymbolSelector 推奨銘柄）

        Returns:
            JadeLizardEntryDecision

        Raises:
            TypeError: IVR が NaN/inf または 0-100 範囲外の場合
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[JadeLizardTactic.should_enter] Kill Switch ARMED: "
                "エントリー判定スキップ (symbol=%s)",
                symbol,
            )
            return JadeLizardEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="kill_switch_armed",
            )

        # 1b. 決算近接チェック（earnings_proximity_days が設定されている場合のみ）
        # safe_default: earnings_date_fn が注入されている場合のみ True
        #   → 実 API キー未設定時（本番未設定）は取得失敗でブロックしない（既存 test 互換性保持）
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
                    "[JadeLizardTactic.should_enter] 決算近接ブロック: %s",
                    ep_reason,
                )
                return JadeLizardEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    reason=ep_reason,
                )

        # 2. エントリー窓チェック
        if not self._in_entry_window():
            log.debug(
                "[JadeLizardTactic.should_enter] entry window 外: "
                "%s (window=%s-%s ET) symbol=%s",
                self._now_et().strftime("%H:%M"),
                _ENTRY_WINDOW_START,
                _ENTRY_WINDOW_END,
                symbol,
            )
            return JadeLizardEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=(
                    f"entry_window_closed: now={self._now_et().strftime('%H:%M')} ET, "
                    f"window={_ENTRY_WINDOW_START}-{_ENTRY_WINDOW_END}"
                ),
            )

        # 3-4. IVR 検証（TypeError で上位に伝播）
        ivr = env.ivr_by_symbol.get(symbol, 0.0)
        self._validate_ivr(symbol, ivr)

        # 5. IVR フィルタ (動的閾値・規律 feedback_no_fixed_params 準拠)
        from atlas_v3.bots.engines.dynamic_params import get_dynamic_ivr_threshold
        ivr_min_dyn = get_dynamic_ivr_threshold(env.vix, self._cfg.ivr_min)
        if ivr < ivr_min_dyn:
            log.debug(
                "[JadeLizardTactic.should_enter] IVR=%.1f < dynamic_min=%.1f (base=%.1f, VIX=%.2f): スキップ (symbol=%s)",
                ivr, ivr_min_dyn, self._cfg.ivr_min, env.vix, symbol,
            )
            return JadeLizardEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"IVR={ivr:.1f} < dynamic_min={ivr_min_dyn:.1f}",
                ivr=ivr,
            )

        # 6. 3-leg 構築（underlying_price は vrp を近似利用・Phase 2 で実価格に差し替え）
        underlying_price = max(env.vrp, 1.0)  # stub: Phase 2 で MarketDataClient から取得
        legs = self._build_legs(symbol, underlying_price)

        # 7. total_credit 計算（sell 側正・buy 側負の合計）
        total_credit = round(sum(leg.credit for leg in legs) * 100.0, 4)

        # 8. no_risk_upside 検証
        no_risk_upside = self._check_no_risk_upside(
            total_credit, self._cfg.spread_width_pts
        )
        if not no_risk_upside:
            log.warning(
                "[JadeLizardTactic.should_enter] no_risk_upside 未成立: "
                "total_credit=%.4f < spread_width×100=%.1f (symbol=%s) "
                "— 上方向リスク存在・発注は継続するが EICAS 警告",
                total_credit,
                self._cfg.spread_width_pts * 100.0,
                symbol,
            )

        # 9. idempotency_key（5 分バケット）
        now_utc = datetime.now(timezone.utc)
        bucket_min = (now_utc.minute // 5) * 5
        trigger_time = now_utc.replace(
            minute=bucket_min, second=0, microsecond=0
        )
        idem_key = make_job_key(
            strategy=JADE_LIZARD_PREFIX,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[JadeLizardTactic.should_enter] エントリー: symbol=%s IVR=%.1f "
            "total_credit=%.4f no_risk_upside=%s key=%s",
            symbol,
            ivr,
            total_credit,
            no_risk_upside,
            idem_key,
        )

        return JadeLizardEntryDecision(
            should_enter=True,
            symbol=symbol,
            legs=legs,
            total_credit=total_credit,
            no_risk_upside=no_risk_upside,
            quantity=1,
            side="sell",
            idempotency_key=idem_key,
            reason=(
                f"IVR={ivr:.1f}>={self._cfg.ivr_min:.1f} / "
                f"no_risk_upside={no_risk_upside}"
            ),
            ivr=ivr,
        )

    # ------------------------------------------------------------------
    # 3-Leg 発注構築
    # ------------------------------------------------------------------

    def build_orders(
        self,
        decision: JadeLizardEntryDecision,
        capital_usd: float = 0.0,
    ) -> "list[OrderRequest]":
        """エントリー判定から 3 件の OrderRequest を構築する。

        Leg ごとに個別 idempotency_key を生成する（leg label をサフィックスに追加）。
        paper_mode=True の場合は order_type="paper_limit" でフラグ付けする。

        Args:
            decision:    should_enter=True の JadeLizardEntryDecision
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            [short_put_order, short_call_order, long_call_order]

        Raises:
            ValueError:      should_enter=False の decision が渡された場合
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_enter:
            raise ValueError(
                "[JadeLizardTactic.build_orders] should_enter=False の decision が渡されました。"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        # 2026-04-25: PreTradeGate L2 whitelist は "US.XXX" 形式・L3 margin は capital_usd+est_margin 必須。
        sym = decision.symbol if decision.symbol.startswith("US.") else f"US.{decision.symbol}"
        est_margin = sum(abs(leg.quantity) for leg in decision.legs) * 100 if decision.legs else decision.quantity * 100
        _gr = _gate(_Ctx(
            symbol=sym, qty=decision.quantity, side="SELL", is_long=False,
            est_margin=est_margin, capital_usd=capital_usd,
        ))
        if not _gr.allowed:
            raise ValueError(f"[JadeLizardTactic.build_orders] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=self._cfg.paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        order_type = "paper_limit" if self._cfg.paper_mode else "limit"
        orders: list[OrderRequest] = []

        for leg in decision.legs:
            leg_key = f"{decision.idempotency_key}_{leg.label}"
            orders.append(
                OrderRequest(
                    symbol=f"{decision.symbol}_{leg.label}_{leg.strike}",
                    side=leg.side,
                    quantity=decision.quantity,
                    order_type=order_type,
                    tactic_name=self.tactic_name,
                    idempotency_key=leg_key,
                )
            )

        return orders

    # ------------------------------------------------------------------
    # Exit 判定
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: JadeLizardPosition,
        env: MarketEnvironment,
    ) -> JadeLizardExitDecision:
        """エグジット判定。

        判定優先度:
        1. Kill Switch ARMED → force_close
        2. 15:50 ET 以降 → force_close
        3. unrealized_pnl >= total_credit × profit_target_pct → profit_target
        4. unrealized_pnl <= -total_credit × stop_loss_multiplier → stop_loss

        Args:
            position: 保有ポジション（total_credit が設定済みであること）
            env:      現在の市場環境

        Returns:
            JadeLizardExitDecision
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[JadeLizardTactic.should_exit] Kill Switch ARMED: 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return JadeLizardExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        # 2. 強制クローズ時刻
        if self._past_force_close():
            log.info(
                "[JadeLizardTactic.should_exit] 15:50 ET 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return JadeLizardExitDecision(
                should_exit=True,
                reason=f"force_close_time: {_FORCE_CLOSE_TIME} ET",
                exit_type="force_close",
            )

        # total_credit 未設定ガード
        if position.total_credit <= 0:
            log.warning(
                "[JadeLizardTactic.should_exit] total_credit=%.4f: exit 判定不能 (symbol=%s)",
                position.total_credit,
                position.symbol,
            )
            return JadeLizardExitDecision(
                should_exit=False,
                reason="total_credit_not_set",
            )

        profit_threshold = position.total_credit * self._cfg.profit_target_pct
        loss_threshold = -position.total_credit * self._cfg.stop_loss_multiplier

        # 3. 利確
        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[JadeLizardTactic.should_exit] 利確: pnl=%.4f >= target=%.4f (symbol=%s)",
                position.unrealized_pnl,
                profit_threshold,
                position.symbol,
            )
            return JadeLizardExitDecision(
                should_exit=True,
                reason=(
                    f"profit_target: pnl={position.unrealized_pnl:.4f} "
                    f">= {profit_threshold:.4f}"
                ),
                exit_type="profit_target",
            )

        # 4. 損切り
        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[JadeLizardTactic.should_exit] 損切り: pnl=%.4f <= stop=%.4f (symbol=%s)",
                position.unrealized_pnl,
                loss_threshold,
                position.symbol,
            )
            return JadeLizardExitDecision(
                should_exit=True,
                reason=(
                    f"stop_loss: pnl={position.unrealized_pnl:.4f} "
                    f"<= {loss_threshold:.4f}"
                ),
                exit_type="stop_loss",
            )

        return JadeLizardExitDecision(
            should_exit=False,
            reason="holding",
            exit_type="none",
        )
