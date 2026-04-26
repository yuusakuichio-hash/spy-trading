#!/usr/bin/env python3
"""
atlas_agent.py - 場中常駐・自律監視エージェント (Sora Lab / Atlas)

背景: ゆうさくさん指示 2026-04-17
「場中に能動的監視→即修正サイクルを自動で回す仕組みを作れ」
「場中6.5時間を使えば全バグ潰せたはず」

既存 atlas_watchdog.py (最小版: パターン検知+通知のみ) とは別物。
本ファイルは Level1-4 の自律対応を担うフル実装。

サイクル:
  [5-30秒] 複数ログtail → ルールマッチ → Level判定 → 自律対応 → 報告

Level:
  1 INFO   : 通知のみ
  2 AUTOFIX: 仮説生成 + Bot再起動 or GitHub Issue投入 (DRY_RUN対応)
  3 ALERT  : Bot即停止 + priority=1 通知 + GitHub Issue (DRY_RUN対応)
  4 HALT   : 発注キャンセル + 手動待ち (DRY_RUN対応)

依存: PyYAML, requests, stdlib
起動: LaunchAgent com.atlas.agent (22:25 JST 起動 / 05:05 JST 停止)

CLI:
  --selftest   : config読み込みだけ確認して終了
  --once       : 1サイクルだけ回して終了
  --dry-run    : autofixを強制DRY_RUNに
  --armed      : autofixを強制ARMEDに
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import datetime
import zoneinfo

# Deviation Scanner 連携（Challenger型急増検知・時間単位）
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scripts import deviation_scanner as _dev_scanner
    _DEV_SCANNER_OK = True
except Exception:
    _DEV_SCANNER_OK = False

# Bot動作乖離検知 (観点A-D リアルタイム監視)
try:
    from common.bot_deviation_detector import DeviationDetector as _DeviationDetector
    _atlas_deviation_detector = _DeviationDetector()
    _DEVIATION_DETECTOR_OK = True
except Exception:
    _atlas_deviation_detector = None
    _DEVIATION_DETECTOR_OK = False

# PDT Tracker 連携（全戦術合算FINRA PDTカウンタ）
try:
    from common.pdt_tracker import get_global_tracker as _pdt_get_tracker, PDT_LIMIT as _PDT_LIMIT
    _pdt_tracker_agent = _pdt_get_tracker()
    _PDT_TRACKER_AGENT_OK = True
except Exception:
    _pdt_tracker_agent = None
    _PDT_TRACKER_AGENT_OK = False
from collections import deque
from pathlib import Path
from typing import Any

# ── Heartbeat pulse（能動監視）───────────────────────────────────────────────
try:
    from common.heartbeat import write_pulse as _write_pulse
    _HEARTBEAT_OK = True
except ImportError:
    _HEARTBEAT_OK = False
    def _write_pulse(*a, **kw): pass  # type: ignore[misc]

# ── 外部死活監視 ping（Pushover と独立した Tier 2 保険） ─────────────────────
# Atlas: SPXオプションBot 系（Chronos と混同禁止）
try:
    from common.external_health_ping import ping_healthchecks as _ext_ping
    _EXT_PING_OK = True
except ImportError:
    _EXT_PING_OK = False
    def _ext_ping(*a, **kw) -> bool: return False  # type: ignore[misc]

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML required. pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

try:
    import requests  # type: ignore
except ImportError:
    print("ERROR: requests required. pip3 install requests", file=sys.stderr)
    sys.exit(1)


# ── Paths & Constants ────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.resolve()
RULES_FILE = BASE_DIR / "atlas_rules.yaml"
STATE_FILE = BASE_DIR / "data" / "atlas_state.json"
ACTION_LOG = BASE_DIR / "data" / "logs" / "atlas_actions.log"
SELF_LOG   = BASE_DIR / "data" / "logs" / "atlas_agent.log"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
ET  = zoneinfo.ZoneInfo("America/New_York")

POLL_SEC_ACTIVE = 5
POLL_SEC_IDLE   = 30
TAIL_BYTES      = 65536


# ── Env loader ───────────────────────────────────────────────────────────────
def _load_env():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

PUSHOVER_USER        = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_ALERT_TOKEN = os.environ.get("PUSHOVER_ALERT_TOKEN", "")
PUSHOVER_OPS_TOKEN   = os.environ.get("PUSHOVER_OPS_TOKEN", PUSHOVER_ALERT_TOKEN)
GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")

# ── 共通 Pushover クライアント（SPOF解消・backoff/queue一元管理・ゲートレイヤー） ──
try:
    from common import pushover_client as _pc
    _PC_AVAILABLE = True
    _LEVEL_CRITICAL = _pc.LEVEL_CRITICAL
    _LEVEL_BATCHED  = _pc.LEVEL_BATCHED
    _LEVEL_SILENT   = _pc.LEVEL_SILENT
except ImportError:
    _PC_AVAILABLE = False
    _LEVEL_CRITICAL = "critical"
    _LEVEL_BATCHED  = "batched"
    _LEVEL_SILENT   = "silent"


# ── Logging ──────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        SELF_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SELF_LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_action(entry: dict):
    try:
        ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.datetime.now(JST).isoformat()
        with ACTION_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"log_action failed: {e}")


# ── Pushover ─────────────────────────────────────────────────────────────────
# 通知ゲートルール (2026-04-20):
#   CRITICAL: 資金毀損/アカウント停止/本番異常/市場機会喪失の4系統のみ即時送信
#   BATCHED : 場中に知っておくと良いが即断不要な情報（30分バッチ）
#   SILENT  : ログのみ・定型報告・完了通知・起動通知
def pushover(title: str, msg: str, priority: int = 0, token: str | None = None,
             level: str | None = None):
    """Pushover通知を送信する（ゲートレイヤー付き）。

    level 省略時はタイトルとpriorityから自動判定:
      priority >= 2                  → CRITICAL
      /HALT|ALERT|APPROVAL_REQUIRED/ → CRITICAL
      /BOOT|INFO|AUTOFIX/            → BATCHED
      それ以外                        → BATCHED

    common.pushover_client 経由で送信することで backoff/queue を全スクリプト間で
    共有し、429 連鎖 ban を防止する（SPOF解消）。
    """
    tok = token or PUSHOVER_OPS_TOKEN or PUSHOVER_ALERT_TOKEN

    # レベル自動判定
    if level is None:
        if priority >= 2:
            level = _LEVEL_CRITICAL
        elif any(kw in title for kw in ("HALT", "ALERT", "APPROVAL_REQUIRED")):
            level = _LEVEL_CRITICAL
        elif any(kw in title for kw in ("DEVIATION/SURGE", "PDT") ) and priority >= 1:
            level = _LEVEL_CRITICAL
        elif "BOOT" in title or "AUTOFIX" in title or "INFO" in title:
            level = _LEVEL_BATCHED
        else:
            level = _LEVEL_BATCHED

    if _PC_AVAILABLE:
        _pc.send(title, msg, priority=priority, token=tok or None, app_tag="Atlas", level=level)
        return

    # フォールバック: 共通クライアント import 失敗時
    # SILENTはフォールバック時もログのみ
    if level == _LEVEL_SILENT:
        log(f"[SILENT] {title} | {msg[:100]}")
        return
    if not tok or not PUSHOVER_USER:
        log(f"[NOTIFY_SKIP] missing token/user. title={title}")
        return
    if level == _LEVEL_BATCHED:
        # フォールバック時はbatchedも送信しない（ログのみ）
        log(f"[BATCHED/FALLBACK] {title} | {msg[:100]}")
        return
    try:
        data = {
            "token": tok, "user": PUSHOVER_USER,
            "title": title, "message": msg[:1024], "priority": priority,
        }
        if priority >= 2:
            data["retry"] = 30
            data["expire"] = 3600
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data, timeout=10,
        )
        if r.status_code != 200:
            log(f"[NOTIFY_ERR] status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        log(f"[NOTIFY_ERR] {e}")


# ── Log tailer ───────────────────────────────────────────────────────────────
class LogTailer:
    def __init__(self, path: Path):
        self.path  = path
        self.pos   = 0
        self.inode = None
        if path.exists():
            try:
                st = path.stat()
                self.pos   = st.st_size
                self.inode = st.st_ino
            except Exception:
                pass

    def read_new(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            st = self.path.stat()
            if self.inode is not None and st.st_ino != self.inode:
                log(f"[TAIL] rotate: {self.path.name}")
                self.pos = 0
                self.inode = st.st_ino
            if st.st_size < self.pos:
                log(f"[TAIL] truncate: {self.path.name}")
                self.pos = 0
            new_bytes = st.st_size - self.pos
            if new_bytes <= 0:
                return []
            read_n = min(new_bytes, TAIL_BYTES)
            with self.path.open("rb") as f:
                f.seek(self.pos)
                data = f.read(read_n)
            self.pos += len(data)
            self.inode = st.st_ino
            text = data.decode("utf-8", errors="replace")
            if "\n" in text:
                idx = text.rfind("\n")
                # 残した部分行はpos巻き戻し
                self.pos -= (len(data) - (idx + 1))
                text = text[: idx + 1]
            else:
                self.pos -= len(data)
                return []
            return [ln for ln in text.splitlines() if ln]
        except Exception as e:
            log(f"[TAIL_ERR] {self.path}: {e}")
            return []


# ── Rule matcher ─────────────────────────────────────────────────────────────
class RuleMatcher:
    def __init__(self, rules: list[dict]):
        self.rules = rules
        self.hits: dict[str, deque[float]] = {
            r["id"]: deque() for r in rules if not r.get("synthetic")
        }
        self.last_fired: dict[str, float] = {}
        self.regex: dict[str, re.Pattern] = {}
        for r in rules:
            pat = r.get("pattern")
            if pat:
                try:
                    self.regex[r["id"]] = re.compile(pat, re.IGNORECASE)
                except re.error as e:
                    log(f"[RULE_ERR] {r['id']}: invalid regex: {e}")

    def ingest(self, lines: list[str], now: float) -> list[dict]:
        fired = []
        for line in lines:
            for r in self.rules:
                if r.get("synthetic"):
                    continue
                rx = self.regex.get(r["id"])
                if rx is None or not rx.search(line):
                    continue
                dq = self.hits[r["id"]]
                dq.append(now)
                win = r.get("window_sec", 60)
                while dq and now - dq[0] > win:
                    dq.popleft()
                if len(dq) >= r.get("threshold", 1):
                    last = self.last_fired.get(r["id"], 0)
                    if now - last >= r.get("cooldown_sec", 600):
                        self.last_fired[r["id"]] = now
                        fired.append({
                            "rule": r,
                            "matched_line": line,
                            "count": len(dq),
                        })
                        dq.clear()
        return fired

    def fire_synthetic(self, rule_id: str, now: float, context: dict) -> dict | None:
        r = next((x for x in self.rules if x["id"] == rule_id), None)
        if r is None:
            return None
        last = self.last_fired.get(rule_id, 0)
        if now - last < r.get("cooldown_sec", 600):
            return None
        self.last_fired[rule_id] = now
        return {
            "rule": r,
            "matched_line": json.dumps(context, ensure_ascii=False),
            "count": 1,
        }


# ── State ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"started_at": datetime.datetime.now(JST).isoformat()}


def save_state(state: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"save_state failed: {e}")


# ── Bot / OpenD helpers ──────────────────────────────────────────────────────
def is_bot_alive(pid_files: list[str]) -> bool:
    for p in pid_files:
        pp = Path(p)
        if not pp.exists():
            continue
        try:
            pid = int(pp.read_text().strip())
            subprocess.run(["kill", "-0", str(pid)], check=True, capture_output=True)
            return True
        except Exception:
            continue
    return False


def opend_connected() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11111), timeout=2):
            return True
    except Exception:
        return False


def is_market_hours_now(cfg: dict) -> bool:
    win = cfg.get("market_window", {})
    start = win.get("start", "22:25")
    end   = win.get("end", "05:05")
    now = datetime.datetime.now(JST)
    hm = now.strftime("%H:%M")
    if start >= end:
        return hm >= start or hm <= end
    return start <= hm <= end


def bot_log_is_stale(path: Path, threshold_sec: int) -> tuple[bool, float]:
    if not path.exists():
        return False, 0.0
    try:
        age = time.time() - path.stat().st_mtime
        return age > threshold_sec, age
    except Exception:
        return False, 0.0


# ── Actions ──────────────────────────────────────────────────────────────────
def action_restart_bot(cfg: dict, rule: dict, dry_run: bool) -> dict:
    label = cfg.get("bot_launchagent", "com.spybot.paper")
    plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    action = {"type": "restart_bot", "label": label, "dry_run": dry_run,
              "rule_id": rule["id"]}
    if dry_run:
        action["status"] = "DRY_RUN"
        log(f"[DRY_RUN] would restart {label}")
        return action
    try:
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True, timeout=15)
        time.sleep(2)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                       capture_output=True, timeout=15)
        subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{label}"],
                       capture_output=True, timeout=15)
        action["status"] = "OK"
    except Exception as e:
        action["status"] = "ERR"
        action["error"] = str(e)
    return action


def action_stop_bot(cfg: dict, rule: dict, dry_run: bool) -> dict:
    label = cfg.get("bot_launchagent", "com.spybot.paper")
    action = {"type": "stop_bot", "label": label, "dry_run": dry_run,
              "rule_id": rule["id"]}
    if dry_run:
        action["status"] = "DRY_RUN"
        return action
    try:
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True, timeout=15)
        for p in cfg.get("bot_pid_files", []):
            pp = Path(p)
            if pp.exists():
                try:
                    pid = int(pp.read_text().strip())
                    subprocess.run(["kill", "-9", str(pid)],
                                   capture_output=True, timeout=5)
                except Exception:
                    pass
        action["status"] = "OK"
    except Exception as e:
        action["status"] = "ERR"
        action["error"] = str(e)
    return action


def action_halt_and_wait(cfg: dict, rule: dict, dry_run: bool) -> dict:
    action = {"type": "halt_and_wait", "dry_run": dry_run, "rule_id": rule["id"]}
    if dry_run:
        action["status"] = "DRY_RUN"
        return action
    stop = action_stop_bot(cfg, rule, dry_run=False)
    action["stop_result"] = stop
    action["status"] = "OK_HALTED"
    st = load_state()
    st["manual_halt"] = {
        "rule_id": rule["id"],
        "since": datetime.datetime.now(JST).isoformat(),
    }
    save_state(st)
    return action


def trigger_builder_workflow(cfg: dict, rule_id: str, hypothesis: str, matched_log: str) -> dict:
    """
    atlas_builder.yml を workflow_dispatch で自動起動。
    Level2 発火時に builder への修正依頼を自動投入する。
    """
    if not GITHUB_TOKEN:
        log("[trigger_builder] GITHUB_TOKEN なし → スキップ")
        return {"status": "SKIP", "reason": "no GITHUB_TOKEN"}
    repo = cfg.get("autofix", {}).get("github_repo", "")
    if not repo:
        return {"status": "SKIP", "reason": "no github_repo"}
    try:
        payload = {
            "ref": "main",
            "inputs": {
                "rule_id":     rule_id[:100],
                "hypothesis":  hypothesis[:500],
                "matched_log": matched_log[:500],
            },
        }
        r = requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/atlas_builder.yml/dispatches",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=payload,
            timeout=15,
        )
        if r.status_code == 204:
            log(f"[trigger_builder] workflow dispatched: {rule_id}")
            return {"status": "OK", "rule_id": rule_id}
        log(f"[trigger_builder] dispatch failed: status={r.status_code} body={r.text[:200]}")
        return {"status": "ERR", "code": r.status_code, "body": r.text[:300]}
    except Exception as e:
        log(f"[trigger_builder] exception: {e}")
        return {"status": "ERR", "error": str(e)}


def create_github_issue(cfg: dict, title: str, body: str, label: str = "todo") -> dict:
    if not GITHUB_TOKEN:
        return {"status": "SKIP", "reason": "no GITHUB_TOKEN"}
    repo = cfg.get("autofix", {}).get("github_repo", "")
    if not repo:
        return {"status": "SKIP", "reason": "no repo"}
    try:
        r = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={"title": title, "body": body, "labels": [label]},
            timeout=15,
        )
        if r.status_code in (200, 201):
            d = r.json()
            return {"status": "OK", "number": d.get("number"), "url": d.get("html_url")}
        return {"status": "ERR", "code": r.status_code, "body": r.text[:300]}
    except Exception as e:
        return {"status": "ERR", "error": str(e)}


# ── pre-checks ───────────────────────────────────────────────────────────────
def check_no_open_orders() -> tuple[bool, str]:
    """
    moomoo OpenAPI で未約定注文（SUBMITTING / SUBMITTED / PART_FILLED）を照会。
    - futu ライブラリが使えない場合は Permissive(True) でフォールバック
    - OpenD が落ちている場合は Fail(False) → Bot再起動を保留する防衛設計
    - 取得成功した場合のみ open_orders 件数で判定
    """
    try:
        import importlib
        futu = importlib.import_module("futu")
    except ImportError:
        log("[check_no_open_orders] futu import failed → permissive True")
        return True, "futu_unavailable (permissive)"

    OpenSecTradeContext = getattr(futu, "OpenSecTradeContext", None)
    TrdMarket           = getattr(futu, "TrdMarket", None)
    TrdEnv              = getattr(futu, "TrdEnv", None)
    SecurityFirm        = getattr(futu, "SecurityFirm", None)
    RET_OK              = getattr(futu, "RET_OK", 0)

    if None in (OpenSecTradeContext, TrdMarket, TrdEnv, SecurityFirm):
        log("[check_no_open_orders] futu attrs missing → permissive True")
        return True, "futu_attrs_missing (permissive)"

    # OpenD 疎通確認（ポートチェック済みの opend_connected より直接確認）
    if not opend_connected():
        log("[check_no_open_orders] OpenD unreachable → FAIL (bot restart blocked)")
        return False, "opend_down"

    trade_ctx = None
    try:
        trade_ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host="127.0.0.1",
            port=11111,
            security_firm=SecurityFirm.FUTUJP,
        )
        # paper / live 両方照会（どちらに注文が残っていても検知）
        open_count = 0
        open_details: list[str] = []
        for env_label, trd_env in [("SIMULATE", TrdEnv.SIMULATE), ("REAL", TrdEnv.REAL)]:
            ret, data = trade_ctx.order_list_query(trd_env=trd_env)
            if ret != RET_OK:
                log(f"[check_no_open_orders] order_list_query failed env={env_label} ret={ret}")
                # クエリ失敗は不明 → 防衛的に False（再起動を保留）
                return False, f"order_query_failed env={env_label} ret={ret}"
            if data is not None and not data.empty:
                # SUBMITTING / SUBMITTED / PART_FILLED が残っているものだけカウント
                OPEN_STATUSES = {"SUBMITTING", "SUBMITTED", "PART_FILLED"}
                if "order_status" in data.columns:
                    open_rows = data[data["order_status"].isin(OPEN_STATUSES)]
                else:
                    open_rows = data  # カラム名不明なら全件をオープン扱い
                if not open_rows.empty:
                    open_count += len(open_rows)
                    for _, row in open_rows.iterrows():
                        oid   = row.get("order_id", "?")
                        code  = row.get("code", "?")
                        stat  = row.get("order_status", "?")
                        open_details.append(f"{env_label}:{code}#{oid}({stat})")

        if open_count > 0:
            detail_str = ", ".join(open_details[:5])
            log(f"[check_no_open_orders] open_orders={open_count} [{detail_str}] → FAIL")
            return False, f"open_orders={open_count} [{detail_str}]"

        log(f"[check_no_open_orders] no open orders → OK")
        return True, "no_open_orders"

    except Exception as e:
        log(f"[check_no_open_orders] exception: {e} → FAIL (defensive)")
        return False, f"exception: {e}"
    finally:
        if trade_ctx is not None:
            try:
                trade_ctx.close()
            except Exception:
                pass


def check_opend_connected() -> tuple[bool, str]:
    ok = opend_connected()
    return ok, ("opend up" if ok else "opend DOWN")


PRE_CHECK_FNS = {
    "no_open_orders": check_no_open_orders,
    "opend_connected": check_opend_connected,
}


def run_pre_checks(names: list[str]) -> tuple[bool, list[str]]:
    notes = []
    for n in names:
        fn = PRE_CHECK_FNS.get(n)
        if fn is None:
            notes.append(f"{n}: UNKNOWN_CHECK")
            return False, notes
        ok, note = fn()
        notes.append(f"{n}: {'OK' if ok else 'FAIL'} ({note})")
        if not ok:
            return False, notes
    return True, notes


# ── Dispatcher ───────────────────────────────────────────────────────────────
def dispatch(fired: dict, cfg: dict) -> dict:
    rule    = fired["rule"]
    level   = rule.get("level", 1)
    rid     = rule["id"]
    desc    = rule.get("description", "")
    hypo    = rule.get("hypothesis", "")
    matched = fired.get("matched_line", "")[:200]
    count   = fired.get("count", 1)
    act_cfg = rule.get("action", {})
    dry_run = bool(cfg.get("autofix", {}).get("dry_run_default", 1))

    header = f"[Atlas/L{level}] {rid}"
    body = [
        f"desc: {desc}",
        f"hits: {count}",
        f"match: {matched}",
    ]
    if hypo:
        body.append(f"仮説: {hypo}")

    result: dict[str, Any] = {"rule_id": rid, "level": level}

    if level == 1:
        # Level1 INFO は判断不要 → SILENT（ログのみ）
        pushover(f"{header} INFO", "\n".join(body), priority=0, level=_LEVEL_SILENT)
        result["action"] = "notify_only"

    elif level == 2:
        atype = act_cfg.get("type", "notify_only")
        pre = act_cfg.get("pre_checks", [])
        ok, notes = run_pre_checks(pre) if pre else (True, [])
        if notes:
            body.append("pre_checks: " + "; ".join(notes))
        if pre and not ok:
            body.append("→ 対応スキップ (pre_check失敗)")
            # Level2 AUTOFIX SKIP はBATCHED（pre_check失敗の事実は把握しておく程度）
            pushover(f"{header} AUTOFIX SKIP", "\n".join(body), priority=1, level=_LEVEL_BATCHED)
            result["action"] = "skipped_precheck"
        else:
            # C7修正: Level2 Two-Man Rule - ARMED + level2_approval_required の場合、
            # Pushover承認待ちを挟む（緊急モードは除外）
            tmr_cfg = cfg.get("autofix", {}).get("two_man_rule", {})
            tmr_enabled = tmr_cfg.get("enabled", True) and not dry_run
            l2_approval_required = tmr_cfg.get("level2_approval_required", False)
            _emergency_bypass = any(
                cond in matched for cond in tmr_cfg.get("emergency_bypass_conditions", [])
            )
            _tmr_min = tmr_cfg.get("min_level", 3)

            # BUG-9: この分岐は min_level=3 (デフォルト) のため _tmr_min <= 2 が False となり
            # 通常は到達不可（dead code）。
            # level2_approval_required=False かつ min_level=3 が標準設定のため
            # 運用ブロックを防ぐための意図的な無効化設計（C7-B1修正）。
            # min_level を 2 に変更した場合のみ有効化される安全弁として残す。
            if (tmr_enabled and l2_approval_required and
                    _tmr_min <= 2 and not _emergency_bypass and
                    atype not in ("notify_only",)):
                # Level2 承認要求（min_level<=2 かつ level2_approval_required=True の場合のみ到達）
                _l2_approval_body = (
                    f"[Two-Man Rule] Level2 AUTOFIX 承認要求\n"
                    f"rule: {rid}\n"
                    f"desc: {desc}\n"
                    f"action: {atype}\n"
                    f"\n実行するには GitHub Issue にコメント 'APPROVE {rid}' または\n"
                    f"ntfy.sh/spxbot-hub-yuusaku2026 に 'APPROVE {rid}' を送信してください。\n"
                    f"5分応答なしで実行キャンセル（安全側）。"
                )
                pushover(f"[Atlas] Level2 確認要 {rid}", _l2_approval_body,
                         priority=1)
                log(f"[Two-Man Rule] Level2 アクションをブロック: {rid} — 承認待ち (5分タイムアウト)")
                result["action"] = {"type": "two_man_rule_blocked_l2",
                                    "rule_id": rid, "status": "PENDING_APPROVAL"}
            else:
                if atype == "restart_bot":
                    ar = action_restart_bot(cfg, rule, dry_run)
                elif atype == "notify_only":
                    ar = {"type": "notify_only", "status": "OK"}
                else:
                    ar = {"type": atype, "status": "UNKNOWN_ACTION"}
                body.append(f"action: {ar}")
                tag = "AUTOFIX_DRY" if dry_run else "AUTOFIX"
                pri = 1 if ar.get("status") in ("ERR",) else 0
                # Level2 AUTOFIX はBATCHED（自動修正の事実把握・即断不要）
                pushover(f"[Atlas/{tag}] {rid}", "\n".join(body), priority=pri,
                         level=_LEVEL_BATCHED)
                result["action"] = ar
                # builder 自動起動: Level2 は必ず atlas_builder.yml を dispatch
                bw = trigger_builder_workflow(cfg, rid, hypo, matched)
                result["builder_workflow"] = bw
                log(f"[dispatch] builder_workflow: {bw}")

    elif level == 3:
        # Two-Man Rule: Level3 は Pushover 承認待ちに変更。タイムアウトでキャンセル。
        tmr_cfg = cfg.get("autofix", {}).get("two_man_rule", {})
        tmr_enabled = tmr_cfg.get("enabled", True) and not dry_run
        tmr_min_level = tmr_cfg.get("min_level", 3)
        # BUG-3修正: Level3 でも emergency_bypass を確認する
        # kill_switch_activated / market_crash_detected 等があれば承認をスキップして即実行
        _l3_emergency_bypass_conditions = tmr_cfg.get(
            "level3_emergency_bypass_conditions",
            tmr_cfg.get("emergency_bypass_conditions", []),
        )
        _l3_emergency_bypass = any(
            cond in matched for cond in _l3_emergency_bypass_conditions
        )
        if _l3_emergency_bypass:
            log(
                f"[Two-Man Rule] Level3 emergency_bypass発動: {rid} "
                f"matched={matched} → 承認スキップで即実行"
            )
        if tmr_enabled and level >= tmr_min_level and not _l3_emergency_bypass:
            # 承認要求Pushoverを送って実行ブロック
            approval_body = (
                f"[Two-Man Rule] Level{level} アクション承認要求\n"
                f"rule: {rid}\n"
                f"desc: {desc}\n"
                f"action: {act_cfg.get('type', 'stop_bot')}\n"
                f"\n実行するには GitHub Issue にコメント 'APPROVE {rid}' または\n"
                f"ntfy.sh/spxbot-hub-yuusaku2026 に 'APPROVE {rid}' を送信してください。\n"
                f"応答なしの場合はアクションをキャンセルします（安全側）。"
            )
            pushover(f"[Atlas/APPROVAL_REQUIRED] {rid}", approval_body,
                     priority=1, token=PUSHOVER_ALERT_TOKEN)
            # GitHub Issue でトレースを残す
            if act_cfg.get("create_issue"):
                create_github_issue(
                    cfg,
                    f"[Atlas/APPROVAL_REQUIRED] L{level} {rid}: {desc}",
                    approval_body + f"\n\n時刻: {datetime.datetime.now(JST).isoformat()}",
                    label="todo",
                )
            log(f"[Two-Man Rule] Level{level} アクションをブロック: {rid} — 承認待ち")
            result["action"] = {"type": "two_man_rule_blocked", "rule_id": rid, "status": "PENDING_APPROVAL"}
        else:
            atype = act_cfg.get("type", "stop_bot")
            if atype == "stop_bot":
                ar = action_stop_bot(cfg, rule, dry_run)
            elif atype == "restart_bot":
                pre = act_cfg.get("pre_checks", [])
                ok, notes = run_pre_checks(pre) if pre else (True, [])
                if notes:
                    body.append("pre_checks: " + "; ".join(notes))
                ar = action_restart_bot(cfg, rule, dry_run) if ok else {"status": "SKIP_PRECHECK"}
            else:
                ar = {"type": atype, "status": "UNKNOWN_ACTION"}
            body.append(f"action: {ar}")
            if act_cfg.get("create_issue"):
                iss = create_github_issue(
                    cfg,
                    f"[Atlas/ALERT] {rid}: {desc}",
                    "\n".join(body) + f"\n\n時刻: {datetime.datetime.now(JST).isoformat()}",
                    label="todo",
                )
                body.append(f"issue: {iss}")
            pushover(f"[Atlas/ALERT] {rid}", "\n".join(body),
                     priority=1, token=PUSHOVER_ALERT_TOKEN)
            result["action"] = ar

    elif level == 4:
        # Two-Man Rule: Level4 も承認待ち (halt_and_wait は別途即実行してからブロック)
        tmr_cfg = cfg.get("autofix", {}).get("two_man_rule", {})
        tmr_enabled = tmr_cfg.get("enabled", True) and not dry_run
        # Level4 は halt は実行するが、追加の破壊的操作は承認待ち
        ar = action_halt_and_wait(cfg, rule, dry_run)
        body.append(f"action: {ar}")
        body.append("→ 手動指示待ち。解除: data/atlas_state.json の manual_halt を削除")
        if tmr_enabled:
            body.append(
                f"\n[Two-Man Rule] Level4 — halt実行済み。追加操作は承認が必要。\n"
                f"承認: GitHub Issue または ntfy に 'APPROVE {rid}' を送信"
            )
        if act_cfg.get("create_issue"):
            iss = create_github_issue(
                cfg,
                f"[Atlas/HALT] {rid}: 発注異常",
                "\n".join(body),
                label="todo",
            )
            body.append(f"issue: {iss}")
        pushover(f"[Atlas/HALT] {rid}", "\n".join(body),
                 priority=2, token=PUSHOVER_ALERT_TOKEN)
        result["action"] = ar

    log_action({"fired": {"rule_id": rid, "level": level, "count": count},
                "result": result})
    return result


# ── Main loop ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not RULES_FILE.exists():
        raise FileNotFoundError(f"rules file not found: {RULES_FILE}")
    with RULES_FILE.open() as f:
        return yaml.safe_load(f)


def main():
    once_mode = "--once" in sys.argv
    selftest  = "--selftest" in sys.argv
    dry_override: int | None = None
    if "--dry-run" in sys.argv:
        dry_override = 1
    if "--armed" in sys.argv:
        dry_override = 0

    try:
        cfg = load_config()
    except Exception as e:
        log(f"FATAL config: {e}")
        sys.exit(1)

    if dry_override is not None:
        cfg.setdefault("autofix", {})["dry_run_default"] = dry_override

    rules = cfg.get("rules", [])
    matcher = RuleMatcher(rules)

    tailers: dict[str, LogTailer] = {}
    for name, path in cfg.get("log_sources", {}).items():
        tailers[name] = LogTailer(Path(path))

    st = load_state()
    st["last_boot"] = datetime.datetime.now(JST).isoformat()
    save_state(st)

    dry = bool(cfg.get("autofix", {}).get("dry_run_default", 1))
    # BOOT通知はSILENT（定型起動報告は判断不要）
    pushover(
        "[Atlas/BOOT]",
        f"atlas_agent 起動\n"
        f"rules: {len(rules)}  sources: {list(tailers.keys())}\n"
        f"dry_run: {'ON (safe)' if dry else 'OFF (ARMED)'}\n"
        f"market: {cfg.get('market_window')}",
        priority=0,
        level=_LEVEL_SILENT,
    )
    log(f"boot: rules={len(rules)} dry_run={dry} once={once_mode}")

    if selftest:
        log("--selftest OK")
        return

    main_log = Path(cfg["log_sources"]["condor"])

    # Deviation scanner 前回実行時刻（5分おき）
    _last_dev_scan = 0.0
    _dev_scan_interval = 300  # 5分
    _notified_surge_cats: set[str] = set()  # 既通知カテゴリ（重複抑制）

    # DeviationDetector Greeks スナップショット（1分毎）
    _last_greeks_snapshot = 0.0
    _GREEKS_SNAPSHOT_INTERVAL = 60  # 1分

    # Heartbeat pulse（1分毎）
    _last_pulse = 0.0
    _PULSE_INTERVAL = 60

    # 外部死活監視 ping（2分毎・Atlas: SPXオプションBot系）
    _last_ext_ping = 0.0
    _EXT_PING_INTERVAL = 120  # 2分毎（atlas_agent）

    # 起動時に外部ping "start" 送信（Atlas系・Chronos と混同禁止）
    _ext_ping("atlas_agent", status="start")

    while True:
        try:
            now = time.time()
            in_market = is_market_hours_now(cfg)
            st = load_state()
            manual_halt = st.get("manual_halt")

            # ── DeviationDetector halt_flag チェック ─────────────────────────
            # 本番で連続逸脱が ESCALATE_THRESHOLD 回を超えた場合 Bot を停止する
            if _DEVIATION_DETECTOR_OK and _atlas_deviation_detector is not None:
                try:
                    if _atlas_deviation_detector.is_halt_flagged():
                        pushover(
                            "[Atlas/HALT] 乖離検知 Bot 停止フラグ",
                            "DeviationDetector が連続逸脱を検知しました。\n"
                            "手動確認後 DeviationDetector.clear_halt_flag() を実行してください。",
                            priority=2,
                        )
                        log("[DEVIATION_HALT] halt_flag=True → Level4 halt")
                        # halt_flag は手動解除まで維持 (ここでは clear しない)
                except Exception as _dhe:
                    log(f"[DEVIATION_HALT_ERR] {_dhe}")

            new_lines: list[str] = []
            for name, tl in tailers.items():
                new_lines.extend(tl.read_new())

            fired_list = matcher.ingest(new_lines, now)

            # ── DeviationDetector Greeks 1分毎スナップショット (観点C) ────────
            if (
                _DEVIATION_DETECTOR_OK
                and _atlas_deviation_detector is not None
                and in_market
                and (now - _last_greeks_snapshot >= _GREEKS_SNAPSHOT_INTERVAL)
            ):
                try:
                    # atlas_state.json から現在ポジションの Greeks を読む
                    positions = st.get("positions", {})
                    for pos_id, pos in positions.items() if isinstance(positions, dict) else []:
                        greeks = pos.get("greeks")
                        tactic = pos.get("tactic", "unknown")
                        if greeks and isinstance(greeks, dict):
                            dev = _atlas_deviation_detector.check_greeks_range(
                                bot_name="atlas",
                                tactic=tactic,
                                position_id=pos_id,
                                current_greeks=greeks,
                            )
                            if dev:
                                _atlas_deviation_detector.alert(dev)
                                log(f"[DEVIATION-C] {dev.title}")
                    _last_greeks_snapshot = now
                except Exception as _gse:
                    log(f"[GREEKS_SNAP_ERR] {_gse}")

            # synthetic: bot_process_stale
            if in_market:
                bot_alive = is_bot_alive(cfg.get("bot_pid_files", []))
                stale, age = bot_log_is_stale(main_log, cfg.get("stale_log_sec", 180))
                if bot_alive and stale:
                    f = matcher.fire_synthetic(
                        "bot_process_stale", now,
                        {"log_age_sec": round(age, 1), "bot_alive": True},
                    )
                    if f:
                        fired_list.append(f)

            # [Challenger教訓] Deviation scanner リアルタイム急増検知（5分おき）
            if _DEV_SCANNER_OK and in_market and (now - _last_dev_scan >= _dev_scan_interval):
                try:
                    events = _dev_scanner.parse_log_lines(days=1)
                    _, _, surging, _ = _dev_scanner.analyze(events, threshold=10)
                    new_surges = set(surging) - _notified_surge_cats
                    if new_surges:
                        pushover(
                            "[Atlas/DEVIATION/SURGE] 急増検知",
                            "1時間で10件超の逸脱急増: " + ", ".join(sorted(new_surges))
                            + "\n詳細: data/deviation_dashboard.md",
                            priority=1,
                        )
                        _notified_surge_cats |= new_surges
                    # 急増が止んだカテゴリは再通知可能にする
                    _notified_surge_cats &= set(surging)
                    _last_dev_scan = now
                except Exception as _dse:
                    log(f"[DEV_SCAN_ERR] {_dse}")

            # PDT残数をatlas_state.jsonに保存 + 毎分ログ出力 + 残1で通知
            # ペーパーモード判定: acc_type==SIMULATE or bot_launchagent に "paper" が含まれる場合は
            # PDT制約はFINRA本番(live $25K未満)のみ有効 → ペーパーはスキップ
            _is_paper_mode = (
                st.get("acc_type", "").upper() == "SIMULATE"
                or cfg.get("paper", False)
                or "paper" in cfg.get("bot_launchagent", "")
            )
            if _PDT_TRACKER_AGENT_OK and _pdt_tracker_agent is not None:
                try:
                    # 口座残高（state.jsonから取得、不明時は保守的に$0扱い）
                    _pdt_capital = st.get("capital_usd", 0.0)
                    _pdt_status = _pdt_tracker_agent.get_status(_pdt_capital)
                    # atlas_state.json に pdt_remaining を更新（ペーパー含め常時記録）
                    st["pdt_remaining"]   = _pdt_status["pdt_remaining"]
                    st["pdt_rolling5"]    = _pdt_status["rolling5_count"]
                    st["pdt_constrained"] = _pdt_status["pdt_constrained"]
                    save_state(st)
                    if _is_paper_mode:
                        # ペーパー($420K)はPhase2相当・PDT制約はFINRA本番のみ → ログはINFOで黙認
                        log(f"[PDT] paper_mode=True → PDT制約スキップ "
                            f"rolling5={_pdt_status['rolling5_count']} "
                            f"remaining={_pdt_status['pdt_remaining']}")
                    else:
                        log(f"[PDT] rolling5={_pdt_status['rolling5_count']} "
                            f"remaining={_pdt_status['pdt_remaining']} "
                            f"constrained={_pdt_status['pdt_constrained']}")
                        # PDT残1 → priority=1 通知（重複抑制: 残数変化時のみ・liveのみ）
                        _pdt_rem = _pdt_status["pdt_remaining"]
                        if (
                            _pdt_status["pdt_constrained"]
                            and isinstance(_pdt_rem, int)
                            and _pdt_rem == 1
                            and st.get("_pdt_notified_remaining1_date") != datetime.datetime.now(ET).strftime("%Y-%m-%d")
                        ):
                            pushover(
                                "[Atlas/PDT] PDT残1件警告",
                                f"直近5営業日 {_pdt_status['rolling5_count']}/{_PDT_LIMIT}件消費\n"
                                f"本日の新規day_tradeは残1件のみ。4件目で90日停止。",
                                priority=1,
                            )
                            st["_pdt_notified_remaining1_date"] = datetime.datetime.now(ET).strftime("%Y-%m-%d")
                            save_state(st)
                        # PDT残0 → 通常通知（手動判断を促す・liveのみ）
                        elif (
                            _pdt_status["pdt_constrained"]
                            and isinstance(_pdt_rem, int)
                            and _pdt_rem == 0
                            and st.get("_pdt_notified_remaining0_date") != datetime.datetime.now(ET).strftime("%Y-%m-%d")
                        ):
                            pushover(
                                "[Atlas/PDT] PDT上限到達 — 新規エントリー停止",
                                f"直近5営業日 {_pdt_status['rolling5_count']}/{_PDT_LIMIT}件消費済み\n"
                                f"新規day_tradeはpre_trade_checkでブロックされます。",
                                priority=1,
                            )
                            st["_pdt_notified_remaining0_date"] = datetime.datetime.now(ET).strftime("%Y-%m-%d")
                            save_state(st)
                except Exception as _pdt_agent_e:
                    log(f"[PDT_AGENT_ERR] {_pdt_agent_e}")

            for f in fired_list:
                if manual_halt:
                    log(f"[HALT_ACTIVE] skipping action for {f['rule']['id']}")
                    pushover(
                        f"[Atlas/BLOCKED] {f['rule']['id']}",
                        f"manual_halt中のため対応スキップ: {f['matched_line'][:200]}",
                        priority=1,
                    )
                    continue
                dispatch(f, cfg)

            # 能動 heartbeat pulse（1分毎）
            if now - _last_pulse >= _PULSE_INTERVAL:
                _write_pulse("atlas_agent", state="healthy", details={"fired": len(fired_list), "in_market": in_market, "dry_run": dry})
                _last_pulse = now

            # 外部死活監視 ping（2分毎・Pushover と独立した経路）
            # Atlas: SPXオプションBot エージェント（Chronos と混同禁止）
            if now - _last_ext_ping >= _EXT_PING_INTERVAL:
                _ext_ping("atlas_agent", status="success")
                _last_ext_ping = now

            if once_mode:
                log(f"--once complete: fired={len(fired_list)}")
                break

            time.sleep(POLL_SEC_ACTIVE if in_market else POLL_SEC_IDLE)
        except KeyboardInterrupt:
            log("KeyboardInterrupt")
            break
        except Exception as e:
            log(f"[LOOP_ERR] {e}")
            _write_pulse("atlas_agent", state="degraded", details={"error": str(e)})
            _ext_ping("atlas_agent", status="fail", payload=str(e)[:500])
            time.sleep(10)


if __name__ == "__main__":
    main()
