"""common/market_specs.py — market_specs.yaml の Python ローダー

役割:
  market_specs.yaml を読み込み、市場セッション定数・判定関数を提供する。
  common/market_calendar.py はこのモジュールから値を取得することで
  ハードコード値を排除し、Single Source of Truth を維持する。

使用例:
    from common.market_specs import get_session_jst, is_in_session
    from datetime import datetime, timezone, timedelta

    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    print(is_in_session("cme_futures_equity", now))
    print(is_in_session("spx_options", now))
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

# ── 定数 ─────────────────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))

_SPECS_PATH = Path(__file__).parent / "market_specs.yaml"

# キャッシュ（モジュールロード時に一度だけ読む）
_specs_cache: dict | None = None


def _load_specs() -> dict:
    """market_specs.yaml を読み込む（キャッシュ付き）。"""
    global _specs_cache
    if _specs_cache is not None:
        return _specs_cache

    if not _YAML_AVAILABLE:
        raise ImportError(
            "PyYAML が見つかりません。`pip install pyyaml` を実行してください。"
        )

    if not _SPECS_PATH.exists():
        raise FileNotFoundError(
            f"market_specs.yaml が見つかりません: {_SPECS_PATH}\n"
            "common/market_specs.yaml が存在することを確認してください。"
        )

    with _SPECS_PATH.open(encoding="utf-8") as f:
        _specs_cache = yaml.safe_load(f)

    return _specs_cache


def get_market_spec(market: str) -> dict:
    """指定した市場の仕様辞書を返す。

    Args:
        market: "spx_options" または "cme_futures_equity"

    Returns:
        market_specs.yaml の markets[market] 辞書
    """
    specs = _load_specs()
    markets = specs.get("markets", {})
    if market not in markets:
        available = list(markets.keys())
        raise ValueError(
            f"未知の market: {market!r}\n"
            f"利用可能: {available}\n"
            f"定義ファイル: {_SPECS_PATH}"
        )
    return markets[market]


def get_session_jst(
    market: str,
    dst: bool = True,
) -> list[tuple[str, str]]:
    """指定した市場の JST セッション時刻リストを返す。

    Args:
        market: "spx_options" または "cme_futures_equity"
        dst:    True = 夏時間 (EDT+13h)、False = 冬時間 (EST+14h)

    Returns:
        [{open: "HH:MM", close: "HH:MM"}, ...] のリスト
        CME 先物の場合は open_day / close_day 形式になる

    Note:
        この関数は仕様確認・ドキュメント目的の値を返す。
        実際のセッション判定は is_in_session() を使う。
    """
    spec = get_market_spec(market)
    key = "session_jst_edt" if dst else "session_jst_est"

    sessions = spec.get(key, [])
    result = []
    for s in sessions:
        open_val = s.get("open") or s.get("open_day", "unknown")
        close_val = s.get("close") or s.get("close_day", "unknown")
        result.append((str(open_val), str(close_val)))
    return result


def get_daily_break_jst(
    market: str,
    dst: bool = True,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """デイリー休止時刻を (start_hm, end_hm) のタプルで返す。

    Args:
        market: "spx_options" または "cme_futures_equity"
        dst:    True = 夏時間、False = 冬時間

    Returns:
        ((start_h, start_m), (end_h, end_m)) または None（休止なし）
    """
    spec = get_market_spec(market)
    key = "daily_break_jst_edt" if dst else "daily_break_jst_est"
    breaks = spec.get(key)
    if not breaks:
        return None

    b = breaks[0]
    start_str: str = b["start"]
    end_str: str = b["end"]
    sh, sm = (int(x) for x in start_str.split(":"))
    eh, em = (int(x) for x in end_str.split(":"))
    return (sh, sm), (eh, em)


def is_in_session(market: str, now: datetime) -> bool:
    """指定した市場の取引可能時間帯かを判定する。

    この関数は common/market_calendar.py の is_in_market_hours() を
    market_specs.yaml ベースで実行する薄いラッパー。

    Args:
        market: "spx_options" または "cme_futures_equity"
        now:    判定基準日時（tzinfo 付き推奨）

    Returns:
        True = 取引可能、False = 閉場 / 休止中
    """
    from common.market_calendar import is_in_market_hours

    # market_calendar は "cme_futures" / "spx_options" を受け付ける
    # market_specs.yaml は "cme_futures_equity" で定義しているため変換
    cal_key = _to_calendar_key(market)
    return is_in_market_hours(cal_key, now)


def _to_calendar_key(market: str) -> str:
    """market_specs.yaml のキーを market_calendar.py のキーに変換。"""
    mapping = {
        "cme_futures_equity": "cme_futures",
        "spx_options": "spx_options",
    }
    if market not in mapping:
        # 既に calendar キー形式であればそのまま通す
        if market in ("cme_futures", "spx_options"):
            return market
        raise ValueError(
            f"変換できない market キー: {market!r}\n"
            f"market_specs.yaml のキーか market_calendar.py のキーを指定してください。"
        )
    return mapping[market]


def print_confusion_prevention() -> None:
    """混同防止チェックリストをコンソールに表示する（Hook・デバッグ用）。"""
    specs = _load_specs()
    items = specs.get("confusion_prevention", [])
    print("=" * 60)
    print("  MARKET SPEC GUARD — 混同防止チェックリスト")
    print("=" * 60)
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    print(f"  Source: {_SPECS_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    # python -m common.market_specs で現在の仕様を確認できる
    print_confusion_prevention()
    print()
    now = datetime.now(JST)
    print(f"現在時刻 (JST): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print()
    for market in ("spx_options", "cme_futures_equity"):
        sessions = get_session_jst(market)
        in_session = is_in_session(market, now)
        spec = get_market_spec(market)
        print(f"[{market}]")
        print(f"  説明: {spec['description']}")
        print(f"  用途: {spec['used_by']}")
        print(f"  セッション (JST/EDT): {sessions}")
        db = get_daily_break_jst(market)
        if db:
            print(f"  デイリー休止 (JST/EDT): {db[0]}〜{db[1]}")
        print(f"  現在取引可能: {in_session}")
        print()
