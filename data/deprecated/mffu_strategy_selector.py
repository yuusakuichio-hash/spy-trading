#!/usr/bin/env python3
"""
mffu_strategy_selector.py — MFFU先物向け 戦術選択エンジン

設計根拠 (mffu_multi_strategy_design.md B):
  Atlas strategy_selector.py の設計思想を先物向けに踏襲。
  VIX帯 × 時間帯 × セッション × MFFUルール状態の4次元マトリクスで戦術を動的選択。

Atlas流用:
  - compute_dynamic_vix_thresholds() — VIX帯動的算出 (strategy_selector.py)
  - compute_vix_percentile()         — VIXパーセンタイル (strategy_selector.py)
  固定閾値は最小限（パニックラインのみ）。

新規実装:
  - select_futures_strategy()       — MFFU先物版 メイン選択関数
  - check_consistency_safety()      — Consistency Rule 35%予防ブロック
  - _is_orb_window()               — 時間帯判定ヘルパー
  - _is_overnight_entry_window()   — 翌日持ち越しエントリー時間帯

セッション統合 (futures_session_strategy.py):
  select_futures_strategy() の env dict に 'session' キーを追加することで
  Asia / London / US Open / US Midday / US Close の5セッション別戦術を適用する。
  'session' が指定されない場合は従来の時間帯判定のみで動作（後方互換性維持）。

戦術選択マトリクス（設計書 B-2 + セッション拡張）:
  VIX >= elevated (≈20) かつ ORB窓 → ORB（フル枠）
  VIX z > 1.5 かつ 15:45 ET → VIX-MR Long（翌日持ち越し）
  VIX < 18 かつ 15:45 ET → Trend Follow (SMA20/50)
  VIX [15-20] かつ gap|0.3-2.0%| → Gap Fill（将来実装）
  Asia セッション → Range trade / VWAP reversion
  London セッション → Trend Following / London breakout
  US Midday → Range trade / Mean reversion（控えめサイズ）
  US Close → EOD reversal / VWAP return
"""

from __future__ import annotations

import logging
import datetime
import zoneinfo
from typing import Optional

log = logging.getLogger(__name__)

ET = zoneinfo.ZoneInfo("America/New_York")

# ── Time-of-Day Bias（後方互換: ImportError 時は無効化）──────────────────────
try:
    from futures_time_of_day_bias import (
        calc_tod_bias,
        apply_tod_bias_to_size_pct,
        get_tod_slot_info,
    )
    _TOD_BIAS_AVAILABLE = True
    log.info("[MFFUStrategySelector] futures_time_of_day_bias: loaded")
except ImportError:
    _TOD_BIAS_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_time_of_day_bias not available: TOD bias disabled")

# ── Asia Range Fade（後方互換: ImportError 時は無効化）───────────────────────
try:
    from futures_asia_range_fade import AsiaRangeFadeStrategy, is_asia_session
    _ASIA_RANGE_AVAILABLE = True
    _asia_range_strategy  = AsiaRangeFadeStrategy()
    log.info("[MFFUStrategySelector] futures_asia_range_fade: loaded")
except ImportError:
    _ASIA_RANGE_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_asia_range_fade not available")

# ── Gap Fill Advanced（後方互換: ImportError 時は無効化）─────────────────────
try:
    from futures_gap_fill_advanced import GapFillAdvancedStrategy, check_gap_fill_entry
    _GAP_FILL_ADVANCED_AVAILABLE = True
    log.info("[MFFUStrategySelector] futures_gap_fill_advanced: loaded")
except ImportError:
    _GAP_FILL_ADVANCED_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_gap_fill_advanced not available")

# ── P2戦術: Volume Profile（後方互換: ImportError 時は無効化）──────────────────
try:
    from futures_volume_profile import (
        calc_volume_profile,
        VolumeProfileStrategy,
    )
    _VOLUME_PROFILE_AVAILABLE = True
    log.info("[MFFUStrategySelector] futures_volume_profile: loaded")
except ImportError:
    _VOLUME_PROFILE_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_volume_profile not available")

# ── P2戦術: Economic Event Reaction（後方互換: ImportError 時は無効化）─────────
try:
    from futures_economic_event import EconomicEventStrategy
    _ECONOMIC_EVENT_AVAILABLE = True
    _econ_event_strategy = EconomicEventStrategy()
    log.info("[MFFUStrategySelector] futures_economic_event: loaded")
except ImportError:
    _ECONOMIC_EVENT_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_economic_event not available")

# ── P2戦術: Range Break Improved（後方互換: ImportError 時は無効化）─────────────
try:
    from futures_range_break_improved import (
        RangeBreakImprovedStrategy,
        calc_donchian_channel,
        calc_dynamic_donchian_period,
    )
    _RANGE_BREAK_IMPROVED_AVAILABLE = True
    log.info("[MFFUStrategySelector] futures_range_break_improved: loaded")
except ImportError:
    _RANGE_BREAK_IMPROVED_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_range_break_improved not available")

