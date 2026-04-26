#!/usr/bin/env python3
"""
purple_cell_audit.py  -- Purple Cell: Gemini API 独立監査エンジン
Phase C 施策3

Anthropic系バイアスの盲点を Google Gemini 2.5 Pro で独立検証する。
- redteam report を読み込み → Gemini API に送信 → メタ検証結果を保存
- Gemini API key は GEMINI_API_KEY 環境変数から取得
- 未設定時はログのみ・exit 0 (エラーにしない)

起動タイミング:
  - 週1回の大型cycle (日曜 02:00 JST LaunchAgentから)
  - 重大完了宣言時に手動/自動起動: python3 purple_cell_audit.py --trigger manual

出力:
  data/governance/purple_cell_YYYYMMDD_HHMMSS.json
  data/governance/purple_cell_latest.json (最新へのシンボリックリンク代替コピー)
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = "/Users/yuusakuichio/trading"
GOVERNANCE_DIR = f"{BASE}/data/governance"
JST = timezone(timedelta(hours=9))

PUSHOVER_USER = "u2cevk8nktib3sr148rw2hs78ecvux"
PUSHOVER_TOKEN_REPORT = "afv2594jgkc4jvh2vgf7dnnyft1gdi"

GEMINI_API_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

SYSTEM_PROMPT = """You are an independent security auditor reviewing an AI trading bot governance report.
Your task: Find critical blindspots, logical flaws, and undetected failure modes that the Blue Team (builders) missed.
Be adversarial. Assume the builders have optimization bias toward declaring success.
Focus on:
1. Test coverage gaps that mutation testing would reveal
2. Schema contract violations between modules
3. Self-referential test loops (tests written by the same agent that wrote the code)
4. Missing error path testing
5. State machine edge cases
6. Governance mechanism failures (can the guard itself be bypassed?)
Output in JSON format:
{
  "critical_issues": [...],
  "high_issues": [...],
  "audit_verdict": "PASS|CONDITIONAL_PASS|FAIL",
  "confidence": 0-100,
  "top_3_blindspots": [...]
}"""


def load_latest_redteam_report() -> str:
    """最新のredteamレポートを読み込む"""
    candidates = []

    # redteam review log
    review_log = f"{BASE}/data/logs/redteam_review.log" if os.path.exists(f"{BASE}/data/logs/redteam_review.log") else None

    # data/audit/ 以下のファイル
    audit_dir = f"{BASE}/data/audit"
    if os.path.exists(audit_dir):
        for f in Path(audit_dir).glob("*.md"):
            candidates.append((f.stat().st_mtime, str(f)))
        for f in Path(audit_dir).glob("*.json"):
            candidates.append((f.stat().st_mtime, str(f)))

    # data/ 直下の audit/redteam 関連ファイル
    for pattern in ["*redteam*", "*audit*", "*cycle*"]:
        for f in Path(f"{BASE}/data").glob(pattern):
            if f.is_file():
                candidates.append((f.stat().st_mtime, str(f)))

    # governance scores
    scores_path = f"{GOVERNANCE_DIR}/scores.json"
    if os.path.exists(scores_path):
        candidates.append((os.path.getmtime(scores_path), scores_path))

    if not candidates:
        return "(No redteam reports found)"

    # 最新3ファイルを結合
    candidates.sort(reverse=True)
    content_parts = []
    for _, fpath in candidates[:3]:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                text = f.read(3000)  # 各ファイル最大3000文字
            content_parts.append(f"=== {fpath} ===\n{text}")
        except Exception as e:
            content_parts.append(f"=== {fpath} === (read error: {e})")

    return "\n\n".join(content_parts)


def call_gemini(api_key: str, report_content: str) -> dict:
    """Gemini API を呼び出してメタ検証結果を取得"""
    url = f"{GEMINI_API_ENDPOINT}?key={api_key}"

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": f"{SYSTEM_PROMPT}\n\n## Report to Audit:\n{report_content[:8000]}"
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        }
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            response_body = resp.read().decode("utf-8")
            return json.loads(response_body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {error_body[:500]}")


def extract_text_from_gemini_response(response: dict) -> str:
    """Gemini レスポンスからテキスト部分を抽出"""
    try:
        candidates = response.get("candidates", [])
        if not candidates:
            return "{}"
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return "{}"
        return parts[0].get("text", "{}")
    except Exception:
        return "{}"


def send_pushover(title: str, message: str, priority: int = 0):
    try:
        args = [
            "curl", "-s",
            "--form-string", f"token={PUSHOVER_TOKEN_REPORT}",
            "--form-string", f"user={PUSHOVER_USER}",
            "--form-string", f"title={title}",
            "--form-string", f"message={message}",
            "--form-string", f"priority={priority}",
            "https://api.pushover.net/1/messages.json",
        ]
        subprocess.run(args, capture_output=True, timeout=10)
    except Exception as e:
        print(f"[purple_cell] Pushover error: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Purple Cell: Gemini Independent Audit")
    parser.add_argument("--trigger", default="scheduled", choices=["scheduled", "manual", "post-completion"],
                        help="起動理由")
    parser.add_argument("--report-file", help="監査対象レポートファイル (省略時は自動検索)")
    args = parser.parse_args()

    now = datetime.now(JST)
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")

    os.makedirs(GOVERNANCE_DIR, exist_ok=True)

    api_key = os.environ.get("GEMINI_API_KEY", "")

    # レポートを読み込む
    if args.report_file and os.path.exists(args.report_file):
        with open(args.report_file, encoding="utf-8", errors="replace") as f:
            report_content = f.read()
    else:
        report_content = load_latest_redteam_report()

    print(f"[purple_cell] START trigger={args.trigger} {now.isoformat()}")
    print(f"[purple_cell] report_content length={len(report_content)}")

    result = {
        "timestamp": now.isoformat(),
        "trigger": args.trigger,
        "api_key_set": bool(api_key),
        "report_content_length": len(report_content),
        "audit_result": None,
        "error": None,
        "status": "skipped",
    }

    if not api_key:
        print("[purple_cell] GEMINI_API_KEY not set. Logging only mode. (not an error)")
        result["status"] = "skipped_no_key"
        result["message"] = (
            "Gemini API key not configured. "
            "Set GEMINI_API_KEY env var to enable Purple Cell audits."
        )
    else:
        try:
            print("[purple_cell] Calling Gemini API...")
            raw_response = call_gemini(api_key, report_content)
            audit_text = extract_text_from_gemini_response(raw_response)

            # JSON パース試行
            try:
                audit_parsed = json.loads(audit_text)
            except json.JSONDecodeError:
                audit_parsed = {"raw_text": audit_text[:2000], "parse_error": True}

            result["audit_result"] = audit_parsed
            result["status"] = "completed"

            verdict = audit_parsed.get("audit_verdict", "UNKNOWN")
            confidence = audit_parsed.get("confidence", 0)
            critical_count = len(audit_parsed.get("critical_issues", []))

            print(f"[purple_cell] verdict={verdict} confidence={confidence}% critical={critical_count}")

            # Pushover通知 (FAIL または CRITICAL 1件以上の場合)
            if verdict == "FAIL" or critical_count > 0:
                issues_summary = "; ".join(
                    str(x)[:50] for x in audit_parsed.get("critical_issues", [])[:3]
                )
                send_pushover(
                    "[SYS] Purple Cell: FAIL",
                    f"verdict={verdict} confidence={confidence}%\nCRITICAL {critical_count}件\n{issues_summary}",
                    priority=1,
                )
            elif verdict == "CONDITIONAL_PASS":
                send_pushover(
                    "[SYS] Purple Cell: 条件付きPASS",
                    f"confidence={confidence}% | 確認推奨事項あり",
                    priority=0,
                )

        except Exception as e:
            print(f"[purple_cell] ERROR: {e}", file=sys.stderr)
            result["status"] = "error"
            result["error"] = str(e)

    # JSON保存
    out_path = f"{GOVERNANCE_DIR}/purple_cell_{timestamp_str}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # latest コピー
    latest_path = f"{GOVERNANCE_DIR}/purple_cell_latest.json"
    with open(latest_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[purple_cell] saved: {out_path}")
    print(f"[purple_cell] DONE status={result['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
