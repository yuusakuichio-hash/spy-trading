---
name: Backtest Pre-registration (OSF準拠)
about: バックテスト実行前に仮説・基準・方法論を事前登録する（p-hacking防止）
title: "[PREREG] <戦術名・期間・仮説の一文要約>"
labels: ["preregistration", "backtest"]
assignees: []
---

<!--
Sora Lab 規律: バックテストを実行する前にこのIssueを作成し、クローズせずに保持する。
事後的な基準変更・仮説変更は禁止。変更が必要な場合は新規Issueで事前登録し直す。

scripts/backtest_validator.py はこのIssueのフォームフィールドを読み取り、
バックテスト結果が事前登録基準を満たしているかを照合する。
-->

## 1. 識別子
- **preregistration_id**: <!-- 例: PREREG-2026-001 (一意のID) -->
- **登録日時 (JST)**: <!-- 例: 2026-04-12 09:00 -->
- **登録者**: <!-- 例: Sora Lab / strategist agent -->

## 2. 仮説
<!--
検証する仮説を一文で明記する。
「この戦術はランダムエントリーよりも有意に高いリターンを生む」形式が望ましい。
-->
- **主仮説 (H1)**:
- **帰無仮説 (H0)**:

## 3. 検証対象の戦術・パラメータ
- **戦術名 (tactic)**: <!-- 例: cs_sell / orb_buy / straddle_buy -->
- **対象銘柄**: <!-- 例: SPY, SPX, QQQ -->
- **期間 (開始〜終了)**: <!-- 例: 2025-01-01 〜 2025-12-31 -->
- **パラメータ (固定値)**: <!-- 例: width=5, delta=0.10, dte=0 -->

## 4. 主要評価指標 (Primary Outcome)
<!-- 1つのみ指定。複数指定はp-hacking の温床になるため禁止 -->
- **primary_metric**: <!-- 例: sharpe_ratio / win_rate / profit_factor / cagr -->
- **合格基準 (success_threshold)**: <!-- 例: sharpe_ratio >= 1.2 -->

## 5. 副次評価指標 (Secondary Outcomes)
<!-- 参考のみ。合否判定には使わない -->
| metric          | 参考基準   | 備考 |
|-----------------|-----------|------|
| max_drawdown    |           |      |
| win_rate        |           |      |
| profit_factor   |           |      |
| total_trades    |           |      |

## 6. サンプルサイズ・検出力
- **最低トレード数 (min_trades)**: <!-- 例: 100 (少ないと統計的に無意味) -->
- **有意水準 (alpha)**: <!-- 例: 0.05 -->
- **検出力 (power) 目標**: <!-- 例: 0.80 -->

## 7. 除外条件
<!-- バックテストから除外するイベント・日付・条件を事前に列挙する -->
- 例: COVIDショック期間 (2020-03-01 〜 2020-04-30) は含まない
- 例: VIX > 40 の日はスキップ (戦術設計上の前提)
-

## 8. 停止条件
<!-- バックテストを途中停止する条件 (look-ahead biasを防ぐための事前定義) -->
- 例: 連続20敗 → バックテスト停止・NO_GO判定
- 例: max_drawdown > 30% → バックテスト停止

## 9. 承認ゲート
- [ ] 仮説は実行前に記述された（事後修正なし）
- [ ] primary_metric は1つのみ
- [ ] min_trades は統計的有意性を担保できる値か確認済み
- [ ] パラメータはバックテスト結果を見る前に固定された
- [ ] scripts/backtest_validator.py でこのIssue番号を指定してから実行する

## 10. backtest_validator.py 実行コマンド
```bash
python3 scripts/backtest_validator.py \
  --prereg-issue <このIssue番号> \
  --results-file <バックテスト結果CSV> \
  --primary-metric <primary_metric> \
  --threshold <success_threshold>
```

## 11. 結果 (バックテスト完了後に記入)
<!-- バックテスト後にここを埋める。事前部分は変更禁止 -->
- **実行日時**:
- **primary_metric 実測値**:
- **判定 (PASS/FAIL)**:
- **総トレード数**:
- **特記事項**:

---
_Sora Lab Blinded Backtest Pre-registration — OSF (Open Science Framework) 準拠_
_参考: Nosek et al. (2018) "The preregistration revolution" PNAS_
