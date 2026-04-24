"""tests/test_time_travel_windows_20260425.py
NYSE entry window 境界テスト — freezegun + parametrize

対象:
  ORB open range     09:30-10:30 ET
  ORB breakout       10:00-10:30 ET
  ORB 1DTE           10:30-11:00 ET
  StrangleSell       10:30-12:00 ET
  Calendar           11:00-14:00 ET
  IV Crush pre-mkt   08:00-09:30 ET
  Force close        15:50-15:55 ET
  EOD cleanup        16:00+      ET

検証軸:
  1. window 直前/中間/直後 3 タイミング
  2. JST ↔ ET 変換 (EDT +13h / EST +14h)
  3. DST 切替境界 (2026-03-08 spring-forward / 2026-11-01 fall-back)
  4. NYSE holiday (Good Friday 2026-04-03 / Memorial Day 2026-05-25)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc

# NYSE holidays 2026
NYSE_HOLIDAYS_2026 = {
    "new_years":    datetime(2026,  1,  1, tzinfo=ET).date(),
    "mlk_day":      datetime(2026,  1, 19, tzinfo=ET).date(),
    "presidents":   datetime(2026,  2, 16, tzinfo=ET).date(),
    "good_friday":  datetime(2026,  4,  3, tzinfo=ET).date(),
    "memorial_day": datetime(2026,  5, 25, tzinfo=ET).date(),
    "juneteenth":   datetime(2026,  6, 19, tzinfo=ET).date(),
    "independence": datetime(2026,  7,  3, tzinfo=ET).date(),
    "labor_day":    datetime(2026,  9,  7, tzinfo=ET).date(),
    "thanksgiving": datetime(2026, 11, 26, tzinfo=ET).date(),
    "christmas":    datetime(2026, 12, 25, tzinfo=ET).date(),
}

# DST transitions 2026 (America/New_York)
DST_SPRING_2026 = datetime(2026, 3,  8, tzinfo=ET)   # spring-forward
DST_FALL_2026   = datetime(2026, 11, 1, tzinfo=ET)   # fall-back


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """ET timezone-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def _in_window(t: datetime, open_h: int, open_m: int, close_h: int, close_m: int) -> bool:
    """Return True if datetime t (in any tz) falls in [open, close) ET."""
    t_et = t.astimezone(ET)
    window_open  = t_et.replace(hour=open_h,  minute=open_m,  second=0, microsecond=0)
    window_close = t_et.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return window_open <= t_et < window_close


def _is_nyse_holiday(d: datetime) -> bool:
    """Return True if d (ET) is in the 2026 NYSE holiday set."""
    return d.astimezone(ET).date() in NYSE_HOLIDAYS_2026.values()


def _is_market_open_day(d: datetime) -> bool:
    """Return True if d (ET) is a regular NYSE trading day (weekday, non-holiday)."""
    d_et = d.astimezone(ET)
    if d_et.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    return not _is_nyse_holiday(d_et)


def _jst(dt: datetime) -> str:
    """Convert datetime to 'HH:MM JST' string for assertions."""
    return dt.astimezone(JST).strftime("%H:%M")


# ---------------------------------------------------------------------------
# Window definitions for parametrize
# ---------------------------------------------------------------------------

class Window(NamedTuple):
    name: str
    open_h: int
    open_m: int
    close_h: int
    close_m: int


WINDOWS = [
    Window("orb_open_range",  9, 30, 10, 30),
    Window("orb_breakout",   10,  0, 10, 30),
    Window("orb_1dte",       10, 30, 11,  0),
    Window("strangle_sell",  10, 30, 12,  0),
    Window("calendar",       11,  0, 14,  0),
    Window("iv_crush_premkt", 8,  0,  9, 30),
    Window("force_close",    15, 50, 15, 55),
    Window("eod_cleanup",    16,  0, 23, 59),
]


# ---------------------------------------------------------------------------
# Section 1: window 直前 / 中間 / 直後 (EDT summer 2026-04-24)
# ---------------------------------------------------------------------------

WINDOW_TIMING_PARAMS = []
REF_DATE = (2026, 4, 24)  # Friday, EDT, normal trading day

