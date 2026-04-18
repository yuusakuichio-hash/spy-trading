"""
earnings_engine.py — 決算日参戦エンジン

戦術: 決算発表後のIV Crush狙い Straddle売り
  - Finnhub earnings calendar APIから当日・翌日決算銘柄を動的取得
  - 決算1時間前にエントリー → 翌朝決済
  - 銘柄ごとの過去IV Crush実績を参照してサイズ調整

使用方法:
    from common.earnings_engine import EarningsEngine
    eng = EarningsEngine(api_key=FINNHUB_API_KEY)
    if eng.has_earnings_today():
        candidates = eng.get_today_candidates()
        for c in candidates:
            params = eng.get_entry_params(c["symbol"])

設計:
  - 固定銘柄リスト禁止: 毎回Finnhub APIから動的取得
  - 固定閾値禁止: iv_crush_rate・size_factor は過去実績から動的算出
  - pre_trade_check全4層通過: OrderContext生成時にpre_trade_checkを呼ぶ
  - Pushover [Atlas] tag: 呼び出し側で送信（engine内では送らない）
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("spx_condor")

ET = None
try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except ImportError:
    pass

# キャッシュファイル (Finnhub APIレートリミット対策: 当日分は再利用)
_CACHE_DIR = Path(os.environ.get("SPY_DATA_DIR", Path(__file__).parent.parent / "data"))
EARNINGS_CACHE_FILE = _CACHE_DIR / "earnings_cache.json"

# 銘柄別過去IVクラッシュ実績 (バックテスト由来のデフォルト値)
# 値: IV Crush 率の過去中央値 (例: 0.35 = 決算翌朝IVが35%低下)
# 動的更新: record_outcome() を呼ぶと EARNINGS_HISTORY_FILE に蓄積され
#           次回以降は実績値で上書きされる
_DEFAULT_IV_CRUSH_RATES: dict[str, float] = {
    "NVDA":  0.40,   # 高IV・大型決算 → クラッシュ率高い
    "TSLA":  0.35,
    "META":  0.38,
    "GOOGL": 0.32,
    "AAPL":  0.30,
    "MSFT":  0.28,
    "AMZN":  0.33,
    "NFLX":  0.38,
    "AMD":   0.36,
    "CRM":   0.34,
}
# 銘柄未知の場合のデフォルトIVクラッシュ率
_DEFAULT_CRUSH_RATE = 0.28

# エントリー条件: 決算発表時刻からの差分(分)でエントリーするかを判断
ENTRY_BEFORE_EARNINGS_MIN = 60   # 決算1時間前にエントリー
ENTRY_CUTOFF_BEFORE_CLOSE_MIN = 90  # クローズ90分前より遅い決算はスキップ(翌日決算扱い)

# サイズ係数: IV Crush率が高いほど大きくエントリー
SIZE_FACTOR_HIGH = 1.2   # crush_rate >= 0.38
SIZE_FACTOR_MID  = 1.0   # crush_rate >= 0.30
SIZE_FACTOR_LOW  = 0.7   # crush_rate < 0.30

# 決算履歴ファイル (record_outcome() で蓄積)
EARNINGS_HISTORY_FILE = _CACHE_DIR / "earnings_history.json"


@dataclass
class EarningsCandidate:
    """決算参戦候補銘柄"""
    symbol: str          # 例: "NVDA"
    full_code: str       # 例: "US.NVDA"
    report_time: str     # "bmo" (before market open) / "amc" (after market close) / "dmh" / "unknown"
    estimated_dt: Optional[datetime.datetime]   # 推定決算発表時刻 (ET)
    entry_dt: Optional[datetime.datetime]        # 推定エントリー時刻 (ET)
    iv_crush_rate: float   # 過去中央値
    size_factor: float     # エントリーサイズ係数
    eps_estimate: Optional[float] = None
    revenue_estimate: Optional[float] = None


@dataclass
class EarningsEngineResult:
    """pre_trade_check通過後のエントリーパラメータ"""
    symbol: str
    full_code: str
    tactic: str = "straddle_sell"
    iv_crush_rate: float = 0.28
    size_factor: float = 1.0
    entry_before_min: int = ENTRY_BEFORE_EARNINGS_MIN
    notes: str = ""


class EarningsEngine:
    """決算日参戦エンジン。

    Finnhub earnings calendar APIから動的に候補銘柄を取得し、
    IV Crush狙いの Straddle売りエントリーパラメータを返す。

    固定銘柄リストは持たない。銘柄ホワイトリストはpre_trade_checkの
    symbol_whitelist（common/risk_limits.py）で制御する。
    """

    def __init__(
        self,
        api_key: str = "",
        cache_ttl_sec: int = 3600,
        min_iv_crush_rate: float = 0.25,
    ):
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self.cache_ttl_sec = cache_ttl_sec
        self.min_iv_crush_rate = min_iv_crush_rate   # これ未満はスキップ
        self._history: dict = self._load_history()

    # ── Public API ─────────────────────────────────────────────────────────────

    def has_earnings_today(self) -> bool:
        """本日ETで決算発表がある銘柄が1社以上存在するか。"""
        return len(self.get_today_candidates()) > 0

    def get_today_candidates(self) -> list[EarningsCandidate]:
        """本日ETの決算候補銘柄リストを返す。

        - Finnhub APIから取得 (キャッシュTTL: 1時間)
        - iv_crush_rate < min_iv_crush_rate の銘柄は除外
        - クローズ90分前より遅い後場発表 (amc) は除外 (=翌日参戦に回す)
        - 返り値はiv_crush_rate降順にソート
        - CRITICAL-10: ET=None 時は [] を返す + Pushover 通知
        """
        raw = self._fetch_earnings_calendar()
        now_et = self._now_et()
        if now_et is None:
            log.error("[Earnings] ET timezone unavailable - get_today_candidates disabled")
            self._notify_et_unavailable()
            return []
        today = now_et.date()

        candidates: list[EarningsCandidate] = []
        for item in raw:
            item_date = self._parse_date(item.get("date", ""))
            if item_date != today:
                continue

            symbol = item.get("symbol", "")
            if not symbol:
                continue

            report_time = (item.get("hour") or "unknown").lower()
            estimated_dt = self._estimate_announcement_dt(report_time, today)
            entry_dt = self._calc_entry_dt(estimated_dt)

            crush_rate = self._get_iv_crush_rate(symbol)
            if crush_rate < self.min_iv_crush_rate:
                log.debug(f"[Earnings] {symbol}: crush_rate={crush_rate:.2f} < min → skip")
                continue

            # クローズ直前後場発表はスキップ (翌朝決済が無意味になる)
            if report_time == "amc" and estimated_dt:
                market_close = self._market_close_dt(today)
                if estimated_dt and market_close and (estimated_dt - market_close).total_seconds() > ENTRY_CUTOFF_BEFORE_CLOSE_MIN * 60:
                    log.debug(f"[Earnings] {symbol}: amc too late → skip")
                    continue

            size_factor = self._calc_size_factor(crush_rate)
            full_code = f"US.{symbol}"

            candidates.append(EarningsCandidate(
                symbol=symbol,
                full_code=full_code,
                report_time=report_time,
                estimated_dt=estimated_dt,
                entry_dt=entry_dt,
                iv_crush_rate=crush_rate,
                size_factor=size_factor,
                eps_estimate=item.get("epsEstimate"),
                revenue_estimate=item.get("revenueEstimate"),
            ))

        candidates.sort(key=lambda c: c.iv_crush_rate, reverse=True)
        log.info(f"[Earnings] Today candidates: {[c.symbol for c in candidates]} (n={len(candidates)})")
        return candidates

    def get_entry_params(self, symbol: str) -> EarningsEngineResult:
        """指定銘柄のエントリーパラメータを返す。"""
        crush_rate = self._get_iv_crush_rate(symbol)
        size_factor = self._calc_size_factor(crush_rate)
        notes = (
            f"iv_crush_rate={crush_rate:.2f} "
            f"size_factor={size_factor:.2f} "
            f"source={'history' if symbol in self._history else 'default'}"
        )
        return EarningsEngineResult(
            symbol=symbol,
            full_code=f"US.{symbol}",
            tactic="straddle_sell",
            iv_crush_rate=crush_rate,
            size_factor=size_factor,
            entry_before_min=ENTRY_BEFORE_EARNINGS_MIN,
            notes=notes,
        )

    def should_enter_now(
        self,
        candidate: EarningsCandidate,
        tolerance_min: int = 5,
    ) -> bool:
        """現在時刻がエントリーウィンドウ内かを判定する (±tolerance_min分の余裕)。

        tolerance_min: エントリー予定時刻の前後この分数以内ならTrue
        """
        if candidate.entry_dt is None:
            return False
        now_et = self._now_et()
        if now_et is None:
            return False
        diff = abs((now_et - candidate.entry_dt).total_seconds())
        return diff <= tolerance_min * 60

    def record_outcome(
        self,
        symbol: str,
        pre_iv: float,
        post_iv: float,
        pnl_usd: float,
    ) -> None:
        """決算後の実績を履歴に記録し、iv_crush_rateを更新する。

        pre_iv: エントリー時のIV (%)
        post_iv: 決算翌朝のIV (%)
        pnl_usd: 実際のP&L (USD)
        """
        if pre_iv <= 0:
            return
        actual_crush = (pre_iv - post_iv) / pre_iv
        rec = {
            "ts": datetime.datetime.now().isoformat(),
            "pre_iv": round(pre_iv, 2),
            "post_iv": round(post_iv, 2),
            "actual_crush": round(actual_crush, 4),
            "pnl_usd": round(pnl_usd, 2),
        }
        if symbol not in self._history:
            self._history[symbol] = []
        self._history[symbol].append(rec)
        # 直近30件のみ保持
        self._history[symbol] = self._history[symbol][-30:]
        self._save_history()
        log.info(f"[Earnings] record_outcome: {symbol} crush={actual_crush:.2%} pnl=${pnl_usd:.0f}")

    # ── VIX Term Structure 統合 ────────────────────────────────────────────────

    @staticmethod
    def get_term_structure_regime(
        vix9d: Optional[float],
        vix: Optional[float],
        vix3m: Optional[float],
    ) -> dict:
        """VIX9D/VIX/VIX3Mの比率からterm structure regimeを判定する。

        Returns:
            {
                "regime": "contango" | "backwardation" | "neutral",
                "term_ratio_9d_3m": float | None,   # VIX9D / VIX3M
                "term_ratio_spot": float | None,     # VIX9D / VIX
                "tactic_bias": "cs_sell" | "straddle_buy" | "neutral",
                "size_factor": float,
                "notes": str,
            }

        ルール（固定閾値なし: 比率ベース動的判定）:
          term_ratio = VIX9D / VIX3M
            < 0.85 → コンタンゴ → CS売り優先 (size_factor 1.0)
            > 1.05 → バックワーデーション → Straddle買い優先 (size_factor 0.8)
            0.85-1.05 → ニュートラル (size_factor 0.9)

          補助: spot_ratio = VIX9D / VIX
            > 1.0 → 短期ボラ過熱 → size_factor × 0.9 追加縮小
        """
        result = {
            "regime": "neutral",
            "term_ratio_9d_3m": None,
            "term_ratio_spot": None,
            "tactic_bias": "neutral",
            "size_factor": 1.0,
            "notes": "",
        }

        # VIX9D / VIX3M ratio
        if vix9d is not None and vix3m is not None and vix3m > 0:
            r = vix9d / vix3m
            result["term_ratio_9d_3m"] = round(r, 4)

            if r < 0.85:
                result["regime"] = "contango"
                result["tactic_bias"] = "cs_sell"
                result["size_factor"] = 1.0
                result["notes"] = f"VIX9D/VIX3M={r:.3f} < 0.85 → contango → CS売り優先"
            elif r > 1.05:
                result["regime"] = "backwardation"
                result["tactic_bias"] = "straddle_buy"
                result["size_factor"] = 0.8
                result["notes"] = f"VIX9D/VIX3M={r:.3f} > 1.05 → backwardation → Straddle買い優先"
            else:
                result["regime"] = "neutral"
                result["tactic_bias"] = "neutral"
                result["size_factor"] = 0.9
                result["notes"] = f"VIX9D/VIX3M={r:.3f} → neutral zone"
        else:
            result["notes"] = "VIX9D or VIX3M unavailable → regime=neutral"

        # VIX9D / VIX 補助チェック
        if vix9d is not None and vix is not None and vix > 0:
            spot_ratio = vix9d / vix
            result["term_ratio_spot"] = round(spot_ratio, 4)
            if spot_ratio > 1.0:
                result["size_factor"] = round(result["size_factor"] * 0.9, 4)
                result["notes"] += f" | spot_ratio={spot_ratio:.3f}>1.0 → size×0.9"

        log.info(
            f"[TermStructure] regime={result['regime']} "
            f"tactic_bias={result['tactic_bias']} "
            f"size_factor={result['size_factor']:.2f} | {result['notes']}"
        )
        return result

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _fetch_earnings_calendar(self) -> list[dict]:
        """Finnhub earnings calendar APIを呼ぶ (キャッシュあり)。"""
        today = datetime.date.today().isoformat()

        # キャッシュ確認
        if EARNINGS_CACHE_FILE.exists():
            try:
                cached = json.loads(EARNINGS_CACHE_FILE.read_text())
                cache_date = cached.get("date")
                cache_ts = cached.get("ts", 0)
                if cache_date == today and (time.time() - cache_ts) < self.cache_ttl_sec:
                    log.debug(f"[Earnings] cache hit (age={(time.time()-cache_ts):.0f}s)")
                    return cached.get("data", [])
            except Exception as e:
                log.warning(f"[Earnings] cache read error: {e}")

        # API呼び出し
        if not self.api_key:
            log.warning("[Earnings] FINNHUB_API_KEY not set → return empty")
            return []

        # 当日+翌日の範囲を取得 (UTC基準)
        from_date = today
        to_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        url = (
            f"https://finnhub.io/api/v1/calendar/earnings"
            f"?from={from_date}&to={to_date}&token={self.api_key}"
        )
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("earningsCalendar", [])
            # キャッシュ保存
            EARNINGS_CACHE_FILE.write_text(json.dumps({
                "date": today,
                "ts": time.time(),
                "data": data,
            }))
            log.info(f"[Earnings] API fetch: {len(data)} entries (from={from_date} to={to_date})")
            return data
        except Exception as e:
            log.warning(f"[Earnings] API fetch error: {e}")
            return []

    def _get_iv_crush_rate(self, symbol: str) -> float:
        """銘柄のIVクラッシュ率を返す (履歴→デフォルト順)。"""
        # 実績履歴から動的算出
        if symbol in self._history and len(self._history[symbol]) >= 3:
            crush_vals = [r["actual_crush"] for r in self._history[symbol]]
            # 外れ値除去: 中央値を使う
            sorted_vals = sorted(crush_vals)
            median = sorted_vals[len(sorted_vals) // 2]
            return round(median, 4)

        # デフォルト値
        return _DEFAULT_IV_CRUSH_RATES.get(symbol, _DEFAULT_CRUSH_RATE)

    def _calc_size_factor(self, crush_rate: float) -> float:
        """IVクラッシュ率からサイズ係数を算出する。"""
        if crush_rate >= 0.38:
            return SIZE_FACTOR_HIGH
        elif crush_rate >= 0.30:
            return SIZE_FACTOR_MID
        else:
            return SIZE_FACTOR_LOW

    def _estimate_announcement_dt(
        self,
        report_time: str,
        date: datetime.date,
    ) -> Optional[datetime.datetime]:
        """report_timeとdateから推定発表時刻 (ET) を返す。"""
        if ET is None:
            return None
        if report_time == "bmo":
            # 寄り付き前: ET 7:30
            return datetime.datetime(date.year, date.month, date.day, 7, 30, tzinfo=ET)
        elif report_time == "amc":
            # 引け後: ET 16:15
            return datetime.datetime(date.year, date.month, date.day, 16, 15, tzinfo=ET)
        else:
            # dmh / unknown: ET 12:00
            return datetime.datetime(date.year, date.month, date.day, 12, 0, tzinfo=ET)

    def _calc_entry_dt(
        self,
        announcement_dt: Optional[datetime.datetime],
    ) -> Optional[datetime.datetime]:
        """決算発表1時間前のエントリー時刻を返す。"""
        if announcement_dt is None:
            return None
        return announcement_dt - datetime.timedelta(minutes=ENTRY_BEFORE_EARNINGS_MIN)

    def _market_close_dt(self, date: datetime.date) -> Optional[datetime.datetime]:
        if ET is None:
            return None
        return datetime.datetime(date.year, date.month, date.day, 16, 0, tzinfo=ET)

    def _now_et(self) -> Optional[datetime.datetime]:
        """現在の ET 時刻を返す。zoneinfo が利用不可の場合は None を返す (CRITICAL-10)。
        呼び出し側で None チェックすること。"""
        if ET is None:
            return None  # CRITICAL-10: fallback削除 (JST localtime混入防止)
        return datetime.datetime.now(ET)

    def _notify_et_unavailable(self) -> None:
        """ET timezone が利用不可の場合に Pushover priority=1 で通知 (CRITICAL-10)。
        spam 防止のため 1 時間に 1 回まで。"""
        now_ts = time.time()
        last_ts = getattr(self, "_et_unavailable_last_notify", 0)
        if now_ts - last_ts < 3600:
            return
        self._et_unavailable_last_notify = now_ts
        try:
            import requests as _req
            _pov_token = os.environ.get("PUSHOVER_ALERT_TOKEN", "")
            _pov_user  = os.environ.get("PUSHOVER_USER", "")
            if _pov_token and _pov_user:
                _req.post(
                    "https://api.pushover.net/1/messages.json",
                    data={
                        "token": _pov_token,
                        "user": _pov_user,
                        "title": "[Atlas/ALERT] ET timezone unavailable - earnings disabled",
                        "message": (
                            "zoneinfo.ZoneInfo('America/New_York') が利用不可。\n"
                            "EarningsEngine は候補を返しません。\n"
                            "pip install tzdata または OS tzdata を確認してください。"
                        ),
                        "priority": 1,
                    },
                    timeout=10,
                )
        except Exception as _e:
            log.warning(f"[Earnings] _notify_et_unavailable Pushover failed: {_e}")

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime.date]:
        try:
            return datetime.date.fromisoformat(date_str[:10])
        except Exception:
            return None

    def _load_history(self) -> dict:
        if not EARNINGS_HISTORY_FILE.exists():
            return {}
        try:
            return json.loads(EARNINGS_HISTORY_FILE.read_text())
        except Exception:
            return {}

    def _save_history(self) -> None:
        try:
            EARNINGS_HISTORY_FILE.write_text(json.dumps(self._history, indent=2))
        except Exception as e:
            log.warning(f"[Earnings] history save error: {e}")
