# Phase 2 Engine Native 移植 — Test Baseline (2026-04-25)

## Summary

9 engine + TradeEngine core の native 移植が test 全件 PASS で baseline 確立。
Knight Capital 型デプロイ不整合 (Redteam a647bd1 判定) の根治済状態。

## Test Results (505/505 PASS)

| Engine | Source spy_bot.py lines | Native file | Tests | Status |
|---|---|---|---|---|
| ORB | 7680-8798 | atlas_v3/bots/engines/orb_native.py | 60 | PASS |
| Calendar | 8836-9393 | atlas_v3/bots/engines/calendar_native.py | 47 | PASS |
| IVCrush | 10905-11311 | atlas_v3/bots/engines/ivcrush_native.py | 33 | PASS |
| StrangleSell | 11333-11845 | atlas_v3/bots/engines/strangle_sell_native.py | 53 | PASS |
| IronCondorSell | 11884-12560 | atlas_v3/bots/engines/iron_condor_sell_native.py | 64 | PASS |
| Butterfly | 12769-13337 | atlas_v3/bots/engines/butterfly_native.py | 68 | PASS |
| Straddle+GammaScalp | 9498-9968 | atlas_v3/bots/engines/straddle_native.py | 74 | PASS |
| StraddleBuy | 10154-10757 | atlas_v3/bots/engines/straddle_buy_native.py | 64 | PASS |
| TradeEngine core | 4433-5347 | atlas_v3/core/trade_engine.py | 42 | PASS |
| **合計** | | | **505** | **PASS** |

## Evidence

```
$ /opt/homebrew/bin/python3 -m pytest tests/test_orb_native_engine_20260425.py \
    tests/test_calendar_native_engine_20260425.py \
    tests/test_ivcrush_native_engine_20260425.py \
    tests/test_strangle_sell_native_engine_20260425.py \
    tests/test_iron_condor_sell_native_engine_20260425.py \
    tests/test_butterfly_native_engine_20260425.py \
    tests/test_straddle_gamma_native_20260425.py \
    tests/test_straddle_buy_native_engine_20260425.py \
    tests/test_atlas_trade_engine_native_20260425.py -q --tb=line

505 passed, 101 warnings in 5.99s
```

(警告 101 件は futu pb2 由来の DeprecationWarning・本実装無関係)

## 設計共通項

- TacticBase ABC 継承・duck-typing Protocol で futu SDK 直接依存ゼロ
- spy_bot.py 書換ゼロ (schg lock 維持・参照のみ)
- chainguard_wrapper + symbol_aware_price 経由で SPX=300 / SPY-hardcode bug 防止
- common_v3/risk/pre_trade_check (4-Layer Gate) 統合
- common_v3/risk/kill_switch (Kill Switch 全経路) 統合
- earnings_proximity 5 営業日前 block (premium 売り戦術)
- PDTGuard で live 移行時 FINRA PDT 物理強制
- idempotency_key で重複発注防止

## TradeEngine core 統合層

- moomoo_breaker (CircuitBreaker fail-closed)
- BulkheadPool "moomoo" (upstream 隔離・thread pool 分離)
- common_v3/risk/pre_trade_check (4-Layer Gate)
- common_v3/risk/kill_switch (Kill Switch)
- week/monthly option suffix 除去 logic で whitelist 照合

## 関連 commit

- d4929a7 Phase 2 Engine 移植 9/9 実装
- 11ff046 spy_bot ChainGuard center 動的取得 (BUG-20260425-003/004 根治)
- 4769990 spy_bot calc_ivr typo 緊急修正 (BUG-20260425-001/002 根治)
- d1a837c symbol_selector 7 戦術 weight + alias 辞書

## fork session note

このタスクは auto-continue fork session で実行 (2026-04-25 後半)。
着手 1 件・完遂・新規 md 追加のみ・既存非破壊・side-effect なし。

このターンで終了します。
