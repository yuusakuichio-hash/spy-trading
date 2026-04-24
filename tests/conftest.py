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
def _isolate_state_dirs(tmp_path, monkeypatch):
    """2026-04-24 22:58 JST 事故再発防止: pytest が本番 data/state_v3/ を汚染
    しないよう、全テストで env var + module attr の両方を tmp_path に差し替える。

    env var は新規 import 時の初期値に適用、monkeypatch.setattr は既 import 済
    module の module-level 定数を tmp_path に差し替える。両方必要なのは、
    モジュールが既に一度 import されてると os.getenv が effective にならない
    ため (Python の module load timing 制約)。

    対象:
    - common_v3.risk.kill_switch._STATE_DIR / FLAG_FILE / AUDIT_FILE
    - atlas_v3.ops.monitor._STATE_DIR / _MONITOR_LOG
    - atlas_v3.ops.latency_monitor._STATE_DIR / _LATENCY_LOG
    - atlas_v3.ops.moomoo_provider._HWM_STATE_FILE

    autouse=True で全テストに適用。本番環境 (ライブ実行) では env 未設定のため
    従来どおり data/state_v3/ を使用する (後方互換)。
    """
    from pathlib import Path as _P
    isolated_state = tmp_path / "state_v3"
    isolated_state.mkdir(exist_ok=True)
    hwm_path = tmp_path / "moomoo_hwm.json"
    monkeypatch.setenv("TRADING_STATE_DIR", str(isolated_state))
    monkeypatch.setenv("TRADING_MOOMOO_HWM_PATH", str(hwm_path))

    # 既 import 済 module の module-level 定数を直接差し替える
    _patches = [
        ("common_v3.risk.kill_switch", [
            ("_STATE_DIR", isolated_state),
            ("FLAG_FILE", isolated_state / "kill_switch.flag"),
            ("AUDIT_FILE", isolated_state / "kill_switch_audit.jsonl"),
        ]),
        ("atlas_v3.ops.monitor", [
            ("_STATE_DIR", isolated_state),
            ("_MONITOR_LOG", isolated_state / "monitor_state.jsonl"),
        ]),
        ("atlas_v3.ops.latency_monitor", [
            ("_STATE_DIR", isolated_state),
            ("_LATENCY_LOG", isolated_state / "latency_samples.jsonl"),
        ]),
        ("atlas_v3.ops.moomoo_provider", [
            ("_HWM_STATE_FILE", hwm_path),
        ]),
    ]
    for mod_name, attrs in _patches:
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            for attr_name, attr_value in attrs:
                if hasattr(mod, attr_name):
                    monkeypatch.setattr(mod, attr_name, attr_value)
        except ImportError:
            pass  # futu SDK 未インストール環境等
    yield


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
