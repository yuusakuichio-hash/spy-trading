# Redteam 監査結果: executor_sync_only_guard.sh

**監査日時**: 2026-04-23
**監査対象**: `.claude/hooks/executor_sync_only_guard.sh` (129行) + `tests/test_executor_sync_only_guard.py` (134行・15 test PASS)
**仕様根拠**: `data/specs/v3/common_spec_v3_20260422.md` B16 L474-L482
**hook 登録確認**: `.claude/settings.local.json` L120-L127 で `Write|Edit|NotebookEdit` PreToolUse に登録済み

**判定**: **FAIL（受領不可）**

**根拠**: Phase 2 Builder が「うっかり書きそうな」攻撃 7 種（kill_switch 並列化系）+ 動的 import 攻撃 2 種 + クラッシュ fail-open 1 種 = 計 **10 件の現実的 bypass 経路** を実証。仕様 B16 L477「Kill Switch 確認は同期経路で必須実行（並列化禁止）」の物理強制を満たさない。

なお、当初 20 件 bypass を主張したが、bash echo の改行エスケープ違いを除外して python subprocess で再検証した結果、上記 10 件が真の bypass。`endswith()` パス偽装と `import asyncio.events` 系については hook が startswith match で正しく block しており、当初指摘の一部は誤検知だったことを訂正する（**Red Team 自身の自己批判**: 検証手段の精度を確保すべきだった）。

ただし真の bypass 10 件は依然として CRITICAL であり、Phase 2 Builder が現実に書く可能性が高い書き方を素通りさせるため、判定 FAIL は変わらない。

---

## CRITICAL（block 経路に穴あり・即修正要）

### C-01: lambda 包みで完全 bypass
- 攻撃: `executor.submit(lambda: kill_switch.is_active())`
- 結果: rc=0
- 原因: hook L99-L106 `_leading_callable_name` が `ast.Lambda` を扱わない（None 返却）
- 影響: Builder が「fn を呼び出し済みで渡してしまう型エラーを避けるため lambda で包む」と素朴に書くと kill_switch チェックが thread pool に逃げる
- Therac-25 (1985-87) と同型: 設計意図 (sync only) を突破する自然な書き方が放置

### C-02: 変数経由（fn = ...; submit(fn)）で bypass
- 攻撃:
  ```python
  func = kill_switch.is_active
  executor.submit(func)
  ```
- 結果: rc=0
- 原因: AST literal 探索のみ・dataflow / alias 解析なし
- 影響: 1 ステップ間接参照で完全に外れる。refactor 時の典型パターン

### C-03: rename import で bypass
- 攻撃: `from common.kill_switch import is_active as ka; executor.submit(ka)`
- 結果: rc=0
- 原因: alias 名 `ka` は FORBIDDEN_IN_EXECUTOR set に含まれない・import alias 追跡なし
- 影響: 「健全な変数名」に偽装可能 → 規律違反を意図的に隠す手段

### C-04: getattr 動的属性アクセスで bypass
- 攻撃: `executor.submit(getattr(kill_switch, 'is_active'))`
- 結果: rc=0
- 原因: 動的属性アクセスは AST literal 探索の射程外

### C-05: wrapper method で bypass
- 攻撃: `executor.submit(self._wrap_kill_switch)`（中身が kill_switch.is_active）
- 結果: rc=0
- 影響: 既存 codebase の `chronos_pre_trade_check.py` `common/pre_trade_check.py` 等は wrapper パターン多用。Phase 2 移植時に **既存パターンそのままで通る**

### C-06: functools.partial で bypass
- 攻撃: `executor.submit(partial(kill_switch.is_active))`
- 結果: rc=0
- 原因: ast.Call ネスト探索が `_leading_callable_name` で再帰追跡されない（func=ast.Name("partial") を取り出して partial で停止）

### C-07: dict lookup で bypass
- 攻撃: `fns = {"k": kill_switch.is_active}; executor.submit(fns["k"])`
- 結果: rc=0
- 原因: `ast.Subscript` ノードは _leading_callable_name で None 返却

