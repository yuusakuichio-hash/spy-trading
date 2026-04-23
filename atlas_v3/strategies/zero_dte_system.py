"""atlas_v3/strategies/zero_dte_system.py — 0DTE System 戦術（Type C: StateCarrying）

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 / ADR-013 v2 / Gemini A2 ペルソナ
     Sprint 1-B Phase B A2: 0DTE システム・スキャルピング

戦術分類: Type C (state_carrying)
理由: 0DTE 日内状態（ORB レンジ / Gamma Level / 日次損益）を state として保持。
     再起動後も当日 state を復元して継続できること。

ストラクチャー:
    Iron Fly        — VIX 中位・GEX 絶対値大・pin risk
    Credit Spread   — 方向性バイアス + ORB ブレイクアウト
    Butterfly       — 超低 VIX・レンジ相場

エントリートリガー:
    1. ORB ブレイクアウト（09:45-10:00 ET 確定後）
    2. VIX / VWAP / Gamma Level 反発突破の複合フィルタ
    3. CPI/FOMC 発表日: IV Crush 狙い → only credit spread / iron fly (no long premium)

損切り:
    - プレミアム 50% 逆行（short structure は損失がクレジットの 50% に到達）
    - 強制クローズ: 15:30 ET（終値まで 30 分で delta exposure 除去）

Shadow Live 考慮（o3 警告反映）:
    - Paper 発注と実弾発注を同時記録して fill 乖離をモニタリングする経路を設計上確保
    - shadow_live_mode=True のとき paper + live の両発注 request を返す（Phase 2 で broker 接続）

StateCarryingTactic Protocol 実装:
    observe(env, market_data) → ORB レンジ・Gamma Level 観測・state 保持
    should_enter(env, symbol_candidates) → list[ZeroDTEEntryDecision]
    build_order(decision) → OrderRequest
    should_exit(position, env) → ZeroDTEExitDecision
    build_exit_order(position, decision) → OrderRequest
    persist_state(storage) → StorageBackend への永続化
    restore_state(storage) → StorageBackend から state 復元

必須要件:
- TacticBase ABC 継承
- slippage_tolerance_bps config 必須
- idempotency_key は OrderRequest に必ず設定
- kill_switch 連動（preflight + should_exit の両方でチェック）
- CC ≤ 20 規律

ORB 転用: atlas_v3/strategies/orb_1dte_spy.py の ORBRange / ORB 判定ロジック（L151 以降）を
          本戦術の entry trigger として転用。orb_1dte_spy.py は deprecated 候補として
          data/research_v3/orb_1dte_spy_disposition_20260423.md に記録済み。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from atlas_v3.strategies.orb_1dte_spy import ORBRange  # ORB 転用
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: 強制クローズ時刻（ET 時・分）。15:30 ET = 上場終値まで 30 分前。
FORCE_CLOSE_HOUR_ET: int = 15
FORCE_CLOSE_MINUTE_ET: int = 30

#: Gamma Level 反発確認の最小 GEX 絶対値（Iron Fly 適合判定用）
GAMMA_LEVEL_GEX_ABS_MIN: float = 0.5

#: daily_pnl_stop (=Daily Stop): 当日累積損失がこの値に達したら全ポジション強制クローズ
#: 実値は ZeroDTEConfig.daily_stop_loss で override する
_DEFAULT_DAILY_STOP: float = -2000.0


# ---------------------------------------------------------------------------
# StorageBackend Protocol（orb_1dte_spy と同一 contract）
# ---------------------------------------------------------------------------

@runtime_checkable
class StorageBackend(Protocol):
    """state 永続化バックエンド Protocol。"""
    def save(self, key: str, data: dict) -> None: ...
    def load(self, key: str) -> dict | None: ...


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ZeroDTEConfig:
    """0DTE System 戦術設定。

    Attributes:
        slippage_tolerance_bps:   スリッページ許容幅（basis points）
        vix_max:                  エントリー最大 VIX（これ以上は long premium 禁止）
        vix_iron_fly_max:         Iron Fly 採用最大 VIX
        vix_credit_spread_max:    Credit Spread 採用最大 VIX
        stop_loss_premium_pct:    プレミアム逆行損切り水準（0.5 = 50%）
        profit_target_pct:        利確目標（クレジット比。credit structure 用）
        orb_window_minutes:       ORB 観測窓（分）。9:30-9:45 ET = 15 分
        force_close_hour_et:      強制クローズ時（ET・24h 表記）
        force_close_minute_et:    強制クローズ分（ET）
        daily_stop_loss:          Daily Stop 金額（負値）
        quantity:                 デフォルト発注枚数
        shadow_live_mode:         True のとき paper+live 両 request 生成（fill 乖離計測）
        iv_crush_mode:            CPI/FOMC 発表日 IV Crush モード。long premium 禁止
    """
    slippage_tolerance_bps: int = 20
    vix_max: float = 35.0
    vix_iron_fly_max: float = 25.0
    vix_credit_spread_max: float = 35.0
    stop_loss_premium_pct: float = 0.50    # 50% premium 逆行で損切り
    profit_target_pct: float = 0.50        # credit の 50% 利確
    orb_window_minutes: int = 15           # 09:30-09:45 ET
    force_close_hour_et: int = FORCE_CLOSE_HOUR_ET
    force_close_minute_et: int = FORCE_CLOSE_MINUTE_ET
    daily_stop_loss: float = _DEFAULT_DAILY_STOP
    quantity: int = 1
    shadow_live_mode: bool = False
    iv_crush_mode: bool = False


# ---------------------------------------------------------------------------
# ストラクチャー選択 Enum
# ---------------------------------------------------------------------------

StructureType = Literal["iron_fly", "credit_spread", "butterfly", "none"]


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ZeroDTEEntryDecision:
    """0DTE エントリー決定。

    Attributes:
        should_enter:     True のときエントリー
        symbol:           対象銘柄
        side:             "sell"（credit structure）/ "buy"（net long structure）
        quantity:         発注枚数
        structure:        Iron Fly / Credit Spread / Butterfly
        direction:        Credit Spread のときの方向（"call_spread" or "put_spread"）
        orb_high:         ORB High（転用）
        orb_low:          ORB Low（転用）
        current_price:    現在価格（proxy）
        gamma_level:      Gamma Level 観測値（GEX proxy）
        reason:           エントリー根拠テキスト
        idempotency_key:  冪等性キー
        shadow_live:      True のとき shadow live 経路（fill 乖離計測用）
    """
    should_enter: bool
    symbol: str
    side: str = "sell"
    quantity: int = 1
    structure: StructureType = "none"
    direction: Literal["call_spread", "put_spread", "none"] = "none"
    orb_high: float = 0.0
    orb_low: float = 0.0
    current_price: float = 0.0
    gamma_level: float = 0.0
    reason: str = ""
    idempotency_key: str = ""
    shadow_live: bool = False


@dataclass(frozen=True)
class ZeroDTEExitDecision:
    """0DTE エグジット決定。

    Attributes:
        should_exit:  True のとき exit
        reason:       exit 根拠テキスト
        exit_type:    "profit_target" / "stop_loss" / "force_close" /
                      "daily_stop" / "eod_close" / "none"
    """
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "profit_target", "stop_loss", "force_close",
        "daily_stop", "eod_close", "none"
    ] = "none"


# ---------------------------------------------------------------------------
# Position stub（ic_sell.py / orb_1dte_spy.py と同等）
# ---------------------------------------------------------------------------

@dataclass
class ZeroDTEPosition:
    """0DTE ポジション表現（最小 stub）。

    Attributes:
        symbol:          銘柄
        quantity:        枚数
        entry_price:     エントリー価格（premium 支払いまたは受取）
        current_price:   現在価格
        tactic_name:     戦術名
        entry_time:      エントリー時刻（UTC）
        unrealized_pnl:  未実現損益
        max_credit:      Credit structure 受取クレジット（credit structure 用）
        structure:       Iron Fly / Credit Spread / Butterfly
        direction:       Credit Spread 方向
        pos_direction:   ポジション方向 — "credit"（売り建て）/ "long"（買い建て）。
                         C-11 修正: build_exit_order の side 決定に使用。
                         credit → buy_to_close（side="buy"）
                         long  → sell_to_close（side="sell"）
    """
    symbol: str
    quantity: int
    entry_price: float
    current_price: float = 0.0
    tactic_name: str = ""
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: float = 0.0
    max_credit: float = 0.0
    structure: StructureType = "none"
    direction: Literal["call_spread", "put_spread", "none"] = "none"
    pos_direction: Literal["credit", "long"] = "credit"


# ---------------------------------------------------------------------------
# ZeroDTESystemTactic — Type C: StateCarrying
# ---------------------------------------------------------------------------

class ZeroDTESystemTactic(TacticBase):
    """0DTE System 戦術（Type C: state_carrying）。

    ADR-013 v2 / Gemini ペルソナ A2 準拠。

    ORB ブレイクアウト（orb_1dte_spy.py 転用）+ VIX/VWAP/Gamma Level フィルタにより
    当日満期（0DTE）オプション構造体（Iron Fly / Credit Spread / Butterfly）の
    エントリーを判断する。

    Shadow Live モード（o3 警告対応）:
        shadow_live_mode=True のとき、paper と live の両発注 request を
        fill 乖離計測用に生成する経路を設計上確保している。
        Phase 2 で BrokerClient 接続時に乖離 log を起動する。

    Args:
        config: ZeroDTEConfig（slippage_tolerance_bps 必須）
    """

    _STATE_KEY = "0dte_system_state"

    def __init__(self, config: ZeroDTEConfig | None = None) -> None:
        self._cfg = config or ZeroDTEConfig()
        # symbol → ORBRange（orb_1dte_spy 転用）
        self._orb_ranges: dict[str, ORBRange] = {}
        # 当日累積損益（daily_stop_loss 判定用）
        self._daily_pnl: float = 0.0
        # C-5: _daily_pnl への並行 read-modify-write を防ぐロック
        self._pnl_lock: threading.Lock = threading.Lock()
        # Gamma Level 観測値（GEX proxy）: symbol → float
        self._gamma_levels: dict[str, float] = {}

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "state_carrying"

    @property
    def tactic_name(self) -> str:
        return "0dte_system"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック:
        1. Kill Switch ARMED → False
        2. VIX > vix_max → False（高恐怖環境は 0DTE 非適合）
        3. env None → False
        4. daily_pnl <= daily_stop_loss → False（Daily Stop 発動中）

        Returns:
            True  — 戦術発動可能
            False — 発動不可（log に理由出力済み）
        """
        if env is None:
            log.warning("[ZeroDTESystemTactic.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[ZeroDTESystemTactic.preflight] Kill Switch ARMED: 0dte_system 無効化"
            )
            return False

        if env.vix > self._cfg.vix_max:
            log.info(
                "[ZeroDTESystemTactic.preflight] VIX=%.1f > max=%.1f: "
                "高恐怖環境・0DTE スキップ",
                env.vix,
                self._cfg.vix_max,
            )
            return False

        # C-4: 境界到達（==）で即停止するため < ではなく <= を使わず
        #      daily_pnl < stop（stop に到達した時点で停止・-1999.99 > -2000 は継続しない）
        #      注意: stop_loss は負値。daily_pnl <= stop_loss で停止（境界値含む）
        if self._daily_pnl <= self._cfg.daily_stop_loss:
            log.warning(
                "[ZeroDTESystemTactic.preflight] Daily Stop 発動: "
                "daily_pnl=%.2f <= stop=%.2f",
                self._daily_pnl,
                self._cfg.daily_stop_loss,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # StateCarryingTactic Protocol 実装
    # ------------------------------------------------------------------

    def observe(self, env: MarketEnvironment, market_data: Any) -> None:
        """ORB レンジ + Gamma Level を観測・state 更新する。

        market_data に get_orb_range(symbol, window_minutes) があれば ORB を更新。
        market_data に get_gex(symbol) があれば Gamma Level を更新。
        どちらも未実装の場合は既存 state を保持（Phase 2 で market_data 連携を追加）。

        Args:
            env:         現在の市場環境（MarketEnvironment）
            market_data: MarketDataClient（Phase 2 で futu/moomoo 分足データ連携）
        """
        self._observe_orb(market_data)
        self._observe_gamma(env, market_data)

    def _observe_orb(self, market_data: Any) -> None:
        """ORB レンジ観測サブルーチン（CC 削減のため抽出）。"""
        if not hasattr(market_data, "get_orb_range"):
            log.debug(
                "[ZeroDTESystemTactic._observe_orb] market_data.get_orb_range 未実装: "
                "既存 ORB state 保持 (%d 銘柄)",
                len(self._orb_ranges),
            )
            return

        symbols = getattr(market_data, "tracked_symbols", list(self._orb_ranges.keys()))
        for symbol in symbols:
            try:
                raw: dict = market_data.get_orb_range(
                    symbol, window_minutes=self._cfg.orb_window_minutes
                )
                self._orb_ranges[symbol] = ORBRange(
                    high=raw["high"],
                    low=raw["low"],
                    is_confirmed=raw.get("is_confirmed", False),
                    observed_at=datetime.now(timezone.utc),
                    symbol=symbol,
                )
                log.info(
                    "[ZeroDTESystemTactic._observe_orb] ORB 更新: %s H=%.2f L=%.2f "
                    "confirmed=%s",
                    symbol,
                    raw["high"],
                    raw["low"],
                    raw.get("is_confirmed", False),
                )
            except Exception as exc:
                log.error(
                    "[ZeroDTESystemTactic._observe_orb] ORB 取得失敗: %s %s (既存 state 保持)",
                    symbol,
                    exc,
                )
                raise

    def _observe_gamma(self, env: MarketEnvironment, market_data: Any) -> None:
        """Gamma Level（GEX proxy）観測サブルーチン（CC 削減のため抽出）。

        market_data.get_gex(symbol) があれば更新。
        なければ env.gex を全銘柄に適用するフォールバック。
        """
        if hasattr(market_data, "get_gex"):
            symbols = getattr(market_data, "tracked_symbols", list(self._gamma_levels.keys()))
            for symbol in symbols:
                try:
                    self._gamma_levels[symbol] = market_data.get_gex(symbol)
                except Exception as exc:
                    log.error(
                        "[ZeroDTESystemTactic._observe_gamma] GEX 取得失敗: %s %s",
                        symbol, exc,
                    )
        else:
            # env.gex をフォールバックとして全銘柄に適用
            for symbol in list(self._orb_ranges.keys()):
                self._gamma_levels[symbol] = env.gex

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol_candidates: list[str],
    ) -> list[ZeroDTEEntryDecision]:
        """複数 symbol 評価・0DTE エントリー判定。

        各銘柄に対して:
        1. ORB ブレイクアウト判定（orb_1dte_spy 転用ロジック）
        2. VIX/GEX ベースのストラクチャー選択
        3. iv_crush_mode のとき long premium 禁止フィルタ

        Args:
            env:               現在の市場環境
            symbol_candidates: SymbolSelector から渡された候補銘柄リスト

        Returns:
            エントリー可能な銘柄の決定リスト
        """
        decisions: list[ZeroDTEEntryDecision] = []
        for symbol in symbol_candidates:
            orb = self._orb_ranges.get(symbol)
            gamma = self._gamma_levels.get(symbol, env.gex)
            decision = self._evaluate_entry(env, symbol, orb, gamma)
            decisions.append(decision)
        return decisions

    def _evaluate_entry(
        self,
        env: MarketEnvironment,
        symbol: str,
        orb: ORBRange | None,
        gamma_level: float,
    ) -> ZeroDTEEntryDecision:
        """単一銘柄の 0DTE エントリー評価（CC ≤ 20 のため抽出）。

        判定順:
        1. ORB 未確定 → no_entry
        2. VIX → ストラクチャー選択
        3. ORB ブレイクアウト方向 → direction 確定
        4. Gamma Level 反発 → Iron Fly 強化判定
        5. idempotency_key 生成・決定返却
        """
        if orb is None or not orb.is_confirmed:
            return ZeroDTEEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="ORB 未確定・観測待ち",
            )

        structure = self._select_structure(env, gamma_level)
        if structure == "none":
            return ZeroDTEEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"ストラクチャー選択不可: VIX={env.vix:.1f} GEX={gamma_level:.2f}",
            )

        # C-1 修正: env.current_price_by_symbol があれば実価格取得・なければ None
        current_price: float | None = None
        cpbs: dict[str, float] | None = getattr(env, "current_price_by_symbol", None)
        if cpbs is not None:
            current_price = cpbs.get(symbol)

        direction, breakout_reason = self._orb_breakout_direction(orb, current_price)

        # Credit Spread は方向性が必要（ORB ブレイクアウトが前提）
        if structure == "credit_spread" and direction == "none":
            return ZeroDTEEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="Credit Spread: ORB ブレイクアウトなし",
            )

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[ZeroDTESystemTactic._evaluate_entry] エントリー決定: %s "
            "structure=%s direction=%s VIX=%.1f GEX=%.2f key=%s",
            symbol, structure, direction, env.vix, gamma_level, idem_key,
        )
        return ZeroDTEEntryDecision(
            should_enter=True,
            symbol=symbol,
            side="sell",
            quantity=self._cfg.quantity,
            structure=structure,
            direction=direction,  # type: ignore[arg-type]
            orb_high=orb.high,
            orb_low=orb.low,
            current_price=current_price if current_price is not None else 0.0,
            gamma_level=gamma_level,
            reason=f"{structure}/{direction}: {breakout_reason} VIX={env.vix:.1f}",
            idempotency_key=idem_key,
            shadow_live=self._cfg.shadow_live_mode,
        )

    def _select_structure(
        self,
        env: MarketEnvironment,
        gamma_level: float,
    ) -> StructureType:
        """VIX / GEX からストラクチャーを選択する（CC 削減のため抽出）。

        Iron Fly:       VIX <= vix_iron_fly_max かつ |GEX| >= GAMMA_LEVEL_GEX_ABS_MIN
        Butterfly:      VIX < 15 かつ env.bias == "neutral"
        Credit Spread:  VIX <= vix_credit_spread_max（方向性バイアス）
        none:           条件不一致（vix_max 超えは preflight で既にブロック済み）
        """
        if env.vix < 15.0 and env.bias == "neutral":
            return "butterfly"
        if env.vix <= self._cfg.vix_iron_fly_max and abs(gamma_level) >= GAMMA_LEVEL_GEX_ABS_MIN:
            return "iron_fly"
        if env.vix <= self._cfg.vix_credit_spread_max:
            return "credit_spread"
        return "none"

    def _orb_breakout_direction(
        self,
        orb: ORBRange,
        current_price: float | None = None,
    ) -> tuple[Literal["call_spread", "put_spread", "none"], str]:
        """ORB ブレイクアウト方向を判定する（orb_1dte_spy.py L317-L334 転用）。

        C-1 修正: current_price 引数を追加。orb.high 固定を廃止。
        current_price=None のとき（Phase 1 互換・価格未取得）は "none" を返す。

        Args:
            orb:           ORBRange（確定済み）
            current_price: 実価格。None のとき方向判定不能 → "none"

        Returns:
            (direction, reason_text)
        """
        if orb.high <= 0:
            return "none", "ORB high=0 (無効)"

        if current_price is None:
            return "none", "current_price 未取得 (Phase 1 互換)"

        buffer = orb.high * 0.001   # 0.1% buffer（ORB1DTEConfig.breakout_buffer_pct 相当）

        if current_price > orb.high + buffer:
            return "call_spread", f"upper_breakout: price={current_price:.2f} > H={orb.high:.2f}"
        if current_price < orb.low - buffer:
            return "put_spread", f"lower_breakout: price={current_price:.2f} < L={orb.low:.2f}"
        return "none", f"range_bound: price={current_price:.2f} ORB={orb.low:.2f}-{orb.high:.2f}"

    def build_order(self, decision: ZeroDTEEntryDecision) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest

        if not decision.should_enter:
            raise ValueError(
                f"[ZeroDTESystemTactic.build_order] should_enter=False: {decision}"
            )

        return OrderRequest(
            symbol=decision.symbol,
            side=decision.side,
            quantity=decision.quantity,
            order_type="limit",
            tactic_name=self.tactic_name,
            idempotency_key=decision.idempotency_key,
        )

    def should_exit(
        self,
        position: ZeroDTEPosition,
        env: MarketEnvironment,
        current_et_hour: int | None = None,
        current_et_minute: int | None = None,
    ) -> ZeroDTEExitDecision:
        """エグジット判定。

        判定順:
        1. Kill Switch ARMED → force_close
        2. daily_pnl <= daily_stop_loss → daily_stop（全ポジション停止）
        3. 強制クローズ時刻（15:30 ET）→ eod_close
        4. プレミアム 50% 逆行 → stop_loss
        5. 利確目標到達 → profit_target

        Args:
            position:          現在ポジション
            env:               現在の市場環境
            current_et_hour:   現在の ET 時（テスト用・None のとき UTC から換算しない）
            current_et_minute: 現在の ET 分（テスト用）
        """
        if kill_switch_is_active():
            log.warning(
                "[ZeroDTESystemTactic.should_exit] Kill Switch ARMED: 強制クローズ (%s)",
                position.symbol,
            )
            return ZeroDTEExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        if self._daily_pnl <= self._cfg.daily_stop_loss:
            log.warning(
                "[ZeroDTESystemTactic.should_exit] Daily Stop 発動: "
                "daily_pnl=%.2f <= stop=%.2f (%s)",
                self._daily_pnl, self._cfg.daily_stop_loss, position.symbol,
            )
            return ZeroDTEExitDecision(
                should_exit=True,
                reason=f"daily_stop: pnl={self._daily_pnl:.2f}",
                exit_type="daily_stop",
            )

        # 15:30 ET 強制クローズ
        if self._is_force_close_time(current_et_hour, current_et_minute):
            # None のとき ET 実時刻を取得してログ・reason に使う
            if current_et_hour is None or current_et_minute is None:
                _et = datetime.now(ZoneInfo("America/New_York"))
                _h, _m = _et.hour, _et.minute
            else:
                _h, _m = current_et_hour, current_et_minute
            log.info(
                "[ZeroDTESystemTactic.should_exit] 時刻強制クローズ: %02d:%02d ET (%s)",
                _h, _m, position.symbol,
            )
            return ZeroDTEExitDecision(
                should_exit=True,
                reason=f"force_close_time: {_h:02d}:{_m:02d} ET",
                exit_type="eod_close",
            )

        return self._check_pnl_exit(position)

    def _is_force_close_time(
        self,
        hour: int | None,
        minute: int | None,
        et_date: "datetime | None" = None,
    ) -> bool:
        """15:30–15:59 ET（RTH 終了前 30 分）かどうかを判定する（CC 削減のため抽出）。

        C-2 修正: hour/minute が None のとき datetime.now(ET) で内部換算する。
        これにより UTC 渡し誤りや DST 混同を防ぐ（UTC 渡しで 15:30 UTC = 11:30 ET
        の早期クローズを回避）。

        C-6 修正: 旧実装では `hour > force_close_hour_et`（=15）が 16:00–23:59 すべて
        True になっていた（restore 直後の 22:00 ET で eod_close 連打が発生）。
        修正後は RTH 終了時刻（15:30 ET）以降かつ当日 RTH 内（hour < 16）のみ True。
        具体的には `hour == 15 and minute >= 30` のみを RTH 強制クローズ対象とし、
        16:00 ET 以降はアフターアワー／翌日判定として False を返す。
        同日判定（et_date）も追加して前日 restore 後の誤発動を防ぐ。

        Args:
            hour:    ET 時（None のとき内部取得）
            minute:  ET 分（None のとき内部取得）
            et_date: 比較対象の ET 日付（None のとき内部取得した今日 ET）。
                     呼出側から渡すことで単体テストが可能。
        """
        if hour is None or minute is None:
            et_now = datetime.now(ZoneInfo("America/New_York"))
            hour = et_now.hour
            minute = et_now.minute

        # C-6: RTH 終了は 15:30–15:59 ET のみ。16:00 以降はアフターアワー → False
        return (
            hour == self._cfg.force_close_hour_et
            and minute >= self._cfg.force_close_minute_et
        )

    def _check_pnl_exit(self, position: ZeroDTEPosition) -> ZeroDTEExitDecision:
        """PnL ベースの exit 判定（profit_target / stop_loss）。CC 削減のため抽出。

        C-3 修正: max_credit=0 かつ entry_price=0 は「不正 state」として即 exit。
        ガンマ爆発 pin 張り付きリスクを防ぐため force_close 相当で処理する。
        """
        if position.max_credit <= 0 and position.entry_price <= 0:
            log.error(
                "[ZeroDTESystemTactic._check_pnl_exit] 不正 state: "
                "max_credit=%.2f entry_price=%.2f → 即時強制クローズ (%s)",
                position.max_credit, position.entry_price, position.symbol,
            )
            return ZeroDTEExitDecision(
                should_exit=True,
                reason="invalid_state: max_credit=0 and entry_price=0",
                exit_type="force_close",
            )

        # credit structure: max_credit 基準
        if position.max_credit > 0:
            return self._check_credit_pnl_exit(position)

        # long structure: entry_price 基準（止むなし）
        return self._check_long_pnl_exit(position)

    def _check_credit_pnl_exit(self, position: ZeroDTEPosition) -> ZeroDTEExitDecision:
        """Credit structure の PnL exit 判定。"""
        profit_threshold = position.max_credit * self._cfg.profit_target_pct
        loss_threshold = -position.max_credit * self._cfg.stop_loss_premium_pct

        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[ZeroDTESystemTactic] 利確: pnl=%.2f >= target=%.2f (%s)",
                position.unrealized_pnl, profit_threshold, position.symbol,
            )
            return ZeroDTEExitDecision(
                should_exit=True,
                reason=f"profit_target: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[ZeroDTESystemTactic] 損切り: pnl=%.2f <= stop=%.2f (%s)",
                position.unrealized_pnl, loss_threshold, position.symbol,
            )
            return ZeroDTEExitDecision(
                should_exit=True,
                reason=f"stop_loss: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        return ZeroDTEExitDecision(should_exit=False, reason="holding", exit_type="none")

    def _check_long_pnl_exit(self, position: ZeroDTEPosition) -> ZeroDTEExitDecision:
        """Long structure の PnL exit 判定（entry_price 基準）。"""
        stop_threshold = -position.entry_price * self._cfg.stop_loss_premium_pct
        profit_threshold = position.entry_price * self._cfg.profit_target_pct

        if position.unrealized_pnl >= profit_threshold:
            return ZeroDTEExitDecision(
                should_exit=True,
                reason=f"profit_target_long: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        if position.unrealized_pnl <= stop_threshold:
            return ZeroDTEExitDecision(
                should_exit=True,
                reason=f"stop_loss_long: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        return ZeroDTEExitDecision(should_exit=False, reason="holding", exit_type="none")

    def build_exit_order(
        self,
        position: ZeroDTEPosition,
        decision: ZeroDTEExitDecision,
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。

        C-11 修正: pos_direction に応じて exit side を切り替える。
            credit（売り建て）→ buy_to_close（side="buy"）
            long（買い建て）  → sell_to_close（side="sell"）
        idem key に side を含めることで credit/long 混在時の key 衝突を防ぐ。
        """
        from atlas_v3.core.engine import OrderRequest

        if not decision.should_exit:
            raise ValueError(
                "[ZeroDTESystemTactic.build_exit_order] should_exit=False"
            )

        # C-11: pos_direction で exit side を決定
        exit_side = "buy" if position.pos_direction == "credit" else "sell"

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        # C-11: side を strategy 文字列に含めて key 衝突を防ぐ
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit_{exit_side}",
            symbol=position.symbol,
            trigger_time=trigger_time,
        )

        return OrderRequest(
            symbol=position.symbol,
            side=exit_side,
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # Daily PnL 管理
    # ------------------------------------------------------------------

    def update_daily_pnl(self, realized_pnl: float) -> None:
        """当日確定損益を累積する（ポジションクローズ時に呼ぶ）。

        C-5: 並行クローズによる read-modify-write 競合を _pnl_lock で原子化する。
        複数スレッドが同時に update_daily_pnl を呼んだ場合も正確な累積が保証される。

        Args:
            realized_pnl: 確定損益（プラス=利益・マイナス=損失）
        """
        with self._pnl_lock:
            self._daily_pnl += realized_pnl
            _total = self._daily_pnl
        log.info(
            "[ZeroDTESystemTactic.update_daily_pnl] "
            "daily_pnl 更新: delta=%.2f → total=%.2f (stop=%.2f)",
            realized_pnl, _total, self._cfg.daily_stop_loss,
        )

    def reset_daily_pnl(self) -> None:
        """日次リセット（翌日 00:00 ET に呼ぶ）。"""
        with self._pnl_lock:
            self._daily_pnl = 0.0
        log.info("[ZeroDTESystemTactic.reset_daily_pnl] 日次 PnL リセット")

    # ------------------------------------------------------------------
    # state 永続化 / 復元
    # ------------------------------------------------------------------

    def persist_state(self, storage: StorageBackend) -> None:
        """0DTE state を StorageBackend に永続化する（再起動耐性）。"""
        state_data: dict = {
            "orb_ranges": {
                sym: {
                    "high": orb.high,
                    "low": orb.low,
                    "is_confirmed": orb.is_confirmed,
                    "observed_at": orb.observed_at.isoformat(),
                    "symbol": orb.symbol,
                }
                for sym, orb in self._orb_ranges.items()
            },
            "gamma_levels": dict(self._gamma_levels),
            "daily_pnl": self._daily_pnl,
            "persisted_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            storage.save(self._STATE_KEY, state_data)
        except OSError as exc:
            log.error(
                "[ZeroDTESystemTactic.persist_state] state 永続化失敗 (OSError): %s",
                exc,
            )
            self._escalate_persist_failure(exc)
            raise
        except Exception as exc:  # noqa: BLE001
            log.error(
                "[ZeroDTESystemTactic.persist_state] state 永続化失敗 (unexpected): %s",
                exc,
            )
            self._escalate_persist_failure(exc)
            raise
        log.debug(
            "[ZeroDTESystemTactic.persist_state] state 永続化: "
            "%d 銘柄 daily_pnl=%.2f",
            len(self._orb_ranges),
            self._daily_pnl,
        )

    @staticmethod
    def _escalate_persist_failure(exc: Exception) -> None:
        """C-8: persist_state 失敗時の Pushover escalation。

        Pushover が利用不可でも二次例外を起こさない（log のみにフォールバック）。
        """
        try:
            from common.pushover_client import send as _pushover_send  # type: ignore[import]
            _pushover_send(
                title="[ZeroDTESystemTactic] CRITICAL: persist_state failure",
                message=f"0DTE state 永続化失敗 — 再起動後の state 復元不可。\n{exc}",
                priority=1,
            )
        except Exception as pushover_exc:  # noqa: BLE001
            log.error(
                "[ZeroDTESystemTactic._escalate_persist_failure] "
                "Pushover escalation 失敗 (non-critical): %s",
                pushover_exc,
            )

    def restore_state(self, storage: StorageBackend) -> None:
        """StorageBackend から 0DTE state を復元する（再起動後の継続）。"""
        data = storage.load(self._STATE_KEY)
        if data is None:
            log.info(
                "[ZeroDTESystemTactic.restore_state] 保存 state なし: 初期状態を使用"
            )
            return

        self._daily_pnl = data.get("daily_pnl", 0.0)
        self._gamma_levels = data.get("gamma_levels", {})

        et_tz = ZoneInfo("America/New_York")
        today_et = datetime.now(et_tz).date()

        for sym, raw in data.get("orb_ranges", {}).items():
            try:
                observed_at = datetime.fromisoformat(raw["observed_at"])
                # C-7: observed_at の ET 日付が今日でなければ confirmed をリセット。
                # 前日 ORB を当日朝に誤使用するのを防ぐ。
                observed_date_et = observed_at.astimezone(et_tz).date()
                is_confirmed = raw["is_confirmed"]
                if observed_date_et != today_et:
                    is_confirmed = False
                    log.warning(
                        "[ZeroDTESystemTactic.restore_state] 前日 ORB 検出 (%s): "
                        "observed_at=%s (ET %s) ≠ today=%s → confirmed リセット",
                        sym, observed_at.isoformat(), observed_date_et, today_et,
                    )
                self._orb_ranges[sym] = ORBRange(
                    high=raw["high"],
                    low=raw["low"],
                    is_confirmed=is_confirmed,
                    observed_at=observed_at,
                    symbol=raw["symbol"],
                )
            except (KeyError, ValueError) as exc:
                log.error(
                    "[ZeroDTESystemTactic.restore_state] state 復元失敗 (%s): %s",
                    sym, exc,
                )
                raise

        log.info(
            "[ZeroDTESystemTactic.restore_state] state 復元: "
            "%d 銘柄 daily_pnl=%.2f (保存: %s)",
            len(self._orb_ranges),
            self._daily_pnl,
            data.get("persisted_at", "unknown"),
        )
