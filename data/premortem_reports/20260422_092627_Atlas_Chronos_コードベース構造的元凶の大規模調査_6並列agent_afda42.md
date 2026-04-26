# Premortem Report

- **Generated**: 2026-04-22T09:26:27
- **Source**: fallback
- **Task**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース肥大・未統合・二重定義・死コードが元凶か。改修 vs 1から書き直し判定。spy_bot.py 18858行 / chronos_bot.py MFFUBot定義不在 / symbol_selector二重定義 等。完璧なバグゼロBot実現の技術ボトルネック特定
- **Overall Risk**: medium
- **GO/NO-GO**: CONDITIONAL_GO
- **Top3 Blockers**: F01, F02, F06
- **Required Gates**:
  - PDT制約チェック実装確認
  - 証拠金超過ガード確認
  - DD上限 kill switch 動作確認

## A. 致命的失敗シナリオ

| id | title | prob | impact | detection | mitigation |
|---|---|---|---|---|---|
| F01 | PDT違反で口座が90日ロック | medium | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F02 | 資金管理ロジックのバグで証拠金超過発注 | low | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F03 | DD上限超過後もエントリー継続で連鎖損失 | low | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F04 | 市場クローズ直前に発注→約定できずポジション持ち越し | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F05 | VIX急騰時に戦術切替が遅延し最悪タイミングで発注 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F06 | 二重発注防止ロジックのバグで同一シグナルを複数回発注 | low | catastrophic | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F07 | fill 確認なしで損切注文が未執行のまま放置 | medium | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |
| F08 | タイムゾーン誤りで場外に発注ループが走る | low | high | ログ監視 / smoke test / assert | 事前バックアップ・DRY_RUN・pre-check・段階リリース |

### 詳細
- **F01 PDT違反で口座が90日ロック**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『PDT違反で口座が90日ロック』が発生し、復旧不能または無音で不正動作する。
- **F02 資金管理ロジックのバグで証拠金超過発注**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『資金管理ロジックのバグで証拠金超過発注』が発生し、復旧不能または無音で不正動作する。
- **F03 DD上限超過後もエントリー継続で連鎖損失**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『DD上限超過後もエントリー継続で連鎖損失』が発生し、復旧不能または無音で不正動作する。
- **F04 市場クローズ直前に発注→約定できずポジション持ち越し**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『市場クローズ直前に発注→約定できずポジション持ち越し』が発生し、復旧不能または無音で不正動作する。
- **F05 VIX急騰時に戦術切替が遅延し最悪タイミングで発注**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『VIX急騰時に戦術切替が遅延し最悪タイミングで発注』が発生し、復旧不能または無音で不正動作する。
- **F06 二重発注防止ロジックのバグで同一シグナルを複数回発注**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『二重発注防止ロジックのバグで同一シグナルを複数回発注』が発生し、復旧不能または無音で不正動作する。
- **F07 fill 確認なしで損切注文が未執行のまま放置**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『fill 確認なしで損切注文が未執行のまま放置』が発生し、復旧不能または無音で不正動作する。
- **F08 タイムゾーン誤りで場外に発注ループが走る**: Atlas/Chronos コードベース構造的元凶の大規模調査(6並列agent・外部知見+コード全走査)。コードベース... に対し『タイムゾーン誤りで場外に発注ループが走る』が発生し、復旧不能または無音で不正動作する。

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
