"""tests/test_moomoo_smoke_timeout_20260424.py — S-5 regression

2026-04-24 事故:
    moomoo OpenD が応答しない状態で MoomooMetricProvider.smoke_test() が永久 hang。
    caller の startup flow 全体が blocking（ADR-014 Decision 2 の fail-closed 設計が
    無効化）。

修正:
    smoke_test() に ThreadPoolExecutor + Future.result(timeout) で wall-clock cap。
    timeout 時は AuthenticationError raise（既存 catch 流に乗る）。

テスト:
    T-1: 正常応答時は timeout しない・既存動作維持
    T-2: get_acc_list() が hang した場合 AuthenticationError raise
    T-3: timeout 値を明示指定できる
    T-4: SIMULATE 行 0 件なら AuthenticationError (Paper 口座未設定検知)
"""
from __future__ import annotations

import time

import pandas as pd
import pytest

from atlas_v3.ops import moomoo_provider as mp


class _FakeRetOk:
    """RET_OK 定数 (== 0) の stand-in for test."""
    value = 0


def _make_df(rows):
    """test helper: get_acc_list 戻り値と同じ形の DataFrame を作る。"""
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["acc_id", "trd_env"])


class _FakeTradeCtxOK:
    """正常応答する get_acc_list の fake。"""
    def get_acc_list(self):
        return 0, _make_df([
            {"acc_id": "sim1", "trd_env": "SIMULATE"},
            {"acc_id": "real1", "trd_env": "REAL"},
        ])


class _FakeTradeCtxHang:
    """get_acc_list が hang する fake（timeout trigger 用）。"""
    def get_acc_list(self):
        time.sleep(30)  # test の timeout より長く
        return 0, _make_df([])


class _FakeTradeCtxNoSim:
    """SIMULATE 行がない fake（Paper 口座未設定シナリオ）。"""
    def get_acc_list(self):
        return 0, _make_df([
            {"acc_id": "real1", "trd_env": "REAL"},
        ])


@pytest.fixture(autouse=True)
def _patch_trdenv(monkeypatch):
    """TrdEnv.SIMULATE を文字列 'SIMULATE' に差し替える（futu SDK 未インストール test 環境用）。"""
    class _TE:
        SIMULATE = "SIMULATE"
        REAL = "REAL"
    monkeypatch.setattr(mp, "TrdEnv", _TE)
    monkeypatch.setattr(mp, "RET_OK", 0)
    monkeypatch.setattr(mp, "FUTU_AVAILABLE", True)


def _make_provider_with_ctx(ctx):
    """MoomooMetricProvider を ctx 注入で作る（_ensure_connected を skip）。"""
    p = mp.MoomooMetricProvider()
    p._trade_ctx = ctx
    # _ensure_connected を no-op に置換
    p._ensure_connected = lambda: None
    return p


def test_smoke_test_ok_when_normal_response():
    """T-1: 正常応答時は timeout に引っかからず完走する。"""
    provider = _make_provider_with_ctx(_FakeTradeCtxOK())
    provider.smoke_test(timeout_secs=5.0)  # no exception


def test_smoke_test_raises_on_hang():
    """T-2: get_acc_list が hang したら AuthenticationError で抜ける（caller 保護）。"""
    provider = _make_provider_with_ctx(_FakeTradeCtxHang())
    t0 = time.time()
    with pytest.raises(mp.AuthenticationError, match="timeout"):
        provider.smoke_test(timeout_secs=1.0)
    elapsed = time.time() - t0
    # timeout が効いていれば概ね 1 秒前後で抜ける（API 実 hang 30s 待ちにならない）
    assert elapsed < 5.0, f"timeout did not fire promptly; elapsed={elapsed:.1f}s"


def test_smoke_test_timeout_configurable():
    """T-3: timeout 値を caller から指定できる（default 15s も含め kwarg 経由）。"""
    provider = _make_provider_with_ctx(_FakeTradeCtxHang())
    t0 = time.time()
    with pytest.raises(mp.AuthenticationError):
        provider.smoke_test(timeout_secs=0.5)
    elapsed = time.time() - t0
    assert elapsed < 3.0, f"0.5s timeout did not fire; elapsed={elapsed:.1f}s"


def test_smoke_test_raises_when_no_simulate_account():
    """T-4: SIMULATE 行 0 件なら AuthenticationError (Paper 口座未設定検知)。"""
    provider = _make_provider_with_ctx(_FakeTradeCtxNoSim())
    with pytest.raises(mp.AuthenticationError, match="SIMULATE|Paper"):
        provider.smoke_test(timeout_secs=5.0)
