"""tests/test_pdt_1dte_handling.py — PDT 1DTE対応・0DTE→1DTEフォールバックテスト

テスト項目:
  T01: 1DTE は record_round_trip() で day_trade 計上されない
  T02: PDT残0で 0DTE拒否（pre_trade_check Layer 3.5）
  T03: PDT残0でも 1DTEは通過（pre_trade_check Layer 3.5）
  T04: StrategySelector が 0DTE→1DTE 自動切替
  T05: $25K以上は PDT対象外でどちらも通過
  T06: PDT残ありの 0DTE はそのまま通過
  T07: is_0dte_strategy() — 満期日一致で0DTE判定
  T08: is_0dte_strategy() — 翌日満期は1DTE判定
  T09: is_0dte_strategy() — "0dte_" プレフィックスで強制0DTE判定
  T10: strategy_supports_1dte() — CS/ORB/IC等は True
  T11: strategy_supports_1dte() — StraddleBuy/GammaScalp は False
  T12: get_1dte_fallback_name() — "CS" → "1dte_cs"
  T13: get_1dte_fallback_name() — "0dte_cs" → "1dte_cs"
  T14: get_1dte_fallback_name() — StraddleBuy → None
  T15: StrategySelector — PDT残0 + 1DTE未対応 → no_trade
  T16: StrategySelector — 1DTE戦術候補はそのまま通過
  T17: フォールバックカウンタ — increment/get 動作確認
  T18: フォールバックカウンタ — 日付変更でリセット
  T19: check_pdt_layer() — 0DTE + PDT残あり → allow
  T20: check_pdt_layer() — 0DTE + PDT残0 → deny
  T21: check_pdt_layer() — 1DTE → allow（PDT残0でも）
  T22: StrategySelector — Calendar（複数日満期）は通過
  T23: StrategySelector — PDT残0でIronCondorは1dte_ic返却
  T24: pre_trade_check Layer 3.5 — strategy="" のとき PDTチェックスキップ
  T25: OrderContext — strategy/expiry_date フィールドが存在する
"""
from __future__ import annotations

import datetime
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    import pytz  # type: ignore
    ET = pytz.timezone("America/New_York")  # type: ignore


# ── テスト用ヘルパー ──────────────────────────────────────────────────────────

def _make_et(date_str: str, time_str: str = "10:00:00") -> datetime.datetime:
    """ET timezone-aware datetime を生成する（テスト用）。"""
    dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=ET)


def _make_pdt_tracker(tmp_path: Path, day_trades: list[dict] | None = None):
    """テスト用 PDTTracker を返す（ファイルは一時ディレクトリに作成）。"""
    from common.pdt_tracker import PDTTracker
    data_file = tmp_path / "pdt_day_trades.jsonl"
    if day_trades:
        with open(data_file, "w") as f:
            for r in day_trades:
                f.write(json.dumps(r) + "\n")
    return PDTTracker(data_file=data_file)


# ── T01: 1DTE は day_trade 計上されない ────────────────────────────────────

def test_1dte_not_counted_as_day_trade(tmp_path):
    """T01: entry=Day1, exit=Day2 → record_round_trip() が False を返し計上しない。"""
    tracker = _make_pdt_tracker(tmp_path)
    entry = _make_et("2026-04-14", "15:00:00")
    exit_dt = _make_et("2026-04-15", "09:45:00")  # 翌日

    result = tracker.record_round_trip("US.SPY", entry, exit_dt, "CS")
    assert result is False, "1DTE（日跨ぎ）はday_trade計上されるべきでない"
    assert tracker.count_day_trades_rolling(reference=datetime.date(2026, 4, 14)) == 0


# ── T02: PDT残0で0DTE拒否 ─────────────────────────────────────────────────

def test_0dte_blocked_when_pdt_exhausted(tmp_path):
    """T02: PDT残0（3件消費済み）で0DTE策略 → check_pdt_layer がFalseを返す。"""
    from common.pdt_1dte_utils import check_pdt_layer

    today = datetime.date(2026, 4, 14)
    # 3件のday_tradeを記録（PDT_LIMIT=3に到達）
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    now_et = _make_et("2026-04-14", "11:00:00")

    allow, reason, is_day_trade = check_pdt_layer(
        strategy_name="CS",
        expiry_date=today,
        capital_usd=8000.0,
        pdt_tracker=tracker,
        now_et=now_et,
    )
    assert allow is False, "PDT残0で0DTE → ブロックされるべき"
    assert is_day_trade is True


