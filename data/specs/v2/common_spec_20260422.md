# 共通コア仕様書 v2 — Interface 凍結版
作成日: 2026-04-22
根拠ファイル: common/ 全ファイル実読・data/research/self_healing_bot_20260422.md・sre_unattended_observability_20260422.md・broker_spec_drift_adaptation_20260422.md・codebase_metrics_20260422.md

**この仕様は Atlas / Chronos 実装開始前に凍結される。Part C の interface は実装時に変更禁止。**

---

# Part A — ゆうさくさん向け（非エンジニア）

## A1. 共通コアとは何か

Atlas（オプション取引）と Chronos（先物取引）の 2 つの Bot は、同じ土台の上で動いています。この土台が「共通コア」です。

例えると、2 台の車（Atlas と Chronos）が同じエンジン・ブレーキシステム・計器類を共有しているイメージです。どちらが走っていても、ブレーキの効き方・速度計の見方は同じルールで管理されます。

共通コアは現在 `trading/common/` フォルダにまとまっており、そこに 46 ファイルが存在します。この仕様書は、そのフォルダの中で「特に重要で Atlas / Chronos 両方が必ず守るルール」を整理したものです。

## A2. 何をしてくれるか

| 機能 | 一言説明 | ゆうさくさんへの恩恵 |
|---|---|---|
| 認証管理 | ブローカーへのログイン回数を制限する | OpenD 残り 4 回が誤操作で消えない |
| 通知 | Pushover / ntfy / Gmail の 3 経路で連絡 | 1 つの経路が詰まっても必ず届く |
| 発注 | 重複発注を物理的に防ぐ | 同じ注文が 2 回出ることはない |
| 建玉管理 | 現在持っているポジションの統一管理 | 2 つの Bot がバラバラに動いても合計リスクが把握できる |
| Kill Switch | 緊急停止ボタン。人間だけが解除できる | 何かあれば即止まり、勝手に動き出さない |
| 市場データ | VIX・オプション価格等の取得 | 常に最新の市場情報を元に判断している |
| 二重実行防止 | 同じ処理が 2 回走らないようにする | システム障害でも誤発注しない |
| 仕様変更監視 | ブローカーのルール変更を自動で検知する | ルール変更を見逃して罰則を受けることがない |
| 可観測性 | Bot が生きているか常に確認する | 「いつの間にか止まっていた」が 5 分以内にわかる |
| 自己回復 | クラッシュしても自動で再起動する | 夜中に倒れても朝には回復している |

## A3. ゆうさくさんが承認する場面

以下は Bot が自動で判断せず、ゆうさくさんの確認を待つ場面です。月に数件以内の想定です。

1. **Kill Switch の解除** — 緊急停止後の再稼働。Pushover で「解除しますか」の通知が届きます。CLI コマンド 1 発で解除します。自動解除は禁止されています。
2. **ブローカー仕様変更の反映** — ルール変更を検知した時、Pushover に「こう変えていいですか」の提案が届きます。「承認」と返信するだけで反映されます。
3. **Auth Budget の緊急解除** — OpenD / Tradovate の認証試行上限に近づいた時の通知。`AUTH_BUDGET_BYPASS=1` で解除する場面もありますが、通常は不要です。

## A4. 触らないでほしいもの

| 対象 | 理由 |
|---|---|
| OpenD ログイン操作 | 残り 4 回制限。共通コアが `auth_budget.py` で物理ガード済み |
| `data/kill_switch.flag` ファイルを直接 rm で削除 | 自動再発動ロジックで検知・再ロックされる |
| `data/auth_budget/` フォルダ内の jsonl ファイル直接編集 | 認証回数カウンタが壊れる |
| `common/` フォルダ内のファイルを単独で無断編集 | Atlas / Chronos 両方に影響するため redteam レビュー必須 |

---

# Part B — 開発者用 Interface 仕様

## B1. 認証管理 auth_budget

### 責務
ブローカー・サービス別の認証試行回数を時間窓で管理し、rate limit / 永久 BAN を物理的に防止する。

### 現行実装の確認済み仕様（継承）
根拠: `common/auth_budget.py` 実読

```
SERVICES = {
    "tradovate_demo":  max=4, window_sec=3600    # 公式 5/h の 1 枠余裕
    "tradovate_live":  max=4, window_sec=3600, critical=True
    "opend":           max=3, window_sec=86400   # 残 4 回制限・24h
    "moomoo":          max=3, window_sec=3600
    "gmail_oauth":     max=2, window_sec=3600
    "mffu":            max=5, window_sec=3600
}
```

`AUTH_BUDGET_BYPASS=1` 環境変数で全チェック無効化（緊急解除専用）。

### Interface（凍結）

```python
# common/auth_budget.py

class AuthBudgetExceeded(Exception):
    """予算超過ブロック時に raise"""
    pass

class AuthBudget:
    @classmethod
    def check_budget(cls, service: str) -> tuple[bool, int, str]:
        """
        試行前の予算確認。
        Returns: (allowed: bool, remaining: int, reason: str)
          allowed=False の時 reason に詳細。
        """

    @classmethod
    def record_attempt(cls, service: str, success: bool = False, note: str = "") -> None:
        """試行後に必ず呼ぶ。成功・失敗どちらも記録。"""

    @classmethod
    def hard_block_if_exhausted(cls, service: str) -> None:
        """
        超過なら AuthBudgetExceeded を raise。
        critical サービスは raise 前に Pushover 通知。
        """

    @classmethod
    def get_summary(cls) -> dict[str, dict]:
        """
        全サービスの状況。朝次 digest 用。
        Returns: {service: {count, max, remaining, success_rate, window_sec, critical, note}}
        """
```

