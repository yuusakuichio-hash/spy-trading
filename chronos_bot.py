#!/usr/bin/env python3
"""
chronos_bot.py — MyFundedFutures (MFFU) 先物自動売買Bot v1

ブローカー : Tradovate (demo/live)
対象       : MES (Micro E-mini S&P 500) / ES (E-mini S&P 500)
口座       : MFFU $50K Core 評価口座

設計方針:
  apex_bot.pyベースに以下を変更:
    - ApexRuleGuard → MFFURuleGuard
      (Daily Loss / Trailing DD → EOD Drawdown ベース)
      (Intraday DD制限なし)
      (Consistency 40%ライン)
    - NewsTradingFilter 強化
      (FOMC/CPI/NFP カレンダー連動・前後2分自動停止)
    - MFFUScalingPlan (MFFUの公式Scale Plan準拠)

  Atlas基盤実流用 (実質約30%):
    portfolio_risk.can_take_risk() — 流用済み
    spy_bot.calc_kelly_fraction() — 流用済み（strategy_filter対応済み）
    FuturesORBStrategy — apex_bot.pyより流用・rule_guardのみ差し替え
    strategy_selector.select_strategy() — 未使用（SPY/SPXオプション向けのため除外）
    spy_bot.premarket_assessment() — 未使用（先物には不要・シグネチャ非互換）
    spy_bot.IntradayMonitor — 未使用（先物には不要・シグネチャ非互換）
    chronos_strategy_selector.select_futures_strategy() — 先物専用セレクター（新規）

動作モード:
  --paper   : Tradovate demoアカウント（デフォルト）
  --live    : Tradovate liveアカウント（本番）
  --dry-run : API接続なし・全ロジックをテスト
"""

from __future__ import annotations

import os
import sys
import json
import math
import time
import uuid
import logging
import datetime
import argparse
import zoneinfo
from pathlib import Path
from typing import Optional

