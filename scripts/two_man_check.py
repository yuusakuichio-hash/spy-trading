#!/usr/bin/env python3
"""
Two-Man Rule チェッカー
deploy-approval ラベルの最新 open Issue のコメントを検索し、
BUILDER_APPROVED と OPS_APPROVED が両方揃っているかを確認する。

Exit codes:
  0 — 両承認揃い (デプロイ続行)
  1 — エラー (GitHub CLI / API 失敗)
  2 — 承認不足 (デプロイブロック)
"""

import subprocess
import sys
import json
import os


REQUIRED_APPROVALS = {
    "BUILDER_APPROVED": False,
    "OPS_APPROVED": False,
}


def run(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_latest_deploy_issue() -> dict | None:
    """deploy-approval ラベルの最新 open Issue を取得"""
    code, out, err = run([
        "gh", "issue", "list",
        "--label", "deploy-approval",
        "--state", "open",
        "--limit", "1",
        "--json", "number,title,url,createdAt",
    ])
    if code != 0:
        print(f"ERROR: gh issue list failed: {err}", file=sys.stderr)
        return None
    if not out or out == "[]":
        return None
    issues = json.loads(out)
    return issues[0] if issues else None


def get_issue_comments(issue_number: int) -> list[dict]:
    """Issue のコメント一覧を取得"""
    code, out, err = run([
        "gh", "issue", "view",
        str(issue_number),
        "--json", "comments",
    ])
    if code != 0:
        print(f"ERROR: gh issue view failed: {err}", file=sys.stderr)
        return []
    data = json.loads(out)
    return data.get("comments", [])


def check_approvals(comments: list[dict]) -> dict[str, bool]:
    """コメント本文から承認キーワードを探す"""
    found = {k: False for k in REQUIRED_APPROVALS}
    for comment in comments:
        body = comment.get("body", "")
        for keyword in found:
            if keyword in body:
                found[keyword] = True
    return found


def main() -> int:
    # ISSUE_NUMBER 環境変数が指定されていれば優先使用（CI用）
    issue_number_env = os.environ.get("TWO_MAN_ISSUE_NUMBER")

    if issue_number_env:
        try:
            issue_number = int(issue_number_env)
            issue_url = f"(Issue #{issue_number} from env)"
        except ValueError:
            print(f"ERROR: TWO_MAN_ISSUE_NUMBER is not a valid integer: {issue_number_env}", file=sys.stderr)
            return 1
    else:
        issue = get_latest_deploy_issue()
        if not issue:
            print("ERROR: deploy-approval ラベルの open Issue が見つかりません。", file=sys.stderr)
            print("デプロイ前に Issue を作成し、builder/ops の承認を取得してください。", file=sys.stderr)
            return 2

        issue_number = issue["number"]
        issue_url = issue["url"]
        print(f"Issue #{issue_number}: {issue['title']}")
        print(f"URL: {issue_url}")

    comments = get_issue_comments(issue_number)
    print(f"コメント数: {len(comments)}")

    approvals = check_approvals(comments)

    print("\n--- 承認状態 ---")
    all_approved = True
    for keyword, approved in approvals.items():
        status = "OK" if approved else "MISSING"
        print(f"  {keyword}: {status}")
        if not approved:
            all_approved = False

    print()
    if all_approved:
        print("Two-Man Rule: PASSED — 両承認確認済み。デプロイを続行します。")
        return 0
    else:
        missing = [k for k, v in approvals.items() if not v]
        print(f"Two-Man Rule: BLOCKED — 承認不足: {', '.join(missing)}")
        print("Issue コメントに BUILDER_APPROVED / OPS_APPROVED を投稿してください。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
