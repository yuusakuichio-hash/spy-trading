#!/usr/bin/env python3
"""
scripts/ground_truth_reconciler.py — Ground Truth Reconciliation Layer

1分毎に外部真実と内部クレームを照合する。

照合対象:
  A. 価格 reality check  — 内部使用価格 vs 外部3ソース majority vote
  B. Trade reality check — ログ上のエントリクレーム vs 実ログ件数
  C. Service health     — systemctl is-active + 直近ログ沈黙検出

外部ソース:
  1. Yahoo Finance  (yfinance)  — ^GSPC / SPY / MES=F
  2. Finnhub API    (requests)  — SPY quote (c field)
  3. CME public delayed data  — ES=F via yfinance (15min delay acceptable)

Usage:
  python3 scripts/ground_truth_reconciler.py          # 単発1回実行
  python3 scripts/ground_truth_reconciler.py --loop   # 1分間隔ループ (LaunchAgent用)
  python3 scripts/ground_truth_reconciler.py --smoke  # smoke test (3回即時実行)
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import yfinance as yf

# ── パス設定 ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# .env を明示ロード (LaunchAgent は shell 環境を継承しないため)
_env_path = ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

from common.pushover_client import send as pushover_send

# ── ログ設定 ─────────────────────────────────────────────────────────────────
LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "ground_truth_reconciler.log"),
    ],
)
log = logging.getLogger("gtr")

# ── 設定定数 ─────────────────────────────────────────────────────────────────
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")  # .env ロード後に評価
PRICE_THRESHOLD_PCT = 1.0        # 乖離アラート閾値(%)
SERVICE_LOG_SILENCE_SEC = 600    # 10分沈黙でゾンビ判定
RECONCILE_STATE_PATH = ROOT / "data" / "ground_truth_state.json"
JST = datetime.timezone(datetime.timedelta(hours=9))

# 監視対象サービス: (service_name, log_file_path)
WATCHED_SERVICES = [
    ("atlas_agent", LOG_DIR / "atlas_agent.log"),
    ("atlas_watchdog", LOG_DIR / "atlas_watchdog.log"),
    ("chronos_bot", LOG_DIR / "chronos_bot.log"),        # HIGH 8 fix: 非存在時 alert
]

# ── 価格フェッチ ─────────────────────────────────────────────────────────────

def fetch_yahoo(symbol: str) -> Optional[float]:
    """Yahoo Finance から最新価格取得。"""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d", interval="1m")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning(f"[Yahoo] {symbol} 取得失敗: {e}")
        return None


def fetch_finnhub(symbol: str) -> Optional[float]:
    """Finnhub から最新価格取得。symbol は SPY 等の株式ティッカー。"""
    key = os.environ.get("FINNHUB_API_KEY", "")
    if not key:
        log.warning("[Finnhub] FINNHUB_API_KEY 未設定")
        return None
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={key}"
        r = requests.get(url, timeout=8)
        data = r.json()
        c = data.get("c")
        if c and float(c) > 0:
            return float(c)
        return None
    except Exception as e:
        log.warning(f"[Finnhub] {symbol} 取得失敗: {e}")
        return None


def majority_vote_spx() -> dict:
    """
    SPX価格を3ソースから取得し majority vote で真実価格を算出。
    MES=F は SPX の先物価格 (≒SPX) として使う。

    Returns:
        {
          "truth_price": float | None,   # majority vote 価格
          "sources": {...},              # 各ソース価格
          "agreement": bool,            # 3ソース中2ソース以上が ±1% 内に一致
          "ts": str,                    # ISO8601
        }
    """
    sources: dict[str, Optional[float]] = {}

    # ソース1: Yahoo ^GSPC (SPX spot)
    sources["yahoo_gspc"] = fetch_yahoo("^GSPC")

    # ソース2: Finnhub SPY (SPY は SPX の 1/10 相当・×10 換算)
    spy_finnhub = fetch_finnhub("SPY")
    sources["finnhub_spy_x10"] = spy_finnhub * 10 if spy_finnhub else None

    # ソース3: Yahoo MES=F (CME E-mini Micro 先物・15min delay OK)
    sources["yahoo_mes_f"] = fetch_yahoo("MES=F")

    valid = [v for v in sources.values() if v is not None]
    log.info(f"[PriceCheck] ソース: {sources}")

    if len(valid) == 0:
        return {"truth_price": None, "sources": sources, "agreement": False, "ts": _now_iso()}

    # median を truth price として採用
    sorted_valid = sorted(valid)
    truth = sorted_valid[len(sorted_valid) // 2]

    # agreement: truth との乖離が ±1% 以内のソースが 2つ以上
    agree_count = sum(1 for v in valid if abs(v - truth) / truth * 100 <= PRICE_THRESHOLD_PCT)
    agreement = agree_count >= 2

    return {
        "truth_price": truth,
        "sources": sources,
        "agreement": agreement,
        "ts": _now_iso(),
    }


def majority_vote_spy() -> dict:
    """SPY価格を3ソースから取得。"""
    sources: dict[str, Optional[float]] = {}
    sources["yahoo_spy"] = fetch_yahoo("SPY")
    sources["finnhub_spy"] = fetch_finnhub("SPY")
    # 参考: ES=F / 10 で SPX/10 ≒ SPY (近似)
    es = fetch_yahoo("ES=F")
    sources["yahoo_es_div10"] = es / 10 if es else None

    valid = [v for v in sources.values() if v is not None]
    if not valid:
        return {"truth_price": None, "sources": sources, "agreement": False, "ts": _now_iso()}

    sorted_valid = sorted(valid)
    truth = sorted_valid[len(sorted_valid) // 2]
    agree_count = sum(1 for v in valid if abs(v - truth) / truth * 100 <= PRICE_THRESHOLD_PCT)

    return {
        "truth_price": truth,
        "sources": sources,
        "agreement": agree_count >= 2,
        "ts": _now_iso(),
    }


# ── A. 価格 reality check ────────────────────────────────────────────────────

def check_price_reality() -> list[dict]:
    """
    Atlas が使っている価格と外部真実を比較。
    anomaly があれば alert dict を返す。
    """
    anomalies = []

    spx_result = majority_vote_spx()
    spy_result = majority_vote_spy()

    for label, result in [("SPX", spx_result), ("SPY", spy_result)]:
        if result["truth_price"] is None:
            log.warning(f"[PriceCheck] {label} 全ソース取得失敗 → スキップ")
            anomalies.append({
                "type": "price_source_failure",
                "symbol": label,
                "detail": "全外部ソース取得不能",
                "result": result,
            })
            continue

        if not result["agreement"]:
            log.warning(f"[PriceCheck] {label} ソース間不一致: {result['sources']}")
            anomalies.append({
                "type": "price_source_disagreement",
                "symbol": label,
                "detail": f"majority vote 不成立: {result['sources']}",
                "truth_price": result["truth_price"],
                "result": result,
            })
        else:
            log.info(f"[PriceCheck] {label} OK truth={result['truth_price']:.2f} agreement=True")

    return anomalies


# ── B. Trade reality check ───────────────────────────────────────────────────

def _count_log_entries(log_path: Path, pattern: str, since_minutes: int) -> int:
    """log_path から直近 since_minutes 分以内の pattern マッチ行数を数える。"""
    if not log_path.exists():
        return 0
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=since_minutes)
    count = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # タイムスタンプ抽出 (例: [2026-04-21 22:59:33] or 2026-04-21T22:59:33)
                m = re.search(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
                if m:
                    try:
                        ts = datetime.datetime.fromisoformat(m.group(1))
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass
                if re.search(pattern, line):
                    count += 1
    except Exception as e:
        log.warning(f"[TradeCheck] {log_path} 読み取りエラー: {e}")
    return count


def check_trade_reality() -> list[dict]:
    """
    ログ上のエントリクレームをチェックする。
    市場時間外はスキップ。
    """
    anomalies = []
    now_et = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=4)  # EDT
    # 市場時間: 09:30-16:00 ET
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)

    if not (market_open <= now_et <= market_close):
        log.info(f"[TradeCheck] 市場時間外 ({now_et.strftime('%H:%M ET')}) → スキップ")
        return []

    # ORB: 22:45-00:00 JST = 09:45-11:00 ET
    # ORB エントリーは場中なら atlas_agent.log / spy_bot.log に記録されるはず
    atlas_log = LOG_DIR / "atlas_agent.log"
    orb_claims = _count_log_entries(atlas_log, r"\[ORB\].*(Entry|entry|ENTRY|エントリー)", 90)
    log.info(f"[TradeCheck] ORB エントリークレーム (直近90分): {orb_claims}")

    # CS_sell: premarket_check が 23:30 ET ±10 分にあるはず
    cs_check_time_et = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    elapsed_from_cs = abs((now_et - cs_check_time_et).total_seconds())
    if elapsed_from_cs < 3600:  # CS窓の1時間以内
        spy_log = ROOT / "data" / "logs" / "atlas_agent.log"
        cs_claims = _count_log_entries(spy_log, r"premarket_check|cs_sell|CS_SELL", 90)
        if cs_claims == 0:
            log.warning("[TradeCheck] CS_sell: premarket_check ログなし (直近90分)")
            anomalies.append({
                "type": "zero_trade_alert",
                "tactic": "cs_sell",
                "detail": "premarket_check ログが直近90分に存在しない",
            })

    return anomalies


# ── C. Service health reality check ─────────────────────────────────────────

def _get_log_last_modified_sec(log_path: Path) -> Optional[float]:
    """ログファイルの最終更新から何秒経過したか。"""
    if not log_path.exists():
        return None
    mtime = log_path.stat().st_mtime
    return time.time() - mtime


def _is_service_active(service_name: str) -> bool:
    """systemctl is-active でサービス稼働確認 (Mac は launchctl)。"""
    # Mac: launchctl list でプロセス確認
    try:
        result = subprocess.run(
            ["pgrep", "-f", service_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_service_health() -> list[dict]:
    """
    サービス active + ログ沈黙を両方チェック。
    active だがログ沈黙 → zombie 判定。
    """
    anomalies = []
    for service_name, log_path in WATCHED_SERVICES:
        if not log_path.exists():
            # HIGH 8 fix (2026-04-22): 非存在時は alert を発生させる (skip 廃止)
            log.warning(f"[ServiceCheck] {service_name}: ログ未存在 → ALERT")
            anomalies.append({
                "type": "log_missing_alert",
                "service": service_name,
                "log_path": str(log_path),
                "detail": f"{service_name} のログファイルが存在しません: {log_path}",
            })
            continue

        is_active = _is_service_active(service_name)
        silence_sec = _get_log_last_modified_sec(log_path)

        if silence_sec is None:
            log.warning(f"[ServiceCheck] {service_name}: ログ読み取り不能")
            continue

        log.info(
            f"[ServiceCheck] {service_name}: active={is_active} "
            f"log_silence={silence_sec:.0f}s"
        )

        if is_active and silence_sec > SERVICE_LOG_SILENCE_SEC:
            log.warning(
                f"[ServiceCheck] {service_name}: ZOMBIE検出 "
                f"(active=True だがログ {silence_sec:.0f}s 沈黙)"
            )
            anomalies.append({
                "type": "zombie_service",
                "service": service_name,
                "detail": f"process active but log silent {silence_sec:.0f}s "
                          f"(threshold={SERVICE_LOG_SILENCE_SEC}s)",
                "log_path": str(log_path),
            })
        elif not is_active:
            log.warning(f"[ServiceCheck] {service_name}: プロセス停止")
            anomalies.append({
                "type": "service_down",
                "service": service_name,
                "detail": "pgrep で見つからない",
            })

    return anomalies


# ── アノマリ通知 ─────────────────────────────────────────────────────────────

def _alert(anomalies: list[dict]) -> None:
    """アノマリをまとめて Pushover 送信。"""
    if not anomalies:
        return

    lines = []
    for a in anomalies:
        atype = a.get("type", "unknown")
        if atype == "price_source_failure":
            lines.append(f"[価格ソース障害] {a['symbol']}: {a['detail']}")
        elif atype == "price_source_disagreement":
            lines.append(
                f"[価格不一致] {a['symbol']} truth={a.get('truth_price','?'):.1f}\n"
                f"  ソース: {a.get('result', {}).get('sources', {})}"
            )
        elif atype == "zero_trade_alert":
            lines.append(f"[ゼロトレードアラート] {a['tactic']}: {a['detail']}")
        elif atype == "zombie_service":
            lines.append(f"[ゾンビプロセス] {a['service']}: {a['detail']}")
        elif atype == "service_down":
            lines.append(f"[サービス停止] {a['service']}")
        elif atype == "log_missing_alert":
            # HIGH 8 fix (2026-04-22): ログ未存在アラート
            lines.append(f"[ログ未存在] {a['service']}: {a['log_path']}")
        else:
            lines.append(f"[ANOMALY] {a}")

    title = f"[GTR] アノマリ {len(anomalies)}件"
    message = "\n".join(lines)
    log.warning(f"Pushover送信: {title}\n{message}")

    pushover_send(
        title,
        message,
        priority=1,
        app_tag="GTR",
    )


# ── 状態永続化 ───────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if RECONCILE_STATE_PATH.exists():
        try:
            return json.loads(RECONCILE_STATE_PATH.read_text())
        except Exception:
            pass
    return {"runs": [], "anomaly_count": 0}


def _save_state(state: dict) -> None:
    RECONCILE_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _now_iso() -> str:
    return datetime.datetime.now(JST).isoformat()


# ── メインロジック ────────────────────────────────────────────────────────────

def run_once() -> dict:
    """1回の reconcile サイクルを実行。結果を返す。"""
    ts = _now_iso()
    log.info(f"=== GTR reconcile cycle start {ts} ===")

    all_anomalies: list[dict] = []

    # A. 価格チェック
    price_anomalies = check_price_reality()
    all_anomalies.extend(price_anomalies)

    # B. トレードチェック
    trade_anomalies = check_trade_reality()
    all_anomalies.extend(trade_anomalies)

    # C. サービスヘルス
    health_anomalies = check_service_health()
    all_anomalies.extend(health_anomalies)

    # アノマリがあれば通知
    _alert(all_anomalies)

    result = {
        "ts": ts,
        "anomalies": all_anomalies,
        "anomaly_count": len(all_anomalies),
        "ok": len(all_anomalies) == 0,
    }
    log.info(f"=== GTR cycle done: anomalies={len(all_anomalies)} ok={result['ok']} ===")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ground Truth Reconciler")
    parser.add_argument("--loop", action="store_true", help="1分間隔でループ実行")
    parser.add_argument("--smoke", action="store_true", help="smoke test: 3回即時実行")
    parser.add_argument("--interval", type=int, default=60, help="ループ間隔(秒)")
    args = parser.parse_args()

    state = _load_state()

    if args.smoke:
        log.info("[smoke] 3回即時実行モード")
        for i in range(3):
            log.info(f"[smoke] run {i+1}/3")
            result = run_once()
            state["runs"].append(result)
            state["anomaly_count"] = state.get("anomaly_count", 0) + result["anomaly_count"]
            _save_state(state)
            if i < 2:
                time.sleep(10)  # smoke では10秒間隔
        log.info(f"[smoke] 完了 total_anomalies={state['anomaly_count']}")
        return

    if args.loop:
        log.info(f"[loop] {args.interval}秒間隔でループ開始")
        while True:
            try:
                result = run_once()
                state["runs"] = state.get("runs", [])[-100:]  # 直近100件保持
                state["runs"].append(result)
                state["anomaly_count"] = state.get("anomaly_count", 0) + result["anomaly_count"]
                _save_state(state)
            except Exception as e:
                log.exception(f"[loop] reconcile cycle 例外: {e}")
            time.sleep(args.interval)
        return

    # デフォルト: 単発1回実行
    result = run_once()
    state["runs"].append(result)
    state["anomaly_count"] = state.get("anomaly_count", 0) + result["anomaly_count"]
    _save_state(state)


if __name__ == "__main__":
    main()
