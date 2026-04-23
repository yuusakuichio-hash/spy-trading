# Redteam 再監査結果 r2: mffu_dry_run_guard.sh

**監査日**: 2026-04-23
**監査対象**:
- `/Users/yuusakuichio/trading/.claude/hooks/mffu_dry_run_guard.sh` (293 行・builder 7 件修正済)
- `/Users/yuusakuichio/trading/tests/test_mffu_dry_run_guard.py` (31 ケース)
**前回 r1**: `data/governance/redteam_audit_mffu_dry_run_guard_20260423.md` (FAIL)
**仕様**: `data/specs/v3/chronos_spec_v3_20260422.md` B5 R2b L172-L192
**監査者**: Redteam (独立 session)

---

## 判定: **FAIL**

### 根拠サマリ

builder 修正後の hook に対して新規攻撃を 19 件投入した結果、**14 件が素通り（74%）**。さらに既知 xfail 5 件も実投入で BYPASS 確認。修正済 7 件のうち C-1, C-2 partial, H-3, C-4 partial の 4 件で**新たな bypass 経路を発見**。M-1 bypass log は「権限欠如時 silent skip」と「tool_name/file_path 固定文字列」で監査価値ほぼゼロ。前回 r1 と同じ構造的欠陥（文字列 prefix match + AST 入門）が温存されており、Phase 2 builder の典型書き方が今も素通る。

事前判定基準の **FAIL 条件「修正済とされる項目で新規 bypass 発見」「仕様 B5 R2b 未充足」両方該当**。

### 重要事実

- pytest 23 PASS + 8 xfail を独立再現済み（contract test の自己申告は再現性 OK）
- ただし **navigator audit 報告ファイル `navigator_audit_mffu_dry_run_guard_20260423.md` が物理的に存在しない**（`data/governance/` 配下に該当ファイルなし）→ navigator PASS は口頭/タスク内宣言のみ・物理証跡欠落・虚偽完了警戒（`feedback_false_completion_5th_governance.md`）

---

## 攻撃シナリオ（重大度順）

### CRITICAL-r2-1: hook 自身を別ディレクトリに置けば全 prod path 検査が無効化（Knight Capital 型）

**前提**: 攻撃者または builder が CI / pre-commit / 別 worktree から hook を起動

**実証**:
```
fake_root/.claude/hooks/mffu_dry_run_guard.sh をコピー
cwd=fake_root で起動（fake_root に .git なし）
→ git rev-parse 失敗 → __file__ fallback → REPO_ROOT=fake_root
→ 実 repo の /Users/.../trading/config/prod/mffu.yaml への書き込み payload を投入
→ rel_path 解決時 "outside repo" 判定 → return 0（素通り）
→ exit=0、dry_run: true がそのまま prod yaml に書き込まれる
```

**最悪結果**: git worktree / submodule / CI pipeline 内で hook が呼ばれた場合、本物の repo 内 prod path への書き込みを完全に見逃す。**Knight Capital 2012**（8 サーバ中 1 台に旧コード残存）と同型。「ガードは存在するが起動コンテキストが違うと無効化」する構造的欠陥。

**重症度**: CRITICAL（修正済とされた C-1 が実は新たな絶対パス迂回を生んだ）

---

### CRITICAL-r2-2: AST detect 14 種以上の自明 bypass

**前提**: builder が「dry_run=True」以外の Python 表現で truthy 値を渡す

**実証（19 件中 BYPASS 確認分のみ抜粋・全て exit=0）**:

| # | 攻撃 payload | 種別 |
|---|---|---|
| C-2.NEW-1 | `MFFUFlexRules(*[p, s, True])` | List unpack |
| C-2.NEW-2 | `MFFUFlexRules(p, s, dry_run=not False)` | UnaryOp |
| C-2.NEW-3 | `MFFUFlexRules(p, s, dry_run=1==1)` | Compare |
| C-2.NEW-4 | `MFFUFlexRules(p, s, dry_run=2)` | int truthy ≠ 1 |
| C-2.NEW-5 | `MFFUFlexRules(p, s, dry_run=[1])` | list truthy |
| H-3.NEW-1 | `MFFUFlexRules(p, s, False, True)` | 4th positional |
| H-3.NEW-2 | `args=(p,s,True); MFFUFlexRules(*args)` | starred expand |
| H-2.NEW-1 | `configs:\n  - name: x\n    dry_run: true` | yaml list element |
| C-4.NEW-1 | `config/preprod/mffu.yaml` | path 別名 |
| C-4.NEW-2 | `config/canary/mffu.yaml` | path 別名 |
| C-4.NEW-3 | `config/production/mffu.yaml` | "production" ≠ "prod" |
| M-3.NEW | NotebookEdit cell_source | matcher 名目だけ |
| C-1.NEW-1 | symlink to external | symlink 経由 |
| C-1.NEW-2 | `Config/Prod/` 大文字 | macOS case-insensitive FS |

