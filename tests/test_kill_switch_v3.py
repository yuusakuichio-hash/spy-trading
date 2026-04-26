"""tests/test_kill_switch_v3.py — KillSwitch v3 テスト

Sprint 0.5 Day 3 #2 — spec B9 実装検証

テスト項目:
  T01: activate() 新規発動 → True を返す
  T02: activate() 二重呼出し → False (冪等スキップ)
  T03: activate() scope パラメータが flag に保存される
  T04: is_active() flag 存在時 True / 不在時 False
  T05: get_state() 発動中は dict / 未発動は None
  T06: deactivate() 正常解除 → True / flag 削除確認
  T07: deactivate() flag 不在時 early return → False (audit 追記なし)
  T08: FirmScopedKillSwitch 正常発動 → True / per-firm flag 作成
  T09: FirmScopedKillSwitch 発動時 global activate() 連動
  T10: FirmScopedKillSwitch 冪等スキップ → False
  T11: FirmScopedKillSwitch deactivate() 正常解除 → per-firm flag 削除
  T12: FirmScopedKillSwitch deactivate() flag 不在 → False (early return)
  T13: audit log が append-only で正しく記録される
  T14: concurrent activate() race — 1 回だけ True
  T15: 無効 firm で ValueError
  T16: deactivate() early return 時 audit log に追記しない
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

# ── sys.path 設定 ─────────────────────────────────────────────────────────────
_tests_dir = Path(__file__).parent
_project_dir = _tests_dir.parent
for _p in [str(_project_dir), str(_project_dir.parent)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── import ────────────────────────────────────────────────────────────────────
import common_v3.risk.kill_switch as ks_module
from common_v3.risk.kill_switch import (
    FLAG_FILE,
    AUDIT_FILE,
    FirmScopedKillSwitch,
    activate,
    deactivate,
    get_state,
    is_active,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """各テスト前後に state_v3 ディレクトリを tmp_path に差し替える。

    既存の data/state_v3/ を汚染しない。
    """
    state_dir = tmp_path / "state_v3"
    state_dir.mkdir()

    # モジュール内のパス定数を tmp_path に向ける
    monkeypatch.setattr(ks_module, "_STATE_DIR", state_dir)
    monkeypatch.setattr(ks_module, "FLAG_FILE", state_dir / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", state_dir / "kill_switch_audit.jsonl")

    yield

    # teardown: フラグファイルをクリーンアップ (残留防止)
    for f in state_dir.glob("*.flag"):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def _read_audit_lines(tmp_path) -> list[dict]:
    """audit JSONL を読み込んで dict リストで返す。"""
    audit_file = tmp_path / "state_v3" / "kill_switch_audit.jsonl"
    if not audit_file.exists():
        return []
    lines = [
        json.loads(line)
        for line in audit_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return lines


def _firm_flag(tmp_path, firm: str) -> Path:
    return tmp_path / "state_v3" / f"kill_switch_{firm}.flag"


# ── T01: 新規発動 → True ──────────────────────────────────────────────────────

def test_T01_activate_new_returns_true(tmp_path):
    result = activate(reason="test_reason", activator="test_agent")
    assert result is True, "新規発動は True を返すべき"


# ── T02: 冪等スキップ → False ─────────────────────────────────────────────────

def test_T02_activate_idempotent_returns_false(tmp_path):
    first = activate(reason="first", activator="a1")
    second = activate(reason="second", activator="a2")
    assert first is True
    assert second is False, "既 ARMED 時は False を返す (冪等スキップ)"


# ── T03: scope パラメータ保存 ─────────────────────────────────────────────────

def test_T03_activate_scope_stored_in_flag(tmp_path):
    scope = {"tactic": "orb", "symbol": "SPY"}
    activate(reason="scope_test", activator="builder", scope=scope)
    state = get_state()
    assert state is not None
    assert state.get("scope") == scope, "scope が flag に保存されるべき"


# ── T04: is_active() ─────────────────────────────────────────────────────────

def test_T04_is_active_reflects_flag_existence(tmp_path):
    assert is_active() is False, "未発動時は False"
    activate(reason="r", activator="a")
    assert is_active() is True, "発動後は True"
    deactivate(activator="a")
    assert is_active() is False, "解除後は False"


# ── T05: get_state() ─────────────────────────────────────────────────────────

def test_T05_get_state_returns_dict_or_none(tmp_path):
    assert get_state() is None, "未発動時は None"
    activate(reason="state_test", activator="tester")
    state = get_state()
    assert isinstance(state, dict)
    assert state["reason"] == "state_test"
    assert state["activator"] == "tester"
    assert "activated_at" in state


# ── T06: deactivate() 正常解除 ───────────────────────────────────────────────

def test_T06_deactivate_returns_true_and_removes_flag(tmp_path):
    activate(reason="deact_test", activator="a")
    assert is_active() is True
    result = deactivate(activator="b", reason="test_deactivate")
    assert result is True, "正常解除は True を返す"
    assert is_active() is False, "flag が削除されるべき"


# ── T07: deactivate() early return ───────────────────────────────────────────

def test_T07_deactivate_early_return_when_not_active(tmp_path):
    # flag が存在しない状態で deactivate
    result = deactivate(activator="nobody")
    assert result is False, "FLAG_FILE 不在時は False (early return)"


# ── T08: FirmScopedKillSwitch 正常発動 ───────────────────────────────────────

def test_T08_firm_scoped_activate_creates_per_firm_flag(tmp_path):
    fks = FirmScopedKillSwitch(firm="mffu")
    result = fks.activate(reason="rule_violation", activator="chronos")
    assert result is True
    assert fks.is_active() is True
    # per-firm フラグファイルが存在する
    assert _firm_flag(tmp_path, "mffu").exists()


# ── T09: FirmScopedKillSwitch → global 連動 ──────────────────────────────────

def test_T09_firm_activate_triggers_global_kill_switch(tmp_path):
    fks = FirmScopedKillSwitch(firm="tradeify")
    assert is_active() is False
    fks.activate(reason="drawdown_limit", activator="atlas")
    # global flag も発動されているべき
    assert is_active() is True, "per-firm 発動時 global activate() が連動するべき"


# ── T10: FirmScopedKillSwitch 冪等スキップ ────────────────────────────────────

def test_T10_firm_scoped_activate_idempotent(tmp_path):
    fks = FirmScopedKillSwitch(firm="apex")
    first = fks.activate(reason="first", activator="a")
    second = fks.activate(reason="second", activator="b")
    assert first is True
    assert second is False, "per-firm 二重発動は False"


# ── T11: FirmScopedKillSwitch deactivate() 正常解除 ──────────────────────────

def test_T11_firm_scoped_deactivate_removes_per_firm_flag(tmp_path):
    fks = FirmScopedKillSwitch(firm="bulenox")
    fks.activate(reason="test", activator="a")
    assert fks.is_active() is True
    result = fks.deactivate(activator="admin")
    assert result is True
    assert fks.is_active() is False
    assert not _firm_flag(tmp_path, "bulenox").exists()


# ── T12: FirmScopedKillSwitch deactivate() early return ──────────────────────

def test_T12_firm_scoped_deactivate_early_return(tmp_path):
    fks = FirmScopedKillSwitch(firm="mffu")
    result = fks.deactivate(activator="nobody")
    assert result is False, "per-firm flag 不在時は False"


# ── T13: audit log append-only ───────────────────────────────────────────────

def test_T13_audit_log_append_only(tmp_path):
    activate(reason="first_activate", activator="a1")
    deactivate(activator="a1", reason="cleanup")
    activate(reason="second_activate", activator="a2")

    lines = _read_audit_lines(tmp_path)
    events = [e["event"] for e in lines]
    assert "activate" in events
    assert "deactivate" in events
    # activate が 2 回記録されている
    assert events.count("activate") == 2, "audit log に activate が 2 件記録されるべき"
    # 各エントリが必須フィールドを持つ
    for entry in lines:
        assert "ts" in entry
        assert "event" in entry
        assert "activator" in entry
        assert "pid" in entry


# ── T14: concurrent activate() から thread 呼出 → RuntimeError ──────────────
# Sprint 1 C-001: @sync_only が統合されたため、別スレッドからの activate() 呼出は
# RuntimeError("sync-only contract violation") を送出する。
# 以前のテスト意図（flock による 1 True 保証）は @sync_only により不要になった。
# 非 main thread からの呼出自体が物理的に禁止されるため。

def test_T14_concurrent_activate_only_one_true(tmp_path):
    """別スレッドから activate() を呼ぶと @sync_only が RuntimeError を送出する。

    Sprint 1 C-001 以前: 複数スレッドから activate() → True 1 回のみ確認
    Sprint 1 C-001 以降: 別スレッドからの呼出は RuntimeError → threads 内では
    results に True/False が追加されず、errors にエラーが追加される。
    """
    results: list[bool] = []
    errors: list[RuntimeError] = []
    lock = threading.Lock()

    def _try_activate():
        try:
            r = activate(reason="concurrent", activator="thread")
            with lock:
                results.append(r)
        except RuntimeError as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=_try_activate, name=f"T14-{i}") for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # @sync_only により全 10 スレッドが RuntimeError を送出する
    assert len(errors) == 10, f"全スレッドが RuntimeError のはず: errors={len(errors)}"
    assert len(results) == 0, "非 main thread からは results に追加されない"
    for e in errors:
        assert "sync-only contract violation" in str(e)


# ── T15: 無効 firm で ValueError ─────────────────────────────────────────────

def test_T15_invalid_firm_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="firm must be one of"):
        FirmScopedKillSwitch(firm="unknown_firm")  # type: ignore[arg-type]


# ── T16: deactivate early return 時 audit 追記なし ───────────────────────────

def test_T16_deactivate_early_return_no_audit_append(tmp_path):
    # flag を作らずに deactivate
    deactivate(activator="nobody")
    lines = _read_audit_lines(tmp_path)
    deactivate_events = [e for e in lines if e["event"] == "deactivate"]
    assert len(deactivate_events) == 0, (
        "FLAG_FILE 不在の deactivate は audit log に追記してはならない"
    )


# ── C-1: FirmScoped.activate 後に global が立っている ────────────────────────

def test_firm_activate_atomic_global_first(tmp_path):
    """FirmScopedKillSwitch.activate() 後に global flag が立っていることを確認。

    C-1 fix: global activate() が先に呼ばれるので per-firm 書込後に
    global が存在することは当然だが、明示的にアサートする。
    """
    fks = FirmScopedKillSwitch(firm="mffu")
    assert is_active() is False
    result = fks.activate(reason="c1_test", activator="redteam")
    assert result is True
    # global flag が立っている
    assert is_active() is True, (
        "FirmScopedKillSwitch.activate() 後に global is_active() が False — "
        "C-1 fix が機能していない"
    )
    # per-firm flag も立っている
    assert fks.is_active() is True


# ── C-1: per-firm flag 書込失敗でも global は立っている ─────────────────────

def test_firm_activate_crash_leaves_global_armed(tmp_path, monkeypatch):
    """per-firm flag 書込が失敗しても global は立っている (C-1 fail-safe)。

    _write_flag を per-firm 書込時だけ例外を投げるようにモックして
    「SIGKILL が per-firm 書込直前に割り込んだ」状況を再現する。
    global activate() は先に呼ばれているので global flag は既に存在する。
    """
    call_count = {"n": 0}
    original_write_flag = ks_module._write_flag

    def _mock_write_flag(path: Path, data: dict) -> None:
        call_count["n"] += 1
        # 最初の呼出は global FLAG_FILE 書込 → 通す
        # 2 回目は per-firm flag 書込 → クラッシュ模擬
        if call_count["n"] >= 2:
            raise OSError("simulated crash before per-firm write")
        original_write_flag(path, data)

    monkeypatch.setattr(ks_module, "_write_flag", _mock_write_flag)

    fks = FirmScopedKillSwitch(firm="apex")
    try:
        fks.activate(reason="crash_test", activator="redteam")
    except OSError:
        pass  # per-firm 書込クラッシュは期待通り

    # global flag は先に立っているので is_active() は True (安全側)
    assert is_active() is True, (
        "per-firm 書込クラッシュ後も global is_active() が True であるべき (C-1 安全側)"
    )


# ── C-2: 破損 JSON flag で get_state() が None でなく corrupted=True dict を返す

def test_read_flag_corrupted_returns_dict_not_none(tmp_path):
    """破損 JSON フラグファイルで get_state() が None でなく corrupted=True dict を返す。

    C-2 fix: except (json.JSONDecodeError, OSError) → fail-safe dict。
    呼出側の `get_state() is not None` guard が bypass されなくなる。

    ks_module.FLAG_FILE を参照する (_clean_state で monkeypatch 済みの値)。
    """
    # ks_module.FLAG_FILE は _clean_state fixture で tmp_path に向いている
    flag = ks_module.FLAG_FILE
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("{broken json <<<", encoding="utf-8")

    state = get_state()
    assert state is not None, (
        "破損 flag で get_state() が None を返した — "
        "C-2 fix が機能していない (guard bypass の危険)"
    )
    assert state.get("corrupted") is True, (
        f"破損 flag の get_state() に corrupted=True がない: {state}"
    )
    assert state.get("reason") == "CORRUPTED", (
        f"破損 flag の reason が 'CORRUPTED' でない: {state}"
    )


# ── C-001 r2: FirmScopedKillSwitch @sync_only テスト ─────────────────────────

def test_firm_scoped_deactivate_in_async_loop_raises(tmp_path):
    """FirmScopedKillSwitch.deactivate() を asyncio event loop 内から呼ぶと RuntimeError。

    @sync_only が FirmScopedKillSwitch.deactivate にも適用されていることを確認する。
    """
    import asyncio

    fks = FirmScopedKillSwitch(firm="mffu")
    # まず発動しておく (main thread から)
    fks.activate(reason="setup", activator="test")

    async def _call_deactivate_from_loop():
        # asyncio event loop 内から @sync_only メソッドを直接呼ぶ → RuntimeError
        fks.deactivate(activator="test")

    with pytest.raises(RuntimeError, match="sync-only contract violation"):
        asyncio.run(_call_deactivate_from_loop())


def test_firm_scoped_is_active_in_thread_raises(tmp_path):
    """FirmScopedKillSwitch.is_active() を非 main thread から呼ぶと RuntimeError。

    @sync_only が FirmScopedKillSwitch.is_active にも適用されていることを確認する。
    """
    fks = FirmScopedKillSwitch(firm="apex")
    errors: list[RuntimeError] = []
    lock = threading.Lock()

    def _call_from_thread():
        try:
            fks.is_active()
        except RuntimeError as e:
            with lock:
                errors.append(e)

    t = threading.Thread(target=_call_from_thread, name="T-firm-is-active")
    t.start()
    t.join()

    assert len(errors) == 1, f"非 main thread から FirmScoped.is_active() が RuntimeError を送出しなかった: {errors}"
    assert "sync-only contract violation" in str(errors[0])


def test_async_impl_is_active_via_to_thread_works(tmp_path):
    """async_impl.is_active_async() が @sync_only 関数を asyncio.to_thread 経由で正常呼出する。

    asyncio context から @sync_only 関数を直接呼ぶと RuntimeError だが、
    async_impl の to_thread ラッパーは別スレッドで実行するため成功する。
    """
    import asyncio
    from common_v3.executor.async_impl import is_active_async

    # 未発動状態で確認
    result_before = asyncio.run(is_active_async())
    assert result_before is False, "未発動時に is_active_async() が True を返した"

    # main thread から発動してから asyncio 経由で確認
    activate(reason="async_impl_test", activator="redteam")
    result_after = asyncio.run(is_active_async())
    assert result_after is True, "発動後に is_active_async() が False を返した"


# ── C-2: 破損 flag でも is_active() は True (fail-safe) ─────────────────────

def test_is_active_true_when_flag_corrupted(tmp_path):
    """破損 flag ファイルが存在するとき is_active() は True (fail-safe)。

    is_active() はファイル存在チェックのみなので破損内容に関わらず True。
    これが「発注 guard bypass を防ぐ」安全側の動作。

    ks_module.FLAG_FILE を参照する (_clean_state で monkeypatch 済みの値)。
    """
    flag = ks_module.FLAG_FILE
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("{broken json <<<", encoding="utf-8")

    assert is_active() is True, (
        "破損 flag が存在するのに is_active() が False — fail-safe 違反"
    )


# ── H-3: async_impl 経由の per-firm activate/deactivate E2E テスト ─────────────
# B16 規律: asyncio は sys.modules 経由で参照 (直接 import 禁止)

def _arun(coro):
    """asyncio.run 相当。B16 規律に従い sys.modules 経由で asyncio を参照。"""
    return sys.modules["asyncio"].run(coro)


def test_firm_activate_async_via_to_thread_works(tmp_path):
    """firm_activate_async が global + per-firm 両方を正しく立てる E2E 確認。

    CRITICAL-1 修正の核心テスト:
    修正前: to_thread(raw_fn, fks, ...) → raw_fn 内で global activate() 呼出
            → @sync_only が worker thread で RuntimeError → silent failure
    修正後: FirmScopedKillSwitch.activate の内部実装が _activate_raw を呼ぶため
            worker thread でも RuntimeError は発生しない
    """
    from common_v3.executor.async_impl import firm_activate_async

    fks = FirmScopedKillSwitch(firm="mffu")

    result = _arun(firm_activate_async(fks, reason="h3_test", activator="redteam"))
    assert result is True, "firm_activate_async が True を返さなかった"
    assert is_active() is True, (
        "firm_activate_async 後に global is_active() が False "
        "— CRITICAL-1 fix が機能していない"
    )
    assert fks.is_active() is True, "per-firm flag が立っていない"
    assert _firm_flag(tmp_path, "mffu").exists(), "per-firm flag ファイルが存在しない"


def test_firm_deactivate_async_works(tmp_path):
    """firm_deactivate_async が per-firm flag を正しく解除する。

    H-3: per-firm flag のみ削除し、global flag は変更しないことを確認。
    """
    from common_v3.executor.async_impl import firm_deactivate_async

    fks = FirmScopedKillSwitch(firm="tradeify")
    fks.activate(reason="setup", activator="test")
    assert fks.is_active() is True
    assert is_active() is True

    result = _arun(firm_deactivate_async(fks, activator="redteam"))
    assert result is True, "firm_deactivate_async が True を返さなかった"
    assert fks.is_active() is False, "per-firm flag が解除されていない"
    assert is_active() is True, (
        "firm_deactivate_async が global flag を誤って解除した"
    )


def test_activate_async_works(tmp_path):
    """activate_async が _activate_raw 経由でグローバル flag を正しく立てる。

    CRITICAL-1 fix: activate_async は _activate_raw を直接 to_thread に渡す。
    """
    from common_v3.executor.async_impl import activate_async

    assert is_active() is False

    result = _arun(activate_async(
        reason="activate_async_e2e",
        activator="redteam",
        scope={"test": True},
    ))
    assert result is True, "activate_async が True を返さなかった"
    assert is_active() is True, "activate_async 後に global is_active() が False"
    state = get_state()
    assert state is not None
    assert state["reason"] == "activate_async_e2e"
    assert state.get("scope") == {"test": True}


def test_unwrap_assert_non_sync_only_raises(tmp_path):
    """_unwrap は @sync_only 未適用の関数に対して AssertionError を送出する。

    H-1 fix: getattr(fn, '__sync_only__', False) assertion が機能することを確認。
    """
    from common_v3.executor.async_impl import _unwrap

    def plain_function():
        pass

    with pytest.raises(AssertionError, match="is not decorated with @sync_only"):
        _unwrap(plain_function)
