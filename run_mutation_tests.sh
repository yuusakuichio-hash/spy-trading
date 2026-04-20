#!/bin/bash
# MF-9 fix: mutation testing 用テストランナー
# mutmut 3.x は tests_dir を stats collection に使い、runner をミュータント検証に使う。
# runner はコアロジックをカバーするテストのみを実行する（flaky テストは除外）。
cd /Users/yuusakuichio/trading
python3 -m pytest \
  tests/test_chronos_schema_contract.py \
  tests/test_prop_firm_rules.py \
  tests/test_prop_firm_redteam.py \
  tests/test_audit_high_phase2.py \
  tests/test_critical_7_8_10.py \
  tests/test_f12_f13_critical_fixes_20260419.py \
  tests/test_redteam_critical.py \
  tests/test_properties.py \
  -q --tb=no 2>/dev/null