### 入出力契約
- 呼び出しパターン: `hard_block_if_exhausted(service)` → 認証実行 → `record_attempt(service, success=True/False)`
- `check_budget` は副作用なし（記録しない）
- 永続化先: `data/auth_budget/{service}.jsonl`（追記型 JSONL）

### エラー経路
- `AuthBudgetExceeded`: 呼び出し元は認証をスキップして ops に報告
- `critical=True` サービスが枯渇: Pushover priority=2 自動送信

### ボトルネックと対処
- ファイル I/O 競合: `_get_lock(service)` による per-service `threading.Lock` で対処済み
- 不明サービス名: デフォルト max=3/3600s にフォールバック（安全側）

---

## B2. 通知 notify

### 責務
Pushover / ntfy.sh / Gmail の 3 経路で通知を送る。EICAS 3 層（Warning / Caution / Advisory）に対応し、Advisory は日次 digest に集約する。Pushover 月枠超過時は自動 failover する。

### 現行実装の確認済み仕様（継承）
根拠: `common/pushover_client.py` 実読

- `LEVEL_CRITICAL`: 即時送信（4 系統: 資金損失 / アカウント停止 / 本番異常 / 市場機会喪失）
- `LEVEL_BATCHED`: 30 分毎バッチ送信（Caution 相当）
- `LEVEL_SILENT`: ログのみ（Advisory 相当）
- dedup: SHA-256(title + msg[:100])、1 時間窓。priority=2 は dedup 無視
- backoff: 連続 3 回 429 → 30 分沈黙。ban 中は queue 追記
- 静穏時間: JST 22:00-4:00 は非緊急通知を morning_queue へ保留

### 欠陥と新設計（4/22 SRE 監査より）
根拠: `data/research/sre_unattended_observability_20260422.md`

現状の欠陥:
- Pushover 単一チャンネル依存（10,000/月 超過時に escalation 経路なし）
- `advisory` レベルも Pushover を消費している

新設計（この仕様で確定）:

```
EICAS 3 層マッピング:
  Warning  (priority=2) → Pushover ALERT + ntfy + Gmail — 3 経路冗長
  Caution  (priority=1) → Pushover OPS token — 単一 (< 500/月)
  Advisory (priority≦0) → LEVEL_SILENT のみ。Pushover 送信禁止
                          → 日次 digest に集約（JST 09:00 に 1 通）
```

### Interface（凍結）

```python
# common/pushover_client.py — 公開 API

def send(
    title: str,
    message: str,
    priority: int = 0,
    *,
    token: str | None = None,
    app_tag: str = "SYS",
    level: str = LEVEL_BATCHED,
) -> bool:
    """
    ゲートレイヤー付き送信。
    SILENT  → ログのみ。
    BATCHED → バッチキューに追記。
    CRITICAL → 即時送信。ban 中はキューへ。
    Returns: True = 送信成功または正常キュー
    """

def send_silent(title: str, message: str, *, app_tag: str = "SYS") -> bool:
    """Advisory 通知。ログのみ・Pushover 送信なし。"""

def send_batched(title: str, message: str, priority: int = 0, *, token: str | None = None, app_tag: str = "SYS") -> bool:
    """Caution 通知。30 分バッチで集約送信。"""

def send_critical(title: str, message: str, priority: int = 1, *, token: str | None = None, app_tag: str = "SYS") -> bool:
    """Warning 通知。即時送信。4 系統専用。"""

def send_alert(
    title: str,
    message: str,
    priority: int = 0,
    *,
    token: str | None = None,
    app_tag: str = "SYS",
    channels: list[str] | None = None,
) -> dict[str, bool]:
    """
    Multi-channel 送信。channels 省略時は全チャンネル試行。
    Returns: {"pushover": bool, "ntfy": bool, "gmail": bool, "discord": bool}
    """

def flush_queue() -> int:
    """backoff 解除後のキュー再送。Returns: 送信成功件数。"""

def flush_batch_queue() -> int:
    """バッチキューフラッシュ。LaunchAgent から 30 分毎に呼ぶ。"""
```

### 入出力契約
- `priority=2` は必ず `send_alert` を使う（3 経路冗長）
- 通常トレード実行ログは `send_silent` のみ使用可（Pushover 禁止）
- 夜間緊急キーワード（`SYSTEM_HALT_30MIN` / `LOSS_3PCT` 等）は priority=2 かつ `send_alert` 必須

### エラー経路
- Pushover 429: 30 分バックオフ後に自動再送
- ntfy 未設定: `NTFY_TOPIC` 未設定なら graceful skip（raise しない）
- Gmail 未設定: `GMAIL_APP_PASSWORD` 未設定なら graceful skip

### ボトルネックと対処
- Pushover 10,000/月 枠: `send_alert` の ntfy / Gmail fallback で排除
- advisory 大量 log: `send_silent` 経由で Pushover 消費ゼロ

---

## B3. 発注 order

### 責務
Atlas / Chronos 両方で使う共通発注インターフェース。idempotency key による二重発注防止・retry / backoff を一元管理する。

