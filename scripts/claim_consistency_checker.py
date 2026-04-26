#!/usr/bin/env python3
"""
scripts/claim_consistency_checker.py — Cross-agent disagreement detector

目的:
    複数 agent (builder/ops/redteam/analyst) が同じ事象について
    異なる主張をした時に自動検知+調停する。
    silent lie でも他 agent の観測と矛盾 → 発覚する仕組み。

動作:
    1. agent 報告ファイル (data/ops/agent_claims/*.jsonl) を読み込む
    2. 同一 claim_key について agent 間の値を matrix 化
    3. 矛盾 (例: ops: "active"、redteam: "down") → alert + 実機再検証
    4. 実機 ground truth (filesystem/log) と照合して最終判定
    5. data/ops/agent_disagreement_log.jsonl に記録
    6. 矛盾ありなら Pushover alert

常駐モード:
    python3 scripts/claim_consistency_checker.py --daemon
    5分毎に reconcile を実行する。

単発実行:
    python3 scripts/claim_consistency_checker.py

Smoke test:
    python3 scripts/claim_consistency_checker.py --smoke-test
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ---- ログ設定 ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("claim_checker")

# ---- パス定数 ---------------------------------------------------------------
_DATA_OPS = _PROJECT_ROOT / "data" / "ops"
_CLAIMS_DIR = _DATA_OPS / "agent_claims"
_DISAGREEMENT_LOG = _DATA_OPS / "agent_disagreement_log.jsonl"
_HEARTBEAT_DIR = _PROJECT_ROOT / "data" / "heartbeats"

# 5分周期 (daemon モード)
_RECONCILE_INTERVAL_SEC = 300

# claim が古すぎる場合に無視する閾値 (分)
_CLAIM_MAX_AGE_MINUTES = 60

# ---- 既知の claim キー → 実機検証ハンドラ ----------------------------------
# claim_key: str → ground_truth_value: str | None (None=取得不可)
def _ground_truth_atlas_status() -> str | None:
    """Atlas heartbeat ファイルから稼働状態を取得。"""
    hb = _HEARTBEAT_DIR / "atlas_agent.json"
    if not hb.exists():
        return "down"
    try:
        data = json.loads(hb.read_text())
        ts = datetime.datetime.fromisoformat(data.get("ts", "1970-01-01T00:00:00"))
        age = (datetime.datetime.now() - ts).total_seconds()
        return "active" if age < 600 else "stale"
    except Exception:
        return None


def _ground_truth_chronos_status() -> str | None:
    """Chronos heartbeat ファイルから稼働状態を取得。"""
    hb = _HEARTBEAT_DIR / "chronos_agent.json"
    if not hb.exists():
        return "down"
    try:
        data = json.loads(hb.read_text())
        ts = datetime.datetime.fromisoformat(data.get("ts", "1970-01-01T00:00:00"))
        age = (datetime.datetime.now() - ts).total_seconds()
        return "active" if age < 600 else "stale"
    except Exception:
        return None


def _ground_truth_trade_count() -> str | None:
    """当日の発注ログから件数を取得する。"""
    log_dir = _PROJECT_ROOT / "data" / "logs"
    today = datetime.date.today().isoformat()
    patterns = [
        log_dir / f"atlas_trades_{today}.jsonl",
        log_dir / f"atlas_orders_{today}.jsonl",
        _PROJECT_ROOT / "data" / f"atlas_trades_{today}.jsonl",
    ]
    for p in patterns:
        if p.exists():
            count = sum(1 for line in p.read_text().splitlines() if line.strip())
            return str(count)
    # ファイルなし = 0件 (市場未開時)
    return "0"


_GROUND_TRUTH_HANDLERS: dict[str, Any] = {
    "atlas_status": _ground_truth_atlas_status,
    "chronos_status": _ground_truth_chronos_status,
    "trade_count": _ground_truth_trade_count,
}

# ---- Claim データ構造 -------------------------------------------------------
# {
#   "ts": "2026-04-21T01:00:00",
#   "agent": "ops",
#   "claim_key": "atlas_status",
#   "claim_value": "active",
#   "context": "heartbeat confirmed",
# }

ClaimRecord = dict[str, Any]


def _load_recent_claims() -> list[ClaimRecord]:
    """_CLAIMS_DIR 以下の全 .jsonl を読み込み、新しい順に返す。"""
    if not _CLAIMS_DIR.exists():
        _CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
        return []

    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=_CLAIM_MAX_AGE_MINUTES)
    records: list[ClaimRecord] = []

    for f in sorted(_CLAIMS_DIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.datetime.fromisoformat(rec.get("ts", "1970-01-01T00:00:00"))
                if ts >= cutoff:
                    records.append(rec)
            except Exception:
                pass

    return sorted(records, key=lambda r: r.get("ts", ""), reverse=True)


def _build_matrix(records: list[ClaimRecord]) -> dict[str, dict[str, str]]:
    """
    claim_key → {agent: latest_value} の matrix を構築する。
    同一 agent の同一 claim_key は最新のものを採用。
    """
    matrix: dict[str, dict[str, str]] = {}
    seen: dict[tuple[str, str], bool] = {}

    # ts降順なので先に見た方が新しい
    for rec in records:
        key = rec.get("claim_key", "")
        agent = rec.get("agent", "")
        value = rec.get("claim_value", "")
        if not key or not agent:
            continue
        pair = (key, agent)
        if pair in seen:
            continue
        seen[pair] = True
        if key not in matrix:
            matrix[key] = {}
        matrix[key][agent] = value

    return matrix


def _detect_disagreements(
    matrix: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """
    matrix を走査して矛盾を検出する。
    同一 claim_key について 2 以上の agent が異なる値を主張している場合を矛盾とする。
    ただし数値の場合は ±10% 以内を一致とみなす。
    """
    disagreements: list[dict[str, Any]] = []

    for claim_key, agent_values in matrix.items():
        if len(agent_values) < 2:
            continue

        values = list(agent_values.values())
        # 数値かどうか判定
        nums = []
        for v in values:
            try:
                nums.append(float(v))
            except ValueError:
                nums = []
                break

        if nums:
            # 数値: 最大-最小 > max*0.1 かつ > 0 で矛盾
            spread = max(nums) - min(nums)
            threshold = max(abs(max(nums)) * 0.1, 1)
            if spread > threshold:
                disagreements.append({
                    "claim_key": claim_key,
                    "agent_values": agent_values,
                    "reason": f"numeric_spread={spread:.2f} threshold={threshold:.2f}",
                })
        else:
            # 文字列: 全て同じでなければ矛盾
            unique = set(values)
            if len(unique) > 1:
                disagreements.append({
                    "claim_key": claim_key,
                    "agent_values": agent_values,
                    "reason": f"string_mismatch: {unique}",
                })

    return disagreements


def _get_ground_truth(claim_key: str) -> str | None:
    """claim_key に対応する実機 ground truth を返す。"""
    handler = _GROUND_TRUTH_HANDLERS.get(claim_key)
    if handler is None:
        return None
    try:
        return handler()
    except Exception as e:
        log.warning("ground_truth handler error for %s: %s", claim_key, e)
        return None


def _adjudicate(
    disagreement: dict[str, Any],
) -> dict[str, Any]:
    """
    矛盾に対して実機 ground truth を取得し、どの agent が正しいか判定する。
    Returns adjudicated dict with 'ground_truth', 'correct_agents', 'wrong_agents'.
    """
    claim_key = disagreement["claim_key"]
    agent_values = disagreement["agent_values"]

    gt = _get_ground_truth(claim_key)
    result = {**disagreement, "ground_truth": gt}

    if gt is None:
        result["verdict"] = "ground_truth_unavailable"
        result["correct_agents"] = []
        result["wrong_agents"] = []
        return result

    correct = []
    wrong = []
    for agent, val in agent_values.items():
        # 数値比較
        try:
            if abs(float(val) - float(gt)) / max(abs(float(gt)), 1) < 0.1:
                correct.append(agent)
            else:
                wrong.append(agent)
        except ValueError:
            if str(val).lower() == str(gt).lower():
                correct.append(agent)
            else:
                wrong.append(agent)

    result["verdict"] = "adjudicated"
    result["correct_agents"] = correct
    result["wrong_agents"] = wrong
    return result


def _log_disagreement(adj: dict[str, Any]) -> None:
    """adjudicated disagreement を JSONL に記録する。"""
    _DISAGREEMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        **adj,
    }
    with open(_DISAGREEMENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _send_alert(adj: dict[str, Any]) -> None:
    """矛盾を Pushover で通知する。"""
    try:
        from common.pushover_client import send, LEVEL_CRITICAL

        claim_key = adj["claim_key"]
        agent_values = adj["agent_values"]
        gt = adj.get("ground_truth", "N/A")
        wrong = adj.get("wrong_agents", [])

        title = f"[ALERT] Agent disagreement: {claim_key}"
        lines = [f"claim_key: {claim_key}"]
        for ag, val in agent_values.items():
            mark = "WRONG" if ag in wrong else "OK"
            lines.append(f"  {ag}: {val} [{mark}]")
        lines.append(f"ground_truth: {gt}")
        if wrong:
            lines.append(f"疑義agent: {', '.join(wrong)}")

        send(title, "\n".join(lines), priority=1, app_tag="ALERT", level=LEVEL_CRITICAL)
    except Exception as e:
        log.error("Pushover alert failed: %s", e)


def reconcile(*, alert: bool = True) -> list[dict[str, Any]]:
    """
    メイン reconcile サイクル。
    矛盾 + adjudication 結果のリストを返す。
    """
    records = _load_recent_claims()
    matrix = _build_matrix(records)
    disagreements = _detect_disagreements(matrix)

    adjudicated: list[dict[str, Any]] = []
    for d in disagreements:
        adj = _adjudicate(d)
        _log_disagreement(adj)
        adjudicated.append(adj)
        log.warning(
            "DISAGREEMENT detected: key=%s agents=%s gt=%s wrong=%s",
            adj["claim_key"],
            adj["agent_values"],
            adj.get("ground_truth"),
            adj.get("wrong_agents"),
        )
        if alert:
            _send_alert(adj)

    if not disagreements:
        log.info("reconcile: no disagreements (matrix keys=%s)", list(matrix.keys()))

    return adjudicated


# ---- Agent claim 書き込みヘルパー ------------------------------------------

def write_claim(
    agent: str,
    claim_key: str,
    claim_value: str,
    context: str = "",
) -> None:
    """
    Agent が自分の観測結果を記録する。
    呼び出し元: ops/redteam/analyst/builder が観測後すぐに呼ぶ。

    Example:
        from scripts.claim_consistency_checker import write_claim
        write_claim("ops", "atlas_status", "active", "heartbeat ts=2026-04-21T01:00:00")
    """
    _CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    path = _CLAIMS_DIR / f"{agent}_{today}.jsonl"
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "claim_key": claim_key,
        "claim_value": str(claim_value),
        "context": context,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---- Smoke test ------------------------------------------------------------

def run_smoke_test() -> bool:
    """
    2 agent に「Atlas 発注件数」を投げ、意図的な矛盾を検知できるか確認する。
    True = detector 正常動作。
    """
    import tempfile
    import shutil

    log.info("=== smoke test start ===")

    # 一時ディレクトリに切り替え
    original_claims_dir = _CLAIMS_DIR
    tmp = Path(tempfile.mkdtemp())
    tmp_claims = tmp / "agent_claims"
    tmp_claims.mkdir()
    tmp_log = tmp / "agent_disagreement_log.jsonl"

    # モンキーパッチ
    import scripts.claim_consistency_checker as _self
    _self._CLAIMS_DIR = tmp_claims
    _self._DISAGREEMENT_LOG = tmp_log

    results: list[str] = []

    try:
        # --- Case 1: ops vs redteam で atlas_status が矛盾 ---
        write_claim("ops", "atlas_status", "active", "smoke: ops sees active")
        write_claim("redteam", "atlas_status", "down", "smoke: redteam sees down")

        # --- Case 2: ops vs analyst で trade_count が一致 (矛盾なし) ---
        write_claim("ops", "trade_count", "5", "smoke: ops count=5")
        write_claim("analyst", "trade_count", "5", "smoke: analyst count=5")

        # --- Case 3: ops vs builder で trade_count が大幅乖離 ---
        write_claim("ops", "trade_count_v2", "3", "smoke: ops count=3")
        write_claim("builder", "trade_count_v2", "100", "smoke: builder count=100")

        # reconcile (alert=False でPushover不使用)
        adjs = reconcile(alert=False)

        # 検証
        keys_found = {a["claim_key"] for a in adjs}

        assert "atlas_status" in keys_found, "atlas_status disagreement not detected"
        assert "trade_count" not in keys_found, "trade_count false positive"
        assert "trade_count_v2" in keys_found, "trade_count_v2 disagreement not detected"

        # wrong_agents 検証 (ground truth が実機で取れた場合のみ)
        atlas_adj = next(a for a in adjs if a["claim_key"] == "atlas_status")
        assert atlas_adj["agent_values"]["ops"] == "active"
        assert atlas_adj["agent_values"]["redteam"] == "down"

        results.append("PASS: atlas_status disagreement detected")
        results.append("PASS: trade_count no false positive")
        results.append("PASS: trade_count_v2 numeric spread detected")

        log.info("smoke test PASSED: %s", results)
        return True

    except AssertionError as e:
        log.error("smoke test FAILED: %s", e)
        return False
    finally:
        # 復元
        _self._CLAIMS_DIR = original_claims_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ---- Daemon モード ----------------------------------------------------------

def daemon_loop() -> None:
    """5分毎に reconcile を実行する常駐ループ。"""
    log.info("daemon started (interval=%ds)", _RECONCILE_INTERVAL_SEC)
    while True:
        try:
            reconcile()
        except Exception as e:
            log.error("reconcile error: %s", e)
        time.sleep(_RECONCILE_INTERVAL_SEC)


# ---- CLI -------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-agent disagreement detector")
    parser.add_argument("--daemon", action="store_true", help="5分毎常駐モード")
    parser.add_argument("--smoke-test", action="store_true", help="smoke test 実行")
    parser.add_argument(
        "--write-claim",
        nargs=4,
        metavar=("AGENT", "KEY", "VALUE", "CONTEXT"),
        help="claim を書き込む (単発)",
    )
    args = parser.parse_args()

    if args.smoke_test:
        ok = run_smoke_test()
        sys.exit(0 if ok else 1)

    if args.write_claim:
        agent, key, value, context = args.write_claim
        write_claim(agent, key, value, context)
        log.info("wrote claim: agent=%s key=%s value=%s", agent, key, value)
        return

    if args.daemon:
        daemon_loop()
    else:
        adjs = reconcile()
        if adjs:
            log.warning("disagreements found: %d", len(adjs))
            sys.exit(2)


if __name__ == "__main__":
    main()
