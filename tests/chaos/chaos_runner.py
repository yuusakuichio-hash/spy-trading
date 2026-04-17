#!/usr/bin/env python3
"""Chaos Engineering Runner (Netflix Chaos Monkey 2011 思想)

atlas_agent の自動対応ロジック(L1-L4)を、市場クローズ中に故障注入で検証。
5シナリオ: OpenD切断 / Gammaループ / strike不整合 / Bot hang / Deep ITM

使い方:
    python3 tests/chaos/chaos_runner.py --scenario all [--wait 60]
    python3 tests/chaos/chaos_runner.py --scenario gamma_loop
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import subprocess
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
    # 5分で20行以上で常態化判定発火
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


def scenario_deep_itm():
    """Deep ITM異常価格発注試行 (L2 期待)"""
    _log("scenario: deep_itm")
    lines = [
        "[ORB] Entry option_price=$1697.30 (Deep ITM異常・delta=0.987)",
        "[ORB] Deep ITM ガード発動: 発注拒否",
    ]
    _inject(lines, "deep_itm")
    return {"expected_level": 2, "rule_hint": "deep_itm_detected"}


SCENARIOS = {
    "opend_disconnect": scenario_opend_disconnect,
    "gamma_loop": scenario_gamma_loop,
    "strike_mismatch": scenario_strike_mismatch,
    "bot_hang": scenario_bot_hang,
    "deep_itm": scenario_deep_itm,
}


def check_atlas_response(injected_meta, wait_sec=60):
    """注入後 wait_sec 待機して atlas_actions.log に反応あったか確認"""
    _log(f"待機 {wait_sec}秒...")
    time.sleep(wait_sec)
    if not ACTION_LOG.exists():
        return {"atlas_responded": False, "reason": "action_log 未存在（atlas_agent未稼働）"}
    # 直近 wait_sec 秒の action_log 行を見る
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all",
                    choices=list(SCENARIOS.keys()) + ["all"])
    ap.add_argument("--wait", type=int, default=60,
                    help="各シナリオ後の観察待機秒数")
    ap.add_argument("--report", action="store_true",
                    help="chaos_reports/ にレポート書き込み")
    args = ap.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts_label = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []

    scenarios_to_run = [args.scenario] if args.scenario != "all" else list(SCENARIOS.keys())

    for sname in scenarios_to_run:
        fn = SCENARIOS[sname]
        meta = fn()
        response = check_atlas_response(meta, wait_sec=args.wait)
        results.append({
            "scenario": sname,
            "injected": meta,
            "atlas_response": response,
            "pass": response.get("atlas_responded", False),
        })

    passed = sum(1 for r in results if r["pass"])
    _log(f"完了: {passed}/{len(results)} シナリオで atlas_agent が反応")

    if args.report:
        report_path = REPORT_DIR / f"chaos_{ts_label}.md"
        lines = [
            f"# Chaos Engineering Report — {ts_label}",
            "",
            f"**結果**: {passed}/{len(results)} シナリオ で atlas_agent 反応確認",
            "",
            "## 理論",
            "Netflix Chaos Monkey (2011) 思想。「壊れないものを作るのではなく、壊れても回復できるシステムを作る」。",
            "",
            "## シナリオ別結果",
            "",
        ]
        for r in results:
            icon = "✅" if r["pass"] else "🔴"
            lines.append(f"### {icon} {r['scenario']}")
            lines.append(f"- 注入内容: {r['injected'].get('rule_hint', 'N/A')} / 期待Level: {r['injected'].get('expected_level')}")
            resp = r['atlas_response']
            lines.append(f"- atlas_agent反応: {resp.get('response_count', 0)} 件 / 反応あり: {resp.get('atlas_responded')}")
            if resp.get('reason'):
                lines.append(f"- 注記: {resp['reason']}")
            lines.append("")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        _log(f"report: {report_path}")
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))

    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
