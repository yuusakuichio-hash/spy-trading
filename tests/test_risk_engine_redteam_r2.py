"""tests/test_risk_engine_redteam_r2.py — RiskEngine Redteam r2 CRITICAL fix テスト

対象 fix:
  C-α: NaN/inf bypass 防止 (returns_history に nan/inf → DENY)
  C-β: kill_switch_bypass_approver allowlist + audit log 追記
  C-γ: _escalate_kill_switch_failure 非同期化 (Pushover blocking 解消)
  C-δ: returns_unit=ratio 時 max_var_ratio で判定 (unit 不整合 ValueError)

完了条件: 既存 102 + 本ファイル >= 8 tests PASS / regression 0
"""
from __future__ import annotations

import json
import math
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from common_v3.risk.engine import (
    OptionRequest,
    PortfolioSnapshot,
    PositionSizingMethod,
    RiskConfig,
    RiskDecision,
    RiskEngine,
    _FAT_TAIL_MULTIPLIER,
    _MIN_VAR_HISTORY,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _sufficient_returns(worst: float = -500.0, n: int = 100) -> tuple:
    """C-1/C-2 準拠 (n >= 100) の returns_history を生成する。"""
    return (worst,) + tuple(0.0 for _ in range(n - 1))


def _patch_ks(active: bool = False):
    return patch(
        "common_v3.risk.engine.RiskEngine._check_kill_switch",
        return_value=RiskDecision(allowed=False, reason="kill_switch is active", sizing=0)
        if active else None,
    )


def _make_engine(**overrides) -> RiskEngine:
    base = dict(
        max_notional_usd=50_000.0,
        max_daily_loss_usd=-2_000.0,
        max_drawdown_pct=0.10,
        max_var_usd=100_000.0,
        max_assignment_risk_usd=20_000.0,
        fixed_size_contracts=1,
        kelly_fraction=0.25,
        vix_size_base=20.0,
        sizing_method=PositionSizingMethod.FIXED,
    )
    base.update(overrides)
    return RiskEngine(config=RiskConfig(**base))


# ---------------------------------------------------------------------------
# C-α: NaN/inf bypass 防止
# ---------------------------------------------------------------------------

class TestCAlphaNanInfBypass:

    def test_nan_in_returns_history_denied(self) -> None:
        """C-α: returns_history に nan が含まれると check_all が DENY する"""
        eng = _make_engine()
        returns = (float("nan"),) * 100
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=returns,
        )
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is False
        assert "nan/inf" in decision.reason

    def test_inf_in_returns_history_denied(self) -> None:
        """C-α: returns_history に inf が含まれると check_all が DENY する"""
        eng = _make_engine()
        returns = (float("inf"),) * 100
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=returns,
        )
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is False
        assert "nan/inf" in decision.reason

    def test_mixed_nan_in_returns_raises_value_error(self) -> None:
        """C-α: check_var() で returns_history の途中に nan → ValueError"""
        eng = _make_engine()
        # 99 件は正常、1 件が nan → isfinite 検査で検知
        returns = tuple(-float(i) for i in range(99)) + (float("nan"),)
        portfolio = PortfolioSnapshot(returns_history=returns)
        with pytest.raises(ValueError, match="nan/inf in returns_history"):
            eng.check_var(portfolio)

    def test_neg_inf_in_returns_raises_value_error(self) -> None:
        """C-α: check_var() で returns_history に -inf → ValueError"""
        eng = _make_engine()
        returns = (float("-inf"),) + tuple(0.0 for _ in range(99))
        portfolio = PortfolioSnapshot(returns_history=returns)
        with pytest.raises(ValueError, match="nan/inf in returns_history"):
            eng.check_var(portfolio)

    def test_nan_in_request_notional_denied(self) -> None:
        """C-α: request_notional=nan → check_all が DENY する"""
        eng = _make_engine()
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=_sufficient_returns(),
        )
        with _patch_ks(False):
            decision = eng.check_all(
                request_notional=float("nan"), portfolio=portfolio
            )
        assert decision.allowed is False
        assert "nan/inf" in decision.reason

    def test_clean_returns_with_fat_tail_passes(self) -> None:
        """C-α: 全要素 finite な returns_history は通常通り ALLOW される"""
        eng = _make_engine(max_var_usd=100_000.0)
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=_sufficient_returns(worst=-100.0),
        )
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# C-β: kill_switch_bypass_approver allowlist + audit log
# ---------------------------------------------------------------------------

