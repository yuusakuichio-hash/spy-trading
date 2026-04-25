"""
tests/test_morning_digest_auth_budget_20260425.py
— Morning Digest auth_budget section 統合テスト (Medium #6)

カバレッジ:
  T-01: _build_auth_budget_section — 全サービス試行ゼロなら空文字
  T-02: _build_auth_budget_section — 上限到達で alert 行を含む
  T-03: _build_auth_budget_section — 残り1回で alert 行を含む
  T-04: _build_auth_budget_section — 成功率低下で失敗 note を含む
  T-05: _build_auth_budget_section — 次リセット時刻が "HH:MM JST" 形式
  T-06: _build_auth_budget_section — critical マーク付きサービスに [CRITICAL] を含む
  T-07: _build_auth_budget_section — ImportError 時に空文字を返す（例外非伝播）
  T-08: send_morning_digest — auth_section がメッセージ本文に組み込まれる
  T-09: send_morning_digest — auth_section が空のときはメッセージに認証行がない
  T-10: _auth_next_reset_jst — 試行なしで "未使用" を返す
  T-11: _auth_next_reset_jst — 試行ありで reset 時刻 (oldest_ts + window_sec) が返る
  T-12: _auth_recent_failures — 成功のみなら空リスト
  T-13: _auth_recent_failures — 失敗レコードの note を最大 limit 件返す
  T-14: _build_auth_budget_section — 残り 2+ かつ成功率 100% なら表示しない
  T-15: send_morning_digest — Pushover 未設定でもログのみ完了 (True 返す)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# プロジェクトルートを sys.path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

import common.auth_budget as _ab
from scripts.morning_digest_send import (
    _build_auth_budget_section,
    _auth_next_reset_jst,
    _auth_recent_failures,
    send_morning_digest,
)


# ── フィクスチャ ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_auth_budget(tmp_path, monkeypatch):
    """各テスト前後に auth_budget の BUDGET_DIR を tmp_path に向け、
    本番 data/auth_budget/ を汚染しないようにする。"""
    monkeypatch.setenv("AUTH_BUDGET_BYPASS", "")
    original_dir = _ab.BUDGET_DIR
    _ab.BUDGET_DIR = tmp_path
    yield tmp_path
    _ab.BUDGET_DIR = original_dir


def _write_attempt(
    tmp_path: Path,
    service: str,
    *,
    ts: float | None = None,
    success: bool = True,
    note: str = "",
) -> None:
    """テスト用に直接 JSONL に試行レコードを書き込む"""
    if ts is None:
        ts = time.time()
    rec = {
        "ts": ts,
        "dt": "2026-01-01T00:00:00Z",
        "service": service,
        "success": success,
        "note": note,
    }
    path = tmp_path / f"{service}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ── T-01 ~ T-07: _build_auth_budget_section ───────────────────────────────────

class TestBuildAuthBudgetSection:

    def test_t01_empty_when_no_attempts(self, _isolate_auth_budget):
        """全サービス試行ゼロなら空文字を返す"""
        result = _build_auth_budget_section()
        assert result == "", f"試行ゼロで空文字を期待したが: {repr(result)}"

    def test_t02_exhausted_service_in_alert(self, _isolate_auth_budget):
        """上限到達サービスが alert 行に含まれる"""
        tmp_path = _isolate_auth_budget
        spec = _ab.SERVICES["tradovate_demo"]
        for i in range(spec["max"]):
            _write_attempt(tmp_path, "tradovate_demo", success=False, note=f"fail_{i}")

        result = _build_auth_budget_section()
        assert result != "", "上限到達で空文字でないはず"
        assert "tradovate_demo" in result
        assert "上限到達" in result

    def test_t03_remaining_one_in_alert(self, _isolate_auth_budget):
        """残り1回のサービスが alert 行に含まれる"""
        tmp_path = _isolate_auth_budget
        spec = _ab.SERVICES["tradovate_demo"]
        # max-1 回記録 → remaining=1
        for i in range(spec["max"] - 1):
            _write_attempt(tmp_path, "tradovate_demo", success=True, note=f"ok_{i}")

        result = _build_auth_budget_section()
        assert result != "", "残り1回で空文字でないはず"
        assert "tradovate_demo" in result
        assert "残り1回" in result

    def test_t04_low_success_rate_includes_failure_note(self, _isolate_auth_budget):
        """成功率低下で失敗 note が含まれる"""
        tmp_path = _isolate_auth_budget
        # tradovate_demo max=4。上限到達させないように count=3 (remaining=1)
        # だと「残り1回」ブランチに先に入るため、mffu (max=5) を使う。
        # mffu: max=5, critical=False
        # 成功1 + 失敗2 = 成功率33% < 50%, count=3 >= 2, remaining=2
        _write_attempt(tmp_path, "mffu", success=True, note="ok_1")
        _write_attempt(tmp_path, "mffu", success=False, note="AUTH_FAIL_A")
        _write_attempt(tmp_path, "mffu", success=False, note="AUTH_FAIL_B")

        result = _build_auth_budget_section()
        assert "mffu" in result, f"mffu が表示されていない: {result}"
        assert "成功率" in result, f"成功率表示が見当たらない: {result}"
        assert "AUTH_FAIL" in result, f"失敗 note が含まれていない: {result}"

    def test_t05_next_reset_jst_format(self, _isolate_auth_budget):
        """次リセット時刻が 'HH:MM JST' 形式を含む"""
        tmp_path = _isolate_auth_budget
        spec = _ab.SERVICES["tradovate_demo"]
        for i in range(spec["max"]):
            _write_attempt(tmp_path, "tradovate_demo", success=False, note=f"f_{i}")

        result = _build_auth_budget_section()
        assert "JST" in result, f"'JST' が含まれていない: {result}"
        # HH:MM JST パターン確認
        import re
        assert re.search(r"\d{2}:\d{2} JST", result), f"HH:MM JST パターンが見当たらない: {result}"

    def test_t06_critical_mark_in_critical_service(self, _isolate_auth_budget):
        """critical=True のサービスに [CRITICAL] が含まれる"""
        tmp_path = _isolate_auth_budget
        # opend: max=3, critical=True
        spec = _ab.SERVICES["opend"]
        for i in range(spec["max"]):
            _write_attempt(tmp_path, "opend", success=False, note=f"opend_fail_{i}")

        result = _build_auth_budget_section()
        assert "opend" in result
        assert "[CRITICAL]" in result, f"[CRITICAL] マークが見当たらない: {result}"

    def test_t07_importerror_returns_empty(self, _isolate_auth_budget, monkeypatch):
        """ImportError 時に空文字を返す（例外非伝播）"""
        # builtins.__import__ を上書きして auth_budget import を失敗させる
        import builtins
        original_import = builtins.__import__

        def _failing_import(name, *args, **kwargs):
            if "auth_budget" in name:
                raise ImportError("mocked import failure")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _failing_import)
        result = _build_auth_budget_section()
        assert result == "", f"ImportError 時は空文字を期待したが: {repr(result)}"


# ── T-08 ~ T-09: send_morning_digest との統合 ────────────────────────────────

class TestSendMorningDigestAuthIntegration:

    def _make_queue_entry(self, title: str = "test", priority: int = 0) -> dict:
        return {
            "ts": time.time(),
            "title": title,
            "message": "test message",
            "priority": priority,
        }

    def test_t08_auth_section_included_in_message(self, _isolate_auth_budget, tmp_path, monkeypatch):
        """auth_section がメッセージ本文に組み込まれる"""
        # tradovate_demo を上限到達させる
        ab_tmp = _isolate_auth_budget
        spec = _ab.SERVICES["tradovate_demo"]
        for i in range(spec["max"]):
            _write_attempt(ab_tmp, "tradovate_demo", success=False, note=f"f_{i}")

        # morning queue に 1 件入れる
        queue_path = tmp_path / "pushover_morning_queue.jsonl"
        with open(queue_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._make_queue_entry()) + "\n")

        sent_messages: list[str] = []

        def mock_http_post(tok, user, title, message, priority=0):
            sent_messages.append(message)
            return True, False

        monkeypatch.setenv("PUSHOVER_TOKEN", "fake_token")
        monkeypatch.setenv("PUSHOVER_USER", "fake_user")

        import scripts.morning_digest_send as _mds
        monkeypatch.setattr(_mds, "_load_morning_queue", lambda: [self._make_queue_entry()])
        monkeypatch.setattr(_mds, "_clear_morning_queue", lambda: None)
        monkeypatch.setattr(_mds, "_http_post", mock_http_post)
        monkeypatch.setattr(_mds, "_DEFAULT_TOKEN", "fake_token")
        monkeypatch.setattr(_mds, "_DEFAULT_USER", "fake_user")

        result = _mds.send_morning_digest()
        assert result is True
        assert len(sent_messages) == 1
        assert "認証試行" in sent_messages[0], (
            f"auth_section がメッセージに含まれていない: {sent_messages[0]}"
        )

    def test_t09_no_auth_section_when_clean(self, _isolate_auth_budget, monkeypatch):
        """auth_section が空のときはメッセージに認証行がない"""
        # 試行ゼロ → auth_section = ""

        sent_messages: list[str] = []

        def mock_http_post(tok, user, title, message, priority=0):
            sent_messages.append(message)
            return True, False

        import scripts.morning_digest_send as _mds
        monkeypatch.setattr(_mds, "_load_morning_queue", lambda: [
            {"ts": time.time(), "title": "自己修復完了", "message": "ok", "priority": 0}
        ])
        monkeypatch.setattr(_mds, "_clear_morning_queue", lambda: None)
        monkeypatch.setattr(_mds, "_http_post", mock_http_post)
        monkeypatch.setattr(_mds, "_DEFAULT_TOKEN", "fake_token")
        monkeypatch.setattr(_mds, "_DEFAULT_USER", "fake_user")

        result = _mds.send_morning_digest()
        assert result is True
        assert len(sent_messages) == 1
        assert "認証試行" not in sent_messages[0], (
            f"試行ゼロなのに認証行が含まれた: {sent_messages[0]}"
        )


# ── T-10 ~ T-11: _auth_next_reset_jst ────────────────────────────────────────

class TestAuthNextResetJst:

    def test_t10_no_attempts_returns_unused(self, _isolate_auth_budget):
        """試行なしで '未使用' を返す"""
        result = _auth_next_reset_jst("tradovate_demo", 3600)
        assert result == "未使用", f"期待: '未使用', 実際: {result}"

    def test_t11_returns_correct_reset_time(self, _isolate_auth_budget):
        """試行ありで oldest_ts + window_sec が JST で返る"""
        tmp_path = _isolate_auth_budget
        now = time.time()
        # 1時間前に試行
        old_ts = now - 1800  # 30分前
        _write_attempt(tmp_path, "tradovate_demo", ts=old_ts, success=False)

        window_sec = 3600
        result = _auth_next_reset_jst("tradovate_demo", window_sec)
        assert result != "未使用"
        assert "JST" in result

        # 計算された reset 時刻が oldest_ts + window_sec に対応するか検証
        # reset_jst = (old_ts + window_sec) → UTC + 9h
        expected_reset_ts = old_ts + window_sec
        import re
        m = re.match(r"(\d{2}):(\d{2}) JST", result)
        assert m is not None, f"形式不正: {result}"
        hh, mm = int(m.group(1)), int(m.group(2))
        jst_struct = time.gmtime(expected_reset_ts + 9 * 3600)
        assert hh == jst_struct.tm_hour, f"時: 期待 {jst_struct.tm_hour}, 実際 {hh}"
        assert mm == jst_struct.tm_min, f"分: 期待 {jst_struct.tm_min}, 実際 {mm}"


# ── T-12 ~ T-13: _auth_recent_failures ───────────────────────────────────────

class TestAuthRecentFailures:

    def test_t12_success_only_returns_empty(self, _isolate_auth_budget):
        """成功のみなら空リスト"""
        tmp_path = _isolate_auth_budget
        _write_attempt(tmp_path, "tradovate_demo", success=True, note="ok_1")
        _write_attempt(tmp_path, "tradovate_demo", success=True, note="ok_2")

        result = _auth_recent_failures("tradovate_demo", 3600)
        assert result == [], f"成功のみで空リストを期待したが: {result}"

    def test_t13_failure_notes_returned_up_to_limit(self, _isolate_auth_budget):
        """失敗レコードの note を最大 limit 件返す"""
        tmp_path = _isolate_auth_budget
        notes = ["FAIL_A", "FAIL_B", "FAIL_C", "FAIL_D", "FAIL_E"]
        now = time.time()
        for i, note in enumerate(notes):
            _write_attempt(
                tmp_path, "tradovate_demo",
                ts=now - (len(notes) - i),  # 古い順に記録
                success=False,
                note=note,
            )

        result = _auth_recent_failures("tradovate_demo", 3600, limit=3)
        assert len(result) == 3, f"limit=3 で 3件を期待したが: {len(result)} 件"
        # 新しい順（降順）で返るため最新の 3 件が含まれる
        for note in result:
            assert note in notes, f"予期しない note: {note}"


# ── T-14 ~ T-15: 追加カバレッジ ──────────────────────────────────────────────

class TestEdgeCases:

    def test_t14_healthy_service_not_displayed(self, _isolate_auth_budget):
        """残り 2+ かつ成功率 100% のサービスは表示しない"""
        tmp_path = _isolate_auth_budget
        # tradovate_demo max=4 のうち 2 回成功（remaining=2, rate=100%）
        _write_attempt(tmp_path, "tradovate_demo", success=True, note="ok_1")
        _write_attempt(tmp_path, "tradovate_demo", success=True, note="ok_2")

        result = _build_auth_budget_section()
        assert result == "", f"健全なサービスは表示しないはず: {result}"

    def test_t15_send_digest_no_pushover_env(self, _isolate_auth_budget, monkeypatch, caplog):
        """Pushover 未設定でもキューをクリアし True を返す"""
        import scripts.morning_digest_send as _mds
        monkeypatch.setattr(_mds, "_DEFAULT_TOKEN", "")
        monkeypatch.setattr(_mds, "_DEFAULT_USER", "")
        cleared = []
        monkeypatch.setattr(_mds, "_load_morning_queue", lambda: [
            {"ts": time.time(), "title": "test", "message": "msg", "priority": 0}
        ])
        monkeypatch.setattr(_mds, "_clear_morning_queue", lambda: cleared.append(True))

        result = _mds.send_morning_digest()
        assert result is True
        assert cleared, "キューがクリアされなかった"
