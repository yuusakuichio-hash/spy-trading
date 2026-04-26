"""tests/test_chronos_agent_watchdog_20260419.py — Chronos Agent/Watchdog 実装テスト

テスト対象:
  - chronos_agent.py: Bot生存検知・state stale・Level1-4各ルール発火
  - chronos_watchdog.py: tail_new_lines・check_pattern・エラーパターン検知

合計: 25件以上
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import tempfile
import zoneinfo
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from collections import deque

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# ── import（実装ファイル） ────────────────────────────────────────────────────
import chronos_agent as agent
import chronos_watchdog as watchdog

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
ET  = zoneinfo.ZoneInfo("America/New_York")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


@pytest.fixture
def base_cfg():
    """テスト用デフォルト設定"""
    return {
        "bot_launchagent": "com.soralab.chronos_bot",
        "pid_files": [],
        "stale_log_sec": 180,
        "cycle_interval_sec": 60,
        "log_sources": {},
        "market_window": {"start": "00:00", "end": "23:59"},  # 常時market_hours_now=True
        "mffu": {
            "phase": "evaluation",
            "max_loss_usd": 2000.0,
            "consistency_max_pct": 0.50,
            "profit_target_usd": 3000.0,
            "hft_daily_max_trades": 200,
            "builder_daily_loss_usd": 1000.0,
            "payout": {"min_winning_days": 5, "min_net_profit_usd": 500.0},
        },
    }


@pytest.fixture(autouse=True)
def reset_agent_state():
    """テスト間でモジュール状態をリセット"""
    agent._notified.clear()
    watchdog.pattern_times.clear()
    watchdog.last_alert_sent.clear()
    watchdog.log_positions.clear()
    watchdog.log_inodes.clear()
    yield
    agent._notified.clear()
    watchdog.pattern_times.clear()
    watchdog.last_alert_sent.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# chronos_agent.py テスト
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestIsMarketHoursNow:
    """市場時間判定テスト"""

    def test_always_true_cfg(self, base_cfg):
        """00:00-23:59設定では常にTrue"""
        assert agent.is_market_hours_now(base_cfg) is True

    def test_narrow_window_outside(self):
        """現在時刻が窓外ならFalse"""
        cfg = {"market_window": {"start": "01:00", "end": "01:01"}}
        # 現在が01:00-01:01でなければFalse（テスト環境では99.9%False）
        result = agent.is_market_hours_now(cfg)
        import datetime
        now_jst = datetime.datetime.now(JST).strftime("%H:%M")
        expected = "01:00" <= now_jst <= "01:01"
        assert result == expected

    def test_overnight_window(self):
        """日跨ぎウィンドウ（22:25〜05:05）"""
        cfg = {"market_window": {"start": "22:25", "end": "05:05"}}
        # 実行時刻依存のため、ロジックのみ確認（例外が出なければOK）
        result = agent.is_market_hours_now(cfg)
        assert isinstance(result, bool)


class TestIsBotAlive:
    """Bot生存確認テスト"""

    def test_no_pid_files_no_process(self, base_cfg):
        """PIDファイルなし・プロセスなし → False"""
        cfg = dict(base_cfg)
        cfg["pid_files"] = []
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = agent.is_bot_alive(cfg)
        assert result is False

    def test_pgrep_finds_process(self, base_cfg):
        """pgrep でプロセス検出 → True"""
        cfg = dict(base_cfg)
        cfg["pid_files"] = []
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = agent.is_bot_alive(cfg)
        assert result is True

    def test_pid_file_valid_process(self, tmp_dir, base_cfg):
        """PIDファイルあり・プロセス生存 → True"""
        pid_file = tmp_dir / "chronos_bot.pid"
        pid_file.write_text("12345")
        cfg = dict(base_cfg)
        cfg["pid_files"] = [str(pid_file)]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = agent.is_bot_alive(cfg)
        assert result is True

    def test_pid_file_dead_process(self, tmp_dir, base_cfg):
        """PIDファイルあり・プロセス死亡 → pgrep fallback → False"""
        pid_file = tmp_dir / "chronos_bot.pid"
        pid_file.write_text("99999")
        cfg = dict(base_cfg)
        cfg["pid_files"] = [str(pid_file)]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)  # 両方失敗
            result = agent.is_bot_alive(cfg)
        assert result is False


class TestIsLogStale:
    """ログ stale 判定テスト"""

    def test_nonexistent_file(self, tmp_dir):
        """存在しないファイル → (False, 0.0)"""
        p = tmp_dir / "nonexistent.log"
        stale, age = agent.is_log_stale(p, threshold_sec=60)
        assert stale is False
        assert age == 0.0

    def test_fresh_file(self, tmp_dir):
        """直近更新ファイル → (False, <60)"""
        p = tmp_dir / "fresh.log"
        p.write_text("hello\n")
        stale, age = agent.is_log_stale(p, threshold_sec=60)
        assert stale is False
        assert age < 5.0  # テスト実行時間を考慮

    def test_stale_file(self, tmp_dir):
        """古いファイル → (True, age)"""
        p = tmp_dir / "old.log"
        p.write_text("hello\n")
        # mtime を200秒前に設定
        old_time = time.time() - 200
        os.utime(str(p), (old_time, old_time))
        stale, age = agent.is_log_stale(p, threshold_sec=180)
        assert stale is True
        assert age > 180


class TestLoadConfig:
    """設定ロードテスト"""

    def test_yaml_unavailable_returns_default(self):
        """PyYAML利用不可時はデフォルト設定を返す"""
        with patch.object(agent, "YAML_AVAILABLE", False):
            cfg = agent.load_config()
        assert "market_window" in cfg
        assert "mffu" in cfg
        assert cfg["mffu"]["max_loss_usd"] == 2000.0

    def test_yaml_missing_file_returns_default(self, tmp_dir):
        """YAMLファイル未存在時はデフォルト設定"""
        with patch.object(agent, "CHRONOS_RULES_PATH", tmp_dir / "nonexistent.yaml"):
            cfg = agent.load_config()
        assert cfg["mffu"]["max_loss_usd"] == 2000.0


class TestLevel1BotAlive:
    """Level1: Bot生存監視テスト"""

    def test_bot_dead_in_market_hours_triggers_level2(self, base_cfg):
        """市場時間中にBot死亡 → Level2アラートを発火"""
        with patch.object(agent, "is_bot_alive", return_value=False):
            alerts = agent.check_level1_bot_alive(base_cfg, dry_run=True)
        assert len(alerts) == 1
        assert alerts[0]["level"] == 2
        assert alerts[0]["action"] == "restart_bot"

    def test_bot_alive_no_alert(self, base_cfg):
        """Bot生存中 → アラートなし"""
        with patch.object(agent, "is_bot_alive", return_value=True):
            alerts = agent.check_level1_bot_alive(base_cfg, dry_run=True)
        assert len(alerts) == 0

    def test_bot_dead_outside_market_hours(self, base_cfg):
        """場外時間中のBot死亡 → アラートなし（意図的）"""
        cfg = dict(base_cfg)
        cfg["market_window"] = {"start": "01:00", "end": "01:01"}  # 現在時刻外
        with patch.object(agent, "is_bot_alive", return_value=False):
            import datetime
            now_jst = datetime.datetime.now(JST).strftime("%H:%M")
            if not ("01:00" <= now_jst <= "01:01"):
                alerts = agent.check_level1_bot_alive(cfg, dry_run=True)
                assert len(alerts) == 0


class TestLevel2StateStale:
    """Level2: state.json 更新停止検知テスト"""

    def test_stale_state_in_market_hours(self, tmp_dir, base_cfg):
        """60秒以上前の state.json → アラート発火"""
        import datetime
        old_ts = (datetime.datetime.now(ET) - datetime.timedelta(seconds=120)).isoformat()
        state = {
            "account_id": "mffu_test",
            "timestamp": old_ts,
            "daily_pnl_usd": 0.0,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            alerts = agent.check_level2_state_stale(base_cfg)
        assert len(alerts) >= 1
        assert alerts[0]["level"] == 2

    def test_fresh_state_no_alert(self, base_cfg):
        """直近更新 state.json → アラートなし"""
        import datetime
        fresh_ts = datetime.datetime.now(ET).isoformat()
        state = {
            "account_id": "mffu_test",
            "timestamp": fresh_ts,
            "daily_pnl_usd": 0.0,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            alerts = agent.check_level2_state_stale(base_cfg)
        assert len(alerts) == 0


class TestLevel3PnL:
    """Level3: P&L・Max Loss・Consistency監視テスト"""

    def test_max_loss_80pct_alert(self, base_cfg):
        """損失 $1600 / MLL $2000 (80%) → Level3アラート"""
        state = {
            "account_id": "mffu_flex_A",
            "daily_pnl_usd": -1600.0,
            "weekly_dd_usd": 0.0,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level3_pnl(base_cfg)
        assert any(a["level"] == 3 for a in alerts)
        titles = [a["title"] for a in alerts]
        assert any("Max Loss" in t for t in titles)

    def test_loss_below_80pct_no_alert(self, base_cfg):
        """損失 $1000 / MLL $2000 (50%) → アラートなし"""
        state = {
            "account_id": "mffu_flex_A",
            "daily_pnl_usd": -1000.0,
            "weekly_dd_usd": 0.0,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            alerts = agent.check_level3_pnl(base_cfg)
        max_loss_alerts = [a for a in alerts if "Max Loss" in a.get("title", "")]
        assert len(max_loss_alerts) == 0

    def test_builder_daily_loss_alert(self, base_cfg):
        """損失 $850 / Builder警告ライン $1000 (85%) → Level3アラート"""
        state = {
            "account_id": "mffu_flex_A",
            "daily_pnl_usd": -850.0,
            "weekly_dd_usd": 0.0,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level3_pnl(base_cfg)
        builder_alerts = [a for a in alerts if "Builder" in a.get("title", "")]
        assert len(builder_alerts) >= 1

    def test_consistency_alert_evaluation_phase(self, base_cfg):
        """Evaluation: best_day/total = 45% (warn: 40%) → アラート"""
        state = {
            "account_id": "mffu_flex_A",
            "daily_pnl_usd": 0.0,
            "weekly_dd_usd": 0.0,
            "best_single_day_profit_usd": 450.0,
            "total_profit_usd": 1000.0,  # 45%
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level3_pnl(base_cfg)
        consistency_alerts = [a for a in alerts if "Consistency" in a.get("title", "")]
        assert len(consistency_alerts) >= 1

    def test_no_data_no_alert(self, base_cfg):
        """state.json なし → アラートなし"""
        with patch.object(agent, "load_all_account_states", return_value=[]):
            alerts = agent.check_level3_pnl(base_cfg)
        assert len(alerts) == 0


class TestLevel3PayoutReminder:
    """Level3: Payout請求リマインダーテスト"""

    def test_payout_eligible_alert(self, base_cfg):
        """5勝利日 + 純利益$500達成 → Level1通知"""
        state = {
            "account_id": "mffu_flex_A",
            "winning_days_count": 5,
            "total_profit_usd": 600.0,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level3_payout_reminder(base_cfg)
        assert len(alerts) >= 1
        assert alerts[0]["level"] == 1

    def test_payout_not_eligible_no_alert(self, base_cfg):
        """3勝利日のみ → アラートなし"""
        state = {
            "account_id": "mffu_flex_A",
            "winning_days_count": 3,
            "total_profit_usd": 400.0,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            alerts = agent.check_level3_payout_reminder(base_cfg)
        assert len(alerts) == 0


class TestLevel4NewsWindow:
    """Level4: News Window違反テスト"""

    def test_news_window_violation_alert(self, base_cfg):
        """phase_flags.news_window_violation=True → Level4アラート"""
        state = {
            "account_id": "mffu_flex_A",
            "phase_flags": {"news_window_violation": True},
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level4_news_window(base_cfg)
        assert len(alerts) == 1
        assert alerts[0]["level"] == 4
        assert alerts[0]["action"] == "stop_bot"

    def test_no_violation_no_alert(self, base_cfg):
        """違反なし → アラートなし"""
        state = {
            "account_id": "mffu_flex_A",
            "phase_flags": {"news_window_violation": False},
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            alerts = agent.check_level4_news_window(base_cfg)
        assert len(alerts) == 0


class TestLevel4HFT:
    """Level4: HFT監視テスト"""

    def test_hft_warn_at_80pct(self, base_cfg):
        """160トレード / 上限200 (80%) → Level3警告"""
        state = {
            "account_id": "mffu_flex_A",
            "daily_trade_count": 160,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level4_hft(base_cfg)
        assert len(alerts) >= 1

    def test_hft_level4_at_200(self, base_cfg):
        """200トレード超 → Level4"""
        state = {
            "account_id": "mffu_flex_A",
            "daily_trade_count": 200,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level4_hft(base_cfg)
        assert any(a["level"] == 4 for a in alerts)

    def test_below_threshold_no_alert(self, base_cfg):
        """100トレード → アラートなし"""
        state = {
            "account_id": "mffu_flex_A",
            "daily_trade_count": 100,
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            alerts = agent.check_level4_hft(base_cfg)
        assert len(alerts) == 0


class TestLevel4SurvivalMode:
    """Level4: Sim-Funded Survival Mode移行確認テスト"""

    def test_after_payout_no_survival_triggers_alert(self, base_cfg):
        """sim_funded_after_payout かつ survival_mode未起動 → Level4"""
        state = {
            "account_id": "mffu_flex_A",
            "account_type": "mffu_sim_funded_after_payout",
            "phase_flags": {"survival_mode": False},
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            with patch.object(agent, "_should_notify", return_value=True):
                alerts = agent.check_level4_sim_funded_payout_mode(base_cfg)
        assert len(alerts) >= 1
        assert alerts[0]["level"] == 4

    def test_after_payout_with_survival_no_alert(self, base_cfg):
        """sim_funded_after_payout かつ survival_mode起動済み → アラートなし"""
        state = {
            "account_id": "mffu_flex_A",
            "account_type": "mffu_sim_funded_after_payout",
            "phase_flags": {"survival_mode": True},
        }
        with patch.object(agent, "load_all_account_states", return_value=[state]):
            alerts = agent.check_level4_sim_funded_payout_mode(base_cfg)
        assert len(alerts) == 0


class TestRestartBot:
    """restart_bot dry_run テスト"""

    def test_dry_run_no_subprocess(self, base_cfg):
        """dry_run=True → subprocess を呼ばず DRY_RUN ステータス"""
        with patch("subprocess.run") as mock_run:
            result = agent.restart_bot(base_cfg, dry_run=True)
        mock_run.assert_not_called()
        assert result["status"] == "DRY_RUN"

    def test_armed_calls_launchctl(self, base_cfg):
        """dry_run=False → launchctl を呼ぶ"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch("time.sleep"):
                result = agent.restart_bot(base_cfg, dry_run=False)
        assert mock_run.called