# ── セッション戦術エンジン（後方互換: ImportError 時は無効化）─────────────────
try:
    from futures_session_strategy import (
        get_current_session,
        select_mffu_strategies as _session_select,
        SessionBasedStrategy,
    )
    _SESSION_AVAILABLE = True
    _sess_engine = SessionBasedStrategy()
except ImportError:
    _SESSION_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_session_strategy not available: session axis disabled")

# ── P1戦術: VIX Term Structure（後方互換: ImportError 時は無効化）────────────
try:
    from futures_vix_term_structure import VIXTermStructureStrategy
    _VIX_TERM_STRUCTURE_AVAILABLE = True
    _vix_term_structure_strategy  = VIXTermStructureStrategy()
    log.info("[MFFUStrategySelector] futures_vix_term_structure: loaded")
except ImportError:
    _VIX_TERM_STRUCTURE_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_vix_term_structure not available")

# ── P1戦術: ES-NQ Spread（後方互換: ImportError 時は無効化）─────────────────
try:
    from futures_es_nq_spread import ESNQSpreadStrategy
    _ES_NQ_SPREAD_AVAILABLE = True
    _es_nq_spread_strategy  = ESNQSpreadStrategy()
    log.info("[MFFUStrategySelector] futures_es_nq_spread: loaded")
except ImportError:
    _ES_NQ_SPREAD_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_es_nq_spread not available")

# ── P1戦術: Level Trading（後方互換: ImportError 時は無効化）────────────────
try:
    from futures_level_trading import LevelTradingStrategy
    _LEVEL_TRADING_AVAILABLE = True
    log.info("[MFFUStrategySelector] futures_level_trading: loaded")
except ImportError:
    _LEVEL_TRADING_AVAILABLE = False
    log.warning("[MFFUStrategySelector] futures_level_trading not available")

# ── Atlas strategy_selector から流用 ─────────────────────────────────────────
try:
    from strategy_selector import (
        compute_dynamic_vix_thresholds,
        compute_vix_percentile,
    )
    _ATLAS_AVAILABLE = True
except ImportError:
    _ATLAS_AVAILABLE = False
    log.warning("[MFFUStrategySelector] strategy_selector not available: using fallbacks")

# ── JudgementLogic 統合（後方互換: ImportError 時は無効化）──────────────────────
try:
    from judgement_logic import JudgementLogic, SentimentContext
    _JUDGEMENT_AVAILABLE = True
    log.info("[MFFUStrategySelector] judgement_logic: loaded")
except ImportError:
    _JUDGEMENT_AVAILABLE = False
    log.warning("[MFFUStrategySelector] judgement_logic not available: sentiment bias disabled")


def _fallback_compute_dynamic_vix_thresholds(vix_history: list[float]) -> dict:
    """strategy_selector が import できない場合のフォールバック。"""
    return {"calm": 16.0, "elevated": 22.0, "panic": 30.0}


def _fallback_compute_vix_percentile(current_vix: float, vix_history: list[float]) -> float:
    return 50.0


# ── 公開 API（インポート先から直接呼べるようラップ） ──────────────────────────
if _ATLAS_AVAILABLE:
    _compute_thresholds = compute_dynamic_vix_thresholds
    _compute_percentile = compute_vix_percentile
else:
    _compute_thresholds = _fallback_compute_dynamic_vix_thresholds
    _compute_percentile = _fallback_compute_vix_percentile


# ── ORB エントリーウィンドウ定義 ──────────────────────────────────────────────
ORB_WINDOW_START = "09:35"
ORB_WINDOW_END   = "11:00"

# VIX-MR / TF エントリー決定時刻（翌日持ち越し判断）
OVERNIGHT_ENTRY_WINDOW_START = "15:40"
OVERNIGHT_ENTRY_WINDOW_END   = "15:55"

# Consistency Rule 予防ブロック閾値（40%ルールに対し5%のバッファ）
CONSISTENCY_SAFETY_PCT = 0.35

# Daily loss フロア（EOD DD $2,000 の 3%相当）
# $50K 口座: $50,000 × 0.03 = $1,500
DAILY_LOSS_FLOOR_PCT = 0.03


def _is_orb_window(time_et: str) -> bool:
    """現在時刻がORBエントリーウィンドウ内かチェックする。"""
    return ORB_WINDOW_START <= time_et <= ORB_WINDOW_END


def _is_overnight_entry_window(time_et: str) -> bool:
    """現在時刻が翌日持ち越しエントリー決定ウィンドウ内かチェックする。"""
    return OVERNIGHT_ENTRY_WINDOW_START <= time_et <= OVERNIGHT_ENTRY_WINDOW_END


