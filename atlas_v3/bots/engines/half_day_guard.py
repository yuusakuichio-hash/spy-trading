"""atlas_v3/bots/engines/half_day_guard.py — 半日取引日 force_close 調整 (L02)

設計思想
--------
NYSE 半日取引日 (感謝祭翌日 Black Friday / クリスマス前日) は 13:00 ET クローズ。
現行 spy_bot.py は 15:50 ET force_close のため半日取引日に発動すると
市場クローズ後のため約定不可。

ソース一次情報
--------------
- NYSE 公式休日カレンダー (nyse.com/markets/hours-calendars)
- 0DTE SPY オプションも 13:00 ET に expiry 扱い
- research_remaining_gaps.md N10 項 (2026-04-14 調査)

実装 (atlas_v3 namespace・spy_bot.py 書換禁止)
----------------------------------------------
- 2026-2027 確定ハードコードリスト
- check(date) → HalfDayInfo(is_half_day, force_close_et)
- force_close_et: 半日=12:45 ET / 通常=15:50 ET
- 年次更新: data/specs/nyse_half_days.yaml に外出し推奨
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

try:
    import zoneinfo
    _ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    import pytz  # type: ignore
    _ET = pytz.timezone("America/New_York")  # type: ignore

log = logging.getLogger(__name__)

# 2026-2027 NYSE 半日取引日 (公式確認済)
# 更新時は nyse.com/markets/hours-calendars を参照
_NYSE_HALF_DAYS_2026_2027: frozenset[str] = frozenset({
    "2026-11-27",  # ブラックフライデー (感謝祭=2026-11-26)
    "2026-12-24",  # クリスマス前日
    # 2026-07-03 は全日休場 (独立記念日 7/4=土 → 観察日 7/3 金)
    "2027-11-26",  # ブラックフライデー (感謝祭=2027-11-25)
    "2027-12-24",  # クリスマス前日
    # 2027-07-04 は日曜 → 観察日 2027-07-05 月 全日休場
})

# 半日取引日の NYSE クローズ時刻 ET
HALF_DAY_CLOSE_ET_H: int = 13
HALF_DAY_CLOSE_ET_M: int = 0

# 半日取引日の force_close マージン (クローズ 15 分前)
HALF_DAY_FORCE_CLOSE_ET_H: int = 12
HALF_DAY_FORCE_CLOSE_ET_M: int = 45

# 通常取引日の force_close (spy_bot.py FORCE_CLOSE_H=15 FORCE_CLOSE_M=50)
NORMAL_FORCE_CLOSE_ET_H: int = 15
NORMAL_FORCE_CLOSE_ET_M: int = 50


@dataclass(frozen=True)
class HalfDayInfo:
    """半日取引日チェック結果 DTO。"""
    is_half_day: bool
    date_str: str
    force_close_h: int
    force_close_m: int
    reason: str

    @property
    def force_close_time_et(self) -> datetime.time:
        """force_close 時刻 (ET)。"""
        return datetime.time(self.force_close_h, self.force_close_m)

    @property
    def force_close_et(self) -> datetime.time:
        """force_close 時刻 (ET) のエイリアス (test API)."""
        return self.force_close_time_et

    @property
    def trade_date(self) -> datetime.date:
        """date_str から date 復元。"""
        return datetime.date.fromisoformat(self.date_str)


class HalfDayGuard:
    """NYSE 半日取引日を検出し force_close 時刻を動的に返すガード。

    例::
        guard = HalfDayGuard()
        info = guard.check(datetime.date(2026, 11, 27))
        # info.is_half_day=True, info.force_close_h=12, info.force_close_m=45
    """

    def __init__(
        self,
        half_days: Optional[frozenset[str]] = None,
    ) -> None:
        self._half_days: frozenset[str] = half_days if half_days is not None else _NYSE_HALF_DAYS_2026_2027

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def check(self, trade_date: datetime.date) -> HalfDayInfo:
        """trade_date が NYSE 半日取引日かを返す。

        Parameters
        ----------
        trade_date: チェック対象日 (ET での市場日)

        Returns
        -------
        HalfDayInfo
        """
        assert isinstance(trade_date, datetime.date), f"trade_date must be date, got {type(trade_date)}"
        date_str = trade_date.isoformat()
        is_half = date_str in self._half_days

        if is_half:
            return HalfDayInfo(
                is_half_day=True,
                date_str=date_str,
                force_close_h=HALF_DAY_FORCE_CLOSE_ET_H,
                force_close_m=HALF_DAY_FORCE_CLOSE_ET_M,
                reason=f"{date_str} is NYSE half-day: force_close={HALF_DAY_FORCE_CLOSE_ET_H:02d}:{HALF_DAY_FORCE_CLOSE_ET_M:02d} ET",
            )

        return HalfDayInfo(
            is_half_day=False,
            date_str=date_str,
            force_close_h=NORMAL_FORCE_CLOSE_ET_H,
            force_close_m=NORMAL_FORCE_CLOSE_ET_M,
            reason=f"{date_str} is normal trading day: force_close={NORMAL_FORCE_CLOSE_ET_H:02d}:{NORMAL_FORCE_CLOSE_ET_M:02d} ET",
        )

    def check_today_et(self) -> HalfDayInfo:
        """現在の ET 時刻から本日のチェックを行う。"""
        today_et = datetime.datetime.now(_ET).date()
        return self.check(today_et)

    def get_all_half_days(self) -> list[str]:
        """登録済み半日取引日一覧を昇順で返す。"""
        return sorted(self._half_days)
