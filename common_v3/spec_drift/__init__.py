"""common_v3.spec_drift — 仕様 vs 実装 drift 検出 (本実装)

Public API:
- DriftFinding: drift 検出結果 dataclass
- SpecDriftChecker: 仕様 yaml と実装定数を照合
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftFinding:
    """drift 検出結果 1 件."""
    spec_path: str
    impl_path: str
    field_name: str
    spec_value: Any
    impl_value: Any
    severity: str = "warning"  # "info" / "warning" / "critical"

    @property
    def message(self) -> str:
        return (
            f"[{self.severity.upper()}] {self.field_name}: "
            f"spec={self.spec_value!r} != impl={self.impl_value!r} "
            f"({self.spec_path} vs {self.impl_path})"
        )


class SpecDriftChecker:
    """仕様 yaml と実装定数の差分を検出する.

    使い方:
        checker = SpecDriftChecker()
        checker.add_check(spec_path="data/specs/...yaml", spec_field="vix_max",
                          impl_path="atlas_v3/...", impl_value=25.0)
        findings = checker.check()
    """

    def __init__(self) -> None:
        self._checks: list[dict] = []

    def add_check(
        self, spec_path: str, spec_field: str, impl_path: str,
        impl_value: Any, severity: str = "warning",
    ) -> None:
        self._checks.append({
            "spec_path": spec_path,
            "spec_field": spec_field,
            "impl_path": impl_path,
            "impl_value": impl_value,
            "severity": severity,
        })

    def check(self) -> list[DriftFinding]:
        """全 check を実行して drift findings を返す."""
        findings: list[DriftFinding] = []
        for c in self._checks:
            spec_val = self._read_spec_field(c["spec_path"], c["spec_field"])
            if spec_val is None:
                continue  # spec 不在ならスキップ
            if spec_val != c["impl_value"]:
                findings.append(DriftFinding(
                    spec_path=c["spec_path"],
                    impl_path=c["impl_path"],
                    field_name=c["spec_field"],
                    spec_value=spec_val,
                    impl_value=c["impl_value"],
                    severity=c["severity"],
                ))
        return findings

    @staticmethod
    def _read_spec_field(spec_path: str, field_name: str) -> Optional[Any]:
        """spec yaml ファイルから field を読む (yaml ライブラリで)."""
        path = Path(spec_path)
        if not path.exists():
            return None
        try:
            import yaml
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            # ネストしたキーは "a.b.c" 形式で対応
            keys = field_name.split(".")
            cursor = data
            for k in keys:
                if not isinstance(cursor, dict) or k not in cursor:
                    return None
                cursor = cursor[k]
            return cursor
        except Exception as e:
            log.debug("[SpecDrift] read failed: %s: %s", spec_path, e)
            return None


__all__ = [
    "DriftFinding",
    "SpecDriftChecker",
]
