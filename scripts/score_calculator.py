#!/usr/bin/env python3
"""
score_calculator.py  -- LLM採点権剥奪・外部プロセス採点エンジン
Phase C 施策1

入力(コマンドライン引数 または --input-dir):
  --grep-result     grep出力ファイル(空=skip/fail)
  --ast-result      AST検証出力ファイル(空=skip/fail)
  --pytest-xml      pytest --junit-xml 出力ファイル
  --mutation-score  数値 (mutmut run後にmutmut results出力)
  --target-file     採点対象ファイル名(表示用)

出力:
  data/governance/scores.json  に結果書込
  exit 0  -> overall_pass=true
  exit 2  -> overall_pass=false  (採点失敗もexit 2)
  exit 1  -> 引数エラー

redteam / secretary は scores.json の overall_pass のみ参照する。
LLMが「〜はず」「〜と思われる」で採点する余地を排除する。
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
OUTPUT_PATH = f"{BASE}/data/governance/scores.json"
JST = timezone(timedelta(hours=9))

THRESHOLDS = {
    "mutation_score_min": 50,   # 50%未満 -> fail
    "pytest_fail_max": 0,       # 失敗テスト数 0まで許容
}


def parse_pytest_xml(xml_path: str) -> dict:
    """junit xml を解析して pass/fail/errors を返す"""
    if not xml_path or not os.path.exists(xml_path):
        return {"parsed": False, "reason": f"file not found: {xml_path}"}
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        if suite is None:
            # multiple testsuites
            suites = root.findall("testsuite")
            tests = sum(int(s.get("tests", 0)) for s in suites)
            failures = sum(int(s.get("failures", 0)) for s in suites)
            errors = sum(int(s.get("errors", 0)) for s in suites)
        else:
            tests = int(suite.get("tests", 0))
            failures = int(suite.get("failures", 0))
            errors = int(suite.get("errors", 0))
        passed = tests - failures - errors
        return {
            "parsed": True,
            "tests": tests,
            "failures": failures,
            "errors": errors,
            "passed": passed,
            "pass": (failures + errors) <= THRESHOLDS["pytest_fail_max"],
        }
    except Exception as e:
        return {"parsed": False, "reason": str(e)}


def check_grep_result(grep_path: str) -> dict:
    """grep出力ファイルを確認。空またはエラーなら fail"""
    if not grep_path:
        return {"checked": False, "reason": "not provided", "pass": False}
    if not os.path.exists(grep_path):
        return {"checked": False, "reason": f"file not found: {grep_path}", "pass": False}
    with open(grep_path) as f:
        content = f.read().strip()
    if not content:
        return {"checked": True, "pass": False, "reason": "grep output empty (no matches)", "content": ""}
    return {"checked": True, "pass": True, "content": content[:500]}


def check_ast_result(ast_path: str) -> dict:
    """AST検証ファイルを確認。'OK' または 'PASS' を含む行があれば pass"""
    if not ast_path:
        return {"checked": False, "reason": "not provided", "pass": False}
    if not os.path.exists(ast_path):
        return {"checked": False, "reason": f"file not found: {ast_path}", "pass": False}
    with open(ast_path) as f:
        content = f.read()
    lines = content.strip().splitlines()
    # 最後の行またはOK/PASSを含む行で判定
    last = lines[-1].strip() if lines else ""
    passed = any(
        kw in line.upper()
        for line in lines
        for kw in ("OK", "PASS", "SUCCESS", "NO ERROR")
    )
    # ERRORやFAILを含む行があれば強制fail
    has_error = any(
        kw in line.upper()
        for line in lines
        for kw in ("ERROR", "FAIL", "EXCEPTION", "TRACEBACK")
    )
    final_pass = passed and not has_error
    return {
        "checked": True,
        "pass": final_pass,
        "last_line": last,
        "has_error_keyword": has_error,
        "content": content[:500],
    }


def check_mutation_score(score_val) -> dict:
    """mutation score の数値チェック"""
    if score_val is None:
        return {"checked": False, "reason": "not provided", "pass": False, "score": None}
    try:
        score = float(score_val)
    except (ValueError, TypeError):
        return {"checked": False, "reason": f"invalid value: {score_val}", "pass": False, "score": None}
    return {
        "checked": True,
        "pass": score >= THRESHOLDS["mutation_score_min"],
        "score": score,
        "threshold": THRESHOLDS["mutation_score_min"],
    }


def main():
    parser = argparse.ArgumentParser(description="Governance Score Calculator")
    parser.add_argument("--grep-result", help="grep出力テキストファイルパス")
    parser.add_argument("--ast-result", help="AST検証出力テキストファイルパス")
    parser.add_argument("--pytest-xml", help="pytest --junit-xml 出力XMLファイルパス")
    parser.add_argument("--mutation-score", type=float, help="mutation score (0-100)")
    parser.add_argument("--target-file", default="(unknown)", help="採点対象ファイル名(表示用)")
    parser.add_argument("--allow-missing", action="store_true",
                        help="未提供項目をfailではなくskipとして扱う(デバッグ用)")
    args = parser.parse_args()

    now = datetime.now(JST).isoformat()

    grep_result = check_grep_result(args.grep_result)
    ast_result = check_ast_result(args.ast_result)
    pytest_result = parse_pytest_xml(args.pytest_xml)
    mutation_result = check_mutation_score(args.mutation_score)

    # overall_pass の判定
    # allow_missing=False (本番): 未提供 = fail
    checks_pass = []

    if args.allow_missing:
        # デバッグ: 提供されたものだけ判定
        if args.grep_result:
            checks_pass.append(grep_result.get("pass", False))
        if args.ast_result:
            checks_pass.append(ast_result.get("pass", False))
        if args.pytest_xml:
            checks_pass.append(pytest_result.get("pass", False))
        if args.mutation_score is not None:
            checks_pass.append(mutation_result.get("pass", False))
        overall_pass = all(checks_pass) if checks_pass else False
    else:
        # 本番: 全4項目必須
        checks_pass = [
            grep_result.get("pass", False),
            ast_result.get("pass", False),
            pytest_result.get("pass", False),
            mutation_result.get("pass", False),
        ]
        overall_pass = all(checks_pass)

    scores = {
        "timestamp": now,
        "target_file": args.target_file,
        "overall_pass": overall_pass,
        "checks": {
            "grep": grep_result,
            "ast": ast_result,
            "pytest": pytest_result,
            "mutation": mutation_result,
        },
        "thresholds": THRESHOLDS,
        "allow_missing": args.allow_missing,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)

    # stdout サマリー
    status_str = "PASS" if overall_pass else "FAIL"
    print(f"[score_calculator] {status_str} | target={args.target_file} | {now}")
    print(f"  grep:     {'PASS' if grep_result.get('pass') else 'FAIL'}")
    print(f"  ast:      {'PASS' if ast_result.get('pass') else 'FAIL'}")
    print(f"  pytest:   {'PASS' if pytest_result.get('pass') else 'FAIL'}")
    print(f"  mutation: {'PASS (' + str(mutation_result.get('score')) + '%)' if mutation_result.get('pass') else 'FAIL (' + str(mutation_result.get('score')) + '%)'}")
    print(f"  -> scores.json written: {OUTPUT_PATH}")

    sys.exit(0 if overall_pass else 2)


if __name__ == "__main__":
    main()
