# Postmortem: AtlasTrader delegate 設計却下 (Redteam)

**Incident ID**: PM-20260425-001
**Date**: 2026-04-25 06:15 JST
**Severity**: Medium (設計却下・実装取り残しゼロ・archive 済)
**Author**: sora (Claude)
**Status**: Resolved (subprocess 版採択に移行)

## Summary

atlas_v3/bots/ 新規実装で AtlasTrader = SPYCreditSpreadBot の composition wrapper (delegate pattern) 設計を採用。builder agent a146537 が実装 + 33 tests PASS まで完成。しかし redteam agent abf2420 の事前攻撃で 8 攻撃面・CRITICAL 3 件 (thread safety 崩壊 / env 二重上書き / PID lock 衝突) が指摘され却下。subprocess 境界隔離方式 (agent a531d8) に設計変更。

## Timeline (JST)

| 時刻 | 出来事 |
|---|---|
| 06:10 | ゆうさくさん指摘「Atlas bot だけと思った」→ 統合判断 |
| 06:11 | strategist a3507eb / Explore ae82e40 / builder a146537 / redteam abf2420 の 4 agent 並列投入 |
| 06:13 | a146537 builder が delegate 版 AtlasTrader 実装開始 |
| 06:14 | a3507eb strategist 完了 (delegate 前提の骨格設計・Navigator レビュー gate 推奨) |
| 06:15 | abf2420 redteam 完了 — delegate 採用すべきでない結論 |
| 06:15 | sora: delegate 版と subprocess 版の矛盾認識・subprocess 版 agent a531d8 追加投入 |
| 06:16 | a146537 builder 完了 (delegate 版 33 tests PASS) — しかし redteam 指摘で採用却下 |
| 06:17 | sora: delegate 版 4 ファイル + test 1 ファイルを atlas_v3/bots/_archived_delegate_version_20260425/ へ退避 |

## Contributing Factors (Blameless)

1. **並列投入時の責任領域不明確**: builder と redteam を同時投入したため builder 側が「設計の可否判定」が redteam から返る前に実装完了
2. **設計却下パス未定義**: 完了 agent の成果物が「却下」扱いになった時の archive / rollback 手順が事前に無かった
3. **delegate pattern の既知欠陥 (module-level 副作用等) を事前 premortem で列挙していなかった** — premortem は「統合」全体の risk を 7 scenarios で書いたが、delegate vs subprocess の設計選択肢レベルでは分析されなかった
4. **spy_bot.py が schg lock 下で書換不可** — wrapper から内部修正できない物理制約が delegate 破綻の根本要因

## What went well

- Redteam を並列投入していたため、builder 完了直後に却下判定が出て**実装を本番に injecting する前に止められた**
- Agent 並列投入で設計 + 攻撃 + 実装 + 探索の 4 視点が 5 分以内に揃った
- delegate 版コードを捨てず archive 保存したため将来 Engine 移植時に参照可能
- Redteam が対案 (subprocess 境界) を具体的に提示したため方針転換が即実行できた

## What went wrong

- builder 実装工数 (~170 秒 + 33 tests ) が無駄化 (ただし archive として学習資料化)
- ゆうさくさんに「delegate 版採用」の報告を一瞬してから即訂正する形になった (認識混乱)

## Action Items

1. **[完了] delegate 版 archive**: atlas_v3/bots/_archived_delegate_version_20260425/ に保存
2. **[完了] bug_ledger BUG-20260425-009 登録**
3. **[進行中] subprocess 版実装 (a531d8)**
4. **[保留] 設計選択肢が複数ある場合 Agent 投入順を Redteam → Builder に変更する規律化** — memory `feedback_parallel_agent_order.md` に明文化候補
5. **[保留] premortem 時に「採用設計案の対抗案」列挙を必須化** — hook `premortem_content_scorer.py` の採点軸に追加候補

## Lessons

- **Design choice で複数案ある場合は Redteam 先行**: builder を走らせる前に redteam で各案を破壊試験してから survivor を builder に渡す
- **並列投入は「互いの結果に依存しない task」に限定**: 設計 vs 攻撃 のような論理的依存は直列で
- **既存 stateful module の wrapper は subprocess 境界でのみ安全**: delegate は 5 大欠陥 (state race / logger / env / PID / method 依存) で破綻

## Related

- BUG-20260425-009 (bug_ledger)
- BUG-20260425-008 (Atlas 発注機能未移植・ADR-014 Decision 3)
- Redteam output: /private/tmp/claude-501/.../abf2420d254909a88.output (archived)
