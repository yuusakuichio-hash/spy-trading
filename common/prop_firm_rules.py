"""
common/prop_firm_rules.py — プロップファーム契約リスク 4 層防護の中核モジュール

設計書: data/prop_firm_countermeasures_design.md
調査:   data/prop_firm_risk_analysis_20260420.md
実装指示: data/builder_instructions/chronos_prop_safety_20260420.md

Layer PF-1 Pre-Trade チェック関数群を提供する。
各関数は (allow: bool, reason: str) を返す。
統合エントリポイント: check_prop_firm_compliance()

後方互換: 既存 chronos_mffu_rules.py は壊さない。
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).parent / "prop_firm_rules.yaml"
_rules_cache: Optional[dict] = None


# ── YAML ロード ──────────────────────────────────────────────────────────────

def load_rules() -> dict:
    """YAML をキャッシュ付きでロードする。テスト時は reload_rules() でリセット可能。"""
    global _rules_cache
    if _rules_cache is None:
        with open(_RULES_PATH, encoding="utf-8") as f:
            _rules_cache = yaml.safe_load(f)
    return _rules_cache


def reload_rules() -> dict:
    """キャッシュを無効化して再ロードする（テスト用）。"""
    global _rules_cache
    _rules_cache = None
    return load_rules()


def get_plan_rules(firm: str, plan: str) -> dict:
    """firm / plan のルール dict を返す。不明なら KeyError。

    Args:
        firm: "mffu" | "tradeify" | "apex"
        plan: "core_50k" | "rapid_50k" | "lightning_150k" | 等

    Raises:
        KeyError: firm または plan が YAML に存在しない場合
    """
    rules = load_rules()
    return rules["firms"][firm][plan]


def get_common_prohibited() -> dict:
    """共通禁止事項 (common_prohibited) を返す。"""
    return load_rules()["common_prohibited"]


def is_rapid_enabled() -> bool:
    """meta.rapid_enabled フラグを返す。Phase A 完了前は False。"""
    return bool(load_rules().get("meta", {}).get("rapid_enabled", False))


# ── Layer PF-1 個別チェック関数 ──────────────────────────────────────────────

def check_mll_breach(
    balance: float,
    peak_balance: float,
    mll: float,
    drawdown_type: str,
) -> tuple[bool, str]:
    """MLL（最大損失上限）ブリーチチェック。

    drawdown_type に応じて Intraday / EOD Trailing / EOD Static を判定する。

    予兆 80% 到達時点でも False を返し発注を止める（口座死亡の未然防止）。

    Args:
        balance:       現在口座残高（Intraday の場合は含み損益込みの balance）
        peak_balance:  ピーク残高（Intraday の場合は intraday ピーク）
        mll:           最大損失上限ドル値
        drawdown_type: "intraday_trailing_4pct" | "eod_trailing_3pct" | "eod_trailing" | "eod_static"

    Returns:
        (allow, reason)
    """
    if drawdown_type.startswith("intraday"):
        dd = peak_balance - balance
        if dd >= mll:
            return False, (
                f"Intraday MLL超過: peak={peak_balance:.0f}, current={balance:.0f}, "
                f"DD={dd:.0f} >= MLL={mll:.0f}"
            )
        if dd >= mll * 0.80:
            return False, (
                f"Intraday MLL予兆80%到達: DD={dd:.0f} >= MLL×0.80={mll*0.80:.0f}"
            )
    elif drawdown_type.startswith("eod_trailing"):
        floor = peak_balance - mll
        if balance < floor:
            return False, (
                f"EOD Trailing MLL超過: balance={balance:.0f} < floor={floor:.0f} "
                f"(peak={peak_balance:.0f} - MLL={mll:.0f})"
            )
        if balance < floor + mll * 0.20:
            return False, (
                f"EOD Trailing MLL予兆80%: balance={balance:.0f}, floor={floor:.0f}, "
                f"バッファ残={balance - floor:.0f}"
            )
    elif drawdown_type == "eod_static":
        # Flex: peak_balance は account_size（固定 floor）
        floor = peak_balance - mll
        if balance < floor:
            return False, (
                f"EOD Static MLL超過: balance={balance:.0f} < floor={floor:.0f}"
            )
        if balance < floor + mll * 0.20:
            return False, (
                f"EOD Static MLL予兆80%: balance={balance:.0f}, floor={floor:.0f}"
            )
    return True, ""


def check_daily_loss_limit(
    daily_pnl: float,
    limit: Optional[float],
) -> tuple[bool, str]:
    """Daily Loss Limit 到達チェック。

    limit が None または 0 の場合は常に allow=True（DLL なしプランに対応）。
    Builder $1,000 soft pause / Tradeify $1,250 など firm 固有値を YAML から取得して渡す。

    Args:
        daily_pnl: 当日 PnL（損失時は負値）
        limit:     DLL 上限額（正値で渡す: 1000, 1250 等）。None なら無制限。
    """
    if limit is None or limit == 0:
        return True, ""
    if daily_pnl <= -abs(limit):
        return False, f"DLL到達: 日次PnL={daily_pnl:.0f} <= -{abs(limit):.0f}"
    # 予兆 80%
    if daily_pnl <= -abs(limit) * 0.80:
        return False, f"DLL予兆80%: 日次PnL={daily_pnl:.0f} <= -{abs(limit)*0.80:.0f}"
    return True, ""


def check_consistency(
    cycle_daily_pnl: list[float],
    max_pct: float,
    next_trade_est_pnl: float = 0,
    today_realized_pnl: float = 0,
) -> tuple[bool, str]:
    """Consistency ルール予測チェック（Eval / Funded 共通）。

    P0-CRITICAL-6 根本修正:
      - cycle_daily_pnl は「確定済み過去日」の PnL のみを含む（今日は含まない）
      - today_total は「今日の確定済み + 次取引の見込み PnL」として独立管理
      - 過去日リストに今日の値が混入すると今日が二重計上されるバグを防ぐ

    Args:
        cycle_daily_pnl:    Payout cycle 内の「過去日」日次 PnL リスト（今日は含めない）
        max_pct:            上限比率（0.40 = 40%）
        next_trade_est_pnl: 次取引の見込み PnL
        today_realized_pnl: 今日の確定済み PnL（当日分のみ）

    Returns:
        (allow, reason)
    """
    # 過去日の正値のみを抽出（今日は含まない）
    past_positive = [p for p in cycle_daily_pnl if p > 0]

    # 今日のトータル = 今日の確定済み + 次取引の見込み
    today_total = float(today_realized_pnl) + max(0.0, float(next_trade_est_pnl))

    # 全正値日リスト: 過去日 + 今日（今日 > 0 の場合のみ追加）
    if today_total > 0:
        all_positive = past_positive + [today_total]
    else:
        all_positive = past_positive

    total = sum(all_positive)
    if total <= 0:
        return True, ""

    max_day = max(all_positive)
    pct = max_day / total

    # 予兆: 目標の 90%
    if pct >= max_pct * 0.90:
        return False, (
            f"Consistency予兆90%到達: 最大日={pct*100:.1f}% >= 上限{max_pct*100:.0f}%×0.9"
            f" (today_total={today_total:.0f}, cycle_total={total:.0f})"
        )
    if pct > max_pct:
        return False, (
            f"Consistency違反予兆: 最大日={pct*100:.1f}% > 上限{max_pct*100:.0f}%"
            f" (today_total={today_total:.0f}, cycle_total={total:.0f})"
        )
    return True, ""


def check_max_contracts(
    requested_qty: int,
    plan_rules: dict,
    current_balance: float,
    contract_type: str = "mini",
) -> tuple[bool, str]:
    """枚数上限チェック。Flex は残高連動テーブル対応。

    Args:
        requested_qty: 発注枚数
        plan_rules:    get_plan_rules() で取得した dict
        current_balance: 現在残高（Flex 残高連動用）
        contract_type: "mini" | "micro"
    """
    # Flex Sim-Funded: 残高連動テーブル
    tiers = plan_rules.get("max_contracts_mini_funded_tiers")
    if tiers:
        max_allowed = 1
        for tier in tiers:
            if current_balance >= tier["balance_min"]:
                max_allowed = tier["max_mini"]
        if contract_type == "micro":
            max_allowed *= 10
        if requested_qty > max_allowed:
            return False, (
                f"枚数上限超過(Flex残高連動): {requested_qty} > {max_allowed} "
                f"(balance={current_balance:.0f})"
            )
        return True, ""

    # 通常プラン
    if contract_type == "micro":
        max_allowed = plan_rules.get("max_contracts_micro") or plan_rules.get("max_contracts_mini", 5) * 10
    else:
        max_allowed = plan_rules.get("max_contracts_mini", 5)

    if requested_qty > max_allowed:
        return False, f"枚数上限超過: {requested_qty} > {max_allowed} ({contract_type})"
    return True, ""


def get_cme_trading_day(now: Optional[datetime.datetime] = None) -> datetime.date:
    """CME Globex の「取引日」を ET で返す。

    P1-HIGH-10: CME のセッション境界は 17:00 ET（前日終了 / 当日開始）。
    17:00 ET 以降は「翌取引日」としてカウントする。
    DST は pytz/zoneinfo で自動対応（America/New_York を使用）。

    例:
        月曜 16:59 ET → 月曜の取引日
        月曜 17:00 ET → 火曜の取引日（取引日カウントが切り替わる）
        金曜 17:00 ET → 月曜の取引日（週末を跨ぐため）
    """
    try:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        et = pytz.timezone("America/New_York")

    if now is None:
        now = datetime.datetime.now(tz=et)
    elif now.tzinfo is None:
        # naive → UTC として ET に変換
        now = now.replace(tzinfo=datetime.timezone.utc).astimezone(et)
    else:
        now = now.astimezone(et)

    # 17:00 ET 以降は翌取引日
    if now.hour >= 17:
        next_day = now.date() + datetime.timedelta(days=1)
        # 土曜 17:00 → 月曜、日曜 17:00 → 月曜
        while next_day.weekday() in (5, 6):  # 5=土, 6=日
            next_day += datetime.timedelta(days=1)
        return next_day
    return now.date()


def check_hft_daily_count(
    trades_today: int,
    limit: int = 180,
) -> tuple[bool, str]:
    """HFT 認定回避: 1日 180 件上限（MFFU 200 認定ラインの安全マージン 90%）。

    150 件（83%）到達で予兆警告を返す。

    P1-HIGH-10: 「取引日」は CME Session 境界（17:00 ET）で定義する。
    呼出側で get_cme_trading_day() を使って当日カウンタをリセットすること。

    Args:
        trades_today: 当日の完了取引件数（CME取引日カウント）
        limit:        上限（デフォルト 180）
    """
    if trades_today >= limit:
        return False, f"HFT日次上限到達: {trades_today}/{limit}件"
    warn_threshold = int(limit * 0.833)  # ~150
    if trades_today >= warn_threshold:
        log.warning("[HFT予兆] 当日取引数 %d/%d 件", trades_today, limit)
    return True, ""


def check_microscalping(
    recent_trades: list[dict],
    min_hold_sec: int = 15,
    max_short_ratio: float = 0.40,
) -> tuple[bool, str]:
    """Microscalping 検出: 直近 20 件で 10 秒以下保有が 40% 超なら拒否。

    Tradeify: 50% で違反。安全マージン 80% = 40% を上限とする。

    Args:
        recent_trades:  [{"entry_ts": datetime, "exit_ts": datetime, ...}, ...]
                        exit_ts がない（オープン中）エントリは無視する
        min_hold_sec:   短保有判定秒数（デフォルト 15 秒）
        max_short_ratio: 短保有割合上限（デフォルト 0.40 = 40%）
    """
    # P1-HIGH-11: entry_ts は fill_time_actual（約定時刻）、exit_ts は close_fill_time_actual を使う。
    # 呼出側で Tradovate API のフィル時刻を明示的に渡すこと。
    # entry_ts / exit_ts が None または非 datetime の場合はスキップ（計測不能）。
    closed = [
        t for t in recent_trades
        if t.get("exit_ts") is not None
        and isinstance(t.get("entry_ts"), datetime.datetime)
        and isinstance(t.get("exit_ts"), datetime.datetime)
    ]
    if len(closed) < 5:
        return True, ""  # サンプル不足: チェックスキップ
    window = closed[-20:]
    short_holds = [
        t for t in window
        if (t["exit_ts"] - t["entry_ts"]).total_seconds() < 10
    ]
    ratio = len(short_holds) / len(window)
    if ratio > max_short_ratio:
        return False, (
            f"Microscalping予兆: 10秒以下={ratio*100:.1f}% > 上限{max_short_ratio*100:.0f}%"
        )
    return True, ""


def check_hedging(
    symbol: str,
    side: str,
    open_positions: list[dict],
) -> tuple[bool, str]:
    """ヘッジ禁止チェック（同一シンボル / 相関商品両建て）。

    Args:
        symbol:         発注銘柄
        side:           "BUY" | "SELL"
        open_positions: [{"symbol": str, "side": "BUY"|"SELL", ...}, ...]
    """
    prohibited = get_common_prohibited()
    same_product = prohibited["hedging"]["same_product_pairs"]
    opposite = "SELL" if side.upper() == "BUY" else "BUY"

    for pos in open_positions:
        pos_side = pos.get("side", "").upper()
        # BUY/LONG / SELL/SHORT を正規化
        if pos_side in ("LONG",):
            pos_side = "BUY"
        elif pos_side in ("SHORT",):
            pos_side = "SELL"

        if pos_side != opposite:
            continue

        pos_sym = pos.get("symbol", "").upper()
        # 同一シンボル両建て
        if pos_sym == symbol.upper():
            return False, f"ヘッジ禁止: {symbol} で BUY と SELL 両建て"
        # 相関商品ペア両建て
        pair = frozenset([symbol.upper(), pos_sym])
        for known in same_product:
            if pair == frozenset(known):
                return False, (
                    f"相関ヘッジ禁止: {symbol}({side}) と {pos_sym}({pos_side}) は同一プロダクト"
                )
    return True, ""


def check_t1_news_blackout(
    now: datetime.datetime,
    upcoming_events: list[dict],
    phase: str,
    plan_rules: dict,
) -> tuple[bool, str]:
    """T1 ニュース前後 2 分ブラックアウト。

    Flex Funded は t1_news_funded_allowed=true のため例外的にスキップ。

    Args:
        now:             現在日時（timezone-aware 推奨）
        upcoming_events: [{"tier": int, "ts": datetime, "name": str}, ...]
        phase:           "evaluation" | "funded" | "sim_funded" 等
        plan_rules:      get_plan_rules() の返り値
    """
    if phase in ("funded", "sim_funded") and plan_rules.get("t1_news_funded_allowed"):
        return True, ""
    blackout_sec = 120
    for ev in upcoming_events:
        if ev.get("tier") != 1:
            continue
        ev_ts = ev["ts"]
        # timezone 混在対応
        if hasattr(ev_ts, "tzinfo") and ev_ts.tzinfo and not now.tzinfo:
            now = now.replace(tzinfo=datetime.timezone.utc)
        elif not (hasattr(ev_ts, "tzinfo") and ev_ts.tzinfo) and now.tzinfo:
            ev_ts = ev_ts.replace(tzinfo=datetime.timezone.utc)

        # P1-HIGH-9: abs() を廃止。未来イベント（発表前）と過去イベント（発表後）を分離する。
        # - 未来イベント: now < ev_ts の場合 → 発表直前窓（イベントまで blackout_sec 以内）
        # - 過去イベント: now > ev_ts の場合 → 発表直後窓（イベントから blackout_sec 以内）
        # abs() を使うと発表から数時間後のイベントも誤ってブロックする問題があった。
        seconds_to_event = (ev_ts - now).total_seconds()  # 正=未来、負=過去

        if -blackout_sec <= seconds_to_event <= blackout_sec:
            if seconds_to_event >= 0:
                label = f"発表まで{seconds_to_event:.0f}秒"
            else:
                label = f"発表から{abs(seconds_to_event):.0f}秒経過"
            return False, (
                f"T1 News blackout: {ev.get('name','?')} @ {ev['ts']}, {label}"
            )
    return True, ""


def check_dca_pattern(
    symbol: str,
    side: str,
    open_positions: list[dict],
    firm: str,
    phase: str,
) -> tuple[bool, str]:
    """Apex PA 口座: 損失ポジへの追加発注（DCA）を物理的に禁止。

    Apex PA のみ適用（MFFU / Tradeify は対象外）。

    Args:
        symbol:         発注銘柄
        side:           "BUY" | "SELL"
        open_positions: [{"symbol": str, "side": str, "unrealized_pnl": float}, ...]
        firm:           "apex" | "mffu" | "tradeify"
        phase:          "pa" | "evaluation" | "funded" 等
    """
    if firm.lower() != "apex" or phase.lower() != "pa":
        return True, ""
    for pos in open_positions:
        pos_sym = pos.get("symbol", "").upper()
        pos_side = pos.get("side", "").upper()
        if pos_side in ("LONG",):
            pos_side = "BUY"
        elif pos_side in ("SHORT",):
            pos_side = "SELL"
        if pos_sym == symbol.upper() and pos_side == side.upper():
            upnl = pos.get("unrealized_pnl", 0)
            if upnl < 0:
                return False, (
                    f"Apex DCA禁止: {symbol} {side} 損失ポジ(unrealized={upnl:.0f})への追加"
                )
    return True, ""


def check_inactivity(
    last_trade_date: Optional[datetime.date],
    max_days: int = 7,
) -> tuple[bool, str]:
    """Flex Sim-Funded の 7 日 inactivity 失効警告。

    6 日目でアラート、7 日目で失効扱いとして False を返す。

    Args:
        last_trade_date: 最後に取引した日付（None なら今日扱いで常に True）
        max_days:        失効までの日数（デフォルト 7）
    """
    if last_trade_date is None:
        return True, ""
    # P1-HIGH-13: date.today() はローカルTZ依存。MFFU/CME はET基準のためET日付を使う。
    try:
        import zoneinfo as _zi
        _et_tz = _zi.ZoneInfo("America/New_York")
    except ImportError:
        import pytz as _pytz
        _et_tz = _pytz.timezone("America/New_York")
    _today_et = datetime.datetime.now(tz=_et_tz).date()
    days = (_today_et - last_trade_date).days
    if days >= max_days:
        return False, f"Inactivity失効: {days}日取引なし >= {max_days}日"
    if days >= max_days - 1:
        return False, f"Inactivity予兆: {days}日経過（残り{max_days - days}日で失効）"
    return True, ""


def check_payout_eligibility_with_freeze(
    account_state: dict,
    rules: dict,
) -> tuple[bool, str]:
    """Payout Freeze Layer (Layer PF-4): Consistency 予兆で追加エントリーを freeze。

    cycle_daily_pnl に基づき、目標 consistency の 90% に達した場合は
    その日の追加エントリーを止め、Payout 申請可能な状態を維持する。

    Args:
        account_state: {"cycle_daily_pnl": list[float], ...}
        rules:         get_plan_rules() の返り値
    """
    cycle_pnl = account_state.get("cycle_daily_pnl", [])
    if not cycle_pnl:
        return True, ""
    positive = [p for p in cycle_pnl if p > 0]
    total = sum(positive)
    if total <= 0:
        return True, ""
    max_day = max(positive)
    pct = max_day / total
    target_pct = rules.get("consistency_funded_pct") or rules.get("consistency_pct")
    if not target_pct:
        return True, ""
    if pct >= target_pct * 0.90:
        return False, (
            f"Consistency予兆 Payout Freeze: 最大日{pct*100:.1f}% >= 目標{target_pct*100:.0f}%×0.9"
        )
    return True, ""


# ── 統合チェック（Layer PF-1 エントリポイント） ──────────────────────────────

def check_prop_firm_compliance(
    firm: str,
    plan: str,
    phase: str,
    account_state: dict,
    order_ctx: dict,
) -> tuple[bool, str, str]:
    """Pre-Trade 統合チェック。chronos_pre_trade_check.py の Layer PF-1 として呼ぶ。

    全チェックが通ると (True, "PF-1-PASS", "全チェック合格") を返す。
    いずれかで弾かれると (False, "PF-1-XXX", reason) を返す。

    Args:
        firm:  "mffu" | "tradeify" | "apex"
        plan:  "core_50k" | "rapid_50k" | "lightning_150k" 等
        phase: "evaluation" | "sim_funded" | "funded" | "pa" | "live"
        account_state: {
            balance:           float,   # 現在残高
            peak_balance:      float,   # ピーク残高
            daily_pnl:         float,   # 当日 PnL
            cycle_daily_pnl:   list[float],
            trades_today:      int,
            recent_trades:     list[dict],  # {"entry_ts", "exit_ts"}
            open_positions:    list[dict],  # {"symbol", "side", "unrealized_pnl"}
            last_trade_date:   date | None,
            payout_count:      int,
        }
        order_ctx: {
            symbol:          str,
            side:            "BUY" | "SELL",
            qty:             int,
            contract_type:   "mini" | "micro",  # default "mini"
            est_pnl:         float,
            upcoming_events: list[dict],
        }

    Returns:
        (allow: bool, layer: str, reason: str)
    """
    try:
        rules = get_plan_rules(firm, plan)
    except KeyError as e:
        return False, "PF-1-CONFIG", f"YAML に firm/plan が未定義: {e}"

    # ── Rapid 起動フラグチェック ────────────────────────────────────────────
    if plan == "rapid_50k" and not is_rapid_enabled():
        return False, "PF-1-RAPID-DISABLED", (
            "Rapid は Phase A 完了まで起動禁止。"
            "meta.rapid_enabled=true に設定後に再試行してください。"
        )

    # ── 1. MLL ─────────────────────────────────────────────────────────────
    payout_count = account_state.get("payout_count", 0)
    if plan == "flex_50k" and payout_count >= 1:
        mll = rules.get("mll_after_first_payout", rules.get("mll", 2000))
    elif "mll" in rules:
        mll = rules["mll"]
    elif "max_trailing_drawdown" in rules:
        mll = rules["max_trailing_drawdown"]
    elif "trailing_drawdown" in rules:
        mll = rules["trailing_drawdown"]
    else:
        mll = rules.get("mll_default", 2000)

    # P0-CRITICAL-5: sim_funded フェーズでは drawdown_type_sim_funded を優先参照する。
    # Rapid Sim Funded は intraday_trailing_4pct が適用されるため、
    # evaluation/funded と同じ eod_trailing を使うと過小チェックになる。
    if phase == "sim_funded" and "drawdown_type_sim_funded" in rules:
        drawdown_type_for_check = rules["drawdown_type_sim_funded"]
    else:
        drawdown_type_for_check = rules.get("drawdown_type", "eod_trailing")

    ok, msg = check_mll_breach(
        account_state["balance"],
        account_state["peak_balance"],
        mll,
        drawdown_type_for_check,
    )
    if not ok:
        return False, "PF-1-MLL", msg

    # ── 2. Daily Loss Limit ────────────────────────────────────────────────
    dll = rules.get("daily_loss_limit_soft_pause") or rules.get("daily_loss_limit")
    ok, msg = check_daily_loss_limit(account_state.get("daily_pnl", 0), dll)
    if not ok:
        return False, "PF-1-DLL", msg

    # ── 3. Consistency ────────────────────────────────────────────────────
    if phase in ("funded", "sim_funded", "live"):
        con_pct = rules.get("consistency_funded_pct") or rules.get("consistency_pct")
    else:
        con_pct = rules.get("consistency_eval_pct") or rules.get("consistency_pct")

    if con_pct:
        ok, msg = check_consistency(
            account_state.get("cycle_daily_pnl", []),
            con_pct,
            order_ctx.get("est_pnl", 0),
            # MF-4 fix: today_realized_pnl を渡す（H-1 Day 1 Consistency bypass 防止）。
            # account_state に daily_pnl が入っている場合はそれを当日確定済み PnL として使用する。
            today_realized_pnl=account_state.get("daily_pnl", 0),
        )
        if not ok:
            return False, "PF-1-CON", msg

    # ── 4. 枚数上限 ────────────────────────────────────────────────────────
    ok, msg = check_max_contracts(
        order_ctx.get("qty", 1),
        rules,
        account_state.get("balance", 0),
        order_ctx.get("contract_type", "mini"),
    )
    if not ok:
        return False, "PF-1-QTY", msg

    # ── 5. HFT ─────────────────────────────────────────────────────────────
    ok, msg = check_hft_daily_count(account_state.get("trades_today", 0))
    if not ok:
        return False, "PF-1-HFT", msg

    # ── 6. Microscalping ───────────────────────────────────────────────────
    ok, msg = check_microscalping(account_state.get("recent_trades", []))
    if not ok:
        return False, "PF-1-MSC", msg

    # ── 7. ヘッジ ──────────────────────────────────────────────────────────
    ok, msg = check_hedging(
        order_ctx["symbol"],
        order_ctx["side"],
        account_state.get("open_positions", []),
    )
    if not ok:
        return False, "PF-1-HEDGE", msg

    # ── 8. T1 News Blackout ────────────────────────────────────────────────
    ok, msg = check_t1_news_blackout(
        datetime.datetime.now(),
        order_ctx.get("upcoming_events", []),
        phase,
        rules,
    )
    if not ok:
        return False, "PF-1-NEWS", msg

    # ── 9. DCA（Apex PA 専用） ────────────────────────────────────────────
    ok, msg = check_dca_pattern(
        order_ctx["symbol"],
        order_ctx["side"],
        account_state.get("open_positions", []),
        firm,
        phase,
    )
    if not ok:
        return False, "PF-1-DCA", msg

    # ── 10. Inactivity（Flex 専用） ────────────────────────────────────────
    ok, msg = check_inactivity(account_state.get("last_trade_date"))
    if not ok:
        return False, "PF-1-INACT", msg

    return True, "PF-1-PASS", "全チェック合格"