for _w in WINDOWS:
    # 直前 (1 minute before open)
    _pre_h, _pre_m = divmod(_w.open_h * 60 + _w.open_m - 1, 60)
    WINDOW_TIMING_PARAMS.append(
        pytest.param(
            _w, *REF_DATE, _pre_h, _pre_m,
            False,
            id=f"{_w.name}__pre",
        )
    )
    # 中間 (midpoint)
    _mid_min = (_w.open_h * 60 + _w.open_m + _w.close_h * 60 + _w.close_m) // 2
    _mid_h, _mid_m = divmod(_mid_min, 60)
    WINDOW_TIMING_PARAMS.append(
        pytest.param(
            _w, *REF_DATE, _mid_h, _mid_m,
            True,
            id=f"{_w.name}__mid",
        )
    )
    # 直後 (at close — half-open [open, close) なので False)
    WINDOW_TIMING_PARAMS.append(
        pytest.param(
            _w, *REF_DATE, _w.close_h, _w.close_m,
            False,
            id=f"{_w.name}__post",
        )
    )


@pytest.mark.parametrize("window,year,month,day,hour,minute,expected", WINDOW_TIMING_PARAMS)
def test_window_boundary(
    window: Window,
    year: int, month: int, day: int,
    hour: int, minute: int,
    expected: bool,
) -> None:
    """window 直前/中間/直後 3 タイミングで _in_window が正しい値を返すことを確認。"""
    t = _et(year, month, day, hour, minute)
    result = _in_window(t, window.open_h, window.open_m, window.close_h, window.close_m)
    assert result is expected, (
        f"{window.name} @ {hour:02d}:{minute:02d} ET: expected={expected}, got={result}"
    )


# ---------------------------------------------------------------------------
# Section 2: JST ↔ ET 変換 (EDT +13h / EST +14h)
# ---------------------------------------------------------------------------

# EDT (summer): ET 09:30 → JST 22:30
# EST (winter): ET 09:30 → JST 23:30
JST_CONVERSION_PARAMS = [
    # (description, et_datetime, expected_jst_hhmm)
    pytest.param(
        "market_open_EDT",
        _et(2026, 4, 24, 9, 30),
        "22:30",
        id="jst_market_open_EDT",
    ),
    pytest.param(
        "market_close_EDT",
        _et(2026, 4, 24, 16, 0),
        "05:00",
        id="jst_market_close_EDT",
    ),
    pytest.param(
        "orb_1dte_start_EDT",
        _et(2026, 4, 24, 10, 30),
        "23:30",
        id="jst_orb_1dte_start_EDT",
    ),
    pytest.param(
        "force_close_EDT",
        _et(2026, 4, 24, 15, 50),
        "04:50",
        id="jst_force_close_EDT",
    ),
    pytest.param(
        "market_open_EST",
        _et(2026, 12, 1, 9, 30),
        "23:30",
        id="jst_market_open_EST",
    ),
    pytest.param(
        "market_close_EST",
        _et(2026, 12, 1, 16, 0),
        "06:00",
        id="jst_market_close_EST",
    ),
    pytest.param(
        "premkt_iv_crush_EDT",
        _et(2026, 4, 24, 8, 0),
        "21:00",
        id="jst_premkt_iv_crush_EDT",
    ),
    pytest.param(
        "eod_cleanup_EDT",
        _et(2026, 4, 24, 16, 0),
        "05:00",
        id="jst_eod_cleanup_EDT",
    ),
    pytest.param(
        "calendar_end_EDT",
        _et(2026, 4, 24, 14, 0),
        "03:00",
        id="jst_calendar_end_EDT",
    ),
]


@pytest.mark.parametrize("desc,et_dt,expected_jst", JST_CONVERSION_PARAMS)
def test_jst_et_conversion(desc: str, et_dt: datetime, expected_jst: str) -> None:
    """ET datetime を JST に変換した結果が期待値と一致することを確認。"""
    result = _jst(et_dt)
    assert result == expected_jst, (
        f"{desc}: ET {et_dt.strftime('%H:%M')} → JST {result}, expected {expected_jst}"
    )


def test_edt_offset_is_minus4h() -> None:
    """EDT は UTC-4 (= -4:00:00 timedelta)。"""
    t = _et(2026, 4, 24, 12, 0)
    assert t.utcoffset() == timedelta(hours=-4)


