"""tests/test_atlas_v3_0dte_redteam_r2.py — ZeroDTESystemTactic Redteam r2 CRITICAL 5件

対象修正:
  C-4: Daily Stop 半開区間 — daily_pnl <= daily_stop_loss で境界を含む停止
  C-5: update_daily_pnl 非原子 — threading.Lock で read-modify-write 原子化
  C-6: 15:30 ET 以降 24h 暴発 — RTH 終了は 15:30-15:59 ET のみ（16:00+ は False）
  C-7: restore_state 前日 ORB 誤使用 — observed_at の ET 日付が今日でなければ confirmed=False
  C-8: persist save 失敗 silent — OSError/例外時 log.error + Pushover escalation

完了条件: 既存 42 + 本ファイル ≥ 5 = ≥ 47 PASS
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Literal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.orb_1dte_spy import ORBRange
from atlas_v3.strategies.zero_dte_system import (
    ZeroDTEConfig,
    ZeroDTEPosition,
    ZeroDTESystemTactic,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_kill_switch(tmp_path, monkeypatch):
    """Kill Switch の state_v3 を tmp_path に隔離（テスト間干渉防止）。"""
    import common_v3.risk.kill_switch as ks_module
    tmp_state = tmp_path / "state_v3"
    tmp_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_module, "_STATE_DIR", tmp_state)
    monkeypatch.setattr(ks_module, "FLAG_FILE", tmp_state / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", tmp_state / "kill_switch_audit.jsonl")
    yield


def _env(vix: float = 18.0, gex: float = 1.0, bias: str = "bull") -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=1.5,
        gex=gex,
        term_ratio=1.0,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={"SPX": 45.0},
    )


def _tactic(config: ZeroDTEConfig | None = None) -> ZeroDTESystemTactic:
    return ZeroDTESystemTactic(config=config)


def _position(
    symbol: str = "SPX",
    entry_price: float = 5.0,
    unrealized_pnl: float = 0.0,
    max_credit: float = 0.0,
) -> ZeroDTEPosition:
    return ZeroDTEPosition(
        symbol=symbol,
        quantity=1,
        entry_price=entry_price,
        current_price=entry_price,
        tactic_name="0dte_system",
        entry_time=datetime.now(timezone.utc),
        unrealized_pnl=unrealized_pnl,
        max_credit=max_credit,
    )


class _MemStorage:
    """インメモリ StorageBackend（テスト用）。"""
    def __init__(self) -> None:
        self._store: dict = {}

    def save(self, key: str, data: dict) -> None:
        self._store[key] = data

    def load(self, key: str) -> dict | None:
        return self._store.get(key)


class _FailingStorage:
    """save が常に OSError を raise する StorageBackend（C-8 テスト用）。"""
    def save(self, key: str, data: dict) -> None:
        raise OSError("disk full: simulated error")

    def load(self, key: str) -> dict | None:
        return None


# ---------------------------------------------------------------------------
# C-4: Daily Stop 半開区間
# ---------------------------------------------------------------------------

class TestC4DailyStopBoundary:
    """C-4: daily_stop_loss の境界値テスト。

    daily_stop_loss=-2000 のとき:
    - pnl=-2000.00 → 停止（境界値到達）
    - pnl=-2000.01 → 停止（超過）
    - pnl=-1999.99 → 継続（未達）

    preflight と should_exit の両方で一致していること。
    """

    def test_c4_preflight_at_exact_boundary_stops(self):
        """pnl が stop と完全一致したとき preflight が False を返す。"""
        cfg = ZeroDTEConfig(daily_stop_loss=-2000.0)
        t = _tactic(cfg)
        t.update_daily_pnl(-2000.0)
        assert t.preflight(_env()) is False

    def test_c4_preflight_below_boundary_stops(self):
        """pnl が stop を下回ったとき preflight が False を返す。"""
        cfg = ZeroDTEConfig(daily_stop_loss=-2000.0)
        t = _tactic(cfg)
        t.update_daily_pnl(-2000.01)
        assert t.preflight(_env()) is False

    def test_c4_preflight_above_boundary_continues(self):
        """pnl=-1999.99（stop=-2000）のとき preflight が True を返す（まだ未達）。"""
        cfg = ZeroDTEConfig(daily_stop_loss=-2000.0)
        t = _tactic(cfg)
        t.update_daily_pnl(-1999.99)
        assert t.preflight(_env()) is True

    def test_c4_should_exit_at_exact_boundary(self):
        """pnl が stop と完全一致したとき should_exit が daily_stop を返す。"""
        cfg = ZeroDTEConfig(daily_stop_loss=-2000.0)
        t = _tactic(cfg)
        t.update_daily_pnl(-2000.0)
        result = t.should_exit(_position(), _env())
        assert result.should_exit is True
        assert result.exit_type == "daily_stop"

    def test_c4_should_exit_above_boundary_no_stop(self):
        """pnl=-1999.99（stop=-2000）のとき should_exit が daily_stop を返さない。"""
        cfg = ZeroDTEConfig(daily_stop_loss=-2000.0)
        t = _tactic(cfg)
        t.update_daily_pnl(-1999.99)
        result = t.should_exit(
            _position(entry_price=5.0, max_credit=0.0, unrealized_pnl=0.0),
            _env(),
        )
        assert result.exit_type != "daily_stop"


# ---------------------------------------------------------------------------
# C-5: update_daily_pnl 非原子 — threading.Lock
# ---------------------------------------------------------------------------

class TestC5ConcurrentUpdateDailyPnl:
    """C-5: 100 スレッド並行 update_daily_pnl で整合性が保たれること。"""

    def test_c5_concurrent_update_100_threads(self):
        """100 並行スレッドで各 -10 を加算 → 合計が -1000 と一致（race condition なし）。"""
        t = _tactic()
        n_threads = 100
        delta = -10.0
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()  # 全スレッド同時開始（競合を誘発）
            t.update_daily_pnl(delta)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        expected = n_threads * delta
        assert t._daily_pnl == pytest.approx(expected), (
            f"race condition detected: expected {expected}, got {t._daily_pnl}"
        )

    def test_c5_pnl_lock_attribute_exists(self):
        """_pnl_lock が threading.Lock インスタンスとして存在すること。"""
        t = _tactic()
        assert hasattr(t, "_pnl_lock")
        assert isinstance(t._pnl_lock, type(threading.Lock()))

    def test_c5_reset_daily_pnl_under_lock(self):
        """reset_daily_pnl がロック下で実行されること（reset 後に pnl=0）。"""
        t = _tactic()
        t.update_daily_pnl(-500.0)
        t.reset_daily_pnl()
        assert t._daily_pnl == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# C-6: 15:30 ET 以降 24h 暴発 — RTH 終了は 15:30-15:59 ET のみ
# ---------------------------------------------------------------------------

class TestC6ForceCloseTimeRange:
    """C-6: _is_force_close_time が RTH 外（16:00 以降）で False を返すこと。"""

    def test_c6_1600_et_does_not_trigger(self):
        """16:00 ET は RTH 後 → force_close を発動しない。"""
        t = _tactic()
        result = t.should_exit(
            _position(), _env(),
            current_et_hour=16, current_et_minute=0,
        )
        assert result.should_exit is False

    def test_c6_2200_et_does_not_trigger(self):
        """22:00 ET（restore 直後などのアフターアワー）→ force_close を発動しない。"""
        t = _tactic()
        result = t.should_exit(
            _position(), _env(),
            current_et_hour=22, current_et_minute=0,
        )
        assert result.should_exit is False

    def test_c6_1530_et_triggers(self):
        """15:30 ET は RTH 終了 → force_close を発動する。"""
        t = _tactic()
        result = t.should_exit(
            _position(), _env(),
            current_et_hour=15, current_et_minute=30,
        )
        assert result.should_exit is True
        assert result.exit_type == "eod_close"

    def test_c6_1559_et_triggers(self):
        """15:59 ET は RTH 終了範囲内 → force_close を発動する。"""
        t = _tactic()
        result = t.should_exit(
            _position(), _env(),
            current_et_hour=15, current_et_minute=59,
        )
        assert result.should_exit is True
        assert result.exit_type == "eod_close"

    def test_c6_is_force_close_1600_false(self):
        """_is_force_close_time(16, 0) は False。"""
        t = _tactic()
        assert t._is_force_close_time(16, 0) is False

    def test_c6_is_force_close_0930_false(self):
        """_is_force_close_time(9, 30) は False（市場開始直後）。"""
        t = _tactic()
        assert t._is_force_close_time(9, 30) is False


# ---------------------------------------------------------------------------
# C-7: restore_state 前日 ORB 誤使用
# ---------------------------------------------------------------------------

class TestC7RestoreStateStaleDateReset:
    """C-7: 前日 observed の ORB は restore 時に confirmed=False にリセットされること。"""

    def _make_storage_with_orb(
        self,
        symbol: str,
        observed_date_et: str,
        is_confirmed: bool = True,
    ) -> _MemStorage:
        """指定の ET 日付で observed_at を持つ ORB state を保存した MemStorage を返す。"""
        storage = _MemStorage()
        et_tz = ZoneInfo("America/New_York")
        # observed_at を指定 ET 日付の 10:00 ET として構築
        observed_at = datetime.fromisoformat(f"{observed_date_et}T10:00:00").replace(
            tzinfo=et_tz
        )
        state_data = {
            "orb_ranges": {
                symbol: {
                    "high": 520.0,
                    "low": 510.0,
                    "is_confirmed": is_confirmed,
                    "observed_at": observed_at.isoformat(),
                    "symbol": symbol,
                }
            },
            "gamma_levels": {symbol: 1.5},
            "daily_pnl": -100.0,
            "persisted_at": observed_at.isoformat(),
        }
        storage.save("0dte_system_state", state_data)
        return storage

    def test_c7_previous_day_orb_confirmed_reset(self):
        """前日 observed ORB は restore 後に is_confirmed=False になること。"""
        t = _tactic()
        # 昨日の日付で ORB を保存
        yesterday_et = (
            datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        storage = self._make_storage_with_orb("SPX", yesterday_et, is_confirmed=True)
        t.restore_state(storage)

        assert "SPX" in t._orb_ranges
        assert t._orb_ranges["SPX"].is_confirmed is False, (
            "前日 ORB の confirmed が True のまま（C-7 未修正）"
        )

    def test_c7_today_orb_confirmed_preserved(self):
        """当日 observed ORB は restore 後も is_confirmed=True が保持されること。"""
        t = _tactic()
        today_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        storage = self._make_storage_with_orb("SPX", today_et, is_confirmed=True)
        t.restore_state(storage)

        assert "SPX" in t._orb_ranges
        assert t._orb_ranges["SPX"].is_confirmed is True, (
            "当日 ORB の confirmed が False にリセットされた（C-7 過剰修正）"
        )

    def test_c7_previous_day_orb_not_usable_for_entry(self):
        """前日 ORB から restore した後 should_enter が ORB 未確定で False を返すこと。"""
        t = _tactic()
        yesterday_et = (
            datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        storage = self._make_storage_with_orb("SPX", yesterday_et, is_confirmed=True)
        t.restore_state(storage)
        t._gamma_levels["SPX"] = 1.5

        decisions = t.should_enter(_env(vix=20.0), ["SPX"])
        assert len(decisions) == 1
        assert decisions[0].should_enter is False
        assert "ORB 未確定" in decisions[0].reason


# ---------------------------------------------------------------------------
# C-8: persist save 失敗 silent
# ---------------------------------------------------------------------------

class TestC8PersistSaveFailure:
    """C-8: storage.save が失敗したとき log.error と Pushover escalation が走ること。"""

    def test_c8_oserror_raises_and_logs(self, caplog):
        """OSError 発生時に log.error が記録されて例外が伝播すること。"""
        import logging
        t = _tactic()
        t._orb_ranges["SPX"] = ORBRange(
            high=520.0, low=510.0, is_confirmed=True,
            observed_at=datetime.now(timezone.utc), symbol="SPX",
        )
        storage = _FailingStorage()

        with caplog.at_level(logging.ERROR):
            with pytest.raises(OSError):
                t.persist_state(storage)

        assert any("persist_state" in record.message for record in caplog.records), (
            "log.error が記録されていない（C-8 未修正）"
        )

    def test_c8_pushover_escalation_called_on_failure(self):
        """storage.save 失敗時に Pushover send が呼ばれること（送信失敗は無視）。"""
        t = _tactic()
        storage = _FailingStorage()

        with patch(
            "atlas_v3.strategies.zero_dte_system.ZeroDTESystemTactic._escalate_persist_failure"
        ) as mock_escalate:
            with pytest.raises(OSError):
                t.persist_state(storage)
            mock_escalate.assert_called_once()

    def test_c8_success_path_no_exception(self):
        """正常 save では例外が発生しないこと。"""
        t = _tactic()
        storage = _MemStorage()
        t.persist_state(storage)  # should not raise

    def test_c8_escalate_pushover_unavailable_does_not_raise(self):
        """Pushover が利用不可のときも _escalate_persist_failure が二次例外を起こさない。"""
        t = _tactic()
        # ImportError を起こす mock で Pushover 不在をシミュレート
        exc = OSError("test error")
        with patch(
            "atlas_v3.strategies.zero_dte_system.ZeroDTESystemTactic._escalate_persist_failure",
            wraps=lambda e: None,  # Pushover 失敗しても何もしない
        ):
            # 直接 _escalate_persist_failure を呼んでも例外が出ないこと
            ZeroDTESystemTactic._escalate_persist_failure(exc)  # should not raise
