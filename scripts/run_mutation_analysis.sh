#!/usr/bin/env bash
# scripts/run_mutation_analysis.sh — Mutation Testing 起動スクリプト (2026-04-25)
#
# mutmut run で atlas_v3/ops/ 配下の 4 ファイルを対象に mutation testing を実行し、
# surviving mutation の report を data/mutation_reports/ に生成する。
#
# 対象:
#   atlas_v3/ops/chainguard_wrapper.py
#   atlas_v3/ops/portfolio_risk_gate.py
#   atlas_v3/ops/mass_verify_safe_runner.py
#   atlas_v3/ops/moomoo_opend_relogin.py
#
# 前提:
#   - mutmut が PATH にインストール済 (pip install mutmut / brew install mutmut)
#   - pytest が通る状態であること
#
# 使い方:
#   bash scripts/run_mutation_analysis.sh
#   bash scripts/run_mutation_analysis.sh --target chainguard_wrapper  # 1ファイルのみ
#   bash scripts/run_mutation_analysis.sh --no-report                  # report 生成スキップ
#
# 出力:
#   data/mutation_reports/YYYYMMDD_HHMMSS_surviving.txt  — surviving mutation 一覧
#   data/mutation_reports/YYYYMMDD_HHMMSS_summary.json   — 集計 JSON

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPORT_DIR="${PROJECT_ROOT}/data/mutation_reports"
TS="$(date +%Y%m%d_%H%M%S)"

# デフォルト対象ファイル (スペース区切り)
TARGETS=(
    "atlas_v3/ops/chainguard_wrapper.py"
    "atlas_v3/ops/portfolio_risk_gate.py"
    "atlas_v3/ops/mass_verify_safe_runner.py"
    "atlas_v3/ops/moomoo_opend_relogin.py"
)

GENERATE_REPORT=true
SINGLE_TARGET=""

# ── 引数パース ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            SINGLE_TARGET="$2"
            shift 2
            ;;
        --no-report)
            GENERATE_REPORT=false
            shift
            ;;
        *)
            echo "[run_mutation_analysis] unknown arg: $1" >&2
            exit 1
            ;;
    esac
done

