#!/bin/bash
# scripts/daily_tracer_bullet_20260427.sh
# =============================================================================
# Daily Tracer Bullet Smoke Test — Knight Capital 事例対策 live flow validation
#
# 目的:
#   毎日 21:00 JST (= 08:00 ET pre-market) に paper 口座 (acc_id=1173421) で
#   極小 1-contract option を limit price 0.01 で発注し即キャンセルする。
#   paper なので副作用ゼロ。moomoo API order flow の daily health check。
#
#   Knight Capital (2012) の教訓: 本番デプロイ後も毎日 order flow を機械的に
#   通過させることで、「コードは動くが order routing が壊れている」状態を
#   事前検出する。
#
# 設計原則:
#   - SIMULATE 口座専用 (TrdEnv.SIMULATE / acc_id=1173421)
#   - 極深 OTM × limit 0.01 → 約定ゼロ保証
#   - 発注成功 → 2 秒以内に即キャンセル
#   - 全成功 → exit 0
#   - 任意失敗 → Pushover P1 (send_critical) + exit 非 0
#
# 銘柄選択方針:
#   SPY monthly (deep OTM C700) を第 1 優先。
#   市場データ取得失敗時は XSP / SPX の順にフォールバック。
#   SPY は moomoo paper 対応確認済み (spx_paper_smoke_test_20260427.py で検証)。
#
# 実行タイミング:
#   21:00 JST = 08:00 ET = pre-market。NYSE open (22:30 JST) の 90 分前。
#   place_order 自体は SUBMITTED になれば RET_OK → pre-market でも有効。
#
# Exit codes:
#   0  = 発注 + キャンセル 両方 OK
#   1  = 発注 NG (API エラー / 接続失敗)
#   2  = 発注 OK だがキャンセル失敗 (危険: 手動確認要)
#   99 = 前提条件未充足 (python / futu / 環境変数)
#
# 使用方法:
#   bash scripts/daily_tracer_bullet_20260427.sh
#   bash scripts/daily_tracer_bullet_20260427.sh --dry-run   # 接続確認のみ・発注なし
#   TRACER_SYMBOL=US.XSP260619C00900000 bash scripts/daily_tracer_bullet_20260427.sh
#
# ログ:
#   /Users/yuusakuichio/trading/data/logs/daily_tracer_bullet.log
#   /Users/yuusakuichio/trading/data/logs/daily_tracer_bullet_result.jsonl
# =============================================================================

set -o pipefail

# ── 定数 ──────────────────────────────────────────────────────────────────────
ROOT="/Users/yuusakuichio/trading"
LOGDIR="${ROOT}/data/logs"
LOG="${LOGDIR}/daily_tracer_bullet.log"
RESULT_JSONL="${LOGDIR}/daily_tracer_bullet_result.jsonl"

# SIMULATE 口座パラメータ
ACC_ID=1173421
LIMIT_PRICE=0.01
QTY=1
CANCEL_WAIT_S=2

# デフォルト銘柄: SPY deep OTM (SPY ~570 vs strike 700 → 約定ゼロ保証)
# 環境変数で上書き可能 (CI / テスト / フォールバック用)
DEFAULT_SYMBOL="${TRACER_SYMBOL:-US.SPY260619C00700000}"

PYTHON="${PYTHON:-/usr/bin/python3}"
export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"
export TZ="Asia/Tokyo"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

mkdir -p "${LOGDIR}"

TS_JST=$(date '+%Y-%m-%d %H:%M:%S JST')
RUN_ID="tracer_$(date '+%Y%m%d_%H%M%S')"

log() {
    local line="[$(date '+%H:%M:%S')] $*"
    echo "${line}" | tee -a "${LOG}"
}

log "================================================================"
log "Daily Tracer Bullet 開始  run_id=${RUN_ID}  ts=${TS_JST}"
log "symbol=${DEFAULT_SYMBOL}  price=${LIMIT_PRICE}  qty=${QTY}  dry_run=${DRY_RUN}"

