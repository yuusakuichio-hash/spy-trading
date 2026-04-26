"""common/strategy_selector.py — 環境適応型戦術選択 + PDT 0DTE→1DTEフォールバック + HMMレジームフィルタ

役割:
  - PDT残数 / capital / 市場環境に応じて最適戦術を返す
  - PDT残0 + capital < $25K + 0DTE戦術 → 同ロジックの1DTE版へ自動切替
  - 1DTE未実装戦術 → "no_trade" 返却
  - HMMレジームフィルタ: FRAGILE_DIV → skip, FRAGILE_TREND → size×0.5

使い方:
    from common.strategy_selector import StrategySelector, SelectionResult
    selector = StrategySelector(pdt_tracker=tracker)
    result = selector.select(
        candidate="CS",
        expiry_date=today,
        capital_usd=8000.0,
    )
    # result.strategy: "1dte_cs" or "CS" or "no_trade"
    # result.fallback_activated: True if 0DTE→1DTE切替が発生
    # result.regime_state: "STABLE" / "FRAGILE_TREND" / "FRAGILE_DIV" / "UNKNOWN"
    # result.regime_size_multiplier: 1.0 / 0.5 / 0.0
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

from common.pdt_1dte_utils import (
    is_0dte_strategy,
    strategy_supports_1dte,
    strategy_satisfies_no_pdt,
    get_1dte_fallback_name,
    increment_fallback_count,
    notify_fallback_activated,
)

log = logging.getLogger(__name__)

# HMMレジーム分類器（オプショナル: インポート失敗でも動作継続）
try:
    from common.hmm_regime import get_global_classifier, RegimeState as _RegimeState
    _HMM_AVAILABLE = True
    log.info("[StrategySelector] HMMレジーム分類器ロード成功")
except Exception as _hmm_import_err:
    _HMM_AVAILABLE = False
    log.warning(f"[StrategySelector] HMMレジーム分類器ロード失敗 → レジームフィルタ無効: {_hmm_import_err}")

try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    import pytz  # type: ignore
    ET = pytz.timezone("America/New_York")  # type: ignore


@dataclass
class SelectionResult:
    """戦術選択結果。"""
    strategy: str               # 最終選択戦術名（"no_trade" = 取引なし）
    is_0dte: bool               # 0DTE day tradeか
    fallback_activated: bool    # 0DTE→1DTEフォールバックが発動したか
    pdt_remaining: int | float  # 選択時点のPDT残本数
    reason: str                 # 選択理由（デバッグ用）
    original_candidate: str     # フォールバック前の元候補戦術名
    satisfies_no_pdt: bool = False  # PDT残0でもこの戦術が選択可能か
    # HMMレジーム情報（Atlas施策4）
    regime_state: str = "UNKNOWN"           # STABLE / FRAGILE_TREND / FRAGILE_DIV / UNKNOWN
    regime_size_multiplier: float = 1.0    # 1.0 / 0.5 / 0.0


class StrategySelector:
    """環境適応型戦術選択エンジン。

    Args:
        pdt_tracker: PDTTrackerインスタンス（Noneの場合はPDTチェック無効）
    """

    def __init__(self, pdt_tracker=None) -> None:
        self._pdt = pdt_tracker

    def select(
        self,
        candidate: str,
        expiry_date: Optional[datetime.date],
        capital_usd: float,
        now_et: Optional[datetime.datetime] = None,
    ) -> SelectionResult:
        """戦術候補を受け取り、PDT状況を考慮した最終選択を返す。

        Args:
            candidate:   元の戦術候補名（例: "CS", "ORB", "0dte_cs"）
            expiry_date: オプション満期日
            capital_usd: 現在の口座残高（USD）
            now_et:      現在のET時刻（Noneなら自動取得）

        Returns:
            SelectionResult
        """
        if now_et is None:
            now_et = datetime.datetime.now(ET)

        # ── [HMMレジームフィルタ] Atlas施策4 ───────────────────────────────────
        # FRAGILE_DIV → no_trade (エントリースキップ)
        # FRAGILE_TREND → size乗数0.5 (SelectionResultに記録、サイズ調整は呼び出し側が行う)
        # UNKNOWN → 保守的に0.5乗数、エントリーは継続
        _regime_state = "UNKNOWN"
        _regime_multiplier = 1.0
        if _HMM_AVAILABLE:
            try:
                _clf = get_global_classifier()
                _regime_enum = _clf.predict_current()
                _regime_state = _regime_enum.value
                _regime_multiplier = _clf.get_size_multiplier(_regime_enum)
                log.info(
                    f"[StrategySelector][HMM] regime={_regime_state}, "
                    f"size_multiplier={_regime_multiplier:.1f}"
                )
                if _clf.should_skip_entry(_regime_enum):
                    is_0dte = is_0dte_strategy(candidate, expiry_date, now_et)
                    log.info(
                        f"[StrategySelector][HMM] FRAGILE_DIV → no_trade: {candidate}"
                    )
                    return SelectionResult(
                        strategy="no_trade",
                        is_0dte=is_0dte,
                        fallback_activated=False,
                        pdt_remaining=float("inf"),
                        reason=f"HMMレジーム={_regime_state} → エントリースキップ",
                        original_candidate=candidate,
                        satisfies_no_pdt=False,
                        regime_state=_regime_state,
                        regime_size_multiplier=0.0,
                    )
            except Exception as _hmm_err:
                log.warning(f"[StrategySelector][HMM] レジーム取得失敗 → 無視: {_hmm_err}")
                _regime_state = "UNKNOWN"
                _regime_multiplier = 0.5  # 取得失敗時は保守的に

        # $25K以上はPDT対象外 → そのまま通過
        if capital_usd >= 25_000.0:
            is_0dte = is_0dte_strategy(candidate, expiry_date, now_et)
            return SelectionResult(
                strategy=candidate,
                is_0dte=is_0dte,
                fallback_activated=False,
                pdt_remaining=float("inf"),
                reason=f"capital=${capital_usd:.0f} >= $25K → PDT対象外",
                original_candidate=candidate,
                satisfies_no_pdt=True,
                regime_state=_regime_state,
                regime_size_multiplier=_regime_multiplier,
            )

        # PDTトラッカーなし → チェック無効
        if self._pdt is None:
            is_0dte = is_0dte_strategy(candidate, expiry_date, now_et)
            return SelectionResult(
                strategy=candidate,
                is_0dte=is_0dte,
                fallback_activated=False,
                pdt_remaining=float("inf"),
                reason="PDTTracker未設定 → チェック無効",
                original_candidate=candidate,
                satisfies_no_pdt=strategy_satisfies_no_pdt(candidate),
                regime_state=_regime_state,
                regime_size_multiplier=_regime_multiplier,
            )

        is_0dte = is_0dte_strategy(candidate, expiry_date, now_et)
        remaining = self._pdt.remaining_allowed(capital_usd)
        no_pdt_ok = strategy_satisfies_no_pdt(candidate)

        # 1DTE以上 → PDT不問でそのまま通過
        if not is_0dte:
            return SelectionResult(
                strategy=candidate,
                is_0dte=False,
                fallback_activated=False,
                pdt_remaining=remaining,
                reason=f"1DTE以上 → PDT対象外 (capital=${capital_usd:.0f})",
                original_candidate=candidate,
                satisfies_no_pdt=True,
                regime_state=_regime_state,
                regime_size_multiplier=_regime_multiplier,
            )

        # 0DTE + PDT残あり → そのまま通過
        if remaining > 0:
            return SelectionResult(
                strategy=candidate,
                is_0dte=True,
                fallback_activated=False,
                pdt_remaining=remaining,
                reason=f"0DTE PDT残{remaining}本あり → 通過",
                original_candidate=candidate,
                satisfies_no_pdt=no_pdt_ok,
                regime_state=_regime_state,
                regime_size_multiplier=_regime_multiplier,
            )

        # 0DTE + PDT残0 + satisfies_no_pdt=True → 満期放置前提で通過
        # （CS売り・IC売り等: 満期OTM消滅 → expired_worthless で PDT 不計上）
        if no_pdt_ok:
            log.info(
                f"[StrategySelector] PDT残0でも通過: {candidate} "
                f"(satisfies_no_pdt=True, capital=${capital_usd:.0f})"
            )
            return SelectionResult(
                strategy=candidate,
                is_0dte=True,
                fallback_activated=False,
                pdt_remaining=0,
                reason=(
                    f"{candidate}: satisfies_no_pdt=True → 満期放置前提で通過 "
                    f"(PDT残0, capital=${capital_usd:.0f})"
                ),
                original_candidate=candidate,
                satisfies_no_pdt=True,
                regime_state=_regime_state,
                regime_size_multiplier=_regime_multiplier,
            )

        # 0DTE + PDT残0 → フォールバック判定
        log.info(
            f"[StrategySelector] PDT残0: {candidate} → 1DTEフォールバック判定 "
            f"(capital=${capital_usd:.0f})"
        )

        if not strategy_supports_1dte(candidate):
            log.info(
                f"[StrategySelector] {candidate}: 1DTE未対応 → no_trade"
            )
            return SelectionResult(
                strategy="no_trade",
                is_0dte=True,
                fallback_activated=False,
                pdt_remaining=0,
                reason=f"{candidate}は1DTE未対応戦術 → no_trade",
                original_candidate=candidate,
                satisfies_no_pdt=False,
                regime_state=_regime_state,
                regime_size_multiplier=_regime_multiplier,
            )

        fallback_name = get_1dte_fallback_name(candidate)
        if fallback_name is None:
            return SelectionResult(
                strategy="no_trade",
                is_0dte=True,
                fallback_activated=False,
                pdt_remaining=0,
                reason=f"{candidate}: 1DTE版名称生成失敗 → no_trade",
                original_candidate=candidate,
                satisfies_no_pdt=False,
                regime_state=_regime_state,
                regime_size_multiplier=_regime_multiplier,
            )

        # フォールバック発動
        count = increment_fallback_count(now_et)
        notify_fallback_activated(candidate, fallback_name)

        log.info(
            f"[StrategySelector] 0DTE→1DTE自動切替: {candidate} → {fallback_name} "
            f"(本日{count}回目)"
        )

        return SelectionResult(
            strategy=fallback_name,
            is_0dte=False,  # 1DTE = PDT対象外
            fallback_activated=True,
            pdt_remaining=0,
            reason=(
                f"0DTE→1DTEフォールバック: {candidate} → {fallback_name} "
                f"(PDT残0, capital=${capital_usd:.0f}, 本日{count}回目)"
            ),
            original_candidate=candidate,
            satisfies_no_pdt=True,  # 1DTEは常にsatisfies_no_pdt=True
            regime_state=_regime_state,
            regime_size_multiplier=_regime_multiplier,
        )

    def select_with_pdt_check(
        self,
        candidate: str,
        expiry_date: Optional[datetime.date],
        capital_usd: float,
        now_et: Optional[datetime.datetime] = None,
    ) -> SelectionResult:
        """select()のエイリアス（API統一性のため）。"""
        return self.select(candidate, expiry_date, capital_usd, now_et)


# ── グローバルシングルトン ────────────────────────────────────────────────────

_global_selector: Optional[StrategySelector] = None


def get_global_selector(pdt_tracker=None) -> StrategySelector:
    """プロセスごとのシングルトンを返す。

    Args:
        pdt_tracker: 初回呼び出し時のみ有効（既存インスタンスがある場合は無視）
    """
    global _global_selector
    if _global_selector is None:
        _global_selector = StrategySelector(pdt_tracker=pdt_tracker)
    return _global_selector


def select_strategy(
    candidate: str,
    expiry_date: Optional[datetime.date],
    capital_usd: float,
    pdt_tracker=None,
    now_et: Optional[datetime.datetime] = None,
) -> SelectionResult:
    """モジュールレベルのショートカット関数。

    spy_bot.py から `from common.strategy_selector import select_strategy` で使用。
    """
    selector = get_global_selector(pdt_tracker)
    return selector.select(candidate, expiry_date, capital_usd, now_et)
