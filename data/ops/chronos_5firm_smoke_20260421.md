# Chronos 5 Firm Smoke Test — 20260421

実行時刻: 2026-04-21T14:15:52.620635+00:00
結果: 11/11 PASS

| # | label | strategy_id | expect | actual | verdict | tp_logId | reason |
|---|-------|-------------|--------|--------|---------|----------|--------|
| 1 | demo_allow | chronos_orb_mes_demo | allow | allow | **PASS** | 3a34c707-6722-4f... | all_constraints_passed |
| 2 | demo_max_contracts_block | chronos_orb_mes_demo | block | block | **PASS** |  | [demo] blocked: max_contracts: qty=11 > max=10 |
| 3 | rapid_allow | chronos_orb_mes_rapid_sim | allow | allow | **PASS** | 3ca46194-6cb2-43... | all_constraints_passed |
| 4 | rapid_dll_block | chronos_orb_mes_rapid_sim | block | block | **PASS** |  | [mffu_rapid] blocked: daily_loss_limit: pnl=-400.00 <= -400 |
| 5 | pro_allow | chronos_orb_mes_pro_sim | allow | allow | **PASS** | 3996d474-db30-41... | all_constraints_passed |
| 6 | pro_max_contracts_block | chronos_orb_mes_pro_sim | block | block | **PASS** |  | [mffu_pro] blocked: max_contracts: qty=6 > max=5 |
| 7 | builder_allow | chronos_orb_mes_builder_sim | allow | allow | **PASS** | 744e011d-09fc-47... | all_constraints_passed |
| 8 | builder_dll_block | chronos_orb_mes_builder_sim | block | block | **PASS** |  | [mffu_builder] blocked: daily_loss_limit: pnl=-1000.00 <= -1 |
| 9 | builder_force_close_block | chronos_orb_mes_builder_sim | block | block | **PASS** |  | [mffu_builder] blocked: force_close_et: new position not all |
| 10 | tradeify_allow | chronos_orb_mes_tradeify_sim | allow | allow | **PASS** | c00df143-3a34-47... | all_constraints_passed |
| 11 | tradeify_dll_block | chronos_orb_mes_tradeify_sim | block | block | **PASS** |  | [tradeify] blocked: daily_loss_limit: pnl=-1250.00 <= -1250 |

## 詳細

### demo_allow
- description: demo: 5枚・損失なし → allow
- expect_allowed: True
- actual_allowed: True
- verdict: **PASS**
- firm_reason: `all_constraints_passed`
- tp_sent: True
- tp_log_id: 3a34c707-6722-4f44-9c20-8da0e6dec33a

### demo_max_contracts_block
- description: demo: 11枚 (max=10超) → block
- expect_allowed: False
- actual_allowed: False
- verdict: **PASS**
- firm_reason: `[demo] blocked: max_contracts: qty=11 > max=10`
- tp_sent: False
- tp_log_id: 

### rapid_allow
- description: rapid: 2枚・pnl -$100 (DLL $400未満) → allow
- expect_allowed: True
- actual_allowed: True
- verdict: **PASS**
- firm_reason: `all_constraints_passed`
- tp_sent: True
- tp_log_id: 3ca46194-6cb2-4363-9880-2d27cdca064a

### rapid_dll_block
- description: rapid: pnl -$400 (DLL $400 触達) → block
- expect_allowed: False
- actual_allowed: False
- verdict: **PASS**
- firm_reason: `[mffu_rapid] blocked: daily_loss_limit: pnl=-400.00 <= -400`
- tp_sent: False
- tp_log_id: 

### pro_allow
- description: pro: 4枚 (max=5以内)・pnl -$500 → allow
- expect_allowed: True
- actual_allowed: True
- verdict: **PASS**
- firm_reason: `all_constraints_passed`
- tp_sent: True
- tp_log_id: 3996d474-db30-415a-be06-e1cdf15e18e0

### pro_max_contracts_block
- description: pro: 6枚 (max=5超) → block
- expect_allowed: False
- actual_allowed: False
- verdict: **PASS**
- firm_reason: `[mffu_pro] blocked: max_contracts: qty=6 > max=5`
- tp_sent: False
- tp_log_id: 

### builder_allow
- description: builder: 2枚・DLL $1000未満・15:55前 → allow
- expect_allowed: True
- actual_allowed: True
- verdict: **PASS**
- firm_reason: `all_constraints_passed`
- tp_sent: True
- tp_log_id: 744e011d-09fc-471f-8141-85f6dffe1fb6

### builder_dll_block
- description: builder: pnl -$1000 (DLL $1000 触達) → block
- expect_allowed: False
- actual_allowed: False
- verdict: **PASS**
- firm_reason: `[mffu_builder] blocked: daily_loss_limit: pnl=-1000.00 <= -1000`
- tp_sent: False
- tp_log_id: 

### builder_force_close_block
- description: builder: 16:00 ET (force_close_et=15:55超) → block
- expect_allowed: False
- actual_allowed: False
- verdict: **PASS**
- firm_reason: `[mffu_builder] blocked: force_close_et: new position not allowed after 15:55 ET`
- tp_sent: False
- tp_log_id: 

### tradeify_allow
- description: tradeify: 3枚・pnl -$300 (DLL $1250未満) → allow
- expect_allowed: True
- actual_allowed: True
- verdict: **PASS**
- firm_reason: `all_constraints_passed`
- tp_sent: True
- tp_log_id: c00df143-3a34-472d-b8c3-c2fdbba8b924

### tradeify_dll_block
- description: tradeify: pnl -$1250 (DLL $1250 触達) → block
- expect_allowed: False
- actual_allowed: False
- verdict: **PASS**
- firm_reason: `[tradeify] blocked: daily_loss_limit: pnl=-1250.00 <= -1250`
- tp_sent: False
- tp_log_id: 

## firm 別集計

| firm | total | allow | block | fail |
|------|-------|-------|-------|------|
| demo | 2 | 1 | 1 | 0 |
| rapid | 2 | 1 | 1 | 0 |
| pro | 2 | 1 | 1 | 0 |
| builder | 3 | 1 | 2 | 0 |
| tradeify | 2 | 1 | 1 | 0 |
