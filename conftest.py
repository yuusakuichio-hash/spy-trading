"""conftest.py — pytest グローバル設定

カスタムマーク登録:
  slow: 実 launchctl / 外部プロセスを使う integration test。
        CI では `pytest -m "not slow"` でスキップする。
"""

import sys
from pathlib import Path
import pytest

# MF-9 fix: mutmut 3.x は mutants/ ディレクトリから pytest を実行するため
# プロジェクトルートの common/ が参照できなくなる問題を防ぐ。
# conftest.py の親ディレクトリをたどってプロジェクトルートを sys.path に追加する。
_conf_dir = Path(__file__).parent
_project_root = str(_conf_dir.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_this_dir = str(_conf_dir)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

# MF-9 fix: mutmut 3.x の stats collection は mutants/ から全 tests/ を collect しようとする。
# common/ の一部ファイルが mutants/common/ にコピーされていない場合、
# それらをインポートするテストで collection error が発生する。
# `collect_ignore_glob` で ImportError を起こす可能性があるテストを
# mutants/ 環境実行時に除外する。
_in_mutants_env = "mutants" in str(_conf_dir)

collect_ignore_glob = []
if _in_mutants_env:
    # mutants/ から実行時: prop_firm_cross_account 等が mutants/common/ に
    # コピーされていないため ImportError が起きるファイルを除外する
    collect_ignore_glob = [
        "tests/test_prop_firm_redteam.py",
        "tests/test_prop_firm_rules_yaml.py",
        "tests/test_chronos_bot_e2e.py",
    ]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow integration tests (real launchctl, deselect with -m 'not slow')",
    )
