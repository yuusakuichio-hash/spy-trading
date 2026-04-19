#!/usr/bin/env python3
"""
chronos_watchdog.py — プロセス監視（最後の砦）(Sora Lab / Chronos)

役割:
  - chronos_bot.py / chronos_agent.py ログの新規行を tail
  - エラーパターン検知（Exception/Traceback/CRITICAL等）
  - 閾値超過時に Pushover priority=1 で即通知
  - 5分毎 health check（ファイルサイズ・プロセス確認）
  - atlas_watchdog.py の Chronos 版ミラー

設計方針:
  - シンプル・軽量・「最後の砦」として常に動作
  - 高機能自律対応は chronos_agent.py が担当
  - 役割分担: fleet_watcher=合算DD/hedging・agent=Bot生存・watchdog=ログパターン

依存: requests, stdlib
起動: LaunchAgent com.chronos.watchdog（Disabled=true・手動loadで有効化）
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
from datetime import datetime
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

CHRONOS_LOG_PATH = Path(
    os.environ.get("CHRONOS_LOG_DIR", str(BASE_DIR / "data" / "logs"))
) / "chronos.log"

WATCHDOG_LOG_PATH = Path(
    os.environ.get("CHRONOS_LOG_DIR", str(BASE_DIR / "data" / "logs"))
) / "chronos_watchdog.log"

HEALTH_CHECK_INTERVAL_SEC = 300   # 5分毎 health check
CHECK_INTERVAL_SEC = 10            # ログ tail チェック間隔（秒）

PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_OPS_TOKEN", os.environ.get("PUSHOVER_TOKEN", ""))

# ── 自己回復設定 ──────────────────────────────────────────────────────────────
RECOVERY_STATE_PATH = BASE_DIR / "data" / "chronos_watchdog_recovery_state.json"
RECOVERY_COOLDOWN_SEC = 600          # 試行間隔 10分
RECOVERY_MAX_ATTEMPTS = 3            # 3回失敗で人間介入要求
# 実在サービス名: launchctl list | grep soralab で確認済み (2026-04-20)
# 旧値 "com.chronos.agent" は実在しない → 自己回復が100%失敗していた
LAUNCHCTL_SERVICE_ID = "com.soralab.chronos_agent"
LAUNCHCTL_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / "com.soralab.chronos_agent.plist"
)

# ── Pushover backoff 設定 ─────────────────────────────────────────────────────
PUSHOVER_BACKOFF_STATE_PATH = BASE_DIR / "data" / "pushover_backoff_state.json"
PUSHOVER_QUEUE_PATH         = BASE_DIR / "data" / "pushover_queue.jsonl"
PUSHOVER_429_MAX_CONSECUTIVE = 3     # 連続この回数で沈黙
PUSHOVER_BACKOFF_DURATION_SEC = 1800 # 30分

# ── モジュールレベル backoff 状態 ─────────────────────────────────────────────
_pushover_consecutive_429: int = 0
_pushover_backoff_until: float = 0.0

# ── アラートパターン定義 ─────────────────────────────────────────────────────
# (pattern, label, priority)
ALERT_PATTERNS: list[tuple[re.Pattern, str, int]] = [
    # 即時priority=2（CRITICAL）
    (re.compile(r"\bCRITICAL\b", re.IGNORECASE),                       "CRITICAL",             2),
    # priority=1（ERROR/違反）
    (re.compile(r"\bTraceback\b",                re.IGNORECASE),        "Traceback",            1),
    (re.compile(r"Exception.*Error|Error.*Exception", re.IGNORECASE),   "Exception",            1),
    (re.compile(r"\bERROR\b",                    re.IGNORECASE),        "ERROR",                1),
    (re.compile(r"margin.*違反|margin.*violation", re.IGNORECASE),      "margin違反",           1),
    (re.compile(r"safety.?buffer.*違反|safety.?buffer.*breach", re.IGNORECASE), "MFFU_Safety_Buffer", 1),
    (re.compile(r"consistency.*違反|consistency.*breach", re.IGNORECASE), "MFFU_Consistency",   1),
    (re.compile(r"daily.?loss.*limit|max.?loss.*limit", re.IGNORECASE),  "MFFU_MaxLoss",       1),
    (re.compile(r"news.*window.*violation|T1.*violation", re.IGNORECASE), "MFFU_NewsWindow",    2),
    (re.compile(r"hft.*violation|trade.*200.*day", re.IGNORECASE),        "MFFU_HFT",           1),
    (re.compile(r"kill.?switch.*activated|kill.*switch.*active", re.IGNORECASE), "KillSwitch", 1),
    (re.compile(r"Tradovate.*disconnect|connection.*lost|connection.*refused", re.IGNORECASE), "TradovateDisconnect", 1),
    # priority=0（警告）
    (re.compile(r"\bWARNING\b",                  re.IGNORECASE),        "WARNING",              0),
]

# ウィンドウ・閾値設定（パターン別）
_PATTERN_CONFIG: dict[str, dict[str, int]] = {
    "CRITICAL":          {"window_sec": 60,  "threshold": 1,  "cooldown_sec": 60},
    "MFFU_NewsWindow":   {"window_sec": 60,  "threshold": 1,  "cooldown_sec": 60},
    "KillSwitch":        {"window_sec": 60,  "threshold": 1,  "cooldown_sec": 120},
    "TradovateDisconnect": {"window_sec": 120, "threshold": 3, "cooldown_sec": 300},
    "Traceback":         {"window_sec": 300, "threshold": 1,  "cooldown_sec": 120},
    "Exception":         {"window_sec": 300, "threshold": 3,  "cooldown_sec": 300},
    "MFFU_Safety_Buffer":{"window_sec": 300, "threshold": 1,  "cooldown_sec": 300},
    "MFFU_Consistency":  {"window_sec": 300, "threshold": 1,  "cooldown_sec": 300},
    "MFFU_MaxLoss":      {"window_sec": 300, "threshold": 1,  "cooldown_sec": 300},
    "MFFU_HFT":          {"window_sec": 300, "threshold": 1,  "cooldown_sec": 300},
    "margin違反":         {"window_sec": 300, "threshold": 1,  "cooldown_sec": 300},
    "ERROR":             {"window_sec": 300, "threshold": 10, "cooldown_sec": 60},
    "WARNING":           {"window_sec": 300, "threshold": 10, "cooldown_sec": 60},
}
_DEFAULT_PATTERN_CFG = {"window_sec": 300, "threshold": 10, "cooldown_sec": 60}

# ── ロギング設定 ─────────────────────────────────────────────────────────────
WATCHDOG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("chronos_watchdog")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(WATCHDOG_LOG_PATH), encoding="utf-8"),
    ],
)

# ── パターン別タイムスタンプキュー（状態保持） ───────────────────────────────
pattern_times: dict[str, deque] = defaultdict(deque)
last_alert_sent: dict[str, float] = {}

# 監視対象ログのポジション（ファイル名→バイト位置）
log_positions: dict[str, int] = {}
log_inodes: dict[str, int] = {}

# 最後の health check 時刻
_last_health_check: float = 0.0

# HealthCheck Pushover 最終送信時刻（ban防止 1h cooldown）
_last_health_alert: float = 0.0
HEALTH_ALERT_COOLDOWN_SEC = 3600  # 1時間に1回まで


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


# ── Pushover（[Chronos]タグ付き） ────────────────────────────────────────────
def pushover_send(title: str, message: str, priority: int = 1) -> None:
    """Pushover通知を送信する。[Chronos/Watchdog]タグを強制付与する。

    common.pushover_client 経由で送信することで backoff/queue を全スクリプト間で
    共有し、429 連鎖 ban を防止する（SPOF解消）。
    project_pushover_tag_convention.md のタグ規約（[Chronos]プレフィックス）に準拠。
    """
    if not title.startswith("["):
        title = f"[Chronos/Watchdog] {title}"

    if _PC_AVAILABLE:
        _pc.send(
            title,
            message,
            priority=priority,
            token=PUSHOVER_TOKEN or None,
            app_tag="Chronos/Watchdog",
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
            data["retry"] = 30
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
    """data/chronos_watchdog_recovery_state.json を読む。"""
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

    # クールダウン中はスキップ（1回目以降のみ）
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
                "[Chronos/Watchdog] 自己回復 attempt=1",
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
            # bootstrap 前に bootout（存在しない場合は無視）
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
                capture_output=True, text=True, timeout=30, shell=False,
            )
            result = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                capture_output=True, text=True, timeout=30, shell=False,
            )
            log.info(
                "[RECOVERY] bootstrap returncode=%d stdout=%s stderr=%s",
                result.returncode, result.stdout.strip(), result.stderr.strip(),
            )
            pushover_send(
                "[Chronos/Watchdog] 自己回復 attempt=2",
                (
                    f"更新停止({issue_label})継続\n"
                    f"launchctl bootstrap 再登録実行\n"
                    f"plist={plist_path}\n"
                    f"returncode={result.returncode}"
                ),
                priority=1,
            )
        except Exception as e:
            log.warning("[RECOVERY] bootstrap error: %s", e)

    else:
        # 3回目以降: 人間介入要求
        log.error(
            "[RECOVERY] attempt=%d: 回復不可 — 人間介入要求 service=%s",
            attempt, LAUNCHCTL_SERVICE_ID,
        )
        pushover_send(
            "[Chronos/Watchdog] 回復不可・人間介入要",
            (
                f"更新停止({issue_label})が {attempt} 回回復試行後も継続\n"
                f"service: {LAUNCHCTL_SERVICE_ID}\n"
                f"手動での確認・再起動が必要です"
            ),
            priority=2,
        )


# ── ログ tail ─────────────────────────────────────────────────────────────────
def tail_new_lines(log_path: Path, last_pos: int) -> tuple[list[str], int]:
    """ログファイルの新規行を読み取る。ローテーション検知対応。

    atlas_watchdog.py の tail_new_lines() を移植・強化。

    Returns:
        (新規行のリスト, 次回の読み取り開始バイト位置)
    """
    path_str = str(log_path)
    if not log_path.exists():
        return [], last_pos

    try:
        st = log_path.stat()
        current_size = st.st_size
        current_inode = st.st_ino

        # ローテーション検知（inode変更またはサイズ縮小）
        prev_inode = log_inodes.get(path_str)
        if prev_inode is not None and (current_inode != prev_inode or current_size < last_pos):
            log.info("[TAIL] log rotated: %s", log_path.name)
            last_pos = 0

        log_inodes[path_str] = current_inode

        if current_size <= last_pos:
            return [], last_pos

        read_size = min(current_size - last_pos, 65536)  # 最大64KB/サイクル
        with log_path.open("rb") as f:
            f.seek(last_pos)
            data = f.read(read_size)

        new_pos = last_pos + len(data)

        # 末尾の不完全行は次サイクルに繰り越し
        text = data.decode("utf-8", errors="replace")
        if "\n" in text:
            last_nl = text.rfind("\n")
            new_pos = last_pos + len(text[:last_nl + 1].encode("utf-8", errors="replace"))
            text = text[:last_nl + 1]
        else:
            # 改行なし = 不完全行のみ → 次サイクル
            return [], last_pos

        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines, new_pos

    except Exception as e:
        log.warning("[TAIL_ERR] %s: %s", log_path, e)
        return [], last_pos


def check_pattern(line: str, now: float) -> list[tuple[str, int]]:
    """1行に対してアラートパターンをマッチし、閾値超過したパターンのリストを返す。

    atlas_watchdog.py の check_patterns() を関数型に移植。

    Returns:
        [(pattern_label, priority), ...] — 閾値超過したパターンのみ
    """
    triggered: list[tuple[str, int]] = []

    for regex, label, priority in ALERT_PATTERNS:
        if not regex.search(line):
            continue

        q = pattern_times[label]
        q.append(now)

        pcfg = _PATTERN_CONFIG.get(label, _DEFAULT_PATTERN_CFG)
        window_sec = pcfg["window_sec"]
        threshold = pcfg["threshold"]
        cooldown_sec = pcfg["cooldown_sec"]

        # ウィンドウ外のエントリを削除
        while q and now - q[0] > window_sec:
            q.popleft()

        count = len(q)

        if count >= threshold:
            last_sent = last_alert_sent.get(label, 0.0)
            if now - last_sent >= cooldown_sec:
                last_alert_sent[label] = now
                triggered.append((label, priority))
                q.clear()  # 通知後はキューリセット（重複抑制）

    return triggered


# ── Health Check ─────────────────────────────────────────────────────────────
def run_health_check(watch_paths: list[Path]) -> None:
    """5分毎のヘルスチェック: 監視対象ファイルの存在とサイズを確認する。

    更新停止を検知したとき、通知の前に自己回復を試みる。
    recovery attempt=1→kickstart, attempt=2→bootstrap, attempt>=3→人間介入要求。
    """
    log.info("[HealthCheck] 開始: %d ファイル監視中", len(watch_paths))
    issues: list[str] = []
    stale_detected = False

    for p in watch_paths:
        if not p.exists():
            issues.append(f"不存在: {p.name}")
            continue
        try:
            size = p.stat().st_size
            mtime = p.stat().st_mtime
            age = time.time() - mtime
            if age > 600:  # 10分以上更新なし
                label = f"更新停止({age:.0f}秒): {p.name}"
                issues.append(label)
                stale_detected = True
                # 自己回復パスを先に実行し、通知は attempt>=3 のときだけ watchdog が送る
                _attempt_self_recovery(label)
            else:
                log.info("[HealthCheck] OK: %s size=%dB age=%.0fs", p.name, size, age)
        except Exception as e:
            issues.append(f"stat失敗 {p.name}: {e}")

    # 更新停止以外の問題（不存在・stat失敗）は即通知
    non_stale_issues = [i for i in issues if "更新停止" not in i]
    if non_stale_issues:
        msg = "ヘルスチェック異常:\n" + "\n".join(non_stale_issues)
        log.warning("[HealthCheck] %s", msg)
        global _last_health_alert
        _now = time.time()
        if _now - _last_health_alert >= HEALTH_ALERT_COOLDOWN_SEC:
            pushover_send("[Chronos/Watchdog] HealthCheck異常", msg, priority=1)
            _last_health_alert = _now
        else:
            log.info(
                "[HealthCheck] 通知スキップ(cooldown中 残%.0fs)",
                HEALTH_ALERT_COOLDOWN_SEC - (_now - _last_health_alert),
            )
    elif not stale_detected:
        # 全ファイル正常 → recovery state をリセット
        _reset_recovery_state()
        log.info("[HealthCheck] 完了: 全ファイル正常")


# ── メインループ ──────────────────────────────────────────────────────────────
def run() -> None:
    """Watchdogメインループ。"""
    log.info("[Chronos Watchdog] 起動: %s 監視開始", CHRONOS_LOG_PATH)

    # backoff 状態を起動時に復元
    _load_backoff_state()

    # 監視対象ファイルリスト
    watch_paths: list[Path] = [
        CHRONOS_LOG_PATH,
        BASE_DIR / "data" / "logs" / "chronos_agent.log",
    ]

    # 初期ポジション設定（起動前ログをスキップ）
    for p in watch_paths:
        path_str = str(p)
        if p.exists():
            try:
                st = p.stat()
                log_positions[path_str] = st.st_size
                log_inodes[path_str] = st.st_ino
                log.info("[Watchdog] 初期位置: %s %dB", p.name, st.st_size)
            except Exception:
                log_positions[path_str] = 0
        else:
            log_positions[path_str] = 0
            log.info("[Watchdog] ファイル未存在（待機中）: %s", p)

    # 起動通知
    pushover_send(
        "[Chronos/Watchdog] 起動",
        (
            f"chronos_watchdog.py 起動完了\n"
            f"監視: {', '.join(p.name for p in watch_paths)}\n"
            f"チェック間隔: {CHECK_INTERVAL_SEC}秒\n"
            f"HealthCheck: {HEALTH_CHECK_INTERVAL_SEC}秒毎"
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
            now = time.time()

            # 各ログファイルを tail して新規行を取得
            for p in watch_paths:
                path_str = str(p)
                last_pos = log_positions.get(path_str, 0)
                new_lines, new_pos = tail_new_lines(p, last_pos)
                log_positions[path_str] = new_pos

                for line in new_lines:
                    triggered = check_pattern(line, now)
                    for label, priority in triggered:
                        log.warning(
                            "[Watchdog] pattern=%s priority=%d line=%s",
                            label, priority, line[:120],
                        )
                        excerpt = line[:400]
                        pushover_send(
                            f"[Chronos/Watchdog] {label}",
                            f"パターン検知: {label}\n\nログ:\n{excerpt}",
                            priority=priority,
                        )

            # 5分毎 Health Check
            if now - _last_health_check >= HEALTH_CHECK_INTERVAL_SEC:
                run_health_check(watch_paths)
                _last_health_check = now

            # 能動 heartbeat pulse（1分毎）
            if now - _last_pulse >= _PULSE_INTERVAL:
                _write_pulse("chronos_watchdog", state="healthy", details={"watching": len(watch_paths)})
                _last_pulse = now

            time.sleep(CHECK_INTERVAL_SEC)

        except KeyboardInterrupt:
            log.info("[Chronos Watchdog] KeyboardInterrupt → 終了")
            break
        except Exception as e:
            log.error("[WATCHDOG_ERR] %s", e)
            _write_pulse("chronos_watchdog", state="degraded", details={"error": str(e)})
            time.sleep(10)


if __name__ == "__main__":
    run()