### C-08: __import__ / importlib.import_module で asyncio bypass
- 攻撃A: `asyncio = __import__('asyncio')` → rc=0
- 攻撃B: `import importlib; asyncio = importlib.import_module('asyncio')` → rc=0
- 原因: hook の import 検査は ast.Import / ast.ImportFrom のみ・関数呼び出し経由の動的 import は射程外
- 影響: 規律遵守を装いつつ asyncio 取得可能（偽善コード）

### C-09: tool_input が dict でない場合 AttributeError → rc=1（block されない）
- 攻撃: `{"tool_name":"Write","tool_input":"evil"}`
- 結果: AttributeError → exit code 1
- 仕様: Claude Code hook は **rc=2 のみ block**・rc=1 は warning として allow
- 影響: malformed payload で hook クラッシュ → 規律違反コード通過（Boeing 737MAX MCAS 単一センサ依存「異常時 fail-open」と同型）
- hook L61 `tool_input = data.get("tool_input", {}) or {}` は None / falsy には対応するが str 型には対応していない

### C-10: SyntaxError silent skip による段階的 inject
- 攻撃: 一時的 SyntaxError コードをコミット（hook 通過）→ 次の Edit で SyntaxError だけ修正 → asyncio + kill_switch 並列化が **2 ステップで定着**
- 原因: hook L74-L77 が SyntaxError を silent skip
- 事前判定基準 PASS 条件 4 つ目「SyntaxError 時 ALLOW する理由が docstring/comment で明示」を **未充足**

---

## HIGH（運用で塞ぐ余地あり）

### H-01: keyword 引数で関数渡し
- `executor.submit(fn=kill_switch.is_active)` は L112 `if node.args:` (positional only) で素通り可能性
- 標準 ThreadPoolExecutor.submit は `submit(fn, *args)` で fn 位置のみだが、TaskExecutor Protocol ラッパー実装次第
- 修正: `node.keywords` も走査

### H-02: bypass env var の悪習化リスク
- `EXECUTOR_SYNC_GUARD_BYPASS=1` を CI / 開発 env に常時 ON する誘惑
- 現状: bypass 使用時のログ・監査機構なし
- 推奨: bypass 使用時に Pushover / log 強制（andon_multichannel.py 連動）

### H-03: PreToolUse のみで PostToolUse 監査なし
- C-01〜C-08 の bypass で書かれたコードを **後追い検出する機構ゼロ**
- 推奨: 週次 `grep -rn "executor.submit" common_v3/` AST 再検査 CI ジョブ

### H-04: contract test の自己満足
- 15/15 PASS は Blue Team の自信の根拠だが、本監査で 10 種 bypass 実証
- 「テスト通ったから安全」は Knight Capital 型の事故文化（Normalization of Deviance）
- 「自分のテストで自分を保証する自家撞着」（feedback_independent_verification_mandatory.md）の典型

---

## MEDIUM

### M-01: huge stdin で OOM / timeout
- 巨大 new_string で ast.parse が CPU spike → hook timeout → block 失敗の可能性

### M-02: tempfile race
- L18 `INPUT_FILE=$(mktemp)` の短い窓・symlink race 攻撃価値は低い

### M-03: starmap 等の並列化メソッド未網羅
- L111 `if method in ("submit", "map"):` のみ
- ProcessPoolExecutor の executor.shutdown 後再作成等は射程外

### M-04: tool_name lowercase 偽装（Claude Code 仕様信頼前提なら実害低）

---

## LOW

### L-01: spec ref hardcode（日付込み）
- 仕様改訂時にメッセージに古い日付残存

### L-02: error message に修正案なし
- BLOCKED 時に具体的リファクタ案を提示しない → Builder が回避策を探して bypass を「発見」する恐れ

---

## 戦略的指摘

### S-01: 「AST literal pattern match」アプローチの根本的限界
- 本 hook は「禁止文字列を AST 上で見つける」ブラックリスト方式
- 構造的に dataflow / alias / 動的属性アクセスに無力（プログラム解析の基礎）
- **本質的解決**: kill_switch / idempotency 関数を `@sync_only` デコレータで物理マークし、デコレータ内部で `threading.current_thread() is threading.main_thread()` を検証する **runtime guard** に切替えるべき
  - Builder のコード書き方に依存しない
  - lambda 包んでも partial かましても thread 違反で必ず例外
  - LTCM 1998（モデル過信）と同型の「事前検査だけで安全と信じる」誤りからの脱却

