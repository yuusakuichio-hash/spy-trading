# atlas_v3 仕様書 v3（2026-04-22 起草・2026-04-23 R1 改訂）

**位置付け**: Atlas 新実装（moomoo/futu経由 SPY/SPX オプション）・common_v3 依存

**起草元**:
- `data/specs/v2/atlas_spec_20260422.md`（知識抽出源・v2 draft）
- 既存 `spy_bot.py` 18,858 行（参照のみ・silent except 31.4%/MI 0.00 は継承しない）
- `data/specs/v3/common_spec_v3_20260422.md`（Interface 依存）

**改訂履歴**:
- 2026-04-22 初版
- 2026-04-23 R1: Redteam C-07（TacticEngine 3 分類）/ C-08（percentile 動的化）/ C-09（SPX whitelist 分離）反映
- 2026-04-23 R2: Redteam R1 再検証 R-01（gamma_scalp Type D 新設・Type A からの誤分類修正）反映
- 2026-04-23 R2a: Redteam R2 再検証 R2-02（TacticBase ABC 追加・dispatch 地獄 + silent AttributeError 封鎖）反映

---

## Part A: ゆうさくさん向け

### A1. Bot が何をするか
- 市場: 米国オプション（moomoo/futu 経由）
- 対象: SPY / SPX / QQQ / IWM / TSLA / NVDA / AAPL / MSFT / AMZN / META / GOOGL
- 戦術: 10 種類（環境適応型・動的選択）
- 時間: JST 22:20-05:10（平日・夏時間）
- 目標: 月利 5-8% 安定継続

### A2. 銘柄 × 戦術マトリクス（抜粋・詳細は v2 仕様書 A2 参照）

| 銘柄 | 適合戦術 | 制約 |
|---|---|---|
| SPY | 全戦術可 | PDT 4 回制限 |
| SPX | 全戦術可 | **moomoo paper 非対応**（whitelist 登録済・本番のみ） |
| QQQ/IWM | CS/IC/Butterfly/ORB | 流動性時間帯制限 |
| 個別 7 銘柄 | earnings / straddle / delta_hedge 中心 | 決算日配慮 |

### A3. 10 戦術カタログ

| # | 戦術 | 環境 | 方向 | 備考 |
|---|---|---|---|---|
| 1 | cs_sell | 低 VIX・方向性あり | 片方向 | 初心者向け |
| 2 | ic_sell | 中 VIX・IVR 中高 | 中立 | 安定収益 |
| 3 | butterfly | 低 IVR | 中立 | 低コスト |
| 4 | calendar_sell | IVR 高・コンタンゴ | 中立 | 時間価値 |
| 5 | strangle_sell | IVR 高・方向感なし | 中立 | 高リスク |
| 6 | straddle_buy | 高ボラ予想 | 両方向 | イベント前 |
| 7 | orb_1dte | 方向性ある朝 | 片方向 | ORB 手法 |
| 8 | delta_hedge | δ > 0.30 | 中立化 | 管理 |
| 9 | earnings_iv_crush | 決算日 | 中立 | IV 低下狙い |
| 10 | gamma_scalp | IVR 50%+ / RV<IV / VIX>20 | 両方向 | 2026-04 MVP |

### A4. 1 日の運用フロー（JST）
```
21:45  Pre-market check（hook で自動）
22:00  環境観測（VIX/IVR/VRP/GEX/term_ratio/bias）
22:20  市場 open・ORB 観測開始
22:50  戦術発動判定（dynamic selector）
~04:00 場中監視・delta_hedge・早期 exit
04:50  force_close（全残ポジ決済）
05:10  市場 close・日次 AAR 生成
```

### A5. リスクガード
- 日次最大損失: 動的算出（口座残高比）
- PDT 4 回ルール: `pdt_tracker` 連動
- FOMC / CPI / 雇用統計: pre-blackout
- 決算日: 個別銘柄のみ earnings 戦術で参戦・他戦術はスキップ
- Kill Switch: `common_v3/risk/kill_switch.py` 4 層

### A6. 期待される自律度
- **完全自動**: 発注・決済・delta_hedge・force_close・日次集計
- **承認要**: 新戦術採用・大幅パラメータ変更（月 1-2 件想定）

---

## Part B: Interface 凍結（Phase 2 Builder 参照）

### B1. `atlas_v3/core/engine.py`
**責務**: メインループ（CC ≤ 20）

```python
class AtlasEngine:
    def __init__(self, market_data: MarketDataClient, broker: BrokerClient): ...
    def run_session(self, session_id: str) -> SessionResult: ...
    def tick(self) -> list[OrderResult]: ...  # 1 tick (60秒) 処理
```

### B2. `atlas_v3/core/env_observer.py`（R1 改訂・Redteam C-08 対応）
**責務**: 環境観測スコア体系（動的算出・固定閾値禁止・percentile 自体も動的化）

