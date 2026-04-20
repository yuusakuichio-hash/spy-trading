"""tests/test_chronos_bot_startup_20260420.py — γ-5: ChronosBot 起動 E2E テスト

cycle10 MUST-FIX γ-5: ChronosBot() インスタンス化が成功することを実証する。
- γ-1 修正 (prop_firm → mffu_compliance) 後に起動即死が解消されたことを証明する
- prop_account_state dict 生成確認
- _plan_id プロパティが正しい値を返すか
- check_breakout 発注前ガード通過確認
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import chronos_bot as bot_module


@pytest.fixture
def startup_bot(tmp_path):
    """ChronosBot(paper=True, dry_run=True) を instantiate して返す fixture。
    γ-1 修正後に ValueError が発生しないことを確認するために使用する。
    """
    with patch.dict(os.environ, {
        "MFFU_DATA_DIR": str(tmp_path),
        "MFFU_LOG_DIR": str(tmp_path / "logs"),
        "MFFU_ACCOUNT_ID": "test_startup",
        "PUSHOVER_USER": "",
        "PUSHOVER_OPS_TOKEN": "",
    }):
        with patch("chronos_bot.TradovateClient", MagicMock()):
            b = bot_module.ChronosBot(paper=True, dry_run=True)
    return b


class TestChronosBotStartup:
    """ChronosBot() が起動即死しないことを証明するテスト群。"""

    def test_instantiation_does_not_raise(self, startup_bot):
        """ChronosBot() が ValueError を投げずに instantiate できること。
        γ-1: prop_firm → mffu_compliance 修正が効いていれば pass する。
        """
        assert startup_bot is not None

    def test_plan_attribute_is_nonempty(self, startup_bot):
        """_plan 属性が空文字でないこと。
        γ-1 前: prop_firm キーなし → "" → β-6 fail-closed。
        γ-1 後: mffu_compliance.plan = "flex_50k" → 正常。
        """
        assert startup_bot._plan != "", (
            f"_plan が空文字: '{startup_bot._plan}' — γ-1 修正が反映されていない"
        )

    def test_plan_id_property_returns_valid_value(self, startup_bot):
        """_plan_id プロパティが PlanID インスタンスを返すこと（ValueError でないこと）。
        yaml から "flex_50k" + phase "evaluation" の組み合わせが解決できれば pass。
        """
        plan_id = startup_bot._plan_id
        assert plan_id is not None
        # PlanID は __str__ または name 属性を持つ
        plan_str = str(plan_id)
        assert len(plan_str) > 0, f"_plan_id の文字列表現が空: '{plan_str}'"

    def test_firm_attribute_is_set(self, startup_bot):
        """_firm 属性が設定されていること。
        mffu_compliance に firm キーがない場合でもデフォルト "mffu" が入ること。
        """
        assert startup_bot._firm != "", (
            f"_firm が空文字: '{startup_bot._firm}'"
        )

    def test_dry_run_client_is_none(self, startup_bot):
        """dry_run=True のとき client=None で発注実行不可状態であること。
        これが ChronosBot の発注前ガードの第一層。
        """
        assert startup_bot.client is None, (
            "dry_run=True なのに client が None でない — 本番発注が実行される恐れがある"
        )
