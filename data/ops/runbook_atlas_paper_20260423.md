# Atlas Paper 運用 Runbook

**作成日**: 2026-04-23  
**担当**: builder (Sora Lab)  
**対象**: Atlas v3 Paper モード (moomoo TrdEnv.SIMULATE)  
**承認**: ゆうさくさん最終確認必須（本番移行前）

---

## 1. 起動手順

### 1.1 事前チェック（起動前に必ず実行）

```bash
# 0. コンプライアンス事前チェック（NEW-H-1 必須・判断 2: --mode paper で WARN のみ）
#    PENDING_OWNER_APPROVAL_PAPER は WARN のみ（paper 起動継続）
#    PENDING_OWNER_APPROVAL_LIVE は CRITICAL（起動ブロック）
python3 scripts/preflight_compliance_check.py --all --mode paper || exit 1

# 1. Kill Switch 状態確認（ARMED なら発注できない）
python3 -c "from common_v3.risk.kill_switch import is_active; print('KS:', is_active())"

# 2. 設定ファイル読み込みテスト
python3 -c "from atlas_v3.ops.risk_config_loader import load_paper_risk_config; c = load_paper_risk_config(); print('config OK:', c)"

# 3. vault 接続確認（MOOMOO_APP_ID が .env.d/moomoo_paper.env に設定されているか）
python3 -c "from atlas_v3.ops.vault import load_from_env; c = load_from_env(); print('vault OK:', c)"

# 4. 監視 daemon ヘルスチェック（bootstrap_paper_monitor 経由で起動・MonitorDaemon() 直接呼出禁止）
python3 -c "
from atlas_v3.ops.monitor import MonitorDaemon, MonitorConfig
d = MonitorDaemon(MonitorConfig(daily_loss_usd=-400.0, pushover_enabled=False, kill_switch_on_emergency=False), allow_default_config=True)
checks = d.check_once(pnl_day_usd=0.0, drawdown_pct=0.0, latency_ms=0.0)
print([c.check_name + ':' + c.level.value for c in checks])
"
```

### 1.2 OpenD 起動（moomoo Paper モード）

```bash
# macOS launchctl 経由（推奨）
# OpenD を SIMULATE モードで起動していること
# 起動確認: ポート 11111 が LISTEN 状態になるまで待つ
nc -z 127.0.0.1 11111 && echo "OpenD OK" || echo "OpenD NOT ready"
```

### 1.3 Atlas Bot 起動（判断 1: atlas_v3/main.py 独立 entry point）

```bash
# atlas_v3 独立 entry point 経由（推奨）
# spy_bot.py には触らない
python3 -m atlas_v3.main --mode paper

# または LaunchAgent plist 経由（macOS）
launchctl load ~/Library/LaunchAgents/com.soralab.atlas-paper.plist

# デバッグ時（preflight スキップ）
python3 -m atlas_v3.main --mode paper --skip-preflight
```

### 1.4 監視 daemon 起動

```bash
# 監視 daemon はメインプロセスと同一プロセスで動作するため
# spy_bot.py 起動後に自動的に MonitorDaemon.start() が呼ばれる（設定済みの場合）
# ログ確認:
tail -f /Users/yuusakuichio/trading/data/state_v3/monitor_state.jsonl
```

---

## 2. 日常確認手順（毎朝 JST 09:00 目標）

```bash
# 1. 昨日の日次損益確認
cat /Users/yuusakuichio/trading/data/state_v3/monitor_state.jsonl | tail -20 | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    if d['check_name'] == 'daily_loss':
        print(d['ts'][:10], 'pnl:', d['value'])
"

# 2. レイテンシ p99 確認
python3 -c "
from atlas_v3.ops.latency_monitor import LatencyMonitor
# p99 は state_v3/latency_samples.jsonl から確認
import json
from pathlib import Path
f = Path('data/state_v3/latency_samples.jsonl')
if f.exists():
    samples = [json.loads(l)['latency_ms'] for l in f.read_text().splitlines()[-500:] if l]
    if samples:
        print('recent p99 from last 500:', sorted(samples, reverse=True)[max(0, len(samples)//100-1)])
"

# 3. Kill Switch 状態確認
python3 -c "from common_v3.risk.kill_switch import is_active, get_state; print('active:', is_active()); print('state:', get_state())"
```

---

## 3. 障害対応手順

### 3.1 Bot が停止した場合

```
症状: Pushover に heartbeat timeout アラートが届く
```

```bash
# 1. プロセス確認
ps aux | grep spy_bot.py

# 2. ログ確認
tail -100 /root/logs/spy_bot.log 2>/dev/null || tail -100 /tmp/spy_bot.log

# 3. Kill Switch 状態確認
python3 -c "from common_v3.risk.kill_switch import is_active, get_state; print(is_active(), get_state())"

# 4. Kill Switch が ARMED なら手動解除（ゆうさくさん承認後）
python3 -c "from common_v3.risk.kill_switch import deactivate; print(deactivate(activator='yuusaku_manual', reason='manual_reset_after_check'))"

# 5. 再起動
launchctl unload ~/Library/LaunchAgents/com.spybot.paper.plist
launchctl load ~/Library/LaunchAgents/com.spybot.paper.plist
```

### 3.2 日次損失制限到達

```
症状: monitor.jsonl に daily_loss EMERGENCY が記録される
```

