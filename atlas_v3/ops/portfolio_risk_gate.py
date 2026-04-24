"""atlas_v3/ops/portfolio_risk_gate.py — VIX spike × PortfolioRisk entry halt gate

Redteam aa60 CRITICAL #3:
  VIX が閾値を超えても entry halt が発動せず、PortfolioRisk チェックが
  無制限に新規エントリを許可し続けるバグ。

設計方針:
- `check_entry_allowed(vix, current_entries, limits)` が唯一の公開 gate API
- VIX >= 30 で entry halt（configurable threshold）
- current_entries >= max_concurrent_entries で entry halt
- 両チェックは AND 条件ではなく OR 条件（どちらか一方でも halt）
- グローバル状態は _GateState singleton で管理（プロセス内シングルトン）
- spy_bot.py 側: entry 前に check_entry_allowed を挿入で有効化

Interface 契約:
    GateConfig:  vix_halt_threshold / max_concurrent_entries
    GateDecision: allowed / reason / active_rules
    check_entry_allowed(vix, current_entries, config) -> GateDecision

Usage (将来統合時):
    from atlas_v3.ops.portfolio_risk_gate import (
        GateConfig, check_entry_allowed
    )
    decision = check_entry_allowed(current_vix, len(open_positions), config)
    if not decision.allowed:
        log.warning("Entry halted: %s", decision.reason)
        return
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ── 公開例外 ──────────────────────────────────────────────────────────────────

class PortfolioRiskGateError(RuntimeError):
    """Gate 設定不正またはチェック実行失敗"""


# ── 設定 dataclass ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateConfig:
    """PortfolioRisk Gate の閾値設定。

    frozen=True により実行中の誤書換を物理防止する。
    """
    vix_halt_threshold: float = 30.0
    """VIX がこの値以上で entry halt。デフォルト 30.0 (Standard VIX spike 水準)"""

    max_concurrent_entries: int = 10
    """同時保有エントリ数の上限。超過で entry halt。"""

    vix_warning_threshold: float = 25.0
    """VIX がこの値以上で警告ログを出力する（halt はしない）。"""

    cooldown_secs: float = 300.0
    """VIX halt 発動後、halt が解除されてから再エントリを許可するまでの秒数。"""

    def __post_init__(self) -> None:
        if self.vix_halt_threshold <= 0:
            raise PortfolioRiskGateError(
                f"GateConfig.vix_halt_threshold must be positive, got {self.vix_halt_threshold}"
            )
        if self.max_concurrent_entries <= 0:
            raise PortfolioRiskGateError(
                f"GateConfig.max_concurrent_entries must be positive, got {self.max_concurrent_entries}"
            )
        if self.vix_warning_threshold > self.vix_halt_threshold:
            raise PortfolioRiskGateError(
                f"vix_warning_threshold ({self.vix_warning_threshold}) must be <= "
                f"vix_halt_threshold ({self.vix_halt_threshold})"
            )


DEFAULT_GATE_CONFIG = GateConfig()


# ── Gate 判定結果 ─────────────────────────────────────────────────────────────

@dataclass
class GateDecision:
    """entry_allowed チェックの判定結果。"""
    allowed: bool
    reason: str
    active_rules: list[str] = field(default_factory=list)
    vix: float | None = None
    current_entries: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def halt(
        cls,
        reason: str,
        rules: list[str],
        vix: float | None = None,
        current_entries: int = 0,
    ) -> "GateDecision":
        return cls(
            allowed=False,
            reason=reason,
            active_rules=rules,
            vix=vix,
            current_entries=current_entries,
        )

    @classmethod
    def allow(
        cls,
        vix: float | None = None,
        current_entries: int = 0,
        active_rules: list[str] | None = None,
    ) -> "GateDecision":
        return cls(
            allowed=True,
            reason="ok",
            active_rules=active_rules or [],
            vix=vix,
            current_entries=current_entries,
        )


# ── グローバル Gate 状態 ──────────────────────────────────────────────────────

@dataclass
class _GateState:
    """プロセス内シングルトンの Gate 状態（スレッドセーフ）。"""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _vix_halt_activated_at: float | None = None  # monotonic time
    _vix_halt_cleared_at: float | None = None    # monotonic time
    _last_vix: float | None = None
    _halt_count: int = 0

    def record_vix_halt(self) -> None:
        with self._lock:
            self._vix_halt_activated_at = time.monotonic()
            self._halt_count += 1
            log.error("[PortfolioRiskGate] VIX halt activated (count=%d)", self._halt_count)

    def record_vix_clear(self) -> None:
        with self._lock:
            self._vix_halt_cleared_at = time.monotonic()
            self._vix_halt_activated_at = None
            log.info("[PortfolioRiskGate] VIX halt cleared")

    def is_in_cooldown(self, cooldown_secs: float) -> bool:
        with self._lock:
            if self._vix_halt_cleared_at is None:
                return False
            age = time.monotonic() - self._vix_halt_cleared_at
            return age < cooldown_secs

    def set_last_vix(self, vix: float) -> None:
        with self._lock:
            self._last_vix = vix

    def get_halt_count(self) -> int:
        with self._lock:
            return self._halt_count

    def reset(self) -> None:
        """テスト・リセット用。"""
        with self._lock:
            self._vix_halt_activated_at = None
            self._vix_halt_cleared_at = None
            self._last_vix = None
            self._halt_count = 0


_gate_state = _GateState()


def reset_gate_state() -> None:
    """テスト・システム再起動時のリセット用。本番コードからは呼ばない。"""
    _gate_state.reset()


# ── 公開 API ─────────────────────────────────────────────────────────────────

def check_entry_allowed(
    vix: float,
    current_entries: int,
    config: GateConfig = DEFAULT_GATE_CONFIG,
) -> GateDecision:
    """VIX spike と max_concurrent_entries を評価して entry 許可判定を返す。

    どちらか一方の条件を満たせば halt を返す（OR 条件）。

    Args:
        vix:              現在の VIX 値（CBOE VIX Index 水準）
        current_entries:  現在の同時保有エントリ数
        config:           GateConfig インスタンス（デフォルト: DEFAULT_GATE_CONFIG）

    Returns:
        GateDecision: allowed=True / False と reason

    Raises:
        PortfolioRiskGateError: 入力値が不正な場合
    """
    if vix < 0:
        raise PortfolioRiskGateError(f"vix must be >= 0, got {vix}")
    if current_entries < 0:
        raise PortfolioRiskGateError(f"current_entries must be >= 0, got {current_entries}")

    _gate_state.set_last_vix(vix)
    active_rules: list[str] = []
    halt_reasons: list[str] = []

    # ── Rule 1: VIX spike halt ────────────────────────────────────────────────
    if vix >= config.vix_halt_threshold:
        _gate_state.record_vix_halt()
        active_rules.append("vix_spike_halt")
        halt_reasons.append(
            f"VIX={vix:.1f} >= threshold={config.vix_halt_threshold:.1f}"
        )
        log.error(
            "[PortfolioRiskGate] Entry HALTED: %s",
            halt_reasons[-1],
        )
    elif vix >= config.vix_warning_threshold:
        log.warning(
            "[PortfolioRiskGate] VIX warning: vix=%.1f >= warning=%.1f (halt at %.1f)",
            vix, config.vix_warning_threshold, config.vix_halt_threshold,
        )
        # VIX が halt 閾値を下回った → 前回の halt 状態をクリア
        _gate_state.record_vix_clear()
    else:
        # 完全クリア
        if _gate_state.is_in_cooldown(0):
            _gate_state.record_vix_clear()

    # ── Rule 2: クールダウン（VIX が halt 閾値を下回ってもすぐには再エントリしない） ──
    if "vix_spike_halt" not in active_rules and _gate_state.is_in_cooldown(config.cooldown_secs):
        active_rules.append("vix_cooldown")
        halt_reasons.append(
            f"VIX cooldown active (post-spike wait {config.cooldown_secs:.0f}s)"
        )
        log.warning(
            "[PortfolioRiskGate] Entry HALTED: %s",
            halt_reasons[-1],
        )

    # ── Rule 3: max_concurrent_entries ────────────────────────────────────────
    if current_entries >= config.max_concurrent_entries:
        active_rules.append("max_concurrent_entries")
        halt_reasons.append(
            f"current_entries={current_entries} >= max={config.max_concurrent_entries}"
        )
        log.warning(
            "[PortfolioRiskGate] Entry HALTED: %s",
            halt_reasons[-1],
        )

    # ── 判定 ──────────────────────────────────────────────────────────────────
    if halt_reasons:
        return GateDecision.halt(
            reason=" | ".join(halt_reasons),
            rules=active_rules,
            vix=vix,
            current_entries=current_entries,
        )

    return GateDecision.allow(vix=vix, current_entries=current_entries)


def check_entry_allowed_with_log(
    vix: float,
    current_entries: int,
    config: GateConfig = DEFAULT_GATE_CONFIG,
    *,
    context: str = "",
) -> GateDecision:
    """check_entry_allowed に構造化ログを追加した版（Pushover 連携用）。

    allowed=False 時は log.error で発火するため
    MonitorDaemon の log grep アラートに引っかかる。
    """
    decision = check_entry_allowed(vix, current_entries, config)
    if not decision.allowed:
        log.error(
            "[PortfolioRiskGate][HALT] context=%r vix=%.1f entries=%d rules=%s reason=%s",
            context, vix, current_entries, decision.active_rules, decision.reason,
        )
    else:
        log.debug(
            "[PortfolioRiskGate][ALLOW] context=%r vix=%.1f entries=%d",
            context, vix, current_entries,
        )
    return decision
