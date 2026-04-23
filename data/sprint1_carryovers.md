# Phase 2 Sprint 1 持ち越し項目

**最終更新**: 2026-04-23
**目的**: Sprint 0.5 で「Sprint 1 で塞ぐ」と明示した未解決項目を忘却防止のため物理記録

---

## C-001: executor_sync_only_guard 抜本対策（runtime guard 化）

**起票**: 2026-04-23（Sprint 0.5 Day 1 / Redteam r2 audit FAIL 後）

**背景**:
- Sprint 0.5 P0 #7 hook (`executor_sync_only_guard.sh`) は AST 静的解析で実装
- Redteam r1 = FAIL（10件 bypass）→ builder 修正で 6 件 BLOCK + 5 件 xfail 永続化 → navigator PASS → Redteam r2 = FAIL
- Redteam r2 新発見:
  1. NotebookEdit が new_source/source キー無対応で完全素通り
  2. C-09 修正は dict 型のみ・内部値の AttributeError で rc=1 fail-open
  3. lambda body / kwargs / `__import__` 動的引数で 14件以上の bypass 残存
- AST 静的解析の限界が露呈（dataflow / alias / 動的属性アクセスは原理的に追えない）

**Sprint 1 で必須実装**:

### 1. `@sync_only` デコレータ + runtime guard
- 対象関数: `common/kill_switch.py` の `is_active`, `activate`, `deactivate`, `check_kill_switch`
- 対象関数: idempotency 系の `check_and_mark` 等
- 実装方針:
  ```python
  import threading
  from functools import wraps

  def sync_only(fn):
      @wraps(fn)
      def wrapper(*args, **kwargs):
          if threading.current_thread() is not threading.main_thread():
              raise RuntimeError(
                  f"{fn.__name__} called from non-main thread "
                  f"({threading.current_thread().name}). "
                  "Sync-only contract violation."
              )
          return fn(*args, **kwargs)
      return wrapper
  ```
- これで C-01〜C-08 全て runtime で必ず止まる（AST hook の補助に格下げ）

### 2. AST hook の補完修正（runtime guard と二重防御）
- C-09 完全 fail-closed 化: `tool_input` 内部値も型検査
- NotebookEdit の new_source / source キー対応 or matcher から外す
- xfail test の `strict=True` 化（XPASS 時に CI 落とす）

### 3. Redteam r2 が指摘した H-01（keyword 引数 bypass）対応
- `node.keywords` も走査して `submit(fn=kill_switch.is_active)` を検出

**Sprint 1 完了基準**:
- Redteam r3 audit で PASS（FAIL から 2 段階上げ）
- xfail を strict=True にして全 PASS（XPASS なら CI 落ちる）

**関連証跡**:
- `data/governance/redteam_audit_executor_sync_guard_20260423.md` (r1)
- `data/governance/navigator_audit_executor_sync_guard_20260423.md`
- `data/governance/redteam_audit_executor_sync_guard_20260423_r2.md` (r2)

---

---

## C-002: xfail_strict 全件 strict=False 構造的欠陥（#7 + #8 共通）

**起票**: 2026-04-23（#7 navigator 申し送り → #8 で再発）

**問題**:
- `tests/test_executor_sync_only_guard_redteam.py` の xfail 5 件全件 `strict=False`
- `tests/test_mffu_dry_run_guard.py` の xfail 8 件全件 `strict=False`
- `pyproject.toml` に `xfail_strict = true` グローバル設定なし
- → Sprint 1 で runtime guard / AST 強化等で実際に修正されても XPASS が CI で見逃され、古い xfail が残置される

**Sprint 1 で必須実装**:
- `pyproject.toml` の `[tool.pytest.ini_options]` に `xfail_strict = true` 追加
- または各 xfail に `strict=True` を個別設定
- 効果: Sprint 1 で C-02/C-03/C-04/C-05/C-07/C-3/H-4 等が実際に塞がったら CI が自動的に FAIL → 古い xfail マーカー除去を強制

---

## C-003: mffu_dry_run_guard NotebookEdit cell_source 素通り

