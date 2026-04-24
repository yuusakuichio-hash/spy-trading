"""atlas_v3/bots/engines/iron_condor_sell_native.py — Wide IC Sell 戦術エンジン

spy_bot.IronCondorSellEngine を atlas_v3 TacticBase 規律に再実装した native 版。
iron_fly.py（ATM Iron Fly）とは別戦術: こちらは OTM 幅広 Iron Condor 売り。

4 フェーズ構造:
  Phase 1: premarket_check / reset_daily  -- VIX/IVR/連続損失・環境フィルタ
  Phase 2: execute_entry                  -- CALL CS + PUT CS 同時売り
  Phase 3: check_exit                     -- TP/SL/タイムストップ (毎 tick)
  Phase 4: _close_position                -- 全 4 leg クローズ

デルタ目標（動的算出）:
  base = CALL/PUT_DELTA_BASE (0.20)
  高VIX (>=28): −0.03 縮小 | 高IVR (>=70%ile): +0.03 拡大

スプレッド幅（動的算出）:
  spread_width = ATR_14 * WIDTH_ATR_MULT  (ATR 取得失敗時: WIDTH_DEFAULT=5)

資本配分（動的算出）:
  VIX < 28: CAPITAL_PCT_BASE (0.40) | VIX >= 28: CAPITAL_PCT_HIGH (0.30)

統合:
  - TacticBase + preflight
  - PDTGuard (PDTBlockedError)
  - earnings_proximity (5 営業日以内でブロック)
  - common_v3.risk.pre_trade_check (check_order_critical_only)
  - kill_switch (is_active)

禁則:
  - spy_bot.py / chronos_bot.py への書き込み・import 禁止
  - asyncio 内直接呼び出し禁止 (sync 専用)
  - ハードコードパラメータ禁止: 全て config DTO 経由
  - CC <= 20 per method
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Optional
from zoneinfo import ZoneInfo

from atlas_v3.bots.engines.earnings_calendar_check import is_near_earnings
from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active
from common_v3.risk.pre_trade_check import (
    GateResult,
    OrderCtx,
    PreTradeConfig,
    check_order_critical_only,
)

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# 設定定数（spy_bot.py と同値・config DTO 経由で override 可能）
# ---------------------------------------------------------------------------

_CALL_DELTA_BASE: float = 0.20
_PUT_DELTA_BASE: float = 0.20
_VIX_HIGH_THRESHOLD: float = 28.0
_VIX_MIN: float = 18.0
_VIX_MAX: float = 40.0
_IVR_MIN_PCT: float = 40.0
_WIDTH_ATR_MULT: float = 0.50
_WIDTH_MIN: int = 1
_WIDTH_DEFAULT: int = 5
_CAPITAL_PCT_BASE: float = 0.40
_CAPITAL_PCT_HIGH: float = 0.30
_PROFIT_TARGET_PCT: float = 0.50
_STOP_LOSS_MULT: float = 2.0
_MAX_QTY: int = 3
_MAX_QTY_PAPER: int = 10
_SMALL_ACCOUNT_USD: float = 15_000.0
_MAX_CONSECUTIVE_LOSSES: int = 3
_ENTRY_CUTOFF_H: int = 15
_ENTRY_CUTOFF_M: int = 30
_FORCE_CLOSE_H: int = 15
_FORCE_CLOSE_M: int = 45
_EARLY_CLOSE_H: int = 12
_EARLY_CLOSE_M: int = 50
_VIX_SPIKE_SIZE_FACTOR: float = 0.50
_EARNINGS_PROXIMITY_DAYS: int = 5

# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=False)
class IronCondorSellConfig:
    """Wide IC Sell エンジン設定。

    各フィールドは spy_bot.py の対応定数と 1:1 で対応する。
    テスト・バックテストで DI 注入して override できる。

    Attributes:
        call_delta_base:      CALL 売りデルタ目標 (base・VIX/IVR 補正前)
        put_delta_base:       PUT 売りデルタ目標 (base・絶対値)
        vix_high_threshold:   高 VIX 判定閾値（この値以上でデルタ縮小・配分縮小）
        vix_min:              エントリー最低 VIX（低 IV ではプレミアム不足）
        vix_max:              エントリー上限 VIX（方向性リスク大）
        ivr_min_pct:          IVR の 60 日パーセンタイル下限
        width_atr_mult:       spread_width = ATR_14 * this
        width_min:            スプレッド幅最小値
        width_default:        ATR 取得失敗時フォールバック幅
        capital_pct_base:     通常 VIX での資本配分率
        capital_pct_high:     高 VIX での資本配分率
        profit_target_pct:    net_credit の何 % で利確するか
        stop_loss_mult:       net_credit × この倍率で損切り
        max_qty:              本番最大枚数
        max_qty_paper:        ペーパー最大枚数
        small_account_usd:    この金額以下は 1 枚上限
        max_consecutive_losses: 連敗でその日停止
        entry_cutoff_h:       エントリーカットオフ時 (ET)
        entry_cutoff_m:       エントリーカットオフ分 (ET)
        force_close_h:        強制クローズ時 (ET)
        force_close_m:        強制クローズ分 (ET)
        early_close_h:        半日取引日 強制クローズ時 (ET)
        early_close_m:        半日取引日 強制クローズ分 (ET)
        vix_spike_size_factor: VIX スパイク翌日 qty 縮小係数
        earnings_proximity_days: 決算 N 営業日以内でブロック
    """
    call_delta_base: float = _CALL_DELTA_BASE
    put_delta_base: float = _PUT_DELTA_BASE
    vix_high_threshold: float = _VIX_HIGH_THRESHOLD
    vix_min: float = _VIX_MIN
    vix_max: float = _VIX_MAX
    ivr_min_pct: float = _IVR_MIN_PCT
    width_atr_mult: float = _WIDTH_ATR_MULT
    width_min: int = _WIDTH_MIN
    width_default: int = _WIDTH_DEFAULT
    capital_pct_base: float = _CAPITAL_PCT_BASE
    capital_pct_high: float = _CAPITAL_PCT_HIGH
    profit_target_pct: float = _PROFIT_TARGET_PCT
    stop_loss_mult: float = _STOP_LOSS_MULT
    max_qty: int = _MAX_QTY
    max_qty_paper: int = _MAX_QTY_PAPER
    small_account_usd: float = _SMALL_ACCOUNT_USD
    max_consecutive_losses: int = _MAX_CONSECUTIVE_LOSSES
    entry_cutoff_h: int = _ENTRY_CUTOFF_H
    entry_cutoff_m: int = _ENTRY_CUTOFF_M
    force_close_h: int = _FORCE_CLOSE_H
    force_close_m: int = _FORCE_CLOSE_M
    early_close_h: int = _EARLY_CLOSE_H
    early_close_m: int = _EARLY_CLOSE_M
    vix_spike_size_factor: float = _VIX_SPIKE_SIZE_FACTOR
    earnings_proximity_days: int = _EARNINGS_PROXIMITY_DAYS


# ---------------------------------------------------------------------------
# Position DTO
# ---------------------------------------------------------------------------


@dataclass
class IronCondorSellPosition:
    """Wide IC Sell ポジション（4 leg）。

    Attributes:
        symbol:           原資産銘柄コード (例: "US.SPY")
        expiry:           満期日文字列 "YYYY-MM-DD"
        qty:              枚数
        call_sell_code:   CALL 売り脚 オプションコード
        call_buy_code:    CALL 買い脚 オプションコード
        put_sell_code:    PUT 売り脚 オプションコード
        put_buy_code:     PUT 買い脚 オプションコード
        call_sell_strike: CALL 売りストライク
        call_buy_strike:  CALL 買いストライク
        put_sell_strike:  PUT 売りストライク
        put_buy_strike:   PUT 買いストライク
        call_net_credit:  CALL CS ネットクレジット / contract
        put_net_credit:   PUT CS ネットクレジット / contract
        net_credit:       合計ネットクレジット (= call + put)
        spread_width:     スプレッド幅 (strike 単位)
        vix:              エントリー時 VIX
        max_loss_per_contract: 最大損失 / contract (USD)
        entry_time:       エントリー ISO 文字列
    """
    symbol: str
    expiry: str
    qty: int
    call_sell_code: str
    call_buy_code: str
    put_sell_code: str
    put_buy_code: str
    call_sell_strike: float
    call_buy_strike: float
    put_sell_strike: float
    put_buy_strike: float
    call_net_credit: float
    put_net_credit: float
    net_credit: float = field(init=False)
    spread_width: float = 5.0
    vix: float = 20.0
    max_loss_per_contract: float = field(init=False)
    entry_time: str = field(default_factory=lambda: datetime.now(ET).isoformat())

    def __post_init__(self) -> None:
        self.net_credit = round(self.call_net_credit + self.put_net_credit, 4)
        self.max_loss_per_contract = max(
            0.0, (self.spread_width - self.net_credit) * 100
        )


# ---------------------------------------------------------------------------
# エントリー / エグジット 決定 DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IronCondorSellEntryDecision:
    """IC Sell エントリー判定結果。

    Attributes:
        should_enter:    True = エントリー可
        symbol:          対象銘柄コード
        reason:          判定根拠テキスト
        vix:             判定時 VIX
        ivr_pct:         判定時 IVR パーセンタイル
        call_delta:      計算後 CALL デルタ目標
        put_delta:       計算後 PUT デルタ目標
        spread_width:    計算後スプレッド幅
        qty:             計算後枚数
        capital_pct:     計算後資本配分率
        vix_spike_30:    VIX スパイク翌日フラグ
        idempotency_key: 冪等性キー
    """
    should_enter: bool
    symbol: str
    reason: str = ""
    vix: float = 0.0
    ivr_pct: float = 0.0
    call_delta: float = 0.0
    put_delta: float = 0.0
    spread_width: int = 0
    qty: int = 0
    capital_pct: float = 0.0
    vix_spike_30: bool = False
    idempotency_key: str = ""


@dataclass(frozen=True)
class IronCondorSellExitDecision:
    """IC Sell エグジット判定結果。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "profit_target", "stop_loss", "force_close", "kill_switch", "none"
    ] = "none"


