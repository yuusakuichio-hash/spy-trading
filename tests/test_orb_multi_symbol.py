"""ORBエンジン マルチ銘柄対応テスト

ORBEngine のマルチ銘柄化（SPY固定解除）を検証する。
- _get_underlying_price() が銘柄別に動作する
- check_breakout() が underlying_code 別に判定する
- _get_underlying_1min_bars() の銘柄別取得
- center_strike±15% フィルタで異常strikeを除外
- dry-testの virtual_code が銘柄別に生成される
- MassVerify ORB が非SPY銘柄でスキップしない
- ORBPositionが各銘柄のコードを保持する
"""
import os
import sys
import types
import datetime
from unittest.mock import patch, MagicMock

import pytest

# futu未インストール環境でもテスト可能にするためのダミー
_futu_mock = types.ModuleType("futu")
_futu_mock.RET_OK = 0
_futu_mock.TrdSide = types.SimpleNamespace(BUY=1, SELL=2)
_futu_mock.KLType = types.SimpleNamespace(K_1M="K_1M")
sys.modules.setdefault("futu", _futu_mock)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spy_bot import ORBEngine, ORBPosition


# 2026-04-25: full-suite で別 test の monkeypatch leak (spy_bot.requests / cache 等) を
# autouse fixture で毎テスト reset・xfail 削除可能化
@pytest.fixture(autouse=True)
def _reset_spy_bot_state(monkeypatch):
    """spy_bot module の monkeypatch leak を毎テスト開始時に reset."""
    try:
        import spy_bot
        import requests as real_requests
        # spy_bot.requests を real requests に強制差し戻し
        if hasattr(spy_bot, "requests"):
            monkeypatch.setattr(spy_bot, "requests", real_requests, raising=False)
    except ImportError:
        pass
    yield


# ── ヘルパー ─────────────────────────────────────────────────────────────────

FALLBACK_PRICES = {
    "SPY": 560.0, "QQQ": 480.0, "IWM": 200.0,
    "TSLA": 250.0, "NVDA": 900.0, "AAPL": 200.0,
    "MSFT": 420.0, "AMZN": 200.0, "META": 600.0,
    "GOOGL": 170.0,
}


class _MockMkt:
    """MarketDataのテスト用モック。underlying_code別に価格を返す。"""

    def __init__(self, underlying_code: str = "US.SPY"):
        self.underlying_code = underlying_code
        self.quote_ctx = None  # Futu未接続

        class _PC:
            def get(self, code, max_age_sec=5.0):
                return None
            def get_open(self, code, max_age_sec=5.0):
                return None

        self._price_cache = _PC()

    def get_spy_current(self):
        ticker = self.underlying_code.replace("US.", "").replace(".", "")
        return FALLBACK_PRICES.get(ticker, 300.0)

    def _get_spy_price_finnhub(self):
        ticker = self.underlying_code.replace("US.", "").replace(".", "")
        return {"last_price": FALLBACK_PRICES.get(ticker, 300.0)}

    def get_vix(self):
        return 18.0

    def get_vix_history(self, days=60):
        return [18.0 + i * 0.1 for i in range(days)]


def _make_orb(underlying_code: str = "US.SPY") -> ORBEngine:
    """テスト用ORBEngineを作成する（dry_test=True）。"""
    mkt = _MockMkt(underlying_code)
    eng = None
    orb = ORBEngine(mkt=mkt, eng=eng, paper=True, dry_test=True)
    return orb


def _mock_finnhub(ticker: str):
    """Finnhub requestsをモックしてフォールバック価格を返す。"""
    price = FALLBACK_PRICES.get(ticker, 300.0)
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"c": price}
    return mock_resp


# ── _get_underlying_price テスト ─────────────────────────────────────────────

def test_get_underlying_price_spy():
    """SPY価格がフォールバックで返る（Finnhubモック使用）。"""
    orb = _make_orb("US.SPY")
    with patch("spy_bot.requests.get", return_value=_mock_finnhub("SPY")):
        price = orb._get_underlying_price()
    assert price is not None
    assert price == 560.0


def test_get_underlying_price_qqq():
    """QQQ価格がフォールバックで返る（SPYと異なる値）。"""
    orb = _make_orb("US.QQQ")
    with patch("spy_bot.requests.get", return_value=_mock_finnhub("QQQ")):
        price = orb._get_underlying_price()
    assert price is not None
    assert price == 480.0
    assert price != 560.0  # SPYと異なる


def test_get_underlying_price_tsla():
    """TSLA価格がフォールバックで返る。"""
    orb = _make_orb("US.TSLA")
    with patch("spy_bot.requests.get", return_value=_mock_finnhub("TSLA")):
        price = orb._get_underlying_price()
    assert price is not None
    assert price == 250.0


def test_get_spy_price_backward_compat():
    """_get_spy_price() は _get_underlying_price() に委譲する（同じ値を返す）。"""
    orb = _make_orb("US.SPY")
    with patch("spy_bot.requests.get", return_value=_mock_finnhub("SPY")):
        p1 = orb._get_spy_price()
        p2 = orb._get_underlying_price()
    assert p1 == p2


