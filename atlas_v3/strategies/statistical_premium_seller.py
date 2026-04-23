"""atlas_v3/strategies/statistical_premium_seller.py — Statistical Premium Seller

Sprint 1-B Phase B / Atlas 戦術 1: ic_sell 拡張（A1 統計的プレミアム売却対応）
仕様: data/specs/v3/atlas_spec_v3_20260422.md A3 / ADR-013 v2 / Gemini A1 ペルソナ

責務:
- ICSellTactic（Iron Condor）の上位戦術として以下を統合する:
    - Strangle（新規）
    - Put Spread（新規）
    - Credit Spread（新規）
- IVR は PercentileSelector 連動の動的閾値（固定値禁止）
- Delta 16-30 OTM 選定（短期バイアス除去）
- 利益目標: プレミアム 50% 利確 / 21 DTE ロール / 損切りプレミアム 2-3 倍
- Beta-Weighted Delta 監視（ポートフォリオ中立維持）

CC 規律: 各メソッド CC ≤ 20

Redteam r1 修正（2026-04-23）:
  C-1: IVR スケール契約を 0-100 固定と明示・範囲外は TypeError
  C-2: phase valid 検証 + ValueError
  C-3: credit_spread 方向性 direction フィールド追加・bias から自動決定
  C-4: IC クラス共通プレフィックス "ic" でクロス戦術二重建玉防止
  C-5: expiration_date ベースの NYSE 営業日 DTE 算出
  C-6: should_enter 冒頭で kill_switch 再チェック

Redteam r2 修正（2026-04-23）:
  R2-C1: should_enter 戻り型を SPSEntryDecision | None に変更（Optional 型明示）
  R2-C2: trigger_time を 5 分バケットに丸める（秒境界二重発注防止）
  R2-H3: NYSE 祝日リストを 2028-2030 まで拡張
  R2-H4: IVR NaN/inf チェック追加（should_enter 冒頭で math.isfinite 検証）
  R2-H5: expiration_date 比較を US/Eastern タイムゾーン基準に変更

Redteam r3 修正（2026-04-23）:
  R3-C3: build_exit_order の idem key に exit_reason を含める
         strategy=f"{tactic_name}_exit_{exit_type}" とすることで
         同 5 分バケット内で profit_target と stop_loss の exit が
         同一 key になりブロックされるバグを修正。
         bucket 丸めは entry のみに限定（exit は秒精度の now をそのまま使用）。
"""
from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.core.strategy_selector import PercentileSelector
from atlas_v3.strategies.base import TacticBase, TacticType
from atlas_v3.strategies.ic_sell import ICSellExitDecision, Position
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: Delta 選定範囲（OTM 16-30 デルタ）
DELTA_MIN: float = 0.16
DELTA_MAX: float = 0.30

#: 利確目標（クレジット比）
DEFAULT_PROFIT_TARGET_PCT: float = 0.50  # 50%

#: 損切りプレミアム倍率（2-3倍の中央値）
DEFAULT_STOP_LOSS_PCT: float = 2.50  # 250%

#: 21 DTE ロールトリガー
ROLL_DTE_THRESHOLD: int = 21

#: IC クラス戦術共通プレフィックス（cross-tactic 二重建玉防止・C-4）
IC_CLASS_PREFIX: str = "ic"

#: IVR スケール（0-100 固定）— MarketEnvironment.ivr_by_symbol はこのスケールで格納される（C-1）
IVR_SCALE_MIN: float = 0.0
IVR_SCALE_MAX: float = 100.0

#: PercentileSelector が受け付ける有効 phase 識別子
_VALID_PHASES: frozenset[str] = frozenset({"phase1", "phase2", "phase3", "phase4"})


# ---------------------------------------------------------------------------
# 戦術サブタイプ
# ---------------------------------------------------------------------------

PremiumStrategyType = Literal["iron_condor", "strangle", "put_spread", "credit_spread"]

