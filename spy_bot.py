#!/usr/bin/env python3
"""
spy_bot.py — SPY Credit Spread Bot (0DTE) v2
Broker  : moomoo / Futu Securities Japan (口座2756)

戦略アーキテクチャ:
  2タクティクス × VIX環境 × IVR補正 × VIXスパイク回復

  [STANDARD] 10:30 ET — SMA方向ベースCS
    VIX < 22: 通常フル運用 (delta 0.25)
    VIX 22-35: 縮小運用 (delta 0.20)
    VIX >= 35: 標準エントリーなし → ORFに委ねる

  [ORF] 13:00 ET — Opening Range Fade CS
    VIX >= 22かつ寄り付き30分で|move| >= 0.8%のとき発動
    方向: 寄り付き下落 → Put CS / 寄り付き上昇 → Call CS
    VIX 22-35: delta 0.20 / VIX 35-50: delta 0.15 / VIX > 50: 停止

  [IVR補正] 両タクティクスに適用
    IVR > 75 → delta +0.05 (高IV環境でより積極的)
    IVR < 25 → delta -0.05 (低IV環境で守り)

  [VIXスパイク回復] 前日VIX+3以上のスパイク翌日
    当日エントリーに delta +0.05 上乗せ (回復バイアス)

Flags:
  --paper          ペーパー取引 (TrdEnv.SIMULATE)
  --test-connect   接続テストして終了
  --demo-compare   7変数パラメータシミュレーション (発注なし)
  --dry-test       futu接続なし・市場時間外でも全ロジックをテスト
                   (VIX/SPY価格はYahoo/Finnhubから実データ取得)

NOTE: LaunchAgent 22:00 JST (= 9:00 EDT) 起動必須
      OpenD ログイン済み・127.0.0.1:11111 動作中であること
"""

import os
import sys
import csv
import json
import math
import re
import time
import uuid
import logging
import datetime
import resource
import requests
import traceback
import zoneinfo
from pathlib import Path
from typing import Optional

# ── .env loader ────────────────────────────────────────────────────────────────
def _load_env_file():
    """
    .envファイルの値で環境変数を上書きする。
    setdefault(既存値優先)ではなく明示的に上書きすることで、
    .zshrc等の古い設定より.envの最新設定を優先する（V2-H2対応）。
    """
    for candidate in [Path("/root/spxbot/.env"), Path(__file__).parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                k, v = key.strip(), val.strip()
                if k in os.environ and os.environ[k] != v:
                    import sys as _sys
                    print(
                        f"[.env] {k} overriding existing value "
                        f"(old={os.environ[k][:20]!r} new={v[:20]!r})",
                        file=_sys.stderr,
                    )
                os.environ[k] = v
            break

_load_env_file()

# ── Path constants ─────────────────────────────────────────────────────────────
_BASE_DIR = Path(os.environ.get("SPY_DATA_DIR", Path(__file__).parent / "data"))
LOG_DIR   = Path(os.environ.get("SPY_LOG_DIR", _BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "condor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("spx_condor")

# ── Timezone ────────────────────────────────────────────────────────────────────
ET  = zoneinfo.ZoneInfo("America/New_York")
JST = zoneinfo.ZoneInfo("Asia/Tokyo")

# ── Credentials ────────────────────────────────────────────────────────────────
FINNHUB_API_KEY      = os.environ.get("FINNHUB_API_KEY", "")
PUSHOVER_TOKEN       = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_ALERT_TOKEN = os.environ.get("PUSHOVER_ALERT_TOKEN", "")  # SPX Alert — 障害・緊急用
PUSHOVER_USER        = os.environ.get("PUSHOVER_USER", "")
TRADE_PASSWORD = os.environ.get("TRADE_PASSWORD", "")

# ── Bot identity ────────────────────────────────────────────────────────────────
STRATEGY_NAME   = "SPY Credit Spread 0DTE v2"
UNDERLYING_CODE = "US.SPY"

OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111

# ══════════════════════════════════════════════════════════════════════════════
# Strategy parameters
# ══════════════════════════════════════════════════════════════════════════════

# ── Standard entry (10:30 ET, SMA direction) ──────────────────────────────────
# dict key = VIX upper bound (exclusive). None = no trade.
STANDARD_PARAMS = {
    22:  {"delta": 0.25, "width": 10, "capital_pct": 0.55},  # VIX < 22: normal
    35:  {"delta": 0.20, "width": 10, "capital_pct": 0.40},  # VIX 22-35: elevated
    999: None,                                                 # VIX >= 35: skip standard
}

# ── ORF entry (13:00 ET, opening-range-fade direction) ───────────────────────
# Activated only when VIX >= ORF_VIX_THRESHOLD and |orf_move| >= ORF_MOVE_THRESHOLD
ORF_PARAMS = {
    35:  {"delta": 0.20, "width": 10, "capital_pct": 0.40},  # VIX 22-35 + ORF
    50:  {"delta": 0.15, "width": 10, "capital_pct": 0.30},  # VIX 35-50 + ORF
    999: None,                                                 # VIX >= 50: halt
}

# ── ORF trigger conditions ─────────────────────────────────────────────────────
# These constants serve as FALLBACK values only.
# At runtime, check_opening_range() computes dynamic thresholds:
#   ORF_VIX_THRESHOLD  → IntradayMonitor._vix_elevated_threshold * 0.9  (P70相当)
#   ORF_MOVE_THRESHOLD → base 0.008 scaled by VIX/20, cap=0.020
ORF_VIX_THRESHOLD  = 22     # fallback: VIX >= this → ORF check at 10:00 ET
ORF_MOVE_THRESHOLD = 0.008  # fallback: |move| >= 0.8% in first 30min → ORF triggered

# ── IVR (IV Rank) delta adjustment ─────────────────────────────────────────────
# Fallback constants (used when IVR history < 20 days).
# Dynamic thresholds are computed via MarketData.get_ivr_percentiles() at runtime.
IVR_HIGH = 75    # fallback: IVR > 75 → +0.05 delta
IVR_LOW  = 25    # fallback: IVR < 25 → -0.05 delta

# ── ThetaData-based IVR ────────────────────────────────────────────────────────
# True: 実績IVデータ(greeks_first_order_SPY.parquet)からIVRを算出する。
# False: 既存のVIX-52週レンジ方式を維持する。
# フォールバック: parquetファイル未存在・days不足時は既存方式にフォールする。
USE_THETADATA_IVR = True

# ── Portfolio Vega管理 ─────────────────────────────────────────────────────────
VEGA_WARN_THRESHOLD  = 1000   # ポートフォリオVega合計がこれを超えたら警告
VEGA_VIX_SPIKE_PCT   = 0.20   # VIX前日比+20%超でVegaリスク縮小発動
VEGA_REDUCE_FACTOR   = 0.70   # Vegaリスク縮小時の枚数係数（30%縮小）

# ── VIX spike recovery ─────────────────────────────────────────────────────────
VIX_SPIKE_THRESHOLD = 3.0  # previous day VIX rose >= 3 → recovery day → +0.05 delta

# ── Profit target & stop loss ─────────────────────────────────────────────────
PROFIT_TARGET  = 0.80  # fallback: 80% of net credit (used when VIX unavailable)
STOP_LOSS_MULT = 1.00  # 100% of net credit (= spread width cap)


def calc_dynamic_profit_target(vix: float, hours_remaining: float) -> float:
    """VIXと残り時間から動的プロフィットターゲットを算出する。

    設計根拠（data/research_dynamic_params_design.md より）:
    - PT=0.53-0.62帯域でWR>=90%。PT=0.80はほぼ未達
    - VIXが高いほどプレミアムが大きい → PT高め
    - 残り時間が少ないほど到達困難 → PT低め

    基本式: PT = 0.50 + (vix - 15) * 0.01 - (6 - hours_remaining) * 0.03
      - vix=15, hours=6 → 0.50 (基準)
      - vix=20, hours=6 → 0.55 (高VIX)
      - vix=15, hours=2 → 0.38 → floor=0.40
      - vix=30, hours=1 → 0.50 + 0.15 - 0.15 = 0.50

    Floor=0.40, Cap=0.80
    """
    pt = 0.50 + (vix - 15.0) * 0.01 - (6.0 - hours_remaining) * 0.03
    return max(0.40, min(0.80, round(pt, 4)))


def calc_dynamic_stop_loss(vix: float, hours_remaining: float) -> float:
    """VIXと残り時間から動的ストップロス倍率を算出する。

    設計根拠（data/research_dynamic_params_design.md より）:
    - バックテストでSL=1.0〜1.2帯域がavg最良
    - 低VIX: 時間価値が味方 → SL緩め（ノイズ損切りを防ぐ）
    - 高VIX: 急変リスク増大 → SL締め
    - 残り時間が少ない → ガンマリスク増大 → SL締め

    基本式: SL = 1.00 + (20 - vix) * 0.01 + (hours_remaining - 3) * 0.02
      - vix=20, hours=3 → 1.00 (基準)
      - vix=14, hours=5 → 1.00 + 0.06 + 0.04 = 1.10
      - vix=30, hours=5 → 1.00 - 0.10 + 0.04 = 0.94
      - vix=14, hours=1 → 1.00 + 0.06 - 0.04 = 1.02
      - vix=30, hours=1 → 1.00 - 0.10 - 0.04 = 0.86 → floor=0.90

    Floor=0.90, Cap=1.50
    """
    sl = 1.00 + (20.0 - vix) * 0.01 + (hours_remaining - 3.0) * 0.02
    return max(0.90, min(1.50, round(sl, 4)))


def calc_vix9d_vvix_size_factor(vix9d: Optional[float], vvix: Optional[float], vix: float) -> float:
    """VIX9D/VIX比率とVVIXからサイズ係数を算出する。

    条件:
      - VIX9D/VIX > 1.0: 短期ボラティリティが現在VIXを上回る → サイズ x0.7
      - VVIX > 120: ボラティリティのボラティリティが過熱 → サイズ x0.8
      - 両条件同時: x0.7 × x0.8 = x0.56

    vix9d または vvix が None の場合はその条件をスキップ。
    """
    factor = 1.0
    if vix9d is not None and vix > 0:
        ratio = vix9d / vix
        if ratio > 1.0:
            factor *= 0.7
            log.info(f"[VIX9D/VVIX] VIX9D/VIX={ratio:.3f} > 1.0 → size × 0.7")
    if vvix is not None and vvix > 120:
        factor *= 0.8
        log.info(f"[VIX9D/VVIX] VVIX={vvix:.1f} > 120 → size × 0.8")
    return round(factor, 4)


# ── Multi-symbol mode ─────────────────────────────────────────────────────────
# True: プレマーケットで上位N銘柄を選択して同時運用。各銘柄独立エントリー/エグジット
# False (--no-multi): 従来の1銘柄モード（後方互換）
ENABLE_MULTI_SYMBOL     = True
MULTI_SYMBOL_MAX_N      = 3     # 本番: 最大3銘柄
MULTI_SYMBOL_MAX_N_PAPER = 5   # ペーパー: 最大5銘柄（検証重視）
MULTI_SYMBOL_MIN_SCORE  = 30.0  # この閾値以上のスコアのみ追加銘柄として採用

# ── Paper mass-verify mode ────────────────────────────────────────────────────
# ペーパーモードでは全銘柄×全戦術を並列実行して1日50〜100件のトレードを生成する。
# 銘柄スコアフィルタなし・各銘柄に複数戦術を同時割当・PortfolioRisk制限なし。
PAPER_MASS_VERIFY_MODE = True   # Falseにすると従来の5銘柄1戦術モードに戻る
PAPER_MASS_VERIFY_SYMBOLS = [   # 大量検証対象の全10銘柄 (US..SPXはSPXW混入源のため除外)
    "US.SPY", "US.QQQ", "US.IWM",               # ETF (US..SPXはSPXWと別管理)
    "US.TSLA", "US.NVDA", "US.AAPL",             # 個別株 (月水金0DTE)
    "US.MSFT", "US.AMZN", "US.META", "US.GOOGL", # 個別株
]
PAPER_MASS_VERIFY_TACTICS = ["cs_sell", "orb_buy", "straddle_buy"]  # 全戦術
# active_symbols キーフォーマット: "{symbol}_{tactic}" で銘柄×戦術をユニーク管理
PAPER_MASS_VERIFY_ENTRY_INTERVAL_MIN = 90  # 同一symbol+tacticで再エントリーまでの間隔(分)

# ── Position limits ───────────────────────────────────────────────────────────
MAX_QTY                = 3      # 本番: 3 contracts max (gap protection)
MAX_QTY_PAPER          = 20     # ペーパー: デモ口座証拠金制約に合わせて調整
SMALL_ACCOUNT_USD      = 15000  # below this: 1 contract max
MAX_CONSECUTIVE_LOSSES = 3      # halt after 3 straight losses

# ── Entry / exit windows (ET) ─────────────────────────────────────────────────
STANDARD_ENTRY_H  = 10
STANDARD_ENTRY_M  = 30
ORF_CHECK_H       = 10
ORF_CHECK_M       = 0
ORF_ENTRY_H       = 13
ORF_ENTRY_M       = 0
FORCE_CLOSE_H     = 15
FORCE_CLOSE_M     = 50

# ── Dynamic entry window (P2) ─────────────────────────────────────────────────
# True: 市場時間中（ET 9:45〜15:30）は毎tick条件チェック、揃ったらエントリー
# False: 従来の固定時刻（10:30 standard / 13:00 ORF）即エントリー動作に戻す
ENABLE_DYNAMIC_ENTRY_WINDOW  = True
# [DEPRECATED - False時フォールバック専用] standard: 10:30-11:00 ET
STANDARD_WINDOW_END_H        = 11
STANDARD_WINDOW_END_M        = 0
# [DEPRECATED - False時フォールバック専用] ORF: 13:00-13:30 ET
ORF_WINDOW_END_H             = 13
ORF_WINDOW_END_M             = 30
# 常時判定モード: オープンから最低15分経過後からエントリー可 (ET 9:45以降)
DYNAMIC_ENTRY_MIN_OPEN_MIN   = 30   # whipsaw回避: 市場オープン後30分間はエントリーしない (10:00 ET以降)
# 常時判定モード: 15:30 ET以降はエントリーしない (0DTE残り時間20分では意味がない)
DYNAMIC_ENTRY_CUTOFF_H       = 15
DYNAMIC_ENTRY_CUTOFF_M       = 30
# エントリー判定の必須条件閾値
DYNAMIC_ENTRY_MIN_ENV_SCORE  = 60   # 環境スコア最低ライン
# VRPはweakシグナル扱い（False=必須にしない・Trueで必須化）
DYNAMIC_ENTRY_VRP_REQUIRED   = False

# ── Early close days (半日取引日) ─────────────────────────────────────────────
# NYSE公式パターン: ブラックフライデー / クリスマス前日 / 独立記念日前日
# 全て13:00 ET クローズ。毎年1月に次年度分を追加すること。
# 注意: 2026-07-03は独立記念日前日だが7/4=土曜のため全日休場（半日ではない）
EARLY_CLOSE_DAYS = {
    # date_str (YYYY-MM-DD) -> (close_hour, close_minute) in ET
    "2026-11-27": (13, 0),  # ブラックフライデー
    "2026-12-24": (13, 0),  # クリスマス前日
    "2027-07-03": (13, 0),  # 独立記念日前日（7/4=日->観察日7/5 なので7/3は半日）
    "2027-11-26": (13, 0),  # ブラックフライデー
    "2027-12-24": (13, 0),  # クリスマス前日
}
# 半日取引日の強制決済時刻: クローズ15分前 (12:45 ET)
EARLY_CLOSE_FORCE_H = 12
EARLY_CLOSE_FORCE_M = 45
# 半日取引日の graceful exit 時刻: クローズ30分後 (13:30 ET)
EARLY_CLOSE_EXIT_H  = 13
EARLY_CLOSE_EXIT_M  = 30


def is_early_close_today() -> bool:
    """今日が半日取引日かどうかを返す。"""
    today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
    return today_str in EARLY_CLOSE_DAYS


def get_early_close_time():
    """今日の早期クローズ時刻 (hour, minute) in ET のタプル。半日でなければ None。"""
    today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
    return EARLY_CLOSE_DAYS.get(today_str)


# ── SMA ───────────────────────────────────────────────────────────────────────
SMA_PERIOD = 20

# ── Margin usage monitoring (G-NEW1) ─────────────────────────────────────────
# initial_margin / total_assets > MARGIN_USAGE_MAX_ENTRY → エントリー停止
# initial_margin / total_assets > MARGIN_USAGE_ALERT     → Pushover警告
MARGIN_USAGE_MAX_ENTRY   = 0.70   # 70%超 → 新規エントリー停止
MARGIN_USAGE_ALERT       = 0.90   # 90%超 → 緊急Pushover警告

# ── Bid/Ask spread quality filter (G-NEW2) ────────────────────────────────────
# slippage_est / net_credit > SPREAD_COST_RATIO_MAX → スキップ（スリッページが利益の1/3以上）
SPREAD_COST_RATIO_MAX    = 0.33

# ── Capital phase auto-transition (G-NEW9) ────────────────────────────────────
# 口座残高に応じてパラメータを自動調整
# Phase 1: ~375万円 (PDT制限あり) / Phase 2: ~1000万円 / Phase 3: 1000万円超
CAPITAL_PHASE_USD = {
    1: {"threshold_usd": 25000,  "max_qty": 2,  "max_risk_pct": 0.05, "tactics": ["CS", "IC", "ORB"]},
    2: {"threshold_usd": 67000,  "max_qty": 5,  "max_risk_pct": 0.06, "tactics": ["CS", "IC", "ORB", "CALENDAR"]},
    3: {"threshold_usd": 999999, "max_qty": 10, "max_risk_pct": 0.07, "tactics": ["CS", "IC", "ORB", "CALENDAR", "STRADDLE"]},
}

# ── Portfolio Greeks monitoring (N3-v2) ──────────────────────────────────────
# IntradayMonitor.tick()内で5分ごとにポートフォリオグリークスを計算してアクション
GREEKS_CHECK_INTERVAL_TICKS = 5   # tick=60s × 5 = 5分ごとにグリークスチェック
# ガンマ閾値: |total_gamma| > GAMMA_RISK_THRESHOLD → ポジション縮小検討
GAMMA_RISK_THRESHOLD     = 0.05   # 0DTE CS 3枚相当の目安

# ── Delta Hedge (N4-DH) ───────────────────────────────────────────────────────
# CS/IC 売りポジションのネットDeltaをモニターし過大な偏りをヘッジする
DELTA_HEDGE_TRIGGER      = 0.30   # |total_delta| > この値でヘッジ発動
DELTA_HEDGE_TRIGGER_LATE = 0.40   # 14:30 ET以降（Gamma爆発帯）はより保守的な閾値
DELTA_HEDGE_UNWIND       = 0.15   # |total_delta| < この値でヘッジ解除
# ヘッジ枚数係数: ヘッジ用CALL/PUTの想定delta（ATM近辺）
DELTA_HEDGE_CONTRACT_DELTA = 0.50 # ヘッジ用CALL/PUTの想定delta（ATM近辺）
# PDT動的制御: 緊急ヘッジ発動の絶対Delta閾値（$25K未満・PDT枠残あり時）
DELTA_HEDGE_EMERGENCY_THRESHOLD = 0.50  # |total_delta| > 0.5 = 緊急（PDT枠消費してでもヘッジ）
# PDT週次ヘッジ発動上限（FINRA PDT: 5営業日で3回まで当日往復取引可）
DELTA_HEDGE_WEEKLY_BUDGET = 3  # 週3回まで緊急ヘッジにPDT枠を使用可

# ── Theta Decay最適化 (N4-TH) ────────────────────────────────────────────────
# 過去30日の時間帯別Theta/Premium比率から最適エントリー時間帯を算出する
THETA_OPTIMAL_LOOKBACK_DAYS = 30  # 分析対象: 過去30日
# Theta/Premium比率スコア閾値: この値以上の時間帯を「優良帯」とする
THETA_OPTIMAL_MIN_RATIO  = 0.005  # 1日あたりTheta/Premium >= 0.5%

# ── Intraday Monitor (P0) ─────────────────────────────────────────────────────
ENABLE_INTRADAY_MONITOR = True
INTRADAY_TICK_SEC        = 60   # VIXレジーム監視間隔（秒）
INTRADAY_FULL_EVAL_SEC   = 900  # フル環境再評価間隔（15分=900秒）
# VIXレジーム閾値は過去60日のパーセンタイルから動的算出（固定値禁止）
VIX_HISTORY_DAYS         = 60
VIX_CALM_PERCENTILE      = 30   # P30以下 → calm
VIX_ELEVATED_PERCENTILE  = 80   # P80以上 → elevated
VIX_CRISIS_PERCENTILE    = 95   # P95以上 → crisis
# VIX変化率閾値（%/時）— こちらも動的計算の基準
VIX_RATE_ELEVATED        = 5.0  # 5%/h → elevated
VIX_RATE_CRISIS          = 10.0 # 10%/h → crisis

# ── Crash retry (P0: メインループ自律回復) ───────────────────────────────────
# True にすると run_forever() がクラッシュ後に最大 MAX_CRASH_RETRIES 回再試行する。
# テスト用: _main_loop() 内で raise RuntimeError("test crash") を一時挿入して確認。
ENABLE_CRASH_RETRY = True
MAX_CRASH_RETRIES  = 3        # 最大再試行回数（超えたら終了・LaunchAgentが再起動）
CRASH_BACKOFF_SEC  = 30       # 再接続前の待機時間（秒）

# ── Limit order entry (P1) ────────────────────────────────────────────────────
# True: 指値注文（midプライス起点 → 10秒ごとに0.01改善 → 5回後に成行フォールバック）
# False: 従来の成行注文に戻す（緊急切り戻し用）
ENABLE_LIMIT_ENTRY       = True
LIMIT_ADJUST_INTERVAL    = 10    # 秒: 約定確認→価格改善の間隔
LIMIT_ADJUST_STEP        = 0.01  # 1セント刻みで価格改善
LIMIT_MAX_ADJUST_STEPS   = 5     # 最大5回調整（5セント分）
LIMIT_HIGH_VIX_THRESHOLD = 30    # このVIX以上は最初から成行（急変時はスリッページ最小化優先）

# ── VRP (Volatility Risk Premium) P1 ─────────────────────────────────────────
ENABLE_VRP_CHECK         = True
VRP_REALIZED_DAYS        = 30   # 実現ボラティリティ計算日数
VRP_NEGATIVE_PENALTY     = 15   # VRP < 0 時の環境スコア減点

# ── Economic Calendar (P2) ────────────────────────────────────────────────────
ECON_CALENDAR_FILE       = _BASE_DIR / "economic_calendar_2026.json"

# ── Key Level Integration (G1+G2+G13) ────────────────────────────────────────
# True: ES ONレンジ/前日SPY OHLC-VWAP/OI集中ストライク/EMを統合して毎朝算出
# sell_strikeがKEY_LEVEL_PROXIMITYドル以内の場合はエントリーをスキップする
ENABLE_KEY_LEVELS    = True
KEY_LEVEL_PROXIMITY  = 2.0  # ドル: Key Level ±2.0内にsell_strikeがあればスキップ

# ══════════════════════════════════════════════════════════════════════════════
# File paths
# ══════════════════════════════════════════════════════════════════════════════
PNL_FILE            = _BASE_DIR / "condor_pnl.json"
EVENTS_FILE         = _BASE_DIR / "events.json"
SMA_CACHE_FILE      = _BASE_DIR / "sma_cache.json"
IVR_CACHE_FILE      = _BASE_DIR / "ivr_cache.json"
VIX_SPIKE_FILE      = _BASE_DIR / "vix_spike.json"
FAILURES_FILE       = _BASE_DIR / "spx_condor_failures.json"
MEMORY_WARN_FILE    = _BASE_DIR / "condor_memory_warn.json"
RECOVERY_COUNT_FILE  = _BASE_DIR / "recovery_count.json"
NEXT_DAY_BIAS_FILE   = _BASE_DIR / "next_day_bias.json"
DEMO_LOG_FILE        = LOG_DIR / "demo_compare.log"
REPORTS_DIR          = _BASE_DIR / "reports"
MEMORY_WARN_PCT     = 80
VIX_OPTIMAL_PARAMS_FILE = _BASE_DIR / "thetadata" / "vix_optimal_params.json"

# ── NYSE holidays 2025-2027 ────────────────────────────────────────────────────
US_HOLIDAYS = {
    datetime.date(2025, 1, 1),  datetime.date(2025, 1, 20), datetime.date(2025, 2, 17),
    datetime.date(2025, 4, 18), datetime.date(2025, 5, 26), datetime.date(2025, 6, 19),
    datetime.date(2025, 7, 4),  datetime.date(2025, 9, 1),  datetime.date(2025, 11, 27),
    datetime.date(2025, 12, 25),
    datetime.date(2026, 1, 1),  datetime.date(2026, 1, 19), datetime.date(2026, 2, 16),
    datetime.date(2026, 4, 3),  datetime.date(2026, 5, 25), datetime.date(2026, 6, 19),
    datetime.date(2026, 7, 3),  datetime.date(2026, 9, 7),  datetime.date(2026, 11, 26),
    datetime.date(2026, 11, 27),datetime.date(2026, 12, 25),
    datetime.date(2027, 1, 1),  datetime.date(2027, 1, 18), datetime.date(2027, 2, 15),
    datetime.date(2027, 3, 26), datetime.date(2027, 5, 31), datetime.date(2027, 6, 18),
    datetime.date(2027, 7, 5),  datetime.date(2027, 9, 6),  datetime.date(2027, 11, 25),
    datetime.date(2027, 12, 24),
}

NOTRADE_KEYWORDS = ["fomc", "cpi", "nfp", "non-farm", "opex", "quadruple",
                    "pce", "gdp", "jobless", "claims"]

# ══════════════════════════════════════════════════════════════════════════════
# Symbol-specific parameters  (data/symbol_params.json)
# ══════════════════════════════════════════════════════════════════════════════

SYMBOL_PARAMS_FILE = _BASE_DIR / "symbol_params.json"
_SYMBOL_PARAMS: dict = {}


def load_symbol_params() -> dict:
    """data/symbol_params.json を読み込んでキャッシュする。

    ファイルが存在しない・破損の場合は空 dict を返す（フォールバック動作継続）。
    """
    global _SYMBOL_PARAMS
    try:
        p = SYMBOL_PARAMS_FILE
        if p.exists():
            data = json.loads(p.read_text())
            _SYMBOL_PARAMS = data
            log.info(f"[SymbolParams] ロード: {[k for k in data if not k.startswith('_')]} 銘柄")
            return data
        else:
            log.warning(f"[SymbolParams] {p} が存在しない → デフォルト値使用")
    except Exception as e:
        log.warning(f"[SymbolParams] ロード失敗: {e}")
    _SYMBOL_PARAMS = {}
    return {}


def get_param(symbol: str, tactic: str, param_name: str):
    """symbol_params.json から指定パラメータを取得する。

    優先順位: symbol.overrides[tactic][param_name]
              > _defaults[tactic][param_name]
              > None

    Args:
        symbol:     "US.SPY" 等の銘柄コード
        tactic:     "cs" / "ic" / "orb" / "straddle" / "calendar" / "risk"
        param_name: キー名 (例: "width_atr_mult")

    Returns:
        値 or None (見つからない場合)
    """
    params = _SYMBOL_PARAMS
    if not params:
        return None

    # symbol 固有の override
    sym_entry = params.get(symbol, {})
    overrides  = sym_entry.get("overrides", {})
    tactic_ov  = overrides.get(tactic, {})
    if param_name in tactic_ov:
        return tactic_ov[param_name]

    # _defaults フォールバック
    defaults = params.get("_defaults", {})
    tactic_def = defaults.get(tactic, {})
    return tactic_def.get(param_name)


def get_symbol_meta(symbol: str) -> dict:
    """symbol_params.json から銘柄メタ情報を返す。

    Returns:
        {
          "strike_interval": 1.0,
          "type": "etf" / "stock",
          "has_earnings": bool,
          "baseline_volume": int,
          "baseline_spread_pct": float,
          "earnings_date": "YYYY-MM-DD" (optional),
        }
        銘柄が見つからない場合は空 dict。
    """
    params = _SYMBOL_PARAMS
    entry = params.get(symbol, {})
    return {k: v for k, v in entry.items() if k != "overrides"}


def calc_dynamic_width(symbol: str, atr_14: Optional[float]) -> int:
    """ATR(14) と symbol_params の width_atr_mult からスプレッド幅を動的算出する。

    計算式: width = max(min_width_strikes, round(ATR_14 * width_atr_mult))
    ATR が None の場合は STANDARD_PARAMS のデフォルト幅 (10) を返す。
    strike_interval (ストライク刻み) に合わせて丸める。

    SPY  (ATR≈5, mult=0.50): 2.5 → 3 (min=1)
    TSLA (ATR≈15, mult=0.50): 7.5 → 8
    """
    if atr_14 is None:
        return 10  # フォールバック: 既存デフォルト値

    mult      = get_param(symbol, "cs", "width_atr_mult") or 0.50
    min_width = get_param(symbol, "cs", "min_width_strikes") or 1
    interval  = get_symbol_meta(symbol).get("strike_interval") or 1.0

    raw = atr_14 * mult
    # ストライク刻みに合わせて丸め（interval単位で最も近い値）
    if interval > 0:
        rounded = round(raw / interval) * interval
    else:
        rounded = round(raw)

    width = max(min_width, max(1, int(rounded)))
    log.info(
        f"[DynWidth] {symbol}: ATR={atr_14:.2f} × mult={mult} = {raw:.2f} "
        f"→ interval={interval} → width={width}"
    )
    return width


def calc_hv_adjusted_sl(symbol: str, base_sl: float, hv_20: Optional[float]) -> float:
    """HV(20日) からストップロス倍率を調整する。

    式: adjusted_sl = base_sl * (1 + max(0, HV_20 - 0.20) * sl_vol_adj_coeff)
    HV が None の場合は base_sl をそのまま返す。

    SPY  (HV=0.22): 1.00 * (1 + max(0, 0.22-0.20)) = 1.02 → ほぼ変わらない
    TSLA (HV=0.45): 1.00 * (1 + 0.25*1.3) = 1.325
    """
    if hv_20 is None:
        return base_sl

    coeff = get_param(symbol, "cs", "sl_vol_adj_coeff") or 1.0
    adjusted = base_sl * (1.0 + max(0.0, hv_20 - 0.20) * coeff)
    adjusted = round(max(0.90, min(3.00, adjusted)), 4)
    log.info(
        f"[HVAdjSL] {symbol}: base_sl={base_sl:.4f} HV={hv_20:.3f} coeff={coeff} "
        f"→ adjusted_sl={adjusted:.4f}"
    )
    return adjusted


def calc_orb_breakout_threshold(symbol: str, atr_daily_pct: Optional[float]) -> float:
    """ATR日次%からORBブレイクアウト閾値を動的算出する。

    式: threshold = ATR_daily_pct * breakout_atr_pct_mult
    ATR が None の場合は ORF_MOVE_THRESHOLD のフォールバック値 (0.008) を返す。

    SPY  (ATR 1.5%): 1.5% * 0.30 = 0.45%
    TSLA (ATR 3.5%): 3.5% * 0.25 = 0.875% (TSLA override: mult=0.25)
    """
    if atr_daily_pct is None:
        return ORF_MOVE_THRESHOLD  # fallback

    mult = get_param(symbol, "orb", "breakout_atr_pct_mult") or 0.30
    threshold = round(atr_daily_pct * mult, 5)
    log.info(
        f"[ORBThreshold] {symbol}: ATR%={atr_daily_pct:.4f} × mult={mult} "
        f"→ breakout_threshold={threshold:.4f}"
    )
    return threshold


def _futu_to_yahoo_ticker(symbol: str) -> str:
    """futu シンボルコードを Yahoo Finance ティッカーに変換する。

    US.SPY  → "SPY"
    US.QQQ  → "QQQ"
    US..SPX → "^SPX"   ← ドット2つはCBOEインデックス（先頭 ^ が必要）
    US.TSLA → "TSLA"
    """
    if symbol.startswith("US.."):
        return "^" + symbol[4:]
    return symbol.replace("US.", "", 1)


# 起動時にロード
load_symbol_params()

# ── VIX帯別最適パラメータ (data/thetadata/vix_optimal_params.json から起動時ロード) ──
# JSONが存在しない場合は {} → apply_vix_band_overrides が既存ロジックにフォールバック
def _load_vix_optimal_params() -> dict:
    try:
        if VIX_OPTIMAL_PARAMS_FILE.exists():
            data = json.loads(VIX_OPTIMAL_PARAMS_FILE.read_text())
            log.info(f"[VIXBand] vix_optimal_params.json ロード: {list(data.keys())}")
            return data
    except Exception as e:
        log.warning(f"[VIXBand] vix_optimal_params.json ロード失敗: {e}")
    return {}

_VIX_BAND_PARAMS: dict = _load_vix_optimal_params()


def get_vix_band(vix: float) -> str:
    """VIX値からVIX帯名を返す。

    calm:    VIX < 15
    normal:  15 <= VIX < 20
    elevated: 20 <= VIX < 25
    high:    25 <= VIX < 30
    crisis:  VIX >= 30
    """
    if vix < 15:
        return "calm"
    if vix < 20:
        return "normal"
    if vix < 25:
        return "elevated"
    if vix < 30:
        return "high"
    return "crisis"


def apply_vix_band_overrides(params: dict, vix: float) -> tuple:
    """VIX帯JSONからwidth/take_profitを params に上書きし、size_factorを返す。

    Returns:
        (params_updated: dict, size_factor: float, band: str)
        - params_updated: width が上書きされたparams（shallow copy）
        - size_factor: high/crisis = 0.5、それ以外 = 1.0
        - band: "calm"/"normal"/"elevated"/"high"/"crisis"
    """
    if not _VIX_BAND_PARAMS:
        return params, 1.0, "unknown"

    band = get_vix_band(vix)
    band_cfg = _VIX_BAND_PARAMS.get(band)
    if not band_cfg:
        return params, 1.0, band

    updated = dict(params)

    # width を JSON 値で上書き
    if "width" in band_cfg and band_cfg["width"] is not None:
        old_w = updated.get("width")
        updated["width"] = int(band_cfg["width"])
        log.info(
            f"[VIXBand] {band}: width {old_w} → {updated['width']} "
            f"(VIX={vix:.1f})"
        )

    # high/crisis は confidence=low → サイズを50%に制限
    confidence = band_cfg.get("confidence", "high")
    size_factor = 0.5 if confidence == "low" else 1.0
    if size_factor < 1.0:
        log.info(
            f"[VIXBand] {band}: confidence=low → size_factor=0.5 (VIX={vix:.1f})"
        )

    return updated, size_factor, band

# ── futu import guard ─────────────────────────────────────────────────────────
FUTU_AVAILABLE = False
try:
    from futu import (OpenQuoteContext, OpenSecTradeContext,
                      TrdMarket, TrdEnv, TrdSide, OrderType, ModifyOrderOp,
                      TimeInForce,
                      RET_OK, SecurityFirm,
                      StockQuoteHandlerBase, SubType)
    import futu as ft
    FUTU_AVAILABLE = True
except ImportError:
    log.warning("futu-api not installed; running in dry-run mode")

# ── dry-test mode flag (set from args in __main__) ─────────────────────────────
# True: futu発注なし・市場時間バイパス・VirtualPositionManager使用・実データ取得
DRY_TEST: bool = False

# ── 外部モジュール import guard ────────────────────────────────────────────────
# portfolio_risk: Bot間ポートフォリオリスク統合
_PORTFOLIO_RISK_AVAILABLE = False
try:
    from portfolio_risk import (
        can_take_risk, update_positions as _pr_update_positions,
        clear_positions as _pr_clear_positions,
        check_weekly_dd, check_monthly_dd,
        check_direction_conflict, record_daily_pnl,
    )
    _PORTFOLIO_RISK_AVAILABLE = True
    log.info("[Module] portfolio_risk ロード成功")
except ImportError as _e:
    log.warning(f"[Module] portfolio_risk ロード失敗 → 保守的フォールバック動作: {_e}")
    # 防衛的: モジュール不在時は新規リスクを一切取らず、DD超過扱いで新規停止・衝突扱いで重複回避
    def can_take_risk(additional_risk, account_balance):
        log.warning("[PortfolioRisk/fallback] 保守的判定: can_take_risk=False（モジュール未ロード）")
        return False
    def _pr_update_positions(bot_name, positions): pass
    def _pr_clear_positions(bot_name): pass
    def check_weekly_dd(account_balance): return True  # DD超過扱いで新規停止（保守的）
    def check_monthly_dd(account_balance): return True  # DD超過扱いで新規停止（保守的）
    def check_direction_conflict(spy_direction, momentum_direction): return True  # 衝突扱いで重複回避（保守的）
    def record_daily_pnl(date_str, pnl_usd, bot_name): pass

# strategy_selector: 環境適応型戦術選択エンジン（Phase 1: ログ出力のみ）
_STRATEGY_SELECTOR_AVAILABLE = False
try:
    from strategy_selector import select_strategy as _ss_select_strategy
    _STRATEGY_SELECTOR_AVAILABLE = True
    log.info("[Module] strategy_selector ロード成功")
except ImportError as _e:
    log.warning(f"[Module] strategy_selector ロード失敗 → フォールバック動作: {_e}")
    def _ss_select_strategy(env): return None

# symbol_selector: 銘柄選択エンジン（Phase 1: ログ出力のみ）
_SYMBOL_SELECTOR_AVAILABLE = False
try:
    from symbol_selector import SymbolSelector as _SymbolSelector
    _SYMBOL_SELECTOR_AVAILABLE = True
    log.info("[Module] symbol_selector ロード成功")
except ImportError as _e:
    log.warning(f"[Module] symbol_selector ロード失敗 → フォールバック動作: {_e}")
    _SymbolSelector = None

# greeks_monitor: ポートフォリオギリシャ合計管理
_GREEKS_MONITOR_AVAILABLE = False
try:
    from greeks_monitor import (
        calc_portfolio_greeks as _gm_calc_portfolio_greeks,
        check_greeks_limits as _gm_check_greeks_limits,
    )
    _GREEKS_MONITOR_AVAILABLE = True
    log.info("[Module] greeks_monitor ロード成功")
except ImportError as _e:
    log.warning(f"[Module] greeks_monitor ロード失敗 → フォールバック動作: {_e}")
    def _gm_calc_portfolio_greeks(positions, quote_ctx=None): return {}
    def _gm_check_greeks_limits(greeks): return []


# ── Self-Checking Pair qty計算 (NASA TMR) ─────────────────────────────────────
_QTY_CALCULATOR_AVAILABLE = False
try:
    from common.qty_calculator import (
        calc_qty_verified as _calc_qty_verified,
        tmr_verify_spread_qty as _tmr_verify_spread_qty,
        QtyMismatchError,
    )
    _QTY_CALCULATOR_AVAILABLE = True
    log.info("[Module] common.qty_calculator ロード成功 (TMR発注検証有効)")
except ImportError as _e:
    log.warning(f"[Module] common.qty_calculator ロード失敗 → TMR検証なし: {_e}")
    def _calc_qty_verified(cash, premium, max_risk_pct, *, min_qty=1, max_qty=None):
        raise ImportError("common.qty_calculator not available")
    def _tmr_verify_spread_qty(cash, spread_width, capital_pct, qty_from_calc_qty):
        pass  # no-op fallback
    class QtyMismatchError(Exception):
        pass


# ══════════════════════════════════════════════════════════════════════════════
# VirtualPositionManager — dry-testモード用の仮想ポジション管理
# ══════════════════════════════════════════════════════════════════════════════
class VirtualPositionManager:
    """dry-testモードで実際の発注の代わりに使うインメモリポジション管理。

    TradeEngine のメソッドが dry-testフラグを確認してこのクラスに委譲する。
    """

    def __init__(self):
        self._positions: list = []

    def add_position(self, code: str, qty: int, cost_price: float, position_side: str):
        """エントリー時に仮想ポジションを追加する。
        position_side: "LONG" or "SHORT"
        """
        self._positions.append({
            "code":          code,
            "qty":           qty,
            "cost_price":    cost_price,
            "position_side": position_side,
            "unrealized_pl": 0.0,
        })
        log.info(f"[DRY-TEST][VirtualPos] added: {code} qty={qty} side={position_side} cost={cost_price:.2f}")

    def update_unrealized_pl(self, spy_current: float):
        """SPY現在価格を使ってオプション価値を概算しunrealized_plを更新する。
        0DTE ATMオプションの時間価値はゼロに向かうため、
        sell脚(SHORT)の価値が下がる → P&L=プラス と近似する。
        簡易モデル: 各レグのcost_priceからの変動を時間経過比例で減衰させる。
        """
        now_et = datetime.datetime.now(ET)
        # 市場開始から終了(15:50)までの経過割合を時間価値減衰に使う
        session_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        session_end   = now_et.replace(hour=15, minute=50, second=0, microsecond=0)
        total_secs = (session_end - session_start).total_seconds()
        elapsed_secs = max(0.0, (now_et - session_start).total_seconds())
        decay_ratio = min(elapsed_secs / total_secs, 1.0) if total_secs > 0 else 0.5

        for pos in self._positions:
            cost = pos.get("cost_price", 0.0)
            qty  = abs(pos.get("qty", 1))
            if pos.get("position_side") == "SHORT":
                # SELLした (credit received): 時間経過でオプション価値が下がる → 利益
                remaining_val = cost * (1.0 - decay_ratio)
                pos["unrealized_pl"] = round((cost - remaining_val) * qty * 100, 2)
            else:
                # BUYした (hedge leg): 時間経過でオプション価値が下がる → 損失
                remaining_val = cost * (1.0 - decay_ratio)
                pos["unrealized_pl"] = round(-(cost - remaining_val) * qty * 100, 2)

    def get_positions(self) -> list:
        """現在の仮想ポジションリストを返す。"""
        return list(self._positions)

    def remove_all(self):
        """全仮想ポジションをクリアする。"""
        count = len(self._positions)
        self._positions.clear()
        log.info(f"[DRY-TEST][VirtualPos] cleared {count} positions")

    @property
    def has_positions(self) -> bool:
        return len(self._positions) > 0


# ══════════════════════════════════════════════════════════════════════════════
# Utility functions
# ══════════════════════════════════════════════════════════════════════════════

def pushover(title: str, message: str, priority: int = 0) -> bool:
    """Pushover通知を送信する。成功時True、失敗時Falseを返す（V2-M3対応）。"""
    # dry-testモードではタイトルに [DRY-TEST] プレフィックスを付加
    if DRY_TEST and not title.startswith("[DRY-TEST]"):
        title = f"[DRY-TEST] {title}"
    if not title.startswith("["):
        title = f"[Atlas] {title}"
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                  "title": title, "message": message, "priority": priority},
            timeout=10,
        )
        if not resp.ok:
            log.warning(f"pushover HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        log.warning(f"pushover: {e}")
        return False


def pushover_alert(title: str, message: str, priority: int = 1) -> bool:
    """SPX Alertトークンで緊急通知を送信。成功時True、失敗時Falseを返す（V2-M3対応）。"""
    # dry-testモードではタイトルに [DRY-TEST] プレフィックスを付加
    if DRY_TEST and not title.startswith("[DRY-TEST]"):
        title = f"[DRY-TEST] {title}"
    if not title.startswith("["):
        title = f"[Atlas/ALERT] {title}"
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_ALERT_TOKEN, "user": PUSHOVER_USER,
                  "title": title, "message": message, "priority": priority},
            timeout=10,
        )
        if not resp.ok:
            log.warning(f"pushover_alert HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        log.warning(f"pushover_alert: {e}")
        return False


def _check_pushover_token() -> bool:
    """
    起動時にPushoverトークンの有効性を確認する（V2-C2対応）。
    テストメッセージを1回送信し、HTTP 200以外なら警告ログを出してFalseを返す。
    """
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log.error("[startup] PUSHOVER_TOKEN or PUSHOVER_USER not set — notifications disabled")
        return False
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   PUSHOVER_TOKEN,
                "user":    PUSHOVER_USER,
                "title":   "[spy_bot] 起動確認",
                "message": "Pushoverトークン有効性確認 — spy_bot起動",
                "priority": -2,  # lowest priority: no sound/vibration
            },
            timeout=10,
        )
        if resp.ok:
            log.info("[startup] Pushover token OK")
            return True
        else:
            log.error(f"[startup] Pushover token check FAILED HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"[startup] Pushover token check exception: {e}")
        return False


def load_pnl() -> list:
    try:
        if PNL_FILE.exists():
            return json.loads(PNL_FILE.read_text()).get("trades", [])
    except Exception:
        pass
    return []


def _append_research_log(filename: str, record: dict):
    """調査用ログをJSONL形式で追記（ガンマスキャルピング・出来高スパイク）"""
    try:
        path = LOG_DIR / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"research_log: {e}")


_EXPIRY_RE = re.compile(r"^(?:US\.)?[A-Z]+(\d{6})[CP]")

def _option_is_expired(code: str, today_str: str) -> bool:
    """futuオプションコード (例: US.SPY260413P668000) のexpiryが
    today_str (YYYY-MM-DD) より前かどうかを返す。

    Args:
        code: futuオプションコード
        today_str: 今日の日付文字列 (YYYY-MM-DD)

    Returns:
        True if expiry < today (期限切れ), False otherwise
    """
    m = _EXPIRY_RE.match(code or "")
    if not m:
        return False  # パターン不一致はオプション以外とみなし除外しない
    yy_mm_dd = m.group(1)  # 例: "260413"
    try:
        expiry = datetime.date(2000 + int(yy_mm_dd[:2]),
                               int(yy_mm_dd[2:4]),
                               int(yy_mm_dd[4:6]))
        today = datetime.date.fromisoformat(today_str)
        return expiry < today
    except (ValueError, IndexError):
        return False


def append_pnl_entry(record: dict):
    try:
        PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
        trades = load_pnl()
        record.setdefault("date", datetime.datetime.now(ET).strftime("%Y-%m-%d"))
        record.setdefault("ts",   datetime.datetime.now(ET).isoformat())

        # バグ3修正: exit イベントの重複記録防止（冪等性）
        # 同日・同 spread_key・同 reason の exit が既に存在する場合はスキップする。
        if record.get("event") == "exit":
            _dup_key = (record.get("date"), record.get("spread_key"), record.get("reason"))
            if _dup_key[0] and _dup_key[1] and _dup_key[2]:
                for _existing in trades:
                    if (_existing.get("event") == "exit"
                            and _existing.get("date") == _dup_key[0]
                            and _existing.get("spread_key") == _dup_key[1]
                            and _existing.get("reason") == _dup_key[2]):
                        log.warning(
                            f"[append_pnl] 重複exit検出 → スキップ "
                            f"(date={_dup_key[0]}, spread_key={_dup_key[1]}, "
                            f"reason={_dup_key[2]})"
                        )
                        return

        trades.append(record)
        PNL_FILE.write_text(json.dumps({"trades": trades}, indent=2))
    except Exception as e:
        log.warning(f"append_pnl: {e}")


def sweep_expiry_pnl(date_str: Optional[str] = None, dry_run: bool = False) -> list:
    """当日（date_str、省略時はET今日）に entry があって exit がない0DTEエントリーを検出し、
    満期OTM消滅（full credit profit）として exit を記録する。

    0DTE CSの勝ちパターンはほぼ全て満期OTM消滅であるため、force_closeが約定しなかった場合
    （OTMでmoomooが拒否）にこの関数で確実にexitを記録する。

    16:05 ET以降に自動呼び出しされる。手動でも呼び出し可能。

    Args:
        date_str: 対象日付 (YYYY-MM-DD)。省略時はET今日。
        dry_run:  Trueの場合は記録せずに検出結果のみ返す。

    Returns:
        記録した（またはdry_runで検出した）レコードのリスト。
    """
    if date_str is None:
        date_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")

    trades = load_pnl()

    # 対象日のentryをtrade_id別に収集（trade_idがないものは除外）
    entries_by_tid: dict = {}
    for t in trades:
        if t.get("event") == "entry" and t.get("date") == date_str and t.get("trade_id"):
            tid = t["trade_id"]
            # 同一trade_idに複数entryがある場合は最後のものを使用
            entries_by_tid[tid] = t

    # 対象日のexitをtrade_id別に収集
    exits_by_tid: dict = {}
    for t in trades:
        if t.get("event") == "exit" and t.get("trade_id"):
            exits_by_tid[t["trade_id"]] = t

    # MassVerify系tacticはsweep対象外（_check_mass_verify_exitが別ルートでexit記録するため）
    # reconcile_pnl.py の MASS_VERIFY_TACTICS と同等のセットを維持する
    _SWEEP_EXCLUDE_TACTICS = {
        "MassVerify_CS",
        "parallel_A_baseline",
        "parallel_B_tight_delta",
        "parallel_C_wide_spread",
        "parallel_D_ic",
        "mass_verify_cs_sell",
        "mass_verify_orb_buy",
        "dry_test",
        "iv_crush_earnings",  # IVCrushEngineが別ルートでexit記録
    }

    # 未exitのentryを特定（sweep除外tacticはスキップ）
    orphan_tids = [
        tid for tid in entries_by_tid
        if tid not in exits_by_tid
        and entries_by_tid[tid].get("tactic", "") not in _SWEEP_EXCLUDE_TACTICS
    ]

    results = []
    for tid in orphan_tids:
        entry = entries_by_tid[tid]
        entry_credit = entry.get("net_credit")
        entry_qty    = entry.get("qty")
        tactic       = entry.get("tactic", "unknown")
        direction    = entry.get("direction", "unknown")
        sell_strike  = entry.get("sell_strike")
        buy_strike   = entry.get("buy_strike")
        signal_id    = entry.get("signal_id")

        try:
            pnl_usd = round(float(entry_credit) * int(entry_qty) * 100, 2) if (entry_credit is not None and entry_qty is not None) else 0.0
        except (ValueError, TypeError):
            pnl_usd = 0.0

        record = {
            "event":       "exit",
            "reason":      "expiry_sweep_otm",
            "date":        date_str,
            "ts":          f"{date_str}T16:05:00-04:00",  # 満期時刻 16:00 ET の直後
            "tactic":      tactic,
            "direction":   direction,
            "sell_strike": sell_strike,
            "buy_strike":  buy_strike,
            "pnl_usd":     pnl_usd,
            "entry_credit": entry_credit,
            "exit_status": "expiry_otm_full_profit",
            "trade_id":    tid,
            "signal_id":   signal_id,
        }

        results.append(record)

        if not dry_run:
            append_pnl_entry(record)
            log.info(
                f"[ExpirySweep] {date_str} trade_id={tid[:20]}... "
                f"tactic={tactic} direction={direction} "
                f"credit={entry_credit} qty={entry_qty} pnl=${pnl_usd:+.2f} → recorded"
            )
        else:
            log.info(
                f"[ExpirySweep DRY] {date_str} trade_id={tid[:20]}... "
                f"tactic={tactic} pnl=${pnl_usd:+.2f} → would record"
            )

    if not orphan_tids:
        log.debug(f"[ExpirySweep] {date_str}: 未exit entryなし（全トレード記録済み）")

    return results


def calc_kelly_fraction(
    pnl_file: Path,
    lookback: int = 20,
    strategy_filter: Optional[str] = None,
) -> Optional[float]:
    """直近lookbackトレードからHalf Kelly fractionを算出する。

    condor_pnl.jsonのexitイベントのpnl_usdを使用。
    トレード数が10未満の場合はNoneを返す（データ不足）。

    Args:
        pnl_file:        PnLファイルパス
        lookback:        直近何件を使うか（デフォルト20）
        strategy_filter: 指定した場合、そのstrategyのexitのみ対象にする。
                         Noneの場合は全strategy合算（後方互換）。
                         例: "ORB", "CS", "VIX-MR", "TF"

    Returns:
        Half Kelly fraction（上限0.25・下限0.05でクランプ）、またはNone
    """
    try:
        try:
            trades = json.loads(pnl_file.read_text()).get("trades", [])
        except Exception:
            trades = []
        exits = [
            t for t in trades
            if t.get("event") == "exit" and t.get("pnl_usd") is not None
            and (strategy_filter is None or t.get("strategy") == strategy_filter)
        ]
        recent = exits[-lookback:]
        if len(recent) < 10:
            return None

        wins = [t for t in recent if float(t["pnl_usd"]) > 0]
        losses = [t for t in recent if float(t["pnl_usd"]) <= 0]

        win_rate = len(wins) / len(recent)
        if not wins or not losses:
            # 全勝または全敗の場合はKelly算出不可（edge case）
            return None

        avg_win  = sum(float(t["pnl_usd"]) for t in wins) / len(wins)
        avg_loss = abs(sum(float(t["pnl_usd"]) for t in losses) / len(losses))
        if avg_loss == 0:
            return None

        b = avg_win / avg_loss  # 勝ち/負け比率
        kelly = (win_rate * b - (1 - win_rate)) / b
        half_kelly = kelly / 2.0

        return round(max(0.05, min(0.25, half_kelly)), 4)
    except Exception as e:
        log.warning(f"calc_kelly_fraction: {e}")
        return None


def _exit_fill_stats(last_exit_fills: dict) -> dict:
    """close_all_positions の _last_exit_fills から exit約定価格統計を返す。

    _last_exit_fills の構造:
        {code: {"price": float|None, "position_side": "SHORT"|"LONG"}}

    Returns:
        dict with keys:
            exit_fill_prices: {code: price}  (Noneを除いたもの)
            exit_fill_avg:    全レグのdealt_avg_priceの単純平均 (float|None)
            exit_net_cost:    SHORT脚avg - LONG脚avg (買い戻しコスト正味)
                              SHORT脚はcredit回収側、LONG脚は売却側
                              exit_net_cost > 0 → 買い戻しコストが大きい（損）
                              データ不足時は None
    """
    if not last_exit_fills:
        return {"exit_fill_prices": {}, "exit_fill_avg": None, "exit_net_cost": None}

    fill_prices: dict = {}
    short_prices: list = []
    long_prices: list = []
    for code, info in last_exit_fills.items():
        if isinstance(info, dict):
            price = info.get("price")
            ps    = info.get("position_side", "LONG")
        else:
            # 旧形式 (float|None) への後方互換
            price = info
            ps    = "LONG"
        if price is not None:
            fill_prices[code] = price
            if ps == "SHORT":
                short_prices.append(price)
            else:
                long_prices.append(price)

    avg = (round(sum(fill_prices.values()) / len(fill_prices), 4)
           if fill_prices else None)

    # exit_net_cost = SHORT脚平均（買い戻し代金）- LONG脚平均（売却代金）
    # クレジットスプレッドでは SHORT脚を買い戻し、LONG脚を売る
    exit_net_cost = None
    if short_prices and long_prices:
        short_avg = sum(short_prices) / len(short_prices)
        long_avg  = sum(long_prices) / len(long_prices)
        exit_net_cost = round(short_avg - long_avg, 4)

    return {
        "exit_fill_prices": fill_prices,
        "exit_fill_avg": avg,
        "exit_net_cost": exit_net_cost,
    }


def check_signal_divergence(signal_id: Optional[str]):
    """同一signal_idの本番/ペーパーentryとexitを照合し乖離を検知する。
    pnl_usdの符号が違うか30%以上乖離していたらPushover priority=1通知。
    """
    if not signal_id:
        return
    try:
        trades = load_pnl()
        exits = [t for t in trades
                 if t.get("signal_id") == signal_id and t.get("event") == "exit"
                 and t.get("pnl_usd") is not None]
        if len(exits) < 2:
            return  # 本番/ペーパー両方のexitが揃ってから比較
        pnls = [float(t["pnl_usd"]) for t in exits]
        # 符号違い
        signs = {1 if p >= 0 else -1 for p in pnls}
        if len(signs) > 1:
            pushover_alert(
                "signal乖離検知",
                f"signal_id={signal_id}\n"
                f"本番/ペーパーでP&L符号が逆転: {pnls}",
                priority=1,
            )
            return
        # 30%以上乖離（最大値と最小値の差がmax値の30%超）
        max_abs = max(abs(p) for p in pnls)
        min_abs = min(abs(p) for p in pnls)
        if max_abs > 0 and (max_abs - min_abs) / max_abs >= 0.30:
            pushover_alert(
                "signal乖離検知",
                f"signal_id={signal_id}\n"
                f"P&L 30%以上乖離: {pnls}",
                priority=1,
            )
    except Exception as e:
        log.warning(f"check_signal_divergence: {e}")


def append_monthly_csv(record: dict):
    try:
        from pathlib import Path as _P
        _P(LOG_DIR).mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(ET)
        csv_path = LOG_DIR / f"condor_{now.strftime('%Y-%m')}.csv"
        fieldnames = ["timestamp", "expiry", "direction", "sell_strike", "buy_strike",
                      "qty", "net_credit", "result", "tactic"]
        exists = csv_path.exists()
        with open(csv_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
                w.writeheader()
            record.setdefault("timestamp", now.isoformat())
            w.writerow(record)
    except Exception as e:
        log.warning(f"csv append: {e}")


def load_failures() -> int:
    try:
        if FAILURES_FILE.exists():
            return int(json.loads(FAILURES_FILE.read_text()).get("count", 0))
    except Exception:
        pass
    return 0


def save_failures(count: int):
    try:
        FAILURES_FILE.write_text(json.dumps({"count": count}))
    except Exception:
        pass


def get_capital_phase(cash_usd: float) -> dict:
    """口座残高（USD）から資金フェーズを自動判定してパラメータを返す（G-NEW9）。

    フェーズ定義（CAPITAL_PHASE_USD）:
      Phase 1: ~$25,000 (JPY約375万円 / PDT制限あり) → max_qty=2, risk=5%
      Phase 2: ~$67,000 (JPY約1000万円)               → max_qty=5, risk=6%
      Phase 3: $67,000超                               → max_qty=10, risk=7%

    Returns:
        {"phase": int, "max_qty": int, "max_risk_pct": float, "tactics": list}
    """
    for phase_num in sorted(CAPITAL_PHASE_USD.keys()):
        cfg = CAPITAL_PHASE_USD[phase_num]
        if cash_usd < cfg["threshold_usd"]:
            log.info(
                f"[CapitalPhase] Phase {phase_num}: "
                f"cash=${cash_usd:.0f} < ${cfg['threshold_usd']:,} "
                f"→ max_qty={cfg['max_qty']} max_risk={cfg['max_risk_pct']:.0%}"
            )
            return {"phase": phase_num, **cfg}
    # 最終フェーズ（Phase 3）
    cfg3 = CAPITAL_PHASE_USD[3]
    log.info(
        f"[CapitalPhase] Phase 3: cash=${cash_usd:.0f} "
        f"→ max_qty={cfg3['max_qty']} max_risk={cfg3['max_risk_pct']:.0%}"
    )
    return {"phase": 3, **cfg3}


def get_trading_mode(cash_usd: float, paper: bool = False) -> str:
    """資金サイズとペーパーフラグからPDT動作モードを決定する。

    ペーパーモード: 常に 'full'（規制外・全戦術検証用）
    本番モード:
      cash_usd < 25,000 → 'pdt_constrained'
        - CS/IC 売りのみ（OTM狙い・1DTE化）
        - ORB/Straddle買い/GammaScalp 無効
      cash_usd >= 25,000 → 'full'
        - 全戦術解禁

    Returns:
        'pdt_constrained' | 'full'
    """
    if paper:
        return "full"
    if cash_usd < 25000:
        log.info(
            f"[PDT] pdt_constrained モード: cash=${cash_usd:.0f} < $25,000 "
            f"→ CS/IC 1DTE のみ・ORB/Straddle/GammaScalp 無効"
        )
        return "pdt_constrained"
    log.info(
        f"[PDT] full モード解禁: cash=${cash_usd:.0f} >= $25,000 "
        f"→ 全戦術解禁"
    )
    return "full"


def should_delta_hedge(
    position_delta_abs: float,
    cash_usd: float,
    weekly_hedge_count: int,
    is_emergency: bool,
) -> tuple[bool, str]:
    """Delta Hedge発動可否をPDTリスクと緊急度から動的判定する。

    判定ロジック:
      $25K以上          → 無制限発動（PDT対象外）
      $25K未満・週3回超 → 完全ブロック（PDT枠使い切り）
      $25K未満・枠残あり・緊急（|Delta|>0.5） → 発動（PDT枠消費）
      $25K未満・枠残あり・非緊急 → スキップ（保守的）

    Args:
        position_delta_abs: ポートフォリオの|total_delta|
        cash_usd: 現在の口座残高（USD）
        weekly_hedge_count: 今週のDeltaHedgeでのPDT消費回数
        is_emergency: 緊急度フラグ（外部から注入可・通常はposition_delta_absで判定）

    Returns:
        (allowed: bool, reason: str)
    """
    # $25K以上: PDT対象外 → 制限なし発動
    if cash_usd >= 25000:
        return True, "cash>=$25K: 制限なし"

    # $25K未満: 週次PDT枠チェック
    if weekly_hedge_count >= DELTA_HEDGE_WEEKLY_BUDGET:
        return False, (
            f"PDT枠使い切り: 今週{weekly_hedge_count}/{DELTA_HEDGE_WEEKLY_BUDGET}回消費済み"
        )

    # 緊急判定: |Delta| > 緊急閾値 または 外部フラグ
    effective_emergency = is_emergency or (position_delta_abs > DELTA_HEDGE_EMERGENCY_THRESHOLD)

    if effective_emergency:
        remaining = DELTA_HEDGE_WEEKLY_BUDGET - weekly_hedge_count
        return True, (
            f"緊急ヘッジ発動: |Delta|={position_delta_abs:.3f}>{DELTA_HEDGE_EMERGENCY_THRESHOLD}"
            f" / PDT残{remaining}回"
        )

    # 非緊急・$25K未満 → 保守的にスキップ
    remaining = DELTA_HEDGE_WEEKLY_BUDGET - weekly_hedge_count
    return False, (
        f"非緊急スキップ: |Delta|={position_delta_abs:.3f}<={DELTA_HEDGE_EMERGENCY_THRESHOLD}"
        f" (cash<$25K, PDT残{remaining}回は温存)"
    )


def calc_dd_peak_ratio(pnl_file: Optional[Path] = None) -> dict:
    """ピーク比ドローダウンを計算する（G-NEW6）。

    condor_pnl.jsonのexitイベントのpnl_usdを累積してピーク・DD・現在値を返す。

    Returns:
        {
          "peak_cumulative": float,   # 累積利益の過去最高
          "current_cumulative": float, # 現在の累積利益
          "drawdown_usd": float,      # ピークからの下落額（負の値）
          "drawdown_pct": float,      # ピークからの下落率（負の値）
          "trade_count": int,
        }
    """
    pnl_file = pnl_file or PNL_FILE
    result = {
        "peak_cumulative": 0.0, "current_cumulative": 0.0,
        "drawdown_usd": 0.0, "drawdown_pct": 0.0, "trade_count": 0,
    }
    try:
        trades = load_pnl()
        exits = [t for t in trades if t.get("event") == "exit" and t.get("pnl_usd") is not None]
        if not exits:
            return result
        cumulative = 0.0
        peak = 0.0
        for t in exits:
            cumulative += float(t["pnl_usd"])
            if cumulative > peak:
                peak = cumulative
        dd_usd = cumulative - peak
        dd_pct = (dd_usd / peak * 100) if peak > 0 else 0.0
        result.update({
            "peak_cumulative": round(peak, 2),
            "current_cumulative": round(cumulative, 2),
            "drawdown_usd": round(dd_usd, 2),
            "drawdown_pct": round(dd_pct, 2),
            "trade_count": len(exits),
        })
        log.info(
            f"[DD] peak=${peak:.0f} current=${cumulative:.0f} "
            f"dd=${dd_usd:.0f} ({dd_pct:.1f}%)"
        )
    except Exception as e:
        log.warning(f"calc_dd_peak_ratio: {e}")
    return result


def calc_category_stats(pnl_file: Optional[Path] = None) -> dict:
    """カテゴリ別成績分析（G-NEW5）。

    VIX帯別・曜日別・戦術別でP&Lを集計する。

    Returns:
        {
          "by_vix_band": {"low(<15)": {...}, "mid(15-25)": {...}, "high(25+)": {...}},
          "by_weekday": {"Monday": {...}, ...},
          "by_strategy": {"CS": {...}, "IC": {...}, ...},
        }
        各カテゴリ内: {"trades": int, "wins": int, "losses": int, "total_pnl": float, "win_rate": float}
    """
    pnl_file = pnl_file or PNL_FILE

    def _empty_bucket():
        return {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "win_rate": 0.0}

    def _update_bucket(bucket: dict, pnl: float):
        bucket["trades"] += 1
        bucket["total_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["win_rate"] = round(bucket["wins"] / bucket["trades"] * 100, 1) if bucket["trades"] > 0 else 0.0
        bucket["total_pnl"] = round(bucket["total_pnl"], 2)

    vix_bands = {"low(<15)": _empty_bucket(), "mid(15-25)": _empty_bucket(), "high(25+)": _empty_bucket()}
    weekdays  = {d: _empty_bucket() for d in ["Monday","Tuesday","Wednesday","Thursday","Friday"]}
    strategies = {}

    try:
        trades = load_pnl()
        exits  = [t for t in trades if t.get("event") == "exit" and t.get("pnl_usd") is not None]

        for t in exits:
            pnl = float(t["pnl_usd"])
            # VIX帯別
            vix = t.get("vix") or 0
            if vix < 15:
                _update_bucket(vix_bands["low(<15)"], pnl)
            elif vix < 25:
                _update_bucket(vix_bands["mid(15-25)"], pnl)
            else:
                _update_bucket(vix_bands["high(25+)"], pnl)
            # 曜日別
            try:
                dt = datetime.date.fromisoformat(t.get("date", "2000-01-01"))
                wd = dt.strftime("%A")
                if wd not in weekdays:
                    weekdays[wd] = _empty_bucket()
                _update_bucket(weekdays[wd], pnl)
            except Exception:
                pass
            # 戦術別
            strat = t.get("strategy") or t.get("tactic") or "CS"
            if strat not in strategies:
                strategies[strat] = _empty_bucket()
            _update_bucket(strategies[strat], pnl)
    except Exception as e:
        log.warning(f"calc_category_stats: {e}")

    return {
        "by_vix_band": vix_bands,
        "by_weekday": weekdays,
        "by_strategy": strategies,
    }


def _gen_daily_trade_journal(session_date: str, session_records: list) -> None:
    """日次トレードジャーナルを data/journal/trade_journal_YYYYMMDD.json に保存する（N4）。

    各トレードの「なぜエントリーしたか」「環境スコア」「strategy_selectorの判定理由」を記録し、
    エグジット後に「仮説は正しかったか」を自動評価する。

    Args:
        session_date: "YYYY-MM-DD" 形式の日付文字列
        session_records: その日のcondor_pnl.jsonレコードリスト（entry/exit/env_snapshot）
    """
    try:
        journal_dir = _BASE_DIR / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        journal_path = journal_dir / f"trade_journal_{session_date.replace('-','')}.json"

        entries       = {t.get("trade_id"): t for t in session_records if t.get("event") == "entry" and t.get("trade_id")}
        exits         = {t.get("trade_id"): t for t in session_records if t.get("event") == "exit"  and t.get("trade_id")}
        env_snapshots = [t for t in session_records if t.get("event") == "env_snapshot"]

        trades_journal = []
        # entry/exitが紐付いているトレードを結合
        all_trade_ids = set(entries.keys()) | set(exits.keys())
        for tid in all_trade_ids:
            entry = entries.get(tid, {})
            exit_ = exits.get(tid, {})
            pnl_usd = float(exit_.get("pnl_usd", 0) or 0)
            net_credit = float(entry.get("net_credit", 0) or 0)

            # 仮説の正否: 利益が出た = 環境評価が正しかった
            hypothesis_correct = None
            if exit_:
                hypothesis_correct = pnl_usd > 0

            # エントリー理由の自動生成（環境スコア・戦術・VIX・方向から生成）
            entry_reason_parts = []
            vix_at_entry = entry.get("vix")
            if vix_at_entry:
                entry_reason_parts.append(f"VIX={vix_at_entry:.1f}")
            strategy = entry.get("strategy") or entry.get("tactic") or "CS"
            entry_reason_parts.append(f"戦術={strategy}")
            direction = entry.get("direction")
            if direction:
                entry_reason_parts.append(f"方向={direction}")
            delta_actual = entry.get("delta_actual")
            if delta_actual:
                entry_reason_parts.append(f"delta={delta_actual:.3f}")

            # env_snapshotから追加情報を取得
            env = next((s for s in env_snapshots
                        if s.get("strategy") == strategy or s.get("direction") == direction), {})
            env_score = env.get("env_score") or entry.get("env_score")
            if env_score:
                entry_reason_parts.append(f"env_score={env_score:.0f}")
            regime = env.get("regime")
            if regime:
                entry_reason_parts.append(f"regime={regime}")

            record = {
                "trade_id":           tid,
                "date":               session_date,
                "entry_time":         entry.get("ts"),
                "exit_time":          exit_.get("ts"),
                "strategy":           strategy,
                "direction":          direction,
                "sell_strike":        entry.get("sell_strike"),
                "buy_strike":         entry.get("buy_strike"),
                "net_credit":         net_credit,
                "pnl_usd":            pnl_usd if exit_ else None,
                "exit_reason":        exit_.get("reason"),
                "hypothesis_correct": hypothesis_correct,
                "entry_reason":       " / ".join(entry_reason_parts),
                "env_score":          env_score,
                "vix_at_entry":       vix_at_entry,
                "regime_at_entry":    regime,
                "slippage":           entry.get("slippage"),
                "qty":                entry.get("qty"),
            }
            trades_journal.append(record)

        # env_snapshotから当日のマーケット環境サマリーを生成
        market_context = {}
        if env_snapshots:
            latest_env = env_snapshots[-1]
            market_context = {
                "vix":        latest_env.get("vix"),
                "vrp":        latest_env.get("vrp"),
                "ivr":        latest_env.get("ivr"),
                "sma20":      latest_env.get("sma20"),
                "env_score":  latest_env.get("env_score"),
                "regime":     latest_env.get("regime"),
            }

        journal = {
            "date":           session_date,
            "generated_at":   datetime.datetime.now(ET).isoformat(),
            "trade_count":    len(trades_journal),
            "session_pnl":    round(sum(t["pnl_usd"] or 0 for t in trades_journal), 2),
            "win_count":      sum(1 for t in trades_journal if t.get("hypothesis_correct") is True),
            "market_context": market_context,
            "trades":         trades_journal,
        }
        journal_path.write_text(json.dumps(journal, indent=2, ensure_ascii=False))
        log.info(f"[Journal] 日次ジャーナル保存: {journal_path}")
    except Exception as e:
        log.warning(f"_gen_daily_trade_journal: {e}")


def check_consecutive_losses() -> bool:
    trades = load_pnl()
    exits = [t for t in trades if t.get("event") == "exit"]
    recent = exits[-MAX_CONSECUTIVE_LOSSES:]
    if len(recent) < MAX_CONSECUTIVE_LOSSES:
        return False
    if all((t.get("pnl_usd", 0) or 0) < 0 for t in recent):
        pushover("SPY Credit Spread", f"連続{MAX_CONSECUTIVE_LOSSES}敗 → 本日停止", priority=1)
        return True
    return False


def check_memory_usage():
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        if usage > MEMORY_WARN_PCT:
            data = {}
            if MEMORY_WARN_FILE.exists():
                data = json.loads(MEMORY_WARN_FILE.read_text())
            data["count"] = data.get("count", 0) + 1
            data["max_pct"] = max(float(data.get("max_pct", 0)), usage)
            data["last"] = datetime.datetime.now(ET).isoformat()
            MEMORY_WARN_FILE.write_text(json.dumps(data))
    except Exception:
        pass


# ── VIX spike cache (persist across sessions) ─────────────────────────────────
def save_vix_spike_data(vix: float, spike_for_tomorrow: bool):
    """Save today's VIX and whether tomorrow is a recovery day."""
    try:
        VIX_SPIKE_FILE.parent.mkdir(parents=True, exist_ok=True)
        VIX_SPIKE_FILE.write_text(json.dumps({
            "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
            "vix":  vix,
            "spike_for_tomorrow": spike_for_tomorrow,
        }))
    except Exception as e:
        log.warning(f"vix spike save: {e}")


def is_recovery_day() -> bool:
    """True if yesterday had VIX spike >= dynamic threshold (ATR-based) → today is likely a bounce day."""
    try:
        if not VIX_SPIKE_FILE.exists():
            return False
        data = json.loads(VIX_SPIKE_FILE.read_text())
        cache_date = datetime.date.fromisoformat(data["date"])
        today = datetime.datetime.now(ET).date()
        # Accept cache from up to 4 calendar days ago (covers weekends)
        if (today - cache_date).days > 4:
            return False
        return bool(data.get("spike_for_tomorrow", False))
    except Exception:
        return False


def get_yesterday_vix() -> Optional[float]:
    try:
        if VIX_SPIKE_FILE.exists():
            data = json.loads(VIX_SPIKE_FILE.read_text())
            return float(data.get("vix", 0)) or None
    except Exception:
        pass
    return None


# ── IVR (IV Rank) cache ───────────────────────────────────────────────────────
# Cache format (v2 with history):
#   {
#     "latest": {"date": "YYYY-MM-DD", "ivr": float},
#     "history": [{"date": "YYYY-MM-DD", "ivr": float}, ...]   # max 252 entries
#   }
# Legacy format (v1, date/ivr at top-level) is read-compatible.

def load_ivr_cache() -> Optional[float]:
    """Return today's (or yesterday's) cached IVR value, or None if stale/missing."""
    try:
        if IVR_CACHE_FILE.exists():
            data = json.loads(IVR_CACHE_FILE.read_text())
            # v2 format
            if "latest" in data:
                entry = data["latest"]
            else:
                entry = data  # v1 legacy
            cache_date = datetime.date.fromisoformat(entry["date"])
            if (datetime.datetime.now(ET).date() - cache_date).days <= 1:
                return float(entry["ivr"])
    except Exception:
        pass
    return None


def load_ivr_history() -> list:
    """Return list of IVR float values from the history store (newest last).
    Used by get_ivr_percentiles() to compute dynamic thresholds.
    """
    try:
        if IVR_CACHE_FILE.exists():
            data = json.loads(IVR_CACHE_FILE.read_text())
            if "history" in data:
                return [float(e["ivr"]) for e in data["history"] if "ivr" in e]
            # v1 legacy: single entry
            if "ivr" in data:
                return [float(data["ivr"])]
    except Exception:
        pass
    return []


def save_ivr_cache(ivr: float):
    """Save IVR to cache, appending to rolling 252-day history."""
    try:
        IVR_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        new_entry = {"date": today_str, "ivr": ivr}

        # Load existing data to preserve history
        existing: dict = {}
        if IVR_CACHE_FILE.exists():
            try:
                existing = json.loads(IVR_CACHE_FILE.read_text())
            except Exception:
                existing = {}

        # Migrate v1 to v2
        if "history" not in existing:
            old_history: list = []
            if "latest" in existing and "ivr" in existing["latest"]:
                old_history = [existing["latest"]]
            elif "ivr" in existing:
                old_history = [{"date": existing.get("date", today_str),
                                "ivr": existing["ivr"]}]
            existing = {"latest": new_entry, "history": old_history}

        # Append to history (avoid duplicate for same date)
        history: list = existing.get("history", [])
        if not history or history[-1].get("date") != today_str:
            history.append(new_entry)
        else:
            history[-1] = new_entry  # overwrite same-day entry
        # Keep max 252 entries
        if len(history) > 252:
            history = history[-252:]

        IVR_CACHE_FILE.write_text(json.dumps({
            "latest": new_entry,
            "history": history,
        }))
    except Exception as e:
        log.warning(f"ivr cache save: {e}")


# ── ThetaData IVR ──────────────────────────────────────────────────────────────

def _thetadata_greeks_dir(date_str: str) -> Path:
    """data/thetadata/YYYYMMDD/ ディレクトリパスを返す。"""
    return _BASE_DIR / "thetadata" / date_str.replace("-", "")


def calc_ivr_from_thetadata(symbol: str = "SPY", lookback_days: int = 20) -> Optional[float]:
    """ThetaData greeks_first_order parquetの実績IVを使ってIVRを算出する。

    Algorithm:
        1. 直近 lookback_days 日分の greeks_first_order_{symbol}.parquet を読む
        2. 各日のATM implied_vol（underlying_price近傍のstrike）中央値をday_ivとする
        3. 過去の day_iv 履歴に対する今日の day_iv のパーセンタイル順位を返す (0-100)

    フォールバック条件（NoneをReturnし呼び出し元が既存方式へ切替）:
        - USE_THETADATA_IVR が False
        - parquetファイルが 5 日未満しか存在しない
        - pandas/pyarrow が importできない
    """
    if not USE_THETADATA_IVR:
        return None

    try:
        import pandas as pd  # type: ignore
    except ImportError:
        log.debug("calc_ivr_from_thetadata: pandas unavailable → fallback")
        return None

    thetadata_root = _BASE_DIR / "thetadata"
    if not thetadata_root.exists():
        return None

    # 利用可能な日付ディレクトリを降順で列挙
    available_dates = sorted(
        [d for d in thetadata_root.iterdir()
         if d.is_dir() and d.name.isdigit() and len(d.name) == 8],
        key=lambda d: d.name,
        reverse=True,
    )

    if len(available_dates) < 5:
        log.debug(f"calc_ivr_from_thetadata: only {len(available_dates)} days → fallback")
        return None

    target_file = f"greeks_first_order_{symbol}.parquet"
    day_ivs: list = []

    # ファイルが存在しないディレクトリが多数ある場合も考慮し全ディレクトリをスキャン
    # (最大 lookback_days 件のIV値が集まったら停止)
    for date_dir in available_dates:
        parquet_path = date_dir / target_file
        if not parquet_path.exists():
            continue
        try:
            df = pd.read_parquet(parquet_path, columns=["strike", "implied_vol", "underlying_price"])
            if df.empty:
                continue
            # ATM: underlying_price と strike の差が最小のものを使う
            underlying = df["underlying_price"].median()
            df["dist"] = (df["strike"] - underlying).abs()
            atm_rows = df.nsmallest(10, "dist")
            iv_median = atm_rows["implied_vol"].median()
            if iv_median > 0:
                day_ivs.append(float(iv_median))
        except Exception as _e:
            log.debug(f"calc_ivr_from_thetadata: {date_dir.name} skip: {_e}")
            continue

        if len(day_ivs) >= lookback_days:
            break

    if len(day_ivs) < 5:
        log.debug(f"calc_ivr_from_thetadata: insufficient IV days ({len(day_ivs)}) → fallback")
        return None

    # 最新の day_iv (day_ivs[0] = most recent date) のパーセンタイル順位
    current_iv = day_ivs[0]
    history_ivs = day_ivs[1:]  # 直近を除いた過去
    if not history_ivs:
        return None

    sorted_hist = sorted(history_ivs)
    rank = sum(1 for v in sorted_hist if v <= current_iv) / len(sorted_hist) * 100
    ivr = round(rank, 1)
    log.info(
        f"[ThetaData IVR] {symbol} IV={current_iv:.4f} "
        f"rank={ivr:.1f} (n={len(history_ivs)} days)"
    )
    return ivr


# ── Portfolio Vega管理 ─────────────────────────────────────────────────────────

def calc_portfolio_vega(positions: list, quote_ctx=None) -> dict:
    """全保有ポジションのVegaを合算してポートフォリオVegaを返す。

    Args:
        positions: eng.get_open_positions() から取得したポジションリスト
        quote_ctx: futu QuoteContext（スナップショット取得に使用）

    Returns:
        {
            "total_vega": float,     # ポートフォリオVega合計 (ドル/IV 1%変化)
            "position_count": int,
            "warning": bool,         # VEGA_WARN_THRESHOLD 超過
            "details": list          # 各ポジションの vega 詳細
        }
    """
    if not positions:
        return {"total_vega": 0.0, "position_count": 0, "warning": False, "details": []}

    total_vega = 0.0
    details: list = []

    # futu スナップショットから vega を取得する
    if FUTU_AVAILABLE and quote_ctx is not None:
        try:
            codes = [p.get("code", "") for p in positions if p.get("code")]
            if codes:
                ret, snap = quote_ctx.get_market_snapshot(codes)
                if ret == RET_OK and not snap.empty:
                    snap_dict = {
                        row["code"]: row
                        for _, row in snap.iterrows()
                    }
                    for pos in positions:
                        code = pos.get("code", "")
                        qty  = abs(float(pos.get("qty", 1)))
                        row  = snap_dict.get(code, {})
                        vega_per_contract = float(
                            row.get("option_vega", 0) or row.get("vega", 0) or 0
                        )
                        # vega はIV1%変化での1契約×100株 価値変化
                        pos_vega = vega_per_contract * qty * 100
                        total_vega += pos_vega
                        details.append({"code": code, "vega": round(pos_vega, 2), "qty": qty})
        except Exception as _vg_e:
            log.debug(f"calc_portfolio_vega futu: {_vg_e}")

    warning = abs(total_vega) > VEGA_WARN_THRESHOLD
    return {
        "total_vega": round(total_vega, 2),
        "position_count": len(positions),
        "warning": warning,
        "details": details,
    }


def calc_vega_size_factor(vix_current: float, vix_prev: float) -> float:
    """VIX急騰時のVegaベースサイズ縮小係数を計算する。

    VIX前日比が VEGA_VIX_SPIKE_PCT (+20%) を超えた場合に VEGA_REDUCE_FACTOR を返す。
    それ以外は 1.0（縮小なし）。

    Args:
        vix_current: 現在のVIX
        vix_prev: 前日のVIX（0以下の場合は縮小しない）

    Returns:
        float: 1.0（変更なし）または VEGA_REDUCE_FACTOR（縮小）
    """
    if vix_prev <= 0:
        return 1.0
    change_pct = (vix_current - vix_prev) / vix_prev
    if change_pct > VEGA_VIX_SPIKE_PCT:
        log.info(
            f"[VegaSize] VIX急騰 {vix_prev:.1f}→{vix_current:.1f} "
            f"(+{change_pct*100:.1f}% > +{VEGA_VIX_SPIKE_PCT*100:.0f}%) "
            f"→ サイズ係数 {VEGA_REDUCE_FACTOR}"
        )
        return VEGA_REDUCE_FACTOR
    return 1.0


# ── VIX params selector ────────────────────────────────────────────────────────
def get_params(vix: float, params_table: dict) -> Optional[dict]:
    """Return params for given VIX from a params table. None = no trade."""
    for vix_max in sorted(params_table.keys()):
        if vix < vix_max:
            return params_table[vix_max]
    return None


def apply_ivr_delta(
    params: dict,
    ivr: Optional[float],
    ivr_thresholds: Optional[tuple] = None,
) -> dict:
    """Apply IVR delta adjustment to a copy of params. Returns new dict.

    Args:
        params: strategy parameter dict (will not be mutated).
        ivr: current IV Rank (0-100), or None to skip adjustment.
        ivr_thresholds: (ivr_low, ivr_high) dynamic thresholds from
            MarketData.get_ivr_percentiles().  Falls back to the
            module-level IVR_LOW / IVR_HIGH constants when None or when
            history is insufficient (get_ivr_percentiles returns the
            fallback values itself).
    """
    if ivr is None:
        return params
    if ivr_thresholds is not None:
        ivr_low, ivr_high = ivr_thresholds
    else:
        ivr_low, ivr_high = float(IVR_LOW), float(IVR_HIGH)
    params = dict(params)
    if ivr > ivr_high:
        params["delta"] = round(min(params["delta"] + 0.05, 0.40), 2)
        log.info(f"IVR={ivr:.1f} > {ivr_high:.1f} (P75) → delta boosted to {params['delta']}")
    elif ivr < ivr_low:
        params["delta"] = round(max(params["delta"] - 0.05, 0.10), 2)
        log.info(f"IVR={ivr:.1f} < {ivr_low:.1f} (P25) → delta reduced to {params['delta']}")
    return params


def apply_recovery_delta(params: dict) -> dict:
    """Boost delta on recovery day (day after VIX spike)."""
    if not is_recovery_day():
        return params
    params = dict(params)
    params["delta"] = round(min(params["delta"] + 0.05, 0.40), 2)
    log.info(f"Recovery day (VIX spike yesterday) → delta boosted to {params['delta']}")
    return params


def apply_delayed_vix_delta(params: dict, vix_is_fallback: bool) -> dict:
    """廃止関数（後方互換のためシグネチャ維持）。
    Yahoo Finance VIXはexchangeDataDelayedBy=0（遅延ゼロ）が確認済みのため
    delta調整は不要。呼び出し元ごと削除済み。この関数はno-op。
    """
    return params


def calc_qty(cash: float, params: dict, paper: bool = False) -> int:
    """クレジットスプレッドのqty（枚数）を算出する。

    G-NEW9: 資金フェーズ自動移行対応。
    口座残高からフェーズを判定してmax_qtyを動的に設定する。
    paperモードではMAX_QTY_PAPERを優先する。
    """
    margin = params["width"] * 100
    max_by_capital = int(cash * params["capital_pct"] / margin)
    if cash < SMALL_ACCOUNT_USD:
        return max(1, min(max_by_capital, 1))
    if paper:
        limit = MAX_QTY_PAPER
    else:
        # G-NEW9: 資金フェーズに応じたmax_qty
        # JPY建て口座のため USD概算変換（150円/ドル）でフェーズ判定
        cash_usd = cash / 150.0
        phase_cfg = get_capital_phase(cash_usd)
        limit = phase_cfg["max_qty"]
    return max(1, min(max_by_capital, limit))


def calc_qty_mass_verify(
    cash: float,
    params: dict,
    n_combinations: int,
    paper: bool = True,
) -> int:
    """MassVerifyモード用のqty計算。

    P0-2: 証拠金配分の動的制御。
    総証拠金を組み合わせ数（銘柄×戦術）で割り、1組み合わせあたりの上限を算出。
    固定ハードコードなし — cash・n_combinations・paramsから動的に算出する。

    Args:
        cash: 口座残高（JPY建て）
        params: 戦術パラメータ（width/capital_pct等）
        n_combinations: 現在のactive_symbols数（銘柄×戦術の組み合わせ総数）
        paper: ペーパーモードフラグ

    Returns:
        1組み合わせあたりの最大枚数（最低1枚保証）
    """
    if n_combinations <= 0:
        return calc_qty(cash, params, paper=paper)

    # 1組み合わせあたりに割り当てられる証拠金上限（総資金を均等分割）
    # 固定パラメータ原則違反を避けるため capital_pct も活用する
    # cashはJPY建て・margin_per_contractはUSD建てのため換算必須（150円/ドル）
    cash_usd = cash / 150.0
    per_slot_cash = (cash_usd * params.get("capital_pct", 0.02)) / n_combinations
    margin_per_contract = params.get("width", 5) * 100  # 1枚あたりのスプレッド証拠金（USD）
    if margin_per_contract <= 0:
        return 1
    qty_by_slot = int(per_slot_cash / margin_per_contract)

    # 通常のcalc_qty上限（フェーズ制限）も適用して保守的な値を採る
    base_qty = calc_qty(cash, params, paper=paper)
    result = max(1, min(qty_by_slot, base_qty))
    return result


# ── No-trade day checks ────────────────────────────────────────────────────────
def fetch_events_weekly():
    now = datetime.datetime.now(ET)
    if now.weekday() != 0:
        return
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        resp = requests.get(url, timeout=10)
        events_raw = resp.json()
        events = []
        for ev in events_raw:
            title = ev.get("title", "").lower()
            country = ev.get("country", "").lower()
            impact = ev.get("impact", "").lower()
            if country == "usd" and impact in ("high", "medium"):
                if any(kw in title for kw in NOTRADE_KEYWORDS):
                    events.append({"date": ev.get("date", ""), "keyword": title})
        EVENTS_FILE.write_text(json.dumps({"fetched": now.date().isoformat(), "events": events}))
        log.info(f"Events fetched: {len(events)} high-impact USD events")
    except Exception as e:
        log.warning(f"fetch_events: {e}")


def is_notrade_today() -> bool:
    today    = datetime.datetime.now(ET).date()
    tomorrow = today + datetime.timedelta(days=1)
    # Holiday eve
    if tomorrow in US_HOLIDAYS:
        return True
    # Quarterly OpEx (3rd Friday of March/June/Sep/Dec)
    if today.month in (3, 6, 9, 12) and today.weekday() == 4:
        first_day    = today.replace(day=1)
        first_friday = first_day + datetime.timedelta(days=(4 - first_day.weekday()) % 7)
        if today == first_friday + datetime.timedelta(weeks=2):
            return True
    # Economic events (FOMC, CPI, NFP etc.)
    if EVENTS_FILE.exists():
        try:
            data = json.loads(EVENTS_FILE.read_text())
            if (today - datetime.date.fromisoformat(data["fetched"])).days <= 7:
                today_str = today.isoformat()
                for ev in data.get("events", []):
                    if ev.get("date", "").startswith(today_str):
                        log.info(f"No-trade: {ev['keyword']}")
                        return True
        except Exception:
            pass
    return False


# ══════════════════════════════════════════════════════════════════════════════
# PriceCache — リアルタイムプッシュデータのインメモリキャッシュ
# ══════════════════════════════════════════════════════════════════════════════
class PriceCache:
    """futu subscribe APIのプッシュデータをキャッシュする。
    SpyQuoteHandlerがon_recv_rspでupdate()を呼び、
    MarketDataのget_vix()/get_spy_snapshot()がget()で参照する。
    """
    def __init__(self):
        self._prices: dict = {}      # {"US.SPY": 580.12, "US.VIX": 18.5}
        self._timestamps: dict = {}  # {"US.SPY": 1700000000.0, ...}

    def update(self, code: str, price: float):
        self._prices[code] = price
        self._timestamps[code] = time.time()

    def get(self, code: str, max_age_sec: float = 5.0) -> Optional[float]:
        if code not in self._prices:
            return None
        if time.time() - self._timestamps[code] > max_age_sec:
            return None
        return self._prices[code]

    def update_open(self, code: str, price: float):
        key = f"{code}_open"
        self._prices[key] = price
        self._timestamps[key] = time.time()

    def get_open(self, code: str, max_age_sec: float = 5.0) -> Optional[float]:
        key = f"{code}_open"
        if key not in self._prices:
            return None
        if time.time() - self._timestamps.get(key, 0) > max_age_sec:
            return None
        return self._prices[key]


# ══════════════════════════════════════════════════════════════════════════════
# SpyQuoteHandler / OptionQuoteHandler — futuプッシュコールバック → PriceCacheへ書き込む
# ══════════════════════════════════════════════════════════════════════════════
_QuoteHandlerBase = StockQuoteHandlerBase if FUTU_AVAILABLE else object


class SpyQuoteHandler(_QuoteHandlerBase):
    """futu subscribe のコールバック。
    受信したQUOTEデータをPriceCacheに書き込む。
    別スレッドから呼ばれるためPriceCacheはスレッドセーフな読み書きのみ使用。
    """
    def __init__(self, cache: PriceCache):
        if FUTU_AVAILABLE:
            super().__init__()
        self._cache = cache

    def on_recv_rsp(self, rsp_pb):
        ret_code, data = super().on_recv_rsp(rsp_pb)
        if ret_code == RET_OK and data is not None and not data.empty:
            for i in range(len(data)):
                row = data.iloc[i]
                code = row["code"]
                last_p = row.get("last_price", 0)
                open_p = row.get("open_price", 0)
                if last_p and float(last_p) > 0:
                    self._cache.update(code, float(last_p))
                if open_p and float(open_p) > 0:
                    self._cache.update_open(code, float(open_p))
        return ret_code, data


class OptionQuoteHandler(_QuoteHandlerBase):
    """保有中のUS Optionsレッグのリアルタイム価格をPriceCacheに書き込む。
    StockQuoteHandlerBaseはUS Optionsコードにも対応しており、
    SubType.QUOTEでlast_priceが返る（公式futu SDK仕様）。
    別スレッドから呼ばれるためPriceCacheはスレッドセーフな読み書きのみ使用。
    """
    def __init__(self, cache: PriceCache):
        if FUTU_AVAILABLE:
            super().__init__()
        self._cache = cache

    def on_recv_rsp(self, rsp_pb):
        ret_code, data = super().on_recv_rsp(rsp_pb)
        if ret_code == RET_OK and data is not None and not data.empty:
            for i in range(len(data)):
                row = data.iloc[i]
                code = row["code"]
                last_p = row.get("last_price", 0)
                if last_p and float(last_p) > 0:
                    self._cache.update(code, float(last_p))
                    log.debug(f"[OptionQuoteHandler] {code} last_price={float(last_p):.4f}")
        return ret_code, data


class ATMOptionQuoteHandler(_QuoteHandlerBase):
    """ATMオプションのリアルタイムbid/askからIVを計算してPriceCacheに"VIX_ATM_IV"を書き込む。

    bid=0かつask=0の場合は場外とみなしスキップ（キャッシュを汚染しない）。
    IV計算はBlack-Scholes mid-priceから逆算（brentq）。
    計算コストが高いためsimplified approx.を先に試みる。
    """
    def __init__(self, cache: PriceCache, spy_price: float):
        if FUTU_AVAILABLE:
            super().__init__()
        self._cache = cache
        self._spy_price = spy_price
        self._iv_samples: list = []  # 複数コードのIVを蓄積して平均化

    def on_recv_rsp(self, rsp_pb):
        ret_code, data = super().on_recv_rsp(rsp_pb)
        if ret_code != RET_OK or data is None or data.empty:
            return ret_code, data

        now_et = datetime.datetime.now(ET)
        close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        t_sec = (close_et - now_et).total_seconds()
        if t_sec <= 60:
            return ret_code, data  # 市場終了直前はスキップ
        T = max(t_sec / (365 * 24 * 3600), 1e-6)

        for i in range(len(data)):
            row = data.iloc[i]
            # option_implied_volatility フィールドが直接取れる場合はそれを使う
            iv_direct = float(row.get("option_implied_volatility", 0) or 0)
            if iv_direct > 0.01:
                self._iv_samples.append(iv_direct)
                continue

            bid = float(row.get("bid_price", 0) or 0)
            ask = float(row.get("ask_price", 0) or 0)
            if bid <= 0 and ask <= 0:
                continue  # 場外または流動性なし → スキップ
            mid = (bid + ask) / 2.0
            if mid <= 0:
                continue

            # strike_price フィールドを取得（オプションスナップでは通常含まれる）
            strike = float(row.get("strike_price", 0) or 0)
            if strike <= 0:
                # コードからストライクを推定（例: US.SPY260415C540000 → 540.0）
                code = str(row.get("code", ""))
                m = re.search(r"[CP](\d+)$", code)
                if m:
                    strike = float(m.group(1)) / 1000.0
                if strike <= 0:
                    continue

            # Black-Scholes で mid-price から IV を逆算
            try:
                from scipy.optimize import brentq as _brentq_atm
                is_call = "C" in str(row.get("code", ""))
                S = self._spy_price
                K = strike

                def _bs_mid(sigma: float) -> float:
                    if sigma <= 0:
                        return 0.0
                    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
                    d2 = d1 - sigma * math.sqrt(T)
                    from math import erf
                    def _ncdf(x):
                        return 0.5 * (1.0 + erf(x / math.sqrt(2)))
                    if is_call:
                        return S * _ncdf(d1) - K * _ncdf(d2)
                    else:
                        return K * _ncdf(-d2) - S * _ncdf(-d1)

                iv_bs = _brentq_atm(lambda s: _bs_mid(s) - mid, 1e-4, 10.0, xtol=1e-4, maxiter=50)
                if 0.01 < iv_bs < 5.0:
                    self._iv_samples.append(iv_bs)
            except Exception:
                pass

        # 蓄積したIVサンプルを平均化してPriceCacheに書き込む
        if self._iv_samples:
            avg_iv = sum(self._iv_samples) / len(self._iv_samples)
            vix_approx = avg_iv * 100.0
            self._cache.update("VIX_ATM_IV", vix_approx)
            log.debug(f"[ATMSubscribe] VIX_ATM_IV={vix_approx:.2f} (samples={len(self._iv_samples)})")
            self._iv_samples.clear()

        return ret_code, data


# ══════════════════════════════════════════════════════════════════════════════
# MarketData — quote context wrapper
# ══════════════════════════════════════════════════════════════════════════════
class MarketData:
    def __init__(self, underlying_code: str = UNDERLYING_CODE):
        self.underlying_code: str = underlying_code  # 動的銘柄選択: デフォルトUS.SPY
        self.quote_ctx = None
        self._vix_is_fallback: bool = False  # True when VIX came from Yahoo Finance (futu未対応)
        self._spy_is_fallback: bool = False  # True when SPY came from Finnhub (futu権限なし)
        self._finnhub_cache: dict = {}  # {"SPY": {"data": dict, "ts": datetime}} 5分キャッシュ
        self._price_cache: PriceCache = PriceCache()  # subscribeプッシュデータキャッシュ
        self._subscribe_ok: bool = False  # subscribe成功フラグ
        self._subscribed_option_codes: set = set()  # 現在subscribeしているオプションコード
        # ATM IV subscribe 管理
        self._atm_subscribed_codes: set = set()  # 現在ATM subscribeしているオプションコード
        self._last_atm_spy_price: Optional[float] = None  # 前回ATM subscribe時のSPY価格
        self._atm_resubscribe_threshold: float = 2.0  # SPYが$2以上動いたら再subscribe

    def connect(self) -> bool:
        if not FUTU_AVAILABLE:
            return False
        try:
            self.quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
            self._setup_subscribe()
            return True
        except Exception as e:
            log.error(f"QuoteContext connect failed: {e}")
            return False

    def _setup_subscribe(self):
        """SPY/VIXのリアルタイムプッシュをsubscribeする。
        失敗してもBotは正常動作（既存のpollingにフォールバック）。
        US.SPYはNasdaq Basic未購入のため権限エラーになる可能性あり（INFO扱い・障害ではない）。
        US.VIXはfutu APIがIndices非対応のためINFO扱い。
        オプションレッグのsubscribeはsubscribe_option_legs()で別途行う。
        """
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return
        try:
            handler = SpyQuoteHandler(self._price_cache)
            self.quote_ctx.set_handler(handler)
            # US.SPYのsubscribeを試行（Nasdaq Basic未購入の場合は権限エラー・INFO扱い）
            spy_codes = [self.underlying_code]
            ret, msg = self.quote_ctx.subscribe(spy_codes, [SubType.QUOTE],
                                                subscribe_push=True, is_first_push=True)
            if ret == RET_OK:
                self._subscribe_ok = True
                log.info(f"subscribe OK: {spy_codes} → リアルタイムプッシュ有効")
            else:
                log.info(f"subscribe非対応 ({spy_codes}): {msg} → pollingにフォールバック")
            # US.VIXのsubscribeを別途試行（Indices非対応のためINFO扱い）
            ret_vix, msg_vix = self.quote_ctx.subscribe(["US.VIX"], [SubType.QUOTE],
                                                        subscribe_push=True, is_first_push=True)
            if ret_vix == RET_OK:
                log.info("subscribe OK: US.VIX → リアルタイムプッシュ有効")
            else:
                log.info(f"US.VIX subscribe非対応 ({msg_vix}) → Yahoo Financeにフォールバック")
        except Exception as e:
            log.warning(f"_setup_subscribe例外: {e} → pollingにフォールバック")

    def update_atm_subscribe(self, spy_price: float) -> None:
        """ATM付近のオプション（Call/Put各3本=計6コントラクト）をsubscribeし、
        bid/askからIVを計算してPriceCacheに"VIX_ATM_IV"としてキャッシュする。

        ATMが$2以上動いた場合は古いATMコードをunsubscribeして再subscribe。
        bid=ask=0（場外）はスキップして既存キャッシュを保持。
        失敗しても既存のYahoo/futuフォールバックにフォールバック（INFO扱い）。
        """
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return
        if spy_price <= 0:
            return

        # ATMが大きく動いた場合のみ再subscribe（スロット節約）
        if self._last_atm_spy_price is not None:
            if abs(spy_price - self._last_atm_spy_price) < self._atm_resubscribe_threshold:
                # ATM変化なし → 既存subscribeのpushを待つだけ（何もしない）
                return

        try:
            now_et = datetime.datetime.now(ET)
            expiry = now_et.strftime("%Y-%m-%d")

            # ATM付近のコードを取得（Call 3本・Put 3本）
            new_atm_codes: list = []
            for opt_type_ft, opt_label in [
                (ft.OptionType.CALL, "CALL"),
                (ft.OptionType.PUT,  "PUT"),
            ]:
                ret, chain_df = self.quote_ctx.get_option_chain(
                    self.underlying_code, start=expiry, end=expiry,
                    option_type=opt_type_ft,
                )
                if ret != RET_OK or chain_df.empty:
                    continue
                chain_df = chain_df.copy()
                chain_df["strike_price"] = chain_df["strike_price"].astype(float)
                # [ChainGuard] center_strike±20%フィルタで銘柄混入を除外
                if spy_price and spy_price > 0:
                    chain_df = chain_df[
                        (chain_df["strike_price"] - spy_price).abs() / spy_price <= 0.20
                    ]
                    if chain_df.empty:
                        log.warning(f"[ATMSubscribe/ChainGuard] {self.underlying_code} "
                                    f"center={spy_price:.1f} 全strike±20%外→銘柄混入疑い・スキップ")
                        continue
                chain_df["dist"] = (chain_df["strike_price"] - spy_price).abs()
                chain_sorted = chain_df.nsmallest(3, "dist")
                new_atm_codes.extend(chain_sorted["code"].tolist())

            if not new_atm_codes:
                log.debug("[ATMSubscribe] ATMコード取得失敗")
                return

            # 古いATMコードをunsubscribe
            if self._atm_subscribed_codes:
                old_codes = list(self._atm_subscribed_codes - set(new_atm_codes))
                if old_codes:
                    try:
                        self.quote_ctx.unsubscribe(old_codes, [SubType.QUOTE])
                        self._atm_subscribed_codes -= set(old_codes)
                        log.debug(f"[ATMSubscribe] unsubscribe old: {old_codes}")
                    except Exception:
                        pass

            # 新しいATMコードをsubscribe
            subscribe_codes = [c for c in new_atm_codes if c not in self._atm_subscribed_codes]
            if subscribe_codes:
                atm_handler = ATMOptionQuoteHandler(self._price_cache, spy_price)
                self.quote_ctx.set_handler(atm_handler)
                ret_s, msg_s = self.quote_ctx.subscribe(
                    subscribe_codes, [SubType.QUOTE],
                    subscribe_push=True, is_first_push=True,
                )
                if ret_s == RET_OK:
                    self._atm_subscribed_codes.update(subscribe_codes)
                    self._last_atm_spy_price = spy_price
                    log.info(
                        f"[ATMSubscribe] subscribe OK: {len(subscribe_codes)}本 "
                        f"(SPY={spy_price:.2f}) → VIX_ATM_IV をPriceCacheに流す"
                    )
                else:
                    log.info(f"[ATMSubscribe] subscribe失敗: {msg_s} → フォールバック継続")
        except Exception as e:
            log.debug(f"[ATMSubscribe] 例外: {e}")

    def subscribe_option_legs(self, codes: list) -> None:
        """保有中のオプションレッグをsubscribeしてリアルタイム価格をPriceCacheに流す。
        US OptionsはLV1権限付与済みのためSubType.QUOTEが使用可能。
        エントリー完了後に呼び出す（get_open_positionsで取得したコードを渡す）。
        失敗してもBotは正常動作（既存のget_market_snapshotにフォールバック）。
        """
        if not FUTU_AVAILABLE or not self.quote_ctx or not codes:
            return
        # 重複除去（すでにsubscribe済みのコードは除外）
        new_codes = [c for c in codes if c and c not in self._subscribed_option_codes]
        if not new_codes:
            return
        try:
            # OptionQuoteHandlerをset_handlerで登録
            # 注: set_handlerは上書きなのでSpyQuoteHandlerと共用する場合はどちらかのみ有効。
            # SPY subscribeは権限エラーで実質機能していないため、OptionQuoteHandlerを優先登録。
            opt_handler = OptionQuoteHandler(self._price_cache)
            self.quote_ctx.set_handler(opt_handler)
            ret, msg = self.quote_ctx.subscribe(new_codes, [SubType.QUOTE],
                                                subscribe_push=True, is_first_push=True)
            if ret == RET_OK:
                self._subscribed_option_codes.update(new_codes)
                log.info(f"[OptionSubscribe] subscribe OK: {new_codes}")
            else:
                log.info(f"[OptionSubscribe] subscribe失敗: {new_codes} → {msg} "
                         f"(get_market_snapshotにフォールバック)")
        except Exception as e:
            log.warning(f"[OptionSubscribe] 例外: {e} → get_market_snapshotにフォールバック")

    def unsubscribe_all_option_legs(self) -> None:
        """subscribeしているオプションレッグを全てunsubscribeしてスロットを解放する。
        決済完了後に呼び出す。
        """
        if not FUTU_AVAILABLE or not self.quote_ctx or not self._subscribed_option_codes:
            return
        codes = list(self._subscribed_option_codes)
        try:
            ret, msg = self.quote_ctx.unsubscribe(codes, [SubType.QUOTE])
            if ret == RET_OK:
                log.info(f"[OptionSubscribe] unsubscribe OK: {codes}")
            else:
                log.info(f"[OptionSubscribe] unsubscribe失敗: {codes} → {msg}")
        except Exception as e:
            log.warning(f"[OptionSubscribe] unsubscribe例外: {e}")
        finally:
            self._subscribed_option_codes.clear()

    def get_cached_option_price(self, code: str, max_age_sec: float = 10.0) -> Optional[float]:
        """PriceCacheからオプションのリアルタイム価格を取得する。
        OptionQuoteHandlerが書き込んだlast_priceを返す。
        キャッシュがない（subscribe失敗・タイムアウト）場合はNoneを返す。
        """
        return self._price_cache.get(code, max_age_sec=max_age_sec)

    def get_option_price(self, code: str) -> Optional[float]:
        """オプションコードの現在価格を取得する（ペーパー大量検証モード用）。

        1. PriceCacheのキャッシュ（subscribe経由）を確認
        2. キャッシュなし → get_market_snapshot()でポーリング
        3. DRY_TESTまたはfutu未接続 → Noneを返す（呼び出し側でfallback）

        Returns:
            mid_price (float) または None
        """
        # キャッシュから先に確認（subscribe済みの場合は高速）
        cached = self._price_cache.get(code, max_age_sec=30.0)
        if cached is not None:
            return cached

        if not FUTU_AVAILABLE or not self.quote_ctx:
            return None

        try:
            ret, snap = self.quote_ctx.get_market_snapshot([code])
            if ret != RET_OK or snap is None:
                return None
            try:
                rows = snap.to_dict("records") if hasattr(snap, "to_dict") else list(snap)
            except Exception:
                return None
            if not rows:
                return None
            row = rows[0]
            ask = float(row.get("ask_price") or row.get("ask", 0) or 0)
            bid = float(row.get("bid_price") or row.get("bid", 0) or 0)
            last = float(row.get("last_price") or row.get("last", 0) or 0)
            if ask > 0 and bid > 0:
                return (ask + bid) / 2.0
            if last > 0:
                return last
            return None
        except Exception as _gop_e:
            log.debug(f"[MarketData] get_option_price({code}): {_gop_e}")
            return None

    def close(self):
        if self.quote_ctx:
            try:
                self.quote_ctx.close()
            except Exception:
                pass

    def _get_vix_from_atm_straddle(self) -> Optional[float]:
        """ATMストラドルのIVからVIX代替値を算出する。
        futu VIXもYahoo VIXも取得できない場合の最終フォールバック。

        ロジック:
          1. get_spy_snapshot() で SPY 現在値を取得（Finnhub フォールバックあり）
          2. 0DTE の expiry を ET 現在日付から取得
          3. get_option_chain() で ATM の Call/Put コードを取得
          4. get_market_snapshot() の option_implied_volatility を直接読む
             → フィールドが 0 の場合は Black-Scholes(brentq) で mid-price から逆算
          5. IV（小数）× 100 = VIX 近似値として返す

        制約:
          - オプション snapshot は US Options LV1 権限で取得可（確認済み）
          - SPY 株式 snapshot は権限なし → Finnhub フォールバック経由
        """
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return None
        try:
            # 1. SPY 現在値
            spy_snap = self.get_spy_snapshot()
            if spy_snap is None:
                return None
            spy_price = spy_snap.get("last_price", 0.0)
            if spy_price <= 0:
                return None

            # 2. 0DTE expiry（ET 当日日付）
            now_et = datetime.datetime.now(ET)
            expiry = now_et.strftime("%Y-%m-%d")

            # 3. ATM 付近の Call/Put を chain から取得
            iv_values: list = []
            for opt_type_ft, opt_label in [
                (ft.OptionType.CALL, "CALL"),
                (ft.OptionType.PUT,  "PUT"),
            ]:
                ret, chain_df = self.quote_ctx.get_option_chain(
                    self.underlying_code, start=expiry, end=expiry,
                    option_type=opt_type_ft,
                )
                if ret != RET_OK or chain_df.empty:
                    log.debug(f"[VIX-ATM-IV] chain 取得失敗: {opt_label} {expiry}")
                    continue

                # ATM に最も近いストライクを 1 本だけ選ぶ
                chain_df["strike_price"] = chain_df["strike_price"].astype(float)
                chain_df["dist"] = (chain_df["strike_price"] - spy_price).abs()
                atm_row = chain_df.loc[chain_df["dist"].idxmin()]
                atm_code = atm_row["code"]
                atm_strike = float(atm_row["strike_price"])

                # 4. snapshot で IV を取得
                ret2, snap = self.quote_ctx.get_market_snapshot([atm_code])
                if ret2 != RET_OK or snap.empty:
                    log.debug(f"[VIX-ATM-IV] snapshot 失敗: {atm_code}")
                    continue

                srow = snap.iloc[0]
                iv_direct = float(srow.get("option_implied_volatility", 0) or 0)

                if iv_direct > 0:
                    iv_values.append(iv_direct)
                    log.debug(
                        f"[VIX-ATM-IV] {opt_label} K={atm_strike:.1f} "
                        f"IV(direct)={iv_direct*100:.1f}%"
                    )
                    continue

                # IV フィールドが 0 → B-S で mid-price から逆算（brentq）
                bid = float(srow.get("bid_price", 0) or 0)
                ask = float(srow.get("ask_price", 0) or 0)
                mid = (bid + ask) / 2.0
                if mid <= 0:
                    log.debug(f"[VIX-ATM-IV] {opt_label} mid=0, skip")
                    continue

                # 残存時間 T（年率換算）: 当日 16:00 ET まで
                close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
                t_sec = (close_et - now_et).total_seconds()
                if t_sec <= 60:  # 市場終了直前は計算不安定なのでスキップ
                    log.debug(f"[VIX-ATM-IV] t_sec={t_sec:.0f} < 60s, skip BS")
                    continue
                T = max(t_sec / (365 * 24 * 3600), 1e-6)

                # Black-Scholes call/put 価格（金利・配当ゼロ近似）
                from scipy.optimize import brentq

                def bs_price(sigma: float, is_call: bool) -> float:
                    if sigma <= 0 or T <= 0:
                        return 0.0
                    d1 = (math.log(spy_price / atm_strike) + 0.5 * sigma**2 * T) / (
                        sigma * math.sqrt(T)
                    )
                    d2 = d1 - sigma * math.sqrt(T)
                    from math import erf

                    def norm_cdf(x: float) -> float:
                        return 0.5 * (1.0 + erf(x / math.sqrt(2)))

                    if is_call:
                        return spy_price * norm_cdf(d1) - atm_strike * norm_cdf(d2)
                    else:
                        return atm_strike * norm_cdf(-d2) - spy_price * norm_cdf(-d1)

                is_call = opt_label == "CALL"
                try:
                    iv_bs = brentq(
                        lambda s: bs_price(s, is_call) - mid,
                        1e-4, 10.0, xtol=1e-4, maxiter=100,
                    )
                    if 0.01 < iv_bs < 5.0:  # 1% ~ 500% の合理的範囲
                        iv_values.append(iv_bs)
                        log.debug(
                            f"[VIX-ATM-IV] {opt_label} K={atm_strike:.1f} "
                            f"mid={mid:.3f} IV(BS)={iv_bs*100:.1f}%"
                        )
                except Exception as e:
                    log.debug(f"[VIX-ATM-IV] brentq 失敗 {opt_label}: {e}")

            if not iv_values:
                return None

            vix_approx = (sum(iv_values) / len(iv_values)) * 100.0
            log.info(f"[VIX-ATM-IV] ATMストラドルIVからVIX算出: {vix_approx:.1f}")
            return vix_approx

        except Exception as e:
            log.warning(f"[VIX-ATM-IV] 計算失敗: {e}")
            return None

    def _get_vix_yahoo(self) -> Optional[float]:
        """Yahoo Finance経由でVIX取得（futu未対応のため標準ソース）。
        VIXはCBOEインデックス値のためexchangeDataDelayedBy=0（遅延ゼロ）確認済み。
        """
        try:
            resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5,
            )
            data = resp.json()
            v = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
            if v > 0:
                if not self._vix_is_fallback:
                    self._vix_is_fallback = True
                    log.info(f"VIX: Yahoo Finance使用（futu未対応）")
                return v
        except Exception as e:
            log.warning(f"Yahoo VIX取得失敗: {e}")
        return None

    def get_vix(self) -> Optional[float]:
        if DRY_TEST:
            # dry-testモード: Yahoo Finance実データを使用（固定値禁止）
            v = self._get_vix_yahoo()
            if v is not None:
                log.info(f"[DRY-TEST] VIX via Yahoo Finance: {v:.2f}")
                return v
            # Yahoo 失敗 → ATM ストラドル IV
            v = self._get_vix_from_atm_straddle()
            if v is not None:
                log.info(f"[DRY-TEST] VIX via ATM Straddle IV: {v:.2f}")
                return v
            log.warning("[DRY-TEST] VIX取得失敗: フォールバック18.0を使用")
            return 18.0
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return 18.0  # dry-run default
        # 1. PriceCacheにATM subscribe IVがあれば最優先使用（場中のみ有効・max_age=30s）
        cached_atm_iv = self._price_cache.get("VIX_ATM_IV", max_age_sec=30.0)
        if cached_atm_iv is not None:
            self._vix_is_fallback = False
            log.debug(f"[VIXBand] VIX via ATM subscribe cache: {cached_atm_iv:.2f}")
            return cached_atm_iv
        # 2. PriceCacheにsubscribeプッシュデータがあれば優先使用（ほぼ0秒遅延）
        cached_vix = self._price_cache.get("US.VIX", max_age_sec=5.0)
        if cached_vix is not None:
            self._vix_is_fallback = False
            return cached_vix
        for attempt in range(2):
            try:
                ret, data = self.quote_ctx.get_market_snapshot(["US.VIX"])
                if ret == RET_OK and not data.empty:
                    self._vix_is_fallback = False  # futu成功 → リアルタイムデータ
                    return float(data.iloc[0]["last_price"])
            except Exception as e:
                log.warning(f"get_vix attempt {attempt + 1}: {e}")
            if attempt == 0:
                time.sleep(1)
        # Futu失敗 → Yahoo Finance（標準ソース）
        yahoo_vix = self._get_vix_yahoo()
        if yahoo_vix is not None:
            return yahoo_vix
        # Yahoo 失敗 → ATM ストラドル IV（最終フォールバック）
        return self._get_vix_from_atm_straddle()

    def get_vix9d_vvix(self) -> tuple:
        """Yahoo FinanceからVIX9D（9日VIX）とVVIX（VIXのVIX）を取得する。

        Returns:
            (vix9d, vvix) — 取得失敗の場合はそれぞれNone
        """
        values: dict = {}
        for symbol, key in (("%5EVIX9D", "vix9d"), ("%5EVVIX", "vvix")):
            try:
                resp = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=5,
                )
                v = float(resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
                if v > 0:
                    values[key] = v
            except Exception as e:
                log.warning(f"get_vix9d_vvix {symbol}: {e}")
        return values.get("vix9d"), values.get("vvix")

    def get_global_risk_data(self) -> dict:
        """Yahoo FinanceからグローバルインデックスとUS国債利回りを取得する。

        取得対象:
          - Nikkei 225 (^N225), DAX (^GDAXI), FTSE 100 (^FTSE) — 前日比%
          - 10年利回り (^TNX), 2年利回り (^TWOYEAR), 3ヶ月T-bill (^IRX) — 水準(%)
          - 2Y-10Y スプレッド (逆転 < 0 = 景気後退シグナル・最重要)
          - 10Y-3M スプレッド (逆転 < 0 = 信用収縮シグナル)

        Returns:
            {
              "nikkei_chg": float|None,      # 前日比 %
              "dax_chg": float|None,
              "ftse_chg": float|None,
              "us10y": float|None,           # 10年利回り %
              "us2y": float|None,            # 2年利回り %
              "us3m": float|None,            # 3ヶ月利回り %
              "spread_10y_2y": float|None,   # 10Y - 2Y (負=逆転=景気後退シグナル)
              "spread_10y_3m": float|None,   # 10Y - 3M (負=逆転=信用収縮シグナル)
              "down_count": int,             # 0.5%超下落インデックス数 (0-3)
              "up_count": int,               # 0.5%超上昇インデックス数 (0-3)
              "global_risk_signal": str,     # "risk_off" / "neutral" / "risk_on"
            }
        """
        headers = {"User-Agent": "Mozilla/5.0"}
        result: dict = {
            "nikkei_chg": None, "dax_chg": None, "ftse_chg": None,
            "us10y": None, "us2y": None, "us3m": None,
            "spread_10y_2y": None, "spread_10y_3m": None,
            "down_count": 0, "up_count": 0,
            "global_risk_signal": "neutral",
        }

        # ── グローバル株価インデックス ────────────────────────────────────────
        index_map = [
            ("%5EN225",  "nikkei_chg"),
            ("%5EGDAXI", "dax_chg"),
            ("%5EFTSE",  "ftse_chg"),
        ]
        for encoded, key in index_map:
            try:
                resp = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}",
                    headers=headers, timeout=8,
                )
                meta = resp.json()["chart"]["result"][0]["meta"]
                price = float(meta["regularMarketPrice"])
                prev  = float(meta.get("previousClose") or meta.get("chartPreviousClose") or 0)
                if prev > 0:
                    chg = round((price - prev) / prev * 100, 3)
                    result[key] = chg
            except Exception as e:
                log.warning(f"get_global_risk_data {encoded}: {e}")

        # ── US国債利回り ──────────────────────────────────────────────────────
        # 2YY=F = 2年物国債利回り（ES先物ギャップ方向・リスクオン/オフ判断に使用）
        # ^TNX=10Y, 2YY=F=2Y(Futures proxy→yield%), ^IRX=3M T-bill
        yield_map = [("%5ETNX", "us10y"), ("2YY%3DF", "us2y"), ("%5EIRX", "us3m")]
        for encoded, key in yield_map:
            try:
                resp = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}",
                    headers=headers, timeout=8,
                )
                v = float(resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
                if v > 0:
                    result[key] = round(v, 3)
            except Exception as e:
                log.warning(f"get_global_risk_data {encoded}: {e}")

        # ── 2Y-10Yスプレッド（景気後退シグナル・最重要）──────────────────────
        if result["us10y"] is not None and result["us2y"] is not None:
            result["spread_10y_2y"] = round(result["us10y"] - result["us2y"], 3)

        # ── 10Y-3Mスプレッド（信用収縮シグナル）─────────────────────────────
        if result["us10y"] is not None and result["us3m"] is not None:
            result["spread_10y_3m"] = round(result["us10y"] - result["us3m"], 3)

        # ── リスクシグナル集計 ───────────────────────────────────────────────
        chg_vals = [result["nikkei_chg"], result["dax_chg"], result["ftse_chg"]]
        down_count = sum(1 for c in chg_vals if c is not None and c < -0.5)
        up_count   = sum(1 for c in chg_vals if c is not None and c >  0.5)
        result["down_count"] = down_count
        result["up_count"]   = up_count

        sp_3m = result["spread_10y_3m"]
        sp_2y = result["spread_10y_2y"]
        if (down_count >= 2
                or (sp_3m is not None and sp_3m < -0.3)
                or (sp_2y is not None and sp_2y < -0.3)):
            result["global_risk_signal"] = "risk_off"
        elif up_count >= 2:
            result["global_risk_signal"] = "risk_on"
        # else: neutral

        log.info(
            f"[GlobalRisk] Nikkei={result['nikkei_chg']}% "
            f"DAX={result['dax_chg']}% FTSE={result['ftse_chg']}% "
            f"10Y={result['us10y']}% 2Y={result['us2y']}% 3M={result['us3m']}% "
            f"10Y-2Y={result['spread_10y_2y']} 10Y-3M={result['spread_10y_3m']} "
            f"down={down_count} up={up_count} signal={result['global_risk_signal']}"
        )
        return result

    def get_put_call_ratio(self) -> Optional[float]:
        """Put/Call ratioを取得する（G-NEW4）。

        取得順序（フォールバック方式）:
        1. futu get_option_chain() から SPY の当日 OI合計を Call/Put で計算（最も正確）
        2. VIXをベースにした代用値（VIX正規化P/C推定）

        解釈:
          P/C > 1.2 = 極端な恐怖 → 売り戦術に有利（IVが高い）
          P/C < 0.5 = 極端な楽観 → 買い戦術に有利
          0.5〜1.2  = 通常レンジ

        Returns:
            float|None: P/C ratio。取得失敗時は None。
        """
        # 1. futu option_chain から OI比率計算（futu接続時）
        if FUTU_AVAILABLE and self.quote_ctx is not None:
            try:
                import futu as ft
                now_et = datetime.datetime.now(ET)
                expiry = now_et.strftime("%Y-%m-%d")
                call_oi = 0
                put_oi  = 0
                for opt_type in [ft.OptionType.CALL, ft.OptionType.PUT]:
                    ret, df = self.quote_ctx.get_option_chain(
                        self.underlying_code, start=expiry, end=expiry,
                        option_type=opt_type,
                    )
                    if ret == 0 and not df.empty:
                        oi_col = "open_interest" if "open_interest" in df.columns else None
                        if oi_col:
                            total = int(df[oi_col].fillna(0).sum())
                            if opt_type == ft.OptionType.CALL:
                                call_oi = total
                            else:
                                put_oi = total
                if call_oi > 0 and put_oi > 0:
                    pc = round(put_oi / call_oi, 3)
                    log.info(f"[PutCallRatio] futu OI: put={put_oi} call={call_oi} P/C={pc:.3f}")
                    return pc
            except Exception as e:
                log.debug(f"get_put_call_ratio futu: {e}")

        # 2. VIXベースの代用P/C推定
        # VIX 15以下→楽観 P/C≈0.6, VIX 25以上→恐怖 P/C≈1.1
        # 線形補間: P/C = 0.6 + (vix - 15) * 0.05 clamp(0.45, 1.5)
        try:
            vix = self._get_vix_yahoo()
            if vix and vix > 0:
                pc_est = 0.6 + (vix - 15.0) * 0.05
                pc_est = round(max(0.45, min(1.50, pc_est)), 3)
                log.info(f"[PutCallRatio] VIX={vix:.1f} → P/C推定={pc_est:.3f} (VIXベース代用)")
                return pc_est
        except Exception as e:
            log.debug(f"get_put_call_ratio vix_fallback: {e}")

        return None

    def get_skew_index(self) -> Optional[float]:
        """CBOE SKEW指数をYahoo Finance経由で取得する（G-NEW11）。

        SKEW(^SKEW): テールリスクの市場認識。
          SKEW < 100: テールリスク低い
          SKEW 110-120: 通常レンジ（OTMプットに需要）
          SKEW > 135: 高テールリスク → OTM PUTが高い → IVスキュー大きい → CS売りで短いストライク有利

        Returns:
            float|None: SKEW値。取得失敗時は None。
        """
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5ESKEW",
                headers=headers, timeout=8,
            )
            meta = resp.json()["chart"]["result"][0]["meta"]
            skew = float(meta["regularMarketPrice"])
            if skew > 0:
                log.info(f"[SKEW] ^SKEW={skew:.1f}")
                return round(skew, 1)
        except Exception as e:
            log.warning(f"get_skew_index: {e}")
        return None

    def get_news_sentiment(self, symbol: str = "SPY") -> dict:
        """Finnhub News APIで直近ニュースのセンチメントを取得する（G-NEW8）。

        直近24時間のニュースを取得して重大イベント（FRB発言・地政学リスク）を検知する。
        Finnhub無料枠: 30req/s。

        Returns:
            {
              "count": int,          # 直近24時間のニュース件数
              "has_fed_news": bool,  # FRB/Fed関連ニュースあり
              "has_geo_risk": bool,  # 地政学リスク関連ニュースあり
              "risk_level": "low"|"medium"|"high",
              "headlines": list[str],  # 上位3件
            }
        """
        result = {"count": 0, "has_fed_news": False, "has_geo_risk": False,
                  "risk_level": "low", "headlines": []}
        try:
            now = datetime.datetime.now(ET)
            from_dt = (now - datetime.timedelta(hours=24)).strftime("%Y-%m-%d")
            to_dt   = now.strftime("%Y-%m-%d")
            resp = requests.get(
                "https://finnhub.io/api/v1/company-news",
                params={
                    "symbol": symbol,
                    "from": from_dt, "to": to_dt,
                    "token": FINNHUB_API_KEY,
                },
                timeout=8,
            )
            articles = resp.json()
            if not isinstance(articles, list):
                return result

            result["count"] = len(articles)
            fed_keywords = ["fed ", "federal reserve", "fomc", "powell", "rate cut",
                            "rate hike", "interest rate", "monetary policy", "hawkish", "dovish"]
            geo_keywords = ["war", "conflict", "sanction", "tariff", "invasion",
                            "military", "geopolit", "crisis", "default"]

            headlines = []
            for art in articles[:20]:  # 最新20件をチェック
                headline = str(art.get("headline", "")).lower()
                if any(kw in headline for kw in fed_keywords):
                    result["has_fed_news"] = True
                if any(kw in headline for kw in geo_keywords):
                    result["has_geo_risk"] = True
                if len(headlines) < 3:
                    headlines.append(art.get("headline", ""))

            result["headlines"] = headlines

            if result["has_fed_news"] and result["has_geo_risk"]:
                result["risk_level"] = "high"
            elif result["has_fed_news"] or result["has_geo_risk"]:
                result["risk_level"] = "medium"

            log.info(
                f"[News] count={result['count']} fed={result['has_fed_news']} "
                f"geo={result['has_geo_risk']} risk={result['risk_level']}"
            )
        except Exception as e:
            log.warning(f"get_news_sentiment: {e}")
        return result

    def _get_spy_price_finnhub(self) -> Optional[dict]:
        """Finnhub経由でSPY価格取得（futuがUS Securities権限なし時の標準ソース）。
        5分以内のキャッシュがあればAPI呼び出しをスキップして返す。
        """
        now = datetime.datetime.now(ET)
        cached = self._finnhub_cache.get("SPY")
        if cached is not None:
            age_sec = (now - cached["ts"]).total_seconds()
            if age_sec < 300:  # 5分 = 300秒
                log.debug(f"SPY price via Finnhub cache (age={age_sec:.0f}s): last={cached['data']['last_price']:.2f}")
                return cached["data"]
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": "SPY", "token": FINNHUB_API_KEY},
                timeout=5,
            )
            data = resp.json()
            last  = float(data.get("c") or 0)   # current price
            open_ = float(data.get("o") or last) # open price
            if last > 0:
                if not self._spy_is_fallback:
                    self._spy_is_fallback = True
                    log.info(f"SPY: Finnhub使用（futu権限なし）")
                log.debug(f"SPY price via Finnhub: last={last:.2f} open={open_:.2f}")
                result = {"open_price": open_, "last_price": last}
                self._finnhub_cache["SPY"] = {"data": result, "ts": now}
                return result
        except Exception as e:
            log.warning(f"Finnhub SPY取得失敗: {e}")
        return None

    def get_spy_snapshot(self) -> Optional[dict]:
        """Returns dict with open_price and last_price for SPY."""
        if DRY_TEST:
            # dry-testモード: Finnhub実データを使用（固定値禁止）
            snap = self._get_spy_price_finnhub()
            if snap is not None:
                log.info(f"[DRY-TEST] SPY via Finnhub: last={snap['last_price']:.2f}")
                return snap
            log.warning("[DRY-TEST] SPY取得失敗: フォールバック562.5を使用")
            return {"open_price": 562.5, "last_price": 562.5}
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return {"open_price": 562.5, "last_price": 562.5}  # dry-run
        # PriceCacheにsubscribeプッシュデータがあれば優先使用（ほぼ0秒遅延）
        cached_last = self._price_cache.get(self.underlying_code, max_age_sec=5.0)
        cached_open = self._price_cache.get_open(self.underlying_code, max_age_sec=5.0)
        if cached_last is not None:
            self._spy_is_fallback = False
            result = {
                "last_price": cached_last,
                "open_price": cached_open if cached_open is not None else cached_last,
            }
            return result
        for attempt in range(2):
            try:
                ret, snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                if ret == RET_OK and not snap.empty:
                    row = snap.iloc[0]
                    result = {
                        "open_price":  float(row.get("open_price", 0) or 0),
                        "last_price":  float(row.get("last_price", 0) or 0),
                    }
                    if result["last_price"] > 0:
                        self._spy_is_fallback = False  # futu成功
                        return result
            except Exception as e:
                log.warning(f"get_spy_snapshot attempt {attempt + 1}: {e}")
            if attempt == 0:
                time.sleep(1)
        # Futu失敗 → Finnhub（標準ソース）
        return self._get_spy_price_finnhub()

    def get_spy_open(self) -> Optional[float]:
        snap = self.get_spy_snapshot()
        if snap:
            val = snap.get("open_price") or snap.get("last_price")
            return val if val and val > 0 else None
        return None

    def get_spy_current(self) -> Optional[float]:
        snap = self.get_spy_snapshot()
        if snap:
            val = snap.get("last_price") or snap.get("open_price")
            return val if val and val > 0 else None
        return None

    def calc_ivr(self, current_vix: float) -> Optional[float]:
        """
        IV Rank = 過去IVデータに対する現在IVのパーセンタイル順位 (0-100)。

        優先度:
          1. ThetaData実績IVベース（USE_THETADATA_IVR=True かつデータ充足時）
          2. キャッシュ（同日・前日の計算済み値）
          3. futu VIX kline
          4. Yahoo Finance VIX kline

        Returns 0-100, or None if data unavailable.
        """
        # 1st: ThetaData実績IVベース（USE_THETADATA_IVR=Trueの場合に優先）
        if USE_THETADATA_IVR:
            _symbol = (self.underlying_code or "US.SPY").replace("US.", "")
            td_ivr = calc_ivr_from_thetadata(symbol=_symbol)
            if td_ivr is not None:
                save_ivr_cache(td_ivr)
                return td_ivr
            log.debug("calc_ivr: ThetaData IVR unavailable → VIX-based fallback")

        cached = load_ivr_cache()
        if cached is not None:
            return cached

        if not FUTU_AVAILABLE or not self.quote_ctx:
            return None

        closes = None

        # futu (VIX kline)
        try:
            end_date   = datetime.datetime.now(ET).date()
            start_date = end_date - datetime.timedelta(days=380)
            ret, kline, _ = self.quote_ctx.request_history_kline(
                "US.VIX",
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                ktype=ft.KLType.K_DAY,
                max_count=300,
            )
            if ret == RET_OK and not kline.empty:
                closes = kline["close"].astype(float).tolist()[-252:]
        except Exception as e:
            log.warning(f"calc_ivr futu: {e}")

        # 2nd: Yahoo Finance fallback (1 year of VIX daily closes)
        if not closes:
            try:
                end_ts = int(time.time())
                start_ts = end_ts - 380 * 86400
                resp = requests.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                    params={"period1": start_ts, "period2": end_ts,
                            "interval": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                data = resp.json()
                closes_raw = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                closes = [float(c) for c in closes_raw if c is not None][-252:]
                if closes:
                    log.info(f"IVR: VIX history via Yahoo ({len(closes)} days)")
            except Exception as e:
                log.warning(f"calc_ivr Yahoo fallback: {e}")

        if not closes or len(closes) < 20:
            log.warning("IVR: VIX history insufficient")
            return None

        vix_52w_high = max(closes)
        vix_52w_low  = min(closes)
        if vix_52w_high <= vix_52w_low:
            return 50.0
        ivr = (current_vix - vix_52w_low) / (vix_52w_high - vix_52w_low) * 100
        ivr = max(0.0, min(100.0, round(ivr, 1)))
        save_ivr_cache(ivr)
        log.info(f"IVR={ivr:.1f} (VIX={current_vix:.1f}, 52w: {vix_52w_low:.1f}-{vix_52w_high:.1f})")
        return ivr

    def get_ivr_percentiles(self) -> tuple:
        """Compute dynamic IVR thresholds from rolling history.

        Returns (ivr_low, ivr_high) using P25 and P75 of the stored IVR history.
        Falls back to (IVR_LOW, IVR_HIGH) constants when history has fewer than
        20 data points.

        Design rationale (from research_dynamic_params_design.md):
        - Backtest period IVR distribution: p25=7.4, p75=17.4, max=36.1
        - Fixed IVR_HIGH=75 essentially never fires (max observed = 36.1)
        - P25/P75 of actual distribution correctly captures 'low/high IV' regimes
        """
        history = load_ivr_history()
        if len(history) < 20:
            log.warning(
                f"get_ivr_percentiles: history too short ({len(history)} days), "
                f"using fallback ({IVR_LOW}, {IVR_HIGH})"
            )
            return float(IVR_LOW), float(IVR_HIGH)
        sorted_ivr = sorted(history)
        n = len(sorted_ivr)
        p25_idx = int(0.25 * (n - 1))
        p75_idx = int(0.75 * (n - 1))
        ivr_low  = round(sorted_ivr[p25_idx], 1)
        ivr_high = round(sorted_ivr[p75_idx], 1)
        log.info(
            f"IVR percentiles: P25={ivr_low} P75={ivr_high} "
            f"(n={n}, range={sorted_ivr[0]:.1f}-{sorted_ivr[-1]:.1f})"
        )
        return ivr_low, ivr_high

    def get_vix_history(self, days: int = 60) -> list:
        """過去N営業日のVIX終値リストを返す（動的閾値算出用）。
        futu klineが使えない場合はYahoo Financeフォールバック。
        """
        # Try futu first
        if FUTU_AVAILABLE and self.quote_ctx:
            try:
                end_date   = datetime.datetime.now(ET).date()
                start_date = end_date - datetime.timedelta(days=int(days * 1.7))
                ret, kline, _ = self.quote_ctx.request_history_kline(
                    "US.VIX",
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d"),
                    ktype=ft.KLType.K_DAY,
                    max_count=days + 20,
                )
                if ret == RET_OK and not kline.empty:
                    closes = kline["close"].astype(float).tolist()
                    return closes[-days:] if len(closes) > days else closes
            except Exception as e:
                log.warning(f"get_vix_history futu: {e}")

        # Yahoo Finance fallback (past ~90 calendar days to get 60 trading days)
        try:
            end_ts   = int(time.time())
            start_ts = end_ts - (days * 2) * 86400
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
                f"?period1={start_ts}&period2={end_ts}&interval=1d",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            data = resp.json()
            closes_raw = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [float(c) for c in closes_raw if c is not None]
            return closes[-days:] if len(closes) > days else closes
        except Exception as e:
            log.warning(f"get_vix_history yahoo: {e}")
        return []

    def calc_dynamic_vix_spike_threshold(
        self,
        atr_period: int = 20,
        atr_multiplier: float = 1.5,
        floor: float = 1.5,
        cap: float = 6.0,
    ) -> float:
        """過去atr_period日のVIX日次変化量のATRから動的スパイク閾値を算出。

        VIX=12環境で+3は25%上昇、VIX=40環境で+3は7.5%上昇と、
        絶対値固定では環境依存が大きいため、VIX変動幅自体から閾値を動的に算出する。

        閾値 = ATR(20日) × 1.5
        Floor=1.5, Cap=6.0 でフォールバック値(3.0)を包む範囲に収める。

        データ不足・取得失敗時はVIX_SPIKE_THRESHOLD(=3.0)をフォールバックとして返す。
        """
        try:
            closes = self.get_vix_history(days=atr_period + 5)
            if len(closes) < atr_period + 1:
                log.warning(
                    f"calc_dynamic_vix_spike_threshold: VIX履歴不足 "
                    f"({len(closes)}日分, 必要={atr_period + 1}日) → フォールバック {VIX_SPIKE_THRESHOLD}"
                )
                return VIX_SPIKE_THRESHOLD

            # 日次変化量の絶対値リスト（ATRの近似: 終値差の絶対値）
            daily_changes = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
            # 直近atr_period日分を使用
            recent_changes = daily_changes[-atr_period:]
            atr = sum(recent_changes) / len(recent_changes)

            threshold = atr * atr_multiplier
            threshold = max(floor, min(cap, threshold))

            log.info(
                f"[DynVIXThreshold] ATR({atr_period}d)={atr:.3f} × {atr_multiplier} "
                f"= {atr * atr_multiplier:.3f} → clamp({floor},{cap}) = {threshold:.3f}"
            )
            return threshold

        except Exception as e:
            log.warning(f"calc_dynamic_vix_spike_threshold failed: {e} → フォールバック {VIX_SPIKE_THRESHOLD}")
            return VIX_SPIKE_THRESHOLD

    def get_spy_daily_closes(self, days: int = 35) -> list:
        """過去N営業日のSPY終値リストを返す（実現ボラティリティ計算用）。"""
        if FUTU_AVAILABLE and self.quote_ctx:
            try:
                end_date   = datetime.datetime.now(ET).date()
                start_date = end_date - datetime.timedelta(days=int(days * 1.7))
                ret, kline, _ = self.quote_ctx.request_history_kline(
                    self.underlying_code,
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d"),
                    ktype=ft.KLType.K_DAY,
                    max_count=days + 10,
                )
                if ret == RET_OK and not kline.empty:
                    closes = kline["close"].astype(float).tolist()
                    return closes[-days:] if len(closes) > days else closes
            except Exception as e:
                log.warning(f"get_spy_daily_closes futu: {e}")

        # Yahoo Finance fallback
        try:
            end_ts = int(time.time())
            start_ts = end_ts - (days * 2) * 86400
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/SPY",
                params={"period1": start_ts, "period2": end_ts,
                        "interval": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            data = resp.json()
            closes = [float(c) for c in data["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
            if closes:
                log.info(f"get_spy_daily_closes via Yahoo: {len(closes)} days")
                return closes[-days:] if len(closes) > days else closes
        except Exception as e:
            log.warning(f"get_spy_daily_closes yahoo: {e}")

        # Finnhub fallback
        try:
            end_ts   = int(time.time())
            start_ts = end_ts - (days * 2) * 86400
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/candle",
                params={"symbol": "SPY", "resolution": "D",
                        "from": start_ts, "to": end_ts,
                        "token": FINNHUB_API_KEY},
                timeout=10,
            )
            data = resp.json()
            closes = [float(c) for c in data.get("c", [])]
            if closes:
                log.info(f"get_spy_daily_closes via Finnhub: {len(closes)} days")
            return closes[-days:] if len(closes) > days else closes
        except Exception as e:
            log.warning(f"get_spy_daily_closes finnhub: {e}")
        return []

    def calc_vrp(self, current_vix: float) -> Optional[float]:
        """VRP = VIX - 30日実現ボラティリティ（年率換算）。
        VRP < 0はプレミアム売りの期待値がマイナスであることを示す。
        """
        closes = self.get_spy_daily_closes(VRP_REALIZED_DAYS + 1)
        if len(closes) < VRP_REALIZED_DAYS + 1:
            log.warning(f"VRP: SPY daily closes insufficient ({len(closes)}/{VRP_REALIZED_DAYS + 1})")
            return None
        # log returns
        log_returns = [math.log(closes[i] / closes[i - 1])
                       for i in range(1, len(closes))]
        if not log_returns:
            return None
        # standard deviation of log returns
        mean_r = sum(log_returns) / len(log_returns)
        var_r  = sum((r - mean_r) ** 2 for r in log_returns) / (len(log_returns) - 1)
        realized_vol = math.sqrt(var_r) * math.sqrt(252) * 100  # annualized, in %
        vrp = current_vix - realized_vol
        log.info(f"VRP={vrp:.2f} (VIX={current_vix:.1f}, RV30={realized_vol:.1f}%)")
        return round(vrp, 2)

    def get_symbol_atr(self, symbol: str, period: int = 14) -> Optional[float]:
        """任意銘柄のATR(N日)を取得する。

        futu request_history_kline → Yahoo Finance の順でフォールバック。
        symbol: "US.SPY", "US.TSLA" 等の futu コード形式。

        Returns: ATR (ドル) または None
        """
        ticker = _futu_to_yahoo_ticker(symbol)  # "US..SPX" → "^SPX"
        closes, highs, lows = [], [], []

        # futu
        if FUTU_AVAILABLE and self.quote_ctx:
            try:
                end_date   = datetime.datetime.now().date()
                start_date = end_date - datetime.timedelta(days=int(period * 2.5))
                ret, kline, _ = self.quote_ctx.request_history_kline(
                    symbol,
                    start=start_date.strftime("%Y-%m-%d"),
                    end=end_date.strftime("%Y-%m-%d"),
                    ktype=ft.KLType.K_DAY,
                    max_count=period + 5,
                )
                if ret == RET_OK and not kline.empty and len(kline) >= period:
                    closes = kline["close"].astype(float).tolist()
                    highs  = kline["high"].astype(float).tolist()
                    lows   = kline["low"].astype(float).tolist()
            except Exception as e:
                log.warning(f"[SymbolATR] {symbol} futu kline error: {e}")

        # Yahoo Finance フォールバック
        if not closes:
            try:
                end_ts   = int(time.time())
                start_ts = end_ts - period * 3 * 86400
                resp = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                    params={"period1": start_ts, "period2": end_ts, "interval": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=8,
                )
                resp.raise_for_status()
                quotes = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
                raw_c = quotes.get("close", [])
                raw_h = quotes.get("high", [])
                raw_l = quotes.get("low", [])
                triplets = [
                    (float(c), float(h), float(l))
                    for c, h, l in zip(raw_c, raw_h, raw_l)
                    if c is not None and h is not None and l is not None
                ]
                if triplets:
                    closes = [t[0] for t in triplets]
                    highs  = [t[1] for t in triplets]
                    lows   = [t[2] for t in triplets]
            except Exception as e:
                log.warning(f"[SymbolATR] {symbol} Yahoo kline error: {e}")

        if len(closes) < period + 1:
            log.warning(f"[SymbolATR] {symbol}: insufficient data ({len(closes)} bars, need {period+1})")
            return None

        # True Range 計算
        trs = []
        for i in range(1, len(closes)):
            h = highs[i]; l = lows[i]; pc = closes[i - 1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))

        atr = sum(trs[-period:]) / period
        log.info(f"[SymbolATR] {symbol}: ATR({period})={atr:.3f}")
        return round(atr, 4)

    def get_symbol_hv(self, symbol: str, period: int = 20) -> Optional[float]:
        """任意銘柄のHV(N日)を取得する（年率換算、小数表記）。

        Yahoo Finance から日次終値を取得してログリターンの標準偏差を計算。

        Returns: HV (年率、小数。例: 0.22 = 22%) または None
        """
        ticker = _futu_to_yahoo_ticker(symbol)  # "US..SPX" → "^SPX"
        try:
            end_ts   = int(time.time()) - 86400
            start_ts = end_ts - 90 * 86400
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"period1": start_ts, "period2": end_ts, "interval": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            resp.raise_for_status()
            raw_c = [c for c in resp.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", [])
                     if c is not None]
            if len(raw_c) < period + 1:
                log.warning(f"[SymbolHV] {symbol}: insufficient data ({len(raw_c)})")
                return None
            log_returns = [math.log(raw_c[i] / raw_c[i - 1]) for i in range(1, len(raw_c))]
            recent = log_returns[-period:]
            mean_r = sum(recent) / len(recent)
            var_r  = sum((r - mean_r) ** 2 for r in recent) / (len(recent) - 1)
            hv = math.sqrt(var_r) * math.sqrt(252)
            log.info(f"[SymbolHV] {symbol}: HV({period})={hv:.4f} ({hv*100:.1f}%)")
            return round(hv, 4)
        except Exception as e:
            log.warning(f"[SymbolHV] {symbol} Yahoo error: {e}")
            return None

    def get_symbol_atr_pct(self, symbol: str, period: int = 14) -> Optional[float]:
        """ATR(N日) を % 表記 (小数) で返す。

        atr_daily_pct = ATR / last_price
        ORBブレイクアウト閾値の計算に使う。

        Returns: ATR% (小数。例: 0.015 = 1.5%) または None
        """
        atr = self.get_symbol_atr(symbol, period)
        if atr is None:
            return None

        # 直近価格を取得して割り算
        ticker = _futu_to_yahoo_ticker(symbol)  # "US..SPX" → "^SPX"
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5,
            )
            resp.raise_for_status()
            price = float(resp.json()["chart"]["result"][0]["meta"].get("regularMarketPrice") or 0)
            if price > 0:
                pct = atr / price
                log.info(f"[SymbolATR%] {symbol}: ATR={atr:.2f} / price={price:.2f} = {pct:.4f} ({pct*100:.2f}%)")
                return round(pct, 6)
        except Exception as e:
            log.warning(f"[SymbolATR%] {symbol} price fetch error: {e}")

        return None

    def get_option_chain_with_greeks(self, expiry: str, opt_type: str,
                                     center_strike: Optional[float] = None) -> list:
        """指定満期・方向のオプションチェーン（delta・bid/ask含む）を取得する。

        [P0 BUG修正 2026/04/17]
        center_strike を指定すると、チェーン全体をstrike順に center_strike に近い
        200件に絞ってsnapshotする。指定しない場合は従来通り先頭200件（チェーンが
        strike順にソートされている前提で低strike側200件）を使う。

        SPXW等の大型チェーンではstrike数が200を超えるため、center_strikeを指定
        しないと現在価格周辺のstrikeがsnapshot範囲外になるバグがあった。
        """
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return []
        futu_opt_type = ft.OptionType.PUT if opt_type == "PUT" else ft.OptionType.CALL
        ret, chain_df = self.quote_ctx.get_option_chain(
            self.underlying_code, start=expiry, end=expiry, option_type=futu_opt_type)
        if ret != RET_OK or chain_df.empty:
            log.warning(f"get_option_chain failed {self.underlying_code} {expiry} {opt_type}")
            return []
        codes = chain_df["code"].tolist()
        if not codes:
            return []

        # [P0修正] center_strike指定時は中心に近い200件に絞る
        if center_strike is not None and "strike_price" in chain_df.columns:
            try:
                df_sorted = chain_df.assign(
                    _dist=(chain_df["strike_price"].astype(float) - float(center_strike)).abs()
                ).sort_values("_dist")
                codes = df_sorted["code"].tolist()[:200]
                if codes:
                    log.debug(
                        f"[Chain] center={center_strike:.1f} underlying={self.underlying_code} "
                        f"selected {len(codes)} strikes within "
                        f"±{abs(float(df_sorted.iloc[min(len(df_sorted)-1, 199)]['strike_price']) - float(center_strike)):.1f}"
                    )
            except Exception as e:
                log.debug(f"[Chain] center_strike sort failed: {e} → fallback to first 200")
                codes = codes[:200]
        else:
            codes = codes[:200]

        ret2, snap = self.quote_ctx.get_market_snapshot(codes)
        if ret2 != RET_OK or snap.empty:
            log.warning("option chain snapshot failed")
            return []
        chain_dict = chain_df.set_index("code").to_dict("index")
        result = []
        for _, row in snap.iterrows():
            code = row.get("code", "")
            ci   = chain_dict.get(code, {})
            _strike = float(row.get("option_strike_price", ci.get("strike_price", 0)))
            # [BUG修正 2026-04-12] center_strike±20%範囲外のstrikeは別銘柄チェーン混入とみなし除外
            # 例: mkt.underlying_codeをUS..SPXに切替中にSPY ATM(710)でchainを取得すると
            # SPXW(5400系)strikeが混入しstrike不整合Pushoverが爆発するバグを防ぐ
            if center_strike is not None and center_strike > 0 and _strike > 0:
                _dev = abs(_strike - float(center_strike)) / float(center_strike)
                if _dev > 0.20:
                    log.debug(
                        f"[Chain] strike={_strike} center={center_strike:.1f} "
                        f"乖離={_dev*100:.1f}% > 20% → 除外 ({self.underlying_code})"
                    )
                    continue
            result.append({
                "code":         code,
                "strike_price": _strike,
                "delta":        abs(float(row.get("option_delta", 0))),
                "bid_price":    float(row.get("bid_price", 0)),
                "ask_price":    float(row.get("ask_price", 0)),
                "last_price":   float(row.get("last_price", 0)),
                "option_type":  opt_type,
            })
        return result

    def find_by_delta(self, chain: list, target_delta: float) -> Optional[dict]:
        if not chain:
            return None
        return min(chain, key=lambda o: abs(o.get("delta", 0) - target_delta))

    def find_by_strike(self, chain: list, target_strike: float) -> Optional[dict]:
        if not chain:
            return None
        return min(chain, key=lambda o: abs(o.get("strike_price", 0) - target_strike))

    def get_option_greeks(self, code: str) -> dict:
        """単一オプションのグリークスを取得（ガンマスキャルピング調査用）"""
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return {}
        try:
            ret, snap = self.quote_ctx.get_market_snapshot([code])
            if ret != RET_OK or snap.empty:
                return {}
            row = snap.iloc[0]
            return {
                "delta": float(row.get("option_delta", 0) or 0),
                "gamma": float(row.get("option_gamma", 0) or 0),
                "theta": float(row.get("option_theta", 0) or 0),
                "iv":    float(row.get("option_implied_volatility", 0) or 0),
                "last":  float(row.get("last_price", 0) or 0),
            }
        except Exception as e:
            log.warning(f"get_option_greeks: {e}")
        return {}

    def scan_option_volumes(self, expiry: str, spy_price: float) -> list:
        """0DTE全ストライクの出来高をスキャン（出来高スパイク検知調査用）"""
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return []
        results = []
        try:
            for opt_type in [ft.OptionType.CALL, ft.OptionType.PUT]:
                ret, chain = self.quote_ctx.get_option_chain(
                    self.underlying_code, start=expiry, end=expiry, option_type=opt_type)
                if ret != RET_OK or chain.empty:
                    continue
                # ATM付近±20ストライクに絞る（API負荷軽減）
                all_codes = chain["code"].tolist()
                strikes   = chain["strike_price"].astype(float).tolist()
                nearby    = [(c, s) for c, s in zip(all_codes, strikes)
                             if abs(s - spy_price) <= 20]
                if not nearby:
                    continue
                codes = [c for c, _ in nearby]
                ret2, snap = self.quote_ctx.get_market_snapshot(codes[:60])
                if ret2 != RET_OK or snap.empty:
                    continue
                for _, row in snap.iterrows():
                    results.append({
                        "type":          "CALL" if opt_type == ft.OptionType.CALL else "PUT",
                        "strike":        float(row.get("option_strike_price", 0) or 0),
                        "volume":        int(row.get("volume", 0) or 0),
                        "open_interest": int(row.get("option_open_interest", 0) or 0),
                        "delta":         float(row.get("option_delta", 0) or 0),
                    })
        except Exception as e:
            log.warning(f"scan_option_volumes: {e}")
        return results

    def is_alive(self) -> bool:
        """Quote contextの生存確認。OpenDとの接続状態のみを返す。

        get_global_state()でOpenDとの疎通を確認し、qot_logined='1'であればTrue。
        us_qot_right=NOによるSPYスナップショット失敗（権限不足）は切断ではないため、
        _vix_is_fallbackフラグはここでは判定しない。
        """
        if not FUTU_AVAILABLE or not self.quote_ctx:
            return False
        try:
            ret, data = self.quote_ctx.get_global_state()
            if ret != RET_OK:
                return False
            # qot_logined='1' でQuote serverへのログインを確認
            qot_logined = data.get("qot_logined", "0") if isinstance(data, dict) else "0"
            return str(qot_logined) == "1"
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════════════════════
# TradeEngine — order execution wrapper
# ══════════════════════════════════════════════════════════════════════════════
class TradeEngine:
    def __init__(self, paper: bool = False):
        self.paper      = paper
        self.trade_ctx  = None
        self.account_id = None
        self.trade_env  = (TrdEnv.SIMULATE if paper else TrdEnv.REAL) if FUTU_AVAILABLE else None
        self.unlock_ok  = False
        self._virtual_pos = VirtualPositionManager()  # dry-testモード用
        # 直近エントリー/エグジットの実約定価格（dealt_avg_price）キャッシュ
        # place_credit_spread / close_all_positions が書き込み、呼び出し元が参照する
        self._last_entry_fills: dict = {}  # {"sell": float|None, "buy": float|None}
        self._last_exit_fills: dict  = {}  # {order_id: float}  (code→avg_price)

    def connect(self) -> bool:
        if not FUTU_AVAILABLE:
            return False
        try:
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=OPEND_HOST, port=OPEND_PORT,
                security_firm=SecurityFirm.FUTUJP,
            )
            self._resolve_account()
            self._unlock()
            return True
        except Exception as e:
            log.error(f"TradeEngine connect: {e}")
            return False

    def close(self):
        if self.trade_ctx:
            try:
                self.trade_ctx.close()
            except Exception:
                pass

    def _resolve_account(self):
        ret, data = self.trade_ctx.get_acc_list()
        if ret != RET_OK or data.empty:
            log.warning("get_acc_list failed; account_id unresolved")
            return
        env = TrdEnv.SIMULATE if self.paper else TrdEnv.REAL
        rows = data[data["trd_env"] == env]
        if not rows.empty:
            self.account_id = str(rows.iloc[0]["acc_id"])
            log.info(f"Account resolved: {self.account_id} env={env}")

    def _unlock(self):
        if not TRADE_PASSWORD:
            return
        try:
            ret, data = self.trade_ctx.unlock_trade(password=TRADE_PASSWORD)
            if ret == RET_OK:
                self.unlock_ok = True
            elif "unlock button" in str(data) or "disabled in the GUI" in str(data):
                log.warning("unlock_trade disabled in GUI; assuming GUI-unlocked")
                self.unlock_ok = True
        except Exception as e:
            log.warning(f"unlock: {e}")

    def get_account_cash(self) -> float:
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return 10000.0  # dry-run
        try:
            ret, data = self.trade_ctx.accinfo_query(
                trd_env=self.trade_env, acc_id=int(self.account_id or 0))
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                return float(row.get("net_assets", 0) or row.get("cash", 0) or 10000)
        except Exception as e:
            log.warning(f"get_account_cash: {e}")
        return 10000.0

    def get_margin_usage_ratio(self) -> Optional[float]:
        """証拠金使用率を返す。futu accinfo_query の initial_margin / total_assets。

        Returns:
            float: 0.0〜1.0 (0%〜100%)。取得失敗時は None。
            dry-run/未接続時は 0.0（エントリー許可として扱う）。
        """
        if DRY_TEST or not FUTU_AVAILABLE or not self.trade_ctx:
            return 0.0
        try:
            ret, data = self.trade_ctx.accinfo_query(
                trd_env=self.trade_env, acc_id=int(self.account_id or 0))
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                initial_margin = float(row.get("initial_margin", 0) or 0)
                total_assets   = float(row.get("total_assets", 0) or
                                       row.get("net_assets", 0) or 0)
                if total_assets > 0:
                    ratio = initial_margin / total_assets
                    log.info(f"[MarginUsage] initial_margin={initial_margin:.2f} "
                             f"total_assets={total_assets:.2f} ratio={ratio:.3f}")
                    return round(ratio, 4)
        except Exception as e:
            log.warning(f"get_margin_usage_ratio: {e}")
        return None

    def check_margin_and_alert(self) -> bool:
        """証拠金使用率を確認してエントリー可否を返す。

        Returns:
            True: エントリー可（使用率70%未満）
            False: エントリー停止（使用率70%以上）

        90%超の場合はPushover緊急警告も送信する。
        """
        ratio = self.get_margin_usage_ratio()
        if ratio is None:
            return True  # 取得失敗はエントリー許可（サービス継続優先）
        if ratio >= MARGIN_USAGE_ALERT:
            log.error(f"[MarginUsage] 緊急: 使用率={ratio:.1%} >= {MARGIN_USAGE_ALERT:.0%} → 警告")
            pushover_alert(
                "証拠金危険水準",
                f"証拠金使用率={ratio:.1%}\n({MARGIN_USAGE_ALERT:.0%}超=危険)\n新規エントリー停止",
                priority=1,
            )
            return False
        if ratio >= MARGIN_USAGE_MAX_ENTRY:
            log.warning(f"[MarginUsage] エントリー停止: 使用率={ratio:.1%} >= {MARGIN_USAGE_MAX_ENTRY:.0%}")
            return False
        log.info(f"[MarginUsage] OK: 使用率={ratio:.1%} < {MARGIN_USAGE_MAX_ENTRY:.0%}")
        return True

    def _place_single_leg(self, code: str, side, qty: int, label: str,
                          init_price: Optional[float] = None,
                          use_limit: bool = False
                          ) -> tuple:
        """1本足を発注する。

        ENABLE_LIMIT_ENTRY=True かつ use_limit=True の場合は指値注文を試みる。
        - 初回: init_price（midプライス）で指値発注
        - LIMIT_ADJUST_INTERVAL秒ごとに約定確認
        - 未約定なら 0.01刻みで価格改善（売り脚は引き下げ、買い脚は引き上げ）
        - LIMIT_MAX_ADJUST_STEPS 回調整後も未約定 → 成行にフォールバック
        - init_price が None の場合は成行に直接フォールバック

        Returns:
            (order_id, fill_method)
              order_id  : str if success, None if failed
              fill_method: "limit" | "market_fallback" | "market" | "failed"
        """
        env = self.trade_env
        acc = int(self.account_id or 0)

        # ── 指値モード ────────────────────────────────────────────────────────
        if use_limit and init_price is not None:
            price = round(init_price, 2)
            order_id = None

            # 初回指値発注
            ret, data = self.trade_ctx.place_order(
                price=price, qty=qty, code=code,
                trd_side=side, order_type=OrderType.NORMAL,
                trd_env=env, acc_id=acc,
                time_in_force=TimeInForce.DAY,
            )
            if ret != RET_OK:
                log.warning(f"Leg {label} limit initial failed: {data}")
                # 初回発注自体が失敗 → 成行フォールバック
            else:
                order_id = data.iloc[0].get("order_id", "") if not data.empty else ""
                log.info(f"Leg {label} limit placed: code={code} qty={qty} "
                         f"price={price:.2f} order_id={order_id}")

                # 調整ループ
                for step in range(LIMIT_MAX_ADJUST_STEPS + 1):
                    time.sleep(LIMIT_ADJUST_INTERVAL)

                    # 約定確認
                    ret_q, order_data = self.trade_ctx.order_list_query(
                        order_id=str(order_id),
                        trd_env=env, acc_id=acc,
                    )
                    if ret_q == RET_OK and not order_data.empty:
                        status = order_data.iloc[0].get("order_status", "")
                        if status == "FILLED_ALL":
                            log.info(f"Leg {label} limit filled at step={step} "
                                     f"price={price:.2f}")
                            return order_id, "limit"

                    # 未約定 → 価格改善（最終ステップを超えたらループ抜けて成行）
                    if step < LIMIT_MAX_ADJUST_STEPS:
                        if side == TrdSide.SELL:
                            price = round(price - LIMIT_ADJUST_STEP, 2)
                        else:
                            price = round(price + LIMIT_ADJUST_STEP, 2)
                        ret_m, _ = self.trade_ctx.modify_order(
                            modify_order_op=ModifyOrderOp.NORMAL,
                            order_id=str(order_id),
                            qty=qty, price=price,
                            trd_env=env, acc_id=acc,
                        )
                        log.info(f"Leg {label} limit adjust step={step + 1} "
                                 f"new_price={price:.2f} ret={ret_m}")

                # 全調整後も未約定 → キャンセルして成行フォールバック
                if order_id:
                    self.trade_ctx.modify_order(
                        modify_order_op=ModifyOrderOp.CANCEL,
                        order_id=str(order_id),
                        qty=qty, price=price,
                        trd_env=env, acc_id=acc,
                    )
                    time.sleep(0.5)
                    log.warning(f"Leg {label} limit not filled after {LIMIT_MAX_ADJUST_STEPS} "
                                f"steps → market fallback")

        # ── 成行モード（指値無効 / init_price=None / 指値失敗フォールバック）─────
        fill_method = "market_fallback" if (use_limit and init_price is not None) else "market"
        for attempt in range(2):
            ret, data = self.trade_ctx.place_order(
                price=0, qty=qty, code=code,
                trd_side=side, order_type=OrderType.MARKET,
                trd_env=env, acc_id=acc,
                time_in_force=TimeInForce.DAY,
            )
            if ret == RET_OK:
                order_id = data.iloc[0].get("order_id", "") if not data.empty else ""
                log.info(f"Leg {label} {fill_method} OK: code={code} qty={qty} "
                         f"order_id={order_id}")
                return order_id, fill_method
            log.warning(f"Leg {label} {fill_method} attempt {attempt + 1} failed: {data}")
            if attempt == 0:
                time.sleep(1)
        log.error(f"Leg {label} failed after all attempts")
        return None, "failed"

    def _reverse_leg(self, code: str, original_side, qty: int, label: str):
        """original_side の反対方向で決済注文を出す。

        Args:
            original_side: TrdSide.SELL（SHORT脚）または TrdSide.BUY（LONG脚）を必須で渡す。
                           None を渡すと ValueError を送出する（サイレント二重化防止）。
        """
        if original_side is None:
            raise ValueError(
                f"_reverse_leg: original_side=None は禁止。"
                f"SHORT脚は TrdSide.SELL、LONG脚は TrdSide.BUY を明示的に渡すこと。"
                f" label={label}, code={code}"
            )
        reverse = TrdSide.BUY if original_side == TrdSide.SELL else TrdSide.SELL
        env = self.trade_env
        acc = int(self.account_id or 0)
        ret, data = self.trade_ctx.place_order(
            price=0, qty=qty, code=code,
            trd_side=reverse, order_type=OrderType.MARKET,
            trd_env=env, acc_id=acc, time_in_force=TimeInForce.DAY,
        )
        if ret == RET_OK:
            log.info(f"Unwind OK: {label}")

    def _confirm_fills(self, order_ids: list, direction: str,
                       use_limit: bool = False) -> dict:
        """発注後に全レグの約定を確認し、実約定価格を返す。（P0-1）

        order_list_query で FILLED_ALL を確認するまでポーリングする。
        - MARKET注文: 2秒間隔 × 最大15回（最大30秒）
        - LIMIT注文 (use_limit=True): 2秒間隔 × 最大30回（最大60秒）
          指値調整の時間を含むため余裕を2倍に設定。

        未約定が残る場合はログ＋Pushoverアラートを送る。

        Returns:
            dict: {order_id: dealt_avg_price (float|None)}
                  未約定・クエリ失敗の order_id は None を格納。
        """
        if not order_ids:
            return {}
        env = self.trade_env
        acc = int(self.account_id or 0)

        # ポーリング設定: MARKET=30秒(2s×15回), LIMIT=60秒(2s×30回)
        poll_interval = 2.0
        max_polls = 30 if use_limit else 15
        fills: dict = {}
        pending = list(order_ids)  # まだFILLED_ALL未確認の order_id

        for poll_idx in range(max_polls):
            time.sleep(poll_interval)
            still_pending = []
            for order_id in pending:
                ret, data = self.trade_ctx.order_list_query(
                    order_id=str(order_id),
                    trd_env=env, acc_id=acc,
                )
                if ret != RET_OK:
                    log.warning(f"約定確認クエリ失敗: order_id={order_id} ret={ret} "
                                f"(poll {poll_idx + 1}/{max_polls})")
                    still_pending.append(order_id)
                    fills[order_id] = None
                    continue
                if data.empty:
                    log.warning(f"約定確認: order_id={order_id} データなし "
                                f"(poll {poll_idx + 1}/{max_polls})")
                    still_pending.append(order_id)
                    fills[order_id] = None
                    continue
                status = data.iloc[0].get("order_status", "")
                avg_price = data.iloc[0].get("dealt_avg_price", None)
                try:
                    avg_price = float(avg_price) if avg_price is not None else None
                except (ValueError, TypeError):
                    avg_price = None
                if status == "FILLED_ALL":
                    fills[order_id] = avg_price
                    log.info(f"約定確認OK: order_id={order_id} status={status} "
                             f"dealt_avg_price={avg_price} "
                             f"(poll {poll_idx + 1}/{max_polls})")
                else:
                    fills.setdefault(order_id, None)
                    fills[order_id] = avg_price  # 部分約定価格も更新
                    still_pending.append(order_id)
                    log.debug(f"待機中: order_id={order_id} status={status} "
                              f"(poll {poll_idx + 1}/{max_polls})")
            pending = still_pending
            if not pending:
                break  # 全レグ約定済み

        unfilled = pending  # ポーリング上限到達時点で未約定のもの
        for order_id in unfilled:
            log.warning(f"未約定タイムアウト: order_id={order_id} "
                        f"(max_polls={max_polls} interval={poll_interval}s)")

        if unfilled:
            pushover_alert(
                f"SPY CS 約定未確認 [{direction}]",
                f"以下の注文が未約定（FILLED_ALL以外）:\n{unfilled}\n手動確認が必要です",
                priority=1,
            )
        return fills

    def place_credit_spread(self, sell_code: str, buy_code: str,
                            qty: int, direction: str,
                            sell_init_price: Optional[float] = None,
                            buy_init_price: Optional[float] = None,
                            vix: Optional[float] = None) -> bool:
        """クレジットスプレッドを発注する。

        ENABLE_LIMIT_ENTRY=True の場合:
          - VIX <= LIMIT_HIGH_VIX_THRESHOLD → 指値注文（midプライス起点）
          - VIX >  LIMIT_HIGH_VIX_THRESHOLD → 成行（急変時はスリッページ最小化優先）
          - sell_init_price / buy_init_price が None の場合は成行にフォールバック

        指値の結果（fill_method "limit" or "market_fallback" or "market"）は
        _last_entry_fills["fill_methods"] に記録される。
        """
        if DRY_TEST:
            # dry-testモード: 発注せずVirtualPositionManagerに仮想ポジションを追加
            # net_creditはオプションチェーンなしなので固定値$0.50を使用
            virtual_net_credit = 0.50
            log.info(f"[DRY-TEST] {direction} CS: SELL={sell_code} BUY={buy_code} qty={qty} "
                     f"credit=${virtual_net_credit:.2f}")
            self._virtual_pos.add_position(sell_code, qty, virtual_net_credit, "SHORT")
            self._virtual_pos.add_position(buy_code,  qty, virtual_net_credit * 0.3, "LONG")
            return True
        if not FUTU_AVAILABLE or not self.trade_ctx:
            log.info(f"[DRY-RUN] {direction} CS: SELL={sell_code} BUY={buy_code} qty={qty}")
            return True

        # 指値を使うかどうかを決定
        high_vix = (vix is not None and vix > LIMIT_HIGH_VIX_THRESHOLD)
        use_limit = (ENABLE_LIMIT_ENTRY
                     and not high_vix
                     and sell_init_price is not None
                     and buy_init_price is not None)
        if ENABLE_LIMIT_ENTRY and high_vix:
            log.info(f"[LimitEntry] VIX={vix:.1f} > {LIMIT_HIGH_VIX_THRESHOLD} → 成行モード")

        legs = [
            (sell_code, TrdSide.SELL, f"{direction}_sell", sell_init_price),
            (buy_code,  TrdSide.BUY,  f"{direction}_buy",  buy_init_price),
        ]
        placed = []
        order_ids = []
        fill_methods = []
        for code, side, label, init_price in legs:
            time.sleep(0.5)
            order_id, fill_method = self._place_single_leg(
                code, side, qty, label,
                init_price=init_price,
                use_limit=use_limit,
            )
            if order_id is not None:
                placed.append((code, side, label))
                order_ids.append(order_id)
                fill_methods.append(fill_method)
            else:
                # いずれかのレグが失敗 → 発注済みレグを反転して巻き戻す
                for p_code, p_side, p_label in reversed(placed):
                    self._reverse_leg(p_code, p_side, qty, p_label)
                return False
        log.info(f"{direction} CS placed: qty={qty} fill_methods={fill_methods}")
        # P0-1: 約定確認（失敗でもエントリー自体はTrueを返す＝ポジションは入っているはず）
        # order_ids[0]=sell, order_ids[1]=buy の順（legs定義順）
        # use_limit=True の場合はポーリング上限を2倍に延長（指値調整時間を含むため）
        fill_map = self._confirm_fills(order_ids, direction, use_limit=use_limit)
        sell_fill = fill_map.get(order_ids[0]) if len(order_ids) > 0 else None
        buy_fill  = fill_map.get(order_ids[1]) if len(order_ids) > 1 else None
        self._last_entry_fills = {
            "sell": sell_fill,
            "buy": buy_fill,
            "fill_methods": fill_methods,
            "sell_init_price": sell_init_price,
            "buy_init_price": buy_init_price,
        }
        log.info(f"Entry fills: sell_avg={sell_fill} buy_avg={buy_fill} "
                 f"methods={fill_methods}")
        return True

    def get_open_positions(self) -> list:
        if DRY_TEST:
            return self._virtual_pos.get_positions()
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return []
        ret, data = self.trade_ctx.position_list_query(
            trd_env=self.trade_env, acc_id=int(self.account_id or 0))
        return data.to_dict("records") if ret == RET_OK else []

    def is_alive(self) -> bool:
        """Trade contextの生存確認。get_acc_listで疎通テスト。"""
        if not FUTU_AVAILABLE or not self.trade_ctx:
            return False
        try:
            ret, _ = self.trade_ctx.get_acc_list()
            return ret == RET_OK
        except Exception:
            return False

    def cancel_all_open_orders(self, reason: str = "eod_sweep") -> int:
        """全未約定オーダーをキャンセル。15:55ET直前やshutdown時に呼ぶ。

        Returns: キャンセル成功件数
        """
        if DRY_TEST or not FUTU_AVAILABLE or not self.trade_ctx:
            log.info(f"[SweepCancel/{reason}] DRY_TEST or no trade_ctx → skip")
            return 0
        env = self.trade_env
        acc = int(self.account_id or 0)
        try:
            ret, df = self.trade_ctx.order_list_query(trd_env=env, acc_id=acc)
        except Exception as e:
            log.warning(f"[SweepCancel/{reason}] order_list_query 例外: {e}")
            return 0
        if ret != RET_OK or df is None or df.empty:
            log.info(f"[SweepCancel/{reason}] no open orders")
            return 0
        active_status = {"SUBMITTED", "WAITING_SUBMIT", "FILLED_PART", "SUBMITTING"}
        canceled = 0
        for _, row in df.iterrows():
            status = str(row.get("order_status", ""))
            if status not in active_status:
                continue
            oid = row.get("order_id")
            if not oid:
                continue
            try:
                self.trade_ctx.modify_order(
                    modify_order_op=ModifyOrderOp.CANCEL,
                    order_id=oid, price=0, qty=0,
                    trd_env=env, acc_id=acc,
                )
                canceled += 1
                log.info(f"[SweepCancel/{reason}] order_id={oid} code={row.get('code')} canceled")
            except Exception as e:
                log.warning(f"[SweepCancel/{reason}] cancel失敗 oid={oid}: {e}")
        if canceled > 0:
            try:
                pushover_alert(f"[Atlas/SweepCancel] {reason}", f"{canceled}件の未約定オーダーをキャンセル")
            except Exception:
                pass
        return canceled

    def close_all_positions(self, reason: str = "force_close") -> bool:
        """全ポジションを決済し、約定を確認する。

        Returns True if all positions were successfully closed.
        """
        if DRY_TEST:
            positions = self._virtual_pos.get_positions()
            if not positions:
                log.info(f"[DRY-TEST] No virtual positions to close ({reason})")
                return True
            # P&L概算: unrealized_plの合計
            total_pnl = sum(p.get("unrealized_pl", 0.0) for p in positions)
            log.info(f"[DRY-TEST] close_all_positions({reason}): {len(positions)} positions "
                     f"概算P&L=${total_pnl:.2f}")
            self._virtual_pos.remove_all()
            return True

        positions = self.get_open_positions()
        if not positions:
            log.info(f"No positions to close ({reason})")
            return True

        # 期限切れポジションを除外し、1回だけPushover通知する
        now_et = datetime.datetime.now(ET)
        today_str = now_et.strftime("%Y-%m-%d")
        _expired_warned = set()
        active_positions = []
        for pos in positions:
            code = pos.get("code", "")
            if _option_is_expired(code, today_str):
                if code not in _expired_warned:
                    log.warning(f"期限切れポジション残存: {code} → 決済注文スキップ")
                    _expired_warned.add(code)
                continue
            active_positions.append(pos)
        if _expired_warned:
            # 自動クリーンアップ(#8)が処理するため緊急通知不要
            log.info(f"期限切れポジション検出（自動処理予定）: {list(_expired_warned)}")
        positions = active_positions

        if not positions:
            log.info(f"No active (non-expired) positions to close ({reason})")
            return True

        log.info(f"Closing {len(positions)} positions ({reason})")
        env = self.trade_env
        acc = int(self.account_id or 0)

        # P1-1: Credit SpreadのSELL脚（SHORT）を先に決済してレグリスクを閉じる
        # SHORT脚を買い戻すことでデルタリスクをまず消す（優秀なトレーダーの基本）
        short_positions = [p for p in positions if p.get("position_side") == "SHORT"]
        long_positions  = [p for p in positions if p.get("position_side") != "SHORT"]
        ordered_positions = short_positions + long_positions
        if short_positions:
            log.info(f"Close order sequence: {len(short_positions)} SHORT first, "
                     f"then {len(long_positions)} LONG (leg risk control)")

        failed = []
        close_order_ids: list = []  # exit約定価格取得用
        close_order_codes: dict = {}  # order_id → {"code": str, "position_side": str}
        for pos in ordered_positions:
            code = pos.get("code", "")
            qty  = abs(int(pos.get("qty", 0)))
            if qty == 0:
                continue
            position_side = pos.get("position_side", "LONG")
            side = TrdSide.BUY if position_side == "SHORT" else TrdSide.SELL
            ret, data = self.trade_ctx.place_order(
                price=0, qty=qty, code=code,
                trd_side=side, order_type=OrderType.MARKET,
                trd_env=env, acc_id=acc,
                time_in_force=TimeInForce.DAY,
            )
            if ret != RET_OK:
                log.error(f"Close order FAILED for {code} x{qty}: {data}")
                failed.append(code)
            else:
                order_id = data.iloc[0].get("order_id", "?") if not data.empty else "?"
                log.info(f"Close order sent: {code} x{qty} "
                         f"side={position_side} order_id={order_id}")
                close_order_ids.append(order_id)
                close_order_codes[order_id] = {"code": code, "position_side": position_side}

        # 約定確認: 3秒待ってポジションを再チェック（qty=0のゾンビは除外）
        import time
        time.sleep(3)
        remaining_raw = self.get_open_positions()
        remaining = [
            p for p in remaining_raw
            if abs(int(float(p.get("qty", 0)))) > 0
        ]
        if remaining:
            remaining_codes = [p.get("code", "?") for p in remaining]
            # 0DTE失効ポジションかどうか判定（日付が今日より前なら失効済み）
            now_et = datetime.datetime.now(ET)
            today_str = now_et.strftime("%y%m%d")
            _truly_open = [c for c in remaining_codes if today_str in c]
            _expired = [c for c in remaining_codes if today_str not in c]
            if _expired and not _truly_open:
                # 全て失効済み → 正常。通知不要（自動クリーンアップが処理する）
                log.info(f"Expired 0DTE positions after close ({reason}): {_expired} — auto-cleanup will handle")
                return True
            elif _truly_open:
                log.error(f"POSITIONS STILL OPEN after close ({reason}): {_truly_open}")
                pushover_alert(
                    "決済確認中",
                    f"{reason}で決済指示 → 約定確認中({len(_truly_open)}件)\n"
                    f"次のループで再確認します",
                    priority=0,
                )
                return False
            else:
                log.warning(f"Unknown position codes after close ({reason}): {remaining_codes}")
                return False

        # exit実約定価格を収集して _last_exit_fills に格納
        # {code: {"price": float|None, "position_side": str}} の形式で保存
        if close_order_ids:
            fill_map = self._confirm_fills(close_order_ids, f"close_{reason}")
            self._last_exit_fills = {}
            for oid, price in fill_map.items():
                info = close_order_codes.get(oid, {})
                c = info.get("code", oid) if isinstance(info, dict) else oid
                ps = info.get("position_side", "LONG") if isinstance(info, dict) else "LONG"
                self._last_exit_fills[c] = {"price": price, "position_side": ps}
            log.info(f"Exit fills ({reason}): {self._last_exit_fills}")
        else:
            self._last_exit_fills = {}

        log.info(f"All positions closed successfully ({reason})")
        return True


# ══════════════════════════════════════════════════════════════════════════════
# SMADirectionDetector — 20SMA direction for standard entry
# ══════════════════════════════════════════════════════════════════════════════
class SMADirectionDetector:
    """
    SPY Open > SMA20 → PUT CS (bull bias, sell puts below market)
    SPY Open < SMA20 → CALL CS (bear bias, sell calls above market)
    """
    def __init__(self, quote_ctx):
        self.quote_ctx = quote_ctx

    def get_direction(self, spy_open: Optional[float] = None) -> Optional[str]:
        sma20 = self._get_sma20()
        if sma20 is None:
            log.warning("SMA20 unavailable → skip direction")
            return None
        if spy_open is None or spy_open <= 0:
            # dry-testモードまたはquote_ctxがない場合はスナップショットをスキップ
            if self.quote_ctx is not None and not DRY_TEST:
                try:
                    ret, snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                    if ret == RET_OK and not snap.empty:
                        spy_open = float(snap.iloc[0].get("open_price", 0) or
                                         snap.iloc[0].get("last_price", 0))
                except Exception:
                    pass
        if not spy_open:
            return None
        direction = "PUT" if spy_open > sma20 else "CALL"
        log.info(f"SMA direction: SPY_open={spy_open:.2f} SMA20={sma20:.2f} → {direction}")
        return direction

    def _get_sma20(self) -> Optional[float]:
        # Cache: reuse within same day
        try:
            if SMA_CACHE_FILE.exists():
                data = json.loads(SMA_CACHE_FILE.read_text())
                today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
                if data.get("date") == today:
                    return float(data["sma20"])
        except Exception:
            pass

        if DRY_TEST:
            # dry-testモード: Yahoo実データから直接SMA計算（futu不要）
            # 後続のYahooフォールバックコードに処理を委譲するためNoneをセットして続行
            pass
        elif not FUTU_AVAILABLE or not self.quote_ctx:
            return 560.0  # dry-run (futuなし・非dry-testの固定値)

        sma20 = None

        # 1st: futu
        try:
            end_date   = datetime.datetime.now(ET).date()
            start_date = end_date - datetime.timedelta(days=40)
            ret, kline, _ = self.quote_ctx.request_history_kline(
                self.underlying_code,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                ktype=ft.KLType.K_DAY,
                max_count=30,
            )
            if ret == RET_OK and not kline.empty:
                closes = kline["close"].astype(float).tolist()
                if len(closes) >= SMA_PERIOD:
                    sma20 = sum(closes[-SMA_PERIOD:]) / SMA_PERIOD
        except Exception as e:
            log.warning(f"SMA20 futu: {e}")

        # 2nd: Yahoo Finance fallback
        if sma20 is None:
            try:
                end_ts = int(time.time())
                start_ts = end_ts - 45 * 86400
                resp = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/SPY",
                    params={"period1": start_ts, "period2": end_ts,
                            "interval": "1d"},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10,
                )
                data = resp.json()
                closes = [float(c) for c in data["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
                if len(closes) >= SMA_PERIOD:
                    sma20 = sum(closes[-SMA_PERIOD:]) / SMA_PERIOD
                    log.info(f"SMA20 via Yahoo Finance: {sma20:.2f}")
            except Exception as e:
                log.warning(f"SMA20 Yahoo fallback: {e}")

        # 3rd: Finnhub fallback
        if sma20 is None:
            try:
                end_ts = int(time.time())
                start_ts = end_ts - 45 * 86400
                resp = requests.get(
                    "https://finnhub.io/api/v1/stock/candle",
                    params={"symbol": "SPY", "resolution": "D",
                            "from": start_ts, "to": end_ts,
                            "token": FINNHUB_API_KEY},
                    timeout=10,
                )
                data = resp.json()
                closes = [float(c) for c in data.get("c", [])]
                if len(closes) >= SMA_PERIOD:
                    sma20 = sum(closes[-SMA_PERIOD:]) / SMA_PERIOD
                    log.info(f"SMA20 via Finnhub: {sma20:.2f}")
            except Exception as e:
                log.warning(f"SMA20 Finnhub fallback: {e}")

        if sma20 is not None:
            SMA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SMA_CACHE_FILE.write_text(json.dumps({
                "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                "sma20": sma20,
            }))
        return sma20


# ══════════════════════════════════════════════════════════════════════════════
# DemoLogger — records parameter variants without placing orders
# Runs at two windows: 10:30 ET (standard variants) and 13:00 ET (ORF variants)
# ══════════════════════════════════════════════════════════════════════════════

# Standard entry variants (simulated at 10:30 ET)
DEMO_STANDARD_VARIANTS = [
    {"label": "Std_d020_w10", "delta": 0.20, "width": 10},
    {"label": "Std_d025_w10", "delta": 0.25, "width": 10},  # current setting
    {"label": "Std_d030_w10", "delta": 0.30, "width": 10},
    {"label": "Std_d025_w20", "delta": 0.25, "width": 20},
]

# ORF entry variants (simulated at 13:00 ET, only when orf_triggered=True)
DEMO_ORF_VARIANTS = [
    {"label": "ORF_d015_w10", "delta": 0.15, "width": 10},
    {"label": "ORF_d020_w10", "delta": 0.20, "width": 10},  # current ORF setting
    {"label": "ORF_d020_w20", "delta": 0.20, "width": 20},
]


class DemoLogger:
    def __init__(self, quote_ctx, underlying_code: str = UNDERLYING_CODE):
        self.quote_ctx = quote_ctx
        self.underlying_code = underlying_code

    def run(self, tactic: str, direction: str, expiry: str, vix: float,
            ivr: Optional[float] = None) -> list:
        """
        tactic: "standard" or "orf"
        direction: "PUT" or "CALL"
        """
        variants = DEMO_STANDARD_VARIANTS if tactic == "standard" else DEMO_ORF_VARIANTS
        log.info(f"[DEMO-{tactic.upper()}] direction={direction} expiry={expiry} "
                 f"VIX={vix:.1f} IVR={ivr:.0f if ivr else 'N/A'}")
        results = []
        for v in variants:
            r = self._sim(v, direction, expiry)
            r.update({
                "tactic": tactic, "vix": vix, "ivr": ivr,
                "direction": direction, "expiry": expiry,
                "ts": datetime.datetime.now(ET).isoformat(),
            })
            results.append(r)
            log.info(f"[DEMO] {v['label']}: sell={r.get('sell_strike','N/A')} "
                     f"buy={r.get('buy_strike','N/A')} credit=${r.get('net_credit',0):.2f} "
                     f"delta_actual={r.get('delta_actual','N/A')}")
        self._write_log(results)
        return results

    def _sim(self, variant: dict, direction: str, expiry: str) -> dict:
        if not FUTU_AVAILABLE or not self.quote_ctx:
            sell_strike = 560.0 if direction == "PUT" else 580.0
            buy_strike  = sell_strike - variant["width"] if direction == "PUT" \
                          else sell_strike + variant["width"]
            return {
                "label":       variant["label"],
                "sell_strike": sell_strike,
                "buy_strike":  buy_strike,
                "net_credit":  round(variant["delta"] * variant["width"] * 0.4, 2),
                "delta_actual": variant["delta"],
            }
        try:
            futu_opt = ft.OptionType.PUT if direction == "PUT" else ft.OptionType.CALL
            ret, chain_df = self.quote_ctx.get_option_chain(
                self.underlying_code, start=expiry, end=expiry, option_type=futu_opt)
            if ret != RET_OK or chain_df.empty:
                return {"label": variant["label"], "error": "chain_failed"}
            # [ChainGuard] center_strike±20%フィルタで銘柄混入を除外
            spy_price_ref = getattr(self, "_cached_spy_price", None) or 0
            if spy_price_ref > 0:
                chain_df = chain_df.copy()
                chain_df["strike_price"] = chain_df["strike_price"].astype(float)
                chain_df = chain_df[
                    (chain_df["strike_price"] - spy_price_ref).abs() / spy_price_ref <= 0.20
                ]
                if chain_df.empty:
                    log.warning(f"[grid_search/ChainGuard] {self.underlying_code} "
                                f"center={spy_price_ref:.1f} 全strike±20%外→銘柄混入疑い")
                    return {"label": variant["label"], "error": "chain_guard_empty"}
            codes = chain_df["code"].tolist()
            ret2, snap = self.quote_ctx.get_market_snapshot(codes[:200])
            if ret2 != RET_OK or snap.empty:
                return {"label": variant["label"], "error": "snapshot_failed"}
            chain_dict = chain_df.set_index("code").to_dict("index")
            chain = []
            for _, row in snap.iterrows():
                code = row.get("code", "")
                ci   = chain_dict.get(code, {})
                chain.append({
                    "strike_price": float(row.get("option_strike_price",
                                                  ci.get("strike_price", 0))),
                    "delta":        abs(float(row.get("option_delta", 0))),
                    "bid_price":    float(row.get("bid_price", 0)),
                    "ask_price":    float(row.get("ask_price", 0)),
                })
            if not chain:
                return {"label": variant["label"], "error": "empty_chain"}
            sell_opt = min(chain, key=lambda o: abs(o["delta"] - variant["delta"]))
            sell_strike = sell_opt["strike_price"]
            buy_target  = sell_strike - variant["width"] if direction == "PUT" \
                          else sell_strike + variant["width"]
            buy_opt     = min(chain, key=lambda o: abs(o["strike_price"] - buy_target))
            return {
                "label":        variant["label"],
                "sell_strike":  sell_strike,
                "buy_strike":   buy_opt["strike_price"],
                "net_credit":   round(sell_opt["bid_price"] - buy_opt["ask_price"], 2),
                "delta_actual": sell_opt["delta"],
            }
        except Exception as e:
            return {"label": variant["label"], "error": str(e)[:80]}

    def _write_log(self, results: list):
        try:
            DEMO_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DEMO_LOG_FILE, "a") as f:
                f.write(json.dumps(results) + "\n")
        except Exception as e:
            log.warning(f"demo log write: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# EntryBuilder — construct and place a credit spread
# ══════════════════════════════════════════════════════════════════════════════
class EntryBuilder:
    """Encapsulates chain fetch + order placement for one CS entry."""

    def __init__(self, mkt: MarketData, eng: TradeEngine):
        self.mkt = mkt
        self.eng = eng

    def _build_virtual_chain(self, expiry: str, direction: str,
                              params: dict) -> tuple:
        """dry-testモード用: SPY現在価格から仮想オプションチェーンを構築する。
        Returns (sell_opt, buy_opt, net_credit) dict tuple.
        """
        spy_price = self.mkt.get_spy_current() or 562.5
        target_delta = params["delta"]
        spread_width = params["width"]
        # deltaからATMオフセットを概算: delta=0.25 → 約1σ = spy_price * 0.01 * vix / 16
        # 簡易: ATMから delta/0.5 * spread_widthのオフセット
        otm_offset = spread_width * (1.0 - target_delta * 2)
        if direction == "PUT":
            sell_strike = round(spy_price - otm_offset, 0)
            buy_strike  = sell_strike - spread_width
        else:
            sell_strike = round(spy_price + otm_offset, 0)
            buy_strike  = sell_strike + spread_width

        # 仮想コードを生成 (例: VIRTUAL_PUT_560_550_20260414)
        sell_code = f"VIRTUAL_{direction}_SELL_{sell_strike:.0f}_{expiry}"
        buy_code  = f"VIRTUAL_{direction}_BUY_{buy_strike:.0f}_{expiry}"
        net_credit = 0.50  # dry-testモードの固定クレジット

        sell_opt = {
            "code": sell_code, "strike_price": sell_strike,
            "delta": target_delta, "bid_price": net_credit + 0.05,
            "ask_price": net_credit, "option_type": direction,
        }
        buy_opt = {
            "code": buy_code, "strike_price": buy_strike,
            "delta": target_delta * 0.5, "bid_price": 0.05,
            "ask_price": 0.05, "option_type": direction,
        }
        return sell_opt, buy_opt, net_credit

    def place(self, expiry: str, qty: int, params: dict,
              vix: float, direction: str, tactic: str,
              bot: Optional['SPYCreditSpreadBot'] = None,
              signal_id: Optional[str] = None,
              key_levels: Optional[dict] = None) -> bool:
        target_delta = params["delta"]

        # ── ATRベース動的width計算 ──────────────────────────────────────────
        # symbol_params.json が存在する場合: ATR × width_atr_mult で算出
        # フォールバック: params["width"] (既存の固定値 10)
        _symbol = self.mkt.underlying_code if self.mkt else UNDERLYING_CODE
        if _SYMBOL_PARAMS:
            _atr = self.mkt.get_symbol_atr(_symbol, period=14) if self.mkt else None
            spread_width = calc_dynamic_width(_symbol, _atr)
        else:
            spread_width = params.get("width", 10)

        log.info(f"[{tactic}] {direction} CS: expiry={expiry} qty={qty} "
                 f"VIX={vix:.1f} delta={target_delta} width={spread_width}")

        if DRY_TEST:
            # dry-testモード: 仮想チェーンを使用（futuオプションチェーン不要）
            sell_opt, buy_opt, _virtual_credit = self._build_virtual_chain(expiry, direction, params)
            sell_strike = sell_opt["strike_price"]
            log.info(f"[DRY-TEST] 仮想CS: SELL {sell_strike:.0f} / "
                     f"BUY {buy_opt['strike_price']:.0f} credit=${_virtual_credit:.2f}")
        else:
            # [P0 BUG修正] center_strike=現在価格 でチェーンを周辺に絞る
            _center = self.mkt.get_spy_current() if self.mkt else None
            chain = self.mkt.get_option_chain_with_greeks(
                expiry, direction, center_strike=float(_center) if _center else None)
            if not chain:
                pushover("SPY CS", f"チェーン取得失敗 {direction} {expiry} ({tactic})")
                return False

            sell_opt = self.mkt.find_by_delta(chain, target_delta)
            if not sell_opt:
                pushover("SPY CS", f"SELL脚見つからず {direction} ({tactic})")
                return False

            sell_strike = sell_opt["strike_price"]

            # [P0 BUG検証] sell_strikeが現在価格から±20%超乖離なら異常
            if _center and _center > 0:
                _sell_dev = abs(sell_strike - _center) / _center
                if _sell_dev > 0.20:
                    log.error(
                        f"[{tactic}] SELL strike整合性NG: {sell_strike} vs "
                        f"underlying={_center:.2f} 乖離={_sell_dev*100:.1f}% "
                        f"symbol={self.mkt.underlying_code}"
                    )
                    # priority=0: 修正1(chain±20%フィルタ)通過後の残余バグのみ到達
                    # MassVerify多銘柄で爆発しないよう静音ログのみ（priority=1→0）
                    pushover_alert(
                        f"[{tactic}] CS SELL strike不整合",
                        f"sell={sell_strike} underlying={_center:.2f}",
                        priority=0,
                    )
                    return False

            buy_target  = sell_strike - spread_width if direction == "PUT" \
                          else sell_strike + spread_width
            buy_opt = self.mkt.find_by_strike(chain, buy_target)
            if not buy_opt:
                pushover("SPY CS", f"BUY脚見つからず {direction} ({tactic})")
                return False

        # Key Level proximity check (G1+G2+G13)
        if ENABLE_KEY_LEVELS and key_levels is not None:
            all_kl = key_levels.get("all_levels", [])
            close_kl = [kl for kl in all_kl if abs(sell_strike - kl) < KEY_LEVEL_PROXIMITY]
            if close_kl:
                msg = (f"Key Level近接スキップ: SELL {sell_strike:.1f} が {close_kl} の "
                       f"{KEY_LEVEL_PROXIMITY}ドル以内 ({tactic})")
                log.warning(f"[KeyLevel] {msg}")
                pushover("SPY CS エントリー中止", msg)
                return False

        net_credit = round(sell_opt.get("bid_price", 0) - buy_opt.get("ask_price", 0), 2)
        log.info(f"{direction} CS: SELL {sell_strike:.1f} / BUY {buy_opt['strike_price']:.1f} "
                 f"width={spread_width} credit=${net_credit:.2f}")

        # P0-3: net_credit <= 0 はBid-Askスプレッドが広すぎてcreditが取れない → 中止
        if net_credit <= 0:
            msg = (f"net_credit={net_credit:.2f} <= 0: Bid-Askスプレッド過大でcreditゼロ。"
                   f"SELL {sell_strike:.1f} bid={sell_opt.get('bid_price', 0):.2f} / "
                   f"BUY {buy_opt['strike_price']:.1f} ask={buy_opt.get('ask_price', 0):.2f} "
                   f"({tactic})")
            log.warning(msg)
            pushover("SPY CS エントリー中止", msg)
            return False

        # P0-4: Bid/Askスプレッド幅チェック
        # slippage_est = (sell_opt ask-bid)/2 + (buy_opt ask-bid)/2
        # slippage_est / net_credit > 0.33 → スリッページがcreditの33%超 → エントリー中止
        sell_bid = sell_opt.get("bid_price", 0)
        sell_ask = sell_opt.get("ask_price", 0)
        buy_bid  = buy_opt.get("bid_price", 0)
        buy_ask  = buy_opt.get("ask_price", 0)
        slippage_est = (sell_ask - sell_bid) / 2 + (buy_ask - buy_bid) / 2
        slippage_ratio = slippage_est / net_credit if net_credit > 0 else float("inf")
        log.info(f"Bid/Ask slippage_est={slippage_est:.3f} ratio={slippage_ratio:.3f} "
                 f"(sell_spread={sell_ask - sell_bid:.3f} buy_spread={buy_ask - buy_bid:.3f})")
        if slippage_ratio > SPREAD_COST_RATIO_MAX:
            log.warning(
                f"[Entry] Bid/Ask spread too wide: slippage={slippage_est:.3f} / "
                f"credit={net_credit:.3f} = {slippage_ratio:.1%} > {SPREAD_COST_RATIO_MAX:.0%}"
            )
            msg = (f"Bid/Askスプレッド過大でスキップ: slippage_est=${slippage_est:.3f} "
                   f"({slippage_ratio:.1%} of credit ${net_credit:.2f})\n"
                   f"SELL {sell_strike:.1f} bid={sell_bid:.2f}/ask={sell_ask:.2f} / "
                   f"BUY {buy_opt['strike_price']:.1f} bid={buy_bid:.2f}/ask={buy_ask:.2f} "
                   f"({tactic})")
            log.warning(msg)
            pushover("SPY CS エントリー中止", msg)
            return False

        # midプライスを算出（指値注文の初回価格）
        sell_mid = round((sell_bid + sell_ask) / 2, 2)
        buy_mid  = round((buy_bid  + buy_ask)  / 2, 2)

        ok = self.eng.place_credit_spread(
            sell_code=sell_opt["code"],
            buy_code=buy_opt["code"],
            qty=qty, direction=direction,
            sell_init_price=sell_mid,
            buy_init_price=buy_mid,
            vix=vix,
        )

        now_et   = datetime.datetime.now(ET)
        time_key = f"{now_et.hour}:{now_et.minute:02d}"
        if ok:
            # P1-4: trade_idを生成してentry/exitを紐付ける
            trade_id = str(uuid.uuid4())
            if bot is not None:
                bot._current_trade_id = trade_id
                bot._current_signal_id = signal_id  # 本番/ペーパー横断照合用
                bot._last_entry_ts = datetime.datetime.now(ET)  # バグ1: GammaEarlyExit最低保持時間用
            pushover(
                f"SPY CS [{tactic}]",
                f"0DTE {direction} エントリー {time_key}ET\n"
                f"SELL {sell_strike:.1f} / BUY {buy_opt['strike_price']:.1f} "
                f"(w={spread_width} δ={target_delta})\n"
                f"{qty}枚 credit=${net_credit:.2f}"
            )
            # 実約定価格を取得 (DRY_TEST/DRY_RUNでは None)
            _ef = self.eng._last_entry_fills if hasattr(self.eng, "_last_entry_fills") else {}
            fill_sell = _ef.get("sell")
            fill_buy  = _ef.get("buy")
            _fill_methods = _ef.get("fill_methods", [])
            _sell_init = _ef.get("sell_init_price")
            _buy_init  = _ef.get("buy_init_price")
            actual_net_credit = (
                round(fill_sell - fill_buy, 4)
                if fill_sell is not None and fill_buy is not None
                else None
            )
            # sell_fill > theo_sell_bid: slippage>0 = worseで受け取れた, <0 = こちらに有利
            slippage_entry = (
                round((sell_opt.get("bid_price", 0) - fill_sell) +
                      (fill_buy - buy_opt.get("ask_price", 0)), 4)
                if fill_sell is not None and fill_buy is not None
                else None
            )
            # 指値起点との乖離 (init vs fill)
            slippage_per_leg_sell = (
                round(_sell_init - fill_sell, 4)
                if _sell_init is not None and fill_sell is not None else None
            )
            slippage_per_leg_buy = (
                round(fill_buy - _buy_init, 4)
                if _buy_init is not None and fill_buy is not None else None
            )
            fill_method_str = "/".join(_fill_methods) if _fill_methods else "market"
            append_pnl_entry({
                "event": "entry", "tactic": tactic, "expiry": expiry,
                "direction": direction, "sell_strike": sell_strike,
                "buy_strike": buy_opt["strike_price"],
                "qty": qty, "net_credit": net_credit,
                "fill_price_sell": fill_sell,
                "fill_price_buy": fill_buy,
                "actual_net_credit": actual_net_credit,
                "slippage": slippage_entry,
                "init_price_sell": _sell_init,
                "init_price_buy": _buy_init,
                "slippage_per_leg_sell": slippage_per_leg_sell,
                "slippage_per_leg_buy": slippage_per_leg_buy,
                "fill_method": fill_method_str,
                "vix": round(vix, 2),
                "delta_actual": round(sell_opt.get("delta", 0), 4),
                "trade_id": trade_id,
                "signal_id": signal_id,
            })
            append_monthly_csv({
                "expiry": expiry, "direction": direction,
                "sell_strike": sell_strike, "buy_strike": buy_opt["strike_price"],
                "qty": qty, "net_credit": net_credit,
                "result": "entered", "tactic": tactic,
            })
        else:
            pushover("SPY CS", f"エントリー失敗 {direction} {expiry} {time_key}ET ({tactic})")

        return ok

    def place_iron_condor(self, expiry: str, qty: int, params: dict,
                          vix: float, tactic: str,
                          bot: Optional['SPYCreditSpreadBot'] = None,
                          signal_id: Optional[str] = None,
                          key_levels: Optional[dict] = None) -> bool:
        """Iron Condor エントリー: PUT CS + CALL CS の2スプレッドを発注する。

        PUT CS は既存の place() を利用。CALL CS は direction="CALL" で同じく place() を利用。
        signal_id は両スプレッドで共有してグループ紐付けを行う。
        trade_id は PUT CS 側の生成値を IC 全体の共通 trade_id として保持する。
        check_exits の spread_groups は UNDERLYING_YYMMDD でグループ化するため、
        PUT/CALL 両脚が同一グループに入り net P&L で PT/SL 判定される。
        """
        now_et   = datetime.datetime.now(ET)
        time_key = f"{now_et.hour}:{now_et.minute:02d}"

        log.info(f"[{tactic}] Iron Condor: expiry={expiry} qty={qty} VIX={vix:.1f}")

        # ── PUT CS ────────────────────────────────────────────────────────────
        put_ok = self.place(
            expiry=expiry, qty=qty, params=params, vix=vix,
            direction="PUT", tactic=tactic, bot=bot,
            signal_id=signal_id, key_levels=key_levels,
        )
        if not put_ok:
            pushover("SPY IC", f"IC PUT脚エントリー失敗 {time_key}ET ({tactic}) → ICキャンセル")
            return False

        # PUT 側で生成された trade_id / signal_id を IC 全体の共通値として保持
        ic_trade_id  = bot._current_trade_id  if bot is not None else None
        ic_signal_id = bot._current_signal_id if bot is not None else signal_id

        # CALL CS 用パラメータ: delta=0.16（仕様指定） / width は PUT CS と同じ
        call_params          = dict(params)
        call_params["delta"] = 0.16

        # ── CALL CS ───────────────────────────────────────────────────────────
        call_ok = self.place(
            expiry=expiry, qty=qty, params=call_params, vix=vix,
            direction="CALL", tactic=tactic, bot=bot,
            signal_id=ic_signal_id, key_levels=key_levels,
        )
        if not call_ok:
            # CALL 脚失敗: PUT 脚は既に発注済み → 片側スプレッドとして監視継続
            pushover(
                "SPY IC 警告",
                f"IC CALL脚エントリー失敗 {time_key}ET ({tactic})\n"
                f"PUT脚は発注済み → 片側スプレッドのまま監視継続",
                priority=1,
            )
            # bot の trade_id/signal_id を PUT 側の値に戻す
            if bot is not None:
                bot._current_trade_id  = ic_trade_id
                bot._current_signal_id = ic_signal_id
            return False

        # IC 両脚成功: CALL 側 place() が bot の trade_id/signal_id を上書きするため
        # PUT 側で生成した共通値に統一する
        if bot is not None:
            bot._current_trade_id  = ic_trade_id
            bot._current_signal_id = ic_signal_id

        # IC エントリーサマリーを condor_pnl.json に strategy="IC" で記録
        append_pnl_entry({
            "event": "ic_entry_summary",
            "strategy": "IC",
            "tactic": tactic,
            "expiry": expiry,
            "qty": qty,
            "vix": round(vix, 2),
            "trade_id": ic_trade_id,
            "signal_id": ic_signal_id,
            "ts": datetime.datetime.now(ET).isoformat(),
        })

        pushover(
            f"SPY IC [{tactic}]",
            f"Iron Condor エントリー {time_key}ET\n"
            f"PUT CS + CALL CS (\u03b4={params['delta']:.2f}/{call_params['delta']:.2f}) "
            f"w={params['width']} x{qty}\u679a",
        )
        log.info(
            f"[Entry] Iron Condor: PUT CS + CALL CS "
            f"(delta={params['delta']}/{call_params['delta']} width={params['width']}) "
            f"x{qty} signal_id={ic_signal_id}"
        )
        return True


# ══════════════════════════════════════════════════════════════════════════════
# IntradayMonitor — P0: VIXレジーム監視 + 動的閾値 + ストップ引き締め
# ══════════════════════════════════════════════════════════════════════════════
class IntradayMonitor:
    """60秒ごとにVIXレジーム監視。レジーム遷移時にアクションを実行。

    閾値は過去60日のVIXパーセンタイルから動的に算出する。
    固定パラメータは環境適応ではない（Sora Lab規律）。
    """

    REGIMES = ("calm", "normal", "elevated", "crisis")

    def __init__(self, mkt: MarketData, eng: 'TradeEngine', bot: 'SPYCreditSpreadBot'):
        self.mkt = mkt
        self.eng = eng
        self.bot = bot

        # 動的閾値（初期化時に計算、15分ごとに更新）
        self._vix_calm_threshold: float = 15.0
        self._vix_elevated_threshold: float = 22.0
        self._vix_crisis_threshold: float = 30.0
        # VIX変化率の動的ベース値（フォールバックは定数値）
        self._vix_rate_elevated: float = VIX_RATE_ELEVATED
        self._vix_rate_crisis: float = VIX_RATE_CRISIS
        self._thresholds_updated: Optional[datetime.datetime] = None

        # レジーム状態
        self._current_regime: str = "normal"
        self._previous_regime: str = "normal"
        self._vix_history_1h: list = []  # (timestamp, vix) — 直近1時間のVIX値

        # ウォームアップ: 前日VIXを初期値として投入し即判断可能にする
        self._started_at: datetime.datetime = datetime.datetime.now(ET)
        _yesterday_vix = get_yesterday_vix()
        if _yesterday_vix is not None:
            # 前日VIXで過去15分分のデータを擬似生成 → warmup即完了
            _now = datetime.datetime.now(ET)
            for i in range(16):
                _pseudo_ts = _now - datetime.timedelta(minutes=15 - i)
                self._vix_history_1h.append((_pseudo_ts, _yesterday_vix))
            self._warmup_complete = True
            log.info(f"[IntradayMonitor] 前日VIX={_yesterday_vix:.1f}で即ウォームアップ完了")
        else:
            self._warmup_complete = False
            log.info("[IntradayMonitor] 前日VIX取得不可 → 通常ウォームアップ(15分)")


        # ストップ倍率の動的管理
        self._base_stop_mult: float = STOP_LOSS_MULT
        self._current_stop_mult: float = STOP_LOSS_MULT

        # フル再評価用
        self._morning_score: Optional[float] = None
        self._last_full_eval: Optional[datetime.datetime] = None

        # クライシス解除後ウォームアップ（VIXスパイク後平均4.8日回復: research_vix_rate_strategies.md §3-3）
        # crisis→normal/calm 遷移日の日付文字列 "YYYY-MM-DD"（ET）。Noneは非解除中
        # セッション間跨ぎのためcondor_pnl.jsonから復元する
        self._crisis_resolved_date: Optional[str] = self._load_crisis_resolved_date()

        # N3-v2: ポートフォリオグリークス管理
        # GREEKS_CHECK_INTERVAL_TICKS tick（5分）ごとにgreeks_monitor.pyを呼び出す
        self._greeks_tick_counter: int = 0
        self._last_greeks: Optional[dict] = None

        # N4-DH: Delta Hedge状態管理
        # ヘッジポジション保有中フラグ（ヘッジ解除判定に使用）
        self._delta_hedge_active: bool = False
        # ヘッジコードリスト（解除時に閉じるオプションコード）
        self._delta_hedge_codes: list = []
        # ヘッジ発動回数（日次リセット）
        self._delta_hedge_count: int = 0
        # PDT週次ヘッジカウンタ: $25K未満時の緊急ヘッジPDT消費を週単位で追跡
        # FINRA PDTルール: 5営業日（月〜金）で3回以内のデイトレード
        # 月曜ET 0:00にリセット。$25K以上では参照しない。
        self._pdt_weekly_hedge_count: int = 0
        # カウンタが属する週の月曜日日付（ET）。週変わり検知に使用
        _today_et = datetime.datetime.now(ET).date()
        self._pdt_week_start: datetime.date = (
            _today_et - datetime.timedelta(days=_today_et.weekday())
        )  # weekday()=0がMonday

        # 初回閾値計算
        self._update_dynamic_thresholds()

    # ── post-crisis warmup ─────────────────────────────────────────────────────

    def _load_crisis_resolved_date(self) -> Optional[str]:
        """condor_pnl.jsonから最新のcrisis_resolvedイベントの日付を取得。"""
        try:
            trades = load_pnl()
            resolved = [t for t in trades if t.get("event") == "crisis_resolved"]
            if resolved:
                return resolved[-1].get("date")
        except Exception as e:
            log.warning(f"[IntradayMonitor] _load_crisis_resolved_date: {e}")
        return None

    @staticmethod
    def _count_biz_days_since(date_str: str) -> int:
        """date_str (YYYY-MM-DD, ET) から今日(ET)までの経過営業日数を返す。
        date_str当日を0日目として翌営業日が1日目。
        例: 月曜に解除 → 火曜=1, 水曜=2, 木曜=3
        """
        try:
            start = datetime.date.fromisoformat(date_str)
            today = datetime.datetime.now(ET).date()
            if today <= start:
                return 0
            count = 0
            d = start + datetime.timedelta(days=1)
            while d <= today:
                if d.weekday() < 5 and d not in US_HOLIDAYS:
                    count += 1
                d += datetime.timedelta(days=1)
            return count
        except Exception:
            return 99  # 解析失敗 → ウォームアップ解除扱い

    def post_crisis_size_factor(self) -> float:
        """クライシス解除後ウォームアップ中のサイズ係数を返す。
        - 解除後1〜2営業日: 0.5 (50%縮小)
        - 3営業日目以降: 1.0 (フルサイズ)
        根拠: VIXスパイク後平均回復4.8日 (CBOEデータ) — research_vix_rate_strategies.md §3-3
        """
        if self._crisis_resolved_date is None:
            return 1.0
        days = self._count_biz_days_since(self._crisis_resolved_date)
        if days <= 2:
            return 0.5
        return 1.0

    def _update_dynamic_thresholds(self):
        """過去60日のVIXデータからパーセンタイルベースの閾値を算出。"""
        vix_data = self.mkt.get_vix_history(VIX_HISTORY_DAYS)
        if len(vix_data) < 20:
            log.warning(f"IntradayMonitor: VIX history insufficient ({len(vix_data)} days), "
                        f"using fallback thresholds")
            # データ不足時のフォールバック（最低限の安全策）
            self._vix_calm_threshold = 15.0
            self._vix_elevated_threshold = 22.0
            self._vix_crisis_threshold = 30.0
            self._vix_rate_elevated = VIX_RATE_ELEVATED
            self._vix_rate_crisis = VIX_RATE_CRISIS
            return

        sorted_vix = sorted(vix_data)
        n = len(sorted_vix)

        def percentile(data: list, pct: int) -> float:
            """線形補間によるパーセンタイル計算。"""
            k = (pct / 100.0) * (len(data) - 1)
            f = int(k)
            c = min(f + 1, len(data) - 1)
            d = k - f
            return data[f] + d * (data[c] - data[f])

        self._vix_calm_threshold = round(percentile(sorted_vix, VIX_CALM_PERCENTILE), 1)
        self._vix_elevated_threshold = round(percentile(sorted_vix, VIX_ELEVATED_PERCENTILE), 1)
        self._vix_crisis_threshold = round(percentile(sorted_vix, VIX_CRISIS_PERCENTILE), 1)

        # VIX変化率ベース値の動的算出
        # 日次VIX変化率(%/日)のP70/P90を計算し、係数0.6を乗じて「最も激しい1時間」に近似。
        # 設計根拠: data/research_vix_rate_strategies.md §3-5
        #   intradayP75=2.41%/h, P90=4.22%/h (60日実測)
        #   固定値ELEVATED=5.0はP95相当(6.7%の時間), CRISIS=10.0はP99超(0.6%)
        # Floor/Cap: elevated=(2.0, 8.0), crisis=(4.0, 15.0)
        daily_changes = [
            abs(vix_data[i] - vix_data[i - 1]) / max(vix_data[i - 1], 1.0) * 100
            for i in range(1, len(vix_data))
        ]
        if daily_changes:
            sorted_chg = sorted(daily_changes)
            nc = len(sorted_chg)
            p70_daily = sorted_chg[int(0.70 * (nc - 1))]
            p90_daily = sorted_chg[int(0.90 * (nc - 1))]
            self._vix_rate_elevated = round(max(2.0, min(8.0, p70_daily * 0.6)), 1)
            self._vix_rate_crisis = round(max(4.0, min(15.0, p90_daily * 0.6)), 1)
        else:
            self._vix_rate_elevated = VIX_RATE_ELEVATED
            self._vix_rate_crisis = VIX_RATE_CRISIS

        self._thresholds_updated = datetime.datetime.now(ET)

        log.info(f"[IntradayMonitor] Dynamic thresholds updated: "
                 f"calm<{self._vix_calm_threshold} "
                 f"elevated>{self._vix_elevated_threshold} "
                 f"crisis>{self._vix_crisis_threshold} "
                 f"rate_elevated>{self._vix_rate_elevated}%/h "
                 f"rate_crisis>{self._vix_rate_crisis}%/h "
                 f"(from {n} days of VIX data)")

    def _dynamic_rate_threshold(self, vix: float, base_rate: float) -> float:
        """VIXレベルに応じてrate閾値を動的にスケールする。

        低VIX時は微小な絶対変動でもrate%が大きくなるため、閾値を引き上げる。
        高VIX時はrateが小さくても実際の変動幅が大きいため、閾値を引き下げる。

        スケール基準: elevated閾値を1.0xとし、VIXがその半分なら2.0x、2倍なら0.7x。
        """
        ref = self._vix_elevated_threshold
        if ref <= 0:
            return base_rate
        scale = ref / max(vix, 1.0)  # VIX低い→scale大→閾値高い
        scale = max(0.7, min(scale, 3.0))  # 0.7x〜3.0xに制限
        return base_rate * scale

    def _classify_regime(self, vix: float) -> str:
        """VIX値と変化率からレジームを判定。

        rate閾値はVIXレベルに応じて動的スケールする。
        低VIX時は微小変動を無視し、高VIX時は敏感に反応する。
        """
        rate = self._calc_vix_rate_per_hour()

        # VIXレベルに応じた動的rate閾値（ベース値は_update_dynamic_thresholds()で算出）
        crisis_rate = self._dynamic_rate_threshold(vix, self._vix_rate_crisis)
        elevated_rate = self._dynamic_rate_threshold(vix, self._vix_rate_elevated)

        # crisis判定: VIX単独で超過 OR (VIX elevated以上 かつ 動的rate超過)
        if vix > self._vix_crisis_threshold:
            return "crisis"
        if vix > self._vix_elevated_threshold and rate > crisis_rate:
            return "crisis"
        # elevated判定
        if vix > self._vix_elevated_threshold or rate > elevated_rate:
            return "elevated"
        # calm判定（VIXが低く、変化率も低い）
        if vix < self._vix_calm_threshold and rate < 3.0:
            return "calm"
        return "normal"

    def _check_warmup_complete(self, now: datetime.datetime) -> bool:
        """データの信頼度を動的に評価してウォームアップ完了を判定する。

        条件（全て満たす必要あり）:
        1. データポイント >= 15（60秒tickで15回=15分相当）
        2. 最古と最新のデータの時間幅 >= 10分
        3. VIXデータの分散が計算可能（データに幅がある）
        """
        n = len(self._vix_history_1h)
        if n < 15:
            return False
        oldest_ts = self._vix_history_1h[0][0]
        latest_ts = self._vix_history_1h[-1][0]
        elapsed_min = (latest_ts - oldest_ts).total_seconds() / 60.0
        if elapsed_min < 10:
            return False
        return True

    def _calc_vix_rate_per_hour(self) -> float:
        """直近1時間のVIX変化率（%/時）を計算。

        起動直後の誤判定を防ぐため、15分以上かつ10データポイント以上を要求する。
        """
        if len(self._vix_history_1h) < 10:  # 60秒tickで10回=10分相当
            return 0.0
        oldest_ts, oldest_vix = self._vix_history_1h[0]
        latest_ts, latest_vix = self._vix_history_1h[-1]
        elapsed_hours = (latest_ts - oldest_ts).total_seconds() / 3600.0
        if elapsed_hours < 0.25 or oldest_vix == 0:  # 15分未満は不正確（起動直後の誤判定防止）
            return 0.0
        return abs(latest_vix - oldest_vix) / oldest_vix * 100.0 / elapsed_hours

    def _handle_regime_transition(self, old_regime: str, new_regime: str, vix: float):
        """レジーム遷移時のアクションを実行。"""
        rate = self._calc_vix_rate_per_hour()
        log.warning(f"[IntradayMonitor] REGIME CHANGE: {old_regime} -> {new_regime} "
                    f"(VIX={vix:.1f}, rate={rate:.1f}%/h)")

        if new_regime == "crisis":
            # crisis: 全ポジション即手仕舞い + 緊急通知
            log.warning(f"[IntradayMonitor] CRISIS DETECTED — closing all positions")
            # P0-2: 決済前にunrealized_plを合算してpnl_usdとして記録する
            # N/Aの場合はcondor_pnl.jsonのentryからcreditベース概算
            crisis_positions = self.eng.get_open_positions()
            crisis_pnl_usd = 0.0
            crisis_na_count = 0
            for _p in crisis_positions:
                try:
                    _pl = _p.get("unrealized_pl", 0)
                    if _pl not in (None, "N/A", ""):
                        crisis_pnl_usd += float(_pl)
                    else:
                        crisis_na_count += 1
                except (ValueError, TypeError):
                    crisis_na_count += 1
            crisis_exit_status = "exact"
            crisis_entry_credit = None
            if crisis_na_count > 0:
                _today_et = datetime.datetime.now(ET).strftime("%Y-%m-%d")
                _crisis_entries = [
                    t for t in load_pnl()
                    if t.get("event") == "entry" and t.get("date") == _today_et
                ]
                if _crisis_entries:
                    _e = _crisis_entries[-1]
                    _ec = _e.get("net_credit")
                    _eq = _e.get("qty")
                    crisis_entry_credit = _ec
                    try:
                        crisis_pnl_usd = round(float(_ec) * int(_eq) * 100, 2)
                        crisis_exit_status = "estimated"
                        log.info(
                            f"[CrisisClose] unrealized_pl=N/A({crisis_na_count}件) "
                            f"→ creditフォールバック entry_credit={_ec} "
                            f"概算P&L=${crisis_pnl_usd:.2f}"
                        )
                    except (ValueError, TypeError):
                        crisis_exit_status = "unavailable"
                else:
                    crisis_exit_status = "unavailable"
                    log.warning(
                        f"[CrisisClose] unrealized_pl=N/A({crisis_na_count}件) かつ "
                        f"当日entryレコードなし → P&L算出不可"
                    )
            self.eng.close_all_positions("intraday_crisis")
            _crisis_fill_stats = _exit_fill_stats(
                self.eng._last_exit_fills if hasattr(self.eng, "_last_exit_fills") else {}
            )
            pushover_alert(
                "CRISIS: VIXレジーム危機",
                f"VIX={vix:.1f} (閾値: {self._vix_crisis_threshold})\n"
                f"変化率: {rate:.1f}%/h\n"
                f"全ポジション即手仕舞い実行",
                priority=1,
            )
            _crisis_signal_id = self.bot._current_signal_id if self.bot else None
            append_pnl_entry({
                "event": "exit", "reason": "intraday_crisis",
                "vix": vix, "rate_per_h": round(rate, 1),
                "regime": "crisis",
                "pnl_usd": round(crisis_pnl_usd, 2),
                "entry_credit": crisis_entry_credit,
                "exit_status": crisis_exit_status,
                "exit_fill_prices": _crisis_fill_stats["exit_fill_prices"],
                "exit_fill_avg": _crisis_fill_stats["exit_fill_avg"],
                "exit_net_cost": _crisis_fill_stats["exit_net_cost"],
                "trade_id": self.bot._current_trade_id if self.bot else None,
                "signal_id": _crisis_signal_id,
            })
            check_signal_divergence(_crisis_signal_id)
            if self.bot and hasattr(self.bot, "mkt"):
                self.bot.mkt.unsubscribe_all_option_legs()
            if self.bot and hasattr(self.bot, "_on_position_closed"):
                self.bot._on_position_closed(crisis_pnl_usd)

        elif old_regime == "crisis" and new_regime in ("normal", "calm"):
            # crisis解除: ウォームアップ期間を開始する
            # VIXスパイク後平均4.8日で回復 → 1-2営業日はサイズ50%縮小
            _resolved_date = datetime.datetime.now(ET).strftime("%Y-%m-%d")
            self._crisis_resolved_date = _resolved_date
            append_pnl_entry({
                "event": "crisis_resolved",
                "date": _resolved_date,
                "old_regime": old_regime,
                "new_regime": new_regime,
                "vix": round(vix, 2),
            })
            log.warning(
                f"[IntradayMonitor] CRISIS RESOLVED: crisis -> {new_regime} "
                f"(VIX={vix:.1f}) → post-crisis warmup 1-2 biz days at 50% size"
            )
            pushover(
                f"VIXレジーム: crisis解除 → {new_regime}",
                f"VIX={vix:.1f}\nウォームアップ開始: 1-2営業日はサイズ50%縮小",
            )
            # calm遷移時はストップもワイドに
            if new_regime == "calm":
                self._current_stop_mult = self._base_stop_mult * 1.2
            else:
                self._current_stop_mult = self._base_stop_mult

        elif new_regime == "calm":
            # calm遷移: 時間価値が味方する低ボラ環境 → ストップをワイドに（1.2x）
            self._current_stop_mult = self._base_stop_mult * 1.2
            log.info(f"[IntradayMonitor] Calm regime: stop widened to "
                     f"{self._current_stop_mult:.2f} ({self._base_stop_mult:.2f} * 1.2)")
            pushover(
                "VIXレジーム: calm",
                f"VIX={vix:.1f} (閾値: {self._vix_calm_threshold})\n"
                f"ストップ拡大: {self._current_stop_mult:.2f}x (時間価値優位環境)",
            )

        elif old_regime == "normal" and new_regime == "elevated":
            # elevated: ストップを引き締め（50%に縮小）
            self._current_stop_mult = self._base_stop_mult * 0.50
            log.info(f"[IntradayMonitor] Stop tightened: {self._base_stop_mult:.2f} -> "
                     f"{self._current_stop_mult:.2f}")
            pushover(
                "VIXレジーム: elevated",
                f"VIX={vix:.1f} (閾値: {self._vix_elevated_threshold})\n"
                f"ストップ引き締め: {self._current_stop_mult:.2f}x",
            )

        elif old_regime == "elevated" and new_regime == "normal":
            # normal復帰: ストップを通常に戻す
            self._current_stop_mult = self._base_stop_mult
            log.info(f"[IntradayMonitor] Stop restored: {self._current_stop_mult:.2f}")
            pushover(
                "VIXレジーム: normal復帰",
                f"VIX={vix:.1f}\nストップ通常復帰: {self._current_stop_mult:.2f}x",
            )

        elif old_regime == "calm" and new_regime == "normal":
            # calm→normal: ストップを通常に戻す
            self._current_stop_mult = self._base_stop_mult
            log.info(f"[IntradayMonitor] Calm->normal: stop restored to {self._current_stop_mult:.2f}")
            pushover(
                "VIXレジーム: calm→normal",
                f"VIX={vix:.1f}\nストップ通常復帰: {self._current_stop_mult:.2f}x",
            )

        elif old_regime == "calm" and new_regime in ("elevated", "crisis"):
            # calm→elevated/crisisは急変を意味する
            self._current_stop_mult = self._base_stop_mult * 0.50
            log.warning(f"[IntradayMonitor] Rapid VIX shift calm->{new_regime}, stop tightened")

    def _full_environment_eval(self, vix: float) -> Optional[float]:
        """15分ごとのフル環境再評価。環境スコアを算出して朝と比較。"""
        # 簡易環境スコア: VIXベース（低い方が高スコア）+ VRP
        base_score = 100.0

        # VIXコンポーネント（動的閾値ベース）
        if vix > self._vix_crisis_threshold:
            base_score -= 50
        elif vix > self._vix_elevated_threshold:
            base_score -= 25
        elif vix > self._vix_calm_threshold:
            base_score -= 10

        # VIX変化率コンポーネント（動的閾値ベース）
        rate = self._calc_vix_rate_per_hour()
        if rate > self._dynamic_rate_threshold(vix, self._vix_rate_crisis):
            base_score -= 30
        elif rate > self._dynamic_rate_threshold(vix, self._vix_rate_elevated):
            base_score -= 15

        # VRPコンポーネント
        if ENABLE_VRP_CHECK:
            vrp = self.mkt.calc_vrp(vix)
            if vrp is not None and vrp < 0:
                base_score -= VRP_NEGATIVE_PENALTY
                log.info(f"[IntradayMonitor] VRP={vrp:.2f} < 0 → score -{VRP_NEGATIVE_PENALTY}")

        return max(0.0, base_score)

    def set_morning_score(self, score: float):
        """朝のエントリー判断時のスコアを記録。"""
        self._morning_score = score
        log.info(f"[IntradayMonitor] Morning score set: {score:.1f}")

    def init_base_stop_mult(self, vix: float, hours_remaining: float,
                            symbol: Optional[str] = None, mkt: Optional['MarketData'] = None):
        """起動時（朝のpremarket assessment後）にVIX×残り時間でbase_stop_multを設定する。

        calc_dynamic_stop_loss()で算出した値を_base_stop_multに設定する。
        symbol_params.json が存在する場合はHV(20日)で追加調整する:
          adjusted_sl = base_sl * (1 + max(0, HV_20 - 0.20) * sl_vol_adj_coeff)
        _current_stop_multも同値にリセット（日中のレジーム変化がベースから相対計算されるため）。
        VIX取得不可・データ不足時はSTOP_LOSS_MULT(=1.00)にフォールバックする。
        """
        if vix is None or vix <= 0:
            log.warning("[IntradayMonitor] init_base_stop_mult: VIX無効 → fallback STOP_LOSS_MULT")
            return
        new_base = calc_dynamic_stop_loss(vix, hours_remaining)

        # HVベース調整（symbol_params.json存在時のみ）
        if _SYMBOL_PARAMS and symbol and mkt:
            try:
                hv = mkt.get_symbol_hv(symbol, period=20)
                new_base = calc_hv_adjusted_sl(symbol, new_base, hv)
            except Exception as _e:
                log.debug(f"[IntradayMonitor] HV調整スキップ: {_e}")

        self._base_stop_mult = new_base
        self._current_stop_mult = new_base
        log.info(f"[IntradayMonitor] base_stop_mult initialized: {new_base:.4f} "
                 f"(VIX={vix:.1f}, hours_remaining={hours_remaining:.2f})")

    def tick(self):
        """毎ティック（60秒ごと）呼び出し。VIXレジーム監視のメインループ。"""
        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("[IntradayMonitor] VIX unavailable, skipping tick")
            return

        now = datetime.datetime.now(ET)

        # VIX履歴を追加（1時間分を保持）
        self._vix_history_1h.append((now, vix))
        cutoff = now - datetime.timedelta(hours=1)
        self._vix_history_1h = [(ts, v) for ts, v in self._vix_history_1h if ts >= cutoff]

        # ウォームアップ判定: データの信頼度を動的に評価
        if not self._warmup_complete:
            self._warmup_complete = self._check_warmup_complete(now)
            if not self._warmup_complete:
                # VIXレベル単独の判定のみ許可（rateは使わない）
                if vix > self._vix_crisis_threshold and self._current_regime != "crisis":
                    self._previous_regime = self._current_regime
                    self._handle_regime_transition(self._current_regime, "crisis", vix)
                    self._current_regime = "crisis"
            else:
                log.info(f"[IntradayMonitor] Warmup complete: "
                         f"data_points={len(self._vix_history_1h)}, "
                         f"elapsed={(now - self._started_at).total_seconds()/60:.0f}min")

        if self._warmup_complete:
            # レジーム判定（ウォームアップ完了後のみフル判定）
            new_regime = self._classify_regime(vix)
            if new_regime != self._current_regime:
                self._previous_regime = self._current_regime
                self._handle_regime_transition(self._current_regime, new_regime, vix)
                self._current_regime = new_regime

        # 15分ごとのフル環境再評価
        if (self._last_full_eval is None or
                (now - self._last_full_eval).total_seconds() >= INTRADAY_FULL_EVAL_SEC):
            score = self._full_environment_eval(vix)
            self._last_full_eval = now
            if score is not None:
                log.info(f"[IntradayMonitor] Env score={score:.1f} "
                         f"(morning={self._morning_score or 'N/A'})")
                # 朝のスコアから20点以上低下 → ストップ引き締め
                if (self._morning_score is not None and
                        self._morning_score - score >= 20 and
                        self._current_stop_mult == self._base_stop_mult):
                    self._current_stop_mult = self._base_stop_mult * 0.50
                    log.warning(f"[IntradayMonitor] Score dropped {self._morning_score:.0f}"
                                f" -> {score:.0f} (delta={self._morning_score - score:.0f})"
                                f" → stop tightened to {self._current_stop_mult:.2f}")
                    pushover(
                        "環境スコア低下",
                        f"朝: {self._morning_score:.0f} → 現在: {score:.0f}\n"
                        f"ストップ引き締め: {self._current_stop_mult:.2f}x",
                    )

        # 動的閾値の更新（15分ごと、フル再評価と同タイミング）
        if (self._thresholds_updated is None or
                (now - self._thresholds_updated).total_seconds() >= INTRADAY_FULL_EVAL_SEC):
            self._update_dynamic_thresholds()

        # N3-v2: ポートフォリオグリークス管理（5分ごと）
        self._greeks_tick_counter += 1
        if self._greeks_tick_counter >= GREEKS_CHECK_INTERVAL_TICKS:
            self._greeks_tick_counter = 0
            self._check_portfolio_greeks()

    def _check_portfolio_greeks(self):
        """ポートフォリオ合計グリークスを計算して閾値超過時にアクションを取る（N3-v2）。

        greeks_monitor.pyのcalc_portfolio_greeks()を使用する。
        15:00 ET以降はガンマが加速するため、閾値を厳しくする。
        """
        try:
            from greeks_monitor import calc_portfolio_greeks, check_greeks_limits
        except ImportError:
            log.debug("[Greeks] greeks_monitor not available, skipping")
            return
        try:
            positions = self.eng.get_open_positions()
            if not positions:
                return
            quote_ctx = self.mkt.quote_ctx if self.mkt else None
            greeks = calc_portfolio_greeks(positions, quote_ctx)
            self._last_greeks = greeks

            # 15:00 ET以降はガンマリスクが3.2倍（CBOEデータ）→閾値を半分に厳しくする
            et_now = datetime.datetime.now(ET)
            gamma_thr = GAMMA_RISK_THRESHOLD
            if et_now.hour >= 15:
                gamma_thr = GAMMA_RISK_THRESHOLD / 2.0

            total_gamma = greeks.get("total_gamma", 0.0)
            if abs(total_gamma) > gamma_thr:
                direction = "ショートガンマ" if total_gamma < 0 else "ロングガンマ"
                log.warning(
                    f"[Greeks] ガンマリスク警告: total_gamma={total_gamma:+.4f} "
                    f"({direction}, 閾値={gamma_thr:.4f})"
                )
                if et_now.hour >= 15 and total_gamma < -gamma_thr:
                    # 15時以降ショートガンマ警告 → Pushover通知
                    pushover_alert(
                        "ガンマリスク警告",
                        f"15時以降のショートガンマ集中\n"
                        f"total_gamma={total_gamma:+.4f} (閾値±{gamma_thr:.4f})\n"
                        f"ポジション縮小を検討",
                    )
            # 全ギリシャ文字チェック（delta/vega超過も記録）
            check_greeks_limits(greeks)

            # N4-DH: Delta Hedgeチェック（greeks計算後に実行）
            self._try_delta_hedge(greeks)
        except Exception as e:
            log.debug(f"[Greeks] _check_portfolio_greeks: {e}")

    def _reset_pdt_weekly_hedge_if_needed(self):
        """月曜ET 0:00に週次PDTヘッジカウンタをリセットする。

        毎回 _try_delta_hedge() の先頭で呼び出す。
        週変わり（前回カウンタ取得週 != 今週の月曜）を検知してリセット。
        """
        today_et = datetime.datetime.now(ET).date()
        this_monday = today_et - datetime.timedelta(days=today_et.weekday())
        if this_monday != self._pdt_week_start:
            log.info(
                f"[DeltaHedge] 週次PDTカウンタリセット: "
                f"{self._pdt_week_start} → {this_monday} "
                f"(前週消費: {self._pdt_weekly_hedge_count}/{DELTA_HEDGE_WEEKLY_BUDGET}回)"
            )
            self._pdt_weekly_hedge_count = 0
            self._pdt_week_start = this_monday

    def _try_delta_hedge(self, greeks: dict):
        """N4-DH: ポートフォリオDeltaが閾値超過した場合にオプションでヘッジする。

        設計根拠:
        - CS/IC 売りは理論上デルタ中立付近だが、価格変動でDeltaが偏る
        - |total_delta| > 0.30 → CALL/PUT 買いでデルタを中和
        - 14:30 ET以降はGamma爆発帯 → 閾値を 0.40 に引き上げ（保守的）
        - |total_delta| < 0.15 に戻ったら → ヘッジ解除
        - PDT制約下（pdt_constrained）: 精密PDT動的判定（should_delta_hedge()）で制御
          - $25K以上: 制限なし発動
          - $25K未満・週3回超: 完全ブロック
          - $25K未満・枠残あり・緊急（|Delta|>0.5）: 発動（Pushover警告 + PDT残回数通知）
          - $25K未満・枠残あり・非緊急: スキップ（PDT枠温存）
          - $25K未満・PDT枠ゼロ・危険Delta: 警告Pushover（「入金を」提案）

        発動条件:
          - 新規ヘッジ: |total_delta| > trigger AND should_delta_hedge() == True
          - ヘッジ解除: |total_delta| < unwind AND _delta_hedge_active
          - 発動上限: 1日3回まで（Gamma加速帯での過剰ヘッジ防止）

        フォールバック:
          - futu接続なし/dry-testモード: ログのみ（実発注なし）
          - Greeks取得失敗: スキップ
        """
        try:
            # 週次PDTカウンタのリセットチェック（月曜切替）
            self._reset_pdt_weekly_hedge_if_needed()

            et_now = datetime.datetime.now(ET)
            total_delta = greeks.get("total_delta", 0.0)
            delta_abs = abs(total_delta)

            # 時間帯別閾値: 14:30 ET以降はGamma爆発帯 → より保守的
            total_min = et_now.hour * 60 + et_now.minute
            trigger = DELTA_HEDGE_TRIGGER_LATE if total_min >= (14 * 60 + 30) else DELTA_HEDGE_TRIGGER
            unwind = DELTA_HEDGE_UNWIND

            # ヘッジ解除チェックを最初に行う（PDT判定より優先）
            # 既存ヘッジ保有中は口座残高に関わらず収束判定を実行する
            if self._delta_hedge_active:
                if delta_abs < unwind:
                    log.info(
                        f"[DeltaHedge] UNWIND: |total_delta|={delta_abs:.4f} < {unwind:.2f}"
                        f" → ヘッジ解除"
                    )
                    self._delta_hedge_active = False
                    self._delta_hedge_codes = []
                    pushover(
                        "DeltaHedge解除",
                        f"total_delta={total_delta:+.4f} < ±{unwind:.2f} → ヘッジ解除",
                    )
                else:
                    log.debug(
                        f"[DeltaHedge] ヘッジ継続中: total_delta={total_delta:+.4f}"
                    )
                return

            # 口座残高の取得（bot経由 → 精密PDT判定に使用）
            # bot=None（dry-test/テスト環境）のときは $25K以上として扱う（制限なし）
            _cash_usd: float = 25000.0  # フォールバック: 制限なし
            if self.bot is not None:
                try:
                    _raw = getattr(self.bot, "_last_cash_usd", None)
                    if _raw is None and self.eng:
                        _raw_jpy = self.eng.get_account_cash()
                        _raw = _raw_jpy / 150.0 if _raw_jpy and _raw_jpy > 1000 else _raw_jpy
                    if _raw is not None:
                        _cash_usd = float(_raw)
                except Exception:
                    pass  # フォールバック値($25K)を維持

            # 精密PDT判定: should_delta_hedge() で新規ヘッジ発動可否を動的決定
            # ヘッジ解除は上で実施済み。ここからは新規ヘッジの可否判定のみ
            _allowed, _reason = should_delta_hedge(
                position_delta_abs=delta_abs,
                cash_usd=_cash_usd,
                weekly_hedge_count=self._pdt_weekly_hedge_count,
                is_emergency=False,  # 緊急度はposition_delta_absから自動判定
            )

            if not _allowed:
                log.debug(f"[DeltaHedge] スキップ: {_reason}")
                # PDT枠ゼロかつ危険Delta → 緊急Pushover警告
                if (
                    self._pdt_weekly_hedge_count >= DELTA_HEDGE_WEEKLY_BUDGET
                    and delta_abs > DELTA_HEDGE_EMERGENCY_THRESHOLD
                ):
                    log.warning(
                        f"[DeltaHedge] URGENT: Delta={delta_abs:.3f} だがPDT枠なし"
                        f" (週{self._pdt_weekly_hedge_count}/{DELTA_HEDGE_WEEKLY_BUDGET}回消費)"
                    )
                    pushover(
                        "URGENT: DeltaHedge不可・入金検討",
                        f"Delta {total_delta:+.3f} (危険水準)\n"
                        f"PDT枠使い切り: 今週{self._pdt_weekly_hedge_count}/{DELTA_HEDGE_WEEKLY_BUDGET}回\n"
                        f"口座残高≈${_cash_usd:.0f} (<$25,000)\n"
                        f"解決策: 口座に入金して$25K以上にする",
                        priority=1,
                    )
                return

            # ヘッジ発動上限チェック（日次）
            if self._delta_hedge_count >= 3:
                log.info(
                    f"[DeltaHedge] 本日ヘッジ発動上限 (3回) に達した → スキップ"
                    f" (total_delta={total_delta:+.4f})"
                )
                return

            # ヘッジ発動チェック
            if delta_abs <= trigger:
                log.debug(
                    f"[DeltaHedge] 閾値内: |total_delta|={delta_abs:.4f} <= {trigger:.2f}"
                )
                return

            # ヘッジ必要枚数算出
            # excess_delta = |total_delta| - trigger（超過分のみをヘッジ）
            excess_delta = delta_abs - trigger
            hedge_qty = max(1, math.ceil(excess_delta / DELTA_HEDGE_CONTRACT_DELTA))
            # total_delta > 0（上昇バイアス）→ PUT買い、< 0（下落バイアス）→ CALL買い
            hedge_direction = "PUT" if total_delta > 0 else "CALL"

            # 緊急ヘッジ（$25K未満・PDT枠消費）かどうか判定
            _is_pdt_emergency = _cash_usd < 25000 and delta_abs > DELTA_HEDGE_EMERGENCY_THRESHOLD
            _pdt_remaining = max(0, DELTA_HEDGE_WEEKLY_BUDGET - self._pdt_weekly_hedge_count)

            log.warning(
                f"[DeltaHedge] TRIGGER: total_delta={total_delta:+.4f}"
                f" (trigger=±{trigger:.2f})"
                f" → {hedge_direction} 買い {hedge_qty}枚でヘッジ"
                f" (excess={excess_delta:.4f}, 日次={self._delta_hedge_count + 1}/3"
                + (f", PDT残{_pdt_remaining}回" if _cash_usd < 25000 else "") + ")"
            )

            # dry-test / futu未接続 → ログのみ（実発注なし）
            _dry = not FUTU_AVAILABLE
            if self.bot is not None:
                _dry = _dry or getattr(self.bot, "dry_test", False)

            if _dry:
                log.info(
                    f"[DeltaHedge] dry-test/futu未接続 → 実発注スキップ"
                    f" (方針: {hedge_direction} x{hedge_qty})"
                )
                self._delta_hedge_active = True
                self._delta_hedge_count += 1
                if _is_pdt_emergency:
                    self._pdt_weekly_hedge_count += 1
                return

            # 実ヘッジ発注: ATMオプション買い
            try:
                expiry_today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
                spy_price = self.mkt.get_spy_current() if self.mkt else None
                if spy_price is None:
                    log.warning("[DeltaHedge] SPY価格取得失敗 → ヘッジスキップ")
                    return

                # ATMストライク算出（$1刻みに丸め）
                atm_strike = round(spy_price)

                # futu オプションコード生成
                # 形式: US.SPYYYMMDDCP STRIKE（8桁 ×1000 例: US.SPY260417P00553000）
                _exp_compact = expiry_today.replace("-", "")[2:]  # YYMMDD
                _type_char = "C" if hedge_direction == "CALL" else "P"
                _strike_int = int(atm_strike * 1000)
                hedge_code = f"US.SPY{_exp_compact}{_type_char}{_strike_int:08d}"

                log.info(f"[DeltaHedge] ヘッジ発注: {hedge_code} x{hedge_qty} BUY")
                if self.eng:
                    import futu as _futu_dh
                    order_id, fill_method = self.eng._place_single_leg(
                        hedge_code, _futu_dh.TrdSide.BUY, hedge_qty,
                        f"delta_hedge_{hedge_direction}",
                        use_limit=False,
                    )
                    if order_id and fill_method != "failed":
                        self._delta_hedge_active = True
                        self._delta_hedge_codes = [hedge_code]
                        self._delta_hedge_count += 1
                        if _is_pdt_emergency:
                            self._pdt_weekly_hedge_count += 1
                        _new_remaining = max(0, DELTA_HEDGE_WEEKLY_BUDGET - self._pdt_weekly_hedge_count)
                        _pushover_body = (
                            f"total_delta={total_delta:+.4f} (閾値±{trigger:.2f})\n"
                            f"{hedge_direction} 買い {hedge_qty}枚\n"
                            f"コード: {hedge_code}\n"
                            f"本日{self._delta_hedge_count}回目"
                        )
                        if _cash_usd < 25000:
                            _pushover_body += (
                                f"\nPDT残: {_new_remaining}/{DELTA_HEDGE_WEEKLY_BUDGET}回"
                            )
                            if _is_pdt_emergency:
                                _pushover_body += " (緊急発動・PDT枠消費)"
                        pushover("DeltaHedge発動", _pushover_body)
                    else:
                        log.error(f"[DeltaHedge] 発注失敗: fill_method={fill_method} code={hedge_code}")
            except Exception as _he:
                log.warning(f"[DeltaHedge] 発注処理エラー: {_he}")

        except Exception as e:
            log.debug(f"[DeltaHedge] _try_delta_hedge: {e}")

    @staticmethod
    def _theta_optimal_window(pnl_data: Optional[list] = None) -> dict:
        """N4-TH: 過去トレードから時間帯別勝率×P&Lで最適エントリー窓を算出する。

        設計根拠:
        - 0DTE オプションのTheta崩壊は一様でなく、14:00-15:00 ET付近が最大
        - 過去condor_pnl.jsonのenv_snapshotから time_et を抽出して時間帯別に集計
        - スコア = 時間帯別勝率 × 平均P&L（正規化）

        Returns:
            {
              "optimal_hour": int|None,   # 最適な時間帯 (ET hour, 0-23)
              "hourly_scores": dict,      # hour -> avg score (0.0-1.0)
              "is_optimal_now": bool,     # 現在がoptimal_hour帯か
              "reason": str,             # 判断理由テキスト
            }
        データ不足の場合は学術的デフォルト (14時ET) を返す。
        """
        try:
            if pnl_data is None:
                pnl_data = load_pnl()

            snapshots = [
                r for r in pnl_data
                if r.get("event") == "env_snapshot" and r.get("time_et")
            ]
            exits = [
                r for r in pnl_data
                if r.get("event") == "exit" and r.get("pnl_usd") is not None
            ]

            # 過去THETA_OPTIMAL_LOOKBACK_DAYS日分にフィルタ
            _tz = ET
            cutoff = datetime.datetime.now(_tz) - datetime.timedelta(days=THETA_OPTIMAL_LOOKBACK_DAYS)

            # exitをtrade_idでインデックス化（O(n)ルックアップ回避）
            exit_by_trade: dict = {}
            for ex in exits:
                tid = ex.get("trade_id")
                if tid:
                    exit_by_trade[tid] = ex

            hourly_wins: dict = {}   # hour -> [1=win, 0=loss]
            hourly_net: dict = {}    # hour -> [pnl_usd]

            for snap in snapshots:
                try:
                    ts_str = snap["time_et"]
                    ts = datetime.datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_tz)
                    ts_et = ts.astimezone(_tz)
                    # タイムゾーン比較のため両辺をaware datetimeに統一
                    if cutoff.tzinfo is None:
                        _cutoff_aware = cutoff.replace(tzinfo=_tz)
                    else:
                        _cutoff_aware = cutoff
                    if ts_et < _cutoff_aware:
                        continue
                    hour = ts_et.hour
                except (ValueError, KeyError, TypeError):
                    continue

                trade_id = snap.get("trade_id")
                matched_exit = exit_by_trade.get(trade_id) if trade_id else None
                if matched_exit is None:
                    continue

                pnl = float(matched_exit.get("pnl_usd", 0) or 0)
                hourly_wins.setdefault(hour, []).append(1 if pnl > 0 else 0)
                hourly_net.setdefault(hour, []).append(pnl)

            # 時間帯スコア計算: 勝率 × 平均P&L正規化
            hourly_scores: dict = {}
            for h in sorted(set(list(hourly_wins.keys()) + list(hourly_net.keys()))):
                wins = hourly_wins.get(h, [])
                nets = hourly_net.get(h, [])
                if not wins:
                    continue
                win_rate = sum(wins) / len(wins)
                avg_pnl = sum(nets) / len(nets) if nets else 0.0
                hourly_scores[h] = round(win_rate * (1.0 + max(0.0, avg_pnl) / 100.0), 4)

            if not hourly_scores:
                optimal_hour = 14
                reason = "データ不足 → デフォルト最適時間帯 14:00 ET (学術的知見)"
            else:
                optimal_hour = max(hourly_scores, key=lambda h: hourly_scores[h])
                best_score = hourly_scores[optimal_hour]
                reason = (
                    f"過去{THETA_OPTIMAL_LOOKBACK_DAYS}日分析:"
                    f" 最適時間帯={optimal_hour}時ET"
                    f" (score={best_score:.3f},"
                    f" N={len(hourly_wins.get(optimal_hour, []))}件)"
                )

            et_now = datetime.datetime.now(ET)
            is_optimal_now = (et_now.hour == optimal_hour)

            log.info(
                f"[ThetaOptimal] {reason}"
                f" | now_ET={et_now.hour}h → optimal={is_optimal_now}"
            )

            return {
                "optimal_hour": optimal_hour,
                "hourly_scores": hourly_scores,
                "is_optimal_now": is_optimal_now,
                "reason": reason,
            }
        except Exception as e:
            log.debug(f"[ThetaOptimal] _theta_optimal_window: {e}")
            return {
                "optimal_hour": None,
                "hourly_scores": {},
                "is_optimal_now": False,
                "reason": f"エラー: {e}",
            }

    @property
    def current_regime(self) -> str:
        return self._current_regime

    @property
    def current_stop_mult(self) -> float:
        return self._current_stop_mult

    def reset_daily(self):
        """日次リセット。"""
        self._current_regime = "normal"
        self._previous_regime = "normal"
        self._vix_history_1h.clear()
        self._current_stop_mult = self._base_stop_mult
        self._started_at = datetime.datetime.now(ET)
        self._warmup_complete = False
        self._morning_score = None
        self._last_full_eval = None
        # N4-DH: Delta Hedge 日次リセット
        self._delta_hedge_active = False
        self._delta_hedge_codes = []
        self._delta_hedge_count = 0
        # 週次PDTカウンタのリセットチェック（月曜切替時のみリセット）
        self._reset_pdt_weekly_hedge_if_needed()
        log.info("[IntradayMonitor] Daily reset")


# ══════════════════════════════════════════════════════════════════════════════
# Key Level Integration (G1+G2+G13)
# ══════════════════════════════════════════════════════════════════════════════
def build_key_levels(mkt: 'MarketData', expiry: Optional[str] = None) -> dict:
    """毎朝プレマーケット（ET 9:25）に呼び出し、その日の重要価格帯マップを構築する。

    取得項目:
      1. ES先物オーバーナイト高値/安値 (Yahoo Finance ES=F intraday 5m)
      2. 前日SPY 高値/安値/終値/VWAP≈(H+L+C)/3 (Yahoo Finance SPY daily)
      3. 0DTE OI集中ストライク: Call/Putそれぞれ最大OIのストライク (futu)
      4. Expected Move上下限: ATMストラドル価格 or VIX式近似 (futu/fallback)

    Returns:
        {
          "es_on_high": float|None,    "es_on_low": float|None,
          "spy_prev_high": float|None, "spy_prev_low": float|None,
          "spy_prev_close": float|None,"spy_prev_vwap": float|None,
          "oi_call_peak": float|None,  "oi_put_peak": float|None,
          "em_upper": float|None,      "em_lower": float|None,
          "all_levels": list[float],   # proximity checkに使うフラットリスト
          "error": str|None,
        }
    """
    result: dict = {
        "es_on_high":    None, "es_on_low":     None,
        "spy_prev_high": None, "spy_prev_low":  None,
        "spy_prev_close":None, "spy_prev_vwap": None,
        "oi_call_peak":  None, "oi_put_peak":   None,
        "em_upper":      None, "em_lower":      None,
        "all_levels":    [],
        "error":         None,
    }

    headers = {"User-Agent": "Mozilla/5.0"}

    # ── 1. ES先物オーバーナイト高値/安値 ──────────────────────────────────
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/ES%3DF",
            params={"interval": "5m", "range": "1d"},
            headers=headers, timeout=10,
        )
        chart = resp.json()["chart"]["result"][0]
        timestamps = chart.get("timestamp", [])
        highs  = chart["indicators"]["quote"][0].get("high", [])
        lows   = chart["indicators"]["quote"][0].get("low", [])
        now_et = datetime.datetime.now(ET)
        mkt_open_ts = now_et.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
        on_highs = [h for ts, h in zip(timestamps, highs)
                    if ts is not None and h is not None and ts < mkt_open_ts]
        on_lows  = [l for ts, l in zip(timestamps, lows)
                    if ts is not None and l is not None and ts < mkt_open_ts]
        if on_highs:
            result["es_on_high"] = round(max(on_highs), 2)
        if on_lows:
            result["es_on_low"]  = round(min(on_lows), 2)
        log.info(f"[KeyLevel] ES ON range: high={result['es_on_high']} low={result['es_on_low']}")
    except Exception as e:
        log.warning(f"[KeyLevel] ES ON range fetch error: {e}")
        result["error"] = str(e)

    # ── 2. 前日SPY OHLC / VWAP≈(H+L+C)/3 ─────────────────────────────────
    try:
        end_ts   = int(datetime.datetime.now(ET).timestamp())
        start_ts = end_ts - 10 * 86400
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
            params={"period1": start_ts, "period2": end_ts, "interval": "1d"},
            headers=headers, timeout=10,
        )
        q = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]
        closes = [c for c in q.get("close", []) if c is not None]
        highs2 = [h for h in q.get("high", [])  if h is not None]
        lows2  = [l for l in q.get("low", [])   if l is not None]
        if len(closes) >= 2:
            result["spy_prev_close"] = round(closes[-2], 2)
            result["spy_prev_high"]  = round(highs2[-2], 2)
            result["spy_prev_low"]   = round(lows2[-2], 2)
            result["spy_prev_vwap"]  = round(
                (highs2[-2] + lows2[-2] + closes[-2]) / 3, 2)
        log.info(f"[KeyLevel] SPY prev: H={result['spy_prev_high']} "
                 f"L={result['spy_prev_low']} C={result['spy_prev_close']} "
                 f"VWAP={result['spy_prev_vwap']}")
    except Exception as e:
        log.warning(f"[KeyLevel] SPY prev OHLC fetch error: {e}")
        if not result["error"]:
            result["error"] = str(e)

    # ── 3. 0DTE OI集中ストライク + 4. Expected Move ────────────────────────
    spy_price = mkt.get_spy_current() or mkt.get_spy_open() or 0.0
    if expiry and FUTU_AVAILABLE and mkt.quote_ctx and spy_price > 0:
        try:
            vol_data = mkt.scan_option_volumes(expiry, spy_price)
            calls = [d for d in vol_data if d["type"] == "CALL" and d["open_interest"] > 0]
            puts  = [d for d in vol_data if d["type"] == "PUT"  and d["open_interest"] > 0]
            if calls:
                result["oi_call_peak"] = max(calls, key=lambda x: x["open_interest"])["strike"]
            if puts:
                result["oi_put_peak"]  = max(puts,  key=lambda x: x["open_interest"])["strike"]
            log.info(f"[KeyLevel] OI peaks: call={result['oi_call_peak']} "
                     f"put={result['oi_put_peak']}")

            # ATMストラドル → Expected Move
            # [P0 BUG修正] center_strike=spy_price でチェーンを現在価格周辺に絞る
            atm_call_chain = mkt.get_option_chain_with_greeks(
                expiry, "CALL", center_strike=float(spy_price))
            atm_put_chain  = mkt.get_option_chain_with_greeks(
                expiry, "PUT", center_strike=float(spy_price))
            if atm_call_chain and atm_put_chain:
                atm_call = min(atm_call_chain, key=lambda o: abs(o["strike_price"] - spy_price))
                atm_put  = min(atm_put_chain,  key=lambda o: abs(o["strike_price"] - spy_price))
                em = atm_call.get("bid_price", 0) + atm_put.get("bid_price", 0)
                if em > 0:
                    result["em_upper"] = round(spy_price + em, 2)
                    result["em_lower"] = round(spy_price - em, 2)
                    log.info(f"[KeyLevel] EM straddle: em={em:.2f} "
                             f"upper={result['em_upper']} lower={result['em_lower']}")
        except Exception as e:
            log.warning(f"[KeyLevel] OI/EM fetch error: {e}")

    # EM fallback: VIX式近似 (futu不可またはATMチェーン取得失敗時)
    if result["em_upper"] is None and spy_price > 0:
        try:
            vix_resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                headers=headers, timeout=5,
            )
            vix_val = float(vix_resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            em_approx = spy_price * vix_val / (16.0 * math.sqrt(252))
            result["em_upper"] = round(spy_price + em_approx, 2)
            result["em_lower"] = round(spy_price - em_approx, 2)
            log.info(f"[KeyLevel] EM approx (VIX={vix_val:.1f}): "
                     f"upper={result['em_upper']} lower={result['em_lower']}")
        except Exception as e:
            log.warning(f"[KeyLevel] EM fallback error: {e}")

    # ── all_levels: proximity checkに使うフラットリスト ────────────────────
    candidates = [
        result["es_on_high"],   result["es_on_low"],
        result["spy_prev_high"],result["spy_prev_low"],
        result["spy_prev_close"],result["spy_prev_vwap"],
        result["oi_call_peak"], result["oi_put_peak"],
        result["em_upper"],     result["em_lower"],
    ]
    result["all_levels"] = [round(v, 2) for v in candidates if v is not None]
    log.info(f"[KeyLevel] all_levels={result['all_levels']}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# PremarketAssessment — P1: VRPを含む朝の環境評価
# ══════════════════════════════════════════════════════════════════════════════
def _load_next_day_bias() -> str:
    """next_day_bias.jsonからdirection_biasとes_directionを合成して返す（bull/bear/neutral）。

    ロジック:
      - dir_bias (VIX term structure) と es_direction (ES先物) が一致 → その値を返す
      - どちらかが "neutral" → もう一方を返す
      - 両者が異なる (bull vs bear) → 合成困難なため "neutral"
      - ファイルなし・読み込みエラー → "neutral"
    """
    if not NEXT_DAY_BIAS_FILE.exists():
        return "neutral"
    try:
        ndb = json.loads(NEXT_DAY_BIAS_FILE.read_text())
        dir_bias = ndb.get("direction_bias", "neutral")
        es_dir   = ndb.get("es_direction",   "neutral")
        if dir_bias == es_dir:
            return dir_bias
        elif dir_bias == "neutral":
            return es_dir
        elif es_dir == "neutral":
            return dir_bias
        return "neutral"  # bull vs bear → 合成困難
    except Exception as e:
        log.warning(f"[Bias] next_day_bias load error: {e}")
        return "neutral"


def premarket_assessment(mkt: MarketData, vix: float,
                          intraday_monitor: Optional['IntradayMonitor'] = None,
                          expiry: Optional[str] = None) -> dict:
    """エントリー前の環境評価。VRP・VIX・経済カレンダー・VIX9D/VVIXを統合してスコアを返す。

    VIXスコア減点はIntradayMonitorの動的閾値を使用する（固定値禁止）。
    intraday_monitorがNoneの場合はフォールバック値を使用。

    Returns:
        {"score": float, "vrp": float|None, "vix": float,
         "econ_event": bool, "recommendation": str,
         "vix9d": float|None, "vvix": float|None,
         "vix9d_vix_ratio": float|None, "vix9d_vvix_size_factor": float,
         "global_risk": dict,
         "put_call_ratio": float|None, "skew": float|None,
         "news_sentiment": dict, "size_factor": float}
    """
    score = 100.0
    result = {"vix": vix, "vrp": None, "econ_event": False, "recommendation": "proceed",
              "key_levels": None, "bias": "neutral",
              "vix9d": None, "vvix": None, "vix9d_vix_ratio": None,
              "vix9d_vvix_size_factor": 1.0,
              "global_risk": {},
              "put_call_ratio": None, "skew": None,
              "news_sentiment": {}, "size_factor": 1.0}

    # Key Level統合マップ (G1+G2+G13)
    if ENABLE_KEY_LEVELS:
        try:
            result["key_levels"] = build_key_levels(mkt, expiry)
        except Exception as _kl_e:
            log.warning(f"[Premarket] build_key_levels error: {_kl_e}")

    # P1-3: VIXコンポーネント — IntradayMonitorの動的閾値を使用（固定値禁止）
    if intraday_monitor is not None:
        vix_calm_thr     = intraday_monitor._vix_calm_threshold
        vix_elevated_thr = intraday_monitor._vix_elevated_threshold
        vix_crisis_thr   = intraday_monitor._vix_crisis_threshold
    else:
        # フォールバック（IntradayMonitor未初期化時）
        vix_calm_thr     = 15.0
        vix_elevated_thr = 22.0
        vix_crisis_thr   = 30.0

    if vix > vix_crisis_thr:
        score -= 40
    elif vix > vix_elevated_thr:
        score -= 20
    elif vix > vix_calm_thr:
        score -= 10

    log.debug(f"[Premarket] VIX thresholds (dynamic): "
              f"calm={vix_calm_thr} elevated={vix_elevated_thr} crisis={vix_crisis_thr}")

    # 前日Gap分析: ギャップ率が大きい場合はエントリー環境として不利
    result["gap_pct"] = None
    try:
        closes = mkt.get_spy_daily_closes(3)
        if len(closes) >= 2:
            prev_close = closes[-2]   # 前日終値（最新2件の古い方）
            snap = mkt.get_spy_snapshot()
            current_price = snap.get("last_price") if snap else None
            if prev_close and prev_close > 0 and current_price and current_price > 0:
                gap_pct = (current_price - prev_close) / prev_close * 100
                result["gap_pct"] = round(gap_pct, 3)
                if abs(gap_pct) >= 2.0:
                    score -= 20
                    log.info(f"[Assessment] Gap={gap_pct:+.1f}% → score adjustment -20")
                elif abs(gap_pct) >= 1.0:
                    score -= 10
                    log.info(f"[Assessment] Gap={gap_pct:+.1f}% → score adjustment -10")
                else:
                    log.debug(f"[Assessment] Gap={gap_pct:+.2f}% → no adjustment")
    except Exception as _gap_e:
        log.warning(f"[Premarket] Gap analysis error: {_gap_e}")

    # VRP (Volatility Risk Premium)
    if ENABLE_VRP_CHECK:
        vrp = mkt.calc_vrp(vix)
        result["vrp"] = vrp
        if vrp is not None:
            if vrp < 0:
                score -= VRP_NEGATIVE_PENALTY
                log.warning(f"[Premarket] VRP={vrp:.2f} NEGATIVE → premium selling unfavorable")
            elif vrp < 2:
                score -= 5
                log.info(f"[Premarket] VRP={vrp:.2f} low → reduced edge")
            else:
                log.info(f"[Premarket] VRP={vrp:.2f} → premium selling favorable")

    # 経済カレンダー（P2）
    if ECON_CALENDAR_FILE.exists():
        try:
            cal = json.loads(ECON_CALENDAR_FILE.read_text())
            today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
            events_today = [e for e in cal.get("events", [])
                           if e.get("date", "").startswith(today_str)]
            if events_today:
                high_impact = [e for e in events_today if e.get("impact") == "high"]
                if high_impact:
                    score -= 15
                    result["econ_event"] = True
                    names = ", ".join(e.get("name", "?") for e in high_impact)
                    log.info(f"[Premarket] High-impact event today: {names}")
        except Exception as e:
            log.warning(f"[Premarket] Calendar read error: {e}")

    # VIX9D / VVIX コンポーネント（Premarket Assessment統合）
    vix9d, vvix = mkt.get_vix9d_vvix()
    vix9d_vvix_factor = calc_vix9d_vvix_size_factor(vix9d, vvix, vix)
    result["vix9d"] = round(vix9d, 2) if vix9d is not None else None
    result["vvix"]  = round(vvix,  1) if vvix  is not None else None
    result["vix9d_vix_ratio"] = round(vix9d / vix, 3) if (vix9d is not None and vix > 0) else None
    result["vix9d_vvix_size_factor"] = vix9d_vvix_factor

    # グローバルリスク（日経/DAX/FTSE + 10Y-3Mスプレッド）
    try:
        gr = mkt.get_global_risk_data()
        result["global_risk"] = gr
        down_count = gr.get("down_count", 0)
        spread     = gr.get("spread_10y_3m")
        signal     = gr.get("global_risk_signal", "neutral")
        # 2/3以上のインデックスが0.5%超下落 → -10
        if down_count >= 2:
            score -= 10
            log.info(f"[Premarket] GlobalRisk: {down_count}/3 indices down>0.5% → -10")
        # 全3インデックスが1.5%超下落 → 追加-10
        chg_vals = [gr.get("nikkei_chg"), gr.get("dax_chg"), gr.get("ftse_chg")]
        if all(c is not None and c < -1.5 for c in chg_vals):
            score -= 10
            log.warning("[Premarket] GlobalRisk: ALL 3 indices down>1.5% → additional -10")
        # 10Y-3Mスプレッド逆転 (< -0.3%) → -5
        if spread is not None and spread < -0.3:
            score -= 5
            log.info(f"[Premarket] GlobalRisk: 10Y-3M spread={spread:.3f}% (inverted) → -5")
        # 2Y-10Yスプレッド: 逆転(< 0) → -5、深い逆転(< -0.5%) → 追加-5
        spread_2y = gr.get("spread_10y_2y")
        if spread_2y is not None:
            if spread_2y < -0.5:
                score -= 10
                log.warning(f"[Premarket] GlobalRisk: 10Y-2Y spread={spread_2y:.3f}% (deep inversion) → -10")
            elif spread_2y < 0:
                score -= 5
                log.info(f"[Premarket] GlobalRisk: 10Y-2Y spread={spread_2y:.3f}% (inverted) → -5")
            else:
                log.info(f"[Premarket] GlobalRisk: 10Y-2Y spread={spread_2y:.3f}% (normal)")
        log.info(f"[Premarket] GlobalRisk: signal={signal} "
                 f"Nikkei={gr.get('nikkei_chg')}% DAX={gr.get('dax_chg')}% "
                 f"FTSE={gr.get('ftse_chg')}% 10Y={gr.get('us10y')}% "
                 f"2Y={gr.get('us2y')}% 3M={gr.get('us3m')}% "
                 f"10Y-2Y={spread_2y} 10Y-3M={spread}")
    except Exception as _gr_e:
        log.warning(f"[Premarket] get_global_risk_data error: {_gr_e}")

    # ── Put/Call Ratio (G-NEW4) ───────────────────────────────────────────────
    _size_factor = 1.0
    try:
        pc_ratio = mkt.get_put_call_ratio()
        result["put_call_ratio"] = pc_ratio
        if pc_ratio is not None:
            if pc_ratio > 1.2:
                # 極端な恐怖: 売り戦術に有利（IV膨張）→ スコアを維持、サイズは縮小しない
                log.info(f"[Premarket] P/C={pc_ratio:.3f} > 1.2 (恐怖) → CS売りに有利")
            elif pc_ratio < 0.5:
                # 極端な楽観: オーバーバリュー → サイズ縮小シグナル
                score -= 5
                _size_factor *= 0.8
                log.info(f"[Premarket] P/C={pc_ratio:.3f} < 0.5 (楽観過多) → -5 size×0.8")
            else:
                log.info(f"[Premarket] P/C={pc_ratio:.3f} (通常レンジ)")
    except Exception as _pc_e:
        log.warning(f"[Premarket] put_call_ratio error: {_pc_e}")

    # ── SKEW Index (G-NEW11) ──────────────────────────────────────────────────
    try:
        skew = mkt.get_skew_index()
        result["skew"] = skew
        if skew is not None:
            if skew > 140:
                # 高テールリスク: OTMプット需要大 → CSのショートストライクをより離す
                score -= 5
                log.warning(f"[Premarket] SKEW={skew:.1f} > 140 (高テールリスク) → -5")
            else:
                log.info(f"[Premarket] SKEW={skew:.1f} (通常)")
    except Exception as _sk_e:
        log.warning(f"[Premarket] skew_index error: {_sk_e}")

    # ── News Sentiment (G-NEW8) ───────────────────────────────────────────────
    try:
        news = mkt.get_news_sentiment("SPY")
        result["news_sentiment"] = news
        if news.get("risk_level") == "high":
            score -= 10
            _size_factor *= 0.7
            log.warning(f"[Premarket] News risk=HIGH (Fed+Geo) → -10 size×0.7")
        elif news.get("risk_level") == "medium":
            score -= 5
            _size_factor *= 0.85
            log.info(f"[Premarket] News risk=MEDIUM → -5 size×0.85")
    except Exception as _nw_e:
        log.warning(f"[Premarket] news_sentiment error: {_nw_e}")

    result["size_factor"] = round(_size_factor, 4)

    # ── ^TNX Intraday Change Check ────────────────────────────────────────────
    # get_global_risk_dataで10年債利回りは取得済み。
    # 急変動(前日比±3%以上)の場合はリスク縮小シグナル（IntradayMonitor.tickでも監視）
    _us10y = result["global_risk"].get("us10y")
    if _us10y is not None:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp_tnx = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX",
                headers=headers, timeout=8,
            )
            tnx_meta = resp_tnx.json()["chart"]["result"][0]["meta"]
            tnx_prev = float(tnx_meta.get("previousClose") or tnx_meta.get("chartPreviousClose") or 0)
            if tnx_prev > 0:
                tnx_chg_pct = (_us10y - tnx_prev) / tnx_prev * 100
                result["tnx_chg_pct"] = round(tnx_chg_pct, 2)
                if abs(tnx_chg_pct) >= 3.0:
                    score -= 8
                    _size_factor *= 0.5
                    log.warning(
                        f"[Premarket] 10Y利回り急変動: {tnx_chg_pct:+.2f}% → -8 size×0.5"
                    )
        except Exception as _tnx_e:
            log.debug(f"[Premarket] tnx_chg_pct error: {_tnx_e}")

    result["size_factor"] = round(min(result["size_factor"], _size_factor), 4)

    # 推奨判定
    if score < 50:
        result["recommendation"] = "skip"
    elif score < 70:
        result["recommendation"] = "reduce_size"

    # プレマーケットバイアス（bull/bear/neutral）— next_day_bias.jsonから読み込む
    result["bias"] = _load_next_day_bias()
    log.info(f"[Premarket] bias={result['bias']} "
             f"(from next_day_bias: term_structure + ES direction)")

    result["score"] = round(score, 1)
    gr_sig = result["global_risk"].get("global_risk_signal", "N/A")
    log.info(f"[Premarket] Assessment: score={result['score']}, VRP={result['vrp']}, "
             f"econ={result['econ_event']}, rec={result['recommendation']}, "
             f"bias={result['bias']}, "
             f"VIX9D={result['vix9d']} VVIX={result['vvix']} "
             f"VIX9D/VIX={result['vix9d_vix_ratio']} size_factor={vix9d_vvix_factor} "
             f"pc_ratio={result['put_call_ratio']} skew={result['skew']} "
             f"news_risk={result['news_sentiment'].get('risk_level','N/A')} "
             f"global_risk={gr_sig}")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# ORB (Opening Range Breakout) — 買い戦略コンポーネント
# Atlas統合: spy_bot.py に momentum_bot.py の ORB ロジックを統合
# ══════════════════════════════════════════════════════════════════════════════

# ── ORB 定数 ─────────────────────────────────────────────────────────────────
ORB_PERIOD_MIN           = 5       # ORB形成時間（分）: 9:30-9:35
ORB_BREAKOUT_CUTOFF_H    = 11      # エントリー締め切り時刻(ET): 11:00
ORB_BREAKOUT_CUTOFF_M    = 0
ORB_EXIT_TIME_H          = 15      # タイムストップ時刻(ET): 15:30
ORB_EXIT_TIME_M          = 30
ORB_TP_PCT               = 1.00    # +100% (2倍)
ORB_SL_PCT               = -0.50   # -50%
ORB_MAX_RISK_PCT         = 0.02    # 口座の2%を最大リスク
ORB_MAX_QTY              = 3       # 最大3契約
ORB_MAX_CONSECUTIVE_LOSSES = 3     # 3連敗で当日停止
ORB_MAX_DAILY_LOSS_PCT   = 0.05    # 日次最大損失5%
ORB_SMALL_ACCOUNT_USD    = 15000   # この金額以下は1契約まで
ORB_VIX_MIN              = 20.0    # VIX下限（値動き不足時スキップ）
ORB_VIX_MAX              = 40.0    # VIX上限（過度な恐怖相場はスキップ）
ORB_GAP_THRESHOLD_PCT    = 2.0     # ギャップ率(%)補正閾値
ORB_GAP_BULL_SIZE_BOOST  = 1.3     # Gap方向一致 → ×1.3
ORB_PRIME_END_H          = 11      # 11:00 ET まではPrime帯
ORB_PRIME_END_M          = 0
ORB_LATE_FACTOR          = 0.7     # 11:00以降 → ×0.7
ORB_CUTOFF_H             = 12      # 12:00以降はエントリーしない
ORB_CUTOFF_M             = 0
ORB_PNL_FILE             = _BASE_DIR / "momentum_pnl.json"

# ORB VIXトレンド判定: IntradayMonitorのregimeがelevated/crisis/normalかつVIX>=20で有効
# CS売りと独立して動作（両方同時エントリー可。PortfolioRisk統合で制御）
ENABLE_ORB               = True    # グローバルON/OFF。--no-orb で False にする

# ── Calendar Spread parameters ────────────────────────────────────────────────
# エントリー条件: IVR > P75 かつ VIX > CALENDAR_VIX_MIN かつ VIX5日EMA下降傾向
# front: 0DTE CALL/PUT売り / back: CALENDAR_BACK_DAYS DTE CALL/PUT買い
# ストライク: ATM (delta≈0.50)
CALENDAR_VIX_MIN         = 20.0   # VIX下限（IV低い環境ではカレンダー優位性なし）
CALENDAR_VIX_MAX         = 50.0   # VIX上限（過度な恐怖相場はスキップ）
CALENDAR_BACK_DAYS       = 7      # backレッグDTE（SPYのweekly: 7日）
CALENDAR_ENTRY_H         = 10     # エントリー開始時刻(ET): 10:30
CALENDAR_ENTRY_M         = 30
CALENDAR_CUTOFF_H        = 12     # エントリー締め切り(ET): 12:00
CALENDAR_CUTOFF_M        = 0
CALENDAR_FORCE_CLOSE_H   = 15     # フォースクローズ時刻(ET): 15:45
CALENDAR_FORCE_CLOSE_M   = 45
CALENDAR_MAX_LOSS_PCT    = 0.30   # 最大損失30%(初期debitに対して)
CALENDAR_IV_CRUSH_PCT    = 0.10   # front IV が10%以上低下→IV crush利確
CALENDAR_MAX_RISK_PCT    = 0.02   # 口座の2%を最大リスク
CALENDAR_MAX_QTY         = 2      # 最大2契約
CALENDAR_PNL_FILE        = _BASE_DIR / "calendar_pnl.json"
ENABLE_CALENDAR          = True   # グローバルON/OFF


def _orb_load_pnl() -> list:
    """ORB PnLファイルを読み込む。"""
    try:
        if ORB_PNL_FILE.exists():
            return json.loads(ORB_PNL_FILE.read_text()).get("trades", [])
    except Exception:
        pass
    return []


def _orb_append_pnl(record: dict):
    """ORB PnLエントリーを追記する。"""
    try:
        ORB_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
        trades = _orb_load_pnl()
        record.setdefault("date", datetime.datetime.now(ET).strftime("%Y-%m-%d"))
        record.setdefault("ts",   datetime.datetime.now(ET).isoformat())
        record.setdefault("bot",  "orb_atlas")
        trades.append(record)
        ORB_PNL_FILE.write_text(json.dumps({"trades": trades}, indent=2))
    except Exception as e:
        log.warning(f"[ORB] _orb_append_pnl: {e}")


def _orb_check_consecutive_losses() -> bool:
    """直近ORB_MAX_CONSECUTIVE_LOSSES件が全て負けなら True を返す。

    バグ3修正: momentum_pnl.jsonが空またはトレード数が閾値未満の場合は
    データ不足としてFalseを返す（誤発動防止）。
    """
    try:
        trades = _orb_load_pnl()
    except Exception:
        return False  # 読み込み失敗はスキップ
    if not trades:
        return False  # データなし → 連続損失なし
    exits = [t for t in trades
             if t.get("event") == "exit" and t.get("bot") == "orb_atlas"]
    recent = exits[-ORB_MAX_CONSECUTIVE_LOSSES:]
    if len(recent) < ORB_MAX_CONSECUTIVE_LOSSES:
        return False  # データ不足 → チェックスキップ
    return all((t.get("pnl_usd", 0) or 0) < 0 for t in recent)


class ORBPosition:
    """ORBエントリー後のロングオプションポジションを管理する。"""

    def __init__(self, code: str, qty: int, entry_price: float,
                 direction: str, orb_high: float, orb_low: float):
        self.code          = code
        self.qty           = qty
        self.entry_price   = entry_price
        self.direction     = direction   # "CALL" or "PUT"
        self.orb_high      = orb_high
        self.orb_low       = orb_low
        self.orb_range     = orb_high - orb_low
        self.partial_closed = 0

    @property
    def sl_price(self) -> float:
        return self.entry_price * (1 + ORB_SL_PCT)  # 0.50倍

    @property
    def tp_price(self) -> float:
        return self.entry_price * (1 + ORB_TP_PCT)  # 2倍

    def check_exit(self, current_price: float) -> Optional[str]:
        """TP/SL到達でエグジット理由を返す。Noneはホールド継続。"""
        pnl_pct = (current_price - self.entry_price) / self.entry_price
        if pnl_pct >= ORB_TP_PCT:
            return "profit_target"
        if pnl_pct <= ORB_SL_PCT:
            return "stop_loss"
        return None


class ORBEngine:
    """SPY Opening Range Breakout 買い戦略エンジン。
    spy_bot.py の MarketData/TradeEngine/IntradayMonitor を共有する。
    SPYCreditSpreadBot から参照される。
    """

    def __init__(self, mkt: 'MarketData', eng: 'TradeEngine',
                 paper: bool = False, dry_test: bool = False):
        self.mkt       = mkt
        self.eng       = eng
        self.paper     = paper
        self.dry_test  = dry_test

        # 日次状態
        self.orb_high:    Optional[float] = None
        self.orb_low:     Optional[float] = None
        self.orb_range:   Optional[float] = None
        self.today_vix:   Optional[float] = None
        self.position:    Optional[ORBPosition] = None
        self.trade_done:  bool = False
        self.orb_checked: bool = False   # 9:35 ORB記録完了フラグ
        self.breakout_direction: Optional[str] = None  # CALL/PUT/None
        self.entry_done:  bool = False   # エントリー済みフラグ
        self._daily_loss_halted: bool = False

        # サイズ係数（premarket_checkでセット）
        self._assessment:          Optional[dict]  = None
        self._kelly_fraction:      Optional[float] = None
        self._vix9d_vvix_factor:   float           = 1.0
        self._gap_pct:             Optional[float] = None
        self._gap_size_factor:     float           = 1.0
        self._time_zone_factor:    float           = 1.0

    def reset_daily(self):
        """EODまたは日付変わり時に日次状態をリセットする。"""
        self.orb_high    = None
        self.orb_low     = None
        self.orb_range   = None
        self.today_vix   = None
        self.position    = None
        self.trade_done  = False
        self.orb_checked = False
        self.breakout_direction = None
        self.entry_done  = False
        self._daily_loss_halted = False
        self._assessment = None
        self._kelly_fraction    = None
        self._vix9d_vvix_factor = 1.0
        self._gap_pct           = None
        self._gap_size_factor   = 1.0
        self._time_zone_factor  = 1.0

    # ── Phase 1: プレマーケット環境チェック ────────────────────────────────
    def premarket_check(self, intraday_monitor: Optional['IntradayMonitor'] = None) -> bool:
        """VIX・環境スコアでORBエントリー可否を判断する。
        CS売り(premarket_assessment)の評価結果を受け取って判断するため、
        独立したAPI呼び出しは最小限に抑える。
        """
        if self.dry_test:
            self.today_vix = 22.0
            log.info(f"[ORB][DRY-TEST] premarket_check: vix={self.today_vix:.1f} → OK")
            return True

        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("[ORB] premarket_check: VIX取得失敗 → スキップ")
            return False

        self.today_vix = vix

        # VIXフィルタ: バックテスト根拠(勝率41%→67%)
        # ペーパーモードはVIX条件をバイパス（全環境でデータを収集する）
        if not self.paper:
            if vix < ORB_VIX_MIN:
                log.info(f"[ORB] Skip: VIX={vix:.2f} < {ORB_VIX_MIN} (値動き不足)")
                return False
            if vix > ORB_VIX_MAX:
                log.info(f"[ORB] Skip: VIX={vix:.2f} > {ORB_VIX_MAX} (過度な恐怖相場)")
                return False
        else:
            log.info(f"[ORB][PAPER] VIX={vix:.2f} → VIX条件バイパス（ペーパー検証モード）")

        # 環境スコアは SPYCreditSpreadBot のpremarket_assessmentから受け取る
        if self._assessment:
            score = self._assessment.get("score", 100.0)
            gap_pct = self._assessment.get("gap_pct")
            # CS売りはGap-20だがORB買いはGap+20（方向性ボーナス）
            if gap_pct is not None and abs(gap_pct) >= ORB_GAP_THRESHOLD_PCT:
                score += 20.0
            if score < DYNAMIC_ENTRY_MIN_ENV_SCORE:
                log.info(f"[ORB] Skip: 環境スコア={score:.1f} < {DYNAMIC_ENTRY_MIN_ENV_SCORE}")
                return False
            self._gap_pct = gap_pct
            vix9d_factor  = self._assessment.get("vix9d_vvix_size_factor", 1.0)
            self._vix9d_vvix_factor = vix9d_factor

        # Kellyフラクション計算
        try:
            kf = calc_kelly_fraction(ORB_PNL_FILE, lookback=20)
            self._kelly_fraction = kf
        except Exception as _ke:
            log.debug(f"[ORB] calc_kelly_fraction: {_ke}")
            self._kelly_fraction = None

        # DDチェック
        if _PORTFOLIO_RISK_AVAILABLE and self.eng:
            try:
                _cash = self.eng.get_account_cash()
                if _cash and _cash > 0:
                    if check_weekly_dd(_cash):
                        log.info("[ORB] premarket: 週次DD上限到達 → ORBスキップ")
                        return False
                    if check_monthly_dd(_cash):
                        log.info("[ORB] premarket: 月次DD上限到達 → ORBスキップ")
                        return False
            except Exception as _e:
                log.debug(f"[ORB] DDチェック失敗（無視）: {_e}")

        log.info(f"[ORB] premarket_check OK: VIX={vix:.2f}")
        return True

    # ── Phase 2: ORB記録（9:35 ETに呼び出す）──────────────────────────────
    def record_opening_range(self) -> bool:
        """9:30-9:35の5分間高値/安値を記録する。"""
        if self.dry_test:
            try:
                resp = requests.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": "SPY", "token": FINNHUB_API_KEY},
                    timeout=5,
                )
                spy_price = float(resp.json().get("c") or 0) or 560.0
            except Exception:
                spy_price = 560.0
            self.orb_high  = spy_price + 0.5
            self.orb_low   = spy_price - 0.5
            self.orb_range = 1.0
            self.orb_checked = True
            log.info(f"[ORB][DRY-TEST] ORB: H={self.orb_high:.2f} L={self.orb_low:.2f}")
            return True

        # futu or Yahoo/Finnhub から1分足取得
        bars = self._get_spy_1min_bars(minutes=10)
        if not bars:
            log.warning("[ORB] 1分足データ取得失敗")
            return False

        # 9:30-9:35 ETの足を抽出
        orb_bars = []
        now_et_dt = datetime.datetime.now(ET)
        for bar in bars:
            t_et = bar["time"].astimezone(ET)
            bar_start = t_et.replace(hour=9, minute=30, second=0, microsecond=0)
            bar_end   = t_et.replace(hour=9, minute=35, second=0, microsecond=0)
            if bar_start <= t_et < bar_end:
                orb_bars.append(bar)
        if not orb_bars:
            orb_bars = bars[-5:] if len(bars) >= 5 else bars
        if not orb_bars:
            return False

        self.orb_high  = max(bar["high"]  for bar in orb_bars)
        self.orb_low   = min(bar["low"]   for bar in orb_bars)
        self.orb_range = self.orb_high - self.orb_low
        self.orb_checked = True
        log.info(f"[ORB] Opening Range: H={self.orb_high:.2f} L={self.orb_low:.2f} "
                 f"Range={self.orb_range:.2f}")
        return True

    def _get_spy_1min_bars(self, minutes: int = 10) -> list:
        """SPY 1分足データを取得する。futu → Yahoo → Finnhub の順で試みる。"""
        if FUTU_AVAILABLE and self.mkt.quote_ctx and not self.dry_test:
            try:
                import futu as ft
                end_dt   = datetime.datetime.now(ET)
                start_dt = end_dt - datetime.timedelta(minutes=minutes + 5)
                ret, kline, _ = self.mkt.quote_ctx.request_history_kline(
                    self.mkt.underlying_code,
                    start=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    end=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    ktype=ft.KLType.K_1M,
                    max_count=minutes + 5,
                )
                if ret == 0 and not kline.empty:
                    bars = []
                    for _, row in kline.iterrows():
                        bars.append({
                            "time":  datetime.datetime.now(ET),
                            "open":  float(row.get("open", 0)),
                            "high":  float(row.get("high", 0)),
                            "low":   float(row.get("low", 0)),
                            "close": float(row.get("close", 0)),
                        })
                    return bars[-minutes:] if len(bars) > minutes else bars
            except Exception as e:
                log.debug(f"[ORB] 1min futu: {e}")

        # Yahoo Finance
        try:
            end_ts   = int(time.time())
            start_ts = end_ts - 3600
            resp = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
                params={"period1": start_ts, "period2": end_ts, "interval": "1m"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            data = resp.json()
            result_data = data["chart"]["result"][0]
            timestamps  = result_data["timestamp"]
            quotes      = result_data["indicators"]["quote"][0]
            bars = []
            for i, ts in enumerate(timestamps):
                o, h, lo, c = (quotes.get(k, [None]*len(timestamps))[i]
                               for k in ("open", "high", "low", "close"))
                if None in (o, h, lo, c):
                    continue
                bars.append({"time": datetime.datetime.fromtimestamp(ts, tz=ET),
                             "open": float(o), "high": float(h),
                             "low": float(lo), "close": float(c)})
            if bars:
                log.info(f"[ORB] 1min bars via Yahoo: {len(bars)}")
                return bars[-minutes:] if len(bars) > minutes else bars
        except Exception as e:
            log.debug(f"[ORB] 1min Yahoo: {e}")

        # Finnhub
        try:
            end_ts   = int(time.time())
            start_ts = end_ts - 3600
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/candle",
                params={"symbol": "SPY", "resolution": "1",
                        "from": start_ts, "to": end_ts,
                        "token": FINNHUB_API_KEY},
                timeout=10,
            )
            data = resp.json()
            if data.get("s") == "no_data":
                return []
            bars = []
            for i, ts in enumerate(data.get("t", [])):
                bars.append({"time": datetime.datetime.fromtimestamp(ts, tz=ET),
                             "open": float(data["o"][i]), "high": float(data["h"][i]),
                             "low":  float(data["l"][i]), "close": float(data["c"][i])})
            if bars:
                log.info(f"[ORB] 1min bars via Finnhub: {len(bars)}")
            return bars[-minutes:] if len(bars) > minutes else bars
        except Exception as e:
            log.debug(f"[ORB] 1min Finnhub: {e}")
        return []

    # ── Phase 3: ブレイクアウトチェック（毎tick呼び出す）──────────────────
    def check_breakout(self) -> Optional[str]:
        """現在のSPY価格でORBブレイクアウトを判定する。

        Returns: "CALL" / "PUT" / None
        """
        if not self.orb_checked or self.orb_high is None:
            return None
        if self.entry_done or self.trade_done:
            return None

        # カットオフ時刻チェック
        now_et = datetime.datetime.now(ET)
        if not self.dry_test:
            if (now_et.hour > ORB_BREAKOUT_CUTOFF_H or
                    (now_et.hour == ORB_BREAKOUT_CUTOFF_H
                     and now_et.minute >= ORB_BREAKOUT_CUTOFF_M)):
                return None

        spy_price = self._get_spy_price()
        if not spy_price or spy_price <= 0:
            return None

        if spy_price > self.orb_high:
            log.info(f"[ORB] CALL ブレイク: SPY={spy_price:.2f} > H={self.orb_high:.2f}")
            return "CALL"
        if spy_price < self.orb_low:
            log.info(f"[ORB] PUT ブレイク: SPY={spy_price:.2f} < L={self.orb_low:.2f}")
            return "PUT"
        return None

    def _get_spy_price(self) -> Optional[float]:
        """SPY現在価格を取得する（underlying_code非依存・SPY固定）。

        [P0 BUG修正 2026/04/12]
        self.mkt.get_spy_current() は内部で self.mkt.underlying_code を参照するため、
        MassVerifyで underlying_code が SPX 等に切替わっている場合にSPX価格を返してしまう。
        ORBエンジンはSPY固定設計のため、本メソッドは常にSPYのスナップショットを直接取得する。
        """
        if self.dry_test:
            try:
                resp = requests.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": "SPY", "token": FINNHUB_API_KEY},
                    timeout=5,
                )
                p = float(resp.json().get("c") or 0)
                return p if p > 0 else 560.0
            except Exception:
                return 560.0
        # 本番: underlying_code に関わらず US.SPY を直接スナップショット取得
        if FUTU_AVAILABLE and self.mkt and self.mkt.quote_ctx:
            _spy_cached = self.mkt._price_cache.get("US.SPY", max_age_sec=5.0)
            if _spy_cached is not None and _spy_cached > 0:
                return _spy_cached
            try:
                _ret, _snap = self.mkt.quote_ctx.get_market_snapshot(["US.SPY"])
                if _ret == RET_OK and not _snap.empty:
                    _p = float(_snap.iloc[0].get("last_price", 0) or 0)
                    if _p > 0:
                        return _p
            except Exception as _e:
                log.debug(f"[ORB] _get_spy_price snapshot: {_e}")
        # Futu失敗 → Finnhub（underlying_code非依存）
        if self.mkt:
            snap = self.mkt._get_spy_price_finnhub()
            if snap:
                return snap.get("last_price")
        return None

    # ── Phase 4: エントリー実行 ────────────────────────────────────────────
    def execute_entry(self, direction: str) -> Optional[ORBPosition]:
        """ブレイクアウト確認後にATM 0DTE オプションを買い注文する。

        [P0 BUG修正 2026/04/17] Symbol mismatch対策:
        _execute_entry_impl を呼び出し、underlying_code を必ず SPY に統一してから
        チェーン取得・発注を行う。終了時に元のunderlying_codeを復元する。
        """
        # 元のunderlying_codeを退避
        _orb_orig_underlying = (self.mkt.underlying_code if self.mkt
                                 else UNDERLYING_CODE)
        try:
            return self._execute_entry_impl(direction)
        finally:
            # underlying_codeを必ず復元
            if self.mkt and self.mkt.underlying_code != _orb_orig_underlying:
                log.info(
                    f"[ORB] underlying_code復元: {self.mkt.underlying_code} → "
                    f"{_orb_orig_underlying}"
                )
                self.mkt.underlying_code = _orb_orig_underlying

    def _execute_entry_impl(self, direction: str) -> Optional[ORBPosition]:
        """execute_entry の実装本体。underlying_code復元は呼び出し側で管理。"""
        # 市場クローズ後ガード（dry_test除く）: 16:00 ET 以降はエントリーしない（0DTE残値ゼロ）
        if not self.dry_test:
            _now_et = datetime.datetime.now(ET)
            if _now_et.hour >= 16:
                log.info(f"[ORB] execute_entry: 16:00 ET以降 ({_now_et.strftime('%H:%M')}) → エントリー中止")
                self.trade_done = True
                return None

        # バグ3修正: ORB用PnLファイルで連続損失チェック（空データ時は誤発動しない）
        if _orb_check_consecutive_losses():
            log.info(f"[ORB] 連続{ORB_MAX_CONSECUTIVE_LOSSES}敗 → 本日ORBエントリー停止")
            pushover("[ORB]", f"連続{ORB_MAX_CONSECUTIVE_LOSSES}敗 → 本日停止", priority=1)
            self.trade_done = True
            return None

        # [P0 BUG修正 2026/04/12] underlying_code を spy_price 取得前に SPY に固定する
        # [2026-04-18 修正] ORBはSPY専用戦術（内部ロジックがSPY hardcode）のため、
        # 他銘柄がSymbolSelectorで選ばれた場合は**エントリー中止**する。
        # 以前は「SPY強制切替」で他銘柄をSPYとして処理してたが、これは設計違反
        # （TSLAが選ばれてSPYのORBエントリーが発生する）。
        # ORB真マルチ銘柄対応は別リファクタで実施（SymbolSelector側でORB時はSPY限定）。
        _orb_orig_underlying = self.mkt.underlying_code
        if _orb_orig_underlying != UNDERLYING_CODE:
            log.warning(
                f"[ORB] underlying_code={_orb_orig_underlying} != SPY → "
                f"ORBマルチ銘柄未対応のためエントリー中止"
            )
            return None

        spy_price = self._get_spy_price()
        if not spy_price or spy_price <= 0:
            log.error("[ORB] execute_entry: SPY価格取得失敗")
            return None

        atm_strike = round(spy_price)

        # [P0 BUG修正 2026/04/17] Symbol mismatch防止 (underlying_code固定は上記に移動済み)
        # ORBエンジンは内部的にSPY固定ロジック（Yahoo/Finnhub SPY hardcode・OR計算SPY価格・
        # ブレイク判定SPY価格）のため、チェーン取得時もSPYに固定する。
        # SymbolSelectorが.SPX等を選んだ場合、SPXW 0DTEチェーンの先頭200件にATM付近の
        # strikeが含まれず、deep ITM/OTM strikeが誤選択されるバグがあった。
        # ORBマルチ銘柄対応は別リファクタリングで実施する（Atlas Phase 2）。

        # 時間帯係数
        entry_time = datetime.datetime.now(ET).time()
        prime_end  = datetime.time(ORB_PRIME_END_H, ORB_PRIME_END_M)
        if entry_time >= prime_end:
            self._time_zone_factor = ORB_LATE_FACTOR
        else:
            self._time_zone_factor = 1.0
        log.info(f"[ORB] Entry: SPY={spy_price:.2f} ATM={atm_strike} "
                 f"direction={direction} time_factor={self._time_zone_factor:.2f} "
                 f"underlying={self.mkt.underlying_code}")

        # Gap方向一致チェック（サイズボーナス最終確認）
        if self._gap_pct is not None and abs(self._gap_pct) >= ORB_GAP_THRESHOLD_PCT:
            gap_up  = self._gap_pct > 0
            is_call = direction == "CALL"
            if (gap_up and is_call) or (not gap_up and not is_call):
                self._gap_size_factor = ORB_GAP_BULL_SIZE_BOOST
            else:
                self._gap_size_factor = 1.0

        today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")

        # dry-testモード: 仮想ポジション
        if self.dry_test:
            virtual_price = 1.50
            virtual_code  = (f"US.SPY{datetime.datetime.now(ET).strftime('%y%m%d')}"
                             f"{'C' if direction == 'CALL' else 'P'}{int(atm_strike * 1000)}")
            cash = 10000.0
            qty  = self._calc_qty(cash, virtual_price)
            log.info(f"[ORB][DRY-TEST] Entry: {direction} {virtual_code} x{qty} "
                     f"@ ${virtual_price:.2f}")
            pushover("[ORB][DRY-TEST]", f"エントリー: {direction} ATM={atm_strike} "
                     f"x{qty} @ ${virtual_price:.2f}")
            pos = ORBPosition(virtual_code, qty, virtual_price,
                              direction, self.orb_high, self.orb_low)
            _orb_append_pnl({"event": "entry", "direction": direction,
                             "code": virtual_code, "strike": atm_strike,
                             "qty": qty, "entry_price": virtual_price,
                             "orb_high": self.orb_high, "orb_low": self.orb_low,
                             "vix": self.today_vix})
            return pos

        # 本番: VIX履歴でdelta選択
        vix = self.today_vix or 20.0
        vix_history = self.mkt.get_vix_history(days=60)
        if len(vix_history) >= 20:
            sorted_h = sorted(vix_history)
            n = len(sorted_h)
            p50 = sorted_h[int(0.50 * (n - 1))]
            p80 = sorted_h[int(0.80 * (n - 1))]
            if vix < p50:
                target_delta = 0.50
            elif vix < p80:
                target_delta = 0.60
            else:
                target_delta = 0.70
        else:
            target_delta = 0.50

        # [P0 BUG修正] center_strike=atm_strike でチェーンを現在価格周辺に絞る
        # SPXW等でチェーンが200超の場合、先頭200件制限で現在価格周辺strikeが
        # snapshot範囲外になり、誤ったdeep ITM/OTM strikeを選ぶバグがあった
        chain = self.mkt.get_option_chain_with_greeks(
            today_str, direction, center_strike=float(atm_strike))
        if not chain:
            log.error(f"[ORB] オプションチェーン取得失敗 ({direction} {today_str})")
            return None

        opt = self.mkt.find_by_delta(chain, target_delta)
        if opt is None:
            opt = self.mkt.find_by_strike(chain, float(atm_strike))
        if opt is None:
            log.error("[ORB] オプション選択失敗")
            return None

        # [P0 BUG検証] 選んだoption_strikeがATM strikeから極端に乖離していないか検証
        # ATM基準で ±15% を超える strikeは明らかに異常（チェーン範囲外・スケール不一致）
        _opt_strike = opt.get("strike_price", 0)
        _deviation = abs(_opt_strike - float(atm_strike)) / max(float(atm_strike), 1.0)
        if _deviation > 0.15:
            log.error(
                f"[ORB] strike整合性NG: option_strike={_opt_strike} vs "
                f"atm_strike={atm_strike} 乖離={_deviation*100:.1f}% "
                f"underlying={self.mkt.underlying_code} → エントリー中止"
            )
            pushover_alert(
                "[ORB] strike不整合でエントリー中止",
                f"option_strike={_opt_strike} atm={atm_strike} "
                f"underlying={self.mkt.underlying_code}",
                priority=1,
            )
            return None

        option_code  = opt["code"]
        option_strike= opt["strike_price"]
        bid_price    = opt.get("bid_price", 0)
        ask_price    = opt.get("ask_price", 0)
        mid_price    = (bid_price + ask_price) / 2 if bid_price and ask_price else None
        option_price = mid_price or opt.get("last_price", 0)

        if not option_price or option_price <= 0:
            log.error(f"[ORB] オプション価格取得失敗: {option_code}")
            return None

        cash = self.eng.get_account_cash() if self.eng else 10000.0
        qty  = self._calc_qty(cash, option_price)

        # [P0 BUG修正 2026/04/12] deep ITM 異常価格ガード
        # option_price >= 100 は deep ITM を示す（SPY 0DTE ATM は通常 $1〜$15 程度）。
        # MassVerifyでのunderlying_code切替バグ等でdeep ITMが選ばれた場合の最終防衛。
        _DEEP_ITM_THRESHOLD = 50.0  # $50以上 = deep ITM異常
        if option_price >= _DEEP_ITM_THRESHOLD:
            log.error(
                f"[ORB] deep ITM異常価格を検出 → 発注拒否: "
                f"option_price=${option_price:.2f} (threshold=${_DEEP_ITM_THRESHOLD:.0f}) "
                f"strike={option_strike} atm={atm_strike} "
                f"underlying={self.mkt.underlying_code}"
            )
            pushover_alert(
                "[ORB] deep ITM異常 → 発注拒否",
                f"price=${option_price:.2f} strike={option_strike} atm={atm_strike}",
                priority=1,
            )
            return None

        # PortfolioRiskチェック
        if _PORTFOLIO_RISK_AVAILABLE and cash and cash > 0:
            try:
                _risk = option_price * 0.50 * qty * 100
                if not can_take_risk(_risk, cash):
                    log.info("[ORB] PortfolioRisk合計リスク上限 → スキップ")
                    return None
            except Exception as _e:
                log.debug(f"[ORB] can_take_risk: {_e}")

        log.info(f"[ORB] Option: {option_code} strike={option_strike} "
                 f"delta={opt.get('delta', 0):.3f} price=${option_price:.2f} qty={qty}")

        # 発注
        order_id = None
        if FUTU_AVAILABLE and self.eng and self.eng.trade_ctx:
            high_vix   = vix > 30
            use_limit  = not high_vix and mid_price is not None
            order_id, fill_method = self.eng._place_single_leg(
                code=option_code, side=TrdSide.BUY, qty=qty,
                label=f"ORB_{direction}",
                init_price=mid_price if use_limit else None,
                use_limit=use_limit,
            )
            if order_id is None:
                log.error("[ORB] 発注失敗")
                pushover_alert("[ORB] 発注失敗", f"{direction} {option_code}", priority=1)
                return None
            log.info(f"[ORB] 発注OK: order_id={order_id}")
        else:
            log.info(f"[ORB][DRY-RUN] BUY {direction} {option_code} x{qty} @ ${option_price:.2f}")

        pos = ORBPosition(option_code, qty, option_price,
                          direction, self.orb_high, self.orb_low)

        _orb_append_pnl({"event": "entry", "direction": direction,
                         "code": option_code, "strike": option_strike,
                         "qty": qty, "entry_price": option_price,
                         "orb_high": self.orb_high, "orb_low": self.orb_low,
                         "vix": vix})

        if _PORTFOLIO_RISK_AVAILABLE:
            try:
                _pr_update_positions("orb_atlas", [{"entry_price": option_price,
                                                      "qty": qty, "direction": direction}])
            except Exception:
                pass

        pushover("[ORB]", f"エントリー: {direction} Strike={option_strike} x{qty} "
                 f"@ ${option_price:.2f}\nORB H={self.orb_high:.2f} L={self.orb_low:.2f} "
                 f"TP=${pos.tp_price:.2f} SL=${pos.sl_price:.2f}")
        return pos

    def _calc_qty(self, cash: float, option_price: float) -> int:
        """サイズ計算。Kelly/VIX9D/Gap/時間帯係数を全て適用する。"""
        kelly_frac = self._kelly_fraction
        if kelly_frac is not None and kelly_frac > 0:
            risk_pct = kelly_frac
        else:
            risk_pct = ORB_MAX_RISK_PCT

        risk = cash * risk_pct
        risk *= self._vix9d_vvix_factor
        risk *= self._time_zone_factor

        max_loss_per_contract = option_price * abs(ORB_SL_PCT) * 100
        if max_loss_per_contract <= 0:
            return 1
        qty = max(1, int(risk / max_loss_per_contract))
        qty = min(qty, ORB_MAX_QTY)
        if cash < ORB_SMALL_ACCOUNT_USD:
            qty = min(qty, 1)

        gap_factor = self._gap_size_factor
        if gap_factor > 1.0:
            boosted = min(int(qty * gap_factor), ORB_MAX_QTY)
            if boosted > qty:
                qty = boosted
        return qty

    # ── Phase 5: エグジット監視（check_orb_exit として毎tick呼び出す）──────
    def check_exit(self, intraday_monitor: Optional['IntradayMonitor'] = None) -> Optional[dict]:
        """保有ポジションのTP/SL/タイムストップを毎tickチェックする。

        Returns:
            決済完了時: {"reason": str, "exit_price": float, "pnl_usd": float}
            継続中: None
        """
        if self.position is None:
            return None

        pos = self.position
        now_et_time = datetime.datetime.now(ET).time()
        time_stop   = datetime.time(ORB_EXIT_TIME_H, ORB_EXIT_TIME_M)

        # タイムストップ
        if not self.dry_test and now_et_time >= time_stop:
            exit_price = self._get_option_price(pos) or pos.entry_price * 0.3
            log.info("[ORB] 15:30 タイムストップ")
            return self._close_position(pos, exit_price, "time_stop")

        current_price = self._get_option_price(pos)
        if not current_price or current_price <= 0:
            return None

        pnl_pct = (current_price - pos.entry_price) / pos.entry_price

        # crisis regime → 含み益があれば即利確
        if intraday_monitor is not None and not self.dry_test:
            try:
                regime = intraday_monitor.current_regime
                if regime == "crisis" and pnl_pct > 0:
                    log.warning(f"[ORB] Crisis regime: 含み益{pnl_pct:+.1%} → 即利確")
                    return self._close_position(pos, current_price, "crisis_profit_take")
            except Exception:
                pass

        reason = pos.check_exit(current_price)
        if reason:
            return self._close_position(pos, current_price, reason)
        return None

    def _get_option_price(self, pos: ORBPosition) -> Optional[float]:
        """保有オプションの現在価格を取得する。"""
        if self.dry_test:
            # 時間価値減衰シミュレーション
            now_et_dt    = datetime.datetime.now(ET)
            session_start = now_et_dt.replace(hour=9,  minute=30, second=0, microsecond=0)
            session_end   = now_et_dt.replace(hour=15, minute=30, second=0, microsecond=0)
            total_secs    = (session_end - session_start).total_seconds()
            elapsed       = max(0.0, (now_et_dt - session_start).total_seconds())
            decay         = min(elapsed / total_secs, 1.0) if total_secs > 0 else 0.5
            return round(max(pos.entry_price * (1.5 - decay), pos.entry_price * 0.1), 4)

        if not self.mkt:
            return None
        cached = self.mkt.get_cached_option_price(pos.code, max_age_sec=15.0)
        if cached and cached > 0:
            return cached
        if not FUTU_AVAILABLE or not self.mkt.quote_ctx:
            return None
        try:
            ret, snap = self.mkt.quote_ctx.get_market_snapshot([pos.code])
            if ret == 0 and not snap.empty:
                price = float(snap.iloc[0].get("last_price", 0) or 0)
                return price if price > 0 else None
        except Exception as e:
            log.debug(f"[ORB] _get_option_price: {e}")
        return None

    def _close_position(self, pos: ORBPosition,
                        exit_price: float, reason: str) -> dict:
        """ポジションを決済してPnLを記録する。"""
        remaining_qty = pos.qty - pos.partial_closed
        pnl_usd = (exit_price - pos.entry_price) * remaining_qty * 100
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0

        log.info(f"[ORB] 決済({reason}): {pos.direction} {remaining_qty}枚 "
                 f"@ ${exit_price:.2f} P&L=${pnl_usd:+.2f} ({pnl_pct:+.1%})")

        # 本番決済注文
        if not self.dry_test and FUTU_AVAILABLE and self.eng and self.eng.trade_ctx:
            try:
                ret, data = self.eng.trade_ctx.place_order(
                    price=0, qty=remaining_qty, code=pos.code,
                    trd_side=TrdSide.SELL,
                    order_type=OrderType.MARKET,
                    trd_env=self.eng.trade_env,
                    acc_id=int(self.eng.account_id or 0),
                    time_in_force=TimeInForce.DAY,
                )
                if ret != 0:
                    log.error(f"[ORB] 決済注文失敗: {data}")
                    pushover_alert("[ORB] 決済注文失敗", f"{pos.code} {reason}", priority=1)
            except Exception as e:
                log.error(f"[ORB] 決済注文例外: {e}")

        _orb_append_pnl({"event": "exit", "reason": reason,
                         "code": pos.code, "direction": pos.direction,
                         "qty": remaining_qty, "entry_price": pos.entry_price,
                         "exit_price": exit_price, "pnl_usd": round(pnl_usd, 2),
                         "pnl_pct": round(pnl_pct, 4), "vix": self.today_vix})

        event_label = {"profit_target": "TP達成", "stop_loss": "SL到達",
                       "time_stop": "タイムストップ",
                       "crisis_profit_take": "Crisis即利確"}.get(reason, reason)
        mode_label = "paper" if self.paper else "live"
        pushover("[ORB]", f"{event_label} [{mode_label}]\n"
                 f"{pos.direction} {remaining_qty}枚 @ ${exit_price:.2f}\n"
                 f"P&L: ${pnl_usd:+.2f} ({pnl_pct:+.1%})",
                 priority=1 if "stop_loss" in reason else 0)

        if _PORTFOLIO_RISK_AVAILABLE:
            try:
                _pr_clear_positions("orb_atlas")
                record_daily_pnl(datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                                 pnl_usd, "orb_atlas")
            except Exception:
                pass

        self.position  = None
        self.trade_done = True
        return {"reason": reason, "exit_price": exit_price, "pnl_usd": pnl_usd}

    # ── strategy_selector: ORBを使うべき環境か判定 ─────────────────────────
    @staticmethod
    def should_trade_today(vix: Optional[float],
                           assessment: Optional[dict] = None,
                           paper: bool = False) -> bool:
        """環境データからORBエントリーが適切かを判定する。

        設計根拠:
        - ORBはトレンド+値動きが強い日に有効。バックテストでVIX>=20の日に勝率67%。
        - CS売りとは独立して判断（VIX高い日でも方向性があればORBは有効）。
        - paper=True 時はVIX条件をバイパス（全環境での検証が目的）。

        Returns:
            True: ORBを当日実行する
            False: ORBをスキップ
        """
        if not ENABLE_ORB:
            return False
        if vix is None:
            return False
        # ペーパーモードはVIX条件をバイパス（全環境でデータを収集する）
        if not paper:
            if vix < ORB_VIX_MIN or vix > ORB_VIX_MAX:
                log.info(f"[ORB] Skip: VIX={vix:.2f} out of range [{ORB_VIX_MIN},{ORB_VIX_MAX}]")
                return False
        if assessment:
            score = assessment.get("score", 100.0)
            gap_pct = assessment.get("gap_pct")
            if gap_pct is not None and abs(gap_pct) >= ORB_GAP_THRESHOLD_PCT:
                score += 20.0
            if score < DYNAMIC_ENTRY_MIN_ENV_SCORE:
                return False
        return True


# ══════════════════════════════════════════════════════════════════════════════
# CalendarEngine — SPY Calendar Spread 戦術
# ══════════════════════════════════════════════════════════════════════════════

class CalendarPosition:
    """カレンダースプレッドのポジション管理。

    front: 0DTE売りレッグ（シータ崩壊が速い）
    back: 7DTE買いレッグ（シータ崩壊が遅い・IV crush後のバリュー保持）
    """

    def __init__(
        self,
        front_code: str,
        back_code: str,
        strike: float,
        qty: int,
        direction: str,         # "CALL" or "PUT"
        front_entry_price: float,
        back_entry_price: float,
        front_iv: float,
    ):
        self.front_code        = front_code
        self.back_code         = back_code
        self.strike            = strike
        self.qty               = qty
        self.direction         = direction
        self.front_entry_price = front_entry_price
        self.back_entry_price  = back_entry_price
        self.initial_debit     = back_entry_price - front_entry_price  # 正の値 = コスト
        self.front_iv          = front_iv          # エントリー時のfront IV（crush判定用）
        self.front_closed      = False             # front満期消滅 or 手動クローズ済み


class CalendarEngine:
    """SPY Calendar Spread（カレンダースプレッド）エンジン。

    設計根拠（Step 1-4）:
    Step 1 分解:
      - エントリー: IVRがP75以上かつVIX>20かつVIX5日傾向が下降（IVクラッシュ期待）
      - front（0DTE）を売り: シータ崩壊が極端に速い
      - back（7DTE）を買い: シータ崩壊遅い・IV crush後のvega利益を保持
      - ストライク: ATM（delta≈0.50）でvega感度最大化

    Step 2 データ:
      - IVR: MarketData.calc_ivr() / get_ivr_percentiles() で動的P75算出
      - VIX: MarketData.get_vix() + get_vix_history()でEMAトレンド
      - オプションチェーン: quote_ctx.get_option_chain()で0DTE/7DTE両方取得可能
        （futu APIはstart/endで満期範囲指定可能）
      - IV: get_market_snapshot()の option_implied_volatility

    Step 3 ルール化:
      - エントリー: IVR > ivr_high_threshold AND VIX > CALENDAR_VIX_MIN
                   AND vix_5day_ema_slope < 0（下降傾向）
      - IV crush利確: front_current_iv / front_entry_iv - 1 <= -CALENDAR_IV_CRUSH_PCT
      - Max loss: (current_debit - initial_debit) / initial_debit >= CALENDAR_MAX_LOSS_PCT
      - front満期→back単独管理: 翌日以降にbackをcloseして利確

    Step 4 課題:
      - 証拠金: futu JapanではCallやPut Calendarはスプレッド認識される可能性あり
        → ペーパーでまず確認。認識されない場合naked short扱いで証拠金高
      - back持ち越し: PDT非該当。翌日のcheck_back_leg()で決済
      - 7DTEオプション存在確認: SPYはWeekly発行あり → 通常7DTE付近のexpiryが存在する
    """

    def __init__(self, mkt: 'MarketData', eng: 'TradeEngine',
                 paper: bool = False, dry_test: bool = False):
        self.mkt      = mkt
        self.eng      = eng
        self.paper    = paper
        self.dry_test = dry_test

        # 日次状態
        self.position:      Optional[CalendarPosition] = None
        self.entry_done:    bool = False
        self.trade_done:    bool = False
        self.today_vix:     Optional[float] = None
        self._entry_attempted: bool = False

    def reset_daily(self):
        """日付変わり・EODに日次状態をリセットする。
        front満期後にbackが残存する場合はposition.front_closedをTrueにしておく。
        """
        # front消滅後のback持ち越しは翌日のcheck_back_leg()で処理
        if self.position is not None and not self.position.front_closed:
            # frontがまだ生きている状態でリセット → ポジション破棄
            log.warning("[Calendar] reset_daily: frontクローズ未完了のままリセット")
        # back持ち越し中（front_closed=True）はpositionを保持
        if self.position is None or not self.position.front_closed:
            self.position = None

        self.entry_done       = False
        self.trade_done       = False
        self.today_vix        = None
        self._entry_attempted = False

    # ── 環境チェック ──────────────────────────────────────────────────────────
    @staticmethod
    def should_trade_today(
        vix: Optional[float],
        ivr: Optional[float],
        ivr_high_threshold: float,
        vix_history: list,
        paper: bool = False,
    ) -> bool:
        """カレンダースプレッドを今日実行すべきか判定する。

        条件:
        1. ENABLE_CALENDAR が True
        2. VIX が CALENDAR_VIX_MIN〜CALENDAR_VIX_MAX の範囲内
        3. IVR が ivr_high_threshold (動的P75) 以上（IVが高い環境）
        4. VIX 5日EMA傾向が下降（IV crush期待）
        ペーパーモード(paper=True)では 2〜4 の条件をバイパス（全環境検証が目的）。

        Returns:
            True: カレンダーを当日実行する
            False: スキップ
        """
        if not ENABLE_CALENDAR:
            return False
        if vix is None:
            return False
        # ペーパーモードはVIX/IVR/トレンド条件をバイパス（全環境でデータを収集する）
        if paper:
            log.info(f"[Calendar][PAPER] VIX={vix:.2f} IVR={ivr} → 条件バイパス（ペーパー検証モード）")
            return True
        if vix < CALENDAR_VIX_MIN or vix > CALENDAR_VIX_MAX:
            return False
        if ivr is None or ivr < ivr_high_threshold:
            return False
        # VIX 5日傾向: 直近5日の終値でEMAスロープを計算
        # データが3日未満ならスキップ（過信しない）
        if len(vix_history) >= 3:
            recent = vix_history[-5:] if len(vix_history) >= 5 else vix_history[-3:]
            # 単純な線形傾向: 末尾が先頭より低ければ下降
            slope = recent[-1] - recent[0]
            if slope >= 0:
                log.info(f"[Calendar] VIXトレンド上昇(slope={slope:.2f}) → スキップ")
                return False
        return True

    # ── バックレッグexpiryを探す ──────────────────────────────────────────────
    def _find_back_expiry(self, spy_price: float) -> Optional[str]:
        """7DTE付近の最も近いexpiryを探す。
        futu get_option_chainのstart/endで範囲指定して取得する。
        dry_testでは固定値を返す。
        """
        if self.dry_test:
            target_dt = datetime.datetime.now(ET) + datetime.timedelta(days=CALENDAR_BACK_DAYS)
            # 土日は翌月曜に調整
            while target_dt.weekday() >= 5:
                target_dt += datetime.timedelta(days=1)
            return target_dt.strftime("%Y-%m-%d")

        if not FUTU_AVAILABLE or not self.mkt.quote_ctx:
            return None
        try:
            import futu as ft
            now_et = datetime.datetime.now(ET)
            # 5〜14日先の範囲でチェーンを検索
            start_dt = now_et + datetime.timedelta(days=5)
            end_dt   = now_et + datetime.timedelta(days=14)
            ret, chain_df = self.mkt.quote_ctx.get_option_chain(
                self.mkt.underlying_code,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                option_type=ft.OptionType.CALL,
            )
            if ret != 0 or chain_df.empty:
                log.warning("[Calendar] back expiry検索失敗 → fallback")
                # fallback: 7日後の平日
                target_dt = now_et + datetime.timedelta(days=CALENDAR_BACK_DAYS)
                while target_dt.weekday() >= 5:
                    target_dt += datetime.timedelta(days=1)
                return target_dt.strftime("%Y-%m-%d")
            # expiry_dateカラムが存在するか確認
            if "expiry_date" not in chain_df.columns:
                # futuはstrike_priceはあるがexpiry_dateがない場合がある
                # codeからexpiry解析 (例: "US.SPY260423C00500000" → "2026-04-23")
                def _parse_expiry(code: str) -> Optional[str]:
                    m = re.search(r'(\d{6})[CP]', code)
                    if m:
                        ds = m.group(1)
                        return f"20{ds[:2]}-{ds[2:4]}-{ds[4:]}"
                    return None
                expiries = chain_df["code"].apply(_parse_expiry).dropna().unique().tolist()
            else:
                expiries = chain_df["expiry_date"].unique().tolist()
            if not expiries:
                target_dt = now_et + datetime.timedelta(days=CALENDAR_BACK_DAYS)
                while target_dt.weekday() >= 5:
                    target_dt += datetime.timedelta(days=1)
                return target_dt.strftime("%Y-%m-%d")
            # 7DTE付近の最も近いexpiryを選ぶ
            target_date = (now_et + datetime.timedelta(days=CALENDAR_BACK_DAYS)).date()
            best = min(expiries, key=lambda d: abs(
                (datetime.datetime.strptime(d, "%Y-%m-%d").date() - target_date).days
            ))
            return best
        except Exception as e:
            log.warning(f"[Calendar] _find_back_expiry: {e}")
            return None

    # ── ATMストライクを探す ────────────────────────────────────────────────────
    def _find_atm_option(self, expiry: str, opt_type: str,
                         spy_price: float) -> Optional[dict]:
        """指定満期・方向のATMオプション（delta≈0.50）を返す。"""
        # [P0 BUG修正] center_strike=spy_price でチェーンを現在価格周辺に絞る
        chain = self.mkt.get_option_chain_with_greeks(
            expiry, opt_type, center_strike=float(spy_price))
        if not chain:
            return None
        opt = self.mkt.find_by_strike(chain, spy_price)
        # [P0 BUG検証] 選んだstrikeが現在価格から±15%超乖離なら異常
        if opt is not None and spy_price > 0:
            _s = opt.get("strike_price", 0)
            _dev = abs(_s - spy_price) / spy_price
            if _dev > 0.15:
                log.error(
                    f"[Calendar] strike整合性NG: {_s} vs underlying={spy_price:.2f} "
                    f"乖離={_dev*100:.1f}% symbol={self.mkt.underlying_code}"
                )
                return None
        return opt

    # ── エントリー実行 ────────────────────────────────────────────────────────
    def execute_entry(self, spy_price: float, vix: float) -> Optional[CalendarPosition]:
        """カレンダースプレッドを発注する。

        (1) 0DTE(front)のATMオプションを売り
        (2) 7DTE(back)のATMオプションを買い
        (3) CalendarPositionを返す

        dry_testモードでは仮想発注（実際にfutu APIを叩かない）。
        """
        now_et = datetime.datetime.now(ET)

        # dry-testモード
        if self.dry_test:
            front_expiry = now_et.strftime("%Y-%m-%d")
            back_expiry  = self._find_back_expiry(spy_price)
            direction    = "CALL" if spy_price > 0 else "CALL"  # 常にCALL（dry-test）
            atm_strike   = round(spy_price / 5) * 5  # $5丸め
            front_code   = f"DRY_FRONT_{atm_strike}C_{front_expiry}"
            back_code    = f"DRY_BACK_{atm_strike}C_{back_expiry}"
            front_price  = 0.30
            back_price   = 0.60
            front_iv     = 0.30
            qty          = 1
            log.info(
                f"[Calendar][DRY-TEST] ENTRY: {direction} strike={atm_strike} "
                f"front={front_code} back={back_code} qty={qty} "
                f"debit=${back_price - front_price:.2f}"
            )
            self._record_pnl("entry", 0, direction, atm_strike, qty)
            return CalendarPosition(
                front_code=front_code,
                back_code=back_code,
                strike=atm_strike,
                qty=qty,
                direction=direction,
                front_entry_price=front_price,
                back_entry_price=back_price,
                front_iv=front_iv,
            )

        if not FUTU_AVAILABLE or not self.mkt.quote_ctx:
            log.warning("[Calendar] execute_entry: futu未接続 → スキップ")
            return None

        # 今日の0DTEとbackのexpiryを決定
        front_expiry = now_et.strftime("%Y-%m-%d")
        back_expiry  = self._find_back_expiry(spy_price)
        if back_expiry is None:
            log.warning("[Calendar] backレッグexpiry取得失敗")
            return None

        # 方向はCALL（IVRが高い環境では方向中立 → CALLカレンダーを基本とする）
        # TODO: 市場バイアスによりPUT/CALLを切り替える拡張可能
        direction = "CALL"

        # ATMオプション取得
        front_opt = self._find_atm_option(front_expiry, direction, spy_price)
        back_opt  = self._find_atm_option(back_expiry, direction, spy_price)

        if front_opt is None or back_opt is None:
            log.warning(f"[Calendar] ATMオプション取得失敗: front={front_opt} back={back_opt}")
            return None

        front_code    = front_opt["code"]
        back_code     = back_opt["code"]
        atm_strike    = front_opt["strike_price"]
        front_ask     = front_opt.get("ask_price", 0.0)
        front_bid     = front_opt.get("bid_price", 0.0)
        back_ask      = back_opt.get("ask_price", 0.0)
        back_bid      = back_opt.get("bid_price", 0.0)
        front_iv_raw  = front_opt.get("delta", 0.0)  # delta≈0.50 確認用（IVはsnapshotから）

        # debitコストの確認
        front_mid = (front_bid + front_ask) / 2 if (front_bid + front_ask) > 0 else front_ask
        back_mid  = (back_bid + back_ask) / 2 if (back_bid + back_ask) > 0 else back_ask
        net_debit = back_mid - front_mid
        if net_debit <= 0:
            log.warning(f"[Calendar] net_debit={net_debit:.2f} <= 0 → スキップ（不合理なプライス）")
            return None

        # サイズ計算（口座資金の CALENDAR_MAX_RISK_PCT 以内）
        try:
            cash = self.eng.get_account_cash()
        except Exception:
            cash = 10000.0
        max_risk_usd = cash * CALENDAR_MAX_RISK_PCT
        # debitコスト × 100（1契約） × qty
        qty = max(1, min(CALENDAR_MAX_QTY, int(max_risk_usd / (net_debit * 100))))

        log.info(
            f"[Calendar] ENTRY: {direction} strike={atm_strike} "
            f"front={front_code} back={back_code} "
            f"front_mid={front_mid:.2f} back_mid={back_mid:.2f} "
            f"debit={net_debit:.2f} qty={qty}"
        )

        # 発注: front売り → back買い
        import futu as ft
        front_order_id, _ = self.eng._place_single_leg(
            front_code, ft.TrdSide.SELL, qty, "calendar_front",
            init_price=front_mid, use_limit=ENABLE_LIMIT_ENTRY,
        )
        if front_order_id is None:
            log.warning("[Calendar] frontレッグ発注失敗")
            return None

        time.sleep(0.5)
        back_order_id, _ = self.eng._place_single_leg(
            back_code, ft.TrdSide.BUY, qty, "calendar_back",
            init_price=back_mid, use_limit=ENABLE_LIMIT_ENTRY,
        )
        if back_order_id is None:
            # backが失敗 → frontを巻き戻す
            log.warning("[Calendar] backレッグ発注失敗 → frontを巻き戻す")
            self.eng._reverse_leg(front_code, ft.TrdSide.SELL, qty, "calendar_front_reverse")
            return None

        # IV取得（snapshot）
        front_iv = 0.30  # fallback
        try:
            greeks = self.mkt.get_option_greeks(front_code)
            front_iv = greeks.get("iv", 0.30) or 0.30
        except Exception:
            pass

        self._record_pnl("entry", 0, direction, atm_strike, qty)
        return CalendarPosition(
            front_code=front_code,
            back_code=back_code,
            strike=atm_strike,
            qty=qty,
            direction=direction,
            front_entry_price=front_mid,
            back_entry_price=back_mid,
            front_iv=front_iv,
        )

    # ── エグジット監視 ────────────────────────────────────────────────────────
    def check_exit(self, intraday_monitor: Optional['IntradayMonitor'] = None) -> Optional[dict]:
        """保有中ポジションの決済条件をチェックする。

        決済条件（front生存中）:
        1. IV crush: front IVが entry比 -10% 以上低下
        2. Max loss: debitが初期比 +30% 以上（back価値 < front価値で逆転）
        3. 15:45 ET フォースクローズ

        Returns: {"reason": str, "pnl_usd": float} or None
        """
        if self.position is None:
            return None
        pos = self.position

        now_et = datetime.datetime.now(ET)
        h, m = now_et.hour, now_et.minute

        # フォースクローズ時刻チェック（15:45 ET）
        if not self.dry_test:
            if h > CALENDAR_FORCE_CLOSE_H or (h == CALENDAR_FORCE_CLOSE_H and m >= CALENDAR_FORCE_CLOSE_M):
                return self._close_position("force_close_time")

        # dry_testモード: 起動7分後にIV crush シミュレート
        if self.dry_test:
            from_start = (now_et - getattr(self, '_dry_test_start',
                                           now_et - datetime.timedelta(minutes=10))).total_seconds() / 60.0
            if from_start >= 7.0:
                return self._close_position("iv_crush_drytest")
            return None

        # front IV確認
        if not pos.front_closed:
            try:
                greeks = self.mkt.get_option_greeks(pos.front_code)
                current_front_iv = greeks.get("iv", None)
                if current_front_iv and pos.front_iv > 0:
                    iv_change_pct = (current_front_iv - pos.front_iv) / pos.front_iv
                    if iv_change_pct <= -CALENDAR_IV_CRUSH_PCT:
                        log.info(
                            f"[Calendar] IV crush検出: iv={current_front_iv:.3f} "
                            f"entry={pos.front_iv:.3f} chg={iv_change_pct:.1%}"
                        )
                        return self._close_position("iv_crush")
            except Exception as e:
                log.debug(f"[Calendar] IV取得失敗: {e}")

        # Max loss チェック（現在のdebitコストを推定）
        try:
            front_snap = self.mkt.get_option_greeks(pos.front_code)
            back_snap  = self.mkt.get_option_greeks(pos.back_code)
            front_last = front_snap.get("last", pos.front_entry_price)
            back_last  = back_snap.get("last", pos.back_entry_price)
            current_debit = back_last - front_last
            if pos.initial_debit > 0:
                loss_pct = (current_debit - pos.initial_debit) / pos.initial_debit
                if loss_pct >= CALENDAR_MAX_LOSS_PCT:
                    log.info(
                        f"[Calendar] Max loss到達: current_debit={current_debit:.2f} "
                        f"initial={pos.initial_debit:.2f} loss={loss_pct:.1%}"
                    )
                    return self._close_position("max_loss")
        except Exception as e:
            log.debug(f"[Calendar] max loss check失敗: {e}")

        return None

    def check_back_leg(self) -> Optional[dict]:
        """front満期消滅後のback単独ポジション管理。
        front_closed=True かつ back が生きている場合に呼ぶ。
        簡易版: back lastが back_entry_price の 1.5倍 or 0.5倍で決済。
        """
        if self.position is None or not self.position.front_closed:
            return None
        pos = self.position
        try:
            back_snap = self.mkt.get_option_greeks(pos.back_code)
            back_last = back_snap.get("last", None)
            if back_last is None:
                return None
            if back_last >= pos.back_entry_price * 1.5:
                return self._close_back_only("back_profit_target")
            if back_last <= pos.back_entry_price * 0.5:
                return self._close_back_only("back_stop_loss")
        except Exception as e:
            log.debug(f"[Calendar] check_back_leg: {e}")
        return None

    def _close_position(self, reason: str) -> dict:
        """frontとbackの両レッグをクローズする。"""
        pos = self.position
        pnl_usd = 0.0

        if self.dry_test:
            pnl_usd = pos.initial_debit * pos.qty * 100 * 0.5  # dry-test: 仮想利益50%
            log.info(f"[Calendar][DRY-TEST] CLOSE: reason={reason} pnl=${pnl_usd:.2f}")
            self._record_pnl("exit", pnl_usd, pos.direction, pos.strike, pos.qty, reason)
            self.position  = None
            self.trade_done = True
            return {"reason": reason, "pnl_usd": pnl_usd}

        try:
            import futu as ft
            if not pos.front_closed:
                self.eng._place_single_leg(
                    pos.front_code, ft.TrdSide.BUY, pos.qty, "cal_front_close"
                )
            self.eng._place_single_leg(
                pos.back_code, ft.TrdSide.SELL, pos.qty, "cal_back_close"
            )
        except Exception as e:
            log.warning(f"[Calendar] _close_position: {e}")

        # PnL簡易計算（実約定価格は取得困難なので初期debitベース）
        pnl_usd = -(pos.initial_debit * pos.qty * 100)  # デフォルトは損失
        if reason == "iv_crush":
            pnl_usd = abs(pnl_usd) * 0.5  # 利益と仮定
        log.info(f"[Calendar] CLOSE: reason={reason} pnl_est=${pnl_usd:.2f}")
        self._record_pnl("exit", pnl_usd, pos.direction, pos.strike, pos.qty, reason)
        self.position  = None
        self.trade_done = True
        return {"reason": reason, "pnl_usd": pnl_usd}

    def _close_back_only(self, reason: str) -> dict:
        """backレッグのみをクローズする（front満期後）。"""
        pos = self.position
        pnl_usd = 0.0
        try:
            import futu as ft
            self.eng._place_single_leg(
                pos.back_code, ft.TrdSide.SELL, pos.qty, "cal_back_only_close"
            )
            back_snap = self.mkt.get_option_greeks(pos.back_code)
            back_last = back_snap.get("last", pos.back_entry_price)
            # back単独のP&L (front売りは満期でpremium全受け取り済み)
            front_premium_received = pos.front_entry_price * pos.qty * 100
            back_pnl = (back_last - pos.back_entry_price) * pos.qty * 100
            pnl_usd  = front_premium_received + back_pnl
        except Exception as e:
            log.warning(f"[Calendar] _close_back_only: {e}")
        log.info(f"[Calendar] BACK_CLOSE: reason={reason} pnl_est=${pnl_usd:.2f}")
        self._record_pnl("exit", pnl_usd, pos.direction, pos.strike, pos.qty, reason)
        self.position  = None
        self.trade_done = True
        return {"reason": reason, "pnl_usd": pnl_usd}

    def _record_pnl(self, event: str, pnl_usd: float, direction: str,
                    strike: float, qty: int, reason: str = "") -> None:
        """PnLをJSONファイルに記録する（ORBと同パターン）。"""
        try:
            CALENDAR_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if CALENDAR_PNL_FILE.exists():
                existing = json.loads(CALENDAR_PNL_FILE.read_text())
            trades = existing.get("trades", [])
            entry = {
                "event":     event,
                "date":      datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                "pnl_usd":   round(pnl_usd, 2),
                "direction": direction,
                "strike":    strike,
                "qty":       qty,
                "reason":    reason,
            }
            trades.append(entry)
            CALENDAR_PNL_FILE.write_text(json.dumps({"trades": trades}, indent=2))
        except Exception as e:
            log.warning(f"[Calendar] _record_pnl: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Gamma Scalp (Atlas戦術) — StraddleEngine + GammaScalpEngine
# ══════════════════════════════════════════════════════════════════════════════

# ── パラメータ定数 ─────────────────────────────────────────────────────────────
GAMMA_SCALP_VIX_MIN          = 20.0   # VIX下限（ガンマが小さい環境ではスキップ）
GAMMA_SCALP_ENTRY_H          = 9      # ストラドルエントリー開始時刻(ET)
GAMMA_SCALP_ENTRY_M          = 45
GAMMA_SCALP_CUTOFF_H         = 10     # ストラドルエントリー締め切り(ET)
GAMMA_SCALP_CUTOFF_M         = 30
GAMMA_SCALP_ATR_TRIGGER      = 0.40   # ATR(14) × この係数以上の5分変動でスキャルプ発動
GAMMA_SCALP_MAX_PER_DAY      = 5      # 1日のスキャルプ上限回数（手数料考慮）
GAMMA_SCALP_STOP_LOSS_PCT    = 0.50   # ストラドルコストの50%損失でストップ
GAMMA_SCALP_FORCE_CLOSE_H    = 15     # 強制クローズ時刻(ET): 15:30
GAMMA_SCALP_FORCE_CLOSE_M    = 30
GAMMA_SCALP_MIN_INTERVAL_MIN = 10.0   # スキャルプ間の最小インターバル（分）
GAMMA_SCALP_PNL_FILE         = _BASE_DIR / "gamma_scalp_pnl.json"
ENABLE_GAMMA_SCALP           = True   # グローバルON/OFF


def _gamma_scalp_load_pnl() -> list:
    try:
        if GAMMA_SCALP_PNL_FILE.exists():
            return json.loads(GAMMA_SCALP_PNL_FILE.read_text()).get("trades", [])
    except Exception:
        pass
    return []


def _gamma_scalp_append_pnl(record: dict):
    try:
        GAMMA_SCALP_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
        trades = _gamma_scalp_load_pnl()
        record.setdefault("date", datetime.datetime.now(ET).strftime("%Y-%m-%d"))
        record.setdefault("ts",   datetime.datetime.now(ET).isoformat())
        record.setdefault("bot",  "gamma_scalp_atlas")
        trades.append(record)
        GAMMA_SCALP_PNL_FILE.write_text(json.dumps({"trades": trades}, indent=2))
    except Exception as e:
        log.warning(f"[GammaScalp] _gamma_scalp_append_pnl: {e}")


def _calc_spy_atr14(spy_closes: list) -> Optional[float]:
    """SPY日次終値リストからATR(14)の近似値を返す。

    ATR近似: |close[i] - close[i-1]| の直近14日平均。
    データが15日未満の場合はNoneを返す。
    """
    if len(spy_closes) < 15:
        return None
    daily_ranges = [abs(spy_closes[i] - spy_closes[i - 1]) for i in range(1, len(spy_closes))]
    recent = daily_ranges[-14:]
    return sum(recent) / len(recent)


def _fetch_spy_closes_for_atr(days: int = 20) -> list:
    """Yahoo FinanceからSPY日次終値を取得する（ATR計算用）。"""
    try:
        end_ts   = int(time.time())
        start_ts = end_ts - (days + 10) * 86400
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY",
            params={"period1": start_ts, "period2": end_ts, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        closes_raw = resp.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        return [float(c) for c in closes_raw if c is not None]
    except Exception as e:
        log.warning(f"[GammaScalp] _fetch_spy_closes_for_atr: {e}")
        return []


class StraddlePosition:
    """ストラドルポジション（ATM CALL + PUT ロング）を管理するデータクラス。"""

    def __init__(self,
                 call_code: str, put_code: str,
                 call_qty: int, put_qty: int,
                 call_entry_price: float, put_entry_price: float,
                 spy_price_at_entry: float, expiry: str):
        self.call_code          = call_code
        self.put_code           = put_code
        self.call_qty           = call_qty
        self.put_qty            = put_qty
        self.call_entry_price   = call_entry_price
        self.put_entry_price    = put_entry_price
        self.spy_price_at_entry = spy_price_at_entry
        self.expiry             = expiry
        self.entry_ts           = datetime.datetime.now(ET)
        self.total_cost         = (call_entry_price * call_qty + put_entry_price * put_qty) * 100
        self.scalp_count        = 0  # スキャルプ実施回数（日次上限管理用）

    @property
    def stop_loss_threshold(self) -> float:
        return self.total_cost * GAMMA_SCALP_STOP_LOSS_PCT

    def current_pnl(self, call_current: float, put_current: float) -> float:
        call_val = call_current * self.call_qty * 100
        put_val  = put_current  * self.put_qty  * 100
        return (call_val + put_val) - self.total_cost


class StraddleEngine:
    """SPY 0DTE ストラドル（ATM CALL + PUT 買い）エントリー・エグジットエンジン。

    GammaScalpEngine がポジション管理を担当するため、このクラスは
    ストラドルのエントリー判断・発注・ポジション記録に特化する。

    条件:
      - VIX > GAMMA_SCALP_VIX_MIN (デフォルト: 20)
      - ET 9:45〜10:30 の間にエントリー
      - ATMストライクのCALL + PUT を1〜3枚ずつ購入
    """

    def __init__(self, mkt: 'MarketData', eng: 'TradeEngine',
                 paper: bool = False, dry_test: bool = False):
        self.mkt      = mkt
        self.eng      = eng
        self.paper    = paper
        self.dry_test = dry_test

        self.position:   Optional[StraddlePosition] = None
        self.entry_done: bool = False
        self.today_vix:  Optional[float] = None

    def reset_daily(self):
        self.position   = None
        self.entry_done = False
        self.today_vix  = None

    def should_enter_today(self, vix: Optional[float]) -> bool:
        """当日ストラドルエントリーを実行すべきか判定する。
        ペーパーモード(self.paper=True)の場合はVIX条件をバイパス。
        """
        if not ENABLE_GAMMA_SCALP:
            return False
        if vix is None:
            return False
        # ペーパーモードはVIX条件をバイパス（全環境でデータを収集する）
        if self.paper:
            log.info(f"[GammaScalp][PAPER] VIX={vix} → VIX条件バイパス（ペーパー検証モード）")
            return True
        return vix > GAMMA_SCALP_VIX_MIN

    def execute_entry(self) -> Optional['StraddlePosition']:
        """ATM ストラドルをエントリーする。dry_testモードはVirtualPosに登録。"""
        import math as _math

        vix = self.mkt.get_vix()
        if not self.should_enter_today(vix):
            log.info(f"[StraddleEngine] skip: VIX={vix} <= {GAMMA_SCALP_VIX_MIN}")
            return None

        self.today_vix = vix
        expiry = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        spy_price = self.mkt.get_spy_current()
        if spy_price is None or spy_price <= 0:
            log.warning("[StraddleEngine] SPY価格取得失敗 → スキップ")
            return None

        atm_strike = round(spy_price)
        cash       = self.eng.get_account_cash()

        # ストラドル1枚コスト概算: ATM IV由来プレミアム
        # 計算式: SPY * (vix/100) / 16 * sqrt(1/252) * 100 (per leg per share)
        T = 1.0 / 252.0
        est_premium_per_leg = spy_price * (vix / 100.0) / 16.0 * _math.sqrt(T) * 100
        est_cost_1lot = est_premium_per_leg * 2 * 100  # CALL + PUT × 100 shares
        max_risk      = cash * 0.05
        qty           = max(1, min(3, int(max_risk / est_cost_1lot))) if est_cost_1lot > 0 else 1

        log.info(
            f"[StraddleEngine] entry plan: SPY={spy_price:.2f} strike={atm_strike} "
            f"vix={vix:.2f} qty={qty} est_cost/lot=${est_cost_1lot:.0f}"
        )

        date_str  = datetime.datetime.now(ET).strftime("%y%m%d")
        call_code = f"US.SPY{date_str}C{int(atm_strike * 1000)}"
        put_code  = f"US.SPY{date_str}P{int(atm_strike * 1000)}"

        if self.dry_test:
            call_price = max(est_premium_per_leg / 100.0, 0.01)
            put_price  = max(est_premium_per_leg / 100.0, 0.01)
            self.eng._virtual_pos.add_position(call_code, qty, call_price, "LONG")
            self.eng._virtual_pos.add_position(put_code,  qty, put_price,  "LONG")
            pos = StraddlePosition(
                call_code=call_code, put_code=put_code,
                call_qty=qty, put_qty=qty,
                call_entry_price=call_price, put_entry_price=put_price,
                spy_price_at_entry=spy_price, expiry=expiry,
            )
            self.position   = pos
            self.entry_done = True
            log.info(
                f"[StraddleEngine][DRY-TEST] straddle entered: "
                f"CALL={call_code} PUT={put_code} qty={qty} "
                f"call_px={call_price:.4f} put_px={put_price:.4f} "
                f"total_cost=${pos.total_cost:.0f}"
            )
            _gamma_scalp_append_pnl({
                "event": "straddle_entry",
                "call_code": call_code, "put_code": put_code, "qty": qty,
                "call_entry_price": call_price, "put_entry_price": put_price,
                "spy_at_entry": spy_price, "vix": vix,
                "total_cost": pos.total_cost, "dry_test": True,
            })
            pushover("[StraddleEngine] ストラドルエントリー(DRY-TEST)",
                     f"SPY={spy_price:.2f} strike={atm_strike} qty={qty} "
                     f"コスト概算${pos.total_cost:.0f}")
            return pos

        # 本番/ペーパー
        if not FUTU_AVAILABLE or not self.eng.trade_ctx:
            log.warning("[StraddleEngine] TradeContext未接続 → スキップ")
            return None

        call_order_id, _ = self.eng._place_single_leg(
            call_code, TrdSide.BUY, qty, "straddle_call",
            init_price=None, use_limit=False,
        )
        if call_order_id is None:
            log.error("[StraddleEngine] CALL発注失敗 → ストラドルエントリー中止")
            return None

        put_order_id, _ = self.eng._place_single_leg(
            put_code, TrdSide.BUY, qty, "straddle_put",
            init_price=None, use_limit=False,
        )
        if put_order_id is None:
            log.error("[StraddleEngine] PUT発注失敗 → CALL脚のみ残留リスク")
            pushover_alert("[StraddleEngine] PUT発注失敗",
                           f"CALL={call_code}は約定済み。手動でCALL売却が必要な可能性。",
                           priority=1)
            return None

        fills     = self.eng._confirm_fills(
            [call_order_id, put_order_id], "straddle_entry", use_limit=False,
        )
        call_fill = fills.get(call_order_id) or max(est_premium_per_leg / 100.0, 0.01)
        put_fill  = fills.get(put_order_id)  or max(est_premium_per_leg / 100.0, 0.01)

        pos = StraddlePosition(
            call_code=call_code, put_code=put_code,
            call_qty=qty, put_qty=qty,
            call_entry_price=call_fill, put_entry_price=put_fill,
            spy_price_at_entry=spy_price, expiry=expiry,
        )
        self.position   = pos
        self.entry_done = True
        log.info(
            f"[StraddleEngine] straddle entered: "
            f"CALL={call_code}@{call_fill:.2f} PUT={put_code}@{put_fill:.2f} qty={qty}"
        )
        _gamma_scalp_append_pnl({
            "event": "straddle_entry",
            "call_code": call_code, "put_code": put_code, "qty": qty,
            "call_entry_price": call_fill, "put_entry_price": put_fill,
            "spy_at_entry": spy_price, "vix": vix, "total_cost": pos.total_cost,
        })
        pushover("[StraddleEngine] ストラドルエントリー",
                 f"SPY={spy_price:.2f} strike={atm_strike} qty={qty} "
                 f"CALL@{call_fill:.2f} PUT@{put_fill:.2f} コスト${pos.total_cost:.0f}")
        return pos

    def close_straddle(self, pos: 'StraddlePosition', reason: str):
        """ストラドルポジションをクローズする。"""
        if pos is None:
            return
        spy_price = self.mkt.get_spy_current() or 0.0

        if self.dry_test:
            self.eng._virtual_pos.remove_all()
            log.info(f"[StraddleEngine][DRY-TEST] straddle closed: reason={reason}")
            _gamma_scalp_append_pnl({
                "event": "straddle_exit", "reason": reason,
                "spy_at_exit": spy_price, "scalp_count": pos.scalp_count, "dry_test": True,
            })
            self.position = None
            return

        if not FUTU_AVAILABLE or not self.eng.trade_ctx:
            log.warning("[StraddleEngine] TradeContext未接続 → クローズスキップ")
            return

        for code, qty in [(pos.call_code, pos.call_qty), (pos.put_code, pos.put_qty)]:
            if qty > 0:
                self.eng._place_single_leg(
                    code, TrdSide.SELL, qty, f"straddle_close_{reason}",
                    init_price=None, use_limit=False,
                )

        log.info(f"[StraddleEngine] straddle closed: reason={reason}")
        _gamma_scalp_append_pnl({
            "event": "straddle_exit", "reason": reason,
            "spy_at_exit": spy_price, "scalp_count": pos.scalp_count,
        })
        pushover("[StraddleEngine] ストラドルクローズ",
                 f"reason={reason} SPY={spy_price:.2f} scalp_count={pos.scalp_count}")
        self.position = None


class GammaScalpEngine:
    """ガンマスキャルプエンジン。

    StraddleEngineでポジションを開いた後、このエンジンが毎tickを監視し
    SPY価格変動がATR(14)の閾値を超えたタイミングでスキャルプを実行する。

    スキャルプのロジック:
      - SPY価格が5分で上昇 → CALLを利確して新しいATM CALLを買い直し（デルタリセット）
      - SPY価格が5分で下落 → PUTを利確して新しいATM PUTを買い直し（デルタリセット）
    これにより「ガンマから利益を刈り取り、デルタをニュートラルに戻す」を繰り返す。

    PDT注意: 0DTEオプションの売買はPDT対象。ペーパーテスト推奨。
    手数料考慮: スキャルプ1回往復$2。1日上限5回（GAMMA_SCALP_MAX_PER_DAY）。
    """

    def __init__(self, straddle_eng: 'StraddleEngine',
                 mkt: 'MarketData', eng: 'TradeEngine',
                 paper: bool = False, dry_test: bool = False):
        self.straddle_eng = straddle_eng
        self.mkt          = mkt
        self.eng          = eng
        self.paper        = paper
        self.dry_test     = dry_test

        self._spy_price_history: list = []
        self._atr14:             Optional[float] = None
        self._scalp_count_today: int = 0
        self._last_scalp_ts:     Optional[datetime.datetime] = None
        self._min_scalp_interval_min: float = GAMMA_SCALP_MIN_INTERVAL_MIN

    def reset_daily(self):
        self._spy_price_history  = []
        self._atr14              = None
        self._scalp_count_today  = 0
        self._last_scalp_ts      = None

    def initialize_atr(self):
        """起動時にSPY ATR(14)を計算して保持する。"""
        closes = _fetch_spy_closes_for_atr(days=20)
        self._atr14 = _calc_spy_atr14(closes)
        if self._atr14 is not None:
            log.info(f"[GammaScalpEngine] ATR(14)={self._atr14:.2f}")
        else:
            log.warning("[GammaScalpEngine] ATR(14)計算失敗 → スキャルプは無効")

    def update_price(self, spy_price: float):
        """現在価格を履歴に追記する（60秒ごと）。直近30分分のみ保持。"""
        now    = datetime.datetime.now(ET)
        cutoff = now - datetime.timedelta(minutes=30)
        self._spy_price_history.append((now, spy_price))
        self._spy_price_history = [
            (ts, p) for ts, p in self._spy_price_history if ts >= cutoff
        ]

    def _get_5min_move(self) -> Optional[float]:
        """直近5分間のSPY価格変動（ドル、正=上昇/負=下落）を返す。"""
        now    = datetime.datetime.now(ET)
        cutoff = now - datetime.timedelta(minutes=5)
        past   = [(ts, p) for ts, p in self._spy_price_history if ts <= cutoff]
        if not past or not self._spy_price_history:
            return None
        return self._spy_price_history[-1][1] - past[-1][1]

    def monitor_gamma_opportunity(self) -> Optional[str]:
        """ガンマスキャルプの機会を監視する。

        Returns "CALL"(上昇), "PUT"(下落), または None（機会なし）。
        """
        pos = self.straddle_eng.position
        if pos is None:
            return None
        if self._scalp_count_today >= GAMMA_SCALP_MAX_PER_DAY:
            return None
        if self._last_scalp_ts is not None:
            elapsed = (datetime.datetime.now(ET) - self._last_scalp_ts).total_seconds() / 60.0
            if elapsed < self._min_scalp_interval_min:
                return None
        if self._atr14 is None:
            return None

        move = self._get_5min_move()
        if move is None:
            return None

        threshold = self._atr14 * GAMMA_SCALP_ATR_TRIGGER
        if abs(move) < threshold:
            return None

        direction = "CALL" if move > 0 else "PUT"
        log.info(
            f"[GammaScalpEngine] opportunity: move={move:+.3f} thr={threshold:.3f} "
            f"dir={direction} count={self._scalp_count_today}/{GAMMA_SCALP_MAX_PER_DAY}"
        )
        return direction

    def execute_scalp(self, direction: str) -> bool:
        """ガンマスキャルプを実行する。Returns True=成功, False=失敗/スキップ。"""
        pos = self.straddle_eng.position
        if pos is None:
            return False

        spy_price = self.mkt.get_spy_current()
        if spy_price is None or spy_price <= 0:
            return False

        new_atm_strike  = round(spy_price)
        now_et          = datetime.datetime.now(ET)
        expiry_date_str = now_et.strftime("%y%m%d")

        if direction == "CALL":
            close_code = pos.call_code
            close_qty  = pos.call_qty
            new_code   = f"US.SPY{expiry_date_str}C{int(new_atm_strike * 1000)}"
            side_label = "CALL"
        else:
            close_code = pos.put_code
            close_qty  = pos.put_qty
            new_code   = f"US.SPY{expiry_date_str}P{int(new_atm_strike * 1000)}"
            side_label = "PUT"

        log.info(
            f"[GammaScalpEngine] scalp: close {close_code} qty={close_qty} "
            f"→ open {new_code} (SPY={spy_price:.2f} strike={new_atm_strike})"
        )

        if self.dry_test:
            self._scalp_count_today += 1
            pos.scalp_count         += 1
            self._last_scalp_ts      = now_et
            if direction == "CALL":
                pos.call_code = new_code
            else:
                pos.put_code  = new_code
            log.info(
                f"[GammaScalpEngine][DRY-TEST] scalp executed: "
                f"dir={direction} count={self._scalp_count_today}"
            )
            _gamma_scalp_append_pnl({
                "event": "scalp", "direction": direction,
                "old_code": close_code, "new_code": new_code,
                "spy_price": spy_price, "new_strike": new_atm_strike,
                "scalp_count_today": self._scalp_count_today, "dry_test": True,
            })
            pushover("[GammaScalpEngine] スキャルプ実行(DRY-TEST)",
                     f"{side_label}利確→新ATM SPY={spy_price:.2f} "
                     f"strike={new_atm_strike} scalp#{self._scalp_count_today}")
            return True

        # 本番/ペーパー
        if not FUTU_AVAILABLE or not self.eng.trade_ctx:
            log.warning("[GammaScalpEngine] TradeContext未接続 → スキャルプスキップ")
            return False

        sell_id, _ = self.eng._place_single_leg(
            close_code, TrdSide.SELL, close_qty, f"gamma_scalp_close_{side_label}",
            init_price=None, use_limit=False,
        )
        if sell_id is None:
            log.warning(f"[GammaScalpEngine] {side_label}売却失敗 → スキャルプ中止")
            return False

        buy_id, _ = self.eng._place_single_leg(
            new_code, TrdSide.BUY, close_qty, f"gamma_scalp_open_{side_label}",
            init_price=None, use_limit=False,
        )
        if buy_id is None:
            log.warning(f"[GammaScalpEngine] 新{side_label}購入失敗")
            pushover_alert("[GammaScalpEngine] 新オプション購入失敗",
                           f"{side_label}売却済み・新ATM購入失敗。片脚残留リスクあり。手動確認要。",
                           priority=1)
            return False

        if direction == "CALL":
            pos.call_code = new_code
        else:
            pos.put_code  = new_code

        self._scalp_count_today += 1
        pos.scalp_count         += 1
        self._last_scalp_ts      = now_et
        log.info(
            f"[GammaScalpEngine] scalp executed: {side_label} "
            f"{close_code} → {new_code} count={self._scalp_count_today}"
        )
        _gamma_scalp_append_pnl({
            "event": "scalp", "direction": direction,
            "old_code": close_code, "new_code": new_code,
            "spy_price": spy_price, "new_strike": new_atm_strike,
            "scalp_count_today": self._scalp_count_today,
        })
        pushover("[GammaScalpEngine] スキャルプ実行",
                 f"{side_label}利確→新ATM SPY={spy_price:.2f} "
                 f"strike={new_atm_strike} scalp#{self._scalp_count_today}")
        return True

    def check_stop_loss(self) -> bool:
        """ストラドルのストップロス条件を確認する。True=発動。"""
        pos = self.straddle_eng.position
        if pos is None or self.dry_test:
            return False
        if not FUTU_AVAILABLE or not self.mkt.quote_ctx:
            return False
        try:
            codes = [pos.call_code, pos.put_code]
            ret, snap_df = self.mkt.quote_ctx.get_market_snapshot(codes)
            if ret != RET_OK or snap_df.empty:
                return False
            prices = {}
            for _, row in snap_df.iterrows():
                prices[row.get("code", "")] = float(row.get("last_price", 0) or 0)
            pnl = pos.current_pnl(
                prices.get(pos.call_code, 0.0),
                prices.get(pos.put_code,  0.0),
            )
            if pnl <= -pos.stop_loss_threshold:
                log.warning(
                    f"[GammaScalpEngine] STOP LOSS: pnl=${pnl:.2f} "
                    f"<= -${pos.stop_loss_threshold:.2f}"
                )
                return True
        except Exception as e:
            log.warning(f"[GammaScalpEngine] check_stop_loss: {e}")
        return False

    def tick(self):
        """メインループから毎tick（60秒ごと）呼ばれる処理。

        順番: 価格更新 → 強制クローズ確認 → SL確認 → スキャルプ機会確認
        """
        if not ENABLE_GAMMA_SCALP:
            return
        pos = self.straddle_eng.position
        if pos is None:
            return

        spy_price = self.mkt.get_spy_current()
        if spy_price and spy_price > 0:
            self.update_price(spy_price)

        now_et = datetime.datetime.now(ET)
        # 強制クローズ（0DTE ガンマリスク対策）
        if (now_et.hour > GAMMA_SCALP_FORCE_CLOSE_H or
                (now_et.hour == GAMMA_SCALP_FORCE_CLOSE_H
                 and now_et.minute >= GAMMA_SCALP_FORCE_CLOSE_M)):
            log.info(
                f"[GammaScalpEngine] force close at {now_et.strftime('%H:%M')} ET"
            )
            self.straddle_eng.close_straddle(pos, "force_close_time")
            return

        if self.check_stop_loss():
            self.straddle_eng.close_straddle(pos, "stop_loss")
            return

        direction = self.monitor_gamma_opportunity()
        if direction is not None:
            self.execute_scalp(direction)


# ══════════════════════════════════════════════════════════════════════════════
# StraddleBuyEngine — ATM Long Straddle + Synthetic Delta Hedge (0DTE)
# Low-IV環境エントリー型。research_straddle_design.md Step 1-4 設計準拠。
# ══════════════════════════════════════════════════════════════════════════════

STRADDLE_BUY_TP_PCT            = 0.40   # 利確: +40%
STRADDLE_BUY_SL_PCT            = -0.25  # 損切: -25%
STRADDLE_BUY_VIX_SPIKE_PCT     = 10.0  # VIX急騰(>10%/h)+含み益で即利確
STRADDLE_BUY_MAX_RISK_PCT      = 0.02  # 口座の2%を最大リスク
STRADDLE_BUY_MAX_QTY           = 3     # 最大3契約
STRADDLE_BUY_SMALL_ACCOUNT_USD = 15000 # この金額以下は1契約まで
STRADDLE_BUY_MAX_HEDGE_COUNT   = 5     # 1日最大ヘッジ回数
STRADDLE_BUY_EXIT_H            = 15    # タイムストップ 15:50 ET
STRADDLE_BUY_EXIT_M            = 50
STRADDLE_BUY_MIN_ENV_SCORE     = 60    # 環境スコア最低ライン
STRADDLE_BUY_PNL_FILE          = _BASE_DIR / "straddle_pnl.json"

# デルタヘッジバンド（VIXで動的算出。VIX高→バンド狭く）
STRADDLE_BUY_HEDGE_BAND_LOW    = 0.25  # VIX < 15
STRADDLE_BUY_HEDGE_BAND_MID    = 0.20  # VIX 15-20
STRADDLE_BUY_HEDGE_BAND_HIGH   = 0.15  # VIX 20-25
STRADDLE_BUY_HEDGE_BAND_CRISIS = 0.10  # VIX > 25

ENABLE_STRADDLE_BUY            = True  # グローバルON/OFF

# ── IV Crush Earnings 戦術定数 ───────────────────────────────────────────────────
# 決算前IV拡張→決算直前にストラドル売り→Vol Crush利確
IV_CRUSH_ENTRY_START_H     = 15    # エントリー開始 15:00 ET（引け前60分）
IV_CRUSH_ENTRY_START_M     = 0
IV_CRUSH_ENTRY_END_H       = 15    # エントリー終了 15:30 ET（引け前30分）
IV_CRUSH_ENTRY_END_M       = 30
IV_CRUSH_EXIT_H            = 9     # 翌日エグジット開始（市場オープン後）
IV_CRUSH_EXIT_M            = 45    # 9:45 ET
IV_CRUSH_EXIT_DEADLINE_H   = 10    # エグジット期限 10:15 ET
IV_CRUSH_EXIT_DEADLINE_M   = 15
IV_CRUSH_IV_PERCENTILE_MIN = 0.80  # IV が過去30日の80%ile以上でエントリー
IV_CRUSH_STOP_LOSS_PCT     = 0.10  # 10%損切り（受け取ったプレミアムの10%増加）
IV_CRUSH_PROFIT_TARGET_PCT = 0.50  # 50%利確（プレミアムの50%を利確）
IV_CRUSH_DAYS_BEFORE_MAX   = 3     # 決算前何日以内を対象にするか
IV_CRUSH_MAX_RISK_PCT      = 0.02  # 口座の2%を最大リスク
IV_CRUSH_MAX_QTY           = 2     # 最大2契約
IV_CRUSH_PNL_FILE          = _BASE_DIR / "iv_crush_pnl.json"
ENABLE_IV_CRUSH            = True  # グローバルON/OFF


def _straddle_buy_load_pnl() -> list:
    try:
        if STRADDLE_BUY_PNL_FILE.exists():
            return json.loads(STRADDLE_BUY_PNL_FILE.read_text()).get("trades", [])
    except Exception:
        pass
    return []


def _straddle_buy_append_pnl(record: dict):
    try:
        STRADDLE_BUY_PNL_FILE.parent.mkdir(parents=True, exist_ok=True)
        trades = _straddle_buy_load_pnl()
        record.setdefault("date", datetime.datetime.now(ET).strftime("%Y-%m-%d"))
        record.setdefault("ts",   datetime.datetime.now(ET).isoformat())
        record.setdefault("bot",  "straddle_buy_atlas")
        trades.append(record)
        STRADDLE_BUY_PNL_FILE.write_text(json.dumps({"trades": trades}, indent=2))
    except Exception as e:
        log.warning(f"[STRADDLE_BUY] _straddle_buy_append_pnl: {e}")


class StraddleBuyPosition:
    """ATM Long Straddle (CALL+PUT) のポジションを管理する。"""

    def __init__(self, call_code: str, put_code: str,
                 qty: int, call_price: float, put_price: float, strike: float):
        self.call_code   = call_code
        self.put_code    = put_code
        self.qty         = qty
        self.call_price  = call_price
        self.put_price   = put_price
        self.entry_cost  = (call_price + put_price) * qty * 100
        self.strike      = strike
        self.hedge_count = 0
        self.hedge_legs: dict = {}  # {option_code: qty}

    @property
    def entry_price_per_unit(self) -> float:
        """1ユニット（CALL+PUT）のエントリー価格合計。"""
        return self.call_price + self.put_price


class StraddleBuyEngine:
    """SPY ATM Long Straddle + Synthetic Delta Hedge エンジン（Low-IV環境型）。

    ORBEngineと同じ4フェーズ構造:
      Phase 1: premarket_check()  — IVR < P25 + env_score チェック
      Phase 2: execute_entry()    — ATM CALL+PUT 同時買い発注
      Phase 3: check_exit()       — TP/SL/タイムストップ/VIX急騰監視（毎tick）
      Phase 4: check_hedge()      — シンセティックデルタヘッジ（毎tick）

    シンセティックヘッジ:
      デリバティブ専用口座のためSPY株売買不可。
      ポートフォリオデルタが±HEDGE_BANDを超えたらATMオプション追加発注でデルタ調整。
      delta > +BAND → 追加PUT買い（デルタを下げる）
      delta < -BAND → 追加CALL買い（デルタを上げる）

    設計根拠 (research_straddle_design.md Step 1-4参照):
      - VIX低い時（IVR < P25）に安くストラドルを仕込む
      - VIX急騰（+10%/h以上）で即利確、静かな日はシータ損切
      - 0DTEガンマスキャルピングではなくLow-IVエントリー戦術
    """

    def __init__(self, mkt: 'MarketData', eng: 'TradeEngine',
                 paper: bool = False, dry_test: bool = False):
        self.mkt      = mkt
        self.eng      = eng
        self.paper    = paper
        self.dry_test = dry_test

        self.today_vix:      Optional[float]             = None
        self.position:       Optional[StraddleBuyPosition] = None
        self.trade_done:     bool                        = False
        self.entry_done:     bool                        = False
        self._assessment:    Optional[dict]              = None
        self._kelly_fraction: Optional[float]            = None
        self._vix_prev:      Optional[float]             = None
        self._vix_check_ts:  Optional[datetime.datetime] = None

    def reset_daily(self):
        self.today_vix       = None
        self.position        = None
        self.trade_done      = False
        self.entry_done      = False
        self._assessment     = None
        self._kelly_fraction = None
        self._vix_prev       = None
        self._vix_check_ts   = None

    # ── Phase 1: プレマーケット環境チェック ────────────────────────────────
    def premarket_check(self) -> bool:
        """IVR (VIX が60日P25以下) + env_score でエントリー可否を判断する。"""
        if not ENABLE_STRADDLE_BUY:
            return False
        if self.dry_test:
            self.today_vix = 16.0
            log.info("[STRADDLE_BUY][DRY-TEST] premarket_check OK: vix=16.0")
            return True

        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("[STRADDLE_BUY] premarket_check: VIX取得失敗")
            return False
        self.today_vix     = vix
        self._vix_prev     = vix
        self._vix_check_ts = datetime.datetime.now(ET)

        # IVR条件: VIX が60日履歴のP25以下
        vix_history = self.mkt.get_vix_history(days=60)
        if len(vix_history) >= 20:
            sorted_h = sorted(vix_history)
            p25      = sorted_h[int(0.25 * (len(sorted_h) - 1))]
            ivr_ok   = vix <= p25
        else:
            ivr_ok = vix < 18.0  # フォールバック
        if not ivr_ok:
            log.info(f"[STRADDLE_BUY] Skip: VIX={vix:.2f} > P25 (IVR高すぎ → コスト高)")
            return False

        # 環境スコア
        if self._assessment:
            score = self._assessment.get("score", 100.0)
            if score < STRADDLE_BUY_MIN_ENV_SCORE:
                log.info(f"[STRADDLE_BUY] Skip: env_score={score:.1f} < {STRADDLE_BUY_MIN_ENV_SCORE}")
                return False

        # Kelly
        try:
            self._kelly_fraction = calc_kelly_fraction(STRADDLE_BUY_PNL_FILE, lookback=20)
        except Exception:
            self._kelly_fraction = None

        # DD
        if _PORTFOLIO_RISK_AVAILABLE and self.eng:
            try:
                _cash = self.eng.get_account_cash()
                if _cash and _cash > 0:
                    if check_weekly_dd(_cash) or check_monthly_dd(_cash):
                        log.info("[STRADDLE_BUY] premarket: DD上限到達 → スキップ")
                        return False
            except Exception:
                pass

        log.info(f"[STRADDLE_BUY] premarket_check OK: VIX={vix:.2f} (P25以下)")
        return True

    # ── Phase 2: エントリー実行 ────────────────────────────────────────────
    def execute_entry(self) -> Optional[StraddleBuyPosition]:
        """ATM 0DTE CALL + PUT を同時買い発注する。マルチ銘柄対応。"""
        underlying = self.mkt.underlying_code  # _try_mass_verify_entryで切替済み
        sym_ticker = underlying.replace("US.", "").replace(".", "")  # "SPY", "QQQ", "META" 等
        spy_price = self._get_underlying_price()
        if not spy_price or spy_price <= 0:
            log.error(f"[STRADDLE_BUY] execute_entry: {sym_ticker}価格取得失敗")
            return None

        # strike_interval は symbol_params.json の銘柄別設定を使用（例: SPY=1.0, SPXW=5.0）
        _interval = get_symbol_meta(underlying).get("strike_interval") or 1.0
        # ATM = 最近傍の刻みに丸め（SPY 560.3→560, SPXW 5410→5410）
        atm_strike = round(spy_price / _interval) * _interval
        today_str  = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        log.info(f"[STRADDLE_BUY] Entry: {sym_ticker}={spy_price:.2f} ATM={atm_strike} interval={_interval}")

        if self.dry_test:
            call_price   = 1.80
            put_price    = 1.80
            _dt_str      = datetime.datetime.now(ET).strftime('%y%m%d')
            virtual_call = f"US.{sym_ticker}{_dt_str}C{int(atm_strike * 1000)}"
            virtual_put  = f"US.{sym_ticker}{_dt_str}P{int(atm_strike * 1000)}"
            cash = 10000.0
            qty  = self._calc_qty(cash, call_price + put_price)
            log.info(f"[STRADDLE_BUY][DRY-TEST] {virtual_call}/{virtual_put} x{qty}")
            pushover("[STRADDLE_BUY][DRY-TEST]",
                     f"エントリー: ATM={atm_strike} x{qty} "
                     f"C${call_price:.2f}+P${put_price:.2f}")
            pos = StraddleBuyPosition(virtual_call, virtual_put, qty,
                                      call_price, put_price, float(atm_strike))
            _straddle_buy_append_pnl({"event": "entry", "strike": atm_strike, "qty": qty,
                                      "call_price": call_price, "put_price": put_price,
                                      "entry_cost": pos.entry_cost, "vix": self.today_vix})
            return pos

        # [P0 BUG修正] center_strike=atm_strike でチェーンを現在価格周辺に絞る
        call_chain = self.mkt.get_option_chain_with_greeks(
            today_str, "CALL", center_strike=float(atm_strike))
        put_chain  = self.mkt.get_option_chain_with_greeks(
            today_str, "PUT", center_strike=float(atm_strike))
        if not call_chain or not put_chain:
            log.error("[STRADDLE_BUY] オプションチェーン取得失敗")
            return None

        call_opt = (self.mkt.find_by_delta(call_chain, 0.50)
                    or self.mkt.find_by_strike(call_chain, float(atm_strike)))
        put_opt  = (self.mkt.find_by_delta(put_chain,  0.50)
                    or self.mkt.find_by_strike(put_chain,  float(atm_strike)))
        if call_opt is None or put_opt is None:
            log.error("[STRADDLE_BUY] ATM オプション選択失敗")
            return None

        # [P0 BUG検証] strike整合性チェック（ATM基準 ±15%超は異常）
        for _tag, _opt in [("CALL", call_opt), ("PUT", put_opt)]:
            _s = _opt.get("strike_price", 0)
            _dev = abs(_s - float(atm_strike)) / max(float(atm_strike), 1.0)
            if _dev > 0.15:
                log.error(
                    f"[STRADDLE_BUY] {_tag} strike整合性NG: {_s} vs ATM {atm_strike} "
                    f"乖離={_dev*100:.1f}% underlying={self.mkt.underlying_code}"
                )
                # priority=0: chain±20%フィルタ通過後の残余バグのみ到達（静音）
                pushover_alert(
                    "[STRADDLE_BUY] strike不整合",
                    f"{_tag} strike={_s} atm={atm_strike}",
                    priority=0,
                )
                return None

        call_code = call_opt["code"]
        put_code  = put_opt["code"]
        call_mid  = ((call_opt.get("bid_price", 0) + call_opt.get("ask_price", 0)) / 2
                     or call_opt.get("last_price", 0))
        put_mid   = ((put_opt.get("bid_price", 0) + put_opt.get("ask_price", 0)) / 2
                     or put_opt.get("last_price", 0))

        if not call_mid or not put_mid or call_mid <= 0 or put_mid <= 0:
            log.error(f"[STRADDLE_BUY] 価格取得失敗 CALL={call_mid} PUT={put_mid}")
            return None

        cash = self.eng.get_account_cash() if self.eng else 10000.0
        qty  = self._calc_qty(cash, call_mid + put_mid)

        if _PORTFOLIO_RISK_AVAILABLE and cash and cash > 0:
            try:
                if not can_take_risk((call_mid + put_mid) * qty * 100, cash):
                    log.info("[STRADDLE_BUY] PortfolioRisk上限 → スキップ")
                    return None
            except Exception:
                pass

        log.info(f"[STRADDLE_BUY] CALL:{call_code} ${call_mid:.2f} "
                 f"PUT:{put_code} ${put_mid:.2f} qty={qty}")

        if FUTU_AVAILABLE and self.eng and self.eng.trade_ctx:
            use_limit = (self.today_vix or 20.0) <= 30
            call_oid, _ = self.eng._place_single_leg(
                code=call_code, side=TrdSide.BUY, qty=qty, label="STRADDLE_BUY_CALL",
                init_price=call_mid if use_limit else None, use_limit=use_limit)
            if call_oid is None:
                log.error("[STRADDLE_BUY] CALL発注失敗")
                pushover_alert("[STRADDLE_BUY] CALL発注失敗", call_code, priority=1)
                return None
            put_oid, _ = self.eng._place_single_leg(
                code=put_code, side=TrdSide.BUY, qty=qty, label="STRADDLE_BUY_PUT",
                init_price=put_mid if use_limit else None, use_limit=use_limit)
            if put_oid is None:
                log.error("[STRADDLE_BUY] PUT発注失敗（CALL約定済み）")
                pushover_alert("[STRADDLE_BUY] PUT発注失敗",
                               f"CALL {call_code} 約定済。手動確認要", priority=1)
                return None
            log.info(f"[STRADDLE_BUY] 発注OK: CALL={call_oid} PUT={put_oid}")
        else:
            log.info(f"[STRADDLE_BUY][DRY-RUN] BUY CALL {call_code} x{qty} @ ${call_mid:.2f}")
            log.info(f"[STRADDLE_BUY][DRY-RUN] BUY PUT  {put_code}  x{qty} @ ${put_mid:.2f}")

        pos = StraddleBuyPosition(call_code, put_code, qty,
                                  call_mid, put_mid, float(atm_strike))
        _straddle_buy_append_pnl({"event": "entry", "strike": atm_strike, "qty": qty,
                                   "call_code": call_code, "put_code": put_code,
                                   "call_price": call_mid, "put_price": put_mid,
                                   "entry_cost": pos.entry_cost, "vix": self.today_vix})

        if _PORTFOLIO_RISK_AVAILABLE:
            try:
                _pr_update_positions("straddle_buy_atlas",
                                     [{"entry_price": call_mid + put_mid,
                                       "qty": qty, "direction": "STRADDLE_BUY"}])
            except Exception:
                pass

        pushover("[STRADDLE_BUY]",
                 f"エントリー: ATM={atm_strike} x{qty}\n"
                 f"C${call_mid:.2f}+P${put_mid:.2f}=${call_mid+put_mid:.2f}\n"
                 f"TP:+{STRADDLE_BUY_TP_PCT:.0%} SL:{STRADDLE_BUY_SL_PCT:.0%}")
        return pos

    def _calc_qty(self, cash: float, straddle_cost: float) -> int:
        risk_pct = (self._kelly_fraction
                    if self._kelly_fraction and self._kelly_fraction > 0
                    else STRADDLE_BUY_MAX_RISK_PCT)
        max_loss = straddle_cost * 100
        if max_loss <= 0:
            return 1
        qty = max(1, int(cash * risk_pct / max_loss))
        qty = min(qty, STRADDLE_BUY_MAX_QTY)
        if cash < STRADDLE_BUY_SMALL_ACCOUNT_USD:
            qty = min(qty, 1)
        return qty

    def _get_underlying_price(self) -> Optional[float]:
        """現在の mkt.underlying_code の価格を取得する（マルチ銘柄対応）。"""
        underlying = self.mkt.underlying_code  # _try_mass_verify_entryで切替済み
        sym_ticker = underlying.replace("US.", "").replace(".", "")  # "SPY", "QQQ" 等
        if self.dry_test:
            try:
                resp = requests.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": sym_ticker, "token": FINNHUB_API_KEY}, timeout=5)
                p = float(resp.json().get("c") or 0)
                # フォールバック: 銘柄別デフォルト価格
                _fallback = {"SPY": 560.0, "QQQ": 480.0, "IWM": 200.0,
                             "TSLA": 250.0, "NVDA": 900.0, "AAPL": 200.0,
                             "MSFT": 420.0, "AMZN": 200.0, "META": 600.0,
                             "GOOGL": 170.0}.get(sym_ticker, 300.0)
                return p if p > 0 else _fallback
            except Exception:
                return {"SPY": 560.0, "QQQ": 480.0, "IWM": 200.0,
                        "TSLA": 250.0, "NVDA": 900.0, "AAPL": 200.0,
                        "MSFT": 420.0, "AMZN": 200.0, "META": 600.0,
                        "GOOGL": 170.0}.get(sym_ticker, 300.0)
        return self.mkt.get_spy_current()  # underlying_code切替済みのため銘柄別に動作

    def _get_straddle_current_value(self, pos: StraddleBuyPosition) -> Optional[float]:
        """ストラドルの現在価値（CALL+PUT合計）を1枚あたりで返す。"""
        if self.dry_test:
            now_et        = datetime.datetime.now(ET)
            session_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            session_end   = now_et.replace(hour=15, minute=30, second=0, microsecond=0)
            total_secs    = (session_end - session_start).total_seconds()
            elapsed       = max(0.0, (now_et - session_start).total_seconds())
            decay         = min(elapsed / total_secs, 1.0) if total_secs > 0 else 0.5
            entry_unit    = pos.entry_price_per_unit
            return round(max(entry_unit * (1.3 - decay), entry_unit * 0.1), 4)

        if not self.mkt:
            return None
        call_p = self.mkt.get_cached_option_price(pos.call_code, max_age_sec=15.0)
        put_p  = self.mkt.get_cached_option_price(pos.put_code,  max_age_sec=15.0)
        if call_p and put_p and call_p > 0 and put_p > 0:
            return call_p + put_p
        if not FUTU_AVAILABLE or not self.mkt.quote_ctx:
            return None
        try:
            ret, snap = self.mkt.quote_ctx.get_market_snapshot(
                [pos.call_code, pos.put_code])
            if ret == 0 and not snap.empty:
                prices = {str(row.get("code", "")): float(row.get("last_price", 0) or 0)
                          for _, row in snap.iterrows()}
                c = prices.get(pos.call_code)
                p = prices.get(pos.put_code)
                if c and p and c > 0 and p > 0:
                    return c + p
        except Exception as e:
            log.debug(f"[STRADDLE_BUY] _get_straddle_current_value: {e}")
        return None

    # ── Phase 3: エグジット監視（毎tick）──────────────────────────────────
    def check_exit(self) -> Optional[dict]:
        """TP/SL/タイムストップ/VIX急騰を毎tickチェックする。

        Returns:
            決済完了時: {"reason": str, "exit_value": float, "pnl_usd": float}
            継続中: None
        """
        if self.position is None:
            return None

        pos         = self.position
        now_et_time = datetime.datetime.now(ET).time()
        time_stop   = datetime.time(STRADDLE_BUY_EXIT_H, STRADDLE_BUY_EXIT_M)

        if not self.dry_test and now_et_time >= time_stop:
            cv = self._get_straddle_current_value(pos) or pos.entry_price_per_unit * 0.3
            log.info("[STRADDLE_BUY] 15:50 タイムストップ")
            return self._close_position(pos, cv, "time_stop")

        cv = self._get_straddle_current_value(pos)
        if not cv or cv <= 0:
            return None

        pnl_pct = (cv - pos.entry_price_per_unit) / pos.entry_price_per_unit

        # VIX急騰時利確
        if not self.dry_test:
            vix_now = self.mkt.get_vix()
            if vix_now and self._vix_prev and self._vix_check_ts:
                elapsed_h = ((datetime.datetime.now(ET) - self._vix_check_ts)
                             .total_seconds() / 3600.0)
                if elapsed_h > 0:
                    vix_chg = (vix_now - self._vix_prev) / self._vix_prev * 100.0 / elapsed_h
                    if vix_chg > STRADDLE_BUY_VIX_SPIKE_PCT and pnl_pct > 0:
                        log.warning(f"[STRADDLE_BUY] VIX急騰({vix_chg:.1f}%/h) → 即利確")
                        return self._close_position(pos, cv, "vix_spike_profit_take")
                if elapsed_h >= 1.0:
                    self._vix_prev     = vix_now
                    self._vix_check_ts = datetime.datetime.now(ET)

        if pnl_pct >= STRADDLE_BUY_TP_PCT:
            return self._close_position(pos, cv, "profit_target")
        if pnl_pct <= STRADDLE_BUY_SL_PCT:
            return self._close_position(pos, cv, "stop_loss")
        return None

    def _close_position(self, pos: StraddleBuyPosition,
                        exit_value: float, reason: str) -> dict:
        pnl_usd = (exit_value - pos.entry_price_per_unit) * pos.qty * 100
        pnl_pct = ((exit_value - pos.entry_price_per_unit) / pos.entry_price_per_unit
                   if pos.entry_price_per_unit else 0)
        log.info(f"[STRADDLE_BUY] 決済({reason}): {pos.qty}枚 "
                 f"@ ${exit_value:.2f} P&L=${pnl_usd:+.2f} ({pnl_pct:+.1%})")

        if not self.dry_test and FUTU_AVAILABLE and self.eng and self.eng.trade_ctx:
            for code, label in [(pos.call_code, "CALL"), (pos.put_code, "PUT")]:
                try:
                    ret, data = self.eng.trade_ctx.place_order(
                        price=0, qty=pos.qty, code=code,
                        trd_side=TrdSide.SELL, order_type=OrderType.MARKET,
                        trd_env=self.eng.trade_env,
                        acc_id=int(self.eng.account_id or 0),
                        time_in_force=TimeInForce.DAY,
                    )
                    if ret != 0:
                        log.error(f"[STRADDLE_BUY] {label}決済失敗: {data}")
                        pushover_alert(f"[STRADDLE_BUY] {label}決済失敗",
                                       f"{code} {reason}", priority=1)
                except Exception as e:
                    log.error(f"[STRADDLE_BUY] {label}決済例外: {e}")
            for h_code, h_qty in pos.hedge_legs.items():
                if h_qty <= 0:
                    continue
                try:
                    self.eng.trade_ctx.place_order(
                        price=0, qty=abs(h_qty), code=h_code,
                        trd_side=TrdSide.SELL, order_type=OrderType.MARKET,
                        trd_env=self.eng.trade_env,
                        acc_id=int(self.eng.account_id or 0),
                        time_in_force=TimeInForce.DAY,
                    )
                except Exception as e:
                    log.error(f"[STRADDLE_BUY] ヘッジレッグ決済例外 {h_code}: {e}")

        _straddle_buy_append_pnl({"event": "exit", "reason": reason,
                                   "call_code": pos.call_code, "put_code": pos.put_code,
                                   "qty": pos.qty, "entry_cost": pos.entry_cost,
                                   "exit_value": round(exit_value, 4),
                                   "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_pct, 4),
                                   "hedge_count": pos.hedge_count, "vix": self.today_vix})

        event_label = {"profit_target": "TP達成", "stop_loss": "SL到達",
                       "time_stop": "タイムストップ",
                       "vix_spike_profit_take": "VIX急騰利確"}.get(reason, reason)
        pushover("[STRADDLE_BUY]",
                 f"{event_label} [{'paper' if self.paper else 'live'}]\n"
                 f"{pos.qty}枚 @ ${exit_value:.2f} P&L:${pnl_usd:+.2f} ({pnl_pct:+.1%})",
                 priority=1 if "stop_loss" in reason else 0)

        if _PORTFOLIO_RISK_AVAILABLE:
            try:
                _pr_clear_positions("straddle_buy_atlas")
                record_daily_pnl(datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                                 pnl_usd, "straddle_buy_atlas")
            except Exception:
                pass

        self.position   = None
        self.trade_done = True
        return {"reason": reason, "exit_value": exit_value, "pnl_usd": pnl_usd}

    # ── Phase 4: シンセティックデルタヘッジ（毎tick）─────────────────────
    def check_hedge(self) -> bool:
        """デルタが ±HEDGE_BAND を超えたらATMオプション追加発注でデルタ調整する。"""
        if self.position is None:
            return False
        pos = self.position
        if pos.hedge_count >= STRADDLE_BUY_MAX_HEDGE_COUNT:
            return False

        vix        = self.today_vix or 20.0
        if not self.dry_test:
            vix = self.mkt.get_vix() or vix
        hedge_band = self._calc_hedge_band(vix)

        portfolio_delta = self._get_portfolio_delta(pos)
        if portfolio_delta is None:
            return False
        if abs(portfolio_delta) <= hedge_band:
            return False

        log.info(f"[STRADDLE_BUY][HEDGE] delta={portfolio_delta:+.3f} "
                 f"band={hedge_band:.2f} ({pos.hedge_count+1}/{STRADDLE_BUY_MAX_HEDGE_COUNT})")

        if self.dry_test:
            direction = "PUT" if portfolio_delta > 0 else "CALL"
            log.info(f"[STRADDLE_BUY][DRY-TEST][HEDGE] 追加{direction} delta={portfolio_delta:+.3f}")
            pos.hedge_count += 1
            _straddle_buy_append_pnl({"event": "hedge", "direction": direction,
                                       "portfolio_delta": round(portfolio_delta, 4),
                                       "hedge_band": hedge_band,
                                       "hedge_count": pos.hedge_count})
            return True

        today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        direction = "PUT" if portfolio_delta > 0 else "CALL"
        spy_price = self._get_underlying_price() or pos.strike
        # [P0 BUG修正] center_strike=現在価格 でチェーンを周辺に絞る
        chain     = self.mkt.get_option_chain_with_greeks(
            today_str, direction, center_strike=float(spy_price))
        if not chain:
            log.warning(f"[STRADDLE_BUY][HEDGE] {direction}チェーン取得失敗")
            return False

        hedge_opt = (self.mkt.find_by_delta(chain, 0.50)
                     or self.mkt.find_by_strike(chain, spy_price))
        if hedge_opt is None:
            log.warning("[STRADDLE_BUY][HEDGE] ヘッジオプション選択失敗")
            return False

        # [P0 BUG検証] strike整合性チェック
        _hs = hedge_opt.get("strike_price", 0)
        if spy_price > 0 and abs(_hs - spy_price) / spy_price > 0.15:
            log.error(
                f"[STRADDLE_BUY][HEDGE] strike整合性NG: {_hs} vs "
                f"underlying={spy_price:.2f} symbol={self.mkt.underlying_code}"
            )
            return False

        h_code = hedge_opt["code"]
        h_bid  = hedge_opt.get("bid_price", 0)
        h_ask  = hedge_opt.get("ask_price", 0)
        h_mid  = (h_bid + h_ask) / 2 if h_bid and h_ask else hedge_opt.get("last_price", 0)
        if not h_mid or h_mid <= 0:
            log.warning("[STRADDLE_BUY][HEDGE] ヘッジ価格取得失敗")
            return False

        hedge_qty = 1
        if FUTU_AVAILABLE and self.eng and self.eng.trade_ctx:
            order_id, _ = self.eng._place_single_leg(
                code=h_code, side=TrdSide.BUY, qty=hedge_qty,
                label=f"STRADDLE_BUY_HEDGE_{direction}",
                init_price=h_mid, use_limit=True)
            if order_id is None:
                log.warning(f"[STRADDLE_BUY][HEDGE] 発注失敗: {h_code}")
                return False
        else:
            log.info(f"[STRADDLE_BUY][DRY-RUN][HEDGE] BUY {direction} {h_code} "
                     f"x{hedge_qty} @ ${h_mid:.2f}")

        pos.hedge_count += 1
        pos.hedge_legs[h_code] = pos.hedge_legs.get(h_code, 0) + hedge_qty
        _straddle_buy_append_pnl({"event": "hedge", "direction": direction,
                                   "code": h_code, "qty": hedge_qty, "price": h_mid,
                                   "portfolio_delta": round(portfolio_delta, 4),
                                   "hedge_band": hedge_band,
                                   "hedge_count": pos.hedge_count})
        log.info(f"[STRADDLE_BUY][HEDGE] 完了: {direction} {h_code} x{hedge_qty} "
                 f"@ ${h_mid:.2f} 回数={pos.hedge_count}")
        return True

    def _calc_hedge_band(self, vix: float) -> float:
        """VIX水準からヘッジバンド幅を動的算出する（VIX高→バンド狭く）。"""
        if vix < 15:
            return STRADDLE_BUY_HEDGE_BAND_LOW
        elif vix < 20:
            return STRADDLE_BUY_HEDGE_BAND_MID
        elif vix < 25:
            return STRADDLE_BUY_HEDGE_BAND_HIGH
        else:
            return STRADDLE_BUY_HEDGE_BAND_CRISIS

    def _get_portfolio_delta(self, pos: StraddleBuyPosition) -> Optional[float]:
        """ポートフォリオデルタを取得する。greeks_monitor → futu snapshot の優先順。"""
        if self.dry_test:
            return 0.22  # シミュレーション: PUT方向ヘッジを発動させる値

        if _GREEKS_MONITOR_AVAILABLE and self.mkt and self.mkt.quote_ctx:
            try:
                positions_for_greeks = [
                    {"code": pos.call_code, "qty": pos.qty},
                    {"code": pos.put_code,  "qty": pos.qty},
                ]
                for h_code, h_qty in pos.hedge_legs.items():
                    positions_for_greeks.append({"code": h_code, "qty": h_qty})
                greeks = _gm_calc_portfolio_greeks(positions_for_greeks, self.mkt.quote_ctx)
                if greeks and "total_delta" in greeks:
                    return float(greeks["total_delta"])
            except Exception as e:
                log.debug(f"[STRADDLE_BUY] _get_portfolio_delta: {e}")

        if not FUTU_AVAILABLE or not self.mkt.quote_ctx:
            return None
        try:
            ret, snap = self.mkt.quote_ctx.get_market_snapshot(
                [pos.call_code, pos.put_code])
            if ret == 0 and not snap.empty:
                total_delta = 0.0
                for _, row in snap.iterrows():
                    d = float(row.get("option_delta", row.get("delta", 0)) or 0)
                    total_delta += d * pos.qty
                return total_delta
        except Exception as e:
            log.debug(f"[STRADDLE_BUY] _get_portfolio_delta snapshot: {e}")
        return None

    @staticmethod
    def should_trade_today(vix: Optional[float],
                           assessment: Optional[dict] = None,
                           paper: bool = False) -> bool:
        """Low-IVストラドルエントリーが適切な環境かを判定する。

        条件: VIX < 25 (IVが安い帯) + env_score >= MIN
        ペーパーモード(paper=True)の場合はVIX条件をバイパス（全環境検証が目的）。
        """
        if not ENABLE_STRADDLE_BUY:
            return False
        if vix is None:
            return False
        # ペーパーモードはVIX上限条件をバイパス（全環境でデータを収集する）
        if not paper and vix >= 25.0:
            return False
        if assessment:
            if assessment.get("score", 100.0) < STRADDLE_BUY_MIN_ENV_SCORE:
                return False
        return True




# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# calc_iv_crush_params — IV Crush 動的パラメータ算出
# ══════════════════════════════════════════════════════════════════════════════

def calc_iv_crush_params(
    symbol: str,
    vix_current: float,
    cash_usd: float,
    historical_iv_data=None,
    pnl_history=None,
) -> dict:
    """
    IV Crush 戦術の動的パラメータを算出する。

    引数:
        symbol            : 対象銘柄 (例: "TSLA", "AAPL")
        vix_current       : 現在のVIX値
        cash_usd          : 口座現金 (USD)
        historical_iv_data: 過去30日分のIV値リスト (Noneまたは20件未満は履歴不足扱い)
        pnl_history       : 過去トレードのPnLリスト (Kelly補正用)

    戻値:
        dict with keys:
            iv_percentile_min      : エントリー最低IVパーセンタイル閾値 (0-1)
            iv_percentile_min_abs  : 実測IVの80%ile値 (履歴不足時はNone)
            profit_target_pct      : 利確率 (0-1)
            stop_loss_pct          : 損切率 (0-1)
            max_risk_pct           : 口座リスク上限率 (0-1)
            max_qty                : 最大契約数
            _source                : デバッグ用ソース記述
    """
    source_parts = []

    # ── 1. VIX帯判定 ──────────────────────────────────────────────────────────
    # VIX低: <15  中: 15-20  高: >20
    if vix_current < 15.0:
        vix_band = "low"
    elif vix_current < 20.0:
        vix_band = "mid"
    else:
        vix_band = "high"
    source_parts.append(f"vix_band={vix_band}({vix_current:.1f})")

    # ── 2. iv_percentile_min: VIX高時は閾値を下げる ──────────────────────────
    # VIX高い = IV全体が高い → 相対的な高IV判定を緩和してエントリー機会確保
    if vix_band == "high":
        iv_percentile_min = 0.75
    else:
        iv_percentile_min = 0.80
    source_parts.append(f"iv_min_vix={iv_percentile_min:.2f}")

    # IV履歴30件以上あれば実測P80を使う (閾値は固定のまま、absを記録)
    iv_percentile_min_abs = None
    if historical_iv_data and len(historical_iv_data) >= 20:
        sorted_h = sorted(historical_iv_data)
        idx = int(iv_percentile_min * (len(sorted_h) - 1))
        iv_percentile_min_abs = sorted_h[idx]
        iv_percentile_min = 0.80  # 履歴が揃っている場合は標準閾値に戻す
        source_parts.append(f"iv_hist_ok(n={len(historical_iv_data)} abs={iv_percentile_min_abs:.3f})")

    # ── 3. profit_target_pct: VIX帯別TP ─────────────────────────────────────
    # VIX低: 35%  中: 50%  高: 60%
    # 高VIX = クラッシュ後の急反発リスク → 早めに利確
    tp_map = {"low": 0.35, "mid": 0.50, "high": 0.60}
    profit_target_pct = tp_map[vix_band]

    # PnL履歴から勝率補正 (±5%)
    if pnl_history and len(pnl_history) >= 5:
        wins = sum(1 for p in pnl_history if p > 0)
        win_rate = wins / len(pnl_history)
        if win_rate >= 0.65:
            profit_target_pct = min(0.70, profit_target_pct + 0.05)
            source_parts.append(f"tp_win+5%(wr={win_rate:.0%})")
        elif win_rate <= 0.40:
            profit_target_pct = max(0.30, profit_target_pct - 0.05)
            source_parts.append(f"tp_loss-5%(wr={win_rate:.0%})")

    # ── 4. stop_loss_pct: VIX帯別SL ─────────────────────────────────────────
    # VIX低: 12%  中: 10%  高: 8% (高VIX = 予想外動きが大きい → タイトに)
    sl_map = {"low": 0.12, "mid": 0.10, "high": 0.08}
    stop_loss_pct = sl_map[vix_band]

    # ── 5. 資金フェーズ判定 ──────────────────────────────────────────────────
    # Phase1: ~40万円(~2667USD)  Phase2: ~133万円(~8867USD)  Phase3: それ以上
    # (1USD≈150円換算)
    if cash_usd < 8_000:
        phase = 1
        max_risk_pct = 0.015   # 1.5%
        max_qty_base = 1
    elif cash_usd < 50_000:
        phase = 2
        max_risk_pct = 0.020   # 2.0%
        max_qty_base = 2
    else:
        phase = 3
        max_risk_pct = 0.025   # 2.5%
        max_qty_base = 3
    source_parts.append(f"phase={phase}(cash={cash_usd:.0f})")

    # Kelly補正: PnL履歴から勝率でQtyを±1調整
    max_qty = max_qty_base
    if pnl_history and len(pnl_history) >= 5:
        wins = sum(1 for p in pnl_history if p > 0)
        win_rate = wins / len(pnl_history)
        if win_rate < 0.40:
            max_qty = max(1, max_qty - 1)
            source_parts.append(f"kelly_qty-1(wr={win_rate:.0%})")

    return {
        "iv_percentile_min":     iv_percentile_min,
        "iv_percentile_min_abs": iv_percentile_min_abs,
        "profit_target_pct":     profit_target_pct,
        "stop_loss_pct":         stop_loss_pct,
        "max_risk_pct":          max_risk_pct,
        "max_qty":               max_qty,
        "_source":               "|".join(source_parts),
    }


# IVCrushEngine — 決算前IV拡張 → Vol Crush 利確エンジン
# ══════════════════════════════════════════════════════════════════════════════
import dataclasses as _dc


@_dc.dataclass
class IVCrushPosition:
    """IV Crush戦術のポジション情報。"""
    symbol: str
    call_code: str
    put_code: str
    strike: float
    qty: int
    call_entry_price: float
    put_entry_price: float
    entry_premium: float
    entry_iv: float
    entry_time: str
    earnings_date: str
    earnings_hour: str
    expiry: str


class IVCrushEngine:
    """
    決算前 IV 拡張 → Vol Crush 利確エンジン。

    PDT対応: 1DTE（翌営業日満期）でストラドル売り → 翌朝決済。
    フェーズ:
      premarket_check() → 決算カレンダー確認
      check_entry()     → 15:00-15:30 ET にIV条件確認→売り
      check_exit()      → 翌日 9:45-10:15 ET に決済
      reset_daily()     → 日次リセット
    """

    def __init__(self, mkt, eng, paper=False, dry_test=False):
        self.mkt      = mkt
        self.eng      = eng
        self.paper    = paper
        self.dry_test = dry_test
        self.position    = None
        self.trade_done  = False
        self.entry_done  = False
        self._entry_iv_history = []
        self._calendar = None
        self._today_earnings_info = None
        self._dry_test_start = datetime.datetime.now(ET)
        self._dynamic_params = None  # 日次キャッシュ

    def _get_calendar(self):
        if self._calendar is None:
            try:
                import sys as _sys, os as _os
                _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
                from earnings_calendar import EarningsCalendar
                self._calendar = EarningsCalendar()
            except Exception as e:
                log.warning(f"[IVCrush] EarningsCalendar インポート失敗: {e}")
        return self._calendar

    def _calc_dynamic_params(self, symbol: str) -> dict:
        """動的パラメータを算出してキャッシュ。同日中は同一オブジェクトを返す。"""
        if self._dynamic_params is not None:
            return self._dynamic_params
        vix_current = 20.0
        cash_usd = 15_000.0
        try:
            if hasattr(self.mkt, "get_vix"):
                v = self.mkt.get_vix()
                if v is not None:
                    vix_current = float(v)
        except Exception:
            pass
        try:
            if hasattr(self.eng, "get_account_cash"):
                c = self.eng.get_account_cash()
                if c is not None:
                    cash_usd = float(c)
        except Exception:
            pass
        self._dynamic_params = calc_iv_crush_params(
            symbol=symbol,
            vix_current=vix_current,
            cash_usd=cash_usd,
            historical_iv_data=self._entry_iv_history,
            pnl_history=None,
        )
        log.info(
            f"[IVCrush] dynamic_params({symbol}): "
            f"iv_min={self._dynamic_params['iv_percentile_min']:.2f} "
            f"tp={self._dynamic_params['profit_target_pct']:.2f} "
            f"sl={self._dynamic_params['stop_loss_pct']:.2f} "
            f"risk={self._dynamic_params['max_risk_pct']:.3f} "
            f"qty={self._dynamic_params['max_qty']} "
            f"src={self._dynamic_params['_source']}"
        )
        return self._dynamic_params

    def reset_daily(self):
        self.position   = None
        self.trade_done = False
        self.entry_done = False
        self._today_earnings_info = None
        self._dry_test_start = datetime.datetime.now(ET)
        self._dynamic_params = None  # 日次キャッシュリセット

    def premarket_check(self):
        """今日が IV Crush エントリー日かどうかを確認する。"""
        if not ENABLE_IV_CRUSH:
            return False
        if self.dry_test:
            self._today_earnings_info = {
                "ticker": "TSLA", "futu_symbol": "US.TSLA",
                "date": datetime.date.today().isoformat(), "hour": "amc",
                "days_until": 0, "entry_date": datetime.date.today().isoformat(),
            }
            log.info("[IVCrush][DRY-TEST] premarket_check OK: TSLA 決算当日シミュレート")
            return True
        cal = self._get_calendar()
        if cal is None:
            return False
        try:
            cal.refresh()
        except Exception as e:
            log.warning(f"[IVCrush] カレンダー更新失敗: {e}")
        upcoming = cal.get_upcoming_symbols(days=IV_CRUSH_DAYS_BEFORE_MAX)
        if not upcoming:
            log.info("[IVCrush] 本日エントリー対象なし")
            return False
        today_str = datetime.date.today().isoformat()
        for item in upcoming:
            if item.get("entry_date") == today_str:
                self._today_earnings_info = item
                log.info(
                    f"[IVCrush] エントリー日: {item['ticker']} "
                    f"決算={item['date']}({item['hour']}) あと{item['days_until']}日"
                )
                return True
        log.info(f"[IVCrush] 今日({today_str})はエントリー日ではない")
        return False

    def check_entry(self):
        """15:00-15:30 ET の間にIV条件確認→ストラドル売り。"""
        if not ENABLE_IV_CRUSH or self.entry_done or self.trade_done:
            return False
        if self._today_earnings_info is None:
            return False
        now_et = datetime.datetime.now(ET)
        h, m   = now_et.hour, now_et.minute
        if self.dry_test:
            elapsed = (now_et - self._dry_test_start).total_seconds() / 60.0
            if elapsed < 5.0:
                return False
            return self._execute_entry()
        entry_start = IV_CRUSH_ENTRY_START_H * 60 + IV_CRUSH_ENTRY_START_M
        entry_end   = IV_CRUSH_ENTRY_END_H   * 60 + IV_CRUSH_ENTRY_END_M
        now_min     = h * 60 + m
        if not (entry_start <= now_min < entry_end):
            return False
        if not self._check_iv_condition():
            log.info("[IVCrush] IV条件未達 → スキップ")
            return False
        return self._execute_entry()

    def _check_iv_condition(self):
        """現在のIVが過去30日の動的パーセンタイル以上かを確認する。"""
        ticker   = (self._today_earnings_info or {}).get("ticker", "TSLA")
        futu_sym = f"US.{ticker}"
        dp = self._calc_dynamic_params(ticker)
        iv_pct_min = dp["iv_percentile_min"]
        try:
            atm_strike = self.mkt.get_atm_strike(futu_sym)
            if atm_strike is None:
                return True
            expiry    = self._get_entry_expiry()
            call_code = self.mkt.get_option_code(futu_sym, expiry, atm_strike, "CALL")
            if call_code is None:
                return True
            greeks     = self.mkt.get_option_greeks(call_code)
            current_iv = greeks.get("iv")
            if current_iv is None:
                return True
            if len(self._entry_iv_history) >= 20:
                sorted_h = sorted(self._entry_iv_history)
                idx      = int(iv_pct_min * (len(sorted_h) - 1))
                p_thresh = sorted_h[idx]
                ok       = current_iv >= p_thresh
                log.info(
                    f"[IVCrush] IV={current_iv:.3f} P{iv_pct_min:.0%}={p_thresh:.3f} "
                    f"→ {'OK' if ok else 'NG'}"
                )
                return ok
            log.info(f"[IVCrush] IV={current_iv:.3f} 履歴不足({len(self._entry_iv_history)}件) → 許可")
            return True
        except Exception as e:
            log.warning(f"[IVCrush] IV条件チェックエラー: {e} → 許可")
            return True

    def _get_entry_expiry(self):
        """1DTE満期日（翌営業日）を返す。PDT回避のため常に1DTE。"""
        d = datetime.date.today() + datetime.timedelta(days=1)
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        return d.isoformat()

    def _execute_entry(self):
        """ATMストラドル売りを執行する。"""
        if self._today_earnings_info is None:
            return False
        ticker   = self._today_earnings_info.get("ticker", "TSLA")
        futu_sym = f"US.{ticker}"
        try:
            if self.dry_test:
                expiry = self._get_entry_expiry()
                self.position = IVCrushPosition(
                    symbol=futu_sym, call_code=f"{futu_sym}_CALL_dummy",
                    put_code=f"{futu_sym}_PUT_dummy", strike=500.0, qty=1,
                    call_entry_price=5.0, put_entry_price=5.0,
                    entry_premium=10.0, entry_iv=0.85,
                    entry_time=datetime.datetime.now(ET).isoformat(),
                    earnings_date=self._today_earnings_info.get("date", ""),
                    earnings_hour=self._today_earnings_info.get("hour", "amc"),
                    expiry=expiry,
                )
                self.entry_done = True
                log.info(f"[IVCrush][DRY-TEST] ENTRY: {ticker} straddle sell premium=$10 expiry={expiry}")
                self._record_pnl("entry", 0.0, ticker, 500.0, 1, "dry_test")
                return True
            atm_strike = self.mkt.get_atm_strike(futu_sym)
            if atm_strike is None:
                return False
            expiry    = self._get_entry_expiry()
            call_code = self.mkt.get_option_code(futu_sym, expiry, atm_strike, "CALL")
            put_code  = self.mkt.get_option_code(futu_sym, expiry, atm_strike, "PUT")
            if not call_code or not put_code:
                return False
            call_greeks   = self.mkt.get_option_greeks(call_code)
            put_greeks    = self.mkt.get_option_greeks(put_code)
            call_mid      = call_greeks.get("ask", call_greeks.get("last", 0.0))
            put_mid       = put_greeks.get("ask",  put_greeks.get("last",  0.0))
            entry_iv      = call_greeks.get("iv", 0.5)
            if call_mid <= 0 or put_mid <= 0:
                return False
            cash          = self.eng.get_account_cash() or 15000
            premium_total = (call_mid + put_mid) * 100
            dp            = self._calc_dynamic_params(ticker)
            qty           = max(1, min(dp["max_qty"], int(cash * dp["max_risk_pct"] / premium_total)))
            import futu as ft
            call_oid, _call_fm = self.eng._place_single_leg(call_code, ft.TrdSide.SELL, qty, "iv_crush_call_sell")
            put_oid,  _put_fm  = self.eng._place_single_leg(put_code,  ft.TrdSide.SELL, qty, "iv_crush_put_sell")
            if not call_oid or not put_oid:
                log.warning(f"[IVCrush] 発注失敗 call_oid={call_oid}({_call_fm}) put_oid={put_oid}({_put_fm})")
                # 片方だけ成功した場合はロールバック
                if call_oid:
                    self.eng._reverse_leg(call_code, ft.TrdSide.SELL, qty, "iv_crush_rollback_call")
                if put_oid:
                    self.eng._reverse_leg(put_code, ft.TrdSide.SELL, qty, "iv_crush_rollback_put")
                return False
            entry_premium = call_mid + put_mid
            self.position = IVCrushPosition(
                symbol=futu_sym, call_code=call_code, put_code=put_code,
                strike=atm_strike, qty=qty,
                call_entry_price=call_mid, put_entry_price=put_mid,
                entry_premium=entry_premium, entry_iv=entry_iv,
                entry_time=datetime.datetime.now(ET).isoformat(),
                earnings_date=self._today_earnings_info.get("date", ""),
                earnings_hour=self._today_earnings_info.get("hour", "amc"),
                expiry=expiry,
            )
            self.entry_done = True
            log.info(
                f"[IVCrush] ENTRY: {ticker} straddle sell strike={atm_strike} "
                f"premium=${entry_premium:.2f} IV={entry_iv:.3f} qty={qty} expiry={expiry}"
            )
            self._record_pnl("entry", 0.0, ticker, atm_strike, qty, "entry")
            return True
        except Exception as e:
            log.warning(f"[IVCrush] execute_entry エラー: {e}")
            return False

    def check_exit(self):
        """翌日 9:45-10:15 ET に50%利確・10%損切・タイムストップを監視する。"""
        if self.position is None or self.trade_done:
            return None
        now_et = datetime.datetime.now(ET)
        h, m   = now_et.hour, now_et.minute
        if self.dry_test:
            elapsed = (now_et - self._dry_test_start).total_seconds() / 60.0
            if elapsed >= 10.0:
                return self._close_position("vol_crush_drytest")
            return None
        exit_start    = IV_CRUSH_EXIT_H        * 60 + IV_CRUSH_EXIT_M
        exit_deadline = IV_CRUSH_EXIT_DEADLINE_H * 60 + IV_CRUSH_EXIT_DEADLINE_M
        now_min = h * 60 + m
        if now_min < exit_start:
            return None
        pos = self.position
        if now_min >= exit_deadline:
            log.info(f"[IVCrush] タイムストップ({h}:{m:02d} ET)")
            return self._close_position("time_stop")
        try:
            call_snap = self.mkt.get_option_greeks(pos.call_code)
            put_snap  = self.mkt.get_option_greeks(pos.put_code)
            call_now  = call_snap.get("last", pos.call_entry_price)
            put_now   = put_snap.get("last",  pos.put_entry_price)
            current_premium = call_now + put_now
            if pos.entry_premium > 0:
                chg = (current_premium - pos.entry_premium) / pos.entry_premium
                ticker_for_dp = str(pos.symbol).replace("US.", "")
                dp = self._calc_dynamic_params(ticker_for_dp)
                if chg <= -dp["profit_target_pct"]:
                    log.info(f"[IVCrush] 利確: premium={current_premium:.2f} chg={chg:.1%} tp={dp['profit_target_pct']:.2f}")
                    return self._close_position("vol_crush_profit")
                if chg >= dp["stop_loss_pct"]:
                    log.info(f"[IVCrush] 損切: premium={current_premium:.2f} chg={chg:.1%} sl={dp['stop_loss_pct']:.2f}")
                    return self._close_position("stop_loss")
        except Exception as e:
            log.debug(f"[IVCrush] check_exit エラー: {e}")
        return None

    def _close_position(self, reason):
        """ストラドルを買い戻して決済する。"""
        pos = self.position
        pnl_usd = 0.0
        if self.dry_test:
            pnl_usd = pos.entry_premium * pos.qty * 100 * 0.5
            log.info(f"[IVCrush][DRY-TEST] CLOSE: {reason} pnl=${pnl_usd:.2f}")
            self._record_pnl("exit", pnl_usd, pos.symbol, pos.strike, pos.qty, reason)
            self.position = None
            self.trade_done = True
            return {"reason": reason, "pnl_usd": pnl_usd}
        exit_premium = None
        try:
            import futu as ft
            # H4: 実価格取得してP&L計算
            try:
                _call_snap = self.mkt.get_option_greeks(pos.call_code)
                _put_snap  = self.mkt.get_option_greeks(pos.put_code)
                _call_exit = _call_snap.get("last", pos.call_entry_price)
                _put_exit  = _put_snap.get("last", pos.put_entry_price)
                if _call_exit > 0 and _put_exit > 0:
                    exit_premium = _call_exit + _put_exit
            except Exception as _pe:
                log.warning(f"[IVCrush] exit価格取得失敗 → 固定率で概算: {_pe}")
            # C1: tupleを正しく展開して発注失敗を検出
            _call_close_oid, _call_close_fm = self.eng._place_single_leg(pos.call_code, ft.TrdSide.BUY, pos.qty, "iv_crush_call_buy")
            _put_close_oid,  _put_close_fm  = self.eng._place_single_leg(pos.put_code,  ft.TrdSide.BUY, pos.qty, "iv_crush_put_buy")
            if not _call_close_oid or not _put_close_oid:
                log.warning(f"[IVCrush] 決済発注一部失敗: call={_call_close_fm} put={_put_close_fm}")
        except Exception as e:
            log.warning(f"[IVCrush] _close_position エラー: {e}")
        # H4: 実価格ベースでP&L計算（取得できた場合優先、できなければ固定率概算）
        if exit_premium is not None:
            # ストラドル売り: entry > exit → 利益
            pnl_usd = (pos.entry_premium - exit_premium) * pos.qty * 100
        else:
            _ticker_dp = str(pos.symbol).replace("US.", "")
            _dp = self._calc_dynamic_params(_ticker_dp)
            if reason in ("vol_crush_profit", "vol_crush_drytest"):
                pnl_usd = pos.entry_premium * pos.qty * 100 * _dp["profit_target_pct"]
            elif reason == "stop_loss":
                pnl_usd = -pos.entry_premium * pos.qty * 100 * _dp["stop_loss_pct"]
        log.info(f"[IVCrush] CLOSE: {reason} pnl_usd=${pnl_usd:.2f} exit_premium={exit_premium}")
        self._record_pnl("exit", pnl_usd, pos.symbol, pos.strike, pos.qty, reason)
        self.position = None
        self.trade_done = True
        return {"reason": reason, "pnl_usd": pnl_usd}

    def _record_pnl(self, event, pnl, symbol, strike, qty, reason):
        try:
            record = {
                "event": event, "symbol": str(symbol).replace("US.", ""),
                "strike": strike, "qty": qty, "pnl_usd": round(pnl, 2),
                "reason": reason, "timestamp": datetime.datetime.now(ET).isoformat(),
            }
            existing = []
            if IV_CRUSH_PNL_FILE.exists():
                try:
                    existing = json.loads(IV_CRUSH_PNL_FILE.read_text())
                except Exception:
                    existing = []
            existing.append(record)
            IV_CRUSH_PNL_FILE.write_text(json.dumps(existing, indent=2))
        except Exception as e:
            log.debug(f"[IVCrush] _record_pnl エラー: {e}")

    def is_active(self):
        """ポジション保有中かどうか。"""
        return self.position is not None

# ══════════════════════════════════════════════════════════════════════════════
# SPYCreditSpreadBot — main orchestrator
# ══════════════════════════════════════════════════════════════════════════════
class SPYCreditSpreadBot:
    def __init__(self, paper: bool = False, test_connect: bool = False,
                 demo_compare: bool = False, dry_test: bool = False,
                 no_multi: bool = False):
        self.paper        = paper
        self.test_connect = test_connect
        self.demo_compare = demo_compare
        self.dry_test     = dry_test
        self.underlying_code: str = UNDERLYING_CODE  # 動的銘柄選択: premarketフェーズでセット

        # ── マルチ銘柄モード ─────────────────────────────────────────────────
        # --no-multi で無効化可能。有効時はプレマーケットで上位N銘柄を選択する
        self._multi_enabled: bool = ENABLE_MULTI_SYMBOL and not no_multi
        # 本日の運用銘柄リスト (プレマーケットでセット)
        # 例: [{"symbol": "US.SPY", "tactic": "cs_sell"}, {"symbol": "US.TSLA", "tactic": "orb_buy"}]
        self.active_symbols: list = []
        # 銘柄ごとの日次計画 (symbol → {tactic, strategy_selector_result, ...})
        self.daily_plan: dict = {}
        # マルチ銘柄ポジション追跡 (symbol → {tactic, trade_id, qty, open: bool})
        self.multi_positions: dict = {}
        # マルチ銘柄エントリー試行済みフラグ (symbol → bool)
        self._multi_entry_attempted: dict = {}
        # マルチ銘柄エグジット完了フラグ (symbol → bool)
        self._multi_exit_done: dict = {}

        self.mkt          = MarketData(underlying_code=self.underlying_code)
        self.eng          = TradeEngine(paper=paper)
        self.builder      = None  # initialized after connect
        self.intraday_monitor: Optional[IntradayMonitor] = None  # initialized after connect
        # dry-testモード: 起動時刻を記録（5分後にエントリーウィンドウを開く）
        self._dry_test_start: datetime.datetime = datetime.datetime.now(ET)

        # Daily state — reset at EOD
        self.traded_today      = False
        self.orf_checked       = False  # 10:00 ET check done
        self.orf_triggered     = False  # ORF conditions met
        self.orf_direction: Optional[str] = None
        self._standard_entry_done = False  # 10:30 ET entry attempted flag
        self._orf_entry_done   = False  # 13:00 ET entry attempted flag
        # Dynamic entry window state
        self._standard_window_open: bool = False   # 10:30でウィンドウが開いている
        self._orf_window_open: bool = False         # 13:00でウィンドウが開いている
        self._window_assessment: Optional[dict] = None  # ウィンドウ開始時のassessment結果（30分ごとに再取得）
        self._window_assessment_time: Optional[datetime.datetime] = None  # 最後にassessmentを取得した時刻
        self._pending_size_factor: float = 1.0  # _check_entry_conditions → run_standard_entry への時間帯係数受け渡し
        self._nightly_checked  = False
        self._monthly_export   = False
        self._intraday_tick_count = 0  # 60秒ごとのtick用カウンタ
        # Premarket bias × OR confluence (G3: 仮説→OR検証フィードバックループ)
        self._premarket_bias: str    = "neutral"   # bull/bear/neutral (next_day_bias.jsonから)
        self._or_actual_direction: str = "neutral" # bull/bear (check_opening_rangeで設定)
        self._bias_confluence: str   = "neutral"   # "confluence"/"conflict"/"neutral"
        self._force_close_done = False  # 15:50 force close 完了フラグ
        self._force_close_retry_count = 0  # 15:50 force close リトライ回数
        self._expiry_sweep_done = False  # 16:05 満期掃引完了フラグ（1日1回）
        self._warned_na_positions: set = set()  # N/A警告済みpositionコード（重複抑制）
        self._assessment_refreshed: bool = False  # 改善2: 10:00 ET以降のORB確定後assessment再評価フラグ
        self._current_trade_id: Optional[str] = None  # P1-4: entry-exit紐付け用trade_id
        self._current_signal_id: Optional[str] = None  # 本番/ペーパー横断照合用signal_id
        self._last_entry_ts: Optional[datetime.datetime] = None  # バグ1: GammaEarlyExit用エントリー時刻
        self._gamma_exit_pending: set = set()  # バグ3修正: GammaEarlyExit二重発射防止。exit指示済みspread_keyを保持
        self._daily_loss_halted: bool = False  # P1-2: 日次最大損失で停止済みフラグ
        self._trade_ctx_dead_count: int = 0   # Trade context連続死検知カウンタ
        self._trade_ctx_check_count: int = 0  # 30s×30 = 15分ごとチェック用カウンタ
        self._quote_ctx_dead_count: int = 0   # Quote context連続死検知カウンタ
        self._quote_ctx_check_count: int = 0  # 15分ごとチェック用カウンタ
        # [VIXBand] エントリー時にセット → ExitMonitorが参照する take_profit オーバーライド
        self._vix_band_take_profit_override: Optional[float] = None

        # ── ペーパーモード: CS/IC 複数回エントリー管理 ──────────────────────────
        # ペーパーモードでは検証目的で90分ごとにCS/ICエントリーフラグをリセットする
        # 本番モードでは使用しない（traded_today フラグによる1日1件制限を維持）
        self._paper_last_standard_entry_et: Optional[datetime.datetime] = None
        PAPER_MULTI_ENTRY_INTERVAL_MIN = 90  # noqa: F841 (定数はここで定義し参照はメインループ)

        # ── ペーパー大量検証モード: 銘柄×戦術の再エントリー追跡 ────────────────────
        # キー: "{symbol}_{tactic}" 例: "US.SPY_cs_sell"
        # 値: 最後にエントリーした datetime (ET) または None (未エントリー)
        # PAPER_MASS_VERIFY_ENTRY_INTERVAL_MIN 経過後に再エントリーを許可する
        self._mass_verify_last_entry: dict = {}   # symbol_tactic → datetime | None
        self._mass_verify_positions: dict = {}    # symbol_tactic → ポジション情報

        # ── PDT動作モード ────────────────────────────────────────────────────
        # connect後に get_trading_mode(cash_usd, paper) でセットされる。
        # 'pdt_constrained': cash < $25,000（本番のみ）→ CS/IC 1DTE + OTM 限定
        # 'full': cash >= $25,000 またはペーパーモード → 全戦術解禁
        # 起動時は "unknown" で初期化し、connect完了後に確定する。
        self.trading_mode: str = "full" if paper else "unknown"
        # PDT制約下で使用する満期オフセット日数（0DTE→1DTE化）
        self.pdt_expiry_offset_days: int = 1  # pdt_constrained時に翌営業日満期を使用
        # PDT該当トレード累計カウンタ（週次レポート用）
        self._pdt_trade_count: int = 0  # 0DTE実施→PDT消費として計上
        # 最新の口座残高キャッシュ（USD）: IntradayMonitor._try_delta_hedge()から参照
        # connect後の資金チェック・定期更新時に更新される
        self._last_cash_usd: float = 0.0

        # ── ORB (Atlas統合) ──────────────────────────────────────────────────
        # ORBEngine はconnect後に初期化（MarketData/TradeEngineを渡すため）
        self.orb_engine: Optional[ORBEngine] = None
        self._orb_premarket_ok: bool = False   # premarket_check結果キャッシュ
        self._orb_entry_attempted: bool = False  # ORBエントリー試行済みフラグ

        # ── Calendar Spread (Atlas統合) ──────────────────────────────────────
        # CalendarEngine はconnect後に初期化（MarketData/TradeEngineを渡すため）
        self.calendar_engine: Optional[CalendarEngine] = None
        self._calendar_entry_attempted: bool = False  # カレンダーエントリー試行済み

        # ── Gamma Scalp (Atlas統合) ──────────────────────────────────────────
        # StraddleEngine + GammaScalpEngine はconnect後に初期化
        self.straddle_engine:   Optional[StraddleEngine]   = None
        self.gamma_scalp_engine: Optional[GammaScalpEngine] = None
        self._gamma_scalp_entry_attempted: bool = False    # ストラドルエントリー試行済み

        # ── StraddleBuy (Atlas統合) ─────────────────────────────────────────
        # StraddleBuyEngine はconnect後に初期化
        self.straddle_buy_engine: Optional[StraddleBuyEngine] = None
        self._straddle_buy_premarket_ok:    bool = False
        self._straddle_buy_entry_attempted: bool = False

        # ── IVCrush (Atlas統合) ──────────────────────────────────────────────
        # IVCrushEngine はconnect後に初期化（MarketData/TradeEngineを渡すため）
        self.iv_crush_engine: Optional[IVCrushEngine] = None
        self._iv_crush_premarket_ok:    bool = False
        self._iv_crush_entry_attempted: bool = False

        self.consecutive_start_failures = load_failures()

    def _reset_daily_state(self):
        self.traded_today  = False
        self.orf_checked   = False
        self.orf_triggered = False
        self.orf_direction = None
        self._standard_entry_done = False
        self._orf_entry_done = False
        self._standard_window_open = False
        self._orf_window_open = False
        self._window_assessment = None
        self._window_assessment_time = None
        self._pending_size_factor = 1.0
        self._premarket_bias = "neutral"
        self._or_actual_direction = "neutral"
        self._bias_confluence = "neutral"
        self._force_close_done = False
        self._force_close_retry_count = 0
        self._expiry_sweep_done = False  # 16:05 満期掃引フラグ日次リセット
        self._daily_loss_halted = False
        self._warned_na_positions = set()
        self._assessment_refreshed = False  # 改善2: リセット
        # ORB日次リセット
        if self.orb_engine is not None:
            self.orb_engine.reset_daily()
        self._orb_premarket_ok    = False
        self._orb_entry_attempted = False
        # Calendar日次リセット
        if self.calendar_engine is not None:
            self.calendar_engine.reset_daily()
        self._calendar_entry_attempted = False
        # Gamma Scalp日次リセット
        if self.straddle_engine is not None:
            self.straddle_engine.reset_daily()
        if self.gamma_scalp_engine is not None:
            self.gamma_scalp_engine.reset_daily()
        self._gamma_scalp_entry_attempted = False
        # StraddleBuy日次リセット
        if self.straddle_buy_engine is not None:
            self.straddle_buy_engine.reset_daily()
        self._straddle_buy_premarket_ok    = False
        self._straddle_buy_entry_attempted = False
        # IVCrush日次リセット
        if self.iv_crush_engine is not None:
            self.iv_crush_engine.reset_daily()
        self._iv_crush_premarket_ok    = False
        self._iv_crush_entry_attempted = False
        # ペーパー複数回エントリー: 日次リセット
        self._paper_last_standard_entry_et = None
        # マルチ銘柄日次リセット
        self.active_symbols.clear()
        self.daily_plan.clear()
        self.multi_positions.clear()
        self._multi_entry_attempted.clear()
        self._multi_exit_done.clear()
        # ペーパー大量検証モード日次リセット
        self._mass_verify_last_entry.clear()
        self._mass_verify_positions.clear()
        # バグ3修正: GammaEarlyExit二重発射防止フラグ日次リセット
        self._gamma_exit_pending.clear()

    # ── 銘柄選択 (premarketフェーズ) ────────────────────────────────────────────
    def _select_symbol_premarket(self):
        """symbol_selectorで今日の最適銘柄を選択してself.underlying_codeにセットする。

        戦術タイプは strategy_selector の推奨を参照する。
        strategy_selectorが使えない場合はVIXで簡易判定（VIX<20→buy/>=20→sell）。

        失敗時: SPYにフォールバックして継続（Bot動作に影響なし）。
        """
        if not _SYMBOL_SELECTOR_AVAILABLE or _SymbolSelector is None:
            log.info("[SymbolSelector] モジュール未ロード → SPYで継続")
            return

        try:
            # 戦術タイプの簡易判定（sell/buy）
            tactic = "sell"
            try:
                _vix_for_sel = self.mkt.get_vix()
                if _vix_for_sel is not None and _vix_for_sel < 20.0:
                    tactic = "buy"
            except Exception:
                pass

            # SymbolSelectorを初期化して選択実行
            _sym_sel = _SymbolSelector()
            _sym_sel.connect()
            try:
                _sym_result = _sym_sel.select(tactic=tactic, paper=self.paper)
            finally:
                _sym_sel.close()

            selected = _sym_result.get("symbol", UNDERLYING_CODE)
            score    = _sym_result.get("score", 0)
            reason   = _sym_result.get("reason", "")

            # 全銘柄スコアをログに出力
            all_scores = _sym_result.get("scores", {})
            scores_str = " | ".join(
                f"{s.replace('US.', '')} score={all_scores[s].total:.0f}"
                for s in sorted(all_scores, key=lambda k: all_scores[k].total, reverse=True)
            ) if all_scores else "N/A"
            log.info(
                f"[SymbolSelector] 今日の銘柄: {selected.replace('US.', '')} "
                f"(score={score}, reason={reason}, tactic={tactic}) | {scores_str}"
            )

            # フォールバック: オプションチェーン確認（選択銘柄でチェーン取れるか）
            if selected != UNDERLYING_CODE:
                # SPY以外の銘柄が選ばれた場合はオプションチェーン疎通確認
                _chain_ok = self._verify_option_chain(selected)
                if not _chain_ok:
                    log.warning(
                        f"[SymbolSelector] {selected.replace('US.','')} オプションチェーン取得失敗 "
                        f"→ SPYにフォールバック"
                    )
                    selected = UNDERLYING_CODE

            # self.underlying_code と self.mkt.underlying_code を同期
            self.underlying_code = selected
            self.mkt.underlying_code = selected
            log.info(f"[SymbolSelector] 適用銘柄: {self.underlying_code.replace('US.','')}")

        except Exception as _sel_e:
            log.warning(f"[SymbolSelector] 銘柄選択失敗 → SPYにフォールバック: {_sel_e}")
            self.underlying_code = UNDERLYING_CODE
            self.mkt.underlying_code = UNDERLYING_CODE

    def _select_multi_symbols_premarket(self):
        """マルチ銘柄モード: プレマーケットで上位N銘柄を選択してself.active_symbolsをセットする。

        各銘柄に最適な戦術タイプを割り当てる。
        ペーパー大量検証モード (PAPER_MASS_VERIFY_MODE=True) では全10銘柄×全戦術を並列実行する。
        失敗時は[self.underlying_code]（単一銘柄）にフォールバック。
        """
        if not self._multi_enabled:
            # マルチ無効: active_symbolsに現在の1銘柄のみをセット
            self.active_symbols = [{"symbol": self.underlying_code, "tactic": "cs_sell"}]
            log.info(f"[Multi] マルチ銘柄無効 → {self.underlying_code.replace('US.', '')}のみ")
            return

        # ── ペーパー大量検証モード ───────────────────────────────────────────────
        # ペーパー時かつPAPER_MASS_VERIFY_MODE=Trueの場合、全銘柄×全戦術をactive_symbolsに登録。
        # スコアフィルタなし・1銘柄に複数戦術割当可能・90分ごとに再エントリーを許可。
        if self.paper and PAPER_MASS_VERIFY_MODE:
            candidates = []
            for sym in PAPER_MASS_VERIFY_SYMBOLS:
                for tac in PAPER_MASS_VERIFY_TACTICS:
                    # P0-1修正: ペーパー大量検証モードではオプションチェーン事前確認をスキップ。
                    # futuペーパー口座はQQQ/IWM等の非SPYオプションチェーンが取得できないが、
                    # 実際の発注は試みる（失敗すればplace()がFalseを返す）。
                    # dry-test / futu未接続の場合も同様にスキップ。
                    candidates.append({
                        "symbol": sym,
                        "tactic": tac,
                        "score": 100,   # スコアフィルタなし
                        "reason": "mass_verify",
                    })
                    log.debug(
                        f"[MassVerify] {sym.replace('US.','')}×{tac} "
                        f"チェーン確認スキップ（ペーパー検証モード）→ 発注試行"
                    )

            if not candidates:
                candidates = [{"symbol": UNDERLYING_CODE, "tactic": "cs_sell",
                               "score": 0, "reason": "フォールバック"}]

            self.active_symbols = candidates

            # daily_plan: symbol_tactic をキーとして全組み合わせを登録
            for item in self.active_symbols:
                _key = f"{item['symbol']}_{item['tactic']}"
                self.daily_plan[_key] = {
                    "tactic": item["tactic"],
                    "symbol": item["symbol"],
                    "score": item.get("score", 0),
                    "reason": item.get("reason", ""),
                }

            _n_sym = len({i["symbol"] for i in self.active_symbols})
            _n_tac = len({i["tactic"] for i in self.active_symbols})
            log.info(
                f"[MassVerify] 大量検証モード: {len(self.active_symbols)}エントリー "
                f"({_n_sym}銘柄 × {_n_tac}戦術) "
                + ", ".join(
                    f"{i['symbol'].replace('US.','')}×{i['tactic']}"
                    for i in self.active_symbols[:6]
                )
                + (f" ... 他{len(self.active_symbols)-6}件" if len(self.active_symbols) > 6 else "")
            )
            return

        # ── 通常マルチ銘柄モード（本番 or ペーパー大量検証無効時）──────────────────
        if not _SYMBOL_SELECTOR_AVAILABLE or _SymbolSelector is None:
            log.info("[Multi] SymbolSelector未ロード → 1銘柄モードで継続")
            self.active_symbols = [{"symbol": self.underlying_code, "tactic": "cs_sell"}]
            return

        try:
            # 戦術タイプの簡易判定
            tactic = "sell"
            try:
                _vix = self.mkt.get_vix()
                if _vix is not None and _vix < 20.0:
                    tactic = "buy"
            except Exception:
                pass

            max_n = MULTI_SYMBOL_MAX_N_PAPER if self.paper else MULTI_SYMBOL_MAX_N
            _sym_sel = _SymbolSelector()
            _sym_sel.connect()
            try:
                selected_list = _sym_sel.select_top_n(
                    n=max_n,
                    tactic=tactic,
                    paper=self.paper,
                    min_score=MULTI_SYMBOL_MIN_SCORE,
                )
            finally:
                _sym_sel.close()

            # オプションチェーン確認 (SPY以外)
            verified = []
            for item in selected_list:
                sym = item["symbol"]
                if sym == UNDERLYING_CODE or self._verify_option_chain(sym):
                    verified.append(item)
                else:
                    log.warning(f"[Multi] {sym.replace('US.','')} オプションチェーン取得失敗 → スキップ")

            if not verified:
                verified = [{"symbol": UNDERLYING_CODE, "tactic": tactic, "score": 0, "reason": "フォールバック"}]

            self.active_symbols = verified

            # daily_planにも反映
            for item in self.active_symbols:
                self.daily_plan[item["symbol"]] = {
                    "tactic": item["tactic"],
                    "score":  item.get("score", 0),
                    "reason": item.get("reason", ""),
                }

            log.info(
                f"[Multi] 本日の運用銘柄: "
                + ", ".join(f"{i['symbol'].replace('US.','')}({i['tactic']}/score={i.get('score',0)})"
                            for i in self.active_symbols)
            )

        except Exception as _e:
            log.warning(f"[Multi] 銘柄選択失敗 → {UNDERLYING_CODE.replace('US.','')}のみ: {_e}")
            self.active_symbols = [{"symbol": self.underlying_code, "tactic": "cs_sell"}]

    def _verify_option_chain(self, symbol: str) -> bool:
        """指定銘柄の当日0DTEオプションチェーンが取得できるかを確認する。

        dry-testモードまたはfutu未接続の場合は常にTrueを返す（テスト継続のため）。

        Note: SymbolSelectorがselect()内でget_option_chainを多数呼ぶため、その直後に
        再度呼ぶとfutuのレート制限(30秒10回)に引っかかりret=-1になる場合がある。
        この場合はchain_dfがエラー文字列になる。レート制限エラーは「取得失敗」ではなく
        「直前のSelector処理でチェーン取得済み」を意味するのでTrueを返す。
        """
        if DRY_TEST or not FUTU_AVAILABLE or not self.mkt.quote_ctx:
            return True
        try:
            today = datetime.datetime.now(ET).strftime("%Y-%m-%d")
            ret, chain_df = self.mkt.quote_ctx.get_option_chain(
                symbol, start=today, end=today
            )
            if ret == RET_OK and chain_df is not None:
                try:
                    is_empty = chain_df.empty
                    sym_short = symbol.replace("US.", "")
                    if not is_empty:
                        log.debug(
                            f"[SymbolSelector] _verify_option_chain({sym_short}): OK "
                            f"({len(chain_df)} rows)"
                        )
                    return not is_empty
                except Exception:
                    return True
            # ret=-1 の場合: エラー文字列を確認してレート制限か権限エラーかを区別する
            sym_short = symbol.replace("US.", "")
            err_msg = chain_df if isinstance(chain_df, str) else str(chain_df)
            if "too frequent" in err_msg or "rate" in err_msg.lower():
                # レート制限: SymbolSelectorが直前に呼び出し済み → チェーン取得可能と判断
                log.info(
                    f"[SymbolSelector] _verify_option_chain({sym_short}): "
                    f"レート制限（Selector直後） → チェーン取得可能と判断してTrue"
                )
                return True
            log.warning(
                f"[SymbolSelector] _verify_option_chain({sym_short}): "
                f"失敗 ret={ret} err='{err_msg[:80]}'"
            )
            return False
        except Exception as _e:
            log.warning(f"[SymbolSelector] _verify_option_chain({symbol}): 例外 {_e}")
            return False

    # ── Premarket bias × OR confluence ────────────────────────────────────────
    def _compute_bias_confluence(self):
        """プレマーケットバイアスとOR実際方向を照合してself._bias_confluenceを更新する。
        一致（コンフルエンス）: サイズ x1.2 / 矛盾: サイズ x0.5 / neutral: 変更なし
        """
        bias = self._premarket_bias
        or_dir = self._or_actual_direction
        if bias == "neutral" or or_dir == "neutral":
            self._bias_confluence = "neutral"
        elif bias == or_dir:
            self._bias_confluence = "confluence"
        else:
            self._bias_confluence = "conflict"
        log.info(
            f"[BiasConfluence] premarket_bias={bias} or_direction={or_dir} "
            f"→ {self._bias_confluence}"
        )

    # ── 10:00 ET: Opening Range Fade check ────────────────────────────────────
    def check_opening_range(self):
        """
        Compare SPY price at 10:00 ET vs open price.
        If VIX >= orf_vix_thr and |move| >= orf_move_thr:
          - orf_triggered = True
          - orf_direction = opposite of the move (fade the open)
          - standard 10:30 entry is skipped; ORF entry at 13:00 is used

        Dynamic thresholds (ORF_VIX_THRESHOLD / ORF_MOVE_THRESHOLD are fallback only):
          orf_vix_thr  = IntradayMonitor._vix_elevated_threshold * 0.9  (P70相当)
          orf_move_thr = 0.008 * (vix / 20.0), cap=0.020
        """
        vix = self.mkt.get_vix()
        if vix is None:
            log.warning("ORF check: VIX unavailable → skipping ORF")
            return

        # ── Dynamic VIX threshold: IntradayMonitor._vix_elevated_threshold * 0.9 ──
        if (self.intraday_monitor is not None
                and self.intraday_monitor._vix_elevated_threshold > 0):
            orf_vix_thr = round(self.intraday_monitor._vix_elevated_threshold * 0.9, 1)
            log.debug(f"ORF VIX threshold (dynamic): {orf_vix_thr:.1f} "
                      f"(elevated={self.intraday_monitor._vix_elevated_threshold:.1f} * 0.9)")
        else:
            orf_vix_thr = ORF_VIX_THRESHOLD
            log.debug(f"ORF VIX threshold (fallback): {orf_vix_thr}")

        # ── Dynamic move threshold: ATRベース（symbol_params対応）──
        # symbol_params.json が存在する場合: ATR_daily_pct * breakout_atr_pct_mult で算出
        # フォールバック: base=0.008 scaled by VIX/20, cap=0.020
        _orb_symbol = self.mkt.underlying_code if self.mkt else UNDERLYING_CODE
        if _SYMBOL_PARAMS:
            _atr_pct = self.mkt.get_symbol_atr_pct(_orb_symbol, period=14) if self.mkt else None
            orf_move_thr_raw = calc_orb_breakout_threshold(_orb_symbol, _atr_pct)
            orf_move_thr = round(min(0.030, max(0.004, orf_move_thr_raw)), 4)
            log.debug(f"ORF move threshold (ATR-based): {orf_move_thr:.4f} "
                      f"(symbol={_orb_symbol} ATR%={_atr_pct})")
        else:
            orf_move_thr = round(min(0.020, ORF_MOVE_THRESHOLD * (vix / 20.0)), 4)
            log.debug(f"ORF move threshold (VIX-scaled): {orf_move_thr:.4f} (VIX={vix:.1f}/20)")

        if vix < orf_vix_thr:
            log.info(f"ORF check: VIX={vix:.1f} < {orf_vix_thr} → ORF inactive, standard entry proceeds")
            return

        snap = self.mkt.get_spy_snapshot()
        if snap is None:
            log.warning("ORF check: SPY snapshot unavailable")
            return

        spy_open    = snap.get("open_price") or snap.get("last_price")
        spy_current = snap.get("last_price") or snap.get("open_price")

        if not spy_open or not spy_current or spy_open == 0:
            log.warning("ORF check: SPY price data invalid")
            return

        move = (spy_current - spy_open) / spy_open
        log.info(f"ORF check: SPY open={spy_open:.2f} now={spy_current:.2f} "
                 f"move={move:+.2%} VIX={vix:.1f} "
                 f"vix_thr={orf_vix_thr:.1f} move_thr={orf_move_thr:.2%}")

        if abs(move) >= orf_move_thr:
            # Fade the opening move:
            #   drop at open → sell Put CS (expect stabilization/recovery)
            #   rally at open → sell Call CS (expect stabilization/pullback)
            self.orf_direction = "PUT" if move < 0 else "CALL"
            self.orf_triggered = True
            log.info(f"ORF TRIGGERED: move={move:+.2%} → {self.orf_direction} CS at 13:00 ET")
            pushover(
                "SPY CS ORF発動",
                f"10:00ET: SPY {move:+.2%} (VIX={vix:.1f})\n"
                f"→ {self.orf_direction} CS を 13:00ET にエントリー予定\n"
                f"標準10:30エントリーはスキップ\n"
                f"[動的閾値] vix_thr={orf_vix_thr:.1f} move_thr={orf_move_thr:.2%}",
            )
        else:
            log.info(f"ORF check: |move|={abs(move):.2%} < {orf_move_thr:.2%} → "
                     f"no ORF, standard 10:30 entry proceeds")

    def get_expiry_0dte(self) -> str:
        """今日のET日付を0DTE expiry文字列 (YYYY-MM-DD) で返す。"""
        return datetime.datetime.now(ET).strftime("%Y-%m-%d")

    def get_expiry_for_mode(self) -> str:
        """trading_mode に応じて expiry 日付文字列を返す。

        pdt_constrained: 翌営業日を返す（1DTE化・PDT消費回避）
          - 土曜なら月曜、金曜なら月曜（週末スキップ）
          - 週末以外は翌日
        full: 今日（0DTE）を返す
        """
        now_et = datetime.datetime.now(ET)
        if self.trading_mode != "pdt_constrained":
            return now_et.strftime("%Y-%m-%d")
        # 翌営業日を計算（土日をスキップ）
        next_day = now_et + datetime.timedelta(days=1)
        while next_day.weekday() >= 5:  # 5=土曜, 6=日曜
            next_day += datetime.timedelta(days=1)
        expiry_str = next_day.strftime("%Y-%m-%d")
        log.info(
            f"[PDT] 1DTE化: expiry={expiry_str} "
            f"(今日{now_et.strftime('%Y-%m-%d')} → 翌営業日)"
        )
        return expiry_str

    # ── Standard entry (SMA direction) ───────────────────────────────────────
    def run_standard_entry(self):
        """SMA-direction based entry. Skipped if ORF is triggered."""
        if self.orf_triggered:
            log.info("Standard entry skipped: ORF triggered, ORF entry will handle this")
            return

        now      = datetime.datetime.now(ET)
        time_key = f"{now.hour}:{now.minute:02d}"

        # ── [G-NEW1] 証拠金使用率チェック ───────────────────────────────────────
        if not self.eng.check_margin_and_alert():
            log.info("[MarginCheck] 証拠金使用率超過 → エントリースキップ")
            self.traded_today = True
            return

        # ── [PortfolioRisk] 週次/月次DDチェック ───────────────────────────────
        _pr_cash_for_dd = self.eng.get_account_cash() if _PORTFOLIO_RISK_AVAILABLE else 0
        if _PORTFOLIO_RISK_AVAILABLE and _pr_cash_for_dd > 0:
            if check_weekly_dd(_pr_cash_for_dd):
                log.info("[PortfolioRisk] 週次DD上限到達 → エントリースキップ")
                self.traded_today = True
                return
            if check_monthly_dd(_pr_cash_for_dd):
                log.info("[PortfolioRisk] 月次DD上限到達 → エントリースキップ")
                self.traded_today = True
                return

        if check_consecutive_losses():
            self.traded_today = True
            return

        # 改善3: セカンダリーエントリー時の既存ポジション含み損チェック
        # 既にポジションを持っている場合、含み損なら追加エントリーを禁止する
        _existing_positions = self.eng.get_open_positions()
        if _existing_positions:
            _total_unrealized = 0.0
            for _ep in _existing_positions:
                _upl = _ep.get("unrealized_pl", 0)
                try:
                    if _upl not in (None, "N/A", ""):
                        _total_unrealized += float(_upl)
                except (ValueError, TypeError):
                    pass
            if _total_unrealized < 0:
                log.info(
                    f"[Entry] 既存ポジション含み損のため追加エントリースキップ "
                    f"(unrealized_pl={_total_unrealized:.2f})"
                )
                return

        vix = self.mkt.get_vix()
        if vix is None:
            pushover("SPY CS", f"エントリースキップ {time_key}ET: VIX取得不可")
            self.traded_today = True
            return

        # Premarket assessment (P1: VRP + 経済カレンダー統合 + P1-3動的VIX閾値)
        assessment = premarket_assessment(self.mkt, vix, self.intraday_monitor)
        if self.intraday_monitor:
            self.intraday_monitor.set_morning_score(assessment["score"])
            # base_stop_multをVIX×残り時間で動的初期化
            _now_et = datetime.datetime.now(ET)
            _fc = _now_et.replace(hour=FORCE_CLOSE_H, minute=FORCE_CLOSE_M, second=0, microsecond=0)
            _hours_rem = max(0.0, (_fc - _now_et).total_seconds() / 3600.0)
            self.intraday_monitor.init_base_stop_mult(
                vix, _hours_rem,
                symbol=self.underlying_code,
                mkt=self.mkt,
            )
        if assessment["recommendation"] == "skip":
            log.info(f"Premarket assessment SKIP: score={assessment['score']}, "
                     f"VRP={assessment['vrp']}")
            pushover("SPY CS", f"エントリースキップ {time_key}ET: "
                     f"環境スコア{assessment['score']}点 (VRP={assessment['vrp']})")
            self.traded_today = True
            return

        params = get_params(vix, STANDARD_PARAMS)
        if params is None:
            log.info(f"VIX={vix:.1f} >= 35 → standard entry halted (ORF may activate)")
            # Don't set traded_today; ORF at 13:00 might still fire if triggered
            return

        # ── [VIXBand] VIX帯別最適パラメータ適用 ──────────────────────────────
        params, _vix_band_size_factor, _vix_band = apply_vix_band_overrides(params, vix)
        # take_profit はExitMonitor側で参照するためBotインスタンスにセット
        _band_cfg = _VIX_BAND_PARAMS.get(_vix_band, {})
        _band_take_profit = _band_cfg.get("take_profit") if _band_cfg else None
        self._vix_band_take_profit_override = _band_take_profit

        # Kelly基準によるcapital_pct上書き（Half Kelly・上限として使用）
        # Kellyが既存capital_pctより小さい場合のみ適用（既存ロジック優先）
        _kelly = calc_kelly_fraction(PNL_FILE)
        if _kelly is not None:
            if _kelly < params["capital_pct"]:
                params = dict(params)
                params["capital_pct"] = _kelly
                log.info(
                    f"[Kelly] capital_pct={_kelly:.4f} (from {20} trades) "
                    f"← VIXティア値より小さいため適用"
                )
            else:
                log.info(
                    f"[Kelly] kelly={_kelly:.4f} >= capital_pct={params['capital_pct']:.4f} "
                    f"→ 既存値を維持"
                )

        # R1: 全係数適用前にoriginal_capital_pctを保存（下限フロア計算用）
        _original_capital_pct = params["capital_pct"]

        # reduce_size: 環境スコアが中程度の場合サイズ縮小
        if assessment["recommendation"] == "reduce_size":
            params = dict(params)
            params["capital_pct"] = params["capital_pct"] * 0.60
            log.info(f"Premarket reduce_size: capital_pct -> {params['capital_pct']:.2f}")

        # post-crisis warmup: crisis解除後1-2営業日はサイズ50%縮小
        if self.intraday_monitor:
            _pc_factor = self.intraday_monitor.post_crisis_size_factor()
            if _pc_factor < 1.0:
                params = dict(params)
                params["capital_pct"] = params["capital_pct"] * _pc_factor
                log.info(
                    f"[post_crisis_warmup] standard: capital_pct *= {_pc_factor} "
                    f"→ {params['capital_pct']:.2f} "
                    f"(resolved={self.intraday_monitor._crisis_resolved_date})"
                )
                pushover(
                    "SPY CS",
                    f"クライシス解除後ウォームアップ中 {time_key}ET: "
                    f"サイズ{_pc_factor*100:.0f}%に縮小 "
                    f"(解除: {self.intraday_monitor._crisis_resolved_date})",
                )

        # VIX9D/VVIX サイズ係数（premarket_assessmentで計算済み）
        _vix9d_factor = assessment.get("vix9d_vvix_size_factor", 1.0)
        if _vix9d_factor < 1.0:
            params = dict(params)
            params["capital_pct"] = round(params["capital_pct"] * _vix9d_factor, 4)
            log.info(
                f"[VIX9D/VVIX] standard: capital_pct *= {_vix9d_factor} "
                f"→ {params['capital_pct']:.4f} "
                f"(VIX9D={assessment.get('vix9d')} VVIX={assessment.get('vvix')})"
            )

        # [VIXBand] high/crisis 時のサイズ50%制限（他の係数適用後に適用）
        if _vix_band_size_factor < 1.0:
            params = dict(params)
            params["capital_pct"] = round(params["capital_pct"] * _vix_band_size_factor, 4)
            log.info(
                f"[VIXBand] {_vix_band}: capital_pct *= {_vix_band_size_factor} "
                f"→ {params['capital_pct']:.4f}"
            )

        # IVR + recovery day adjustments
        ivr             = self.mkt.calc_ivr(vix)
        ivr_thresholds  = self.mkt.get_ivr_percentiles()
        params = apply_ivr_delta(params, ivr, ivr_thresholds)
        params = apply_recovery_delta(params)

        # 時間帯別サイズ係数（_check_entry_conditionsが設定したPending値を消費）
        _time_zone_factor = getattr(self, "_pending_size_factor", 1.0)
        self._pending_size_factor = 1.0  # 消費後リセット
        if _time_zone_factor < 1.0:
            params = dict(params)
            params["capital_pct"] = round(params["capital_pct"] * _time_zone_factor, 4)
            log.info(
                f"[TimeZone] standard: capital_pct *= {_time_zone_factor} "
                f"→ {params['capital_pct']:.4f} (ET {now.hour:02d}:{now.minute:02d})"
            )

        # R1: 複数係数の重複適用による過剰縮小防止（下限フロア = original * 0.25）
        _floor = _original_capital_pct * 0.25
        if params["capital_pct"] < _floor:
            params = dict(params)
            log.info(
                f"[SizeFloor] capital_pct {params['capital_pct']:.4f} < floor {_floor:.4f} "
                f"(original {_original_capital_pct:.4f} * 0.25) → clamped to floor"
            )
            params["capital_pct"] = round(_floor, 4)

        # SMA direction
        spy_open  = self.mkt.get_spy_open()
        direction = SMADirectionDetector(self.mkt.quote_ctx).get_direction(spy_open=spy_open)
        if direction is None:
            pushover("SPY CS", f"エントリースキップ {time_key}ET: SMA方向判定不可")
            self.traded_today = True
            return

        cash   = self.eng.get_account_cash()
        qty    = calc_qty(cash, params, paper=self.paper)
        # [TMR] NASA Self-Checking Pair: arithmetic を2経路で照合
        if _QTY_CALCULATOR_AVAILABLE:
            try:
                _tmr_verify_spread_qty(cash, params.get("width", 10), params.get("capital_pct", 0.55), qty)
            except QtyMismatchError:
                pushover("SPY CS", "[TMR ERROR] qty arithmetic mismatch — order BLOCKED", priority=1)
                self.traded_today = True
                return
        expiry = self.get_expiry_for_mode()  # pdt_constrained: 1DTE / full: 0DTE

        # ── [PortfolioVega] VIX急騰時のVegaベースサイズ縮小 ─────────────────
        _vix_prev = get_yesterday_vix() or 0.0
        _vega_factor = calc_vega_size_factor(vix, _vix_prev)
        if _vega_factor < 1.0:
            qty = max(1, int(qty * _vega_factor))
            log.info(f"[PortfolioVega] VIX急騰サイズ縮小: qty→{qty} (factor={_vega_factor})")

        # ── [PortfolioRisk] 合計リスクチェック ───────────────────────────────
        if _PORTFOLIO_RISK_AVAILABLE and cash > 0:
            _additional_risk = params.get("width", 10) * qty * 100
            if not can_take_risk(_additional_risk, cash):
                log.info("[PortfolioRisk] 合計リスク上限 → エントリースキップ")
                self.traded_today = True
                return

        # ── [StrategySelector] Phase1: 推奨戦術をログ出力（実際の選択は既存ロジック） ──
        if _STRATEGY_SELECTOR_AVAILABLE:
            try:
                _ss_env = {
                    "vix": vix,
                    "vix_rate": 0.0,
                    "vrp": assessment.get("vrp"),
                    "env_score": assessment.get("score", 50),
                    "gap_pct": assessment.get("gap_pct", 0),
                    "bias": assessment.get("bias", "neutral"),
                    "vix_history": [],
                }
                _ss_rec = _ss_select_strategy(_ss_env)
                if _ss_rec and _ss_rec.get("primary"):
                    _ss_primary = _ss_rec["primary"]
                    log.info(
                        f"[StrategySelector] 推奨: {_ss_primary['strategy']} "
                        f"(confidence={_ss_primary['confidence']:.2f}) "
                        f"実際: {'IC' if (vix < 18.0 and (assessment.get('score', 0) or 0) >= 70) else 'CS'}"
                    )
            except Exception as _ss_e:
                log.debug(f"[StrategySelector] スキップ: {_ss_e}")

        # ── [SymbolSelector] 今日の適用銘柄をログ出力（起動時に選択済み） ───────
        log.info(
            f"[SymbolSelector] エントリー銘柄: {self.underlying_code.replace('US.', '')} "
            f"(connect時にselect済み)"
        )

        if self.demo_compare:
            DemoLogger(self.mkt.quote_ctx, self.underlying_code).run("standard", direction, expiry, vix, ivr)

        # ── IC vs CS 自動切替 ────────────────────────────────────────────────
        # VIX < 18 (calm 環境) かつ 環境スコア >= 70 の場合に Iron Condor を選択する。
        # IC ならコール側・プット側の両方からクレジットを取れるため低VIX環境で有効。
        _env_score = assessment.get("score", 0) or 0
        _use_ic = (vix < 18.0 and _env_score >= 70)
        if _use_ic:
            log.info(
                f"[Entry] IC 選択: VIX={vix:.1f} < 18 かつ env_score={_env_score} >= 70 "
                f"→ Iron Condor (PUT CS + CALL CS)"
            )
            signal_id = f"{now.strftime('%Y-%m-%d')}_standard_IC_{now.strftime('%H:%M')}"
            ok = self.builder.place_iron_condor(
                expiry=expiry, qty=qty, params=params, vix=vix,
                tactic="standard", bot=self, signal_id=signal_id,
            )
            entry_direction = "IC"
        else:
            if _use_ic is False and vix < 18.0:
                log.info(
                    f"[Entry] CS 選択: VIX={vix:.1f} < 18 だが env_score={_env_score} < 70 "
                    f"→ 通常 CS ({direction})"
                )
            signal_id = f"{now.strftime('%Y-%m-%d')}_standard_{direction}_{now.strftime('%H:%M')}"
            ok = self.builder.place(expiry, qty, params, vix, direction, "standard", bot=self,
                                    signal_id=signal_id)
            entry_direction = direction

        if ok:
            # PDTトレードカウンタ更新（0DTE=PDT消費・1DTE=PDT非消費）
            if self.trading_mode == "pdt_constrained":
                log.info("[PDT] 1DTE エントリー完了 → PDT消費なし（翌日満期）")
            else:
                self._pdt_trade_count += 1
                log.debug(f"[PDT] 0DTEエントリー → _pdt_trade_count={self._pdt_trade_count}")
            # 動的パラメータを蓄積（バックテスト精度向上用）
            regime = self.intraday_monitor.current_regime if self.intraday_monitor else "N/A"
            sma20 = SMADirectionDetector(self.mkt.quote_ctx)._get_sma20()
            append_pnl_entry({
                "event": "env_snapshot", "time_et": datetime.datetime.now(ET).isoformat(),
                "vix": round(vix, 2), "vrp": assessment.get("vrp"),
                "ivr": round(ivr, 1) if ivr else None,
                "sma20": round(sma20, 2) if sma20 else None,
                "spy_open": round(spy_open, 2) if spy_open else None,
                "env_score": assessment.get("score"),
                "regime": regime, "direction": entry_direction,
                "qty": qty, "params": {k: v for k, v in params.items() if k in ("delta", "width", "capital_pct")},
                "strategy": "IC" if _use_ic else "CS",
            })
            # ── [PortfolioRisk] エントリー後にポジション情報を共有ファイルへ ──
            if _PORTFOLIO_RISK_AVAILABLE:
                try:
                    _pr_update_positions("spy_bot", [{
                        "sell_strike": 0, "buy_strike": 0,
                        "net_credit": 0,
                        "qty": qty,
                        "direction": entry_direction,
                    }])
                except Exception as _pr_e:
                    log.warning(f"[PortfolioRisk] update_positions失敗: {_pr_e}")
            # エントリー後に保有レッグをsubscribeしてリアルタイム価格をキャッシュに流す
            # futuへの反映まで少し待ってからget_open_positionsを呼ぶ
            if not DRY_TEST and FUTU_AVAILABLE:
                time.sleep(2)
                _entry_positions = self.eng.get_open_positions()
                _option_codes = [p.get("code", "") for p in _entry_positions if p.get("code")]
                if _option_codes:
                    self.mkt.subscribe_option_legs(_option_codes)
        self._update_vix_cache(vix)
        self.traded_today = True
        # ペーパーモード: 次回リセット基準時刻を記録（90分後にフラグリセット）
        if self.paper:
            self._paper_last_standard_entry_et = datetime.datetime.now(ET)

    # ── 13:00 ET: ORF entry ────────────────────────────────────────────────────
    def run_orf_entry(self):
        """Opening Range Fade entry. Only runs if orf_triggered = True."""
        if not self.orf_triggered:
            return
        if self.traded_today:
            log.info("ORF entry skipped: already traded today")
            return

        now      = datetime.datetime.now(ET)
        time_key = f"{now.hour}:{now.minute:02d}"

        # ── [G-NEW1] 証拠金使用率チェック ───────────────────────────────────────
        if not self.eng.check_margin_and_alert():
            log.info("[MarginCheck] ORF: 証拠金使用率超過 → エントリースキップ")
            self.traded_today = True
            return

        if check_consecutive_losses():
            self.traded_today = True
            return

        vix = self.mkt.get_vix()
        if vix is None:
            pushover("SPY CS", f"ORFスキップ {time_key}ET: VIX取得不可")
            self.traded_today = True
            return

        params = get_params(vix, ORF_PARAMS)
        if params is None:
            log.info(f"ORF: VIX={vix:.1f} >= 50 → ORF halted")
            pushover("SPY CS", f"ORFノートレード {time_key}ET: VIX={vix:.1f} >= 50")
            self.traded_today = True
            return

        # ── [VIXBand] VIX帯別最適パラメータ適用 (ORF) ───────────────────────
        params, _orf_vix_band_size_factor, _orf_vix_band = apply_vix_band_overrides(params, vix)
        _orf_band_cfg = _VIX_BAND_PARAMS.get(_orf_vix_band, {})
        _orf_band_take_profit = _orf_band_cfg.get("take_profit") if _orf_band_cfg else None
        self._vix_band_take_profit_override = _orf_band_take_profit

        # post-crisis warmup: crisis解除後1-2営業日はサイズ50%縮小
        if self.intraday_monitor:
            _pc_factor = self.intraday_monitor.post_crisis_size_factor()
            if _pc_factor < 1.0:
                params = dict(params)
                params["capital_pct"] = params["capital_pct"] * _pc_factor
                log.info(
                    f"[post_crisis_warmup] orf: capital_pct *= {_pc_factor} "
                    f"→ {params['capital_pct']:.2f} "
                    f"(resolved={self.intraday_monitor._crisis_resolved_date})"
                )
                pushover(
                    "SPY CS",
                    f"クライシス解除後ウォームアップ中(ORF) {time_key}ET: "
                    f"サイズ{_pc_factor*100:.0f}%に縮小 "
                    f"(解除: {self.intraday_monitor._crisis_resolved_date})",
                )

        # VIX9D/VVIX サイズ係数（ORFはpremarket_assessmentを呼ばないため直接取得）
        _orf_vix9d, _orf_vvix = self.mkt.get_vix9d_vvix()
        _vix9d_factor = calc_vix9d_vvix_size_factor(_orf_vix9d, _orf_vvix, vix)
        if _vix9d_factor < 1.0:
            params = dict(params)
            params["capital_pct"] = round(params["capital_pct"] * _vix9d_factor, 4)
            log.info(
                f"[VIX9D/VVIX] orf: capital_pct *= {_vix9d_factor} "
                f"→ {params['capital_pct']:.4f} "
                f"(VIX9D={_orf_vix9d} VVIX={_orf_vvix})"
            )

        ivr             = self.mkt.calc_ivr(vix)
        ivr_thresholds  = self.mkt.get_ivr_percentiles()
        params = apply_ivr_delta(params, ivr, ivr_thresholds)
        params = apply_recovery_delta(params)

        # [VIXBand] high/crisis 時のサイズ50%制限 (ORF)
        if _orf_vix_band_size_factor < 1.0:
            params = dict(params)
            params["capital_pct"] = round(params["capital_pct"] * _orf_vix_band_size_factor, 4)
            log.info(
                f"[VIXBand] orf {_orf_vix_band}: capital_pct *= {_orf_vix_band_size_factor} "
                f"→ {params['capital_pct']:.4f}"
            )

        cash   = self.eng.get_account_cash()
        qty    = calc_qty(cash, params, paper=self.paper)
        # [TMR] NASA Self-Checking Pair: arithmetic を2経路で照合 (ORF)
        if _QTY_CALCULATOR_AVAILABLE:
            try:
                _tmr_verify_spread_qty(cash, params.get("width", 10), params.get("capital_pct", 0.40), qty)
            except QtyMismatchError:
                pushover("SPY ORF", "[TMR ERROR] qty arithmetic mismatch — order BLOCKED", priority=1)
                self.traded_today = True
                return
        expiry = self.get_expiry_for_mode()  # pdt_constrained: 1DTE / full: 0DTE

        # ── [PortfolioVega] VIX急騰時のVegaベースサイズ縮小 (ORF) ────────────
        _orf_vix_prev_spike = getattr(self, "_yesterday_vix", 0.0) or 0.0
        _orf_vega_factor = calc_vega_size_factor(vix, _orf_vix_prev_spike)
        if _orf_vega_factor < 1.0:
            qty = max(1, int(qty * _orf_vega_factor))
            log.info(f"[PortfolioVega] ORF VIX急騰サイズ縮小: qty→{qty} (factor={_orf_vega_factor})")

        # ── [PortfolioRisk] 週次/月次DD + 合計リスクチェック ─────────────────
        if _PORTFOLIO_RISK_AVAILABLE and cash > 0:
            if check_weekly_dd(cash):
                log.info("[PortfolioRisk] ORF: 週次DD上限到達 → エントリースキップ")
                self.traded_today = True
                return
            if check_monthly_dd(cash):
                log.info("[PortfolioRisk] ORF: 月次DD上限到達 → エントリースキップ")
                self.traded_today = True
                return
            _orf_additional_risk = params.get("width", 10) * qty * 100
            if not can_take_risk(_orf_additional_risk, cash):
                log.info("[PortfolioRisk] ORF: 合計リスク上限 → エントリースキップ")
                self.traded_today = True
                return

        log.info(f"ORF entry: {self.orf_direction} VIX={vix:.1f} "
                 f"delta={params['delta']} IVR={ivr if ivr else 'N/A'}")

        if self.demo_compare:
            DemoLogger(self.mkt.quote_ctx, self.underlying_code).run("orf", self.orf_direction, expiry, vix, ivr)

        orf_signal_id = f"{now.strftime('%Y-%m-%d')}_orf_{self.orf_direction}_{now.strftime('%H:%M')}"
        ok = self.builder.place(expiry, qty, params, vix, self.orf_direction, "orf", bot=self,
                                signal_id=orf_signal_id)
        if ok:
            regime = self.intraday_monitor.current_regime if self.intraday_monitor else "N/A"
            append_pnl_entry({
                "event": "env_snapshot", "time_et": datetime.datetime.now(ET).isoformat(),
                "vix": round(vix, 2), "ivr": round(ivr, 1) if ivr else None,
                "env_score": None, "regime": regime,
                "direction": self.orf_direction, "tactic": "orf",
                "qty": qty, "params": {k: v for k, v in params.items() if k in ("delta", "width", "capital_pct")},
            })
            # ── [PortfolioRisk] ORFエントリー後にポジション情報を共有ファイルへ ──
            if _PORTFOLIO_RISK_AVAILABLE:
                try:
                    _pr_update_positions("spy_bot", [{
                        "sell_strike": 0, "buy_strike": 0,
                        "net_credit": 0,
                        "qty": qty,
                        "direction": self.orf_direction,
                    }])
                except Exception as _pr_e:
                    log.warning(f"[PortfolioRisk] ORF update_positions失敗: {_pr_e}")
            # エントリー後に保有レッグをsubscribeしてリアルタイム価格をキャッシュに流す
            if not DRY_TEST and FUTU_AVAILABLE:
                time.sleep(2)
                _orf_positions = self.eng.get_open_positions()
                _orf_option_codes = [p.get("code", "") for p in _orf_positions if p.get("code")]
                if _orf_option_codes:
                    self.mkt.subscribe_option_legs(_orf_option_codes)
        self._update_vix_cache(vix)
        self.traded_today = True

    @staticmethod
    def _get_time_zone_size_factor(now_et: datetime.datetime) -> float:
        """ETの現在時刻から時間帯別サイズ係数を返す。
          ET 9:45-12:59 (Prime Zone)      : 1.0
          ET 13:00-13:59 (セカンダリー帯) : 0.8
          ET 14:00-15:30 (ガンマ加速帯)  : 0.6
        """
        total_min = now_et.hour * 60 + now_et.minute
        if total_min < 13 * 60:
            return 1.0
        elif total_min < 14 * 60:
            return 0.8
        else:
            return 0.6

    def _check_entry_conditions(self, assessment: dict) -> tuple:
        """動的エントリーウィンドウ内の条件チェック。
        Returns (ok: bool, size_factor: float)
          ok = True: エントリー可、False: まだ待つ（またはスキップ）
          size_factor: 時間帯別サイズ係数（_get_time_zone_size_factorで算出）

        必須条件:
          - VIXレジームが calm or normal (elevated/crisis 禁止)
          - 環境スコア >= DYNAMIC_ENTRY_MIN_ENV_SCORE
        推奨条件 (DYNAMIC_ENTRY_VRP_REQUIRED=True の場合のみ必須):
          - VRP > 0
        """
        # 時間帯別サイズ係数
        size_factor = self._get_time_zone_size_factor(datetime.datetime.now(ET))

        # VIXレジームチェック
        if self.intraday_monitor:
            regime = self.intraday_monitor.current_regime
            if regime in ("elevated", "crisis"):
                log.info(f"[DynamicEntry] 条件NG: regime={regime}")
                return False, size_factor

        # 環境スコアチェック
        score = assessment.get("score", 0)
        if score < DYNAMIC_ENTRY_MIN_ENV_SCORE:
            log.info(f"[DynamicEntry] 条件NG: env_score={score} < {DYNAMIC_ENTRY_MIN_ENV_SCORE}")
            return False, size_factor

        # VRPチェック（DYNAMIC_ENTRY_VRP_REQUIRED=True の場合のみ必須）
        if DYNAMIC_ENTRY_VRP_REQUIRED:
            vrp = assessment.get("vrp")
            if vrp is None or vrp <= 0:
                log.info(f"[DynamicEntry] 条件NG: VRP={vrp} <= 0 (required)")
                return False, size_factor

        # N4-TH: Theta最適時間帯チェック（サイズ係数の調整のみ・エントリーは止めない）
        try:
            if self.intraday_monitor is not None:
                _theta_win = IntradayMonitor._theta_optimal_window()
                _is_optimal = _theta_win.get("is_optimal_now", False)
                _optimal_hour = _theta_win.get("optimal_hour")
                if _is_optimal:
                    # 最適時間帯 → サイズ係数を +10% 上乗せ（最大1.0でキャップ）
                    size_factor = min(1.0, size_factor * 1.10)
                    log.info(
                        f"[ThetaOptimal] 最適時間帯 ({_optimal_hour}時ET)"
                        f" → size_factor={size_factor:.2f}"
                    )
                else:
                    log.debug(
                        f"[ThetaOptimal] 非最適時間帯 (optimal={_optimal_hour}h,"
                        f" now={datetime.datetime.now(ET).hour}h)"
                        f" → size_factorそのまま"
                    )
        except Exception as _te:
            log.debug(f"[ThetaOptimal] チェックスキップ: {_te}")

        log.info(
            f"[DynamicEntry] 条件OK: score={score}, "
            f"vrp={assessment.get('vrp')}, "
            f"regime={self.intraday_monitor.current_regime if self.intraday_monitor else 'N/A'}, "
            f"size_factor={size_factor}"
        )
        return True, size_factor

    def _update_vix_cache(self, vix: float):
        """Save today's VIX and spike flag for tomorrow."""
        yesterday_vix = get_yesterday_vix()
        spike = False
        if yesterday_vix:
            dyn_threshold = self.mkt.calc_dynamic_vix_spike_threshold()
            if (vix - yesterday_vix) >= dyn_threshold:
                spike = True
                log.info(f"VIX spike detected: {yesterday_vix:.1f} → {vix:.1f} "
                         f"(+{vix - yesterday_vix:.1f} >= dyn_threshold={dyn_threshold:.2f}) "
                         f"→ tomorrow is recovery day")
        save_vix_spike_data(vix, spike_for_tomorrow=spike)

    def _on_position_closed(self, pnl_usd: float):
        """ポジション決済完了後に呼ぶ後処理。
        portfolio_risk.pyのポジションクリアと日次PnL記録を行う。
        """
        if not _PORTFOLIO_RISK_AVAILABLE:
            return
        try:
            _pr_clear_positions("spy_bot")
            date_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
            record_daily_pnl(date_str, pnl_usd, "spy_bot")
            log.info(f"[PortfolioRisk] 決済完了: pnl=${pnl_usd:+.2f} positions cleared")
        except Exception as _prc_e:
            log.warning(f"[PortfolioRisk] _on_position_closed失敗: {_prc_e}")

    # ── Research data logging（ログのみ・発注なし） ───────────────────────────
    def _log_research_data(self):
        """
        ガンマスキャルピング・出来高スパイク検知の調査用データをログに記録。
        発注は一切しない。1週間分のデータ蓄積後にバックテストで効果検証する。
        """
        now_et  = datetime.datetime.now(ET)
        ts      = now_et.isoformat()
        expiry  = now_et.strftime("%Y-%m-%d")
        spy     = self.mkt.get_spy_current()
        if not spy:
            return

        # ① デルタ監視（ポジション保有中のみ）
        positions = self.eng.get_open_positions() if FUTU_AVAILABLE else []
        if positions:
            for pos in positions:
                code = pos.get("code", "")
                if not code:
                    continue
                greeks = self.mkt.get_option_greeks(code)
                if greeks:
                    entry = {
                        "ts": ts, "type": "delta_monitor",
                        "code": code, "spy": spy,
                        "delta": greeks.get("delta"),
                        "gamma": greeks.get("gamma"),
                        "theta": greeks.get("theta"),
                        "iv":    greeks.get("iv"),
                    }
                    _append_research_log("delta_monitor.jsonl", entry)

        # ② 出来高スパイク（5分ごと＝research_tick×2が10の倍数）
        self._vol_tick = getattr(self, "_vol_tick", 0) + 1
        if self._vol_tick >= 5:
            self._vol_tick = 0
            vols = self.mkt.scan_option_volumes(expiry, spy)
            if vols:
                entry = {"ts": ts, "type": "volume_scan", "spy": spy, "strikes": vols}
                _append_research_log("volume_spike.jsonl", entry)
                # スパイク候補をログ出力（出来高上位3件）
                top = sorted(vols, key=lambda x: x["volume"], reverse=True)[:3]
                for t in top:
                    if t["volume"] > 500:
                        log.info(f"[RESEARCH] Vol spike: {t['type']} ${t['strike']:.0f} "
                                 f"vol={t['volume']} OI={t['open_interest']}")

    # ── P1-2: 日次最大損失チェック ────────────────────────────────────────────
    def _check_daily_loss_limit(self) -> bool:
        """当日の累積損失が口座の5%を超えたら取引停止。
        condor_pnl.jsonの当日exitレコードのpnl_usdを合算して判定。
        Returns True if halt triggered (or already halted).
        """
        if self._daily_loss_halted:
            return True

        today_str = datetime.datetime.now(ET).strftime("%Y-%m-%d")
        today_exits = [
            t for t in load_pnl()
            if t.get("event") == "exit" and t.get("date") == today_str
        ]
        cumulative_loss = sum(t.get("pnl_usd", 0) or 0 for t in today_exits)

        if cumulative_loss >= 0:
            return False  # まだ損失なし or プラス

        # 初期資金を取得（P1-2: 口座残高の5%をリミットとする）
        try:
            account_cash = self.eng.get_account_cash()
        except Exception:
            account_cash = 10000.0  # フォールバック

        daily_loss_limit = -(account_cash * 0.05)

        if cumulative_loss < daily_loss_limit:
            self._daily_loss_halted = True
            self.traded_today = True
            msg = (f"日次最大損失ヒット: ${cumulative_loss:.0f} < 制限${daily_loss_limit:.0f} "
                   f"(口座${account_cash:.0f}の5%) → 本日取引停止")
            log.warning(msg)
            pushover_alert("SPY CS 日次損失上限", msg, priority=1)
            return True

        return False

    # ── Multi-symbol entry/exit helpers ──────────────────────────────────────
    def _try_multi_symbol_entry(self, symbol: str, tactic: str) -> None:
        """マルチ銘柄2銘柄目以降のエントリーを試行する。

        戦術タイプに応じて既存Engineを再利用または簡易CSエントリーを行う。
        ORB向き銘柄("buy"): ORBEngineを銘柄を一時切替してエントリー。
        CS/IC向き銘柄("sell"): EntryBuilderを使ってCSエントリー。

        Args:
            symbol: "US.TSLA" 等
            tactic: "sell" (CS) / "buy" (ORB)
        """
        if not self.builder:
            log.warning(f"[Multi] {symbol.replace('US.','')} EntryBuilder未初期化 → スキップ")
            return

        try:
            # ── 証拠金配分チェック ─────────────────────────────────────────
            _cash = self.eng.get_account_cash()
            if _cash <= 0:
                log.warning(f"[Multi] {symbol.replace('US.','')} 口座残高取得失敗 → スキップ")
                return

            # マルチ銘柄時の推定リスク（1銘柄あたりの上限）
            try:
                from portfolio_risk import calc_multi_symbol_allocation, can_take_risk_multi
                _alloc = calc_multi_symbol_allocation(
                    [i["symbol"] for i in self.active_symbols],
                    _cash,
                )
                _max_risk = _alloc.get(symbol, _cash * 0.02)
                _can_enter = can_take_risk_multi(_max_risk * 0.5, symbol, _cash)
            except Exception:
                _can_enter = True  # フォールバック: チェックをスキップ

            if not _can_enter:
                log.info(f"[Multi] {symbol.replace('US.','')} 証拠金制約でエントリーなし")
                return

            if tactic == "buy" and self.orb_engine is not None:
                # ORB Engine: 銘柄を一時切替して記録 + エントリー
                _orig_sym  = self.mkt.underlying_code
                _orig_orb  = self.orb_engine.today_vix
                try:
                    # ORBエンジンの銘柄を切替えてブレイクアウトチェック
                    self.mkt.underlying_code = symbol
                    _orb_dir = self.orb_engine.check_breakout()
                    if _orb_dir:
                        _orb_pos = self.orb_engine.execute_entry(_orb_dir)
                        if _orb_pos:
                            self.multi_positions[symbol] = {
                                "tactic": "orb_buy",
                                "position": _orb_pos,
                                "engine": "orb",
                                "entry_time": datetime.datetime.now(ET).isoformat(),
                            }
                            log.info(
                                f"[Multi] {symbol.replace('US.','')} ORBエントリー完了 "
                                f"direction={_orb_dir}"
                            )
                    else:
                        log.info(f"[Multi] {symbol.replace('US.','')} ORBブレイクアウトなし → スキップ")
                finally:
                    self.mkt.underlying_code = _orig_sym
            else:
                # CS売り: 既存のrun_standard_entry()と同じパラメータで発注
                # ただしself.underlying_codeを一時切替
                _orig_sym2 = self.underlying_code
                try:
                    self.underlying_code        = symbol
                    self.mkt.underlying_code    = symbol
                    # VIXと環境スコアを再取得
                    _ms_vix = self.mkt.get_vix() or 20.0
                    _ms_params = get_params(_ms_vix, STANDARD_PARAMS)
                    if _ms_params is None:
                        log.info(f"[Multi] {symbol.replace('US.','')} VIX={_ms_vix:.1f} CS売りスキップ")
                        return
                    _ms_cash = self.eng.get_account_cash()
                    _ms_qty  = calc_qty(_ms_cash, _ms_params, paper=self.paper)
                    # [TMR] NASA Self-Checking Pair: arithmetic を2経路で照合 (Multi)
                    if _QTY_CALCULATOR_AVAILABLE:
                        try:
                            _tmr_verify_spread_qty(
                                _ms_cash,
                                _ms_params.get("width", 10),
                                _ms_params.get("capital_pct", 0.55),
                                _ms_qty,
                            )
                        except QtyMismatchError:
                            pushover("SPY Multi", "[TMR ERROR] qty arithmetic mismatch — order BLOCKED", priority=1)
                            return
                    if _ms_qty <= 0:
                        log.info(f"[Multi] {symbol.replace('US.','')} qty=0 → スキップ")
                        return
                    _ms_expiry = self.get_expiry_0dte()
                    # EntryBuilderのplace()を呼ぶ（symbol対応）
                    if self.builder:
                        _ms_result = self.builder.place(
                            expiry=_ms_expiry,
                            qty=_ms_qty,
                            params=_ms_params,
                            vix=_ms_vix,
                            direction=None,  # SMAで自動判定
                            tactic="multi_cs",
                        )
                        if _ms_result and _ms_result.get("status") == "filled":
                            self.multi_positions[symbol] = {
                                "tactic": "cs_sell",
                                "result": _ms_result,
                                "engine": "cs",
                                "entry_time": datetime.datetime.now(ET).isoformat(),
                                "net_credit": _ms_result.get("net_credit", 0),
                                "sell_code":  _ms_result.get("sell_code"),
                                "buy_code":   _ms_result.get("buy_code"),
                            }
                            log.info(
                                f"[Multi] {symbol.replace('US.','')} CSエントリー完了 "
                                f"credit=${_ms_result.get('net_credit', 0):.2f} qty={_ms_qty}"
                            )
                            pushover(
                                "SPY CS [Multi]",
                                f"{symbol.replace('US.','')} CS売り エントリー\n"
                                f"credit=${_ms_result.get('net_credit', 0):.2f} qty={_ms_qty}"
                            )
                finally:
                    self.underlying_code        = _orig_sym2
                    self.mkt.underlying_code    = _orig_sym2

        except Exception as _me:
            log.warning(f"[Multi] {symbol.replace('US.','')} エントリー例外: {_me}")

    def _check_multi_symbol_exit(self, symbol: str) -> None:
        """マルチ銘柄2銘柄目以降のエグジット条件を確認する。

        TP(80%回収) / SL / 15:50 force close を確認する。
        完了時はself.multi_positionsからエントリーを削除してself._multi_exit_done[symbol]=Trueをセット。

        Args:
            symbol: "US.TSLA" 等
        """
        pos_info = self.multi_positions.get(symbol)
        if not pos_info:
            return

        now_et = datetime.datetime.now(ET)
        tactic = pos_info.get("tactic", "cs_sell")

        try:
            # ── 15:50 force close ───────────────────────────────────────────
            if (now_et.hour > FORCE_CLOSE_H
                    or (now_et.hour == FORCE_CLOSE_H and now_et.minute >= FORCE_CLOSE_M)):
                log.info(f"[Multi] {symbol.replace('US.','')} 15:50 force close")
                if tactic == "orb_buy":
                    _ms_pos = pos_info.get("position")
                    if _ms_pos and self.orb_engine:
                        _orig = self.mkt.underlying_code
                        try:
                            self.mkt.underlying_code = symbol
                            _fc_price = (
                                self.orb_engine._get_option_price(_ms_pos)
                                or _ms_pos.entry_price * 0.3
                            )
                            self.orb_engine._close_position(_ms_pos, _fc_price, "force_close_multi")
                        finally:
                            self.mkt.underlying_code = _orig
                elif tactic == "cs_sell":
                    sell_code = pos_info.get("sell_code")
                    buy_code  = pos_info.get("buy_code")
                    if sell_code and buy_code:
                        # 買い戻し: SHORT脚(sell_code)→BUYで閉じる、LONG脚(buy_code)→SELLで閉じる
                        # _reverse_leg は original_side の反対方向を発注する
                        self.eng._reverse_leg(sell_code, TrdSide.SELL, 1, f"multi_force_close_{symbol}")
                        self.eng._reverse_leg(buy_code,  TrdSide.BUY,  1, f"multi_force_close_{symbol}")

                self._multi_exit_done[symbol] = True
                del self.multi_positions[symbol]
                pushover("SPY CS [Multi]", f"{symbol.replace('US.','')} force close (15:50 ET)")
                return

            # ── ORB: check_exit()を委譲 ──────────────────────────────────
            if tactic == "orb_buy" and self.orb_engine:
                _ms_pos = pos_info.get("position")
                if _ms_pos:
                    _orig = self.mkt.underlying_code
                    try:
                        self.mkt.underlying_code = symbol
                        # 現在価格を取得してcheck_exit()
                        _ms_price = self.orb_engine._get_option_price(_ms_pos)
                        if _ms_price is not None:
                            _exit_reason = _ms_pos.check_exit(_ms_price)
                            if _exit_reason:
                                _exit_result = self.orb_engine._close_position(
                                    _ms_pos, _ms_price, _exit_reason)
                                if _exit_result:
                                    _pnl = _exit_result.get("pnl_usd", 0)
                                    log.info(
                                        f"[Multi] {symbol.replace('US.','')} ORB決済 "
                                        f"reason={_exit_reason} pnl=${_pnl:+.2f}"
                                    )
                                    pushover(
                                        "SPY CS [Multi]",
                                        f"{symbol.replace('US.','')} ORB決済\n"
                                        f"reason={_exit_reason} pnl=${_pnl:+.2f}"
                                    )
                                self._multi_exit_done[symbol] = True
                                del self.multi_positions[symbol]
                    finally:
                        self.mkt.underlying_code = _orig

        except Exception as _ee:
            log.warning(f"[Multi] {symbol.replace('US.','')} エグジット確認例外: {_ee}")

    # ── ペーパー大量検証モード: エントリー/エグジット ──────────────────────────────

    def _try_mass_verify_entry(self, symbol: str, tactic: str, key: str) -> None:
        """ペーパー大量検証モード用エントリー。

        銘柄×戦術の組み合わせごとに独立してエントリーを試行する。
        PortfolioRisk制限なし・1銘柄複数戦術同時保有可能。
        エントリー成功時は self._mass_verify_positions[key] にポジション情報を記録し、
        condor_pnl.json に symbol/tactic タグ付きで entry イベントを記録する。
        """
        if not self.builder:
            log.debug(f"[MassVerify] {key} EntryBuilder未初期化 → スキップ")
            return

        try:
            _orig_sym = self.underlying_code
            _orig_mkt = self.mkt.underlying_code
            try:
                # 銘柄を一時切替
                self.underlying_code     = symbol
                self.mkt.underlying_code = symbol

                if tactic == "orb_buy" and self.orb_engine is not None:
                    # ── ORB戦術 ────────────────────────────────────────────
                    # [2026-04-18] ORB は SPY 専用（内部ロジックSPY hardcode）のため
                    # 他銘柄選定時は即スキップ。WARNING 出さない。
                    if symbol != UNDERLYING_CODE:
                        log.debug(
                            f"[MassVerify] {symbol.replace('US.','')}×orb_buy "
                            f"スキップ(ORBはSPY専用)"
                        )
                        return
                    _orb_dir = self.orb_engine.check_breakout()
                    if _orb_dir is None:
                        # dry-testでは方向をシミュレート
                        if DRY_TEST:
                            _orb_dir = "CALL"
                        else:
                            log.debug(
                                f"[MassVerify] {symbol.replace('US.','')}×orb_buy "
                                f"ブレイクアウトなし → スキップ"
                            )
                            return
                    _orb_pos = self.orb_engine.execute_entry(_orb_dir)
                    if _orb_pos:
                        self._mass_verify_positions[key] = {
                            "tactic": "orb_buy",
                            "position": _orb_pos,
                            "engine": "orb",
                            "entry_time": datetime.datetime.now(ET).isoformat(),
                            "symbol": symbol,
                        }
                        append_pnl_entry({
                            "event": "entry",
                            "strategy": "mass_verify_orb",
                            "symbol": symbol.replace("US.", ""),
                            "tactic": "orb_buy",
                            "mass_verify_key": key,
                            "direction": _orb_dir,
                        })
                        log.info(
                            f"[MassVerify] {symbol.replace('US.','')}×orb_buy "
                            f"エントリー完了 direction={_orb_dir}"
                        )

                elif tactic == "straddle_buy" and self.straddle_buy_engine is not None:
                    # ── StraddleBuy戦術（マルチ銘柄対応）───────────────────
                    # mkt.underlying_code は_try_mass_verify_entry冒頭で既にsymbolに切替済み。
                    # execute_entry()内でget_symbol_meta()を使いstrike丸め・コード生成を銘柄別に行う。
                    # US..SPX は PAPER_MASS_VERIFY_SYMBOLS から除外済みのため混入しない。
                    _sb_pos = self.straddle_buy_engine.execute_entry()
                    if _sb_pos:
                        self._mass_verify_positions[key] = {
                            "tactic": "straddle_buy",
                            "position": _sb_pos,
                            "engine": "straddle_buy",
                            "entry_time": datetime.datetime.now(ET).isoformat(),
                            "symbol": symbol,
                        }
                        append_pnl_entry({
                            "event": "entry",
                            "strategy": "mass_verify_straddle_buy",
                            "symbol": symbol.replace("US.", ""),
                            "tactic": "straddle_buy",
                            "mass_verify_key": key,
                        })
                        log.info(
                            f"[MassVerify] {symbol.replace('US.','')}×straddle_buy "
                            f"エントリー完了"
                        )
                    else:
                        log.debug(
                            f"[MassVerify] {symbol.replace('US.','')}×straddle_buy "
                            f"execute_entry失敗 → スキップ"
                        )

                else:
                    # ── CS売り戦術（デフォルト）──────────────────────────────
                    _mv_vix = self.mkt.get_vix() or 20.0
                    _mv_params = get_params(_mv_vix, STANDARD_PARAMS)
                    if _mv_params is None:
                        log.debug(
                            f"[MassVerify] {symbol.replace('US.','')}×cs_sell "
                            f"VIX={_mv_vix:.1f} → パラメータなし スキップ"
                        )
                        return
                    _mv_cash = self.eng.get_account_cash()
                    # P0-2: MassVerify証拠金動的配分。総合count分の1に上限を設定
                    _mv_n_combos = len(self.active_symbols) if self.active_symbols else 1
                    _mv_qty = calc_qty_mass_verify(
                        _mv_cash, _mv_params, _mv_n_combos, paper=self.paper
                    )
                    log.debug(
                        f"[MassVerify] {symbol.replace('US.','')}×cs_sell "
                        f"qty={_mv_qty} (cash={_mv_cash:.0f}, combos={_mv_n_combos})"
                    )
                    if _mv_qty <= 0:
                        log.debug(
                            f"[MassVerify] {symbol.replace('US.','')}×cs_sell "
                            f"qty=0 → スキップ"
                        )
                        return
                    _mv_expiry = self.get_expiry_0dte()
                    # builder.place() は bool を返す（True=成功/False=失敗）
                    _mv_ok = self.builder.place(
                        expiry=_mv_expiry,
                        qty=_mv_qty,
                        params=_mv_params,
                        vix=_mv_vix,
                        direction=None,
                        tactic="MassVerify_CS",
                    )
                    if _mv_ok:
                        # エントリー成功: net_credit は eng._last_entry_fills から取得
                        _ef = (self.eng._last_entry_fills
                               if hasattr(self.eng, "_last_entry_fills") else {})
                        _fill_sell = _ef.get("sell") or 0.0
                        _fill_buy  = _ef.get("buy")  or 0.0
                        _net_credit = round(_fill_sell - _fill_buy, 4) if _fill_sell else 0.0
                        self._mass_verify_positions[key] = {
                            "tactic": "cs_sell",
                            "engine": "cs",
                            "entry_time": datetime.datetime.now(ET).isoformat(),
                            "symbol": symbol,
                            "net_credit": _net_credit,
                            # sell_code/buy_code: eng.get_open_positions()で後から特定
                            # force_close時はeng.close_all_positions()で一括決済
                        }
                        append_pnl_entry({
                            "event": "entry",
                            "strategy": "mass_verify_cs",
                            "symbol": symbol.replace("US.", ""),
                            "tactic": "cs_sell",
                            "mass_verify_key": key,
                            "net_credit": _net_credit,
                            "qty": _mv_qty,
                            "vix": round(_mv_vix, 2),
                        })
                        log.info(
                            f"[MassVerify] {symbol.replace('US.','')}×cs_sell "
                            f"エントリー完了 credit=${_net_credit:.2f} "
                            f"qty={_mv_qty}"
                        )
                    else:
                        log.debug(
                            f"[MassVerify] {symbol.replace('US.','')}×cs_sell "
                            f"place失敗 → スキップ"
                        )

            finally:
                self.underlying_code     = _orig_sym
                self.mkt.underlying_code = _orig_mkt

        except Exception as _mve:
            log.warning(
                f"[MassVerify] {key} エントリー例外: {_mve}\n{traceback.format_exc()}"
            )

    def _check_mass_verify_exit(self, symbol: str, tactic: str, key: str) -> None:
        """ペーパー大量検証モード用エグジット監視。

        TP(80%回収) / SL / 15:50 force close をチェックする。
        決済完了時は self._mass_verify_positions から削除し、
        condor_pnl.json に symbol/tactic タグ付きで exit イベントを記録する。
        """
        pos_info = self._mass_verify_positions.get(key)
        if not pos_info:
            return

        now_et = datetime.datetime.now(ET)
        _tactic = pos_info.get("tactic", "cs_sell")
        _sym    = pos_info.get("symbol", symbol)

        try:
            # ── 15:50 force close ─────────────────────────────────────────────
            _force = (
                now_et.hour > FORCE_CLOSE_H
                or (now_et.hour == FORCE_CLOSE_H and now_et.minute >= FORCE_CLOSE_M)
            )
            if _force:
                log.info(
                    f"[MassVerify] {_sym.replace('US.','')}×{_tactic} "
                    f"15:50 force close"
                )
                _pnl = self._mass_verify_close_position(pos_info, "force_close")
                append_pnl_entry({
                    "event": "exit",
                    "strategy": f"mass_verify_{_tactic}",
                    "symbol": _sym.replace("US.", ""),
                    "tactic": _tactic,
                    "mass_verify_key": key,
                    "reason": "force_close_15:50",
                    "pnl_usd": round(_pnl, 2),
                })
                del self._mass_verify_positions[key]
                log.info(
                    f"[MassVerify] {_sym.replace('US.','')}×{_tactic} "
                    f"force close 完了 pnl=${_pnl:+.2f}"
                )
                return

            # ── ORB: check_exit委譲 ──────────────────────────────────────────
            if _tactic == "orb_buy" and self.orb_engine:
                _mv_pos = pos_info.get("position")
                if _mv_pos:
                    _orig = self.mkt.underlying_code
                    try:
                        self.mkt.underlying_code = _sym
                        _mv_price = self.orb_engine._get_option_price(_mv_pos)
                        if _mv_price is not None:
                            _exit_reason = _mv_pos.check_exit(_mv_price)
                            if _exit_reason:
                                _exit_result = self.orb_engine._close_position(
                                    _mv_pos, _mv_price, _exit_reason
                                )
                                _pnl = _exit_result.get("pnl_usd", 0) if _exit_result else 0.0
                                append_pnl_entry({
                                    "event": "exit",
                                    "strategy": "mass_verify_orb",
                                    "symbol": _sym.replace("US.", ""),
                                    "tactic": "orb_buy",
                                    "mass_verify_key": key,
                                    "reason": _exit_reason,
                                    "pnl_usd": round(_pnl, 2),
                                })
                                log.info(
                                    f"[MassVerify] {_sym.replace('US.','')}×orb_buy "
                                    f"決済完了 reason={_exit_reason} pnl=${_pnl:+.2f}"
                                )
                                del self._mass_verify_positions[key]
                    finally:
                        self.mkt.underlying_code = _orig

            # ── StraddleBuy: check_exit委譲 ──────────────────────────────────
            elif _tactic == "straddle_buy" and self.straddle_buy_engine:
                _sb_exit = self.straddle_buy_engine.check_exit()
                if _sb_exit is not None:
                    _pnl = _sb_exit.get("pnl_usd", 0)
                    append_pnl_entry({
                        "event": "exit",
                        "strategy": "mass_verify_straddle_buy",
                        "symbol": _sym.replace("US.", ""),
                        "tactic": "straddle_buy",
                        "mass_verify_key": key,
                        "reason": _sb_exit.get("reason", ""),
                        "pnl_usd": round(_pnl, 2),
                    })
                    log.info(
                        f"[MassVerify] {_sym.replace('US.','')}×straddle_buy "
                        f"決済完了 reason={_sb_exit.get('reason','')} pnl=${_pnl:+.2f}"
                    )
                    del self._mass_verify_positions[key]

            # ── CS売り: eng.get_open_positions()でポジションを特定してTP/SLチェック ──
            elif _tactic == "cs_sell":
                # DRY_TESTでは mkt.get_option_price() がNoneを返すのでTP/SL監視スキップ
                # 実ペーパーモード（futu接続あり）のみ監視する
                if not DRY_TEST and FUTU_AVAILABLE:
                    net_credit = pos_info.get("net_credit", 0)
                    if net_credit > 0:
                        # eng.get_open_positions()から当該シンボルのCSレッグを特定
                        _sym_bare = _sym.replace("US.", "")
                        _open_pos = self.eng.get_open_positions()
                        _sym_legs = [
                            p for p in _open_pos
                            if _sym_bare in p.get("code", "")
                        ]
                        if len(_sym_legs) >= 2:
                            # SELL脚 (SHORT): qty < 0、BUY脚 (LONG): qty > 0
                            _sell_legs = [p for p in _sym_legs
                                          if p.get("position_side") == "SHORT"
                                          or (p.get("qty", 0) < 0)]
                            _buy_legs  = [p for p in _sym_legs
                                          if p.get("position_side") == "LONG"
                                          or (p.get("qty", 0) > 0)]
                            if _sell_legs and _buy_legs:
                                _sl = _sell_legs[0]
                                _bl = _buy_legs[0]
                                _sell_price = self.mkt.get_option_price(_sl["code"])
                                _buy_price  = self.mkt.get_option_price(_bl["code"])
                                if _sell_price is not None and _buy_price is not None:
                                    _current_cost = _sell_price - _buy_price
                                    # TP: コストが初期クレジットの20%以下（80%回収）
                                    if _current_cost <= net_credit * 0.20:
                                        _pnl = (net_credit - _current_cost) * 100
                                        append_pnl_entry({
                                            "event": "exit",
                                            "strategy": "mass_verify_cs",
                                            "symbol": _sym.replace("US.", ""),
                                            "tactic": "cs_sell",
                                            "mass_verify_key": key,
                                            "reason": "take_profit_80pct",
                                            "pnl_usd": round(_pnl, 2),
                                        })
                                        log.info(
                                            f"[MassVerify] {_sym.replace('US.','')}×cs_sell "
                                            f"TP 80% 決済 pnl=${_pnl:+.2f}"
                                        )
                                        # close legs
                                        # SHORT脚(_sl)はSELLを渡す→BUYで閉じる
                                        # LONG脚(_bl)はBUYを渡す→SELLで閉じる
                                        self.eng._reverse_leg(
                                            _sl["code"], TrdSide.SELL, 1,
                                            f"mv_tp_{_sym}"
                                        )
                                        self.eng._reverse_leg(
                                            _bl["code"], TrdSide.BUY,  1,
                                            f"mv_tp_{_sym}"
                                        )
                                        del self._mass_verify_positions[key]
                                    # SL: コストが初期クレジットの200%超（2倍損失）
                                    elif _current_cost >= net_credit * 2.0:
                                        _pnl = (net_credit - _current_cost) * 100
                                        append_pnl_entry({
                                            "event": "exit",
                                            "strategy": "mass_verify_cs",
                                            "symbol": _sym.replace("US.", ""),
                                            "tactic": "cs_sell",
                                            "mass_verify_key": key,
                                            "reason": "stop_loss_2x",
                                            "pnl_usd": round(_pnl, 2),
                                        })
                                        log.info(
                                            f"[MassVerify] {_sym.replace('US.','')}×cs_sell "
                                            f"SL 2x 決済 pnl=${_pnl:+.2f}"
                                        )
                                        # SHORT脚(_sl)はSELLを渡す→BUYで閉じる
                                        # LONG脚(_bl)はBUYを渡す→SELLで閉じる
                                        self.eng._reverse_leg(
                                            _sl["code"], TrdSide.SELL, 1,
                                            f"mv_sl_{_sym}"
                                        )
                                        self.eng._reverse_leg(
                                            _bl["code"], TrdSide.BUY,  1,
                                            f"mv_sl_{_sym}"
                                        )
                                        del self._mass_verify_positions[key]

        except Exception as _ee:
            log.warning(
                f"[MassVerify] {key} エグジット確認例外: {_ee}"
            )

    def _mass_verify_close_position(self, pos_info: dict, reason: str) -> float:
        """ペーパー大量検証モード: ポジションをクローズしてPnL(USD)を返す。

        DRY_TESTモードではVirtualPositionManagerまたはゼロPnLを返す。
        """
        tactic = pos_info.get("tactic", "cs_sell")
        symbol = pos_info.get("symbol", UNDERLYING_CODE)
        _orig  = self.mkt.underlying_code
        try:
            self.mkt.underlying_code = symbol
            if tactic == "orb_buy" and self.orb_engine:
                _pos = pos_info.get("position")
                if _pos:
                    # [2026-04-18 防衛] entry_price/exit_priceの型エラー防止
                    try:
                        _entry_price = float(getattr(_pos, "entry_price", 0) or 0)
                    except (TypeError, ValueError):
                        log.warning(f"[MassVerify/ORB] entry_price 型不正: {getattr(_pos, 'entry_price', None)!r}")
                        return 0.0
                    _raw_price = self.orb_engine._get_option_price(_pos)
                    try:
                        _fc_price = float(_raw_price) if _raw_price else _entry_price * 0.3
                    except (TypeError, ValueError):
                        log.warning(f"[MassVerify/ORB] get_option_price str返却: {_raw_price!r}")
                        _fc_price = _entry_price * 0.3
                    # ORBPosition entry_price を強制的に float化してから渡す
                    _pos.entry_price = _entry_price
                    _result = self.orb_engine._close_position(
                        _pos, _fc_price, reason
                    )
                    return _result.get("pnl_usd", 0) if _result else 0.0
            elif tactic == "straddle_buy" and self.straddle_buy_engine:
                _sb_exit = self.straddle_buy_engine.check_exit()
                return _sb_exit.get("pnl_usd", 0) if _sb_exit else 0.0
            elif tactic == "cs_sell":
                # eng.get_open_positions()から当該シンボルのポジションを検索してクローズ
                net_credit = pos_info.get("net_credit", 0)
                _sym_bare = symbol.replace("US.", "")
                _open_pos = self.eng.get_open_positions()
                _sym_legs = [
                    p for p in _open_pos
                    if _sym_bare in p.get("code", "")
                ]
                if not DRY_TEST:
                    for _leg in _sym_legs:
                        # SHORT脚(position_side=="SHORT" or qty<0)はSELL→BUYで閉じる
                        # LONG脚(position_side=="LONG"  or qty>0)はBUY→SELLで閉じる
                        _is_short = (
                            _leg.get("position_side") == "SHORT"
                            or _leg.get("qty", 0) < 0
                        )
                        _orig_side = TrdSide.SELL if _is_short else TrdSide.BUY
                        self.eng._reverse_leg(
                            _leg["code"], _orig_side, 1,
                            f"mv_force_close_{symbol}"
                        )
                # PnL計算: 実価格が取れない場合はゼロ概算
                _pnl = 0.0
                if _sym_legs and not DRY_TEST:
                    _sell_legs = [p for p in _sym_legs
                                  if p.get("position_side") == "SHORT"
                                  or (p.get("qty", 0) < 0)]
                    _buy_legs  = [p for p in _sym_legs
                                  if p.get("position_side") == "LONG"
                                  or (p.get("qty", 0) > 0)]
                    if _sell_legs and _buy_legs:
                        _sp = self.mkt.get_option_price(_sell_legs[0]["code"]) or 0.0
                        _bp = self.mkt.get_option_price(_buy_legs[0]["code"])  or 0.0
                        _pnl = (net_credit - (_sp - _bp)) * 100
                return _pnl
        except Exception as _fce:
            log.warning(f"[MassVerify] _mass_verify_close_position 例外: {_fce}")
        finally:
            self.mkt.underlying_code = _orig
        return 0.0

    # ── Exit monitor ──────────────────────────────────────────────────────────
    def check_exits(self):
        """PT 80% / SL dynamic (IntradayMonitor) / 15:50 force close."""
        # P1-2: 日次最大損失制限チェック（毎ループ先頭で確認）
        if self._check_daily_loss_limit():
            return

        if DRY_TEST:
            self._check_exits_dry_test()
            return

        now       = datetime.datetime.now(ET)

        # ── 16:05 ET 満期掃引: 当日の未exitエントリーを満期OTM消滅として記録 ──
        # force_close(15:50)が約定しなかった（OTMでmoomooが拒否）ケースをカバーする。
        # 1日1回のみ実行（_expiry_sweep_done フラグで制御）
        if (now.hour == 16 and now.minute >= 5
                and self.traded_today
                and not getattr(self, "_expiry_sweep_done", False)):
            self._expiry_sweep_done = True
            try:
                today_str = now.strftime("%Y-%m-%d")
                swept = sweep_expiry_pnl(date_str=today_str, dry_run=False)
                if swept:
                    total_swept_pnl = sum(r.get("pnl_usd", 0) or 0 for r in swept)
                    log.info(
                        f"[ExpirySweep] 16:05掃引完了: {len(swept)}件 "
                        f"合計${total_swept_pnl:+.2f} → condor_pnl.json記録済み"
                    )
                    pushover(
                        "SPY CS 満期掃引",
                        f"16:05 満期OTM消滅 {len(swept)}件を記録\n"
                        f"合計P&L: ${total_swept_pnl:+.2f}",
                    )
                    # P&L累積に反映
                    self._on_position_closed(total_swept_pnl)
                else:
                    log.debug("[ExpirySweep] 16:05: 未exitエントリーなし（全記録済み）")
            except Exception as _sweep_e:
                log.warning(f"[ExpirySweep] 16:05掃引エラー: {_sweep_e}")

        positions = self.eng.get_open_positions()
        if not positions:
            return

        # バグ2修正: エグジット記録前に trade_id/signal_id をスナップショットする。
        # ICエントリー等で _current_trade_id が上書きされた場合でも、
        # このループで監視しているポジションのtrade_idを正しく記録できるようにする。
        _snap_trade_id  = self._current_trade_id
        _snap_signal_id = self._current_signal_id

        # ── [GreeksMonitor] 5分ごとにポートフォリオギリシャを計算・ログ出力 ──
        if _GREEKS_MONITOR_AVAILABLE:
            try:
                _now_ts = now.timestamp()
                _last_greeks_check = getattr(self, "_greeks_last_check", 0)
                if _now_ts - _last_greeks_check >= 300:  # 5分 = 300秒
                    self._greeks_last_check = _now_ts
                    _greeks = _gm_calc_portfolio_greeks(positions, self.mkt.quote_ctx)
                    _greek_warnings = _gm_check_greeks_limits(_greeks)
                    if _greek_warnings:
                        for _gw in _greek_warnings:
                            log.warning(f"[Greeks] {_gw}")
            except Exception as _gm_e:
                log.debug(f"[GreeksMonitor] スキップ: {_gm_e}")

        # ── [PortfolioVega] 5分ごとにVega合計を計算・閾値超過時に警告 ─────────
        try:
            _pv_now_ts = now.timestamp()
            _pv_last_check = getattr(self, "_portfolio_vega_last_check", 0)
            if _pv_now_ts - _pv_last_check >= 300:  # 5分 = 300秒
                self._portfolio_vega_last_check = _pv_now_ts
                _pv = calc_portfolio_vega(positions, self.mkt.quote_ctx)
                if _pv["warning"]:
                    log.warning(
                        f"[PortfolioVega] 警告: total_vega={_pv['total_vega']:+.0f} "
                        f"(閾値: ±{VEGA_WARN_THRESHOLD}) "
                        f"positions={_pv['position_count']}"
                    )
                else:
                    log.debug(f"[PortfolioVega] total_vega={_pv['total_vega']:+.0f}")
                # 最新値をインスタンスに保存（daily_summaryで参照）
                self._last_portfolio_vega = _pv
        except Exception as _pv_e:
            log.debug(f"[PortfolioVega] スキップ: {_pv_e}")

        # G-NEW10: 15:55 ET 裸ポジション自動検出
        # force_close(15:50)後も残存するシングルレッグを検出・Pushover通知する
        if (now.hour == 15 and now.minute == 55
                and self.traded_today
                and not getattr(self, "_naked_leg_checked", False)):
            self._naked_leg_checked = True
            try:
                _today_str_naked = now.strftime("%Y-%m-%d")
                _naked_active = [p for p in positions
                                 if not _option_is_expired(p.get("code", ""), _today_str_naked)]
                if _naked_active:
                    _naked_codes = [p.get("code", "?") for p in _naked_active]
                    log.error(f"[NakedLeg] 15:55 残存ポジション検出: {_naked_codes}")
                    pushover_alert(
                        "裸ポジション検出",
                        f"15:55 未決済ポジション残存: {len(_naked_active)}件\n"
                        f"{', '.join(_naked_codes[:5])}\n"
                        f"force close実行中",
                        priority=1,
                    )
                    self.eng.close_all_positions("naked_leg_15:55")
            except Exception as _nl_e:
                log.warning(f"[NakedLeg] 15:55チェックエラー: {_nl_e}")

        # 期限切れポジションおよびqty=0のゾンビを監視対象から除外（決済注文を送らない）
        today_str = now.strftime("%Y-%m-%d")
        active_positions = []
        for pos in positions:
            code = pos.get("code", "")
            # qty=0 のゾンビレコード（futuが返す残骸）は監視対象外
            try:
                if abs(int(float(pos.get("qty", 0)))) == 0:
                    log.debug(f"監視ループ: qty=0のゾンビレコードを無視: {code}")
                    continue
            except (ValueError, TypeError):
                pass
            if _option_is_expired(code, today_str):
                log.warning(f"[Cleanup] Expired 0DTE position removed: {code}")
                # condor_pnl.jsonに失効済みとして記録（net_credit * qty * 100 を利益として記録）
                # バグ修正: t.get("credit") は誤り → net_credit キーを使用。pnl_usdはnet_credit*qty*100で算出。
                try:
                    # 重複記録防止: 同一trade_id + expired_0dte のexitが既にある場合はスキップ
                    _existing_exits = [
                        t for t in load_pnl()
                        if t.get("event") == "exit"
                        and t.get("reason") == "expired_0dte"
                        and t.get("trade_id") == _snap_trade_id
                        and t.get("expired_code") == code
                    ]
                    if _existing_exits:
                        log.debug(f"[Cleanup] expired_0dte already recorded for {code}, skipping")
                    else:
                        entry_record = None
                        for t in load_pnl():
                            if t.get("event") == "entry" and t.get("trade_id") == _snap_trade_id:
                                entry_record = t
                                break
                        entry_credit = entry_record.get("net_credit") if entry_record else None
                        entry_qty    = entry_record.get("qty") if entry_record else None
                        try:
                            pnl_usd = round(float(entry_credit) * int(entry_qty) * 100, 2) if (entry_credit is not None and entry_qty is not None) else 0.0
                        except (ValueError, TypeError):
                            pnl_usd = 0.0
                        append_pnl_entry({
                            "event": "exit",
                            "reason": "expired_0dte",
                            "pnl_usd": pnl_usd,
                            "entry_credit": entry_credit,
                            "exit_status": "expired_otm_full_profit",
                            "trade_id": _snap_trade_id,
                            "signal_id": _snap_signal_id,
                            "expired_code": code,
                        })
                        log.info(f"[Cleanup] expired_0dte recorded: trade_id={_snap_trade_id} pnl=${pnl_usd:+.2f} credit={entry_credit} qty={entry_qty}")
                except Exception as _exp_e:
                    log.warning(f"[Cleanup] append_pnl for expired failed: {_exp_e}")
            else:
                active_positions.append(pos)
        positions = active_positions
        if not positions:
            return

        # force close: 半日取引日は12:45 ET、通常は15:50 ET（エスカレーション付きリトライ）
        _ec_time = get_early_close_time()
        _fc_h = EARLY_CLOSE_FORCE_H if _ec_time else FORCE_CLOSE_H
        _fc_m = EARLY_CLOSE_FORCE_M if _ec_time else FORCE_CLOSE_M
        _fc_label = f"{_fc_h:02d}:{_fc_m:02d}" + (" ET 半日早期 force close" if _ec_time else " ET force close")
        if now.hour > _fc_h or (now.hour == _fc_h and now.minute >= _fc_m):
            if self._force_close_done:
                return  # 完了済み（成功 or 3回失敗後）・無限ループ防止
            log.info(f"{_fc_label} (試行 {self._force_close_retry_count + 1}回目)")
            # P0-2: 決済前にunrealized_plを合算してpnl_usdとして記録する
            # N/Aの場合はcondor_pnl.jsonのentryからcreditベース概算
            force_pnl_usd = 0.0
            force_na_count = 0
            for _p in positions:
                try:
                    _pl = _p.get("unrealized_pl", 0)
                    if _pl not in (None, "N/A", ""):
                        force_pnl_usd += float(_pl)
                    else:
                        force_na_count += 1
                except (ValueError, TypeError):
                    force_na_count += 1
            force_exit_status = "exact"
            force_entry_credit = None
            if force_na_count > 0:
                # entryレコードからcreditフォールバック
                _today_et = now.strftime("%Y-%m-%d")
                _today_entries = [
                    t for t in load_pnl()
                    if t.get("event") == "entry" and t.get("date") == _today_et
                ]
                if _today_entries:
                    _e = _today_entries[-1]
                    _ec = _e.get("net_credit")
                    _eq = _e.get("qty")
                    force_entry_credit = _ec
                    try:
                        force_pnl_usd = round(float(_ec) * int(_eq) * 100, 2)
                        force_exit_status = "estimated"
                        log.info(
                            f"[ForceClose] unrealized_pl=N/A({force_na_count}件) "
                            f"→ creditフォールバック entry_credit={_ec} "
                            f"概算P&L=${force_pnl_usd:.2f}"
                        )
                    except (ValueError, TypeError):
                        force_exit_status = "unavailable"
                        log.warning(
                            f"[ForceClose] unrealized_pl=N/A かつ entryレコード不完全 "
                            f"→ P&L算出不可"
                        )
                else:
                    force_exit_status = "unavailable"
                    log.warning(
                        f"[ForceClose] unrealized_pl=N/A({force_na_count}件) かつ "
                        f"当日entryレコードなし → P&L算出不可"
                    )
            _fc_reason = "early_close_force_close" if _ec_time else "15:50_force_close"
            ok = self.eng.close_all_positions(_fc_reason)
            _fc_fill_stats = _exit_fill_stats(
                self.eng._last_exit_fills if hasattr(self.eng, "_last_exit_fills") else {}
            )
            if ok:
                # 成功: 通常通知して完了
                pushover("SPY CS", f"{_fc_label} {len(positions)}件 完了")
                self._force_close_done = True
                append_pnl_entry({
                    "event": "exit", "reason": _fc_reason,
                    "pnl_usd": round(force_pnl_usd, 2),
                    "entry_credit": force_entry_credit,
                    "exit_status": force_exit_status,
                    "exit_fill_prices": _fc_fill_stats["exit_fill_prices"],
                    "exit_fill_avg": _fc_fill_stats["exit_fill_avg"],
                    "exit_net_cost": _fc_fill_stats["exit_net_cost"],
                    "trade_id": _snap_trade_id,
                    "signal_id": _snap_signal_id,
                })
                check_signal_divergence(_snap_signal_id)
                self.mkt.unsubscribe_all_option_legs()
                self._on_position_closed(force_pnl_usd)
            else:
                # 失敗: リトライカウントを増やす
                self._force_close_retry_count += 1
                remaining = len(positions)
                if self._force_close_retry_count < 3:
                    # 1〜2回目: ログのみ（Pushover不要）
                    log.warning(
                        f"force close リトライ中 ({self._force_close_retry_count}回目) "
                        f"残存 {remaining}件"
                    )
                else:
                    # 3回目: 0DTE失効の可能性が高い。通知は状況報告のみ
                    log.warning(
                        f"force close 3回未約定 残存 {remaining}件 → 0DTE失効の可能性（自動クリーンアップ対象）"
                    )
                    pushover(
                        "SPY CS",
                        f"{_fc_label} 決済3回未約定 {remaining}件\n"
                        f"0DTE失効の可能性が高い（次回ループで自動処理）",
                        priority=0,
                    )
                    self._force_close_done = True
                    append_pnl_entry({
                        "event": "exit", "reason": _fc_reason + "_failed",
                        "pnl_usd": round(force_pnl_usd, 2),
                        "entry_credit": force_entry_credit,
                        "exit_status": force_exit_status,
                        "exit_fill_prices": _fc_fill_stats["exit_fill_prices"],
                        "exit_fill_avg": _fc_fill_stats["exit_fill_avg"],
                        "exit_net_cost": _fc_fill_stats["exit_net_cost"],
                        "trade_id": _snap_trade_id,
                        "signal_id": _snap_signal_id,
                    })
                    check_signal_divergence(_snap_signal_id)
                    self._on_position_closed(force_pnl_usd)
            return

        # 動的ストップ倍率（IntradayMonitorが引き締めた場合はそちらを使用）
        active_stop = STOP_LOSS_MULT
        if self.intraday_monitor:
            active_stop = self.intraday_monitor.current_stop_mult

        # ── P0-4: スプレッド単位でグループ化してnet P&Lでトリガー判定 ──────────
        # futu オプションコード例: US.SPY251216C590000
        # expiry（6桁日付）+ underlying（SPY） でグループキーを生成する
        import re as _re
        _SPREAD_KEY_RE = _re.compile(
            r"^(?:US\.)?([A-Z]+)(\d{6})[CP]"
        )

        def _spread_key(code: str) -> str:
            """コードから 'UNDERLYING_YYMMDD' 形式のスプレッドキーを返す。"""
            m = _SPREAD_KEY_RE.match(code or "")
            if m:
                return f"{m.group(1)}_{m.group(2)}"
            return code  # マッチしない場合はそのままコードをキーにする

        def _safe_float(v) -> float:
            try:
                return float(v) if v not in (None, "N/A", "") else 0.0
            except (ValueError, TypeError):
                return 0.0

        def _get_position_pl(p) -> tuple:
            """ポジションのP&Lを取得する。戻り値: (pl_usd, source)
            source: "pl_val" | "unrealized_pl" | "realtime_cache" | "market_cost_calc" | "unavailable"
            futu SIMULATEではunrealized_plがN/Aになるため優先順位に従って参照する:
              1. pl_val (pl_val_valid=True かつ 0以外)
              2. unrealized_pl
              3. PriceCache（OptionQuoteHandlerのリアルタイムlast_price）+ cost_price
              4. market_val + cost_price から計算
            """
            # 1. pl_val を試す
            pl_val = p.get("pl_val")
            pl_val_valid = p.get("pl_val_valid", False)
            # pl_val_validはboolまたはnp.bool_で返るのでtruthyチェック
            if pl_val_valid and pl_val not in (None, "N/A", ""):
                try:
                    v = float(pl_val)
                    if v != 0.0:
                        return v, "pl_val"
                except (ValueError, TypeError):
                    pass

            # 2. unrealized_pl を試す
            upl = p.get("unrealized_pl")
            if upl not in (None, "N/A", ""):
                try:
                    return float(upl), "unrealized_pl"
                except (ValueError, TypeError):
                    pass

            # 3. PriceCacheのリアルタイム価格 + cost_priceからP&Lを計算
            _pos_code = p.get("code", "")
            _cached_lp = self.mkt.get_cached_option_price(_pos_code, max_age_sec=10.0)
            _cp3 = _safe_float(p.get("cost_price", 0))
            _qty_raw3 = p.get("qty", 0)
            _qty3 = 0.0
            if _qty_raw3 not in (None, "N/A", ""):
                try:
                    _qty3 = float(_qty_raw3)
                except (ValueError, TypeError):
                    pass
            if _cached_lp is not None and _cp3 != 0.0 and _qty3 != 0.0:
                if _qty3 < 0:  # SHORT
                    _pl3 = (_cp3 - _cached_lp) * abs(_qty3) * 100
                else:           # LONG
                    _pl3 = (_cached_lp - _cp3) * _qty3 * 100
                log.debug(
                    f"[ExitMonitor] {_pos_code}: realtime_cache last_price={_cached_lp:.4f} "
                    f"cost={_cp3:.4f} qty={_qty3} → pl=${_pl3:.2f}"
                )
                return _pl3, "realtime_cache"

            # 4. market_val + cost_price から計算
            mv = _safe_float(p.get("market_val", 0))
            cp = _safe_float(p.get("cost_price", 0))
            qty_raw = p.get("qty", 0)
            qty = 0.0
            if qty_raw not in (None, "N/A", ""):
                try:
                    qty = float(qty_raw)
                except (ValueError, TypeError):
                    pass
            if mv != 0.0 and cp != 0.0 and qty != 0.0:
                if qty < 0:  # SHORT: credit received upfront, profit = cost - current_val
                    pl = cp * abs(qty) * 100 + mv
                else:  # LONG
                    pl = mv - cp * qty * 100
                return pl, "market_cost_calc"

            return 0.0, "unavailable"

        # スプレッド単位にポジションをグループ化
        spread_groups: dict = {}
        for pos in positions:
            key = _spread_key(pos.get("code", ""))
            spread_groups.setdefault(key, []).append(pos)

        # condor_pnl.json から当日entryレコードを取得（creditフォールバック用）
        today_et = now.strftime("%Y-%m-%d")
        today_entries = [
            t for t in load_pnl()
            if t.get("event") == "entry" and t.get("date") == today_et
        ]

        def _credit_fallback_pnl(leg_codes: list) -> tuple:
            """
            unrealized_pl / cost_price が N/A の場合に condor_pnl.json の
            net_credit からP&Lを概算する。
            戻り値: (pnl_usd, pl_ratio, exit_status)
            exit_status: "estimated" | "unavailable"
            """
            if not today_entries:
                return None, None, "unavailable"
            # 当日最新entryを使用
            entry = today_entries[-1]
            entry_credit = entry.get("net_credit")
            entry_qty = entry.get("qty")
            if entry_credit is None or entry_qty is None:
                return None, None, "unavailable"
            try:
                entry_credit = float(entry_credit)
                entry_qty = int(entry_qty)
            except (ValueError, TypeError):
                return None, None, "unavailable"

            # 現在のオプション価格をスナップショットから取得してexit costを概算
            exit_cost = 0.0
            snap_ok = False
            if FUTU_AVAILABLE and self.mkt.quote_ctx and leg_codes:
                try:
                    ret_s, snap_df = self.mkt.quote_ctx.get_market_snapshot(leg_codes)
                    if ret_s == RET_OK and not snap_df.empty:
                        for _, row in snap_df.iterrows():
                            mid = (_safe_float(row.get("bid_price", 0))
                                   + _safe_float(row.get("ask_price", 0))) / 2.0
                            exit_cost += mid
                        snap_ok = True
                except Exception:
                    pass

            if not snap_ok:
                # スナップショットも取れない場合：creditそのままを利益とみなす（期限切れ＝full profit）
                pnl_usd = round(entry_credit * entry_qty * 100, 2)
                pl_ratio = 1.0
                return pnl_usd, pl_ratio, "estimated"

            pnl_usd = round((entry_credit - exit_cost) * entry_qty * 100, 2)
            cost_basis = abs(entry_credit * entry_qty * 100)
            pl_ratio = pnl_usd / cost_basis if cost_basis > 0 else 0.0
            return pnl_usd, pl_ratio, "estimated"

        # ── 動的プロフィットターゲット計算 ──────────────────────────────────────
        # market close = force close 時刻 (_fc_h:_fc_m ET) を基準に残り時間を算出
        _close_minutes   = _fc_h * 60 + _fc_m
        _now_minutes     = now.hour * 60 + now.minute
        _hours_remaining = max(0.0, (_close_minutes - _now_minutes) / 60.0)
        try:
            _vix_for_pt = self.mkt.get_vix()
        except Exception:
            _vix_for_pt = None
        if _vix_for_pt and _vix_for_pt > 0:
            _dynamic_pt = calc_dynamic_profit_target(_vix_for_pt, _hours_remaining)
        else:
            _dynamic_pt = PROFIT_TARGET  # VIX取得失敗時はフォールバック
        # [VIXBand] take_profit オーバーライド（高/クライシス帯で調整済みPTを優先）
        _vix_band_pt_override = getattr(self, "_vix_band_take_profit_override", None)
        if _vix_band_pt_override is not None:
            _dynamic_pt = float(_vix_band_pt_override)
            log.debug(
                f"[VIXBand] take_profit override: {_dynamic_pt:.2f} "
                f"(calc_dynamic_profit_target was {calc_dynamic_profit_target(_vix_for_pt, _hours_remaining) if _vix_for_pt else PROFIT_TARGET:.2f})"
            )
        log.debug(
            f"[ExitMonitor] dynamic_pt={_dynamic_pt:.2f} "
            f"(vix={_vix_for_pt}, hours_remaining={_hours_remaining:.2f})"
        )

        for spread_key_str, legs in spread_groups.items():
            try:
                leg_codes = [p.get("code", "") for p in legs]

                # P&L取得（pl_val優先・unrealized_pl・market_cost_calc・unavailableの順）
                leg_pl_results = [_get_position_pl(p) for p in legs]
                unavailable_legs = [
                    legs[i].get("code", "") for i, (_, src) in enumerate(leg_pl_results)
                    if src == "unavailable"
                ]
                for na_code in unavailable_legs:
                    if na_code not in self._warned_na_positions:
                        log.warning(
                            f"[ExitMonitor] {na_code}: unrealized_pl/pl_val/market_val全てN/A "
                            f"— creditフォールバックを試みる（この警告は1回のみ）"
                        )
                        self._warned_na_positions.add(na_code)

                # スプレッド全体のnet P&L（各脚のpl_usdを合算）
                pl_sources = [src for _, src in leg_pl_results]
                total_pl_usd = sum(pl for pl, _ in leg_pl_results)
                total_cost_basis = sum(
                    _safe_float(p.get("cost_price", 0))
                    * abs(int(p.get("qty", 0)) if p.get("qty") not in (None, "N/A", "") else 0)
                    * 100
                    for p in legs
                )
                log.debug(
                    f"[ExitMonitor] {spread_key_str}: pl_sources={pl_sources} "
                    f"total_pl=${total_pl_usd:.2f} cost_basis=${total_cost_basis:.2f}"
                )

                exit_status = "exact"
                # 全脚がunavailableかつcost_basisも0の場合のみcreditフォールバック
                if all(src == "unavailable" for src in pl_sources) and total_cost_basis == 0:
                    # futu から P&L も cost_price も取れない → creditフォールバック
                    fb_pnl, fb_ratio, fb_status = _credit_fallback_pnl(leg_codes)
                    if fb_status == "unavailable":
                        log.warning(
                            f"[ExitMonitor] {spread_key_str}: P&L算出不可 "
                            f"(unrealized_pl=N/A かつ entryレコードなし) → スキップ"
                        )
                        append_pnl_entry({
                            "event": "exit_monitor_skip",
                            "spread_key": spread_key_str,
                            "legs": leg_codes,
                            "exit_status": "unavailable",
                            "reason": "cost_price_and_entry_credit_unavailable",
                        })
                        continue
                    total_pl_usd = fb_pnl
                    pl_ratio = fb_ratio
                    exit_status = fb_status
                    entry_credit_val = today_entries[-1].get("net_credit") if today_entries else None
                    log.info(
                        f"[ExitMonitor] {spread_key_str}: creditフォールバック "
                        f"entry_credit={entry_credit_val} → 概算P&L=${total_pl_usd:.2f} "
                        f"ratio={pl_ratio:.1%}"
                    )
                else:
                    if total_cost_basis != 0:
                        pl_ratio = total_pl_usd / abs(total_cost_basis)
                    else:
                        # P&Lは取れているがcost_basisが0（SIMULATEでcost_priceが0返りのケース）
                        # creditフォールバックでratioを補完する
                        fb_pnl2, fb_ratio2, fb_status2 = _credit_fallback_pnl(leg_codes)
                        if fb_status2 != "unavailable" and fb_ratio2 is not None:
                            pl_ratio = fb_ratio2
                            log.info(
                                f"[ExitMonitor] {spread_key_str}: cost_basis=0 "
                                f"→ creditフォールバックでratio補完 ratio={pl_ratio:.1%}"
                            )
                        else:
                            pl_ratio = 0.0
                            log.warning(
                                f"[ExitMonitor] {spread_key_str}: cost_basis=0 かつ "
                                f"creditフォールバック不可 → pl_ratio=0.0でスキップ扱い"
                            )
                    entry_credit_val = today_entries[-1].get("net_credit") if today_entries else None

                log.debug(f"[ExitMonitor] spread={spread_key_str} legs={len(legs)} "
                          f"net_pl=${total_pl_usd:.2f} ratio={pl_ratio:.1%} "
                          f"status={exit_status}")

                # 改善4: 含み益30%でbreak-even stop移動
                # 含み益率が30%以上に達した場合、ストップをbreak-even（損益ゼロ）ラインに移動する。
                # active_stopを0.0にすることで、pl_ratio <= 0 (= 損益ゼロ以下) でSLが発動する。
                if pl_ratio >= 0.30 and active_stop > 0.0:
                    log.info(
                        f"[BreakEvenStop] {spread_key_str}: 含み益{pl_ratio:.1%} >= 30% → "
                        f"ストップをbreak-even (0%)に移動 (旧active_stop={active_stop:.2f})"
                    )
                    active_stop = 0.0

                # 11:00-12:00 ET デルタボラティリティゾーン: ストップを10%タイトに
                # 出来高スパイク分析で、SPY変動なしでもIV由来のデルタ急変が
                # この時間帯に集中（15件中13件）することが判明。リスクが見えないところで
                # 変動しやすいため、BreakEvenStop確定後にさらに10%締める。
                if 11 <= now.hour < 12 and active_stop > 0.0:
                    _tightened_stop = active_stop * 0.90
                    log.info(
                        f"[ExitMonitor] 11:00-12:00 ET delta volatility zone: "
                        f"stop tightened 10% ({active_stop:.4f} → {_tightened_stop:.4f})"
                    )
                    active_stop = _tightened_stop

                # 改善5: 15:00 ET以降のガンマ急増時early exit
                # 15:00以降はガンマリスクが急増するため、含み益が50%未満なら早期Exit検討
                # バグ1修正: エントリー直後は pl_ratio が当然0.50未満のため、
                # エントリーから最低5分経過してからのみ発動する
                # バグ3修正: exit指示を出したspread_keyを _gamma_exit_pending に積み、
                # 次ループで約定確認中のポジションに再発射するのを防ぐ
                _gee_entry_ts = getattr(self, "_last_entry_ts", None)
                _gee_min_elapsed = (
                    (now - _gee_entry_ts).total_seconds() > 300
                    if _gee_entry_ts is not None else True
                )
                _gee_pending = spread_key_str in getattr(self, "_gamma_exit_pending", set())
                if _gee_pending:
                    log.debug(
                        f"[GammaEarlyExit] {spread_key_str}: exit_pending → スキップ"
                        f"（約定確認待ちまたは決済済み）"
                    )
                elif (now.hour >= 15 and now.minute >= 0
                        and pl_ratio < 0.50
                        and total_pl_usd != 0.0
                        and _gee_min_elapsed):
                    # exit指示発射前に pending フラグをセット（二重発射防止）
                    self._gamma_exit_pending.add(spread_key_str)
                    log.info(
                        f"[GammaEarlyExit] {spread_key_str}: 15:00 ET以降 かつ "
                        f"含み益{pl_ratio:.1%} < 50% → ガンマリスク回避のため早期クローズ"
                    )
                    self.eng.close_all_positions("gamma_early_exit")
                    _gee_fill_stats = _exit_fill_stats(
                        self.eng._last_exit_fills if hasattr(self.eng, "_last_exit_fills") else {}
                    )
                    pushover("SPY CS",
                             f"15:00早期Exit {pl_ratio:.0%} (ガンマリスク回避)", priority=0)
                    append_pnl_entry({
                        "event": "exit", "reason": "gamma_early_exit",
                        "spread_key": spread_key_str,
                        "legs": leg_codes,
                        "pnl_usd": round(total_pl_usd, 2),
                        "pl_ratio": round(pl_ratio, 4),
                        "entry_credit": entry_credit_val,
                        "exit_status": exit_status,
                        "exit_fill_prices": _gee_fill_stats["exit_fill_prices"],
                        "exit_fill_avg": _gee_fill_stats["exit_fill_avg"],
                        "exit_net_cost": _gee_fill_stats["exit_net_cost"],
                        "trade_id": _snap_trade_id,
                        "signal_id": _snap_signal_id,
                    })
                    check_signal_divergence(_snap_signal_id)
                    self.mkt.unsubscribe_all_option_legs()
                    self._on_position_closed(total_pl_usd)
                    break

                if pl_ratio >= _dynamic_pt:
                    log.info(f"PT {pl_ratio:.1%} >= {_dynamic_pt:.0%} "
                             f"→ スプレッド全体クローズ {spread_key_str} {leg_codes}")
                    self.eng.close_all_positions("profit_target")
                    _pt_fill_stats = _exit_fill_stats(
                        self.eng._last_exit_fills if hasattr(self.eng, "_last_exit_fills") else {}
                    )
                    pushover("SPY CS", f"利確 {pl_ratio:.0%} (PT {_dynamic_pt:.0%})")
                    append_pnl_entry({
                        "event": "exit", "reason": "profit_target",
                        "spread_key": spread_key_str,
                        "legs": leg_codes,
                        "pnl_usd": round(total_pl_usd, 2),
                        "pl_ratio": round(pl_ratio, 4),
                        "entry_credit": entry_credit_val,
                        "exit_status": exit_status,
                        "exit_fill_prices": _pt_fill_stats["exit_fill_prices"],
                        "exit_fill_avg": _pt_fill_stats["exit_fill_avg"],
                        "exit_net_cost": _pt_fill_stats["exit_net_cost"],
                        "trade_id": _snap_trade_id,
                        "signal_id": _snap_signal_id,
                    })
                    check_signal_divergence(_snap_signal_id)
                    self.mkt.unsubscribe_all_option_legs()
                    self._on_position_closed(total_pl_usd)
                    break
                elif pl_ratio <= -active_stop:
                    regime_info = ""
                    if self.intraday_monitor:
                        regime_info = f" regime={self.intraday_monitor.current_regime}"
                    log.info(f"SL {pl_ratio:.1%} <= -{active_stop:.2f}{regime_info} "
                             f"→ スプレッド全体クローズ {spread_key_str} {leg_codes}")
                    self.eng.close_all_positions("stop_loss")
                    _sl_fill_stats = _exit_fill_stats(
                        self.eng._last_exit_fills if hasattr(self.eng, "_last_exit_fills") else {}
                    )
                    pushover("SPY CS",
                             f"損切 {pl_ratio:.0%} (SL {active_stop:.0%}{regime_info})",
                             priority=1)
                    append_pnl_entry({
                        "event": "exit", "reason": "stop_loss",
                        "spread_key": spread_key_str,
                        "legs": leg_codes,
                        "pnl_usd": round(total_pl_usd, 2),
                        "pl_ratio": round(pl_ratio, 4),
                        "stop_mult": active_stop,
                        "entry_credit": entry_credit_val,
                        "exit_status": exit_status,
                        "exit_fill_prices": _sl_fill_stats["exit_fill_prices"],
                        "exit_fill_avg": _sl_fill_stats["exit_fill_avg"],
                        "exit_net_cost": _sl_fill_stats["exit_net_cost"],
                        "trade_id": _snap_trade_id,
                        "signal_id": _snap_signal_id,
                    })
                    check_signal_divergence(_snap_signal_id)
                    self.mkt.unsubscribe_all_option_legs()
                    self._on_position_closed(total_pl_usd)
                    break
            except Exception as e:
                log.warning(f"exit monitor spread {spread_key_str}: {e}")

    # ── dry-test用 Exit monitor ────────────────────────────────────────────────
    def _check_exits_dry_test(self):
        """dry-testモード専用のexit判定。
        VirtualPositionManagerのunrealized_plを更新してPT/SL判定する。
        15分後に強制決済して全フローをテストする。
        """
        positions = self.eng._virtual_pos.get_positions()
        if not positions:
            return

        now_et = datetime.datetime.now(ET)
        elapsed_min = (now_et - self._dry_test_start).total_seconds() / 60.0

        # spy現在価格でunrealized_plを更新
        spy_current = self.mkt.get_spy_current()
        if spy_current:
            self.eng._virtual_pos.update_unrealized_pl(spy_current)
            positions = self.eng._virtual_pos.get_positions()

        # 動的ストップ
        active_stop = STOP_LOSS_MULT
        if self.intraday_monitor:
            active_stop = self.intraday_monitor.current_stop_mult

        # スプレッド全体のnet P&L
        total_pl_usd = sum(p.get("unrealized_pl", 0.0) for p in positions)
        # cost_basisはSHORTレグのcost_price * qty * 100
        short_legs = [p for p in positions if p.get("position_side") == "SHORT"]
        total_cost_basis = sum(
            p.get("cost_price", 0.0) * abs(p.get("qty", 0)) * 100
            for p in short_legs
        )
        pl_ratio = (total_pl_usd / total_cost_basis) if total_cost_basis > 0 else 0.0

        log.info(f"[DRY-TEST][ExitMonitor] {len(positions)} positions "
                 f"pl=${total_pl_usd:.2f} ratio={pl_ratio:.1%} elapsed={elapsed_min:.1f}min")

        # dry-testモード: 起動15分後に強制決済（フローの最終ステップをテスト）
        if elapsed_min >= 15.0:
            log.info(f"[DRY-TEST] 15分経過 → force close (dry-test session end)")
            pushover(
                "[DRY-TEST] SPY CS セッション終了",
                f"dry-testモード 15分経過\n"
                f"仮想P&L: ${total_pl_usd:.2f} ({pl_ratio:.0%})\n"
                f"ポジション: {len(positions)}件 → 決済",
            )
            # バグ3修正: _current_trade_id は ICエントリー等で上書きされる場合があるため
            # force close直前にスナップショットして記録する
            _dry_snap_trade_id  = self._current_trade_id
            _dry_snap_signal_id = self._current_signal_id
            append_pnl_entry({
                "event": "exit", "reason": "dry_test_force_close",
                "pnl_usd": round(total_pl_usd, 2),
                "pl_ratio": round(pl_ratio, 4),
                "exit_status": "dry_test",
                "exit_fill_prices": {},
                "exit_fill_avg": None,
                "exit_net_cost": None,
                "trade_id": _dry_snap_trade_id,
                "signal_id": _dry_snap_signal_id,
            })
            check_signal_divergence(_dry_snap_signal_id)
            self._on_position_closed(total_pl_usd)
            self.eng.close_all_positions("dry_test_force_close")
            # ── 各エンジンの dry-test force close ────────────────────────────
            # VirtualPositionManagerとは別に各エンジン専用ポジションもクローズしてPnL記録する
            if self.orb_engine is not None and self.orb_engine.position is not None:
                _orb_pos = self.orb_engine.position
                _orb_exit_price = self.orb_engine._get_option_price(_orb_pos) or _orb_pos.entry_price
                _orb_pnl = (_orb_exit_price - _orb_pos.entry_price) * _orb_pos.qty * 100
                _orb_append_pnl({"event": "exit", "reason": "dry_test_force_close",
                                  "code": _orb_pos.code, "direction": _orb_pos.direction,
                                  "qty": _orb_pos.qty, "entry_price": _orb_pos.entry_price,
                                  "exit_price": _orb_exit_price, "pnl_usd": round(_orb_pnl, 2),
                                  "pnl_pct": round((_orb_exit_price - _orb_pos.entry_price) / _orb_pos.entry_price if _orb_pos.entry_price else 0, 4),
                                  "vix": getattr(_orb_pos, "vix", None),
                                  "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                                  "bot": "orb_atlas"})
                self.orb_engine.position   = None
                self.orb_engine.trade_done = True
                log.info(f"[DRY-TEST][ORB] force close: pnl=${_orb_pnl:+.2f}")
            if self.straddle_buy_engine is not None and self.straddle_buy_engine.position is not None:
                _sb_pos = self.straddle_buy_engine.position
                _sb_cost = _sb_pos.entry_cost
                _sb_pnl  = 0.0  # dry-testでは価格変動なし
                _straddle_buy_append_pnl({"event": "exit", "reason": "dry_test_force_close",
                                           "pnl_usd": round(_sb_pnl, 2), "entry_cost": _sb_cost,
                                           "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                                           "bot": "straddle_buy_atlas"})
                self.straddle_buy_engine.position   = None
                self.straddle_buy_engine.trade_done = True
                log.info(f"[DRY-TEST][STRADDLE_BUY] force close: pnl=${_sb_pnl:+.2f}")
            if self.gamma_scalp_engine is not None:
                _gs_pos = getattr(self.gamma_scalp_engine.straddle_eng, "position", None)
                if _gs_pos is not None:
                    _gs_cost = _gs_pos.total_cost
                    _gamma_scalp_append_pnl({"event": "exit", "reason": "dry_test_force_close",
                                              "total_cost": _gs_cost,
                                              "date": datetime.datetime.now(ET).strftime("%Y-%m-%d"),
                                              "bot": "gamma_scalp_atlas"})
                    self.gamma_scalp_engine.straddle_eng.position = None
                    log.info("[DRY-TEST][GammaScalp] force close straddle position")
            # dry-testモード: セッション正常終了
            log.info("[DRY-TEST] セッション完了。全ロジックテスト終了。")
            raise KeyboardInterrupt("dry-test complete")

        # dry-test: 動的PT計算（通常版と同じロジック）
        _dt_close_minutes   = FORCE_CLOSE_H * 60 + FORCE_CLOSE_M
        _dt_now_minutes     = now_et.hour * 60 + now_et.minute
        _dt_hours_remaining = max(0.0, (_dt_close_minutes - _dt_now_minutes) / 60.0)
        try:
            _dt_vix = self.mkt.get_vix()
        except Exception:
            _dt_vix = None
        if _dt_vix and _dt_vix > 0:
            _dt_dynamic_pt = calc_dynamic_profit_target(_dt_vix, _dt_hours_remaining)
        else:
            _dt_dynamic_pt = PROFIT_TARGET
        # [VIXBand] take_profit オーバーライド（dry-test版）
        _dt_vix_band_pt_override = getattr(self, "_vix_band_take_profit_override", None)
        if _dt_vix_band_pt_override is not None:
            _dt_dynamic_pt = float(_dt_vix_band_pt_override)
            log.debug(f"[VIXBand][DRY-TEST] take_profit override: {_dt_dynamic_pt:.2f}")

        if pl_ratio >= _dt_dynamic_pt:
            log.info(f"[DRY-TEST] PT {pl_ratio:.1%} >= {_dt_dynamic_pt:.0%} → 仮想決済")
            pushover("[DRY-TEST] SPY CS 利確",
                     f"PT {pl_ratio:.0%} 仮想P&L=${total_pl_usd:.2f}")
            append_pnl_entry({
                "event": "exit", "reason": "profit_target",
                "pnl_usd": round(total_pl_usd, 2), "pl_ratio": round(pl_ratio, 4),
                "exit_status": "dry_test",
                "exit_fill_prices": {},
                "exit_fill_avg": None,
                "exit_net_cost": None,
                "trade_id": self._current_trade_id,
                "signal_id": self._current_signal_id,
            })
            check_signal_divergence(self._current_signal_id)
            self._on_position_closed(total_pl_usd)
            self.eng.close_all_positions("profit_target")

        elif pl_ratio <= -active_stop:
            log.info(f"[DRY-TEST] SL {pl_ratio:.1%} <= -{active_stop:.2f} → 仮想決済")
            pushover("[DRY-TEST] SPY CS 損切",
                     f"SL {pl_ratio:.0%} 仮想P&L=${total_pl_usd:.2f}", priority=1)
            append_pnl_entry({
                "event": "exit", "reason": "stop_loss",
                "pnl_usd": round(total_pl_usd, 2), "pl_ratio": round(pl_ratio, 4),
                "stop_mult": active_stop, "exit_status": "dry_test",
                "exit_fill_prices": {},
                "exit_fill_avg": None,
                "exit_net_cost": None,
                "trade_id": self._current_trade_id,
                "signal_id": self._current_signal_id,
            })
            check_signal_divergence(self._current_signal_id)
            self._on_position_closed(total_pl_usd)
            self.eng.close_all_positions("stop_loss")

    # ── Daily summary (9:00 JST = 20:00 ET) ──────────────────────────────────
    def run_daily_summary(self):
        now_et  = datetime.datetime.now(ET)
        now_jst = now_et.astimezone(JST)

        session_date = now_et.strftime("%Y-%m-%d")
        pnl_data     = load_pnl()
        session      = [t for t in pnl_data if t.get("date") == session_date]
        entries      = [t for t in session if t.get("event") == "entry"]
        exits        = [t for t in session if t.get("event") == "exit"]
        session_pnl  = sum(t.get("pnl_usd", 0) or 0 for t in exits)
        wins         = sum(1 for t in exits if (t.get("pnl_usd") or 0) > 0)
        losses       = len(exits) - wins
        week_start   = now_et.date() - datetime.timedelta(days=5)
        weekly_pnl   = sum(t.get("pnl_usd", 0) or 0
                           for t in pnl_data
                           if t.get("event") == "exit" and t.get("date", "") >= str(week_start))
        total_pnl    = sum(t.get("pnl_usd", 0) or 0
                           for t in pnl_data if t.get("event") == "exit")

        # Next day plan
        next_et = (now_et + datetime.timedelta(days=1)).date()
        if next_et.weekday() >= 5:
            plan = "週末休場"
        elif next_et in US_HOLIDAYS:
            plan = "祝日休場"
        else:
            plan = f"10:30ET (+ ORF@13:00ETスタンバイ)"

        mem_warn = ""
        try:
            if MEMORY_WARN_FILE.exists():
                mw = json.loads(MEMORY_WARN_FILE.read_text())
                if mw.get("count", 0) > 0:
                    mem_warn = f"メモリ警告{mw['count']}回(最大{mw.get('max_pct', 0):.0f}%)"
                MEMORY_WARN_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        flags = []
        if self.paper:
            flags.append("PAPER")
        if self.demo_compare:
            flags.append("DEMO")
        flag_str = " [" + "/".join(flags) + "]" if flags else ""

        lines = [f"[REPORT/Atlas] 日次 ({now_jst.strftime('%m/%d')} 09:00JST){flag_str}"]
        if exits:
            lines.append(f"昨日: {len(entries)}エントリー {wins}勝{losses}敗 P&L:${session_pnl:+.0f}")
        elif entries:
            lines.append(f"昨日: {len(entries)}エントリー(決済未確認)")
        else:
            lines.append("昨日: エントリーなし")
        lines.append(f"今日予定: {plan}")
        if mem_warn:
            lines.append(mem_warn)
        lines.append(f"週間:${weekly_pnl:+.0f} / 累計:${total_pnl:+.0f}")

        # G-NEW6: ドローダウン追跡
        try:
            dd_info = calc_dd_peak_ratio()
            if dd_info["trade_count"] > 0:
                lines.append(
                    f"DD: peak=${dd_info['peak_cumulative']:+.0f} "
                    f"現在=${dd_info['current_cumulative']:+.0f} "
                    f"({dd_info['drawdown_pct']:.1f}%)"
                )
        except Exception as _dd_e:
            log.debug(f"run_daily_summary: dd_info error: {_dd_e}")

        # Portfolio Vega（直近のcheck_exitsで計算した値を参照）
        try:
            _pv_data = getattr(self, "_last_portfolio_vega", None)
            if _pv_data is not None:
                _pv_total = _pv_data.get("total_vega", 0.0)
                _pv_warn  = _pv_data.get("warning", False)
                _pv_str = f"Portfolio Vega: {_pv_total:+.0f}"
                if _pv_warn:
                    _pv_str += f" [警告: >{VEGA_WARN_THRESHOLD}]"
                lines.append(_pv_str)
        except Exception as _pv_sum_e:
            log.debug(f"run_daily_summary: portfolio_vega error: {_pv_sum_e}")

        pushover("[REPORT/Atlas]", "\n".join(lines))
        log.info("Daily summary sent")

        # G-NEW5: カテゴリ別成績分析（ログ記録）
        try:
            cat_stats = calc_category_stats()
            log.info("[CategoryStats] VIX帯別: " + str({
                k: f"{v['trades']}T {v['win_rate']}% ${v['total_pnl']:+.0f}"
                for k, v in cat_stats["by_vix_band"].items() if v["trades"] > 0
            }))
            log.info("[CategoryStats] 曜日別: " + str({
                k: f"{v['trades']}T {v['win_rate']}%"
                for k, v in cat_stats["by_weekday"].items() if v["trades"] > 0
            }))
            log.info("[CategoryStats] 戦術別: " + str({
                k: f"{v['trades']}T {v['win_rate']}% ${v['total_pnl']:+.0f}"
                for k, v in cat_stats["by_strategy"].items() if v["trades"] > 0
            }))
        except Exception as _cs_e:
            log.debug(f"run_daily_summary: calc_category_stats error: {_cs_e}")

        # N4: 構造化トレードジャーナル自動生成
        try:
            _gen_daily_trade_journal(session_date, session)
        except Exception as _jl_e:
            log.warning(f"run_daily_summary: trade_journal error: {_jl_e}")

        # G-NEW9: 資金フェーズ確認 + PDTモード報告
        try:
            _cash_usd = self.eng.get_account_cash() / 150.0  # JPY→USD概算(150円/ドル)
            self._last_cash_usd = _cash_usd  # IntradayMonitor._try_delta_hedge()用キャッシュ更新
            _phase = get_capital_phase(_cash_usd)
            log.info(
                f"[CapitalPhase] 現在フェーズ: Phase {_phase['phase']} "
                f"(cash≈${_cash_usd:.0f}, max_qty={_phase['max_qty']})"
            )
            # PDT動作モードをリアルタイムで再評価（翌日の動作モードを更新）
            _new_mode = get_trading_mode(_cash_usd, paper=self.paper)
            if _new_mode != self.trading_mode:
                log.info(
                    f"[PDT] 動作モード変更: {self.trading_mode} → {_new_mode} "
                    f"(cash≈${_cash_usd:.0f})"
                )
                if _new_mode == "full" and self.trading_mode == "pdt_constrained":
                    pushover(
                        "フル戦術モード解禁",
                        f"cash≈${_cash_usd:.0f}（>=$25,000）→ 翌日から全戦術解禁",
                    )
                self.trading_mode = _new_mode
            # デイリーPDT状況をログ（Pushover報告に含める）
            _pdt_summary = (
                f"PDTモード: {self.trading_mode} / "
                f"cash≈${_cash_usd:.0f} / "
                f"Phase {_phase['phase']} / "
                f"当日PDT消費: {self._pdt_trade_count}回"
            )
            log.info(f"[PDT] {_pdt_summary}")
        except Exception as _ph_e:
            log.debug(f"run_daily_summary: capital_phase error: {_ph_e}")

        # 翌日バイアス算出（週末・祝日でもデータは保存する）
        try:
            self.compute_next_day_bias()
        except Exception as e:
            log.warning(f"compute_next_day_bias failed: {e}")

    # ── Next day bias ──────────────────────────────────────────────────────────
    def compute_next_day_bias(self):
        """翌日取引日の方向性バイアスを算出し data/next_day_bias.json に保存。
        run_daily_summary (ET 20:00 = JST 9:00) から呼び出す。

        使用データ（全て Yahoo Finance）:
          - ^VIX9D / ^VIX3M  → Term Structure ratio
          - ^VVIX             → VIX of VIX（ボラ荒れリスク）
          - ES=F              → ES先物 vs 前日終値

        算出ルール:
          term_ratio = VIX9D / VIX3M
            < 0.85 → contango → direction_bias = "bull"
            > 1.00 → backwardation → direction_bias = "bear"
            else   → "neutral"

          vvix > 120 → size_bias = "reduce"
          else       → size_bias = "normal"

          es_chg = (ES現在値 - ES前日終値) / ES前日終値
            > +0.003 → es_direction = "bull"
            < -0.003 → es_direction = "bear"
            else     → "neutral"

        結果は次のキーで保存:
          ts, vix9d, vix3m, term_ratio, vvix, es_price, es_prev_close,
          es_chg_pct, direction_bias, size_bias, es_direction
        """
        now_et = datetime.datetime.now(ET)
        result: dict = {
            "ts":              now_et.isoformat(),
            "vix9d":           None,
            "vix3m":           None,
            "term_ratio":      None,
            "vvix":            None,
            "es_price":        None,
            "es_prev_close":   None,
            "es_chg_pct":      None,
            "direction_bias":  "neutral",
            "size_bias":       "normal",
            "es_direction":    "neutral",
            "error":           None,
        }

        try:
            headers = {"User-Agent": "Mozilla/5.0"}

            # ── VIX9D ──────────────────────────────────────────────────────
            resp9d = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX9D",
                headers=headers, timeout=8,
            )
            vix9d = float(resp9d.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            result["vix9d"] = round(vix9d, 2)

            # ── VIX3M ──────────────────────────────────────────────────────
            resp3m = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX3M",
                headers=headers, timeout=8,
            )
            vix3m = float(resp3m.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            result["vix3m"] = round(vix3m, 2)

            # ── Term Structure ─────────────────────────────────────────────
            if vix3m > 0:
                term_ratio = vix9d / vix3m
                result["term_ratio"] = round(term_ratio, 4)
                if term_ratio < 0.85:
                    result["direction_bias"] = "bull"
                elif term_ratio > 1.00:
                    result["direction_bias"] = "bear"
                else:
                    result["direction_bias"] = "neutral"

            # ── VVIX ───────────────────────────────────────────────────────
            respvv = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EVVIX",
                headers=headers, timeout=8,
            )
            vvix = float(respvv.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            result["vvix"] = round(vvix, 2)
            result["size_bias"] = "reduce" if vvix > 120 else "normal"

            # ── ES先物 ─────────────────────────────────────────────────────
            respES = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/ES%3DF",
                headers=headers, timeout=8,
            )
            es_meta = respES.json()["chart"]["result"][0]["meta"]
            es_price      = float(es_meta["regularMarketPrice"])
            es_prev_close = float(es_meta.get("previousClose") or es_meta.get("chartPreviousClose") or 0)
            result["es_price"]      = round(es_price, 2)
            result["es_prev_close"] = round(es_prev_close, 2)
            if es_prev_close > 0:
                es_chg = (es_price - es_prev_close) / es_prev_close
                result["es_chg_pct"] = round(es_chg * 100, 3)
                if es_chg > 0.003:
                    result["es_direction"] = "bull"
                elif es_chg < -0.003:
                    result["es_direction"] = "bear"
                else:
                    result["es_direction"] = "neutral"

        except Exception as e:
            result["error"] = str(e)
            log.warning(f"compute_next_day_bias data fetch error: {e}")

        # ── JSON保存 ───────────────────────────────────────────────────────
        try:
            NEXT_DAY_BIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
            NEXT_DAY_BIAS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            log.info(f"next_day_bias saved: dir={result['direction_bias']} "
                     f"size={result['size_bias']} es={result['es_direction']} "
                     f"term_ratio={result['term_ratio']} vvix={result['vvix']}")
        except Exception as e:
            log.warning(f"next_day_bias write error: {e}")
            return

        # ── Pushover通知 ───────────────────────────────────────────────────
        if result.get("error"):
            pushover(
                "SPY CS 翌日バイアス (エラー)",
                f"データ取得失敗: {result['error'][:100]}",
            )
            return

        dir_label = {"bull": "ブル寄り", "bear": "ベア寄り", "neutral": "中立"}.get(
            result["direction_bias"], result["direction_bias"]
        )
        es_label = {"bull": "強気", "bear": "弱気", "neutral": "中立"}.get(
            result["es_direction"], result["es_direction"]
        )
        size_label = "サイズ縮小" if result["size_bias"] == "reduce" else "通常サイズ"

        lines = [
            f"翌日バイアス ({now_et.strftime('%m/%d')} ET引け後)",
            f"Term Structure: {result['term_ratio']:.4f} → {dir_label}",
            f"  VIX9D={result['vix9d']} / VIX3M={result['vix3m']}",
            f"VVIX: {result['vvix']} → {size_label}",
            f"ES先物: {result['es_price']} (前日比{result['es_chg_pct']:+.2f}%) → {es_label}",
        ]
        pushover("SPY CS 翌日バイアス", "\n".join(lines))

    # ── Connection test ────────────────────────────────────────────────────────
    def _run_connection_test(self):
        log.info("=== Connection Test ===")
        ok = self.mkt.connect()
        log.info(f"Quote context: {'OK' if ok else 'FAIL'}")
        if ok:
            vix = self.mkt.get_vix()
            log.info(f"VIX: {vix}")
            spy = self.mkt.get_spy_snapshot()
            log.info(f"SPY snapshot: {spy}")
        ok2 = self.eng.connect()
        log.info(f"Trade context: {'OK' if ok2 else 'FAIL'}")
        if ok2:
            cash = self.eng.get_account_cash()
            log.info(f"Cash: ${cash:,.0f}")
        self.mkt.close()
        self.eng.close()
        log.info("=== Connection Test Done ===")

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run_forever(self):
        log.info(f"=== {STRATEGY_NAME} starting ===")
        log.info(f"Mode: {'PAPER ' if self.paper else ''}{'DEMO ' if self.demo_compare else ''}LIVE={not self.paper}")

        if self.test_connect:
            self.mkt.connect()
            self.eng.connect()
            self._run_connection_test()
            return

        flags_str = ""
        if self.paper:
            flags_str += "[PAPER]"
        if self.demo_compare:
            flags_str += "[DEMO]"
        if self.dry_test:
            flags_str += "[DRY-TEST]"
        # V2-C2: 起動時にPushoverトークン有効性を確認する
        _check_pushover_token()
        pushover(f"SPY CS {flags_str}", f"起動{flags_str}")
        fetch_events_weekly()

        if DRY_TEST:
            # dry-testモード: futu接続をスキップ・VirtualPositionManagerのみで動作
            log.info("[DRY-TEST] futu接続スキップ: VirtualPositionManager + Yahoo/Finnhubで動作")
            self.builder = EntryBuilder(self.mkt, self.eng)
            if ENABLE_INTRADAY_MONITOR:
                self.intraday_monitor = IntradayMonitor(self.mkt, self.eng, self)
                log.info("[DRY-TEST] IntradayMonitor initialized (Yahoo VIX使用)")
            # ORBEngine初期化
            if ENABLE_ORB:
                self.orb_engine = ORBEngine(self.mkt, self.eng,
                                            paper=self.paper, dry_test=True)
                log.info("[DRY-TEST] ORBEngine initialized")
            # CalendarEngine初期化
            if ENABLE_CALENDAR:
                self.calendar_engine = CalendarEngine(self.mkt, self.eng,
                                                      paper=self.paper, dry_test=True)
                self.calendar_engine._dry_test_start = self._dry_test_start
                log.info("[DRY-TEST] CalendarEngine initialized")
            # GammaScalpEngine初期化
            if ENABLE_GAMMA_SCALP:
                self.straddle_engine = StraddleEngine(self.mkt, self.eng,
                                                      paper=self.paper, dry_test=True)
                self.gamma_scalp_engine = GammaScalpEngine(
                    self.straddle_engine, self.mkt, self.eng,
                    paper=self.paper, dry_test=True,
                )
                self.gamma_scalp_engine.initialize_atr()
                log.info("[DRY-TEST] GammaScalpEngine initialized")
            # StraddleBuyEngine初期化
            if ENABLE_STRADDLE_BUY:
                self.straddle_buy_engine = StraddleBuyEngine(
                    self.mkt, self.eng, paper=self.paper, dry_test=True)
                log.info("[DRY-TEST] StraddleBuyEngine initialized")
            # IVCrushEngine初期化 (dry_test)
            if ENABLE_IV_CRUSH:
                self.iv_crush_engine = IVCrushEngine(
                    self.mkt, self.eng, paper=self.paper, dry_test=True)
                log.info("[DRY-TEST] IVCrushEngine initialized")
        else:
            if not self.mkt.connect():
                log.error("Quote context connect failed")
                self.consecutive_start_failures += 1
                save_failures(self.consecutive_start_failures)
                if self.consecutive_start_failures >= 3:
                    pushover("SPY CS 起動失敗", f"OpenD接続失敗{self.consecutive_start_failures}回", priority=1)
                return

            if not self.eng.connect():
                log.error("Trade context connect failed")
                self.consecutive_start_failures += 1
                save_failures(self.consecutive_start_failures)
                return

            self.builder = EntryBuilder(self.mkt, self.eng)
            if ENABLE_INTRADAY_MONITOR:
                self.intraday_monitor = IntradayMonitor(self.mkt, self.eng, self)
                log.info("IntradayMonitor initialized")
            # ORBEngine初期化
            if ENABLE_ORB:
                self.orb_engine = ORBEngine(self.mkt, self.eng,
                                            paper=self.paper, dry_test=False)
                log.info("ORBEngine initialized")
            # CalendarEngine初期化
            if ENABLE_CALENDAR:
                self.calendar_engine = CalendarEngine(self.mkt, self.eng,
                                                      paper=self.paper, dry_test=False)
                log.info("CalendarEngine initialized")
            # GammaScalpEngine初期化
            if ENABLE_GAMMA_SCALP:
                self.straddle_engine = StraddleEngine(self.mkt, self.eng,
                                                      paper=self.paper, dry_test=False)
                self.gamma_scalp_engine = GammaScalpEngine(
                    self.straddle_engine, self.mkt, self.eng,
                    paper=self.paper, dry_test=False,
                )
                self.gamma_scalp_engine.initialize_atr()
                log.info("GammaScalpEngine initialized")
            # StraddleBuyEngine初期化
            if ENABLE_STRADDLE_BUY:
                self.straddle_buy_engine = StraddleBuyEngine(
                    self.mkt, self.eng, paper=self.paper, dry_test=False)
                log.info("StraddleBuyEngine initialized")
            # IVCrushEngine初期化
            if ENABLE_IV_CRUSH:
                self.iv_crush_engine = IVCrushEngine(
                    self.mkt, self.eng, paper=self.paper, dry_test=False)
                log.info("IVCrushEngine initialized")
            self.consecutive_start_failures = 0
            save_failures(0)
            log.info("OpenD connected")

        # ── PDT動作モード確定 ────────────────────────────────────────────────
        # ペーパーモードは init で "full" セット済み。本番のみ cash を確認してモードを決定する。
        if not self.paper:
            try:
                _raw_cash = self.eng.get_account_cash()
                # moomooは円建て口座のため JPY→USD 換算（150円/ドル）
                _cash_usd = _raw_cash / 150.0 if _raw_cash > 1000 else _raw_cash
                self._last_cash_usd = _cash_usd  # IntradayMonitor._try_delta_hedge()用
                self.trading_mode = get_trading_mode(_cash_usd, paper=False)
                log.info(
                    f"[PDT] 動作モード確定: {self.trading_mode} "
                    f"(cash≈${_cash_usd:.0f} / ¥{_raw_cash:.0f})"
                )
                if self.trading_mode == "pdt_constrained":
                    pushover(
                        "PDT制約モード",
                        f"cash≈${_cash_usd:.0f}（<$25,000）→ CS/IC 1DTE限定で稼働",
                    )
                else:
                    pushover(
                        "フル戦術モード解禁",
                        f"cash≈${_cash_usd:.0f}（>=$25,000）→ 全戦術解禁",
                    )
            except Exception as _pdt_e:
                log.warning(f"[PDT] モード確定失敗（fullにフォールバック）: {_pdt_e}")
                self.trading_mode = "full"

        # ── 銘柄選択 (premarketフェーズ) ────────────────────────────────────────
        # symbol_selectorで毎朝の環境データ(ATR/IV/流動性/コスト)から最適銘柄を選択する。
        # 選択結果をself.underlying_codeとself.mkt.underlying_codeに反映する。
        # 失敗時はSPYにフォールバック（Bot継続動作）。
        self._select_symbol_premarket()

        # ── マルチ銘柄選択 (premarketフェーズ) ──────────────────────────────────
        # マルチ銘柄モード有効時: 上位N銘柄をactive_symbolsにセットして本日の運用計画を立てる。
        # _select_symbol_premarket()の後に呼ぶ（underlying_codeがセット済みであること）。
        self._select_multi_symbols_premarket()

        # ── ペーパーモード: assessment プリロード ─────────────────────────────
        # 起動直後に assessment を先行取得して _window_assessment にセットする。
        # これにより市場オープン（9:30 ET）直後の最初のtickからエントリー条件チェックが機能する。
        # 本番モードは市場時間帯に動的に取得するので不要。dry-testはループ内で処理。
        if self.paper and not DRY_TEST:
            try:
                _preload_vix = self.mkt.get_vix()
                if _preload_vix is not None:
                    self._window_assessment = premarket_assessment(
                        self.mkt, _preload_vix, self.intraday_monitor
                    )
                    self._window_assessment_time = datetime.datetime.now(ET)
                    log.info(
                        f"[PAPER] 起動時 assessment プリロード完了: "
                        f"score={self._window_assessment.get('score')} "
                        f"rec={self._window_assessment.get('recommendation')} "
                        f"vix={_preload_vix:.2f}"
                    )
                else:
                    log.warning("[PAPER] 起動時 assessment プリロード: VIX取得失敗 → スキップ")
            except Exception as _preload_e:
                log.warning(f"[PAPER] 起動時 assessment プリロード失敗: {_preload_e}")

        # ── 起動時ポジション引継ぎ ──────────────────────────────────────────
        # Bot再起動後に既存ポジションがあれば監視を再開（二重エントリー防止）
        # dry-testモードはVirtualPositionManagerのみなので引継ぎ不要
        if DRY_TEST:
            log.info("[DRY-TEST] 起動時ポジション引継ぎスキップ")
        orphaned = [] if DRY_TEST else self.eng.get_open_positions()
        if orphaned:
            now_et_startup = datetime.datetime.now(ET)
            today_str_startup = now_et_startup.strftime("%Y-%m-%d")
            expired_at_startup = []
            active_at_startup = []
            for p in orphaned:
                code = p.get("code", "")
                # qty=0 のゾンビレコード（futuが返す残骸）は引継ぎ対象外
                try:
                    if abs(int(float(p.get("qty", 0)))) == 0:
                        log.info(f"起動時: qty=0のゾンビレコードを無視: {code}")
                        continue
                except (ValueError, TypeError):
                    pass
                if _option_is_expired(code, today_str_startup):
                    log.warning(f"期限切れポジション検出: {code} → 無視")
                    expired_at_startup.append(code)
                else:
                    active_at_startup.append(p)

            if expired_at_startup:
                # 自動クリーンアップが処理。通常通知で状況報告のみ
                log.info(f"起動時に期限切れポジション検出（自動除去予定）: {expired_at_startup}")

            if active_at_startup:
                # P1修正: ペーパーモードでは既存ポジションをカウントから除外し新規エントリーを許可。
                # MassVerify検証では「昨日のポジション残り」が今日の検証を妨げないようにする。
                # 本番モードは従来通り traded_today=True でエントリーをブロック。
                if self.paper:
                    log.warning(
                        f"起動時に既存ポジション{len(active_at_startup)}件検出 "
                        f"→ ペーパーモードのため本日カウントから除外・新規エントリー許可"
                    )
                    pushover(
                        "SPY Paper 再起動",
                        f"既存ポジション{len(active_at_startup)}件検出\n"
                        f"ペーパーモード: 除外して新規検証継続",
                        priority=0,
                    )
                    # traded_today は False のまま維持（新規エントリーを許可）
                else:
                    log.warning(f"起動時に既存ポジション{len(active_at_startup)}件検出 → 監視再開・本日エントリーなし")
                    pushover("SPY CS 再起動", f"既存ポジション{len(active_at_startup)}件検出\n監視再開・追加エントリーなし", priority=1)
                    self.traded_today = True  # 本番: 追加エントリーを防ぐ
            elif not expired_at_startup:
                pass  # 期限切れも有効ポジションもなし: 何もしない

        if DRY_TEST:
            # dry-testモード: リトライなし・futu close不要
            try:
                self._main_loop()
            except KeyboardInterrupt as e:
                msg = str(e)
                if "dry-test complete" in msg:
                    log.info("[DRY-TEST] セッション完了")
                else:
                    log.info("[DRY-TEST] Stopped by user")
            except Exception as e:
                log.error(f"[DRY-TEST] Unhandled: {e}\n{traceback.format_exc()}")
            return

        if ENABLE_CRASH_RETRY:
            self._run_with_retry()
        else:
            try:
                self._main_loop()
            except KeyboardInterrupt:
                log.info("Stopped by user")
            except Exception as e:
                log.error(f"Unhandled: {e}\n{traceback.format_exc()}")
                pushover_alert("SPY CS クラッシュ", str(e)[:200])
            finally:
                self.mkt.close()
                self.eng.close()

    def _run_with_retry(self):
        """クラッシュ→バックオフ→再接続→ループ再開のリトライラッパー。
        MAX_CRASH_RETRIES 回超えたら終了し LaunchAgent に再起動を委ねる。

        テスト方法:
            _main_loop() の先頭に以下を一時追加してクラッシュを再現する:
                raise RuntimeError("test crash P0")
        """
        crash_count = 0
        while crash_count < MAX_CRASH_RETRIES:
            try:
                self._main_loop()
                break  # 正常終了（17:30 ET graceful exit 等）
            except KeyboardInterrupt:
                log.info("Stopped by user")
                break
            except Exception as e:
                crash_count += 1
                log.error(
                    f"Crash #{crash_count}/{MAX_CRASH_RETRIES}: {e}\n"
                    f"{traceback.format_exc()}"
                )
                pushover_alert(
                    "SPY CS クラッシュ",
                    f"#{crash_count}/{MAX_CRASH_RETRIES}: {str(e)[:150]}",
                )
                if crash_count >= MAX_CRASH_RETRIES:
                    log.error("MAX_CRASH_RETRIES 到達 → 終了（LaunchAgentが再起動）")
                    pushover_alert(
                        "SPY CS リトライ上限到達",
                        f"{MAX_CRASH_RETRIES}回クラッシュ → プロセス終了・LaunchAgent再起動待ち",
                    )
                    break
                log.info(f"Backoff {CRASH_BACKOFF_SEC}s → 再接続...")
                time.sleep(CRASH_BACKOFF_SEC)
                # コンテキスト再接続
                try:
                    self.mkt.close()
                except Exception:
                    pass
                try:
                    self.eng.close()
                except Exception:
                    pass
                if not self.mkt.connect():
                    log.error("再接続: Quote context 失敗 → リトライカウント継続")
                elif not self.eng.connect():
                    log.error("再接続: Trade context 失敗 → リトライカウント継続")
                else:
                    log.info(f"再接続成功 → メインループ再開 (crash #{crash_count})")
        # セッション終了後に必ずコンテキストを閉じる
        for ctx_name, ctx in [("mkt", self.mkt), ("eng", self.eng)]:
            try:
                ctx.close()
            except Exception as _ce:
                log.warning(f"{ctx_name}.close() failed: {_ce}")

    def _main_loop(self):
        """メインループ本体。run_forever() または _run_with_retry() から呼ばれる。
        17:30 ET の正常終了は break で抜ける（return しない）ことで finally を使わず
        呼び出し元の finally に終了処理を委ねる設計。
        """
        while True:
            now = datetime.datetime.now(ET)
            h, m = now.hour, now.minute

            # ── 20:00 ET = 09:00 JST: daily summary ──
            if not DRY_TEST and h == 20 and m == 0 and not self._nightly_checked:
                self.run_daily_summary()
                self._nightly_checked = True
                # Monthly PnL export on 1st of month
                if now.astimezone(JST).day == 1 and not self._monthly_export:
                    self._export_monthly_pnl_csv()
                    self._monthly_export = True

            # ── Hourly memory check ──
            if m == 0:
                memkey = f"_memcheck_{h}"
                if not getattr(self, memkey, False):
                    check_memory_usage()
                    setattr(self, memkey, True)

            # ── Outside market hours → sleep ──
            if DRY_TEST:
                # dry-testモード: 常に市場時間内とみなす
                in_market = True
            else:
                _ec = get_early_close_time()
                if _ec:
                    # 半日取引日: クローズ時刻まで市場内
                    _ec_h, _ec_m = _ec
                    in_market = ((h == 9 and m >= 30) or (10 <= h < _ec_h)
                                 or (h == _ec_h and m < _ec_m))
                else:
                    in_market = (h == 9 and m >= 30) or (10 <= h < 16)
            if not in_market:
                _ec2 = get_early_close_time()
                if _ec2:
                    _ec2_h, _ec2_m = _ec2
                    _final_check_h = _ec2_h  # クローズ時刻以降でfinal check
                    _exit_h = EARLY_CLOSE_EXIT_H
                    _exit_m = EARLY_CLOSE_EXIT_M
                    _exit_label = f"{_exit_h:02d}:{_exit_m:02d}ET (半日)"
                    _final_label = f"{_ec2_h:02d}:{_ec2_m:02d}"
                else:
                    _final_check_h = 16
                    _exit_h = 17
                    _exit_m = 30
                    _exit_label = "17:30ET"
                    _final_label = "16:00"
                if not DRY_TEST and h >= _final_check_h and self.traded_today:
                    # ── final position check (通常16:00 ET / 半日はクローズ時刻) ──
                    today_str_fc = datetime.datetime.now(ET).strftime("%Y-%m-%d")
                    remaining = self.eng.get_open_positions()
                    active = [p for p in remaining
                              if not _option_is_expired(p.get("code", ""), today_str_fc)]
                    if active:
                        log.error(
                            f"{_final_label} ET final check: {len(active)} positions still open! "
                            f"codes={[p.get('code','?') for p in active]}"
                        )
                        pushover_alert(
                            "ポジション残存",
                            f"{_final_label}最終確認: {len(active)}件のポジションが未決済\n手動確認必要",
                            priority=1,
                        )
                        self.eng.close_all_positions(f"{_final_label}_final_check")
                        self.mkt.unsubscribe_all_option_legs()
                    self._reset_daily_state()
                # graceful self-exit (通常17:30 ET / 半日は13:30 ET)
                if not DRY_TEST and h == _exit_h and m >= _exit_m:
                    log.info(f"{_exit_label}: daily session complete, exiting for LaunchAgent restart")
                    pushover("SPY CS", f"本日セッション終了 ({_exit_label})")
                    break
                time.sleep(30)
                continue

            # ── No-trade day → sleep 1h （ペーパーではスキップ：大量検証が目的）──
            if not DRY_TEST and not self.paper and is_notrade_today():
                if not getattr(self, "_notrade_alerted", False):
                    pushover("SPY CS", "本日ノートレード\n（VIX水準またはイベント回避ルール）")
                    self._notrade_alerted = True
                log.info("No-trade day → sleep 1h")
                time.sleep(3600)
                continue

            # ── ORB: 9:35 ET: Opening Range 記録 ─────────────────────────────
            # strategy_selector: premarket_assessmentの評価を共有してORB実行可否を判断
            if self.orb_engine is not None and not self.orb_engine.orb_checked:
                if DRY_TEST:
                    _orb_elapsed_min = (datetime.datetime.now(ET) - self._dry_test_start).total_seconds() / 60.0
                    if _orb_elapsed_min >= 0.5:  # dry-testでは30秒後にORB記録
                        # assessmentをORBEngineに渡す
                        if self._window_assessment:
                            self.orb_engine._assessment = self._window_assessment
                        _orb_vix = self.orb_engine.today_vix or self.mkt.get_vix()
                        if ORBEngine.should_trade_today(_orb_vix, self._window_assessment,
                                                        paper=self.paper):
                            self._orb_premarket_ok = self.orb_engine.premarket_check(
                                self.intraday_monitor)
                            if self._orb_premarket_ok:
                                self.orb_engine.record_opening_range()
                                log.info("[DRY-TEST][ORB] Opening Range 記録完了")
                        else:
                            self.orb_engine.orb_checked = True  # スキップマーク
                            log.info("[DRY-TEST][ORB] should_trade_today=False → スキップ")
                else:
                    # 9:35-11:00 ET の間に1回だけ記録
                    if h == 9 and m >= 35:
                        if self._window_assessment:
                            self.orb_engine._assessment = self._window_assessment
                        _orb_vix = self.mkt.get_vix()
                        if ORBEngine.should_trade_today(_orb_vix, self._window_assessment,
                                                        paper=self.paper):
                            self._orb_premarket_ok = self.orb_engine.premarket_check(
                                self.intraday_monitor)
                            if self._orb_premarket_ok:
                                self.orb_engine.record_opening_range()
                                log.info("[ORB] 9:35 ET Opening Range 記録完了")
                        else:
                            self.orb_engine.orb_checked = True
                            log.info("[ORB] should_trade_today=False → ORBスキップ")

            # ── ORB: ブレイクアウト監視 + エントリー ─────────────────────────
            # PDT制約チェック: pdt_constrained時はORBをスキップ（0DTE買い=PDT消費）
            if self.trading_mode == "pdt_constrained" and not self._orb_entry_attempted:
                log.info("[PDT] pdt_constrained → ORB買いスキップ（0DTE買いはPDT消費）")
                self._orb_entry_attempted = True

            if (self.orb_engine is not None
                    and self.orb_engine.orb_checked
                    and self._orb_premarket_ok
                    and not self.orb_engine.entry_done
                    and not self.orb_engine.trade_done
                    and not self._orb_entry_attempted):
                if DRY_TEST:
                    _orb_elapsed_min = (datetime.datetime.now(ET) - self._dry_test_start).total_seconds() / 60.0
                    if _orb_elapsed_min >= 1.5:  # dry-testでは1.5分後にブレイクアウトシミュレート
                        log.info("[DRY-TEST][ORB] ブレイクアウトシミュレート → CALL")
                        self._orb_entry_attempted = True
                        _orb_pos = self.orb_engine.execute_entry("CALL")
                        if _orb_pos:
                            self.orb_engine.position  = _orb_pos
                            self.orb_engine.entry_done = True
                            if not DRY_TEST and self.mkt:
                                try:
                                    self.mkt.subscribe_option_legs([_orb_pos.code])
                                except Exception:
                                    pass
                else:
                    _orb_direction = self.orb_engine.check_breakout()
                    if _orb_direction is not None:
                        self._orb_entry_attempted = True
                        self.orb_engine.entry_done = True
                        _orb_pos = self.orb_engine.execute_entry(_orb_direction)
                        if _orb_pos:
                            self.orb_engine.position = _orb_pos
                            if FUTU_AVAILABLE and self.mkt:
                                try:
                                    self.mkt.subscribe_option_legs([_orb_pos.code])
                                except Exception:
                                    pass
                    elif not DRY_TEST:
                        # カットオフ到達チェック（11:00 ET 以降はエントリーしない）
                        if (h > ORB_BREAKOUT_CUTOFF_H or
                                (h == ORB_BREAKOUT_CUTOFF_H and m >= ORB_BREAKOUT_CUTOFF_M)):
                            log.info("[ORB] 11:00 ETカットオフ到達 → 本日ORBエントリーなし")
                            self.orb_engine.trade_done = True
                            self._orb_entry_attempted  = True

            # ── ORB: エグジット監視（ポジション保有中の毎tick）─────────────────
            if (self.orb_engine is not None
                    and self.orb_engine.position is not None):
                _orb_exit_result = self.orb_engine.check_exit(self.intraday_monitor)
                if _orb_exit_result is not None:
                    log.info(f"[ORB] 決済完了: reason={_orb_exit_result['reason']} "
                             f"pnl=${_orb_exit_result['pnl_usd']:+.2f}")
                    if not DRY_TEST and self.mkt:
                        try:
                            self.mkt.unsubscribe_all_option_legs()
                        except Exception:
                            pass

            # ── Calendar: エントリー監視（10:30〜12:00 ET）─────────────────────
            if (self.calendar_engine is not None
                    and not self.calendar_engine.entry_done
                    and not self.calendar_engine.trade_done
                    and not self._calendar_entry_attempted):
                _can_enter_calendar = False
                if DRY_TEST:
                    _cal_elapsed = (datetime.datetime.now(ET) - self._dry_test_start).total_seconds() / 60.0
                    if _cal_elapsed >= 3.0:  # dry-testでは3分後にカレンダーエントリー
                        _can_enter_calendar = True
                else:
                    _cal_min = h * 60 + m
                    _cal_entry_min  = CALENDAR_ENTRY_H * 60 + CALENDAR_ENTRY_M
                    _cal_cutoff_min = CALENDAR_CUTOFF_H * 60 + CALENDAR_CUTOFF_M
                    _can_enter_calendar = _cal_entry_min <= _cal_min < _cal_cutoff_min

                if _can_enter_calendar:
                    # 環境チェック
                    _cal_vix     = self.mkt.get_vix() if not DRY_TEST else 25.0
                    _cal_ivr     = self.mkt.calc_ivr(_cal_vix) if _cal_vix else None
                    if _cal_ivr is None and DRY_TEST:
                        _cal_ivr = 80.0  # dry-test用仮想IVR
                    _, _cal_ivr_high = self.mkt.get_ivr_percentiles()
                    _cal_vix_hist = self.mkt.get_vix_history(days=10)
                    if DRY_TEST:
                        _cal_vix_hist = [30.0, 28.0, 26.0, 24.0, 22.0]  # 下降傾向

                    if CalendarEngine.should_trade_today(
                        _cal_vix, _cal_ivr, _cal_ivr_high, _cal_vix_hist,
                        paper=self.paper
                    ):
                        self._calendar_entry_attempted = True
                        _cal_spy = self.mkt.get_spy_current() if not DRY_TEST else 560.0
                        if _cal_spy and _cal_spy > 0:
                            _cal_pos = self.calendar_engine.execute_entry(
                                _cal_spy, _cal_vix or 25.0
                            )
                            if _cal_pos:
                                self.calendar_engine.position   = _cal_pos
                                self.calendar_engine.entry_done = True
                                log.info(
                                    f"[Calendar] エントリー完了: "
                                    f"{_cal_pos.direction} strike={_cal_pos.strike} "
                                    f"debit=${_cal_pos.initial_debit:.2f}"
                                )
                                pushover(
                                    "SPY Calendar",
                                    f"カレンダーSP エントリー\n"
                                    f"{_cal_pos.direction} ATM={_cal_pos.strike} "
                                    f"debit=${_cal_pos.initial_debit:.2f} "
                                    f"qty={_cal_pos.qty}",
                                )
                            else:
                                log.info("[Calendar] エントリー失敗 → 本日スキップ")
                                self.calendar_engine.trade_done = True
                    else:
                        # 環境条件未満 → カットオフ到達後にスキップマーク
                        if not DRY_TEST:
                            _cal_min = h * 60 + m
                            if _cal_min >= CALENDAR_CUTOFF_H * 60 + CALENDAR_CUTOFF_M:
                                log.info("[Calendar] カットオフ到達: 本日エントリーなし")
                                self.calendar_engine.trade_done  = True
                                self._calendar_entry_attempted   = True

            # ── Calendar: エグジット監視（ポジション保有中の毎tick）──────────────
            if (self.calendar_engine is not None
                    and self.calendar_engine.position is not None):
                _cal_exit = self.calendar_engine.check_exit(self.intraday_monitor)
                if _cal_exit is not None:
                    log.info(
                        f"[Calendar] 決済完了: reason={_cal_exit['reason']} "
                        f"pnl=${_cal_exit['pnl_usd']:+.2f}"
                    )
                    pushover(
                        "SPY Calendar",
                        f"カレンダーSP 決済\n"
                        f"reason={_cal_exit['reason']} "
                        f"pnl=${_cal_exit['pnl_usd']:+.2f}",
                    )
                else:
                    # backレッグ単独管理（front満期後）
                    _cal_back = self.calendar_engine.check_back_leg()
                    if _cal_back is not None:
                        log.info(
                            f"[Calendar] backレッグ決済: reason={_cal_back['reason']} "
                            f"pnl=${_cal_back['pnl_usd']:+.2f}"
                        )

            # ── Gamma Scalp: ストラドルエントリー（ET 9:45〜10:30）────────────────
            # PDT制約チェック: pdt_constrained時はGammaScalpをスキップ（0DTE買い=PDT消費）
            if self.trading_mode == "pdt_constrained" and not self._gamma_scalp_entry_attempted:
                log.info("[PDT] pdt_constrained → GammaScalpスキップ（0DTE買いはPDT消費）")
                self._gamma_scalp_entry_attempted = True

            if (self.straddle_engine is not None
                    and self.gamma_scalp_engine is not None
                    and ENABLE_GAMMA_SCALP
                    and not self.straddle_engine.entry_done
                    and not self._gamma_scalp_entry_attempted):
                if DRY_TEST:
                    # dry-testモード: 起動3分後にストラドルエントリー試行
                    _elapsed_min = (datetime.datetime.now(ET) - self._dry_test_start).total_seconds() / 60.0
                    if _elapsed_min >= 3.0:
                        log.info(f"[DRY-TEST] GammaScalp straddle entry ({_elapsed_min:.1f}min after start)")
                        self._gamma_scalp_entry_attempted = True
                        _vix_for_scalp = self.mkt.get_vix()
                        if self.straddle_engine.should_enter_today(_vix_for_scalp):
                            self.straddle_engine.execute_entry()
                        else:
                            log.info(
                                f"[GammaScalp] skip straddle: VIX={_vix_for_scalp} "
                                f"<= {GAMMA_SCALP_VIX_MIN}"
                            )
                else:
                    # 本番/ペーパー: ET 9:45〜10:30 の間にエントリー
                    _gs_min = h * 60 + m
                    _gs_entry_start = GAMMA_SCALP_ENTRY_H * 60 + GAMMA_SCALP_ENTRY_M
                    _gs_cutoff      = GAMMA_SCALP_CUTOFF_H * 60 + GAMMA_SCALP_CUTOFF_M
                    if _gs_entry_start <= _gs_min < _gs_cutoff:
                        self._gamma_scalp_entry_attempted = True
                        _vix_for_scalp = self.mkt.get_vix()
                        if self.straddle_engine.should_enter_today(_vix_for_scalp):
                            self.straddle_engine.execute_entry()
                        else:
                            log.info(
                                f"[GammaScalp] skip straddle: VIX={_vix_for_scalp} "
                                f"<= {GAMMA_SCALP_VIX_MIN}"
                            )
                    elif _gs_min >= _gs_cutoff:
                        # カットオフ到達でスキップマーク
                        log.info("[GammaScalp] カットオフ到達: 本日ストラドルエントリーなし")
                        self._gamma_scalp_entry_attempted = True

            # ── Gamma Scalp: 毎tick監視（ポジション保有中）────────────────────────
            if (self.gamma_scalp_engine is not None
                    and self.straddle_engine is not None
                    and self.straddle_engine.position is not None):
                self.gamma_scalp_engine.tick()

            # ── StraddleBuy: プレマーケットチェック + エントリー ─────────────────
            # strategy_selector がstraddle_buyを選んだ環境（VIX < P25）でエントリー
            # エントリーウィンドウ: 9:45〜10:30 ET (ORBと同じタイミング)
            # PDT制約チェック: pdt_constrained時はStraddleBuyをスキップ（0DTE買い=PDT消費）
            if (self.trading_mode == "pdt_constrained"
                    and not self._straddle_buy_entry_attempted):
                log.info("[PDT] pdt_constrained → StraddleBuyスキップ（0DTE買いはPDT消費）")
                self._straddle_buy_entry_attempted = True

            if (self.straddle_buy_engine is not None
                    and not self.straddle_buy_engine.entry_done
                    and not self.straddle_buy_engine.trade_done
                    and not self._straddle_buy_entry_attempted):
                if DRY_TEST:
                    _sb_elapsed_min = (datetime.datetime.now(ET) - self._dry_test_start
                                       ).total_seconds() / 60.0
                    # premarket_check: 起動後30秒
                    if _sb_elapsed_min >= 0.5 and not self._straddle_buy_premarket_ok:
                        if self.straddle_buy_engine._assessment is None and self._window_assessment:
                            self.straddle_buy_engine._assessment = self._window_assessment
                        _sb_vix = self.straddle_buy_engine.today_vix or self.mkt.get_vix()
                        if StraddleBuyEngine.should_trade_today(_sb_vix, self._window_assessment,
                                                                paper=self.paper):
                            self._straddle_buy_premarket_ok = \
                                self.straddle_buy_engine.premarket_check()
                        else:
                            self._straddle_buy_premarket_ok    = False
                            self._straddle_buy_entry_attempted = True
                            log.info("[DRY-TEST][STRADDLE_BUY] should_trade_today=False → スキップ")
                    # エントリー: 起動後2分
                    if (_sb_elapsed_min >= 2.0 and self._straddle_buy_premarket_ok
                            and not self._straddle_buy_entry_attempted):
                        self._straddle_buy_entry_attempted = True
                        log.info("[DRY-TEST][STRADDLE_BUY] エントリー試行")
                        _sb_pos = self.straddle_buy_engine.execute_entry()
                        if _sb_pos:
                            self.straddle_buy_engine.position   = _sb_pos
                            self.straddle_buy_engine.entry_done = True
                else:
                    # 本番/ペーパー: ET 9:35 に premarket_check、9:45〜10:30 にエントリー
                    if h == 9 and m >= 35 and not self._straddle_buy_premarket_ok:
                        if self._window_assessment:
                            self.straddle_buy_engine._assessment = self._window_assessment
                        _sb_vix = self.mkt.get_vix()
                        if StraddleBuyEngine.should_trade_today(_sb_vix, self._window_assessment,
                                                                paper=self.paper):
                            self._straddle_buy_premarket_ok = \
                                self.straddle_buy_engine.premarket_check()
                            log.info(f"[STRADDLE_BUY] 9:35 premarket_check: "
                                     f"{'OK' if self._straddle_buy_premarket_ok else 'NG'}")
                        else:
                            self._straddle_buy_premarket_ok    = False
                            self._straddle_buy_entry_attempted = True
                            log.info("[STRADDLE_BUY] should_trade_today=False → スキップ")

                    _sb_entry_min = 9 * 60 + 45
                    _sb_cutoff_min = 10 * 60 + 30
                    _current_min = h * 60 + m
                    if (self._straddle_buy_premarket_ok
                            and _sb_entry_min <= _current_min < _sb_cutoff_min
                            and not self._straddle_buy_entry_attempted):
                        self._straddle_buy_entry_attempted = True
                        _sb_pos = self.straddle_buy_engine.execute_entry()
                        if _sb_pos:
                            self.straddle_buy_engine.position   = _sb_pos
                            self.straddle_buy_engine.entry_done = True
                            if FUTU_AVAILABLE and self.mkt:
                                try:
                                    self.mkt.subscribe_option_legs(
                                        [_sb_pos.call_code, _sb_pos.put_code])
                                except Exception:
                                    pass
                    elif (_current_min >= _sb_cutoff_min
                            and not self._straddle_buy_entry_attempted):
                        log.info("[STRADDLE_BUY] 10:30 ET カットオフ → 本日エントリーなし")
                        self._straddle_buy_entry_attempted = True

            # ── StraddleBuy: エグジット + ヘッジ監視（ポジション保有中）─────────
            if (self.straddle_buy_engine is not None
                    and self.straddle_buy_engine.position is not None):
                # エグジット監視
                _sb_exit = self.straddle_buy_engine.check_exit()
                if _sb_exit is not None:
                    log.info(f"[STRADDLE_BUY] 決済完了: reason={_sb_exit['reason']} "
                             f"pnl=${_sb_exit['pnl_usd']:+.2f}")
                    if not DRY_TEST and self.mkt:
                        try:
                            self.mkt.unsubscribe_all_option_legs()
                        except Exception:
                            pass
                else:
                    # エグジットしていない場合のみヘッジ監視
                    self.straddle_buy_engine.check_hedge()

            # ── IVCrush: エントリー（15:00-15:30 ET）+ エグジット監視（翌日9:45-10:15 ET）──
            if self.iv_crush_engine is not None:
                # premarket_check: 9:00-9:30 ET（決算日かどうか確認）
                if not DRY_TEST:
                    if h == 9 and 0 <= m < 30 and not self._iv_crush_premarket_ok:
                        self._iv_crush_premarket_ok = self.iv_crush_engine.premarket_check()
                else:
                    # dry_test: 起動後15秒でpremarket_check
                    _ic_elapsed = (datetime.datetime.now(ET) - self._dry_test_start).total_seconds() / 60.0
                    if _ic_elapsed >= 0.25 and not self._iv_crush_premarket_ok:
                        self._iv_crush_premarket_ok = self.iv_crush_engine.premarket_check()

                # check_entry: 決算日の15:00-15:30 ET
                if (self._iv_crush_premarket_ok
                        and not self.iv_crush_engine.entry_done
                        and not self._iv_crush_entry_attempted):
                    _ic_entered = self.iv_crush_engine.check_entry()
                    if _ic_entered:
                        self._iv_crush_entry_attempted = True
                        log.info("[IVCrush] エントリー完了 → エグジット監視開始")

                # check_exit: ポジション保有中（翌日オープン後）
                if self.iv_crush_engine.is_active():
                    _ic_exit = self.iv_crush_engine.check_exit()
                    if _ic_exit is not None:
                        log.info(
                            f"[IVCrush] 決済完了: reason={_ic_exit['reason']} "
                            f"pnl=${_ic_exit['pnl_usd']:+.2f}"
                        )

            # ── 10:00 ET: ORF check (window: 10:00~10:29) ──
            if DRY_TEST:
                # dry-testモード: 起動1分後にORFチェック実行
                _elapsed_min = (datetime.datetime.now(ET) - self._dry_test_start).total_seconds() / 60.0
                if _elapsed_min >= 1.0 and not self.orf_checked:
                    log.info(f"[DRY-TEST] ORF check triggered ({_elapsed_min:.1f}min after start)")
                    self.check_opening_range()
                    self.orf_checked = True
            else:
                if h == ORF_CHECK_H and m >= ORF_CHECK_M and m < STANDARD_ENTRY_M and not self.orf_checked:
                    self.check_opening_range()
                    self.orf_checked = True

            # ── ペーパーモード: CS/IC 複数回エントリー（90分ごとにリセット）──────────
            # 本番では traded_today=True で1日1件制限。
            # ペーパーモードは全7戦術×複数シナリオの検証が目的なので、
            # 90分（1.5時間）ごとに CS/IC エントリーフラグをリセットして再エントリーを許可する。
            # ORB/Calendar/GammaScalp/StraddleBuy は設計上1日1件でよい（各戦術の性質上）。
            if (self.paper and not DRY_TEST and in_market
                    and self._standard_entry_done and self._paper_last_standard_entry_et is not None):
                _paper_now_et = datetime.datetime.now(ET)
                _paper_elapsed = (_paper_now_et - self._paper_last_standard_entry_et).total_seconds() / 60.0
                if _paper_elapsed >= 90.0:
                    log.info(
                        f"[PAPER] CS/IC 90分経過 ({_paper_elapsed:.0f}min) → エントリーフラグリセット "
                        f"(前回: {self._paper_last_standard_entry_et.strftime('%H:%M')} ET)"
                    )
                    self._standard_entry_done = False
                    self.traded_today = False
                    self._paper_last_standard_entry_et = None

            # ── Standard / ORF entry ──────────────────────────────────────────
            # ENABLE_DYNAMIC_ENTRY_WINDOW=True:
            #   市場時間中（ET 9:45〜15:30）は毎tick条件チェック、揃ったらエントリー。
            #   - standard: orf_triggered=False の場合
            #   - ORF: orf_triggered=True の場合（ORF方向でエントリー）
            #   時刻ウィンドウは廃止。ORFチェック（10:00 ET）は残す。
            # ENABLE_DYNAMIC_ENTRY_WINDOW=False: 従来の固定時刻即エントリー。
            if DRY_TEST:
                # dry-testモード: 起動5分後にstandard entry実行
                _elapsed_min = (datetime.datetime.now(ET) - self._dry_test_start).total_seconds() / 60.0
                if (_elapsed_min >= 5.0 and not self.traded_today
                        and not self._standard_entry_done):
                    log.info(f"[DRY-TEST] Standard entry triggered ({_elapsed_min:.1f}min after start)")
                    self._standard_entry_done = True
                    self.run_standard_entry()
                # dry-testモード: 起動8分後かつORFトリガー済みの場合にORFエントリー
                if (_elapsed_min >= 8.0 and self.orf_triggered and not self.traded_today
                        and not self._orf_entry_done):
                    log.info(f"[DRY-TEST] ORF entry triggered ({_elapsed_min:.1f}min after start)")
                    self._orf_entry_done = True
                    self.run_orf_entry()
            elif not ENABLE_DYNAMIC_ENTRY_WINDOW:
                # フォールバック: 従来の固定時刻動作
                if (h == STANDARD_ENTRY_H and m >= STANDARD_ENTRY_M
                        and not self.traded_today and not self._standard_entry_done):
                    self._standard_entry_done = True
                    self.run_standard_entry()
                if (h == ORF_ENTRY_H and m >= ORF_ENTRY_M and m < 30
                        and self.orf_triggered and not self.traded_today
                        and not self._orf_entry_done):
                    self._orf_entry_done = True
                    self.run_orf_entry()
            else:
                # 常時判定モード: ET 9:45〜15:30 の間、毎tickエントリー条件をチェック
                _market_open_min = h * 60 + m  # 現在の分（ET）
                _min_entry_min   = 9 * 60 + 30 + DYNAMIC_ENTRY_MIN_OPEN_MIN   # 9:45 ET
                _cutoff_min      = DYNAMIC_ENTRY_CUTOFF_H * 60 + DYNAMIC_ENTRY_CUTOFF_M  # 15:30 ET
                _in_entry_zone   = _min_entry_min <= _market_open_min < _cutoff_min

                # 改善2: ORB確定後（10:00 ET以降）のassessment1回再評価
                # ORBは9:30-10:00の最初の30分レンジで確定する。10:00以降に再評価することで
                # ORB確定後の市場状況（方向性・VIX動き）をassessmentに反映させる。
                if (not self._assessment_refreshed
                        and _market_open_min >= 10 * 60
                        and self._window_assessment is not None):
                    _vix_for_orb_refresh = self.mkt.get_vix()
                    if _vix_for_orb_refresh is not None:
                        _now_for_orb = datetime.datetime.now(ET)
                        self._window_assessment = premarket_assessment(
                            self.mkt, _vix_for_orb_refresh, self.intraday_monitor
                        )
                        self._window_assessment_time = _now_for_orb
                        self._assessment_refreshed = True
                        if self.intraday_monitor:
                            self.intraday_monitor.set_morning_score(
                                self._window_assessment["score"]
                            )
                        log.info(
                            f"[DynamicEntry] ORB確定後assessment再評価 (10:00 ET以降1回): "
                            f"score={self._window_assessment.get('score')} "
                            f"recommendation={self._window_assessment.get('recommendation')}"
                        )

                if _in_entry_zone and not self.traded_today:
                    if not self.orf_triggered and not self._standard_entry_done:
                        # ── Standard entry: 条件チェック毎tick ──────────────
                        # assessmentを30分ごとに再取得（初回 or 30分経過）
                        _now_for_assess = datetime.datetime.now(ET)
                        _needs_refresh = (
                            self._window_assessment is None
                            or self._window_assessment_time is None
                            or (_now_for_assess - self._window_assessment_time).total_seconds() >= 1800
                        )
                        if not self._standard_window_open:
                            self._standard_window_open = True
                        if _needs_refresh:
                            _vix_for_assess = self.mkt.get_vix()
                            if _vix_for_assess is not None:
                                _is_refresh = self._window_assessment is not None
                                self._window_assessment = premarket_assessment(
                                    self.mkt, _vix_for_assess, self.intraday_monitor
                                )
                                self._window_assessment_time = _now_for_assess
                                if self.intraday_monitor:
                                    self.intraday_monitor.set_morning_score(
                                        self._window_assessment["score"]
                                    )
                                _action = "refreshed" if _is_refresh else "initialized"
                                log.info(
                                    f"[DynamicEntry] Standard: assessment {_action} at "
                                    f"{h:02d}:{m:02d} ET "
                                    f"score={self._window_assessment.get('score')}, "
                                    f"vrp={self._window_assessment.get('vrp')}, "
                                    f"rec={self._window_assessment.get('recommendation')}"
                                )
                                # スコアが60未満に落ちた場合のポジション保護ログ
                                if _is_refresh and self._window_assessment.get("score", 100) < 60:
                                    log.warning(
                                        f"[DynamicEntry] Assessment refresh: score dropped below 60 "
                                        f"({self._window_assessment.get('score')}) → entry blocked; "
                                        f"existing positions: tighten stops if held"
                                    )
                            else:
                                log.warning("[DynamicEntry] Standard: VIX取得不可 → skip")
                                self._standard_entry_done = True

                        # 毎tick条件チェック（ウォームアップ完了チェックを含む）
                        _warmup_ok = (
                            self.paper  # ペーパーモード: warmupスキップ
                            or not (ENABLE_INTRADAY_MONITOR and self.intraday_monitor)
                            or self.intraday_monitor._warmup_complete
                        )
                        if not _warmup_ok:
                            log.info(f"[DynamicEntry] Standard: warmup未完了 → skip tick")
                        elif self._window_assessment is not None:
                            _entry_ok, _size_factor = self._check_entry_conditions(
                                self._window_assessment
                            )
                            if _entry_ok:
                                log.info(
                                    f"[DynamicEntry] Standard entry conditions met at "
                                    f"{h:02d}:{m:02d} ET "
                                    f"size_factor={_size_factor} → firing run_standard_entry()"
                                )
                                self._standard_entry_done = True
                                self._pending_size_factor = _size_factor
                                self.run_standard_entry()
                            else:
                                _remaining = _cutoff_min - _market_open_min
                                log.info(
                                    f"[DynamicEntry] Standard: conditions not met at "
                                    f"{h:02d}:{m:02d} ET, remaining={_remaining}min to cutoff"
                                )

                    elif self.orf_triggered and not self._orf_entry_done:
                        # ── ORF entry: regime が calm/normal になれば即エントリー ──
                        if not self._orf_window_open:
                            self._orf_window_open = True
                            log.info(f"[DynamicEntry] ORF: monitoring started at {h:02d}:{m:02d} ET")
                        _orf_warmup_ok = (
                            self.paper  # ペーパーモード: warmupスキップ
                            or not (ENABLE_INTRADAY_MONITOR and self.intraday_monitor)
                            or self.intraday_monitor._warmup_complete
                        )
                        _orf_regime_ok = True
                        if self.intraday_monitor:
                            _orf_r = self.intraday_monitor.current_regime
                            if _orf_r in ("elevated", "crisis"):
                                _orf_regime_ok = False
                        if not _orf_warmup_ok:
                            log.info(f"[DynamicEntry] ORF: warmup未完了 → skip tick")
                        elif _orf_regime_ok:
                            log.info(
                                f"[DynamicEntry] ORF entry conditions met at "
                                f"{h:02d}:{m:02d} ET → firing run_orf_entry()"
                            )
                            self._orf_entry_done = True
                            self.run_orf_entry()
                        else:
                            _remaining = _cutoff_min - _market_open_min
                            log.info(
                                f"[DynamicEntry] ORF: regime={self.intraday_monitor.current_regime} "
                                f"at {h:02d}:{m:02d} ET, remaining={_remaining}min to cutoff"
                            )

                elif not _in_entry_zone and _market_open_min >= _cutoff_min:
                    # 15:30 ET を過ぎてもエントリー未完了の場合は当日スキップを記録
                    if not self._standard_entry_done and not self.orf_triggered:
                        if self._standard_window_open:
                            log.info(
                                f"[DynamicEntry] Standard: カットオフ({DYNAMIC_ENTRY_CUTOFF_H}:"
                                f"{DYNAMIC_ENTRY_CUTOFF_M:02d}ET)到達 → 本日エントリーなし "
                                f"(score={self._window_assessment.get('score') if self._window_assessment else 'N/A'})"
                            )
                            pushover(
                                "SPY CS",
                                f"エントリーなし: {DYNAMIC_ENTRY_CUTOFF_H}:{DYNAMIC_ENTRY_CUTOFF_M:02d}ET"
                                f"カットオフ到達 "
                                f"(score={self._window_assessment.get('score') if self._window_assessment else 'N/A'})"
                            )
                        self._standard_entry_done = True
                    if not self._orf_entry_done and self.orf_triggered:
                        if self._orf_window_open:
                            log.info(
                                f"[DynamicEntry] ORF: カットオフ({DYNAMIC_ENTRY_CUTOFF_H}:"
                                f"{DYNAMIC_ENTRY_CUTOFF_M:02d}ET)到達 → ORFエントリーなし"
                            )
                            pushover(
                                "SPY CS",
                                f"ORFエントリーなし: {DYNAMIC_ENTRY_CUTOFF_H}:{DYNAMIC_ENTRY_CUTOFF_M:02d}ET"
                                f"カットオフ到達（regime悪化または条件未達）"
                            )
                        self._orf_entry_done = True
                        self.traded_today = True

            # ── sleep間隔をVIXレジームに応じて動的に決定 ──
            # calm/normal: 30秒, elevated: 15秒, crisis: 5秒
            if ENABLE_INTRADAY_MONITOR and self.intraday_monitor:
                _regime = self.intraday_monitor.current_regime
            else:
                _regime = "normal"
            if _regime == "crisis":
                _sleep_sec = 5
            elif _regime == "elevated":
                _sleep_sec = 15
            else:
                _sleep_sec = 5
            # 60秒ごとのtickに必要な累積tick数（60 / sleep_sec）
            _ticks_per_60s = 60 // _sleep_sec  # crisis:12, elevated:12, normal:12

            # ── Intraday Monitor (60秒ごとにVIXレジーム監視) ──
            if ENABLE_INTRADAY_MONITOR and self.intraday_monitor:
                self._intraday_tick_count += 1
                if self._intraday_tick_count >= _ticks_per_60s:  # _sleep_sec × _ticks_per_60s = 60秒
                    self._intraday_tick_count = 0
                    self.intraday_monitor.tick()
                    # [VIXBand] ATM subscribe: 場中のみSPY価格でATMを追跡
                    _now_et_atm = datetime.datetime.now(ET)
                    if not DRY_TEST and FUTU_AVAILABLE and 9 <= _now_et_atm.hour < 16:
                        try:
                            _spy_snap = self.mkt.get_spy_snapshot()
                            if _spy_snap and _spy_snap.get("last_price", 0) > 0:
                                self.mkt.update_atm_subscribe(_spy_snap["last_price"])
                        except Exception as _atm_e:
                            log.debug(f"[ATMSubscribe] tick更新スキップ: {_atm_e}")

            # ── Exit monitor (every 30s during market hours) ──
            self.check_exits()

            # ── Multi-symbol: 追加銘柄のエントリー/エグジット監視 ───────────────
            # 通常モード: active_symbols の2銘柄目以降を処理（1銘柄目は既存フローで処理済み）
            # 大量検証モード: active_symbols 全体を処理（全銘柄×全戦術を毎tick監視）
            _mass_verify_active = self.paper and PAPER_MASS_VERIFY_MODE and self._multi_enabled

            if _mass_verify_active and self.active_symbols:
                # ── ペーパー大量検証モード ──────────────────────────────────────
                _market_min_mv = h * 60 + m
                if DRY_TEST:
                    # dry-testモード: 起動から2分後にエントリーウィンドウを開く
                    _dry_elapsed_mv = (
                        datetime.datetime.now(ET) - self._dry_test_start
                    ).total_seconds() / 60.0
                    _entry_ok_zone_mv = _dry_elapsed_mv >= 2.0
                else:
                    _entry_ok_zone_mv = (
                        (9 * 60 + 30 + DYNAMIC_ENTRY_MIN_OPEN_MIN)
                        <= _market_min_mv
                        < (DYNAMIC_ENTRY_CUTOFF_H * 60 + DYNAMIC_ENTRY_CUTOFF_M)
                    )
                _now_et_mv = datetime.datetime.now(ET)

                for _mv_item in self.active_symbols:
                    _mv_sym    = _mv_item["symbol"]
                    _mv_tactic = _mv_item.get("tactic", "cs_sell")
                    _mv_key    = f"{_mv_sym}_{_mv_tactic}"

                    # ── エントリー監視 ──────────────────────────────────────────
                    if _entry_ok_zone_mv:
                        _last_et = self._mass_verify_last_entry.get(_mv_key)
                        _interval_ok = (
                            _last_et is None
                            or (_now_et_mv - _last_et).total_seconds() / 60.0
                               >= PAPER_MASS_VERIFY_ENTRY_INTERVAL_MIN
                        )
                        # ポジション保有中は再エントリーしない
                        _pos_open = _mv_key in self._mass_verify_positions

                        if _interval_ok and not _pos_open:
                            log.info(
                                f"[MassVerify] {_mv_sym.replace('US.','')}×{_mv_tactic} "
                                f"エントリー試行"
                                + (f" (前回から{(_now_et_mv - _last_et).total_seconds()/60:.0f}分)"
                                   if _last_et else " (初回)")
                            )
                            self._mass_verify_last_entry[_mv_key] = _now_et_mv
                            self._try_mass_verify_entry(_mv_sym, _mv_tactic, _mv_key)

                    elif (_market_min_mv >= DYNAMIC_ENTRY_CUTOFF_H * 60 + DYNAMIC_ENTRY_CUTOFF_M
                            and _mv_key not in self._mass_verify_last_entry):
                        log.debug(
                            f"[MassVerify] {_mv_sym.replace('US.','')}×{_mv_tactic} "
                            f"カットオフ → 本日エントリーなし"
                        )
                        self._mass_verify_last_entry[_mv_key] = None  # 試行済みマーク

                    # ── エグジット監視 ─────────────────────────────────────────
                    if _mv_key in self._mass_verify_positions:
                        self._check_mass_verify_exit(_mv_sym, _mv_tactic, _mv_key)

            elif self._multi_enabled and len(self.active_symbols) > 1:
                # ── 通常マルチ銘柄モード（本番 or ペーパー大量検証無効時）──────────
                _market_min_multi = h * 60 + m
                _entry_ok_zone = (
                    (9 * 60 + 30 + DYNAMIC_ENTRY_MIN_OPEN_MIN)
                    <= _market_min_multi
                    < (DYNAMIC_ENTRY_CUTOFF_H * 60 + DYNAMIC_ENTRY_CUTOFF_M)
                )
                for _ms_item in self.active_symbols[1:]:  # 2銘柄目以降
                    _ms_sym = _ms_item["symbol"]
                    _ms_tactic = _ms_item.get("tactic", "cs_sell")

                    # エントリー監視
                    if (_entry_ok_zone
                            and not self._multi_entry_attempted.get(_ms_sym, False)):
                        _ms_entry_ok = False
                        if self._window_assessment is not None:
                            _ms_score = self._window_assessment.get("score", 0)
                            _ms_entry_ok = (_ms_score >= DYNAMIC_ENTRY_MIN_ENV_SCORE)

                        if _ms_entry_ok:
                            log.info(
                                f"[Multi] {_ms_sym.replace('US.','')} エントリー試行 "
                                f"tactic={_ms_tactic} score={self._window_assessment.get('score')}"
                            )
                            self._multi_entry_attempted[_ms_sym] = True
                            self._try_multi_symbol_entry(_ms_sym, _ms_tactic)

                    elif (_market_min_multi >= DYNAMIC_ENTRY_CUTOFF_H * 60 + DYNAMIC_ENTRY_CUTOFF_M
                            and not self._multi_entry_attempted.get(_ms_sym, False)):
                        log.info(f"[Multi] {_ms_sym.replace('US.','')} カットオフ → エントリーなし")
                        self._multi_entry_attempted[_ms_sym] = True

                    # エグジット監視
                    if (_ms_sym in self.multi_positions
                            and not self._multi_exit_done.get(_ms_sym, False)):
                        self._check_multi_symbol_exit(_ms_sym)

            # ── Trade context 死活監視 (15分ごと) ──────────────────────────
            # DRY_TESTモードは発注しないので監視不要
            if not DRY_TEST and FUTU_AVAILABLE:
                self._trade_ctx_check_count += 1
                if self._trade_ctx_check_count >= (900 // _sleep_sec):  # 900秒 = 15分
                    self._trade_ctx_check_count = 0
                    if not self.eng.is_alive():
                        self._trade_ctx_dead_count += 1
                        log.error(
                            f"Trade context 死活確認失敗 "
                            f"({self._trade_ctx_dead_count}回連続)"
                        )
                        if self._trade_ctx_dead_count >= 3:
                            # 3回連続 dead → Bot終了してLaunchAgentに再起動させる
                            log.error(
                                "Trade context 3回連続切断 → Bot終了 (LaunchAgent再起動待ち)"
                            )
                            pushover_alert(
                                "Trade context切断",
                                "Trade context 3回連続切断\n決済不能の可能性あり\nBot終了→LaunchAgent再起動",
                                priority=1,
                            )
                            raise SystemExit("trade_ctx_dead_x3")
                        else:
                            pushover_alert(
                                "Trade context切断",
                                f"Trade context切断 ({self._trade_ctx_dead_count}回連続)\n"
                                f"決済不能の可能性あり 手動確認推奨",
                                priority=1,
                            )
                    else:
                        self._trade_ctx_dead_count = 0  # 復帰確認

            # ── Quote context 死活監視 (15分ごと) ───────────────────────────
            # DRY_TESTモードはfutu不使用なので監視不要。フォールバック中はis_alive()=False。
            if not DRY_TEST and FUTU_AVAILABLE:
                self._quote_ctx_check_count += 1
                if self._quote_ctx_check_count >= (900 // _sleep_sec):  # 900秒 = 15分
                    self._quote_ctx_check_count = 0
                    if not self.mkt.is_alive():
                        self._quote_ctx_dead_count += 1
                        log.error(
                            f"Quote context 死活確認失敗 "
                            f"({self._quote_ctx_dead_count}回連続)"
                        )
                        if self._quote_ctx_dead_count >= 3:
                            log.error(
                                "Quote context 3回連続切断 → データ品質低下 (Bot継続)"
                            )
                            pushover_alert(
                                "Quote context切断",
                                "Quote context 3回連続切断\nデータ品質低下（フォールバック中）\nBot動作は継続",
                                priority=1,
                            )
                            # Bot終了はしない（フォールバックで動作継続）
                            self._quote_ctx_dead_count = 0  # アラート送信後リセットして繰り返し通知しない
                        else:
                            log.warning(
                                f"Quote context切断 ({self._quote_ctx_dead_count}回連続) "
                                f"フォールバックで継続中"
                            )
                    else:
                        self._quote_ctx_dead_count = 0  # 復帰確認

            # ── Research logging (ログのみ・発注なし) ──────────────────────
            # 60秒ごとに実行（_ticks_per_60s カウント）
            self._research_tick = getattr(self, "_research_tick", 0) + 1
            if self._research_tick >= _ticks_per_60s:
                self._research_tick = 0
                self._log_research_data()

            time.sleep(_sleep_sec)

    def _export_monthly_pnl_csv(self):
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            now_et     = datetime.datetime.now(ET)
            prev_month = (now_et.date().replace(day=1) - datetime.timedelta(days=1))
            month_str  = prev_month.strftime("%Y%m")
            usdjpy     = 150.0
            try:
                resp = requests.get(
                    "https://finnhub.io/api/v1/forex/rates",
                    params={"base": "USD", "token": FINNHUB_API_KEY},
                    timeout=5,
                )
                rates = resp.json().get("quote", {})
                jpy = float(rates.get("JPY", 0))
                if jpy > 0:
                    usdjpy = jpy
            except Exception:
                pass
            trades       = load_pnl()
            month_prefix = prev_month.strftime("%Y-%m")
            month_trades = [t for t in trades if t.get("date", "").startswith(month_prefix)]
            if not month_trades:
                return
            csv_path   = REPORTS_DIR / f"condor_{month_str}.csv"
            fieldnames = ["date", "ts", "event", "tactic", "expiry", "direction",
                          "sell_strike", "buy_strike", "qty", "net_credit",
                          "pnl_usd", "pnl_jpy", "reason", "pl_ratio"]
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for t in month_trades:
                    t["pnl_jpy"] = round((t.get("pnl_usd", 0) or 0) * usdjpy, 0)
                    writer.writerow(t)
            pushover("SPY CS", f"月次CSV: {csv_path.name} ({len(month_trades)}件)")
        except Exception as e:
            log.warning(f"monthly csv: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────
# PIDロックはfcntl.flock()でアトミックロックを取得する。
# 旧実装のexists()→read→write はTOCTOU競合があり二重起動を完全には防げなかった。
import fcntl as _fcntl

_pid_lock_fd = None  # ファイルディスクリプタをグローバル保持（GCで閉じないため）

def _acquire_pid_lock(paper: bool) -> Path:
    """同一モードの二重起動を防ぐPIDファイルロック（fcntl.flock版）。
    flock(LOCK_EX|LOCK_NB)でアトミックに排他ロックを取得する。
    既に同モードのプロセスが起動中なら即終了（通知なし）。
    """
    global _pid_lock_fd
    mode = "paper" if paper else "live"
    pid_path = LOG_DIR / f"spy_bot_{mode}.pid"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        fd = open(pid_path, "w")
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _pid_lock_fd = fd  # fdをグローバルに保持してロックを維持
        log.info(f"[PID LOCK] {mode}モード ロック取得 PID={os.getpid()}")
    except (IOError, OSError):
        # ロック取得失敗 = 既に同モードのプロセスが起動中
        log.warning(f"[PID LOCK] 既に{mode}プロセスが起動中 → 終了")
        sys.exit(0)

    return pid_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=STRATEGY_NAME)
    parser.add_argument("--paper",        action="store_true", help="Paper trade mode")
    parser.add_argument("--test-connect", action="store_true", help="Test connection and exit")
    parser.add_argument("--demo-compare", action="store_true",
                        help="Log 7 parameter variants (4 standard + 3 ORF) without placing orders")
    parser.add_argument("--dry-test",     action="store_true",
                        help="futu接続なし・市場時間外でも全ロジックをテスト "
                             "(VIX/SPY価格はYahoo/Finnhubから実データ取得)")
    parser.add_argument("--no-orb",      action="store_true",
                        help="ORB買い戦略を無効化する（CS売りのみで動作）")
    parser.add_argument("--no-calendar", action="store_true",
                        help="カレンダースプレッド戦略を無効化する")
    parser.add_argument("--no-multi",   action="store_true",
                        help="マルチ銘柄同時運用を無効化して従来の1銘柄モードで動作する")
    args = parser.parse_args()

    # --dry-test フラグをグローバルに反映（MarketData/TradeEngineが参照）
    if args.dry_test:
        DRY_TEST = True
        log.info("[DRY-TEST] モード有効: futu接続なし・市場時間バイパス・VirtualPositionManager使用")

    # --no-orb フラグをグローバルに反映
    if args.no_orb:
        ENABLE_ORB = False
        log.info("[ORB] --no-orb: ORB戦略無効")

    # --no-calendar フラグをグローバルに反映
    if args.no_calendar:
        ENABLE_CALENDAR = False
        log.info("[Calendar] --no-calendar: カレンダースプレッド戦略無効")

    # --no-multi フラグをグローバルに反映
    if args.no_multi:
        ENABLE_MULTI_SYMBOL = False  # noqa: F841  (グローバル再バインドは実質不要、__init__で確認)
        log.info("[Multi] --no-multi: マルチ銘柄無効 → 1銘柄モードで動作")

    # --test-connect と --dry-test はロック不要（すぐ終わる / 独立テスト）
    pid_lock_path = None
    if not args.test_connect and not args.dry_test:
        pid_lock_path = _acquire_pid_lock(args.paper)

    try:
        bot = SPYCreditSpreadBot(
            paper=args.paper,
            test_connect=args.test_connect,
            demo_compare=args.demo_compare,
            dry_test=args.dry_test,
            no_multi=getattr(args, "no_multi", False),
        )
        bot.run_forever()
    finally:
        # PIDロックファイルを削除してflockを解放
        if _pid_lock_fd is not None:
            try:
                _fcntl.flock(_pid_lock_fd, _fcntl.LOCK_UN)
                _pid_lock_fd.close()
            except OSError:
                pass
        if pid_lock_path and pid_lock_path.exists():
            try:
                pid_lock_path.unlink()
            except OSError:
                pass
