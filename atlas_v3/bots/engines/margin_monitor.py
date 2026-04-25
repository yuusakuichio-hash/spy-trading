"""atlas_v3/bots/engines/margin_monitor.py — 証拠金使用率監視 (L05)

設計思想
--------
証拠金使用率 (margin_utilization = used_margin / total_equity) が
過大になると追加証拠金 (Margin Call) リスクが発生。
優秀トレーダーは証拠金使用率を管理して口座爆発を防ぐ。

ソース一次情報
--------------
- futu get_acc_list() の margin_ratio フィールド (moomoo_help.md 確認済)
- research_atlas_trader_gap_v2.md G-NEW1 項
- 一般的 prop firm 基準: used_margin / buying_power <= 50% 推奨

実装 (atlas_v3 namespace・spy_bot.py 書換禁止)
----------------------------------------------
- check(used_margin, total_equity) → MarginCheckResult
- WARNING_THRESHOLD: 0.50 (50% 使用で警告)
- BLOCK_THRESHOLD: 0.80 (80% 使用で新規エントリーブロック)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 証拠金使用率閾値
MARGIN_WARNING_THRESHOLD: float = 0.50
MARGIN_BLOCK_THRESHOLD: float = 0.80


@dataclass(frozen=True)
class MarginCheckResult:
    """証拠金使用率チェック結果 DTO。

    fields:
        allowed/is_warning/utilization/used_margin/total_equity/reason: 既存
        status: "ok" / "warning" / "blocked" (test/外部 API 互換 alias)
        margin_ratio: utilization の alias
    """
    allowed: bool
    is_warning: bool
    utilization: float
    used_margin: float
    total_equity: float
    reason: str
    status: str = "ok"
    margin_ratio: float = 0.0


class MarginMonitor:
    """証拠金使用率を監視して新規エントリーの可否を返す。

    例::
        monitor = MarginMonitor()
        result = monitor.check(used_margin=40_000.0, total_equity=100_000.0)
        # utilization=0.40: allowed=True, is_warning=False
    """

    def __init__(
        self,
        warning_threshold: float = MARGIN_WARNING_THRESHOLD,
        block_threshold: float = MARGIN_BLOCK_THRESHOLD,
    ) -> None:
        assert 0 < warning_threshold < block_threshold <= 1.0, (
            f"invalid thresholds: warning={warning_threshold} block={block_threshold}"
        )
        self._warn = warning_threshold
        self._block = block_threshold

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def check(
        self,
        used_margin: float,
        total_equity: float,
    ) -> MarginCheckResult:
        """証拠金使用率を計算して可否を返す。

        Parameters
        ----------
        used_margin  : 現在使用中の証拠金 (USD)
        total_equity : 口座総資産 (USD)
        """
        assert not math.isnan(used_margin), "used_margin is NaN"
        assert not math.isnan(total_equity), "total_equity is NaN"
        assert used_margin >= 0, f"used_margin={used_margin} < 0"
        assert total_equity > 0, f"total_equity={total_equity} must be > 0"

        utilization = used_margin / total_equity

        if utilization >= self._block:
            return MarginCheckResult(
                allowed=False,
                is_warning=True,
                utilization=utilization,
                used_margin=used_margin,
                total_equity=total_equity,
                reason=(
                    f"margin_utilization={utilization:.1%} >= block_threshold={self._block:.1%}: "
                    "new entries blocked"
                ),
                status="blocked",
                margin_ratio=utilization,
            )

        if utilization >= self._warn:
            return MarginCheckResult(
                allowed=True,
                is_warning=True,
                utilization=utilization,
                used_margin=used_margin,
                total_equity=total_equity,
                reason=(
                    f"margin_utilization={utilization:.1%} >= warning_threshold={self._warn:.1%}: "
                    "WARNING - consider reducing size"
                ),
                status="warning",
                margin_ratio=utilization,
            )

        return MarginCheckResult(
            allowed=True,
            is_warning=False,
            utilization=utilization,
            used_margin=used_margin,
            total_equity=total_equity,
            reason=f"margin_utilization={utilization:.1%}: OK",
            status="ok",
            margin_ratio=utilization,
        )
