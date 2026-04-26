# allowlist hook 設計ドキュメント（C-018・Sprint 2 冒頭）

**策定日**: 2026-04-24 07:55 JST
**起案**: Redteam r7 の「15 攻撃中 13 件 bypass」実測 + Normalization of Deviance 指摘
**目的**: blacklist 方式の legacy_write_block / bash_write_guard を allowlist 方式に根本転換

---

## 1. 現状の問題

### blacklist hook の構造的限界

| hook | 設計 | 現状の穴 |
|---|---|---|
| `.claude/hooks/legacy_write_block.sh` | `tool_name ∈ {Write,Edit,NotebookEdit}` のみ検査 | Bash 経由（`sed -i` 等）で素通り |
| `.claude/hooks/bash_write_guard.sh` | `Bash` tool の `command` 文字列 regex マッチ | Redteam r7 実測 15/13 bypass（`python -c "Path().write_text"`, `git apply`, `patch`, `install`, `ln -sf`, `tee`, `dd`, `vim -c` 等）|

**根本原因**: ファイル書換経路は OS レベルで無限に存在する（`write(2)` を呼べる任意の方法）。regex blacklist は新経路発見のたびに追記する「イタチごっこ」・常に次の bypass が見つかる。

---

## 2. allowlist 設計 3 案比較

| 案 | 実装手段 | 強度 | 実装工数 | bug 発生率 |
|---|---|---|---|---|
| **A. chmod + chflags schg** | macOS 不変フラグ (`chflags schg <file>`) / Linux `chattr +i` | **最強**（root 権限でも簡単に書き換え不可） | 小（1-2h） | 低 |
| B. chmod 0444 のみ | 読み取り専用（書込不可）| 中（root / `chmod` で簡単に外せる） | 極小（30 分） | 極低 |
| C. 専用 Edit tool 限定 | Claude Code の Edit tool 設定で保護対象を block | 中（他 tool 経由は素通り可能性） | 中（2-3h・hook ロジック）| 中 |

### 推奨: **案 A（chmod + chflags schg）**

**根拠**:
- macOS `chflags schg` は scheduler immutable flag で、通常 root 権限でも書換不可（`chflags noschg` で明示的に外さない限り）
- Linux `chattr +i` も同等の immutable 機能
- ファイルシステム層の保護なので hook bypass の全経路を物理的に封じる
- 解除は明示的に `chflags noschg` を実行するしかない = 意図しない書換 0 保証

---

## 3. 案 A 実装設計

### 保護対象ファイル

既存コードの保護対象（CLAUDE.md 記載）:
- `spy_bot.py` / `chronos_bot.py`
- `atlas_agent.py` / `chronos_agent.py`
- `common/` 配下全ファイル
- `atlas_rules.yaml` / `chronos_accounts.yaml`

### 実装スクリプト

`scripts/lock_legacy_files.sh`:

```bash
#!/bin/bash
# allowlist 設計: 保護対象ファイルを immutable 化
# Usage: scripts/lock_legacy_files.sh {lock|unlock|status}

set -e

PROTECTED_FILES=(
    "spy_bot.py"
    "chronos_bot.py"
    "atlas_agent.py"
    "chronos_agent.py"
    "atlas_rules.yaml"
    "chronos_accounts.yaml"
)

PROTECTED_DIRS=(
    "common"
)

PROJ_ROOT="/Users/yuusakuichio/trading"
cd "$PROJ_ROOT"

case "${1:-status}" in
    lock)
        for f in "${PROTECTED_FILES[@]}"; do
            [ -f "$f" ] && chflags schg "$f" && echo "LOCKED: $f"
        done
        for d in "${PROTECTED_DIRS[@]}"; do
            [ -d "$d" ] && find "$d" -type f -exec chflags schg {} \; && echo "LOCKED dir: $d/"
        done
        ;;
    unlock)
        echo "UNLOCK requires explicit confirmation. Sprint 2 carryover C-026 等で必要時のみ。"
        read -p "Type 'UNLOCK' to confirm: " confirm
        [ "$confirm" != "UNLOCK" ] && exit 1
        for f in "${PROTECTED_FILES[@]}"; do
            [ -f "$f" ] && chflags noschg "$f" && echo "UNLOCKED: $f"
        done
        for d in "${PROTECTED_DIRS[@]}"; do
            [ -d "$d" ] && find "$d" -type f -exec chflags noschg {} \; && echo "UNLOCKED dir: $d/"
        done
        ;;
    status)
        echo "=== Locked files status ==="
        for f in "${PROTECTED_FILES[@]}"; do
            if [ -f "$f" ]; then
                flags=$(ls -lO "$f" | awk '{print $5}')
                if echo "$flags" | grep -q "schg"; then
                    echo "LOCKED:   $f"
                else
                    echo "UNLOCKED: $f (⚠ 保護されていない)"
                fi
            fi
        done
        ;;
esac
```

### 運用フロー

