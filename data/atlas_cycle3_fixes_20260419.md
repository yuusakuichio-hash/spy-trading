# Atlas cycle3 修正ログ + rollback手順書 (2026-04-19)

## バックアップ
`/tmp/atlas_cycle3_backup_20260419.tar.gz`

## 修正概要

### C6-B1 [CRITICAL・最緊急] token git履歴漏洩
- `data/token_rotation_20260419.md`: 平文トークンを `<REDACTED>` に置換
- `.gitignore`: `data/token_*` / `data/credentials_*` を追加
- `data/token_revoke_procedure_20260419.md`: BFG/git-filter-repo手順書を新設
- **次アクション (ゆうさく要対応)**: VPS側トークンの実際のrevocation + BFG実行

### C7-B1 [CRITICAL] Level2 TMR承認機構不在
- `atlas_rules.yaml`: `level2_approval_required: false` (承認ループ未実装のため無効化)
- `atlas_rules.yaml`: `min_level: 3` (安全側へ戻し・運用継続性確保)
- コードに TODO: 承認受付ループ実装後 min_level=2 に戻す
- Level3承認(Pushover経由)は引き続き動作

### C4-B1 [CRITICAL] EARLY_CLOSE_EXIT 市場クローズ後
- `spy_bot.py:487-488`: `EARLY_CLOSE_EXIT_H=12, EARLY_CLOSE_EXIT_M=50`
  - 旧値: `H=13, M=30` (クローズ後30分) → 修正: `H=12, M=50` (クローズ前10分)

### C2-B1 [CRITICAL] `place_credit_spread` 常に return True
- `spy_bot.py`: fill確認後 sell_fill/buy_fill の両方がNoneでない場合のみ True を返す実装
- 片脚未約定を検知して反転決済を発動・Pushover priority=2 送信

### C3-B1 [CRITICAL] uuid毎回新規でidempotency機能ゼロ
- `spy_bot.py` ORBエンジン: `orb_{ticker}_{direction}_{YYYYMMDDHHMM}` (決定的値)
- `spy_bot.py` Calendarエンジン: `calendar_{sym}_{direction}_{YYYYMMDDHHMM}` (決定的値)
- `spy_bot.py` DeltaHedge: `delta_hedge_{direction}_{ticker}_{YYYYMMDDHHMM}` (決定的値)

### C1-B1/B2/B3 [HIGH] UNWIND動的qty・即除去・指値fallback
- `spy_bot.py`: `_delta_hedge_qty_map` によるqty動的取得
- `spy_bot.py`: UNWIND成功分を `_delta_hedge_codes.remove()` で即除去
- `spy_bot.py`: 指値試行 (`delta_hedge_unwind_limit`) → 成行fallback

### C2-B2 [HIGH] PUT巻き戻し指値試行fallback
- `spy_bot.py` IC_SELL: `_ic_unwind_leg()` ヘルパーで指値→成行fallback実装

### C5-B1 [HIGH] 3回失敗後 `_on_position_closed` 呼出削除
- `spy_bot.py`: 決済3回失敗時に `_on_position_closed` を呼ばない
- 理由: ポジションが存在している可能性がある → 幽霊ポジション防止

### 新設: Atlas採点スクリプト
- `scripts/atlas_evaluation.py`: A1-A16の16項目AST解析採点
- 採点結果: `data/eval/atlas_trader_eval_v2_YYYYMMDD.md`
- 実行: `python3 scripts/atlas_evaluation.py --no-push`

### 新設: cycle3テスト
- `tests/test_atlas_cycle3_fixes_20260419.py`: 38テスト・全PASS

## テスト結果
- `test_atlas_cycle3_fixes_20260419.py`: 38/38 PASS
- `test_atlas_critical_fixes_20260419.py`: 39/39 PASS (C7テスト修正済み)
- 採点: **73/80 (91.2%) EXCELLENT**

## Rollback手順

```bash
# バックアップから戻す
cd /Users/yuusakuichio/trading
tar -xzf /tmp/atlas_cycle3_backup_20260419.tar.gz

# または個別ファイルを git checkout で戻す
git checkout <コミットID> spy_bot.py
git checkout <コミットID> atlas_agent.py
git checkout <コミットID> atlas_rules.yaml
```

## 次サイクル要検討
- A4 pre_trade_check: 全エンジンで check_order() 呼出カバレッジ向上 (現在3/5)
- A10 Idempotency: _idem_store の分単位key確認テスト追加 (現在4/5)
- A14 Phase自動遷移: 口座残高ベースのPhase切替実装 (現在4/5)
- A16 監視: daily_aar.py の launchd 組み込み確認 (現在4/5)
- C7-B1 TODO: GitHub Issue経由の承認受付ループ実装後 min_level=2 に戻す
