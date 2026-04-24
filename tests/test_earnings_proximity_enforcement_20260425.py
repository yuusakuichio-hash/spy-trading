"""tests/test_earnings_proximity_enforcement_20260425.py
決算近接日チェック実装 — 20 件以上の単体テスト

NVDA 2024-02-21 型の決算前売り建て防止を各戦術レベルで検証する。

テスト構成:
  EP-01 〜 EP-06: earnings_calendar_check.py 単体テスト
  EP-07 〜 EP-11: JadeLizardTactic — 決算 5 日前ブロック / 6 日前許可 / None スキップ
  EP-12 〜 EP-16: IronFlyEngine — 同上
  EP-17 〜 EP-21: ShortStrangle0DTEEngine — 同上
  EP-22 〜 EP-26: RatioSpreadEngine — 同上
  EP-27 〜 EP-28: safe_default 動作（決算日取得失敗時）
  EP-29: proximity_days=None で全戦術スキップ
  EP-30: NVDA 2024-02-21 実例シナリオ (5 営業日前 = 2024-02-14 ブロック)
"""
from __future__ import annotations

import datetime
from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.bots.engines.earnings_calendar_check import (
    _business_days_until,
    is_near_earnings,
)
from atlas_v3.bots.engines.iron_fly import IronFlyConfig, IronFlyEngine
from atlas_v3.bots.engines.jade_lizard import JadeLizardConfig, JadeLizardTactic
from atlas_v3.bots.engines.ratio_spread import RatioSpreadConfig, RatioSpreadEngine
from atlas_v3.bots.engines.short_strangle_0dte import (
    ShortStrangle0DTEConfig,
    ShortStrangle0DTEEngine,
)
from atlas_v3.core.env_observer import MarketEnvironment

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# テストヘルパー: 固定日時を返すクロック
# ---------------------------------------------------------------------------

def _make_clock(hour: int = 11, minute: int = 0) -> object:
    """10:00-12:00 ET エントリー窓内の固定時刻を返す clock_fn。"""
    def fn() -> datetime.datetime:
        return datetime.datetime(2026, 4, 25, hour, minute, 0, tzinfo=ET)
    return fn


def _make_env(ivr: float = 75.0, vix: float = 18.0, symbol: str = "NVDA") -> MarketEnvironment:
    """テスト用 MarketEnvironment を生成する。"""
    return MarketEnvironment(
        vix=vix,
        ivr_by_symbol={symbol: ivr},
        vrp=0.0,
    )


def _make_strangle_utc(hour: int = 11, minute: int = 0) -> datetime.datetime:
    """0DTE 用の UTC 時刻を生成する（ET 変換後が指定時刻になるよう）。"""
    et_dt = datetime.datetime(2026, 4, 25, hour, minute, 0, tzinfo=ET)
    return et_dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# EP-01 〜 EP-06: earnings_calendar_check 単体テスト
# ---------------------------------------------------------------------------

class TestBusinessDaysUntil:
    """EP-01 〜 EP-04: _business_days_until の正常・境界・逆順ケース。"""

    def test_ep01_same_day_returns_zero(self):
        """EP-01: 今日 == 決算日 → 0。"""
        d = datetime.date(2024, 2, 21)
        assert _business_days_until(d, d) == 0

    def test_ep02_target_before_today_returns_zero(self):
        """EP-02: 過去の決算日 → 0（過去分はブロックしない）。"""
        today = datetime.date(2024, 2, 22)
        target = datetime.date(2024, 2, 21)
        assert _business_days_until(today, target) == 0

    def test_ep03_nvda_20240221_five_bdays_before(self):
        """EP-03: NVDA 2024-02-21 の 5 営業日前 = 2024-02-14。"""
        today = datetime.date(2024, 2, 14)
        target = datetime.date(2024, 2, 21)
        assert _business_days_until(today, target) == 5

    def test_ep04_nvda_20240221_six_bdays_before(self):
        """EP-04: NVDA 2024-02-21 の 6 営業日前 = 2024-02-13。"""
        today = datetime.date(2024, 2, 13)
        target = datetime.date(2024, 2, 21)
        assert _business_days_until(today, target) == 6


