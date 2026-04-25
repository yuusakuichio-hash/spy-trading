"""atlas_v3/bots/engines/slippage_guard.py — Bid/Ask スリッページガード (L01)

設計思想
--------
tastytrade 公式研究 (2014/2019): Credit Spread の実効スリッページ
(ask-bid)/2 が net_credit の 33% を超えると期待値がマイナスに転化する。
Tom Sosnoff / Tony Battista が番組で繰り返し言及している基準。

ソース一次情報
--------------
- tastytrade "Managing Slippage" 2014 研究アーカイブ
- tastytrade "Market Measures" 2019 スプレッドコスト解析
- r/thetagang: "Bid-Ask width / mid_price <= 10%" 基準 (2022-2024 複数スレッド)

実装ルール (atlas_v3 namespace・spy_bot.py 書換禁止)
-----------------------------------------------------
- slippage_est = (sell_ask - sell_bid)/2 + (buy_ask - buy_bid)/2
- if slippage_est / net_credit > SLIPPAGE_RATIO_MAX → BLOCK
- SLIPPAGE_RATIO_MAX = 0.33 (tastytrade 基準)
- net_credit <= 0 の場合は別エラーとして区別
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# tastytrade 公式推奨基準
SLIPPAGE_RATIO_MAX: float = 0.33


@dataclass(frozen=True)
class SlippageCheckResult:
    """スリッページチェック結果 DTO。"""
    allowed: bool
    reason: str
    slippage_est: float = 0.0
    net_credit: float = 0.0
    slippage_ratio: float = 0.0


class SlippageGuard:
    """Bid/Ask スリッページが許容範囲内かを検査するガード。

    各エンジンの発注前に呼び出すことで、流動性の低いオプションへの
    エントリーを物理ブロックする。

    例::
        from atlas_v3.bots.engines.slippage_guard import SlippageGuard

        guard = SlippageGuard()
        result = guard.check(
            sell_bid=2.30, sell_ask=2.40,
            buy_bid=1.80, buy_ask=1.90,
        )
        if not result.allowed:
            log.warning("slippage blocked: %s", result.reason)
            return
    """

    def __init__(self, max_ratio: float = SLIPPAGE_RATIO_MAX) -> None:
        assert 0 < max_ratio <= 1.0, f"max_ratio={max_ratio} out of (0, 1]"
        self._max_ratio = max_ratio

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def check(
        self,
        sell_bid: float,
        sell_ask: float,
        buy_bid: float,
        buy_ask: float,
    ) -> SlippageCheckResult:
        """スリッページ比率を計算して許容判定を返す。

        Parameters
        ----------
        sell_bid / sell_ask: ショートレグの Bid / Ask
        buy_bid  / buy_ask:  ロングレグの Bid / Ask

        Returns
        -------
        SlippageCheckResult
        """
        assert sell_bid >= 0.0, f"sell_bid={sell_bid} < 0"
        assert sell_ask >= sell_bid, f"sell_ask={sell_ask} < sell_bid={sell_bid}"
        assert buy_bid >= 0.0, f"buy_bid={buy_bid} < 0"
        assert buy_ask >= buy_bid, f"buy_ask={buy_ask} < buy_bid={buy_bid}"

        # net_credit = ショート受取 - ロング支払
        net_credit = round(sell_bid - buy_ask, 4)

        if net_credit <= 0.0:
            return SlippageCheckResult(
                allowed=False,
                reason=f"net_credit={net_credit:.4f} <= 0: unprofitable spread",
                slippage_est=0.0,
                net_credit=net_credit,
                slippage_ratio=float("inf"),
            )

        slippage_est = (sell_ask - sell_bid) / 2.0 + (buy_ask - buy_bid) / 2.0
        ratio = slippage_est / net_credit

        if ratio > self._max_ratio:
            return SlippageCheckResult(
                allowed=False,
                reason=(
                    f"slippage_ratio={ratio:.3f} > max={self._max_ratio:.3f} "
                    f"(slippage_est={slippage_est:.4f} / net_credit={net_credit:.4f})"
                ),
                slippage_est=slippage_est,
                net_credit=net_credit,
                slippage_ratio=ratio,
            )

        return SlippageCheckResult(
            allowed=True,
            reason=f"slippage_ratio={ratio:.3f} OK (<= {self._max_ratio:.3f})",
            slippage_est=slippage_est,
            net_credit=net_credit,
            slippage_ratio=ratio,
        )

    # ------------------------------------------------------------------
    # 便利ラッパー: dict 形式の leg data から直接呼ぶ
    # ------------------------------------------------------------------

    def check_from_legs(
        self,
        sell_opt: dict,
        buy_opt: dict,
    ) -> SlippageCheckResult:
        """leg dict から計算する。

        dict キー: bid_price/ask_price (futu 形式) または bid/ask (短縮形)。
        どちらの key set も無い場合 KeyError を raise (test 互換)。
        """
        def _get_bid_ask(d: dict) -> tuple[float, float]:
            if "bid_price" in d and "ask_price" in d:
                return float(d["bid_price"]), float(d["ask_price"])
            if "bid" in d and "ask" in d:
                return float(d["bid"]), float(d["ask"])
            raise KeyError(
                f"slippage leg dict requires (bid_price+ask_price) or (bid+ask): keys={list(d.keys())}"
            )
        sell_bid, sell_ask = _get_bid_ask(sell_opt)
        buy_bid, buy_ask = _get_bid_ask(buy_opt)
        return self.check(sell_bid, sell_ask, buy_bid, buy_ask)
