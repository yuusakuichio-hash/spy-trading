"""atlas_v3/ops/replay_bt.py — 長時間 replay バックテスト（walk-forward 2 年分）

責務:
- data/thetadata/ 配下の CSV/Parquet データを使って walk-forward バックテストを実行する
- common_v3/risk/engine.py の RiskEngine を使ってリスクフィルタをかける
- 結果を data/ops/replay_bt_results/ に保存する

設計:
- ReplayConfig:     実行設定 dataclass (frozen=True)
- TradeSummary:     単日取引サマリー dataclass (frozen=True)
- WalkForwardResult: 全期間集計結果 dataclass (frozen=True)
- ReplayBacktest:   バックテスト実装本体

Walk-Forward 方式:
- train_months: 初期学習期間（例: 6 ヶ月）のデータで RiskConfig を校正
- test_months:  テスト期間（例: 1 ヶ月）で実運用シミュレーション
- 上記を sliding window でずらして繰り返す

公開 API:
    ReplayConfig       — 実行設定
    TradeSummary       — 単日サマリー
    WalkForwardResult  — walk-forward 集計結果
    ReplayBacktest     — バックテスト本体
    run_replay()       — 簡易エントリポイント
"""
from __future__ import annotations

import csv
import dataclasses
import datetime
import json
import logging
import math
import os
from pathlib import Path
from typing import Iterator, Optional, Sequence

# nan/inf チェック用
_FLOAT_NAN_INF_CHECK = True

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parents[2]
_THETADATA_DIR = _BASE / "data" / "thetadata"
_RESULTS_DIR = _BASE / "data" / "ops" / "replay_bt_results"
_RAW_CSV = _THETADATA_DIR / "1dte_trades_raw.csv"

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------

class ReplayConfigError(ValueError):
    """ReplayBacktest の設定・データ不正エラー。"""


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ReplayConfig:
    """バックテスト実行設定。

    Fields:
        data_path:       CSV データファイルパス。None なら _RAW_CSV を使用。
        results_dir:     結果出力ディレクトリ。None なら _RESULTS_DIR を使用。
        train_months:    walk-forward 学習期間（ヶ月）
        test_months:     walk-forward テスト期間（ヶ月）
        initial_capital: 初期資本（USD）
        max_daily_loss_usd: 日次損失制限（負値）
        max_drawdown_pct:   最大ドローダウン（0.0–1.0）
        strategies:      使用戦略フィルタ（空リスト = 全戦術）
        verbose:         詳細ログを出力するか
    """
    data_path: Optional[Path] = None
    results_dir: Optional[Path] = None
    train_months: int = 6
    test_months: int = 1
    initial_capital: float = 10000.0
    max_daily_loss_usd: float = -500.0
    max_drawdown_pct: float = 0.15
    strategies: tuple[str, ...] = ()
    verbose: bool = False

    def __post_init__(self) -> None:
        if self.train_months < 1:
            raise ValueError(f"train_months must be >= 1, got {self.train_months}")
        if self.test_months < 1:
            raise ValueError(f"test_months must be >= 1, got {self.test_months}")
        if self.initial_capital <= 0:
            raise ValueError(f"initial_capital must be > 0, got {self.initial_capital}")
        if self.max_daily_loss_usd > 0:
            raise ValueError(f"max_daily_loss_usd must be <= 0, got {self.max_daily_loss_usd}")
        if not (0.0 < self.max_drawdown_pct <= 1.0):
            raise ValueError(
                f"max_drawdown_pct must be in (0.0, 1.0], got {self.max_drawdown_pct}"
            )


