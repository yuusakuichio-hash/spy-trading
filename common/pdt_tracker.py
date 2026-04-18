"""common/pdt_tracker.py — 全戦術合算 PDT（Pattern Day Trader）カウンタ

FINRA PDTルール（厳密定義）:
  - 証拠金口座で $25,000 未満の場合、5営業日ローリングで4回以上の
    当日往復取引（同日 open + close）を行うと90日間デイトレ禁止
  - $25,000 以上は PDT 対象外（無制限）

PDT対象・対象外の明確な区分:
  - day_trade = purchase AND sale（自分で両方の注文を出した場合）のみ
  - 満期によるOTM消滅 (expired_worthless) → sale 行為ではない → PDT対象外
  - ITM自動行使 (assigned) → 原資産受渡しは sale ではない → PDT対象外（翌日以降売却は別track）
  - SPX等の現金決済 (cash_settled) → ヨーロピアン現金決済は sale 行為ではない → PDT対象外

exit_type 値:
  - "manual_close": 手動決済注文（SL/TP/Kill Switch/タイムストップ等）→ PDT対象
  - "expired_worthless": 満期OTM消滅 → PDT対象外
  - "assigned": ITM自動行使 → PDT対象外
  - "cash_settled": SPX等の現金決済 → PDT対象外

設計:
  - append-only JSONL: data/pdt_day_trades.jsonl
  - fcntl.flock で書込 atomic（複数プロセス競合対応）
  - 再起動時はファイルから復元（メモリ依存なし）
  - ETタイムゾーン厳密化（JST/UTC で計算しない）
  - 5営業日ローリング: 土日をスキップして直近5営業日を計算

使い方:
    from common.pdt_tracker import PDTTracker
    tracker = PDTTracker()
    # 手動決済（PDT対象）
    tracker.record_round_trip("US.SPY", entry_time, exit_time, "CS",
                              exit_type="manual_close")
    # 満期放置（PDT対象外）
    tracker.record_round_trip("US.SPY", entry_time, exit_time, "CS",
                              exit_type="expired_worthless")
    remaining = tracker.remaining_allowed(capital_usd=8000.0)
    if tracker.can_enter_new_day_trade(capital_usd=8000.0):
        # エントリー可
"""
from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ETタイムゾーン（厳密化: UTCやJSTで計算しない）
try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except Exception:
    import pytz  # type: ignore
    ET = pytz.timezone("America/New_York")  # type: ignore

PDT_LIMIT = 3          # $25K未満: 5営業日で3回まで（4回目で違反）
PDT_THRESHOLD_USD = 25_000.0  # この金額以上は PDT 対象外

# PDT対象外 exit_type セット（これらは day_trade として計上しない）
_NON_PDT_EXIT_TYPES: frozenset = frozenset({
    "expired_worthless",  # 満期OTM消滅: sale行為ではない
    "assigned",           # ITM自動行使: 原資産受渡し（翌日以降売却は別track）
    "cash_settled",       # SPX等のヨーロピアン現金決済: sale行為ではない
})

# デフォルトデータファイル
_DEFAULT_DATA_FILE = Path(
    os.environ.get("SPY_DATA_DIR", Path(__file__).parents[1] / "data")
) / "pdt_day_trades.jsonl"

# 満期放置の戦術統計ファイル（PDT対象外取引の記録）
_DEFAULT_NON_PDT_FILE = Path(
    os.environ.get("SPY_DATA_DIR", Path(__file__).parents[1] / "data")
) / "pdt_non_day_trades.jsonl"


def _is_business_day(d: datetime.date) -> bool:
    """土日を除く営業日判定（祝日は未考慮・0DTE戦略は市場カレンダーで別制御）。"""
    return d.weekday() < 5  # 0=月 ... 4=金


def _last_n_business_days(n: int, reference: Optional[datetime.date] = None) -> list[datetime.date]:
    """referenceから遡って直近n営業日のリストを返す（referenceを含む）。

    Args:
        n:         取得する営業日数
        reference: 基準日（Noneなら今日のET日付）

    Returns:
        降順ではなく昇順（古い順）のdateリスト
    """
    if reference is None:
        reference = datetime.datetime.now(ET).date()
    result: list[datetime.date] = []
    d = reference
    while len(result) < n:
        if _is_business_day(d):
            result.append(d)
        d -= datetime.timedelta(days=1)
    result.reverse()
    return result


