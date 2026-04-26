#!/usr/bin/env python3
"""
scripts/autonomous_sentinel.py — Sora Lab 自律監視番兵

30分毎 cron 起動 (com.soralab.autonomous_sentinel.plist) 。
ゆうさくさん睡眠中にも以下を自律実行する:

  1. Atlas Bot / Chronos Bot / 各 monitor の状態確認
  2. 自動修復 (auto-remediation): crash/log silence/webhook inactive
  3. 修復不能な異常は 複数経路 escalation
  4. unified status dashboard 生成 (data/ops/unified_status.md)

Escalation 経路 (優先順):
  A. Pushover       — 既存 client 経由 (ban 中はキューへ)
  B. LINE Notify    — LINE_NOTIFY_TOKEN で即送信
  C. Gmail SMTP     — 自前 SMTP (yuusakuichio@gmail.com 宛)
  D. macOS 音声通知  — osascript say + display notification
  E. ファイル flagging — data/ops/ESCALATION_PENDING.flag

Usage:
  python3 scripts/autonomous_sentinel.py           # 1回実行
  python3 scripts/autonomous_sentinel.py --smoke   # smoke test モード
  python3 scripts/autonomous_sentinel.py --dashboard # dashboard のみ生成
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── プロジェクトルート設定 ────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# .env ロード (LaunchAgent は shell env を継承しない)
def _load_env() -> None:
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

from common.pushover_client import send as pushover_send

# ── パス定数 ──────────────────────────────────────────────────────────────────
_TRADING_DIR  = Path(os.environ.get("SORA_TRADING_DIR", _PROJECT_ROOT))
OPS_DIR       = _TRADING_DIR / "data" / "ops"
HEARTBEAT_DIR = _TRADING_DIR / "data" / "heartbeats"
LOG_DIR       = _TRADING_DIR / "logs"
REMEDIATION_LOG = OPS_DIR / "remediation" / "auto_remediation_log.jsonl"
DASHBOARD_PATH  = OPS_DIR / "unified_status.md"
ESCALATION_FLAG = OPS_DIR / "ESCALATION_PENDING.flag"

OPS_DIR.mkdir(parents=True, exist_ok=True)
(OPS_DIR / "remediation").mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── ロガー設定 ────────────────────────────────────────────────────────────────
log = logging.getLogger("autonomous_sentinel")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] sentinel: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

_fh = logging.FileHandler(LOG_DIR / "autonomous_sentinel.log")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)

# ── 閾値定数 ──────────────────────────────────────────────────────────────────
HEARTBEAT_STALE_SEC   = 300   # 5分: heartbeat stale
LOG_SILENCE_SEC       = 600   # 10分: log 沈黙 → restart
QUOTE_SILENCE_SEC     = 300   # 5分: quote context 切断疑い

# ── Bot 定義 ──────────────────────────────────────────────────────────────────
# (process_name: LaunchAgent plist label で一意に識別できるもの)
BOTS: list[dict] = [
    {
        "name":       "atlas_agent",
        "plist":      "com.soralab.market_hours_atlas_monitor",
        "heartbeat":  "atlas_agent.json",
        "log":        _TRADING_DIR / "data" / "logs" / "atlas_agent.log",
        "python_script": str(_TRADING_DIR / "atlas_agent.py"),
        "critical":   True,
    },
    {
        "name":       "atlas_watchdog",
        "plist":      None,
        "heartbeat":  "atlas_watchdog.json",
        "log":        _TRADING_DIR / "data" / "logs" / "atlas_watchdog.log",
        "python_script": str(_TRADING_DIR / "atlas_watchdog.py"),
        "critical":   False,
    },
    {
        "name":       "chronos_agent",
        "plist":      "com.soralab.chronos_agent",
        "heartbeat":  "chronos_agent.json",
        "log":        _TRADING_DIR / "logs" / "chronos_agent.log",
        "python_script": str(_TRADING_DIR / "chronos_agent.py"),
        "critical":   True,
    },
    {
        "name":       "chronos_watchdog",
        "plist":      "com.soralab.chronos_bot",
        "heartbeat":  "chronos_watchdog.json",
        "log":        _TRADING_DIR / "logs" / "mffu_bot.log",
        "python_script": str(_TRADING_DIR / "chronos_watchdog.py"),
        "critical":   False,
    },
    {
        "name":       "sora_heartbeat_monitor",
        "plist":      None,
        "heartbeat":  None,
        "log":        _TRADING_DIR / "logs" / "sora_heartbeat_monitor.log",
        "python_script": str(_TRADING_DIR / "sora_heartbeat_monitor.py"),
        "critical":   False,
    },
]

# ── escalation state (重複防止) ───────────────────────────────────────────────
_ESCALATION_DEDUP_PATH = OPS_DIR / "escalation_dedup.json"
_ESCALATION_COOLDOWN_SEC = 1800  # 30分: 同一問題で再 escalation しない

def _load_escalation_dedup() -> dict:
    if _ESCALATION_DEDUP_PATH.exists():
        try:
            return json.loads(_ESCALATION_DEDUP_PATH.read_text())
        except Exception:
            pass
    return {}

def _save_escalation_dedup(d: dict) -> None:
    _ESCALATION_DEDUP_PATH.write_text(json.dumps(d))

def _is_recently_escalated(key: str) -> bool:
    d = _load_escalation_dedup()
    ts = d.get(key, 0)
    return (time.time() - ts) < _ESCALATION_COOLDOWN_SEC

def _mark_escalated(key: str) -> None:
    d = _load_escalation_dedup()
    d[key] = time.time()
    _save_escalation_dedup(d)


# ══════════════════════════════════════════════════════════════════════════════
# §1  状態確認ヘルパー
# ══════════════════════════════════════════════════════════════════════════════

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat_age_sec(hb_file: str) -> Optional[float]:
    """heartbeat ファイルの更新からの経過秒。ファイルなし → None"""
    path = HEARTBEAT_DIR / hb_file
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ts_str = data.get("ts", "")
        if not ts_str:
            return None
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now_utc() - ts).total_seconds()
    except Exception:
        return None


def _log_age_sec(log_path: Path) -> Optional[float]:
    """log ファイルの最終更新からの経過秒。ファイルなし → None"""
    if not log_path.exists():
        return None
    mtime = log_path.stat().st_mtime
    return time.time() - mtime


def _pid_running(pid: int) -> bool:
    """PID が生きているか確認"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _get_heartbeat_pid(hb_file: str) -> Optional[int]:
    path = HEARTBEAT_DIR / hb_file
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("pid")
    except Exception:
        return None


