"""tests/test_chronos_bot_strategy_selector.py — chronos_bot STRATEGY_SELECTOR 確認テスト

cycle2 STR-2: TestChronosBotStrategySelector を
test_chronos_agent_watchdog_20260419.py から分離して独立ファイル化。
水増し扱いを解消し、単体の設計規律テストとして適切な場所に配置する。

テスト目的:
  - chronos_bot.py が Atlas の strategy_selector（SPY/SPXオプション向け）を
    先物環境で使用しないことを設計上の制約として確認する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


class TestChronosBotStrategySelector:
    """chronos_bot.py の strategy_selector 除外確認テスト"""

    def test_strategy_selector_unavailable_in_chronos_bot(self):
        """chronos_bot.py: STRATEGY_SELECTOR_AVAILABLE = False

        設計規律: chronos_bot はオプション向け strategy_selector を使用しない。
        先物専用セレクター (chronos_strategy_selector) を使用する。
        この定数が True になっていたら設計違反。
        """
        import chronos_bot
        assert chronos_bot.STRATEGY_SELECTOR_AVAILABLE is False, (
            "STRATEGY_SELECTOR_AVAILABLE が True になっています。"
            "chronos_bot は先物専用設計であり、"
            "SPY/SPXオプション向け strategy_selector を使用してはなりません。"
        )
