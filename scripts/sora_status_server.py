#!/usr/bin/env python3
"""Sora Lab 死活監視 HTTP ダッシュボード（スマホ Safari 向け）。

用途:
  ゆうさくさんがスマホ Safari で http://<MacのLAN IP>:8765/ を開くと
  ソラ本人の生存 + Builder 進捗 + 直近レポート + 進行中タスクを確認できる。

起動:
  python3 scripts/sora_status_server.py  （foreground）
  launchd で常時起動（com.soralab.status-server.plist）

セッション跨ぎ:
  Claude Code 本体とは別プロセス。別セッション立ち上げても動き続ける。

規律:
  プッシュ通知不要（2026-04-24 ゆうさくさん指示）・オンデマンド pull 型。
"""
from __future__ import annotations

import html
import json
import os
import socket
import subprocess
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = Path.home() / ".claude" / "projects" / "-Users-yuusakuichio-trading"
MONITOR_TARGET_FILE = PROJECT_ROOT / "data" / "monitor_target.txt"
MONITOR_LOG = PROJECT_ROOT / "data" / "logs" / "builder_monitor_5min.log"

PORT = int(os.environ.get("SORA_STATUS_PORT", "8765"))


def get_latest_session_jsonl() -> Path | None:
    if not SESSION_DIR.exists():
        return None
    files = sorted(SESSION_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def extract_last_activity(jsonl_path: Path, limit: int = 200) -> tuple[str, str]:
    """最新の書込みから「意味のある role 状態」を判定して返す。
    user role かつ tool_result のみ → assistant_working (ソラが tool 実行中)
    user role かつ text 有り → user_text (本物のゆうさくさん発言・応答待ち)
    assistant role かつ tool_use 有り → assistant_tool_use (ソラが tool 呼び出し中)
    assistant role かつ text のみ → assistant_text (ソラの応答書込)
    """
    last_ts = ""
    last_state = "unknown"
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("timestamp", "")
            if ts <= last_ts:
                continue
            raw_type = rec.get("type", "")
            msg = rec.get("message", {})
            content = msg.get("content", None)
            has_tool_result = False
            has_text = False
            has_tool_use = False
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        it = item.get("type", "")
                        if it == "tool_result":
                            has_tool_result = True
                        elif it == "text":
                            if (item.get("text") or "").strip():
                                has_text = True
                        elif it == "tool_use":
                            has_tool_use = True
            elif isinstance(content, str):
                if content.strip():
                    has_text = True
            if raw_type == "user":
                if has_tool_result and not has_text:
                    state = "assistant_working"  # tool 実行結果受信（ソラ作業中）
                elif has_text:
                    state = "user_text"  # 本物のユーザー発言
                else:
                    state = "user_other"
            elif raw_type == "assistant":
                if has_tool_use:
                    state = "assistant_tool_use"  # ソラ tool 呼出中
                elif has_text:
                    state = "assistant_text"  # ソラ応答書込
                else:
                    state = "assistant_other"
            elif raw_type == "system":
                state = "system"
            else:
                state = raw_type or "unknown"
            last_ts = ts
            last_state = state
    except Exception:
        pass
    return last_ts, last_state


def extract_last_text(jsonl_path: Path, limit: int = 100, max_chars: int = 300) -> str:
    last_text = ""
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        t = item.get("text", "").strip()
                        if t:
                            last_text = t[:max_chars]
    except Exception:
        pass
    return last_text


def status_classify(elapsed_sec: int, last_role: str = "") -> tuple[str, str]:
    """閾値ゆとりを持たせた判定（Builder は tool 実行中 3min 書込なしも正常）。
    last_role='user' = ゆうさくさんの発言が最新 → 応答準備中表示。
    """
    if last_role == "user":
        return "RESPONDING", "#16a34a"
    if elapsed_sec < 180:
        return "ALIVE", "#16a34a"
    if elapsed_sec < 600:
        return "IDLE", "#22c55e"
    if elapsed_sec < 1800:
        return "STALE", "#ca8a04"
    return "DEAD?", "#dc2626"


def check_claude_processes() -> tuple[int, int, int]:
    """(総数, 現役, 待機) を返す。
    現役 = 直近 30 分以内に CPU 使った (state=R/S で etime 経過中かつ最近アクティブ)。
    待機 = 長時間 idle の claude プロセス。
    判定は簡易 = etime < 7200 (2h) を現役候補・それ以上を待機扱い。
    ただし精度問題あるため:「現役 = CPU time > 1min」を現役判定に使う。
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,etime,time,command"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return 0, 0, 0
        lines = [l for l in result.stdout.splitlines() if "claude" in l and "grep" not in l]
        total = len(lines)
        active = 0
        for line in lines:
            parts = line.strip().split(None, 3)
            if len(parts) < 3:
                continue
            cpu_time = parts[2]
            # ps time: "MM:SS.dd" or "HH:MM:SS" or "D-HH:MM:SS"
            try:
                cleaned = cpu_time.split(".")[0]  # 小数部（centi-sec）除去
                if "-" in cleaned:
                    days_str, rest = cleaned.split("-", 1)
                    days = int(days_str)
                else:
                    days = 0
                    rest = cleaned
                segs = rest.split(":")
                if len(segs) == 2:
                    secs = int(segs[0]) * 60 + int(segs[1])
                elif len(segs) == 3:
                    secs = int(segs[0]) * 3600 + int(segs[1]) * 60 + int(segs[2])
                else:
                    secs = 0
                secs += days * 86400
                if secs >= 60:
                    active += 1
            except (ValueError, IndexError):
                pass
        waiting = max(0, total - active)
        return total, active, waiting
    except Exception:
        return 0, 0, 0


def get_monitor_target() -> str:
    if MONITOR_TARGET_FILE.exists():
        return MONITOR_TARGET_FILE.read_text(encoding="utf-8").strip()
    return ""


def find_agent_jsonl(agent_id: str) -> Path | None:
    if not agent_id:
        return None
    for pattern in [f"agent-{agent_id}.jsonl", f"*{agent_id}*.jsonl"]:
        for p in SESSION_DIR.rglob(pattern):
            return p
    return None


def tail_log(n: int = 12) -> str:
    if not MONITOR_LOG.exists():
        return "(ログなし)"
    try:
        with MONITOR_LOG.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception as exc:
        return f"(読込失敗: {exc})"


def find_all_active_subagents(now_ts: float, max_age_sec: int = 1800) -> list[dict]:
    """全 subagent jsonl を検出し直近 mtime 以内のものを列挙。
    種別（builder/navigator/redteam 等）と task description を meta.json から抽出。
    """
    agents = []
    if not SESSION_DIR.exists():
        return agents
    for p in SESSION_DIR.rglob("agent-*.jsonl"):
        try:
            mtime = p.stat().st_mtime
            elapsed = int(now_ts - mtime)
            if elapsed > max_age_sec:
                continue
            with p.open("r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            if line_count == 0:
                continue
            agent_id = p.stem.replace("agent-", "")
            meta_path = p.parent / f"agent-{agent_id}.meta.json"
            kind = "?"
            desc = ""
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    kind = meta.get("agentType") or meta.get("subagent_type") or meta.get("slug") or "?"
                    desc = meta.get("description", "")[:40]
                except Exception:
                    pass
            agents.append({
                "id": agent_id,
                "kind": kind,
                "desc": desc,
                "elapsed": elapsed,
                "lines": line_count,
                "mtime": mtime,
            })
        except Exception:
            continue
    agents.sort(key=lambda a: a["elapsed"])
    return agents


def _render_tunnel_url() -> str:
    """cloudflared quick tunnel URL をヘッダ下に表示。"""
    url = _get_public_tunnel_url()
    if not url:
        return ""
    return f"""
<div class="tunnel-url">
  🌐 外出先 URL: <a href="{url}">{url}</a>
</div>
"""


def _render_supervisor_status() -> str:
    """Supervisor daemon + auto_fork 状況をヘッダ下に表示。"""
    state_file = PROJECT_ROOT / "data" / "state_v3" / "supervisor_last_kick.json"
    log_file = PROJECT_ROOT / "data" / "logs" / "supervisor.log"
    last_kick = "n/a"
    last_probe_line = ""
    auto_fork_enabled = False
    try:
        if state_file.exists():
            st = json.loads(state_file.read_text(encoding="utf-8"))
            last_kick = st.get("last_kick_ts", "n/a")[:19].replace("T", " ")
    except Exception:
        pass
    try:
        if log_file.exists():
            lines = [l for l in log_file.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            if lines:
                last_probe_line = lines[-1][-120:]
    except Exception:
        pass
    # auto_fork today count
    today = datetime.now().strftime("%Y%m%d")
    fork_today = 0
    try:
        for p in (PROJECT_ROOT / "data" / "logs").glob(f"auto_fork_{today}_*.log"):
            fork_today += 1
    except Exception:
        pass
    color = "#22c55e" if "active" in last_probe_line else ("#f59e0b" if "IDLE" in last_probe_line else "#6b7280")
    return f"""
<div class="sup-status" style="border-left:3px solid {color}">
  👁 Supervisor: <span style="color:{color}">{html.escape(last_probe_line or "(no probe yet)")}</span>
  <div class="sup-sub">last kick: {html.escape(last_kick)} / auto_fork today: {fork_today}/6</div>
</div>
"""


def _get_public_tunnel_url():  # -> Optional[str]（typing import 省略のため annotation 削除）
    """cloudflared quick tunnel のログから公開 URL を抽出。"""
    log_file = PROJECT_ROOT / "data" / "logs" / "cloudflared_tunnel.log"
    if not log_file.exists():
        return None
    try:
        content = log_file.read_text(encoding="utf-8")
        import re
        matches = re.findall(r"https://[a-z-]+\.trycloudflare\.com", content)
        if matches:
            return matches[-1]
    except Exception:
        pass
    return None


def _render_live_health() -> str:
    """実測ヘルス（sprint_state.json 非依存で OS から直接検出）。

    2026-04-24 22:58 JST の KillSwitch 誤発動 + allowlist lock 完了が
    dashboard 上で 20 分以上反映されなかった事案の根治として追加。
    """
    sections = []

    # 1. KillSwitch flag 検出
    ks_flag = PROJECT_ROOT / "data" / "state_v3" / "kill_switch.flag"
    if ks_flag.exists():
        try:
            ks_data = json.loads(ks_flag.read_text(encoding="utf-8"))
            reason = ks_data.get("reason", "unknown")[:100]
            activated = ks_data.get("activated_at", "")[:19]
            pid = ks_data.get("pid", "")
            sections.append(
                f'<div class="health-row emergency">🚨 KillSwitch ARMED: '
                f'{html.escape(reason)}<br>'
                f'<span class="health-sub">activated: {html.escape(activated)} UTC / pid: {html.escape(str(pid))}</span></div>'
            )
        except Exception:
            sections.append('<div class="health-row emergency">🚨 KillSwitch ARMED (data unparseable)</div>')
    else:
        sections.append('<div class="health-row ok">✓ KillSwitch released</div>')

    # 2. allowlist schg lock status
    schg_files = ["spy_bot.py", "chronos_bot.py", "atlas_agent.py", "common/kill_switch.py"]
    schg_count = 0
    for f in schg_files:
        p = PROJECT_ROOT / f
        if p.exists():
            try:
                result = subprocess.run(
                    ["stat", "-f", "%Sf", str(p)],
                    capture_output=True, text=True, timeout=2,
                )
                if "schg" in result.stdout:
                    schg_count += 1
            except Exception:
                pass
    total = len(schg_files)
    cls = "ok" if schg_count == total else ("warn" if schg_count > 0 else "emergency")
    icon = "🔒" if schg_count == total else ("⚠" if schg_count > 0 else "🔓")
    sections.append(
        f'<div class="health-row {cls}">{icon} Allowlist (C-018): {schg_count}/{total} files schg-locked</div>'
    )

    # 3. atlas-paper daemon: PID alive を第一判定・heartbeat 警告は直近 60s のみ
    pid_paper = None
    etime = "?"
    try:
        result = subprocess.run(
            ["pgrep", "-f", "atlas_v3.main.*paper"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            pid_paper = result.stdout.strip().splitlines()[0]
            etime_result = subprocess.run(
                ["ps", "-p", pid_paper, "-o", "etime="],
                capture_output=True, text=True, timeout=2,
            )
            etime = etime_result.stdout.strip() if etime_result.returncode == 0 else "?"
    except Exception:
        pass

    if pid_paper is None:
        sections.append('<div class="health-row emergency">💀 atlas-paper daemon NOT running</div>')
    else:
        # 直近 60 秒以内の stderr に "No heartbeat for" があれば stale 警告
        # （古い log の残留や、別 PID からの警告は拾わない）
        stderr_log = PROJECT_ROOT / "data" / "state_v3" / "atlas-paper-stderr.log"
        recent_stale = False
        stale_age_s = 0
        if stderr_log.exists():
            try:
                mtime = stderr_log.stat().st_mtime
                now_ts = datetime.now().timestamp()
                if (now_ts - mtime) < 60:
                    size = stderr_log.stat().st_size
                    with open(stderr_log, "rb") as fh:
                        if size > 2000:
                            fh.seek(-2000, 2)
                        tail = fh.read().decode("utf-8", errors="ignore")
                    import re as _re
                    matches = _re.findall(r"No heartbeat for (\d+)s", tail)
                    if matches:
                        stale_age_s = int(matches[-1])
                        if stale_age_s > 300:
                            recent_stale = True
            except Exception:
                pass
        if recent_stale:
            sections.append(
                f'<div class="health-row emergency">💔 atlas-paper PID {html.escape(pid_paper)} heartbeat stale: {stale_age_s}s (threshold 300s)</div>'
            )
        else:
            sections.append(
                f'<div class="health-row ok">💚 atlas-paper alive: PID {html.escape(pid_paper)} (elapsed {html.escape(etime)})</div>'
            )

    # 4. spy-bot-paper daemon 状態 (2026-04-24 23:52 追加・場中 paper 戦略層)
    # 2026-04-26: atlas_v3 native ランチャー (com.soralab.atlas-trader) が paper 発注層を担当する
    # 移行を進めた。判定は実態ベース（OR）:
    #   - ATLAS_TRADER_ACTIVE=1 が明示設定されている、または
    #   - launchctl list で com.soralab.atlas-trader が稼働 PID 持ち
    # のいずれかなら旧 spy-bot-paper 検査を skip し warn ノイズを抑制する。
    # （script 契約上 ATLAS_TRADER_ACTIVE のデフォルトは 0 で固定・安全側）
    atlas_trader_active = os.environ.get("ATLAS_TRADER_ACTIVE", "0") == "1"
    if not atlas_trader_active:
        try:
            _at = subprocess.run(
                ["launchctl", "list", "com.soralab.atlas-trader"],
                capture_output=True, text=True, timeout=2,
            )
            if _at.returncode == 0 and '"PID"' in _at.stdout and '"PID" = 0' not in _at.stdout:
                atlas_trader_active = True
        except Exception:
            pass

    if atlas_trader_active:
        # 新層 (atlas-trader) が稼働している前提・spy-bot-paper 監視 skip
        pass
    else:
        pid_spy = None
        spy_etime = "?"
        try:
            result = subprocess.run(
                ["pgrep", "-f", "spy_bot.py.*--paper"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid_spy = result.stdout.strip().splitlines()[0]
                etime_result = subprocess.run(
                    ["ps", "-p", pid_spy, "-o", "etime="],
                    capture_output=True, text=True, timeout=2,
                )
                spy_etime = etime_result.stdout.strip() if etime_result.returncode == 0 else "?"
        except Exception:
            # spy-bot-paper pgrep 失敗は非致命的・status server で log 不要
            pass

        if pid_spy is not None:
            sections.append(
                f'<div class="health-row ok">🤖 spy-bot-paper alive: PID {html.escape(pid_spy)} (elapsed {html.escape(spy_etime)})</div>'
            )
        else:
            try:
                lc_result = subprocess.run(
                    ["launchctl", "list", "com.soralab.spy-bot-paper"],
                    capture_output=True, text=True, timeout=2,
                )
                if lc_result.returncode == 0:
                    sections.append(
                        '<div class="health-row warn">⚠ spy-bot-paper daemon 未稼働 (launchd 登録済・起動失敗の可能性)</div>'
                    )
                else:
                    sections.append(
                        '<div class="health-row warn">💤 spy-bot-paper daemon 未登録 (paper 発注層停止中)</div>'
                    )
            except Exception:
                # spy-bot-paper launchctl 失敗は status server には致命的でないので warn 表示のみ
                sections.append('<div class="health-row warn">? spy-bot-paper status 判定不能</div>')

    # 5. moomoo OpenD preemptive relogin heartbeat (案 F・12h 周期)
    relogin_hb_file = PROJECT_ROOT / "data" / "state_v3" / "opend_relogin_heartbeat.jsonl"
    if relogin_hb_file.exists():
        try:
            lines = relogin_hb_file.read_text(encoding="utf-8").splitlines()
            last_record = None
            for line in reversed(lines[-5:]):
                try:
                    rec = json.loads(line)
                    if last_record is None:
                        last_record = rec
                        break
                except Exception:
                    continue
            if last_record is not None:
                ts_str = last_record.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age_secs = (datetime.now(timezone.utc) - ts).total_seconds()
                except Exception:
                    age_secs = -1
                status = last_record.get("status", "unknown")
                if status == "success" and 0 <= age_secs < 25 * 3600:
                    age_h = int(age_secs / 3600)
                    age_m = int((age_secs % 3600) / 60)
                    sections.append(
                        f'<div class="health-row ok">🔄 OpenD relogin OK: 最終 success {age_h}h{age_m}m 前 (次発火 08:00 / 20:00 JST)</div>'
                    )
                elif status != "success":
                    err = last_record.get("details", {}).get("error", "unknown")[:80]
                    sections.append(
                        f'<div class="health-row warn">⚠ OpenD relogin 最新 {html.escape(status)}: {html.escape(err)}</div>'
                    )
                else:
                    sections.append(
                        f'<div class="health-row warn">⚠ OpenD relogin heartbeat stale: {int(age_secs/3600)}h old</div>'
                    )
        except Exception:
            sections.append('<div class="health-row warn">? OpenD relogin heartbeat 読取失敗</div>')
    else:
        sections.append('<div class="health-row warn">🔄 OpenD relogin: 初回実行前 (launchd 登録済・次発火待ち)</div>')

    return f"""
<div class="sec">
<div class="sec-title">🩺 Live Health (実測)</div>
{''.join(sections)}
</div>
"""


def _render_phase_section() -> str:
    """Sprint phase 進捗表示（data/sprint_state.json 読み込み）。"""
    state_file = PROJECT_ROOT / "data" / "sprint_state.json"
    if not state_file.exists():
        return ""
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return ""

    current_id = state.get("current_phase_id", "")
    current_label = state.get("current_phase_label", "")
    overall_note = state.get("overall_note", "")
    phases = state.get("phases", [])

    status_style = {
        "completed": ("✅", "#22c55e", "completed"),
        "ready (ゆうさくさん手動実行待ち)": ("⏸", "#f59e0b", "ready-user-action"),
        "code ready (futu install + login 待ち)": ("⏸", "#f59e0b", "ready-user-action"),
        "pending": ("⏳", "#6b7280", "pending"),
        "pending (Day 1/2 完了後)": ("⏳", "#6b7280", "pending"),
    }

    rows = []
    for p in phases:
        is_current = (p.get("id") == current_id)
        stat = p.get("status", "pending")
        icon, color, _ = status_style.get(stat, ("•", "#9ca3af", ""))
        border = f"border:2px solid #f59e0b" if is_current else "border-left:3px solid " + color
        marker = "▶ " if is_current else ""
        blocker = p.get("blocker", "")
        blocker_html = f"<div class='phase-blocker'>⚠ {html.escape(blocker)}</div>" if blocker else ""
        artifact = p.get("artifact", "")
        artifact_html = f"<div class='phase-artifact'>{html.escape(artifact)}</div>" if artifact else ""
        rows.append(f"""
        <div class="phase-row" style="{border}">
            <div class="phase-line">
                <span class="phase-icon" style="color:{color}">{icon}</span>
                <b>{marker}{html.escape(p.get("name", ""))}</b>
                <span class="phase-status" style="color:{color}">{html.escape(stat)}</span>
            </div>
            <div class="phase-purpose">{html.escape(p.get("purpose", ""))}</div>
            {artifact_html}
            {blocker_html}
        </div>
        """)

    return f"""
<div class="sec">
<div class="sec-title">Sprint Phase Progress</div>
<div class="phase-header">📍 Now: <b>{html.escape(current_label)}</b></div>
<div class="phase-note">{html.escape(overall_note)}</div>
{''.join(rows)}
</div>
"""


def build_html() -> str:
    now_utc = datetime.now(timezone.utc)
    now_jst = (now_utc + timedelta(hours=9)).strftime("%H:%M:%S")

    proc_total, proc_active, proc_waiting = check_claude_processes()
    latest = get_latest_session_jsonl()

    # 秘書ソラ本人判定（Sora Lab 全体ではなく本セッションのみ）
    if latest is None:
        sora_banner = '<div class="banner dead">✗ セッション jsonl なし</div>'
        sora_line = ""
    else:
        mtime = latest.stat().st_mtime
        elapsed = int(now_utc.timestamp() - mtime)
        last_ts, last_state = extract_last_activity(latest)
        # user_text = 本物の応答待ち / assistant_working = tool 実行中（作業中）
        if last_state == "user_text":
            sora_banner = f"""<div class="banner waiting">
                ⚠️ Sora: Preparing Response<br>
                <span class="banner-sub">user message received / {elapsed}s elapsed</span>
            </div>"""
        elif last_state in ("assistant_working", "assistant_tool_use"):
            sora_banner = f"""<div class="banner alive">
                ✓ Sora: Working (tool use)<br>
                <span class="banner-sub">last write {elapsed}s ago</span>
            </div>"""
        elif last_state == "assistant_text" and elapsed < 60:
            sora_banner = f"""<div class="banner alive">
                ✓ Sora: Writing Response<br>
                <span class="banner-sub">last write {elapsed}s ago</span>
            </div>"""
        elif elapsed < 60:
            sora_banner = f"""<div class="banner alive">
                ✓ Sora: Active<br>
                <span class="banner-sub">last write {elapsed}s ago</span>
            </div>"""
        elif elapsed < 600:
            sora_banner = f"""<div class="banner standby">
                ⏸ Sora: Standby<br>
                <span class="banner-sub">responded / awaiting next instruction / last {elapsed}s ago</span>
            </div>"""
        elif elapsed < 1800:
            sora_banner = f"""<div class="banner idle">
                ⏳ Sora: Long Idle<br>
                <span class="banner-sub">silent for {elapsed}s / please check</span>
            </div>"""
        else:
            sora_banner = f"""<div class="banner dead">
                ✗ Sora: No Response<br>
                <span class="banner-sub">no update for {elapsed}s</span>
            </div>"""
        sora_line = f"<div class='line'>session <code>{latest.stem[:8]}</code> / main proc {proc_total} (active {proc_active})</div>"

    # 全 subagent 検出
    # 2026-04-25: 30 分 → 90 分に拡張 (1 時間以内のバースト的並列起動を全て可視化)
    agents = find_all_active_subagents(now_utc.timestamp(), max_age_sec=5400)
    if agents:
        kind_emoji = {
            "builder": "🔨",
            "navigator": "🧭",
            "redteam": "🎯",
            "strategist": "♟️",
            "analyst": "📊",
            "sns": "📣",
            "ops": "🛠",
            "secretary": "✉️",
            "journal": "📖",
            "governance": "⚖️",
            "general-purpose": "🧰",
            "Explore": "🔍",
            "Plan": "🗺",
        }
        active_count = 0
        done_count = 0
        rows = []
        for a in agents:
            if a["elapsed"] < 60:
                dot = '<span style="color:#22c55e">●</span>'
                status = "active"
                active_count += 1
            elif a["elapsed"] < 180:
                dot = '<span style="color:#22c55e">◐</span>'
                status = "tool-running"
                active_count += 1
            elif a["elapsed"] < 900:
                dot = '<span style="color:#9ca3af">⏹</span>'
                status = "completed"
                done_count += 1
            else:
                dot = '<span style="color:#475569">⏹</span>'
                status = "stopped"
                done_count += 1
            emoji = kind_emoji.get(a["kind"], "🤖")
            kind_label = html.escape(a["kind"])
            desc = html.escape(a["desc"]) if a["desc"] else ""
            rows.append(f"""<tr>
                <td>{dot}</td>
                <td>{emoji} <b>{kind_label}</b></td>
                <td class="status-{status}">{status}</td>
                <td>{a['elapsed']}s</td>
                <td>{a['lines']}L</td>
            </tr>
            <tr class="desc-row"><td></td><td colspan="4">{desc}</td></tr>""")
        agents_section = f"""
        <table class="agents">
          <thead><tr><th></th><th>agent</th><th>status</th><th>elapsed</th><th>lines</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
        <div class="hint">active {active_count} / done·stopped {done_count} (last 30 min)</div>
        <div class="hint">● green=active (&lt;60s) / ◐ green=tool-running (&lt;180s) / ⏹ grey=completed·stopped</div>
        """
    else:
        agents_section = '<div class="hint">no active agents</div>'

    # UPCOMING agents (dispatch 予定・空なら表示しない)
    upcoming_section = ""
    queue_file = PROJECT_ROOT / "data" / "agent_queue.jsonl"
    if queue_file.exists():
        try:
            with queue_file.open("r", encoding="utf-8") as f:
                queued = [json.loads(line) for line in f if line.strip()]
            if queued:
                rows_up = []
                kind_emoji_up = {
                    "builder": "🔨", "navigator": "🧭", "redteam": "🎯",
                    "strategist": "♟️", "analyst": "📊", "sns": "📣",
                    "ops": "🛠", "secretary": "✉️", "journal": "📖",
                    "governance": "⚖️",
                }
                for q in queued[:5]:
                    emoji = kind_emoji_up.get(q.get("kind", ""), "🤖")
                    waits = q.get("waits_for", "")
                    waits_str = f"<span class='sub-en'>(waits: {html.escape(waits)})</span>" if waits else ""
                    rows_up.append(f"<li>{emoji} <b>{html.escape(q.get('kind', ''))}</b> — {html.escape(q.get('task', '')[:60])} {waits_str}</li>")
                upcoming_section = f"""
<div class="sec">
<div class="sec-title">Upcoming ({len(queued)})</div>
<ul class="upcoming">{''.join(rows_up)}</ul>
</div>
"""
        except Exception:
            pass

    # Builder 修正進捗（要点のみ）
    target = get_monitor_target()
    progress_line = ""
    if target:
        agent_jsonl = find_agent_jsonl(target)
        if agent_jsonl is not None:
            try:
                import re
                fix_nums = set()
                with agent_jsonl.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = rec.get("message", {})
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    t = item.get("text", "")
                                    for m in re.finditer(r"修正\s*(\d+)", t):
                                        fix_nums.add(int(m.group(1)))
                max_fix = max(fix_nums) if fix_nums else 0
                progress_line = f"<div class='line'>Builder fix progress: <b>{len(fix_nums)}/14</b> covered (latest #{max_fix})</div>"
            except Exception:
                pass

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>Sora Lab Monitor</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:-apple-system,"Hiragino Sans",sans-serif;background:#0b1220;color:#e5e7eb;padding:10px;margin:0;font-size:13px}}
  .head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
  .head h1{{font-size:16px;margin:0}}
  .now{{font-size:11px;color:#60a5fa}}
  .banner{{border-radius:10px;padding:14px;text-align:center;font-size:22px;font-weight:bold;margin-bottom:10px;line-height:1.3}}
  .banner-sub{{font-size:12px;font-weight:normal;opacity:0.85}}
  .banner.waiting{{background:#78350f;color:#fde68a;border:3px solid #f59e0b;animation:pulse 1.5s infinite}}
  .banner.alive{{background:#14532d;color:#bbf7d0;border:2px solid #22c55e}}
  .banner.standby{{background:#0c4a6e;color:#bae6fd;border:2px solid #0284c7}}
  .banner.idle{{background:#1e293b;color:#cbd5e1;border:2px solid #64748b}}
  .banner.dead{{background:#7f1d1d;color:#fecaca;border:3px solid #dc2626}}
  .desc-row td{{padding:2px 6px 6px 28px;font-size:10px;color:#9ca3af;border-top:none}}
  .status-active{{color:#22c55e;font-weight:bold}}
  .status-tool-running{{color:#86efac}}
  .status-completed{{color:#9ca3af}}
  .status-stopped{{color:#64748b}}
  .sub-en{{font-size:10px;color:#6b7280;font-weight:normal}}
  .upcoming{{margin:4px 0;padding-left:20px;color:#9ca3af}}
  .upcoming li{{font-size:12px;margin-bottom:3px}}
  .upcoming li b{{color:#cbd5e1}}
  .phase-header{{font-size:13px;color:#fde68a;margin-bottom:6px;padding:6px;background:#451a03;border-radius:4px}}
  .phase-note{{font-size:11px;color:#9ca3af;margin-bottom:8px;font-style:italic}}
  .phase-row{{background:#111827;padding:8px 10px;margin-bottom:6px;border-radius:6px}}
  .phase-line{{font-size:12px;color:#e5e7eb;margin-bottom:3px}}
  .phase-icon{{margin-right:4px}}
  .phase-status{{font-size:10px;margin-left:8px;opacity:0.8}}
  .phase-purpose{{font-size:11px;color:#9ca3af;margin-top:2px;padding-left:20px}}
  .phase-artifact{{font-size:10px;color:#6b7280;padding-left:20px;margin-top:2px}}
  .phase-blocker{{font-size:11px;color:#fca5a5;padding-left:20px;margin-top:3px;background:#450a0a;padding:4px 6px;border-radius:3px}}
  .tunnel-url{{font-size:11px;color:#fde68a;padding:4px 8px;background:#1e3a8a;border-radius:4px;margin-bottom:8px;word-break:break-all}}
  .tunnel-url a{{color:#bae6fd;text-decoration:none}}
  .sup-status{{font-size:10px;color:#cbd5e1;padding:4px 8px;background:#1f2937;border-radius:4px;margin-bottom:8px}}
  .sup-sub{{font-size:9px;color:#6b7280;margin-top:2px}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.75}}}}
  .line{{font-size:11px;color:#9ca3af;margin:4px 0;word-break:break-all}}
  .sec{{margin-top:10px;border-top:1px solid #374151;padding-top:8px}}
  .sec-title{{font-size:11px;color:#9ca3af;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em}}
  .agents{{width:100%;border-collapse:collapse;font-size:12px}}
  .agents th{{text-align:left;color:#6b7280;font-weight:normal;padding:2px 6px}}
  .agents td{{padding:3px 6px;border-top:1px solid #1f2937}}
  code{{background:#1f2937;padding:1px 4px;border-radius:3px;font-size:11px}}
  .hint{{font-size:10px;color:#6b7280;margin-top:6px}}
  .health-row{{font-size:12px;padding:5px 8px;margin:3px 0;border-radius:4px;border-left:3px solid #6b7280}}
  .health-row.ok{{background:#052e16;border-left-color:#22c55e;color:#bbf7d0}}
  .health-row.warn{{background:#451a03;border-left-color:#f59e0b;color:#fde68a}}
  .health-row.emergency{{background:#450a0a;border-left-color:#ef4444;color:#fecaca;animation:pulse 2s infinite}}
  .health-sub{{font-size:10px;color:#9ca3af;display:block;margin-top:2px}}
</style>
</head>
<body>
<div class="head">
  <h1>★ Sora Lab Monitor</h1>
  <div class="now">{now_jst} / 5s refresh</div>
</div>
{_render_tunnel_url()}
{_render_supervisor_status()}

{sora_banner}
{sora_line}

<div class="sec">
<div class="sec-title">Recent agent activity</div>
{agents_section}
</div>

<div class="sec">
<div class="sec-title">Progress</div>
{progress_line if progress_line else '<div class="line">-</div>'}
</div>

{upcoming_section}

{_render_live_health()}

{_render_phase_section()}
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = build_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> int:
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    ip = get_lan_ip()
    print(f"Sora status server listening on http://{ip}:{PORT}/ (also 0.0.0.0)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
