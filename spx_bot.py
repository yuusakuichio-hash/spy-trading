#!/usr/bin/env python3
"""
SPX/SPY 0DTE/1DTE Credit Spread Bot
Full production implementation with dynamic sizing, tail hedge, event calendar
"""

import os
import sys
import json
import time
import logging
import datetime
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
LOG_DIR = Path("/var/log/spx_bot")
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

# ── Constants ─────────────────────────────────────────────────────────────────
ET = zoneinfo.ZoneInfo("America/New_York")
PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER  = "u2cevk8nktib3sr148rw2hs78ecvux"
EVENTS_FILE    = Path("/root/events.json")
FAILURES_FILE  = Path("/root/spx_bot_failures.json")
TRADE_PASSWORD = os.environ.get("TRADE_PASSWORD", "")
OPEND_HOST     = "127.0.0.1"
OPEND_PORT     = 11111
UNDERLYING     = "SPY"
SPREAD_WIDTH   = 5.0       # $5 fixed
SELL_DELTA     = 0.20
HEDGE_DELTA    = 0.05
HEDGE_MAX_COST = 10.0      # $10 max per hedge contract
PROFIT_TARGET  = 0.50      # 50% of credit
STOP_LOSS_MULT = 2.00      # 200% of credit
FORCE_CLOSE_H  = 15
FORCE_CLOSE_M  = 50
ENTRY_TIMES    = [(10, 30), (14, 0)]

# US market holidays 2026 (NYSE)
US_HOLIDAYS_2026 = {
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
}

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
            if (datetime.datetime.now() - last).total_seconds() > 86400:
                return 0
            return int(data.get("count", 0))
    except Exception:
        pass
    return 0

def save_failures(count: int):
    try:
        FAILURES_FILE.write_text(json.dumps({
            "count": count,
            "last": datetime.datetime.now().isoformat()
        }))
    except Exception as e:
        log.warning(f"Could not save failure count: {e}")

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
    if tomorrow in US_HOLIDAYS_2026:
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

