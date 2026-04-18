#!/usr/bin/env python3
"""
apex_bot.py — Apex Trader Funding 先物自動売買Bot v1

ブローカー : Tradovate (demo/live)
対象       : MES (Micro E-mini S&P 500) / ES (E-mini S&P 500)
口座       : Apex Trader Funding $50K評価口座（5/1〜）

設計方針:
  Atlas基盤流用 (60-70%):
    strategy_selector.select_strategy()   — 環境適応型戦術選択
    portfolio_risk.can_take_risk()        — リスク上限チェック
    spy_bot.calc_kelly_fraction()         — ポジションサイズ（Half Kelly）
    spy_bot.premarket_assessment()        — プレマーケット環境評価
    spy_bot.IntradayMonitor              — 日中VIXレジーム監視

  新規実装:
    TradovateClient                       — Tradovate REST API接続
    ApexRuleGuard                         — Apex全ルール遵守層
    FuturesORBStrategy                   — 先物ORBエントリーロジック
    ContractRoller                        — 先物限月ロールオーバー

動作モード:
  --paper     : Tradovate demoアカウントで動作（デフォルト）
  --live      : Tradovate liveアカウントで動作（本番）
  --dry-run   : API接続なし・全ロジックをテスト（mock価格使用）
  --backtest  : 過去データでバックテスト（Day 2実装予定）

Flags:
  --paper      Demo口座（デフォルト）
  --live       Live口座（本番・要注意）
  --dry-run    接続なし・ロジックテスト
  --account-size 50000  口座サイズ指定 (デフォルト: 50000)
  --product MES         先物製品コード (デフォルト: MES)

NOTE: 5/1 Apex評価開始前にdemoで動作確認を完了させること。
"""

from __future__ import annotations

import os
import sys
import json
import math
import time
import uuid
import logging
import datetime
import argparse
import zoneinfo
from pathlib import Path
from typing import Optional

