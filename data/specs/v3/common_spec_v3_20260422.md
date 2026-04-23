# common_v3 仕様書 v3（2026-04-22 起草・2026-04-23 R1 改訂）

**位置付け**: Atlas/Chronos の共通コア・interface 凍結・Phase 2 実装の基盤

**起草元**:
- `data/specs/v2/common_spec_20260422.md`（知識抽出源・v2 draft）
- `memory/project_agent_organization_20260422.md`（新組織・機械検証層 L1-L6）
- `data/research/self_healing_bot_20260422.md` / `sre_unattended_observability_20260422.md`（外部知見）
- `memory/CURRENT_STATE.md`（現状）

**改訂履歴**:
- 2026-04-22 初版
- 2026-04-23 R1: Gemini MUST-FIX 3 件 + Redteam CRITICAL 11 件 + 案B（sync/async 両対応 abstract Interface）反映
- 2026-04-23 R2: Redteam R1 再検証 CRITICAL 4 件（R-02 TaskExecutor Future 型統一・R-03 Kill Switch 同期経路 AST hook 明記）反映

**完成基準**（Phase 1 C-2 dry-run）:
- Builder が実装できる粒度の interface 凍結
- Navigator が監視できる規律明記
- Redteam が攻撃視点で検証可能
- Auditor（Gemini Flash）が三権外監査可能
- ゆうさくさん最終承認

---

## Part A: ゆうさくさん向け（共通コアとは何か）

### A1. 共通コアの役割
Atlas（SPY/SPX オプション）と Chronos（CME 先物）の両方が使う**土台**。
- 認証管理・通知・発注・建玉監視・Kill Switch・市場データ・可観測性
- Atlas/Chronos どちらを直しても共通コアは変わらない
- 逆に共通コアを変えると両方に影響・変更時は 3 者検証厳格化

### A2. 何をしてくれるか（機能 11 領域）

| 領域 | 役割 | ゆうさくさんへの影響 |
|---|---|---|
| auth_budget | OpenD / Tradovate の認証試行制限 | BAN 防止（月次確認不要） |
| llm_budget | Gemini/OpenAI の月額上限 | 想定外課金防止（残高監視通知） |
| notify | Pushover/ntfy/Gmail 通知 | 朝の要約と緊急アラートを受け取る |
| order | 発注・決済 | - |
| position | 建玉管理 | 残高差異時に通知 |
| kill_switch | 緊急停止 | Andon Cord 発令で即時停止 |
| market_data | 価格・Greeks 取得 | - |
| idempotency | 二重発注防止 | 想定外の重複取引防止 |
| spec_drift_watcher | プロップ/ブローカー仕様変更監視 | 月数件の承認判断 |
| observability | 稼働監視・Deadman Switch | 異常時に自動通知 |
| self_healing | Circuit Breaker / Supervisor Tree | 場中のクラッシュ自動回復 |

### A3. ゆうさくさんが承認する場面（月 3-5 件想定）
- Kill Switch 解除
- プロップ/ブローカー仕様変更の yaml 反映
- 新戦略採用
- 重大パラメータ変更

### A4. 触らないもの（物理ガード済）
- OpenD 残り認証回数（auth_budget で max=3/24h 物理ガード）
- 既存コード（legacy_write_block で物理ガード）

---

## Part B: Interface 凍結（実装者向け・Phase 2 Builder 参照）

### B1. `common_v3/auth/budget.py`
**責務**: 既存 `common/auth_budget.py` の仕様継承 + 軽量化

**継承する定数**: `SERVICES` dict（tradovate_demo / tradovate_live / opend / moomoo / gmail_oauth / mffu）・各 service の `max / window_sec / critical / note`

