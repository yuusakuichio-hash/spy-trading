#!/usr/bin/env python3
"""
spx_condor_bot.py — SPX Iron Condor Bot (0DTE / 1DTE)
Broker  : moomoo / Futu Securities Japan (口座2756)
Strategy: SPX Iron Condor — bull-put spread + bear-call spread simultaneously
VIX gate:
  VIX < 25   → 0DTE condor + 1DTE condor (entry at 10:30 ET)
  25 ≤ VIX < 30 → 1DTE condor only
  VIX ≥ 30  → no trade

Built on the proven infrastructure of spx_bot.py.
NOTE: Requires OpenD to be logged-in and running on 127.0.0.1:11111.
      Underlying code "US.SPX" must be verified once OpenD is accessible.
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
    env_path = Path("/root/spxbot/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env_file()

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_DIR = Path(os.environ.get("SPX_LOG_DIR", "/var/log/spx_bot"))
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

# ── Constants ──────────────────────────────────────────────────────────────────
ET             = zoneinfo.ZoneInfo("America/New_York")
PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER  = "u2cevk8nktib3sr148rw2hs78ecvux"

# NOTE: "US.SPX" must be verified against moomoo OpenD once login is resolved.
#       If SPX index options use a different code prefix, update here.
UNDERLYING_CODE      = "US.SPX"
UNDERLYING_DISPLAY   = "SPX"

SPREAD_WIDTH   = 5.0        # $5 per wing
SELL_DELTA     = 0.20       # OTM sell-leg target delta
PROFIT_TARGET  = 0.50       # close at 50% of credit
STOP_LOSS_MULT = 2.00       # stop at 200% of credit (2× debit)
FORCE_CLOSE_H  = 15
FORCE_CLOSE_M  = 50
ENTRY_TIMES    = [(10, 30), (14, 0)]

# VIX regime thresholds
VIX_TIER_DUAL  = 25.0   # < 25  → 0DTE + 1DTE
VIX_TIER_1DTE  = 30.0   # 25–30 → 1DTE only ; ≥ 30 → stop

OPEND_HOST  = "127.0.0.1"
OPEND_PORT  = 11111
TRADE_PASSWORD = os.environ.get("TRADE_PASSWORD", "")

EVENTS_FILE         = Path("/root/events.json")
FAILURES_FILE       = Path("/root/spx_condor_failures.json")
TRADE_LOCK_FILE     = Path("/root/trade_lock.json")
PNL_FILE            = Path("/root/spxbot/condor_pnl.json")
MONTHLY_CSV_DIR     = Path("/var/log/spx_bot")
REPORTS_DIR         = Path("/root/spxbot/reports")
MEMORY_WARN_FILE    = Path("/root/spxbot/condor_memory_warn.json")
RECOVERY_COUNT_FILE = Path("/root/spxbot/recovery_count.json")
MEMORY_WARN_PCT     = 80

# US market holidays 2025–2027 (NYSE)
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


# ── Pushover ───────────────────────────────────────────────────────────────────
def pushover(title: str, message: str, priority: int = 0) -> bool:
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                  "title": title, "message": message, "priority": priority},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Pushover failed: {e}")
        return False


# ── Failure counter ────────────────────────────────────────────────────────────
def load_failures() -> int:
    try:
        if FAILURES_FILE.exists():
            data = json.loads(FAILURES_FILE.read_text())
            last = datetime.datetime.fromisoformat(data.get("last", "2000-01-01T00:00:00"))
            if (datetime.datetime.now() - last).total_seconds() > 86400:
                return 0
            return int(data.get("count", 0))
    except Exception:
        pass
    return 0

def save_failures(count: int):
    try:
        FAILURES_FILE.write_text(json.dumps(
            {"count": count, "last": datetime.datetime.now().isoformat()}))
    except Exception as e:
        log.warning(f"save_failures: {e}")


# ── P&L JSON ───────────────────────────────────────────────────────────────────
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
        log.warning(f"append_pnl_entry: {e}")


# ── Monthly CSV ────────────────────────────────────────────────────────────────
def append_monthly_csv(record: dict):
    try:
        MONTHLY_CSV_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(ET)
        csv_path = MONTHLY_CSV_DIR / f"condor_{now.strftime('%Y-%m')}.csv"
        fieldnames = ["timestamp", "expiry", "put_sell", "put_buy", "call_sell", "call_buy",
                      "qty", "net_credit", "result", "pnl"]
        write_header = not csv_path.exists()
        with open(csv_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            record.setdefault("timestamp", now.isoformat())
            writer.writerow(record)
    except Exception as e:
        log.warning(f"monthly CSV: {e}")


# ── Memory monitor ─────────────────────────────────────────────────────────────
def check_memory_usage():
    try:
        with open("/proc/meminfo") as f:
            info = {p[0].rstrip(":"): int(p[1]) for line in f for p in [line.split()] if len(p) >= 2}
        total, available = info.get("MemTotal", 0), info.get("MemAvailable", 0)
        if total == 0:
            return
        used_pct = (total - available) / total * 100
        if used_pct > MEMORY_WARN_PCT:
            log.warning(f"Memory high: {used_pct:.1f}%")
            try:
                data = json.loads(MEMORY_WARN_FILE.read_text()) if MEMORY_WARN_FILE.exists() else {}
                data["count"] = data.get("count", 0) + 1
                data["max_pct"] = max(float(data.get("max_pct", 0)), used_pct)
                data["last"] = datetime.datetime.now(ET).isoformat()
                MEMORY_WARN_FILE.write_text(json.dumps(data))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"Memory check: {e}")


# ── Event calendar ─────────────────────────────────────────────────────────────
def fetch_events_weekly():
    now = datetime.datetime.now(ET)
    if now.weekday() != 0:
        return
    if EVENTS_FILE.exists() and (now.timestamp() - EVENTS_FILE.stat().st_mtime) < 86400 * 6:
        return
    log.info("Fetching weekly event calendar...")
    try:
        week_start = now.date()
        week_end   = week_start + datetime.timedelta(days=6)
        r = requests.post(
            "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData",
            headers={
                "User-Agent": "Mozilla/5.0",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.investing.com/economic-calendar/",
            },
            data={"country[]": "5", "importance[]": ["2", "3"],
                  "dateFrom": str(week_start), "dateTo": str(week_end)},
            timeout=15,
        )
        events = []
        for row in r.json().get("data", "").split("<tr"):
            for kw in NOTRADE_KEYWORDS:
                if kw in row.lower():
                    events.append({"keyword": kw, "raw": row[:80]})
                    break
        EVENTS_FILE.write_text(json.dumps({"fetched": str(now.date()), "events": events}))
        log.info(f"Events: {len(events)} high-impact this week")
    except Exception as e:
        log.warning(f"Event fetch (non-fatal): {e}")

def is_notrade_today() -> bool:
    today    = datetime.datetime.now(ET).date()
    tomorrow = today + datetime.timedelta(days=1)
    if tomorrow in US_HOLIDAYS:
        return True
    if today.month in (3, 6, 9, 12) and today.weekday() == 4:
        first_day     = today.replace(day=1)
        first_friday  = first_day + datetime.timedelta(days=(4 - first_day.weekday()) % 7)
        third_friday  = first_friday + datetime.timedelta(weeks=2)
        if today == third_friday:
            log.info("Quarterly OpEx → no trade")
            return True
    if not EVENTS_FILE.exists():
        return False
    try:
        data    = json.loads(EVENTS_FILE.read_text())
        fetched = datetime.date.fromisoformat(data["fetched"])
        if (today - fetched).days <= 7 and data.get("events"):
            log.info(f"No-trade event: {data['events'][0]['keyword']}")
            return True
    except Exception:
        pass
    return False

def next_business_day(d: datetime.date) -> datetime.date:
    """Return the next business day after d (skip weekends + US_HOLIDAYS)."""
    nxt = d + datetime.timedelta(days=1)
    for _ in range(10):
        if nxt.weekday() < 5 and nxt not in US_HOLIDAYS:
            return nxt
        nxt += datetime.timedelta(days=1)
    return nxt


# ── futu-api import ────────────────────────────────────────────────────────────
try:
    from futu import (OpenQuoteContext, OpenSecTradeContext,
                      TrdMarket, TrdEnv, TrdSide, OrderType,
                      RET_OK, SecurityFirm)
    import futu as ft
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    log.warning("futu-api not installed — DRY-RUN mode")


# ══════════════════════════════════════════════════════════════════════════════
# MarketData
# ══════════════════════════════════════════════════════════════════════════════
class MarketData:
    def __init__(self):
        self.quote_ctx = None

    def connect(self) -> bool:
        if not FUTU_AVAILABLE:
            return False
        try:
            self.quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
            log.info("Quote context connected")
            return True
        except OSError as e:
            log.error(f"Quote connect OSError: {e}")
            pushover("SPX Condor OpenD障害", f"接続失敗: {str(e)[:100]}", priority=1)
            return False
        except Exception as e:
            log.error(f"Quote connect failed: {e}")
            pushover("SPX Condor OpenD障害", f"Quote接続失敗: {str(e)[:100]}", priority=1)
            return False

    def close(self):
        if self.quote_ctx:
            self.quote_ctx.close()

    def get_vix(self) -> Optional[float]:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 18.0  # dry-run
        ret, data = self.quote_ctx.get_market_snapshot(["US.VIX"])
        if ret == RET_OK and not data.empty:
            return float(data.iloc[0]["last_price"])
        log.warning("get_vix failed")
        return None

    def get_vix_prev_close(self) -> Optional[float]:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 17.0
        ret, data, _ = self.quote_ctx.request_history_kline(
            "US.VIX", start="2024-01-01",
            ktype=ft.KLType.K_DAY, max_count=5
        )
        if ret == RET_OK and len(data) >= 2:
            return float(data["close"].iloc[-2])
        return None

    def get_option_chain_with_greeks(self, expiry: str, opt_type: str) -> list:
        """
        Returns list of dicts with keys: code, strike_price, delta, bid_price, ask_price.
        Two-step: get_option_chain for codes, then get_market_snapshot for Greeks.

        opt_type: "PUT" or "CALL"
        """
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return []

        futu_opt_type = ft.OptionType.PUT if opt_type == "PUT" else ft.OptionType.CALL

        # Step 1: get option chain (codes + strike info)
        ret, chain_df = self.quote_ctx.get_option_chain(
            UNDERLYING_CODE,
            index_option_type=ft.IndexOptionType.NORMAL,
            start=expiry, end=expiry,
            option_type=futu_opt_type,
        )
        if ret != RET_OK or chain_df.empty:
            log.warning(f"get_option_chain failed for {UNDERLYING_CODE} {expiry} {opt_type}: {chain_df}")
            return []

        codes = chain_df["code"].tolist()
        if not codes:
            return []

        # Step 2: get Greeks via market snapshot (max 200 per call to stay safe)
        ret2, snap = self.quote_ctx.get_market_snapshot(codes[:200])
        if ret2 != RET_OK or snap.empty:
            log.warning(f"get_market_snapshot failed for option chain: {snap}")
            # Fall back: return chain without Greeks (delta=0 → find_strike_by_delta falls back to strike distance)
            return [{"code": row["code"],
                     "strike_price": float(row.get("strike_price", 0)),
                     "delta": 0.0,
                     "bid_price": 0.0, "ask_price": 0.0, "last_price": 0.0}
                    for _, row in chain_df.iterrows()]

        # Merge
        chain_dict = chain_df.set_index("code").to_dict("index")
        result = []
        for _, row in snap.iterrows():
            code = row.get("code", "")
            chain_info = chain_dict.get(code, {})
            result.append({
                "code":         code,
                "strike_price": float(row.get("option_strike_price",
                                              chain_info.get("strike_price", 0))),
                "delta":        abs(float(row.get("option_delta", 0))),
                "bid_price":    float(row.get("bid_price", 0)),
                "ask_price":    float(row.get("ask_price", 0)),
                "last_price":   float(row.get("last_price", 0)),
                "option_type":  opt_type,
            })
        return result

    def find_strike_by_delta(self, chain: list, target_delta: float) -> Optional[dict]:
        """Return option with delta closest to target_delta."""
        if not chain:
            return None
        return min(chain, key=lambda o: abs(o.get("delta", 0) - target_delta))

    def find_option_at_strike(self, chain: list, target_strike: float) -> Optional[dict]:
        """Return option with strike closest to target_strike."""
        if not chain:
            return None
        return min(chain, key=lambda o: abs(o.get("strike_price", 0) - target_strike))


# ══════════════════════════════════════════════════════════════════════════════
# TradeEngine
# ══════════════════════════════════════════════════════════════════════════════
class TradeEngine:
    def __init__(self):
        self.account_id = ""
        self.trade_env  = TrdEnv.REAL if FUTU_AVAILABLE else None
        self.trade_ctx  = None
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
            log.info("Trade context connected")
            self._resolve_account()

            if self.trade_env == TrdEnv.REAL:
                if TRADE_PASSWORD:
                    ret, data = self.trade_ctx.unlock_trade(password=TRADE_PASSWORD)
                    if ret != RET_OK:
                        if "unlock button" in str(data) or "disabled in the GUI" in str(data):
                            log.warning("unlock_trade disabled in GUI mode; assuming GUI-unlocked")
                            self.unlock_ok = True
                        else:
                            log.error(f"unlock_trade failed: {data}")
                            pushover("SPX Condor", f"❌取引ロック解除失敗: {str(data)[:120]}", priority=1)
                            self.trade_ctx.close()
                            self.trade_ctx = None
                            return False
                    else:
                        log.info("unlock_trade: OK")
                        self.unlock_ok = True
                else:
                    log.warning("TRADE_PASSWORD not set; skipping unlock_trade")
            else:
                self.unlock_ok = True

            return True
        except OSError as e:
            log.error(f"Trade connect OSError: {e}")
            pushover("SPX Condor OpenD障害", f"Trade接続失敗: {str(e)[:100]}", priority=1)
            return False
        except Exception as e:
            log.error(f"Trade connect failed: {e}")
            pushover("SPX Condor OpenD障害", f"Trade接続失敗: {str(e)[:100]}", priority=1)
            return False

    def _resolve_account(self):
        ret, data = self.trade_ctx.get_acc_list()
        if ret != RET_OK or data.empty:
            log.warning("get_acc_list failed; account_id unresolved")
            return
        real = data[data["trd_env"] == "REAL"]
        if not real.empty:
            deriv = real[real["acc_type"] == "DERIVATIVES"]
            if not deriv.empty:
                self.account_id = str(int(deriv.iloc[0]["acc_id"]))
                self.trade_env  = TrdEnv.REAL
                log.info(f"REAL DERIVATIVES account: {self.account_id}")
                return
            self.account_id = str(int(real.iloc[0]["acc_id"]))
            self.trade_env  = TrdEnv.REAL
            log.warning(f"No DERIVATIVES account; using first REAL: {self.account_id}")
            return
        sim = data[data["trd_env"] == "SIMULATE"]
        if not sim.empty:
            self.account_id = str(int(sim.iloc[0]["acc_id"]))
            self.trade_env  = TrdEnv.SIMULATE
            log.warning(f"SIMULATE account: {self.account_id}")

    def close(self):
        if self.trade_ctx:
            self.trade_ctx.close()

    def get_account_cash(self) -> float:
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return 5000.0  # dry-run
        if not self.account_id:
            return 5000.0
        ret, data = self.trade_ctx.accinfo_query(
            trd_env=self.trade_env, acc_id=int(self.account_id))
        if ret == RET_OK and not data.empty:
            row = data.iloc[0]
            net = float(row.get("net_assets", 0))
            cash = float(row.get("cash", 0))
            capital = net if net > 0 else cash
            log.info(f"Capital: net_assets=${net:,.2f} cash=${cash:,.2f} → ${capital:,.2f}")
            return capital if capital > 0 else 5000.0
        pushover("SPX Condor", "⚠️残高取得失敗・フォールバック$5,000", priority=0)
        return 5000.0

    def check_startup_margin(self, usd_to_jpy: float = 150.0) -> bool:
        capital = self.get_account_cash()
        jpy = capital * usd_to_jpy
        THRESHOLD = 500_000
        if jpy < THRESHOLD:
            msg = f"⚠️証拠金不足: ${capital:,.0f}(≈¥{jpy:,.0f}) < ¥{THRESHOLD:,}"
            log.warning(msg)
            pushover("SPX Condor 証拠金警告", msg, priority=1)
            return False
        log.info(f"Margin OK: ${capital:,.0f}(≈¥{jpy:,.0f})")
        return True

    def calc_position_size(self, cash: float, vix: float,
                           vix_prev: Optional[float], num_expiries: int = 1) -> int:
        """
        Dynamic sizing. When num_expiries > 1, total margin is shared across expiries
        so each condor gets qty = base_qty // num_expiries (min 1).
        Iron Condor margin per contract = SPREAD_WIDTH * 100 (wider of put/call spread).
        """
        now     = datetime.datetime.now(ET)
        month   = now.month
        weekday = now.weekday()

        ratio = 0.20
        if vix_prev and vix >= vix_prev * 1.20:
            ratio = 0.40
            log.info(f"VIX spike {vix_prev:.1f}→{vix:.1f} → ratio 40%")
        elif is_notrade_today():
            ratio = 0.30
        elif weekday in (0, 4):
            ratio = 0.25

        if month in (9, 10):
            ratio *= 0.5
        elif month in (7, 11):
            ratio *= 1.5

        margin_per_condor = SPREAD_WIDTH * 100  # $500 per condor
        base_qty = max(1, int((cash * ratio) / (margin_per_condor * num_expiries)))
        log.info(f"Position size: cash=${cash:.0f} ratio={ratio:.0%} "
                 f"num_expiries={num_expiries} → {base_qty} contracts each")
        return base_qty

    def _place_single_leg(self, code: str, side, qty: int, label: str) -> bool:
        """Place one leg at market. Returns True on success."""
        env = self.trade_env
        acc = int(self.account_id)
        log.info(f"Leg {label}: {'SELL' if side == TrdSide.SELL else 'BUY'} {code} qty={qty}")
        ret, data = self.trade_ctx.place_order(
            price=0, qty=qty, code=code,
            trd_side=side, order_type=OrderType.MARKET,
            trd_env=env, acc_id=acc,
        )
        if ret != RET_OK:
            log.error(f"Leg {label} failed: {data}")
            return False
        return True

    def _reverse_leg(self, code: str, original_side, qty: int, label: str):
        """Buy back a sell leg (or sell back a buy leg) to unwind a partial fill."""
        reverse = TrdSide.BUY if original_side == TrdSide.SELL else TrdSide.SELL
        env = self.trade_env
        acc = int(self.account_id)
        for attempt in range(3):
            ret, data = self.trade_ctx.place_order(
                price=0, qty=qty, code=code,
                trd_side=reverse, order_type=OrderType.MARKET,
                trd_env=env, acc_id=acc,
            )
            if ret == RET_OK:
                log.info(f"Unwind OK: {label}")
                return True
            log.warning(f"Unwind attempt {attempt+1}/3 for {label} failed: {data}")
            time.sleep(2)
        pushover("NAKED POSITION RISK",
                 f"Unwind FAILED for {label} ({code}). Manual intervention required!",
                 priority=2)
        return False

    def place_condor(self,
                     put_sell_code: str, put_buy_code: str,
                     call_sell_code: str, call_buy_code: str,
                     qty: int) -> bool:
        """
        Place Iron Condor: 4 legs in order.
        On any failure, unwind already-placed legs before returning False.

        Leg order:
          1. SELL put  (short put spread leg 1)
          2. BUY  put  (short put spread leg 2 — protection)
          3. SELL call (short call spread leg 1)
          4. BUY  call (short call spread leg 2 — protection)
        """
        if not FUTU_AVAILABLE or not self.trade_ctx:
            log.info(f"[DRY-RUN] Condor: "
                     f"SELL_PUT={put_sell_code} BUY_PUT={put_buy_code} "
                     f"SELL_CALL={call_sell_code} BUY_CALL={call_buy_code} qty={qty}")
            return True

        legs = [
            (put_sell_code,  TrdSide.SELL, "put_sell"),
            (put_buy_code,   TrdSide.BUY,  "put_buy"),
            (call_sell_code, TrdSide.SELL, "call_sell"),
            (call_buy_code,  TrdSide.BUY,  "call_buy"),
        ]
        placed = []

        for code, side, label in legs:
            time.sleep(0.5)
            ok = self._place_single_leg(code, side, qty, label)
            if ok:
                placed.append((code, side, label))
            else:
                # Unwind in reverse order
                log.error(f"Condor leg {label} failed → unwinding {len(placed)} placed legs")
                for p_code, p_side, p_label in reversed(placed):
                    self._reverse_leg(p_code, p_side, qty, p_label)
                return False

        log.info(f"Iron Condor placed successfully: qty={qty}")
        return True

    def get_open_positions(self) -> list:
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return []
        ret, data = self.trade_ctx.position_list_query(
            trd_env=self.trade_env, acc_id=int(self.account_id))
        return data.to_dict("records") if ret == RET_OK else []

    def close_all_positions(self, reason: str = "force_close"):
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
                trd_env=env, acc_id=acc,
            )


# ══════════════════════════════════════════════════════════════════════════════
# SPXCondorBot — main strategy
# ══════════════════════════════════════════════════════════════════════════════
class SPXCondorBot:
    def __init__(self):
        self.mkt  = MarketData()
        self.eng  = TradeEngine()
        self.traded_times: dict = {}
        self.consecutive_start_failures = load_failures()
        self._margin_ok = True

    # ── Expiry helpers ─────────────────────────────────────────────────────────
    def get_expiry_0dte(self) -> str:
        """0DTE = today (if market day). Adjust backward if holiday."""
        today = datetime.datetime.now(ET).date()
        for _ in range(10):
            if today.weekday() < 5 and today not in US_HOLIDAYS:
                return today.strftime("%Y-%m-%d")
            today -= datetime.timedelta(days=1)
        return datetime.datetime.now(ET).date().strftime("%Y-%m-%d")

    def get_expiry_1dte(self) -> str:
        """1DTE = next business day."""
        today = datetime.datetime.now(ET).date()
        return next_business_day(today).strftime("%Y-%m-%d")

    def should_enter(self, h: int, m: int) -> bool:
        for eh, em in ENTRY_TIMES:
            key = f"{eh}:{em:02d}"
            if h == eh and m == em and key not in self.traded_times:
                return True
        return False

    # ── Core entry logic ───────────────────────────────────────────────────────
    def run_condor_for_expiry(self, expiry: str, qty: int,
                              vix: float, time_key: str) -> bool:
        """Build and place one Iron Condor for the given expiry."""
        log.info(f"Building condor for expiry={expiry} qty={qty} VIX={vix:.1f}")

        # Get option chains with Greeks
        put_chain  = self.mkt.get_option_chain_with_greeks(expiry, "PUT")
        call_chain = self.mkt.get_option_chain_with_greeks(expiry, "CALL")

        if not put_chain or not call_chain:
            msg = f"❌チェーン取得失敗 {expiry}"
            log.error(msg)
            pushover("SPX Condor", msg)
            return False

        # Find sell strikes at ~0.20 delta
        put_sell_opt  = self.mkt.find_strike_by_delta(put_chain,  SELL_DELTA)
        call_sell_opt = self.mkt.find_strike_by_delta(call_chain, SELL_DELTA)

        if not put_sell_opt or not call_sell_opt:
            pushover("SPX Condor", f"❌売りストライク見つからず {expiry}")
            return False

        put_sell_strike  = put_sell_opt["strike_price"]
        call_sell_strike = call_sell_opt["strike_price"]

        # Sanity: put sell must be below call sell
        if put_sell_strike >= call_sell_strike:
            msg = (f"⚠️ストライク逆転: put_sell={put_sell_strike} >= "
                   f"call_sell={call_sell_strike} → スキップ {expiry}")
            log.warning(msg)
            pushover("SPX Condor", msg)
            return False

        put_buy_strike  = put_sell_strike  - SPREAD_WIDTH
        call_buy_strike = call_sell_strike + SPREAD_WIDTH

        put_buy_opt  = self.mkt.find_option_at_strike(put_chain,  put_buy_strike)
        call_buy_opt = self.mkt.find_option_at_strike(call_chain, call_buy_strike)

        if not put_buy_opt or not call_buy_opt:
            pushover("SPX Condor", f"❌買いストライク見つからず {expiry}")
            return False

        # Estimate net credit (put spread + call spread)
        put_credit  = (put_sell_opt.get("bid_price", 0)  - put_buy_opt.get("ask_price", 0))
        call_credit = (call_sell_opt.get("bid_price", 0) - call_buy_opt.get("ask_price", 0))
        net_credit  = round(put_credit + call_credit, 2)

        log.info(
            f"Condor {expiry}: "
            f"PUT  {put_sell_strike:.0f}/{put_buy_strike:.0f} (δ{put_sell_opt['delta']:.2f}) "
            f"CALL {call_sell_strike:.0f}/{call_buy_strike:.0f} (δ{call_sell_opt['delta']:.2f}) "
            f"credit=${net_credit:.2f} qty={qty}"
        )

        success = self.eng.place_condor(
            put_sell_code  = put_sell_opt["code"],
            put_buy_code   = put_buy_opt["code"],
            call_sell_code = call_sell_opt["code"],
            call_buy_code  = call_buy_opt["code"],
            qty            = qty,
        )

        if success:
            dte = "0DTE" if expiry == self.get_expiry_0dte() else "1DTE"
            pushover(
                "SPX Condor",
                f"✅{dte} エントリー {time_key}ET\n"
                f"PUT  {put_sell_strike:.0f}/{put_buy_strike:.0f}\n"
                f"CALL {call_sell_strike:.0f}/{call_buy_strike:.0f}\n"
                f"{qty}枚 net credit=${net_credit:.2f}"
            )
            append_monthly_csv({
                "expiry": expiry,
                "put_sell":  put_sell_strike, "put_buy":  put_buy_strike,
                "call_sell": call_sell_strike,"call_buy": call_buy_strike,
                "qty": qty, "net_credit": net_credit, "result": "entered",
            })
            append_pnl_entry({
                "event":     "entry",
                "expiry":    expiry,
                "put_sell":  put_sell_strike, "put_buy":  put_buy_strike,
                "call_sell": call_sell_strike,"call_buy": call_buy_strike,
                "qty": qty, "net_credit": net_credit,
            })
        else:
            pushover("SPX Condor", f"❌エントリー失敗 {expiry} {time_key}ET")

        return success

    def run_entry(self):
        now      = datetime.datetime.now(ET)
        time_key = f"{now.hour}:{now.minute:02d}"

        vix      = self.mkt.get_vix()
        vix_prev = self.mkt.get_vix_prev_close()

        if vix is None:
            log.error("VIX data unavailable → skip entry")
            pushover("SPX Condor", f"❌エントリー失敗 {time_key}ET: VIX取得不可")
            self.traded_times[time_key] = True
            return

        log.info(f"VIX={vix:.1f} vix_prev={vix_prev}")

        # ── VIX gate ──────────────────────────────────────────────────────────
        if vix >= VIX_TIER_1DTE:
            log.info(f"VIX={vix:.1f} >= {VIX_TIER_1DTE} → no trade")
            pushover("SPX Condor", f"⏭️ノートレード {time_key}ET: VIX={vix:.1f}≥{VIX_TIER_1DTE}")
            self.traded_times[time_key] = True
            return

        # ── Determine expiries ────────────────────────────────────────────────
        expiry_0dte = self.get_expiry_0dte()
        expiry_1dte = self.get_expiry_1dte()

        if vix < VIX_TIER_DUAL:
            # Both 0DTE and 1DTE condors
            expiries = [expiry_0dte, expiry_1dte]
            vix_label = f"VIX={vix:.1f}<{VIX_TIER_DUAL} → 0DTE+1DTE"
        else:
            # 1DTE only (VIX 25–30)
            expiries = [expiry_1dte]
            vix_label = f"VIX={vix:.1f} {VIX_TIER_DUAL}–{VIX_TIER_1DTE} → 1DTEのみ"

        log.info(vix_label)

        cash = self.eng.get_account_cash()
        qty  = self.eng.calc_position_size(cash, vix, vix_prev, num_expiries=len(expiries))

        # ── Place condors ─────────────────────────────────────────────────────
        any_success = False
        for expiry in expiries:
            ok = self.run_condor_for_expiry(expiry, qty, vix, time_key)
            if ok:
                any_success = True

        self.traded_times[time_key] = True
        if any_success:
            self.traded_times["entered_today"] = True

    def check_exits(self):
        """P&L monitor: profit target / stop loss / 15:50 force close."""
        now       = datetime.datetime.now(ET)
        positions = self.eng.get_open_positions()

        # Force close at 15:50 ET
        if now.hour > FORCE_CLOSE_H or (now.hour == FORCE_CLOSE_H and now.minute >= FORCE_CLOSE_M):
            if positions:
                log.info("15:50 ET force close")
                self.eng.close_all_positions("15:50_force_close")
                pushover("SPX Condor", f"🔔15:50 force close {len(positions)}件")
                append_pnl_entry({"event": "exit", "reason": "force_close_1550",
                                   "count": len(positions)})
            if now.minute >= 55:
                remaining = self.eng.get_open_positions()
                if remaining:
                    pushover("SPX Condor 残存", f"⚠️15:55以降も{len(remaining)}件残存", priority=1)
            return

        for pos in positions:
            cost_basis = pos.get("cost_price", 0) * abs(pos.get("qty", 0)) * 100
            current_pl = pos.get("pl_val", 0)
            if cost_basis == 0:
                continue
            pl_ratio = current_pl / abs(cost_basis)

            if pl_ratio >= PROFIT_TARGET:
                log.info(f"Profit target {pl_ratio:.1%} → close {pos.get('code')}")
                self.eng.close_all_positions("profit_target")
                pushover("SPX Condor", f"✅利確 {pl_ratio:.0%} クローズ")
                append_pnl_entry({"event": "exit", "reason": "profit_target",
                                   "code": pos.get("code", ""), "qty": pos.get("qty", 0),
                                   "pnl_usd": round(float(current_pl), 2),
                                   "pl_ratio": round(pl_ratio, 4)})
                break
            elif pl_ratio <= -STOP_LOSS_MULT:
                log.info(f"Stop loss {pl_ratio:.1%} → close {pos.get('code')}")
                self.eng.close_all_positions("stop_loss")
                pushover("SPX Condor", f"⛔損切り {pl_ratio:.0%} クローズ", priority=1)
                append_pnl_entry({"event": "exit", "reason": "stop_loss",
                                   "code": pos.get("code", ""), "qty": pos.get("qty", 0),
                                   "pnl_usd": round(float(current_pl), 2),
                                   "pl_ratio": round(pl_ratio, 4)})
                break

    # ── Nightly / daily summary (identical logic to spx_bot.py) ───────────────
    def _notrade_reason_for_date(self, target: datetime.date) -> str:
        day_after = target + datetime.timedelta(days=1)
        if day_after in US_HOLIDAYS:
            return f"翌日祝日({day_after})"
        if target.month in (3, 6, 9, 12) and target.weekday() == 4:
            first_day = target.replace(day=1)
            first_friday = first_day + datetime.timedelta(days=(4 - first_day.weekday()) % 7)
            if target == first_friday + datetime.timedelta(weeks=2):
                return "四半期OpEx"
        if EVENTS_FILE.exists():
            try:
                data = json.loads(EVENTS_FILE.read_text())
                if (target - datetime.date.fromisoformat(data["fetched"])).days <= 7:
                    for ev in data.get("events", []):
                        return ev["keyword"].upper()
            except Exception:
                pass
        return ""

    def _get_lock_expiry_warning(self) -> str:
        try:
            if not TRADE_LOCK_FILE.exists():
                return ""
            data = json.loads(TRADE_LOCK_FILE.read_text())
            expiry = datetime.date.fromisoformat(data.get("expiry", ""))
            days_left = (expiry - datetime.datetime.now(ET).date()).days
            if days_left <= 7:
                return f"⚠️取引ロック残り{days_left}日・更新必要（期限:{data['expiry']}）"
        except Exception:
            pass
        return ""

    def run_nightly_jst_check(self):
        tomorrow_et = (datetime.datetime.now(ET) + datetime.timedelta(days=1)).date()
        if tomorrow_et.weekday() < 5:
            reason = self._notrade_reason_for_date(tomorrow_et)
            if reason:
                pushover("SPX Condor", f"⏭️明日ノートレード: {reason}")
        warn = self._get_lock_expiry_warning()
        if warn:
            pushover("SPX Condor", warn, priority=1)

    def run_daily_summary_jst(self):
        now_et  = datetime.datetime.now(ET)
        jst     = zoneinfo.ZoneInfo("Asia/Tokyo")
        now_jst = now_et.astimezone(jst)

        session_date = now_et.strftime("%Y-%m-%d")
        pnl_data     = load_pnl()
        session      = [t for t in pnl_data if t.get("date") == session_date]
        entries      = [t for t in session if t.get("event") == "entry"]
        exits        = [t for t in session if t.get("event") == "exit"]
        session_pnl  = sum(t.get("pnl_usd", 0) or 0 for t in exits)
        wins         = sum(1 for t in exits if (t.get("pnl_usd") or 0) > 0)
        losses       = len(exits) - wins

        week_start  = now_et.date() - datetime.timedelta(days=5)
        weekly_pnl  = sum(t.get("pnl_usd", 0) or 0
                          for t in pnl_data
                          if t.get("event") == "exit" and t.get("date", "") >= str(week_start))
        total_pnl   = sum(t.get("pnl_usd", 0) or 0
                          for t in pnl_data if t.get("event") == "exit")

        mem_warn = ""
        try:
            if MEMORY_WARN_FILE.exists():
                mw = json.loads(MEMORY_WARN_FILE.read_text())
                if mw.get("count", 0) > 0:
                    mem_warn = f"⚠️メモリ警告{mw['count']}回(最大{mw.get('max_pct', 0):.0f}%)"
                MEMORY_WARN_FILE.unlink(missing_ok=True)
        except Exception:
            pass

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

        next_et = (now_et + datetime.timedelta(days=1)).date()
        notrade_reason = self._notrade_reason_for_date(next_et)
        if next_et.weekday() >= 5:
            plan_str = "週末休場"
        elif notrade_reason:
            plan_str = f"ノートレード({notrade_reason})"
        else:
            jst_times = [f"{(h + 13) % 24}:{m:02d}" for h, m in ENTRY_TIMES]
            plan_str  = "・".join(jst_times) + " JST"

        lock_note = self._get_lock_expiry_warning()

        has_issues = bool(entries or exits or mem_warn or recovery_warn or lock_note)
        if not has_issues:
            pushover("SPX Condor", f"✅全正常 | 今日:{plan_str}")
        else:
            lines = [f"📊SPX Condor日次 ({now_jst.strftime('%m/%d')} 09:00JST)"]
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
            lines.append(f"週間:${weekly_pnl:+.0f} / 累計:${total_pnl:+.0f}")
            pushover("SPX Condor 日次", "\n".join(lines))
        log.info("Daily summary sent")

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run_forever(self):
        log.info("=== SPX Condor Bot starting ===")
        pushover("SPX Condor", "🚀起動しました")
        fetch_events_weekly()

        if not self.mkt.connect():
            log.error("Quote context connect failed")
            self.consecutive_start_failures += 1
            save_failures(self.consecutive_start_failures)
            if self.consecutive_start_failures >= 3:
                pushover("Condor 起動失敗3回連続",
                         f"OpenD接続失敗{self.consecutive_start_failures}回", priority=1)
            return

        if not self.eng.connect():
            log.error("Trade context connect failed")
            self.consecutive_start_failures += 1
            save_failures(self.consecutive_start_failures)
            return

        self.consecutive_start_failures = 0
        save_failures(0)
        log.info("OpenD connected successfully")

        self._margin_ok = self.eng.check_startup_margin()

        try:
            while True:
                now = datetime.datetime.now(ET)
                h, m = now.hour, now.minute

                # 20:00 ET = 9:00 JST: daily summary + nightly check
                if h == 20 and m == 0 and "nightly_checked" not in self.traded_times:
                    self.run_daily_summary_jst()
                    self.run_nightly_jst_check()
                    self.traded_times["nightly_checked"] = True
                    jst = zoneinfo.ZoneInfo("Asia/Tokyo")
                    if now.astimezone(jst).day == 1 and "monthly_export" not in self.traded_times:
                        self._export_monthly_pnl_csv()
                        self.traded_times["monthly_export"] = True

                # Hourly memory check
                if m == 0 and f"memcheck_{h}" not in self.traded_times:
                    check_memory_usage()
                    self.traded_times[f"memcheck_{h}"] = True

                # Outside market hours: sleep
                in_market = (h == 9 and m >= 30) or (10 <= h < 16)
                if not in_market:
                    if h >= 16 and self.traded_times:
                        log.info("EOD → resetting traded_times")
                        self.traded_times = {}
                    time.sleep(30)
                    continue

                # No-trade day check
                if is_notrade_today():
                    log.info("No-trade day → sleep 1h")
                    time.sleep(3600)
                    continue

                # Entry window
                if self.should_enter(h, m):
                    if self._margin_ok:
                        self.run_entry()
                    else:
                        log.warning("Entry skipped: margin check failed")
                        self.traded_times[f"{h}:{m:02d}"] = True

                # Exit monitor
                self.check_exits()

                time.sleep(30)

        except KeyboardInterrupt:
            log.info("Stopped by user")
        except Exception as e:
            log.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
            pushover("SPX Condor クラッシュ", str(e)[:200], priority=1)
        finally:
            self.mkt.close()
            self.eng.close()

    def _export_monthly_pnl_csv(self):
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            now_et = datetime.datetime.now(ET)
            prev_month = (now_et.date().replace(day=1) - datetime.timedelta(days=1))
            month_str  = prev_month.strftime("%Y%m")
            usdjpy = 150.0
            try:
                import yfinance as yf
                hist = yf.Ticker("JPY=X").history(period="1d")
                if not hist.empty:
                    usdjpy = float(hist["Close"].iloc[-1])
            except Exception:
                pass
            trades = load_pnl()
            month_prefix  = prev_month.strftime("%Y-%m")
            month_trades  = [t for t in trades if t.get("date", "").startswith(month_prefix)]
            if not month_trades:
                return
            csv_path = REPORTS_DIR / f"condor_{month_str}.csv"
            fieldnames = ["date", "ts", "event", "expiry", "put_sell", "put_buy",
                          "call_sell", "call_buy", "qty", "net_credit",
                          "pnl_usd", "pnl_jpy", "reason", "pl_ratio"]
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for t in month_trades:
                    pnl_usd = t.get("pnl_usd", 0) or 0
                    t["pnl_jpy"] = round(pnl_usd * usdjpy, 0)
                    writer.writerow(t)
            log.info(f"Monthly CSV: {csv_path} ({len(month_trades)} trades)")
            pushover("SPX Condor", f"📄月次CSV: {csv_path.name} ({len(month_trades)}件)")
        except Exception as e:
            log.warning(f"Monthly CSV export: {e}")


if __name__ == "__main__":
    bot = SPXCondorBot()
    bot.run_forever()