# ── .env ロード ────────────────────────────────────────────────────────────────
def _load_env_file():
    for candidate in [Path("/root/spxbot/.env"), Path(__file__).parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
            break

_load_env_file()

# ── パス定数 ───────────────────────────────────────────────────────────────────
_BASE_DIR = Path(os.environ.get("APEX_DATA_DIR", Path(__file__).parent / "data"))
LOG_DIR   = Path(os.environ.get("APEX_LOG_DIR", _BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "apex_bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("apex_bot")

# ── タイムゾーン ───────────────────────────────────────────────────────────────
ET  = zoneinfo.ZoneInfo("America/New_York")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# ── Atlas基盤モジュール import ─────────────────────────────────────────────────
# V2-3修正: apex_bot は先物Bot なので SPX/SPY用 strategy_selector ではなく
#           mffu_strategy_selector.select_futures_strategy() を使う。
# env_score 算出のみに使用（エントリー判断はapex_rule_simulator + orb が担う）。
try:
    from chronos_strategy_selector import select_futures_strategy, build_env_dict
    STRATEGY_SELECTOR_AVAILABLE = True
    log.info("mffu_strategy_selector: loaded (V2-3 fix)")
except ImportError as e:
    STRATEGY_SELECTOR_AVAILABLE = False
    log.warning(f"mffu_strategy_selector not available: {e}")
    # fallback: Atlas版を試みる（後方互換）
    try:
        from strategy_selector import select_strategy
        log.warning("strategy_selector (SPX版) fallback loaded — V2-3未修正状態")
    except ImportError:
        pass

try:
    from portfolio_risk import (
        can_take_risk, update_positions, clear_positions,
        check_weekly_dd, check_monthly_dd, record_daily_pnl,
        load_positions,
    )
    PORTFOLIO_RISK_AVAILABLE = True
    log.info("portfolio_risk: loaded")
except ImportError as e:
    PORTFOLIO_RISK_AVAILABLE = False
    log.warning(f"portfolio_risk not available: {e}")

try:
    from spy_bot import calc_kelly_fraction
    KELLY_AVAILABLE = True
    log.info("spy_bot.calc_kelly_fraction: loaded")
except ImportError as e:
    KELLY_AVAILABLE = False
    log.warning(f"spy_bot.calc_kelly_fraction not available: {e}")

# ── 新規モジュール import ──────────────────────────────────────────────────────
from tradovate_client import TradovateClient, _get_front_month_symbol, CONTRACT_SPECS
from apex_rule_simulator import (
    APEX_ACCOUNT_RULES,
    check_daily_loss_limit,
    check_trailing_drawdown,
    check_consistency_rule,
    check_profit_target,
    get_allowed_contracts,
)

# ── 認証情報 ───────────────────────────────────────────────────────────────────
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")

# ── デフォルトパラメータ ───────────────────────────────────────────────────────
DEFAULT_ACCOUNT_SIZE = 50_000
DEFAULT_PRODUCT      = "MES"

# ORB設定（先物用）
ORB_OPENING_PERIOD_MINUTES = 30   # 9:30〜10:00 ET をオープニングレンジとする
ORB_ENTRY_WINDOW_MINUTES   = 120  # 10:00〜12:00 ET をエントリーウィンドウとする
ORB_STOP_ATR_MULT          = 1.0  # ストップ = ORレンジ × 1.0倍
ORB_TARGET_ATR_MULT        = 2.0  # 利確 = ORレンジ × 2.0倍（RR=1:2）

# 日次ループ間隔
MAIN_LOOP_SLEEP_SECS = 60   # 60秒ごとにメインループを実行

# ── Pushover通知 ──────────────────────────────────────────────────────────────

def pushover(title: str, message: str, priority: int = 0) -> bool:
    """Pushover通知を送信する。"""
    import requests as _requests
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log.warning("pushover: token/user not set")
        return False
    try:
        resp = _requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":    PUSHOVER_TOKEN,
                "user":     PUSHOVER_USER,
                "title":    title,
                "message":  message,
                "priority": priority,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        log.warning(f"pushover HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.warning(f"pushover: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# VIX / 市場データ取得（軽量版・spy_botのMarketDataに依存しない独立実装）
# ─────────────────────────────────────────────────────────────────────────────

def get_vix() -> Optional[float]:
    """現在のVIXをyahoo financeから取得する。"""
    import requests as _requests
    try:
        resp = _requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if result:
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]
            if closes:
                return round(closes[-1], 2)
    except Exception as e:
        log.warning(f"get_vix: {e}")
    return None


def get_vix_history(days: int = 60) -> list[float]:
    """VIX日次終値を取得する（直近N日）。"""
    import requests as _requests
    try:
        resp = _requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "1d", "range": f"{days}d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if result:
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            return [c for c in closes if c is not None]
    except Exception as e:
        log.warning(f"get_vix_history: {e}")
    return []


def get_sp500_daily_closes(days: int = 5) -> list[float]:
    """S&P500(SPY)の日次終値を取得する。"""
    import requests as _requests
    try:
        resp = _requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
            params={"interval": "1d", "range": f"{days}d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if result:
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            return [c for c in closes if c is not None]
    except Exception as e:
        log.warning(f"get_sp500_daily_closes: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# ApexRuleGuard — Apex全ルール遵守層
# ─────────────────────────────────────────────────────────────────────────────

class ApexRuleGuard:
    """
    Apexのルールを監視し、違反前に自動停止する保護層。

    設計思想:
      - 毎tick（60秒ごと）にルールチェックを実行
      - Daily Loss 80%消費時点で「警告」→ ポジションを縮小
      - Daily Loss 95%消費時点で「緊急停止」→ 全ポジションをクローズ
      - Trailing DD 70%消費時点で「警告」
      - Trailing DD 90%消費時点で「緊急停止」

    マージンバッファ:
      ルール違反ラインの手前で停止することで安全マージンを確保する。
      「ルール上限の80%到達で縮小・95%到達で停止」が実践者間の標準的な設定
      (dearvn/tradovate実装・Apex community実践者の報告より)
    """

    # 警告・停止トリガーのしきい値（上限消費比率）
    DAILY_LOSS_WARN_PCT   = 0.80   # 80%消費で警告
    DAILY_LOSS_STOP_PCT   = 0.95   # 95%消費で緊急停止
    TRAILING_DD_WARN_PCT  = 0.70   # 70%消費で警告
    TRAILING_DD_STOP_PCT  = 0.90   # 90%消費で緊急停止

    def __init__(self, account_size: int):
        self.account_size    = account_size
        self.rules           = APEX_ACCOUNT_RULES[account_size]
        self.initial_balance = float(account_size)

        # 状態
        self.day_start_balance: float = self.initial_balance
        self.high_water_mark:   float = self.initial_balance
        self.daily_pnls:        list  = []  # Funded開始以降の日次P&L
        self.today_pnl:         float = 0.0

        # A-3修正: Apex Trailing DD HWMフリーズ仕様
        # initial_balance + max_trailing_dd に達したらHWMを固定する
        # 例: $50K口座では $52,500 でHWMフリーズ → threshold=$50,000 に固定
        self._hwm_frozen: bool = False
        self._hwm_freeze_threshold: float = self.initial_balance + self.rules.max_trailing_dd

        # フラグ
        self._daily_loss_halted:  bool = False
        self._trailing_dd_halted: bool = False
        self._warned_daily_loss:  bool = False
        self._warned_trailing_dd: bool = False

    def reset_day(self, current_balance: float):
        """日次リセット（毎朝呼び出す）。"""
        if self.today_pnl != 0:
            self.daily_pnls.append(self.today_pnl)
        self.day_start_balance  = current_balance
        self.today_pnl          = 0.0
        self._daily_loss_halted = False
        self._warned_daily_loss = False
        log.info(f"[ApexRuleGuard] day reset: start_balance=${current_balance:,.0f} "
                 f"hwm=${self.high_water_mark:,.0f}")

    def update_pnl(self, realized_pnl: float):
        """確定P&Lを記録する。"""
        self.today_pnl += realized_pnl
        log.debug(f"[ApexRuleGuard] today_pnl updated: ${self.today_pnl:+,.2f}")

    def update_hwm(self, current_balance: float, open_pnl: float = 0.0):
        """ハイウォーターマークを更新する（毎tick呼び出す）。

        A-3修正: Apex公式仕様に従いHWMフリーズを実装。
        残高が initial_balance + max_trailing_dd に達した時点でHWMを凍結する。
        例: $50K口座では $52,500 でHWM凍結 → Trailing threshold = $50,000 固定。
        凍結後は残高が上昇してもHWMは上がらない（不要なemergency_closeを防止）。
        """
        effective = current_balance + open_pnl
        if not self._hwm_frozen:
            if effective >= self._hwm_freeze_threshold:
                # HWMフリーズ: 上昇をここで止める
                self._hwm_frozen = True
                self.high_water_mark = self._hwm_freeze_threshold
                log.info(
                    f"[ApexRuleGuard] HWM FROZEN at ${self._hwm_freeze_threshold:,.0f} "
                    f"(initial=${self.initial_balance:,.0f} + "
                    f"max_dd=${self.rules.max_trailing_dd:,.0f})"
                )
            else:
                self.high_water_mark = max(self.high_water_mark, effective)

    def check(
        self,
        current_balance: float,
        open_pnl:        float = 0.0,
    ) -> dict:
        """
        全ルールをチェックする。毎tick（60秒ごと）に呼び出す。

        Returns:
            {
              "safe":         bool,   — True=取引継続OK
              "action":       str,    — "ok" | "warn" | "halt" | "emergency_close"
              "reasons":      list,   — 警告・停止理由
              "daily_loss":   dict,
              "trailing_dd":  dict,
              "consistency":  dict,
              "profit_target": dict,
            }
        """
        self.update_hwm(current_balance, open_pnl)

        # ルールチェック実行
        dl  = check_daily_loss_limit(
            self.rules, self.day_start_balance, current_balance, open_pnl
        )
        tdd = check_trailing_drawdown(
            self.rules, self.initial_balance, current_balance,
            self.high_water_mark, open_pnl
        )
        cr  = check_consistency_rule(
            self.rules, self.daily_pnls, self.today_pnl
        )
        pt  = check_profit_target(
            self.rules, self.initial_balance, current_balance
        )

        action  = "ok"
        reasons = []

        # Daily Loss チェック
        dl_used_pct = 1.0 - (dl["remaining"] / self.rules.max_daily_loss)
        if not dl["passed"]:
            action = "emergency_close"
            reasons.append(
                f"DAILY_LOSS_VIOLATED: lost ${-dl['daily_loss']:.0f} "
                f"(limit ${self.rules.max_daily_loss})"
            )
            self._daily_loss_halted = True
        elif dl_used_pct >= self.DAILY_LOSS_STOP_PCT:
            action = "halt"
            reasons.append(
                f"DAILY_LOSS_NEAR_LIMIT_{self.DAILY_LOSS_STOP_PCT*100:.0f}PCT: "
                f"remaining=${dl['remaining']:.0f}"
            )
            self._daily_loss_halted = True
        elif dl_used_pct >= self.DAILY_LOSS_WARN_PCT and not self._warned_daily_loss:
            if action == "ok":
                action = "warn"
            reasons.append(
                f"DAILY_LOSS_WARNING_{self.DAILY_LOSS_WARN_PCT*100:.0f}PCT: "
                f"remaining=${dl['remaining']:.0f}"
            )
            self._warned_daily_loss = True

        # Trailing DD チェック
        tdd_used_pct = 1.0 - (tdd["remaining"] / self.rules.max_trailing_dd)
        if not tdd["passed"]:
            action = "emergency_close"
            reasons.append(
                f"TRAILING_DD_VIOLATED: drawdown=${tdd['drawdown']:.0f} "
                f"(limit ${self.rules.max_trailing_dd})"
            )
            self._trailing_dd_halted = True
        elif tdd_used_pct >= self.TRAILING_DD_STOP_PCT:
            if action not in ("emergency_close",):
                action = "halt"
            reasons.append(
                f"TRAILING_DD_NEAR_LIMIT_{self.TRAILING_DD_STOP_PCT*100:.0f}PCT: "
                f"remaining=${tdd['remaining']:.0f}"
            )
            self._trailing_dd_halted = True
        elif tdd_used_pct >= self.TRAILING_DD_WARN_PCT and not self._warned_trailing_dd:
            if action == "ok":
                action = "warn"
            reasons.append(
                f"TRAILING_DD_WARNING_{self.TRAILING_DD_WARN_PCT*100:.0f}PCT: "
                f"remaining=${tdd['remaining']:.0f}"
            )
            self._warned_trailing_dd = True

        # Consistency Rule チェック（情報のみ・自動停止はしない）
        if not cr["passed"] and cr.get("violation_amount", 0) > 0:
            if action == "ok":
                action = "warn"
            reasons.append(
                f"CONSISTENCY_WARN: today_pnl=${self.today_pnl:.0f} > "
                f"max_allowed=${cr['max_allowed']:.0f}"
            )

        is_safe = action in ("ok", "warn")

        return {
            "safe":          is_safe,
            "action":        action,
            "reasons":       reasons,
            "violations":    reasons,   # check_all_rules との整合性のため alias
            "daily_loss":    dl,
            "trailing_dd":   tdd,
            "consistency":   cr,
            "profit_target": pt,
        }

    def can_enter_new_position(self, current_balance: float, open_pnl: float = 0.0) -> bool:
        """新規エントリーが許可されるかチェック。"""
        if self._daily_loss_halted or self._trailing_dd_halted:
            log.warning("[ApexRuleGuard] new entry BLOCKED: already halted")
            return False

        result = self.check(current_balance, open_pnl)
        if not result["safe"]:
            log.warning(f"[ApexRuleGuard] new entry BLOCKED: {result['reasons']}")
            return False

        return True

    def get_allowed_contracts(self, current_profit: float) -> int:
        """現在の利益に応じた許容コントラクト数を返す。"""
        return get_allowed_contracts(self.account_size, current_profit)

    def status_summary(self, current_balance: float, open_pnl: float = 0.0) -> str:
        """現在のルール状況をサマリー文字列で返す。"""
        dl  = check_daily_loss_limit(
            self.rules, self.day_start_balance, current_balance, open_pnl
        )
        tdd = check_trailing_drawdown(
            self.rules, self.initial_balance, current_balance,
            self.high_water_mark, open_pnl
        )
        pt  = check_profit_target(
            self.rules, self.initial_balance, current_balance
        )

        return (
            f"Balance=${current_balance:,.0f} "
            f"DL_remaining=${dl['remaining']:.0f}({dl['margin_pct']:.0f}%) "
            f"TDD_remaining=${tdd['remaining']:.0f}({tdd['margin_pct']:.0f}%) "
            f"Profit=${current_balance - self.initial_balance:+,.0f}/"
            f"${pt['target']:,.0f}({pt['progress_pct']:.0f}%)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# FuturesORBStrategy — 先物ORBエントリーロジック
# ─────────────────────────────────────────────────────────────────────────────

class FuturesORBStrategy:
    """
    先物 Opening Range Breakout 戦略。

    タイムライン（ET）:
      9:30  — 市場オープン・ORレンジ計測開始
      10:00 — ORレンジ確定（高値/安値）
      10:00〜12:00 — ブレイクアウト監視・エントリーウィンドウ
      15:45 — 強制クローズ（満期前30分）

    ロジック:
      1. 9:30〜10:00のOR高値・安値を計測
      2. VIX環境・環境スコアでエントリー許可判定
      3. OR高値ブレイク → Buy / OR安値ブレイク → Sell
      4. ストップ = ORレンジの1.0倍
      5. 利確 = ORレンジの2.0倍（RR=1:2）
      6. 1日1回のみエントリー
    """

    def __init__(
        self,
        client:       TradovateClient,
        rule_guard:   ApexRuleGuard,
        product:      str = "MES",
        account_size: int = 50_000,
    ):
        self.client       = client
        self.rule_guard   = rule_guard
        self.product      = product
        self.account_size = account_size

        # OR状態
        self._or_high:    Optional[float] = None
        self._or_low:     Optional[float] = None
        self._or_complete = False

        # エントリー状態
        self._entry_done:    bool = False
        self._current_order: Optional[dict] = None
        self._stop_price:    Optional[float] = None
        self._target_price:  Optional[float] = None
        self._entry_side:    Optional[str]   = None  # "Long" | "Short"
        self._entry_price:   Optional[float] = None

        # 日次リセット用
        self._trade_id: Optional[str] = None

    def reset_day(self):
        """日次リセット。"""
        self._or_high     = None
        self._or_low      = None
        self._or_complete = False
        self._entry_done  = False
        self._current_order = None
        self._stop_price  = None
        self._target_price = None
        self._entry_side  = None
        self._entry_price = None
        self._trade_id    = None
        log.info(f"[FuturesORB] day reset")

    def update_or_candle(self, high: float, low: float):
        """ORレンジのローソク足を更新する（9:30〜10:00）。"""
        if self._or_high is None or high > self._or_high:
            self._or_high = high
        if self._or_low is None or low < self._or_low:
            self._or_low = low

    def finalize_or(self):
        """10:00 ETにORレンジを確定する。"""
        if self._or_high is not None and self._or_low is not None:
            self._or_complete = True
            or_range = self._or_high - self._or_low
            log.info(f"[FuturesORB] OR finalized: high={self._or_high:.2f} "
                     f"low={self._or_low:.2f} range={or_range:.2f}")

    @property
    def or_range(self) -> Optional[float]:
        if self._or_high is not None and self._or_low is not None:
            return self._or_high - self._or_low
        return None

    def check_breakout(
        self,
        current_price:    float,
        current_balance:  float,
        vix:              float,
        env_score:        float,
        open_pnl:         float = 0.0,
    ) -> Optional[dict]:
        """
        ブレイクアウト判定とエントリー実行。

        Args:
            current_price:   現在の先物価格
            current_balance: 現在の口座残高
            vix:             現在のVIX
            env_score:       プレマーケット環境スコア
            open_pnl:        含み損益
        Returns:
            エントリー結果dict またはNone
        """
        if not self._or_complete:
            return None

        if self._entry_done:
            return None

        if self._or_high is None or self._or_low is None:
            return None

        # Apexルールチェック
        if not self.rule_guard.can_enter_new_position(current_balance, open_pnl):
            log.info("[FuturesORB] entry blocked by ApexRuleGuard")
            return None

        # 環境フィルター（VIX > 35 または env_score < 40 はスキップ）
        if vix > 35.0:
            log.info(f"[FuturesORB] entry skipped: VIX={vix:.1f} > 35.0")
            return None

        if env_score < 40.0:
            log.info(f"[FuturesORB] entry skipped: env_score={env_score:.1f} < 40")
            return None

        or_range = self.or_range

        # ブレイクアウト判定
        action = None
        if current_price > self._or_high:
            action = "Buy"
        elif current_price < self._or_low:
            action = "Sell"

        if action is None:
            return None

        # コントラクト数計算（Kelly × スケーリングプラン）
        current_profit = current_balance - self.rule_guard.initial_balance
        max_contracts  = self.rule_guard.get_allowed_contracts(current_profit)

        # Kelly分数でコントラクト数を決定（B-5修正: or_range を渡す）
        n_contracts = self._calc_contracts(
            account_balance = current_balance,
            max_contracts   = max_contracts,
            or_range        = or_range,
        )

        if n_contracts < 1:
            log.info("[FuturesORB] entry skipped: n_contracts < 1")
            return None

        symbol = self.client.get_front_month_symbol(self.product)

        # ストップ・利確レベル
        if action == "Buy":
            stop_price   = self._or_high - or_range * ORB_STOP_ATR_MULT
            target_price = self._or_high + or_range * ORB_TARGET_ATR_MULT
        else:
            stop_price   = self._or_low + or_range * ORB_STOP_ATR_MULT
            target_price = self._or_low - or_range * ORB_TARGET_ATR_MULT

        # 発注
        log.info(f"[FuturesORB] entry signal: {action} {n_contracts}x{symbol} "
                 f"@{current_price:.2f} stop={stop_price:.2f} target={target_price:.2f} "
                 f"or_range={or_range:.2f}")

        order = self.client.place_order(
            symbol     = symbol,
            action     = action,
            qty        = n_contracts,
            order_type = "Market",
        )

        if not order:
            log.error("[FuturesORB] entry order failed")
            return None

        self._entry_done   = True
        self._stop_price   = stop_price
        self._target_price = target_price
        self._entry_side   = "Long" if action == "Buy" else "Short"
        self._entry_price  = current_price
        self._trade_id     = str(uuid.uuid4())[:8]

        log.info(f"[FuturesORB] entry confirmed: trade_id={self._trade_id} "
                 f"side={self._entry_side} stop={stop_price:.2f} target={target_price:.2f}")

        return {
            "trade_id":    self._trade_id,
            "action":      action,
            "symbol":      symbol,
            "qty":         n_contracts,
            "entry_price": current_price,
            "stop_price":  stop_price,
            "target_price": target_price,
            "order":       order,
        }

    def _calc_contracts(
        self,
        account_balance: float,
        max_contracts: int,
        or_range: Optional[float] = None,
    ) -> int:
        """
        Kelly分数を使ってコントラクト数を計算する。

        B-5修正: Kelly分数は資本比（0.0〜1.0）であり max_contracts への乗数ではない。
        正しいセマンティクス:
            kelly = 0.10 → 資本の10%をリスクにさらす
            dollar_risk = account_balance * kelly
            contracts = floor(dollar_risk / risk_per_contract)
        修正前: floor(0.10 * 5) = floor(0.5) = 0 → max(1,0) = 1 で常時1枚固定
        修正後: dollar_risk=5000(10%@50K) / risk_per_contract=500(100pts×5) = 10枚
        """
        kelly: Optional[float] = None

        if KELLY_AVAILABLE:
            pnl_file = _BASE_DIR / "apex_pnl.json"
            kelly = calc_kelly_fraction(pnl_file)

        if kelly is None:
            # Kelly算出不可（データ不足）→ 保守的に1枚から開始
            return 1

        if or_range is None or or_range <= 0:
            # or_range 不明時は保守的に1枚
            return 1

        # B-5修正: リスク予算ベースの枚数算出
        point_value       = CONTRACT_SPECS.get(self.product, {}).get("point_value", 5.0)
        risk_per_contract = or_range * ORB_STOP_ATR_MULT * point_value
        if risk_per_contract <= 0:
            return 1
        dollar_risk = account_balance * kelly
        n           = max(1, min(math.floor(dollar_risk / risk_per_contract), max_contracts))
        log.info(
            f"_calc_contracts(B-5): kelly={kelly:.4f} "
            f"or_range={or_range:.2f} point_value={point_value} "
            f"risk_per_contract=${risk_per_contract:.0f} "
            f"dollar_risk=${dollar_risk:.0f} contracts={n}"
        )
        return n

    def check_exit(
        self,
        current_price:   float,
        current_balance: float,
        open_pnl:        float,
    ) -> Optional[str]:
        """
        エグジット条件をチェックする。

        Returns:
            "stop_hit" | "target_hit" | "force_close" | None
        """
        if not self._entry_done:
            return None

        if self._stop_price is None or self._target_price is None:
            return None

        # Apexルールチェック（緊急停止）
        rule_result = self.rule_guard.check(current_balance, open_pnl)
        if rule_result["action"] == "emergency_close":
            log.warning(f"[FuturesORB] EMERGENCY CLOSE: {rule_result['reasons']}")
            return "emergency_close"

        # ストップ/利確判定
        if self._entry_side == "Long":
            if current_price <= self._stop_price:
                return "stop_hit"
            if current_price >= self._target_price:
                return "target_hit"
        elif self._entry_side == "Short":
            if current_price >= self._stop_price:
                return "stop_hit"
            if current_price <= self._target_price:
                return "target_hit"

        return None

    def execute_exit(self, reason: str) -> Optional[dict]:
        """エグジット注文を実行する。"""
        symbol = self.client.get_front_month_symbol(self.product)
        log.info(f"[FuturesORB] executing exit: reason={reason} symbol={symbol}")

        result = self.client.close_position(symbol)

        if result:
            log.info(f"[FuturesORB] exit confirmed: {result}")
            self._entry_done = False
        else:
            log.error("[FuturesORB] exit order failed")

        return result


# ─────────────────────────────────────────────────────────────────────────────
# ContractRoller — 先物限月ロールオーバー
# ─────────────────────────────────────────────────────────────────────────────

class ContractRoller:
    """
    先物限月ロールオーバー管理。

    S&P500先物のロールオーバーは:
      四半期限月(H/M/U/Z)の第3金曜日 8日前（木曜日）
    例: 2025年6月限月(MESU5) → 2025年9月限月(MESZ5) への切替

    Botは毎朝フロント限月を確認し、前日と変わっていればロールオーバーを検知する。
    """

    def __init__(self, product: str = "MES"):
        self.product         = product
        self._last_symbol:   Optional[str] = None
        self._rollover_count = 0

    def check_rollover(self) -> dict:
        """
        現在のフロント限月を確認し、ロールオーバーが必要かチェックする。
        Returns:
            {"rolled": bool, "old_symbol": str|None, "new_symbol": str, "product": str}
        """
        new_symbol = _get_front_month_symbol(self.product)
        rolled     = (self._last_symbol is not None and self._last_symbol != new_symbol)

        if rolled:
            self._rollover_count += 1
            log.info(f"[ContractRoller] ROLLOVER detected: "
                     f"{self._last_symbol} -> {new_symbol} "
                     f"(count={self._rollover_count})")
            pushover(
                "Apex Bot: Contract Rollover",
                f"{self._last_symbol} → {new_symbol}",
                priority=0,
            )

        old_symbol        = self._last_symbol
        self._last_symbol = new_symbol

        return {
            "rolled":     rolled,
            "old_symbol": old_symbol,
            "new_symbol": new_symbol,
            "product":    self.product,
        }


# ─────────────────────────────────────────────────────────────────────────────
# ApexBot — メインBot
# ─────────────────────────────────────────────────────────────────────────────

class ApexBot:
    """
    Apex Trader Funding 先物自動売買Botのメインクラス。

    run_forever() を呼び出すと日次ループを開始する。
    """

    def __init__(
        self,
        account_size: int  = DEFAULT_ACCOUNT_SIZE,
        product:      str  = DEFAULT_PRODUCT,
        paper:        bool = True,   # True=demo, False=live
        dry_run:      bool = False,
    ):
        self.account_size = account_size
        self.product      = product
        self.paper        = paper
        self.dry_run      = dry_run

        log.info(f"[ApexBot] init: account_size=${account_size:,} "
                 f"product={product} paper={paper} dry_run={dry_run}")

        # コンポーネント初期化
        env = "DEMO" if paper else "LIVE"

        if not dry_run:
            self.client = TradovateClient(env=env)
        else:
            self.client = None
            log.info("[ApexBot] dry_run: TradovateClient not initialized")

        self.rule_guard = ApexRuleGuard(account_size)

        self.orb = FuturesORBStrategy(
            client       = self.client,
            rule_guard   = self.rule_guard,
            product      = product,
            account_size = account_size,
        )

        self.roller = ContractRoller(product=product)

        # 日次状態
        self._premarket_done:      bool = False
        self._or_building:         bool = False
        self._or_finalized:        bool = False
        self._force_close_done:    bool = False
        self._nightly_done:        bool = False
        self._last_loop_date:      Optional[datetime.date] = None
        self._session_balance:     float = float(account_size)
        self._vix:                 Optional[float] = None
        self._vix_history:         list  = []
        self._env_score:           float = 50.0  # デフォルト

    # ── 認証 ──────────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Tradovateに接続する。"""
        if self.dry_run:
            log.info("[ApexBot] dry_run: connect skipped")
            return True

        log.info("[ApexBot] connecting to Tradovate...")
        if not self.client.authenticate():
            log.error("[ApexBot] authentication failed")
            pushover("Apex Bot: Auth Failed", "Tradovate認証失敗", priority=1)
            return False

        # 残高取得
        balance = self.client.get_account_balance()
        if balance:
            self._session_balance = balance.get("total_equity", float(self.account_size))
            log.info(f"[ApexBot] connected: balance=${self._session_balance:,.0f}")

        return True

    # ── プレマーケット評価 ─────────────────────────────────────────────────────

    def run_premarket(self) -> bool:
        """
        9:00-9:30 ET: プレマーケット評価を実行する。
        - VIX/VIX履歴取得
        - strategy_selectorで環境スコア算出
        - Apexルールのday_start_balanceをリセット
        Returns: エントリー許可=True
        """
        log.info("[ApexBot] running premarket assessment...")

        # VIX取得
        self._vix = get_vix()
        if self._vix is None:
            log.warning("[ApexBot] VIX取得失敗 → fallback 20.0")
            self._vix = 20.0

        # VIX履歴取得
        self._vix_history = get_vix_history(60)
        log.info(f"[ApexBot] VIX={self._vix:.1f} history={len(self._vix_history)}days")

        # 残高取得・day_startリセット
        if not self.dry_run and self.client:
            balance_info = self.client.get_account_balance()
            if balance_info:
                self._session_balance = balance_info.get("total_equity", self._session_balance)
        self.rule_guard.reset_day(self._session_balance)

        # ロールオーバーチェック
        rollover = self.roller.check_rollover()
        if rollover["rolled"]:
            log.info(f"[ApexBot] contract rolled: {rollover['old_symbol']} -> {rollover['new_symbol']}")

        # V2-3修正: mffu_strategy_selector で環境評価（先物Bot専用セレクター）
        # env_score 算出のみに使用。エントリー判断は apex_rule_simulator + orb が担う。
        if STRATEGY_SELECTOR_AVAILABLE and self._vix_history:
            import datetime as _dt
            import zoneinfo as _zi
            _ET = _zi.ZoneInfo("America/New_York")
            time_et_str = _dt.datetime.now(_ET).strftime("%H:%M")
            env_dict = build_env_dict(
                vix               = self._vix or 20.0,
                vix_history       = self._vix_history,
                vix_z             = 0.0,
                time_et           = time_et_str,
                account_balance   = self._session_balance,
            )
            try:
                ss_result = select_futures_strategy(env_dict)
                # select_futures_strategy が env_dict["env_score"] を書き戻す（B-1修正済み）
                self._env_score = env_dict.get("env_score", 50.0)
                primary_strategy = ss_result[0]["strategy"] if ss_result else "no_trade"
                log.info(f"[ApexBot] mffu_strategy_selector: "
                         f"primary={primary_strategy} "
                         f"score={self._env_score:.1f}")
            except Exception as e:
                log.warning(f"[ApexBot] mffu_strategy_selector error: {e}")
        else:
            # fallback: VIXベースの簡易スコア
            if self._vix < 15:
                self._env_score = 80.0
            elif self._vix < 22:
                self._env_score = 65.0
            elif self._vix < 30:
                self._env_score = 45.0
            else:
                self._env_score = 20.0
            log.info(f"[ApexBot] env_score (fallback): {self._env_score:.1f}")

        # Apexルール上のエントリー可否
        pt = check_profit_target(
            self.rule_guard.rules,
            self.rule_guard.initial_balance,
            self._session_balance,
        )
        if pt["achieved"]:
            log.info(f"[ApexBot] PROFIT TARGET ACHIEVED! profit=${pt['profit']:.0f}")
            pushover(
                "Apex Bot: Profit Target達成",
                f"利益 ${pt['profit']:,.0f} / 目標 ${pt['target']:,.0f}",
                priority=1,
            )

        log.info(f"[ApexBot] premarket done: "
                 f"{self.rule_guard.status_summary(self._session_balance)}")
        self._premarket_done = True
        return True

    # ── メインループ ──────────────────────────────────────────────────────────

    def _get_current_balance_and_pnl(self) -> tuple[float, float]:
        """現在の残高と含み損益を返す。dry_runはfallback値を返す。"""
        if self.dry_run or not self.client:
            return self._session_balance, 0.0

        balance_info = self.client.get_account_balance()
        if balance_info:
            balance  = balance_info.get("total_equity", self._session_balance)
            open_pnl = balance_info.get("unrealized_pnl", 0.0)
            return balance, open_pnl

        return self._session_balance, 0.0

    def _get_current_price(self) -> Optional[float]:
        """現在の先物価格を返す。dry_runはNoneを返す。"""
        if self.dry_run or not self.client:
            return None

        symbol = self.client.get_front_month_symbol(self.product)
        quote  = self.client.get_quote(symbol)
        if quote:
            return quote.get("last")
        return None

    def _update_or_range(self):
        """ORレンジ更新（9:30〜10:00 ET）。"""
        if self.dry_run or not self.client:
            return

        symbol = self.client.get_front_month_symbol(self.product)
        # 1分足を取得してORレンジを更新
        bars = self.client.get_bars(symbol, bar_type="MinuteBar", unit=1, count=5)
        for bar in bars:
            if bar.get("high") and bar.get("low"):
                self.orb.update_or_candle(bar["high"], bar["low"])

    def run_forever(self):
        """
        メインループ。60秒ごとに実行。
        市場時間（9:00〜16:30 ET）に稼働する。
        """
        log.info("[ApexBot] starting run_forever()")
        pushover("Apex Bot: 起動", f"${self.account_size:,} {self.product} paper={self.paper}")

        if not self.connect():
            log.error("[ApexBot] connection failed, exiting")
            return

        while True:
            try:
                now_et   = datetime.datetime.now(ET)
                now_date = now_et.date()
                t        = now_et.time()

                # 日次リセット（新しい日が始まったら）
                if self._last_loop_date != now_date:
                    self._daily_reset(now_date)
                    self._last_loop_date = now_date

                # 週末はスキップ
                if now_et.weekday() >= 5:  # 5=土, 6=日
                    log.debug("[ApexBot] weekend, sleeping 1h")
                    time.sleep(3600)
                    continue

                # ── 9:00〜9:30 ET: プレマーケット ──
                if datetime.time(9, 0) <= t < datetime.time(9, 30):
                    if not self._premarket_done:
                        self.run_premarket()
                        self.orb.reset_day()

                # ── 9:30〜10:00 ET: ORレンジ計測 ──
                elif datetime.time(9, 30) <= t < datetime.time(10, 0):
                    if self._premarket_done and not self._or_finalized:
                        self._update_or_range()
                        log.debug(f"[ApexBot] OR building: high={self.orb._or_high} low={self.orb._or_low}")

                # ── 10:00 ET: ORレンジ確定 ──
                elif datetime.time(10, 0) <= t < datetime.time(10, 1):
                    if not self._or_finalized:
                        self.orb.finalize_or()
                        self._or_finalized = True
                        log.info(f"[ApexBot] OR confirmed: range={self.orb.or_range}")

                # ── 10:00〜12:00 ET: エントリーウィンドウ ──
                elif datetime.time(10, 0) <= t < datetime.time(12, 0):
                    if self._or_finalized and not self.orb._entry_done:
                        balance, open_pnl = self._get_current_balance_and_pnl()
                        price = self._get_current_price()

                        if price:
                            entry = self.orb.check_breakout(
                                current_price   = price,
                                current_balance = balance,
                                vix             = self._vix or 20.0,
                                env_score       = self._env_score,
                                open_pnl        = open_pnl,
                            )
                            if entry:
                                log.info(f"[ApexBot] ENTRY: {entry}")
                                pushover(
                                    "Apex Bot: エントリー",
                                    f"{entry['action']} {entry['qty']}x{entry['symbol']} "
                                    f"@{entry['entry_price']:.2f}",
                                )

                # ── エントリー後: エグジット監視 ──
                # B-3修正: elif → if に変更。
                # elif だと 10:00-12:00 のエントリーウィンドウ elif チェーンと排他になり
                # エントリー完了直後の同 tick でエグジット監視に入れない。
                # if にすることで、エントリーウィンドウ中にエントリーが完了した tick でも
                # 即座にエグジット監視を開始できる（15:45 force_close が安全網として機能）。
                if self.orb._entry_done:
                    balance, open_pnl = self._get_current_balance_and_pnl()
                    price = self._get_current_price()

                    if price:
                        exit_reason = self.orb.check_exit(price, balance, open_pnl)
                        if exit_reason:
                            result = self.orb.execute_exit(exit_reason)
                            if result:
                                log.info(f"[ApexBot] EXIT: reason={exit_reason}")
                                pushover(
                                    f"Apex Bot: エグジット ({exit_reason})",
                                    f"balance=${balance:,.0f}",
                                )

                # ── 15:45 ET: 強制クローズ ──
                if datetime.time(15, 45) <= t < datetime.time(15, 50):
                    if not self._force_close_done:
                        self._force_close()

                # ── 16:30 ET: 日次レポート ──
                if datetime.time(16, 30) <= t < datetime.time(16, 35):
                    if not self._nightly_done:
                        self._run_nightly()

                # ── Apexルール定期チェック（毎tick）──
                if self.orb._entry_done:
                    balance, open_pnl = self._get_current_balance_and_pnl()
                    rule_result = self.rule_guard.check(balance, open_pnl)
                    if rule_result["action"] == "emergency_close":
                        log.warning(f"[ApexBot] EMERGENCY CLOSE triggered: {rule_result['reasons']}")
                        self.orb.execute_exit("emergency_close")
                        pushover(
                            "Apex Bot: 緊急クローズ",
                            f"rule violations: {rule_result['reasons']}",
                            priority=1,
                        )
                    elif rule_result["action"] == "warn":
                        for reason in rule_result["reasons"]:
                            log.warning(f"[ApexBot] RULE WARNING: {reason}")

                # トークンrenew（ensure_authenticated）
                if not self.dry_run and self.client:
                    self.client.ensure_authenticated()

            except KeyboardInterrupt:
                log.info("[ApexBot] KeyboardInterrupt: shutting down")
                break
            except Exception as e:
                log.error(f"[ApexBot] loop error: {e}", exc_info=True)
                pushover("Apex Bot: エラー", str(e)[:200], priority=0)

            time.sleep(MAIN_LOOP_SLEEP_SECS)

        log.info("[ApexBot] run_forever() exited")
        pushover("Apex Bot: 停止", "run_forever() exited")

    def _daily_reset(self, today: datetime.date):
        """日次リセット処理。"""
        log.info(f"[ApexBot] daily reset for {today}")
        self._premarket_done   = False
        self._or_building      = False
        self._or_finalized     = False
        self._force_close_done = False
        self._nightly_done     = False

    def _force_close(self):
        """15:45 ET: 全ポジションを強制クローズする。"""
        log.info("[ApexBot] force close at 15:45 ET")
        if not self.dry_run and self.client:
            results = self.client.close_all_positions()
            if results:
                log.info(f"[ApexBot] force close: {results}")
                pushover("Apex Bot: 強制クローズ", f"15:45 ET: {len(results)}件クローズ")
        self._force_close_done = True

    def _run_nightly(self):
        """16:30 ET: 日次レポートと記録。"""
        log.info("[ApexBot] running nightly report")

        balance, _ = self._get_current_balance_and_pnl()
        today_pnl  = balance - self.rule_guard.day_start_balance

        self.rule_guard.update_pnl(today_pnl)

        # PnLをJSONファイルに記録
        pnl_file = _BASE_DIR / "apex_pnl.json"
        pnl_data = {}
        if pnl_file.exists():
            try:
                pnl_data = json.loads(pnl_file.read_text())
            except Exception:
                pnl_data = {"trades": []}

        if "trades" not in pnl_data:
            pnl_data["trades"] = []

        now_jst = datetime.datetime.now(JST)
        pnl_data["trades"].append({
            "event":   "exit",
            "date":    now_jst.strftime("%Y-%m-%d"),
            "pnl_usd": round(today_pnl, 2),
        })

        pnl_file.write_text(json.dumps(pnl_data, indent=2))
        log.info(f"[ApexBot] nightly: today_pnl=${today_pnl:+,.2f} recorded")

        status = self.rule_guard.status_summary(balance)
        pushover("Apex Bot: 日次レポート", f"P&L=${today_pnl:+,.0f}\n{status}")

        # portfolio_risk.pyにも記録
        if PORTFOLIO_RISK_AVAILABLE:
            try:
                record_daily_pnl(
                    now_jst.strftime("%Y-%m-%d"),
                    today_pnl,
                    "apex_bot",
                )
            except Exception as e:
                log.warning(f"[ApexBot] portfolio_risk.record_daily_pnl: {e}")

        self._nightly_done = True


# ─────────────────────────────────────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Apex Trader Funding Bot")
    parser.add_argument("--paper",        action="store_true", default=True,
                        help="Demo口座で動作（デフォルト）")
    parser.add_argument("--live",         action="store_true",
                        help="Live口座で動作（本番）")
    parser.add_argument("--dry-run",      action="store_true",
                        help="API接続なし・ロジックテスト")
    parser.add_argument("--account-size", type=int, default=DEFAULT_ACCOUNT_SIZE,
                        help=f"口座サイズ（デフォルト: {DEFAULT_ACCOUNT_SIZE}）")
    parser.add_argument("--product",      type=str, default=DEFAULT_PRODUCT,
                        help=f"先物製品コード（デフォルト: {DEFAULT_PRODUCT}）")
    parser.add_argument("--test-connect", action="store_true",
                        help="接続テストのみ実行して終了")
    args = parser.parse_args()

    paper = not args.live

    if args.test_connect:
        from tradovate_client import TradovateClient
        client = TradovateClient(env="DEMO" if paper else "LIVE")
        result = client.test_connection()
        print("\n=== Tradovate Connection Test ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return

    bot = ApexBot(
        account_size = args.account_size,
        product      = args.product,
        paper        = paper,
        dry_run      = args.dry_run,
    )

    bot.run_forever()


if __name__ == "__main__":
    main()
