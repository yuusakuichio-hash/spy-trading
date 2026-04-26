# common_v3 — Atlas/Chronos 共通コア（全書き直し v3）

**作成**: 2026-04-22 / ゆうさくさん全書き直し方針確定日

## 目的

既存 `common/`（15,427 行・`kill_switch.py` 冪等性欠陥・`pushover_client.py` 46KB 肥大）の負債を排除した最小共通層。
**Atlas/Chronos の両方から interface 経由で参照される**。全書き直しの土台・Phase 2 で先行実装。

## 想定構造（1,330 行推定）

```
common_v3/
├── auth/
│   └── budget.py              # 既存 auth_budget.py を最小版に
├── notify/
│   ├── eicas.py               # Warning/Caution/Advisory 3 層分離
│   └── andon.py               # 3 経路 OR (Pushover + ntfy + KILL_SWITCH)
├── llm/
│   └── budget.py              # llm_budget.py（実装済）を参照
├── order/
│   ├── models.py              # OrderRequest / Leg / OrderResult dataclass
│   ├── idempotency.py         # 二重発注防止
│   └── reconcile.py           # desired vs actual state
├── position/
│   └── models.py              # Position / PortfolioSnapshot
├── risk/
│   └── kill_switch.py         # 冪等性付き新設計
├── market/
│   ├── data.py                # MarketDataClient 統一窓口
│   └── time.py                # JST/ET 変換・市場時間判定
├── observability/
│   ├── deadman.py             # healthchecks.io 経由
│   ├── health_check.py        # startup/liveness/readiness
│   └── synthetic_probe.py
├── spec_drift/
│   ├── watcher.py             # broker/prop 仕様変更検知
│   └── registry.yaml
└── tests/
```

## 既実装（2026-04-22）

- `common/llm_budget.py` → `common_v3/llm/budget.py` への昇格予定・暫定的に既存パスで動作中

## 凍結 API（実装時に変更禁止・Navigator が監視）

Atlas/Chronos の両 bot が参照する interface はここで凍結する。
変更する場合は Flow 3（重大判断）案件として別途承認必要。

## 関連

- `data/research/sre_unattended_observability_20260422.md`
- `data/research/self_healing_bot_20260422.md`
- `data/research/broker_spec_drift_adaptation_20260422.md`
- `data/specs/v2/common_spec_20260422.md`（知識抽出源）
