"""atlas_v3/ops/monitor.py — 24h 監視 + オンコール体制 daemon

責務:
- Atlas Paper Bot の健全性を 24h 監視する daemon
- Pushover escalation 3 経路連動（priority 0/1/2）
- common_v3/risk/kill_switch.py と連動（異常時 KillSwitch 発動）
- data/ops/monitor_state.jsonl に監視ログを append-only で記録

設計:
- MonitorConfig: 監視設定 dataclass (frozen=True)
- HealthCheck: チェック結果 dataclass (frozen=True)
- MonitorDaemon: daemon 本体（stop() で停止可能・テスト容易性確保）
- MetricProvider: Bot 実データ注入用 Protocol（RT-R2-001 修正）
- 外部 I/O は最小限（Pushover 送信 + ファイル書込のみ）
- CC ≤ 10 per method

公開 API:
    MonitorConfig            — 監視設定
    HealthCheck              — チェック結果
    MonitorDaemon            — daemon 本体
    AlertLevel               — アラートレベル Enum
    MetricProvider           — Bot 実データ注入 Protocol
    bootstrap_paper_monitor  — NEW-C-1: YAML ロードから daemon 起動まで一貫 entry point

KillSwitch 復旧手順 (CRIT-R4-4):
    kill_switch が ARMED 状態で daemon が連続失敗ループする場合は以下を実行:

    # 1. kill_switch_recover.py で自動復旧試行（scripts/kill_switch_recover.py --probe）
    python3 scripts/kill_switch_recover.py --probe

    # 2. 手動解除（ゆうさくさん確認後）
    python3 -c "from common_v3.risk.kill_switch import deactivate; print(deactivate(activator='yuusaku_manual', reason='manual_reset_after_check'))"

    # 3. 連続失敗後の待機挙動
    - max_consecutive_failures 回連続失敗 → EMERGENCY 発令 + kill_switch 発動 + 3秒待機 + 自動 probe
    - probe 成功（kill_switch 解除 or 問題消滅）→ consecutive_failures reset で再開
    - probe 失敗 → andon_multichannel EMERGENCY + daemon 停止
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import math
import os
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parents[2]
_STATE_DIR = _BASE / "data" / "state_v3"
_MONITOR_LOG = _STATE_DIR / "monitor_state.jsonl"

# ---------------------------------------------------------------------------
# AlertLevel
# ---------------------------------------------------------------------------

class AlertLevel(str, Enum):
    """Pushover アラートレベル。

    INFO:     通常動作ログ（Pushover 送信なし）
    WARNING:  軽微異常（Pushover priority=0・サイレント）
    CRITICAL: 重大異常（Pushover priority=1・音声通知）
    EMERGENCY: 緊急停止必要（Pushover priority=2・繰り返し通知 + KillSwitch 発動）
    """
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


# ---------------------------------------------------------------------------
# MetricProvider Protocol（RT-R2-001: Bot 実データ注入用）
# ---------------------------------------------------------------------------

@runtime_checkable
class MetricProvider(Protocol):
    """Bot 実データを MonitorDaemon に注入するための Protocol。

    実装例:
        class BotMetricProvider:
            def get_metrics(self) -> dict:
                return {
                    "pnl_day_usd": bot.daily_pnl,
                    "drawdown_pct": bot.drawdown_pct,
                    "latency_ms": bot.last_latency_ms,
                }

    MonitorConfig.metric_provider に渡すことで、_run_loop が
    デフォルト値（pnl=0/dd=0/lat=0）ではなく実データで check_once() を呼ぶ。

    RT-R2-001 修正: metric_provider=None のままでは監視が実質無効になるため、
    明示的に callable を渡さない限り CRITICAL ログで警告する。
    """
    def get_metrics(self) -> dict:
        """現在の Bot メトリクスを返す。

        Returns dict with keys:
            pnl_day_usd (float): 当日損益（USD）
            drawdown_pct (float): 現在ドローダウン率（0.0–1.0）
            latency_ms (float): 直近 API レイテンシ（ms）
        """
        ...


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class MonitorConfig:
    """監視 daemon 設定。

    Fields:
        check_interval_secs:    チェック間隔（秒）。デフォルト 15.0 seconds。
                                HIGH-R4-2: YAML の monitor.check_interval_secs で override 可能。
        heartbeat_timeout_secs: heartbeat 無応答でアラートを上げるまでの秒数。
        max_latency_ms:         API 応答レイテンシ上限（ms）。超過で WARNING。
        daily_loss_usd:         日次損失アラート閾値（USD・負値）。
                                RT-R2-003: 0 不可（0 だと threshold*1.5=0 → 微損で EMERGENCY）
        drawdown_pct:           ドローダウンアラート閾値（0.0–1.0）。
        pushover_enabled:       Pushover 送信を有効にするか。
        kill_switch_on_emergency: EMERGENCY 時に KillSwitch を発動するか。
        kill_switch_on_drawdown_breach: CRITICAL ドローダウン超過時も KillSwitch 発動するか（H-2）。
        heartbeat_file:         daemon 生存確認用 heartbeat ファイルパス。None なら touch しない（H-3）。
        heartbeat_write_interval_secs: heartbeat ファイルを touch する間隔（秒）。デフォルト 30（H-3）。
        max_consecutive_failures: check_once の連続失敗でdaemon停止する回数（H-8）。
        log_path:               監視ログ出力先。None なら _MONITOR_LOG を使用。
        log_max_bytes:          HIGH-R4-2: ログファイルのサイズ上限（bytes）。0 なら rotation なし。
        log_backup_count:       HIGH-R4-2: ローテーション後に保持するバックアップ数。
        metric_provider:        Bot 実データ注入用 callable。None なら _run_loop で全ゼロ値使用
                                （RT-R2-001 修正: None の場合は起動時に CRITICAL ログ警告）。
        drawdown_breach_count:  HIGH-R4-3: drawdown KillSwitch 発動に必要な連続超過回数（hysteresis）。
                                デフォルト 3。途中で閾値以下に戻れば counter reset。
    """
    check_interval_secs: float = 15.0  # NEW-H-2: 60→15 でflash crash検知遅延を排除
    heartbeat_timeout_secs: float = 300.0
    max_latency_ms: float = 500.0
    daily_loss_usd: float = -400.0
    drawdown_pct: float = 0.12
    pushover_enabled: bool = True
    kill_switch_on_emergency: bool = True
    kill_switch_on_drawdown_breach: bool = True  # NEW-H-4: safe-by-default（旧 False → True）
    heartbeat_file: Optional[Path] = None
    heartbeat_write_interval_secs: float = 30.0
    max_consecutive_failures: int = 3
    log_path: Optional[Path] = None
    log_max_bytes: int = 10 * 1024 * 1024  # HIGH-R4-2: 10MB デフォルト
    log_backup_count: int = 5              # HIGH-R4-2: 5世代バックアップ
    metric_provider: Optional[Callable[[], dict]] = None
    drawdown_breach_count: int = 1         # HIGH-R4-3: hysteresis（連続 N 回でKillSwitch）
                                           # デフォルト 1 = 後方互換（1回超過で CRITICAL）
                                           # 誤発火防止のため 3 以上を推奨:
                                           #   MonitorConfig(drawdown_breach_count=3)
    probe_on_consecutive_failure: bool = False  # CRIT-R4-4: 連続失敗後の自動 probe（opt-in）
                                               # True にすると連続失敗後に _probe_recovery() を呼ぶ
                                               # デフォルト False = 後方互換（即停止）

    # C4 fix: Schmitt Trigger hysteresis（振動脆弱修正）
    # hysteresis_upper: counter 増加トリガー上閾値（drawdown がここを超えると carry-counter++）
    #   None の場合は drawdown_pct を使用（後方互換）
    # hysteresis_lower: counter 減少トリガー下閾値（drawdown がここを下回ると carry-counter--）
    #   None の場合は drawdown_pct * 0.8 を使用（上閾値の 80% = 20% margin）
    # 例: drawdown_pct=0.12, hysteresis_upper=None(→0.12), hysteresis_lower=None(→0.096)
    #   → drawdown > 0.12 で counter++
    #   → drawdown < 0.096 で counter--
    #   → 0.096–0.12 の帯域では counter 保持（Flash Crash 型振動でカウンターが永遠にリセットされない）
    # デフォルト None = 後方互換（下閾値 = 上閾値 × 0.8 を自動適用）
    hysteresis_upper: Optional[float] = None  # None → drawdown_pct（後方互換）
    hysteresis_lower: Optional[float] = None  # None → drawdown_pct * 0.8 (Schmitt margin)

    def __post_init__(self) -> None:
        if self.check_interval_secs <= 0:
            raise ValueError(f"check_interval_secs must be > 0, got {self.check_interval_secs}")
        if self.heartbeat_timeout_secs <= 0:
            raise ValueError(f"heartbeat_timeout_secs must be > 0, got {self.heartbeat_timeout_secs}")
        # RT-R2-003: daily_loss_usd=0 許容禁止（threshold*1.5=0 → 微損で EMERGENCY 発火）
        if self.daily_loss_usd >= 0:
            raise ValueError(
                f"daily_loss_usd must be negative (< 0), got {self.daily_loss_usd}. "
                "daily_loss_usd=0 is forbidden: threshold*1.5=0 would trigger EMERGENCY on any loss."
            )
        if not (0.0 < self.drawdown_pct <= 1.0):
            raise ValueError(f"drawdown_pct must be in (0.0, 1.0], got {self.drawdown_pct}")
        if self.max_consecutive_failures < 1:
            raise ValueError(
                f"max_consecutive_failures must be >= 1, got {self.max_consecutive_failures}"
            )
        if self.drawdown_breach_count < 1:
            raise ValueError(
                f"drawdown_breach_count must be >= 1, got {self.drawdown_breach_count}"
            )
        # HIGH-R6-1 fix: Schmitt Trigger 閾値バリデーション（片側 None の auto-fill 後に逆転検査）
        # 旧実装: both-not-None の場合のみ逆転チェック → 片側指定で auto-fill 後に逆転する可能性
        # 例: upper=0.05, lower=None → auto-fill lower=0.05*0.8=0.04 → OK
        #     upper=None(→drawdown_pct=0.12), lower=0.2 → upper(0.12) < lower(0.2) → 逆転
        # 新実装: None を auto-fill した後の実効値で逆転チェックを行う

        # 実効値を計算（None の場合のデフォルト値を適用）
        effective_upper = self.hysteresis_upper if self.hysteresis_upper is not None else self.drawdown_pct
        effective_lower = self.hysteresis_lower if self.hysteresis_lower is not None else self.drawdown_pct * 0.8

        # 個別値の範囲チェック（None でない場合のみ）
        if self.hysteresis_upper is not None and not (0.0 < self.hysteresis_upper <= 1.0):
            raise ValueError(
                f"hysteresis_upper must be in (0.0, 1.0], got {self.hysteresis_upper}"
            )
        if self.hysteresis_lower is not None and not (0.0 < self.hysteresis_lower <= 1.0):
            raise ValueError(
                f"hysteresis_lower must be in (0.0, 1.0], got {self.hysteresis_lower}"
            )

        # HIGH-R6-1 fix: auto-fill 後の実効値で逆転チェック
        # （片側 None でも auto-fill 後に upper <= lower となる設定を弾く）
        if effective_lower >= effective_upper:
            raise ValueError(
                f"Schmitt Trigger: effective hysteresis_lower ({effective_lower}) must be < "
                f"effective hysteresis_upper ({effective_upper}). "
                f"(hysteresis_upper={self.hysteresis_upper!r} → effective {effective_upper}, "
                f"hysteresis_lower={self.hysteresis_lower!r} → effective {effective_lower}). "
                "HIGH-R6-1 fix: auto-filled None values are also validated for inversion."
            )


@dataclasses.dataclass(frozen=True)
class HealthCheck:
    """単一チェック結果。

    Fields:
        ts:           ISO 8601 タイムスタンプ（UTC）
        level:        アラートレベル
        check_name:   チェック名（例: "heartbeat", "latency", "daily_loss"）
        message:      詳細メッセージ
        value:        計測値（数値の場合のみ設定）
        threshold:    閾値（None なら閾値チェックなし）
    """
    ts: str
    level: AlertLevel
    check_name: str
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None

    def is_ok(self) -> bool:
        return self.level == AlertLevel.INFO

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "level": self.level.value,
            "check_name": self.check_name,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
        }


# ---------------------------------------------------------------------------
# MonitorDaemon
# ---------------------------------------------------------------------------

class MonitorDaemon:
    """Atlas Paper Bot 24h 監視 daemon。

    使用方法:
        daemon = MonitorDaemon(config)
        daemon.start()
        # ... 必要な時間だけ動作 ...
        daemon.stop()

    テスト容易性:
        - check_once() で単発チェックを実行できる（daemon 起動不要）
        - _send_alert() は Pushover 送信をラップ（モック容易）
        - _activate_kill_switch() は kill_switch.activate() をラップ（モック容易）

    RT-R2-001: metric_provider を MonitorConfig に設定することで、
    _run_loop が Bot 実データを使って check_once() を呼び出す。
    metric_provider=None の場合は全ゼロ値（監視実質無効）になるため
    start() 時に CRITICAL ログ警告を出す。
    """

    def __init__(
        self,
        config: Optional[MonitorConfig] = None,
        *,
        pushover_send: Optional[Callable] = None,
        allow_default_config: bool = False,
    ) -> None:
        # NEW-C-1: config=None はデフォルトで禁止（allow_default_config=True で明示 opt-in 時のみ許可）
        # 裸デフォルト fallback を使うと YAML single source of truth が機能しないため。
        # bootstrap_paper_monitor() 経由で起動することで設定は必ず YAML から供給される。
        if config is None:
            if not allow_default_config:
                raise ValueError(
                    "MonitorDaemon requires an explicit MonitorConfig. "
                    "Use bootstrap_paper_monitor() for production startup, or pass "
                    "MonitorConfig(...) explicitly. "
                    "To allow bare default (tests/debug only): "
                    "MonitorDaemon(allow_default_config=True)."
                )
            config = MonitorConfig()
        self._config = config
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_heartbeat: float = time.monotonic()
        self._lock = threading.Lock()
        # テスト注入: pushover 送信関数を差し替え可能
        self._pushover_send = pushover_send
        # HIGH-R4-3: drawdown hysteresis カウンター（連続超過 N 回でKillSwitch）
        self._drawdown_breach_counter: int = 0

    @property
    def config(self) -> MonitorConfig:
        return self._config

    def start(self) -> None:
        """daemon スレッドを起動する。

        RT-R2-001: metric_provider=None の場合は CRITICAL ログ警告を出す。
        全ゼロ値では監視が実質無効になるため、明示的に provider を設定することを推奨。
        """
        if self._thread is not None and self._thread.is_alive():
            log.warning("[Monitor] already running, skip start()")
            return
        # RT-R2-001: metric_provider 未設定警告
        if self._config.metric_provider is None:
            log.critical(
                "[Monitor] metric_provider is None. "
                "_run_loop will use all-zero metrics (pnl=0/dd=0/lat=0). "
                "Monitoring is effectively disabled. "
                "Set MonitorConfig.metric_provider to a callable that returns "
                "{'pnl_day_usd': float, 'drawdown_pct': float, 'latency_ms': float}."
            )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="atlas-monitor",
            daemon=True,
        )
        self._thread.start()
        log.info("[Monitor] started (interval=%.0fs)", self._config.check_interval_secs)

    def stop(self, timeout: float = 5.0) -> None:
        """daemon スレッドを停止する。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        log.info("[Monitor] stopped")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def record_heartbeat(self) -> None:
        """Bot から heartbeat を受信したことを記録する。"""
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def check_once(
        self,
        pnl_day_usd: float = 0.0,
        drawdown_pct: float = 0.0,
        latency_ms: float = 0.0,
    ) -> list[HealthCheck]:
        """全チェックを 1 回実行して HealthCheck リストを返す。

        テスト用に外部から直接呼び出せる。
        daemon 起動なしに単体テストが可能。

        Args:
            pnl_day_usd:   当日損益（USD）
            drawdown_pct:  現在ドローダウン率
            latency_ms:    直近 API レイテンシ（ms）

        Returns:
            HealthCheck のリスト（各チェック 1 件）
        """
        checks: list[HealthCheck] = []
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

        checks.append(self._check_heartbeat(ts))
        checks.append(self._check_daily_loss(ts, pnl_day_usd))
        checks.append(self._check_drawdown(ts, drawdown_pct))
        checks.append(self._check_latency(ts, latency_ms))

        for chk in checks:
            self._handle_check(chk)

        self._write_log(checks)
        return checks

    # ------------------------------------------------------------------
    # 内部チェックメソッド
    # ------------------------------------------------------------------

    def _check_heartbeat(self, ts: str) -> HealthCheck:
        """Bot heartbeat の経過時間をチェックする。"""
        with self._lock:
            elapsed = time.monotonic() - self._last_heartbeat

        if elapsed > self._config.heartbeat_timeout_secs:
            return HealthCheck(
                ts=ts,
                level=AlertLevel.EMERGENCY,
                check_name="heartbeat",
                message=(
                    f"No heartbeat for {elapsed:.0f}s "
                    f"(threshold={self._config.heartbeat_timeout_secs:.0f}s). "
                    "Bot may be dead."
                ),
                value=elapsed,
                threshold=self._config.heartbeat_timeout_secs,
            )
        return HealthCheck(
            ts=ts,
            level=AlertLevel.INFO,
            check_name="heartbeat",
            message=f"OK (last {elapsed:.0f}s ago)",
            value=elapsed,
            threshold=self._config.heartbeat_timeout_secs,
        )

    def _check_daily_loss(self, ts: str, pnl_day_usd: float) -> HealthCheck:
        """日次損益が制限を超えていないかチェックする。

        NEW-C-4: NaN/inf 入力は即 EMERGENCY + KillSwitch（比較演算が全 False になるため先頭でガード）。
        threshold は負値前提（__post_init__ で < 0 を保証）。
        - pnl > threshold                    → INFO（正常）
        - threshold >= pnl > threshold * 1.5 → CRITICAL（閾値超過・軽微）
        - pnl <= threshold * 1.5             → EMERGENCY（閾値の 1.5 倍超過・深刻）

        threshold=-400 の境界値例:
          pnl=-399  → INFO
          pnl=-400  → CRITICAL  (threshold * 1.5 = -600, -400 > -600)
          pnl=-500  → CRITICAL  (-500 > -600)
          pnl=-600  → EMERGENCY (-600 <= -600)
          pnl=-601  → EMERGENCY (-601 <= -600)
          pnl=NaN   → EMERGENCY（壊れた値・即 KillSwitch）
          pnl=inf   → EMERGENCY（壊れた値・即 KillSwitch）
        """
        # NEW-C-4: NaN/inf は比較演算が全 False になるため先頭で即 EMERGENCY
        if math.isnan(pnl_day_usd) or math.isinf(pnl_day_usd):
            bad_chk = HealthCheck(
                ts=ts,
                level=AlertLevel.EMERGENCY,
                check_name="daily_loss",
                message=(
                    f"pnl_day_usd is {pnl_day_usd!r} (NaN or inf). "
                    "Corrupted metric — activating KillSwitch immediately."
                ),
                value=None,
                threshold=self._config.daily_loss_usd,
            )
            if self._config.kill_switch_on_emergency:
                self._activate_kill_switch(bad_chk)
            return bad_chk

        threshold = self._config.daily_loss_usd  # 例: -400.0
        if pnl_day_usd > threshold:
            return HealthCheck(
                ts=ts,
                level=AlertLevel.INFO,
                check_name="daily_loss",
                message=f"OK (pnl={pnl_day_usd:.2f}, threshold={threshold:.2f})",
                value=pnl_day_usd,
                threshold=threshold,
            )
        # pnl_day_usd <= threshold（損失が閾値以上）
        # threshold * 1.5 は threshold が負値なのでより小さい（深刻側）
        emergency_threshold = threshold * 1.5
        level = AlertLevel.EMERGENCY if pnl_day_usd <= emergency_threshold else AlertLevel.CRITICAL
        return HealthCheck(
            ts=ts,
            level=level,
            check_name="daily_loss",
            message=(
                f"Daily loss exceeded: pnl={pnl_day_usd:.2f} "
                f"<= threshold={threshold:.2f} "
                f"(emergency_threshold={emergency_threshold:.2f})"
            ),
            value=pnl_day_usd,
            threshold=threshold,
        )

    def _check_drawdown(self, ts: str, drawdown_pct: float) -> HealthCheck:
        """ドローダウンが制限を超えていないかチェックする。

        C4 fix: Schmitt Trigger hysteresis（振動脆弱修正）。
        旧実装: 上閾値超過で counter++、閾値以下で即 counter=0 reset
          → Flash Crash 型振動（閾値付近の上下）で counter が永遠に reset される
          → KillSwitch が永遠に発動しない脆弱性

        新実装: Schmitt Trigger 方式
          - 上閾値 (hysteresis_upper = drawdown_pct) を超えると carry-counter++
          - 下閾値 (hysteresis_lower = drawdown_pct * 0.8) を下回ると carry-counter--
          - 上下閾値の帯域内では counter を保持（振動しない）
          - CRITICAL は counter が drawdown_breach_count 以上になった時のみ発火

        例: drawdown_pct=0.12, hysteresis_upper=0.12, hysteresis_lower=0.096
          drawdown: 0.13 → counter++（上閾値超過）
          drawdown: 0.11 → counter 保持（帯域内 0.096–0.12）
          drawdown: 0.13 → counter++（再び上閾値超過）
          drawdown: 0.08 → counter--（下閾値以下）
          これにより 0.11 付近の振動でカウンターがリセットされない。
        """
        # C4 fix: Schmitt Trigger 上下閾値を解決
        upper = self._config.hysteresis_upper
        if upper is None:
            upper = self._config.drawdown_pct  # 後方互換
        lower = self._config.hysteresis_lower
        if lower is None:
            lower = self._config.drawdown_pct * 0.8  # 旧閾値の 80% = 20% margin

        threshold = self._config.drawdown_pct

        if drawdown_pct > upper:
            # 上閾値超過 → carry-counter++
            self._drawdown_breach_counter += 1
            breach_count = self._config.drawdown_breach_count
            if self._drawdown_breach_counter >= breach_count:
                return HealthCheck(
                    ts=ts,
                    level=AlertLevel.CRITICAL,
                    check_name="drawdown",
                    message=(
                        f"Drawdown exceeded: {drawdown_pct:.4f} "
                        f"> upper={upper:.4f} "
                        f"(consecutive_breach={self._drawdown_breach_counter}/{breach_count})"
                    ),
                    value=drawdown_pct,
                    threshold=threshold,
                )
            # hysteresis: breach_count 未満は WARNING のみ（KillSwitch 非発動）
            return HealthCheck(
                ts=ts,
                level=AlertLevel.WARNING,
                check_name="drawdown",
                message=(
                    f"Drawdown exceeded: {drawdown_pct:.4f} "
                    f"> upper={upper:.4f} "
                    f"(schmitt_hysteresis: {self._drawdown_breach_counter}/{breach_count} — "
                    "KillSwitch will fire if sustained)"
                ),
                value=drawdown_pct,
                threshold=threshold,
            )

        if drawdown_pct < lower:
            # 下閾値以下に回復 → carry-counter--（ゼロ以下にはならない）
            if self._drawdown_breach_counter > 0:
                self._drawdown_breach_counter -= 1
                log.info(
                    "[Monitor] drawdown %.4f below lower=%.4f (Schmitt recovery). "
                    "Decremented breach counter to %d.",
                    drawdown_pct, lower, self._drawdown_breach_counter,
                )
        # else: 帯域内 (lower <= drawdown_pct <= upper) → counter 保持（Schmitt Trigger 核心）

        if self._drawdown_breach_counter > 0:
            # 帯域内だが counter > 0 = 過去に上閾値超過があった状態 → WARNING 継続
            breach_count = self._config.drawdown_breach_count
            return HealthCheck(
                ts=ts,
                level=AlertLevel.WARNING,
                check_name="drawdown",
                message=(
                    f"Drawdown in hysteresis band: {drawdown_pct:.4f} "
                    f"(lower={lower:.4f}, upper={upper:.4f}). "
                    f"Counter maintained: {self._drawdown_breach_counter}/{breach_count}"
                ),
                value=drawdown_pct,
                threshold=threshold,
            )

        return HealthCheck(
            ts=ts,
            level=AlertLevel.INFO,
            check_name="drawdown",
            message=(
                f"OK (drawdown={drawdown_pct:.4f}, upper={upper:.4f}, "
                f"lower={lower:.4f})"
            ),
            value=drawdown_pct,
            threshold=threshold,
        )

    def _check_latency(self, ts: str, latency_ms: float) -> HealthCheck:
        """API レイテンシが閾値を超えていないかチェックする。"""
        threshold = self._config.max_latency_ms
        if latency_ms > threshold:
            return HealthCheck(
                ts=ts,
                level=AlertLevel.WARNING,
                check_name="latency",
                message=(
                    f"High latency: {latency_ms:.1f}ms "
                    f"> threshold={threshold:.1f}ms"
                ),
                value=latency_ms,
                threshold=threshold,
            )
        return HealthCheck(
            ts=ts,
            level=AlertLevel.INFO,
            check_name="latency",
            message=f"OK (latency={latency_ms:.1f}ms, threshold={threshold:.1f}ms)",
            value=latency_ms,
            threshold=threshold,
        )

    # ------------------------------------------------------------------
    # アラート発火
    # ------------------------------------------------------------------

    def _handle_check(self, chk: HealthCheck) -> None:
        """チェック結果に応じてアラート・KillSwitch 発動を行う。

        KillSwitch 発動条件:
        - EMERGENCY + kill_switch_on_emergency=True（既存）
        - CRITICAL drawdown + kill_switch_on_drawdown_breach=True（H-2 追加）
        """
        if chk.level == AlertLevel.INFO:
            return

        log.warning("[Monitor] %s: %s — %s", chk.level.value, chk.check_name, chk.message)

        if self._config.pushover_enabled:
            self._send_alert(chk)

        if chk.level == AlertLevel.EMERGENCY and self._config.kill_switch_on_emergency:
            self._activate_kill_switch(chk)
            return

        # H-2: drawdown CRITICAL 超過時も KillSwitch 発動（設定 on 時のみ）
        if (
            chk.level == AlertLevel.CRITICAL
            and chk.check_name == "drawdown"
            and self._config.kill_switch_on_drawdown_breach
        ):
            log.critical(
                "[Monitor] KillSwitch triggered by drawdown CRITICAL breach: %s",
                chk.message,
            )
            self._activate_kill_switch(chk)

    def _send_alert(self, chk: HealthCheck) -> None:
        """Pushover アラートを 3 経路で送信する。

        priority 割り当て:
            WARNING   → priority=0（サイレント通知）
            CRITICAL  → priority=1（音声通知）
            EMERGENCY → priority=2（繰り返し通知・60s間隔・10分継続）

        RT-R2-007: Pushover API response の status を確認。
        rate-limit detected → 即 ntfy fallback 発火。
        """
        priority_map = {
            AlertLevel.WARNING: 0,
            AlertLevel.CRITICAL: 1,
            AlertLevel.EMERGENCY: 2,
        }
        priority = priority_map.get(chk.level, 0)

        title = f"[Atlas-Monitor] {chk.level.value.upper()}: {chk.check_name}"
        message = chk.message

        if self._pushover_send is not None:
            # テスト注入パス
            try:
                self._pushover_send(title=title, message=message, priority=priority)
            except Exception as e:
                log.error("[Monitor] Pushover send failed (injected): %s", e)
            return

        # 本番パス: Pushover 優先・失敗時のみ ntfy fallback 発火（CRITICAL 6 修正）
        # RT-R2-007: Pushover status 確認・rate-limit 検出で即 fallback
        pushover_ok = self._send_pushover(title, message, priority)
        if not pushover_ok:
            self._send_ntfy_fallback(title, message, chk.level)

    def _send_pushover(self, title: str, message: str, priority: int) -> bool:
        """common.pushover_client 経由で送信する。

        RT-R2-007: API response から status / request を確認。
        rate-limited (429) または status != 1 → False を返して ntfy fallback 発火。

        Returns:
            True: 送信成功（HTTP 200 かつ response status=1）
            False: 送信失敗（例外・HTTP エラー・API status!=1・rate-limit）
        """
        try:
            import common.pushover_client as _pushover
            kwargs: dict = {"title": title, "message": message, "priority": priority}
            if priority == 2:
                kwargs["retry"] = 60
                kwargs["expire"] = 600
            result = _pushover.send(**kwargs)

            # RT-R2-007: send() が dict を返す場合は status/request を検証
            if isinstance(result, dict):
                api_status = result.get("status")
                if api_status is not None and api_status != 1:
                    log.warning(
                        "[Monitor] Pushover API status=%s (expected 1). "
                        "Possible rate-limit or error. Triggering ntfy fallback.",
                        api_status,
                    )
                    return False
            return True
        except Exception as e:
            # HTTP 429 rate-limit を含む全例外で fallback
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower():
                log.warning(
                    "[Monitor] Pushover rate-limited (429). Triggering ntfy fallback."
                )
            else:
                log.error("[Monitor] Pushover send failed: %s", e)
            return False

    def _send_ntfy_fallback(
        self, title: str, message: str, level: AlertLevel
    ) -> None:
        """ntfy.sh fallback 送信（Pushover 失敗時のバックアップ）。

        ntfy Priority ヘッダは数値 1-5 が仕様。
        ref: https://docs.ntfy.sh/publish/#message-priority
          1=min, 2=low, 3=default, 4=high, 5=urgent (≒ max)
        """
        # CRITICAL 5 修正: Enum.value 文字列ではなく数値 int を使用
        _ntfy_priority_map = {
            AlertLevel.WARNING: 3,    # default
            AlertLevel.CRITICAL: 4,   # high
            AlertLevel.EMERGENCY: 5,  # urgent/max
        }
        priority_int = _ntfy_priority_map.get(level, 3)
        try:
            import urllib.request
            topic = os.environ.get("NTFY_TOPIC", "spxbot-hub-yuusaku2026")
            url = f"https://ntfy.sh/{topic}"
            body = f"{title}\n{message}".encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"Title": title, "Priority": str(priority_int)},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            log.error("[Monitor] ntfy fallback failed: %s", e)

    def _activate_kill_switch(self, chk: HealthCheck) -> None:
        """EMERGENCY 時に KillSwitch を発動する。

        RT-R2-005: ファイル書込失敗時は retry 3 回 + 全失敗で andon_multichannel 連動。
        fail-closed（最悪ケースで即停止を保証）。
        """
        MAX_RETRIES = 3
        last_exc: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                from common_v3.risk.kill_switch import activate as ks_activate
                activated = ks_activate(
                    reason=f"monitor_emergency:{chk.check_name}:{chk.message[:100]}",
                    activator="atlas_monitor_daemon",
                )
                if activated:
                    log.critical(
                        "[Monitor] KillSwitch ACTIVATED by monitor: %s", chk.message
                    )
                else:
                    log.warning("[Monitor] KillSwitch already active (idempotent skip)")
                return  # 成功
            except Exception as e:
                last_exc = e
                log.error(
                    "[Monitor] KillSwitch activation failed (attempt %d/%d): %s",
                    attempt, MAX_RETRIES, e,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(0.5 * attempt)  # バックオフ

        # 全リトライ失敗 → andon_multichannel で EMERGENCY 再送（fail-closed）
        log.critical(
            "[Monitor] KillSwitch activation FAILED after %d retries. "
            "Triggering andon_multichannel. Last error: %s",
            MAX_RETRIES, last_exc,
        )
        self._trigger_andon_emergency(chk, last_exc)

    def _trigger_andon_emergency(
        self, chk: HealthCheck, exc: Optional[Exception]
    ) -> None:
        """KillSwitch 全失敗時の最終手段: andon_multichannel 経由で EMERGENCY 発令。

        RT-R2-005: fail-closed 保証のための最終エスカレーション。
        """
        try:
            import sys as _sys
            hooks_dir = _BASE / ".claude" / "hooks"
            hooks_str = str(hooks_dir)
            if hooks_str not in _sys.path:
                _sys.path.insert(0, hooks_str)
            from andon_multichannel import pull_andon
            pull_andon(
                reason=(
                    f"KillSwitch activation FAILED: {chk.check_name} {chk.message[:100]}. "
                    f"Error: {exc}"
                ),
                source="atlas_monitor_daemon",
            )
        except Exception as andon_err:
            log.critical(
                "[Monitor] andon_multichannel also failed: %s. "
                "SYSTEM IS IN UNKNOWN STATE. Manual intervention required.",
                andon_err,
            )

    # ------------------------------------------------------------------
    # ログ書込
    # ------------------------------------------------------------------

    def _write_log(self, checks: list[HealthCheck]) -> None:
        """チェック結果を monitor_state.jsonl に append-only で書き込む。

        HIGH-R4-2: サイズベースのローテーション。
        - log_max_bytes > 0 かつ現在のファイルサイズが上限を超えた場合はローテーション実行。
        - ローテーション: monitor_state.jsonl.N → .N+1 形式（標準 logging.handlers.RotatingFileHandler と同方式）
        - log_backup_count 世代まで保持。
        - check_interval_secs=15 の場合、1チェックあたり約200B×4チェック=800B/15s
          → 10MB = 約12,500秒 ≒ 3.5時間でローテーション発生（適切な頻度）
        """
        log_path = self._config.log_path or _MONITOR_LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # HIGH-R4-2: サイズ確認とローテーション
        max_bytes = self._config.log_max_bytes
        if max_bytes > 0 and log_path.exists():
            try:
                if log_path.stat().st_size >= max_bytes:
                    self._rotate_log(log_path)
            except Exception as e:
                log.error("[Monitor] log rotation check failed: %s", e)

        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                for chk in checks:
                    fh.write(json.dumps(chk.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
        except Exception as e:
            log.error("[Monitor] log write failed: %s", e)

    def _rotate_log(self, log_path: Path) -> None:
        """HIGH-R4-2: サイズ上限超過時のログローテーション。

        monitor_state.jsonl.1 → .2 → ... → .N の順にシフト。
        backup_count 世代を超えた古いファイルは削除。
        """
        backup_count = self._config.log_backup_count
        if backup_count <= 0:
            return
        try:
            # 古いバックアップを後ろからシフト
            for i in range(backup_count - 1, 0, -1):
                old = Path(f"{log_path}.{i}")
                new = Path(f"{log_path}.{i + 1}")
                if old.exists():
                    old.rename(new)
            # 現在のログを .1 に移動
            if log_path.exists():
                log_path.rename(Path(f"{log_path}.1"))
            log.info(
                "[Monitor] Log rotated: %s (max_bytes=%d, backup_count=%d)",
                log_path.name, self._config.log_max_bytes, backup_count,
            )
        except Exception as e:
            log.error("[Monitor] log rotation failed: %s", e)

    # ------------------------------------------------------------------
    # daemon ループ
    # ------------------------------------------------------------------

    def _touch_heartbeat_file(self) -> None:
        """daemon 生存確認用 heartbeat ファイルに現在時刻を書き込む（H-3）。

        RT-R2-H2: disk full 等で touch 失敗した場合は andon_multichannel 連動で EMERGENCY。
        """
        hb_file = self._config.heartbeat_file
        if hb_file is None:
            return
        try:
            hb_file.parent.mkdir(parents=True, exist_ok=True)
            hb_file.write_text(
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                encoding="utf-8",
            )
        except Exception as e:
            log.error("[Monitor] heartbeat file write failed: %s", e)
            # RT-R2-H2: 失敗時 andon_multichannel EMERGENCY 連動
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            emergency_chk = HealthCheck(
                ts=ts,
                level=AlertLevel.EMERGENCY,
                check_name="heartbeat_file_write_failed",
                message=(
                    f"Heartbeat file write failed: {e}. "
                    "Possible disk full or permission error. Bot liveness unknown."
                ),
            )
            if self._config.pushover_enabled:
                self._send_alert(emergency_chk)
            # andon_multichannel 連動で fail-closed
            self._trigger_andon_emergency(emergency_chk, exc=e)

    def _fetch_metrics(self) -> dict:
        """metric_provider から Bot 実データを取得する。

        NEW-C-2 (fail-closed 化):
        - provider=None: 永久全盲を防ぐため EMERGENCY 発令 + KillSwitch 発動 + raise
        - provider 失敗: zero-fallback を完全削除。例外を raise して daemon に伝播させ
          _run_loop の consecutive_failures カウンタを増加させる（fail-closed）。
        - dict 欠損キー: KeyError を raise（silent zero-fallback 禁止）

        RT-R2-001: _run_loop で check_once() に渡す実データを取得。
        metric_provider は bootstrap_paper_monitor() または MonitorConfig に明示設定必須。
        """
        if self._config.metric_provider is None:
            # NEW-C-2: provider=None のまま運用すると監視が永久全盲になる。
            # EMERGENCY 発令 + KillSwitch + raise でフェイルクローズ。
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            emergency_chk = HealthCheck(
                ts=ts,
                level=AlertLevel.EMERGENCY,
                check_name="metric_provider_missing",
                message=(
                    "metric_provider is None. Monitoring is blind. "
                    "Set MonitorConfig.metric_provider before starting daemon."
                ),
            )
            if self._config.pushover_enabled:
                self._send_alert(emergency_chk)
            if self._config.kill_switch_on_emergency:
                self._activate_kill_switch(emergency_chk)
            raise RuntimeError(
                "[Monitor] metric_provider is None — monitoring is blind. "
                "Activating KillSwitch. Stop daemon and set metric_provider."
            )

        # provider 失敗時: zero-fallback 禁止 → 例外を上げて fail-closed
        raw = self._config.metric_provider()

        # CRIT-R4-5: raw=None の場合は raw.keys() で AttributeError が発生するため先頭でガード
        if raw is None:
            raise RuntimeError(
                "[Monitor] metric_provider() returned None. "
                "Provider must return a dict with keys: "
                "pnl_day_usd / drawdown_pct / latency_ms. "
                "Returning None is treated as a fatal provider failure (fail-closed)."
            )
        if not isinstance(raw, dict):
            raise RuntimeError(
                f"[Monitor] metric_provider() returned {type(raw).__name__} (expected dict). "
                "Provider must return a dict with keys: "
                "pnl_day_usd / drawdown_pct / latency_ms."
            )

        # 必須キー欠損チェック（silent zero-fallback 禁止）
        missing_keys = {"pnl_day_usd", "drawdown_pct", "latency_ms"} - raw.keys()
        if missing_keys:
            raise KeyError(
                f"[Monitor] metric_provider() returned dict missing required keys: "
                f"{sorted(missing_keys)}. Got keys: {sorted(raw.keys())}"
            )

        return {
            "pnl_day_usd": float(raw["pnl_day_usd"]),
            "drawdown_pct": float(raw["drawdown_pct"]),
            "latency_ms": float(raw["latency_ms"]),
        }

    def _run_loop(self) -> None:
        """daemon メインループ（スレッド内で実行される）。

        H-3: heartbeat_file が設定されている場合は heartbeat_write_interval_secs ごとに touch する。
        H-8: check_once の連続失敗が max_consecutive_failures 回に達したら EMERGENCY 送信して停止する。
        RT-R2-001: metric_provider から実データを取得して check_once() に注入する。
        CRIT-R4-4: 連続失敗 max 回到達後は即停止せず 3 秒待機して自動 probe を実施。
        - probe 成功（例外なし）→ consecutive_failures reset して続行（自爆ループ防止）
        - probe 失敗 → andon_multichannel EMERGENCY + daemon 停止
        """
        log.info("[Monitor] loop started")
        consecutive_failures = 0
        last_hb_touch = time.monotonic()

        while not self._stop_event.is_set():
            # H-3: heartbeat ファイル定期 touch
            now = time.monotonic()
            if now - last_hb_touch >= self._config.heartbeat_write_interval_secs:
                self._touch_heartbeat_file()
                last_hb_touch = now

            try:
                # RT-R2-001: provider から実データを取得して check_once() に注入
                metrics = self._fetch_metrics()
                self.check_once(
                    pnl_day_usd=metrics["pnl_day_usd"],
                    drawdown_pct=metrics["drawdown_pct"],
                    latency_ms=metrics["latency_ms"],
                )
                consecutive_failures = 0  # 成功したらリセット
            except Exception as e:
                consecutive_failures += 1
                log.error(
                    "[Monitor] check_once error (%d/%d): %s",
                    consecutive_failures,
                    self._config.max_consecutive_failures,
                    e,
                )
                # H-8 + CRIT-R4-4: 連続失敗上限に達したら EMERGENCY 送信 + 自動 probe
                if consecutive_failures >= self._config.max_consecutive_failures:
                    log.critical(
                        "[Monitor] Consecutive failures reached %d. "
                        "Sending EMERGENCY and probing for recovery.",
                        consecutive_failures,
                    )
                    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    emergency_chk = HealthCheck(
                        ts=ts,
                        level=AlertLevel.EMERGENCY,
                        check_name="daemon_consecutive_failures",
                        message=(
                            f"check_once failed {consecutive_failures} times consecutively. "
                            f"Last error: {e}"
                        ),
                    )
                    if self._config.pushover_enabled:
                        self._send_alert(emergency_chk)
                    if self._config.kill_switch_on_emergency:
                        self._activate_kill_switch(emergency_chk)

                    # CRIT-R4-4: 自動 probe（probe_on_consecutive_failure=True 時のみ）
                    # kill_switch 復旧や一時的エラー解消を検出して自爆ループを防ぐ
                    # デフォルト False = 後方互換（即停止）
                    if self._config.probe_on_consecutive_failure and not self._stop_event.wait(timeout=3.0):
                        probe_ok = self._probe_recovery()
                        if probe_ok:
                            log.warning(
                                "[Monitor] Recovery probe succeeded. "
                                "Resetting consecutive_failures and resuming.",
                            )
                            consecutive_failures = 0
                            continue
                        # probe 失敗 → daemon 停止
                        log.critical("[Monitor] Recovery probe failed. Stopping daemon.")
                    # probe 無効 or probe 失敗 → daemon 停止
                    self._stop_event.set()
                    break

            self._stop_event.wait(timeout=self._config.check_interval_secs)
        log.info("[Monitor] loop stopped")

    def _is_dummy_provider(self, zero_detection_n: int = 0) -> bool:
        """CRIT-R6-3 fix: metric_provider が Dummy 系 provider か確認する。

        旧実装: type(instance).__name__ == "DummyMetricProvider" → 文字列判定
          → SneakyDummy(DummyMetricProvider) サブクラスで bypass 可能。

        新実装:
        1. isinstance チェック: bound method の __self__ が DummyMetricProvider の
           isinstance チェック（サブクラスも含む）。
        2. runtime zero-value detection: zero_detection_n > 0 の場合のみ有効。
           bound method が取れない場合（lambda 等）で、直近 zero_detection_n 回
           連続で全 metric が 0.0 の provider を「疑わしい」と判定して True を返す。
           zero_detection_n=0（デフォルト）ではこの機能は無効（後方互換）。

        CRIT-R6-3 修正: SneakyDummy バイパス完全防止。

        後方互換:
          デフォルト zero_detection_n=0 で旧挙動（isinstance のみ）を維持する。
          zero detection を有効化する場合は明示的に zero_detection_n=5 を渡す。
          （r6 テスト: lambda 全ゼロ → False の期待値を維持）

        Args:
            zero_detection_n: 連続 0 値検出の試行回数。0 = 無効（デフォルト・後方互換）。

        Returns:
            True: provider が Dummy 系（DummyMetricProvider またはサブクラス、
                  もしくは zero_detection_n > 0 時の連続 zero-value provider）
            False: 実 provider（YFinanceMetricProvider 等）
        """
        fn = self._config.metric_provider
        if fn is None:
            return False

        # bound method の場合は __self__ から元インスタンスを取得
        instance = getattr(fn, "__self__", None)
        if instance is not None:
            # CRIT-R6-3 fix: isinstance でサブクラスも含めてチェック
            # 循環 import 回避のため遅延 import
            try:
                from atlas_v3.main import DummyMetricProvider
                if isinstance(instance, DummyMetricProvider):
                    return True
            except ImportError:
                # import 失敗時は文字列判定にフォールバック
                cls_name = type(instance).__name__
                if "Dummy" in cls_name or cls_name == "DummyMetricProvider":
                    return True

        # CRIT-R6-3 runtime zero-value detection（オプトイン: zero_detection_n > 0 時のみ）:
        # zero_detection_n=0（デフォルト）では無効（後方互換・lambda 全ゼロ → False を維持）。
        if zero_detection_n > 0:
            try:
                zero_count = 0
                required_keys = {"pnl_day_usd", "drawdown_pct", "latency_ms"}
                for _ in range(zero_detection_n):
                    try:
                        result = fn()
                        if not isinstance(result, dict):
                            break
                        missing = required_keys - result.keys()
                        if missing:
                            break
                        # 全 metric が 0.0 かチェック
                        all_zero = all(result.get(k, -1.0) == 0.0 for k in required_keys)
                        if all_zero:
                            zero_count += 1
                        else:
                            break  # 非ゼロ値があれば実 provider
                    except Exception:
                        break  # 例外が出たら実 provider（Dummy は例外を出さない）
                if zero_count >= zero_detection_n:
                    log.warning(
                        "[Monitor] _is_dummy_provider: zero-value detection triggered "
                        "(%d/%d consecutive zero metrics). Treating as Dummy provider (CRIT-R6-3 fix).",
                        zero_count, zero_detection_n,
                    )
                    return True
            except Exception as e:
                log.debug("[Monitor] _is_dummy_provider zero-detection error: %s", e)

        return False

    def _probe_recovery(self) -> bool:
        """CRIT-R4-4: 連続失敗後の回復確認 probe。

        metric_provider() が例外を起こさず dict を返せるかテストする。
        成功すれば True を返し、_run_loop は consecutive_failures を reset して続行する。
        失敗すれば False を返し、_run_loop は daemon を停止する（andon 連動済み）。

        C3 fix: probe 成功時（True を返す時）に KillSwitch を deactivate() する。
        状態機械: activate → probe 成功 → deactivate → counter reset → monitoring 再開
        これにより probe 成功で counter だけリセットして KillSwitch が ARMED のまま
        残る「ゾンビ状態」を解消する。

        H3 fix: DummyMetricProvider 使用時は probe 結果を strict=False 扱い（False を返す）。
        Dummy は例外を上げず dict を返すため、probe が常に成功し KillSwitch が無限解除される。
        Dummy 使用中は「実データで回復確認できていない」として False を返す。

        Returns:
            True: probe 成功（実 provider での回復確認 + KillSwitch deactivate 済み）
            False: probe 失敗（問題継続 / Dummy 使用中）
        """
        # H3 fix: Dummy 使用中は probe 失敗扱い
        if self._is_dummy_provider():
            log.warning(
                "[Monitor] Recovery probe: DummyMetricProvider detected. "
                "Probe treated as FAIL to prevent Dummy-probe collusion (H3 fix). "
                "Replace with real provider (--provider yfinance) to enable recovery."
            )
            return False

        try:
            raw = self._config.metric_provider() if self._config.metric_provider else None
            if raw is None or not isinstance(raw, dict):
                return False
            # 必須キーの存在確認
            required = {"pnl_day_usd", "drawdown_pct", "latency_ms"}
            if not required.issubset(raw.keys()):
                return False

            # C3 fix: probe 成功 → global KillSwitch を deactivate して「ゾンビ状態」解消
            # CRIT-R6-2 fix: 全 firm の per-firm flag も一括解除（FirmScopedKillSwitch.deactivate_all()）
            # 状態機械: activate → probe 成功 → deactivate(global+all_firms) → counter reset → 再開
            try:
                from common_v3.risk.kill_switch import deactivate as ks_deactivate
                # C3 fix: global flag を解除（必須・失敗でも probe は True を返す）
                ks_deactivate(
                    activator="atlas_monitor_probe_recovery",
                    reason="probe_recovery_success_c3_fix_crit_r6_2",
                )
                log.warning(
                    "[Monitor] KillSwitch DEACTIVATED by probe_recovery (C3 fix). "
                    "State machine: activate → probe success → deactivate → resumed."
                )
            except Exception as deact_err:
                # deactivate 失敗は非致命的（probe 自体は成功している）
                log.error(
                    "[Monitor] KillSwitch deactivate failed in probe_recovery: %s. "
                    "Probe still returns True but KillSwitch may remain ARMED.",
                    deact_err,
                )

            # CRIT-R6-2 fix: per-firm flag を全 firm で解除（FirmScopedKillSwitch.deactivate_all）
            # global deactivate とは独立して試みる（失敗でも probe は True を返す）
            try:
                from common_v3.risk.kill_switch import FirmScopedKillSwitch
                firm_results = FirmScopedKillSwitch.deactivate_all(
                    activator="atlas_monitor_probe_recovery"
                )
                if firm_results:
                    log.warning(
                        "[Monitor] probe_recovery: per-firm flags DEACTIVATED (CRIT-R6-2 fix): %s",
                        firm_results,
                    )
            except Exception as firm_deact_err:
                # per-firm deactivate 失敗は非致命的
                log.warning(
                    "[Monitor] per-firm KillSwitch deactivate failed in probe_recovery: %s. "
                    "CRIT-R6-2 fix: per-firm flags may remain ARMED.",
                    firm_deact_err,
                )

            return True

        except Exception as probe_err:
            log.warning("[Monitor] Recovery probe raised: %s", probe_err)
            return False


# ---------------------------------------------------------------------------
# NEW-C-1: bootstrap_paper_monitor — YAML ロードから MonitorDaemon 起動まで一貫 entry point
# ---------------------------------------------------------------------------

def bootstrap_paper_monitor(
    metric_provider: Callable[[], dict],
    *,
    config_path: Optional[Path] = None,
    run_preflight: bool = True,
    preflight_mode: str = "paper",
    pushover_send: Optional[Callable] = None,
) -> "MonitorDaemon":
    """YAML 設定を読み込み、preflight チェックを実行し、MonitorDaemon を起動して返す。

    NEW-C-1 修正:
    - MonitorConfig の裸デフォルト fallback（`MonitorDaemon()` 呼び出し）による
      YAML single source of truth 崩壊を防ぐ。
    - 本関数を production 起動の唯一の entry point として提供することで、
      YAML → MonitorConfig → MonitorDaemon の配線を保証する。
    - run_preflight=True（デフォルト）で preflight_compliance_check.py を実行し、
      PENDING_OWNER_APPROVAL 等の物理ブロックが機能することを確認する。

    判断 2: preflight_mode="paper" (デフォルト) で PENDING_OWNER_APPROVAL_PAPER を WARN 扱い。
            preflight_mode="live" で PENDING_OWNER_APPROVAL_LIVE を CRITICAL 扱い（起動 block）。

    Args:
        metric_provider:  Bot 実データを返す callable（必須）。
                          dict keys: pnl_day_usd / drawdown_pct / latency_ms
        config_path:      YAML 設定ファイルパス。None なら
                          atlas_v3/ops/risk_config_loader.py の default を使用。
        run_preflight:    True なら起動前に preflight_compliance_check.py を実行する。
        preflight_mode:   "paper" / "live"（判断 2 タグ分割に対応）。デフォルト "paper"。
        pushover_send:    テスト用 Pushover 送信関数（本番は None でよい）。

    Returns:
        起動済み MonitorDaemon インスタンス

    Raises:
        RuntimeError: preflight チェック失敗 / YAML 読み込み失敗
        ValueError:   metric_provider が None
    """
    if metric_provider is None:
        raise ValueError(
            "bootstrap_paper_monitor() requires metric_provider. "
            "Pass a callable that returns "
            "{'pnl_day_usd': float, 'drawdown_pct': float, 'latency_ms': float}."
        )

    # NEW-H-1 連携: preflight コンプライアンスチェック（判断 2: mode 引数で WARN/CRITICAL 分岐）
    if run_preflight:
        _run_preflight_check(mode=preflight_mode)

    # YAML から MonitorConfig を構築
    monitor_config = _load_monitor_config_from_yaml(config_path)

    # metric_provider を MonitorConfig に注入（frozen dataclass なので新規生成）
    # MonitorConfig は frozen=True なので dataclasses.replace() を使用
    import dataclasses as _dc
    config_with_provider = _dc.replace(
        monitor_config,
        metric_provider=metric_provider,
    )

    daemon = MonitorDaemon(config_with_provider, pushover_send=pushover_send)
    daemon.start()
    log.info("[bootstrap_paper_monitor] MonitorDaemon started via YAML config.")
    return daemon


def _run_preflight_check(mode: str = "paper") -> None:
    """preflight_compliance_check.py を実行してコンプライアンスチェックを行う。

    NEW-H-1 対応: bootstrap_paper_monitor() から呼ばれ、PENDING_OWNER_APPROVAL 等の
    物理ブロック機能を確認する。
    失敗時は RuntimeError を raise して起動を中断する。

    判断 2 対応: --mode paper で WARN のみ許容（起動継続）/ --mode live で CRITICAL 強制。
    bootstrap_paper_monitor() からは mode="paper" で呼ぶ（デフォルト）。

    Args:
        mode: "paper" (WARN のみ・起動継続) / "live" (CRITICAL・起動 block)
    """
    import subprocess
    import sys
    scripts_dir = _BASE / "scripts"
    preflight_script = scripts_dir / "preflight_compliance_check.py"

    if not preflight_script.exists():
        log.warning(
            "[bootstrap_paper_monitor] preflight_compliance_check.py not found at %s. "
            "Skipping preflight check.",
            preflight_script,
        )
        return

    result = subprocess.run(
        [sys.executable, str(preflight_script), "--all", "--mode", mode],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"preflight_compliance_check.py FAILED (exit={result.returncode}, mode={mode}). "
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}\n"
            "Fix all compliance issues before starting MonitorDaemon."
        )
    log.info("[bootstrap_paper_monitor] preflight_compliance_check.py passed (mode=%s).", mode)


def _load_monitor_config_from_yaml(config_path: Optional[Path] = None) -> "MonitorConfig":
    """YAML ファイルから MonitorConfig を構築する。

    CRIT-R4-2 修正（YAML single source of truth 看板倒れ解消）:
    - 旧実装: load_paper_risk_config() で RiskConfig を取得し getattr() でフォールバック
      → RiskConfig に monitor 専用属性（monitor_check_interval_secs 等）が存在しないため
        getattr() が常にデフォルト値を返し、YAML の monitor 設定が反映されない
    - 新実装: risk_config_loader.load_monitor_config_from_yaml() を直接使用
      → MonitorConfig のフィールドを YAML から直接構築する専用関数を利用
      → monitor.check_interval_secs 等の YAML キーが正しく反映される

    config_path=None の場合は risk_config_loader の _CONFIG_FILE (atlas_paper_risk.yaml) を使用。

    Returns:
        MonitorConfig（YAML から構築）

    Raises:
        RuntimeError: YAML 読み込み失敗
    """
    try:
        from atlas_v3.ops.risk_config_loader import load_monitor_config_from_yaml as _loader
        # CRIT-R4-2: load_monitor_config_from_yaml() は MonitorConfig 専用の YAML ローダ
        # RiskConfig 経由の getattr() フォールバックは使わない
        monitor_config = _loader(config_path=config_path)
        log.info(
            "[_load_monitor_config_from_yaml] MonitorConfig loaded from YAML: "
            "check_interval=%.1fs, daily_loss=%.1f, drawdown_pct=%.3f",
            monitor_config.check_interval_secs,
            monitor_config.daily_loss_usd,
            monitor_config.drawdown_pct,
        )
        return monitor_config
    except ImportError:
        log.warning(
            "[_load_monitor_config_from_yaml] risk_config_loader not available. "
            "Using MonitorConfig defaults."
        )
        return MonitorConfig()
    except Exception as e:
        raise RuntimeError(
            f"[_load_monitor_config_from_yaml] Failed to load YAML config: {e}"
        ) from e
