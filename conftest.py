"""conftest.py — pytest グローバル設定

カスタムマーク登録:
  slow: 実 launchctl / 外部プロセスを使う integration test。
        CI では `pytest -m "not slow"` でスキップする。
"""

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow integration tests (real launchctl, deselect with -m 'not slow')",
    )
