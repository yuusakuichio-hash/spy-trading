"""atlas_v3/ops/risk_config_loader.py — atlas_paper_risk.yaml -> RiskConfig / MonitorConfig 変換

責務:
- data/configs/atlas_paper_risk.yaml を読み込み common_v3.risk.engine.RiskConfig を返す
- YAML スキーマ検証（必須キー / 型チェック）
- 読み込み失敗時は RiskConfigLoadError を raise（サイレント失敗禁止）

RT-R2-H3 修正: daily_loss_usd が 3 系統（YAML=-500, MonitorConfig=-400, ReplayConfig=-500）
で不整合だった問題を解消。
- YAML (data/configs/atlas_paper_risk.yaml) を single source of truth とする
- load_monitor_config_from_yaml(): MonitorConfig を YAML から構築
- load_replay_config_from_yaml(): ReplayConfig を YAML から構築
- MonitorConfig/ReplayConfig のハードコードされたデフォルト値は
  起動パスでは使用せず、YAML 経由で値を渡すことを推奨

公開 API:
    RiskConfigLoadError       — 読み込み/変換エラー
    load_paper_risk_config()  — -> common_v3.risk.engine.RiskConfig
    load_monitor_config_from_yaml() — -> atlas_v3.ops.monitor.MonitorConfig (RT-R2-H3)
    load_replay_config_from_yaml()  — -> atlas_v3.ops.replay_bt.ReplayConfig (RT-R2-H3)
    read_daily_loss_usd()     — YAML から daily_loss_usd のみ読む（単体参照用）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parents[2]
_CONFIG_FILE = _BASE / "data" / "configs" / "atlas_paper_risk.yaml"


class RiskConfigLoadError(Exception):
    """atlas_paper_risk.yaml の読み込み/変換エラー。"""


# ---------------------------------------------------------------------------
# 内部ユーティリティ
# ---------------------------------------------------------------------------

def _load_yaml_raw(config_path: Optional[Path] = None) -> dict:
    """YAML を読み込んで dict を返す。"""
    try:
        import yaml
    except ImportError:
        raise RiskConfigLoadError(
            "PyYAML not installed. Run: pip install pyyaml"
        )

    target = config_path or _CONFIG_FILE
    if not target.exists():
        raise RiskConfigLoadError(f"Config file not found: {target}")

    try:
        raw: dict = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as e:
        raise RiskConfigLoadError(f"YAML parse error: {e}")

    if not isinstance(raw, dict):
        raise RiskConfigLoadError(f"Expected dict at YAML root, got {type(raw)}")

    return raw


def _get_float(raw: dict, section: str, key: str) -> float:
    """YAML dict から float 値を取得する。"""
    try:
        val = raw[section][key]
        return float(val)
    except (KeyError, TypeError, ValueError) as e:
        raise RiskConfigLoadError(
            f"Invalid value at {section}.{key}: {e}"
        )


# ---------------------------------------------------------------------------
# 公開 API: RiskConfig
# ---------------------------------------------------------------------------

def load_paper_risk_config(config_path: Path | None = None) -> Any:
    """atlas_paper_risk.yaml を読み込んで RiskConfig を返す。

    Args:
        config_path: テスト用にパスを上書き。None なら _CONFIG_FILE を使用。

    Returns:
        common_v3.risk.engine.RiskConfig

    Raises:
        RiskConfigLoadError: ファイル不在 / スキーマ不正 / 型変換失敗
    """
    from common_v3.risk.engine import RiskConfig, PositionSizingMethod

    raw = _load_yaml_raw(config_path)

    # --- 必須セクション検証 ---
    required_sections = {"max_notional", "max_daily_loss", "max_drawdown"}
    missing = required_sections - raw.keys()
    if missing:
        raise RiskConfigLoadError(f"Missing required YAML sections: {missing}")

    max_notional = _get_float(raw, "max_notional", "usd")
    max_daily_loss = _get_float(raw, "max_daily_loss", "usd")
    max_drawdown_pct = _get_float(raw, "max_drawdown", "pct")

    # オプショナル
    max_premium_notional: float | None = None
    if "max_premium_notional" in raw and isinstance(raw["max_premium_notional"], dict):
        try:
            max_premium_notional = float(raw["max_premium_notional"]["usd"])
        except (KeyError, TypeError, ValueError):
            pass

    max_var_usd = 5000.0
    if "max_var" in raw and isinstance(raw["max_var"], dict):
        try:
            max_var_usd = float(raw["max_var"]["usd"])
        except (KeyError, TypeError, ValueError):
            pass

    max_assignment_risk = 20000.0
    if "max_assignment_risk" in raw and isinstance(raw["max_assignment_risk"], dict):
        try:
            max_assignment_risk = float(raw["max_assignment_risk"]["usd"])
        except (KeyError, TypeError, ValueError):
            pass

    # サイジング
    sizing_raw = raw.get("sizing", {}) or {}
    method_str = str(sizing_raw.get("method", "FIXED")).upper()
    try:
        sizing_method = PositionSizingMethod(method_str.lower())
    except ValueError:
        raise RiskConfigLoadError(
            f"Unknown sizing method: {method_str!r}. "
            f"Must be one of: {[m.value for m in PositionSizingMethod]}"
        )

    fixed_size = int(sizing_raw.get("fixed_size_contracts", 1))
    kelly_fraction = float(sizing_raw.get("kelly_fraction", 0.25))
    vix_size_base = float(sizing_raw.get("vix_size_base", 20.0))

    # --- RiskConfig 構築 ---
    try:
        config = RiskConfig(
            max_notional_usd=max_notional,
            max_daily_loss_usd=max_daily_loss,
            max_drawdown_pct=max_drawdown_pct,
            max_var_usd=max_var_usd,
            max_assignment_risk_usd=max_assignment_risk,
            max_premium_notional=max_premium_notional,
            fixed_size_contracts=fixed_size,
            kelly_fraction=kelly_fraction,
            vix_size_base=vix_size_base,
            sizing_method=sizing_method,
            returns_unit="usd",
        )
    except Exception as e:
        raise RiskConfigLoadError(f"RiskConfig construction failed: {e}")

    return config


# ---------------------------------------------------------------------------
# 公開 API: RT-R2-H3 単一真実源読み取りユーティリティ
# ---------------------------------------------------------------------------

def read_daily_loss_usd(config_path: Optional[Path] = None) -> float:
    """YAML から max_daily_loss.usd のみ読んで返す。

    RT-R2-H3: MonitorConfig / ReplayConfig が YAML の値を参照する際の
    共通エントリポイント。

    Returns:
        daily_loss_usd (負値)

    Raises:
        RiskConfigLoadError: ファイル不在 / 値不正
    """
    raw = _load_yaml_raw(config_path)
    required = {"max_daily_loss"}
    missing = required - raw.keys()
    if missing:
        raise RiskConfigLoadError(f"Missing required YAML section: {missing}")
    return _get_float(raw, "max_daily_loss", "usd")


def read_drawdown_pct(config_path: Optional[Path] = None) -> float:
    """YAML から max_drawdown.pct のみ読んで返す。

    RT-R2-H3: MonitorConfig / ReplayConfig が YAML の値を参照する際の
    共通エントリポイント。

    Returns:
        drawdown_pct (0.0–1.0)

    Raises:
        RiskConfigLoadError: ファイル不在 / 値不正
    """
    raw = _load_yaml_raw(config_path)
    required = {"max_drawdown"}
    missing = required - raw.keys()
    if missing:
        raise RiskConfigLoadError(f"Missing required YAML section: {missing}")
    return _get_float(raw, "max_drawdown", "pct")


def load_monitor_config_from_yaml(
    config_path: Optional[Path] = None,
    **override_kwargs,
) -> Any:
    """atlas_paper_risk.yaml から MonitorConfig を構築する。

    RT-R2-H3: MonitorConfig の daily_loss_usd / drawdown_pct を
    YAML（single source of truth）から読み込む。
    MonitorConfig デフォルト値（daily_loss_usd=-400）との不整合を解消。

    Args:
        config_path:      テスト用にパスを上書き。None なら _CONFIG_FILE を使用。
        **override_kwargs: MonitorConfig の任意フィールドを上書きできる
                          （テスト用途・pushover_enabled=False 等）

    Returns:
        atlas_v3.ops.monitor.MonitorConfig

    Raises:
        RiskConfigLoadError: YAML 読み込み失敗
    """
    from atlas_v3.ops.monitor import MonitorConfig

    raw = _load_yaml_raw(config_path)

    # 必須セクション
    required = {"max_daily_loss", "max_drawdown"}
    missing = required - raw.keys()
    if missing:
        raise RiskConfigLoadError(f"Missing required YAML sections for MonitorConfig: {missing}")

    daily_loss_usd = _get_float(raw, "max_daily_loss", "usd")
    drawdown_pct = _get_float(raw, "max_drawdown", "pct")

    # YAML 値をベースに override_kwargs で上書き
    kwargs: dict = {
        "daily_loss_usd": daily_loss_usd,
        "drawdown_pct": drawdown_pct,
    }
    kwargs.update(override_kwargs)

    try:
        return MonitorConfig(**kwargs)
    except Exception as e:
        raise RiskConfigLoadError(f"MonitorConfig construction failed: {e}")


def load_replay_config_from_yaml(
    config_path: Optional[Path] = None,
    **override_kwargs,
) -> Any:
    """atlas_paper_risk.yaml から ReplayConfig を構築する。

    RT-R2-H3: ReplayConfig の max_daily_loss_usd / max_drawdown_pct を
    YAML（single source of truth）から読み込む。

    Args:
        config_path:      テスト用にパスを上書き。None なら _CONFIG_FILE を使用。
        **override_kwargs: ReplayConfig の任意フィールドを上書きできる

    Returns:
        atlas_v3.ops.replay_bt.ReplayConfig

    Raises:
        RiskConfigLoadError: YAML 読み込み失敗
    """
    from atlas_v3.ops.replay_bt import ReplayConfig

    raw = _load_yaml_raw(config_path)

    required = {"max_daily_loss", "max_drawdown"}
    missing = required - raw.keys()
    if missing:
        raise RiskConfigLoadError(f"Missing required YAML sections for ReplayConfig: {missing}")

    max_daily_loss_usd = _get_float(raw, "max_daily_loss", "usd")
    max_drawdown_pct = _get_float(raw, "max_drawdown", "pct")

    kwargs: dict = {
        "max_daily_loss_usd": max_daily_loss_usd,
        "max_drawdown_pct": max_drawdown_pct,
    }
    kwargs.update(override_kwargs)

    try:
        return ReplayConfig(**kwargs)
    except Exception as e:
        raise RiskConfigLoadError(f"ReplayConfig construction failed: {e}")
