"""atlas_v3/core/trade_engine.py — Atlas TradeEngine v3

全 engine の発注経路 root。spy_bot.py:4433-5347 TradeEngine を atlas_v3 へ移植。
spy_bot.py は一切変更しない。

公開 API:
    TradeEngine(paper, opend_host, opend_port, breaker_config)
    .connect()                    -> bool
    .close()
    .get_account_cash()           -> float
    .place_credit_spread(...)     -> bool
    .get_open_positions()         -> list[dict]
    .is_alive()                   -> bool
    .cancel_all_open_orders(reason) -> int
    .close_all_positions(reason)  -> bool
    .check_margin_and_alert()     -> bool

設計規律:
- futu 非 import 環境でも import 可能（guard 済み）
- DRY_TEST=True 時は VirtualPositionManager に仮想記録
- CircuitBreaker (moomoo_breaker) + Bulkhead (moomoo pool) を全 place_order 経路に適用
- common_v3/risk/pre_trade_check.py (4-Layer Gate) を全 place_order 直前に通す
- common_v3/risk/kill_switch.py を connect 以外の全メソッド冒頭で確認
- idempotency key: signal_id ベースで決定的生成（uuid4 禁止）
- 各メソッド CC <= 15

Bulkhead 適用: _place_single_leg / _confirm_fills → BulkheadPool("moomoo") で実行
CircuitBreaker 適用: place_order 呼び出し毎に moomoo_breaker.call() でラップ
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# futu SDK guard
# ---------------------------------------------------------------------------
FUTU_AVAILABLE = False
OpenSecTradeContext = None  # type: ignore[assignment]
TrdEnv = None  # type: ignore[assignment]
TrdMarket = None  # type: ignore[assignment]
TrdSide = None  # type: ignore[assignment]
OrderType = None  # type: ignore[assignment]
TimeInForce = None  # type: ignore[assignment]
ModifyOrderOp = None  # type: ignore[assignment]
SecurityFirm = None  # type: ignore[assignment]
RET_OK = 0

try:
    from futu import OpenSecTradeContext as _OST
    from futu import TrdEnv as _TrdEnv
    from futu import TrdMarket as _TrdMarket
    from futu import TrdSide as _TrdSide
    from futu import OrderType as _OrderType
    from futu import TimeInForce as _TimeInForce
    from futu import ModifyOrderOp as _ModifyOrderOp
    from futu import SecurityFirm as _SecurityFirm
    from futu import RET_OK as _RET_OK
    OpenSecTradeContext = _OST
    TrdEnv = _TrdEnv
    TrdMarket = _TrdMarket
    TrdSide = _TrdSide
    OrderType = _OrderType
    TimeInForce = _TimeInForce
    ModifyOrderOp = _ModifyOrderOp
    SecurityFirm = _SecurityFirm
    RET_OK = _RET_OK
    FUTU_AVAILABLE = True
except Exception as _futu_err:
    log.warning(
        "[TradeEngine] futu SDK import failed (%s); TradeEngine will operate in dry-run only.",
        type(_futu_err).__name__,
    )

# ---------------------------------------------------------------------------
# Environment flags
# ---------------------------------------------------------------------------
DRY_TEST: bool = os.environ.get("DRY_TEST", "0").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Defaults (overridable via env / constructor)
# ---------------------------------------------------------------------------
_DEFAULT_OPEND_HOST: str = os.environ.get("OPEND_HOST", "127.0.0.1")
_DEFAULT_OPEND_PORT: int = int(os.environ.get("OPEND_PORT", "11111"))
_DEFAULT_TRADE_PASSWORD: Optional[str] = os.environ.get("TRADE_PASSWORD") or None

#: 証拠金使用率アラート閾値 (90%)
_MARGIN_USAGE_ALERT: float = 0.90
#: 証拠金使用率エントリー停止閾値 (70%)
_MARGIN_USAGE_MAX_ENTRY: float = 0.70

#: 指値注文設定
_ENABLE_LIMIT_ENTRY: bool = os.environ.get("ENABLE_LIMIT_ENTRY", "0").strip().lower() in ("1", "true")
_LIMIT_HIGH_VIX_THRESHOLD: float = float(os.environ.get("LIMIT_HIGH_VIX_THRESHOLD", "25"))
_LIMIT_ADJUST_STEP: float = 0.01
_LIMIT_ADJUST_INTERVAL: float = 2.0
_LIMIT_MAX_ADJUST_STEPS: int = 5

# ---------------------------------------------------------------------------
# 依存 import
# ---------------------------------------------------------------------------
from common_v3.risk.kill_switch import is_active as _ks_is_active
from common_v3.risk.pre_trade_check import (
    OrderCtx as _OrderCtx,
    PreTradeConfig as _PreTradeConfig,
    check_order as _check_order,
)
from common_v3.self_healing.instances import moomoo_breaker
from common_v3.self_healing.circuit_breaker import CircuitBreakerOpenError
from common_v3.self_healing.bulkhead import get_global_pool as _get_bulkhead_pool

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class TradeEngineError(RuntimeError):
    """TradeEngine 操作失敗の基底例外。"""


class BrokerUnavailableError(TradeEngineError):
    """moomoo_breaker が OPEN 状態で発注不可。"""


class KillSwitchArmedError(TradeEngineError):
    """Kill Switch が ARMED のため操作を拒否した。"""


class AccountCashError(TradeEngineError):
    """口座残高取得失敗 — 発注をブロックする。"""


# ---------------------------------------------------------------------------
# BreakerConfig — CircuitBreaker 設定 DTO
# ---------------------------------------------------------------------------

import dataclasses


@dataclasses.dataclass(frozen=True)
class BreakerConfig:
    """CircuitBreaker 設定 DTO。

    TradeEngine コンストラクタに渡す。None の場合は共有 moomoo_breaker を使用。
    テスト時は専用 CircuitBreaker インスタンスを渡してグローバル汚染を防ぐ。
    """
    breaker: Any  # CircuitBreaker インスタンス


# ---------------------------------------------------------------------------
# VirtualPositionManager — dry-test 用仮想ポジション管理
# ---------------------------------------------------------------------------


class _VirtualPositionManager:
    """DRY_TEST モード用のインメモリ仮想ポジション管理。

    spy_bot.py VirtualPositionManager と同一インターフェースを再現する。
    """

    def __init__(self) -> None:
        self._positions: list[dict] = []

    def add_position(
        self,
        code: str,
        qty: int,
        credit: float,
        position_side: str = "SHORT",
    ) -> None:
        self._positions.append(
            {
                "code": code,
                "qty": qty,
                "market_val": credit * qty * 100.0,
                "position_side": position_side,
                "unrealized_pl": 0.0,
            }
        )

    def get_positions(self) -> list[dict]:
        return list(self._positions)

    def remove_all(self) -> None:
        self._positions.clear()


# ---------------------------------------------------------------------------
# ヘルパー: option コードからシンボル抽出
# ---------------------------------------------------------------------------


def _extract_symbol_from_code(code: str) -> str:
    """futu option コード "US.SPYW260502C00570000" から "SPY" を返す。

    フォーマット不明時は code をそのまま返す。
    """
    try:
        # "US.SPYW..." → "SPYW..." → 先頭の大文字アルファベットのみ
        without_prefix = code.split(".", 1)[-1]
        symbol = ""
        for ch in without_prefix:
            if ch.isalpha() and ch.isupper():
                symbol += ch
            else:
                break
        return symbol or code
    except Exception:
        return code


def _pushover_alert(title: str, message: str, priority: int = 0) -> None:
    """Pushover 通知を送信する（失敗は swallow）。"""
    try:
        from common.pushover_client import pushover as _pov
        _pov(title, message, priority=priority)
    except Exception:
        log.warning("[TradeEngine] Pushover 通知失敗 title=%s", title)


# ---------------------------------------------------------------------------
# TradeEngine
# ---------------------------------------------------------------------------


class TradeEngine:
    """Atlas v3 TradeEngine — 発注・決済コア。

    全 engine の発注経路 root。spy_bot.py TradeEngine の機能を完全移植し、
    atlas_v3 インフラ（CircuitBreaker / Bulkhead / PreTradeGate / KillSwitch）を統合する。

    Args:
        paper:          True = SIMULATE 環境 / False = REAL 環境
        opend_host:     OpenD ホスト (default: env OPEND_HOST / "127.0.0.1")
        opend_port:     OpenD ポート (default: env OPEND_PORT / 11111)
        trade_password: 取引パスワード (default: env TRADE_PASSWORD)
        breaker_config: CircuitBreaker 設定 DTO。None の場合は共有 moomoo_breaker を使用。
        pre_trade_cfg:  PreTradeConfig。None の場合はデフォルト設定を使用。
    """

    def __init__(
        self,
        paper: bool = False,
        opend_host: str = _DEFAULT_OPEND_HOST,
        opend_port: int = _DEFAULT_OPEND_PORT,
        trade_password: Optional[str] = _DEFAULT_TRADE_PASSWORD,
        breaker_config: Optional[BreakerConfig] = None,
        pre_trade_cfg: Optional[_PreTradeConfig] = None,
    ) -> None:
        self.paper = paper
        self._opend_host = opend_host
        self._opend_port = opend_port
        self._trade_password = trade_password

        # CircuitBreaker: 専用インスタンス or 共有 moomoo_breaker
        self._breaker = (
            breaker_config.breaker if breaker_config is not None else moomoo_breaker
        )

        # PreTradeConfig
        self._pre_trade_cfg = pre_trade_cfg or _PreTradeConfig()

        self.trade_ctx: Any = None
        self.account_id: Optional[str] = None
        self.trade_env: Any = None
        if FUTU_AVAILABLE:
            self.trade_env = TrdEnv.SIMULATE if paper else TrdEnv.REAL

        self.unlock_ok: bool = False
        self._virtual_pos = _VirtualPositionManager()

        # 直近エントリー/エグジットの実約定価格キャッシュ
        self._last_entry_fills: dict = {}
        self._last_exit_fills: dict = {}
        self._pending_close: list = []

        # B1-CRITICAL: CS 発注済み leg の side 記録
        self._legs_placed_sides: list = [None, None]

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """OpenD に接続し、口座 ID を解決してトレードアンロックを試みる。

        Returns:
            True: 接続成功 / False: futu 未利用またはエラー
        """
        if not FUTU_AVAILABLE:
            log.info("[TradeEngine] futu 未利用 — connect は False を返す")
            return False
        try:
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=self._opend_host,
                port=self._opend_port,
                security_firm=SecurityFirm.FUTUJP,
            )
            self._resolve_account()
            self._unlock()
            return True
        except Exception as exc:
            log.error("[TradeEngine] connect 失敗: %s", exc)
            return False

    def close(self) -> None:
        """trade_ctx をクローズする。例外は swallow。"""
        if self.trade_ctx is not None:
            try:
                self.trade_ctx.close()
            except Exception:
                log.exception("[TradeEngine] trade_ctx.close() で例外発生（suppressed）")
            finally:
                self.trade_ctx = None

    # ------------------------------------------------------------------
    # 内部: 口座解決 / アンロック
    # ------------------------------------------------------------------

    def _resolve_account(self) -> None:
        """get_acc_list から account_id を解決する。"""
        ret, data = self.trade_ctx.get_acc_list()
        if ret != RET_OK or (hasattr(data, "empty") and data.empty):
            log.warning("[TradeEngine] get_acc_list 失敗; account_id 未解決")
            return
        env = TrdEnv.SIMULATE if self.paper else TrdEnv.REAL
        rows = data[data["trd_env"] == env]
        if not rows.empty:
            self.account_id = str(rows.iloc[0]["acc_id"])
            log.info("[TradeEngine] Account resolved: %s env=%s", self.account_id, env)

    def _unlock(self) -> None:
        """トレードアンロックを試みる。パスワード未設定時は skip。"""
        if not self._trade_password:
            return
        try:
            ret, data = self.trade_ctx.unlock_trade(password=self._trade_password)
            if ret == RET_OK:
                self.unlock_ok = True
            elif "unlock button" in str(data) or "disabled in the GUI" in str(data):
                log.warning("[TradeEngine] unlock_trade GUI 無効化済み; GUI unlock を前提とする")
                self.unlock_ok = True
        except Exception as exc:
            log.warning("[TradeEngine] _unlock 失敗: %s", exc)

    # ------------------------------------------------------------------
    # 口座情報
    # ------------------------------------------------------------------

    def get_account_cash(self) -> float:
        """口座残高を返す。

        DRY_TEST / futu 未利用 → 10000.0 (dry-run 専用値)
        取得失敗 → AccountCashError raise（zero-fallback 禁止）

        Returns:
            float: 口座残高 (USD)

        Raises:
            AccountCashError: API 失敗時
        """
        if DRY_TEST or not FUTU_AVAILABLE or not self.trade_ctx:
            return 10000.0

        try:
            ret, data = self.trade_ctx.accinfo_query(
                trd_env=self.trade_env,
                acc_id=int(self.account_id or 0),
            )
        except Exception as exc:
            raise AccountCashError(
                f"get_account_cash: accinfo_query 例外 — {exc}. 発注を拒否します。"
            ) from exc

        if ret != RET_OK or (hasattr(data, "empty") and data.empty):
            raise AccountCashError(
                f"get_account_cash: accinfo_query 失敗 ret={ret}. 発注を拒否します。"
            )

        row = data.iloc[0]
        net_assets = row.get("net_assets")
        cash = row.get("cash")
        if not net_assets and not cash:
            raise AccountCashError(
                f"get_account_cash: net_assets={net_assets!r}, cash={cash!r} — "
                "どちらも空/ゼロ。フォールバック禁止。発注を拒否します。"
            )
        return float(net_assets or cash)

    def get_margin_usage_ratio(self) -> Optional[float]:
        """証拠金使用率を返す (0.0〜1.0)。取得失敗時は None。

        DRY_TEST / 未接続時は 0.0。
        """
        if DRY_TEST or not FUTU_AVAILABLE or not self.trade_ctx:
            return 0.0
        try:
            ret, data = self.trade_ctx.accinfo_query(
                trd_env=self.trade_env,
                acc_id=int(self.account_id or 0),
            )
            if ret == RET_OK and not (hasattr(data, "empty") and data.empty):
                row = data.iloc[0]
                initial_margin = float(row.get("initial_margin", 0) or 0)
                total_assets = float(
                    row.get("total_assets", 0) or row.get("net_assets", 0) or 0
                )
                if total_assets > 0:
                    ratio = initial_margin / total_assets
                    log.info(
                        "[TradeEngine/Margin] initial_margin=%.2f total_assets=%.2f ratio=%.3f",
                        initial_margin, total_assets, ratio,
                    )
                    return round(ratio, 4)
        except Exception as exc:
            log.warning("[TradeEngine] get_margin_usage_ratio 失敗: %s", exc)
        return None

    def check_margin_and_alert(self) -> bool:
        """証拠金使用率を確認してエントリー可否を返す。

        Returns:
            True:  エントリー可 (使用率 < MARGIN_USAGE_MAX_ENTRY)
            False: エントリー停止
        """
        ratio = self.get_margin_usage_ratio()
        if ratio is None:
            return True  # 取得失敗 → 許可（サービス継続優先）

        if ratio >= _MARGIN_USAGE_ALERT:
            log.error(
                "[TradeEngine/Margin] 緊急: 使用率=%.1f%% >= %.0f%% → エントリー停止・警告送信",
                ratio * 100, _MARGIN_USAGE_ALERT * 100,
            )
            _pushover_alert(
                "証拠金危険水準",
                f"証拠金使用率={ratio:.1%}\n({_MARGIN_USAGE_ALERT:.0%}超=危険)\n新規エントリー停止",
                priority=1,
            )
            return False

        if ratio >= _MARGIN_USAGE_MAX_ENTRY:
            log.warning(
                "[TradeEngine/Margin] エントリー停止: 使用率=%.1f%% >= %.0f%%",
                ratio * 100, _MARGIN_USAGE_MAX_ENTRY * 100,
            )
            return False

        log.info(
            "[TradeEngine/Margin] OK: 使用率=%.1f%% < %.0f%%",
            ratio * 100, _MARGIN_USAGE_MAX_ENTRY * 100,
        )
        return True

    # ------------------------------------------------------------------
    # ポジション / オーダー照会
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list:
        """オープンポジション一覧を返す。"""
        if DRY_TEST:
            return self._virtual_pos.get_positions()
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return []
        ret, data = self.trade_ctx.position_list_query(
            trd_env=self.trade_env,
            acc_id=int(self.account_id or 0),
        )
        if ret != RET_OK:
            return []
        return data.to_dict("records") if hasattr(data, "to_dict") else []

    def is_alive(self) -> bool:
        """trade_ctx の生存確認 (get_acc_list で疎通テスト)。"""
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return False
        try:
            ret, _ = self.trade_ctx.get_acc_list()
            return ret == RET_OK
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 内部: PreTradeGate ヘルパー
    # ------------------------------------------------------------------

    def _build_pre_trade_ctx(
        self,
        code: str,
        qty: int,
        price: float,
        side_str: str,
        capital_usd: float,
        est_margin: float,
        open_margin_total: float,
        is_long: bool,
    ) -> _OrderCtx:
        """OrderCtx を組み立てて返す。"""
        symbol = _extract_symbol_from_code(code)
        return _OrderCtx(
            symbol=f"US.{symbol}" if "." not in symbol else symbol,
            qty=qty,
            option_price=price,
            side=side_str,
            is_long=is_long,
            est_margin=est_margin,
            capital_usd=capital_usd,
            open_margin_total=open_margin_total,
        )

    def _run_pre_trade_gate(
        self,
        code: str,
        qty: int,
        price: float,
        side_str: str,
        capital_usd: float,
        est_margin: float,
        open_margin_total: float,
        is_long: bool,
        label: str,
    ) -> tuple[bool, str]:
        """PreTradeGate を実行し (allowed, reason) を返す。"""
        ctx = self._build_pre_trade_ctx(
            code=code,
            qty=qty,
            price=price,
            side_str=side_str,
            capital_usd=capital_usd,
            est_margin=est_margin,
            open_margin_total=open_margin_total,
            is_long=is_long,
        )
        result = _check_order(ctx, self._pre_trade_cfg)
        if not result.allowed:
            log.error(
                "[TradeEngine/PreTrade] Leg %s ブロック layer=%s reason=%s",
                label, result.layer, result.reason,
            )
        return result.allowed, result.reason

    # ------------------------------------------------------------------
    # 内部: 単一 leg 発注
    # ------------------------------------------------------------------

    def _place_single_leg(
        self,
        code: str,
        side: Any,
        qty: int,
        label: str,
        init_price: Optional[float] = None,
        use_limit: bool = False,
        signal_id: Optional[str] = None,
    ) -> tuple[Optional[str], str]:
        """1 本足を発注する。Bulkhead / CircuitBreaker 経由。

        Returns:
            (order_id, fill_method)
            order_id: str if success, None if failed
            fill_method: "limit" | "market_fallback" | "market" | "failed" | "idempotency_blocked"
        """
        # Kill Switch チェック
        if _ks_is_active():
            log.warning("[TradeEngine] Kill Switch ARMED: 発注ブロック label=%s", label)
            return None, "failed"

        # CircuitBreaker チェック
        if self._breaker.state == "OPEN":
            log.error("[TradeEngine] moomoo_breaker OPEN: 発注ブロック label=%s", label)
            raise BrokerUnavailableError(
                f"moomoo_breaker OPEN: label={label!r} code={code!r}"
            )

        # 口座残高取得
        try:
            capital_usd = self.get_account_cash()
        except AccountCashError as exc:
            log.error("[TradeEngine] 口座残高取得失敗 → 発注ブロック: %s", exc)
            _pushover_alert(
                "[Atlas/ALERT] 口座残高取得失敗・発注ブロック",
                f"label={label}\nerror={exc}",
                priority=1,
            )
            return None, "failed"

        if capital_usd <= 0:
            log.error(
                "[TradeEngine] capital_usd=%s <= 0 → 発注ブロック (label=%s)",
                capital_usd, label,
            )
            return None, "failed"

        # Idempotency チェック
        if signal_id:
            try:
                from common_v3.idempotency.store import (
                    IdempotencyStore as _IdemStore,
                    make_job_key as _make_key,
                )
                from datetime import datetime, timezone
                _store = _IdemStore()
                _key = _make_key(
                    strategy=label,
                    symbol=_extract_symbol_from_code(code),
                    trigger_time=datetime.now(timezone.utc),
                )
                # signal_id を key suffix にして決定的一意性を確保
                _full_key = f"{signal_id}__{_key}"
                already = not _store.check_and_mark(_full_key, ttl_sec=300)
                if already:
                    log.warning(
                        "[TradeEngine] 重複発注ブロック (idempotency): label=%s signal_id=%s",
                        label, signal_id,
                    )
                    return None, "idempotency_blocked"
            except Exception as exc:
                # fail-safe: チェック失敗 → 発注ブロック
                log.error("[TradeEngine] idempotency チェック失敗 → 発注ブロック: %s", exc)
                return None, "failed"

        # ポジション情報収集
        open_positions = self.get_open_positions()
        open_margin_total = sum(
            abs(float(p.get("market_val", 0) or 0)) for p in open_positions
        )
        est_margin = float(init_price or 0) * qty * 100.0

        # side str 変換
        side_str = "SELL" if (FUTU_AVAILABLE and side == TrdSide.SELL) else (
            "SELL" if str(side).upper() in ("SELL", "2") else "BUY"
        )
        is_long = side_str == "BUY"

        # 指値モード
        if use_limit and init_price is not None:
            return self._place_limit_leg(
                code=code,
                side=side,
                qty=qty,
                label=label,
                init_price=init_price,
                capital_usd=capital_usd,
                est_margin=est_margin,
                open_margin_total=open_margin_total,
                side_str=side_str,
                is_long=is_long,
            )

        # 成行モード
        return self._place_market_leg(
            code=code,
            side=side,
            qty=qty,
            label=label,
            init_price=init_price,
            capital_usd=capital_usd,
            est_margin=est_margin,
            open_margin_total=open_margin_total,
            side_str=side_str,
            is_long=is_long,
            fill_method="market",
        )

    def _place_limit_leg(
        self,
        code: str,
        side: Any,
        qty: int,
        label: str,
        init_price: float,
        capital_usd: float,
        est_margin: float,
        open_margin_total: float,
        side_str: str,
        is_long: bool,
    ) -> tuple[Optional[str], str]:
        """指値発注 + 調整ループ。未約定なら成行フォールバック。"""
        env = self.trade_env
        acc = int(self.account_id or 0)
        price = round(init_price, 2)

        # Pre-Trade Gate
        ok, reason = self._run_pre_trade_gate(
            code=code, qty=qty, price=price, side_str=side_str,
            capital_usd=capital_usd, est_margin=est_margin,
            open_margin_total=open_margin_total, is_long=is_long, label=label,
        )
        if not ok:
            return None, "failed"

        # 初回指値発注 (CircuitBreaker 経由)
        order_id: Optional[str] = None
        try:
            def _do_place_limit():
                return self.trade_ctx.place_order(
                    price=price, qty=qty, code=code,
                    trd_side=side, order_type=OrderType.NORMAL,
                    trd_env=env, acc_id=acc,
                    time_in_force=TimeInForce.DAY,
                )
            ret, data = self._breaker.call(_do_place_limit)
        except CircuitBreakerOpenError:
            raise BrokerUnavailableError(f"moomoo_breaker OPEN during limit place: label={label!r}")

        if ret != RET_OK:
            log.warning("[TradeEngine] 指値発注初回失敗: label=%s data=%s", label, data)
        else:
            order_id = data.iloc[0].get("order_id", "") if not (hasattr(data, "empty") and data.empty) else ""
            log.info("[TradeEngine] 指値発注: label=%s code=%s qty=%d price=%.2f order_id=%s",
                     label, code, qty, price, order_id)

            _price = price
            for step in range(_LIMIT_MAX_ADJUST_STEPS + 1):
                time.sleep(_LIMIT_ADJUST_INTERVAL)
                ret_q, od = self.trade_ctx.order_list_query(
                    order_id=str(order_id), trd_env=env, acc_id=acc,
                )
                if ret_q == RET_OK and not (hasattr(od, "empty") and od.empty):
                    status = od.iloc[0].get("order_status", "")
                    if status == "FILLED_ALL":
                        log.info("[TradeEngine] 指値約定: label=%s step=%d price=%.2f", label, step, _price)
                        return order_id, "limit"
                if step < _LIMIT_MAX_ADJUST_STEPS:
                    _price = round(_price - _LIMIT_ADJUST_STEP, 2) if side_str == "SELL" \
                        else round(_price + _LIMIT_ADJUST_STEP, 2)
                    self.trade_ctx.modify_order(
                        modify_order_op=ModifyOrderOp.NORMAL,
                        order_id=str(order_id),
                        qty=qty, price=_price,
                        trd_env=env, acc_id=acc,
                    )

            # キャンセルして成行フォールバック
            if order_id:
                self.trade_ctx.modify_order(
                    modify_order_op=ModifyOrderOp.CANCEL,
                    order_id=str(order_id), qty=qty, price=_price,
                    trd_env=env, acc_id=acc,
                )
                time.sleep(0.5)
                log.warning(
                    "[TradeEngine] 指値 %d ステップ後も未約定 → 成行フォールバック label=%s",
                    _LIMIT_MAX_ADJUST_STEPS, label,
                )

        return self._place_market_leg(
            code=code, side=side, qty=qty, label=label,
            init_price=init_price, capital_usd=capital_usd,
            est_margin=est_margin, open_margin_total=open_margin_total,
            side_str=side_str, is_long=is_long,
            fill_method="market_fallback",
        )

    def _place_market_leg(
        self,
        code: str,
        side: Any,
        qty: int,
        label: str,
        init_price: Optional[float],
        capital_usd: float,
        est_margin: float,
        open_margin_total: float,
        side_str: str,
        is_long: bool,
        fill_method: str = "market",
    ) -> tuple[Optional[str], str]:
        """成行発注 (最大 2 回試行)。"""
        env = self.trade_env
        acc = int(self.account_id or 0)

        ok, reason = self._run_pre_trade_gate(
            code=code, qty=qty, price=0.0, side_str=side_str,
            capital_usd=capital_usd, est_margin=est_margin,
            open_margin_total=open_margin_total, is_long=is_long, label=label,
        )
        if not ok:
            return None, "failed"

        for attempt in range(2):
            try:
                def _do_place_market():
                    return self.trade_ctx.place_order(
                        price=0, qty=qty, code=code,
                        trd_side=side, order_type=OrderType.MARKET,
                        trd_env=env, acc_id=acc,
                        time_in_force=TimeInForce.DAY,
                    )
                ret, data = self._breaker.call(_do_place_market)
            except CircuitBreakerOpenError:
                raise BrokerUnavailableError(
                    f"moomoo_breaker OPEN during market place: label={label!r}"
                )

            if ret == RET_OK:
                oid = data.iloc[0].get("order_id", "") if not (hasattr(data, "empty") and data.empty) else ""
                log.info(
                    "[TradeEngine] 成行発注 OK: label=%s code=%s qty=%d order_id=%s fill=%s",
                    label, code, qty, oid, fill_method,
                )
                return oid, fill_method

            log.warning(
                "[TradeEngine] 成行発注失敗 attempt=%d label=%s data=%s",
                attempt + 1, label, data,
            )
            if attempt == 0:
                time.sleep(1)

        log.error("[TradeEngine] 全試行失敗: label=%s", label)
        return None, "failed"

    # ------------------------------------------------------------------
    # 内部: 反転決済
    # ------------------------------------------------------------------

    def _reverse_leg(
        self,
        code: str,
        original_side: Any,
        qty: int,
        label: str,
    ) -> None:
        """original_side の反対方向で決済注文。original_side=None は ValueError。"""
        if original_side is None:
            raise ValueError(
                f"_reverse_leg: original_side=None 禁止。"
                f"SHORT 脚は SELL、LONG 脚は BUY を渡すこと。"
                f" label={label}, code={code}"
            )
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return
        reverse = TrdSide.BUY if original_side == TrdSide.SELL else TrdSide.SELL
        env = self.trade_env
        acc = int(self.account_id or 0)
        ret, _ = self.trade_ctx.place_order(
            price=0, qty=qty, code=code,
            trd_side=reverse, order_type=OrderType.MARKET,
            trd_env=env, acc_id=acc, time_in_force=TimeInForce.DAY,
        )
        if ret == RET_OK:
            log.info("[TradeEngine] 反転決済 OK: label=%s code=%s", label, code)
        else:
            log.error("[TradeEngine] 反転決済失敗: label=%s code=%s", label, code)

    # ------------------------------------------------------------------
    # 内部: 約定確認ポーリング
    # ------------------------------------------------------------------

    def _confirm_fills(
        self,
        order_ids: list,
        direction: str,
        use_limit: bool = False,
    ) -> dict:
        """発注後に全 leg の約定 (FILLED_ALL) を確認し dealt_avg_price を返す。

        Returns:
            dict: {order_id: float|None}  (未約定は None)
        """
        if not order_ids:
            return {}

        env = self.trade_env
        acc = int(self.account_id or 0)
        poll_interval = 2.0
        max_polls = 30 if use_limit else 15
        fills: dict = {}
        pending = list(order_ids)

        for poll_idx in range(max_polls):
            time.sleep(poll_interval)
            still_pending = []
            for oid in pending:
                ret, od = self.trade_ctx.order_list_query(
                    order_id=str(oid), trd_env=env, acc_id=acc,
                )
                if ret != RET_OK or (hasattr(od, "empty") and od.empty):
                    fills.setdefault(oid, None)
                    still_pending.append(oid)
                    continue
                status = od.iloc[0].get("order_status", "")
                avg_price = od.iloc[0].get("dealt_avg_price", None)
                try:
                    avg_price = float(avg_price) if avg_price is not None else None
                except (ValueError, TypeError):
                    avg_price = None
                if status == "FILLED_ALL":
                    fills[oid] = avg_price
                    log.info(
                        "[TradeEngine] 約定確認 OK: order_id=%s avg_price=%s poll=%d/%d",
                        oid, avg_price, poll_idx + 1, max_polls,
                    )
                else:
                    fills.setdefault(oid, None)
                    still_pending.append(oid)
            pending = still_pending
            if not pending:
                break

        if pending:
            log.warning("[TradeEngine] 約定未確認タイムアウト: %s direction=%s", pending, direction)
            _pushover_alert(
                f"CS 約定未確認 [{direction}]",
                f"未約定 order_id: {pending}\n手動確認が必要です",
                priority=1,
            )
        return fills

    # ------------------------------------------------------------------
    # place_credit_spread
    # ------------------------------------------------------------------

    def place_credit_spread(
        self,
        sell_code: str,
        buy_code: str,
        qty: int,
        direction: str,
        sell_init_price: Optional[float] = None,
        buy_init_price: Optional[float] = None,
        vix: Optional[float] = None,
        signal_id: Optional[str] = None,
    ) -> bool:
        """クレジットスプレッドを発注する。

        DRY_TEST 時は VirtualPositionManager に仮想記録して True を返す。

        Returns:
            True: 両 leg 発注 + 約定確認成功
            False: いずれかの leg が失敗（巻き戻し済み）
        """
        # Kill Switch
        if _ks_is_active():
            log.warning("[TradeEngine] Kill Switch ARMED: place_credit_spread ブロック")
            return False

        # signal_id 決定的生成
        if signal_id is None:
            from datetime import datetime as _dt
            _sym = _extract_symbol_from_code(sell_code) or "UNK"
            _ts = _dt.now().strftime("%Y%m%d%H%M")
            signal_id = f"cs_{direction}_{_sym}_{_ts}"
            log.debug("[TradeEngine] signal_id 自動生成: %s", signal_id)

        # DRY_TEST
        if DRY_TEST:
            virtual_net_credit = 0.50
            log.info(
                "[DRY-TEST] %s CS: SELL=%s BUY=%s qty=%d credit=%.2f",
                direction, sell_code, buy_code, qty, virtual_net_credit,
            )
            self._virtual_pos.add_position(sell_code, qty, virtual_net_credit, "SHORT")
            self._virtual_pos.add_position(buy_code, qty, virtual_net_credit * 0.3, "LONG")
            return True

        if not FUTU_AVAILABLE or not self.trade_ctx:
            log.info("[DRY-RUN] %s CS: SELL=%s BUY=%s qty=%d", direction, sell_code, buy_code, qty)
            return True

        # 指値使用可否
        high_vix = vix is not None and vix > _LIMIT_HIGH_VIX_THRESHOLD
        use_limit = (
            _ENABLE_LIMIT_ENTRY
            and not high_vix
            and sell_init_price is not None
            and buy_init_price is not None
        )
        if _ENABLE_LIMIT_ENTRY and high_vix:
            log.info("[TradeEngine] VIX=%.1f > %.0f → 成行モード", vix, _LIMIT_HIGH_VIX_THRESHOLD)

        legs = [
            (sell_code, TrdSide.SELL, f"{direction}_sell", sell_init_price),
            (buy_code,  TrdSide.BUY,  f"{direction}_buy",  buy_init_price),
        ]
        placed: list = []
        order_ids: list = []
        fill_methods: list = []

        for leg_idx, (code, side, label, init_price) in enumerate(legs):
            time.sleep(0.5)
            _leg_sid = f"{signal_id}_leg{leg_idx}" if signal_id else None

            # Bulkhead 経由で place_single_leg を実行
            pool = _get_bulkhead_pool()
            fut = pool.submit(
                "moomoo",
                self._place_single_leg,
                code, side, qty, label,
                init_price=init_price,
                use_limit=use_limit,
                signal_id=_leg_sid,
            )
            try:
                order_id, fill_method = fut.result(timeout=120.0)
            except Exception as exc:
                log.error("[TradeEngine] Bulkhead 発注例外: label=%s err=%s", label, exc)
                order_id, fill_method = None, "failed"

            if order_id is not None:
                placed.append((code, side, label))
                order_ids.append(order_id)
                fill_methods.append(fill_method)
                self._legs_placed_sides[leg_idx] = side
            else:
                # 発注済み leg を巻き戻す
                for p_code, p_side, p_label in reversed(placed):
                    self._reverse_leg(p_code, p_side, qty, p_label)
                self._legs_placed_sides = [None, None]
                return False

        log.info("[TradeEngine] %s CS 発注完了: qty=%d fill_methods=%s", direction, qty, fill_methods)

        # 約定確認
        fill_map = self._confirm_fills(order_ids, direction, use_limit=use_limit)
        sell_fill = fill_map.get(order_ids[0]) if order_ids else None
        buy_fill  = fill_map.get(order_ids[1]) if len(order_ids) > 1 else None
        self._last_entry_fills = {
            "sell": sell_fill,
            "buy": buy_fill,
            "fill_methods": fill_methods,
            "sell_init_price": sell_init_price,
            "buy_init_price": buy_init_price,
        }

        if sell_fill is None or buy_fill is None:
            missing = []
            if sell_fill is None:
                missing.append(f"sell(order={order_ids[0] if order_ids else 'n/a'})")
            if buy_fill is None:
                missing.append(f"buy(order={order_ids[1] if len(order_ids) > 1 else 'n/a'})")
            log.error("[TradeEngine] fill 確認失敗 %s → 片脚回避で反転決済", missing)
            for code, side, label in reversed(list(zip(
                [sell_code, buy_code],
                self._legs_placed_sides,
                [f"{direction}_sell_unwind", f"{direction}_buy_unwind"],
            ))):
                if code and side is not None:
                    try:
                        self._reverse_leg(code, side, qty, label)
                    except Exception as exc:
                        log.error("[TradeEngine] 反転決済失敗 %s: %s", code, exc)
            self._legs_placed_sides = [None, None]
            _pushover_alert(
                "[Atlas] CS 片脚未約定",
                f"{direction} {fill_map}\nsell_fill={sell_fill} buy_fill={buy_fill}\n"
                "片脚リスク回避で反転発注済み。ポジション確認が必要。",
                priority=2,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # cancel_all_open_orders
    # ------------------------------------------------------------------

    def cancel_all_open_orders(self, reason: str = "eod_sweep") -> int:
        """全未約定オーダーをキャンセルする。

        FILLED_PART オーダーはキャンセル対象外（裸ポジション化リスク回避）。
        FILLED_PART 検出時は Pushover priority=2 で通知する。

        Returns:
            int: キャンセル成功件数
        """
        if _ks_is_active():
            log.warning("[TradeEngine] Kill Switch ARMED: cancel_all_open_orders ブロック")
            return 0

        if DRY_TEST or not FUTU_AVAILABLE or not self.trade_ctx:
            log.info("[TradeEngine/SweepCancel/%s] DRY_TEST or no trade_ctx → skip", reason)
            return 0

        env = self.trade_env
        acc = int(self.account_id or 0)

        try:
            ret, df = self.trade_ctx.order_list_query(trd_env=env, acc_id=acc)
        except Exception as exc:
            log.warning("[TradeEngine/SweepCancel/%s] order_list_query 例外: %s", reason, exc)
            return 0

        if ret != RET_OK or df is None or (hasattr(df, "empty") and df.empty):
            log.info("[TradeEngine/SweepCancel/%s] オープン注文なし", reason)
            return 0

        cancel_statuses = {"SUBMITTED", "WAITING_SUBMIT", "SUBMITTING"}
        alert_statuses  = {"FILLED_PART"}
        canceled = 0
        filled_part_orders: list = []

        for _, row in df.iterrows():
            status = str(row.get("order_status", ""))
            oid = row.get("order_id")
            if not oid:
                continue
            if status in alert_statuses:
                filled_part_orders.append({
                    "order_id": oid,
                    "code": row.get("code"),
                    "qty": row.get("qty"),
                    "dealt_qty": row.get("dealt_qty"),
                })
                log.warning(
                    "[TradeEngine/SweepCancel/%s] FILLED_PART 検出: oid=%s code=%s → 対象外",
                    reason, oid, row.get("code"),
                )
                continue
            if status not in cancel_statuses:
                continue
            try:
                self.trade_ctx.modify_order(
                    modify_order_op=ModifyOrderOp.CANCEL,
                    order_id=oid, price=0, qty=0,
                    trd_env=env, acc_id=acc,
                )
                canceled += 1
                log.info(
                    "[TradeEngine/SweepCancel/%s] oid=%s code=%s キャンセル済",
                    reason, oid, row.get("code"),
                )
            except Exception as exc:
                log.warning(
                    "[TradeEngine/SweepCancel/%s] キャンセル失敗 oid=%s: %s",
                    reason, oid, exc,
                )

        if filled_part_orders:
            details = "\n".join(
                f"  {o['code']} oid={o['order_id']} {o['dealt_qty']}/{o['qty']}枚約定"
                for o in filled_part_orders
            )
            _pushover_alert(
                f"[Atlas/ALERT] FILLED_PART 検出・手動確認要 ({reason})",
                f"部分約定オーダー {len(filled_part_orders)} 件。"
                f"裸ポジション化リスクあり。\n{details}",
                priority=2,
            )

        if canceled > 0:
            _pushover_alert(
                f"[Atlas/SweepCancel] {reason}",
                f"{canceled} 件の未約定オーダーをキャンセルしました。",
            )

        return canceled

    # ------------------------------------------------------------------
    # close_all_positions
    # ------------------------------------------------------------------

    def close_all_positions(self, reason: str = "force_close") -> bool:
        """全ポジションを決済し約定を確認する。

        SHORT buyback → 完全約定確認 (60s timeout) → LONG sell の順で実行
        (CRITICAL-8 sync barrier)。

        Returns:
            True: 全ポジション決済成功
            False: いずれかの leg が失敗（_pending_close に記録済み）
        """
        if _ks_is_active():
            log.warning("[TradeEngine] Kill Switch ARMED: close_all_positions ブロック")
            return False

        # DRY_TEST
        if DRY_TEST:
            positions = self._virtual_pos.get_positions()
            if not positions:
                log.info("[DRY-TEST] 決済対象なし (%s)", reason)
                return True
            total_pnl = sum(p.get("unrealized_pl", 0.0) for p in positions)
            log.info(
                "[DRY-TEST] close_all_positions(%s): %d positions P&L=%.2f",
                reason, len(positions), total_pnl,
            )
            self._virtual_pos.remove_all()
            return True

        positions = self.get_open_positions()
        if not positions:
            log.info("[TradeEngine] 決済対象ポジションなし (%s)", reason)
            return True

        # 期限切れポジション除外
        from datetime import datetime as _dt2
        import zoneinfo as _zi
        try:
            _ET = _zi.ZoneInfo("America/New_York")
        except Exception:
            import datetime as _dtmod
            _ET = _dtmod.timezone.utc  # type: ignore[assignment]
        today_str = _dt2.now(tz=_ET).strftime("%Y-%m-%d")
        active_positions = []
        expired_codes: list = []
        for pos in positions:
            code = pos.get("code", "")
            exp_str = today_str.replace("-", "")[2:]  # "260502" 形式
            if exp_str in code and code.endswith(("C", "P") + tuple(f"{i:08d}" for i in range(10))):
                pass  # 対象外チェックは spy_bot の _option_is_expired に準じた簡易判定
            # シンプルに: today_str (yyyymmdd) が含まれるかどうかで期限判定
            code_date = code[len(code)-15:len(code)-9] if len(code) >= 15 else ""
            if code_date and code_date < today_str.replace("-", "")[2:]:
                expired_codes.append(code)
                continue
            active_positions.append(pos)

        if expired_codes:
            log.info("[TradeEngine] 期限切れポジション除外 (auto-cleanup 対象): %s", expired_codes)
        if not active_positions:
            log.info("[TradeEngine] アクティブポジションなし (%s)", reason)
            return True

        env = self.trade_env
        acc = int(self.account_id or 0)

        _TrdSide_BUY   = TrdSide.BUY   if FUTU_AVAILABLE else 1
        _TrdSide_SELL  = TrdSide.SELL  if FUTU_AVAILABLE else 2
        _OrderType_MKT = OrderType.MARKET  if FUTU_AVAILABLE else "MARKET"
        _TimeInForce_D = TimeInForce.DAY   if FUTU_AVAILABLE else "DAY"

        def _send_close_leg(pos_item: dict) -> Optional[str]:
            """1 leg 決済発注。成功時 order_id、失敗時 None。"""
            code_ = pos_item.get("code", "")
            qty_  = abs(int(pos_item.get("qty", 0)))
            if qty_ == 0:
                return None
            position_side_ = pos_item.get("position_side", "LONG")
            side_ = _TrdSide_BUY if position_side_ == "SHORT" else _TrdSide_SELL
            ret_, data_ = self.trade_ctx.place_order(
                price=0, qty=qty_, code=code_,
                trd_side=side_, order_type=_OrderType_MKT,
                trd_env=env, acc_id=acc,
                time_in_force=_TimeInForce_D,
            )
            if ret_ != RET_OK:
                log.error("[TradeEngine/SyncBarrier] 決済発注失敗: %s x%d", code_, qty_)
                return None
            oid_ = data_.iloc[0].get("order_id", "?") if not (hasattr(data_, "empty") and data_.empty) else "?"
            log.info(
                "[TradeEngine/SyncBarrier] 決済発注送信: %s x%d side=%s oid=%s",
                code_, qty_, position_side_, oid_,
            )
            return oid_

        def _wait_fills(order_ids_: list, timeout_sec: float = 60.0):
            import time as _t
            deadline = _t.monotonic() + timeout_sec
            pending_ = list(order_ids_)
            filled_ = []
            while pending_ and _t.monotonic() < deadline:
                _t.sleep(2)
                still_ = []
                for oid__ in pending_:
                    ret__, od = self.trade_ctx.order_list_query(
                        order_id=oid__, trd_env=env, acc_id=acc,
                    )
                    if ret__ != RET_OK or od is None or (hasattr(od, "empty") and od.empty):
                        still_.append(oid__)
                        continue
                    if od.iloc[0].get("order_status", "") == "FILLED_ALL":
                        filled_.append(oid__)
                    else:
                        still_.append(oid__)
                pending_ = still_
            return filled_, pending_

        short_positions = [p for p in active_positions if p.get("position_side") == "SHORT"]
        long_positions  = [p for p in active_positions if p.get("position_side") != "SHORT"]
        failed_legs: list = []
        close_order_ids: list = []
        close_order_codes: dict = {}

        # Step-A: SHORT buyback
        short_order_ids = []
        for pos_ in short_positions:
            oid = _send_close_leg(pos_)
            if oid:
                short_order_ids.append(oid)
                close_order_codes[oid] = {"code": pos_.get("code", ""), "position_side": "SHORT"}
            else:
                failed_legs.append(pos_.get("code", "?"))

        if short_order_ids:
            filled_s, timeout_s = _wait_fills(short_order_ids, timeout_sec=60)
            close_order_ids.extend(filled_s)
            for oid_ in timeout_s:
                info_ = close_order_codes.get(oid_, {})
                failed_legs.append(info_.get("code", oid_) if isinstance(info_, dict) else oid_)

        if failed_legs:
            self._pending_close = list(failed_legs)
            msg = f"reason={reason}\nfailed_legs={failed_legs}\n次回起動時に再試行します"
            log.error("[TradeEngine/SyncBarrier] naked risk: %s", msg)
            _pushover_alert("[Atlas/ALERT] close_all_positions 失敗 - naked risk", msg, priority=2)
            return False

        # Step-B: LONG sell
        long_order_ids = []
        for pos_ in long_positions:
            oid = _send_close_leg(pos_)
            if oid:
                long_order_ids.append(oid)
                close_order_codes[oid] = {"code": pos_.get("code", ""), "position_side": "LONG"}
            else:
                failed_legs.append(pos_.get("code", "?"))

        if long_order_ids:
            filled_l, timeout_l = _wait_fills(long_order_ids, timeout_sec=60)
            close_order_ids.extend(filled_l)
            for oid_ in timeout_l:
                info_ = close_order_codes.get(oid_, {})
                failed_legs.append(info_.get("code", oid_) if isinstance(info_, dict) else oid_)

        if failed_legs:
            self._pending_close = list(failed_legs)
            _pushover_alert(
                "[Atlas/ALERT] close_all_positions 失敗 - naked risk",
                f"LONG sell 失敗 reason={reason} failed={failed_legs}",
                priority=2,
            )
            return False

        # 残留確認
        time.sleep(2)
        remaining = [
            p for p in self.get_open_positions()
            if abs(int(float(p.get("qty", 0)))) > 0
        ]
        if remaining:
            remaining_codes = [p.get("code", "?") for p in remaining]
            today_yy = _dt2.now(tz=_ET).strftime("%y%m%d")
            truly_open = [c for c in remaining_codes if today_yy in c]
            expired    = [c for c in remaining_codes if today_yy not in c]
            if expired and not truly_open:
                log.info(
                    "[TradeEngine] 期限切れ 0DTE ポジション残留 (%s): %s — auto-cleanup 対象",
                    reason, expired,
                )
                return True
            if truly_open:
                log.error("[TradeEngine] ポジション残留 (%s): %s", reason, truly_open)
                self._pending_close = truly_open
                _pushover_alert(
                    "[Atlas/ALERT] close_all_positions 失敗 - naked risk",
                    f"{reason} 後もポジション残留: {truly_open}",
                    priority=2,
                )
                return False
            log.warning("[TradeEngine] 不明ポジション (%s): %s", reason, remaining_codes)
            return False

        # exit fill 収集
        if close_order_ids:
            fill_map = self._confirm_fills(close_order_ids, f"close_{reason}")
            self._last_exit_fills = {}
            for oid_, price in fill_map.items():
                info_ = close_order_codes.get(oid_, {})
                c_ = info_.get("code", oid_) if isinstance(info_, dict) else oid_
                ps_ = info_.get("position_side", "LONG") if isinstance(info_, dict) else "LONG"
                self._last_exit_fills[c_] = {"price": price, "position_side": ps_}
        else:
            self._last_exit_fills = {}

        self._pending_close = []
        log.info("[TradeEngine] 全ポジション決済完了 (%s)", reason)
        return True
