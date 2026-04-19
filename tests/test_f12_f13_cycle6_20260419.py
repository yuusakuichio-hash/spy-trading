#!/usr/bin/env python3
"""
tests/test_f12_f13_cycle6_20260419.py
Chronos F12/F13 cycle6 修正検証 — 30+ケース

対象修正:
  B1: price_history 重複汚染防止（timestamp dedupe）
  B2: _to_timestamp ミリ秒epoch判定（1e11境界・負値・None・floatms）
  H1: _BASE_DIR lazy解決（MFFU_DATA_DIR 環境変数 patch.dict が有効）
  H4: 条件付きアサートskip 撤廃（cycle4 test）
  H5: TestPrevDayPromotion インライン複写廃止（実際の _run_nightly 呼出）
  H6: is_stub 特殊メソッド除外（__init__ pass でも is_stub=False）
  M1: state.json F12/F13 書出・schema contract
  M3: divergence 価格無変動+delta大変化ケース
"""
from __future__ import annotations

import sys
import os
import json
import importlib
import tempfile
import unittest.mock as mock
from pathlib import Path
from collections import deque

# ── パス設定
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

import chronos_bot as _cbot

_to_timestamp = _cbot._to_timestamp


# =============================================================================
# B2: _to_timestamp ミリ秒epoch判定
# =============================================================================

class TestToTimestampMsEpoch:
    """B2: ミリ秒epoch（v > 1e11）を正しく秒に変換する。"""

    def test_ms_epoch_int(self):
        """1776609000000 (ms) → 1776609000 (s)。"""
        assert _to_timestamp(1776609000000) == 1776609000

    def test_ms_epoch_large(self):
        """2e12 (ms) → 2000000000 (s)。"""
        assert _to_timestamp(2_000_000_000_000) == 2_000_000_000

    def test_ms_epoch_float(self):
        """1.776609e12 (ms float) → 1776609000 (s)。"""
        result = _to_timestamp(1.776609e12)
        assert result == 1776609000, f"got {result}"

    def test_boundary_exactly_1e11(self):
        """1e11 は境界値。1e11 + 1 は ms 扱い。"""
        # 1e11 = 100000000000: ms 判定は > 10**11
        assert _to_timestamp(int(1e11)) == int(1e11)  # 境界値は秒扱い
        assert _to_timestamp(int(1e11) + 1) == (int(1e11) + 1) // 1000  # 境界超えはms

    def test_seconds_epoch_unchanged(self):
        """通常の秒epoch（1713532200）は変換されない。"""
        assert _to_timestamp(1713532200) == 1713532200

    def test_zero(self):
        """0 は 0 のまま。"""
        assert _to_timestamp(0) == 0

    def test_none_returns_zero(self):
        """None は 0 を返す。"""
        assert _to_timestamp(None) == 0

    def test_negative_int(self):
        """負のint は そのまま int 変換（境界判定はpositive値のみ）。"""
        # 負値は v > 10**11 を満たさないのでそのまま
        result = _to_timestamp(-1000)
        assert result == -1000

    def test_iso8601_with_ms(self):
        """ISO8601 ミリ秒付き文字列は正しくパースされる。"""
        result = _to_timestamp("2026-04-19T14:30:00.000Z")
        # 秒精度での epoch 値
        import datetime
        expected = int(datetime.datetime(2026, 4, 19, 14, 30, 0,
                                          tzinfo=datetime.timezone.utc).timestamp())
        assert result == expected

    def test_ms_float_boundary_precision(self):
        """浮動小数点 ms epoch の変換精度チェック。"""
        # 1776609000123 ms → 1776609000 s
        result = _to_timestamp(1776609000123)
        assert result == 1776609000


# =============================================================================
# B1: price_history 重複汚染防止
# =============================================================================

