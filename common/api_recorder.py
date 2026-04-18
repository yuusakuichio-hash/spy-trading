"""common/api_recorder.py — FutuOpenD APIコール記録・再生 wrapper

Record-Replay 設計思想:
  航空業界の Flight Data Recorder (FDR) と同様に、
  本番/ペーパー環境でのすべてのAPIコールを時系列で記録する。
  記録を replay_runner.py で再生することで、コード修正の影響を
  実データで事前検証できる（dry_test限界バグを本番前に発見）。

動作モード:
  RECORD — 実APIコールをwrapし、引数/戻り値をJSONLに記録
  REPLAY — 記録済みJSONLを読み込み、実APIコールなしで戻り値を再現
  PASSTHROUGH — 記録も再生もしない（デフォルト・通常稼働）

使い方:
    from common.api_recorder import APIRecorder, get_recorder
    recorder = get_recorder()               # グローバルシングルトン
    recorder.start_record(session_id)       # 記録開始
    recorder.start_replay("recorded/...")   # 再生開始
    recorder.stop()                         # PASSスルーに戻す

    # APIコールをラップして記録/再生
    result = recorder.call("get_market_snapshot", real_fn, codes)

記録ファイル形式 (JSONL, 1行=1コール):
    {"ts": "...", "method": "...", "args_hash": "...", "args_repr": "...", "ret": ..., "elapsed_ms": ...}

H-3: args_hash を追加。REPLAY時に method 名 + args_hash が不一致の場合は raise
H-4: enum/非シリアライズ型を {"__type__": "enum", "qualname": "...", "value": ...} で記録し型を保全
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# デフォルト記録ディレクトリ
_DEFAULT_RECORD_DIR = Path(__file__).resolve().parents[1] / "tests" / "recorded"


class RecorderMode(str, Enum):
    PASSTHROUGH = "passthrough"
    RECORD = "record"
    REPLAY = "replay"


class ReplayExhaustedError(Exception):
    """再生モードで記録が尽きた場合に送出"""
    pass


class ReplayMethodMismatchError(Exception):
    """再生モードでメソッド名またはargs hashが一致しない場合に送出 (H-3)"""
    pass


class APIRecorder:
    """FutuOpenD APIコールを記録・再生するラッパー。

    スレッドセーフ（RLock使用）。
    シングルトンインスタンスは get_recorder() で取得する。
    """

    def __init__(self, record_dir: Path = _DEFAULT_RECORD_DIR):
        self.record_dir = Path(record_dir)
        self._mode: RecorderMode = RecorderMode.PASSTHROUGH
        self._session_id: Optional[str] = None
        self._record_path: Optional[Path] = None
        self._record_file = None
        self._replay_queue: list[dict] = []
        self._replay_index: int = 0
        self._call_count: int = 0
        self._lock = threading.RLock()

    # ── モード切替 ────────────────────────────────────────────────

    def start_record(self, session_id: Optional[str] = None) -> Path:
        """記録モードを開始する。

        Args:
            session_id: ファイル名に付与するID（Noneの場合は日時を自動生成）

        Returns:
            記録ファイルのPath
        """
        with self._lock:
            if session_id is None:
                session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._session_id = session_id
            self.record_dir.mkdir(parents=True, exist_ok=True)
            self._record_path = self.record_dir / f"session_{session_id}.jsonl"
            self._record_file = open(self._record_path, "a", encoding="utf-8")
            self._mode = RecorderMode.RECORD
            self._call_count = 0
            log.info(f"[APIRecorder] RECORD開始: {self._record_path}")
            return self._record_path

    def start_replay(self, record_path: str | Path) -> int:
        """再生モードを開始する。

        Args:
            record_path: 再生するJSONLファイルのパス

        Returns:
            読み込んだエントリー数
        """
        with self._lock:
            path = Path(record_path)
            if not path.exists():
                raise FileNotFoundError(f"記録ファイルが見つかりません: {path}")
            entries = []
            with open(path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        log.warning(f"[APIRecorder] L{lineno} parse error: {e}")
            self._replay_queue = entries
            self._replay_index = 0
            self._mode = RecorderMode.REPLAY
            self._call_count = 0
            log.info(f"[APIRecorder] REPLAY開始: {path} ({len(entries)} エントリー)")
            return len(entries)

    def stop(self) -> dict:
        """PASSスルーモードに戻す。記録ファイルを閉じる。

        Returns:
            セッション統計 dict
        """
        with self._lock:
            stats = {
                "mode": self._mode.value,
                "session_id": self._session_id,
                "call_count": self._call_count,
                "record_path": str(self._record_path) if self._record_path else None,
            }
            if self._record_file:
                try:
                    self._record_file.close()
                except Exception:
                    pass
                self._record_file = None
            self._mode = RecorderMode.PASSTHROUGH
            self._replay_queue = []
            self._replay_index = 0
            log.info(f"[APIRecorder] 停止: {stats}")
            return stats

    # ── メインAPI ─────────────────────────────────────────────────

    def call(
        self,
        method: str,
        real_fn: Callable,
        *args,
        **kwargs,
    ) -> Any:
        """APIコールをモードに応じて実行・記録・再生する。

        PASSTHROUGH: real_fn(*args, **kwargs) をそのまま呼ぶ
        RECORD:      real_fn を呼んで結果をJSONLに書き込む
        REPLAY:      記録から次のエントリーを取り出して返す

        Args:
            method: メソッド名（記録・照合に使用）
            real_fn: 実際のAPIコール関数
            *args, **kwargs: real_fn への引数

        Returns:
            APIコールの戻り値（RECORDはreal_fn結果、REPLAYは記録値）

        Raises:
            ReplayExhaustedError: REPLAYで記録が尽きた場合
            ReplayMethodMismatchError: REPLAYでメソッド名またはargsが不一致の場合 (H-3)
        """
        with self._lock:
            mode = self._mode

        if mode == RecorderMode.PASSTHROUGH:
            return real_fn(*args, **kwargs)

        elif mode == RecorderMode.RECORD:
            return self._record_call(method, real_fn, *args, **kwargs)

        elif mode == RecorderMode.REPLAY:
            return self._replay_call(method, args, kwargs)

        return real_fn(*args, **kwargs)

    def _record_call(self, method: str, real_fn: Callable, *args, **kwargs) -> Any:
        """実APIコールして結果を記録する"""
        args_hash = _compute_args_hash(args, kwargs)
        t0 = time.monotonic()
        try:
            result = real_fn(*args, **kwargs)
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            entry = {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "method": method,
                "args_hash": args_hash,
                "args_repr": _safe_repr(args, kwargs),
                "ret": None,
                "exception": type(e).__name__,
                "exception_msg": str(e),
                "elapsed_ms": round(elapsed_ms, 2),
            }
            self._write_entry(entry)
            raise

        elapsed_ms = (time.monotonic() - t0) * 1000
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "method": method,
            "args_hash": args_hash,
            "args_repr": _safe_repr(args, kwargs),
            "ret": _make_serializable(result),
            "elapsed_ms": round(elapsed_ms, 2),
        }
        self._write_entry(entry)
        return result

    def _replay_call(self, method: str, args: tuple, kwargs: dict) -> Any:
        """記録からエントリーを取り出して再生する (H-3: args_hash 照合)"""
        with self._lock:
            if self._replay_index >= len(self._replay_queue):
                raise ReplayExhaustedError(
                    f"[APIRecorder] REPLAY終了: {method} のエントリーが尽きました "
                    f"(index={self._replay_index}, total={len(self._replay_queue)})"
                )
            entry = self._replay_queue[self._replay_index]
            self._replay_index += 1
            self._call_count += 1

        # H-3: method名 + args_hash の両方を照合し、不一致は raise
        recorded_method = entry.get("method", "")
        recorded_hash   = entry.get("args_hash", "")
        current_hash    = _compute_args_hash(args, kwargs)

        if recorded_method != method:
            raise ReplayMethodMismatchError(
                f"[APIRecorder] REPLAY method mismatch: "
                f"expected={method} recorded={recorded_method} "
                f"(index={self._replay_index - 1})"
            )
        if recorded_hash and recorded_hash != current_hash:
            raise ReplayMethodMismatchError(
                f"[APIRecorder] REPLAY args hash mismatch for method={method}: "
                f"expected_hash={current_hash[:8]} recorded_hash={recorded_hash[:8]} "
                f"(index={self._replay_index - 1})"
            )

        # 例外が記録されていた場合は再送出
        if "exception" in entry:
            exc_cls = _lookup_exception(entry["exception"])
            raise exc_cls(entry.get("exception_msg", "replayed exception"))

        ret = entry.get("ret")
        log.debug(f"[APIRecorder] REPLAY {method} → {type(ret).__name__}")
        return ret

    def _write_entry(self, entry: dict) -> None:
        with self._lock:
            self._call_count += 1
            if self._record_file:
                try:
                    self._record_file.write(json.dumps(entry, ensure_ascii=False, default=str))
                    self._record_file.write("\n")
                    self._record_file.flush()
                except Exception as e:
                    log.error(f"[APIRecorder] 書き込みエラー: {e}")

    # ── 状態確認 ──────────────────────────────────────────────────

    @property
    def mode(self) -> RecorderMode:
        with self._lock:
            return self._mode

    @property
    def is_recording(self) -> bool:
        return self.mode == RecorderMode.RECORD

    @property
    def is_replaying(self) -> bool:
        return self.mode == RecorderMode.REPLAY

    def status(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode.value,
                "session_id": self._session_id,
                "call_count": self._call_count,
                "replay_index": self._replay_index,
                "replay_total": len(self._replay_queue),
                "record_path": str(self._record_path) if self._record_path else None,
            }

    def get_recorded_entries(self) -> list[dict]:
        """記録済みエントリーをすべて返す（テスト用）"""
        if self._record_path and self._record_path.exists():
            entries = []
            with open(self._record_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            return entries
        return []


# ── グローバルシングルトン ────────────────────────────────────────

_global_recorder: Optional[APIRecorder] = None
_recorder_lock = threading.Lock()


def get_recorder() -> APIRecorder:
    """グローバルAPIRecorderシングルトンを返す"""
    global _global_recorder
    if _global_recorder is None:
        with _recorder_lock:
            if _global_recorder is None:
                _global_recorder = APIRecorder()
    return _global_recorder


def set_recorder(recorder: APIRecorder) -> None:
    """テスト用: グローバルRecorderを差し替える"""
    global _global_recorder
    with _recorder_lock:
        _global_recorder = recorder


# ── ユーティリティ ────────────────────────────────────────────────

def _compute_args_hash(args: tuple, kwargs: dict, max_repr_len: int = 1000) -> str:
    """args/kwargs の内容から SHA-256 ハッシュを計算する (H-3)。
    repr() が安定しない型は str() で代替。"""
    try:
        s = repr(args) + repr(sorted(kwargs.items()))
        if len(s) > max_repr_len:
            s = s[:max_repr_len]
        return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]
    except Exception:
        return ""


def _safe_repr(args: tuple, kwargs: dict, max_len: int = 200) -> str:
    """引数を安全に文字列化する（サイズ制限あり）"""
    try:
        s = f"args={args!r} kwargs={kwargs!r}"
        if len(s) > max_len:
            s = s[:max_len] + "...(truncated)"
        return s
    except Exception:
        return "(repr failed)"


def _make_serializable(obj: Any) -> Any:
    """JSONシリアライズ可能な型に変換する。

    H-4: enum/非シリアライズ型は {"__type__": "enum", "qualname": "...", "value": ...} 形式で
    記録して型情報を保全する。str(obj) への fallback は型情報が失われるため使わない。
    """
    try:
        import pandas as pd
        if isinstance(obj, pd.DataFrame):
            return {"__type__": "DataFrame", "records": obj.to_dict(orient="records")}
        if isinstance(obj, pd.Series):
            return {"__type__": "Series", "data": obj.to_dict()}
    except ImportError:
        pass

    if isinstance(obj, tuple):
        return {"__type__": "tuple", "items": [_make_serializable(v) for v in obj]}
    if isinstance(obj, list):
        return [_make_serializable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}

    # JSON基本型はそのまま
    if isinstance(obj, (int, float, bool, str, type(None))):
        return obj

    # H-4: Enum は型・qualified name・値を保全して記録
    if isinstance(obj, Enum):
        return {
            "__type__": "enum",
            "qualname": f"{type(obj).__module__}.{type(obj).__qualname__}",
            "name": obj.name,
            "value": obj.value,
        }

    # datetime 型は ISO 文字列で保存（型タグ付き）
    if isinstance(obj, datetime.datetime):
        return {"__type__": "datetime", "iso": obj.isoformat()}
    if isinstance(obj, datetime.date):
        return {"__type__": "date", "iso": obj.isoformat()}

    # bytes は base64 で保存
    if isinstance(obj, bytes):
        import base64
        return {"__type__": "bytes", "b64": base64.b64encode(obj).decode()}

    # H-4: str fallback は避け、型情報を残す
    return {"__type__": "unknown", "repr": repr(obj)[:200]}


def _lookup_exception(name: str) -> type:
    """例外クラス名からクラスを逆引きする"""
    builtins_map = {
        "ValueError": ValueError,
        "RuntimeError": RuntimeError,
        "ConnectionError": ConnectionError,
        "TimeoutError": TimeoutError,
        "OSError": OSError,
        "IOError": IOError,
        "Exception": Exception,
    }
    return builtins_map.get(name, RuntimeError)
