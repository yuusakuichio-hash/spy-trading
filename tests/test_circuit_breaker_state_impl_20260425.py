"""tests/test_circuit_breaker_state_impl_20260425.py

Sprint 1 state machine 実装確認テスト

対象:
  CircuitBreaker.state プロパティ / call(fn) / reset(approver)
  - 状態遷移: CLOSED → OPEN → HALF_OPEN → CLOSED
  - fail_max 到達で OPEN 遷移
  - reset_timeout 経過で HALF_OPEN 遷移（プロパティアクセス時）
  - HALF_OPEN 成功で CLOSED 遷移
  - HALF_OPEN 失敗で OPEN 再遷移
  - reset(approver='yuusaku') で CLOSED 復帰
  - engine.py の except NotImplementedError: pass が除去され fail-closed であること

完了基準: 20 件以上 PASS
"""
from __future__ import annotations

import time

import pytest

from common_v3.self_healing.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerApproverInvalid,
    CircuitBreakerOpenError,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_cb(fail_max: int = 3, reset_timeout: float = 300.0) -> CircuitBreaker:
    """テスト用 CircuitBreaker を生成する。"""
    return CircuitBreaker(name="test_cb", fail_max=fail_max, reset_timeout=reset_timeout)


def _force_fail(cb: CircuitBreaker, n: int) -> None:
    """cb.call() を通じて n 回失敗を記録する。"""
    def _fail():
        raise RuntimeError("forced failure")

    for _ in range(n):
        with pytest.raises(RuntimeError):
            cb.call(_fail)


# ===========================================================================
# 1. 初期状態
# ===========================================================================

class TestInitialState:
    """新規生成直後は CLOSED であること。"""

    def test_initial_state_is_closed(self):
        """生成直後の state は 'CLOSED'"""
        cb = _make_cb()
        assert cb.state == "CLOSED"

    def test_reset_timeout_default(self):
        """reset_timeout デフォルト 300.0 秒"""
        cb = CircuitBreaker(name="default_rt")
        assert cb.reset_timeout == 300.0

    def test_reset_timeout_custom(self):
        """reset_timeout カスタム値が保持される"""
        cb = CircuitBreaker(name="custom_rt", reset_timeout=60.0)
        assert cb.reset_timeout == 60.0

    def test_reset_timeout_immutable(self):
        """reset_timeout は書き換え不可（frozen design: CircuitBreakerFrozenViolation or AttributeError）"""
        from common_v3.self_healing.circuit_breaker import CircuitBreakerFrozenViolation
        cb = _make_cb()
        with pytest.raises((CircuitBreakerFrozenViolation, AttributeError)):
            cb.reset_timeout = 999.0  # type: ignore[misc]


# ===========================================================================
# 2. fail_max 到達で OPEN 遷移
# ===========================================================================

class TestFailMaxTriggersOpen:
    """fail_max 回失敗で OPEN に遷移する。"""

    def test_below_fail_max_stays_closed(self):
        """fail_max-1 回失敗でも CLOSED のまま"""
        cb = _make_cb(fail_max=3)
        _force_fail(cb, 2)
        assert cb.state == "CLOSED"

    def test_fail_max_reached_opens(self):
        """fail_max 回失敗で OPEN に遷移する"""
        cb = _make_cb(fail_max=3)
        _force_fail(cb, 3)
        assert cb.state == "OPEN"

    def test_fail_max_1_opens_immediately(self):
        """fail_max=1 の場合は 1 回失敗で即 OPEN"""
        cb = _make_cb(fail_max=1)
        _force_fail(cb, 1)
        assert cb.state == "OPEN"

    def test_fail_max_5_requires_5_failures(self):
        """fail_max=5 では 4 回失敗後は CLOSED のまま"""
        cb = _make_cb(fail_max=5)
        _force_fail(cb, 4)
        assert cb.state == "CLOSED"
        _force_fail(cb, 1)
        assert cb.state == "OPEN"


# ===========================================================================
# 3. OPEN 状態での call() は CircuitBreakerOpenError を raise
# ===========================================================================

