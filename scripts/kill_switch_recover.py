#!/usr/bin/env python3
"""scripts/kill_switch_recover.py — KillSwitch 復旧・probe スクリプト (CRIT-R4-4)

責務:
- monitor.py の連続失敗後の自動 probe で使う復旧確認ユーティリティ
- KillSwitch 状態確認 + 手動解除（ゆうさくさん承認後）
- MonitorDaemon の「連続失敗 N 回 → EMERGENCY + 3秒待機 → probe → 回復で続行」と連携

設計:
- --probe: KillSwitch 状態を確認し問題がなければ exit 0（monitor が続行できる状態か確認）
- --deactivate: ゆうさくさん承認後に手動解除（activator を "yuusaku_manual" に固定）
- --status: 現在の KillSwitch 状態を表示

使用方法:
    # MonitorDaemon が連続失敗した場合に状態確認
    python3 scripts/kill_switch_recover.py --probe

    # KillSwitch が ARMED になっている場合は状態確認
    python3 scripts/kill_switch_recover.py --status

    # ゆうさくさん確認後に手動解除（必ず --reason を明記）
    python3 scripts/kill_switch_recover.py --deactivate --reason "latency_resolved_20260423"

終了コード:
    0: probe OK（monitor 続行可能）/ deactivate 成功 / status 表示
    1: probe NG（monitor は停止すべき）/ deactivate 失敗
    2: その他エラー（import 失敗等）

自爆ループ防止 (CRIT-R4-4):
    monitor の連続失敗が KillSwitch の ARMED 状態に起因する場合:
    1. KillSwitch を解除（ゆうさくさん確認後）
    2. monitor を再起動（launchctl または手動）
    3. probe で回復確認後に続行

    連続失敗が KillSwitch 以外（metric_provider エラー等）の場合:
    1. metric_provider の実装を確認・修正
    2. monitor を再起動
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 復旧手順 docstring（CRIT-R4-4: runbook連携）
# ---------------------------------------------------------------------------
_RECOVERY_GUIDE = """
KillSwitch 復旧手順 (CRIT-R4-4)
=================================

【状況別対応】

1. KillSwitch が ARMED で monitor が連続失敗している場合:
   a. 原因を確認:
      python3 scripts/kill_switch_recover.py --status

   b. 原因調査（audit log 確認）:
      cat data/state_v3/kill_switch_audit.jsonl | tail -20 | python3 -c "
      import sys, json
      for l in sys.stdin:
          d = json.loads(l)
          print(d.get('ts','')[:19], d.get('event'), d.get('reason','')[:60])
      "

   c. 問題解消後にゆうさくさん承認を受けて解除:
      python3 scripts/kill_switch_recover.py --deactivate --reason "原因_解消済"

   d. monitor 再起動:
      launchctl unload ~/Library/LaunchAgents/com.soralab.atlas-paper.plist
      launchctl load ~/Library/LaunchAgents/com.soralab.atlas-paper.plist

2. metric_provider が None または例外を返している場合:
   - atlas_v3/main.py の metric_provider 設定を確認
   - Bot プロセスが生きているか確認: ps aux | grep atlas
   - Bot を再起動してから monitor を再起動

3. 自爆ループが疑われる場合（連続失敗が繰り返す）:
   - monitor ログを確認:
     tail -50 data/state_v3/monitor_state.jsonl
   - 3秒待機 probe が何度も失敗している場合は手動停止:
     launchctl unload ~/Library/LaunchAgents/com.soralab.atlas-paper.plist
