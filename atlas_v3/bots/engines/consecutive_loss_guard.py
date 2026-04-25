"""atlas_v3/bots/engines/consecutive_loss_guard.py — 連敗ストップ・サイジング調整 (L03)

設計思想
--------
Marty Schwartz ("Pit Bull" 1998 ch.10): 4 連敗でトレード停止。
MFFU best practice: 3 連敗でポジションサイズ半減。
Millennium LP Letter 2023: 5% pod shutdown。

本実装は「3 連敗でサイズ係数 0.5 / 継続連敗でさらに低減」のルールを採用。
これは Schwartz の「4 連敗ストップ」を取込みながらも完全停止より
Bot 継続稼働を優先する atlas_v3 方針に沿った設計。

ソース一次情報
--------------
- Schwartz "Pit Bull" (1998) ch.10: "4 losers/day stop"
- MFFU best practice (myfundedfutures.com official) position halving
- top100_traders_gap_analysis_20260422.md D01/D07/D10 項

実装 (atlas_v3 namespace・spy_bot.py 書換禁止)
----------------------------------------------
- 3連敗: size_factor = 0.5
- 4連敗: size_factor = 0.25
- 5連敗以上: size_factor = 0.0 (complete stop)
- 勝ちが入ったら consecutive_losses リセット
- paper_mode: チェック有効 / size_factor 反映有効 (paper でも検証)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# 連敗数→サイズ係数マッピング (Schwartz + MFFU ルール合成)
_LOSS_STREAK_TO_FACTOR: dict[int, float] = {
    0: 1.0,
    1: 1.0,
    2: 1.0,
    3: 0.5,   # 3連敗: 半減 (MFFU best practice)
    4: 0.25,  # 4連敗: 1/4 (Schwartz 停止基準前の最後の警告)
}
_STOP_THRESHOLD: int = 5  # 5連敗以上: 完全停止


@dataclass(frozen=True)
class ConsecutiveLossResult:
    """連敗ガード判定結果 DTO。"""
    allowed: bool
    size_factor: float
    consecutive_losses: int
    reason: str

    @property
    def is_halted(self) -> bool:
        """allowed=False の semantic alias (test API)."""
        return not self.allowed


class ConsecutiveLossGuard:
    """連敗数に応じてサイズ係数を動的に返すガード。

    例::
        guard = ConsecutiveLossGuard()
        # 3連敗後
        guard.record_loss()
        guard.record_loss()
        guard.record_loss()
        result = guard.check()
        # result.size_factor = 0.5, result.allowed = True

        # 5連敗後
        guard.record_loss()
        guard.record_loss()
        result = guard.check()
        # result.size_factor = 0.0, result.allowed = False (完全停止)
    """

    def __init__(self) -> None:
        self._consecutive_losses: int = 0

    # ------------------------------------------------------------------
    # 状態更新
    # ------------------------------------------------------------------

    def record_loss(self) -> None:
        """損失トレードを1件記録する。"""
        self._consecutive_losses += 1
        log.info("consecutive_loss_guard: loss recorded, streak=%d", self._consecutive_losses)

    def record_win(self) -> None:
        """勝ちトレードを記録してストリークをリセットする。"""
        if self._consecutive_losses > 0:
            log.info("consecutive_loss_guard: win resets streak from %d", self._consecutive_losses)
        self._consecutive_losses = 0

    def record_breakeven(self) -> None:
        """損益ゼロのトレード (ストリークはリセットしない)。"""
        pass  # 連敗カウントに影響なし

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def check(self) -> ConsecutiveLossResult:
        """現在の連敗数に基づいてサイズ係数と発注可否を返す。

        Returns
        -------
        ConsecutiveLossResult
        """
        n = self._consecutive_losses
        assert n >= 0, f"consecutive_losses={n} < 0: invalid state"

        if n >= _STOP_THRESHOLD:
            return ConsecutiveLossResult(
                allowed=False,
                size_factor=0.0,
                consecutive_losses=n,
                reason=f"consecutive_losses={n} >= stop_threshold={_STOP_THRESHOLD}: HALT",
            )

        factor = _LOSS_STREAK_TO_FACTOR.get(n, 0.0)
        allowed = factor > 0.0

        if n == 0:
            reason = "no losing streak: full size"
        else:
            reason = f"consecutive_losses={n}: size_factor={factor:.2f}"

        return ConsecutiveLossResult(
            allowed=allowed,
            size_factor=factor,
            consecutive_losses=n,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 状態アクセサ
    # ------------------------------------------------------------------

    @property
    def consecutive_losses(self) -> int:
        """現在の連敗数 (read-only)。"""
        return self._consecutive_losses

    def reset(self) -> None:
        """連敗カウントを強制リセット (日次セッション切替等)。"""
        log.info("consecutive_loss_guard: manual reset from %d", self._consecutive_losses)
        self._consecutive_losses = 0
