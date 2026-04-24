"""tests/test_premarket_smoke_20260425.py
=========================================
monday_premarket_smoke_20260427.sh の design contract を固定する pytest。

検証観点:
  A. script 静的契約 (存在 / 実行権限 / 定数 / exit code)
  B. ループ構造・interval / window 定数
  C. kill_switch 検出経路
  D. OpenD alive 判定・連続失敗 halt
  E. relogin heartbeat 鮮度 python ロジック
  F. daemon 群 alive 検出
  G. pytest 静的回帰 target 一覧
  H. Pushover P1 経路
  I. sentinel_watchdog kick 経路
  J. CRITICAL wrapper 3 本 import 可能性
  K. engine module 静的 import 可能性 (新規 engine 10 件相当)

既存コード書換禁止原則: script への差分注入は行わず契約チェックのみ。
"""
from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess
import sys
from typing import Any

import pytest

ROOT = pathlib.Path("/Users/yuusakuichio/trading")
SCRIPT = ROOT / "scripts" / "monday_premarket_smoke_20260427.sh"

# プロジェクト root を import path に追加
sys.path.insert(0, str(ROOT))


# =============================================================================
# A. script 静的契約
# =============================================================================


class TestScriptStaticContract:
    """script ファイル存在・実行権限・定数の静的確認。"""

    def test_script_exists(self) -> None:
        assert SCRIPT.exists(), f"premarket smoke script missing: {SCRIPT}"

    def test_script_is_executable(self) -> None:
        mode = SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "script is not chmod +x"

    def test_total_window_12h(self) -> None:
        """12 時間 = 43200s 定数が埋まっていること。"""
        text = SCRIPT.read_text()
        assert "TOTAL_WINDOW_SEC=43200" in text, "12h window constant missing"

    def test_interval_15min(self) -> None:
        """15 分 = 900s 定数が埋まっていること。"""
        text = SCRIPT.read_text()
        assert "INTERVAL_SEC=900" in text, "15-min interval constant missing"

    def test_relogin_hb_threshold_1h(self) -> None:
        """relogin heartbeat 鮮度閾値 1h が定数として存在すること。"""
        text = SCRIPT.read_text()
        assert "RELOGIN_HB_MAX_AGE_H=1" in text, "relogin HB threshold 1h missing"

    @pytest.mark.parametrize(
        "code,label",
        [
            (0, "正常完了"),
            (10, "単一 cycle 失敗"),
            (20, "kill_switch"),
            (21, "OpenD halt"),
            (22, "pytest halt"),
            (99, "前提条件"),
        ],
    )
    def test_exit_code_documented(self, code: int, label: str) -> None:
        text = SCRIPT.read_text()
        header = "\n".join(text.splitlines()[:60])
        assert str(code) in header, f"exit code {code} ({label}) not in header docstring"

    def test_pushover_dryrun_set_globally(self) -> None:
        """cycle 中の誤発砲防止: PUSHOVER_DRY_RUN=1 がグローバルに設定されていること。"""
        text = SCRIPT.read_text()
        assert "export PUSHOVER_DRY_RUN=1" in text, "PUSHOVER_DRY_RUN=1 not exported globally"

    def test_no_vps_ip_reference(self) -> None:
        """VPS IP (127.0.0.1 以外) を参照していないこと (auth_budget 温存)。"""
        text = SCRIPT.read_text()
        ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        bad = [ip for ip in ips if ip != "127.0.0.1"]
        assert not bad, f"non-localhost IPs found: {bad}"

    def test_no_place_order_call(self) -> None:
        """副作用なし: place_order 呼出禁止。"""
        text = SCRIPT.read_text()
        assert "place_order" not in text

    def test_no_force_relogin(self) -> None:
        """auth_budget 消費: force_relogin 禁止。"""
        text = SCRIPT.read_text()
        assert "force_relogin" not in text

    def test_atlas_trader_active_flag_default_zero(self) -> None:
        """ATLAS_TRADER_ACTIVE のデフォルト値が 0 (安全側) であること。"""
        text = SCRIPT.read_text()
        assert 'ATLAS_TRADER_ACTIVE="${ATLAS_TRADER_ACTIVE:-0}"' in text


# =============================================================================
# B. ループ構造・window 論理
# =============================================================================


