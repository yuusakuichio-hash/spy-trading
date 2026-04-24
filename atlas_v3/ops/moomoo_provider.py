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

# S-4 fix (Redteam r8): rate limit 対策
# moomoo OpenD (futu-api) は公称 30req/30s 程度。check_interval=15s に対して retry 3 件 burst
# で 15s 内に 4 call 発射（初回 + retry 3）→ 他 bot と共存で超過リスク。
# _MIN_REQUEST_INTERVAL_SECS で最低間隔を保証（rate limit 擬似ガード）
_MIN_REQUEST_INTERVAL_SECS = 1.0

# B-1 fix (Redteam r8): RET_OK 固定値前提の runtime assert
# futu-api 公式 doc では RET_OK = 0 だが、将来 SDK 変更で別値になる可能性。
# import 成功時は SDK 値を使用・fallback 0 使用時は startup で warning ログ

# S-2 fix (Redteam r8): 401/unauth 判定の多言語対応
# 中国語エラー: "未授权" "权限不足" "会话已过期"
# 英語エラー: "401" "unauth" "unauthorized" "session expired"
# 日本語エラー: "認証" "権限なし" "セッション期限"
_AUTH_ERROR_PATTERNS = [
    "401",
    "unauth",
    "unauthorized",
    "session expired",
    "未授权",
    "权限不足",
    "会话已过期",
    "認証",
    "権限",
    "セッション期限",
]