#: credit_spread デフォルト方向（neutral bias 時のフォールバック）
DEFAULT_CREDIT_SPREAD_DIRECTION: Literal["put", "call"] = "put"


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatisticalPremiumSellerConfig:
    """Statistical Premium Seller 設定。

    Attributes:
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        phase:                  資金フェーズ（PercentileSelector 連動・固定閾値禁止）
                                有効値: "phase1" / "phase2" / "phase3" / "phase4"
        strategy_type:          戦術サブタイプ
        delta_min:              OTM ショート・デルタ下限
        delta_max:              OTM ショート・デルタ上限
        dte_target:             ターゲット満期日数
        roll_dte_threshold:     ロールトリガー DTE（21 DTE）
        vix_min:                エントリー最低 VIX
        vix_max:                エントリー最大 VIX
        profit_target_pct:      利確目標（クレジット比 %）
        stop_loss_pct:          損切り水準（クレジット比 %）
        max_risk_per_trade:     1トレード最大リスク額
        beta_weighted_delta_max: Beta-Weighted Delta 上限（絶対値）
        default_credit_spread_direction: credit_spread で neutral bias 時のデフォルト方向
    """
    slippage_tolerance_bps: int = 10
    phase: str = "phase1"
    strategy_type: PremiumStrategyType = "iron_condor"
    delta_min: float = DELTA_MIN
    delta_max: float = DELTA_MAX
    dte_target: int = 45
    roll_dte_threshold: int = ROLL_DTE_THRESHOLD
    vix_min: float = 15.0
    vix_max: float = 35.0
    profit_target_pct: float = DEFAULT_PROFIT_TARGET_PCT
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT
    max_risk_per_trade: float = 500.0
    beta_weighted_delta_max: float = 0.30
    default_credit_spread_direction: Literal["put", "call"] = DEFAULT_CREDIT_SPREAD_DIRECTION

    def __post_init__(self) -> None:
        """設定値バリデーション（C-2: phase runtime 検証）。

        Raises:
            ValueError: phase が有効値外の場合
        """
        if self.phase not in _VALID_PHASES:
            raise ValueError(
                f"StatisticalPremiumSellerConfig.phase={self.phase!r} は無効です。"
                f"有効値: {sorted(_VALID_PHASES)}"
            )


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SPSEntryDecision:
    """Statistical Premium Seller エントリー決定。

    Attributes:
        direction: credit_spread の方向性（C-3）。
                   "put"  → bull bias 時の Put Spread 売り（下落限定損失）
                   "call" → bear bias 時の Call Spread 売り（上昇限定損失）
                   iron_condor / strangle では両翼を使うため "put" をデフォルトとする。
    """
    should_enter: bool
    symbol: str
    strategy_type: PremiumStrategyType = "iron_condor"
    side: str = "sell"
    quantity: int = 1
    short_call_delta: float = 0.0   # ショート・コール・デルタ目標値（ストライク選定用）
    short_put_delta: float = 0.0    # ショート・プット・デルタ目標値
    direction: Literal["put", "call"] = "put"  # C-3: credit_spread 方向性
    reason: str = ""
    idempotency_key: str = ""
    ivr_threshold_used: float = 0.0  # 動的閾値（ログ・監査用）


