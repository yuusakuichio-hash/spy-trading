"""tests/test_engine_multi_symbol_skip_ratelimit_20260426.py

検証: AtlasEngine._dispatch_enter_exit が multi-symbol tactic から ValueError を受けた時、
初回のみ INFO ログを出し、同一 tactic_name の 2 回目以降は DEBUG に降格すること。

背景:
β-2 最小配線で _dispatch_enter_exit は symbol="" を渡す。multi-symbol tactic
（weekly_gamma_scalp 等）は空 symbol で ValueError を raise する設計。skip 自体は
正当だが毎 tick INFO が出ると atlas-trader stderr が肥大化する。
2026-04-26 fix で同 tactic_name の skip log を初回 INFO・以降 DEBUG にした。
"""
from __future__ import annotations

import logging

import pytest
from unittest.mock import MagicMock

from atlas_v3.core.engine import AtlasEngine
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase


class _MultiSymbolStubTactic(TacticBase):
    tactic_name = "multi_symbol_stub"
    tactic_type = "enter_exit"

    def preflight(self, env):
        return True

    def should_enter(self, env, symbol: str = ""):
        if not symbol:
            raise ValueError(f"symbol='{symbol}' は非対応銘柄です。対応: ['IWM', 'QQQ', 'SPY']")
        return None

    def should_exit(self, env, position):
        return None

    def build_order(self, env, decision):
        return None


def _make_engine() -> AtlasEngine:
    return AtlasEngine(
        market_data=MagicMock(),
        broker=MagicMock(),
        tactics=[_MultiSymbolStubTactic()],
    )


def _env() -> MarketEnvironment:
    return MarketEnvironment(vix=15.0, ivr_by_symbol={})


def test_first_skip_logs_at_info(caplog):
    """初回 skip は INFO レベルで記録される。"""
    engine = _make_engine()
    tactic = engine._tactics[0]
    with caplog.at_level(logging.INFO, logger="atlas_v3.core.engine"):
        engine._dispatch_enter_exit(tactic, _env())

    info_messages = [r for r in caplog.records if r.levelname == "INFO"]
    assert any("multi-symbol or invalid symbol" in r.getMessage() for r in info_messages), (
        f"初回 skip は INFO で出るべき。records={[r.getMessage() for r in caplog.records]}"
    )


def test_second_skip_demotes_to_debug(caplog):
    """同一 tactic_name の 2 回目以降の skip は DEBUG に降格する。"""
    engine = _make_engine()
    tactic = engine._tactics[0]

    # 初回（INFO 出る）
    engine._dispatch_enter_exit(tactic, _env())

    # 2 回目（DEBUG に降格・INFO に出ないこと検証）
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="atlas_v3.core.engine"):
        engine._dispatch_enter_exit(tactic, _env())

    info_messages = [r for r in caplog.records if r.levelname == "INFO"]
    debug_messages = [r for r in caplog.records if r.levelname == "DEBUG"]

    assert not any("multi-symbol or invalid symbol" in r.getMessage() for r in info_messages), (
        f"2 回目は INFO に出てはいけない (rate-limit 違反)。info={[r.getMessage() for r in info_messages]}"
    )
    assert any("multi-symbol or invalid symbol" in r.getMessage() for r in debug_messages), (
        f"2 回目は DEBUG に降格して出るべき (silent skip 禁止規律)。debug={[r.getMessage() for r in debug_messages]}"
    )


def test_seen_set_persists_across_ticks():
    """_multi_symbol_skip_seen state が tick 間で保持される。"""
    engine = _make_engine()
    tactic = engine._tactics[0]
    assert "multi_symbol_stub" not in engine._multi_symbol_skip_seen

    engine._dispatch_enter_exit(tactic, _env())
    assert "multi_symbol_stub" in engine._multi_symbol_skip_seen

    # 2 回目呼んでも set サイズは変わらない
    engine._dispatch_enter_exit(tactic, _env())
    assert engine._multi_symbol_skip_seen == {"multi_symbol_stub"}