# S-3 fix: high_water_mark の session 永続化パス
from pathlib import Path as _Path
_HWM_STATE_FILE = _Path("/Users/yuusakuichio/trading/data/state_v3/moomoo_hwm.json")


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
        # S-3 fix: high_water_mark を session 跨ぎで永続化（再起動での drawdown 隠蔽防止）
        self._high_water_mark_usd: float = self._load_hwm()
        # S-4 fix: rate limit 対策・最後の request 時刻
        self._last_request_ts: float = 0.0
        # C-017: smoke_test で SIMULATE 行から解決する Paper 口座 acc_id（初期 None）
        # get_metrics() は設定済なら accinfo_query(acc_id=...) で明示指定する
        self._paper_acc_id: Optional[int] = None
        # B-1 fix: RET_OK 値の runtime 確認（SDK 未 import 時は 0 fallback なので warning）
        if not FUTU_AVAILABLE:
            log.warning(
                "[MoomooProvider] futu SDK unavailable. RET_OK fallback=0. "
                "Sprint 3+ で futu 公式 doc から RET_OK 値確認・固定値 assert 追加予定。"
            )

    def _load_hwm(self) -> float:
        """S-3 fix: 永続化された high_water_mark を読込（起動時）。"""
        try:
            if _HWM_STATE_FILE.exists():
                import json
                data = json.loads(_HWM_STATE_FILE.read_text(encoding="utf-8"))
                hwm = float(data.get("high_water_mark_usd", 0.0))
                log.info("[MoomooProvider] Loaded persisted high_water_mark: $%.2f", hwm)
                return hwm
        except Exception as exc:
            log.warning("[MoomooProvider] Failed to load hwm state: %s. Starting from 0.", exc)
        return 0.0

    def _save_hwm(self) -> None:
        """S-3 fix: high_water_mark を永続化（drawdown 隠蔽防止）。"""
        try:
            import json
            _HWM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HWM_STATE_FILE.write_text(
                json.dumps({"high_water_mark_usd": self._high_water_mark_usd}),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("[MoomooProvider] Failed to save hwm state: %s", exc)

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

    def smoke_test(self, timeout_secs: float = 15.0) -> None:
        """startup 時に get_acc_list() で 401/unauth 検出（ADR-014 Decision 2）。
        S-2 fix: 多言語 auth error パターンで中国語/日本語 moomoo 応答も検知。
        S-5 fix 2026-04-24: OpenD 応答 hang で caller が永久に blocking しないよう
        ThreadPoolExecutor + Future.result(timeout) で wall-clock cap を強制。

        Args:
            timeout_secs: get_acc_list() 単体の wall-clock timeout（default 15s）

        Raises:
            AuthenticationError: 認証失敗・Paper 口座未設定・timeout hang 検知時
            RuntimeError: OpenD 未起動 / 予期せぬ例外
        """
        self._ensure_connected()
        try:
            # S-5 fix: hang 検知のため daemon thread で API 呼出・main thread は timeout 管理。
            # ThreadPoolExecutor の with 文は hang 時に context exit が worker join を待つため
            # 使わず、daemon=True + join(timeout) で wall-clock cap を強制する。
            import threading as _th
            _result: dict = {}
            def _runner():
                try:
                    _result["value"] = self._trade_ctx.get_acc_list()
                except BaseException as _e:  # noqa: BLE001 — propagate any
                    _result["error"] = _e
            _t = _th.Thread(target=_runner, daemon=True, name="moomoo_smoke_worker")
            _t.start()
            _t.join(timeout=timeout_secs)
            if _t.is_alive():
                raise AuthenticationError(
                    f"smoke_test timeout after {timeout_secs}s. "
                    "OpenD unreachable or Paper 口座 login 未了の可能性。"
                )
            if "error" in _result:
                raise RuntimeError(f"get_acc_list raised in worker: {_result['error']}")
            ret, data = _result["value"]
            # S-3 fix 2026-04-24: spy_bot.py:4461-4470 と同じパターン。
            # get_acc_list() は全環境の DataFrame を返すので trd_env 列で SIMULATE 行を抽出。
            if ret != RET_OK:
                data_lower = str(data).lower()
                is_auth = any(pat.lower() in data_lower for pat in _AUTH_ERROR_PATTERNS)
                if is_auth:
                    raise AuthenticationError(
                        f"get_acc_list auth error (ret={ret}, data={data}). "
                        "moomoo session likely expired. Re-login required."
                    )
                raise AuthenticationError(
                    f"get_acc_list failed (ret={ret}, data={data}). "
                    "moomoo session likely expired. Re-login required."
                )
            if data is None or (hasattr(data, "empty") and data.empty) or (not hasattr(data, "empty") and len(data) == 0):
                raise AuthenticationError("get_acc_list returned empty account list")
            # SIMULATE (Paper 口座) が 1 件以上あることを確認（Paper monitor の前提）
            sim_rows = data[data["trd_env"] == TrdEnv.SIMULATE] if hasattr(data, "__getitem__") else data
            if hasattr(sim_rows, "empty") and sim_rows.empty:
                raise AuthenticationError(
                    "get_acc_list returned no SIMULATE (Paper) account. "
                    "moomoo app の『デモ取引』を有効化してください。"
                )
            # C-017: SIMULATE 行の先頭 acc_id をキャッシュ（get_metrics で accinfo_query 明示指定用）
            try:
                self._paper_acc_id = int(sim_rows.iloc[0]["acc_id"])
                log.info("[MoomooProvider] Paper acc_id resolved: %d", self._paper_acc_id)
            except (IndexError, KeyError, ValueError, TypeError) as _acc_err:
                log.warning("[MoomooProvider] acc_id resolution failed: %s. Falling back to trd_env-only query.", _acc_err)
                self._paper_acc_id = None
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

        # S-4 fix: rate limit 擬似ガード（直前 request から _MIN_REQUEST_INTERVAL_SECS 経過を保証）
        now = time.monotonic()
        since_last = now - self._last_request_ts
        if since_last < _MIN_REQUEST_INTERVAL_SECS:
            wait = _MIN_REQUEST_INTERVAL_SECS - since_last
            log.debug("[MoomooProvider] rate limit guard: waiting %.2fs", wait)
            time.sleep(wait)

        start = time.monotonic()
        last_err: Optional[Exception] = None
        for attempt in range(self._retry_max):
            self._last_request_ts = time.monotonic()
            try:
                # C-017: Paper 口座 acc_id キャッシュ済なら accinfo_query に明示指定
                # (smoke_test 未実行環境での後方互換のため None 時は省略)
                _accinfo_kwargs: dict = {"trd_env": TrdEnv.SIMULATE}
                if self._paper_acc_id is not None:
                    _accinfo_kwargs["acc_id"] = int(self._paper_acc_id)

                # R4: hang 検知 — daemon thread で呼出・join(timeout) で wall-clock cap
                # (smoke_test と同パターン・OpenD 応答 hang で monitor thread が永久 block しない)
                import threading as _th
                _call_result: dict = {}
                def _call_runner():
                    try:
                        _call_result["value"] = self._trade_ctx.accinfo_query(**_accinfo_kwargs)
                    except BaseException as _e:  # noqa: BLE001 — propagate any
                        _call_result["error"] = _e
                _ct = _th.Thread(target=_call_runner, daemon=True, name="moomoo_accinfo_worker")
                _ct.start()
                _ct.join(timeout=self._socket_timeout_secs)
                if _ct.is_alive():
                    raise RuntimeError(
                        f"accinfo_query hang detected (timeout={self._socket_timeout_secs}s). "
                        "OpenD unreachable or session blocked."
                    )
                if "error" in _call_result:
                    raise _call_result["error"] if isinstance(_call_result["error"], Exception) else RuntimeError(f"accinfo_query raised: {_call_result['error']}")
                ret, data = _call_result["value"]

                if ret != RET_OK:
                    # S-2 fix: 多言語 auth error パターン検知
                    data_lower = str(data).lower()
                    if any(pat.lower() in data_lower for pat in _AUTH_ERROR_PATTERNS):
                        raise AuthenticationError(
                            f"accinfo_query auth error (ret={ret}, data={data})"
                        )
                    raise RuntimeError(f"accinfo_query failed (ret={ret}, data={data})")
                if data is None or len(data) == 0:
                    raise RuntimeError("accinfo_query returned empty data")

                row = data.iloc[0]
                # S-6 fix: pd.isna() で NaN 明示検出（row.get() or 0.0 は NaN を回避しない）
                import pandas as pd
                def _safe_float(key: str) -> float:
                    v = row.get(key)
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return 0.0
                    try:
                        f = float(v)
                        return 0.0 if pd.isna(f) else f
                    except (TypeError, ValueError):
                        return 0.0
                total_assets = _safe_float("total_assets")
                realized_pl = _safe_float("realized_pl")
                unrealized_pl = _safe_float("unrealized_pl")

                pnl_day_usd = realized_pl + unrealized_pl

                if total_assets > self._high_water_mark_usd:
                    self._high_water_mark_usd = total_assets
                    # S-3 fix: HWM 更新時に即永続化（drawdown 隠蔽防止）
                    self._save_hwm()
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
