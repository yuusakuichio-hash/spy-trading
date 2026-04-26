#!/usr/bin/env python3
"""
inventory_dependency_map.py — 棚卸前の依存関係マップ作成

memory / hook / agent / CLAUDE.md / 既存コード の相互参照を全件 grep で把握し、
削除可否判断の基礎データを作る。

バグなし規律: 削除前に依存関係を完全把握しないとリンク切れ・silent failure を生む。

出力:
  data/governance/inventory/memory_deps.json
  data/governance/inventory/hook_deps.json
  data/governance/inventory/agent_deps.json
  data/governance/inventory/dead_candidates.json
  data/governance/inventory/inventory_summary.md
"""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path("/Users/yuusakuichio/trading")
MEMORY_DIR = Path("/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory")
OUT_DIR = ROOT / "data" / "governance" / "inventory"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 検索対象の root ディレクトリ
SEARCH_ROOTS = [
    ROOT,
    Path("/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading"),
]

# 検索除外ディレクトリ
EXCLUDE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "data/logs", "data/llm_budget", "data/auth_budget",
    "data/sessions", "data/eval/daily",
    "archive",
}


def list_memory_files() -> list[Path]:
    files = []
    for p in MEMORY_DIR.iterdir():
        if p.is_file() and p.suffix == ".md":
            files.append(p)
    return files


def list_hook_files() -> list[Path]:
    return list((ROOT / ".claude" / "hooks").glob("*.sh")) + list(
        (ROOT / ".claude" / "hooks").glob("*.py")
    )


def list_agent_files() -> list[Path]:
    return list((ROOT / ".claude" / "agents").glob("*.md"))


def grep_references(needle: str) -> list[dict]:
    """needle 文字列を全 search root から grep し、ヒット箇所を返す"""
    results = []
    cmd = [
        "grep", "-r", "-l", "--include=*.md", "--include=*.py", "--include=*.sh",
        "--include=*.yaml", "--include=*.yml", "--include=*.json", "--include=*.toml",
    ]
    for ex in EXCLUDE_DIRS:
        cmd.extend(["--exclude-dir", ex.split("/")[-1]])
    cmd.append(needle)
    for root in SEARCH_ROOTS:
        cmd_root = cmd + [str(root)]
        try:
            r = subprocess.run(cmd_root, capture_output=True, text=True, timeout=30)
            for line in r.stdout.splitlines():
                line = line.strip()
                if line:
                    results.append(line)
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
    return list(set(results))


def analyze_memory() -> dict:
    """memory 全件の参照状況を分析"""
    memory_files = list_memory_files()
    print(f"[memory] analyzing {len(memory_files)} files...", file=sys.stderr)

    result = {}
    for i, mf in enumerate(memory_files, 1):
        if i % 20 == 0:
            print(f"  [memory] {i}/{len(memory_files)}", file=sys.stderr)
        name = mf.stem
        # ファイル名で参照されてるか（拡張子なし）
        refs = grep_references(name + ".md")
        # 自分自身を除外
        refs_external = [r for r in refs if not r.endswith(mf.name)]
        result[mf.name] = {
            "stem": name,
            "size_bytes": mf.stat().st_size,
            "mtime": mf.stat().st_mtime,
            "ref_count": len(refs_external),
            "refs": refs_external[:10],  # 上位 10 件のみ保存
        }
    return result


def analyze_hooks() -> dict:
    hook_files = list_hook_files()
    print(f"[hooks] analyzing {len(hook_files)} files...", file=sys.stderr)

    # settings.local.json 内に登録されているか確認
    settings_path = ROOT / ".claude" / "settings.local.json"
    settings_text = settings_path.read_text() if settings_path.exists() else ""

    result = {}
    for hf in hook_files:
        name = hf.name
        registered = name in settings_text
        # ファイル名で他から参照されてるか（実行コマンドや doc 言及）
        refs = grep_references(name)
        refs_external = [r for r in refs if not r.endswith(name)]
        result[name] = {
            "size_bytes": hf.stat().st_size,
            "mtime": hf.stat().st_mtime,
            "registered_in_settings": registered,
            "ref_count": len(refs_external),
            "refs": refs_external[:10],
        }
    return result


def analyze_agents() -> dict:
    agent_files = list_agent_files()
    print(f"[agents] analyzing {len(agent_files)} files...", file=sys.stderr)
    result = {}
    for af in agent_files:
        name = af.name
        stem = af.stem
        refs = grep_references(stem)
        refs_external = [r for r in refs if not r.endswith(name)]
        result[name] = {
            "stem": stem,
            "size_bytes": af.stat().st_size,
            "mtime": af.stat().st_mtime,
            "ref_count": len(refs_external),
            "refs": refs_external[:10],
        }
    return result


def find_dead_candidates(memory: dict, hooks: dict, agents: dict) -> dict:
    """参照 0 件 かつ 7 日以上前作成 = 真の死コード候補
    （新規ファイルは「まだ参照されてないだけ」で死コードではない）
    """
    import time
    cutoff = time.time() - 7 * 86400  # 7 日前
    dead_memory = [
        name for name, info in memory.items()
        if info["ref_count"] == 0 and info["mtime"] < cutoff
    ]
    dead_hooks = [
        name for name, info in hooks.items()
        if info["ref_count"] == 0 and not info["registered_in_settings"]
        and info["mtime"] < cutoff
    ]
    dead_agents = [
        name for name, info in agents.items()
        if info["ref_count"] == 0 and info["mtime"] < cutoff
    ]
    # 新規候補（除外されたが要確認）
    new_unrefs_memory = [
        name for name, info in memory.items()
        if info["ref_count"] == 0 and info["mtime"] >= cutoff
    ]
    return {
        "memory": dead_memory,
        "hooks": dead_hooks,
        "agents": dead_agents,
        "new_files_unrefs_memory": new_unrefs_memory,
        "note": "死コード判定: 参照0件 かつ mtime 7日以上前。新規(7日以内)は別枠。",
    }