### 現行実装の確認済み仕様（継承）
根拠: `common/idempotency.py` 実読

- IdempotencyStore: ファイル永続化（`data/idempotency_keys.json`）
- TTL: 86400 秒（1 営業日）
- key フォーマット: `idm_{SHA256[:12]}` (len=16、moomoo remark 64 バイト上限に収まる)
- `check_and_register(key)`: True=新規 OK、False=重複ブロック
- `clear_key(key)`: 発注失敗時に再試行を許可する

### 追加仕様（新設計）

共通発注 dataclass を新設する。moomoo / Tradovate 双方への抽象化に使う。

### Interface（凍結）

```python
# common/order_interface.py（新設ファイル）

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

class OrderSide(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    LIMIT  = "LIMIT"
    MARKET = "MARKET"

@dataclass
class Leg:
    """単一レグ。マルチレグ戦略は Leg のリストで表現する。"""
    symbol:     str           # ブローカー固有シンボル（例: "US.SPY"）
    side:       OrderSide
    qty:        int
    order_type: OrderType     = OrderType.LIMIT
    limit_price: Optional[float] = None
    strike:     Optional[float]  = None
    expiry:     Optional[str]    = None   # "YYYY-MM-DD"
    option_type: Optional[str]   = None  # "CALL" / "PUT"

@dataclass
class OrderRequest:
    """ブローカー共通発注リクエスト。"""
    legs:             list[Leg]
    idempotency_key:  str             = field(default_factory=lambda: str(uuid.uuid4()))
    tactic_name:      str             = ""   # "cs_sell" / "orb_buy" 等
    account_id:       str             = ""
    time_in_force:    str             = "DAY"
    note:             str             = ""

@dataclass
class OrderResult:
    """発注結果。成否・ブローカー発行 order_id を含む。"""
    success:          bool
    order_id:         str             = ""
    idempotency_key:  str             = ""
    error_code:       str             = ""
    error_message:    str             = ""
    raw_response:     dict            = field(default_factory=dict)


# common/idempotency.py — 変更なし・既存継承

class IdempotencyStore:
    @staticmethod
    def make_key(signal_id: str, label: str) -> str:
        """
        Returns: "idm_{SHA256[:12]}" (len=16, ≤ 64 bytes)
        """

    def check_and_register(self, key: str) -> bool:
        """
        True  → 新規発注 OK。ストアに登録。
        False → 重複。ブロックすべき。
        """

    def clear_key(self, key: str) -> None:
        """発注失敗時に呼ぶ。再試行を許可する。"""

def get_store() -> IdempotencyStore:
    """グローバルシングルトン。"""
```

### Retry / Backoff 仕様（新設計）

```
retry policy:
  - max_attempts: 3
  - initial_delay_sec: 1.0
  - backoff_factor: 2.0  (exponential backoff)
  - retryable errors: network timeout, broker 429, broker 503
  - non-retryable: insufficient funds, symbol not found, kill switch active
  - on final failure: clear_key(idempotency_key) → 呼び出し元に raise
```

### 入出力契約
- `OrderRequest.idempotency_key` は呼び出し前に `IdempotencyStore.check_and_register()` で確認必須
- `Leg.qty` は 1 以上。`check_order()` (pre_trade_check) 通過後のみ発注可
- `OrderResult.success=False` の場合、`clear_key` を呼んで再試行可能にする

### エラー経路
- 重複発注: `check_and_register` が False → 発注ブロック、ログのみ
- retry 上限: `send_batched` で通知 + `OrderResult(success=False)` 返却

---

## B4. 建玉 position

### 責務
Atlas / Chronos が共有する建玉の統一表現と、desired state vs actual state の reconciliation。

### 現行実装の確認済み仕様（継承）
根拠: `common/portfolio_aggregator.py` 実読（`data/portfolio_positions.json` / `data/condor_pnl.json` を統合）

### Interface（凍結）

```python
# common/position_model.py（新設ファイル）

from dataclasses import dataclass, field
from typing import Optional
import datetime

@dataclass
class Position:
    """ブローカー横断の共通建玉表現。"""
    position_id:   str                    # ブローカー発行 ID
    symbol:        str                    # "US.SPY" 等
    side:          str                    # "LONG" / "SHORT"
    qty:           int
    avg_cost:      float
    current_price: float                  = 0.0
    unrealized_pnl: float                = 0.0
    margin_used:   float                  = 0.0
    delta:         float                  = 0.0
    tactic_name:   str                    = ""
    account_id:    str                    = ""
    opened_at:     Optional[datetime.datetime] = None
    broker:        str                    = "moomoo"   # "moomoo" / "tradovate"
    extra:         dict                   = field(default_factory=dict)

@dataclass
class PortfolioSnapshot:
    """全 Bot の合算ポジションスナップショット。"""
    positions:          list[Position]
    total_margin_used:  float
    total_delta:        float
    total_unrealized_pnl: float
    snapshot_at:        datetime.datetime
    open_position_count: int


# common/portfolio_aggregator.py — 既存関数（継承・シグネチャ変更禁止）

def aggregate_portfolio_risk() -> dict:
    """全 Bot 合算ポジション / 証拠金 / デルタ。"""

def check_loss_gates(capital_usd: float, limits: "RiskLimits") -> tuple[bool, str]:
    """
    日次 / 週次 / 月次 DD ゲートチェック。
    Returns: (allow: bool, reason: str)
    月次 DD 超過時は kill_switch.activate() を呼ぶ。
    """

def check_cross_bot_limits(capital_usd: float, limits: "RiskLimits") -> tuple[bool, str]:
    """Bot 間合算リスク上限チェック。"""
```

