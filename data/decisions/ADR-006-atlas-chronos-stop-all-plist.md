# ADR-006: 案 A 採用 — Atlas/Chronos 全 launchd plist 退避

**起票日**: 2026-04-23 06:36 JST
**起票者**: ソラ提案 → ゆうさくさん承認（「Aでいいよ」）
**ステータス**: accepted（完了 / 復元可能）
**関連**: ゆうさくさん指示「Atlas と Chronos は止めて・通知も刷新プロジェクトのものだけに」

---

## コンテキスト

- ゆうさく指示: 「前に作ったAtlasとChronosは止めて、それに伴うプッシュ通知も止めて、刷新プロジェクトに集中したい」
- 現状調査:
  - launchd 登録 70+（atlas/chronos/spybot/sora/soralab/spxbot prefix）
  - 動作中プロセス 8 件（chronos_watchdog/agent, atlas_watchdog, spy_bot watchdog, sora_heartbeat, market_hours_atlas_monitor, fleet_watcher×2）
- 通知方針: ソラ→ゆうさく作業報告は残す・自動 bot 通知は止める

## 選択肢

| 案 | 内容 | バグ発生率 | 復元性 |
|---|---|---|---|
| **A** | **全 plist を `~/Library/LaunchAgents/disabled_20260423/` へ退避 + launchctl unload + 動作中プロセス kill。復元 = mv で 1 コマンド** | 低 | ◯（移動のみ・削除なし） |
| B | 各 plist を 1 件ずつ判定して選択停止 | 中（70 件判定で漏れ可能性） | ◯ |
| C | atlas/chronos プロセスのみ kill（plist は残す） | 高（一時停止・通知再開） | ◯ |

## 採用案

**採用**: A

**判断者**: ゆうさく承認

**理由**:
- 退避（移動のみ）= 削除なし・1 コマンド復元可能
- 70 件判定の漏れリスク回避
- 「刷新に集中」の意図に最大限合致

## 想定結果（事前）

- 短期: Atlas/Chronos 自動通知ゼロ・動作中プロセス全 kill
- 中期（Phase 2 完了後）: 必要に応じて plist 個別復元 or 新規 atlas_v3/chronos_v3 用 plist 新設

## 実結果（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- launchd unload + plist 退避: 76 件（active 70 + bak/disabled サフィックス 6）
- 動作中プロセス kill: 8 件 → 0 件確認
- 残存 plist: 0 件（OS 標準系のみ・無関係）
- 退避先: `~/Library/LaunchAgents/disabled_20260423/`

## 振り返り（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- 復元コマンドを ADR と Sprint 1 持ち越しに記録済 = 失念防止
- com.spxbot.* prefix を当初 grep し損ねた（atlas/chronos/spybot/sora/soralab で漏れ）→ finance_tracker.py 紐付き plist 1 件を後から追加退避
- 学習: prefix list の網羅性チェックを最初の grep で実施する規律必要

## 関連証跡

- `~/Library/LaunchAgents/disabled_20260423/`（76 件保存）
- 復元コマンド:
  ```bash
  mv ~/Library/LaunchAgents/disabled_20260423/*.plist ~/Library/LaunchAgents/
  for f in ~/Library/LaunchAgents/com.{atlas,chronos,spybot,sora,soralab,spxbot}.*.plist; do launchctl load "$f"; done
  ```
