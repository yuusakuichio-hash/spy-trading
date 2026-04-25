"""atlas_v3.broker — broker 抽象化 layer (β-2 配線 skeleton)

Responsibility
--------------
moomoo / yfinance / stub の各 provider を統一 interface で扱う Protocol 定義 +
factory:

1. **BrokerProtocol** (本モジュールで定義予定)
   place_order / cancel_order / get_positions / get_account_info の interface
2. **MoomooMetricProvider** (atlas_v3.ops.moomoo_provider)
   moomoo OpenD 経由の本番 broker
3. **YFinanceMetricProvider** (atlas_v3.ops.yfinance_provider)
   moomoo 認証失敗時の fallback (paper のみ)
4. **_StubBroker** (atlas_v3.bots.main 内)
   dry-run / paper smoke 用の発注スキップ broker

## Why

現状 atlas_v3/bots/main.py の build_engine_native() で broker を直接組み立てる際、
各 provider の interface が暗黙の duck typing 一致に依存している。
Protocol 定義があれば mypy 等で型チェック可能になり、新 provider 追加時の
interface 違反を即検知できる。

Boeing 737 MAX MCAS 2018-19 と同型の「複数センサー (provider) 間で
interface 整合性が暗黙化されていて事故」を防ぐ構造的対策。

## Public API (β-2 後段で実装予定)

- ``BrokerProtocol`` (typing.Protocol)
- ``BrokerFactory(mode: Literal["live", "paper", "dry"]) -> BrokerProtocol``

## How to apply

β-2 後段で AtlasEngine constructor を ``broker = BrokerFactory(mode).build()``
に変更することで、mode 切替が 1 引数で完結する。

現状は skeleton。既存 provider を lazy re-export で import path 統一のみ提供。
"""

__all__ = [
    "MoomooMetricProvider",
    "YFinanceMetricProvider",
]


def __getattr__(name):
    """Lazy import で循環 import を回避。"""
    if name == "MoomooMetricProvider":
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        return MoomooMetricProvider
    if name == "YFinanceMetricProvider":
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        return YFinanceMetricProvider
    raise AttributeError(f"module 'atlas_v3.broker' has no attribute {name!r}")
