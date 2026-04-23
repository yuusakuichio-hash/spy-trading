#!/usr/bin/env bash
# executor_sync_only_guard.sh
# PreToolUse (Write|Edit|NotebookEdit) hook: common_v3/executor の AST 検査
# kill_switch / idempotency の並列化禁止を物理強制
# 仕様: data/specs/v3/common_spec_v3_20260422.md B16 L474-L482
# bypass: EXECUTOR_SYNC_GUARD_BYPASS=1

set -euo pipefail

BYPASS_VAR_NAME="EXECUTOR_SYNC_GUARD_BYPASS"
ASYNC_IMPL_ALLOWED_SUFFIX="common_v3/executor/async_impl.py"
SPEC_REF="data/specs/v3/common_spec_v3_20260422.md B16"

if [ "${EXECUTOR_SYNC_GUARD_BYPASS:-0}" = "1" ]; then
  exit 0
fi

INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE"

python3 - "$INPUT_FILE" "$ASYNC_IMPL_ALLOWED_SUFFIX" "$SPEC_REF" "$BYPASS_VAR_NAME" <<'PY'
import sys
import json
import ast

NUM_EXPECTED_ARGS: int = 4  # input_path + async_allowed_suffix + spec_ref + bypass_var
ARGV_SLICE_END: int = NUM_EXPECTED_ARGS + 1

# argv 境界検査（呼出し元 bash が引数を揃えていない場合の早期エラー）
if len(sys.argv) < ARGV_SLICE_END:
    sys.stderr.write(
        f"[EXECUTOR_SYNC_GUARD] internal error: expected {NUM_EXPECTED_ARGS} args, "
        f"got {len(sys.argv) - 1}\n"
    )
    sys.exit(1)

assert len(sys.argv) >= ARGV_SLICE_END, "argv guard above should have exited"

input_path, async_allowed_suffix, spec_ref, bypass_var = sys.argv[1:ARGV_SLICE_END]

FORBIDDEN_IN_EXECUTOR = frozenset({
    "check_kill_switch",
    "is_active",
    "activate",
    "deactivate",
    "check_and_mark",
})
assert len(FORBIDDEN_IN_EXECUTOR) > 0, "FORBIDDEN set must not be empty"

try:
    with open(input_path) as f:
        data = json.load(f)
except (json.JSONDecodeError, UnicodeDecodeError, OSError):
    # Fail-closed policy (C-09 fix 2026-04-23):
    # Claude Code は正常な JSON payload を送る前提だが、
    # malformed payload は「壊れたセンサ」と同等（Boeing 737MAX MCAS 型 fail-open を防ぐ）。
    # 壊れた payload はリスクゼロではないため block する。
    sys.exit(2)

tool_name = data.get("tool_name", "")
if tool_name not in ("Write", "Edit", "NotebookEdit"):
    sys.exit(0)

tool_input = data.get("tool_input", {})
# Fail-closed type guard (C-09 fix 2026-04-23):
# Claude Code hook の tool_input は dict 以外を送ることはない仕様だが、
# str/list/int/None などが来た場合は malformed payload として block する（fail-closed）。
# rc=2 のみが Claude Code で block 扱い。rc=1 は warning として allow されるため、
# AttributeError で rc=1 になると規律違反コードが通過する（Boeing 737MAX 型）。
if not isinstance(tool_input, dict):
    sys.stderr.write(
        f"[EXECUTOR_SYNC_GUARD] BLOCKED: tool_input is not a dict "
        f"(type={type(tool_input).__name__}) — malformed payload, fail-closed\n"
    )
    sys.exit(2)
tool_input = tool_input or {}
file_path = tool_input.get("file_path", "") or ""
if not file_path.endswith(".py"):
    sys.exit(0)

code = (
    tool_input.get("new_string")
    or tool_input.get("content")
    or ""
)
if not code.strip():
    sys.exit(0)

try:
    tree = ast.parse(code, filename=file_path)
