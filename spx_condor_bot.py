#!/usr/bin/env python3
"""
spx_condor_bot.py — SPY Credit Spread Bot (0DTE) v2
Broker  : moomoo / Futu Securities Japan (口座2756)

戦略アーキテクチャ:
  2タクティクス × VIX環境 × IVR補正 × VIXスパイク回復

  [STANDARD] 10:30 ET — SMA方向ベースCS
    VIX < 22: 通常フル運用 (delta 0.25)
    VIX 22-35: 縮小運用 (delta 0.20)
    VIX >= 35: 標準エントリーなし → ORFに委ねる

  [ORF] 13:00 ET — Opening Range Fade CS
    VIX >= 22かつ寄り付き30分で|move| >= 0.8%のとき発動
    方向: 寄り付き下落 → Put CS / 寄り付き上昇 → Call CS
    VIX 22-35: delta 0.20 / VIX 35-50: delta 0.15 / VIX > 50: 停止

  [IVR補正] 両タクティクスに適用
    IVR > 75 → delta +0.05 (高IV環境でより積極的)
    IVR < 25 → delta -0.05 (低IV環境で守り)

  [VIXスパイク回復] 前日VIX+3以上のスパイク翌日
    当日エントリーに delta +0.05 上乗せ (回復バイアス)

Flags:
  --paper          ペーパー取引 (TrdEnv.SIMULATE)
  --test-connect   接続テストして終了
  --demo-compare   7変数パラメータシミュレーション (発注なし)

NOTE: LaunchAgent 22:00 JST (= 9:00 EDT) 起動必須
      OpenD ログイン済み・127.0.0.1:11111 動作中であること
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

# ── .env loader ────────────────────────────────────────────────────────────────
def _load_env_file():
    for candidate in [Path("/root/spxbot/.env"), Path(__file__).parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
            break

_load_env_file()

# ── Path constants ─────────────────────────────────────────────────────────────
_BASE_DIR = Path(os.environ.get("SPX_DATA_DIR", Path(__file__).parent / "data"))
LOG_DIR   = Path(os.environ.get("SPX_LOG_DIR", _BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "condor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("spx_condor")

# ── Timezone ────────────────────────────────────────────────────────────────────
ET  = zoneinfo.ZoneInfo("America/New_York")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# ── Credentials ────────────────────────────────────────────────────────────────
PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER  = "u2cevk8nktib3sr148rw2hs78ecvux"
TRADE_PASSWORD = os.environ.get("TRADE_PASSWORD", "")

# ── Bot identity ────────────────────────────────────────────────────────────────
STRATEGY_NAME   = "SPY Credit Spread 0DTE v2"
UNDERLYING_CODE = "US.SPY"

OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111

# ══════════════════════════════════════════════════════════════════════════════
# Strategy parameters
# ══════════════════════════════════════════════════════════════════════════════

# ── Standard entry (10:30 ET, SMA direction) ──────────────────────────────────
# dict key = VIX upper bound (exclusive). None = no trade.
STANDARD_PARAMS = {
    22:  {"delta": 0.25, "width": 10, "capital_pct": 0.55},  # VIX < 22: normal
    35:  {"delta": 0.20, "width": 10, "capital_pct": 0.40},  # VIX 22-35: elevated
    999: None,                                                 # VIX >= 35: skip standard
}

# ── ORF entry (13:00 ET, opening-range-fade direction) ───────────────────────
# Activated only when VIX >= ORF_VIX_THRESHOLD and |orf_move| >= ORF_MOVE_THRESHOLD
ORF_PARAMS = {
    35:  {"delta": 0.20, "width": 10, "capital_pct": 0.40},  # VIX 22-35 + ORF
    50:  {"delta": 0.15, "width": 10, "capital_pct": 0.30},  # VIX 35-50 + ORF
    999: None,                                                 # VIX >= 50: halt
}

# ── ORF trigger conditions ─────────────────────────────────────────────────────
ORF_VIX_THRESHOLD  = 22     # VIX >= this → ORF check at 10:00 ET
ORF_MOVE_THRESHOLD = 0.008  # |move| >= 0.8% in first 30min → ORF triggered

# ── IVR (IV Rank) delta adjustment ─────────────────────────────────────────────
IVR_HIGH = 75    # IVR > 75 → +0.05 delta (high premium environment)
IVR_LOW  = 25    # IVR < 25 → -0.05 delta (low premium environment)

# ── VIX spike recovery ─────────────────────────────────────────────────────────
VIX_SPIKE_THRESHOLD = 3.0  # previous day VIX rose >= 3 → recovery day → +0.05 delta

# ── Profit target & stop loss ─────────────────────────────────────────────────
PROFIT_TARGET  = 0.80  # 80% of net credit
STOP_LOSS_MULT = 1.00  # 100% of net credit (= spread width cap)

# ── Position limits ───────────────────────────────────────────────────────────
MAX_QTY                = 3      # 3 contracts max (gap protection)
SMALL_ACCOUNT_USD      = 15000  # below this: 1 contract max
MAX_CONSECUTIVE_LOSSES = 3      # halt after 3 straight losses

# ── Entry / exit windows (ET) ─────────────────────────────────────────────────
STANDARD_ENTRY_H  = 10
STANDARD_ENTRY_M  = 30
ORF_CHECK_H       = 10
ORF_CHECK_M       = 0
ORF_ENTRY_H       = 13
ORF_ENTRY_M       = 0
FORCE_CLOSE_H     = 15
FORCE_CLOSE_M     = 50

# ── SMA ───────────────────────────────────────────────────────────────────────
SMA_PERIOD = 20

# ══════════════════════════════════════════════════════════════════════════════
# File paths
# ══════════════════════════════════════════════════════════════════════════════
PNL_FILE            = _BASE_DIR / "condor_pnl.json"
EVENTS_FILE         = _BASE_DIR / "events.json"
SMA_CACHE_FILE      = _BASE_DIR / "sma_cache.json"
IVR_CACHE_FILE      = _BASE_DIR / "ivr_cache.json"
VIX_SPIKE_FILE      = _BASE_DIR / "vix_spike.json"
FAILURES_FILE       = _BASE_DIR / "spx_condor_failures.json"
MEMORY_WARN_FILE    = _BASE_DIR / "condor_memory_warn.json"
RECOVERY_COUNT_FILE = _BASE_DIR / "recovery_count.json"
DEMO_LOG_FILE       = LOG_DIR / "demo_compare.log"
REPORTS_DIR         = _BASE_DIR / "reports"
MEMORY_WARN_PCT     = 80

# ── NYSE holidays 2025-2027 ────────────────────────────────────────────────────
US_HOLIDAYS = {
    datetime.date(2025, 1, 1),  datetime.date(2025, 1, 20), datetime.date(2025, 2, 17),
    datetime.date(2025, 4, 18), datetime.date(2025, 5, 26), datetime.date(2025, 6, 19),
    datetime.date(2025, 7, 4),  datetime.date(2025, 9, 1),  datetime.date(2025, 11, 27),
    datetime.date(2025, 12, 25),
    datetime.date(2026, 1, 1),  datetime.date(2026, 1, 19), datetime.date(2026, 2, 16),
    datetime.date(2026, 4, 3),  datetime.date(2026, 5, 25), datetime.date(2026, 6, 19),
    datetime.date(2026, 7, 3),  datetime.date(2026, 9, 7),  datetime.date(2026, 11, 26),
    datetime.date(2026, 11, 27),datetime.date(2026, 12, 25),
    datetime.date(2027, 1, 1),  datetime.date(2027, 1, 18), datetime.date(2027, 2, 15),
    datetime.date(2027, 3, 26), datetime.date(2027, 5, 31), datetime.date(2027, 6, 18),
    datetime.date(2027, 7, 5),  datetime.date(2027, 9, 6),  datetime.date(2027, 11, 25),
    datetime.date(2027, 12, 24),
}

NOTRADE_KEYWORDS = ["fomc", "cpi", "nfp", "non-farm", "opex", "quadruple",
                    "pce", "gdp", "jobless", "claims"]

# ── futu import guard ─────────────────────────────────────────────────────────
FUTU_AVAILABLE = False
try:
    from futu import (OpenQuoteContext, OpenSecTradeContext,
                      TrdMarket, TrdEnv, TrdSide, OrderType,
                      RET_OK, SecurityFirm)
    import futu as ft
    FUTU_AVAILABLE = True
except ImportError:
    log.warning("futu-api not installed; running in dry-run mode")


# ══════════════════════════════════════════════════════════════════════════════
# Utility functions
# ══════════════════════════════════════════════════════════════════════════════

def pushover(title: str, message: str, priority: int = 0):
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                  "title": title, "message": message, "priority": priority},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"pushover: {e}")


def load_pnl() -> list:
    try:
        if PNL_FILE.exists():
            return json.loads(PNL_FILE.read_text()).get("trades", [])
    except Exception:
        pass
    return []


def append_pnl_entry(record: dict):
    try:
        PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
        trades = load_pnl()
        record.setdefault("date", datetime.datetime.now(ET).strftime("%Y-%m-%d"))
        record.setdefault("ts",   datetime.datetime.now(ET).isoformat())
        trades.append(record)
        PNL_FILE.write_text(json.dumps({"trades": trades}, indent=2))
    except Exception as e:
        log.warning(f"append_pnl: {e}")


def append_monthly_csv(record: dict):
    try:
        from pathlib import Path as _P
        _P(LOG_DIR).mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(ET)
        csv_path = LOG_DIR / f"condor_{now.strftime('%Y-%m')}.csv"
        fieldnames = ["timestamp", "expiry", "direction", "sell_strike", "buy_strike",
                      "qty", "net_credit", "result", "tactic"]
        exists = csv_path.exists()
        with open(csv_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
                w.writeheader()
            record.setdefault("timestamp", now.isoformat())
            w.writerow(record)
    except Exception as e:
        log.warning(f"csv append: {e}")


def load_failures() -> int:
    try:
        if FAILURES_FILE.exists():
            return int(json.loads(FAILURES_FILE.read_text()).get("count", 0))
    except Exception:
        pass
    return 0


def save_failures(count: int):
    try:
        FAILURES_FILE.write_text(json.dumps({"count": count}))
    except Exception:
        pass


def check_consecutive_losses() -> bool:
    trades = load_pnl()
    exits = [t for t in trades if t.get("event") == "exit"]
    recent = exits[-MAX_CONSECUTIVE_LOSSES:]
    if len(recent) < MAX_CONSECUTIVE_LOSSES:
        return False
    if all((t.get("pnl_usd", 0) or 0) < 0 for t in recent):
        pushover("SPY Credit Spread", f"連続{MAX_CONSECUTIVE_LOSSES}敗 → 本日停止", priority=1)
        return True
    return False


def check_memory_usage():
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        if usage > MEMORY_WARN_PCT:
            data = {}
            if MEMORY_WARN_FILE.exists():
                data = json.loads(MEMORY_WARN_FILE.read_text())
            data["count"] = data.get("count", 0) + 1
            data["max_pct"] = max(float(data.get("max_pct", 0)), usage)
            data["last"] = datetime.datetime.now(ET).isoformat()
            MEMORY_WARN_FILE.write_text(json.dumps(data))
    except Exception:
        pass


# ── VIX spike cache (persist across sessions) ─────────────────────────────────
def save_vix_spike_data(vix: float, spike_for_tomorrow: bool):
    """Save today's VIX and whether tomorrow is a recovery day."""
    try:
        VIX_SPIKE_FILE.parent.mkdir(parents=True, exist_ok=True)
        VIX_SPIKE_FILE.write_text(json.dumps({
            "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
            "vix":  vix,
            "spike_for_tomorrow": spike_for_tomorrow,
        }))
    except Exception as e:
        log.warning(f"vix spike save: {e}")


