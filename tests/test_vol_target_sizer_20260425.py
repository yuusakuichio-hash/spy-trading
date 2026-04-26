"""tests/test_vol_target_sizer_20260425.py — L10 VolTargetSizer pytest"""
import math
import pytest
from atlas_v3.bots.engines.vol_target_sizer import VolTargetSizer, VolTargetResult


@pytest.fixture
def sizer():
    return VolTargetSizer(target_vol=0.12)


def _flat_returns(n: int, daily_return: float) -> list:
    """daily_return を「daily ボラ」として標本に与える。
    std dev = daily_return となるよう符号交番で生成 (realized_vol_ann が
    daily_return * sqrt(252) と一致)。元実装の同値羅列は std=0 で
    test_high_vol_reduces_factor 等の期待値が成立しなかった (2026-04-25 修正)。"""
    return [daily_return if i % 2 == 0 else -daily_return for i in range(n)]


class TestVolTargetSizerBasic:
    def test_matched_vol_factor_one(self, sizer):
        # realized annual vol = 12% → size_factor = 1.0
        # daily vol = 0.12 / sqrt(252) ≈ 0.00756
        daily_vol = 0.12 / math.sqrt(252)
        returns = _flat_returns(20, daily_vol)
        result = sizer.compute(returns)
        assert result.size_factor == pytest.approx(1.0, abs=0.05)

    def test_high_vol_reduces_factor(self, sizer):
        # realized vol = 24% → factor = 12/24 = 0.5
        daily_vol = 0.24 / math.sqrt(252)
        returns = _flat_returns(20, daily_vol)
        result = sizer.compute(returns)
        assert result.size_factor == pytest.approx(0.5, abs=0.05)

    def test_low_vol_capped_at_one(self, sizer):
        # realized vol = 6% → factor = 12/6 = 2.0 → capped at 1.0
        daily_vol = 0.06 / math.sqrt(252)
        returns = _flat_returns(20, daily_vol)
        result = sizer.compute(returns)
        assert result.size_factor == pytest.approx(1.0, abs=0.001)

    def test_very_high_vol_small_factor(self, sizer):
        daily_vol = 0.60 / math.sqrt(252)
        returns = _flat_returns(20, daily_vol)
        result = sizer.compute(returns)
        assert result.size_factor < 0.25


class TestVolTargetSizerEdgeCases:
    def test_insufficient_data_fallback(self, sizer):
        # < 2 returns → fallback to size_factor=1.0
        result = sizer.compute([0.001])
        assert result.size_factor == 1.0
        assert "insufficient" in result.reason.lower() or result.source == "fallback"

    def test_empty_returns_fallback(self, sizer):
        result = sizer.compute([])
        assert result.size_factor == 1.0

    def test_zero_vol_returns_factor_one(self, sizer):
        # all-zero returns → std=0 → fallback
        returns = [0.0] * 20
        result = sizer.compute(returns)
        assert result.size_factor == 1.0

    def test_nan_return_raises_or_fallback(self, sizer):
        returns = [0.001, float("nan"), 0.002]
        result = sizer.compute(returns)
        # Should either raise or return fallback (not crash silently)
        assert isinstance(result, VolTargetResult)


class TestVolTargetSizerResult:
    def test_result_is_dataclass(self, sizer):
        result = sizer.compute(_flat_returns(20, 0.005))
        assert isinstance(result, VolTargetResult)
        assert hasattr(result, "size_factor")
        assert hasattr(result, "realized_vol_ann")
        assert hasattr(result, "target_vol")
        assert hasattr(result, "reason")
        assert hasattr(result, "source")

    def test_target_vol_preserved(self, sizer):
        result = sizer.compute(_flat_returns(20, 0.005))
        assert result.target_vol == pytest.approx(0.12)


class TestVolTargetSizerCustomTarget:
    def test_custom_target_vol(self):
        sizer = VolTargetSizer(target_vol=0.20)
        result = sizer.compute(_flat_returns(20, 0.20 / math.sqrt(252)))
        assert result.size_factor == pytest.approx(1.0, abs=0.05)
        assert result.target_vol == pytest.approx(0.20)
