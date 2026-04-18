"""tests/test_pdt_tracker.py — PDTTracker 単体テスト（20件以上）

テスト対象:
  common/pdt_tracker.py

カバー範囲:
  - Empty初期状態
  - 同日open+close → day_trade計上
  - 日跨ぎopen+close → 計上なし
  - 5営業日ローリング境界テスト
  - $25K以上は無制限
  - $25K未満で4回目ブロック
  - 週末跨ぎ（月曜に遡って金曜を含む）
  - ファイル永続化・再起動復元
  - race condition（複数close同時）
  - get_status() 各フィールド
  - ETタイムゾーン厳密化
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import threading
from pathlib import Path

# テスト用にプロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zoneinfo
ET = zoneinfo.ZoneInfo("America/New_York")

from common.pdt_tracker import PDTTracker, PDT_LIMIT, PDT_THRESHOLD_USD

# ── テスト用ヘルパー ───────────────────────────────────────────────────────────

def _et(year, month, day, hour=10, minute=30, second=0) -> datetime.datetime:
    """ET aware datetime を生成する。"""
    return datetime.datetime(year, month, day, hour, minute, second,
                             tzinfo=ET)


def _make_tracker(tmp_dir) -> PDTTracker:
    """一時ディレクトリに JSONL を持つ Tracker を生成する。"""
    return PDTTracker(data_file=Path(tmp_dir) / "pdt_day_trades.jsonl")


# ── テストスイート ─────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> bool:
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}")
    results.append((name, cond))
    return cond


def run_all_tests():
    # ─────────────────────────────────────────────
    print("\n=== T01: Empty初期状態 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        check("count=0（空ファイル）", t.count_day_trades_rolling() == 0)
        check("remaining=3（$25K未満）", t.remaining_allowed(8000) == 3)
        check("can_enter=True（$25K未満・空）", t.can_enter_new_day_trade(8000) is True)

    # ─────────────────────────────────────────────
    print("\n=== T02: 同日open+close → day_trade計上 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        entry = _et(2026, 4, 17, 10, 30)  # 金曜
        exit_ = _et(2026, 4, 17, 14, 0)
        result = t.record_round_trip("US.SPY", entry, exit_, "CS")
        check("同日 → True返却", result is True)
        check("count=1", t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17)) == 1)

    # ─────────────────────────────────────────────
    print("\n=== T03: 日跨ぎopen+close → 計上なし ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        entry = _et(2026, 4, 16, 15, 30)  # 木曜
        exit_ = _et(2026, 4, 17, 9, 45)   # 金曜
        result = t.record_round_trip("US.SPY", entry, exit_, "CS")
        check("日跨ぎ → False返却", result is False)
        check("count=0（未計上）", t.count_day_trades_rolling() == 0)

    # ─────────────────────────────────────────────
    print("\n=== T04: 5営業日ローリング境界テスト ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # 4/13(月)〜4/17(金) → 5営業日
        # 4/18(土)・4/19(日) → 週末なので営業日ウィンドウに入らない
        dates_in_window = [
            (2026, 4, 13), (2026, 4, 14), (2026, 4, 15), (2026, 4, 16), (2026, 4, 17)
        ]
        date_outside = (2026, 4, 7)  # 火曜だが4/17基準の5営業日ウィンドウ外

        for y, m, d in dates_in_window:
            t.record_round_trip("US.SPY", _et(y, m, d, 10), _et(y, m, d, 14), "CS")

        # 4/7(火)はウィンドウ外
        t.record_round_trip("US.SPY", _et(*date_outside, 10), _et(*date_outside, 14), "MANUAL")

        count = t.count_day_trades_rolling(days=5, reference=datetime.date(2026, 4, 17))
        check("ウィンドウ内5件がカウント", count == 5)
        count_outer = t.count_day_trades_rolling(days=5, reference=datetime.date(2026, 4, 14))
        # 4/14(火)基準の5営業日: 4/8(水)・4/9(木)・4/10(金)・4/13(月)・4/14(火)
        # 4/7は含まれない。4/13と4/14の2件のみ含まれる
        check("4/14基準では4/7(火)は含まれない", count_outer == 2)

    # ─────────────────────────────────────────────
    print("\n=== T05: $25K以上は無制限 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # 4件記録
        for i in range(4):
            d = 14 + i  # 月〜木
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "IC")
        rem = t.remaining_allowed(25_000.0)
        check("$25K exactly → unlimited", rem == float("inf"))
        rem2 = t.remaining_allowed(100_000.0)
        check("$100K → unlimited", rem2 == float("inf"))
        can = t.can_enter_new_day_trade(25_000.0)
        check("$25K → can_enter=True", can is True)

    # ─────────────────────────────────────────────
    print("\n=== T06: $25K未満で4回目ブロック ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        ref = datetime.date(2026, 4, 18)
        # 月〜水で3件消費
        for d in [14, 15, 16]:
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "CS")
        count = t.count_day_trades_rolling(reference=ref)
        check("3件消費後 count=3", count == 3)
        rem = t.remaining_allowed(8000.0)
        check("3件消費後 remaining=0", rem == 0)
        can = t.can_enter_new_day_trade(8000.0)
        check("3件消費後 can_enter=False（4回目ブロック）", can is False)

    # ─────────────────────────────────────────────
    print("\n=== T07: $25K未満で2件消費・残1 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        for d in [14, 15]:
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "ORB")
        rem = t.remaining_allowed(8000.0)
        check("2件消費後 remaining=1", rem == 1)
        can = t.can_enter_new_day_trade(8000.0)
        check("2件消費後 can_enter=True", can is True)

    # ─────────────────────────────────────────────
    print("\n=== T08: 週末跨ぎ（月曜に金曜を含む） ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # 4/13(月)〜4/17(金) に各1件 + 4/12(日)に1件
        # 4/13: 月, 4/14: 火, 4/15: 水, 4/16: 木, 4/17: 金 → 5営業日
        # 4/12: 日曜 → 営業日外
        for d in [13, 14, 15, 16, 17]:
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "Straddle")
        count_from_fri = t.count_day_trades_rolling(
            days=5, reference=datetime.date(2026, 4, 17)
        )
        # 4/17基準5営業日: 4/13, 4/14, 4/15, 4/16, 4/17 → 5件
        check("4/17(金)基準で5件", count_from_fri == 5)
        # 翌週 4/20(月)基準: 4/14, 4/15, 4/16, 4/17, 4/20 → 4件（4/13は除外）
        count_from_next_mon = t.count_day_trades_rolling(
            days=5, reference=datetime.date(2026, 4, 20)
        )
        check("4/20(月)基準で4件（4/13除外）", count_from_next_mon == 4)
        # 週末（4/18土, 4/19日）に記録してもカウントされない
        t.record_round_trip("US.SPY", _et(2026, 4, 18, 10), _et(2026, 4, 18, 14), "Manual")
        count_after = t.count_day_trades_rolling(
            days=5, reference=datetime.date(2026, 4, 20)
        )
        check("土曜のレコードは5営業日ウィンドウに含まれない", count_after == 4)

    # ─────────────────────────────────────────────
    print("\n=== T09: ファイル永続化・再起動復元 ===")
    with tempfile.TemporaryDirectory() as tmp:
        fpath = Path(tmp) / "pdt_day_trades.jsonl"
        # 1件記録
        t1 = PDTTracker(data_file=fpath)
        t1.record_round_trip("US.SPY", _et(2026, 4, 17, 10), _et(2026, 4, 17, 14), "CS")
        check("ファイル存在確認", fpath.exists())
        # 新インスタンスで復元
        t2 = PDTTracker(data_file=fpath)
        count = t2.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("再起動後もcount=1", count == 1)

    # ─────────────────────────────────────────────
    print("\n=== T10: 複数銘柄・戦術の合算 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        ref = datetime.date(2026, 4, 17)
        combos = [
            ("US.SPY", "CS"),
            ("US.QQQ", "IC"),
            ("US.SPY", "ORB"),
        ]
        for sym, strat in combos:
            t.record_round_trip(sym, _et(2026, 4, 17, 10), _et(2026, 4, 17, 14), strat)
        count = t.count_day_trades_rolling(reference=ref)
        check("CS+IC+ORBの合算=3", count == 3)

    # ─────────────────────────────────────────────
    print("\n=== T11: ETタイムゾーン厳密化 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # JST naive datetime を渡した場合: UTC との差異テスト
        # JST 2026-04-18 01:00 = ET 2026-04-17 12:00（夏時間 -13h）
        # naive として渡すと ET として解釈 → 4/17に計上
        naive_entry = datetime.datetime(2026, 4, 17, 10, 30, 0)  # naive → ET解釈
        naive_exit  = datetime.datetime(2026, 4, 17, 14, 0, 0)
        result = t.record_round_trip("US.SPY", naive_entry, naive_exit, "NAIVE_TEST")
        check("naive datetime → ETとして解釈・計上", result is True)

    # ─────────────────────────────────────────────
    print("\n=== T12: get_status() フィールド検証 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        ref = datetime.date(2026, 4, 17)
        t.record_round_trip("US.SPY", _et(2026, 4, 17, 10), _et(2026, 4, 17, 14), "CS")
        status = t.get_status(8000.0)
        check("pdt_constrained=True", status["pdt_constrained"] is True)
        check("rolling5_count=1", status["rolling5_count"] == 1)
        check("pdt_limit=3", status["pdt_limit"] == 3)
        check("pdt_remaining=2", status["pdt_remaining"] == 2)
        check("can_enter=True", status["can_enter"] is True)
        check("business_days_window has 5 entries", len(status["business_days_window"]) == 5)

    # ─────────────────────────────────────────────
    print("\n=== T13: get_status() $25K以上 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        for d in [14, 15, 16, 17, 18]:
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "IC")
        status = t.get_status(25_001.0)
        check("pdt_constrained=False ($25K+)", status["pdt_constrained"] is False)
        check("pdt_remaining=unlimited ($25K+)", status["pdt_remaining"] == "unlimited")
        check("can_enter=True ($25K+)", status["can_enter"] is True)

    # ─────────────────────────────────────────────
    print("\n=== T14: race condition（複数スレッド同時書込） ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        errors: list[Exception] = []

        def _write(idx: int):
            try:
                d = 14 + (idx % 5)
                h = 10 + (idx % 4)
                t.record_round_trip(
                    "US.SPY",
                    _et(2026, 4, d, h, 0),
                    _et(2026, 4, d, h, 30),
                    f"RACE_{idx}",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        check("race condition: エラーなし", len(errors) == 0)
        # JSONL が破損していないか確認（全行パース可能）
        lines = t.data_file.read_text().strip().split("\n")
        valid_lines = [l for l in lines if l.strip()]
        check("全行が有効なJSON", all(_is_valid_json(l) for l in valid_lines))

    # ─────────────────────────────────────────────
    print("\n=== T15: Early close day（半日取引）は通常通りカウント ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # 半日取引日（例: 感謝祭翌日）でも同日open+closeなら計上
        t.record_round_trip(
            "US.SPY",
            _et(2026, 11, 27, 10, 0),   # 金曜（感謝祭翌日）
            _et(2026, 11, 27, 12, 59),  # 13:00ET close前
            "CS",
        )
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 11, 27))
        check("半日取引日も1件としてカウント", count == 1)

    # ─────────────────────────────────────────────
    print("\n=== T16: JSONL 形式の整合性確認 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        t.record_round_trip("US.SPY", _et(2026, 4, 17, 10), _et(2026, 4, 17, 14), "IC")
        lines = t.data_file.read_text().strip().split("\n")
        check("1行追記", len([l for l in lines if l.strip()]) == 1)
        import json
        record = json.loads(lines[0])
        check("date フィールド存在", "date" in record)
        check("symbol フィールド存在", "symbol" in record)
        check("strategy フィールド存在", "strategy" in record)
        check("entry_time フィールド存在", "entry_time" in record)
        check("exit_time フィールド存在", "exit_time" in record)

    # ─────────────────────────────────────────────
    print("\n=== T17: JSONL 破損行耐性 ===")
    with tempfile.TemporaryDirectory() as tmp:
        fpath = Path(tmp) / "pdt_day_trades.jsonl"
        # 正常1行 + 破損1行 + 正常1行
        import json as _json
        with open(fpath, "w") as f:
            f.write(_json.dumps({"date": "2026-04-17", "symbol": "US.SPY", "strategy": "CS",
                                 "entry_time": "2026-04-17T10:00:00", "exit_time": "2026-04-17T14:00:00",
                                 "is_business_day": True}) + "\n")
            f.write("CORRUPTED_LINE\n")
            f.write(_json.dumps({"date": "2026-04-17", "symbol": "US.QQQ", "strategy": "IC",
                                 "entry_time": "2026-04-17T10:30:00", "exit_time": "2026-04-17T14:30:00",
                                 "is_business_day": True}) + "\n")
        t = PDTTracker(data_file=fpath)
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("破損行をスキップして正常行2件を集計", count == 2)

    # ─────────────────────────────────────────────
    print("\n=== T18: 0件→3件→ブロック→翌週リセット確認 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        # 4/13(月)・4/14(火)・4/15(水) で3件消費
        for d in [13, 14, 15]:
            t.record_round_trip("US.SPY", _et(2026, 4, d, 10), _et(2026, 4, d, 14), "CS")
        can_thu = t.can_enter_new_day_trade(8000.0)
        check("第1週木曜はブロック（3件消費済）", can_thu is False)
        # 翌週 4/20(月)基準の5営業日: 4/14(火), 4/15(水), 4/16(木), 4/17(金), 4/20(月)
        # → 4/13(月)は含まれない → count=2 (4/14, 4/15のみ)
        count_next_week = t.count_day_trades_rolling(
            days=5, reference=datetime.date(2026, 4, 20)
        )
        check("翌週月曜基準: 4/13(月)はウィンドウ外 → count=2", count_next_week == 2)

    # ─────────────────────────────────────────────
    print("\n=== T19: DeltaHedge戦術も計上確認 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        t.record_round_trip("US.SPY", _et(2026, 4, 17, 10), _et(2026, 4, 17, 14), "DeltaHedge")
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check("DeltaHedge戦術も合算カウント", count == 1)

    # ─────────────────────────────────────────────
    print("\n=== T20: 複数戦術が同一営業日に混在 ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        strategies = ["CS", "IC", "ORB", "Butterfly", "Calendar", "Strangle", "DeltaHedge"]
        for strat in strategies:
            t.record_round_trip("US.SPY", _et(2026, 4, 17, 10), _et(2026, 4, 17, 14), strat)
        count = t.count_day_trades_rolling(reference=datetime.date(2026, 4, 17))
        check(f"7戦術合算={count}", count == 7)
        rem = t.remaining_allowed(8000.0)
        check("7件消費 → remaining=0（上限超過）", rem == 0)

    # ─────────────────────────────────────────────
    print("\n=== T21: remaining_allowed 境界値テスト ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = _make_tracker(tmp)
        ref = datetime.date(2026, 4, 17)
        # 0件
        check("0件 → remaining=3", t.remaining_allowed(8000.0) == 3)
        t.record_round_trip("US.SPY", _et(2026, 4, 17, 10), _et(2026, 4, 17, 10, 30), "CS")
        check("1件 → remaining=2", t.remaining_allowed(8000.0) == 2)
        t.record_round_trip("US.SPY", _et(2026, 4, 17, 11), _et(2026, 4, 17, 11, 30), "IC")
        check("2件 → remaining=1", t.remaining_allowed(8000.0) == 1)
        t.record_round_trip("US.SPY", _et(2026, 4, 17, 12), _et(2026, 4, 17, 12, 30), "ORB")
        check("3件 → remaining=0", t.remaining_allowed(8000.0) == 0)
        # $24,999
        check("$24,999 → constrained", t.remaining_allowed(24_999.0) == 0)
        # $25,000
        check("$25,000 → unlimited", t.remaining_allowed(25_000.0) == float("inf"))


# ── ユーティリティ ──────────────────────────────────────────────────────────────

def _is_valid_json(s: str) -> bool:
    import json
    try:
        json.loads(s)
        return True
    except Exception:
        return False


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

    sys.exit(0 if all_ok else 1)