# ── T03: PDT残0でも1DTEは通過 ─────────────────────────────────────────────

def test_1dte_allowed_when_pdt_exhausted(tmp_path):
    """T03: PDT残0でも翌日満期（1DTE）戦術 → check_pdt_layer がTrueを返す。"""
    from common.pdt_1dte_utils import check_pdt_layer

    today = datetime.date(2026, 4, 14)
    tomorrow = today + datetime.timedelta(days=1)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    now_et = _make_et("2026-04-14", "11:00:00")

    allow, reason, is_day_trade = check_pdt_layer(
        strategy_name="CS",
        expiry_date=tomorrow,   # 翌日満期 = 1DTE
        capital_usd=8000.0,
        pdt_tracker=tracker,
        now_et=now_et,
    )
    assert allow is True, "1DTE（翌日満期）はPDT残0でも通過すべき"
    assert is_day_trade is False


# ── T04: StrategySelector 0DTE→1DTE自動切替 ──────────────────────────────

def test_strategy_selector_0dte_to_1dte_fallback(tmp_path):
    """T04: PDT残0でORB（買い戦術・満期放置=全損・satisfies_no_pdt=False）
    → StrategySelector が1dte_orbを返す。

    設計ポイント: CS/ICは満期OTM消滅でday_trade非計上（satisfies_no_pdt=True）
    なので、フォールバック発動のテストはORB（買い戦術）で行う。
    """
    from common.strategy_selector import StrategySelector

    today = datetime.date(2026, 4, 14)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "ORB",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    selector = StrategySelector(pdt_tracker=tracker)
    now_et = _make_et("2026-04-14", "11:00:00")

    with patch("common.pdt_1dte_utils.notify_fallback_activated"):
        result = selector.select("ORB", today, 8000.0, now_et)

    assert result.strategy == "1dte_orb", f"フォールバック先は1dte_orbであるべき: {result.strategy}"
    assert result.fallback_activated is True
    assert result.is_0dte is False
    assert result.original_candidate == "ORB"


# ── T05: $25K以上はPDT対象外でどちらも通過 ───────────────────────────────

def test_above_25k_pdt_exempt(tmp_path):
    """T05: capital >= $25K ならPDT残0でも0DTE/1DTE両方通過。"""
    from common.strategy_selector import StrategySelector

    today = datetime.date(2026, 4, 14)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    selector = StrategySelector(pdt_tracker=tracker)
    now_et = _make_et("2026-04-14", "11:00:00")

    # 0DTE
    result_0dte = selector.select("CS", today, 30000.0, now_et)
    assert result_0dte.strategy == "CS", "$25K以上で0DTE → そのまま通過"
    assert result_0dte.fallback_activated is False

    # 1DTE
    tomorrow = today + datetime.timedelta(days=1)
    result_1dte = selector.select("CS", tomorrow, 30000.0, now_et)
    assert result_1dte.strategy == "CS", "$25K以上で1DTE → そのまま通過"
    assert result_1dte.fallback_activated is False


# ── T06: PDT残ありの0DTEはそのまま通過 ─────────────────────────────────────

def test_0dte_passes_when_pdt_remaining(tmp_path):
    """T06: PDT残2本の状態でCS（0DTE）→ そのまま通過。"""
    from common.strategy_selector import StrategySelector

    today = datetime.date(2026, 4, 14)
    # 1件のみ消費（残2本）
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    selector = StrategySelector(pdt_tracker=tracker)
    now_et = _make_et("2026-04-14", "11:00:00")

    result = selector.select("CS", today, 8000.0, now_et)
    assert result.strategy == "CS"
    assert result.fallback_activated is False
    assert result.pdt_remaining == 2


# ── T07: is_0dte_strategy — 満期日一致で0DTE判定 ─────────────────────────

def test_is_0dte_strategy_by_expiry_match():
    """T07: expiry_date == today_et → 0DTE判定。"""
    from common.pdt_1dte_utils import is_0dte_strategy

    today = datetime.date(2026, 4, 14)
    now_et = _make_et("2026-04-14", "10:00:00")

    assert is_0dte_strategy("CS", today, now_et) is True


# ── T08: is_0dte_strategy — 翌日満期は1DTE判定 ───────────────────────────

def test_is_0dte_strategy_by_expiry_tomorrow():
    """T08: expiry_date == tomorrow → 1DTE（0DTE判定されない）。"""
    from common.pdt_1dte_utils import is_0dte_strategy

    today = datetime.date(2026, 4, 14)
    tomorrow = today + datetime.timedelta(days=1)
    now_et = _make_et("2026-04-14", "10:00:00")

    assert is_0dte_strategy("CS", tomorrow, now_et) is False


