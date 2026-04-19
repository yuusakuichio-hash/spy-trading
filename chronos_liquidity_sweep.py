#!/usr/bin/env python3
"""
chronos_liquidity_sweep.py — F13 Liquidity Sweep 認識実装

定義:
  大出来高で前日高値・安値・VWAP 等の liquidity levels を突破し、
  即反転するパターン (stop hunt reversal / ICT流)。

検知ロジック:
  1. 前日高値・安値・前日 VWAP を追跡 (prev_high_sweep / prev_low_sweep)
  2. 現在価格が liquidity level を突破した直後の price action 監視
  3. 反転条件: 突破→反対方向への急反発 (ATR 0.5倍以上)
  4. 出来高フィルタ: 直近20分平均の2倍以上を伴う突破のみ

データソース:
  - 前日高安: chronos_bot.py / chronos_vwap.py から取得
  - IB 高安: FuturesORBStrategy.ib_high / ib_low から取得
  - VWAP: chronos_vwap.py から取得
  - 出来高: Tradovate 1分足バーから取得

戦略連携:
  chronos_strategy_selector.py の env["liquidity_sweep_signal"] として渡す。

参照:
  - chronos_rules.yaml: liquidity_sweep セクション
  - tests/test_f12_f13_implementation_20260419.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


# ── データクラス ─────────────────────────────────────────────────────────────

@dataclass
class SweepSignal:
    """Liquidity Sweep 検知シグナル。"""
    level_type:     str     # "prev_high" | "prev_low" | "prev_vwap" | "ib_high" | "ib_low"
    level_price:    float   # 突破された liquidity level の価格
    sweep_price:    float   # 突破時の最高値 (high sweep) / 最安値 (low sweep)
    direction:      str     # "sweep_high" | "sweep_low"
    volume_ratio:   float   # 突破バーの出来高 / 直近20分平均 (>2.0 で有効)
    atr_breach:     float   # 突破幅 / ATR (>0.0 で有効)
    confirmed:      bool    # 反転確認済みか (post_sweep_bars で判定)
    reason:         str     # 選択理由

    @property
    def is_valid(self) -> bool:
        """有効なシグナルか (出来高・突破幅フィルタ適用)。"""
        return self.volume_ratio >= 2.0 and self.atr_breach >= 0.0


@dataclass
class BarSnapshot:
    """1分足バーのスナップショット。"""
    timestamp: int
    open:  float
    high:  float
    low:   float
    close: float
    volume: float


# ── メインクラス ─────────────────────────────────────────────────────────────

class LiquiditySweepDetector:
    """
    Liquidity Sweep / Stop Hunt Reversal 検知クラス。

    使用方法:
        detector = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            ib_high=5140.0, ib_low=5110.0,
        )

        # 各バーで check_sweep を呼ぶ
        signal = detector.check_sweep(current_bar, volume_20m_avg, atr)

        if signal:
            # post_sweep_bars を収集して reversal 確認
            confirmed = detector.is_reversal_confirmed(post_bars)
    """

    def __init__(
        self,
        prev_high:  float,
        prev_low:   float,
        prev_vwap:  float,
        ib_high:    Optional[float] = None,
        ib_low:     Optional[float] = None,
        volume_multiplier:      float = 2.0,
        reversal_atr_mult:      float = 0.5,
        post_sweep_window_sec:  int   = 30,
        confirm_bars:           int   = 2,
    ):
        """
        Args:
            prev_high:             前日高値
            prev_low:              前日安値
            prev_vwap:             前日 VWAP
            ib_high:               IB (Initial Balance) 高値 (9:30-10:30 ET)
            ib_low:                IB 安値
            volume_multiplier:     有効突破の最低出来高倍率 (chronos_rules.yaml)
            reversal_atr_mult:     反転確認に必要な最低 ATR 倍率 (chronos_rules.yaml)
            post_sweep_window_sec: sweep 後の監視窓 (秒) (chronos_rules.yaml)
            confirm_bars:          反転確認に必要な最低バー数 (chronos_rules.yaml)
        """
        self.prev_high  = prev_high
        self.prev_low   = prev_low
        self.prev_vwap  = prev_vwap
        self.ib_high    = ib_high
        self.ib_low     = ib_low

        self.volume_multiplier     = volume_multiplier
        self.reversal_atr_mult     = reversal_atr_mult
        self.post_sweep_window_sec = post_sweep_window_sec
        self.confirm_bars          = confirm_bars

        # 全 liquidity levels を一元管理
        self._levels: dict[str, float] = self._build_levels()

        # 最後に検知したスイープ (未確認)
        self._pending_sweep: Optional[SweepSignal] = None
        self._sweep_timestamp: Optional[int] = None

        log.info(
            f"[LiquiditySweepDetector] init: "
            f"prev_high={prev_high:.2f} prev_low={prev_low:.2f} "
            f"prev_vwap={prev_vwap:.2f} ib_high={ib_high} ib_low={ib_low}"
        )

    def _build_levels(self) -> dict[str, float]:
        """全 liquidity levels を構築する。"""
        levels: dict[str, float] = {
            "prev_high": self.prev_high,
            "prev_low":  self.prev_low,
            "prev_vwap": self.prev_vwap,
        }
        if self.ib_high is not None:
            levels["ib_high"] = self.ib_high
        if self.ib_low is not None:
            levels["ib_low"] = self.ib_low
        return levels

    def update_levels(
        self,
        prev_high:  Optional[float] = None,
        prev_low:   Optional[float] = None,
        prev_vwap:  Optional[float] = None,
        ib_high:    Optional[float] = None,
        ib_low:     Optional[float] = None,
    ) -> None:
        """
        日次または IB 確定後に liquidity levels を更新する。

        chronos_bot.py の daily_reset() / on_ib_finalized() から呼ぶ。
        """
        if prev_high is not None:
            self.prev_high = prev_high
        if prev_low is not None:
            self.prev_low = prev_low
        if prev_vwap is not None:
            self.prev_vwap = prev_vwap
        if ib_high is not None:
            self.ib_high = ib_high
        if ib_low is not None:
            self.ib_low = ib_low

        self._levels = self._build_levels()
        log.info(
            f"[LiquiditySweepDetector] levels updated: {self._levels}"
        )

    # ── sweep 検知 ────────────────────────────────────────────────────────────

    def check_sweep(
        self,
        current_bar:    BarSnapshot,
        volume_20m_avg: float,
        atr:            float,
    ) -> Optional[SweepSignal]:
        """
        現在バーで Liquidity Sweep が発生したか確認する。

        Args:
            current_bar:    現在の 1 分足バー
            volume_20m_avg: 直近 20 分間の出来高平均
            atr:            現在の ATR 値

        Returns:
            SweepSignal (出来高フィルタ通過済み) または None
        """
        if volume_20m_avg <= 0 or atr <= 0:
            return None

        volume_ratio = current_bar.volume / volume_20m_avg if volume_20m_avg > 0 else 0.0

        # 出来高フィルタ (chronos_rules.yaml: volume_multiplier = 2.0)
        if volume_ratio < self.volume_multiplier:
            return None

        # 各 liquidity level に対して sweep を確認
        for level_type, level_price in self._levels.items():
            signal = self._check_level_sweep(
                current_bar  = current_bar,
                level_type   = level_type,
                level_price  = level_price,
                volume_ratio = volume_ratio,
                atr          = atr,
            )
            if signal:
                self._pending_sweep    = signal
                self._sweep_timestamp  = current_bar.timestamp
                log.info(
                    f"[LiquiditySweepDetector] sweep detected: "
                    f"type={level_type} level={level_price:.2f} "
                    f"dir={signal.direction} vol_ratio={volume_ratio:.1f}x"
                )
                return signal

        return None

    def _check_level_sweep(
        self,
        current_bar:  BarSnapshot,
        level_type:   str,
        level_price:  float,
        volume_ratio: float,
        atr:          float,
    ) -> Optional[SweepSignal]:
        """
        指定 liquidity level に対する sweep を確認する。

        High sweep: バーの high が level を突破し、close が level を下回る
        Low  sweep: バーの low  が level を突破し、close が level を上回る
        """
        if level_price <= 0:
            return None

        # High Sweep (stop hunt above): bar.high > level かつ bar.close < level
        if current_bar.high > level_price and current_bar.close < level_price:
            breach = current_bar.high - level_price
            atr_breach = breach / atr if atr > 0 else 0.0
            return SweepSignal(
                level_type   = level_type,
                level_price  = level_price,
                sweep_price  = current_bar.high,
                direction    = "sweep_high",
                volume_ratio = volume_ratio,
                atr_breach   = atr_breach,
                confirmed    = False,
                reason       = (
                    f"Sweep high: {level_type}={level_price:.2f} "
                    f"bar_high={current_bar.high:.2f} "
                    f"bar_close={current_bar.close:.2f} "
                    f"vol={volume_ratio:.1f}x ATR_breach={atr_breach:.2f}x"
                ),
            )

        # Low Sweep (stop hunt below): bar.low < level かつ bar.close > level
        if current_bar.low < level_price and current_bar.close > level_price:
            breach = level_price - current_bar.low
            atr_breach = breach / atr if atr > 0 else 0.0
            return SweepSignal(
                level_type   = level_type,
                level_price  = level_price,
                sweep_price  = current_bar.low,
                direction    = "sweep_low",
                volume_ratio = volume_ratio,
                atr_breach   = atr_breach,
                confirmed    = False,
                reason       = (
                    f"Sweep low: {level_type}={level_price:.2f} "
                    f"bar_low={current_bar.low:.2f} "
                    f"bar_close={current_bar.close:.2f} "
                    f"vol={volume_ratio:.1f}x ATR_breach={atr_breach:.2f}x"
                ),
            )

        return None

    # ── 反転確認 ─────────────────────────────────────────────────────────────

    def is_reversal_confirmed(
        self,
        post_sweep_bars: list[BarSnapshot],
        atr:             float = 0.0,
    ) -> bool:
        """
        Sweep 後の価格 action で反転を確認する。

        反転確認条件:
          1. 最低 confirm_bars 本のバーが利用可能
          2. High sweep → 価格が継続して下落 (bars が high sweep 水準を回復しない)
          3. Low  sweep → 価格が継続して上昇 (bars が low sweep 水準を回復しない)
          4. atr > 0 の場合: reversal_atr_mult × ATR 以上の反転幅

        Args:
            post_sweep_bars: sweep 直後の 1 分足バーリスト (新しい順)
            atr:             現在の ATR (0.0 の場合は ATR フィルタをスキップ)

        Returns:
            bool: True = 反転確認済み
        """
        if self._pending_sweep is None:
            return False

        if len(post_sweep_bars) < self.confirm_bars:
            log.debug(
                f"[LiquiditySweepDetector] reversal check: "
                f"insufficient bars {len(post_sweep_bars)} < {self.confirm_bars}"
            )
            return False

        sweep = self._pending_sweep
        confirm_bars = post_sweep_bars[:self.confirm_bars]

        if sweep.direction == "sweep_high":
            # High sweep → 反転確認: 全 confirm バーの close < sweep.level_price
            reversed_ok = all(b.close < sweep.level_price for b in confirm_bars)

            if reversed_ok and atr > 0:
                # ATR フィルタ: 反転幅 >= reversal_atr_mult × ATR
                reversal_move = sweep.sweep_price - min(b.low for b in confirm_bars)
                reversed_ok = reversal_move >= self.reversal_atr_mult * atr

            if reversed_ok:
                sweep.confirmed = True
                log.info(
                    f"[LiquiditySweepDetector] reversal confirmed: "
                    f"HIGH sweep at {sweep.level_price:.2f} → price fell"
                )
            return reversed_ok

        elif sweep.direction == "sweep_low":
            # Low sweep → 反転確認: 全 confirm バーの close > sweep.level_price
            reversed_ok = all(b.close > sweep.level_price for b in confirm_bars)

            if reversed_ok and atr > 0:
                # ATR フィルタ
                reversal_move = max(b.high for b in confirm_bars) - sweep.sweep_price
                reversed_ok = reversal_move >= self.reversal_atr_mult * atr

            if reversed_ok:
                sweep.confirmed = True
                log.info(
                    f"[LiquiditySweepDetector] reversal confirmed: "
                    f"LOW sweep at {sweep.level_price:.2f} → price rose"
                )
            return reversed_ok

        return False

    # ── エントリーシグナル取得 ────────────────────────────────────────────────

    def get_entry_signal(
        self,
        post_sweep_bars: list[BarSnapshot],
        atr:             float = 0.0,
    ) -> Optional[dict]:
        """
        Sweep + Reversal が確認されたエントリーシグナルを返す。

        chronos_strategy_selector.py の env["liquidity_sweep_signal"] として渡す。

        Returns:
            シグナルがある場合:
                {
                    "signal":      "long" | "short",
                    "level_type":  str,
                    "level_price": float,
                    "confidence":  float,
                    "reason":      str,
                }
            シグナルなしの場合: None
        """
        if self._pending_sweep is None:
            return None

        confirmed = self.is_reversal_confirmed(post_sweep_bars, atr)
        if not confirmed:
            return None

        sweep = self._pending_sweep

        # High sweep + reversal → SHORT エントリー (上への stop hunt 後の下落)
        # Low  sweep + reversal → LONG  エントリー (下への stop hunt 後の上昇)
        if sweep.direction == "sweep_high":
            signal_dir = "short"
        else:
            signal_dir = "long"

        # 信頼度: 出来高比率と ATR 突破量から算出
        confidence = min(
            0.5 + (sweep.volume_ratio - 2.0) * 0.1 + sweep.atr_breach * 0.1,
            0.90,
        )

        signal = {
            "signal":      signal_dir,
            "level_type":  sweep.level_type,
            "level_price": sweep.level_price,
            "confidence":  confidence,
            "reason":      (
                f"liquidity_sweep_reversal: {sweep.reason} "
                f"→ {signal_dir} conf={confidence:.2f}"
            ),
        }

        log.info(
            f"[LiquiditySweepDetector] entry signal: {signal_dir} "
            f"level={sweep.level_type}@{sweep.level_price:.2f} "
            f"confidence={confidence:.2f}"
        )

        # シグナル消費後はリセット
        self._pending_sweep   = None
        self._sweep_timestamp = None

        return signal

    # ── 状態 API ─────────────────────────────────────────────────────────────

    def has_pending_sweep(self) -> bool:
        """未確認の sweep シグナルがあるか。"""
        return self._pending_sweep is not None

    def get_pending_sweep(self) -> Optional[SweepSignal]:
        """未確認の sweep シグナルを返す。"""
        return self._pending_sweep

    def clear_pending(self) -> None:
        """未確認の sweep シグナルをクリアする (時間切れ時)。"""
        if self._pending_sweep is not None:
            log.info(
                f"[LiquiditySweepDetector] pending sweep cleared: "
                f"{self._pending_sweep.level_type}"
            )
        self._pending_sweep   = None
        self._sweep_timestamp = None

    def is_sweep_expired(self, current_ts: int) -> bool:
        """
        sweep が post_sweep_window_sec を超えて期限切れか確認する。

        chronos_rules.yaml: liquidity_sweep.post_sweep_window_sec = 30

        Args:
            current_ts: 現在の Unix timestamp (seconds)

        Returns:
            bool: True = 期限切れ (clear_pending() を呼ぶべき)
        """
        if self._sweep_timestamp is None:
            return False
        elapsed = current_ts - self._sweep_timestamp
        expired = elapsed > self.post_sweep_window_sec
        if expired:
            log.debug(
                f"[LiquiditySweepDetector] sweep expired: "
                f"elapsed={elapsed}s > window={self.post_sweep_window_sec}s"
            )
        return expired

    def get_levels(self) -> dict[str, float]:
        """現在の liquidity levels を返す。"""
        return dict(self._levels)