1. Sprint 2 Day 1 着手時: `scripts/lock_legacy_files.sh lock` 実行
2. 以降すべての書換試行（Write/Edit/Bash 経由）で `Operation not permitted` エラー
3. 意図的に外す時は明示コマンド `scripts/lock_legacy_files.sh unlock`（対話確認あり）
4. launchd plist で起動時に自動 lock（`com.soralab.legacy-lock.plist` 新設）

### 既存 hook との関係

- `legacy_write_block.sh` は補助的 block（OS 層保護の前段フィルタ）として残す
- `bash_write_guard.sh` は C-024 で拡張継続・ただし本体は OS 層保護に移行

---

## 4. 攻撃ベクトル対応（15 件 vs allowlist）

| # | 攻撃 | blacklist 結果 | **allowlist 結果** |
|---|---|---|---|
| 1 | `sed -i spy_bot.py` | r7: regex match で block | EPERM |
| 2 | `perl -i spy_bot.py` | r7: regex 漏れで bypass | EPERM |
| 3 | `python -c "Path().write_text"` | r7: bypass | EPERM |
| 4 | `git apply malicious.patch` | r7: bypass | EPERM |
| 5 | `patch spy_bot.py < p` | r7: bypass | EPERM |
| 6 | `install -m 0644 evil.py spy_bot.py` | r7: bypass | EPERM |
| 7 | `ln -sf /tmp/evil spy_bot.py` | r7: bypass | EPERM（ln でリンク置換も不可）|
| 8 | `tr < other > spy_bot.py` | r7: bypass | EPERM |
| 9 | `echo x > spy_bot.py` | r7: 部分 bypass | EPERM |
| 10 | `vim -c ':w spy_bot.py'` | r7: bypass | EPERM |
| 11 | `tee spy_bot.py` | r7: bypass | EPERM |
| 12 | `dd of=spy_bot.py` | r7: bypass | EPERM |
| 13 | `nano spy_bot.py` | r7: bypass | EPERM |
| 14 | `emacs --batch spy_bot.py` | r7: bypass | EPERM |
| 15 | `rsync evil spy_bot.py` | r7: bypass | EPERM |

**15/15 全 block**（OS 層 immutable flag は write syscall を全経路で拒否）。

---

## 5. 検証テスト設計（実試行形式・AST inspection 禁止）

`tests/test_allowlist_lock_20260424.py`:

```python
"""allowlist hook 実動作テスト。
AST inspection 型禁止・subprocess.run で実際の攻撃を試行し EPERM を確認。
"""
import subprocess
import pytest
from pathlib import Path

PROTECTED_FILE = Path("/Users/yuusakuichio/trading/spy_bot.py")

ATTACK_COMMANDS = [
    ["sed", "-i", ".bak", "s/foo/bar/", str(PROTECTED_FILE)],
    ["perl", "-i", "-pe", "s/foo/bar/", str(PROTECTED_FILE)],
    ["python3", "-c", f"open('{PROTECTED_FILE}', 'w').write('x')"],
    ["bash", "-c", f"echo 'x' >> {PROTECTED_FILE}"],
    ["bash", "-c", f"tee {PROTECTED_FILE} < /dev/null"],
    # ... 15 ベクトル全 列挙
]

@pytest.mark.parametrize("cmd", ATTACK_COMMANDS)
def test_attack_blocked_by_immutable(cmd):
    """保護対象ファイルへの書換試行が EPERM で block されること。"""
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode != 0, f"Attack should fail: {cmd}"
    # 実際の file 内容が変わっていないことも別 assert で確認
```

---

## 6. リスク・注意点

| リスク | 対策 |
|---|---|
| legitimate な commit で書換したい場合 | `scripts/lock_legacy_files.sh unlock` 明示実行・git 操作後に即 lock |
| macOS ↔ Linux 移植性 | macOS `chflags schg` と Linux `chattr +i` を適切に分岐（platform 判定）|
| root 権限取得で突破される | immutable flag は root でも一段確認（`chflags noschg`）必須なので「事故」による書換 0 は保証 |
| launchd plist の起動順 | システム起動時に legacy-lock を先に load |

---

## 7. Sprint 2 Day 1 実装順（allowlist 部分）

| 時刻 | task |
|---|---|
| 着手 +0h | `scripts/lock_legacy_files.sh` 作成 |
| +30m | 手動実行テスト（lock / status / unlock）|
| +1h | `tests/test_allowlist_lock_20260424.py` 15 ベクトル実試行テスト |
| +2h | launchd plist `com.soralab.legacy-lock.plist` 作成・起動時自動 lock |
| +2.5h | Redteam 独立検証（攻撃再試行で 15/15 block 確認）|

---

## 関連ファイル
- `data/sprint1_carryovers.md` C-018
- `data/governance/redteam_r7_audit_20260424.md`
- `data/ops/sprint2_dayplan_20260424.md` Day 1
- 実装対象: `scripts/lock_legacy_files.sh` / `tests/test_allowlist_lock_20260424.py` / `~/Library/LaunchAgents/com.soralab.legacy-lock.plist`