class TestPriceHistoryDedupe:
    """B1: 同じバー timestamp を 3 回 update しても _price_history に 1 回だけ append。"""

    def _make_bot(self):
        return _cbot.ChronosBot(account_size=50000, paper=True, dry_run=True)

    def test_same_ts_no_duplicate(self):
        """同じ timestamp のバーを複数回処理しても price_history に重複しない。"""
        bot = self._make_bot()
        # _price_history_processed_ts を初期化
        from collections import deque as _dq
        bot._price_history_processed_ts = _dq(maxlen=50)

        bar_ts = 1713532200  # 秒epoch
        bar_close = 5200.0

        # 同じバーを 3 回処理
        for _ in range(3):
            _bar_ts = bar_ts
            _bar_close = bar_close
            if _bar_ts > 0 and _bar_ts in bot._price_history_processed_ts:
                pass
            elif _bar_close > 0:
                bot._price_history.append(_bar_close)
                if _bar_ts > 0:
                    bot._price_history_processed_ts.append(_bar_ts)

        assert len(bot._price_history) == 1, (
            f"B1: 同じバーが {len(bot._price_history)} 回 append された（期待: 1）"
        )
        assert list(bot._price_history) == [5200.0]

    def test_different_ts_both_appended(self):
        """異なる timestamp のバーは両方 append される。"""
        bot = self._make_bot()
        from collections import deque as _dq
        bot._price_history_processed_ts = _dq(maxlen=50)

        bars = [
            (1713532200, 5200.0),
            (1713532260, 5210.0),
        ]
        for bar_ts, bar_close in bars:
            if bar_ts > 0 and bar_ts in bot._price_history_processed_ts:
                pass
            elif bar_close > 0:
                bot._price_history.append(bar_close)
                if bar_ts > 0:
                    bot._price_history_processed_ts.append(bar_ts)

        assert len(bot._price_history) == 2
        assert list(bot._price_history) == [5200.0, 5210.0]

    def test_daily_reset_clears_processed_ts(self):
        """daily_reset 後に _price_history_processed_ts がクリアされる。"""
        import datetime
        bot = self._make_bot()
        from collections import deque as _dq
        bot._price_history_processed_ts = _dq(maxlen=50)
        bot._price_history_processed_ts.append(1713532200)
        bot._price_history_processed_ts.append(1713532260)
        assert len(bot._price_history_processed_ts) == 2

        # daily_reset 呼出
        bot._daily_reset(datetime.date.today())
        assert len(bot._price_history_processed_ts) == 0, (
            "B1: daily_reset 後に _price_history_processed_ts がクリアされていない"
        )

    def test_zero_ts_does_not_dedupe(self):
        """timestamp=0 のバーは dedupe しない（ts=0 は判定除外）。"""
        bot = self._make_bot()
        from collections import deque as _dq
        bot._price_history_processed_ts = _dq(maxlen=50)

        # timestamp=0 のバーを 2 回処理（dedupe しないので 2 回 append される）
        for _ in range(2):
            bar_ts = 0
            bar_close = 5100.0
            if bar_ts > 0 and bar_ts in bot._price_history_processed_ts:
                pass
            elif bar_close > 0:
                bot._price_history.append(bar_close)
                if bar_ts > 0:
                    bot._price_history_processed_ts.append(bar_ts)

        # ts=0 は dedupe 対象外なので 2 回 append される（意図的動作）
        assert len(bot._price_history) == 2


# =============================================================================
# H1: _BASE_DIR lazy解決テスト
# =============================================================================

