"""
tests/test_econ_events.py — EconEvents モジュール テスト (12テスト)
"""
import sys
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except ImportError:
    import pytz
    ET = pytz.timezone("America/New_York")

from common.econ_events import (
    EconEvent, EventStatus,
    get_event_status, is_entry_blocked,
    _load_calendar_json, _BLACKOUT_MINUTES, _HIGH_IMPACT_EVENTS,
)


def _make_now_et(hour: int, minute: int, date: datetime.date = None) -> datetime.datetime:
    """テスト用のET時刻を作成。"""
    if date is None:
        date = datetime.date(2026, 5, 13)  # CPI発表想定日
    return datetime.datetime(date.year, date.month, date.day, hour, minute,
                             tzinfo=ET)


def _make_cpi_event(date: datetime.date = None, hour: int = 8, minute: int = 30) -> EconEvent:
    """テスト用CPI発表イベントを生成。"""
    if date is None:
        date = datetime.date(2026, 5, 13)
    return EconEvent(
        name="CPI",
        date=date,
        release_time_et=datetime.time(hour, minute),
        impact="high",
        description="CPI Release",
    )


def _make_fomc_event(date: datetime.date = None) -> EconEvent:
    """テスト用FOMCイベントを生成。"""
    if date is None:
        date = datetime.date(2026, 5, 6)
    return EconEvent(
        name="FOMC",
        date=date,
        release_time_et=datetime.time(14, 0),
        impact="high",
        description="FOMC Decision",
    )


def test_no_events_today_not_blocked():
    """当日イベントなしはブラックアウトにならない。"""
    now = _make_now_et(10, 0)
    status = get_event_status(now_et=now, events=[])
    assert status.is_blackout is False
    assert status.blackout_event is None


def test_blackout_before_cpi():
    """CPI発表15分前はブラックアウト。"""
    now = _make_now_et(8, 20)  # 8:30発表の10分前
    events = [_make_cpi_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.is_blackout is True
    assert status.blackout_event == "CPI"


def test_blackout_after_cpi():
    """CPI発表後20分はブラックアウト。"""
    now = _make_now_et(8, 45)  # 8:30発表の15分後
    events = [_make_cpi_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.is_blackout is True
    assert status.post_event is True


def test_not_blackout_before_window():
    """CPI発表1時間前はブラックアウトではない。"""
    now = _make_now_et(7, 0)  # 8:30発表の1.5時間前
    events = [_make_cpi_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.is_blackout is False


def test_not_blackout_after_window():
    """CPI発表1時間後はブラックアウト解除。"""
    now = _make_now_et(10, 0)  # 8:30発表の1.5時間後
    events = [_make_cpi_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.is_blackout is False


def test_fomc_longer_blackout():
    """FOMCは前30分ブラックアウト。"""
    now = _make_now_et(13, 40, datetime.date(2026, 5, 6))  # 14:00の20分前
    events = [_make_fomc_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.is_blackout is True
    assert status.blackout_event == "FOMC"


def test_high_impact_flag():
    """FOMC/CPI/NFPは is_high_impact=True。"""
    now = _make_now_et(8, 25)  # CPI 5分前
    events = [_make_cpi_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.is_high_impact is True


def test_straddle_signal_post_fomc():
    """FOMC発表後30分以内は straddle_signal=True。"""
    now = _make_now_et(14, 15, datetime.date(2026, 5, 6))  # 14:00発表の15分後
    events = [_make_fomc_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.straddle_signal is True


def test_is_entry_blocked_true():
    """ブラックアウト中は is_entry_blocked=True。"""
    now = _make_now_et(8, 25)
    events = [_make_cpi_event()]
    blocked = is_entry_blocked(now_et=now, events=events)
    assert blocked is True


def test_is_entry_blocked_false():
    """ブラックアウト外は is_entry_blocked=False。"""
    now = _make_now_et(12, 0)
    events = [_make_cpi_event()]
    blocked = is_entry_blocked(now_et=now, events=events)
    assert blocked is False


def test_different_day_not_blocked():
    """別日のイベントは当日ブラックアウトに影響しない。"""
    today   = datetime.date(2026, 5, 14)
    now     = datetime.datetime(2026, 5, 14, 10, 0, tzinfo=ET)
    # 昨日のCPI
    events  = [_make_cpi_event(date=datetime.date(2026, 5, 13))]
    status  = get_event_status(now_et=now, events=events)
    assert status.is_blackout is False


def test_minutes_to_event_is_populated():
    """次のイベントまでの分数が設定される。"""
    now = _make_now_et(7, 0)  # 8:30発表の90分前
    events = [_make_cpi_event()]
    status = get_event_status(now_et=now, events=events)
    assert status.minutes_to_event is not None
    assert status.minutes_to_event > 0


def test_load_calendar_json_invalid_path():
    """存在しないパスは空リストを返す (エラーにならない)。"""
    events = _load_calendar_json([Path("/nonexistent/path.json")])
    assert isinstance(events, list)
    assert len(events) == 0


if __name__ == "__main__":
    tests = [
        test_no_events_today_not_blocked,
        test_blackout_before_cpi,
        test_blackout_after_cpi,
        test_not_blackout_before_window,
        test_not_blackout_after_window,
        test_fomc_longer_blackout,
        test_high_impact_flag,
        test_straddle_signal_post_fomc,
        test_is_entry_blocked_true,
        test_is_entry_blocked_false,
        test_different_day_not_blocked,
        test_minutes_to_event_is_populated,
        test_load_calendar_json_invalid_path,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