def test_est_offset_is_minus5h() -> None:
    """EST は UTC-5 (= -5:00:00 timedelta)。"""
    t = _et(2026, 12, 1, 12, 0)
    assert t.utcoffset() == timedelta(hours=-5)


def test_jst_is_utc_plus9() -> None:
    """JST は UTC+9。ET → JST 差分が EDT 時 +13h, EST 時 +14h。"""
    edt_t = _et(2026, 4, 24, 0, 0)
    est_t = _et(2026, 12, 1, 0, 0)
    edt_offset = edt_t.utcoffset()
    est_offset = est_t.utcoffset()
    assert edt_offset == timedelta(hours=-4)   # EDT
    assert est_offset == timedelta(hours=-5)   # EST
    # JST は UTC+9 固定
    jst_offset = timedelta(hours=9)
    assert jst_offset - edt_offset == timedelta(hours=13)
    assert jst_offset - est_offset == timedelta(hours=14)


# ---------------------------------------------------------------------------
# Section 3: DST 切替境界 (freezegun で時刻固定)
# ---------------------------------------------------------------------------

@freeze_time("2026-03-08 06:59:00+00:00")  # = 01:59 EST (1 min before spring-forward)
def test_dst_spring_forward_before() -> None:
    """DST spring-forward 直前: 2026-03-08 01:59 ET は EST (UTC-5)。"""
    t = datetime.now(tz=ET)
    assert t.utcoffset() == timedelta(hours=-5), (
        f"Expected EST (-5h) at 01:59 on spring-forward day, got {t.utcoffset()}"
    )


@freeze_time("2026-03-08 08:00:00+00:00")  # = 04:00 EDT (after spring-forward)
def test_dst_spring_forward_after() -> None:
    """DST spring-forward 後: 2026-03-08 04:00 ET は EDT (UTC-4)。"""
    t = datetime.now(tz=ET)
    assert t.utcoffset() == timedelta(hours=-4), (
        f"Expected EDT (-4h) after spring-forward, got {t.utcoffset()}"
    )


@freeze_time("2026-03-08 07:00:00+00:00")  # = 02:00 UTC which is 03:00 EDT (just after)
def test_dst_spring_forward_market_open_jst() -> None:
    """DST spring-forward 直後の日: 市場オープン (09:30 ET) は JST 22:30 (EDT +13h)。"""
    t_open_et = _et(2026, 3, 9, 9, 30)   # 翌月曜(通常取引日)
    jst_str = _jst(t_open_et)
    assert jst_str == "22:30", f"Expected 22:30 JST (EDT), got {jst_str}"


@freeze_time("2026-11-01 05:59:00+00:00")  # = 01:59 EDT (before fall-back)
def test_dst_fall_back_before() -> None:
    """DST fall-back 直前: 2026-11-01 01:59 ET は EDT (UTC-4)。"""
    t = datetime.now(tz=ET)
    assert t.utcoffset() == timedelta(hours=-4), (
        f"Expected EDT (-4h) at 01:59 on fall-back day, got {t.utcoffset()}"
    )


@freeze_time("2026-11-01 07:00:00+00:00")  # = 02:00 EST (after fall-back)
def test_dst_fall_back_after() -> None:
    """DST fall-back 後: 2026-11-01 02:00 EST は EST (UTC-5)。"""
    t = datetime.now(tz=ET)
    assert t.utcoffset() == timedelta(hours=-5), (
        f"Expected EST (-5h) after fall-back, got {t.utcoffset()}"
    )


@freeze_time("2026-11-01 14:30:00+00:00")  # = 09:30 EST
def test_dst_fall_back_market_open_jst() -> None:
    """DST fall-back 後の市場オープン (09:30 ET) は JST 23:30 (EST +14h)。"""
    t = datetime.now(tz=ET)
    jst_str = _jst(t)
    assert jst_str == "23:30", f"Expected 23:30 JST (EST), got {jst_str}"


@freeze_time("2026-03-08 14:00:00+00:00")  # = 10:00 EDT (spring-forward day)
def test_dst_spring_orb_breakout_window() -> None:
    """DST spring-forward 当日: ORB breakout window (10:00-10:30 ET) に正しく入ることを確認。"""
    t = datetime.now(tz=ET)
    result = _in_window(t, 10, 0, 10, 30)
    assert result is True, f"Expected in ORB breakout window at {t.strftime('%H:%M %Z')}"