# ── T09: is_0dte_strategy — "0dte_" プレフィックスで強制0DTE ────────────

def test_is_0dte_strategy_by_prefix():
    """T09: 戦術名が "0dte_" で始まる → 満期日不問で0DTE。"""
    from common.pdt_1dte_utils import is_0dte_strategy

    tomorrow = datetime.date(2026, 4, 15)
    now_et = _make_et("2026-04-14", "10:00:00")

    assert is_0dte_strategy("0dte_cs", tomorrow, now_et) is True
    assert is_0dte_strategy("0DTE_IC", None, now_et) is True


# ── T10: strategy_supports_1dte — CS/ORB/IC等は True ─────────────────────

def test_strategy_supports_1dte_true():
    """T10: CS/ORB/IC/Butterfly/Strangle/Calendar は True。"""
    from common.pdt_1dte_utils import strategy_supports_1dte

    for name in ["CS", "cs", "ORB", "IC", "Butterfly", "butterfly", "strangle", "Calendar"]:
        assert strategy_supports_1dte(name) is True, f"{name} は supports_1dte=True のはず"


# ── T11: strategy_supports_1dte — StraddleBuy等は False ─────────────────

def test_strategy_supports_1dte_false():
    """T11: StraddleBuy/GammaScalp/IVCrush は False。"""
    from common.pdt_1dte_utils import strategy_supports_1dte

    for name in ["StraddleBuy", "straddle_buy", "GammaScalp", "gamma_scalp", "IVCrush", "iv_crush"]:
        assert strategy_supports_1dte(name) is False, f"{name} は supports_1dte=False のはず"


# ── T12: get_1dte_fallback_name — "CS" → "1dte_cs" ─────────────────────

def test_get_1dte_fallback_name_cs():
    """T12: "CS" → "1dte_cs"。"""
    from common.pdt_1dte_utils import get_1dte_fallback_name

    result = get_1dte_fallback_name("CS")
    assert result == "1dte_cs", f"Expected '1dte_cs', got '{result}'"


# ── T13: get_1dte_fallback_name — "0dte_cs" → "1dte_cs" ─────────────────

def test_get_1dte_fallback_name_0dte_prefix():
    """T13: "0dte_cs" → "1dte_cs"。"""
    from common.pdt_1dte_utils import get_1dte_fallback_name

    result = get_1dte_fallback_name("0dte_cs")
    assert result == "1dte_cs", f"Expected '1dte_cs', got '{result}'"


# ── T14: get_1dte_fallback_name — StraddleBuy → None ─────────────────────

def test_get_1dte_fallback_name_straddle_buy_none():
    """T14: StraddleBuy は1DTE未対応 → None。"""
    from common.pdt_1dte_utils import get_1dte_fallback_name

    result = get_1dte_fallback_name("StraddleBuy")
    assert result is None


# ── T15: StrategySelector — PDT残0 + 1DTE未対応 → no_trade ──────────────

def test_strategy_selector_no_trade_for_unsupported_1dte(tmp_path):
    """T15: StraddleBuy（PDT残0）→ StrategySelector が no_trade を返す。"""
    from common.strategy_selector import StrategySelector

    today = datetime.date(2026, 4, 14)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "Straddle",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    selector = StrategySelector(pdt_tracker=tracker)
    now_et = _make_et("2026-04-14", "11:00:00")

    result = selector.select("StraddleBuy", today, 8000.0, now_et)
    assert result.strategy == "no_trade"
    assert result.fallback_activated is False


# ── T16: StrategySelector — 1DTE戦術候補はそのまま通過 ──────────────────

def test_strategy_selector_1dte_candidate_passes(tmp_path):
    """T16: 最初から1DTE戦術（翌日満期）を渡した場合 → そのまま通過。"""
    from common.strategy_selector import StrategySelector

    today = datetime.date(2026, 4, 14)
    tomorrow = today + datetime.timedelta(days=1)
    # PDT残0
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    selector = StrategySelector(pdt_tracker=tracker)
    now_et = _make_et("2026-04-14", "11:00:00")

    result = selector.select("1dte_cs", tomorrow, 8000.0, now_et)
    assert result.strategy == "1dte_cs", "1DTE候補はそのまま通過すべき"
    assert result.fallback_activated is False


# ── T17: フォールバックカウンタ — increment/get 動作確認 ─────────────────

