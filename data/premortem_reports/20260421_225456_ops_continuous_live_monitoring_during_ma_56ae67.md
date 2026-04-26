# Premortem Report

- **Generated**: 2026-04-21T22:54:56
- **Source**: fallback
- **Task**: ops continuous live monitoring during market with 1-minute granularity data feed health strategy fire patterns auto-restart automated healing
- **Overall Risk**: medium
- **GO/NO-GO**: CONDITIONAL_GO
- **Top3 Blockers**: F01, F04, F05
- **Required Gates**:
  - Auth smoke test (401確認)
  - Retry/timeout 設定確認
  - 冪等性・べき等ガード実装

## A. 致命的失敗シナリオ

| id | title | prob | impact | detection | mitigation |
|---|---|---|---|---|---|
| F01 | Bearer トークン期限切れで全リクエスト401 | high | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F02 | エンドポイントURL変更で silent 404 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F03 | レート制限429で発注ループが詰まる | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F04 | タイムアウト未設定でスレッドが永久ブロック | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F05 | リプレイ攻撃でべき等でない操作が二重実行 | low | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F06 | TLS証明書期限切れで接続拒否 | low | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F07 | JSONレスポンスのフィールド名変更で KeyError | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F08 | ネットワーク瞬断でリトライなし→発注欠落 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |

### 詳細
- **F01 Bearer トークン期限切れで全リクエスト401**: ops continuous live monitoring during market with 1-minute g... に対し『Bearer トークン期限切れで全リクエスト401』が発生し、復旧不能または無音で不正動作する。
- **F02 エンドポイントURL変更で silent 404**: ops continuous live monitoring during market with 1-minute g... に対し『エンドポイントURL変更で silent 404』が発生し、復旧不能または無音で不正動作する。
- **F03 レート制限429で発注ループが詰まる**: ops continuous live monitoring during market with 1-minute g... に対し『レート制限429で発注ループが詰まる』が発生し、復旧不能または無音で不正動作する。
- **F04 タイムアウト未設定でスレッドが永久ブロック**: ops continuous live monitoring during market with 1-minute g... に対し『タイムアウト未設定でスレッドが永久ブロック』が発生し、復旧不能または無音で不正動作する。
- **F05 リプレイ攻撃でべき等でない操作が二重実行**: ops continuous live monitoring during market with 1-minute g... に対し『リプレイ攻撃でべき等でない操作が二重実行』が発生し、復旧不能または無音で不正動作する。
- **F06 TLS証明書期限切れで接続拒否**: ops continuous live monitoring during market with 1-minute g... に対し『TLS証明書期限切れで接続拒否』が発生し、復旧不能または無音で不正動作する。
- **F07 JSONレスポンスのフィールド名変更で KeyError**: ops continuous live monitoring during market with 1-minute g... に対し『JSONレスポンスのフィールド名変更で KeyError』が発生し、復旧不能または無音で不正動作する。
- **F08 ネットワーク瞬断でリトライなし→発注欠落**: ops continuous live monitoring during market with 1-minute g... に対し『ネットワーク瞬断でリトライなし→発注欠落』が発生し、復旧不能または無音で不正動作する。

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
