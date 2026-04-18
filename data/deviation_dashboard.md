# Atlas Deviation Dashboard

**生成日時**: 2026-04-18 10:32 JST  
**対象期間**: 直近7日  
**🚨 急増検知（1h ≥10件・即日対応）**: 0件  
**🟠 当日累積（24h ≥30件・AAR強調）**: 4件  
**🔴 常態化検知（2日連続・週次レビュー）**: 3件  

## 理論的背景

Challenger O-ring事故（1986）の教訓: 同じ異常が繰り返されても「今までも大丈夫だったから」と正常扱いされる現象（Normalization of Deviance・Diane Vaughan 1996）。

**Sora Labの改善ペースに合わせ3段階で検知**（急増/当日/常態化）。

## 🚨 急増検知（1時間で10件超・即日対応必須）

_急増検知なし_

## 🟠 当日累積（24hで30件超・翌AARで強調）

| カテゴリ | 直近24h件数 | 累計 |
|---|---:|---:|
| `exit_verify_exception` | 1430 | 1430 |
| `chain_fetch_fail` | 34 | 274 |
| `gamma_early_exit` | 110 | 120 |
| `no_positions` | 50 | 50 |

## 🔴 常態化検知（2日連続・要中期対処）

| カテゴリ | 件数 | 期間 | rate/h |
|---|---:|---|---:|
| `chain_fetch_fail` | 274 | 2日 | 4.3 |
| `gamma_early_exit` | 120 | 2日 | 2.5 |
| `quote_context_disconnect` | 97 | 3日 | 1.3 |

## 全逸脱カテゴリ（頻度降順）

| カテゴリ | 件数 | 初回 | 最終 | rate/h | 常態化 |
|---|---:|---|---|---:|---|
| `exit_verify_exception` | 1430 | 04/18 04:37 | 04/18 04:49 | 1430.0 | 🟢 |
| `chain_fetch_fail` | 274 | 04/15 12:27 | 04/18 03:39 | 4.3 | 🔴 |
| `gamma_early_exit` | 120 | 04/16 04:14 | 04/18 04:30 | 2.5 | 🔴 |
| `quote_context_disconnect` | 97 | 04/14 22:45 | 04/18 04:16 | 1.3 | 🔴 |
| `no_positions` | 50 | 04/18 04:00 | 04/18 04:28 | 50.0 | 🟢 |
| `insufficient_margin` | 36 | 04/16 03:58 | 04/17 03:53 | 1.5 | 🟢 |
| `orb_spy_override` | 19 | 04/18 04:37 | 04/18 06:07 | 12.6 | 🟢 |
| `spread_too_wide` | 16 | 04/17 23:09 | 04/18 04:30 | 3.0 | 🟢 |
| `strike_mismatch` | 15 | 04/18 07:33 | 04/18 07:33 | 15.0 | 🟢 |
| `close_incomplete` | 1 | 04/15 04:50 | 04/15 04:50 | 1.0 | 🟢 |
| `entry_aborted` | 1 | 04/16 23:20 | 04/16 23:20 | 1.0 | 🟢 |

## サンプルログ

### `exit_verify_exception`
```
2026-04-18 04:37:36,806 [WARNING] [MassVerify] US.SPY_orb_buy エグジット確認例外: unsupported operand type(s) for -: 'str' and 'float'
```

### `chain_fetch_fail`
```
2026-04-15 12:27:53,241 [ERROR] [ORB Entry] orb_tp50_sl30_p5_d50: オプションチェーン取得失敗 (CALL)
```

### `gamma_early_exit`
```
2026-04-16 04:14:13,522 [INFO] Closing 3 positions (gamma_early_exit)
```

### `quote_context_disconnect`
```
2026-04-14 22:45:32,524 [WARNING] Quote context切断 (1回連続) フォールバックで継続中
```

### `no_positions`
```
2026-04-18 04:00:44,644 [ERROR] Close order FAILED for US.SPXW260417C5400000 x1: Not enough positions
```