class PDTTracker:
    """全戦術合算 PDT カウンタ。

    Args:
        data_file: JSONL 永続化ファイルパス（省略時は data/pdt_day_trades.jsonl）
    """

    def __init__(self, data_file: Optional[Path] = None) -> None:
        self.data_file = Path(data_file) if data_file else _DEFAULT_DATA_FILE
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        # PDT対象外取引の記録ファイル（satisfied_no_pdt確認用）
        self._non_pdt_file = self.data_file.parent / (
            self.data_file.stem.replace("pdt_day_trades", "pdt_non_day_trades")
            + self.data_file.suffix
        )
        self._non_pdt_file.parent.mkdir(parents=True, exist_ok=True)

    # ── 書込 ──────────────────────────────────────────────────────────────────

    def record_round_trip(
        self,
        symbol: str,
        entry_time: datetime.datetime,
        exit_time: datetime.datetime,
        strategy: str,
        exit_type: str = "manual_close",
    ) -> bool:
        """open + close の往復取引を記録する。

        FINRAの厳密なPDT定義に準拠:
        - day_trade = purchase AND sale（自分で両方の注文を出した場合）
        - 満期放置・自動行使・現金決済は sale 行為ではないため PDT 対象外

        Args:
            symbol:     銘柄コード（例: "US.SPY"）
            entry_time: エントリー時刻（timezone-aware または naive → ETとして解釈）
            exit_time:  クローズ時刻（同上）
            strategy:   戦術名（例: "CS", "IC", "ORB", "DeltaHedge"）
            exit_type:  決済種別（デフォルト: "manual_close"）
                - "manual_close": 手動決済注文 → PDT対象（同日なら day_trade）
                - "expired_worthless": 満期OTM消滅 → PDT対象外
                - "assigned": ITM自動行使 → PDT対象外
                - "cash_settled": SPX等の現金決済 → PDT対象外

        Returns:
            True: day_trade として計上した
            False: 日跨ぎ・PDT対象外 exit_type のため計上しなかった
        """
        # naive datetime は ET として解釈
        entry_et = _to_et(entry_time)
        exit_et  = _to_et(exit_time)

        entry_date = entry_et.date()
        exit_date  = exit_et.date()

        # PDT対象外 exit_type → day_trade 計上しない（非PDT記録ファイルに保存）
        if exit_type in _NON_PDT_EXIT_TYPES:
            log.info(
                f"[PDT] {strategy} {symbol}: exit_type={exit_type} → PDT対象外（計上なし）"
            )
            non_pdt_record = {
                "date":       entry_date.isoformat(),
                "symbol":     symbol,
                "strategy":   strategy,
                "exit_type":  exit_type,
                "entry_time": entry_et.isoformat(),
                "exit_time":  exit_et.isoformat(),
                "is_business_day": _is_business_day(entry_date),
                "pdt_exempt_reason": exit_type,
            }
            self._append_non_pdt(non_pdt_record)
            return False

        # manual_close の場合: 同日かどうかチェック
        if entry_date != exit_date:
            log.debug(
                f"[PDT] {strategy} {symbol}: 日跨ぎのため計上スキップ "
                f"(entry={entry_date} exit={exit_date})"
            )
            return False

        record = {
            "date":       entry_date.isoformat(),
            "symbol":     symbol,
            "strategy":   strategy,
            "exit_type":  exit_type,
            "entry_time": entry_et.isoformat(),
            "exit_time":  exit_et.isoformat(),
            "is_business_day": _is_business_day(entry_date),
        }

        self._append(record)
        log.info(
            f"[PDT] day_trade 計上: {strategy} {symbol} {entry_date} "
            f"exit_type={exit_type} "
            f"(rolling5={self.count_day_trades_rolling()}件)"
        )
        return True

    # ── 読込・集計 ─────────────────────────────────────────────────────────────

    def _load_records(self) -> list[dict]:
        """JSONL から全レコードを読み込む。読み込みエラーは warn してスキップ。"""
        if not self.data_file.exists():
            return []
        records: list[dict] = []
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        log.warning(f"[PDT] JSONL parse error (skipped): {e}")
        except Exception as e:
            log.warning(f"[PDT] ファイル読み込みエラー: {e}")
        return records

    def count_day_trades_rolling(
        self,
        days: int = 5,
        reference: Optional[datetime.date] = None,
    ) -> int:
        """過去 days 営業日（ローリング）の day_trade 件数を返す。

        FINRAの「5営業日ローリング」に準拠。
        土日は営業日としてカウントしない。

        Args:
            days:      ローリング営業日数（デフォルト5）
            reference: 基準日（Noneなら今日のET日付）

        Returns:
            day_trade 件数
        """
        biz_days = set(_last_n_business_days(days, reference))
        records = self._load_records()
        count = sum(
            1 for r in records
            if _parse_date(r.get("date", "")) in biz_days
        )
        return count

    def remaining_allowed(self, capital_usd: float) -> int | float:
        """残りエントリー可能なday_trade本数を返す。

        Args:
            capital_usd: 現在の口座残高（USD）

        Returns:
            $25K以上なら float('inf')（無制限）
            $25K未満なら max(0, 3 - count)
        """
        if capital_usd >= PDT_THRESHOLD_USD:
            return float("inf")
        used = self.count_day_trades_rolling()
        return max(0, PDT_LIMIT - used)

    def can_enter_new_day_trade(self, capital_usd: float) -> bool:
        """新規 day_trade エントリーが可能かどうかを返す。

        Args:
            capital_usd: 現在の口座残高（USD）

        Returns:
            True: エントリー可 / False: PDT上限到達でブロック
        """
        rem = self.remaining_allowed(capital_usd)
        if rem == float("inf"):
            return True
        allowed = rem > 0
        if not allowed:
            log.warning(
                f"[PDT] BLOCKED: 5営業日ローリング {self.count_day_trades_rolling()}/{PDT_LIMIT}件 "
                f"capital=${capital_usd:.0f} < ${PDT_THRESHOLD_USD:.0f}"
            )
        return allowed

    def count_non_pdt_by_exit_type(
        self,
        exit_type: Optional[str] = None,
        reference: Optional[datetime.date] = None,
    ) -> int:
        """PDT対象外取引件数を集計する（Daily AAR第7章用）。

        Args:
            exit_type: フィルタする exit_type（Noneなら全PDT対象外）
            reference: 基準日（Noneなら今日のET日付）

        Returns:
            該当件数
        """
        if reference is None:
            reference = datetime.datetime.now(ET).date()
        if not self._non_pdt_file.exists():
            return 0
        try:
            count = 0
            with open(self._non_pdt_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        rec_date = _parse_date(rec.get("date", ""))
                        if rec_date != reference:
                            continue
                        if exit_type is None or rec.get("exit_type") == exit_type:
                            count += 1
                    except json.JSONDecodeError:
                        pass
            return count
        except Exception as e:
            log.warning(f"[PDT] non_pdt ファイル読み込みエラー: {e}")
            return 0

    def get_daily_pdt_summary(
        self,
        reference: Optional[datetime.date] = None,
    ) -> dict:
        """Daily AAR第7章「PDT状況」追加項目を返す。

        Returns:
            {
                "expired_worthless_count": int,   # 満期放置件数（OTM消滅でPDT回避）
                "assigned_count": int,             # ITM早期close件数（PDT消費してITM回避）
                "cash_settled_count": int,         # 現金決済件数（SPX等）
                "manual_close_count": int,         # 手動決済件数（PDT計上対象）
                "total_non_pdt_exits": int,        # PDT対象外合計
            }
        """
        if reference is None:
            reference = datetime.datetime.now(ET).date()
        expired_worthless = self.count_non_pdt_by_exit_type("expired_worthless", reference)
        assigned = self.count_non_pdt_by_exit_type("assigned", reference)
        cash_settled = self.count_non_pdt_by_exit_type("cash_settled", reference)
        manual_close = self.count_day_trades_rolling(days=1, reference=reference)
        return {
            "expired_worthless_count": expired_worthless,
            "assigned_count":          assigned,
            "cash_settled_count":      cash_settled,
            "manual_close_count":      manual_close,
            "total_non_pdt_exits":     expired_worthless + assigned + cash_settled,
        }

    def get_status(self, capital_usd: float) -> dict:
        """PDT状況のサマリーdictを返す（atlas_state.json統合用）。

        Returns:
            {
                "capital_usd": float,
                "pdt_constrained": bool,        # $25K未満かどうか
                "rolling5_count": int,          # 直近5営業日の消費数
                "pdt_limit": int,               # 上限（$25K未満の場合のみ意味あり）
                "pdt_remaining": int | str,     # 残数（$25K以上なら "unlimited"）
                "can_enter": bool,
                "business_days_window": list[str],
                "today_pdt_summary": dict,      # Daily AAR第7章用サマリー
                "today_0dte_count": int,        # 当日0DTE day_trade数（AAR第7章）
                "today_1dte_count": int,        # 当日1DTE（PDT対象外）数（AAR第7章）
                "today_fallback_count": int,    # 当日0DTE→1DTEフォールバック発動回数（AAR第7章）
            }
        """
        constrained = capital_usd < PDT_THRESHOLD_USD
        biz_days = _last_n_business_days(5)
        count = self.count_day_trades_rolling()
        rem = self.remaining_allowed(capital_usd)

        # 当日0DTE数（= 当日のday_trade計上件数）
        today_0dte = self.count_day_trades_rolling(days=1)

        # 当日1DTE数（PDT対象外として別途記録された件数）
        today_1dte = self.count_non_pdt_by_exit_type()

        # フォールバック発動回数（pdt_1dte_utils のメモリカウンタから取得）
        today_fallback = 0
        try:
            from common.pdt_1dte_utils import get_fallback_count
            today_fallback = get_fallback_count()
        except Exception:
            pass

        return {
            "capital_usd":           capital_usd,
            "pdt_constrained":       constrained,
            "rolling5_count":        count,
            "pdt_limit":             PDT_LIMIT if constrained else None,
            "pdt_remaining":         int(rem) if rem != float("inf") else "unlimited",
            "can_enter":             self.can_enter_new_day_trade(capital_usd),
            "business_days_window":  [d.isoformat() for d in biz_days],
            "today_pdt_summary":     self.get_daily_pdt_summary(),
            "today_0dte_count":      today_0dte,
            "today_1dte_count":      today_1dte,
            "today_fallback_count":  today_fallback,
        }

    # ── 内部ユーティリティ ────────────────────────────────────────────────────

    def _append(self, record: dict) -> None:
        """fcntl.flock を使って atomic に JSONL 追記する。"""
        line = json.dumps(record, ensure_ascii=False)
        try:
            with open(self.data_file, "a", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception as e:
            log.error(f"[PDT] 書込エラー: {e}")

    def _append_non_pdt(self, record: dict) -> None:
        """PDT対象外取引を別ファイルに atomic 追記する。"""
        line = json.dumps(record, ensure_ascii=False)
        try:
            with open(self._non_pdt_file, "a", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception as e:
            log.error(f"[PDT] non_pdt 書込エラー: {e}")


# ── ヘルパー関数 ──────────────────────────────────────────────────────────────

def _to_et(dt: datetime.datetime) -> datetime.datetime:
    """datetime を ET に変換する。naive は ET として解釈する。"""
    if dt.tzinfo is None:
        # naive → ET として解釈（localize）
        try:
            return dt.replace(tzinfo=ET)
        except Exception:
            return dt
    return dt.astimezone(ET)


def _parse_date(date_str: str) -> Optional[datetime.date]:
    """YYYY-MM-DD 形式の文字列を datetime.date に変換する。失敗時は None。"""
    try:
        return datetime.date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


# ── グローバルシングルトン ────────────────────────────────────────────────────

_global_tracker: Optional[PDTTracker] = None


def get_global_tracker() -> PDTTracker:
    """プロセスごとのシングルトンを返す。"""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = PDTTracker()
    return _global_tracker
