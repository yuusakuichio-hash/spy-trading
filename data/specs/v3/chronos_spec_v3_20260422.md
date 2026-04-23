# chronos_v3 仕様書 v3（2026-04-22 起草・2026-04-23 R1 改訂）

**位置付け**: Chronos 新実装（CME 先物・MFFU プロップ経由）・common_v3 依存

**起草元**:
- `data/specs/v2/chronos_spec_20260422.md`（知識抽出源・v2 draft）
- 既存 `chronos_bot.py` 4,724 行（参照のみ・`MFFUBot` class 未定義問題を継承しない）
- `memory/project_session_20260421_night_complete.md`（Oliver 公式許可・TradersPost 経路確定）

**改訂履歴**:
- 2026-04-22 初版
- 2026-04-23 R1: Redteam C-10（MFFU 動的値契約）/ C-11（Part F 解消）反映
- 2026-04-23 R2: Redteam R1 再検証 R-04（mffu_flex.yaml bootstrap 手順追加）反映
- 2026-04-23 R2b: Gemini R2 再検証 MUST-FIX（dry_run モード契約 + 本番 False 強制 AST hook）反映

---

## Part A: ゆうさくさん向け

### A1. Bot が何をするか
- 市場: CME 先物（MES / MNQ / ES / NQ / M2K / MYM 等）
- プロップ: MFFU Flex（既購入）/ 将来 Tradeify / Apex / Bulenox
- 戦術: 11 種類
- 時間: 月 07:00 - 土 06:00 JST（24h・毎日 06:00-07:00 休止）
- 目標: MFFU Flex payout 月 60 万円級

### A2. マルチ銘柄

| 銘柄 | tick size | multiplier | 主要戦術 |
|---|---|---|---|
| MES | 0.25 | $5 | ORB / session / asia_range |
| MNQ | 0.25 | $2 | ORB / gap_fill |
| ES | 0.25 | $50 | vix_term / es_nq_spread |
| NQ | 0.25 | $20 | volume_profile |
| M2K | 0.1 | $5 | range_break |
| MYM | 1.0 | $0.50 | economic_event |

### A3. 11 戦術カタログ（既存 `chronos_strategy_selector.py` 継承）

| # | 戦術 | 環境 |
|---|---|---|
| 1 | ORB | 朝・方向性 |
| 2 | VIX term structure | VIX 構造変化 |
| 3 | ES-NQ spread | 相対価格 |
| 4 | session strategy | 時間帯別 |
| 5 | asia range fade | アジア時間 |
| 6 | gap fill | 窓埋め |
| 7 | volume profile | 出来高 |
| 8 | economic event | 経済イベント |
| 9 | range break | レンジ突破 |
| 10 | cumulative delta | 買い/売り圧 |
| 11 | liquidity sweep | 流動性掃引 |

### A4. MFFU Flex ルール（絶対遵守）
- Profit target: 動的（契約時点の最新値を `chronos_rules_plugin/mffu_flex.py` 参照）
- Max Loss Limit: 動的
- Consistency Rule 50%（Eval 期間のみ）
- Weekend Hold 禁止（金曜 16:00 ET 全決済）
- HFT 禁止（200+ trades/日）
- 自動化: **semi-managed webhook→Python→REST 公式許可**（2026-04-21 Oliver 回答）

### A5. 他プロップ比較（将来対応）
- Tradeify Lightning $50K: Instant Funded / 90/10 split / $2,000 DD
- Apex: EIN+W-8BEN-E で法人契約可
- Bulenox: 同上
- Topstep: US-LLC 限定で日本 GK 不可

### A6. 時系列運用フロー（JST 24h）
```
月 07:00  週始動・週初ポジション check
随時      ORB（アジア・欧州・US 各 session）
随時      戦術切替（env_observer baseline）
金 16:00 ET (土 05:00 JST)  全決済
日       停止
月 06:00-07:00 休止（CME 休場）
```

### A7. リスクガード 5 層
1. 銘柄別 DD limit
2. 戦術別 Sharpe 劣化トリガー
3. アカウント別 MFFU rule
4. portfolio 合算 limit
5. 人間監視（Andon）

