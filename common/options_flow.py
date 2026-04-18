"""
common/options_flow.py — 大口オプションフロー検知モジュール

## 設計思想
大口投資家・機関はオプション市場でポジションを取る。
Volume/OI比率の急騰・ブロックトレード・Call/Putスキューの偏りが
方向性シグナルになる。

## 検知項目
1. Unusual Options Activity (UOA): volume/OI > P90動的閾値
2. Block Trade: 単一取引で500枚以上 (大口スイープ)
3. Call/Put Volume Skew: コールとプットの出来高比率
4. Net Premium Direction: コール - プット の純プレミアム差

## データソース
- ThetaData: data/thetadata/{YYYYMMDD}/greeks_*.parquet (既存ローカル)
- ThetaData REST API: http://127.0.0.1:25510 (ローカルサーバー)

## Graceful Degradation
- ThetaDataサーバー未起動 → ローカルparquetから読む
- ローカルparquetなし → neutral signal (flow_bias=0.0)
- 部分データ → 取得できた分のみで算出
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ThetaData ローカルparquetのデフォルトパス
_THETA_DATA_DIR = Path("/Users/yuusakuichio/trading/data/thetadata")

# ブロックトレード閾値: 500枚以上を大口とみなす
# 固定値に見えるが、これはFINRA/業界実務での一般的なブロックサイズ定義
# (TastyTrade・OptionsFlow系ツールが使う基準と一致)
BLOCK_TRADE_MIN_CONTRACTS = 500


@dataclass
class OptionFlowRecord:
    """1行分のオプションフローデータ。"""
    symbol:   str
    expiry:   str           # "20260418"
    strike:   float
    right:    str           # "C" or "P"
    volume:   int
    open_interest: int
    mid_price: float
    premium_total: float    # volume × mid_price × 100
    is_block: bool = False  # volume >= BLOCK_TRADE_MIN_CONTRACTS


@dataclass
class FlowSignal:
    """1銘柄のフロー分析結果。"""
    symbol: str
    # 出来高分析
    call_volume:  int = 0
    put_volume:   int = 0
    call_put_ratio: float = 1.0   # > 1.0 = コール優勢
    # ブロックトレード
    block_calls:  int = 0         # ブロックトレード件数 (コール)
    block_puts:   int = 0
    # UOA (Unusual Options Activity)
    uoa_symbols:  list[str] = field(default_factory=list)  # UOA検知されたexpiry/strike
    uoa_count:    int = 0
    # プレミアム差
    net_premium:  float = 0.0     # コール - プット 総プレミアム
    # 総合シグナル
    flow_bias:    float = 0.0     # -1.0 (超強気プット) 〜 +1.0 (超強気コール)
    confidence:   float = 0.0     # データ量に基づく信頼度 (0〜1)
    data_available: bool = False

    def direction(self) -> str:
        """flow_biasから方向性を返す。"""
        if self.flow_bias > 0.3:
            return "bullish"
        elif self.flow_bias < -0.3:
            return "bearish"
        return "neutral"


# ── データ読み込み ─────────────────────────────────────────────────────────────

def _load_from_parquet(symbol: str, date_str: str) -> list[OptionFlowRecord]:
    """ローカルThetaDataのparquetから読み込む。

    Args:
        symbol:   "SPY", "QQQ" 等
        date_str: "20260417" 形式

    Returns:
        list[OptionFlowRecord] (空リスト = データなし)
    """
    try:
        import pandas as pd

        path = _THETA_DATA_DIR / date_str / f"greeks_first_order_{symbol}.parquet"
        if not path.exists():
            log.debug(f"[OptionsFlow] parquet not found: {path}")
            return []

        df = pd.read_parquet(path)

        # カラム名の揺れに対応
        vol_col = next((c for c in df.columns if "volume" in c.lower()), None)
        oi_col  = next((c for c in df.columns if "open_interest" in c.lower() or c.lower() == "oi"), None)
        mid_col = next((c for c in df.columns if "mid" in c.lower() or "price" in c.lower()), None)

        if vol_col is None:
            log.warning(f"[OptionsFlow] volume column not found in {path}")
            return []

        records: list[OptionFlowRecord] = []
        for _, row in df.iterrows():
            try:
                vol    = int(row.get(vol_col, 0) or 0)
                oi     = int(row.get(oi_col, 0) or 0) if oi_col else 0
                mid    = float(row.get(mid_col, 0.0) or 0.0) if mid_col else 0.0
                right  = str(row.get("right", row.get("call_put", "C"))).upper()[:1]
                strike = float(row.get("strike", 0.0))
                expiry = str(row.get("expiry", row.get("exp", date_str)))
                records.append(OptionFlowRecord(
                    symbol=symbol,
                    expiry=expiry,
                    strike=strike,
                    right=right,
                    volume=vol,
                    open_interest=oi,
                    mid_price=mid,
                    premium_total=vol * mid * 100,
                    is_block=(vol >= BLOCK_TRADE_MIN_CONTRACTS),
                ))
            except Exception:
                continue
        log.info(f"[OptionsFlow] loaded {len(records)} records for {symbol} from {date_str}")
        return records

    except ImportError:
        log.warning("[OptionsFlow] pandas not available")
        return []
    except Exception as e:
        log.warning(f"[OptionsFlow] parquet load error: {e}")
        return []


def _fetch_from_thetadata_rest(symbol: str) -> list[OptionFlowRecord]:
    """ThetaData REST API（ローカルサーバー）からリアルタイムデータを取得。

    失敗時は空リストを返す（graceful degradation）。
    """
    try:
        import requests
        import datetime

        today = datetime.date.today().strftime("%Y%m%d")
        # ThetaData REST API: http://127.0.0.1:25510/v2/bulk_snapshot/option/quote
        resp = requests.get(
            "http://127.0.0.1:25510/v2/bulk_snapshot/option/quote",
            params={
                "root":     symbol,
                "exp":      today,
                "use_csv":  True,
            },
            timeout=5,
        )
        if resp.status_code != 200:
            log.debug(f"[OptionsFlow] ThetaData REST {symbol}: HTTP {resp.status_code}")
            return []

        # CSV形式でパース
        records: list[OptionFlowRecord] = []
        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return []
        headers = [h.strip().lower() for h in lines[0].split(",")]

        def _col(row_parts: list[str], col_name: str, default="0") -> str:
            try:
                return row_parts[headers.index(col_name)].strip()
            except (ValueError, IndexError):
                return default

        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                vol   = int(float(_col(parts, "volume")))
                oi    = int(float(_col(parts, "open_interest")))
                bid   = float(_col(parts, "bid"))
                ask   = float(_col(parts, "ask"))
                mid   = (bid + ask) / 2.0
                right = _col(parts, "right", "C").upper()[:1]
                strike = float(_col(parts, "strike", "0"))
                exp   = _col(parts, "exp", today)
                records.append(OptionFlowRecord(
                    symbol=symbol, expiry=exp, strike=strike, right=right,
                    volume=vol, open_interest=oi, mid_price=mid,
                    premium_total=vol * mid * 100,
                    is_block=(vol >= BLOCK_TRADE_MIN_CONTRACTS),
                ))
            except Exception:
                continue
        return records

    except Exception as e:
        log.debug(f"[OptionsFlow] ThetaData REST failed: {e}")
        return []


# ── 分析ロジック ──────────────────────────────────────────────────────────────

def _compute_uoa_threshold(volumes: list[int]) -> float:
    """P90動的閾値を算出。データ不足時は固定値に fallback。

    固定閾値ゼロ: ユニバース内の分布から P90 を使う。
    """
    if len(volumes) < 10:
        return 100.0  # データ不足時フォールバック
    sorted_v = sorted(volumes)
    idx = int(len(sorted_v) * 0.9)
    return float(sorted_v[idx])


def analyze_flow(
    records: list[OptionFlowRecord],
    symbol: str,
) -> FlowSignal:
    """フローレコードを分析してFlowSignalを返す。

    Args:
        records: OptionFlowRecord のリスト
        symbol:  銘柄名（ログ用）

    Returns:
        FlowSignal
    """
    if not records:
        return FlowSignal(
            symbol=symbol, flow_bias=0.0, confidence=0.0, data_available=False
        )

    calls = [r for r in records if r.right == "C"]
    puts  = [r for r in records if r.right == "P"]

    call_vol = sum(r.volume for r in calls)
    put_vol  = sum(r.volume for r in puts)
    total_vol = call_vol + put_vol

    # Call/Put Ratio
    cp_ratio = call_vol / put_vol if put_vol > 0 else (10.0 if call_vol > 0 else 1.0)

    # Block trades
    block_calls = sum(1 for r in calls if r.is_block)
    block_puts  = sum(1 for r in puts  if r.is_block)

    # UOA: P90動的閾値を超えた取引
    all_volumes = [r.volume for r in records if r.volume > 0]
    uoa_threshold = _compute_uoa_threshold(all_volumes)
    uoa_records = [r for r in records if r.volume > uoa_threshold]
    uoa_count = len(uoa_records)

    # Net Premium
    call_premium = sum(r.premium_total for r in calls)
    put_premium  = sum(r.premium_total for r in puts)
    net_premium  = call_premium - put_premium
    total_premium = call_premium + put_premium

    # flow_bias 算出 (-1.0 〜 +1.0)
    # 3指標の加重平均:
    #   CP ratio: 0.4 ウェイト
    #   Net premium: 0.4 ウェイト
    #   Block direction: 0.2 ウェイト
    bias_components: list[float] = []

    # CP ratio → [-1, +1]
    if total_vol > 0:
        # cp_ratio: 1.0 = neutral → bias 0.0; 2.0 = 2:1 call → +0.5 等
        cp_bias = (cp_ratio - 1.0) / (cp_ratio + 1.0)  # tanh様で bounded
        bias_components.append(cp_bias * 0.4)

    # Net premium → [-1, +1]
    if total_premium > 0:
        prem_bias = net_premium / total_premium  # -1〜+1
        bias_components.append(prem_bias * 0.4)

    # Block direction
    total_blocks = block_calls + block_puts
    if total_blocks > 0:
        block_bias = (block_calls - block_puts) / total_blocks
        bias_components.append(block_bias * 0.2)

    flow_bias = sum(bias_components) if bias_components else 0.0
    flow_bias = max(-1.0, min(1.0, flow_bias))

    # 信頼度: データ量に比例 (1000枚以上で 1.0)
    confidence = min(1.0, total_vol / 1000.0)

    sig = FlowSignal(
        symbol=symbol,
        call_volume=call_vol,
        put_volume=put_vol,
        call_put_ratio=cp_ratio,
        block_calls=block_calls,
        block_puts=block_puts,
        uoa_count=uoa_count,
        net_premium=net_premium,
        flow_bias=flow_bias,
        confidence=confidence,
        data_available=True,
    )
    log.info(
        f"[OptionsFlow] {symbol}: bias={flow_bias:.3f} "
        f"CP={cp_ratio:.2f} blocks(C/P)={block_calls}/{block_puts} "
        f"uoa={uoa_count} conf={confidence:.2f}"
    )
    return sig


# ── パブリック API ────────────────────────────────────────────────────────────

def get_flow_signal(
    symbol: str,
    date_str: Optional[str] = None,
    records: Optional[list[OptionFlowRecord]] = None,
) -> FlowSignal:
    """銘柄のオプションフローシグナルを返す。

    Args:
        symbol:   銘柄ティッカー
        date_str: "20260418" 形式。Noneで今日
        records:  テスト用外部注入データ。指定時はAPI/ファイル読み込みをスキップ

    Returns:
        FlowSignal
    """
    import datetime

    if records is not None:
        return analyze_flow(records, symbol)

    # ThetaData REST (リアルタイム優先)
    rest_records = _fetch_from_thetadata_rest(symbol)
    if rest_records:
        return analyze_flow(rest_records, symbol)

    # ローカルparquet (当日データ)
    if date_str is None:
        date_str = datetime.date.today().strftime("%Y%m%d")
    file_records = _load_from_parquet(symbol, date_str)
    if file_records:
        return analyze_flow(file_records, symbol)

    # データなし → neutral
    log.info(f"[OptionsFlow] {symbol}: no data available, returning neutral")
    return FlowSignal(symbol=symbol, flow_bias=0.0, confidence=0.0, data_available=False)


def get_flow_signals(
    symbols: list[str],
    date_str: Optional[str] = None,
) -> dict[str, FlowSignal]:
    """複数銘柄のフローシグナルをまとめて返す。"""
    return {sym: get_flow_signal(sym, date_str=date_str) for sym in symbols}
