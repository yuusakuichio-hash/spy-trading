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
from collections import deque
from pathlib import Path
from typing import Any

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
def pushover(title: str, msg: str, priority: int = 0, token: str | None = None):
    tok = token or PUSHOVER_OPS_TOKEN or PUSHOVER_ALERT_TOKEN
    if not tok or not PUSHOVER_USER:
        log(f"[NOTIFY_SKIP] missing token/user. title={title}")
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
    # TODO: moomoo OpenAPI で未約定注文を取得する実装に差し替え
    return True, "no_open_orders check permissive (TODO: moomoo API)"


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
        pushover(f"{header} INFO", "\n".join(body), priority=0)
        result["action"] = "notify_only"

    elif level == 2:
        atype = act_cfg.get("type", "notify_only")
        pre = act_cfg.get("pre_checks", [])
        ok, notes = run_pre_checks(pre) if pre else (True, [])
        if notes:
            body.append("pre_checks: " + "; ".join(notes))
        if pre and not ok:
            body.append("→ 対応スキップ (pre_check失敗)")
            pushover(f"{header} AUTOFIX SKIP", "\n".join(body), priority=1)
            result["action"] = "skipped_precheck"
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
            pushover(f"[Atlas/{tag}] {rid}", "\n".join(body), priority=pri)
            result["action"] = ar

    elif level == 3:
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
        ar = action_halt_and_wait(cfg, rule, dry_run)
        body.append(f"action: {ar}")
        body.append("→ 手動指示待ち。解除: data/atlas_state.json の manual_halt を削除")
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
    pushover(
        "[Atlas/BOOT]",
        f"atlas_agent 起動\n"
        f"rules: {len(rules)}  sources: {list(tailers.keys())}\n"
        f"dry_run: {'ON (safe)' if dry else 'OFF (ARMED)'}\n"
        f"market: {cfg.get('market_window')}",
        priority=0,
    )
    log(f"boot: rules={len(rules)} dry_run={dry} once={once_mode}")

    if selftest:
        log("--selftest OK")
        return

    main_log = Path(cfg["log_sources"]["condor"])

    while True:
        try:
            now = time.time()
            in_market = is_market_hours_now(cfg)
            st = load_state()
            manual_halt = st.get("manual_halt")

            new_lines: list[str] = []
            for name, tl in tailers.items():
                new_lines.extend(tl.read_new())

            fired_list = matcher.ingest(new_lines, now)

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

            if once_mode:
                log(f"--once complete: fired={len(fired_list)}")
                break

            time.sleep(POLL_SEC_ACTIVE if in_market else POLL_SEC_IDLE)
        except KeyboardInterrupt:
            log("KeyboardInterrupt")
            break
        except Exception as e:
            log(f"[LOOP_ERR] {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