**重症度**: CRITICAL（builder が悪意なく書く可能性が高い 5 件以上）

**歴史的類比**:
- **Therac-25 (1985-87)**: 入力 sanitization が「想定パターンしか弾かない」典型。今 hook は同じ罠
- **737MAX (2018-19)**: 単一センサ（=AST 静的解析の constant-only matcher）依存

---

### CRITICAL-r2-3: 起動時 guard（spec L190）完全未実装

**仕様要求**: `spec L190: ENVIRONMENT=prod 環境で起動時 mode=='dry_run' なら即 sys.exit(1) + EICAS Warning`

**実証**:
- `chronos_v3/prop/mffu_flex.py`: **存在しない**
- `chronos_rules_plugin/mffu_flex.py`: 存在するが
  - `sys.exit` / `raise` なし
  - `ENVIRONMENT` / `os.getenv` なし
  - `dry_run` 言及なし
- すなわち **L190 要求の起動時 guard ゼロ**

**最悪結果**: hook を全部 bypass されても、起動時に最後の砦として stop すべきだが、その砦自体がない。**defense in depth の 2 層目が空っぽ**。

**重症度**: CRITICAL（仕様 R2b の「物理封鎖」未充足）

---

### HIGH-r2-1: bypass log は監査として機能していない

**実証**:
1. **silent skip on permission error**: `bypass_log.jsonl` を 444 (read-only) にして bypass 実行 → exit=0、log への追記なし、エラーなし、stderr 沈黙。`>> "$_LOG_PATH" 2>/dev/null || true` で書き込み失敗を**完全に握り潰す**
2. **fixed dummy fields**: 既存 10 行全て `tool_name="(bypass_before_parse)"` / `file_path="(bypass_before_parse)"` 固定。**何が・どのファイルへの・どのツールでの bypass か追跡不能**
3. 結果: bypass log は「件数だけ数えられる無価値な統計」。監査トレースとしては Therac-25 のエラーログ並みに役立たない

**重症度**: HIGH（M-1 修正は形だけで実効性ゼロ）

---

### HIGH-r2-2: chronos_rules_plugin/ の yaml が完全盲点

**実証**: `chronos_rules_plugin/configs/mffu.yaml` への `dry_run: true` 書き込み → exit=0

**原因**: `is_prod_yaml` は `config/prod/` prefix のみ。`is_prod_py` は `chronos_rules_plugin/` を含むが yaml は除外

**最悪結果**: 起動時 guard 未実装の `chronos_rules_plugin/mffu_flex.py` が yaml をロードする時、その yaml が同ディレクトリ配下なら hook は無防備

**重症度**: HIGH

---

### MEDIUM-r2-1: xfail_strict 未設定（#7 と同じ構造的欠陥）

**実証**: `pyproject.toml` に `xfail_strict` の記述なし

**問題**: 8 件の xfail tests が「将来 PASS したら教えてくれる」機能を持たない。Sprint 1 で修正したのに気付かない・regression が xfail に紛れて見えない

**navigator 申し送り 1 と同見解**: #7 同様の構造的欠陥。「#7 と同じ問題なので持ち越し OK」は**前例累積の悪手**。Sprint 0.5 で 1 行追加（`xfail_strict = true`）すれば終わるのに見送りは怠慢

**重症度**: MEDIUM（即修正可・Sprint 1 持ち越し不可）

---

### MEDIUM-r2-2: NotebookEdit 素通り（navigator 申し送り 2 を実害として承認）

**実証**: `tool_name="NotebookEdit"` + `cell_source="dry_run: true\n"` を `config/prod/mffu.yaml` 宛で投入 → exit=0

**実害**: ノートブックは Bot 開発で**直接は使わない**ものの、jupyter 系の探索的分析で `cell_source` に config 文字列を埋めるパターンは一般的。matcher に名目だけ入れて実装ゼロは「設計の嘘」（feedback_false_completion）

**最低限の修正**: matcher から NotebookEdit を**外す**（暗黙の安全保証を撤回）か、`cell_source` も読む。中途半端は監査者を騙す

**重症度**: MEDIUM

---

## 見逃しバグ（コード行番号付き）

### bug-1: L75 prefix CSV split で空文字列除外不十分
```python
prod_py_prefixes = tuple(p for p in prod_py_prefixes_csv.split(",") if p)
```
末尾カンマや連続カンマの場合は OK だが、**空白文字混入** (`"a, b"`) で `" b"` が prefix になり常に false。CSV 設定変更時の落とし穴。`p.strip()` がない。

