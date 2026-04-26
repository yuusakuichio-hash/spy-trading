# Chronos プロップファーム安全装置 実装報告 2026-04-20

**担当**: builder (Sonnet 4.6)
**実装日**: 2026-04-20
**仕様書**: data/builder_instructions/chronos_prop_safety_20260420.md

---

## 完了状況

| Phase | タスク | ステータス | テスト |
|---|---|---|---|
| A-1 | common/prop_firm_rules.yaml 新設 | 完了 | 44 件合格 |
| A-2 | common/prop_firm_rules.py 新設（チェック関数 10 個） | 完了 | 73 件合格 |
| A-3 | chronos_pre_trade_check.py Layer PF-1 統合 | 完了 | 既存テスト継続合格 |
| A-4 | Rapid Intraday Trailing 対応（check_mll_breach 分岐済） | 完了 | S01/S15 で検証 |
| B-5 | common/prop_firm_cross_account.py 新設 | 完了 | 19 件合格 |
| B-6 | chronos_intraday_monitor.py 新設 | 完了 | 18 件合格 |
| B-7 | Funded Consistency 40% ガード（Core） | 完了 | YAML 参照済み |
| B-8 | Tradeify 35% Day 1 ガード | 完了 | S03 で検証 |
| C-9 | FirmScopedKillSwitch（common/kill_switch.py 拡張） | 完了 | kill_switch 既存テスト継続合格 |
| C-10 | check_payout_eligibility_with_freeze() | 完了 | S19 で検証 |
| C-11 | Redteam 検証テストスイート 20 シナリオ | 完了 | **20/20 合格** |

---

## テスト結果サマリー

| テストファイル | 件数 | 結果 |
|---|---|---|
| tests/test_prop_firm_rules_yaml.py | 44 | 全合格 |
| tests/test_prop_firm_rules.py | 73 | 全合格 |
| tests/test_prop_firm_cross_account.py | 19 | 全合格 |
| tests/test_chronos_intraday_monitor.py | 18 | 全合格 |
| tests/test_prop_firm_redteam.py | 21 (20+メタ) | **全合格** |
| **Phase A/B/C 合計** | **175** | **全合格** |

**全体回帰**: 1874 passed / 32 failed（既存失敗のみ・新規回帰ゼロ）

---

## 新設・変更ファイル

| ファイル | 種別 | 説明 |
|---|---|---|
| common/prop_firm_rules.yaml | 新設 | 全 firm/プラン YAML 単一管理。meta.rapid_enabled=false |
| common/prop_firm_rules.py | 新設 | Layer PF-1 チェック関数群 + 統合エントリポイント |
| common/prop_firm_cross_account.py | 新設 | Layer PF-2 CrossAccountGuard（3 秒遅延 + 相関検出） |
| common/kill_switch.py | 拡張 | FirmScopedKillSwitch クラス追加（後方互換維持） |
| chronos_intraday_monitor.py | 新設 | Layer PF-3 ChronosIntradayMonitor（10 秒ループ） |
| chronos_pre_trade_check.py | 拡張 | FuturesOrderContext に prop フィールド追加 + PF-1 統合 |

---

## 防護アーキテクチャ概要

```
[発注リクエスト]
   ↓
Layer PF-1: check_prop_firm_compliance()（同期・全 trade 必須）
   ├─ MLL / Safety Buffer（Intraday/EOD/Static 別判定）
   ├─ Daily Loss Limit（Builder $1K soft pause / Tradeify $3K）
   ├─ Consistency（Core 40% / Tradeify 35% Day 1 / Eval 50%）
   ├─ 枚数上限（Flex 残高連動テーブル対応）
   ├─ HFT 180 件/日 物理上限
   ├─ Microscalping 10 秒以下 40% 上限
   ├─ ヘッジ禁止（MES/ES, MNQ/NQ 等相関ペア）
   ├─ T1 News 2 分ブラックアウト
   ├─ DCA 禁止（Apex PA 専用）
   └─ Inactivity 失効（Flex 7 日）
   ↓（allow=True）
Layer PF-2: CrossAccountGuard.check_before_order()
   ├─ 同 firm 3 秒最低遅延
   └─ 他口座同銘柄同方向 active 検出
   ↓
Layer PF-3: ChronosIntradayMonitor（バックグラウンド 10 秒ループ）
   ├─ Rapid Intraday Trailing 80% 予兆 → 強制全決済
   ├─ Flex Survival Mode（初回 Payout 後 MLL=$100 強制）
   ├─ Flex inactivity 6 日アラート
   └─ Apex PA DCA 注文キャンセル
   ↓
Layer PF-4: FirmScopedKillSwitch + check_payout_eligibility_with_freeze()
   ├─ firm 単位 Kill Switch（1 firm 違反で他継続）
   └─ Consistency 予兆 90% → 追加エントリー freeze
```