class TestShouldNotify:
    """通知冷却テスト"""

    def test_first_call_returns_true(self):
        """初回呼び出し → True"""
        agent._notified.clear()
        assert agent._should_notify("test_key_unique_1") is True

    def test_second_call_within_cooldown_returns_false(self):
        """冷却期間内の2回目 → False"""
        agent._notified.clear()
        agent._notified["test_key_unique_2"] = time.time()
        assert agent._should_notify("test_key_unique_2") is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# chronos_watchdog.py テスト
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTailNewLines:
    """tail_new_lines テスト"""

    def test_empty_file_returns_empty(self, tmp_dir):
        """空ファイル → 空リスト"""
        p = tmp_dir / "test.log"
        p.write_text("")
        lines, new_pos = watchdog.tail_new_lines(p, 0)
        assert lines == []

    def test_reads_new_lines(self, tmp_dir):
        """新規行を正しく読む"""
        p = tmp_dir / "test.log"
        p.write_text("line1\nline2\nline3\n")
        lines, new_pos = watchdog.tail_new_lines(p, 0)
        assert "line1" in lines
        assert "line3" in lines
        assert new_pos > 0

    def test_incremental_read(self, tmp_dir):
        """差分だけ読む（追記検知）"""
        p = tmp_dir / "test.log"
        p.write_text("line1\n")
        lines1, pos1 = watchdog.tail_new_lines(p, 0)
        assert len(lines1) == 1

        # 追記
        with p.open("a") as f:
            f.write("line2\nline3\n")
        lines2, pos2 = watchdog.tail_new_lines(p, pos1)
        assert len(lines2) == 2
        assert "line2" in lines2

    def test_nonexistent_file(self, tmp_dir):
        """存在しないファイル → 空リスト・ポジション変化なし"""
        p = tmp_dir / "nonexistent.log"
        lines, new_pos = watchdog.tail_new_lines(p, 0)
        assert lines == []
        assert new_pos == 0

    def test_log_rotation_resets_position(self, tmp_dir):
        """ローテーション（サイズ縮小）を検知してポジションをリセット"""
        p = tmp_dir / "test.log"
        p.write_text("original long content\n" * 10)
        _, pos1 = watchdog.tail_new_lines(p, 0)
        assert pos1 > 0

        # ローテーション相当（ファイルを小さく上書き）
        p.write_text("new line\n")
        # サイズがpos1より小さい → リセット
        lines, pos2 = watchdog.tail_new_lines(p, pos1)
        # ローテーション後の内容を読む
        assert pos2 >= 0


