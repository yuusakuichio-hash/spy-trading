"""common_v3/risk/engine.py — 共通 Risk Engine (Sprint 1-B Phase B)

仕様: data/specs/v3/common_spec_v3_20260422.md  (ADR-013 v2 / Sprint 1-B)

責務:
- Atlas v3 + Chronos v3 の全戦術が Plug-in 経由でリスクチェックを通す中核エンジン
- 発注前に check_all() を呼ぶことで全リスク制約を一括適用する
- 各チェック関数は独立して呼び出し可能（テスト容易性）

設計規律:
- 純 sync / 副作用なし（ファイル I/O・外部 API 呼出を持たない）
- kill_switch との協調: check_all() は kill_switch.is_active() を先頭で確認する
- KillSwitch の発動は呼出側の責務（RiskEngine は判定のみ行い発動しない）
- CC ≤ 10 per method

公開 API:
    RiskConfig          — 設定 dataclass（frozen=True）
    PortfolioSnapshot   — ポートフォリオ状態 dataclass（frozen=True）
    OptionRequest       — オプション発注リクエスト dataclass（frozen=True）
    RiskDecision        — 判定結果 dataclass（frozen=True）
    RiskEngine          — リスク判定エンジン本体
    PositionSizingMethod — ポジションサイジング手法 Enum
"""
from __future__ import annotations

import dataclasses
import logging
import math
import queue
import threading
import time
from enum import Enum
from typing import Literal, Sequence

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: VaR/CVaR 計算時のデフォルト信頼水準
_DEFAULT_VAR_CONFIDENCE: float = 0.99

#: ファットテール補正乗数（t 分布 df=3 の 99% 分位は正規の約 1.65 倍）
_FAT_TAIL_MULTIPLIER: float = 1.65

#: CVaR = VaR * (Expected Shortfall ratio for t(df=3) tail)
#: 簡易近似: CVaR ≈ VaR * 1.33  (t(3), 99% 信頼水準)
_CVAR_RATIO: float = 1.33

#: 最大 quantity 上限（sanity guard）
_MAX_QUANTITY_SANITY: int = 10_000

#: C-1/C-2: VaR 計算に必要な最低履歴件数（99% 分位 = 真の 1% に n>=100 必要）
_MIN_VAR_HISTORY: int = 100

# ---------------------------------------------------------------------------
# CR-1: _escalate_kill_switch_failure — Queue(maxsize=100) + single-active-thread
#        + 10s debounce
# ---------------------------------------------------------------------------
#
# 設計方針:
#   - queue.Queue(maxsize=100) で pending メッセージ数を上限 100 に制限（DoS 防止）
#   - threading.BoundedSemaphore(1) で同時実行スレッドを 1 本に制限（worker スレッド相当）
#   - 10s debounce で短時間大量呼出時の Pushover スパムを防止
#   - Thread は call-site から起動（with patch(...) コンテキスト内でのテスト互換性を維持）
#
# テスト互換性:
#   with patch("common.pushover_client.send", ...):
#       RiskEngine._escalate_kill_switch_failure(...)  # Thread は patch context 内で開始
#   Thread が send を import するのは patch context 内なので mock が有効

#: pending メッセージ数カウンタ（DoS 上限 100）
_ESCALATION_QUEUE: queue.Queue[str] = queue.Queue(maxsize=100)

#: 現在実行中の escalation Thread（None = 実行中なし）
_ESCALATION_ACTIVE_THREAD: threading.Thread | None = None
_ESCALATION_THREAD_LOCK = threading.Lock()

#: デバウンス: 最後の send 開始時刻（monotonic）
_ESCALATION_LAST_SENT: float = 0.0
_ESCALATION_LAST_SENT_LOCK = threading.Lock()

#: デバウンス間隔（秒）
_ESCALATION_DEBOUNCE_SECS: float = 10.0


def _reset_escalation_state_for_test() -> None:
    """テスト専用: debounce タイマーをリセットする。

    本番コードから呼ばない。conftest の autouse fixture から各テスト前に呼ぶことで
    テスト間の debounce 状態持ち越しを防ぐ。
    Thread 参照もリセットして前のテストで起動した Thread が semaphore を保持したまま
    次のテストに持ち越される問題を防ぐ。
    """
    global _ESCALATION_LAST_SENT, _ESCALATION_ACTIVE_THREAD  # noqa: PLW0603
    with _ESCALATION_LAST_SENT_LOCK:
        _ESCALATION_LAST_SENT = 0.0
    with _ESCALATION_THREAD_LOCK:
        _ESCALATION_ACTIVE_THREAD = None


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------

