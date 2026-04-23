# orb_1dte_spy.py 転用判定記録（2026-04-23）

## 経緯

Sprint 1-B Phase B において Gemini A2 ペルソナ「0DTE System 戦術」を実装するにあたり、
`atlas_v3/strategies/orb_1dte_spy.py` の転用可否を判定した。

---

## 転用判定: 部分転用（採用）

### 転用対象

| 要素 | 転用先 | 備考 |
|---|---|---|
| `ORBRange` dataclass | `zero_dte_system.py` で import 転用 | ORB 観測 state 共有 |
| ORB ブレイクアウト判定ロジック（L297-L334 相当）| `_orb_breakout_direction()` として再実装 | buffer 計算・direction 分岐 |
| `_observe_orb()` パターン | `ZeroDTESystemTactic._observe_orb()` として転用 | get_orb_range API 互換 |

### 転用しなかった要素

| 要素 | 理由 |
|---|---|
| `ORB1DTEConfig` | 0DTE 専用設定 `ZeroDTEConfig` を新設（DTE=0 固定・daily_stop 等を追加） |
| `ORBEntryDecision` / `ORBExitDecision` | 0DTE 専用の `ZeroDTEEntryDecision` / `ZeroDTEExitDecision` を新設（structure / direction フィールド追加） |
| `ORB1DTESPYTactic` class | 1DTE 専用設計のため 0DTE 戦術には直接流用しない |

---

## orb_1dte_spy.py の今後の扱い（判定）

### 判定: deprecated 候補（即時削除は保留）

理由:
- `ORBRange` を `zero_dte_system.py` が import しているため、即時削除すると ImportError が発生する
- `orb_1dte_spy.py` が担う 1DTE ORB 戦術は BT 結果未確定（Phase 0-B1 で「要再検討」判定済み）
- Phase 2 の 1DTE vs 0DTE BT 比較完了後に削除 or 統合を最終判断する

### 移行パス（Phase 2 完了後）

1. `ORBRange` を `common_v3/market/orb_range.py` に昇格移動
2. `orb_1dte_spy.py` の import を `common_v3.market.orb_range` に更新
3. `zero_dte_system.py` の import を同様に更新
4. `ORB1DTESPYTactic` を `orb_1dte_spy.py` ごと deprecated アーカイブへ移動

---

## 参照元

- `atlas_v3/strategies/zero_dte_system.py`（実装）
- `data/research_v3/bt_results/atlas_0dte_system_20260423.md`（BT 方針）
- `data/specs/v3/atlas_spec_v3_20260422.md` B5 / ADR-013 v2
