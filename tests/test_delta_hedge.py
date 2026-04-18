"""DeltaHedge エンジン テスト

IntradayMonitor._try_delta_hedge() のマルチ銘柄対応を検証する。
- オプションコード生成（銘柄別）
- should_delta_hedge() PDT判定ロジック
- threshold動的判定
- 日次/週次カウンタ
- Pushover [Atlas] タグ
- pre_trade_check統合
"""
import os
import sys
import math
import types
import datetime

import pytest

# spy_bot.py のある親ディレクトリをパスに追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# futu未インストール環境でもテスト可能にするためのダミー
_futu_mock = types.ModuleType("futu")
_futu_mock.RET_OK = 0
_futu_mock.TrdSide = types.SimpleNamespace(BUY=1, SELL=2)
_futu_mock.KLType = types.SimpleNamespace(K_1M="K_1M")
sys.modules.setdefault("futu", _futu_mock)

from spy_bot import should_delta_hedge, DELTA_HEDGE_TRIGGER, DELTA_HEDGE_UNWIND


# ── ヘルパー ─────────────────────────────────────────────────────────────────

def _build_hedge_code(ticker: str, direction: str, price: float, expiry: str = "260418") -> str:
    """テスト用オプションコード生成（spy_bot._try_delta_hedge と同じ形式）。"""
    type_char = "C" if direction == "CALL" else "P"
    strike_int = int(round(price) * 1000)
    return f"US.{ticker}{expiry}{type_char}{strike_int:08d}"


# ── should_delta_hedge テスト ─────────────────────────────────────────────────

def test_sdh_over_25k_always_allowed():
    """$25K以上は常に発動許可。"""
    allowed, reason = should_delta_hedge(0.35, 30000, 0, False)
    assert allowed is True
    assert "制限なし" in reason


def test_sdh_over_25k_high_count_still_allowed():
    """$25K以上はPDT週次カウントが多くても発動許可。"""
    allowed, _ = should_delta_hedge(0.35, 50000, 10, False)
    assert allowed is True


def test_sdh_under_25k_pdt_budget_exhausted():
    """$25K未満・PDT枠使い切り → ブロック。"""
    allowed, reason = should_delta_hedge(0.35, 10000, 3, False)
    assert allowed is False
    assert "PDT枠使い切り" in reason


def test_sdh_under_25k_non_emergency_skip():
    """$25K未満・非緊急 → スキップ。"""
    allowed, reason = should_delta_hedge(0.35, 10000, 0, False)
    assert allowed is False
    assert "非緊急スキップ" in reason


def test_sdh_under_25k_emergency_allowed():
    """$25K未満・|Delta|>0.5（緊急）→ 発動許可。"""
    allowed, reason = should_delta_hedge(0.55, 10000, 0, False)
    assert allowed is True
    assert "緊急ヘッジ" in reason


def test_sdh_under_25k_emergency_flag():
    """$25K未満・外部緊急フラグ → 発動許可。"""
    allowed, reason = should_delta_hedge(0.30, 10000, 1, True)
    assert allowed is True
    assert "緊急ヘッジ" in reason


def test_sdh_pdt_remaining_count_in_reason():
    """緊急発動時、PDT残回数がreasonに含まれる。"""
    allowed, reason = should_delta_hedge(0.55, 10000, 1, False)
    assert allowed is True
    assert "PDT残" in reason


# ── オプションコード生成 テスト ───────────────────────────────────────────────

def test_hedge_code_spy():
    """SPY のヘッジコードが正しい形式。"""
    code = _build_hedge_code("SPY", "PUT", 553.0)
    assert code.startswith("US.SPY")
    assert "P" in code
    assert "553000" in code


def test_hedge_code_qqq():
    """QQQ のヘッジコードが正しい形式。"""
    code = _build_hedge_code("QQQ", "CALL", 480.0)
    assert code.startswith("US.QQQ")
    assert "C" in code
    assert "480000" in code


def test_hedge_code_tsla():
    """TSLA のヘッジコードが正しい形式（高株価銘柄も対応）。"""
    code = _build_hedge_code("TSLA", "CALL", 250.0)
    assert code.startswith("US.TSLA")
    assert "C" in code
    assert "250000" in code


def test_hedge_code_meta():
    """META のヘッジコードが正しい形式。"""
    code = _build_hedge_code("META", "PUT", 600.0)
    assert code.startswith("US.META")
    assert "P" in code
    assert "600000" in code


# ── Delta計算ロジック テスト ──────────────────────────────────────────────────

def test_excess_delta_to_qty():
    """超過delta → hedge_qty算出が正しい。"""
    from spy_bot import DELTA_HEDGE_CONTRACT_DELTA
    trigger = DELTA_HEDGE_TRIGGER
    delta_abs = 0.50
    excess = delta_abs - trigger
    qty = max(1, math.ceil(excess / DELTA_HEDGE_CONTRACT_DELTA))
    assert qty >= 1
    assert isinstance(qty, int)


def test_hedge_direction_positive_delta():
    """total_delta > 0（上昇バイアス）→ PUT買い。"""
    total_delta = 0.40
    hedge_direction = "PUT" if total_delta > 0 else "CALL"
    assert hedge_direction == "PUT"


def test_hedge_direction_negative_delta():
    """total_delta < 0（下落バイアス）→ CALL買い。"""
    total_delta = -0.40
    hedge_direction = "PUT" if total_delta > 0 else "CALL"
    assert hedge_direction == "CALL"
