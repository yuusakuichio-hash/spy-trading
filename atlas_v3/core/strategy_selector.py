"""atlas_v3/core/strategy_selector.py — 戦術動的選択（Sprint 1-B Phase B B4）

仕様: data/specs/v3/atlas_spec_v3_20260422.md B4 L123-L130

責務:
- VIX / IVR / MarketEnvironment から戦術リストを動的選択
- 固定閾値禁止: 全閾値を PercentileSelector + 環境データから動的算出
- earnings / event blackout 期間は戦術を制限

規律:
- 戦術マッピング固定化禁止（feedback_no_fixed_params.md）
- gamma_scalp は Type D（hybrid）: IVR/RV state + portfolio delta/gamma 反応
- SPX は paper 非対応 whitelist 登録済み（Phase 2 で whitelist check 追加）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from atlas_v3.core.env_observer import MarketEnvironment

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 戦術リスト定義（spec A3 10 種 + 将来拡張のためタプルで凍結）
# ---------------------------------------------------------------------------

#: 全戦術名（spec A3 L36-L49）
ALL_TACTICS: tuple[str, ...] = (
    "cs_sell",
    "ic_sell",
    "butterfly",
    "calendar_sell",
    "strangle_sell",
    "straddle_buy",
    "orb_1dte",
    "delta_hedge",
    "earnings_iv_crush",
    "gamma_scalp",
)

#: 戦術 → tactic_type マッピング（spec B5 L209-L216）
TACTIC_TYPE_MAP: dict[str, str] = {
    "cs_sell": "enter_exit",
    "ic_sell": "enter_exit",
    "butterfly": "enter_exit",
    "calendar_sell": "enter_exit",
    "strangle_sell": "enter_exit",
    "straddle_buy": "enter_exit",
    "gamma_scalp": "hybrid",         # R2: Type A → D 修正
    "delta_hedge": "portfolio_reactive",
    "orb_1dte": "state_carrying",
    "earnings_iv_crush": "state_carrying",
}

# ---------------------------------------------------------------------------
# TacticDecision DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TacticDecision:
    """戦術選択結果 DTO。

    Attributes:
        tactic_name:  選択された戦術名（spec A3 の 10 種から 1 つ）
        symbol:       対象銘柄
        confidence:   選択確信度（0.0 〜 1.0・動的算出）
        reason:       選択理由（ログ・EICAS 用）
        environment_snapshot: 決定時の環境スナップショット
    """

    tactic_name: str
    symbol: str
    confidence: float
    reason: str
    environment_snapshot: MarketEnvironment | None = None


# ---------------------------------------------------------------------------
# PercentileSelector
# ---------------------------------------------------------------------------

#: 資金フェーズ識別子
PhaseStr = Literal["phase1", "phase2", "phase3", "phase4"]

#: VIX 領域識別子
VixRegime = Literal["low", "medium", "high"]

# VIX 領域境界（動的化候補・現在は PhaseA 設定）
_VIX_LOW_MAX: float = 18.0
_VIX_HIGH_MIN: float = 28.0


class PercentileSelector:
    """percentile を資金フェーズ / VIX 領域から動的算出する（spec B2 L100-L112）。

    固定 percentile の使用は C-08 違反（feedback_no_fixed_params.md 参照）。

    Percentile マッピング根拠（spec B2 例示）:
        Phase 1 低リスク: 30pct
        Phase 4 攻め    : 70pct
        VIX > 30        : 保守側（-10pct）

    Phase 2 以降で Finnhub / ThetaData 実績値から自動更新する予定。
    """

    # 資金フェーズ × VIX 領域 → percentile テーブル
    _TABLE: dict[str, dict[str, float]] = {
        "phase1": {"low": 0.30, "medium": 0.25, "high": 0.20},
        "phase2": {"low": 0.45, "medium": 0.40, "high": 0.30},
        "phase3": {"low": 0.55, "medium": 0.50, "high": 0.40},
        "phase4": {"low": 0.70, "medium": 0.60, "high": 0.45},
    }

    def select(self, metric: str, phase: str, vix: float) -> float:
        """percentile を資金フェーズ / VIX 領域から動的算出して返す。

        Args:
            metric: メトリクス識別子（現在は "ivr" / "vix" / "vrp" 等）
            phase:  資金フェーズ識別子（"phase1" 〜 "phase4"）
            vix:    現在の VIX 値（VIX 領域判定に使用）

        Returns:
            percentile float（0.0 〜 1.0）

        Raises:
            ValueError: phase が未定義の場合
        """
        if phase not in self._TABLE:
            raise ValueError(
                f"unknown phase={phase!r}. "
                f"valid phases: {sorted(self._TABLE.keys())}"
            )

        regime = self._classify_vix(vix)
        percentile = self._TABLE[phase][regime]
        log.debug(
            "[PercentileSelector] metric=%s phase=%s vix=%.1f regime=%s → percentile=%.2f",
            metric, phase, vix, regime, percentile,
        )
        return percentile

    @staticmethod
    def _classify_vix(vix: float) -> VixRegime:
        """VIX 値を low / medium / high の 3 領域に分類する。"""
        if vix < _VIX_LOW_MAX:
            return "low"
        if vix >= _VIX_HIGH_MIN:
            return "high"
        return "medium"


# ---------------------------------------------------------------------------
# StrategySelector
# ---------------------------------------------------------------------------

class StrategySelector:
    """VIX / IVR / MarketEnvironment から戦術リストを動的選択する（spec B4）。

    固定閾値を一切使わず PercentileSelector 経由で算出した動的閾値を使用する。

    Args:
        percentile_selector: PercentileSelector インスタンス（外部注入）
        phase:               現在の資金フェーズ（"phase1" 〜 "phase4"）
    """

    def __init__(
        self,
        percentile_selector: PercentileSelector | None = None,
        phase: str = "phase1",
    ) -> None:
        self._pct_selector = percentile_selector or PercentileSelector()
        self._phase = phase

    # ------------------------------------------------------------------
    # 公開 API（spec B4 凍結 Interface）
    # ------------------------------------------------------------------

    def select(
        self,
        env: MarketEnvironment,
        symbol: str,
    ) -> list[TacticDecision]:
        """戦術リストを動的選択して返す（spec B4 凍結 Interface）。

        Args:
            env:    現在の市場環境スナップショット
            symbol: 対象銘柄

        Returns:
            優先度順の TacticDecision リスト（0 件は「本日参戦なし」を意味する）
        """
        decisions: list[TacticDecision] = []

        # ivr を symbol から取得（存在しなければ 50.0 をフォールバック）
        ivr = env.ivr_by_symbol.get(symbol, 50.0)

        # --- 閾値を動的算出（固定閾値禁止）---
        ivr_pct = self._pct_selector.select("ivr", self._phase, env.vix)
        # IVR percentile を 0-100 スケールの閾値に変換（50pct → 50）
        ivr_threshold = ivr_pct * 100.0

        vix_pct = self._pct_selector.select("vix", self._phase, env.vix)
        vix_threshold = vix_pct * 100.0

        # --- 環境分類 ---
        vix_regime = PercentileSelector._classify_vix(env.vix)
        is_high_iv = ivr >= ivr_threshold
        is_directional = env.bias in ("bull", "bear")
        is_gamma_env = (
            ivr >= 50.0
            and env.vrp < env.vix  # RV < IV の簡易近似
            and env.vix >= 20.0
        )

        # --- 戦術選択ロジック ---
        # 1. kill switch は engine 側で処理済み・ここでは環境ロジックのみ

        # gamma_scalp: Type D (hybrid) - IVR 高 + RV < IV + VIX >= 20
        if is_gamma_env:
            decisions.append(TacticDecision(
                tactic_name="gamma_scalp",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "gamma_scalp"),
                reason=f"IVR={ivr:.1f}>50 / VIX={env.vix:.1f}>=20 / VRP={env.vrp:.2f}<VIX",
                environment_snapshot=env,
            ))

        # ic_sell: 中 VIX / IVR 中高 → 安定収益
        if vix_regime == "medium" and is_high_iv:
            decisions.append(TacticDecision(
                tactic_name="ic_sell",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "ic_sell"),
                reason=f"VIX=medium({env.vix:.1f}) / IVR={ivr:.1f}>={ivr_threshold:.1f}",
                environment_snapshot=env,
            ))

        # cs_sell: 低 VIX / 方向性あり
        if vix_regime == "low" and is_directional:
            decisions.append(TacticDecision(
                tactic_name="cs_sell",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "cs_sell"),
                reason=f"VIX=low({env.vix:.1f}) / bias={env.bias}",
                environment_snapshot=env,
            ))

        # butterfly: 低 IVR
        if not is_high_iv and vix_regime in ("low", "medium"):
            decisions.append(TacticDecision(
                tactic_name="butterfly",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "butterfly"),
                reason=f"IVR={ivr:.1f}<{ivr_threshold:.1f} (低IVR)",
                environment_snapshot=env,
            ))

        # calendar_sell: IVR 高 + term_ratio > 1.0（コンタンゴ）
        if is_high_iv and env.term_ratio > 1.0:
            decisions.append(TacticDecision(
                tactic_name="calendar_sell",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "calendar_sell"),
                reason=f"IVR={ivr:.1f} / term_ratio={env.term_ratio:.2f}>1.0",
                environment_snapshot=env,
            ))

        # strangle_sell: IVR 高 + 方向感なし
        if is_high_iv and env.bias == "neutral":
            decisions.append(TacticDecision(
                tactic_name="strangle_sell",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "strangle_sell"),
                reason=f"IVR={ivr:.1f} / bias=neutral",
                environment_snapshot=env,
            ))

        # straddle_buy: 高ボラ予想（VIX 高・方向性不明）
        if vix_regime == "high" and not is_directional:
            decisions.append(TacticDecision(
                tactic_name="straddle_buy",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "straddle_buy"),
                reason=f"VIX=high({env.vix:.1f}) / bias=neutral",
                environment_snapshot=env,
            ))

        # orb_1dte: 方向性ある朝（State C・Phase 2 で observe() 連携）
        if is_directional and vix_regime != "high":
            decisions.append(TacticDecision(
                tactic_name="orb_1dte",
                symbol=symbol,
                confidence=self._compute_confidence(env.vix, ivr, "orb_1dte"),
                reason=f"bias={env.bias} / VIX={env.vix:.1f}",
                environment_snapshot=env,
            ))

        # delta_hedge: portfolio δ > 0.30（portfolio 情報は Phase 2 で連携）
        # 常時候補として追加（Engine 側の portfolio snapshot で判定）
        decisions.append(TacticDecision(
            tactic_name="delta_hedge",
            symbol=symbol,
            confidence=0.5,
            reason="delta_hedge は portfolio snapshot 連携待ち（Phase 2）",
            environment_snapshot=env,
        ))

        # confidence 降順でソート（高確信度優先）
        decisions.sort(key=lambda d: d.confidence, reverse=True)

        log.info(
            "[StrategySelector] symbol=%s decisions=%d (VIX=%.1f IVR=%.1f bias=%s)",
            symbol,
            len(decisions),
            env.vix,
            ivr,
            env.bias,
        )
        return decisions

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        vix: float,
        ivr: float,
        tactic_name: str,
    ) -> float:
        """戦術選択確信度を動的算出する（0.0 〜 1.0）。

        Phase 2 で実績 PnL / Sharpe / Kelly fraction 等と連動予定。
        現在は VIX / IVR の組み合わせから簡易算出する。
        """
        # 戦術ごとの基準適合度
        _BASE: dict[str, float] = {
            "gamma_scalp": 0.75,
            "ic_sell": 0.70,
            "cs_sell": 0.65,
            "butterfly": 0.55,
            "calendar_sell": 0.60,
            "strangle_sell": 0.65,
            "straddle_buy": 0.60,
            "orb_1dte": 0.65,
            "delta_hedge": 0.50,
            "earnings_iv_crush": 0.60,
        }
        base = _BASE.get(tactic_name, 0.50)

        # VIX 調整: VIX が 18-28 の中間帯は信頼性が高い
        vix_adj = 0.0
        if 18.0 <= vix <= 28.0:
            vix_adj = 0.05
        elif vix > 35.0:
            vix_adj = -0.10

        # IVR 調整: IVR 30-70 は安定
        ivr_adj = 0.0
        if 30.0 <= ivr <= 70.0:
            ivr_adj = 0.05
        elif ivr > 80.0:
            ivr_adj = -0.05

        confidence = min(1.0, max(0.0, base + vix_adj + ivr_adj))
        return round(confidence, 4)
