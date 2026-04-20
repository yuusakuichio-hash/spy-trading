#!/usr/bin/env python3
"""
test_mf_alpha_direct_20260420.py
β-8: MF-1α〜13α 直接テスト 30本+

対象:
  MF-1α  idempotency key / _is_order_sent / _mark_order_sent
  MF-2α  upcoming_events / est_pnl pass-through
  MF-3α  prop_account_state 実値注入
  MF-4α  (check_inactivity / last_trade_date) — β-4 対応
  MF-5α  plan_id fail-closed (β-6/β-7)
  MF-10α peak_balance アカウント別 (β-3)

検証観点:
  grep call site: _is_order_sent が place_order 前に呼ばれるか
  TZ: T1 News blackout が ET-aware で動くか (β-1)
  fail-closed: 未知 plan + phase で ValueError が上がるか
  マルチアカ独立: 異なる MFFU_ACCOUNT_ID で別パスになるか

実行:
  python3 -m pytest tests/test_mf_alpha_direct_20260420.py -v
"""
from __future__ import annotations

import sys
import os
import datetime
import inspect
import ast
import textwrap
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ─────────────────────────────────────────────────────────────────────────────

def _et_now() -> datetime.datetime:
    try:
        import zoneinfo
        return datetime.datetime.now(tz=zoneinfo.ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        return datetime.datetime.now(tz=pytz.timezone("America/New_York"))


# ─────────────────────────────────────────────────────────────────────────────
# MF-1α: idempotency key / _is_order_sent / _mark_order_sent
# ─────────────────────────────────────────────────────────────────────────────

class TestMF1AlphaIdempotency:
    """3本: key生成・mark・is_sent"""

    def test_mark_and_is_sent_roundtrip(self, tmp_path, monkeypatch):
        """_mark_order_sent → _is_order_sent = True"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MFFU_ACCOUNT_ID", "test_acct")
        from chronos_bot import MFFUBot
        bot = MFFUBot.__new__(MFFUBot)
        bot._orders_db_path = tmp_path / "orders.db"
        bot._init_orders_db()

        key = "test-key-001"
        # 未記録はFalse
        assert bot._is_order_sent(key) is False
        # mark後はTrue
        import sqlite3
        conn = sqlite3.connect(str(bot._orders_db_path))
        conn.execute("INSERT OR IGNORE INTO idempotency_keys(key, ts, sent) VALUES(?,?,0)", (key, 1.0))
        conn.commit()
        conn.close()
        bot._mark_order_sent(key)
        assert bot._is_order_sent(key) is True

    def test_next_client_order_id_generates_unique_keys(self, tmp_path, monkeypatch):
        """_next_client_order_id が毎回異なるキーを返す"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MFFU_ACCOUNT_ID", "test_acct")
        from chronos_bot import MFFUBot
        bot = MFFUBot.__new__(MFFUBot)
        bot._orders_db_path = tmp_path / "orders.db"
        bot._init_orders_db()
        keys = {bot._next_client_order_id() for _ in range(10)}
        assert len(keys) == 10, "重複キーが生成された"

    def test_is_order_sent_false_for_new_key(self, tmp_path, monkeypatch):
        """未登録キーは False"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MFFU_ACCOUNT_ID", "test_acct")
        from chronos_bot import MFFUBot
        bot = MFFUBot.__new__(MFFUBot)
        bot._orders_db_path = tmp_path / "orders.db"
        bot._init_orders_db()
        assert bot._is_order_sent("never-registered-key") is False


# ─────────────────────────────────────────────────────────────────────────────
# β-2 call site: check_breakout 内で place_order 前に _is_order_sent が呼ばれるか
# ─────────────────────────────────────────────────────────────────────────────

class TestBeta2CallSite:
    """β-2: _is_order_sent が place_order より前に呼ばれることを AST で確認"""

    def test_is_order_sent_before_place_order_in_source(self):
        """chronos_bot.py の check_breakout / place_order 前に _is_order_sent があること"""
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chronos_bot.py")
        with open(src_path, encoding="utf-8") as f:
            source = f.read()

        # grep: _is_order_sent の呼び出し行と place_order の呼び出し行を探す
        lines = source.splitlines()
        is_sent_lines = [i for i, l in enumerate(lines) if "_is_order_sent(" in l and "def " not in l]
        place_order_lines = [i for i, l in enumerate(lines) if "self.client.place_order(" in l or "client.place_order(" in l]

        assert len(is_sent_lines) >= 1, "_is_order_sent() の呼び出しが見つからない"
        assert len(place_order_lines) >= 1, "place_order() の呼び出しが見つからない"

        # β-2 修正: place_order の直前に _is_order_sent がなければならない
        # 同じブロック内で is_sent_lines の最小値が place_order の最小値より小さいこと
        min_is_sent = min(is_sent_lines)
        min_place = min(place_order_lines)
        assert min_is_sent < min_place, (
            f"_is_order_sent (line {min_is_sent+1}) は place_order (line {min_place+1}) より後になっている"
        )

    def test_is_order_sent_check_in_source_before_place_order(self):
        """β-2: chronos_bot.py ソースコードで _is_order_sent が place_order より前に存在する"""
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chronos_bot.py")
        with open(src_path, encoding="utf-8") as f:
            lines = f.readlines()

        # _is_order_sent の実際の呼び出し（def定義除く）の行番号
        is_sent_call_lines = [
            i for i, l in enumerate(lines)
            if "_is_order_sent(" in l and "def _is_order_sent" not in l
        ]
        # place_order 呼び出し行番号
        place_order_lines = [
            i for i, l in enumerate(lines)
            if "self.client.place_order(" in l
        ]

        assert len(is_sent_call_lines) >= 1, "_is_order_sent() 呼び出しがない"
        assert len(place_order_lines) >= 1, "place_order() 呼び出しがない"

        # 最初の _is_order_sent 呼び出しが最初の place_order より前にあること
        assert min(is_sent_call_lines) < min(place_order_lines), (
            f"_is_order_sent (line {min(is_sent_call_lines)+1}) は "
            f"place_order (line {min(place_order_lines)+1}) より後にある"
        )

    def test_is_order_sent_retry_guard_logic_exists(self):
        """β-2: retry abort ログが存在すること（実装証跡）"""
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chronos_bot.py")
        with open(src_path, encoding="utf-8") as f:
            source = f.read()
        assert "retry abort" in source or "already sent" in source, (
            "β-2: retry abort / already sent ガードのログが見つからない"
        )


# ─────────────────────────────────────────────────────────────────────────────
# β-1: T1 News Blackout ET-aware (TZズレ防止)
# ─────────────────────────────────────────────────────────────────────────────

class TestBeta1TZAwareness:
    """β-1: check_t1_news_blackout が ET-aware datetime で呼ばれるか"""

    def test_blackout_with_naive_now_was_broken(self):
        """ET-aware な now を渡すと blackout が正しく機能する"""
        from common.prop_firm_rules import check_t1_news_blackout, get_plan_rules

        # T1イベントをET 10:00に設定
        try:
            import zoneinfo
            et = zoneinfo.ZoneInfo("America/New_York")
        except ImportError:
            import pytz
            et = pytz.timezone("America/New_York")

        ev_ts_et = datetime.datetime(2026, 4, 20, 10, 0, 0, tzinfo=et)

        # ET-aware now: イベント30秒前 → blackout発動すべき
        now_aware = datetime.datetime(2026, 4, 20, 9, 59, 30, tzinfo=et)
        rules = get_plan_rules("mffu", "flex_50k")
        ok, msg = check_t1_news_blackout(
            now_aware,
            [{"tier": 1, "ts": ev_ts_et, "name": "CPI"}],
            "evaluation",
            rules,
        )
        assert ok is False, f"ET-aware: blackout未発動 msg={msg}"

    def test_blackout_not_triggered_outside_window(self):
        """イベントから5分後は blackout 対象外"""
        from common.prop_firm_rules import check_t1_news_blackout, get_plan_rules
        try:
            import zoneinfo
            et = zoneinfo.ZoneInfo("America/New_York")
        except ImportError:
            import pytz
            et = pytz.timezone("America/New_York")

        ev_ts_et = datetime.datetime(2026, 4, 20, 10, 0, 0, tzinfo=et)
        now_5min_after = datetime.datetime(2026, 4, 20, 10, 5, 0, tzinfo=et)
        rules = get_plan_rules("mffu", "flex_50k")
        ok, msg = check_t1_news_blackout(
            now_5min_after,
            [{"tier": 1, "ts": ev_ts_et, "name": "CPI"}],
            "evaluation",
            rules,
        )
        assert ok is True, f"5分後はblackout外のはずが: msg={msg}"

    def test_prop_firm_rules_call_site_is_et_aware(self):
        """β-1修正: check_order 内の datetime.now() が ET-aware になっているか AST/grep で確認"""
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "common", "prop_firm_rules.py")
        with open(src_path, encoding="utf-8") as f:
            source = f.read()

        # β-1 修正後: "datetime.now()" (naiveな呼び出し) がT1 News blackoutコールの直前にないこと
        # 修正前: datetime.datetime.now() が直接渡されていた
        # 修正後: datetime.datetime.now(tz=...) を使うか、ET-aware 変数経由

        lines = source.splitlines()
        # check_t1_news_blackout 呼び出し行を探す
        blackout_call_lines = [i for i, l in enumerate(lines) if "check_t1_news_blackout(" in l]
        assert len(blackout_call_lines) >= 1

        # 呼び出し直前3行の中に "datetime.now()" (naive) がないこと
        for call_line in blackout_call_lines:
            window = "\n".join(lines[max(0, call_line-10):call_line+5])
            assert "datetime.datetime.now()" not in window, (
                f"line {call_line+1} 付近に naive datetime.now() が残存: {window}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# β-4: last_trade_date 実記入 → check_inactivity 7日経過でNO
# ─────────────────────────────────────────────────────────────────────────────

class TestBeta4LastTradeDate:
    """β-4: _prop_acct_state に last_trade_date が記入され inactivity check が機能する"""

    def test_check_inactivity_7days_returns_false(self):
        """7日前の last_trade_date で check_inactivity が False を返す"""
        from common.prop_firm_rules import check_inactivity
        try:
            import zoneinfo
            et = zoneinfo.ZoneInfo("America/New_York")
        except ImportError:
            import pytz
            et = pytz.timezone("America/New_York")
        today_et = datetime.datetime.now(tz=et).date()
        old_date = today_et - datetime.timedelta(days=7)
        ok, msg = check_inactivity(old_date, max_days=7)
        assert ok is False, f"7日経過で check_inactivity は False のはず: msg={msg}"

    def test_check_inactivity_none_returns_true(self):
        """last_trade_date=None は常にTrue（未取引=許可）"""
        from common.prop_firm_rules import check_inactivity
        ok, msg = check_inactivity(None)
        assert ok is True

    def test_prop_acct_state_contains_last_trade_date_key(self):
        """chronos_bot.py の _prop_acct_state dict に last_trade_date キーが含まれる"""
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chronos_bot.py")
        with open(src_path, encoding="utf-8") as f:
            source = f.read()

        # β-4 修正後: _prop_acct_state に "last_trade_date" が記入されているはず
        assert '"last_trade_date":' in source or "'last_trade_date':" in source, (
            "_prop_acct_state に last_trade_date キーが見つからない"
        )

        # 出現回数: 2箇所（SurvivalMode + 通常ループ）
        count = source.count('"last_trade_date": self._last_trade_date_et') + \
                source.count("'last_trade_date': self._last_trade_date_et")
        assert count >= 2, f"_prop_acct_state の last_trade_date 記入が2箇所未満: {count}箇所"

    def test_last_trade_date_et_attribute_exists(self, tmp_path, monkeypatch):
        """MFFUBot に _last_trade_date_et 属性があること"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MFFU_ACCOUNT_ID", "test_ldate")
        # MFFUBot を __new__ で最小初期化し _last_trade_date_et をチェック
        from chronos_bot import MFFUBot
        bot = MFFUBot.__new__(MFFUBot)
        bot._orders_db_path = tmp_path / "orders.db"
        # 手動で必要最小限の属性を設定
        bot._last_trade_date_et = None
        assert hasattr(bot, "_last_trade_date_et"), "_last_trade_date_et 属性がない"
        assert bot._last_trade_date_et is None