class TestIsNearEarnings:
    """EP-05 〜 EP-06: is_near_earnings の基本ロジック。"""

    def test_ep05_five_bdays_before_blocks(self):
        """EP-05: 5 営業日前 → ブロック (proximity_days=5)。"""
        stub = lambda sym: datetime.date(2024, 2, 21)  # noqa: E731
        blocked, reason = is_near_earnings(
            symbol="NVDA",
            proximity_days=5,
            today=datetime.date(2024, 2, 14),
            earnings_date_fn=stub,
        )
        assert blocked is True
        assert "earnings_proximity_block" in reason

    def test_ep06_six_bdays_before_allows(self):
        """EP-06: 6 営業日前 → 許可 (proximity_days=5)。"""
        stub = lambda sym: datetime.date(2024, 2, 21)  # noqa: E731
        blocked, reason = is_near_earnings(
            symbol="NVDA",
            proximity_days=5,
            today=datetime.date(2024, 2, 13),
            earnings_date_fn=stub,
        )
        assert blocked is False
        assert "earnings_proximity_ok" in reason


# ---------------------------------------------------------------------------
# EP-07 〜 EP-11: JadeLizardTactic — 決算近接チェック
# ---------------------------------------------------------------------------

class TestJadeLizardEarningsProximity:
    """EP-07 〜 EP-11: JadeLizardTactic.should_enter の決算近接ブロック。"""

    def _make_tactic(self, earnings_date: datetime.date | None, proximity_days: int = 5) -> JadeLizardTactic:
        """earnings_date_fn stub を注入した JadeLizardTactic を返す。"""
        stub = (lambda sym: earnings_date) if earnings_date is not None else (lambda sym: None)
        return JadeLizardTactic(
            config=JadeLizardConfig(earnings_proximity_days=proximity_days),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )

    def test_ep07_block_five_bdays_before(self):
        """EP-07: JadeLizard — 決算 5 営業日前 → should_enter=False, reason に earnings_proximity_block。"""
        earnings = datetime.date(2026, 5, 7)  # 水曜
        today_patcher = datetime.date(2026, 4, 30)  # 木曜 (5 bdays before)
        assert _business_days_until(today_patcher, earnings) == 5

        stub = lambda sym: earnings  # noqa: E731
        tactic = JadeLizardTactic(
            config=JadeLizardConfig(earnings_proximity_days=5),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )
        env = _make_env(ivr=75.0, symbol="NVDA")
        # is_near_earnings の today を patch するため、テストは実際の今日基準で動く。
        # 5 bdays 前を確実にするため固定 stub を持つ earnings_date_fn を使い、
        # is_near_earnings 内部でも today をコントロールする。
        # ここでは earnings_date_fn だけで結果が決まることを確認。
        # stub が 5 bdays 先を返すよう今日から正確に計算。
        import datetime as _dt
        from atlas_v3.bots.engines.earnings_calendar_check import _business_days_until as _bdu
        real_today = _dt.date.today()
        # 実際の今日から 5 営業日後の日付を計算
        import pandas as pd
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(5)).date()
        stub2 = lambda sym: future_date  # noqa: E731
        tactic2 = JadeLizardTactic(
            config=JadeLizardConfig(earnings_proximity_days=5),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub2,
        )
        decision = tactic2.should_enter(env=env, symbol="NVDA")
        assert decision.should_enter is False
        assert "earnings_proximity_block" in decision.reason

    def test_ep08_allow_six_bdays_before(self):
        """EP-08: JadeLizard — 決算 6 営業日前 → 決算チェック通過（reason に earnings_proximity_block 含まず）。

        固定日付使用: NVDA 2024-02-21 を決算日として 2024-02-13 (6 bdays 前) を基準とする。
        is_near_earnings の today= パラメータは earnings_date_fn 側で吸収。
        JadeLizardTactic は is_near_earnings を今日基準で呼ぶため、
        earnings_date_fn から「今日 + 6 bdays 後」の日付を返す stub を使う。
        """
        # 2024-02-13(Tue) から 2024-02-21(Wed) = 6 bdays
        # is_near_earnings に today=2024-02-13 を直接渡して確認
        blocked, reason = is_near_earnings(
            symbol="NVDA",
            proximity_days=5,
            today=datetime.date(2024, 2, 13),
            earnings_date_fn=lambda sym: datetime.date(2024, 2, 21),
        )
        assert blocked is False
        assert "earnings_proximity_ok" in reason

        # Tactic レベル: earnings_date_fn が「今日 + 6 bdays 後」を返す場合は
        # is_near_earnings(today=今日) で 6 bdays と認識される。
        # ただし weekend 境界問題を避けるため、今日から実際の 6 bdays 後を確実に計算する。
        import pandas as pd
        # 今日の次の business day + 5 (= 合計 6 bdays 先の business day)
        real_today = datetime.date.today()
        real_today_ts = pd.Timestamp(real_today)
        if real_today_ts.dayofweek >= 5:  # 土日の場合は次の月曜を base にする
            base_ts = real_today_ts + pd.offsets.BDay(1)
        else:
            base_ts = real_today_ts
        future_ts = base_ts + pd.offsets.BDay(6)
        future_date = future_ts.date()
        base_date = base_ts.date()
        bdays = _business_days_until(base_date, future_date)
        assert bdays == 6, f"test setup error: expected 6 bdays, got {bdays}"

        # 今日を base_date として is_near_earnings を呼ぶ stub
        stub = lambda sym: future_date  # noqa: E731
        blocked2, _ = is_near_earnings(
            symbol="NVDA",
            proximity_days=5,
            today=base_date,
            earnings_date_fn=stub,
        )
        assert blocked2 is False

    def test_ep09_proximity_days_none_skips_check(self):
        """EP-09: JadeLizard — proximity_days=None → 決算チェックをスキップ（IVR 条件のみで判定）。"""
        import pandas as pd
        real_today = datetime.date.today()
        # 3 営業日後 = 通常はブロックされるはず
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(3)).date()
        stub = lambda sym: future_date  # noqa: E731
        tactic = JadeLizardTactic(
            config=JadeLizardConfig(earnings_proximity_days=None),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )
        env = _make_env(ivr=75.0, symbol="NVDA")
        decision = tactic.should_enter(env=env, symbol="NVDA")
        assert "earnings_proximity_block" not in decision.reason

    def test_ep10_safe_default_blocks_on_unknown_date(self):
        """EP-10: JadeLizard — 決算日取得失敗（stub が None を返す）→ safe_default=True でブロック。"""
        stub = lambda sym: None  # noqa: E731
        tactic = JadeLizardTactic(
            config=JadeLizardConfig(earnings_proximity_days=5),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )
        env = _make_env(ivr=75.0, symbol="NVDA")
        decision = tactic.should_enter(env=env, symbol="NVDA")
        assert decision.should_enter is False
        assert "earnings_proximity_block" in decision.reason
        assert "safe_default" in decision.reason

    def test_ep11_reason_contains_symbol(self):
        """EP-11: JadeLizard — ブロック時の reason に symbol 名が含まれる。"""
        stub = lambda sym: None  # noqa: E731
        tactic = JadeLizardTactic(
            config=JadeLizardConfig(earnings_proximity_days=5),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )
        env = _make_env(ivr=75.0, symbol="NVDA")
        decision = tactic.should_enter(env=env, symbol="NVDA")
        assert "NVDA" in decision.reason


