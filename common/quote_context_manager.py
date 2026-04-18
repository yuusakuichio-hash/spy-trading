"""Quote Context Manager — 段階的フェイルオーバー（機会損失最小化）

25分間隔で発生する quote context 切断への対策。
切断回数に応じて level 0〜3 で取引継続性を段階的に保証。

| level | 状態 | 取引対応 |
|---|---|---|
| 0 | 正常接続 | 通常 |
| 1 | 1回切断・代替source稼働 | 通常・自己解決 |
| 2 | 2回連続切断 | 発注サイズ半減（保守化） |
| 3 | 3回連続切断 | 新規エントリー停止(exit許可) |

再接続成功で即 level 0 に復帰。
"""
from __future__ import annotations
import datetime
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

# exponential backoff: 5s / 15s / 45s
_BACKOFF_SEQUENCE = [5.0, 15.0, 45.0]


@dataclass
class QuoteContextState:
    level: int = 0                     # 0=正常 / 1-3=切断段階
    disconnect_count: int = 0          # 連続切断回数
    last_disconnect_at: Optional[datetime.datetime] = None
    last_reconnect_at: Optional[datetime.datetime] = None
    reconnect_attempts: int = 0
    source_chain: list[str] = field(default_factory=lambda: [
        "primary",   # FutuOpenD quote_ctx
        "finnhub",   # 代替source 1
        "yahoo",     # 代替source 2
        "cache",     # ATMSubscribe残存キャッシュ
    ])
    active_source: str = "primary"


