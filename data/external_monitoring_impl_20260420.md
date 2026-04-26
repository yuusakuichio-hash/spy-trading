# 外部死活監視 実装サマリー (2026-04-20)

## 実装完了

### 背景
2026-04-20 Pushover IP ban で全通知経路が死亡。
UptimeRobot + Healthchecks.io の2段構成で完全独立外部監視を実装した。

---

## 実装ファイル

### 新規作成

| ファイル | 役割 |
|---|---|
| `common/external_health_ping.py` | Healthchecks.io ping 共通モジュール |
| `tests/test_external_health_ping.py` | 21件テスト（全合格） |
| `scripts/external_health_aggregator.py` | 10分毎集約ping スクリプト |
| `LaunchAgents/com.sora.external_health_check.plist` | 集約ping LaunchAgent |
| `docs/external_monitoring_setup.md` | セットアップ手順書 |

### 変更

| ファイル | 変更内容 |
|---|---|
| `chronos_agent.py` | 起動時start/2分毎success/エラー時fail ping追加 (Chronos CME先物) |
| `atlas_agent.py` | 同上 (Atlas SPXオプション) |
| `chronos_watchdog.py` | 5分毎success/エラー時fail ping追加 (Chronos) |
| `atlas_watchdog.py` | 同上 (Atlas) |
| `sora_heartbeat_monitor.py` | 5分毎success/エラー時fail ping追加 |
| `health_server.py` | /health に全Bot heartbeat状態集約・healthy/degraded/critical判定 |
| `.env` | HC_UUID_* プレースホルダー追加（gitignore対象・ローカルのみ） |

---

## アーキテクチャ

```
Tier 1 主監視（UptimeRobot）
  UptimeRobot → 5分毎 GET http://198.13.37.17:8080/health
  health_server → heartbeat ファイルを見て healthy/degraded/critical を返す
  503 返却 → UptimeRobot から Email/SMS

Tier 2 保険（Healthchecks.io）
  各Bot メインループ → 2-5分毎に ping_healthchecks() を呼ぶ
  ping 届かない → Healthchecks.io から Email/SMS
  集約ping (10分毎) → health_aggregator チェックで全体状態を報告
```

---

## 設計原則（守られた点）

- Pushover 経路と完全独立（別認証・別ベンダー・別ネットワーク経路）
- ping 失敗でも本業継続（try-except で吸収・False 返却のみ）
- Atlas/Chronos 混同禁止コメントを全ファイルに明示
- UUID 未設定は warning + False 返却（本業に影響なし）
- 新Bot 追加は `_COMPONENT_ENV_MAP` に1行追記するだけ

---

## 残作業（ゆうさくさんが実施）

1. Healthchecks.io アカウント作成 → UUID 取得（9個）
2. .env に UUID を入力
3. UptimeRobot アカウント作成 → Monitor 追加（VPS IP:8080）
4. LaunchAgent ロード: `launchctl load LaunchAgents/com.sora.external_health_check.plist`
5. 動作確認: `python3 -m common.external_health_ping --list`

詳細手順: `docs/external_monitoring_setup.md`

---

## コミット履歴

- `b1eba09` feat(monitoring): external_health_ping 共通モジュール新設 + テスト21件全合格
- `271a2d3` feat(monitoring): 全Bot に外部死活監視 ping 統合 + health_server 拡張
- (次) feat(monitoring): setup guide + 実装サマリー

---

*作成: Sora Lab builder / 2026-04-20*
