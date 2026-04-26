"""common/itm_risk_check.py — ITM接近検知 + 強制クローズ判定

PDT観点でのITM回避設計:
  - SHORT legがITMに入ると自動行使リスクが生じる（現金決済のSPX除く）
  - ITM自動行使はPDT対象外だが、原資産受渡しが発生する（SPY等の現物決済）
  - PDT消費してでもITM回避が必要なケース → manual_close（PDT day_trade として計上）

検知ロジック:
  - 15:30 ET: SHORT leg の OTM距離 < ITM_WARNING_DISTANCE_USD → WARNING
  - 15:45 ET: SHORT leg が ITM化濃厚 → 強制 manual_close 推奨 + Pushover priority=1

使い方:
    from common.itm_risk_check import ITMRiskChecker
    checker = ITMRiskChecker()

    # SHORT legのストライクと現在価格を渡す
    risk = checker.check(
        underlying_price=560.50,
        short_strike=560.0,
        option_side="CALL",  # "CALL" or "PUT"
        now_et=datetime.datetime.now(ET),
    )
    # risk.should_force_close → True なら強制クローズ推奨
    # risk.is_warning → True なら警告レベル
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    import pytz  # type: ignore
    ET = pytz.timezone("America/New_York")  # type: ignore

# ITM接近の警告閾値: SHORT legのOTM距離がこれ未満 → WARNING
ITM_WARNING_DISTANCE_USD = 0.50  # $0.50 未満で警告

# 強制クローズの時刻閾値（ET）
ITM_FORCE_CLOSE_WARNING_H = 15
ITM_FORCE_CLOSE_WARNING_M = 30  # 15:30 ET → 警告
ITM_FORCE_CLOSE_EXECUTE_H = 15
ITM_FORCE_CLOSE_EXECUTE_M = 45  # 15:45 ET → 強制クローズ推奨


@dataclass
class ITMRiskResult:
    """ITM接近チェック結果。"""
    is_warning: bool           # 15:30以降にOTM距離 < $0.50
    should_force_close: bool   # 15:45以降にITM化濃厚 → 強制クローズ推奨
    otm_distance: float        # 現在のOTM距離（$）。マイナス = 既にITM
    short_strike: float
    underlying_price: float
    option_side: str           # "CALL" or "PUT"
    now_et: datetime.datetime
    message: str               # Pushover送信用メッセージ


class ITMRiskChecker:
    """SHORT legのITM接近を検知してPDT強制クローズを判定するクラス。

    PDT消費してでもITM回避が必要かどうかを判定する。
    SPX（現金決済）は自動行使リスクなしだが警告は出す。
    """

    def __init__(self, warning_distance: float = ITM_WARNING_DISTANCE_USD) -> None:
        """
        Args:
            warning_distance: OTM距離の警告閾値（$）。デフォルト $0.50
        """
        self.warning_distance = warning_distance

    def check(
        self,
        underlying_price: float,
        short_strike: float,
        option_side: str,
        now_et: Optional[datetime.datetime] = None,
        is_cash_settled: bool = False,
    ) -> ITMRiskResult:
        """SHORT legのITM接近リスクをチェックする。

        Args:
            underlying_price: 原資産の現在価格（$）
            short_strike:     SHORT legのストライク価格（$）
            option_side:      "CALL" または "PUT"
            now_et:           現在のET時刻（Noneなら自動取得）
            is_cash_settled:  True = 現金決済（SPX等）→ 自動行使リスクなし

        Returns:
            ITMRiskResult
        """
        if now_et is None:
            now_et = datetime.datetime.now(ET)

        # OTM距離を計算
        # CALL: SHORT strike - underlying（プラスがOTM）
        # PUT: underlying - SHORT strike（プラスがOTM）
        if option_side.upper() == "CALL":
            otm_distance = short_strike - underlying_price
        else:  # PUT
            otm_distance = underlying_price - short_strike

        # 時刻判定
        cur_min = now_et.hour * 60 + now_et.minute
        warn_min = ITM_FORCE_CLOSE_WARNING_H * 60 + ITM_FORCE_CLOSE_WARNING_M
        exec_min = ITM_FORCE_CLOSE_EXECUTE_H * 60 + ITM_FORCE_CLOSE_EXECUTE_M

        # 警告判定: 15:30以降 + OTM距離 < 閾値
        is_warning = (cur_min >= warn_min) and (otm_distance < self.warning_distance)

        # 強制クローズ判定: 15:45以降 + ITM化（OTM距離 <= 0）または接近（< $0.50）
        should_force_close = (
            (cur_min >= exec_min) and (otm_distance < self.warning_distance)
        )

        # 現金決済は自動行使リスクなし → should_force_close を緩和
        # ただし警告は継続（レポート目的）
        if is_cash_settled:
            should_force_close = False

        # メッセージ生成
        itm_label = "ITM" if otm_distance <= 0 else f"OTM ${otm_distance:.2f}"
        settled_label = "（現金決済・行使リスクなし）" if is_cash_settled else ""
        if should_force_close:
            message = (
                f"[Atlas/PDT] ITM強制クローズ推奨{settled_label}\n"
                f"{option_side} SHORT {short_strike:.0f} vs 原資産 ${underlying_price:.2f}\n"
                f"OTM距離: {itm_label}\n"
                f"PDT消費してITM回避推奨 → manual_close実行"
            )
        elif is_warning:
            message = (
                f"[Atlas/PDT] ITM接近警告{settled_label}\n"
                f"{option_side} SHORT {short_strike:.0f} vs 原資産 ${underlying_price:.2f}\n"
                f"OTM距離: {itm_label} → 15:45でITM強制クローズの可能性"
            )
        else:
            message = (
                f"[PDT] ITMリスク正常: {option_side} SHORT {short_strike:.0f} "
                f"OTM ${otm_distance:.2f}"
            )

        if is_warning or should_force_close:
            log.warning(f"[ITMRisk] {message}")
        else:
            log.debug(f"[ITMRisk] {message}")

        return ITMRiskResult(
            is_warning=is_warning,
            should_force_close=should_force_close,
            otm_distance=otm_distance,
            short_strike=short_strike,
            underlying_price=underlying_price,
            option_side=option_side,
            now_et=now_et,
            message=message,
        )

    def check_spread(
        self,
        underlying_price: float,
        call_short_strike: Optional[float],
        put_short_strike: Optional[float],
        now_et: Optional[datetime.datetime] = None,
        is_cash_settled: bool = False,
    ) -> Optional[ITMRiskResult]:
        """Credit Spread / Iron Condor の全SHORT legをまとめてチェックする。

        2つのSHORT legのうち、より危険な方（OTM距離が小さい方）を返す。

        Args:
            underlying_price:  原資産の現在価格（$）
            call_short_strike: CALL SHORT ストライク（Noneなら無視）
            put_short_strike:  PUT SHORT ストライク（Noneなら無視）
            now_et:            現在のET時刻
            is_cash_settled:   True = 現金決済（SPX等）

        Returns:
            最も危険なITMRiskResult（危険なものがなければNone）
        """
        results: list[ITMRiskResult] = []
        if call_short_strike is not None:
            results.append(self.check(
                underlying_price, call_short_strike, "CALL",
                now_et, is_cash_settled
            ))
        if put_short_strike is not None:
            results.append(self.check(
                underlying_price, put_short_strike, "PUT",
                now_et, is_cash_settled
            ))

        if not results:
            return None

        # OTM距離が最小（最も危険）なものを返す
        return min(results, key=lambda r: r.otm_distance)


# ── グローバルシングルトン ────────────────────────────────────────────────────

_global_checker: Optional[ITMRiskChecker] = None


def get_global_itm_checker() -> ITMRiskChecker:
    """プロセスごとのシングルトンを返す。"""
    global _global_checker
    if _global_checker is None:
        _global_checker = ITMRiskChecker()
    return _global_checker