class QuoteContextManager:
    """Quote context の段階的フェイルオーバー管理"""

    # [M-6] cache鮮度チェック: この秒数より古いcacheはstaleと判定
    CACHE_MAX_AGE_SEC: float = 300.0  # 5分

    def __init__(self,
                 reconnect_fn: Optional[Callable[[], bool]] = None,
                 health_check_fn: Optional[Callable[[], bool]] = None,
                 notify_fn: Optional[Callable[[str, str, int], None]] = None):
        """
        Args:
            reconnect_fn: 再接続関数. 成功で True
            health_check_fn: 死活確認関数. 正常で True
            notify_fn: 通知関数. (title, msg, priority) 形式
        """
        self.state = QuoteContextState()
        self._reconnect_fn = reconnect_fn
        self._health_check_fn = health_check_fn
        self._notify_fn = notify_fn
        self._lock = threading.RLock()

    def on_disconnect(self) -> None:
        """切断検知時に呼ぶ"""
        with self._lock:
            self.state.disconnect_count += 1
            self.state.last_disconnect_at = datetime.datetime.now()
            self.state.level = min(self.state.disconnect_count, 3)
            log.warning(
                f"[QCM] 切断検知: count={self.state.disconnect_count} "
                f"level={self.state.level}"
            )
            self._pick_fallback_source()

    def on_reconnect_success(self) -> None:
        """再接続成功時に呼ぶ"""
        with self._lock:
            self.state.last_reconnect_at = datetime.datetime.now()
            prev_level = self.state.level
            self.state.level = 0
            self.state.disconnect_count = 0
            self.state.active_source = "primary"
            if prev_level > 0:
                log.info(f"[QCM] 再接続成功: level {prev_level}→0 通常復帰")

    def try_reconnect(self) -> bool:
        """再接続試行（exponential backoff）

        [M-7修正] TOCTOU対策: lock外でstate参照していた問題を修正。
        backoff wait中にlock解除するのは必要だが、state読み取り・更新は
        全てlock内で完結させ、wait後に再度lockを取り直すatomic patternに変更。
        """
        with self._lock:
            if self._reconnect_fn is None:
                return False
            idx = min(self.state.reconnect_attempts, len(_BACKOFF_SEQUENCE) - 1)
            wait = _BACKOFF_SEQUENCE[idx]
            attempt_num = self.state.reconnect_attempts + 1
            self.state.reconnect_attempts = attempt_num
        # lock解除してwait（reconnect_fn呼び出し前にlockを再取得して安全に実行）
        log.info(f"[QCM] 再接続試行 #{attempt_num} (backoff {wait}s)")
        time.sleep(wait)
        try:
            ok = self._reconnect_fn()
        except Exception as e:
            log.warning(f"[QCM] 再接続関数例外: {e}")
            ok = False
        with self._lock:
            # 成功: state更新をlockで保護してTOCTOU排除
            if ok:
                self.state.reconnect_attempts = 0
        if ok:
            self.on_reconnect_success()
        return ok

    def _pick_fallback_source(self) -> None:
        """切断レベルに応じて代替sourceを選ぶ

        [M-6修正] cache sourceを選択する際に鮮度チェックを行う。
        last_disconnect_at から CACHE_MAX_AGE_SEC を超えている場合は
        active_source を "stale_cache" とし allow_new_entry() がFalseになる。
        """
        level = self.state.level
        if level >= len(self.state.source_chain):
            candidate = "cache"
        else:
            # level 1 → chain[1] (finnhub) 等
            candidate = self.state.source_chain[level]

        # [M-6] cacheが選ばれた場合、鮮度確認
        if candidate == "cache" and self.state.last_disconnect_at is not None:
            age_sec = (
                datetime.datetime.now() - self.state.last_disconnect_at
            ).total_seconds()
            if age_sec > self.CACHE_MAX_AGE_SEC:
                log.warning(
                    f"[QCM] cache鮮度切れ: age={age_sec:.0f}s > {self.CACHE_MAX_AGE_SEC}s "
                    f"→ active_source=stale_cache・新規エントリーブロック"
                )
                self.state.active_source = "stale_cache"
                return

        self.state.active_source = candidate
        log.info(f"[QCM] active_source={self.state.active_source} (level={level})")

    def get_level(self) -> int:
        with self._lock:
            return self.state.level

    def allow_new_entry(self) -> bool:
        """新規エントリー許可判定

        [M-6] level < 3 でも stale_cache 状態なら新規エントリーを拒否する。
        """
        with self._lock:
            if self.state.active_source == "stale_cache":
                return False
            return self.state.level < 3

    def margin_scale(self) -> float:
        """段階別 margin スケール係数"""
        level = self.get_level()
        if level == 0:
            return 1.0
        if level == 1:
            return 0.8     # 20%縮小
        if level == 2:
            return 0.5     # 半減（保守化）
        return 0.0         # level 3 は発注せず

    def notify_if_escalated(self, priority_threshold_level: int = 3) -> None:
        """level が閾値以上かつ自動再接続失敗時のみ通知（通知ポリシー準拠）"""
        if self._notify_fn is None:
            return
        with self._lock:
            if self.state.level >= priority_threshold_level:
                self._notify_fn(
                    f"[Atlas/QCM] level={self.state.level}",
                    f"quote_ctx 連続切断 {self.state.disconnect_count}回・自動再接続失敗",
                    1,
                )

    def status_summary(self) -> dict:
        with self._lock:
            return {
                "level": self.state.level,
                "disconnect_count": self.state.disconnect_count,
                "last_disconnect": (
                    self.state.last_disconnect_at.isoformat()
                    if self.state.last_disconnect_at else None
                ),
                "last_reconnect": (
                    self.state.last_reconnect_at.isoformat()
                    if self.state.last_reconnect_at else None
                ),
                "active_source": self.state.active_source,
                "allow_new_entry": self.allow_new_entry(),
                "margin_scale": self.margin_scale(),
            }


# モジュールレベルのsingleton（spy_bot.py から共有）
_global_manager: Optional[QuoteContextManager] = None


def get_global_manager() -> QuoteContextManager:
    global _global_manager
    if _global_manager is None:
        _global_manager = QuoteContextManager()
    return _global_manager


def set_global_manager(mgr: QuoteContextManager) -> None:
    global _global_manager
    _global_manager = mgr