**凍結 Interface**:
```python
class AuthBudget:
    @classmethod
    def check_budget(cls, service: str) -> tuple[bool, int, str]: ...
    @classmethod
    def record_attempt(cls, service: str, success: bool = False, note: str = "") -> None: ...
    @classmethod
    def hard_block_if_exhausted(cls, service: str) -> None: ...
    @classmethod
    def get_summary(cls) -> dict: ...

class AuthBudgetExceeded(Exception): ...
```

**入出力契約**:
- `check_budget` 戻り値: `(allowed: bool, remaining: int, reason: str)`
- `hard_block_if_exhausted` は exhausted で `AuthBudgetExceeded` raise
- ログ: `data/logs/auth_budget.log` に append

**bypass**: `AUTH_BUDGET_BYPASS=1` 環境変数

### B2. `common_v3/llm/budget.py`
**責務**: `common/llm_budget.py`（2026-04-22 Phase 0 で実装済）をそのまま移行

**凍結 Interface**:
```python
class LLMBudget:
    @classmethod
    def record_usage(cls, vendor: str, model: str = "", input_tokens: int = 0,
                     output_tokens: int = 0, actual_cost_usd: float = 0.0,
                     priority: str = "normal", success: bool = True, note: str = "") -> None: ...
    @classmethod
    def check_budget(cls, vendor: str, est_cost_usd: float = 0.0,
                     priority: str = "normal") -> tuple[bool, str, dict]: ...
    @classmethod
    def hard_block_if_exhausted(cls, vendor: str, est_cost_usd: float = 0.0,
                                priority: str = "normal") -> None: ...

class LLMBudgetExceeded(Exception): ...
class LLMRateLimitExceeded(Exception): ...
```

**priority**: `critical` / `normal` / `optional`
**vendor**: `openai` / `gemini` / `anthropic`

### B3. `common_v3/notify/eicas.py`
**責務**: EICAS 3 層通知（Warning / Caution / Advisory）

**レベル定義（R1 改訂・Redteam C-03 対応）**:
- `Warning`: ゆうさくさん即通知（Pushover priority=2 + ntfy）**※ Kill Switch とは分離**
- `Caution`: ソラ統合通知（Pushover priority=1）
- `Advisory`: 日次 digest に集約（04:00 JST に 1 通）

**Kill Switch 発動条件（独立経路・EICAS Warning と分離）**:
- MLL/DLL 超過（per-account）
- Portfolio DD 超過（account_equity の動的閾値）
- FirmScopedKillSwitch（プロップ別 rule 違反）
- 明示的 `pull_andon()` 呼出

EICAS Warning は「ゆうさくさんに判断を促す」のみ。自動停止は行わない（Boeing 777 以降の航空原則・Therac-25 型 interlock 誤動作回避）。

**ログフォーマット（Gemini Fix 3 対応）**:
```python
@dataclass(frozen=True)
class EICASRecord:
    """EICAS ログ 1 行（JSONL append・非エンジニア時 LLM 食わせ前提）"""
    timestamp: datetime  # UTC ISO8601
    level: Literal["WARNING", "CAUTION", "ADVISORY"]
    title: str
    message: str
    source: str  # "atlas.engine" / "chronos.prop.mffu" 等
    metadata: dict[str, str]  # 自由 key-value
```
保存先: `data/state_v3/eicas.jsonl`（append-only・改竄検知可能）

**凍結 Interface**:
```python
class EICAS:
    @staticmethod
    def warning(title: str, message: str, source: str = "",
                metadata: dict[str, str] | None = None) -> EICASRecord: ...
    @staticmethod
    def caution(title: str, message: str, source: str = "",
                metadata: dict[str, str] | None = None) -> EICASRecord: ...
    @staticmethod
    def advisory(title: str, message: str, source: str = "",
                 metadata: dict[str, str] | None = None) -> None: ...
    @staticmethod
    def flush_advisory_digest() -> dict: ...
```

### B4. `common_v3/notify/andon.py`
**責務**: Andon Cord 3 経路・`.claude/hooks/andon_multichannel.py` を ライブラリ化

