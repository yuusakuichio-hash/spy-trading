#!/usr/bin/env python3
"""
chronos_agent.py — 常駐監視・自己回復エージェント (Sora Lab / Chronos)

役割:
  - chronos_bot.py プロセスの生死監視
  - MFFU規約違反検知（Level3/4）
  - Pushover通知 ([Chronos]タグ)
  - 異常検知 → 自律対応 → ログ記録
  - Atlas の atlas_agent.py と同じ Level 1-4 設計

Level:
  1 INFO   : 通知のみ
  2 AUTOFIX: Bot再起動 (DRY_RUN対応)
  3 ALERT  : Bot即停止 + priority=1 通知
  4 HALT   : 手動待ち + MFFU致命的違反

設計方針:
  - atlas_agent.py の設計をChronos用にミラーリング
  - Atlasと独立したプロセスで常駐
  - fleet_watcher: 合算DD/hedging担当（役割分担）
  - chronos_agent: Bot生存・MFFU規約・P&L監視
  - chronos_watchdog: ログパターン検知（最後の砦）

依存: PyYAML, requests, stdlib
起動: LaunchAgent com.soralab.chronos_agent（Disabled=true・手動loadで有効化）

CLI:
  --selftest   : config読み込みのみ確認して終了
  --once       : 1サイクルだけ回して終了
  --dry-run    : autofixをDRY_RUNに
  --armed      : autofixをARMEDに
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import subprocess
import sys
import time
import zoneinfo
from pathlib import Path
from typing import Any

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

# ── Atlas共通基盤（再利用） ──────────────────────────────────────────────────
try:
    from common.kill_switch import is_active as kill_switch_is_active, reason as kill_switch_reason
    from common import kill_switch
    KILL_SWITCH_AVAILABLE = True
except ImportError:
    KILL_SWITCH_AVAILABLE = False
    def kill_switch_is_active() -> bool:
        return False
    def kill_switch_reason() -> str:
        return ""

# ── PyYAML ───────────────────────────────────────────────────────────────────
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# ── ロギング設定 ─────────────────────────────────────────────────────────────
_LOG_DIR = Path(os.environ.get("CHRONOS_LOG_DIR", "data/logs"))
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _LOG_DIR / "chronos_agent.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("chronos_agent")

# ── 定数 ─────────────────────────────────────────────────────────────────────
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
ET  = zoneinfo.ZoneInfo("America/New_York")

BASE_DIR = Path(__file__).parent.resolve()
CHRONOS_RULES_PATH = BASE_DIR / "chronos_rules.yaml"

# 環境変数
PUSHOVER_USER        = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_OPS_TOKEN   = os.environ.get("PUSHOVER_OPS_TOKEN", "")
PUSHOVER_ALERT_TOKEN = os.environ.get("PUSHOVER_ALERT_TOKEN", PUSHOVER_OPS_TOKEN)

# デフォルト設定（YAML読み込み失敗時のフォールバック）
_DEFAULT_CFG: dict[str, Any] = {
    "bot_launchagent": "com.soralab.chronos_bot",
    "pid_files": ["data/logs/chronos_bot.pid"],
    "stale_log_sec": 180,
    "cycle_interval_sec": 60,
    "log_sources": {
        "chronos": "data/logs/chronos.log",
        "chronos_agent": "data/logs/chronos_agent.log",
    },
    "market_window": {"start": "22:25", "end": "05:05"},
    "mffu": {
        "max_loss_usd": 2000.0,
        "consistency_max_pct": 0.50,
        "profit_target_usd": 3000.0,
        "hft_daily_max_trades": 200,
        "builder_daily_loss_usd": 1000.0,
        "payout": {
            "min_winning_days": 5,
            "min_net_profit_usd": 500.0,
        },
    },
}

# state.json エージェント用パス
AGENT_STATE_PATH = BASE_DIR / "data" / "chronos_agent_state.json"

# アカウントディレクトリ
ACCOUNTS_DIR = BASE_DIR / "data" / "accounts"

# 既通知フラグ（重複通知抑制）
_notified: dict[str, float] = {}
_NOTIFY_COOLDOWN_SEC = 300  # 同一アラートを5分以内に再送しない


# ── Pushover（[Chronos]タグ付き） ────────────────────────────────────────────
def pushover(title: str, message: str, priority: int = 0) -> bool:
    """Pushover通知を送信する。[Chronos]タグを強制付与する。

    project_pushover_tag_convention.md のタグ規約（[Chronos]プレフィックス）に準拠。
    """
    if not title.startswith("[Chronos"):
        title = f"[Chronos] {title}"
    tok = PUSHOVER_OPS_TOKEN or PUSHOVER_ALERT_TOKEN
    if not tok or not PUSHOVER_USER:
        log.warning("[NOTIFY_SKIP] missing token/user. title=%s", title)
        return False
    try:
        import requests as _req
        data: dict[str, Any] = {
            "token": tok,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message[:1024],
            "priority": priority,
        }
        if priority >= 2:
            data["retry"] = 30
            data["expire"] = 3600
        r = _req.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("[NOTIFY_ERR] status=%s body=%s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("[NOTIFY_ERR] %s", e)
        return False


def pushover_alert(title: str, message: str, priority: int = 1) -> bool:
    """Pushover緊急通知を送信する。ALERTトークン使用。"""
    if not title.startswith("[Chronos"):
        title = f"[Chronos/ALERT] {title}"
    tok = PUSHOVER_ALERT_TOKEN or PUSHOVER_OPS_TOKEN
    if not tok or not PUSHOVER_USER:
        log.warning("[NOTIFY_SKIP] missing token/user. title=%s", title)
        return False
    try:
        import requests as _req
        data: dict[str, Any] = {
            "token": tok,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message[:1024],
            "priority": priority,
        }
        if priority >= 2:
            data["retry"] = 30
            data["expire"] = 3600
        r = _req.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning("[NOTIFY_ERR] %s", e)
        return False


def _should_notify(key: str) -> bool:
    """同一キーの通知が cooldown 内ならFalse"""
    now = time.time()
    last = _notified.get(key, 0.0)
    if now - last < _NOTIFY_COOLDOWN_SEC:
        return False
    _notified[key] = now
    return True


# ── YAML設定ロード ────────────────────────────────────────────────────────────
def load_config() -> dict[str, Any]:
    """chronos_rules.yaml から agent セクションを読み込む。失敗時はデフォルト値を返す。"""
    if not YAML_AVAILABLE:
        log.warning("[Config] PyYAML unavailable. using default config.")
        return _DEFAULT_CFG.copy()
    if not CHRONOS_RULES_PATH.exists():
        log.warning("[Config] %s not found. using default config.", CHRONOS_RULES_PATH)
        return _DEFAULT_CFG.copy()
    try:
        raw = yaml.safe_load(CHRONOS_RULES_PATH.read_text(encoding="utf-8"))
        agent_cfg: dict[str, Any] = raw.get("agent", {})
        mffu_cfg: dict[str, Any] = raw.get("mffu_compliance", {})

        # agent セクションをフラット化してデフォルトにマージ
        cfg = _DEFAULT_CFG.copy()
        cfg.update(agent_cfg)

        # MFFU制約値をマージ
        evaluation = mffu_cfg.get("evaluation", {})
        sim_funded  = mffu_cfg.get("sim_funded", {})
        payout_cfg  = mffu_cfg.get("payout", {})
        phase       = mffu_cfg.get("phase", "evaluation")
        rule_src    = evaluation if phase == "evaluation" else sim_funded

        cfg["mffu"] = {
            "phase": phase,
            "max_loss_usd": float(rule_src.get("max_loss_limit_usd", 2000.0)),
            "consistency_max_pct": float(rule_src.get("consistency_max_pct", 0.50)),
            "profit_target_usd": float(evaluation.get("profit_target_usd", 3000.0)),
            "hft_daily_max_trades": 200,   # MFFU公式: 200トレード/日でHFTとみなす
            "builder_daily_loss_usd": 1000.0,  # Builder Daily Loss警告ライン
            "payout": {
                "min_winning_days": int(payout_cfg.get("min_winning_days", 5)),
                "min_net_profit_usd": float(payout_cfg.get("min_net_profit_usd", 500.0)),
            },
            "survival_mode": raw.get("survival_mode_after_payout", {}),
        }
        return cfg
    except Exception as e:
        log.warning("[Config] YAML parse error: %s. using default.", e)
        return _DEFAULT_CFG.copy()


# ── プロセス監視 ─────────────────────────────────────────────────────────────
def is_bot_alive_for_account(account_id: str) -> bool:
    """特定アカウントの chronos_bot.py プロセスが生存しているか確認する。

    cycle2 BUG-4: pgrep -f で5アカ全てが同一パターンになる問題を解消。
    data/accounts/<account_id>/pid.lock を読み、os.kill(pid, 0) で生存確認する。

    Args:
        account_id: アカウントID（MFFU_ACCOUNT_ID の値）

    Returns:
        True = 生存, False = 死亡 or PIDファイルなし
    """
    pid_lock = ACCOUNTS_DIR / account_id / "pid.lock"
    if not pid_lock.exists():
        return False
    try:
        pid = int(pid_lock.read_text().strip())
        os.kill(pid, 0)  # signal 0: プロセス存在確認（シグナルは送らない）
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def is_bot_alive(cfg: dict[str, Any]) -> bool:
    """chronos_bot.py プロセスが生存しているか確認する。

    優先順:
    1. data/accounts/*/pid.lock (cycle2 BUG-4: アカウント別 PID ファイル)
    2. cfg["pid_files"] リスト + kill -0
    3. pgrep -f "chronos_bot.py" (フォールバック)
    """
    # 1. アカウント別 PID ファイル経由チェック（BUG-4 厳密化）
    if ACCOUNTS_DIR.exists():
        for pid_lock in ACCOUNTS_DIR.glob("*/pid.lock"):
            account_id = pid_lock.parent.name
            if is_bot_alive_for_account(account_id):
                return True

    pid_files: list[str] = cfg.get("pid_files", _DEFAULT_CFG["pid_files"])

    # 2. cfg 指定 PIDファイル経由チェック
    for pid_file_str in pid_files:
        pp = Path(pid_file_str)
        if not pp.is_absolute():
            pp = BASE_DIR / pp
        if not pp.exists():
            continue
        try:
            pid = int(pp.read_text().strip())
            result = subprocess.run(
                ["kill", "-0", str(pid)],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
        except (ValueError, subprocess.TimeoutExpired, OSError):
            continue

    # 3. pgrep フォールバック（精度向上: 完全パス一致で絞り込む）
    try:
        result = subprocess.run(
            ["pgrep", "-f", "chronos_bot.py"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def restart_bot(cfg: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """chronos_bot.py を LaunchAgent 経由で再起動する。

    atlas_agent.py の action_restart_bot() と同じ設計。
    dry_run=True の場合はログのみ。
    """
    label = cfg.get("bot_launchagent", _DEFAULT_CFG["bot_launchagent"])
    action: dict[str, Any] = {
        "type": "restart_bot",
        "label": label,
        "dry_run": dry_run,
    }
    if dry_run:
        action["status"] = "DRY_RUN"
        log.info("[DRY_RUN] would restart: %s", label)
        return action
    try:
        uid = os.getuid()
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True, timeout=15,
        )
        time.sleep(2)
        if plist.exists():
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                capture_output=True, timeout=15,
            )
        subprocess.run(
            ["launchctl", "kickstart", f"gui/{uid}/{label}"],
            capture_output=True, timeout=15,
        )
        action["status"] = "OK"
        log.info("[restart_bot] kicked: %s", label)
    except Exception as e:
        action["status"] = "ERR"
        action["error"] = str(e)
        log.error("[restart_bot] failed: %s", e)
    return action


def stop_bot(cfg: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """chronos_bot.py を停止する（Level3/4用）。"""
    label = cfg.get("bot_launchagent", _DEFAULT_CFG["bot_launchagent"])
    action: dict[str, Any] = {
        "type": "stop_bot",
        "label": label,
        "dry_run": dry_run,
    }
    if dry_run:
        action["status"] = "DRY_RUN"
        log.info("[DRY_RUN] would stop: %s", label)
        return action
    try:
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True, timeout=15,
        )
        # PIDファイルからもkill
        for pid_file_str in cfg.get("pid_files", []):
            pp = Path(pid_file_str)
            if not pp.is_absolute():
                pp = BASE_DIR / pp
            if pp.exists():
                try:
                    pid = int(pp.read_text().strip())
                    subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=5)
                except Exception:
                    pass
        action["status"] = "OK"
        log.info("[stop_bot] stopped: %s", label)
    except Exception as e:
        action["status"] = "ERR"
        action["error"] = str(e)
    return action


# ── ログ stale チェック ───────────────────────────────────────────────────────
def is_log_stale(log_path: Path, threshold_sec: int = 180) -> tuple[bool, float]:
    """ログファイルの最終更新時刻が threshold_sec 秒以上前なら stale と判定する。

    atlas_agent.py の bot_log_is_stale() と同設計。

    Returns:
        (is_stale, age_seconds)
    """
    if not log_path.exists():
        return False, 0.0
    try:
        age = time.time() - log_path.stat().st_mtime
        return age > threshold_sec, age
    except Exception:
        return False, 0.0


# ── 市場時間チェック ─────────────────────────────────────────────────────────
def is_market_hours_now(cfg: dict[str, Any]) -> bool:
    """market_window（JST）内かどうかを判定する。"""
    win = cfg.get("market_window", _DEFAULT_CFG["market_window"])
    start = win.get("start", "22:25")
    end   = win.get("end", "05:05")
    now = datetime.datetime.now(JST)
    hm = now.strftime("%H:%M")
    if start >= end:
        # 日跨ぎ（22:25〜翌05:05）
        return hm >= start or hm <= end
    return start <= hm <= end


# ── アカウント state.json 読み込み ────────────────────────────────────────────
def load_all_account_states() -> list[dict[str, Any]]:
    """data/accounts/*/state.json を全件読み込む。"""
    states = []
    if not ACCOUNTS_DIR.exists():
        return states
    for state_file in ACCOUNTS_DIR.glob("*/state.json"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            data["_account_dir"] = state_file.parent.name
            states.append(data)
        except Exception as e:
            log.warning("[LoadState] %s: %s", state_file, e)
    return states


def get_state_age_sec(state: dict[str, Any]) -> float:
    """state.json の timestamp フィールドから経過秒数を計算する。"""
    ts_str = state.get("timestamp", "")
    if not ts_str:
        return float("inf")
    try:
        ts = datetime.datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        return (datetime.datetime.now(ET) - ts).total_seconds()
    except Exception:
        return float("inf")


# ── エージェントstate保存 ─────────────────────────────────────────────────────
def save_agent_state(state: dict[str, Any]) -> None:
    try:
        AGENT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AGENT_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("[AgentState] save failed: %s", e)


def load_agent_state() -> dict[str, Any]:
    try:
        if AGENT_STATE_PATH.exists():
            return json.loads(AGENT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ── Level1: Bot生存監視 ───────────────────────────────────────────────────────
def check_level1_bot_alive(cfg: dict[str, Any], dry_run: bool) -> list[dict[str, Any]]:
    """Level1: Bot生存確認。死亡検知→Level2で再起動。"""
    alerts = []
    alive = is_bot_alive(cfg)
    if not alive and is_market_hours_now(cfg):
        log.warning("[L1] chronos_bot.py 死亡検知")
        alerts.append({
            "level": 2,  # 自動再起動
            "key": "bot_dead",
            "title": "Bot死亡検知",
            "message": "chronos_bot.py プロセスが見つかりません。自動再起動を試みます。",
            "action": "restart_bot",
        })
    return alerts


# ── Level2: 市場データ・Bot動作監視 ─────────────────────────────────────────
def check_level2_state_stale(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Level2: state.json 更新停止検知（60秒以上更新なし）。"""
    alerts = []
    stale_threshold = 60.0  # 秒

    states = load_all_account_states()
    for state in states:
        age = get_state_age_sec(state)
        account_id = state.get("account_id", state.get("_account_dir", "unknown"))
        if age > stale_threshold and is_market_hours_now(cfg):
            key = f"state_stale_{account_id}"
            log.warning("[L2] state.json stale: %s age=%.0fs", account_id, age)
            alerts.append({
                "level": 2,
                "key": key,
                "title": f"state.json更新停止 {account_id}",
                "message": f"アカウント {account_id} の state.json が {age:.0f}秒間更新されていません。\nBot応答停止の可能性。",
                "action": "notify_only",
            })
    return alerts


def check_level2_log_stale(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Level2: Botログ更新停止検知。"""
    alerts = []
    stale_sec = int(cfg.get("stale_log_sec", 180))
    log_sources: dict[str, str] = cfg.get("log_sources", {})

    for name, path_str in log_sources.items():
        if name == "chronos_agent":
            continue  # 自分自身は除外
        p = Path(path_str)
        if not p.is_absolute():
            p = BASE_DIR / p
        stale, age = is_log_stale(p, stale_sec)
        if stale and is_market_hours_now(cfg):
            key = f"log_stale_{name}"
            log.warning("[L2] log stale: %s age=%.0fs", name, age)
            alerts.append({
                "level": 2,
                "key": key,
                "title": f"ログ更新停止 {name}",
                "message": f"{name} ログが {age:.0f}秒間更新されていません。\nパス: {path_str}",
                "action": "notify_only",
            })
    return alerts


# ── Level3: 戦術動作・P&L監視 ────────────────────────────────────────────────
def check_level3_pnl(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Level3: 日次P&L計算・Max Loss接近（80%到達）警告 / Consistency Rule接近警告。"""
    alerts = []
    mffu = cfg.get("mffu", _DEFAULT_CFG["mffu"])
    max_loss_usd = float(mffu.get("max_loss_usd", 2000.0))
    consistency_max_pct = float(mffu.get("consistency_max_pct", 0.50))
    builder_daily_loss = float(mffu.get("builder_daily_loss_usd", 1000.0))

    states = load_all_account_states()
    for state in states:
        account_id = state.get("account_id", state.get("_account_dir", "unknown"))
        daily_pnl = float(state.get("daily_pnl_usd", 0.0))
        weekly_dd = float(state.get("weekly_dd_usd", 0.0))

        # Max Loss 80%接近チェック（損失方向）
        if daily_pnl < 0:
            loss_abs = abs(daily_pnl)
            loss_pct = loss_abs / max_loss_usd
            if loss_pct >= 0.80:
                key = f"max_loss_80pct_{account_id}"
                log.warning("[L3] Max Loss 80pct over: %s loss=%.2f/%.2f", account_id, loss_abs, max_loss_usd)
                alerts.append({
                    "level": 3,
                    "key": key,
                    "title": f"Max Loss 80%接近 {account_id}",
                    "message": (
                        f"日次損失 ${loss_abs:.2f} / MLL ${max_loss_usd:.2f} ({loss_pct*100:.0f}%)\n"
                        f"即座のサイズ縮小を検討してください。"
                    ),
                    "action": "notify_only",
                })

        # Builder Daily Loss $1,000接近警告
        if daily_pnl < 0 and abs(daily_pnl) >= builder_daily_loss * 0.80:
            key = f"builder_daily_loss_{account_id}"
            if _should_notify(key):
                log.warning("[L3] Builder Daily Loss接近: %s pnl=%.2f", account_id, daily_pnl)
                alerts.append({
                    "level": 3,
                    "key": key,
                    "title": f"Builder Daily Loss接近 {account_id}",
                    "message": (
                        f"日次損失 ${abs(daily_pnl):.2f} / 警告ライン ${builder_daily_loss:.2f}\n"
                        f"サイズ削減を推奨します。"
                    ),
                    "action": "notify_only",
                })

        # Consistency Rule 40%（max_pct×0.80）接近警告
        # Evaluation フェーズのみ適用
        if mffu.get("phase") == "evaluation" and consistency_max_pct > 0:
            warn_pct = consistency_max_pct * 0.80  # 50%ルールの80% = 40%
            # state.json に best_single_day_profit があれば確認
            best_day = float(state.get("best_single_day_profit_usd", 0.0))
            total_profit = float(state.get("total_profit_usd", 0.0))
            if total_profit > 0 and best_day > 0:
                actual_pct = best_day / total_profit
                if actual_pct >= warn_pct:
                    key = f"consistency_warn_{account_id}"
                    if _should_notify(key):
                        log.warning(
                            "[L3] Consistency 接近: %s best_day=%.2f total=%.2f (%.0f%%)",
                            account_id, best_day, total_profit, actual_pct * 100,
                        )
                        alerts.append({
                            "level": 3,
                            "key": key,
                            "title": f"Consistency Rule接近 {account_id}",
                            "message": (
                                f"最大1日利益 ${best_day:.2f} / 累積利益 ${total_profit:.2f} = {actual_pct*100:.0f}%\n"
                                f"MFFU Consistency上限: {consistency_max_pct*100:.0f}%\n"
                                f"本日はポジションサイズを抑えてください。"
                            ),
                            "action": "notify_only",
                        })

    return alerts


def check_level3_payout_reminder(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Level3: Payout請求タイミングリマインダー。"""
    alerts = []
    mffu = cfg.get("mffu", _DEFAULT_CFG["mffu"])
    payout_cfg = mffu.get("payout", {})
    min_winning_days = int(payout_cfg.get("min_winning_days", 5))
    min_net_profit = float(payout_cfg.get("min_net_profit_usd", 500.0))

    states = load_all_account_states()
    for state in states:
        account_id = state.get("account_id", state.get("_account_dir", "unknown"))
        winning_days = int(state.get("winning_days_count", 0))
        net_profit = float(state.get("total_profit_usd", 0.0))

        if winning_days >= min_winning_days and net_profit >= min_net_profit:
            key = f"payout_eligible_{account_id}"
            if _should_notify(key):
                log.info("[L3] Payout条件達成: %s", account_id)
                alerts.append({
                    "level": 1,
                    "key": key,
                    "title": f"Payout請求可能 {account_id}",
                    "message": (
                        f"勝利日数: {winning_days} / {min_winning_days}日達成\n"
                        f"純利益: ${net_profit:.2f} / ${min_net_profit:.2f}達成\n"
                        f"Payout請求タイミングです。"
                    ),
                    "action": "notify_only",
                })

    return alerts


# ── Level4: MFFU規約遵守（致命的違反） ───────────────────────────────────────
def check_level4_news_window(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Level4: News Window T1ウィンドウ違反検知。

    state.json の last_order_time と recent_orders を確認し、
    ニュースイベントWindow内に注文があればCRITICALとして報告。
    実発注検知のみ（シミュレーション内部判断はchronos_bot.pyが担当）。
    """
    alerts = []
    # ニュース窓は chronos_bot.py 内で enforce されているため、
    # エージェント側は state.json の違反フラグを確認する設計。
    states = load_all_account_states()
    for state in states:
        account_id = state.get("account_id", state.get("_account_dir", "unknown"))
        phase_flags = state.get("phase_flags", {})
        news_violation = phase_flags.get("news_window_violation", False)
        if news_violation:
            key = f"news_window_violation_{account_id}"
            if _should_notify(key):
                log.critical("[L4] News Window違反: %s", account_id)
                alerts.append({
                    "level": 4,
                    "key": key,
                    "title": f"[CRITICAL] News Window違反 {account_id}",
                    "message": (
                        f"T1ニュースWindow内に注文が発生しました。\n"
                        f"アカウント: {account_id}\n"
                        f"MFFUルール違反の可能性。即時確認してください。"
                    ),
                    "action": "stop_bot",
                })
    return alerts


def check_level4_hft(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Level4: HFT（200trades/day）警告。"""
    alerts = []
    mffu = cfg.get("mffu", _DEFAULT_CFG["mffu"])
    hft_limit = int(mffu.get("hft_daily_max_trades", 200))
    warn_threshold = int(hft_limit * 0.80)  # 160件で警告

    states = load_all_account_states()
    for state in states:
        account_id = state.get("account_id", state.get("_account_dir", "unknown"))
        daily_trades = int(state.get("daily_trade_count", 0))
        if daily_trades >= warn_threshold:
            key = f"hft_warn_{account_id}"
            if _should_notify(key):
                log.warning("[L4] HFT接近: %s trades=%d/%d", account_id, daily_trades, hft_limit)
                alerts.append({
                    "level": 3 if daily_trades < hft_limit else 4,
                    "key": key,
                    "title": f"HFT上限接近 {account_id}",
                    "message": (
                        f"本日取引数: {daily_trades} / 上限 {hft_limit}\n"
                        f"200件超えでHFT違反（MFFU規約）。エントリー停止を検討してください。"
                    ),
                    "action": "notify_only",
                })
    return alerts


def check_level4_sim_funded_payout_mode(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Level4: Sim-Funded初回Payout後のMax Loss $100モード移行確認。"""
    alerts = []
    states = load_all_account_states()
    for state in states:
        account_id = state.get("account_id", state.get("_account_dir", "unknown"))
        account_type = state.get("account_type", "")
        phase_flags = state.get("phase_flags", {})
        survival_mode = phase_flags.get("survival_mode", False)

        # Sim-Funded後にsurvival_modeが未起動なら警告
        if "sim_funded_after_payout" in account_type and not survival_mode:
            key = f"survival_mode_not_activated_{account_id}"
            if _should_notify(key):
                log.critical("[L4] Survival Mode未移行: %s", account_id)
                alerts.append({
                    "level": 4,
                    "key": key,
                    "title": f"[CRITICAL] Survival Mode未移行 {account_id}",
                    "message": (
                        f"初回Payout後のアカウント ({account_id}) で\n"
                        f"Max Loss $100 Survival Modeが未起動です。\n"
                        f"即座に確認・手動移行してください。"
                    ),
                    "action": "notify_only",
                })
    return alerts


# ── アラート dispatch ─────────────────────────────────────────────────────────
def dispatch_alert(alert: dict[str, Any], cfg: dict[str, Any], dry_run: bool) -> None:
    """アラートをレベルに応じて対応する。"""
    level = alert.get("level", 1)
    key = alert.get("key", "unknown")
    title = alert.get("title", "")
    message = alert.get("message", "")
    action = alert.get("action", "notify_only")

    # 通知冷却
    if not _should_notify(key):
        return

    if level == 1:
        pushover(f"INFO {title}", message, priority=0)
        log.info("[L1/notify] %s", title)

    elif level == 2:
        if action == "restart_bot":
            result = restart_bot(cfg, dry_run=dry_run)
            status_msg = f"action: {result.get('status', '?')}"
            pushover(
                f"AUTOFIX {title}",
                f"{message}\n{status_msg}",
                priority=1,
            )
            log.info("[L2/restart] %s: %s", title, result.get("status"))
        else:
            pushover(f"WARNING {title}", message, priority=1)
            log.warning("[L2/notify] %s", title)

    elif level == 3:
        pushover_alert(f"ALERT {title}", message, priority=1)
        log.error("[L3/alert] %s", title)
        if action == "stop_bot":
            result = stop_bot(cfg, dry_run=dry_run)
            log.error("[L3/stop] %s: %s", title, result.get("status"))

    elif level == 4:
        pushover_alert(f"HALT {title}", message, priority=2)
        log.critical("[L4/halt] %s", title)
        if action == "stop_bot":
            result = stop_bot(cfg, dry_run=dry_run)
            log.critical("[L4/stop] %s: %s", title, result.get("status"))
        # agent_state に manual_halt を記録
        agent_state = load_agent_state()
        agent_state["manual_halt"] = {
            "key": key,
            "since": datetime.datetime.now(JST).isoformat(),
            "title": title,
        }
        save_agent_state(agent_state)


# ── 監視ループ ─────────────────────────────────────────────────────────────────
def monitor_cycle(cfg: dict[str, Any], dry_run: bool = True) -> list[dict[str, Any]]:
    """1サイクルの監視処理を実行する。

    処理順:
      1. Kill Switch チェック
      2. Level1: Bot生存確認
      3. Level2: state.json更新停止 / ログstale確認
      4. Level3: P&L / Consistency / Payout
      5. Level4: News Window / HFT / Survival Mode
      6. アラート dispatch
    """
    fired_alerts: list[dict[str, Any]] = []

    # Kill Switch チェック
    if KILL_SWITCH_AVAILABLE and kill_switch_is_active():
        reason = kill_switch_reason()
        log.warning("[CYCLE] Kill Switch active: %s skip all checks", reason)
        return fired_alerts

    # manual_halt チェック
    agent_state = load_agent_state()
    if agent_state.get("manual_halt"):
        halt_info = agent_state["manual_halt"]
        log.warning("[CYCLE] manual_halt active: %s monitoring only", halt_info.get("key"))
        # haltでも生死確認だけは継続
        fired_alerts += check_level1_bot_alive(cfg, dry_run)
    else:
        # Level1
        fired_alerts += check_level1_bot_alive(cfg, dry_run)
        # Level2
        fired_alerts += check_level2_state_stale(cfg)
        fired_alerts += check_level2_log_stale(cfg)
        # Level3
        fired_alerts += check_level3_pnl(cfg)
        fired_alerts += check_level3_payout_reminder(cfg)
        # Level4
        fired_alerts += check_level4_news_window(cfg)
        fired_alerts += check_level4_hft(cfg)
        fired_alerts += check_level4_sim_funded_payout_mode(cfg)

    for alert in fired_alerts:
        dispatch_alert(alert, cfg, dry_run)

    # agentループ自己記録
    agent_state = load_agent_state()
    agent_state["last_cycle_jst"] = datetime.datetime.now(JST).isoformat()
    agent_state["last_cycle_alerts"] = len(fired_alerts)
    save_agent_state(agent_state)

    return fired_alerts


def run(dry_run: bool = True, armed: bool = False, once: bool = False) -> None:
    """Chronos Agentメインループ。"""
    cfg = load_config()
    if armed:
        dry_run = False
    cycle_interval = int(cfg.get("cycle_interval_sec", 60))

    log.info(
        "[Chronos Agent] 起動 dry_run=%s armed=%s once=%s cycle_interval=%ds",
        dry_run, armed, once, cycle_interval,
    )

    # 起動通知
    pushover(
        "[Chronos] Agent起動",
        (
            f"chronos_agent.py 起動\n"
            f"dry_run: {'ON' if dry_run else 'OFF (ARMED)'}\n"
            f"cycle: {cycle_interval}秒\n"
            f"市場時間: {cfg.get('market_window', {})}"
        ),
        priority=0,
    )

    while True:
        try:
            fired = monitor_cycle(cfg, dry_run=dry_run)
            if fired:
                log.info("[CYCLE] %d alerts fired", len(fired))

            if once:
                log.info("[Chronos Agent] --once 完了: fired=%d", len(fired))
                break

            time.sleep(cycle_interval)

        except KeyboardInterrupt:
            log.info("[Chronos Agent] KeyboardInterrupt → 終了")
            break
        except Exception as e:
            log.error("[LOOP_ERR] %s", e)
            time.sleep(10)


# ── manual_halt 解除 ─────────────────────────────────────────────────────────
def unhalt() -> None:
    """manual_halt を解除する。

    cycle2 BUG-5: Level4 HALT 後の復旧 CLI。
    agent_state["manual_halt"] を pop して保存し、Pushover に解除通知を送る。

    使い方:
        python3 chronos_agent.py --unhalt
    """
    agent_state = load_agent_state()
    halt_info = agent_state.pop("manual_halt", None)
    if halt_info is None:
        log.info("[unhalt] manual_halt は設定されていません（すでに解除済み）。")
        print("[chronos_agent] manual_halt is not set. Nothing to unhalt.")
        return

    save_agent_state(agent_state)
    msg = (
        f"manual_halt 解除完了\n"
        f"解除されたhalt: {halt_info.get('title', '?')}\n"
        f"原因key: {halt_info.get('key', '?')}\n"
        f"halt開始: {halt_info.get('since', '?')}\n"
        f"次回サイクルから全チェック再開します。\n"
        f"再停止コマンド: なし（Level4アラートで自動設定）\n"
        f"解除コマンド: python3 chronos_agent.py --unhalt"
    )
    log.info("[unhalt] %s", msg.replace("\n", " | "))
    pushover("[Chronos] manual_halt 解除", msg, priority=1)
    print(f"[chronos_agent] unhalt OK: {halt_info.get('title', '?')}")


# ── CLI エントリーポイント ────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Chronos Agent — 常駐監視・自己回復エージェント")
    parser.add_argument("--selftest", action="store_true", help="config読み込みのみ確認して終了")
    parser.add_argument("--once", action="store_true", help="1サイクルのみ実行して終了")
    parser.add_argument("--dry-run", action="store_true", help="autofixをDRY_RUNに")
    parser.add_argument("--armed", action="store_true", help="autofixをARMEDに（実際に再起動する）")
    parser.add_argument(
        "--unhalt",
        action="store_true",
        help="manual_halt を解除してモニタリングを再開する。解除: python3 chronos_agent.py --unhalt",
    )
    args = parser.parse_args()

    if args.unhalt:
        unhalt()
        return

    if args.selftest:
        cfg = load_config()
        log.info("[Chronos Agent] selftest: config OK. mffu=%s", cfg.get("mffu"))
        log.info("[Chronos Agent] selftest: market_window=%s", cfg.get("market_window"))
        log.info("[Chronos Agent] selftest: YAML_AVAILABLE=%s KILL_SWITCH_AVAILABLE=%s",
                 YAML_AVAILABLE, KILL_SWITCH_AVAILABLE)
        return

    run(dry_run=args.dry_run, armed=args.armed, once=args.once)


if __name__ == "__main__":
    main()