def _is_pgrep_alive(script_name: str) -> bool:
    """python3 <script_name> が pgrep で見つかるか"""
    try:
        res = subprocess.run(
            ["pgrep", "-f", script_name],
            capture_output=True, text=True, timeout=5
        )
        return res.returncode == 0
    except Exception:
        return False


def _check_bot(bot: dict) -> dict:
    """
    Bot の状態を確認し status dict を返す。
    status: "ok" | "stale" | "dead" | "unknown"
    """
    name = bot["name"]
    hb_file = bot.get("heartbeat")
    log_path = bot.get("log")

    result: dict = {
        "name":    name,
        "status":  "unknown",
        "hb_age":  None,
        "log_age": None,
        "pid":     None,
        "pid_alive": None,
        "details": [],
    }

    # heartbeat チェック
    if hb_file:
        hb_age = _heartbeat_age_sec(hb_file)
        result["hb_age"] = hb_age
        pid = _get_heartbeat_pid(hb_file)
        result["pid"] = pid
        if pid:
            result["pid_alive"] = _pid_running(pid)

        if hb_age is None:
            result["details"].append("heartbeat ファイルなし")
            result["status"] = "dead"
        elif hb_age > HEARTBEAT_STALE_SEC:
            result["details"].append(f"heartbeat {hb_age:.0f}秒前 (stale)")
            result["status"] = "stale"
        else:
            result["status"] = "ok"
            result["details"].append(f"heartbeat {hb_age:.0f}秒前 (正常)")

    # log チェック
    if log_path and Path(str(log_path)).exists():
        log_age = _log_age_sec(Path(str(log_path)))
        result["log_age"] = log_age
        if log_age is not None and log_age > LOG_SILENCE_SEC:
            result["details"].append(f"log {log_age:.0f}秒沈黙")
            if result["status"] == "ok":
                result["status"] = "stale"
    elif log_path:
        result["details"].append("log ファイルなし")

    # pgrep で最終確認
    script_path = bot.get("python_script", "")
    script_name = Path(script_path).name if script_path else name
    proc_alive = _is_pgrep_alive(script_name)
    if not proc_alive and result["status"] == "ok":
        result["status"] = "stale"
        result["details"].append("pgrep: プロセス見当たらず")
    elif proc_alive:
        result["details"].append("pgrep: 動作中")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# §2  Escalation 経路