**凍結 Interface**:
```python
def pull_andon(reason: str, source: str = "unknown") -> dict: ...
def check_kill_switch() -> bool: ...
def release_kill_switch(releaser: str, reason: str) -> bool: ...
```

**連動**: `common_v3/risk/kill_switch.py` の `activate()` を必ず呼出（P0 修正済の設計）

### B5. `common_v3/order/models.py`
**責務**: OrderRequest / Leg / OrderResult dataclass

```python
@dataclass(frozen=True)
class Leg:
    symbol: str
    option_type: Literal["CALL", "PUT", "STOCK", "FUTURE"] = "STOCK"
    strike: float | None = None
    expiry: date | None = None
    qty: int = 0
    side: Literal["BUY", "SELL"] = "BUY"

@dataclass(frozen=True)
class OrderRequest:
    request_id: str
    legs: tuple[Leg, ...]
    strategy: str
    net_credit_debit: float | None = None
    max_slippage: float = 0.05
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass
class OrderResult:
    request_id: str
    broker_order_id: str | None
    status: Literal["PENDING", "FILLED", "CANCELLED", "REJECTED"]
    filled_qty: int = 0
    filled_price: float | None = None
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

### B6. `common_v3/idempotency/store.py`（R1 改訂・Redteam C-01 対応）
**責務**: 二重発注物理防止

**path 決定**: scaffold 既存の `common_v3/idempotency/__init__.py` に合わせ `common_v3/idempotency/store.py` で確定。`common_v3/order/idempotency.py` は**作らない**（scaffold 衝突回避）。

```python
class IdempotencyStore:
    """永続化層 Interface（B15 StorageBackend）経由で保存"""
    def __init__(self, storage: StorageBackend): ...
    def check_and_mark(self, key: str, ttl_sec: int = 300) -> bool:
        """True=新規・False=重複"""

def make_job_key(strategy: str, symbol: str, trigger_time: datetime) -> str: ...
def with_idempotency(store: IdempotencyStore, key: str,
                     func: Callable, ttl_sec: int = 300) -> Any: ...
```

**既存 `common/idempotency.py` IdempotencyStore は Phase 2 で本 Interface に置換。**

### B7. `common_v3/order/reconcile.py`
**責務**: desired_state vs actual_state 突合・逸脱時 Andon

```python
def reconcile_positions(desired: list[Position], actual: list[Position]) -> list[Diff]: ...
def enforce_reconciliation(broker_client) -> None: ...
```

### B8. `common_v3/position/models.py`
**責務**: Position / PortfolioSnapshot dataclass

```python
@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_price: float
    pnl_unrealized: float
    delta: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class PortfolioSnapshot:
    timestamp: datetime
    positions: tuple[Position, ...]
    cash: float
    total_delta: float
    total_pnl: float
```

### B9. `common_v3/risk/kill_switch.py`（R1 改訂・Redteam C-02 対応）
**責務**: 既存 `common/kill_switch.py` の冪等性欠陥修正版

**修正内容**（4/22 redteam 発見）:
- `activate()` 戻り値を **bool** に変更（True=新規発動 / False=既 ARMED 冪等スキップ）→ Pushover 二重送信防止
- `deactivate()` は FLAG_FILE 不在時 early return（audit log 追記しない）
- 4 層 trigger: per-tactic / per-symbol / per-account / portfolio
- **FirmScopedKillSwitch 統合経路明記**: プロップ別（MFFU/Tradeify 等）の scope 付き kill は `FirmScopedKillSwitch(firm=...).activate()` 経由。グローバル kill と別 flag file (`data/state_v3/kill_switch_{firm}.flag`)・発動時は全体 `activate()` も連動。

**凍結 Interface**:
```python
def activate(reason: str = "manual", activator: str = "unknown",
             scope: dict | None = None) -> bool:
    """True=新規発動 / False=既 ARMED 冪等スキップ"""

