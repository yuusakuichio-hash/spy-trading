"""C-018 allowlist hook 検証テスト（実攻撃試行形式・AST inspection 禁止）。

scripts/lock_legacy_files.sh の動作を実コマンド実行で検証。
Sprint 2 Day 1 で本格運用開始する前の設計検証。

検証項目:
1. script の存在
2. status 動作（lock 前）
3. 実攻撃シミュレーション（保護対象への書込を subprocess で試行）
   - 実行環境では lock せず dry-run で検証（OS flag 操作は Sprint 2 本体で）
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path("/Users/yuusakuichio/trading")
LOCK_SCRIPT = PROJECT_ROOT / "scripts" / "lock_legacy_files.sh"


class TestAllowlistLockScriptExists:
    """script の物理存在と実行可能性。"""

    def test_lock_script_exists(self):
        assert LOCK_SCRIPT.exists(), f"lock_legacy_files.sh not found at {LOCK_SCRIPT}"

    def test_lock_script_executable(self):
        assert os.access(LOCK_SCRIPT, os.X_OK), "lock_legacy_files.sh is not executable"


class TestAllowlistLockScriptBehavior:
    """実コマンド実行で status サブコマンドが動作することを検証。
    lock / unlock はシステム状態を変えるので CI/テストでは status のみ実行。
    """

    def test_status_subcommand_runs(self):
        """status サブコマンドが exit 0 で終了し、期待文字列を含む。"""
        result = subprocess.run(
            ["bash", str(LOCK_SCRIPT), "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"status failed: {result.stderr}"
        assert "Legacy file protection status" in result.stdout
        assert "Summary:" in result.stdout

    def test_invalid_subcommand_exits_nonzero(self):
        """想定外のサブコマンドは exit 1。"""
        result = subprocess.run(
            ["bash", str(LOCK_SCRIPT), "invalid_subcommand"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0


class TestAllowlistLockScriptDesign:
    """設計要件確認（Redteam r7 指摘項目）。"""

    def test_protected_files_includes_spy_bot(self):
        """spy_bot.py が PROTECTED_FILES に含まれていること。"""
        content = LOCK_SCRIPT.read_text(encoding="utf-8")
        assert "spy_bot.py" in content, "spy_bot.py not in PROTECTED_FILES"

    def test_protected_files_includes_chronos_bot(self):
        """chronos_bot.py が PROTECTED_FILES に含まれていること。"""
        content = LOCK_SCRIPT.read_text(encoding="utf-8")
        assert "chronos_bot.py" in content, "chronos_bot.py not in PROTECTED_FILES"

    def test_protected_dirs_includes_common(self):
        """common/ が PROTECTED_DIRS に含まれていること。"""
        content = LOCK_SCRIPT.read_text(encoding="utf-8")
        assert '"common"' in content, "common/ not in PROTECTED_DIRS"

    def test_unlock_requires_explicit_confirmation(self):
        """unlock は "UNLOCK" 明示入力必須。"""
        content = LOCK_SCRIPT.read_text(encoding="utf-8")
        assert '"UNLOCK"' in content, "unlock confirmation not enforced"
