#!/usr/bin/env python3
"""
tests/test_decision_engine.py — DecisionEngine Phase 1 テスト

テスト対象:
  - ThreatLevel 分類
  - Bayesian 信頼度スコアリング
  - 組み込みルール全件 (6ルール)
  - SBAR形式通知文フォーマット
  - 判断ログ (JSONL) 書き込み
  - カスタムルール登録
  - シングルトン
  - dry_run モード
  - executor 差し替え
  - 未知 threat_id のデフォルト処理
  - 判断時間 (elapsed_ms) の上限チェック
  - evaluate → execute の一連フロー

最低15ケース保証。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# プロジェクトルートを sys.path に追加
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.decision_engine import (
    DecisionEngine,
    DecisionPhase,
    ThreatLevel,
    ThreatRule,
    Decision,
    _bayesian_confidence,
    get_engine,
    reset_engine,
)


# ─────────────────────────────────────────────────────────────────────────────
# フィクスチャ
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_log(tmp_path: Path) -> Path:
    """一時ログファイルパス"""
    return tmp_path / "decision_log.jsonl"


@pytest.fixture
def mock_pushover() -> MagicMock:
    """Pushover送信のモック"""
    return MagicMock(return_value=True)


@pytest.fixture
def engine(tmp_log: Path, mock_pushover: MagicMock) -> DecisionEngine:
    """テスト用 DecisionEngine"""
    return DecisionEngine(log_path=tmp_log, pushover_send=mock_pushover)


@pytest.fixture
def dry_engine(tmp_log: Path, mock_pushover: MagicMock) -> DecisionEngine:
    """dry_run モードの DecisionEngine"""
    return DecisionEngine(log_path=tmp_log, pushover_send=mock_pushover, dry_run=True)


# ─────────────────────────────────────────────────────────────────────────────
# ケース 1: Bayesian 信頼度スコアリングの数値正確性
# ─────────────────────────────────────────────────────────────────────────────

class TestBayesianConfidence:
    def test_high_prior_high_likelihood_yields_high_confidence(self):
        """事前確率・尤度が共に高いと信頼度が高くなる"""
        confidence = _bayesian_confidence(prior=0.9, likelihood=0.9)
        assert confidence > 0.95, f"expected >0.95, got {confidence}"

    def test_low_prior_low_likelihood_yields_low_confidence(self):
        """事前確率・尤度が共に低いと信頼度が低くなる"""
        confidence = _bayesian_confidence(prior=0.1, likelihood=0.1)
        assert confidence < 0.1, f"expected <0.1, got {confidence}"

    def test_boundary_zero_prior(self):
        """事前確率ゼロ → 信頼度ゼロ"""
        confidence = _bayesian_confidence(prior=0.0, likelihood=0.8)
        assert confidence == pytest.approx(0.0, abs=1e-6)

    def test_boundary_one_prior_one_likelihood(self):
        """両方1.0 → 信頼度1.0"""
        confidence = _bayesian_confidence(prior=1.0, likelihood=1.0)
        assert confidence == pytest.approx(1.0, abs=1e-6)

    def test_output_range_always_0_to_1(self):
        """出力は常に 0.0〜1.0"""
        import random
        random.seed(42)
        for _ in range(50):
            p = random.random()
            l = random.random()
            c = _bayesian_confidence(p, l)
            assert 0.0 <= c <= 1.0, f"out of range: prior={p} likelihood={l} confidence={c}"


# ─────────────────────────────────────────────────────────────────────────────
# ケース 2: ThreatLevel 分類
# ─────────────────────────────────────────────────────────────────────────────

class TestThreatLevels:
    def test_pushover_429_is_critical(self, engine: DecisionEngine):
        """Pushover 429 は CRITICAL レベル"""
        decision = engine.evaluate(
            "pushover_429",
            {"consecutive_429": 2},
            source="test",
        )
        assert decision.level == ThreatLevel.CRITICAL

    def test_heartbeat_stale_is_high(self, engine: DecisionEngine):
        """Heartbeat stale は HIGH レベル"""
        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "chronos_agent", "age_sec": 300},
            source="test",
        )
        assert decision.level == ThreatLevel.HIGH

    def test_vix_spike_is_medium(self, engine: DecisionEngine):
        """VIX 急騰は MEDIUM レベル"""
        decision = engine.evaluate(
            "vix_spike",
            {"vix_current": 30.0, "vix_prev": 20.0},
            source="test",
        )
        assert decision.level == ThreatLevel.MEDIUM

    def test_kill_switch_is_critical(self, engine: DecisionEngine):
        """Kill Switch は CRITICAL レベル"""
        decision = engine.evaluate(
            "kill_switch",
            {"triggered": True, "reason": "DD exceeded", "source": "dd_tracker"},
            source="test",
        )
        assert decision.level == ThreatLevel.CRITICAL


# ─────────────────────────────────────────────────────────────────────────────
# ケース 3: 各ルールのアクション判断
# ─────────────────────────────────────────────────────────────────────────────

class TestRuleActions:
    def test_heartbeat_stale_restart_on_first_attempt(self, engine: DecisionEngine):
        """Heartbeat stale 初回 → restart"""
        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "atlas_agent", "age_sec": 300, "restart_attempt": 0},
        )
        assert decision.action == "restart"

    def test_heartbeat_stale_escalate_after_3_attempts(self, engine: DecisionEngine):
        """Heartbeat stale 3回失敗後 → escalate"""
        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "atlas_agent", "age_sec": 600, "restart_attempt": 3},
        )
        assert decision.action == "escalate"

    def test_consecutive_loss_halt_on_5_losses(self, engine: DecisionEngine):
        """5連敗 → halt_new_entries"""
        decision = engine.evaluate(
            "consecutive_loss",
            {"consecutive_losses": 5, "loss_pct": 2.0, "strategy": "cs_sell"},
        )
        assert decision.action == "halt_new_entries"

    def test_consecutive_loss_notify_on_1_loss(self, engine: DecisionEngine):
        """1連敗 → notify"""
        decision = engine.evaluate(
            "consecutive_loss",
            {"consecutive_losses": 1, "loss_pct": 0.5, "strategy": "cs_sell"},
        )
        assert decision.action == "notify"

    def test_dd_breach_halt_at_limit(self, engine: DecisionEngine):
        """DD 上限突破 → halt"""
        decision = engine.evaluate(
            "dd_breach",
            {"dd_pct": 20.0, "dd_limit": 20.0, "firm": "MFFU"},
        )
        assert decision.action == "halt"

    def test_dd_breach_halt_new_entries_at_50pct(self, engine: DecisionEngine):
        """
        DD 50% (dd_pct=10, dd_limit=20) → halt_new_entries
        prior=0.7 × likelihood=0.5 → confidence=0.70 >= 0.7 → halt_new_entries
        """
        decision = engine.evaluate(
            "dd_breach",
            {"dd_pct": 10.0, "dd_limit": 20.0, "firm": "MFFU"},
        )
        assert decision.action == "halt_new_entries"

    def test_dd_breach_notify_at_25pct(self, engine: DecisionEngine):
        """
        DD 25% (dd_pct=5, dd_limit=20) → notify
        prior=0.7 × likelihood=0.25 → confidence < 0.7
        """
        decision = engine.evaluate(
            "dd_breach",
            {"dd_pct": 5.0, "dd_limit": 20.0, "firm": "MFFU"},
        )
        assert decision.action == "notify"

    def test_kill_switch_always_halt(self, engine: DecisionEngine):
        """Kill Switch は常に halt"""
        decision = engine.evaluate(
            "kill_switch",
            {"triggered": True, "reason": "test", "source": "test"},
        )
        assert decision.action == "halt"

    def test_vix_spike_switch_defensive_above_40(self, engine: DecisionEngine):
        """VIX 40超 → switch_to_defensive"""
        decision = engine.evaluate(
            "vix_spike",
            {"vix_current": 45.0, "vix_prev": 20.0},
        )
        assert decision.action == "switch_to_defensive"

    def test_process_dead_restart(self, engine: DecisionEngine):
        """プロセス死亡 → restart"""
        decision = engine.evaluate(
            "process_dead",
            {"component": "chronos_agent", "pid": 12345, "pid_exists": False, "restart_attempt": 0},
        )
        assert decision.action == "restart"


# ─────────────────────────────────────────────────────────────────────────────
# ケース 4: SBAR 形式フォーマット
# ─────────────────────────────────────────────────────────────────────────────

class TestSBARFormat:
    def test_sbar_contains_four_sections(self, engine: DecisionEngine):
        """SBAR文は [S] [B] [A] [R] の4セクションを含む"""
        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "chronos_agent", "age_sec": 200, "restart_attempt": 0},
        )
        sbar = engine.sbar(decision)
        assert "[S]" in sbar
        assert "[B]" in sbar
        assert "[A]" in sbar
        assert "[R]" in sbar

    def test_sbar_mentions_component(self, engine: DecisionEngine):
        """SBAR文はコンポーネント名を含む"""
        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "atlas_agent", "age_sec": 200, "restart_attempt": 0},
        )
        sbar = engine.sbar(decision)
        assert "atlas_agent" in sbar


# ─────────────────────────────────────────────────────────────────────────────
# ケース 5: 判断ログ (JSONL) 書き込み
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionLog:
    def test_log_written_after_evaluate(self, engine: DecisionEngine, tmp_log: Path):
        """evaluate() 後にログファイルが存在する"""
        engine.evaluate("heartbeat_stale", {"component": "test", "age_sec": 150})
        assert tmp_log.exists(), "decision_log.jsonl が作成されていない"

    def test_log_is_valid_jsonl(self, engine: DecisionEngine, tmp_log: Path):
        """ログが有効な JSONL形式"""
        engine.evaluate("heartbeat_stale", {"component": "test", "age_sec": 150})
        engine.evaluate("vix_spike", {"vix_current": 25.0, "vix_prev": 20.0})
        lines = tmp_log.read_text().strip().splitlines()
        assert len(lines) >= 2
        for line in lines:
            obj = json.loads(line)  # パース失敗でテスト失敗
            assert "threat_id" in obj
            assert "level" in obj
            assert "action" in obj
            assert "confidence" in obj
            assert "ts" in obj

    def test_log_appends_not_overwrites(self, engine: DecisionEngine, tmp_log: Path):
        """ログは上書きではなく追記"""
        engine.evaluate("heartbeat_stale", {"component": "test", "age_sec": 150})
        engine.evaluate("vix_spike", {"vix_current": 25.0, "vix_prev": 20.0})
        lines = tmp_log.read_text().strip().splitlines()
        assert len(lines) == 2  # 2件追記されている

    def test_log_confidence_is_float(self, engine: DecisionEngine, tmp_log: Path):
        """ログの confidence は float"""
        engine.evaluate("heartbeat_stale", {"component": "test", "age_sec": 150})
        obj = json.loads(tmp_log.read_text().strip().splitlines()[0])
        assert isinstance(obj["confidence"], float)


# ─────────────────────────────────────────────────────────────────────────────
# ケース 6: カスタムルール登録
# ─────────────────────────────────────────────────────────────────────────────

class TestCustomRule:
    def test_register_custom_rule(self, engine: DecisionEngine):
        """カスタムルールを登録して evaluate できる"""
        custom_rule = ThreatRule(
            threat_id="custom_test",
            level=ThreatLevel.LOW,
            prior=0.5,
            likelihood_fn=lambda ctx: 1.0,
            action_fn=lambda ctx, conf: "custom_action",
            sbar_fn=lambda ctx, action: "[S] test\n[B] bg\n[A] asses\n[R] rec",
            description="カスタムテストルール",
        )
        engine.register_rule(custom_rule)
        assert "custom_test" in engine.list_rules()
        decision = engine.evaluate("custom_test", {})
        assert decision.action == "custom_action"

    def test_list_rules_returns_all_defaults(self, engine: DecisionEngine):
        """デフォルトルールが全件登録されている"""
        rules = engine.list_rules()
        for expected in [
            "pushover_429", "heartbeat_stale", "consecutive_loss",
            "vix_spike", "dd_breach", "kill_switch", "process_dead",
        ]:
            assert expected in rules, f"ルール '{expected}' が未登録"


# ─────────────────────────────────────────────────────────────────────────────
# ケース 7: 未知 threat_id のデフォルト処理
# ─────────────────────────────────────────────────────────────────────────────

class TestUnknownThreat:
    def test_unknown_threat_returns_decision(self, engine: DecisionEngine):
        """未知の threat_id でも Decision が返る（クラッシュしない）"""
        decision = engine.evaluate("totally_unknown_xyz", {"foo": "bar"})
        assert isinstance(decision, Decision)
        assert decision.action == "notify"

    def test_unknown_threat_confidence_is_default(self, engine: DecisionEngine):
        """未知の threat_id の信頼度はデフォルト 0.5"""
        decision = engine.evaluate("totally_unknown_xyz", {})
        assert decision.confidence == pytest.approx(0.5, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# ケース 8: dry_run モード
# ─────────────────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_execute_logs_but_no_side_effects(
        self, dry_engine: DecisionEngine, mock_pushover: MagicMock
    ):
        """dry_run では executor が呼ばれない（通知も実行されない）"""
        called = []
        executors = {"restart": lambda ctx: called.append("restarted") or True}
        decision = dry_engine.evaluate(
            "heartbeat_stale",
            {"component": "test", "age_sec": 300, "restart_attempt": 0},
        )
        dry_engine.execute(decision, executors=executors)
        assert len(called) == 0, "dry_run で executor が実行された"
        # dry_run では Pushover も呼ばない
        mock_pushover.assert_not_called()

    def test_dry_run_decision_marked_executed(self, dry_engine: DecisionEngine):
        """dry_run でも executed=True が設定される"""
        decision = dry_engine.evaluate("heartbeat_stale", {"component": "x", "age_sec": 300})
        dry_engine.execute(decision)
        assert decision.executed is True
        assert decision.execution_result == "dry_run"


# ─────────────────────────────────────────────────────────────────────────────
# ケース 9: executor 差し替えと execute フロー
# ─────────────────────────────────────────────────────────────────────────────

class TestExecute:
    def test_execute_calls_correct_executor(
        self, engine: DecisionEngine, mock_pushover: MagicMock
    ):
        """execute() が action に対応する executor を呼ぶ"""
        called = {}

        def mock_restart(ctx: dict) -> bool:
            called["component"] = ctx.get("component")
            return True

        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "atlas_agent", "age_sec": 300, "restart_attempt": 0},
        )
        assert decision.action == "restart"

        result = engine.execute(decision, executors={"restart": mock_restart})
        assert result is True
        assert called.get("component") == "atlas_agent"

    def test_execute_pushover_called_for_high_level(
        self, engine: DecisionEngine, mock_pushover: MagicMock
    ):
        """HIGH レベルの execute では Pushover が呼ばれる"""
        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "test", "age_sec": 300, "restart_attempt": 0},
        )
        engine.execute(decision)
        assert mock_pushover.called

    def test_execute_pushover_priority_for_critical(
        self, engine: DecisionEngine, mock_pushover: MagicMock
    ):
        """CRITICAL レベルの execute では priority=2 で Pushover が呼ばれる"""
        decision = engine.evaluate(
            "kill_switch",
            {"triggered": True, "reason": "test", "source": "test"},
        )
        engine.execute(decision)
        call_kwargs = mock_pushover.call_args
        priority = call_kwargs.kwargs.get("priority") or call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
        # priority は keyword argument で渡される
        if call_kwargs.kwargs:
            assert call_kwargs.kwargs.get("priority") == 2
        else:
            # positional の場合
            assert call_kwargs.args[2] == 2

    def test_execute_marks_decision_as_executed(
        self, engine: DecisionEngine, mock_pushover: MagicMock
    ):
        """execute() 後に decision.executed=True"""
        decision = engine.evaluate("notify_only", {"foo": "bar"})
        engine.execute(decision)
        assert decision.executed is True
        assert decision.executed_at is not None


# ─────────────────────────────────────────────────────────────────────────────
# ケース 10: 判断時間上限チェック (elapsed_ms)
# ─────────────────────────────────────────────────────────────────────────────

class TestElapsedTime:
    def test_critical_decision_under_100ms(self, engine: DecisionEngine, tmp_log: Path):
        """CRITICAL 判断は 100ms 以内に完了する"""
        t0 = time.time()
        engine.evaluate(
            "pushover_429",
            {"consecutive_429": 3},
        )
        elapsed_ms = (time.time() - t0) * 1000
        assert elapsed_ms < 100, f"CRITICAL 判断が 100ms を超えた: {elapsed_ms:.1f}ms"

    def test_high_decision_under_500ms(self, engine: DecisionEngine):
        """HIGH 判断は 500ms 以内に完了する"""
        t0 = time.time()
        engine.evaluate(
            "heartbeat_stale",
            {"component": "chronos_agent", "age_sec": 300},
        )
        elapsed_ms = (time.time() - t0) * 1000
        assert elapsed_ms < 500, f"HIGH 判断が 500ms を超えた: {elapsed_ms:.1f}ms"

    def test_elapsed_ms_logged(self, engine: DecisionEngine, tmp_log: Path):
        """elapsed_ms がログに記録される"""
        engine.evaluate("vix_spike", {"vix_current": 30.0, "vix_prev": 20.0})
        obj = json.loads(tmp_log.read_text().strip().splitlines()[0])
        assert "elapsed_ms" in obj
        assert isinstance(obj["elapsed_ms"], float)


# ─────────────────────────────────────────────────────────────────────────────
# ケース 11: シングルトン
# ─────────────────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_engine_returns_same_instance(self):
        """get_engine() は同じインスタンスを返す"""
        reset_engine()
        e1 = get_engine()
        e2 = get_engine()
        assert e1 is e2

    def test_reset_engine_creates_new_instance(self):
        """reset_engine() 後は新しいインスタンス"""
        reset_engine()
        e1 = get_engine()
        reset_engine()
        e2 = get_engine()
        assert e1 is not e2

    def teardown_method(self):
        reset_engine()


# ─────────────────────────────────────────────────────────────────────────────
# ケース 12: evaluate → execute の完全フロー
# ─────────────────────────────────────────────────────────────────────────────

class TestFullFlow:
    def test_full_flow_heartbeat_stale(
        self, engine: DecisionEngine, tmp_log: Path, mock_pushover: MagicMock
    ):
        """
        heartbeat_stale の完全フロー:
        evaluate → decision.action == restart → execute → success
        """
        restart_called = []
        executors = {"restart": lambda ctx: restart_called.append(ctx) or True}

        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "chronos_agent", "age_sec": 300, "restart_attempt": 0},
            source="sora_heartbeat_monitor",
        )

        assert decision.level == ThreatLevel.HIGH
        assert decision.action == "restart"
        assert decision.confidence > 0.5

        result = engine.execute(decision, executors=executors)

        assert result is True
        assert len(restart_called) == 1
        assert restart_called[0]["component"] == "chronos_agent"
        assert decision.executed is True
        assert decision.execution_result == "success"

        # ログ確認
        lines = tmp_log.read_text().strip().splitlines()
        # evaluate で1行 + execute で1行
        assert len(lines) >= 1
        log_obj = json.loads(lines[-1])
        assert log_obj["threat_id"] == "heartbeat_stale"

    def test_full_flow_kill_switch(
        self, engine: DecisionEngine, mock_pushover: MagicMock
    ):
        """
        kill_switch の完全フロー:
        evaluate → decision.action == halt → execute → Pushover priority=2
        """
        halt_called = []
        executors = {"halt": lambda ctx: halt_called.append(True) or True}

        decision = engine.evaluate(
            "kill_switch",
            {"triggered": True, "reason": "DD breach 20%", "source": "dd_tracker"},
        )
        assert decision.action == "halt"
        assert decision.level == ThreatLevel.CRITICAL

        engine.execute(decision, executors=executors)
        assert len(halt_called) == 1
        assert mock_pushover.called


# ─────────────────────────────────────────────────────────────────────────────
# ケース 13: ThreatRule の likelihood_fn / action_fn の境界値
# ─────────────────────────────────────────────────────────────────────────────

class TestRuleBoundaries:
    def test_heartbeat_missing_file_gives_max_likelihood(self, engine: DecisionEngine):
        """ファイルなし (age_sec=inf) は最大信頼度"""
        decision = engine.evaluate(
            "heartbeat_stale",
            {"component": "test", "age_sec": float("inf"), "restart_attempt": 0},
        )
        assert decision.confidence > 0.9

    def test_pushover_zero_consecutive_gives_low_action_confidence(self, engine: DecisionEngine):
        """consecutive_429=0 では低信頼度"""
        decision = engine.evaluate("pushover_429", {"consecutive_429": 0})
        # prior=0.9 で likelihood が低くても confidence は中程度になりうる
        # action は notify_fallback（confidence < 0.5）
        assert decision.confidence < 0.8  # 0回429でも prior が高いため完全ゼロにはならない

    def test_consecutive_loss_reduce_size_at_3_losses(self, engine: DecisionEngine):
        """3連敗 → reduce_size"""
        decision = engine.evaluate(
            "consecutive_loss",
            {"consecutive_losses": 3, "loss_pct": 1.0, "strategy": "ic_sell"},
        )
        # 3連敗 × prior=0.6 → confidence >= 0.6 → reduce_size
        assert decision.action in ("reduce_size", "halt_new_entries")


# ─────────────────────────────────────────────────────────────────────────────
# ケース 14: reasoning フィールドに TEM / FORDEC キーワードが含まれる
# ─────────────────────────────────────────────────────────────────────────────

class TestReasoning:
    def test_reasoning_contains_tem(self, engine: DecisionEngine):
        """reasoning に [TEM] が含まれる"""
        decision = engine.evaluate("heartbeat_stale", {"component": "test", "age_sec": 200})
        assert "[TEM]" in decision.reasoning

    def test_reasoning_contains_fordec(self, engine: DecisionEngine):
        """reasoning に [FORDEC] が含まれる"""
        decision = engine.evaluate("consecutive_loss", {"consecutive_losses": 2, "loss_pct": 1.0})
        assert "[FORDEC]" in decision.reasoning

    def test_reasoning_contains_confidence(self, engine: DecisionEngine):
        """reasoning に confidence の数値が含まれる"""
        decision = engine.evaluate("vix_spike", {"vix_current": 30.0, "vix_prev": 20.0})
        assert "confidence=" in decision.reasoning


# ─────────────────────────────────────────────────────────────────────────────
# ケース 15: build_context / detect / decide を個別に呼び出せる
# ─────────────────────────────────────────────────────────────────────────────

class TestLayerByLayer:
    def test_build_context_returns_threat_context(self, engine: DecisionEngine):
        """build_context が ThreatContext を返す"""
        from common.decision_engine import ThreatContext
        ctx = engine.build_context("heartbeat_stale", {"age_sec": 200}, source="test")
        assert isinstance(ctx, ThreatContext)
        assert ctx.threat_id == "heartbeat_stale"
        assert ctx.source == "test"

    def test_detect_returns_level_and_confidence(self, engine: DecisionEngine):
        """detect が (level, confidence, desc) を返す"""
        from common.decision_engine import ThreatContext
        ctx = engine.build_context("heartbeat_stale", {"age_sec": 200})
        level, confidence, desc = engine.detect(ctx)
        assert isinstance(level, ThreatLevel)
        assert 0.0 <= confidence <= 1.0
        assert isinstance(desc, str)

    def test_decide_returns_decision(self, engine: DecisionEngine):
        """decide が Decision を返す"""
        from common.decision_engine import ThreatContext
        ctx = engine.build_context("heartbeat_stale", {"age_sec": 200, "restart_attempt": 0})
        level, confidence, _ = engine.detect(ctx)
        decision = engine.decide(ctx, level, confidence)
        assert isinstance(decision, Decision)
        assert decision.threat_id == "heartbeat_stale"

    def test_get_rule_returns_correct_rule(self, engine: DecisionEngine):
        """get_rule が正しいルールを返す"""
        rule = engine.get_rule("kill_switch")
        assert rule is not None
        assert rule.level == ThreatLevel.CRITICAL
        assert rule.prior == pytest.approx(1.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
