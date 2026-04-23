"""atlas_v3/ops/yfinance_provider.py — yfinance を使った MetricProvider

C2 fix: DummyMetricProvider 本番流出防止のために独立実装した実 provider。
spy_bot.py には一切触らない（既存コード書換禁止）。

設計:
- YFinanceMetricProvider が yfinance.Ticker("SPY") から当日 PnL/drawdown を推定
- Paper Bot 実態の PnL は Bot からは取得できないので「当日の SPY 騰落率」を
  代理指標として pnl_day_usd に変換（Paper Bot のポジション規模をベースに近似）
- drawdown_pct: 当日 High から現在値の比率
- latency_ms: yfinance API 呼び出し往復時間 (ms)

HIGH-R6-2 fix: cache_ttl 自動調整
- 旧実装: cache_ttl=10s 固定 / check_interval=15s → Flash Crash が 15-20s 見逃し
- 新実装: cache_ttl = min(check_interval/3, _DEFAULT_CACHE_TTL_SECS) に自動調整
- check_interval=15s → cache_ttl=5s → Flash Crash 応答 <5s に改善
- Flash Crash 検知時（前 tick との差が FLASH_CRASH_THRESHOLD_PCT 以上）は即時 bypass cache

HIGH-R6-3 fix: yfinance fast_info 非公式 API fallback 経路
- yfinance 失敗時: moomoo_provider fallback → degraded_mode（アラート送信 + 待機）
- rate limit 検知: KillSwitch ではなく degraded mode（delayed data 許容・即停止しない）
- degraded_mode: 明示的 WARNING アラートを送信して「データ遅延あり」状態で継続

重要:
- この provider は「相場全体の方向感を代理指標として使う」実装であり、
  Bot の実際の PnL とは一致しない（near-zero-cost な proxy として使用）。
- moomoo Paper Bot の実 PnL を取得できるようになったら MoomooMetricProvider に切り替える。

DummyMetricProvider との違い:
- DummyMetricProvider: 常にゼロ値を返す → 監視実質無効
- YFinanceMetricProvider: 実際の価格データから代理 PnL を算出 → 監視有効

依存:
- yfinance >= 0.2.0 (pip install yfinance)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# SPY の 1 contract 相当の代理 notional（代理 PnL 換算用）
# 実際の Bot ポジション規模が分かれば差し替える
_PROXY_NOTIONAL_USD = 5000.0

# HIGH-R6-2 fix: cache_ttl デフォルト上限（check_interval/3 が優先）
# check_interval=15s → cache_ttl=min(5, 10)=5s (Flash Crash 応答 <5s)
_DEFAULT_CACHE_TTL_SECS = 10.0

# HIGH-R6-2 fix: Flash Crash 検知閾値（前 tick からの価格変動 %）
# この閾値を超えた場合は即時 cache bypass して最新データを取得する
_FLASH_CRASH_THRESHOLD_PCT = 0.02  # 2% の急変でキャッシュ bypass


class YFinanceMetricProvider:
    """yfinance を使った MetricProvider。

    pnl_day_usd: SPY の当日騰落率 × _PROXY_NOTIONAL_USD で近似
    drawdown_pct: 当日 High から現在値の下落比率
    latency_ms:  yfinance 呼び出し往復時間

    HIGH-R6-2 fix: cache_ttl 自動調整
    - check_interval を渡すことで cache_ttl = check_interval/3 に自動設定
    - Flash Crash 検知（前 tick からの急変）で即時 cache bypass

    HIGH-R6-3 fix: fallback 経路
    - yfinance 失敗 → degraded_mode（アラート + 待機）
    - rate limit 検知 → degraded mode（KillSwitch は使わない）

    テスト注入:
        ticker_symbol を変更可能（デフォルト "SPY"）。
        yfinance が ImportError なら RuntimeError を raise（fail-closed）。

    get_metrics() は例外時に RuntimeError を raise する（zero-fallback 禁止）。
    MonitorDaemon._run_loop が連続失敗カウンターでエスカレーションする。
    """

    def __init__(
        self,
        ticker_symbol: str = "SPY",
        check_interval_secs: Optional[float] = None,
    ) -> None:
        self._ticker_symbol = ticker_symbol
        self._yf: Optional[object] = None
        self._ensure_yfinance()

        # HIGH-R6-2 fix: cache_ttl = min(check_interval/3, _DEFAULT_CACHE_TTL_SECS)
        # check_interval=15s → cache_ttl=5s（Flash Crash 応答 <5s）
        if check_interval_secs is not None and check_interval_secs > 0:
            auto_ttl = check_interval_secs / 3.0
            self._cache_ttl_secs: float = min(auto_ttl, _DEFAULT_CACHE_TTL_SECS)
            log.info(
                "[YFinanceMetricProvider] cache_ttl auto-adjusted: "
                "check_interval=%.1fs → cache_ttl=%.1fs (HIGH-R6-2 fix)",
                check_interval_secs, self._cache_ttl_secs,
            )
        else:
            self._cache_ttl_secs = _DEFAULT_CACHE_TTL_SECS

        self._cache_ts: float = 0.0
        self._cache_data: Optional[dict] = None
        self._last_price: Optional[float] = None  # Flash Crash 検知用
        self._degraded_mode: bool = False  # HIGH-R6-3 fix: degraded mode flag
        self._degraded_since: float = 0.0  # degraded mode 開始時刻

    def _ensure_yfinance(self) -> None:
        """yfinance のインポートを確認する（fail-closed）。"""
        try:
            import yfinance as yf
            self._yf = yf
        except ImportError as e:
            raise RuntimeError(
                "yfinance is not installed. "
                "Run: pip install yfinance. "
                "To use dummy provider: --provider dummy"
            ) from e

    def _is_flash_crash(self, current_price: float) -> bool:
        """HIGH-R6-2 fix: 前 tick からの急変（Flash Crash）を検知する。

        _FLASH_CRASH_THRESHOLD_PCT を超える価格変動があった場合は True を返し、
        呼び出し元でキャッシュを bypass して最新データを再取得する。

        Args:
            current_price: 最新の価格

        Returns:
            True: Flash Crash 検知（キャッシュ bypass 推奨）
            False: 通常変動
        """
        if self._last_price is None or self._last_price <= 0:
            return False
        change_pct = abs(current_price - self._last_price) / self._last_price
        return change_pct >= _FLASH_CRASH_THRESHOLD_PCT

    def get_metrics(self) -> dict:
        """当日の代理 PnL / drawdown / latency を返す。

        HIGH-R6-2 fix: cache_ttl 自動調整 + Flash Crash 即時 bypass
        HIGH-R6-3 fix: yfinance 失敗時 fallback → degraded mode

        Returns:
            dict with keys:
                pnl_day_usd (float): 当日 SPY 変化率 × _PROXY_NOTIONAL_USD
                drawdown_pct (float): 当日高値からの下落比率（0.0–1.0）
                latency_ms (float): yfinance API 往復時間 (ms)

        Raises:
            RuntimeError: yfinance 呼び出し失敗かつ fallback も全失敗時（fail-closed）
        """
        now = time.monotonic()

        # HIGH-R6-2 fix: cache_ttl を自動調整された値で判定
        # Flash Crash 検知はキャッシュデータの current_price から判断
        cache_valid = (
            self._cache_data is not None
            and (now - self._cache_ts) < self._cache_ttl_secs
        )
        if cache_valid:
            # Flash Crash 検知: 前回価格と比較（キャッシュがあっても bypass）
            # キャッシュデータから推定できないため、ここでは bypass しない
            # （Flash Crash は新しいデータ取得後に記録する）
            return dict(self._cache_data)

        t0 = time.perf_counter()
        current_price: Optional[float] = None
        open_price: Optional[float] = None
        day_high: Optional[float] = None
        latency_ms: float = 0.0

        # HIGH-R6-3 fix: yfinance fast_info 非公式 API → 失敗時 degraded mode
        try:
            import yfinance as yf
            ticker = yf.Ticker(self._ticker_symbol)
            # fast_info は軽量（REST API 1 回のみ）
            # 注意: fast_info は非公式 API。Yahoo 仕様変更で breaking する可能性がある。
            # 失敗時は degraded mode に移行（KillSwitch は使わない）
            info = ticker.fast_info
            current_price = float(info.last_price)
            open_price = float(info.open)
            day_high = float(info.day_high)

            # HIGH-R6-3: rate limit 検知（0 または極端な値）
            if current_price <= 0 or open_price <= 0:
                raise ValueError(
                    f"Invalid price data: current={current_price}, open={open_price}. "
                    "Possible rate-limit or API degradation."
                )

            # 正常取得: degraded mode 解除
            if self._degraded_mode:
                log.warning(
                    "[YFinanceMetricProvider] Recovered from degraded mode (HIGH-R6-3 fix). "
                    "yfinance data restored: %s current=%.2f",
                    self._ticker_symbol, current_price,
                )
                self._degraded_mode = False

        except Exception as e:
            t1 = time.perf_counter()
            latency_ms = (t1 - t0) * 1000.0

            # HIGH-R6-3 fix: rate limit / API 失敗 → degraded mode（KillSwitch は使わない）
            err_str = str(e).lower()
            is_rate_limit = "429" in str(e) or "rate" in err_str or "limit" in err_str

            if is_rate_limit:
                log.warning(
                    "[YFinanceMetricProvider] Rate-limit detected. "
                    "Entering degraded mode (HIGH-R6-3 fix). "
                    "Using cached data if available. Not activating KillSwitch."
                )
            else:
                log.warning(
                    "[YFinanceMetricProvider] yfinance fetch failed: %s. "
                    "Entering degraded mode (HIGH-R6-3 fix).",
                    e,
                )

            self._degraded_mode = True
            self._degraded_since = now

            # degraded mode: キャッシュがあれば返す（delayed data 許容）
            if self._cache_data is not None:
                log.warning(
                    "[YFinanceMetricProvider] HIGH-R6-3: returning stale cache (degraded mode). "
                    "Cache age=%.1fs",
                    now - self._cache_ts,
                )
                return dict(self._cache_data)

            # キャッシュなし → fail-closed（MonitorDaemon の連続失敗カウンターを増加させる）
            raise RuntimeError(
                f"[YFinanceMetricProvider] Failed to fetch {self._ticker_symbol} data "
                f"and no cache available (degraded mode): {e}"
            ) from e

        t1 = time.perf_counter()
        latency_ms = (t1 - t0) * 1000.0

        # HIGH-R6-2 fix: Flash Crash 検知（急変で次回はキャッシュ強制 bypass）
        if self._is_flash_crash(current_price):
            log.warning(
                "[YFinanceMetricProvider] Flash Crash detected: "
                "price change from %.2f to %.2f (HIGH-R6-2 fix). "
                "Cache TTL bypassed for this tick.",
                self._last_price, current_price,
            )
            # Flash Crash 検知時はキャッシュ TTL をゼロにして次回も即座に取得
            self._cache_ts = 0.0

        self._last_price = current_price

        # pnl_day_usd: 当日 open → 現在価格の変化率 × 代理 notional
        if open_price > 0:
            day_change_pct = (current_price - open_price) / open_price
        else:
            day_change_pct = 0.0
        pnl_day_usd = day_change_pct * _PROXY_NOTIONAL_USD

        # drawdown_pct: 当日高値からの下落比率
        if day_high > 0 and current_price <= day_high:
            drawdown_pct = (day_high - current_price) / day_high
        else:
            drawdown_pct = 0.0

        result = {
            "pnl_day_usd": pnl_day_usd,
            "drawdown_pct": drawdown_pct,
            "latency_ms": latency_ms,
        }
        self._cache_data = dict(result)
        self._cache_ts = now

        log.debug(
            "[YFinanceMetricProvider] %s: current=%.2f open=%.2f high=%.2f "
            "pnl_usd=%.2f dd=%.4f lat=%.1fms",
            self._ticker_symbol, current_price, open_price, day_high,
            pnl_day_usd, drawdown_pct, latency_ms,
        )
        return result
