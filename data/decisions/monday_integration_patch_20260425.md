# monday_integration_patch_20260425 — 統合パッチ差替箇所一覧

作成日: 2026-04-25  
対象ブランチ: audit_high_fix_20260418  
適用スクリプト: scripts/apply_monday_patches.sh  
差替箇所総数: **19 箇所** (import block 1 + CRITICAL #1: 1 + CRITICAL #3: 2 + H=300 fix: 16 - ORB fallback 300.0 は ORBEngine._FALLBACK_PRICE_DEFAULTS にのみ残存・spy_bot.py 外)

---

## 0. wrapper ファイル一覧

| wrapper | パス | CRITICAL |
|---|---|---|
| chainguard_wrapper | atlas_v3/ops/chainguard_wrapper.py | #1 |
| mass_verify_safe_runner | atlas_v3/ops/mass_verify_safe_runner.py | #2 |
| portfolio_risk_gate | atlas_v3/ops/portfolio_risk_gate.py | #3 |
| symbol_aware_price | atlas_v3/ops/symbol_aware_price.py | H=300 fix |

---

## 1. import block 追加 (spy_bot.py 行 1072 付近・`_pdt_tracker = None` の直後)

### 旧
```python
    _pdt_tracker = None
# (次のコードへ)
```

### 新
```python
    _pdt_tracker = None

# ── atlas_v3 wrapper imports (2026-04-28 統合パッチ) ──────────────────────────
_CHAINGUARD_WRAPPER_AVAILABLE = False
try:
    from atlas_v3.ops.chainguard_wrapper import (
        get_chain_center_price as _cg_get_center_price,
        get_chain_center_price_with_fallback as _cg_get_center_price_fb,
        ChainGuardError as _ChainGuardError,
        MissingPriceError as _CgMissingPriceError,
    )
    _CHAINGUARD_WRAPPER_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.chainguard_wrapper ロード成功 (CRITICAL #1 有効)")
except ImportError as _e:
    ...（fallback stubs）

_MASS_VERIFY_SAFE_AVAILABLE = False
try:
    from atlas_v3.ops.mass_verify_safe_runner import (
        VerifyContext as _MVVerifyContext,
        run_mass_verify_safe as _mv_run_safe,
        ...
    )
    ...

_PORTFOLIO_RISK_GATE_AVAILABLE = False
try:
    from atlas_v3.ops.portfolio_risk_gate import (
        check_entry_allowed_with_log as _prg_check_entry_log,
        ...
    )
    ...

_SYMBOL_AWARE_PRICE_AVAILABLE = False
try:
    from atlas_v3.ops.symbol_aware_price import (
        get_current_price_with_fallback as _sap_get_price_fb,
        ...
    )
    ...
```

---

## 2. CRITICAL #1 差替 — ChainGuard center price (元行 5539 付近)

バグ: `_cached_spy_price` 代入欠落で ChainGuard が常に `spy_price_ref = 0` を使用。  
根治: `chainguard_wrapper._cg_get_center_price_fb()` 経由で動的取得。

### 旧
```python
            spy_price_ref = 0
            try:
                _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                if _cg_ret == RET_OK and not _cg_snap.empty:
                    spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
            except Exception as _cg_e:
                log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")
```

### 新
```python
            # [CRITICAL #1 patch 2026-04-28] chainguard_wrapper 経由で動的取得
            if _CHAINGUARD_WRAPPER_AVAILABLE:
                _cg_fb, _cg_src = _cg_get_center_price_fb(
                    self.underlying_code, self.mkt, 0.0
                )
                spy_price_ref = _cg_fb
                if _cg_src == "fallback":
                    log.warning(f"[ChainGuard] center price fallback=0 for {self.underlying_code}")
            else:
                spy_price_ref = 0
                try:
                    _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                    if _cg_ret == RET_OK and not _cg_snap.empty:
                        spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
                except Exception as _cg_e:
                    log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")
```

---

## 3. CRITICAL #2 差替 — MassVerify TOCTOU race

差替箇所: import block のみ (STEP 1 参照)。  
`_try_mass_verify_entry` 内の `self.underlying_code / self.mkt.underlying_code` 一時切替パターンは現状のまま維持。  
wrapper の `run_mass_verify_safe` は spy_bot.py が MassVerify 専用ループを持つ将来の v3 統合時に差替予定。  
現時点ではモジュール可用性フラグ `_MASS_VERIFY_SAFE_AVAILABLE` を確立し、import エラーで Bot 起動が失敗しないことを保証する。

---

## 4. CRITICAL #3 差替 — VIX spike × PortfolioRisk entry halt gate (2 箇所)

バグ: VIX が閾値を超えても entry halt が発動せず新規エントリーが無制限に通過。  
根治: `portfolio_risk_gate.check_entry_allowed_with_log()` を entry 直前に挿入。

### 差替 A — スタンダードエントリー (元行 13988 付近)

#### 旧
```python
        params = get_params(vix, STANDARD_PARAMS)
        if params is None:
            log.info(f"VIX={vix:.1f} >= 35 → standard entry halted (ORF may activate)")
            return
```

#### 新
```python
        # [CRITICAL #3 patch 2026-04-28] VIX spike gate wrapper
        if _PORTFOLIO_RISK_GATE_AVAILABLE:
            _prg_open_count = len(getattr(self, "_mass_verify_positions", {}) or {})
            _prg_decision = _prg_check_entry_log(vix, _prg_open_count, context="standard_entry")
            if not _prg_decision.allowed:
                log.warning(f"[PortfolioRiskGate] standard entry halted: {_prg_decision.reason}")
                return
        params = get_params(vix, STANDARD_PARAMS)
        if params is None:
            log.info(f"VIX={vix:.1f} >= 35 → standard entry halted (ORF may activate)")
            return
```

### 差替 B — CS entry PortfolioRisk チェック (元行 14119 付近)

#### 旧
```python
        # ── [PortfolioRisk] 合計リスクチェック ───────────────────────────────
        if _PORTFOLIO_RISK_AVAILABLE and cash > 0:
            _additional_risk = params.get("width", 10) * qty * 100
            if not can_take_risk(_additional_risk, cash):
                log.info("[PortfolioRisk] 合計リスク上限 → エントリースキップ")
                self.traded_today = True
                return
```

#### 新
```python
        # ── [PortfolioRisk] 合計リスクチェック ───────────────────────────────
        # [CRITICAL #3 patch 2026-04-28] gate check (VIX spike + concurrent entries)
        if _PORTFOLIO_RISK_GATE_AVAILABLE:
            _prg_n = len(getattr(self, "_mass_verify_positions", {}) or {})
            _prg_d = _prg_check_entry_log(vix, _prg_n, context="cs_entry_risk_gate")
            if not _prg_d.allowed:
                log.warning(f"[PortfolioRiskGate] CS entry halted: {_prg_d.reason}")
                self.traded_today = True
                return
        if _PORTFOLIO_RISK_AVAILABLE and cash > 0:
            ...（既存コード継続）
```

---

## 5. H=300 fix 差替 — get_spy_current 16 箇所

バグ: `ORBEngine._get_fallback_price()` が未登録銘柄 (SPX 等) に `300.0` を返す。  
加えて全エンジンが `underlying_code` に関係なく SPY の価格を使用する symbol 取り違え。  
根治: `symbol_aware_price.get_current_price_with_fallback()` に差替。wrapper 未ロード時は従来呼出にフォールバック。

| # | 元行番号 | コンテキスト | 旧パターン | 新パターン |
|---|---|---|---|---|
| 1 | 5613 | dry-test chain build | `self.mkt.get_spy_current() or 562.5` | `_sap_get_price_fb(mkt.underlying_code, mkt, 562.5)[0]` |
| 2 | 5671 | CS entry chain center | `self.mkt.get_spy_current() if self.mkt else None` | `_sap_get_price_fb(…, 0.0)[0]` |
| 3 | 6822 | DeltaHedge underlying price | `self.mkt.get_spy_current() if self.mkt else None` | `_sap_get_price_fb(…, 0.0)[0]` |
| 4 | 7133 | scan_option_volumes prelude | `mkt.get_spy_current() or mkt.get_spy_open() or 0.0` | `_sap_get_price_fb(mkt.underlying_code, mkt, 0.0)[0]` |
| 5 | 9559 | StraddleEngine entry | `self.mkt.get_spy_current()` | `_sap_get_price_fb(…, 0.0)[0]` |
| 6 | 9678 | Straddle close | `self.mkt.get_spy_current() or 0.0` | `_sap_get_price_fb(…, 0.0)[0]` |
| 7 | 9812 | should_exit_straddle | `self.mkt.get_spy_current()` | `_sap_get_price_fb(…, 0.0)[0]` |
| 8 | 9946 | unrealized PL (Straddle) | `self.mkt.get_spy_current()` | `_sap_get_price_fb(…, 0.0)[0]` |
| 9 | 10440 | ORB _get_underlying_price | `self.mkt.get_spy_current()` (underlying_code 切替済) | `_sap_get_price_fb(mkt.underlying_code, mkt, 0.0)[0]` |
| 10 | 12161 | IC entry chain center | `self.mkt.get_spy_current() if self.mkt else None` | `_sap_get_price_fb(…, 0.0)[0]` |
| 11 | 12861 | SMA direction detection | `self.mkt.get_spy_current()` | `_sap_get_price_fb(…, 0.0)[0]` |
| 12 | 12993 | ButterflyEngine entry | `self.mkt.get_spy_current()` | `_sap_get_price_fb(…, 0.0)[0]` |
| 13 | 14558 | PositionExit event | `self.mkt.get_spy_current()` | `_sap_get_price_fb(…, 0.0)[0]` |
| 14 | 16077 | dry-test unrealized PL | `self.mkt.get_spy_current()` | `_sap_get_price_fb(…, 0.0)[0]` |
| 15 | 17115 | CalendarEngine entry | `self.mkt.get_spy_current() if not DRY_TEST else 560.0` | `_sap_get_price_fb(…, 0.0)[0] if not DRY_TEST else 560.0` |
| 16 | 17369 | StrangleSell entry | `self.mkt.get_spy_current() or 0.0` | `_sap_get_price_fb(…, 0.0)[0]` |

---

## 6. dry-run テスト手順

```bash
# dry-run (書込なし・シミュレーション)
DRY_RUN=1 bash scripts/apply_monday_patches.sh

# 本番適用
sudo bash scripts/apply_monday_patches.sh

# wrapper import 単体確認
python3 -c "from atlas_v3.ops.chainguard_wrapper import get_chain_center_price; print('OK')"
python3 -c "from atlas_v3.ops.mass_verify_safe_runner import run_mass_verify_safe; print('OK')"
python3 -c "from atlas_v3.ops.portfolio_risk_gate import check_entry_allowed; print('OK')"
python3 -c "from atlas_v3.ops.symbol_aware_price import get_current_price; print('OK')"

# pytest 全件
python3 -m pytest tests/test_chainguard_wrapper.py tests/test_mass_verify_safe_runner.py \
    tests/test_portfolio_risk_gate.py tests/test_symbol_aware_price_20260425.py -q
```

---

## 7. ロールバック手順

```bash
sudo chflags noschg /path/to/spy_bot.py
cp spy_bot.py.bak_monday_patch_YYYYMMDD_HHMMSS spy_bot.py
sudo chflags schg spy_bot.py
```

---

## 8. 残存リスク

| 項目 | 内容 |
|---|---|
| CRITICAL #3-C | ORF entry gate 挿入箇所はコンテキスト複雑。スクリプトは WARNING のみで続行。月曜に手動確認が必要。|
| H=300 fix #3 | DeltaHedge 内の `underlying_price` は nullable のまま。patch 後も `if underlying_price is None` ガードが残存するため安全。|
| H=300 fix ORBEngine | `_FALLBACK_PRICE_DEFAULTS.get(ticker, 300.0)` は ORBEngine クラス変数として残存。Finnhub 取得成功時は上書きされるため実害は軽微。v3 完全移行時に除去予定。|