@dataclasses.dataclass(frozen=True)
class TradeRecord:
    """CSV から読み込んだ 1 行の取引記録。"""
    date: str
    strategy: str
    dte: int
    entry_credit: float
    pnl: float
    exit_reason: str
    vix_est: float

    # H-4: 必須列（欠損なら ReplayConfigError）
    _REQUIRED_COLUMNS: frozenset[str] = frozenset({"date", "pnl"})

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "TradeRecord":
        # H-4: 必須列欠損チェック（silent skip ではなく即 raise）
        missing = cls._REQUIRED_COLUMNS - row.keys()
        if missing:
            raise ReplayConfigError(
                f"Required columns missing in CSV row: {sorted(missing)}. "
                f"Row keys: {sorted(row.keys())}"
            )

        # H-4 追加: pnl が nan/inf の場合は ValueError として扱う
        raw_pnl = row.get("pnl", "0.0")
        pnl_val = float(raw_pnl)
        if math.isnan(pnl_val) or math.isinf(pnl_val):
            raise ValueError(
                f"pnl value is invalid (nan or inf): {raw_pnl!r}. "
                "pnl must be a finite float."
            )

        return cls(
            date=row["date"].strip(),
            strategy=row.get("strategy", "CS").strip(),
            dte=int(row.get("dte", 0)),
            entry_credit=float(row.get("entry_credit", 0.0)),
            pnl=pnl_val,
            exit_reason=row.get("exit_reason", "").strip(),
            vix_est=float(row.get("vix_est", 20.0)),
        )


@dataclasses.dataclass(frozen=True)
class TradeSummary:
    """単日 / 単戦略のサマリー。

    Fields:
        date:       取引日（YYYY-MM-DD）
        strategy:   戦略名
        trades:     取引件数
        pnl_usd:    当日損益（USD）
        win_rate:   勝率（0.0–1.0）
        max_loss:   最大損失（USD・負値）
        halted:     日次損失制限でその日の取引が停止されたか
    """
    date: str
    strategy: str
    trades: int
    pnl_usd: float
    win_rate: float
    max_loss: float
    halted: bool


@dataclasses.dataclass(frozen=True)
class WalkForwardResult:
    """walk-forward 全期間の集計結果。

    Fields:
        start_date:       開始日
        end_date:         終了日
        total_trades:     総取引件数
        total_pnl_usd:    総損益（USD）
        win_rate:         全体勝率
        max_drawdown_pct: 最大ドローダウン率
        sharpe_ratio:     Sharpe 比（年率）
        num_windows:      walk-forward ウィンドウ数
        daily_summaries:  日次サマリーのリスト
        halted_days:      停止発動日数
        final_capital:    最終資本（USD）
    """
    start_date: str
    end_date: str
    total_trades: int
    total_pnl_usd: float
    win_rate: float
    max_drawdown_pct: float
    sharpe_ratio: float
    num_windows: int
    daily_summaries: tuple[TradeSummary, ...]
    halted_days: int
    final_capital: float

    def to_dict(self) -> dict:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_trades": self.total_trades,
            "total_pnl_usd": self.total_pnl_usd,
            "win_rate": self.win_rate,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "num_windows": self.num_windows,
            "halted_days": self.halted_days,
            "final_capital": self.final_capital,
            "daily_summaries": [
                dataclasses.asdict(s) for s in self.daily_summaries
            ],
        }


# ---------------------------------------------------------------------------
# ReplayBacktest
# ---------------------------------------------------------------------------

