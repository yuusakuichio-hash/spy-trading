#!/usr/bin/env python3
"""
test_mffu_selector_integration.py — mffu_strategy_selector 統合テスト

検証内容:
  - 12戦術全てが check_entry を呼ぶ (dry_run 相当)
  - [StrategyCheck] ログが 3戦術（vix_term_structure/es_nq_spread/level_trading）から出力される
  - select_futures_strategy が env_score を書き戻す
  - B-3/V2-1 修正確認
"""

import logging
import pytest

# ── テスト共通データ ───────────────────────────────────────────────────────────

def make_full_env():
    """12戦術全てが評価されるよう全データを揃えた env dict を返す。"""
    from chronos_strategy_selector import build_env_dict
    env = build_env_dict(
        vix               = 20.0,
        vix_history       = [18.0, 19.0, 20.0, 21.0, 22.0] * 12,   # 60日分
        vix_z             = 0.0,
        time_et           = "10:30",
        account_pnl_day   = 0.0,
        account_pnl_month = 0.0,
        account_balance   = 50_000.0,
        consistency_used  = 0.0,
        gap_pct           = 0.5,
        sma20_vs_sma50    = "above",
        session           = "us_open",
        atr_5d            = 5.0,
    )
    # P1 / P2 戦術用フィールド
    env["current_price"] = 5500.0
    env["atr"]           = 10.0
    # Level Trading
    env["ib_finalized"]        = True
    env["current_volume"]      = 1000
    env["ib_high"]             = 5520.0
    env["ib_low"]              = 5480.0
    env["vwap"]                = 5500.0
    env["level_trading_levels"] = {
        "R1": 5520.0, "S1": 5480.0, "L4": 5460.0, "H4": 5540.0
    }
    # VIX Term Structure
    env["vix3m"] = 22.0
    env["vix6m"] = 23.0
    # ES-NQ Spread
    env["es_price"] = 5500.0
    env["nq_price"] = 19000.0
    env["es_nq_ratio_history"] = [5500 / 19000] * 20   # 20日分
    return env


# ── 統合テスト ────────────────────────────────────────────────────────────────

class TestAllStrategiesEvaluated:
    """12戦術全てが check_entry を呼ぶことを確認するテスト。"""

    def test_selector_returns_list(self):
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        result = select_futures_strategy(env)
        assert isinstance(result, list), "select_futures_strategy must return list"
        assert len(result) >= 1, "Must return at least 1 strategy"

    def test_env_score_written_back(self):
        """B-1修正: env_score が env dict に書き戻されること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        select_futures_strategy(env)
        assert "env_score" in env, "env_score must be written back to env dict"
        assert 0.0 < env["env_score"] <= 100.0, f"env_score={env['env_score']} out of range"

    def test_strategy_check_logs_emitted(self, caplog):
        """[StrategyCheck] ログが vix_term_structure/es_nq_spread/level_trading に対して出力されること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        with caplog.at_level(logging.INFO, logger="mffu_strategy_selector"):
            select_futures_strategy(env)

        log_text = caplog.text
        assert "[StrategyCheck] vix_term_structure:" in log_text, (
            "vix_term_structure StrategyCheck log missing"
        )
        assert "[StrategyCheck] es_nq_spread:" in log_text, (
            "es_nq_spread StrategyCheck log missing"
        )
        assert "[StrategyCheck] level_trading:" in log_text, (
            "level_trading StrategyCheck log missing"
        )

    def test_vix_term_structure_active_with_vix3m(self, caplog):
        """vix3m が提供されれば vix_term_structure: active=True になること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        with caplog.at_level(logging.INFO, logger="mffu_strategy_selector"):
            select_futures_strategy(env)
        assert "vix_term_structure: active=True" in caplog.text

    def test_es_nq_spread_active_with_prices(self, caplog):
        """es_price/nq_price/ratio_history が提供されれば es_nq_spread: active=True になること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        with caplog.at_level(logging.INFO, logger="mffu_strategy_selector"):
            select_futures_strategy(env)
        assert "es_nq_spread: active=True" in caplog.text

    def test_level_trading_active_with_levels(self, caplog):
        """level_trading_levels/ib_finalized/current_price が揃えば level_trading: active=True になること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        with caplog.at_level(logging.INFO, logger="mffu_strategy_selector"):
            select_futures_strategy(env)
        assert "level_trading: active=True" in caplog.text

    def test_vix_term_structure_inactive_without_vix3m(self, caplog):
        """vix3m が欠損している場合 vix_term_structure: active=False になること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        del env["vix3m"]
        with caplog.at_level(logging.INFO, logger="mffu_strategy_selector"):
            select_futures_strategy(env)
        assert "vix_term_structure: active=False" in caplog.text

    def test_es_nq_spread_inactive_without_prices(self, caplog):
        """es_price が欠損している場合 es_nq_spread: active=False になること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        del env["es_price"]
        with caplog.at_level(logging.INFO, logger="mffu_strategy_selector"):
            select_futures_strategy(env)
        assert "es_nq_spread: active=False" in caplog.text

    def test_level_trading_inactive_without_ib_finalized(self, caplog):
        """ib_finalized=False の場合 level_trading: active=False になること。"""
        from chronos_strategy_selector import select_futures_strategy
        env = make_full_env()
        env["ib_finalized"] = False
        with caplog.at_level(logging.INFO, logger="mffu_strategy_selector"):
            select_futures_strategy(env)
        assert "level_trading: active=False" in caplog.text

    def test_no_trade_returns_when_daily_loss_floor(self):
        """daily_loss_floor 以下の場合 no_trade が返ること。"""
        from chronos_strategy_selector import select_futures_strategy, build_env_dict
        env = build_env_dict(
            vix             = 20.0,
            vix_history     = [20.0] * 60,
            vix_z           = 0.0,
            time_et         = "10:30",
            account_pnl_day = -2000.0,   # 大きな損失
            account_balance = 50_000.0,
        )
        result = select_futures_strategy(env)
        assert result[0]["strategy"] == "no_trade"


# ── V2-1: ESNQSpread PnL積算テスト ───────────────────────────────────────────

class TestESNQSpreadPnL:
    """V2-1修正: close_position が pnl を返すこと。"""

    def test_close_position_returns_pnl_field(self):
        from futures_es_nq_spread import ESNQSpreadStrategy
        strat = ESNQSpreadStrategy()
        # ダミーポジションを設定
        strat._position = {"es": "short", "nq": "long", "z": 2.5, "strategy": "es_nq_spread"}
        strat._entry_z  = 2.5

        closed = strat.close_position("mean_reversion_complete", pnl=150.0)
        assert closed is not None
        assert "pnl" in closed
        assert closed["pnl"] == 150.0
        assert closed["close_reason"] == "mean_reversion_complete"
        assert strat._position is None

    def test_close_position_default_pnl_is_zero(self):
        from futures_es_nq_spread import ESNQSpreadStrategy
        strat = ESNQSpreadStrategy()
        strat._position = {"es": "long", "nq": "short", "z": -2.5, "strategy": "es_nq_spread"}
        strat._entry_z  = -2.5

        closed = strat.close_position("stop_loss")
        assert closed["pnl"] == 0.0

    def test_close_position_none_when_no_position(self):
        from futures_es_nq_spread import ESNQSpreadStrategy
        strat = ESNQSpreadStrategy()
        result = strat.close_position("eod_force_close", pnl=0.0)
        assert result is None