# ── .env ロード ────────────────────────────────────────────────────────────────
def _load_env_file():
    for candidate in [Path("/root/spxbot/.env"), Path(__file__).parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
            break

_load_env_file()

# ── パス定数 ───────────────────────────────────────────────────────────────────
_BASE_DIR = Path(os.environ.get("MFFU_DATA_DIR", Path(__file__).parent / "data"))
LOG_DIR   = Path(os.environ.get("MFFU_LOG_DIR", _BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "mffu_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mffu_bot")

# ── タイムゾーン ───────────────────────────────────────────────────────────────
ET  = zoneinfo.ZoneInfo("America/New_York")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# ── Atlas基盤モジュール import ─────────────────────────────────────────────────
try:
    from strategy_selector import select_strategy, compute_vix_percentile
    STRATEGY_SELECTOR_AVAILABLE = True
    log.info("strategy_selector: loaded")
except ImportError as e:
    STRATEGY_SELECTOR_AVAILABLE = False
    log.warning(f"strategy_selector not available: {e}")

try:
    from portfolio_risk import (
        can_take_risk, update_positions, clear_positions,
        check_weekly_dd, check_monthly_dd, record_daily_pnl,
        load_positions,
    )
    PORTFOLIO_RISK_AVAILABLE = True
    log.info("portfolio_risk: loaded")
except ImportError as e:
    PORTFOLIO_RISK_AVAILABLE = False
    log.warning(f"portfolio_risk not available: {e}")

try:
    from spy_bot import calc_kelly_fraction
    KELLY_AVAILABLE = True
    log.info("spy_bot.calc_kelly_fraction: loaded")
except ImportError as e:
    KELLY_AVAILABLE = False
    log.warning(f"spy_bot.calc_kelly_fraction not available: {e}")

# ── MFFU固有モジュール ─────────────────────────────────────────────────────────
from tradovate_client import TradovateClient, _get_front_month_symbol, CONTRACT_SPECS
from chronos_rule_simulator import (
    MFFU_ACCOUNT_RULES,
    check_eod_drawdown,
    check_consistency_rule,
    check_profit_target,
    get_allowed_contracts,
    MFFU_HIGH_IMPACT_EVENTS,
    NEWS_EVENT_BLACKOUT_MINUTES,
)

# ── マルチ戦術モジュール ────────────────────────────────────────────────────────
try:
    from futures_vix_mr import VIXMRStrategy, calc_vix_z_score
    VIX_MR_AVAILABLE = True
    log.info("futures_vix_mr: loaded")
except ImportError as e:
    VIX_MR_AVAILABLE = False
    log.warning(f"futures_vix_mr not available: {e}")

try:
    from futures_trend_follow import TrendFollowStrategy
    TREND_FOLLOW_AVAILABLE = True
    log.info("futures_trend_follow: loaded")
except ImportError as e:
    TREND_FOLLOW_AVAILABLE = False
    log.warning(f"futures_trend_follow not available: {e}")

try:
    from futures_level_trading import LevelTradingStrategy
    LEVEL_TRADING_AVAILABLE = True
    log.info("futures_level_trading: loaded")
except ImportError as e:
    LEVEL_TRADING_AVAILABLE = False
    log.warning(f"futures_level_trading not available: {e}")

try:
    from chronos_strategy_selector import (
        select_futures_strategy,
        build_env_dict,
        check_consistency_safety,
    )
    MFFU_SELECTOR_AVAILABLE = True
    log.info("chronos_strategy_selector: loaded")
except ImportError as e:
    MFFU_SELECTOR_AVAILABLE = False
    log.warning(f"chronos_strategy_selector not available: {e}")

try:
    from futures_session_strategy import get_current_session
    SESSION_STRATEGY_AVAILABLE = True
    log.info("futures_session_strategy: loaded")
except ImportError as e:
    SESSION_STRATEGY_AVAILABLE = False
    log.warning(f"futures_session_strategy not available: {e}")

# ── P0新規戦術モジュール ────────────────────────────────────────────────────────
try:
    from futures_time_of_day_bias import (
        calc_tod_bias,
        apply_tod_bias_to_size_pct,
        get_tod_slot_info,
    )
    TOD_BIAS_AVAILABLE = True
    log.info("futures_time_of_day_bias: loaded")
except ImportError as e:
    TOD_BIAS_AVAILABLE = False
    log.warning(f"futures_time_of_day_bias not available: {e}")

try:
    from futures_asia_range_fade import (
        AsiaRangeFadeStrategy,
        is_asia_session,
    )
    ASIA_RANGE_AVAILABLE = True
    log.info("futures_asia_range_fade: loaded")
except ImportError as e:
    ASIA_RANGE_AVAILABLE = False
    log.warning(f"futures_asia_range_fade not available: {e}")

try:
    from futures_gap_fill_advanced import (
        GapFillAdvancedStrategy,
        load_economic_calendar,
    )
    GAP_FILL_ADVANCED_AVAILABLE = True
    log.info("futures_gap_fill_advanced: loaded")
except ImportError as e:
    GAP_FILL_ADVANCED_AVAILABLE = False
    log.warning(f"futures_gap_fill_advanced not available: {e}")

# ── P2新規戦術モジュール（Volume Profile / Economic Event / Range Break 改良版）──
try:
    from futures_volume_profile import calc_volume_profile, VolumeProfileStrategy
    VOLUME_PROFILE_AVAILABLE = True
    log.info("futures_volume_profile: loaded")
except ImportError as e:
    VOLUME_PROFILE_AVAILABLE = False
    log.warning(f"futures_volume_profile not available: {e}")

try:
    from futures_economic_event import EconomicEventStrategy as _EconEventStrategy
    ECONOMIC_EVENT_AVAILABLE = True
    log.info("futures_economic_event: loaded")
except ImportError as e:
    ECONOMIC_EVENT_AVAILABLE = False
    log.warning(f"futures_economic_event not available: {e}")

try:
    from futures_range_break_improved import (
        RangeBreakImprovedStrategy,
        calc_donchian_channel,
        calc_dynamic_donchian_period,
    )
    RANGE_BREAK_IMPROVED_AVAILABLE = True
    log.info("futures_range_break_improved: loaded")
except ImportError as e:
    RANGE_BREAK_IMPROVED_AVAILABLE = False
    log.warning(f"futures_range_break_improved not available: {e}")

# ── P1新規戦術モジュール（VIX Term Structure / ES-NQ Spread）─────────────────────
try:
    from futures_vix_term_structure import (
        VIXTermStructureStrategy,
        fetch_vix_term_structure_data,
    )
    VIX_TERM_STRUCTURE_AVAILABLE = True
    log.info("futures_vix_term_structure: loaded")
except ImportError as e:
    VIX_TERM_STRUCTURE_AVAILABLE = False
    log.warning(f"futures_vix_term_structure not available: {e}")

try:
    from futures_es_nq_spread import (
        ESNQSpreadStrategy,
        fetch_es_nq_prices,
    )
    ES_NQ_SPREAD_AVAILABLE = True
    log.info("futures_es_nq_spread: loaded")
except ImportError as e:
    ES_NQ_SPREAD_AVAILABLE = False
    log.warning(f"futures_es_nq_spread not available: {e}")

# ── 認証情報 ───────────────────────────────────────────────────────────────────
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")

# ── デフォルトパラメータ ───────────────────────────────────────────────────────
DEFAULT_ACCOUNT_SIZE = 50_000
DEFAULT_PRODUCT      = "MES"

# ORB設定（先物用）
ORB_OPENING_PERIOD_MINUTES = 30    # 9:30〜10:00 ET をオープニングレンジとする
ORB_ENTRY_WINDOW_MINUTES   = 120   # 10:00〜12:00 ET をエントリーウィンドウとする
ORB_STOP_ATR_MULT          = 1.0   # ストップ = ORレンジ × 1.0倍
ORB_TARGET_ATR_MULT        = 2.0   # 利確 = ORレンジ × 2.0倍（RR=1:2）

# 日次ループ間隔
MAIN_LOOP_SLEEP_SECS = 60

# ── Daily Strong Close Rule（設計書 C-4 + タスク要件）──────────────────────────
# 日内利益 +5% を超えたら残り時間ノートレード（Consistency保護）
DAILY_PROFIT_CAP_PCT    = 0.05   # +5%
# 日内損失 -2% を超えたら即全ポジクローズ
DAILY_LOSS_HALT_PCT     = 0.02   # -2%
# 週次DD制限: -3%超で翌週まで停止
WEEKLY_DD_HALT_PCT      = 0.03   # -3%


# ── Pushover通知 ──────────────────────────────────────────────────────────────

def pushover(title: str, message: str, priority: int = 0) -> bool:
    """Pushover通知を送信する。"""
    import requests as _requests
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log.warning("pushover: token/user not set")
        return False
    try:
        resp = _requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":    PUSHOVER_TOKEN,
                "user":     PUSHOVER_USER,
                "title":    title,
                "message":  message,
                "priority": priority,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        log.warning(f"pushover HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.warning(f"pushover: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# VIX / 市場データ取得
# ─────────────────────────────────────────────────────────────────────────────

def get_vix() -> Optional[float]:
    """現在のVIXをyahoo financeから取得する。"""
    import requests as _requests
    try:
        resp = _requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if result:
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]
            if closes:
                return round(closes[-1], 2)
    except Exception as e:
        log.warning(f"get_vix: {e}")
    return None


def get_vix_history(days: int = 60) -> list[float]:
    """VIX日次終値を取得する（直近N日）。"""
    import requests as _requests
    try:
        resp = _requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": f"{days}d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if result:
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            return [c for c in closes if c is not None]
    except Exception as e:
        log.warning(f"get_vix_history: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# NewsTradingFilter — FOMC/CPI/NFP 前後2分の取引停止
# ─────────────────────────────────────────────────────────────────────────────

class NewsTradingFilter:
    """
    MFFUのニュース取引制限フィルター。

    MFFU禁止: FOMC / CPI / NFP イベント前後2分（Apexの5分より短い）

    設計:
      - econ_calendarファイル(JSON)からイベント時刻をロード
      - is_blackout() で現在時刻がブラックアウト期間内かチェック
      - カレンダーがない場合はデフォルトのweekly/monthly パターンにfallback

    econ_calendar.json フォーマット:
      [
        {"event": "CPI",  "datetime_et": "2026-04-10T08:30:00"},
        {"event": "FOMC", "datetime_et": "2026-04-29T14:00:00"},
        ...
      ]
    """

    BLACKOUT_MINUTES = NEWS_EVENT_BLACKOUT_MINUTES  # 2分

    def __init__(self, calendar_path: Optional[Path] = None):
        self.calendar_path = calendar_path or (_BASE_DIR / "econ_calendar.json")
        self._events: list[dict] = []
        self._load_calendar()

    def _load_calendar(self):
        """カレンダーファイルをロードする。"""
        if self.calendar_path.exists():
            try:
                raw = json.loads(self.calendar_path.read_text())
                self._events = [
                    e for e in raw
                    if e.get("event", "").upper() in MFFU_HIGH_IMPACT_EVENTS
                ]
                log.info(f"[NewsTradingFilter] loaded {len(self._events)} events "
                         f"from {self.calendar_path}")
            except Exception as e:
                log.warning(f"[NewsTradingFilter] calendar load error: {e}")
                self._events = []
        else:
            log.info(f"[NewsTradingFilter] no calendar file found at {self.calendar_path}")
            self._events = []

    def reload(self):
        """カレンダーを再ロードする（毎朝呼び出す）。"""
        self._load_calendar()

    def is_blackout(self, now_et: Optional[datetime.datetime] = None) -> dict:
        """
        現在時刻がブラックアウト期間内かチェックする。

        Returns:
            {
              "blocked":    bool,
              "event":      str|None,   — 対象イベント名
              "event_time": str|None,   — イベント時刻
              "minutes_to": float|None, — イベントまでの分数（負=経過後）
            }
        """
        if now_et is None:
            now_et = datetime.datetime.now(ET)

        blackout_delta = datetime.timedelta(minutes=self.BLACKOUT_MINUTES)

        for event in self._events:
            event_name = event.get("event", "").upper()
            dt_str     = event.get("datetime_et", "")

            try:
                # B-4修正: offset-aware 文字列の場合 .replace(tzinfo=ET) は4時間ズレを起こす。
                # naive な場合のみ .replace(tzinfo=ET) し、aware な場合は .astimezone(ET) を使う。
                parsed = datetime.datetime.fromisoformat(dt_str)
                if parsed.tzinfo is None:
                    event_dt = parsed.replace(tzinfo=ET)
                else:
                    event_dt = parsed.astimezone(ET)
            except Exception:
                continue

            diff = now_et - event_dt   # 正 = イベント後
            abs_diff = abs(diff)

            if abs_diff <= blackout_delta:
                minutes_to = -diff.total_seconds() / 60  # 正=前、負=後
                log.warning(
                    f"[NewsTradingFilter] BLACKOUT: event={event_name} "
                    f"event_time={event_dt.isoformat()} "
                    f"minutes_to={minutes_to:+.1f}"
                )
                return {
                    "blocked":    True,
                    "event":      event_name,
                    "event_time": dt_str,
                    "minutes_to": minutes_to,
                }

        return {
            "blocked":    False,
            "event":      None,
            "event_time": None,
            "minutes_to": None,
        }

    def next_event(self, now_et: Optional[datetime.datetime] = None) -> Optional[dict]:
        """次の高影響イベントを返す（今後24時間以内）。"""
        if now_et is None:
            now_et = datetime.datetime.now(ET)

        upcoming = []
        for event in self._events:
            dt_str = event.get("datetime_et", "")
            try:
                # B-4修正: offset-aware 文字列対応
                parsed = datetime.datetime.fromisoformat(dt_str)
                if parsed.tzinfo is None:
                    event_dt = parsed.replace(tzinfo=ET)
                else:
                    event_dt = parsed.astimezone(ET)
            except Exception:
                continue

            diff_seconds = (event_dt - now_et).total_seconds()
            if 0 < diff_seconds <= 86400:  # 今後24時間以内
                upcoming.append({
                    "event":      event.get("event"),
                    "event_time": dt_str,
                    "minutes_to": diff_seconds / 60,
                })

        if not upcoming:
            return None

        # 最近のイベントを返す
        upcoming.sort(key=lambda x: x["minutes_to"])
        return upcoming[0]


# ─────────────────────────────────────────────────────────────────────────────
# MFFURuleGuard — MFFU全ルール遵守層
# ─────────────────────────────────────────────────────────────────────────────

class MFFURuleGuard:
    """
    MFFUのルールを監視し、違反前に自動停止する保護層。

    Apex版との主な違い:
      - Daily Loss (Trailing DD) → EOD Drawdown ベース
        (日中含み損は監視対象外。EODのみ確認)
      - Intraday DD制限なし → 日中ルール違反はEOD時のみ評価
      - Consistency 40%ライン（Apex 30%より緩い）
      - 緊急停止のトリガーはEOD DDのみ

    設計思想:
      - MFFUはIntraday DDがないため、日中の大きな含み損でも
        クローズ前に強制停止されない
      - ただし"合理的な日中モニタリング"として
        DAILY_LOSS_WARN = 実効残高(含み損込み)が threshold の80%以内になったら警告
      - EOD確定時のみルール違反判定
    """

    # EOD Drawdown基準の警告・停止しきい値
    # MFFUはIntraday DDがないが、日中の含み損が大きくなりすぎた場合の
    # 予防的モニタリングとして使用する
    INTRADAY_WARN_PCT  = 0.75   # 75%消費で警告（日中含み損ベース）
    INTRADAY_STOP_PCT  = 0.90   # 90%消費で予防的停止（任意・保守的設定）
    EOD_WARN_PCT       = 0.70   # EOD確定後 70%消費で警告（翌日エントリー制限）

    def __init__(self, account_size: int):
        self.account_size    = account_size
        self.rules           = MFFU_ACCOUNT_RULES[account_size]
        self.initial_balance = float(account_size)

        # 状態
        self.day_start_balance: float = self.initial_balance
        self.eod_balance:       float = self.initial_balance   # 前日EOD確定残高
        self.daily_pnls:        list  = []
        self.today_pnl:         float = 0.0
        self.trading_days:      int   = 0

        # フラグ
        self._eod_dd_halted:         bool = False
        self._intraday_warned:       bool = False
        self._intraday_stop_applied: bool = False

    def reset_day(self, eod_balance: float):
        """
        日次リセット（毎朝呼び出す）。
        前日EOD残高を確定させてから翌日の基準にする。
        """
        if self.today_pnl != 0:
            self.daily_pnls.append(self.today_pnl)
            self.trading_days += 1

        self.eod_balance       = eod_balance
        self.day_start_balance = eod_balance
        self.today_pnl         = 0.0
        self._eod_dd_halted    = False
        self._intraday_warned  = False
        self._intraday_stop_applied = False

        log.info(f"[MFFURuleGuard] day reset: eod_balance=${eod_balance:,.0f} "
                 f"initial=${self.initial_balance:,.0f} "
                 f"trading_days={self.trading_days}")

    def update_pnl(self, realized_pnl: float):
        """確定P&Lを記録する。"""
        self.today_pnl += realized_pnl
        log.debug(f"[MFFURuleGuard] today_pnl updated: ${self.today_pnl:+,.2f}")

    def check_intraday(
        self,
        current_balance: float,
        open_pnl:        float = 0.0,
    ) -> dict:
        """
        日中モニタリング（MFFUはIntraday DD制限なしだが予防的チェック）。

        MFFUのルール: 日中含み損はDrawdown判定に影響しない。
        ここでは「もし今クローズしたらEOD残高はいくらか」で
        EOD DD上限との距離を計算し、警告のみ行う。

        Returns:
            {
              "safe":             bool,
              "action":           str,   — "ok" | "warn" | "preventive_halt"
              "hypothetical_eod": float, — 仮にクローズした場合のEOD残高
              "eod_dd":           dict,
              "consistency":      dict,
              "reasons":          list,
            }
        """
        # 仮EOD残高（現在の含み損込み実効残高）
        hypothetical_eod = current_balance + open_pnl
        today_pnl_with_open = hypothetical_eod - self.day_start_balance

        # EOD DD チェック（仮残高で計算）
        eod_dd = check_eod_drawdown(
            self.rules, self.initial_balance, hypothetical_eod
        )
        cr = check_consistency_rule(
            self.rules, self.daily_pnls, today_pnl_with_open
        )

        action  = "ok"
        reasons = []

        # 仮EOD残高でDD上限の何%消費しているか
        eod_used_pct = 1.0 - (eod_dd["remaining"] / self.rules.eod_drawdown) if self.rules.eod_drawdown > 0 else 0.0

        if not eod_dd["passed"]:
            # MFFUはIntraday DDがないため即強制停止ではない
            # ただし、このまま保持するとEOD違反確定なので緊急クローズ推奨
            action = "preventive_halt"
            reasons.append(
                f"INTRADAY_EOD_DD_BREACH_HYPOTHETICAL: "
                f"hypothetical_eod=${hypothetical_eod:.0f} < "
                f"threshold=${eod_dd['threshold']:.0f} "
                f"[MFFU: no intraday rule, but EOD violation if held]"
            )
            self._intraday_stop_applied = True
        elif eod_used_pct >= self.INTRADAY_STOP_PCT and not self._intraday_stop_applied:
            action = "preventive_halt"
            reasons.append(
                f"INTRADAY_PREVENTIVE_HALT_{self.INTRADAY_STOP_PCT*100:.0f}PCT: "
                f"remaining=${eod_dd['remaining']:.0f} "
                f"[preventive only - MFFU has no intraday rule]"
            )
            self._intraday_stop_applied = True
        elif eod_used_pct >= self.INTRADAY_WARN_PCT and not self._intraday_warned:
            action = "warn"
            reasons.append(
                f"INTRADAY_EOD_WARN_{self.INTRADAY_WARN_PCT*100:.0f}PCT: "
                f"remaining=${eod_dd['remaining']:.0f}"
            )
            self._intraday_warned = True

        if not cr["passed"] and cr.get("violation_amount", 0) > 0:
            if action == "ok":
                action = "warn"
            reasons.append(
                f"CONSISTENCY_WARN: today_pnl=${today_pnl_with_open:.0f} > "
                f"max_allowed=${cr['max_allowed']:.0f} "
                f"(limit {self.rules.consistency_limit*100:.0f}%)"
            )

        is_safe = action in ("ok", "warn")

        return {
            "safe":             is_safe,
            "action":           action,
            "hypothetical_eod": hypothetical_eod,
            "eod_dd":           eod_dd,
            "consistency":      cr,
            "reasons":          reasons,
            "violations":       reasons,
        }

    def check_eod(self, eod_balance: float) -> dict:
        """
        EOD確定チェック（全ポジションクローズ後に呼び出す）。
        MFFUの実際のルール判定はここで行う。

        Returns:
            {
              "passed":  bool,
              "eod_dd":  dict,
              "reasons": list,
            }
        """
        today_pnl = eod_balance - self.day_start_balance
        eod_dd    = check_eod_drawdown(self.rules, self.initial_balance, eod_balance)
        cr        = check_consistency_rule(self.rules, self.daily_pnls, today_pnl)
        pt        = check_profit_target(self.rules, self.initial_balance, eod_balance)

        reasons = []
        if not eod_dd["passed"]:
            reasons.append(
                f"EOD_DD_VIOLATED: balance=${eod_balance:.0f} < "
                f"threshold=${eod_dd['threshold']:.0f} "
                f"(drawdown=${eod_dd['drawdown']:.0f} > limit=${eod_dd['limit']:.0f})"
            )
            self._eod_dd_halted = True

        if not cr["passed"] and cr.get("violation_amount", 0) > 0:
            reasons.append(
                f"CONSISTENCY_VIOLATED: today_pnl=${today_pnl:.0f} > "
                f"max_allowed=${cr['max_allowed']:.0f}"
            )

        return {
            "passed":       len([r for r in reasons if "EOD_DD_VIOLATED" in r]) == 0,
            "eod_dd":       eod_dd,
            "consistency":  cr,
            "profit_target": pt,
            "reasons":      reasons,
            "today_pnl":    today_pnl,
        }

    def can_enter_new_position(
        self,
        current_balance: float,
        open_pnl: float = 0.0,
    ) -> bool:
        """新規エントリーが許可されるかチェック。"""
        if self._eod_dd_halted:
            log.warning("[MFFURuleGuard] new entry BLOCKED: EOD DD halted from previous EOD")
            return False

        result = self.check_intraday(current_balance, open_pnl)
        if result["action"] == "preventive_halt":
            log.warning(f"[MFFURuleGuard] new entry BLOCKED: {result['reasons']}")
            return False

        return True

    def get_allowed_contracts(self, current_profit: float) -> int:
        """現在の利益に応じた許容コントラクト数を返す。"""
        return get_allowed_contracts(self.account_size, current_profit)

    def status_summary(self, current_balance: float, open_pnl: float = 0.0) -> str:
        """現在のルール状況をサマリー文字列で返す。"""
        hypothetical_eod = current_balance + open_pnl
        eod_dd = check_eod_drawdown(self.rules, self.initial_balance, hypothetical_eod)
        pt     = check_profit_target(self.rules, self.initial_balance, current_balance)

        return (
            f"Balance=${current_balance:,.0f} "
            f"EOD_DD_remaining=${eod_dd['remaining']:.0f}({eod_dd['margin_pct']:.0f}%) "
            f"Profit=${current_balance - self.initial_balance:+,.0f}/"
            f"${pt['target']:,.0f}({pt['progress_pct']:.0f}%) "
            f"TradingDays={self.trading_days}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# FuturesORBStrategy — 先物ORBエントリーロジック（MFFUルールガード版）
# ─────────────────────────────────────────────────────────────────────────────

class FuturesORBStrategy:
    """
    先物 Opening Range Breakout 戦略。
    apex_bot.pyのFuturesORBStrategyから流用し、
    rule_guardをMFFURuleGuardに差し替えたもの。

    タイムライン（ET）:
      9:30  — 市場オープン・ORレンジ計測開始
      10:00 — ORレンジ確定
      10:00〜12:00 — ブレイクアウト監視・エントリーウィンドウ
      15:45 — 強制クローズ（日次EOD確定前）
    """

    def __init__(
        self,
        client:       Optional[TradovateClient],
        rule_guard:   MFFURuleGuard,
        news_filter:  NewsTradingFilter,
        product:      str = "MES",
        account_size: int = 50_000,
    ):
        self.client       = client
        self.rule_guard   = rule_guard
        self.news_filter  = news_filter
        self.product      = product
        self.account_size = account_size

        # OR状態
        self._or_high:    Optional[float] = None
        self._or_low:     Optional[float] = None
        self._or_complete = False

        # エントリー状態
        self._entry_done:    bool = False
        self._current_order: Optional[dict] = None
        self._stop_price:    Optional[float] = None
        self._target_price:  Optional[float] = None
        self._entry_side:    Optional[str]   = None
        self._entry_price:   Optional[float] = None
        self._trade_id:      Optional[str]   = None

    def reset_day(self):
        """日次リセット。"""
        self._or_high      = None
        self._or_low       = None
        self._or_complete  = False
        self._entry_done   = False
        self._current_order = None
        self._stop_price   = None
        self._target_price = None
        self._entry_side   = None
        self._entry_price  = None
        self._trade_id     = None
        log.info("[FuturesORB] day reset")

    def update_or_candle(self, high: float, low: float):
        """ORレンジのローソク足を更新する（9:30〜10:00）。"""
        if self._or_high is None or high > self._or_high:
            self._or_high = high
        if self._or_low is None or low < self._or_low:
            self._or_low = low

    def finalize_or(self):
        """10:00 ETにORレンジを確定する。"""
        if self._or_high is not None and self._or_low is not None:
            self._or_complete = True
            or_range = self._or_high - self._or_low
            log.info(f"[FuturesORB] OR finalized: high={self._or_high:.2f} "
                     f"low={self._or_low:.2f} range={or_range:.2f}")

    @property
    def or_range(self) -> Optional[float]:
        if self._or_high is not None and self._or_low is not None:
            return self._or_high - self._or_low
        return None

    def check_breakout(
        self,
        current_price:   float,
        current_balance: float,
        vix:             float,
        env_score:       float,
        open_pnl:        float = 0.0,
        now_et:          Optional[datetime.datetime] = None,
    ) -> Optional[dict]:
        """
        ブレイクアウト判定とエントリー実行。
        ニュースフィルターチェックを追加（Apex版との差分）。
        """
        if not self._or_complete or self._entry_done:
            return None
        if self._or_high is None or self._or_low is None:
            return None

        # MFFUニュースブラックアウトチェック（Apex版との差分: 2分/前後）
        news_check = self.news_filter.is_blackout(now_et)
        if news_check["blocked"]:
            log.info(
                f"[FuturesORB] entry blocked: NEWS BLACKOUT "
                f"event={news_check['event']} "
                f"minutes_to={news_check['minutes_to']:+.1f}"
            )
            return None

        # MFFURuleGuardチェック
        if not self.rule_guard.can_enter_new_position(current_balance, open_pnl):
            log.info("[FuturesORB] entry blocked by MFFURuleGuard")
            return None

        # 環境フィルター（VIX > 35 または env_score < 40 はスキップ）
        if vix > 35.0:
            log.info(f"[FuturesORB] entry skipped: VIX={vix:.1f} > 35.0")
            return None
        if env_score < 40.0:
            log.info(f"[FuturesORB] entry skipped: env_score={env_score:.1f} < 40")
            return None

        or_range = self.or_range

        # ブレイクアウト判定
        action = None
        if current_price > self._or_high:
            action = "Buy"
        elif current_price < self._or_low:
            action = "Sell"

        if action is None:
            return None

        # コントラクト数計算
        current_profit = current_balance - self.rule_guard.initial_balance
        max_contracts  = self.rule_guard.get_allowed_contracts(current_profit)
        n_contracts    = self._calc_contracts(
            account_balance = current_balance,
            max_contracts   = max_contracts,
            or_range        = or_range,
        )

        if n_contracts < 1:
            log.info("[FuturesORB] entry skipped: n_contracts < 1")
            return None

        if self.client is None:
            log.info(f"[FuturesORB] dry_run entry signal: {action} {n_contracts}x "
                     f"@{current_price:.2f}")
            symbol = _get_front_month_symbol(self.product)
        else:
            symbol = self.client.get_front_month_symbol(self.product)

        # ストップ・利確レベル
        if action == "Buy":
            stop_price   = self._or_high - or_range * ORB_STOP_ATR_MULT
            target_price = self._or_high + or_range * ORB_TARGET_ATR_MULT
        else:
            stop_price   = self._or_low + or_range * ORB_STOP_ATR_MULT
            target_price = self._or_low - or_range * ORB_TARGET_ATR_MULT

        log.info(
            f"[FuturesORB] entry signal: {action} {n_contracts}x{symbol} "
            f"@{current_price:.2f} stop={stop_price:.2f} target={target_price:.2f} "
            f"or_range={or_range:.2f}"
        )

        # 発注
        order = None
        if self.client is not None:
            order = self.client.place_order(
                symbol     = symbol,
                action     = action,
                qty        = n_contracts,
                order_type = "Market",
            )
            if not order:
                log.error("[FuturesORB] entry order failed")
                return None
        else:
            # dry_run: mock注文
            order = {
                "order_id":   f"DRYRUN-{uuid.uuid4().hex[:8]}",
                "status":     "Filled",
                "symbol":     symbol,
                "action":     action,
                "qty":        n_contracts,
                "order_type": "Market",
            }

        self._entry_done   = True
        self._stop_price   = stop_price
        self._target_price = target_price
        self._entry_side   = "Long" if action == "Buy" else "Short"
        self._entry_price  = current_price
        self._trade_id     = str(uuid.uuid4())[:8]

        log.info(f"[FuturesORB] entry confirmed: trade_id={self._trade_id} "
                 f"side={self._entry_side} stop={stop_price:.2f} target={target_price:.2f}")

        return {
            "trade_id":    self._trade_id,
            "action":      action,
            "symbol":      symbol,
            "qty":         n_contracts,
            "entry_price": current_price,
            "stop_price":  stop_price,
            "target_price": target_price,
            "order":       order,
        }

    def _calc_contracts(
        self,
        account_balance: float,
        max_contracts: int,
        monthly_realized_pnl: float = 0.0,
        today_max_win_per_contract: Optional[float] = None,
        or_range: Optional[float] = None,
    ) -> int:
        """Consistency-aware Kelly でコントラクト数を計算する。

        MFFU Consistency Rule（40%ライン）を考慮したKelly上限を適用する。
        1日の利益が月間PnLの40%を超えないようにコントラクト数を制限する。

        Args:
            account_balance:              口座残高 (USD)
            max_contracts:                ルールガードが許可する最大コントラクト数
            monthly_realized_pnl:         当月の確定PnL合計 (USD)。0以下の場合は制限なし。
            today_max_win_per_contract:   今日1コントラクトあたりの最大期待利益 (USD)。
                                          None の場合はConsistency制限を適用しない。
            or_range:                     ORレンジ幅（points）。Kelly枚数計算に使用。
                                          None の場合は保守的な1枚を返す。

        Returns:
            最終コントラクト数（1以上）

        B-5修正: Kelly分数は資本比（0.0〜1.0）であり max_contracts への乗数ではない。
        正しいセマンティクス:
            kelly = 0.10 → 資本の10%をリスクにさらす
            dollar_risk = account_balance * kelly
            contracts = floor(dollar_risk / risk_per_contract)
        修正前: floor(0.10 * 5) = floor(0.5) = 0 → max(1,0) = 1 で常時1枚固定
        修正後: dollar_risk=5000(10%@50K) / risk_per_contract=500(100pts×5) = 10 枚 → min(10, max)
        """
        kelly: Optional[float] = None
        if KELLY_AVAILABLE:
            pnl_file = _BASE_DIR / "mffu_pnl.json"
            kelly = calc_kelly_fraction(pnl_file, strategy_filter=None)

        if kelly is None:
            base_contracts = 1
        elif or_range is None or or_range <= 0:
            # or_range 不明時は保守的に1枚
            base_contracts = 1
        else:
            # B-5修正: リスク予算ベースの枚数算出
            point_value      = CONTRACT_SPECS.get(self.product, {}).get("point_value", 5.0)
            risk_per_contract = or_range * ORB_STOP_ATR_MULT * point_value
            if risk_per_contract <= 0:
                base_contracts = 1
            else:
                dollar_risk    = account_balance * kelly
                base_contracts = max(1, min(math.floor(dollar_risk / risk_per_contract), max_contracts))
            log.info(
                f"_calc_contracts(B-5): kelly={kelly:.4f} "
                f"or_range={or_range:.2f} point_value={point_value} "
                f"risk_per_contract=${risk_per_contract:.0f} "
                f"dollar_risk=${dollar_risk:.0f} "
                f"base_contracts={base_contracts}"
            )

        # Consistency-aware Kelly: MFFU Consistency 40%制約
        # 1日の利益が月間PnLの40%を超えないようにコントラクト数を制限する
        if (
            monthly_realized_pnl > 0
            and today_max_win_per_contract is not None
            and today_max_win_per_contract > 0
        ):
            # 1日に稼いでよい最大額 = 月間PnL × 40% × 35% の余裕バッファ
            # (40%ライン違反を避けるため35%で制限)
            monthly_target = account_balance * 0.06  # 月利6%想定（$50K口座で$3K）
            max_daily_pnl = max(monthly_realized_pnl, monthly_target) * 0.35
            consistency_cap = math.floor(max_daily_pnl / today_max_win_per_contract)
            consistency_cap = max(1, consistency_cap)

            if consistency_cap < base_contracts:
                log.info(
                    f"_calc_contracts: Consistency cap {consistency_cap} < Kelly {base_contracts} "
                    f"(monthly_pnl=${monthly_realized_pnl:.0f}, max_daily=${max_daily_pnl:.0f}, "
                    f"win_per_contract=${today_max_win_per_contract:.0f})"
                )
                return consistency_cap

        log.info(f"_calc_contracts: kelly={kelly} base={base_contracts} max={max_contracts}")
        return base_contracts

    def check_exit(
        self,
        current_price:   float,
        current_balance: float,
        open_pnl:        float,
        now_et:          Optional[datetime.datetime] = None,
    ) -> Optional[str]:
        """
        エグジット条件をチェックする。

        MFFUの違い: Intraday DD違反ではなく、
        EOD残高が閾値を下回りそうな場合の"予防的停止"を返す。

        Returns:
            "stop_hit" | "target_hit" | "preventive_eod_halt" | None
        """
        if not self._entry_done:
            return None
        if self._stop_price is None or self._target_price is None:
            return None

        # MFFURuleGuard日中チェック（予防的停止）
        rule_result = self.rule_guard.check_intraday(current_balance, open_pnl)
        if rule_result["action"] == "preventive_halt":
            log.warning(f"[FuturesORB] PREVENTIVE EOD HALT: {rule_result['reasons']}")
            return "preventive_eod_halt"

        # ストップ/利確判定
        if self._entry_side == "Long":
            if current_price <= self._stop_price:
                return "stop_hit"
            if current_price >= self._target_price:
                return "target_hit"
        elif self._entry_side == "Short":
            if current_price >= self._stop_price:
                return "stop_hit"
            if current_price <= self._target_price:
                return "target_hit"

        return None

    def execute_exit(self, reason: str) -> Optional[dict]:
        """エグジット注文を実行する。"""
        if self.client is None:
            log.info(f"[FuturesORB] dry_run exit: reason={reason}")
            self._entry_done = False
            return {"dry_run": True, "reason": reason}

        symbol = self.client.get_front_month_symbol(self.product)
        log.info(f"[FuturesORB] executing exit: reason={reason} symbol={symbol}")

        result = self.client.close_position(symbol)
        if result:
            log.info(f"[FuturesORB] exit confirmed: {result}")
            self._entry_done = False
        else:
            log.error("[FuturesORB] exit order failed")

        return result


# ─────────────────────────────────────────────────────────────────────────────
# ContractRoller — 先物限月ロールオーバー（apex_bot.pyと同一）
# ─────────────────────────────────────────────────────────────────────────────

class ContractRoller:
    """先物限月ロールオーバー管理（apex_bot.pyと同一実装）。"""

    def __init__(self, product: str = "MES"):
        self.product         = product
        self._last_symbol:   Optional[str] = None
        self._rollover_count = 0

    def check_rollover(self) -> dict:
        new_symbol = _get_front_month_symbol(self.product)
        rolled     = (self._last_symbol is not None and self._last_symbol != new_symbol)

        if rolled:
            self._rollover_count += 1
            log.info(f"[ContractRoller] ROLLOVER detected: "
                     f"{self._last_symbol} -> {new_symbol} "
                     f"(count={self._rollover_count})")
            pushover(
                "MFFU Bot: Contract Rollover",
                f"{self._last_symbol} → {new_symbol}",
                priority=0,
            )

        old_symbol        = self._last_symbol
        self._last_symbol = new_symbol

        return {
            "rolled":     rolled,
            "old_symbol": old_symbol,
            "new_symbol": new_symbol,
            "product":    self.product,
        }


# ─────────────────────────────────────────────────────────────────────────────
# MFFUBot — メインBot
# ─────────────────────────────────────────────────────────────────────────────

class ChronosBot:
    """
    MyFundedFutures 先物自動売買Botのメインクラス。

    run_forever() を呼び出すと日次ループを開始する。
    """

    def __init__(
        self,
        account_size: int  = DEFAULT_ACCOUNT_SIZE,
        product:      str  = DEFAULT_PRODUCT,
        paper:        bool = True,
        dry_run:      bool = False,
    ):
        self.account_size = account_size
        self.product      = product
        self.paper        = paper
        self.dry_run      = dry_run

        log.info(
            f"[MFFUBot] init: account_size=${account_size:,} "
            f"product={product} paper={paper} dry_run={dry_run}"
        )

        env = "DEMO" if paper else "LIVE"

        if not dry_run:
            self.client: Optional[TradovateClient] = TradovateClient(env=env)
        else:
            self.client = None
            log.info("[MFFUBot] dry_run: TradovateClient not initialized")

        self.rule_guard   = MFFURuleGuard(account_size)
        self.news_filter  = NewsTradingFilter()

        self.orb = FuturesORBStrategy(
            client       = self.client,
            rule_guard   = self.rule_guard,
            news_filter  = self.news_filter,
            product      = product,
            account_size = account_size,
        )

        self.roller = ContractRoller(product=product)

        # ── マルチ戦術インスタンス ─────────────────────────────────────────────
        self.vix_mr: Optional[VIXMRStrategy] = (
            VIXMRStrategy(client=self.client, product=product)
            if VIX_MR_AVAILABLE else None
        )

        self.trend_follow: Optional[TrendFollowStrategy] = (
            TrendFollowStrategy(client=self.client, product=product)
            if TREND_FOLLOW_AVAILABLE else None
        )

        self.level_trading: Optional[LevelTradingStrategy] = (
            LevelTradingStrategy(product=product, vix=20.0)
            if LEVEL_TRADING_AVAILABLE else None
        )

        # ── P0新規戦術インスタンス ─────────────────────────────────────────────
        self.asia_range_fade: Optional["AsiaRangeFadeStrategy"] = (
            AsiaRangeFadeStrategy()
            if ASIA_RANGE_AVAILABLE else None
        )

        self.gap_fill_advanced: Optional["GapFillAdvancedStrategy"] = (
            GapFillAdvancedStrategy()
            if GAP_FILL_ADVANCED_AVAILABLE else None
        )

        # ── P1新規戦術インスタンス（VIX Term Structure / ES-NQ Spread）──────────
        self.vix_term_structure: Optional["VIXTermStructureStrategy"] = (
            VIXTermStructureStrategy()
            if VIX_TERM_STRUCTURE_AVAILABLE else None
        )

        self.es_nq_spread: Optional["ESNQSpreadStrategy"] = (
            ESNQSpreadStrategy()
            if ES_NQ_SPREAD_AVAILABLE else None
        )

        # 日次状態
        self._premarket_done:       bool  = False
        self._or_finalized:         bool  = False
        self._force_close_done:     bool  = False
        self._nightly_done:         bool  = False
        self._overnight_done:       bool  = False   # 翌日持ち越しエントリー完了フラグ
        self._daily_halt:           bool  = False   # Daily Strong Close Rule 発動フラグ
        self._last_loop_date:       Optional[datetime.date] = None
        self._session_balance:      float = float(account_size)
        self._vix:                  Optional[float] = None
        self._vix_z:                float = 0.0
        self._vix_history:          list  = []
        self._env_score:            float = 50.0
        self._today_realized_pnl:   float = 0.0    # 当日確定P&L（Daily Strong Close用）
        self._month_realized_pnl:   float = 0.0    # 月間累積P&L（Consistency監視用）
        self._weekly_realized_pnl:  float = 0.0    # 週次P&L（週次DD監視用）

    # ── 接続 ──────────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Tradovateに接続する。"""
        if self.dry_run:
            log.info("[MFFUBot] dry_run: connect skipped")
            return True

        log.info("[MFFUBot] connecting to Tradovate...")
        if not self.client.authenticate():
            log.error("[MFFUBot] authentication failed")
            pushover("MFFU Bot: Auth Failed", "Tradovate認証失敗", priority=1)
            return False

        balance = self.client.get_account_balance()
        if balance:
            # B-2修正: 純現金残高（balance）を使う。total_equityは含み損益込みのため不可
            self._session_balance = balance.get("balance", float(self.account_size))
            log.info(f"[MFFUBot] connected: balance=${self._session_balance:,.0f}")

        return True

    # ── プレマーケット評価 ─────────────────────────────────────────────────────

    def run_premarket(self) -> bool:
        """9:00-9:30 ET: プレマーケット評価を実行する。"""
        log.info("[MFFUBot] running premarket assessment...")

        # ニュースカレンダー再ロード（毎朝最新に）
        self.news_filter.reload()

        # 次のニュースイベント確認
        next_event = self.news_filter.next_event()
        if next_event:
            log.info(
                f"[MFFUBot] next high-impact event: "
                f"{next_event['event']} in {next_event['minutes_to']:.0f}min"
            )

        # VIX取得
        self._vix = get_vix()
        if self._vix is None:
            log.warning("[MFFUBot] VIX取得失敗 → fallback 20.0")
            self._vix = 20.0

        # VIX履歴取得
        self._vix_history = get_vix_history(60)
        log.info(f"[MFFUBot] VIX={self._vix:.1f} history={len(self._vix_history)}days")

        # VIX Zスコア算出（VIX-MR 戦術用）
        if VIX_MR_AVAILABLE and self._vix_history:
            z = calc_vix_z_score(self._vix, self._vix_history)
            self._vix_z = z if z is not None else 0.0
            log.info(f"[MFFUBot] VIX-Z={self._vix_z:.2f}")
        else:
            self._vix_z = 0.0

        # P1: VIX Term Structure 更新（毎朝プレマーケットで期間構造を確認）
        if VIX_TERM_STRUCTURE_AVAILABLE and self.vix_term_structure is not None:
            try:
                ts_data = fetch_vix_term_structure_data()
                vix3m = ts_data.get("vix3m")
                vix6m = ts_data.get("vix6m")
                if vix3m is not None:
                    self.vix_term_structure.update(
                        vix   = self._vix,
                        vix3m = vix3m,
                        vix6m = vix6m,
                    )
                    log.info(
                        f"[MFFUBot] VIX Term Structure: "
                        f"structure={self.vix_term_structure.structure} "
                        f"size_mult={self.vix_term_structure.size_multiplier:.2f}"
                    )
                else:
                    log.warning("[MFFUBot] VIX3M取得失敗 → term structure 更新スキップ")
            except Exception as e:
                log.warning(f"[MFFUBot] VIX Term Structure update error: {e}")

        # P1: ES-NQ Spread 比率履歴更新（毎朝日次足終値を取得して比率を積み上げ）
        if ES_NQ_SPREAD_AVAILABLE and self.es_nq_spread is not None:
            try:
                es_nq_data = fetch_es_nq_prices(days=30)
                ratios = es_nq_data.get("ratios", [])
                if ratios:
                    # 既存履歴をリセットして最新データで再構築
                    self.es_nq_spread._ratio_history = ratios
                    log.info(
                        f"[MFFUBot] ES-NQ spread history: {len(ratios)} days "
                        f"latest_es={es_nq_data.get('latest_es')} "
                        f"latest_nq={es_nq_data.get('latest_nq')}"
                    )
                else:
                    log.warning("[MFFUBot] ES-NQ price data unavailable → spread 更新スキップ")
            except Exception as e:
                log.warning(f"[MFFUBot] ES-NQ spread update error: {e}")

        # 残高取得・EODリセット（B-2修正: 純現金残高を使う）
        if not self.dry_run and self.client:
            balance_info = self.client.get_account_balance()
            if balance_info:
                self._session_balance = balance_info.get("balance", self._session_balance)

        self.rule_guard.reset_day(self._session_balance)

        # ロールオーバーチェック
        rollover = self.roller.check_rollover()
        if rollover["rolled"]:
            log.info(f"[MFFUBot] contract rolled: {rollover['old_symbol']} -> {rollover['new_symbol']}")

        # 環境スコア算出: mffu_strategy_selector.select_futures_strategy() を使用する。
        # NOTE: atlas の strategy_selector.select_strategy() はSPY/SPXオプション向けのため
        #       先物Botでは使わない。mffu_strategy_selector のみが正しいセレクター。
        if MFFU_SELECTOR_AVAILABLE and self._vix_history:
            try:
                import datetime as _dt
                from zoneinfo import ZoneInfo as _ZoneInfo
                _time_et = _dt.datetime.now(_ZoneInfo("America/New_York")).strftime("%H:%M")
                env_dict = build_env_dict(
                    vix=self._vix,
                    vix_history=self._vix_history,
                    vix_z=self._vix_z,
                    time_et=_time_et,
                    account_pnl_day=self._today_realized_pnl,
                    account_pnl_month=self._month_realized_pnl,
                    account_balance=self._session_balance,
                )
                mffu_result = select_futures_strategy(env_dict)
                # CRITICAL-2修正: 返り値は list[dict]。[0]でprimaryを取得する
                primary = mffu_result[0] if mffu_result else {}
                self._env_score = env_dict.get("env_score", 50.0)
                log.info(
                    f"[MFFUBot] mffu_strategy_selector: "
                    f"primary={primary.get('strategy')} "
                    f"confidence={primary.get('confidence', 0.0):.2f} "
                    f"score={self._env_score:.1f}"
                )
            except Exception as e:
                log.warning(f"[MFFUBot] mffu_strategy_selector error: {e}")
                # フォールバック: VIX帯別スコア
                if self._vix < 15:
                    self._env_score = 80.0
                elif self._vix < 22:
                    self._env_score = 65.0
                elif self._vix < 30:
                    self._env_score = 45.0
                else:
                    self._env_score = 20.0
        else:
            if self._vix < 15:
                self._env_score = 80.0
            elif self._vix < 22:
                self._env_score = 65.0
            elif self._vix < 30:
                self._env_score = 45.0
            else:
                self._env_score = 20.0
            log.info(f"[MFFUBot] env_score (fallback): {self._env_score:.1f}")

        # Level Trading: 当日レベルを計算（プレマーケット必須タスク）
        if self.level_trading is not None:
            self.level_trading.reset_day()
            self._compute_level_trading_levels()

        # Profit Target達成チェック
        pt = check_profit_target(
            self.rule_guard.rules,
            self.rule_guard.initial_balance,
            self._session_balance,
        )
        if pt["achieved"]:
            log.info(f"[MFFUBot] PROFIT TARGET ACHIEVED! profit=${pt['profit']:.0f}")
            pushover(
                "MFFU Bot: Profit Target達成",
                f"利益 ${pt['profit']:,.0f} / 目標 ${pt['target']:,.0f}",
                priority=1,
            )

        # ── Gap Fill Advanced: 前日終値・当日始値・ATR5日をセットアップ ──────────
        if GAP_FILL_ADVANCED_AVAILABLE and self.gap_fill_advanced is not None:
            try:
                gf_prev_close: Optional[float] = None
                gf_current_open: Optional[float] = None
                gf_atr_5d: float = 10.0  # フォールバック値

                if not self.dry_run and self.client:
                    symbol = self.client.get_front_month_symbol(self.product)
                    bars = self.client.get_bars(symbol, bar_type="DailyBar", unit=1, count=6)
                    if bars and len(bars) >= 2:
                        gf_prev_close   = bars[-2].get("close")
                        gf_current_open = bars[-1].get("open")
                        # ATR5日: 直近5本の high-low 平均
                        ranges = [b.get("high", 0) - b.get("low", 0) for b in bars[-6:-1] if b.get("high") and b.get("low")]
                        if ranges:
                            gf_atr_5d = sum(ranges) / len(ranges)

                # フォールバック（dry_run または取得失敗）
                if gf_prev_close is None:
                    gf_prev_close   = self._session_balance / 10.0
                if gf_current_open is None:
                    gf_current_open = gf_prev_close

                self.gap_fill_advanced.setup(
                    prev_close   = gf_prev_close,
                    current_open = gf_current_open,
                    atr_5d       = gf_atr_5d,
                )
                log.info(
                    f"[MFFUBot] Gap Fill Advanced setup: "
                    f"prev_close={gf_prev_close:.2f} "
                    f"open={gf_current_open:.2f} "
                    f"atr_5d={gf_atr_5d:.2f}"
                )
            except Exception as e:
                log.warning(f"[MFFUBot] Gap Fill Advanced setup error: {e}")

        log.info(
            f"[MFFUBot] premarket done: "
            f"{self.rule_guard.status_summary(self._session_balance)}"
        )
        self._premarket_done = True
        return True

    # ── ヘルパー ──────────────────────────────────────────────────────────────

    def _compute_level_trading_levels(self):
        """
        プレマーケット時に当日の Level Trading レベルを計算する。

        前日 OHLC を Tradovate から取得する（dry_run 時はフォールバック値を使用）。
        """
        if self.level_trading is None:
            return

        prev_high  = None
        prev_low   = None
        prev_close = None

        if not self.dry_run and self.client:
            try:
                symbol = self.client.get_front_month_symbol(self.product)
                # 日足2本分取得（[0]=前日, [1]=当日途中）
                bars = self.client.get_bars(symbol, bar_type="DailyBar", unit=1, count=2)
                if bars and len(bars) >= 1:
                    prev_bar = bars[0]
                    prev_high  = prev_bar.get("high")
                    prev_low   = prev_bar.get("low")
                    prev_close = prev_bar.get("close")
            except Exception as e:
                log.warning(f"[MFFUBot] _compute_level_trading_levels: {e}")

        if prev_high is None or prev_low is None or prev_close is None:
            # dry_run / APIエラー時: セッション残高から推定（±0.5%レンジ想定）
            ref = self._session_balance / 10.0   # 概算の価格参照（MESの場合）
            prev_high  = ref * 1.005
            prev_low   = ref * 0.995
            prev_close = ref
            log.info(
                f"[MFFUBot] Level Trading: using fallback OHLC "
                f"H={prev_high:.2f} L={prev_low:.2f} C={prev_close:.2f}"
            )

        self.level_trading.compute_daily_levels(
            prev_high  = prev_high,
            prev_low   = prev_low,
            prev_close = prev_close,
            vix        = self._vix,
        )
        log.info(
            f"[MFFUBot] Level Trading levels computed:\n"
            f"{self.level_trading.levels_summary()}"
        )

    def _get_current_balance_and_pnl(self) -> tuple[float, float]:
        """
        (cash_balance, open_pnl) を返す。

        B-2修正: total_equity（= 現金 + 含み損益）を使うと
        rule_guard.check_intraday(balance, open_pnl) 内の
        hypothetical_eod = balance + open_pnl が二重カウントになる。
        "balance" キー（= totalCashValue = 純現金）を使うことで正確な計算にする。
        """
        if self.dry_run or not self.client:
            return self._session_balance, 0.0
        balance_info = self.client.get_account_balance()
        if balance_info:
            # "balance" = totalCashValue（純現金）。"total_equity" は含み損益込みのため使わない
            cash_balance = balance_info.get("balance", self._session_balance)
            open_pnl     = balance_info.get("unrealized_pnl", 0.0)
            return cash_balance, open_pnl
        return self._session_balance, 0.0

    def _get_current_price(self) -> Optional[float]:
        if self.dry_run or not self.client:
            return None
        symbol = self.client.get_front_month_symbol(self.product)
        quote  = self.client.get_quote(symbol)
        if quote:
            return quote.get("last")
        return None

    def _update_or_range(self):
        if self.dry_run or not self.client:
            return
        symbol = self.client.get_front_month_symbol(self.product)
        bars   = self.client.get_bars(symbol, bar_type="MinuteBar", unit=1, count=5)
        for bar in bars:
            if bar.get("high") and bar.get("low"):
                self.orb.update_or_candle(bar["high"], bar["low"])

    # ── メインループ ──────────────────────────────────────────────────────────

    def run_forever(self):
        """メインループ。60秒ごとに実行。"""
        log.info("[MFFUBot] starting run_forever()")
        pushover(
            "MFFU Bot: 起動",
            f"${self.account_size:,} {self.product} paper={self.paper}"
        )

        if not self.connect():
            log.error("[MFFUBot] connection failed, exiting")
            return

        while True:
            try:
                now_et   = datetime.datetime.now(ET)
                now_date = now_et.date()
                t        = now_et.time()

                # 日次リセット
                if self._last_loop_date != now_date:
                    self._daily_reset(now_date)
                    self._last_loop_date = now_date

                # 週末はスキップ
                if now_et.weekday() >= 5:
                    log.debug("[MFFUBot] weekend, sleeping 1h")
                    time.sleep(3600)
                    continue

                # ── 9:00〜9:30 ET: プレマーケット ──
                if datetime.time(9, 0) <= t < datetime.time(9, 30):
                    if not self._premarket_done:
                        self.run_premarket()
                        self.orb.reset_day()

                # ── 9:30〜10:00 ET: ORレンジ計測 ──
                elif datetime.time(9, 30) <= t < datetime.time(10, 0):
                    if self._premarket_done and not self._or_finalized:
                        self._update_or_range()

                # ── 10:00 ET: ORレンジ確定 ──
                elif datetime.time(10, 0) <= t < datetime.time(10, 1):
                    if not self._or_finalized:
                        self.orb.finalize_or()
                        self._or_finalized = True
                        log.info(f"[MFFUBot] OR confirmed: range={self.orb.or_range}")

                # ── Daily Strong Close Rule チェック（毎分）──
                if not self._daily_halt and self._premarket_done:
                    balance, _ = self._get_current_balance_and_pnl()
                    dsc_result = self.check_daily_strong_close(balance)
                    if dsc_result["halt"]:
                        log.warning(
                            f"[MFFUBot] DAILY STRONG CLOSE: "
                            f"action={dsc_result['action']} "
                            f"reason={dsc_result['reason']}"
                        )
                        self._daily_halt = True
                        if dsc_result["action"] == "close_all":
                            # 全ポジクローズ
                            if not self.dry_run and self.client:
                                self.client.close_all_positions()
                            if self.vix_mr is not None and self.vix_mr.has_position:
                                price = self._get_current_price()
                                if price:
                                    self.vix_mr.force_close(
                                        price, dry_run=self.dry_run, reason="daily_loss_halt"
                                    )
                            pushover(
                                "MFFU Bot: Daily Loss Halt",
                                dsc_result["reason"],
                                priority=1,
                            )
                        else:
                            pushover(
                                "MFFU Bot: Daily Profit Cap",
                                dsc_result["reason"],
                                priority=0,
                            )

                # ── 週次DD制限チェック ──
                if self._premarket_done and self.check_weekly_dd_halt():
                    if not self._daily_halt:
                        self._daily_halt = True
                        pushover(
                            "MFFU Bot: 週次DD停止",
                            f"週次P&L={self._weekly_realized_pnl:+.0f} <= -{WEEKLY_DD_HALT_PCT:.0%}",
                            priority=1,
                        )

                # ── 10:00〜12:00 ET: エントリーウィンドウ ──
                elif datetime.time(10, 0) <= t < datetime.time(12, 0):
                    if self._or_finalized and not self._daily_halt:
                        balance, open_pnl = self._get_current_balance_and_pnl()
                        price = self._get_current_price()

                        if price:
                            # ORB エントリー（ORBがまだエントリーしていない場合）
                            if not self.orb._entry_done:
                                entry = self.orb.check_breakout(
                                    current_price   = price,
                                    current_balance = balance,
                                    vix             = self._vix or 20.0,
                                    env_score       = self._env_score,
                                    open_pnl        = open_pnl,
                                    now_et          = now_et,
                                )
                                if entry:
                                    log.info(f"[MFFUBot] ORB ENTRY: {entry}")
                                    pushover(
                                        "MFFU Bot: ORBエントリー",
                                        f"{entry['action']} {entry['qty']}x{entry['symbol']} "
                                        f"@{entry['entry_price']:.2f}",
                                    )

                            # Level Trading エントリー（ORBと独立・並行）
                            if (
                                self.level_trading is not None
                                and not self.level_trading.has_position
                                and self.rule_guard.can_enter_new_position(balance, open_pnl)
                            ):
                                # IBを更新（10:30以降はIB確定済みとして使用）
                                if datetime.time(10, 30) <= t:
                                    level_signal = self.level_trading.check_entry(
                                        price      = price,
                                        volume     = 0,   # volume未取得時は0（フィルターOFF）
                                        kelly_frac = 0.5,
                                    )
                                    if level_signal:
                                        log.info(f"[MFFUBot] LEVEL ENTRY: {level_signal}")
                                        pushover(
                                            "MFFU Bot: Level Tradingエントリー",
                                            f"{level_signal['action']} "
                                            f"level={level_signal['level']} "
                                            f"conf={level_signal['confidence']:.2f} "
                                            f"@{price:.2f} "
                                            f"stop={level_signal['stop_price']:.2f}",
                                        )

                # ── Level Trading エグジット監視 ──
                if self.level_trading is not None and self.level_trading.has_position:
                    price = self._get_current_price()
                    if price:
                        level_exit = self.level_trading.manage_position(
                            pos   = {"side": self.level_trading._entry_side or "Long"},
                            price = price,
                        )
                        if level_exit:
                            log.info(f"[MFFUBot] LEVEL EXIT: {level_exit}")
                            pushover(
                                f"MFFU Bot: Level Tradingエグジット ({level_exit['reason']})",
                                f"exit_price={level_exit['exit_price']:.2f}",
                            )

                # ── ORB エントリー後: エグジット監視 ──
                if self.orb._entry_done:
                    balance, open_pnl = self._get_current_balance_and_pnl()
                    price = self._get_current_price()

                    if price:
                        exit_reason = self.orb.check_exit(
                            price, balance, open_pnl, now_et
                        )
                        if exit_reason:
                            result = self.orb.execute_exit(exit_reason)
                            if result:
                                log.info(f"[MFFUBot] EXIT: reason={exit_reason}")
                                pushover(
                                    f"MFFU Bot: エグジット ({exit_reason})",
                                    f"balance=${balance:,.0f}",
                                )

                # ── Asia Range Fade: レンジ形成 & エントリー管理 ──
                # Asia session (18:00-03:00 ET) 中に価格を収集してレンジを形成する
                if ASIA_RANGE_AVAILABLE and self.asia_range_fade is not None:
                    if is_asia_session(now_et) and not self._daily_halt:
                        price = self._get_current_price()
                        if price:
                            # 02:00 ETまでは価格を追加
                            self.asia_range_fade.add_price(price, now_et)
                            # 02:00 ET になったらレンジ確定
                            if t == datetime.time(2, 0) and not self.asia_range_fade.range_confirmed:
                                confirmed = self.asia_range_fade.confirm_range()
                                if confirmed:
                                    log.info(
                                        f"[MFFUBot] Asia range confirmed: "
                                        f"high={confirmed['high']:.2f} "
                                        f"low={confirmed['low']:.2f}"
                                    )
                            # レンジ確定後はエントリー・管理
                            if self.asia_range_fade.range_confirmed:
                                vix_now = self._vix or 20.0
                                atr_20d = self._env_score  # 暫定: env_scoreをATR代替に使用
                                # 実運用では atr_20d を別途計算する
                                result = self.asia_range_fade.evaluate(
                                    current_price = price,
                                    atr_20d       = max(5.0, atr_20d / 10),
                                    vix           = vix_now,
                                    now_et        = now_et,
                                )
                                if result and result.get("type") == "entry":
                                    log.info(f"[MFFUBot] ASIA RANGE FADE ENTRY: {result}")
                                    pushover(
                                        "MFFU Bot: Asia Range Fadeエントリー",
                                        f"{result['side'].upper()} "
                                        f"@{result['entry']:.2f} "
                                        f"tp={result['tp']:.2f} sl={result['sl']:.2f}",
                                    )
                                elif result and result.get("action") == "close":
                                    log.info(
                                        f"[MFFUBot] ASIA RANGE FADE CLOSE: "
                                        f"reason={result.get('reason')}"
                                    )
                                    pushover(
                                        f"MFFU Bot: Asia Range Fadeクローズ "
                                        f"({result.get('reason')})",
                                        f"price={result.get('price', 0):.2f}",
                                    )

                # ── Gap Fill Advanced: エントリー & 管理 ──
                if (
                    GAP_FILL_ADVANCED_AVAILABLE
                    and self.gap_fill_advanced is not None
                    and self._premarket_done
                    and not self._daily_halt
                ):
                    if datetime.time(9, 35) <= t <= datetime.time(11, 30):
                        price = self._get_current_price()
                        if price:
                            result = self.gap_fill_advanced.evaluate(
                                current_price = price,
                                vix           = self._vix or 20.0,
                                now_et        = now_et,
                                sma_trend     = self.orb._sma_state if hasattr(self.orb, "_sma_state") else None,
                            )
                            if result and result.get("type") == "entry":
                                log.info(f"[MFFUBot] GAP FILL ADVANCED ENTRY: {result}")
                                pushover(
                                    "MFFU Bot: Gap Fill Advancedエントリー",
                                    f"{result['side'].upper()} "
                                    f"gap={result['gap_pct']:.2f}% "
                                    f"tp={result['tp']:.2f} sl={result['sl']:.2f}",
                                )
                            elif result and result.get("action") == "close":
                                log.info(
                                    f"[MFFUBot] GAP FILL ADVANCED CLOSE: "
                                    f"reason={result.get('reason')}"
                                )
                                pushover(
                                    f"MFFU Bot: Gap Fill Advancedクローズ "
                                    f"({result.get('reason')})",
                                    f"price={result.get('price', 0):.2f}",
                                )

                # ── オーバーナイトポジション管理（VIX-MR / TF）──
                if datetime.time(9, 35) <= t <= datetime.time(15, 30):
                    self._manage_overnight_positions(now_et)

                # ── 15:40〜15:55 ET: 翌日持ち越しエントリー判断 ──
                if datetime.time(15, 40) <= t < datetime.time(15, 55):
                    if not self._overnight_done and not self._daily_halt:
                        self._run_overnight_entries(now_et)

                # ── 15:45 ET: 強制クローズ（ORB等の当日建玉）──
                if datetime.time(15, 45) <= t < datetime.time(15, 50):
                    if not self._force_close_done:
                        self._force_close()

                # ── 16:30 ET: EODチェック + 日次レポート ──
                if datetime.time(16, 30) <= t < datetime.time(16, 35):
                    if not self._nightly_done:
                        self._run_nightly()

                # トークンrenew
                if not self.dry_run and self.client:
                    self.client.ensure_authenticated()

            except KeyboardInterrupt:
                log.info("[MFFUBot] KeyboardInterrupt: shutting down")
                break
            except Exception as e:
                log.error(f"[MFFUBot] loop error: {e}", exc_info=True)
                pushover("MFFU Bot: エラー", str(e)[:200], priority=0)

            time.sleep(MAIN_LOOP_SLEEP_SECS)

        log.info("[MFFUBot] run_forever() exited")
        pushover("MFFU Bot: 停止", "run_forever() exited")

    def select_strategies(
        self,
        time_et:      str,
        gap_pct:      float = 0.0,
        daily_prices: Optional[list] = None,
        session:      Optional[str]  = None,
    ) -> list[dict]:
        """
        現在の環境から稼働すべき戦術リストを返す（mffu_strategy_selectorを使用）。

        Args:
            time_et:      現在時刻 "HH:MM"（ET）
            gap_pct:      始値ギャップ率 (%)
            daily_prices: 日次足終値リスト（TF戦術のSMA算出用）
            session:      セッション名（None の場合は time_et から自動判定）

        Returns:
            戦術リスト（select_futures_strategy の返値と同形式）
        """
        if not MFFU_SELECTOR_AVAILABLE:
            log.warning("[MFFUBot] chronos_strategy_selector not available: fallback to ORB only")
            return [{"strategy": "orb", "size_pct": 1.0, "confidence": 0.7, "reason": "fallback"}]

        # セッション判定（未指定の場合は time_et から自動算出）
        if session is None and SESSION_STRATEGY_AVAILABLE:
            session = get_current_session(time_et)
            log.info(f"[MFFUBot] auto-detected session={session} for time_et={time_et}")

        # SMA状態（TF戦術用）
        sma_state = None
        if daily_prices and len(daily_prices) >= 51:
            from futures_trend_follow import calc_sma
            sma20 = calc_sma(daily_prices, 20)
            sma50 = calc_sma(daily_prices, 50)
            if sma20 is not None and sma50 is not None:
                sma_state = "above" if sma20 > sma50 else "below"

        env = build_env_dict(
            vix               = self._vix or 20.0,
            vix_history       = self._vix_history,
            vix_z             = self._vix_z,
            time_et           = time_et,
            account_pnl_day   = self._today_realized_pnl,
            account_pnl_month = self._month_realized_pnl,
            account_balance   = self._session_balance,
            consistency_used  = 0.0,  # TODO: Consistency監視値を渡す
            gap_pct           = gap_pct,
            sma20_vs_sma50    = sma_state,
            session           = session,
        )

        # ── P2戦術用フィールドを env に追加 ──────────────────────────────────
        # current_price / atr / vp_profile / donchian_channel / volume 等を渡す。
        # 未取得の場合は None（各戦術はスキップ）。
        if hasattr(self, "_current_price") and self._current_price:
            env["current_price"] = self._current_price

        if hasattr(self, "_atr") and self._atr:
            env["atr"] = self._atr

        # Volume Profile: 事前に算出済みの場合のみ渡す
        if hasattr(self, "_vp_profile") and self._vp_profile:
            env["vp_profile"] = self._vp_profile

        # Donchian Channel: 事前に算出済みの場合のみ渡す
        if hasattr(self, "_donchian_channel") and self._donchian_channel:
            env["donchian_channel"] = self._donchian_channel
            env["current_volume"]   = getattr(self, "_current_volume", 0.0)
            env["avg_volume"]       = getattr(self, "_avg_volume", 0.0)
            env["recent_closes"]    = getattr(self, "_recent_closes", None)

        strategies = select_futures_strategy(env)

        # P1: VIX Term Structure のsize_multiplierを既存戦術サイズに適用
        # no_trade 戦術は乗算対象から外す
        if (
            VIX_TERM_STRUCTURE_AVAILABLE
            and self.vix_term_structure is not None
            and self.vix_term_structure.size_multiplier != 1.0
        ):
            mult = self.vix_term_structure.size_multiplier
            for s in strategies:
                if s.get("strategy") != "no_trade":
                    original = s["size_pct"]
                    s["size_pct"] = min(1.0, original * mult)
                    log.info(
                        f"[MFFUBot] VTS size_mult applied: "
                        f"strategy={s['strategy']} "
                        f"{original:.2f} × {mult:.2f} = {s['size_pct']:.2f} "
                        f"(structure={self.vix_term_structure.structure})"
                    )

        return strategies

    def check_daily_strong_close(self, balance: float) -> dict:
        """
        Daily Strong Close Rule チェック（設計書 C-4）。

        - 日内損失 -2% 超 → 即全ポジクローズ
        - 日内利益 +5% 超 → 残り時間ノートレード（Consistency保護）

        Args:
            balance: 現在の口座残高

        Returns:
            {
              "halt":    bool,   # True = 取引停止
              "action":  str,    # "close_all" | "no_new_entry" | "ok"
              "reason":  str,
            }
        """
        pnl     = balance - self.rule_guard.day_start_balance
        pnl_pct = pnl / self.rule_guard.day_start_balance if self.rule_guard.day_start_balance > 0 else 0

        # 損失 -2% → 即クローズ
        if pnl_pct <= -DAILY_LOSS_HALT_PCT:
            return {
                "halt":   True,
                "action": "close_all",
                "reason": (
                    f"daily_loss_halt: pnl={pnl:+.0f} "
                    f"({pnl_pct:+.1%}) <= -{DAILY_LOSS_HALT_PCT:.0%}"
                ),
            }

        # 利益 +5% → ノートレード（Consistency保護）
        if pnl_pct >= DAILY_PROFIT_CAP_PCT:
            return {
                "halt":   True,
                "action": "no_new_entry",
                "reason": (
                    f"daily_profit_cap: pnl={pnl:+.0f} "
                    f"({pnl_pct:+.1%}) >= +{DAILY_PROFIT_CAP_PCT:.0%} "
                    "Consistency保護"
                ),
            }

        return {"halt": False, "action": "ok", "reason": ""}

    def check_weekly_dd_halt(self) -> bool:
        """
        週次DD制限チェック（設計書 Weekly DD -3%超で翌週まで停止）。

        Returns:
            True = 停止すべき
        """
        if self.rule_guard.account_size <= 0:
            return False

        weekly_dd_pct = self._weekly_realized_pnl / self.rule_guard.initial_balance
        if weekly_dd_pct <= -WEEKLY_DD_HALT_PCT:
            log.warning(
                f"[MFFUBot] weekly DD halt: "
                f"weekly_pnl={self._weekly_realized_pnl:+.0f} "
                f"({weekly_dd_pct:+.1%}) <= -{WEEKLY_DD_HALT_PCT:.0%}"
            )
            return True
        return False

    def _run_overnight_entries(
        self,
        now_et: datetime.datetime,
    ):
        """
        15:40-15:55 ET: 翌日持ち越し戦術（VIX-MR / TF）のエントリー判断。
        """
        if self._overnight_done or self._daily_halt:
            return

        time_str = now_et.strftime("%H:%M")
        strategies = self.select_strategies(time_et=time_str, session=None)  # session auto-detect

        balance, _ = self._get_current_balance_and_pnl()
        price      = self._get_current_price()
        if price is None:
            log.warning("[MFFUBot] overnight entries: price unavailable")
            return

        for s in strategies:
            strategy_name = s.get("strategy")

            # VIX-MR エントリー
            if strategy_name == "vix_mr_long" and self.vix_mr is not None:
                entry_check = self.vix_mr.should_enter(
                    current_vix  = self._vix or 20.0,
                    vix_history  = self._vix_history,
                    size_pct     = s.get("size_pct", 1.0),
                )
                if entry_check["enter"]:
                    qty = self.rule_guard.get_allowed_contracts(
                        balance - self.rule_guard.initial_balance
                    )
                    qty = max(1, round(qty * s.get("size_pct", 1.0)))
                    entry = self.vix_mr.enter_long(
                        current_price = price,
                        qty           = qty,
                        entry_date    = now_et.date(),
                        dry_run       = self.dry_run,
                    )
                    if entry:
                        log.info(f"[MFFUBot] VIX-MR ENTRY: {entry}")
                        pushover(
                            "MFFU Bot: VIX-MR エントリー",
                            f"Long {entry['qty']}x{entry['symbol']} "
                            f"@{entry['entry_price']:.2f} "
                            f"z={entry_check.get('z_score', 0):.2f}",
                        )

        # P1: VIX Term Structure MR Longエントリー（独立シグナル）
        if (
            VIX_TERM_STRUCTURE_AVAILABLE
            and self.vix_term_structure is not None
            and self._vix is not None
        ):
            try:
                ts_data = fetch_vix_term_structure_data()
                vix3m = ts_data.get("vix3m")
                if vix3m is not None:
                    vts_entry = self.vix_term_structure.check_entry(
                        vix             = self._vix,
                        vix3m           = vix3m,
                        vix6m           = ts_data.get("vix6m"),
                        account_balance = self._session_balance,
                    )
                    if vts_entry is not None:
                        log.info(f"[MFFUBot] VIX Term Structure MR ENTRY signal: {vts_entry}")
                        pushover(
                            "MFFU Bot: VIX Term Structure MR エントリーシグナル",
                            f"direction={vts_entry['direction']} "
                            f"structure={self.vix_term_structure.structure} "
                            f"size={vts_entry['size_pct']:.0%} "
                            f"reason={vts_entry['reason'][:80]}",
                        )
                        # 実際の発注はVIX-MRエンジンと同様にrule_guardを通じて行う
                        # TODO: VTSエントリー専用発注実装（現在はシグナルログのみ）
            except Exception as e:
                log.warning(f"[MFFUBot] VIX Term Structure entry check error: {e}")

        # P1: ES-NQ Spread エントリーチェック（ペアトレード）
        if (
            ES_NQ_SPREAD_AVAILABLE
            and self.es_nq_spread is not None
            and not self.es_nq_spread.has_position
        ):
            try:
                es_nq_data = fetch_es_nq_prices(days=30)
                latest_es = es_nq_data.get("latest_es")
                latest_nq = es_nq_data.get("latest_nq")
                if latest_es is not None and latest_nq is not None:
                    spread_entry = self.es_nq_spread.check_entry(
                        es=latest_es,
                        nq=latest_nq,
                    )
                    if spread_entry is not None:
                        log.info(f"[MFFUBot] ES-NQ Spread ENTRY signal: {spread_entry}")
                        pushover(
                            "MFFU Bot: ES-NQ Spreadエントリーシグナル",
                            f"ES={spread_entry['es'].upper()} "
                            f"NQ={spread_entry['nq'].upper()} "
                            f"z={spread_entry['z']:.2f} "
                            f"size={spread_entry['size_pct']:.0%}",
                        )
                        # ポジション記録（実際の発注はTradovate API経由で実装予定）
                        # TODO: 両足同時発注実装（証拠金2倍確認後）
                        self.es_nq_spread.enter_position(spread_entry)
            except Exception as e:
                log.warning(f"[MFFUBot] ES-NQ spread entry check error: {e}")

        self._overnight_done = True

    def _manage_overnight_positions(
        self,
        now_et: datetime.datetime,
    ):
        """
        VIX-MR / TF ポジションの日中管理（毎分ループで呼ぶ）。
        """
        price = self._get_current_price()
        if price is None:
            return

        today = now_et.date()

        # VIX-MR ポジション管理
        if self.vix_mr is not None and self.vix_mr.has_position:
            exit_info = self.vix_mr.manage_position(
                current_price = price,
                today         = today,
                dry_run       = self.dry_run,
            )
            if exit_info:
                log.info(f"[MFFUBot] VIX-MR EXIT: {exit_info}")
                pnl = exit_info.get("pnl_per_contract", 0.0)
                self._today_realized_pnl  += pnl
                self._month_realized_pnl  += pnl
                self._weekly_realized_pnl += pnl
                pushover(
                    f"MFFU Bot: VIX-MR エグジット ({exit_info['reason']})",
                    f"pnl={pnl:+.2f} hold_days={exit_info.get('hold_days', 0)}",
                )

        # P1: ES-NQ Spread ポジション管理（毎分エグジット確認）
        if (
            ES_NQ_SPREAD_AVAILABLE
            and self.es_nq_spread is not None
            and self.es_nq_spread.has_position
        ):
            try:
                es_nq_data = fetch_es_nq_prices(days=30)
                latest_es = es_nq_data.get("latest_es")
                latest_nq = es_nq_data.get("latest_nq")
                if latest_es is not None and latest_nq is not None:
                    exit_reason = self.es_nq_spread.check_exit(
                        es=latest_es,
                        nq=latest_nq,
                    )
                    if exit_reason is not None:
                        # V2-1修正: close_position に pnl=0.0 を渡す。
                        # 実際の発注（TODO）が実装されたら発注結果から pnl を計算して渡すこと。
                        # 現時点では記録のみ（発注未実装のため pnl=0.0）。
                        closed = self.es_nq_spread.close_position(exit_reason, pnl=0.0)
                        log.info(f"[MFFUBot] ES-NQ Spread EXIT: {exit_reason} {closed}")
                        pnl_from_spread = closed.get("pnl", 0.0) if closed else 0.0
                        if pnl_from_spread != 0.0:
                            self._today_realized_pnl  += pnl_from_spread
                            self._month_realized_pnl  += pnl_from_spread
                            self._weekly_realized_pnl += pnl_from_spread
                        pushover(
                            f"MFFU Bot: ES-NQ Spreadエグジット ({exit_reason})",
                            f"ES={latest_es:.2f} NQ={latest_nq:.2f}",
                        )
            except Exception as e:
                log.warning(f"[MFFUBot] ES-NQ spread exit check error: {e}")

    def _daily_reset(self, today: datetime.date):
        """日次リセット処理。"""
        log.info(f"[MFFUBot] daily reset for {today}")
        self._premarket_done   = False
        self._or_finalized     = False
        self._force_close_done = False
        self._nightly_done     = False
        self._overnight_done   = False
        self._daily_halt       = False
        self._today_realized_pnl = 0.0

        # Level Trading 日次リセット（プレマーケットで再計算される）
        if self.level_trading is not None:
            self.level_trading.reset_day()

        # Asia Range Fade リセット（Asiaセッション開始時に再度 reset() される）
        if self.asia_range_fade is not None:
            self.asia_range_fade.reset()

        # Gap Fill Advanced リセット（当日の prev_close/open は _premarket() で設定）
        # GapFillAdvancedStrategy は setup() を呼ぶまで動かないため reset は不要

        # P1: ES-NQ Spread — 日次リセット（強制クローズ後にポジション状態をクリア）
        # VIX Term Structure は毎朝 run_premarket() で再取得・更新されるためここでは不要
        if self.es_nq_spread is not None and self.es_nq_spread.has_position:
            log.info(
                "[MFFUBot] daily reset: ES-NQ spread position force-closed by EOD rule"
            )
            # V2-1修正: EOD強制クローズでも pnl 積算経路を維持（現在は0.0）
            closed = self.es_nq_spread.close_position("eod_force_close", pnl=0.0)
            eod_pnl = closed.get("pnl", 0.0) if closed else 0.0
            if eod_pnl != 0.0:
                self._today_realized_pnl  += eod_pnl
                self._month_realized_pnl  += eod_pnl
                self._weekly_realized_pnl += eod_pnl

        # 月初め（1日）は月次P&Lをリセット
        if today.day == 1:
            log.info("[MFFUBot] monthly reset: _month_realized_pnl = 0")
            self._month_realized_pnl = 0.0

        # 週初め（月曜）は週次P&Lをリセット
        if today.weekday() == 0:
            log.info("[MFFUBot] weekly reset: _weekly_realized_pnl = 0")
            self._weekly_realized_pnl = 0.0

    def _force_close(self):
        """15:45 ET: 全ポジションを強制クローズする。MFFUはEOD前に全決済が必要。"""
        log.info("[MFFUBot] force close at 15:45 ET (pre-EOD)")
        if not self.dry_run and self.client:
            results = self.client.close_all_positions()
            if results:
                log.info(f"[MFFUBot] force close: {results}")
                pushover("MFFU Bot: 強制クローズ", f"15:45 ET: {len(results)}件クローズ")
        self._force_close_done = True

    def _run_nightly(self):
        """
        16:30 ET: EOD確定チェック + 日次レポート。
        MFFUはEOD残高でルール判定するため、ここでcheck_eod()を実行する。
        """
        log.info("[MFFUBot] running nightly EOD check + report")

        balance, _ = self._get_current_balance_and_pnl()

        # MFFUのEODチェック（最重要）
        eod_result = self.rule_guard.check_eod(balance)
        if not eod_result["passed"]:
            log.error(f"[MFFUBot] EOD RULE VIOLATION: {eod_result['reasons']}")
            pushover(
                "MFFU Bot: EOD違反！",
                f"violations: {eod_result['reasons'][:1]}",
                priority=2,
            )
        else:
            log.info(f"[MFFUBot] EOD check passed: "
                     f"balance=${balance:,.0f} "
                     f"drawdown=${eod_result['eod_dd']['drawdown']:.0f} "
                     f"remaining=${eod_result['eod_dd']['remaining']:.0f}")

        today_pnl = eod_result["today_pnl"]
        self.rule_guard.update_pnl(today_pnl)

        # PnLをJSONファイルに記録
        pnl_file = _BASE_DIR / "mffu_pnl.json"
        pnl_data: dict = {}
        if pnl_file.exists():
            try:
                pnl_data = json.loads(pnl_file.read_text())
            except Exception:
                pnl_data = {"trades": []}

        if "trades" not in pnl_data:
            pnl_data["trades"] = []

        now_jst = datetime.datetime.now(JST)
        pnl_data["trades"].append({
            "event":      "exit",
            "date":       now_jst.strftime("%Y-%m-%d"),
            "pnl_usd":    round(today_pnl, 2),
            "eod_balance": round(balance, 2),
        })

        pnl_file.write_text(json.dumps(pnl_data, indent=2))
        log.info(f"[MFFUBot] nightly: today_pnl=${today_pnl:+,.2f} recorded")

        status = self.rule_guard.status_summary(balance)
        pushover("MFFU Bot: 日次レポート", f"P&L=${today_pnl:+,.0f}\n{status}")

        if PORTFOLIO_RISK_AVAILABLE:
            try:
                record_daily_pnl(
                    now_jst.strftime("%Y-%m-%d"),
                    today_pnl,
                    "mffu_bot",
                )
            except Exception as e:
                log.warning(f"[MFFUBot] portfolio_risk.record_daily_pnl: {e}")

        self._nightly_done = True


# ─────────────────────────────────────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MyFundedFutures Bot")
    parser.add_argument("--paper",        action="store_true", default=True,
                        help="Demo口座で動作（デフォルト）")
    parser.add_argument("--live",         action="store_true",
                        help="Live口座で動作（本番）")
    parser.add_argument("--dry-run",      action="store_true",
                        help="API接続なし・ロジックテスト")
    parser.add_argument("--account-size", type=int, default=DEFAULT_ACCOUNT_SIZE,
                        help=f"口座サイズ（デフォルト: {DEFAULT_ACCOUNT_SIZE}）")
    parser.add_argument("--product",      type=str, default=DEFAULT_PRODUCT,
                        help=f"先物製品コード（デフォルト: {DEFAULT_PRODUCT}）")
    parser.add_argument("--test-connect", action="store_true",
                        help="接続テストのみ実行して終了")
    args = parser.parse_args()

    paper = not args.live

    if args.test_connect:
        client = TradovateClient(env="DEMO" if paper else "LIVE")
        result = client.test_connection()
        print("\n=== Tradovate Connection Test (MFFU) ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return

    bot = MFFUBot(
        account_size = args.account_size,
        product      = args.product,
        paper        = paper,
        dry_run      = args.dry_run,
    )
    bot.run_forever()


if __name__ == "__main__":
    main()
# ── Chronos 命名統一エイリアス ────────────────────────────────────────────────
# ChronosBot は実装本体クラス（旧MFFUBot）の公式名称
# MFFUBot は後方互換 alias として保持（テスト・既存コードとの互換）
MFFUBot = ChronosBot  # noqa: E305  (A案統一: ChronosBot が正式名)


class ChronosClient:
    """先物ブローカー接続ラッパー。

    実ブローカー接続は TradovateClient が担う。
    このクラスは test_chronos_e2e.py との後方互換 + 将来ブローカー切替用の
    インターフェース層として保持する。
    """

    def __init__(self, paper: bool = True, dry_run: bool = False) -> None:
        self.paper = paper
        self.dry_run = dry_run
        self._connected = False

    def connect(self) -> bool:
        raise NotImplementedError(
            "ChronosClient.connect: TradovateClient を直接使用してください。"
        )

    def disconnect(self) -> None:
        raise NotImplementedError(
            "ChronosClient.disconnect: TradovateClient を直接使用してください。"
        )

    def get_account_info(self) -> dict:
        raise NotImplementedError("ChronosClient.get_account_info: 未実装")

    def get_quote(self, symbol: str) -> dict:
        raise NotImplementedError("ChronosClient.get_quote: 未実装")

    def place_order(self, symbol: str, side: str, qty: int,
                    order_type: str = "MARKET", limit_price: float | None = None) -> dict:
        raise NotImplementedError("ChronosClient.place_order: 未実装")

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("ChronosClient.cancel_order: 未実装")

    def get_positions(self) -> list:
        raise NotImplementedError("ChronosClient.get_positions: 未実装")


class ChronosStrategy:
    """環境適応型先物戦略エンジン（インターフェース層）。

    実戦術は chronos_strategy_selector.select_futures_strategy() が担う。
    """

    def __init__(self, rules: dict, client: ChronosClient) -> None:
        self.rules = rules
        self.client = client

    def select_tactic(self, market_data: dict) -> str:
        raise NotImplementedError("ChronosStrategy.select_tactic: バックテスト後に実装")

    def compute_entry(self, tactic: str, market_data: dict) -> dict | None:
        raise NotImplementedError("ChronosStrategy.compute_entry: バックテスト後に実装")

    def compute_exit(self, position: dict, market_data: dict) -> bool:
        raise NotImplementedError("ChronosStrategy.compute_exit: バックテスト後に実装")


def run(paper: bool = True, dry_run: bool = False, once: bool = False) -> None:
    """Chronosメインループ（ChronosBot.run_forever() のラッパー）。

    once=True は NotImplementedError（1サイクル実行は --dry-run を使用）。
    """
    if once:
        raise NotImplementedError(
            "run(once=True): 1サイクル実行は ChronosBot.run_forever() に統合済み。"
            " --dry-run フラグを使用してください。"
        )
    bot = ChronosBot(paper=paper, dry_run=dry_run)
    bot.run_forever()
