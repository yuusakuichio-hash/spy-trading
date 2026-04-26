"""tests/fixtures/hil_scenarios.py — HIL シナリオ注入 fixture (2026-04-25)

Hardware-in-the-Loop (HIL) レベルで合成市場シナリオを atlas_v3/ops の
gate / wrapper ロジックに注入するための高レベル fixture 群。

5 シナリオ:
  1. vix_spike_30pct  — VIX が 30% スパイク (例: 15 → 19.5)
  2. gap_up_5pct      — 原資産が +5% ギャップアップ
  3. crash_10pct      — 原資産が -10% クラッシュ + VIX 急騰
  4. iv_crush         — IV が半分に圧縮 (決算後)
  5. earnings_announce — 決算発表直前の IV 急騰 + volume 急増

設計方針:
  - synthetic_option_chain.generate_option_chain と連携 (高レベル API)
  - asyncio 禁止 (B16 規律): async def / await 使わない
  - 既存コード無変更: tests/ 配下のみで完結
  - GateConfig / check_entry_allowed / get_chain_center_price を直接呼び出し
  - pytest fixture として @pytest.fixture で提供
  - 全 fixture は dataclass で型安全に返す
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# atlas_v3.ops
from atlas_v3.ops.chainguard_wrapper import (
    ChainGuardError,
    MissingPriceError,
    _clear_cache,
    get_chain_center_price,
    get_chain_center_price_with_fallback,
)
from atlas_v3.ops.portfolio_risk_gate import (
    DEFAULT_GATE_CONFIG,
    GateConfig,
    GateDecision,
    check_entry_allowed,
    reset_gate_state,
)

# synthetic option chain
from tests.fixtures.synthetic_option_chain import (
    ChainParams,
    generate_option_chain,
)


# ────────────────────────────────────────────────────────────
# 共通 dataclass
# ────────────────────────────────────────────────────────────

@dataclass
class HILScenarioResult:
    """HIL シナリオ実行結果の統一コンテナ。"""

    scenario_name: str
    underlying_price: float
    vix: float
    iv: float
    chain_df: pd.DataFrame           # BSM 合成 option chain
    gate_decision: GateDecision      # portfolio_risk_gate の判定
    center_price: float              # chainguard center price
    center_price_source: str         # "live" / "cache" / "fallback"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def entry_allowed(self) -> bool:
        return self.gate_decision.allowed

    @property
    def chain_row_count(self) -> int:
        return len(self.chain_df)

    @property
    def atm_call_delta(self) -> float | None:
        """ATM call の delta (最も行使価格が原資産に近い call)。"""
        calls = self.chain_df[self.chain_df["option_type"] == "CALL"]
        if calls.empty:
            return None
        idx = (calls["strike_price"] - self.underlying_price).abs().idxmin()
        return float(calls.loc[idx, "delta"])

    @property
    def atm_iv(self) -> float | None:
        """ATM call の implied_volatility。"""
        calls = self.chain_df[self.chain_df["option_type"] == "CALL"]
        if calls.empty:
            return None
        idx = (calls["strike_price"] - self.underlying_price).abs().idxmin()
        return float(calls.loc[idx, "implied_volatility"])


# ────────────────────────────────────────────────────────────
# 内部: シナリオビルダー
# ────────────────────────────────────────────────────────────

def _build_market_data_dict(symbol: str, price: float) -> dict:
    """chainguard が受け付ける dict 形式の market_data を生成する。"""
    return {symbol: {"last_price": price}}


def _run_scenario(
    *,
    scenario_name: str,
    symbol: str,
    underlying_price: float,
    vix: float,
    iv: float,
    expiry_days: float = 0.02,
    gate_config: GateConfig = DEFAULT_GATE_CONFIG,
    current_entries: int = 0,
    extra: dict | None = None,
) -> HILScenarioResult:
    """シナリオ実行の共通実装。"""
    # cache を必ずフラッシュしてフレッシュな価格取得を強制
    _clear_cache(symbol)
    reset_gate_state()

    # option chain 生成 (synthetic_option_chain の scenario パラメタは
    # underlying_price / iv を直接渡すため scenario="normal" を base にする)
    chain_params = ChainParams(
        symbol=symbol,
        underlying_price=underlying_price,
        iv=iv,
        expiry_days=expiry_days,
        scenario="normal",
    )
    chain_df = generate_option_chain(chain_params)

    # portfolio_risk_gate
    gate_decision = check_entry_allowed(vix, current_entries, gate_config)

    # chainguard center price
    market_data = _build_market_data_dict(symbol, underlying_price)
    center_price, source = get_chain_center_price_with_fallback(
        symbol, market_data, fallback_price=underlying_price
    )

    return HILScenarioResult(
        scenario_name=scenario_name,
        underlying_price=underlying_price,
        vix=vix,
        iv=iv,
        chain_df=chain_df,
        gate_decision=gate_decision,
        center_price=center_price,
        center_price_source=source,
        extra=extra or {},
    )


# ────────────────────────────────────────────────────────────
# 5 シナリオ ビルダー関数 (fixture 外からも直接呼べる)
# ────────────────────────────────────────────────────────────

def make_vix_spike_30_scenario(
    symbol: str = "US.SPY",
    base_underlying: float = 540.0,
    base_vix: float = 15.0,
    current_entries: int = 0,
    gate_config: GateConfig = DEFAULT_GATE_CONFIG,
) -> HILScenarioResult:
    """VIX が 30% スパイクするシナリオ。

    base_vix=15 → vix=19.5 (30% 上昇) は halt 閾値 30.0 未満のため entry は許可される。
    base_vix=25 → vix=32.5 は halt 閾値 30.0 超のため entry halt。
    """
    spiked_vix = base_vix * 1.30
    # IV も VIX に比例して上昇 (0.20 base → 0.26)
    spiked_iv = 0.20 * 1.30
    return _run_scenario(
        scenario_name="vix_spike_30pct",
        symbol=symbol,
        underlying_price=base_underlying,
        vix=spiked_vix,
        iv=spiked_iv,
        current_entries=current_entries,
        gate_config=gate_config,
        extra={"base_vix": base_vix, "spike_pct": 30.0},
    )


def make_gap_up_5_scenario(
    symbol: str = "US.SPY",
    base_underlying: float = 540.0,
    base_vix: float = 14.0,
    current_entries: int = 0,
    gate_config: GateConfig = DEFAULT_GATE_CONFIG,
) -> HILScenarioResult:
    """原資産が +5% ギャップアップするシナリオ。

    ギャップアップ時は VIX は下がる傾向 (approx 0.85x)。
    center price が正確に gap 後の価格を反映することを確認。
    """
    gaped_price = base_underlying * 1.05
    post_gap_vix = base_vix * 0.85
    return _run_scenario(
        scenario_name="gap_up_5pct",
        symbol=symbol,
        underlying_price=gaped_price,
        vix=post_gap_vix,
        iv=0.16,
        current_entries=current_entries,
        gate_config=gate_config,
        extra={"base_underlying": base_underlying, "gap_pct": 5.0},
    )


def make_crash_10_scenario(
    symbol: str = "US.SPY",
    base_underlying: float = 540.0,
    base_vix: float = 18.0,
    current_entries: int = 0,
    gate_config: GateConfig = DEFAULT_GATE_CONFIG,
) -> HILScenarioResult:
    """原資産が -10% クラッシュ + VIX 急騰シナリオ。

    crash 時は VIX が 2.0x 以上に跳ね上がる傾向。
    portfolio_risk_gate は VIX >= 30 で entry halt を返すことを確認。
    """
    crashed_price = base_underlying * 0.90
    crash_vix = base_vix * 2.0  # 18 → 36: halt 閾値 30 超
    crash_iv = 0.17 * 1.40
    return _run_scenario(
        scenario_name="crash_10pct",
        symbol=symbol,
        underlying_price=crashed_price,
        vix=crash_vix,
        iv=crash_iv,
        current_entries=current_entries,
        gate_config=gate_config,
        extra={"base_underlying": base_underlying, "crash_pct": 10.0, "crash_vix": crash_vix},
    )


def make_iv_crush_scenario(
    symbol: str = "US.SPY",
    base_underlying: float = 540.0,
    base_vix: float = 12.0,
    current_entries: int = 0,
    gate_config: GateConfig = DEFAULT_GATE_CONFIG,
) -> HILScenarioResult:
    """決算後 IV が半分に圧縮されるシナリオ。

    IV crush 後の option premium が大幅低下し、
    center price は underlying に影響なし (IV は chain 内のみ変化)。
    """
    crushed_iv = 0.25 * 0.50  # 25% → 12.5%
    return _run_scenario(
        scenario_name="iv_crush",
        symbol=symbol,
        underlying_price=base_underlying,
        vix=base_vix,
        iv=crushed_iv,
        current_entries=current_entries,
        gate_config=gate_config,
        extra={"iv_before": 0.25, "iv_after": crushed_iv, "crush_ratio": 0.50},
    )


def make_earnings_announce_scenario(
    symbol: str = "US.SPY",
    base_underlying: float = 540.0,
    base_vix: float = 16.0,
    current_entries: int = 0,
    gate_config: GateConfig = DEFAULT_GATE_CONFIG,
) -> HILScenarioResult:
    """決算発表直前: IV 急騰 + volume 急増シナリオ。

    決算前は IV が 1.5-2.0x に上昇する。
    VIX は 16 程度で halt 閾値未満 → entry 許可 (通常想定)。
    ATM delta が ±0.5 前後であることを確認。
    """
    pre_earnings_iv = 0.17 * 1.60  # IV 60% 上昇
    return _run_scenario(
        scenario_name="earnings_announce",
        symbol=symbol,
        underlying_price=base_underlying,
        vix=base_vix,
        iv=pre_earnings_iv,
        current_entries=current_entries,
        gate_config=gate_config,
        extra={"iv_multiplier": 1.60, "pre_earnings_iv": pre_earnings_iv},
    )


# ────────────────────────────────────────────────────────────
# pytest fixtures
# ────────────────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def hil_vix_spike_30():
        """VIX 30% スパイクシナリオの HILScenarioResult。"""
        return make_vix_spike_30_scenario()

    @pytest.fixture
    def hil_vix_spike_30_halt():
        """VIX 30% スパイクが halt 閾値を超えるシナリオ (base_vix=25)。"""
        return make_vix_spike_30_scenario(base_vix=25.0)

    @pytest.fixture
    def hil_gap_up_5():
        """gap up +5% シナリオの HILScenarioResult。"""
        return make_gap_up_5_scenario()

    @pytest.fixture
    def hil_crash_10():
        """crash -10% シナリオの HILScenarioResult。"""
        return make_crash_10_scenario()

    @pytest.fixture
    def hil_iv_crush():
        """IV crush シナリオの HILScenarioResult。"""
        return make_iv_crush_scenario()

    @pytest.fixture
    def hil_earnings_announce():
        """決算発表前シナリオの HILScenarioResult。"""
        return make_earnings_announce_scenario()

    @pytest.fixture
    def hil_all_scenarios():
        """全 5 シナリオの結果を dict[str, HILScenarioResult] で返す。"""
        return {
            "vix_spike_30": make_vix_spike_30_scenario(),
            "gap_up_5": make_gap_up_5_scenario(),
            "crash_10": make_crash_10_scenario(),
            "iv_crush": make_iv_crush_scenario(),
            "earnings_announce": make_earnings_announce_scenario(),
        }

    @pytest.fixture
    def hil_scenario_factory():
        """カスタムパラメタで HIL シナリオを生成するファクトリ fixture。"""
        def _make(
            scenario: str,
            symbol: str = "US.SPY",
            base_underlying: float = 540.0,
            base_vix: float = 15.0,
            current_entries: int = 0,
            gate_config: GateConfig = DEFAULT_GATE_CONFIG,
        ) -> HILScenarioResult:
            builders = {
                "vix_spike_30": make_vix_spike_30_scenario,
                "gap_up_5": make_gap_up_5_scenario,
                "crash_10": make_crash_10_scenario,
                "iv_crush": make_iv_crush_scenario,
                "earnings_announce": make_earnings_announce_scenario,
            }
            if scenario not in builders:
                raise ValueError(
                    f"Unknown scenario: {scenario!r}. Available: {list(builders.keys())}"
                )
            return builders[scenario](
                symbol=symbol,
                base_underlying=base_underlying,
                base_vix=base_vix,
                current_entries=current_entries,
                gate_config=gate_config,
            )
        return _make

except ImportError:
    pass
