#!/usr/bin/env python3
"""
download_1dte_data.py — 1DTE option data downloader

既存 data/thetadata/ は0DTE(当日満期)のみ。
本スクリプトは trade_date t に expiration = t+1 business day を指定して
1DTE option データをDLし、data/thetadata_1dte/ に保存する。

既存 Standard サブスクリプションで動作確認済 (SPY 2026-04-15→2026-04-16 実証)。
"""

from __future__ import annotations

import os
import sys
import time
import json
import io
import logging
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import pandas as pd
except ImportError:
    print("pandas required. pip3 install pandas pyarrow")
    sys.exit(1)

BASE_URL = "http://localhost:25503"
DATA_DIR = Path("/Users/yuusakuichio/trading/data/thetadata_1dte")
LOG_DIR = Path("/Users/yuusakuichio/trading/data/logs")
LOG_FILE = LOG_DIR / "1dte_download.log"
PROGRESS_FILE = DATA_DIR / "progress.json"

SYMBOLS_PRIMARY = ["SPY", "QQQ", "IWM"]
SYMBOLS_SECONDARY = ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]

START_DATE = "2024-01-02"
END_DATE = "2026-04-16"
INTERVAL = "5m"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed": {}, "failed": {}}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def get_expirations(symbol: str) -> list[str]:
    last_err = None
    for attempt in range(5):
        try:
            resp = requests.get(
                f"{BASE_URL}/v3/option/list/expirations",
                params={"symbol": symbol}, timeout=30,
            )
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")[1:]
            exps = []
            for line in lines:
                parts = line.strip().split(",")
                if len(parts) >= 2:
                    d = parts[1].strip().strip('"')
                    if START_DATE <= d <= "2026-04-17":
                        exps.append(d)
            return sorted(exps)
        except Exception as e:
            last_err = e
            log.warning(f"get_expirations {symbol} attempt {attempt+1}/5: {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"get_expirations {symbol} failed after 5 attempts: {last_err}")


def trade_exp_pairs(exps: list[str]) -> list[tuple[str, str]]:
    """Build (trade_date, expiration) pairs for 1DTE."""
    pairs = []
    for i in range(1, len(exps)):
        trade_date = exps[i - 1]
        expiration = exps[i]
        if START_DATE <= trade_date <= END_DATE:
            dt_trade = datetime.strptime(trade_date, "%Y-%m-%d").date()
            dt_exp = datetime.strptime(expiration, "%Y-%m-%d").date()
            delta_days = (dt_exp - dt_trade).days
            if 1 <= delta_days <= 5:
                pairs.append((trade_date, expiration))
    return pairs


def _fetch_with_retry(url: str, params: dict, timeout: int = 60, retries: int = 3) -> Optional[pd.DataFrame]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 200 and resp.text.strip():
                if "subscription" in resp.text.lower():
                    return None
                df = pd.read_csv(io.StringIO(resp.text))
                if len(df) > 0:
                    return df
                return None
            # 5xx: retry; 4xx: give up
            if 500 <= resp.status_code < 600:
                time.sleep(1 + attempt)
                continue
            return None
        except requests.exceptions.Timeout:
            time.sleep(1 + attempt)
            continue
        except Exception:
            time.sleep(1 + attempt)
            continue
    return None


def fetch_fo(symbol: str, trade_date: str, expiration: str) -> Optional[pd.DataFrame]:
    exp_nd = expiration.replace("-", "")
    td_nd = trade_date.replace("-", "")
    params = {
        "symbol": symbol,
        "expiration": exp_nd,
        "start_date": td_nd,
        "end_date": td_nd,
        "interval": INTERVAL,
    }
    return _fetch_with_retry(
        f"{BASE_URL}/v3/option/history/greeks/first_order", params, timeout=60, retries=3,
    )


def fetch_eod(symbol: str, expiration: str, query_date: str) -> Optional[pd.DataFrame]:
    exp_nd = expiration.replace("-", "")
    qd_nd = query_date.replace("-", "")
    params = {
        "symbol": symbol,
        "expiration": exp_nd,
        "start_date": qd_nd,
        "end_date": qd_nd,
    }
    return _fetch_with_retry(
        f"{BASE_URL}/v3/option/history/greeks/eod", params, timeout=60, retries=3,
    )


def save_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, compression="zstd", index=False)