```python
@dataclass
class MarketEnvironment:
    vix: float
    ivr_by_symbol: dict[str, float]
    vrp: float
    gex: float
    term_ratio: float
    bias: Literal["bull", "bear", "neutral"]

class PercentileSelector:
    """percentile 自体を資金フェーズ / VIX 領域から動的算出"""
    def select(self, metric: str, phase: str, vix: float) -> float:
        """例: Phase 1 低リスク = 30pct / Phase 4 攻め = 70pct / VIX>30 は保守側へ"""

class EnvObserver:
    def snapshot(self) -> MarketEnvironment: ...
    def get_dynamic_threshold(self, metric: str,
                              percentile_selector: PercentileSelector) -> float:
        """percentile を外部注入・固定引数禁止"""
```

**規律**: `get_dynamic_threshold(metric, 0.3)` の形式禁止（C-08 違反）。必ず `PercentileSelector` 経由で資金フェーズ・VIX 領域連動。

### B3. `atlas_v3/core/symbol_selector.py`
**責務**: マルチ銘柄動的選択（既存 common/symbol_selector.py の健全版を継承）

```python
class SymbolSelector:
    def select(self, env: MarketEnvironment) -> list[str]: ...
    def filter_by_tactic(self, symbols: list[str], tactic: str) -> list[str]: ...
```

### B4. `atlas_v3/core/strategy_selector.py`
**責務**: 戦術動的選択

```python
class StrategySelector:
    def select(self, env: MarketEnvironment, symbol: str) -> list[TacticDecision]: ...
    # 固定閾値禁止・環境データから動的算出
```

### B5. `atlas_v3/strategies/` 各戦術（R1 改訂・Redteam C-07 対応）

10 ファイル（`cs_sell.py` `ic_sell.py` ...）。戦術特性ごとに **4 種の Protocol** を使い分け（Boeing 737MAX MCAS 同型の単一 interface 強制を回避）。**全戦術は共通基底 `TacticBase` ABC を継承必須**（dispatch 地獄回避・silent AttributeError 経路封鎖）:

**共通基底 ABC（R2 追加・Redteam R2-02 対応）**
```python
from abc import ABC, abstractmethod

class TacticBase(ABC):
    """全戦術の共通基底・Engine から dispatch される必須メソッド"""
    @property
    @abstractmethod
    def tactic_type(self) -> Literal["enter_exit", "portfolio_reactive",
                                     "state_carrying", "hybrid"]: ...

    @property
    @abstractmethod
    def tactic_name(self) -> str: ...

    @abstractmethod
    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check・False なら戦術無効化（silent skip 禁止）"""
```

各 Protocol（EnterExitTactic / PortfolioReactiveTactic / StateCarryingTactic / HybridTactic）は `TacticBase` 継承を前提に宣言。Engine は `isinstance(tactic, TacticBase)` で統一 dispatch。

**Type A: Enter/Exit 型（単純エントリー/エグジット・単一 symbol）**
対象: cs_sell / ic_sell / butterfly / calendar_sell / strangle_sell / straddle_buy / gamma_scalp
```python
class EnterExitTactic(Protocol):
    def should_enter(self, env: MarketEnvironment, symbol: str) -> EntryDecision: ...
    def build_order(self, decision: EntryDecision) -> OrderRequest: ...
    def should_exit(self, position: Position, env: MarketEnvironment) -> ExitDecision: ...
    def build_exit_order(self, position: Position, decision: ExitDecision) -> OrderRequest: ...
```

**Type B: Portfolio 反応型（単一 symbol 入口を持たず・既存建玉に反応）**
対象: delta_hedge
```python
class PortfolioReactiveTactic(Protocol):
    def should_react(self, portfolio: PortfolioSnapshot,
                     env: MarketEnvironment) -> ReactionDecision: ...
    def build_orders(self, decision: ReactionDecision) -> list[OrderRequest]:
        """複数レッグ同時・hedge 対象を portfolio 単位で決定"""
```

**Type C: State-carrying 型（ORB range・event calendar 等の state 保持必須）**
対象: orb_1dte / earnings_iv_crush
```python
class StateCarryingTactic(Protocol):
    def observe(self, env: MarketEnvironment, market_data: MarketDataClient) -> None:
        """state 更新（09:30-09:45 ET ORB range 観測 / 決算日カレンダー更新）"""
    def should_enter(self, env: MarketEnvironment,
                     symbol_candidates: list[str]) -> list[EntryDecision]:
        """複数 symbol 同時評価（earnings で銘柄リスト取得）"""
    def build_order(self, decision: EntryDecision) -> OrderRequest: ...
    def should_exit(self, position: Position, env: MarketEnvironment) -> ExitDecision: ...
    def build_exit_order(self, position: Position, decision: ExitDecision) -> OrderRequest: ...
    def persist_state(self, storage: StorageBackend) -> None:
        """再起動耐性・state を B15 経由で永続化"""
```

