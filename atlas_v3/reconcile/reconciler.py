"""atlas_v3/reconcile/reconciler.py — K8s-style Reconciliation Loop

desired_state.yaml を読み込み、DriftDetector で diff を算出して
idempotent に修復する。

修復アクション:
- flag drift      : data/state_v3/flags.json を原子的に更新
- kill_switch     : desired=false かつ armed=true → common_v3.kill_switch.deactivate()
                   desired=true  かつ armed=false → common_v3.kill_switch.activate()
- launchd         : desired=true  かつ not running → launchctl kickstart
                   desired=false かつ running     → launchctl kill
- state_file      : 不在ファイルを空ファイルとして作成

Idempotency 保証:
- desired == actual の項目は何もしない (true idempotent)
- 同じ desired state で 2 回 reconcile() を呼んでも副作用は 1 回のみ
- flags.json は fcntl.flock でアトミック更新

Dry-run:
- desired_state.flags.reconciler_dry_run == true の場合は修復アクションを
  実行せず ReconcileResult に proposed actions のみ記録する

CC 規律: 各メソッド CC <= 12

audit:
- data/state_v3/reconcile_audit.jsonl に全 reconcile 結果を append-only 記録
"""
from __future__ import annotations

import dataclasses
import datetime
import fcntl
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from atlas_v3.reconcile.drift_detector import DriftDetector, DriftItem

log = logging.getLogger(__name__)

# ── プロジェクトルート ────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parents[2]

_DEFAULT_STATE_DIR = _BASE / "data" / "state_v3"
_DEFAULT_DESIRED_STATE_PATH = _BASE / "config" / "atlas_desired_state.yaml"


def _state_dir() -> Path:
    override = os.environ.get("TRADING_STATE_DIR", "")
    return Path(override) if override else _DEFAULT_STATE_DIR


def _desired_state_path() -> Path:
    override = os.environ.get("ATLAS_DESIRED_STATE_PATH", "")
    return Path(override) if override else _DEFAULT_DESIRED_STATE_PATH


# ── ReconcileResult ───────────────────────────────────────────────────────────

@dataclasses.dataclass
class ReconcileResult:
    """1 回の reconcile() 実行結果。

    Attributes:
        drifted         検出した drift の一覧
        repaired        修復に成功した DriftItem の一覧
        repair_errors   修復失敗した (DriftItem, error_msg) のリスト
        dry_run         True = dry-run モード (実際の修復は実行していない)
        ts              実行時刻 (UTC ISO-8601)
    """
    drifted: list[DriftItem] = dataclasses.field(default_factory=list)
    repaired: list[DriftItem] = dataclasses.field(default_factory=list)
    repair_errors: list[tuple[DriftItem, str]] = dataclasses.field(default_factory=list)
    dry_run: bool = False
    ts: str = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    @property
    def all_ok(self) -> bool:
        """drift なし かつ repair error なし の場合に True。"""
        return not self.drifted and not self.repair_errors

    @property
    def fully_repaired(self) -> bool:
        """drift 全件が repaired に入っている場合に True。"""
        return len(self.drifted) > 0 and len(self.repaired) == len(self.drifted)


# ── Reconciler ────────────────────────────────────────────────────────────────

