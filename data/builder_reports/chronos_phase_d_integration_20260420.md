# Phase D 統合レポート — chronos_bot.py

日時: 2026-04-20
担当: builder (Sonnet 4.6)

## 完了条件チェック

| 条件 | 結果 |
|------|------|
| grep で select_futures_strategy_with_plan がヒット | PASS (行 157, 1967, 3570) |
| grep で get_kelly_fraction がヒット | PASS (行 175, 190, 194, 199, 1374) |
| grep で _PLAN_TACTIC_PROFILES がヒット | chronos_strategy_selector.py 側に定義。chronos_bot.py からは select_futures_strategy_with_plan 経由で間接参照（直接 import は不要） |
| Phase D loaded ログ出力 | PASS — `Phase D loaded: common.kelly_sizer / get_kelly_fraction` |
| 既存テスト回帰ゼロ | PASS — 1192 passed (test_hmm_regime の1件は変更前から失敗の既存障害) |

## 変更内容

### 1. インポート追加 (chronos_bot.py)

- `chronos_strategy_selector` から `select_futures_strategy_with_plan` を追加インポート
- `common.kelly_sizer` から `KellySizer`, `calc_plan_kelly` をインポートし `get_kelly_fraction` ラッパーを定義
- `KELLY_SIZER_AVAILABLE` フラグで optional import を保護

### 2. `ChronosBot._plan_id` プロパティ新設

- `self._firm` / `self._plan` / `self._phase_for_prop` から plan_id 文字列を動的構築
- 例: `firm=mffu, plan=flex_50k, phase=evaluation` → `"flex_eval"`
- マッピング表: flex/rapid/pro/builder/tradeify/apex に対応

### 3. `env_dict` / `env` への `plan_id` 追加

2箇所に追加:
- `_premarket_setup` 内 `env_dict["plan_id"] = self._plan_id`
- `get_active_strategies` 内 `env["plan_id"] = self._plan_id`
- 同時に `trade_count_today` と `open_positions` も格納

### 4. `select_futures_strategy` → `select_futures_strategy_with_plan` に置換

2箇所を置換:
- 行 1967: `_premarket_setup` 内
- 行 3570: `get_active_strategies` 内

### 5. `FuturesORBStrategy._calc_contracts` に `plan_id` パラメータ追加

- `plan_id: Optional[str] = None` を新規引数として追加
- `KELLY_SIZER_AVAILABLE and plan_id` のとき `get_kelly_fraction(plan_id)` を優先使用
- フォールバック: `spy_bot.calc_kelly_fraction` (既存動作を維持)

### 6. `FuturesORBStrategy.plan_id` 属性追加

- `__init__` で `self.plan_id: str = ""` を初期化
- `ChronosBot._phase_for_prop` 更新箇所で `self.orb.plan_id = self._plan_id` を同期

### 7. `_calc_contracts` 呼び出しに `plan_id=self.plan_id or None` を追加

## テスト結果

```
tests/test_chronos_phase_d_20260420.py: 61 passed
tests/ (全体): 1192 passed, 5 skipped, 1 failed (HMM既存障害)
回帰: 0件
```

## 検証コマンド

```bash
grep -nE "select_futures_strategy_with_plan|get_kelly_fraction|_PLAN_TACTIC_PROFILES" chronos_bot.py
python3 chronos_bot.py --dry-run 2>&1 | grep "Phase D loaded"
```