def is_recovery_day() -> bool:
    """True if yesterday had VIX spike >= VIX_SPIKE_THRESHOLD → today is likely a bounce day."""
    try:
        if not VIX_SPIKE_FILE.exists():
            return False
        data = json.loads(VIX_SPIKE_FILE.read_text())
        cache_date = datetime.date.fromisoformat(data["date"])
        today = datetime.datetime.now(ET).date()
        # Accept cache from up to 4 calendar days ago (covers weekends)
        if (today - cache_date).days > 4:
            return False
        return bool(data.get("spike_for_tomorrow", False))
    except Exception:
        return False


def get_yesterday_vix() -> Optional[float]:
    try:
        if VIX_SPIKE_FILE.exists():
            data = json.loads(VIX_SPIKE_FILE.read_text())
            return float(data.get("vix", 0)) or None
    except Exception:
        pass
    return None


# ── IVR (IV Rank) cache ───────────────────────────────────────────────────────
def load_ivr_cache() -> Optional[float]:
    try:
        if IVR_CACHE_FILE.exists():
            data = json.loads(IVR_CACHE_FILE.read_text())
            cache_date = datetime.date.fromisoformat(data["date"])
            if (datetime.datetime.now(ET).date() - cache_date).days <= 1:
                return float(data["ivr"])
    except Exception:
        pass
    return None


def save_ivr_cache(ivr: float):
    try:
        IVR_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        IVR_CACHE_FILE.write_text(json.dumps({
            "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
            "ivr": ivr,
        }))
    except Exception as e:
        log.warning(f"ivr cache save: {e}")


# ── VIX params selector ────────────────────────────────────────────────────────
def get_params(vix: float, params_table: dict) -> Optional[dict]:
    """Return params for given VIX from a params table. None = no trade."""
    for vix_max in sorted(params_table.keys()):
        if vix < vix_max:
            return params_table[vix_max]
    return None


def apply_ivr_delta(params: dict, ivr: Optional[float]) -> dict:
    """Apply IVR delta adjustment to a copy of params. Returns new dict."""
    if ivr is None:
        return params
    params = dict(params)
    if ivr > IVR_HIGH:
        params["delta"] = round(min(params["delta"] + 0.05, 0.40), 2)
        log.info(f"IVR={ivr:.0f} > {IVR_HIGH} → delta boosted to {params['delta']}")
    elif ivr < IVR_LOW:
        params["delta"] = round(max(params["delta"] - 0.05, 0.10), 2)
        log.info(f"IVR={ivr:.0f} < {IVR_LOW} → delta reduced to {params['delta']}")
    return params


