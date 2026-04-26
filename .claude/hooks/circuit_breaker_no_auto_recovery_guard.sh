#!/usr/bin/env bash
# circuit_breaker_no_auto_recovery_guard.sh
# PreToolUse (Write|Edit|NotebookEdit) hook
# purpose: CircuitBreaker(auto_recovery=True) の早期検出（補助 AST hook）
# 主防御: CircuitBreaker.__init__ runtime guard (common_v3/self_healing/circuit_breaker.py)
# 補助防御: 本 hook による Write/Edit 時の静的早期検出
# bypass: CIRCUIT_BREAKER_GUARD_BYPASS=1
# spec ref: data/specs/v3/common_spec_v3_20260422.md B14 L356-L385
# ADR: data/decisions/ADR-007-circuit-breaker-runtime-guard-first.md
# 注意: AST は dataflow/alias/動的属性を原理的に追えない → bypass は xfail 想定
#       それらの bypass は runtime guard (circuit_breaker.py) が塞ぐ

set -euo pipefail

BYPASS_VAR_NAME="CIRCUIT_BREAKER_GUARD_BYPASS"
SPEC_REF="data/specs/v3/common_spec_v3_20260422.md B14 L356-L385"
BYPASS_LOG="data/governance/bypass_log.jsonl"

if [ "${CIRCUIT_BREAKER_GUARD_BYPASS:-0}" = "1" ]; then
  _TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown")
  _REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
  _LOG_PATH="${_REPO_ROOT}/${BYPASS_LOG}"
  if [ -d "$(dirname "$_LOG_PATH")" ]; then
    printf '{"timestamp":"%s","hook":"circuit_breaker_no_auto_recovery_guard","tool_name":"(bypass)","file_path":"(bypass)"}\n' \
      "$_TIMESTAMP" >> "$_LOG_PATH" 2>/dev/null || true
  fi
  exit 0
fi

INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE"

python3 - \
  "$INPUT_FILE" \
  "$SPEC_REF" \
  "$BYPASS_VAR_NAME" \
  "$BYPASS_LOG" \
<<'PYEOF'
import sys
import json
import ast
import subprocess
from pathlib import Path

input_path, spec_ref, bypass_var, bypass_log_rel = sys.argv[1:5]


def _get_repo_root():
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        if out:
            return Path(out).resolve()
    except Exception:
        pass
    try:
        return Path(__file__).resolve().parent.parent.parent
    except Exception:
        pass
    return None


REPO_ROOT = _get_repo_root()


def _normalize_to_relative(file_path):
    if not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        return file_path
    if REPO_ROOT is None:
        return file_path
    try:
        return str(p.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return None


TARGET_CLASSES = {"CircuitBreaker", "CircuitBreakerBackend"}
AUTO_RECOVERY_KWARG = "auto_recovery"


def _ast_is_truthy_constant(node):
    if isinstance(node, ast.Constant) and node.value is True:
        return True
    if isinstance(node, ast.Name) and node.id == "True":
        return True
    return False


def _string_match_fallback(code):
    """SyntaxError fallback: 正規表現で auto_recovery=True を検出。
    AST parse 失敗時の安全側フォールバック。攻撃者が意図的に SyntaxError を
    埋め込んで hook を通過するのを防ぐ。
    設計注記: これは補助 hook のフォールバック。主防御は runtime guard。
    """
    import re
    pattern = r'CircuitBreaker\s*\(.*?auto_recovery\s*=\s*True'
    match = re.search(pattern, code, re.DOTALL)
    if match:
        return ["L?: CircuitBreaker(..., auto_recovery=True) は禁止です (string match fallback)。"]
    return []


def detect_auto_recovery_true(code):
    violations = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        sys.stderr.write(
            "[CIRCUIT_BREAKER_GUARD] WARNING: AST parse 失敗・string match fallback 実行中\n"
        )
        return _string_match_fallback(code)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            class_name = func.id
        elif isinstance(func, ast.Attribute):
            class_name = func.attr
        else:
            continue
        if class_name not in TARGET_CLASSES:
            continue
        for kw in node.keywords:
            if kw.arg == AUTO_RECOVERY_KWARG and _ast_is_truthy_constant(kw.value):
                try:
                    val_str = ast.unparse(kw.value)
                except Exception:
                    val_str = "?"
                violations.append(
                    "L{}: {}(..., {}={}) は禁止です。auto_recovery は False 固定。".format(
                        node.lineno, class_name, AUTO_RECOVERY_KWARG, val_str
                    )
                )
    return violations


def main():
    try:
        with open(input_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return 0
    tool_name = data.get("tool_name", "")
    if tool_name not in ("Write", "Edit", "NotebookEdit"):
        return 0
    tool_input = data.get("tool_input") or {}
    raw_file_path = tool_input.get("file_path") or ""
    code = tool_input.get("new_string") or tool_input.get("content") or ""
    if not code.strip():
        return 0
    if not raw_file_path.endswith(".py"):
        return 0
    rel_path = _normalize_to_relative(raw_file_path)
    if rel_path is None:
        return 0
    violations = detect_auto_recovery_true(code)
    if not violations:
        return 0
    sys.stderr.write("[CIRCUIT_BREAKER_GUARD] BLOCKED:\n")
    for v in violations:
        sys.stderr.write("  - {}\n".format(v))
    sys.stderr.write("  file: {}\n".format(raw_file_path))
    sys.stderr.write("  spec: {}\n".format(spec_ref))
    sys.stderr.write(
        "  補助 AST hook: 変数/alias/動的属性 bypass は runtime guard がカバー\n"
    )
    sys.stderr.write("  bypass: export {}=1\n".format(bypass_var))
    return 2


sys.exit(main())
PYEOF
