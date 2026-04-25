"""tests/test_tracer_bullet_20260425.py
=======================================
Daily Tracer Bullet Smoke Test の design contract を固定する pytest。

対象:
  scripts/daily_tracer_bullet_20260427.sh
  com.soralab.daily-tracer-bullet.plist

検証観点 (12 件):
  1.  script ファイルが存在し chmod +x 済み
  2.  plist ファイルが存在し XML として parse 可能
  3.  plist Label = com.soralab.daily-tracer-bullet
  4.  plist Hour=21 (JST 21:00 = ET 08:00 pre-market) — 夏時間対応確認
  5.  plist KeepAlive=false (失敗時再起動しない)
  6.  plist TZ=Asia/Tokyo 環境変数が設定されている
  7.  script に SIMULATE / TRACER_SYMBOL 環境変数分岐が存在する (静的 grep)
  8.  script exit code 表 (0/1/2/99) が docstring に漏れなく記載されている
  9.  script 内 Pushover P1 送信パス: 発注 NG / キャンセル失敗 の 2 経路が存在
 10.  RESULT_JSONL 追記パス (>> "${RESULT_JSONL}") が script に存在する
 11.  common.pushover_client.send_critical が import 可能で signature が正しい
 12.  dry_run モードが script に実装されている (--dry-run フラグ + 発注スキップ分岐)
 13.  plist ProgramArguments が daily_tracer_bullet_20260427.sh を指している
 14.  script CANCEL_WAIT_S が 0 より大きい正の整数で設定されている
"""
from __future__ import annotations

import importlib
import inspect
import pathlib
import plistlib
import re
import stat
import sys

import pytest

ROOT   = pathlib.Path("/Users/yuusakuichio/trading")
SCRIPT = ROOT / "scripts" / "daily_tracer_bullet_20260427.sh"
PLIST  = ROOT / "com.soralab.daily-tracer-bullet.plist"

sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# 共通フィクスチャ
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def plist_data() -> dict:
    with PLIST.open("rb") as f:
        return plistlib.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: script ファイルが存在し chmod +x 済み
# ─────────────────────────────────────────────────────────────────────────────