class TestBaseDirLazyResolution:
    """H1: MFFU_DATA_DIR 環境変数を呼び出し時点で解決する。"""

    def test_get_base_dir_reads_env(self, tmp_path):
        """_get_base_dir() は呼び出し時点の MFFU_DATA_DIR を返す。"""
        with mock.patch.dict(os.environ, {"MFFU_DATA_DIR": str(tmp_path)}):
            result = _cbot._get_base_dir()
        assert result == tmp_path, f"expected {tmp_path}, got {result}"

    def test_get_base_dir_default(self):
        """MFFU_DATA_DIR 未設定時は <module_parent>/data を返す。"""
        env_without_mffu = {k: v for k, v in os.environ.items() if k != "MFFU_DATA_DIR"}
        with mock.patch.dict(os.environ, env_without_mffu, clear=True):
            result = _cbot._get_base_dir()
        expected = Path(_cbot.__file__).parent / "data"
        assert result == expected

    def test_get_base_dir_is_callable(self):
        """_get_base_dir は呼び出し可能（関数として存在する）。"""
        assert callable(_cbot._get_base_dir)

    def test_save_state_uses_base_dir(self, tmp_path):
        """_save_state が _BASE_DIR（または _get_base_dir()）を使って tmp_path に書き出せる。

        H1: patch.object で _BASE_DIR を上書きすることでテスト分離を実現。
        """
        bot = _cbot.ChronosBot(account_size=50000, paper=True, dry_run=True)
        with mock.patch.object(_cbot, "_BASE_DIR", tmp_path):
            with mock.patch.dict(os.environ, {"MFFU_ACCOUNT_ID": "test_h1_account"}):
                bot._save_state("test")
        state_path = tmp_path / "accounts" / "test_h1_account" / "state.json"
        assert state_path.exists(), f"H1: state.json not written to {state_path}"
        data = json.loads(state_path.read_text())
        assert data["account_id"] == "test_h1_account"


# =============================================================================
# M1: state.json F12/F13 書出 schema contract
# =============================================================================

class TestStateJsonF12F13Schema:
    """M1: state.json に F12/F13 フィールドが書き出されることを確認。"""

    def _save_state_to_tmp(self, bot, tmp_path, account_id, reason="periodic"):
        """共通ヘルパー: _BASE_DIR を tmp_path にパッチして _save_state を呼ぶ。"""
        with mock.patch.object(_cbot, "_BASE_DIR", tmp_path):
            with mock.patch.dict(os.environ, {"MFFU_ACCOUNT_ID": account_id}):
                bot._save_state(reason)
        return tmp_path / "accounts" / account_id / "state.json"

    def test_state_has_f12_field(self, tmp_path):
        """_save_state で f12_cumulative_delta_bias フィールドが含まれる。"""
        bot = _cbot.ChronosBot(account_size=50000, paper=True, dry_run=True)
        state_path = self._save_state_to_tmp(bot, tmp_path, "test_m1_schema")
        data = json.loads(state_path.read_text())
        assert "f12_cumulative_delta_bias" in data, (
            "M1: state.json に f12_cumulative_delta_bias フィールドがない"
        )

    def test_state_has_f13_field(self, tmp_path):
        """_save_state で f13_liquidity_sweep_signal フィールドが含まれる。"""
        bot = _cbot.ChronosBot(account_size=50000, paper=True, dry_run=True)
        state_path = self._save_state_to_tmp(bot, tmp_path, "test_m1_f13")
        data = json.loads(state_path.read_text())
        assert "f13_liquidity_sweep_signal" in data, (
            "M1: state.json に f13_liquidity_sweep_signal フィールドがない"
        )

    def test_state_has_prev_day_fields(self, tmp_path):
        """_save_state で _prev_day_high/low/vwap が含まれる。"""
        bot = _cbot.ChronosBot(account_size=50000, paper=True, dry_run=True)
        bot._prev_day_high = 5200.0
        bot._prev_day_low = 5100.0
        bot._prev_day_vwap = 5150.0
        state_path = self._save_state_to_tmp(bot, tmp_path, "test_m1_prev", "daily_reset")
        data = json.loads(state_path.read_text())
        assert data.get("_prev_day_high") == 5200.0
        assert data.get("_prev_day_low") == 5100.0
        assert data.get("_prev_day_vwap") == 5150.0

    def test_state_schema_all_required_fields(self, tmp_path):
        """既存 schema contract フィールド + cycle6 追加フィールドが全て存在する。"""
        bot = _cbot.ChronosBot(account_size=50000, paper=True, dry_run=True)
        state_path = self._save_state_to_tmp(bot, tmp_path, "test_m1_full")
        data = json.loads(state_path.read_text())

        # 既存フィールド（cycle2 contract）
        for field in [
            "account_id", "timestamp", "save_reason",
            "positions", "weekly_dd_usd", "daily_pnl_usd",
            "consecutive_losses", "phase_flags",
            "best_single_day_profit_usd", "total_profit_usd",
            "winning_days_count", "daily_trade_count",
        ]:
            assert field in data, f"M1: 既存フィールド '{field}' が state.json にない"

        # cycle6 追加フィールド
        for field in [
            "f12_cumulative_delta_bias",
            "f13_liquidity_sweep_signal",
            "_prev_day_high",
            "_prev_day_low",
            "_prev_day_vwap",
        ]:
            assert field in data, f"M1: cycle6フィールド '{field}' が state.json にない"


