"""tests/test_hil_scenarios_20260425.py — HIL シナリオ注入テスト (2026-04-25)

hil_scenarios.py の 5 シナリオが portfolio_risk_gate / chainguard_wrapper /
synthetic_option_chain と正しく連携することを 10 件以上のテストで検証する。

asyncio 禁止 (B16 規律) — 全テストは純粋な同期コード。
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from atlas_v3.ops.chainguard_wrapper import _clear_cache
from atlas_v3.ops.portfolio_risk_gate import (
    DEFAULT_GATE_CONFIG,
    GateConfig,
    reset_gate_state,
)
from tests.fixtures.hil_scenarios import (
    HILScenarioResult,
    make_crash_10_scenario,
    make_earnings_announce_scenario,
    make_gap_up_5_scenario,
    make_iv_crush_scenario,
    make_vix_spike_30_scenario,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """各テスト前後に gate state / chainguard cache をリセットする。"""
    reset_gate_state()
    _clear_cache()
    yield
    reset_gate_state()
    _clear_cache()


# ────────────────────────────────────────────────────────────
# TC-01: HILScenarioResult の共通型・構造確認
# ────────────────────────────────────────────────────────────

class TestHILResultStructure:
    """TC-01: HILScenarioResult の共通プロパティが正常に返ること。"""

    @pytest.mark.parametrize("make_fn, expected_name", [
        (make_vix_spike_30_scenario, "vix_spike_30pct"),
        (make_gap_up_5_scenario, "gap_up_5pct"),
        (make_crash_10_scenario, "crash_10pct"),
        (make_iv_crush_scenario, "iv_crush"),
        (make_earnings_announce_scenario, "earnings_announce"),
    ])
    def test_scenario_name_set_correctly(self, make_fn, expected_name):
        """各シナリオの scenario_name が正しく設定されること。"""
        result = make_fn()
        assert result.scenario_name == expected_name

    @pytest.mark.parametrize("make_fn", [
        make_vix_spike_30_scenario,
        make_gap_up_5_scenario,
        make_crash_10_scenario,
        make_iv_crush_scenario,
        make_earnings_announce_scenario,
    ])
    def test_chain_df_is_dataframe(self, make_fn):
        """全シナリオが pandas DataFrame を返すこと。"""
        result = make_fn()
        assert isinstance(result.chain_df, pd.DataFrame)

    @pytest.mark.parametrize("make_fn", [
        make_vix_spike_30_scenario,
        make_gap_up_5_scenario,
        make_crash_10_scenario,
        make_iv_crush_scenario,
        make_earnings_announce_scenario,
    ])
    def test_chain_df_has_required_columns(self, make_fn):
        """chain DataFrame が必須カラムを持つこと。"""
        required = {
            "code", "strike_price", "option_type",
            "delta", "gamma", "theta", "vega",
            "implied_volatility", "open_interest", "volume",
        }
        result = make_fn()
        missing = required - set(result.chain_df.columns)
        assert not missing, f"Missing columns: {missing}"


# ────────────────────────────────────────────────────────────
# TC-02: VIX spike 30% シナリオ
# ────────────────────────────────────────────────────────────

class TestVIXSpike30:
    """TC-02: VIX 30% スパイクシナリオの動作確認。"""

    def test_entry_allowed_when_vix_below_halt_threshold(self):
        """base_vix=15 → spiked=19.5 は halt 閾値 30.0 未満で entry 許可。"""
        result = make_vix_spike_30_scenario(base_vix=15.0)
        assert result.vix == pytest.approx(15.0 * 1.30, rel=1e-3)
        assert result.entry_allowed is True

    def test_entry_halted_when_vix_exceeds_halt_threshold(self):
        """base_vix=25 → spiked=32.5 は halt 閾値 30.0 超で entry halt。"""
        result = make_vix_spike_30_scenario(base_vix=25.0)
        assert result.vix > 30.0
        assert result.entry_allowed is False

    def test_vix_spike_iv_increases(self):
        """VIX スパイク時に chain の ATM IV が base より高くなること。"""
        result = make_vix_spike_30_scenario(base_vix=15.0)
        atm_iv = result.atm_iv
        assert atm_iv is not None
        # IV は 0.20 * 1.30 = 0.26 前後 (ATM 付近)
        assert atm_iv > 0.20

    def test_center_price_matches_underlying(self):
        """chainguard center price が underlying_price と一致すること。"""
        result = make_vix_spike_30_scenario()
        assert result.center_price == pytest.approx(result.underlying_price, rel=1e-6)

    def test_halt_reason_contains_vix(self):
        """halt 時の reason 文字列に 'VIX' が含まれること。"""
        result = make_vix_spike_30_scenario(base_vix=25.0)
        assert result.entry_allowed is False
        assert "VIX" in result.gate_decision.reason


# ────────────────────────────────────────────────────────────
# TC-03: gap up +5% シナリオ
# ────────────────────────────────────────────────────────────

class TestGapUp5:
    """TC-03: gap up +5% シナリオの動作確認。"""

    def test_underlying_price_increased_by_5pct(self):
        """underlying_price が base の 1.05x になっていること。"""
        base = 540.0
        result = make_gap_up_5_scenario(base_underlying=base)
        assert result.underlying_price == pytest.approx(base * 1.05, rel=1e-6)

    def test_entry_allowed_on_gap_up(self):
        """gap up 時は VIX が下がるため entry が許可されること。"""
        result = make_gap_up_5_scenario(base_vix=14.0)
        assert result.entry_allowed is True

    def test_center_price_reflects_gap(self):
        """center price が gap 後の価格を正確に反映すること。"""
        base = 540.0
        result = make_gap_up_5_scenario(base_underlying=base)
        assert result.center_price == pytest.approx(base * 1.05, rel=1e-6)
        assert result.center_price_source == "live"


# ────────────────────────────────────────────────────────────
# TC-04: crash -10% シナリオ
# ────────────────────────────────────────────────────────────

class TestCrash10:
    """TC-04: crash -10% シナリオの動作確認。"""

    def test_underlying_price_decreased_by_10pct(self):
        """underlying_price が base の 0.90x になっていること。"""
        base = 540.0
        result = make_crash_10_scenario(base_underlying=base)
        assert result.underlying_price == pytest.approx(base * 0.90, rel=1e-6)

    def test_entry_halted_on_crash(self):
        """crash 時は VIX が急騰して entry halt になること。"""
        result = make_crash_10_scenario(base_vix=18.0)
        # crash_vix = 18.0 * 2.0 = 36.0 > 30.0 (halt threshold)
        assert result.vix > 30.0
        assert result.entry_allowed is False

    def test_iv_elevated_on_crash(self):
        """crash 時に chain の ATM IV が通常より高くなること。"""
        result = make_crash_10_scenario()
        atm_iv = result.atm_iv
        assert atm_iv is not None
        # crash_iv = 0.17 * 1.40 = 0.238 前後
        assert atm_iv > 0.20

    def test_put_delta_negative_on_crash(self):
        """crash シナリオで ATM put の delta が負であること。"""
        result = make_crash_10_scenario()
        puts = result.chain_df[result.chain_df["option_type"] == "PUT"]
        assert not puts.empty
        idx = (puts["strike_price"] - result.underlying_price).abs().idxmin()
        put_delta = float(puts.loc[idx, "delta"])
        assert put_delta < 0.0, f"ATM put delta should be negative, got {put_delta}"


# ────────────────────────────────────────────────────────────
# TC-05: IV crush シナリオ
# ────────────────────────────────────────────────────────────

class TestIVCrush:
    """TC-05: IV crush シナリオの動作確認。"""

    def test_iv_is_half_of_base(self):
        """crush 後の iv が 0.125 (= 0.25 * 0.50) 前後であること。"""
        result = make_iv_crush_scenario()
        assert result.iv == pytest.approx(0.25 * 0.50, rel=1e-3)

    def test_entry_allowed_on_iv_crush(self):
        """IV crush 後は VIX が低水準なので entry 許可。"""
        result = make_iv_crush_scenario(base_vix=12.0)
        assert result.entry_allowed is True

    def test_option_premium_lower_than_normal(self):
        """IV crush 後の option premium が通常 IV より低くなること。"""
        # normal シナリオ (IV=0.17) と比較
        from tests.fixtures.synthetic_option_chain import generate_option_chain, ChainParams

        crush_result = make_iv_crush_scenario()
        normal_params = ChainParams(
            symbol="US.SPY",
            underlying_price=crush_result.underlying_price,
            iv=0.17,
            expiry_days=0.02,
            scenario="normal",
        )
        normal_df = generate_option_chain(normal_params)

        # ATM call の last_price を比較
        crush_calls = crush_result.chain_df[crush_result.chain_df["option_type"] == "CALL"]
        normal_calls = normal_df[normal_df["option_type"] == "CALL"]

        if crush_calls.empty or normal_calls.empty:
            pytest.skip("No calls in chain")

        crush_idx = (crush_calls["strike_price"] - crush_result.underlying_price).abs().idxmin()
        normal_idx = (normal_calls["strike_price"] - crush_result.underlying_price).abs().idxmin()

        crush_premium = float(crush_calls.loc[crush_idx, "last_price"])
        normal_premium = float(normal_calls.loc[normal_idx, "last_price"])

        assert crush_premium < normal_premium, (
            f"IV crush premium ({crush_premium:.4f}) should be < "
            f"normal premium ({normal_premium:.4f})"
        )

    def test_center_price_unchanged_by_iv_crush(self):
        """IV crush は underlying price に影響しないので center price が変わらないこと。"""
        base = 540.0
        result = make_iv_crush_scenario(base_underlying=base)
        assert result.center_price == pytest.approx(base, rel=1e-6)


# ────────────────────────────────────────────────────────────
# TC-06: earnings announce シナリオ
# ────────────────────────────────────────────────────────────

class TestEarningsAnnounce:
    """TC-06: 決算発表前シナリオの動作確認。"""

    def test_iv_elevated_before_earnings(self):
        """決算前は IV が 1.6x に上昇すること。"""
        result = make_earnings_announce_scenario()
        expected_iv = 0.17 * 1.60
        assert result.iv == pytest.approx(expected_iv, rel=1e-3)

    def test_entry_allowed_before_earnings(self):
        """決算前 VIX=16 は halt 閾値未満で entry 許可。"""
        result = make_earnings_announce_scenario(base_vix=16.0)
        assert result.entry_allowed is True

    def test_atm_call_delta_near_half(self):
        """ATM call delta が 0.40-0.65 の範囲内であること (BSM 理論値)。"""
        result = make_earnings_announce_scenario()
        delta = result.atm_call_delta
        assert delta is not None
        assert 0.40 <= delta <= 0.65, (
            f"ATM call delta={delta:.4f} out of expected range [0.40, 0.65]"
        )

    def test_chain_has_both_calls_and_puts(self):
        """chain DataFrame に CALL と PUT が両方存在すること。"""
        result = make_earnings_announce_scenario()
        types = set(result.chain_df["option_type"].unique())
        assert "CALL" in types
        assert "PUT" in types


# ────────────────────────────────────────────────────────────
# TC-07: max_concurrent_entries halt
# ────────────────────────────────────────────────────────────

class TestMaxEntriesHalt:
    """TC-07: current_entries >= max_concurrent_entries で halt になること。"""

    def test_halt_on_max_entries_exceeded(self):
        """current_entries=10 (デフォルト max=10) で entry halt。"""
        config = GateConfig(max_concurrent_entries=10)
        result = make_gap_up_5_scenario(current_entries=10, gate_config=config)
        assert result.entry_allowed is False
        assert "max_concurrent_entries" in result.gate_decision.active_rules

    def test_allow_below_max_entries(self):
        """current_entries=9 (max=10) では entry 許可。"""
        config = GateConfig(max_concurrent_entries=10)
        result = make_gap_up_5_scenario(current_entries=9, gate_config=config)
        assert result.entry_allowed is True


# ────────────────────────────────────────────────────────────
# TC-08: hil_all_scenarios fixture 互換確認 (inline)
# ────────────────────────────────────────────────────────────

class TestAllScenariosDict:
    """TC-08: make_* を全件呼び出したときの結果が全て HILScenarioResult であること。"""

    def test_all_five_scenarios_return_hil_result(self):
        """5 シナリオ全てが HILScenarioResult を返すこと。"""
        makers = [
            make_vix_spike_30_scenario,
            make_gap_up_5_scenario,
            make_crash_10_scenario,
            make_iv_crush_scenario,
            make_earnings_announce_scenario,
        ]
        for make_fn in makers:
            result = make_fn()
            assert isinstance(result, HILScenarioResult), (
                f"{make_fn.__name__} did not return HILScenarioResult"
            )

    def test_scenario_names_are_unique(self):
        """5 シナリオの scenario_name が全て異なること。"""
        names = [
            make_vix_spike_30_scenario().scenario_name,
            make_gap_up_5_scenario().scenario_name,
            make_crash_10_scenario().scenario_name,
            make_iv_crush_scenario().scenario_name,
            make_earnings_announce_scenario().scenario_name,
        ]
        assert len(names) == len(set(names)), f"Duplicate scenario names: {names}"


# ────────────────────────────────────────────────────────────
# TC-09: put-call parity の簡易確認 (BSM self-consistency)
# ────────────────────────────────────────────────────────────

class TestPutCallParity:
    """TC-09: synthetic chain の put-call parity が近似的に成立すること。

    C - P ≈ S - K * exp(-r * T) (配当ゼロ・近似)
    """

    @pytest.mark.parametrize("make_fn", [
        make_vix_spike_30_scenario,
        make_gap_up_5_scenario,
        make_crash_10_scenario,
        make_iv_crush_scenario,
        make_earnings_announce_scenario,
    ])
    def test_put_call_parity_approximate(self, make_fn):
        """各シナリオで ATM の put-call parity が 5% 以内に収まること。"""
        result = make_fn()
        df = result.chain_df
        S = result.underlying_price
        r = 0.045  # ChainParams default risk_free_rate
        T = max(0.02 / 365.0, 0.5 / 24.0 / 365.0)  # expiry_days=0.02

        # ATM に最も近い strike を選択
        strikes = df["strike_price"].unique()
        atm_K = float(min(strikes, key=lambda k: abs(k - S)))

        call_row = df[(df["strike_price"] == atm_K) & (df["option_type"] == "CALL")]
        put_row = df[(df["strike_price"] == atm_K) & (df["option_type"] == "PUT")]

        if call_row.empty or put_row.empty:
            pytest.skip("ATM call or put not found in chain")

        C = float(call_row["last_price"].iloc[0])
        P = float(put_row["last_price"].iloc[0])

        lhs = C - P
        rhs = S - atm_K * math.exp(-r * T)

        # 許容誤差: 5% (合成 chain の IV skew 等の影響を考慮)
        if abs(rhs) > 0.01:
            rel_err = abs(lhs - rhs) / abs(rhs)
            assert rel_err < 0.05, (
                f"Put-call parity violated for {result.scenario_name}: "
                f"C-P={lhs:.4f}, S-K*e^(-rT)={rhs:.4f}, rel_err={rel_err:.3%}"
            )


# ────────────────────────────────────────────────────────────
# TC-10: hil_scenario_factory fixture (inline call)
# ────────────────────────────────────────────────────────────

class TestHILScenarioFactory:
    """TC-10: hil_scenario_factory 相当のファクトリが全シナリオをカバーすること。"""

    @pytest.mark.parametrize("scenario_name", [
        "vix_spike_30",
        "gap_up_5",
        "crash_10",
        "iv_crush",
        "earnings_announce",
    ])
    def test_factory_builds_each_scenario(self, scenario_name):
        """factory 経由で各シナリオが正常に生成されること。"""
        from tests.fixtures.hil_scenarios import (
            make_vix_spike_30_scenario,
            make_gap_up_5_scenario,
            make_crash_10_scenario,
            make_iv_crush_scenario,
            make_earnings_announce_scenario,
        )
        builders = {
            "vix_spike_30": make_vix_spike_30_scenario,
            "gap_up_5": make_gap_up_5_scenario,
            "crash_10": make_crash_10_scenario,
            "iv_crush": make_iv_crush_scenario,
            "earnings_announce": make_earnings_announce_scenario,
        }
        result = builders[scenario_name]()
        assert isinstance(result, HILScenarioResult)
        assert result.chain_row_count > 0
        assert result.center_price > 0.0

    def test_custom_gate_config_applied(self):
        """カスタム GateConfig が正しく適用されること。

        vix_warning_threshold は vix_halt_threshold 以下でなければならない制約があるため、
        両方をセットで指定する。
        """
        strict_config = GateConfig(
            vix_halt_threshold=10.0,
            vix_warning_threshold=8.0,  # warning < halt の制約を守る
        )
        result = make_vix_spike_30_scenario(base_vix=8.0, gate_config=strict_config)
        # spiked_vix = 8.0 * 1.30 = 10.4 > 10.0 (custom threshold)
        assert result.entry_allowed is False
