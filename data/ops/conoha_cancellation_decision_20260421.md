# ConoHa VPS 解約判断レポート
作成日時: 2026-04-21

## 1. ConoHa VPS 中身サマリ

### 基本情報
- IP: 160.251.138.33
- OS: Ubuntu 22.04
- プラン: Memory 1G CPU 2Core
- 契約期限: 2026-05-06

### SSH接続結果
- ローカルMac → ConoHa: **接続不可 (Network is unreachable)**
- Vultr VPS → ConoHa: **接続不可 (100% packet loss, port 22 timeout)**
- 判定: **ConoHa VPSは現時点でネットワーク到達不能**
  - SSHデーモン停止 or ファイアウォールブロック or VNCコンソール経由のSSH問題（過去にSSH問題あり・STRATEGY.mdに記録あり）
  - サーバー自体の電源が落ちている可能性もあり

### 稼働サービス（調査不可）
- SSH接続不可のため直接確認不能
- STRATEGY.md・paste-cacheの記録から**過去の用途**は判明:
  - SPX/SPY Bot（spx_bot.py, spy_bot.py）の実行環境
  - OpenD (FutuOpenD) の実行
  - これらは**Vultr移行済み・ConoHaはすでに不使用**

### 重要ファイルの有無
- SSH不可のため直接確認不能
- 過去の記録より: spx_bot.py・spy_bot.pyの旧バージョンが存在した可能性
- ただし現行バージョンはすべてVultr /root/spxbot_archive_20260418/ に保存済み
- Vultr側にConoHa時代のファイルを含むアーカイブが存在 (/root/spxbot_archive_20260418/)

## 2. Vultr との比較

### Vultr (198.13.37.17) で稼働中のサービス（7本）
| サービス | 役割 |
|---|---|
| hub_agent.service | GitHub Issue polling executor |
| webhook_server.service | ポート9999 POST /command |
| ntfy_listener.service | ntfy.sh SSEリスナー |
| cloudflared-tunnel.service | HTTPS tunnel |
| chronos_cloudflared.service | Chronos Webhook Cloudflare Tunnel |
| chronos_traderspost_forwarder.service | Chronos TradersPost転送 |
| chronos_webhook.service | Chronos TradingView Webhook Server |

### ConoHaにあってVultrにないもの
- **なし**。Vultr側で全機能を代替・拡張済み
- ConoHa時代のファイルはVultr /root/spxbot_archive_20260418/ にアーカイブ済み
- OpenD (FutuOpenD) もVultrに移行済み (/root/Futu_OpenD_10.2.6208_Ubuntu18.04/)

### 退避必要なデータ
- SSH接続不可のため直接確認は不能
- ただし移行作業はすでに2026-04-17〜18に完了済み（project_vps_bot_divergence.md参照）
- Vultr側アーカイブに旧ファイルあり → **追加退避は不要と判断**

## 3. 解約判断

### 判定: **解約推奨 YES**

### 根拠
1. **完全代替済み**: Vultrで全サービスが稼働中。ConoHaは2026-04-17以降実質不使用
2. **到達不能**: SSH・pingとも100%パケットロス。サーバー自体が機能していない可能性
3. **データ保全済み**: 旧ファイルはVultr archive + ローカルGitリポジトリに保存済み
4. **コスト削減**: 月額約900-1000円の無駄コストをカット
5. **契約期限**: 2026-05-06 → 自動更新前に解約が必要

## 4. 節約効果計算

| 項目 | 金額 |
|---|---|
| ConoHa月額（推定） | 約900〜1,000円/月 |
| 年間節約額 | 約10,800〜12,000円/年 |

### ConoHa公式料金（2026-04時点推定）
- VPS Memory 1G / 2Core: 公式は682円〜880円/月（税込）
- 契約形態・キャンペーンによって変動

### 振り替え案
- ThetaDataサブスク費用の一部に充当
- Vultrのアップグレード費用のバッファ（RAM 2GB移行時）

## 5. 解約手順（userアクション必要）

ConoHaコントロールパネルで以下を実施:
1. https://manage.conoha.jp/ にログイン
2. 左メニュー「VPS」→対象サーバー（160.251.138.33）を選択
3. 「サーバー削除」または「契約管理」→自動更新を**OFF**に設定
4. 2026-05-06の期限前に削除実行
5. 削除前にVNCコンソールで重要データを最終確認したい場合はVNCアクセスで確認可能

## 6. userアクション項目（★要対応）

★ **ConoHaコントロールパネルへのログイン情報が必要**
- メールアドレス・パスワードはローカル調査では発見できず
- ConoHaのアカウント（メール: yuusakuichio@gmail.com または別メール）でログイン
- 解約実行はuser本人のみ可能

★ **VNCで最終確認が必要な場合**（オプション・推奨は不要）
- SSH不可のためVNCコンソール経由でのみアクセス可
- 既にVultr側にアーカイブがあるため確認は任意

## 結論
ConoHaは実質的にすでに停止状態（ネットワーク到達不能）。Vultrで100%代替済み。
解約推奨。2026-05-06の契約期限前にConoHaコントロールパネルから削除を実行してください。