# ---------------------------------------------------------------------------
# PremarketAssessment — premarket_check の結果キャッシュ
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PremarketAssessment:
    """premarket_check の結果を保持するキャッシュ DTO。"""
    ok: bool
    vix: float
    ivr_pct: float
    vix_spike_30: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# TradeEngine Protocol（broker 依存排除・テスト用 stub 差し込み可能）
# ---------------------------------------------------------------------------

class NoOpTradeEngine:
    """テスト用 TradeEngine stub（broker 接続なし）。

    全 place_* メソッドは成功を返す。
    テスト側から monkeypatch で差し替えることを想定。
    """

    def get_account_cash(self) -> float:
        return 25_000.0

    def get_open_positions(self) -> list:
        return []

    def get_vix(self) -> Optional[float]:
        return 20.0

    def get_vix_history(self, days: int = 60) -> list[float]:
        return [20.0] * days

    def get_symbol_atr(self, symbol: str, period: int = 14) -> Optional[float]:
        return 4.0

    def get_spy_current(self) -> Optional[float]:
        return 560.0

    def get_option_chain_with_greeks(
        self,
        expiry: str,
        chain_type: str,
        center_strike: Optional[float] = None,
    ) -> list:
        return []

    def find_by_delta(self, chain: list, delta: float) -> Optional[dict]:
        return None

    def find_by_strike(self, chain: list, strike: float) -> Optional[dict]:
        return None

    def place_credit_spread(
        self,
        sell_code: str,
        buy_code: str,
        qty: int,
        direction: str,
        sell_init_price: float = 0.0,
        buy_init_price: float = 0.0,
        vix: float = 20.0,
    ) -> bool:
        log.info(
            "[NoOpTradeEngine.place_credit_spread] DRY direction=%s qty=%d "
            "sell=%s buy=%s",
            direction, qty, sell_code, buy_code,
        )
        return True

    def _place_single_leg(
        self,
        code: str,
        side: object,
        qty: int,
        label: str,
        init_price: Optional[float] = None,
        use_limit: bool = False,
    ) -> tuple:
        return (f"DRY_OID_{label}", "dry_fill")


