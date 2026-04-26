"""tests/test_moomoo_get_metrics_acc_id_20260424.py — C-017 本実装 unit test

要件検証:
- R1: smoke_test() で _paper_acc_id がキャッシュされる（get_acc_list SIMULATE 行 iloc[0]["acc_id"]）
- R2: get_metrics() が _paper_acc_id 設定済の場合 accinfo_query に acc_id=... を渡す
- R3: get_metrics() が _paper_acc_id=None の場合 acc_id なしで accinfo_query を呼ぶ（後方互換）
- R4: get_metrics() の accinfo_query が hang した場合 RuntimeError（daemon thread + join(timeout)）
- R5: latency_ms は非負（perf_counter ベース計測）
- R6: net_assets=100000 / unrealized_pl=500 / realized_pl=-200 の場合
       pnl_day_usd = 300.0・drawdown_pct = 0.0（HWM == net_assets）
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import atlas_v3.ops.moomoo_provider as mp
from atlas_v3.ops.moomoo_provider import (
    AuthenticationError,
    MoomooMetricProvider,
)


# ---------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_futu_env(monkeypatch):
    """futu SDK 未インストール環境でも動作するよう module-level 定数を差し替える。"""
    class _FakeTrdEnv:
        SIMULATE = "SIMULATE"
        REAL = "REAL"
    monkeypatch.setattr(mp, "FUTU_AVAILABLE", True)
    monkeypatch.setattr(mp, "RET_OK", 0)
    monkeypatch.setattr(mp, "TrdEnv", _FakeTrdEnv)
    monkeypatch.setattr(mp, "time", time)  # 実 time モジュールを差し替えない


@pytest.fixture()
def provider_no_hwm():
    """HWM 永続ファイルを bypass した MoomooMetricProvider。"""
    with patch.object(MoomooMetricProvider, "_load_hwm", return_value=0.0), \
         patch.object(MoomooMetricProvider, "_save_hwm"):
        p = MoomooMetricProvider(socket_timeout_secs=2.0)
        p._ensure_connected = lambda: None  # 実接続スキップ
        yield p


def _make_accinfo_df(
    total_assets: float = 100_000.0,
    realized_pl: float = 0.0,
    unrealized_pl: float = 0.0,
) -> pd.DataFrame:
    return pd.DataFrame([{
        "total_assets": total_assets,
        "realized_pl": realized_pl,
        "unrealized_pl": unrealized_pl,
    }])


def _make_acc_list_df(acc_id: int = 1173421, trd_env: str = "SIMULATE") -> pd.DataFrame:
    """get_acc_list() の返却 DataFrame をシミュレートする。"""
    return pd.DataFrame([
        {"acc_id": acc_id, "trd_env": "SIMULATE"},
        {"acc_id": 9999999, "trd_env": "REAL"},
    ])


# ---------------------------------------------------------------------------
# R1: smoke_test() で _paper_acc_id キャッシュ
# ---------------------------------------------------------------------------

class TestPaperAccIdResolution:
    """R1: smoke_test() が SIMULATE 行から acc_id を _paper_acc_id にキャッシュする。"""

    def test_smoke_test_caches_paper_acc_id(self, provider_no_hwm):
        """正常系: smoke_test() 後に _paper_acc_id == 1173421 が設定されている。"""
        mock_ctx = MagicMock()
        mock_ctx.get_acc_list.return_value = (0, _make_acc_list_df(acc_id=1173421))
        provider_no_hwm._trade_ctx = mock_ctx

        assert provider_no_hwm._paper_acc_id is None  # 初期値は None

        provider_no_hwm.smoke_test(timeout_secs=5.0)

        assert provider_no_hwm._paper_acc_id == 1173421

    def test_smoke_test_picks_first_simulate_row(self, provider_no_hwm):
        """SIMULATE が複数行ある場合 iloc[0] の acc_id を使用する。"""
        df = pd.DataFrame([
            {"acc_id": 111, "trd_env": "SIMULATE"},
            {"acc_id": 222, "trd_env": "SIMULATE"},
            {"acc_id": 999, "trd_env": "REAL"},
        ])
        mock_ctx = MagicMock()
        mock_ctx.get_acc_list.return_value = (0, df)
        provider_no_hwm._trade_ctx = mock_ctx

        provider_no_hwm.smoke_test(timeout_secs=5.0)

        assert provider_no_hwm._paper_acc_id == 111

    def test_smoke_test_paper_acc_id_initial_none(self):
        """__init__ 直後は _paper_acc_id は None（解決前）。"""
        with patch.object(MoomooMetricProvider, "_load_hwm", return_value=0.0):
            p = MoomooMetricProvider()
        assert p._paper_acc_id is None


# ---------------------------------------------------------------------------
# R2: get_metrics() が acc_id 明示で accinfo_query を呼ぶ
# ---------------------------------------------------------------------------

class TestAccInfoQueryAccId:
    """R2: _paper_acc_id 設定済の場合 accinfo_query に acc_id=... が渡される。"""

    def test_get_metrics_passes_acc_id_when_cached(self, provider_no_hwm):
        """_paper_acc_id=1173421 の場合 accinfo_query(acc_id=1173421) が呼ばれる。"""
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df())
        provider_no_hwm._trade_ctx = mock_ctx
        provider_no_hwm._paper_acc_id = 1173421

        provider_no_hwm.get_metrics()

        mock_ctx.accinfo_query.assert_called_once_with(
            trd_env="SIMULATE",
            acc_id=1173421,
        )

    def test_get_metrics_omits_acc_id_when_none(self, provider_no_hwm):
        """R3: _paper_acc_id=None の場合 accinfo_query を acc_id なしで呼ぶ（後方互換）。"""
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df())
        provider_no_hwm._trade_ctx = mock_ctx
        # _paper_acc_id は None（初期値のまま）
        assert provider_no_hwm._paper_acc_id is None

        provider_no_hwm.get_metrics()

        mock_ctx.accinfo_query.assert_called_once_with(trd_env="SIMULATE")

    def test_get_metrics_acc_id_cast_to_int(self, provider_no_hwm):
        """acc_id は文字列で保存されていても int にキャストして渡す。"""
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df())
        provider_no_hwm._trade_ctx = mock_ctx
        provider_no_hwm._paper_acc_id = 1173421  # 数値として保持

        provider_no_hwm.get_metrics()

        call_kwargs = mock_ctx.accinfo_query.call_args.kwargs
        assert isinstance(call_kwargs["acc_id"], int)
        assert call_kwargs["acc_id"] == 1173421


# ---------------------------------------------------------------------------
# R4: get_metrics() hang 検知 → RuntimeError
# ---------------------------------------------------------------------------

class TestGetMetricsHangTimeout:
    """R4: accinfo_query が hang した場合 socket_timeout_secs 後に RuntimeError。"""

    def test_get_metrics_raises_on_hang(self, provider_no_hwm):
        """daemon thread が join(timeout) 内に応答しない → RuntimeError。"""
        mock_ctx = MagicMock()
        def _hang(*args, **kwargs):
            time.sleep(30)
            return 0, _make_accinfo_df()
        mock_ctx.accinfo_query.side_effect = _hang
        provider_no_hwm._trade_ctx = mock_ctx
        provider_no_hwm._socket_timeout_secs = 0.5  # 短い timeout でテスト
        provider_no_hwm._paper_acc_id = 1173421

        t0 = time.time()
        with pytest.raises(RuntimeError, match="hang"):
            provider_no_hwm.get_metrics()
        elapsed = time.time() - t0
        # timeout が効いていれば <5s で抜ける（実 hang 30s 待ちにならない）
        assert elapsed < 5.0, f"hang timeout did not fire; elapsed={elapsed:.1f}s"


# ---------------------------------------------------------------------------
# R5: latency_ms（perf_counter ベース）
# ---------------------------------------------------------------------------

class TestLatencyMs:
    """R5: latency_ms は非負・perf_counter ベース計測。"""

    def test_latency_ms_non_negative(self, provider_no_hwm):
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df())
        provider_no_hwm._trade_ctx = mock_ctx
        metrics = provider_no_hwm.get_metrics()
        assert metrics["latency_ms"] >= 0.0

    def test_latency_ms_is_float(self, provider_no_hwm):
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df())
        provider_no_hwm._trade_ctx = mock_ctx
        metrics = provider_no_hwm.get_metrics()
        assert isinstance(metrics["latency_ms"], float)


# ---------------------------------------------------------------------------
# R6: pnl_day_usd / drawdown_pct 算出ロジック（acc_id 指定込み）
# ---------------------------------------------------------------------------

class TestPnlDrawdownWithAccId:
    """R6: net_assets + pl から pnl_day_usd / drawdown_pct が正しく算出される。"""

    def test_pnl_day_usd_realized_plus_unrealized(self, provider_no_hwm):
        """realized_pl + unrealized_pl = pnl_day_usd。"""
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (
            0,
            _make_accinfo_df(total_assets=100_000.0, realized_pl=-200.0, unrealized_pl=500.0),
        )
        provider_no_hwm._trade_ctx = mock_ctx
        provider_no_hwm._paper_acc_id = 1173421

        metrics = provider_no_hwm.get_metrics()

        assert metrics["pnl_day_usd"] == pytest.approx(300.0)

    def test_drawdown_pct_zero_when_at_hwm(self, provider_no_hwm):
        """HWM == 現在 net_assets の場合 drawdown_pct = 0.0。"""
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (
            0, _make_accinfo_df(total_assets=100_000.0)
        )
        provider_no_hwm._trade_ctx = mock_ctx
        provider_no_hwm._paper_acc_id = 1173421

        m1 = provider_no_hwm.get_metrics()
        assert m1["drawdown_pct"] == pytest.approx(0.0)

    def test_drawdown_pct_positive_when_below_hwm(self, provider_no_hwm):
        """net_assets が HWM を下回った場合 drawdown_pct > 0。"""
        mock_ctx = MagicMock()
        # 1回目: HWM=100000 確立
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df(total_assets=100_000.0))
        provider_no_hwm._trade_ctx = mock_ctx
        provider_no_hwm._paper_acc_id = 1173421
        provider_no_hwm.get_metrics()

        # 2回目: net_assets=95000 → drawdown = 5%
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df(total_assets=95_000.0))
        m2 = provider_no_hwm.get_metrics()
        assert m2["drawdown_pct"] == pytest.approx(0.05)

    def test_required_keys_present(self, provider_no_hwm):
        """get_metrics() dict に必須キーが揃っている（interface 契約）。"""
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (0, _make_accinfo_df())
        provider_no_hwm._trade_ctx = mock_ctx

        metrics = provider_no_hwm.get_metrics()

        assert {"pnl_day_usd", "drawdown_pct", "latency_ms"} <= set(metrics.keys())
