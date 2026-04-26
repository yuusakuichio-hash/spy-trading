"""VIXスパイク30%検知 + IC売り優先バイアス テスト (施策8)

検証対象:
  - save_vix_daily_close: 日次終値ログ追記・スパイクフラグ計算
  - _get_prev_vix_daily_close: 前日終値取得
  - is_vix_spike_30_day: スパイク翌日判定（各種境界条件）
  - record_vix_spike_ic_trade: 実績別集計への記録
  - premarket_assessment: vix_spike_ic_bias キーが返却される
  - IronCondorSellEngine.premarket_check: _vix_spike_30 フラグ設定
  - IronCondorSellEngine.execute_entry: サイズ縮小 (0.5x)
  - VIX >= 40 時の disable ロジック
"""
import os
import sys
import json
import datetime
import tempfile
import types
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# futu を mock して ImportError を回避
futu_mock = types.ModuleType("futu")
futu_mock.TrdSide = types.SimpleNamespace(BUY="BUY", SELL="SELL")
futu_mock.OrderType = types.SimpleNamespace(MARKET="MARKET", LIMIT="LIMIT")
futu_mock.TrdMarket = types.SimpleNamespace(US="US")
futu_mock.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
futu_mock.RET_OK = 0
futu_mock.RET_ERROR = -1
futu_mock.SecurityFirm = types.SimpleNamespace(FUTUINC="FUTUINC")
futu_mock.SubType = types.SimpleNamespace(TICKER="TICKER")
futu_mock.TimeInForce = types.SimpleNamespace(DAY="DAY")
futu_mock.ModifyOrderOp = types.SimpleNamespace(CANCEL="CANCEL")
futu_mock.StockQuoteHandlerBase = object
futu_mock.OpenQuoteContext = object
futu_mock.OpenSecTradeContext = object
sys.modules.setdefault("futu", futu_mock)

_TRADING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TRADING_DIR)

import spy_bot as bot_mod
from spy_bot import (
    save_vix_daily_close,
    _get_prev_vix_daily_close,
    is_vix_spike_30_day,
    record_vix_spike_ic_trade,
    VIX_DAILY_SPIKE_30_PCT,
    VIX_SPIKE_IC_SIZE_FACTOR,
    VIX_SPIKE_IC_DISABLE_ABOVE,
    IronCondorSellEngine,
)


# ── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_log_with_spike(tmpdir: Path, vix_close: float, spike_30: bool, days_ago: int = 0) -> Path:
    """テスト用 vix_daily_log.jsonl を作成する。日付は ET タイムゾーンで計算。"""
    import pytz
    ET = pytz.timezone("America/New_York")
    log_file = tmpdir / "vix_daily_log.jsonl"
    target_date = (datetime.datetime.now(ET) - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")
    record = {
        "date":      target_date,
        "vix_close": vix_close,
        "delta_pct": 0.35 if spike_30 else 0.05,
        "spike_30":  spike_30,
    }
    log_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return log_file


class MockMkt:
    def __init__(self, vix=25.0):
        self.underlying_code = "US.SPY"
        self._vix = vix

    def get_vix(self):
        return self._vix

    def get_vix_history(self, days=60):
        return [self._vix + (i % 5 - 2) for i in range(days)]

    def get_spy_current(self):
        return 560.0

    def get_option_chain_with_greeks(self, *a, **k):
        return None

    def find_by_delta(self, *a, **k):
        return None

    def find_by_strike(self, *a, **k):
        return None

    def get_symbol_atr(self, *a, **k):
        return 5.0

    def calc_vrp(self, *a, **k):
        return 3.0

    def get_vix9d_vvix(self):
        return None, None

    def get_global_risk_data(self):
        return {}

    def get_put_call_ratio(self):
        return None

    def get_skew_index(self):
        return None

    def get_news_sentiment(self, *a, **k):
        return {}

    def get_spy_daily_closes(self, *a, **k):
        return []

    def get_spy_snapshot(self):
        return None


# ── テスト群 ─────────────────────────────────────────────────────────────────

class TestSaveVixDailyClose:
    """save_vix_daily_close の単体テスト"""

    def test_appends_record_no_prev(self, tmp_path):
        """前日ログなし → spike_30=False で追記される"""
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", tmp_path / "vix_daily_log.jsonl"):
            save_vix_daily_close(20.0)
            records = [json.loads(l) for l in (tmp_path / "vix_daily_log.jsonl").read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["vix_close"] == 20.0
        assert records[0]["spike_30"] is False
        assert records[0]["delta_pct"] is None

    def test_spike_30_flag_set_when_above_30pct(self, tmp_path):
        """前日20→当日27（+35%）→ spike_30=True"""
        log_file = tmp_path / "vix_daily_log.jsonl"
        # 前日レコードを先に書く
        prev = {"date": "2026-01-01", "vix_close": 20.0, "delta_pct": None, "spike_30": False}
        log_file.write_text(json.dumps(prev) + "\n", encoding="utf-8")

        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            save_vix_daily_close(27.0)
            records = [json.loads(l) for l in log_file.read_text().splitlines()]

        assert len(records) == 2
        latest = records[-1]
        assert latest["spike_30"] is True
        assert latest["delta_pct"] == pytest.approx(0.35, abs=0.01)

    def test_spike_30_flag_not_set_when_below_30pct(self, tmp_path):
        """前日20→当日24（+20%）→ spike_30=False"""
        log_file = tmp_path / "vix_daily_log.jsonl"
        prev = {"date": "2026-01-01", "vix_close": 20.0, "delta_pct": None, "spike_30": False}
        log_file.write_text(json.dumps(prev) + "\n", encoding="utf-8")

        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            save_vix_daily_close(24.0)
            records = [json.loads(l) for l in log_file.read_text().splitlines()]

        assert records[-1]["spike_30"] is False

    def test_exact_30pct_boundary_triggers_spike(self, tmp_path):
        """前日20→当日26（+30.0%）→ spike_30=True（境界値: >= 30%）"""
        log_file = tmp_path / "vix_daily_log.jsonl"
        prev = {"date": "2026-01-01", "vix_close": 20.0, "delta_pct": None, "spike_30": False}
        log_file.write_text(json.dumps(prev) + "\n", encoding="utf-8")

        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            save_vix_daily_close(26.0)  # exactly +30%
            records = [json.loads(l) for l in log_file.read_text().splitlines()]

        assert records[-1]["spike_30"] is True


class TestGetPrevVixDailyClose:
    """_get_prev_vix_daily_close の単体テスト"""

    def test_returns_none_when_file_missing(self, tmp_path):
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", tmp_path / "missing.jsonl"):
            assert _get_prev_vix_daily_close() is None

    def test_returns_last_vix_close(self, tmp_path):
        log_file = tmp_path / "vix_daily_log.jsonl"
        lines = [
            json.dumps({"date": "2026-01-01", "vix_close": 18.5, "delta_pct": None, "spike_30": False}),
            json.dumps({"date": "2026-01-02", "vix_close": 22.3, "delta_pct": 0.20, "spike_30": False}),
        ]
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            result = _get_prev_vix_daily_close()
        assert result == pytest.approx(22.3)


class TestIsVixSpike30Day:
    """is_vix_spike_30_day の境界条件テスト"""

    def test_returns_false_when_file_missing(self, tmp_path):
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", tmp_path / "missing.jsonl"):
            assert is_vix_spike_30_day() is False

    def test_returns_true_when_spike_flag_set_today(self, tmp_path):
        log_file = _make_log_with_spike(tmp_path, vix_close=25.0, spike_30=True, days_ago=0)
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            assert is_vix_spike_30_day() is True

    def test_returns_false_when_spike_not_set(self, tmp_path):
        log_file = _make_log_with_spike(tmp_path, vix_close=25.0, spike_30=False, days_ago=0)
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            assert is_vix_spike_30_day() is False

    def test_returns_false_when_record_too_old(self, tmp_path):
        """5日以上前のレコード → stale として False（固定日付で ET タイムゾーン依存を排除）"""
        log_file = tmp_path / "vix_daily_log.jsonl"
        # 固定で2000-01-01（明らかに古い日付）
        old_record = {
            "date": "2000-01-01",
            "vix_close": 25.0,
            "delta_pct": 0.35,
            "spike_30": True,
        }
        log_file.write_text(json.dumps(old_record) + "\n", encoding="utf-8")
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            assert is_vix_spike_30_day() is False

    def test_accepts_record_from_today(self, tmp_path):
        """当日レコード（spike_30=True）→ 有効"""
        import pytz
        ET = pytz.timezone("America/New_York")
        today_et = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        log_file = tmp_path / "vix_daily_log.jsonl"
        record = {"date": today_et, "vix_close": 25.0, "delta_pct": 0.35, "spike_30": True}
        log_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            assert is_vix_spike_30_day() is True

    def test_returns_false_when_vix_above_disable_threshold(self, tmp_path):
        """VIX終値 >= 40 → IC disable（高VIXは逆リスク）"""
        log_file = _make_log_with_spike(tmp_path, vix_close=VIX_SPIKE_IC_DISABLE_ABOVE, spike_30=True, days_ago=0)
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            assert is_vix_spike_30_day() is False

    def test_returns_false_when_vix_well_above_disable_threshold(self, tmp_path):
        """VIX終値=55（明らかに危険域）→ False"""
        log_file = _make_log_with_spike(tmp_path, vix_close=55.0, spike_30=True, days_ago=0)
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            assert is_vix_spike_30_day() is False

    def test_returns_true_when_vix_just_below_disable_threshold(self, tmp_path):
        """VIX終値=39.9（閾値未満）→ spike=True なら True"""
        log_file = _make_log_with_spike(tmp_path, vix_close=39.9, spike_30=True, days_ago=0)
        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file):
            assert is_vix_spike_30_day() is True


class TestRecordVixSpikeIcTrade:
    """record_vix_spike_ic_trade の単体テスト"""

    def test_appends_to_jsonl(self, tmp_path):
        trade_file = tmp_path / "vix_spike_trades.jsonl"
        with patch.object(bot_mod, "VIX_SPIKE_TRADES_FILE", trade_file):
            record_vix_spike_ic_trade({
                "date": "2026-04-20", "symbol": "US.SPY",
                "event": "entry", "pnl_usd": None,
                "vix": 25.0, "delta_pct": 0.35, "signal_id": "test-001",
            })
        records = [json.loads(l) for l in trade_file.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["tactic"] == "ic_sell_vix_spike"
        assert records[0]["signal_id"] == "test-001"

    def test_multiple_trades_appended(self, tmp_path):
        trade_file = tmp_path / "vix_spike_trades.jsonl"
        with patch.object(bot_mod, "VIX_SPIKE_TRADES_FILE", trade_file):
            for i in range(3):
                record_vix_spike_ic_trade({"event": "entry", "signal_id": f"sig-{i}"})
        records = [json.loads(l) for l in trade_file.read_text().splitlines()]
        assert len(records) == 3


class TestPremarketAssessmentSpikeFlag:
    """premarket_assessment が vix_spike_ic_bias キーを返すことを確認"""

    def _run_assessment(self, vix=22.0, spike_30=False, tmp_path=None):
        mkt = MockMkt(vix=vix)
        log_file = tmp_path / "vix_daily_log.jsonl" if tmp_path else None

        patches = []
        if log_file is not None:
            if spike_30:
                _make_log_with_spike(tmp_path, vix_close=vix * 0.75, spike_30=True, days_ago=0)
            p = patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file)
            patches.append(p)

        # Disable components that would need real data
        with patch.object(bot_mod, "ENABLE_VRP_CHECK", False), \
             patch.object(bot_mod, "ENABLE_KEY_LEVELS", False), \
             patch.object(bot_mod, "ECON_CALENDAR_FILE", Path("/dev/null")), \
             patch.object(bot_mod, "is_vix_spike_30_day", return_value=spike_30):
            result = bot_mod.premarket_assessment(mkt, vix)

        return result

    def test_spike_bias_false_by_default(self, tmp_path):
        result = self._run_assessment(spike_30=False)
        assert "vix_spike_ic_bias" in result
        assert result["vix_spike_ic_bias"] is False

    def test_spike_bias_true_when_spike_detected(self, tmp_path):
        result = self._run_assessment(spike_30=True)
        assert result["vix_spike_ic_bias"] is True


class TestIronCondorPremarketCheckSpike:
    """IronCondorSellEngine.premarket_check でスパイクフラグが設定されることを確認"""

    def _make_engine(self, vix=25.0):
        mkt = MockMkt(vix=vix)
        engine = IronCondorSellEngine(mkt=mkt, eng=None, paper=True, dry_test=False)
        engine.today_vix = vix
        return engine

    def test_spike_flag_set_on_premarket_check(self, tmp_path):
        engine = self._make_engine(vix=25.0)
        log_file = _make_log_with_spike(tmp_path, vix_close=19.0, spike_30=True, days_ago=0)

        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file), \
             patch.object(bot_mod, "ENABLE_IC_SELL", True), \
             patch.object(bot_mod, "_ic_sell_check_consecutive_losses", return_value=False), \
             patch.object(bot_mod, "_PORTFOLIO_RISK_AVAILABLE", False):
            result = engine.premarket_check()

        assert engine._vix_spike_30 is True
        assert result is True

    def test_spike_flag_false_when_no_spike(self, tmp_path):
        engine = self._make_engine(vix=25.0)
        log_file = _make_log_with_spike(tmp_path, vix_close=19.0, spike_30=False, days_ago=0)

        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file), \
             patch.object(bot_mod, "ENABLE_IC_SELL", True), \
             patch.object(bot_mod, "_ic_sell_check_consecutive_losses", return_value=False), \
             patch.object(bot_mod, "_PORTFOLIO_RISK_AVAILABLE", False):
            result = engine.premarket_check()

        assert engine._vix_spike_30 is False

    def test_vix_above_40_disables_ic_on_premarket(self, tmp_path):
        """VIX >= 40 → IC_SELL_VIX_MAX チェックでスキップ"""
        engine = self._make_engine(vix=41.0)
        log_file = _make_log_with_spike(tmp_path, vix_close=30.0, spike_30=True, days_ago=0)

        with patch.object(bot_mod, "VIX_DAILY_LOG_FILE", log_file), \
             patch.object(bot_mod, "ENABLE_IC_SELL", True), \
             patch.object(bot_mod, "_ic_sell_check_consecutive_losses", return_value=False), \
             patch.object(bot_mod, "_PORTFOLIO_RISK_AVAILABLE", False):
            result = engine.premarket_check()

        assert result is False