class PositionSizingMethod(str, Enum):
    """ポジションサイジング手法。

    KELLY:      フルケリー比率（推奨: フラクショナルケリーを使う場合は別途 kelly_fraction 設定）
    FIXED:      固定数量（config.fixed_size_contracts を使用）
    VIX_LINKED: VIX に反比例するリスク予算（高ボラ時に size を縮小）
    """
    KELLY = "kelly"
    FIXED = "fixed"
    VIX_LINKED = "vix_linked"


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class RiskConfig:
    """RiskEngine 設定。

    Args:
        max_notional_usd:           1 発注あたりの最大想定元本（USD）
        max_daily_loss_usd:         1 日の最大損失（USD・負値）
        max_drawdown_pct:           最大許容ドローダウン率（0.0–1.0）
        max_var_usd:                VaR 上限（USD・ポジション単位）
        max_assignment_risk_usd:    ショートオプション行使リスク上限（USD）
        max_premium_notional:       ロングオプション最大プレミアム支払い上限（USD）
                                    None = チェックなし（後方互換）
        fixed_size_contracts:       FIXED 方式での固定枚数
        kelly_fraction:             KELLY 方式での Kelly 比率縮小係数（0.0–1.0）
        vix_size_base:              VIX_LINKED の基準 VIX（この VIX で fixed_size_contracts が適用）
        sizing_method:              デフォルトのサイジング手法
        returns_unit:               returns_history の単位（"usd" or "ratio"）。
                                    check_var() 冒頭で呼出側の unit と照合する（C-4）。
        kill_switch_bypass_approver: kill_switch 例外時の手動 bypass 承認者 ID（C-β）。
                                    None = bypass 不可。設定時は audit log に記録。
        kill_switch_bypass_approver_allowlist: bypass 承認を許可する approver ID の
                                    frozenset（C-β）。None = allowlist チェックなし
                                    （kill_switch_bypass_approver との後方互換）。
                                    空 frozenset() 設定時は全 approver を拒否する。
        max_var_ratio:              VaR 上限（ratio 単位・returns_unit="ratio" 時に使用）。
                                    None = チェックなし。returns_unit="ratio" かつ
                                    max_var_ratio=None の場合は max_var_usd で判定（後方互換）。
                                    C-δ: unit と max_var_* の不整合で ValueError raise。
    """
    max_notional_usd: float = 50_000.0
    max_daily_loss_usd: float = -2_000.0
    max_drawdown_pct: float = 0.10
    max_var_usd: float = 5_000.0
    max_assignment_risk_usd: float = 20_000.0
    max_premium_notional: float | None = None
    fixed_size_contracts: int = 1
    kelly_fraction: float = 0.25
    vix_size_base: float = 20.0
    sizing_method: PositionSizingMethod = PositionSizingMethod.FIXED
    returns_unit: Literal["usd", "ratio"] = "usd"
    kill_switch_bypass_approver: str | None = None
    kill_switch_bypass_approver_allowlist: frozenset[str] | None = None
    max_var_ratio: float | None = None

    def __post_init__(self) -> None:
        if self.max_daily_loss_usd > 0:
            raise ValueError(
                f"max_daily_loss_usd must be <= 0, got {self.max_daily_loss_usd}"
            )
        if not (0.0 < self.max_drawdown_pct <= 1.0):
            raise ValueError(
                f"max_drawdown_pct must be in (0.0, 1.0], got {self.max_drawdown_pct}"
            )
        if not (0.0 < self.kelly_fraction <= 1.0):
            raise ValueError(
                f"kelly_fraction must be in (0.0, 1.0], got {self.kelly_fraction}"
            )
        if self.vix_size_base <= 0:
            raise ValueError(
                f"vix_size_base must be > 0, got {self.vix_size_base}"
            )
        if self.fixed_size_contracts <= 0:
            raise ValueError(
                f"fixed_size_contracts must be > 0, got {self.fixed_size_contracts}"
            )
        if self.returns_unit not in ("usd", "ratio"):
            raise ValueError(
                f"returns_unit must be 'usd' or 'ratio', got {self.returns_unit!r}"
            )
        if self.max_premium_notional is not None and self.max_premium_notional <= 0:
            raise ValueError(
                f"max_premium_notional must be > 0 if set, got {self.max_premium_notional}"
            )
        # C-δ: unit と max_var_* の不整合チェック
        # ratio 単位で max_var_usd=default(5000) かつ max_var_ratio=None → 警告なし（後方互換）
        # ratio 単位で max_var_ratio が明示設定されかつ max_var_usd も明示設定 → ValueError
        if self.returns_unit == "ratio" and self.max_var_ratio is not None:
            if self.max_var_ratio <= 0:
                raise ValueError(
                    f"max_var_ratio must be > 0 if set, got {self.max_var_ratio}"
                )
        if self.max_var_ratio is not None and self.returns_unit == "usd":
            raise ValueError(
                "max_var_ratio is set but returns_unit='usd': unit/limit mismatch. "
                "Use max_var_usd for usd unit, or set returns_unit='ratio'."
            )


