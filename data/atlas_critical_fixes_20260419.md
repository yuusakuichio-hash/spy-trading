# Atlas CRITICAL 7件修正 2026-04-19

## 修正概要

### C1: DeltaHedge UNWIND 実close発注
- ファイル: spy_bot.py (旧 6391-6401, 現在+約50行)
- 変更: フラグだけ落とすのではなく _delta_hedge_codes 内のコードに SELL 発注追加
- 発注成功確認後にフラグ変更。失敗時 priority=2 でPushover + フラグ維持

### C2: IC CALL失敗時のPUT脚巻き戻し
- ファイル: spy_bot.py (IronCondorSellEngine.execute_entry)
- 変更: CALL CS失敗時にPUT CS を即巻き戻し (PUT SELL→BUY, PUT BUY→SELL)
- 巻き戻し失敗時 priority=2 でPushover、成功時も priority=1 で完了報告

### C3: hedge/ORB/Cal/Butterfly idempotency key付与
- ファイル: spy_bot.py (ORBEngine.execute_entry, CalendarEngine.execute_entry, ButterflyEngine._execute_entry, DeltaHedge発注部)
- 変更: signal_id パラメータ追加、None の場合は `{戦術}_{ticker}_{YYYYMMDDHHMMSS}_{uuid8}` 形式で自動生成
- _place_single_leg に signal_id を渡して冪等性チェックを有効化

### C4: ORBEngine early_close対応
- ファイル: spy_bot.py (ORBEngine.check_exit)
- 変更: is_early_close_today() 判定を追加。早期クローズ日は time_stop を EARLY_CLOSE_EXIT_H:M に前倒し

### C5: trade_ctx死時のfail-safe
- ファイル: spy_bot.py (force_close 3回失敗処理)
- 変更: priority=2 Pushover 送信 + data/logs/emergency_manual_close_required.log 記録
- 旧: priority=0 で「0DTE失効の可能性」と軽い通知のみ

### C6: Bearer token rotation + gitignore
- ファイル: .gitignore, data/token_rotation_20260419.md
- 変更: .gitignore に .claude/skills/ を追加（認証情報のgit追跡を防止）
- token rotation 手順書を data/token_rotation_20260419.md に作成

### C7: atlas_agent Level2 Two-Man Rule
- ファイル: atlas_agent.py (dispatch 関数 Level2 処理), atlas_rules.yaml
- 変更: Level2 AUTOFIX 実行前に Pushover 承認要求 (5分タイムアウト)
- atlas_rules.yaml: min_level: 3→2, level2_approval_required: true, emergency_bypass_conditions 追加

## テスト結果
- 新規テスト: 39件 (tests/test_atlas_critical_fixes_20260419.py)
- 全テスト: 837件全合格 (旧798件 + 新39件)
- Atlas FW採点: 80/80 (100%) EXCELLENT

## Rollback手順
```bash
# バックアップから復元
cp /tmp/atlas_critical_backup_20260419.tar.gz /tmp/atlas_restore_tmp/
cd /tmp/atlas_restore_tmp/
tar -xzf atlas_critical_backup_20260419.tar.gz
# spy_bot.py と atlas_agent.py を差し替え
cp spy_bot.py /Users/yuusakuichio/trading/spy_bot.py
cp atlas_agent.py /Users/yuusakuichio/trading/atlas_agent.py

# atlas_rules.yaml の two_man_rule を元に戻す
# min_level: 2 → 3
# level2_approval_required: true → 削除
# emergency_bypass_conditions → 削除

# .gitignore から .claude/skills/ 行を削除

# テスト確認
python3 -m pytest tests/ -q
```

## ブランチ
- 修正ブランチ: audit_high_fix_20260418
- 変更ファイル: spy_bot.py, atlas_agent.py, atlas_rules.yaml, .gitignore
- 新規ファイル: tests/test_atlas_critical_fixes_20260419.py, data/token_rotation_20260419.md, data/research_collective2.md, data/atlas_critical_fixes_20260419.md
