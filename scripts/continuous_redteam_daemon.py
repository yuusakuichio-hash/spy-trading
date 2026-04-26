#!/usr/bin/env python3
"""
continuous_redteam_daemon.py — 常駐 adversarial red team daemon

5 分ごとに直近 Claude 応答トランスクリプトから断定文 (claim) を抽出し、
各 claim に反例探索を仕掛ける。反例 found → violation log。
反例ゼロの場合も "壊れてたら何が観測される？" チャレンジを実施。

Usage:
    python3 scripts/continuous_redteam_daemon.py [--once] [--lookback 300]

Flags:
    --once      1 回実行して終了 (LaunchAgent 5 分間隔で使う)
    --lookback  秒数 (デフォルト 300 = 5 分)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ─────────────────────────── 定数 ────────────────────────────────
BASE_DIR = Path("/Users/yuusakuichio/trading")
TRANSCRIPT_DIR = Path("/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading")
LOG_DIR = BASE_DIR / "data" / "logs"
VIOLATION_LOG = LOG_DIR / "redteam_daemon_violations.log"
CHALLENGE_LOG = LOG_DIR / "redteam_daemon_challenges.log"
STATE_FILE = LOG_DIR / "redteam_daemon_state.json"

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER = "u2cevk8nktib3sr148rw2hs78ecvux"

# ─────────────────────────── Claim パターン ───────────────────────
# (label, regex) — 断定文パターン
CLAIM_PATTERNS: list[tuple[str, str]] = [
    ("bot_active",        r"(Bot|Atlas|Chronos)\s*(稼[働動]中|active|running|is\s+up)"),
    ("paper_active",      r"(ペーパー|paper)\s*(動いてる|稼働中|正常|active|running)"),
    ("test_pass",         r"(全件?合格|全テスト[Pp][Aa][Ss][Ss]|全PASS|\d+\s*/\s*\d+\s*PASS|0\s*fail)"),
    ("service_up",        r"(service|サービス)\s*(起動中|稼働中|active|running)"),
    ("trade_confirmed",   r"(発注|trade|エントリー|TP)\s*(確認済|成功|完了|done)"),
    ("log_normal",        r"(log|ログ)\s*(正常|clean|エラーなし|no\s+error)"),
    ("deploy_complete",   r"(デプロイ|deploy)\s*(完了|success|done)"),
    ("fix_complete",      r"(修正|fix)\s*(完了|済|done)"),
    ("e2e_pass",          r"E2E\s*(PASS|合格|成功|\d+/\d+)"),
    ("mutation_pass",     r"mutation\s*(score\s*[:=])?\s*\d+%"),
    ("watchdog_active",   r"watchdog\s*(稼働中|active|running|監視中)"),
    ("heartbeat_ok",      r"heartbeat\s*(OK|正常|alive|active)"),
]

# ─────────────────────────── 反例探索ルール ───────────────────────
@dataclass
class CounterEvidenceCheck:
    label: str
    description: str
    # チェック関数 -> (found: bool, detail: str)
    # found=True → 反例発見 (violation)

def _check_log_recent_output(log_path: Path, minutes: int = 10) -> tuple[bool, str]:
    """ログに直近 N 分以内の有意な出力があるか確認"""
    if not log_path.exists():
        return True, f"log not found: {log_path}"
    mtime = log_path.stat().st_mtime
    age_sec = time.time() - mtime
    if age_sec > minutes * 60:
        return True, f"log stale: {log_path.name} ({age_sec/60:.1f} min ago)"
    # ファイルが空に近い
    size = log_path.stat().st_size
    if size < 10:
        return True, f"log near-empty: {log_path.name} ({size} bytes)"
    return False, f"log ok: {log_path.name} ({age_sec:.0f}s ago, {size}b)"

def _check_service_error(log_path: Path) -> tuple[bool, str]:
    """ログ末尾 200 行にエラー/例外があるか"""
    if not log_path.exists():
        return True, f"log missing: {log_path}"
    try:
        text = log_path.read_text(errors="replace")
        lines = text.splitlines()[-200:]
        errors = [l for l in lines if re.search(r"(ERROR|Exception|Traceback|CRITICAL|FATAL)", l, re.I)]
        if errors:
            return True, f"errors in log: {errors[-1][:120]}"
        return False, "no errors in recent log"
    except Exception as e:
        return True, f"log read error: {e}"

def _check_atlas_state() -> tuple[bool, str]:
    """atlas_state.json の last_updated が 30 分以内か"""
    st = BASE_DIR / "data" / "atlas_state.json"
    if not st.exists():
        return True, "atlas_state.json missing"
    try:
        d = json.loads(st.read_text())
        ts = d.get("last_updated") or d.get("updated_at") or d.get("timestamp")
        if not ts:
            return True, "atlas_state.json has no timestamp field"
        # parse ISO
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age = (datetime.datetime.now(tz=datetime.timezone.utc) - dt).total_seconds()
        if age > 1800:
            return True, f"atlas_state.json stale: {age/60:.1f} min"
        return False, f"atlas_state fresh: {age:.0f}s"
    except Exception as e:
        return True, f"atlas_state parse error: {e}"

def _check_chronos_watchdog_state() -> tuple[bool, str]:
    """chronos_watchdog_recovery_state.json で last_seen が新鮮か"""
    st = BASE_DIR / "data" / "chronos_watchdog_recovery_state.json"
    if not st.exists():
        return True, "chronos_watchdog_recovery_state.json missing"
    try:
        d = json.loads(st.read_text())
        ts = d.get("last_seen") or d.get("last_updated")
        if not ts:
            return True, "no timestamp in chronos_watchdog_recovery_state.json"
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age = (datetime.datetime.now(tz=datetime.timezone.utc) - dt).total_seconds()
        if age > 1800:
            return True, f"chronos watchdog state stale: {age/60:.1f} min"
        return False, f"chronos watchdog state fresh: {age:.0f}s"
    except Exception as e:
        return True, f"watchdog state parse error: {e}"

def _check_launchd_service(label: str) -> tuple[bool, str]:
    """launchctl list でサービスが実際に動いているか"""
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return True, f"launchctl list {label}: not found / error"
        # PID があれば稼働中
        pid_match = re.search(r'"PID"\s*=\s*(\d+)', r.stdout)
        if not pid_match:
            return True, f"{label}: no PID (not running)"
        return False, f"{label}: PID={pid_match.group(1)}"
    except subprocess.TimeoutExpired:
        return True, f"launchctl timeout for {label}"
    except Exception as e:
        return True, f"launchctl error: {e}"

def _check_atlas_log() -> tuple[bool, str]:
    return _check_service_error(LOG_DIR / "atlas_agent.log")

def _check_chronos_log() -> tuple[bool, str]:
    return _check_service_error(LOG_DIR / "chronos_bot.log" if (LOG_DIR / "chronos_bot.log").exists() else LOG_DIR / "atlas_agent.log")

def _check_atlas_log_recency() -> tuple[bool, str]:
    return _check_log_recent_output(LOG_DIR / "atlas_agent.log", minutes=15)

def _check_paper_trade_log() -> tuple[bool, str]:
    """ペーパートレードのアクションログが存在し最近の出力があるか"""
    return _check_log_recent_output(LOG_DIR / "atlas_actions.log", minutes=30)

# claim label → 反例チェックリスト のマッピング
COUNTER_CHECKS: dict[str, list[tuple[str, object]]] = {
    "bot_active": [
        ("atlas_log_errors", _check_atlas_log),
        ("atlas_log_recency", _check_atlas_log_recency),
        ("atlas_state_fresh", _check_atlas_state),
    ],
    "paper_active": [
        ("paper_trade_log", _check_paper_trade_log),
        ("atlas_log_errors", _check_atlas_log),
    ],
    "watchdog_active": [
        ("chronos_watchdog_state", _check_chronos_watchdog_state),
    ],
    "heartbeat_ok": [
        ("atlas_log_recency", _check_atlas_log_recency),
    ],
    "service_up": [
        ("atlas_log_errors", _check_atlas_log),
    ],
    "log_normal": [
        ("atlas_log_errors", _check_atlas_log),
        ("chronos_log_errors", _check_chronos_log),
    ],
    "e2e_pass": [],     # 反例なし → Active Challenge で対応
    "test_pass": [],
    "fix_complete": [],
    "deploy_complete": [],
    "trade_confirmed": [
        ("atlas_actions_recency", _check_paper_trade_log),
    ],
    "mutation_pass": [],
}

# ─────────────────────────── Active Challenge ─────────────────────
ACTIVE_CHALLENGES: dict[str, list[str]] = {
    "bot_active": [
        "atlas_agent.log に直近 15 分の INFO/TRADE 出力がゼロになっていないか？",
        "atlas_state.json の last_updated が 30 分以上前になっていないか？",
        "プロセス異常終了して再起動ループに入っていないか (ERROR 連続)？",
    ],
    "paper_active": [
        "atlas_actions.log に直近 30 分のエントリーまたはフォローが存在するか？",
        "market closed でもないのに TRADE 出力がゼロになっていないか？",
    ],
    "test_pass": [
        "selective test 実行 (一部のみ) で全体テストを skip していないか？",
        "mock 差し替えで実際の接続をバイパスしていないか？",
        "assertion が薄くて何でも pass するテストになっていないか？",
    ],
    "fix_complete": [
        "実 grep 確認なしに完了と判断していないか？",
        "AST 検証なしに構文正しいと仮定していないか？",
        "mutation score 未計測のまま完了宣言していないか？",
    ],
    "e2e_pass": [
        "E2E は実際の外部 API/ログ往復を確認したか、それとも mock 往復か？",
        "テスト通過後に対象ファイルが上書きされていないか？",
    ],
    "deploy_complete": [
        "デプロイ後に launchctl list で PID 確認したか？",
        "log に STARTUP 行が出力されたか？",
    ],
    "watchdog_active": [
        "watchdog が検知ループに入ったまま回復アクションを実行していないケースはないか？",
        "chronos_watchdog_recovery_state.json が 30 分以上更新されていないか？",
    ],
    "mutation_pass": [
        "mutation score は実際に mutmut run を実行した結果か、それとも推測値か？",
        "score 計算対象ファイルは本当に変更されたファイルか？",
    ],
    "heartbeat_ok": [
        "heartbeat log の最終行タイムスタンプが 15 分以上前になっていないか？",
        "heartbeat は送信だけで受信確認を省略していないか？",
    ],
    "log_normal": [
        "tail -100 で確認した場合に ERROR 行がないか、全期間ではなく直近だけ確認していないか？",
        "stderr log を見落としていないか？",
    ],
    "service_up": [
        "systemd/launchd の status が active でも実プロセスが zombie になっていないか？",
    ],
    "trade_confirmed": [
        "trade 完了は fill confirmation を確認したか、それとも発注 API コールだけか？",
    ],
}

# ─────────────────────────── Transcript 解析 ──────────────────────

@dataclass
class Claim:
    label: str
    text: str
    source_file: str
    timestamp: str


def extract_claims_from_transcripts(lookback_sec: int = 300) -> list[Claim]:
    """直近 lookback_sec 秒以内の assistant メッセージから claim を抽出"""
    cutoff = time.time() - lookback_sec
    claims: list[Claim] = []

    # 最新の jsonl ファイル (mtime > cutoff - 3600) を対象
    jsonl_files = sorted(
        TRANSCRIPT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for jsonl_path in jsonl_files[:5]:  # 最大 5 ファイル
        if jsonl_path.stat().st_mtime < cutoff - 3600:
            break
        try:
            lines = jsonl_path.read_text(errors="replace").splitlines()
        except Exception:
            continue

        for line in lines:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            if d.get("type") != "assistant":
                continue

            # timestamp フィルタ
            ts_str = d.get("timestamp", "")
            try:
                if ts_str:
                    ts_dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts_dt.timestamp() < cutoff:
                        continue
            except Exception:
                pass

            # テキスト抽出
            msg = d.get("message", {})
            content = msg.get("content", [])
            text = ""
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text += c.get("text", "")

            if not text:
                continue

            # claim パターンマッチ
            for label, pattern in CLAIM_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE | re.DOTALL):
                    # マッチした周辺テキスト (最大 200 chars)
                    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                    start = max(0, m.start() - 60)
                    end = min(len(text), m.end() + 60)
                    snippet = text[start:end].replace("\n", " ")

                    claims.append(Claim(
                        label=label,
                        text=snippet,
                        source_file=jsonl_path.name,
                        timestamp=ts_str or datetime.datetime.now(tz=JST).isoformat(),
                    ))

    return claims


# ─────────────────────────── 反例探索 ────────────────────────────

@dataclass
class EvidenceResult:
    claim: Claim
    counter_found: bool
    check_name: str
    detail: str
    challenges: list[str]


def run_counter_evidence(claims: list[Claim]) -> list[EvidenceResult]:
    results: list[EvidenceResult] = []

    for claim in claims:
        checks = COUNTER_CHECKS.get(claim.label, [])
        counter_found = False
        found_check = "none"
        found_detail = "no counter-evidence checks defined"

        for check_name, check_fn in checks:
            try:
                found, detail = check_fn()
            except Exception as e:
                found, detail = True, f"check error: {e}"

            if found:
                counter_found = True
                found_check = check_name
                found_detail = detail
                break
            else:
                found_detail = detail

        challenges = ACTIVE_CHALLENGES.get(claim.label, [])

        # checks がゼロの場合も challenges は active に
        if not checks:
            found_check = "no_checks_defined"
            found_detail = "no automated checks — active challenges only"

        results.append(EvidenceResult(
            claim=claim,
            counter_found=counter_found,
            check_name=found_check,
            detail=found_detail,
            challenges=challenges,
        ))

    return results


# ─────────────────────────── ログ出力 ────────────────────────────

def write_violation(result: EvidenceResult) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M:%S JST")
    line = (
        f"[{ts}] VIOLATION claim={result.claim.label} "
        f"check={result.check_name} | {result.detail}\n"
        f"  claim_text: {result.claim.text[:120]}\n"
        f"  source: {result.claim.source_file}\n"
    )
    with VIOLATION_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def write_challenges(results: list[EvidenceResult]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M:%S JST")
    lines = [f"\n=== [{ts}] Active Challenges ===\n"]
    for r in results:
        if r.challenges:
            lines.append(f"[{r.claim.label}] claim: {r.claim.text[:80]}\n")
            for ch in r.challenges:
                lines.append(f"  ? {ch}\n")
    with CHALLENGE_LOG.open("a", encoding="utf-8") as f:
        f.writelines(lines)


def send_pushover_violation(violations: list[EvidenceResult]) -> None:
    """重大 violation は Pushover で通知"""
    if not violations:
        return
    # 静穏時間チェック (JST 22:00-04:00 は非緊急スキップ)
    now_h = datetime.datetime.now(tz=JST).hour
    is_quiet = now_h >= 22 or now_h < 4

    for v in violations[:3]:  # 最大 3 件
        msg = (
            f"[Redteam] CLAIM VIOLATED: {v.claim.label}\n"
            f"check: {v.check_name}\n"
            f"{v.detail[:100]}"
        )
        priority = 0 if is_quiet else 1
        try:
            subprocess.run(
                [
                    "curl", "-s",
                    "--form-string", f"token={PUSHOVER_TOKEN}",
                    "--form-string", f"user={PUSHOVER_USER}",
                    "--form-string", f"message={msg}",
                    "--form-string", f"title=[SYS/REDTEAM] Adversarial Alert",
                    "--form-string", f"priority={priority}",
                    "https://api.pushover.net/1/messages.json",
                ],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass


# ─────────────────────────── State 管理 ──────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run": None, "total_claims": 0, "total_violations": 0, "runs": 0}


def save_state(state: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ─────────────────────────── メイン ──────────────────────────────

def run_once(lookback_sec: int = 300) -> dict:
    ts = datetime.datetime.now(tz=JST).isoformat()
    print(f"[{ts}] continuous_redteam_daemon: scanning last {lookback_sec}s ...")

    claims = extract_claims_from_transcripts(lookback_sec)
    print(f"  claims found: {len(claims)}")
    for c in claims:
        print(f"    [{c.label}] {c.text[:60]}")

    if not claims:
        print("  no claims extracted — no action")
        return {"claims": 0, "violations": 0}

    results = run_counter_evidence(claims)

    violations = [r for r in results if r.counter_found]
    print(f"  violations: {len(violations)} / {len(results)}")

    for v in violations:
        print(f"  VIOLATION [{v.claim.label}] {v.check_name}: {v.detail[:80]}")
        write_violation(v)

    write_challenges(results)
    send_pushover_violation(violations)

    return {"claims": len(claims), "violations": len(violations)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="1 回実行して終了")
    parser.add_argument("--lookback", type=int, default=300, help="スキャン範囲 (秒)")
    args = parser.parse_args()

    state = load_state()

    result = run_once(args.lookback)

    state["last_run"] = datetime.datetime.now(tz=JST).isoformat()
    state["total_claims"] = state.get("total_claims", 0) + result["claims"]
    state["total_violations"] = state.get("total_violations", 0) + result["violations"]
    state["runs"] = state.get("runs", 0) + 1
    save_state(state)

    ts = datetime.datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M:%S JST")
    print(f"[{ts}] done. claims={result['claims']} violations={result['violations']} total_runs={state['runs']}")


if __name__ == "__main__":
    main()
