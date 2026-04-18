#!/usr/bin/env python3
"""
mffu_emergency_stop.py — MFFU Bot 緊急停止スクリプト

使用場面:
  - MFFU口座の急激な損失発生時
  - Bot異常動作を検知した時
  - 手動での即時停止が必要な時
  - EOD DD接近で人的判断が必要な時

実行方法:
  python3 mffu_emergency_stop.py
  python3 mffu_emergency_stop.py --dry-run   # APIコール省略・動作確認のみ
  python3 mffu_emergency_stop.py --no-kill   # ポジション決済のみ・Bot停止なし
  python3 mffu_emergency_stop.py --no-close  # Bot停止のみ・ポジション決済なし

動作:
  1. LaunchAgent停止（com.mffubot）
  2. 稼働中のmffu_bot.pyプロセス全終了
  3. Tradovate DEMO/LIVE口座の全ポジションを市場価格で決済
  4. Pushover priority=2（緊急）でゆうさくさんに通知
  5. 緊急停止ログを data/mffu_emergency_stop.log に保存

注意: --live フラグなしではDEMO口座のみ操作する（デフォルト安全設計）
"""

from __future__ import annotations

import os
import sys
import json
import signal
import logging
import datetime
import argparse
import subprocess
import zoneinfo
from pathlib import Path

