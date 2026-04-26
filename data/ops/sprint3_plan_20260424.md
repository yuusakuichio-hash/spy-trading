# Sprint 3 先行計画（2026-04-24 策定）

**Sprint 3 位置付け**: Sprint 2 paper 開始後の 30 日運用並行で進める拡張開発。live 移行の前段。

---

## Sprint 3 目標

| 目標 | 根拠 |
|---|---|
| 1. **Paper 30 日成績の戦略判定**（2026-05-27 頃） | ペーパー勝率 / PF / 月利で 2026-10 月 60 万 gate 確度算出 |
| 2. **発注 API 実装**（C-017 read-only → write enable） | Sprint 3 スコープ拡張・premortem F05 べき等性対応 |
| 3. **VPS 移行検討**（Mac mini → 24h VPS）| live 移行時の常時稼働要件 |
| 4. **Sprint 2 carryover 残解消**（C-025 assert 他） | DoD 厳格化 |

---

## Day-by-Day 計画（目安 5-7 日）

### Week 1: Paper 成績評価 + 発注 API 準備

| Day | 内容 |
|---|---|
| 1 | Paper 30 日成績集計 / 月利 / DD / sharpe / 約定率 |
| 2 | 戦略判定（gate 通過 or FAIL）・撤退 or 継続 判断 |
| 3 | 発注 API 設計 (place_order / cancel_order / position 管理) |
| 4 | 発注 API 実装 (mock test) |
| 5 | 発注 API 実接続 smoke test |

### Week 2: VPS 移行 or live 移行準備

| Day | 内容 |
|---|---|
| 6 | VPS 検討（Mac mini 24h 稼働 vs VPS 新規 vs Conoha 再利用）|
| 7 | live 移行前 final checklist / Redteam r10 相当 |

---

## Sprint 2 carryover 継続項目

| ID | 内容 | Sprint 3 優先度 |
|---|---|---|
| C-025 | RiskEngine.check_var / _check_kill_switch 等に assert 追加 | HIGH |
| B-1 | RET_OK 固定値前提 assert（futu doc 照合）| MEDIUM |
| T-3 | atlas_v3/common_v3 自体の lock 対象追加 | MEDIUM |
| S-4 | moomoo rate limit 本格対策（token bucket）| MEDIUM |

---

## 発注 API 設計方針（C-017 拡張）

### 新設予定メソッド

| method | 機能 |
|---|---|
| `place_order(symbol, qty, order_type, limit_price)` | 発注 |
| `cancel_order(order_id)` | キャンセル |
| `modify_order(order_id, new_qty, new_price)` | 変更 |
| `get_active_orders()` | アクティブ注文一覧 |
| `get_filled_orders(date_range)` | 約定履歴 |

### べき等性（F05 対策）

- `client_order_id` を SHA256(symbol + qty + price + ts_5min_bucket) で生成
- moomoo `set_client_order_id` or 内部 dedup store で重複発注防止

### テスト方針

- mock test: 発注確認 / キャンセル / 変更 / 約定シミュレーション
- 実 paper 口座 smoke test: 1 株単位発注 → 即キャンセル確認
- 攻撃ベクトル実試行: 重複 client_order_id / 無効 symbol / rate limit

---

## VPS 移行検討（ADR-014 Decision 1 撤回条件該当時）

### 選択肢

| 案 | メリット | デメリット | コスト |
|---|---|---|---|
| A. Mac mini 継続 | 現状通り・OpenD 既稼働 | 24h 要求時 sleep / 停電リスク | 0 円 |
| B. Conoha 再利用 | 既存 VPS（auth 抵触注意）| moomoo OpenD headless 動作調査要 | 既存月額 |
| C. AWS EC2 等新規 | 拡張性高 | 設定複雑 / 費用大 | 数千円/月 |
| D. Mac mini + Conoha 分散 | 冗長化 | 構成複雑 | 既存 |

**推奨**: Paper 30 日成績で live 移行見合いなら **案 A 継続**。live 移行なら **案 D 冗長化**。

---

## Redteam r10 攻撃観点（Sprint 3 完了前）

1. 発注 API 重複発注 bypass（べき等性穴）
2. rate limit 超過での発注遅延 / 取りこぼし
3. modify_order 競合（同時に cancel + modify）
4. 約定通知 (position update) の遅延で monitor 誤判断
5. VPS 移行時の network latency / RTT 増加での p99 悪化
6. live 移行時の TrdEnv.REAL 誤設定（paper → live の切替事故）

---

## Sprint 3 前提条件（満たせなければ延期）

- [ ] Paper 30 日完走・成績集計済
- [ ] 2026-05-27 時点で gate 通過判定
- [ ] moomoo Paper 口座で発注 smoke test 実行可能
- [ ] ゆうさくさん live 移行決裁（税務影響含む）

---

## 関連ファイル

- `data/sprint1_carryovers.md` Sprint 2 carryover 全件
- `data/decisions/ADR-014-moomoo-provider-scope-20260424.md` Sprint 2 スコープ
- `data/ops/sprint2_dayplan_20260424.md` Sprint 2 計画
- `memory/project_300m_roadmap_20260421_v6.md` 全体ロードマップ