def apply_recovery_delta(params: dict) -> dict:
    """Boost delta on recovery day (day after VIX spike)."""
    if not is_recovery_day():
        return params
    params = dict(params)
    params["delta"] = round(min(params["delta"] + 0.05, 0.40), 2)
    log.info(f"Recovery day (VIX spike yesterday) → delta boosted to {params['delta']}")
    return params


def calc_qty(cash: float, params: dict) -> int:
    margin = params["width"] * 100
    max_by_capital = int(cash * params["capital_pct"] / margin)
    if cash < SMALL_ACCOUNT_USD:
        return max(1, min(max_by_capital, 1))
    return max(1, min(max_by_capital, MAX_QTY))


# ── No-trade day checks ────────────────────────────────────────────────────────
def fetch_events_weekly():
    now = datetime.datetime.now(ET)
    if now.weekday() != 0:
        return
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(url, timeout=10)
        events_raw = resp.json()
        events = []
        for ev in events_raw:
            title = ev.get("title", "").lower()
            country = ev.get("country", "").lower()
            impact = ev.get("impact", "").lower()
            if country == "usd" and impact in ("high", "medium"):
                if any(kw in title for kw in NOTRADE_KEYWORDS):
                    events.append({"date": ev.get("date", ""), "keyword": title})
        EVENTS_FILE.write_text(json.dumps({"fetched": now.date().isoformat(), "events": events}))
        log.info(f"Events fetched: {len(events)} high-impact USD events")
    except Exception as e:
        log.warning(f"fetch_events: {e}")


def is_notrade_today() -> bool:
    today    = datetime.datetime.now(ET).date()
    tomorrow = today + datetime.timedelta(days=1)
    # Holiday eve
    if tomorrow in US_HOLIDAYS:
        return True
    # Quarterly OpEx (3rd Friday of March/June/Sep/Dec)
    if today.month in (3, 6, 9, 12) and today.weekday() == 4:
        first_day    = today.replace(day=1)
        first_friday = first_day + datetime.timedelta(days=(4 - first_day.weekday()) % 7)
        if today == first_friday + datetime.timedelta(weeks=2):
            return True
    # Economic events (FOMC, CPI, NFP etc.)
    if EVENTS_FILE.exists():
        try:
            data = json.loads(EVENTS_FILE.read_text())
            if (today - datetime.date.fromisoformat(data["fetched"])).days <= 7:
                today_str = today.isoformat()
                for ev in data.get("events", []):
                    if ev.get("date", "").startswith(today_str):
                        log.info(f"No-trade: {ev['keyword']}")
                        return True
        except Exception:
            pass
    return False


# ══════════════════════════════════════════════════════════════════════════════
# MarketData — quote context wrapper
# ══════════════════════════════════════════════════════════════════════════════
class MarketData:
    def __init__(self):
        self.quote_ctx = None

    def connect(self) -> bool:
        if not FUTU_AVAILABLE:
            return False
        try:
            self.quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
            return True
        except Exception as e:
            log.error(f"QuoteContext connect failed: {e}")
            return False

    def close(self):
        if self.quote_ctx:
            try:
                self.quote_ctx.close()
            except Exception:
                pass

    def get_vix(self) -> Optional[float]:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 18.0  # dry-run default
        try:
            ret, data = self.quote_ctx.get_market_snapshot(["US.VIX"])
            if ret == RET_OK and not data.empty:
                return float(data.iloc[0]["last_price"])
        except Exception as e:
            log.warning(f"get_vix: {e}")
        return None

    def get_spy_snapshot(self) -> Optional[dict]:
        """Returns dict with open_price and last_price for SPY."""
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return {"open_price": 562.5, "last_price": 562.5}  # dry-run
        try:
            ret, snap = self.quote_ctx.get_market_snapshot([UNDERLYING_CODE])
            if ret == RET_OK and not snap.empty:
                row = snap.iloc[0]
                return {
                    "open_price":  float(row.get("open_price", 0) or 0),
                    "last_price":  float(row.get("last_price", 0) or 0),
                }
        except Exception as e:
            log.error(f"get_spy_snapshot: {e}")
        return None

    def get_spy_open(self) -> Optional[float]:
        snap = self.get_spy_snapshot()
        if snap:
            val = snap.get("open_price") or snap.get("last_price")
            return val if val and val > 0 else None
        return None

    def get_spy_current(self) -> Optional[float]:
        snap = self.get_spy_snapshot()
        if snap:
            val = snap.get("last_price") or snap.get("open_price")
            return val if val and val > 0 else None
        return None

    def calc_ivr(self, current_vix: float) -> Optional[float]:
        """
        IV Rank = (current_VIX - 52w_low) / (52w_high - 52w_low) × 100
        Returns 0-100, or None if data unavailable.
        Uses 252 trading days of VIX daily kline.
        """
        cached = load_ivr_cache()
        if cached is not None:
            return cached

        if not FUTU_AVAILABLE or not self.quote_ctx:
            return None
        try:
            end_date   = datetime.datetime.now(ET).date()
            start_date = end_date - datetime.timedelta(days=380)  # extra buffer
            ret, kline = self.quote_ctx.get_history_kline(
                "US.VIX",
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                ktype=ft.KLType.K_DAY,
                max_count=300,
            )
            if ret != RET_OK or kline.empty:
                log.warning("IVR: VIX kline unavailable")
                return None
            highs = kline["high"].astype(float).tolist()[-252:]
            lows  = kline["low"].astype(float).tolist()[-252:]
            vix_52w_high = max(highs)
            vix_52w_low  = min(lows)
            if vix_52w_high <= vix_52w_low:
                return 50.0
            ivr = (current_vix - vix_52w_low) / (vix_52w_high - vix_52w_low) * 100
            ivr = max(0.0, min(100.0, round(ivr, 1)))
            save_ivr_cache(ivr)
            log.info(f"IVR={ivr:.1f} (VIX={current_vix:.1f}, 52w: {vix_52w_low:.1f}-{vix_52w_high:.1f})")
            return ivr
        except Exception as e:
            log.warning(f"calc_ivr: {e}")
            return None

    def get_option_chain_with_greeks(self, expiry: str, opt_type: str) -> list:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return []
        futu_opt_type = ft.OptionType.PUT if opt_type == "PUT" else ft.OptionType.CALL
        ret, chain_df = self.quote_ctx.get_option_chain(
            UNDERLYING_CODE, start=expiry, end=expiry, option_type=futu_opt_type)
        if ret != RET_OK or chain_df.empty:
            log.warning(f"get_option_chain failed {UNDERLYING_CODE} {expiry} {opt_type}")
            return []
        codes = chain_df["code"].tolist()
        if not codes:
            return []
        ret2, snap = self.quote_ctx.get_market_snapshot(codes[:200])
        if ret2 != RET_OK or snap.empty:
            log.warning("option chain snapshot failed")
            return []
        chain_dict = chain_df.set_index("code").to_dict("index")
        result = []
        for _, row in snap.iterrows():
            code = row.get("code", "")
            ci   = chain_dict.get(code, {})
            result.append({
                "code":         code,
                "strike_price": float(row.get("option_strike_price",
                                              ci.get("strike_price", 0))),
                "delta":        abs(float(row.get("option_delta", 0))),
                "bid_price":    float(row.get("bid_price", 0)),
                "ask_price":    float(row.get("ask_price", 0)),
                "last_price":   float(row.get("last_price", 0)),
                "option_type":  opt_type,
            })
        return result

    def find_by_delta(self, chain: list, target_delta: float) -> Optional[dict]:
        if not chain:
            return None
        return min(chain, key=lambda o: abs(o.get("delta", 0) - target_delta))

    def find_by_strike(self, chain: list, target_strike: float) -> Optional[dict]:
        if not chain:
            return None
        return min(chain, key=lambda o: abs(o.get("strike_price", 0) - target_strike))