**起票**: 2026-04-23（#8 navigator 申し送り）

**問題**:
- hook の matcher が NotebookEdit を含むが、tool_input の処理は `new_string`/`content` キーのみ
- NotebookEdit の `cell_source` キーを処理せず、現実に素通り（独立確認済み: exit 0）
- 「名目だけガード」状態

**Sprint 1 で必須実装**:
- 選択肢 A: `cell_source` キー対応（新コード追加）
- 選択肢 B: matcher から NotebookEdit を除外（hook 設定改修）
- 推奨: B（NotebookEdit は Bot 開発で使用頻度低・hook 範囲を絞る方が堅実）

---

---

## C-004: mffu_dry_run_guard 抜本対策（runtime guard 化）

**起票**: 2026-04-23（Sprint 0.5 Day 1 / Redteam r2 audit FAIL 後・案 B' 採用）

**背景**:
- Sprint 0.5 P0 #8 hook (`mffu_dry_run_guard.sh`) は AST 静的解析で実装
- Redteam r1 = FAIL（17件中12件素通り）→ builder 7 件修正 → navigator PASS → Redteam r2 = FAIL
- Redteam r2 新発見:
  1. **fake repo root 攻撃**: hook を別 dir コピー + cwd で `git rev-parse` 失敗 → `__file__` fallback で REPO_ROOT 偽装 → 実 prod path 書き込み素通り（Knight Capital 型）
  2. **AST 14+ 新規 bypass**: `*[p,s,True]` / `not False` / `1==1` / `dry_run=2` / `dry_run=[1]` / 4th positional / `*args` 展開 / `config/preprod/` `config/canary/` `config/production/` / `chronos_rules_plugin/*.yaml` / NotebookEdit / symlink / 大文字 `Config/Prod/`
  3. **3 構造的欠陥**: spec L190 起動時 guard 完全未実装 + bypass log permission error で silent skip + navigator audit 報告ファイル物理不在
- AST 静的解析の限界が露呈（#7 と同じ）

**Sprint 1 で必須実装**:

### 1. spec L190 の起動時 runtime guard を MFFUFlexRules class に実装
- 場所: `chronos_v3/prop/mffu_flex.py`（Phase 2 で新規実装）
- 実装方針:
  ```python
  class MFFUFlexRules:
      def __init__(self, yaml_path, storage, dry_run=False):
          if os.environ.get("ENVIRONMENT") == "prod" and dry_run:
              from common.kill_switch import activate
              activate(reason="MFFU dry_run mode in prod environment")
              raise SystemExit(
                  "[FATAL] MFFUFlexRules(dry_run=True) is forbidden in prod"
              )
          # ...
  ```
- これで AST hook の bypass 14+ 件は全て runtime で必ず止まる

### 2. AST hook の補強修正（runtime guard と二重防御）
- fake repo root 攻撃対策: `git rev-parse` 失敗時は安全側に倒す（fallback でも repo 内 path 厳格判定 / または fail-closed）
- bypass log: permission error 時 fallback path（`/tmp/bypass_log_*.jsonl`）+ stderr 警告
- NotebookEdit を matcher から除外（Bot 開発で使用頻度低）
- staging/dev WARN を BLOCK に格上げ（Normalization of Deviance 防止）
- 大文字パス対応: `Path.resolve()` 後に `.lower()` 比較 or macOS case-insensitive FS 検知

### 3. spec drift watcher 起動
- spec L155 の「spec_drift watcher が以後の変更を自動検知」が未実装
- yaml schema 変更を 24h scan + git blame 照合 CI

**Sprint 1 完了基準**:
- Redteam r3 audit で PASS（FAIL から 2 段階上げ）
- xfail を strict=True にして全 PASS（XPASS なら CI 落ちる）
- runtime guard が prod 環境で必ず raise することを E2E test で確認

**関連証跡**:
- `data/governance/redteam_audit_mffu_dry_run_guard_20260423.md` (r1)
- `data/governance/navigator_audit_mffu_dry_run_guard_20260423.md`（※ Redteam r2 が「物理不在」と指摘・要確認）
- `data/governance/redteam_audit_mffu_dry_run_guard_20260423_r2.md` (r2)