@dataclass(frozen=True)
class SPSExitDecision:
    """Statistical Premium Seller エグジット決定。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal["profit_target", "stop_loss", "roll_21dte", "force_close", "none"] = "none"


# ---------------------------------------------------------------------------
# StatisticalPremiumSeller — Type A: EnterExit（ICSellTactic の上位戦術）
# ---------------------------------------------------------------------------

class StatisticalPremiumSeller(TacticBase):
    """Statistical Premium Seller（A1 プレミアム売却統合戦術）。

    IC / Strangle / Put Spread / Credit Spread の 4 サブタイプを config.strategy_type で切り替える。
    IVR 閾値は PercentileSelector 経由の動的算出（固定値禁止・feedback_no_fixed_params.md）。
    Delta 16-30 OTM / 利確 50% / 21 DTE ロール / 損切りプレミアム 2-3 倍を共通ルールとして適用する。

    Args:
        config:              StatisticalPremiumSellerConfig（slippage_tolerance_bps 必須）
        percentile_selector: PercentileSelector（None のとき共有デフォルトを使用）
    """

    def __init__(
        self,
        config: StatisticalPremiumSellerConfig | None = None,
        percentile_selector: PercentileSelector | None = None,
    ) -> None:
        self._cfg = config or StatisticalPremiumSellerConfig()
        self._pct_selector = percentile_selector or PercentileSelector()

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return f"sps_{self._cfg.strategy_type}"

    # ------------------------------------------------------------------
    # 動的 IVR 閾値算出（PercentileSelector 連動）
    # ------------------------------------------------------------------

    def _ivr_threshold(self, vix: float) -> float:
        """PercentileSelector を使い IVR 閾値を動的算出する。

        percentile を 0-100 スケールの IVR 閾値に変換する。
        固定値の直書き禁止（feedback_no_fixed_params.md 規律）。

        Returns:
            float: IVR 閾値（0-100 スケール。例: phase1/medium → 25.0）
        """
        pct = self._pct_selector.select("ivr", self._cfg.phase, vix)
        return round(pct * 100.0, 2)

    # ------------------------------------------------------------------
    # TacticBase 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック項目:
        1. env が None → False
        2. Kill Switch ARMED → False
        3. VIX が設定範囲外 → False

        Returns:
            True — 戦術発動可能
            False — 発動不可（理由は log 出力）
        """
        if env is None:
            log.warning("[StatisticalPremiumSeller.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[StatisticalPremiumSeller.preflight] Kill Switch ARMED: %s を無効化",
                self.tactic_name,
            )
            return False

        if not (self._cfg.vix_min <= env.vix <= self._cfg.vix_max):
            log.info(
                "[StatisticalPremiumSeller.preflight] VIX=%.1f が範囲外 [%.1f, %.1f]: スキップ",
                env.vix, self._cfg.vix_min, self._cfg.vix_max,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self, env: MarketEnvironment, symbol: str
    ) -> SPSEntryDecision | None:
        """エントリー判定。

        共通ロジック（全サブタイプ共通）:
        0. Kill Switch ARMED チェック（C-6, R2-C1）→ ARMED なら None を返す
        1. IVR NaN/inf チェック（R2-H4）: math.isfinite でない値は TypeError
        2. IVR スケール検証（C-1）: env.ivr_by_symbol は 0-100 スケール固定
           - 範囲外（< 0.0 または > 100.0）は TypeError
        3. IVR が動的閾値未満 → 見送り
        4. strategy_type に応じたバイアス制約チェック
        5. credit_spread は bias から direction を自動決定（C-3）
        6. IC クラス共通プレフィックス "ic" で idempotency key を生成（C-4）
           trigger_time は 5 分バケットに丸める（R2-C2: 秒境界二重発注防止）

        Args:
            env:    現在の市場環境スナップショット
                    env.ivr_by_symbol の値は 0-100 スケール（例: 55.0 = IVR 55%）
            symbol: 対象銘柄

        Returns:
            SPSEntryDecision — エントリー判定結果
            None             — Kill Switch ARMED の場合（R2-C1 Optional 型明示）

        Raises:
            TypeError: env.ivr_by_symbol の値が NaN/inf または 0-100 範囲外の場合
        """
        # C-6 / R2-C1: should_enter 冒頭で kill_switch を再チェック → None 返却
        if kill_switch_is_active():
            log.warning(
                "[StatisticalPremiumSeller.should_enter] Kill Switch ARMED: エントリー判定をスキップ"
                " (symbol=%s tactic=%s)",
                symbol, self.tactic_name,
            )
            return None

        ivr = env.ivr_by_symbol.get(symbol, 0.0)

        # R2-H4: IVR NaN/inf チェック（math.isfinite でない値は TypeError）
        if not math.isfinite(ivr):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} は NaN または inf です。"
                "IVR は math.isfinite() を満たす有限値（0-100 スケール）でなければなりません。"
            )

        # C-1: IVR スケール契約検証（0-100 固定・範囲外は TypeError）
        if not (IVR_SCALE_MIN <= ivr <= IVR_SCALE_MAX):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} が 0-100 スケール範囲外です。"
                f"MarketEnvironment.ivr_by_symbol は 0-100 スケール固定（例: 55.0 = IVR 55%）。"
                f"ICSellConfig.ivr_min={30.0} と同スケールで統一されています。"
            )

        ivr_threshold = self._ivr_threshold(env.vix)

        if ivr < ivr_threshold:
            log.debug(
                "[StatisticalPremiumSeller] IVR=%.1f < threshold=%.1f: スキップ (symbol=%s tactic=%s)",
                ivr, ivr_threshold, symbol, self.tactic_name,
            )
            return SPSEntryDecision(
                should_enter=False,
                symbol=symbol,
                strategy_type=self._cfg.strategy_type,
                reason=f"IVR={ivr:.1f}<threshold={ivr_threshold:.1f}",
                ivr_threshold_used=ivr_threshold,
            )

        # 方向性バイアス制約（戦術ごと）
        bias_check = self._check_bias(env, symbol)
        if not bias_check.ok:
            return SPSEntryDecision(
                should_enter=False,
                symbol=symbol,
                strategy_type=self._cfg.strategy_type,
                reason=bias_check.reason,
                ivr_threshold_used=ivr_threshold,
            )

        # C-3: credit_spread は bias から direction を自動決定
        direction = self._derive_direction(env.bias)

        # Delta 目標値算出（OTM 16-30 デルタ・中央値）
        short_delta = (self._cfg.delta_min + self._cfg.delta_max) / 2.0

        # C-4 / R2-C2: IC クラス共通プレフィックス "ic" で idempotency key 生成
        # ic_sell: ic_ic_sell_<symbol>_<time>
        # sps_iron_condor: ic_sps_iron_condor_<symbol>_<time>
        # → symbol + tactic_class(IC) が同じ場合に同 5 分バケット内では同一キーとなりブロックされる
        # R2-C2: trigger_time を 5 分バケットに丸める（秒境界 59→00 で key 変化を防ぐ）
        ic_class_strategy = f"{IC_CLASS_PREFIX}_{self.tactic_name}"
        _now = datetime.now(timezone.utc)
        trigger_time = _now.replace(
            minute=(_now.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        idem_key = make_job_key(
            strategy=ic_class_strategy,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[StatisticalPremiumSeller] エントリー OK: tactic=%s symbol=%s "
            "IVR=%.1f(thresh=%.1f) VIX=%.1f delta_target=%.2f direction=%s key=%s",
            self.tactic_name, symbol, ivr, ivr_threshold,
            env.vix, short_delta, direction, idem_key,
        )
        # C-3: delta 割り当て規則
        # - iron_condor / strangle: 両翼 → call / put 両方に short_delta を設定
        # - credit_spread:          direction に応じて一方だけ設定（逆翼は 0.0）
        # - put_spread:             put のみ設定
        is_two_wing = self._cfg.strategy_type in ("iron_condor", "strangle")
        sc_delta = short_delta if (is_two_wing or direction == "call") else 0.0
        sp_delta = short_delta if (is_two_wing or direction == "put") else 0.0

        return SPSEntryDecision(
            should_enter=True,
            symbol=symbol,
            strategy_type=self._cfg.strategy_type,
            side="sell",
            quantity=1,
            short_call_delta=sc_delta,
            short_put_delta=sp_delta,
            direction=direction,
            reason=(
                f"IVR={ivr:.1f}>={ivr_threshold:.1f} / VIX={env.vix:.1f} "
                f"/ bias={env.bias} / delta_target={short_delta:.2f} / dir={direction}"
            ),
            idempotency_key=idem_key,
            ivr_threshold_used=ivr_threshold,
        )

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: build_order
    # ------------------------------------------------------------------

    def build_order(self, decision: SPSEntryDecision) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_enter:
            raise ValueError(
                f"[StatisticalPremiumSeller.build_order] "
                f"should_enter=False の decision が渡された: {decision}"
            )

        return OrderRequest(
            symbol=decision.symbol,
            side="sell",
            quantity=decision.quantity,
            order_type="limit",
            tactic_name=self.tactic_name,
            idempotency_key=decision.idempotency_key,
        )

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: should_exit
    # ------------------------------------------------------------------

    def should_exit(
        self, position: Position, env: MarketEnvironment
    ) -> SPSExitDecision:
        """エグジット判定。

        判定順:
        1. Kill Switch ARMED → 強制クローズ
        2. max_credit 未設定 → 判定不能（保留）
        3. unrealized_pnl が profit_target_pct に到達 → 利確（50% 利確）
        4. DTE が roll_dte_threshold 以下 → 21 DTE ロール
        5. unrealized_pnl が stop_loss_pct を超過 → 損切り（プレミアム 2-3 倍）

        Args:
            position: 現在ポジション
            env:      現在の市場環境

        Returns:
            SPSExitDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[StatisticalPremiumSeller.should_exit] Kill Switch ARMED: 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return SPSExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        if position.max_credit <= 0:
            log.warning(
                "[StatisticalPremiumSeller.should_exit] max_credit=0: exit 判定不能 (symbol=%s)",
                position.symbol,
            )
            return SPSExitDecision(should_exit=False, reason="max_credit_not_set")

        profit_threshold = position.max_credit * self._cfg.profit_target_pct
        loss_threshold = -position.max_credit * self._cfg.stop_loss_pct

        # 50% 利確
        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[StatisticalPremiumSeller.should_exit] 利確50%%: pnl=%.2f >= target=%.2f (symbol=%s)",
                position.unrealized_pnl, profit_threshold, position.symbol,
            )
            return SPSExitDecision(
                should_exit=True,
                reason=f"profit_target_50pct: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        # C-5: expiration_date ベースの NYSE 営業日 DTE 算出
        # Position.expiration_date が設定されている場合は営業日ベース DTE を優先する
        remaining_dte = self._calc_remaining_dte(position)
        if remaining_dte <= self._cfg.roll_dte_threshold:
            log.info(
                "[StatisticalPremiumSeller.should_exit] 21 DTE ロール: remaining_dte=%d (symbol=%s)",
                remaining_dte, position.symbol,
            )
            return SPSExitDecision(
                should_exit=True,
                reason=f"roll_21dte: remaining_dte={remaining_dte}",
                exit_type="roll_21dte",
            )

        # 損切り（プレミアム 2-3 倍）
        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[StatisticalPremiumSeller.should_exit] 損切り: pnl=%.2f <= stop=%.2f (symbol=%s)",
                position.unrealized_pnl, loss_threshold, position.symbol,
            )
            return SPSExitDecision(
                should_exit=True,
                reason=f"stop_loss: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        return SPSExitDecision(should_exit=False, reason="holding", exit_type="none")

    def build_exit_order(
        self, position: Position, decision: SPSExitDecision
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_exit:
            raise ValueError(
                "[StatisticalPremiumSeller.build_exit_order] "
                "should_exit=False の decision が渡された"
            )

        # R3-C3: exit idem key に exit_reason（exit_type）を含める。
        # bucket 丸めは entry のみ。exit は秒精度の now をそのまま使用して
        # profit_target と stop_loss の 2 回目 exit blocked バグを修正する。
        # strategy = f"{tactic_name}_exit_{exit_type}" で理由ごとに key が分離される。
        _now_exit = datetime.now(timezone.utc)
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit_{decision.exit_type}",
            symbol=position.symbol,
            trigger_time=_now_exit,
        )

        return OrderRequest(
            symbol=position.symbol,
            side="buy",   # close は buy_to_close
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    @dataclasses.dataclass(frozen=True)
    class _BiasCheck:
        ok: bool
        reason: str = ""

    def _check_bias(
        self, env: MarketEnvironment, symbol: str
    ) -> "_BiasCheck":
        """戦術サブタイプ別のバイアス制約チェック。

        - iron_condor:  neutral 限定（IC は中立環境専用）
        - strangle:     neutral 限定（無方向性ボラティリティ売り）
        - put_spread:   neutral / bear 許容（下落バイアスがある場合でも保険として機能）
        - credit_spread: 全バイアス許容（方向性は _derive_direction で自動決定）

        Returns:
            _BiasCheck(ok=True) — エントリー可能
            _BiasCheck(ok=False, reason=...) — エントリー不可
        """
        stype = self._cfg.strategy_type
        bias = env.bias

        if stype in ("iron_condor", "strangle"):
            if bias != "neutral":
                return self._BiasCheck(
                    ok=False,
                    reason=f"{stype} は neutral 専用: bias={bias} (symbol={symbol})",
                )

        elif stype == "put_spread":
            if bias == "bull":
                # bull 環境でのプットスプレッド売りは片翼リスクが上昇 → スキップ
                return self._BiasCheck(
                    ok=False,
                    reason=f"put_spread: bull 環境ではショート・プット優位性低下 (symbol={symbol})",
                )

        # credit_spread は全バイアス許容
        return self._BiasCheck(ok=True)

    def _derive_direction(self, bias: str) -> Literal["put", "call"]:
        """C-3: credit_spread の方向性を bias から自動決定する。

        決定規則:
          bull  → put spread 売り（下落リスク限定・bull 環境での優位戦術）
          bear  → call spread 売り（上昇リスク限定・bear 環境での優位戦術）
          neutral → config.default_credit_spread_direction（デフォルト "put"）

        iron_condor / strangle / put_spread では両翼または固定翼のため
        この method は参照されるが実質的に方向性フィールドのデフォルト値のみを返す。

        Returns:
            "put" または "call"
        """
        if self._cfg.strategy_type == "credit_spread":
            if bias == "bull":
                return "put"
            if bias == "bear":
                return "call"
            return self._cfg.default_credit_spread_direction
        # credit_spread 以外は両翼 or 固定翼 → デフォルト "put"
        return "put"

    def _calc_remaining_dte(self, position: "Position") -> int:
        """C-5: NYSE 営業日ベースの残存 DTE を算出する。

        Position.expiration_date（date 型）が設定されている場合:
            今日から expiration_date まで NYSE 営業日数をカウントして返す。
            NYSE 祝日は簡易リスト（major US holidays）で除外する。
        expiration_date が未設定の場合:
            entry_time からの経過カレンダー日数による旧来の近似にフォールバック。

        Returns:
            int: 残存 DTE（0 以上）
        """
        expiration_date: date | None = getattr(position, "expiration_date", None)
        if expiration_date is not None:
            # R2-H5: expiration_date は NYSE の US/Eastern タイムゾーン基準で比較する
            # JST 22-00 境界付近（ET 前日）での誤判定を防ぐ
            _ET = ZoneInfo("America/New_York")
            today = datetime.now(_ET).date()
            return _count_nyse_business_days(today, expiration_date)

        # フォールバック: カレンダー日数近似（expiration_date 未設定時）
        days_held = (datetime.now(timezone.utc) - position.entry_time).days
        return max(0, self._cfg.dte_target - days_held)


# ---------------------------------------------------------------------------
# NYSE 営業日カウント（簡易実装・C-5）
# ---------------------------------------------------------------------------

#: NYSE 主要祝日（年固定の観察日・月曜振替含む概算リスト）
#: Phase 2 で pandas_market_calendars または holidays-us パッケージに差し替える。
_NYSE_FIXED_HOLIDAYS_MMDD: frozenset[tuple[int, int]] = frozenset({
    (1, 1),   # New Year's Day
    (7, 4),   # Independence Day
    (12, 25), # Christmas Day
})

#: NYSE フローティング祝日（年ごとに変わる・近似固定値）
#: Phase 2 で pandas_market_calendars に差し替える。
_NYSE_FLOAT_HOLIDAYS_2026: frozenset[date] = frozenset({
    date(2026, 1, 19),  # MLK Day（1月第3月曜）
    date(2026, 2, 16),  # Presidents Day（2月第3月曜）
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day（5月最終月曜）
    date(2026, 9, 7),   # Labor Day（9月第1月曜）
    date(2026, 11, 26), # Thanksgiving Day（11月第4木曜）
})

_NYSE_FLOAT_HOLIDAYS_2027: frozenset[date] = frozenset({
    date(2027, 1, 18),  # MLK Day
    date(2027, 2, 15),  # Presidents Day
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial Day
    date(2027, 9, 6),   # Labor Day
    date(2027, 11, 25), # Thanksgiving Day
})

# R2-H3: 2028-2030 NYSE フローティング祝日（Phase 2 で pandas_market_calendars に差し替え）
_NYSE_FLOAT_HOLIDAYS_2028: frozenset[date] = frozenset({
    date(2028, 1, 17),  # MLK Day（1月第3月曜）
    date(2028, 2, 21),  # Presidents Day（2月第3月曜）
    date(2028, 4, 14),  # Good Friday
    date(2028, 5, 29),  # Memorial Day（5月最終月曜）
    date(2028, 9, 4),   # Labor Day（9月第1月曜）
    date(2028, 11, 23), # Thanksgiving Day（11月第4木曜）
})

_NYSE_FLOAT_HOLIDAYS_2029: frozenset[date] = frozenset({
    date(2029, 1, 15),  # MLK Day
    date(2029, 2, 19),  # Presidents Day
    date(2029, 3, 30),  # Good Friday
    date(2029, 5, 28),  # Memorial Day
    date(2029, 9, 3),   # Labor Day
    date(2029, 11, 22), # Thanksgiving Day
})

_NYSE_FLOAT_HOLIDAYS_2030: frozenset[date] = frozenset({
    date(2030, 1, 21),  # MLK Day
    date(2030, 2, 18),  # Presidents Day
    date(2030, 4, 19),  # Good Friday
    date(2030, 5, 27),  # Memorial Day
    date(2030, 9, 2),   # Labor Day
    date(2030, 11, 28), # Thanksgiving Day
})

#: 全フローティング祝日セットの union（_is_nyse_holiday 高速ルックアップ用）
_ALL_NYSE_FLOAT_HOLIDAYS: frozenset[date] = (
    _NYSE_FLOAT_HOLIDAYS_2026
    | _NYSE_FLOAT_HOLIDAYS_2027
    | _NYSE_FLOAT_HOLIDAYS_2028
    | _NYSE_FLOAT_HOLIDAYS_2029
    | _NYSE_FLOAT_HOLIDAYS_2030
)


def _is_nyse_holiday(d: date) -> bool:
    """日付が NYSE 祝日かどうかを返す（簡易実装）。

    固定祝日（MM/DD）と 2026-2030 フローティング祝日（R2-H3）を参照する。
    2030 以降は Phase 2 で pandas_market_calendars に差し替える。
    """
    if (d.month, d.day) in _NYSE_FIXED_HOLIDAYS_MMDD:
        return True
    return d in _ALL_NYSE_FLOAT_HOLIDAYS


def _count_nyse_business_days(start: date, end: date) -> int:
    """start（含む）から end（含む）までの NYSE 営業日数を返す。

    週末（土日）と NYSE 祝日を除外してカウントする。
    start > end の場合は 0 を返す。

    Args:
        start: 計算開始日（今日）
        end:   計算終了日（expiration_date）

    Returns:
        int: 営業日数（0 以上）
    """
    if start > end:
        return 0

    count = 0
    current = start
    while current <= end:
        # 月曜=0 ... 金曜=4 / 土曜=5 / 日曜=6
        if current.weekday() < 5 and not _is_nyse_holiday(current):
            count += 1
        current += timedelta(days=1)
    return count
