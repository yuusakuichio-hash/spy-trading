"""common/market_calendar.py — 市場別セッション判定（一元管理）

役割:
  is_in_market_hours(market, now) で各市場の取引可能時間帯を判定する。
  chronos_watchdog / atlas_watchdog / chronos_agent などから参照することで
  時間帯ゲートのロジックを一元管理し、コピペ誤りを防ぐ。

対応市場:
  "spx_options" — SPX 0DTE オプション (Atlas)
      CBOE 通常取引時間: 09:30-16:00 ET
      プレマーケット guard: 22:20-05:10 JST (夏時間 EDT+13h)
      ※ Atlas は市場前後の分析窓も含む広めの帯を定義している

  "cme_futures" — CME E-mini 先物 (Chronos / MES / MNQ 等)
      Globex 時間: 日曜 18:00 ET〜金曜 17:00 ET (= 月曜 07:00 JST 〜 土曜 06:00 JST)
      デイリー休止: 毎日 17:00-18:00 ET (= 翌日 06:00-07:00 JST)

夏時間 (EDT) / 冬時間 (EST) メモ:
  夏時間: EDT = ET + 13h (JST)  — 3月第2日曜〜11月第1日曜
  冬時間: EST = ET + 14h (JST)  — 11月第1日曜〜3月第2日曜
  2026年: 夏時間開始 3/8 (日)、冬時間開始 11/1 (日)

  現在の実装は夏時間 (EDT) を優先でハードコード。
  冬時間期間(11/1〜3/8)はデイリー休止が 07:00-08:00 JST になるため
  CME_DAILY_BREAK_START / END を更新すること。

  TODO(DST切替): 2026/11/1 に冬時間対応を実施。
    - CME_DAILY_BREAK_START = (7, 0)  → (8, 0)
    - CME_DAILY_BREAK_END   = (7, 0)  → (8, 0)  [同一日に end も変わる]
    - CME_OPEN_WEEKDAY_HOUR = 7       → 8
    - CME_CLOSE_SAT_HOUR    = 6       → 7
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Literal

# ── タイムゾーン定数 ──────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))

# ── CME先物 セッション定数 (夏時間・JST) ─────────────────────────────────────
# 週開場: 月曜 07:00 JST 以降
# 週閉場: 土曜 06:00 JST 以降（土曜6時〜月曜7時前はクローズ）
# デイリー休止: 毎日 06:00-07:00 JST
CME_OPEN_WEEKDAY_HOUR_JST  = 7   # 月曜7時から開場
CME_CLOSE_SAT_HOUR_JST     = 6   # 土曜6時から閉場
CME_DAILY_BREAK_START_JST  = (6, 0)   # 毎日 06:00 JST 休止開始
CME_DAILY_BREAK_END_JST    = (7, 0)   # 毎日 07:00 JST 休止終了

# ── SPXオプション セッション定数 (夏時間・JST) ────────────────────────────────
# Atlas の market_window: 22:20〜05:10 JST (日跨ぎ)
SPX_WINDOW_START_JST = (22, 20)
SPX_WINDOW_END_JST   = (5,  10)


def is_in_market_hours(
    market: Literal["spx_options", "cme_futures"],
    now: datetime,
) -> bool:
    """指定された市場の取引可能時間帯かどうかを判定する。

    Args:
        market: "spx_options" または "cme_futures"
        now:    判定基準日時（tzinfo 付き推奨。naive の場合は JST とみなす）

    Returns:
        True = 取引可能時間帯内
        False = 閉場 / 休止中
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=JST)

    now_jst = now.astimezone(JST)

    if market == "cme_futures":
        return _is_cme_futures_open(now_jst)
    elif market == "spx_options":
        return _is_spx_options_open(now_jst)
    else:
        raise ValueError(f"未知の market: {market!r}. 'spx_options' か 'cme_futures' を指定してください")


def _is_cme_futures_open(now_jst: datetime) -> bool:
    """CME E-mini 先物 (MES/MNQ 等) Globex の取引可能時間を判定する。

    セッション: 月曜 07:00 JST 〜 土曜 06:00 JST (24時間連続)
    デイリー休止: 毎日 06:00-07:00 JST (1時間)

    weekday(): 0=月, 1=火, 2=水, 3=木, 4=金, 5=土, 6=日

    閉場ケース:
      1. 土曜 06:00 JST 以降〜月曜 07:00 JST 未満 (週末クローズ)
         - 土曜 6時以降
         - 日曜 全日
         - 月曜 7時前
      2. 毎日 06:00-07:00 JST (デイリー休止)
    """
    weekday = now_jst.weekday()  # 0=月, 6=日
    h = now_jst.hour
    m = now_jst.minute
    hm = (h, m)

    # 週末クローズ判定
    if weekday == 5 and hm >= (CME_CLOSE_SAT_HOUR_JST, 0):
        # 土曜 06:00 JST 以降 → クローズ
        return False
    if weekday == 6:
        # 日曜 全日 → クローズ
        return False
    if weekday == 0 and hm < (CME_OPEN_WEEKDAY_HOUR_JST, 0):
        # 月曜 07:00 JST 前 → クローズ
        return False

    # デイリー休止判定 (毎日 06:00-07:00 JST)
    if CME_DAILY_BREAK_START_JST <= hm < CME_DAILY_BREAK_END_JST:
        return False

    return True


def _is_spx_options_open(now_jst: datetime) -> bool:
    """SPX 0DTE オプション (Atlas) の市場監視時間帯を判定する。

    Atlas の market_window: 22:20〜05:10 JST (日跨ぎ)
    平日のみ (月〜金) で判定する。土日は閉場。
    """
    weekday = now_jst.weekday()  # 0=月, 6=日
    h = now_jst.hour
    m = now_jst.minute
    hm = (h, m)

    # 土日はクローズ
    if weekday in (5, 6):
        return False

    # 日跨ぎ窓: 22:20 以降 または 05:10 以前
    start = SPX_WINDOW_START_JST  # (22, 20)
    end   = SPX_WINDOW_END_JST    # ( 5, 10)

    # 日跨ぎ窓 (start > end) の判定
    if hm >= start or hm <= end:
        return True

    return False


def cme_futures_session_label(now: datetime) -> str:
    """CME先物の現在セッション名を返す（デバッグ・ログ用）。

    Returns:
        "OPEN" | "DAILY_BREAK" | "WEEKEND_CLOSE"
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    now_jst = now.astimezone(JST)

    weekday = now_jst.weekday()
    h = now_jst.hour
    m = now_jst.minute
    hm = (h, m)

    if weekday == 5 and hm >= (CME_CLOSE_SAT_HOUR_JST, 0):
        return "WEEKEND_CLOSE"
    if weekday == 6:
        return "WEEKEND_CLOSE"
    if weekday == 0 and hm < (CME_OPEN_WEEKDAY_HOUR_JST, 0):
        return "WEEKEND_CLOSE"

    if CME_DAILY_BREAK_START_JST <= hm < CME_DAILY_BREAK_END_JST:
        return "DAILY_BREAK"

    return "OPEN"
