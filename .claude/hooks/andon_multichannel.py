#!/usr/bin/env python3
"""
andon_multichannel.py — Andon Cord 3 経路物理実装（Toyota Jidoka 由来）

flow_audit P0-4 対応: Pushover 単一経路依存（10000/月 quota で死んだ）を解消。
3 経路 OR 条件で「誰でも STOP 発令可能」を物理担保する。

経路:
  1. Pushover（既存・quota 超過時に失敗）
  2. ntfy.sh（セルフホスト or 公式・低コスト）
  3. data/KILL_SWITCH ファイル（ローカルファイル・確実）

経路のいずれか 1 つでも成功すれば Andon 発令成立。
3 経路全失敗の場合はゆうさくさん iPhone に物理アラート（要別実装）。

Claude Code hook としても使える（PreToolUse / UserPromptSubmit 等で)。
CLI からも直接呼べる: python3 andon_multichannel.py --pull --reason "..."
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# ── 定数 ─────────────────────────────────────────────────────────────────────

KILL_SWITCH_PATH = Path(__file__).parent.parent.parent / "data" / "KILL_SWITCH"
ANDON_LOG_PATH = Path(__file__).parent.parent.parent / "data" / "governance" / "andon_log.jsonl"
ANDON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _log_event(record: dict) -> None:
    """Andon イベント記録（append-only hash-chain）"""
    record["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(ANDON_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[WARN] andon_log 書込失敗: {e}", file=sys.stderr)


def _send_pushover(title: str, message: str) -> tuple[bool, str]:
    """Pushover 通知（経路 1）"""
    token = os.environ.get("PUSHOVER_ALERT_TOKEN") or os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER_KEY") or os.environ.get("PUSHOVER_USER")
    if not token or not user:
        return False, "pushover: token/user 未設定"

    # SMOKE_TEST=1 環境変数または reason に "smoke_test" 含む場合は priority=1 へ降格
    # （priority=2 は retry 30s + expire 3600s で確認するまで鳴り続けるため）
    smoke = os.environ.get("ANDON_SMOKE_TEST", "") == "1" or "smoke_test" in message.lower()
    priority = 1 if smoke else 2
    try:
        body_dict = {
            "token": token,
            "user": user,
            "title": title[:250],
            "message": message[:1024],
            "priority": priority,
        }
        # priority=2 のみ retry/expire を付与
        if priority == 2:
            body_dict["retry"] = 30
            body_dict["expire"] = 3600
        body = json.dumps(body_dict).encode()
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            r = json.loads(resp.read())
            if r.get("status") == 1:
                return True, "pushover: ok"
            return False, f"pushover: status={r.get('status')} err={r.get('errors', [])}"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            err_body = str(e)
        return False, f"pushover: HTTP {e.code} {err_body[:200]}"
    except Exception as e:
        return False, f"pushover: {type(e).__name__}: {e}"


def _send_ntfy(title: str, message: str) -> tuple[bool, str]:
    """ntfy.sh 通知（経路 2）"""
    topic = os.environ.get("NTFY_ANDON_TOPIC") or os.environ.get("NTFY_TOPIC")
    if not topic:
        return False, "ntfy: NTFY_ANDON_TOPIC/NTFY_TOPIC 未設定"

    try:
        url = f"https://ntfy.sh/{topic}"
        req = urllib.request.Request(
            url,
            data=message.encode("utf-8")[:4000],
            headers={
                "Title": title[:100],
                "Priority": "urgent",
                "Tags": "rotating_light,andon,sora_lab",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True, "ntfy: ok"
            return False, f"ntfy: HTTP {resp.status}"
    except Exception as e:
        return False, f"ntfy: {type(e).__name__}: {e}"


def _touch_kill_switch(reason: str) -> tuple[bool, str]:
    """
    KILL_SWITCH ファイル生成 + 既存 bot 用 common.kill_switch も同時発動（経路 3・最確実）

    2 重ファイル更新:
      1. data/KILL_SWITCH (本 hook 用・PreToolUse hook が check)
      2. data/kill_switch.flag (既存 spy_bot/chronos_bot/atlas_agent/chronos_agent/pre_trade_check が check)

    Three Mile Island 同型問題対応 (Redteam S1):
    Andon を引いたが既存 bot が止まらない事象を完全排除するため、
    common.kill_switch.activate() を必ず呼び出す。
    """
    results = []
    overall_ok = True

    # 経路 3a: hook 用 KILL_SWITCH ファイル
    try:
        KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pulled_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "pid": os.getpid(),
        }
        KILL_SWITCH_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        results.append(f"hook KILL_SWITCH: ok")
    except Exception as e:
        overall_ok = False
        results.append(f"hook KILL_SWITCH: {type(e).__name__}: {e}")

    # 経路 3b: 既存 bot 用 common.kill_switch.activate() 同時呼出
    try:
        import sys as _sys
        TRADING_ROOT = str(Path(__file__).parent.parent.parent)
        if TRADING_ROOT not in _sys.path:
            _sys.path.insert(0, TRADING_ROOT)
        from common.kill_switch import activate as _bot_activate
        _bot_activate(reason=f"andon_multichannel: {reason[:200]}", activator="andon_multichannel")
        results.append(f"bot kill_switch.flag: ok")
    except Exception as e:
        overall_ok = False
        results.append(f"bot kill_switch.flag: {type(e).__name__}: {str(e)[:100]}")

    detail = " / ".join(results)
    return overall_ok, f"kill_switch: {detail}"


def pull_andon(reason: str, source: str = "unknown") -> dict:
    """
    Andon Cord を引く（3 経路全発火・少なくとも 1 経路成功で成立）。

    Returns:
        {"success": bool, "channels": {"pushover": bool, "ntfy": bool, "kill_switch": bool}, "details": {...}}
    """
    title = f"[ANDON] Sora Lab 緊急停止 ({source})"
    msg = f"Andon Cord 発令\n\n理由: {reason}\n発令元: {source}\n時刻: {datetime.now(timezone.utc).isoformat()}\n\n全 agent 停止・M&M Conference 発動推奨"

    results = {}
    details = {}
    # 3 経路並列ではなく順次（実装簡素化・合計 30s 以内）
    for name, fn in [
        ("kill_switch", lambda: _touch_kill_switch(reason)),  # 最確実から
        ("ntfy", lambda: _send_ntfy(title, msg)),
        ("pushover", lambda: _send_pushover(title, msg)),
    ]:
        ok, detail = fn()
        results[name] = ok
        details[name] = detail

    success_count = sum(1 for v in results.values() if v)
    overall_success = success_count >= 1

    record = {
        "event": "andon_pull",
        "source": source,
        "reason": reason,
        "channels_success": results,
        "channels_detail": details,
        "success_count": success_count,
        "overall_success": overall_success,
    }
    _log_event(record)

    return {
        "success": overall_success,
        "channels": results,
        "details": details,
    }


def check_kill_switch() -> bool:
    """KILL_SWITCH ファイルの存在確認"""
    return KILL_SWITCH_PATH.exists()


def release_kill_switch(releaser: str, reason: str) -> bool:
    """KILL_SWITCH 解除（人間承認が必要な前提・自動解除禁止）"""
    if not KILL_SWITCH_PATH.exists():
        return False
    try:
        existing = json.loads(KILL_SWITCH_PATH.read_text())
    except Exception:
        existing = {}
    record = {
        "event": "andon_release",
        "releaser": releaser,
        "reason": reason,
        "original_pull": existing,
    }
    _log_event(record)
    KILL_SWITCH_PATH.unlink()
    return True


# ── hook 互換モード（PreToolUse で KILL_SWITCH が存在したら全 tool block） ──

def hook_mode() -> int:
    """
    Claude Code hook として動作: stdin の JSON を読んで、KILL_SWITCH が存在すれば exit 2 で block。

    Redteam B2 対応: stdin が不正でも KILL_SWITCH チェックは必ず実施する。
    json.loads 失敗時に return 0 で素通りさせると Andon 発動中に block されない silent failure。
    """
    # KILL_SWITCH 存在は stdin に依らず最優先チェック
    if check_kill_switch():
        try:
            payload = json.loads(KILL_SWITCH_PATH.read_text())
        except Exception:
            payload = {}
        print(
            f"[ANDON_BLOCK] KILL_SWITCH 発令中 → 全 tool 呼出を block\n"
            f"  pulled_at: {payload.get('pulled_at', '?')}\n"
            f"  reason: {payload.get('reason', '?')}\n"
            f"  解除: python3 .claude/hooks/andon_multichannel.py --release --releaser=<name> --reason=<理由>",
            file=sys.stderr,
        )
        return 2
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Andon Cord 3 経路")
    parser.add_argument("--pull", action="store_true", help="Andon 発令")
    parser.add_argument("--release", action="store_true", help="KILL_SWITCH 解除")
    parser.add_argument("--check", action="store_true", help="KILL_SWITCH 状態確認")
    parser.add_argument("--reason", default="manual", help="発令/解除理由")
    parser.add_argument("--source", default="cli", help="発令元識別")
    parser.add_argument("--releaser", default="", help="解除者（必須・--release 時）")
    parser.add_argument("--hook", action="store_true", help="hook モード（stdin で JSON 受領・KILL_SWITCH あれば exit 2）")
    args = parser.parse_args()

    if args.hook:
        sys.exit(hook_mode())

    if args.pull:
        result = pull_andon(args.reason, args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["success"] else 2)

    if args.release:
        if not args.releaser:
            print("ERROR: --releaser is required", file=sys.stderr)
            sys.exit(1)
        ok = release_kill_switch(args.releaser, args.reason)
        print(f"released: {ok}")
        sys.exit(0 if ok else 1)

    if args.check:
        active = check_kill_switch()
        if active:
            try:
                payload = json.loads(KILL_SWITCH_PATH.read_text())
            except Exception:
                payload = {}
            print(f"ACTIVE: {json.dumps(payload, ensure_ascii=False)}")
            sys.exit(2)
        else:
            print("INACTIVE")
            sys.exit(0)

    parser.print_help()


if __name__ == "__main__":
    main()
