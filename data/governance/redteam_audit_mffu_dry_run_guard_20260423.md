# Redteam 監査結果: mffu_dry_run_guard.sh

**監査日**: 2026-04-23
**監査対象**: `/Users/yuusakuichio/trading/.claude/hooks/mffu_dry_run_guard.sh` (154行)
**仕様根拠**: `data/specs/v3/chronos_spec_v3_20260422.md` B5 R2b L172-L192
**監査者**: Redteam (Claude Opus 4.7 別 session)

---

## 判定: **FAIL**

**根拠**: 17 件の攻撃ベクトルを実投入した結果、**12 件が素通り**。仕様 L172-L192 が要求する「物理強制」は外形上動くが、防御として穴だらけ。特に **絶対パス bypass (U)** と **Python AST 検出の自明な回避 (F-K)**、**positional ではない subclass override (R)**、**`config/staging/` 経由 ETag swap 攻撃 (N)** の 4 つは Phase 2 Builder が「悪意なく」書く可能性すらあり、CONDITIONAL-PASS にも届かない。contract test 不在で regression 検知も不可能。

事前判定基準の FAIL 条件「spec の 2 要件が現実に通る」「Phase 2 Builder の典型的書き方素通り」の両方に該当。

---

## CRITICAL

### C-1: 絶対パスで全プレフィックス検査を 100% bypass
- 攻撃: `file_path = "/Users/yuusakuichio/trading/config/prod/x.yaml"` で `dry_run: true` を Write
- 結果: EXIT=0 (素通り)
- 原因: `is_prod_yaml` / `is_prod_py` が `file_path.startswith("config/prod/")` 等の **相対 prefix string match** のみ
- 重大性: **CLAUDE.md の最上位環境注記** "you should only use absolute file paths" と矛盾。Claude Code Tool が file_path を絶対パス渡しした瞬間、ガードが完全無効化する。これは「典型的書き方」で日常的に再現する
- 歴史的類比: **Knight Capital (2012)** のデプロイ不整合と同型。ガードは存在するが起動 path が違うので呼ばれない。$440M損失の構造そのもの
- 修正: `Path(file_path).resolve().relative_to(repo_root)` で正規化後に prefix 判定

### C-2: Python AST 検出は constant True kwarg しか見ない (6 種素通り)
- 全て EXIT=0 で素通り:
  - F: `MFFUFlexRules(**{"dry_run": True})` (dict unpack)
  - G: `cfg = {"dry_run": True}; MFFUFlexRules(**cfg)` (variable unpack)
  - H: `MFFUFlexRules(dry_run=bool(1))` (Call expression)
  - I: `MFFUFlexRules(dry_run=os.getenv("DR")=="1")` (runtime expr)
  - J: `cls = MFFUFlexRules; cls(dry_run=True)` (alias 経由)
  - K: `class MyMFFU(MFFUFlexRules): pass; MyMFFU(dry_run=True)` (subclass)
- 原因: `detect_python_dry_run_true` が
  1. `func.id` または `func.attr` が `"MFFUFlexRules"` 文字列一致のみ (alias / subclass 検出不能)
  2. `kw.value` が `ast.Constant and value.value is True` のみ (任意の Python 式回避可能)
- 重大性: 静的解析である以上完全捕捉は無理だが、**最低限 Truthy な Constant (1, "true", "yes") と Name="True"** は捕まえる必要。現状は AST 入門レベル
- 歴史的類比: **Therac-25 (1985-87)** の入力 sanitization 漏れ。「想定したパターンしか弾かない」典型