# 単一ターゲット指定の場合は絞り込む
if [[ -n "$SINGLE_TARGET" ]]; then
    MATCHED=()
    for t in "${TARGETS[@]}"; do
        if [[ "$t" == *"${SINGLE_TARGET}"* ]]; then
            MATCHED+=("$t")
        fi
    done
    if [[ ${#MATCHED[@]} -eq 0 ]]; then
        echo "[run_mutation_analysis] No target matched: ${SINGLE_TARGET}" >&2
        echo "Available targets:"
        for t in "${TARGETS[@]}"; do
            echo "  $t"
        done
        exit 1
    fi
    TARGETS=("${MATCHED[@]}")
fi

# ── 前提確認 ──────────────────────────────────────────────────────────────────
cd "${PROJECT_ROOT}"

MUTMUT_CMD=""
if command -v mutmut &>/dev/null; then
    MUTMUT_CMD="mutmut"
elif [[ -x "/opt/homebrew/bin/mutmut" ]]; then
    MUTMUT_CMD="/opt/homebrew/bin/mutmut"
elif python3 -m mutmut --version &>/dev/null 2>&1; then
    MUTMUT_CMD="python3 -m mutmut"
else
    echo "[run_mutation_analysis] ERROR: mutmut not found." >&2
    echo "  Install: pip install mutmut  or  brew install mutmut" >&2
    exit 1
fi

echo "[run_mutation_analysis] mutmut: ${MUTMUT_CMD}"
echo "[run_mutation_analysis] targets: ${TARGETS[*]}"
echo "[run_mutation_analysis] project_root: ${PROJECT_ROOT}"

mkdir -p "${REPORT_DIR}"

# ── 対象ごとに mutmut run ──────────────────────────────────────────────────────
TOTAL_MUTANTS=0
TOTAL_SURVIVED=0
TOTAL_KILLED=0
TOTAL_TIMEOUT=0
ALL_SURVIVED_LINES=()

for TARGET_FILE in "${TARGETS[@]}"; do
    ABS_TARGET="${PROJECT_ROOT}/${TARGET_FILE}"
    if [[ ! -f "${ABS_TARGET}" ]]; then
        echo "[run_mutation_analysis] SKIP (not found): ${TARGET_FILE}" >&2
        continue
    fi

    echo ""
    echo "=========================================="
    echo "[run_mutation_analysis] running: ${TARGET_FILE}"
    echo "=========================================="

    # mutmut run --paths-to-mutate で対象ファイルを指定
    # --runner で pytest を明示 (conftest.py のある project root で実行)
    ${MUTMUT_CMD} run \
        --paths-to-mutate "${ABS_TARGET}" \
        --runner "python3 -m pytest tests/ -x -q --tb=no --no-header" \
        --no-progress \
        || true  # exit code 1 は surviving mutant 存在を示すため無視

    # 結果収集
    RESULTS_RAW="$(${MUTMUT_CMD} results 2>/dev/null || echo "")"
    SURVIVED_RAW="$(echo "${RESULTS_RAW}" | grep -E "^SURVIVED" || true)"
    KILLED_RAW="$(echo "${RESULTS_RAW}" | grep -E "^KILLED" || true)"
    TIMEOUT_RAW="$(echo "${RESULTS_RAW}" | grep -E "^TIMEOUT" || true)"

    N_SURVIVED=$(echo "${SURVIVED_RAW}" | grep -c "SURVIVED" || echo "0")
    N_KILLED=$(echo "${KILLED_RAW}" | grep -c "KILLED" || echo "0")
    N_TIMEOUT=$(echo "${TIMEOUT_RAW}" | grep -c "TIMEOUT" || echo "0")
    N_TOTAL=$(( N_SURVIVED + N_KILLED + N_TIMEOUT ))

    echo "[run_mutation_analysis] ${TARGET_FILE}: total=${N_TOTAL} killed=${N_KILLED} survived=${N_SURVIVED} timeout=${N_TIMEOUT}"

    TOTAL_MUTANTS=$(( TOTAL_MUTANTS + N_TOTAL ))
    TOTAL_SURVIVED=$(( TOTAL_SURVIVED + N_SURVIVED ))
    TOTAL_KILLED=$(( TOTAL_KILLED + N_KILLED ))
    TOTAL_TIMEOUT=$(( TOTAL_TIMEOUT + N_TIMEOUT ))

    # surviving mutation の diff 取得
    if [[ -n "${SURVIVED_RAW}" ]]; then
        while IFS= read -r line; do
            MUTANT_ID="$(echo "${line}" | grep -oP '\d+' | head -1 || true)"
            if [[ -n "${MUTANT_ID}" ]]; then
                DIFF="$(${MUTMUT_CMD} show "${MUTANT_ID}" 2>/dev/null || echo "(diff unavailable)")"
                ALL_SURVIVED_LINES+=("=== [SURVIVED] ${TARGET_FILE} mutant #${MUTANT_ID} ===")
                ALL_SURVIVED_LINES+=("${DIFF}")
                ALL_SURVIVED_LINES+=("")
            fi
        done <<< "${SURVIVED_RAW}"
    fi
done

# ── サマリー表示 ──────────────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "[run_mutation_analysis] SUMMARY"
echo "=========================================="
echo "  Total mutants : ${TOTAL_MUTANTS}"
echo "  Killed        : ${TOTAL_KILLED}"
echo "  Survived      : ${TOTAL_SURVIVED}"
echo "  Timeout       : ${TOTAL_TIMEOUT}"

if [[ ${TOTAL_MUTANTS} -gt 0 ]]; then
    KILL_RATE=$(python3 -c "print(f'{${TOTAL_KILLED} / ${TOTAL_MUTANTS} * 100:.1f}')" 2>/dev/null || echo "N/A")
    echo "  Kill rate     : ${KILL_RATE}%"
fi

if [[ ${TOTAL_SURVIVED} -gt 0 ]]; then
    echo ""
    echo "  [WARNING] ${TOTAL_SURVIVED} surviving mutant(s) detected."
    echo "  These indicate test coverage gaps — add tests to kill them."
fi

# ── レポート生成 ───────────────────────────────────────────────────────────────
if [[ "${GENERATE_REPORT}" == "true" ]]; then
    SURVIVING_REPORT="${REPORT_DIR}/${TS}_surviving.txt"
    SUMMARY_JSON="${REPORT_DIR}/${TS}_summary.json"

    # surviving mutation 詳細レポート
    {
        echo "# Mutation Testing — Surviving Mutants Report"
        echo "# Generated: $(date)"
        echo "# Targets: ${TARGETS[*]}"
        echo ""
        if [[ ${#ALL_SURVIVED_LINES[@]} -eq 0 ]]; then
            echo "(No surviving mutants)"
        else
            for L in "${ALL_SURVIVED_LINES[@]}"; do
                echo "${L}"
            done
        fi
    } > "${SURVIVING_REPORT}"

    # JSON サマリー
    python3 - <<PYEOF
import json, sys
data = {
    "ts": "${TS}",
    "targets": ${#TARGETS[@]},
    "target_files": [
$(for t in "${TARGETS[@]}"; do echo "        \"${t}\","; done)
    ],
    "total_mutants": ${TOTAL_MUTANTS},
    "killed": ${TOTAL_KILLED},
    "survived": ${TOTAL_SURVIVED},
    "timeout": ${TOTAL_TIMEOUT},
    "kill_rate_pct": round(${TOTAL_KILLED} / max(${TOTAL_MUTANTS}, 1) * 100, 1),
    "pass": ${TOTAL_SURVIVED} == 0,
}
with open("${SUMMARY_JSON}", "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"[run_mutation_analysis] summary written: ${SUMMARY_JSON}")
PYEOF

    echo "[run_mutation_analysis] surviving report: ${SURVIVING_REPORT}"
fi

# ── 終了コード ────────────────────────────────────────────────────────────────
# surviving mutant が 0 なら 0 (成功), それ以外は 1 (要対応)
if [[ ${TOTAL_SURVIVED} -eq 0 ]]; then
    echo "[run_mutation_analysis] ALL MUTANTS KILLED. mutation score = 100%."
    exit 0
else
    echo "[run_mutation_analysis] SURVIVING MUTANTS EXIST. Kill rate < 100%." >&2
    exit 1
fi