# ---------------------------------------------------------------------------
# IronCondorSellEngine — TacticBase 実装
# ---------------------------------------------------------------------------


class IronCondorSellEngine(TacticBase):
    """Wide Iron Condor Sell 戦術エンジン（Type A: enter_exit）。

    spy_bot.IronCondorSellEngine を atlas_v3 TacticBase 規律に移植した版。
    iron_fly.py の ATM 中心構造とは異なり、OTM デルタ目標の広い IC 売りを実行する。

    インターフェース（spy_bot 互換）:
        reset_daily()           — 日次リセット
        premarket_check()       — Phase 1 環境フィルタ (bool)
        execute_entry()         — Phase 2 エントリー実行 (IronCondorSellPosition | None)
        check_exit()            — Phase 3 エグジット監視 (bool)

    TacticBase 追加:
        preflight(env)          — kill_switch / VIX 基本チェック
        should_enter_decision() — DTO ベースエントリー判定
        should_exit_decision()  — DTO ベースエグジット判定

    Args:
        trade_engine:      TradeEngine 実装（None なら NoOpTradeEngine）
        config:            IronCondorSellConfig（None ならデフォルト値）
        earnings_date_fn:  決算日取得関数 DI（テスト用 stub 注入可能）
        paper:             True = paper モード（PDT スキップ・サイズ上限緩和）
        dry_test:          True = dry-test モード（発注なし・固定値応答）
    """

    # PDT 1DTE 対応（IC は翌日満期でも同等の IV crush 収益構造）
    supports_1dte: bool = True
    # allow_expiry_pass_through: EOD タイムストップ = 満期消滅として記録（PDT 非消費）
    allow_expiry_pass_through: bool = True

    def __init__(
        self,
        trade_engine: object | None = None,
        config: IronCondorSellConfig | None = None,
        earnings_date_fn: Optional[Callable[[str], Optional[object]]] = None,
        paper: bool = False,
        dry_test: bool = False,
    ) -> None:
        self._eng = trade_engine or NoOpTradeEngine()
        self._cfg = config or IronCondorSellConfig()
        self._earnings_date_fn = earnings_date_fn
        self.paper = paper
        self.dry_test = dry_test
        # 日次状態
        self.today_vix: Optional[float] = None
        self.position: Optional[IronCondorSellPosition] = None
        self.trade_done: bool = False
        self.entry_done: bool = False
        self._assessment: Optional[PremarketAssessment] = None
        self._vix_spike_30: bool = False

    # ------------------------------------------------------------------
    # TacticBase ABC
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "iron_condor_sell"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック順:
        1. env None ガード
        2. Kill Switch ARMED → False
        3. VIX >= vix_max → False（方向性リスク大）

        Returns:
            True — 戦術発動可能 / False — 発動不可（理由は log に出力）
        """
        if env is None:
            log.warning("[IronCondorSellEngine.preflight] env=None: preflight 失敗")
            return False
        if kill_switch_is_active():
            log.warning("[IronCondorSellEngine.preflight] Kill Switch ARMED: 無効化")
            return False
        if env.vix >= self._cfg.vix_max:
            log.info(
                "[IronCondorSellEngine.preflight] VIX=%.2f >= vix_max=%.2f: スキップ",
                env.vix, self._cfg.vix_max,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Phase 0: 日次リセット
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """日次リセット。BOD（取引開始前）に呼ぶ。"""
        self.today_vix = None
        self.position = None
        self.trade_done = False
        self.entry_done = False
        self._assessment = None
        self._vix_spike_30 = False
        log.debug("[IronCondorSellEngine] reset_daily: 日次状態リセット完了")

    # ------------------------------------------------------------------
    # Phase 1: プレマーケット環境チェック
    # ------------------------------------------------------------------

    def premarket_check(self, symbol: str = "US.SPY") -> bool:
        """VIX / IVR / 連続損失 / Kill Switch を確認してエントリー可否を返す。

        Args:
            symbol: チェック対象銘柄コード（デフォルト "US.SPY"）

        Returns:
            True — エントリー可 / False — スキップ
        """
        if kill_switch_is_active():
            log.warning("[IronCondorSellEngine.premarket_check] Kill Switch ARMED: スキップ")
            return False

        # dry_test モード: 固定 VIX で即 OK
        if self.dry_test:
            self.today_vix = 22.0
            self._vix_spike_30 = False
            self._assessment = PremarketAssessment(
                ok=True, vix=22.0, ivr_pct=55.0, vix_spike_30=False,
                reason="dry_test",
            )
            log.info("[IronCondorSellEngine.premarket_check][DRY-TEST] OK: vix=22.0")
            return True

        vix = self._eng.get_vix() if hasattr(self._eng, "get_vix") else None
        if vix is None:
            log.warning("[IronCondorSellEngine.premarket_check] VIX 取得失敗 → スキップ")
            self._assessment = PremarketAssessment(
                ok=False, vix=0.0, ivr_pct=0.0, vix_spike_30=False,
                reason="vix_fetch_failed",
            )
            return False

        self.today_vix = vix

        if vix < self._cfg.vix_min:
            log.info(
                "[IronCondorSellEngine.premarket_check] VIX=%.1f < %.1f: プレミアム不足",
                vix, self._cfg.vix_min,
            )
            self._assessment = PremarketAssessment(
                ok=False, vix=vix, ivr_pct=0.0, vix_spike_30=False,
                reason=f"vix_too_low: {vix:.1f} < {self._cfg.vix_min}",
            )
            return False

        if vix >= self._cfg.vix_max:
            log.info(
                "[IronCondorSellEngine.premarket_check] VIX=%.1f >= %.1f: 方向性リスク大",
                vix, self._cfg.vix_max,
            )
            self._assessment = PremarketAssessment(
                ok=False, vix=vix, ivr_pct=0.0, vix_spike_30=False,
                reason=f"vix_too_high: {vix:.1f} >= {self._cfg.vix_max}",
            )
            return False

        ivr_pct = self._get_ivr_percentile()
        if ivr_pct < self._cfg.ivr_min_pct:
            log.info(
                "[IronCondorSellEngine.premarket_check] IVR=%d%%ile < %d%%ile: スキップ",
                int(ivr_pct), int(self._cfg.ivr_min_pct),
            )
            self._assessment = PremarketAssessment(
                ok=False, vix=vix, ivr_pct=ivr_pct, vix_spike_30=False,
                reason=f"ivr_too_low: {ivr_pct:.0f} < {self._cfg.ivr_min_pct}",
            )
            return False

        if self._check_consecutive_losses():
            log.info(
                "[IronCondorSellEngine.premarket_check] %d 連敗: 当日停止",
                self._cfg.max_consecutive_losses,
            )
            self._assessment = PremarketAssessment(
                ok=False, vix=vix, ivr_pct=ivr_pct, vix_spike_30=False,
                reason="consecutive_losses_exceeded",
            )
            return False

        vix_spike_30 = self._is_vix_spike_30_day(vix)
        self._vix_spike_30 = vix_spike_30
        self._assessment = PremarketAssessment(
            ok=True, vix=vix, ivr_pct=ivr_pct, vix_spike_30=vix_spike_30,
            reason="ok",
        )
        log.info(
            "[IronCondorSellEngine.premarket_check] OK: VIX=%.1f IVR=%d%%ile spike30=%s",
            vix, int(ivr_pct), vix_spike_30,
        )
        return True

    # ------------------------------------------------------------------
    # Phase 2: エントリー実行
    # ------------------------------------------------------------------

    def execute_entry(
        self,
        symbol: str = "US.SPY",
        signal_id: Optional[str] = None,
    ) -> Optional[IronCondorSellPosition]:
        """CALL CS + PUT CS を同時売り発注する。

        premarket_check() を通過していることを前提に呼ぶ。
        戻り値が None の場合はエントリー見送り（ログに理由を出力する）。

        Args:
            symbol:    対象銘柄コード (例: "US.SPY")
            signal_id: 外部シグナル ID（None なら自動生成）

        Returns:
            IronCondorSellPosition または None（エントリー見送り）

        Raises:
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        if self.entry_done:
            log.debug("[IronCondorSellEngine.execute_entry] entry_done=True: スキップ")
            return None

        # Kill Switch 最終確認
        if kill_switch_is_active():
            log.warning("[IronCondorSellEngine.execute_entry] Kill Switch ARMED: 中止")
            return None

        # エントリーカットオフ
        now_et = datetime.now(ET)
        cutoff_min = self._cfg.entry_cutoff_h * 60 + self._cfg.entry_cutoff_m
        now_min = now_et.hour * 60 + now_et.minute
        if not self.dry_test and now_min >= cutoff_min:
            log.info(
                "[IronCondorSellEngine.execute_entry] %d:%02d ET 以降 → カットオフ",
                self._cfg.entry_cutoff_h, self._cfg.entry_cutoff_m,
            )
            return None

        # 決算近接チェック
        if self._cfg.earnings_proximity_days > 0:
            safe = self._earnings_date_fn is not None
            blocked, ep_reason = is_near_earnings(
                symbol=symbol,
                proximity_days=self._cfg.earnings_proximity_days,
                earnings_date_fn=self._earnings_date_fn,
                safe_default=safe,
            )
            if blocked:
                log.info(
                    "[IronCondorSellEngine.execute_entry] 決算近接ブロック: %s",
                    ep_reason,
                )
                return None

        # PDT チェック
        capital_usd = self._get_cash()
        pdt_guard = PDTGuard(paper_mode=self.paper, capital_usd=capital_usd)
        pdt_result = pdt_guard.check_can_trade(symbol)
        if not pdt_result.allowed:
            raise PDTBlockedError(
                f"[IronCondorSellEngine.execute_entry] PDT blocked: {pdt_result.reason}"
            )

        # signal_id 自動生成
        if signal_id is None:
            ticker = symbol.replace("US.", "").replace(".", "")
            ts = now_et.strftime("%Y%m%d%H%M")
            signal_id = f"ic_{ticker}_{ts}_{uuid.uuid4().hex[:8]}"

        today_str = now_et.strftime("%Y-%m-%d")
        vix = self.today_vix or 20.0
        ivr_pct = self._get_ivr_percentile()
        call_delta, put_delta = self._calc_dynamic_deltas(vix, ivr_pct)
        spread_width = self._calc_dynamic_width(symbol)
        capital_pct = self._calc_capital_pct(vix)
        cash = self._get_cash()
        qty = self._calc_qty(cash, spread_width, capital_pct)

        if self._vix_spike_30:
            qty = max(1, int(qty * self._cfg.vix_spike_size_factor))
            log.info(
                "[IronCondorSellEngine.execute_entry][VIXSpike30] qty 縮小: x%.2f -> %d",
                self._cfg.vix_spike_size_factor, qty,
            )

        log.info(
            "[IronCondorSellEngine.execute_entry] %s VIX=%.1f IVR=%d%%ile "
            "cD=%.2f pD=%.2f w=%d qty=%d",
            symbol, vix, int(ivr_pct), call_delta, put_delta, spread_width, qty,
        )

        # dry_test: 固定ストライクでポジション生成
        if self.dry_test:
            return self._build_dry_position(
                symbol, today_str, qty, spread_width, vix,
                call_delta, put_delta, signal_id,
            )

        # 実市場エントリー
        center = None
        if hasattr(self._eng, "get_spy_current"):
            center = self._eng.get_spy_current()

        call_chain = self._eng.get_option_chain_with_greeks(
            today_str, "CALL", center_strike=float(center) if center else None
        )
        put_chain = self._eng.get_option_chain_with_greeks(
            today_str, "PUT", center_strike=float(center) if center else None
        )
        if not call_chain or not put_chain:
            log.error("[IronCondorSellEngine.execute_entry] チェーン取得失敗 → 中止")
            return None

        call_sell_opt = self._eng.find_by_delta(call_chain, call_delta)
        if not call_sell_opt:
            log.error(
                "[IronCondorSellEngine.execute_entry] CALL SELL 脚見つからず (delta=%.2f)",
                call_delta,
            )
            return None
        call_sell_strike = call_sell_opt["strike_price"]

        call_buy_opt = self._eng.find_by_strike(call_chain, call_sell_strike + spread_width)
        if not call_buy_opt:
            log.error(
                "[IronCondorSellEngine.execute_entry] CALL BUY 脚見つからず (target=%.1f)",
                call_sell_strike + spread_width,
            )
            return None

        put_sell_opt = self._eng.find_by_delta(put_chain, put_delta)
        if not put_sell_opt:
            log.error(
                "[IronCondorSellEngine.execute_entry] PUT SELL 脚見つからず (delta=%.2f)",
                put_delta,
            )
            return None
        put_sell_strike = put_sell_opt["strike_price"]

        put_buy_opt = self._eng.find_by_strike(put_chain, put_sell_strike - spread_width)
        if not put_buy_opt:
            log.error(
                "[IronCondorSellEngine.execute_entry] PUT BUY 脚見つからず (target=%.1f)",
                put_sell_strike - spread_width,
            )
            return None

        # pre_trade_check (critical-only: Kill Switch + L1 Deep ITM + L4 qty sanity)
        est_margin = spread_width * 100 * qty
        for opt, side_str in [(call_sell_opt, "SELL"), (put_sell_opt, "SELL")]:
            ctx = OrderCtx(
                symbol=symbol,
                qty=qty,
                option_price=opt.get("ask_price", 0.0),
                side=side_str,
                is_long=False,
                est_margin=est_margin / 2,
                capital_usd=cash,
            )
            gate_result: GateResult = check_order_critical_only(ctx)
            if not gate_result.allowed:
                log.warning(
                    "[IronCondorSellEngine.execute_entry] pre_trade_check NG: %s",
                    gate_result.reason,
                )
                return None

        # net_credit 検証
        call_credit = round(
            call_sell_opt.get("bid_price", 0.0) - call_buy_opt.get("ask_price", 0.0), 4
        )
        put_credit = round(
            put_sell_opt.get("bid_price", 0.0) - put_buy_opt.get("ask_price", 0.0), 4
        )
        if call_credit <= 0 or put_credit <= 0:
            log.warning(
                "[IronCondorSellEngine.execute_entry] net_credit NG: CALL=%.4f PUT=%.4f",
                call_credit, put_credit,
            )
            return None

        # 発注 (PUT CS → CALL CS)
        def _mid(opt: dict) -> float:
            return round((opt.get("bid_price", 0.0) + opt.get("ask_price", 0.0)) / 2, 2)

        put_ok = self._eng.place_credit_spread(
            sell_code=put_sell_opt["code"], buy_code=put_buy_opt["code"],
            qty=qty, direction="PUT",
            sell_init_price=_mid(put_sell_opt), buy_init_price=_mid(put_buy_opt),
            vix=vix,
        )
        if not put_ok:
            log.error("[IronCondorSellEngine.execute_entry] PUT CS 発注失敗 → IC キャンセル")
            return None

        call_ok = self._eng.place_credit_spread(
            sell_code=call_sell_opt["code"], buy_code=call_buy_opt["code"],
            qty=qty, direction="CALL",
            sell_init_price=_mid(call_sell_opt), buy_init_price=_mid(call_buy_opt),
            vix=vix,
        )
        if not call_ok:
            log.error("[IronCondorSellEngine.execute_entry] CALL CS 発注失敗 → PUT 脚巻き戻し試行")
            self._unwind_put_leg(
                put_sell_opt=put_sell_opt,
                put_buy_opt=put_buy_opt,
                qty=qty,
            )
            return None

        pos = IronCondorSellPosition(
            symbol=symbol,
            expiry=today_str,
            qty=qty,
            call_sell_code=call_sell_opt["code"],
            call_buy_code=call_buy_opt["code"],
            put_sell_code=put_sell_opt["code"],
            put_buy_code=put_buy_opt["code"],
            call_sell_strike=call_sell_strike,
            call_buy_strike=call_buy_opt["strike_price"],
            put_sell_strike=put_sell_strike,
            put_buy_strike=put_buy_opt["strike_price"],
            call_net_credit=call_credit,
            put_net_credit=put_credit,
            spread_width=spread_width,
            vix=vix,
        )
        self.entry_done = True
        self.trade_done = True
        self.position = pos
        log.info(
            "[IronCondorSellEngine.execute_entry] 完了: %s CALL %s/%s PUT %s/%s "
            "credit=%.4f x%d",
            symbol,
            call_sell_strike, call_buy_opt["strike_price"],
            put_sell_strike, put_buy_opt["strike_price"],
            pos.net_credit, qty,
        )
        return pos

    # ------------------------------------------------------------------
    # Phase 3: エグジット監視
    # ------------------------------------------------------------------

    def check_exit(
        self,
        now_et: Optional[datetime] = None,
        is_early_close: bool = False,
    ) -> bool:
        """毎 tick 呼ぶ。TP / SL / タイムストップを確認してクローズする。

        Args:
            now_et:        現在 ET 時刻（None なら datetime.now(ET)）。テスト DI 用。
            is_early_close: True = 半日取引日フラグ（強制クローズ時刻を前倒し）

        Returns:
            True — クローズした / False — 保有継続
        """
        if self.position is None:
            return False

        pos = self.position
        t = now_et or datetime.now(ET)

        # Kill Switch 最優先
        if kill_switch_is_active():
            log.warning(
                "[IronCondorSellEngine.check_exit] Kill Switch ARMED: 強制クローズ"
            )
            return self._close_position(pos, reason="kill_switch", now_et=t)

        # タイムストップ
        if not self.dry_test:
            fc_h = self._cfg.early_close_h if is_early_close else self._cfg.force_close_h
            fc_m = self._cfg.early_close_m if is_early_close else self._cfg.force_close_m
            if (t.hour, t.minute) >= (fc_h, fc_m):
                log.info(
                    "[IronCondorSellEngine.check_exit] タイムストップ %d:%02d ET → 強制クローズ",
                    fc_h, fc_m,
                )
                return self._close_position(pos, reason="force_close_eod", now_et=t)

        # 現在価値評価
        current_value = self._estimate_current_value(pos, now_et=t)
        if current_value is None:
            return False

        current_pnl = round(pos.net_credit - current_value, 4)
        tp_threshold = round(pos.net_credit * self._cfg.profit_target_pct, 4)
        sl_threshold = round(pos.net_credit * self._cfg.stop_loss_mult, 4)

        if current_pnl >= tp_threshold:
            log.info(
                "[IronCondorSellEngine.check_exit] TP: pnl=%.4f >= target=%.4f",
                current_pnl, tp_threshold,
            )
            return self._close_position(pos, reason="profit_target", now_et=t)

        if current_pnl <= -sl_threshold:
            log.info(
                "[IronCondorSellEngine.check_exit] SL: pnl=%.4f <= -%.4f",
                current_pnl, sl_threshold,
            )
            return self._close_position(pos, reason="stop_loss", now_et=t)

        return False

    # ------------------------------------------------------------------
    # DTO ベース判定 API (AtlasEngine dispatch 用)
    # ------------------------------------------------------------------

    def should_enter_decision(
        self,
        env: MarketEnvironment,
        symbol: str,
        now_et: Optional[datetime] = None,
    ) -> IronCondorSellEntryDecision:
        """MarketEnvironment から DTO ベースのエントリー判定を返す。

        AtlasEngine の dispatch ループで呼ぶ thin wrapper。
        実際の発注は execute_entry() が担う。

        Returns:
            IronCondorSellEntryDecision
        """
        if not self.preflight(env):
            return IronCondorSellEntryDecision(
                should_enter=False, symbol=symbol, reason="preflight_failed",
            )
        t = now_et or datetime.now(ET)
        cutoff_min = self._cfg.entry_cutoff_h * 60 + self._cfg.entry_cutoff_m
        if t.hour * 60 + t.minute >= cutoff_min:
            return IronCondorSellEntryDecision(
                should_enter=False, symbol=symbol, reason="entry_cutoff",
            )
        if self.entry_done:
            return IronCondorSellEntryDecision(
                should_enter=False, symbol=symbol, reason="entry_already_done",
            )

        vix = env.vix
        ivr_pct = env.ivr_by_symbol.get(symbol, 50.0)
        call_delta, put_delta = self._calc_dynamic_deltas(vix, ivr_pct)
        spread_width = self._calc_dynamic_width(symbol)
        capital_pct = self._calc_capital_pct(vix)
        cash = self._get_cash()
        qty = self._calc_qty(cash, spread_width, capital_pct)

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name, symbol=symbol, trigger_time=trigger_time,
        )
        return IronCondorSellEntryDecision(
            should_enter=True,
            symbol=symbol,
            reason="all_checks_passed",
            vix=vix,
            ivr_pct=ivr_pct,
            call_delta=call_delta,
            put_delta=put_delta,
            spread_width=spread_width,
            qty=qty,
            capital_pct=capital_pct,
            vix_spike_30=self._vix_spike_30,
            idempotency_key=idem_key,
        )

    def should_exit_decision(
        self,
        position: IronCondorSellPosition,
        now_et: Optional[datetime] = None,
        is_early_close: bool = False,
    ) -> IronCondorSellExitDecision:
        """DTO ベースのエグジット判定（check_exit の純粋版）。

        ポジション変更・ログ副作用なし。テスト用。

        Returns:
            IronCondorSellExitDecision
        """
        if kill_switch_is_active():
            return IronCondorSellExitDecision(
                should_exit=True, reason="kill_switch_armed", exit_type="kill_switch",
            )

        t = now_et or datetime.now(ET)
        fc_h = self._cfg.early_close_h if is_early_close else self._cfg.force_close_h
        fc_m = self._cfg.early_close_m if is_early_close else self._cfg.force_close_m
        if (t.hour, t.minute) >= (fc_h, fc_m):
            return IronCondorSellExitDecision(
                should_exit=True, reason=f"force_close_{fc_h}:{fc_m:02d}_ET",
                exit_type="force_close",
            )

        cv = self._estimate_current_value(position, now_et=t)
        if cv is None:
            return IronCondorSellExitDecision(
                should_exit=False, reason="current_value_unavailable", exit_type="none",
            )

        pnl = round(position.net_credit - cv, 4)
        tp = round(position.net_credit * self._cfg.profit_target_pct, 4)
        sl = round(position.net_credit * self._cfg.stop_loss_mult, 4)

        if pnl >= tp:
            return IronCondorSellExitDecision(
                should_exit=True,
                reason=f"profit_target: pnl={pnl:.4f} >= {tp:.4f}",
                exit_type="profit_target",
            )
        if pnl <= -sl:
            return IronCondorSellExitDecision(
                should_exit=True,
                reason=f"stop_loss: pnl={pnl:.4f} <= -{sl:.4f}",
                exit_type="stop_loss",
            )
        return IronCondorSellExitDecision(should_exit=False, reason="holding", exit_type="none")

    # ------------------------------------------------------------------
    # is_active
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """ポジション保有中かどうかを返す。"""
        return self.position is not None

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _get_cash(self) -> float:
        try:
            cash = self._eng.get_account_cash()
            return float(cash) if cash and cash > 0 else 10_000.0
        except Exception:
            return 10_000.0

    def _get_ivr_percentile(self) -> float:
        """現在 VIX が過去 60 日中の何 %ile か返す。取得失敗時は 50.0。"""
        try:
            vix = self.today_vix or (self._eng.get_vix() if hasattr(self._eng, "get_vix") else 20.0)
            hist = self._eng.get_vix_history(days=60) if hasattr(self._eng, "get_vix_history") else []
            if len(hist) >= 20:
                below = sum(1 for v in hist if v <= vix)
                return round(below / len(hist) * 100, 1)
        except Exception:
            pass
        return 50.0

    def _calc_dynamic_deltas(
        self, vix: float, ivr_pct: float
    ) -> tuple[float, float]:
        """VIX と IVR パーセンタイルからデルタ目標を動的算出する。"""
        call_delta = self._cfg.call_delta_base
        put_delta = self._cfg.put_delta_base
        if vix >= self._cfg.vix_high_threshold:
            call_delta = max(0.10, call_delta - 0.03)
            put_delta = max(0.10, put_delta - 0.03)
            log.info(
                "[IronCondorSellEngine] 高 VIX (%.1f) デルタ縮小: -> %.2f",
                vix, call_delta,
            )
        if ivr_pct >= 70.0:
            call_delta = min(0.35, call_delta + 0.03)
            put_delta = min(0.35, put_delta + 0.03)
            log.info(
                "[IronCondorSellEngine] 高 IVR (%d%%ile) デルタ拡大: -> %.2f",
                int(ivr_pct), call_delta,
            )
        return round(call_delta, 4), round(put_delta, 4)

    def _calc_dynamic_width(self, symbol: str) -> int:
        """ATR(14) から動的スプレッド幅を算出する。"""
        try:
            atr = (
                self._eng.get_symbol_atr(symbol, period=14)
                if hasattr(self._eng, "get_symbol_atr") else None
            )
        except Exception:
            atr = None
        if atr is None:
            return self._cfg.width_default
        raw = atr * self._cfg.width_atr_mult
        rounded = round(raw)
        width = max(self._cfg.width_min, max(1, int(rounded)))
        log.info(
            "[IronCondorSellEngine] %s ATR=%.2f x %.2f -> width=%d",
            symbol, atr, self._cfg.width_atr_mult, width,
        )
        return width

    def _calc_capital_pct(self, vix: float) -> float:
        """VIX に応じた資本配分率を返す。"""
        if vix >= self._cfg.vix_high_threshold:
            return self._cfg.capital_pct_high
        return self._cfg.capital_pct_base

    def _calc_qty(self, cash: float, spread_width: int, capital_pct: float) -> int:
        """発注枚数を算出する。"""
        if cash <= 0 or spread_width <= 0:
            return 1
        raw_qty = int(cash * capital_pct / (spread_width * 100))
        qty = max(1, raw_qty)
        max_qty = self._cfg.max_qty_paper if self.paper else self._cfg.max_qty
        if cash <= self._cfg.small_account_usd:
            max_qty = 1
        qty = min(qty, max_qty)
        log.info(
            "[IronCondorSellEngine] qty=%d (cash=%.0f width=%d pct=%.2f)",
            qty, cash, spread_width, capital_pct,
        )
        return qty

    def _check_consecutive_losses(self) -> bool:
        """直近 N 回がすべて損失なら True を返す（spy_bot._ic_sell_check_consecutive_losses 相当）。

        本実装はストレージに依存しないため、サブクラスや DI で override 可能。
        デフォルトは False（ブロックしない）。
        """
        return False

    def _is_vix_spike_30_day(self, current_vix: float) -> bool:
        """VIX 30% スパイク翌日かどうかを判定する。

        判定を持たない場合は False を返す（spy_bot.is_vix_spike_30_day 相当）。
        サブクラスや DI で override して実 spy_bot 判定を注入できる。
        """
        return False

    def _estimate_current_value(
        self,
        pos: IronCondorSellPosition,
        now_et: Optional[datetime] = None,
    ) -> Optional[float]:
        """IC 全体の現在買い戻しコストを推計する。

        dry_test モードでは時間経過による線形 decay でシミュレートする。

        Returns:
            現在価値 (float) または None（取得不能）
        """
        if self.dry_test:
            t = now_et or datetime.now(ET)
            start = t.replace(hour=9, minute=30, second=0, microsecond=0)
            end = t.replace(hour=15, minute=50, second=0, microsecond=0)
            total = max((end - start).total_seconds(), 1.0)
            elp = max(0.0, (t - start).total_seconds())
            decay = min(elp / total, 1.0)
            return pos.net_credit * (1.0 - decay * 0.8)

        try:
            today_str = (now_et or datetime.now(ET)).strftime("%Y-%m-%d")

            def _mid_by_strike(chain_type: str, strike: float) -> Optional[float]:
                chain = self._eng.get_option_chain_with_greeks(today_str, chain_type)
                if not chain:
                    return None
                opt = self._eng.find_by_strike(chain, strike)
                if not opt:
                    return None
                bid = opt.get("bid_price", 0.0)
                ask = opt.get("ask_price", 0.0)
                return round((bid + ask) / 2, 4) if ask > 0 else None

            cs = _mid_by_strike("CALL", pos.call_sell_strike)
            cb = _mid_by_strike("CALL", pos.call_buy_strike)
            ps = _mid_by_strike("PUT", pos.put_sell_strike)
            pb = _mid_by_strike("PUT", pos.put_buy_strike)
            if any(v is None for v in [cs, cb, ps, pb]):
                return None
            return round((cs - cb) + (ps - pb), 4)
        except Exception as exc:
            log.debug("[IronCondorSellEngine._estimate_current_value] %s", exc)
            return None

    def _close_position(
        self,
        pos: IronCondorSellPosition,
        reason: str,
        now_et: Optional[datetime] = None,
    ) -> bool:
        """IC ポジションを全 4 leg クローズする。

        dry_test / NoOp モードでは実発注なし。

        Returns:
            True — クローズ成功 / False — 一部失敗（ログに詳細）
        """
        t = now_et or datetime.now(ET)
        ticker = pos.symbol.replace("US.", "").replace(".", "")
        log.info(
            "[IronCondorSellEngine._close_position] %s reason=%s pos=%s/%s/%s/%s x%d",
            ticker, reason,
            pos.call_sell_strike, pos.call_buy_strike,
            pos.put_sell_strike, pos.put_buy_strike,
            pos.qty,
        )

        if self.dry_test:
            self.position = None
            self.trade_done = True
            return True

        legs = [
            (pos.put_buy_code, "SELL", "ic_put_buy_cover"),
            (pos.put_sell_code, "BUY", "ic_put_sell_cover"),
            (pos.call_buy_code, "SELL", "ic_call_buy_cover"),
            (pos.call_sell_code, "BUY", "ic_call_sell_cover"),
        ]
        close_ok = True
        for code, side_str, label in legs:
            try:
                oid, fill_method = self._eng._place_single_leg(
                    code, side_str, pos.qty, label,
                    init_price=None, use_limit=False,
                )
                if oid is None:
                    log.error(
                        "[IronCondorSellEngine._close_position] %s NG fill=%s",
                        label, fill_method,
                    )
                    close_ok = False
                else:
                    log.info(
                        "[IronCondorSellEngine._close_position] %s OK oid=%s",
                        label, oid,
                    )
            except Exception as exc:
                log.error(
                    "[IronCondorSellEngine._close_position] %s 例外: %s", label, exc
                )
                close_ok = False

        self.position = None
        self.trade_done = True
        return close_ok

    def _unwind_put_leg(
        self,
        put_sell_opt: dict,
        put_buy_opt: dict,
        qty: int,
    ) -> bool:
        """CALL CS 発注失敗後に PUT 脚を逆方向で巻き戻す（C2-B2 修正相当）。

        Returns:
            True — 巻き戻し成功 / False — 失敗（手動決済が必要）
        """
        if self.dry_test:
            log.info("[IronCondorSellEngine._unwind_put_leg][DRY-TEST] 巻き戻し成功")
            return True

        try:
            oid1, f1 = self._eng._place_single_leg(
                put_sell_opt["code"], "BUY", qty, "ic_put_sell_reverse",
                init_price=put_sell_opt.get("bid_price"), use_limit=True,
            )
            oid2, f2 = self._eng._place_single_leg(
                put_buy_opt["code"], "SELL", qty, "ic_put_buy_reverse",
                init_price=put_buy_opt.get("bid_price"), use_limit=True,
            )
            success = bool(oid1 and oid2 and f1 != "failed" and f2 != "failed")
            if success:
                log.info("[IronCondorSellEngine._unwind_put_leg] 巻き戻し成功")
            else:
                log.error(
                    "[IronCondorSellEngine._unwind_put_leg] 巻き戻し失敗 → 手動決済要: "
                    "PUT SELL %s / PUT BUY %s",
                    put_sell_opt["code"], put_buy_opt["code"],
                )
            return success
        except Exception as exc:
            log.error("[IronCondorSellEngine._unwind_put_leg] 例外: %s", exc)
            return False

    def _build_dry_position(
        self,
        symbol: str,
        today_str: str,
        qty: int,
        spread_width: int,
        vix: float,
        call_delta: float,
        put_delta: float,
        signal_id: str,
    ) -> IronCondorSellPosition:
        """dry_test 用の固定ポジションを生成する。"""
        spy_price = 560.0
        atm = round(spy_price)
        cs_strike = float(atm + spread_width * 2)
        cb_strike = cs_strike + spread_width
        ps_strike = float(atm - spread_width * 2)
        pb_strike = ps_strike - spread_width
        ticker = symbol.replace("US.", "").replace(".", "")
        dt = datetime.now(ET).strftime("%y%m%d")
        cs_code = f"US.{ticker}{dt}C{int(cs_strike * 1000)}"
        cb_code = f"US.{ticker}{dt}C{int(cb_strike * 1000)}"
        ps_code = f"US.{ticker}{dt}P{int(ps_strike * 1000)}"
        pb_code = f"US.{ticker}{dt}P{int(pb_strike * 1000)}"

        pos = IronCondorSellPosition(
            symbol=symbol,
            expiry=today_str,
            qty=qty,
            call_sell_code=cs_code,
            call_buy_code=cb_code,
            put_sell_code=ps_code,
            put_buy_code=pb_code,
            call_sell_strike=cs_strike,
            call_buy_strike=cb_strike,
            put_sell_strike=ps_strike,
            put_buy_strike=pb_strike,
            call_net_credit=0.40,
            put_net_credit=0.40,
            spread_width=spread_width,
            vix=vix,
        )
        self.entry_done = True
        self.trade_done = True
        self.position = pos
        log.info(
            "[IronCondorSellEngine._build_dry_position] CALL %s/%s PUT %s/%s "
            "credit=%.4f x%d signal_id=%s",
            cs_strike, cb_strike, ps_strike, pb_strike,
            pos.net_credit, qty, signal_id,
        )
        return pos