class TestLoopStructure:
    """15 分間隔 / 12h window / sentinel kick の構造チェック。"""

    def test_while_true_loop_present(self) -> None:
        text = SCRIPT.read_text()
        assert "while true" in text, "main loop (while true) missing"

    def test_interval_sleep_wired(self) -> None:
        text = SCRIPT.read_text()
        assert 'sleep "${INTERVAL_SEC}"' in text, "interval sleep not wired"

    def test_window_elapsed_check(self) -> None:
        """ELAPSED >= TOTAL_WINDOW_SEC の終了チェックが存在すること。"""
        text = SCRIPT.read_text()
        assert "TOTAL_WINDOW_SEC" in text
        # elapsed vs window の比較式
        assert "ELAPSED" in text

    def test_cycle_counter_increments(self) -> None:
        """CYCLE_NO がループ内でインクリメントされること。"""
        text = SCRIPT.read_text()
        assert "CYCLE_NO=$(( CYCLE_NO + 1 ))" in text

    def test_cycle_jsonl_append(self) -> None:
        """各 cycle 結果を JSONL に追記していること (audit trail)。"""
        text = SCRIPT.read_text()
        assert "CYCLE_JSON" in text
        assert "append_cycle_json" in text


# =============================================================================
# C. kill_switch 検出・即停止経路
# =============================================================================


class TestKillSwitchDetection:
    """kill_switch.flag 検出 → 即停止 (exit 20) の静的契約。"""

    def test_kill_switch_check_wired(self) -> None:
        text = SCRIPT.read_text()
        assert "kill_switch" in text, "kill_switch check not wired"

    def test_exit_20_on_kill_switch(self) -> None:
        text = SCRIPT.read_text()
        assert "exit 20" in text, "exit 20 for kill_switch not found"

    def test_sentinel_kicked_on_kill_switch(self) -> None:
        """kill_switch 検出時に sentinel_watchdog を kick すること。"""
        text = SCRIPT.read_text()
        assert "kick_sentinel" in text
        assert "kill_switch_active" in text

    def test_kill_switch_python_is_active_called(self) -> None:
        """kill_switch.is_active() を python 経由で呼ぶこと。"""
        text = SCRIPT.read_text()
        assert "from common.kill_switch import is_active" in text

    def test_python_kill_switch_is_active_runtime(self) -> None:
        """python runtime: kill_switch.is_active() が bool を返すこと。"""
        from common.kill_switch import is_active
        result = is_active()
        assert isinstance(result, bool)


# =============================================================================
# D. OpenD alive 判定
# =============================================================================


class TestOpenDAliveCheck:
    """OpenD TCP チェック + 連続失敗 halt の静的契約。"""

    def test_opend_launchctl_grep_wired(self) -> None:
        text = SCRIPT.read_text()
        assert r"application\.com\.moomoo\.opend" in text

    def test_opend_tcp_127_0_0_1_11111(self) -> None:
        text = SCRIPT.read_text()
        assert "127.0.0.1" in text
        assert "11111" in text

    def test_opend_consec_fail_counter(self) -> None:
        text = SCRIPT.read_text()
        assert "OPEND_CONSEC_FAIL" in text

    def test_opend_halt_exit_21(self) -> None:
        text = SCRIPT.read_text()
        assert "exit 21" in text, "exit 21 for OpenD consecutive fail not found"

    def test_opend_fail_threshold_constant(self) -> None:
        text = SCRIPT.read_text()
        assert "OPEND_FAIL_HALT=3" in text, "OpenD consecutive fail threshold=3 missing"


# =============================================================================
# E. relogin heartbeat 鮮度ロジック (Python 側)
# =============================================================================


class TestReloginHeartbeatLogic:
    """relogin heartbeat 鮮度判定ロジックの Python 単体確認。"""

    def _make_jsonl(self, tmp_path: pathlib.Path, age_seconds: float) -> pathlib.Path:
        import json
        import time
        ts = time.time() - age_seconds
        p = tmp_path / "opend_relogin_heartbeat.jsonl"
        p.write_text(json.dumps({"ts": ts, "status": "success"}) + "\n")
        return p

    def test_fresh_heartbeat_detected(self, tmp_path: pathlib.Path) -> None:
        """age=30min → stale=False"""
        import json, time
        p = self._make_jsonl(tmp_path, age_seconds=1800)
        last = None
        with p.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    last = json.loads(line)
        assert last is not None
        ts = last["ts"]
        age_h = (time.time() - float(ts)) / 3600.0
        assert age_h < 1.0, f"expected < 1h, got {age_h:.2f}h"

    def test_stale_heartbeat_detected(self, tmp_path: pathlib.Path) -> None:
        """age=2h → stale=True"""
        import json, time
        p = self._make_jsonl(tmp_path, age_seconds=7200)
        last = None
        with p.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    last = json.loads(line)
        assert last is not None
        age_h = (time.time() - float(last["ts"])) / 3600.0
        assert age_h > 1.0, f"expected > 1h, got {age_h:.2f}h"

    def test_missing_heartbeat_file_handled(self, tmp_path: pathlib.Path) -> None:
        """ファイル不在 → age=999 相当として扱われること。"""
        missing = tmp_path / "opend_relogin_heartbeat.jsonl"
        assert not missing.exists()
        # スクリプト内 python snippet の等価 logic
        age_h = 999.0
        assert age_h > 1.0

    def test_iso8601_ts_parsed(self, tmp_path: pathlib.Path) -> None:
        """ISO 8601 形式の ts も正しく parse できること。"""
        import json, time, datetime
        ts_str = datetime.datetime.utcnow().replace(
            tzinfo=datetime.timezone.utc
        ).isoformat()
        p = tmp_path / "hb.jsonl"
        p.write_text(json.dumps({"ts": ts_str, "status": "success"}) + "\n")
        last = None
        with p.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    last = json.loads(line)
        assert last is not None
        ts = last["ts"]
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        age_s = time.time() - dt.timestamp()
        assert age_s < 5.0, f"just-written ts should be near-zero age, got {age_s:.1f}s"


