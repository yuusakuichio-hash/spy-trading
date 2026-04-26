"""atlas_v3/ops/log_rotator.py — HIGH-R6-5: ログローテーション

HIGH-R6-5 fix: log rotation なし・GB 肥大化防止。
旧実装: monitor.py が _rotate_log() を内部で持つが、他ログファイルには未適用。
新実装: LogRotator クラスで全ログファイルを一元管理。

設計:
- size-based rotation: ファイルサイズが max_bytes を超えたらローテーション
- max_backups 世代まで保持（.1, .2, ... .N）
- atlas-paper-stdout.log / atlas-paper-stderr.log / monitor_state.jsonl 等を対象
- 定期実行: rotate_all() を daemon loop から定期的に呼ぶ

使用方法:
    from atlas_v3.ops.log_rotator import LogRotator
    rotator = LogRotator()
    rotator.rotate_all()  # 全対象ログを確認・必要に応じてローテーション

デフォルト設定:
    max_bytes: 10MB per file
    max_backups: 10 generations
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parents[2]
_STATE_DIR = _BASE / "data" / "state_v3"

# デフォルトのログローテーション設定
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10MB
_DEFAULT_MAX_BACKUPS = 10               # 10 世代

# ローテーション対象ログファイル（サービス起動時に動的に追加も可）
_DEFAULT_LOG_FILES = [
    _STATE_DIR / "atlas-paper-stdout.log",
    _STATE_DIR / "atlas-paper-stderr.log",
    _STATE_DIR / "monitor_state.jsonl",
    _STATE_DIR / "kill_switch_audit.jsonl",
]


class LogRotator:
    """サイズベースのログローテーター。

    使用方法:
        rotator = LogRotator()
        rotator.add_log_file(Path("/path/to/my.log"))
        rotator.rotate_all()

    デフォルトで atlas-paper stdout/stderr と monitor_state.jsonl を管理。
    """

    def __init__(
        self,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_backups: int = _DEFAULT_MAX_BACKUPS,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be > 0, got {max_bytes}")
        if max_backups < 1:
            raise ValueError(f"max_backups must be >= 1, got {max_backups}")
        self._max_bytes = max_bytes
        self._max_backups = max_backups
        self._log_files: list[Path] = list(_DEFAULT_LOG_FILES)

    def add_log_file(self, path: Path) -> None:
        """ローテーション対象ログファイルを追加する。"""
        if path not in self._log_files:
            self._log_files.append(path)

    def rotate_all(self) -> dict[str, bool]:
        """全対象ログファイルを確認し、必要に応じてローテーションする。

        Returns:
            dict: {file_name: rotated_bool}
        """
        results: dict[str, bool] = {}
        for log_path in self._log_files:
            rotated = self.rotate_if_needed(log_path)
            results[log_path.name] = rotated
        return results

    def rotate_if_needed(self, log_path: Path) -> bool:
        """指定ファイルがサイズ上限を超えていればローテーションする。

        Args:
            log_path: ローテーション対象ファイルパス

        Returns:
            True: ローテーションを実行した
            False: ローテーション不要（サイズ未超過 or ファイル不在）
        """
        if not log_path.exists():
            return False
        try:
            file_size = log_path.stat().st_size
        except OSError as e:
            log.warning("[LogRotator] stat() failed for %s: %s", log_path.name, e)
            return False

        if file_size < self._max_bytes:
            return False

        return self._do_rotate(log_path)

    def _do_rotate(self, log_path: Path) -> bool:
        """ローテーションを実行する。

        .N → .N+1 の順にシフト。max_backups を超えた古いファイルは削除。
        最後に現在のファイルを .1 にリネーム。

        Args:
            log_path: ローテーション対象ファイルパス

        Returns:
            True: 成功 / False: 失敗
        """
        try:
            # 古いバックアップを後ろからシフト
            for i in range(self._max_backups - 1, 0, -1):
                old = Path(f"{log_path}.{i}")
                new = Path(f"{log_path}.{i + 1}")
                if old.exists():
                    try:
                        old.rename(new)
                    except OSError as e:
                        log.warning(
                            "[LogRotator] rename %s.%d → %s.%d failed: %s",
                            log_path.name, i, log_path.name, i + 1, e,
                        )

            # max_backups を超えた古い世代を削除
            oldest = Path(f"{log_path}.{self._max_backups}")
            if oldest.exists():
                try:
                    oldest.unlink()
                except OSError as e:
                    log.warning("[LogRotator] unlink oldest %s failed: %s", oldest.name, e)

            # 現在ログを .1 にリネーム
            backup_1 = Path(f"{log_path}.1")
            if log_path.exists():
                log_path.rename(backup_1)

            log.info(
                "[LogRotator] Rotated %s (size=%d bytes, max=%d bytes, backups=%d)",
                log_path.name,
                backup_1.stat().st_size if backup_1.exists() else 0,
                self._max_bytes,
                self._max_backups,
            )
            return True

        except Exception as e:
            log.error("[LogRotator] Rotation failed for %s: %s", log_path.name, e)
            return False

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def max_backups(self) -> int:
        return self._max_backups

    @property
    def log_files(self) -> list[Path]:
        return list(self._log_files)


# ---------------------------------------------------------------------------
# モジュールレベルのデフォルト rotator（シングルトン的な使い方）
# ---------------------------------------------------------------------------

_default_rotator: Optional[LogRotator] = None


def get_default_rotator() -> LogRotator:
    """デフォルト LogRotator を返す（シングルトン）。"""
    global _default_rotator
    if _default_rotator is None:
        _default_rotator = LogRotator()
    return _default_rotator


def rotate_all_default() -> dict[str, bool]:
    """デフォルト設定で全ログをローテーションする（convenience function）。"""
    return get_default_rotator().rotate_all()
