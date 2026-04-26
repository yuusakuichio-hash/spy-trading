# 仕様書 v3 修正計画（Gemini + Redteam 指摘反映用 draft）

**作成**: 2026-04-23（Phase 1 C-2 dry-run 結果反映）
**ステータス**: **R1 改訂完了** / Gemini + Redteam 再検証（R2）実行中

## R1 改訂完了（2026-04-23 03:00 JST）
3 本すべて改訂済:
- `data/specs/v3/common_spec_v3_20260422.md`（B3/B6/B9/B10/B11/B14 改訂 + B14b/B15/B16 新設 + Part F/G）
- `data/specs/v3/atlas_spec_v3_20260422.md`（B2 Dynamic Percentile / B5 3 Type 分類 / B8 Whitelist 委譲）
- `data/specs/v3/chronos_spec_v3_20260422.md`（B5 MFFU 動的値契約 + MFFURuleMissingError）

**再検証**: Gemini Flash（background agent aecb3c46...） / Redteam（background agent a019e5ec...）並行起動済

## Gemini MUST-FIX（3 件）への対応 draft

### Fix 1: 永続化層（Storage Interface）追加
**対象**: `common_v3/common_spec_v3_20260422.md` Part B に B15 として追加

```python
# common_v3/storage/persistence.py

class StorageBackend(Protocol):
    """永続化層の統一 Interface (SQLite / JSONL 可換)"""
    def save_order(self, order: OrderResult) -> None: ...
    def load_order(self, request_id: str) -> OrderResult | None: ...
    def save_position_snapshot(self, snap: PortfolioSnapshot) -> None: ...
    def load_latest_snapshot(self) -> PortfolioSnapshot | None: ...
    def save_idempotency_key(self, key: str, ttl_sec: int) -> None: ...
    def check_idempotency_key(self, key: str) -> bool: ...
    def save_kill_switch_audit(self, event: dict) -> None: ...

# デフォルト実装（Phase 2 確定）:
# - SQLite（標準ライブラリ・非エンジニア可読）
# - JSONL append-only（event sourcing）
# パス: data/state_v3/*.sqlite3 / data/state_v3/*.jsonl
```

**判断**: Gemini 指摘通り SQLite + JSONL ハイブリッド
- SQLite: orders / positions / idempotency（read/write 多い）
- JSONL append-only: kill_switch_audit / eicas_log（event sourcing・改竄検知）

### Fix 2: asyncio 採用可否 → **同期（sync）で確定**
**対象**: 各仕様書に「非同期ポリシー」節追加

**判断根拠**:
- ゆうさくさん非エンジニア・debug 難易度重視
- Atlas 場中 3-4 時間 / Chronos 24h 監視で tick 単位 60 秒猶予
- moomoo/Tradovate API は synchronous request で十分
- Gemini 指摘: 「非同期は非エンジニアのデバッグ難易度を爆上げ」

**規律**:
- 全 Bot コードは同期
- 並列実行が必要な箇所（Navigator 並走監視等）は `concurrent.futures.ThreadPoolExecutor` で明示管理
- `asyncio` 禁止（Phase 2 linter で物理 block 候補）

### Fix 3: EICAS ログフォーマット固定
**対象**: `common_v3/notify/eicas.py` の Interface に追記

```python
@dataclass(frozen=True)
class EICASRecord:
    """EICAS ログ 1 行（JSONL append）"""
    timestamp: datetime  # UTC ISO8601
    level: Literal["WARNING", "CAUTION", "ADVISORY"]
    title: str
    message: str
    source: str  # "atlas.engine" / "chronos.prop.mffu" 等
    metadata: dict[str, str]  # 自由 key-value
    # エンジニア不在時 LLM 食わせ前提の構造化
```

**保存先**: `data/state_v3/eicas.jsonl`（append-only）

## Gemini 独自直言への対応

### 「整備士のいない F1 カー」
- Self-healing の過剰設計を**シンプル停止+詳細ログ**に寄せる
- Circuit Breaker の動作を「3 回失敗で**停止**（自動復旧ではない・再起動は人間承認）」に制限
- 非エンジニアが「どこで何が起きたか」を 5 分以内で読み解ける粒度のエラーメッセージ

### 「Self-healing がバグなし絶対を隠蔽」
- `common_v3/self_healing/circuit_breaker.py` の設計変更:
  - 旧案: 失敗しても自動 retry・reset_timeout で復帰
  - 新案: 失敗で即停止・手動承認のみ復帰
  - これで「隠蔽」リスク排除

### 「Common 肥大化ビッグバン統合の罠」
- DAG 実装順で既に明示済だが、**各 common_v3 モジュール完成時に smoke test**を必須化
- Atlas/Chronos 依存前に Common 各モジュール単体で動作確認

## Redteam 結果反映欄（完了後追記）

（Redteam agent a75c6a5f... 完了後にここに追記）

## 統合後の Phase 1 C-2 判定（予定）

- Gemini: CONDITIONAL-GO（MUST-FIX 3 件対処で GO）
- Redteam: 待ち
- Navigator 代替役: 本 draft レビュー待ち

**全 3 pass で**:
- 仕様書 v3 を本 draft で修正
- ゆうさくさん最終承認
- Phase 1 C-2 達成 → Phase 2 着手 gate 通過（他の必須 4 項目の状態次第）

## 関連
- `data/governance/gemini_verify/spec_v3_verdict_20260423_023223.md`
- `data/specs/v3/common_spec_v3_20260422.md` / `atlas_spec_v3_20260422.md` / `chronos_spec_v3_20260422.md`
- （Redteam 結果ファイル・完了後追記）
