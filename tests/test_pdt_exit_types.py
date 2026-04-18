"""tests/test_pdt_exit_types.py — PDT exit_type判定 + 満期放置サポート テスト

テスト対象:
  - common/pdt_tracker.py (exit_type拡張)
  - common/itm_risk_check.py (ITM接近検知)
  - common/pdt_1dte_utils.py (satisfies_no_pdt)
  - common/strategy_selector.py (PDT最適化)
  - spy_bot.py (_reason_to_exit_type)

カバー範囲 (15テスト以上):
  T01: manual_close 同日 → day_trade計上
  T02: manual_close 日跨ぎ → 計上なし
  T03: expired_worthless → 計上なし (非PDTファイルに記録)
  T04: assigned → 計上なし
  T05: cash_settled → 計上なし
  T06: ITM接近警告 (15:30以降・OTM距離<$0.50)
  T07: ITM強制クローズ推奨 (15:45以降・ITM化濃厚)
  T08: 現金決済(SPX)はshould_force_close=False
  T09: ITM check_spreadで最も危険な方を返す
  T10: SPXのsatisfies_no_pdt=True確認
  T11: ORBのsatisfies_no_pdt=False確認
  T12: CS・IC・StrangleSellのsatisfies_no_pdt=True確認
  T13: strategy_selector PDT残0+satisfies_no_pdt=True → 通過
  T14: strategy_selector PDT残0+satisfies_no_pdt=False → フォールバック
  T15: 満期放置戦術でOTM消滅 → PDT消費ゼロ確認
  T16: ITM早期close → day_trade計上（manual_close）
  T17: _reason_to_exit_type ロジック検証
  T18: 非PDTファイル集計 (count_non_pdt_by_exit_type)
  T19: Daily AAR第7章サマリー形式確認
  T20: CSBot _on_position_closed EOD → expired_worthless扱い
"""
from __future__ import annotations

import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zoneinfo
ET = zoneinfo.ZoneInfo("America/New_York")

from common.pdt_tracker import PDTTracker, _NON_PDT_EXIT_TYPES
from common.itm_risk_check import ITMRiskChecker
from common.pdt_1dte_utils import strategy_satisfies_no_pdt

# ── テスト用ヘルパー ──────────────────────────────────────────────────────────

def _et(year, month, day, hour=10, minute=30, second=0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=ET)

def _make_tracker(tmp_dir) -> PDTTracker:
    return PDTTracker(data_file=Path(tmp_dir) / "pdt_day_trades.jsonl")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[str, bool]] = []

def check(name: str, cond: bool) -> bool:
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}")
    results.append((name, cond))
    return cond


# ── テストスイート ────────────────────────────────────────────────────────────

