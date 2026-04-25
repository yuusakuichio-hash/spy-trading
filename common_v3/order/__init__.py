"""common_v3.order — 発注ロジック共通化 (β-2 配線 skeleton)

Responsibility
--------------
11 engine (broken_wing_butterfly / iron_fly / ratio_spread / short_strangle_0dte /
earnings_straddle_buy / pmcc / jade_lizard / weekly_gamma_scalp / diagonal_spread /
vix_tail_hedge / orb_native) の place_order / build_order に散在する以下の処理を
共通化する責務を持つ:

1. PreTradeGate L1-L4 統合チェック (capital_usd / est_margin / US.prefix 補完)
2. PDTGuard 連携 (paper_mode / capital_usd / rolling 3 件判定)
3. KillSwitch 再チェック (tick 開始後に ARMED 化された場合の救済)
4. Idempotency Key 生成 (with_idempotency 経由・OrderNotSentError wrap)
5. estimate_historical_calibration による margin/qty proxy 算出

## Why (このモジュールの存在理由)

2026-04-25 時点で 9 engine が `_Ctx(symbol, qty, side, is_long)` のみ渡し
capital_usd / est_margin が欠落していた → PreTradeGate L3 B-2 fail-closed で
本番経路の発注詰まり (commit `9a0cbb1b` で 9 engine 個別に修正)。

このモジュールで共通化されていれば 1 箇所修正で全 engine が PreTradeGate を
正しく呼ぶ状態を維持できる。これは Knight Capital 2012 ($440M 損失) 型の
「同一バグの 9 箇所コピペ」を防ぐ唯一の構造的対策。

## Public API (β-2 後段で実装予定)

- ``prepare_order_ctx(symbol, qty, side, is_long, capital_usd, legs=None) -> OrderCtx``
  -> US.prefix 補完 + est_margin proxy 算出 + capital_usd 設定済 OrderCtx を返す
- ``check_pre_trade_and_pdt(ctx, paper_mode, tracker=None) -> GateResult``
  -> PreTradeGate + PDTGuard 連携・両方通過時のみ allowed=True
- ``submit_with_idempotency(broker, ctx, decision, env) -> OrderResult``
  -> idempotency_key 生成 → broker.place_order() 呼出 → エラー時 rollback

## How to apply

β-2 後段で各 engine の place_order を本モジュール経由に置換する際:
1. engine 内の ``_Ctx(...)`` 個別呼出を ``prepare_order_ctx(...)`` 経由に変更
2. ``_gate(ctx)`` 呼出を ``check_pre_trade_and_pdt(...)`` に統合
3. PDTGuard 個別 import を削除 (本モジュール内で吸収)
4. 全 engine の test mock も統一可能 (現在 30+ 箇所散在)

現状は skeleton。既存 common_v3.risk.pre_trade_check を re-export することで
import path 統一のみ提供。
"""

from common_v3.risk.pre_trade_check import (
    OrderCtx,
    GateResult,
    check_order,
    check_order_critical_only,
)

__all__ = [
    "OrderCtx",
    "GateResult",
    "check_order",
    "check_order_critical_only",
]
