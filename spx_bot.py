#!/usr/bin/env python3
"""
SPX/SPY 0DTE/1DTE Credit Spread Bot
Full production implementation with dynamic sizing, tail hedge, event calendar
"""

import os
import sys
import csv
import json
import time
import logging
import datetime
import resource
import requests
import traceback
import zoneinfo
from pathlib import Path
from typing import Optional

# ── .env loader ───────────────────────────────────────────────────────────────
def _load_env_file():
    """Load /root/spxbot/.env into os.environ (if it exists)."""
    env_path = Path("/root/spxbot/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env_file()

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = Path(os.environ.get("SPX_LOG_DIR", "/var/log/spx_bot"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("spx_bot")

# ══════════════════════════════════════════════════════════════════════════════
# ── Strategy Parameters ──────────────────────────────────────────────────────
# All tunable parameters are defined here. Code body references these names only.
# Format: VARIABLE = value  # unit | meaning | basis (fixed/dynamic-candidate)
# ══════════════════════════════════════════════════════════════════════════════

# ── Underlying & spread geometry ─────────────────────────────────────────────
UNDERLYING     = "SPY"         # ticker symbol | ETF for C2 compatibility
SPREAD_WIDTH   = 5.0           # USD | spread width (sell-buy strikes) | fixed leg, review monthly
SELL_DELTA     = 0.20          # delta | target sell strike delta (~20 delta = ~20% ITM prob) | dynamic-candidate
HEDGE_DELTA    = 0.05          # delta | OTM put tail-hedge target delta | fixed
HEDGE_MAX_COST = 10.0          # USD/contract | max premium for tail hedge; skip if more expensive | fixed

# ── Entry & exit timing (ET) ─────────────────────────────────────────────────
ENTRY_TIMES    = [(10, 30), (14, 0)]  # list[(hour,min)] | ET entry windows | fixed
FORCE_CLOSE_H  = 15            # hour ET | force-close hour | fixed (15:50 ET)
FORCE_CLOSE_M  = 50            # minute  | force-close minute | fixed

# ── Profit / loss targets (legacy fixed; superseded by dynamic functions when enabled) ──
PROFIT_TARGET  = 0.50          # ratio | take-profit at 50% of collected credit | dynamic-candidate (VIX×time)
STOP_LOSS_MULT = 2.00          # multiple | stop-loss at 200% of collected credit | dynamic-candidate (VIX×time)

# ── VIX gate (legacy fixed; superseded by dynamic_vix_gate() when ENABLE_DYNAMIC_VIX_GATE=True) ──
VIX_GATE_FIXED = 25.0          # VIX level | no-trade above this VIX | dynamic-candidate (IVR-linked)

# ── SMA trend filter ──────────────────────────────────────────────────────────
SMA_PERIOD     = 20            # days | simple moving average period for trend direction | dynamic-candidate

# ── Kelly Criterion position sizing ──────────────────────────────────────────
KELLY_LOOKBACK_TRADES  = 20    # trades | past N closed trades used for Kelly calculation | dynamic-candidate
KELLY_FRACTION         = 0.50  # ratio  | half-Kelly multiplier (50% of full Kelly) | fixed convention
KELLY_MAX_FRACTION     = 0.25  # ratio  | safety cap: never risk more than 25% of account | fixed

# ── Consecutive-loss circuit breaker ─────────────────────────────────────────
CONSECUTIVE_LOSS_STOP  = 3     # count  | pause rest of day after N consecutive losses | dynamic-candidate (win-rate linked)
STARTUP_FAIL_LIMIT     = 3     # count  | Pushover alert after N consecutive OpenD connect failures | fixed

# ── Weekly / monthly drawdown rules ──────────────────────────────────────────
WEEKLY_LOSS_LIMIT      = 5     # count  | pause rest of week after N losses this week | dynamic-candidate (win-rate linked)
MONTHLY_DD_THRESHOLD   = 0.15  # ratio  | reduce size when monthly DD exceeds 15% | dynamic-candidate (fund-phase)
MONTHLY_DD_SIZE_MULT   = 0.50  # ratio  | size multiplier when monthly DD threshold breached | fixed

# ── Daily profit cap ─────────────────────────────────────────────────────────
DAILY_PROFIT_CAP_PCT   = 0.03  # ratio  | stop trading when daily P&L >= +3% of account | dynamic-candidate (fund-phase)

# ── IVR (Implied Volatility Rank) thresholds ─────────────────────────────────
IVR_SKIP_THRESHOLD     = 30    # %      | IVR below this → reduce size (legacy threshold; superseded by continuous function) | dynamic-candidate
IVR_FULL_THRESHOLD     = 50    # %      | IVR above this → full size (legacy threshold) | dynamic-candidate

# ── VIX spike exit ───────────────────────────────────────────────────────────
VIX_SPIKE_EXIT_PCT     = 0.15  # ratio  | exit all positions if VIX rises +15% from entry VIX | dynamic-candidate (ATR-based)

# ── ORB (Opening Range Breakout) ─────────────────────────────────────────────
ORB_WINDOW_MINUTES     = 30    # minutes | ORB recording window from 9:30 ET (9:30-10:00) | dynamic-candidate

# ── Expected Move validation ─────────────────────────────────────────────────
EM_SIZE_REDUCTION      = 0.50  # ratio  | size multiplier when sell strike is inside Expected Move | fixed

# ── Time-based dynamic stop tiers (legacy; superseded by dynamic_stop_loss_multiplier when ENABLE_DYNAMIC_STOP_LOSS=True) ──
DYNAMIC_STOP_TIERS = [
    # (hour, minute, new_multiplier) — checked in descending time order
    (15, 0, 1.00),   # 15:00 ET onwards: stop at 100% of credit
    (14, 0, 1.50),   # 14:00 ET onwards: stop at 150% of credit
]
DYNAMIC_STOP_DEFAULT   = 2.00  # multiple | before 14:00 ET: original 200% | fixed

# ── Trailing profit target ────────────────────────────────────────────────────
TRAILING_FIRST_TARGET  = 0.50  # ratio  | first partial close threshold (50% of credit) | dynamic-candidate
TRAILING_PARTIAL_RATIO = 0.50  # ratio  | close this fraction of position at first target | fixed
TRAILING_SECOND_TARGET = 0.75  # ratio  | full close threshold (75% of credit) | dynamic-candidate
TRAILING_GIVEBACK_RATIO = 0.30 # ratio  | close if profit retraces 30% from peak | fixed

# ── Volume spike detection ────────────────────────────────────────────────────
VOLUME_SPIKE_MULT         = 2.0  # multiple | spike = last-5min vol is Nx above 20d avg | dynamic-candidate
VOLUME_SPIKE_LOOKBACK_MIN = 5    # minutes  | lookback window for current vol measurement | fixed

# ── DIX / GEX (squeezemetrics) ───────────────────────────────────────────────
GEX_CSV_URL            = "https://squeezemetrics.com/monitor/static/DIX.csv"
GEX_CACHE_FILE         = Path(os.environ.get("SPX_DATA_DIR", "/tmp")) / "gex_dix_cache.json"
GEX_NEGATIVE_SKIP      = True   # bool   | skip trade when GEX is negative | fixed
DIX_BULL_THRESHOLD     = 0.45   # ratio  | DIX > 45% → institutional buying → bull put favored | dynamic-candidate
DIX_BEAR_THRESHOLD     = 0.40   # ratio  | DIX < 40% → institutional selling → bear call favored | dynamic-candidate

# ── Market breadth (RSP/SPY ratio) ───────────────────────────────────────────
BREADTH_LOOKBACK_DAYS     = 5   # days   | comparison window for RSP/SPY divergence | fixed
BREADTH_DIVERGE_THRESHOLD = 0.01 # ratio | RSP/SPY ratio drop > 1% while SPY up = danger | dynamic-candidate

# ── Dynamic calc engine internal parameters ───────────────────────────────────
# dynamic_vix_spike_exit_pct()
VIX_SPIKE_ATR_COEFF    = 1.5   # multiple | spike = VIX_ATR × this coefficient | dynamic-candidate
VIX_SPIKE_FLOOR        = 0.05  # ratio    | minimum spike threshold (never ignore +5% VIX move) | fixed
VIX_SPIKE_CAP          = 0.25  # ratio    | maximum spike threshold (never wait for +25%) | fixed

# dynamic_stop_loss_multiplier()
DYNSTOP_BASE           = 1.5   # multiple | center of stop range (1.0–2.0); anchored to VIX=20, 6.5h session | fixed
DYNSTOP_VIX_BASELINE   = 20.0  # VIX pts  | VIX normalization baseline (VIX=20 → vix_factor=1.0) | fixed
DYNSTOP_SESSION_HOURS  = 6.5   # hours    | full trading session duration for time_factor normalization | fixed
DYNSTOP_FLOOR          = 0.80  # multiple | minimum stop (never risk < 80% of credit) | fixed
DYNSTOP_CAP            = 3.00  # multiple | maximum stop (never risk > 300% of credit) | fixed

# dynamic_profit_target()
DYNPROFIT_BASE         = 0.50  # ratio    | profit target base (calibrated to VIX=20, full day) | fixed
DYNPROFIT_VIX_BASELINE = 20.0  # VIX pts  | same normalization baseline as stop | fixed
DYNPROFIT_FLOOR        = 0.25  # ratio    | minimum target (always take at least 25% profit) | fixed
DYNPROFIT_CAP          = 0.75  # ratio    | maximum target (never wait beyond 75%) | fixed

# dynamic_afternoon_size()
DYNSIZE_DECAY_K        = 0.0033 # 1/min   | exponential decay rate; calibrated: 210min after first hour → 0.50 | fixed
DYNSIZE_FLOOR          = 0.15  # ratio    | minimum size after afternoon decay | fixed

# dynamic_weekly_loss_limit()
DYNWEEKLY_BASE         = 5.0   # count    | base limit calibrated to win_rate=65% | fixed
DYNWEEKLY_WIN_CALIB    = 0.65  # ratio    | win rate at which base limit applies | fixed
DYNWEEKLY_FLOOR        = 2     # count    | never stop after fewer than 2 losses | fixed
DYNWEEKLY_CAP          = 8     # count    | never allow more than 8 weekly losses | fixed

# dynamic_consecutive_loss_stop()
DYNCS_FLOOR            = 2     # count    | minimum consecutive loss stop | fixed
DYNCS_CAP              = 6     # count    | maximum consecutive loss stop | fixed

# _calc_recent_win_rate()
WIN_RATE_LOOKBACK      = 30    # trades   | recent trade window for win-rate calculation | fixed
WIN_RATE_MIN_TRADES    = 10    # trades   | minimum trades required before win-rate is used | fixed

# Fund phase boundaries (get_risk_params)
PHASE1_CAP_USD         = 20_000  # USD   | ~300万円 at ¥150/$; Phase1→Phase2 transition | dynamic-candidate
PHASE2_CAP_USD         = 67_000  # USD   | ~1000万円 at ¥150/$; Phase2→Phase3 transition | dynamic-candidate

# Startup margin threshold
MARGIN_THRESHOLD_JPY   = 500_000  # JPY  | halt entries if account < ¥500,000 | fixed

# ── Position sizing ratios (calc_position_size) ───────────────────────────────
SIZE_RATIO_BASE        = 0.20  # ratio | base capital allocation per trade | dynamic-candidate
SIZE_RATIO_VIX_SPIKE   = 0.40  # ratio | allocation when VIX spikes +20% from prev close | dynamic-candidate
SIZE_RATIO_OPEX        = 0.30  # ratio | allocation on OpEx / no-trade event days | fixed
SIZE_RATIO_MON_FRI     = 0.25  # ratio | allocation on Monday and Friday (more volatile) | fixed
SIZE_VIX_SPIKE_PCT     = 1.20  # multiple | VIX spike trigger: today >= prev × this value | fixed
SIZE_SEASONAL_SEP_OCT  = 0.50  # multiple | seasonal size multiplier for Sep/Oct | fixed
SIZE_SEASONAL_JUL_NOV  = 1.50  # multiple | seasonal size multiplier for Jul/Nov | fixed

# ── Constants (infrastructure; not tunable strategy params) ──────────────────
ET = zoneinfo.ZoneInfo("America/New_York")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")
EVENTS_FILE          = Path("/root/events.json")
FAILURES_FILE        = Path("/root/spx_bot_failures.json")
TRADE_LOCK_FILE      = Path("/root/trade_lock.json")
MONTHLY_CSV_DIR      = Path("/var/log/spx_bot")
PNL_FILE             = Path("/root/spxbot/pnl.json")
MEMORY_WARN_FILE     = Path("/root/spxbot/memory_warn.json")
RECOVERY_COUNT_FILE  = Path("/root/spxbot/recovery_count.json")
REPORTS_DIR          = Path("/root/spxbot/reports")
MEMORY_WARN_MB       = 450  # Legacy: process RSS threshold (MB)
MEMORY_WARN_PCT      = 80   # System-wide memory usage % threshold
TRADE_PASSWORD  = os.environ.get("TRADE_PASSWORD", "")
OPEND_HOST     = "127.0.0.1"
OPEND_PORT     = 11111

# ── Feature flags (ON/OFF toggles for new improvements) ─────────────────────
ENABLE_IVR_FILTER       = True   # #1: IVR filter — skip/reduce size when IV is cheap
ENABLE_VIX_TERM_STRUCT  = True   # #2: VIX Term Structure — reduce size in backwardation
ENABLE_AFTERNOON_SIZE   = True   # #3: 14:00 ET entry size limited to 50% of base
ENABLE_VIX_SPIKE_EXIT   = True   # #4: Exit when VIX rises +15% from entry level

# #5: Kelly Criterion position sizing
ENABLE_KELLY_SIZING       = True

# #6: Weekly/monthly DD-based pause rules
ENABLE_EXTENDED_DD_RULES  = True

# #7: Daily profit cap
ENABLE_DAILY_PROFIT_CAP   = True

# #8: ORB (Opening Range Breakout) alignment filter
ENABLE_ORB_FILTER         = True

# #9: Expected Move strike validation
ENABLE_EXPECTED_MOVE      = True

# #10: Time-based dynamic stop adjustment
ENABLE_DYNAMIC_STOP       = True

# #11: Trailing profit target (partial close)
ENABLE_TRAILING_PROFIT    = True

# #12: Volume spike detection
ENABLE_VOLUME_SPIKE       = True

# #13: GEX (Gamma Exposure) filter via squeezemetrics
ENABLE_GEX_FILTER         = True

# #14: DIX / Put-Call Ratio
ENABLE_DIX_PCR            = True

# #15: VWAP direction filter
ENABLE_VWAP_DIRECTION     = True

# #16: Market breadth divergence (RSP/SPY ratio)
ENABLE_MARKET_BREADTH     = True

# ══════════════════════════════════════════════════════════════════════════════
# Dynamic Parameter System — Feature Flags
# When ENABLE_DYNAMIC_* is True, the corresponding parameter is computed
# from market environment instead of using the fixed constant above.
# ══════════════════════════════════════════════════════════════════════════════
ENABLE_DYNAMIC_IVR_SIZING      = True   # IVR → continuous sizing function (replaces IVR_SKIP/FULL thresholds)
ENABLE_DYNAMIC_VIX_SPIKE_EXIT  = True   # ATR-based VIX spike exit (replaces VIX_SPIKE_EXIT_PCT)
ENABLE_DYNAMIC_STOP            = True   # VIX × time-to-expiry stop (replaces DYNAMIC_STOP_TIERS)
ENABLE_DYNAMIC_AFTERNOON_SIZE  = True   # Continuous time decay function (replaces 50% flat)
ENABLE_DYNAMIC_WEEKLY_LOSS     = True   # Win-rate-based weekly loss limit (replaces WEEKLY_LOSS_LIMIT=5)
ENABLE_DYNAMIC_MONTHLY_DD      = True   # Fund-phase-based monthly DD (replaces MONTHLY_DD_THRESHOLD=15%)
ENABLE_DYNAMIC_DAILY_CAP       = True   # Fund-phase-based daily profit cap (replaces DAILY_PROFIT_CAP_PCT=3%)
ENABLE_DYNAMIC_VIX_GATE        = True   # IVR-linked VIX gate (replaces fixed 25)
ENABLE_DYNAMIC_PROFIT_TARGET   = True   # VIX × time dynamic profit target (replaces fixed 50%)
ENABLE_DYNAMIC_STOP_LOSS       = True   # VIX × time dynamic stop loss (replaces fixed 200%)
ENABLE_DYNAMIC_CONSEC_STOP     = True   # Win-rate-linked consecutive loss stop (replaces fixed 3)
ENABLE_PREMARKET_ASSESSMENT    = True   # Daily premarket environment scoring

# US market holidays 2025–2027 (NYSE)
US_HOLIDAYS = {
    # 2025
    datetime.date(2025, 1, 1),   # New Year's Day
    datetime.date(2025, 1, 20),  # MLK Day
    datetime.date(2025, 2, 17),  # Presidents' Day
    datetime.date(2025, 4, 18),  # Good Friday
    datetime.date(2025, 5, 26),  # Memorial Day
    datetime.date(2025, 6, 19),  # Juneteenth
    datetime.date(2025, 7, 4),   # Independence Day
    datetime.date(2025, 9, 1),   # Labor Day
    datetime.date(2025, 11, 27), # Thanksgiving
    datetime.date(2025, 12, 25), # Christmas
    # 2026
    datetime.date(2026, 1, 1),   # New Year's Day
    datetime.date(2026, 1, 19),  # MLK Day
    datetime.date(2026, 2, 16),  # Presidents' Day
    datetime.date(2026, 4, 3),   # Good Friday
    datetime.date(2026, 5, 25),  # Memorial Day
    datetime.date(2026, 6, 19),  # Juneteenth
    datetime.date(2026, 7, 3),   # Independence Day (observed)
    datetime.date(2026, 9, 7),   # Labor Day
    datetime.date(2026, 11, 26), # Thanksgiving
    datetime.date(2026, 11, 27), # Day after Thanksgiving (early close, skip)
    datetime.date(2026, 12, 25), # Christmas
    # 2027
    datetime.date(2027, 1, 1),   # New Year's Day
    datetime.date(2027, 1, 18),  # MLK Day
    datetime.date(2027, 2, 15),  # Presidents' Day
    datetime.date(2027, 3, 26),  # Good Friday
    datetime.date(2027, 5, 31),  # Memorial Day
    datetime.date(2027, 6, 18),  # Juneteenth (observed)
    datetime.date(2027, 7, 5),   # Independence Day (observed)
    datetime.date(2027, 9, 6),   # Labor Day
    datetime.date(2027, 11, 25), # Thanksgiving
    datetime.date(2027, 12, 24), # Christmas (observed)
}
# Keep legacy alias for backward compat
US_HOLIDAYS_2026 = US_HOLIDAYS

# No-trade event keywords
NOTRADE_KEYWORDS = ["fomc", "cpi", "nfp", "non-farm", "opex", "quadruple",
                    "pce", "gdp", "jobless", "claims"]

# ── Pushover ──────────────────────────────────────────────────────────────────
def pushover(title: str, message: str, priority: int = 0) -> bool:
    """Send Pushover notification. Only called for critical alerts."""
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   PUSHOVER_TOKEN,
                "user":    PUSHOVER_USER,
                "title":   title,
                "message": message,
                "priority": priority,
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Pushover failed: {e}")
        return False

# ── Failure counter (persists across restarts) ────────────────────────────────
def load_failures() -> int:
    try:
        if FAILURES_FILE.exists():
            data = json.loads(FAILURES_FILE.read_text())
            # Reset counter if last failure was >24h ago
            last = datetime.datetime.fromisoformat(data.get("last", "2000-01-01T00:00:00"))
            if (datetime.datetime.now(ET) - last.replace(tzinfo=ET)).total_seconds() > 86400:
                return 0
            return int(data.get("count", 0))
    except Exception:
        pass
    return 0

def save_failures(count: int):
    try:
        FAILURES_FILE.write_text(json.dumps({
            "count": count,
            "last": datetime.datetime.now(ET).isoformat()
        }))
    except Exception as e:
        log.warning(f"Could not save failure count: {e}")

# ── Monthly CSV trade log ─────────────────────────────────────────────────────
def append_monthly_csv(record: dict):
    """Append a trade record to /var/log/spx_bot/YYYY-MM.csv"""
    try:
        MONTHLY_CSV_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(ET)
        csv_path = MONTHLY_CSV_DIR / f"{now.strftime('%Y-%m')}.csv"
        fieldnames = ["timestamp", "direction", "sell_strike", "buy_strike",
                      "qty", "net_credit", "result", "pnl"]
        write_header = not csv_path.exists()
        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            record.setdefault("timestamp", now.isoformat())
            writer.writerow(record)
        log.info(f"Monthly CSV updated: {csv_path}")
    except Exception as e:
        log.warning(f"monthly CSV write failed: {e}")

# ── pnl.json management ───────────────────────────────────────────────────────
def load_pnl() -> list:
    """Load pnl.json, return list of trade records."""
    try:
        if PNL_FILE.exists():
            return json.loads(PNL_FILE.read_text()).get("trades", [])
    except Exception:
        pass
    return []

def append_pnl_entry(record: dict):
    """Append trade record to pnl.json (entries + exits)."""
    try:
        PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
        trades = load_pnl()
        record.setdefault("date", datetime.datetime.now(ET).strftime("%Y-%m-%d"))
        record.setdefault("ts", datetime.datetime.now(ET).isoformat())
        trades.append(record)
        PNL_FILE.write_text(json.dumps({"trades": trades}, indent=2))
    except Exception as e:
        log.warning(f"pnl.json write failed: {e}")

# ── #5 Kelly Criterion position sizing ───────────────────────────────────────
def calc_kelly_fraction() -> Optional[float]:
    """Calculate Half-Kelly fraction from past KELLY_LOOKBACK_TRADES closed trades.
    Returns None if insufficient data (< KELLY_LOOKBACK_TRADES trades).
    Kelly % = (W * R - L) / R, then multiply by KELLY_FRACTION (default 0.50).
    W = win rate, L = loss rate (1 - W), R = avg_win / avg_loss (payoff ratio).
    """
    if not ENABLE_KELLY_SIZING:
        return None
    trades = load_pnl()
    exits = [t for t in trades if t.get("event") == "exit" and t.get("pnl_usd") is not None]
    if len(exits) < KELLY_LOOKBACK_TRADES:
        log.info(f"Kelly: insufficient data ({len(exits)}/{KELLY_LOOKBACK_TRADES} trades) → default sizing")
        return None
    recent = exits[-KELLY_LOOKBACK_TRADES:]
    wins = [t for t in recent if (t.get("pnl_usd") or 0) > 0]
    losses = [t for t in recent if (t.get("pnl_usd") or 0) <= 0]
    if not wins or not losses:
        log.info(f"Kelly: all wins or all losses in last {KELLY_LOOKBACK_TRADES} trades → skip")
        return None
    W = len(wins) / len(recent)
    L = 1.0 - W
    avg_win = sum(t["pnl_usd"] for t in wins) / len(wins)
    avg_loss = abs(sum(t["pnl_usd"] for t in losses) / len(losses))
    if avg_loss == 0:
        return None
    R = avg_win / avg_loss
    kelly_full = (W * R - L) / R
    if kelly_full <= 0:
        log.info(f"Kelly: negative edge ({kelly_full:.3f}) → minimum size")
        return 0.0
    half_kelly = kelly_full * KELLY_FRACTION
    half_kelly = min(half_kelly, KELLY_MAX_FRACTION)  # safety cap
    log.info(f"Kelly: W={W:.2f} R={R:.2f} full={kelly_full:.3f} half={half_kelly:.3f}")
    return half_kelly


# ── #6 Weekly/monthly DD-based pause rules ───────────────────────────────────
def check_weekly_loss_limit() -> bool:
    """Return True if weekly losses >= WEEKLY_LOSS_LIMIT → should pause rest of week."""
    if not ENABLE_EXTENDED_DD_RULES:
        return False
    now = datetime.datetime.now(ET)
    monday = (now - datetime.timedelta(days=now.weekday())).date()
    monday_str = monday.strftime("%Y-%m-%d")
    trades = load_pnl()
    weekly_losses = [t for t in trades
                     if t.get("event") == "exit"
                     and t.get("date", "") >= monday_str
                     and (t.get("pnl_usd") or 0) <= 0]
    # Dynamic weekly loss limit from win rate
    if ENABLE_DYNAMIC_WEEKLY_LOSS:
        win_rate, num_trades_wr = _calc_recent_win_rate()
        limit = dynamic_weekly_loss_limit(win_rate, num_trades_wr)
    else:
        limit = WEEKLY_LOSS_LIMIT
    if len(weekly_losses) >= limit:
        log.warning(f"Weekly loss limit: {len(weekly_losses)} losses >= {limit} → pause")
        return True
    return False


def check_monthly_dd_size_multiplier(account_cash: float) -> float:
    """Return size multiplier based on monthly DD. 1.0 = normal, <1.0 = reduced."""
    if not ENABLE_EXTENDED_DD_RULES:
        return 1.0
    now = datetime.datetime.now(ET)
    month_start_str = now.date().replace(day=1).strftime("%Y-%m-%d")
    trades = load_pnl()
    monthly_exits = [t for t in trades
                     if t.get("event") == "exit"
                     and t.get("date", "") >= month_start_str]
    monthly_pnl = sum(t.get("pnl_usd", 0) or 0 for t in monthly_exits)
    if account_cash <= 0:
        return 1.0
    dd_pct = abs(monthly_pnl) / account_cash if monthly_pnl < 0 else 0.0
    # Dynamic monthly DD threshold from fund phase
    if ENABLE_DYNAMIC_MONTHLY_DD:
        fund_params = get_risk_params(account_cash)
        threshold = fund_params["monthly_dd_limit"]
    else:
        threshold = MONTHLY_DD_THRESHOLD
    if dd_pct > threshold:
        log.warning(f"Monthly DD {dd_pct:.1%} > {threshold:.0%} → size x{MONTHLY_DD_SIZE_MULT}")
        return MONTHLY_DD_SIZE_MULT
    return 1.0


# ── #7 Daily profit cap ─────────────────────────────────────────────────────
def check_daily_profit_cap(account_cash: float) -> bool:
    """Return True if today's profit >= DAILY_PROFIT_CAP_PCT of account → stop trading.
    Abnormally high profit days = market instability → risk avoidance."""
    if not ENABLE_DAILY_PROFIT_CAP:
        return False
    now = datetime.datetime.now(ET)
    today_str = now.strftime("%Y-%m-%d")
    trades = load_pnl()
    today_exits = [t for t in trades
                   if t.get("event") == "exit"
                   and t.get("date", "") == today_str]
    today_pnl = sum(t.get("pnl_usd", 0) or 0 for t in today_exits)
    if account_cash <= 0:
        return False
    profit_pct = today_pnl / account_cash
    # Dynamic daily profit cap from fund phase
    if ENABLE_DYNAMIC_DAILY_CAP:
        fund_params = get_risk_params(account_cash)
        cap_pct = fund_params["daily_profit_cap"]
    else:
        cap_pct = DAILY_PROFIT_CAP_PCT
    if profit_pct >= cap_pct:
        log.info(f"Daily profit cap: ${today_pnl:+.0f} ({profit_pct:.1%} >= {cap_pct:.0%}) → stop")
        return True
    return False


# ── Memory monitor ────────────────────────────────────────────────────────────
def check_memory_usage():
    """Check system-wide memory usage %. If >MEMORY_WARN_PCT, flag for morning summary (no immediate push)."""
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f.readlines():
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        if total == 0:
            return
        used_pct = (total - available) / total * 100
        if used_pct > MEMORY_WARN_PCT:
            log.warning(f"System memory high: {used_pct:.1f}% > {MEMORY_WARN_PCT}%")
            try:
                MEMORY_WARN_FILE.parent.mkdir(parents=True, exist_ok=True)
                data = {}
                if MEMORY_WARN_FILE.exists():
                    data = json.loads(MEMORY_WARN_FILE.read_text())
                data["count"] = data.get("count", 0) + 1
                data["max_pct"] = max(float(data.get("max_pct", 0)), used_pct)
                data["last"] = datetime.datetime.now(ET).isoformat()
                MEMORY_WARN_FILE.write_text(json.dumps(data))
            except Exception as e:
                log.warning(f"Memory warn flag write failed: {e}")
    except FileNotFoundError:
        # /proc/meminfo not available (macOS dev): fall back to RSS check
        try:
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = rss_kb / (1024 * 1024) if sys.platform == "darwin" else rss_kb / 1024
            if rss_mb > MEMORY_WARN_MB:
                log.warning(f"Process RSS high: {rss_mb:.0f}MB > {MEMORY_WARN_MB}MB")
        except Exception:
            pass
    except Exception as e:
        log.warning(f"Memory check failed: {e}")

# ── Event calendar ────────────────────────────────────────────────────────────
def fetch_events_weekly():
    """Monday: fetch investing.com economic calendar → events.json"""
    now = datetime.datetime.now(ET)
    if now.weekday() != 0:
        return
    if EVENTS_FILE.exists():
        age = now.timestamp() - EVENTS_FILE.stat().st_mtime
        if age < 86400 * 6:
            return
    log.info("Fetching weekly event calendar...")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.investing.com/economic-calendar/",
        }
        week_start = now.date()
        week_end   = week_start + datetime.timedelta(days=6)
        payload = {
            "country[]": "5",  # USA
            "importance[]": ["2", "3"],
            "dateFrom": week_start.strftime("%Y-%m-%d"),
            "dateTo":   week_end.strftime("%Y-%m-%d"),
        }
        r = requests.post(
            "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData",
            headers=headers, data=payload, timeout=15,
        )
        data = r.json()
        events = []
        for row in data.get("data", "").split("<tr"):
            for kw in NOTRADE_KEYWORDS:
                if kw in row.lower():
                    events.append({"keyword": kw, "raw": row[:80]})
                    break
        EVENTS_FILE.write_text(json.dumps({"fetched": str(now.date()), "events": events}))
        log.info(f"Events saved: {len(events)} high-impact events this week")
    except Exception as e:
        log.warning(f"Event fetch failed (non-fatal): {e}")

def is_notrade_today() -> bool:
    """Check if today has FOMC/CPI/NFP/OpEx event or next day is holiday."""
    today = datetime.datetime.now(ET).date()
    tomorrow = today + datetime.timedelta(days=1)

    # No-trade if next trading day is a market holiday
    if tomorrow in US_HOLIDAYS:
        log.info(f"Next day ({tomorrow}) is a market holiday → no trade")
        return True

    # Quarterly OpEx: 3rd Friday of Mar/Jun/Sep/Dec
    if today.month in (3, 6, 9, 12) and today.weekday() == 4:
        first_day = today.replace(day=1)
        first_friday = first_day + datetime.timedelta(days=(4 - first_day.weekday()) % 7)
        third_friday = first_friday + datetime.timedelta(weeks=2)
        if today == third_friday:
            log.info("Quarterly OpEx → no trade")
            return True

    if not EVENTS_FILE.exists():
        return False
    try:
        data = json.loads(EVENTS_FILE.read_text())
        fetched = datetime.date.fromisoformat(data["fetched"])
        if (today - fetched).days > 7:
            return False
        for ev in data.get("events", []):
            log.info(f"No-trade event detected: {ev['keyword']}")
            return True
    except Exception:
        pass
    return False


# ── Improvement #8: ORB (Opening Range Breakout) alignment filter ────────────
# Records 9:30-10:00 ET high/low, checks if breakout direction matches SMA trend.
# Usage: call check_orb_alignment() before entry at 10:30 ET.
# TODO: integrate into SPXBot.run_entry() before placing the spread.

def check_orb_alignment(quote_ctx, sma_direction: str, orb_state: dict) -> dict:
    """
    Check Opening Range Breakout alignment with SMA direction.

    Args:
        quote_ctx: MarketData instance (must have get_spy_price())
        sma_direction: "bull" if SPY > SMA20, "bear" if SPY < SMA20
        orb_state: mutable dict tracking ORB state across calls, keys:
            - "high": float, OR high (updated during 9:30-10:00)
            - "low": float, OR low (updated during 9:30-10:00)
            - "recording": bool, True while inside the OR window
            - "finalized": bool, True once OR window is complete
            - "breakout_dir": str or None, "bull"/"bear"/None after finalization

    Returns:
        dict with:
            - "aligned": bool -- True if ORB direction matches SMA or not yet finalized
            - "orb_high": float
            - "orb_low": float
            - "breakout_dir": str or None
            - "reason": str -- human-readable explanation
    """
    if not ENABLE_ORB_FILTER:
        return {"aligned": True, "orb_high": 0, "orb_low": 0,
                "breakout_dir": None, "reason": "ORB filter disabled"}

    now = datetime.datetime.now(ET)
    h, m = now.hour, now.minute

    # Phase 1: Recording window (9:30 to 9:30+ORB_WINDOW_MINUTES ET)
    or_start_h, or_start_m = 9, 30
    total_end_min = or_start_h * 60 + or_start_m + ORB_WINDOW_MINUTES
    or_end_h = total_end_min // 60
    or_end_m = total_end_min % 60

    current_total_min = h * 60 + m
    or_start_total = or_start_h * 60 + or_start_m
    or_end_total = or_end_h * 60 + or_end_m

    in_or_window = or_start_total <= current_total_min < or_end_total

    if in_or_window:
        price = quote_ctx.get_spy_price()
        if price is not None:
            if "high" not in orb_state or price > orb_state["high"]:
                orb_state["high"] = price
            if "low" not in orb_state or price < orb_state["low"]:
                orb_state["low"] = price
            orb_state["recording"] = True
            orb_state["finalized"] = False
        log.info(f"ORB recording: high={orb_state.get('high', 0):.2f} "
                 f"low={orb_state.get('low', 0):.2f}")
        return {"aligned": True, "orb_high": orb_state.get("high", 0),
                "orb_low": orb_state.get("low", 0),
                "breakout_dir": None, "reason": "ORB still recording (9:30-10:00)"}

    # Phase 2: Finalize OR range and determine breakout direction
    if not orb_state.get("finalized") and orb_state.get("recording"):
        orb_state["finalized"] = True
        orb_state["recording"] = False
        log.info(f"ORB finalized: high={orb_state.get('high', 0):.2f} "
                 f"low={orb_state.get('low', 0):.2f}")

    orb_high = orb_state.get("high", 0)
    orb_low = orb_state.get("low", 0)

    if orb_high == 0 or orb_low == 0:
        return {"aligned": True, "orb_high": orb_high, "orb_low": orb_low,
                "breakout_dir": None, "reason": "ORB data not available"}

    # Determine breakout direction from current price vs OR range
    price = quote_ctx.get_spy_price()
    if price is None:
        return {"aligned": True, "orb_high": orb_high, "orb_low": orb_low,
                "breakout_dir": None, "reason": "Price unavailable for ORB check"}

    breakout_dir = None
    if price > orb_high:
        breakout_dir = "bull"
    elif price < orb_low:
        breakout_dir = "bear"
    # else: price is inside OR range -- no clear breakout

    orb_state["breakout_dir"] = breakout_dir

    if breakout_dir is None:
        return {"aligned": True, "orb_high": orb_high, "orb_low": orb_low,
                "breakout_dir": None,
                "reason": f"Price {price:.2f} inside OR [{orb_low:.2f}-{orb_high:.2f}], no breakout"}

    aligned = (breakout_dir == sma_direction)
    reason = (f"ORB breakout={breakout_dir} SMA={sma_direction} "
              f"{'ALIGNED' if aligned else 'CONFLICT'} "
              f"(price={price:.2f} OR=[{orb_low:.2f}-{orb_high:.2f}])")
    if not aligned:
        log.warning(f"ORB filter: {reason} -> skip entry")
    else:
        log.info(f"ORB filter: {reason} -> entry OK")

    return {"aligned": aligned, "orb_high": orb_high, "orb_low": orb_low,
            "breakout_dir": breakout_dir, "reason": reason}


# ── Improvement #9: Expected Move validation ────────────────────────────────
# Calculates EM from ATM straddle price, verifies sell strike is outside EM.
# If sell strike is inside EM, log warning and recommend size reduction.
# TODO: integrate into SPXBot.run_entry() after finding sell_strike.

def check_expected_move(quote_ctx, spy_price: float, sell_strike: float,
                        direction: str, option_chain: list) -> dict:
    """
    Validate that the sell strike is outside the Expected Move range.

    EM = ATM Straddle Price (sum of ATM option mids for the given chain side).
    For a full straddle, both call and put chains would be needed; here we
    estimate using 2x the ATM option mid from the available chain side,
    which approximates when call/put ATM prices are similar.

    EM defines the market's expected range: [SPY - EM, SPY + EM].
    Sell strikes inside this range have higher probability of being breached.

    Args:
        quote_ctx: MarketData instance
        spy_price: current SPY price
        sell_strike: the chosen sell strike price
        direction: "bull_put" or "bear_call"
        option_chain: list of option dicts from get_option_chain()

    Returns:
        dict with:
            - "outside_em": bool -- True if sell strike is safely outside EM
            - "em_value": float -- the Expected Move in dollar terms
            - "em_upper": float -- SPY + EM
            - "em_lower": float -- SPY - EM
            - "size_multiplier": float -- 1.0 if OK, EM_SIZE_REDUCTION if inside
            - "reason": str
    """
    if not ENABLE_EXPECTED_MOVE:
        return {"outside_em": True, "em_value": 0, "em_upper": 0, "em_lower": 0,
                "size_multiplier": 1.0, "reason": "Expected Move check disabled"}

    if spy_price <= 0 or not option_chain:
        return {"outside_em": True, "em_value": 0, "em_upper": 0, "em_lower": 0,
                "size_multiplier": 1.0, "reason": "Insufficient data for EM calculation"}

    # Find ATM strike (closest to current SPY price)
    all_strikes = set(o.get("strike_price", 0) for o in option_chain if o.get("strike_price", 0) > 0)
    if not all_strikes:
        return {"outside_em": True, "em_value": 0, "em_upper": 0, "em_lower": 0,
                "size_multiplier": 1.0, "reason": "No valid strikes in chain"}

    atm_strike = min(all_strikes, key=lambda s: abs(s - spy_price))

    # Get ATM option price (mid of bid/ask)
    atm_options = [o for o in option_chain
                   if abs(o.get("strike_price", 0) - atm_strike) < 0.5]

    atm_mid_sum = 0.0
    for opt in atm_options:
        bid = opt.get("bid_price", opt.get("last_price", 0)) or 0
        ask = opt.get("ask_price", opt.get("last_price", 0)) or 0
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (opt.get("last_price", 0) or 0)
        atm_mid_sum += mid

    # We only have one side of the chain (puts or calls).
    # Approximate straddle as 2x single-side ATM mid.
    if atm_mid_sum > 0 and len(atm_options) == 1:
        em = atm_mid_sum * 2.0
    elif atm_mid_sum > 0:
        em = atm_mid_sum  # multiple ATM options found, use sum directly
    else:
        return {"outside_em": True, "em_value": 0, "em_upper": spy_price,
                "em_lower": spy_price, "size_multiplier": 1.0,
                "reason": "EM calculation returned 0 (ATM price unavailable)"}

    em_upper = spy_price + em
    em_lower = spy_price - em

    # Check if sell strike is outside EM
    if direction == "bull_put":
        outside = sell_strike < em_lower
        distance = em_lower - sell_strike
    else:  # bear_call
        outside = sell_strike > em_upper
        distance = sell_strike - em_upper

    size_mult = 1.0
    if not outside:
        size_mult = EM_SIZE_REDUCTION
        log.warning(
            f"EM WARNING: sell strike {sell_strike:.0f} is INSIDE Expected Move "
            f"[{em_lower:.2f}-{em_upper:.2f}] (EM={em:.2f}, SPY={spy_price:.2f}) "
            f"-> size reduced to {EM_SIZE_REDUCTION:.0%}"
        )
    else:
        log.info(
            f"EM OK: sell strike {sell_strike:.0f} is {abs(distance):.2f} outside EM "
            f"[{em_lower:.2f}-{em_upper:.2f}] (EM={em:.2f})"
        )

    return {
        "outside_em": outside,
        "em_value": round(em, 2),
        "em_upper": round(em_upper, 2),
        "em_lower": round(em_lower, 2),
        "size_multiplier": size_mult,
        "reason": f"EM={em:.2f} [{em_lower:.2f}-{em_upper:.2f}] "
                  f"strike={sell_strike:.0f} {'OUTSIDE' if outside else 'INSIDE'}"
    }


# ── Improvement #10: Time-based dynamic stop adjustment ─────────────────────
# As expiration approaches, gamma risk increases. Tighten stops accordingly.
# 14:00 ET+: 200% -> 150%, 15:00 ET+: 150% -> 100%.
# TODO: integrate into SPXBot.check_exits() replacing STOP_LOSS_MULT with
#       get_dynamic_stop_multiplier().

def get_dynamic_stop_multiplier() -> float:
    """
    Return the stop-loss multiplier based on current ET time.
    Later in the day = tighter stop to account for increasing gamma risk in 0DTE.

    Returns:
        float: stop-loss multiplier (e.g., 2.0, 1.5, 1.0)
    """
    if not ENABLE_DYNAMIC_STOP:
        return STOP_LOSS_MULT  # original fixed value

    now = datetime.datetime.now(ET)
    h, m = now.hour, now.minute

    # Check tiers in descending time order (highest time first in list)
    for tier_h, tier_m, multiplier in DYNAMIC_STOP_TIERS:
        if h > tier_h or (h == tier_h and m >= tier_m):
            log.info(f"Dynamic stop: {h}:{m:02d} ET >= {tier_h}:{tier_m:02d} "
                     f"-> stop multiplier {multiplier:.2f}x (was {STOP_LOSS_MULT:.2f}x)")
            return multiplier

    log.info(f"Dynamic stop: {h}:{m:02d} ET -> default {DYNAMIC_STOP_DEFAULT:.2f}x")
    return DYNAMIC_STOP_DEFAULT


# ── Improvement #11: Trailing Profit Target ─────────────────────────────────
# After hitting 50% profit, close half. Trail the rest to 75% or giveback limit.
# State is tracked per-position in a dict passed by the caller.
# TODO: integrate into SPXBot.check_exits() replacing fixed PROFIT_TARGET logic.

def check_trailing_profit(pl_ratio: float, trailing_state: dict) -> dict:
    """
    Determine trailing profit action based on current P&L ratio.

    Workflow:
    1. pl_ratio >= 50% -> close half (TRAILING_PARTIAL_RATIO), start trailing
    2. While trailing:
       - pl_ratio >= 75% -> close remainder (full profit)
       - pl_ratio drops below (peak - peak * 30%) -> close remainder (giveback)
    3. Before 50% -> hold (no action)

    Args:
        pl_ratio: current profit as ratio of credit (e.g., 0.5 = 50% profit)
        trailing_state: mutable dict tracking trailing state, keys:
            - "first_close_done": bool -- set True after first partial close
            - "peak_pl_ratio": float -- highest pl_ratio seen after first close
            - (caller should also track partial qty externally)

    Returns:
        dict with:
            - "action": str -- "hold", "partial_close", "full_close"
            - "close_ratio": float -- fraction of REMAINING position to close (0.0-1.0)
            - "reason": str
    """
    if not ENABLE_TRAILING_PROFIT:
        return {"action": "hold", "close_ratio": 0.0,
                "reason": "Trailing profit disabled"}

    first_done = trailing_state.get("first_close_done", False)
    peak = trailing_state.get("peak_pl_ratio", 0.0)

    # Phase 1: Not yet hit first target
    if not first_done:
        if pl_ratio >= TRAILING_FIRST_TARGET:
            trailing_state["first_close_done"] = True
            trailing_state["peak_pl_ratio"] = pl_ratio
            log.info(f"Trailing profit: first target hit at {pl_ratio:.1%} "
                     f"-> partial close {TRAILING_PARTIAL_RATIO:.0%}")
            return {
                "action": "partial_close",
                "close_ratio": TRAILING_PARTIAL_RATIO,
                "reason": f"First target {TRAILING_FIRST_TARGET:.0%} reached "
                          f"(current {pl_ratio:.1%})"
            }
        return {"action": "hold", "close_ratio": 0.0,
                "reason": f"Below first target ({pl_ratio:.1%} < {TRAILING_FIRST_TARGET:.0%})"}

    # Phase 2: Trailing the remaining position
    if pl_ratio > peak:
        trailing_state["peak_pl_ratio"] = pl_ratio
        peak = pl_ratio

    # Check full profit target
    if pl_ratio >= TRAILING_SECOND_TARGET:
        log.info(f"Trailing profit: second target hit at {pl_ratio:.1%} -> full close")
        return {
            "action": "full_close",
            "close_ratio": 1.0,
            "reason": f"Second target {TRAILING_SECOND_TARGET:.0%} reached "
                      f"(current {pl_ratio:.1%})"
        }

    # Check giveback: profit retraced more than TRAILING_GIVEBACK_RATIO from peak
    giveback_threshold = peak * (1.0 - TRAILING_GIVEBACK_RATIO)
    if pl_ratio <= giveback_threshold and peak > TRAILING_FIRST_TARGET:
        log.info(f"Trailing profit: giveback triggered at {pl_ratio:.1%} "
                 f"(peak was {peak:.1%}, threshold {giveback_threshold:.1%}) -> full close")
        return {
            "action": "full_close",
            "close_ratio": 1.0,
            "reason": f"Giveback: {pl_ratio:.1%} dropped below "
                      f"{giveback_threshold:.1%} (peak {peak:.1%} - {TRAILING_GIVEBACK_RATIO:.0%})"
        }

    log.info(f"Trailing profit: holding remainder (pl={pl_ratio:.1%}, "
             f"peak={peak:.1%}, giveback_at={giveback_threshold:.1%})")
    return {"action": "hold", "close_ratio": 0.0,
            "reason": f"Trailing: pl={pl_ratio:.1%} peak={peak:.1%}"}


# ══════════════════════════════════════════════════════════════════════════════
# Dynamic Parameter Calculation Engine
# All functions return computed values from market environment data.
# Each has a fallback to the legacy fixed constant when data is unavailable.
# ══════════════════════════════════════════════════════════════════════════════

import math

# ── common/symbol_selector import guard ──────────────────────────────────────
_SYMBOL_SELECTOR_AVAILABLE = False
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
    from common.symbol_selector import (
        SymbolMetrics as _SymbolMetrics,
        select_symbols as _select_symbols,
        get_default_universe as _get_default_universe,
    )
    _SYMBOL_SELECTOR_AVAILABLE = True
except Exception as _e:
    log.warning(f"common.symbol_selector import failed: {_e}. Symbol selection disabled.")


def dynamic_ivr_size_multiplier(ivr: float) -> float:
    """Continuous IVR-based size multiplier (replaces threshold-based IVR filter).

    Maps IVR (0-100%) to a size multiplier using a sigmoid-like curve:
        IVR=0%  -> 0.20 (minimum size, IV extremely cheap)
        IVR=20% -> 0.50
        IVR=35% -> 0.80
        IVR=50% -> 1.00 (full size)
        IVR=70% -> 1.10
        IVR=100%-> 1.20 (slightly oversized when IV is rich)

    Rationale: Credit spreads benefit from high IV (more premium collected).
    Low IVR means IV is cheap relative to its range -> less premium -> reduce size.
    """
    if not ENABLE_DYNAMIC_IVR_SIZING:
        # Legacy threshold logic
        if ivr < IVR_SKIP_THRESHOLD:
            return 0.5
        return 1.0

    # Piecewise linear interpolation
    # Points: (ivr, multiplier)
    breakpoints = [
        (0,   0.20),
        (20,  0.50),
        (35,  0.80),
        (50,  1.00),
        (70,  1.10),
        (100, 1.20),
    ]
    # Clamp IVR to [0, 100]
    ivr = max(0.0, min(100.0, ivr))

    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if ivr <= x1:
            t = (ivr - x0) / (x1 - x0) if x1 != x0 else 0.0
            result = y0 + t * (y1 - y0)
            log.info(f"DynamicIVR: IVR={ivr:.1f}% -> size_mult={result:.2f}")
            return round(result, 3)

    return 1.20  # IVR=100%


def dynamic_vix_spike_exit_pct(vix_atr_20d: float, current_vix: float) -> float:
    """ATR-based VIX spike exit threshold (replaces fixed +15%).

    Uses 20-day ATR of VIX to determine what constitutes a 'spike'.
    Spike threshold = (ATR * coefficient) / current_vix.
    Higher ATR -> higher threshold (VIX is already volatile, larger move needed).
    Lower ATR -> lower threshold (VIX is calm, even small moves are significant).

    Coefficient: 1.5 (exits on a move of 1.5x the daily ATR).
    Floor: 5% (never ignore a +5% VIX move).
    Cap: 25% (never wait for +25% before exiting).
    """
    if not ENABLE_DYNAMIC_VIX_SPIKE_EXIT:
        return VIX_SPIKE_EXIT_PCT  # legacy 0.15

    if vix_atr_20d is None or vix_atr_20d <= 0 or current_vix <= 0:
        log.info("DynamicVIXSpike: ATR unavailable, using legacy 15%")
        return VIX_SPIKE_EXIT_PCT

    raw_pct = (vix_atr_20d * VIX_SPIKE_ATR_COEFF) / current_vix
    result = max(VIX_SPIKE_FLOOR, min(VIX_SPIKE_CAP, raw_pct))
    log.info(f"DynamicVIXSpike: ATR={vix_atr_20d:.2f} VIX={current_vix:.1f} coeff={VIX_SPIKE_ATR_COEFF} "
             f"-> threshold={result:.1%} (raw={raw_pct:.1%})")
    return round(result, 3)


def dynamic_stop_loss_multiplier(vix: float, hours_to_expiry: float) -> float:
    """VIX x time-to-expiry dynamic stop loss (replaces tiered/fixed stop).

    Intuition:
    - High VIX + lots of time remaining -> wider stop (allow for volatility)
    - Low VIX + near expiry -> tight stop (gamma risk is extreme)

    Formula: base_stop * vix_factor * time_factor
    - vix_factor = VIX / 20 (normalized to VIX=20 baseline)
    - time_factor = sqrt(hours_to_expiry / 6.5) (normalized to full trading day)
    - base_stop = 1.5 (center of 1.0-2.0 range)

    Floor: 0.80 (never risk < 80% of credit)
    Cap: 3.00 (never risk > 300% of credit)
    """
    if not ENABLE_DYNAMIC_STOP_LOSS:
        return STOP_LOSS_MULT  # legacy 2.0

    if vix is None or vix <= 0:
        return STOP_LOSS_MULT
    if hours_to_expiry is None or hours_to_expiry <= 0:
        hours_to_expiry = 0.1  # near expiry minimum

    vix_factor = vix / DYNSTOP_VIX_BASELINE
    time_factor = math.sqrt(max(hours_to_expiry, 0.1) / DYNSTOP_SESSION_HOURS)
    raw = DYNSTOP_BASE * vix_factor * time_factor
    result = max(DYNSTOP_FLOOR, min(DYNSTOP_CAP, raw))
    log.info(f"DynamicStop: VIX={vix:.1f} hours_left={hours_to_expiry:.1f} "
             f"-> stop={result:.2f}x (vix_f={vix_factor:.2f} time_f={time_factor:.2f})")
    return round(result, 2)


def dynamic_profit_target(vix: float, hours_to_expiry: float) -> float:
    """VIX x time dynamic profit target (replaces fixed 50%).

    High VIX -> take profit earlier (more volatile, gains can evaporate).
    Near expiry -> take profit earlier (gamma accelerates).

    Formula: base * (1.0 / vix_factor) * time_factor
    - Inverse VIX relationship: higher VIX = lower target = take profit sooner
    - time_factor: less time = lower target

    Floor: 0.25 (always take at least 25% profit)
    Cap: 0.75 (never wait beyond 75%)
    """
    if not ENABLE_DYNAMIC_PROFIT_TARGET:
        return PROFIT_TARGET  # legacy 0.50

    if vix is None or vix <= 0:
        return PROFIT_TARGET
    if hours_to_expiry is None or hours_to_expiry <= 0:
        hours_to_expiry = 0.1

    vix_factor = vix / DYNPROFIT_VIX_BASELINE
    time_factor = math.sqrt(max(hours_to_expiry, 0.1) / DYNSTOP_SESSION_HOURS)
    raw = DYNPROFIT_BASE * (1.0 / max(vix_factor, 0.5)) * time_factor
    result = max(DYNPROFIT_FLOOR, min(DYNPROFIT_CAP, raw))
    log.info(f"DynamicProfit: VIX={vix:.1f} hours_left={hours_to_expiry:.1f} "
             f"-> target={result:.0%}")
    return round(result, 3)


def dynamic_afternoon_size(hour: int, minute: int) -> float:
    """Continuous time-based size decay (replaces flat 50% after 14:00).

    Size decays as a function of minutes elapsed since market open (9:30 ET).
    Uses a smooth exponential decay curve:
        09:30 -> 1.00 (full size)
        10:30 -> 1.00 (first entry window, no reduction)
        12:00 -> 0.80
        13:00 -> 0.65
        14:00 -> 0.50
        14:30 -> 0.40
        15:00 -> 0.30
        15:30 -> 0.20

    Rationale: Gamma risk increases exponentially toward expiry for 0DTE.
    """
    if not ENABLE_DYNAMIC_AFTERNOON_SIZE:
        # Legacy: flat 50% after 14:00
        if hour >= 14:
            return 0.50
        return 1.00

    # Minutes since 9:30 ET
    market_open_min = 9 * 60 + 30
    current_min = hour * 60 + minute
    elapsed = max(0, current_min - market_open_min)
    total_session = 390  # 9:30 to 16:00 = 390 minutes

    if elapsed <= 60:
        # First hour: no reduction
        return 1.00

    # Exponential decay: size = 1.0 * exp(-k * (elapsed - 60))
    # Calibrated so that at 14:00 (270 min elapsed, 210 past first hour) -> 0.50
    # -k * 210 = ln(0.50) -> k = 0.693 / 210 = 0.0033 (see DYNSIZE_DECAY_K)
    size = math.exp(-DYNSIZE_DECAY_K * (elapsed - 60))
    result = max(DYNSIZE_FLOOR, min(1.00, size))
    log.info(f"DynamicAfternoonSize: {hour}:{minute:02d} ET elapsed={elapsed}min -> size={result:.2f}")
    return round(result, 3)


def dynamic_weekly_loss_limit(recent_win_rate: float, lookback_trades: int) -> int:
    """Win-rate-based weekly loss limit (replaces fixed 5 losses).

    Higher win rate -> can tolerate more weekly losses before pausing.
    Lower win rate -> should pause sooner.

    Formula: base_limit * (win_rate / 0.65)
    - Calibrated to win_rate=65% -> 5 losses (the old default)
    - win_rate=80% -> 6 losses
    - win_rate=50% -> 4 losses
    - win_rate=40% -> 3 losses

    Floor: 2 (always stop after 2 losses minimum)
    Cap: 8 (never allow > 8 weekly losses)
    """
    if not ENABLE_DYNAMIC_WEEKLY_LOSS:
        return WEEKLY_LOSS_LIMIT  # legacy 5

    if recent_win_rate is None or lookback_trades < WIN_RATE_MIN_TRADES:
        log.info(f"DynamicWeeklyLoss: insufficient data ({lookback_trades} trades), using legacy {WEEKLY_LOSS_LIMIT}")
        return WEEKLY_LOSS_LIMIT

    ratio = recent_win_rate / DYNWEEKLY_WIN_CALIB
    raw = DYNWEEKLY_BASE * ratio
    result = max(DYNWEEKLY_FLOOR, min(DYNWEEKLY_CAP, int(round(raw))))
    log.info(f"DynamicWeeklyLoss: win_rate={recent_win_rate:.1%} -> limit={result} "
             f"(from {lookback_trades} trades)")
    return result


def dynamic_consecutive_loss_stop(recent_win_rate: float, lookback_trades: int) -> int:
    """Win-rate-linked consecutive loss stop (replaces fixed 3).

    Higher win rate -> 3 consecutive losses is more anomalous, stop sooner.
    Lower win rate -> 3 consecutive losses is expected, allow more.

    Calibrated:
    - win_rate=80% -> stop after 2 (3 in a row at 80% is a 0.8% event)
    - win_rate=65% -> stop after 3 (legacy default)
    - win_rate=50% -> stop after 4
    - win_rate=40% -> stop after 5

    Floor: 2, Cap: 6
    """
    if not ENABLE_DYNAMIC_CONSEC_STOP:
        return CONSECUTIVE_LOSS_STOP  # legacy fixed value

    if recent_win_rate is None or lookback_trades < WIN_RATE_MIN_TRADES:
        return CONSECUTIVE_LOSS_STOP

    # Inverse relationship: higher win rate = lower consecutive stop
    if recent_win_rate >= 0.80:
        result = 2
    elif recent_win_rate >= 0.65:
        result = 3
    elif recent_win_rate >= 0.50:
        result = 4
    else:
        result = 5
    result = max(DYNCS_FLOOR, min(DYNCS_CAP, result))
    log.info(f"DynamicConsecStop: win_rate={recent_win_rate:.1%} -> stop_after={result}")
    return result


def dynamic_vix_gate(ivr: float) -> float:
    """IVR-linked VIX gate (replaces fixed 25).

    When IVR is high (IV is relatively expensive), we can tolerate higher absolute VIX
    because the premium collected compensates for the risk.

    IVR=0-20% -> VIX gate at 22 (conservative when IV is cheap)
    IVR=20-50% -> VIX gate at 25 (standard)
    IVR=50-80% -> VIX gate at 28
    IVR=80-100% -> VIX gate at 32 (aggressive when IV is rich)

    Floor: 20, Cap: 35
    """
    if not ENABLE_DYNAMIC_VIX_GATE:
        return VIX_GATE_FIXED  # legacy fixed value

    if ivr is None:
        return VIX_GATE_FIXED

    # Linear interpolation from IVR to VIX gate
    breakpoints = [
        (0,   22.0),
        (20,  25.0),
        (50,  28.0),
        (80,  32.0),
        (100, 35.0),
    ]
    ivr = max(0.0, min(100.0, ivr))
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if ivr <= x1:
            t = (ivr - x0) / (x1 - x0) if x1 != x0 else 0.0
            result = y0 + t * (y1 - y0)
            log.info(f"DynamicVIXGate: IVR={ivr:.1f}% -> VIX_gate={result:.1f}")
            return round(result, 1)
    return 35.0


# ── Fund Phase Auto-Transition ───────────────────────────────────────────────

def get_risk_params(account_balance_usd: float) -> dict:
    """Return risk parameters based on account balance phase.

    Phase transitions are automatic based on balance:
        Phase 1 (< $20,000 / ~300万円): Aggressive growth
        Phase 2 ($20,000 - $67,000 / 300万-1000万円): Balanced
        Phase 3 (> $67,000 / 1000万円+): Capital preservation

    Uses continuous interpolation within each phase for smooth transitions.
    """
    # Phase boundaries: use module-level PHASE1_CAP_USD / PHASE2_CAP_USD
    PHASE1_CAP = PHASE1_CAP_USD    # ~300万円 at ¥150/$
    PHASE2_CAP = PHASE2_CAP_USD    # ~1000万円 at ¥150/$

    if account_balance_usd < PHASE1_CAP:
        params = {
            "max_risk_per_trade": 0.02,    # 2%
            "daily_dd_limit": 0.05,         # 5%
            "monthly_dd_limit": 0.15,       # 15%
            "daily_profit_cap": 0.05,       # 5%
            "phase": 1,
            "phase_name": "Growth",
        }
    elif account_balance_usd < PHASE2_CAP:
        # Smooth interpolation between Phase 1 and Phase 3
        t = (account_balance_usd - PHASE1_CAP) / (PHASE2_CAP - PHASE1_CAP)
        params = {
            "max_risk_per_trade": 0.02 - t * 0.005,     # 2.0% -> 1.5%
            "daily_dd_limit": 0.05 - t * 0.02,           # 5.0% -> 3.0%
            "monthly_dd_limit": 0.15 - t * 0.05,         # 15% -> 10%
            "daily_profit_cap": 0.05 - t * 0.02,         # 5% -> 3%
            "phase": 2,
            "phase_name": "Balanced",
        }
    else:
        # Phase 3: further tightening based on how far above threshold
        t = min(1.0, (account_balance_usd - PHASE2_CAP) / PHASE2_CAP)
        params = {
            "max_risk_per_trade": 0.015 - t * 0.01,     # 1.5% -> 0.5%
            "daily_dd_limit": 0.03 - t * 0.015,          # 3.0% -> 1.5%
            "monthly_dd_limit": 0.10 - t * 0.05,         # 10% -> 5%
            "daily_profit_cap": 0.03 - t * 0.015,        # 3% -> 1.5%
            "phase": 3,
            "phase_name": "Preservation",
        }

    # Enforce floors
    params["max_risk_per_trade"] = max(0.005, params["max_risk_per_trade"])
    params["daily_dd_limit"] = max(0.015, params["daily_dd_limit"])
    params["monthly_dd_limit"] = max(0.05, params["monthly_dd_limit"])
    params["daily_profit_cap"] = max(0.015, params["daily_profit_cap"])
    params["account_balance_usd"] = account_balance_usd

    log.info(f"FundPhase: ${account_balance_usd:,.0f} -> Phase {params['phase']} "
             f"({params['phase_name']}) risk={params['max_risk_per_trade']:.1%} "
             f"dailyDD={params['daily_dd_limit']:.1%} monthlyDD={params['monthly_dd_limit']:.1%}")
    return params


# ── Premarket Assessment (Daily Environment Scoring) ─────────────────────────

def calc_hours_to_expiry() -> float:
    """Calculate hours remaining until 16:00 ET (market close)."""
    now = datetime.datetime.now(ET)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = close_time - now
    hours = max(0.0, delta.total_seconds() / 3600.0)
    return round(hours, 2)


def _calc_recent_win_rate() -> tuple:
    """Calculate win rate from recent trades. Returns (win_rate, num_trades)."""
    trades = load_pnl()
    exits = [t for t in trades if t.get("event") == "exit" and t.get("pnl_usd") is not None]
    if len(exits) < WIN_RATE_MIN_TRADES:
        return None, len(exits)
    recent = exits[-WIN_RATE_LOOKBACK:]  # last WIN_RATE_LOOKBACK trades
    wins = sum(1 for t in recent if (t.get("pnl_usd") or 0) > 0)
    return wins / len(recent), len(recent)


def daily_premarket_assessment(quote_ctx_or_none, account_balance_usd: float) -> dict:
    """Compute daily environment score (0-100) and set all dynamic parameters.

    Called once before market open. Returns a dict of all parameters for the day.

    Scoring components (each 0-20, total 0-100):
        1. VIX Level Score: VIX 12-20 optimal, <12 or >25 penalized
        2. IVR Score: Higher IVR = better premium = higher score
        3. Term Structure Score: Contango=20, Backwardation=5
        4. Trend Alignment Score: SMA direction + ORB alignment
        5. Market Breadth Score: No divergence=20, Divergence=5

    Environment Score -> Position Size Scalar:
        90-100: 1.20 (max aggression)
        70-89:  1.00 (normal)
        50-69:  0.70 (cautious)
        30-49:  0.40 (defensive)
        0-29:   0.00 (no trade)
    """
    result = {
        "env_score": 50,
        "env_grade": "C",
        "env_size_scalar": 0.70,
        "components": {},
        "dynamic_params": {},
    }

    if not ENABLE_PREMARKET_ASSESSMENT:
        result["env_score"] = 50
        result["env_size_scalar"] = 1.0
        result["env_grade"] = "N/A"
        return result

    score = 0

    # ── Component 1: VIX Level (0-20) ──
    vix = None
    ivr = None
    if quote_ctx_or_none is not None:
        vix = quote_ctx_or_none.get_vix()
        ivr = quote_ctx_or_none.get_ivr()

    if vix is not None:
        if 12 <= vix <= 20:
            vix_score = 20  # sweet spot
        elif 20 < vix <= 25:
            vix_score = 15
        elif 10 <= vix < 12:
            vix_score = 12
        elif 25 < vix <= 30:
            vix_score = 8
        else:
            vix_score = 3
        score += vix_score
        result["components"]["vix"] = {"value": vix, "score": vix_score}
    else:
        score += 10  # neutral default
        result["components"]["vix"] = {"value": None, "score": 10}

    # ── Component 2: IVR (0-20) ──
    if ivr is not None:
        ivr_score = min(20, int(ivr / 5))  # IVR 100% = 20, IVR 50% = 10
        score += ivr_score
        result["components"]["ivr"] = {"value": ivr, "score": ivr_score}
    else:
        score += 10
        result["components"]["ivr"] = {"value": None, "score": 10}

    # ── Component 3: Term Structure (0-20) ──
    if quote_ctx_or_none is not None:
        is_back = quote_ctx_or_none.is_backwardation()
        if is_back is True:
            ts_score = 5   # backwardation = dangerous
        elif is_back is False:
            ts_score = 20  # contango = favorable
        else:
            ts_score = 10
        score += ts_score
        result["components"]["term_structure"] = {"value": "backwardation" if is_back else "contango", "score": ts_score}
    else:
        score += 10
        result["components"]["term_structure"] = {"value": None, "score": 10}

    # ── Component 4: GEX (0-20) ──
    gex_result = check_gex_filter()
    if gex_result.get("gex", 0) != 0:
        gex_score = 20 if gex_result.get("gex_positive") else 5
        score += gex_score
        result["components"]["gex"] = {"value": gex_result["gex"], "score": gex_score}
    else:
        score += 10
        result["components"]["gex"] = {"value": None, "score": 10}

    # ── Component 5: Market Breadth (0-20) ──
    breadth_result = check_market_breadth()
    if breadth_result.get("divergence"):
        breadth_score = 5
    elif breadth_result.get("signal") == "positive_divergence":
        breadth_score = 18
    else:
        breadth_score = 15
    score += breadth_score
    result["components"]["breadth"] = {"value": breadth_result.get("signal"), "score": breadth_score}

    # ── Environment Score -> Grade + Size Scalar ──
    result["env_score"] = score
    if score >= 90:
        result["env_grade"] = "A+"
        result["env_size_scalar"] = 1.20
    elif score >= 80:
        result["env_grade"] = "A"
        result["env_size_scalar"] = 1.10
    elif score >= 70:
        result["env_grade"] = "B"
        result["env_size_scalar"] = 1.00
    elif score >= 60:
        result["env_grade"] = "C+"
        result["env_size_scalar"] = 0.80
    elif score >= 50:
        result["env_grade"] = "C"
        result["env_size_scalar"] = 0.70
    elif score >= 40:
        result["env_grade"] = "D"
        result["env_size_scalar"] = 0.50
    elif score >= 30:
        result["env_grade"] = "D-"
        result["env_size_scalar"] = 0.30
    else:
        result["env_grade"] = "F"
        result["env_size_scalar"] = 0.00  # no trade

    # ── Compute all dynamic parameters for the day ──
    hours_left = calc_hours_to_expiry()
    win_rate, num_trades = _calc_recent_win_rate()
    fund_params = get_risk_params(account_balance_usd)

    # IVR-based sizing
    ivr_mult = dynamic_ivr_size_multiplier(ivr) if ivr is not None else 1.0

    # VIX ATR for spike exit
    vix_atr = None
    if quote_ctx_or_none is not None:
        vix_atr = quote_ctx_or_none.get_vix_atr_20d()
    spike_exit_pct = dynamic_vix_spike_exit_pct(vix_atr, vix) if vix else VIX_SPIKE_EXIT_PCT

    # VIX gate
    vix_gate = dynamic_vix_gate(ivr) if ivr is not None else 25.0

    # Stop loss and profit target
    stop_mult = dynamic_stop_loss_multiplier(vix, hours_left) if vix else STOP_LOSS_MULT
    profit_tgt = dynamic_profit_target(vix, hours_left) if vix else PROFIT_TARGET

    # Weekly/consecutive loss limits
    weekly_limit = dynamic_weekly_loss_limit(win_rate, num_trades)
    consec_stop = dynamic_consecutive_loss_stop(win_rate, num_trades)

    # Monthly DD (from fund phase)
    monthly_dd = fund_params["monthly_dd_limit"] if ENABLE_DYNAMIC_MONTHLY_DD else MONTHLY_DD_THRESHOLD
    daily_cap = fund_params["daily_profit_cap"] if ENABLE_DYNAMIC_DAILY_CAP else DAILY_PROFIT_CAP_PCT

    result["dynamic_params"] = {
        "ivr_size_multiplier": ivr_mult,
        "vix_spike_exit_pct": spike_exit_pct,
        "vix_gate": vix_gate,
        "stop_loss_mult": stop_mult,
        "profit_target": profit_tgt,
        "weekly_loss_limit": weekly_limit,
        "consecutive_loss_stop": consec_stop,
        "monthly_dd_threshold": monthly_dd,
        "daily_profit_cap": daily_cap,
        "fund_phase": fund_params["phase"],
        "fund_phase_name": fund_params["phase_name"],
        "max_risk_per_trade": fund_params["max_risk_per_trade"],
        "hours_to_expiry": hours_left,
        "win_rate": win_rate,
        "num_trades_analyzed": num_trades,
    }

    log.info(f"PremarketAssessment: score={score}/100 grade={result['env_grade']} "
             f"scalar={result['env_size_scalar']:.2f} phase={fund_params['phase']} "
             f"vix_gate={vix_gate:.1f} stop={stop_mult:.2f}x profit={profit_tgt:.0%}")

    # ── Symbol Selection (common/symbol_selector) ──────────────────────────────
    # 戦術は環境スコアから自動決定
    #   score >= 70 (B以上)  : credit_spread (プレミアム売り)
    #   score >= 50, VIX高め  : iron_condor
    #   score < 50            : no trade (銘柄選択スキップ)
    result["selected_symbols"] = [UNDERLYING]  # デフォルトは既存の単一銘柄
    result["symbol_tactic"] = "credit_spread"

    if _SYMBOL_SELECTOR_AVAILABLE and score >= 50:
        try:
            # 環境スコアから戦術を決定
            if score >= 70:
                sym_tactic = "credit_spread"
            else:
                sym_tactic = "iron_condor"

            # メトリクス構築: IVR/VIXはすでに取得済み。他はNone (動的補完の余地あり)
            # 現状はユニバースのうちSPYのみ実データ付きで評価、他はNullメトリクス
            universe = _get_default_universe()
            metrics_list: list[_SymbolMetrics] = []
            for sym in universe:
                m = _SymbolMetrics(symbol=sym)
                if sym == UNDERLYING:
                    m.ivr = ivr  # SPYのIVRは取得済み
                    m.vix_correlation = 0.95  # SPY≒VIX相関は既知の定数
                metrics_list.append(m)

            top = _select_symbols(
                metrics_list,
                tactic=sym_tactic,
                top_n=3,
                earnings_exclude=True,
            )
            if top:
                result["selected_symbols"] = [s.symbol for s in top]
                result["symbol_scores"] = {
                    s.symbol: round(s.score, 4) for s in top
                }
                result["symbol_tactic"] = sym_tactic
                log.info(
                    f"[SymbolSelector] tactic={sym_tactic} "
                    f"selected={result['selected_symbols']}"
                )
        except Exception as _sel_err:
            log.warning(f"[SymbolSelector] selection failed: {_sel_err}. "
                        f"Falling back to {UNDERLYING}.")

    return result


# ── moomoo futu-api client ────────────────────────────────────────────────────
try:
    from futu import OpenQuoteContext, OpenSecTradeContext, TrdMarket, TrdEnv
    from futu import TrdSide, OrderType, RET_OK, SecurityFirm
    import futu as ft
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    log.warning("futu-api not installed. Running in DRY-RUN mode.")

class MarketData:
    def __init__(self):
        self.quote_ctx = None

    def connect(self):
        if not FUTU_AVAILABLE:
            return False
        try:
            self.quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
            log.info("Quote context connected")
            return True
        except OSError as e:
            if "Connection refused" in str(e) or "timed out" in str(e).lower():
                msg = f"❌OpenD接続タイムアウト/拒否: {e}"
                log.error(msg)
                pushover("SPX Bot OpenD障害", f"接続タイムアウト・OpenD未起動の可能性: {str(e)[:100]}", priority=1)
            else:
                log.error(f"Quote connect OSError: {e}")
                pushover("SPX Bot OpenD障害", f"Quote接続エラー: {str(e)[:100]}", priority=1)
            return False
        except Exception as e:
            err_str = str(e).lower()
            if "auth" in err_str or "login" in err_str or "password" in err_str:
                log.error(f"Quote connect auth failure: {e}")
                pushover("SPX Bot OpenD障害", f"認証失敗: {str(e)[:100]}", priority=1)
            elif "limit" in err_str or "rate" in err_str or "too many" in err_str:
                log.error(f"Quote connect API limit: {e}")
                pushover("SPX Bot OpenD障害", f"API制限: {str(e)[:100]}", priority=0)
            else:
                log.error(f"Quote connect failed: {e}")
                pushover("SPX Bot OpenD障害", f"Quote接続失敗: {str(e)[:100]}", priority=1)
            return False

    def close(self):
        if self.quote_ctx:
            self.quote_ctx.close()

    def get_spy_price(self) -> Optional[float]:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 560.0  # dry-run
        ret, data = self.quote_ctx.get_market_snapshot(["US.SPY"])
        if ret == RET_OK and not data.empty:
            return float(data.iloc[0]["last_price"])
        return None

    def get_sma20(self) -> Optional[float]:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 558.0  # dry-run
        ret, data, _ = self.quote_ctx.request_history_kline(
            "US.SPY", start="2024-01-01",
            ktype=ft.KLType.K_DAY, max_count=SMA_PERIOD + 10
        )
        if ret == RET_OK and len(data) >= SMA_PERIOD:
            return float(data["close"].tail(SMA_PERIOD).mean())
        return None

    def get_vix(self) -> Optional[float]:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 18.0  # dry-run
        ret, data = self.quote_ctx.get_market_snapshot(["US.VIX"])
        if ret == RET_OK and not data.empty:
            return float(data.iloc[0]["last_price"])
        return None

    def get_vix_prev_close(self) -> Optional[float]:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 17.0  # dry-run
        ret, data, _ = self.quote_ctx.request_history_kline(
            "US.VIX", start="2024-01-01",
            ktype=ft.KLType.K_DAY, max_count=5
        )
        if ret == RET_OK and len(data) >= 2:
            return float(data["close"].iloc[-2])
        return None

    def get_option_chain(self, expiry: str, direction: str) -> list:
        """Returns list of option contracts for given expiry & direction."""
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return []
        ret, data = self.quote_ctx.get_option_chain(
            "US.SPY", index_option_type=ft.IndexOptionType.ETF,
            start=expiry, end=expiry
        )
        if ret != RET_OK:
            return []
        opt_type = "PUT" if direction == "bull_put" else "CALL"
        subset = data[data["option_type"] == opt_type]
        return subset.to_dict("records")

    # ── #1: IVR (Implied Volatility Rank) via yfinance ─────────────────────
    def get_ivr(self) -> Optional[float]:
        """Calculate IVR: current VIX position within past-1-year range (0-100%).
        Returns None on failure. Uses yfinance for ^VIX historical data."""
        try:
            import yfinance as yf
            vix_ticker = yf.Ticker("^VIX")
            hist = vix_ticker.history(period="1y")
            if hist.empty or len(hist) < 20:
                log.warning("IVR: insufficient VIX history")
                return None
            high_1y = float(hist["High"].max())
            low_1y  = float(hist["Low"].min())
            current = float(hist["Close"].iloc[-1])
            if high_1y == low_1y:
                return 50.0
            ivr = (current - low_1y) / (high_1y - low_1y) * 100
            log.info(f"IVR: {ivr:.1f}% (VIX={current:.1f}, 1Y range={low_1y:.1f}-{high_1y:.1f})")
            return round(ivr, 1)
        except Exception as e:
            log.warning(f"IVR calculation failed: {e}")
            return None

    # ── #2: VIX Term Structure (VIX vs VIX3M) via yfinance ───────────────
    def get_vix3m(self) -> Optional[float]:
        """Get VIX3M (3-month VIX) current value via yfinance."""
        try:
            import yfinance as yf
            ticker = yf.Ticker("^VIX3M")
            hist = ticker.history(period="5d")
            if hist.empty:
                log.warning("VIX3M: no data from yfinance")
                return None
            val = float(hist["Close"].iloc[-1])
            log.info(f"VIX3M: {val:.2f}")
            return val
        except Exception as e:
            log.warning(f"VIX3M fetch failed: {e}")
            return None

    def is_backwardation(self) -> Optional[bool]:
        """Check if VIX term structure is in backwardation (VIX > VIX3M).
        Returns True=backwardation, False=contango, None=data unavailable."""
        vix_val = self.get_vix()
        vix3m   = self.get_vix3m()
        if vix_val is None or vix3m is None:
            return None
        is_back = vix_val > vix3m
        state = "BACKWARDATION" if is_back else "CONTANGO"
        log.info(f"VIX Term Structure: VIX={vix_val:.1f} vs VIX3M={vix3m:.1f} → {state}")
        return is_back

    def get_vix_atr_20d(self) -> Optional[float]:
        """Calculate 20-day ATR of VIX for dynamic spike exit threshold."""
        try:
            import yfinance as yf
            vix_ticker = yf.Ticker("^VIX")
            hist = vix_ticker.history(period="30d")
            if hist.empty or len(hist) < 20:
                return None
            # ATR = average of daily (High - Low) over last 20 days
            tr = hist["High"].tail(20) - hist["Low"].tail(20)
            atr = float(tr.mean())
            log.info(f"VIX ATR(20d): {atr:.2f}")
            return atr
        except Exception as e:
            log.warning(f"VIX ATR calculation failed: {e}")
            return None

    def find_strike_by_delta(self, chain: list, target_delta: float, side: str) -> Optional[dict]:
        """Find option closest to target delta."""
        if not chain:
            return None
        best = None
        best_diff = float("inf")
        for opt in chain:
            d = abs(opt.get("delta", 0))
            diff = abs(d - target_delta)
            if diff < best_diff:
                best_diff = diff
                best = opt
        return best


class TradeEngine:
    def __init__(self, account_id: str = ""):
        self.account_id = account_id  # resolved dynamically on connect
        self.trade_env = TrdEnv.REAL if FUTU_AVAILABLE else None
        self.trade_ctx = None
        self.unlock_ok = False  # set True after successful unlock_trade

    def connect(self):
        if not FUTU_AVAILABLE:
            return False
        try:
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=OPEND_HOST, port=OPEND_PORT,
                security_firm=SecurityFirm.FUTUJP,
            )
            log.info("Trade context connected")

            # Resolve account ID first (determines REAL vs SIMULATE)
            self._resolve_account()

            # ── Unlock trade (REAL account only) ─────────────────────────
            if self.trade_env == TrdEnv.REAL:
                if TRADE_PASSWORD:
                    ret, data = self.trade_ctx.unlock_trade(password=TRADE_PASSWORD)
                    if ret != RET_OK:
                        msg_str = str(data)
                        if "unlock button" in msg_str or "disabled in the GUI" in msg_str:
                            # GUI AppImage disables API unlock; trading lock managed by GUI
                            log.warning("unlock_trade API disabled in GUI mode; assuming GUI-unlocked")
                            self.unlock_ok = True
                        else:
                            log.error(f"unlock_trade failed: {data}")
                            pushover(
                                "SPX Bot",
                                f"❌取引ロック解除失敗・Bot停止: {msg_str[:120]}",
                                priority=1,
                            )
                            self.trade_ctx.close()
                            self.trade_ctx = None
                            return False
                    else:
                        log.info("unlock_trade: success")
                        self.unlock_ok = True
                else:
                    log.warning("TRADE_PASSWORD not set; skipping unlock_trade")
            else:
                log.info(f"SIMULATE mode ({self.account_id}); unlock_trade不要")
                self.unlock_ok = True  # SIMULATE needs no unlock

            return True
        except OSError as e:
            if "Connection refused" in str(e) or "timed out" in str(e).lower():
                log.error(f"Trade connect timeout/refused: {e}")
                pushover("SPX Bot OpenD障害", f"Trade接続タイムアウト・OpenD未起動の可能性: {str(e)[:100]}", priority=1)
            else:
                log.error(f"Trade connect OSError: {e}")
                pushover("SPX Bot OpenD障害", f"Trade接続エラー: {str(e)[:100]}", priority=1)
            return False
        except Exception as e:
            err_str = str(e).lower()
            if "auth" in err_str or "login" in err_str or "password" in err_str:
                log.error(f"Trade connect auth failure: {e}")
                pushover("SPX Bot OpenD障害", f"Trade認証失敗: {str(e)[:100]}", priority=1)
            elif "limit" in err_str or "rate" in err_str or "too many" in err_str:
                log.error(f"Trade connect API limit: {e}")
                pushover("SPX Bot OpenD障害", f"Trade API制限: {str(e)[:100]}", priority=0)
            else:
                log.error(f"Trade connect failed: {e}")
                pushover("SPX Bot OpenD障害", f"Trade接続失敗: {str(e)[:100]}", priority=1)
            return False

    def _resolve_account(self):
        """Find the best account: prefer REAL DERIVATIVES (JP_DERIVATIVE), else any REAL, else SIMULATE."""
        ret, data = self.trade_ctx.get_acc_list()
        if ret != RET_OK or data.empty:
            log.warning("get_acc_list failed; account_id unresolved")
            return
        real = data[data["trd_env"] == "REAL"]
        if not real.empty:
            # Prefer acc_type == DERIVATIVES (オプション専用口座)
            deriv = real[real["acc_type"] == "DERIVATIVES"]
            if not deriv.empty:
                self.account_id = str(int(deriv.iloc[0]["acc_id"]))
                self.trade_env = TrdEnv.REAL
                jp_types = deriv.iloc[0].get("jp_acc_type", "")
                log.info(f"Resolved REAL DERIVATIVES account: acc_id={self.account_id} jp_acc_type={jp_types}")
                return
            # Fall back to any REAL account
            self.account_id = str(int(real.iloc[0]["acc_id"]))
            self.trade_env = TrdEnv.REAL
            log.warning(f"No DERIVATIVES account; using first REAL acc_id={self.account_id}")
            return
        # Fall back to SIMULATE
        sim = data[data["trd_env"] == "SIMULATE"]
        if not sim.empty:
            self.account_id = str(int(sim.iloc[0]["acc_id"]))
            self.trade_env = TrdEnv.SIMULATE
            log.warning(f"No REAL account found; using SIMULATE acc_id={self.account_id}")
            return
        log.error("No usable account found in get_acc_list()")

    def close(self):
        if self.trade_ctx:
            self.trade_ctx.close()

    def get_account_cash(self) -> float:
        """Return available capital for position sizing.
        Uses net_assets (margin-based equity) if available, else cash.
        Fallback: $2,500 (≈ ¥379,000 ÷ 150)."""
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return 2500.0  # dry-run: ¥379,000 ÷ 150
        if not self.account_id:
            log.error("account_id not resolved; using fallback $2,500")
            return 2500.0
        ret, data = self.trade_ctx.accinfo_query(
            trd_env=self.trade_env, acc_id=int(self.account_id)
        )
        if ret == RET_OK and not data.empty:
            row = data.iloc[0]
            # Prefer net_assets (total equity incl. unrealized P&L) for margin-based sizing
            net_assets = float(row.get("net_assets", 0))
            cash = float(row.get("cash", 0))
            capital = net_assets if net_assets > 0 else cash
            log.info(
                f"Account capital ({self.trade_env} acc={self.account_id}): "
                f"net_assets=${net_assets:,.2f} cash=${cash:,.2f} → using ${capital:,.2f}"
            )
            return capital if capital > 0 else 2500.0
        log.error(f"accinfo_query failed for acc_id={self.account_id}: {data}")
        pushover("SPX Bot", "⚠️残高取得失敗・フォールバック使用中($2,500)", priority=0)
        return 2500.0

    def check_startup_margin(self, usd_to_jpy: float = 150.0) -> bool:
        """Startup margin check: warn and halt entries if balance < MARGIN_THRESHOLD_JPY.
        Returns True if margin is sufficient, False if entries should be skipped."""
        capital = self.get_account_cash()
        jpy_equiv = capital * usd_to_jpy
        if jpy_equiv < MARGIN_THRESHOLD_JPY:
            msg = (f"⚠️証拠金不足: ${capital:,.0f} (≈¥{jpy_equiv:,.0f}) "
                   f"< ¥{MARGIN_THRESHOLD_JPY:,} → エントリー停止")
            log.warning(msg)
            pushover("SPX Bot 証拠金警告", msg, priority=1)
            return False
        log.info(f"Margin check OK: ${capital:,.0f} (≈¥{jpy_equiv:,.0f})")
        return True

    def calc_position_size(self, cash: float, vix: float, vix_prev: Optional[float]) -> int:
        """Dynamic position sizing per strategy spec."""
        now = datetime.datetime.now(ET)
        month = now.month
        weekday = now.weekday()  # 0=Mon, 4=Fri

        # Base ratio
        ratio = SIZE_RATIO_BASE

        # VIX spike +SIZE_VIX_SPIKE_PCT
        if vix_prev and vix >= vix_prev * SIZE_VIX_SPIKE_PCT:
            ratio = SIZE_RATIO_VIX_SPIKE
            log.info(f"VIX spike +{SIZE_VIX_SPIKE_PCT:.0%} ({vix_prev:.1f}→{vix:.1f}) → ratio {SIZE_RATIO_VIX_SPIKE:.0%}")
        # OpEx week
        elif is_notrade_today():
            ratio = SIZE_RATIO_OPEX
        # Friday or Monday
        elif weekday in (0, 4):
            ratio = SIZE_RATIO_MON_FRI

        # Seasonal multiplier
        if month in (9, 10):
            ratio *= SIZE_SEASONAL_SEP_OCT
            log.info(f"Seasonal Sep/Oct → ratio ×{SIZE_SEASONAL_SEP_OCT} = {ratio:.0%}")
        elif month in (7, 11):
            ratio *= SIZE_SEASONAL_JUL_NOV
            log.info(f"Seasonal Jul/Nov → ratio ×{SIZE_SEASONAL_JUL_NOV} = {ratio:.0%}")

        # Each spread: $5 wide × 100 = $500 margin
        margin_per_contract = SPREAD_WIDTH * 100
        max_contracts = int((cash * ratio) / margin_per_contract)
        contracts = max(1, max_contracts)
        log.info(f"Position size: cash={cash:.0f}, ratio={ratio:.0%}, contracts={contracts}")
        return contracts

    def place_spread(self, sell_code: str, buy_code: str, qty: int, direction: str) -> bool:
        """Place credit spread: sell leg1, buy leg2. Retry leg2, buyback leg1 on failure."""
        if not FUTU_AVAILABLE or not self.trade_ctx:
            log.info(f"[DRY-RUN] {direction}: SELL {sell_code} / BUY {buy_code} qty={qty}")
            return True

        env = self.trade_env
        acc = int(self.account_id)

        # Leg1: Sell
        log.info(f"Leg1 SELL: {sell_code} qty={qty}")
        ret1, data1 = self.trade_ctx.place_order(
            price=0, qty=qty, code=sell_code,
            trd_side=TrdSide.SELL, order_type=OrderType.MARKET,
            trd_env=env, acc_id=acc
        )
        if ret1 != RET_OK:
            log.error(f"Leg1 sell failed: {data1}")
            return False

        time.sleep(1)

        # Leg2: Buy (3 attempts)
        log.info(f"Leg2 BUY: {buy_code} qty={qty}")
        for attempt in range(3):
            ret2, data2 = self.trade_ctx.place_order(
                price=0, qty=qty, code=buy_code,
                trd_side=TrdSide.BUY, order_type=OrderType.MARKET,
                trd_env=env, acc_id=acc
            )
            if ret2 == RET_OK:
                log.info(f"Spread placed successfully: {direction}")
                return True
            log.warning(f"Leg2 buy attempt {attempt+1}/3 failed: {data2}")
            time.sleep(2)

        # Leg2 failed all retries → buy back leg1
        log.error("Leg2 failed 3x → buying back Leg1 to prevent naked position")
        for attempt in range(3):
            ret_back, _ = self.trade_ctx.place_order(
                price=0, qty=qty, code=sell_code,
                trd_side=TrdSide.BUY, order_type=OrderType.MARKET,
                trd_env=env, acc_id=acc
            )
            if ret_back == RET_OK:
                log.info("Leg1 buyback successful")
                return False
            time.sleep(2)

        # Leg1 buyback also failed → CRITICAL ALERT
        pushover(
            "NAKED POSITION RISK",
            f"Leg2 failed AND Leg1 buyback failed! May have naked {sell_code} (acc {self.account_id})",
            priority=2
        )
        return False

    def place_hedge(self, put_code: str, qty: int) -> bool:
        """Buy OTM puts for tail hedge."""
        if not FUTU_AVAILABLE or not self.trade_ctx:
            log.info(f"[DRY-RUN] Tail hedge: BUY {put_code} qty={qty}")
            return True
        ret, data = self.trade_ctx.place_order(
            price=0, qty=qty, code=put_code,
            trd_side=TrdSide.BUY, order_type=OrderType.MARKET,
            trd_env=self.trade_env, acc_id=int(self.account_id)
        )
        if ret == RET_OK:
            log.info(f"Tail hedge placed: {put_code} qty={qty}")
            return True
        log.warning(f"Tail hedge failed: {data}")
        return False

    def get_open_positions(self) -> list:
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return []
        ret, data = self.trade_ctx.position_list_query(
            trd_env=self.trade_env, acc_id=int(self.account_id)
        )
        if ret == RET_OK:
            return data.to_dict("records")
        return []

    def close_all_positions(self, reason: str = "force_close"):
        """Close all open option positions at market."""
        positions = self.get_open_positions()
        if not positions:
            log.info(f"No positions to close ({reason})")
            return
        log.info(f"Force closing {len(positions)} positions ({reason})")
        env = self.trade_env
        acc = int(self.account_id)
        for pos in positions:
            code = pos.get("code", "")
            qty  = abs(int(pos.get("qty", 0)))
            if qty == 0:
                continue
            side = TrdSide.BUY if pos.get("position_side") == "LONG" else TrdSide.SELL
            self.trade_ctx.place_order(
                price=0, qty=qty, code=code,
                trd_side=side, order_type=OrderType.MARKET,
                trd_env=env, acc_id=acc
            )


# ── Main bot logic ────────────────────────────────────────────────────────────
class SPXBot:
    def __init__(self):
        self.mkt  = MarketData()
        self.eng  = TradeEngine()
        # traded_times is a dict: key → True for entry times, "direction" → direction string
        self.traded_times: dict = {}
        self.consecutive_start_failures: int = load_failures()
        self._margin_ok: bool = True  # set False if startup margin check fails
        self.entry_vix: Optional[float] = None  # #4: VIX level at entry time for spike exit
        # Dynamic parameter state (set daily by premarket assessment)
        self._daily_params: dict = {}
        self._premarket_done: bool = False

    def get_expiry(self) -> str:
        """0DTE on Mon/Wed/Fri, 1DTE on Tue/Thu.
        If calculated expiry is a US market holiday, use previous business day."""
        now = datetime.datetime.now(ET)
        wd  = now.weekday()
        if wd in (0, 2, 4):  # Mon, Wed, Fri → 0DTE
            expiry_date = now.date()
        else:                 # Tue, Thu → 1DTE
            expiry_date = (now + datetime.timedelta(days=1)).date()
        # Adjust backward if expiry falls on a holiday or weekend
        for _ in range(10):
            if expiry_date not in US_HOLIDAYS and expiry_date.weekday() < 5:
                break
            expiry_date -= datetime.timedelta(days=1)
        return expiry_date.strftime("%Y-%m-%d")

    def should_enter(self, current_hour: int, current_min: int) -> bool:
        for h, m in ENTRY_TIMES:
            key = f"{h}:{m:02d}"
            if current_hour == h and current_min == m and key not in self.traded_times:
                return True
        return False

    def run_entry(self):
        now      = datetime.datetime.now(ET)
        expiry   = self.get_expiry()
        time_key = f"{now.hour}:{now.minute:02d}"

        spy      = self.mkt.get_spy_price()
        sma      = self.mkt.get_sma20()
        vix      = self.mkt.get_vix()
        vix_prev = self.mkt.get_vix_prev_close()

        if spy is None or sma is None or vix is None:
            log.error("Market data unavailable, skipping entry")
            pushover("SPX Bot", f"❌エントリー失敗 {time_key}ET: 市場データ取得失敗")
            self.traded_times[time_key] = True
            return

        log.info(f"SPY={spy:.2f} SMA20={sma:.2f} VIX={vix:.2f} VIX_prev={vix_prev}")

        # Environment score no-trade gate
        dp = self._daily_params.get("dynamic_params", {})
        env_score = self._daily_params.get("env_score", 50)
        env_scalar = self._daily_params.get("env_size_scalar", 1.0)
        if ENABLE_PREMARKET_ASSESSMENT and env_scalar <= 0.0:
            log.info(f"Environment score {env_score}/100 -> NO TRADE (grade F)")
            pushover("SPX Bot", f"⏭️ノートレード {time_key}ET: 環境スコア{env_score}/100 (Grade F)")
            self.traded_times[time_key] = True
            return

        # VIX gate (dynamic: IVR-linked, or fixed 25)
        vix_gate_threshold = dp.get("vix_gate", VIX_GATE_FIXED)
        if vix >= vix_gate_threshold:
            log.info(f"VIX={vix:.1f} >= {vix_gate_threshold:.1f} → no trade (dynamic gate)")
            pushover("SPX Bot", f"⏭️ノートレード {time_key}ET: VIX={vix:.1f}>={vix_gate_threshold:.1f}")
            self.traded_times[time_key] = True
            return

        # ── #1: IVR Filter (dynamic continuous function) ──────────────────
        size_multiplier = 1.0  # accumulated multiplier for size adjustments
        if ENABLE_IVR_FILTER:
            ivr = self.mkt.get_ivr()
            if ivr is not None:
                ivr_mult = dynamic_ivr_size_multiplier(ivr)
                size_multiplier *= ivr_mult
                log.info(f"IVR filter (dynamic): IVR={ivr:.1f}% -> size_mult={ivr_mult:.2f}")
            else:
                log.warning("IVR filter: data unavailable, proceeding with full size")

        # ── #2: VIX Term Structure ───────────────────────────────────────────
        if ENABLE_VIX_TERM_STRUCT:
            backwardation = self.mkt.is_backwardation()
            if backwardation is True:
                size_multiplier *= 0.5
                log.info("VIX Term Structure: BACKWARDATION → size 50%")
            elif backwardation is False:
                log.info("VIX Term Structure: CONTANGO → no adjustment")
            else:
                log.warning("VIX Term Structure: data unavailable, no adjustment")

        direction = "bull_put" if spy > sma else "bear_call"
        log.info(f"Direction: {direction} (SPY {'>' if spy > sma else '<'} SMA20)")

        # 14:00 entry: only if direction matches 10:30 direction
        if now.hour == 14:
            morning_dir = self.traded_times.get("direction")
            if morning_dir and morning_dir != direction:
                log.info(f"14:00 entry: direction mismatch ({morning_dir} vs {direction}) → skip")
                pushover("SPX Bot", f"⏭️ノートレード {time_key}ET: 方向不一致({morning_dir}→{direction})")
                self.traded_times[time_key] = True
                return

        # ── #3: Afternoon size (dynamic continuous decay) ──────────────────
        if ENABLE_AFTERNOON_SIZE:
            afternoon_mult = dynamic_afternoon_size(now.hour, now.minute)
            if afternoon_mult < 1.0:
                size_multiplier *= afternoon_mult
                log.info(f"Afternoon size (dynamic): {now.hour}:{now.minute:02d} -> {afternoon_mult:.2f}")

        cash = self.eng.get_account_cash()

        # ── #5: Kelly Criterion sizing override ─────────────────────────────
        kelly = calc_kelly_fraction()
        if kelly is not None:
            if kelly == 0.0:
                # Negative edge → use minimum 1 contract, skip normal sizing
                qty = 1
                log.info("Kelly: negative edge → forced 1 contract")
            else:
                margin_per = SPREAD_WIDTH * 100
                qty = max(1, int((cash * kelly) / margin_per))
                log.info(f"Kelly sizing: cash={cash:.0f} kelly={kelly:.3f} → qty={qty}")
        else:
            qty = self.eng.calc_position_size(cash, vix, vix_prev)

        # ── #6: Monthly DD size multiplier ──────────────────────────────────
        monthly_mult = check_monthly_dd_size_multiplier(cash)
        if monthly_mult < 1.0:
            size_multiplier *= monthly_mult

        # Apply environment score scalar from premarket assessment
        if ENABLE_PREMARKET_ASSESSMENT and env_scalar != 1.0:
            size_multiplier *= env_scalar
            log.info(f"Environment scalar: {env_scalar:.2f} (score={env_score}/100)")

        # Apply accumulated size multiplier from filters (#1, #2, #3, #5, #6, env)
        if size_multiplier != 1.0:
            original_qty = qty
            qty = max(1, int(qty * size_multiplier))
            log.info(f"Size adjustment: {original_qty} → {qty} (multiplier={size_multiplier:.2f})")

        # Get option chain
        chain = self.mkt.get_option_chain(expiry, direction)

        # Find sell strike (delta ~0.20)
        sell_opt = self.mkt.find_strike_by_delta(chain, SELL_DELTA, "sell")
        if not sell_opt:
            log.warning("Could not find sell strike, skipping")
            pushover("SPX Bot", f"❌エントリー失敗 {time_key}ET: 売りストライク見つからず")
            self.traded_times[time_key] = True
            return

        sell_strike = sell_opt.get("strike_price", 0)
        buy_strike  = sell_strike - SPREAD_WIDTH if direction == "bull_put" else sell_strike + SPREAD_WIDTH

        # Find buy option at target strike
        candidates = [o for o in chain if abs(o.get("strike_price", 0) - buy_strike) < 1.5]
        if not candidates:
            log.warning(f"Could not find buy strike near {buy_strike}, skipping")
            pushover("SPX Bot", f"❌エントリー失敗 {time_key}ET: 買いストライク見つからず({buy_strike}近辺)")
            self.traded_times[time_key] = True
            return
        buy_opt = min(candidates, key=lambda o: abs(o.get("strike_price", 0) - buy_strike))

        sell_code = sell_opt.get("code", "")
        buy_code  = buy_opt.get("code", "")

        dir_label = "BullPut" if direction == "bull_put" else "BearCall"
        opt_label = "P" if direction == "bull_put" else "C"
        # Estimate net credit: sell at bid, buy at ask (per share × 100 = per contract)
        sell_bid = sell_opt.get("bid_price", sell_opt.get("last_price", 0))
        buy_ask  = buy_opt.get("ask_price", buy_opt.get("last_price", 0))
        net_credit = round(sell_bid - buy_ask, 2)
        log.info(f"Spread: SELL {sell_code} @ strike {sell_strike} / BUY {buy_code} @ strike {buy_strike} / qty={qty} / net_credit=${net_credit}")
        success = self.eng.place_spread(sell_code, buy_code, qty, direction)

        if success:
            self.traded_times[time_key] = True

            # ── #4: Record VIX at entry for spike exit monitoring ────────
            if ENABLE_VIX_SPIKE_EXIT:
                self.entry_vix = vix
                log.info(f"VIX spike exit: entry VIX recorded = {vix:.2f}")

            # Record morning direction for 14:00 filter
            if now.hour == 10:
                self.traded_times["direction"] = direction

            credit_str = f"${net_credit:.2f}" if net_credit > 0 else "成行"
            pushover(
                "SPX Bot",
                f"✅エントリー {time_key}ET {dir_label} "
                f"SELL {sell_strike:.0f}{opt_label}/BUY {buy_strike:.0f}{opt_label} {qty}枚 {credit_str}"
            )
            append_monthly_csv({
                "direction": direction,
                "sell_strike": sell_strike,
                "buy_strike": buy_strike,
                "qty": qty,
                "net_credit": net_credit,
                "result": "entered",
            })
            append_pnl_entry({
                "event": "entry",
                "direction": direction,
                "sell_strike": sell_strike,
                "buy_strike": buy_strike,
                "qty": qty,
                "net_credit": net_credit,
            })

            # Tail hedge: buy delta-0.05 OTM put (daily, once)
            if "hedge" not in self.traded_times:
                put_chain = self.mkt.get_option_chain(expiry, "bull_put")
                hedge_opt = self.mkt.find_strike_by_delta(put_chain, HEDGE_DELTA, "buy")
                if hedge_opt:
                    hedge_price = hedge_opt.get("ask", 999) * 100
                    if hedge_price <= HEDGE_MAX_COST:
                        if self.eng.place_hedge(hedge_opt.get("code", ""), qty):
                            self.traded_times["hedge"] = True
                    else:
                        log.info(f"Tail hedge too expensive: ${hedge_price:.2f} > ${HEDGE_MAX_COST}")
        else:
            self.traded_times[time_key] = True
            pushover("SPX Bot", f"❌エントリー失敗 {time_key}ET {dir_label} 注文失敗")

    def check_exits(self):
        """Check P&L on open positions and apply profit/loss/force-close/VIX-spike rules."""
        now = datetime.datetime.now(ET)
        positions = self.eng.get_open_positions()

        # ── #4: VIX spike exit (dynamic ATR-based threshold) ──
        if ENABLE_VIX_SPIKE_EXIT and self.entry_vix is not None and positions:
            current_vix = self.mkt.get_vix()
            if current_vix is not None:
                # Use dynamic threshold from daily params if available
                dp = self._daily_params.get("dynamic_params", {})
                spike_threshold = dp.get("vix_spike_exit_pct", VIX_SPIKE_EXIT_PCT)
                vix_change_pct = (current_vix - self.entry_vix) / self.entry_vix
                if vix_change_pct >= spike_threshold:
                    entry_vix_val = self.entry_vix  # save before reset
                    msg = (f"VIX spike exit: {entry_vix_val:.1f} → {current_vix:.1f} "
                           f"(+{vix_change_pct:.1%} >= +{spike_threshold:.0%})")
                    log.warning(msg)
                    self.eng.close_all_positions("vix_spike_exit")
                    self.entry_vix = None  # Reset after exit
                    pushover("SPX Bot VIX急騰決済", msg, priority=1)
                    append_pnl_entry({
                        "event": "exit", "reason": "vix_spike_exit",
                        "vix_entry": entry_vix_val,
                        "vix_current": current_vix,
                        "vix_change_pct": round(vix_change_pct, 4),
                    })
                    return

        # Force close at 15:50
        if now.hour > FORCE_CLOSE_H or (now.hour == FORCE_CLOSE_H and now.minute >= FORCE_CLOSE_M):
            if positions:
                log.info("15:50 ET → force closing all positions")
                self.eng.close_all_positions("15:50_force_close")

            # Alert if still open at 15:55
            if now.minute >= 55:
                remaining = self.eng.get_open_positions()
                if remaining:
                    pushover(
                        "15:55 ポジション残存",
                        f"{len(remaining)}件のポジションが15:55以降も残存",
                        priority=1
                    )
            return

        for pos in positions:
            cost_basis = pos.get("cost_price", 0) * abs(pos.get("qty", 0)) * 100
            current_pl = pos.get("pl_val", 0)
            if cost_basis == 0:
                continue

            pl_ratio = current_pl / abs(cost_basis)

            # Dynamic profit target and stop loss
            hours_left = calc_hours_to_expiry()
            current_vix_for_stops = self.mkt.get_vix() or 18.0
            dyn_profit = dynamic_profit_target(current_vix_for_stops, hours_left)
            dyn_stop = dynamic_stop_loss_multiplier(current_vix_for_stops, hours_left)

            if pl_ratio >= dyn_profit:
                log.info(f"Profit target {pl_ratio:.1%} >= {dyn_profit:.0%} (dynamic) → closing {pos.get('code')}")
                self.eng.close_all_positions("profit_target")
                append_pnl_entry({
                    "event": "exit", "reason": "profit_target",
                    "code": pos.get("code", ""), "qty": pos.get("qty", 0),
                    "pnl_usd": round(float(current_pl), 2),
                    "pl_ratio": round(pl_ratio, 4),
                    "dynamic_profit_target": dyn_profit,
                    "dynamic_stop_loss": dyn_stop,
                })
                break
            elif pl_ratio <= -dyn_stop:
                log.info(f"Stop loss {pl_ratio:.1%} <= -{dyn_stop:.0%} (dynamic) → closing {pos.get('code')}")
                self.eng.close_all_positions("stop_loss")
                append_pnl_entry({
                    "event": "exit", "reason": "stop_loss",
                    "code": pos.get("code", ""), "qty": pos.get("qty", 0),
                    "pnl_usd": round(float(current_pl), 2),
                    "pl_ratio": round(pl_ratio, 4),
                    "dynamic_profit_target": dyn_profit,
                    "dynamic_stop_loss": dyn_stop,
                })
                break

    # ── Nightly helpers (20:00 ET = 9:00 JST) ────────────────────────────────

    def _notrade_reason_for_date(self, target: datetime.date) -> str:
        """Return human-readable no-trade reason for target date, or '' if tradeable."""
        day_after = target + datetime.timedelta(days=1)
        if day_after in US_HOLIDAYS:
            return f"翌日祝日({day_after})"
        if target.month in (3, 6, 9, 12) and target.weekday() == 4:
            first_day = target.replace(day=1)
            first_friday = first_day + datetime.timedelta(days=(4 - first_day.weekday()) % 7)
            third_friday = first_friday + datetime.timedelta(weeks=2)
            if target == third_friday:
                return "四半期OpEx"
        if EVENTS_FILE.exists():
            try:
                data = json.loads(EVENTS_FILE.read_text())
                fetched = datetime.date.fromisoformat(data["fetched"])
                if (target - fetched).days <= 7:
                    for ev in data.get("events", []):
                        return ev["keyword"].upper()
            except Exception:
                pass
        return ""

    def _get_lock_expiry_warning(self) -> str:
        """Return warning string if trade lock expires within 7 days, else ''."""
        try:
            if not TRADE_LOCK_FILE.exists():
                return ""
            data = json.loads(TRADE_LOCK_FILE.read_text())
            expiry_str = data.get("expiry", "")
            if not expiry_str:
                return ""
            expiry = datetime.date.fromisoformat(expiry_str)
            days_left = (expiry - datetime.datetime.now(ET).date()).days
            if days_left <= 7:
                return f"⚠️取引ロック残り{days_left}日・更新必要（期限:{expiry_str}）"
        except Exception:
            pass
        return ""

    def run_nightly_jst_check(self):
        """20:00 ET (= 9:00 JST) nightly check: notrade tomorrow + lock expiry."""
        # 'tomorrow' in ET = 'today' in JST (the upcoming trading session)
        tomorrow_et = (datetime.datetime.now(ET) + datetime.timedelta(days=1)).date()
        wd = tomorrow_et.weekday()

        # No-trade notification for the upcoming session
        if wd < 5:  # skip weekends
            reason = self._notrade_reason_for_date(tomorrow_et)
            if reason:
                pushover("SPX Bot", f"⏭️今日ノートレード {reason}")
                log.info(f"Nightly: notrade tomorrow ({tomorrow_et}): {reason}")

        # Lock expiry warning
        expiry_warn = self._get_lock_expiry_warning()
        if expiry_warn:
            pushover("SPX Bot", expiry_warn, priority=1)
            log.warning(f"Nightly: {expiry_warn}")

    def _export_monthly_pnl_csv(self):
        """1st of month: export pnl.json → reports/trades_YYYYMM.csv with USD/JPY rate."""
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            now_et = datetime.datetime.now(ET)
            # Export previous month
            first_this_month = now_et.date().replace(day=1)
            prev_month = first_this_month - datetime.timedelta(days=1)
            month_str = prev_month.strftime("%Y%m")

            # Fetch USD/JPY rate via yfinance (best-effort)
            usdjpy = 150.0
            try:
                import yfinance as yf
                ticker = yf.Ticker("JPY=X")
                hist = ticker.history(period="1d")
                if not hist.empty:
                    usdjpy = float(hist["Close"].iloc[-1])
                    log.info(f"USD/JPY rate: {usdjpy:.2f}")
            except Exception as e:
                log.warning(f"yfinance USD/JPY fetch failed: {e}")

            trades = load_pnl()
            month_prefix = prev_month.strftime("%Y-%m")
            month_trades = [t for t in trades if t.get("date", "").startswith(month_prefix)]

            if not month_trades:
                log.info(f"No trades in {month_prefix} to export")
                return

            csv_path = REPORTS_DIR / f"trades_{month_str}.csv"
            fieldnames = ["date", "ts", "event", "direction", "sell_strike", "buy_strike",
                          "qty", "net_credit", "pnl_usd", "pnl_jpy", "reason", "pl_ratio"]
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for t in month_trades:
                    t["usdjpy"] = usdjpy
                    pnl_usd = t.get("pnl_usd", 0) or 0
                    t["pnl_jpy"] = round(pnl_usd * usdjpy, 0)
                    writer.writerow(t)
            log.info(f"Monthly CSV exported: {csv_path} ({len(month_trades)} trades, USD/JPY={usdjpy:.2f})")
            pushover("SPX Bot", f"📄月次CSV出力: {csv_path.name} ({len(month_trades)}件, ¥{usdjpy:.1f}/$)")
        except Exception as e:
            log.warning(f"Monthly CSV export failed: {e}")

    def run_daily_summary_jst(self):
        """9:00 JST (= 20:00 ET) daily summary.
        Full report on issues; compact 1-line when all normal."""
        now_et = datetime.datetime.now(ET)
        jst = zoneinfo.ZoneInfo("Asia/Tokyo")
        now_jst = now_et.astimezone(jst)
        today_jst = now_jst.date()

        # ── 1. Yesterday's P&L (current ET session date, just ended at 16:00 ET)
        session_date = now_et.strftime("%Y-%m-%d")
        pnl_data = load_pnl()
        session_trades = [t for t in pnl_data if t.get("date") == session_date]
        entries  = [t for t in session_trades if t.get("event") == "entry"]
        exits    = [t for t in session_trades if t.get("event") == "exit"]
        session_pnl = sum(t.get("pnl_usd", 0) or 0 for t in exits)
        wins     = sum(1 for t in exits if (t.get("pnl_usd") or 0) > 0)
        losses   = len(exits) - wins

        # ── 2. Weekly P&L (past 5 ET trading days)
        week_start = now_et.date() - datetime.timedelta(days=5)
        week_trades = [t for t in pnl_data
                       if t.get("event") == "exit" and t.get("date", "") >= str(week_start)]
        weekly_pnl = sum(t.get("pnl_usd", 0) or 0 for t in week_trades)
        total_pnl  = sum(t.get("pnl_usd", 0) or 0
                         for t in pnl_data if t.get("event") == "exit")

        # ── 3. Memory warning flag
        mem_warn = ""
        try:
            if MEMORY_WARN_FILE.exists():
                mw = json.loads(MEMORY_WARN_FILE.read_text())
                if mw.get("count", 0) > 0:
                    mem_warn = f"⚠️メモリ警告{mw['count']}回(最大{mw.get('max_pct', 0):.0f}%)"
                MEMORY_WARN_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        # ── 4. Auto-recovery count (written by health_check GitHub Actions via SSH)
        recovery_warn = ""
        try:
            if RECOVERY_COUNT_FILE.exists():
                rc = json.loads(RECOVERY_COUNT_FILE.read_text())
                count = rc.get("count", 0)
                if count > 0:
                    recovery_warn = f"夜中に自動復旧{count}回"
                RECOVERY_COUNT_FILE.write_text(json.dumps({"count": 0}))
        except Exception:
            pass

        # ── 5. Today's trade plan (upcoming ET session)
        next_et = (now_et + datetime.timedelta(days=1)).date()
        notrade_reason = self._notrade_reason_for_date(next_et)
        if next_et.weekday() >= 5:
            plan_str = "週末休場"
        elif notrade_reason:
            plan_str = f"ノートレード({notrade_reason})"
        else:
            # ET entry times → JST (dynamically handles EDT/EST)
            jst = zoneinfo.ZoneInfo("Asia/Tokyo")
            jst_times = []
            for h, m in ENTRY_TIMES:
                et_dt = datetime.datetime.now(ET).replace(hour=h, minute=m)
                jst_dt = et_dt.astimezone(jst)
                jst_times.append(f"{jst_dt.hour}:{jst_dt.minute:02d}")
            plan_str = "・".join(jst_times) + " JST"

        # ── 6. Lock expiry warning
        lock_note = self._get_lock_expiry_warning()

        # ── Determine compact vs. full report
        has_trades = bool(entries or exits)
        has_issues = bool(mem_warn or recovery_warn or lock_note or
                          session_pnl != 0 or has_trades)

        if not has_issues:
            msg = f"✅全正常 | 今日:{plan_str}"
            pushover("SPX Bot", msg)
        else:
            lines = [f"📊SPX Bot日次 ({today_jst.strftime('%m/%d')} 09:00JST)"]
            if exits:
                lines.append(f"昨日: {len(entries)}エントリー {wins}勝{losses}敗 P&L:${session_pnl:+.0f}")
            elif entries:
                lines.append(f"昨日: {len(entries)}エントリー(決済未確認)")
            else:
                lines.append("昨日: エントリーなし")
            lines.append(f"今日予定: {plan_str}")
            if recovery_warn:
                lines.append(f"🔄{recovery_warn}")
            if mem_warn:
                lines.append(mem_warn)
            if lock_note:
                lines.append(lock_note)
            lines.append(f"週間: ${weekly_pnl:+.0f} / 累計: ${total_pnl:+.0f}")
            msg = "\n".join(lines)
            pushover("SPX Bot 日次レポート", msg)
        log.info(f"Daily summary sent: {msg[:100]}")

    def run_forever(self):
        log.info("=== SPX Bot starting ===")
        fetch_events_weekly()

        if not self.mkt.connect():
            log.error("Cannot connect to OpenD (quote context)")
            self.consecutive_start_failures += 1
            save_failures(self.consecutive_start_failures)
            if self.consecutive_start_failures >= STARTUP_FAIL_LIMIT:
                pushover(
                    f"Bot起動失敗{STARTUP_FAIL_LIMIT}回連続",
                    f"OpenD接続失敗が{self.consecutive_start_failures}回連続しています",
                    priority=1
                )
            return

        if not self.eng.connect():
            log.error("Cannot connect to OpenD (trade context)")
            self.consecutive_start_failures += 1
            save_failures(self.consecutive_start_failures)
            return

        # Reset failure counter on successful connect
        self.consecutive_start_failures = 0
        save_failures(0)
        log.info("Connected to OpenD successfully")

        # ── Startup margin check ──────────────────────────────────────────
        self._margin_ok = self.eng.check_startup_margin()

        try:
            while True:
                now = datetime.datetime.now(ET)
                h, m = now.hour, now.minute

                # 20:00 ET (= 9:00 JST): daily summary + notrade/lock check
                if h == 20 and m == 0 and "nightly_checked" not in self.traded_times:
                    self.run_daily_summary_jst()
                    self.run_nightly_jst_check()
                    self.traded_times["nightly_checked"] = True
                    self.traded_times["daily_summary"] = True
                    # Monthly CSV export on 1st of JST month
                    jst = zoneinfo.ZoneInfo("Asia/Tokyo")
                    now_jst = now.astimezone(jst)
                    if now_jst.day == 1 and "monthly_export" not in self.traded_times:
                        self._export_monthly_pnl_csv()
                        self.traded_times["monthly_export"] = True

                # Hourly memory check
                if m == 0 and f"memcheck_{h}" not in self.traded_times:
                    check_memory_usage()
                    self.traded_times[f"memcheck_{h}"] = True

                # Premarket assessment: run once at 9:25 ET (5 min before open)
                if h == 9 and 25 <= m < 30 and not self._premarket_done:
                    log.info("=== Premarket Assessment ===")
                    cash = self.eng.get_account_cash()
                    self._daily_params = daily_premarket_assessment(self.mkt, cash)
                    self._premarket_done = True
                    dp = self._daily_params
                    env_msg = (
                        f"Premarket: score={dp['env_score']}/100 "
                        f"grade={dp['env_grade']} scalar={dp['env_size_scalar']:.2f}\n"
                        f"Phase={dp['dynamic_params'].get('fund_phase_name', 'N/A')} "
                        f"VIXgate={dp['dynamic_params'].get('vix_gate', 25):.1f} "
                        f"Stop={dp['dynamic_params'].get('stop_loss_mult', 2.0):.2f}x "
                        f"Profit={dp['dynamic_params'].get('profit_target', 0.5):.0%}"
                    )
                    log.info(env_msg)
                    if dp["env_score"] < 30:
                        pushover("SPX Bot", f"Premarket: grade={dp['env_grade']} score={dp['env_score']} -> NO TRADE today")

                # Market hours: 9:30-16:00 ET
                if not (9 <= h < 16 or (h == 9 and m >= 30)):
                    if h >= 16:
                        # EOD reset
                        if self.traded_times:
                            log.info("EOD -> resetting traded_times")
                            self.traded_times = {}
                            self.entry_vix = None  # #4: Reset entry VIX for next session
                            self._premarket_done = False  # Reset for next day
                            self._daily_params = {}
                    time.sleep(10)
                    continue

                # No-trade check
                if is_notrade_today():
                    log.info("No-trade day → sleeping 1h")
                    time.sleep(3600)
                    continue

                # Entry check (skip if margin insufficient or risk limits hit)
                if self.should_enter(h, m):
                    # ── #6: Weekly loss limit check ─────────────────────────
                    if check_weekly_loss_limit():
                        log.warning("Entry skipped: weekly loss limit reached")
                        pushover("SPX Bot", f"⏭️ノートレード {h}:{m:02d}ET: 週間損失上限到達")
                        self.traded_times[f"{h}:{m:02d}"] = True
                    # ── #7: Daily profit cap check ──────────────────────────
                    elif check_daily_profit_cap(self.eng.get_account_cash()):
                        log.info("Entry skipped: daily profit cap reached")
                        pushover("SPX Bot", f"⏭️打ち止め {h}:{m:02d}ET: 日次利益上限+{DAILY_PROFIT_CAP_PCT:.0%}")
                        self.traded_times[f"{h}:{m:02d}"] = True
                    elif getattr(self, "_margin_ok", True):
                        self.run_entry()
                    else:
                        log.warning("Entry skipped: startup margin check failed")
                        self.traded_times[f"{h}:{m:02d}"] = True

                # Exit check
                self.check_exits()

                time.sleep(10)

        except KeyboardInterrupt:
            log.info("Bot stopped by user")
        except Exception as e:
            log.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
            pushover("Bot クラッシュ", str(e)[:200], priority=1)
        finally:
            self.mkt.close()
            self.eng.close()


# ══════════════════════════════════════════════════════════════════════════════
# Improvement #12–#16: New standalone filter functions
# These are added as new functions to avoid conflicts with #1–#11 changes.
# Each returns a dict with 'signal' and optional metadata.
# ══════════════════════════════════════════════════════════════════════════════

def check_volume_spike(quote_ctx) -> dict:
    """#12: Detect volume spike — last 5min volume > 2x of 20-day avg 5min volume.

    Uses futu get_cur_kline (K_5M) for current session and
    request_history_kline (K_5M) for 20-day historical average.

    Returns:
        {'spike': True/False, 'ratio': float, 'reason': str}
        spike=True means entry should be DELAYED.
    """
    result = {"spike": False, "ratio": 0.0, "reason": ""}

    if not ENABLE_VOLUME_SPIKE:
        result["reason"] = "volume spike check disabled"
        return result

    if not FUTU_AVAILABLE or quote_ctx is None:
        # Dry-run: no spike
        result["reason"] = "dry-run mode, no volume data"
        log.info(f"#12 VolumeSpike: {result['reason']}")
        return result

    try:
        import futu as _ft

        # Current session: get last few 5-min bars
        ret, cur_data = quote_ctx.get_cur_kline("US.SPY", num=2, ktype=_ft.KLType.K_5M)
        if ret != RET_OK or cur_data.empty:
            result["reason"] = "current 5min kline unavailable"
            log.warning(f"#12 VolumeSpike: {result['reason']}")
            return result

        last_5min_vol = float(cur_data["volume"].iloc[-1])

        # Historical: 20 trading days × ~78 bars/day of 5min = ~1560 bars
        # We just need enough to get 20-day average per-bar volume
        ret2, hist_data, _ = quote_ctx.request_history_kline(
            "US.SPY", start="2024-01-01",
            ktype=_ft.KLType.K_5M, max_count=1000
        )
        if ret2 != RET_OK or hist_data.empty or len(hist_data) < 100:
            result["reason"] = "insufficient historical 5min data"
            log.warning(f"#12 VolumeSpike: {result['reason']}")
            return result

        # Average volume per 5min bar over the available history
        avg_5min_vol = float(hist_data["volume"].mean())

        if avg_5min_vol <= 0:
            result["reason"] = "average volume is zero"
            log.warning(f"#12 VolumeSpike: {result['reason']}")
            return result

        ratio = last_5min_vol / avg_5min_vol
        is_spike = ratio >= VOLUME_SPIKE_MULT

        result["spike"] = is_spike
        result["ratio"] = round(ratio, 2)
        result["last_vol"] = last_5min_vol
        result["avg_vol"] = round(avg_5min_vol, 0)

        if is_spike:
            result["reason"] = (
                f"SPIKE: last 5min vol={last_5min_vol:,.0f} "
                f"is {ratio:.1f}x avg ({avg_5min_vol:,.0f})"
            )
        else:
            result["reason"] = (
                f"normal: last 5min vol={last_5min_vol:,.0f} "
                f"is {ratio:.1f}x avg ({avg_5min_vol:,.0f})"
            )

        log.info(f"#12 VolumeSpike: {result['reason']}")
        return result

    except Exception as e:
        result["reason"] = f"error: {e}"
        log.warning(f"#12 VolumeSpike: {result['reason']}")
        return result


def _fetch_squeezemetrics_data() -> dict:
    """Shared helper: download and cache squeezemetrics DIX.csv (DIX + GEX).

    Returns latest row as dict: {'date': str, 'price': float, 'dix': float, 'gex': float}
    or empty dict on failure. Caches for 4 hours to avoid repeated downloads.
    """
    try:
        # Check cache first
        if GEX_CACHE_FILE.exists():
            cache = json.loads(GEX_CACHE_FILE.read_text())
            cached_ts = cache.get("fetched_ts", 0)
            if time.time() - cached_ts < 14400:  # 4 hours
                log.info(f"#13/#14 squeezemetrics: using cache (age={int(time.time()-cached_ts)}s)")
                return cache.get("data", {})

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        r = requests.get(GEX_CSV_URL, timeout=15, headers=headers)
        r.raise_for_status()

        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            log.warning("#13/#14 squeezemetrics: CSV has no data rows")
            return {}

        # Parse header and last row
        header = lines[0].strip().split(",")
        last_row = lines[-1].strip().split(",")
        if len(last_row) < 4:
            log.warning("#13/#14 squeezemetrics: last row incomplete")
            return {}

        data = {
            "date": last_row[0],
            "price": float(last_row[1]),
            "dix": float(last_row[2]),
            "gex": float(last_row[3]),
        }

        # Cache it
        try:
            GEX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            GEX_CACHE_FILE.write_text(json.dumps({
                "fetched_ts": time.time(),
                "data": data,
            }))
        except Exception as e:
            log.warning(f"#13/#14 cache write failed: {e}")

        log.info(f"#13/#14 squeezemetrics fetched: date={data['date']} DIX={data['dix']:.4f} GEX={data['gex']:,.0f}")
        return data

    except Exception as e:
        log.warning(f"#13/#14 squeezemetrics fetch failed: {e}")
        return {}


def check_gex_filter() -> dict:
    """#13: GEX (Gamma Exposure) filter from squeezemetrics.com free CSV.

    Positive GEX → dealers hedge against moves → range-bound → credit spreads favorable.
    Negative GEX → dealers amplify moves → trending → credit spreads risky.

    Returns:
        {'skip': True/False, 'gex': float, 'gex_positive': bool, 'reason': str}
        skip=True means entry should be SKIPPED.
    """
    result = {"skip": False, "gex": 0.0, "gex_positive": True, "reason": ""}

    if not ENABLE_GEX_FILTER:
        result["reason"] = "GEX filter disabled"
        return result

    try:
        data = _fetch_squeezemetrics_data()
        if not data:
            result["reason"] = "squeezemetrics data unavailable"
            log.info(f"#13 GEX: {result['reason']}")
            return result

        gex = data["gex"]
        result["gex"] = gex
        result["gex_positive"] = gex > 0
        result["data_date"] = data["date"]

        if gex < 0 and GEX_NEGATIVE_SKIP:
            result["skip"] = True
            result["reason"] = f"NEGATIVE GEX={gex:,.0f} on {data['date']} → skip (dealers amplify moves)"
        elif gex < 0:
            result["reason"] = f"negative GEX={gex:,.0f} on {data['date']} (warning only, skip disabled)"
        else:
            result["reason"] = f"positive GEX={gex:,.0f} on {data['date']} → range-bound favorable"

        log.info(f"#13 GEX: {result['reason']}")
        return result

    except Exception as e:
        result["reason"] = f"error: {e}"
        log.warning(f"#13 GEX: {result['reason']}")
        return result


def check_dix_pcr() -> dict:
    """#14: DIX (Dark Index) from squeezemetrics + directional bias.

    DIX > 45% → institutional buying → bull put favored.
    DIX < 40% → institutional selling → bear call favored.

    Put/Call Ratio: not available via free API (CBOE/yfinance blocked).
    Instead, we use DIX as the sole dark pool sentiment indicator.

    Returns:
        {'dix': float, 'bias': str, 'reason': str}
        bias: 'bull_put', 'bear_call', or 'neutral'
    """
    result = {"dix": 0.0, "bias": "neutral", "reason": ""}

    if not ENABLE_DIX_PCR:
        result["reason"] = "DIX/PCR filter disabled"
        return result

    try:
        data = _fetch_squeezemetrics_data()
        if not data:
            result["reason"] = "squeezemetrics data unavailable"
            log.info(f"#14 DIX: {result['reason']}")
            return result

        dix = data["dix"]
        result["dix"] = round(dix, 4)
        result["data_date"] = data["date"]

        if dix > DIX_BULL_THRESHOLD:
            result["bias"] = "bull_put"
            result["reason"] = (
                f"DIX={dix:.2%} > {DIX_BULL_THRESHOLD:.0%} on {data['date']} "
                f"→ institutional buying → bull_put favored"
            )
        elif dix < DIX_BEAR_THRESHOLD:
            result["bias"] = "bear_call"
            result["reason"] = (
                f"DIX={dix:.2%} < {DIX_BEAR_THRESHOLD:.0%} on {data['date']} "
                f"→ institutional selling → bear_call favored"
            )
        else:
            result["reason"] = (
                f"DIX={dix:.2%} on {data['date']} "
                f"(between {DIX_BEAR_THRESHOLD:.0%}-{DIX_BULL_THRESHOLD:.0%}) → neutral"
            )

        log.info(f"#14 DIX: {result['reason']}")
        return result

    except Exception as e:
        result["reason"] = f"error: {e}"
        log.warning(f"#14 DIX: {result['reason']}")
        return result


def check_vwap_direction(quote_ctx) -> dict:
    """#15: VWAP-based direction — SPY above or below intraday VWAP.

    Uses futu get_rt_data which provides avg_price (= VWAP) per minute.
    Fallback: compute VWAP from 1-min kline data (price * volume / cumulative volume).

    SPY > VWAP → buyers in control → bull put confidence UP.
    SPY < VWAP → sellers in control → bear call confidence UP.

    Returns:
        {'above_vwap': bool/None, 'spy_price': float, 'vwap': float,
         'bias': str, 'reason': str}
        bias: 'bull_put', 'bear_call', or 'neutral'
    """
    result = {"above_vwap": None, "spy_price": 0.0, "vwap": 0.0,
              "bias": "neutral", "reason": ""}

    if not ENABLE_VWAP_DIRECTION:
        result["reason"] = "VWAP direction filter disabled"
        return result

    if not FUTU_AVAILABLE or quote_ctx is None:
        result["reason"] = "dry-run mode, no VWAP data"
        log.info(f"#15 VWAP: {result['reason']}")
        return result

    try:
        import futu as _ft

        # Method 1: get_rt_data provides avg_price (VWAP-like) per minute
        ret, rt_data = quote_ctx.get_rt_data("US.SPY")
        if ret == RET_OK and not rt_data.empty and len(rt_data) >= 5:
            last_row = rt_data.iloc[-1]
            spy_price = float(last_row["cur_price"])
            vwap = float(last_row["avg_price"])

            if vwap > 0:
                result["spy_price"] = round(spy_price, 2)
                result["vwap"] = round(vwap, 2)
                result["above_vwap"] = spy_price > vwap
                diff_pct = (spy_price - vwap) / vwap * 100

                if spy_price > vwap:
                    result["bias"] = "bull_put"
                    result["reason"] = (
                        f"SPY={spy_price:.2f} > VWAP={vwap:.2f} "
                        f"(+{diff_pct:.2f}%) → buyers in control → bull_put"
                    )
                else:
                    result["bias"] = "bear_call"
                    result["reason"] = (
                        f"SPY={spy_price:.2f} < VWAP={vwap:.2f} "
                        f"({diff_pct:.2f}%) → sellers in control → bear_call"
                    )

                log.info(f"#15 VWAP: {result['reason']}")
                return result

        # Method 2: Compute VWAP from 1-min kline data
        ret2, kl_data = quote_ctx.get_cur_kline("US.SPY", num=200, ktype=_ft.KLType.K_1M)
        if ret2 == RET_OK and not kl_data.empty and len(kl_data) >= 10:
            # VWAP = cumulative(typical_price * volume) / cumulative(volume)
            tp = (kl_data["high"] + kl_data["low"] + kl_data["close"]) / 3.0
            cum_tpv = (tp * kl_data["volume"]).cumsum()
            cum_vol = kl_data["volume"].cumsum()
            vwap_series = cum_tpv / cum_vol
            vwap = float(vwap_series.iloc[-1])
            spy_price = float(kl_data["close"].iloc[-1])

            result["spy_price"] = round(spy_price, 2)
            result["vwap"] = round(vwap, 2)
            result["above_vwap"] = spy_price > vwap
            diff_pct = (spy_price - vwap) / vwap * 100

            if spy_price > vwap:
                result["bias"] = "bull_put"
                result["reason"] = (
                    f"SPY={spy_price:.2f} > VWAP(calc)={vwap:.2f} "
                    f"(+{diff_pct:.2f}%) → bull_put"
                )
            else:
                result["bias"] = "bear_call"
                result["reason"] = (
                    f"SPY={spy_price:.2f} < VWAP(calc)={vwap:.2f} "
                    f"({diff_pct:.2f}%) → bear_call"
                )

            log.info(f"#15 VWAP: {result['reason']}")
            return result

        result["reason"] = "VWAP data unavailable from both rt_data and kline"
        log.warning(f"#15 VWAP: {result['reason']}")
        return result

    except Exception as e:
        result["reason"] = f"error: {e}"
        log.warning(f"#15 VWAP: {result['reason']}")
        return result


def check_market_breadth() -> dict:
    """#16: Market breadth divergence detection.

    Uses RSP (Invesco S&P 500 Equal Weight ETF) vs SPY ratio as breadth proxy.
    NYSE Advance/Decline data is not freely available via yfinance.

    Logic: If SPY rises but RSP/SPY ratio declines over BREADTH_LOOKBACK_DAYS,
    it indicates narrowing market breadth = hidden weakness = danger signal.

    Returns:
        {'divergence': True/False, 'spy_chg': float, 'ratio_chg': float,
         'signal': str, 'reason': str}
        divergence=True → SPY up but breadth weakening → risky for credit spreads.
    """
    result = {"divergence": False, "spy_chg": 0.0, "ratio_chg": 0.0,
              "signal": "normal", "reason": ""}

    if not ENABLE_MARKET_BREADTH:
        result["reason"] = "market breadth check disabled"
        return result

    try:
        import yfinance as yf

        lookback = BREADTH_LOOKBACK_DAYS + 5  # extra buffer for non-trading days
        spy_hist = yf.Ticker("SPY").history(period=f"{lookback}d")
        rsp_hist = yf.Ticker("RSP").history(period=f"{lookback}d")

        if spy_hist.empty or rsp_hist.empty:
            result["reason"] = "SPY or RSP history unavailable"
            log.warning(f"#16 Breadth: {result['reason']}")
            return result

        # Align dates
        spy_close = spy_hist["Close"].tail(BREADTH_LOOKBACK_DAYS + 1)
        rsp_close = rsp_hist["Close"].tail(BREADTH_LOOKBACK_DAYS + 1)

        if len(spy_close) < 2 or len(rsp_close) < 2:
            result["reason"] = "insufficient data for breadth calculation"
            log.warning(f"#16 Breadth: {result['reason']}")
            return result

        # SPY change over lookback period
        spy_start = float(spy_close.iloc[0])
        spy_end = float(spy_close.iloc[-1])
        spy_chg = (spy_end - spy_start) / spy_start

        # RSP/SPY ratio change
        ratio_start = float(rsp_close.iloc[0]) / spy_start
        ratio_end = float(rsp_close.iloc[-1]) / spy_end
        ratio_chg = (ratio_end - ratio_start) / ratio_start

        result["spy_chg"] = round(spy_chg, 4)
        result["ratio_chg"] = round(ratio_chg, 4)
        result["spy_price"] = round(spy_end, 2)
        result["rsp_price"] = round(float(rsp_close.iloc[-1]), 2)

        # Divergence: SPY up but breadth (RSP/SPY ratio) declining
        if spy_chg > 0 and ratio_chg < -BREADTH_DIVERGE_THRESHOLD:
            result["divergence"] = True
            result["signal"] = "danger"
            result["reason"] = (
                f"DIVERGENCE: SPY +{spy_chg:.2%} but RSP/SPY ratio {ratio_chg:.2%} "
                f"over {BREADTH_LOOKBACK_DAYS}d → narrowing breadth → danger"
            )
        elif spy_chg < 0 and ratio_chg > BREADTH_DIVERGE_THRESHOLD:
            result["signal"] = "positive_divergence"
            result["reason"] = (
                f"Positive divergence: SPY {spy_chg:.2%} but RSP/SPY ratio +{ratio_chg:.2%} "
                f"→ breadth stronger than headline → potential recovery"
            )
        else:
            result["reason"] = (
                f"No divergence: SPY {spy_chg:+.2%}, RSP/SPY ratio {ratio_chg:+.2%} "
                f"over {BREADTH_LOOKBACK_DAYS}d"
            )

        log.info(f"#16 Breadth: {result['reason']}")
        return result

    except Exception as e:
        result["reason"] = f"error: {e}"
        log.warning(f"#16 Breadth: {result['reason']}")
        return result


if __name__ == "__main__":
    bot = SPXBot()
    bot.run_forever()