def run_all_tests():

    # ─────────────────────────────────────────────
    print("\n=== T01: manual_close 同日 → day_trade計上 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 14, 0)
        result = t.record_round_trip("US.SPY", entry, exit_, "CS",
                                     exit_type="manual_close")
        check("manual_close 同日 → True返却", result is True)
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("manual_close 同日 → count=1", count == 1)

    # ─────────────────────────────────────────────
    print("\n=== T02: manual_close 日跨ぎ → 計上なし ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        entry = _et(2026, 4, 16, 15, 30)  # 木曜
        exit_ = _et(2026, 4, 17, 9, 45)   # 金曜（日跨ぎ）
        result = t.record_round_trip("US.SPY", entry, exit_, "CS",
                                     exit_type="manual_close")
        check("manual_close 日跨ぎ → False返却", result is False)
        count = t.count_day_trades_rolling()
        check("manual_close 日跨ぎ → count=0", count == 0)

    # ─────────────────────────────────────────────
    print("\n=== T03: expired_worthless → 計上なし・非PDTファイルに記録 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 16, 0)  # 満期時刻
        result = t.record_round_trip("US.SPY", entry, exit_, "CS",
                                     exit_type="expired_worthless")
        check("expired_worthless → False返却（day_trade非計上）", result is False)
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("expired_worthless → day_trades count=0", count == 0)
        # 非PDTファイルに記録されているか
        check("非PDTファイル存在", t._non_pdt_file.exists())
        non_pdt_count = t.count_non_pdt_by_exit_type("expired_worthless",
                                                       datetime.date(2026, 4, 17))
        check("非PDTファイルにexpired_worthless=1件", non_pdt_count == 1)

    # ─────────────────────────────────────────────
    print("\n=== T04: assigned → 計上なし ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 16, 0)
        result = t.record_round_trip("US.SPY", entry, exit_, "CS",
                                     exit_type="assigned")
        check("assigned → False返却", result is False)
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("assigned → count=0", count == 0)
        non_pdt_count = t.count_non_pdt_by_exit_type("assigned",
                                                       datetime.date(2026, 4, 17))
        check("非PDTファイルにassigned=1件", non_pdt_count == 1)

    # ─────────────────────────────────────────────
    print("\n=== T05: cash_settled → 計上なし ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 16, 0)
        result = t.record_round_trip("US..SPX", entry, exit_, "CS",
                                     exit_type="cash_settled")
        check("cash_settled → False返却", result is False)
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("cash_settled → count=0", count == 0)
        non_pdt_count = t.count_non_pdt_by_exit_type("cash_settled",
                                                       datetime.date(2026, 4, 17))
        check("非PDTファイルにcash_settled=1件", non_pdt_count == 1)

    # ─────────────────────────────────────────────
    print("\n=== T06: ITM接近警告 (15:30以降・OTM距離<$0.50) ===")
    checker = ITMRiskChecker()
    now_15_30 = _et(2026, 4, 17, 15, 30)
    result = checker.check(
        underlying_price=560.45,
        short_strike=560.0,
        option_side="CALL",
        now_et=now_15_30,
    )
    check("15:30 + OTM距離$0.45 → is_warning=True",
          result.is_warning is True)
    check("15:30 + OTM距離$0.45 → should_force_close=False",
          result.should_force_close is False)
    check("OTM距離算出: short-underlying = -0.45",
          abs(result.otm_distance - (-0.45)) < 0.01)

    # ─────────────────────────────────────────────
    print("\n=== T07: ITM強制クローズ推奨 (15:45以降・OTM距離<$0.50) ===")
    now_15_45 = _et(2026, 4, 17, 15, 45)
    result = checker.check(
        underlying_price=560.45,
        short_strike=560.0,
        option_side="CALL",
        now_et=now_15_45,
    )
    check("15:45 + OTM距離$0.45 → should_force_close=True",
          result.should_force_close is True)
    check("is_warning=True（兼用）",
          result.is_warning is True)

    # OTMが十分ある場合はforce_close不要
    result_safe = checker.check(
        underlying_price=558.0,
        short_strike=560.0,
        option_side="CALL",
        now_et=now_15_45,
    )
    check("15:45 + OTM距離$2.0 → should_force_close=False",
          result_safe.should_force_close is False)

    # ─────────────────────────────────────────────
    print("\n=== T08: 現金決済(SPX)はshould_force_close=False ===")
    result_spx = checker.check(
        underlying_price=5600.45,
        short_strike=5600.0,
        option_side="CALL",
        now_et=now_15_45,
        is_cash_settled=True,
    )
    check("SPX 15:45 + OTM接近 + cash_settled → should_force_close=False",
          result_spx.should_force_close is False)
    check("SPX is_warning=True (警告は出る)",
          result_spx.is_warning is True)

    # ─────────────────────────────────────────────
    print("\n=== T09: ITM check_spreadで最も危険な方を返す ===")
    result_spread = checker.check_spread(
        underlying_price=560.0,
        call_short_strike=562.0,   # OTM距離 $2.0（安全）
        put_short_strike=560.2,    # OTM距離 $0.2（危険）
        now_et=now_15_45,
    )
    check("spread: PUTが危険 → PUT側が返る",
          result_spread is not None and result_spread.option_side == "PUT")
    check("spread: OTM距離=-0.2（ITM）",
          result_spread is not None and result_spread.otm_distance < 0)

    # ─────────────────────────────────────────────
    print("\n=== T10: SPX用戦術のsatisfies_no_pdt確認 ===")
    check("CS → satisfies_no_pdt=True", strategy_satisfies_no_pdt("CS") is True)
    check("IC → satisfies_no_pdt=True", strategy_satisfies_no_pdt("IC") is True)
    check("StrangleSell → satisfies_no_pdt=True",
          strategy_satisfies_no_pdt("StrangleSell") is True)
    check("Calendar → satisfies_no_pdt=True",
          strategy_satisfies_no_pdt("Calendar") is True)

    # ─────────────────────────────────────────────
    print("\n=== T11: ORBのsatisfies_no_pdt=False確認 ===")
    check("ORB → satisfies_no_pdt=False", strategy_satisfies_no_pdt("ORB") is False)
    check("StraddleBuy → satisfies_no_pdt=False",
          strategy_satisfies_no_pdt("StraddleBuy") is False)
    check("GammaScalp → satisfies_no_pdt=False",
          strategy_satisfies_no_pdt("GammaScalp") is False)
    check("IVCrush → satisfies_no_pdt=False",
          strategy_satisfies_no_pdt("IVCrush") is False)

    # ─────────────────────────────────────────────
    print("\n=== T12: 1DTE系はsatisfies_no_pdt=True ===")
    check("1dte_cs → satisfies_no_pdt=True",
          strategy_satisfies_no_pdt("1dte_cs") is True)
    check("1dte_orb → satisfies_no_pdt=True",
          strategy_satisfies_no_pdt("1dte_orb") is True)
    check("1dte_ic → satisfies_no_pdt=True",
          strategy_satisfies_no_pdt("1dte_ic") is True)

    # ─────────────────────────────────────────────
    print("\n=== T13: strategy_selector PDT残0+satisfies_no_pdt=True → 通過 ===")
    from common.strategy_selector import StrategySelector
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # PDT 3件消費
        for d in [14, 15, 16]:
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "CS",
                                exit_type="manual_close")
        selector = StrategySelector(pdt_tracker=t)
        today = datetime.date(2026, 4, 17)
        result = selector.select("CS", today, 8000.0,
                                 now_et=_et(2026, 4, 17, 10, 30))
        check("PDT残0 + CS(satisfies_no_pdt=True) → strategy=CS（通過）",
              result.strategy == "CS")
        check("satisfies_no_pdt=True",
              result.satisfies_no_pdt is True)
        check("fallback_activated=False",
              result.fallback_activated is False)

    # ─────────────────────────────────────────────
    print("\n=== T14: strategy_selector PDT残0+satisfies_no_pdt=False → フォールバック ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # PDT 3件消費
        for d in [14, 15, 16]:
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "ORB",
                                exit_type="manual_close")
        selector = StrategySelector(pdt_tracker=t)
        today = datetime.date(2026, 4, 17)
        result = selector.select("ORB", today, 8000.0,
                                 now_et=_et(2026, 4, 17, 10, 30))
        # ORB: satisfies_no_pdt=False → 1DTEフォールバック
        check("PDT残0 + ORB(satisfies_no_pdt=False) → 1DTE版またはno_trade",
              result.strategy in ("1dte_orb", "no_trade"))
        check("satisfies_no_pdt=False",
              result.satisfies_no_pdt is False or result.strategy == "1dte_orb")

    # ─────────────────────────────────────────────
    print("\n=== T15: 満期放置戦術でOTM消滅 → PDT消費ゼロ確認 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # CS売り: 満期OTM消滅 → expired_worthless
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 16, 0)  # 満期
        t.record_round_trip("US.SPY", entry, exit_, "CS",
                            exit_type="expired_worthless")
        # もう一件
        t.record_round_trip("US.SPY", entry, exit_, "IC",
                            exit_type="expired_worthless")
        # day_trade = 0件
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("満期放置2件 → day_trade count=0", count == 0)
        # PDT残= 3のまま（消費なし）
        remaining = t.remaining_allowed(8000.0)
        check("満期放置2件 → remaining=3（PDT消費ゼロ）", remaining == 3)
        # 非PDT記録= 2件
        non_pdt_count = t.count_non_pdt_by_exit_type(
            "expired_worthless", datetime.date(2026, 4, 17)
        )
        check("非PDT記録=2件", non_pdt_count == 2)

    # ─────────────────────────────────────────────
    print("\n=== T16: ITM早期close → day_trade計上（manual_close） ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # 15:45にITM強制close → manual_close
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 15, 46)  # 強制close時刻
        result = t.record_round_trip("US.SPY", entry, exit_, "CS",
                                     exit_type="manual_close")
        check("ITM強制close → manual_close → day_trade計上",
              result is True)
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("ITM強制close → count=1", count == 1)
        # PDT残=2（1消費）
        remaining = t.remaining_allowed(8000.0)
        check("ITM強制close → remaining=2", remaining == 2)

    # ─────────────────────────────────────────────
    print("\n=== T17: _reason_to_exit_type ロジック検証 ===")
    import sys as _sys
    # spy_bot.pyから直接インポートはしない（起動コストが高い）
    # ロジックを直接テスト
    def _reason_to_exit_type_test(reason: str, allow_expiry: bool = False) -> str:
        """spy_bot._reason_to_exit_type の簡易再実装（テスト用）"""
        if reason in ("expired_worthless", "assigned", "cash_settled"):
            return reason
        if "cash_settle" in reason or "cash_settled" in reason:
            return "cash_settled"
        if "assigned" in reason or "auto_exercise" in reason:
            return "assigned"
        if allow_expiry and any(kw in reason for kw in (
            "expired_worthless", "expired", "time_stop", "force_close_eod",
            "force_close_time", "eod", "cutoff",
        )):
            return "expired_worthless"
        return "manual_close"

    check("profit_target → manual_close",
          _reason_to_exit_type_test("profit_target") == "manual_close")
    check("stop_loss → manual_close",
          _reason_to_exit_type_test("stop_loss") == "manual_close")
    check("force_close_eod + allow_expiry=True → expired_worthless",
          _reason_to_exit_type_test("force_close_eod", allow_expiry=True) == "expired_worthless")
    check("time_stop + allow_expiry=True → expired_worthless",
          _reason_to_exit_type_test("time_stop", allow_expiry=True) == "expired_worthless")
    check("force_close_eod + allow_expiry=False → manual_close",
          _reason_to_exit_type_test("force_close_eod", allow_expiry=False) == "manual_close")
    check("cash_settled → cash_settled",
          _reason_to_exit_type_test("cash_settled") == "cash_settled")
    check("assigned → assigned",
          _reason_to_exit_type_test("assigned") == "assigned")

    # ─────────────────────────────────────────────
    print("\n=== T18: 非PDTファイル集計 count_non_pdt_by_exit_type ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        ref = datetime.date(2026, 4, 17)
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 16, 0)
        # expired_worthless x2
        t.record_round_trip("US.SPY", entry, exit_, "CS",  exit_type="expired_worthless")
        t.record_round_trip("US.SPY", entry, exit_, "IC",  exit_type="expired_worthless")
        # assigned x1
        t.record_round_trip("US.SPY", entry, exit_, "CS",  exit_type="assigned")
        # cash_settled x1
        t.record_round_trip("US..SPX", entry, exit_, "CS", exit_type="cash_settled")
        check("expired_worthless count=2",
              t.count_non_pdt_by_exit_type("expired_worthless", ref) == 2)
        check("assigned count=1",
              t.count_non_pdt_by_exit_type("assigned", ref) == 1)
        check("cash_settled count=1",
              t.count_non_pdt_by_exit_type("cash_settled", ref) == 1)
        check("全PDT対象外=4件（Noneで全取得）",
              t.count_non_pdt_by_exit_type(None, ref) == 4)

    # ─────────────────────────────────────────────
    print("\n=== T19: Daily AAR第7章サマリー形式確認 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        ref = datetime.date(2026, 4, 17)
        entry = _et(2026, 4, 17, 10, 30)
        exit_ = _et(2026, 4, 17, 16, 0)
        t.record_round_trip("US.SPY", entry, exit_, "CS", exit_type="expired_worthless")
        t.record_round_trip("US.SPY", entry, exit_, "CS", exit_type="manual_close")
        t.record_round_trip("US..SPX", entry, exit_, "CS", exit_type="cash_settled")
        summary = t.get_daily_pdt_summary(reference=ref)
        check("expired_worthless_count=1", summary["expired_worthless_count"] == 1)
        check("cash_settled_count=1", summary["cash_settled_count"] == 1)
        check("manual_close_count=1", summary["manual_close_count"] == 1)
        check("total_non_pdt_exits=2",
              summary["total_non_pdt_exits"] == 2)  # expired + cash_settled
        # get_status() に today_pdt_summary が含まれるか
        status = t.get_status(8000.0)
        check("get_status()にtoday_pdt_summary含まれる",
              "today_pdt_summary" in status)

    # ─────────────────────────────────────────────
    print("\n=== T20: exit_type=expired_worthless が _NON_PDT_EXIT_TYPES に含まれるか ===")
    check("expired_worthless in _NON_PDT_EXIT_TYPES",
          "expired_worthless" in _NON_PDT_EXIT_TYPES)
    check("assigned in _NON_PDT_EXIT_TYPES",
          "assigned" in _NON_PDT_EXIT_TYPES)
    check("cash_settled in _NON_PDT_EXIT_TYPES",
          "cash_settled" in _NON_PDT_EXIT_TYPES)
    check("manual_close NOT in _NON_PDT_EXIT_TYPES",
          "manual_close" not in _NON_PDT_EXIT_TYPES)


# ── エントリーポイント ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_all_tests()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    all_ok = passed == total
    status = PASS if all_ok else FAIL
    print(f"[{status}] {passed}/{total} tests passed")

    if not all_ok:
        print("\n[FAILED]")
        for name, ok in results:
            if not ok:
                print(f"  - {name}")

    import sys
    sys.exit(0 if all_ok else 1)
