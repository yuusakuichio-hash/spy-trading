"""atlas_v3/bots/engines/adaptive_monitor_interval.py — VIX適応型監視間隔 (L09)

設計思想
--------
高 VIX 環境では市場変動が速く、30 秒ポーリングでは止損が遅れる。
VIX レベルに応じて監視間隔を動的に変更する方式 B (方式 A の push ベースは
将来タスク)。

ソース一次情報
--------------
- research_remaining_gaps.md N7 項: "高VIX時に30s→10s"
- futu ストリーミング QUOTE callback (N7 方式 A・将来実装)
- 現行 spy_bot.py IntradayMonitor._vix_elevated_threshold 参照

実装 (atlas_v3 namespace・spy_bot.py 書換禁止)
----------------------------------------------
- get_interval(vix) → 秒数
- VIX < 20: 30s (通常)
- 20 <= VIX < 30: 15s (やや緊張)
- VIX >= 30: 10s (高緊張)
- テスト: 各閾値での間隔値確認
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# VIX 閾値→間隔 (秒) マッピング
_VIX_INTERVAL_MAP: list[tuple[float, int]] = [
    (20.0, 30),   # VIX < 20: 通常 30 秒
    (30.0, 15),   # 20 <= VIX < 30: 15 秒
    (float("inf"), 10),  # VIX >= 30: 10 秒
]

# デフォルト間隔 (VIX 取得失敗時)
DEFAULT_INTERVAL_SEC: int = 30


@dataclass(frozen=True)
class MonitorIntervalResult:
    """監視間隔計算結果 DTO。

    fields:
        interval_sec: 監視ポーリング間隔 (秒)
        vix: 入力 VIX 値 (alias: vix_input)
        regime: 内部 regime ラベル ("low_vol" / "elevated_vol" / "high_vol" / "extreme_vol" / "unknown")
        vix_band: 公開 band ラベル ("low" / "medium" / "high" / "unknown")
        vix_input: vix の alias (test/外部 API 互換)
        reason: 人間可読の判定根拠
    """
    interval_sec: int
    vix: float
    regime: str
    reason: str
    vix_band: str = "unknown"
    vix_input: float = 0.0


class AdaptiveMonitorInterval:
    """VIX 水準に応じて監視ポーリング間隔を動的に決定する。

    例::
        ami = AdaptiveMonitorInterval()
        result = ami.get_interval(vix=25.0)
        # result.interval_sec = 15
        time.sleep(result.interval_sec)
    """

    def get_interval(self, vix: Optional[float]) -> MonitorIntervalResult:
        """VIX から適切な監視間隔(秒)を返す。

        Parameters
        ----------
        vix: 現在の VIX 値 (None または 0.0 の場合はデフォルト)
        """
        if vix is None or vix <= 0.0:
            return MonitorIntervalResult(
                interval_sec=DEFAULT_INTERVAL_SEC,
                vix=vix or 0.0,
                regime="unknown",
                reason=f"VIX unavailable: default={DEFAULT_INTERVAL_SEC}s",
                vix_band="unknown",
                vix_input=vix or 0.0,
            )

        assert vix > 0, f"vix={vix} must be > 0"

        for threshold, interval in _VIX_INTERVAL_MAP:
            if vix < threshold:
                if threshold <= 20.0:
                    regime = "low_vol"
                    band = "low"
                elif threshold <= 30.0:
                    regime = "elevated_vol"
                    band = "medium"
                else:
                    regime = "high_vol"
                    band = "high"
                return MonitorIntervalResult(
                    interval_sec=interval,
                    vix=vix,
                    regime=regime,
                    reason=f"VIX={vix:.1f} -> interval={interval}s (< {threshold:.0f})",
                    vix_band=band,
                    vix_input=vix,
                )

        # VIX >= 最大閾値 (inf にはならないが safety)
        last_interval = _VIX_INTERVAL_MAP[-1][1]
        return MonitorIntervalResult(
            interval_sec=last_interval,
            vix=vix,
            regime="extreme_vol",
            reason=f"VIX={vix:.1f} >= max threshold: interval={last_interval}s",
            vix_band="high",
            vix_input=vix,
        )