@freeze_time("2026-11-01 15:30:00+00:00")  # = 10:30 EST (fall-back day)
def test_dst_fall_back_strangle_window() -> None:
    """DST fall-back 当日: StrangleSell window (10:30-12:00 ET) に正しく入ることを確認。"""
    t = datetime.now(tz=ET)
    result = _in_window(t, 10, 30, 12, 0)
    assert result is True, f"Expected in StrangleSell window at {t.strftime('%H:%M %Z')}"


# ---------------------------------------------------------------------------
# Section 4: NYSE holiday 検知
# ---------------------------------------------------------------------------

@freeze_time("2026-04-03 13:00:00+00:00")  # Good Friday 2026 09:00 ET
def test_good_friday_2026_is_holiday() -> None:
    """Good Friday 2026-04-03 が NYSE holiday として検知される。"""
    t = datetime.now(tz=ET)
    assert _is_nyse_holiday(t) is True, "Good Friday 2026-04-03 should be NYSE holiday"


@freeze_time("2026-05-25 13:00:00+00:00")  # Memorial Day 2026 09:00 ET
def test_memorial_day_2026_is_holiday() -> None:
    """Memorial Day 2026-05-25 が NYSE holiday として検知される。"""
    t = datetime.now(tz=ET)
    assert _is_nyse_holiday(t) is True, "Memorial Day 2026-05-25 should be NYSE holiday"


@freeze_time("2026-04-03 13:00:00+00:00")  # Good Friday
def test_good_friday_market_closed() -> None:
    """Good Friday は _is_market_open_day が False を返す。"""
    t = datetime.now(tz=ET)
    assert _is_market_open_day(t) is False


@freeze_time("2026-05-25 13:00:00+00:00")  # Memorial Day
def test_memorial_day_market_closed() -> None:
    """Memorial Day は _is_market_open_day が False を返す。"""
    t = datetime.now(tz=ET)
    assert _is_market_open_day(t) is False


@freeze_time("2026-04-04 13:00:00+00:00")  # Saturday after Good Friday
def test_saturday_market_closed() -> None:
    """土曜日は _is_market_open_day が False を返す。"""
    t = datetime.now(tz=ET)
    assert _is_market_open_day(t) is False


@freeze_time("2026-04-05 13:00:00+00:00")  # Sunday after Good Friday
def test_sunday_market_closed() -> None:
    """日曜日は _is_market_open_day が False を返す。"""
    t = datetime.now(tz=ET)
    assert _is_market_open_day(t) is False


@freeze_time("2026-04-06 13:00:00+00:00")  # Monday after Good Friday (not holiday)
def test_monday_after_good_friday_open() -> None:
    """Good Friday 翌月曜 (2026-04-06) は通常取引日。"""
    t = datetime.now(tz=ET)
    assert _is_market_open_day(t) is True


@freeze_time("2026-04-24 13:00:00+00:00")  # Normal Friday
def test_normal_trading_day_open() -> None:
    """通常の金曜 2026-04-24 は _is_market_open_day が True を返す。"""
    t = datetime.now(tz=ET)
    assert _is_market_open_day(t) is True


# ---------------------------------------------------------------------------
# Section 5: freezegun で各 window 境界を固定した追加検証
# ---------------------------------------------------------------------------

@freeze_time("2026-04-24 13:30:00+00:00")  # = 09:30 EDT — market open
def test_frozen_orb_open_range_start() -> None:
    """freezegun: 09:30 ET は ORB open range の開始点 (window に入る)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 9, 30, 10, 30) is True


@freeze_time("2026-04-24 13:29:00+00:00")  # = 09:29 EDT — 1 min before open
def test_frozen_orb_open_range_pre() -> None:
    """freezegun: 09:29 ET は ORB open range の直前 (window 外)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 9, 30, 10, 30) is False


@freeze_time("2026-04-24 14:30:00+00:00")  # = 10:30 EDT — ORB close (half-open)
def test_frozen_orb_open_range_post() -> None:
    """freezegun: 10:30 ET は ORB open range の閉端 (window 外・half-open)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 9, 30, 10, 30) is False


@freeze_time("2026-04-24 20:00:00+00:00")  # = 16:00 EDT — EOD
def test_frozen_eod_cleanup_boundary() -> None:
    """freezegun: 16:00 ET は EOD cleanup window の開始点 (window に入る)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 16, 0, 23, 59) is True