---

---

## C-005: CircuitBreaker frozen design 抜本対策（ADR-008）

**起票**: 2026-04-23（Sprint 0.5 Day 1 / Redteam #3 audit FAIL 後・案 D 採用）

**背景**:
- Sprint 0.5 P0 #3 CircuitBreaker は ADR-007（runtime guard 主防御）で実装
- Redteam #3 audit で「runtime guard が __init__ 単一関所依存」と判定（CRITICAL 2 / HIGH 4 / MEDIUM 4 / LOW 2）
- 6 経路で BYPASS 実測:
  1. `__new__` + `__dict__["_auto_recovery"] = True`（__init__ skip）
  2. post-init 代入（`cb._auto_recovery = True`）
  3. pickle round-trip
  4. copy.deepcopy
  5. subclass `__init__` 完全 override
  6. module monkey-patch / sys.modules 差替え
- 3 度目の同型失敗パターン（#7/#8 = AST 限界 / #3 = runtime guard 限界）

**Sprint 1 で必須実装**（ADR-008 採用案 A）:

### 1. CircuitBreaker frozen design
- `__slots__` 必須化
- property setter 全 raise（`cb._auto_recovery = True` で AttributeError）
- `__init_subclass__` で subclass `__init__` override 禁止
- pickle `__reduce__` override（pickle 不可能化）
- `__new__` 監視（_INIT_REQUIRED_KEY パターン）

### 2. 名義インスタンス module 化（spec B14 L382-L383 完全実装）
- `common_v3/self_healing/instances.py` に `tradovate_breaker` / `moomoo_breaker` 定義
- 利用側は instances.py から import 必須
- 直接 instantiation を hook で WARN

### 3. sys.modules 監視（runtime sentinel）
- `common_v3/self_healing/sentinel.py` で module integrity 検証
- monkey-patch 検出時 raise

### 4. 同型 frozen design を KillSwitch / IdempotencyStore にも適用
- ADR-008 設計指針を全 critical class に展開

### 5. approver 検証強化
- NFKC 正規化
- whitelist 方式（`{"yuusaku"}` のみ許可）
- str subclass 禁止（`type(approver) is str` チェック）

**Sprint 1 完了基準**:
- Redteam r2 audit で PASS（FAIL から 2 段階上げ）
- 6 BYPASS 経路全て raise 確認

**関連証跡**:
- `data/governance/redteam_audit_circuit_breaker_20260423.md` (FAIL)
- `data/governance/navigator_audit_circuit_breaker_20260423.md`
- `data/decisions/ADR-007-circuit-breaker-runtime-guard-first.md`（判断ミス）
- `data/decisions/ADR-008-frozen-design-final-enforcement.md`

---

## C-006: selective_test_detector hook 残存 CRITICAL（Redteam r4）

**起票**: 2026-04-23（Sprint 0.5 Day 2 / 朝の user 発言ロスト根治後・Redteam r4 audit 後）

**背景**:
- Sprint 0.5 Day 2 朝に `.claude/hooks/selective_test_detector.sh` の誤検出で
  ゆうさくさんの user 発言が 12 回連続 HARD BLOCK され発言ロスト発生
- 根治として scope narrowing (raw user event を境界) を導入
- Redteam r1 → r4 まで 4 サイクル audit → C1/C2/C3 は塞いだが r4 で新規 C4/C5 残存

**Sprint 1 持ち越し理由**:
- C4/C5 はいずれもエッジケース（10000 行超 compact 後 session / `pytest tests` 末尾 `/` なし）
- Sprint 1 で runtime guard 化（C-001/C-004 と同枠）に合流させれば同時解消が期待できる
- 今 AST hook で個別塞ぎは二重作業リスク・バグなし最優先規律と衝突

### C-006-1: 10000 行上限で境界未検出時の退化
- 現状: `lines[-10000:]` 上限 → 境界が 10001 行以上前にあると `_last_u=-1` → `lines[-200:]` fallback で scope narrowing 意図崩壊
- Sprint 1 対応: 境界未検出時 **fail-open (exit 0)** へ統一（no-transcript mode と同じ思想）
- または transcript 全行 scan（メモリ負荷評価必要）