class TestCBetaBypassAudit:

    def test_empty_approver_raises_value_error(self) -> None:
        """C-β: approver='' → ValueError"""
        eng = RiskEngine(config=RiskConfig(
            kill_switch_bypass_approver="",
        ))
        with patch(
            "common_v3.risk.kill_switch.is_active",
            side_effect=ImportError("module fail"),
        ):
            with pytest.raises(ValueError, match="empty or whitespace-only"):
                eng._check_kill_switch()

    def test_whitespace_approver_raises_value_error(self) -> None:
        """C-β: approver='   ' (スペースのみ) → ValueError"""
        eng = RiskEngine(config=RiskConfig(
            kill_switch_bypass_approver="   ",
        ))
        with patch(
            "common_v3.risk.kill_switch.is_active",
            side_effect=ImportError("module fail"),
        ):
            with pytest.raises(ValueError, match="empty or whitespace-only"):
                eng._check_kill_switch()

    def test_approver_not_in_allowlist_raises_value_error(self) -> None:
        """C-β: approver が allowlist 外 → ValueError"""
        eng = RiskEngine(config=RiskConfig(
            kill_switch_bypass_approver="unknown_person",
            kill_switch_bypass_approver_allowlist=frozenset({"yuusakuichio", "admin"}),
        ))
        with patch(
            "common_v3.risk.kill_switch.is_active",
            side_effect=ImportError("module fail"),
        ):
            with pytest.raises(ValueError, match="not in allowlist"):
                eng._check_kill_switch()

    def test_approver_in_allowlist_returns_none(self) -> None:
        """C-β: approver が allowlist 内 → None 返却（check_all 続行）"""
        eng = RiskEngine(config=RiskConfig(
            kill_switch_bypass_approver="yuusakuichio",
            kill_switch_bypass_approver_allowlist=frozenset({"yuusakuichio"}),
        ))
        with patch(
            "common_v3.risk.kill_switch.is_active",
            side_effect=ImportError("module fail"),
        ):
            with patch.object(eng, "_write_bypass_audit"):
                result = eng._check_kill_switch()
        assert result is None

    def test_bypass_writes_to_audit_jsonl(self, tmp_path) -> None:
        """C-β: bypass 使用時 kill_switch_audit.jsonl に追記される"""
        audit_file = tmp_path / "kill_switch_audit.jsonl"

        eng = RiskEngine(config=RiskConfig(
            kill_switch_bypass_approver="yuusakuichio",
            kill_switch_bypass_approver_allowlist=frozenset({"yuusakuichio"}),
        ))

        written: list[dict] = []

        def _fake_write_audit(event, reason, activator, extra=None):
            entry = {"event": event, "reason": reason, "activator": activator}
            if extra:
                entry.update(extra)
            written.append(entry)

        with patch(
            "common_v3.risk.kill_switch.is_active",
            side_effect=ImportError("module fail"),
        ):
            with patch(
                "common_v3.risk.kill_switch._write_audit",
                side_effect=_fake_write_audit,
            ):
                result = eng._check_kill_switch()

        assert result is None
        # audit が 1 件追記されているか
        assert len(written) == 1
        assert written[0]["event"] == "risk_engine_bypass"
        assert written[0]["activator"] == "yuusakuichio"


# ---------------------------------------------------------------------------
# C-γ: _escalate_kill_switch_failure 非同期化
# ---------------------------------------------------------------------------