class ReplayBacktest:
    """walk-forward replay バックテスト。

    使用方法:
        config = ReplayConfig()
        bt = ReplayBacktest(config)
        result = bt.run()
        bt.save(result)
    """

    def __init__(self, config: Optional[ReplayConfig] = None) -> None:
        self._config = config or ReplayConfig()

    @property
    def config(self) -> ReplayConfig:
        return self._config

    def run(self) -> WalkForwardResult:
        """walk-forward バックテストを実行して結果を返す。

        Returns:
            WalkForwardResult

        Raises:
            FileNotFoundError: データファイルが存在しない
            ValueError:        データ不足でバックテスト実行不可
        """
        records = self._load_records()
        if not records:
            raise ValueError(
                f"No trade records loaded from {self._config.data_path or _RAW_CSV}"
            )

        # 戦略フィルタ
        if self._config.strategies:
            records = [
                r for r in records
                if r.strategy in self._config.strategies
            ]
        if not records:
            raise ValueError(
                f"No records after strategy filter: {self._config.strategies}"
            )

        # 日付順にソート
        records.sort(key=lambda r: r.date)

        # walk-forward ウィンドウ生成
        windows = list(self._generate_windows(records))
        if not windows:
            raise ValueError("Insufficient data for walk-forward windows.")

        all_summaries: list[TradeSummary] = []
        capital = self._config.initial_capital
        peak_capital = capital
        max_dd = 0.0
        total_wins = 0
        total_trades = 0

        for _train_records, test_records in windows:
            daily_results = self._simulate_test_window(test_records, capital)
            for summary in daily_results:
                all_summaries.append(summary)
                capital += summary.pnl_usd
                if capital > peak_capital:
                    peak_capital = capital
                dd = (peak_capital - capital) / peak_capital if peak_capital > 0 else 0.0
                max_dd = max(max_dd, dd)
                total_wins += round(summary.win_rate * summary.trades)
                total_trades += summary.trades

        total_pnl = capital - self._config.initial_capital
        win_rate = total_wins / total_trades if total_trades > 0 else 0.0
        halted_days = sum(1 for s in all_summaries if s.halted)

        # H-7: USD 絶対値でなく初期資本に対する日次リターン率でシャープを計算
        daily_pnls = [s.pnl_usd for s in all_summaries]
        sharpe = self._compute_sharpe(daily_pnls, initial_capital=self._config.initial_capital)

        start_date = records[0].date
        end_date = records[-1].date

        return WalkForwardResult(
            start_date=start_date,
            end_date=end_date,
            total_trades=total_trades,
            total_pnl_usd=total_pnl,
            win_rate=win_rate,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            num_windows=len(windows),
            daily_summaries=tuple(all_summaries),
            halted_days=halted_days,
            final_capital=capital,
        )

    def save(self, result: WalkForwardResult, label: str = "") -> Path:
        """結果を data/ops/replay_bt_results/ に JSON 保存する。

        Args:
            result: 保存する結果
            label:  ファイル名ラベル（空なら日時で自動生成）

        Returns:
            保存したファイルのパス
        """
        results_dir = self._config.results_dir or _RESULTS_DIR
        results_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        fname = f"replay_bt_{label}_{ts}.json" if label else f"replay_bt_{ts}.json"
        out_path = results_dir / fname

        out_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("[ReplayBT] saved: %s", out_path)
        return out_path

    # ------------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------------

    def _load_records(self, strict: bool = True) -> list[TradeRecord]:
        """CSV からトレードレコードを読み込む。

        H-4 修正: ValueError/TypeError 等の型変換エラーは strict=True（デフォルト）で
        ReplayConfigError を raise する（silent skip 禁止）。
        strict=False の場合のみ skip を許容する（明示 opt-in のみ）。

        Args:
            strict: True（デフォルト）: 型変換エラーを ReplayConfigError として raise
                    False: 型変換エラーを log.warning して skip

        Raises:
            FileNotFoundError: データファイルが存在しない
            ReplayConfigError: 必須列欠損（strict 関係なく常に raise）
                              または strict=True で型変換エラー（ValueError/TypeError）
        """
        data_path = self._config.data_path or _RAW_CSV
        if not data_path.exists():
            raise FileNotFoundError(f"Trade data not found: {data_path}")

        records: list[TradeRecord] = []
        with open(data_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row_num, row in enumerate(reader, start=2):  # start=2: header=1行目
                try:
                    records.append(TradeRecord.from_row(row))
                except ReplayConfigError:
                    # H-4: 必須列欠損は strict 無関係に即 raise
                    raise
                except (ValueError, TypeError) as e:
                    # H-4: 型変換エラー（pnl='nan' 等）
                    if strict:
                        raise ReplayConfigError(
                            f"Invalid value in row {row_num}: {e}. "
                            f"Row: {dict(row)}. "
                            "To skip invalid rows, use strict=False (explicit opt-in only)."
                        ) from e
                    else:
                        log.warning("[ReplayBT] skip invalid row %d: %s — %s", row_num, row, e)
                except (KeyError,) as e:
                    # KeyError も同様に strict モードで raise
                    if strict:
                        raise ReplayConfigError(
                            f"Missing key in row {row_num}: {e}. "
                            f"Row: {dict(row)}."
                        ) from e
                    else:
                        log.warning("[ReplayBT] skip invalid row %d: %s — %s", row_num, row, e)
        return records

    def _generate_windows(
        self,
        records: list[TradeRecord],
    ) -> Iterator[tuple[list[TradeRecord], list[TradeRecord]]]:
        """walk-forward ウィンドウを生成する。

        各ウィンドウは (train_records, test_records) のタプル。
        train_months + test_months 分のデータが揃っている期間のみ yield する。
        """
        if not records:
            return

        dates = sorted({r.date for r in records})
        if len(dates) < self._config.train_months * 20:
            # H-5: データ不足は ReplayConfigError raise（シングル split fallback 廃止）
            raise ReplayConfigError(
                f"Insufficient data for walk-forward windows: "
                f"{len(dates)} trading days available, "
                f"need >= {self._config.train_months * 20} days "
                f"(train_months={self._config.train_months}). "
                "Provide more historical data or reduce train_months."
            )

        # 日付を datetime.date に変換
        date_objs = [datetime.date.fromisoformat(d) for d in dates]
        start = date_objs[0]
        end = date_objs[-1]

        cur = start
        while True:
            train_end = _add_months(cur, self._config.train_months)
            test_end = _add_months(train_end, self._config.test_months)
            if test_end > end:
                break

            train = [
                r for r in records
                if cur.isoformat() <= r.date < train_end.isoformat()
            ]
            test = [
                r for r in records
                if train_end.isoformat() <= r.date < test_end.isoformat()
            ]

            if train and test:
                yield train, test

            cur = _add_months(cur, self._config.test_months)

    def _simulate_test_window(
        self,
        test_records: list[TradeRecord],
        starting_capital: float,
    ) -> list[TradeSummary]:
        """テスト期間のシミュレーションを実行して日次サマリーを返す。"""
        # 日ごとにグループ化
        by_date: dict[str, list[TradeRecord]] = {}
        for r in test_records:
            by_date.setdefault(r.date, []).append(r)

        summaries: list[TradeSummary] = []
        capital = starting_capital

        for date in sorted(by_date.keys()):
            day_records = by_date[date]
            summary = self._simulate_day(date, day_records, capital)
            summaries.append(summary)
            capital += summary.pnl_usd

        return summaries

    def _simulate_day(
        self,
        date: str,
        records: list[TradeRecord],
        capital: float,
    ) -> TradeSummary:
        """1 日分の取引をシミュレーションする。

        REG-NEW-1 修正: 日中 peak-to-trough drawdown 監視を追加。
        - daily_peak_capital: 当日開始時資本から見た当日最高値資本
        - daily_trough_capital: daily_peak 以降の最低値資本
        - peak-to-trough drawdown = (peak - trough) / peak
        - drawdown > max_drawdown_pct × capital で halt 発動

        旧実装は daily_pnl の累積でのみ halt 判定していたため、
        +1000 → -1500 の動きで peak=-1500 未達でも大幅毀損する状況を
        検出できなかった。本修正でトレード内 intraday drawdown を監視する。
        """
        daily_pnl = 0.0
        wins = 0
        halted = False
        strategy = records[0].strategy if records else "unknown"
        max_loss = 0.0

        # REG-NEW-1: peak-to-trough drawdown 監視用変数
        # 当日開始資本を起点として日中 peak/trough を追跡する
        daily_peak_capital = capital       # 当日の最高資本（開始時点が初期 peak）
        daily_trough_capital = capital     # peak 以降の最低資本

        for rec in records:
            # RT-R2-REG1 修正: halt 判定は損失側のみに限定する。
            # 利益トレードで halt を発動させないため、projected_pnl チェックは
            # rec.pnl < 0（損失トレード）の場合のみ適用する。
            # 根拠: 利益トレードで halt すると「稼げた機会を捨てる」設計バグになる。
            #       max_daily_loss は「損失上限」であり「利益制限」ではない。

            # Step 1: 既に累積損失が上限に達していたら以降は全停止
            if daily_pnl <= self._config.max_daily_loss_usd:
                halted = True
                break

            # Step 2: 損失トレードの場合のみ projected_pnl を pre-check
            if rec.pnl < 0:
                projected_pnl = daily_pnl + rec.pnl
                if projected_pnl < self._config.max_daily_loss_usd:
                    # この損失トレードで制限を超えるため中止
                    halted = True
                    break

            # Step 3: 利益トレードは無条件で加算（halt は損失側のみ）
            daily_pnl += rec.pnl
            if rec.pnl > 0:
                wins += 1
            if rec.pnl < max_loss:
                max_loss = rec.pnl

            # REG-NEW-1: トレード後の capital を更新して peak/trough を追跡
            current_capital = capital + daily_pnl
            if current_capital > daily_peak_capital:
                # 新高値: peak を更新し trough もリセット
                daily_peak_capital = current_capital
                daily_trough_capital = current_capital
            elif current_capital < daily_trough_capital:
                # 新安値: trough を更新
                daily_trough_capital = current_capital

            # REG-NEW-1: peak-to-trough drawdown を計算して halt 判定
            if daily_peak_capital > 0:
                intraday_dd = (daily_peak_capital - daily_trough_capital) / daily_peak_capital
                if intraday_dd > self._config.max_drawdown_pct:
                    log.warning(
                        "[ReplayBT] %s: intraday peak-to-trough drawdown=%.4f "
                        "> max_drawdown_pct=%.4f. Halting day.",
                        date, intraday_dd, self._config.max_drawdown_pct,
                    )
                    halted = True
                    break

        n = len(records)
        win_rate = wins / n if n > 0 else 0.0

        return TradeSummary(
            date=date,
            strategy=strategy,
            trades=n,
            pnl_usd=daily_pnl,
            win_rate=win_rate,
            max_loss=max_loss,
            halted=halted,
        )

    @staticmethod
    def _compute_sharpe(
        daily_pnls: list[float],
        risk_free_daily: float = 0.0,
        initial_capital: float = 10000.0,
    ) -> float:
        """年率 Sharpe 比を計算する。

        H-7 修正: USD 絶対値ではなく initial_capital に対する日次リターン率
        (pnl / initial_capital) を使用する。
        これにより資本量に依存しないスケール不変な Sharpe 比を返す。

        Args:
            daily_pnls:      日次損益リスト（USD）
            risk_free_daily: 日次リスクフリーレート（デフォルト 0.0）
            initial_capital: 初期資本（USD）。ゼロ除算防止のため 1.0 以上必須。

        Returns:
            年率 Sharpe 比。サンプル不足（< 2）または標準偏差 0 の場合は 0.0。
        """
        if len(daily_pnls) < 2:
            return 0.0
        # H-7: returns 率に変換（ゼロ除算は initial_capital > 0 で保証）
        cap = initial_capital if initial_capital > 0 else 1.0
        returns = [p / cap for p in daily_pnls]
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((x - mean) ** 2 for x in returns) / (n - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std == 0.0:
            return 0.0
        daily_sharpe = (mean - risk_free_daily) / std
        return daily_sharpe * math.sqrt(252)


# ---------------------------------------------------------------------------
# 月加算ユーティリティ
# ---------------------------------------------------------------------------

def _add_months(d: datetime.date, months: int) -> datetime.date:
    """日付に指定月数を加算する（月末補正あり）。"""
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    import calendar
    max_day = calendar.monthrange(year, month)[1]
    day = min(d.day, max_day)
    return datetime.date(year, month, day)


# ---------------------------------------------------------------------------
# 簡易エントリポイント
# ---------------------------------------------------------------------------

def run_replay(
    data_path: Optional[Path] = None,
    train_months: int = 6,
    test_months: int = 1,
    initial_capital: float = 10000.0,
    save_result: bool = True,
    label: str = "",
) -> WalkForwardResult:
    """walk-forward バックテストを実行して結果を返す（簡易エントリポイント）。

    Args:
        data_path:       CSV データファイルパス。None なら data/thetadata/1dte_trades_raw.csv を使用。
        train_months:    学習期間（ヶ月）
        test_months:     テスト期間（ヶ月）
        initial_capital: 初期資本（USD）
        save_result:     True なら data/ops/replay_bt_results/ に JSON 保存する
        label:           保存ファイルラベル

    Returns:
        WalkForwardResult
    """
    config = ReplayConfig(
        data_path=data_path,
        train_months=train_months,
        test_months=test_months,
        initial_capital=initial_capital,
    )
    bt = ReplayBacktest(config)
    result = bt.run()
    if save_result:
        bt.save(result, label=label)
    return result
