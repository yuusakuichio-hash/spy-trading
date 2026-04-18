"""
tests/test_news_sentiment.py — NewsSentiment モジュール テスト (12テスト)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.news_sentiment import (
    NewsSentiment, NewsArticle,
    get_news_sentiment, get_news_sentiments, filter_by_news,
    _classify_article, _compute_sentiment_score,
)


def _make_article(headline: str, summary: str = "") -> dict:
    """テスト用記事データを生成。"""
    return {
        "headline": headline,
        "summary": summary,
        "source": "TestNews",
        "datetime": 1713100000,
        "url": "https://example.com",
    }


def test_classify_positive_earnings_beat():
    """決算サプライズ（beat）はポジティブに分類される。"""
    sentiment, high_impact = _classify_article(
        "AAPL beats Q2 earnings estimates, shares surge",
        "Apple reported earnings beat for the quarter"
    )
    assert sentiment == "positive"


def test_classify_negative_earnings_miss():
    """決算ミスはネガティブに分類される。"""
    sentiment, high_impact = _classify_article(
        "TSLA misses revenue estimates by 15%",
        "Tesla reported a significant earnings miss"
    )
    assert sentiment == "negative"


def test_classify_negative_lawsuit():
    """訴訟ニュースはネガティブに分類される。"""
    sentiment, _ = _classify_article(
        "SEC investigation into accounting practices",
        "The company faces a regulatory probe and potential fine"
    )
    assert sentiment == "negative"


def test_classify_positive_merger():
    """M&Aニュースはポジティブ・高インパクトに分類される。"""
    sentiment, high_impact = _classify_article(
        "Company agrees to acquisition deal at 30% premium",
        "Merger agreement signed"
    )
    assert sentiment == "positive"
    assert high_impact is True


def test_classify_neutral_irrelevant():
    """無関係なニュースはneutralに分類される。"""
    sentiment, _ = _classify_article(
        "Company announces new office location in Denver",
        "The new office will open next quarter"
    )
    assert sentiment == "neutral"


def test_compute_sentiment_score_positive():
    """ポジティブ記事が多い場合、スコアが正の値になる。"""
    score = _compute_sentiment_score(pos=8, neg=2, neutral=5, high_impact=False)
    assert score > 0.0


def test_compute_sentiment_score_negative():
    """ネガティブ記事が多い場合、スコアが負の値になる。"""
    score = _compute_sentiment_score(pos=1, neg=9, neutral=3, high_impact=False)
    assert score < 0.0


def test_compute_sentiment_score_high_impact_amplification():
    """高インパクトニュースでスコアが増幅される。"""
    score_normal = _compute_sentiment_score(pos=7, neg=2, neutral=1, high_impact=False)
    score_high   = _compute_sentiment_score(pos=7, neg=2, neutral=1, high_impact=True)
    assert score_high >= score_normal
    assert -1.0 <= score_high <= 1.0


def test_compute_sentiment_score_zero_total():
    """記事ゼロの場合はスコアが0.0。"""
    score = _compute_sentiment_score(pos=0, neg=0, neutral=0, high_impact=False)
    assert score == 0.0


def test_get_news_sentiment_with_injected_data():
    """外部注入データでセンチメント分析が正しく動作する。"""
    articles = [
        _make_article("NVDA beats earnings, raises guidance"),
        _make_article("NVDA shares upgrade to strong buy"),
        _make_article("NVDA announces new AI chip partnership"),
        _make_article("General market update for tech sector"),
    ]
    result = get_news_sentiment("NVDA", raw_articles=articles)
    assert isinstance(result, NewsSentiment)
    assert result.symbol == "NVDA"
    assert result.total_count == 4
    assert result.data_available is True
    assert result.sentiment_score > 0  # ポジティブ優勢


def test_get_news_sentiment_no_api_key():
    """APIキーなし・外部データなし → neutral で返る。"""
    result = get_news_sentiment("SPY", api_key="")
    assert result.data_available is False
    assert result.sentiment_score == 0.0


def test_filter_by_news_excludes_negative():
    """ネガティブ銘柄がcredit_spreadから除外される。"""
    sentiments = {
        "AAPL": NewsSentiment("AAPL", sentiment_score=0.5, data_available=True),
        "TSLA": NewsSentiment("TSLA", sentiment_score=-0.5, data_available=True,
                              negative_count=5, positive_count=1, total_count=6),
        "NVDA": NewsSentiment("NVDA", sentiment_score=0.3, data_available=True),
    }
    allowed, excluded = filter_by_news(
        ["AAPL", "TSLA", "NVDA"], sentiments, tactic="credit_spread"
    )
    assert "TSLA" in excluded
    assert "AAPL" in allowed
    assert "NVDA" in allowed


def test_filter_by_news_straddle_keeps_all():
    """straddle_buy はネガティブ銘柄を除外しない（ボラ狙い）。"""
    sentiments = {
        "TSLA": NewsSentiment("TSLA", sentiment_score=-0.8, data_available=True,
                              negative_count=8, positive_count=1, total_count=9),
    }
    allowed, excluded = filter_by_news(["TSLA"], sentiments, tactic="straddle_buy")
    assert "TSLA" in allowed
    assert "TSLA" not in excluded


def test_sentiment_label():
    """sentiment_score から正しいラベルが返る。"""
    pos_sent  = NewsSentiment("X", sentiment_score=0.5, data_available=True)
    neg_sent  = NewsSentiment("X", sentiment_score=-0.5, data_available=True)
    neu_sent  = NewsSentiment("X", sentiment_score=0.1, data_available=True)
    assert pos_sent.label() == "positive"
    assert neg_sent.label() == "negative"
    assert neu_sent.label() == "neutral"


if __name__ == "__main__":
    tests = [
        test_classify_positive_earnings_beat,
        test_classify_negative_earnings_miss,
        test_classify_negative_lawsuit,
        test_classify_positive_merger,
        test_classify_irrelevant := test_classify_neutral_irrelevant,
        test_compute_sentiment_score_positive,
        test_compute_sentiment_score_negative,
        test_compute_sentiment_score_high_impact_amplification,
        test_compute_sentiment_score_zero_total,
        test_get_news_sentiment_with_injected_data,
        test_get_news_sentiment_no_api_key,
        test_filter_by_news_excludes_negative,
        test_filter_by_news_straddle_keeps_all,
        test_sentiment_label,
    ]
    tests = [
        test_classify_positive_earnings_beat,
        test_classify_negative_earnings_miss,
        test_classify_negative_lawsuit,
        test_classify_positive_merger,
        test_classify_neutral_irrelevant,
        test_compute_sentiment_score_positive,
        test_compute_sentiment_score_negative,
        test_compute_sentiment_score_high_impact_amplification,
        test_compute_sentiment_score_zero_total,
        test_get_news_sentiment_with_injected_data,
        test_get_news_sentiment_no_api_key,
        test_filter_by_news_excludes_negative,
        test_filter_by_news_straddle_keeps_all,
        test_sentiment_label,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