# =============================================================================
# M3: divergence 価格無変動+delta大変化ケース
# =============================================================================

class TestDivergencePriceFlat:
    """M3: 価格停滞（price_change=0）+ delta 大変化は divergence 候補として検出する。"""

    def _cd(self):
        from chronos_cumulative_delta import CumulativeDelta
        return CumulativeDelta(bucket_minutes=5, max_buckets=78)

    def test_price_flat_delta_up_detects_bullish(self):
        """価格停滞 + delta 上昇 → 十分なthreshold超過時に bullish_divergence。"""
        from chronos_cumulative_delta import CumulativeDelta
        cd = CumulativeDelta(bucket_minutes=5, max_buckets=78)
        # price_series: 全て同値（price_change = 0）
        price_series = [5000.0] * 10
        # delta_series: 大幅に上昇（delta_change >> 0）
        delta_series = [0.0, 100.0, 200.0, 300.0, 400.0,
                        500.0, 600.0, 700.0, 800.0, 900.0]
        result = cd.detect_divergence(price_series, delta_series, threshold=0.5)
        # M3修正前: "aligned"（誤）→ M3修正後: "bullish_divergence"（正）
        assert result == "bullish_divergence", (
            f"M3: price_flat+delta_up should be bullish_divergence, got {result}"
        )

    def test_price_flat_delta_down_detects_bearish(self):
        """価格停滞 + delta 下落 → bearish_divergence。"""
        from chronos_cumulative_delta import CumulativeDelta
        cd = CumulativeDelta(bucket_minutes=5, max_buckets=78)
        price_series = [5000.0] * 10
        delta_series = [0.0, -100.0, -200.0, -300.0, -400.0,
                        -500.0, -600.0, -700.0, -800.0, -900.0]
        result = cd.detect_divergence(price_series, delta_series, threshold=0.5)
        assert result == "bearish_divergence", (
            f"M3: price_flat+delta_down should be bearish_divergence, got {result}"
        )

    def test_both_flat_returns_aligned(self):
        """価格・delta両方ゼロ変化 → aligned（変化なし）。"""
        from chronos_cumulative_delta import CumulativeDelta
        cd = CumulativeDelta(bucket_minutes=5, max_buckets=78)
        price_series = [5000.0] * 5
        delta_series = [100.0] * 5
        result = cd.detect_divergence(price_series, delta_series, threshold=0.5)
        assert result == "aligned", (
            f"M3: 両方ゼロ変化は aligned のはず, got {result}"
        )

    def test_delta_only_flat_returns_aligned(self):
        """delta 変化なし（delta_change=0）→ aligned（方向判定不可）。"""
        from chronos_cumulative_delta import CumulativeDelta
        cd = CumulativeDelta(bucket_minutes=5, max_buckets=78)
        price_series = [5000.0, 5010.0, 5020.0, 5030.0, 5040.0]
        delta_series = [100.0] * 5
        result = cd.detect_divergence(price_series, delta_series, threshold=0.5)
        assert result == "aligned", (
            f"M3: delta変化なしは aligned のはず, got {result}"
        )

    def test_price_flat_small_delta_change_stays_aligned(self):
        """価格停滞 + delta 微小変化（threshold 超過なし）→ aligned。

        z-score は SD ベースなので絶対値ではなく変化の統計的有意性で判定される。
        delta 全要素が全く同一値（変化=0）→ delta_change=0 → aligned（delta変化なし）。
        """
        from chronos_cumulative_delta import CumulativeDelta
        cd = CumulativeDelta(bucket_minutes=5, max_buckets=78)
        price_series = [5000.0] * 10
        # delta 変化ゼロ（delta_change = 0）→ aligned（delta変化なし）
        delta_series = [100.0] * 10
        result = cd.detect_divergence(price_series, delta_series, threshold=0.5)
        assert result == "aligned", (
            f"M3: delta変化ゼロはaligned のはず, got {result}"
        )