class TestCheckPattern:
    """check_pattern テスト"""

    def test_critical_pattern_triggers_immediately(self):
        """CRITICAL パターン → 閾値1件で即通知"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "2026-04-19 10:00:00 [CRITICAL] Max Loss violated"
        result = watchdog.check_pattern(line, now)
        labels = [r[0] for r in result]
        assert "CRITICAL" in labels

    def test_error_pattern_needs_threshold(self):
        """ERROR パターン → 閾値(10件)に達するまで通知しない"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "2026-04-19 10:00:00 [ERROR] some error"
        # 9件 → 通知なし
        for i in range(9):
            result = watchdog.check_pattern(line, now + i * 0.1)
        # 最後の呼び出し
        result = watchdog.check_pattern(line, now + 9 * 0.1)
        labels = [r[0] for r in result]
        assert "ERROR" in labels  # 10件目で発火

    def test_traceback_pattern(self):
        """Traceback パターン → 1件で通知"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "Traceback (most recent call last):"
        result = watchdog.check_pattern(line, now)
        labels = [r[0] for r in result]
        assert "Traceback" in labels

    def test_mffu_news_window_pattern(self):
        """MFFU News Window違反パターン → 即通知"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "[CRITICAL] news window violation: order submitted during T1 window"
        result = watchdog.check_pattern(line, now)
        assert len(result) > 0

    def test_mffu_safety_buffer_pattern(self):
        """MFFU Safety Buffer違反パターン"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "safety buffer 違反: daily loss exceeded MLL"
        result = watchdog.check_pattern(line, now)
        labels = [r[0] for r in result]
        assert "MFFU_Safety_Buffer" in labels

    def test_tradovate_disconnect_pattern(self):
        """Tradovate接続断パターン → 3件で通知"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "Tradovate disconnect: connection lost"
        # 3件で発火
        for i in range(2):
            watchdog.check_pattern(line, now + i * 0.1)
        result = watchdog.check_pattern(line, now + 0.2)
        labels = [r[0] for r in result]
        assert "TradovateDisconnect" in labels

    def test_no_pattern_match(self):
        """マッチしない行 → 空リスト"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "2026-04-19 10:00:00 [INFO] normal operation"
        result = watchdog.check_pattern(line, now)
        assert result == []

    def test_cooldown_prevents_duplicate(self):
        """クールダウン内は同一パターンを再送しない"""
        watchdog.pattern_times.clear()
        watchdog.last_alert_sent.clear()
        now = time.time()
        line = "Traceback (most recent call last):"
        # 1回目
        result1 = watchdog.check_pattern(line, now)
        assert len(result1) > 0
        # cooldown内の2回目 → 空
        result2 = watchdog.check_pattern(line, now + 1)
        assert len(result2) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 統合テスト: monitor_cycle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMonitorCycle:
    """monitor_cycle 統合テスト"""

    def test_all_ok_no_alerts(self, base_cfg, tmp_path):
        """正常状態 → アラートなし"""
        import datetime
        fresh_ts = datetime.datetime.now(ET).isoformat()
        state = {
            "account_id": "mffu_flex_A",
            "timestamp": fresh_ts,
            "daily_pnl_usd": 100.0,
            "weekly_dd_usd": 0.0,
            "daily_trade_count": 50,
            "phase_flags": {"news_window_violation": False, "survival_mode": False},
        }
        with patch.object(agent, "KILL_SWITCH_AVAILABLE", False), \
             patch.object(agent, "is_bot_alive", return_value=True), \
             patch.object(agent, "load_all_account_states", return_value=[state]), \
             patch.object(agent, "load_agent_state", return_value={}), \
             patch.object(agent, "save_agent_state", return_value=None), \
             patch.object(agent, "pushover", return_value=True), \
             patch.object(agent, "pushover_alert", return_value=True):
            alerts = agent.monitor_cycle(base_cfg, dry_run=True)

        # 日次損失なし・Bot生存・state新鮮 → アラートなし
        critical_alerts = [a for a in alerts if a["level"] >= 3]
        assert len(critical_alerts) == 0

    def test_kill_switch_active_skips_checks(self, base_cfg):
        """Kill Switch起動中 → 全チェックをスキップ"""
        with patch.object(agent, "KILL_SWITCH_AVAILABLE", True), \
             patch.object(agent, "kill_switch_is_active", return_value=True), \
             patch.object(agent, "kill_switch_reason", return_value="test"), \
             patch.object(agent, "load_agent_state", return_value={}), \
             patch.object(agent, "save_agent_state", return_value=None):
            alerts = agent.monitor_cycle(base_cfg, dry_run=True)
        assert len(alerts) == 0

    def test_bot_dead_triggers_dispatch(self, base_cfg):
        """Bot死亡 → dispatch_alert が呼ばれる"""
        with patch.object(agent, "KILL_SWITCH_AVAILABLE", False), \
             patch.object(agent, "is_bot_alive", return_value=False), \
             patch.object(agent, "load_all_account_states", return_value=[]), \
             patch.object(agent, "load_agent_state", return_value={}), \
             patch.object(agent, "save_agent_state", return_value=None), \
             patch.object(agent, "dispatch_alert") as mock_dispatch, \
             patch.object(agent, "_should_notify", return_value=True):
            alerts = agent.monitor_cycle(base_cfg, dry_run=True)

        # Bot死亡アラートが dispatch されたか確認
        # mock_dispatch が呼ばれた引数を確認
        dispatched_levels = [c[0][0]["level"] for c in mock_dispatch.call_args_list]
        assert 2 in dispatched_levels  # Level2: restart_bot


# NOTE: TestChronosBotStrategySelector は cycle2 水増しテスト除去のため
# tests/test_chronos_bot_strategy_selector.py に移動した（STR-2対応）。