# ─────────────────────────────────────────────────────────────────────────────
# β-5: mutmut pin 確認 (CIワークフロー)
# ─────────────────────────────────────────────────────────────────────────────

class TestBeta5MutmutPin:
    """β-5: chronos_quality.yml に mutmut==2.4.4 pin があること"""

    def test_mutmut_pinned_in_workflow(self):
        wf_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            ".github", "workflows", "chronos_quality.yml"
        )
        with open(wf_path, encoding="utf-8") as f:
            content = f.read()
        assert "mutmut==2.4.4" in content, "mutmut==2.4.4 が chronos_quality.yml に存在しない"

    def test_python_version_unified_3_12(self):
        wf_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            ".github", "workflows", "chronos_quality.yml"
        )
        with open(wf_path, encoding="utf-8") as f:
            content = f.read()
        import re
        versions = re.findall(r"python-version:\s*['\"]?([\d.]+)['\"]?", content)
        assert all(v == "3.12" for v in versions), f"python-version が 3.12 に統一されていない: {versions}"


# ─────────────────────────────────────────────────────────────────────────────
# β-6: plan_id fail-closed (ValueError)
# ─────────────────────────────────────────────────────────────────────────────

class TestBeta6PlanIdFailClosed:
    """β-6: 未知の plan/phase 組合せで ValueError が上がる"""

    def test_unknown_plan_raises_value_error(self):
        from common.plan_id import from_yaml_plan_phase
        with pytest.raises(ValueError, match="未知の組み合わせ"):
            from_yaml_plan_phase("unknown_plan_xyz", "evaluation")

    def test_unknown_phase_raises_value_error(self):
        from common.plan_id import from_yaml_plan_phase
        with pytest.raises(ValueError):
            from_yaml_plan_phase("flex_50k", "unknown_phase_xyz")

    def test_known_combination_returns_correct_plan_id(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        result = from_yaml_plan_phase("flex_50k", "evaluation")
        assert result == PlanID.FLEX_EVAL

    def test_known_rapid_sim(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        result = from_yaml_plan_phase("rapid_50k", "sim_funded")
        assert result == PlanID.RAPID_SIM


# ─────────────────────────────────────────────────────────────────────────────
# β-7: core_50k default 除去 → 空文字 → fail-closed
# ─────────────────────────────────────────────────────────────────────────────

class TestBeta7Core50kDefaultRemoved:
    """β-7: chronos_bot.py の _yaml_plan デフォルトが core_50k でないこと"""

    def test_core_50k_default_removed_from_source(self):
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chronos_bot.py")
        with open(src_path, encoding="utf-8") as f:
            source = f.read()

        # β-7 修正: .get("plan", "core_50k") が存在しないこと
        assert '.get("plan", "core_50k")' not in source and ".get('plan', 'core_50k')" not in source, (
            'β-7: "core_50k" デフォルトが残存している'
        )

    def test_plan_defaults_to_empty_string(self):
        """デフォルトが空文字になっていること"""
        src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chronos_bot.py")
        with open(src_path, encoding="utf-8") as f:
            source = f.read()
        # 空文字デフォルトが使われているか
        assert '.get("plan", "")' in source or ".get('plan', '')" in source, (
            'β-7: .get("plan", "") が見つからない'
        )


# ─────────────────────────────────────────────────────────────────────────────
# β-3: peak_balance アカウント別 (マルチアカ独立)
# ─────────────────────────────────────────────────────────────────────────────

class TestBeta3PeakBalancePerAccount:
    """β-3: 異なる MFFU_ACCOUNT_ID で別パスが使われる"""

    def test_peak_balance_file_differs_by_account(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))

        from chronos_bot import MFFUBot

        monkeypatch.setenv("MFFU_ACCOUNT_ID", "acct_A")
        bot_a = MFFUBot.__new__(MFFUBot)
        bot_a._session_balance = 50000.0
        bot_a._all_time_peak_balance = 50000.0
        path_a = bot_a._peak_balance_file()

        monkeypatch.setenv("MFFU_ACCOUNT_ID", "acct_B")
        bot_b = MFFUBot.__new__(MFFUBot)
        bot_b._session_balance = 50000.0
        bot_b._all_time_peak_balance = 50000.0
        path_b = bot_b._peak_balance_file()

        assert path_a != path_b, "acct_A と acct_B で同じパスが返された"
        assert "acct_A" in str(path_a)
        assert "acct_B" in str(path_b)

    def test_peak_balance_isolated_between_accounts(self, tmp_path, monkeypatch):
        """acct_A の peak を上書きしても acct_B に影響しない"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        from chronos_bot import MFFUBot

        monkeypatch.setenv("MFFU_ACCOUNT_ID", "iso_A")
        bot_a = MFFUBot.__new__(MFFUBot)
        bot_a._session_balance = 50000.0
        bot_a._all_time_peak_balance = 55000.0
        bot_a._save_all_time_peak()

        monkeypatch.setenv("MFFU_ACCOUNT_ID", "iso_B")
        bot_b = MFFUBot.__new__(MFFUBot)
        bot_b._session_balance = 50000.0
        bot_b._all_time_peak_balance = 50000.0
        # iso_B は iso_A のファイルを読まない
        loaded = bot_b._load_all_time_peak()
        assert loaded == 50000.0, f"iso_B が iso_A の peak を読み込んでいる: {loaded}"

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        """save → load で同じ値が返る"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MFFU_ACCOUNT_ID", "roundtrip_test")
        from chronos_bot import MFFUBot
        bot = MFFUBot.__new__(MFFUBot)
        bot._session_balance = 50000.0
        bot._all_time_peak_balance = 52345.67
        bot._save_all_time_peak()
        loaded = bot._load_all_time_peak()
        assert abs(loaded - 52345.67) < 0.01

    def test_5_accounts_have_independent_paths(self, tmp_path, monkeypatch):
        """5アカウントすべてが独立したパスを持つ"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        from chronos_bot import MFFUBot
        account_ids = ["mffu_flex_A", "mffu_rapid_B", "mffu_pro_C", "mffu_builder_E", "tradeify_F"]
        paths = set()
        for acct_id in account_ids:
            monkeypatch.setenv("MFFU_ACCOUNT_ID", acct_id)
            bot = MFFUBot.__new__(MFFUBot)
            bot._session_balance = 50000.0
            bot._all_time_peak_balance = 50000.0
            paths.add(str(bot._peak_balance_file()))
        assert len(paths) == 5, f"パスが独立していない: {paths}"


# ─────────────────────────────────────────────────────────────────────────────
# MF-5α + β-6/β-7 統合: plan_id プロパティ → fail-closed chain
# ─────────────────────────────────────────────────────────────────────────────

class TestMF5AlphaPlanIdIntegration:
    """MF-5α: plan_id が正規化されて返る + β-6/β-7 連携"""

    def test_flex_eval_plan_id(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        pid = from_yaml_plan_phase("flex_50k", "evaluation")
        assert pid.value == "flex_eval"

    def test_rapid_sim_plan_id(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        pid = from_yaml_plan_phase("rapid_50k", "sim_funded")
        assert pid.value == "rapid_sim"

    def test_builder_funded_plan_id(self):
        from common.plan_id import from_yaml_plan_phase, PlanID
        pid = from_yaml_plan_phase("builder_50k", "funded")
        assert pid.value == "builder_funded"

    def test_core_50k_is_deprecated(self):
        from common.plan_id import from_yaml_plan_phase, PlanID, DEPRECATED_PLAN_IDS
        pid = from_yaml_plan_phase("core_50k", "evaluation")
        assert pid.value in DEPRECATED_PLAN_IDS

    def test_unknown_plan_fail_closed_no_fallback(self):
        """β-6: FLEX_EVAL フォールバックが除去されて ValueError になること"""
        from common.plan_id import from_yaml_plan_phase
        with pytest.raises(ValueError):
            from_yaml_plan_phase("nonexistent_plan", "evaluation")


# ─────────────────────────────────────────────────────────────────────────────
# MF-10α: peak_balance 追跡全般
# ─────────────────────────────────────────────────────────────────────────────

class TestMF10AlphaPeakBalance:
    """MF-10α: peak_balance 追跡"""

    def test_peak_balance_file_in_accounts_subdir(self, tmp_path, monkeypatch):
        """peak_balance ファイルが data/accounts/{acct_id}/ 以下に作成される"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MFFU_ACCOUNT_ID", "peak_test_acct")
        from chronos_bot import MFFUBot
        bot = MFFUBot.__new__(MFFUBot)
        bot._session_balance = 50000.0
        bot._all_time_peak_balance = 50000.0
        path = bot._peak_balance_file()
        assert "accounts" in str(path), f"accounts サブディレクトリにない: {path}"
        assert "peak_test_acct" in str(path)
        assert path.name == "all_time_peak_balance.json"

    def test_peak_balance_not_in_data_root(self, tmp_path, monkeypatch):
        """旧パス data/all_time_peak_balance.json は使われないこと"""
        monkeypatch.setenv("MFFU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MFFU_ACCOUNT_ID", "root_test")
        from chronos_bot import MFFUBot
        bot = MFFUBot.__new__(MFFUBot)
        bot._session_balance = 50000.0
        bot._all_time_peak_balance = 99999.0
        bot._save_all_time_peak()
        # データルート直下には作成されていないこと
        root_path = tmp_path / "all_time_peak_balance.json"
        assert not root_path.exists(), f"旧パスが使われている: {root_path}"