### C-006-2: full pytest 正規表現の甘さ
- 現状: `pytest\s+tests/\s*($|-)` → `pytest tests`（末尾 `/` なし）や `pytest tests/ && ...` を full 認識せず
- Sprint 1 対応: 正規表現拡張 `pytest\s+tests/?\s*($|-|&)` 等で cover

**Sprint 1 完了基準**:
- Redteam r5 audit で NO CRITICAL
- runtime guard 化と同時に AST hook は補助に格下げ（ADR-007 路線）

**関連証跡**:
- `data/logs/selective_test_violations.log`（朝の 12 回連続 BLOCK 実ログ）
- TestSelectiveTestDetector 9/9 PASS（`tests/test_guard_hooks.py`）
- Redteam r3/r4 audit summary（会話ログ保持・個別レポート未生成）

---

## C-007: Deadman library ハードニング + spec path 乖離解消

**起票**: 2026-04-23（Sprint 0.5 Day 2 / #4 Deadman path 完了時・Navigator CONDITIONAL-PASS 条件 + Redteam HIGH 4 件）

**背景**:
- Sprint 0.5 Day 2 #4 Deadman path で `common_v3/observability/deadman.py` を新規実装・`scripts/dead_man_switch.py` を委譲化
- Navigator: CONDITIONAL-PASS（spec path 乖離の追跡条件）
- Redteam: NO CRITICAL / HIGH 4 件（うち新規 regression は 2 件）

### C-007-1: spec L320 beacon path 乖離解消（Navigator CONDITIONAL 条件）
- spec: `data/state_v3/deadman/*.beacon`
- 実装: 既存 `data/ops/heartbeat/dead_man_ping.jsonl` を継承（既存 LaunchAgent 互換）
- Sprint 1 対応: `data/specs/v3/common_spec_v3_20260422.md` L320 を現実追従で修正

### C-007-2: write_beacon に fsync/flock 追加（Redteam H1 / B4）
- 現状: `"a"` mode write・fsync なし・lock なし
- リスク: SIGKILL / launchd timeout で行破損 → 次回 JSONDecodeError → `get_last_ping` None 返却 → CRIT 誤報連打
- Sprint 1 対応: `fcntl.flock` + `os.fsync(fd)` or atomic tmp-rename

### C-007-3: lib 側 PING_FILE rotation 実装（Redteam H2 / B3・新規 regression）
- 現状: lib に rotate なし（既存 scripts 側にのみ）
- リスク: lib 経由で beacon を書き続けると JSONL 永久成長 → `get_last_ping` 全件 readlines → 数ヶ月で OOM
- Sprint 1 対応: lib に `_rotate_ping_file()` 相当を追加

### C-007-4: COMPONENTS 二重定義の統合（Redteam H3 / S6・新規 regression）
- 現状: `scripts/dead_man_switch.py` L53 と `common_v3/observability/deadman.py` L41 で別々に定義
- リスク: 片方更新漏れで CLI / LaunchAgent / Bot 呼出の監視対象が分裂
- Sprint 1 対応: scripts 側を `from common_v3.observability.deadman import COMPONENTS` で import し二重定義排除
- 既存テスト `test_chronos_audit_critical_20260422.py:319` の参照先も lib に揃える

### C-007-5: Pushover 失敗時の多経路化（Redteam H4 / S3 / R1）
- 現状: `_send_alert` が `except Exception` 握り潰し・fallback log のみ
- リスク: Pushover サーバ障害時に silent failure（Therac-25 型）
- Sprint 1 対応: `.claude/hooks/andon_multichannel.py` との統合で ntfy / email / Pushover の 3 経路必須化

### C-007-6: scripts 残骸の片付け（Redteam B1/B2/B3・LOW）
- `scripts/dead_man_switch.py` L41 `pushover_send` 未使用 import
- L112-132 `_read_last_ping` 死コード（lib 側 `get_last_ping` 使用中）
- Sprint 1 対応: 3 日 shadow 完了後の deprecated stub 化と同時に片付け

