#!/usr/bin/env python3
"""
common/bot_deviation_detector.py — Bot動作乖離検知モジュール (Sora Lab)

背景: ゆうさくさん指示 2026-04-20
「本番始まったら時間を割けない。想定乖離をリアルタイムで自律検知する仕組みを作れ」

4観点リアルタイム検知:
  観点A: 戦術パフォーマンス乖離 (20件到達後にBT期待値±50%超でアラート)
  観点B: エントリー構造整合性   (約定後5秒以内にポジション構造を verify)
  観点C: Greeks レンジ監視     (1分毎 snapshot で想定レンジ逸脱)
  観点D: 発注フロー異常         (レイテンシ/スリッページ/リジェクト率)

通知設計:
  - deviation 検知: Pushover priority=1 (即時)
  - 同一 deviation が ESCALATE_THRESHOLD 回連続: priority=2 + bot_halt_flag=True
  - decision_log.jsonl に全記録 (週次AARで分析)

ペーパー/本番切替:
  IS_LIVE=1 の場合は escalation 閾値を厳しくする (2回→本番halt)

使用例:
    from common.bot_deviation_detector import DeviationDetector, Deviation
    detector = DeviationDetector()
    dev = detector.check_performance_deviation("atlas", realized_pnl, bt_expected)
    if dev:
        detector.alert(dev)
"""
from __future__ import annotations

import json
import logging
import os
import time
import datetime
import zoneinfo
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ── パス定数 ─────────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parents[1]  # trading/
EXPECTATIONS_PATH = _BASE_DIR / "data" / "strategy_expectations.json"
DECISION_LOG_PATH = _BASE_DIR / "data" / "logs" / "decision_log.jsonl"
STATE_PATH        = _BASE_DIR / "data" / "deviation_detector_state.json"

# ── ロガー ───────────────────────────────────────────────────────────────────
log = logging.getLogger("bot_deviation_detector")
if not log.handlers:
    import sys
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] deviation_detector: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# ── Pushover 共通クライアント ─────────────────────────────────────────────────
try:
    from common import pushover_client as _pc
    _PC_AVAILABLE = True
except ImportError:
    try:
        import pushover_client as _pc  # type: ignore
        _PC_AVAILABLE = True
    except ImportError:
        _PC_AVAILABLE = False

# ── 環境定数 ─────────────────────────────────────────────────────────────────
IS_LIVE = os.environ.get("IS_LIVE", "0") == "1"

# 本番は厳しく (2回連続で halt)、ペーパーは緩く (5回)
ESCALATE_THRESHOLD = 2 if IS_LIVE else 5

# 観点A: パフォーマンス乖離を評価するのに必要な最小約定件数
MIN_TRADES_FOR_PERF_CHECK = int(os.environ.get("DEVIATION_MIN_TRADES", "20"))

JST = zoneinfo.ZoneInfo("Asia/Tokyo")


# ─────────────────────────────────────────────────────────────────────────────
# データクラス
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Deviation:
    """乖離検知結果。alert() に渡すことで通知・記録が行われる。"""
    perspective: str          # "A"/"B"/"C"/"D"
    bot_name: str             # "atlas" / "chronos"
    tactic: str               # 戦術名 (例: "cs_sell", "orb_buy")
    severity: str             # "WARNING" / "CRITICAL"
    title: str                # Pushover タイトル
    message: str              # Pushover 本文
    details: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts_jst"] = datetime.datetime.fromtimestamp(self.ts, tz=JST).isoformat()
        return d


# ─────────────────────────────────────────────────────────────────────────────
# 期待値ローダー
# ─────────────────────────────────────────────────────────────────────────────