### S-02: hook 単独では Phase 2 を守りきれない
- AST hook は予防の 1 層（Defense in Depth の最外層）
- Sentinel daemon (`scripts/dead_man_switch.py`) と連動した runtime 監査が必要
- 現状は Challenger 1986 O-ring と同じ「単一防御依存」構造

### S-03: Red Team 自身の自己批判
- 当初 bash echo で改行エスケープを誤り、20/20 bypass と過大主張
- python subprocess で再検証して 10 件に修正
- 教訓: 検証手段の精度を **必ず複数手段でクロスチェック** すべき
- それでも真の 10 件は CRITICAL のため判定 FAIL は変わらない

### S-04: settings.local.json への登録は確認済
- L120-L127 で `Write|Edit|NotebookEdit` matcher 登録確認
- 各 hook 独立実行のため legacy_write_block / mffu_dry_run_guard との順序衝突なし

---

## Phase 2 Sprint 1 着手前修正必須項目

優先順:

1. **C-09 修正** (5 min): tool_input が dict でない場合の type check 追加 + 例外時 sys.exit(2) (block・fail-closed) で異常時も止める
2. **C-10 docstring 追加** (2 min): SyntaxError silent skip の理由を hook 内 comment で明記、または fail-closed 化
3. **C-01〜C-08 構造的修正** (要設計判断・1〜2 hour):
   - 即時パッチ: `_leading_callable_name` を ast.Lambda / ast.Subscript / partial 等の Call ネストも追跡（B-02/B-04/B-06/B-07 緩和）
   - 即時パッチ: import alias 解析を追加し alias 名から元名を解決（C-03 緩和）
   - 即時パッチ: `__import__('asyncio')` / `importlib.import_module('asyncio')` の文字列引数を AST 検査（C-08 緩和）
   - 構造的解決: S-01 のデコレータ + runtime guard 方式へ切替（C-01〜C-05 抜本対策）
4. **redteam test 永続化** (10 min): 本監査の 10 攻撃を `tests/test_executor_sync_only_guard_redteam.py` に追加（Phase 2 sprint 1 着手前に全 BLOCK 確認）
5. **runtime guard 設計** (Phase 2 Sprint 1 並走可): kill_switch.py に `@sync_only` デコレータ実装 + Phase 2 Builder 規約化

---

## Phase 2 Sprint 1 持ち越し可能項目（優先度低）

- H-04: 既に test 永続化で対応すれば緩和
- M-01 (huge stdin): 実害遠い
- M-02 (tempfile race): 攻撃価値低
- L-01 / L-02: cosmetic

---

## 重症度評価

- **CRITICAL**: 10件（C-01〜C-10）→ 受領不可
- **HIGH**: 4件
- **MEDIUM**: 4件
- **LOW**: 2件

**総合**: 仕様 B16 L474-L482 の物理強制要求を満たさず、Phase 2 Builder の典型書き方で容易に bypass 可能。**FAIL**。

最低 C-09/C-10 修正 + redteam test 永続化（合計 ~30min）で **CONDITIONAL-PASS** へ昇格可能。完全 PASS には S-01 の runtime guard 設計切替が必要。

---

## 引用古典事例

- **Knight Capital (2012)**: $440M / 8min。デプロイ不整合で old code path が動いた → 本 hook は old kill_switch 並列化を物理停止できないため同型事故予防不可
- **Therac-25 (1985-87)**: race condition + UI で setup 時の安全機構 bypass → lambda/partial bypass は同質
- **Boeing 737MAX MCAS (2018-19)**: 単一センサ依存・異常時 fail-open → AttributeError → rc=1 → allow は同型
- **Challenger O-ring (1986)**: 単一防御依存・「これまで動いた」過信 → 15/15 test PASS で安全と信じる Blue Team 文化
- **Chernobyl (1986)**: 試験時の安全装置無効化 → bypass env var の運用悪化リスク

