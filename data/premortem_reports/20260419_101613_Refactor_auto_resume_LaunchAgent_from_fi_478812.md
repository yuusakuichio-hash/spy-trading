# Premortem Report

- **Generated**: 2026-04-19T10:16:13
- **Source**: fallback
- **Task**: Refactor auto_resume LaunchAgent from fixed StartCalendarInterval to StartInterval 1800s polling. Each invocation checks work_queue.md for ACTIVE TASKS and runs claude CLI test command to detect 429 limit. If limit cleared and ACTIVE exists dispatch the queue. Maximum 30-minute loss vs 24-hour loss on fixed schedule
- **Files**: Library/LaunchAgents/com.sora.auto_resume.plist, scripts/auto_resume.sh
- **Overall Risk**: medium
- **GO/NO-GO**: CONDITIONAL_GO
- **Top3 Blockers**: F01, F08, F10
- **Required Gates**:
  - 事前バックアップ
  - smoke test 実施
  - roll-back 手順文書化

## A. 致命的失敗シナリオ

| id | title | prob | impact | detection | mitigation |
|---|---|---|---|---|---|
| F01 | 依存サービス未起動でフェイル | high | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F02 | API rate limit / auth 失敗 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F03 | データ型/スキーマ不整合で silent fail | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F04 | タイムゾーン混在 (JST/ET/UTC) | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F05 | 並行実行で race condition | low | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F06 | ディスク/メモリ枯渇 | low | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F07 | 冬時間/夏時間境界で off-by-one | low | medium | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F08 | 既存ファイル破壊・未バックアップ | medium | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F09 | テスト不足で本番初回に発覚 | high | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F10 | roll-back 手順未定義で復旧不能 | low | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |

### 詳細
- **F01 依存サービス未起動でフェイル**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『依存サービス未起動でフェイル』が発生し、復旧不能または無音で不正動作する。
- **F02 API rate limit / auth 失敗**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『API rate limit / auth 失敗』が発生し、復旧不能または無音で不正動作する。
- **F03 データ型/スキーマ不整合で silent fail**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『データ型/スキーマ不整合で silent fail』が発生し、復旧不能または無音で不正動作する。
- **F04 タイムゾーン混在 (JST/ET/UTC)**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『タイムゾーン混在 (JST/ET/UTC)』が発生し、復旧不能または無音で不正動作する。
- **F05 並行実行で race condition**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『並行実行で race condition』が発生し、復旧不能または無音で不正動作する。
- **F06 ディスク/メモリ枯渇**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『ディスク/メモリ枯渇』が発生し、復旧不能または無音で不正動作する。
- **F07 冬時間/夏時間境界で off-by-one**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『冬時間/夏時間境界で off-by-one』が発生し、復旧不能または無音で不正動作する。
- **F08 既存ファイル破壊・未バックアップ**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『既存ファイル破壊・未バックアップ』が発生し、復旧不能または無音で不正動作する。
- **F09 テスト不足で本番初回に発覚**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『テスト不足で本番初回に発覚』が発生し、復旧不能または無音で不正動作する。
- **F10 roll-back 手順未定義で復旧不能**: Refactor auto_resume LaunchAgent from fixed StartCalendarInt... に対し『roll-back 手順未定義で復旧不能』が発生し、復旧不能または無音で不正動作する。

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
