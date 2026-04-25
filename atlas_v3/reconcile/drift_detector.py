"""atlas_v3/reconcile/drift_detector.py — Drift Detector

desired state と running state の diff を算出する。

検査対象:
1. flags (paper_mode / pdt_constrained / chaos_mode / engine_enabled)
   - running state は data/state_v3/flags.json から読む
   - 存在しない場合はデフォルト値を使用
2. kill_switch.armed
   - common_v3.risk.kill_switch.is_active() で現状を確認
3. launchd_services
   - launchctl list で PID != "-" かどうかで起動中を判定
4. required_state_files
   - Path.exists() で存在確認

設計規律:
- 各検査メソッドは副作用なし (read-only)
- DriftItem は frozen dataclass (immutable)
- launchctl 呼び出し失敗は DriftItem(kind="launchd", error=...) で記録
- @sync_only 不要 (I/O は subprocess / file のみ)

CC 規律: 各メソッド CC <= 10
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── プロジェクトルート ────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parents[2]  # atlas_v3/reconcile → trading/

# flags.json パスを env var で override 可能（テスト隔離）
_DEFAULT_FLAGS_PATH = _BASE / "data" / "state_v3" / "flags.json"


def _flags_path() -> Path:
    override = os.environ.get("ATLAS_FLAGS_STATE_PATH", "")
    return Path(override) if override else _DEFAULT_FLAGS_PATH


# ── DriftItem ─────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class DriftItem:
    """単一 diff 項目。

    Attributes:
        kind        検査カテゴリ ("flag" / "kill_switch" / "launchd" / "state_file")
        key         対象の識別子 (flag 名 / service label / file path)
        desired     desired state の値
        actual      running state の値
        error       検査中に発生したエラーメッセージ (None = 正常)
    """
    kind: str
    key: str
    desired: Any
    actual: Any
    error: str | None = None

    @property
    def is_drifted(self) -> bool:
        """desired != actual かつ error がない場合に True。"""
        if self.error is not None:
            return False
        return self.desired != self.actual

    @property
    def has_error(self) -> bool:
        """検査中にエラーが発生した場合に True。"""
        return self.error is not None


# ── running state 取得ヘルパー ────────────────────────────────────────────────

def _read_flags_state() -> dict[str, Any]:
    """data/state_v3/flags.json を読む。不在または破損時は空 dict。"""
    path = _flags_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("flags.json read error: %s", exc)
        return {}


def _launchctl_running_labels() -> dict[str, bool] | None:
    """launchctl list を実行して {label: is_running} dict を返す。

    launchctl 呼び出し失敗時は None を返す。
    is_running = True: PID 列が "-" でない (実際の PID が存在する)
    """
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            log.warning("launchctl list returned %d: %s", result.returncode, result.stderr)
            return None
        labels: dict[str, bool] = {}
        for line in result.stdout.splitlines()[1:]:  # ヘッダ行をスキップ
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            pid_col = parts[0].strip()
            label = parts[2].strip()
            labels[label] = pid_col != "-"
        return labels
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.warning("launchctl list failed: %s", exc)
        return None


def _kill_switch_is_active() -> bool | None:
    """common_v3.risk.kill_switch.is_active() を呼ぶ。

    import 失敗時は None を返す。
    """
    try:
        from common_v3.risk.kill_switch import _is_active_raw
        return _is_active_raw()
    except Exception as exc:
        log.warning("kill_switch import/call failed: %s", exc)
        return None


# ── DriftDetector ─────────────────────────────────────────────────────────────

class DriftDetector:
    """Running state と desired state の diff を算出する (read-only)。

    desired_state は Reconciler から dict として渡される。
    DriftDetector 自体は副作用を持たない。
    """

    def __init__(self, desired: dict[str, Any]) -> None:
        """
        Args:
            desired: atlas_desired_state.yaml をパースした dict
        """
        self._desired = desired

    # ── 公開 API ────────────────────────────────────────────────────────────

    def detect_all(self) -> list[DriftItem]:
        """全カテゴリの drift を検査して DriftItem リストを返す。"""
        items: list[DriftItem] = []
        items.extend(self._detect_flags())
        items.extend(self._detect_kill_switch())
        items.extend(self._detect_launchd())
        items.extend(self._detect_state_files())
        return items

    def detect_drifted_only(self) -> list[DriftItem]:
        """drift のある項目 (is_drifted=True) のみ返す。"""
        return [i for i in self.detect_all() if i.is_drifted]

    # ── flags ────────────────────────────────────────────────────────────────

    def _detect_flags(self) -> list[DriftItem]:
        """flags セクションの drift を検査する。"""
        desired_flags: dict[str, Any] = self._desired.get("flags", {})
        actual_flags = _read_flags_state()
        items: list[DriftItem] = []

        # reconciler_dry_run はランタイム制御フラグ → drift 対象外
        _SKIP_FLAGS = {"reconciler_dry_run"}

        for flag_name, desired_val in desired_flags.items():
            if flag_name in _SKIP_FLAGS:
                continue
            actual_val = actual_flags.get(flag_name)
            items.append(DriftItem(
                kind="flag",
                key=flag_name,
                desired=desired_val,
                actual=actual_val,
            ))
        return items

    # ── kill_switch ──────────────────────────────────────────────────────────

    def _detect_kill_switch(self) -> list[DriftItem]:
        """kill_switch.armed の drift を検査する。"""
        desired_ks: dict[str, Any] = self._desired.get("kill_switch", {})
        desired_armed: bool = desired_ks.get("armed", False)

        actual_armed = _kill_switch_is_active()
        if actual_armed is None:
            return [DriftItem(
                kind="kill_switch",
                key="armed",
                desired=desired_armed,
                actual=None,
                error="kill_switch import failed",
            )]
        return [DriftItem(
            kind="kill_switch",
            key="armed",
            desired=desired_armed,
            actual=actual_armed,
        )]

    # ── launchd ──────────────────────────────────────────────────────────────

    def _detect_launchd(self) -> list[DriftItem]:
        """launchd_services セクションの drift を検査する。

        chaos_mode フラグ連動: flags.chaos_mode == True なら
        com.soralab.chaos-weekly の desired running を True に上書きする。
        """
        desired_services: list[dict[str, Any]] = self._desired.get("launchd_services", [])
        chaos_mode: bool = self._desired.get("flags", {}).get("chaos_mode", False)

        running_map = _launchctl_running_labels()
        items: list[DriftItem] = []

        for svc in desired_services:
            label: str = svc.get("label", "")
            desired_running: bool = svc.get("running", False)

            # chaos_mode 連動上書き
            if label == "com.soralab.chaos-weekly":
                desired_running = chaos_mode

            if running_map is None:
                items.append(DriftItem(
                    kind="launchd",
                    key=label,
                    desired=desired_running,
                    actual=None,
                    error="launchctl unavailable",
                ))
                continue

            actual_running = running_map.get(label)  # None = サービス未登録
            items.append(DriftItem(
                kind="launchd",
                key=label,
                desired=desired_running,
                actual=actual_running,
            ))
        return items

    # ── state_files ─────────────────────────────────────────────────────────

    def _detect_state_files(self) -> list[DriftItem]:
        """required_state_files の存在チェック。"""
        required: list[str] = self._desired.get("required_state_files", [])
        items: list[DriftItem] = []
        for rel_path in required:
            full_path = _BASE / rel_path
            exists = full_path.exists()
            items.append(DriftItem(
                kind="state_file",
                key=rel_path,
                desired=True,   # 存在すべき
                actual=exists,
            ))
        return items
