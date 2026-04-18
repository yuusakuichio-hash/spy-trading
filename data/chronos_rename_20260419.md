# Chronos 命名統一 A案 — 実施記録 (2026-04-19)

## 移動したファイル一覧 (before → after)

| 旧ファイル | 新ファイル | 内容 |
|---|---|---|
| mffu_bot.py (2333行) | chronos_bot.py (2411行) | メインBot実装本体 |
| mffu_strategy_selector.py (920行) | chronos_strategy_selector.py (920行) | 戦術選択エンジン |
| mffu_emergency_stop.py (396行) | chronos_emergency_stop.py (396行) | 緊急停止スクリプト |
| mffu_rule_simulator.py (660行) | chronos_rule_simulator.py (660行) | MFFUルールシミュレーター |
| ~/LaunchAgents/com.mffubot.plist | ~/LaunchAgents/com.chronos.bot.plist | LaunchAgent設定 |

### 退避先 (rollback用)
- ファイル本体: `/Users/yuusakuichio/trading/data/deprecated/`
- バックアップ: `/Users/yuusakuichio/trading/.bak_chronos_rename_20260419_053049/`

## 置換した参照箇所の統計

| 種別 | 件数 |
|---|---|
| import文 (from mffu_* → from chronos_*) | 約12箇所 |
| class MFFUBot → class ChronosBot | 1箇所 |
| LaunchAgent Label/ProgramArguments | 2箇所 |
| Pushover タグ [MFFU] → [Chronos] | 4箇所 |
| patch("mffu_bot.*") → patch("chronos_bot.*") | 3箇所 |
| test_mffu_bot.py / test_apex_bot.py import更新 | 8箇所 |

## 保持した命名（意図的）

- `MFFURuleGuard`, `MFFUAccountRules`, `MFFUScalingTier`, `MFFUSimResult` — クラス名 (プロップファーム固有ルールの実装)
- `select_mffu_strategies()` — futures_session_strategy.py の関数名 (スコープ外)
- `MFFU_PLANS`, `mffu_compliance` — 辞書・YAML キー名 (MFFU=プロップファーム名として保持)
- `mffu_allowed` — symbol_meta フィールド名 (MFFU口座での取引可否フラグ)
- `.claude/hooks/service_recommend_guard.sh` — `"mffu"` キー (外部サービス名辞書・変更不要)

## テスト結果

- `test_chronos_e2e.py`: **27/27 PASS**
- `test_mffu_bot.py`: **174/174 PASS**
- `test_mffu_selector_integration.py`: 7 FAIL (リネーム前から既存FAIL・今回無関係)

## rollback手順

```bash
# 1. 新ファイルを削除
rm /Users/yuusakuichio/trading/chronos_bot.py \
   /Users/yuusakuichio/trading/chronos_strategy_selector.py \
   /Users/yuusakuichio/trading/chronos_emergency_stop.py \
   /Users/yuusakuichio/trading/chronos_rule_simulator.py

# 2. 旧ファイルを復元
cp /Users/yuusakuichio/trading/.bak_chronos_rename_20260419_053049/mffu_bot.py \
   /Users/yuusakuichio/trading/
cp /Users/yuusakuichio/trading/.bak_chronos_rename_20260419_053049/mffu_strategy_selector.py \
   /Users/yuusakuichio/trading/
cp /Users/yuusakuichio/trading/.bak_chronos_rename_20260419_053049/mffu_emergency_stop.py \
   /Users/yuusakuichio/trading/
cp /Users/yuusakuichio/trading/.bak_chronos_rename_20260419_053049/mffu_rule_simulator.py \
   /Users/yuusakuichio/trading/

# 3. LaunchAgent 復元
cp /Users/yuusakuichio/trading/.bak_chronos_rename_20260419_053049/com.mffubot.plist \
   ~/Library/LaunchAgents/
rm ~/Library/LaunchAgents/com.chronos.bot.plist

# 4. テストファイル復元
# test_mffu_bot.py / test_apex_bot.py は import が chronos_* になっているため
# バックアップから復元:
cp /Users/yuusakuichio/trading/.bak_chronos_rename_20260419_053049/... \
   /Users/yuusakuichio/trading/
# (テストバックアップは .bak_chronos_rename_20260419_053049/hooks_bak に hooks のみ含まれる)
# テストファイルは git checkout で復元可:
# git checkout HEAD -- test_mffu_bot.py test_apex_bot.py test_mffu_selector_integration.py

# 5. 確認
python3 -c "from mffu_bot import MFFUBot; print('rollback OK')"
```

## 注意点

- LaunchAgent `com.chronos.bot.plist` はまだ `launchctl load` していない（disabled状態）
- `com.soralab.chronos_bot.plist` は別途既存の plist（旧スタブ用）で22:25 JST起動設定
  - 本番移行時は `com.chronos.bot.plist`（22:00 JST・account-size/product引数あり）を使用する
- `data/deprecated/mffu_*.py` は rollback 用に保持。削除しない