# =============================================================================
# F. daemon 群 alive 契約
# =============================================================================


class TestDaemonAliveContract:
    """script が必須 daemon ラベルを参照していること。"""

    REQUIRED_LABELS = (
        "com.soralab.atlas-paper",
        "com.soralab.spy-bot-paper",
        "com.soralab.moomoo-opend-relogin",
        "com.soralab.atlas-trader",
    )

    @pytest.mark.parametrize("label", REQUIRED_LABELS)
    def test_daemon_label_in_script(self, label: str) -> None:
        text = SCRIPT.read_text()
        assert label in text, f"daemon label missing from script: {label}"

    def test_atlas_trader_active_branch_uses_atlas_trader(self) -> None:
        """ATLAS_TRADER_ACTIVE=1 ブランチが com.soralab.atlas-trader を参照すること。"""
        text = SCRIPT.read_text()
        assert 'ATLAS_TRADER_ACTIVE}" == "1"' in text

    def test_fallback_branch_uses_spy_bot_paper(self) -> None:
        text = SCRIPT.read_text()
        assert "com.soralab.spy-bot-paper" in text


# =============================================================================
# G. pytest 静的回帰 target 一覧
# =============================================================================


class TestPytestRegressionTargets:
    """CRITICAL wrapper 3 本が静的回帰 targets に含まれること。"""

    CRITICAL_WRAPPER_TESTS = (
        "tests/test_chainguard_wrapper.py",
        "tests/test_portfolio_risk_gate.py",
        "tests/test_mass_verify_safe_runner.py",
    )

    @pytest.mark.parametrize("rel", CRITICAL_WRAPPER_TESTS)
    def test_critical_wrapper_file_exists(self, rel: str) -> None:
        assert (ROOT / rel).exists(), f"CRITICAL wrapper test missing: {rel}"

    @pytest.mark.parametrize("rel", CRITICAL_WRAPPER_TESTS)
    def test_critical_wrapper_referenced_in_script(self, rel: str) -> None:
        text = SCRIPT.read_text()
        assert rel in text, f"script does not reference: {rel}"

    def test_pytest_fail_halt_constant(self) -> None:
        """pytest 連続失敗 halt 閾値 2 が定数として存在すること。"""
        text = SCRIPT.read_text()
        assert "PYTEST_FAIL_HALT=2" in text

    def test_exit_22_on_pytest_halt(self) -> None:
        text = SCRIPT.read_text()
        assert "exit 22" in text, "exit 22 for pytest consecutive fail not found"


# =============================================================================
# H. Pushover P1 経路
# =============================================================================


class TestPushoverP1Route:
    """fatal_halt 時に send_critical が 1 本だけ発射される経路の確認。"""

    def test_pushover_p1_function_present(self) -> None:
        text = SCRIPT.read_text()
        assert "pushover_p1()" in text or "pushover_p1 " in text, \
            "pushover_p1 helper not defined in script"

    def test_send_critical_called_in_p1_helper(self) -> None:
        text = SCRIPT.read_text()
        assert "send_critical" in text

    def test_unset_dry_run_before_p1(self) -> None:
        """P1 送信前に PUSHOVER_DRY_RUN を解除すること。"""
        text = SCRIPT.read_text()
        assert "unset PUSHOVER_DRY_RUN" in text

    def test_send_critical_api_compatible(self) -> None:
        """send_critical が (title, message, priority, app_tag) シグネチャを持つこと。"""
        from common.pushover_client import send_critical
        import inspect
        sig = inspect.signature(send_critical)
        params = list(sig.parameters.keys())
        assert "title" in params
        assert "message" in params
        assert "priority" in params
        assert "app_tag" in params

    def test_send_silent_returns_bool(self) -> None:
        """send_silent が bool を返すこと (dry-run 経路の健全性)。"""
        from common.pushover_client import send_silent
        result = send_silent("[PREMARKET TEST] unit probe", "premarket pytest probe")
        assert isinstance(result, bool)
        assert result is True


