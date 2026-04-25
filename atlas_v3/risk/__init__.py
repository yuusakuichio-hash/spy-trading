"""atlas_v3.risk — 11 戦術共通リスク管理 facade (β-2 配線 skeleton)

Responsibility
--------------
4 つの個別 guard を集約する単一 entry point:

1. **DrawdownTracker** (atlas_v3.bots.engines.drawdown_tracker)
   ピーク資産比 DD でサイズ係数を動的調整
2. **ConsecutiveLossGuard** (atlas_v3.bots.engines.consecutive_loss_guard)
   連敗数に応じた halt / size_factor 調整
3. **HalfDayGuard** (atlas_v3.bots.engines.half_day_guard)
   NYSE 半日取引日での force_close 時刻動的取得
4. **MarginMonitor** (atlas_v3.bots.engines.margin_monitor)
   margin 使用率の monitoring + escalation

## Why

現状各 guard が独立した dataclass / class で散在し、tactic engine が個別に
呼出す必要がある。共通 RiskAggregator があれば「全 guard 通過か」を 1 メソッドで
判定できる。

これは PG&E 2018 California camp fire (個別装置の monitoring が分散していて
統合可視化されていない事故再発防止と同型の構造的対策。

## Public API (β-2 後段で実装予定)

- ``RiskAggregator(config)`` クラス
  - ``check_all(env, position_state) -> AggregateResult``
    -> 4 guard 全件チェック・1 件でも block なら allowed=False
- ``AggregateResult`` dataclass
  - allowed: bool
  - blocking_guards: list[str]  # block した guard 名のリスト
  - size_factor: float  # 全 guard の最小値
  - halt: bool  # 1 件でも halt 発火なら True

## How to apply

β-2 後段で各 engine の preflight() を:

```python
def preflight(self, env):
    risk = RiskAggregator(self._risk_config)
    result = risk.check_all(env, self._position_state)
    if not result.allowed:
        log.info("[%s.preflight] blocked by %s", self.tactic_name, result.blocking_guards)
        return False
    return True
```

に置換すると、4 guard の個別呼出が 1 行に集約される。

現状は skeleton。既存 4 guard を re-export して import path 統一のみ提供。
"""

from atlas_v3.bots.engines.drawdown_tracker import DrawdownTracker, DrawdownSnapshot
from atlas_v3.bots.engines.consecutive_loss_guard import (
    ConsecutiveLossGuard,
    ConsecutiveLossResult,
)
from atlas_v3.bots.engines.half_day_guard import HalfDayGuard, HalfDayInfo

__all__ = [
    "DrawdownTracker",
    "DrawdownSnapshot",
    "ConsecutiveLossGuard",
    "ConsecutiveLossResult",
    "HalfDayGuard",
    "HalfDayInfo",
]
