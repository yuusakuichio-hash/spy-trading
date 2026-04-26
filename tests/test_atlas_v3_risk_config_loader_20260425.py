"""tests/test_atlas_v3_risk_config_loader_20260425.py — atlas_v3/ops/risk_config_loader.py coverage tests

対象: atlas_v3/ops/risk_config_loader.py (113 stmts)
happy path: 8 件 / error path: 5 件
推定 coverage: ~72%
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from atlas_v3.ops.risk_config_loader import (
    RiskConfigLoadError,
    load_paper_risk_config,
    load_monitor_config_from_yaml,
    load_replay_config_from_yaml,
    read_daily_loss_usd,
    read_drawdown_pct,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_MINIMAL_YAML = textwrap.dedent("""\
    max_notional:
      usd: 10000
    max_daily_loss:
      usd: -500
    max_drawdown:
      pct: 0.15
    sizing:
      method: FIXED
      fixed_size_contracts: 1
      kelly_fraction: 0.25
      vix_size_base: 20.0
""")

_FULL_YAML = textwrap.dedent("""\
    max_notional:
      usd: 20000
    max_daily_loss:
      usd: -800
    max_drawdown:
      pct: 0.20
    max_var:
      usd: 3000
    max_assignment_risk:
      usd: 15000
    max_premium_notional:
      usd: 2000
    sizing:
      method: KELLY
      fixed_size_contracts: 2
      kelly_fraction: 0.5
      vix_size_base: 18.0