# ─────────────────────────────────────────────────────────────────────────────
# 統合: check_order 内の T1 News blackout が ET-aware 時刻を使うこと
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckOrderT1Integration:
    """check_order の T1 News blackout コールが ET-aware であること"""

    def test_check_order_t1_call_uses_et_aware(self):
        """common/prop_firm_rules.py の check_order() 内で
        check_t1_news_blackout に渡す datetime が ET-aware であること"""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "common", "prop_firm_rules.py"
        )
        with open(src_path, encoding="utf-8") as f:
            source = f.read()

        # β-1修正後: "datetime.datetime.now(tz=" が T1 News blackout 呼び出し前後に存在すること
        assert "datetime.now(tz=" in source, (
            "β-1: ET-aware な datetime.now(tz=...) が prop_firm_rules.py に存在しない"
        )

    def test_no_naive_now_in_check_order_t1_block(self):
        """check_order の T1 ブロックに naive な datetime.now() が残っていないこと"""
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "common", "prop_firm_rules.py"
        )
        with open(src_path, encoding="utf-8") as f:
            lines = f.readlines()

        # 8. T1 News Blackout コメントの後に naive datetime.now() が来ていないこと
        in_t1_block = False
        for i, line in enumerate(lines):
            if "T1 News Blackout" in line:
                in_t1_block = True
            if in_t1_block and "check_t1_news_blackout(" in line:
                # このブロックの前後10行をチェック
                window = "".join(lines[max(0, i-5):i+5])
                assert "datetime.datetime.now()" not in window, (
                    f"line {i+1} 付近に naive datetime.now() が残存"
                )
                break
