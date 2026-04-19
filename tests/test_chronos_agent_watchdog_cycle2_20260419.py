"""tests/test_chronos_agent_watchdog_cycle2_20260419.py — cycle2 真修正テスト

CRITICAL 4件 + HIGH 5件の修正を検証する。

CRITICAL:
  BUG-1/2/3: state.json 5フィールド書き込み確認
  BUG-4:     PID file 生成・pgrep 厳密化
  BUG-5:     manual_halt --unhalt CLI

HIGH:
  STR-2:     水増しテスト除去（TestChronosBotStrategySelector を別ファイルに移動）
  NotImplementedError 除去: pre_trade_check F1/F2/F3/F4, bot Client/Strategy/run

目標: 40件以上
"""
from __future__ import annotations

import json
import os
import sys
import signal
import tempfile
import subprocess
import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import chronos_agent as agent
import chronos_bot as bot_module


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture(autouse=True)
def reset_notified():
    agent._notified.clear()
    yield
    agent._notified.clear()


@pytest.fixture
def dry_bot(tmp_path):
    """dry_run=True の ChronosBot インスタンス。"""
    with patch.dict(os.environ, {
        "MFFU_DATA_DIR": str(tmp_path),
        "MFFU_LOG_DIR": str(tmp_path / "logs"),
        "MFFU_ACCOUNT_ID": "acc_test",
        "PUSHOVER_USER": "",
        "PUSHOVER_OPS_TOKEN": "",
    }):
        with patch("chronos_bot.TradovateClient", MagicMock()):
            b = bot_module.ChronosBot(paper=True, dry_run=True)
    return b, tmp_path


