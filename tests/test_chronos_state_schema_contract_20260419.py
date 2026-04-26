"""tests/test_chronos_state_schema_contract_20260419.py — Schema Contract テスト

cycle2 schema contract test 導入:
  - chronos_bot._save_state() が書き出すフィールド集合を取得
  - chronos_agent が参照する LOAD_STATE_REQUIRED フィールドを取得
  - 差分=0件を assert
  - E2E往復テスト: bot._save_state(tmp) → agent.load_all_account_states(tmp) → 値が正しく読める

依存: chronos_bot.ChronosBot, chronos_agent.load_all_account_states
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import chronos_agent as agent
import chronos_bot as bot_module


# ── agent.py が state.json で参照するフィールド一覧（手動定義・変更時はここも更新） ──
# grep で抽出した全 state.get() 参照フィールド:
#   agent.py:591  best_single_day_profit_usd
#   agent.py:592  total_profit_usd
#   agent.py:628  winning_days_count
#   agent.py:629  total_profit_usd (再利用)
#   agent.py:665  phase_flags.news_window_violation
#   agent.py:694  daily_trade_count
# 加えて既存フィールド:
#   account_id, timestamp, account_type, weekly_dd_usd, daily_pnl_usd,
#   consecutive_losses, phase_flags (dict)
AGENT_REQUIRED_TOP_LEVEL = {
    "account_id",
    "timestamp",
    "account_type",
    "weekly_dd_usd",
    "daily_pnl_usd",
    "consecutive_losses",
    "phase_flags",
    # cycle2 追加
    "best_single_day_profit_usd",
    "total_profit_usd",
    "winning_days_count",
    "daily_trade_count",
}

AGENT_REQUIRED_PHASE_FLAGS = {
    "survival_mode",
    "kill_switch_day",
    "daily_halt",
    "daily_soft_stop_active",
    # cycle2 追加
    "news_window_violation",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def dry_bot(tmp_path):
    """dry_run=True の ChronosBot インスタンスを生成する。
    MFFU_DATA_DIR を tmp_path に向けてファイル汚染を防ぐ。
    """
    with patch.dict(os.environ, {
        "MFFU_DATA_DIR": str(tmp_path),
        "MFFU_LOG_DIR": str(tmp_path / "logs"),
        "MFFU_ACCOUNT_ID": "test_account",
        "PUSHOVER_USER": "",
        "PUSHOVER_OPS_TOKEN": "",
    }):
        with patch("chronos_bot.TradovateClient", MagicMock()):
            b = bot_module.ChronosBot(paper=True, dry_run=True)
    return b, tmp_path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Schema Contract Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStateSchemaContractTopLevel:
    """_save_state() が agent.py の参照フィールドを全て書き出すか確認する。"""

    def test_all_required_top_level_fields_present(self, dry_bot):
        """agent.py が参照する全トップレベルフィールドが state.json に存在する。"""
        bot, tmp_path = dry_bot
        # _BASE_DIR はモジュールレベルで確定するため、bot_module._BASE_DIR をパッチする
        with patch.object(bot_module, "_BASE_DIR", tmp_path):
            bot._save_state("schema_test")

        candidates = list((tmp_path / "accounts").glob("*/state.json"))
        assert len(candidates) > 0, "state.json が存在しない"
        state = json.loads(candidates[0].read_text())

        missing = AGENT_REQUIRED_TOP_LEVEL - set(state.keys())
        assert missing == set(), (
            f"state.json に必要なフィールドが不足しています: {missing}\n"
            f"実際のキー: {sorted(state.keys())}"
        )

    def test_all_required_phase_flags_present(self, dry_bot):
        """agent.py が参照する phase_flags のサブフィールドが全て存在する。"""
        bot, tmp_path = dry_bot
        with patch.object(bot_module, "_BASE_DIR", tmp_path):
            bot._save_state("schema_test_phase")

        candidates = list((tmp_path / "accounts").glob("*/state.json"))
        assert len(candidates) > 0, "state.json が存在しない"
        state = json.loads(candidates[0].read_text())

        phase_flags = state.get("phase_flags", {})
        missing = AGENT_REQUIRED_PHASE_FLAGS - set(phase_flags.keys())
        assert missing == set(), (
            f"phase_flags に必要なフィールドが不足しています: {missing}\n"
            f"実際のキー: {sorted(phase_flags.keys())}"
        )


class TestStateSchemaContractValues:
    """各フィールドの型・デフォルト値が正しいか確認する。"""

    def _load_state(self, bot, tmp_path, reason="value_test"):
        with patch.object(bot_module, "_BASE_DIR", tmp_path):
            bot._save_state(reason)
        candidates = list((tmp_path / "accounts").glob("*/state.json"))
        assert candidates, "state.json が存在しない"
        return json.loads(candidates[0].read_text())

    def test_best_single_day_profit_is_float(self, dry_bot):
        bot, tmp_path = dry_bot
        state = self._load_state(bot, tmp_path)
        val = state["best_single_day_profit_usd"]
        assert isinstance(val, (int, float)), f"float expected, got {type(val)}"

    def test_total_profit_is_float(self, dry_bot):
        bot, tmp_path = dry_bot
        state = self._load_state(bot, tmp_path)
        val = state["total_profit_usd"]
        assert isinstance(val, (int, float)), f"float expected, got {type(val)}"

    def test_winning_days_is_int(self, dry_bot):
        bot, tmp_path = dry_bot
        state = self._load_state(bot, tmp_path)
        val = state["winning_days_count"]
        assert isinstance(val, int), f"int expected, got {type(val)}"

    def test_daily_trade_count_is_int(self, dry_bot):
        bot, tmp_path = dry_bot
        state = self._load_state(bot, tmp_path)
        val = state["daily_trade_count"]
        assert isinstance(val, int), f"int expected, got {type(val)}"

    def test_news_window_violation_is_bool(self, dry_bot):
        bot, tmp_path = dry_bot
        state = self._load_state(bot, tmp_path)
        val = state["phase_flags"]["news_window_violation"]
        assert isinstance(val, bool), f"bool expected, got {type(val)}"

    def test_default_values_are_zero(self, dry_bot):
        """初期状態では全て 0/False であること。"""
        bot, tmp_path = dry_bot
        state = self._load_state(bot, tmp_path)
        assert state["best_single_day_profit_usd"] == 0.0
        assert state["total_profit_usd"] == 0.0
        assert state["winning_days_count"] == 0
        assert state["daily_trade_count"] == 0
        assert state["phase_flags"]["news_window_violation"] is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# E2E 往復テスト: bot._save_state → agent.load_all_account_states
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStateE2ERoundTrip:
    """bot が書いた state.json を agent が正しく読めるか確認する。"""

    def test_roundtrip_all_cycle2_fields(self, dry_bot, tmp_path):
        """bot._save_state() → agent.load_all_account_states() で値が往復できる。"""
        bot, bot_tmp = dry_bot

        # bot 側の値を設定
        bot._best_single_day_profit = 1234.56
        bot._total_profit = 9876.54
        bot._winning_days = 7
        bot._daily_trade_count = 42
        bot._news_window_violation_flag = True

        with patch.object(bot_module, "_BASE_DIR", bot_tmp):
            bot._save_state("e2e_test")

        # agent 側で読み込む（ACCOUNTS_DIR を bot_tmp/accounts に向ける）
        with patch.object(agent, "ACCOUNTS_DIR", bot_tmp / "accounts"):
            states = agent.load_all_account_states()

        assert len(states) >= 1, "agent がstateを読めなかった"
        s = states[0]

        assert s["best_single_day_profit_usd"] == pytest.approx(1234.56, abs=0.01)
        assert s["total_profit_usd"] == pytest.approx(9876.54, abs=0.01)
        assert s["winning_days_count"] == 7
        assert s["daily_trade_count"] == 42
        assert s["phase_flags"]["news_window_violation"] is True

    def test_roundtrip_consistency_check_reads_best_day(self, dry_bot, tmp_path):
        """agent.check_level3_pnl() が best_single_day_profit_usd を正しく読める。"""
        bot, bot_tmp = dry_bot

        # Consistency Rule 接近ケース: best_day = 40% of total (warn_pct=40%)
        bot._best_single_day_profit = 400.0
        bot._total_profit = 1000.0
        bot._winning_days = 3
        bot._daily_trade_count = 10
        # account_type を evaluation に設定
        bot._account_type = "evaluation"

        with patch.object(bot_module, "_BASE_DIR", bot_tmp):
            bot._save_state("e2e_consistency")

        with patch.object(agent, "ACCOUNTS_DIR", bot_tmp / "accounts"):
            states = agent.load_all_account_states()

        assert len(states) >= 1
        s = states[0]
        # agent.py L591-595 相当の計算
        best_day = float(s.get("best_single_day_profit_usd", 0.0))
        total_profit = float(s.get("total_profit_usd", 0.0))
        assert best_day == pytest.approx(400.0, abs=0.01)
        assert total_profit == pytest.approx(1000.0, abs=0.01)
        actual_pct = best_day / total_profit
        assert actual_pct == pytest.approx(0.40, abs=0.001)

    def test_roundtrip_hft_check_reads_daily_trade_count(self, dry_bot, tmp_path):
        """agent.check_level4_hft() が daily_trade_count を正しく読める。"""
        bot, bot_tmp = dry_bot
        bot._daily_trade_count = 165  # HFT警告閾値(160)超え

        with patch.object(bot_module, "_BASE_DIR", bot_tmp):
            bot._save_state("e2e_hft")

        with patch.object(agent, "ACCOUNTS_DIR", bot_tmp / "accounts"):
            states = agent.load_all_account_states()

        assert len(states) >= 1
        daily_trades = int(states[0].get("daily_trade_count", 0))
        assert daily_trades == 165

    def test_roundtrip_news_violation_check(self, dry_bot, tmp_path):
        """agent.check_level4_news_window() が news_window_violation を正しく読める。"""
        bot, bot_tmp = dry_bot
        bot._news_window_violation_flag = True

        with patch.object(bot_module, "_BASE_DIR", bot_tmp):
            bot._save_state("e2e_news")

        with patch.object(agent, "ACCOUNTS_DIR", bot_tmp / "accounts"):
            states = agent.load_all_account_states()

        assert len(states) >= 1
        phase_flags = states[0].get("phase_flags", {})
        assert phase_flags.get("news_window_violation") is True

    def test_roundtrip_payout_check_reads_winning_days(self, dry_bot, tmp_path):
        """agent.check_level3_payout_reminder() が winning_days_count を正しく読める。"""
        bot, bot_tmp = dry_bot
        bot._winning_days = 8
        bot._total_profit = 2000.0

        with patch.object(bot_module, "_BASE_DIR", bot_tmp):
            bot._save_state("e2e_payout")

        with patch.object(agent, "ACCOUNTS_DIR", bot_tmp / "accounts"):
            states = agent.load_all_account_states()

        assert len(states) >= 1
        winning_days = int(states[0].get("winning_days_count", 0))
        total_profit = float(states[0].get("total_profit_usd", 0.0))
        assert winning_days == 8
        assert total_profit == pytest.approx(2000.0, abs=0.01)
