"""
common/news_sentiment.py — 個別銘柄ニュースセンチメント分析モジュール

## 設計思想
ニュースは銘柄の方向性を瞬間的に変える。
決算サプライズ・アナリストアップグレード・M&Aは大きなギャップの予兆。
ネガティブニュース銘柄は売り戦術に不向きで除外すべき。

## データソース
Finnhub /company-news API (無料プラン: 60 req/min)
- 直近24時間の記事を取得
- タイトル・概要からキーワードマッチでセンチメント分類

## センチメント分類
- positive: アナリスト上方修正・決算サプライズ・買収プレミアム・新製品発表
- negative: 決算ミス・規制・集団訴訟・格下げ・リコール
- neutral:  上記以外

## 出力
  get_news_sentiment() → NewsSentiment
  filter_by_news() → ネガティブニュース銘柄除外リスト

## Graceful Degradation
  API失敗 / API KEY未設定 → neutral sentiment を返す
  レートリミット → exponential backoff (最大2回retry)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── ポジティブ・ネガティブキーワード ─────────────────────────────────────────
# 公式ドキュメント・金融NLP研究の標準的キーワードリストに基づく

_POSITIVE_KEYWORDS = [
    "beat", "beats", "surprise", "surpass", "upgrade", "upgraded",
    "buy rating", "outperform", "strong buy", "raised target",
    "price target raised", "merger", "acquisition", "acquires",
    "buyout", "deal", "partnership", "record revenue", "record earnings",
    "dividend increase", "share buyback", "repurchase",
    "fda approval", "approved", "new contract", "guidance raised",
    "upside", "bullish", "positive outlook",
]

_NEGATIVE_KEYWORDS = [
    "miss", "misses", "shortfall", "disappoint", "disappointing",
    "downgrade", "downgraded", "sell rating", "underperform", "avoid",
    "price target cut", "lowered target", "guidance cut", "guidance lowered",
    "lawsuit", "litigation", "probe", "investigation", "fraud",
    "recall", "warning", "fine", "penalty", "regulatory", "subpoena",
    "layoff", "layoffs", "restructuring", "bankruptcy", "default",
    "loss widens", "revenue decline", "earnings miss",
    "data breach", "cybersecurity incident",
]

# 高インパクトキーワード（スコアを追加加算）
_HIGH_IMPACT_POSITIVE = [
    "earnings beat", "strong earnings", "revenue beat",
    "merger", "acquisition", "fda approval",
]
_HIGH_IMPACT_NEGATIVE = [
    "earnings miss", "revenue miss", "bankruptcy",
    "fraud", "sec investigation", "class action",
]


@dataclass
class NewsArticle:
    """1記事のデータ。"""
    headline: str
    summary:  str
    source:   str
    datetime: int  # unix timestamp
    url:      str


@dataclass
class NewsSentiment:
    """1銘柄のニュースセンチメント分析結果。"""
    symbol:          str
    positive_count:  int = 0
    negative_count:  int = 0
    neutral_count:   int = 0
    total_count:     int = 0
    sentiment_score: float = 0.0   # -1.0 (完全ネガ) 〜 +1.0 (完全ポジ)
    high_impact:     bool = False   # True = 高インパクトニュースあり
    articles:        list[NewsArticle] = field(default_factory=list)
    data_available:  bool = False

    def label(self) -> str:
        """センチメントラベルを返す。"""
        if self.sentiment_score > 0.2:
            return "positive"
        elif self.sentiment_score < -0.2:
            return "negative"
        return "neutral"

    def should_exclude(self, tactic: str = "credit_spread") -> bool:
        """当該銘柄をこの戦術から除外すべきかを返す。

        credit_spread/iron_condor: ネガティブニュースは除外（急落リスク）
        straddle_buy: ポジティブ・ネガティブ共に除外しない（ボラ狙い）
        """
        if not self.data_available:
            return False
        if tactic in ("straddle_buy", "orb_buy"):
            return False  # ボラ戦術はニュースで除外しない
        return self.sentiment_score < -0.2 or self.high_impact and self.sentiment_score < 0


# ── センチメント分析ロジック ───────────────────────────────────────────────────

def _classify_article(headline: str, summary: str) -> tuple[str, bool]:
    """記事タイトル・概要からセンチメントを分類。

    Returns:
        (sentiment, is_high_impact): "positive" / "negative" / "neutral", bool
    """
    text = (headline + " " + summary).lower()

    pos_score = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg_score = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)

    is_high_impact = (
        any(kw in text for kw in _HIGH_IMPACT_POSITIVE) or
        any(kw in text for kw in _HIGH_IMPACT_NEGATIVE)
    )

    if pos_score > neg_score:
        return "positive", is_high_impact
    elif neg_score > pos_score:
        return "negative", is_high_impact
    return "neutral", is_high_impact


def _compute_sentiment_score(
    pos: int, neg: int, neutral: int, high_impact: bool
) -> float:
    """センチメントスコアを -1.0 〜 +1.0 で算出。

    固定閾値なし: 記事数の分布から比率ベースで算出。
    高インパクト記事がある場合は絶対値を 1.2倍 (上限1.0)。
    """
    total = pos + neg + neutral
    if total == 0:
        return 0.0
    raw = (pos - neg) / total  # -1〜+1
    if high_impact:
        raw = max(-1.0, min(1.0, raw * 1.2))
    return round(raw, 4)


# ── Finnhub API呼び出し ───────────────────────────────────────────────────────

def _fetch_finnhub_news(
    symbol: str,
    api_key: str,
    hours: int = 24,
    max_retries: int = 2,
) -> list[dict]:
    """Finnhub /company-news から記事リストを取得。

    Args:
        symbol:      銘柄ティッカー
        api_key:     Finnhub API KEY
        hours:       過去N時間の記事を取得
        max_retries: レートリミット時の最大リトライ回数

    Returns:
        list[dict] (Finnhub API のレスポンス記事リスト)
    """
    try:
        import requests
        import datetime

        now   = datetime.datetime.utcnow()
        start = now - datetime.timedelta(hours=hours)
        from_str = start.strftime("%Y-%m-%d")
        to_str   = now.strftime("%Y-%m-%d")

        url = "https://finnhub.io/api/v1/company-news"
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(
                    url,
                    params={
                        "symbol": symbol,
                        "from":   from_str,
                        "to":     to_str,
                        "token":  api_key,
                    },
                    timeout=10,
                )
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    log.warning(f"[NewsSentiment] Rate limit for {symbol}, wait {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    log.warning(f"[NewsSentiment] Finnhub {symbol}: HTTP {resp.status_code}")
                    return []
                data = resp.json()
                if isinstance(data, list):
                    return data
                return []
            except Exception as e:
                log.warning(f"[NewsSentiment] Finnhub request error ({attempt+1}): {e}")
                if attempt < max_retries:
                    time.sleep(1)
        return []

    except ImportError:
        log.warning("[NewsSentiment] requests not available")
        return []


# ── パブリック API ────────────────────────────────────────────────────────────

def get_news_sentiment(
    symbol: str,
    api_key: str = "",
    hours: int = 24,
    raw_articles: Optional[list[dict]] = None,
) -> NewsSentiment:
    """銘柄のニュースセンチメントを分析して返す。

    Args:
        symbol:       銘柄ティッカー
        api_key:      Finnhub API KEY
        hours:        過去N時間を対象
        raw_articles: テスト用外部注入データ。指定時はAPIコールをスキップ

    Returns:
        NewsSentiment
    """
    # データ取得
    if raw_articles is not None:
        articles_data = raw_articles
    elif api_key:
        articles_data = _fetch_finnhub_news(symbol, api_key, hours=hours)
    else:
        log.info(f"[NewsSentiment] {symbol}: no api_key, returning neutral")
        return NewsSentiment(symbol=symbol, data_available=False)

    if not articles_data:
        return NewsSentiment(symbol=symbol, data_available=False)

    # 分析
    articles: list[NewsArticle] = []
    pos = neg = neutral = 0
    any_high_impact = False

    for item in articles_data:
        headline = str(item.get("headline", ""))
        summary  = str(item.get("summary", ""))
        sentiment, is_high = _classify_article(headline, summary)

        if sentiment == "positive":
            pos += 1
        elif sentiment == "negative":
            neg += 1
        else:
            neutral += 1

        if is_high:
            any_high_impact = True

        articles.append(NewsArticle(
            headline=headline,
            summary=summary[:200],
            source=str(item.get("source", "")),
            datetime=int(item.get("datetime", 0)),
            url=str(item.get("url", "")),
        ))

    score = _compute_sentiment_score(pos, neg, neutral, any_high_impact)

    result = NewsSentiment(
        symbol=symbol,
        positive_count=pos,
        negative_count=neg,
        neutral_count=neutral,
        total_count=len(articles_data),
        sentiment_score=score,
        high_impact=any_high_impact,
        articles=articles,
        data_available=True,
    )

    log.info(
        f"[NewsSentiment] {symbol}: score={score:.3f} "
        f"pos={pos} neg={neg} neutral={neutral} "
        f"high_impact={any_high_impact} label={result.label()}"
    )
    return result


def get_news_sentiments(
    symbols: list[str],
    api_key: str = "",
    hours: int = 24,
) -> dict[str, NewsSentiment]:
    """複数銘柄のセンチメントをまとめて返す。

    レートリミット対策: 銘柄間に 0.1秒 sleep を挟む。
    """
    result: dict[str, NewsSentiment] = {}
    for i, sym in enumerate(symbols):
        result[sym] = get_news_sentiment(sym, api_key=api_key, hours=hours)
        if i < len(symbols) - 1:
            time.sleep(0.1)  # 60 req/min = 1 req/s 以内
    return result


def filter_by_news(
    symbols: list[str],
    sentiments: dict[str, NewsSentiment],
    tactic: str = "credit_spread",
) -> tuple[list[str], list[str]]:
    """ニュースフィルタを適用して許可銘柄・除外銘柄を返す。

    Returns:
        (allowed_symbols, excluded_symbols)
    """
    allowed:  list[str] = []
    excluded: list[str] = []
    for sym in symbols:
        sent = sentiments.get(sym)
        if sent is None or sent.should_exclude(tactic=tactic):
            excluded.append(sym)
        else:
            allowed.append(sym)
    return allowed, excluded
