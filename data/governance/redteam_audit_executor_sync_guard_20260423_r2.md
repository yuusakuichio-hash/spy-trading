# Redteam 再audit 結果 (R2): executor_sync_only_guard.sh

**監査日時**: 2026-04-23
**監査者**: Redteam (独立 session・builder/navigator 両報告を鵜呑みにしない)
**監査対象**:
- `/Users/yuusakuichio/trading/.claude/hooks/executor_sync_only_guard.sh` (修正版・196行)
- `/Users/yuusakuichio/trading/tests/test_executor_sync_only_guard.py` (15 test)
- `/Users/yuusakuichio/trading/tests/test_executor_sync_only_guard_redteam.py` (21 test = 16 PASS + 5 XFAIL)

**前回判定**: FAIL（CRITICAL 10件・HIGH 4件・MEDIUM 4件・LOW 2件）
**前回レポート**: `data/governance/redteam_audit_executor_sync_guard_20260423.md`

---

## 判定: **FAIL**

修正済みとされる 6 項目（C-01/C-06/C-08A/C-08B/C-09/C-10）のうち、**C-09 は半分しか修正されていない**（dict-level type check のみで内部値型は素通り）。さらに **新規 CRITICAL 5 件** を発見した。最も致命的なのは **NotebookEdit が公式 schema (`new_source` キー) で送信されると hook が完全素通りする** こと。これは hook 登録対象 (`.claude/settings.local.json` L120-L127) に NotebookEdit が含まれていながら、AST 検査が new_source を読まないため、設計意図 vs 実装の乖離である（Therac-25 型）。

---

## 攻撃シナリオ（5 件以上）

### S-1: NotebookEdit 公式 schema bypass [新規 CRITICAL]
- **前提**: builder/navigator は new_string キーのみ検査する hook を採用
- **トリガー**: Phase 2 builder が Jupyter notebook で executor.submit を書き、Claude Code が公式 schema (new_source / cell_id / edit_mode) で発行
- **波及**: hook L82-L86 の get("new_string") は None → "" → strip = "" → sys.exit(0)。AST 検査スキップ
- **最悪結果**: notebook ベースで kill_switch 並列化が完全 bypass。仕様 B16 が物理強制不能

### S-2: file_path / new_string 内部値の型偽装で fail-open [新規 CRITICAL]
- **前提**: builder C-09 fix は isinstance(tool_input, dict) のみ
- **トリガー**: payload schema 変動 / マルウェア / Claude Code バグで file_path=int / list / dict / bool が来る
- **波及**: get("file_path", "") は値を返す → endswith("py") で AttributeError → rc=1 (warning allow)
- **最悪結果**: Boeing 737MAX MCAS 型 fail-open。攻撃者は意図的に malformed payload を作成して規律違反コードを通過させられる
- **証拠**:
  - file_path=int + 違反コード → rc=1 (allow)
  - file_path=list + 違反コード → rc=1 (allow)
  - file_path=dict + 違反コード → rc=1 (allow)
  - file_path=bool + 違反コード → rc=1 (allow)

### S-3: lambda body の Subscript / IfExp / NamedExpr で bypass [新規 CRITICAL]
- **前提**: builder C-01 fix は ast.Lambda 再帰だが body の表現が Name/Attribute/Lambda/Call/Subscript のみ対応
- **トリガー**: builder が `lambda: fns['k']()` / `lambda: kill_switch.is_active() if cond else None` / `lambda: (f := kill_switch.is_active)()` を書く
- **波及**: _leading_callable_name は IfExp / NamedExpr / Tuple / List を扱わない → None 返却 → not in FORBIDDEN → allow
- **最悪結果**: 検証で「lambda は塞いだ」と誤認し、lambda の自然な変形で完全 bypass
- **検証**:
  - `executor.submit(lambda: fns['k']())` → rc=0
  - `executor.submit(lambda: kill_switch.is_active() if True else None)` → rc=0
  - `executor.submit(lambda: (f := kill_switch.is_active)())` → rc=0
  - `executor.submit((kill_switch.is_active,)[0])` → rc=0
  - `executor.submit([kill_switch.is_active][0])` → rc=0

