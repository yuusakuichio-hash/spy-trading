#!/usr/bin/env python3
"""
manual_toggle_audit.py — 手動トグルで安全装置を回避できるフィールドの汎用検出

設計原則:
- data/*.json の全フィールドを安全装置系パターンでスキャン
- コード側で derived されているか単純 read なのかを判定
- 3分類: CRITICAL(致命) / HIGH(要検討) / OK で出力
- 毎週月曜 07:00 JST に launchctl で定期実行

Usage:
    python3 scripts/manual_toggle_audit.py [--json] [--data-dir PATH]

Exit codes:
    0: CRITICALなし
    1: CRITICAL 1件以上検出
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# 安全装置系フィールドパターン定義
# ============================================================
# (regex, category_label, severity, description_ja)
SAFETY_PATTERNS: list[tuple[str, str, str, str]] = [
    # --- CRITICAL: 直接発注制御に影響するトグル ---
    (r".*_constrained$",   "TOGGLE_CONSTRAINT",  "CRITICAL", "制約フラグ。手動でFalseに書き換えると安全装置がバイパスされる"),
    (r".*_bypass$",         "TOGGLE_BYPASS",       "CRITICAL", "バイパスフラグ。存在自体が手動回避口になりうる"),
    (r"^kill_.*",           "KILL_SWITCH",         "CRITICAL", "キルスイッチ系フィールド"),
    (r"^force_.*",          "FORCE_OVERRIDE",      "CRITICAL", "強制上書き系フィールド"),
    (r"^override_.*",       "OVERRIDE",            "CRITICAL", "オーバーライド系フィールド"),
    (r"^trading_mode$",     "TRADING_MODE",        "CRITICAL", "トレードモード。paper/live の切り替えが直接発注に影響"),
    (r"^pdt_constrained$",  "PDT_CONSTRAINED",     "CRITICAL", "PDT制約フラグ。derived であるべきだが state.json に残留すると手動書き換え可能"),
    # --- HIGH: 間接的に発注量・損切りに影響 ---
    (r".*_enabled$",        "TOGGLE_ENABLED",      "HIGH",     "有効化フラグ。手動でFalseにするとBotが静止する可能性"),
    (r".*_disabled$",       "TOGGLE_DISABLED",     "HIGH",     "無効化フラグ。手動でTrueにするとBotが停止する可能性"),
    (r".*_blocked$",        "TOGGLE_BLOCKED",      "HIGH",     "ブロックフラグ"),
    (r"^mode$",             "MODE_FIELD",          "HIGH",     "汎用modeフィールド。内容次第では発注に影響"),
    (r".*_mode$",           "MODE_SUFFIX",         "HIGH",     "モード系サフィックス"),
    (r"^capital.*",         "CAPITAL_FIELD",       "HIGH",     "資本量フィールド。過大/過小書き換えで発注量が狂う"),
    (r"^budget.*",          "BUDGET_FIELD",        "HIGH",     "予算フィールド"),
    (r"^phase.*",           "PHASE_CTRL",          "HIGH",     "フェーズ制御フィールド。発注パラメータが変わる可能性"),
    (r"^recovered$",        "RECOVERY_FLAG",       "HIGH",     "回復フラグ。Falseに書き換えると無限リトライ・Trueに書き換えると障害見落とし"),
    (r"^paused$",           "STATE_PAUSED",        "HIGH",     "一時停止フラグ"),
    # --- MEDIUM: 閾値系（書き換えでパラメータ逸脱）---
    (r"^max_.*",            "LIMIT_MAX",           "MEDIUM",   "上限値フィールド。過大書き換えで損失上限が無効化される可能性"),
    (r"^limit_.*",          "LIMIT_FIELD",         "MEDIUM",   "制限値フィールド"),
    (r"^min_.*",            "LIMIT_MIN",           "MEDIUM",   "下限値フィールド"),
    (r"^attempt$",          "ATTEMPT_COUNTER",     "MEDIUM",   "リトライカウンタ。0に書き換えるとウォッチドッグが即リセットされる"),
]

# コードで derived 処理されているフィールドの既知リスト
# (ファイルパターン, フィールド名, derived_by)
KNOWN_DERIVED: list[tuple[str, str, str]] = [
    ("atlas_state.json",                "pdt_constrained",  "common/trading_mode.py:get_pdt_constrained()"),
    ("atlas_watchdog_recovery_state.json", "recovered",     "atlas_watchdog.py:_attempt_recovery()"),
    ("chronos_watchdog_recovery_state.json", "recovered",   "atlas_watchdog.py:_attempt_recovery()"),
    ("atlas_watchdog_recovery_state.json", "attempt",       "atlas_watchdog.py:_attempt_recovery()"),
    ("chronos_watchdog_recovery_state.json", "attempt",     "atlas_watchdog.py:_attempt_recovery()"),
]

# 良性フィールド（誤検出しない）
ALLOWLIST: set[str] = {
    "last_boot", "last_cycle_jst", "last_attempt_ts", "started_at",
    "last_cycle_alerts", "consecutive_429", "backoff_until",
    "_pdt_notified_remaining0_date",
    "pdt_remaining", "pdt_rolling5",  # ← カウンタ系・制御フラグではない
}


def is_allowlisted(key: str) -> bool:
    return key in ALLOWLIST or key.startswith("_")


def classify_field(key: str, value: object) -> tuple[str, str, str] | None:
    """フィールドを分類する。マッチしなければ None を返す。"""
    if is_allowlisted(key):
        return None
    for pattern, category, severity, description in SAFETY_PATTERNS:
        if re.match(pattern, key, re.IGNORECASE):
            return category, severity, description
    return None


def is_derived(filename: str, key: str) -> tuple[bool, str]:
    """既知のderived fieldかチェック。(is_derived, derived_by) を返す。"""
    fname = os.path.basename(filename)
    for fpat, field, derived_by in KNOWN_DERIVED:
        if fpat == fname and field == key:
            return True, derived_by
    return False, ""


def scan_file(fpath: str) -> list[dict]:
    """JSONファイルをスキャンして検出リストを返す。"""
    findings = []
    try:
        with open(fpath, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return [{"file": fpath, "key": "(parse_error)", "error": str(e), "severity": "ERROR"}]

    if not isinstance(data, dict):
        return []

    for key, value in data.items():
        result = classify_field(key, value)
        if result is None:
            continue
        category, severity, description = result
        derived, derived_by = is_derived(fpath, key)

        # derived かつ severity=CRITICAL の場合は HIGH に下げる
        # （コードで管理されているなら手動書き換えリスクは中程度）
        effective_severity = severity
        if derived and severity == "CRITICAL":
            effective_severity = "HIGH"

        # bool/数値は手動書き換えが容易
        writeable = isinstance(value, (bool, int, float, str))

        findings.append({
            "file": os.path.basename(fpath),
            "key": key,
            "type": type(value).__name__,
            "value": value,
            "category": category,
            "severity": effective_severity,
            "original_severity": severity,
            "description": description,
            "derived": derived,
            "derived_by": derived_by,
            "writeable": writeable,
        })

    return findings


def run_scan(data_dir: str) -> list[dict]:
    """data_dir 配下の全 .json をスキャン。"""
    all_findings: list[dict] = []
    json_files = sorted(glob.glob(os.path.join(data_dir, "*.json")))
    for fpath in json_files:
        all_findings.extend(scan_file(fpath))
    return all_findings


def severity_rank(s: str) -> int:
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "OK": 3, "ERROR": -1}.get(s, 9)


def print_report(findings: list[dict], *, json_mode: bool = False) -> int:
    """レポートを出力し、CRITICAL件数を返す。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")

    findings.sort(key=lambda x: severity_rank(x.get("severity", "OK")))

    if json_mode:
        print(json.dumps({
            "scanned_at": now,
            "findings": findings,
            "critical_count": sum(1 for f in findings if f.get("severity") == "CRITICAL"),
            "high_count": sum(1 for f in findings if f.get("severity") == "HIGH"),
            "medium_count": sum(1 for f in findings if f.get("severity") == "MEDIUM"),
        }, ensure_ascii=False, indent=2))
        return sum(1 for f in findings if f.get("severity") == "CRITICAL")

    critical = [f for f in findings if f.get("severity") == "CRITICAL"]
    high = [f for f in findings if f.get("severity") == "HIGH"]
    medium = [f for f in findings if f.get("severity") == "MEDIUM"]

    print(f"\n{'='*60}")
    print(f"  手動トグル監査レポート  {now}")
    print(f"{'='*60}")
    print(f"  CRITICAL: {len(critical)}件  HIGH: {len(high)}件  MEDIUM: {len(medium)}件")
    print(f"{'='*60}\n")

    if critical:
        print("[CRITICAL] 致命的安全装置欠陥 — 即時対応必須")
        print("-" * 50)
        for f in critical:
            derived_tag = f"  (derived: {f['derived_by']})" if f["derived"] else "  (derived: 未確認 — コード側で管理されているか要確認)"
            print(f"  {f['file']}::{f['key']}")
            print(f"    値={f['value']!r}  型={f['type']}  カテゴリ={f['category']}")
            print(f"    説明: {f['description']}")
            print(f"    derived={f['derived']}{derived_tag}")
            print(f"    修正推奨: {'state.json への書き込みを除去し、コード側で毎回算出する' if not f['derived'] else 'state.json のフィールドを除去 (atlas_agent.py line 1024-1028 参照)'}")
            print()

    if high:
        print("[HIGH] 要検討 — 今週中に対応")
        print("-" * 50)
        for f in high:
            derived_tag = f"(derived: {f['derived_by']})" if f["derived"] else "(derived: 未確認)"
            print(f"  {f['file']}::{f['key']} = {f['value']!r}  {derived_tag}")
            print(f"    {f['description']}")
        print()

    if medium:
        print("[MEDIUM] 閾値フィールド — 次スプリント対応")
        print("-" * 50)
        for f in medium:
            print(f"  {f['file']}::{f['key']} = {f['value']!r}  ({f['category']})")
        print()

    if not findings:
        print("[OK] 安全装置系フィールドは検出されませんでした")

    print(f"{'='*60}")
    print(f"  スキャン完了: CRITICAL {len(critical)}件 / HIGH {len(high)}件 / MEDIUM {len(medium)}件")
    print(f"  ログ保存: data/logs/manual_toggle_audit.jsonl")
    print(f"{'='*60}\n")

    return len(critical)


def save_jsonl_log(findings: list[dict], data_dir: str) -> None:
    """監査結果を JSONL に追記。"""
    log_dir = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "manual_toggle_audit.jsonl")
    record = {
        "ts": datetime.now().isoformat(),
        "critical": sum(1 for f in findings if f.get("severity") == "CRITICAL"),
        "high": sum(1 for f in findings if f.get("severity") == "HIGH"),
        "medium": sum(1 for f in findings if f.get("severity") == "MEDIUM"),
        "findings": findings,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="手動トグル監査スクリプト")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    parser.add_argument("--data-dir", default="/Users/yuusakuichio/trading/data",
                        help="スキャン対象ディレクトリ (default: data/)")
    args = parser.parse_args()

    findings = run_scan(args.data_dir)
    save_jsonl_log(findings, args.data_dir)
    critical_count = print_report(findings, json_mode=args.json)

    return 1 if critical_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
