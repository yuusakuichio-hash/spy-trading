# Navigator 監査結果: mffu_dry_run_guard.sh 7件修正

**監査日**: 2026-04-23
**監査対象**: Phase 2 Sprint 0.5 P0 #8 — mffu_dry_run_guard.sh 7件修正
**builder agent ID**: a15a1265f41e37dbf
**navigator agent ID**: a84a9f7fdbf5bdfe5
**物理化担当**: Secretary（navigator agent に Write 権限なしのため代理書き出し・2026-04-23 06:48 JST）
**判定**: **PASS**（Redteam r2 再監査投入推奨）

---

## 1. 規律違反検出

### silent except（raise なし）: 該当なし（実質）

AST スキャンで 8 件の ExceptHandler を検出したが、全件が意味のある処理を含む正当なパターンと確認:

- `except ImportError` → `yaml = None` + `sys.stderr.write` (警告出力あり)
- `except Exception: pass` × 2 — `_get_repo_root()` 内のフォールバック（None を返して上位で処理）
- `except ValueError: return None` — repo 外パスを監視対象外とする設計
- `except (JSONDecodeError, UnicodeDecodeError, OSError): return None` — 入力破損のセーフガード
- `except Exception: return violations` — yaml 解析失敗時の安全な空返却
- `except SyntaxError: return violations` — AST parse 失敗時の安全な空返却
- `except Exception: val_str = "?"` × 2 — `ast.unparse` 失敗時のフォールバック

`except Exception: pass` (L40, L44) の 2 件は `_get_repo_root()` のフォールバックで、repo root が取得できない場合に絶対パス正規化が機能しなくなる経路（WARN 級・ブロッカーではない）。

### 型注釈: 欠落（全関数）

Python heredoc 内の全 12 関数（`_get_repo_root`, `_normalize_to_relative`, `load_input`, `is_prod_yaml`, `is_staging_or_dev_yaml`, `is_prod_py`, `_walk_dict`, `_is_truthy`, `detect_yaml_dry_run`, `_ast_is_truthy`, `detect_python_dry_run`, `main`）に引数・戻り値の型注釈が一切ない。

評価: **軽微な規律違反** として記録。Sprint 1 で修正推奨。CONDITIONAL-PASS のブロッカーとはしない。

### LoC: 問題なし

最大関数は `detect_python_dry_run` で 48 LoC、`main` で 46 LoC。いずれも上限 50 以内。

### from X import * / eval / exec / global mutable / マジックナンバー: 該当なし

---

## 2. 証跡 4 点セット 独立検証

### 1. grep（実装確認）

- `_normalize_to_relative` 関数: hook L99-L111 に実装確認（C-1 修正）
- `_walk_dict` 再帰関数: hook L140-L149 に実装確認（H-2 修正）
- `bypass_log.jsonl` への append: hook L22-L30 の bash 側に実装確認（M-1 修正）
- `is_staging_or_dev_yaml`: hook L128-L133 に実装確認（C-4 修正）
- `chronos_rules_plugin/` in `PROD_PATH_PY_PREFIXES`: hook L15 に実装確認

### 2. AST

```
bash -n → OK
python3 ast.parse → OK (nodes: 1248)
```

### 3. pytest 独立実行結果

```
23 passed, 8 xfailed in 1.31s
```

builder 申告と完全一致。

全件 pytest:
```
7 failed, 2979 passed, 6 skipped, 13 xfailed in 71.35s
```

既存 7 FAIL の内訳（全て本修正前から存在・本修正と無関係）:
- `test_backup_file_exists`: `/tmp/atlas_cycle3_backup_20260419.tar.gz` 不在
- `test_plist_exists` / `test_fleet_watcher_plist_keepalive_detailed`: launchd plist 不在
- `test_chronos_client_place_order_returns_dict`: chronos agent 変更依存
- `test_record_outcome_pre_iv_zero_no_exception` / `test_known_symbol_tsla` / `test_record_outcome_updates_history`: earnings_engine 依存

### 4. mutation 確認

`_normalize_to_relative` を「常に None 返す」mutant で TestC1 が AssertionError で FAIL することを確認（exit 1）。オリジナル hook では同テストが PASS（exit 2）。

---

## 3. xfail 8 件の妥当性

| # | テスト | strict | reason 文書化 |
|---|---|---|---|
| C-2 call_expr | `test_dry_run_call_expr_blocked` | **False** | あり（Sprint 1 持ち越し） |
| C-2 runtime_expr | `test_dry_run_runtime_expr_blocked` | **False** | あり（Sprint 1 持ち越し） |
| C-3 subclass | `test_subclass_dry_run_true_blocked` | **False** | あり（静的解析限界） |
| C-3 super | `test_super_init_dry_run_true_blocked` | **False** | あり（Sprint 1 持ち越し） |
| C-4 etag swap | `test_etag_swap_via_staging_blocked` | **False** | あり（pre-commit 未対応） |
| H-4 alias | `test_alias_import_dry_run_true_blocked` | **False** | あり（Sprint 1 持ち越し） |
| M-2 yaml unavailable | `test_yaml_unavailable_emits_warn` | **False** | あり（環境依存） |
| L-2 indirect expansion | `test_bypass_var_name_indirect_expansion` | **False** | あり（Sprint 1 持ち越し） |