### Reconciliation Loop（新設計）
根拠: `data/research/self_healing_bot_20260422.md` §A-2

```python
# common/reconciler.py（新設ファイル）

def reconcile_once(
    desired: PortfolioSnapshot,
    actual:  PortfolioSnapshot,
    executor: "OrderExecutor",
) -> list[OrderRequest]:
    """
    desired vs actual を比較し、差分を埋める OrderRequest リストを返す。
    - 冪等: 同一 desired で複数回呼んでも副作用は 1 回
    - actual が desired と一致していれば空リストを返す
    - 返した OrderRequest は executor.submit() で実行する
    """
```

### ボトルネックと対処
- ブローカー API 遅延: reconcile は 10 秒間隔で実行（tight loop 禁止）
- 建玉 ID 不一致: `position_id` をブローカー固有 ID として保持し、`symbol + side + opened_at` で突合

---

## B5. Kill Switch

### 責務
per-tactic / per-symbol / per-account / portfolio の 4 層 trigger。解除は人間承認のみ。audit log の冪等性を保証する。

### 現行実装の確認済み仕様（継承）
根拠: `common/kill_switch.py` 実読

- グローバル Kill Switch: `FLAG_FILE = data/kill_switch.flag` の存在で発動
- `is_active()`: キャッシュ廃止・毎回ファイル確認（race condition 修正済み）
- フラグ削除検知: `_activated_at` による自動再発動（直接 rm でのバイパス防止）
- `FirmScopedKillSwitch`: firm 別 Kill Switch、ファイル永続化・30 分クールダウン

### 冪等性欠陥（4/22 redteam 発見・新設計で排除）

**欠陥の内容**: `activate()` は現在の状態確認なしに side effect（ファイル書き込み・Pushover 送信）を実行する。既に `ARMED` 状態で再度 `activate()` を呼ぶと audit log が重複し、Pushover が 2 回飛ぶ。

**新設計**: 発動前に状態確認し、既に発動済みなら audit log 1 件のみ追記してすぐ return。

### Interface（凍結）

```python
# common/kill_switch.py — 変更分のみ示す（既存関数シグネチャは維持）

def activate(reason: str = "manual", activator: str = "unknown") -> bool:
    """
    Kill Switch 発動。

    冪等保証:
      - 既に FLAG_FILE が存在する（= 発動済み）場合、
        audit log に "activate_idempotent_skip" を 1 件追記して return False。
        Pushover は送らない（重複通知防止）。
      - 初回発動時は audit + Pushover + FLAG_FILE 書き込みを実行して return True。

    Returns: True = 新規発動 / False = 既に発動済み（冪等スキップ）
    """

def deactivate(activator: str = "unknown") -> None:
    """
    Kill Switch 解除。人間承認時のみ呼ぶ。
    FLAG_FILE 削除 + _activated_at リセット + audit + Pushover priority=1。
    自動呼び出し禁止。
    """

def is_active() -> bool:
    """キャッシュなし・毎回ファイル確認。フラグ不正削除を検知して自動再発動。"""

def reason() -> str | None:
    """発動理由。発動していなければ None。"""

# --- 4 層 Kill Switch 参照 ---

# Layer 1: portfolio  → is_active() / activate() / deactivate()（グローバル）
# Layer 2: account    → FirmScopedKillSwitch（既存・継承）
# Layer 3: tactic     → 以下の新設 dataclass で管理
# Layer 4: symbol     → 以下の新設 dataclass で管理

@dataclass
class ScopedKillSwitchState:
    """per-tactic / per-symbol の Kill Switch 状態。"""
    scope_type:  str   # "tactic" / "symbol"
    scope_key:   str   # tactic 名 or symbol 名
    active:      bool
    reason:      str
    activated_at: str  # ISO8601

# ScopedKillSwitch は data/scoped_kill_switch.json に永続化する。
# activate / deactivate の冪等性は is_active() チェック後に書き込む設計で確保。
```

### Audit Log 冪等性
```
audit JSONL への書き込みルール:
  - activate(): 初回のみ event="activate" を書く。冪等スキップ時は event="activate_idempotent_skip"
  - deactivate(): 毎回 event="deactivate" を書く（解除は意図的操作のため重複でも記録価値あり）
  - 1 発動 = 1 audit エントリ（重複なし）が不変条件
```

---

## B6. 市場データ market data

### 責務
VIX / IV / Greeks / 板 / 出来高を一元取得し、cache layer で余分な API 呼び出しを削減する。

### 現行実装の確認済み仕様（継承）
根拠: `common/quote_context_manager.py` / `common/market_specs.py` / `data/specs/*_meta.json` の存在確認

`data/specs/` に既に以下のキャッシュファイルがある:
- `finnhub_api_meta.json`
- `moomoo_help_meta.json`
- `yahoo_finance_meta.json`

### Interface（凍結）

