#!/usr/bin/env python3
"""
scripts/opportunity_cost_realtime.py — 資金効率 / 機会損失 リアルタイム定量化

【計算公式】
  Loss(t_min) = capital_jpy × monthly_rate × (t_min / 43200)
  + target_lag_loss(down_time_days)

  target_lag_loss = 目標到達日後退による複利損失累積
    = capital × ((1+r)^lag_months - 1)  where lag_months = down_days / 30

【基準値 (Atlas v6 楽観)】
  monthly_rate   : 11.86% (税前) → 8.89% (税後)
  capital_jpy    : 1,200,000 円 (初期元本)
  USD/JPY        : 150 円 (固定 — 円換算補正用)
  goal_date      : 2027-04-01
  start_date     : 2026-04-17 (ペーパー初稼働)

【alert 閾値】
  market_open 中 (ET 09:30-16:00, JST 22:30-05:00) の down_time:
    >= 60 分  → Pushover P2  (累積逸失¥)
    >= 1 日   → Pushover P2  + 目標後退日数
  累積逸失 >= 100,000 円 → P2 + HARD STOP 指示 (is_market_opportunity_loss=True)

【出力】
  data/opportunity_cost_live.jsonl  — 1時間毎追記
  data/ops/opportunity_cost_dashboard.md — 最新状態を上書き
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

# ── パス設定 ──────────────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE))

from common.pushover_client import send_critical, classify_level, LEVEL_CRITICAL

# ── 定数 ──────────────────────────────────────────────────────────────────────
MONTHLY_RATE_PRE_TAX   = 0.1186   # Atlas v6 楽観 (11.86%)
MONTHLY_RATE_POST_TAX  = 0.0889   # Atlas v6 楽観 税後 (8.89%)
CAPITAL_JPY            = 1_200_000
MINUTES_PER_MONTH      = 43_200   # 30日 × 24h × 60min
GOAL_DATE              = datetime.date(2027, 4, 1)
PAPER_START_DATE       = datetime.date(2026, 4, 17)

# alert 閾値
ALERT_60MIN_JPY        = CAPITAL_JPY * MONTHLY_RATE_POST_TAX / MINUTES_PER_MONTH * 60
ALERT_1DAY_JPY         = CAPITAL_JPY * MONTHLY_RATE_POST_TAX / MINUTES_PER_MONTH * 1440
HARD_STOP_THRESHOLD_JPY = 100_000

# データパス
LIVE_LOG_PATH      = _BASE / "data" / "opportunity_cost_live.jsonl"
DASHBOARD_DIR      = _BASE / "data" / "ops"
DASHBOARD_PATH     = DASHBOARD_DIR / "opportunity_cost_dashboard.md"
DOWN_STATE_PATH    = _BASE / "data" / "bot_down_state.json"


# ── 市場時間判定 (ET 09:30-16:00 = JST 22:30-05:00) ─────────────────────────

def is_market_open(dt_utc: datetime.datetime | None = None) -> bool:
    """現在が NY 市場オープン時間帯かを判定する (UTC ベース)。"""
    if dt_utc is None:
        dt_utc = datetime.datetime.now(datetime.timezone.utc)
    # 夏時間: UTC-4 / 冬時間: UTC-5。ここでは年間通じて ±4h で近似 (誤差最大1h)
    dt_et = dt_utc - datetime.timedelta(hours=4)
    weekday = dt_et.weekday()  # 0=月, 4=金
    if weekday >= 5:
        return False
    t = dt_et.time()
    return datetime.time(9, 30) <= t <= datetime.time(16, 0)


# ── 機会損失計算 ──────────────────────────────────────────────────────────────

def calc_opportunity_cost(
    down_minutes: float,
    capital_jpy: float = CAPITAL_JPY,
    monthly_rate: float = MONTHLY_RATE_POST_TAX,
) -> float:
    """down_time_minutes の機会損失を円で返す。"""
    return capital_jpy * monthly_rate * (down_minutes / MINUTES_PER_MONTH)


def calc_target_lag(
    down_days: float,
    capital_jpy: float = CAPITAL_JPY,
    monthly_rate: float = MONTHLY_RATE_POST_TAX,
) -> tuple[float, float]:
    """
    down_time_days の停止による目標到達遅延額を計算する。

    Returns:
      (lag_days: float, lag_loss_jpy: float)
    """
    lag_months = down_days / 30.0
    lag_loss = capital_jpy * ((1 + monthly_rate) ** lag_months - 1)
    return down_days, lag_loss


def months_to_goal_from(start_capital: float, monthly_rate: float, target_capital: float) -> float:
    """複利で start_capital から target_capital に到達する月数。"""
    import math
    if monthly_rate <= 0 or target_capital <= start_capital:
        return 0.0
    return math.log(target_capital / start_capital) / math.log(1 + monthly_rate)


# ── Bot 停止状態管理 ──────────────────────────────────────────────────────────

def load_down_state() -> dict:
    """data/bot_down_state.json を読み込む。存在しなければ稼働中とみなす。"""
    try:
        if DOWN_STATE_PATH.exists():
            return json.loads(DOWN_STATE_PATH.read_text())
    except Exception:
        pass
    return {
        "is_down": False,
        "down_since_utc": None,
        "cumulative_down_minutes": 0.0,
        "cumulative_loss_jpy": 0.0,
        "alert_60min_sent": False,
        "alert_1day_sent": False,
        "hard_stop_sent": False,
    }


def save_down_state(state: dict) -> None:
    DOWN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOWN_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ── ダッシュボード生成 ────────────────────────────────────────────────────────

def generate_dashboard(state: dict, now_utc: datetime.datetime) -> str:
    today = now_utc.date()
    days_to_goal = (GOAL_DATE - today).days
    capital = CAPITAL_JPY

    # 4/17 以降の試算: ペーパー4日分のダウンタイムは 0 と仮定 (実データなし)
    cum_loss = state.get("cumulative_loss_jpy", 0.0)
    cum_down_min = state.get("cumulative_down_minutes", 0.0)
    is_down = state.get("is_down", False)

    # 1分停止コスト
    cost_per_min  = calc_opportunity_cost(1)
    cost_per_hour = calc_opportunity_cost(60)
    cost_per_day  = calc_opportunity_cost(1440)

    # 目標到達見込み (楽観)
    # 月次追加入金 20万円含む複利計算の近似 (簡易版: 追加入金なし)
    months_opt = months_to_goal_from(capital, MONTHLY_RATE_POST_TAX, 3_000_000)
    goal_est = today + datetime.timedelta(days=int(months_opt * 30))

    lag_days_total = cum_down_min / 1440.0
    _, lag_loss = calc_target_lag(lag_days_total)

    lines = [
        "# Bot 機会損失ダッシュボード",
        f"\n更新: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC (JST {(now_utc + datetime.timedelta(hours=9)).strftime('%H:%M')})",
        "",
        "## 現在状態",
        f"- Bot 稼働中: {'**停止中**' if is_down else '稼働中'}",
        f"- 累積停止時間: {cum_down_min:.1f} 分 ({cum_down_min/60:.1f} 時間)",
        f"- 累積逸失額 (税後): **{cum_loss:,.0f} 円**",
        f"- 目標後退複利損失 (試算): {lag_loss:,.0f} 円",
        "",
        "## 停止コスト早見表",
        f"| 停止時間 | 逸失額 (税後・楽観) |",
        f"|---|---|",
        f"| 1 分 | {cost_per_min:,.0f} 円 |",
        f"| 1 時間 | {cost_per_hour:,.0f} 円 |",
        f"| 1 日 | {cost_per_day:,.0f} 円 |",
        f"| 1 ヶ月 | {CAPITAL_JPY * MONTHLY_RATE_POST_TAX:,.0f} 円 |",
        "",
        "## 目標到達見込み",
        f"- 目標: 月300万円 (2027-04-01)",
        f"- 現在資本: {capital:,} 円",
        f"- 月利 (税後・楽観): {MONTHLY_RATE_POST_TAX*100:.2f}%",
        f"- 資本到達目安: {goal_est.strftime('%Y-%m')} (追加入金なし・楽観ケース)",
        f"- 目標まで残り: {days_to_goal} 日",
        "",
        "## Alert 閾値",
        f"- 市場時間中 60 分停止 → P2 Alert (逸失 {ALERT_60MIN_JPY:,.0f} 円超)",
        f"- 1 日停止 → P2 Alert + 目標後退通知",
        f"- 累積逸失 100,000 円超過 → **HARD STOP 指示** (P2 is_market_opportunity_loss=True)",
        "",
        "## 根拠",
        "- Atlas v6 楽観月利 11.86% (税前) / 8.89% (税後)",
        "- 元本 1,200,000 円 (初期元本)",
        "- 複利計算基準: 30日 = 43,200 分",
        "- 4/17 以降試算: ペーパー稼働期間はダウンタイム 0 と仮定",
    ]
    return "\n".join(lines) + "\n"


# ── メイン ────────────────────────────────────────────────────────────────────

def run_check(bot_is_down: bool = False) -> dict:
    """
    1時間毎に呼ばれる定期チェック。
    bot_is_down=True のとき停止中として損失計算・alert 送信を行う。
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    state = load_down_state()
    market_open = is_market_open(now_utc)

    # 停止開始記録
    if bot_is_down and not state["is_down"]:
        state["is_down"] = True
        state["down_since_utc"] = now_utc.isoformat()

    # 停止解除
    if not bot_is_down and state["is_down"]:
        state["is_down"] = False
        state["down_since_utc"] = None
        state["alert_60min_sent"] = False
        state["alert_1day_sent"] = False

    # 停止中 → 損失加算 (市場時間中のみ)
    current_session_minutes = 0.0
    if state["is_down"] and state.get("down_since_utc") and market_open:
        down_since = datetime.datetime.fromisoformat(state["down_since_utc"])
        current_session_minutes = (now_utc - down_since).total_seconds() / 60.0
        session_loss = calc_opportunity_cost(current_session_minutes)
        state["cumulative_down_minutes"] = state.get("cumulative_down_minutes", 0.0) + current_session_minutes
        state["cumulative_loss_jpy"] = state.get("cumulative_loss_jpy", 0.0) + session_loss

    cum_loss = state.get("cumulative_loss_jpy", 0.0)

    # Alert: 60 分停止 (市場時間中)
    if (bot_is_down and market_open
            and current_session_minutes >= 60
            and not state.get("alert_60min_sent")):
        msg = (
            f"Bot 停止 {current_session_minutes:.0f} 分\n"
            f"逸失額 (税後・楽観): {calc_opportunity_cost(current_session_minutes):,.0f} 円\n"
            f"累積逸失: {cum_loss:,.0f} 円\n"
            f"市場時間中の停止は機会損失を直接積み増す。即対応してください。"
        )
        send_critical("[ALERT] Bot 停止 60 分超過", msg, priority=2, app_tag="Atlas")
        state["alert_60min_sent"] = True

    # Alert: 1 日停止
    if (bot_is_down
            and current_session_minutes >= 1440
            and not state.get("alert_1day_sent")):
        _, lag_loss = calc_target_lag(1.0)
        msg = (
            f"Bot 停止 24 時間超過\n"
            f"1 日停止 = 逸失 {calc_opportunity_cost(1440):,.0f} 円 (市場時間相当)\n"
            f"目標後退複利損失 (試算): {lag_loss:,.0f} 円\n"
            f"累積逸失: {cum_loss:,.0f} 円"
        )
        send_critical("[ALERT] Bot 1 日停止", msg, priority=2, app_tag="Atlas")
        state["alert_1day_sent"] = True

    # Alert: 累積逸失 100,000 円 → HARD STOP
    if cum_loss >= HARD_STOP_THRESHOLD_JPY and not state.get("hard_stop_sent"):
        msg = (
            f"累積逸失が {cum_loss:,.0f} 円に達しました。\n"
            f"閾値 100,000 円を超過。\n"
            f"ゆうさくさんの判断が必要です。Bot を強制停止してください。\n"
            f"cumulative_down_minutes: {state.get('cumulative_down_minutes', 0):.1f} 分"
        )
        send_critical(
            "[HARD STOP] 累積逸失 10 万円超過",
            msg,
            priority=2,
            app_tag="Atlas",
        )
        state["hard_stop_sent"] = True

    save_down_state(state)

    # JSONL 記録
    record = {
        "ts_utc": now_utc.isoformat(),
        "is_down": bot_is_down,
        "market_open": market_open,
        "current_session_down_minutes": round(current_session_minutes, 2),
        "cumulative_down_minutes": round(state.get("cumulative_down_minutes", 0.0), 2),
        "opportunity_cost_session_jpy": round(calc_opportunity_cost(current_session_minutes), 0),
        "cumulative_loss_jpy": round(cum_loss, 0),
        "hard_stop_triggered": state.get("hard_stop_sent", False),
    }
    LIVE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LIVE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ダッシュボード更新
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_PATH.write_text(generate_dashboard(state, now_utc), encoding="utf-8")

    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="機会損失リアルタイム計算")
    parser.add_argument("--down", action="store_true", help="Bot が停止中として記録")
    parser.add_argument("--up", action="store_true", help="Bot が稼働中として記録 (デフォルト)")
    parser.add_argument("--status", action="store_true", help="現在の状態を表示")
    parser.add_argument("--reset", action="store_true", help="累積データをリセット")
    args = parser.parse_args()

    if args.reset:
        if DOWN_STATE_PATH.exists():
            DOWN_STATE_PATH.unlink()
        if LIVE_LOG_PATH.exists():
            LIVE_LOG_PATH.unlink()
        print("リセット完了")
        return

    if args.status:
        state = load_down_state()
        cum_loss = state.get("cumulative_loss_jpy", 0.0)
        cum_min = state.get("cumulative_down_minutes", 0.0)
        print(f"Bot 停止中: {state.get('is_down', False)}")
        print(f"累積停止時間: {cum_min:.1f} 分 ({cum_min/60:.1f} 時間)")
        print(f"累積逸失額 (税後・楽観): {cum_loss:,.0f} 円")
        print(f"1 分停止コスト: {calc_opportunity_cost(1):,.1f} 円")
        print(f"1 時間停止コスト: {calc_opportunity_cost(60):,.0f} 円")
        print(f"1 日停止コスト: {calc_opportunity_cost(1440):,.0f} 円")
        return

    bot_is_down = args.down and not args.up
    record = run_check(bot_is_down=bot_is_down)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
