"""
common/prop_firm_cross_account.py — Layer PF-2: Cross-Account 相関ガード

設計書: data/prop_firm_countermeasures_design.md §2-2
実装指示: data/builder_instructions/chronos_prop_safety_20260420.md Phase B-5

目的:
  5 口座（Core/Rapid/Pro/Flex/Builder）+ Tradeify + Apex で同時 Bot 運用する際の
  相関検出を回避する。プロップファームは「Copy Trading」「同一戦略の複数口座同時発注」を
  アカウント失効の理由として使用する。

防護内容:
  1. 同 firm の前回発注から 3 秒以上経過していること（firm 単位レート制限）
  2. 他口座で同銘柄・同方向のポジションが active な場合は拒否
  3. 戦略パラメータ差別化（accounts.yaml の risk_reward / stop_ticks_offset 参照）

使用方法:
    guard = CrossAccountGuard(min_delay_sec=3)
    ok, reason = guard.check_before_order("mffu", "account_1", "MES", "BUY")
    if ok:
        guard.record_order("mffu", "account_1", "MES", "BUY")
    # ポジション決済後:
    guard.record_close("account_1", "MES", "BUY")
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
import threading
import time as _time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# P0-CRITICAL-3: SQLite共有ストアによるプロセス間対応
# data/cross_account_state.db で全プロセスが状態を共有する。
# threading.Lock（プロセス内のみ有効）を sqlite3 WAL + busy_timeout に置き換える。
_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cross_account_state.db"


def _get_db(path: Path = _DB_PATH) -> sqlite3.Connection:
    """SQLite接続を返す（WALモード + busy_timeout 3秒）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=3.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS firm_last_order (
            firm TEXT PRIMARY KEY,
            ts   REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_active_positions (
            account_id TEXT NOT NULL,
            symbol_side TEXT NOT NULL,
            PRIMARY KEY (account_id, symbol_side)
        )
    """)
    conn.commit()
    return conn


class CrossAccountGuard:
    """口座間の相関検出を物理的に回避する Layer PF-2 ガード。

    P0-CRITICAL-3: threading.Lock から SQLite WAL ベースのプロセス間共有ストアに変更。
    data/cross_account_state.db で5プロセス（5口座）が同じ状態を読み書きする。

    P1-HIGH-12: check_and_record() で TOCTOU race condition を解消する。
    check_before_order() と record_order() を個別に呼ぶと、
    check → （他プロセスが先行して record） → record の順で race が発生する。
    check_and_record() は SQLite トランザクション内で check + record を原子的に行う。

    Attributes:
        min_delay_sec: 同 firm 内で前回発注から必要な最低待機秒数（デフォルト 3 秒）
    """

    def __init__(self, min_delay_sec: int = 3, db_path: Path = _DB_PATH):
        self.min_delay_sec = min_delay_sec
        self._db_path = db_path
        self._lock = threading.Lock()  # プロセス内スレッド競合の追加保護

    def _conn(self) -> sqlite3.Connection:
        return _get_db(self._db_path)

    def check_before_order(
        self,
        firm: str,
        account_id: str,
        symbol: str,
        side: str,
    ) -> tuple[bool, str]:
        """発注前の Cross-Account 相関チェック（read-only）。

        Args:
            firm:       "mffu" | "tradeify" | "apex"
            account_id: 口座識別子（例: "mffu_core_50k_001"）
            symbol:     発注銘柄（例: "MES"）
            side:       "BUY" | "SELL"

        Returns:
            (allow: bool, reason: str)

        注意: P1-HIGH-12 対応として check_and_record() の使用を推奨する。
        """
        now_ts = _time.time()
        key = f"{symbol.upper()}:{side.upper()}"
        with self._lock:
            try:
                conn = self._conn()
                # チェック 1: 同 firm の前回発注から min_delay_sec 経過していること
                row = conn.execute(
                    "SELECT ts FROM firm_last_order WHERE firm=?", (firm,)
                ).fetchone()
                if row is not None:
                    elapsed = now_ts - row[0]
                    if elapsed < self.min_delay_sec:
                        conn.close()
                        return False, (
                            f"Cross-Account delay: firm={firm} "
                            f"前回から{elapsed:.2f}秒 < {self.min_delay_sec}秒"
                        )

                # チェック 2: 他口座で同銘柄・同方向が active
                rows = conn.execute(
                    "SELECT account_id FROM account_active_positions "
                    "WHERE symbol_side=? AND account_id != ?",
                    (key, account_id),
                ).fetchall()
                if rows:
                    other_ids = [r[0] for r in rows]
                    conn.close()
                    return False, (
                        f"Cross-Account 相関検出: {symbol} {side} が "
                        f"他口座 {other_ids} で active — 同方向同時発注禁止"
                    )
                conn.close()
            except Exception as e:
                log.warning("[CrossAccountGuard] check_before_order DB error: %s", e)
        return True, ""

    def check_and_record(
        self,
        firm: str,
        account_id: str,
        symbol: str,
        side: str,
    ) -> tuple[bool, str]:
        """チェックと記録を原子的に実行する（TOCTOU race 解消）。

        P1-HIGH-12: check_before_order() + record_order() を個別呼出しすると
        チェックと記録の間に他プロセスが割り込む TOCTOU race が発生する。
        このメソッドは SQLite EXCLUSIVE トランザクション内で両方を実行する。

        allow=True の場合のみ DB を更新する（チェック失敗時は DB を変更しない）。

        Returns:
            (allow: bool, reason: str)
        """
        now_ts = _time.time()
        key = f"{symbol.upper()}:{side.upper()}"
        with self._lock:
            try:
                conn = self._conn()
                conn.execute("BEGIN EXCLUSIVE")

                # チェック 1: delay
                row = conn.execute(
                    "SELECT ts FROM firm_last_order WHERE firm=?", (firm,)
                ).fetchone()
                if row is not None:
                    elapsed = now_ts - row[0]
                    if elapsed < self.min_delay_sec:
                        conn.execute("ROLLBACK")
                        conn.close()
                        return False, (
                            f"Cross-Account delay: firm={firm} "
                            f"前回から{elapsed:.2f}秒 < {self.min_delay_sec}秒"
                        )

                # チェック 2: 他口座 active
                rows = conn.execute(
                    "SELECT account_id FROM account_active_positions "
                    "WHERE symbol_side=? AND account_id != ?",
                    (key, account_id),
                ).fetchall()
                if rows:
                    other_ids = [r[0] for r in rows]
                    conn.execute("ROLLBACK")
                    conn.close()
                    return False, (
                        f"Cross-Account 相関検出: {symbol} {side} が "
                        f"他口座 {other_ids} で active — 同方向同時発注禁止"
                    )

                # 記録
                conn.execute(
                    "INSERT OR REPLACE INTO firm_last_order(firm, ts) VALUES(?,?)",
                    (firm, now_ts),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO account_active_positions(account_id, symbol_side) VALUES(?,?)",
                    (account_id, key),
                )
                conn.execute("COMMIT")
                conn.close()
            except Exception as e:
                # MF-5 fix: DB 接続失敗時は fail-closed（発注拒否）。
                # 旧実装は allow=True（fail-open）でクロスアカウントガードが無効化されていた。
                # DB 障害時はクロスアカウントヘッジ検出が機能しないため安全側（拒否）に倒す。
                log.error(
                    "[CrossAccountGuard] check_and_record DB error — fail-closed: %s", e
                )
                return False, f"CrossAccountGuard DB error (fail-closed): {e}"
        return True, ""

    def record_order(
        self,
        firm: str,
        account_id: str,
        symbol: str,
        side: str,
    ) -> None:
        """発注実行後に記録する。check_and_record() の使用を推奨。"""
        now_ts = _time.time()
        key = f"{symbol.upper()}:{side.upper()}"
        with self._lock:
            try:
                conn = self._conn()
                conn.execute(
                    "INSERT OR REPLACE INTO firm_last_order(firm, ts) VALUES(?,?)",
                    (firm, now_ts),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO account_active_positions(account_id, symbol_side) VALUES(?,?)",
                    (account_id, key),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.warning("[CrossAccountGuard] record_order DB error: %s", e)
        log.debug(
            "[CrossAccountGuard] record_order: firm=%s account=%s %s %s",
            firm, account_id, symbol, side,
        )

    def record_close(
        self,
        account_id: str,
        symbol: str,
        side: str,
    ) -> None:
        """ポジション決済時に active セットから除去する。"""
        key = f"{symbol.upper()}:{side.upper()}"
        with self._lock:
            try:
                conn = self._conn()
                conn.execute(
                    "DELETE FROM account_active_positions WHERE account_id=? AND symbol_side=?",
                    (account_id, key),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.warning("[CrossAccountGuard] record_close DB error: %s", e)
        log.debug(
            "[CrossAccountGuard] record_close: account=%s %s %s",
            account_id, symbol, side,
        )

    def get_active_positions(self, account_id: str) -> set[str]:
        """account_id の active ポジションセットを返す（テスト用）。"""
        with self._lock:
            try:
                conn = self._conn()
                rows = conn.execute(
                    "SELECT symbol_side FROM account_active_positions WHERE account_id=?",
                    (account_id,),
                ).fetchall()
                conn.close()
                return {r[0] for r in rows}
            except Exception as e:
                log.warning("[CrossAccountGuard] get_active_positions error: %s", e)
                return set()

    def get_last_order_time(self, firm: str) -> Optional[datetime.datetime]:
        """firm の前回発注日時を返す（テスト用）。"""
        with self._lock:
            try:
                conn = self._conn()
                row = conn.execute(
                    "SELECT ts FROM firm_last_order WHERE firm=?", (firm,)
                ).fetchone()
                conn.close()
                if row:
                    return datetime.datetime.fromtimestamp(row[0])
                return None
            except Exception as e:
                log.warning("[CrossAccountGuard] get_last_order_time error: %s", e)
                return None

    def reset(self) -> None:
        """全状態をリセット（テスト用）。"""
        with self._lock:
            try:
                conn = self._conn()
                conn.execute("DELETE FROM firm_last_order")
                conn.execute("DELETE FROM account_active_positions")
                conn.commit()
                conn.close()
            except Exception as e:
                log.warning("[CrossAccountGuard] reset error: %s", e)


# グローバルシングルトン（chronos_bot.py からは这のインスタンスを共有する）
_global_guard: Optional[CrossAccountGuard] = None


def get_global_guard(min_delay_sec: int = 3) -> CrossAccountGuard:
    """グローバルシングルトンを取得する。chronos_bot.py で使用。"""
    global _global_guard
    if _global_guard is None:
        _global_guard = CrossAccountGuard(min_delay_sec=min_delay_sec)
    return _global_guard


def reset_global_guard() -> None:
    """テスト用: グローバルシングルトンをリセットする。"""
    global _global_guard
    _global_guard = None
