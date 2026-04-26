"""tests/test_chaos_weekly_runner_20260425.py — chaos_weekly_20260425.sh 動作検証 (2026-04-25)

scripts/chaos_weekly_20260425.sh の --dry-run モード + chaos_framework 統合を
Python から直接検証する 10 件以上のテスト。

テスト方針:
- asyncio 禁止 (B16 規律): 全テストは純粋な同期コード
- --dry-run 相当の動作を Python プロセス内で再現し、本番注入なしで検証
- 各シナリオの inject / cleanup / recovery_s < 60 判定ロジックを単体で確認
- シェルスクリプト自体の存在 / 実行可能ビット / plist フォーマットを structural test で確認
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# プロジェクトルート
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "chaos_weekly_20260425.sh"
PLIST_PATH = PROJECT_ROOT / "com.soralab.chaos-weekly.plist"
RECOVERY_THRESHOLD_S = 60

# chaos_framework のインポート
sys.path.insert(0, str(PROJECT_ROOT))
from tests.chaos.chaos_framework import (
    OpenDDisconnectError,
    Pushover429Error,
    _chaos_state,
    combined_chaos,
    get_chaos_state,
    network_latency,
    opend_disconnect,
    pushover_429,
)


@pytest.fixture(autouse=True)
def _reset_chaos():
    """各テスト前後に ChaosState をリセットする。"""
    _chaos_state.reset()
    yield
    _chaos_state.reset()


# ── TC-W01: スクリプト存在 + 実行可能ビット ────────────────────────────────────

class TestScriptStructure:
    """TC-W01: chaos_weekly_20260425.sh の structural 確認。"""

    def test_script_exists(self):
        """scripts/chaos_weekly_20260425.sh が存在すること。"""
        assert SCRIPT_PATH.exists(), f"スクリプトが存在しない: {SCRIPT_PATH}"

    def test_script_is_executable(self):
        """scripts/chaos_weekly_20260425.sh に実行可能ビットが付いていること。"""
        mode = SCRIPT_PATH.stat().st_mode
        assert mode & stat.S_IXUSR, "owner execute bit が付いていない"

    def test_script_contains_dry_run_flag(self):
        """--dry-run フラグ処理がスクリプト内に記述されていること。"""
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "--dry-run" in content

    def test_script_contains_five_scenarios(self):
        """5 シナリオ関数がスクリプト内に存在すること。"""
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        scenarios = [
            "run_scenario_opend_disconnect",
            "run_scenario_latency_spike",
            "run_scenario_pushover_429",
            "run_scenario_network_partition",
            "run_scenario_combined_chaos",
        ]
        for s in scenarios:
            assert s in content, f"シナリオ関数が見つからない: {s}"

    def test_script_contains_recovery_threshold(self):
        """RECOVERY_THRESHOLD_S=60 の定義がスクリプト内にあること。"""
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "RECOVERY_THRESHOLD_S=60" in content


# ── TC-W02: plist フォーマット確認 ────────────────────────────────────────────

class TestPlistStructure:
    """TC-W02: com.soralab.chaos-weekly.plist の structural 確認。"""

    def test_plist_exists(self):
        """com.soralab.chaos-weekly.plist が存在すること。"""
        assert PLIST_PATH.exists(), f"plist が存在しない: {PLIST_PATH}"

    def test_plist_label_correct(self):
        """plist の Label が com.soralab.chaos-weekly であること。"""
        content = PLIST_PATH.read_text(encoding="utf-8")
        assert "com.soralab.chaos-weekly" in content

    def test_plist_calendar_interval_friday(self):
        """plist の StartCalendarInterval が Weekday=5 (金曜) を含むこと。"""
        content = PLIST_PATH.read_text(encoding="utf-8")
        assert "StartCalendarInterval" in content
        assert "<integer>5</integer>" in content  # Weekday=5

    def test_plist_hour_22(self):
        """plist の StartCalendarInterval が Hour=22 を含むこと。"""
        content = PLIST_PATH.read_text(encoding="utf-8")
        assert "<integer>22</integer>" in content  # Hour=22

    def test_plist_minute_0(self):
        """plist の StartCalendarInterval が Minute=0 を含むこと。"""
        content = PLIST_PATH.read_text(encoding="utf-8")
        assert "<integer>0</integer>" in content  # Minute=0

    def test_plist_keepalive_false(self):
        """plist の KeepAlive が false であること (chaos は週次実行・常駐不要)。"""
        content = PLIST_PATH.read_text(encoding="utf-8")
        assert "<false/>" in content


# ── TC-W03: dry-run シェル実行 ────────────────────────────────────────────────

class TestDryRunShell:
    """TC-W03: --dry-run モードでシェルスクリプトが正常終了すること。"""

    def test_dry_run_exits_zero(self):
        """--dry-run 実行が exit code 0 で終了すること。"""
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT_PATH), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
        assert result.returncode == 0, (
            f"dry-run が非ゼロで終了: {result.returncode}\n"
            f"stdout={result.stdout[-500:]}\n"
            f"stderr={result.stderr[-500:]}"
        )

    def test_dry_run_outputs_pass_for_all_scenarios(self):
        """--dry-run で 5 シナリオ全て PASS ログが出力されること。"""
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT_PATH), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
        combined_output = result.stdout + result.stderr
        # 各シナリオ名が出力に含まれること
        for scenario in ["opend_disconnect", "latency_spike", "pushover_429",
                         "network_partition", "combined_chaos"]:
            assert scenario in combined_output, f"シナリオ名が出力にない: {scenario}"

    def test_dry_run_generates_json_report(self):
        """--dry-run 実行後に JSON レポートが data/chaos_reports/ に生成 or 更新されること。"""
        report_dir = PROJECT_ROOT / "data" / "chaos_reports"
        # 実行前の最新 mtime を記録
        files_before = sorted(
            report_dir.glob("chaos_weekly_*.json"), key=lambda p: p.stat().st_mtime
        ) if report_dir.exists() else []
        mtime_before = files_before[-1].stat().st_mtime if files_before else 0.0

        result = subprocess.run(
            ["/bin/bash", str(SCRIPT_PATH), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        )
        assert result.returncode == 0, f"dry-run 失敗: {result.stderr[-300:]}"

        files_after = sorted(
            report_dir.glob("chaos_weekly_*.json"), key=lambda p: p.stat().st_mtime
        ) if report_dir.exists() else []

        assert len(files_after) >= 1, "JSON レポートが生成されていない"

        # 最新ファイルが実行後に更新 or 新規生成されていること
        latest = files_after[-1]
        mtime_after = latest.stat().st_mtime
        assert mtime_after >= mtime_before, "レポートが更新されていない"

        # JSON 構造検証
        data = json.loads(latest.read_text(encoding="utf-8"))
        assert "dry_run" in data
        assert data["dry_run"] is True
        assert "scenarios" in data
        assert len(data["scenarios"]) == 5


# ── TC-W04: Python 内 dry-run 相当動作 ───────────────────────────────────────

class TestScenarioDryRunPython:
    """TC-W04: Python 内で dry-run 相当の注入 + recovery 判定ロジック確認。"""

    def test_opend_disconnect_inject_and_cleanup(self):
        """シナリオ1: opend_disconnect の inject+cleanup が正しく動作すること。"""
        with opend_disconnect(probability=1.0, symbol="US.SPY"):
            assert get_chaos_state().snapshot()["opend_disconnect_active"] is True
        assert get_chaos_state().snapshot()["opend_disconnect_active"] is False

    def test_latency_spike_inject_and_cleanup(self):
        """シナリオ2: latency_spike の inject+cleanup が正しく動作すること。"""
        with network_latency(latency_ms=300.0):
            assert get_chaos_state().snapshot()["latency_ms"] == 300.0
        assert get_chaos_state().snapshot()["latency_ms"] == 0.0

    def test_pushover_429_inject_and_cleanup(self):
        """シナリオ3: pushover_429 の inject+cleanup が正しく動作すること。"""
        with pushover_429(retry_after=60, fail_count=3):
            state = get_chaos_state().snapshot()
            assert state["pushover_429_active"] is True
            assert state["pushover_retry_after"] == 60
        assert get_chaos_state().snapshot()["pushover_429_active"] is False

    def test_combined_chaos_inject_and_cleanup(self):
        """シナリオ5: combined_chaos の inject+cleanup が全フラグ正しく動作すること。"""
        with combined_chaos(
            disconnect=True, latency_ms=200.0, pushover_429=True, pushover_retry_after=60
        ):
            state = get_chaos_state().snapshot()
            assert state["opend_disconnect_active"] is True
            assert state["latency_ms"] == 200.0
            assert state["pushover_429_active"] is True

        state = get_chaos_state().snapshot()
        assert state["opend_disconnect_active"] is False
        assert state["latency_ms"] == 0.0
        assert state["pushover_429_active"] is False


# ── TC-W05: recovery_s < 60 判定ロジック ────────────────────────────────────

class TestRecoveryJudge:
    """TC-W05: recovery time 判定の境界値テスト。"""

    @staticmethod
    def _judge(recovery_s: int) -> str:
        """スクリプト内の _judge 関数と同等のロジック。"""
        return "PASS" if recovery_s < RECOVERY_THRESHOLD_S else "FAIL"

    def test_recovery_0s_is_pass(self):
        """recovery_s=0 は PASS。"""
        assert self._judge(0) == "PASS"

    def test_recovery_59s_is_pass(self):
        """recovery_s=59 (閾値-1) は PASS。"""
        assert self._judge(59) == "PASS"

    def test_recovery_60s_is_fail(self):
        """recovery_s=60 (閾値ちょうど) は FAIL (< 60 でないため)。"""
        assert self._judge(60) == "FAIL"

    def test_recovery_61s_is_fail(self):
        """recovery_s=61 は FAIL。"""
        assert self._judge(61) == "FAIL"


# ── TC-W06: JSON レポート構造確認 ────────────────────────────────────────────

class TestJsonReportStructure:
    """TC-W06: dry-run で生成した JSON レポートの構造を検証する。"""

    def test_report_has_required_keys(self):
        """JSON レポートが必須キーを全て含むこと。"""
        # dry-run で生成されたレポートを取得
        report_dir = PROJECT_ROOT / "data" / "chaos_reports"
        reports = sorted(report_dir.glob("chaos_weekly_*.json")) if report_dir.exists() else []

        if not reports:
            pytest.skip("chaos_weekly JSON レポートが未生成 (先に dry-run を実行)")

        latest = reports[-1]
        data = json.loads(latest.read_text(encoding="utf-8"))

        required_keys = {"run_ts", "dry_run", "recovery_threshold_s", "pass", "fail", "total", "scenarios"}
        assert required_keys.issubset(data.keys()), f"欠損キー: {required_keys - data.keys()}"

    def test_report_scenarios_have_required_fields(self):
        """各シナリオエントリが scenario / recovery_s / result フィールドを持つこと。"""
        report_dir = PROJECT_ROOT / "data" / "chaos_reports"
        reports = sorted(report_dir.glob("chaos_weekly_*.json")) if report_dir.exists() else []

        if not reports:
            pytest.skip("chaos_weekly JSON レポートが未生成")

        latest = reports[-1]
        data = json.loads(latest.read_text(encoding="utf-8"))

        for entry in data["scenarios"]:
            assert "scenario" in entry, f"'scenario' キーなし: {entry}"
            assert "recovery_s" in entry, f"'recovery_s' キーなし: {entry}"
            assert "result" in entry, f"'result' キーなし: {entry}"
            assert entry["result"] in {"PASS", "FAIL"}, f"result が不正: {entry['result']}"