```python
# common/market_data.py（新設ファイル — 既存分散実装の統一窓口）

from dataclasses import dataclass
from typing import Optional
import datetime

@dataclass
class QuoteData:
    """単一シンボルの市場クォート。"""
    symbol:       str
    last:         float
    bid:          float
    ask:          float
    volume:       int
    iv:           Optional[float]   = None  # Implied Volatility (0.0-1.0)
    iv_rank:      Optional[float]   = None  # IVR (0-100)
    delta:        Optional[float]   = None
    gamma:        Optional[float]   = None
    theta:        Optional[float]   = None
    vega:         Optional[float]   = None
    fetched_at:   Optional[datetime.datetime] = None
    stale:        bool              = False  # キャッシュ利用時 True

@dataclass
class VixData:
    """VIX 関連指標。"""
    vix_current:  float
    vix_1w_ago:   Optional[float]  = None
    term_ratio:   Optional[float]  = None   # VIX9D/VIX の近似
    fetched_at:   Optional[datetime.datetime] = None


class MarketDataClient:
    """
    市場データ取得の統一 interface。
    内部でキャッシュを持ち、TTL 内は再取得しない。
    """

    def get_quote(self, symbol: str, max_age_sec: int = 30) -> QuoteData:
        """
        シンボルのクォートを返す。
        max_age_sec 以内のキャッシュがあれば再利用（stale=False）。
        キャッシュ切れ / 初回は実 API を叩く。
        取得失敗時: 最後の既知クォートを stale=True で返す。実装はログ必須。
        """

    def get_vix(self, max_age_sec: int = 60) -> VixData:
        """VIX を返す。stale 時は最終既知値を返す（raise しない）。"""

    def get_iv_rank(self, symbol: str, lookback_days: int = 252) -> Optional[float]:
        """IVR (0-100) を返す。データ不足時は None。"""
```

### Cache Layer
```
data/market_data_cache.json に保存:
  {
    "US.SPY": {"quote": QuoteData, "fetched_at": ISO8601},
    "VIX":    {"data": VixData,   "fetched_at": ISO8601}
  }

TTL:
  - quote: 30 秒（場中）/ 300 秒（場外）
  - vix: 60 秒
  - iv_rank: 3600 秒（計算コスト高）
```

### エラー経路
- API タイムアウト: stale=True の前回値を返す。Pushover 送信しない（Alert Fatigue 防止）
- 全キャッシュ失効 + API 断: `QuoteContextManager.get_level()` で circuit breaker 発動

---

## B7. 冪等性 idempotency

### 責務
UUID ベースの一意 ID 管理と二重実行防止パターン。B3（発注）だけでなく、reconcile / batch job / EOD summary 等にも適用する。

### Interface（凍結）

```python
# common/idempotency.py — 既存 IdempotencyStore を継承。追加関数のみ示す。

def make_job_key(job_name: str, date_str: str) -> str:
    """
    日次バッチ等の二重実行防止キー。
    例: make_job_key("eod_summary", "2026-04-22") → "job_eod_summary_20260422"
    TTL は IdempotencyStore デフォルト（86400s）に従う。
    """

def with_idempotency(store: IdempotencyStore, key: str):
    """
    context manager。key が既登録なら本体をスキップ。
    with with_idempotency(store, key):
        do_expensive_work()
    """
```

### 二重実行防止パターン（実装規約）
```
全ての「1日1回実行系」処理:
  key = make_job_key(job_name, today_str)
  if not store.check_and_register(key):
      log.info("already done today: %s", job_name)
      return
  # 以降が実際の処理

全ての発注:
  key = IdempotencyStore.make_key(signal_id, label)
  if not store.check_and_register(key):
      log.warning("duplicate order blocked: %s", key)
      return
  result = place_order(...)
  if not result.success:
      store.clear_key(key)   # 失敗したら次回再試行を許可
```

---

## B8. 仕様変更監視 spec drift watcher

### 責務
ブローカー / プロップファームのルール変更を自動検知し、LLM による YAML patch 提案 → ゆうさくさん承認 → 反映のフローを実現する。

### 現行資産（継承・拡張）
根拠: `data/research/broker_spec_drift_adaptation_20260422.md` §B-3

既存資産:
- `data/specs/finnhub_api_meta.json` / `moomoo_help_meta.json` / `yahoo_finance_meta.json`
- `atlas_rules.yaml` / `chronos_accounts.yaml` — YAML 駆動ルール定義済み

欠陥: 定期 diff なし・アラート経路が人力依存・プロップ規約の watcher なし

### Interface（凍結）

```python
# common/spec_drift_watcher/registry.yaml（設定）
# sources リストは broker_spec_drift_adaptation_20260422.md §E-2 参照

# common/spec_drift_watcher/__init__.py

class SpecDriftWatcher:
    def run_once(self) -> list["DriftEvent"]:
        """
        registry.yaml の全ソースを fetch → 前回 hash と比較 → 変化があれば DriftEvent を返す。
        副作用: data/specs/*_meta.json の content_hash / last_checked / breaking_change_flag を更新。
        """

@dataclass
class DriftEvent:
    source_name:      str          # registry.yaml の name
    change_summary:   str          # LLM 要約（自然言語）
    proposed_patch:   str          # YAML patch 案
    is_breaking:      bool         # 破壊的変更フラグ
    detected_at:      str          # ISO8601
    approval_required: bool = True # 常に True（自動反映禁止）
```

### 承認ゲート（人間承認のみ）
```
1. DriftEvent 検知 → send_alert(..., priority=1) で Pushover + ntfy 送信
2. proposed_patch を data/specs/drift_proposals/YYYYMMDD.yml に保存
3. ゆうさくさんが Pushover で「承認」返信 → hook で git commit & PR 自動作成
4. breaking=True の変更は CI block + kill_switch を SPEC_DRIFT モードに移行
5. rollback: 前版 hash で data/specs/*_meta.json を戻す → atlas_rules.yaml を前版 tag に戻す
```

