#!/usr/bin/env python3
"""
generate_recent_corrections.py
過去7日のJSONLから叱責・訂正パターンを抽出して recent_corrections.md に保存する
SessionStart hook から呼び出すか、cron/LaunchAgent で毎朝実行する
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

JSONL_DIR = Path("/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading")
OUTPUT_FILE = Path("/Users/yuusakuichio/trading/data/recent_corrections.md")

# 叱責・訂正を示すパターン（ユーザー発言から検索）
CORRECTION_PATTERNS = [
    "違うでしょ",
    "なぜ",
    "また",
    "毎回",
    "聞き飽きた",
    "殺すよ",
    "何度も",
    "何回",
    "だから",
    "言ったでしょ",
    "なんで",
    "ちゃんと",
    "できてない",
    "間違え",
    "間違い",
    "おかしい",
    "ダメ",
    "だめ",
    "指示無視",
    "無視",
    "守れ",
    "守って",
    "規律",
    "違反",
    "先延ばし",
    "確認するな",
    "承認不要",
]

def extract_text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return " ".join(parts)
    return ""


def get_messages_from_jsonl(filepath):
    messages = []
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    msg_type = d.get("type", "")
                    timestamp = d.get("timestamp", "")
                    if msg_type in ("user", "assistant") and d.get("message"):
                        msg = d["message"]
                        if isinstance(msg, dict):
                            role = msg.get("role", msg_type)
                            content = msg.get("content", "")
                            text = extract_text_from_content(content)
                            if text.strip():
                                messages.append({
                                    "role": role,
                                    "text": text.strip(),
                                    "timestamp": timestamp,
                                })
                except Exception:
                    pass
    except Exception:
        pass
    return messages


def find_corrections(messages):
    corrections = []
    for i, msg in enumerate(messages):
        if msg["role"] != "user":
            continue
        text = msg["text"]
        matched_patterns = [p for p in CORRECTION_PATTERNS if p in text]
        if not matched_patterns:
            continue

        context_before = []
        j = i - 1
        collected = 0
        while j >= 0 and collected < 2:
            if messages[j]["role"] == "assistant":
                context_before.insert(0, messages[j])
                collected += 1
            j -= 1

        context_after = []
        j = i + 1
        while j < len(messages):
            if messages[j]["role"] == "assistant":
                context_after.append(messages[j])
                break
            j += 1

        corrections.append({
            "timestamp": msg["timestamp"],
            "patterns": matched_patterns,
            "context_before": context_before,
            "user_msg": msg,
            "context_after": context_after,
        })

    return corrections


def format_correction(c, index):
    lines = []
    ts = c["timestamp"] or "unknown"
    patterns_str = "、".join(c["patterns"][:3])
    lines.append(f"### [{index}] {ts[:16]} | パターン: {patterns_str}")
    lines.append("")
    for msg in c["context_before"]:
        preview = msg["text"][:200].replace("\n", " ")
        lines.append(f"**Claude:** {preview}")
        lines.append("")
    user_preview = c["user_msg"]["text"][:300].replace("\n", " ")
    lines.append(f"**ゆうさく:** {user_preview}")
    lines.append("")
    for msg in c["context_after"]:
        preview = msg["text"][:200].replace("\n", " ")
        lines.append(f"**Claude:** {preview}")
        lines.append("")
    lines.append("---")
    return "\n".join(lines)


def main():
    cutoff = datetime.now() - timedelta(days=7)
    jsonl_files = []
    for f in JSONL_DIR.glob("*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime >= cutoff:
                jsonl_files.append((mtime, f))
        except Exception:
            pass

    jsonl_files.sort(reverse=True)

    all_corrections = []
    for mtime, filepath in jsonl_files:
        messages = get_messages_from_jsonl(filepath)
        corrections = find_corrections(messages)
        for c in corrections:
            c["_file"] = filepath.name
        all_corrections.extend(corrections)

    all_corrections.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    all_corrections = all_corrections[:10]

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
    header = f"""# 直近の叱責・訂正コンテキスト（最新10件）
生成日時: {generated_at}
対象期間: 過去7日
用途: SessionStart hookでcontext injectionし、同じ違反を繰り返さないための参照

---

"""

    if not all_corrections:
        body = "（過去7日間に叱責・訂正パターンは検出されませんでした）\n"
    else:
        sections = [format_correction(c, i + 1) for i, c in enumerate(all_corrections)]
        body = "\n".join(sections) + "\n"

    OUTPUT_FILE.write_text(header + body, encoding="utf-8")
    print(f"[generate_recent_corrections] {len(all_corrections)}件抽出 -> {OUTPUT_FILE}")
    return len(all_corrections)


if __name__ == "__main__":
    count = main()
    sys.exit(0)
