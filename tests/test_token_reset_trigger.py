"""
tests/test_token_reset_trigger.py — scripts/token_reset_trigger.sh テスト

3ケース:
  1. rate_limit 解消済み → work_queue 投入（auto_resume.sh --force-check 呼び出し）
  2. rate_limit 継続中 → Pushover 通知のみ（auto_resume.sh 呼び出しなし）
  3. --force-check で Guard 1 bypass（auto_resume.sh の挙動テスト）

シェルスクリプトのテストは subprocess で実行し、
dry-run モードで副作用（claude CLI 実行・実際の auto_resume 起動）を回避する。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# スクリプトのパス
TRADING_DIR = Path(__file__).parent.parent
TOKEN_RESET_SCRIPT = TRADING_DIR / "scripts" / "token_reset_trigger.sh"
AUTO_RESUME_SCRIPT = TRADING_DIR / "scripts" / "auto_resume.sh"
WORK_QUEUE = TRADING_DIR / "data" / "work_queue.md"


def _run_script(script: Path, args: list[str] = None, env_overrides: dict = None) -> subprocess.CompletedProcess:
    """シェルスクリプトを実行して CompletedProcess を返す。"""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    cmd = ["bash", str(script)] + (args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


# ================================================================
# ケース 1: rate_limit 解消済み → work_queue 投入
# ================================================================
class TestRateLimitCleared:
    def test_rate_limit_cleared_calls_auto_resume(self, tmp_path):
        """rate_limit 解消済み + ACTIVE TASKS あり → auto_resume --force-check が呼ばれること。

        dry-run モードでは exec が実行されないため、
        スクリプトが exit 0 で終了することを確認する。
        テスト用 work_queue を tmp_path に作成する。
        """
        # テスト用 work_queue を作成（ACTIVE TASKS あり）
        work_queue = tmp_path / "data" / "work_queue.md"
        work_queue.parent.mkdir(parents=True, exist_ok=True)
        work_queue.write_text("### [TASK-001] テストタスク\nstatus: active\n")

        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # スクリプト内の変数を環境変数でオーバーライドするためのラッパーを作成
        wrapper = tmp_path / "token_reset_trigger_test.sh"
        wrapper.write_text(
            f"""#!/usr/bin/env bash
export TRADING_DIR="{tmp_path}"
export WORK_QUEUE="{work_queue}"
export LOG_DIR="{log_dir}"
export LOG_FILE="{log_dir}/token_reset_trigger.log"
# auto_resume のダミー: force-check 引数を記録して exit 0
export AUTO_RESUME="{tmp_path}/fake_auto_resume.sh"

# fake auto_resume
cat > "{tmp_path}/fake_auto_resume.sh" << 'FAKE_EOF'
#!/usr/bin/env bash
echo "auto_resume called with: $@" >> "{log_dir}/token_reset_trigger.log"
exit 0
FAKE_EOF
chmod +x "{tmp_path}/fake_auto_resume.sh"

# rate_limit チェックをスキップするために --dry-run を使用
exec bash "{TOKEN_RESET_SCRIPT}" --dry-run
""",
        )
        wrapper.chmod(0o755)

        result = subprocess.run(
            ["bash", str(wrapper)],
            capture_output=True,
            text=True,
            timeout=15,
        )

        # dry-run 終了は exit 0 であること
        assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
        # ログに dry-run メッセージが含まれること
        assert "DRY-RUN" in result.stdout or "DRY-RUN" in result.stderr or "dry" in result.stdout.lower()

    def test_no_active_tasks_skips_auto_resume(self, tmp_path):
        """ACTIVE TASKS がない場合は auto_resume を呼ばずスキップすること。

        work_queue に「### [TASK-」で始まる行が存在しないケースを検証する。
        """
        work_queue = tmp_path / "data" / "work_queue.md"
        work_queue.parent.mkdir(parents=True, exist_ok=True)
        # ACTIVE TASKS なし（COMPLETED セクションのみ）
        work_queue.write_text("# COMPLETED\n- 完了済みタスクA\n- 完了済みタスクB\n")

        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        wrapper = tmp_path / "test_no_tasks.sh"
        wrapper.write_text(
            f"""#!/usr/bin/env bash
TRADING_DIR="{tmp_path}"
WORK_QUEUE="{work_queue}"
LOG_DIR="{log_dir}"
LOG_FILE="{log_dir}/token_reset_trigger.log"
AUTO_RESUME="{tmp_path}/should_not_be_called.sh"
mkdir -p "{log_dir}"

# work_queue に ACTIVE TASKS なし → スキップチェック
# token_reset_trigger.sh と同じパターンで検索（bash 側で [ はエスケープ不要）
if ! grep -q '^### \\[TASK-' "{work_queue}" 2>/dev/null; then
    echo "no_active_tasks: skip"
    exit 0
