#!/usr/bin/env python3
"""Chaos Engineering Runner (Netflix Chaos Monkey 2011 思想)

atlas_agent の自動対応ロジック(L1-L4) + pre_trade_check + Kill Switch を
市場クローズ中に故障注入で検証。

Phase 1 (legacy) — 5シナリオ: OpenD切断 / Gammaループ / strike不整合 / Bot hang / Deep ITM
Phase 2 (full)   — 12シナリオ: pre_trade_check/Kill Switch/各戦術の破壊的テスト

使い方:
    python3 tests/chaos/chaos_runner.py --scenario all [--wait 60]
    python3 tests/chaos/chaos_runner.py --scenario all --mode full [--wait 0]
    python3 tests/chaos/chaos_runner.py --scenario gamma_loop
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
LOG = BASE / "data" / "logs" / "condor.log"
ACTION_LOG = BASE / "data" / "logs" / "atlas_actions.log"
REPORT_DIR = BASE / "data" / "chaos_reports"


def _log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _inject(lines, tag="chaos"):
    """condor.log に偽イベント注入"""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,000")
    with open(LOG, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(f"{ts} [WARNING] [CHAOS/{tag}] {line}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 シナリオ (legacy log-injection ベース)
# ─────────────────────────────────────────────────────────────────────────────

def scenario_opend_disconnect():
    """OpenD切断シミュレーション (Level 3 期待)"""
    _log("scenario: opend_disconnect")
    lines = [f"Quote context 切断 (attempt {i}/3)" for i in range(1, 4)]
    lines.append("OpenD connection lost: simulated chaos injection")
    _inject(lines, "opend_disconnect")
    return {"expected_level": 3, "rule_hint": "quote_context_disconnect"}


def scenario_gamma_loop():
    """GammaEarlyExitループ (L2 期待)"""
    _log("scenario: gamma_loop")
    lines = [
        f"[MassVerify] SPXW_CHAOS gamma_early_exit発射 (seq={i})"
        for i in range(1, 22)
    ]
    _inject(lines, "gamma_loop")
    return {"expected_level": 2, "rule_hint": "gamma_early_exit_loop"}


def scenario_strike_mismatch():
    """strike不整合大量発生 (L1-L2 期待)"""
    _log("scenario: strike_mismatch")
    lines = [
        f"[MassVerify_CS] CS SELL strike不整合 sell=5400.0 underlying=710.3 (chaos_seq={i})"
        for i in range(1, 16)
    ]
    _inject(lines, "strike_mismatch")
    return {"expected_level": 1, "rule_hint": "strike_mismatch"}


def scenario_bot_hang():
    """Bot応答停止 (L3 期待・180秒更新なし)"""
    _log("scenario: bot_hang (marker only)")
    _inject(["[BotHang] simulated: update_positions will stop for 180s"], "bot_hang")
    return {"expected_level": 3, "rule_hint": "bot_process_stale",
            "note": "実際の180秒 hang は atlas_agent の bot_process_stale 判定が mtime ベースなので、chaos_runner単体では再現不可。synthetic rule の動作は本質スキップ"}


def scenario_deep_itm_legacy():
    """Deep ITM異常価格発注試行 (L2 期待) — log injection only"""
    _log("scenario: deep_itm (legacy log)")
    lines = [
        "[ORB] Entry option_price=$1697.30 (Deep ITM異常・delta=0.987)",
        "[ORB] Deep ITM ガード発動: 発注拒否",
    ]
    _inject(lines, "deep_itm")
    return {"expected_level": 2, "rule_hint": "deep_itm_detected"}


LEGACY_SCENARIOS = {
    "opend_disconnect": scenario_opend_disconnect,
    "gamma_loop": scenario_gamma_loop,
    "strike_mismatch": scenario_strike_mismatch,
    "bot_hang": scenario_bot_hang,
    "deep_itm": scenario_deep_itm_legacy,
}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 シナリオ (inject_*.py 実コード検証ベース)
# ─────────────────────────────────────────────────────────────────────────────

def _run_inject_module(module_name: str) -> dict:
    """tests/chaos/inject_*.py を動的インポートして run() を実行"""
    import importlib
    chaos_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(chaos_dir))
    try:
        mod = importlib.import_module(module_name)
        return mod.run()
    except Exception as e:
        return {
            "scenario": module_name,
            "pass": False,
            "severity": "CRITICAL",
            "error": str(e),
        }


FULL_SCENARIOS = [
    ("deep_itm_naked",         "inject_deep_itm"),
    ("symbol_contamination",   "inject_symbol_contamination"),
    ("qcm_3x_disconnect",      "inject_qcm_cascade"),
    ("kill_switch",            "inject_kill_switch"),
    ("fat_finger_qty",         "inject_fat_finger_qty"),
    ("whitelist_bypass",       "inject_whitelist_bypass"),
    ("race_condition",         "inject_race_condition"),
    ("cross_bot_risk",         "inject_cross_bot_risk"),
    ("monthly_dd_kill_switch", "inject_monthly_dd"),
    ("api_rate_limit",         "inject_api_rate_limit"),
    ("dst_boundary",           "inject_dst_boundary"),
    ("tmr_mismatch",           "inject_tmr_mismatch"),
]


def check_atlas_response(injected_meta, wait_sec=60):
    """注入後 wait_sec 待機して atlas_actions.log に反応あったか確認"""
    if wait_sec > 0:
        _log(f"待機 {wait_sec}秒...")
        time.sleep(wait_sec)
    if not ACTION_LOG.exists():
        return {"atlas_responded": False, "reason": "action_log 未存在（atlas_agent未稼働）"}
    cutoff = datetime.datetime.now() - datetime.timedelta(seconds=wait_sec + 10)
    responses = []
    try:
        with open(ACTION_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    ts_str = obj.get("ts", "")
                    ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else None
                    if ts and ts.replace(tzinfo=None) >= cutoff:
                        responses.append(obj)
                except Exception:
                    continue
    except Exception as e:
        return {"atlas_responded": False, "reason": f"log read error: {e}"}
    return {
        "atlas_responded": len(responses) > 0,
        "response_count": len(responses),
        "responses": responses[:5],
    }


def _write_report(results: list, ts_label: str, mode: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date_label = ts_label[:8]
    report_path = BASE / "data" / f"chaos_report_{date_label}.md"

    passed = sum(1 for r in results if r.get("pass") or r.get("atlas_response", {}).get("atlas_responded"))
    criticals = [r for r in results if r.get("severity") == "CRITICAL" or
                 (not r.get("pass") and mode == "full")]

    lines = [
        f"# Chaos Engineering Report — {ts_label}",
        "",
        f"**モード**: {mode}",
        f"**結果**: {passed}/{len(results)} シナリオ 合格",
        "",
    ]

    if criticals:
        lines += [
            "## CRITICAL 失敗シナリオ (ガードが効かなかった)",
            "",
        ]
        for r in criticals:
            sname = r.get("scenario", "unknown")
            reason = r.get("actual_reason") or r.get("error") or r.get("loss_gate_reason") or "N/A"
            lines.append(f"- **{sname}**: {reason}")
        lines.append("")

    lines += [
        "## 理論",
        "Netflix Chaos Monkey (2011) 思想。「壊れないものを作るのではなく、壊れても回復できるシステムを作る」。",
        "",
        "## シナリオ別結果",
        "",
    ]

    for r in results:
        is_pass = r.get("pass") or r.get("atlas_response", {}).get("atlas_responded", False)
        icon = "PASS" if is_pass else "FAIL"
        sname = r.get("scenario", "unknown")
        lines.append(f"### [{icon}] {sname}")

        if "injected" in r:
            # legacy mode
            meta = r["injected"]
            lines.append(f"- 注入内容: {meta.get('rule_hint', 'N/A')} / 期待Level: {meta.get('expected_level')}")
            resp = r.get('atlas_response', {})
            lines.append(f"- atlas_agent反応: {resp.get('response_count', 0)} 件 / 反応あり: {resp.get('atlas_responded')}")
            if resp.get('reason'):
                lines.append(f"- 注記: {resp['reason']}")
        else:
            # full mode (inject_*.py)
            lines.append(f"- 説明: {r.get('description', 'N/A')}")
            lines.append(f"- 期待: {r.get('expected', 'N/A')}")
            lines.append(f"- 重要度: {r.get('severity', 'N/A')}")
            if "actual_layer" in r:
                lines.append(f"- 実際 layer={r['actual_layer']} allow={r.get('actual_allow')}")
            if "error" in r:
                lines.append(f"- エラー: {r['error']}")

        lines.append("")

    if mode == "full" and passed == len(results):
        lines += [
            "---",
            "## Atlas Phase 4 Chaos 安全認定",
            "",
            f"全 {len(results)} シナリオ合格。Atlas は Phase 4 chaos 安全と認定する。",
            f"認定日時: {ts_label}",
            "",
        ]

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _send_pushover(title: str, msg: str, priority: int = 0) -> None:
    """Pushover 通知"""
    # credentials.md — project .claude/skills/ を優先、なければ HOME
    cred_candidates = [
        BASE / ".claude" / "skills" / "credentials.md",
        Path.home() / ".claude" / "skills" / "credentials.md",
    ]
    cred_file = next((p for p in cred_candidates if p.exists()), None)
    token = ""
    user = ""
    if cred_file:
        text = cred_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            # credentials 内のコロンは全角「：」(U+FF1A)
            sep = "\uff1a"
            # Pushover USER
            if "Pushover USER" in line and sep in line:
                user = line.split(sep, 1)[1].strip().split()[0]
            # Pushover TOKEN (Sora Ops) — chaos/ops 通知に使用
            if "Pushover TOKEN (Sora Ops)" in line and sep in line:
                token = line.split(sep, 1)[1].strip().split()[0]

    if not token or not user:
        _log(f"[Pushover] credentials 未取得。通知スキップ: {title}")
        return

    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({
        "token": token,
        "user": user,
        "title": title,
        "message": msg,
        "priority": str(priority),
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=data,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            _log(f"[Pushover] sent: {resp.status} {title}")
    except Exception as e:
        _log(f"[Pushover] 送信失敗: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all",
                    choices=list(LEGACY_SCENARIOS.keys()) + ["all"] +
                    [name for name, _ in FULL_SCENARIOS])
    ap.add_argument("--wait", type=int, default=60,
                    help="legacy モード: 各シナリオ後の観察待機秒数")
    ap.add_argument("--mode", default="full", choices=["legacy", "full", "both"],
                    help="full=inject_*.py実コード検証(推奨) / legacy=log注入のみ / both=両方")
    ap.add_argument("--report", action="store_true",
                    help="chaos_reports/ にレポート書き込み (--mode full では常に書き込み)")
    args = ap.parse_args()

    ts_label = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []

    # ── full モード: inject_*.py 実コード検証 ──
    if args.mode in ("full", "both"):
        _log("=== Phase 2: full inject mode ===")

        if args.scenario == "all":
            targets = FULL_SCENARIOS
        else:
            targets = [(args.scenario, f"inject_{args.scenario}")]

        for sname, module_name in targets:
            _log(f"running: {sname} ({module_name})")
            result = _run_inject_module(module_name)
            result["scenario"] = result.get("scenario", sname)
            all_results.append(result)
            icon = "PASS" if result.get("pass") else "FAIL"
            severity = result.get("severity", "")
            _log(f"  [{icon}] {result.get('scenario')} severity={severity}")
            if not result.get("pass"):
                _log(f"  reason: {result.get('actual_reason') or result.get('error') or result.get('description')}")

    # ── legacy モード: log injection ──
    if args.mode in ("legacy", "both"):
        _log("=== Phase 1: legacy log-injection mode ===")

        if args.scenario == "all":
            legacy_targets = list(LEGACY_SCENARIOS.keys())
        else:
            legacy_targets = [args.scenario] if args.scenario in LEGACY_SCENARIOS else []

        for sname in legacy_targets:
            fn = LEGACY_SCENARIOS[sname]
            meta = fn()
            response = check_atlas_response(meta, wait_sec=args.wait)
            all_results.append({
                "scenario": sname,
                "injected": meta,
                "atlas_response": response,
                "pass": response.get("atlas_responded", False),
            })

    # 集計
    passed = sum(1 for r in all_results if r.get("pass") or
                 r.get("atlas_response", {}).get("atlas_responded", False))
    total = len(all_results)
    criticals = [r for r in all_results if not (r.get("pass") or
                 r.get("atlas_response", {}).get("atlas_responded", False))]
    has_critical = any(r.get("severity") == "CRITICAL" for r in criticals)

    _log(f"=== 完了: {passed}/{total} シナリオ合格 ===")

    # レポート生成 (full モードは常時)
    if args.report or args.mode in ("full", "both"):
        report_path = _write_report(all_results, ts_label, args.mode)
        _log(f"report: {report_path}")

    # Pushover 通知
    if args.mode in ("full", "both"):
        if passed == total:
            _send_pushover(
                "[Atlas/CHAOS] Chaos Engineering全戦術検証完了",
                f"全{total}シナリオ合格。Atlas Phase 4 chaos安全認定。",
                priority=0,
            )
        else:
            failed_names = [r.get("scenario") for r in criticals]
            priority = 1 if has_critical else 0
            _send_pushover(
                "[Atlas/CHAOS] CRITICAL: ガード未発動シナリオあり",
                f"{total - passed}/{total} 失敗: {failed_names}",
                priority=priority,
            )

    # JSON出力
    print(json.dumps(all_results, ensure_ascii=False, indent=2, default=str))

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
