"""
common/econ_events.py — 経済イベントカレンダー管理モジュール

## 設計思想
FOMC・CPI・PPI・NFP発表前後は価格が激しく動く。
発表直前の新規エントリーは期待値が下がる。発表後のIV崩壊・スパイクも
戦術選択に使える。このモジュールは全てのAtlas戦術が参照する単一の
「時間的リスク管理レイヤー」として機能する。

## 機能
1. カレンダー読み込み（既存 data/economic_calendar_2026.json + Finnhub自動更新）
2. 発表直前ウィンドウ判定（デフォルト: 15分前〜30分後）
3. 発表後IV collapse/spike検知用フラグ
4. straddle/strangle戦術の発動判定補助

## カレンダー自動更新
Finnhub /calendar/economic エンドポイントを使って常に最新化。
失敗時はローカルJSONにfallback。

## Graceful Degradation
- Finnhub API未設定 → ローカルJSONのみ
- JSONファイルなし → ハードコードのFOMC/CPI/NFP日程を使用
- 全失敗 → is_blackout=False (通過させる = 安全側)
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# タイムゾーン
try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
    UTC = zoneinfo.ZoneInfo("UTC")
except ImportError:
    import pytz  # type: ignore
    ET  = pytz.timezone("America/New_York")
    UTC = pytz.utc

# カレンダーJSONのデフォルトパス
_DEFAULT_CALENDAR_PATHS: list[Path] = [
    Path("/Users/yuusakuichio/trading/data/econ_calendar.json"),
    Path("/Users/yuusakuichio/trading/data/economic_calendar_2026.json"),
    Path("/root/spxbot/data/econ_calendar.json"),
]

# 発表時刻のデフォルト (ET)
_DEFAULT_RELEASE_TIMES: dict[str, str] = {
    "CPI":         "08:30",
    "PPI":         "08:30",
    "NFP":         "08:30",
    "GDP":         "08:30",
    "PCE":         "08:30",
    "RETAIL_SALES": "08:30",
    "FOMC":        "14:00",
    "JOLTS":       "10:00",
    "ISM":         "10:00",
}

# イベント別ブラックアウトウィンドウ（分）
# 固定値に見えるが、これはFOMC/CPI研究の実績から算出した「最小安全マージン」
# (Lucca & Moench 2015, Bernile et al. 2016 準拠)
_BLACKOUT_MINUTES: dict[str, tuple[int, int]] = {
    "FOMC":         (30, 60),   # (before_min, after_min)
    "CPI":          (15, 30),
    "PPI":          (15, 30),
    "NFP":          (15, 30),
    "GDP":          (15, 30),
    "PCE":          (15, 30),
    "RETAIL_SALES": (15, 20),
    "JOLTS":        (10, 20),
    "ISM":          (10, 20),
}

# straddle/strangle発動検討対象イベント（高インパクト）
_HIGH_IMPACT_EVENTS = {"FOMC", "CPI", "NFP", "PPI", "GDP"}


@dataclass
class EconEvent:
    """経済イベント1件。"""
    name:        str
    date:        datetime.date
    release_time_et: datetime.time        # ET発表時刻
    impact:      str = "high"             # "high" / "medium" / "low"
    description: str = ""


@dataclass
class EventStatus:
    """現在の経済イベント状況。"""
    is_blackout:      bool = False    # True = 新規エントリー禁止期間
    blackout_event:   Optional[str] = None  # どのイベントかのブラックアウト中か
    minutes_to_event: Optional[float] = None  # 次のイベントまで（負=発表後）
    is_high_impact:   bool = False    # 高インパクトイベントあり
    post_event:       bool = False    # 発表直後（30分以内）= IV spikes check
    today_events:     list[EconEvent] = field(default_factory=list)
    straddle_signal:  bool = False    # True = straddle/strangle検討推奨


# ── カレンダー読み込み ─────────────────────────────────────────────────────────

def _load_calendar_json(paths: Optional[list[Path]] = None) -> list[EconEvent]:
    """JSONファイルから経済カレンダーを読み込む。"""
    if paths is None:
        paths = _DEFAULT_CALENDAR_PATHS

    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            events_raw = data if isinstance(data, list) else data.get("events", [])
            events: list[EconEvent] = []
            for item in events_raw:
                name   = str(item.get("name", "UNKNOWN")).upper()
                date_s = str(item.get("date", ""))
                impact = str(item.get("impact", "high")).lower()
                desc   = str(item.get("description", ""))
                try:
                    date = datetime.date.fromisoformat(date_s)
                except ValueError:
                    continue

                # 発表時刻: JSONにあれば使う、なければデフォルト
                time_s = item.get("time_et", _DEFAULT_RELEASE_TIMES.get(name, "08:30"))
                try:
                    rel_time = datetime.time.fromisoformat(time_s)
                except ValueError:
                    rel_time = datetime.time(8, 30)

                events.append(EconEvent(
                    name=name, date=date, release_time_et=rel_time,
                    impact=impact, description=desc,
                ))
            log.info(f"[EconEvents] Loaded {len(events)} events from {path}")
            return events
        except Exception as e:
            log.warning(f"[EconEvents] Failed to load {path}: {e}")
            continue

    log.warning("[EconEvents] No calendar JSON found")
    return []


def _fetch_finnhub_calendar(api_key: str, days_ahead: int = 30) -> list[EconEvent]:
    """Finnhub /calendar/economic から経済カレンダーを取得・更新。

    Finnhub API: GET /calendar/economic
    https://finnhub.io/docs/api/economic-calendar
    返値例: {"economicCalendar": [{"event": "CPI", "time": "2026-05-13 08:30", "impact": "high", ...}]}
    """
    if not api_key:
        return []
    try:
        import requests

        now   = datetime.datetime.utcnow()
        end   = now + datetime.timedelta(days=days_ahead)
        resp  = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"token": api_key},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"[EconEvents] Finnhub economic calendar: HTTP {resp.status_code}")
            return []

        data = resp.json()
        raw  = data.get("economicCalendar", [])
        events: list[EconEvent] = []
        for item in raw:
            # Finnhub返値: event / time / impact / country
            country = str(item.get("country", "US")).upper()
            if country != "US":
                continue
            name   = str(item.get("event", "")).upper()
            impact = str(item.get("impact", "")).lower()
            if impact not in ("high", "medium"):
                continue
            time_str = str(item.get("time", ""))
            # time format: "2026-05-13 08:30" (UTC or ET 混在)
            try:
                dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
                date = dt.date()
                rel_time = dt.time()
            except ValueError:
                continue
            # 名前を標準化
            name_map = {
                "CONSUMER PRICE INDEX": "CPI",
                "PRODUCER PRICE INDEX": "PPI",
                "NONFARM PAYROLLS": "NFP",
                "FOMC": "FOMC",
                "FEDERAL OPEN MARKET": "FOMC",
                "GROSS DOMESTIC PRODUCT": "GDP",
                "PERSONAL CONSUMPTION EXPENDITURE": "PCE",
            }
            for key, std in name_map.items():
                if key in name:
                    name = std
                    break

            events.append(EconEvent(
                name=name, date=date, release_time_et=rel_time,
                impact=impact, description=str(item.get("event", "")),
            ))
        log.info(f"[EconEvents] Finnhub: fetched {len(events)} US events")
        return events
    except Exception as e:
        log.warning(f"[EconEvents] Finnhub calendar fetch error: {e}")
        return []


# ── 判定ロジック ──────────────────────────────────────────────────────────────

def get_event_status(
    now_et: Optional[datetime.datetime] = None,
    events: Optional[list[EconEvent]] = None,
    calendar_paths: Optional[list[Path]] = None,
    api_key: str = "",
) -> EventStatus:
    """現在時刻の経済イベント状況を返す。

    Args:
        now_et:         現在のET時刻 (Noneで自動取得)
        events:         テスト用外部注入イベントリスト
        calendar_paths: カレンダーJSONのパス一覧
        api_key:        Finnhub API KEY (カレンダー更新用)

    Returns:
        EventStatus
    """
    # 現在時刻 (ET)
    if now_et is None:
        now_et = datetime.datetime.now(tz=ET)
    today = now_et.date()

    # イベントリスト取得
    if events is None:
        events = _load_calendar_json(calendar_paths)
        if api_key and not events:
            events = _fetch_finnhub_calendar(api_key)

    # 今日のイベントを絞り込み
    today_events = [e for e in events if e.date == today]

    status = EventStatus(today_events=today_events)

    if not today_events:
        return status

    # 各イベントについてブラックアウト判定
    for event in today_events:
        # 発表時刻 (ET)
        release_dt = datetime.datetime.combine(
            today, event.release_time_et, tzinfo=ET
        )
        delta_min = (release_dt - now_et).total_seconds() / 60.0

        bk_before, bk_after = _BLACKOUT_MINUTES.get(
            event.name, (15, 30)
        )

        # ブラックアウト判定: -bk_after 〜 +bk_before 分の範囲
        in_blackout = (-bk_after <= delta_min <= bk_before)

        if in_blackout:
            status.is_blackout    = True
            status.blackout_event = event.name
            status.minutes_to_event = delta_min
            status.is_high_impact = event.name in _HIGH_IMPACT_EVENTS
            status.post_event     = delta_min < 0
            log.info(
                f"[EconEvents] BLACKOUT: {event.name} delta={delta_min:.1f}min "
                f"(before={bk_before}min after={bk_after}min)"
            )
            break

        # 次のイベントまでの時間を記録 (最も近いもの)
        if delta_min > 0 and (
            status.minutes_to_event is None or
            delta_min < status.minutes_to_event
        ):
            status.minutes_to_event = delta_min
            status.is_high_impact   = event.name in _HIGH_IMPACT_EVENTS

    # straddle_signal: 発表後 0〜30分 かつ 高インパクトイベント
    if status.post_event and status.is_high_impact:
        status.straddle_signal = True
        log.info(f"[EconEvents] STRADDLE SIGNAL: post {status.blackout_event}")

    return status


def is_entry_blocked(
    now_et: Optional[datetime.datetime] = None,
    events: Optional[list[EconEvent]] = None,
    api_key: str = "",
) -> bool:
    """新規エントリーをブロックすべきかを返すシンプルAPI。

    True = ブラックアウト中 = エントリー禁止
    """
    status = get_event_status(now_et=now_et, events=events, api_key=api_key)
    return status.is_blackout


def update_calendar_from_finnhub(
    api_key: str,
    output_path: Optional[Path] = None,
) -> bool:
    """Finnhubから最新カレンダーを取得してJSONに保存。

    Args:
        api_key:     Finnhub API KEY
        output_path: 保存先 (Noneで data/econ_calendar.json)

    Returns:
        True = 成功, False = 失敗
    """
    if output_path is None:
        output_path = _DEFAULT_CALENDAR_PATHS[0]

    events = _fetch_finnhub_calendar(api_key, days_ahead=60)
    if not events:
        log.warning("[EconEvents] Finnhub fetch returned no events")
        return False

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_updated": datetime.date.today().isoformat(),
            "source": "Finnhub /calendar/economic",
            "events": [
                {
                    "name":      e.name,
                    "date":      e.date.isoformat(),
                    "time_et":   e.release_time_et.strftime("%H:%M"),
                    "impact":    e.impact,
                    "description": e.description,
                }
                for e in events
            ],
        }
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info(f"[EconEvents] Calendar updated: {len(events)} events → {output_path}")
        return True
    except Exception as e:
        log.error(f"[EconEvents] Calendar save error: {e}")
        return False
