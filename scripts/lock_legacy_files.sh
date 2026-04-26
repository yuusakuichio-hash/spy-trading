#!/bin/bash
# C-018 allowlist 設計: 保護対象ファイルを immutable 化（macOS chflags schg）
# 2026-04-24 策定（Redteam r7 指摘: bash_write_guard blacklist で 15 攻撃中 13 bypass）
#
# Usage:
#   scripts/lock_legacy_files.sh lock     # 保護対象を immutable 化
#   scripts/lock_legacy_files.sh unlock   # 明示確認付きで解除
#   scripts/lock_legacy_files.sh status   # 現状確認
#
# 対象: CLAUDE.md「既存コード書換禁止」記載ファイル
#   - spy_bot.py / chronos_bot.py / atlas_agent.py / chronos_agent.py
#   - atlas_rules.yaml / chronos_accounts.yaml
#   - common/ 配下全ファイル
#
# Sprint 2 Day 1 で本格運用開始。それまでは dry-run で検証。

set -e

PROJ_ROOT="/Users/yuusakuichio/trading"
cd "$PROJ_ROOT"

PROTECTED_FILES=(
    "spy_bot.py"
    "chronos_bot.py"
    "atlas_agent.py"
    "chronos_agent.py"
    "atlas_rules.yaml"
    "chronos_accounts.yaml"
    "atlas_watchdog.py"
    "chronos_watchdog.py"
)

PROTECTED_DIRS=(
    "common"
)

lock_file() {
    local f="$1"
    if [ -f "$f" ]; then
        # 既存 flag を読んで確認
        if ls -lO "$f" 2>/dev/null | grep -q "schg"; then
            echo "  already locked: $f"
        else
            chflags schg "$f" 2>/dev/null && echo "  LOCKED: $f" || echo "  FAILED: $f (needs sudo?)"
        fi
    fi
}

unlock_file() {
    local f="$1"
    if [ -f "$f" ]; then
        if ls -lO "$f" 2>/dev/null | grep -q "schg"; then
            chflags noschg "$f" 2>/dev/null && echo "  UNLOCKED: $f" || echo "  FAILED: $f"
        else
            echo "  not locked: $f"
        fi
    fi
}

case "${1:-status}" in
    lock)
        echo "=== Locking protected files (C-018 allowlist) ==="
        for f in "${PROTECTED_FILES[@]}"; do
            lock_file "$f"
        done
        for d in "${PROTECTED_DIRS[@]}"; do
            if [ -d "$d" ]; then
                echo "Dir: $d/"
                while IFS= read -r f; do
                    lock_file "$f"
                done < <(find -P "$d" -type f -not -type l \( -name "*.py" -o -name "*.yaml" -o -name "*.json" \))
            fi
        done
        echo "=== Done ==="
        ;;
    unlock)
        # O-1 break-glass: 環境変数 ATLAS_UNLOCK_APPROVED に
        # "YYYY-MM-DD:reason" 形式で指定すると対話確認を bypass（Secretary 非対話経由用）
        if [ -n "${ATLAS_UNLOCK_APPROVED:-}" ]; then
            echo "⚠ UNLOCK via ATLAS_UNLOCK_APPROVED env: $ATLAS_UNLOCK_APPROVED"
            # audit log に記録（改変不可な append-only 推奨だが最小実装）
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] UNLOCK via env: $ATLAS_UNLOCK_APPROVED" \
                >> /Users/yuusakuichio/trading/data/logs/lock_legacy_unlock_audit.log 2>/dev/null || true
        else
            echo "⚠ UNLOCK requires explicit confirmation."
            echo "Non-interactive mode: export ATLAS_UNLOCK_APPROVED='YYYY-MM-DD:reason'"
            read -p "Type 'UNLOCK' to confirm: " confirm
            if [ "$confirm" != "UNLOCK" ]; then
                echo "Cancelled."
                exit 1
            fi
        fi
        echo "=== Unlocking protected files ==="
        for f in "${PROTECTED_FILES[@]}"; do
            unlock_file "$f"
        done
        for d in "${PROTECTED_DIRS[@]}"; do
            if [ -d "$d" ]; then
                while IFS= read -r f; do
                    unlock_file "$f"
                done < <(find -P "$d" -type f -not -type l \( -name "*.py" -o -name "*.yaml" -o -name "*.json" \))
            fi
        done
        echo "=== Done (remember to re-lock after legitimate edit) ==="
        ;;
    status)
        echo "=== Legacy file protection status ==="
        locked=0
        unlocked=0
        for f in "${PROTECTED_FILES[@]}"; do
            if [ -f "$f" ]; then
                if ls -lO "$f" 2>/dev/null | grep -q "schg"; then
                    echo "  [LOCKED]   $f"
                    locked=$((locked+1))
                else
                    echo "  [unlocked] $f"
                    unlocked=$((unlocked+1))
                fi
            fi
        done
        for d in "${PROTECTED_DIRS[@]}"; do
            if [ -d "$d" ]; then
                while IFS= read -r f; do
                    if [ -f "$f" ]; then
                        if ls -lO "$f" 2>/dev/null | grep -q "schg"; then
                            locked=$((locked+1))
                        else
                            unlocked=$((unlocked+1))
                        fi
                    fi
                done < <(find -P "$d" -type f -not -type l \( -name "*.py" -o -name "*.yaml" -o -name "*.json" \))
            fi
        done
        echo "=== Summary: locked=$locked / unlocked=$unlocked ==="
        ;;
    *)
        echo "Usage: $0 {lock|unlock|status}"
        exit 1
        ;;
esac