### S-4: __import__ / import_module 動的引数で大規模 bypass [新規 CRITICAL]
- **前提**: builder C-08A/C-08B fix は文字列 Constant 引数のみ検査
- **トリガー**: 攻撃ベクトル多数:
  - 変数経由: `mod = 'asyncio'; __import__(mod)`
  - 文字列演算: `__import__('async' + 'io')` / `__import__('asyncio'.replace(...))`
  - getattr: `getattr(__builtins__, '__import__')('asyncio')`
  - kwarg: `__import__(name='asyncio')` / `importlib.import_module(name='asyncio')`
  - from-importlib: `from importlib import import_module; import_module('asyncio')`
  - bytes decode: `__import__(b'asyncio'.decode())`
  - subscript: `__builtins__['__import__']('asyncio')`
  - exec/eval: `exec('import asyncio')`
  - kwargs unpack: `__import__(**{'name': 'asyncio'})`
  - f-string: `__import__(f'asyncio')`
- **波及**: AST 検査の射程外で全て allow
- **最悪結果**: builder/navigator は「動的 import を塞いだ」と称しているが、塞いだのは最も安直な 2 形式のみ。**実用的攻撃面は 14 件以上残存**

### S-5: file_path 欠落 payload で全検査スキップ [新規 CRITICAL]
- **前提**: hook L78 で file_path = tool_input.get("file_path", "") で空文字
- **トリガー**: payload schema バグ / 攻撃者の意図的欠落
- **波及**: 空文字 .endswith(".py") = False → sys.exit(0)
- **最悪結果**: 違反コード送信時、file_path キー削除だけで検査スキップ
- **証拠**: `{"tool_name": "Write", "tool_input": {"new_string": "executor.submit(kill_switch.is_active)"}}` → rc=0

### S-6: keyword 引数で executor.submit bypass [navigator 申し送り H-01 だが過小評価]
- **前提**: hook L178 if node.args: のみで kwargs 未検査
- **トリガー**: `executor.submit(fn=kill_switch.is_active)` `executor.submit(callable=kill_switch.is_active)` `executor.submit(*[kill_switch.is_active])` 等
- **波及**: node.args が空 → callee=None → 検査スキップ
- **最悪結果**: builder が公式 ProcessPoolExecutor.submit の docstring に従って fn= キーワードで書くだけで bypass
- **navigator 評価ミス**: 「実害低だが Sprint 1 scope 推奨」とされているが、ProcessPoolExecutor / ThreadPoolExecutor.submit の signature は (fn, /, *args, **kwargs) なので fn= は完全に正規ユースケース。実害は CRITICAL レベル

### S-7: Subscript fail-open の固定化 [仕様文書化なし]
- **前提**: hook L168-L171 で Subscript → None 返却を「false-negative 許容」と明記
- **トリガー**: builder が `fns["k"]()` / `factories[type]()` のような正当な dispatcher を書いた場合と、攻撃的に dispatcher 偽装する場合の区別なし
- **波及**: spec_v3 文書 B16 に「Subscript fail-open 仕様」が明記されていなければ、Phase 2 audit 時に「hook が壊れている」と誤判定される
- **最悪結果**: Sprint 1 で runtime guard 未実装のままこの hook を本番運用すると、規律違反が無告知で通過

---

## 見逃しバグ（具体行番号付き）

### B-1 [hook L78] file_path 内部値の型未検査
```bash
file_path = tool_input.get("file_path", "") or ""
```
file_path が int / list / dict / bool の場合 .endswith("py") で AttributeError → rc=1 fail-open。
**修正必須**: `if not isinstance(file_path, str): sys.exit(2)`

