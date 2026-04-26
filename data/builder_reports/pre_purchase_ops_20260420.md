# Pre-Purchase Ops Report — 2026-04-20

対象: MFFU Flex $107購入直前 MUST-FIX 3件
実施日時: 2026-04-20 20:57 JST
担当: builder (Sonnet 4.6)

---

## P-1: LaunchAgent prefix修正 (HIGH Bug-A)

**問題**: `chronos_emergency_stop.py` の `LAUNCHAGENT_LABELS` が `com.chronos.mffu_*` を参照していた。実際のplistは `com.soralab.mffu_*` なので緊急停止時にunloadが空振りになる致命的バグ。

**修正箇所**: `/Users/yuusakuichio/trading/chronos_emergency_stop.py` 行80-87
- `com.chronos.mffu_flex_A` → `com.soralab.mffu_flex_A`
- `com.chronos.mffu_rapid_B` → `com.soralab.mffu_rapid_B`
- `com.chronos.mffu_pro_C` → `com.soralab.mffu_pro_C`
- `com.chronos.mffu_core_D` → `com.soralab.mffu_core_D`
- `com.chronos.mffu_builder_E` → `com.soralab.mffu_builder_E`
- `com.chronos.fleet_watcher` → `com.soralab.fleet_watcher`

**証跡**: `grep -n "com.chronos.mffu" chronos_emergency_stop.py` → コメント行1件のみ（実コード0件）

**dry-run確認**: 全7ラベルが `com.soralab.mffu_*` で正しく出力されることを確認済み。

---

## P-2: MFFU 4アカ LaunchAgent bootstrap

**実施**: 以下4件を `launchctl bootstrap gui/$(id -u)` で登録
- `com.soralab.mffu_flex_A`
- `com.soralab.mffu_rapid_B`
- `com.soralab.mffu_pro_C`
- `com.soralab.mffu_builder_E`

**証跡** (`launchctl list | grep mffu_`):
```
-	0	com.soralab.mffu_pro_C
-	0	com.soralab.mffu_rapid_B
-	0	com.soralab.mffu_flex_A
-	0	com.soralab.mffu_builder_E
```
4件 loaded。exit code 0 は Tradovate認証未設定による期待動作。

---

## P-3: Pushover fallback監視確立

**問題**: Pushover 429 IP ban中は緊急alertがログファイルにしか書かれず、人間に届かない。

**実装**:
- `scripts/watch_emergency_alerts.sh`: `tail -F emergency_alerts.log` で新規行を監視 → EMERGENCY検知時に `osascript display notification` でmacOS通知発動
- `~/Library/LaunchAgents/com.soralab.emergency_watcher.plist`: RunAtLoad=true / KeepAlive=true で常時起動

**証跡**: テストalert書込後、watcher stdoutで `ALERT DETECTED: TEST ALERT — P-3 watch_emergency_alerts.sh 動作確認テスト` を確認。macOS通知発火。

---

## 結論

**MUST-FIX 3件 全件完了。購入OK状態。**

残課題 (購入後対応):
- `com.soralab.mffu_core_D` plistは未作成（Core廃止のためskip）
- Tradovate認証設定後に `launchctl kickstart` で本運用開始
- fleet_watcher plistは別途作成が必要
