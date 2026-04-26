"""tests/test_mutation_config_20260425.py — mutmut config 妥当性テスト (2026-04-25)

mutmut の設定・対象ファイル・実行環境の妥当性を pytest で検証する。
asyncio 禁止 (B16 規律) — 全テストは純粋な同期コード。

TC-01: 対象 4 ファイルが実際に存在すること
TC-02: 対象ファイルが Python として正常 parse できること (AST 妥当性)
TC-03: mutmut が環境内で利用可能であること (shutil.which / subprocess)
TC-04: 対象ファイルに mutation しやすい比較演算子 / 数値リテラルが存在すること
TC-05: run_mutation_analysis.sh が実行権限を持ち bash シンタックスが妥当であること
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# mutation 対象 4 ファイル
MUTATION_TARGETS = [
    "atlas_v3/ops/chainguard_wrapper.py",
    "atlas_v3/ops/portfolio_risk_gate.py",
    "atlas_v3/ops/mass_verify_safe_runner.py",
    "atlas_v3/ops/moomoo_opend_relogin.py",
]

MUTATION_SCRIPT = PROJECT_ROOT / "scripts" / "run_mutation_analysis.sh"


# ── TC-01: 対象ファイル存在確認 ───────────────────────────────────────────────

class TestMutationTargetExistence:
    """TC-01: mutation 対象ファイルが存在すること。"""

    @pytest.mark.parametrize("rel_path", MUTATION_TARGETS)
    def test_target_file_exists(self, rel_path: str):
        """各対象ファイルがプロジェクトルートに存在すること。"""
        abs_path = PROJECT_ROOT / rel_path
        assert abs_path.is_file(), (
            f"Mutation target not found: {abs_path}\n"
            "run_mutation_analysis.sh の TARGETS リストと不整合。"
        )

    def test_all_targets_count(self):
        """対象ファイルが 4 件であること。"""
        assert len(MUTATION_TARGETS) == 4

    def test_targets_are_unique(self):
        """対象ファイルに重複がないこと。"""
        assert len(MUTATION_TARGETS) == len(set(MUTATION_TARGETS))


# ── TC-02: AST parse 妥当性 ───────────────────────────────────────────────────

class TestMutationTargetAstValidity:
    """TC-02: 対象ファイルが正常な Python AST であること。

    mutmut は AST walk で mutation 箇所を特定するため、parse エラーがあると
    mutmut 自体が失敗する。事前確認として pytest で保護する。
    """

    @pytest.mark.parametrize("rel_path", MUTATION_TARGETS)
    def test_ast_parse_succeeds(self, rel_path: str):
        """各対象ファイルが ast.parse() でエラーなく parse できること。"""
        abs_path = PROJECT_ROOT / rel_path
        if not abs_path.is_file():
            pytest.skip(f"File not found: {abs_path}")

        source = abs_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(abs_path))
        except SyntaxError as exc:
            pytest.fail(
                f"AST parse failed for {rel_path}: {exc}\n"
                "mutmut cannot run on files with syntax errors."
            )
        assert isinstance(tree, ast.Module)

    @pytest.mark.parametrize("rel_path", MUTATION_TARGETS)
    def test_no_bom_or_encoding_issue(self, rel_path: str):
        """対象ファイルに BOM や encoding 宣言の不整合がないこと。"""
        abs_path = PROJECT_ROOT / rel_path
        if not abs_path.is_file():
            pytest.skip(f"File not found: {abs_path}")

        raw = abs_path.read_bytes()
        # UTF-8 BOM (EF BB BF) がないこと
        assert not raw.startswith(b"\xef\xbb\xbf"), (
            f"BOM detected in {rel_path}. Remove BOM for mutmut compatibility."
        )


# ── TC-03: mutmut 環境確認 ────────────────────────────────────────────────────

class TestMutmutEnvironment:
    """TC-03: mutmut が環境内で利用可能であること。"""

    def test_mutmut_found_in_path_or_homebrew(self):
        """mutmut が PATH に存在するか /opt/homebrew/bin に存在すること。"""
        candidates = [
            shutil.which("mutmut"),
            "/opt/homebrew/bin/mutmut",
            "/usr/local/bin/mutmut",
        ]
        found = any(c and Path(c).is_file() for c in candidates)
        assert found, (
            "mutmut not found. Install: pip install mutmut  or  brew install mutmut\n"
            f"Checked: {candidates}"
        )

    def test_mutmut_version_output(self):
        """mutmut --version が exit code 0 で返ること。"""
        mutmut_path = shutil.which("mutmut") or "/opt/homebrew/bin/mutmut"
        if not Path(mutmut_path).is_file():
            pytest.skip("mutmut not found")

        result = subprocess.run(
            [mutmut_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"mutmut --version failed:\n{result.stderr}"
        )
        # バージョン文字列に数字が含まれること
        assert any(c.isdigit() for c in result.stdout + result.stderr), (
            f"Unexpected version output: {result.stdout!r}"
        )

    def test_pytest_available(self):
        """pytest が実行可能であること (mutmut の --runner に使用)。"""
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"pytest not available: {result.stderr}"
        )


# ── TC-04: mutation しやすい要素の存在確認 ────────────────────────────────────

class TestMutationTargetContent:
    """TC-04: 対象ファイルに mutation しやすい比較演算子・数値リテラルが存在すること。

    mutation testing が意味を持つためには、mutmut が書き換えられる要素が
    ソース内に存在する必要がある。
    """

    @pytest.mark.parametrize("rel_path", MUTATION_TARGETS)
    def test_has_comparison_operators(self, rel_path: str):
        """対象ファイルに比較演算子 (>=, <=, ==, !=) が 1 件以上あること。"""
        abs_path = PROJECT_ROOT / rel_path
        if not abs_path.is_file():
            pytest.skip(f"File not found: {abs_path}")

        source = abs_path.read_text(encoding="utf-8")
        has_comparison = any(op in source for op in (">=", "<=", "==", "!=", " > ", " < "))
        assert has_comparison, (
            f"{rel_path} has no comparison operators. "
            "mutation testing would generate 0 mutants."
        )

    @pytest.mark.parametrize("rel_path", MUTATION_TARGETS)
    def test_has_numeric_literals(self, rel_path: str):
        """対象ファイルに数値リテラルが 1 件以上あること。"""
        abs_path = PROJECT_ROOT / rel_path
        if not abs_path.is_file():
            pytest.skip(f"File not found: {abs_path}")

        source = abs_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        nums = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and node.value not in (0, 1, True, False)  # 自明な定数は除外
        ]
        assert len(nums) >= 1, (
            f"{rel_path} has no meaningful numeric literals for mutation. "
            f"mutmut would generate very few mutants."
        )

    def test_chainguard_stale_threshold_present(self):
        """chainguard_wrapper.py に stale_threshold 数値が存在すること。"""
        path = PROJECT_ROOT / "atlas_v3/ops/chainguard_wrapper.py"
        if not path.is_file():
            pytest.skip("chainguard_wrapper.py not found")
        source = path.read_text(encoding="utf-8")
        # デフォルト値 30.0 が存在すること
        assert "30.0" in source or "_DEFAULT_STALE_THRESHOLD_SECS" in source

    def test_portfolio_risk_gate_vix_threshold_present(self):
        """portfolio_risk_gate.py に VIX halt 閾値が存在すること。"""
        path = PROJECT_ROOT / "atlas_v3/ops/portfolio_risk_gate.py"
        if not path.is_file():
            pytest.skip("portfolio_risk_gate.py not found")
        source = path.read_text(encoding="utf-8")
        assert "30.0" in source  # vix_halt_threshold のデフォルト

    def test_mass_verify_has_lock_usage(self):
        """mass_verify_safe_runner.py に threading.Lock の使用が存在すること。"""
        path = PROJECT_ROOT / "atlas_v3/ops/mass_verify_safe_runner.py"
        if not path.is_file():
            pytest.skip("mass_verify_safe_runner.py not found")
        source = path.read_text(encoding="utf-8")
        assert "threading.Lock" in source or "Lock()" in source


# ── TC-05: run_mutation_analysis.sh 妥当性 ───────────────────────────────────

class TestMutationScript:
    """TC-05: run_mutation_analysis.sh が正常に設定されていること。"""

    def test_script_exists(self):
        """run_mutation_analysis.sh が存在すること。"""
        assert MUTATION_SCRIPT.is_file(), (
            f"Script not found: {MUTATION_SCRIPT}"
        )

    def test_script_is_executable(self):
        """run_mutation_analysis.sh が実行権限を持つこと。"""
        if not MUTATION_SCRIPT.is_file():
            pytest.skip("Script not found")
        assert os.access(MUTATION_SCRIPT, os.X_OK), (
            f"Script not executable: {MUTATION_SCRIPT}\n"
            "Fix: chmod +x scripts/run_mutation_analysis.sh"
        )

    def test_script_bash_syntax_check(self):
        """bash -n でシンタックスエラーがないこと。"""
        if not MUTATION_SCRIPT.is_file():
            pytest.skip("Script not found")
        bash_path = shutil.which("bash")
        if not bash_path:
            pytest.skip("bash not found")

        result = subprocess.run(
            [bash_path, "-n", str(MUTATION_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"bash syntax error in run_mutation_analysis.sh:\n{result.stderr}"
        )

    def test_script_contains_all_targets(self):
        """スクリプト内に 4 対象ファイルが全て記述されていること。"""
        if not MUTATION_SCRIPT.is_file():
            pytest.skip("Script not found")

        content = MUTATION_SCRIPT.read_text(encoding="utf-8")
        for target in MUTATION_TARGETS:
            filename = Path(target).name
            assert filename in content, (
                f"Target '{filename}' not found in run_mutation_analysis.sh. "
                "スクリプトの TARGETS 配列を確認すること。"
            )

    def test_script_has_report_generation(self):
        """スクリプトに surviving mutation レポート生成ロジックがあること。"""
        if not MUTATION_SCRIPT.is_file():
            pytest.skip("Script not found")

        content = MUTATION_SCRIPT.read_text(encoding="utf-8")
        assert "surviving" in content.lower() or "SURVIVED" in content, (
            "run_mutation_analysis.sh has no surviving mutation report logic."
        )
        assert "mutation_reports" in content or "REPORT_DIR" in content