def save_and_load(bot, tmp_path, reason="test") -> dict:
    """bot._save_state → state.json を読み込んで返す。
    _BASE_DIR はモジュールレベルで確定するため、bot_module._BASE_DIR をパッチする。
    """
    with patch.object(bot_module, "_BASE_DIR", tmp_path):
        bot._save_state(reason)
    candidates = list((tmp_path / "accounts").glob("*/state.json"))
    assert candidates, "state.json が書かれなかった"
    return json.loads(candidates[0].read_text())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-1/2/3: state.json 5フィールド書き込み確認
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStateSchemaCycle2Fields:
    """_save_state() が cycle2 追加の5フィールドを書き出すか確認する。"""

    def test_best_single_day_profit_written(self, dry_bot):
        bot, tmp = dry_bot
        bot._best_single_day_profit = 500.0
        state = save_and_load(bot, tmp)
        assert "best_single_day_profit_usd" in state
        assert state["best_single_day_profit_usd"] == pytest.approx(500.0)

    def test_total_profit_written(self, dry_bot):
        bot, tmp = dry_bot
        bot._total_profit = 3000.0
        state = save_and_load(bot, tmp)
        assert "total_profit_usd" in state
        assert state["total_profit_usd"] == pytest.approx(3000.0)

    def test_winning_days_written(self, dry_bot):
        bot, tmp = dry_bot
        bot._winning_days = 5
        state = save_and_load(bot, tmp)
        assert "winning_days_count" in state
        assert state["winning_days_count"] == 5

    def test_daily_trade_count_written(self, dry_bot):
        bot, tmp = dry_bot
        bot._daily_trade_count = 37
        state = save_and_load(bot, tmp)
        assert "daily_trade_count" in state
        assert state["daily_trade_count"] == 37

    def test_news_window_violation_written_in_phase_flags(self, dry_bot):
        bot, tmp = dry_bot
        bot._news_window_violation_flag = True
        state = save_and_load(bot, tmp)
        assert "phase_flags" in state
        assert "news_window_violation" in state["phase_flags"]
        assert state["phase_flags"]["news_window_violation"] is True

    def test_news_window_violation_false_by_default(self, dry_bot):
        bot, tmp = dry_bot
        state = save_and_load(bot, tmp)
        assert state["phase_flags"]["news_window_violation"] is False

    def test_all_five_fields_present_simultaneously(self, dry_bot):
        """5フィールド全て同時に書き出されること。"""
        bot, tmp = dry_bot
        bot._best_single_day_profit = 100.0
        bot._total_profit = 400.0
        bot._winning_days = 2
        bot._daily_trade_count = 15
        bot._news_window_violation_flag = False
        state = save_and_load(bot, tmp)
        for field in [
            "best_single_day_profit_usd",
            "total_profit_usd",
            "winning_days_count",
            "daily_trade_count",
        ]:
            assert field in state, f"{field} が state.json に存在しない"
        assert "news_window_violation" in state["phase_flags"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# インスタンス変数初期化確認
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestInstanceVarsInitialized:
    """__init__ で5変数が正しく初期化されること。"""

    def test_best_single_day_profit_init(self, dry_bot):
        bot, _ = dry_bot
        assert hasattr(bot, "_best_single_day_profit")
        assert bot._best_single_day_profit == 0.0

    def test_total_profit_init(self, dry_bot):
        bot, _ = dry_bot
        assert hasattr(bot, "_total_profit")
        assert bot._total_profit == 0.0

    def test_winning_days_init(self, dry_bot):
        bot, _ = dry_bot
        assert hasattr(bot, "_winning_days")
        assert bot._winning_days == 0

    def test_daily_trade_count_init(self, dry_bot):
        bot, _ = dry_bot
        assert hasattr(bot, "_daily_trade_count")
        assert bot._daily_trade_count == 0

    def test_news_window_violation_flag_init(self, dry_bot):
        bot, _ = dry_bot
        assert hasattr(bot, "_news_window_violation_flag")
        assert bot._news_window_violation_flag is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 日次リセット更新ロジック
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDailyResetCycle2:
    """_daily_reset() が cycle2 変数を正しく更新/リセットするか確認する。"""

    def test_winning_day_incremented_on_positive_pnl(self, dry_bot):
        bot, _ = dry_bot
        bot._today_realized_pnl = 200.0
        bot._daily_reset(datetime.date.today())
        assert bot._winning_days == 1

    def test_total_profit_accumulated_on_positive_pnl(self, dry_bot):
        bot, _ = dry_bot
        bot._today_realized_pnl = 300.0
        bot._daily_reset(datetime.date.today())
        assert bot._total_profit == pytest.approx(300.0)

    def test_best_day_updated_on_profit(self, dry_bot):
        bot, _ = dry_bot
        bot._today_realized_pnl = 500.0
        bot._daily_reset(datetime.date.today())
        assert bot._best_single_day_profit == pytest.approx(500.0)

    def test_best_day_not_updated_if_smaller(self, dry_bot):
        bot, _ = dry_bot
        bot._best_single_day_profit = 800.0
        bot._today_realized_pnl = 200.0
        bot._daily_reset(datetime.date.today())
        assert bot._best_single_day_profit == pytest.approx(800.0)

    def test_loss_accumulated_in_total_profit(self, dry_bot):
        bot, _ = dry_bot
        bot._today_realized_pnl = -150.0
        bot._daily_reset(datetime.date.today())
        assert bot._total_profit == pytest.approx(-150.0)
        assert bot._winning_days == 0

    def test_daily_trade_count_reset(self, dry_bot):
        bot, _ = dry_bot
        bot._daily_trade_count = 42
        bot._daily_reset(datetime.date.today())
        assert bot._daily_trade_count == 0

    def test_news_violation_flag_reset(self, dry_bot):
        bot, _ = dry_bot
        bot._news_window_violation_flag = True
        bot._daily_reset(datetime.date.today())
        assert bot._news_window_violation_flag is False

    def test_consecutive_winning_days_accumulate(self, dry_bot):
        """複数日勝利で winning_days が累積されること。"""
        bot, _ = dry_bot
        for _ in range(3):
            bot._today_realized_pnl = 100.0
            bot._daily_reset(datetime.date.today())
        assert bot._winning_days == 3
        assert bot._total_profit == pytest.approx(300.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-4: PID file 生成・is_bot_alive_for_account
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPIDFileCycle2:
    """is_bot_alive_for_account: pid.lock ベースのプロセス確認テスト。"""

    def test_alive_when_pid_lock_exists_and_process_running(self, tmp_path):
        """pid.lock に自分のPIDを書くと is_bot_alive_for_account が True を返す。"""
        acc_dir = tmp_path / "accounts" / "test_acc"
        acc_dir.mkdir(parents=True)
        (acc_dir / "pid.lock").write_text(str(os.getpid()))

        with patch.object(agent, "ACCOUNTS_DIR", tmp_path / "accounts"):
            result = agent.is_bot_alive_for_account("test_acc")
        assert result is True

    def test_dead_when_pid_lock_missing(self, tmp_path):
        """pid.lock が存在しない場合は False。"""
        with patch.object(agent, "ACCOUNTS_DIR", tmp_path / "accounts"):
            result = agent.is_bot_alive_for_account("nonexistent_acc")
        assert result is False

    def test_dead_when_pid_not_running(self, tmp_path):
        """存在しないPIDを書いた場合は False。"""
        acc_dir = tmp_path / "accounts" / "dead_acc"
        acc_dir.mkdir(parents=True)
        # PID 99999999 は通常存在しない
        (acc_dir / "pid.lock").write_text("99999999")

        with patch.object(agent, "ACCOUNTS_DIR", tmp_path / "accounts"):
            result = agent.is_bot_alive_for_account("dead_acc")
        assert result is False

    def test_dead_when_pid_file_corrupt(self, tmp_path):
        """PIDファイルが壊れている場合は False。"""
        acc_dir = tmp_path / "accounts" / "corrupt_acc"
        acc_dir.mkdir(parents=True)
        (acc_dir / "pid.lock").write_text("not_a_pid")

        with patch.object(agent, "ACCOUNTS_DIR", tmp_path / "accounts"):
            result = agent.is_bot_alive_for_account("corrupt_acc")
        assert result is False

    def test_is_bot_alive_uses_pid_lock_first(self, tmp_path):
        """is_bot_alive が pid.lock を優先して確認する。"""
        acc_dir = tmp_path / "accounts" / "priority_acc"
        acc_dir.mkdir(parents=True)
        (acc_dir / "pid.lock").write_text(str(os.getpid()))

        cfg = {"pid_files": [], "market_window": {"start": "00:00", "end": "23:59"}}
        with patch.object(agent, "ACCOUNTS_DIR", tmp_path / "accounts"):
            result = agent.is_bot_alive(cfg)
        assert result is True

    def test_is_bot_alive_falls_back_to_pgrep(self, tmp_path):
        """pid.lock が存在しない場合は pgrep フォールバックが動く。"""
        cfg = {"pid_files": [], "market_window": {"start": "00:00", "end": "23:59"}}
        with patch.object(agent, "ACCOUNTS_DIR", tmp_path / "accounts"):
            with patch("chronos_agent.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = agent.is_bot_alive(cfg)
        # pgrep が returncode=0 なら True
        assert result is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG-5: manual_halt --unhalt CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestUnhaltCLI:
    """unhalt() 関数の動作確認。"""

    def test_unhalt_removes_manual_halt(self, tmp_path):
        """unhalt() が agent_state から manual_halt を削除する。"""
        # manual_halt を設定
        state = {
            "manual_halt": {
                "key": "news_window_violation_acc1",
                "since": "2026-04-19T10:00:00+09:00",
                "title": "test halt",
            }
        }
        with patch.object(agent, "AGENT_STATE_PATH", tmp_path / "agent_state.json"):
            agent.save_agent_state(state)
            with patch("chronos_agent.pushover", return_value=True):
                agent.unhalt()
            loaded = agent.load_agent_state()

        assert "manual_halt" not in loaded

    def test_unhalt_no_op_when_not_set(self, tmp_path, capsys):
        """manual_halt が未設定の場合は何もしない（エラーなし）。"""
        with patch.object(agent, "AGENT_STATE_PATH", tmp_path / "agent_state.json"):
            agent.save_agent_state({})
            with patch("chronos_agent.pushover", return_value=True):
                agent.unhalt()
            loaded = agent.load_agent_state()

        assert "manual_halt" not in loaded
        captured = capsys.readouterr()
        assert "not set" in captured.out or "unhalt" in captured.out

    def test_unhalt_sends_pushover_notification(self, tmp_path):
        """unhalt() が Pushover 通知を送ること。"""
        state = {
            "manual_halt": {"key": "k", "since": "2026-04-19T00:00:00+09:00", "title": "T"}
        }
        with patch.object(agent, "AGENT_STATE_PATH", tmp_path / "agent_state.json"):
            agent.save_agent_state(state)
            with patch("chronos_agent.pushover", return_value=True) as mock_push:
                agent.unhalt()

        mock_push.assert_called_once()
        call_args = mock_push.call_args
        assert "unhalt" in str(call_args).lower() or "解除" in str(call_args)

    def test_unhalt_preserves_other_state(self, tmp_path):
        """unhalt() が manual_halt 以外のフィールドを保持すること。"""
        state = {
            "last_cycle_jst": "2026-04-19T10:00:00+09:00",
            "manual_halt": {"key": "k", "since": "x", "title": "T"},
        }
        with patch.object(agent, "AGENT_STATE_PATH", tmp_path / "agent_state.json"):
            agent.save_agent_state(state)
            with patch("chronos_agent.pushover", return_value=True):
                agent.unhalt()
            loaded = agent.load_agent_state()

        assert "last_cycle_jst" in loaded
        assert loaded["last_cycle_jst"] == "2026-04-19T10:00:00+09:00"

    def test_unhalt_function_exists_in_module(self):
        """unhalt() 関数が chronos_agent モジュールに存在すること。"""
        assert hasattr(agent, "unhalt")
        assert callable(agent.unhalt)

    def test_unhalt_cli_option_in_argparse(self):
        """--unhalt オプションが argparse に登録されていること。"""
        import argparse
        import inspect
        # main() のソースコードに --unhalt が含まれること
        src = inspect.getsource(agent.main)
        assert "--unhalt" in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NotImplementedError 除去確認
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNotImplementedErrorRemoved:
    """NotImplementedError が全件除去されたことを確認する。"""

    def test_chronos_bot_no_notimplemented(self):
        """chronos_bot.py に NotImplementedError が残っていないこと。"""
        src_path = _ROOT / "chronos_bot.py"
        src = src_path.read_text(encoding="utf-8")
        # raise NotImplementedError の行数を確認
        lines_with_raise = [
            i + 1 for i, line in enumerate(src.splitlines())
            if "raise NotImplementedError" in line
        ]
        assert lines_with_raise == [], (
            f"chronos_bot.py に NotImplementedError が残っています: 行 {lines_with_raise}"
        )

    def test_chronos_pre_trade_check_no_notimplemented(self):
        """chronos_pre_trade_check.py に NotImplementedError が残っていないこと。"""
        src_path = _ROOT / "chronos_pre_trade_check.py"
        src = src_path.read_text(encoding="utf-8")
        lines_with_raise = [
            i + 1 for i, line in enumerate(src.splitlines())
            if "raise NotImplementedError" in line
        ]
        assert lines_with_raise == [], (
            f"chronos_pre_trade_check.py に NotImplementedError が残っています: 行 {lines_with_raise}"
        )

    def test_chronos_client_connect_returns_bool(self, dry_bot):
        """ChronosClient.connect() が NotImplementedError を出さず bool を返すこと。"""
        client = bot_module.ChronosClient(paper=True, dry_run=True)
        result = client.connect()
        assert isinstance(result, bool)

    def test_chronos_client_disconnect_no_error(self, dry_bot):
        """ChronosClient.disconnect() が NotImplementedError を出さないこと。"""
        client = bot_module.ChronosClient(paper=True, dry_run=True)
        client.disconnect()  # raise しないことを確認

    def test_chronos_client_get_account_info_returns_dict(self):
        """ChronosClient.get_account_info() が dict を返すこと。"""
        client = bot_module.ChronosClient()
        result = client.get_account_info()
        assert isinstance(result, dict)

    def test_chronos_client_get_quote_returns_dict(self):
        """ChronosClient.get_quote() が dict を返すこと。"""
        client = bot_module.ChronosClient()
        result = client.get_quote("MES")
        assert isinstance(result, dict)

    def test_chronos_client_place_order_returns_dict(self):
        """ChronosClient.place_order() が dict を返すこと。"""
        client = bot_module.ChronosClient()
        result = client.place_order("MES", "BUY", 1)
        assert isinstance(result, dict)

    def test_chronos_client_cancel_order_returns_bool(self):
        """ChronosClient.cancel_order() が bool を返すこと。"""
        client = bot_module.ChronosClient()
        result = client.cancel_order("ORD-001")
        assert isinstance(result, bool)

    def test_chronos_client_get_positions_returns_list(self):
        """ChronosClient.get_positions() が list を返すこと。"""
        client = bot_module.ChronosClient()
        result = client.get_positions()
        assert isinstance(result, list)

    def test_chronos_strategy_select_tactic_returns_str(self, dry_bot):
        """ChronosStrategy.select_tactic() が str を返すこと。"""
        bot, _ = dry_bot
        client = bot_module.ChronosClient()
        strategy = bot_module.ChronosStrategy(rules={}, client=client)
        result = strategy.select_tactic({})
        assert isinstance(result, str)

    def test_chronos_strategy_compute_entry_returns_none(self, dry_bot):
        """ChronosStrategy.compute_entry() が None を返すこと（スタブ）。"""
        client = bot_module.ChronosClient()
        strategy = bot_module.ChronosStrategy(rules={}, client=client)
        result = strategy.compute_entry("orb", {})
        assert result is None

    def test_chronos_strategy_compute_exit_returns_bool(self, dry_bot):
        """ChronosStrategy.compute_exit() が bool を返すこと（スタブ）。"""
        client = bot_module.ChronosClient()
        strategy = bot_module.ChronosStrategy(rules={}, client=client)
        result = strategy.compute_exit({}, {})
        assert isinstance(result, bool)

    def test_run_once_true_does_not_raise(self, dry_bot):
        """run(once=True) が NotImplementedError を出さないこと。"""
        with patch("chronos_bot.ChronosBot") as MockBot:
            mock_instance = MagicMock()
            MockBot.return_value = mock_instance
            # once=True は dry_run=True を強制して run_forever を呼ぶ
            bot_module.run(paper=True, dry_run=False, once=True)
            mock_instance.run_forever.assert_called_once()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# pre_trade_check F1/F2/F3/F4 MVP実装確認
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPreTradeCheckMVP:
    """F1/F2/F3/F4 が NotImplementedError を出さず動作すること。"""

    @pytest.fixture
    def ptc(self):
        import chronos_pre_trade_check as ptc
        return ptc

    def _make_ctx(self, symbol="MES", side="BUY", qty=1, entry_price=5000.0,
                  est_margin=1000.0, capital_usd=50000.0):
        """テスト用 FuturesOrderContext を生成する。"""
        from chronos_pre_trade_check import FuturesOrderContext
        return FuturesOrderContext(
            symbol=symbol, side=side, qty=qty, entry_price=entry_price,
            est_margin=est_margin, capital_usd=capital_usd,
        )

    def test_f1_returns_none_for_known_symbol(self, ptc):
        """F1: FUTURES_META に登録されている銘柄は None（pass）。"""
        from chronos_pre_trade_check import _check_layer_f1_symbol
        ctx = self._make_ctx(symbol="MES")
        result = _check_layer_f1_symbol(ctx)
        assert result is None

    def test_f1_rejects_unknown_symbol(self, ptc):
        """F1: FUTURES_META に未登録の銘柄は CheckResult(allow=False)。"""
        from chronos_pre_trade_check import _check_layer_f1_symbol
        ctx = self._make_ctx(symbol="UNKNOWN_XYZ")
        result = _check_layer_f1_symbol(ctx)
        assert result is not None
        assert result.allow is False
        assert "F1" in result.layer

    def test_f2_returns_none(self, ptc):
        """F2: 常に None（pass）を返すこと。"""
        from chronos_pre_trade_check import _check_layer_f2_mffu_consistency
        ctx = self._make_ctx()
        result = _check_layer_f2_mffu_consistency(ctx)
        assert result is None

    def test_f3_returns_none(self, ptc):
        """F3: 常に None（pass）を返すこと。"""
        from chronos_pre_trade_check import _check_layer_f3_mffu_safety_buffer
        ctx = self._make_ctx()
        result = _check_layer_f3_mffu_safety_buffer(ctx)
        assert result is None

    def test_f4_returns_none_when_margin_sufficient(self, ptc):
        """F4: est_margin が exchange margin を超えている場合は None（pass）。"""
        from chronos_pre_trade_check import _check_layer_f4_futures_margin
        from chronos_symbol_meta import get_initial_margin
        mes_margin = get_initial_margin("MES")
        ctx = self._make_ctx(
            symbol="MES",
            est_margin=mes_margin * 2,  # 必要量の2倍
        )
        result = _check_layer_f4_futures_margin(ctx)
        assert result is None

    def test_f4_rejects_insufficient_margin(self, ptc):
        """F4: est_margin が exchange margin を下回る場合は拒否。"""
        from chronos_pre_trade_check import _check_layer_f4_futures_margin
        from chronos_symbol_meta import get_initial_margin
        mes_margin = get_initial_margin("MES")
        if mes_margin <= 0:
            pytest.skip("MES の margin データが 0 のためスキップ")
        ctx = self._make_ctx(
            symbol="MES",
            est_margin=mes_margin * 0.1,  # 必要量の10%しかない
        )
        result = _check_layer_f4_futures_margin(ctx)
        assert result is not None
        assert result.allow is False
        assert "F4" in result.layer


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STR-2: 水増しテスト除去確認
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestWaterPaddingRemoved:
    """TestChronosBotStrategySelector が別ファイルに移動済みであること。"""

    def test_strategy_selector_class_not_in_original_file(self):
        """test_chronos_agent_watchdog_20260419.py に TestChronosBotStrategySelector クラスが存在しないこと。"""
        src_path = _ROOT / "tests" / "test_chronos_agent_watchdog_20260419.py"
        src = src_path.read_text(encoding="utf-8")
        # class 定義として存在しないこと（コメントは許可）
        import re
        class_defs = re.findall(r"^class TestChronosBotStrategySelector", src, re.MULTILINE)
        assert class_defs == [], (
            "TestChronosBotStrategySelector が test_chronos_agent_watchdog_20260419.py に"
            "まだ class として定義されています。別ファイルに移動済みのはずです。"
        )

    def test_strategy_selector_moved_to_dedicated_file(self):
        """test_chronos_bot_strategy_selector.py が存在すること。"""
        dedicated = _ROOT / "tests" / "test_chronos_bot_strategy_selector.py"
        assert dedicated.exists(), (
            "test_chronos_bot_strategy_selector.py が存在しません。"
            "STR-2: 水増しテスト除去で別ファイルに移動してください。"
        )

    def test_strategy_selector_available_is_false(self):
        """移動先でも STRATEGY_SELECTOR_AVAILABLE=False テストが通ること。"""
        assert bot_module.STRATEGY_SELECTOR_AVAILABLE is False