class TestCGammaAsyncEscalation:

    def test_escalation_does_not_block(self) -> None:
        """C-γ: _escalate_kill_switch_failure が非同期 Thread で呼ばれ、
        200ms ブロック時でも 200ms 未満で呼出側に制御が戻ること。
        スレッド起動コストを考慮して閾値は 180ms（send は 200ms スリープ）。
        """
        call_times: list[float] = []
        send_event = threading.Event()
        _SEND_DELAY = 0.2  # 200ms の遅延

        def _slow_pushover_send(**kwargs):
            time.sleep(_SEND_DELAY)
            call_times.append(time.monotonic())
            send_event.set()

        start = time.monotonic()
        with patch("common.pushover_client.send", side_effect=_slow_pushover_send):
            RiskEngine._escalate_kill_switch_failure("test reason")
        elapsed = time.monotonic() - start

        # 非同期なので elapsed は send_delay(200ms) より十分短いはず
        # スレッド起動コスト込みで 180ms 以内を期待
        assert elapsed < _SEND_DELAY - 0.02, (
            f"escalation blocked for {elapsed:.3f}s — expected < {_SEND_DELAY - 0.02:.3f}s "
            "(should be async via daemon Thread)"
        )

        # バックグラウンドスレッドが実際に送信を完了するのを待つ（最大 1s）
        send_event.wait(timeout=1.0)
        assert len(call_times) == 1, "Pushover send should have been called once in background"

    def test_escalation_import_error_logs_error_not_silent(self) -> None:
        """C-γ: Pushover import 失敗時も silent にならず log.error が呼ばれる"""
        log_errors: list[str] = []

        def _capture_error(msg, *args, **kwargs):
            log_errors.append(msg % args if args else msg)

        with patch("common.pushover_client.send", side_effect=ImportError("no pushover")):
            with patch("common_v3.risk.engine.log.error", side_effect=_capture_error):
                RiskEngine._escalate_kill_switch_failure("test reason")
                # バックグラウンドスレッドの完了を少し待つ
                time.sleep(0.15)

        # log.error が呼ばれていること（silent ではない）
        assert any("Pushover escalation failed" in e for e in log_errors), (
            f"Expected log.error for Pushover failure, got: {log_errors}"
        )


# ---------------------------------------------------------------------------
# C-δ: returns_unit=ratio 時 max_var_ratio で判定
# ---------------------------------------------------------------------------

class TestCDeltaReturnsUnitRatio:

    def test_ratio_unit_with_max_var_ratio_pass(self) -> None:
        """C-δ: unit=ratio, max_var_ratio 以内 → ALLOW"""
        eng = RiskEngine(config=RiskConfig(
            returns_unit="ratio",
            max_var_ratio=0.10,   # 10% 上限
            max_var_usd=5_000.0,  # usd 上限は無関係
        ))
        # VaR = 0.02 * 1.65 = 0.033 < 0.10
        returns = (-0.02,) + tuple(0.0 for _ in range(99))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=returns,
        )
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is True

    def test_ratio_unit_with_max_var_ratio_deny(self) -> None:
        """C-δ: unit=ratio, max_var_ratio 超過 → DENY"""
        eng = RiskEngine(config=RiskConfig(
            returns_unit="ratio",
            max_var_ratio=0.02,   # 2% 上限（厳しい）
            max_var_usd=5_000.0,
        ))
        # VaR = 0.05 * 1.65 = 0.0825 > 0.02
        returns = (-0.05,) + tuple(0.0 for _ in range(99))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=returns,
        )
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is False
        assert "VaR exceeded (ratio)" in decision.reason

    def test_usd_unit_uses_max_var_usd(self) -> None:
        """C-δ: unit=usd, max_var_usd で判定（max_var_ratio は無関係）"""
        eng = RiskEngine(config=RiskConfig(
            returns_unit="usd",
            max_var_usd=200.0,    # 200 USD 上限
        ))
        # VaR = 300 * 1.65 = 495 > 200
        returns = (-300.0,) + tuple(0.0 for _ in range(99))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=returns,
        )
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is False
        assert "VaR exceeded:" in decision.reason
        assert "ratio" not in decision.reason

    def test_ratio_unit_without_max_var_ratio_uses_max_var_usd_backward_compat(self) -> None:
        """C-δ: unit=ratio かつ max_var_ratio=None → max_var_usd で判定（後方互換）"""
        eng = RiskEngine(config=RiskConfig(
            returns_unit="ratio",
            max_var_ratio=None,   # 設定なし
            max_var_usd=100_000.0,  # 緩い上限
        ))
        # VaR = 0.05 * 1.65 = 0.0825 << 100_000 → 通過
        returns = (-0.05,) + tuple(0.0 for _ in range(99))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            returns_history=returns,
        )
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is True

    def test_max_var_ratio_with_usd_unit_raises_value_error(self) -> None:
        """C-δ: returns_unit='usd' で max_var_ratio 設定 → ValueError（unit 不整合）"""
        with pytest.raises(ValueError, match="unit/limit mismatch"):
            RiskConfig(
                returns_unit="usd",
                max_var_ratio=0.05,  # usd unit に ratio limit は不整合
            )