@dataclasses.dataclass(frozen=True)
class PortfolioSnapshot:
    """ポートフォリオ現在状態スナップショット。

    Args:
        pnl_day_usd:        当日損益（USD）。損失は負値。
        drawdown_pct:       現在のドローダウン率（0.0–1.0）
        returns_history:    直近リターン系列（VaR 計算に使用）
        current_vix:        現在の VIX 水準
        win_rate:           過去の勝率（KELLY サイジングで使用、0.0–1.0）
        avg_win_usd:        平均利益（USD・KELLY サイジングで使用）
        avg_loss_usd:       平均損失（USD・負値・KELLY サイジングで使用）
    """
    pnl_day_usd: float = 0.0
    drawdown_pct: float = 0.0
    returns_history: tuple[float, ...] = dataclasses.field(default_factory=tuple)
    current_vix: float = 20.0
    win_rate: float = 0.5
    avg_win_usd: float = 100.0
    avg_loss_usd: float = -100.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.drawdown_pct <= 1.0):
            raise ValueError(
                f"drawdown_pct must be in [0.0, 1.0], got {self.drawdown_pct}"
            )
        if self.current_vix <= 0:
            raise ValueError(f"current_vix must be > 0, got {self.current_vix}")
        if not (0.0 <= self.win_rate <= 1.0):
            raise ValueError(
                f"win_rate must be in [0.0, 1.0], got {self.win_rate}"
            )


@dataclasses.dataclass(frozen=True)
class OptionRequest:
    """オプション発注リクエスト（行使リスク検証専用）。

    Args:
        symbol:             原資産銘柄（例: "SPY"）
        strike:             行使価格（USD）
        underlying_price:   原資産現在値（USD）
        contracts:          枚数（正整数）
        is_short:           売り建てなら True（割り当てリスクあり）
        multiplier:         オプション乗数（株式オプションは通常 100）
    """
    symbol: str
    strike: float
    underlying_price: float
    contracts: int
    is_short: bool = True
    multiplier: int = 100

    def __post_init__(self) -> None:
        if self.strike <= 0:
            raise ValueError(f"strike must be > 0, got {self.strike}")
        if self.underlying_price <= 0:
            raise ValueError(
                f"underlying_price must be > 0, got {self.underlying_price}"
            )
        if self.contracts <= 0:
            raise ValueError(
                f"contracts must be > 0, got {self.contracts}"
            )
        if self.multiplier <= 0:
            raise ValueError(
                f"multiplier must be > 0, got {self.multiplier}"
            )


@dataclasses.dataclass(frozen=True)
class RiskDecision:
    """リスク判定結果。

    Args:
        allowed:    True = 発注許可 / False = 発注拒否
        reason:     拒否理由（allowed=True の場合は空文字）
        sizing:     推奨ポジションサイズ（contracts / shares）
        var_usd:    算出した VaR（USD）
        cvar_usd:   算出した CVaR（USD）
    """
    allowed: bool
    reason: str
    sizing: int
    var_usd: float = 0.0
    cvar_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.sizing < 0:
            raise ValueError(f"sizing must be >= 0, got {self.sizing}")


# ---------------------------------------------------------------------------
# RiskEngine
# ---------------------------------------------------------------------------

