"""atlas_v3/ops/moomoo_provider.py — moomoo OpenD 経由の実 Bot PnL MetricProvider（スケルトン）

**Sprint 2 C-017 先行スケルトン（2026-04-24 時点）**
実装は Sprint 2 Day 2 で（ゆうさくさん判断 3 件確定後）。
本ファイルは interface 定義 + NotImplementedError stub のみ。

設計方針（Strategist a7bfe851 事前調査推奨案 A）:
- local OpenD プロセス (127.0.0.1:11111) に futu-api SDK で接続
- `accinfo_query(trd_env=TrdEnv.SIMULATE)` で paper 口座の実 PnL 取得
- spy_bot.py:4420-4514 の TradeEngine 参照パターン流用（**ただし触らない**・読取のみ）
- YFinanceMetricProvider と同一 interface: `get_metrics() -> dict`
- 代理 PnL (yfinance) → 実 PnL (moomoo) への移行

Fail-closed 規律:
- OpenD 未起動時は RuntimeError raise（zero-fallback 禁止）
- 401/unauth は startup smoke test で検出
- socket timeout=5s 明示 + retry 3 回 exponential backoff

ゆうさくさん確認 3 件（memory/project_moomoo_opend_research_20260424.md 参照）:
1. OpenD 常駐場所（Mac mini or VPS）
2. Paper 口座セッション有効期限切れ時の再ログイン手順
3. Sprint 2 スコープ（read-only metrics のみ or 発注含む）

依存: futu-api >= 10.2.6218 (pip install futu-api)
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# OpenD 接続パラメータ（既存 spy_bot.py パターン踏襲）
_DEFAULT_OPEND_HOST = "127.0.0.1"
_DEFAULT_OPEND_PORT = 11111
_DEFAULT_SOCKET_TIMEOUT_SECS = 5.0
_DEFAULT_RETRY_MAX = 3


class MoomooProviderNotImplementedError(NotImplementedError):
    """Sprint 2 Day 2 実装待ちを明示する例外。"""

    def __init__(self, message: str = "") -> None:
        super().__init__(
            "MoomooMetricProvider は Sprint 2 C-017 で実装予定（現在スケルトン）。"
            "詳細: data/specs/builder_prompt_template_sprint2_20260424.md / "
            "memory/project_moomoo_opend_research_20260424.md。"
            + (f" 追加: {message}" if message else "")
        )


class MoomooMetricProvider:
    """moomoo OpenD 経由で Paper Bot の実 PnL を取得する MetricProvider（スケルトン）。

    interface: YFinanceMetricProvider と同一
        get_metrics() -> dict with keys {pnl_day_usd, drawdown_pct, latency_ms}

    Sprint 2 Day 2 実装予定項目:
    1. __init__: OpenD 接続パラメータ受取・futu SDK import guard
    2. _connect: OpenSecTradeContext 作成 + unlock_trade
    3. _fetch_account_info: accinfo_query(TrdEnv.SIMULATE) 呼出
    4. _compute_pnl_metrics: total_assets / realized_pl / unrealized_pl から metric 算出
    5. get_metrics: 上記を統合・fail-closed で RuntimeError raise
    6. _smoke_test: startup 時に get_acc_list() で 401/unauth 検出

    現時点では NotImplementedError を raise する stub のみ。
    """

    def __init__(
        self,
        *,
        opend_host: str = _DEFAULT_OPEND_HOST,
        opend_port: int = _DEFAULT_OPEND_PORT,
        socket_timeout_secs: float = _DEFAULT_SOCKET_TIMEOUT_SECS,
        retry_max: int = _DEFAULT_RETRY_MAX,
        trade_password: Optional[str] = None,
    ) -> None:
        """スケルトン: パラメータだけ受け取って保持。

        Sprint 2 Day 2 で以下を実装:
        - futu SDK import と FUTU_AVAILABLE flag 相当
        - OpenSecTradeContext 初期化（遅延・lazy connect）
        - trade_password の unlock_trade 用保持
        """
        self._opend_host = opend_host
        self._opend_port = opend_port
        self._socket_timeout_secs = socket_timeout_secs
        self._retry_max = retry_max
        self._trade_password = trade_password
        log.info(
            "[MoomooProvider] SKELETON initialized (host=%s port=%d). "
            "Real implementation is pending Sprint 2 C-017.",
            opend_host,
            opend_port,
        )

    def get_metrics(self) -> dict:
        """Sprint 2 Day 2 実装予定。

        Returns:
            dict with keys {pnl_day_usd, drawdown_pct, latency_ms}

        Raises:
            MoomooProviderNotImplementedError: Sprint 2 実装待ち（常時 raise）
        """
        raise MoomooProviderNotImplementedError("get_metrics")

    def smoke_test(self) -> None:
        """Sprint 2 Day 2 実装予定。OpenD 起動 + 認証を startup 時に確認。

        Raises:
            MoomooProviderNotImplementedError: Sprint 2 実装待ち（常時 raise）
        """
        raise MoomooProviderNotImplementedError("smoke_test")
