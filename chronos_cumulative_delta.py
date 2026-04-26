#!/usr/bin/env python3
"""
chronos_cumulative_delta.py — F12 Cumulative Delta 実装

定義: 市場への買い圧/売り圧の累積
  cumulative_delta = sum(buy_volume) - sum(sell_volume)

データソース (ThetaData Pro不要):
  1. Tradovate tick stream: aggressor side (bid fill=sell / ask fill=buy)
  2. 代替 (tick なし): 1分足 close-open 方向 × volume を approximation として使用
     (精度は下がるが F12 スコアリング要件は満たす)

戦略への統合:
  chronos_strategy_selector.py の select_futures_strategy() が
  env["cumulative_delta"] を参照して戦術スコアを調整する。

参照:
  - chronos_rules.yaml: cumulative_delta セクション
  - chronos_strategy_selector.py: cumulative_delta 統合
  - tests/test_f12_f13_implementation_20260419.py
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime as _datetime
from typing import Optional, Union

log = logging.getLogger(__name__)


# ── N-C2: timestamp 変換ヘルパー ─────────────────────────────────────────────

def _to_timestamp(ts: Union[str, int, float]) -> int:
    """
    様々なフォーマットの timestamp を Unix int (秒) に変換する。

    対応フォーマット:
      - ISO8601 文字列: "2026-04-19T09:35:00Z" / "2026-04-19T09:35:00+00:00" etc.
      - int / float: そのまま int にキャスト

    不正値は ValueError を raise する（握り潰し禁止）。

    Args:
        ts: 変換対象のタイムスタンプ

    Returns:
        int: Unix タイムスタンプ (秒)

    Raises:
        ValueError: 変換不能な型/値の場合
        TypeError: ts が str/int/float 以外の場合
    """
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        try:
            # ISO8601: "Z" を "+00:00" に置換して fromisoformat で解析
            normalized = ts.replace("Z", "+00:00")
            dt = _datetime.fromisoformat(normalized)
            return int(dt.timestamp())
        except ValueError as e:
            raise ValueError(
                f"_to_timestamp: ISO8601 parse failed: ts={ts!r} error={e}"
            ) from e
    raise TypeError(
        f"_to_timestamp: unsupported type {type(ts).__name__}: ts={ts!r}"
    )


# ── データクラス ─────────────────────────────────────────────────────────────

@dataclass
class Tick:
    """単一ティックデータ。"""
    price: float
    volume: float
    aggressor_side: str  # "buy" | "sell" | "unknown"
    timestamp: Optional[int] = None  # C1修正: Unix timestamp (seconds)。バケット境界判定に使用


@dataclass
class BarData:
    """1分足バーデータ (tick なしケースの代替入力)。"""
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: int  # Unix timestamp (seconds)


@dataclass
class BucketDelta:
    """1バケット分の Cumulative Delta 集計結果。"""
    timestamp: int          # バケット開始 Unix timestamp
    buy_volume: float       # 買い約定出来高合計
    sell_volume: float      # 売り約定出来高合計
    delta: float            # buy_volume - sell_volume
    cumulative: float       # 日次累積 delta (日次 reset 適用後)


# ── メインクラス ─────────────────────────────────────────────────────────────

class CumulativeDelta:
    """
    Cumulative Delta の計算・管理クラス。

    使用方法:
        cd = CumulativeDelta(bucket_minutes=5)

        # ケース1: Tradovate tick stream 使用
        for tick in tick_stream:
            cd.update(tick)

        # ケース2: 1分足バー使用 (tick なし代替)
        for bar in bars:
            cd.update_from_bar(bar)

        # 現在の delta 取得
        current = cd.get_current_delta()

        # 乖離検出
        signal = cd.detect_divergence(price_series, delta_series)
    """

    def __init__(self, bucket_minutes: int = 5, max_buckets: int = 78):
        """
        Args:
            bucket_minutes: バケット幅 (分)。chronos_rules.yaml の bucket_minutes に対応
            max_buckets:    保持する最大バケット数 (デフォルト: RTH 6.5h = 78 バケット)
        """
        self.bucket_minutes = bucket_minutes
        self.max_buckets = max_buckets

        # 現在バケット
        self._current_buy_vol: float = 0.0
        self._current_sell_vol: float = 0.0
        self._current_bucket_ts: Optional[int] = None

        # 日次累積
        self._daily_cumulative: float = 0.0

        # バケット履歴 (deque で max_buckets を維持)
        self._buckets: deque[BucketDelta] = deque(maxlen=max_buckets)

        # N-C3: 同バー二重計上防止セット（daily_reset でクリア）
        self._processed_ts: set[int] = set()

        log.info(
            f"[CumulativeDelta] init: bucket={bucket_minutes}min "
            f"max_buckets={max_buckets}"
        )

    # ── tick 更新 ──────────────────────────────────────────────────────────────

    def update(self, tick: Tick) -> None:
        """
        Tradovate tick stream からのリアルタイム更新。

        aggressor_side が "unknown" の場合は volume を 0.5/0.5 で按分する。
        tick.timestamp が設定されている場合、バケット境界を判定してflushする (C1修正)。
        """
        if tick.aggressor_side == "buy":
            buy_vol  = tick.volume
            sell_vol = 0.0
        elif tick.aggressor_side == "sell":
            buy_vol  = 0.0
            sell_vol = tick.volume
        else:
            # unknown: 按分
            buy_vol  = tick.volume * 0.5
            sell_vol = tick.volume * 0.5

        # C1修正(改): C2と同じ順序に統一
        # 先にバケット境界判定→flush → その後加算（先に加算するとflush時に混入する）
        # N-C2: _to_timestamp でISO8601/int/float を統一変換
        if tick.timestamp is not None:
            self._maybe_flush_bucket(_to_timestamp(tick.timestamp))

        self._current_buy_vol  += buy_vol
        self._current_sell_vol += sell_vol

        log.debug(
            f"[CumulativeDelta] tick: price={tick.price:.2f} "
            f"vol={tick.volume:.0f} side={tick.aggressor_side} "
            f"buy_vol={self._current_buy_vol:.0f} sell_vol={self._current_sell_vol:.0f}"
        )

    # ── 1分足バー代替更新 ────────────────────────────────────────────────────

    def update_from_bar(self, bar: BarData) -> None:
        """
        1分足バーから Cumulative Delta を近似計算する (tick なし代替)。

        N-C3: 同バー二重計上防止
          bar.timestamp が既に _processed_ts に含まれる場合は即 return。

        近似ロジック:
          close > open → 買い主導 → buy_volume = volume * 0.7, sell = 0.3
          close < open → 売り主導 → buy_volume = volume * 0.3, sell = 0.7
          close == open → 中立 → 0.5 / 0.5 按分

        精度はティック比較で劣るが、方向性の傾向は捉えられる。
        """
        # N-C3: timestamp dedupe（同バー二重計上防止）
        bar_ts = _to_timestamp(bar.timestamp)
        if bar_ts in self._processed_ts:
            log.debug(
                f"[CumulativeDelta] update_from_bar: duplicate ts={bar_ts} skipped"
            )
            return
        self._processed_ts.add(bar_ts)

        if bar.close > bar.open:
            buy_ratio  = 0.7
            sell_ratio = 0.3
        elif bar.close < bar.open:
            buy_ratio  = 0.3
            sell_ratio = 0.7
        else:
            buy_ratio  = 0.5
            sell_ratio = 0.5

        # バーの body/range 比率で重み付け (強い方向性はさらに傾ける)
        body   = abs(bar.close - bar.open)
        rng    = bar.high - bar.low if bar.high > bar.low else 1.0
        body_ratio = min(body / rng, 1.0)

        # 強い方向性 (body/range > 0.7) は比率をさらに拡大
        if body_ratio > 0.7:
            if bar.close > bar.open:
                buy_ratio  = min(0.85, buy_ratio + body_ratio * 0.1)
                sell_ratio = 1.0 - buy_ratio
            else:
                sell_ratio = min(0.85, sell_ratio + body_ratio * 0.1)
                buy_ratio  = 1.0 - sell_ratio

        buy_vol  = bar.volume * buy_ratio
        sell_vol = bar.volume * sell_ratio

        # C2修正: 先に境界判定→flush → そのあと新バー加算（逆転で二重計上を防ぐ）
        # N-C2: bar_ts は _to_timestamp で変換済み
        self._maybe_flush_bucket(bar_ts)

        # 内部ボリュームを直接加算（aggressor side の按分をスキップ）
        self._current_buy_vol  += buy_vol
        self._current_sell_vol += sell_vol

        log.debug(
            f"[CumulativeDelta] bar approx: ts={bar.timestamp} "
            f"O={bar.open:.2f} C={bar.close:.2f} vol={bar.volume:.0f} "
            f"buy={buy_vol:.0f} sell={sell_vol:.0f}"
        )

    def _maybe_flush_bucket(self, current_ts: int) -> None:
        """
        タイムスタンプが新しいバケット境界を超えたら現在バケットを確定する。
        """
        bucket_sec = self.bucket_minutes * 60

        if self._current_bucket_ts is None:
            # 初回: バケット開始タイムスタンプを設定
            self._current_bucket_ts = (current_ts // bucket_sec) * bucket_sec
            return

        expected_next = self._current_bucket_ts + bucket_sec
        if current_ts >= expected_next:
            self._flush_bucket()
            self._current_bucket_ts = (current_ts // bucket_sec) * bucket_sec

    def _flush_bucket(self) -> None:
        """現在バケットを確定して履歴に追加する。"""
        if self._current_bucket_ts is None:
            return

        bucket_delta = self._current_buy_vol - self._current_sell_vol
        self._daily_cumulative += bucket_delta

        bucket = BucketDelta(
            timestamp   = self._current_bucket_ts,
            buy_volume  = self._current_buy_vol,
            sell_volume = self._current_sell_vol,
            delta       = bucket_delta,
            cumulative  = self._daily_cumulative,
        )
        self._buckets.append(bucket)

        log.info(
            f"[CumulativeDelta] bucket flushed: ts={self._current_bucket_ts} "
            f"buy={self._current_buy_vol:.0f} sell={self._current_sell_vol:.0f} "
            f"delta={bucket_delta:.0f} cumulative={self._daily_cumulative:.0f}"
        )

        self._current_buy_vol  = 0.0
        self._current_sell_vol = 0.0

    # ── 日次 reset ───────────────────────────────────────────────────────────

    def daily_reset(self) -> None:
        """
        日次リセット。RTH 9:30 ET 開始時に呼ぶ。

        chronos_rules.yaml: cumulative_delta.daily_reset_et = "09:30"
        """
        self._daily_cumulative = 0.0
        self._current_buy_vol  = 0.0
        self._current_sell_vol = 0.0
        self._current_bucket_ts = None
        self._buckets.clear()
        # N-C3: 同バー dedupe セットをクリア
        self._processed_ts.clear()
        log.info("[CumulativeDelta] daily reset complete")

    # ── 取得 API ─────────────────────────────────────────────────────────────

    def get_current_delta(self) -> float:
        """
        現在の確定済み日次 Cumulative Delta を返す。

        現在バケット (未確定分) は含まない。
        未確定分も含む合計は get_total_delta() を使う。

        Returns:
            float: 日次累積 delta。リセット直後は 0.0
        """
        return self._daily_cumulative

    def get_current_bucket_delta(self) -> float:
        """現在バケット (未確定) の暫定 delta を返す。"""
        return self._current_buy_vol - self._current_sell_vol

    def get_total_delta(self) -> float:
        """
        HIGH-1修正: 確定済み日次 delta + 現在バケット (未確定) delta の合算を返す。

        strategy_selector や判断ロジックでは確定済みのみでなく
        現在進行中のバケットも考慮すべきため、このメソッドを使う。

        Returns:
            float: 確定済み + 未確定合算 delta
        """
        return self._daily_cumulative + (self._current_buy_vol - self._current_sell_vol)

    def get_bucket_delta(self, minutes: int) -> float:
        """
        直近 N 分間の Cumulative Delta 合計を返す。

        Args:
            minutes: 直近 N 分間 (bucket_minutes の倍数推奨)

        Returns:
            float: 直近 N 分間のデルタ合計
        """
        n_buckets = max(1, minutes // self.bucket_minutes)
        recent = list(self._buckets)[-n_buckets:]

        if not recent:
            return 0.0

        total = sum(b.delta for b in recent)
        log.debug(
            f"[CumulativeDelta] get_bucket_delta({minutes}min): "
            f"n_buckets={n_buckets} recent_count={len(recent)} total={total:.0f}"
        )
        return total

    def get_buckets(self) -> list[BucketDelta]:
        """確定済みバケット一覧を返す (古い順)。"""
        return list(self._buckets)

    # ── 乖離検出 ─────────────────────────────────────────────────────────────

    def detect_divergence(
        self,
        price_series:  list[float],
        delta_series:  list[float],
        threshold:     float = 0.3,
    ) -> str:
        """
        価格と Cumulative Delta の乖離を検出する。

        乖離パターン:
          - bullish_divergence: 価格が下落しているが Delta が上昇 → 隠れた買い圧
          - bearish_divergence: 価格が上昇しているが Delta が下落 → 隠れた売り圧
          - aligned:            価格と Delta が同方向 → トレンド確認
          - insufficient_data:  データ不足

        Args:
            price_series: 直近の価格系列 (古い順)
            delta_series: 直近の delta 系列 (古い順)
            threshold:    乖離判定の感度 (0.0-1.0)。
                         chronos_rules.yaml: cumulative_delta.divergence_threshold

        Returns:
            "bullish_divergence" | "bearish_divergence" | "aligned" | "insufficient_data"
        """
        if len(price_series) < 2 or len(delta_series) < 2:
            return "insufficient_data"

        if len(price_series) != len(delta_series):
            log.warning(
                f"[CumulativeDelta] detect_divergence: length mismatch "
                f"price={len(price_series)} delta={len(delta_series)}"
            )
            return "insufficient_data"

        # 最新値と先頭値の差分で方向性を判定
        price_change = price_series[-1] - price_series[0]
        delta_change = delta_series[-1] - delta_series[0]

        # M3: 変化ゼロ判定（両方ゼロのみ aligned。片方ゼロは divergence 候補として継続）
        # 旧: price_change=0 OR delta_change=0 → "aligned"（過剰短絡）
        # 新: 両方ゼロのみ → "aligned"。価格無変動+delta大変化はdivergence候補として扱う。
        if abs(price_change) < 1e-8 and abs(delta_change) < 1e-8:
            return "aligned"  # 両方ゼロ → 変化なし → 乖離なし
        # 価格のみゼロ: delta変化は bullish/bearish divergence の根拠になりうる
        # delta のみゼロ: 価格変化のみ → aligned（delta確認なし）
        if abs(delta_change) < 1e-8:
            return "aligned"  # delta変化なし → 方向性判定不可 → aligned
        # 価格変化がゼロ（価格停滞）+delta大変化: divergence 候補として判定継続
        # price_change=0 の場合 price_dir は正の方向をデフォルトとし、
        # delta_dir との組み合わせで divergence を検出する

        # NEW-H1: z-score 正規化（価格・デルタを同じスケールに統一）
        # price_series と delta_series それぞれの標準偏差で割る。
        # 標準偏差がゼロ（定数系列）の場合は絶対変化量をそのまま使用（安全フォールバック）。
        import statistics as _stats

        def _zscore_change(series: list, change: float) -> float:
            """系列の標準偏差でスケーリング。SD=0 なら change / abs_mean フォールバック。"""
            if len(series) < 2:
                return change
            try:
                sd = _stats.pstdev(series)
                if sd > 1e-10:
                    return change / sd
                # SD ≈ 0 → 絶対変化を |mean| でスケール（定数系列では0を返す）
                mean_abs = abs(sum(series) / len(series))
                return (change / mean_abs) if mean_abs > 1e-10 else 0.0
            except Exception:
                return change

        price_z = _zscore_change(price_series, price_change)
        delta_z = _zscore_change(delta_series, delta_change)

        # 方向性: +1 = 上昇, -1 = 下落, 0 = 停滞（価格変化ゼロ）
        # M3: price_change=0（価格停滞）は direction=0 として divergence 判定に組み込む
        price_dir = 1 if price_change > 0 else (-1 if price_change < 0 else 0)
        delta_dir = 1 if delta_change > 0 else (-1 if delta_change < 0 else 0)

        if price_dir == delta_dir and price_dir != 0:
            # 方向一致（停滞ゼロ以外）→ aligned
            divergence = "aligned"
        elif price_dir == 0:
            # M3: 価格停滞（price_change=0）+ delta 変化 → divergence 候補
            # delta が上昇なら bullish divergence（価格が動かないのに買い圧増大）
            # delta が下落なら bearish divergence（価格が動かないのに売り圧増大）
            diff = abs(delta_z)  # price_z ≈ 0 なので delta_z の絶対値で判定
            if delta_dir > 0:
                divergence = "bullish_divergence" if diff >= threshold else "aligned"
            else:
                divergence = "bearish_divergence" if diff >= threshold else "aligned"
        elif price_dir < 0 and delta_dir > 0:
            # 価格下落 + Delta 上昇 → 買い圧が隠れている
            diff = abs(price_z - delta_z)
            divergence = "bullish_divergence" if diff >= threshold else "aligned"
        else:
            # 価格上昇 + Delta 下落 → 売り圧が隠れている
            diff = abs(price_z - delta_z)
            divergence = "bearish_divergence" if diff >= threshold else "aligned"

        log.info(
            f"[CumulativeDelta] divergence(zscore): "
            f"price_z={price_z:.3f}({price_dir:+}) "
            f"delta_z={delta_z:.3f}({delta_dir:+}) "
            f"diff={abs(price_z-delta_z):.3f} threshold={threshold} "
            f"→ {divergence}"
        )
        return divergence

    # ── 戦略スコア統合 ────────────────────────────────────────────────────────

    def get_strategy_bias(
        self,
        price_series: list[float],
        threshold:    float = 0.3,
    ) -> dict:
        """
        Cumulative Delta から戦略バイアスを返す。

        chronos_strategy_selector.select_futures_strategy() の
        env["cumulative_delta_bias"] として渡す。

        Returns:
            {
                "bias":        "bullish" | "bearish" | "neutral",
                "current":     float,   # 日次累積 delta
                "recent_5m":   float,   # 直近5分 delta
                "divergence":  str,     # detect_divergence の結果
                "confidence":  float,   # 0.0-1.0
            }
        """
        current  = self.get_current_delta()
        recent5m = self.get_bucket_delta(5)
        buckets  = self.get_buckets()

        if len(buckets) < 2:
            return {
                "bias":       "neutral",
                "current":    current,
                "recent_5m":  recent5m,
                "divergence": "insufficient_data",
                "confidence": 0.0,
            }

        delta_series = [b.delta for b in buckets[-10:]]
        divergence   = self.detect_divergence(price_series[-10:], delta_series, threshold)

        # バイアス判定
        if current > 0 and recent5m > 0:
            bias = "bullish"
        elif current < 0 and recent5m < 0:
            bias = "bearish"
        else:
            bias = "neutral"

        # 信頼度 (日次 delta の絶対値で正規化・最大値 10000 で 1.0)
        confidence = min(abs(current) / 10_000.0, 1.0)

        return {
            "bias":       bias,
            "current":    current,
            "recent_5m":  recent5m,
            "divergence": divergence,
            "confidence": confidence,
        }


# ── bid_volume / ask_volume 取得ユーティリティ ────────────────────────────────

def calc_bid_ask_delta(bid_volume: float, ask_volume: float) -> float:
    """
    DOM の bid/ask 出来高から delta を近似する。

    Tradovate Market Depth (MD) WebSocket 経由で取得可能。
    ask_volume (買い約定) - bid_volume (売り約定)

    Args:
        bid_volume: bid 側の出来高 (売り約定)
        ask_volume: ask 側の出来高 (買い約定)

    Returns:
        float: delta (正 = 買い超, 負 = 売り超)
    """
    delta = ask_volume - bid_volume
    log.debug(
        f"[CumulativeDelta] bid_ask_delta: "
        f"ask={ask_volume:.0f} bid={bid_volume:.0f} delta={delta:.0f}"
    )
    return delta


def calc_volume_ratio(buy_volume: float, sell_volume: float) -> float:
    """
    買い/売り出来高比率を計算する (delta_sign の代替指標)。

    Args:
        buy_volume:  買い約定出来高
        sell_volume: 売り約定出来高

    Returns:
        float: buy_volume / (buy_volume + sell_volume)
               0.5 = 均衡、>0.5 = 買い超、<0.5 = 売り超
    """
    total = buy_volume + sell_volume
    if total <= 0:
        return 0.5
    return buy_volume / total