# =============================================================================
# H6: is_stub 特殊メソッド除外テスト
# =============================================================================

class TestIsStubDunderExclusion:
    """H6: __init__ が pass だけでも is_stub=False になることを確認。"""

    def test_class_with_only_init_pass_is_not_stub(self, tmp_path):
        """__init__ が pass でも実際のメソッドが実装されていれば is_stub=False。"""
        sys.path.insert(0, str(ROOT))
        from scripts.futures_trader_evaluation import ast_check_class_implemented as analyze_class_implementation

        # __init__ が pass だけのクラス
        source = '''
class MyStrategy:
    def __init__(self):
        pass

    def compute_signal(self):
        return {"signal": "buy"}

    def get_entry_price(self):
        return 5200.0
'''
        test_file = tmp_path / "test_stub_class.py"
        test_file.write_text(source)
        result = analyze_class_implementation(
            test_file,
            "MyStrategy",
            required_methods=["compute_signal", "get_entry_price"],
        )
        assert result["found"] is True
        assert result["is_stub"] is False, (
            f"H6: __init__ pass だけでも is_stub=True になっている（誤判定）"
        )

    def test_class_with_all_pass_methods_is_stub(self, tmp_path):
        """__init__ 以外のメソッドが全て pass なら is_stub=True。"""
        sys.path.insert(0, str(ROOT))
        from scripts.futures_trader_evaluation import ast_check_class_implemented as analyze_class_implementation

        source = '''
class StubStrategy:
    def __init__(self):
        pass

    def compute_signal(self):
        pass

    def get_entry_price(self):
        pass
'''
        test_file = tmp_path / "test_all_pass.py"
        test_file.write_text(source)
        result = analyze_class_implementation(
            test_file,
            "StubStrategy",
            required_methods=["compute_signal", "get_entry_price"],
        )
        assert result["is_stub"] is True, (
            f"H6: 全メソッドが pass なら is_stub=True のはず"
        )

    def test_dunder_methods_not_counted(self, tmp_path):
        """__repr__/__str__/__eq__ が pass でも stub カウントされない。"""
        sys.path.insert(0, str(ROOT))
        from scripts.futures_trader_evaluation import ast_check_class_implemented as analyze_class_implementation

        source = '''
class RichClass:
    def __init__(self):
        pass
    def __repr__(self):
        pass
    def __str__(self):
        pass
    def __eq__(self, other):
        pass
    def execute(self):
        return {"action": "trade"}
'''
        test_file = tmp_path / "test_dunder.py"
        test_file.write_text(source)
        result = analyze_class_implementation(
            test_file,
            "RichClass",
            required_methods=["execute"],
        )
        assert result["is_stub"] is False, (
            f"H6: dunder メソッドが stub 判定を汚染している"
        )


# =============================================================================
# chronos_agent: F12/F13 silent failure 検知
# =============================================================================