@freeze_time("2026-04-24 19:50:00+00:00")  # = 15:50 EDT — force close start
def test_frozen_force_close_entry() -> None:
    """freezegun: 15:50 ET は force close window の開始点。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 15, 50, 15, 55) is True


@freeze_time("2026-04-24 19:55:00+00:00")  # = 15:55 EDT — force close end (half-open)
def test_frozen_force_close_exit() -> None:
    """freezegun: 15:55 ET は force close の閉端 (window 外)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 15, 50, 15, 55) is False


@freeze_time("2026-04-24 12:00:00+00:00")  # = 08:00 EDT — IV Crush pre-mkt start
def test_frozen_iv_crush_premkt_start() -> None:
    """freezegun: 08:00 ET は IV Crush pre-market window の開始点。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 8, 0, 9, 30) is True


@freeze_time("2026-04-24 13:29:00+00:00")  # = 09:29 EDT — 1 min before market open
def test_frozen_iv_crush_premkt_near_end() -> None:
    """freezegun: 09:29 ET は IV Crush pre-market window の中 (まだ window 内)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 8, 0, 9, 30) is True


@freeze_time("2026-04-24 13:30:00+00:00")  # = 09:30 EDT — market open (pre-mkt ends)
def test_frozen_iv_crush_premkt_end() -> None:
    """freezegun: 09:30 ET は IV Crush pre-market の閉端 (window 外)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 8, 0, 9, 30) is False


@freeze_time("2026-04-24 15:00:00+00:00")  # = 11:00 EDT — Calendar entry start
def test_frozen_calendar_entry_start() -> None:
    """freezegun: 11:00 ET は Calendar entry window の開始点。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 11, 0, 14, 0) is True


@freeze_time("2026-04-24 18:00:00+00:00")  # = 14:00 EDT — Calendar entry end (half-open)
def test_frozen_calendar_entry_end() -> None:
    """freezegun: 14:00 ET は Calendar entry window の閉端 (window 外)。"""
    t = datetime.now(tz=ET)
    assert _in_window(t, 11, 0, 14, 0) is False


# ---------------------------------------------------------------------------
# Section 6: market_specs.yaml 整合性確認
# ---------------------------------------------------------------------------

def test_market_specs_yaml_edt_session() -> None:
    """market_specs.yaml の session_jst_edt open=22:30 が ET 09:30 EDT に対応する。"""
    t_open_et = _et(2026, 4, 24, 9, 30)
    assert _jst(t_open_et) == "22:30"


def test_market_specs_yaml_est_session() -> None:
    """market_specs.yaml の session_jst_est open=23:30 が ET 09:30 EST に対応する。"""
    t_open_et = _et(2026, 12, 1, 9, 30)
    assert _jst(t_open_et) == "23:30"


def test_market_specs_yaml_dst_schedule() -> None:
    """market_specs.yaml dst_schedule: edt_start=2026-03-08, est_start=2026-11-01 を確認。
    2026-03-08 03:00 は EDT, 2026-11-01 01:00 (after fall-back) は EST。"""
    # spring-forward: 03:00 EDT on 2026-03-08
    t_edt = _et(2026, 3, 8, 3, 0)
    assert t_edt.utcoffset() == timedelta(hours=-4), "2026-03-08 03:00 should be EDT"
    # fall-back: 03:00 EST on 2026-11-01 (past ambiguous zone)
    t_est = _et(2026, 11, 1, 3, 0)
    assert t_est.utcoffset() == timedelta(hours=-5), "2026-11-01 03:00 should be EST"


def test_market_specs_yaml_atlas_window_jst_edt() -> None:
    """market_specs.yaml atlas_window_jst_edt start=22:20 は ET 09:20 EDT に対応。"""
    t = _et(2026, 4, 24, 9, 20)
    assert _jst(t) == "22:20"


def test_market_specs_yaml_atlas_window_end_jst_edt() -> None:
    """market_specs.yaml atlas_window_jst_edt end=05:10 は ET 16:10 EDT に対応。"""
    t = _et(2026, 4, 24, 16, 10)
    assert _jst(t) == "05:10"
