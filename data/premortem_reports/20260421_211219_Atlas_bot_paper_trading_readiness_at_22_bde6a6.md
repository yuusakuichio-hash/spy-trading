# Premortem Report

- **Generated**: 2026-04-21T21:12:19
- **Source**: fallback
- **Task**: Atlas bot paper trading readiness at 22:30 JST market open: verify spy_bot atlas_agent launchd schedule, confirm will fire, paper account balance, kill switch state
- **Overall Risk**: medium
- **GO/NO-GO**: CONDITIONAL_GO
- **Top3 Blockers**: F01, F04, F07
- **Required Gates**:
  - バックアップ取得確認
  - ロールバック手順文書化
  - 移行後整合性チェック自動化

## A. 致命的失敗シナリオ

| id | title | prob | impact | detection | mitigation |
|---|---|---|---|---|---|
| F01 | 移行スクリプトで元データを上書き・バックアップなし | medium | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F02 | スキーマバージョン不整合で読み込み silent fail | high | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F03 | 部分移行後にプロセスがクラッシュし中途半端な状態 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F04 | ロールバック手順未定義で旧バージョンに戻せない | low | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F05 | 文字コード (UTF-8/Shift-JIS) 変換で文字化け | low | medium | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F06 | 大容量データで移行タイムアウト→中断 | low | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F07 | 並行アクセス中の移行でデータ破損 | medium | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F08 | 移行後の整合性チェックなしで破損データが本番流入 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |

### 詳細
- **F01 移行スクリプトで元データを上書き・バックアップなし**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『移行スクリプトで元データを上書き・バックアップなし』が発生し、復旧不能または無音で不正動作する。
- **F02 スキーマバージョン不整合で読み込み silent fail**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『スキーマバージョン不整合で読み込み silent fail』が発生し、復旧不能または無音で不正動作する。
- **F03 部分移行後にプロセスがクラッシュし中途半端な状態**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『部分移行後にプロセスがクラッシュし中途半端な状態』が発生し、復旧不能または無音で不正動作する。
- **F04 ロールバック手順未定義で旧バージョンに戻せない**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『ロールバック手順未定義で旧バージョンに戻せない』が発生し、復旧不能または無音で不正動作する。
- **F05 文字コード (UTF-8/Shift-JIS) 変換で文字化け**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『文字コード (UTF-8/Shift-JIS) 変換で文字化け』が発生し、復旧不能または無音で不正動作する。
- **F06 大容量データで移行タイムアウト→中断**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『大容量データで移行タイムアウト→中断』が発生し、復旧不能または無音で不正動作する。
- **F07 並行アクセス中の移行でデータ破損**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『並行アクセス中の移行でデータ破損』が発生し、復旧不能または無音で不正動作する。
- **F08 移行後の整合性チェックなしで破損データが本番流入**: Atlas bot paper trading readiness at 22:30 JST market open: ... に対し『移行後の整合性チェックなしで破損データが本番流入』が発生し、復旧不能または無音で不正動作する。

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
