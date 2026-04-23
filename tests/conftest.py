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


@pytest.fixture(autouse=True)
def _isolate_earnings_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """NAV-R3-2: EarningsEngine の EARNINGS_HISTORY_FILE を tmp_path に差し替えて state 汚染を防ぐ。

    問題:
    - common/earnings_engine.py の EARNINGS_HISTORY_FILE は
      モジュールレベルで評価された data/earnings_history.json を指す。
    - test_earnings.py の各テストは新しい EarningsEngine インスタンスを作成するが、
      _load_history() は EARNINGS_HISTORY_FILE から読み込む。
    - 別テスト（test_earnings.py::TestRecordOutcomeAndHistory など）が
      record_outcome("TSLA", ...) を呼ぶと実際の earnings_history.json に書き込まれ、
      後続テストの setUp で 3 件以上の実績が読み込まれ _get_iv_crush_rate が
      実績中央値を返すようになる（test_known_symbol_tsla: 0.35 != 0.4）。

    修正:
    - autouse=True で全テスト前に common.earnings_engine.EARNINGS_HISTORY_FILE を
      tmp_path/<test_name>/earnings_history.json に monkeypatch する。
    - common/earnings_engine.py は書き換え禁止のため conftest.py fixture で吸収する。
    - テストごとに独立した tmp ファイルを使うため cross-test contamination がゼロになる。
    """
    try:
        import common.earnings_engine as _ee
        isolated_history_file = tmp_path / "earnings_history.json"
        monkeypatch.setattr(_ee, "EARNINGS_HISTORY_FILE", isolated_history_file)
    except ImportError:
        pass  # common.earnings_engine が使えない環境（mutmut 等）は skip
    yield