# ══════════════════════════════════════════════════════════════════════════════
# TradeEngine — order execution wrapper
# ══════════════════════════════════════════════════════════════════════════════
class TradeEngine:
    def __init__(self, paper: bool = False):
        self.paper      = paper
        self.trade_ctx  = None
        self.account_id = None
        self.trade_env  = TrdEnv.SIMULATE if paper else TrdEnv.REAL if FUTU_AVAILABLE else None
        self.unlock_ok  = False

    def connect(self) -> bool:
        if not FUTU_AVAILABLE:
            return False
        try:
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=OPEND_HOST, port=OPEND_PORT,
                security_firm=SecurityFirm.FUTUJP,
            )
            self._resolve_account()
            self._unlock()
            return True
        except Exception as e:
            log.error(f"TradeEngine connect: {e}")
            return False

    def close(self):
        if self.trade_ctx:
            try:
                self.trade_ctx.close()
            except Exception:
                pass

    def _resolve_account(self):
        ret, data = self.trade_ctx.get_acc_list()
        if ret != RET_OK or data.empty:
            log.warning("get_acc_list failed; account_id unresolved")
            return
        env = TrdEnv.SIMULATE if self.paper else TrdEnv.REAL
        rows = data[data["trd_env"] == env]
        if not rows.empty:
            self.account_id = str(rows.iloc[0]["acc_id"])
            log.info(f"Account resolved: {self.account_id} env={env}")

    def _unlock(self):
        if not TRADE_PASSWORD:
            return
        try:
            ret, data = self.trade_ctx.unlock_trade(password=TRADE_PASSWORD)
            if ret == RET_OK:
                self.unlock_ok = True
            elif "unlock button" in str(data) or "disabled in the GUI" in str(data):
                log.warning("unlock_trade disabled in GUI; assuming GUI-unlocked")
                self.unlock_ok = True
        except Exception as e:
            log.warning(f"unlock: {e}")

    def get_account_cash(self) -> float:
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return 10000.0  # dry-run
        try:
            ret, data = self.trade_ctx.accinfo_query(
                trd_env=self.trade_env, acc_id=int(self.account_id or 0))
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                return float(row.get("net_assets", 0) or row.get("cash", 0) or 10000)
        except Exception as e:
            log.warning(f"get_account_cash: {e}")
        return 10000.0

    def _place_single_leg(self, code: str, side, qty: int, label: str) -> bool:
        env = self.trade_env
        acc = int(self.account_id or 0)
        ret, data = self.trade_ctx.place_order(
            price=0, qty=qty, code=code,
            trd_side=side, order_type=OrderType.MARKET,
            trd_env=env, acc_id=acc,
        )
        if ret != RET_OK:
            log.error(f"Leg {label} failed: {data}")
            return False
        log.info(f"Leg {label} OK: code={code} qty={qty}")
        return True

    def _reverse_leg(self, code: str, original_side, qty: int, label: str):
        reverse = TrdSide.BUY if original_side == TrdSide.SELL else TrdSide.SELL
        env = self.trade_env
        acc = int(self.account_id or 0)
        ret, data = self.trade_ctx.place_order(
            price=0, qty=qty, code=code,
            trd_side=reverse, order_type=OrderType.MARKET,
            trd_env=env, acc_id=acc,
        )
        if ret == RET_OK:
            log.info(f"Unwind OK: {label}")

    def place_credit_spread(self, sell_code: str, buy_code: str,
                            qty: int, direction: str) -> bool:
        if not FUTU_AVAILABLE or not self.trade_ctx:
            log.info(f"[DRY-RUN] {direction} CS: SELL={sell_code} BUY={buy_code} qty={qty}")
            return True
        legs = [
            (sell_code, TrdSide.SELL, f"{direction}_sell"),
            (buy_code,  TrdSide.BUY,  f"{direction}_buy"),
        ]
        placed = []
        for code, side, label in legs:
            time.sleep(0.5)
            ok = self._place_single_leg(code, side, qty, label)
            if ok:
                placed.append((code, side, label))
            else:
                for p_code, p_side, p_label in reversed(placed):
                    self._reverse_leg(p_code, p_side, qty, p_label)
                return False
        log.info(f"{direction} CS placed: qty={qty}")
        return True

    def get_open_positions(self) -> list:
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return []
        ret, data = self.trade_ctx.position_list_query(
            trd_env=self.trade_env, acc_id=int(self.account_id or 0))
        return data.to_dict("records") if ret == RET_OK else []

    def close_all_positions(self, reason: str = "force_close"):
        positions = self.get_open_positions()
        if not positions:
            log.info(f"No positions to close ({reason})")
            return
        log.info(f"Closing {len(positions)} positions ({reason})")
        env = self.trade_env
        acc = int(self.account_id or 0)
        for pos in positions:
            code = pos.get("code", "")
            qty  = abs(int(pos.get("qty", 0)))
            if qty == 0:
                continue
            side = TrdSide.BUY if pos.get("position_side") == "LONG" else TrdSide.SELL
            self.trade_ctx.place_order(
                price=0, qty=qty, code=code,
                trd_side=side, order_type=OrderType.MARKET,
                trd_env=env, acc_id=acc,
            )