# ── moomoo futu-api client ────────────────────────────────────────────────────
try:
    from futu import OpenQuoteContext, OpenUSTradeContext, TrdMarket, TrdEnv
    from futu import TrdSide, OrderType, RET_OK
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
        except Exception as e:
            log.error(f"Quote connect failed: {e}")
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
            ktype=ft.KLType.K_DAY, max_count=30
        )
        if ret == RET_OK and len(data) >= 20:
            return float(data["close"].tail(20).mean())
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

    def connect(self):
        if not FUTU_AVAILABLE:
            return False
        try:
            self.trade_ctx = OpenUSTradeContext(
                host=OPEND_HOST, port=OPEND_PORT
            )
            log.info("Trade context connected")

            # Resolve account ID first (determines REAL vs SIMULATE)
            self._resolve_account()

            # ── Unlock trade (REAL account only) ─────────────────────────
            if self.trade_env == TrdEnv.REAL:
                if TRADE_PASSWORD:
                    ret, data = self.trade_ctx.unlock_trade(password=TRADE_PASSWORD)
                    if ret != RET_OK:
                        log.error(f"unlock_trade failed: {data}")
                        pushover(
                            "取引ロック解除失敗",
                            f"unlock_trade failed: {str(data)[:150]}",
                            priority=1,
                        )
                        self.trade_ctx.close()
                        self.trade_ctx = None
                        return False
                    log.info("unlock_trade: success")
                    pushover("取引ロック解除成功", "Bot起動・取引ロック解除完了", priority=0)
                else:
                    log.warning("TRADE_PASSWORD not set; skipping unlock_trade")
            else:
                log.info(f"SIMULATE mode ({self.account_id}); unlock_trade不要")

            return True
        except Exception as e:
            log.error(f"Trade connect failed: {e}")
            return False

    def _resolve_account(self):
        """Find the best available account: prefer REAL, fall back to SIMULATE."""
        ret, data = self.trade_ctx.get_acc_list()
        if ret != RET_OK or data.empty:
            log.warning("get_acc_list failed; account_id unresolved")
            return
        # Prefer REAL account
        real = data[data["trd_env"] == "REAL"]
        if not real.empty:
            self.account_id = str(int(real.iloc[0]["acc_id"]))
            self.trade_env = TrdEnv.REAL
            log.info(f"Resolved REAL account: acc_id={self.account_id}")
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
        return 2500.0

    def calc_position_size(self, cash: float, vix: float, vix_prev: Optional[float]) -> int:
        """Dynamic position sizing per strategy spec."""
        now = datetime.datetime.now(ET)
        month = now.month
        weekday = now.weekday()  # 0=Mon, 4=Fri

        # Base ratio
        ratio = 0.20

        # VIX spike +20%
        if vix_prev and vix >= vix_prev * 1.20:
            ratio = 0.40
            log.info(f"VIX spike +20% ({vix_prev:.1f}→{vix:.1f}) → ratio 40%")
        # OpEx week
        elif is_notrade_today():
            ratio = 0.30
        # Friday or Monday
        elif weekday in (0, 4):
            ratio = 0.25

        # Seasonal multiplier
        if month in (9, 10):
            ratio *= 0.5
            log.info(f"Seasonal Sep/Oct → ratio ×0.5 = {ratio:.0%}")
        elif month in (7, 11):
            ratio *= 1.5
            log.info(f"Seasonal Jul/Nov → ratio ×1.5 = {ratio:.0%}")

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

    def get_expiry(self) -> str:
        """0DTE on Mon/Wed/Fri, 1DTE on Tue/Thu."""
        now = datetime.datetime.now(ET)
        wd  = now.weekday()
        if wd in (0, 2, 4):  # Mon, Wed, Fri
            return now.strftime("%Y-%m-%d")
        else:
            next_day = now + datetime.timedelta(days=1)
            return next_day.strftime("%Y-%m-%d")

    def should_enter(self, current_hour: int, current_min: int) -> bool:
        for h, m in ENTRY_TIMES:
            key = f"{h}:{m:02d}"
            if current_hour == h and current_min == m and key not in self.traded_times:
                return True
        return False

    def run_entry(self):
        now    = datetime.datetime.now(ET)
        expiry = self.get_expiry()

        spy      = self.mkt.get_spy_price()
        sma      = self.mkt.get_sma20()
        vix      = self.mkt.get_vix()
        vix_prev = self.mkt.get_vix_prev_close()

        if spy is None or sma is None or vix is None:
            log.error("Market data unavailable, skipping entry")
            return

        log.info(f"SPY={spy:.2f} SMA20={sma:.2f} VIX={vix:.2f} VIX_prev={vix_prev}")

        # VIX gate
        if vix >= 25:
            log.info(f"VIX={vix:.1f} >= 25 → no trade (gate)")
            return

        direction = "bull_put" if spy > sma else "bear_call"
        log.info(f"Direction: {direction} (SPY {'>' if spy > sma else '<'} SMA20)")

        # 14:00 entry: only if direction matches 10:30 direction
        if now.hour == 14:
            morning_dir = self.traded_times.get("direction")
            if morning_dir and morning_dir != direction:
                log.info(f"14:00 entry: direction mismatch ({morning_dir} vs {direction}) → skip")
                return

        cash = self.eng.get_account_cash()
        qty  = self.eng.calc_position_size(cash, vix, vix_prev)

        # Get option chain
        chain = self.mkt.get_option_chain(expiry, direction)

        # Find sell strike (delta ~0.20)
        sell_opt = self.mkt.find_strike_by_delta(chain, SELL_DELTA, "sell")
        if not sell_opt:
            log.warning("Could not find sell strike, skipping")
            return

        sell_strike = sell_opt.get("strike_price", 0)
        buy_strike  = sell_strike - SPREAD_WIDTH if direction == "bull_put" else sell_strike + SPREAD_WIDTH

        # Find buy option at target strike
        candidates = [o for o in chain if abs(o.get("strike_price", 0) - buy_strike) < 1.5]
        if not candidates:
            log.warning(f"Could not find buy strike near {buy_strike}, skipping")
            return
        buy_opt = min(candidates, key=lambda o: abs(o.get("strike_price", 0) - buy_strike))

        sell_code = sell_opt.get("code", "")
        buy_code  = buy_opt.get("code", "")

        log.info(f"Spread: SELL {sell_code} @ strike {sell_strike} / BUY {buy_code} @ strike {buy_strike} / qty={qty}")
        success = self.eng.place_spread(sell_code, buy_code, qty, direction)

        if success:
            time_key = f"{now.hour}:{now.minute:02d}"
            self.traded_times[time_key] = True

            # Record morning direction for 14:00 filter
            if now.hour == 10:
                self.traded_times["direction"] = direction

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

    def check_exits(self):
        """Check P&L on open positions and apply profit/loss/force-close rules."""
        now = datetime.datetime.now(ET)
        positions = self.eng.get_open_positions()

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

            if pl_ratio >= PROFIT_TARGET:
                log.info(f"Profit target {pl_ratio:.1%} >= {PROFIT_TARGET:.0%} → closing {pos.get('code')}")
                self.eng.close_all_positions("profit_target")
                break
            elif pl_ratio <= -STOP_LOSS_MULT:
                log.info(f"Stop loss {pl_ratio:.1%} <= -{STOP_LOSS_MULT:.0%} → closing {pos.get('code')}")
                self.eng.close_all_positions("stop_loss")
                break

    def run_forever(self):
        log.info("=== SPX Bot starting ===")
        fetch_events_weekly()

        if not self.mkt.connect():
            log.error("Cannot connect to OpenD (quote context)")
            self.consecutive_start_failures += 1
            save_failures(self.consecutive_start_failures)
            if self.consecutive_start_failures >= 3:
                pushover(
                    "Bot起動失敗3回連続",
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

        try:
            while True:
                now = datetime.datetime.now(ET)
                h, m = now.hour, now.minute

                # Market hours: 9:30–16:00 ET
                if not (9 <= h < 16 or (h == 9 and m >= 30)):
                    if h >= 16:
                        # EOD reset
                        if self.traded_times:
                            log.info("EOD → resetting traded_times")
                            self.traded_times = {}
                    time.sleep(30)
                    continue

                # No-trade check
                if is_notrade_today():
                    log.info("No-trade day → sleeping 1h")
                    time.sleep(3600)
                    continue

                # Entry check
                if self.should_enter(h, m):
                    self.run_entry()

                # Exit check
                self.check_exits()

                time.sleep(30)

        except KeyboardInterrupt:
            log.info("Bot stopped by user")
        except Exception as e:
            log.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
            pushover("Bot クラッシュ", str(e)[:200], priority=1)
        finally:
            self.mkt.close()
            self.eng.close()


if __name__ == "__main__":
    bot = SPXBot()
    bot.run_forever()