### ボトルネックと対処
- LLM API 費用: 変化があった時のみ LLM 呼び出し（daily fetch は hash 比較のみ）
- HTML scraping 不安定: changedetection.io と同等の XPath / CSS selector 指定で安定化

---

## B9. 可観測性 observability

### 責務
3 層ヘルスチェック（startup / liveness / readiness）・Deadman Switch・logrotate・SLO/Error Budget・synthetic monitoring。

### 現行の欠陥（確認済み）
根拠: `data/research/sre_unattended_observability_20260422.md` §現状のエビデンス

- `logrotate.conf` のパスが `/var/log/spx_bot/*.log` を指しており、実ログ `data/logs/` は対象外
- Deadman Switch なし（44 時間放置が再発可能）
- SLO 定義なし

### Interface（凍結）

```python
# common/health_check.py（新設ファイル）

from enum import Enum

class HealthStatus(str, Enum):
    OK      = "ok"
    WARN    = "warn"
    FAIL    = "fail"

@dataclass
class HealthResult:
    status:   HealthStatus
    probe:    str           # "startup" / "liveness" / "readiness"
    message:  str
    checked_at: str         # ISO8601

def startup_probe() -> HealthResult:
    """
    起動完了チェック。
    確認項目: moomoo 接続 / Tradovate auth / state rebuild / kill_switch 状態
    FAIL の場合は起動を中断し send_critical で通知。
    """

def liveness_probe() -> HealthResult:
    """
    プロセス生存チェック。heartbeat ファイルのタイムスタンプ更新。
    確認項目: プロセスがデッドロックしていないか（heartbeat_file 更新で代替）
    """

def readiness_probe() -> HealthResult:
    """
    取引可能チェック。
    確認項目:
      - auth_budget remaining > 0
      - kill_switch.is_active() == False
      - market_open（calendar チェック）
      - quote_freshness < 30s
    FAIL の場合は新規発注をブロック（既存ポジション exit は許可）。
    """
```

### Deadman Switch 仕様
根拠: `data/research/self_healing_bot_20260422.md` §C-2 / `sre_unattended_observability_20260422.md` §3ツール3選

```
実装方針:
  - healthchecks.io に Atlas / Chronos 各 1 check を登録（無料枠）
  - Bot 本体: liveness_probe() 内で 1 分毎に healthchecks.io へ HTTP GET
  - 5 分間 no-ping → healthchecks.io が自動 escalate（Pushover / ntfy 送信）
  - Bot 本体とは独立プロセス（launchd cron 相当）で deadman.py を動かす

heartbeat ファイル:
  data/heartbeat/{bot_name}.txt
  内容: 最終生存確認タイムスタンプ（UNIX time）
  更新頻度: 60 秒
```

### logrotate 修正仕様
```
修正後の logrotate.conf（抜粋）:

/Users/yuusakuichio/trading/data/logs/*.log {
    daily
    rotate 7
    size 50M
    maxsize 100M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    dateext
    dateformat -%Y%m%d
}

Python 側: RotatingFileHandler(maxBytes=50*1024*1024, backupCount=5) を全 logger に適用
```

### SLO 定義（3 本）
根拠: `data/research/sre_unattended_observability_20260422.md` §SLO定義

| SLO | 定義 | 目標 |
|---|---|---|
| Atlas 戦術選択サイクル成功率 | 1 分以内に tick→戦術選択→kill_switch 判定が完了した割合 | 99% / 30 日 |
| Chronos webhook 送達率 | Tradovate webhook が 5xx/timeout 以外で応答した割合 | 99.5% / 30 日 |
| Heartbeat freshness | 両 Bot が 1 分以内に healthchecks.io に ping した割合 | 99.9% / 30 日 |

SLO 定義ファイル: `data/slo/slo_definitions.yaml`（新設）

---

## B10. 自己回復 self-healing

### 責務
Supervisor Tree（Erlang OTP 思想）・Circuit Breaker（pybreaker）・Bulkhead（障害分離）・Chaos Monday（週次 random kill）。

### 設計根拠
根拠: `data/research/self_healing_bot_20260422.md` §A-1, A-3, A-4

### Circuit Breaker（Phase 0 実装対象）

```python
# common/circuit_breaker.py（新設ファイル）
# 依存: pip install pybreaker

import pybreaker

class BreakerConfig:
    """Circuit Breaker の設定値。"""
    fail_max:      int    # 連続失敗でブレーカー OPEN
    reset_timeout: int    # 秒。OPEN 後この時間で HALF_OPEN
    fallback:      str    # "paper_mode" / "stale_cache" / "empty_list" / "skip"

# 保護対象とデフォルト設定（根拠: self_healing_bot_20260422.md §A-3）
BREAKER_CONFIGS: dict[str, BreakerConfig] = {
    "tradovate_auth":    BreakerConfig(fail_max=3, reset_timeout=3600, fallback="paper_mode"),
    "pushover_send":     BreakerConfig(fail_max=5, reset_timeout=300,  fallback="ntfy_gmail"),
    "moomoo_quote":      BreakerConfig(fail_max=5, reset_timeout=60,   fallback="stale_cache"),
    "finnhub_earnings":  BreakerConfig(fail_max=3, reset_timeout=1800, fallback="empty_list"),
}

def get_breaker(name: str) -> pybreaker.CircuitBreaker:
    """名前付きブレーカーを返す（シングルトン管理）。"""
```