### bug-2: L107-L111 REPO_ROOT 解決の単一障害点
```python
def _normalize_to_relative(file_path):
    ...
    if REPO_ROOT is None:
        return file_path  # ← 元の絶対パス返す → is_prod_yaml 等は false
    try:
        return str(p.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return None  # outside repo -> not monitored
```
- `REPO_ROOT is None` 時は元のパスを返す → prefix match で false → ガード無効化
- `outside repo` の判定は **None** を返し、main の `if rel_path is None: return 0` で素通り
- **fail-open 設計**（fail-closed が正しい）。Chernobyl 1986 試験時の安全装置 OFF と同型思想

### bug-3: L253 content / new_string の優先順位
```python
code = tool_input.get("new_string") or tool_input.get("content") or ""
```
new_string が空文字列でも `or` で content にフォールバックするのは OK だが、`new_string=""` を明示する空削除と `new_string=None` の区別が曖昧。Edit tool の partial削除攻撃に弱い

### bug-4: L137 chronos_v3/ prefix が実装と乖離
prefix tuple に `chronos_v3/` `common_v3/` があるが、実装は `chronos_rules_plugin/mffu_flex.py`（旧 path）。CI で「新 path に何もない」状態が続けば、ガード対象がスカスカ

---

## 運用的な穴

### ops-1: bypass log のフォーマットが固定 dummy で監査トレース不可能

(HIGH-r2-1 詳細既述)

### ops-2: navigator audit 報告ファイルが物理的に存在しない

`data/governance/navigator_audit_mffu_dry_run_guard_20260423.md` が ls で見つからない。「navigator PASS」は口頭証跡のみ・**虚偽完了報告の直前段階**（feedback_false_completion_4th_20260420 と同型）

### ops-3: hook 配置場所と repo root の relation が暗黙

`__file__` 3 階層上を repo root と仮定する `_get_repo_root()` の fallback は、hook ファイルの相対位置を変えた瞬間崩壊。CI で hook を別 path にコピーして使う運用が出てくれば即無効化

### ops-4: pre-commit / launchd 二重防御の不在

仕様 R2b の防御モデルが PreToolUse hook 1 層しかない。git stage 時 / cron 定期実行による二重チェックがゼロ。**Defense in depth 1 層**は LTCM の単一モデル過信と同型

---

## 戦略的リスク

### strat-1: AST 静的解析で「物理封鎖」を達成しようとする戦略の根本破綻

builder は「Truthy Constant + kwargs 警告 + positional[2]」で C-2 の 80% を捕まえたつもりだが、**実証では 14 種の bypass が現存**。spec L189 の「物理ブロック」要求に対し、現状は「気休めブロック」。

代替戦略:
1. **runtime guard** を `MFFUFlexRules.__init__` 直結で埋め込む
   ```python
   def __init__(self, ..., dry_run=False):
       if dry_run and os.getenv("ENVIRONMENT") == "prod":
           raise RuntimeError("MFFU dry_run forbidden in prod")
   ```
   この 3 行で AST bypass 全て無効化される
2. **mypy plugin** で型レベル禁止
3. **CI grep**: `grep -rn "MFFUFlexRules.*dry_run.*True" config/ chronos_rules_plugin/` を pre-commit で

#7 hook の前例（runtime guard 切替）と同じ判断が #8 でも妥当。AST 静的解析は本質的に**穴の多い網**。

### strat-2: 「Sprint 1 持ち越し」の累積 Normalization of Deviance

xfail 8 件 + 新規 14 件 + spec L190 起動時 guard 未実装 + pre-commit 未実装 + chronos_rules_plugin yaml 監視外 + bypass log 監査価値ゼロ + xfail_strict 未設定 + navigator audit ファイル不在。

合計 **30 件以上が「Sprint 1 で」**として先送り対象になる可能性。Challenger O-ring の「過去問題なかったから今回も大丈夫」と同構造。Sprint 0.5 の品質基準を **CONDITIONAL-PASS 取得目的に最適化**するのは目的倒錯。

### strat-3: builder の自己満足判定パターンの再発

builder が 7 件「修正完了」と宣言したが、実証では 4 件で**修正したつもりが新規 bypass を生んだ**:
- C-1 修正 → fake repo root 攻撃成立（NEW-1）
- C-2 修正 → 14 種の自明 bypass 残存
- H-3 修正 → 4th positional / *args expand 残存
- C-4 修正 → preprod/canary/production 別名素通り

これは feedback_false_completion_5th_governance.md の「実装者の自己満足判定」典型。**builder 単独完了判定禁止**規律違反。

---

## 反論視点（Contrarian）

### Blue Team「7 件修正で CONDITIONAL-PASS」への反論

- builder の主張: 「7 件全部修正・pytest 23 PASS で完了」
- 反論: pytest が PASS するのは builder 自身が xfail 設定で「失敗してもいい項目」を 8 件作ったから。**xfail を strict=False にしている時点でテストは noise にしかならない**（feedback_schema_contract_test_mandatory.md 違反）