### C-007-7: SORA_TRADING_DIR 空文字での cwd 分岐（Redteam S5・MEDIUM）
- lib の default が str・scripts の default が Path で env 未設定時は同値だが env="" で lib のみ `Path("")`=cwd に分岐
- Sprint 1 対応: 両方で default 型を統一 + env 空文字判定で `_PROJECT_ROOT` に落とす

### C-007-8: CRIT 後の自動停止責務（Redteam O3）
- 現状: Dead Man's Switch が Pushover P2 送信のみ・自動停止なし
- Sprint 1 対応: `common.kill_switch.activate()` 連動（ALL_BOTS_DOWN 検知で全 Bot 停止）

**Sprint 1 完了基準**:
- Redteam r2 audit で HIGH すべて CLOSED
- 3 日 shadow 運用差分ゼロ確認
- 本番 Bot 起動時 `common_v3.observability.deadman.write_beacon` 呼び出しで launchd 側と衝突なし E2E 確認

**関連証跡**:
- Builder 実装: `common_v3/observability/deadman.py` / `tests/test_deadman_lib.py` (18/18 PASS)
- Navigator CONDITIONAL-PASS 監査（会話ログ保持）
- Redteam HIGH 4 件監査（会話ログ保持）
- 全体 pytest: 3053 passed / 4 failed (pre-existing)

---

## C-008: Idempotency B15 StorageBackend 経由化

**起票**: 2026-04-23（Sprint 0.5 Day 3 / #1 Idempotency 完了時）

**背景**:
- Spec B6（`data/specs/v3/common_spec_v3_20260422.md` L205-L222）は `IdempotencyStore` を **B15 StorageBackend 経由**で保存する設計
- B15（`common_v3/storage/persistence.py`・L409-）は R1 新設だが本 Sprint 0.5 では未実装
- Day 3 で B15 も同時着手すると複合作業リスク（バグ発生率上昇）

**Day 3 の判断（ゆうさくさん承認案 C）**:
- B6 を **file-based 実装**で先行完結（既存 `data/idempotency_keys.json` 互換）
- `common/idempotency.py`（既存・書換禁止）は無変更
- B15 経由化は本 Sprint 1 持ち越し

**Sprint 1 で必須実装**:

### C-008-1: B15 StorageBackend 実装
- SQLite で orders / positions / idempotency を扱う
- 公開 Interface: `save_idempotency_record` / `load_idempotency_records` / `save_kill_switch_audit` 等
- spec L409-L440 参照

### C-008-2: B6 IdempotencyStore を B15 経由に差替
- `IdempotencyStore.__init__(self, storage: StorageBackend)` に変更
- file-based 実装は deprecated stub として並行稼働（3 日 shadow）
- 差分ゼロ確認後に file-based 実装を削除

### C-008-4: Redteam HIGH 4 件（2026-04-23 Day 3 audit）
**HIGH-1 tz 正規化**: `make_job_key` が `trigger_time.isoformat()` を tz-naive/aware 区別せずハッシュ化 → JST/UTC で別キー。Sprint 1 で `trigger_time.astimezone(timezone.utc).replace(microsecond=0).isoformat()` 正規化。
**HIGH-2 JSON 破損 fail-safe**: corrupt / list / legacy str timestamp で未処理例外 → 呼出側握り潰しで check skip（fail-open）。Sprint 1 で `try/except JSONDecodeError` + corrupt ファイル `.corrupt.{ts}` 退避 + fail-safe（False 返却で発注ブロック + 通知）。
**HIGH-3 SIGKILL atomic write**: `"a+"` mode は Linux で atomic fsync なし。temp-file + `os.fsync` + `os.replace` パターン必須。
**HIGH-4 property/fuzz test 追加**: 提出 10 tests は正常系のみ。`@given(ttl_sec=st.integers())` / JSON 破損 / SIGKILL / tz 混在の property test を Sprint 1 で追加（再発防止）。