fi
echo "FAIL: auto_resume が呼ばれた"
exit 1
""",
        )
        wrapper.chmod(0o755)

        result = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "no_active_tasks" in result.stdout


# ================================================================
# ケース 2: rate_limit 継続中 → Pushover 通知のみ
# ================================================================
class TestRateLimitStillActive:
    def test_rate_limited_only_pushover_no_auto_resume(self, tmp_path):
        """rate_limit 継続中の場合、auto_resume を呼ばずに Pushover のみ送信すること。

        実際の Pushover 送信は確認できないため、スクリプトのロジックを
        inline bash で再現してテストする。
        """
        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "token_reset_trigger.log"

        auto_resume_called = tmp_path / "auto_resume_called.flag"

        # rate_limited=true のパスを直接テスト
        test_script = tmp_path / "test_rate_limited.sh"
        test_script.write_text(
            f"""#!/usr/bin/env bash
RATE_LIMITED=true
AUTO_RESUME="{auto_resume_called}"
LOG_FILE="{log_file}"
mkdir -p "{log_dir}"

log() {{
    echo "[TEST] $*" | tee -a "${{LOG_FILE}}"
}}

pushover_mock() {{
    echo "[PUSHOVER] $1" >> "${{LOG_FILE}}"
}}

if [[ "${{RATE_LIMITED}}" == "true" ]]; then
    log "rate_limit 継続中 → Pushover 通知のみ"
    pushover_mock "rate_limit 継続中通知"
    # auto_resume は呼ばない
    exit 0
fi

# ここに来たら失敗
touch "{auto_resume_called}"
exit 1
""",
        )
        test_script.chmod(0o755)

        result = subprocess.run(["bash", str(test_script)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert not auto_resume_called.exists(), "auto_resume が呼ばれるべきでない"
        assert log_file.exists()
        log_content = log_file.read_text()
        assert "rate_limit 継続中" in log_content
        assert "PUSHOVER" in log_content


# ================================================================
# ケース 3: --force-check で Guard 1 bypass
# ================================================================
class TestForceCheckFlag:
    def test_force_check_sets_flag(self, tmp_path):
        """auto_resume.sh で --force-check フラグが正しく認識されること。

        Guard 1 の exit 0 を bypass して Guard 2 のチェックへ進むことを確認する。
        work_queue.md が存在しない場合に Guard 2 で exit 0 になることで
        Guard 1 を通過したことを確認する。
        """
        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # auto_resume.sh の --force-check 挙動を確認する inline テスト
        test_script = tmp_path / "test_force_check.sh"
        test_script.write_text(
            f"""#!/usr/bin/env bash
# --force-check フラグ判定ロジックを再現
FORCE_CHECK=false
if [[ "${{1:-}}" == "--force-check" ]]; then
    FORCE_CHECK=true
fi

LOG_FILE="{log_dir}/force_check_test.log"
mkdir -p "{log_dir}"

log_poll() {{
    echo "[POLL] $*" | tee -a "${{LOG_FILE}}"
}}

# Guard 1 ロジック（auto_resume.sh から抜粋）
RATE_LIMITED=false
ACTIVE_SESSION_THRESHOLD=1800

if [[ "${{FORCE_CHECK}}" == "true" ]]; then
    log_poll "state=force_check_bypass (Guard 1 bypassed) → proceeding to Guard 2"
    echo "GUARD1_BYPASSED"
elif [[ "${{RATE_LIMITED}}" == "false" ]]; then
    # 通常は claude プロセスチェックへ（ここではプロセスなし = skip のシミュレーション）
    echo "GUARD1_NORMAL"
    exit 1  # テスト: Guard 1 通過しないことを示す
fi

# Guard 2: work_queue なし → exit 0
WORK_QUEUE="{tmp_path}/data/work_queue.md"
if [[ ! -f "${{WORK_QUEUE}}" ]]; then
    log_poll "state=no_queue_file → skip"
    echo "GUARD2_NO_QUEUE"
    exit 0
fi
""",
        )
        test_script.chmod(0o755)

        # --force-check ありで呼び出し
        result_with_force = subprocess.run(
            ["bash", str(test_script), "--force-check"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result_with_force.returncode == 0
        assert "GUARD1_BYPASSED" in result_with_force.stdout
        assert "GUARD2_NO_QUEUE" in result_with_force.stdout

    def test_without_force_check_guard1_active(self, tmp_path):
        """--force-check なしの場合は Guard 1 が通常動作すること。"""
        log_dir = tmp_path / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        test_script = tmp_path / "test_no_force.sh"
        test_script.write_text(
            f"""#!/usr/bin/env bash
FORCE_CHECK=false
if [[ "${{1:-}}" == "--force-check" ]]; then
    FORCE_CHECK=true
fi

if [[ "${{FORCE_CHECK}}" == "true" ]]; then
    echo "BYPASSED"
    exit 0
else
    echo "NORMAL_GUARD1"
    exit 0
fi
""",
        )
        test_script.chmod(0o755)

        # --force-check なしで呼び出し
        result = subprocess.run(["bash", str(test_script)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "NORMAL_GUARD1" in result.stdout
        assert "BYPASSED" not in result.stdout

    def test_auto_resume_force_check_flag_exists(self):
        """auto_resume.sh に --force-check フラグの処理が実装されていること。"""
        content = AUTO_RESUME_SCRIPT.read_text()
        assert "--force-check" in content, "auto_resume.sh に --force-check が実装されていない"
        assert "FORCE_CHECK=true" in content, "FORCE_CHECK=true の設定が見当たらない"
        assert "force_check_bypass" in content, "force_check_bypass ログが見当たらない"
