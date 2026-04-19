#!/usr/bin/env python3
"""
atlas_watchdog.py — プロセス監視（最後の砦）(Sora Lab / Atlas)

役割:
  - condor.log / atlas_agent.log の新規行を tail
  - エラーパターン検知（ERROR/WARNING/strike不整合/gamma_early_exit）
  - 閾値超過時に Pushover priority=1 で即通知
  - 5分毎 health check（ファイルサイズ・プロセス確認）
  - chronos_watchdog.py の Atlas 版ミラー

設計方針:
  - シンプル・軽量・「最後の砦」として常に動作
  - 高機能自律対応は atlas_agent.py が担当
  - 自己回復: 更新停止 → kickstart → bootstrap → 人間介入（3段階）
  - Pushover backoff: 429 連続3回 → 30分沈黙 + ローカルキュー

依存: requests, stdlib
起動: LaunchAgent com.atlas.watchdog
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

# ── Heartbeat pulse（能動監視）───────────────────────────────────────────────
try:
    from common.heartbeat import write_pulse as _write_pulse
    _HEARTBEAT_OK = True
except ImportError:
    _HEARTBEAT_OK = False
    def _write_pulse(*a, **kw): pass  # type: ignore[misc]

# ── 共通 Pushover クライアント（SPOF解消・backoff/queue一元管理） ─────────────
try:
    from common import pushover_client as _pc
    _PC_AVAILABLE = True
except ImportError:
    _PC_AVAILABLE = False

# ── .env ロード ──────────────────────────────────────────────────────────────
def _load_env_file():
    for candidate in [Path("/root/spxbot/.env"), Path(__file__).parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
            break

_load_env_file()

# ── 定数 ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()

LOG_PATH     = BASE_DIR / "data" / "logs" / "condor.log"
WATCHDOG_LOG = BASE_DIR / "data" / "logs" / "atlas_watchdog.log"

HEALTH_CHECK_INTERVAL_SEC = 300   # 5分毎 health check
CHECK_INTERVAL = 10                # ログ tail チェック間隔（秒）

JST = timezone(timedelta(hours=9))

# ── 監視対象定義（時間帯ゲート付き） ──────────────────────────────────────────
# watch_windows_jst: [(開始HH:MM, 終了HH:MM), ...] JST表記
# 窓が日をまたぐ場合（例: 22:20〜05:10）は正しく処理する。
# 将来的に config/watch_targets.yaml に移す余地を残す（現在は .py にハードコード）。
WATCH_TARGETS = [
    {
        "path": BASE_DIR / "data" / "logs" / "condor.log",
        # Atlas (spy_bot/atlas_agent) は 22:25 JST 市場オープン時のみ起動 → 市場時間帯のみ監視
        "watch_windows_jst": [("22:20", "05:10")],
        "service": "com.atlas.agent",
    },
]


def _is_in_watch_window(windows_jst: list[tuple[str, str]]) -> bool:
    """現在時刻(JST)が watch_windows_jst の何れかの窓内にあるか判定する。

    窓が日をまたぐ場合（例: "22:20" → "05:10"）も正しく処理する。
    """
    now_jst = datetime.now(tz=JST)
    now_minutes = now_jst.hour * 60 + now_jst.minute

    for start_str, end_str in windows_jst:
        sh, sm = (int(x) for x in start_str.split(":"))
        eh, em = (int(x) for x in end_str.split(":"))
        start_min = sh * 60 + sm
        end_min   = eh * 60 + em

        if start_min <= end_min:
            # 同日内の窓（例: 00:00〜23:59）
            if start_min <= now_minutes <= end_min:
                return True
        else:
            # 日跨ぎ窓（例: 22:20〜05:10）
            if now_minutes >= start_min or now_minutes <= end_min:
                return True

    return False

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER",  "u2cevk8nktib3sr148rw2hs78ecvux")

# ── 自己回復設定 ──────────────────────────────────────────────────────────────
RECOVERY_STATE_PATH   = BASE_DIR / "data" / "atlas_watchdog_recovery_state.json"
RECOVERY_COOLDOWN_SEC = 600          # 試行間隔 10分
# 実在サービス名: launchctl list | grep atlas で確認済み (2026-04-20)
# com.atlas.agent は正しい。plist は LAUNCHCTL_PLIST_PATH で明示管理
LAUNCHCTL_SERVICE_ID  = "com.atlas.agent"
LAUNCHCTL_PLIST_PATH  = (
    Path.home() / "Library" / "LaunchAgents" / "com.atlas.agent.plist"
)

# ── Pushover backoff 設定 ─────────────────────────────────────────────────────
PUSHOVER_BACKOFF_STATE_PATH   = BASE_DIR / "data" / "atlas_pushover_backoff_state.json"
PUSHOVER_QUEUE_PATH           = BASE_DIR / "data" / "atlas_pushover_queue.jsonl"
PUSHOVER_429_MAX_CONSECUTIVE  = 3      # 連続この回数で沈黙
PUSHOVER_BACKOFF_DURATION_SEC = 1800   # 30分

# ── モジュールレベル backoff 状態 ─────────────────────────────────────────────
_pushover_consecutive_429: int = 0
_pushover_backoff_until: float = 0.0

# ── 検知パターン ──────────────────────────────────────────────────────────────
ALERT_PATTERNS = [
    (re.compile(r'\bERROR\b',    re.IGNORECASE), "ERROR"),
    (re.compile(r'\bWARNING\b',  re.IGNORECASE), "WARNING"),
    (re.compile(r'strike.*不整合|strike mismatch|invalid strike', re.IGNORECASE), "strike不整合"),
    (re.compile(r'gamma_early_exit|early.?exit.*gamma|gamma.*early.?exit', re.IGNORECASE), "gamma_early_exit"),
]

WINDOW_SECONDS = 300  # 5分
THRESHOLD      = 10   # 件数
ALERT_COOLDOWN = 60   # 秒

# パターン別タイムスタンプキュー
pattern_times: dict[str, deque] = defaultdict(lambda: deque())
last_alert_sent: dict[str, float] = {}

# HealthCheck cooldown
_last_health_check: float = 0.0
_last_health_alert: float = 0.0
HEALTH_ALERT_COOLDOWN_SEC = 3600  # 1時間に1回まで

# ── ロギング設定 ─────────────────────────────────────────────────────────────
WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("atlas_watchdog")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(WATCHDOG_LOG), encoding="utf-8"),
    ],
)


# ── Pushover backoff ヘルパー ─────────────────────────────────────────────────
def _load_backoff_state() -> None:
    """起動時に永続化済み backoff 状態を復元する。"""
    global _pushover_consecutive_429, _pushover_backoff_until
    try:
        if PUSHOVER_BACKOFF_STATE_PATH.exists():
            obj = json.loads(PUSHOVER_BACKOFF_STATE_PATH.read_text())
            _pushover_consecutive_429 = int(obj.get("consecutive_429", 0))
            _pushover_backoff_until   = float(obj.get("backoff_until", 0.0))
    except Exception as e:
        log.warning("[BACKOFF] state load error: %s", e)


def _save_backoff_state() -> None:
    try:
        PUSHOVER_BACKOFF_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PUSHOVER_BACKOFF_STATE_PATH.write_text(json.dumps({
            "consecutive_429": _pushover_consecutive_429,
            "backoff_until":   _pushover_backoff_until,
        }))
    except Exception as e:
        log.warning("[BACKOFF] state save error: %s", e)


def _queue_pushover(title: str, message: str, priority: int) -> None:
    """backoff 中に通知をローカルキューへ追記する。"""
    try:
        PUSHOVER_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts":       time.time(),
            "title":    title,
            "message":  message[:1024],
            "priority": priority,
        })
        with PUSHOVER_QUEUE_PATH.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
        log.info("[BACKOFF] queued: %s", title)
    except Exception as e:
        log.warning("[BACKOFF] queue write error: %s", e)


# ── Pushover（[Atlas]タグ付き） ───────────────────────────────────────────────
def pushover_send(title: str, message: str, priority: int = 1) -> None:
    """Pushover通知を送信する。[Atlas/Watchdog]タグを強制付与する。

    common.pushover_client 経由で送信することで backoff/queue を全スクリプト間で
    共有し、429 連鎖 ban を防止する（SPOF解消）。
    project_pushover_tag_convention.md のタグ規約（[Atlas]プレフィックス）に準拠。
    """
    if not title.startswith("["):
        title = f"[Atlas/Watchdog] {title}"

    if _PC_AVAILABLE:
        _pc.send(
            title,
            message,
            priority=priority,
            token=PUSHOVER_TOKEN or None,
            app_tag="Atlas/Watchdog",
        )
        return

    # フォールバック: 共通クライアント import 失敗時は旧実装で送信
    global _pushover_consecutive_429, _pushover_backoff_until
    now = time.time()
    if now < _pushover_backoff_until:
        remaining = _pushover_backoff_until - now
        log.info("[BACKOFF] active (%.0fs remaining) — queuing: %s", remaining, title)
        _queue_pushover(title, message, priority)
        return
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log.warning("[NOTIFY_SKIP] missing token/user. title=%s", title)
        return
    try:
        data: dict[str, Any] = {
            "token":    PUSHOVER_TOKEN,
            "user":     PUSHOVER_USER,
            "title":    title,
            "message":  message[:1024],
            "priority": priority,
        }
        if priority >= 2:
            data["retry"]  = 30
            data["expire"] = 3600
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=10,
        )
        _body_text = resp.text[:500] if hasattr(resp, "text") else ""
        _is_banned = "banned" in _body_text.lower()
        if resp.status_code == 429 or _is_banned:
            _pushover_consecutive_429 += 1
            log.warning(
                "[BACKOFF] 429/banned received (%d/%d)",
                _pushover_consecutive_429, PUSHOVER_429_MAX_CONSECUTIVE,
            )
            if _pushover_consecutive_429 >= PUSHOVER_429_MAX_CONSECUTIVE:
                _pushover_backoff_until = time.time() + PUSHOVER_BACKOFF_DURATION_SEC
                log.warning(
                    "[BACKOFF] entering 30min silence until %.0f", _pushover_backoff_until
                )
                _save_backoff_state()
                _queue_pushover(title, message, priority)
        elif resp.ok:
            if _pushover_consecutive_429 > 0:
                _pushover_consecutive_429 = 0
                _pushover_backoff_until = 0.0
                _save_backoff_state()
        else:
            log.warning("[NOTIFY_ERR] HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("[NOTIFY_ERR] %s", e)


# ── 自己回復ヘルパー ──────────────────────────────────────────────────────────
def _load_recovery_state() -> dict:
    """data/atlas_watchdog_recovery_state.json を読む。"""
    try:
        if RECOVERY_STATE_PATH.exists():
            return json.loads(RECOVERY_STATE_PATH.read_text())
    except Exception as e:
        log.warning("[RECOVERY] state load error: %s", e)
    return {"attempt": 0, "last_attempt_ts": 0.0, "recovered": False}


def _save_recovery_state(state: dict) -> None:
    try:
        RECOVERY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.warning("[RECOVERY] state save error: %s", e)


def _reset_recovery_state() -> None:
    """回復成功 or 通常状態に戻ったときに state をクリアする。"""
    state = {"attempt": 0, "last_attempt_ts": 0.0, "recovered": True}
    _save_recovery_state(state)
    log.info("[RECOVERY] state reset (recovered=True)")


def _attempt_self_recovery(issue_label: str) -> None:
    """更新停止を検知したとき、3段階の自己回復を試みる。

    attempt=1: launchctl kickstart <service>
    attempt=2: launchctl bootstrap 再登録
    attempt>=3: 人間介入要求 (priority=2)

    クールダウン: 10分。回復成功後は _reset_recovery_state() を呼ぶこと。
    """
    state = _load_recovery_state()
    now = time.time()

    elapsed = now - state.get("last_attempt_ts", 0.0)
    if state.get("attempt", 0) > 0 and elapsed < RECOVERY_COOLDOWN_SEC:
        log.info(
            "[RECOVERY] cooldown active (%.0f/%.0fs elapsed)",
            elapsed, RECOVERY_COOLDOWN_SEC,
        )
        return

    attempt = state.get("attempt", 0) + 1
    state["attempt"] = attempt
    state["last_attempt_ts"] = now
    state["recovered"] = False
    _save_recovery_state(state)

    if attempt == 1:
        log.warning("[RECOVERY] attempt=%d: launchctl kickstart %s", attempt, LAUNCHCTL_SERVICE_ID)
        try:
            result = subprocess.run(
                ["launchctl", "kickstart", "-k",
                 f"gui/{os.getuid()}/{LAUNCHCTL_SERVICE_ID}"],
                capture_output=True, text=True, timeout=30, shell=False,
            )
            log.info(
                "[RECOVERY] kickstart returncode=%d stdout=%s stderr=%s",
                result.returncode, result.stdout.strip(), result.stderr.strip(),
            )
            pushover_send(
                "[Atlas/Watchdog] 自己回復 attempt=1",
                (
                    f"更新停止({issue_label})検知\n"
                    f"launchctl kickstart 実行\n"
                    f"returncode={result.returncode}"
                ),
                priority=1,
            )
        except Exception as e:
            log.warning("[RECOVERY] kickstart error: %s", e)

    elif attempt == 2:
        log.warning(
            "[RECOVERY] attempt=%d: launchctl bootstrap 再登録 %s", attempt, LAUNCHCTL_SERVICE_ID
        )
        plist_path = LAUNCHCTL_PLIST_PATH
        try:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
                capture_output=True, text=True, timeout=30, shell=False,
            )
            result = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                capture_output=True, text=True, timeout=30, shell=False,
            )
            log.info(
                "[RECOVERY] bootstrap returncode=%d stdout=%s stderr=%s plist=%s",
                result.returncode, result.stdout.strip(), result.stderr.strip(), plist_path,
            )
            pushover_send(
                "[Atlas/Watchdog] 自己回復 attempt=2",
                (
                    f"更新停止({issue_label})継続\n"
                    f"launchctl bootstrap 再登録実行\n"
                    f"returncode={result.returncode}"
                ),
                priority=1,
            )
        except Exception as e:
            log.warning("[RECOVERY] bootstrap error: %s", e)

    else:
        log.error(
            "[RECOVERY] attempt=%d: 回復不可 — 人間介入要求 service=%s",
            attempt, LAUNCHCTL_SERVICE_ID,
        )
        pushover_send(
            "[Atlas/Watchdog] 回復不可・人間介入要",
            (
                f"更新停止({issue_label})が {attempt} 回回復試行後も継続\n"
                f"service: {LAUNCHCTL_SERVICE_ID}\n"
                f"手動での確認・再起動が必要です"
            ),
            priority=2,
        )


# ── ログ tail ─────────────────────────────────────────────────────────────────
def tail_new_lines(filepath: str, last_pos: int) -> tuple[list[str], int]:
    """ファイルの追記分だけ読む。ログローテーション検知対応。"""
    try:
        size = os.path.getsize(filepath)
    except FileNotFoundError:
        return [], last_pos

    if size < last_pos:
        log.info("[TAIL] log rotated, resetting position: %s", filepath)
        last_pos = 0

    if size == last_pos:
        return [], last_pos

    with open(filepath, "r", errors="replace") as f:
        f.seek(last_pos)
        new_lines = f.readlines()
        new_pos = f.tell()

    return new_lines, new_pos


# ── パターン検知 ──────────────────────────────────────────────────────────────
def check_patterns(lines: list[str]) -> None:
    now = time.time()

    for line in lines:
        line = line.rstrip()
        for pattern, label in ALERT_PATTERNS:
            if pattern.search(line):
                q = pattern_times[label]
                q.append((now, line))

                while q and now - q[0][0] > WINDOW_SECONDS:
                    q.popleft()

                count = len(q)
                log.info("Pattern [%s] detected (%d/%d): %s", label, count, THRESHOLD, line[:120])

                if count >= THRESHOLD:
                    last_sent = last_alert_sent.get(label, 0)
                    if now - last_sent > ALERT_COOLDOWN:
                        last_alert_sent[label] = now
                        recent = [entry[1] for entry in list(q)[-5:]]
                        excerpt = "\n".join(recent)
                        msg = (
                            f"パターン: {label}\n"
                            f"5分以内に {count} 件検知\n\n"
                            f"直近ログ:\n{excerpt[:800]}"
                        )
                        pushover_send(f"[Atlas/Watchdog] {label}", msg, priority=1)


# ── Health Check ─────────────────────────────────────────────────────────────
def run_health_check(watch_paths: list[str]) -> None:
    """5分毎のヘルスチェック: 監視対象ファイルの存在とサイズを確認する。

    WATCH_TARGETS に定義された watch_windows_jst で時間帯ゲートを実施する。
    窓外の場合は skip（alert なし・recovery 試行なし）。
    recovery attempt=1→kickstart, attempt=2→bootstrap, attempt>=3→人間介入要求。
    """
    log.info("[HealthCheck] 開始: %d ファイル監視中", len(watch_paths))
    issues: list[str] = []
    stale_detected = False

    # path → WATCH_TARGETS エントリのマップを構築
    target_map: dict[str, dict] = {
        str(t["path"]): t for t in WATCH_TARGETS
    }

    for filepath in watch_paths:
        p = Path(filepath)
        target = target_map.get(str(p))
        windows_jst = target["watch_windows_jst"] if target else [("00:00", "23:59")]

        if not _is_in_watch_window(windows_jst):
            now_jst = datetime.now(tz=JST).strftime("%H:%M JST")
            log.info(
                "[HealthCheck] SKIP (窓外 %s): %s windows=%s",
                now_jst, p.name, windows_jst,
            )
            continue

        if not p.exists():
            issues.append(f"不存在: {p.name}")
            continue
        try:
            size  = p.stat().st_size
            mtime = p.stat().st_mtime
            age   = time.time() - mtime
            if age > 600:  # 10分以上更新なし
                label = f"更新停止({age:.0f}秒): {p.name}"
                issues.append(label)
                stale_detected = True
                _attempt_self_recovery(label)
            else:
                log.info("[HealthCheck] OK: %s size=%dB age=%.0fs", p.name, size, age)
        except Exception as e:
            issues.append(f"stat失敗 {p.name}: {e}")

    non_stale_issues = [i for i in issues if "更新停止" not in i]
    if non_stale_issues:
        msg = "ヘルスチェック異常:\n" + "\n".join(non_stale_issues)
        log.warning("[HealthCheck] %s", msg)
        global _last_health_alert
        _now = time.time()
        if _now - _last_health_alert >= HEALTH_ALERT_COOLDOWN_SEC:
            pushover_send("[Atlas/Watchdog] HealthCheck異常", msg, priority=1)
            _last_health_alert = _now
        else:
            log.info(
                "[HealthCheck] 通知スキップ(cooldown中 残%.0fs)",
                HEALTH_ALERT_COOLDOWN_SEC - (_now - _last_health_alert),
            )
    elif not stale_detected:
        _reset_recovery_state()
        log.info("[HealthCheck] 完了: 全ファイル正常")


# ── メインループ ──────────────────────────────────────────────────────────────
def main():
    log.info("=== atlas_watchdog started ===")
    log.info("監視対象: %s", LOG_PATH)
    log.info("チェック間隔: %d秒 / 閾値: %d秒で%d件", CHECK_INTERVAL, WINDOW_SECONDS, THRESHOLD)

    _load_backoff_state()

    # 監視対象ファイルリスト（WATCH_TARGETS から構築）
    watch_paths = [str(t["path"]) for t in WATCH_TARGETS]

    # 初回は現在のファイル末尾位置から開始（過去ログはスキップ）
    try:
        last_pos = os.path.getsize(str(LOG_PATH))
        log.info("初期ファイルサイズ: %d bytes (過去ログスキップ)", last_pos)
    except FileNotFoundError:
        last_pos = 0
        log.info("ログファイル未存在。作成を待機: %s", LOG_PATH)

    pushover_send(
        "[Atlas/Watchdog] 起動",
        (
            f"atlas_watchdog.py 起動完了\n"
            f"監視: {', '.join(watch_paths)}\n"
            f"チェック間隔: {CHECK_INTERVAL}秒 / 閾値: {WINDOW_SECONDS}秒/{THRESHOLD}件\n"
            f"自己回復: com.atlas.agent"
        ),
        priority=0,
    )

    global _last_health_check
    _last_health_check = time.time()

    # Heartbeat pulse（1分毎）
    _last_pulse = 0.0
    _PULSE_INTERVAL = 60

    while True:
        try:
            new_lines, last_pos = tail_new_lines(str(LOG_PATH), last_pos)
            if new_lines:
                check_patterns(new_lines)

            now = time.time()
            if now - _last_health_check >= HEALTH_CHECK_INTERVAL_SEC:
                run_health_check(watch_paths)
                _last_health_check = now

            # 能動 heartbeat pulse（1分毎）
            if now - _last_pulse >= _PULSE_INTERVAL:
                _write_pulse("atlas_watchdog", state="healthy", details={"watching": str(LOG_PATH)})
                _last_pulse = now

            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            break
        except Exception as e:
            _write_pulse("atlas_watchdog", state="degraded", details={"error": str(e)})
            time.sleep(10)


if __name__ == "__main__":
    main()