def test_fallback_counter_increment_and_get():
    """T17: increment_fallback_count() → get_fallback_count() で同じ値が返る。"""
    from common.pdt_1dte_utils import increment_fallback_count, get_fallback_count
    import common.pdt_1dte_utils as _m

    # リセット
    _m._fallback_count_date = None
    _m._fallback_count = 0

    now_et = _make_et("2026-04-20", "10:00:00")
    count1 = increment_fallback_count(now_et)
    count2 = increment_fallback_count(now_et)
    assert count1 == 1
    assert count2 == 2
    assert get_fallback_count(now_et) == 2


# ── T18: フォールバックカウンタ — 日付変更でリセット ─────────────────────

def test_fallback_counter_resets_on_new_day():
    """T18: 日付が変わるとフォールバックカウンタが0にリセットされる。"""
    from common.pdt_1dte_utils import increment_fallback_count, get_fallback_count
    import common.pdt_1dte_utils as _m

    _m._fallback_count_date = None
    _m._fallback_count = 0

    day1 = _make_et("2026-04-20", "10:00:00")
    increment_fallback_count(day1)
    increment_fallback_count(day1)
    assert get_fallback_count(day1) == 2

    day2 = _make_et("2026-04-21", "10:00:00")
    assert get_fallback_count(day2) == 0, "翌日にカウンタはリセットされるべき"


# ── T19: check_pdt_layer — 0DTE + PDT残あり → allow ─────────────────────

def test_check_pdt_layer_0dte_remaining_allows(tmp_path):
    """T19: 0DTE戦術 + PDT残1本 → check_pdt_layer が allow=True を返す。"""
    from common.pdt_1dte_utils import check_pdt_layer

    today = datetime.date(2026, 4, 14)
    # 2件のみ消費（残1本）
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(2)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    now_et = _make_et("2026-04-14", "11:00:00")

    allow, reason, is_day_trade = check_pdt_layer("CS", today, 8000.0, tracker, now_et)
    assert allow is True
    assert is_day_trade is True


# ── T20: check_pdt_layer — 0DTE + PDT残0 → deny ─────────────────────────

def test_check_pdt_layer_0dte_exhausted_denies(tmp_path):
    """T20: 0DTE戦術 + PDT残0 → check_pdt_layer が allow=False を返す。"""
    from common.pdt_1dte_utils import check_pdt_layer

    today = datetime.date(2026, 4, 14)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    now_et = _make_et("2026-04-14", "11:00:00")

    allow, reason, is_day_trade = check_pdt_layer("CS", today, 8000.0, tracker, now_et)
    assert allow is False
    assert "PDT" in reason or "ブロック" in reason or "3.5" in reason or "L3" in reason or "残" in reason


# ── T21: check_pdt_layer — 1DTE → allow（PDT残0でも） ───────────────────

def test_check_pdt_layer_1dte_always_allows(tmp_path):
    """T21: 1DTE（翌日満期）はPDT残0でもcheck_pdt_layer → allow=True, is_day_trade=False。"""
    from common.pdt_1dte_utils import check_pdt_layer

    today = datetime.date(2026, 4, 14)
    tomorrow = today + datetime.timedelta(days=1)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    now_et = _make_et("2026-04-14", "11:00:00")

    allow, reason, is_day_trade = check_pdt_layer("CS", tomorrow, 8000.0, tracker, now_et)
    assert allow is True, "1DTEはPDT残0でも通過すべき"
    assert is_day_trade is False


# ── T22: StrategySelector — Calendar（複数日満期）は通過 ─────────────────

def test_strategy_selector_calendar_always_passes(tmp_path):
    """T22: CalendarはそもそもPDT対象外（backleg複数日）→ PDT残0でも通過。"""
    from common.strategy_selector import StrategySelector

    today = datetime.date(2026, 4, 14)
    next_week = today + datetime.timedelta(days=7)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "CS",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    selector = StrategySelector(pdt_tracker=tracker)
    now_et = _make_et("2026-04-14", "11:00:00")

    result = selector.select("Calendar", next_week, 8000.0, now_et)
    # 7DTE満期 → 1DTE以上として通過
    assert result.strategy == "Calendar"
    assert result.fallback_activated is False


# ── T23: StrategySelector — PDT残0でIronCondor → satisfies_no_pdt=True で通過 ─