# ══════════════════════════════════════════════════════════════════════════════
# SMADirectionDetector — 20SMA direction for standard entry
# ══════════════════════════════════════════════════════════════════════════════
class SMADirectionDetector:
    """
    SPY Open > SMA20 → PUT CS (bull bias, sell puts below market)
    SPY Open < SMA20 → CALL CS (bear bias, sell calls above market)
    """
    def __init__(self, quote_ctx):
        self.quote_ctx = quote_ctx

    def get_direction(self, spy_open: Optional[float] = None) -> Optional[str]:
        sma20 = self._get_sma20()
        if sma20 is None:
            log.warning("SMA20 unavailable → skip direction")
            return None
        if spy_open is None or spy_open <= 0:
            try:
                ret, snap = self.quote_ctx.get_market_snapshot([UNDERLYING_CODE])
                if ret == RET_OK and not snap.empty:
                    spy_open = float(snap.iloc[0].get("open_price", 0) or
                                     snap.iloc[0].get("last_price", 0))
            except Exception:
                pass
        if not spy_open:
            return None
        direction = "PUT" if spy_open > sma20 else "CALL"
        log.info(f"SMA direction: SPY_open={spy_open:.2f} SMA20={sma20:.2f} → {direction}")
        return direction

    def _get_sma20(self) -> Optional[float]:
        # Cache: reuse within same day
        try:
            if SMA_CACHE_FILE.exists():
                data = json.loads(SMA_CACHE_FILE.read_text())
                today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
                if data.get("date") == today:
                    return float(data["sma20"])
        except Exception:
            pass

        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 560.0  # dry-run

        try:
            end_date   = datetime.datetime.now(ET).date()
            start_date = end_date - datetime.timedelta(days=40)
            ret, kline = self.quote_ctx.get_history_kline(
                UNDERLYING_CODE,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                ktype=ft.KLType.K_DAY,
                max_count=30,
            )
            if ret != RET_OK or kline.empty:
                return None
            closes = kline["close"].astype(float).tolist()
            if len(closes) < SMA_PERIOD:
                return None
            sma20 = sum(closes[-SMA_PERIOD:]) / SMA_PERIOD
            SMA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SMA_CACHE_FILE.write_text(json.dumps({
                "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                "sma20": sma20,
            }))
            return sma20
        except Exception as e:
            log.warning(f"SMA20 fetch: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
# DemoLogger — records parameter variants without placing orders
# Runs at two windows: 10:30 ET (standard variants) and 13:00 ET (ORF variants)
# ══════════════════════════════════════════════════════════════════════════════

# Standard entry variants (simulated at 10:30 ET)
DEMO_STANDARD_VARIANTS = [
    {"label": "Std_d020_w10", "delta": 0.20, "width": 10},
    {"label": "Std_d025_w10", "delta": 0.25, "width": 10},  # current setting
    {"label": "Std_d030_w10", "delta": 0.30, "width": 10},
    {"label": "Std_d025_w20", "delta": 0.25, "width": 20},
]

# ORF entry variants (simulated at 13:00 ET, only when orf_triggered=True)
DEMO_ORF_VARIANTS = [
    {"label": "ORF_d015_w10", "delta": 0.15, "width": 10},
    {"label": "ORF_d020_w10", "delta": 0.20, "width": 10},  # current ORF setting
    {"label": "ORF_d020_w20", "delta": 0.20, "width": 20},
]


class DemoLogger:
    def __init__(self, quote_ctx):
        self.quote_ctx = quote_ctx

    def run(self, tactic: str, direction: str, expiry: str, vix: float,
            ivr: Optional[float] = None) -> list:
        """
        tactic: "standard" or "orf"
        direction: "PUT" or "CALL"
        """
        variants = DEMO_STANDARD_VARIANTS if tactic == "standard" else DEMO_ORF_VARIANTS
        log.info(f"[DEMO-{tactic.upper()}] direction={direction} expiry={expiry} "
                 f"VIX={vix:.1f} IVR={ivr:.0f if ivr else 'N/A'}")
        results = []
        for v in variants:
            r = self._sim(v, direction, expiry)
            r.update({
                "tactic": tactic, "vix": vix, "ivr": ivr,
                "direction": direction, "expiry": expiry,
                "ts": datetime.datetime.now(ET).isoformat(),
            })
            results.append(r)
            log.info(f"[DEMO] {v['label']}: sell={r.get('sell_strike','N/A')} "
                     f"buy={r.get('buy_strike','N/A')} credit=${r.get('net_credit',0):.2f} "
                     f"delta_actual={r.get('delta_actual','N/A')}")
        self._write_log(results)
        return results

    def _sim(self, variant: dict, direction: str, expiry: str) -> dict:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            sell_strike = 560.0 if direction == "PUT" else 580.0
            buy_strike  = sell_strike - variant["width"] if direction == "PUT" \
                          else sell_strike + variant["width"]
            return {
                "label":       variant["label"],
                "sell_strike": sell_strike,
                "buy_strike":  buy_strike,
                "net_credit":  round(variant["delta"] * variant["width"] * 0.4, 2),
                "delta_actual": variant["delta"],
            }
        try:
            futu_opt = ft.OptionType.PUT if direction == "PUT" else ft.OptionType.CALL
            ret, chain_df = self.quote_ctx.get_option_chain(
                UNDERLYING_CODE, start=expiry, end=expiry, option_type=futu_opt)
            if ret != RET_OK or chain_df.empty:
                return {"label": variant["label"], "error": "chain_failed"}
            codes = chain_df["code"].tolist()
            ret2, snap = self.quote_ctx.get_market_snapshot(codes[:200])
            if ret2 != RET_OK or snap.empty:
                return {"label": variant["label"], "error": "snapshot_failed"}
            chain_dict = chain_df.set_index("code").to_dict("index")
            chain = []
            for _, row in snap.iterrows():
                code = row.get("code", "")
                ci   = chain_dict.get(code, {})
                chain.append({
                    "strike_price": float(row.get("option_strike_price",
                                                  ci.get("strike_price", 0))),
                    "delta":        abs(float(row.get("option_delta", 0))),
                    "bid_price":    float(row.get("bid_price", 0)),
                    "ask_price":    float(row.get("ask_price", 0)),
                })
            if not chain:
                return {"label": variant["label"], "error": "empty_chain"}
            sell_opt = min(chain, key=lambda o: abs(o["delta"] - variant["delta"]))
            sell_strike = sell_opt["strike_price"]
            buy_target  = sell_strike - variant["width"] if direction == "PUT" \
                          else sell_strike + variant["width"]
            buy_opt     = min(chain, key=lambda o: abs(o["strike_price"] - buy_target))
            return {
                "label":        variant["label"],
                "sell_strike":  sell_strike,
                "buy_strike":   buy_opt["strike_price"],
                "net_credit":   round(sell_opt["bid_price"] - buy_opt["ask_price"], 2),
                "delta_actual": sell_opt["delta"],
            }
        except Exception as e:
            return {"label": variant["label"], "error": str(e)[:80]}

    def _write_log(self, results: list):
        try:
            DEMO_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DEMO_LOG_FILE, "a") as f:
                f.write(json.dumps(results) + "\n")
        except Exception as e:
            log.warning(f"demo log write: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# EntryBuilder — construct and place a credit spread
# ══════════════════════════════════════════════════════════════════════════════
class EntryBuilder:
    """Encapsulates chain fetch + order placement for one CS entry."""

    def __init__(self, mkt: MarketData, eng: TradeEngine):
        self.mkt = mkt
        self.eng = eng

    def place(self, expiry: str, qty: int, params: dict,
              vix: float, direction: str, tactic: str) -> bool:
        target_delta = params["delta"]
        spread_width = params["width"]

        log.info(f"[{tactic}] {direction} CS: expiry={expiry} qty={qty} "
                 f"VIX={vix:.1f} delta={target_delta} width={spread_width}")

        chain = self.mkt.get_option_chain_with_greeks(expiry, direction)
        if not chain:
            pushover("SPY CS", f"チェーン取得失敗 {direction} {expiry} ({tactic})")
            return False

        sell_opt = self.mkt.find_by_delta(chain, target_delta)
        if not sell_opt:
            pushover("SPY CS", f"SELL脚見つからず {direction} ({tactic})")
            return False

        sell_strike = sell_opt["strike_price"]
        buy_target  = sell_strike - spread_width if direction == "PUT" \
                      else sell_strike + spread_width
        buy_opt = self.mkt.find_by_strike(chain, buy_target)
        if not buy_opt:
            pushover("SPY CS", f"BUY脚見つからず {direction} ({tactic})")
            return False

        net_credit = round(sell_opt.get("bid_price", 0) - buy_opt.get("ask_price", 0), 2)
        log.info(f"{direction} CS: SELL {sell_strike:.1f} / BUY {buy_opt['strike_price']:.1f} "
                 f"width={spread_width} credit=${net_credit:.2f}")

        ok = self.eng.place_credit_spread(
            sell_code=sell_opt["code"],
            buy_code=buy_opt["code"],
            qty=qty, direction=direction,
        )

        now_et   = datetime.datetime.now(ET)
        time_key = f"{now_et.hour}:{now_et.minute:02d}"
        if ok:
            pushover(
                f"SPY CS [{tactic}]",
                f"0DTE {direction} エントリー {time_key}ET\n"
                f"SELL {sell_strike:.1f} / BUY {buy_opt['strike_price']:.1f} "
                f"(w={spread_width} δ={target_delta})\n"
                f"{qty}枚 credit=${net_credit:.2f}"
            )
            append_pnl_entry({
                "event": "entry", "tactic": tactic, "expiry": expiry,
                "direction": direction, "sell_strike": sell_strike,
                "buy_strike": buy_opt["strike_price"],
                "qty": qty, "net_credit": net_credit,
            })
            append_monthly_csv({
                "expiry": expiry, "direction": direction,
                "sell_strike": sell_strike, "buy_strike": buy_opt["strike_price"],
                "qty": qty, "net_credit": net_credit,
                "result": "entered", "tactic": tactic,
            })
        else:
            pushover("SPY CS", f"エントリー失敗 {direction} {expiry} {time_key}ET ({tactic})")

        return ok


# ══════════════════════════════════════════════════════════════════════════════
# SPYCreditSpreadBot — main orchestrator
# ══════════════════════════════════════════════════════════════════════════════
class SPYCreditSpreadBot:
    def __init__(self, paper: bool = False, test_connect: bool = False,
                 demo_compare: bool = False):
        self.paper        = paper
        self.test_connect = test_connect
        self.demo_compare = demo_compare
        self.mkt          = MarketData()
        self.eng          = TradeEngine(paper=paper)
        self.builder      = None  # initialized after connect

        # Daily state — reset at EOD
        self.traded_today      = False
        self.orf_checked       = False  # 10:00 ET check done
        self.orf_triggered     = False  # ORF conditions met
        self.orf_direction: Optional[str] = None
        self._nightly_checked  = False
        self._monthly_export   = False

        self.consecutive_start_failures = load_failures()

    def _reset_daily_state(self):
        self.traded_today  = False
        self.orf_checked   = False
        self.orf_triggered = False
        self.orf_direction = None
        self._nightly_checked = False
        log.info("Daily state reset")

    def get_expiry_0dte(self) -> str:
        today = datetime.datetime.now(ET).date()
        for _ in range(10):
            if today.weekday() < 5 and today not in US_HOLIDAYS:
                return today.strftime("%Y-%m-%d")
            today -= datetime.timedelta(days=1)
        return datetime.datetime.now(ET).date().strftime("%Y-%m-%d")

    # ── 10:00 ET: Opening Range Fade check ────────────────────────────────────
    def check_opening_range(self):
        """
        Compare SPY price at 10:00 ET vs open price.
        If VIX >= ORF_VIX_THRESHOLD and |move| >= ORF_MOVE_THRESHOLD:
          - orf_triggered = True
          - orf_direction = opposite of the move (fade the open)
          - standard 10:30 entry is skipped; ORF entry at 13:00 is used
        """
        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("ORF check: VIX unavailable → skipping ORF")
            return

        if vix < ORF_VIX_THRESHOLD:
            log.info(f"ORF check: VIX={vix:.1f} < {ORF_VIX_THRESHOLD} → ORF inactive, standard entry proceeds")
            return

        snap = self.mkt.get_spy_snapshot()
        if snap is None:
            log.warning("ORF check: SPY snapshot unavailable")
            return

        spy_open    = snap.get("open_price") or snap.get("last_price")
        spy_current = snap.get("last_price") or snap.get("open_price")

        if not spy_open or not spy_current or spy_open == 0:
            log.warning("ORF check: SPY price data invalid")
            return

        move = (spy_current - spy_open) / spy_open
        log.info(f"ORF check: SPY open={spy_open:.2f} now={spy_current:.2f} "
                 f"move={move:+.2%} VIX={vix:.1f}")

        if abs(move) >= ORF_MOVE_THRESHOLD:
            # Fade the opening move:
            #   drop at open → sell Put CS (expect stabilization/recovery)
            #   rally at open → sell Call CS (expect stabilization/pullback)
            self.orf_direction = "PUT" if move < 0 else "CALL"
            self.orf_triggered = True
            log.info(f"ORF TRIGGERED: move={move:+.2%} → {self.orf_direction} CS at 13:00 ET")
            pushover(
                "SPY CS ORF発動",
                f"10:00ET: SPY {move:+.2%} (VIX={vix:.1f})\n"
                f"→ {self.orf_direction} CS を 13:00ET にエントリー予定\n"
                f"標準10:30エントリーはスキップ",
            )
        else:
            log.info(f"ORF check: |move|={abs(move):.2%} < {ORF_MOVE_THRESHOLD:.1%} → "
                     f"no ORF, standard 10:30 entry proceeds")

    # ── 10:30 ET: Standard entry ───────────────────────────────────────────────
    def run_standard_entry(self):
        """SMA-direction based entry. Skipped if ORF is triggered."""
        if self.orf_triggered:
            log.info("Standard entry skipped: ORF triggered, waiting for 13:00 ET")
            return

        now      = datetime.datetime.now(ET)
        time_key = f"{now.hour}:{now.minute:02d}"

        if check_consecutive_losses():
            self.traded_today = True
            return

        vix = self.mkt.get_vix()
        if vix is None:
            pushover("SPY CS", f"エントリースキップ {time_key}ET: VIX取得不可")
            self.traded_today = True
            return

        params = get_params(vix, STANDARD_PARAMS)
        if params is None:
            log.info(f"VIX={vix:.1f} >= 35 → standard entry halted (ORF may activate)")
            # Don't set traded_today; ORF at 13:00 might still fire if triggered
            return

        # IVR + recovery day adjustments
        ivr    = self.mkt.calc_ivr(vix)
        params = apply_ivr_delta(params, ivr)
        params = apply_recovery_delta(params)

        # SMA direction
        spy_open  = self.mkt.get_spy_open()
        direction = SMADirectionDetector(self.mkt.quote_ctx).get_direction(spy_open=spy_open)
        if direction is None:
            pushover("SPY CS", f"エントリースキップ {time_key}ET: SMA方向判定不可")
            self.traded_today = True
            return

        cash   = self.eng.get_account_cash()
        qty    = calc_qty(cash, params)
        expiry = self.get_expiry_0dte()

        if self.demo_compare:
            DemoLogger(self.mkt.quote_ctx).run("standard", direction, expiry, vix, ivr)

        ok = self.builder.place(expiry, qty, params, vix, direction, "standard")
        self._update_vix_cache(vix)
        self.traded_today = True

    # ── 13:00 ET: ORF entry ────────────────────────────────────────────────────
    def run_orf_entry(self):
        """Opening Range Fade entry. Only runs if orf_triggered = True."""
        if not self.orf_triggered:
            return
        if self.traded_today:
            log.info("ORF entry skipped: already traded today")
            return

        now      = datetime.datetime.now(ET)
        time_key = f"{now.hour}:{now.minute:02d}"

        if check_consecutive_losses():
            self.traded_today = True
            return

        vix = self.mkt.get_vix()
        if vix is None:
            pushover("SPY CS", f"ORFスキップ {time_key}ET: VIX取得不可")
            self.traded_today = True
            return

        params = get_params(vix, ORF_PARAMS)
        if params is None:
            log.info(f"ORF: VIX={vix:.1f} >= 50 → ORF halted")
            pushover("SPY CS", f"ORFノートレード {time_key}ET: VIX={vix:.1f} >= 50")
            self.traded_today = True
            return

        ivr    = self.mkt.calc_ivr(vix)
        params = apply_ivr_delta(params, ivr)
        params = apply_recovery_delta(params)

        cash   = self.eng.get_account_cash()
        qty    = calc_qty(cash, params)
        expiry = self.get_expiry_0dte()

        log.info(f"ORF entry: {self.orf_direction} VIX={vix:.1f} "
                 f"delta={params['delta']} IVR={ivr if ivr else 'N/A'}")

        if self.demo_compare:
            DemoLogger(self.mkt.quote_ctx).run("orf", self.orf_direction, expiry, vix, ivr)

        ok = self.builder.place(expiry, qty, params, vix, self.orf_direction, "orf")
        self._update_vix_cache(vix)
        self.traded_today = True

    def _update_vix_cache(self, vix: float):
        """Save today's VIX and spike flag for tomorrow."""
        yesterday_vix = get_yesterday_vix()
        spike = False
        if yesterday_vix and (vix - yesterday_vix) >= VIX_SPIKE_THRESHOLD:
            spike = True
            log.info(f"VIX spike detected: {yesterday_vix:.1f} → {vix:.1f} "
                     f"(+{vix - yesterday_vix:.1f}) → tomorrow is recovery day")
        save_vix_spike_data(vix, spike_for_tomorrow=spike)

    # ── Exit monitor ──────────────────────────────────────────────────────────
    def check_exits(self):
        """PT 80% / SL 100% / 15:50 force close."""
        now       = datetime.datetime.now(ET)
        positions = self.eng.get_open_positions()
        if not positions:
            return

        # 15:50 ET force close
        if now.hour > FORCE_CLOSE_H or (now.hour == FORCE_CLOSE_H and now.minute >= FORCE_CLOSE_M):
            log.info("15:50 ET force close")
            self.eng.close_all_positions("15:50_force_close")
            pushover("SPY CS", f"15:50 force close {len(positions)}件")
            append_pnl_entry({"event": "exit", "reason": "15:50_force_close"})
            return

        # PT / SL monitor
        for pos in positions:
            try:
                cost_basis = float(pos.get("cost_price", 0) or 0) * \
                             abs(int(pos.get("qty", 0))) * 100
                current_pl = float(pos.get("unrealized_pl", 0) or 0)
                if cost_basis == 0:
                    continue
                pl_ratio = current_pl / abs(cost_basis)

                if pl_ratio >= PROFIT_TARGET:
                    log.info(f"PT {pl_ratio:.1%} >= {PROFIT_TARGET:.0%} → close {pos.get('code')}")
                    self.eng.close_all_positions("profit_target")
                    pushover("SPY CS", f"利確 {pl_ratio:.0%} (PT {PROFIT_TARGET:.0%})")
                    append_pnl_entry({
                        "event": "exit", "reason": "profit_target",
                        "code": pos.get("code", ""), "qty": pos.get("qty", 0),
                        "pnl_usd": round(current_pl, 2), "pl_ratio": round(pl_ratio, 4),
                    })
                    break
                elif pl_ratio <= -STOP_LOSS_MULT:
                    log.info(f"SL {pl_ratio:.1%} <= -{STOP_LOSS_MULT:.0%} → close {pos.get('code')}")
                    self.eng.close_all_positions("stop_loss")
                    pushover("SPY CS", f"損切 {pl_ratio:.0%} (SL {STOP_LOSS_MULT:.0%})", priority=1)
                    append_pnl_entry({
                        "event": "exit", "reason": "stop_loss",
                        "code": pos.get("code", ""), "qty": pos.get("qty", 0),
                        "pnl_usd": round(current_pl, 2), "pl_ratio": round(pl_ratio, 4),
                    })
                    break
            except Exception as e:
                log.warning(f"exit monitor pos: {e}")

    # ── Daily summary (9:00 JST = 20:00 ET) ──────────────────────────────────
    def run_daily_summary(self):
        now_et  = datetime.datetime.now(ET)
        now_jst = now_et.astimezone(JST)

        session_date = now_et.strftime("%Y-%m-%d")
        pnl_data     = load_pnl()
        session      = [t for t in pnl_data if t.get("date") == session_date]
        entries      = [t for t in session if t.get("event") == "entry"]
        exits        = [t for t in session if t.get("event") == "exit"]
        session_pnl  = sum(t.get("pnl_usd", 0) or 0 for t in exits)
        wins         = sum(1 for t in exits if (t.get("pnl_usd") or 0) > 0)
        losses       = len(exits) - wins
        week_start   = now_et.date() - datetime.timedelta(days=5)
        weekly_pnl   = sum(t.get("pnl_usd", 0) or 0
                           for t in pnl_data
                           if t.get("event") == "exit" and t.get("date", "") >= str(week_start))
        total_pnl    = sum(t.get("pnl_usd", 0) or 0
                           for t in pnl_data if t.get("event") == "exit")

        # Next day plan
        next_et = (now_et + datetime.timedelta(days=1)).date()
        if next_et.weekday() >= 5:
            plan = "週末休場"
        elif next_et in US_HOLIDAYS:
            plan = "祝日休場"
        else:
            plan = f"10:30ET (+ ORF@13:00ETスタンバイ)"

        mem_warn = ""
        try:
            if MEMORY_WARN_FILE.exists():
                mw = json.loads(MEMORY_WARN_FILE.read_text())
                if mw.get("count", 0) > 0:
                    mem_warn = f"メモリ警告{mw['count']}回(最大{mw.get('max_pct', 0):.0f}%)"
                MEMORY_WARN_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        flags = []
        if self.paper:
            flags.append("PAPER")
        if self.demo_compare:
            flags.append("DEMO")
        flag_str = " [" + "/".join(flags) + "]" if flags else ""

        lines = [f"SPY CS 日次 ({now_jst.strftime('%m/%d')} 09:00JST){flag_str}"]
        if exits:
            lines.append(f"昨日: {len(entries)}エントリー {wins}勝{losses}敗 P&L:${session_pnl:+.0f}")
        elif entries:
            lines.append(f"昨日: {len(entries)}エントリー(決済未確認)")
        else:
            lines.append("昨日: エントリーなし")
        lines.append(f"今日予定: {plan}")
        if mem_warn:
            lines.append(mem_warn)
        lines.append(f"週間:${weekly_pnl:+.0f} / 累計:${total_pnl:+.0f}")
        pushover("SPY CS 日次", "\n".join(lines))
        log.info("Daily summary sent")

    # ── Connection test ────────────────────────────────────────────────────────
    def _run_connection_test(self):
        log.info("=== Connection Test ===")
        ok = self.mkt.connect()
        log.info(f"Quote context: {'OK' if ok else 'FAIL'}")
        if ok:
            vix = self.mkt.get_vix()
            log.info(f"VIX: {vix}")
            spy = self.mkt.get_spy_snapshot()
            log.info(f"SPY snapshot: {spy}")
        ok2 = self.eng.connect()
        log.info(f"Trade context: {'OK' if ok2 else 'FAIL'}")
        if ok2:
            cash = self.eng.get_account_cash()
            log.info(f"Cash: ${cash:,.0f}")
        self.mkt.close()
        self.eng.close()
        log.info("=== Connection Test Done ===")

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run_forever(self):
        log.info(f"=== {STRATEGY_NAME} starting ===")
        log.info(f"Mode: {'PAPER ' if self.paper else ''}{'DEMO ' if self.demo_compare else ''}LIVE={not self.paper}")

        if self.test_connect:
            self.mkt.connect()
            self.eng.connect()
            self._run_connection_test()
            return

        pushover("SPY CS", f"起動{'[PAPER]' if self.paper else ''}{'[DEMO]' if self.demo_compare else ''}")
        fetch_events_weekly()

        if not self.mkt.connect():
            log.error("Quote context connect failed")
            self.consecutive_start_failures += 1
            save_failures(self.consecutive_start_failures)
            if self.consecutive_start_failures >= 3:
                pushover("SPY CS 起動失敗", f"OpenD接続失敗{self.consecutive_start_failures}回", priority=1)
            return

        if not self.eng.connect():
            log.error("Trade context connect failed")
            self.consecutive_start_failures += 1
            save_failures(self.consecutive_start_failures)
            return

        self.builder = EntryBuilder(self.mkt, self.eng)
        self.consecutive_start_failures = 0
        save_failures(0)
        log.info("OpenD connected")

        try:
            while True:
                now = datetime.datetime.now(ET)
                h, m = now.hour, now.minute

                # ── 20:00 ET = 09:00 JST: daily summary ──
                if h == 20 and m == 0 and not self._nightly_checked:
                    self.run_daily_summary()
                    self._nightly_checked = True
                    # Monthly PnL export on 1st of month
                    if now.astimezone(JST).day == 1 and not self._monthly_export:
                        self._export_monthly_pnl_csv()
                        self._monthly_export = True

                # ── Hourly memory check ──
                if m == 0:
                    memkey = f"_memcheck_{h}"
                    if not getattr(self, memkey, False):
                        check_memory_usage()
                        setattr(self, memkey, True)

                # ── Outside market hours → sleep ──
                in_market = (h == 9 and m >= 30) or (10 <= h < 16)
                if not in_market:
                    if h >= 16 and self.traded_today:
                        self._reset_daily_state()
                    # 17:30 ET: graceful self-exit so LaunchAgent can restart cleanly next night
                    if h == 17 and m >= 30:
                        log.info("17:30 ET: daily session complete, exiting for LaunchAgent restart")
                        pushover("SPY CS", "本日セッション終了 (17:30ET)")
                        break
                    time.sleep(30)
                    continue

                # ── No-trade day → sleep 1h ──
                if is_notrade_today():
                    log.info("No-trade day → sleep 1h")
                    time.sleep(3600)
                    continue

                # ── 10:00 ET: ORF check ──
                if h == ORF_CHECK_H and m == ORF_CHECK_M and not self.orf_checked:
                    self.check_opening_range()
                    self.orf_checked = True

                # ── 10:30 ET: standard entry (skipped if ORF triggered) ──
                if h == STANDARD_ENTRY_H and m == STANDARD_ENTRY_M and not self.traded_today:
                    self.run_standard_entry()

                # ── 13:00 ET: ORF entry (only if triggered) ──
                if h == ORF_ENTRY_H and m == ORF_ENTRY_M and self.orf_triggered and not self.traded_today:
                    self.run_orf_entry()

                # ── Exit monitor (every 30s during market hours) ──
                self.check_exits()

                time.sleep(30)

        except KeyboardInterrupt:
            log.info("Stopped by user")
        except Exception as e:
            log.error(f"Unhandled: {e}\n{traceback.format_exc()}")
            pushover("SPY CS クラッシュ", str(e)[:200], priority=1)
        finally:
            self.mkt.close()
            self.eng.close()

    def _export_monthly_pnl_csv(self):
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            now_et     = datetime.datetime.now(ET)
            prev_month = (now_et.date().replace(day=1) - datetime.timedelta(days=1))
            month_str  = prev_month.strftime("%Y%m")
            usdjpy     = 150.0
            try:
                import yfinance as yf
                hist = yf.Ticker("JPY=X").history(period="1d")
                if not hist.empty:
                    usdjpy = float(hist["Close"].iloc[-1])
            except Exception:
                pass
            trades       = load_pnl()
            month_prefix = prev_month.strftime("%Y-%m")
            month_trades = [t for t in trades if t.get("date", "").startswith(month_prefix)]
            if not month_trades:
                return
            csv_path   = REPORTS_DIR / f"condor_{month_str}.csv"
            fieldnames = ["date", "ts", "event", "tactic", "expiry", "direction",
                          "sell_strike", "buy_strike", "qty", "net_credit",
                          "pnl_usd", "pnl_jpy", "reason", "pl_ratio"]
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for t in month_trades:
                    t["pnl_jpy"] = round((t.get("pnl_usd", 0) or 0) * usdjpy, 0)
                    writer.writerow(t)
            pushover("SPY CS", f"月次CSV: {csv_path.name} ({len(month_trades)}件)")
        except Exception as e:
            log.warning(f"monthly csv: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=STRATEGY_NAME)
    parser.add_argument("--paper",        action="store_true", help="Paper trade mode")
    parser.add_argument("--test-connect", action="store_true", help="Test connection and exit")
    parser.add_argument("--demo-compare", action="store_true",
                        help="Log 7 parameter variants (4 standard + 3 ORF) without placing orders")
    args = parser.parse_args()

    bot = SPYCreditSpreadBot(
        paper=args.paper,
        test_connect=args.test_connect,
        demo_compare=args.demo_compare,
    )
    bot.run_forever()
