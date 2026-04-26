# Silent Bug TOP 100 Scan Report (2026-04-22 03:38 JST)

**Source**: agent a13e851e373d237da (completed in 537s, 120-min budget前半消費)
**Judgment**: 本番移行 **NO-GO 確定**

## Summary
- 既発見 48件 + 新規 61件 = **総計 109件**
- 新規 **CRITICAL 19件** (P0 即修)
- 新規 HIGH 24件 (P1 48h 以内)
- 新規 MEDIUM 13件 / LOW 5件

## Judgment 根拠
新規 CRITICAL のうち以下 6件は任意の1件で資本全損 or MFFU DQ に直結:
1. C-19 symbol_selector.py 二重定義 (test/本番パス乖離)
2. C-1 ChronosClient.place_order stub signature mismatch
3. C-2 common/pre_trade_check.py:244 PDT fail-open
4. C-8 common/kill_switch.py:51 通知 silent pass
5. C-11 chronos_bot.py:626 news calendar 欠落時 blackout=False
6. C-17 chronos_bot.py:3687 evaluate close 分岐 no place_order

Boeing 737MAX (単一 AoA センサ依存) と同様、single silent bug で致命事故の設計が 6箇所存在。

## 全 CRITICAL 19件
(full list - agent output preserved below)