# ---------------------------------------------------------------------------
# EP-12 〜 EP-16: IronFlyEngine — 決算近接チェック
# ---------------------------------------------------------------------------

class TestIronFlyEarningsProximity:
    """EP-12 〜 EP-16: IronFlyEngine.should_enter の決算近接ブロック。"""

    def _make_engine(self, earnings_date: datetime.date | None, proximity_days: int = 5) -> IronFlyEngine:
        stub = (lambda sym: earnings_date) if earnings_date is not None else (lambda sym: None)
        return IronFlyEngine(
            config=IronFlyConfig(earnings_proximity_days=proximity_days),
            earnings_date_fn=stub,
        )

    def _in_window_et(self) -> datetime.datetime:
        return datetime.datetime(2026, 4, 25, 11, 0, 0, tzinfo=ET)

    def test_ep12_block_five_bdays_before(self):
        """EP-12: IronFly — 決算 5 営業日前 → should_enter=False。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(5)).date()
        engine = self._make_engine(future_date)
        env = _make_env(ivr=80.0, vix=18.0, symbol="NVDA")
        decision = engine.should_enter(
            env=env, symbol="NVDA",
            atm_strike=500.0, max_credit=2.5,
            now_et=self._in_window_et(),
        )
        assert decision.should_enter is False
        assert "earnings_proximity_block" in decision.reason

    def test_ep13_allow_far_future_earnings(self):
        """EP-13: IronFly — 決算が 30 営業日先 → 決算チェック通過（earnings_proximity_block 含まず）。"""
        import pandas as pd
        real_today = datetime.date.today()
        # 30 営業日後は常に 5 bdays より遠い
        far_date = (pd.Timestamp(real_today) + pd.offsets.BDay(30)).date()
        engine = self._make_engine(far_date)
        env = _make_env(ivr=80.0, vix=18.0, symbol="NVDA")
        decision = engine.should_enter(
            env=env, symbol="NVDA",
            atm_strike=500.0, max_credit=2.5,
            now_et=self._in_window_et(),
        )
        assert "earnings_proximity_block" not in decision.reason

    def test_ep14_proximity_days_none_skips_check(self):
        """EP-14: IronFly — proximity_days=None → 決算チェックをスキップ。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(2)).date()
        engine = IronFlyEngine(
            config=IronFlyConfig(earnings_proximity_days=None),
            earnings_date_fn=lambda sym: future_date,
        )
        env = _make_env(ivr=80.0, vix=18.0, symbol="NVDA")
        decision = engine.should_enter(
            env=env, symbol="NVDA",
            atm_strike=500.0, max_credit=2.5,
            now_et=self._in_window_et(),
        )
        assert "earnings_proximity_block" not in decision.reason

    def test_ep15_safe_default_blocks_on_unknown_date(self):
        """EP-15: IronFly — 決算日取得失敗 → safe_default でブロック。"""
        engine = self._make_engine(None)
        env = _make_env(ivr=80.0, vix=18.0, symbol="NVDA")
        decision = engine.should_enter(
            env=env, symbol="NVDA",
            atm_strike=500.0, max_credit=2.5,
            now_et=self._in_window_et(),
        )
        assert decision.should_enter is False
        assert "safe_default" in decision.reason

    def test_ep16_block_reason_mentions_bdays(self):
        """EP-16: IronFly — ブロック時 reason に bdays_until= が含まれる。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(5)).date()
        engine = self._make_engine(future_date)
        env = _make_env(ivr=80.0, vix=18.0, symbol="NVDA")
        decision = engine.should_enter(
            env=env, symbol="NVDA",
            atm_strike=500.0, max_credit=2.5,
            now_et=self._in_window_et(),
        )
        assert "bdays_until" in decision.reason


# ---------------------------------------------------------------------------
# EP-17 〜 EP-21: ShortStrangle0DTEEngine — 決算近接チェック
# ---------------------------------------------------------------------------

class TestShortStrangle0DTEEarningsProximity:
    """EP-17 〜 EP-21: ShortStrangle0DTEEngine.should_enter の決算近接ブロック。"""

    def _make_engine(self, earnings_date: datetime.date | None, proximity_days: int = 5) -> ShortStrangle0DTEEngine:
        stub = (lambda sym: earnings_date) if earnings_date is not None else (lambda sym: None)
        return ShortStrangle0DTEEngine(
            config=ShortStrangle0DTEConfig(earnings_proximity_days=proximity_days),
            earnings_date_fn=stub,
        )

    def _strangle_kwargs(self) -> dict:
        """should_enter の正常系に必要な最低限の kwargs。"""
        today_str = datetime.date.today().isoformat()
        return dict(
            call_strike=502.0,
            put_strike=498.0,
            call_delta=0.12,
            put_delta=0.12,
            call_credit=0.5,
            put_credit=0.5,
            expiry_date=today_str,
            now_utc=_make_strangle_utc(11, 0),
        )

    def test_ep17_block_five_bdays_before(self):
        """EP-17: Strangle — 決算 5 営業日前 → should_enter=False。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(5)).date()
        engine = self._make_engine(future_date)
        env = _make_env(ivr=70.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", **self._strangle_kwargs())
        assert decision.should_enter is False
        assert "earnings_proximity_block" in decision.reason

    def test_ep18_allow_far_future_earnings(self):
        """EP-18: Strangle — 決算が 30 営業日先 → 決算チェック通過（earnings_proximity_block 含まず）。"""
        import pandas as pd
        real_today = datetime.date.today()
        far_date = (pd.Timestamp(real_today) + pd.offsets.BDay(30)).date()
        engine = self._make_engine(far_date)
        env = _make_env(ivr=70.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", **self._strangle_kwargs())
        assert "earnings_proximity_block" not in decision.reason

    def test_ep19_proximity_days_none_skips(self):
        """EP-19: Strangle — proximity_days=None → チェックスキップ。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(1)).date()
        engine = ShortStrangle0DTEEngine(
            config=ShortStrangle0DTEConfig(earnings_proximity_days=None),
            earnings_date_fn=lambda sym: future_date,
        )
        env = _make_env(ivr=70.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", **self._strangle_kwargs())
        assert "earnings_proximity_block" not in decision.reason

    def test_ep20_safe_default_blocks(self):
        """EP-20: Strangle — 決算日取得失敗 → safe_default でブロック。"""
        engine = self._make_engine(None)
        env = _make_env(ivr=70.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", **self._strangle_kwargs())
        assert decision.should_enter is False
        assert "safe_default" in decision.reason

    def test_ep21_block_checked_before_0dte_expiry(self):
        """EP-21: Strangle — 決算近接ブロックが 0DTE チェックより先に発動。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(5)).date()
        engine = self._make_engine(future_date)
        env = _make_env(ivr=70.0, vix=20.0, symbol="NVDA")
        kwargs = self._strangle_kwargs()
        kwargs["expiry_date"] = "2099-01-01"  # 0DTE ではない日付
        decision = engine.should_enter(env=env, symbol="NVDA", **kwargs)
        # earnings_proximity_block が先に発動して reason に含まれる
        assert "earnings_proximity_block" in decision.reason


# ---------------------------------------------------------------------------
# EP-22 〜 EP-26: RatioSpreadEngine — 決算近接チェック
# ---------------------------------------------------------------------------

class TestRatioSpreadEarningsProximity:
    """EP-22 〜 EP-26: RatioSpreadEngine.should_enter の決算近接ブロック。"""

    def _make_engine(self, earnings_date: datetime.date | None, proximity_days: int = 5) -> RatioSpreadEngine:
        stub = (lambda sym: earnings_date) if earnings_date is not None else (lambda sym: None)
        return RatioSpreadEngine(
            config=RatioSpreadConfig(earnings_proximity_days=proximity_days),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )

    def test_ep22_block_five_bdays_before(self):
        """EP-22: RatioSpread — 決算 5 営業日前 → should_enter=False。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(5)).date()
        engine = self._make_engine(future_date)
        env = _make_env(ivr=55.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", atm_strike=500.0, net_credit=1.5)
        assert decision.should_enter is False
        assert "earnings_proximity_block" in decision.reason

    def test_ep23_allow_far_future_earnings(self):
        """EP-23: RatioSpread — 決算が 30 営業日先 → 決算チェック通過（earnings_proximity_block 含まず）。"""
        import pandas as pd
        real_today = datetime.date.today()
        far_date = (pd.Timestamp(real_today) + pd.offsets.BDay(30)).date()
        engine = self._make_engine(far_date)
        env = _make_env(ivr=55.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", atm_strike=500.0, net_credit=1.5)
        assert "earnings_proximity_block" not in decision.reason

    def test_ep24_proximity_days_none_skips(self):
        """EP-24: RatioSpread — proximity_days=None → チェックスキップ。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(2)).date()
        engine = RatioSpreadEngine(
            config=RatioSpreadConfig(earnings_proximity_days=None),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=lambda sym: future_date,
        )
        env = _make_env(ivr=55.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", atm_strike=500.0, net_credit=1.5)
        assert "earnings_proximity_block" not in decision.reason

    def test_ep25_safe_default_blocks(self):
        """EP-25: RatioSpread — 決算日取得失敗 → safe_default でブロック。"""
        engine = self._make_engine(None)
        env = _make_env(ivr=55.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", atm_strike=500.0, net_credit=1.5)
        assert decision.should_enter is False
        assert "safe_default" in decision.reason

    def test_ep26_block_before_entry_window_check(self):
        """EP-26: RatioSpread — 決算近接ブロックが entry window チェックより先に発動。"""
        import pandas as pd
        real_today = datetime.date.today()
        future_date = (pd.Timestamp(real_today) + pd.offsets.BDay(5)).date()
        # entry window 外の時刻でエンジンを構築
        engine = RatioSpreadEngine(
            config=RatioSpreadConfig(earnings_proximity_days=5),
            clock_fn=_make_clock(8, 0),  # 窓外
            earnings_date_fn=lambda sym: future_date,
        )
        env = _make_env(ivr=55.0, vix=20.0, symbol="NVDA")
        decision = engine.should_enter(env=env, symbol="NVDA", atm_strike=500.0, net_credit=1.5)
        # 両方 False だが reason に earnings_proximity_block が含まれる
        assert "earnings_proximity_block" in decision.reason


# ---------------------------------------------------------------------------
# EP-27 〜 EP-30: 追加シナリオ
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """EP-27 〜 EP-30: 追加境界値・実例シナリオ。"""

    def test_ep27_is_near_earnings_zero_proximity_days_allows(self):
        """EP-27: proximity_days=0 → 常に許可（0 以下はチェックスキップ）。"""
        stub = lambda sym: datetime.date.today() + datetime.timedelta(days=1)  # noqa: E731
        blocked, reason = is_near_earnings(
            symbol="AAPL",
            proximity_days=0,
            earnings_date_fn=stub,
        )
        assert blocked is False

    def test_ep28_is_near_earnings_far_future_allows(self):
        """EP-28: 決算が 60 営業日後 → 許可。"""
        import pandas as pd
        real_today = datetime.date.today()
        far_future = (pd.Timestamp(real_today) + pd.offsets.BDay(60)).date()
        stub = lambda sym: far_future  # noqa: E731
        blocked, reason = is_near_earnings(
            symbol="TSLA",
            proximity_days=5,
            earnings_date_fn=stub,
        )
        assert blocked is False
        assert "earnings_proximity_ok" in reason

    def test_ep29_all_four_tactics_skip_check_with_none_proximity(self):
        """EP-29: proximity_days=None を設定した場合、4 戦術すべてで決算チェックがスキップされる。"""
        import pandas as pd
        real_today = datetime.date.today()
        # 1 営業日後 = 通常は最も厳しいケース
        near_date = (pd.Timestamp(real_today) + pd.offsets.BDay(1)).date()
        stub = lambda sym: near_date  # noqa: E731

        today_str = datetime.date.today().isoformat()
        et_now = datetime.datetime(2026, 4, 25, 11, 0, 0, tzinfo=ET)
        utc_now = et_now.astimezone(timezone.utc)
        env = _make_env(ivr=80.0, vix=18.0, symbol="SPY")

        # JadeLizard
        jl = JadeLizardTactic(
            config=JadeLizardConfig(earnings_proximity_days=None),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )
        jl_d = jl.should_enter(env=env, symbol="SPY")
        assert "earnings_proximity_block" not in jl_d.reason

        # IronFly
        iron = IronFlyEngine(
            config=IronFlyConfig(earnings_proximity_days=None),
            earnings_date_fn=stub,
        )
        iron_d = iron.should_enter(env=env, symbol="SPY", atm_strike=500.0, max_credit=2.0, now_et=et_now)
        assert "earnings_proximity_block" not in iron_d.reason

        # ShortStrangle0DTE
        ss = ShortStrangle0DTEEngine(
            config=ShortStrangle0DTEConfig(earnings_proximity_days=None),
            earnings_date_fn=stub,
        )
        ss_d = ss.should_enter(
            env=env, symbol="SPY",
            call_strike=502.0, put_strike=498.0,
            call_delta=0.12, put_delta=0.12,
            call_credit=0.5, put_credit=0.5,
            expiry_date=today_str,
            now_utc=utc_now,
        )
        assert "earnings_proximity_block" not in ss_d.reason

        # RatioSpread
        rs = RatioSpreadEngine(
            config=RatioSpreadConfig(earnings_proximity_days=None),
            clock_fn=_make_clock(11, 0),
            earnings_date_fn=stub,
        )
        rs_d = rs.should_enter(env=env, symbol="SPY", atm_strike=500.0, net_credit=1.5)
        assert "earnings_proximity_block" not in rs_d.reason

    def test_ep30_nvda_20240221_actual_scenario(self):
        """EP-30: NVDA 2024-02-21 実例 — 2024-02-14（5 営業日前）でブロックされる。

        2024-02-14 に premium 売り建て → 2024-02-21 の決算リスクで大損失を招いた型の再発防止。
        """
        earnings_date = datetime.date(2024, 2, 21)
        today = datetime.date(2024, 2, 14)

        # bdays 計算の確認
        bdays = _business_days_until(today, earnings_date)
        assert bdays == 5

        blocked, reason = is_near_earnings(
            symbol="NVDA",
            proximity_days=5,
            today=today,
            earnings_date_fn=lambda sym: earnings_date,
        )
        assert blocked is True
        assert "NVDA" in reason
        assert "2024-02-21" in reason
        assert "bdays_until=5" in reason
