"""tests/conftest.py — テストディレクトリ用 pytest 設定

mutmut 3.x は mutants/ ディレクトリから pytest を実行するため、
common/ の一部ファイルが mutants/common/ にコピーされていない場合に
ImportError が発生する可能性がある。

このファイルは tests/ に配置されているため mutants/tests/ にも自動コピーされ、
mutants/ 環境での collection error を防ぐ。
"""

import sys
from pathlib import Path

# MF-9 fix: mutants/ から実行時に プロジェクトルートを sys.path に追加する
_tests_dir = Path(__file__).parent
_project_dir = _tests_dir.parent
_project_parent = _project_dir.parent

for _path in [str(_project_dir), str(_project_parent)]:
    if _path not in sys.path:
        sys.path.insert(0, _path)