### C-008-5: Redteam MEDIUM 4 件
- 48bit hash 衝突空間（`sha256[:12]` → `[:16]` 以上推奨）
- clock skew 未検出（`ts > now + skew_tolerance` で warning）
- 重複ブロック時の escalation 無し（check_and_mark False で Pushover 通知）
- 既存 `data/idempotency_keys.json` との共存戦略（scheme version prefix）

### C-008-7: Redteam r3 HIGH 3 件（OrderNotSentError 追加後）
**HIGH-5**: `OrderNotSentError` が公開 export。外部モジュール/依存ライブラリが broker 送信後の副作用層で誤 raise → unmark → 二重発注経路。現状 docstring 警告のみ・物理強制なし。
- Sprint 1 対応: `with_idempotency` 内で func 実行時間閾値や送信済みフラグ契約 (`func.mark_sent()` 等) を導入し、flag set 後の OrderNotSentError を無視 or 警告。
**HIGH-6**: `_unmark` 中の I/O エラー（flock 失敗 / JSON write 失敗）時の挙動未定義。write 失敗は伝播し、呼出側は「送信前例外」と誤解して別経路で再試行 → 二重発注。
- Sprint 1 対応: `_unmark` を `try/except OSError:` で囲み、失敗時は `OrderNotSentError` を raise しない（例外置換）か、escalation 通知。
**HIGH-7**: 呼出側契約テスト未整備（「どんな例外を OrderNotSentError にすべきか」の規律テストなし）。
- Sprint 1 対応: `tests/contract/test_order_send_contract.py` 追加で broker client レベルの例外種別を型で縛る。

### C-008-6: Redteam LOW 1 件
- NFS/SMB マウントで fcntl.flock 無効（将来 VPS→Mac 共有参照時）

### C-008-3: 既存 `common/idempotency.py` の Phase 2 置換
- spec L222「既存 `common/idempotency.py` IdempotencyStore は Phase 2 で本 Interface に置換」
- Phase 2 着手時に呼び出し側（spy_bot / chronos_bot 等）を `common_v3.idempotency` に切替

**Sprint 1 完了基準**:
- Redteam audit で CRITICAL なし
- SQLite backend 並行稼働 3 日 shadow 差分ゼロ
- 既存 JSON file との数値一致 E2E 確認

**関連証跡**:
- Builder 実装: `common_v3/idempotency/store.py` / `tests/test_idempotency_store.py` (10/10 PASS)
- Day 3 pytest: 3053 passed（pre-existing 5 件 failed は本 Sprint 0.5 対象外）

---

## C-009: earnings test 共有 state 汚染の fixture 化

**起票**: 2026-04-23（Sprint 0.5 Day 3 / #1 Idempotency 完了時の pytest 全件確認で発覚）

**背景**:
- `tests/property/test_earnings_engine_props.py::test_record_outcome_pre_iv_zero_no_exception` が `data/earnings_history.json` を **shared fixture** として使用
- Hypothesis が property 違反 example を shrink して DB (`.hypothesis/database/`) に保存 + JSON に実レコードを累積書き込み
- 1 回の pytest 実行で TSLA/META/NVDA 各 30 件の汚染レコードが蓄積
- 次回 `tests/test_earnings.py::TestIVCrushRateDefault::test_known_symbol_tsla` / `TestRecordOutcomeAndHistory::test_record_outcome_updates_history` が連鎖失敗

**Day 3 の暫定対処（完了）**:
- `data/earnings_history.json` を `{}` で clean 化
- `.hypothesis/database/` 削除
- `test_record_outcome_pre_iv_zero_no_exception` に `@pytest.mark.skip` マーカー追加
- 以降の pytest 実行で再汚染しないことを確認

**Sprint 1 で必須実装**:
- `tests/conftest.py` に autouse fixture で earnings_history.json を tmp_path に isolate
  ```python
  @pytest.fixture(autouse=True)
  def isolate_earnings_history(tmp_path, monkeypatch):
      fake = tmp_path / "earnings_history.json"
      fake.write_text("{}")
      monkeypatch.setenv("EARNINGS_HISTORY_PATH", str(fake))
  ```