---

## 重要な実装判断

1. **Rapid の起動フラグ**: `meta.rapid_enabled=false` を YAML に設定。Phase A 完了を確認後に `true` に変更するまで Rapid は全発注がブロックされる。

2. **YAML 再確認で Rapid drawdown_type を修正**: 公式仕様再確認の結果、rapid_50k の Eval フェーズは `eod`、Sim Funded のみ `intraday_trailing` が正しい。旧設定 `"intraday_trailing_4pct"` は Eval への誤適用だったため修正済み。

3. **Flex mll_after_first_payout=100**: 初回 Payout 後に MLL が $2,000 → $100 に激変する罠を明示的に YAML に記録。ChronosIntradayMonitor の Survival Mode と連携して mll を実行時にも $100 に書き換える。

4. **Builder DLL soft pause**: MFFU 他プランにはない固有ルール（$1,000 到達で新規停止・既存ポジ継続）を DLL チェックで統一的に処理。

---

## 既知の制約・次ステップ

1. **chronos_bot.py への統合**: FuturesOrderContext の prop フィールド（firm/plan/phase/prop_account_state）を chronos_bot.py で設定する必要がある。
2. **CrossAccountGuard の chronos_bot.py 組込み**: `get_global_guard()` を chronos_bot.py の place_order 直前で呼ぶ。
3. **ChronosIntradayMonitor の起動**: `asyncio.create_task(monitor.monitor_loop())` を chronos_bot.py の main に追加。
4. **独立第三者検証**: 本報告書完了後、別 session で redteam に「見落とし・盲点」検証依頼を実施すること（規律）。

---

## Redteam 20 シナリオ一覧

| No | シナリオ | 判定 |
|---|---|---|
| S01 | Rapid Intraday peak $2,100 → $100 戻り | BLOCKED |
| S02 | Core Funded Consistency 42% | BLOCKED |
| S03 | Tradeify Day 1 66% 集中 | BLOCKED |
| S04 | Apex PA DCA 損失ポジ追加 | BLOCKED |
| S05 | Flex 初回 Payout 後 $101 損失 | BLOCKED |
| S06 | HFT 181 件目 | BLOCKED |
| S07 | Microscalping 50% short holds | BLOCKED |
| S08 | MES long + ES short ヘッジ | BLOCKED |
| S09 | FOMC 1 分前発注 | BLOCKED |
| S10 | Cross-Account 2 秒遅延のみ | BLOCKED |
| S11 | Builder DLL $1,000 | BLOCKED |
| S12 | Flex 7 日 inactivity | BLOCKED |
| S13 | Apex EOD Trailing MLL 超過 | BLOCKED |
| S14 | Pro 6 枚発注（上限 5 枚） | BLOCKED |
| S15 | Rapid Intraday 80% 予兆 | BLOCKED |
| S16 | Core Eval Consistency 90% | BLOCKED |
| S17 | MNQ long + NQ short ヘッジ | BLOCKED |
| S18 | Tradeify DLL $3,000 | BLOCKED |
| S19 | Payout Freeze 90% 予兆 | BLOCKED |
| S20 | Rapid rapid_enabled=false フラグ | BLOCKED |
| **合計** | | **20/20** |