def deactivate(activator: str = "unknown", reason: str = "") -> bool: ...
def is_active() -> bool: ...
def get_state() -> dict | None: ...

class FirmScopedKillSwitch:
    def __init__(self, firm: Literal["mffu", "tradeify", "apex", "bulenox"]): ...
    def activate(self, reason: str, activator: str = "unknown") -> bool: ...
    def deactivate(self, activator: str = "unknown") -> bool: ...
    def is_active(self) -> bool: ...
```

**audit log**: `data/state_v3/kill_switch_audit.jsonl`（B15 StorageBackend.save_kill_switch_audit 経由・append-only）

### B10. `common_v3/market/data.py`（R1 改訂・Redteam C-04 対応）
**責務**: 市場データ統一窓口

**error 契約（silent failure 禁止・全メソッド必須）**:
- 戻り値を `MarketDataResult[T]` でラップし、`value / stale / fetched_at / source / error` を常に明示
- 取得失敗時は例外 `MarketDataError` raise または `stale=True` で返却（呼出元が明示選択）
- **silent default（0.0 や 15.0 等）返却は禁止**（CLAUDE.md silent except 禁止規律）

```python
@dataclass(frozen=True)
class MarketDataResult[T]:
    value: T
    stale: bool  # True=キャッシュ期限超過値・呼出元が判断
    fetched_at: datetime
    source: Literal["live", "cache", "fallback"]
    error: str | None = None

class MarketDataError(Exception):
    """取得失敗・stale 上限超・default 返却拒否時に raise"""

class MarketDataClient:
    def get_vix(self, allow_stale: bool = False) -> MarketDataResult[float]: ...
    def get_iv_rank(self, symbol: str, allow_stale: bool = False) -> MarketDataResult[float]: ...
    def get_greeks(self, option_code: str, allow_stale: bool = False) -> MarketDataResult[dict]: ...
    def get_orderbook(self, symbol: str, depth: int = 10,
                      allow_stale: bool = False) -> MarketDataResult[list[dict]]: ...
    def get_historical_volatility(self, symbol: str, days: int = 20,
                                  allow_stale: bool = False) -> MarketDataResult[float]: ...