### B-2 [hook L82-L86] new_string / content の型未検査
```python
code = (
    tool_input.get("new_string")
    or tool_input.get("content")
    or ""
)
if not code.strip():
```
new_string が int / list / dict の場合 .strip() で AttributeError → rc=1 fail-open。
**修正必須**: 取得後 `if not isinstance(code, str): sys.exit(2)`

### B-3 [hook L82-L86] NotebookEdit の new_source キー未読
**証拠**: Claude Code 公式 NotebookEdit schema は new_source / cell_source / source（new_string ではない）。
hook は new_string と content しか見ないため、NotebookEdit は完全 bypass。
**修正必須**: `tool_input.get("new_source") or tool_input.get("source")` も fallback 追加

### B-4 [hook L144-L172] _leading_callable_name の AST node 不完全
**抜け**: ast.IfExp / ast.NamedExpr / ast.Tuple / ast.List / ast.Starred を一切扱わない。
**証拠**: lambda body の `kill_switch.is_active() if cond else None` / `(f := kill_switch.is_active)()` / `(kill_switch.is_active,)[0]` 全て None 返却で allow。
**修正必須**: walk-mode で「lambda body 内の任意の場所に FORBIDDEN 名を含むか」を検査するか、call-graph DFS に切替。

### B-5 [hook L174-L184] node.args しか見ず kwargs 未検査
```python
if node.args:
    callee = _leading_callable_name(node.args[0])
```
keyword 引数 (`submit(fn=...)`)・starred (`submit(*[...])`)・positional 2nd 以降 (`submit(wrap, is_active)`) が完全素通り。
**修正必須**: node.keywords も走査・*args も検査・全 args 内の任意位置に FORBIDDEN 名がないか走査

### B-6 [hook L113-L142] __import__ の文字列定数のみ検査
ast.Constant でない引数（Name / BinOp / Call / JoinedStr / kwargs）が全て無視される。
**修正必須**: 第1引数の type に関わらず、本物の動的 import を遮断するなら runtime guard 必須（AST では本質的に不可能）。**少なくとも文字列以外の引数を __import__ / import_module に渡している場合は警告 BLOCK にすべき**（false positive リスクは fail-closed 原則で許容）。

---

## 運用的な穴（3 件以上）

### O-1 hook 登録対象と検査ロジックの仕様乖離
`.claude/settings.local.json` L120-L127 で `Write|Edit|NotebookEdit` 全て登録されているが、hook 内検査は new_string キーのみ。
NotebookEdit を登録対象に含めながら検査しないなら、登録から外すか new_source 対応必須。**現状は「ザル登録」**。

### O-2 xfail strict=False の運用劣化
test_executor_sync_only_guard_redteam.py の C-02〜C-07 が strict=False。
- Sprint 1 で runtime guard が**部分的に**実装されたとき、XPASS しても test 全体は PASS のまま
- 「Sprint 1 で塞ぐ計画」が文書化されているのは builder 自己申告のみ。
- navigator の申し送り「strict=True 化義務が test 内に未記載」は妥当だが、過小評価。**Sprint 1 が遅延または忘却されると、xfail がそのまま忘れられる**（Normalization of Deviance / Challenger 1986 型）。

### O-3 bypass 環境変数 EXECUTOR_SYNC_GUARD_BYPASS=1 の存在
hook L14-L16 で env=1 だけで完全 bypass。`.claude/settings.local.json` 環境や launchd / cron / pytest fixture で設定されると物理強制が無効化。
**現状は使用箇所監査なし**（grep -r で confirm 必要）。チェルノブイリ型「試験中の安全装置無効化」リスク。

### O-4 fail-open rc=1 の体系的見逃し
Claude Code は rc=2 のみ block 扱い、rc=1 は warning として allow。
hook 内の `sys.exit(1)` は L36（argv guard）のみだが、AttributeError / その他 unhandled exception は subprocess.run 経由で rc=1 で外に出る。
**hook 全体に try/except を被せて fail-closed (rc=2) する設計が欠落**。

### O-5 hook 自身のバージョン管理なし
hook ファイルに version / hash がない。修正版と旧版の区別が git log のみ。Phase 2 中に「hook を一時的に bypass→戻し忘れ」の事故耐性なし。

