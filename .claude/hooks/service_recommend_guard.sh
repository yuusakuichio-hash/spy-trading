#!/usr/bin/env python3
"""
service_recommend_guard: 外部サービス/プラン推奨時に調査証跡を強制
- 発話/Edit/Bashで「推奨」「おすすめ」「〜Plan」「登録」「契約」等のキーワード検知
- data/research_*.md に該当サービス名のファイルがあるか確認
- なければ exit 2 で発話ブロック
"""
import sys, json, re, os, glob, time
from datetime import datetime

log_file = "/Users/yuusakuichio/trading/data/logs/service_recommend_violations.log"
research_dir = "/Users/yuusakuichio/trading/data"

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

# Read/Grep/Glob/調査タスク系はスキップ
if tool_name in ("Read", "Grep", "Glob"):
    sys.exit(0)

check_text = json.dumps(tool_input, ensure_ascii=False)

# 規律文書/メモリ/調査ファイル編集はbypass
bypass_paths = ["memory/", "research_", "docs/", ".claude/hooks/", "premortem_reports/", "redteam_reports/"]
for p in bypass_paths:
    if p in check_text:
        sys.exit(0)

# サービス名辞書（拡張可能）
SERVICES = {
    "mffu": ["mffu", "myfundedfutures", "my funded futures", "builder plan", "rapid plan", "flex plan", "pro plan"],
    "backblaze": ["backblaze"],
    "databento": ["databento"],
    "thetadata": ["thetadata"],
    "collective2": ["collective2", "c2"],
    "tradovate": ["tradovate"],
    "ninjatrader": ["ninjatrader"],
    "topstep": ["topstep"],
    "apex": ["apex trader funding", "apextraderfunding"],
    "ftmo": ["ftmo"],
    "icloud": ["icloud+", "icloud plus"],
}

# 推奨動詞パターン
recommend_verbs = [
    "推奨", "おすすめ", "お勧め", "一択", "ベスト", "最適",
    "採用すべき", "選ぶべき", "これに決め",
    "登録", "契約", "購入", "支払い",
    "sign up", "subscribe", "purchase", "buy"
]

# キーワード検知
detected_services = []
for service, patterns in SERVICES.items():
    for pat in patterns:
        if pat.lower() in check_text.lower():
            detected_services.append(service)
            break

has_recommend_verb = any(v.lower() in check_text.lower() for v in recommend_verbs)

# 両方存在=要調査証跡
if detected_services and has_recommend_verb:
    # 調査ファイル存在確認
    found_files = []
    for service in detected_services:
        pattern = f"{research_dir}/research_{service}*.md"
        matches = glob.glob(pattern)
        if matches:
            found_files.extend(matches)
            continue
        # 日本語名対応
        pattern2 = f"{research_dir}/*{service}*research*.md"
        matches = glob.glob(pattern2)
        if matches:
            found_files.extend(matches)
            continue
        # Agent completion通知を探す（直近30分）
        recent_window = time.time() - 30 * 60

    missing_services = [s for s in detected_services if not any(s in f.lower() for f in found_files)]

    if missing_services:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"=== SERVICE RECOMMEND VIOLATION ===\n")
            f.write(f"Timestamp: {ts}\n")
            f.write(f"Tool: {tool_name}\n")
            f.write(f"Missing services: {missing_services}\n")
            f.write(f"Detected services: {detected_services}\n")
            f.write("---\n")
        sys.stderr.write(f"\n[SERVICE RECOMMEND GUARD] 調査未実施のサービス推奨を検知:\n")
        for s in missing_services:
            sys.stderr.write(f"  ❌ {s}: data/research_{s}*.md が存在しない\n")
        sys.stderr.write("\n[SERVICE RECOMMEND GUARD] 先に調査タスクを投入して data/research_<service>.md を作成してから推奨・登録案内すること\n")
        sys.stderr.write(f"[SERVICE RECOMMEND GUARD] 違反ログ: {log_file}\n")
        sys.exit(2)

sys.exit(0)
