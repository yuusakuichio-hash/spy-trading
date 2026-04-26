#!/usr/bin/env python3
"""
memory_classify.py — memory 棚卸候補リスト作成

inventory_dependency_map の結果 + mtime + ファイル名類似性で memory 212 件を分類。

分類軸:
  P0 継続必須: 2026-04-22 新規律・CURRENT_STATE・高頻度参照 (>=5)・MEMORY.md
  P1 継承: 中頻度参照 (2-4)・重要 project memory
  P2 統合候補: 類似テーマで複数存在
  P3 archive: 古い決定 (mtime 14 日以上前)・過去セッション詳細
  P4 削除候補: 参照 0 件かつ mtime 7 日以上前（真の死コード）

出力:
  data/governance/inventory/memory_classification_20260422.md
"""
import json
import re
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone, timedelta

MEMORY_DIR = Path("/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory")
DEPS_PATH = Path("/Users/yuusakuichio/trading/data/governance/inventory/memory_deps.json")
OUT_PATH = Path("/Users/yuusakuichio/trading/data/governance/inventory/memory_classification_20260422.md")


def load_deps() -> dict:
    return json.loads(DEPS_PATH.read_text())


def find_similar_groups(files: list[str]) -> dict[str, list[str]]:
    """ファイル名の類似性で統合候補グループを抽出"""
    groups = defaultdict(list)

    # patterns: feedback_tone / feedback_tone_language / feedback_language → tone language 等
    # 類似テーマ識別のための簡易キーワード抽出
    keywords = {
        'tone_language': ['tone', 'language', 'word_choice'],
        'time_hour_estimate': ['time_awareness', 'estimate', 'hour', 'minute'],
        'notification': ['notification', 'pushover'],
        'memory_governance': ['memory_over_assumption', 'memory_update_rules', 'auto_memory'],
        'market_hours': ['market_hours', 'market_schedule', 'timezone_rule'],
        'execute_immediate': ['execute_immediately', 'no_confirmation_execute_now',
                              'no_unnecessary_questions', 'recommend_means_execute',
                              'full_speed_default'],
        'false_completion': ['false_completion', 'false_claim'],
        'strategy': ['strategy_first_principles', 'strategy_tagging'],
        'journal': ['journal_role_separation', 'journal_update_flow'],
        'implementation_process': ['implementation_process', 'code_audit_methodology',
                                    'schema_contract_test_mandatory', 'independent_verification_mandatory'],
    }

    for fname in files:
        for group_name, kws in keywords.items():
            if any(kw in fname for kw in kws):
                groups[group_name].append(fname)
                break

    return {k: v for k, v in groups.items() if len(v) >= 2}


def classify(deps: dict) -> dict:
    """各 memory を P0-P4 に分類"""
    now = datetime.now(timezone.utc).timestamp()
    cutoff_new = now - 7 * 86400   # 7 日以内 = 新規
    cutoff_recent = now - 14 * 86400  # 14 日以内 = 現役
    cutoff_old = now - 30 * 86400     # 30 日以上 = 古い

    p0 = []  # 継続必須
    p1 = []  # 継承
    p2 = []  # 統合候補（別途計算）
    p3 = []  # archive 候補
    p4 = []  # 削除候補

    new_files = []

    for fname, info in deps.items():
        ref = info["ref_count"]
        mtime = info["mtime"]

        # 特殊ファイル
        if fname in ("MEMORY.md", "CURRENT_STATE.md"):
            p0.append((fname, ref, mtime, "index/state 最優先参照"))
            continue

        # 2026-04-22 新規作成（規律系は常に P0）
        if mtime >= cutoff_new:
            if 'feedback_' in fname or 'project_session_20260422' in fname:
                p0.append((fname, ref, mtime, "2026-04-22 新規律/セッション記録"))
            else:
                new_files.append((fname, ref, mtime))
            continue

        # 高頻度参照 (>= 5) → P0
        if ref >= 5:
            p0.append((fname, ref, mtime, f"参照 {ref} 件・継続必須"))
            continue

        # 中頻度 (2-4) → P1
        if ref >= 2:
            p1.append((fname, ref, mtime, f"参照 {ref} 件・継承"))
            continue

        # 参照 1 件・mtime 新しめ (30 日以内) → P1
        if ref >= 1 and mtime >= cutoff_old:
            p1.append((fname, ref, mtime, f"参照 {ref} 件・mtime 30 日以内"))
            continue

        # 参照 0 件・古い (7 日以上前) → P4 削除候補
        if ref == 0 and mtime < cutoff_new:
            p4.append((fname, ref, mtime, "参照 0 件・7 日以上前"))
            continue

        # 古い (14 日以上前) → P3 archive
        if mtime < cutoff_recent:
            p3.append((fname, ref, mtime, f"mtime 14 日以上前・参照 {ref}"))
            continue

        # デフォルト: P1
        p1.append((fname, ref, mtime, f"デフォルト P1"))

    # 新規（7 日以内で非規律系）は P1（まだ参照されてないだけ）
    for fname, ref, mtime in new_files:
        p1.append((fname, ref, mtime, f"7 日以内新規・まだ参照少・様子見"))

    return {"p0": p0, "p1": p1, "p2": p2, "p3": p3, "p4": p4}