class RiskEngine:
    """共通 Risk Engine (ADR-013 v2 / Sprint 1-B Phase B).

    Atlas v3 + Chronos v3 の全戦術が Plug-in 経由でリスクチェックを通す。
    発注前に check_all() を呼ぶことで全制約を一括適用する。

    Args:
        config:  RiskConfig インスタンス（None の場合はデフォルト設定）

    設計:
    - 純 sync / 副作用なし（ファイル I/O を持たない）
    - kill_switch との協調は check_all() 内で行う（import は lazy でユニットテスト阻害なし）
    - 各 check_* メソッドは独立して呼び出し可能（テスト容易性・再利用性）
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self._config: RiskConfig = config or RiskConfig()

    @property
    def config(self) -> RiskConfig:
        """設定の読み取り専用アクセス。"""
        return self._config

    # ------------------------------------------------------------------
    # 全チェック統合エントリポイント
    # ------------------------------------------------------------------

    def check_all(
        self,
        request_notional: float,
        portfolio: PortfolioSnapshot,
        option_request: OptionRequest | None = None,
        sizing_method: PositionSizingMethod | None = None,
    ) -> RiskDecision:
        """全リスクチェックを一括実行し RiskDecision を返す。

        処理順（順序は変更禁止・短絡評価あり）:
        1. Kill Switch 確認（ARMED なら即拒否）
        2. max_notional チェック
        3. max_daily_loss チェック
        4. max_drawdown チェック
        5. assignment_risk チェック（option_request が指定された場合）
        6. VaR/CVaR 計算
        7. VaR 上限チェック
        8. ポジションサイジング計算

        Args:
            request_notional:  発注想定元本（USD）
            portfolio:         現在のポートフォリオ状態
            option_request:    オプション発注時のみ指定（None なら行使リスクチェックスキップ）
            sizing_method:     サイジング手法（None なら config.sizing_method を使用）

        Returns:
            RiskDecision（allowed=False の場合は reason に拒否理由が入る）
        """
        method = sizing_method or self._config.sizing_method

        # C-α: 全入力 nan/inf sanitize（VaR bypass を物理ガード）
        nan_reason = self._check_nan_inf_inputs(request_notional, portfolio)
        if nan_reason is not None:
            log.warning("[RiskEngine] DENIED (C-α): %s", nan_reason)
            return RiskDecision(allowed=False, reason=nan_reason, sizing=0)

        # 1. Kill Switch チェック（lazy import でユニットテストの dry-run を阻害しない）
        ks_decision = self._check_kill_switch()
        if ks_decision is not None:
            return ks_decision

        # 2. max_notional
        if not self.check_max_notional(request_notional):
            reason = (
                f"max_notional exceeded: request={request_notional:.2f} "
                f"> limit={self._config.max_notional_usd:.2f}"
            )
            log.warning("[RiskEngine] DENIED: %s", reason)
            return RiskDecision(allowed=False, reason=reason, sizing=0)

        # 3. max_daily_loss
        if not self.check_max_daily_loss(portfolio.pnl_day_usd):
            reason = (
                f"max_daily_loss exceeded: pnl_day={portfolio.pnl_day_usd:.2f} "
                f"< limit={self._config.max_daily_loss_usd:.2f}"
            )
            log.warning("[RiskEngine] DENIED: %s", reason)
            return RiskDecision(allowed=False, reason=reason, sizing=0)

        # 4. max_drawdown
        if not self.check_max_drawdown(portfolio.drawdown_pct):
            reason = (
                f"max_drawdown exceeded: drawdown={portfolio.drawdown_pct:.4f} "
                f"> limit={self._config.max_drawdown_pct:.4f}"
            )
            log.warning("[RiskEngine] DENIED: %s", reason)
            return RiskDecision(allowed=False, reason=reason, sizing=0)

        # 5. assignment_risk（オプションのみ）
        if option_request is not None:
            if not self.check_assignment_risk(option_request):
                assignment_notional = (
                    option_request.strike
                    * option_request.contracts
                    * option_request.multiplier
                )
                reason = (
                    f"assignment_risk exceeded: "
                    f"notional={assignment_notional:.2f} "
                    f"> limit={self._config.max_assignment_risk_usd:.2f}"
                )
                log.warning("[RiskEngine] DENIED: %s", reason)
                return RiskDecision(allowed=False, reason=reason, sizing=0)

        # 6. VaR/CVaR 計算（C-1: データ不足時 / C-α: nan/inf 検出時は DENY）
        try:
            var_usd = self.check_var(portfolio)
        except ValueError as exc:
            exc_str = str(exc)
            # C-α: nan/inf エラーは reason にそのまま伝播させる
            if "nan/inf" in exc_str:
                reason = exc_str
            else:
                reason = "insufficient history for VaR"
            log.warning("[RiskEngine] DENIED: %s", exc_str)
            return RiskDecision(allowed=False, reason=reason, sizing=0)
        cvar_usd = var_usd * _CVAR_RATIO

        # 7. VaR 上限チェック（C-δ: unit=ratio 時は max_var_ratio で判定）
        var_deny_reason = self._check_var_limit(var_usd)
        if var_deny_reason is not None:
            log.warning("[RiskEngine] DENIED: %s", var_deny_reason)
            return RiskDecision(
                allowed=False, reason=var_deny_reason, sizing=0,
                var_usd=var_usd, cvar_usd=cvar_usd,
            )

        # 8. ポジションサイジング（H-2: Kelly avg_loss=0 は ValueError → DENY）
        try:
            sizing = self.check_position_sizing(portfolio, method=method)
        except ValueError as exc:
            reason = f"position_sizing error: {exc}"
            log.warning("[RiskEngine] DENIED: %s", reason)
            return RiskDecision(
                allowed=False, reason=reason, sizing=0,
                var_usd=var_usd, cvar_usd=cvar_usd,
            )

        log.info(
            "[RiskEngine] ALLOWED: notional=%.2f var=%.2f cvar=%.2f sizing=%d method=%s",
            request_notional, var_usd, cvar_usd, sizing, method.value,
        )
        return RiskDecision(
            allowed=True,
            reason="",
            sizing=sizing,
            var_usd=var_usd,
            cvar_usd=cvar_usd,
        )

    # ------------------------------------------------------------------
    # 個別チェックメソッド
    # ------------------------------------------------------------------

    def check_max_notional(self, request_notional: float) -> bool:
        """想定元本が config 上限以内かを確認する。

        Args:
            request_notional: 発注想定元本（USD）

        Returns:
            True  — 上限以内（発注許可）
            False — 上限超過（発注拒否）
        """
        # C-025 Sprint 2 carryover: 境界条件 assert（runtime invariant 検証）
        assert not math.isnan(request_notional), "check_max_notional: request_notional must not be NaN"
        assert not math.isinf(request_notional), "check_max_notional: request_notional must not be inf"
        return request_notional <= self._config.max_notional_usd

    def check_max_daily_loss(self, pnl_day: float) -> bool:
        """当日損益が config 下限（最大損失）を超えているかを確認する。

        H-1 業界慣行: pnl_day がちょうど max_daily_loss_usd に達した時点で発注停止。
        （>= ではなく > を使うことで「限界到達 = 即停止」を実現）

        Args:
            pnl_day: 当日損益（USD・損失は負値）

        Returns:
            True  — 損失が上限より大きい（発注許可）
            False — 損失が上限以下（発注拒否・ちょうど上限も含む）
        """
        # C-025 Sprint 2 carryover: 境界条件 assert（runtime invariant 検証）
        assert not math.isnan(pnl_day), "check_max_daily_loss: pnl_day must not be NaN"
        assert not math.isinf(pnl_day), "check_max_daily_loss: pnl_day must not be inf"
        return pnl_day > self._config.max_daily_loss_usd

    def check_max_drawdown(self, drawdown_pct: float) -> bool:
        """現在のドローダウンが config 上限以内かを確認する。

        Args:
            drawdown_pct: ドローダウン率（0.0–1.0）

        Returns:
            True  — 上限以内（発注許可）
            False — 上限超過（発注拒否）
        """
        # C-025 Sprint 2 carryover: 境界条件 assert（runtime invariant 検証）
        assert not math.isnan(drawdown_pct), "check_max_drawdown: drawdown_pct must not be NaN"
        assert 0.0 <= drawdown_pct <= 1.0, f"check_max_drawdown: drawdown_pct must be in [0, 1], got {drawdown_pct}"
        return drawdown_pct <= self._config.max_drawdown_pct

    def check_assignment_risk(self, option_request: OptionRequest) -> bool:
        """オプションの行使/プレミアムリスクが config 上限以内かを確認する。

        ショートオプション（is_short=True）:
            行使リスク = strike × contracts × multiplier を max_assignment_risk_usd と比較。

        ロングオプション（is_short=False）:
            プレミアム支払いリスク = strike × contracts × multiplier を
            config.max_premium_notional と比較（C-3: None の場合はチェックなし）。

        Args:
            option_request: オプション発注リクエスト

        Returns:
            True  — 上限以内
            False — 上限超過
        """
        notional = (
            option_request.strike
            * option_request.contracts
            * option_request.multiplier
        )

        if option_request.is_short:
            return notional <= self._config.max_assignment_risk_usd
        else:
            # C-3: Long side — max_premium_notional が設定されていればチェック
            if self._config.max_premium_notional is not None:
                return notional <= self._config.max_premium_notional
            return True

    def check_var(
        self,
        portfolio: PortfolioSnapshot,
        expected_unit: Literal["usd", "ratio"] | None = None,
    ) -> float:
        """ポートフォリオの VaR（ES/CVaR ベース・ファットテール対策済み）を計算する。

        C-1: returns_history < _MIN_VAR_HISTORY（100件）の場合は ValueError を raise する。
             呼出側（check_all）は DENY 判定に変換する。

        C-2: n >= 100 が必須のため真の 99% 分位（1% テール）を保証する。

        C-4: expected_unit が config.returns_unit と一致しない場合は AssertionError を raise する。
             expected_unit=None の場合は unit チェックをスキップ（後方互換）。

        ファットテール補正:
        - 正規分布仮定の過少評価を防ぐため _FAT_TAIL_MULTIPLIER（t(df=3) 相当）を乗算
        - これは保守側への補正（VaR が大きくなる方向）

        Args:
            portfolio:     ポートフォリオスナップショット
            expected_unit: 呼出側が想定する returns_history の単位（C-4 schema 契約チェック）

        Returns:
            VaR（USD or 同単位・正値）

        Raises:
            ValueError:    returns_history が _MIN_VAR_HISTORY 未満（C-1）
            AssertionError: expected_unit が config.returns_unit と不一致（C-4）
        """
        # C-4: schema 契約チェック（unit 不一致を早期検知）
        if expected_unit is not None and expected_unit != self._config.returns_unit:
            raise AssertionError(
                f"returns_unit mismatch: caller expects {expected_unit!r} "
                f"but config is {self._config.returns_unit!r}. "
                "Pass correct returns_history unit or update RiskConfig.returns_unit."
            )

        returns = portfolio.returns_history
        n = len(returns)

        # C-1/C-2: 最低 _MIN_VAR_HISTORY 件が必要（不足時は DENY）
        if n < _MIN_VAR_HISTORY:
            raise ValueError(
                f"insufficient history for VaR: need >= {_MIN_VAR_HISTORY} samples, got {n}. "
                "DENY until sufficient data is accumulated."
            )

        # C-α: 各要素の isfinite 検査（nan/inf 混入で VaR bypass を防ぐ）
        for i, r in enumerate(returns):
            if not math.isfinite(r):
                raise ValueError(
                    f"nan/inf in returns_history[{i}]={r}: "
                    "all elements must be finite. DENY until history is clean. (C-alpha)"
                )

        var_usd = self._historical_var_99(returns)

        # ファットテール補正
        return var_usd * _FAT_TAIL_MULTIPLIER

    def check_position_sizing(
        self,
        portfolio: PortfolioSnapshot,
        method: PositionSizingMethod | None = None,
    ) -> int:
        """ポジションサイズを計算する（contracts / shares 単位）。

        Args:
            portfolio: ポートフォリオスナップショット
            method:    サイジング手法（None なら config.sizing_method を使用）

        Returns:
            推奨ポジションサイズ（1 以上の整数）
        """
        effective_method = method or self._config.sizing_method

        if effective_method == PositionSizingMethod.KELLY:
            return self._kelly_sizing(portfolio)
        elif effective_method == PositionSizingMethod.VIX_LINKED:
            return self._vix_linked_sizing(portfolio)
        else:
            # FIXED（デフォルト）
            return self._config.fixed_size_contracts

    # ------------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------------

    def _check_kill_switch(self) -> RiskDecision | None:
        """Kill Switch が ARMED なら即座に拒否 RiskDecision を返す。

        C-5: kill_switch モジュールの import 失敗時は Pushover escalation を発火し、
        bypass_approver が設定されていれば audit log に記録した上で None を返す
        （手動 override 経路）。bypass_approver 未設定時はフェイルセーフ DENY。

        lazy import により、kill_switch モジュールが使用不能な環境（ユニットテスト dry-run）
        でも RiskEngine のインスタンス化・各チェックメソッドの呼び出しが可能。

        Returns:
            RiskDecision(allowed=False) — Kill Switch ARMED の場合
            None                       — Kill Switch 非 ARMED の場合
        """
        try:
            from common_v3.risk.kill_switch import is_active as _ks_is_active
            if _ks_is_active():
                reason = "kill_switch is active"
                log.warning("[RiskEngine] DENIED: %s", reason)
                return RiskDecision(allowed=False, reason=reason, sizing=0)
        except Exception as exc:
            # C-5: import/呼出失敗 → Pushover escalation + bypass 判定
            reason = f"kill_switch check failed: {exc}"
            log.error("[RiskEngine] kill_switch error (C-5 escalation): %s", reason)
            self._escalate_kill_switch_failure(reason)

            approver = self._config.kill_switch_bypass_approver
            if approver is not None:
                # C-β: approver 空文字/スペースは拒否
                stripped = approver.strip()
                if not stripped:
                    raise ValueError(
                        "kill_switch_bypass_approver must not be empty or whitespace-only."
                    )
                # C-β: allowlist チェック（設定されている場合のみ）
                allowlist = self._config.kill_switch_bypass_approver_allowlist
                if allowlist is not None and stripped not in allowlist:
                    raise ValueError(
                        f"kill_switch_bypass_approver {stripped!r} is not in allowlist. "
                        f"Permitted approvers: {sorted(allowlist)}"
                    )
                log.warning(
                    "[RiskEngine] kill_switch bypass by approver=%s: %s",
                    stripped, reason,
                )
                # CR-2: audit 書込失敗 → fail-closed（bypass 拒否）
                try:
                    self._write_bypass_audit(approver=stripped, reason=reason)
                except Exception as audit_exc:
                    audit_reason = "audit write failed"
                    log.error(
                        "[RiskEngine] DENIED (CR-2 fail-closed): %s — %s",
                        audit_reason, audit_exc,
                    )
                    return RiskDecision(
                        allowed=False, reason=audit_reason, sizing=0
                    )
                return None  # 手動 override — check_all を続行

            return RiskDecision(allowed=False, reason=reason, sizing=0)
        return None

    @staticmethod
    def _escalate_kill_switch_failure(reason: str) -> None:
        """C-5 / C-γ / CR-1: kill_switch エラー時の Pushover escalation（非同期送信）。

        CR-1: Queue(maxsize=100) で pending 数上限 + Thread 参照チェックで同時実行 1 本制限
        （無制限スレッド生成 DoS 防止）。10s debounce でスパム防止。
        Thread は call-site から起動するため with patch(...) テストコンテキストと互換。
        呼出側をブロックしない。
        """
        global _ESCALATION_LAST_SENT, _ESCALATION_ACTIVE_THREAD  # noqa: PLW0603

        # CR-1: 10s debounce チェック（ロック内でアトミックに確認・更新）
        now = time.monotonic()
        with _ESCALATION_LAST_SENT_LOCK:
            if now - _ESCALATION_LAST_SENT < _ESCALATION_DEBOUNCE_SECS:
                return  # debounce drop — silent
            _ESCALATION_LAST_SENT = now

        # CR-1: キュー上限チェック（maxsize=100）
        try:
            _ESCALATION_QUEUE.put_nowait(reason)
        except queue.Full:
            log.error(
                "[RiskEngine] escalation queue full (CR-1 DoS guard): dropped reason=%s",
                reason,
            )
            return

        # CR-1: 単一スレッド制限（既存 Thread が生存中ならドロップ）
        with _ESCALATION_THREAD_LOCK:
            if _ESCALATION_ACTIVE_THREAD is not None and _ESCALATION_ACTIVE_THREAD.is_alive():
                # Thread 実行中 — キューから除去してドロップ
                try:
                    _ESCALATION_QUEUE.get_nowait()
                except queue.Empty:
                    pass
                return
            # Thread が終了済み or None — 新しい Thread を起動する

        # キューからメッセージを取り出して Thread に渡す
        try:
            msg = _ESCALATION_QUEUE.get_nowait()
        except queue.Empty:
            return

        # send を calling thread（= with patch(...) コンテキスト内）でキャプチャする。
        # これにより test の with patch("common.pushover_client.send", ...) が有効な間に
        # mock が取得され、Thread が patch context 外で実行されても mock が使われる。
        # C-020 Sprint 2 carryover: silent except 明示化（raise なし・意図的 fallback）
        # 意図: common.pushover_client が unavailable でも dead-man escalation 経路は機能させる。
        # mock patch context 外で使えなくなる可能性があるため import 失敗は None fallback で継続。
        try:
            import common.pushover_client as _pushover_mod
            _pushover_send = _pushover_mod.send
        except Exception as _import_err:  # noqa: BLE001
            # 意図的 silent except: log は出さず（startup 時の一時的な import 失敗は通常発生しない）、
            # _send() 内で _pushover_send is None なら ImportError raise する設計。
            log.debug("[RiskEngine] pushover_client import failed, using None fallback: %s", _import_err)
            _pushover_send = None

        def _send() -> None:
            global _ESCALATION_ACTIVE_THREAD  # noqa: PLW0603
            try:
                if _pushover_send is None:
                    raise ImportError("common.pushover_client not available")
                _pushover_send(
                    title="[RiskEngine] CRITICAL: kill_switch failure",
                    message=f"kill_switch check failed — all orders DENIED.\n{msg}",
                    priority=1,
                )
            except Exception as pushover_exc:
                log.error(
                    "[RiskEngine] Pushover escalation failed (non-critical): %s",
                    pushover_exc,
                )
            finally:
                with _ESCALATION_THREAD_LOCK:
                    _ESCALATION_ACTIVE_THREAD = None

        t = threading.Thread(target=_send, daemon=True)
        with _ESCALATION_THREAD_LOCK:
            _ESCALATION_ACTIVE_THREAD = t
        t.start()

    @staticmethod
    def _write_bypass_audit(approver: str, reason: str) -> None:
        """C-β / CR-2: kill_switch bypass 使用時 kill_switch_audit.jsonl に追記する。

        CR-2: audit 書込失敗は fail-closed（例外を呼出側に伝播させる）。
        bypass を認めたのに audit が残らない状態（audit_fail_open）を禁止する。
        呼出側 (_check_kill_switch) は本例外を受けて
        RiskDecision(allowed=False, reason="audit write failed") を返す。
        """
        from common_v3.risk.kill_switch import _write_audit as _ks_write_audit
        _ks_write_audit(
            event="risk_engine_bypass",
            reason=reason,
            activator=approver,
            extra={"source": "RiskEngine._check_kill_switch"},
        )

    def _check_nan_inf_inputs(
        self,
        request_notional: float,
        portfolio: PortfolioSnapshot,
    ) -> str | None:
        """C-α: check_all 冒頭の nan/inf 入力検査。

        request_notional および主要スカラー値に nan/inf が含まれていたら
        拒否理由文字列を返す。問題なければ None を返す。

        returns_history の各要素は check_var 内で個別検査するため、
        ここでは含めない（check_var の ValueError → DENY 経路で処理）。
        """
        scalars = {
            "request_notional": request_notional,
            "pnl_day_usd": portfolio.pnl_day_usd,
            "drawdown_pct": portfolio.drawdown_pct,
            "current_vix": portfolio.current_vix,
        }
        for name, val in scalars.items():
            if not math.isfinite(val):
                return f"nan/inf in input: {name}={val}"
        return None

    def _check_var_limit(self, var_value: float) -> str | None:
        """C-δ: VaR 上限チェック（unit=ratio 時は max_var_ratio で判定）。

        Returns:
            拒否理由文字列（超過時）/ None（通過時）
        """
        if self._config.returns_unit == "ratio" and self._config.max_var_ratio is not None:
            limit = self._config.max_var_ratio
            if var_value > limit:
                return (
                    f"VaR exceeded (ratio): var={var_value:.6f} > limit={limit:.6f}"
                )
            return None
        # usd 単位（または ratio 単位で max_var_ratio=None の後方互換）
        limit = self._config.max_var_usd
        if var_value > limit:
            return (
                f"VaR exceeded: var={var_value:.2f} > limit={limit:.2f}"
            )
        return None

    def check_kill_switch_health(self) -> dict:
        """C-5: kill_switch モジュールの健全性プローブ。

        呼出側はこのメソッドを定期的に呼び出し、kill_switch が利用可能かを確認できる。

        Returns:
            dict with keys:
                healthy (bool): True = import + is_active() 呼出が成功
                error (str):    エラーメッセージ（healthy=True の場合は空文字）
                is_active (bool | None): kill_switch の現在状態（healthy=False は None）
        """
        try:
            from common_v3.risk.kill_switch import is_active as _ks_is_active
            active = _ks_is_active()
            return {"healthy": True, "error": "", "is_active": active}
        except Exception as exc:
            return {"healthy": False, "error": str(exc), "is_active": None}

    def _historical_var_99(self, returns: Sequence[float]) -> float:
        """ヒストリカル VaR（99% 信頼水準・損失方向）を計算する。

        99% VaR = 1% 最悪分位のリターン（負値）の絶対値。
        returns が全て 0 の場合は 0 を返す（ゼロ除算回避）。

        Args:
            returns: リターン系列（USD 単位を推奨・比率でも可）

        Returns:
            VaR（USD or 同単位・正値）
        """
        sorted_returns = sorted(returns)
        n = len(sorted_returns)
        # C-2: 真の 99% VaR — 1% テール (ceil(n*0.01) 件) の最悪値
        # ceil-1 で「n の 1% に該当する最悪インデックス」を保守側に取る
        # n=100: ceil(1.0)-1=0 → sorted[0] (最悪値)
        # n=1000: ceil(10)-1=9 → sorted[9] (10番目の最悪値)
        idx = max(0, math.ceil(n * 0.01) - 1)
        worst = sorted_returns[idx]
        # 損失（負値）を正値に変換
        return max(0.0, -worst)

    def _kelly_sizing(self, portfolio: PortfolioSnapshot) -> int:
        """フラクショナル Kelly 基準でポジションサイズを計算する。

        Kelly 基準:
          f = (p * b - q) / b
          p = 勝率, q = 1 - p
          b = avg_win / |avg_loss| （ペイオフ比）

        フラクショナル Kelly = f * kelly_fraction（config）

        結果は 1 以上にクランプ（Kelly が負または 0 の場合は最小 1 契約）。

        Args:
            portfolio: ポートフォリオスナップショット

        Returns:
            推奨契約数（1 以上）
        """
        p = portfolio.win_rate
        q = 1.0 - p
        avg_win = portfolio.avg_win_usd
        avg_loss_abs = abs(portfolio.avg_loss_usd)

        # H-2: avg_loss_abs=0 は Kelly 計算不能（勝率過大評価温床）→ DENY
        if avg_loss_abs == 0:
            raise ValueError(
                "avg_loss_usd=0 is invalid for Kelly sizing: "
                "cannot compute Kelly fraction (division by zero). "
                "DENY to prevent win_rate overestimation."
            )

        if avg_win <= 0:
            return self._config.fixed_size_contracts

        b = avg_win / avg_loss_abs
        kelly_full = (p * b - q) / b

        if kelly_full <= 0:
            # Kelly 負 → 期待値マイナス → 最小 1 枚に縮退
            return 1

        fractional = kelly_full * self._config.kelly_fraction
        sizing = max(1, round(fractional * self._config.fixed_size_contracts))
        return min(sizing, _MAX_QUANTITY_SANITY)

    def _vix_linked_sizing(self, portfolio: PortfolioSnapshot) -> int:
        """VIX に反比例するリスク予算でポジションサイズを計算する。

        vix_size_base（デフォルト 20）での size = fixed_size_contracts。
        VIX が vix_size_base の 2 倍（高 vol）→ size は半分。
        VIX が vix_size_base の 半分（低 vol）→ size は 2 倍（上限付き）。

        formula: size = fixed_size_contracts * (vix_size_base / current_vix)

        結果は 1 以上・_MAX_QUANTITY_SANITY 以下にクランプ。

        Args:
            portfolio: ポートフォリオスナップショット（current_vix を使用）

        Returns:
            推奨契約数（1 以上）
        """
        current_vix = portfolio.current_vix
        if current_vix <= 0:
            return self._config.fixed_size_contracts

        ratio = self._config.vix_size_base / current_vix
        sizing = max(1, round(self._config.fixed_size_contracts * ratio))
        return min(sizing, _MAX_QUANTITY_SANITY)
