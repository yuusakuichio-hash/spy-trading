# atlas_v3 — Atlas 新コード（全書き直し v3）

**作成**: 2026-04-22 / ゆうさくさん全書き直し方針確定日

## このディレクトリの目的

既存 `spy_bot.py`（18,858 行・MI 0.00・silent except 31.4%）の負債を排除した最小構成の新実装。
目標関数: バグなし絶対 + autonomous 95%+5% + 2027/04 月 300 万。

## 想定構造（minimum_spec_20260422.md より）

```
atlas_v3/
├── core/
│   ├── engine.py          # メインループ（CC ≤ 20）
│   ├── market_time.py     # 市場時間判定
│   └── env_observer.py    # VIX/IVR/VRP/GEX/term_ratio/bias 観測
├── strategies/
│   ├── cs_sell.py         # Credit Spread
│   ├── ic_sell.py         # Iron Condor
│   ├── butterfly.py
│   ├── calendar.py
│   ├── strangle_sell.py
│   ├── straddle_buy.py
│   ├── orb_buy.py
│   └── delta_hedge.py
├── risk/
│   ├── kill_switch.py     # 冪等性付き新設計
│   ├── pdt_guard.py
│   └── sizing_kelly.py
├── broker/
│   └── moomoo_client.py   # 薄いラッパー
└── tests/
    └── ...                # TDD 先行・cov 80%+
```

## 禁止事項（新コード側で守るルール）

- `except Exception: pass` / `except: pass` 禁止（linter で物理 block）
- 関数 LoC > 50 禁止・class LoC > 300 禁止
- 循環複雑度 > 20 禁止
- global mutable state 禁止
- `from X import *` 禁止
- assert 0 件禁止（境界条件検査必須）
- Mock だけのテスト禁止（integration 含む）
- 型注釈必須（mypy --strict 通過）
- 既存 common/ 等への依存禁止（common_v3/ のみ）

## 実装順序

1. Phase 0: scaffold（本 README）← ここ
2. Phase 2: common_v3/ 先行 → atlas_v3/core/ → strategies/ → risk/ → broker/ → tests/
3. Phase 3: Paper 稼働・30 日検証

## 関連

- `data/research/minimum_spec_20260422.md`
- `data/research/codebase_metrics_20260422.md`
- `data/specs/v2/atlas_spec_20260422.md`（知識抽出源・仕様そのものは v3 で再起稿）
- `memory/project_session_20260422_major_redesign.md`
- `memory/feedback_bug_zero_absolute_20260422.md`
