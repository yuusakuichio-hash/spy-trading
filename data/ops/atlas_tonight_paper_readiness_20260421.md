# Atlas Tonight Paper Readiness 2026-04-21

## 判定: 22:30 fire 保証レベル 95%

## 確認結果

| 項目 | 状態 | 証跡 |
|---|---|---|
| OpenD PID | OK (45300) | ps: moomoo_OpenD 稼働中 |
| com.spybot.paper | OK (22:00 JST fire) | plist: StartCalendarInterval Hour=22 Min=0 |
| com.atlas.smoke_2220 | OK (22:20 JST fire) | plist: Hour=22 Min=20, runs=0 (スケジュール待ち) |
| com.atlas.agent | OK (22:25 JST・現在PID 36822稼働) | launchctl: state=running |
| spy_bot.py | OK | 60780bytes, 2026-04-21 18:10更新 |
| atlas_agent.py | OK | 60780bytes, 2026-04-21 18:10更新 |
| .env FUTU認証 | OK | FUTU_APP_KEY/SECRET設定済み |
| paper mode | OK | com.spybot.paper plist: --paper引数 |
| halt状態 | OK (not active) | atlas_agent.py --halt-status |
| kill_switch.flag | OK (存在しない) | ファイルなし |
| PDT制約 | OK (paper=スキップ) | mode=paper → PDT制約スキップ確認済み |
| Pushover | WARN (静穏時間21:00-4:00) | priority<2は朝まとめ。障害通知は priority=2+キーワードで即時 |
| tradovate auth budget | 無関係 | Atlas/spy_botはmoomoo経由。tradovate=Chronos専用 |

## 課題事項

### ATMSubscribe/ChainGuard 全スキップ (WARN)
- US..SPX のATM IVリアルタイム取得でスキップが継続中
- center=707.9 (SPY価格がSPX subscribeに混入)
- 影響: ATM IV推定が不正確になるが発注自体はブロックしない
- 発注ルートはget_option_chain経由（別ルート）

### Pushover IP ban (WARN・解除済み見込み)
- 2026-04-21 04:27に「IP banned」エラー。06:30までの最終エラー
- 21:16 JST時点のテスト送信: 静穏時間deferred(正常)。banなし確認

### com.spybot.paper 22:00起動・市場オープン前後ウォームアップ
- 22:00起動 → 22:30市場オープンまで30分: symbol_selector / strategy_selector ウォームアップ
- 昨日実績: 03:28:34 [DynamicEntry] Standard entry conditions met → 発注試行確認済み

## fallback計画
- 22:20 smoke_2220 でPID確認 → 停止なら kickstart自動実行
- 手動kick: `launchctl kickstart -k gui/$(id -u)/com.spybot.paper`

## 監視設定
- com.atlas.tonight_monitor: 22:35/23:00/23:30 JST 自動実行
- ログ: /Users/yuusakuichio/trading/data/logs/atlas_tonight_monitor.log
- Pushover: priority=0 → 朝のモーニングダイジェストで確認