except SyntaxError:
    # SyntaxError 時 ALLOW（fail-open）の明示的理由 (C-10 fix 2026-04-23):
    # Claude Code は syntactically valid な Python のみを生成する前提。
    # 部分編集（Edit ツール）の中間状態が一時的に SyntaxError になる場合があり、
    # そのケースを block すると正常な Edit 操作も止まる（偽陽性が実害を生む）。
    # 段階的 inject 攻撃への対策: AST が通った時点で本 hook が再検査する（Edit 完了時）。
    # また CI / pytest 全件実行により SyntaxError + 後修正パターンは別層で検出される。
    # 注意: この設計は AST hook が「最外層」に過ぎないことを意味する。
    #       runtime guard（@sync_only デコレータ）が最終防衛線（Phase 2 Sprint 1 予定）。
    sys.exit(0)

violations = []

is_async_impl = file_path.endswith(async_allowed_suffix)

def _get_string_arg(node: ast.Call) -> str | None:
    """Call ノードの第1引数が文字列定数なら値を返す。それ以外は None。"""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None

if not is_async_impl:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name or ""
                if name == "asyncio" or name.startswith("asyncio."):
                    violations.append(
                        f"L{node.lineno}: 'import {name}' is forbidden outside {async_allowed_suffix}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "asyncio" or module.startswith("asyncio."):
                violations.append(
                    f"L{node.lineno}: 'from {module} import ...' is forbidden outside {async_allowed_suffix}"
                )
        elif isinstance(node, ast.Call):
            # C-08A: __import__('asyncio') の文字列引数検査 (fix 2026-04-23)
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                mod = _get_string_arg(node)
                if mod is not None and (mod == "asyncio" or mod.startswith("asyncio.")):
                    violations.append(
                        f"L{node.lineno}: \"__import__('{mod}')\" is forbidden outside {async_allowed_suffix}"
                    )
            # C-08B: importlib.import_module('asyncio') の文字列引数検査 (fix 2026-04-23)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
                mod = _get_string_arg(node)
                if mod is not None and (mod == "asyncio" or mod.startswith("asyncio.")):
                    violations.append(
                        f"L{node.lineno}: \"importlib.import_module('{mod}')\" is forbidden outside {async_allowed_suffix}"
                    )

def _leading_callable_name(expr: ast.expr) -> str | None:
    """executor.submit(X) の X から禁止関数名を抽出する。
    Best-effort: lambda 包み (C-01)・partial (C-06)・Subscript (C-07) も追跡。
    dataflow / alias (C-02/C-03/C-04/C-05) は AST 静的解析の限界で未対応
    （Phase 2 Sprint 1 で runtime guard に切替予定）。
    """
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Lambda):
        # C-01: lambda: kill_switch.is_active() → body を再帰探索
        return _leading_callable_name(expr.body)
    if isinstance(expr, ast.Call):
        # C-06: partial(kill_switch.is_active) → 最初の引数を探索
        func_name = None
        if isinstance(expr.func, ast.Name):
            func_name = expr.func.id
        elif isinstance(expr.func, ast.Attribute):
            func_name = expr.func.attr
        if func_name in ("partial", "partialmethod"):
            if expr.args:
                return _leading_callable_name(expr.args[0])
        return _leading_callable_name(expr.func)
    if isinstance(expr, ast.Subscript):
        # C-07: fns["k"] → Subscript は直接解決不可（値が実行時依存）
        # None を返して false-negative を許容（Phase 2 Sprint 1 で runtime guard 対応）
        return None
    return None

for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if method in ("submit", "map"):
            if node.args:
                callee = _leading_callable_name(node.args[0])
                if callee in FORBIDDEN_IN_EXECUTOR:
                    violations.append(
                        f"L{node.lineno}: executor.{method}({callee}...) is forbidden "
                        f"(must stay on sync path)"
                    )

if violations:
    sys.stderr.write("[EXECUTOR_SYNC_GUARD] BLOCKED:\n")
    for v in violations:
        sys.stderr.write(f"  - {v}\n")
    sys.stderr.write(f"  spec: {spec_ref}\n")
    sys.stderr.write(f"  bypass: export {bypass_var}=1\n")
    sys.exit(2)

sys.exit(0)
PY
