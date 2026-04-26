"""tests/test_reconcile_loop_20260425.py

Atlas K8s-style Reconciliation Loop テスト — 22 件

テスト分類
----------
[drift_detector]  DriftDetector の各検査カテゴリ
[flags]           flag drift 検出・修復
[kill_switch]     kill_switch armed drift 検出・修復
[launchd]         launchd サービス drift 検出・修復
[state_file]      required_state_files 検出・修復
[reconciler]      Reconciler.reconcile() 統合テスト
[dry_run]         dry_run=true で修復アクションを実行しない
[idempotent]      2 回 reconcile() しても副作用が 1 回のみ
[audit]           reconcile_audit.jsonl への記録
[chaos_mode]      flags.chaos_mode 連動で chaos-weekly desired を上書き
[yaml_load]       desired_state.yaml ロード
[result_props]    ReconcileResult.all_ok / fully_repaired プロパティ
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ── テスト対象 import ─────────────────────────────────────────────────────────
from atlas_v3.reconcile.drift_detector import (
    DriftDetector,
    DriftItem,
    _launchctl_running_labels,
    _read_flags_state,
)
from atlas_v3.reconcile.reconciler import ReconcileResult, Reconciler


# ── フィクスチャ ──────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """state_v3 相当の一時ディレクトリを返し、env var を差し替える。"""
    state_dir = tmp_path / "state_v3"
    state_dir.mkdir(parents=True)
    monkeypatch.setenv("TRADING_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ATLAS_FLAGS_STATE_PATH", str(state_dir / "flags.json"))
    # reconciler.py の _state_dir() がこの env var を参照する
    return state_dir


@pytest.fixture()
def desired_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """最小限の desired_state.yaml を tmp_path に作成し env var を差し替える。"""
    y = {
        "version": "1.0",
        "flags": {
            "paper_mode": True,
            "pdt_constrained": True,
            "chaos_mode": False,
            "engine_enabled": True,
            "reconciler_dry_run": False,
        },
        "kill_switch": {"armed": False},
        "launchd_services": [
            {"label": "com.soralab.atlas-paper", "running": True},
            {"label": "com.soralab.chaos-weekly", "running": False},
        ],
        "required_state_files": [
            "data/state_v3/monitor_state.jsonl",
        ],
    }
    p = tmp_path / "atlas_desired_state.yaml"
    p.write_text(yaml.safe_dump(y), encoding="utf-8")
    monkeypatch.setenv("ATLAS_DESIRED_STATE_PATH", str(p))
    return p


@pytest.fixture()
def reconciler(desired_yaml: Path, tmp_state: Path) -> Reconciler:
    """テスト用 Reconciler インスタンス。"""
    return Reconciler(desired_path=desired_yaml)


# ─────────────────────────────────────────────────────────────────────────────
# [drift_detector] — DriftItem dataclass
# ─────────────────────────────────────────────────────────────────────────────

def test_drift_item_is_drifted_true() -> None:
    """[drift_detector] desired != actual → is_drifted=True。"""
    item = DriftItem(kind="flag", key="paper_mode", desired=True, actual=False)
    assert item.is_drifted is True


def test_drift_item_is_drifted_false_when_equal() -> None:
    """[drift_detector] desired == actual → is_drifted=False。"""
    item = DriftItem(kind="flag", key="paper_mode", desired=True, actual=True)
    assert item.is_drifted is False


def test_drift_item_is_drifted_false_when_error() -> None:
    """[drift_detector] error あり → is_drifted=False (エラー状態は drift 扱いしない)。"""
    item = DriftItem(
        kind="kill_switch", key="armed", desired=False, actual=None,
        error="import failed"
    )
    assert item.is_drifted is False
    assert item.has_error is True


def test_drift_item_frozen() -> None:
    """[drift_detector] DriftItem は frozen dataclass。"""
    item = DriftItem(kind="flag", key="k", desired=True, actual=False)
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        item.desired = False  # type: ignore[misc]


import dataclasses


# ─────────────────────────────────────────────────────────────────────────────
# [flags] — flag drift 検出
# ─────────────────────────────────────────────────────────────────────────────

def test_flags_drift_detected_when_missing(
    tmp_state: Path, desired_yaml: Path
) -> None:
    """[flags] flags.json が存在しない場合、全 flag が drift として検出される。"""
    desired = {"flags": {"paper_mode": True, "engine_enabled": True}}
    detector = DriftDetector(desired)
    items = detector._detect_flags()
    assert len(items) == 2
    for item in items:
        assert item.is_drifted is True  # actual=None != desired=True/True


def test_flags_no_drift_when_synced(
    tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[flags] flags.json が desired と一致している場合、drift なし。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "engine_enabled": True}), encoding="utf-8"
    )
    desired = {"flags": {"paper_mode": True, "engine_enabled": True}}
    detector = DriftDetector(desired)
    drifted = [i for i in detector._detect_flags() if i.is_drifted]
    assert drifted == []


def test_flags_partial_drift(
    tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[flags] 一部フラグのみ drift している場合、該当のみ検出。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "engine_enabled": False}), encoding="utf-8"
    )
    desired = {"flags": {"paper_mode": True, "engine_enabled": True}}
    detector = DriftDetector(desired)
    drifted = [i for i in detector._detect_flags() if i.is_drifted]
    assert len(drifted) == 1
    assert drifted[0].key == "engine_enabled"
    assert drifted[0].desired is True
    assert drifted[0].actual is False


def test_flags_reconciler_dry_run_excluded(tmp_state: Path) -> None:
    """[flags] reconciler_dry_run は drift 対象外。"""
    desired = {"flags": {"paper_mode": True, "reconciler_dry_run": False}}
    detector = DriftDetector(desired)
    items = detector._detect_flags()
    keys = [i.key for i in items]
    assert "reconciler_dry_run" not in keys


# ─────────────────────────────────────────────────────────────────────────────
# [kill_switch] — kill_switch drift 検出
# ─────────────────────────────────────────────────────────────────────────────

def test_kill_switch_drift_detected_when_armed(tmp_state: Path) -> None:
    """[kill_switch] desired=false, actual=true → drift 検出。"""
    desired = {"kill_switch": {"armed": False}}
    detector = DriftDetector(desired)
    with patch(
        "atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=True
    ):
        items = detector._detect_kill_switch()
    assert len(items) == 1
    assert items[0].is_drifted is True
    assert items[0].desired is False
    assert items[0].actual is True


def test_kill_switch_no_drift_when_disarmed(tmp_state: Path) -> None:
    """[kill_switch] desired=false, actual=false → drift なし。"""
    desired = {"kill_switch": {"armed": False}}
    detector = DriftDetector(desired)
    with patch(
        "atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=False
    ):
        items = detector._detect_kill_switch()
    assert items[0].is_drifted is False


def test_kill_switch_error_recorded(tmp_state: Path) -> None:
    """[kill_switch] import 失敗時は error フィールドが設定される。"""
    desired = {"kill_switch": {"armed": False}}
    detector = DriftDetector(desired)
    with patch(
        "atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=None
    ):
        items = detector._detect_kill_switch()
    assert items[0].has_error is True


# ─────────────────────────────────────────────────────────────────────────────
# [launchd] — launchd drift 検出
# ─────────────────────────────────────────────────────────────────────────────

def test_launchd_drift_stopped_service(tmp_state: Path) -> None:
    """[launchd] desired=running, actual=stopped → drift 検出。"""
    desired = {
        "flags": {"chaos_mode": False},
        "launchd_services": [{"label": "com.soralab.atlas-paper", "running": True}],
    }
    detector = DriftDetector(desired)
    running_map = {"com.soralab.atlas-paper": False}
    with patch(
        "atlas_v3.reconcile.drift_detector._launchctl_running_labels",
        return_value=running_map,
    ):
        items = detector._detect_launchd()
    assert items[0].is_drifted is True


def test_launchd_no_drift_running_service(tmp_state: Path) -> None:
    """[launchd] desired=running, actual=running → drift なし。"""
    desired = {
        "flags": {"chaos_mode": False},
        "launchd_services": [{"label": "com.soralab.atlas-paper", "running": True}],
    }
    detector = DriftDetector(desired)
    running_map = {"com.soralab.atlas-paper": True}
    with patch(
        "atlas_v3.reconcile.drift_detector._launchctl_running_labels",
        return_value=running_map,
    ):
        items = detector._detect_launchd()
    assert items[0].is_drifted is False


def test_launchd_chaos_mode_overrides_desired(tmp_state: Path) -> None:
    """[chaos_mode] flags.chaos_mode=True → chaos-weekly の desired を True に上書き。"""
    desired = {
        "flags": {"chaos_mode": True},
        "launchd_services": [{"label": "com.soralab.chaos-weekly", "running": False}],
    }
    detector = DriftDetector(desired)
    # chaos-weekly が停止中 (running=False) → chaos_mode=True なので desired=True に上書き
    running_map = {"com.soralab.chaos-weekly": False}
    with patch(
        "atlas_v3.reconcile.drift_detector._launchctl_running_labels",
        return_value=running_map,
    ):
        items = detector._detect_launchd()
    assert items[0].desired is True   # 上書きされた
    assert items[0].is_drifted is True  # actual=False != desired=True


def test_launchd_error_when_launchctl_unavailable(tmp_state: Path) -> None:
    """[launchd] launchctl 呼び出し失敗 → error フィールドが設定される。"""
    desired = {
        "flags": {"chaos_mode": False},
        "launchd_services": [{"label": "com.soralab.atlas-paper", "running": True}],
    }
    detector = DriftDetector(desired)
    with patch(
        "atlas_v3.reconcile.drift_detector._launchctl_running_labels",
        return_value=None,
    ):
        items = detector._detect_launchd()
    assert items[0].has_error is True


# ─────────────────────────────────────────────────────────────────────────────
# [state_file] — required_state_files
# ─────────────────────────────────────────────────────────────────────────────

def test_state_file_drift_when_missing(tmp_path: Path) -> None:
    """[state_file] ファイルが存在しない → drift 検出。"""
    desired = {"required_state_files": ["data/state_v3/monitor_state.jsonl"]}
    detector = DriftDetector(desired)
    # _BASE / "data/state_v3/monitor_state.jsonl" を mock
    with patch(
        "atlas_v3.reconcile.drift_detector._BASE",
        tmp_path,
    ):
        items = detector._detect_state_files()
    assert items[0].is_drifted is True
    assert items[0].actual is False


def test_state_file_no_drift_when_exists(tmp_path: Path) -> None:
    """[state_file] ファイルが存在する → drift なし。"""
    target = tmp_path / "data" / "state_v3" / "monitor_state.jsonl"
    target.parent.mkdir(parents=True)
    target.touch()
    desired = {"required_state_files": ["data/state_v3/monitor_state.jsonl"]}
    detector = DriftDetector(desired)
    with patch("atlas_v3.reconcile.drift_detector._BASE", tmp_path):
        items = detector._detect_state_files()
    assert items[0].is_drifted is False
    assert items[0].actual is True


# ─────────────────────────────────────────────────────────────────────────────
# [reconciler] — Reconciler.reconcile() 統合テスト
# ─────────────────────────────────────────────────────────────────────────────

def test_reconciler_no_drift_returns_all_ok(
    reconciler: Reconciler, tmp_state: Path, tmp_path: Path
) -> None:
    """[reconciler] drift がない場合、result.all_ok=True。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "pdt_constrained": True,
                    "chaos_mode": False, "engine_enabled": True}),
        encoding="utf-8",
    )
    # desired YAML の required_state_files = "data/state_v3/monitor_state.jsonl"
    # _BASE を tmp_path にパッチするため、ファイルを tmp_path/data/state_v3/ に作成する
    monitor_dir = tmp_path / "data" / "state_v3"
    monitor_dir.mkdir(parents=True, exist_ok=True)
    (monitor_dir / "monitor_state.jsonl").touch()

    with (
        patch("atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=False),
        patch("atlas_v3.reconcile.drift_detector._launchctl_running_labels",
              return_value={
                  "com.soralab.atlas-paper": True,
                  "com.soralab.chaos-weekly": False,
              }),
        patch("atlas_v3.reconcile.drift_detector._BASE", tmp_path),
        patch("atlas_v3.reconcile.reconciler._BASE", tmp_path),
    ):
        result = reconciler.reconcile()

    assert result.all_ok is True
    assert result.drifted == []


def test_reconciler_repairs_flag_drift(
    reconciler: Reconciler, tmp_state: Path
) -> None:
    """[flags] engine_enabled drift → reconciler が flags.json を修復する。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "pdt_constrained": True,
                    "chaos_mode": False, "engine_enabled": False}),  # drift: should be True
        encoding="utf-8",
    )
    monitor_path = tmp_state / "monitor_state.jsonl"
    monitor_path.touch()

    with (
        patch("atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=False),
        patch("atlas_v3.reconcile.drift_detector._launchctl_running_labels",
              return_value={
                  "com.soralab.atlas-paper": True,
                  "com.soralab.chaos-weekly": False,
              }),
        patch("atlas_v3.reconcile.drift_detector._BASE", tmp_state.parent),
        patch("atlas_v3.reconcile.reconciler._BASE", tmp_state.parent),
    ):
        result = reconciler.reconcile()

    assert any(i.key == "engine_enabled" for i in result.repaired)
    # flags.json が修復されているか確認
    after = json.loads(flags_path.read_text())
    assert after["engine_enabled"] is True


def test_reconciler_repairs_state_file(
    reconciler: Reconciler, tmp_state: Path
) -> None:
    """[state_file] 不在ファイルを reconciler が作成する。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "pdt_constrained": True,
                    "chaos_mode": False, "engine_enabled": True}),
        encoding="utf-8",
    )
    # monitor_state.jsonl は作成しない → drift 発生

    with (
        patch("atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=False),
        patch("atlas_v3.reconcile.drift_detector._launchctl_running_labels",
              return_value={
                  "com.soralab.atlas-paper": True,
                  "com.soralab.chaos-weekly": False,
              }),
        patch("atlas_v3.reconcile.drift_detector._BASE", tmp_state.parent),
        patch("atlas_v3.reconcile.reconciler._BASE", tmp_state.parent),
    ):
        result = reconciler.reconcile()

    repaired_keys = [i.key for i in result.repaired]
    assert "data/state_v3/monitor_state.jsonl" in repaired_keys
    created = tmp_state.parent / "data" / "state_v3" / "monitor_state.jsonl"
    assert created.exists()


# ─────────────────────────────────────────────────────────────────────────────
# [dry_run] — dry_run=true で修復しない
# ─────────────────────────────────────────────────────────────────────────────

def test_reconciler_dry_run_no_repair(
    tmp_path: Path, tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[dry_run] dry_run=true の場合、drifted に記録されるが repaired は空。"""
    y = {
        "version": "1.0",
        "flags": {
            "paper_mode": True,
            "engine_enabled": True,
            "reconciler_dry_run": True,   # dry_run 有効
        },
        "kill_switch": {"armed": False},
        "launchd_services": [],
        "required_state_files": [],
    }
    p = tmp_path / "desired_dry.yaml"
    p.write_text(yaml.safe_dump(y), encoding="utf-8")
    # flags.json は存在しない → drift 発生

    reconciler = Reconciler(desired_path=p)
    with (
        patch("atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=False),
        patch("atlas_v3.reconcile.drift_detector._launchctl_running_labels",
              return_value={}),
    ):
        result = reconciler.reconcile()

    assert result.dry_run is True
    assert len(result.drifted) > 0
    assert result.repaired == []


# ─────────────────────────────────────────────────────────────────────────────
# [idempotent] — 2 回 reconcile() しても副作用が 1 回のみ
# ─────────────────────────────────────────────────────────────────────────────

def test_reconciler_idempotent_second_call(
    reconciler: Reconciler, tmp_state: Path
) -> None:
    """[idempotent] 2 回目の reconcile() は drift なし (converged)。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "pdt_constrained": True,
                    "chaos_mode": False, "engine_enabled": False}),  # drift
        encoding="utf-8",
    )
    monitor_path = tmp_state / "monitor_state.jsonl"
    monitor_path.touch()

    patch_ks = patch("atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=False)
    patch_lc = patch("atlas_v3.reconcile.drift_detector._launchctl_running_labels",
                     return_value={"com.soralab.atlas-paper": True, "com.soralab.chaos-weekly": False})
    patch_base_d = patch("atlas_v3.reconcile.drift_detector._BASE", tmp_state.parent)
    patch_base_r = patch("atlas_v3.reconcile.reconciler._BASE", tmp_state.parent)

    with patch_ks, patch_lc, patch_base_d, patch_base_r:
        result1 = reconciler.reconcile()   # 1 回目: 修復あり
        result2 = reconciler.reconcile()   # 2 回目: drift なし

    assert len(result1.repaired) > 0
    assert result2.all_ok is True


# ─────────────────────────────────────────────────────────────────────────────
# [audit] — reconcile_audit.jsonl への記録
# ─────────────────────────────────────────────────────────────────────────────

def test_reconciler_writes_audit(
    reconciler: Reconciler, tmp_state: Path
) -> None:
    """[audit] reconcile 後に reconcile_audit.jsonl が書き込まれる。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "pdt_constrained": True,
                    "chaos_mode": False, "engine_enabled": True}),
        encoding="utf-8",
    )
    monitor_path = tmp_state / "monitor_state.jsonl"
    monitor_path.touch()

    with (
        patch("atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=False),
        patch("atlas_v3.reconcile.drift_detector._launchctl_running_labels",
              return_value={"com.soralab.atlas-paper": True, "com.soralab.chaos-weekly": False}),
        patch("atlas_v3.reconcile.drift_detector._BASE", tmp_state.parent),
        patch("atlas_v3.reconcile.reconciler._BASE", tmp_state.parent),
    ):
        reconciler.reconcile()

    audit_path = tmp_state / "reconcile_audit.jsonl"
    assert audit_path.exists()
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert "ts" in record
    assert "drifted_count" in record


# ─────────────────────────────────────────────────────────────────────────────
# [kill_switch] — reconciler が kill_switch drift を修復
# ─────────────────────────────────────────────────────────────────────────────

def test_reconciler_deactivates_kill_switch(
    reconciler: Reconciler, tmp_state: Path
) -> None:
    """[kill_switch] desired=false, actual=true → reconciler が deactivate() を呼ぶ。"""
    flags_path = tmp_state / "flags.json"
    flags_path.write_text(
        json.dumps({"paper_mode": True, "pdt_constrained": True,
                    "chaos_mode": False, "engine_enabled": True}),
        encoding="utf-8",
    )
    monitor_path = tmp_state / "monitor_state.jsonl"
    monitor_path.touch()

    deactivate_mock = MagicMock()
    with (
        patch("atlas_v3.reconcile.drift_detector._kill_switch_is_active", return_value=True),
        patch("atlas_v3.reconcile.drift_detector._launchctl_running_labels",
              return_value={"com.soralab.atlas-paper": True, "com.soralab.chaos-weekly": False}),
        patch("atlas_v3.reconcile.drift_detector._BASE", tmp_state.parent),
        patch("atlas_v3.reconcile.reconciler._BASE", tmp_state.parent),
        patch("atlas_v3.reconcile.reconciler.Reconciler._repair_kill_switch", deactivate_mock),
    ):
        result = reconciler.reconcile()

    ks_drifted = [i for i in result.drifted if i.kind == "kill_switch"]
    assert len(ks_drifted) == 1
    deactivate_mock.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# [yaml_load] — desired_state.yaml ロード
# ─────────────────────────────────────────────────────────────────────────────

def test_reconciler_load_desired_reads_yaml(
    reconciler: Reconciler,
) -> None:
    """[yaml_load] load_desired() が dict を返す。"""
    desired = reconciler.load_desired()
    assert isinstance(desired, dict)
    assert "flags" in desired
    assert "kill_switch" in desired


def test_reconciler_raises_if_yaml_missing(tmp_path: Path) -> None:
    """[yaml_load] desired YAML が存在しない場合 FileNotFoundError。"""
    r = Reconciler(desired_path=tmp_path / "nonexistent.yaml")
    with pytest.raises(FileNotFoundError):
        r.load_desired()


# ─────────────────────────────────────────────────────────────────────────────
# [result_props] — ReconcileResult プロパティ
# ─────────────────────────────────────────────────────────────────────────────

def test_result_all_ok_true_when_empty() -> None:
    """[result_props] drift なし + error なし → all_ok=True。"""
    r = ReconcileResult()
    assert r.all_ok is True


def test_result_fully_repaired() -> None:
    """[result_props] drifted == repaired → fully_repaired=True。"""
    item = DriftItem(kind="flag", key="k", desired=True, actual=False)
    r = ReconcileResult(drifted=[item], repaired=[item])
    assert r.fully_repaired is True


def test_result_not_fully_repaired_on_error() -> None:
    """[result_props] repair_error あり → fully_repaired=False。"""
    item = DriftItem(kind="flag", key="k", desired=True, actual=False)
    r = ReconcileResult(drifted=[item], repaired=[], repair_errors=[(item, "err")])
    assert r.fully_repaired is False
