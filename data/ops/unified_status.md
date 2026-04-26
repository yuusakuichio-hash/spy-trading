# Sora Lab Unified Status Dashboard
更新: 2026-04-23 06:34:05 JST

## コンポーネント状態
| 優先 | Tier | コンポーネント | HB状態 | HB経過 | LOG状態 | LOG経過 | PID |
|------|------|--------------|--------|--------|---------|---------|-----|
| 🟡 | Atlas | atlas_agent | stale | 1.5時間 | stale | 1.5時間 | 30192 |
| 🔵 | Atlas | atlas_watchdog | ok | 9秒 | ok | 3分 | 5314 |
| 🔵 | Chronos | chronos_agent | ok | 6秒 | no_log | — | 5321 |
| 🔵 | Chronos | chronos_watchdog | ok | 7秒 | no_log | — | 5326 |
| 🔵 | Infra | heartbeat_monitor | no_hb | — | no_log | — | — |
| 🔵 | Infra | dead_man_switch | no_hb | — | ok | 5分 | — |
| 🔵 | Infra | ground_truth | no_hb | — | no_log | — | — |
| 🔵 | Infra | failure_rescue | no_hb | — | ok | 5分 | — |
| 🔵 | Infra | autonomous_sentinel | no_hb | — | ok | 5分 | — |

## Pushover 状態
- 🔵 正常 | consecutive_429=0 | queue=122件

## 直近 Auto-Remediation (最新5件)
- `2026-04-22T21:29:21` ✅ launchctl_kickstart → **atlas_agent** (plist=com.soralab.market_hours_atlas_monitor)
- `2026-04-22T20:59:12` ✅ launchctl_kickstart → **atlas_agent** (plist=com.soralab.market_hours_atlas_monitor)
- `2026-04-22T20:29:04` ✅ launchctl_kickstart → **atlas_agent** (plist=com.soralab.market_hours_atlas_monitor)
- `2026-04-21T20:11:33` ✅ launchctl_kickstart → **atlas_agent** (plist=com.soralab.market_hours_atlas_monitor)
- `2026-04-21T14:40:55` ✅ launchctl_kickstart → **atlas_agent** (plist=com.soralab.market_hours_atlas_monitor)

## Dead Man's Switch (直近3件)
- `2026-04-22T21:29:06` dead_man_switch
- `2026-04-22T21:29:06` chronos_webhook_queue_reader
- `2026-04-22T21:29:06` chronos_bot

## Failure-to-Rescue 直近3件
- ⏳ `2026-04-21T14:11:44` smoke_atlas_001: 4th recurrence
- ✅ `2026-04-21T14:11:44` smoke_atlas_001: recurring test anomaly re2
- ✅ `2026-04-21T14:11:44` smoke_atlas_001_r2: recurring test anomaly

## Escalation Flag
- 🔴 **PENDING** `2026-04-22T21:29:29`: [SentinelALERT] atlas_agent stale

---
*自動生成 by `scripts/generate_status_dashboard.py`*
*sentinel: `scripts/autonomous_sentinel.py` 30分毎*
*閲覧: `cat ~/trading/data/ops/unified_status.md`*