# ══════════════════════════════════════════════════════════════════════════════

def _escalate_pushover(title: str, message: str, priority: int = 1) -> bool:
    """経路A: Pushover (ban 中はキューへ)"""
    try:
        return pushover_send(title, message, priority=priority)
    except Exception as e:
        log.warning("Pushover escalation failed: %s", e)
        return False


def _escalate_line(title: str, message: str) -> bool:
    """経路B: LINE Notify"""
    token = os.environ.get("LINE_NOTIFY_TOKEN", "")
    if not token:
        log.info("LINE_NOTIFY_TOKEN 未設定 → スキップ")
        return False
    try:
        import urllib.request
        import urllib.parse
        body = f"\n[Sora Lab Sentinel]\n{title}\n{message}"
        data = urllib.parse.urlencode({"message": body}).encode()
        req = urllib.request.Request(
            "https://notify-api.line.me/api/notify",
            data=data,
            headers={"Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.warning("LINE Notify escalation failed: %s", e)
        return False


def _escalate_gmail(title: str, message: str) -> bool:
    """経路C: Gmail SMTP (App Password) 経由でメール送信"""
    gmail_user  = os.environ.get("GMAIL_ALERT_USER",  "yuusakuichio@gmail.com")
    gmail_pass  = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not gmail_pass:
        log.info("GMAIL_APP_PASSWORD 未設定 → スキップ")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"{title}\n\n{message}\n\n-- Sora Lab Sentinel {_now_utc().isoformat()}")
        msg["Subject"] = f"[SoraLab ALERT] {title}"
        msg["From"]    = gmail_user
        msg["To"]      = gmail_user
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(gmail_user, gmail_pass)
            smtp.send_message(msg)
        log.info("Gmail escalation sent: %s", title)
        return True
    except Exception as e:
        log.warning("Gmail escalation failed: %s", e)
        return False


def _escalate_macos(title: str, message: str) -> bool:
    """経路D: macOS osascript 音声 + 画面通知"""
    if sys.platform != "darwin":
        return False
    try:
        # 画面通知
        notif_script = (
            f'display notification "{message[:100]}" '
            f'with title "[Sora Lab 緊急]" '
            f'subtitle "{title[:60]}" '
            f'sound name "Sosumi"'
        )
        subprocess.run(
            ["osascript", "-e", notif_script],
            capture_output=True, timeout=10
        )
        # 音声読み上げ
        speak_text = f"Sora Lab 緊急アラート。{title}。{message[:80]}"
        subprocess.run(
            ["say", "-v", "Kyoko", speak_text],
            capture_output=True, timeout=8
        )
        log.info("macOS notification sent: %s", title)
        return True
    except Exception as e:
        log.warning("macOS escalation failed: %s", e)
        return False


def _escalate_flag(title: str, message: str) -> bool:
    """経路E: ファイル flagging (フォールバック最終手段)"""
    try:
        payload = {
            "ts":      _now_utc().isoformat(),
            "title":   title,
            "message": message,
        }
        ESCALATION_FLAG.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("Escalation flag written: %s", ESCALATION_FLAG)
        return True
    except Exception as e:
        log.warning("Flag escalation failed: %s", e)
        return False


def escalate(title: str, message: str, dedup_key: str, priority: int = 1) -> dict:
    """
    全 escalation 経路を順に試みる。
    dedup_key が30分以内に既にエスカレート済みならスキップ。
    戻り値: {route: success_bool, ...}
    """
    if _is_recently_escalated(dedup_key):
        log.info("Escalation dedup: %s (skip)", dedup_key)
        return {}

    log.warning("ESCALATING: %s — %s", title, message)
    results: dict = {}

    results["pushover"] = _escalate_pushover(title, message, priority)
    results["line"]     = _escalate_line(title, message)
    results["gmail"]    = _escalate_gmail(title, message)
    results["macos"]    = _escalate_macos(title, message)
    results["flag"]     = _escalate_flag(title, message)

    any_success = any(results.values())
    log.info("Escalation results for '%s': %s (any_ok=%s)", dedup_key, results, any_success)
    _mark_escalated(dedup_key)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# §3  Auto-Remediation
# ══════════════════════════════════════════════════════════════════════════════

def _log_remediation(action: str, target: str, result: str, details: str = "") -> None:
    REMEDIATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":      _now_utc().isoformat(),
        "action":  action,
        "target":  target,
        "result":  result,
        "details": details,
    }
    with REMEDIATION_LOG.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info("Remediation logged: %s → %s (%s)", action, target, result)


