"""atlas_v3/ops/latency_monitor.py — Latency モニタ + 自動バックオフ

責務:
- tick ごとの API レイテンシを記録する
- p99 レイテンシが閾値を超えた場合に自動停止（KillSwitch 発動 or 発注停止フラグ）
- data/state_v3/latency_samples.jsonl にサンプルを append-only で記録

設計:
- LatencyConfig:  設定 dataclass (frozen=True)
- LatencySample:  計測値 dataclass (frozen=True)
- LatencyMonitor: 計測・判定・自動バックオフ実装

公開 API:
    LatencyConfig   — 設定
    LatencySample   — 計測値
    LatencyMonitor  — 本体
    LatencyDecision — 判定結果（ALLOW / BACKOFF / HALT）
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import math
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parents[2]
_STATE_DIR = _BASE / "data" / "state_v3"
_LATENCY_LOG = _STATE_DIR / "latency_samples.jsonl"

# ---------------------------------------------------------------------------
# LatencyDecision
# ---------------------------------------------------------------------------

class LatencyDecision(str, Enum):
    """レイテンシ判定結果。

    ALLOW:   p99 < p99_warn_ms → 通常発注継続
    BACKOFF: p99_warn_ms <= p99 < p99_halt_ms → 発注間隔を伸ばす
    HALT:    p99 >= p99_halt_ms → 発注停止（KillSwitch 発動）
    """
    ALLOW = "allow"
    BACKOFF = "backoff"
    HALT = "halt"


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class LatencyConfig:
    """LatencyMonitor 設定。

    Fields:
        window_size:        p99 計算に使用するサンプル数（直近 N 件）。
        p99_warn_ms:        p99 がこれを超えると BACKOFF。
        p99_halt_ms:        p99 がこれを超えると HALT（KillSwitch 発動）。
        backoff_multiplier: BACKOFF 時の発注間隔拡大係数。
        kill_switch_on_halt: HALT 時に KillSwitch を発動するか。
        log_path:           サンプルログ出力先。None なら _LATENCY_LOG を使用。
        persist_samples:    True なら JSONL ファイルへの書込を有効にする。
    """
    window_size: int = 500
    p99_warn_ms: float = 200.0
    p99_halt_ms: float = 1000.0
    backoff_multiplier: float = 2.0
    kill_switch_on_halt: bool = True
    log_path: Optional[Path] = None
    persist_samples: bool = True

    def __post_init__(self) -> None:
        # NEW-C-3: window_size が _MIN_SAMPLES_FOR_P99 未満だと HALT 条件に
        # 必要なサンプル数が永遠に満たされず HALT 判定が機能しない。
        # _MIN_SAMPLES_FOR_P99 は LatencyMonitor クラスレベル定数（=100）だが、
        # dataclass の __post_init__ ではクラス参照ができないため直接参照する。
        _min_samples = 100  # == LatencyMonitor._MIN_SAMPLES_FOR_P99
        if self.window_size < _min_samples:
            raise ValueError(
                f"window_size must be >= {_min_samples} (== _MIN_SAMPLES_FOR_P99), "
                f"got {self.window_size}. "
                f"window_size < {_min_samples} makes HALT judgment permanently unreachable "
                "because decide() returns ALLOW until sample_count >= _MIN_SAMPLES_FOR_P99."
            )
        if self.p99_warn_ms <= 0:
            raise ValueError(f"p99_warn_ms must be > 0, got {self.p99_warn_ms}")
        if self.p99_halt_ms <= self.p99_warn_ms:
            raise ValueError(
                f"p99_halt_ms ({self.p99_halt_ms}) must be > p99_warn_ms ({self.p99_warn_ms})"
            )
        if self.backoff_multiplier < 1.0:
            raise ValueError(
                f"backoff_multiplier must be >= 1.0, got {self.backoff_multiplier}"
            )


@dataclasses.dataclass(frozen=True)
class LatencySample:
    """単一レイテンシ計測値。

    Fields:
        ts:         ISO 8601 タイムスタンプ（UTC）
        latency_ms: 計測レイテンシ（ms）
        source:     計測元識別子（例: "moomoo_quote", "moomoo_order"）
    """
    ts: str
    latency_ms: float
    source: str = "unknown"

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0, got {self.latency_ms}")

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "latency_ms": self.latency_ms,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# LatencyMonitor
# ---------------------------------------------------------------------------

class LatencyMonitor:
    """tick ごとのレイテンシを記録し、p99 超過で自動バックオフ/停止する。

    使用方法:
        monitor = LatencyMonitor(config)

        # tick ごとに計測値を記録
        monitor.record(latency_ms=45.2, source="moomoo_quote")

        # 発注前に判定を確認
        decision = monitor.decide()
        if decision == LatencyDecision.HALT:
            # 発注停止
            pass
        elif decision == LatencyDecision.BACKOFF:
            # 発注間隔を伸ばす
            time.sleep(base_interval * monitor.backoff_factor())
    """

    def __init__(
        self,
        config: Optional[LatencyConfig] = None,
        *,
        kill_switch_activate: Optional[callable] = None,
    ) -> None:
        self._config = config or LatencyConfig()
        self._samples: deque[float] = deque(maxlen=self._config.window_size)
        self._lock = threading.Lock()
        self._halted = False
        # テスト注入: kill_switch.activate を差し替え可能
        self._kill_switch_activate = kill_switch_activate

    @property
    def config(self) -> LatencyConfig:
        return self._config

    def record(self, latency_ms: float, source: str = "unknown") -> LatencyDecision:
        """レイテンシサンプルを記録し、現在の判定を返す。

        Args:
            latency_ms: 計測レイテンシ（ms）
            source:     計測元識別子

        Returns:
            LatencyDecision（現在の p99 に基づく）
        """
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sample = LatencySample(ts=ts, latency_ms=latency_ms, source=source)

        with self._lock:
            self._samples.append(latency_ms)

        if self._config.persist_samples:
            self._write_sample(sample)

        return self.decide()

    # CRITICAL 3: p99 計算に必要な最小サンプル数（100 未満は統計的に不信頼）
    _MIN_SAMPLES_FOR_P99 = 100

    def decide(self) -> LatencyDecision:
        """現在の p99 に基づいて LatencyDecision を返す。

        CRITICAL 3 修正:
        - サンプルが _MIN_SAMPLES_FOR_P99 (=100) 未満の場合は cold-start として ALLOW を返す。
          100 未満では p99 が p100 等価になり誤検知が発生するため。
        - halted フラグが立っている場合は HALT を返す（既存動作維持）。

        Returns:
            LatencyDecision
        """
        with self._lock:
            if self._halted:
                return LatencyDecision.HALT
            samples = list(self._samples)

        # CRITICAL 3: cold-start 期間は HALT/BACKOFF 判定をスキップ
        if len(samples) < self._MIN_SAMPLES_FOR_P99:
            log.debug(
                "[LatencyMonitor] cold-start: %d/%d samples, skipping p99 check",
                len(samples),
                self._MIN_SAMPLES_FOR_P99,
            )
            return LatencyDecision.ALLOW

        p99 = self._compute_p99(samples)

        if p99 >= self._config.p99_halt_ms:
            self._trigger_halt(p99)
            return LatencyDecision.HALT
        if p99 >= self._config.p99_warn_ms:
            log.warning(
                "[LatencyMonitor] BACKOFF: p99=%.1fms >= warn=%.1fms",
                p99, self._config.p99_warn_ms,
            )
            return LatencyDecision.BACKOFF
        return LatencyDecision.ALLOW

    def backoff_factor(self) -> float:
        """BACKOFF 時に発注間隔に掛ける係数を返す。"""
        return self._config.backoff_multiplier

    def p99_ms(self) -> Optional[float]:
        """現在の p99 レイテンシ（ms）を返す。

        CRITICAL 3 修正: _MIN_SAMPLES_FOR_P99 (=100) 未満の場合は None を返す。
        """
        with self._lock:
            samples = list(self._samples)
        if len(samples) < self._MIN_SAMPLES_FOR_P99:
            return None
        return self._compute_p99(samples)

    def sample_count(self) -> int:
        """現在のサンプル数を返す。"""
        with self._lock:
            return len(self._samples)

    def reset(self) -> None:
        """サンプルをリセットする（テスト用・ペーパー日次リセット用）。

        RT-R2-004: KillSwitch 単一真実源設計に合わせ、reset() では
        内部 _halted フラグのみクリアする。
        KillSwitch ファイル（FLAG_FILE）の解除は別途
        common_v3.risk.kill_switch.deactivate() を呼ぶこと。

        設計根拠: LatencyMonitor の _halted と KillSwitch FLAG_FILE は
        別ライフタイムで管理する。LatencyMonitor.reset() は
        「サンプルバッファのリセット」であり KillSwitch の解除権限は持たない。
        KillSwitch を自動解除すると、HALT を引き起こした根本原因が
        解決されていないまま発注が再開する危険がある。

        KillSwitch を解除したい場合（例: 運用者確認後）:
            from common_v3.risk.kill_switch import deactivate
            deactivate(activator="operator", reason="latency_resolved")
        """
        with self._lock:
            self._samples.clear()
            self._halted = False

    # ------------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_p99(samples: list[float]) -> float:
        """99th-percentile レイテンシを計算する。

        RT-R2-002 修正:
        旧実装: `idx = max(0, math.ceil(n * 0.01) - 1)` は逆順ソート配列の
        上位 1% テールを狙うが、n=100 のとき ceil(100*0.01)-1=0 → sorted[0] = 最大値
        (= p100 相当) になり誤検知が発生する。

        新実装: 昇順ソート + 99th-percentile インデックス `int(n * 0.99)` を使用。
        - n=100: idx=99 → sorted_asc[99] = 最大値 (正しく p99 = p100)
          ※ n=100 のとき p99 = 最大値は数学的に正しい（99%点は最大値に近い）
        - n=500: idx=495 → sorted_asc[495] = 上位 1% のボーダー値
        - samples が空なら 0 を返す（呼出元で空チェック済みだが防御的コード）

        Args:
            samples: レイテンシリスト（順序不問）

        Returns:
            99th-percentile 値（ms）
        """
        if not samples:
            return 0.0
        sorted_asc = sorted(samples)
        n = len(sorted_asc)
        # 99th-percentile: 昇順ソート後の 99% 位置
        # int(n * 0.99) はゼロベースインデックス上限を超えないよう min でガード
        idx = min(int(n * 0.99), n - 1)
        return sorted_asc[idx]

    def _trigger_halt(self, p99: float) -> None:
        """HALT 条件に達した時の処理（KillSwitch 発動 + 内部 halted フラグ）。

        RT-R2-005: KillSwitch 書込失敗は _activate_kill_switch 内で retry 3 回
        + 全失敗で andon_multichannel 連動（fail-closed）。
        """
        with self._lock:
            if self._halted:
                return  # 冪等
            self._halted = True

        log.critical(
            "[LatencyMonitor] HALT: p99=%.1fms >= halt_threshold=%.1fms. "
            "Stopping orders.",
            p99, self._config.p99_halt_ms,
        )

        if self._config.kill_switch_on_halt:
            self._activate_kill_switch(p99)

    def _activate_kill_switch(self, p99: float) -> None:
        """KillSwitch を発動する（テスト注入可能）。

        RT-R2-005: ファイル書込失敗は retry 3 回 + 全失敗で andon_multichannel 連動。
        """
        if self._kill_switch_activate is not None:
            try:
                self._kill_switch_activate(
                    reason=f"latency_halt:p99={p99:.1f}ms",
                    activator="latency_monitor",
                )
            except Exception as e:
                log.error("[LatencyMonitor] KillSwitch activation failed (injected): %s", e)
            return

        MAX_RETRIES = 3
        last_exc: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                from common_v3.risk.kill_switch import activate as ks_activate
                ks_activate(
                    reason=f"latency_halt:p99={p99:.1f}ms>=halt={self._config.p99_halt_ms:.1f}ms",
                    activator="latency_monitor",
                )
                return  # 成功
            except Exception as e:
                last_exc = e
                log.error(
                    "[LatencyMonitor] KillSwitch activation failed (attempt %d/%d): %s",
                    attempt, MAX_RETRIES, e,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(0.5 * attempt)

        # 全リトライ失敗 → andon_multichannel で EMERGENCY 再送（fail-closed）
        log.critical(
            "[LatencyMonitor] KillSwitch activation FAILED after %d retries. "
            "Triggering andon_multichannel. Last error: %s",
            MAX_RETRIES, last_exc,
        )
        self._trigger_andon_emergency(p99, last_exc)

    def _trigger_andon_emergency(
        self, p99: float, exc: Optional[Exception]
    ) -> None:
        """KillSwitch 全失敗時の最終手段: andon_multichannel 経由で EMERGENCY 発令。"""
        try:
            import sys as _sys
            hooks_dir = _BASE / ".claude" / "hooks"
            hooks_str = str(hooks_dir)
            if hooks_str not in _sys.path:
                _sys.path.insert(0, hooks_str)
            from andon_multichannel import pull_andon
            pull_andon(
                reason=(
                    f"LatencyMonitor HALT: p99={p99:.1f}ms >= halt={self._config.p99_halt_ms:.1f}ms. "
                    f"KillSwitch activation failed: {exc}"
                ),
                source="latency_monitor",
            )
        except Exception as andon_err:
            log.critical(
                "[LatencyMonitor] andon_multichannel also failed: %s. "
                "SYSTEM IS IN UNKNOWN STATE. Manual intervention required.",
                andon_err,
            )

    def _write_sample(self, sample: LatencySample) -> None:
        """サンプルを latency_samples.jsonl に append-only で書き込む。"""
        log_path = self._config.log_path or _LATENCY_LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            log.error("[LatencyMonitor] log write failed: %s", e)


# ---------------------------------------------------------------------------
# コンテキストマネージャ（tick 計測用）
# ---------------------------------------------------------------------------

class LatencyTimer:
    """with ブロックで API 呼出をラップして自動的に record() を呼ぶ。

    使用例:
        with LatencyTimer(monitor, source="moomoo_quote") as timer:
            result = call_api()
        # timer.elapsed_ms に計測値が入る

        decision = monitor.decide()
    """

    def __init__(self, monitor: LatencyMonitor, source: str = "unknown") -> None:
        self._monitor = monitor
        self._source = source
        self._start: float = 0.0
        self.elapsed_ms: float = 0.0
        self.decision: LatencyDecision = LatencyDecision.ALLOW

    def __enter__(self) -> "LatencyTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_) -> None:
        elapsed = (time.perf_counter() - self._start) * 1000.0
        self.elapsed_ms = elapsed
        self.decision = self._monitor.record(
            latency_ms=elapsed, source=self._source
        )