```

**stale 許容上限**: 戦術ごとに `allow_stale_max_sec` を定義（gamma_scalp=5s / ic_sell=300s 等）。上限超過で `allow_stale=True` でも `MarketDataError` raise。

**cache**: `data/state_v3/market_cache/*.json`（TTL 最大 1h）

### B11. `common_v3/observability/deadman.py`（R1 改訂・Redteam C-06 対応）
**責務**: 既存 `scripts/dead_man_switch.py`（LaunchAgent 登録済・COMPONENTS 7 つ監視中）をライブラリ化

**migration 手順（必須・並行稼働禁止）**:
1. `common_v3/observability/deadman.py` 実装（beacon 書込 path は既存と互換: `data/state_v3/deadman/*.beacon`）
2. 既存 `scripts/dead_man_switch.py` 内の `check_and_alert()` を `common_v3.observability.deadman.check_and_alert()` に委譲（既存 launchd plist は触らない）
3. 3 日 shadow 運用（新旧両方記録・差分検証）
4. 差分ゼロ確認後、scripts/dead_man_switch.py を deprecated stub（委譲のみ）に縮小
5. 新旧 beacon path 不一致による両方 silent 死を防ぐため **同一 path** 使用（`data/state_v3/deadman/`）

```python
def write_beacon(component: str) -> None: ...
def check_and_alert() -> dict: ...
def get_last_ping(component: str) -> float | None: ...
def list_components() -> list[str]: ...  # migration 検証用
```

### B12. `common_v3/observability/health_check.py`
**責務**: 3-tier health check

```python
class HealthCheck:
    @staticmethod
    def startup(service: str) -> bool: ...
    @staticmethod
    def liveness(service: str) -> bool: ...
    @staticmethod
    def readiness(service: str) -> bool: ...
```

### B13. `common_v3/spec_drift/watcher.py`
**責務**: Broker/Prop 仕様変更の自動検知

```python
def scan_all_targets() -> list[Drift]: ...
def propose_patch(drift: Drift) -> Patch: ...
```

**承認**: 人間承認ゲート必須（自動反映禁止）

### B14. `common_v3/self_healing/circuit_breaker.py`（R1 改訂・Redteam C-05 + Gemini「自己治癒は停止+ログへ」対応）
**責務**: Circuit Breaker 抽象 Interface（ベンダーロック回避・単一ベンダー障害時の差替え可能性確保）

**設計変更（Gemini 直言反映）**:
- 自動復旧（reset_timeout で ARMED→CLOSED）を **禁止**
- 3 回失敗で **停止 → 人間承認のみ復帰**（Self-healing 誤作動によるバグ隠蔽防止）
- 「整備士のいない F1 カー」化回避・非エンジニアが 5 分以内でログ読解可能な粒度

```python
class CircuitBreakerBackend(Protocol):
    """pybreaker / circuitbreaker / 自製 の差替え可能 Interface"""
    def call(self, func: Callable, *args, **kwargs) -> Any: ...
    @property
    def state(self) -> Literal["CLOSED", "OPEN", "HALF_OPEN"]: ...
    def reset(self, approver: str) -> None:
        """人間承認必須・自動復帰禁止"""

class CircuitBreaker:
    """標準実装（backend 差替え可）"""
    def __init__(self, name: str, fail_max: int = 3,
                 backend: CircuitBreakerBackend | None = None,
                 auto_recovery: bool = False):  # 既定 False（自動復旧禁止）
        ...
```

**デフォルト設定**:
- `tradovate_breaker`: fail_max=3 / auto_recovery=False
- `moomoo_breaker`: fail_max=5 / auto_recovery=False

**復帰手順**: EICAS Warning 発出 → ゆうさくさん確認 → 原因特定 → `reset(approver="yuusaku")` 明示呼出

### B14b. `common_v3/risk/symbol_whitelist.py`（R1 新設・Redteam C-09 対応）
**責務**: ブローカー別の取引可能銘柄管理（SPX whitelist 問題の独立化・2026-04-17 事故再発防止）

**背景**: 旧 `risk_limits.py` で `US.SPX` が長期欠落しトレード 0 件事故。v3 で独立 Interface 化し責務所在を明確化。

```python
class SymbolWhitelist:
    def __init__(self, storage: StorageBackend): ...

    def is_allowed(self, broker: str, symbol: str, mode: Literal["paper", "live"]) -> bool: ...
    def register(self, broker: str, symbol: str, mode: str,
                 approver: str, note: str = "") -> None:
        """手動登録・audit log 必須"""
    def get_allowed(self, broker: str, mode: str) -> list[str]: ...
    def verify_at_startup(self) -> list[str]:
        """起動時に戦術が参照する全銘柄が whitelist に存在するか検証・欠落時 EICAS Warning"""
```

**事故防止**: `verify_at_startup()` を Engine の startup health check で必須実行。欠落銘柄があれば起動中断。

**moomoo SPX 特記**: paper 非対応・live は whitelist 登録済（2026-04-20 確認）

### B15. `common_v3/storage/persistence.py`（R1 新設・Gemini Fix 1 対応）
**責務**: 永続化層統一 Interface（SQLite + JSONL ハイブリッド）

**方針**:
- SQLite: orders / positions / idempotency（read/write 多い・indexed lookup）
- JSONL append-only: eicas / kill_switch_audit / auth_budget_audit（event sourcing・改竄検知）

```python
class StorageBackend(Protocol):
    # order
    def save_order(self, order: OrderResult) -> None: ...
    def load_order(self, request_id: str) -> OrderResult | None: ...

    # position
    def save_position_snapshot(self, snap: PortfolioSnapshot) -> None: ...
    def load_latest_snapshot(self) -> PortfolioSnapshot | None: ...

    # idempotency
    def save_idempotency_key(self, key: str, ttl_sec: int) -> None: ...
    def check_idempotency_key(self, key: str) -> bool: ...

    # kill switch audit
    def save_kill_switch_audit(self, event: dict) -> None: ...

    # eicas
    def append_eicas(self, record: EICASRecord) -> None: ...

class SqliteStorage(StorageBackend): ...  # default: data/state_v3/*.sqlite3
class JsonlStorage(StorageBackend): ...   # append-only: data/state_v3/*.jsonl
class HybridStorage(StorageBackend):
    """orders/positions/idempotency = sqlite / eicas/audit = jsonl"""
```

**パス規約**: `data/state_v3/{orders.sqlite3,positions.sqlite3,idempotency.sqlite3,eicas.jsonl,kill_switch_audit.jsonl}`

### B16. 並列実行ポリシー（R1 新設・案B = sync/async 両対応 abstract Interface 対応）

**判断**: 同期（sync）を**既定**とし、並列が必要な箇所のみ明示的に `concurrent.futures.ThreadPoolExecutor` で管理。`asyncio` は **ブローカー SDK が async のみの場合に限定** 採用（現状 moomoo/Tradovate/TradersPost は sync REST のため不要）。

**並列が必要な実用場面（非エンジニア debug 容易性を保ちつつ対応）**:
- gamma_scalp（秒単位 Greeks 再計算 + delta hedge）
- delta_hedge（portfolio 全体監視）
- multi-symbol env_observer（11 銘柄同時 VIX/IVR 取得）
- multi-account（MFFU + Tradeify 同時監視）

**ExecutorProvider Interface**（sync/async 差替え可能・R2 修正: Future 型統一 + map Iterator 化）:
```python
from concurrent.futures import Future  # 型を concurrent.futures.Future に統一
from typing import Iterator, TypeVar
T = TypeVar("T")

class TaskExecutor(Protocol):
    """sync/async の抽象化・Builder は sync 前提で書き、必要時 async 実装に差替"""
    def submit(self, func: Callable[..., T], *args, **kwargs) -> Future[T]:
        """戻り値は concurrent.futures.Future 固定。asyncio.Future 禁止（Knight Capital 2012 型デッドロック回避）"""
    def map(self, func: Callable[..., T], items: Iterable) -> Iterator[T]:
        """lazy Iterator 返却（全件評価前に中断可能・メモリ効率）"""

class SyncExecutor(TaskExecutor):
    """ThreadPoolExecutor ラッパー（default・max_workers 明示必須・再入防止）"""

class AsyncExecutor(TaskExecutor):
    """asyncio.gather ラッパー（Phase 3 以降・Future 変換層経由）"""
```

**規律（R2 修正・Redteam R-03 対応）**:
- Builder は常に `TaskExecutor` Interface に対してコードを書く（具象 `SyncExecutor` 直呼び禁止）
- `asyncio` 直使用は `common_v3/executor/async_impl.py` 内のみ許可（他所禁止）
- Circuit Breaker / Idempotency / Kill Switch 確認は **同期経路で必須実行**（並列化禁止）
- **物理強制**: `.claude/hooks/executor_sync_only_guard.sh` で以下 AST 検査:
  - `executor.submit(... check_kill_switch ...)` / `executor.submit(... is_active ...)` 等の kill_switch 並列化を検知して block
  - `executor.submit(... check_and_mark ...)` 等の idempotency 並列化も同様
  - `executor.map(... kill_switch ...)` も block
  - Linter で import 違反（`import asyncio` を async_impl.py 外で）検出

---

## Part C: DAG 情報（実装順）

```
Phase 2 実装順（R1 改訂）:
1. common_v3/auth/budget.py          (既存継承・リスク低)
2. common_v3/llm/budget.py           (既存継承)
3. common_v3/storage/persistence.py  (B15・全体土台・最優先)
4. common_v3/order/models.py         (純 dataclass)
5. common_v3/position/models.py      (純 dataclass)
6. common_v3/executor/*               (B16 TaskExecutor 抽象)
7. common_v3/risk/kill_switch.py     (新設計・冪等性 bool・FirmScoped)
8. common_v3/risk/symbol_whitelist.py (C-09 新設)
9. common_v3/notify/eicas.py         (EICASRecord dataclass)
10. common_v3/notify/andon.py         (ライブラリ化・kill_switch 分離)
11. common_v3/idempotency/store.py   (scaffold 既存 path に準拠)
12. common_v3/order/reconcile.py
13. common_v3/market/data.py         (error 契約付き)
14. common_v3/observability/*        (既存 deadman migration 3 日 shadow)
15. common_v3/spec_drift/watcher.py  (Phase 1 実運用済み)
16. common_v3/self_healing/*         (抽象 CircuitBreaker・auto_recovery=False)

Atlas/Chronos は 16 完了後に並列着手可能
```

---

## Part D: 凍結宣言

本仕様書 B1-B14 の interface は Phase 2 実装時に**変更禁止**。変更必要時は Flow 3（重大判断）案件として再審議。

Builder は本仕様書の Interface 定義に従って実装。Navigator は Builder の実装が Interface と一致するか監視。Redteam は攻撃的検証。Auditor は三権外監査。

---

## Part E: テスト要件（Phase 2 実装時）

- カバレッジ 85%+（TDD 厳守）
- silent except 禁止（linter 物理強制）
- Mock 濫用禁止（interaction 検証付き mock のみ）
- integration test 比率 20%+（ice-cream cone anti-pattern 回避）
- Hypothesis property-based testing 適用（boundary case）
- mutation testing score 75%+

---

## Part F: 未確定事項の解消状況（R1・Redteam C-11 対応）

| ID | 未確定事項 | 解消状況 |
|---|---|---|
| F-01 | Navigator 代替役精査 | 2026-04-23 R1: Gemini Flash が Navigator 代替として 3 MUST-FIX 発行済 |
| F-02 | 別 session Redteam 検証 | 2026-04-22 完了: redteam_spec_v3_audit_20260422.md CRITICAL 11 件抽出 |
| F-03 | 案B（sync/async 両対応）の妥当性 | R1 改訂・B16 で ExecutorProvider 抽象化済・再検証待ち |
| F-04 | Storage バックエンド（SQLite vs JSONL） | R1 で B15 確定: sqlite+jsonl ハイブリッド |
| F-05 | Self-healing 自動復旧許容度 | R1 で B14 確定: 自動復旧禁止・人間承認必須 |

**残存 Part F 項目（他 2 spec 側）**:
- atlas_spec_v3 Part F: moomoo paper SPX 扱い / 個別 7 銘柄 earnings / gamma_scalp MVP 実測 → Phase 2 着手前に Builder 調査タスクで埋める（凍結阻止ではなく、Phase 2 実装の直前 gate）
- chronos_spec_v3 Part F: MFFU Flex 最新 Profit Target / Max Loss → 契約時点確認・B5 MFFUFlexRules 契約明記済（C-10 対応）

## Part G: 次ステップ（Phase 1 C-2 dry-run R2）

1. 本 R1 改訂版を Gemini Flash で再検証（CONDITIONAL-GO → GO 判定待ち）
2. Redteam 独立検証（Claude 別 session・新規 agent 起動）
3. 両 GO でゆうさくさん最終承認
4. 承認後、本 R1 を確定版として Phase 2 Builder に渡す

---

## 関連
- `data/specs/v2/common_spec_20260422.md`（知識抽出源）
- `common/llm_budget.py`（既実装・Phase 2 で common_v3 へ移行）
- `common/auth_budget.py`（既存・Phase 2 で common_v3 へ移行）
- `memory/project_agent_organization_20260422.md`