### Supervisor Tree（Phase 1 実装対象）

```python
# common/supervisor.py（新設ファイル）

@dataclass
class WorkerSpec:
    name:         str
    target:       callable         # worker 関数
    max_restarts: int  = 5        # この回数超で FATAL escalation
    period_sec:   int  = 300      # max_restarts を数える時間窓
    restart_delay_sec: int = 5    # 再起動前の待機（thrash 防止）

class Supervisor:
    """
    multiprocessing.Process で worker を管理する簡易 Supervisor。
    Erlang OTP の one_for_one 方式を Python で実現。

    - 30 秒毎に heartbeat ファイルを確認
    - 3 連続 miss で SIGTERM → 5 秒 → SIGKILL → 再起動
    - max_restarts / period_sec 超で FATAL escalation
      (Pushover priority=2 + kill_switch.activate("supervisor_fatal"))
    """

    def start(self, specs: list[WorkerSpec]) -> None:
        """全 worker を起動して監視ループを開始する。"""

    def stop_all(self) -> None:
        """全 worker に SIGTERM を送る。"""
```

### Chaos Monday（Phase 1 実装対象）

```python
# scripts/chaos_monday.py

def run_chaos_kill(bot_name: str) -> None:
    """
    指定 Bot の worker プロセス 1 つを SIGKILL する。
    5 分以内に supervisor が再起動しなければ send_critical で通知。
    実行: 毎週日曜 03:00 JST（launchd plist で設定）。
    """
```

---

# Part C — 他 agent 用 DAG 情報 / 安定 API 一覧

**この Part に記載された API は Atlas / Chronos 実装時に変更禁止。**
変更が必要な場合は secretary agent 経由でゆうさくさん承認を得てから redteam が審査する。

## C1. Atlas が使う共通コア API（凍結）

| API | モジュール | 用途 |
|---|---|---|
| `AuthBudget.hard_block_if_exhausted("opend")` | `common.auth_budget` | moomoo 認証前チェック |
| `AuthBudget.record_attempt("opend", success=True)` | `common.auth_budget` | 認証後記録 |
| `kill_switch.is_active()` | `common.kill_switch` | 全発注前チェック（最優先） |
| `kill_switch.activate(reason, activator)` | `common.kill_switch` | 月次 DD 超過時など |
| `check_order(ctx, limits)` | `common.pre_trade_check` | 発注前 4 層チェック |
| `IdempotencyStore.make_key(signal_id, label)` | `common.idempotency` | 発注 key 生成 |
| `store.check_and_register(key)` | `common.idempotency` | 重複チェック |
| `store.clear_key(key)` | `common.idempotency` | 発注失敗時のリセット |
| `send_critical(title, message, priority=1)` | `common.pushover_client` | Warning 通知 |
| `send_batched(title, message)` | `common.pushover_client` | Caution 通知 |
| `send_silent(title, message)` | `common.pushover_client` | Advisory（ログのみ） |
| `load_limits(phase, capital_usd, paper)` | `common.risk_limits` | フェーズ別リスク閾値取得 |
| `determine_phase(capital_usd, ...)` | `common.risk_limits` | フェーズ自動判定 |
| `liveness_probe()` | `common.health_check` | heartbeat 更新（毎分） |
| `readiness_probe()` | `common.health_check` | 発注可否判定 |
| `MarketDataClient.get_quote(symbol)` | `common.market_data` | クォート取得 |
| `MarketDataClient.get_vix()` | `common.market_data` | VIX 取得 |

## C2. Chronos が使う共通コア API（凍結）

| API | モジュール | 用途 |
|---|---|---|
| `AuthBudget.hard_block_if_exhausted("tradovate_demo")` | `common.auth_budget` | Tradovate 認証前チェック |
| `AuthBudget.hard_block_if_exhausted("mffu")` | `common.auth_budget` | MFFU 認証前チェック |
| `get_firm_kill_switch()` | `common.kill_switch` | FirmScopedKillSwitch 取得 |
| `firm_ks.is_blocked(firm)` | `common.kill_switch` | firm 別発注ブロック判定 |
| `firm_ks.is_in_cooldown(firm)` | `common.kill_switch` | 解除後 30 分静寂窓判定 |
| `check_order(ctx, limits)` | `common.pre_trade_check` | 発注前 4 層チェック |
| `IdempotencyStore.make_key(signal_id, label)` | `common.idempotency` | 発注 key 生成 |
| `store.check_and_register(key)` | `common.idempotency` | 重複チェック |
| `send_critical(...)` / `send_batched(...)` / `send_silent(...)` | `common.pushover_client` | 通知（Atlas と同一 API） |
| `liveness_probe()` / `readiness_probe()` | `common.health_check` | heartbeat / 発注可否 |
| `get_breaker("tradovate_auth")` | `common.circuit_breaker` | Tradovate auth CB 取得 |

## C3. 実装順序の依存関係（DAG）

