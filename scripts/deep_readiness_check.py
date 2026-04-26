#!/usr/bin/env python3
"""deep_readiness_check.py — 実態ベースの readiness チェック (5項目)

既存 readiness_check の誤判定問題を修正:
- service active だけでなく POST /health 200 + JSON 正常値
- launchd list だけでなく 直近 N 秒 log 出力あり
- tactic record だけでなく trade record 実在
- memory file exists だけでなく hash chain 整合

使用例:
    python3 scripts/deep_readiness_check.py
    python3 scripts/deep_readiness_check.py --json
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "data" / "logs"
DATA_DIR = BASE_DIR / "data"

# ─── 設定 ────────────────────────────────────────────────────────────────────
# VPS 上の webhook は ssh 経由で curl (Mac から localhost 不可)
VPS_HOST = "root@198.13.37.17"
VPS_SSH_KEY = os.path.expanduser("~/.ssh/deploy_key")
VPS_WEBHOOK_URL = "http://localhost:8765/chronos/health"
LOG_RECENCY_SEC = 300          # 直近 5 分以内にログ出力があれば alive
TRADE_RECORD_FILE = DATA_DIR / "chronos_webhook_executions.jsonl"
HASH_CHAIN_FILE = DATA_DIR / "chronos_signal_hashes.jsonl"


# ─── チェック関数 ─────────────────────────────────────────────────────────────

def check_webhook_health() -> dict[str, Any]:
    """C1: VPS上の POST /chronos/health 200 + JSON 正常値確認 (ssh経由curl)"""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", VPS_SSH_KEY,
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                VPS_HOST,
                f"curl -s -o - -w '\\n%{{http_code}}' -X POST {VPS_WEBHOOK_URL}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        output_lines = result.stdout.strip().splitlines()
        http_code = output_lines[-1] if output_lines else "0"
        body = "\n".join(output_lines[:-1]) if len(output_lines) > 1 else ""
        try:
            data = json.loads(body)
            ok = http_code == "200" and data.get("status") == "ok"
        except Exception:
            ok = False
        return {
            "id": "C1",
            "name": "webhook /health JSON",
            "ok": ok,
            "detail": f"http={http_code} body={body[:100]}",
        }
    except Exception as e:
        return {"id": "C1", "name": "webhook /health JSON", "ok": False, "detail": str(e)}


def check_log_recency() -> dict[str, Any]:
    """C2: 直近 LOG_RECENCY_SEC 秒以内にログ出力あり"""
    log_files = [
        LOG_DIR / "atlas_agent_stdout.log",
        LOG_DIR / "atlas_agent_stderr.log",
    ]
    now = time.time()
    results = []
    for lf in log_files:
        if not lf.exists():
            results.append(f"{lf.name}: missing")
            continue
        mtime = lf.stat().st_mtime
        age = now - mtime
        if age <= LOG_RECENCY_SEC:
            results.append(f"{lf.name}: OK ({age:.0f}s ago)")
        else:
            results.append(f"{lf.name}: STALE ({age:.0f}s ago)")
    all_ok = all("OK" in r for r in results)
    return {
        "id": "C2",
        "name": "log recency",
        "ok": all_ok,
        "detail": " | ".join(results),
    }


def check_trade_record() -> dict[str, Any]:
    """C3: trade record 実在確認"""
    if not TRADE_RECORD_FILE.exists():
        return {
            "id": "C3",
            "name": "trade record exists",
            "ok": False,
            "detail": f"{TRADE_RECORD_FILE} missing",
        }
    lines = TRADE_RECORD_FILE.read_text().strip().splitlines()
    count = len(lines)
    ok = count > 0
    last = ""
    if lines:
        try:
            last_rec = json.loads(lines[-1])
            last = f"last={last_rec.get('ts', last_rec.get('timestamp', '?'))}"
        except Exception:
            last = f"last_line_len={len(lines[-1])}"
    return {
        "id": "C3",
        "name": "trade record exists",
        "ok": ok,
        "detail": f"count={count} {last}",
    }


def check_hash_chain() -> dict[str, Any]:
    """C4: hash chain 整合確認 (各行の prev_hash が前行のhashと一致)"""
    if not HASH_CHAIN_FILE.exists():
        # hash chain ファイルがない場合は skip (まだ記録なし)
        return {
            "id": "C4",
            "name": "hash chain integrity",
            "ok": True,
            "detail": f"{HASH_CHAIN_FILE.name} not found (skip — no signals yet)",
        }
    lines = HASH_CHAIN_FILE.read_text().strip().splitlines()
    if not lines:
        return {"id": "C4", "name": "hash chain integrity", "ok": True, "detail": "empty file (skip)"}

    errors = []
    prev_hash = ""
    for i, line in enumerate(lines):
        try:
            rec = json.loads(line)
            if i > 0:
                expected_prev = prev_hash
                actual_prev = rec.get("prev_hash", "")
                if actual_prev != expected_prev:
                    errors.append(f"row {i}: prev_hash mismatch")
            # 今の行の hash を計算
            payload = json.dumps({k: v for k, v in rec.items() if k != "row_hash"}, sort_keys=True)
            prev_hash = hashlib.sha256(payload.encode()).hexdigest()
        except json.JSONDecodeError:
            errors.append(f"row {i}: invalid JSON")

    ok = len(errors) == 0
    return {
        "id": "C4",
        "name": "hash chain integrity",
        "ok": ok,
        "detail": f"checked {len(lines)} rows" + (f" errors={errors}" if errors else " OK"),
    }


def check_vps_services() -> dict[str, Any]:
    """C5: VPS chronos サービス active 確認 (ssh経由)"""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", os.path.expanduser("~/.ssh/deploy_key"),
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                "root@198.13.37.17",
                "systemctl is-active chronos_webhook.service chronos_queue_reader.service",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = result.stdout.strip().splitlines()
        services = ["chronos_webhook", "chronos_queue_reader"]
        statuses = dict(zip(services, lines))
        all_ok = all(v == "active" for v in statuses.values())
        detail = " | ".join(f"{k}={v}" for k, v in statuses.items())
        return {"id": "C5", "name": "VPS services active", "ok": all_ok, "detail": detail}
    except Exception as e:
        return {"id": "C5", "name": "VPS services active", "ok": False, "detail": str(e)}


# ─── メイン ──────────────────────────────────────────────────────────────────

def run_checks() -> list[dict[str, Any]]:
    return [
        check_webhook_health(),
        check_log_recency(),
        check_trade_record(),
        check_hash_chain(),
        check_vps_services(),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Deep readiness check")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    ts = datetime.now(timezone.utc).isoformat()
    checks = run_checks()
    passed = sum(1 for c in checks if c["ok"])
    total = len(checks)
    overall_ok = passed == total

    if args.json:
        print(json.dumps({"ts": ts, "passed": passed, "total": total, "ok": overall_ok, "checks": checks}, indent=2))
        sys.exit(0 if overall_ok else 1)

    print(f"\n[deep_readiness_check] {ts}")
    print(f"Result: {passed}/{total} passed {'OK' if overall_ok else 'FAIL'}\n")
    for c in checks:
        icon = "OK" if c["ok"] else "NG"
        print(f"  [{icon}] {c['id']} {c['name']}")
        print(f"       {c['detail']}")
    print()
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