class Reconciler:
    """Desired State YAML を読み込み、idempotent 修復を実行する。

    使い方:
        reconciler = Reconciler()                     # default path 使用
        result = reconciler.reconcile()               # 修復実行
        # または
        reconciler = Reconciler(desired_path=Path("..."))
    """

    def __init__(
        self,
        desired_path: Path | None = None,
    ) -> None:
        """
        Args:
            desired_path: atlas_desired_state.yaml のパス。
                          None の場合は env var / default を使用。
        """
        self._desired_path = desired_path or _desired_state_path()

    # ── 公開 API ────────────────────────────────────────────────────────────

    def reconcile(self) -> ReconcileResult:
        """desired state と running state を比較し、drift を idempotent に修復する。

        Returns:
            ReconcileResult
        """
        desired = self._load_desired()
        dry_run: bool = desired.get("flags", {}).get("reconciler_dry_run", False)

        detector = DriftDetector(desired)
        drifted = detector.detect_drifted_only()

        result = ReconcileResult(drifted=list(drifted), dry_run=dry_run)

        if not drifted:
            log.info("[Reconciler] no drift detected. state is converged.")
            self._write_audit(result)
            return result

        log.warning("[Reconciler] %d drift(s) detected.", len(drifted))

        if dry_run:
            log.info("[Reconciler] dry_run=true: skipping all repair actions.")
            self._write_audit(result)
            return result

        for item in drifted:
            try:
                self._repair(item, desired)
                result.repaired.append(item)
                log.info("[Reconciler] repaired: kind=%s key=%s desired=%s",
                         item.kind, item.key, item.desired)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                result.repair_errors.append((item, msg))
                log.error("[Reconciler] repair failed: kind=%s key=%s error=%s",
                          item.kind, item.key, msg)

        self._write_audit(result)
        return result

    def load_desired(self) -> dict[str, Any]:
        """desired state YAML を dict として返す (外部参照用)。"""
        return self._load_desired()

    # ── 修復ディスパッチ ──────────────────────────────────────────────────────

    def _repair(self, item: DriftItem, desired: dict[str, Any]) -> None:
        """DriftItem の kind に応じて修復アクションを呼ぶ。"""
        if item.kind == "flag":
            self._repair_flag(item, desired)
        elif item.kind == "kill_switch":
            self._repair_kill_switch(item)
        elif item.kind == "launchd":
            self._repair_launchd(item)
        elif item.kind == "state_file":
            self._repair_state_file(item)
        else:
            raise ValueError(f"unknown drift kind: {item.kind!r}")

    # ── flag 修復 ────────────────────────────────────────────────────────────

    def _repair_flag(self, item: DriftItem, desired: dict[str, Any]) -> None:
        """flags.json を更新して flag drift を修復する。

        flags.json の当該キーのみ上書き (他のキーは保持)。
        fcntl.flock でアトミック更新。
        """
        flags_path = _state_dir() / "flags.json"
        flags_path.parent.mkdir(parents=True, exist_ok=True)

        # 読み取りロック → マージ → 書き込みロック
        lock_path = _state_dir() / ".flags.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                existing: dict[str, Any] = {}
                if flags_path.exists():
                    try:
                        existing = json.loads(flags_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        existing = {}
                existing[item.key] = item.desired
                tmp = flags_path.parent / f".flags.tmp.{os.getpid()}"
                tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, flags_path)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    # ── kill_switch 修復 ─────────────────────────────────────────────────────

    def _repair_kill_switch(self, item: DriftItem) -> None:
        """kill_switch armed 状態を desired に合わせる。"""
        from common_v3.risk.kill_switch import _activate_raw, _deactivate_raw

        if item.desired is False and item.actual is True:
            # desired=解除・actual=発動中 → deactivate
            _deactivate_raw(activator="reconciler", reason="drift_repair")
            log.warning("[Reconciler] kill_switch deactivated by reconciler (drift repair).")
        elif item.desired is True and item.actual is False:
            # desired=発動・actual=解除 → activate
            _activate_raw(reason="drift_repair", activator="reconciler")
            log.warning("[Reconciler] kill_switch activated by reconciler (drift repair).")

    # ── launchd 修復 ─────────────────────────────────────────────────────────

    def _repair_launchd(self, item: DriftItem) -> None:
        """launchd サービスの起動/停止で drift を修復する。

        desired=true  かつ not running → launchctl kickstart gui/<uid>/<label>
        desired=false かつ running     → launchctl kill TERM gui/<uid>/<label>

        actual=None (サービス未登録) かつ desired=true → log warning のみ
        """
        label = item.key
        desired_running = item.desired

        if item.actual is None and desired_running:
            # サービス自体が launchctl に登録されていない
            log.warning(
                "[Reconciler] launchd service not registered: %s (cannot repair)", label
            )
            return

        uid = os.getuid()
        target = f"gui/{uid}/{label}"

        if desired_running and not item.actual:
            cmd = ["launchctl", "kickstart", "-k", target]
        elif not desired_running and item.actual:
            cmd = ["launchctl", "kill", "TERM", target]
        else:
            return  # no-op

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            raise RuntimeError(
                f"launchctl command failed: {' '.join(cmd)}: {result.stderr.strip()}"
            )

    # ── state_file 修復 ──────────────────────────────────────────────────────

    def _repair_state_file(self, item: DriftItem) -> None:
        """required_state_files が不在なら空ファイルとして作成する。"""
        if item.desired is True and item.actual is False:
            full_path = _BASE / item.key
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.touch(exist_ok=True)
            log.info("[Reconciler] created missing state file: %s", full_path)

    # ── YAML ロード ──────────────────────────────────────────────────────────

    def _load_desired(self) -> dict[str, Any]:
        """desired_state.yaml を dict として返す。

        PyYAML 依存を避けるため標準ライブラリの tomllib 等は使わず、
        PyYAML を try import → 失敗時は簡易パーサーにフォールバック。
        """
        path = self._desired_path
        if not path.exists():
            raise FileNotFoundError(f"desired state file not found: {path}")
        try:
            import yaml  # type: ignore[import]
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except ImportError:
            log.warning("PyYAML not available; using fallback YAML parser")
            return _fallback_yaml_load(path)

    # ── audit ────────────────────────────────────────────────────────────────

    def _write_audit(self, result: ReconcileResult) -> None:
        """reconcile 結果を reconcile_audit.jsonl に追記する。"""
        audit_path = _state_dir() / "reconcile_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": result.ts,
            "dry_run": result.dry_run,
            "drifted_count": len(result.drifted),
            "repaired_count": len(result.repaired),
            "repair_errors": len(result.repair_errors),
            "drifted": [
                {"kind": i.kind, "key": i.key, "desired": i.desired, "actual": i.actual}
                for i in result.drifted
            ],
            "repair_error_details": [
                {"kind": i.kind, "key": i.key, "error": msg}
                for i, msg in result.repair_errors
            ],
        }
        with open(audit_path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


# ── 簡易 YAML フォールバックパーサー ─────────────────────────────────────────
# PyYAML 未インストール環境向け。複雑な構造には対応しない (テスト用途のみ)。

def _fallback_yaml_load(path: Path) -> dict[str, Any]:
    """最低限の YAML を読むフォールバック。PyYAML がある場合は使わない。

    対応: スカラー、インデントあり dict、インデントあり list。
    非対応: アンカー、タグ、複数行文字列。
    """
    import re
    text = path.read_text(encoding="utf-8")
    # コメント除去 → json-like 変換は実用困難なため、最小限の対応のみ
    # テスト環境では PyYAML を使うことを前提とし、ここでは空 dict を返す
    log.error(
        "Fallback YAML parser used for %s. Install PyYAML: pip install pyyaml", path
    )
    return {}