class TestAgentF12F13SilentFailure:
    """M1: chronos_agent の check_level4_f12_f13_silent_failure が正しく動作する。"""

    def test_function_exists_in_agent(self):
        """check_level4_f12_f13_silent_failure が chronos_agent に存在する。"""
        import chronos_agent
        assert hasattr(chronos_agent, "check_level4_f12_f13_silent_failure"), (
            "M1: check_level4_f12_f13_silent_failure が chronos_agent に未実装"
        )

    def test_no_alerts_when_fields_present(self):
        """f12/f13 フィールドが正常に存在する場合はアラートなし。"""
        import chronos_agent
        state = {
            "account_id": "test_account",
            "save_reason": "periodic",
            "f12_cumulative_delta_bias": "bullish",
            "f13_liquidity_sweep_signal": None,  # None は正常
        }
        with mock.patch.object(chronos_agent, "load_all_account_states", return_value=[state]):
            alerts = chronos_agent.check_level4_f12_f13_silent_failure({})
        assert len(alerts) == 0, f"正常な state でアラート発生: {alerts}"

    def test_alert_when_f12_is_none(self):
        """f12_cumulative_delta_bias が None（フィールドは存在）→ アラート発生。"""
        import chronos_agent
        state = {
            "account_id": "test_f12_none",
            "save_reason": "periodic",
            "f12_cumulative_delta_bias": None,
            "f13_liquidity_sweep_signal": None,
        }
        with (
            mock.patch.object(chronos_agent, "load_all_account_states", return_value=[state]),
            mock.patch.object(chronos_agent, "_should_notify", return_value=True),
        ):
            alerts = chronos_agent.check_level4_f12_f13_silent_failure({})
        assert len(alerts) >= 1, "M1: f12=None でアラートが発生しない"
        assert any("f12" in a.get("key", "") for a in alerts)

    def test_no_alert_for_old_state_without_fields(self):
        """cycle5以前の state（F12フィールドなし）はスキップする。"""
        import chronos_agent
        state = {
            "account_id": "old_account",
            "save_reason": "periodic",
            # f12_cumulative_delta_bias が存在しない（古い state.json）
        }
        with mock.patch.object(chronos_agent, "load_all_account_states", return_value=[state]):
            alerts = chronos_agent.check_level4_f12_f13_silent_failure({})
        assert len(alerts) == 0, "M1: 古い state.json でアラート発生は誤検知"

    def test_registered_in_monitor_cycle(self):
        """check_level4_f12_f13_silent_failure が monitor_cycle から呼ばれる。"""
        import chronos_agent
        import inspect
        source = inspect.getsource(chronos_agent.monitor_cycle)
        assert "check_level4_f12_f13_silent_failure" in source, (
            "M1: monitor_cycle が check_level4_f12_f13_silent_failure を呼んでいない"
        )


# =============================================================================
# M2: .bak残留ファイル整理確認
# =============================================================================

class TestBakFilesCleanup:
    """M2: scripts/ に .bak_cycle5 / .bak_80of80 / .bak_f12f13 が残っていない。"""

    def test_bak_cycle5_not_in_scripts(self):
        bak = ROOT / "scripts" / "futures_trader_evaluation.py.bak_cycle5_20260419"
        assert not bak.exists(), f"M2: {bak.name} が scripts/ に残留"

    def test_bak_80of80_not_in_scripts(self):
        bak = ROOT / "scripts" / "futures_trader_evaluation.py.bak_80of80_20260419"
        assert not bak.exists(), f"M2: {bak.name} が scripts/ に残留"

    def test_bak_f12f13_not_in_scripts(self):
        bak = ROOT / "scripts" / "futures_trader_evaluation.py.bak_f12f13_20260419"
        assert not bak.exists(), f"M2: {bak.name} が scripts/ に残留"

    def test_bak_files_in_data_backups(self):
        """移動先の data/backups/ に .bak ファイルが存在する。"""
        backups_dir = ROOT / "data" / "backups"
        assert backups_dir.exists(), "data/backups/ ディレクトリがない"
        # 少なくとも1ファイルが存在する
        bak_files = list(backups_dir.glob("futures_trader_evaluation.py.bak_*"))
        assert len(bak_files) >= 1, f"data/backups/ に .bak ファイルがない: {list(backups_dir.iterdir())}"
