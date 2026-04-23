# Premortem Report

- **Generated**: 2026-04-22T19:54:13
- **Source**: fallback
- **Task**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤・Atlas/Chronos 両方が参照。bug zero 規律遵守・既存 common 仕様継承 + 冪等性欠陥修正 + EICAS 3 層 + Andon 3 経路 + Circuit Breaker 追加
- **Overall Risk**: medium
- **GO/NO-GO**: CONDITIONAL_GO
- **Top3 Blockers**: F01, F02, F04
- **Required Gates**:
  - 実モジュールimport確認
  - mock/実API乖離チェック
  - env vars CI設定確認

## A. 致命的失敗シナリオ

| id | title | prob | impact | detection | mitigation |
|---|---|---|---|---|---|
| F01 | flaky test がCI環境のみ失敗し本番バグを隠蔽 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F02 | mock が実APIと乖離し false positive 量産 | high | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F03 | env 依存変数が未設定でテスト全スキップ | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F04 | テストが自分のコードをimportせず常に合格 | low | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F05 | coverage 100%でも統合パスが未カバー | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F06 | 並列テストで共有ファイルに race condition | low | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F07 | fixture のティアダウンが漏れてDB/ファイル汚染 | medium | medium | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |

### 詳細
- **F01 flaky test がCI環境のみ失敗し本番バグを隠蔽**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤... に対し『flaky test がCI環境のみ失敗し本番バグを隠蔽』が発生し、復旧不能または無音で不正動作する。
- **F02 mock が実APIと乖離し false positive 量産**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤... に対し『mock が実APIと乖離し false positive 量産』が発生し、復旧不能または無音で不正動作する。
- **F03 env 依存変数が未設定でテスト全スキップ**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤... に対し『env 依存変数が未設定でテスト全スキップ』が発生し、復旧不能または無音で不正動作する。
- **F04 テストが自分のコードをimportせず常に合格**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤... に対し『テストが自分のコードをimportせず常に合格』が発生し、復旧不能または無音で不正動作する。
- **F05 coverage 100%でも統合パスが未カバー**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤... に対し『coverage 100%でも統合パスが未カバー』が発生し、復旧不能または無音で不正動作する。
- **F06 並列テストで共有ファイルに race condition**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤... に対し『並列テストで共有ファイルに race condition』が発生し、復旧不能または無音で不正動作する。
- **F07 fixture のティアダウンが漏れてDB/ファイル汚染**: common_v3 仕様書 v3 起草・14 Interface の凍結記述。Phase 2 Builder 実装の基盤... に対し『fixture のティアダウンが漏れてDB/ファイル汚染』が発生し、復旧不能または無音で不正動作する。

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