def _load_expectations() -> dict[str, Any]:
    """strategy_expectations.json を読み込む。ファイルなければ空 dict。"""
    try:
        if EXPECTATIONS_PATH.exists():
            return json.loads(EXPECTATIONS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("expectations load error: %s", e)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# 状態管理 (連続発生カウンタ)
# ─────────────────────────────────────────────────────────────────────────────

def _load_state() -> dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("state load error: %s", e)
    return {"consecutive": {}, "bot_halt_flag": False}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("state save error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# メイン: DeviationDetector
# ─────────────────────────────────────────────────────────────────────────────

class DeviationDetector:
    """
    Bot動作乖離検知。4観点で継続監視し、乖離時に即通知。

    スレッドセーフ設計ではない（単一Botプロセスからの呼び出しを前提）。
    複数Botから同時に呼び出す場合は各Botのbot_nameを異なる値にすること。
    """

    def __init__(self):
        self._expectations: dict[str, Any] = _load_expectations()
        # トレードカウンタ: {tactic: {"count": int, "cumulative_pnl": float}}
        self._trade_stats: dict[str, dict[str, Any]] = {}

    def _get_tactic_expectation(self, tactic: str) -> dict[str, Any]:
        """期待値 dict を返す。不明戦術は空 dict。"""
        exp = self._expectations
        # Atlas 戦術 (トップレベル) → Chronos (chronos.tactic) の順で探す
        if tactic in exp:
            return exp[tactic]
        chronos = exp.get("chronos", {})
        if tactic in chronos:
            return chronos[tactic]
        return {}

    # ── 観点A: パフォーマンス乖離 ─────────────────────────────────────────────

    def check_performance_deviation(
        self,
        bot_name: str,
        tactic: str,
        realized_pnl: float,
        bt_expected_pnl: float,
        trade_count: int,
    ) -> Optional[Deviation]:
        """
        観点A: 累積PnL が BT期待値の ±(tolerance_pct)% を超えた場合に Deviation を返す。

        Parameters
        ----------
        bot_name       : "atlas" / "chronos"
        tactic         : 戦術名
        realized_pnl   : 累積実現PnL (ドル)
        bt_expected_pnl: BT基準の累積期待値 (ドル, 同件数換算)
        trade_count    : 約定件数 (MIN_TRADES_FOR_PERF_CHECK 未満はスキップ)
        """
        if trade_count < MIN_TRADES_FOR_PERF_CHECK:
            return None
        if bt_expected_pnl == 0:
            return None

        exp = self._get_tactic_expectation(tactic)
        tolerance = float(exp.get("tolerance_pct", 50)) / 100.0  # デフォルト50%

        deviation_ratio = (realized_pnl - bt_expected_pnl) / abs(bt_expected_pnl)

        if abs(deviation_ratio) <= tolerance:
            return None  # 許容範囲内

        direction = "下振れ" if deviation_ratio < 0 else "上振れ"
        severity = "CRITICAL" if abs(deviation_ratio) > tolerance * 2 else "WARNING"

        return Deviation(
            perspective="A",
            bot_name=bot_name,
            tactic=tactic,
            severity=severity,
            title=f"[{bot_name.upper()}/DEV-A] {tactic} パフォーマンス{direction}",
            message=(
                f"戦術: {tactic} | 件数: {trade_count}\n"
                f"BT期待値: {bt_expected_pnl:+.2f}$ → 実績: {realized_pnl:+.2f}$\n"
                f"乖離率: {deviation_ratio*100:+.1f}% (許容: ±{tolerance*100:.0f}%)"
            ),
            details={
                "realized_pnl": realized_pnl,
                "bt_expected_pnl": bt_expected_pnl,
                "deviation_ratio": round(deviation_ratio, 4),
                "tolerance": tolerance,
                "trade_count": trade_count,
            },
        )

    # ── 観点B: エントリー構造整合性 ───────────────────────────────────────────

    def check_entry_integrity(
        self,
        bot_name: str,
        tactic: str,
        order_id: str,
        expected_structure: dict[str, Any],
        actual_fills: list[dict[str, Any]],
    ) -> Optional[Deviation]:
        """
        観点B: strategy_selector が選んだ戦術と実際の建玉構造が一致するか検証。

        expected_structure: {
            "legs": 2,           # レグ数
            "credit": true,      # クレジット戦術 (net credit > 0)
            "width_ratio": [0.02, 0.05]  # ストライク幅 / 原資産価格
        }
        actual_fills: 各レグの約定情報 list
            [{"side": "sell"/"buy", "price": float, "qty": int, "strike": float}, ...]
        """
        exp_struct = expected_structure or self._get_tactic_expectation(tactic).get("entry_structure", {})

        if not exp_struct or not actual_fills:
            return None

        violations: list[str] = []

        # レグ数チェック
        expected_legs = exp_struct.get("legs")
        if expected_legs is not None and len(actual_fills) != expected_legs:
            violations.append(
                f"レグ数不一致: 期待={expected_legs} 実績={len(actual_fills)}"
            )

        # クレジット/デビットチェック
        expected_credit = exp_struct.get("credit")
        if expected_credit is not None and actual_fills:
            sells = sum(f.get("price", 0) * f.get("qty", 1)
                        for f in actual_fills if f.get("side") == "sell")
            buys  = sum(f.get("price", 0) * f.get("qty", 1)
                        for f in actual_fills if f.get("side") == "buy")
            net_credit = sells - buys
            is_credit_trade = net_credit > 0
            if is_credit_trade != expected_credit:
                violations.append(
                    f"Credit/Debit不一致: 期待={'credit' if expected_credit else 'debit'} "
                    f"実績={'credit' if is_credit_trade else 'debit'} (net={net_credit:.2f}$)"
                )

        if not violations:
            return None

        return Deviation(
            perspective="B",
            bot_name=bot_name,
            tactic=tactic,
            severity="CRITICAL",
            title=f"[{bot_name.upper()}/DEV-B] {tactic} エントリー構造違反",
            message=(
                f"注文ID: {order_id}\n"
                + "\n".join(f"・{v}" for v in violations)
            ),
            details={
                "order_id": order_id,
                "violations": violations,
                "expected_structure": exp_struct,
                "actual_fill_count": len(actual_fills),
            },
        )

    # ── 観点C: Greeks レンジ監視 ──────────────────────────────────────────────

    def check_greeks_range(
        self,
        bot_name: str,
        tactic: str,
        position_id: str,
        current_greeks: dict[str, float],
        expected_range: Optional[dict[str, Any]] = None,
    ) -> Optional[Deviation]:
        """
        観点C: ポジションの Greeks が戦術別想定レンジを逸脱していないか確認。

        current_greeks: {"delta": 0.1, "gamma": 0.002, "theta": -0.05, "vega": 0.3}
        expected_range: strategy_expectations.json から自動ロード。明示指定も可。
            {
              "expected_delta_range": [-0.15, 0.15],
              "expected_gamma_range": [-0.005, 0.005],
              ...
            }
        """
        exp = expected_range or self._get_tactic_expectation(tactic)
        if not exp:
            return None

        greek_checks = [
            ("delta", "expected_delta_range"),
            ("gamma", "expected_gamma_range"),
            ("theta", "expected_theta_range"),
            ("vega",  "expected_vega_range"),
        ]

        violations: list[str] = []
        for greek, range_key in greek_checks:
            rng = exp.get(range_key)
            if rng is None or greek not in current_greeks:
                continue
            val = current_greeks[greek]
            lo, hi = float(rng[0]), float(rng[1])
            if not (lo <= val <= hi):
                violations.append(
                    f"{greek}={val:.4f} 範囲外 [{lo:.4f}, {hi:.4f}]"
                )

        if not violations:
            return None

        # delta が大きく外れている = マーケット急変フラグ → CRITICAL
        delta_violation = any("delta=" in v for v in violations)
        severity = "CRITICAL" if delta_violation else "WARNING"

        return Deviation(
            perspective="C",
            bot_name=bot_name,
            tactic=tactic,
            severity=severity,
            title=f"[{bot_name.upper()}/DEV-C] {tactic} Greeks 範囲逸脱",
            message=(
                f"ポジション: {position_id}\n"
                + "\n".join(f"・{v}" for v in violations)
            ),
            details={
                "position_id": position_id,
                "current_greeks": current_greeks,
                "violations": violations,
            },
        )

    # ── 観点D: 発注フロー異常 ─────────────────────────────────────────────────

    def check_execution_anomaly(
        self,
        bot_name: str,
        tactic: str,
        order_id: str,
        submitted_at: float,
        filled_at: Optional[float],
        submitted_price: float,
        filled_price: Optional[float],
        status: str,
    ) -> Optional[Deviation]:
        """
        観点D: 発注→約定フローの異常を検知。

        Parameters
        ----------
        order_id       : 発注ID
        submitted_at   : 発注時刻 (unix timestamp)
        filled_at      : 約定時刻 (None = 未約定)
        submitted_price: 発注時の想定価格
        filled_price   : 約定価格 (None = 未約定)
        status         : "filled" / "rejected" / "cancelled" / "pending"
        """
        exp = self._get_tactic_expectation(tactic)
        violations: list[str] = []

        # リジェクト検知
        if status == "rejected":
            violations.append(f"発注リジェクト: order_id={order_id}")

        # レイテンシ異常 (30秒以上 = ブローカー問題の可能性)
        if filled_at is not None and submitted_at is not None:
            latency_sec = filled_at - submitted_at
            max_latency = float(exp.get("max_fill_latency_sec", 30))
            if latency_sec > max_latency:
                violations.append(
                    f"約定遅延: {latency_sec:.1f}秒 (閾値: {max_latency:.0f}秒)"
                )

        # スリッページ異常
        if filled_price is not None and submitted_price != 0:
            slippage_pct = abs(filled_price - submitted_price) / abs(submitted_price) * 100
            max_slippage = float(exp.get("max_slippage_pct", 2.0))
            if slippage_pct > max_slippage:
                violations.append(
                    f"スリッページ超過: {slippage_pct:.2f}% "
                    f"(期待={submitted_price:.4f}, 実績={filled_price:.4f}, 閾値={max_slippage:.1f}%)"
                )

        if not violations:
            return None

        severity = "CRITICAL" if status == "rejected" else "WARNING"

        return Deviation(
            perspective="D",
            bot_name=bot_name,
            tactic=tactic,
            severity=severity,
            title=f"[{bot_name.upper()}/DEV-D] 発注フロー異常",
            message=(
                f"注文ID: {order_id} | ステータス: {status}\n"
                + "\n".join(f"・{v}" for v in violations)
            ),
            details={
                "order_id": order_id,
                "status": status,
                "submitted_at": submitted_at,
                "filled_at": filled_at,
                "submitted_price": submitted_price,
                "filled_price": filled_price,
                "violations": violations,
            },
        )

    # ── アラート送信 ──────────────────────────────────────────────────────────

    def alert(self, deviation: Deviation) -> bool:
        """
        Deviation を Pushover + decision_log に記録する。
        連続発生が ESCALATE_THRESHOLD 回に達したら priority=2 + halt_flag=True。

        Returns: True = 送信成功 / False = 送信失敗（ログには必ず記録）
        """
        state = _load_state()

        # 連続カウンタ更新
        key = f"{deviation.bot_name}:{deviation.tactic}:{deviation.perspective}"
        consecutive = state.get("consecutive", {})
        count = consecutive.get(key, 0) + 1
        consecutive[key] = count
        state["consecutive"] = consecutive

        # エスカレーション判定
        escalated = count >= ESCALATE_THRESHOLD
        if escalated:
            state["bot_halt_flag"] = True
            priority = 2
            title_prefix = "[ESCALATE] "
        else:
            priority = 1
            title_prefix = ""

        _save_state(state)

        # decision_log に記録 (必ず実施)
        self._write_decision_log(deviation, count, escalated)

        # Pushover 送信
        title   = title_prefix + deviation.title
        message = deviation.message
        if escalated:
            message += f"\n\n!!! 連続{count}回検知 → Bot停止フラグ ON !!!"

        log.warning("[ALERT] %s: %s", title, deviation.message)

        if _PC_AVAILABLE:
            app_tag = "Atlas" if "atlas" in deviation.bot_name.lower() else "Chronos"
            ok = _pc.send(title, message, priority=priority, app_tag=app_tag)
            return ok

        log.warning("[ALERT] pushover_client not available — logged only")
        return False

    def _write_decision_log(self, deviation: Deviation, count: int, escalated: bool) -> None:
        """decision_log.jsonl に1行追記する。"""
        try:
            DECISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = deviation.to_dict()
            entry["consecutive_count"] = count
            entry["escalated"] = escalated
            entry["is_live"] = IS_LIVE
            with DECISION_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("decision_log write error: %s", e)

    # ── 補助: ポジション取得後の構造簡易チェック (観点B ショートカット) ───────

    def verify_ic_structure(
        self,
        bot_name: str,
        order_id: str,
        actual_fills: list[dict[str, Any]],
    ) -> Optional[Deviation]:
        """Iron Condor 特化の構造検証 (4レグ, credit)。"""
        return self.check_entry_integrity(
            bot_name=bot_name,
            tactic="ic_sell",
            order_id=order_id,
            expected_structure={"legs": 4, "credit": True},
            actual_fills=actual_fills,
        )

    def verify_cs_structure(
        self,
        bot_name: str,
        order_id: str,
        actual_fills: list[dict[str, Any]],
    ) -> Optional[Deviation]:
        """Credit Spread 特化の構造検証 (2レグ, credit)。"""
        return self.check_entry_integrity(
            bot_name=bot_name,
            tactic="cs_sell",
            order_id=order_id,
            expected_structure={"legs": 2, "credit": True},
            actual_fills=actual_fills,
        )

    # ── 補助: halt_flag 確認 ─────────────────────────────────────────────────

    @staticmethod
    def is_halt_flagged() -> bool:
        """bot_halt_flag が True の場合 True を返す。Bot ループ先頭でチェックする。"""
        state = _load_state()
        return bool(state.get("bot_halt_flag", False))

    @staticmethod
    def clear_halt_flag() -> None:
        """手動解除時に呼び出す。連続カウンタも全リセット。"""
        _save_state({"consecutive": {}, "bot_halt_flag": False})
        log.info("[HALT_FLAG] cleared")

    # ── 補助: 期待値リロード (ファイルが更新された場合) ──────────────────────

    def reload_expectations(self) -> None:
        """strategy_expectations.json を再読み込みする。"""
        self._expectations = _load_expectations()
        log.info("[RELOAD] expectations loaded: %d tactics", len(self._expectations))
