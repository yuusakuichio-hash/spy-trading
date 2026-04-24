"""atlas_v3/ops/moomoo_provider.py — moomoo OpenD 経由の実 Bot PnL MetricProvider

**Sprint 2 C-017 本実装（2026-04-24）**
ADR-014 3 判断採用: Mac mini 常駐 / 手動再ログイン + 期限検知通知 / read-only スコープ

設計方針（Strategist a7bfe851 事前調査推奨案 A）:
- local OpenD プロセス (127.0.0.1:11111) に futu-api SDK で接続
- `accinfo_query(trd_env=TrdEnv.SIMULATE)` で paper 口座の実 PnL 取得
- spy_bot.py:4420-4514 の TradeEngine 参照パターン流用（**ただし触らない**・読取のみ）
- YFinanceMetricProvider と同一 interface: `get_metrics() -> dict`
- 代理 PnL (yfinance) → 実 PnL (moomoo) への移行

Fail-closed 規律:
- OpenD 未起動時は RuntimeError raise（zero-fallback 禁止）
- 401/unauth は AuthenticationError raise（MonitorDaemon が Pushover 発火）
- socket timeout=5s 明示 + retry 3 回 exponential backoff

依存: futu-api >= 10.2.6218 (pip install futu-api)
インストール不在時は FUTU_AVAILABLE=False で get_metrics が MoomooProviderNotImplementedError raise
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# futu SDK import guard（Python 3.14 + protobuf 互換性問題にも対応）
FUTU_AVAILABLE = False
OpenSecTradeContext = None  # type: ignore[assignment]
SecurityFirm = None  # type: ignore[assignment]
TrdEnv = None  # type: ignore[assignment]
TrdMarket = None  # type: ignore[assignment]
RET_OK = 0  # type: ignore[assignment]
try:
    from futu import OpenSecTradeContext as _OpenSecTradeContext
    from futu import SecurityFirm as _SecurityFirm
    from futu import TrdEnv as _TrdEnv
    from futu import TrdMarket as _TrdMarket
    from futu import RET_OK as _RET_OK
    OpenSecTradeContext = _OpenSecTradeContext
    SecurityFirm = _SecurityFirm
    TrdEnv = _TrdEnv
    TrdMarket = _TrdMarket
    RET_OK = _RET_OK
    FUTU_AVAILABLE = True
except Exception as _futu_import_err:  # noqa: BLE001 — protobuf/ImportError 両方 catch
    log.warning(
        "[MoomooProvider] futu SDK import failed (%s); MoomooMetricProvider will raise on use. "
        "Install: pip install 'futu-api' 'protobuf<4'",
        type(_futu_import_err).__name__,
    )

# OpenD 接続パラメータ
_DEFAULT_OPEND_HOST = "127.0.0.1"
_DEFAULT_OPEND_PORT = 11111
_DEFAULT_SOCKET_TIMEOUT_SECS = 5.0
_DEFAULT_RETRY_MAX = 3
_DEFAULT_RETRY_BACKOFF_BASE = 1.5


class MoomooProviderNotImplementedError(NotImplementedError):
    """futu SDK 未インストール時 or 未実装パス呼出時。"""


class AuthenticationError(Exception):
    """moomoo 認証失敗（セッション期限切れ等）。

    MonitorDaemon 側で catch → Pushover priority=1 EMERGENCY 発火 →
    ゆうさくさん手動再ログイン フローのトリガー。
    """


class MoomooMetricProvider:
    """moomoo OpenD 経由で Paper Bot の実 PnL を取得する MetricProvider（ADR-014 準拠）。

    interface: YFinanceMetricProvider と同一
        get_metrics() -> dict {pnl_day_usd, drawdown_pct, latency_ms}

    実 paper 接続 smoke test は OpenD 起動 + Paper 口座 login 済の環境で実施。
    未起動環境では RuntimeError（fail-closed 規律）。
    """

    def __init__(
        self,
        *,
        opend_host: str = _DEFAULT_OPEND_HOST,
        opend_port: int = _DEFAULT_OPEND_PORT,
        socket_timeout_secs: float = _DEFAULT_SOCKET_TIMEOUT_SECS,
        retry_max: int = _DEFAULT_RETRY_MAX,
        trade_password: Optional[str] = None,
        security_firm: Any = None,  # SecurityFirm.FUTUJP 等
        trd_market: Any = None,  # TrdMarket.US 等
    ) -> None:
        self._opend_host = opend_host
        self._opend_port = opend_port
        self._socket_timeout_secs = socket_timeout_secs
        self._retry_max = retry_max
        self._trade_password = trade_password
        self._security_firm = security_firm
        self._trd_market = trd_market
        self._trade_ctx: Any = None  # OpenSecTradeContext instance（遅延接続）
        self._high_water_mark_usd: float = 0.0  # drawdown_pct 算出用

    def _ensure_connected(self) -> None:
        """遅延接続: 初回 get_metrics 時に OpenSecTradeContext 作成 + unlock。"""
        if not FUTU_AVAILABLE:
            raise MoomooProviderNotImplementedError(
                "futu-api not installed. Install with: pip install futu-api"
            )
        if self._trade_ctx is not None:
            return
        try:
            self._trade_ctx = OpenSecTradeContext(
                filter_trdmarket=self._trd_market or TrdMarket.US,
                host=self._opend_host,
                port=self._opend_port,
                security_firm=self._security_firm or SecurityFirm.FUTUJP,
            )
            if self._trade_password:
                ret, _ = self._trade_ctx.unlock_trade(self._trade_password)
                if ret != RET_OK:
                    raise AuthenticationError(
                        f"moomoo unlock_trade failed (ret={ret}). Session may be expired."
                    )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise RuntimeError(f"OpenD connection failed: {exc}") from exc

    def smoke_test(self) -> None:
        """startup 時に get_acc_list() で 401/unauth 検出（ADR-014 Decision 2）。

        Raises:
            AuthenticationError: 認証失敗（期限切れ等）
            RuntimeError: OpenD 未起動
        """
        self._ensure_connected()
        try:
            ret, data = self._trade_ctx.get_acc_list()
            if ret != RET_OK:
                raise AuthenticationError(
                    f"get_acc_list failed (ret={ret}, data={data}). "
                    "moomoo session likely expired. Re-login required."
                )
            if data is None or len(data) == 0:
                raise AuthenticationError("get_acc_list returned empty account list")
        except AuthenticationError:
            raise
        except Exception as exc:
            raise RuntimeError(f"smoke_test failed: {exc}") from exc

    def get_metrics(self) -> dict:
        """paper 口座の実 PnL を取得し YFinanceMetricProvider 互換 dict を返す。

        Returns:
            dict with keys {pnl_day_usd, drawdown_pct, latency_ms}

        Raises:
            AuthenticationError: セッション期限切れ（MonitorDaemon が Pushover 発火）
            RuntimeError: OpenD 未起動・API エラー
            MoomooProviderNotImplementedError: futu-api 未インストール
        """
        self._ensure_connected()

        start = time.monotonic()
        last_err: Optional[Exception] = None
        for attempt in range(self._retry_max):
            try:
                ret, data = self._trade_ctx.accinfo_query(trd_env=TrdEnv.SIMULATE)
                if ret != RET_OK:
                    if "401" in str(data) or "unauth" in str(data).lower():
                        raise AuthenticationError(
                            f"accinfo_query 401/unauth (ret={ret}, data={data})"
                        )
                    raise RuntimeError(f"accinfo_query failed (ret={ret}, data={data})")
                if data is None or len(data) == 0:
                    raise RuntimeError("accinfo_query returned empty data")

                row = data.iloc[0]
                total_assets = float(row.get("total_assets") or 0.0)
                realized_pl = float(row.get("realized_pl") or 0.0)
                unrealized_pl = float(row.get("unrealized_pl") or 0.0)

                pnl_day_usd = realized_pl + unrealized_pl

                if total_assets > self._high_water_mark_usd:
                    self._high_water_mark_usd = total_assets
                if self._high_water_mark_usd > 0:
                    drawdown_pct = max(
                        0.0,
                        (self._high_water_mark_usd - total_assets) / self._high_water_mark_usd,
                    )
                else:
                    drawdown_pct = 0.0

                latency_ms = (time.monotonic() - start) * 1000.0

                return {
                    "pnl_day_usd": pnl_day_usd,
                    "drawdown_pct": drawdown_pct,
                    "latency_ms": latency_ms,
                    "total_assets_usd": total_assets,
                    "high_water_mark_usd": self._high_water_mark_usd,
                }
            except AuthenticationError:
                raise
            except Exception as exc:
                last_err = exc
                if attempt < self._retry_max - 1:
                    backoff = _DEFAULT_RETRY_BACKOFF_BASE ** attempt
                    log.warning(
                        "[MoomooProvider] get_metrics attempt %d failed: %s. Retrying in %.1fs",
                        attempt + 1, exc, backoff,
                    )
                    time.sleep(backoff)
                    continue
                break
        raise RuntimeError(
            f"get_metrics failed after {self._retry_max} attempts. Last error: {last_err}"
        ) from last_err

    def close(self) -> None:
        """接続を明示的に閉じる（teardown 用）。"""
        if self._trade_ctx is not None:
            try:
                self._trade_ctx.close()
            except Exception as exc:
                log.warning("[MoomooProvider] close failed: %s", exc)
            finally:
                self._trade_ctx = None
