"""tests/conftest.py — テストディレクトリ用 pytest 設定

mutmut 3.x は mutants/ ディレクトリから pytest を実行するため、
common/ の一部ファイルが mutants/common/ にコピーされていない場合に
ImportError が発生する可能性がある。

このファイルは tests/ に配置されているため mutants/tests/ にも自動コピーされ、
mutants/ 環境での collection error を防ぐ。
"""

import sys
from pathlib import Path

import pytest

# MF-9 fix: mutants/ から実行時に プロジェクトルートを sys.path に追加する
_tests_dir = Path(__file__).parent
_project_dir = _tests_dir.parent
_project_parent = _project_dir.parent

for _path in [str(_project_dir), str(_project_parent)]:
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture(autouse=True)
def _reset_risk_engine_escalation_state():
    """CR-1: 各テスト前後に _ESCALATION_LAST_SENT をリセットする。

    common_v3.risk.engine の 10s debounce タイマーはモジュールグローバル変数。
    テスト間で状態が残ると back-to-back テストが debounce に引っかかり
    C-γ テストが order-dependent に失敗する。
    autouse=True で全テストに適用（debounce を使わないテストへの影響はなし）。
    """
    try:
        from common_v3.risk.engine import _reset_escalation_state_for_test
        _reset_escalation_state_for_test()
    except ImportError:
        pass  # common_v3 が使えない環境（mutmut 等）は skip
    yield
    try:
        from common_v3.risk.engine import _reset_escalation_state_for_test
        _reset_escalation_state_for_test()
    except ImportError:
        pass
