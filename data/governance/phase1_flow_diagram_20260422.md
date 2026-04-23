# Phase 1 完了判定フロー図

**作成**: 2026-04-22
**対象文書**: `data/governance/phase1_completion_criteria_20260422.md`

## 1. 判定プロセス フロー（時系列）

```mermaid
flowchart TD
    Start([Phase 1 終盤<br/>完了判定開始]) --> SelfEval[ソラ自己評価<br/>全条件 A/B/C チェック]
    SelfEval --> NavCheck{A-1<br/>Navigator agent<br/>稼働?}
    NavCheck -->|NO| Differ[DIFFER<br/>差し戻し]
    NavCheck -->|YES| AndonCheck{A-3<br/>Andon→bot 経路<br/>完動?}
    AndonCheck -->|NO| Differ
    AndonCheck -->|YES| LieCheck{B-1<br/>虚偽完了候補<br/>全解消?}
    LieCheck -->|NO| Differ
    LieCheck -->|YES| ZeroLieCheck{B-3<br/>Phase 1 中<br/>虚偽完了ゼロ?}
    ZeroLieCheck -->|NO| Differ
    ZeroLieCheck -->|YES| DryRun{C-2<br/>仕様書 1 本<br/>dry-run 成功?}
    DryRun -->|NO| Differ
    DryRun -->|YES| NoGoCheck{D. NO-GO 条件<br/>7 項目<br/>該当なし?}
    NoGoCheck -->|該当あり| Differ
    NoGoCheck -->|該当なし| Auditor[Navigator + Redteam<br/>+ Auditor<br/>独立検証]
    Auditor -->|DIFFER| Differ
    Auditor -->|PASS| User[ゆうさくさん<br/>最終承認]
    User -->|差し戻し| Differ
    User -->|承認| Record[CURRENT_STATE.md<br/>Phase 2 着手記録]
    Record --> GO([Phase 2 着手])
    Differ --> Fix[修正作業]
    Fix --> SelfEval

    style NavCheck fill:#ffcccc
    style AndonCheck fill:#ffcccc
    style LieCheck fill:#ffcccc
    style ZeroLieCheck fill:#ffcccc
    style DryRun fill:#ffcccc
    style GO fill:#ccffcc
    style Differ fill:#ffdddd
```

**赤枠の 5 項目 = 必須 GREEN**（どれか NO で即差し戻し）

---

## 2. 条件カテゴリ構造図

```mermaid
flowchart LR
    subgraph A[A. 機能的完了条件]
        A1[A-1 Navigator 稼働]
        A2[A-2 hook 実稼働]
        A3[A-3 Andon→bot 経路]
        A4[A-4 LLM 有効化判定]
        A5[A-5 llm_budget 稼働]
        A6[A-6 memory 整合性]
    end
    subgraph B[B. 規律的完了条件]
        B1[B-1 虚偽完了全解消]
        B2[B-2 未実装宣言整合]
        B3[B-3 Phase 1 中ゼロ]
    end
    subgraph C[C. 運用的完了条件]
        C1[C-1 認知負荷]
        C2[C-2 dry-run 成功]
        C3[C-3 Free Tier 内]
    end
    subgraph D[D. NO-GO 条件<br/>7 項目]
        D1[虚偽残存]
        D2[Andon 不確実]
        D3[Navigator 未稼働]
        D4[hook 未確認]
        D5[目標道筋不明]
        D6[認知限界]
        D7[Phase 1 虚偽 >=1]
    end
    subgraph MUST[必須 GREEN<br/>5 項目・妥協不可]
        M1[A-1]
        M3[A-3]
        MB1[B-1]
        MB3[B-3]
        MC2[C-2]
    end
    subgraph OPT[Partial GO 許容<br/>時間差対応可]
        O1[A-4]
        O2[A-2 主要 4 hook のみ]
        O3[C-1 自己判断]
    end

    A1 -.-> M1
    A3 -.-> M3
    B1 -.-> MB1
    B3 -.-> MB3
    C2 -.-> MC2
```

---

## 3. ASCII 簡略版（フロー図非対応環境用）

```
[Phase 1 終盤]
    ↓
[ソラ自己評価: A/B/C 全条件チェック]
    ↓
[必須 GREEN 5 項目]
  ├─ A-1 Navigator 稼働? ────── NO → [DIFFER 差し戻し]
  ├─ A-3 Andon→bot 経路完動? ── NO → [DIFFER 差し戻し]
  ├─ B-1 虚偽完了候補全解消? ── NO → [DIFFER 差し戻し]
  ├─ B-3 Phase 1 中虚偽ゼロ? ── NO → [DIFFER 差し戻し]
  └─ C-2 仕様書 1 本 dry-run? ─ NO → [DIFFER 差し戻し]
    ↓ 全 YES
[D. NO-GO 条件 7 項目: どれか該当?] ── YES → [DIFFER 差し戻し]
    ↓ NO（該当なし）
[Navigator + Redteam + Auditor 独立検証] ── DIFFER → [差し戻し]
    ↓ PASS
[ゆうさくさん 最終承認] ── 差し戻し → [修正]
    ↓ 承認
[CURRENT_STATE.md に Phase 2 着手記録]
    ↓
[Phase 2 着手 GO]
```

---

## 4. Partial GO の範囲

```
┌─────────────────────────────────────────┐
│       必須 GREEN（5 項目・妥協不可）      │
│  A-1 / A-3 / B-1 / B-3 / C-2              │
├─────────────────────────────────────────┤
│       Partial GO 許容（時間差対応可）     │
│  A-4 外部 LLM 有効化判定                  │
│  A-2 hook 実稼働（主要 4 つで OK）        │
│  C-1 認知負荷（本人自己判断）             │
├─────────────────────────────────────────┤
│       通常 GREEN（目指すが強制でない）    │
│  A-5 / A-6 / B-2 / C-3                    │
└─────────────────────────────────────────┘
```

---

## 関連ファイル
- `data/governance/phase1_completion_criteria_20260422.md`（本体・文章版）
- `memory/CURRENT_STATE.md`（Phase 0 完了状況記録）
- `memory/project_session_20260422_major_redesign.md`
