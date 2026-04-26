# Atlas v3 / Chronos v3 仕様サマリ（ゆうさくさん承認用）

**作成**: 2026-04-23 13:38 JST
**目的**: 実装着手前の仕様中身をゆうさくさんに確認してもらうため・平易にまとめた版

**仕様書本体**:
- Atlas: `data/specs/v3/atlas_spec_v3_20260422.md`
- Chronos: `data/specs/v3/chronos_spec_v3_20260422.md`
- Common: `data/specs/v3/common_spec_v3_20260422.md`

**検証経緯**:
- Phase 1 C-2 で 4 サイクル策定（R1→R2→R2a→R2b）
- Gemini / Redteam で Claude 起草者のバイアス除去
- ゆうさくさん本人の仕様中身確認は未実施（今回の確認が初）

---

## ★ Atlas v3（SPX/SPY オプション自動売買）

| 項目 | 内容 |
|---|---|
| 対象銘柄 | SPX / SPY オプション |
| 取引時間 | JST 22:20 〜 05:10（EDT・平日のみ）|
| 戦術数 | 10 種類 |
| Sprint 1-B Phase B 実装範囲（★） | **ic_sell / earnings_iv_crush / orb_1dte_spy の 3 種のみ** |
| 残 7 戦術 | Sprint 2 持越し |
| 核構造 | TacticBase ABC 継承必須（dispatch 地獄回避）+ 4 Protocol 分類 |
| 閾値方式 | PercentileSelector で動的算出（VIX × phase の 12 セル・固定値禁止） |
| 連携 | kill_switch / idempotency / moomoo_breaker（CircuitBreaker） |

### 戦術 10 種（BT 結果ベース・v6 楽観月利）

| 戦術 | 月利 | 種類 | Sprint 1-B 実装？ |
|---|---|---|---|
| **ic_sell（symbol_selector 連携）** | **6.23%** | Type A (EnterExit) | ★ 実装 |
| **earnings_iv_crush** | **5.39%** | Type C (StateCarrying) | ★ 実装 |
| portfolio_aggregator | 3.84% | Type B (PortfolioReactive) | Sprint 2 |
| **orb_1dte_spy** | **2.71%** | Type C (StateCarrying) | ★ 実装 |
| strangle_sell | 2.59% | Type A | Sprint 2 |
| ic_sell（単体）| 2.53% | Type A | Sprint 2 |
| orb_1dte_qqq | 2.71% | Type C | Sprint 2 |
| gamma_scalp | 2.7-5.4% | Type D (Hybrid) | Sprint 2 |
| ivr_credit_spread | 0.39% | Type A | Sprint 2 |
| butterfly | 0.36% | Type A | Sprint 2 |

### 4 Protocol 分類

| Type | 名前 | 例 |
|---|---|---|
| A | EnterExit | IC・Strangle・Butterfly（一発注入・目標/損切り）|
| B | PortfolioReactive | portfolio_aggregator（既存建玉を見て調整）|
| C | StateCarrying | earnings / ORB（状態保持・複数 tick 跨ぎ）|
| D | Hybrid | gamma_scalp（全特性含む）|

---

## ★ Chronos v3（ES/MES 先物自動売買）

| 項目 | 内容 |
|---|---|
| 対象銘柄 | ES / MES 先物（CME）|
| 取引時間 | ほぼ 24 時間（月 07:00 〜 土 06:00 JST）/ デイリー休止 06:00-07:00 |
| Prop Firm | **MFFU 4 プラン（Flex / Rapid / Pro / Builder）+ Tradeify Lightning Funded $50K**・計 5 firm 体制（Core は 2026-01-28 Flex 統合で新規購入不可・`data/chronos_tradeify_impl_20260419.md` で実装済）|
| Tradeify 仕様 | $295 一括 / EOD Trailing DD $2,000 / DLL $1,250 / Profit split 90/10（常時）/ 4 minis or 40 micros（併用禁止）|
| 戦術分離 | Tradeify: VWAP Reclaim / Liquidity Sweep ― MFFU: ORB / VIX-MR / Session Break / Gap Fill / Overnight Gap / Pro Rush |
| 発注経路 | TradersPost webhook 経由 |
| Sprint 1-B 実装 | **なし**（o3 レビュー B 案で Sprint 2 持越し確定） |
| 連携 | FirmScopedKillSwitch / prop firm rule runtime guard / MFFUFlexRules |

### Chronos 戦術（Sprint 2 着手）

- 詳細は `data/specs/v3/chronos_spec_v3_20260422.md` 参照
- MFFU 各プランのルールに応じて発注量・損切り変動
- EV 高い時間帯集中（asia_range_fade / economic_event / level_trading 等）

---

## ★ Common v3（共通基盤・Sprint 0.5 + Sprint 1-B で実装済）

| コンポーネント | 用途 | 状態 |
|---|---|---|
| `common_v3/risk/kill_switch.py` | 緊急停止（2-ph commit）| ✅ Sprint 0.5 |
| `common_v3/idempotency/store.py` | 二重発注防止 | ✅ Sprint 0.5 |
| `common_v3/observability/deadman.py` | Bot 死活監視 | ✅ Sprint 0.5 |
| `common_v3/self_healing/circuit_breaker.py` | broker 連敗時停止 | ✅ Sprint 1-B |
| `common_v3/executor/sync_guard.py` | @sync_only runtime | ✅ Sprint 1-B |
| `chronos_v3/prop/mffu_flex.py` | MFFU 準拠 runtime guard | ✅ Sprint 1-B |
| `common_v3/storage/persistence.py` (B15) | SQLite 永続化層 | ⏸ Sprint 2 |

---

## ★ ゆうさくさん確認ポイント

1. **Atlas v3 戦術 3 本の優先順**（ic_sell / earnings_iv_crush / orb_1dte_spy）で合っているか
2. **Chronos v3 Sprint 2 持越し**で良いか（Atlas 先行・ペーパー実走での Atlas 成績確認優先）
3. **4 Protocol 分類**で戦術区別する設計に違和感ないか
4. **PercentileSelector の 12 セル動的閾値**（VIX × phase）の方向性 OK か
5. 既存 `spy_bot.py` は **参照のみで書換禁止**の継続で良いか（v3 は別実装）