def format_ts(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone(timedelta(hours=9))).strftime("%Y-%m-%d")


def write_report(classification: dict, groups: dict, total: int):
    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime("%Y-%m-%d %H:%M JST")

    lines = [
        f"# memory 棚卸候補リスト（{ts}）",
        "",
        f"## 概要",
        f"- 総 memory 件数: {total}",
        f"- P0 継続必須: {len(classification['p0'])}",
        f"- P1 継承: {len(classification['p1'])}",
        f"- P2 統合候補グループ: {len(groups)}",
        f"- P3 archive 候補: {len(classification['p3'])}",
        f"- P4 削除候補: {len(classification['p4'])}",
        "",
        "## 分類基準",
        "- **P0 継続必須**: 2026-04-22 規律・CURRENT_STATE・MEMORY.md・参照 5 件以上",
        "- **P1 継承**: 参照 2-4 件 or 30 日以内 + 参照あり",
        "- **P2 統合候補**: 類似テーマで複数ファイル",
        "- **P3 archive**: mtime 14 日以上前 + 低参照",
        "- **P4 削除候補**: 参照 0 件 + mtime 7 日以上前",
        "",
        "## P0 継続必須",
    ]
    for fname, ref, mtime, reason in sorted(classification["p0"], key=lambda x: -x[1]):
        lines.append(f"- `{fname}` (参照 {ref} / mtime {format_ts(mtime)}) — {reason}")

    lines.extend(["", "## P2 統合候補グループ（類似テーマで複数）"])
    for group_name, files in sorted(groups.items()):
        lines.append(f"\n### {group_name}（{len(files)} 件）")
        for f in files:
            lines.append(f"- `{f}`")
        lines.append("→ **推奨**: 1 ファイルに統合 or 代表ファイルに他を include")

    lines.extend(["", "## P3 archive 候補（mtime 14 日以上前 + 低参照）"])
    for fname, ref, mtime, reason in sorted(classification["p3"], key=lambda x: x[2]):
        lines.append(f"- `{fname}` (参照 {ref} / mtime {format_ts(mtime)}) — {reason}")

    lines.extend(["", "## P4 削除候補（参照 0 件 + mtime 7 日以上前）"])
    if classification["p4"]:
        for fname, ref, mtime, reason in sorted(classification["p4"], key=lambda x: x[2]):
            lines.append(f"- `{fname}` (参照 {ref} / mtime {format_ts(mtime)}) — {reason}")
    else:
        lines.append("（該当なし・真の死コードはゼロ）")

    lines.extend([
        "",
        "## P1 継承（参照 2-4 件 or 30 日以内）",
        f"（{len(classification['p1'])} 件・詳細は memory_deps.json 参照）",
        "",
        "## 棚卸実行方針（ゆうさくさん最終承認待ち）",
        "",
        "### 推奨処理",
        "1. P0 → そのまま継続（触らない）",
        "2. P1 → そのまま継続・必要時個別見直し",
        "3. P2 → **各グループで 1 ファイルに統合**（重複コードの整理）",
        "4. P3 → `archive/2026-04/` へ物理移動",
        "5. P4 → 慎重に削除（まずは archive 移動・1 ヶ月後削除）",
        "",
        "### バグなし観点",
        "- 削除前に各ファイルの依存関係を再 grep 確認",
        "- archive 移動後に pytest 全件実行で連鎖確認",
        "- Navigator + Redteam 独立検証で最終承認",
        "- P4 は即削除でなく 1 ヶ月様子見（参照発生したら差し戻し可能）",
        "",
        "## 次のステップ",
        "1. ゆうさくさん最終承認（この分類で OK か・修正あるか）",
        "2. P2 グループ統合 draft 作成",
        "3. P3 archive 実行",
        "4. P4 慎重に archive（削除ではなく）",
    ])

    OUT_PATH.write_text("\n".join(lines))
    print(f"[saved] {OUT_PATH}")


def main():
    deps = load_deps()
    total = len(deps)
    classification = classify(deps)
    groups = find_similar_groups(list(deps.keys()))
    # P2 件数を classification に反映
    classification["p2"] = groups

    write_report(classification, groups, total)

    print(f"\n=== Summary ===")
    print(f"total: {total}")
    print(f"P0 継続必須: {len(classification['p0'])}")
    print(f"P1 継承: {len(classification['p1'])}")
    print(f"P2 統合候補グループ: {len(groups)}（含まれるファイル合計 {sum(len(v) for v in groups.values())}）")
    print(f"P3 archive 候補: {len(classification['p3'])}")
    print(f"P4 削除候補: {len(classification['p4'])}")


if __name__ == "__main__":
    main()