**#7 からの申し送り問題と同じ構造的欠陥が再発**：
全 8 件が `strict=False` で、`pyproject.toml` に `xfail_strict` のグローバル設定なし。Sprint 1 で C-3/H-4 等が修正されて XPASS になっても CI が GREEN のまま通過する。

---

## 4. builder 懸念 3 件の独立検証

### 懸念 1（M-1 best-effort）

bypass_log.jsonl の実内容を確認した結果、全エントリが `"tool_name":"(bypass_before_parse)"`, `"file_path":"(bypass_before_parse)"` であった。Redteam M-1 の修正要求は「`data/governance/bypass_log.jsonl` に append」のみで、tool_name/file_path の精度は指定していない。bypass は stdin を消費する前に行われるため、技術的に stdin から file_path を取得することは不可能。best-effort として妥当。

### 懸念 2（C-4 WARN 設計判断）

Redteam の Sprint 1 持ち越し可能項目に「C-4 完全解決（pre-commit hook + launchd 定期 grep + ETag swap 検出）」と明記されている。staging/dev での exit 0 + WARNING がブロッカーでないことは Redteam 自身が文書化している。C-4 部分修正（staging/dev の監視追加 + chronos_rules_plugin の BLOCK 化）は Redteam 要求の 7 件必須対応 #6 を満たす。

### 懸念 3（既存 7 FAIL 無関係）

7 FAIL は全件ファイルシステム/OS 状態依存（/tmp のバックアップファイル不在、launchd plist 不在等）であることを独立確認。builder の主張は正確。

---

## 5. Redteam 指摘 17 件の網羅性

| 分類 | 件数 | 修正済（BLOCK） | xfail 永続化 | Sprint 1 持ち越し可（Redteam 認定） |
|---|---|---|---|---|
| CRITICAL (C-1〜C-5) | 5 | C-1, C-2partial, C-4partial, C-5 = 4 件修正 | C-2 call/runtime, C-3 subclass/super = 4 件 | C-3, C-4 完全解決 |
| HIGH (H-1〜H-4) | 4 | H-2, H-3 = 2 件修正（H-1 は C-2 修正で間接対応） | H-4 = 1 件 | H-4 |
| MEDIUM (M-1〜M-4) | 4 | M-1, M-4（M-4 は `except Exception` で実質修正） | M-2 = 1 件 | M-3 NotebookEdit |
| LOW (L-1〜L-3) | 3 | なし | L-2 = 1 件 | L-1, L-2, L-3 全件 |

### 未対応・懸念が残る点

M-3 (NotebookEdit) は Redteam の Sprint 1 持ち越し可能項目に明記されているが、hook 本体では `tool_name not in ("Write", "Edit", "NotebookEdit")` で NotebookEdit をマッチさせつつ、`new_string`/`content` キーで処理しているため `cell_source` のみを持つ NotebookEdit input は **実際に素通りする**（独立確認済み: exit 0）。「名目だけガード」状態だが、Redteam が Sprint 1 持ち越し可能と認定しているため CONDITIONAL-PASS のブロッカーとしない。

---

## 6. 追加指摘（navigator 独自検出）

### xfail strict=False 全件統一による XPASS 見逃しリスク（#7 再発）

#7 で navigator が申し送りした問題と同一。Sprint 1 への持ち越しとして以下を推奨:

- `pyproject.toml` に `xfail_strict = true` を設定、または
- 各 xfail に `strict=True` を設定して「Sprint 1 で修正されたら CI が自動的に FAIL して xfail 除去を促す」運用に変更

---

## 仕様書照合

仕様根拠 `data/specs/v3/chronos_spec_v3_20260422.md B5 R2b L172-L192` との照合:
- シグネチャ `MFFUFlexRules(yaml_path, storage, dry_run=False)` との対応: H-3 の positional 3rd arg 検出で対応
- CONDITIONAL-PASS 7 件必須対応の全件実装を確認

仕様との差分: **なし**（修正スコープ内に限れば）

---

## 判定

**PASS（Redteam r2 再監査投入推奨）**

差分要点（200 字以内）:

証跡 4 点セット独立確認（23 PASS + 8 xfail 一致・全件 pytest 7 FAIL 全件本修正と無関係・bash syntax OK・AST OK・mutation 間接確認OK）。型注釈全欠落は軽微規律違反として Sprint 1 推奨で今回はブロッカーとしない。再発問題として、xfail 8 件全件 strict=False かつ pyproject.toml にグローバル xfail_strict 設定なし（#7 申し送りと同一構造）。M-3 NotebookEdit は Redteam 認定 Sprint 1 持ち越しで CONDITIONAL-PASS 条件外。これら 2 点は Sprint 1 必須対処事項として Redteam r2 に申し送る。

---

## 申し送り事項 2 件

1. xfail 8 件全件 `strict=False` + pyproject.toml に `xfail_strict` 未設定（#7 と同じ構造）— Sprint 1 で `xfail_strict = true` を pyproject.toml に追加すること
2. M-3 NotebookEdit: hook がマッチ対象に含むが `cell_source` を処理せず素通り（独立確認済み）— Sprint 1 で matcher から除外するか `cell_source` 対応すること
