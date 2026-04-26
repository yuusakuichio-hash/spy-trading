# ADR-002: F1 採用 — Redteam #7/#8 並列投入後に #3

**起票日**: 2026-04-23 06:14 JST
**起票者**: ソラ提案 → ゆうさくさん承認（screenshot で F1 選択）
**ステータス**: accepted（完了）
**関連**: ADR-001 / Sprint 0.5 P0 #5/#6 完了後の進行判断

---

## コンテキスト

- Sprint 0.5 P0 #5/#6 builder 修正 + navigator PASS 完了
- 残作業: (a) #7/#8 Redteam 検証 (b) #3 CircuitBreaker (c) #4 Deadman
- pytest 全体 2943 passed (+25) / 5 failed（既存）/ regression ゼロ
- 次の進め方を 3 案で提示

## 選択肢

| 案 | 内容 | バグ発生率 |
|---|---|---|
| **F1** | **#7/#8 Redteam 並列投入・PASS 後に #3** | 低（順次・競合なし） |
| F2 | Redteam 2 + Builder #3 + Navigator #3 同時 | 中（3 軸同時で context 衝突リスク） |
| F3 | Redteam 完了まで一旦停止 | 極低（過剰保守的・進度遅） |

## 採用案

**採用**: F1

**判断者**: ゆうさく承認（screenshot で F1 選択）

**理由**:
- 並列だが Redteam 2 件の結果待ってから次へ = 競合なし
- 「バグなし絶対最優先」と「進度第一」の両立点
- F2 は Navigator 並走前提でも context 衝突リスク
- F3 は goal acceleration first に逆行

## 想定結果（事前）

- 短期: Redteam 2 件 PASS なら #3 へ即進行
- 失敗時: 修正 → 再 audit ループ

## 実結果（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- Redteam #7 r1: FAIL（10 件 bypass）→ builder 修正 → navigator PASS → Redteam r2: FAIL → ADR-003 で B 採用
- Redteam #8 r1: FAIL（17 件中 12 件素通り）→ builder 7 件修正 → navigator PASS → Redteam r2: FAIL → ADR-005 で B' 採用

## 振り返り（事後追記）

**最終更新**: 2026-04-23 06:55 JST

- F1 自体の判断は正しかった（並列で 2 件 audit 走ったので工数効率良）
- ただし Redteam r1 FAIL 後の修正→r2 FAIL ループは想定外（イタチごっこ）
- 学習: AST hook 修正の限界を最初から見越して「runtime guard 主防御」の設計選択を ADR で先んじて議論すべきだった

## 関連証跡

- screenshot 「F1: #7/#8 Redteam並列投入・PASS後に #3」
- ADR-003 / ADR-005 が直接の続き