### C-3: subclass `super().__init__(..., dry_run=True, ...)` 素通り
- 攻撃 Q': `class MyMFFU(MFFUFlexRules): def __init__(self, *a, **kw): super().__init__(*a, dry_run=True, **kw)`
- 結果: EXIT=0
- 原因: `super().__init__` は Call(Attribute(Call(Name("super")), "__init__")) で `name = "__init__"`、target_class 一致せず
- 重大性: spec の TacticBase 拡張が想定する継承パターンと真っ向衝突。Builder が「dry_run を内包したカスタムルール」を作る最も自然な書き方が素通る
- 修正: `super().__init__` 呼び出しは「scope のクラスが MFFUFlexRules を継承していれば」検出対象に加える (静的解析で完全には無理だが best-effort)

### C-4: `config/staging/` `config/dev/` `chronos_rules_plugin/` `tests/` が完全監視外 (ETag swap 攻撃成立)
- N: `config/staging/x.yaml` に `dry_run: true` 書き込み → EXIT=0
- L: `tests/test_x.py` に `MFFUFlexRules(dry_run=True)` → EXIT=0 (これは fixture 名目で正当化される逃げ道)
- M: `chronos_rules_plugin/mffu_flex.py` (実装本体) に `dry_run=True` ハードコード → EXIT=0
- 攻撃シナリオ:
  1. Builder が staging で `dry_run=True` を書き込む (ガード通過)
  2. デプロイスクリプトが `config/staging/*.yaml` を `config/prod/*.yaml` にシンボリックリンク or rsync で本番化
  3. ガードは「Write 時点」しか見ないので **既存ファイルが本番 path に "出現" する経路は完全盲点**
- 歴史的類比: **Knight Capital** 8 サーバ中 1 台に旧コード残存と同型。ガードが「書き込み時のみ」しか効かない構造的欠陥
- 重大性: **Phase 2 で必ず起こる**。仕様 R2b の C-10 再発封鎖は達成できない

### C-5: contract test 不在 (regression 検知不能)
- `tests/test_mffu_dry_run_guard.py` 不在 (ls 確認済)
- 結果: 上記 C-1〜C-4 の修正後も、誰かが hook を「壊す」commit を書いても CI で検知不能
- 規律違反: `feedback_schema_contract_test_mandatory.md` 「CI組込必須」直接違反
- 重大性: 物理ガードの最低条件は「ガード自体が壊れたら即座に bell が鳴る」こと。今は無音

---

## HIGH

### H-1: yaml `dry_run: 1` (整数) 素通り
- yaml.safe_load は `1` を int で返す。hook の `doc.get(dry_run_kwarg) is True` で False
- 攻撃: `dry_run: 1` を prod yaml に書く → EXIT=0
- ただし PropFirmRules 実装側が bool() キャストすれば runtime で True 化する可能性あり (要 builder 側仕様確認)
- 修正: `is True or value == 1 or (isinstance(value, str) and value.lower() in ("true","yes","on","1"))`

### H-2: nested yaml キー (`prop.mffu.dry_run: true`) 素通り
- D: `prop:\n  mffu:\n    dry_run: true` → EXIT=0
- 原因: `doc.get(dry_run_kwarg)` がルート直下しか見ない
- 重大性: 実際の prop_rules yaml は階層化されている (`/Users/yuusakuichio/trading/data/prop_rules/mffu_flex.yaml` 等)。**実本番ファイル形式と一致しない** = ガード設計が現実 yaml schema を見ていない
- 修正: 再帰的にキー検索 (`walk_dict` で全 dry_run キーを拾う)

### H-3: positional 第3引数 `MFFUFlexRules(yaml_path, storage, True)` 素通り
- O: positional True → EXIT=0
- 仕様 L172: `def __init__(self, yaml_path: Path, storage: StorageBackend, dry_run: bool = False)` → 3rd positional で True を渡せる
- 重大性: spec 通りに書いた `MFFUFlexRules(p, s, True)` がガードを通る
- 修正: `node.args` の index 2 (= 3rd positional, self は instance method 呼び出しでは省略) も検査