### Blue Team「navigator が PASS と言った」への反論

- 反論: navigator audit 報告が物理的に存在しない。口頭 PASS は証跡として無効
- 規律違反: feedback_independent_verification_mandatory.md「redteam 必須」と同じく、navigator 判定は物理ファイル化必須

### Blue Team「#7 と同じ問題なので持ち越し OK」への反論

- 反論: 同じ問題が累積する = 構造的欠陥が修正されていない証拠。**Phase 2 で「同じ問題が 3 hook 連続」**になれば、原因は個別 hook ではなく defense モデル全体。runtime guard への切替を Sprint 0.5 で決断すべき

### Blue Team「risk=medium」への反論

- premortem CONDITIONAL_GO・risk=medium とのことだが、新規 14 bypass + 起動時 guard 完全不在 + bypass log 監査価値ゼロ + navigator 報告ファイル不在 を合算すると **risk=high**。M-1〜M-3 の累積で Knight Capital 級の単一障害点が複数

---

## 申し送り 2 件の独立評価

### 申し送り 1（xfail strict=False 構造的欠陥）

**navigator 判断**: Sprint 1 持ち越し
**Redteam 判断**: **NO・即修正必須**

理由:
1. `pyproject.toml` に `xfail_strict = true` の **1 行追加**で済む（工数 30 秒）
2. #7 と同じ問題で持ち越せば「3 hook 連続で同じ穴」が CLAUDE.md 規律違反
3. xfail テストが Sprint 1 で fix されても気付かないリスクは**今すぐ解消可能**

### 申し送り 2（NotebookEdit 素通り）

**navigator 判断**: Sprint 1 持ち越し
**Redteam 判断**: **matcher から NotebookEdit を即外す**

理由:
1. `cell_source` 対応の正攻法 fix は数時間
2. Sprint 1 持ち越しなら**少なくとも matcher から外して暗黙の安全保証を撤回**（5 分作業）
3. 現状は matcher に NotebookEdit を含めて「対応してます」と看板を出しつつ実装ゼロ → 設計の嘘

---

## 重症度評価と対策優先度

| ID | 重症度 | 内容 | 対策優先 |
|---|---|---|---|
| CRITICAL-r2-1 | CRITICAL | fake repo root 攻撃 | P0 |
| CRITICAL-r2-2 | CRITICAL | AST 14+ bypass | P0 |
| CRITICAL-r2-3 | CRITICAL | 起動時 guard 未実装（spec L190） | P0 |
| HIGH-r2-1 | HIGH | bypass log 監査価値ゼロ | P1 |
| HIGH-r2-2 | HIGH | chronos_rules_plugin yaml 盲点 | P1 |
| MEDIUM-r2-1 | MEDIUM | xfail_strict 未設定 | **P0**（30 秒で済む） |
| MEDIUM-r2-2 | MEDIUM | NotebookEdit 名目だけ | P1（matcher 削除なら 5 分） |
| ops-2 | HIGH | navigator audit 報告ファイル不在 | P0（証跡欠落） |
| strat-1 | CRITICAL | AST 静的解析戦略の根本破綻 | P1（Sprint 1 で runtime guard 切替） |

---

## 最終判定

# **FAIL**

**FAIL 理由**:
1. 修正済 7 件のうち C-1 / C-2 partial / H-3 / C-4 partial の 4 件で**新規 bypass を実証**
2. 仕様 B5 R2b L188-L190 の **2 要件のうち 1 つ（起動時 guard L190）が完全未実装**
3. navigator audit 報告ファイルが物理的に存在せず PASS の証跡欠落
4. M-1 bypass log は機能不全（permission error 時 silent skip / fixed dummy fields）

**CONDITIONAL-PASS への到達条件**（Sprint 0.5 内で必須）:
1. `pyproject.toml` に `xfail_strict = true` 追加（30 秒）
2. NotebookEdit を matcher から削除 or cell_source 対応（5 分〜数時間）
3. fake repo root 攻撃対策: `_get_repo_root()` で `.git` 存在確認 + 一致しない場合 fail-closed（30 分）
4. C-2 追加: `ast.UnaryOp(Not, Constant(False))` `ast.Compare` の最低 2 種追加（1 時間）
5. C-4 追加: `preprod/` `canary/` `production/` を prefix tuple に追加（5 分）
6. M-1 修正: bypass log 書き込み失敗時 stderr に WARN 出力（10 分）
7. **navigator audit 報告ファイル物理化**（必須）

**PASS への到達条件**（Sprint 1）:
- runtime guard を MFFUFlexRules.__init__ に埋め込む（spec L190 充足）
- pre-commit hook で git stage 時の二重検査
- bypass log に tool_name/file_path 実値記録（PreToolUse 解析後 bypass 判定にリファクタ）