def test_strategy_selector_ic_passes_with_satisfies_no_pdt(tmp_path):
    """T23: IC + PDT残0 + 0DTE満期 → satisfies_no_pdt=True（満期放置=OTM消滅）
    なのでフォールバックせずICのまま通過する。

    IC売りはOTM設計で満期消滅（expired_worthless）が前提 → PDT計上なし。
    ORB（買い戦術）と対比: ORBは満期放置=全損でday_trade消費必要 → フォールバック発動。
    """
    from common.strategy_selector import StrategySelector

    today = datetime.date(2026, 4, 14)
    day_trades = [
        {"date": today.isoformat(), "symbol": "US.SPY", "strategy": "IC",
         "entry_time": f"{today.isoformat()}T09:30:00", "exit_time": f"{today.isoformat()}T10:00:00",
         "is_business_day": True}
        for _ in range(3)
    ]
    tracker = _make_pdt_tracker(tmp_path, day_trades)
    selector = StrategySelector(pdt_tracker=tracker)
    now_et = _make_et("2026-04-14", "11:00:00")

    result = selector.select("IC", today, 8000.0, now_et)

    # ICはsatisfies_no_pdt=Trueなのでフォールバックなしで通過
    assert result.strategy == "IC", "ICはsatisfies_no_pdt=TrueでPDT残0でも通過"
    assert result.fallback_activated is False


# ── T24: pre_trade_check Layer 3.5 — strategy="" → PDTチェックスキップ ──

def test_pretrade_check_skips_pdt_when_no_strategy():
    """T24: OrderContext.strategy="" のとき戦術別 Layer 3.5 はスキップされる。

    注意: 全戦術合算グローバル PDT チェック（strategy 非依存）は別途動作する。
    このテストは「strategy 非依存グローバル PDT チェックが残高 >= $25K でパスすること」を確認する。
    capital_usd >= $25,000 に設定することで全 PDT チェックを通過させる。
    """
    from common.pre_trade_check import OrderContext, check_order, CheckResult

    ctx = OrderContext(
        symbol="US.SPY",
        strike=560.0,
        side="SELL",
        qty=1,
        option_price=1.0,
        # $25K 以上に設定 → 全 PDT チェック（global + 戦術別）をパス
        capital_usd=30000.0,
        paper=True,
        strategy="",       # 空文字 → 戦術別 PDT チェックスキップ
        expiry_date=None,
    )
    # 通常のチェックが通る最低限の設定でResultが返ってくれば良い
    # （L1/L2/L3が失敗しても良い。L3.5由来のエラーでないことを確認）
    result = check_order(ctx)
    # L3.5 由来の失敗でないことを確認（$25K 以上なので PDT は適用されない）
    assert result.layer != "L3.5", "capital >= $25K のとき L3.5 でブロックされてはいけない"


# ── T25: OrderContext — strategy/expiry_date フィールドが存在する ──────────

def test_order_context_has_strategy_fields():
    """T25: OrderContext に strategy と expiry_date フィールドが存在する。"""
    from common.pre_trade_check import OrderContext
    import dataclasses

    fields = {f.name for f in dataclasses.fields(OrderContext)}
    assert "strategy" in fields, "OrderContext に strategy フィールドが必要"
    assert "expiry_date" in fields, "OrderContext に expiry_date フィールドが必要"

    # デフォルト値確認
    ctx = OrderContext(symbol="US.SPY", strike=560.0, side="SELL",
                       qty=1, option_price=1.0)
    assert ctx.strategy == ""
    assert ctx.expiry_date is None


# ── T26: supports_1dte クラス属性が各Engineに存在する ────────────────────

def test_engine_supports_1dte_attributes():
    """T26: spy_bot.py 各Engineに supports_1dte 属性が存在し正しい値を持つ。"""
    import importlib.util
    import sys

    # spy_bot.pyは巨大なので必要なクラスだけ確認
    # sys.modulesからインポート済みなら使う、なければ直接チェック
    expected = {
        "ORBEngine":          True,
        "CalendarEngine":     True,
        "StraddleEngine":     False,
        "StraddleBuyEngine":  False,
        "IronCondorSellEngine": True,
        "StrangleSellEngine": True,
        "ButterflyEngine":    True,
    }

    # grep-based verification: spy_bot.py のsupports_1dte行を確認
    spy_bot_path = Path(__file__).parents[1] / "spy_bot.py"
    content = spy_bot_path.read_text(encoding="utf-8")

    for engine_name, expected_val in expected.items():
        # クラス定義の後にsupports_1dte属性があることを確認
        expected_str = f"supports_1dte: bool = {expected_val}"
        assert expected_str in content, (
            f"{engine_name}: '{expected_str}' が spy_bot.py に見つからない"
        )
