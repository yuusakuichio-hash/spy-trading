#!/usr/bin/env python3
"""
scripts/failure_to_rescue.py — AHRQ 式 Failure to Rescue Detector

役割:
    各 anomaly について 3 タイムスタンプを追跡する:
      1. detected_at  : 検知時刻
      2. responded_at : 対応開始時刻 (手動 or auto)
      3. resolved_at  : 解決時刻

    escalation ルール:
      detected → responded  15分超 → P2 alert (rescue 遅延)
      responded → resolved  60分超 → P2 alert (rescue failure)
      同一 anomaly_id 3回以上再発  → P2 alert (rescue 効果なし)

ストレージ:
    data/ops/rescue_tracker.jsonl

呼び出し方:
    python3 scripts/failure_to_rescue.py --detect  --id <id> --msg <msg>
    python3 scripts/failure_to_rescue.py --respond --id <id>
    python3 scripts/failure_to_rescue.py --resolve --id <id>
    python3 scripts/failure_to_rescue.py --check           (LaunchAgent から 5分毎)

strategy_aware_monitor との連携:
    anomaly 検知時に --detect を呼ぶ。
    修正完了時に --resolve を呼ぶ。
    自動対応ロジック起動時に --respond を呼ぶ。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from common.pushover_client import send as pushover_send

# ── パス設定 ──────────────────────────────────────────────────────────────────
_TRADING_DIR = Path(os.environ.get("SORA_TRADING_DIR", _PROJECT_ROOT))
TRACKER_FILE = _TRADING_DIR / "data" / "ops" / "rescue_tracker.jsonl"

# ── 閾値 ──────────────────────────────────────────────────────────────────────
RESPONSE_TIMEOUT_SEC = 15 * 60   # 15分: responded_at なし → alert
RESOLVE_TIMEOUT_SEC  = 60 * 60   # 60分: resolved_at なし → alert
RECUR_THRESHOLD      = 3          # 同一 anomaly_id 解決済み再発回数

# ── ロガー ───────────────────────────────────────────────────────────────────
log = logging.getLogger("failure_to_rescue")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] failure_to_rescue: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

LOG_DIR = _TRADING_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_fh = logging.FileHandler(LOG_DIR / "failure_to_rescue.log")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)


# ── JSONL 読み書き ────────────────────────────────────────────────────────────

def _load_all() -> list[dict]:
    if not TRACKER_FILE.exists():
        return []
    records: list[dict] = []
    for line in TRACKER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _save_all(records: list[dict]) -> None:
    TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TRACKER_FILE.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_to_epoch(ts_iso: str | None) -> float | None:
    if ts_iso is None:
        return None
    try:
        return datetime.fromisoformat(ts_iso).timestamp()
    except ValueError:
        return None


# ── 操作関数 ─────────────────────────────────────────────────────────────────

def detect(anomaly_id: str, message: str, source: str = "unknown") -> None:
    """anomaly を検知して記録する。同一 anomaly_id が未解決なら新規追加しない。"""
    records = _load_all()
    # 未解決の同一 anomaly がある場合は重複追加しない
    for rec in records:
        if rec.get("anomaly_id") == anomaly_id and rec.get("resolved_at") is None:
            log.info("detect: already open anomaly_id=%s", anomaly_id)
            return
    record = {
        "anomaly_id":   anomaly_id,
        "message":      message,
        "source":       source,
        "detected_at":  _now_iso(),
        "responded_at": None,
        "resolved_at":  None,
    }
    records.append(record)
    _save_all(records)
    log.info("detect: anomaly_id=%s msg=%s", anomaly_id, message[:80])


def respond(anomaly_id: str) -> bool:
    """anomaly に対応開始を記録する。"""
    records = _load_all()
    for rec in records:
        if rec.get("anomaly_id") == anomaly_id and rec.get("resolved_at") is None:
            if rec.get("responded_at") is None:
                rec["responded_at"] = _now_iso()
                _save_all(records)
                log.info("respond: anomaly_id=%s", anomaly_id)
                return True
            log.info("respond: already responded anomaly_id=%s", anomaly_id)
            return True
    log.warning("respond: open anomaly not found anomaly_id=%s", anomaly_id)
    return False


def resolve(anomaly_id: str) -> bool:
    """anomaly を解決済みにする。"""
    records = _load_all()
    for rec in records:
        if rec.get("anomaly_id") == anomaly_id and rec.get("resolved_at") is None:
            rec["resolved_at"] = _now_iso()
            _save_all(records)
            log.info("resolve: anomaly_id=%s", anomaly_id)
            return True
    log.warning("resolve: open anomaly not found anomaly_id=%s", anomaly_id)
    return False


# ── check ループ ─────────────────────────────────────────────────────────────

def check_pending() -> None:
    """未解決 anomaly を走査し、escalation 条件を判定して P2 alert を送信する。"""
    records = _load_all()
    now = time.time()

    # 再発カウント (解決済み含む全レコード)
    recur_count: dict[str, int] = {}
    for rec in records:
        if rec.get("resolved_at") is not None:
            aid = rec.get("anomaly_id", "")
            recur_count[aid] = recur_count.get(aid, 0) + 1

    for rec in records:
        if rec.get("resolved_at") is not None:
            continue  # 解決済みはスキップ

        aid = rec.get("anomaly_id", "UNKNOWN")
        msg_text = rec.get("message", "")
        detected_epoch = _ts_to_epoch(rec.get("detected_at"))
        responded_epoch = _ts_to_epoch(rec.get("responded_at"))

        if detected_epoch is None:
            continue

        age_total = now - detected_epoch

        # 1. detected → responded 15分超
        if responded_epoch is None and age_total > RESPONSE_TIMEOUT_SEC:
            alert_title = f"[SYS] FTR rescue遅延 {aid}"
            alert_msg = (
                f"anomaly 検知から {age_total/60:.0f}分 未対応\n"
                f"anomaly: {msg_text[:100]}"
            )
            log.error("RESCUE_DELAY: %s | %s", alert_title, alert_msg)
            pushover_send(alert_title, alert_msg, priority=2)

        # 2. responded → resolved 60分超
        elif responded_epoch is not None:
            response_age = now - responded_epoch
            if response_age > RESOLVE_TIMEOUT_SEC:
                alert_title = f"[SYS] FTR rescue失敗 {aid}"
                alert_msg = (
                    f"対応開始から {response_age/60:.0f}分 未解決\n"
                    f"anomaly: {msg_text[:100]}"
                )
                log.error("RESCUE_FAILURE: %s | %s", alert_title, alert_msg)
                pushover_send(alert_title, alert_msg, priority=2)

        # 3. 再発 (同一 anomaly_id の解決済み件数 >= RECUR_THRESHOLD)
        if recur_count.get(aid, 0) >= RECUR_THRESHOLD:
            alert_title = f"[SYS] FTR rescue効果なし {aid}"
            alert_msg = (
                f"同一 anomaly が {recur_count[aid]} 回再発\n"
                f"anomaly: {msg_text[:100]}"
            )
            log.error("RESCUE_INEFFECTIVE: %s | %s", alert_title, alert_msg)
            pushover_send(alert_title, alert_msg, priority=2)
            # 再発アラートは 1回だけ送る (resolved_at を仮セット)
            rec["_recur_alerted"] = True


def list_open() -> list[dict]:
    """未解決 anomaly 一覧を返す (テスト・監視用)。"""
    return [r for r in _load_all() if r.get("resolved_at") is None]


# ── JSONL ローテーション (30日以上古い解決済みを削除) ────────────────────────

def _rotate() -> None:
    cutoff = time.time() - 30 * 86400
    records = _load_all()
    kept = []
    for rec in records:
        resolved = _ts_to_epoch(rec.get("resolved_at"))
        if resolved is not None and resolved < cutoff:
            continue  # 古い解決済みを削除
        kept.append(rec)
    _save_all(kept)


# ── エントリポイント ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Failure to Rescue Detector")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--detect",  action="store_true", help="anomaly 検知")
    grp.add_argument("--respond", action="store_true", help="対応開始")
    grp.add_argument("--resolve", action="store_true", help="解決完了")
    grp.add_argument("--check",   action="store_true", help="pending check (LaunchAgent から)")
    grp.add_argument("--list",    action="store_true", help="未解決一覧表示")
    parser.add_argument("--id",  default=None, help="anomaly ID")
    parser.add_argument("--msg", default="",  help="anomaly メッセージ (--detect 時)")
    parser.add_argument("--src", default="cli", help="検知ソース")
    args = parser.parse_args()

    if args.detect:
        if not args.id:
            parser.error("--detect には --id が必要")
        detect(args.id, args.msg, args.src)

    elif args.respond:
        if not args.id:
            parser.error("--respond には --id が必要")
        respond(args.id)

    elif args.resolve:
        if not args.id:
            parser.error("--resolve には --id が必要")
        resolve(args.id)

    elif args.check:
        _rotate()
        check_pending()

    elif args.list:
        open_list = list_open()
        print(json.dumps(open_list, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
