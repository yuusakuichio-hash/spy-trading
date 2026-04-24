#!/usr/bin/env python3
"""
scripts/spx_paper_smoke_test_20260427.py
=========================================
目的: moomoo paper 口座 (acc_id=1173421) で SPX index option の
      SIMULATE 発注が受け付けられるか否かを公式実機で決着させる。

memory に「SPX paper 非対応」という未確認記録があるが、
このスクリプトの ret_code + msg で一次情報として上書きする。

設計原則（副作用最小化）:
  - deep OTM (現価~7160 → strike 9000) × limit price 0.01 → 絶対約定しない
  - 発注成功 → 即キャンセル（5 秒以内）
  - 失敗 → エラーメッセージを stdout + Pushover に全文記録
  - 全パターン終了後にサマリーを Pushover priority=1 で送信

フォールバック順序:
  Phase 1: SPX  monthly (US.SPX260515C09000000 / US.SPX260619C09000000)
  Phase 2: XSP  monthly (US.XSP260515C00900000) — SPX が NG だった場合
  Phase 3: SPY  monthly (US.SPY260515C00700000) — 対照実験（必ず成功するはず）

実行条件:
  - OpenD が 127.0.0.1:11111 で起動済み + paper 口座ログイン済み
  - 月曜 ET 9:30 以降（市場オープン中）が推奨（市場データ取得可）
  - Pre-market でも place_order 自体は SUBMITTED になれば ret=RET_OK

Usage:
  python3 scripts/spx_paper_smoke_test_20260427.py
  python3 scripts/spx_paper_smoke_test_20260427.py --phase spx
  python3 scripts/spx_paper_smoke_test_20260427.py --phase xsp
  python3 scripts/spx_paper_smoke_test_20260427.py --phase spy
  python3 scripts/spx_paper_smoke_test_20260427.py --all        # 全 phase 順番に実行
  python3 scripts/spx_paper_smoke_test_20260427.py --dry-run    # 接続・アカウント確認のみ（発注しない）

出力:
  stdout: 全詳細ログ
  Pushover: サマリー（priority=1）+ 発注失敗時の error msg（priority=1）
  終了コード:
    0 = SPX paper 対応確認 (Phase 1 成功)
    1 = SPX NG・XSP OK
    2 = SPX/XSP 両方 NG・SPY OK（対照成功）
    3 = 全 phase 失敗（接続問題の可能性）
    4 = dry-run 完了
    99 = 接続エラー / 前提条件未充足

注意:
  - OrderType.NORMAL = futu の US 株式 limit 注文
  - cancel_order は futu-api に存在しない → modify_order(ModifyOrderOp.CANCEL)
  - acc_id=1173421 は US SIMULATE（検証済み）
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ─── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("spx_paper_smoke")

# ─── .env loader ──────────────────────────────────────────────────────────────
def _load_env() -> None:
    for candidate in [
        Path("/root/spxbot/.env"),
        Path(__file__).parent.parent / ".env",
    ]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
            break

_load_env()

# ─── futu imports ─────────────────────────────────────────────────────────────
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
    log.error("futu-api import 失敗: %s  (pip install futu-api)", e)
    sys.exit(99)

# ─── 定数 ─────────────────────────────────────────────────────────────────────
OPEND_HOST    = "127.0.0.1"
OPEND_PORT    = 11111
ACC_ID        = 1173421          # US SIMULATE（公式確認済）
TRADE_ENV     = TrdEnv.SIMULATE
SECURITY_FIRM = SecurityFirm.FUTUSECURITIES
MARKET        = TrdMarket.US

# 発注パラメータ（副作用最小化）
LIMIT_PRICE   = 0.01             # 絶対約定しない最小 premium
QTY           = 1                # 1 contract
CANCEL_WAIT_S = 2                # 発注 → cancel までの待機（秒）

# オプションコード（futu 形式: US.ROOT[YYMMDD][C/P][STRIKE*1000 8桁]）
# SPX monthly: 3rd Friday of month → May 2026=05-15, Jun 2026=06-19
# XSP = 1/10 SPX; deep OTM strike = 900 (XSP ~716 相当時)
# SPY deep OTM = 700 (SPY ~570 相当時)
CANDIDATES: list[dict] = [
    {
        "phase":   "spx_may",
        "label":   "SPX 2026-05-15 C9000",
        "code":    "US.SPX260515C09000000",
        "group":   "spx",
    },
    {
        "phase":   "spx_jun",
        "label":   "SPX 2026-06-19 C9000",
        "code":    "US.SPX260619C09000000",
        "group":   "spx",
    },
    {
        "phase":   "xsp_may",
        "label":   "XSP 2026-05-15 C900",
        "code":    "US.XSP260515C00900000",
        "group":   "xsp",
    },
    {
        "phase":   "xsp_jun",
        "label":   "XSP 2026-06-19 C900",
        "code":    "US.XSP260619C00900000",
        "group":   "xsp",
    },
    {
        "phase":   "spy_may",
        "label":   "SPY 2026-05-15 C700 (対照)",
        "code":    "US.SPY260515C00700000",
        "group":   "spy",
    },
]

# ─── Pushover ─────────────────────────────────────────────────────────────────
def _pushover(title: str, message: str, priority: int = 0) -> None:
    """シンプル直接送信（common.pushover_client を避けて副作用最小化）。"""
    token = os.environ.get("PUSHOVER_TOKEN", "")
    user  = os.environ.get("PUSHOVER_USER", "")
    if not token or not user:
        log.warning("Pushover 設定なし（PUSHOVER_TOKEN / PUSHOVER_USER）→ ログのみ")
        return
    try:
        import requests  # type: ignore[import]
        payload: dict = {
            "token":    token,
            "user":     user,
            "title":    title,
            "message":  message[:1024],
            "priority": priority,
        }
        if priority == 2:
            payload["retry"]  = 60
            payload["expire"] = 3600
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("Pushover HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("Pushover 送信エラー: %s", exc)


# ─── キャンセル ────────────────────────────────────────────────────────────────
def _cancel_order(ctx: OpenSecTradeContext, order_id: int, label: str) -> bool:
    """modify_order(CANCEL) で注文取消し（futu に cancel_order は存在しない）。"""
    log.info("[%s] キャンセル試行: order_id=%s", label, order_id)
    ret, data = ctx.modify_order(
        modify_order_op=ModifyOrderOp.CANCEL,
        order_id=order_id,
        qty=0,
        price=0,
        trd_env=TRADE_ENV,
        acc_id=ACC_ID,
    )
    if ret == RET_OK:
        log.info("[%s] キャンセル OK", label)
        return True
    log.error("[%s] キャンセル失敗: ret=%s data=%s", label, ret, data)
    return False


# ─── 発注テスト 1 件 ──────────────────────────────────────────────────────────
class SmokeResult:
    """1 候補の発注テスト結果。"""

    def __init__(
        self,
        label: str,
        code: str,
        *,
        order_ok: bool,
        order_id: Optional[int],
        ret_code: int,
        ret_msg: str,
        cancel_ok: bool,
        elapsed_s: float,
        note: str = "",
    ) -> None:
        self.label     = label
        self.code      = code
        self.order_ok  = order_ok
        self.order_id  = order_id
        self.ret_code  = ret_code
        self.ret_msg   = ret_msg
        self.cancel_ok = cancel_ok
        self.elapsed_s = elapsed_s
        self.note      = note

    @property
    def supported(self) -> bool:
        """RET_OK で注文が受理された = paper 口座でその銘柄が取引可能。"""
        return self.order_ok

    def summary_line(self) -> str:
        status = "OK" if self.order_ok else "NG"
        cancel = "canceled" if self.cancel_ok else "cancel_fail"
        return (
            f"[{status}] {self.label} ({self.code})"
            f"  ret={self.ret_code}  {self.elapsed_s:.1f}s"
            + (f"  order_id={self.order_id}  {cancel}" if self.order_ok else "")
            + (f"  msg={self.ret_msg[:120]}" if not self.order_ok else "")
        )


def _test_one(ctx: OpenSecTradeContext, candidate: dict, dry_run: bool) -> SmokeResult:
    """1 オプション候補に対して発注→即キャンセルを実行して結果を返す。"""
    label = candidate["label"]
    code  = candidate["code"]
    log.info("=" * 60)
    log.info("[SMOKE] %s  code=%s  price=%.2f  qty=%d", label, code, LIMIT_PRICE, QTY)

    if dry_run:
        log.info("[DRY-RUN] 発注スキップ")
        return SmokeResult(
            label=label, code=code,
            order_ok=False, order_id=None,
            ret_code=-999, ret_msg="dry-run",
            cancel_ok=False, elapsed_s=0.0,
            note="dry-run",
        )

    t0 = time.monotonic()
    try:
        ret, data = ctx.place_order(
            price=LIMIT_PRICE,
            qty=QTY,
            code=code,
            trd_side=TrdSide.BUY,
            order_type=OrderType.NORMAL,  # US limit 注文 (LIMIT は存在しない)
            trd_env=TRADE_ENV,
            acc_id=ACC_ID,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        log.error("[%s] place_order 例外: %s", label, exc)
        return SmokeResult(
            label=label, code=code,
            order_ok=False, order_id=None,
            ret_code=-1, ret_msg=str(exc),
            cancel_ok=False, elapsed_s=elapsed,
            note="exception",
        )

    elapsed = time.monotonic() - t0
    log.info("[%s] place_order ret=%s  elapsed=%.2fs", label, ret, elapsed)

    if ret != RET_OK:
        # NG: ret_msg をフル記録
        msg_str = str(data)
        log.error("[%s] 発注 NG: %s", label, msg_str[:300])
        return SmokeResult(
            label=label, code=code,
            order_ok=False, order_id=None,
            ret_code=ret, ret_msg=msg_str,
            cancel_ok=False, elapsed_s=elapsed,
        )

    # OK: order_id を取得して即キャンセル
    try:
        order_id = int(data["order_id"].iloc[0])
    except Exception as exc:
        log.warning("[%s] order_id 取得失敗: %s  data=%s", label, exc, data)
        order_id = -1

    log.info("[%s] 発注 OK: order_id=%d  data=\n%s", label, order_id, data.to_string())

    # 即キャンセル（副作用最小化: 約定前に消す）
    time.sleep(CANCEL_WAIT_S)
    cancel_ok = _cancel_order(ctx, order_id, label)

    return SmokeResult(
        label=label, code=code,
        order_ok=True, order_id=order_id,
        ret_code=ret, ret_msg="",
        cancel_ok=cancel_ok, elapsed_s=elapsed,
    )


# ─── アカウント確認 ────────────────────────────────────────────────────────────
def _check_account(ctx: OpenSecTradeContext) -> bool:
    """アカウント一覧・残高確認（接続前提条件チェック）。"""
    log.info("=== アカウント確認 ===")
    ret, data = ctx.get_acc_list()
    if ret != RET_OK:
        log.error("get_acc_list 失敗: %s", data)
        return False

    sim_rows = data[data["trd_env"] == "SIMULATE"]
    if sim_rows.empty:
        log.error("SIMULATE 口座が見つからない\n%s", data.to_string())
        return False

    log.info("SIMULATE 口座確認 OK:\n%s",
             sim_rows[["acc_id", "trd_env", "security_firm", "trdmarket_auth"]].to_string())

    ret2, acc_info = ctx.accinfo_query(trd_env=TRADE_ENV, acc_id=ACC_ID)
    if ret2 == RET_OK:
        power = acc_info["power"].iloc[0] if "power" in acc_info.columns else "N/A"
        log.info("購買力: %s", power)
    return True


# ─── メイン ────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="SPX paper smoke test")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--phase",
        choices=["spx", "xsp", "spy", "spx_may", "spx_jun", "xsp_may", "xsp_jun", "spy_may"],
        help="実行する phase（group 指定 or 個別 phase 指定）",
    )
    group.add_argument("--all", action="store_true", help="全 phase を順番に実行")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="接続・アカウント確認のみ実行（発注しない）",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        default=True,
        help="Pushover 通知 ON（デフォルト有効）",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Pushover 通知 OFF",
    )
    args = parser.parse_args()
    notify = not args.no_notify

    # 実行対象候補を選択
    if args.all or args.dry_run:
        targets = CANDIDATES
    elif args.phase:
        # group マッチ ("spx" → spx_may + spx_jun) / 個別 phase マッチ
        targets = [c for c in CANDIDATES
                   if c["group"] == args.phase or c["phase"] == args.phase]
        if not targets:
            log.error("phase '%s' に対応する候補なし", args.phase)
            return 99
    else:
        # デフォルト: SPX → XSP → SPY の全 phase
        targets = CANDIDATES

    log.info("=== SPX paper smoke test 開始 ===")
    log.info("実行候補数: %d  dry_run=%s  notify=%s",
             len(targets), args.dry_run, notify)
    log.info("acc_id=%d  trd_env=SIMULATE  limit_price=%.2f  qty=%d",
             ACC_ID, LIMIT_PRICE, QTY)

    # OpenD 接続
    log.info("OpenD 接続: %s:%d", OPEND_HOST, OPEND_PORT)
    try:
        ctx = OpenSecTradeContext(
            filter_trdmarket=MARKET,
            host=OPEND_HOST,
            port=OPEND_PORT,
            security_firm=SECURITY_FIRM,
        )
    except Exception as exc:
        log.error("OpenD 接続失敗: %s", exc)
        if notify:
            _pushover(
                "[SPX SMOKE] 接続失敗",
                f"OpenD {OPEND_HOST}:{OPEND_PORT} 接続失敗\n{exc}",
                priority=1,
            )
        return 99

    try:
        # アカウント確認
        if not _check_account(ctx):
            if notify:
                _pushover(
                    "[SPX SMOKE] アカウント確認失敗",
                    f"acc_id={ACC_ID} SIMULATE 口座確認失敗",
                    priority=1,
                )
            return 99

        if args.dry_run:
            log.info("=== DRY-RUN 完了（発注なし）===")
            if notify:
                _pushover(
                    "[SPX SMOKE] dry-run 完了",
                    f"接続 OK / アカウント確認 OK\nacc_id={ACC_ID}\n発注はスキップ",
                    priority=0,
                )
            return 4

        # 発注テスト実行
        results: list[SmokeResult] = []
        for candidate in targets:
            result = _test_one(ctx, candidate, dry_run=False)
            results.append(result)
            # 発注失敗は即 Pushover（エラーメッセージを全文保存するため）
            if not result.order_ok and notify:
                _pushover(
                    f"[SPX SMOKE] NG: {result.label}",
                    f"ret={result.ret_code}\n{result.ret_msg[:800]}",
                    priority=1,
                )
            # フェーズ間インターバル（rate limit 対策）
            time.sleep(1)

    finally:
        ctx.close()
        log.info("OpenD コンテキスト close")

    # ─── サマリー ──────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("=== SMOKE TEST サマリー ===")
    spx_ok  = any(r.order_ok for r in results if r.code.startswith("US.SPX"))
    xsp_ok  = any(r.order_ok for r in results if r.code.startswith("US.XSP"))
    spy_ok  = any(r.order_ok for r in results if r.code.startswith("US.SPY"))

    lines = [f"実行日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S JST')}"]
    lines.append(f"acc_id={ACC_ID}  SIMULATE  limit=0.01  qty=1")
    lines.append("")
    for r in results:
        log.info(r.summary_line())
        lines.append(r.summary_line())
    lines.append("")

    # 判定
    if spx_ok:
        verdict = "SPX paper 対応: 確認 (SUPPORTED)"
        exit_code = 0
    elif xsp_ok:
        verdict = "SPX paper 非対応 / XSP paper 対応: 確認 (XSP FALLBACK)"
        exit_code = 1
    elif spy_ok:
        verdict = "SPX/XSP 非対応 / SPY 対応: 確認 (SPX INDEX OPTION UNSUPPORTED)"
        exit_code = 2
    else:
        verdict = "全 phase 失敗 (接続問題 or 全銘柄未対応)"
        exit_code = 3

    lines.append(f"判定: {verdict}")
    log.info("判定: %s  exit_code=%d", verdict, exit_code)

    if notify:
        _pushover(
            f"[SPX SMOKE] {'OK' if exit_code == 0 else 'NG'}: {verdict}",
            "\n".join(lines),
            priority=1,
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
