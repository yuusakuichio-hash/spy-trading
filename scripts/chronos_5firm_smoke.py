#!/usr/bin/env python3
"""
scripts/chronos_5firm_smoke.py — 5 firm 制約シミュレーション smoke test

5 strategy それぞれで "allow 期待" と "block 期待" の 2 signal を送り、
chronos_firm_constraint_enforcer の判定を検証する。

実行:
    python3 scripts/chronos_5firm_smoke.py

出力:
    data/ops/chronos_5firm_smoke_20260421.md  — 結果レポート
    data/chronos_traderspost_executions.jsonl — 実送信ログ追記
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from chronos_firm_constraint_enforcer import FirmConstraintEnforcer, CheckResult  # noqa: E402

ROUTING_YAML = _HERE / "chronos_traderspost_routing.yaml"
EXEC_LOG = _HERE / "data" / "chronos_traderspost_executions.jsonl"
OPS_DIR = _HERE / "data" / "ops"
WEBHOOK_URL = os.environ.get("TRADERSPOST_WEBHOOK_URL_PAPER", "")

import urllib.request
import urllib.error


def _post_tp(payload: dict, timeout: float = 10.0) -> tuple[bool, dict]:
    """TradersPost webhook へ POST。URL 未設定時は dry-run。"""
    if not WEBHOOK_URL:
        return True, {"success": True, "dry_run": True, "logId": "dry-run-" + uuid.uuid4().hex[:8]}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Sora-Lab-Smoke/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            resp_dict = json.loads(raw)
            return resp_dict.get("success", False), resp_dict
    except Exception as e:
        return False, {"error": str(e)}


def _append_exec_log(entry: dict) -> None:
    EXEC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EXEC_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── テストケース定義 ─────────────────────────────────────────────────────────────
# (strategy_id, label, action, qty, daily_pnl_usd, now_et, expect_allowed, description)
def _make_now_et(hour: int, minute: int = 0) -> datetime.datetime:
    today = datetime.date.today()
    return datetime.datetime(today.year, today.month, today.day, hour, minute, 0)


TEST_CASES = [
    # ── Strategy 1: demo ─────────────────────────────────────────────────────────
    {
        "strategy_id": "chronos_orb_mes_demo",
        "label": "demo_allow",
        "action": "BUY", "qty": 5, "daily_pnl_usd": 0.0,
        "now_et": _make_now_et(10, 30),
        "expect_allowed": True,
        "description": "demo: 5枚・損失なし → allow",
    },
    {
        "strategy_id": "chronos_orb_mes_demo",
        "label": "demo_max_contracts_block",
        "action": "BUY", "qty": 11, "daily_pnl_usd": 0.0,
        "now_et": _make_now_et(10, 30),
        "expect_allowed": False,
        "description": "demo: 11枚 (max=10超) → block",
    },

    # ── Strategy 2: mffu_rapid ────────────────────────────────────────────────
    {
        "strategy_id": "chronos_orb_mes_rapid_sim",
        "label": "rapid_allow",
        "action": "BUY", "qty": 2, "daily_pnl_usd": -100.0,
        "now_et": _make_now_et(10, 0),
        "expect_allowed": True,
        "description": "rapid: 2枚・pnl -$100 (DLL $400未満) → allow",
    },
    {
        "strategy_id": "chronos_orb_mes_rapid_sim",
        "label": "rapid_dll_block",
        "action": "BUY", "qty": 1, "daily_pnl_usd": -400.0,
        "now_et": _make_now_et(13, 0),
        "expect_allowed": False,
        "description": "rapid: pnl -$400 (DLL $400 触達) → block",
    },

    # ── Strategy 3: mffu_pro ──────────────────────────────────────────────────
    {
        "strategy_id": "chronos_orb_mes_pro_sim",
        "label": "pro_allow",
        "action": "BUY", "qty": 4, "daily_pnl_usd": -500.0,
        "now_et": _make_now_et(9, 45),
        "expect_allowed": True,
        "description": "pro: 4枚 (max=5以内)・pnl -$500 → allow",
    },
    {
        "strategy_id": "chronos_orb_mes_pro_sim",
        "label": "pro_max_contracts_block",
        "action": "BUY", "qty": 6, "daily_pnl_usd": 0.0,
        "now_et": _make_now_et(10, 0),
        "expect_allowed": False,
        "description": "pro: 6枚 (max=5超) → block",
    },

    # ── Strategy 4: mffu_builder ───────────────────────────────────────────────
    {
        "strategy_id": "chronos_orb_mes_builder_sim",
        "label": "builder_allow",
        "action": "BUY", "qty": 2, "daily_pnl_usd": -200.0,
        "now_et": _make_now_et(10, 0),
        "expect_allowed": True,
        "description": "builder: 2枚・DLL $1000未満・15:55前 → allow",
    },
    {
        "strategy_id": "chronos_orb_mes_builder_sim",
        "label": "builder_dll_block",
        "action": "BUY", "qty": 1, "daily_pnl_usd": -1000.0,
        "now_et": _make_now_et(12, 0),
        "expect_allowed": False,
        "description": "builder: pnl -$1000 (DLL $1000 触達) → block",
    },
    {
        "strategy_id": "chronos_orb_mes_builder_sim",
        "label": "builder_force_close_block",
        "action": "BUY", "qty": 1, "daily_pnl_usd": 0.0,
        "now_et": _make_now_et(16, 0),
        "expect_allowed": False,
        "description": "builder: 16:00 ET (force_close_et=15:55超) → block",
    },

    # ── Strategy 5: tradeify ───────────────────────────────────────────────────
    {
        "strategy_id": "chronos_orb_mes_tradeify_sim",
        "label": "tradeify_allow",
        "action": "BUY", "qty": 3, "daily_pnl_usd": -300.0,
        "now_et": _make_now_et(10, 0),
        "expect_allowed": True,
        "description": "tradeify: 3枚・pnl -$300 (DLL $1250未満) → allow",
    },
    {
        "strategy_id": "chronos_orb_mes_tradeify_sim",
        "label": "tradeify_dll_block",
        "action": "BUY", "qty": 1, "daily_pnl_usd": -1250.0,
        "now_et": _make_now_et(14, 0),
        "expect_allowed": False,
        "description": "tradeify: pnl -$1250 (DLL $1250 触達) → block",
    },
]


def run_smoke() -> list[dict]:
    """全テストケースを実行し結果リストを返す。"""
    enforcer = FirmConstraintEnforcer(ROUTING_YAML)
    results = []

    for tc in TEST_CASES:
        signal_id = f"smoke_{tc['label']}_{uuid.uuid4().hex[:8]}"
        check: CheckResult = enforcer.check(
            strategy_id=tc["strategy_id"],
            action=tc["action"],
            qty=tc["qty"],
            daily_pnl_usd=tc["daily_pnl_usd"],
            now_et=tc["now_et"],
        )

        actual_allowed = check.allowed
        expected_allowed = tc["expect_allowed"]
        verdict = "PASS" if actual_allowed == expected_allowed else "FAIL"

        # allow の場合のみ TP webhook に実際に送信
        tp_sent = False
        tp_response: dict = {}
        if actual_allowed:
            tp_payload = {
                "ticker": "MES",
                "action": tc["action"].lower(),
                "quantity": tc["qty"],
                "sentiment": "bullish" if tc["action"].upper() == "BUY" else "bearish",
                "strategy_name": tc["strategy_id"],
                "firm": enforcer.get_strategy(tc["strategy_id"]).get("firm", ""),
                "signal_id": signal_id,
                "smoke_test": True,
            }
            tp_sent, tp_response = _post_tp(tp_payload)
        else:
            tp_payload = {}

        # exec log に追記
        entry = {
            "signal_id": signal_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "symbol": "MES",
            "action": tc["action"],
            "qty": tc["qty"],
            "routing_mode": "traderspost_paper",
            "strategy_id": tc["strategy_id"],
            "firm_constraint_result": "allow" if actual_allowed else "block",
            "firm_constraint_reason": check.reason,
            "daily_pnl_usd": tc["daily_pnl_usd"],
            "tp_payload": tp_payload if tp_sent else None,
            "tp_response": tp_response if tp_sent else None,
            "retries": 0,
            "error": None if actual_allowed else f"firm_constraint_blocked: {check.reason}",
            "smoke_test": True,
            "smoke_label": tc["label"],
        }
        _append_exec_log(entry)

        result = {
            "label": tc["label"],
            "strategy_id": tc["strategy_id"],
            "description": tc["description"],
            "expect_allowed": expected_allowed,
            "actual_allowed": actual_allowed,
            "firm_reason": check.reason,
            "tp_sent": tp_sent,
            "tp_log_id": tp_response.get("logId", tp_response.get("id", "")) if tp_sent else "",
            "tp_success": tp_response.get("success", False) if tp_sent else False,
            "verdict": verdict,
        }
        results.append(result)
        print(f"[{verdict}] {tc['label']}: allowed={actual_allowed} reason={check.reason[:60]}")

    return results


def _write_report(results: list[dict]) -> Path:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().strftime("%Y%m%d")
    report_path = OPS_DIR / f"chronos_5firm_smoke_{today}.md"

    passed = sum(1 for r in results if r["verdict"] == "PASS")
    total = len(results)

    lines = [
        f"# Chronos 5 Firm Smoke Test — {today}",
        "",
        f"実行時刻: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        f"結果: {passed}/{total} PASS",
        "",
        "| # | label | strategy_id | expect | actual | verdict | tp_logId | reason |",
        "|---|-------|-------------|--------|--------|---------|----------|--------|",
    ]
    for i, r in enumerate(results, 1):
        exp = "allow" if r["expect_allowed"] else "block"
        act = "allow" if r["actual_allowed"] else "block"
        log_id = r["tp_log_id"][:16] + "..." if len(r["tp_log_id"]) > 16 else r["tp_log_id"]
        reason_short = r["firm_reason"][:60]
        lines.append(
            f"| {i} | {r['label']} | {r['strategy_id']} | {exp} | {act} "
            f"| **{r['verdict']}** | {log_id} | {reason_short} |"
        )

    lines += [
        "",
        "## 詳細",
        "",
    ]
    for r in results:
        lines += [
            f"### {r['label']}",
            f"- description: {r['description']}",
            f"- expect_allowed: {r['expect_allowed']}",
            f"- actual_allowed: {r['actual_allowed']}",
            f"- verdict: **{r['verdict']}**",
            f"- firm_reason: `{r['firm_reason']}`",
            f"- tp_sent: {r['tp_sent']}",
            f"- tp_log_id: {r['tp_log_id']}",
            "",
        ]

    lines += [
        "## firm 別集計",
        "",
        "| firm | total | allow | block | fail |",
        "|------|-------|-------|-------|------|",
    ]
    firm_map: dict[str, dict] = {}
    for r in results:
        firm = r["strategy_id"].replace("chronos_orb_mes_", "").replace("_sim", "")
        if firm not in firm_map:
            firm_map[firm] = {"total": 0, "allow": 0, "block": 0, "fail": 0}
        firm_map[firm]["total"] += 1
        if r["actual_allowed"]:
            firm_map[firm]["allow"] += 1
        else:
            firm_map[firm]["block"] += 1
        if r["verdict"] == "FAIL":
            firm_map[firm]["fail"] += 1
    for firm, counts in firm_map.items():
        lines.append(
            f"| {firm} | {counts['total']} | {counts['allow']} "
            f"| {counts['block']} | {counts['fail']} |"
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[Report] written to {report_path}")
    return report_path


if __name__ == "__main__":
    results = run_smoke()
    report_path = _write_report(results)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    total = len(results)
    exit_code = 0 if passed == total else 1
    print(f"\n=== {passed}/{total} PASS | report: {report_path} ===")
    sys.exit(exit_code)
