# Chaos Engineering Report — 20260418_200850

**モード**: full
**結果**: 12/12 シナリオ 合格

## 理論
Netflix Chaos Monkey (2011) 思想。「壊れないものを作るのではなく、壊れても回復できるシステムを作る」。

## シナリオ別結果

### [PASS] deep_itm_naked_long
- 説明: SPXW 5400C @$1697.30 Deep ITM 発注試行
- 期待: L1 拒否 (max_option_price=50.0)
- 重要度: OK
- 実際 layer=L1 allow=False

### [PASS] symbol_contamination
- 説明: underlying 切替中に US.GME chain が混入 → L1 whitelist 拒否期待
- 期待: L1 拒否 (symbol_whitelist)
- 重要度: OK
- 実際 layer=L1 allow=False

### [PASS] qcm_3x_disconnect
- 説明: Quote Context 3回連続切断 → level 3 → 新規エントリー停止
- 期待: level=3 でエントリー拒否 (QCM layer)
- 重要度: OK

### [PASS] kill_switch_activation
- 説明: 日次 -5% 損失 → Kill Switch 発動 → 全発注停止確認
- 期待: 全発注が KILL layer でブロック
- 重要度: OK

### [PASS] fat_finger_qty
- 説明: qty=9999枚発注試行 → L1 max_qty_per_order=50 拒否期待
- 期待: L1 拒否 (qty 9999 > 50)
- 重要度: OK
- 実際 layer=L1 allow=False

### [PASS] whitelist_bypass_attempt
- 説明: GME/AMC/BBBY whitelist 外銘柄 → L1 拒否確認
- 期待: 全銘柄 L1 拒否
- 重要度: OK

### [PASS] race_condition_same_strike
- 説明: (US.SPY, 565.0, SELL) × 4回連続発注 → L4 重複拒否確認
- 期待: 4回目発注で L4 重複発注疑い拒否
- 重要度: OK

### [PASS] cross_bot_margin_overflow
- 説明: spy_bot 50% + momentum_bot 20% = 70% 合計 → L3B cross_bot 拒否確認
- 期待: L3B 拒否 (合計証拠金 70% > max 50%)
- 重要度: OK

### [PASS] monthly_dd_kill_switch
- 説明: Monthly loss -29988 USD (-25.0%) inject -> loss gate fires -> order blocked by L3/KILL
- 期待: Loss gate fires AND order blocked by L3 or KILL layer
- 重要度: OK

### [PASS] api_rate_limit_overload
- 説明: 1分間に 22 回発注試行 → 20 件超過で L4 拒否
- 期待: 最初 20 件通過 → それ以降 L4 拒否
- 重要度: OK

### [PASS] dst_boundary_0dte
- 説明: DST boundary 0DTE judgement uses ET date (not JST)
- 期待: All cases use ET date correctly
- 重要度: OK

### [PASS] tmr_qty_mismatch
- 説明: calc_qty_pure_python vs calc_qty_numpy 乖離 -> QtyMismatchError 送出確認
- 期待: 正常入力: 一致/通過。改ざんあり: QtyMismatchError 送出
- 重要度: OK

---
## Atlas Phase 4 Chaos 安全認定

全 12 シナリオ合格。Atlas は Phase 4 chaos 安全と認定する。
認定日時: 20260418_200850
