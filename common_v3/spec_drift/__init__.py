"""common_v3.spec_drift — 仕様 vs 実装の drift 検出 (β-2 配線 skeleton)

Responsibility
--------------
1. ``data/specs/v3/*.md`` (仕様書) と実装コードの差分検出
2. ``data/specs/*.yaml`` (Tradeify / MFFU 等の prop firm rules) と
   ``chronos_rules_plugin/*.py`` の整合検証
3. commit 前 hook で drift を block (現状: ``.claude/hooks/spec_premortem_required.sh``)
4. 週次 governance reporting (現状: scripts/weekly_deviation_review.py)

## Why

実装が spec から累積 drift する典型例:
- prop firm rule 改定 (例: Tradeify Profit Split 80%→90%) を spec に反映したが
  実装 plugin が古いまま → 違反トレード
- v3 spec の B14 (Circuit Breaker auto_recovery=False) を一時 True にして commit
- API endpoint 改定を仕様書だけ更新して実装が古い path のまま

これは Therac-25 1985-87 と同型の「仕様書では正しい挙動だが、実装が乖離して
事故」を防ぐ governance 機能。

## Public API (β-2 後段で実装予定)

- ``SpecDriftChecker(spec_dir, impl_paths)``
  - ``check() -> list[DriftFinding]``
- ``DriftFinding``: dataclass (path / line / spec_value / impl_value / severity)
- ``register_pre_commit_hook()``
  -> .claude/hooks/spec_premortem_required.sh と連携

## How to apply

β-2 後段で:
1. 既存 ``scripts/weekly_deviation_review.py`` を本モジュール経由に統一
2. ``.claude/hooks/spec_premortem_required.sh`` の判定 logic を本モジュール呼出に
3. ``data/governance/spec_drift_log.jsonl`` への記録機構

現状は skeleton。
"""

__all__ = []