---

## 戦略的リスク（3 件以上）

### R-1 「修正済み」を称する hook が CRITICAL bypass を 6+件残す状態で本番投入される
- builder「6件修正完了」/ navigator「PASS」の合意で本番投入されると、Phase 2 Sprint 1 の runtime guard 設計者は「すでに hook で塞がれている」と誤認
- **Knight Capital 2012 型**: 「テスト済み」の安心感で十分性検証スキップ → $440M 損失
- 本件で言えば、Phase 2 builder が「lambda で包めば通る」と学習して並列化禁止コードを次々通す

### R-2 「Phase 2 Sprint 1 で塞ぐ」の累積先送り
- C-02 / C-03 / C-04 / C-05 / C-07 = 5 件
- B-1 / B-2 / B-3 / B-4 / B-5 / B-6 = 6 件（本日新規発見）
- O-1 / O-2 / O-3 / O-4 / O-5 = 5 件
- **計 16 件を Sprint 1 に先送り**。Sprint 1 scope の現実性が崩壊
- CLAUDE.md「Phase 2 でやる」の先送り累積禁止に正面衝突

### R-3 navigator の PASS 判定が組織信頼を毀損
- navigator は H-01（kwarg）を「実害低」と評価したが、ProcessPoolExecutor.submit の公式 signature が `(fn, /, *args, **kwargs)` で fn= は正規呼び出し → 実害高
- navigator が hook の attack surface を網羅検査していないことが露呈
- **Boeing 737MAX 2018-19 型**: 単一監視（navigator 1人）への過信。Redteam という独立検査がなければ通っていた

---

## 反論視点（Contrarian）

### builder「修正完了」への反論
- 「16 PASS / 5 XFAIL」と称するが、**XFAIL に格下げされた C-02〜C-07 は test PASS を機械的に維持するための偽装**
- xfail strict=False は「test が緑のままでよい」言い訳であって、規律違反コードを通過させる事実は変わらない
- 真の修正完了は「runtime guard 実装 + xfail 削除 + strict=True 化 + 全 PASS」のはず

### navigator「PASS」への反論
- navigator は申し送り 2 件のみ。本日 Redteam が 5 件 CRITICAL を新規発見
- これは navigator 側で「攻撃ベクトル網羅」のチェックリストが運用されていない証左
- 「PASS = 設計通り動作」と「PASS = 攻撃面なし」を navigator は混同している

### 「premortem CONDITIONAL_GO だから本番投入可」への反論
- premortem report risk=medium は**当時の bypass list ベース**の評価
- 本日新規発見 5 件 CRITICAL を premortem は知らない → risk=high に格上げ必須
- CONDITIONAL_GO の「条件」が満たされていない（条件: 既知 bypass を全て塞ぐ or runtime guard 完了）

### 「実用的には十分」への反論
- 「Phase 2 builder がうっかり書く形」のうち、塞いだのは lambda 直書きと partial 直書きの 2 形式
- builder が natural Python (kwarg / lambda+ifexp / lambda+subscript / 変数経由) を書けば即 bypass
- 実用的閾値は 50% 未満。**「半分塞いだから OK」は規律違反**

---

## 重症度評価

| 項目 | severity | 優先度 |
|---|---|---|
| B-3 NotebookEdit new_source 未読 | **CRITICAL** | P0 即時 |
| B-1 file_path 内部値 type 未検査 | **CRITICAL** | P0 即時 |
| B-2 new_string 内部値 type 未検査 | **CRITICAL** | P0 即時 |
| B-4 lambda body の AST node 不完全 (Subscript/IfExp/NamedExpr/Tuple/List) | **CRITICAL** | P0 即時 |
| B-5 kwargs / starred / 2nd 以降の args 未検査 | **CRITICAL** | P0 即時 |
| B-6 __import__ 動的引数 (var/concat/getattr/kwarg/exec) | **HIGH** | P1（runtime guard 必須） |
| O-1 hook 登録 vs 検査仕様乖離 | **HIGH** | P0 即時 |
| O-3 bypass env 監査未実施 | **HIGH** | P1 |
| O-4 rc=1 fail-open の体系的設計欠落 | **HIGH** | P0 即時 |
| S-5 file_path 欠落 payload で検査スキップ | **HIGH** | P0 即時 |
| O-2 xfail strict=False の運用劣化 | **MEDIUM** | P1 |
| O-5 hook version 管理なし | **LOW** | P2 |