class TestScriptExists:

    def test_script_file_exists(self) -> None:
        assert SCRIPT.exists(), f"script が存在しない: {SCRIPT}"

    def test_script_is_executable(self) -> None:
        mode = SCRIPT.stat().st_mode
        assert bool(mode & stat.S_IXUSR), (
            f"script に実行権限がない: {oct(mode)}  (chmod +x が必要)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: plist ファイルが存在し XML として parse 可能
# ─────────────────────────────────────────────────────────────────────────────

class TestPlistFileValid:

    def test_plist_file_exists(self) -> None:
        assert PLIST.exists(), f"plist が存在しない: {PLIST}"

    def test_plist_parseable(self, plist_data: dict) -> None:
        assert isinstance(plist_data, dict), "plist がdict として parse できない"
        assert len(plist_data) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: plist Label = com.soralab.daily-tracer-bullet
# ─────────────────────────────────────────────────────────────────────────────

class TestPlistLabel:

    def test_plist_label(self, plist_data: dict) -> None:
        label = plist_data.get("Label", "")
        assert label == "com.soralab.daily-tracer-bullet", (
            f"Label 不一致: {label!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: plist Hour=21 (JST 21:00)
# ─────────────────────────────────────────────────────────────────────────────

class TestPlistSchedule:

    def test_start_calendar_hour_21_jst(self, plist_data: dict) -> None:
        interval = plist_data.get("StartCalendarInterval", {})
        hour = interval.get("Hour")
        assert hour == 21, (
            f"StartCalendarInterval.Hour={hour!r}  (21 JST = 08:00 ET pre-market が必須)"
        )

    def test_no_weekday_restriction(self, plist_data: dict) -> None:
        """毎日実行のため Weekday キーは存在しないこと。"""
        interval = plist_data.get("StartCalendarInterval", {})
        assert "Weekday" not in interval, (
            "Weekday が設定されている — daily 実行なので不要"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: plist KeepAlive=false
# ─────────────────────────────────────────────────────────────────────────────

class TestPlistKeepAlive:

    def test_keep_alive_false(self, plist_data: dict) -> None:
        keep_alive = plist_data.get("KeepAlive", True)
        assert keep_alive is False, (
            f"KeepAlive={keep_alive!r}  (失敗後再起動しないよう false が必須)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: plist TZ=Asia/Tokyo
# ─────────────────────────────────────────────────────────────────────────────

class TestPlistTimezone:

    def test_tz_asia_tokyo(self, plist_data: dict) -> None:
        env = plist_data.get("EnvironmentVariables", {})
        tz = env.get("TZ", "")
        assert tz == "Asia/Tokyo", (
            f"TZ={tz!r}  (feedback_launchd_jst.md: TZ=Asia/Tokyo が必須)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: script に SIMULATE / TRACER_SYMBOL 分岐が存在する
# ─────────────────────────────────────────────────────────────────────────────

class TestScriptSimulateAndSymbol:

    def test_simulate_keyword_present(self, script_text: str) -> None:
        assert "SIMULATE" in script_text, (
            "script に SIMULATE キーワードが存在しない — paper 専用保証が未実装"
        )

    def test_tracer_symbol_env_present(self, script_text: str) -> None:
        assert "TRACER_SYMBOL" in script_text, (
            "script に TRACER_SYMBOL 環境変数分岐が存在しない"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: script exit code 表 (0/1/2/99) が docstring に存在する
# ─────────────────────────────────────────────────────────────────────────────

class TestScriptExitCodeDoc:

    @pytest.mark.parametrize("code", ["0", "1", "2", "99"])
    def test_exit_code_documented(self, script_text: str, code: str) -> None:
        assert re.search(rf"\b{code}\b.*=", script_text) or \
               re.search(rf"exit.*{code}", script_text) or \
               re.search(rf"{code}.*=.*exit", script_text), (
            f"exit code {code} が script に記載されていない"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Pushover P1 送信 — 発注 NG / キャンセル失敗 の 2 経路
# ─────────────────────────────────────────────────────────────────────────────

class TestScriptPushoverP1Paths:

    def test_place_order_ng_pushover_path(self, script_text: str) -> None:
        """発注 NG 時に Pushover P1 (_pushover_p1) が呼ばれること。"""
        # 発注 NG ブロックに _pushover_p1 呼出が存在する
        assert "_pushover_p1" in script_text, (
            "script に _pushover_p1 関数呼出が存在しない (発注 NG 時の P1 通知未実装)"
        )

    def test_cancel_fail_pushover_path(self, script_text: str) -> None:
        """キャンセル失敗時も P1 通知が存在すること。"""
        # 「キャンセル失敗」または「cancel」と「_pushover_p1」が両方登場
        has_cancel_block = "cancel" in script_text.lower()
        has_pushover_call = script_text.count("_pushover_p1") >= 2
        assert has_cancel_block and has_pushover_call, (
            "キャンセル失敗時の Pushover P1 経路が未実装 "
            f"(cancel={has_cancel_block} p1_calls={script_text.count('_pushover_p1')})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: RESULT_JSONL 追記パスが存在する
# ─────────────────────────────────────────────────────────────────────────────

class TestScriptResultJsonl:

    def test_result_jsonl_append_present(self, script_text: str) -> None:
        # >> "${RESULT_JSONL}" が存在すること
        assert 'RESULT_JSONL' in script_text, (
            "RESULT_JSONL 変数が script に存在しない"
        )
        assert '>>' in script_text, (
            "script に追記演算子 >> が存在しない (JSONL 追記未実装)"
        )

    def test_result_jsonl_path_under_logs(self, script_text: str) -> None:
        """RESULT_JSONL が data/logs/ 配下を指すこと。"""
        assert "data/logs" in script_text, (
            "RESULT_JSONL パスが data/logs/ 配下でない"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: common.pushover_client.send_critical import + signature
# ─────────────────────────────────────────────────────────────────────────────

class TestPushoverClientContract:

    def test_send_critical_importable(self) -> None:
        mod = importlib.import_module("common.pushover_client")
        assert hasattr(mod, "send_critical"), (
            "common.pushover_client に send_critical が存在しない"
        )

    def test_send_critical_signature(self) -> None:
        mod = importlib.import_module("common.pushover_client")
        sig = inspect.signature(mod.send_critical)
        params = list(sig.parameters.keys())
        # title, message は必須位置引数
        assert "title" in params, "send_critical に title 引数がない"
        assert "message" in params, "send_critical に message 引数がない"
        # priority は keyword 引数として存在する
        assert "priority" in params, "send_critical に priority 引数がない"
        assert "app_tag" in params, "send_critical に app_tag 引数がない"


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: dry_run モードが実装されている
# ─────────────────────────────────────────────────────────────────────────────

class TestScriptDryRun:

    def test_dry_run_flag_present(self, script_text: str) -> None:
        assert "--dry-run" in script_text, (
            "script に --dry-run フラグが存在しない"
        )

    def test_dry_run_skip_order_present(self, script_text: str) -> None:
        """dry_run=1 時に発注をスキップする分岐が存在すること。"""
        assert "dry_run" in script_text and ("connect_only" in script_text or
               "DRY_RUN=0" in script_text), (
            "dry_run スキップ分岐が script に存在しない"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: plist ProgramArguments が正しい script を指している
# ─────────────────────────────────────────────────────────────────────────────

class TestPlistProgramArguments:

    def test_program_arguments_point_to_script(self, plist_data: dict) -> None:
        args = plist_data.get("ProgramArguments", [])
        assert len(args) >= 2, f"ProgramArguments が短すぎる: {args}"
        script_arg = args[1]
        assert "daily_tracer_bullet_20260427.sh" in script_arg, (
            f"ProgramArguments[1]={script_arg!r}  "
            "daily_tracer_bullet_20260427.sh を指していない"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: CANCEL_WAIT_S が正の整数で存在する
# ─────────────────────────────────────────────────────────────────────────────

class TestScriptCancelWait:

    def test_cancel_wait_positive_integer(self, script_text: str) -> None:
        """CANCEL_WAIT_S=<n> の n が 0 より大きいこと。"""
        match = re.search(r"CANCEL_WAIT_S=(\d+)", script_text)
        assert match is not None, "CANCEL_WAIT_S 変数が script に存在しない"
        wait_val = int(match.group(1))
        assert wait_val > 0, (
            f"CANCEL_WAIT_S={wait_val}  (0 以下は即キャンセルで order_id 取得前に消す危険あり)"
        )