**Type D: Hybrid 型（Portfolio 反応 + State 保持の両方必須）**
対象: gamma_scalp（IVR/RV/VIX state + portfolio delta/gamma 反応）
```python
class HybridTactic(Protocol):
    """State 保持 + Portfolio 反応の両方が必須な戦術"""
    def observe(self, env: MarketEnvironment, market_data: MarketDataClient) -> None:
        """IVR/RV/VIX history / gamma profile などの state 更新"""
    def should_react(self, portfolio: PortfolioSnapshot,
                     env: MarketEnvironment) -> ReactionDecision: ...
    def build_orders(self, decision: ReactionDecision) -> list[OrderRequest]: ...
    def should_exit(self, position: Position, env: MarketEnvironment) -> ExitDecision: ...
    def build_exit_order(self, position: Position, decision: ExitDecision) -> OrderRequest: ...
    def persist_state(self, storage: StorageBackend) -> None: ...
```

**戦術×Type マッピング（R2 修正・Redteam R-01 対応）**:
| 戦術 | Type | 理由 |
|---|---|---|
| cs_sell / ic_sell / butterfly / calendar_sell / strangle_sell / straddle_buy | A | 単純 enter/exit |
| delta_hedge | B | portfolio 単位反応 |
| orb_1dte | C | ORB range state 保持 |
| earnings_iv_crush | C | Finnhub 取得銘柄リスト保持 |
| gamma_scalp | **D** | IVR/RV state 保持 + portfolio delta/gamma 反応（R2 で Type A から修正・Flash crash 時ヘッジ遅延ベクトル封鎖） |

**規律**: 単一 interface 強制は silent failure 温床。Builder は戦術特性に応じて正しい Type を選択。gamma_scalp を Type A に入れると Flash crash 時のヘッジ遅延で口座吹き飛びリスク（R2 で修正済）。

### B6. `atlas_v3/risk/kelly_sizer.py`
**責務**: Kelly Criterion によるサイジング

```python
def compute_kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float: ...
def size_from_kelly(fraction: float, account_equity: float, phase: str) -> int: ...
```

### B7. `atlas_v3/risk/pdt_guard.py`
**責務**: PDT 4 回制限物理ガード

```python
class PDTGuard:
    def check_can_day_trade(self, account_equity: float, used_count: int) -> bool: ...
    def record_day_trade(self) -> None: ...
```

### B8. `atlas_v3/broker/moomoo_client.py`（R1 改訂・Redteam C-09 対応）
**責務**: moomoo/futu API 薄ラッパー

```python
class MoomooClient:
    def __init__(self, whitelist: SymbolWhitelist, mode: Literal["paper", "live"]): ...
    def place_order(self, request: OrderRequest) -> OrderResult:
        """発注前に whitelist.is_allowed(broker='moomoo', symbol=..., mode=...) 必須呼出"""
    def cancel_order(self, broker_order_id: str) -> bool: ...
    def get_positions(self) -> list[Position]: ...
    def get_account(self) -> AccountSnapshot: ...
```

**SPX whitelist 責務**: `common_v3/risk/symbol_whitelist.py` (B14b) に委譲。本 client は whitelist 登録を**実行時にチェックする責務**のみ持つ。起動時 `SymbolWhitelist.verify_at_startup()` 成功必須。

### B9. `atlas_v3/tools/ast_antipattern.py`（新規・Phase 2 実装必須）
**責務**: pre-commit hook で実装コードの規律違反を検知
- silent except（raise なし）
- CC > 20
- LoC > 50 （関数）/ LoC > 300（class）
- type annotation 欠落
- assert ゼロ

---

## Part C: DAG・実装順（common_v3 完成後）

```
1. atlas_v3/core/env_observer.py    (純データ取得)
2. atlas_v3/core/symbol_selector.py (純ロジック)
3. atlas_v3/core/strategy_selector.py
4. atlas_v3/risk/kelly_sizer.py
5. atlas_v3/risk/pdt_guard.py
6. atlas_v3/strategies/* (10 戦術・並列可)
7. atlas_v3/broker/moomoo_client.py (既存 futu_utils.py 参照)
8. atlas_v3/core/engine.py (最後に統合)
9. atlas_v3/tests/* (TDD 各ステップで)
```

---

## Part D: 凍結宣言
B1-B9 の Interface は Phase 2 実装時変更禁止。

## Part E: テスト要件
common_v3 Part E と同じ（cov 85%+ / silent except 禁止 / mutation 75%+ 等）。

## Part F: 未確定事項（Phase 2 着手前に要調査）
- moomoo paper の SPX 扱い最新仕様
- 個別 7 銘柄の earnings 日程（Finnhub 連動）
- gamma_scalp MVP のフィールド実測データ

## Part G: 関連
- `data/specs/v3/common_spec_v3_20260422.md`
- `data/specs/v2/atlas_spec_20260422.md`（知識抽出源）
- `atlas_v3/README.md`（scaffold 段階）