---

## Part B: Interface 凍結（Phase 2 Builder 参照）

### B1. `chronos_v3/core/engine.py`
```python
class ChronosEngine:
    def __init__(self, broker: BrokerClient, prop_rules: PropRules,
                 market_data: MarketDataClient): ...
    def run_forever(self) -> None: ...
    def tick(self) -> list[OrderResult]: ...
```

### B2. `chronos_v3/core/session_manager.py`
```python
class SessionManager:
    def current_session(self) -> Literal["asia", "europe", "us_premarket", "us_rth",
                                         "us_after", "weekend_closed"]: ...
    def is_market_open(self) -> bool: ...
    def next_force_close(self) -> datetime: ...  # 金曜 16:00 ET
```

### B3. `chronos_v3/core/env_observer.py`
Atlas と同パターン（VIX term structure / ES-NQ spread / volume profile）

### B4. `chronos_v3/strategies/` 11 戦術
各戦術は Atlas と同じ `TacticEngine` Protocol に準拠。

### B5. `chronos_v3/prop/mffu_flex.py`（R1 改訂・Redteam C-10 対応）

**動的値参照の契約（循環依存解消）**:
- Profit Target / Max Loss は `data/prop_rules/mffu_flex.yaml` を唯一の真実源とする（既存 `chronos_rules_plugin/mffu_flex.py` ハードコード定数 `_EVAL_PROFIT_TARGET_USD` は Phase 2 で yaml 参照に置換）
- yaml 更新は `common_v3/spec_drift/watcher.py` が MFFU 公式サイト scrape で検知し、Patch 生成 → 人間承認ゲート → yaml 更新
- 起動時必須検証: `MFFUFlexRules.verify_yaml_freshness()` で yaml mtime > 30 日経過なら EICAS Warning
- 契約未確認時の fallback: 明示的に `None` 返却し呼出元で `MFFURuleMissingError` raise（silent default 禁止）

```python
class MFFUFlexRules:
    def __init__(self, yaml_path: Path, storage: StorageBackend): ...

    def verify_yaml_freshness(self) -> tuple[bool, int]:
        """True=OK / False=30日超古 / int=経過日数"""

    def get_profit_target(self, account_type: Literal["eval", "funded"]) -> float:
        """yaml 読取・存在しなければ MFFURuleMissingError"""

    def get_max_loss(self, account_type: str) -> float: ...

    def check_can_trade(self, account: str, pending_order: OrderRequest) -> tuple[bool, str]: ...
    def check_daily_loss(self, account: str, current_pnl: float) -> bool: ...
    def check_weekend_hold(self, now: datetime) -> bool: ...
    def check_consistency_rule(self, account: str, daily_pnls: list[float]) -> bool: ...

class MFFURuleMissingError(Exception):
    """yaml 未更新 / 値欠落 / 旧式フォーマット"""
```

**更新頻度**: spec_drift watcher が週次 scan / 差分発生時 EICAS Warning → ゆうさくさん承認 → yaml 更新

**yaml bootstrap 手順（R2 新設・Redteam R-04 対応）**:
Phase 2 Builder が chronos_v3 着手時、`data/prop_rules/mffu_flex.yaml` が物理不在なら Chronos 起動不能。以下の手順で初期化:

1. ディレクトリ作成: `mkdir -p data/prop_rules/`
2. 初期 yaml 書込（MFFU 公式 2026-04-20 確認値・契約時点で再確認必須）:
   ```yaml
   # data/prop_rules/mffu_flex.yaml
   schema_version: "1.0"
   source: "MFFU Flex 公式 + 契約文書"
   verified_at: "2026-04-20"
   verified_by: "yuusaku"
   eval:
     profit_target_usd: null    # 契約時点で MFFU ダッシュボードから転記必須
     max_loss_limit_usd: null   # 同上
     consistency_rule_pct: 50
     weekend_hold_forbidden: true
     hft_threshold_trades_per_day: 200
   funded:
     max_loss_limit_usd: null
     consistency_rule_pct: null  # Funded 段階では未適用の可能性
     weekend_hold_forbidden: true
     hft_threshold_trades_per_day: 200
   force_close_et: "16:00"  # 金曜
   ```