def _launchctl_restart(plist_label: str) -> bool:
    """launchctl kickstart -k でサービスを強制再起動する"""
    if not plist_label:
        return False
    try:
        # まず unload → load (macOS launchctl)
        uid = os.getuid()
        domain = f"gui/{uid}"
        # kickstart -k = kill existing + restart
        res = subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{plist_label}"],
            capture_output=True, text=True, timeout=30
        )
        if res.returncode == 0:
            log.info("launchctl kickstart OK: %s", plist_label)
            return True
        else:
            log.warning("launchctl kickstart FAIL (%s): %s %s", plist_label, res.stdout, res.stderr)
            return False
    except Exception as e:
        log.warning("launchctl restart exception: %s", e)
        return False


def _python_launch_bg(script: str) -> bool:
    """python3 <script> をバックグラウンド起動する"""
    if not script or not Path(script).exists():
        return False
    try:
        proc = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("Launched %s (pid=%d)", script, proc.pid)
        return True
    except Exception as e:
        log.warning("python launch failed (%s): %s", script, e)
        return False


def remediate_bot(bot: dict, status: dict) -> bool:
    """
    Bot が stale / dead な場合の自動修復。
    plist がある → launchctl kickstart
    plist なし → python 直接起動
    戻り値: True = 修復試行した
    """
    name   = bot["name"]
    plist  = bot.get("plist")
    script = bot.get("python_script", "")

    log.info("Remediating %s (status=%s)", name, status["status"])

    success = False
    if plist:
        success = _launchctl_restart(plist)
        _log_remediation(
            action="launchctl_kickstart",
            target=name,
            result="ok" if success else "fail",
            details=f"plist={plist}",
        )
    else:
        success = _python_launch_bg(script)
        _log_remediation(
            action="python_launch_bg",
            target=name,
            result="ok" if success else "fail",
            details=f"script={script}",
        )

    return success


# ══════════════════════════════════════════════════════════════════════════════
# §4  Dashboard 生成
# ══════════════════════════════════════════════════════════════════════════════

_PRIORITY_COLOR = {
    "ok":      "青",
    "stale":   "黄",
    "dead":    "赤",
    "unknown": "灰",
}


