# SPX Trading Bot — Claude Code オーケストレーションルール

## プロジェクト概要
- SPX/SPY 0DTE オプション自動取引Bot
- VPS: 198.13.37.17 (Vultr)
- プラットフォーム: moomoo/FutuOpenD API
- GitHub: yuusakuichio-hash/spy-trading

## エージェント組織

| エージェント | 役割 | 起動タイミング |
|---|---|---|
| **secretary** | 会話窓口・タスク整理・GitHub Issue・Pushover報告 | デフォルト窓口 |
| **builder** | VPS/Bot/インフラ構築・デプロイ | 構築・実装タスク |
| **ops** | 監視・障害対応・ログ調査 | 運用・障害対応 |
| **strategist** | 取引戦略立案・バックテスト | 戦略設計・改善 |
| **analyst** | P&L分析・リスク評価・レポート | 分析・レポート |

## オーケストレーションルール

### 1. 調査→計画→実行の3フェーズ必須
破壊的操作・本番変更は必ず：
1. **調査**: 現状をread-onlyで確認
2. **計画**: Pushoverで計画報告・承認待ち
3. **実行**: 承認後のみ実行

### 2. VPS操作の安全規則
- OpenD/FutuOpenD の設定変更: 必ず計画をPushoverで報告してから
- 残り試行回数があるログイン操作: 1回失敗=即停止・報告
- サービス再起動は ops エージェントが担当
- 新規構築は builder エージェントが担当

### 3. 通知ルール
- 全タスク完了時: Pushoverで報告（必須）
- 障害・エラー時: Pushoverでpriority=1（緊急）
- 承認が必要な計画: Pushoverで送信してから待機

### 4. GitHub運用
- TODOは `--label todo` でIssue作成
- VPS実行は `--label hub-command` でIssue作成（hub_agentが自動実行）
- hub_relay ワークフロー: GitHub Actions経由でVPS直接実行

## VPS通信チャンネル（優先順位順）

1. **SSH直接** — 最速・確実（開発時）
   ```bash
   ssh -i ~/.ssh/deploy_key root@198.13.37.17
   ```

2. **webhook_server** — HTTP/9999（プログラム実行）
   ```bash
   curl -X POST http://198.13.37.17:9999/command \
     -H "Authorization: Bearer REDACTED_TOKEN_OLD" \
     -d '{"command": "..."}'
   ```

3. **ntfy.sh** — 非同期コマンド送信
   ```bash
   curl -d "コマンド" https://ntfy.sh/spxbot-hub-yuusaku2026
   # 結果: https://ntfy.sh/spxbot-hub-result-yuusaku2026
   ```

4. **GitHub Actions** — hub_relay ワークフロー
   ```bash
   gh workflow run hub_relay.yml --field command="..."
   ```

5. **GitHub Issue** — hub-commandラベル（hub_agentが60秒以内に処理）

## Pushover設定
- Token: a5rb9ipb3yrdanv3vk4n8x28qt7io9
- User: u2cevk8nktib3sr148rw2hs78ecvux

## 重要な注意事項
- OpenD ログインは残り **2回** — 正しいパスワード確認前は絶対に試行しない
- TRADE_PASSWORD(008095) ≠ ログインパスワード。login_pwdはMD5ハッシュが必要
- cloudflared tunnel URLはVPS再起動で変わる（固定はSSH/webhook/ntfyを使う）
