"""common_v3/risk/kill_switch.py — KillSwitch v3 (Sprint 0.5 Day 3)

既存 common/kill_switch.py の冪等性欠陥修正版。

修正内容 (Redteam C-02 対応):
- activate() 戻り値を bool に変更 (True=新規発動 / False=既 ARMED 冪等スキップ)
  → Pushover 二重送信防止
- deactivate() は FLAG_FILE 不在時 early return (audit log 追記しない)
- file lock (fcntl.flock) で concurrent write 安全化
- FirmScopedKillSwitch: per-firm flag + 発動時 global activate() 連動
- audit log: data/state_v3/kill_switch_audit.jsonl (append-only)

凍結 Interface (spec B9 L263-L278):
    activate(reason, activator, scope) -> bool
    deactivate(activator, reason) -> bool
    is_active() -> bool
    get_state() -> dict | None
    class FirmScopedKillSwitch(firm) -> .activate / .deactivate / .is_active
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
from pathlib import Path
from typing import Literal

from common_v3.executor.sync_guard import sync_only

# ── パス定義 ─────────────────────────────────────────────────────────────────
# 2026-04-24 22:58 JST 事故 (pytest が本番 kill_switch.flag を汚染し
# atlas-paper daemon が誤って KillSwitch 発動) の再発防止として
# TRADING_STATE_DIR env var で state dir を override 可能にする。
# 本番では env 未設定 = 既存 data/state_v3/ を使用 (後方互換)。
# pytest では conftest.py autouse fixture で tmp_path に差し替える。
_BASE = Path(__file__).resolve().parents[2]
_DEFAULT_STATE_DIR = _BASE / "data" / "state_v3"
_STATE_DIR = Path(os.getenv("TRADING_STATE_DIR", str(_DEFAULT_STATE_DIR)))
FLAG_FILE = _STATE_DIR / "kill_switch.flag"
AUDIT_FILE = _STATE_DIR / "kill_switch_audit.jsonl"

_VALID_FIRMS: tuple[str, ...] = ("mffu", "tradeify", "apex", "bulenox")


# ── 内部ユーティリティ ────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    """state_v3 ディレクトリを作成する。"""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _write_audit(
    event: str,
    reason: str = "",
    activator: str = "unknown",
    extra: dict | None = None,
) -> None:
    """audit JSONL に append-only で追記する。

    fcntl.flock で排他ロックを取得してから書き込む。
    ファイルを開く前にディレクトリを作成する。
    """
    _ensure_dirs()
    entry: dict = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": event,
        "reason": reason,
        "activator": activator,
        "pid": os.getpid(),
    }
    if extra:
        entry.update(extra)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_flag(path: Path) -> dict | None:
    """フラグファイルを JSON として読み込む。存在しなければ None。

    C-2 fix: except を FileNotFoundError と (json.JSONDecodeError, OSError) に絞る。
    破損 flag (JSON 壊れ / 読取 OS error) は None ではなく fail-safe dict を返す。
    呼出側の `get_state() is not None` guard で bypass されるのを防ぐ。
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return json.loads(text)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        import logging
        import time as _time
        logging.getLogger(__name__).error("flag file corrupted: %s", e)
        return {
            "reason": "CORRUPTED",
            "activator": "unknown",
            "ts": _time.time(),
            "corrupted": True,
        }
    return None


def _write_flag(path: Path, data: dict) -> None:
    """フラグファイルをアトミックに書き込む。

    一時ファイル (同じディレクトリ内) を使い os.replace() で置き換えることで
    読み取りと競合しても壊れた中間状態が見えない。
    tmp ファイルは path と同じディレクトリに作成する (os.replace は同一 fs が必要)。
    """
    _ensure_dirs()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{path.name}.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ── グローバル KillSwitch ─────────────────────────────────────────────────────

_LOCK_FILE_NAME = ".kill_switch.lock"


def _get_lock_path() -> Path:
    """ロックファイルパスを返す (FLAG_FILE と同じディレクトリ)。"""
    return FLAG_FILE.parent / _LOCK_FILE_NAME


# ── 内部 raw 実装（guard なし・executor スレッドから安全に呼べる） ────────────────

