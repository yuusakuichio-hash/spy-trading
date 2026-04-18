"""common/pdt_1dte_utils.py — PDT 1DTE判定・戦術別PDTチェック・フォールバックロジック

PDTルールの核心:
  - Day trade = 同一営業日のopen + close（0DTE戦術は原則day trade）
  - 1DTE = Day1 open + Day2 close = overnight保有 = PDT対象外
  - CalendarSpreadはそもそも複数日満期 = PDT対象外

Layer 3.5 (pre_trade_check組込用):
  - is_0dte_strategy() で戦術がPDT対象かを判定
  - 0DTE戦術かつPDT残0なら拒否、1DTE以上ならスキップ

strategy_selector フォールバック:
  - PDT残0 + capital < $25K + 0DTE戦術選択 → 同ロジックの1DTE版に自動切替
  - 1DTE未対応戦術 → no_trade
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

log = logging.getLogger(__name__)

try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    import pytz  # type: ignore
    ET = pytz.timezone("America/New_York")  # type: ignore

# ── 戦術別1DTE対応状況 ────────────────────────────────────────────────────────

# 各エンジンの supports_1dte 属性の仕様と一致させる
# True = 1DTE版に切替可能 / False = 0DTE特化・1DTE不可
STRATEGY_SUPPORTS_1DTE: dict[str, bool] = {
    "CS":            True,   # Credit Spread (CSSellEngine)
    "cs":            True,
    "credit_spread": True,
    "ORB":           True,   # ORB Breakout (ORBEngine)
    "orb":           True,
    "orb_breakout":  True,
    "IC":            True,   # Iron Condor (IronCondorSellEngine)
    "ic":            True,
    "iron_condor":   True,
    "strangle":      True,   # Strangle Sell (StrangleSellEngine)
    "StrangleSell":  True,
    "Butterfly":     True,   # Butterfly (ButterflyEngine)
    "butterfly":     True,
    "Calendar":      True,   # Calendar (CalendarEngine) — そもそも複数日満期
    "calendar":      True,
    # 0DTE特化 — IV crush狙いで翌日保有は意味なし
    "StraddleBuy":   False,
    "straddle_buy":  False,
    "straddle":      False,
    "GammaScalp":    False,
    "gamma_scalp":   False,
    "IVCrush":       False,
    "iv_crush":      False,
}

# ── PDT残0でも取引可能な戦術 (satisfies_no_pdt=True) ───────────────────────────
# PDT残0の状態でも選択可能な戦術：
#   1. 満期放置前提（OTM消滅でPDT不消費）の売り戦術
#   2. 1DTE以上（日跨ぎ = PDT対象外）
#   3. SPX等の現金決済（assigned でも PDT 対象外）
# 設計根拠:
#   - CS売り・IC売り（OTM運用想定）: 満期放置 allow_expiry_pass_through=True
#   - 1DTE系全般: overnight保有でday trade非該当
#   - Straddle買い等のvalue-recovery狙い: closeが必要 → False
STRATEGY_SATISFIES_NO_PDT: dict[str, bool] = {
    # 売り戦術（満期放置OK・OTM消滅でPDT不消費）
    "CS":            True,
    "cs":            True,
    "credit_spread": True,
    "IC":            True,
    "ic":            True,
    "iron_condor":   True,
    "strangle":      True,
    "StrangleSell":  True,
    # 1DTE版（日跨ぎ = PDT対象外）
    "1dte_cs":       True,
    "1dte_ic":       True,
    "1dte_orb":      True,
    "1dte_strangle": True,
    "1dte_butterfly":True,
    "1dte_calendar": True,
    # Butterfly売り（OTM運用なら満期放置可）
    "Butterfly":     True,
    "butterfly":     True,
    # Calendar: そもそも複数日満期でPDT非該当
    "Calendar":      True,
    "calendar":      True,
    # 0DTE特化 — 満期放置が損失になるケース → False（強制closeが必要）
    "ORB":           False,  # 買い戦術: OTM消滅=全損 → 時間内にclose必要
    "orb":           False,
    "orb_breakout":  False,
    "StraddleBuy":   False,  # 買い戦術: 価値取り戻し狙いでcloseすべき
    "straddle_buy":  False,
    "straddle":      False,
    "GammaScalp":    False,  # デルタヘッジ系: 同日close前提
    "gamma_scalp":   False,
    "IVCrush":       False,  # IVcrush監視で同日close前提
    "iv_crush":      False,
}


def strategy_satisfies_no_pdt(strategy_name: str) -> bool:
    """PDT残0の状態でもこの戦術が選択可能かどうかを返す。

    True の条件:
      - 満期放置OK（売り戦術でOTM消滅 → expired_worthless → PDT非計上）
      - 1DTE以上（overnight保有 → PDT対象外）
      - 現金決済（SPX等 → assigned でも PDT 対象外）

    False の条件:
      - 買い戦術で満期放置=全損になるもの（ORB・StraddleBuy等）
      - 同日close前提の戦術（GammaScalp・IVCrush等）

    Args:
        strategy_name: 戦術識別子

    Returns:
        True: PDT残0でも選択可能 / False: PDT残が必要
    """
    if strategy_name in STRATEGY_SATISFIES_NO_PDT:
        return STRATEGY_SATISFIES_NO_PDT[strategy_name]

    # 1DTE系プレフィックスはすべてOK
    if strategy_name.startswith("1dte_"):
        return True

    # 未知の戦術: 安全側（PDT残が必要）とみなす
    log.warning(f"[PDT1DTE] 未知の戦術: {strategy_name} → satisfies_no_pdt=False")
    return False


# 0DTE戦術名のプレフィックス/パターン
_0DTE_PREFIXES = ("0dte_", "0DTE_")


def is_0dte_strategy(
    strategy_name: str,
    expiry_date: Optional[datetime.date],
    now_et: Optional[datetime.datetime] = None,
) -> bool:
    """この戦術・満期がPDT対象の0DTE day tradeかどうかを返す。

    判定ロジック:
      1. 戦術名が "0dte_" プレフィックスを持つ → 0DTE
      2. expiry_date が today (ET) と一致 → 0DTE（戦術名に関係なく）
      3. それ以外 → 1DTE以上 = PDT対象外

    Args:
        strategy_name: 戦術識別子（例: "CS", "ORB", "0dte_cs"）
        expiry_date:   オプション満期日（Noneの場合は戦術名のみで判定）
        now_et:        現在のET時刻（Noneなら自動取得）

    Returns:
        True: 0DTE day trade（PDT対象） / False: 1DTE以上（PDT対象外）
    """
    # 戦術名で判定
    if any(strategy_name.startswith(p) for p in _0DTE_PREFIXES):
        return True

    # 満期日で判定
    if expiry_date is not None:
        if now_et is None:
            now_et = datetime.datetime.now(ET)
        today_et = now_et.date()
        if expiry_date == today_et:
            return True

    return False


def strategy_supports_1dte(strategy_name: str) -> bool:
    """この戦術が1DTE版に切替可能かどうかを返す。

    Args:
        strategy_name: 戦術識別子

    Returns:
        True: 1DTE版あり / False: 0DTE特化で1DTE不可
    """
    # 完全一致
    if strategy_name in STRATEGY_SUPPORTS_1DTE:
        return STRATEGY_SUPPORTS_1DTE[strategy_name]

    # プレフィックスマッチ（"0dte_cs" → "cs" を検索）
    for prefix in _0DTE_PREFIXES:
        if strategy_name.startswith(prefix):
            base = strategy_name[len(prefix):]
            if base in STRATEGY_SUPPORTS_1DTE:
                return STRATEGY_SUPPORTS_1DTE[base]

    # 未知の戦術は安全側（1DTE不可）とみなす
    log.warning(f"[PDT1DTE] 未知の戦術名: {strategy_name} → 1DTE不可として処理")
    return False


def get_1dte_fallback_name(strategy_name: str) -> Optional[str]:
    """0DTE戦術名から対応する1DTE版の戦術名を返す。

    Args:
        strategy_name: 0DTE戦術名

    Returns:
        1DTE版戦術名 / None（1DTE版なし）
    """
    if not strategy_supports_1dte(strategy_name):
        return None

    # "0dte_xxx" → "1dte_xxx"
    for prefix in _0DTE_PREFIXES:
        if strategy_name.startswith(prefix):
            return "1dte_" + strategy_name[len(prefix):]

    # プレフィックスなし戦術（"CS", "ORB" 等）は "1dte_" を付与
    return f"1dte_{strategy_name.lower()}"


# ── フォールバックカウンタ（当日分・メモリ管理） ────────────────────────────────

_fallback_count_date: Optional[datetime.date] = None
_fallback_count: int = 0


def increment_fallback_count(now_et: Optional[datetime.datetime] = None) -> int:
    """0DTE→1DTEフォールバック発動回数を記録し、当日の累計を返す。"""
    global _fallback_count_date, _fallback_count
    if now_et is None:
        now_et = datetime.datetime.now(ET)
    today = now_et.date()
    if _fallback_count_date != today:
        _fallback_count_date = today
        _fallback_count = 0
    _fallback_count += 1
    log.info(f"[PDT1DTE] フォールバック発動: 本日{_fallback_count}回目")
    return _fallback_count


def get_fallback_count(now_et: Optional[datetime.datetime] = None) -> int:
    """当日のフォールバック発動回数を返す。"""
    if now_et is None:
        now_et = datetime.datetime.now(ET)
    today = now_et.date()
    if _fallback_count_date != today:
        return 0
    return _fallback_count


# ── PDT + 1DTE 統合チェック（pre_trade_check Layer 3.5 に組み込む用） ───────────

def check_pdt_layer(
    strategy_name: str,
    expiry_date: Optional[datetime.date],
    capital_usd: float,
    pdt_tracker,  # PDTTracker instance（型ヒントは循環インポート回避のため省略）
    now_et: Optional[datetime.datetime] = None,
) -> tuple[bool, str, bool]:
    """Layer 3.5: PDT戦術別チェック。

    Args:
        strategy_name: 戦術名
        expiry_date:   オプション満期日
        capital_usd:   現在の口座残高（USD）
        pdt_tracker:   PDTTrackerインスタンス
        now_et:        現在のET時刻

    Returns:
        (allow: bool, reason: str, is_day_trade: bool)
        - allow=True: エントリー可
        - allow=False: PDTブロック
        - is_day_trade: 0DTE day tradeかどうか（AARレポート用）
    """
    if now_et is None:
        now_et = datetime.datetime.now(ET)

    is_0dte = is_0dte_strategy(strategy_name, expiry_date, now_et)

    if not is_0dte:
        # 1DTE以上 = PDTチェックスキップ
        log.debug(f"[L3.5] {strategy_name}: 1DTE以上 → PDTチェックスキップ")
        return True, "1DTE+ — PDT対象外", False

    # 0DTE = day trade → PDTチェック実行
    can_enter = pdt_tracker.can_enter_new_day_trade(capital_usd)
    remaining = pdt_tracker.remaining_allowed(capital_usd)

    if not can_enter:
        reason = (
            f"[L3.5] 0DTE day trade PDTブロック: "
            f"残{remaining}本 capital=${capital_usd:.0f} strategy={strategy_name}"
        )
        log.warning(reason)
        return False, reason, True

    # 残1本警告
    if remaining == 1 and capital_usd < 25_000.0:
        _notify_pdt_warning(capital_usd, remaining)

    return True, f"0DTE PDTチェック通過 (残{remaining}本)", True


def _notify_pdt_warning(capital_usd: float, remaining: int) -> None:
    """PDT残1警告 Pushover 通知（priority=1）。"""
    try:
        from common.pushover_utils import pushover_alert  # type: ignore
        pushover_alert(
            f"[Atlas/PDT] PDT残{remaining}本・次は1DTEフォールバック",
            priority=1,
            title="PDT警告",
        )
    except Exception:
        try:
            # spy_bot.pyのpushover_alertにフォールバック
            import sys
            for mod_name, mod in sys.modules.items():
                if hasattr(mod, "pushover_alert") and "spy_bot" in mod_name:
                    mod.pushover_alert(
                        f"[Atlas/PDT] PDT残{remaining}本・次は1DTEフォールバック (capital=${capital_usd:.0f})",
                    )
                    break
        except Exception as e:
            log.warning(f"[PDT1DTE] Pushover通知失敗: {e}")


def notify_fallback_activated(strategy_name: str, fallback_name: str) -> None:
    """0DTE→1DTEフォールバック発動 Pushover 通知（priority=0）。"""
    try:
        from common.pushover_utils import pushover_alert  # type: ignore
        pushover_alert(
            f"[Atlas/PDT] 0DTE→1DTE自動切替: {strategy_name} → {fallback_name}",
            priority=0,
            title="PDTフォールバック",
        )
    except Exception:
        try:
            import sys
            for mod_name, mod in sys.modules.items():
                if hasattr(mod, "pushover") and "spy_bot" in mod_name:
                    mod.pushover(
                        f"[Atlas/PDT] 0DTE→1DTE自動切替: {strategy_name} → {fallback_name}",
                    )
                    break
        except Exception as e:
            log.warning(f"[PDT1DTE] フォールバック通知失敗: {e}")