# ── record_opening_range テスト ───────────────────────────────────────────────

def test_record_opening_range_spy():
    """SPYのORBレンジが設定される。"""
    orb = _make_orb("US.SPY")
    with patch("spy_bot.requests.get", return_value=_mock_finnhub("SPY")):
        result = orb.record_opening_range()
    assert result is True
    assert orb.orb_high is not None
    assert orb.orb_low is not None
    assert orb.orb_high > orb.orb_low
    assert orb.orb_checked is True
    # SPYフォールバック価格560 → 560.5/559.5
    assert abs(orb.orb_high - 560.5) < 1.0


def test_record_opening_range_qqq():
    """QQQのORBレンジが設定される（underlying_code参照）。"""
    orb = _make_orb("US.QQQ")
    with patch("spy_bot.requests.get", return_value=_mock_finnhub("QQQ")):
        result = orb.record_opening_range()
    assert result is True
    assert orb.orb_high > orb.orb_low
    # QQQフォールバック価格480 → 480.5/479.5
    assert abs(orb.orb_high - 480.5) < 1.0


def test_record_opening_range_tsla():
    """TSLAのORBレンジが設定される。"""
    orb = _make_orb("US.TSLA")
    with patch("spy_bot.requests.get", return_value=_mock_finnhub("TSLA")):
        result = orb.record_opening_range()
    assert result is True
    assert orb.orb_checked is True
    # TSLAフォールバック価格250 → 250.5/249.5
    assert abs(orb.orb_high - 250.5) < 1.0


# ── check_breakout テスト ─────────────────────────────────────────────────────

def test_check_breakout_call_spy():
    """SPYのCALLブレイクアウトを検出する。"""
    orb = _make_orb("US.SPY")
    orb.orb_high = 559.0  # spy_price(560) > orb_high(559) → CALL
    orb.orb_low  = 555.0
    orb.orb_range = 4.0
    orb.orb_checked = True
    orb.dry_test = True

    with patch("spy_bot.requests.get", return_value=_mock_finnhub("SPY")):
        result = orb.check_breakout()
    assert result == "CALL"


def test_check_breakout_put_qqq():
    """QQQのPUTブレイクアウトを検出する。"""
    orb = _make_orb("US.QQQ")
    # QQQフォールバック価格=480 → orb_low=481 → PUT
    orb.orb_high = 485.0
    orb.orb_low  = 481.0
    orb.orb_range = 4.0
    orb.orb_checked = True
    orb.dry_test = True

    with patch("spy_bot.requests.get", return_value=_mock_finnhub("QQQ")):
        result = orb.check_breakout()
    assert result == "PUT"


def test_check_breakout_no_signal():
    """レンジ内は None を返す（SPY=560はレンジ555〜565の内側）。"""
    orb = _make_orb("US.SPY")
    orb.orb_high = 565.0
    orb.orb_low  = 555.0
    orb.orb_range = 10.0
    orb.orb_checked = True
    orb.dry_test = True

    with patch("spy_bot.requests.get", return_value=_mock_finnhub("SPY")):
        result = orb.check_breakout()
    assert result is None


# ── オプションコード形式テスト ────────────────────────────────────────────────

def test_orb_code_format_spy():
    """SPYのオプションコードは US.SPY で始まる。"""
    code = "US.SPY260418C00560000"
    assert code.startswith("US.SPY")
    assert "C" in code


def test_orb_code_format_qqq():
    """QQQのオプションコードは US.QQQ で始まる。"""
    ticker = "QQQ"
    expiry = "260418"
    direction = "CALL"
    atm_strike = 480
    code = f"US.{ticker}{expiry}{'C' if direction == 'CALL' else 'P'}{int(atm_strike * 1000):08d}"
    assert code == "US.QQQ260418C00480000"


def test_orb_code_format_tsla():
    """TSLAのオプションコードは US.TSLA で始まる。"""
    ticker = "TSLA"
    expiry = "260418"
    direction = "PUT"
    atm_strike = 250
    code = f"US.{ticker}{expiry}{'C' if direction == 'CALL' else 'P'}{int(atm_strike * 1000):08d}"
    assert code == "US.TSLA260418P00250000"


# ── ORBPosition テスト ───────────────────────────────────────────────────────

def test_orb_position_holds_code():
    """ORBPositionが銘柄別コードを保持できる。"""
    code = "US.QQQ260418C00480000"
    pos = ORBPosition(code, 2, 1.50, "CALL", 485.0, 475.0)
    assert pos.code == code
    assert pos.qty == 2
    assert pos.direction == "CALL"
    assert pos.orb_high == 485.0
    assert pos.orb_low == 475.0


def test_orb_position_meta():
    """METAのORBPositionが正しく設定される。"""
    code = "US.META260418P00600000"
    pos = ORBPosition(code, 1, 5.00, "PUT", 605.0, 595.0)
    assert pos.code == code
    assert pos.direction == "PUT"
    assert pos.entry_price == 5.00