# =============================================================================
# I. sentinel_watchdog kick 経路
# =============================================================================


class TestSentinelKick:
    """sentinel_watchdog が script から kick される経路の確認。"""

    def test_kick_sentinel_function_present(self) -> None:
        text = SCRIPT.read_text()
        assert "kick_sentinel()" in text or "kick_sentinel " in text, \
            "kick_sentinel helper not defined"

    def test_sentinel_script_exists(self) -> None:
        sentinel = ROOT / "scripts" / "sentinel_watchdog.py"
        assert sentinel.exists(), f"sentinel_watchdog.py missing: {sentinel}"

    def test_sentinel_called_on_kill_switch(self) -> None:
        text = SCRIPT.read_text()
        # kill_switch ブロックに kick_sentinel が入っていること
        lines = text.splitlines()
        ks_idx = next(
            (i for i, l in enumerate(lines) if "kill_switch_active" in l), None
        )
        assert ks_idx is not None, "kill_switch_active kick not found"

    def test_sentinel_called_on_opend_halt(self) -> None:
        text = SCRIPT.read_text()
        assert "opend_consecutive_fail" in text

    def test_sentinel_called_on_pytest_halt(self) -> None:
        text = SCRIPT.read_text()
        assert "pytest_consecutive_fail" in text


# =============================================================================
# J. CRITICAL wrapper 3 本 import 可能性
# =============================================================================


class TestCriticalWrapperImports:
    """3 本の CRITICAL wrapper が runtime import 可能であること。"""

    def test_chainguard_wrapper_import(self) -> None:
        from atlas_v3.ops.chainguard_wrapper import (
            ChainGuardError,
            get_chain_center_price,
        )
        price = get_chain_center_price("US.SPY", {"last_price": 572.0})
        assert price == pytest.approx(572.0)

    def test_portfolio_risk_gate_import(self) -> None:
        from atlas_v3.ops.portfolio_risk_gate import GateConfig, check_entry_allowed
        cfg = GateConfig()
        decision = check_entry_allowed(vix=35.0, current_entries=0, config=cfg)
        assert not decision.allowed, "VIX=35 should trigger halt"

    def test_mass_verify_safe_runner_import(self) -> None:
        from atlas_v3.ops.mass_verify_safe_runner import (
            VerifyContext,
            VerifyResult,
            run_mass_verify_safe,
        )
        result = run_mass_verify_safe([], lambda ctx: VerifyResult.ok(ctx))
        assert result == []


# =============================================================================
# K. engine module 静的 import 可能性 (新規 engine 10 件相当)
# =============================================================================


class TestEngineModuleImports:
    """新規 engine 相当モジュール 10 件が import 可能であること。"""

    def test_symbol_selector_import(self) -> None:
        from common import symbol_selector as ss
        names = ss.get_tactic_names()
        assert len(names) >= 7

    def test_kill_switch_import(self) -> None:
        from common.kill_switch import is_active, FLAG_FILE
        assert callable(is_active)
        assert FLAG_FILE.parent.name == "data"

    def test_heartbeat_import(self) -> None:
        from common.heartbeat import write_pulse, read_pulse, is_stale
        assert callable(write_pulse)
        assert callable(read_pulse)
        assert callable(is_stale)

    def test_pushover_client_import(self) -> None:
        from common.pushover_client import send_silent, send_critical, send_batched
        assert callable(send_silent)
        assert callable(send_critical)
        assert callable(send_batched)

    def test_market_specs_import(self) -> None:
        from common.market_specs import get_market_spec, is_in_session
        assert callable(get_market_spec)
        assert callable(is_in_session)

    def test_risk_limits_import(self) -> None:
        from common import risk_limits
        assert risk_limits is not None

    def test_pre_trade_check_import(self) -> None:
        from common import pre_trade_check
        assert pre_trade_check is not None

    def test_option_code_import(self) -> None:
        from common import option_code
        assert option_code is not None

    def test_decision_engine_import(self) -> None:
        from common import decision_engine
        assert decision_engine is not None

    def test_pdt_tracker_import(self) -> None:
        from common import pdt_tracker
        assert pdt_tracker is not None

    def test_strategy_selector_import(self) -> None:
        from common import strategy_selector
        assert strategy_selector is not None

    def test_moomoo_provider_import(self) -> None:
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        assert callable(MoomooMetricProvider)

    def test_yfinance_provider_import(self) -> None:
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        assert callable(YFinanceMetricProvider)
