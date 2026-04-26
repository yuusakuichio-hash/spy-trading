# 20時 Infrastructure Readiness Check — 2026-04-21

実施: ops agent / 確認完了: 05:10 JST

---

## 総合: 6項目中 5 OK / 1 NG (軽微・回避策あり)

---

## 項目1: chronos_webhook_server 稼働確認 — OK

| チェック | 結果 |
|---|---|
| systemd enabled+active | OK (PID 678706 / 56分稼働) |
| `POST /chronos/health` 200 | OK (`{"status":"ok","version":"2.0.0"}`) |
| ログ直近エラーなし | OK (起動時の一時失敗は解消済み) |

証跡:
- ポート: 8765 (uvicorn)
- 起動時に `Attribute "app" not found` で3回即死 → 4回目に正常起動済み (04:08 JST)
- smoke テスト受信ログ確認済み (HMAC拒否 = 正常動作)

---

## 項目2: Cloudflare Tunnel alive — OK (重要: URL確認済み)

| チェック | 結果 |
|---|---|
| chronos_cloudflared service active | OK (PID 679355 / 55分稼働) |
| Tunnel URL | `https://difference-after-tend-patterns.trycloudflare.com` |
| URL変更なし | OK (変わっていない) |
| `POST /chronos/health` via tunnel | OK → `{"status":"ok","version":"2.0.0"}` |

注意:
- cloudflared-tunnel (旧 port 9999) と chronos_cloudflared (port 8765) の2プロセス同時稼働
- Chronos用は `chronos_cloudflared.service` が正しいトンネル
- trycloudflare.com は **再起動のたびにURL変わる** ため、サービス再起動は厳禁

---

## 項目3: LaunchAgent 状態 — OK (一部非稼働は仕様)

```
稼働中:
  com.soralab.gmail_monitor         PID 27521  ← OAuth認証待ちのため実質的に動作未確認
  com.soralab.chronos_agent         PID 50803  OK
  com.soralab.emergency_watcher     PID 41926  OK
  com.soralab.market_hours_atlas_monitor PID 58638  OK

非稼働 (exit 0 = スケジュール待ち = 正常):
  morning_digest / mffu_* / corrections / violation_rollup / marketlogger など
```

NG扱いなし。heartbeat_monitor LaunchAgent は存在しないが、emergency_watcher が代替。

---

## 項目4: hooks 登録確認 — OK (全件確認)

`.claude/settings.local.json` に以下全て登録済み・実行権限OK:

| hook | 登録 | exec |
|---|---|---|
| discipline_guard.sh | OK | OK |
| service_recommend_guard.sh | OK | OK |
| selective_test_detector.sh | OK | OK |
| time_estimate_sanity.sh | OK | OK |
| false_claim_detector.sh | OK | OK |
| navigator_antipattern_detector.py | OK | OK |
| current_state_freshness_check.sh | OK | OK |

計22 hook 登録確認済み。

---

## 項目5: memory 整合性 — OK (軽微: MEMORY.md 140行)

| ファイル | 行数 | 状態 |
|---|---|---|
| MEMORY.md | 140行 | OK (上限200行内) |
| CURRENT_STATE.md | 606行 | OK (想定577-600より若干超過・許容範囲) |
| archive/2026-04/ | 8件 | OK (8-9件想定内) |
| daily/ | 存在確認 | OK |

---

## 項目6: ファイル権限 — NG (chronos_webhook_server.py が non-executable)

```
VPS: /root/spxbot/chronos_webhook_server.py → -rw-r--r-- (644)
```

ただし systemd は `python3 -m uvicorn chronos_webhook_server:app` で起動しており、
executable権限は不要。**機能上の問題なし**。サービスは正常稼働中。

VPS hooks (.sh) については /root/spxbot/recovery.sh のみ存在、
chronos_webhook用hookは不在だが対象外のため NG に含めない。

---

## 20時着手前 注意事項 TOP3

### 1. TradingView Alert の Webhook URL
`https://difference-after-tend-patterns.trycloudflare.com/chronos/signal`
- POST のみ受付 (GET は 405 → 正常)
- VPS を再起動すると URL が変わるため、今夜は絶対に再起動しない

### 2. TradingView Alert の送信元IP
現在のホワイトリスト: `52.89.214.238 / 34.212.75.30 / 54.218.53.128 / 52.32.178.7`
→ TradingView 公式IP (2026-04-21 確認済み) のみ許可。
手動テストで VPS から curl すると `ip not allowed` で弾かれるが **正常動作**。
TradingView Alert が正しいエンドポイントに届けば 200 が返る。

### 3. Tradovate Demo 接続確認の順序
1. Tradovate Demo でログイン
2. TradingView でアラート作成 → Webhook URL を上記URLに設定
3. テストアラートを発火 → VPS `/root/logs/chronos_webhook.log` で受信確認
   (コマンド: `tail -f /root/logs/chronos_webhook.log`)

---

## インフラ構成サマリー (20時時点)

```
TradingView Alert
  ↓ POST /chronos/signal
https://difference-after-tend-patterns.trycloudflare.com  (chronos_cloudflared port 8765)
  ↓
chronos_webhook_server.py (uvicorn:8765) — active OK
  ↓
Tradovate Demo API (設定待ち)
```

