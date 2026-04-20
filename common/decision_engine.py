#!/usr/bin/env python3
"""
common/decision_engine.py — Sora Lab Phase 1 Core Decision Engine

フレームワーク統合:
  - TEM  (Threat and Error Management): 脅威検知・先手対応
  - FORDEC (航空意思決定): 構造的判断ループ
  - Bayesian: 信頼度スコアリング（事前確率 × 尤度）
  - START Triage: 緊急度分類 (CRITICAL/HIGH/MEDIUM)
  - SBAR形式通知: Situation/Background/Assessment/Recommendation

4層アーキテクチャ:
  SENSOR → THREAT DETECTION → DECISION → EXECUTION

判断優先度:
  CRITICAL : <1秒で判断・即実行（Pushover 429, Kill Switch, etc.）
  HIGH     : <5秒で判断（Heartbeat stale, 連続損失, etc.）
  MEDIUM   : <30秒で判断（VIX spike, パラメータ逸脱, etc.）

使い方:
    from common.decision_engine import DecisionEngine, ThreatLevel, Decision

    engine = DecisionEngine()
    decision = engine.evaluate(
        threat_id="heartbeat_stale",
        context={"component": "chronos_agent", "age_sec": 180}
    )
    if decision.action != "ignore":
        engine.execute(decision)

ログ:
    data/decision_log.jsonl  — 全判断の追記型ログ

API:
    DecisionEngine.evaluate(threat_id, context) -> Decision
    DecisionEngine.execute(decision) -> bool
    DecisionEngine.sbar(decision) -> str  # SBAR形式通知文
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

# ── パス設定 ──────────────────────────────────────────────────────────────────
_BASE_DIR = Path(os.environ.get("SORA_TRADING_DIR", Path(__file__).resolve().parents[1]))
_DECISION_LOG_PATH = _BASE_DIR / "data" / "decision_log.jsonl"

# ── ロガー ────────────────────────────────────────────────────────────────────
log = logging.getLogger("decision_engine")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] decision_engine: %(message)s"
    ))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Enums / データクラス
# ─────────────────────────────────────────────────────────────────────────────

class ThreatLevel(str, Enum):
    """START Triageに準拠した緊急度分類"""
    CRITICAL = "CRITICAL"   # <1秒判断: システム崩壊・資金危険
    HIGH     = "HIGH"       # <5秒判断: コンポーネント停止・損失連続
    MEDIUM   = "MEDIUM"     # <30秒判断: パラメータ逸脱・環境変化
    LOW      = "LOW"        # バックグラウンド処理


class DecisionPhase(str, Enum):
    """FORDEC フェーズ"""
    FACTS       = "F"  # 事実確認
    OPTIONS     = "O"  # 選択肢列挙
    RISKS       = "R"  # リスク評価
    DECISION    = "D"  # 判断確定
    EXECUTION   = "E"  # 実行
    CHECK       = "C"  # 結果確認


@dataclass
class ThreatContext:
    """SENSOR層が収集した脅威コンテキスト"""
    threat_id: str                          # 脅威識別子 (例: "heartbeat_stale")
    raw_data: dict[str, Any]                # 生データ
    detected_at: float = field(default_factory=time.time)
    source: str = "unknown"                 # 検知元 (例: "heartbeat_monitor")


@dataclass
class Decision:
    """DECISION層が生成した判断オブジェクト"""
    threat_id: str
    level: ThreatLevel
    action: str                             # "restart" / "notify" / "halt" / "ignore" / "escalate"
    confidence: float                       # 0.0〜1.0 (Bayesian信頼度)
    reasoning: str                          # 判断根拠 (FORDEC O/R段階)
    sbar_text: str                          # SBAR形式通知文
    context: dict[str, Any] = field(default_factory=dict)
    decided_at: float = field(default_factory=time.time)
    phase: DecisionPhase = DecisionPhase.DECISION
    # 実行後に付与
    executed: bool = False
    executed_at: float | None = None
    execution_result: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# THREAT DETECTION 層 — 脅威ルール定義
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ThreatRule:
    """脅威検知ルール（TEM原則: 事前定義で判断を機械化）"""
    threat_id: str
    level: ThreatLevel
    prior: float           # 事前確率 (Bayesian prior): この脅威が実際に危険である基礎確率
    likelihood_fn: Callable[[dict[str, Any]], float]  # 尤度関数: context → 0.0〜1.0
    action_fn: Callable[[dict[str, Any], float], str] # 行動選択: (context, confidence) → action
    sbar_fn: Callable[[dict[str, Any], str], str]     # SBAR文生成
    description: str = ""


def _bayesian_confidence(prior: float, likelihood: float) -> float:
    """
    Bayesian信頼度スコアリング。
    P(danger | evidence) = P(evidence | danger) * P(danger) / P(evidence)
    簡略化: confidence = prior * likelihood / (prior * likelihood + (1-prior) * (1-likelihood))
    0割防止付き。結果は 0.0〜1.0。
    """
    p = prior * likelihood
    q = (1.0 - prior) * (1.0 - likelihood)
    denom = p + q
    if denom < 1e-9:
        return 0.0
    return min(1.0, max(0.0, p / denom))


def _sbar(
    situation: str,
    background: str,
    assessment: str,
    recommendation: str,
) -> str:
    """SBAR形式通知文を生成する。"""
    return (
        f"[S] {situation}\n"
        f"[B] {background}\n"
        f"[A] {assessment}\n"
        f"[R] {recommendation}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 組み込みルール定義
# ─────────────────────────────────────────────────────────────────────────────

def _build_default_rules() -> dict[str, ThreatRule]:
    """
    デフォルト脅威ルール群。
    研究レポート (judgment_logic_research_20260420.md) の4ケースを網羅。
    """
    rules: dict[str, ThreatRule] = {}

    # ── CRITICAL: Pushover 429 / ban 検知 ────────────────────────────────────
    def _pushover_429_likelihood(ctx: dict) -> float:
        consecutive = ctx.get("consecutive_429", 0)
        # 1回でも429が出れば確度高い (Swiss Cheese: 1枚目の穴)
        return min(1.0, consecutive * 0.5 + 0.3)

    def _pushover_429_action(ctx: dict, confidence: float) -> str:
        if confidence >= 0.5:
            return "switch_pushover_token"
        return "notify_fallback"

    def _pushover_429_sbar(ctx: dict, action: str) -> str:
        consecutive = ctx.get("consecutive_429", 0)
        return _sbar(
            situation=f"Pushover 429エラー {consecutive}回連続",
            background="通知チャンネルの過負荷またはBAN。Swiss Cheese第1層破綻",
            assessment=f"信頼度に基づき'{action}'を実行。代替トークン切り替えで復旧可能",
            recommendation="1) 代替トークン即切り替え 2) ntfy.sh fallback確認 3) 30分後自動再開",
        )

    rules["pushover_429"] = ThreatRule(
        threat_id="pushover_429",
        level=ThreatLevel.CRITICAL,
        prior=0.9,  # 429が来たら高確率で本当の制限
        likelihood_fn=_pushover_429_likelihood,
        action_fn=_pushover_429_action,
        sbar_fn=_pushover_429_sbar,
        description="Pushover 429/BAN → 即時代替チャンネル切替 (Swiss Cheese原則)",
    )

    # ── HIGH: Heartbeat stale ─────────────────────────────────────────────────
    def _heartbeat_stale_likelihood(ctx: dict) -> float:
        age_sec = ctx.get("age_sec", 0.0)
        if age_sec == float("inf"):
            return 1.0
        # 120秒閾値超過 → 超過量に応じて尤度上昇
        # 240秒(2倍)で 0.9、360秒(3倍)で 1.0
        excess = max(0.0, age_sec - 120)
        return min(1.0, 0.5 + excess / 480)

    def _heartbeat_stale_action(ctx: dict, confidence: float) -> str:
        attempt = ctx.get("restart_attempt", 0)
        if attempt >= 3:
            return "escalate"       # 3回失敗 → 手動介入
        if confidence >= 0.6:
            return "restart"
        return "notify"

    def _heartbeat_stale_sbar(ctx: dict, action: str) -> str:
        comp = ctx.get("component", "unknown")
        age = ctx.get("age_sec", 0)
        age_str = f"{age:.0f}秒" if age != float("inf") else "∞（ファイルなし）"
        attempt = ctx.get("restart_attempt", 0)
        return _sbar(
            situation=f"コンポーネント '{comp}' Heartbeat停止 (経過: {age_str})",
            background=f"最終pulse後{age_str}経過。TEM原則: 1回失敗で即エスカレート（旧3回待ちを廃止）",
            assessment=f"再起動試行{attempt}回目。信頼度に基づき'{action}'を選択",
            recommendation=(
                f"1) '{action}'を即実行 "
                f"2) 成功確認（次pulse 60秒以内） "
                f"3) 失敗時はescalate→手動介入"
            ),
        )

    rules["heartbeat_stale"] = ThreatRule(
        threat_id="heartbeat_stale",
        level=ThreatLevel.HIGH,
        prior=0.75,
        likelihood_fn=_heartbeat_stale_likelihood,
        action_fn=_heartbeat_stale_action,
        sbar_fn=_heartbeat_stale_sbar,
        description="Heartbeat stale → TEM原則で1回失敗即エスカレート (旧3回待ち廃止)",
    )

    # ── HIGH: 戦術連続損失 (FORDEC) ──────────────────────────────────────────
    def _consecutive_loss_likelihood(ctx: dict) -> float:
        consecutive = ctx.get("consecutive_losses", 0)
        loss_pct = ctx.get("loss_pct", 0.0)   # 1トレードの損失%
        # 3連続損失で尤度急上昇、損失率が大きいほど追加加重
        base = min(1.0, consecutive * 0.25)
        size_bonus = min(0.3, loss_pct / 10.0)
        return min(1.0, base + size_bonus)

    def _consecutive_loss_action(ctx: dict, confidence: float) -> str:
        consecutive = ctx.get("consecutive_losses", 0)
        if consecutive >= 5 or confidence >= 0.85:
            return "halt_new_entries"   # 新規エントリ停止
        if consecutive >= 3 or confidence >= 0.6:
            return "reduce_size"        # サイズ縮小
        return "notify"

    def _consecutive_loss_sbar(ctx: dict, action: str) -> str:
        consecutive = ctx.get("consecutive_losses", 0)
        strategy = ctx.get("strategy", "unknown")
        loss_pct = ctx.get("loss_pct", 0.0)
        return _sbar(
            situation=f"戦術'{strategy}'連続損失 {consecutive}回 (直近損失率: {loss_pct:.1f}%)",
            background="FORDEC原則: 事実→選択肢→リスク評価→判断。感情排除・機械的判断",
            assessment=f"Bayesian信頼度ベースで'{action}'を選択。環境変化の可能性を評価",
            recommendation=(
                f"1) '{action}'を即実行 "
                f"2) strategy_selectorに再評価要求 "
                f"3) 現在の市場環境（VIX/IVR）を確認"
            ),
        )

    rules["consecutive_loss"] = ThreatRule(
        threat_id="consecutive_loss",
        level=ThreatLevel.HIGH,
        prior=0.6,
        likelihood_fn=_consecutive_loss_likelihood,
        action_fn=_consecutive_loss_action,
        sbar_fn=_consecutive_loss_sbar,
        description="連続損失 → FORDEC + Bayesian で halt/reduce/notify を機械判断",
    )

    # ── MEDIUM: VIX急騰 ───────────────────────────────────────────────────────
    def _vix_spike_likelihood(ctx: dict) -> float:
        vix_current = ctx.get("vix_current", 20.0)
        vix_prev = ctx.get("vix_prev", 20.0)
        if vix_prev <= 0:
            return 0.0
        change_pct = (vix_current - vix_prev) / vix_prev * 100
        # 10%上昇で尤度0.5、20%で0.9
        return min(1.0, max(0.0, change_pct / 20.0))

    def _vix_spike_action(ctx: dict, confidence: float) -> str:
        vix_current = ctx.get("vix_current", 20.0)
        if vix_current >= 40 or confidence >= 0.8:
            return "switch_to_defensive"  # 防衛戦術（straddle_buy等）
        if confidence >= 0.5:
            return "reduce_delta_exposure"
        return "notify"

    def _vix_spike_sbar(ctx: dict, action: str) -> str:
        vix_current = ctx.get("vix_current", 0)
        vix_prev = ctx.get("vix_prev", 0)
        change_pct = (vix_current - vix_prev) / max(vix_prev, 0.01) * 100
        return _sbar(
            situation=f"VIX急騰: {vix_prev:.1f} → {vix_current:.1f} (+{change_pct:.1f}%)",
            background="START Triage RED: 市場ストレス急上昇。既存ポジションのデルタリスク増大",
            assessment=f"VIX水準・変化率から'{action}'を選択。環境変化を戦術に即反映",
            recommendation=(
                f"1) '{action}'を即実行 "
                f"2) strategy_selectorに環境再評価要求 "
                f"3) 既存ポジションのデルタ確認"
            ),
        )

    rules["vix_spike"] = ThreatRule(
        threat_id="vix_spike",
        level=ThreatLevel.MEDIUM,
        prior=0.55,
        likelihood_fn=_vix_spike_likelihood,
        action_fn=_vix_spike_action,
        sbar_fn=_vix_spike_sbar,
        description="VIX急騰 → START Triageで防衛戦術切替を機械判断",
    )

    # ── MEDIUM: ポートフォリオDD逸脱 ─────────────────────────────────────────
    def _dd_breach_likelihood(ctx: dict) -> float:
        dd_pct = ctx.get("dd_pct", 0.0)
        dd_limit = ctx.get("dd_limit", 20.0)
        if dd_limit <= 0:
            return 0.0
        ratio = dd_pct / dd_limit
        return min(1.0, max(0.0, ratio))

    def _dd_breach_action(ctx: dict, confidence: float) -> str:
        dd_pct = ctx.get("dd_pct", 0.0)
        dd_limit = ctx.get("dd_limit", 20.0)
        ratio = dd_pct / max(dd_limit, 0.01)
        if ratio >= 1.0 or confidence >= 0.9:
            return "halt"            # DD上限突破 → 全停止
        if ratio >= 0.8 or confidence >= 0.7:
            return "halt_new_entries"
        return "notify"

    def _dd_breach_sbar(ctx: dict, action: str) -> str:
        dd_pct = ctx.get("dd_pct", 0.0)
        dd_limit = ctx.get("dd_limit", 20.0)
        firm = ctx.get("firm", "unknown")
        return _sbar(
            situation=f"[{firm}] DD {dd_pct:.1f}% / 上限 {dd_limit:.1f}%",
            background="DDトラッカー警戒レベル。MFFU規約違反リスク",
            assessment=f"DD比率から'{action}'を機械判断。規約違反は口座失効",
            recommendation=(
                f"1) '{action}'を即実行 "
                f"2) kill_switch確認 "
                f"3) 手動でポジション整理を検討"
            ),
        )

    rules["dd_breach"] = ThreatRule(
        threat_id="dd_breach",
        level=ThreatLevel.MEDIUM,
        prior=0.7,
        likelihood_fn=_dd_breach_likelihood,
        action_fn=_dd_breach_action,
        sbar_fn=_dd_breach_sbar,
        description="DD上限接近/突破 → MFFU規約保護のためhalt機械判断",
    )

    # ── CRITICAL: Kill Switch トリガー ───────────────────────────────────────
    def _kill_switch_likelihood(ctx: dict) -> float:
        triggered = ctx.get("triggered", False)
        reason = ctx.get("reason", "")
        if triggered:
            return 1.0
        # reason が設定されているだけで高尤度
        return 0.8 if reason else 0.0

    def _kill_switch_action(ctx: dict, confidence: float) -> str:
        return "halt"  # Kill Switch は常にhalt

    def _kill_switch_sbar(ctx: dict, action: str) -> str:
        reason = ctx.get("reason", "不明")
        source = ctx.get("source", "unknown")
        return _sbar(
            situation=f"Kill Switch トリガー: {reason}",
            background=f"検知元: {source}。SEC Rule 15c3-5 準拠の即時停止機構",
            assessment="Kill Switchは無条件halt。信頼度に関係なく実行",
            recommendation=(
                "1) 全発注即キャンセル "
                "2) 手動確認まで新規発注停止 "
                "3) Pushover priority=2で通知"
            ),
        )

    rules["kill_switch"] = ThreatRule(
        threat_id="kill_switch",
        level=ThreatLevel.CRITICAL,
        prior=1.0,  # Kill Switchは常に信頼
        likelihood_fn=_kill_switch_likelihood,
        action_fn=_kill_switch_action,
        sbar_fn=_kill_switch_sbar,
        description="Kill Switch → SEC 15c3-5準拠の無条件halt",
    )

    # ── HIGH: コンポーネント未応答（プロセス死亡）──────────────────────────
    def _process_dead_likelihood(ctx: dict) -> float:
        pid_exists = ctx.get("pid_exists", True)
        return 0.0 if pid_exists else 1.0

    def _process_dead_action(ctx: dict, confidence: float) -> str:
        attempt = ctx.get("restart_attempt", 0)
        if attempt >= 3:
            return "escalate"
        return "restart"

    def _process_dead_sbar(ctx: dict, action: str) -> str:
        comp = ctx.get("component", "unknown")
        pid = ctx.get("pid", "N/A")
        attempt = ctx.get("restart_attempt", 0)
        return _sbar(
            situation=f"コンポーネント '{comp}' プロセス死亡 (PID: {pid})",
            background=f"プロセス未検出。再起動試行{attempt}回目",
            assessment=f"'{action}'を即実行",
            recommendation=(
                f"1) '{action}'実行 "
                f"2) エラーログ確認 "
                f"3) 3回失敗で手動介入"
            ),
        )

    rules["process_dead"] = ThreatRule(
        threat_id="process_dead",
        level=ThreatLevel.HIGH,
        prior=0.9,
        likelihood_fn=_process_dead_likelihood,
        action_fn=_process_dead_action,
        sbar_fn=_process_dead_sbar,
        description="プロセス死亡 → 即restart、3回失敗でescalate",
    )

    return rules


# ─────────────────────────────────────────────────────────────────────────────
# DecisionEngine 本体
# ─────────────────────────────────────────────────────────────────────────────

class DecisionEngine:
    """
    Sora Lab Phase 1 Core Decision Engine

    TEM + FORDEC + Bayesian 統合クラス。
    4層: SENSOR / THREAT DETECTION / DECISION / EXECUTION

    スレッドセーフ: ログ書き込みはファイル append 方式（atomic行単位）。
    状態保持なし（ステートレス設計）: 呼び出し側が context に状態を渡す。
    """

    def __init__(
        self,
        custom_rules: dict[str, ThreatRule] | None = None,
        log_path: Path | None = None,
        pushover_send: Callable | None = None,
        dry_run: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        custom_rules  : 追加/上書きするルール辞書。Noneはデフォルトルールのみ使用
        log_path      : 判断ログ出力先。Noneは data/decision_log.jsonl
        pushover_send : Pushover送信関数。Noneは common.pushover_client.send を使用
        dry_run       : True の場合、EXECUTION層はログのみ（実際の副作用を実行しない）
        """
        self._rules: dict[str, ThreatRule] = _build_default_rules()
        if custom_rules:
            self._rules.update(custom_rules)

        self._log_path = log_path or _DECISION_LOG_PATH
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        self._dry_run = dry_run

        # Pushover送信関数（テスト差し替え可能）
        if pushover_send is not None:
            self._pushover_send = pushover_send
        else:
            try:
                from common.pushover_client import send as _ps
                self._pushover_send = _ps
            except ImportError:
                self._pushover_send = self._pushover_fallback

        log.info(
            "[DecisionEngine] 初期化完了: rules=%d, dry_run=%s",
            len(self._rules), dry_run
        )

    # ── LAYER 1: SENSOR ──────────────────────────────────────────────────────

    def build_context(
        self,
        threat_id: str,
        raw_data: dict[str, Any],
        source: str = "unknown",
    ) -> ThreatContext:
        """
        SENSOR層: 生データを ThreatContext に正規化する。

        Parameters
        ----------
        threat_id : 脅威識別子 (例: "heartbeat_stale")
        raw_data  : 生データ辞書
        source    : 検知元 (例: "heartbeat_monitor", "atlas_agent")
        """
        return ThreatContext(
            threat_id=threat_id,
            raw_data=raw_data,
            detected_at=time.time(),
            source=source,
        )

    # ── LAYER 2: THREAT DETECTION ─────────────────────────────────────────────

    def detect(self, ctx: ThreatContext) -> tuple[ThreatLevel, float, str]:
        """
        THREAT DETECTION層: Bayesian信頼度と脅威レベルを算出する。

        TEM原則: ルールを事前定義して判断を機械化。
        判断時間目標: CRITICAL <0.1秒, HIGH <0.5秒, MEDIUM <2秒

        Returns
        -------
        (level, confidence, rule_description)
        """
        rule = self._rules.get(ctx.threat_id)
        if rule is None:
            log.warning("[DETECT] unknown threat_id=%s — defaulting to MEDIUM/0.5", ctx.threat_id)
            return ThreatLevel.MEDIUM, 0.5, "Unknown threat — default assessment"

        likelihood = rule.likelihood_fn(ctx.raw_data)
        confidence = _bayesian_confidence(rule.prior, likelihood)

        log.debug(
            "[DETECT] threat=%s level=%s prior=%.2f likelihood=%.2f confidence=%.2f",
            ctx.threat_id, rule.level, rule.prior, likelihood, confidence,
        )
        return rule.level, confidence, rule.description

    # ── LAYER 3: DECISION ─────────────────────────────────────────────────────

    def decide(
        self,
        ctx: ThreatContext,
        level: ThreatLevel,
        confidence: float,
    ) -> Decision:
        """
        DECISION層: FORDEC O/R/D フェーズを経て行動を確定する。

        FORDEC:
          F (Facts)   — ThreatContextに格納済み
          O (Options) — ルールの action_fn で選択肢評価
          R (Risks)   — confidenceで重みづけ
          D (Decision) — action確定
          E/C         — execute()で処理
        """
        rule = self._rules.get(ctx.threat_id)

        if rule is None:
            # 未知の脅威 → デフォルト判断
            action = "notify"
            sbar_text = _sbar(
                situation=f"未知の脅威検知: {ctx.threat_id}",
                background=f"ルール未定義。source={ctx.source}",
                assessment="デフォルト通知のみ実行",
                recommendation="ルール定義を追加してください",
            )
            return Decision(
                threat_id=ctx.threat_id,
                level=level,
                action=action,
                confidence=confidence,
                reasoning="Unknown threat — no rule defined",
                sbar_text=sbar_text,
                context=ctx.raw_data,
            )

        # FORDEC O: 行動選択
        action = rule.action_fn(ctx.raw_data, confidence)

        # FORDEC R: リスク評価 → reasoning
        reasoning = (
            f"[TEM] {rule.description} | "
            f"[FORDEC] prior={rule.prior:.2f} confidence={confidence:.2f} → action={action}"
        )

        # SBAR形式通知文生成
        sbar_text = rule.sbar_fn(ctx.raw_data, action)

        decision = Decision(
            threat_id=ctx.threat_id,
            level=level,
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            sbar_text=sbar_text,
            context=ctx.raw_data,
            phase=DecisionPhase.DECISION,
        )

        log.info(
            "[DECIDE] threat=%s level=%s action=%s confidence=%.2f",
            ctx.threat_id, level.value, action, confidence,
        )
        return decision

    # ── 統合エントリポイント ──────────────────────────────────────────────────

    def evaluate(
        self,
        threat_id: str,
        context: dict[str, Any],
        source: str = "unknown",
    ) -> Decision:
        """
        SENSOR → THREAT DETECTION → DECISION を一括実行する。

        判断時間目標:
          CRITICAL: <1秒   (Pushover 429, Kill Switch)
          HIGH    : <5秒   (Heartbeat stale, 連続損失)
          MEDIUM  : <30秒  (VIX spike, DD逸脱)

        Parameters
        ----------
        threat_id : 脅威識別子
        context   : 生データ辞書
        source    : 検知元

        Returns
        -------
        Decision オブジェクト
        """
        t0 = time.time()

        # SENSOR
        threat_ctx = self.build_context(threat_id, context, source)

        # THREAT DETECTION
        level, confidence, desc = self.detect(threat_ctx)

        # DECISION
        decision = self.decide(threat_ctx, level, confidence)

        elapsed_ms = (time.time() - t0) * 1000
        log.info(
            "[EVALUATE] threat=%s elapsed=%.1fms level=%s action=%s confidence=%.2f",
            threat_id, elapsed_ms, level.value, decision.action, confidence,
        )

        # 判断ログ記録
        self._append_log(decision, elapsed_ms)

        return decision

    # ── LAYER 4: EXECUTION ────────────────────────────────────────────────────

    def execute(
        self,
        decision: Decision,
        executors: dict[str, Callable[[dict[str, Any]], bool]] | None = None,
    ) -> bool:
        """
        EXECUTION層: 判断を実行し、結果をログに記録する。

        FORDEC E/C フェーズ。

        Parameters
        ----------
        decision  : evaluate()が返したDecisionオブジェクト
        executors : action → 実行関数のマッピング（差し替え可能）
                    例: {"restart": lambda ctx: kickstart(ctx["component"])}

        Returns
        -------
        bool: 実行成功 / 失敗
        """
        if self._dry_run:
            log.info("[EXECUTE][DRY_RUN] action=%s threat=%s", decision.action, decision.threat_id)
            decision.executed = True
            decision.executed_at = time.time()
            decision.execution_result = "dry_run"
            self._append_log(decision, 0)
            return True

        action = decision.action
        ctx = decision.context

        # 通知は必ず実行（action に関係なく）
        if decision.level in (ThreatLevel.CRITICAL, ThreatLevel.HIGH):
            priority = 1 if decision.level == ThreatLevel.HIGH else 2
            self._pushover_send(
                title=f"[SYS][{decision.level.value}] {decision.threat_id}",
                message=decision.sbar_text[:1024],
                priority=priority,
            )
        elif decision.level == ThreatLevel.MEDIUM:
            self._pushover_send(
                title=f"[SYS][MEDIUM] {decision.threat_id}",
                message=decision.sbar_text[:1024],
                priority=0,
            )

        # カスタム executor があれば実行
        success = False
        if executors and action in executors:
            try:
                success = bool(executors[action](ctx))
                log.info(
                    "[EXECUTE] action=%s success=%s threat=%s",
                    action, success, decision.threat_id,
                )
            except Exception as exc:
                log.error("[EXECUTE] exception: action=%s err=%s", action, exc)
                success = False
        elif action == "ignore":
            success = True
        elif action == "notify":
            success = True  # 通知は上で実行済み
        else:
            log.info(
                "[EXECUTE] no executor for action=%s — notification only",
                action,
            )
            success = True

        decision.executed = True
        decision.executed_at = time.time()
        decision.execution_result = "success" if success else "failed"
        decision.phase = DecisionPhase.EXECUTION

        self._append_log(decision, 0)
        return success

    # ── SBAR 通知文生成 (外部から呼び出し可能) ───────────────────────────────

    def sbar(self, decision: Decision) -> str:
        """Decision から SBAR形式通知文を返す。"""
        return decision.sbar_text

    # ── ユーティリティ ────────────────────────────────────────────────────────

    def register_rule(self, rule: ThreatRule) -> None:
        """実行時にルールを追加/上書きする。"""
        self._rules[rule.threat_id] = rule
        log.info("[RULE] registered: %s (level=%s)", rule.threat_id, rule.level.value)

    def list_rules(self) -> list[str]:
        """登録済みルール識別子一覧を返す。"""
        return sorted(self._rules.keys())

    def get_rule(self, threat_id: str) -> ThreatRule | None:
        """ルールを取得する。"""
        return self._rules.get(threat_id)

    # ── 内部: ログ書き込み ────────────────────────────────────────────────────

    def _append_log(self, decision: Decision, elapsed_ms: float) -> None:
        """
        data/decision_log.jsonl に1行追記する。
        JSONL形式: 1行1JSON。追記型（既存行を変更しない）。
        """
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "threat_id": decision.threat_id,
            "level": decision.level.value,
            "action": decision.action,
            "confidence": round(decision.confidence, 4),
            "reasoning": decision.reasoning,
            "executed": decision.executed,
            "execution_result": decision.execution_result,
            "elapsed_ms": round(elapsed_ms, 2),
            "context_keys": list(decision.context.keys()),
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("[LOG] write error: %s", e)

    # ── 内部: Pushoverフォールバック ──────────────────────────────────────────

    @staticmethod
    def _pushover_fallback(title: str, message: str, priority: int = 0) -> bool:
        """Pushoverクライアントが利用不可の場合のフォールバック（ログのみ）。"""
        log.warning(
            "[PUSHOVER_FALLBACK] title=%s priority=%d message_len=%d",
            title, priority, len(message),
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# シングルトンファクトリ（atlas_agent / chronos_agent から共有利用）
# ─────────────────────────────────────────────────────────────────────────────

_singleton: DecisionEngine | None = None


def get_engine(dry_run: bool = False) -> DecisionEngine:
    """
    プロセス内シングルトンを返す。

    atlas_agent / chronos_agent / sora_heartbeat_monitor から
    同一プロセス内で共有利用する場合に使用する。
    """
    global _singleton
    if _singleton is None:
        _singleton = DecisionEngine(dry_run=dry_run)
    return _singleton


def reset_engine() -> None:
    """テスト用: シングルトンをリセットする。"""
    global _singleton
    _singleton = None