""")


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "atlas_paper_risk.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# load_paper_risk_config — happy path
# ---------------------------------------------------------------------------

class TestLoadPaperRiskConfig:
    def test_happy_minimal_yaml(self, tmp_path):
        """最小構成 YAML で RiskConfig が返る。"""
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        config = load_paper_risk_config(config_path=cfg_path)
        assert config.max_notional_usd == 10000.0
        assert config.max_daily_loss_usd == -500.0
        assert config.max_drawdown_pct == 0.15

    def test_happy_full_yaml_optional_fields(self, tmp_path):
        """オプショナルフィールド（max_var / max_assignment_risk / max_premium_notional）も読まれる。"""
        cfg_path = _write_yaml(tmp_path, _FULL_YAML)
        config = load_paper_risk_config(config_path=cfg_path)
        assert config.max_var_usd == 3000.0
        assert config.max_assignment_risk_usd == 15000.0
        assert config.max_premium_notional == 2000.0

    def test_happy_sizing_method_kelly(self, tmp_path):
        """sizing.method=KELLY が PositionSizingMethod.KELLY にマップされる。"""
        from common_v3.risk.engine import PositionSizingMethod
        cfg_path = _write_yaml(tmp_path, _FULL_YAML)
        config = load_paper_risk_config(config_path=cfg_path)
        assert config.sizing_method == PositionSizingMethod.KELLY

    def test_happy_sizing_method_fixed(self, tmp_path):
        """sizing.method=FIXED が PositionSizingMethod.FIXED にマップされる。"""
        from common_v3.risk.engine import PositionSizingMethod
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        config = load_paper_risk_config(config_path=cfg_path)
        assert config.sizing_method == PositionSizingMethod.FIXED

    # --- error path ---

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(RiskConfigLoadError, match="Config file not found"):
            load_paper_risk_config(config_path=tmp_path / "no_such.yaml")

    def test_missing_max_notional_section_raises(self, tmp_path):
        """max_notional セクションが欠けていたら RiskConfigLoadError。"""
        yaml = textwrap.dedent("""\
            max_daily_loss:
              usd: -500
            max_drawdown:
              pct: 0.15
        """)
        cfg_path = _write_yaml(tmp_path, yaml)
        with pytest.raises(RiskConfigLoadError, match="Missing required YAML sections"):
            load_paper_risk_config(config_path=cfg_path)

    def test_invalid_sizing_method_raises(self, tmp_path):
        """未定義の sizing.method は RiskConfigLoadError。"""
        yaml = textwrap.dedent("""\
            max_notional:
              usd: 10000
            max_daily_loss:
              usd: -500
            max_drawdown:
              pct: 0.15
            sizing:
              method: UNKNOWN_METHOD
        """)
        cfg_path = _write_yaml(tmp_path, yaml)
        with pytest.raises(RiskConfigLoadError, match="Unknown sizing method"):
            load_paper_risk_config(config_path=cfg_path)

    def test_invalid_yaml_raises(self, tmp_path):
        """壊れた YAML なら RiskConfigLoadError。"""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text("{ unclosed: [bracket", encoding="utf-8")
        with pytest.raises(RiskConfigLoadError, match="YAML parse error"):
            load_paper_risk_config(config_path=cfg_path)

    def test_non_dict_yaml_raises(self, tmp_path):
        """YAML root が dict でない場合 RiskConfigLoadError。"""
        cfg_path = tmp_path / "list.yaml"
        cfg_path.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(RiskConfigLoadError, match="Expected dict at YAML root"):
            load_paper_risk_config(config_path=cfg_path)


# ---------------------------------------------------------------------------
# read_daily_loss_usd / read_drawdown_pct
# ---------------------------------------------------------------------------

class TestReadScalars:
    def test_happy_read_daily_loss_usd(self, tmp_path):
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        val = read_daily_loss_usd(config_path=cfg_path)
        assert val == -500.0

    def test_happy_read_drawdown_pct(self, tmp_path):
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        val = read_drawdown_pct(config_path=cfg_path)
        assert val == 0.15

    def test_read_daily_loss_section_missing_raises(self, tmp_path):
        yaml = "max_drawdown:\n  pct: 0.10\n"
        cfg_path = _write_yaml(tmp_path, yaml)
        with pytest.raises(RiskConfigLoadError, match="Missing required YAML section"):
            read_daily_loss_usd(config_path=cfg_path)


# ---------------------------------------------------------------------------
# load_monitor_config_from_yaml
# ---------------------------------------------------------------------------

class TestLoadMonitorConfig:
    def test_happy_monitor_config_from_yaml(self, tmp_path):
        """MonitorConfig が YAML 値で構築される。"""
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        mc = load_monitor_config_from_yaml(
            config_path=cfg_path,
            pushover_enabled=False,
        )
        # daily_loss_usd が YAML の -500 と一致する
        assert mc.daily_loss_usd == -500.0
        assert mc.drawdown_pct == 0.15

    def test_happy_override_kwargs(self, tmp_path):
        """override_kwargs で任意フィールドを上書きできる。"""
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        mc = load_monitor_config_from_yaml(
            config_path=cfg_path,
            pushover_enabled=False,
            check_interval_secs=30.0,
        )
        assert mc.check_interval_secs == 30.0

    def test_missing_section_raises(self, tmp_path):
        """max_daily_loss セクションが欠けていたら RiskConfigLoadError。"""
        yaml = "max_drawdown:\n  pct: 0.10\n"
        cfg_path = _write_yaml(tmp_path, yaml)
        with pytest.raises(RiskConfigLoadError, match="Missing required YAML sections"):
            load_monitor_config_from_yaml(config_path=cfg_path)


# ---------------------------------------------------------------------------
# load_replay_config_from_yaml
# ---------------------------------------------------------------------------

class TestLoadReplayConfig:
    def test_happy_replay_config_from_yaml(self, tmp_path):
        """ReplayConfig が YAML 値で構築される。"""
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        rc = load_replay_config_from_yaml(config_path=cfg_path)
        assert rc.max_daily_loss_usd == -500.0
        assert rc.max_drawdown_pct == 0.15

    def test_happy_override_initial_capital(self, tmp_path):
        """override_kwargs で initial_capital を上書きできる。"""
        cfg_path = _write_yaml(tmp_path, _MINIMAL_YAML)
        rc = load_replay_config_from_yaml(
            config_path=cfg_path,
            initial_capital=50000.0,
        )
        assert rc.initial_capital == 50000.0

    def test_missing_section_raises(self, tmp_path):
        yaml = "max_notional:\n  usd: 5000\n"
        cfg_path = _write_yaml(tmp_path, yaml)
        with pytest.raises(RiskConfigLoadError, match="Missing required YAML sections"):
            load_replay_config_from_yaml(config_path=cfg_path)
