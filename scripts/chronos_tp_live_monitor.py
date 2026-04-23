#!/usr/bin/env python3
"""
scripts/chronos_tp_live_monitor.py — Chronos TP 実行ログ リアルタイム監視

data/chronos_traderspost_executions.jsonl を 1 分毎に tail し、
新規エントリを検出するたびに firm 別集計 + Pushover 通知する。

日次集計レポートを毎朝 06:30 JST (= 21:30 UTC 前日) に生成する。

起動:
    python3 scripts/chronos_tp_live_monitor.py

環境変数:
    CHRONOS_TP_EXEC_LOG   — 監視対象 jsonl (デフォルト data/chronos_traderspost_executions.jsonl)
    CHRONOS_TP_MONITOR_POLL_SEC — ポーリング間隔秒 (デフォルト 60)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("chronos_tp_monitor")

EXEC_LOG = Path(
    os.environ.get("CHRONOS_TP_EXEC_LOG", str(_HERE / "data" / "chronos_traderspost_executions.jsonl"))
)
OPS_DIR = _HERE / "data" / "ops"
POLL_SEC = float(os.environ.get("CHRONOS_TP_MONITOR_POLL_SEC", "60"))

# Pushover 通知
def _notify(title: str, message: str, priority: int = 0) -> None:
    try:
        from common.pushover_client import send  # noqa: PLC0415
        send(title, message, priority=priority)
    except Exception as e:
        log.warning("[Pushover] 通知失敗 (non-fatal): %s", e)


# ── 集計ストア ────────────────────────────────────────────────────────────────

class FirmStats:
    """firm 別の累計集計を保持する。"""

    def __init__(self) -> None:
        # firm → {"total": int, "allow": int, "block": int, "tp_success": int}
        self._data: dict[str, dict[str, int]] = defaultdict(
            lambda: {"total": 0, "allow": 0, "block": 0, "tp_success": 0}
        )
        self._today_signals: list[dict] = []
        self._today_date: Optional[datetime.date] = None

    def record(self, entry: dict) -> None:
        strategy_id = entry.get("strategy_id") or "unknown"
        firm = _strategy_to_firm(strategy_id)
        result = entry.get("firm_constraint_result", "no_strategy")
        tp_success = bool(
            entry.get("tp_response") and entry["tp_response"].get("success")
        )

        self._data[firm]["total"] += 1
        if result == "allow":
            self._data[firm]["allow"] += 1
        elif result == "block":
            self._data[firm]["block"] += 1
        if tp_success:
            self._data[firm]["tp_success"] += 1

        # 日次リスト
        today = datetime.date.today()
        if self._today_date != today:
            self._today_signals = []
            self._today_date = today
        self._today_signals.append(entry)

    def summary_text(self) -> str:
        lines = ["firm 別累計集計:"]
        for firm, counts in sorted(self._data.items()):
            lines.append(
                f"  {firm}: total={counts['total']} allow={counts['allow']} "
                f"block={counts['block']} tp_ok={counts['tp_success']}"
            )
        return "\n".join(lines)

    def daily_signals(self) -> list[dict]:
        return list(self._today_signals)

    def all_stats(self) -> dict[str, dict[str, int]]:
        return dict(self._data)


def _strategy_to_firm(strategy_id: str) -> str:
    """strategy_id から firm 名を抽出する。"""
    mapping = {
        "chronos_orb_mes_demo": "demo",
        "chronos_orb_mes_rapid_sim": "mffu_rapid",
        "chronos_orb_mes_pro_sim": "mffu_pro",
        "chronos_orb_mes_builder_sim": "mffu_builder",
        "chronos_orb_mes_tradeify_sim": "tradeify",
    }
    return mapping.get(strategy_id, strategy_id)


# ── ファイル tail ──────────────────────────────────────────────────────────────

class JsonlTailReader:
    """JSONL を tail して新行を yield する。起動時は末尾にシークする。"""

    def __init__(self, path: Path, from_beginning: bool = False) -> None:
        self._path = path
        self._pos = 0
        self._fp = None
        self._from_beginning = from_beginning

    def _open(self) -> None:
        self._fp = open(self._path, "r", encoding="utf-8")
        if not self._from_beginning:
            self._fp.seek(0, 2)
        self._pos = self._fp.tell()
        log.info("[TailReader] opened %s pos=%d", self._path, self._pos)

    def read_new_lines(self) -> list[str]:
        if not self._path.exists():
            return []
        if self._fp is None:
            self._open()
        self._fp.seek(self._pos)
        lines = []
        while True:
            line = self._fp.readline()
            if not line:
                break
            lines.append(line.strip())
        self._pos = self._fp.tell()
        return [l for l in lines if l]

    def close(self) -> None:
        if self._fp:
            self._fp.close()
            self._fp = None


# ── 日次レポート生成 ──────────────────────────────────────────────────────────

def write_daily_report(stats: FirmStats, date: Optional[datetime.date] = None) -> Path:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    target_date = date or datetime.date.today()
    report_path = OPS_DIR / "chronos_firm_daily_report.md"

    all_stats = stats.all_stats()
    daily = stats.daily_signals()

    total_signals = sum(s["total"] for s in all_stats.values())
    total_blocks = sum(s["block"] for s in all_stats.values())

    lines = [
        f"# Chronos firm 別日次レポート — {target_date}",
        "",
        f"生成時刻: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        f"累計 signal 数: {total_signals}",
        f"累計 block 数: {total_blocks}",
        "",
        "## firm 別累計集計",
        "",
        "| firm | total | allow | block | tp_ok |",
        "|------|-------|-------|-------|-------|",
    ]
    for firm, counts in sorted(all_stats.items()):
        lines.append(
            f"| {firm} | {counts['total']} | {counts['allow']} "
            f"| {counts['block']} | {counts['tp_success']} |"
        )

    lines += [
        "",
        f"## 当日 signal 一覧 ({len(daily)} 件)",
        "",
        "| timestamp | strategy_id | action | qty | result | reason_short |",
        "|-----------|-------------|--------|-----|--------|--------------|",
    ]
    for e in daily[-50:]:  # 最新50件
        ts = e.get("timestamp", "")[:19]
        sid = e.get("strategy_id", "")
        act = e.get("action", "")
        qty = e.get("qty", "")
        res = e.get("firm_constraint_result", "")
        reason = (e.get("firm_constraint_reason") or "")[:40]
        lines.append(f"| {ts} | {sid} | {act} | {qty} | {res} | {reason} |")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("[DailyReport] written to %s", report_path)
    return report_path


# ── メインループ ──────────────────────────────────────────────────────────────

def run(poll_sec: float = POLL_SEC, from_beginning: bool = False) -> None:
    """メインデーモンループ。"""
    log.info("=" * 60)
    log.info("[ChronosTPMonitor] 起動")
    log.info("  EXEC_LOG=%s", EXEC_LOG)
    log.info("  POLL_SEC=%.0f", poll_sec)
    log.info("=" * 60)

    stats = FirmStats()
    reader = JsonlTailReader(EXEC_LOG, from_beginning=from_beginning)
    running = True
    last_report_date: Optional[datetime.date] = None

    def _shutdown(signum, frame):
        nonlocal running
        log.info("[ChronosTPMonitor] シグナル %d → 終了", signum)
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _notify(
        "[Chronos] TP Live Monitor 起動",
        f"chronos_tp_live_monitor 起動\nexec_log={EXEC_LOG}\npoll={poll_sec}s",
        priority=-1,
    )

    while running:
        try:
            new_lines = reader.read_new_lines()
            for line in new_lines:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # smoke_test フラグ付きは通知スキップ (集計のみ)
                is_smoke = entry.get("smoke_test", False)
                stats.record(entry)

                firm_result = entry.get("firm_constraint_result", "")
                strategy_id = entry.get("strategy_id", "unknown")
                signal_id = entry.get("signal_id", "")
                action = entry.get("action", "")
                qty = entry.get("qty", "")

                if not is_smoke:
                    if firm_result == "block":
                        _notify(
                            "[Chronos/ALERT] firm_constraint BLOCK",
                            (
                                f"strategy={strategy_id}\n"
                                f"signal={signal_id}\n"
                                f"action={action} qty={qty}\n"
                                f"reason={entry.get('firm_constraint_reason', '')[:120]}\n\n"
                                + stats.summary_text()
                            ),
                            priority=1,
                        )
                    elif firm_result == "allow":
                        log.info(
                            "[Monitor] ALLOW signal_id=%s strategy=%s %s x%s",
                            signal_id, strategy_id, action, qty,
                        )

            # 日次レポート: 毎日最初のポーリングで生成
            today = datetime.date.today()
            if last_report_date != today:
                write_daily_report(stats, today)
                last_report_date = today

            time.sleep(poll_sec)

        except Exception as e:
            log.error("[ChronosTPMonitor] ループ例外 (継続): %s", e)
            time.sleep(poll_sec)

    reader.close()
    write_daily_report(stats)
    log.info("[ChronosTPMonitor] 正常終了")


if __name__ == "__main__":
    run()
