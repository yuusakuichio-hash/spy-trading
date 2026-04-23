# Definition of Done (DoD) — Sora Lab

**制定日**: 2026-04-24（ゆうさくさん指示・案 A 先送り否定を受けて）
**根拠**: Agile Manifesto (2001) / Scrum DoD / 最上位規律 #1 バグなし絶対最優先
**物理強制**: `.claude/hooks/sprint_close_gate.sh` で違反時 BLOCK
**bypass**: 原則禁止・緊急時のみ `DOD_BYPASS=1` + 理由ログ必須

---

## 個別タスク DoD（Builder 成果物単位）

タスクが done を主張するには以下の全条件が充足していること:

1. **pytest tests/ 全件走行で 0 failed**（選択テストでの充足不可・feedback_no_selective_testing.md）
2. **新規テスト >= 変更関数数**（TDD 的カバレッジ）
3. **Navigator 独立監査で ACCEPT**（CONDITIONAL-ACCEPT は不可）
4. **Redteam r2 時点で CRITICAL 実害高 == 0**（r1 で発見された CRITICAL は r2 前に全件修正）
5. **HIGH == 0**（2026-04-24 ゆうさくさん指示で 5 → 0 に厳格化・v3 作り直し期間中は例外なし）
6. **回帰 0**（既存コード動作変化なし）
7. **AST / grep / pytest stdout / mutation の証跡 4 点**（feedback_false_completion_5th_governance.md）
8. **既存コード改変なし**（legacy_write_block 遵守）

## Sprint 閉鎖 DoD（Sprint 全体）

次 Sprint 着手条件:

1. **carryover 新規起票 == 0**（Sprint 内で潰しきる・跨ぎ禁止）
2. **全タスク個別 DoD 充足**
3. **ADR 系成果物起票完了**
4. **ゆうさくさん最終判断を要する箇所は確認済み**

### carryover 例外（厳格運用）

以下のみ Sprint 跨ぎ可能（それ以外は Sprint 内決着）:

- **技術的不可能**（外部 API 仕様待ち・依存ライブラリ未対応等）
- **ゆうさくさん判断保留**（戦略方針に関わる決定待ち）
- **コスト > 効果が定量証明**（数値比較で示された低優先）

例外適用時は `.claude/hooks/sprint_meta.json` の `carryover_allowed_exceptions` に理由を記録。

---

## 過去の carryover 膨張（反省材料）

Sprint 1-A/B 期間中に C-001〜C-016 まで膨張。これは DoD 緩和による失敗。v3 期間中は再発禁止。

## 運用原則

- Builder 完了宣言は単独不成立（Navigator + Redteam r2 両者 ACCEPT 必須）
- Redteam Round limit 2 下で CRITICAL 残存ならタスク自体を止めて全件修正まで進めない
- 「先送り」「あとで修正」「carryover でいい」は CLAUDE.md 禁句 TOP5 #1 の新カテゴリ（今後 discipline_guard で検出）

---

## 参照

- `CLAUDE.md` 最上位規律 #1 バグなし絶対最優先
- `memory/feedback_bug_zero_absolute_20260422.md`
- `memory/feedback_no_selective_testing.md`
- `memory/feedback_false_completion_5th_governance.md`
- `.claude/hooks/sprint_close_gate.sh`
- `.claude/hooks/sprint_meta.json`