def check_consistency_safety(
    today_pnl:   float,
    monthly_pnl: float,
    threshold:   float = CONSISTENCY_SAFETY_PCT,
) -> bool:
    """
    今日の利益が月間利益の threshold (35%) を超えそうか確認する。

    MFFU Consistency Rule: 1日の利益 <= 総利益の40%
    予防的に35%でブロックして40%違反を防ぐ。

    Args:
        today_pnl:   今日の確定利益（マイナス = 損失の日）
        monthly_pnl: 今月の累積利益（今日分を含む）
        threshold:   ブロック閾値（デフォルト35%）

    Returns:
        True  = エントリー安全
        False = エントリー停止推奨（Consistency違反リスク）
    """
    if monthly_pnl <= 0:
        return True  # 月間まだ赤字 → 制限なし

    if today_pnl <= 0:
        return True  # 今日は損失の日 → 制限なし

    ratio = today_pnl / monthly_pnl
    if ratio >= threshold:
        log.warning(
            f"[MFFUStrategySelector] Consistency safety block: "
            f"today={today_pnl:.0f} / monthly={monthly_pnl:.0f} "
            f"= {ratio:.1%} >= threshold={threshold:.0%}"
        )
        return False

    return True


def select_futures_strategy(env: dict) -> list[dict]:
    """
    環境dictから今稼働すべき先物戦術リストを返す。

    Args:
        env = {
            'vix':                float,       # 現在VIX
            'vix_history':        list[float], # 過去60日VIX終値
            'vix_z':              float,       # 20日Zスコア（futures_vix_mr.calc_vix_z_scoreで算出）
            'time_et':            'HH:MM',     # 現在時刻（ET）
            'gap_pct':            float,       # 始値ギャップ率 (%)
            'account_pnl_day':    float,       # 今日の確定P&L ($)
            'account_pnl_month':  float,       # 今月の累積P&L ($)
            'account_balance':    float,       # 現在の口座残高 ($)
            'consistency_used_pct': float,     # 今月最大日利益/月間利益 (0-100)
            'sma20_vs_sma50':     str | None,  # "above" | "below" | None
                                               # (SMA20がSMA50の上ならabove)
        }

    Returns:
        list of {
            'strategy':    str,   # 戦術名
            'size_pct':    float, # サイズ倍率 (0.0 - 1.0)
            'confidence':  float, # 信頼度 (0.0 - 1.0)
            'reason':      str,   # 選択理由
        }

        戦術名（12戦術 + no_trade）:
          'orb'                  — Opening Range Breakout
          'vix_mr_long'          — VIX Mean Reversion Long（翌日持ち越し）
          'trend_follow'         — Trend Following SMA20/50
          'asia_range_fade'      — Asia Range Fade
          'gap_fill_advanced'    — Gap Fill Advanced
          'session_based'        — Session Based Strategy
          'vix_term_structure'   — VIX Term Structure MR
          'es_nq_spread'         — ES-NQ Spread Pair Trade
          'level_trading'        — Level Trading
          'volume_profile_long'  — Volume Profile VAL反発Long
          'volume_profile_short' — Volume Profile VAH拒絶Short
          'econ_event_long'      — Economic Event ドリフトLong
          'econ_event_short'     — Economic Event ドリフトShort
          'range_break_long'     — Range Break 改良版 Long
          'range_break_short'    — Range Break 改良版 Short
          'no_trade'             — ノートレード
    """
    strategies = []

    vix         = env.get("vix", 20.0)
    vix_history = env.get("vix_history", [])
    vix_z       = env.get("vix_z", 0.0)
    time_et     = env.get("time_et", "00:00")
    gap_pct     = env.get("gap_pct", 0.0)
    pnl_day     = env.get("account_pnl_day", 0.0)
    pnl_month   = env.get("account_pnl_month", 0.0)
    balance     = env.get("account_balance", 50_000.0)
    cons_used   = env.get("consistency_used_pct", 0.0)
    sma_state   = env.get("sma20_vs_sma50", None)

    # ── ステージ1: MFFUルール絶対チェック ──────────────────────────────────────

    # Daily Loss フロアチェック（EOD DD $2,000 の手前で停止）
    daily_loss_floor = -DAILY_LOSS_FLOOR_PCT * balance
    if pnl_day <= daily_loss_floor:
        return [{
            "strategy":   "no_trade",
            "size_pct":   0.0,
            "confidence": 1.0,
            "reason":     (
                f"daily_loss_floor: pnl_day={pnl_day:.0f} <= "
                f"floor={daily_loss_floor:.0f}"
            ),
        }]

    # Consistency Rule 残存チェック（35%予防ブロック）
    if cons_used >= CONSISTENCY_SAFETY_PCT * 100:
        return [{
            "strategy":   "no_trade",
            "size_pct":   0.0,
            "confidence": 1.0,
            "reason":     (
                f"consistency_safety: used={cons_used:.1f}% >= "
                f"threshold={CONSISTENCY_SAFETY_PCT*100:.0f}%"
            ),
        }]

    # check_consistency_safety による追加確認
    if not check_consistency_safety(pnl_day, pnl_month):
        return [{
            "strategy":   "no_trade",
            "size_pct":   0.0,
            "confidence": 1.0,
            "reason":     "consistency_safety_check_failed",
        }]

    # ── ステージ2: VIX帯動的算出 ────────────────────────────────────────────────

    thresholds = _compute_thresholds(vix_history)
    vix_pctl   = _compute_percentile(vix, vix_history)

    calm_th     = thresholds["calm"]     # ≈ P30 (~16)
    elevated_th = thresholds["elevated"] # ≈ P70 (~22)
    panic_th    = thresholds["panic"]    # ≈ P95 (~30)

    if vix < calm_th:
        vix_band = "low"    # 低ボラ（ORB不適・TF/VMR監視）
    elif vix < elevated_th:
        vix_band = "mid"    # 中ボラ（ORB準備枠・GF可）
    elif vix < panic_th:
        vix_band = "high"   # 高ボラ（ORBベスト・VMR有効）
    else:
        vix_band = "panic"  # パニック（ORB2/3枠・VMR慎重）

    log.info(
        f"[MFFUStrategySelector] VIX={vix:.1f} band={vix_band} "
        f"pctl={vix_pctl:.0f} "
        f"thresholds: calm={calm_th} elevated={elevated_th} panic={panic_th}"
    )

    # ── ステージ2.5a: Time-of-Day Bias 算出 ──────────────────────────────────────
    # 現在ETをdatetimeに変換（time_et は 'HH:MM' 文字列）
    tod_bias_map: dict[str, float] = {}
    if _TOD_BIAS_AVAILABLE:
        try:
            now_et_dt = datetime.datetime.now(ET)
            # time_et 文字列で時刻を上書き（テスト時に任意時刻を渡せるように）
            if time_et and len(time_et) == 5:
                h, m = int(time_et[:2]), int(time_et[3:])
                now_et_dt = now_et_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            for sname in ("orb", "gap_fill", "asia_range_fade", "vix_mr_long", "trend_follow", "level_trading"):
                tod_bias_map[sname] = calc_tod_bias(
                    current_et_time = now_et_dt,
                    strategy_name   = sname,
                    vix_band        = vix_band,
                )
            slot_info = get_tod_slot_info(now_et_dt)
            log.info(
                f"[MFFUStrategySelector] TOD bias: slot={slot_info['slot']} "
                f"time={time_et} vix_band={vix_band}"
            )
        except Exception as e:
            log.warning(f"[MFFUStrategySelector] TOD bias calc error: {e}")

    def _apply_tod(strategy_name: str, size_pct: float) -> float:
        """size_pct に TOD bias を適用する内部ヘルパー。"""
        if not tod_bias_map:
            return size_pct
        bias = tod_bias_map.get(strategy_name, tod_bias_map.get("generic", 1.0))
        return apply_tod_bias_to_size_pct(size_pct, bias) if _TOD_BIAS_AVAILABLE else size_pct

    # ── ステージ2.5b: Asia Range Fade（Asia session中は専用戦術を返す）────────
    # Asia session (18:00-03:00 ET): Asia Range Fade を追加
    if _ASIA_RANGE_AVAILABLE:
        try:
            now_et_dt = datetime.datetime.now(ET)
            if time_et and len(time_et) == 5:
                h, m = int(time_et[:2]), int(time_et[3:])
                now_et_dt = now_et_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if is_asia_session(now_et_dt):
                asia_size = _apply_tod("asia_range_fade", 0.5)
                if asia_size > 0.0:
                    strategies.append({
                        "strategy":   "asia_range_fade",
                        "size_pct":   asia_size,
                        "confidence": 0.55,
                        "reason":     (
                            f"Asia session: Asia Range Fade "
                            f"size={asia_size:.2f} VIX={vix:.1f}({vix_band})"
                        ),
                    })
                    log.info(
                        f"[MFFUStrategySelector] Asia Range Fade added: "
                        f"size={asia_size:.2f}"
                    )
        except Exception as e:
            log.warning(f"[MFFUStrategySelector] Asia Range Fade eval error: {e}")

    # ── ステージ2.5c: Gap Fill Advanced（ORBウィンドウ内のgap条件で追加）────
    # gap_fill（改良版）は gap条件が成立しているときに追加候補として挿入する
    # 実際のエントリーフィルターは GapFillAdvancedStrategy.evaluate() が担う
    if _GAP_FILL_ADVANCED_AVAILABLE and vix_band in ("low", "mid"):
        if 0.3 <= abs(gap_pct) <= 2.0:
            gf_size = _apply_tod("gap_fill", 0.4)
            if gf_size > 0.0:
                strategies.append({
                    "strategy":   "gap_fill_advanced",
                    "size_pct":   gf_size,
                    "confidence": 0.60,
                    "reason":     (
                        f"Gap Fill Advanced candidate: gap={gap_pct:.2f}% "
                        f"vix={vix:.1f}({vix_band}) size={gf_size:.2f}"
                    ),
                })
                log.info(
                    f"[MFFUStrategySelector] Gap Fill Advanced added: "
                    f"gap={gap_pct:.2f}% size={gf_size:.2f}"
                )

    # ── ステージ2.5: セッション軸統合（Asia/London/US Midday/US Close）──────────
    # env dict に 'session' キーがある場合はセッション別戦術を先に追加する。
    # US Open 時間帯のセッション戦術（orb 等）はステージ3で処理するため
    # ここでは us_open 以外のセッションを対象にする。

    session_from_env = env.get("session", None)
    if _SESSION_AVAILABLE and session_from_env is None:
        # session 未指定の場合は time_et から自動判定
        session_from_env = get_current_session(time_et)

    if _SESSION_AVAILABLE and session_from_env not in (None, "us_open"):
        # Asia / London / US Midday / US Close のセッション戦術を追加
        sess_results = _session_select(
            vix          = vix,
            vix_z        = vix_z,
            session      = session_from_env,
            gap_pct      = gap_pct,
            sma_state    = sma_state,
            pnl_day      = pnl_day,
            pnl_month    = pnl_month,
            account_size = balance,
        )
        for sr in sess_results:
            if sr["strategy"] != "no_trade":
                # セッション戦術は既存の戦術と重複しないように追加
                strategies.append({
                    "strategy":   sr["strategy"],
                    "size_pct":   sr["size_pct"],
                    "confidence": sr["confidence"],
                    "reason":     f"[session={session_from_env}] {sr['reason']}",
                })
        log.info(
            f"[MFFUStrategySelector] session={session_from_env} "
            f"added {len(sess_results)} session strategies"
        )

    # ── ステージ2.7: センチメントバイアス適用 ────────────────────────────────────
    # env dict に 'sentiment_score' と 'strategy_bias' があれば
    # 各戦術の size_pct に動的乗数を適用する。
    # 乗数は固定値ではなく sentiment_score の分布から動的に算出する。

    sentiment_score = env.get("sentiment_score", None)
    strategy_bias   = env.get("strategy_bias",   None)

    def _sentiment_size_mult(sentiment: Optional[float], bias: Optional[str]) -> float:
        """
        センチメントスコアとバイアスから size 乗数を算出する。

        乗数は 0.5 〜 1.3 の範囲に制限する。
        50 (中立) → 1.0（変化なし）
        extreme_fear  → mean_reversion 戦術は 1.2 倍、trend 戦術は 0.7 倍
        extreme_greed → mean_reversion 戦術は 1.2 倍、trend 戦術は 0.7 倍
        """
        if sentiment is None:
            return 1.0
        # 中立 (50) からの乖離度: 0.0 〜 1.0
        deviation = abs(sentiment - 50.0) / 50.0
        return max(0.5, min(1.3, 1.0 + deviation * 0.3))

    _sent_mult = _sentiment_size_mult(sentiment_score, strategy_bias)
    if sentiment_score is not None:
        log.info(
            f"[MFFUStrategySelector] sentiment_score={sentiment_score:.1f} "
            f"bias={strategy_bias} size_mult={_sent_mult:.2f}"
        )

    # ── ステージ3: 時間帯 × VIX帯 戦術選択（US Open 時間帯） ────────────────────

    is_orb_window       = _is_orb_window(time_et)
    is_overnight_window = _is_overnight_entry_window(time_et)

    # ── ORB（09:35-11:00 ET）───────────────────────────────────────────────────
    if is_orb_window:
        if vix_band == "panic":
            # panic帯: 2/3サイズで稼働（大波乱対応）
            base_size = 0.67
            tod_size  = _apply_tod("orb", base_size)
            strategies.append({
                "strategy":   "orb",
                "size_pct":   tod_size,
                "confidence": 0.80,
                "reason":     (
                    f"VIX={vix:.1f}({vix_band}) ORB panic帯 "
                    f"size={tod_size:.2f} (TOD adjusted from {base_size})"
                ),
            })
        elif vix_band == "high":
            # high帯: フルサイズ（11年BT最優秀帯）
            base_size = 1.0
            tod_size  = _apply_tod("orb", base_size)
            strategies.append({
                "strategy":   "orb",
                "size_pct":   tod_size,
                "confidence": 0.85,
                "reason":     (
                    f"VIX={vix:.1f}({vix_band}) ORB最優秀帯 "
                    f"size={tod_size:.2f} (TOD adjusted)"
                ),
            })
        elif vix_band == "mid":
            # mid帯: 50%サイズ（期待値低いが取引機会確保）
            base_size = 0.5
            tod_size  = _apply_tod("orb", base_size)
            strategies.append({
                "strategy":   "orb",
                "size_pct":   tod_size,
                "confidence": 0.55,
                "reason":     (
                    f"VIX={vix:.1f}({vix_band}) ORB準備枠 "
                    f"size={tod_size:.2f} (TOD adjusted from {base_size})"
                ),
            })
        # vix_band == "low" → ORB不採用（11年BTで月利-22%）

    # ── 翌日持ち越し戦術（15:40-15:55 ET）─────────────────────────────────────
    if is_overnight_window:

        # VIX-MR Long（Zスコア > 1.5）
        if vix_z > 1.5:
            # panicゾーンでは0.5サイズ（過剰リスク回避）
            base_vmr = 1.0 if vix_band in ("high",) else (0.5 if vix_band == "panic" else 0.7)
            tod_vmr  = _apply_tod("vix_mr_long", base_vmr)
            strategies.append({
                "strategy":   "vix_mr_long",
                "size_pct":   tod_vmr,
                "confidence": 0.70,
                "reason":     (
                    f"VIX-Z={vix_z:.2f} > 1.5 "
                    f"恐怖スパイク→MR long "
                    f"VIX={vix:.1f}({vix_band}) "
                    f"size={tod_vmr:.2f} (TOD adjusted from {base_vmr:.2f})"
                ),
            })

        # Trend Follow（VIX < 18 かつ SMAクロス状態あり）
        if vix < 18.0 and sma_state is not None:
            tod_tf = _apply_tod("trend_follow", 0.3)
            strategies.append({
                "strategy":   "trend_follow",
                "size_pct":   tod_tf,
                "confidence": 0.55,
                "reason":     (
                    f"VIX={vix:.1f}<18 TF低ボラ環境 "
                    f"sma_state={sma_state} "
                    f"size={tod_tf:.2f} (TOD adjusted)"
                ),
            })

    # ── ステージ4: P2戦術（Volume Profile / Economic Event / Range Break 改良版）──
    # P2戦術は既存戦術に追加する（排他ではない）。
    # 稼働時間帯: US市場オープン全体（09:30-16:00 ET）

    _US_MARKET_OPEN  = "09:30"
    _US_MARKET_CLOSE = "16:00"
    _in_us_session   = _US_MARKET_OPEN <= time_et <= _US_MARKET_CLOSE

    if _in_us_session:
        # ── Economic Event Reaction ──────────────────────────────────────────
        # is_blackout / check_entry は EconomicEventStrategy インスタンスが保持する
        # カレンダーを使う。カレンダー未ロードの場合は no_trade とならず neutral になる
        # だけなので安全。
        if _ECONOMIC_EVENT_AVAILABLE:
            try:
                import datetime as _dt
                import zoneinfo as _zi
                _ET = _zi.ZoneInfo("America/New_York")
                now_et_full = _dt.datetime.now(_ET)

                # ブラックアウトチェック（カレンダーがロードされている場合のみ）
                if _econ_event_strategy._calendar:
                    is_bo, bo_reason = _econ_event_strategy.is_blackout_period(now_et_full)
                    if is_bo:
                        # 既存戦術もブロック（ORB等も含めてblackout）
                        log.info(f"[MFFUStrategySelector] econ_event blackout: {bo_reason}")
                        return [{
                            "strategy":   "no_trade",
                            "size_pct":   0.0,
                            "confidence": 1.0,
                            "reason":     f"[econ_blackout] {bo_reason}",
                        }]

                    econ_result = _econ_event_strategy.check_entry(
                        now_et        = now_et_full,
                        current_price = env.get("current_price", 0.0),
                    )
                    if econ_result["signal"] in ("long", "short"):
                        strategies.append({
                            "strategy":   f"econ_event_{econ_result['signal']}",
                            "size_pct":   min(econ_result["size_mult"], 1.0),
                            "confidence": 0.65,
                            "reason":     econ_result["reason"],
                        })
            except Exception as _e:
                log.warning(f"[MFFUStrategySelector] econ_event error: {_e}")

        # ── Volume Profile ───────────────────────────────────────────────────
        # vp_profile は env から渡す（mffu_bot.pyで事前算出して env に含める）。
        # 未提供の場合はスキップ。
        if _VOLUME_PROFILE_AVAILABLE:
            vp_profile = env.get("vp_profile", None)
            if vp_profile and env.get("current_price"):
                try:
                    _vp_strat = VolumeProfileStrategy(
                        vix = vix,
                        atr = env.get("atr"),
                    )
                    vp_result = _vp_strat.check_entry(
                        current_price = env["current_price"],
                        profile       = vp_profile,
                        atr           = env.get("atr"),
                    )
                    if vp_result["signal"] in ("long", "short"):
                        strategies.append({
                            "strategy":   f"volume_profile_{vp_result['signal']}",
                            "size_pct":   min(vp_result["size_mult"], 1.0),
                            "confidence": 0.60,
                            "reason":     vp_result["reason"],
                        })
                except Exception as _e:
                    log.warning(f"[MFFUStrategySelector] volume_profile error: {_e}")

        # ── Range Break 改良版 ───────────────────────────────────────────────
        # donchian_channel は env から渡す（mffu_bot.pyで事前算出）。
        # 未提供の場合はスキップ。
        if _RANGE_BREAK_IMPROVED_AVAILABLE:
            rb_channel = env.get("donchian_channel", None)
            if rb_channel and env.get("current_price"):
                try:
                    _rb_strat = RangeBreakImprovedStrategy(
                        vix = vix,
                        atr = env.get("atr"),
                    )
                    rb_result = _rb_strat.check_entry(
                        current_price        = env["current_price"],
                        channel              = rb_channel,
                        current_volume       = env.get("current_volume", 0.0),
                        avg_volume           = env.get("avg_volume", 0.0),
                        recent_closes        = env.get("recent_closes"),
                        last_break_level     = env.get("last_break_level"),
                        last_break_direction = env.get("last_break_direction"),
                    )
                    if rb_result["signal"] in ("long", "short"):
                        strategies.append({
                            "strategy":   f"range_break_{rb_result['signal']}",
                            "size_pct":   min(rb_result["size_mult"], 1.0),
                            "confidence": 0.60,
                            "reason":     rb_result["reason"],
                        })
                except Exception as _e:
                    log.warning(f"[MFFUStrategySelector] range_break_improved error: {_e}")

    # ── ステージ5: P1統合戦術（VIX Term Structure / ES-NQ Spread / Level Trading）──
    # これらの戦術はデータが揃っている場合のみ check_entry を試みる。
    # データ未提供の場合は active=False でログを出力（dry_run確認用）。

    # ── VIX Term Structure ──────────────────────────────────────────────────
    _vts_active = False
    _vts_signal = None
    if _VIX_TERM_STRUCTURE_AVAILABLE:
        vix3m = env.get("vix3m")
        if vix3m is not None:
            try:
                _vts_signal = _vix_term_structure_strategy.check_entry(
                    vix             = vix,
                    vix3m           = vix3m,
                    vix6m           = env.get("vix6m"),
                    account_balance = balance,
                )
                _vts_active = True
                if _vts_signal is not None:
                    strategies.append({
                        "strategy":   "vix_term_structure",
                        "size_pct":   _vts_signal["size_pct"],
                        "confidence": _vts_signal["confidence"],
                        "reason":     _vts_signal["reason"],
                    })
            except Exception as _e:
                log.warning(f"[MFFUStrategySelector] vix_term_structure error: {_e}")
        log.info(
            f"[StrategyCheck] vix_term_structure: active={_vts_active} "
            f"signal={'yes' if _vts_signal else 'no'} "
            f"{'(vix3m missing)' if vix3m is None else ''}"
        )
    else:
        log.info("[StrategyCheck] vix_term_structure: active=False (module unavailable)")

    # ── ES-NQ Spread ─────────────────────────────────────────────────────────
    _spread_active = False
    _spread_signal = None
    if _ES_NQ_SPREAD_AVAILABLE:
        # selector 内では既存ポジションを持たない独立インスタンスで check_entry を呼ぶ。
        # 実際の発注・ポジション管理は mffu_bot.py の self.es_nq_spread が担う。
        es_price = env.get("es_price")
        nq_price = env.get("nq_price")
        ratio_history = env.get("es_nq_ratio_history", [])
        if es_price is not None and nq_price is not None and ratio_history:
            try:
                _es_nq_spread_strategy._ratio_history = list(ratio_history)
                _spread_signal = _es_nq_spread_strategy.check_entry(
                    es=es_price,
                    nq=nq_price,
                )
                _spread_active = True
                if _spread_signal is not None:
                    strategies.append({
                        "strategy":   "es_nq_spread",
                        "size_pct":   _spread_signal["size_pct"],
                        "confidence": _spread_signal["confidence"],
                        "reason":     _spread_signal["reason"],
                    })
            except Exception as _e:
                log.warning(f"[MFFUStrategySelector] es_nq_spread error: {_e}")
        log.info(
            f"[StrategyCheck] es_nq_spread: active={_spread_active} "
            f"signal={'yes' if _spread_signal else 'no'} "
            f"{'(es_price/nq_price/ratio_history missing)' if not _spread_active else ''}"
        )
    else:
        log.info("[StrategyCheck] es_nq_spread: active=False (module unavailable)")

    # ── Level Trading ────────────────────────────────────────────────────────
    # Level Trading は IB確定後（10:30 ET以降・US市場オープン）に評価する。
    # levels が env に入っている場合のみ check_entry を試みる。
    _level_active = False
    _level_signal = None
    if _LEVEL_TRADING_AVAILABLE and _in_us_session:
        lv_levels   = env.get("level_trading_levels")
        lv_price    = env.get("current_price")
        lv_volume   = env.get("current_volume", 0)
        lv_ib_done  = env.get("ib_finalized", False)
        if lv_levels and lv_price and lv_ib_done:
            try:
                # selector では proper __init__ で一時インスタンスを作成して check_entry を呼ぶ。
                # mffu_bot.py の self.level_trading とは独立したインスタンス（発注なし）。
                _lv_tmp = LevelTradingStrategy(vix=vix)
                _lv_tmp.levels    = lv_levels
                _lv_tmp.ib_high   = env.get("ib_high")
                _lv_tmp.ib_low    = env.get("ib_low")
                _lv_tmp.ib_range  = (
                    (_lv_tmp.ib_high - _lv_tmp.ib_low)
                    if _lv_tmp.ib_high and _lv_tmp.ib_low else None
                )
                _lv_tmp.vwap       = env.get("vwap")
                _lv_tmp._entry_done = False
                _level_signal = _lv_tmp.check_entry(
                    price      = lv_price,
                    volume     = lv_volume,
                    kelly_frac = 0.5,
                )
                _level_active = True
                if _level_signal is not None:
                    strategies.append({
                        "strategy":   "level_trading",
                        "size_pct":   min(_level_signal["confidence"], 1.0),
                        "confidence": _level_signal["confidence"],
                        "reason":     _level_signal.get("reason", "level_touch"),
                    })
            except Exception as _e:
                log.warning(f"[MFFUStrategySelector] level_trading error: {_e}")
        log.info(
            f"[StrategyCheck] level_trading: active={_level_active} "
            f"signal={'yes' if _level_signal else 'no'} "
            f"{'(levels/price/ib missing)' if not _level_active else ''}"
        )
    else:
        log.info(
            f"[StrategyCheck] level_trading: active=False "
            f"{'(module unavailable)' if not _LEVEL_TRADING_AVAILABLE else '(outside us_session)'}"
        )

    # ── 全12戦術の[StrategyCheck]サマリーログ ──────────────────────────────────
    # 各戦術が呼ばれたことをログで確認できるよう、全戦術名を出力する。
    # active = selectorがcheck_entryを呼んだかどうか（シグナル有無ではない）
    _strat_names_active = {s["strategy"] for s in strategies if s["strategy"] != "no_trade"}
    for _sn in [
        "orb", "trend_follow", "vix_mr_long", "asia_range_fade", "gap_fill_advanced",
        "session_based", "vix_term_structure", "es_nq_spread", "level_trading",
        "volume_profile", "economic_event", "range_break_improved",
    ]:
        _in_result = any(s["strategy"].startswith(_sn.split("_long")[0].split("_short")[0]) for s in strategies)
        log.debug(f"[StrategyCheck] {_sn}: in_result={_in_result}")

    # ── フォールバック ──────────────────────────────────────────────────────────
    if not strategies:
        strategies.append({
            "strategy":   "no_trade",
            "size_pct":   0.0,
            "confidence": 1.0,
            "reason":     (
                f"no_matching_regime: VIX={vix:.1f}({vix_band}) "
                f"time={time_et}"
            ),
        })

    log.info(
        f"[MFFUStrategySelector] selected {len(strategies)} strategies: "
        f"{[s['strategy'] for s in strategies]}"
    )

    # B-1修正: env_score を env dict に書き戻す
    # VIXパーセンタイルが低いほど有利（env_score高）
    # vix_pctl=0  → env_score=100 (完全低ボラ)
    # vix_pctl=50 → env_score=50  (中立)
    # vix_pctl=95 → env_score=5   (パニック)
    # no_trade のみの場合はスコアを下限として扱う
    if strategies and strategies[0]["strategy"] == "no_trade" and len(strategies) == 1:
        computed_env_score = max(5.0, 100.0 - vix_pctl)
        computed_env_score = min(computed_env_score, 20.0)  # ルール停止帯は最大20
    else:
        computed_env_score = max(5.0, 100.0 - vix_pctl)
    env["env_score"] = computed_env_score

    log.info(
        f"[MFFUStrategySelector] env_score={computed_env_score:.1f} "
        f"(vix_pctl={vix_pctl:.0f})"
    )

    return strategies