### H-4: `from x import MFFUFlexRules as Rules` の alias import 素通り
- 攻撃 (再現済): `from chronos_rules_plugin.mffu_flex import MFFUFlexRules as Rules; Rules(dry_run=True)` → EXIT=0
- 原因: AST が呼び出し時の identifier を見るだけ。import alias の解決はしない
- 修正: `ast.ImportFrom` で alias map を構築し、Call 時にそれを参照

---

## MEDIUM

### M-1: bypass 環境変数の使用ログがゼロ
- `MFFU_DRY_RUN_GUARD_BYPASS=1` の使用履歴が memory/ data/governance/ に書かれない
- bypass されても監査痕跡なし → 「気付かないうちに恒常 ON」リスク
- 比較: `legacy_write_block.sh` 等の既存ガードはログを書いているか? 要統一
- 修正: bypass 検出時に `data/governance/bypass_log.jsonl` に append (timestamp / file_path / tool_name)

### M-2: yaml モジュール ImportError 時 silent skip
- `try: import yaml except ImportError: yaml = None` で yaml=None なら detect_yaml_dry_run_true は空配列
- 「yaml not installed」環境では yaml 検査が完全無音 → false negative
- 仕様で許容理由が文書化されていない
- 修正: import 失敗時は `sys.stderr.write("[WARN] yaml not available, yaml check skipped\n")` で痕跡残す

### M-3: NotebookEdit の content/new_string 構造未対応
- T: NotebookEdit に対し EXIT=0 (素通り)
- NotebookEdit の tool_input は `cell_source` 等 Notebook 固有キー。hook は `new_string` `content` しか見ない
- 重大性: matcher に NotebookEdit が含まれるが実装で見ていない = 名目だけ
- 修正: NotebookEdit を matcher から外す or `cell_source` 対応

### M-4: yaml 解析時 `yaml.YAMLError` 以外の例外で crash 可能性
- `yaml.safe_load_all(code)` がジェネレータを返すため、list() 展開時に例外発生
- 現コードは `yaml.YAMLError` のみ catch。`yaml.constructor.ConstructorError` 等は Builder の意図的な複雑型 yaml で raise → hook が hard fail (EXIT=1) する
- 修正: `except yaml.YAMLError` を `except Exception` に広げる (silent skip より hard fail の方が良いが、現状中途半端)

---

## LOW

### L-1: spec_ref が hardcode (relative path)
- spec ref が `data/specs/v3/...` 相対パス。spec ファイルが移動した瞬間死に link
- 修正: `data/specs/INDEX.md` 経由参照

### L-2: BYPASS_VAR_NAME 変数定義が未使用
- L10 で定義された `BYPASS_VAR_NAME` を L17 で文字列リテラル `"MFFU_DRY_RUN_GUARD_BYPASS"` で再記述
- 二重管理 → 変名時の不整合源
- 修正: `if [ "${!BYPASS_VAR_NAME:-0}" = "1" ]` で indirect expansion

### L-3: prefix tuple に `chronos_v3/` あるが実装は `chronos_rules_plugin/` (実 path)
- 仕様で `chronos_v3/` 配下が新規実装 path だが、現実の MFFUFlexRules は `chronos_rules_plugin/mffu_flex.py` (旧 path)
- ガードは新 path しか見ない → 実装が旧 path にある間は完全盲点
- これは仕様と実装の path 移行戦略の問題でもある

---

## 戦略的指摘

### S-1: 「文字列マッチ + AST 入門」の防御モデルは Phase 2 で破綻する
Phase 2 で TacticBase / PropFirmRules の継承階層が増える前提で、現状の hook は「文字列で見る」しかしていない。**型情報・継承関係・import alias を解決しない静的解析は、Phase 2 の規模では穴だらけになる**。

代替案: **mypy plugin 化** または **runtime guard を `MFFUFlexRules.__init__` に dunder で埋め込み** (`if dry_run and os.getenv("ENVIRONMENT")=="prod": raise RuntimeError`)。後者は静的解析の限界を超え、実行時に確実に止まる。

