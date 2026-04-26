# Watchdog 時間帯ゲート実装記録 2026-04-20

## 問題
- chronos_watchdog が chronos.log の「10分無更新=異常」を常時監視していた
- chronos_bot は 22:25 JST 市場オープン時のみ起動する設計
- 結果: 市場外時間帯（JST 07:41頃）に「32時間更新停止」と誤検知
- 自己回復パスが `launchctl kickstart com.soralab.chronos_bot` を叩くが市場時間外は起動しない → 5回全失敗
- attempt=6 になり priority=2「回復不可・人間介入要」が 07:11, 07:21, 07:31 に通知

## 修正内容

### A. 時間帯ゲート実装（chronos_watchdog.py / atlas_watchdog.py 両方）

各監視対象に `watch_windows_jst` を定義:

chronos_watchdog.py:
```python
WATCH_TARGETS = [
    {
        "path": .../chronos.log,
        "watch_windows_jst": [("22:20", "05:10")],  # 市場時間帯のみ監視（JST）
        "service": "com.soralab.chronos_bot",
    },
    {
        "path": .../chronos_agent.log,
        "watch_windows_jst": [("00:00", "23:59")],  # 常時監視
        "service": "com.soralab.chronos_agent",
    },
]
```

atlas_watchdog.py:
```python
WATCH_TARGETS = [
    {
        "path": .../condor.log,
        "watch_windows_jst": [("22:20", "05:10")],  # 市場時間帯のみ監視（JST）
        "service": "com.atlas.agent",
    },
]
```

`_is_in_watch_window()` で日跨ぎ窓（22:20〜05:10）を正しく判定。
`run_health_check()` で窓外なら skip（alert なし・recovery 試行なし）。

### B. recovery_state リセット
- 両 JSON とも attempt=6 → attempt=0, recovered=true にリセット

### C. watchdog 再起動
- `launchctl stop/start com.chronos.watchdog` 実行
- `launchctl stop/start com.atlas.watchdog` 実行

## テスト結果（18/18合格）

既存 15件:
- TestChronosRecovery: 4/4 PASSED
- TestChronosPushoverBackoff: 3/3 PASSED
- TestAtlasRecovery: 4/4 PASSED
- TestAtlasPushoverBackoff: 2/2 PASSED
- TestBackoffStatePersistence: 2/2 PASSED

新規 3件:
- test_e_window_inside_stale_triggers_recovery: PASSED（窓内 JST 23:00 + stale → recovery発動）
- test_f_window_outside_stale_skips_recovery: PASSED（窓外 JST 07:41 + stale → skip）
- test_g_boundary_22_25_jst_is_inside_window: PASSED（22:25 JST は窓内・05:15 JST は窓外）

## 変更ファイル
- `/Users/yuusakuichio/trading/chronos_watchdog.py`
- `/Users/yuusakuichio/trading/atlas_watchdog.py`
- `/Users/yuusakuichio/trading/tests/test_watchdog_recovery.py`
- `/Users/yuusakuichio/trading/data/chronos_watchdog_recovery_state.json`（リセット）
- `/Users/yuusakuichio/trading/data/atlas_watchdog_recovery_state.json`（リセット）

## 今後
- 将来的に `config/watch_targets.yaml` に移す余地を残している（現在は .py にハードコード）
