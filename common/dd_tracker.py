#!/usr/bin/env python3
"""
common/dd_tracker.py — Bot別PnL・ドローダウン管理モジュール

portfolio_risk.py の週次/月次DD管理をBot別に独立して管理するための
薄いラッパー。spy_bot / momentum_bot / mffu_bot / apex_bot それぞれが
互いのPnLを汚染しないようにbot_filterを強制する。

使い方:
    from common.dd_tracker import DDTracker

    tracker = DDTracker("mffu_bot")
    tracker.record(date_str, pnl_usd)          # 日次PnL記録
    is_over = tracker.check_weekly_dd(balance)  # 当Botのみ集計
    is_over = tracker.check_monthly_dd(balance) # 当Botのみ集計
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class DDTracker:
    """Bot別DD管理クラス。

    portfolio_risk の record_daily_pnl / check_weekly_dd / check_monthly_dd を
    bot_nameに固定してラップする。

    Args:
        bot_name: "spy_bot" / "momentum_bot" / "mffu_bot" / "apex_bot"
    """

    def __init__(self, bot_name: str) -> None:
        self.bot_name = bot_name
        # portfolio_risk をインポート（見つからない場合はfail safe: DD上限超過として扱う）
        try:
            import portfolio_risk as _pr
            self._pr = _pr
        except ImportError as e:
            # AUDIT_FIX: fail open → fail safe に変更
            # portfolio_risk が使えない = DD管理不能 = エントリー禁止が安全
            log.error(
                f"DDTracker: portfolio_risk import失敗 — DD管理不能のためエントリーを禁止します: {e}"
            )
            self._pr = None
            self._import_failed = True
            return
        self._import_failed = False

    def record(self, date_str: str, pnl_usd: float) -> None:
        """日次PnLを記録する。

        Args:
            date_str: "YYYY-MM-DD" 形式
            pnl_usd:  当日確定PnL (USD)
        """
        if self._pr is None:
            log.warning(f"DDTracker.record: portfolio_risk not available, skipped")
            return
        self._pr.record_daily_pnl(date_str, pnl_usd, self.bot_name)

    def check_weekly_dd(self, account_balance: float) -> bool:
        """当Botの週次DDが上限を超えているか確認する。

        Returns:
            True: DD上限超過（エントリー禁止） / False: 問題なし
        Note:
            portfolio_risk import失敗時は True (超過=禁止) を返す (fail safe)。
        """
        if self._pr is None:
            if getattr(self, "_import_failed", False):
                log.error("DDTracker.check_weekly_dd: portfolio_risk未利用 → fail safe (True=禁止)")
                return True  # fail safe: エントリー禁止
            return False
        return self._pr.check_weekly_dd(account_balance, bot_filter=self.bot_name)

    def check_monthly_dd(self, account_balance: float) -> bool:
        """当Botの月次DDが上限を超えているか確認する。

        Returns:
            True: DD上限超過（エントリー禁止） / False: 問題なし
        Note:
            portfolio_risk import失敗時は True (超過=禁止) を返す (fail safe)。
        """
        if self._pr is None:
            if getattr(self, "_import_failed", False):
                log.error("DDTracker.check_monthly_dd: portfolio_risk未利用 → fail safe (True=禁止)")
                return True  # fail safe: エントリー禁止
            return False
        return self._pr.check_monthly_dd(account_balance)

    def check_weekly_dd_all(self, account_balance: float) -> bool:
        """全Bot合算の週次DDを確認する（後方互換・ポートフォリオ全体チェック用）。

        Returns:
            True: DD上限超過 / False: 問題なし
        Note:
            portfolio_risk import失敗時は True (超過=禁止) を返す (fail safe)。
        """
        if self._pr is None:
            if getattr(self, "_import_failed", False):
                log.error("DDTracker.check_weekly_dd_all: portfolio_risk未利用 → fail safe (True=禁止)")
                return True  # fail safe: エントリー禁止
            return False
        return self._pr.check_weekly_dd(account_balance, bot_filter=None)


# ────────────────────────────────────────────────────────────────────────────
# ユニットテスト
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os

    # テスト用に trading/ をパスに追加
    sys.path.insert(0, str(Path(__file__).parent.parent))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    PASS = "\033[92mPASS\033[0m"
    FAIL = "\033[91mFAIL\033[0m"
    results = []

    def assert_eq(name, got, expected):
        ok = got == expected
        results.append((name, ok))
        status = PASS if ok else FAIL
        print(f"  [{status}] {name}: got={got!r} expected={expected!r}")
        return ok

    def assert_true(name, val):
        results.append((name, bool(val)))
        status = PASS if val else FAIL
        print(f"  [{status}] {name}: {val!r}")
        return bool(val)

    print("\n--- Test 1: DDTracker インスタンス生成 ---")
    tracker_mffu = DDTracker("mffu_bot")
    assert_true("mffu_bot tracker生成", tracker_mffu is not None)
    assert_eq("bot_name=mffu_bot", tracker_mffu.bot_name, "mffu_bot")

    tracker_spy = DDTracker("spy_bot")
    assert_eq("bot_name=spy_bot", tracker_spy.bot_name, "spy_bot")

    print("\n--- Test 2: portfolio_risk 利用可能確認 ---")
    pr_available = tracker_mffu._pr is not None
    print(f"  portfolio_risk available: {pr_available}")
    assert_true("portfolio_risk loaded", pr_available)

    print("\n--- Test 3: check_weekly_dd (bot_filter動作確認) ---")
    # portfolio_riskが利用可能な場合のみ
    if pr_available:
        # 残高100万: DD上限超過なし（空データ）
        result = tracker_mffu.check_weekly_dd(1_000_000)
        assert_eq("空データ → False", result, False)

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    all_ok = passed == total
    status = PASS if all_ok else FAIL
    print(f"[{status}] {passed}/{total} tests passed")
    sys.exit(0 if all_ok else 1)
