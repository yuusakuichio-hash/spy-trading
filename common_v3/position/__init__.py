"""common_v3.position — ポジション snapshot 共通 schema (β-2 配線 skeleton)

Responsibility
--------------
moomoo / yfinance / state_v3 / spy_bot legacy から取得する position を
統一 schema で扱う:

1. ``PositionSnapshot`` dataclass (frozen)
   - symbol / qty / avg_cost / current_price / unrealized_pnl / open_dt
2. ``PositionAggregator``
   - 複数 broker から position を統合
   - 同一銘柄の合算ロジック
3. ``naked_position_detector`` 連携 (atlas_v3.ops.naked_position_detector)
   - SHORT leg のみで LONG hedge 不在の検出

## Why

現状の問題:
- ``spy_bot.py`` の position 取得経路 (legacy・schg lock)
- ``atlas_v3/ops/moomoo_provider.py`` の position 取得 (本番)
- ``data/state_v3/monitor_state.jsonl`` の persisted state
- ``data/atlas_state.json`` の legacy snapshot

これらが個別 schema で乖離しており、各所で type mismatch / field 名違いの
バグが発生する温床になっている (CURRENT_STATE.md L41-58 の atlas-paper crash loop
は SHORT position naked 検出の遅延が遠因)。

## Public API (β-2 後段で実装予定)

- ``PositionSnapshot``: frozen dataclass
- ``PositionAggregator(brokers).all() -> list[PositionSnapshot]``
- ``find_naked_shorts(positions) -> list[PositionSnapshot]``
- ``schema_version: str`` で migration を追跡

## How to apply

β-2 後段で:
1. 各 engine の close_all_positions / get_open_positions を本 schema 経由に
2. legacy spy_bot の dict-based position を frozen dataclass に変換
3. naked_position_detector を本モジュール内に集約

現状は skeleton。
"""

__all__ = []