def _format_age(sec: Optional[float]) -> str:
    if sec is None:
        return "N/A"
    if sec < 60:
        return f"{sec:.0f}秒"
    if sec < 3600:
        return f"{sec/60:.1f}分"
    return f"{sec/3600:.1f}時間"


def generate_dashboard(bot_statuses: list[dict], remediation_done: list[str]) -> str:
    now_jst = datetime.now(timezone(timedelta(hours=9)))
    lines: list[str] = [
        f"# Sora Lab Unified Status Dashboard",
        f"生成: {now_jst.strftime('%Y-%m-%d %H:%M JST')}",
        "",
        "## Bot 状態",
        "| Bot | 状態 | HB経過 | LOG経過 | PID | 詳細 |",
        "|-----|------|--------|---------|-----|------|",
    ]

    for s in bot_statuses:
        color = _PRIORITY_COLOR.get(s["status"], "灰")
        hb    = _format_age(s.get("hb_age"))
        la    = _format_age(s.get("log_age"))
        pid   = str(s.get("pid") or "-")
        det   = " / ".join(s.get("details", []))[:60]
        lines.append(f"| {s['name']} | {color} {s['status']} | {hb} | {la} | {pid} | {det} |")

    lines += [
        "",
        "## 今回の自動修復",
    ]
    if remediation_done:
        for r in remediation_done:
            lines.append(f"- {r}")
    else:
        lines.append("- なし")

    # 最新 remediation log 5件
    lines += ["", "## 直近 Auto-Remediation Log (最新5件)"]
    if REMEDIATION_LOG.exists():
        rows = [
            json.loads(l) for l in REMEDIATION_LOG.read_text().splitlines()
            if l.strip()
        ][-5:]
        for r in reversed(rows):
            ts_str = r.get("ts", "")[:19]
            lines.append(f"- `{ts_str}` {r.get('action')} → {r.get('target')} : **{r.get('result')}**")
    else:
        lines.append("- ログなし")

    # dead_man_ping 最新
    dmp = _TRADING_DIR / "data" / "ops" / "heartbeat" / "dead_man_ping.jsonl"
    lines += ["", "## Dead Man's Switch (直近 ping)"]
    if dmp.exists():
        rows = [json.loads(l) for l in dmp.read_text().splitlines() if l.strip()][-3:]
        for r in reversed(rows):
            ts_str = r.get("ts", "")[:19]
            comp = r.get("component", "?")
            lines.append(f"- `{ts_str}` {comp}")
    else:
        lines.append("- ファイルなし")

    # Pushover ban 状態
    pov_state_path = _TRADING_DIR / "data" / "pushover_client_state.json"
    lines += ["", "## Pushover 状態"]
    if pov_state_path.exists():
        try:
            ps = json.loads(pov_state_path.read_text())
            backoff_until = ps.get("backoff_until", 0.0)
            c429 = ps.get("consecutive_429", 0)
            if backoff_until > time.time():
                remain = int(backoff_until - time.time())
                lines.append(f"- **赤 BAN 中** (あと {remain}秒 / consecutive_429={c429})")
            else:
                lines.append(f"- 青 正常 (consecutive_429={c429})")
        except Exception:
            lines.append("- 状態ファイル読み込みエラー")
    else:
        lines.append("- 状態ファイルなし")

    lines += [
        "",
        "---",
        f"*次回更新: 30分後 (com.soralab.autonomous_sentinel)*",
        f"*閲覧: `cat /Users/yuusakuichio/trading/data/ops/unified_status.md`*",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# §5  メインロジック
# ══════════════════════════════════════════════════════════════════════════════

def run(smoke: bool = False) -> None:
    log.info("=== autonomous_sentinel start (smoke=%s) ===", smoke)

    bot_statuses: list[dict] = []
    remediation_done: list[str] = []

    for bot in BOTS:
        status = _check_bot(bot)
        bot_statuses.append(status)
        log.info("check %s: %s — %s", bot["name"], status["status"], status["details"])

        if smoke:
            # smoke モードでは Atlas を意図的に「dead」と判断してテスト
            if bot["name"] == "atlas_agent":
                status["status"] = "dead"
                status["details"].append("[smoke] 強制 dead")

        if status["status"] in ("dead", "stale"):
            # 自動修復試行
            remediated = remediate_bot(bot, status)
            action_str = "修復試行: " + bot["name"] + " → " + ("OK" if remediated else "FAIL")
            remediation_done.append(action_str)

            # 修復しても critical bot の場合は escalation
            if bot.get("critical"):
                esc_key = f"bot_down_{bot['name']}"
                esc_title = f"[SentinelALERT] {bot['name']} {status['status']}"
                esc_msg = (
                    f"{bot['name']} が {status['status']} 状態。\n"
                    f"自動修復: {'試行済' if remediated else '失敗'}\n"
                    f"詳細: {' / '.join(status['details'])}"
                )
                if smoke:
                    log.info("[smoke] would escalate: %s", esc_title)
                    # smoke でも macOS 通知は実際に送る
                    _escalate_macos(esc_title + " [smoke]", esc_msg)
                else:
                    escalate(esc_title, esc_msg, dedup_key=esc_key, priority=1)

    # Dashboard 生成
    dashboard_md = generate_dashboard(bot_statuses, remediation_done)
    DASHBOARD_PATH.write_text(dashboard_md, encoding="utf-8")
    log.info("Dashboard written: %s (%d chars)", DASHBOARD_PATH, len(dashboard_md))

    # smoke test: LINE + Gmail テスト送信
    if smoke:
        log.info("[smoke] Testing LINE Notify...")
        line_ok = _escalate_line("[smoke] sentinel test", "LINE 経路疎通確認")
        log.info("[smoke] LINE result: %s", line_ok)

        log.info("[smoke] Testing Gmail...")
        gmail_ok = _escalate_gmail("[smoke] sentinel test", "Gmail 経路疎通確認")
        log.info("[smoke] Gmail result: %s", gmail_ok)

        log.info("[smoke] Testing macOS notification...")
        mac_ok = _escalate_macos("[smoke] sentinel test", "macOS 通知経路疎通確認")
        log.info("[smoke] macOS result: %s", mac_ok)

        log.info("[smoke] Testing escalation flag...")
        flag_ok = _escalate_flag("[smoke] sentinel test", "Flag 経路疎通確認")
        log.info("[smoke] Flag result: %s", flag_ok)

        print(f"\n=== SMOKE RESULTS ===")
        print(f"LINE Notify : {'OK' if line_ok else 'FAIL (token 未設定の場合は正常)'}")
        print(f"Gmail SMTP  : {'OK' if gmail_ok else 'FAIL (GMAIL_APP_PASSWORD 未設定の場合は正常)'}")
        print(f"macOS notify: {'OK' if mac_ok else 'FAIL'}")
        print(f"Flag        : {'OK' if flag_ok else 'FAIL'}")
        print(f"Dashboard   : {DASHBOARD_PATH}")
        print(f"Remediation : {len(remediation_done)} actions")

    log.info("=== autonomous_sentinel done ===")


# ══════════════════════════════════════════════════════════════════════════════
# §6  エントリポイント
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Sora Lab 自律監視番兵")
    parser.add_argument("--smoke",     action="store_true", help="smoke test モード")
    parser.add_argument("--dashboard", action="store_true", help="dashboard のみ生成")
    args = parser.parse_args()

    if args.dashboard:
        # 状態チェックなしで dashboard だけ再生成
        statuses = [_check_bot(b) for b in BOTS]
        dash = generate_dashboard(statuses, [])
        DASHBOARD_PATH.write_text(dash, encoding="utf-8")
        print(f"Dashboard written: {DASHBOARD_PATH}")
        return

    run(smoke=args.smoke)


if __name__ == "__main__":
    main()