# ── .env ロード ────────────────────────────────────────────────────────────────
def _load_env_file():
    for candidate in [Path(__file__).parent / ".env", Path("/root/spxbot/.env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
            break

_load_env_file()

# ── パス定数 ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent / "data"
LOG_DIR  = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

EMERGENCY_LOG = LOG_DIR / "mffu_emergency_stop.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(EMERGENCY_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mffu_emergency_stop")

ET  = zoneinfo.ZoneInfo("America/New_York")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# ── 設定 ──────────────────────────────────────────────────────────────────────
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")

LAUNCHAGENT_LABEL = "com.mffubot"
BOT_SCRIPT_NAME   = "mffu_bot.py"


# ─────────────────────────────────────────────────────────────────────────────
# Pushover 通知
# ─────────────────────────────────────────────────────────────────────────────

def pushover_emergency(message: str, title: str = "MFFU 緊急停止") -> bool:
    """priority=2（緊急・確認必要）でPushover通知を送信する。"""
    try:
        import requests
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":    PUSHOVER_TOKEN,
                "user":     PUSHOVER_USER,
                "title":    title,
                "message":  message,
                "priority": 2,   # 緊急: 確認されるまで繰り返し通知
                "retry":    60,  # 60秒ごとに再通知
                "expire":   300, # 5分間繰り返し
                "sound":    "siren",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            log.info(f"[Pushover] emergency sent: {title}")
            return True
        log.error(f"[Pushover] HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.error(f"[Pushover] send failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# LaunchAgent 停止
# ─────────────────────────────────────────────────────────────────────────────

def stop_launchagent(label: str, dry_run: bool = False) -> bool:
    """LaunchAgent を停止する。"""
    cmd = ["launchctl", "unload",
           f"{Path.home()}/Library/LaunchAgents/{label}.plist"]
    log.info(f"[LaunchAgent] stopping: {' '.join(cmd)}")

    if dry_run:
        log.info("[LaunchAgent] dry-run: skipped")
        return True

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            log.info(f"[LaunchAgent] stopped: {label}")
            return True
        log.warning(f"[LaunchAgent] unload returned {result.returncode}: {result.stderr}")
        return False
    except Exception as e:
        log.error(f"[LaunchAgent] stop failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# プロセス終了
# ─────────────────────────────────────────────────────────────────────────────

def kill_bot_processes(script_name: str, dry_run: bool = False) -> int:
    """
    稼働中の mffu_bot.py プロセスを全て終了する。

    Returns:
        終了したプロセス数
    """
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", script_name],
            capture_output=True, text=True
        )
        pids = [int(p.strip()) for p in result.stdout.splitlines() if p.strip()]

        if not pids:
            log.info(f"[Kill] no running {script_name} processes found")
            return 0

        for pid in pids:
            log.info(f"[Kill] terminating PID {pid}: {script_name}")
            if dry_run:
                log.info(f"[Kill] dry-run: would kill PID {pid}")
                killed += 1
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                log.info(f"[Kill] SIGTERM sent to PID {pid}")
                killed += 1
            except ProcessLookupError:
                log.warning(f"[Kill] PID {pid} already gone")
            except PermissionError as e:
                log.error(f"[Kill] permission denied for PID {pid}: {e}")

    except Exception as e:
        log.error(f"[Kill] process search failed: {e}")

    return killed


# ─────────────────────────────────────────────────────────────────────────────
# Tradovate 全ポジション決済
# ─────────────────────────────────────────────────────────────────────────────

def close_all_positions(use_live: bool = False, dry_run: bool = False) -> dict:
    """
    Tradovate 口座の全ポジションを市場価格で決済する。

    Args:
        use_live: True = LIVE口座（本番）、False = DEMO口座（デフォルト）
        dry_run: True = API実行しない

    Returns:
        {
          "success": bool,
          "positions_closed": int,
          "results": list,
          "errors": list,
        }
    """
    env = "LIVE" if use_live else "DEMO"
    log.info(f"[CloseAll] environment: {env} dry_run={dry_run}")

    result = {
        "success":          False,
        "positions_closed": 0,
        "results":          [],
        "errors":           [],
    }

    if dry_run:
        log.info("[CloseAll] dry-run: skipped all API calls")
        result["success"] = True
        return result

    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from tradovate_client import TradovateClient

        client = TradovateClient(env=env)
        connected = client.connect()
        if not connected:
            result["errors"].append(f"Failed to connect to Tradovate {env}")
            log.error(f"[CloseAll] connection failed: {env}")
            return result

        positions = client.close_all_positions()
        result["positions_closed"] = len(positions) if positions else 0
        result["results"]          = positions or []
        result["success"]          = True
        log.info(f"[CloseAll] closed {result['positions_closed']} positions")

    except ImportError as e:
        result["errors"].append(f"tradovate_client import failed: {e}")
        log.error(f"[CloseAll] import error: {e}")
    except Exception as e:
        result["errors"].append(str(e))
        log.error(f"[CloseAll] exception: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 緊急停止ログ保存
# ─────────────────────────────────────────────────────────────────────────────

def save_emergency_record(
    reason:    str,
    close_result: dict,
    killed:    int,
    agent_stopped: bool,
) -> None:
    """緊急停止の記録を JSON に保存する。"""
    record = {
        "timestamp_jst":  datetime.datetime.now(JST).isoformat(),
        "timestamp_et":   datetime.datetime.now(ET).isoformat(),
        "reason":         reason,
        "agent_stopped":  agent_stopped,
        "processes_killed": killed,
        "positions_closed": close_result.get("positions_closed", 0),
        "close_success":  close_result.get("success", False),
        "close_errors":   close_result.get("errors", []),
    }

    records_file = BASE_DIR / "mffu_emergency_records.json"
    records = []
    if records_file.exists():
        try:
            records = json.loads(records_file.read_text())
        except Exception:
            records = []

    records.append(record)
    records_file.write_text(json.dumps(records, indent=2, ensure_ascii=False))
    log.info(f"[Record] saved to {records_file}")


# ─────────────────────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="MFFU Bot 緊急停止スクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python3 mffu_emergency_stop.py                # 通常緊急停止（DEMO口座）
  python3 mffu_emergency_stop.py --dry-run      # 動作確認のみ（API実行なし）
  python3 mffu_emergency_stop.py --no-kill      # ポジション決済のみ（プロセス停止なし）
  python3 mffu_emergency_stop.py --no-close     # プロセス停止のみ（ポジション決済なし）
  python3 mffu_emergency_stop.py --live         # LIVE口座のポジション決済（要注意）
        """
    )
    parser.add_argument("--dry-run",  action="store_true",
                        help="API実行なし・動作確認モード")
    parser.add_argument("--no-kill",  action="store_true",
                        help="プロセス停止をスキップ（ポジション決済のみ）")
    parser.add_argument("--no-close", action="store_true",
                        help="ポジション決済をスキップ（プロセス停止のみ）")
    parser.add_argument("--live",     action="store_true",
                        help="LIVE口座を操作（本番・要注意）")
    parser.add_argument("--reason",   type=str, default="手動緊急停止",
                        help="停止理由（ログ記録用）")
    args = parser.parse_args()

    now_jst = datetime.datetime.now(JST)
    now_et  = datetime.datetime.now(ET)

    log.info("=" * 60)
    log.info(f"MFFU 緊急停止 開始")
    log.info(f"JST: {now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}")
    log.info(f"ET:  {now_et.strftime('%Y-%m-%d %H:%M:%S ET')}")
    log.info(f"理由: {args.reason}")
    log.info(f"モード: dry_run={args.dry_run} no_kill={args.no_kill} "
             f"no_close={args.no_close} live={args.live}")
    log.info("=" * 60)

    # Step 1: LaunchAgent 停止
    agent_stopped = False
    if not args.no_kill:
        log.info("[Step 1] LaunchAgent 停止...")
        agent_stopped = stop_launchagent(LAUNCHAGENT_LABEL, dry_run=args.dry_run)
    else:
        log.info("[Step 1] LaunchAgent 停止: スキップ（--no-kill）")

    # Step 2: Bot プロセス終了
    killed = 0
    if not args.no_kill:
        log.info("[Step 2] Bot プロセス終了...")
        killed = kill_bot_processes(BOT_SCRIPT_NAME, dry_run=args.dry_run)
        log.info(f"[Step 2] {killed} プロセス終了")
    else:
        log.info("[Step 2] Bot プロセス終了: スキップ（--no-kill）")

    # Step 3: 全ポジション決済
    close_result = {"success": True, "positions_closed": 0, "results": [], "errors": []}
    if not args.no_close:
        log.info(f"[Step 3] 全ポジション決済 ({'LIVE' if args.live else 'DEMO'})...")
        close_result = close_all_positions(
            use_live=args.live,
            dry_run=args.dry_run,
        )
        if close_result["success"]:
            log.info(f"[Step 3] {close_result['positions_closed']} ポジション決済完了")
        else:
            log.error(f"[Step 3] 決済失敗: {close_result['errors']}")
    else:
        log.info("[Step 4] ポジション決済: スキップ（--no-close）")

    # Step 4: 緊急停止ログ保存
    log.info("[Step 4] 緊急停止ログ保存...")
    save_emergency_record(
        reason=args.reason,
        close_result=close_result,
        killed=killed,
        agent_stopped=agent_stopped,
    )

    # Step 5: Pushover 緊急通知
    log.info("[Step 5] Pushover 緊急通知...")
    env_label   = "LIVE" if args.live else "DEMO"
    close_count = close_result.get("positions_closed", 0)
    close_ok    = close_result.get("success", False)
    close_err   = close_result.get("errors", [])

    msg_lines = [
        f"理由: {args.reason}",
        f"時刻: {now_jst.strftime('%H:%M JST')} / {now_et.strftime('%H:%M ET')}",
        f"プロセス終了: {killed}件",
        f"ポジション決済: {close_count}件 ({'OK' if close_ok else 'FAILED'})",
    ]
    if close_err:
        msg_lines.append(f"エラー: {close_err[0][:80]}")
    if args.dry_run:
        msg_lines.append("(dry-run: API実行なし)")

    pushover_emergency(
        title=f"MFFU緊急停止 [{env_label}]",
        message="\n".join(msg_lines),
    )

    # 終了ステータス
    success = close_result.get("success", False) or args.no_close
    log.info("=" * 60)
    log.info(f"緊急停止 {'完了' if success else '一部失敗'}")
    log.info("=" * 60)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
