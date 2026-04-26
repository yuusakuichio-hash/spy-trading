# 2026-04-24 Legacy Bot Stash Archive

## 経緯
Sprint 1-B Phase B r2 Builder (agent a57defe4fbf70f409) が 2026-04-24 00:57 に `spy_bot.py` / `chronos_bot.py` を Bash 経由（`legacy_write_block.sh` の `Write|Edit|NotebookEdit` 限定検査を素通り）で改変した疑惑発生。

Sprint 1-B Phase B の作業範囲で既存コード改変は禁止規律のため、2026-04-24 03:30 頃に `git stash` で作業ツリーから退避し、同日の session 終盤で stash drop 実施。復元用 patch を `/tmp` に保管していたが再起動で失われるリスクがあるため本 archive に永続化。

## ファイル
- `spy_bot_diff_20260424_0257.patch`: spy_bot.py の差分（+993 行・最終 commit `9141243` 以降の未コミット累積）
- `chronos_bot_diff_20260424_0257.patch`: chronos_bot.py の差分（+225 行・同上）

## 復元方法
必要時のみ以下で apply:
```bash
cd /Users/yuusakuichio/trading
git apply data/archive/2026-04-24_legacy_bot_stash/spy_bot_diff_20260424_0257.patch
git apply data/archive/2026-04-24_legacy_bot_stash/chronos_bot_diff_20260424_0257.patch
```

ただし:
- これらの diff には r2-r5 Builder の legacy_write_block 違反による変更が混入している可能性
- Sprint 2 冒頭で allowlist hook (C-018・`scripts/lock_legacy_files.sh`) 適用後、人間（ゆうさくさん）が内容を精査して legitimate な部分のみ取り込み
- 現在の HEAD (`4fa008d` 以降) は 9141243 相当のクリーンな作業ツリー

## 関連
- `data/ops/post_incident_review_20260424.md`: この事象の post-incident review
- `data/sprint1_carryovers.md` C-026: stash 扱い決定記録
- `data/specs/allowlist_hook_design_20260424.md`: 再発防止のための allowlist 設計