def _activate_raw(
    reason: str = "manual",
    activator: str = "unknown",
    scope: dict | None = None,
) -> bool:
    """activate() の guard なし内部実装。

    async_impl 経由の executor スレッド、および
    FirmScopedKillSwitch.activate() 内部から呼ぶ唯一の合法経路。
    直接呼出禁止 — 公開 API は activate() を使うこと。

    Returns:
        True  — 新規発動
        False — 既に ARMED (冪等スキップ)
    """
    _ensure_dirs()
    lock_path = _get_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            if FLAG_FILE.exists():
                return False

            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            flag_data: dict = {
                "activated_at": ts,
                "reason": reason,
                "activator": activator,
                "pid": os.getpid(),
            }
            if scope is not None:
                flag_data["scope"] = scope

            _write_flag(FLAG_FILE, flag_data)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)

    _write_audit(
        event="activate",
        reason=reason,
        activator=activator,
        extra={"scope": scope} if scope else None,
    )
    return True


def _deactivate_raw(activator: str = "unknown", reason: str = "") -> bool:
    """deactivate() の guard なし内部実装。

    async_impl 経由の executor スレッドから呼ぶ唯一の合法経路。
    直接呼出禁止 — 公開 API は deactivate() を使うこと。

    Returns:
        True  — 正常解除
        False — FLAG_FILE 不在 (early return)
    """
    if not FLAG_FILE.exists():
        return False

    FLAG_FILE.unlink()
    _write_audit(
        event="deactivate",
        reason=reason or "manual_deactivate",
        activator=activator,
    )
    return True


def _is_active_raw() -> bool:
    """is_active() の guard なし内部実装。

    async_impl 経由の executor スレッドから呼ぶ唯一の合法経路。
    直接呼出禁止 — 公開 API は is_active() を使うこと。
    """
    return FLAG_FILE.exists()


# ── 公開 API（@sync_only ラッパー） ──────────────────────────────────────────────

@sync_only
def activate(
    reason: str = "manual",
    activator: str = "unknown",
    scope: dict | None = None,
) -> bool:
    """Kill Switch を発動する。

    fcntl.flock による排他ロックで check-then-write をアトミック化する。

    Returns:
        True  — 新規発動 (FLAG_FILE を新たに作成した)
        False — 既に ARMED 状態 (冪等スキップ・audit log 追記しない)
    """
    return _activate_raw(reason=reason, activator=activator, scope=scope)


@sync_only
def deactivate(activator: str = "unknown", reason: str = "") -> bool:
    """Kill Switch を解除する。

    FLAG_FILE が不在の場合は early return (audit log 追記しない)。

    Returns:
        True  — 正常解除
        False — FLAG_FILE 不在 (early return)
    """
    return _deactivate_raw(activator=activator, reason=reason)


@sync_only
def is_active() -> bool:
    """現在 Kill Switch が発動中かを返す (キャッシュなし・毎回ファイル確認)。"""
    return _is_active_raw()


@sync_only
def get_state() -> dict | None:
    """発動中の状態を dict で返す。未発動なら None。"""
    return _read_flag(FLAG_FILE)


# ── FirmScopedKillSwitch ──────────────────────────────────────────────────────