```bash
# 当日の発注を停止するが KillSwitch は立てない
# 翌営業日に自動リセット（launchctl plist のスケジュールによる再起動）

# 即時停止が必要な場合:
python3 -c "from common_v3.risk.kill_switch import activate; activate(reason='daily_loss_limit_hit', activator='yuusaku_manual')"
```

### 3.3 レイテンシ HALT 発動

```
症状: latency_samples.jsonl に p99 > halt_threshold が記録される
      KillSwitch が自動発動される
```

```bash
# 1. ネットワーク確認
ping 127.0.0.1  # OpenD 接続確認
nc -z 127.0.0.1 11111

# 2. OpenD 再起動
# （OpenD Manager から手動再起動）

# 3. KillSwitch 解除（OpenD 正常確認後）
python3 -c "from common_v3.risk.kill_switch import deactivate; deactivate(activator='yuusaku_manual', reason='latency_resolved')"

# 4. LatencyMonitor リセット
python3 -c "from atlas_v3.ops.latency_monitor import LatencyMonitor; m = LatencyMonitor(); m.reset(); print('reset OK')"

# 5. Bot 再起動
launchctl unload ~/Library/LaunchAgents/com.spybot.paper.plist
launchctl load ~/Library/LaunchAgents/com.spybot.paper.plist
```

### 3.4 vault 読み込み失敗

```
症状: VaultError が発生する
```

```bash
# 1. .env.d/moomoo_paper.env の内容確認
cat /Users/yuusakuichio/trading/.env.d/moomoo_paper.env

# 2. 実キーが記入されているか確認（REPLACE_WITH_... のままでないか）
grep "REPLACE_WITH" /Users/yuusakuichio/trading/.env.d/moomoo_paper.env && echo "TEMPLATE NOT FILLED" || echo "Keys look set"

# 3. load_from_env テスト
python3 -c "from atlas_v3.ops.vault import load_from_env; print(load_from_env())"
```

---

## 4. ロールバック手順

### 4.1 設定ファイルロールバック

```bash
# atlas_paper_risk.yaml をデフォルト値に戻す
cd /Users/yuusakuichio/trading
git diff data/configs/atlas_paper_risk.yaml  # 変更内容確認
git checkout data/configs/atlas_paper_risk.yaml  # ロールバック

# 変更反映確認
python3 -c "from atlas_v3.ops.risk_config_loader import load_paper_risk_config; print(load_paper_risk_config())"
```

### 4.2 コードロールバック

```bash
# git log で最後の正常コミットを確認
git log --oneline atlas_v3/ops/ | head -10

# 特定コミットに戻す（<hash> を置き換える）
git checkout <hash> -- atlas_v3/ops/

# テスト実行（ロールバック後に必ず実施）
pytest tests/test_atlas_v3_paper_ops.py -v
```

### 4.3 Paper Bot の全停止

```bash
# ペーパー Bot 停止
launchctl unload ~/Library/LaunchAgents/com.spybot.paper.plist

# Kill Switch 発動（全発注停止）
python3 -c "from common_v3.risk.kill_switch import activate; activate(reason='full_stop_paper', activator='yuusaku_manual')"

# 停止確認
python3 -c "from common_v3.risk.kill_switch import is_active; print('KS active:', is_active())"
ps aux | grep spy_bot.py | grep -v grep || echo "No spy_bot process"
```

---

## 5. 運用チェックリスト（週次）

- [ ] 週次損益を data/eval/daily/ で確認
- [ ] ドローダウンが max_drawdown_pct (15%) を超えていないか確認
- [ ] latency_samples.jsonl の p99 が p99_halt_ms (1000ms) 以下か確認
- [ ] kill_switch_audit.jsonl に不審な bypass 記録がないか確認
- [ ] monitor_state.jsonl の EMERGENCY 件数を記録
- [ ] replay_bt_results/ の最新バックテスト結果と実運用の乖離を確認

---

## 6. エスカレーション連絡先

| 状況 | 対応 |
|------|------|
| Bot 停止 (< 30 分) | 自動再起動を待つ（launchctl plist 監視） |
| Bot 停止 (> 30 分) | ゆうさくさん Pushover 通知 + 手動確認 |
| 日次損失制限到達 | 当日停止 → 翌日ゆうさくさん確認 |
| ドローダウン > 12% | Pushover CRITICAL → ゆうさくさん即確認 |
| KillSwitch 自動発動 | Pushover EMERGENCY → ゆうさくさん即承認後手動解除 |

---

## 7. 重要ファイルパス一覧

| ファイル | 用途 |
|----------|------|
| `data/configs/atlas_paper_risk.yaml` | リスクパラメータ設定 |
| `.env.d/moomoo_paper.env` | moomoo credentials テンプレート |
| `data/state_v3/kill_switch.flag` | KillSwitch フラグ（存在 = ARMED） |
| `data/state_v3/kill_switch_audit.jsonl` | KillSwitch 操作ログ |
| `data/state_v3/monitor_state.jsonl` | 監視 daemon ログ |
| `data/state_v3/latency_samples.jsonl` | レイテンシ計測ログ |
| `data/ops/replay_bt_results/` | バックテスト結果 |
| `atlas_v3/ops/vault.py` | Vault 管理 |
| `atlas_v3/ops/monitor.py` | 監視 daemon |
| `atlas_v3/ops/latency_monitor.py` | レイテンシモニタ |
| `atlas_v3/ops/replay_bt.py` | replay バックテスト |