### S-2: Defense in depth の階層が不足
現在は「PreToolUse 1 層」のみ。仕様 R2b は本来:
1. **PreToolUse hook** (この hook、書き込み時)
2. **CI test** (contract test、push 時) ← 不在
3. **起動時 guard** (`if mode=='dry_run' and ENV=='prod': sys.exit(1)`) ← 仕様要求だが実装未確認
4. **runtime sentinel** (heartbeat で dry_run 状態を 1min 毎に EICAS 報告) ← 仕様未言及

の 4 層であるべき。**1 層しかない時点で C-10 再発封鎖は不可能**。Phase 2 Sprint 1 で 3, 4 を追加しないと「仕様としての R2b は未完」。

### S-3: Normalization of Deviance 圧力
- 「とりあえず staging で dry_run=True」 → 「staging で動いたから prod に rsync」 → 「ガードは Write 時しか見ないからすり抜け」
- これは **Challenger O-ring (1986)** の温度逸脱と同じ構造。「過去に問題なかった」を理由に基準が緩む
- ガードは「書き込み一点」しか押さえないので、ファイル移動・コピー・symlink・git mv で迂回される
- 修正: `pre-commit hook` でも同検査を実行 (git stage 時)、`launchd` で `find config/prod -name "*.yaml" -exec grep -l "dry_run.*true"` 定期実行

### S-4: 「片輪解消」評価の解釈ミス可能性
同日の `redteam_audit_mffu_flex_20260423.md` で「片輪解消」評価とのことだが、**この hook は yaml 側の片輪を解消するものではなく、別軸 (書き込み時 path 検査) の話**。yaml 仕様自体の問題 (silent default 等) は本 hook では解決できない。「mffu_flex の問題が解消した」と誤読されないよう Sprint 0.5 のレポートで明示要

---

## Sprint 0.5 Day 2 必須対応 (CONDITIONAL-PASS への到達条件)

優先順位:

1. **[CRITICAL] C-1 修正**: 絶対パス→相対 path 正規化 (`Path.resolve().relative_to(repo_root)`)
2. **[CRITICAL] C-5 修正**: `tests/test_mffu_dry_run_guard.py` 新規作成 (最低 17 攻撃ベクトル全部 + 正常系 3 件 = 20 ケース)
3. **[CRITICAL] C-2 部分修正**: AST に少なくとも以下追加
   - `kw.value` の Constant value が Truthy (1, "true", "yes" 文字列) も検出
   - `kw.value` が `ast.Name(id="True")` も検出 (Py3 では Constant だが defensive)
   - **kwargs unpack (`**dict`) 検出時は警告 (best-effort 「dry_run キーを含む可能性」)
4. **[HIGH] H-2 修正**: nested yaml キーの再帰検索
5. **[HIGH] H-3 修正**: positional index 2 検査
6. **[CRITICAL] C-4 部分修正**: `config/staging/` と `chronos_rules_plugin/` を監視 path に追加
7. **[MEDIUM] M-1**: bypass 使用時 `data/governance/bypass_log.jsonl` 記録

これら 7 件全て + contract test 通過で **CONDITIONAL-PASS**

---

## Phase 2 Sprint 1 持ち越し可能項目

- C-2 の完全解決 (mypy plugin 化 or runtime guard)
- C-3 subclass 検出 (静的解析では限界、runtime guard で代替)
- C-4 完全解決 (pre-commit hook + launchd 定期 grep + ETag swap 検出)
- S-2 Defense in depth 4 層化
- M-3 NotebookEdit 対応 (現状不要であれば matcher から外す)
- L-1〜L-3 軽微改善

---

## 既知事項対応

- 同日 mffu_flex.yaml Redteam 「片輪解消」: 本 hook は yaml 仕様問題を解消しない (S-4 参照)。**両者は独立した別軸の防御** であることを Sprint 0.5 レポートに明記必要
- contract test 不在: 既知の通り。Day 2 必須対応 #2 で解消