- `common/earnings_engine.py` が `EARNINGS_HISTORY_PATH` を参照するよう Phase 2 対応（既存書換禁止の例外）
- `test_record_outcome_pre_iv_zero_no_exception` の skip マーカー解除
- Hypothesis DB を pytest 実行ごとに clean する fixture 追加

**Sprint 1 完了基準**:
- 任意順序で pytest 複数回実行しても shared state 汚染なし
- earnings property test 10/10 PASS
- 他 earnings test への副作用ゼロ

**関連証跡**:
- Day 3 実測: clean 化前 6 failed / clean 化後 4 failed（+2 件が property 由来汚染）
- `data/earnings_history.json` 汚染例: 30 records × 3 symbols で実データなしの fixture 値

---

## C-010: KillSwitch audit log の B15 StorageBackend 経由化

**起票**: 2026-04-23（Sprint 0.5 Day 3 #2 KillSwitch singleton 着手時）

**背景**:
- Spec B9 L280: `audit log: data/state_v3/kill_switch_audit.jsonl` は B15 StorageBackend.save_kill_switch_audit 経由で append-only に書く設計
- B15 は未実装（C-008-1 と同じく Sprint 1 持ち越し）

**Day 3 の暫定対処**:
- file-based の `data/state_v3/kill_switch_audit.jsonl` に直接 append
- flock で concurrent 安全化
- Sprint 1 で B15 経由化

**Sprint 1 対応**:
- B15 StorageBackend 実装後、KillSwitch の audit 書き込みを `StorageBackend.save_kill_switch_audit` 経由に差し替え
- SQLite backend 並行稼働 3 日 shadow 差分ゼロ確認

**Sprint 1 完了基準**:
- audit log の永続化 backend が SQLite
- 既存 file-based の数値一致 E2E 確認

---

## C-011: KillSwitch v3 残存 HIGH/MEDIUM/LOW

**起票**: 2026-04-23（Sprint 0.5 Day 3 #2 KillSwitch / Redteam r1 + r2 audit 後）

**背景**:
- Sprint 0.5 Day 3 #2 KillSwitch singleton で CRITICAL 2 件（C-1 atomic 順序・C-2 _read_flag silent bypass）を Builder r2 で塞ぎ Redteam r2 NO CRITICAL 確認
- 残存 HIGH 4 件 + MEDIUM 3 件 + LOW 2 件は Sprint 1 持ち越し

### C-011-1: Redteam r1 HIGH 3 件
- **H-1**: `_write_flag()` tmp 名に pid のみ・同一 pid 並行プロセス衝突リスク → uuid or `os.urandom(8).hex()` に変更
- **H-2**: `firm` 文字列を `kill_switch_{firm}.flag` に f-string 注入・Literal は runtime 不効 → `re.fullmatch(r"[a-z]+", firm)` 二重防御
- **H-3**: `deactivate` の `FLAG_FILE.unlink()` と audit log 書込が非原子 → audit 先書き後 unlink

### C-011-2: Redteam r2 新規 HIGH 1 件
- **H-4**: `FirmScoped.activate()` の global 連動後、別プロセス `deactivate()` で global flag 消去される race → per-firm flag のみ残存
- 対応: per-firm activate 内で「global check + write」を同一 lock 内に収める・または deactivate 時に per-firm flag も走査 clean

### C-011-3: Redteam r1 MEDIUM 3 件
- **M-1**: audit log の O_APPEND 依存・NFS mount で非原子 → fcntl.flock 先取得後 write
- **M-2**: lock file `"w"` mode truncate → `"a"` or O_CREAT のみ
- **M-3**: `unlink()` の FileNotFoundError race 未 catch → try/except で fail-safe

### C-011-4: Redteam r1 LOW 2 件
- **L-1**: pid のみで containerized 環境で pid 衝突 → hostname + start_time 併記
- **L-2**: `deactivate` に scope 引数なし・per-firm 解除 monitoring 粒度が荒い

### C-011-5: audit log の B15 StorageBackend 経由化
- C-010 と併合（B15 実装時）