class FirmScopedKillSwitch:
    """プロップファーム別スコープ付き Kill Switch。

    発動時は per-firm flag ファイルに加え、グローバル activate() も連動する。
    これにより全体 is_active() でもブロック状態が検知される。

    Spec B9 凍結 Interface:
        __init__(firm: Literal["mffu", "tradeify", "apex", "bulenox"])
        activate(reason, activator) -> bool
        deactivate(activator) -> bool
        is_active() -> bool
    """

    def __init__(
        self,
        firm: Literal["mffu", "tradeify", "apex", "bulenox"],
    ) -> None:
        if firm not in _VALID_FIRMS:
            raise ValueError(
                f"firm must be one of {_VALID_FIRMS}, got {firm!r}"
            )
        self.firm = firm
        self._flag_path = _STATE_DIR / f"kill_switch_{firm}.flag"

    # ── activate ────────────────────────────────────────────────────────────

    @sync_only
    def activate(self, reason: str, activator: str = "unknown") -> bool:
        """Per-firm Kill Switch を発動する。

        C-1 fix: global activate() を先に呼ぶ → その後 per-firm flag を書く。
        SIGKILL が per-firm flag 書込前に割り込んでも global flag は既に立っているため
        is_active() は True (安全側) を返し、per-firm flag だけ残って global なしの
        不整合状態を物理的に作れない。

        fcntl.flock による排他ロックで check-then-write をアトミック化する。

        Returns:
            True  — 新規発動 (per-firm flag を新たに作成した)
            False — 既に ARMED (冪等スキップ)
        """
        _ensure_dirs()
        lock_path = _STATE_DIR / f".kill_switch_{self.firm}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                # per-firm 冪等チェック (ロック内)
                if self._flag_path.exists():
                    return False

                # H-2 fix: global activate 失敗時は per-firm flag を作らず abort。
                # _activate_raw は guard なしで executor スレッドからも安全に呼べる。
                try:
                    _activate_raw(
                        reason=f"firm_trigger:{self.firm}:{reason}",
                        activator=activator,
                        scope={"firm": self.firm},
                    )
                except Exception:
                    # global activate 失敗 → per-firm flag は作らない
                    # flock は finally で自動解放される
                    raise

                ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                firm_flag_data: dict = {
                    "activated_at": ts,
                    "firm": self.firm,
                    "reason": reason,
                    "activator": activator,
                    "pid": os.getpid(),
                }
                _write_flag(self._flag_path, firm_flag_data)
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

        _write_audit(
            event=f"firm_activate_{self.firm}",
            reason=reason,
            activator=activator,
        )
        return True

    # ── deactivate ──────────────────────────────────────────────────────────

    @sync_only
    def deactivate(self, activator: str = "unknown") -> bool:
        """Per-firm Kill Switch を解除する。

        per-firm flag のみ削除する。グローバル flag は別途 deactivate() で解除する。
        (他の firm が still active な可能性があるため自動連動解除はしない)

        Returns:
            True  — 正常解除
            False — per-firm FLAG_FILE 不在 (early return)
        """
        if not self._flag_path.exists():
            return False

        self._flag_path.unlink()
        _write_audit(
            event=f"firm_deactivate_{self.firm}",
            reason="manual_deactivate",
            activator=activator,
        )
        return True

    # ── is_active ────────────────────────────────────────────────────────────

    @sync_only
    def is_active(self) -> bool:
        """Per-firm Kill Switch が発動中かを返す (キャッシュなし・毎回ファイル確認)。"""
        return self._flag_path.exists()

    # ── deactivate_all (CRIT-R6-2 fix) ───────────────────────────────────────

    @classmethod
    def list_all_firm_flags(cls) -> list[tuple[str, Path]]:
        """CRIT-R6-2 fix: 現在 ARMED 状態の全 firm flag を (firm_name, flag_path) のリストで返す。

        _probe_recovery が probe 成功時に全 firm flag を解除するために使用する。
        per-firm flag ファイルが data/state_v3/kill_switch_<firm>.flag に存在する firm のみ返す。

        Returns:
            list of (firm_name, flag_path) tuples for all currently ARMED firms
        """
        result = []
        for firm in _VALID_FIRMS:
            flag_path = _STATE_DIR / f"kill_switch_{firm}.flag"
            if flag_path.exists():
                result.append((firm, flag_path))
        return result

    @classmethod
    def deactivate_all(cls, activator: str = "probe_recovery") -> dict[str, bool]:
        """CRIT-R6-2 fix: 全 firm の per-firm Kill Switch flag を一括解除する。

        _probe_recovery が probe 成功時に呼ぶことで、per-firm flag が ARMED のまま
        残る「ゾンビ状態」を解消する。

        global Kill Switch の deactivate() とは独立して呼ぶこと。
        呼出側は deactivate_all() の後に global deactivate() も呼ぶこと。

        Returns:
            dict: {firm_name: deactivated_bool} — True=解除成功 / False=flag 不在(skip)
        """
        import logging
        _log = logging.getLogger(__name__)
        results: dict[str, bool] = {}
        armed_firms = cls.list_all_firm_flags()
        if not armed_firms:
            _log.debug("[KillSwitch] deactivate_all: no per-firm flags found (all clear).")
            return results
        for firm, flag_path in armed_firms:
            try:
                if flag_path.exists():
                    flag_path.unlink()
                    _write_audit(
                        event=f"firm_deactivate_{firm}_by_deactivate_all",
                        reason="probe_recovery_deactivate_all_c6r2_fix",
                        activator=activator,
                    )
                    results[firm] = True
                    _log.warning(
                        "[KillSwitch] deactivate_all: per-firm flag DEACTIVATED for firm=%s (CRIT-R6-2 fix)",
                        firm,
                    )
                else:
                    results[firm] = False
            except Exception as e:
                _log.error(
                    "[KillSwitch] deactivate_all: failed to deactivate firm=%s: %s",
                    firm, e,
                )
                results[firm] = False
        return results
