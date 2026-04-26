# 棚卸 依存関係マップ サマリ (2026-04-23 03:12 JST)

## 件数
- memory: 214 ファイル
- hooks: 41 ファイル
- agents: 10 ファイル

## 死コード候補（参照 0 件・削除候補）
- memory: 0 ファイル
- hooks: 0 ファイル（settings 未登録 + 参照 0 件）
- agents: 0 ファイル

## 高頻度参照（継承確実候補・5 件以上参照）
### memory top
- 31件: CURRENT_STATE.md
- 28件: MEMORY.md
- 22件: feedback_bug_zero_absolute_20260422.md
- 21件: feedback_independent_verification_mandatory.md
- 19件: feedback_false_completion_5th_governance.md
- 18件: feedback_navigator_mandatory_20260422.md
- 17件: project_pushover_tag_convention.md
- 17件: feedback_no_fixed_params.md
- 14件: feedback_no_numeric_citation.md
- 14件: feedback_implementation_process.md
- 13件: feedback_schema_contract_test_mandatory.md
- 13件: feedback_no_schedule_delay.md
- 13件: project_session_20260422_major_redesign.md
- 12件: feedback_goal_acceleration_first.md
- 11件: feedback_no_selective_testing.md
- 11件: project_llc_establishment_20260419.md
- 11件: feedback_time_awareness_20260422.md
- 11件: project_agent_organization_20260422.md
- 10件: project_retirement_schedule.md
- 10件: feedback_cognitive_limit_design.md
- 10件: feedback_300m_post_tax_and_reinvest.md
- 9件: feedback_language.md
- 9件: feedback_no_private_life_intrusion_20260422.md
- 9件: feedback_aar_improvement_cycle.md
- 9件: feedback_no_confirmation_execute_now.md
- 9件: feedback_builder_time_estimate_minutes.md
- 9件: project_prop_farm_deferred.md
- 8件: project_session_20260421_night_complete.md
- 8件: feedback_notification_policy.md
- 8件: project_atlas_tax_correction_20260420.md
### hooks top
- 24件: discipline_guard.sh
- 18件: andon_multichannel.py
- 17件: legacy_write_block.sh
- 17件: peer_review.sh
- 17件: blue_team_bias_detector.sh
- 17件: claim_ledger_guard.py
- 16件: estimate_historical_calibration.py
- 13件: auth_budget_guard.py
- 11件: chronos_edit_spec_guard.sh
- 11件: sns_truth_guard.sh
- 10件: memory_completion_tracker.sh
- 10件: state_safety_guard.py
- 9件: auditor_required_gate.sh
- 9件: external_self_check.sh
- 9件: inject_recent_corrections.sh
- 9件: spec_premortem_required.sh
- 9件: premortem_gate.sh
- 9件: confidence_assertion_guard.sh
- 9件: selective_test_detector.sh
- 8件: prepend_pending_violations.sh
- 8件: false_claim_detector.sh
- 8件: url_verify_guard.sh
- 8件: service_recommend_guard.sh
- 8件: time_estimate_sanity.sh
- 8件: navigator_antipattern_detector.py
- 7件: proposal_bottleneck_stop_guard.sh
- 7件: stop_summary.sh
- 7件: session_start_market_specs_reload.sh
- 7件: session_start_discipline_reload.sh
- 7件: stop_pending_check.sh
### agents top
- 672件: builder.md
- 249件: strategist.md
- 216件: redteam.md
- 177件: ops.md
- 116件: sns.md
- 92件: analyst.md
- 76件: journal.md
- 67件: secretary.md
- 62件: governance.md
- 28件: navigator.md

## 注意事項（バグなし観点）

- 死コード候補でも「未参照」=「不要」とは限らない（規律 memory は読まれるだけで参照されない）
- 削除前に dry-run（archive 一時退避）必須
- 削除後に pytest 全件実行で hook 連鎖確認
- Navigator + Redteam 独立検証で最終承認

## 出力ファイル
- memory_deps.json: memory 全件の参照状況
- hook_deps.json: hook 全件の参照状況 + settings 登録状況
- agent_deps.json: agent 全件の参照状況
- dead_candidates.json: 死コード候補リスト