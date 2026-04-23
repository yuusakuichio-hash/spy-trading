# Premortem Report

- **Generated**: 2026-04-21T23:48:35
- **Source**: fallback
- **Task**: iterative bug killer daemon cycle tonight: take bug from inventory fix diagnose verify update log move next continuous cycle 100 iterations possible complete all fixes
- **Overall Risk**: medium
- **GO/NO-GO**: CONDITIONAL_GO
- **Top3 Blockers**: F01, F04, F07
- **Required Gates**:
  - デプロイ後 status 確認
  - 旧プロセス停止確認
  - .env 転送確認

## A. 致命的失敗シナリオ

| id | title | prob | impact | detection | mitigation |
|---|---|---|---|---|---|
| F01 | デプロイ先に旧バージョンが残存し新旧混在で動作 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F02 | systemd service が ExecStart パス誤りで即死 | high | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F03 | SSH key 権限が 644 で認証失敗 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F04 | デプロイ後の動作確認なしで本番バグ放置 | high | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F05 | 旧プロセスが残留して二重起動 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F06 | ログディレクトリ未作成でサービス起動失敗 | medium | medium | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F07 | 環境変数 .env 未転送で本番 API key なし | medium | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |

### 詳細
- **F01 デプロイ先に旧バージョンが残存し新旧混在で動作**: iterative bug killer daemon cycle tonight: take bug from inv... に対し『デプロイ先に旧バージョンが残存し新旧混在で動作』が発生し、復旧不能または無音で不正動作する。
- **F02 systemd service が ExecStart パス誤りで即死**: iterative bug killer daemon cycle tonight: take bug from inv... に対し『systemd service が ExecStart パス誤りで即死』が発生し、復旧不能または無音で不正動作する。
- **F03 SSH key 権限が 644 で認証失敗**: iterative bug killer daemon cycle tonight: take bug from inv... に対し『SSH key 権限が 644 で認証失敗』が発生し、復旧不能または無音で不正動作する。
- **F04 デプロイ後の動作確認なしで本番バグ放置**: iterative bug killer daemon cycle tonight: take bug from inv... に対し『デプロイ後の動作確認なしで本番バグ放置』が発生し、復旧不能または無音で不正動作する。
- **F05 旧プロセスが残留して二重起動**: iterative bug killer daemon cycle tonight: take bug from inv... に対し『旧プロセスが残留して二重起動』が発生し、復旧不能または無音で不正動作する。
- **F06 ログディレクトリ未作成でサービス起動失敗**: iterative bug killer daemon cycle tonight: take bug from inv... に対し『ログディレクトリ未作成でサービス起動失敗』が発生し、復旧不能または無音で不正動作する。
- **F07 環境変数 .env 未転送で本番 API key なし**: iterative bug killer daemon cycle tonight: take bug from inv... に対し『環境変数 .env 未転送で本番 API key なし』が発生し、復旧不能または無音で不正動作する。

## B. HAZOP Guide Words

- **No/None**: risk=No/None適用時の逸脱 / mitigation=その要素が完全に欠落したら？
- **More**: risk=More適用時の逸脱 / mitigation=想定より多すぎる/大きすぎる/速すぎる場合は？
- **Less**: risk=Less適用時の逸脱 / mitigation=想定より少ない/小さい/遅い場合は？
- **As well as**: risk=As well as適用時の逸脱 / mitigation=想定外の追加が混入したら？
- **Part of**: risk=Part of適用時の逸脱 / mitigation=一部だけ成立・残りが欠落したら？
- **Reverse**: risk=Reverse適用時の逸脱 / mitigation=逆方向・真逆の動作が起きたら？
- **Other than / Instead of**: risk=Other than / Instead of適用時の逸脱 / mitigation=全く別のものに置き換わったら？
- **Early**: risk=Early適用時の逸脱 / mitigation=想定より早く起きたら？
- **Late**: risk=Late適用時の逸脱 / mitigation=想定より遅く起きたら？
- **Before**: risk=Before適用時の逸脱 / mitigation=前工程より前に発火したら？
- **After**: risk=After適用時の逸脱 / mitigation=後続が先に終わったら？

## C. Competing Hypotheses (ACH)

### H1. 実装は意図通り動く
- evidence_for: 仕様書通り
- evidence_against: 本番未検証
- test: smoke test
### H2. 隠れた副作用がある
- evidence_for: 既存コードとの結合部
- evidence_against: なし
- test: 既存回帰テスト
### H3. 前提条件が既に壊れている
- evidence_for: 依存サービスの稼働状況不明
- evidence_against: 直近稼働ログあり
- test: 依存 healthcheck

## ⚠ Fallback Notice

Haiku API 未使用。理由: `no_api_key`
骨組みテンプレートのみ。API key を設定して再実行推奨。

---
_Gary Klein premortem + HAZOP + ACH, Sora Lab_
