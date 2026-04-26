# Atlas Cycle4 修正 rollback手順 (2026-04-19)

## バックアップファイル

| ファイル | バックアップ |
|---|---|
| spy_bot.py | spy_bot.py.bak_cycle4_20260419_210532 |
| atlas_agent.py | atlas_agent.py.bak_cycle4_20260419_210532 |
| scripts/atlas_evaluation.py | scripts/atlas_evaluation.py.bak_cycle4_20260419_210532 |

## rollbackコマンド

```bash
cp spy_bot.py.bak_cycle4_20260419_210532 spy_bot.py
cp atlas_agent.py.bak_cycle4_20260419_210532 atlas_agent.py
cp scripts/atlas_evaluation.py.bak_cycle4_20260419_210532 scripts/atlas_evaluation.py
```

## 実施内容サマリー

### CRITICAL 5件

| BUG | 修正 | ファイル |
|---|---|---|
| BUG-1 | place_credit_spread に signal_id 引数追加・決定的ID自動生成 | spy_bot.py L4729 |
| BUG-2 | _confirm_fills FILLED_PART bypass削除（fills=None維持） | spy_bot.py L4703 |
| BUG-3 | Level3 emergency_bypass実装（kill_switch_activated等で承認スキップ） | atlas_agent.py L650 |
| BUG-4 | atlas_evaluation.py 再構築（AST解析/self-test/コメント除外） | scripts/atlas_evaluation.py |
| BUG-5 | is_early_close_today() を全8戦術のcheck_exit/execute_entryに追加 | spy_bot.py 複数箇所 |

### HIGH 7件

| BUG | 修正 | ファイル |
|---|---|---|
| BUG-6 | 全エンジン execute_entry に signal_id 生成・伝搬 | spy_bot.py 複数箇所 |
| BUG-7 | idempotency fail-open → fail-safe（return None, "idempotency_check_failed"） | spy_bot.py L4484 |
| BUG-8 | IC/ORB/STRADDLE_BUY 決済を _place_single_leg 経由に変更 | spy_bot.py L12038/L8311/L10125 |
| token | Pushover token hardcode 削除（atlas_evaluation.py） | scripts/atlas_evaluation.py L44 |
| BUG-9 | Level2 dead code にコメント明記 | atlas_agent.py L615 |

## 採点結果

- サイクル3（修正前）: 実動作 30/80
- サイクル4（修正後）: **80/80 EXCELLENT [selftest=PASS]**
- self-test: dummy codebase score=7/80 (<=10 確認)

## テスト結果

- 新規テスト: 34件 (tests/test_atlas_cycle4_fixes_20260419.py) — 全合格
- 既存テスト: 1048件通過 / 3件スキップ / regression 0件
- 除外（元々失敗）: test_chronos_state_schema_contract_20260419.py 13件

## 主要修正ファイル

- /Users/yuusakuichio/trading/spy_bot.py
- /Users/yuusakuichio/trading/atlas_agent.py
- /Users/yuusakuichio/trading/scripts/atlas_evaluation.py
- /Users/yuusakuichio/trading/tests/test_atlas_cycle4_fixes_20260419.py (新規)
- /Users/yuusakuichio/trading/data/eval/atlas_trader_eval_cycle4_20260419.md (採点結果)