```
[既存・変更なし]
  common/auth_budget.py
  common/idempotency.py
  common/kill_switch.py  ← activate() の冪等性のみ修正
  common/pushover_client.py
  common/pre_trade_check.py
  common/risk_limits.py
  common/portfolio_aggregator.py

[新設・Phase 0（48h 以内）]
  common/health_check.py          ← liveness / readiness probe
  common/circuit_breaker.py       ← pybreaker wrapper
  scripts/chaos_monday.py         ← Chaos Monday
  logrotate.conf（パス修正）

[新設・Phase 1（1-2 週）]
  common/order_interface.py       ← OrderRequest / Leg / OrderResult
  common/market_data.py           ← MarketDataClient 統合窓口
  common/position_model.py        ← Position / PortfolioSnapshot
  common/reconciler.py            ← desired vs actual reconciliation
  common/supervisor.py            ← Supervisor Tree

[新設・Phase 2（1 ヶ月）]
  common/spec_drift_watcher/      ← ブローカー仕様変更監視
  data/slo/slo_definitions.yaml   ← SLO 定義
```

## C4. 変更禁止リスト（実装時に絶対変更しない関数・フィールド）

以下は Atlas / Chronos の既存コードがすでに呼び出している。シグネチャ変更は全体破損につながる。

| 関数 / クラス | 変更禁止の理由 |
|---|---|
| `check_order(ctx: OrderContext, limits) -> CheckResult` | Atlas / Chronos の全発注が依存 |
| `OrderContext` の全フィールド | `pre_trade_check.py` 内で直接参照 |
| `CheckResult.allow: bool` | 発注判定の最終フラグ |
| `kill_switch.is_active() -> bool` | `pre_trade_check.py` の最優先チェック |
| `AuthBudget.hard_block_if_exhausted(service: str)` | 認証前の物理ガード |
| `IdempotencyStore.check_and_register(key: str) -> bool` | 二重発注防止の根幹 |
| `RiskLimits` の全フィールド | `load_limits()` の返却型 |
| `FirmScopedKillSwitch.is_blocked(firm: str) -> bool` | Chronos 発注の最終判定 |
| `send_critical / send_batched / send_silent` の引数順序 | 全 Bot の通知呼び出し |

---

## 付録: 既存 hooks の分類（34 個）

根拠: `.claude/hooks/` 実読

| ファイル | 分類 | 判定 |
|---|---|---|
| `discipline_guard.sh` | ガバナンス | 有効・継続 |
| `blue_team_bias_detector.sh` | ガバナンス | 有効・継続 |
| `citation_quote_enforcer.sh` | ガバナンス | 有効・継続 |
| `claim_ledger_guard.py` | ガバナンス | 有効・継続 |
| `confidence_assertion_guard.sh` | ガバナンス | 有効・継続 |
| `deferral_language_guard.sh` | ガバナンス | 有効・継続 |
| `false_claim_detector.sh` | ガバナンス | 有効・継続 |
| `peer_review.sh` | ガバナンス | 有効・継続 |
| `selective_test_detector.sh` | ガバナンス | 有効・継続 |
| `premortem_gate.sh` | ガバナンス | 有効・継続 |
| `redteam_prehook.sh` | ガバナンス | 有効・継続 |
| `sns_truth_guard.sh` | ガバナンス | 有効・継続 |
| `sycophancy_detector.sh` | ガバナンス | 有効・継続 |
| `recommendation_guard.sh` | ガバナンス | 有効・継続 |
| `auth_budget_guard.py` | インフラ | **死コード（誰からも呼ばれない）** — codebase_metrics より確認 |
| `state_safety_guard.py` | インフラ | **死コード（誰からも呼ばれない）** — 同上 |
| `session_start_discipline_reload.sh` | セッション管理 | 有効・継続 |
| `session_start_market_specs_reload.sh` | セッション管理 | 有効・継続 |
| `inject_recent_corrections.sh` | セッション管理 | 有効・継続 |
| `prepend_pending_violations.sh` | セッション管理 | 有効・継続 |
| `prompt_reload_memory.sh` | セッション管理 | 有効・継続 |
| `generate_recent_corrections.py` | セッション管理 | 有効・継続 |
| `navigator_antipattern_detector.py` | ガバナンス | 有効・継続 |
| `pace_check_guard.sh` | ガバナンス | 有効・継続 |
| `self_confidence_probe.sh` | ガバナンス | 有効・継続 |
| `service_recommend_guard.sh` | ガバナンス | 有効・継続 |
| `time_estimate_sanity.sh` | ガバナンス | 有効・継続 |
| `url_verify_guard.sh` | ガバナンス | 有効・継続 |
| `memory_completion_tracker.sh` | セッション管理 | 有効・継続 |
| `stop_pending_check.sh` | セッション管理 | 有効・継続 |
| `stop_summary.sh` | セッション管理 | 有効・継続 |
| `stepwise_test_report.sh` | ガバナンス | 有効・継続 |
| `chronos_edit_spec_guard.sh` | インフラ | 有効・継続 |
| `proposal_bottleneck_stop_guard.sh` | ガバナンス | 有効・継続 |

**死コード 2 件**: `auth_budget_guard.py` / `state_safety_guard.py` は実行パスに接続されていない。Phase 1 のクリーンアップ対象（削除または接続）。

---

**仕様書終了**
保存先: `/Users/yuusakuichio/trading/data/specs/v2/common_spec_20260422.md`
凍結宣言: Part C の interface は Atlas / Chronos 実装開始以降、redteam 審査 + ゆうさくさん承認なしに変更禁止。