def build_env_dict(
    vix:               float,
    vix_history:       list[float],
    vix_z:             float,
    time_et:           str,
    account_pnl_day:   float    = 0.0,
    account_pnl_month: float    = 0.0,
    account_balance:   float    = 50_000.0,
    consistency_used:  float    = 0.0,
    gap_pct:           float    = 0.0,
    sma20_vs_sma50:    Optional[str] = None,
    session:           Optional[str] = None,
    atr_5d:            float    = 0.0,
    sentiment_score:   Optional[float] = None,
    strategy_bias:     Optional[str]   = None,
) -> dict:
    """
    select_futures_strategy() に渡す env dict を構築するヘルパー。

    mffu_bot.py の run_forever() ループから呼ぶ用。

    Args:
        session: セッション名 ("asia"|"london"|"us_open"|"us_midday"|"us_close")
                 None の場合は time_et から自動判定される。
    """
    d = {
        "vix":                  vix,
        "vix_history":          vix_history,
        "vix_z":                vix_z,
        "time_et":              time_et,
        "gap_pct":              gap_pct,
        "account_pnl_day":      account_pnl_day,
        "account_pnl_month":    account_pnl_month,
        "account_balance":      account_balance,
        "consistency_used_pct": consistency_used,
        "sma20_vs_sma50":       sma20_vs_sma50,
        "atr_5d":               atr_5d,
    }
    if session is not None:
        d["session"] = session
    if sentiment_score is not None:
        d["sentiment_score"] = sentiment_score
    if strategy_bias is not None:
        d["strategy_bias"] = strategy_bias
    return d