"""


# ---------------------------------------------------------------------------
# KillSwitch 状態確認
# ---------------------------------------------------------------------------

def _get_kill_switch_status() -> dict:
    """KillSwitch の現在状態を返す。"""
    try:
        from common_v3.risk.kill_switch import is_active, get_state
        active = is_active()
        state = get_state() or {}
        return {"active": active, "state": state, "error": None}
    except Exception as e:
        return {"active": None, "state": {}, "error": str(e)}


def _probe(verbose: bool = True) -> bool:
    """KillSwitch + metric_provider の状態を probe して回復可能かチェックする。

    Returns:
        True: probe OK（monitor 続行可能）
        False: probe NG（問題継続）
    """
    status = _get_kill_switch_status()

    if status["error"]:
        if verbose:
            print(f"[PROBE FAIL] KillSwitch module import error: {status['error']}", file=sys.stderr)
        return False

    if status["active"]:
        if verbose:
            state = status["state"]
            reason = state.get("reason", "unknown")
            ts = state.get("activated_at", "unknown")
            print(
                f"[PROBE FAIL] KillSwitch is ARMED. "
                f"Activated at: {ts}, reason: {reason}. "
                "Deactivate with: python3 scripts/kill_switch_recover.py --deactivate --reason <reason>",
                file=sys.stderr,
            )
        return False

    if verbose:
        print("[PROBE OK] KillSwitch is NOT armed. Monitor can continue.")
    return True


def _deactivate(reason: str, verbose: bool = True) -> bool:
    """KillSwitch を手動解除する（ゆうさくさん承認必須）。

    Returns:
        True: 解除成功
        False: 解除失敗
    """
    try:
        from common_v3.risk.kill_switch import deactivate, is_active
    except Exception as e:
        print(f"[DEACTIVATE FAIL] Import error: {e}", file=sys.stderr)
        return False

    if not is_active():
        if verbose:
            print("[DEACTIVATE] KillSwitch is already inactive. Nothing to do.")
        return True

    try:
        result = deactivate(activator="yuusaku_manual", reason=reason)
        if verbose:
            print(f"[DEACTIVATE OK] KillSwitch deactivated. result={result}")
        return True
    except Exception as e:
        print(f"[DEACTIVATE FAIL] {e}", file=sys.stderr)
        return False


def _show_status(verbose: bool = True) -> None:
    """KillSwitch の現在状態を詳細表示する。"""
    status = _get_kill_switch_status()

    if status["error"]:
        print(f"[STATUS ERROR] {status['error']}", file=sys.stderr)
        return

    active = status["active"]
    state = status["state"]

    print(f"KillSwitch active: {active}")
    if state:
        print(f"  activated_at: {state.get('activated_at', 'N/A')}")
        print(f"  reason:       {state.get('reason', 'N/A')}")
        print(f"  activator:    {state.get('activator', 'N/A')}")

    if active and verbose:
        print()
        print(_RECOVERY_GUIDE)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="KillSwitch 復旧・probe スクリプト (CRIT-R4-4)",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="KillSwitch 状態を probe して monitor 続行可能かチェック（exit 0=OK / 1=NG）",
    )
    parser.add_argument(
        "--deactivate",
        action="store_true",
        help="KillSwitch を手動解除する（ゆうさくさん承認必須。--reason を明記）",
    )
    parser.add_argument(
        "--reason",
        type=str,
        default=None,
        help="--deactivate 時の解除理由（必須）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="現在の KillSwitch 状態を表示する",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="詳細出力を抑制する",
    )

    args = parser.parse_args()

    if not (args.probe or args.deactivate or args.status):
        parser.print_help()
        sys.exit(0)

    if args.probe:
        ok = _probe(verbose=not args.quiet)
        sys.exit(0 if ok else 1)

    if args.deactivate:
        if not args.reason:
            print(
                "[ERROR] --deactivate requires --reason. "
                "Example: --reason 'latency_resolved_20260423'",
                file=sys.stderr,
            )
            sys.exit(2)
        ok = _deactivate(args.reason, verbose=not args.quiet)
        sys.exit(0 if ok else 1)

    if args.status:
        _show_status(verbose=not args.quiet)
        sys.exit(0)


if __name__ == "__main__":
    main()