class TestIronCondorExecuteEntrySpikeSize:
    """スパイク翌日のサイズ縮小（0.5x）を確認"""

    def test_qty_halved_when_spike_30(self):
        """_vix_spike_30=True 時、qty は通常の max(1, int(qty * 0.5)) になる"""
        mkt = MockMkt(vix=25.0)
        engine = IronCondorSellEngine(mkt=mkt, eng=None, paper=True, dry_test=True)
        engine._vix_spike_30 = True
        engine.today_vix = 25.0

        with patch.object(bot_mod, "_is_past_entry_cutoff", return_value=False), \
             patch.object(bot_mod, "_sym_is_allowed", return_value=True), \
             patch.object(bot_mod, "pushover", return_value=None), \
             patch.object(bot_mod, "_ic_sell_append_pnl", return_value=None), \
             patch.object(bot_mod, "record_vix_spike_ic_trade", return_value=None), \
             patch.object(engine, "_get_ivr_percentile", return_value=50.0), \
             patch.object(engine, "_calc_qty", return_value=6) as mock_calc_qty:
            pos = engine.execute_entry()

        # qty = max(1, int(6 * 0.5)) = 3
        assert pos is not None
        assert pos.qty == 3

    def test_qty_normal_when_no_spike(self):
        """_vix_spike_30=False 時、qty は縮小されない"""
        mkt = MockMkt(vix=25.0)
        engine = IronCondorSellEngine(mkt=mkt, eng=None, paper=True, dry_test=True)
        engine._vix_spike_30 = False
        engine.today_vix = 25.0

        with patch.object(bot_mod, "_is_past_entry_cutoff", return_value=False), \
             patch.object(bot_mod, "_sym_is_allowed", return_value=True), \
             patch.object(bot_mod, "pushover", return_value=None), \
             patch.object(bot_mod, "_ic_sell_append_pnl", return_value=None), \
             patch.object(bot_mod, "record_vix_spike_ic_trade", return_value=None), \
             patch.object(engine, "_get_ivr_percentile", return_value=50.0), \
             patch.object(engine, "_calc_qty", return_value=6):
            pos = engine.execute_entry()

        assert pos is not None
        assert pos.qty == 6  # 縮小なし

    def test_qty_minimum_1_even_with_spike(self):
        """スパイク縮小後のqty最小値は1"""
        mkt = MockMkt(vix=25.0)
        engine = IronCondorSellEngine(mkt=mkt, eng=None, paper=True, dry_test=True)
        engine._vix_spike_30 = True
        engine.today_vix = 25.0

        with patch.object(bot_mod, "_is_past_entry_cutoff", return_value=False), \
             patch.object(bot_mod, "_sym_is_allowed", return_value=True), \
             patch.object(bot_mod, "pushover", return_value=None), \
             patch.object(bot_mod, "_ic_sell_append_pnl", return_value=None), \
             patch.object(bot_mod, "record_vix_spike_ic_trade", return_value=None), \
             patch.object(engine, "_get_ivr_percentile", return_value=50.0), \
             patch.object(engine, "_calc_qty", return_value=1):
            pos = engine.execute_entry()

        assert pos is not None
        assert pos.qty >= 1


class TestConstantValues:
    """定数値のサニティチェック"""

    def test_spike_threshold_is_30pct(self):
        assert VIX_DAILY_SPIKE_30_PCT == 0.30

    def test_size_factor_is_half(self):
        assert VIX_SPIKE_IC_SIZE_FACTOR == 0.50

    def test_disable_above_is_40(self):
        assert VIX_SPIKE_IC_DISABLE_ABOVE == 40.0