class TestOpenStateBlocksCall:
    """OPEN 状態では call() が CircuitBreakerOpenError を raise する。"""

    def test_open_raises_circuit_breaker_open_error(self):
        """OPEN 状態で call() → CircuitBreakerOpenError"""
        cb = _make_cb(fail_max=1)
        _force_fail(cb, 1)
        assert cb.state == "OPEN"

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(lambda: "should not run")

        assert "OPEN" in str(exc_info.value)

    def test_circuit_breaker_open_error_has_name(self):
        """CircuitBreakerOpenError.name が CB 名を返す"""
        cb = CircuitBreaker(name="named_cb", fail_max=1)
        _force_fail(cb, 1)

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(lambda: None)

        assert exc_info.value.name == "named_cb"

    def test_circuit_breaker_open_error_has_reset_timeout(self):
        """CircuitBreakerOpenError.reset_timeout が正しい値を返す"""
        cb = CircuitBreaker(name="rt_cb", fail_max=1, reset_timeout=42.0)
        _force_fail(cb, 1)

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.call(lambda: None)

        assert exc_info.value.reset_timeout == 42.0

    def test_function_not_executed_when_open(self):
        """OPEN 状態では関数が呼ばれない"""
        cb = _make_cb(fail_max=1)
        _force_fail(cb, 1)

        executed = {"called": False}

        def _mark():
            executed["called"] = True
            return "result"

        with pytest.raises(CircuitBreakerOpenError):
            cb.call(_mark)

        assert executed["called"] is False


# ===========================================================================
# 4. reset_timeout 経過で HALF_OPEN 遷移
# ===========================================================================

class TestResetTimeoutHalfOpen:
    """reset_timeout 経過後は state プロパティが HALF_OPEN を返す。"""

    def test_open_to_half_open_after_timeout(self):
        """reset_timeout 経過後は HALF_OPEN に遷移する"""
        cb = _make_cb(fail_max=1, reset_timeout=0.02)
        _force_fail(cb, 1)
        assert cb.state == "OPEN"

        time.sleep(0.05)
        assert cb.state == "HALF_OPEN"

    def test_open_before_timeout_stays_open(self):
        """reset_timeout 経過前は OPEN のまま"""
        cb = _make_cb(fail_max=1, reset_timeout=9999.0)
        _force_fail(cb, 1)
        assert cb.state == "OPEN"

    def test_half_open_allows_call(self):
        """HALF_OPEN 状態では call() が実行される（CircuitBreakerOpenError を raise しない）"""
        cb = _make_cb(fail_max=1, reset_timeout=0.02)
        _force_fail(cb, 1)
        time.sleep(0.05)
        assert cb.state == "HALF_OPEN"

        result = cb.call(lambda: "probe_ok")
        assert result == "probe_ok"


# ===========================================================================
# 5. HALF_OPEN 成功で CLOSED 遷移
# ===========================================================================

class TestHalfOpenSuccessCloses:
    """HALF_OPEN 中に成功 → CLOSED に戻る。"""

    def test_half_open_success_transitions_to_closed(self):
        """HALF_OPEN 成功 → CLOSED"""
        cb = _make_cb(fail_max=1, reset_timeout=0.02)
        _force_fail(cb, 1)
        time.sleep(0.05)
        assert cb.state == "HALF_OPEN"

        cb.call(lambda: "ok")
        assert cb.state == "CLOSED"

    def test_half_open_success_clears_failure_count(self):
        """HALF_OPEN 成功後は再び fail_max まで失敗できる"""
        cb = _make_cb(fail_max=2, reset_timeout=0.02)
        _force_fail(cb, 2)
        time.sleep(0.05)
        cb.call(lambda: "ok")
        assert cb.state == "CLOSED"
        # 1 回失敗でも OPEN にならない
        _force_fail(cb, 1)
        assert cb.state == "CLOSED"


# ===========================================================================
# 6. HALF_OPEN 失敗で OPEN 再遷移
# ===========================================================================

class TestHalfOpenFailureReopens:
    """HALF_OPEN 中に失敗 → OPEN に戻る。"""

    def test_half_open_failure_transitions_back_to_open(self):
        """HALF_OPEN 失敗 → OPEN"""
        cb = _make_cb(fail_max=1, reset_timeout=0.02)
        _force_fail(cb, 1)
        time.sleep(0.05)
        assert cb.state == "HALF_OPEN"

        _force_fail(cb, 1)
        assert cb.state == "OPEN"

    def test_half_open_failure_then_timeout_gives_half_open_again(self):
        """HALF_OPEN 失敗 → OPEN → reset_timeout 経過 → HALF_OPEN"""
        cb = _make_cb(fail_max=1, reset_timeout=0.02)
        _force_fail(cb, 1)
        time.sleep(0.05)
        assert cb.state == "HALF_OPEN"
        _force_fail(cb, 1)
        assert cb.state == "OPEN"
        time.sleep(0.05)
        assert cb.state == "HALF_OPEN"


# ===========================================================================
# 7. reset(approver) で CLOSED 復帰
# ===========================================================================