**Sprint 1 完了基準**:
- Redteam r3 audit で HIGH/MEDIUM 全 CLOSED
- E2E SIGKILL/crash consistency テスト全 PASS
- atomic global-per_firm 連携が複数プロセス間で race-free 確認

**関連証跡**:
- Builder r1 実装 + r2 CRITICAL fix: `common_v3/risk/kill_switch.py`
- tests: `tests/test_kill_switch_v3.py`（20/20 PASS）
- Navigator r1/r2 / Redteam r1/r2 audit（会話ログ保持）

---

## C-012: Python 言語仕様レベルの frozen/immutable bypass（Sprint 2 アプローチ検討）

**起票**: 2026-04-23（Sprint 1-B Phase A / C-004 Redteam r2 + C-005 Redteam r2 共通指摘）・ゆうさくさん C 案（言語仕様受入）承認済

**背景**:
- C-004 (MFFUFlexRules) / C-005 (CircuitBreaker) の frozen/immutable 設計に対し、Redteam が Python CPython 3.7+ 言語仕様レベルの bypass 経路を指摘:
  - **closure cell 経由**: `fn.__closure__[i].cell_contents` で closure 変数取得
  - **module-level WeakKeyDict 直書換**: `module._DRY_RUN_STATE[obj] = True`
  - **`object.__setattr__` slot 直書換**（CRITICAL-1 対応で WeakKeyDict に移したがその WeakKeyDict 自体が書換可能）
  - ctypes / gc.get_referents / `function.__code__` 書換 等
- Redteam 自身が r1 で「Python では真の frozen は不可能・ctypes 等も穴」と認める
- 現実的防御対象は「善意の実装者の誤用」・敵対的攻撃者は既に VPS 内コード実行権を持つ時点で別レイヤーで防御必要

**ゆうさくさん判断（2026-04-23 12:30 JST）**:
- **案 C（言語仕様受入）採用**
- 現状の WeakKeyDictionary + closure + inspect.signature.bind 実装で「善意の誤用」防御は十分
- 敵対攻撃防御は別レイヤ（VPS アクセス制御 / 監査ログ / 多要素認証 / os.fork 境界）で対応

**Sprint 2 以降で検討するアプローチ**:

### C-012-1: C 拡張による state 保持（重量級）
- Python オブジェクトのコア state を C 拡張に委譲
- ctypes 経路も内部 struct レイアウトを隠蔽
- 工数: 大・メンテナンス負担増

### C-012-2: os.fork 境界 / 別プロセス signer
- KillSwitch / CircuitBreaker 等の critical state を別プロセスで管理
- IPC（UNIX socket / gRPC）経由でアクセス
- プロセス境界で言語仕様 bypass を遮断
- 工数: 中〜大・ADR-008 書換必要

### C-012-3: 監査ログ + CI hook で「不正書換の痕跡検出」
- 防御でなく検出に徹する
- `function.__closure__` アクセスや module state 直書換をランタイム検出 → Pushover alert
- 工数: 小〜中

### C-012-4: 実運用でのレイヤード防御
- VPS アクセス制御（SSH key 厳格化・MFA）
- 取引口座の 2 要素認証・IP allowlist
- audit log の即時外部転送（改竄検出）
- 工数: 運用設定のみ

**Sprint 2 完了基準**:
- 上記 1-4 を組み合わせた多層防御案を ADR 化
- Redteam audit で「現実的な防御深度は確保」判定

**関連証跡**:
- C-004: `chronos_v3/prop/mffu_flex.py` / Redteam r2 CRITICAL 2
- C-005: `common_v3/self_healing/circuit_breaker.py` / Redteam r2 CRITICAL-7

---

## 確認手順（次セッション開始時）

このファイルを `memory/CURRENT_STATE.md` から参照させ、Sprint 1 着手時に必ず読む。

```bash
# Sprint 1 着手チェック
grep -l "Sprint 1" /Users/yuusakuichio/trading/data/sprint1_carryovers.md && \
  echo "Sprint 1 持ち越し項目あり・必読"
```
