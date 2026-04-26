"""atlas_v3/bots/engines/pdt_guard.py — PDT 保護ガード (read-only wrapper)

目的
----
各 Engine が発注前に通すチェックレイヤー。
common/pdt_tracker.py を read-only で参照し、PDT 上限到達を予測して
発注を物理ブロックする。common/pdt_tracker.py 本体には一切書き込まない。

設計方針
--------
- paper_mode=True: PDT 対象外（常に True を返す）
- live_mode + capital >= 25,000 USD: PDT 対象外（常に True を返す）
- live_mode + capital < 25,000 USD: rolling 5 営業日 3 回未満なら True、到達で False
- earnings straddle 等「翌日クローズ予定」も同日 open+close は PDT 計上対象
  → is_same_day_round_trip=True が想定される場合は count 予測して判定
- schg lock: common/pdt_tracker.py は read-only 参照のみ・書込禁止

呼出パターン（各 Engine のエントリー発注前）
-------------------------------------------
    from atlas_v3.bots.engines.pdt_guard import PDTGuard

    guard = PDTGuard(paper_mode=False, capital_usd=8000.0)
    result = guard.check_can_trade(symbol="US.SPY", trade_date=date.today())
    if not result.allowed:
        log.warning("PDT blocked: %s", result.reason)
        return  # 発注しない

    # → 発注処理へ
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

from common.pdt_tracker import PDTTracker, PDT_THRESHOLD_USD, get_global_tracker

log = logging.getLogger(__name__)


class PDTBlockedError(RuntimeError):
    """PDT 上限到達により発注がブロックされたことを示す例外。

    各 Engine の place_order() / build_order() 冒頭の PDTGuard チェックで
    allowed=False のとき raise する。呼び出し側は本例外を catch して
    EICAS 通知 / スキップ処理を行うこと。
    """

# ETタイムゾーン（pdt_tracker と同じ解決順）
try:
    import zoneinfo
    _ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    import pytz  # type: ignore
    _ET = pytz.timezone("America/New_York")  # type: ignore


# ---------------------------------------------------------------------------
# 結果 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PDTCheckResult:
    """PDT チェック結果。

    Attributes:
        allowed:         True=発注可 / False=PDT ブロック
        reason:          判定根拠テキスト（ログ・EICAS 用）
        rolling5_count:  直近 5 営業日の day_trade 件数（参照値）
        pdt_remaining:   残り許容 day_trade 数（$25K以上=float('inf')）
        paper_mode:      チェック時の paper_mode 値
        capital_usd:     チェック時の口座資金額
    """
    allowed: bool
    reason: str
    rolling5_count: int = 0
    pdt_remaining: int | float = 0
    paper_mode: bool = False
    capital_usd: float = 0.0


# ---------------------------------------------------------------------------
# PDTGuard
# ---------------------------------------------------------------------------

class PDTGuard:
    """各 Engine が発注前に通す PDT 保護ガード。

    common/pdt_tracker.py の PDTTracker を read-only で参照する。
    このクラス自体は PDT ログへの書き込みを一切行わない。
    書き込みは実際の round-trip 完了後に Engine 側または AtlasEngine が
    tracker.record_round_trip() を呼ぶこと。

    Args:
        paper_mode:  True = paper 発注モード（PDT チェックをスキップ）
        capital_usd: 口座資金額（USD）。$25,000 以上は PDT 対象外。
        tracker:     PDTTracker インスタンス（None でグローバルシングルトンを使用）
    """

    def __init__(
        self,
        paper_mode: bool = True,
        capital_usd: float = 0.0,
        tracker: Optional[PDTTracker] = None,
    ) -> None:
        self._paper_mode = paper_mode
        self._capital_usd = capital_usd
        self._tracker: PDTTracker = tracker or get_global_tracker()

    # ------------------------------------------------------------------
    # パブリック API
    # ------------------------------------------------------------------

    def check_can_trade(
        self,
        symbol: str,
        trade_date: Optional[datetime.date] = None,
    ) -> PDTCheckResult:
        """発注前 PDT チェックを実行する（read-only）。

        判定ロジック:
        1. paper_mode=True → 常に allowed=True（PDT 非対象）
        2. capital_usd >= PDT_THRESHOLD_USD → allowed=True（資金 $25K 以上・PDT 非対象）
        3. rolling 5 営業日の day_trade < 3 → allowed=True
        4. rolling 5 営業日の day_trade >= 3 → allowed=False（PDT 上限到達）

        Args:
            symbol:     銘柄コード（ログ用）
            trade_date: 取引予定日（None なら今日の ET 日付）

        Returns:
            PDTCheckResult
        """
        ref = trade_date or datetime.datetime.now(_ET).date()

        # 1. paper mode: PDT 非対象
        if self._paper_mode:
            return PDTCheckResult(
                allowed=True,
                reason="paper_mode=True: PDT チェックスキップ",
                rolling5_count=0,
                pdt_remaining=float("inf"),
                paper_mode=True,
                capital_usd=self._capital_usd,
            )

        # 2. 資金 $25K 以上: PDT 非対象
        if self._capital_usd >= PDT_THRESHOLD_USD:
            count = self._tracker.count_day_trades_rolling(reference=ref)
            return PDTCheckResult(
                allowed=True,
                reason=(
                    f"capital_usd={self._capital_usd:.0f} >= {PDT_THRESHOLD_USD:.0f}: "
                    "PDT 非対象（無制限）"
                ),
                rolling5_count=count,
                pdt_remaining=float("inf"),
                paper_mode=False,
                capital_usd=self._capital_usd,
            )

        # 3/4. $25K 未満: rolling count で判定
        count = self._tracker.count_day_trades_rolling(reference=ref)
        remaining = self._tracker.remaining_allowed(
            capital_usd=self._capital_usd, reference=ref
        )

        if remaining > 0:
            log.debug(
                "[PDTGuard] allowed: symbol=%s rolling5=%d remaining=%d "
                "capital=%.0f",
                symbol, count, int(remaining), self._capital_usd,
            )
            return PDTCheckResult(
                allowed=True,
                reason=(
                    f"PDT 許容範囲内: rolling5={count}/3 remaining={int(remaining)}"
                ),
                rolling5_count=count,
                pdt_remaining=remaining,
                paper_mode=False,
                capital_usd=self._capital_usd,
            )

        log.warning(
            "[PDTGuard] BLOCKED: symbol=%s rolling5=%d/3 capital=%.0f",
            symbol, count, self._capital_usd,
        )
        return PDTCheckResult(
            allowed=False,
            reason=(
                f"PDT 上限到達: rolling5={count}/3 "
                f"capital={self._capital_usd:.0f} < {PDT_THRESHOLD_USD:.0f}"
            ),
            rolling5_count=count,
            pdt_remaining=0,
            paper_mode=False,
            capital_usd=self._capital_usd,
        )

    def check_can_trade_with_count(
        self,
        symbol: str,
        predicted_count: int,
        trade_date: Optional[datetime.date] = None,
    ) -> PDTCheckResult:
        """同日 round-trip 件数を指定して「追加 N 件発注可能か」を判定する。

        earnings straddle 等、発注前に「この取引が day_trade になるか」を
        事前予測した件数込みで PDT 上限到達を判定するユースケース用。

        Args:
            symbol:          銘柄コード（ログ用）
            predicted_count: 今回の発注が day_trade として加算される予定件数
                             （通常は 1 だが multi-leg 同日クローズ予定は 2 等）
            trade_date:      取引予定日

        Returns:
            PDTCheckResult
        """
        if self._paper_mode:
            return PDTCheckResult(
                allowed=True,
                reason="paper_mode=True: PDT チェックスキップ",
                rolling5_count=0,
                pdt_remaining=float("inf"),
                paper_mode=True,
                capital_usd=self._capital_usd,
            )

        if self._capital_usd >= PDT_THRESHOLD_USD:
            return PDTCheckResult(
                allowed=True,
                reason=(
                    f"capital_usd={self._capital_usd:.0f} >= "
                    f"{PDT_THRESHOLD_USD:.0f}: PDT 非対象"
                ),
                rolling5_count=0,
                pdt_remaining=float("inf"),
                paper_mode=False,
                capital_usd=self._capital_usd,
            )

        ref = trade_date or datetime.datetime.now(_ET).date()
        current_count = self._tracker.count_day_trades_rolling(reference=ref)
        projected = current_count + predicted_count

        from common.pdt_tracker import PDT_LIMIT
        remaining_after = max(0, PDT_LIMIT - projected)

        if projected <= PDT_LIMIT:
            return PDTCheckResult(
                allowed=True,
                reason=(
                    f"PDT 予測内: current={current_count} + predicted={predicted_count} "
                    f"= {projected} <= {PDT_LIMIT}"
                ),
                rolling5_count=current_count,
                pdt_remaining=remaining_after,
                paper_mode=False,
                capital_usd=self._capital_usd,
            )

        log.warning(
            "[PDTGuard] BLOCKED (projected): symbol=%s "
            "current=%d + predicted=%d = %d > limit=%d",
            symbol, current_count, predicted_count, projected, PDT_LIMIT,
        )
        return PDTCheckResult(
            allowed=False,
            reason=(
                f"PDT 上限超過予測: current={current_count} + predicted={predicted_count} "
                f"= {projected} > limit={PDT_LIMIT}"
            ),
            rolling5_count=current_count,
            pdt_remaining=0,
            paper_mode=False,
            capital_usd=self._capital_usd,
        )
