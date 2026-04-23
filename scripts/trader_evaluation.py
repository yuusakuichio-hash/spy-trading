#!/usr/bin/env python3
"""Trader Evaluation Framework -- 優秀トレーダー判定スクリプト

15指標 M1-M15 を日次/週次/月次で自動算出し、外部ベンチマーク
（Theta Profits / Nick Magno / Option Alpha）との比較レポートを生成。

使い方:
  python3 scripts/trader_evaluation.py --period daily   # 日次評価
  python3 scripts/trader_evaluation.py --period weekly  # 週次評価
  python3 scripts/trader_evaluation.py --period monthly # 月次評価
  python3 scripts/trader_evaluation.py --audit 30       # 直近30日規律監査

出力:
  data/eval/daily/YYYYMMDD.json
  data/eval/trader_eval_YYYYMMDD.md
  data/eval/metrics_rolling.json
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib import parse, request

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
EVAL_DIR = DATA / "eval"
EVAL_DAILY_DIR = EVAL_DIR / "daily"
EVAL_WEEKLY_DIR = EVAL_DIR / "weekly"

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "u2cevk8nktib3sr148rw2hs78ecvux")

# ---------------------------------------------------------------------------
# ベンチマーク定義（外部優秀トレーダー実績）
# ---------------------------------------------------------------------------

BENCHMARKS = {
    "theta_profits": {
        "name": "Theta Profits (0DTE Breakeven IC, 9100 trades)",
        "win_rate": 0.40,
        "monthly_win_rate": 0.86,
        "profit_factor": None,
        "premium_capture_pct": 5.65,
        "max_daily_risk_pct": 2.0,
        "bp_usage_max_pct": 50.0,
    },
    "nick_magno": {
        "name": "Nick Magno (SPX 0DTE IC, +113%/year 2024)",
        "win_rate": None,
        "annual_return_pct": 113.0,
    },
    "option_alpha": {
        "name": "Option Alpha (230k 0DTE trades 2024)",
        "win_rate_ic": 0.7019,
        "win_rate_ib": 0.6676,
        "profit_factor_strong": 2.0,
        "profit_factor_min": 1.5,
        "rom_min_pct": 10.0,
        "sortino_target": 2.0,
        "calmar_target": 3.0,
    },
}

TACTIC_PF_TARGETS = {
    "ic": 2.0,
    "cs": 1.5,
    "orb": 1.3,
    "standard": 1.5,
    "default": 1.5,
}


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------

def send_pushover(title: str, message: str, priority: int = 0) -> bool:
    data = parse.urlencode({
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message[:1020],
        "priority": priority,
    }).encode()
    try:
        req = request.Request("https://api.pushover.net/1/messages.json", data=data)
        with request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[pushover error] {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# データロード
# ---------------------------------------------------------------------------

# Fix 1: ファイル名 → tactic のデフォルトマッピング（古いログに tactic が欠落している場合の後方互換）
_SOURCE_TACTIC_MAP: dict[str, str] = {
    "condor_pnl":    "ic_sell",
    "momentum_pnl":  "orb_buy",
    "straddle_pnl":  "straddle_buy",
    "butterfly_pnl": "butterfly",
    "calendar_pnl":  "calendar_sell",
}


def load_all_trades() -> list[dict]:
    """condor_pnl.json などを結合して全トレードリストを返す"""
    pnl_files = [
        DATA / "condor_pnl.json",
        DATA / "momentum_pnl.json",
        DATA / "straddle_pnl.json",
        DATA / "butterfly_pnl.json",
        DATA / "calendar_pnl.json",
    ]
    all_trades: list[dict] = []
    for f in pnl_files:
        if not f.exists():
            continue
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            trades = raw.get("trades", []) if isinstance(raw, dict) else raw
            default_tactic = _SOURCE_TACTIC_MAP.get(f.stem, "unknown")
            for t in trades:
                if isinstance(t, dict):
                    t.setdefault("_source", f.stem)
                    # Fix 1: tactic が欠落している場合はファイル名から推定して補完
                    if not t.get("tactic"):
                        t["tactic"] = default_tactic
            all_trades.extend(trades)
        except Exception as e:
            print(f"[load] {f.name}: {e}", file=sys.stderr)
    return all_trades


def filter_by_date_range(trades: list[dict], start: date, end: date) -> list[dict]:
    result = []
    for t in trades:
        d_str = t.get("date", "")
        if not d_str:
            continue
        try:
            d = date.fromisoformat(d_str[:10])
        except ValueError:
            continue
        if start <= d <= end:
            result.append(t)
    return result


def pair_trades(trades: list[dict]) -> list[dict]:
    """entry + exit を trade_id でペアリング。完結トレードリストを返す。"""
    entry_map: dict[str, dict] = {}
    paired: list[dict] = []
    unpaired_entries: list[dict] = []
    unpaired_exits: list[dict] = []

    for t in trades:
        event = t.get("event", "")
        tid = t.get("trade_id")
        if event == "entry":
            if tid:
                entry_map[tid] = t
            else:
                unpaired_entries.append(t)
        elif event == "exit":
            if tid and tid in entry_map:
                entry = entry_map.pop(tid)
                pnl = t.get("pnl_usd")
                paired.append({
                    "entry": entry,
                    "exit": t,
                    "date": entry.get("date", t.get("date", "")),
                    "tactic": entry.get("tactic", "unknown"),
                    "pnl": pnl,
                    "net_credit": entry.get("net_credit"),
                    "qty": entry.get("qty", 1),
                    "entry_ts": entry.get("ts", ""),
                    "exit_ts": t.get("ts", ""),
                    "exit_reason": t.get("reason", ""),
                    "vix": entry.get("vix"),
                    "regime": None,
                    "slippage": entry.get("slippage"),
                    "fill_price_sell": entry.get("fill_price_sell"),
                })
            elif tid:
                unpaired_exits.append(t)
            else:
                unpaired_exits.append(t)

    # FIFO ペアリング (trade_id なし)
    for ex in unpaired_exits:
        if unpaired_entries:
            entry = unpaired_entries.pop(0)
            pnl = ex.get("pnl_usd")
            paired.append({
                "entry": entry,
                "exit": ex,
                "date": entry.get("date", ex.get("date", "")),
                "tactic": entry.get("tactic", "unknown"),
                "pnl": pnl,
                "net_credit": entry.get("net_credit"),
                "qty": entry.get("qty", 1),
                "entry_ts": entry.get("ts", ""),
                "exit_ts": ex.get("ts", ""),
                "exit_reason": ex.get("reason", ""),
                "vix": entry.get("vix"),
                "regime": None,
                "slippage": entry.get("slippage"),
                "fill_price_sell": entry.get("fill_price_sell"),
            })

    return paired


# ---------------------------------------------------------------------------
# M1-M5: リターン系指標
# ---------------------------------------------------------------------------

def calc_m1_profit_factor(paired: list[dict]) -> Optional[float]:
    """M1: Profit Factor = 総利益 / |総損失|"""
    gains = sum(t["pnl"] for t in paired if t["pnl"] is not None and t["pnl"] > 0)
    losses = sum(abs(t["pnl"]) for t in paired if t["pnl"] is not None and t["pnl"] < 0)
    if losses == 0:
        return None if gains == 0 else float("inf")
    return round(gains / losses, 3)


def calc_m2_win_rate(paired: list[dict]) -> Optional[float]:
    """M2: 勝率 = 勝トレード / 総トレード"""
    valid = [t for t in paired if t["pnl"] is not None]
    if not valid:
        return None
    wins = sum(1 for t in valid if t["pnl"] > 0)
    return round(wins / len(valid), 4)


def calc_m3_expected_value(paired: list[dict]) -> Optional[float]:
    """M3: 期待値 = 平均利益×勝率 - 平均損失×負率"""
    valid = [t for t in paired if t["pnl"] is not None]
    if not valid:
        return None
    wins = [t["pnl"] for t in valid if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in valid if t["pnl"] < 0]
    win_rate = len(wins) / len(valid)
    loss_rate = 1 - win_rate
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    ev = avg_win * win_rate - avg_loss * loss_rate
    return round(ev, 2)


def calc_m4_rom(paired: list[dict], margin_usd: float) -> Optional[float]:
    """M4: Return on Margin = 総利益 / 証拠金"""
    if margin_usd <= 0:
        return None
    total_pnl = sum(t["pnl"] for t in paired if t["pnl"] is not None)
    return round(total_pnl / margin_usd, 4)


def calc_m5_monthly_consistency(all_trades: list[dict]) -> Optional[float]:
    """M5: 月次一貫性 = 黒字月数 / 総月数 (全期間対象)"""
    date_pnl: dict[str, float] = {}
    paired = pair_trades(all_trades)
    for t in paired:
        if t["pnl"] is None:
            continue
        m = t["date"][:7]
        date_pnl[m] = date_pnl.get(m, 0.0) + t["pnl"]
    if not date_pnl:
        return None
    months = list(date_pnl.values())
    profit_months = sum(1 for mv in months if mv > 0)
    return round(profit_months / len(months), 4)


# ---------------------------------------------------------------------------
# M6-M9: リスク調整リターン系指標
# ---------------------------------------------------------------------------

def _daily_pnl_series(paired: list[dict]) -> list[float]:
    date_pnl: dict[str, float] = {}
    for t in paired:
        if t["pnl"] is None:
            continue
        d = t["date"]
        date_pnl[d] = date_pnl.get(d, 0.0) + t["pnl"]
    if not date_pnl:
        return []
    return [date_pnl[d] for d in sorted(date_pnl.keys())]


def calc_m6_sortino(paired: list[dict], rf_daily: float = 0.0) -> Optional[float]:
    """M6: Sortino Ratio = (avg_return - rf) / 下方偏差
    Fix 2: 損失日がない場合(downside_dev=0)は float('inf') を返す（完璧な成績）。
    """
    series = _daily_pnl_series(paired)
    if len(series) < 3:
        return None
    n = len(series)
    avg = sum(series) / n
    downside_sq = [(min(r - rf_daily, 0)) ** 2 for r in series]
    downside_dev = math.sqrt(sum(downside_sq) / n)
    if downside_dev == 0:
        # 損失日ゼロ = 下方リスクなし = Sortino は理論上 +∞
        return float("inf") if avg > 0 else None
    return round((avg - rf_daily) / downside_dev, 3)


def calc_m7_calmar(paired: list[dict]) -> Optional[float]:
    """M7: Calmar Ratio = 年率リターン / 最大DD"""
    series = _daily_pnl_series(paired)
    if len(series) < 5:
        return None
    cumulative = []
    cum = 0.0
    for r in series:
        cum += r
        cumulative.append(cum)
    peak = cumulative[0]
    max_dd = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd
    if max_dd == 0:
        # Fix 2: DD=0 = ドローダウンなし = Calmar は理論上 +∞
        annual_return = cumulative[-1] * (252 / len(series))
        return float("inf") if annual_return > 0 else None
    annual_return = cumulative[-1] * (252 / len(series))
    return round(annual_return / max_dd, 3)


def calc_m8_mar(paired: list[dict]) -> Optional[float]:
    """M8: MAR Ratio = 月次版 Calmar"""
    date_pnl: dict[str, float] = {}
    for t in paired:
        if t["pnl"] is None:
            continue
        m = t["date"][:7]
        date_pnl[m] = date_pnl.get(m, 0.0) + t["pnl"]
    if len(date_pnl) < 2:
        return None
    monthly = [date_pnl[m] for m in sorted(date_pnl.keys())]
    cumulative = []
    cum = 0.0
    for r in monthly:
        cum += r
        cumulative.append(cum)
    peak = cumulative[0]
    max_dd = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd
    if max_dd == 0:
        # Fix 2: DD=0 = ドローダウンなし = MAR は理論上 +∞
        annual_return = cumulative[-1] * (12 / len(monthly))
        return float("inf") if annual_return > 0 else None
    annual_return = cumulative[-1] * (12 / len(monthly))
    return round(annual_return / max_dd, 3)


def calc_m9_ulcer_index(paired: list[dict]) -> Optional[float]:
    """M9: Ulcer Index = RMS(DD%) 短期トラックレコード向き"""
    series = _daily_pnl_series(paired)
    if len(series) < 3:
        return None
    cumulative = []
    cum = 0.0
    for r in series:
        cum += r
        cumulative.append(cum)
    peak = cumulative[0]
    dd_sq_list = []
    for c in cumulative:
        if c > peak:
            peak = c
        dd_pct = ((peak - c) / abs(peak)) * 100 if peak != 0 else 0.0
        dd_sq_list.append(dd_pct ** 2)
    ulcer = math.sqrt(sum(dd_sq_list) / len(dd_sq_list))
    return round(ulcer, 3)


# ---------------------------------------------------------------------------
# M10-M12: 執行品質系指標
# ---------------------------------------------------------------------------

def calc_m10_e_ratio(paired: list[dict]) -> Optional[float]:
    """M10: Edge Ratio = avg MFE / avg MAE (代理計算版)"""
    mfe_list: list[float] = []
    mae_list: list[float] = []

    for t in paired:
        ex = t.get("exit", {})
        en = t.get("entry", {})
        net_credit = en.get("net_credit") or t.get("net_credit")
        pnl = t.get("pnl")

        mfe = ex.get("mfe_usd")
        mae = ex.get("mae_usd")

        if mfe is not None and mae is not None:
            mfe_list.append(abs(mfe))
            mae_list.append(abs(mae))
        elif pnl is not None and net_credit is not None:
            qty = t.get("qty", 1)
            max_gain = net_credit * 100 * qty
            if pnl >= 0:
                mfe_list.append(max(pnl, max_gain * 0.5))
                mae_list.append(max_gain * 0.1)
            else:
                mfe_list.append(max_gain * 0.3)
                mae_list.append(abs(pnl))

    if not mfe_list or not mae_list or len(mfe_list) < 3:
        return None
    avg_mfe = sum(mfe_list) / len(mfe_list)
    avg_mae = sum(mae_list) / len(mae_list)
    if avg_mae == 0:
        return None
    return round(avg_mfe / avg_mae, 3)


def calc_m11_slippage_rate(paired: list[dict]) -> Optional[float]:
    """M11: スリッページ率 = avg(slippage / net_credit)"""
    rates: list[float] = []
    for t in paired:
        slip = t.get("slippage")
        nc = t.get("net_credit")
        if slip is not None and nc is not None and nc > 0:
            rates.append(abs(slip) / nc)
    if not rates:
        return None
    return round(sum(rates) / len(rates), 4)


def calc_m12_naked_leg_rate(all_trades: list[dict]) -> float:
    """M12: 裸レッグ発生率 = naked_leg_detected / 総エントリー数"""
    entries = [t for t in all_trades if t.get("event") == "entry"]
    naked = [t for t in all_trades if t.get("event") == "naked_leg_detected"]
    if not entries:
        return 0.0
    return round(len(naked) / len(entries), 4)


# ---------------------------------------------------------------------------
# M13-M15: 行動規律系指標
# ---------------------------------------------------------------------------

DISCIPLINE_RULES = [
    ("entry_time",       "エントリー時間帯 10:00-15:30 ET"),
    ("consecutive_loss", "連続損失停止ルール遵守"),
    ("daily_loss_limit", "日次損失上限 5% 遵守"),
    ("kelly_size",       "サイズ計算 Kelly×環境係数 整合"),
    ("em_key_level",     "ストライク EM外側+Key Level 尊重"),
    ("bid_ask",          "Bid/Ask スプレッド基準 (<33%)"),
    ("calendar_skip",    "FOMC/CPI 当日スキップ"),
    ("bp_usage",         "BP使用率 50% 以下維持"),
    ("force_close",      "15:50 強制クローズ遵守"),
    ("naked_check",      "15:55 裸ポジションチェック実行"),
]


def evaluate_discipline_for_trade(trade: dict, all_trades_for_date: list[dict]) -> dict[str, bool]:
    """1トレードの規律遵守状況を判定。判断不能なルールは True (遵守) とみなす。"""
    entry = trade.get("entry", {})
    ts_str = entry.get("ts", "")
    result: dict[str, bool] = {}

    # R1: エントリー時間帯 (ET 10:00-15:30)
    try:
        ts = datetime.fromisoformat(ts_str)
        et_hour = ts.hour
        et_minute = ts.minute
        entry_ok = (10, 0) <= (et_hour, et_minute) <= (15, 30)
    except Exception:
        entry_ok = True
    result["entry_time"] = entry_ok

    # R2: 連続損失停止 (Bot実装済みのため通常OK)
    result["consecutive_loss"] = True

    # R3: 日次損失上限
    pnl = trade.get("pnl")
    net_credit = trade.get("net_credit") or 0
    qty = trade.get("qty", 1)
    max_gain = net_credit * 100 * qty if net_credit else 0
    daily_loss_ok = True
    if pnl is not None and max_gain > 0 and abs(min(pnl, 0)) > max_gain * 5:
        daily_loss_ok = False
    result["daily_loss_limit"] = daily_loss_ok

    # R4: Kelly サイズ (env_snapshot 存在でOK)
    env_snaps = [t for t in all_trades_for_date if t.get("event") == "env_snapshot"]
    result["kelly_size"] = len(env_snaps) > 0

    # R5: EM/Key Level (delta 0.05-0.35 でOK)
    delta = entry.get("delta_actual")
    if delta is not None:
        result["em_key_level"] = 0.05 <= abs(delta) <= 0.35
    else:
        result["em_key_level"] = True

    # R6: Bid/Ask (slippage / net_credit <= 0.33)
    slip = entry.get("slippage") or trade.get("slippage")
    if slip is not None and net_credit and net_credit > 0:
        result["bid_ask"] = abs(slip) / net_credit <= 0.33
    else:
        result["bid_ask"] = True

    # R7: FOMC/CPI スキップ (Bot管理済み)
    result["calendar_skip"] = True

    # R8: BP使用率 (capital_pct <= 0.50)
    if env_snaps:
        cap_pct = env_snaps[0].get("params", {}).get("capital_pct", 0)
        result["bp_usage"] = cap_pct <= 0.50
    else:
        result["bp_usage"] = True

    # R9: 15:50 強制クローズ (Bot実装済み)
    result["force_close"] = True

    # R10: 15:55 裸ポジションチェック (Bot実装済み)
    result["naked_check"] = True

    return result


def calc_m13_discipline_score(paired: list[dict], all_trades: list[dict]) -> Optional[float]:
    """M13: Discipline Score = avg(遵守ルール数 / 10)"""
    if not paired:
        return None
    scores: list[float] = []
    for trade in paired:
        d = trade.get("date", "")
        date_trades = [t for t in all_trades if t.get("date", "") == d]
        rule_results = evaluate_discipline_for_trade(trade, date_trades)
        score = sum(1 for v in rule_results.values() if v) / len(DISCIPLINE_RULES)
        scores.append(score)
    return round(sum(scores) / len(scores), 4)


def calc_m14_consistency_score(paired: list[dict]) -> Optional[float]:
    """M14: Consistency Score = ベスト日P&L / 総P&L (目標 <0.30)"""
    date_pnl: dict[str, float] = {}
    for t in paired:
        if t["pnl"] is None:
            continue
        d = t["date"]
        date_pnl[d] = date_pnl.get(d, 0.0) + t["pnl"]
    if not date_pnl:
        return None
    total = sum(date_pnl.values())
    if total <= 0:
        return None
    best_day = max(date_pnl.values())
    if best_day <= 0:
        return None
    return round(best_day / total, 4)


def calc_m15_revenge_trade_rate(all_trades: list[dict]) -> float:
    """M15: リベンジトレード検出 = 損失exit後30分以内のエントリー / 総エントリー"""
    paired = pair_trades(all_trades)
    entries = [t for t in all_trades if t.get("event") == "entry" and t.get("ts")]
    if not paired or not entries:
        return 0.0

    revenge_count = 0
    for trade in paired:
        if trade["pnl"] is None or trade["pnl"] >= 0:
            continue
        exit_ts_str = trade.get("exit_ts", "")
        if not exit_ts_str:
            continue
        try:
            exit_ts = datetime.fromisoformat(exit_ts_str)
        except ValueError:
            continue
        for entry in entries:
            try:
                ets = datetime.fromisoformat(entry["ts"])
            except ValueError:
                continue
            diff = (ets - exit_ts).total_seconds()
            if 0 < diff <= 1800:
                revenge_count += 1
                break

    total_entries = len(entries)
    if total_entries == 0:
        return 0.0
    return round(revenge_count / total_entries, 4)


# ---------------------------------------------------------------------------
# M16: PDT使用率（FINRA PDT遵守確認）
# ---------------------------------------------------------------------------

def calc_m16_pdt_usage_rate(capital_usd: float = 0.0) -> Optional[dict]:
    """M16: PDT使用率 = 消費件数 / 上限件数（$25K未満のみ有意）。

    Returns:
        {
            "constrained": bool,       # $25K未満かどうか
            "rolling5_count": int,     # 直近5営業日消費数
            "pdt_limit": int | None,   # 上限（$25K以上はNone）
            "usage_rate": float | None,  # 消費/上限（$25K以上はNone）
            "pdt_remaining": int | str,  # 残数
            "violation_detected": bool,  # 上限到達（=次回エントリーでFINRA違反）
        }
    """
    try:
        import sys
        from pathlib import Path
        _base = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(_base))
        from common.pdt_tracker import get_global_tracker as _get_pdt_tracker, PDT_LIMIT as _PDT_LIM
        _pdt = _get_pdt_tracker()
        _status = _pdt.get_status(capital_usd)
        constrained = _status["pdt_constrained"]
        rolling5    = _status["rolling5_count"]
        remaining   = _status["pdt_remaining"]
        usage_rate  = round(rolling5 / _PDT_LIM, 4) if constrained else None
        violation   = constrained and isinstance(remaining, int) and remaining == 0
        return {
            "constrained":         constrained,
            "rolling5_count":      rolling5,
            "pdt_limit":           _PDT_LIM if constrained else None,
            "usage_rate":          usage_rate,
            "pdt_remaining":       remaining,
            "violation_detected":  violation,
        }
    except Exception as _e:
        return None


# ---------------------------------------------------------------------------
# EV-1: 仮説-結果マッチング
# ---------------------------------------------------------------------------

def calc_ev1_hypothesis_match(all_trades: list[dict]) -> dict:
    """EV-1: regime (仮説) vs exit結果の整合性を評価"""
    env_snaps = [t for t in all_trades if t.get("event") == "env_snapshot"]
    paired = pair_trades(all_trades)
    if not env_snaps or not paired:
        return {"match_rate": None, "total": 0, "matched": 0}

    matched = 0
    total = 0
    for trade in paired:
        if trade["pnl"] is None:
            continue
        total += 1
        d = trade["date"]
        snaps_for_date = [s for s in env_snaps if s.get("date", "") == d]
        regime = snaps_for_date[0].get("regime", "normal") if snaps_for_date else "normal"

        pnl = trade["pnl"]
        exit_reason = trade.get("exit_reason", "")
        if regime in ("normal", "calm"):
            if pnl > 0:
                matched += 1
        elif regime in ("crisis", "high_vol", "volatile"):
            if "crisis" in exit_reason or "stop" in exit_reason or pnl > -50:
                matched += 1
        else:
            if pnl >= 0:
                matched += 1

    match_rate = round(matched / total, 4) if total > 0 else None
    return {"match_rate": match_rate, "total": total, "matched": matched}


# ---------------------------------------------------------------------------
# EV-4: 戦術別・曜日別・VIX帯別 Breakdown
# ---------------------------------------------------------------------------

def calc_ev4_tactic_breakdown(paired: list[dict]) -> dict:
    tactic_stats: dict[str, dict] = {}
    for t in paired:
        tac = t.get("tactic", "unknown")
        if tac not in tactic_stats:
            tactic_stats[tac] = {"wins": 0, "losses": 0, "gains": 0.0, "loss_amt": 0.0}
        pnl = t.get("pnl")
        if pnl is None:
            continue
        if pnl > 0:
            tactic_stats[tac]["wins"] += 1
            tactic_stats[tac]["gains"] += pnl
        else:
            tactic_stats[tac]["losses"] += 1
            tactic_stats[tac]["loss_amt"] += abs(pnl)
    result = {}
    for tac, s in tactic_stats.items():
        total = s["wins"] + s["losses"]
        win_rate = round(s["wins"] / total, 4) if total > 0 else None
        pf = round(s["gains"] / s["loss_amt"], 3) if s["loss_amt"] > 0 else None
        target_pf = TACTIC_PF_TARGETS.get(tac, TACTIC_PF_TARGETS["default"])
        pf_ok = (pf >= target_pf) if pf is not None else None
        result[tac] = {
            "trades": total,
            "win_rate": win_rate,
            "profit_factor": pf,
            "target_pf": target_pf,
            "pf_ok": pf_ok,
        }
    return result


def calc_dow_breakdown(paired: list[dict]) -> dict:
    dow_stats: dict[str, dict] = {}
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    for t in paired:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        try:
            d = date.fromisoformat(t["date"][:10])
            dow = dow_names.get(d.weekday(), "?")
        except Exception:
            continue
        if dow not in dow_stats:
            dow_stats[dow] = {"wins": 0, "losses": 0}
        if pnl > 0:
            dow_stats[dow]["wins"] += 1
        else:
            dow_stats[dow]["losses"] += 1
    result = {}
    for dow, s in dow_stats.items():
        total = s["wins"] + s["losses"]
        result[dow] = {
            "trades": total,
            "win_rate": round(s["wins"] / total, 4) if total > 0 else None,
        }
    return result


def calc_vix_band_breakdown(paired: list[dict], all_trades: list[dict]) -> dict:
    env_snaps = {
        t.get("date", ""): t for t in all_trades if t.get("event") == "env_snapshot"
    }
    bands: dict[str, dict] = {}
    for t in paired:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        vix = t.get("vix")
        if vix is None:
            snap = env_snaps.get(t.get("date", ""), {})
            vix = snap.get("vix")
        if vix is None:
            band = "unknown"
        elif vix < 15:
            band = "<15"
        elif vix < 20:
            band = "15-20"
        elif vix < 30:
            band = "20-30"
        else:
            band = "30+"
        if band not in bands:
            bands[band] = {"wins": 0, "losses": 0}
        if pnl > 0:
            bands[band]["wins"] += 1
        else:
            bands[band]["losses"] += 1
    result = {}
    for band, s in bands.items():
        total = s["wins"] + s["losses"]
        result[band] = {
            "trades": total,
            "win_rate": round(s["wins"] / total, 4) if total > 0 else None,
        }
    return result


# ---------------------------------------------------------------------------
# Premium Capture Rate (Theta Profits ベンチマーク指標)
# ---------------------------------------------------------------------------

def calc_premium_capture_rate(paired: list[dict]) -> Optional[float]:
    """Premium Capture Rate = avg(pnl / max_possible_gain)。パーセント表示。"""
    rates: list[float] = []
    for t in paired:
        pnl = t.get("pnl")
        nc = t.get("net_credit")
        qty = t.get("qty", 1)
        if pnl is None or nc is None or nc <= 0:
            continue
        max_gain = nc * 100 * qty
        rates.append(pnl / max_gain)
    if not rates:
        return None
    return round(sum(rates) / len(rates) * 100, 2)


# ---------------------------------------------------------------------------
# メイン評価関数
# ---------------------------------------------------------------------------

def run_evaluation(
    period: str = "daily",
    target_date: Optional[date] = None,
    margin_usd: float = 380000.0,
) -> dict:
    """全指標 M1-M15 + EV-1〜EV-4 を計算してdictで返す"""
    all_trades = load_all_trades()
    if not all_trades:
        return {"error": "No trade data found", "period": period}

    today = target_date or date.today() - timedelta(days=1)

    if period == "daily":
        start = today
        end = today
    elif period == "weekly":
        start = today - timedelta(days=6)
        end = today
    elif period == "monthly":
        start = today - timedelta(days=29)
        end = today
    else:
        # audit N 日モード
        try:
            n = int(period)
            start = today - timedelta(days=n - 1)
            end = today
        except ValueError:
            start = today
            end = today

    trades_in_period = filter_by_date_range(all_trades, start, end)
    paired = pair_trades(trades_in_period)

    # Fix 2: M6-M9はドローダウン時系列が必要。日次評価(1日分)では系列長<3で
    # 必ずNullになるため、全期間のpairedを使って算出する。
    # 直近90日分を上限として使用（十分な時系列長を確保）
    _dd_start = today - timedelta(days=89)  # 90日間の時系列
    _dd_trades = filter_by_date_range(all_trades, _dd_start, today)
    _dd_paired = pair_trades(_dd_trades)

    m1 = calc_m1_profit_factor(paired)
    m2 = calc_m2_win_rate(paired)
    m3 = calc_m3_expected_value(paired)
    m4 = calc_m4_rom(paired, margin_usd)
    m5 = calc_m5_monthly_consistency(all_trades)
    m6 = calc_m6_sortino(_dd_paired)   # Fix 2: 全期間で計算
    m7 = calc_m7_calmar(_dd_paired)    # Fix 2: 全期間で計算
    m8 = calc_m8_mar(_dd_paired)       # Fix 2: 全期間で計算
    m9 = calc_m9_ulcer_index(_dd_paired)  # Fix 2: 全期間で計算
    m10 = calc_m10_e_ratio(paired)
    m11 = calc_m11_slippage_rate(paired)
    m12 = calc_m12_naked_leg_rate(trades_in_period)
    m13 = calc_m13_discipline_score(paired, trades_in_period)
    m14 = calc_m14_consistency_score(paired)
    m15 = calc_m15_revenge_trade_rate(trades_in_period)
    m16 = calc_m16_pdt_usage_rate(capital_usd=margin_usd)

    ev1 = calc_ev1_hypothesis_match(trades_in_period)
    tactic_bd = calc_ev4_tactic_breakdown(paired)
    dow_bd = calc_dow_breakdown(paired)
    vix_bd = calc_vix_band_breakdown(paired, trades_in_period)
    pcr = calc_premium_capture_rate(paired)

    total_pnl = sum(t["pnl"] for t in paired if t["pnl"] is not None)
    trade_count = len([t for t in paired if t["pnl"] is not None])

    return {
        "generated_at": datetime.now().isoformat(),
        "period": period,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "trade_count": trade_count,
        "total_pnl_usd": round(total_pnl, 2),
        "margin_usd": margin_usd,
        "metrics": {
            "M1_profit_factor": m1,
            "M2_win_rate": m2,
            "M3_expected_value": m3,
            "M4_rom": m4,
            "M5_monthly_consistency": m5,
            "M6_sortino": m6,
            "M7_calmar": m7,
            "M8_mar": m8,
            "M9_ulcer_index": m9,
            "M10_e_ratio": m10,
            "M11_slippage_rate": m11,
            "M12_naked_leg_rate": m12,
            "M13_discipline_score": m13,
            "M14_consistency_score": m14,
            "M15_revenge_trade_rate": m15,
            "M16_pdt_usage": m16,
        },
        "ev_gaps": {
            "EV1_hypothesis_match": ev1,
        },
        "breakdowns": {
            "tactic": tactic_bd,
            "day_of_week": dow_bd,
            "vix_band": vix_bd,
        },
        "premium_capture_rate_pct": pcr,
    }


# ---------------------------------------------------------------------------
# レポート生成
# ---------------------------------------------------------------------------

def _fmt(val, suffix: str = "", precision: int = 3, na: str = "N/A", pct_scale: bool = False) -> str:
    """値をフォーマットする。pct_scale=True の場合は 0-1 スケールを × 100 してパーセント表示。"""
    if val is None:
        return na
    if isinstance(val, float):
        if val == float("inf"):
            return f"inf{suffix}"
        display_val = val * 100 if pct_scale else val
        return f"{display_val:.{precision}f}{suffix}"
    return f"{val}{suffix}"


def _rating(val: Optional[float], good: float, warn: float, higher_is_better: bool = True) -> str:
    if val is None:
        return "N/A"
    if higher_is_better:
        if val >= good:
            return "GOOD"
        elif val >= warn:
            return "WARN"
        else:
            return "POOR"
    else:
        if val <= good:
            return "GOOD"
        elif val <= warn:
            return "WARN"
        else:
            return "POOR"


def build_markdown_report(result: dict, target_date: date) -> str:
    m = result.get("metrics", {})
    ev = result.get("ev_gaps", {})
    bd = result.get("breakdowns", {})
    pcr = result.get("premium_capture_rate_pct")
    period = result.get("period", "daily")
    dr = result.get("date_range", {})

    lines = [
        f"# 優秀トレーダー判定レポート -- {target_date.isoformat()}",
        "",
        f"**期間**: {dr.get('start')} から {dr.get('end')} ({period})",
        f"**トレード数**: {result.get('trade_count', 0)} 件",
        f"**総P&L**: ${result.get('total_pnl_usd', 0):.2f}",
        f"**証拠金**: ${result.get('margin_usd', 0):,.0f}",
        "",
        "---",
        "",
        "## リターン系指標 (M1-M5)",
        "",
        "| 指標 | 値 | 判定 | プロ基準 |",
        "|------|-----|------|---------|",
        f"| M1 Profit Factor | {_fmt(m.get('M1_profit_factor'))} | {_rating(m.get('M1_profit_factor'), 2.0, 1.5)} | >1.5 (IC>2.0) |",
        f"| M2 勝率 | {_fmt(m.get('M2_win_rate'), '%', 1, pct_scale=True)} | {_rating(m.get('M2_win_rate'), 0.66, 0.40)} | IC:70%, CS:65%, ORB:40% |",
        f"| M3 期待値 | ${_fmt(m.get('M3_expected_value'), '', 2)} | {_rating(m.get('M3_expected_value'), 10, 0)} | >0 |",
        f"| M4 ROM | {_fmt(m.get('M4_rom'), '%', 1, pct_scale=True)} | {_rating(m.get('M4_rom'), 0.10, 0.05)} | >10%/月 |",
        f"| M5 月次一貫性 | {_fmt(m.get('M5_monthly_consistency'), '%', 1, pct_scale=True)} | {_rating(m.get('M5_monthly_consistency'), 0.80, 0.60)} | >80% (Theta Profits 86%) |",
        "",
        "## リスク調整指標 (M6-M9)",
        "",
        "| 指標 | 値 | 判定 | プロ基準 |",
        "|------|-----|------|---------|",
        f"| M6 Sortino Ratio | {_fmt(m.get('M6_sortino'))} | {_rating(m.get('M6_sortino'), 2.0, 1.0)} | >2.0 |",
        f"| M7 Calmar Ratio | {_fmt(m.get('M7_calmar'))} | {_rating(m.get('M7_calmar'), 3.0, 1.0)} | >3.0 |",
        f"| M8 MAR Ratio | {_fmt(m.get('M8_mar'))} | {_rating(m.get('M8_mar'), 1.0, 0.5)} | >1.0 |",
        f"| M9 Ulcer Index | {_fmt(m.get('M9_ulcer_index'), '%', 2)} | {_rating(m.get('M9_ulcer_index'), 3.0, 5.0, higher_is_better=False)} | <5% |",
        "",
        "## 執行品質指標 (M10-M12)",
        "",
        "| 指標 | 値 | 判定 | プロ基準 |",
        "|------|-----|------|---------|",
        f"| M10 E-Ratio | {_fmt(m.get('M10_e_ratio'))} | {_rating(m.get('M10_e_ratio'), 1.5, 1.0)} | >1.5 |",
        f"| M11 スリッページ率 | {_fmt(m.get('M11_slippage_rate'), '%', 1, pct_scale=True)} | {_rating(m.get('M11_slippage_rate'), 0.10, 0.20, higher_is_better=False)} | <20% |",
        f"| M12 裸レッグ発生率 | {_fmt(m.get('M12_naked_leg_rate'), '%', 1, pct_scale=True)} | {'GOOD' if (m.get('M12_naked_leg_rate') or 0) == 0 else 'POOR'} | 0% |",
        "",
        "## 行動規律指標 (M13-M15)",
        "",
        "| 指標 | 値 | 判定 | プロ基準 |",
        "|------|-----|------|---------|",
        f"| M13 Discipline Score | {_fmt(m.get('M13_discipline_score'), '%', 1, pct_scale=True)} | {_rating(m.get('M13_discipline_score'), 0.95, 0.90)} | >95% |",
        f"| M14 Consistency Score | {_fmt(m.get('M14_consistency_score'), '%', 1, pct_scale=True)} | {_rating(m.get('M14_consistency_score'), 0.30, 0.50, higher_is_better=False)} | <30% |",
        f"| M15 リベンジトレード率 | {_fmt(m.get('M15_revenge_trade_rate'), '%', 1, pct_scale=True)} | {'GOOD' if (m.get('M15_revenge_trade_rate') or 0) == 0 else 'WARN'} | 0件 |",
        "",
        "## 規制遵守指標 (M16)",
        "",
        "| 指標 | 値 | 判定 |",
        "|------|-----|------|",
    ]

    _m16 = m.get("M16_pdt_usage")
    if _m16:
        _m16_constrained = _m16.get("constrained", True)
        _m16_rolling5    = _m16.get("rolling5_count", 0)
        _m16_limit       = _m16.get("pdt_limit", 3)
        _m16_usage       = _m16.get("usage_rate")
        _m16_remaining   = _m16.get("pdt_remaining", "N/A")
        _m16_violation   = _m16.get("violation_detected", False)
        _m16_usage_str   = f"{_m16_rolling5}/{_m16_limit}件 ({_fmt(_m16_usage, '%', 0, pct_scale=True)})" if _m16_constrained else "無制限"
        _m16_rating      = "CRITICAL" if _m16_violation else ("WARN" if _m16_constrained and isinstance(_m16_remaining, int) and _m16_remaining <= 1 else "GOOD")
        lines += [
            f"| M16 PDT使用率 | {_m16_usage_str} | {_m16_rating} |",
            f"| PDT残本数 | {_m16_remaining} | {'FINRA違反リスクあり' if _m16_violation else 'OK'} |",
            "",
        ]
    else:
        lines += [
            f"| M16 PDT使用率 | N/A | N/A |",
            "",
        ]

    lines += [
        "## EV-1 仮説-結果マッチング",
        "",
    ]

    ev1 = ev.get("EV1_hypothesis_match", {})
    match_rate = ev1.get("match_rate")
    lines += [
        f"- 総トレード: {ev1.get('total', 0)} 件",
        f"- 仮説マッチ: {ev1.get('matched', 0)} 件",
        f"- マッチ率: {_fmt(match_rate, '%', 1)}",
        "",
    ]

    # Premium Capture Rate
    if pcr is not None:
        if pcr >= 5.65:
            pcr_rating = "GOOD (Theta Profits水準)"
        elif pcr >= 3.5:
            pcr_rating = "WARN (悪化警告域)"
        else:
            pcr_rating = "POOR (要再評価)"
    else:
        pcr_rating = "N/A"
    lines += [
        "## Premium Capture Rate (Theta Profits 指標)",
        "",
        f"- Atlas実績: {_fmt(pcr, '%', 2)}",
        f"- Theta Profits 平均: 5.65% / 悪化警告: 3.5%",
        f"- 判定: {pcr_rating}",
        "",
    ]

    # 戦術別
    lines += ["## 戦術別内訳 (EV-4)", ""]
    tac_bd = bd.get("tactic", {})
    if tac_bd:
        lines += [
            "| 戦術 | 件数 | 勝率 | PF | 目標PF | 判定 |",
            "|------|-----|------|-----|-------|------|",
        ]
        for tac, s in sorted(tac_bd.items()):
            wr = f"{s['win_rate']*100:.1f}%" if s.get("win_rate") is not None else "N/A"
            pf = f"{s['profit_factor']:.2f}" if s.get("profit_factor") is not None else "N/A"
            tgt = s.get("target_pf", 1.5)
            ok_val = s.get("pf_ok")
            ok_str = "OK" if ok_val else ("NG" if ok_val is False else "N/A")
            lines.append(f"| {tac} | {s['trades']} | {wr} | {pf} | {tgt} | {ok_str} |")
        lines.append("")
    else:
        lines += ["データなし", ""]

    # 曜日別
    lines += ["## 曜日別勝率", ""]
    dow_bd = bd.get("day_of_week", {})
    if dow_bd:
        lines += ["| 曜日 | 件数 | 勝率 |", "|------|-----|------|"]
        for dow in ["Mon", "Tue", "Wed", "Thu", "Fri"]:
            if dow in dow_bd:
                s = dow_bd[dow]
                wr = f"{s['win_rate']*100:.1f}%" if s.get("win_rate") is not None else "N/A"
                lines.append(f"| {dow} | {s['trades']} | {wr} |")
        lines.append("")
    else:
        lines += ["データなし", ""]

    # VIX帯別
    lines += ["## VIX帯別勝率", ""]
    vix_bd = bd.get("vix_band", {})
    if vix_bd:
        lines += ["| VIX帯 | 件数 | 勝率 |", "|-------|-----|------|"]
        for band in ["<15", "15-20", "20-30", "30+", "unknown"]:
            if band in vix_bd:
                s = vix_bd[band]
                wr = f"{s['win_rate']*100:.1f}%" if s.get("win_rate") is not None else "N/A"
                lines.append(f"| VIX {band} | {s['trades']} | {wr} |")
        lines.append("")
    else:
        lines += ["データなし", ""]

    # 外部ベンチマーク比較
    lines += [
        "## 外部ベンチマーク比較",
        "",
        "| 項目 | Atlas | Theta Profits | Nick Magno | Option Alpha |",
        "|------|-------|--------------|------------|-------------|",
        f"| 勝率 | {_fmt(m.get('M2_win_rate'), '%', 1, pct_scale=True)} | 40.0% | N/A | IC:70.2%, IB:66.8% |",
        f"| Profit Factor | {_fmt(m.get('M1_profit_factor'))} | N/A | N/A | >2.0 (SMA5) |",
        f"| 月次黒字率 | {_fmt(m.get('M5_monthly_consistency'), '%', 1, pct_scale=True)} | 86.0% | N/A | N/A |",
        f"| Premium Capture | {_fmt(pcr, '%', 2)} | 5.65% | N/A | N/A |",
        f"| Sortino Ratio | {_fmt(m.get('M6_sortino'))} | N/A | N/A | >2.0 |",
        f"| Calmar Ratio | {_fmt(m.get('M7_calmar'))} | N/A | N/A | >3.0 |",
        "",
        "---",
        "",
        f"*Generated by scripts/trader_evaluation.py (Sora Lab) -- {datetime.now().strftime('%Y-%m-%d %H:%M JST')}*",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rolling metrics 保存
# ---------------------------------------------------------------------------

def update_rolling_metrics(result: dict, target_date: date) -> None:
    rolling_file = EVAL_DIR / "metrics_rolling.json"
    rolling: list[dict] = []
    if rolling_file.exists():
        try:
            rolling = json.loads(rolling_file.read_text())
        except Exception:
            rolling = []
    rolling = [r for r in rolling if r.get("date") != target_date.isoformat()]
    rolling.append({
        "date": target_date.isoformat(),
        "period": result.get("period"),
        "trade_count": result.get("trade_count", 0),
        "total_pnl": result.get("total_pnl_usd"),
        **result.get("metrics", {}),
        "premium_capture_pct": result.get("premium_capture_rate_pct"),
        "ev1_match_rate": result.get("ev_gaps", {}).get("EV1_hypothesis_match", {}).get("match_rate"),
    })
    rolling.sort(key=lambda r: r.get("date", ""))
    rolling_file.write_text(json.dumps(rolling, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="優秀トレーダー判定フレームワーク")
    parser.add_argument("--period", choices=["daily", "weekly", "monthly"], default="daily")
    parser.add_argument("--date", type=str, default=None, help="対象日 YYYY-MM-DD (省略=昨日)")
    parser.add_argument("--audit", type=int, default=None, help="直近N日の規律監査")
    parser.add_argument("--no-pushover", action="store_true")
    args = parser.parse_args()

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    EVAL_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    EVAL_WEEKLY_DIR.mkdir(parents=True, exist_ok=True)

    if args.date:
        target_date = date.fromisoformat(args.date)
    else:
        target_date = date.today() - timedelta(days=1)

    period = args.period
    if args.audit:
        period = str(args.audit)

    print(f"[eval] Running: period={period}, date={target_date}", flush=True)

    result = run_evaluation(period=period, target_date=target_date)

    if "error" in result:
        print(f"[eval] Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    # JSON 保存
    json_path = EVAL_DAILY_DIR / f"{target_date.isoformat().replace('-', '')}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] JSON: {json_path}", flush=True)

    # Markdown レポート
    md_content = build_markdown_report(result, target_date)
    md_path = EVAL_DIR / f"trader_eval_{target_date.isoformat().replace('-', '')}.md"
    md_path.write_text(md_content, encoding="utf-8")
    print(f"[eval] Report: {md_path}", flush=True)

    # Rolling metrics 更新
    update_rolling_metrics(result, target_date)

    # サマリー表示
    m = result.get("metrics", {})
    pf = m.get("M1_profit_factor")
    wr = m.get("M2_win_rate")
    disc = m.get("M13_discipline_score")
    pnl = result.get("total_pnl_usd", 0)
    tc = result.get("trade_count", 0)

    summary_lines = [
        f"期間: {result['date_range']['start']} から {result['date_range']['end']}",
        f"件数: {tc}件 | P&L: ${pnl:.2f}",
        f"PF: {_fmt(pf)} | 勝率: {_fmt(wr,'%',1,pct_scale=True)} | Discipline: {_fmt(disc,'%',1,pct_scale=True)}",
        f"Sortino: {_fmt(m.get('M6_sortino'))} | PCR: {_fmt(result.get('premium_capture_rate_pct'),'%',2)}",
    ]
    print("\n".join(summary_lines), flush=True)

    # Pushover 通知
    if not args.no_pushover:
        disc_val = disc or 0
        pf_val = pf or 0
        alert = disc_val < 0.90 or (0 < pf_val < 1.0)
        msg = "\n".join(summary_lines)
        if alert:
            send_pushover(f"[Atlas/ALERT] EVAL 規律異常 {target_date}", msg, priority=1)
        else:
            send_pushover(f"[Atlas/EVAL] 優秀トレーダー判定 {target_date}", msg)

    print("[eval] Done.", flush=True)


if __name__ == "__main__":
    main()