3. `chronos_v3/prop/mffu_flex.py` 起動時に `MFFUFlexRules.verify_yaml_freshness()` + null 値検知で `MFFURuleMissingError` raise（silent default 拒否）
4. 契約後・ゆうさくさんが null 値を MFFU ダッシュボードから転記
5. spec_drift watcher が以後の変更を自動検知

**dry_run モード契約（R2b 追加・Gemini R2 MUST-FIX 対応・C-10 再発防止）**:
Builder が yaml 不在 or null 値で Chronos を起動した場合の退避経路として dry_run モードのみ許可。本番発注は物理ブロック。

```python
class MFFUFlexRules:
    def __init__(self, yaml_path: Path, storage: StorageBackend,
                 dry_run: bool = False):
        """dry_run=True: yaml 欠落/null 許容・本番発注ブロック・EICAS Caution 継続発出"""

    @property
    def mode(self) -> Literal["live", "dry_run"]: ...

    def check_can_trade(self, account: str, pending_order: OrderRequest) -> tuple[bool, str]:
        """dry_run 時は (False, 'dry_run mode: 本番発注不可') を強制返却"""
```

**本番 dry_run=False 強制 AST hook**（`.claude/hooks/mffu_dry_run_guard.sh`）:
- `MFFUFlexRules(..., dry_run=True)` を本番設定ファイル（`config/prod/*.yaml`）で検知 → PreToolUse で block
- `ENVIRONMENT=prod` 環境で起動時 `mode=='dry_run'` なら即 `sys.exit(1)` + EICAS Warning

これにより Builder が yaml 未完成のまま「とりあえずハードコード shadow で起動」する C-10 再発経路を物理封鎖。

### B6. `chronos_v3/prop/tradeify.py` / `apex.py` / `bulenox.py`
各プロップの rules 実装（将来対応・skeleton のみ）

### B7. `chronos_v3/broker/traderspost_webhook.py`
```python
class TradersPostClient:
    def send_signal(self, strategy_uuid: str, signal: dict) -> dict: ...
    # 4/21 21:44 初約定済
```

### B8. `chronos_v3/broker/tradovate_client.py`
```python
class TradovateClient:
    def authenticate(self) -> str: ...  # auth_budget 連動
    # Tradovate API rate limit 5/hour
    def get_positions(self) -> list[Position]: ...
    def get_account_summary(self) -> dict: ...
```

---

## Part C: DAG 実装順

```
1. chronos_v3/core/session_manager.py
2. chronos_v3/core/env_observer.py
3. chronos_v3/prop/mffu_flex.py (既存 yaml 継承)
4. chronos_v3/broker/traderspost_webhook.py (既存実績継承)
5. chronos_v3/broker/tradovate_client.py (auth_budget 連動)
6. chronos_v3/strategies/orb.py (最初)
7. chronos_v3/strategies/* (残り 10)
8. chronos_v3/core/engine.py (統合)
9. chronos_v3/prop/tradeify|apex|bulenox (skeleton)
```

---

## Part D: 凍結宣言
B1-B8 の Interface は Phase 2 変更禁止。

## Part E: テスト要件
common_v3 Part E と同じ。MFFU ルール遵守の property-based testing 必須。

## Part F: 未確定事項（Phase 2 前に要調査）
- MFFU Flex の最新 Profit Target / Max Loss 値（契約時点で確認）
- TradersPost 8 日以降の paper auto-submit 挙動
- 他プロップの自動化許可条件

## Part G: 関連
- `data/specs/v3/common_spec_v3_20260422.md`
- `data/specs/v2/chronos_spec_20260422.md`（知識抽出源）
- `memory/project_session_20260421_night_complete.md`（Oliver 許可）
- `chronos_rules_plugin/mffu_flex.py`（既存 yaml 継承元）