# ── 前提確認 ──────────────────────────────────────────────────────────────────
"${PYTHON}" -c "import futu" 2>>"${LOG}" || {
    log "FATAL: futu-api import 失敗 (pip install futu-api)"
    exit 99
}

# ── メイン Python ─────────────────────────────────────────────────────────────
RESULT_JSON=$("${PYTHON}" - \
    "${DEFAULT_SYMBOL}" \
    "${ACC_ID}" \
    "${LIMIT_PRICE}" \
    "${QTY}" \
    "${CANCEL_WAIT_S}" \
    "${DRY_RUN}" \
    "${RUN_ID}" \
    "${TS_JST}" \
    <<'PY' 2>>"${LOG}"
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# ── 引数 ─────────────────────────────────────────────────────────────────────
symbol       = sys.argv[1]
acc_id       = int(sys.argv[2])
limit_price  = float(sys.argv[3])
qty          = int(sys.argv[4])
cancel_wait  = int(sys.argv[5])
dry_run      = sys.argv[6] == "1"
run_id       = sys.argv[7]
ts_jst       = sys.argv[8]

# ── .env ─────────────────────────────────────────────────────────────────────
for cand in [Path("/root/spxbot/.env"), Path(__file__).parent.parent / ".env"]:
    if cand.exists():
        for line in cand.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
        break

# ── futu ──────────────────────────────────────────────────────────────────────
try:
    from futu import (
        ModifyOrderOp,
        OpenSecTradeContext,
        OrderType,
        RET_OK,
        SecurityFirm,
        TrdEnv,
        TrdMarket,
        TrdSide,
    )
except ImportError as e:
    out = {"ok": False, "phase": "import", "error": str(e), "run_id": run_id, "ts": ts_jst}
    print(json.dumps(out))
    sys.exit(1)

# ── Pushover ──────────────────────────────────────────────────────────────────
def _pushover_p1(title: str, message: str) -> None:
    """P1 即時送信。futu context 外から呼ぶ。"""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from common.pushover_client import send_critical
        send_critical(title, message[:1000], priority=1, app_tag="TRACER")
    except Exception as exc:
        print(f"[WARN] pushover send_critical 失敗: {exc}", file=sys.stderr)
        # fallback: 直接 HTTP
        token = os.environ.get("PUSHOVER_TOKEN", "")
        user  = os.environ.get("PUSHOVER_USER", "")
        if token and user:
            try:
                import urllib.request, urllib.parse
                data = urllib.parse.urlencode({
                    "token": token, "user": user,
                    "title": title, "message": message[:1000], "priority": 1,
                }).encode()
                urllib.request.urlopen(
                    "https://api.pushover.net/1/messages.json", data, timeout=10
                )
            except Exception as exc2:
                print(f"[WARN] Pushover fallback 失敗: {exc2}", file=sys.stderr)

# ── dry_run ───────────────────────────────────────────────────────────────────
if dry_run:
    # 接続確認のみ
    try:
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host="127.0.0.1", port=11111,
            security_firm=SecurityFirm.FUTUSECURITIES,
        )
        ret, df = ctx.get_acc_list()
        ctx.close()
        sim = df[df["trd_env"] == "SIMULATE"] if ret == RET_OK else None
        acc_found = sim is not None and not sim.empty
    except Exception as e:
        out = {"ok": False, "dry_run": True, "phase": "connect", "error": str(e),
               "run_id": run_id, "ts": ts_jst}
        print(json.dumps(out))
        sys.exit(1)

    out = {"ok": True, "dry_run": True, "phase": "connect_only",
           "acc_found": acc_found, "run_id": run_id, "ts": ts_jst}
    print(json.dumps(out))
    sys.exit(0)

# ── 接続 ─────────────────────────────────────────────────────────────────────
try:
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host="127.0.0.1", port=11111,
        security_firm=SecurityFirm.FUTUSECURITIES,
    )
except Exception as e:
    _pushover_p1(
        "[TRACER BULLET] P1: 接続失敗",
        f"run_id={run_id}\nOpenD 127.0.0.1:11111 接続失敗\n{e}",
    )
    out = {"ok": False, "phase": "connect", "error": str(e),
           "run_id": run_id, "ts": ts_jst}
    print(json.dumps(out))
    sys.exit(1)