**P0 即時対応必須: 7 件**
**P1 対応: 4 件**
**P2 対応: 1 件**

---

## 修正提案（builder への申し送り）

1. **rc=2 fail-closed wrapper の全 PYscript 包囲**
   ```python
   try:
       <既存ロジック>
   except Exception as e:
       sys.stderr.write(f"[EXECUTOR_SYNC_GUARD] BLOCKED: unhandled error {e}\n")
       sys.exit(2)
   ```

2. **内部値 type 検査の追加**
   ```python
   if not isinstance(file_path, str): sys.exit(2)
   if not isinstance(code, str): sys.exit(2)
   ```

3. **NotebookEdit new_source / source キー fallback**
   ```python
   code = (
       tool_input.get("new_string")
       or tool_input.get("content")
       or tool_input.get("new_source")  # ← 追加
       or tool_input.get("source")      # ← 追加
       or ""
   )
   ```

4. **lambda body 内 FORBIDDEN 名 walk 検査**（_leading_callable_name の限界突破）
   ```python
   def _contains_forbidden(node):
       for sub in ast.walk(node):
           if isinstance(sub, ast.Attribute) and sub.attr in FORBIDDEN_IN_EXECUTOR:
               return sub.attr
           if isinstance(sub, ast.Name) and sub.id in FORBIDDEN_IN_EXECUTOR:
               return sub.id
       return None
   # executor.submit/map の全 args + keywords を _contains_forbidden で走査
   ```

5. **kwargs / starred / 全 args 走査**
   ```python
   targets = list(node.args) + [kw.value for kw in node.keywords]
   for arg in targets:
       if isinstance(arg, ast.Starred): arg = arg.value
       found = _contains_forbidden(arg)
       if found: violations.append(...)
   ```

6. **file_path 必須チェック**
   ```python
   if "file_path" not in tool_input: sys.exit(2)  # schema 不正は fail-closed
   ```

7. **redteam test に B-1〜B-6 の regression 追加**

---

## 結論

**判定: FAIL**

builder の自称「6件修正完了」は実質「2件半（C-01 lambda 直書き / C-06 partial 直書き / C-09 dict-level のみ）」。
NotebookEdit の new_source 完全素通りは hook 登録と検査仕様の根本乖離であり、Phase 2 でこの hook を信頼して並列化禁止を語ること自体が虚構。

**Phase 2 Sprint 0.5 P0 #7 hook はこのまま本番投入してはならない。**

P0 修正 7 件の builder 再修正 → Redteam r3 audit が必須。
xfail に逃した C-02〜C-07 と本件 P0 修正後も残る dataflow / dynamic dispatch 系は **Sprint 1 で必ず runtime guard で塞ぐ** 約束を spec_v3_20260422.md B16 に追記する形で物理化すること。

---

## 自己批判（Redteam r2）

- 前回 r1 で見逃した B-1 / B-2 / B-3 (内部値 type / NotebookEdit) は当時の bash echo 検証では再現困難だった攻撃面で、subprocess + JSON で systematic 列挙して初めて発見できた
- attack matrix を 50 件まで拡張したのは妥当だが、まだ exec/eval 系の semantic 攻撃 / file system race / pytest fixture からの bypass env 注入 / hook 自身の置換攻撃は未検査。**r3 で追加すべき**
- 本日の独立 subprocess クロスチェック（bash / sh 両方）で再現確認済 → r1 の自己批判（echo 改行エスケープ）と同種ミスは回避できた