def process_one(symbol: str, trade_date: str, expiration: str, progress: dict) -> tuple[bool, bool, bool]:
    key = f"{symbol}_{trade_date}"
    if progress["completed"].get(key):
        return True, True, True

    td_nd = trade_date.replace("-", "")
    date_dir = DATA_DIR / td_nd

    fo_path = date_dir / f"greeks_first_order_{symbol}.parquet"
    td_eod_path = date_dir / f"greeks_eod_{symbol}.parquet"
    exp_eod_path = date_dir / f"greeks_expiration_eod_{symbol}.parquet"

    ok_fo = ok_td_eod = ok_exp_eod = False

    if not fo_path.exists():
        df = fetch_fo(symbol, trade_date, expiration)
        if df is not None:
            save_parquet(df, fo_path)
            ok_fo = True
    else:
        ok_fo = True

    if not td_eod_path.exists():
        df = fetch_eod(symbol, expiration, trade_date)
        if df is not None:
            save_parquet(df, td_eod_path)
            ok_td_eod = True
    else:
        ok_td_eod = True

    if not exp_eod_path.exists():
        df = fetch_eod(symbol, expiration, expiration)
        if df is not None:
            save_parquet(df, exp_eod_path)
            ok_exp_eod = True
    else:
        ok_exp_eod = True

    if ok_fo and ok_td_eod and ok_exp_eod:
        progress["completed"][key] = True
    else:
        progress.setdefault("failed", {})[key] = {
            "fo": ok_fo, "td_eod": ok_td_eod, "exp_eod": ok_exp_eod,
            "trade_date": trade_date, "expiration": expiration,
        }

    return ok_fo, ok_td_eod, ok_exp_eod


_progress_lock = threading.Lock()


def download_symbol(symbol: str, progress: dict, max_pairs: Optional[int] = None,
                    workers: int = 3) -> None:
    log.info(f"=== {symbol} ===")
    try:
        exps = get_expirations(symbol)
    except Exception as e:
        log.error(f"get_expirations failed for {symbol}: {e}")
        return
    pairs = trade_exp_pairs(exps)
    if max_pairs:
        pairs = pairs[-max_pairs:]   # 新しい日から
    log.info(f"{symbol}: {len(pairs)} pairs")

    remaining = [(td, ex) for td, ex in pairs if not progress["completed"].get(f"{symbol}_{td}")]
    if not remaining:
        log.info(f"{symbol}: all done")
        return
    log.info(f"{symbol}: {len(remaining)} remaining")

    start = time.time()
    done = 0
    errors = 0

    def _task(td_ex):
        td, ex = td_ex
        try:
            ok_fo, ok_td, ok_exp = process_one(symbol, td, ex, progress)
            return td, ex, (ok_fo and ok_td and ok_exp), None
        except Exception as e:
            return td, ex, False, str(e)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_task, pair) for pair in remaining]
        for i, fut in enumerate(as_completed(futs)):
            td, exp, ok, err = fut.result()
            if ok:
                done += 1
            else:
                errors += 1
            if (i + 1) % 25 == 0:
                with _progress_lock:
                    save_progress(progress)
                elapsed = time.time() - start
                rate = (i + 1) / max(elapsed, 0.001)
                eta_s = (len(remaining) - (i + 1)) / max(rate, 0.001)
                log.info(f"{symbol}: {i+1}/{len(remaining)} ok={done} err={errors} "
                         f"{rate:.1f}/s eta={eta_s:.0f}s")

    with _progress_lock:
        save_progress(progress)
    log.info(f"{symbol}: FINISH done={done} err={errors} elapsed={time.time()-start:.0f}s")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        resp = requests.get(f"{BASE_URL}/v3/option/list/expirations?symbol=SPY", timeout=5)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"ThetaTerminal not reachable: {e}")
        sys.exit(2)

    progress = load_progress()

    for sym in SYMBOLS_PRIMARY:
        download_symbol(sym, progress)

    for sym in SYMBOLS_SECONDARY:
        download_symbol(sym, progress)

    save_progress(progress)
    log.info("ALL DONE")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        progress = load_progress()
        sym = sys.argv[1]
        max_pairs = int(sys.argv[2]) if len(sys.argv) > 2 else None
        download_symbol(sym, progress, max_pairs=max_pairs)
        save_progress(progress)
    else:
        main()