class TestResetRestoresClosed:
    """reset(approver='yuusaku') で OPEN/HALF_OPEN → CLOSED。"""

    def test_reset_from_open_to_closed(self):
        """OPEN 状態を reset() で CLOSED に戻す"""
        cb = _make_cb(fail_max=1)
        _force_fail(cb, 1)
        assert cb.state == "OPEN"

        cb.reset(approver="yuusaku")
        assert cb.state == "CLOSED"

    def test_reset_from_closed_is_noop(self):
        """CLOSED 状態で reset() しても CLOSED のまま（副作用なし）"""
        cb = _make_cb()
        cb.reset(approver="yuusaku")
        assert cb.state == "CLOSED"

    def test_reset_invalid_approver_raises(self):
        """invalid approver では CircuitBreakerApproverInvalid"""
        cb = _make_cb(fail_max=1)
        _force_fail(cb, 1)

        with pytest.raises(CircuitBreakerApproverInvalid):
            cb.reset(approver="auto")

    def test_reset_invalid_approver_does_not_change_state(self):
        """approver 検証失敗時は state が変わらない"""
        cb = _make_cb(fail_max=1)
        _force_fail(cb, 1)
        assert cb.state == "OPEN"

        with pytest.raises(CircuitBreakerApproverInvalid):
            cb.reset(approver="")

        assert cb.state == "OPEN"

    def test_reset_allows_normal_operation_after_recovery(self):
        """reset() 後は通常 call() が成功する"""
        cb = _make_cb(fail_max=1)
        _force_fail(cb, 1)
        cb.reset(approver="yuusaku")

        result = cb.call(lambda: "recovered")
        assert result == "recovered"


# ===========================================================================
# 8. engine.py fail-closed 確認（NotImplementedError pass 除去）
# ===========================================================================

class TestEngineFailClosed:
    """engine.py の except NotImplementedError: pass が除去されていること。"""

    def test_engine_breaker_check_is_fail_closed(self):
        """engine.py に 'except NotImplementedError: pass' がないことを AST で確認する。"""
        import ast
        from pathlib import Path

        engine_path = Path(__file__).parent.parent / "atlas_v3" / "core" / "engine.py"
        source = engine_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                # 'except NotImplementedError: pass' の組み合わせを検出
                if (
                    node.type is not None
                    and isinstance(node.type, ast.Name)
                    and node.type.id == "NotImplementedError"
                ):
                    # body が pass のみかチェック
                    body_is_pass = (
                        len(node.body) == 1
                        and isinstance(node.body[0], ast.Pass)
                    )
                    if body_is_pass:
                        pytest.fail(
                            f"engine.py line {node.lineno}: "
                            "'except NotImplementedError: pass' が残存しています。"
                            "fail-closed 要件違反です。"
                        )

    def test_moomoo_breaker_state_is_callable_from_engine_module(self):
        """engine.py の moomoo_breaker.state が NotImplementedError を raise しない。"""
        from common_v3.self_healing.instances import moomoo_breaker
        # state プロパティが正常に返ること（NotImplementedError が raise されないこと）
        s = moomoo_breaker.state
        assert s in ("CLOSED", "OPEN", "HALF_OPEN")

    def test_breaker_open_blocks_engine_order_submission(self):
        """moomoo_breaker が OPEN 状態のとき engine は BrokerUnavailable を raise する。"""
        from unittest.mock import MagicMock, patch
        from common_v3.self_healing.instances import moomoo_breaker

        # CircuitBreaker の state を OPEN に見せかける patch
        with patch.object(type(moomoo_breaker), "state", new_callable=lambda: property(lambda self: "OPEN")):
            from atlas_v3.core.engine import AtlasEngine, BrokerUnavailable
            from common_v3.risk.kill_switch import is_active as ks_is_active

            engine = AtlasEngine.__new__(AtlasEngine)
            mock_broker = MagicMock()
            mock_market = MagicMock()

            # _submit_order_with_idempotency を呼ぶ最小セットアップ
            engine._broker = mock_broker
            engine._market_data = mock_market
            engine._idempotency_store = MagicMock()
            engine._tick_started_at = None

            from atlas_v3.strategies.base import TacticBase
            mock_tactic = MagicMock(spec=TacticBase)
            mock_tactic.tactic_name = "test_tactic"

            mock_decision = MagicMock()
            mock_decision.side = "buy"
            mock_decision.symbol = "SPY"
            mock_decision.quantity = 1  # quantity sanity check 通過

            # kill_switch は active でないことを保証
            with patch("atlas_v3.core.engine.kill_switch_is_active", return_value=False):
                with pytest.raises(BrokerUnavailable):
                    engine._submit_order_with_idempotency(
                        tactic=mock_tactic,
                        decision=mock_decision,
                        env=MagicMock(),
                    )