def find_high_ref(memory: dict, hooks: dict, agents: dict, threshold: int = 5) -> dict:
    high_memory = sorted(
        [(n, i["ref_count"]) for n, i in memory.items() if i["ref_count"] >= threshold],
        key=lambda x: -x[1],
    )
    high_hooks = sorted(
        [(n, i["ref_count"]) for n, i in hooks.items() if i["ref_count"] >= threshold],
        key=lambda x: -x[1],
    )
    high_agents = sorted(
        [(n, i["ref_count"]) for n, i in agents.items() if i["ref_count"] >= threshold],
        key=lambda x: -x[1],
    )
    return {
        "memory": high_memory[:30],
        "hooks": high_hooks[:30],
        "agents": high_agents[:30],
    }


def write_summary(memory: dict, hooks: dict, agents: dict, dead: dict, high: dict) -> None:
    jst = timezone(timedelta(hours=9))
    ts = datetime.now(jst).strftime("%Y-%m-%d %H:%M JST")
    lines = [
        f"# 棚卸 依存関係マップ サマリ ({ts})",
        "",
        f"## 件数",
        f"- memory: {len(memory)} ファイル",
        f"- hooks: {len(hooks)} ファイル",
        f"- agents: {len(agents)} ファイル",
        "",
        f"## 死コード候補（参照 0 件・削除候補）",
        f"- memory: {len(dead['memory'])} ファイル",
    ]
    for f in dead['memory']:
        lines.append(f"  - {f}")
    lines.append(f"- hooks: {len(dead['hooks'])} ファイル（settings 未登録 + 参照 0 件）")
    for f in dead['hooks']:
        lines.append(f"  - {f}")
    lines.append(f"- agents: {len(dead['agents'])} ファイル")
    for f in dead['agents']:
        lines.append(f"  - {f}")

    lines.extend([
        "",
        f"## 高頻度参照（継承確実候補・5 件以上参照）",
        f"### memory top",
    ])
    for name, count in high['memory']:
        lines.append(f"- {count}件: {name}")
    lines.append("### hooks top")
    for name, count in high['hooks']:
        lines.append(f"- {count}件: {name}")
    lines.append("### agents top")
    for name, count in high['agents']:
        lines.append(f"- {count}件: {name}")

    lines.extend([
        "",
        "## 注意事項（バグなし観点）",
        "",
        "- 死コード候補でも「未参照」=「不要」とは限らない（規律 memory は読まれるだけで参照されない）",
        "- 削除前に dry-run（archive 一時退避）必須",
        "- 削除後に pytest 全件実行で hook 連鎖確認",
        "- Navigator + Redteam 独立検証で最終承認",
        "",
        "## 出力ファイル",
        "- memory_deps.json: memory 全件の参照状況",
        "- hook_deps.json: hook 全件の参照状況 + settings 登録状況",
        "- agent_deps.json: agent 全件の参照状況",
        "- dead_candidates.json: 死コード候補リスト",
    ])
    (OUT_DIR / "inventory_summary.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="memory のみ・高速版")
    args = parser.parse_args()

    memory = analyze_memory()
    (OUT_DIR / "memory_deps.json").write_text(json.dumps(memory, ensure_ascii=False, indent=2))
    print(f"[saved] {OUT_DIR}/memory_deps.json", file=sys.stderr)

    if args.quick:
        print("\n=== quick: memory のみ ===")
        dead_memory = [n for n, i in memory.items() if i["ref_count"] == 0]
        print(f"memory dead candidates: {len(dead_memory)}")
        return

    hooks = analyze_hooks()
    (OUT_DIR / "hook_deps.json").write_text(json.dumps(hooks, ensure_ascii=False, indent=2))
    print(f"[saved] {OUT_DIR}/hook_deps.json", file=sys.stderr)

    agents = analyze_agents()
    (OUT_DIR / "agent_deps.json").write_text(json.dumps(agents, ensure_ascii=False, indent=2))
    print(f"[saved] {OUT_DIR}/agent_deps.json", file=sys.stderr)

    dead = find_dead_candidates(memory, hooks, agents)
    (OUT_DIR / "dead_candidates.json").write_text(json.dumps(dead, ensure_ascii=False, indent=2))
    print(f"[saved] {OUT_DIR}/dead_candidates.json", file=sys.stderr)

    high = find_high_ref(memory, hooks, agents)
    write_summary(memory, hooks, agents, dead, high)
    print(f"[saved] {OUT_DIR}/inventory_summary.md", file=sys.stderr)

    print(f"\n=== Summary ===")
    print(f"memory: {len(memory)} ({len(dead['memory'])} dead candidates)")
    print(f"hooks: {len(hooks)} ({len(dead['hooks'])} dead candidates)")
    print(f"agents: {len(agents)} ({len(dead['agents'])} dead candidates)")


if __name__ == "__main__":
    main()
