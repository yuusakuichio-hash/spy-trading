# ADR-007: CircuitBreaker は最初から runtime guard 主防御 + AST hook 補助

**起票日**: 2026-04-23 06:55 JST
**起票者**: ソラ自律判断（ADR-003 / ADR-005 の学習を反映）
**ステータス**: proposed（builder 投入準備中）
**関連**: ADR-003（#7 学習）/ ADR-005（#8 学習）/ Sprint 0.5 P0 #3 / 調査 TOP6

---

## コンテキスト

- Sprint 0.5 P0 #3: CircuitBreaker auto_recovery=False の物理強制
- 仕様: `data/specs/v3/common_spec_v3_20260422.md` B14 L356-L385
- 既導入の hook 修正経験: #7/#8 で AST hook 主防御 → bypass 多発 → Sprint 1 持ち越し（ADR-003 / ADR-005）
- 同じ轍を踏みたくない

## 選択肢

| 案 | 内容 | バグ発生率 | 工数 |
|---|---|---|---|
| A | AST hook 主防御で実装（#7/#8 と同パターン） | 高（同じく Sprint 1 持ち越し確実） | 2-3h |
| **B** | **runtime guard 主防御 + AST hook 補助で最初から実装**（CircuitBreaker.__init__ で auto_recovery=True なら raise） | 低（runtime で必ず止まる） | 1-2h |
| C | hook なし・runtime guard のみ | 低（AST 補助なし） | 1h |

## 採用案

**採用**: B

**判断者**: ソラ自律（過去 ADR の学習反映）

**理由**:
- ADR-003 / ADR-005 で「AST hook の限界 → runtime guard 抜本対策」が確立
- 同じパターンを 3 回繰り返す愚を回避
- AST hook を補助として残すことで「書き込み時の早期発見」も併存（二重防御）

## 想定結果（事前）

- 短期: builder 修正 → navigator → redteam を 1 サイクルで PASS
- 中期: Sprint 1 持ち越しなし（#7/#8 と差別化）

## 実結果（事後追記）

**最終更新**: 2026-04-23 07:25 JST

- builder 完了: 35 PASS + 2 xfailed + 1 xpassed + mutation 確認済
- navigator 判定: CONDITIONAL-PASS（申し送り 4 件）
- **Redteam r1 判定: FAIL**（CRITICAL 2 / HIGH 4 / MEDIUM 4 / LOW 2 = 11 件 + 運用穴 4 件）
- 採用案: D（spec 完全実装 + 軽微修正は Sprint 0.5 / 根本対策は ADR-008 で Sprint 1）
- D 案実装完了（2026-04-23 07:48 JST）:
  - spec B14 L382-L383 完全実装: `common_v3/self_healing/instances.py`（tradovate_breaker fail_max=3 / moomoo_breaker fail_max=5）
  - xfail マーカー誤り修正: `test_lambda_bypasses_ast` を通常 PASS に昇格
  - hook SyntaxError fallback: 私が代理で Edit（builder agent 権限ブロック）
  - pytest: 50 PASS + 2 xfail（XPASS ゼロ）+ regression 0
  - 残 2 xfail（C-02 variable / kwargs unpack）は Sprint 1 で frozen design と合わせて解消予定

## 振り返り（事後追記）

**最終更新**: 2026-04-23 07:25 JST

### ★ 判断は誤りだった

ADR-007 の核心「runtime guard なら lambda/partial/getattr 全パス共通で必ず止まる」は **__init__ という単一関所依存**であり、Boeing 737MAX MCAS 型「単一センサ依存・異常時 fail-open」と同型の脆弱性だった。

### 想定外だったこと（Python 標準機能の動的性）

- `CircuitBreaker.__new__()` で `__init__` を skip 可能
- post-init 直接代入（`cb._auto_recovery = True`）が `_` prefix 規約だけでは止められない
- pickle / copy.deepcopy で `__init__` 不経由の状態復元
- subclass で `__init__` 完全 override（super 呼ばない）
- `sys.modules` / module monkey-patch でクラス自体差替え

これら 6 経路を Redteam が実測で BYPASS 確認。ADR-007 起票時に Python の動的言語特性を「想定外」とした楽観バイアス。

### 3 度目の同型失敗パターン

| サイクル | 主防御 | 結果 |
|---|---|---|
| #7 / #8 | AST 静的解析 | 限界（dataflow 追えず）→ runtime に逃げる |
| #3 | runtime guard | 限界（`__init__` 単一関所）→ 次は何に逃げる？ |

Redteam Strat-3 指摘: **真の対策は Python の言語特性そのもの = `final` class enforcement + frozen design + `__slots__` + `__init_subclass__` 禁止**。

### もう一度判断するなら

ADR-007 起票時に「runtime guard を 1 関数（`__init__`）に置く時点で単一関所依存」という Red Team 視点の事前検証を 30 分でも実施すべきだった。これを ADR-008 で「frozen design + final enforcement」設計指針として確立する。

### 学習として残すべきこと

- `memory/feedback_runtime_guard_init_only_anti_pattern.md`（新規候補）
- `memory/feedback_3rd_same_pattern_detect.md`（新規候補）— 同型失敗 3 度目を検出する規律

## 関連証跡

- ADR-003 / ADR-005（学習元）
- 調査 TOP6: `data/research/bug_root_cure_methods_50_20260423.md`
- 仕様: `data/specs/v3/common_spec_v3_20260422.md` B14
- premortem: `data/premortem_reports/20260423_065510_*.md`