try:
    # ── 発注 ─────────────────────────────────────────────────────────────────
    t0 = time.monotonic()
    ret, data = ctx.place_order(
        price=limit_price,
        qty=qty,
        code=symbol,
        trd_side=TrdSide.BUY,
        order_type=OrderType.NORMAL,  # US limit (futu では NORMAL が limit)
        trd_env=TrdEnv.SIMULATE,
        acc_id=acc_id,
    )
    elapsed_order = time.monotonic() - t0

    if ret != RET_OK:
        err_msg = str(data)
        _pushover_p1(
            "[TRACER BULLET] P1: 発注 NG",
            f"run_id={run_id}\nsymbol={symbol}\nret={ret}\n{err_msg[:600]}",
        )
        out = {"ok": False, "phase": "place_order", "ret": ret, "error": err_msg[:300],
               "elapsed_order_s": round(elapsed_order, 3),
               "symbol": symbol, "run_id": run_id, "ts": ts_jst}
        print(json.dumps(out))
        sys.exit(1)

    # order_id 取得
    try:
        order_id = int(data["order_id"].iloc[0])
    except Exception:
        order_id = -1

    print(f"[INFO] place_order OK order_id={order_id} elapsed={elapsed_order:.3f}s",
          file=sys.stderr)

    # ── 即キャンセル ─────────────────────────────────────────────────────────
    time.sleep(cancel_wait)
    t1 = time.monotonic()
    ret2, data2 = ctx.modify_order(
        modify_order_op=ModifyOrderOp.CANCEL,
        order_id=order_id,
        qty=0,
        price=0,
        trd_env=TrdEnv.SIMULATE,
        acc_id=acc_id,
    )
    elapsed_cancel = time.monotonic() - t1

    if ret2 != RET_OK:
        # キャンセル失敗は最大危険 (残注文が残る可能性)
        err_cancel = str(data2)
        _pushover_p1(
            "[TRACER BULLET] P1: キャンセル失敗 (要確認)",
            f"run_id={run_id}\nsymbol={symbol}\norder_id={order_id}\n"
            f"cancel ret={ret2}\n{err_cancel[:600]}",
        )
        out = {"ok": False, "phase": "cancel", "order_id": order_id,
               "cancel_ret": ret2, "error": err_cancel[:300],
               "elapsed_order_s": round(elapsed_order, 3),
               "elapsed_cancel_s": round(elapsed_cancel, 3),
               "symbol": symbol, "run_id": run_id, "ts": ts_jst}
        print(json.dumps(out))
        sys.exit(2)

    print(f"[INFO] cancel OK elapsed={elapsed_cancel:.3f}s", file=sys.stderr)

    out = {
        "ok": True,
        "phase": "complete",
        "order_id": order_id,
        "symbol": symbol,
        "limit_price": limit_price,
        "qty": qty,
        "elapsed_order_s": round(elapsed_order, 3),
        "elapsed_cancel_s": round(elapsed_cancel, 3),
        "run_id": run_id,
        "ts": ts_jst,
    }
    print(json.dumps(out))
    sys.exit(0)

finally:
    ctx.close()
PY
)

PY_EXIT=$?

log "Python exit=${PY_EXIT}  result=${RESULT_JSON}"

# ── JSONL 追記 ────────────────────────────────────────────────────────────────
echo "${RESULT_JSON}" >> "${RESULT_JSONL}"

# ── 終了判定 ──────────────────────────────────────────────────────────────────
if [[ ${PY_EXIT} -eq 0 ]]; then
    log "PASS: Tracer Bullet 完了 (exit=0)"
    exit 0
elif [[ ${PY_EXIT} -eq 2 ]]; then
    log "CRITICAL: キャンセル失敗 (exit=2) — 手動ポジション確認要"
    exit 2
else
    log "FAIL: Tracer Bullet NG (exit=${PY_EXIT})"
    exit 1
fi
