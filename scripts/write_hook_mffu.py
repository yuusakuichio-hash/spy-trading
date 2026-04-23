#!/usr/bin/env python3
"""scripts/write_hook_mffu.py — mffu_dry_run_guard.sh の修正版を書き出すユーティリティ。
CONDITIONAL-PASS 到達のための 7 件修正 (C-1/C-2partial/C-4partial/H-2/H-3/M-1/C-5)
usage: python3 scripts/write_hook_mffu.py
"""
from pathlib import Path
import shutil

REPO = Path(__file__).parent.parent
HOOK = REPO / ".claude" / "hooks" / "mffu_dry_run_guard.sh"
BACKUP = REPO / ".claude" / "hooks" / "mffu_dry_run_guard.sh.bak_20260423"

HOOK_CONTENT = r"""#!/usr/bin/env bash
# mffu_dry_run_guard.sh
# PreToolUse (Write|Edit|NotebookEdit) hook
# spec: data/specs/v3/chronos_spec_v3_20260422.md B5 R2b L172-L192
# purpose: C-10 reoccurrence prevention (shadow dry_run in prod)
# bypass: MFFU_DRY_RUN_GUARD_BYPASS=1
# fixes: C-1/C-2partial/C-4partial/H-2/H-3/M-1 (2026-04-23 CONDITIONAL-PASS)

set -euo pipefail

BYPASS_VAR_NAME="MFFU_DRY_RUN_GUARD_BYPASS"
PROD_PATH_YAML_PREFIX="config/prod/"
STAGING_YAML_PREFIX="config/staging/"
DEV_YAML_PREFIX="config/dev/"
PROD_PATH_PY_PREFIXES="config/prod/,common_v3/,chronos_v3/,chronos_rules_plugin/"
TARGET_CLASS="MFFUFlexRules"
DRY_RUN_KWARG="dry_run"
SPEC_REF="data/specs/v3/chronos_spec_v3_20260422.md B5 R2b L172-L192"
BYPASS_LOG="data/governance/bypass_log.jsonl"

# M-1: record bypass usage to audit log before exit 0
if [ "${MFFU_DRY_RUN_GUARD_BYPASS:-0}" = "1" ]; then
  _TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown")
  _REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
  _LOG_PATH="${_REPO_ROOT}/${BYPASS_LOG}"
  if [ -d "$(dirname "$_LOG_PATH")" ]; then
    printf '{"timestamp":"%s","hook":"mffu_dry_run_guard","tool_name":"(bypass_before_parse)","file_path":"(bypass_before_parse)"}\n' \
      "$_TIMESTAMP" >> "$_LOG_PATH" 2>/dev/null || true
  fi
  exit 0
fi

INPUT_FILE=$(mktemp)
trap 'rm -f "$INPUT_FILE"' EXIT
cat > "$INPUT_FILE"

python3 - \
  "$INPUT_FILE" \
  "$PROD_PATH_YAML_PREFIX" \
  "$STAGING_YAML_PREFIX" \
  "$DEV_YAML_PREFIX" \
  "$PROD_PATH_PY_PREFIXES" \
  "$TARGET_CLASS" \
  "$DRY_RUN_KWARG" \
  "$SPEC_REF" \
  "$BYPASS_VAR_NAME" \
  "$BYPASS_LOG" \
<<'PY'
import sys
import json
import ast
import subprocess
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None
    sys.stderr.write("[WARN][MFFU_DRY_RUN_GUARD] pyyaml not available, yaml check skipped\n")

(
    input_path,
    prod_yaml_prefix,
    staging_yaml_prefix,
    dev_yaml_prefix,
    prod_py_prefixes_csv,
    target_class,
    dry_run_kwarg,
    spec_ref,
    bypass_var,
    bypass_log_rel,
) = sys.argv[1:11]

prod_py_prefixes = tuple(p for p in prod_py_prefixes_csv.split(",") if p)


def _get_repo_root():
    # C-1: resolve via git rev-parse, fallback to __file__ 3 levels up
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
    # C-1 fix: normalize absolute paths to repo-root-relative before prefix checks
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
        return None  # outside repo -> not monitored


def load_input(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def is_prod_yaml(rel):
    return rel.startswith(prod_yaml_prefix) and (
        rel.endswith(".yaml") or rel.endswith(".yml")
    )


def is_staging_or_dev_yaml(rel):
    # C-4: add staging/dev yaml paths (WARN level)
    return (
        rel.startswith(staging_yaml_prefix) or rel.startswith(dev_yaml_prefix)
    ) and (rel.endswith(".yaml") or rel.endswith(".yml"))


def is_prod_py(rel):
    # C-1 + C-4: add chronos_rules_plugin/
    return rel.endswith(".py") and any(rel.startswith(p) for p in prod_py_prefixes)


def _walk_dict(d, path=""):
    # H-2 fix: recursive search for dry_run keys in nested dicts
    results = []
    for k, v in d.items():
        cur = f"{path}.{k}" if path else k
        if k == dry_run_kwarg:
            results.append((cur, v))
        if isinstance(v, dict):
            results.extend(_walk_dict(v, cur))
    return results


def _is_truthy(value):
    # H-1 + C-2 partial: detect truthy dry_run values
    if value is True:
        return True
    if isinstance(value, int) and not isinstance(value, bool) and value == 1:
        return True
    if isinstance(value, str) and value.lower() in ("true", "yes", "on", "1"):
        return True
    return False


def detect_yaml_dry_run(code, is_staging=False):
    # H-2: recursive nested key search
    violations = []
    if yaml is None:
        return violations
    label = "[WARN]" if is_staging else "[BLOCK]"
    try:
        docs = list(yaml.safe_load_all(code))
    except Exception:
        return violations
    for idx, doc in enumerate(docs):
        if not isinstance(doc, dict):
            continue
        for key_path, value in _walk_dict(doc):
            if _is_truthy(value):
                env = "staging/dev" if is_staging else "prod"
                violations.append(
                    f"{label} yaml doc #{idx}: '{key_path}: {value!r}' forbidden in {env}"
                )
    return violations


def _ast_is_truthy(node):
    # C-2 partial fix: ast.Constant truthy values + ast.Name("True")
    if isinstance(node, ast.Constant):
        return _is_truthy(node.value)
    if isinstance(node, ast.Name) and node.id == "True":
        return True
    return False


def detect_python_dry_run(code):
    # C-2 partial + H-3: keyword truthy constants + positional arg[2]
    violations = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return violations
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name != target_class:
            continue

        # keyword arguments
        for kw in node.keywords:
            if kw.arg == dry_run_kwarg:
                if _ast_is_truthy(kw.value):
                    try:
                        val_str = ast.unparse(kw.value)
                    except Exception:
                        val_str = "?"
                    violations.append(
                        f"L{node.lineno}: {target_class}(..., {dry_run_kwarg}={val_str}) forbidden"
                    )
            elif kw.arg is None:
                # ** unpack best-effort warning (BLOCK)
                violations.append(
                    f"L{node.lineno}: {target_class}(**...) ** unpack may include {dry_run_kwarg}=True"
                )

        # H-3: positional arg index 2 (dry_run position per spec L172)
        if len(node.args) >= 3 and _ast_is_truthy(node.args[2]):
            try:
                val_str = ast.unparse(node.args[2])
            except Exception:
                val_str = "?"
            violations.append(
                f"L{node.lineno}: {target_class}(_, _, {val_str}) positional 3rd arg truthy forbidden"
            )

    return violations


def main():
    data = load_input(input_path)
    if data is None:
        return 0
    tool_name = data.get("tool_name", "")
    if tool_name not in ("Write", "Edit", "NotebookEdit"):
        return 0
    tool_input = data.get("tool_input") or {}
    raw_file_path = tool_input.get("file_path") or ""
    code = tool_input.get("new_string") or tool_input.get("content") or ""
    if not code.strip():
        return 0

    # C-1: normalize absolute path to repo-relative
    rel_path = _normalize_to_relative(raw_file_path)
    if rel_path is None:
        return 0

    violations = []
    warnings = []

    if is_prod_yaml(rel_path):
        violations.extend(detect_yaml_dry_run(code, is_staging=False))
    elif is_staging_or_dev_yaml(rel_path):
        warnings.extend(detect_yaml_dry_run(code, is_staging=True))

    if is_prod_py(rel_path):
        violations.extend(detect_python_dry_run(code))

    if warnings:
        sys.stderr.write("[MFFU_DRY_RUN_GUARD] WARNING (non-blocking, staging/dev):\n")
        for w in warnings:
            sys.stderr.write(f"  - {w}\n")
        sys.stderr.write(f"  file: {raw_file_path} (normalized: {rel_path})\n")
        sys.stderr.write(f"  spec: {spec_ref}\n")

    if not violations:
        return 0

    sys.stderr.write("[MFFU_DRY_RUN_GUARD] BLOCKED:\n")
    for v in violations:
        sys.stderr.write(f"  - {v}\n")
    sys.stderr.write(f"  file: {raw_file_path} (normalized: {rel_path})\n")
    sys.stderr.write(f"  spec: {spec_ref}\n")
    sys.stderr.write(f"  bypass: export {bypass_var}=1\n")
    return 2


sys.exit(main())
PY
"""

# backup
shutil.copy2(HOOK, BACKUP)
print(f"backup: {BACKUP}")

# write
HOOK.write_text(HOOK_CONTENT, encoding="utf-8")
HOOK.chmod(0o755)
print(f"written: {HOOK}")
print(f"lines: {HOOK_CONTENT.count(chr(10))}")
print(f"bytes: {len(HOOK_CONTENT.encode())}")
