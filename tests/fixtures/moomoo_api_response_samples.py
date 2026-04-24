"""moomoo OpenD API response fixture（Redteam r8 指摘対応）。

Redteam r8 指摘:「mock の妥当性を保証する契約テストがなく、
mock 通る = 本実装 OK は同語反復」

本ファイルは実 API response の構造を固定化し、テスト mock の妥当性を
担保する reference。実際の futu-api SDK 応答を記録 or 推定した DataFrame
を返す factory 関数群。

Sprint 2 Day 2 で実 paper 接続時に、実際の応答と本 fixture を
照合して差分があれば本 fixture を更新。

References:
- futu-api docs: https://openapi.futunn.com/futu-api-doc/quote/get-basic-qot.html
- spy_bot.py:4420-4514 (read-only reference)
"""
from __future__ import annotations

from typing import Any


def accinfo_query_response_normal() -> tuple[int, Any]:
    """正常な accinfo_query 応答。

    Returns:
        (ret_code=0, DataFrame with paper account metrics)
    """
    import pandas as pd
    data = pd.DataFrame([{
        "acc_id": 12345678,
        "trd_env": "SIMULATE",
        "cash": 50000.0,
        "available_funds": 48500.0,
        "total_assets": 100000.0,
        "securities_assets": 50000.0,
        "realized_pl": 500.0,
        "unrealized_pl": -200.0,
        "max_power_short": 50000.0,
        "net_cash_power": 48500.0,
    }])
    return (0, data)


def accinfo_query_response_zero_pnl() -> tuple[int, Any]:
    """PnL ゼロの応答（新口座想定）。"""
    import pandas as pd
    data = pd.DataFrame([{
        "total_assets": 100000.0,
        "realized_pl": 0.0,
        "unrealized_pl": 0.0,
    }])
    return (0, data)


def accinfo_query_response_drawdown() -> tuple[int, Any]:
    """DD 発生中の応答（total_assets 減少）。"""
    import pandas as pd
    data = pd.DataFrame([{
        "total_assets": 85000.0,  # high_water_mark 100000 から 15% 下落想定
        "realized_pl": -5000.0,
        "unrealized_pl": -10000.0,
    }])
    return (0, data)


def accinfo_query_response_nan() -> tuple[int, Any]:
    """pandas NaN を含む応答（S-6 fix 検証用）。"""
    import pandas as pd
    data = pd.DataFrame([{
        "total_assets": 100000.0,
        "realized_pl": float("nan"),
        "unrealized_pl": -200.0,
    }])
    return (0, data)


def accinfo_query_response_401_english() -> tuple[int, str]:
    """401 Unauthorized 応答（英語）。"""
    return (-1, "401 Unauthorized")


def accinfo_query_response_auth_chinese() -> tuple[int, str]:
    """中国語 auth error 応答（S-2 fix 対応）。"""
    return (-1, "未授权访问")


def accinfo_query_response_auth_japanese() -> tuple[int, str]:
    """日本語 auth error 応答（S-2 fix 対応）。"""
    return (-1, "認証エラー")


def accinfo_query_response_empty() -> tuple[int, Any]:
    """空 DataFrame 応答（異常系）。"""
    import pandas as pd
    return (0, pd.DataFrame())


def accinfo_query_response_rate_limit() -> tuple[int, str]:
    """rate limit 応答（S-4 関連）。"""
    return (-1, "rate limit exceeded")


def get_acc_list_response_normal() -> tuple[int, Any]:
    """正常な get_acc_list 応答。"""
    import pandas as pd
    data = pd.DataFrame([{
        "acc_id": 12345678,
        "trd_env": "SIMULATE",
        "acc_type": "MARGIN",
        "security_firm": "FUTUJP",
    }])
    return (0, data)


def get_acc_list_response_empty() -> tuple[int, Any]:
    """空応答（session 期限切れ前の症状）。"""
    import pandas as pd
    return (0, pd.DataFrame())


def get_acc_list_response_401() -> tuple[int, str]:
    """get_acc_list 401 応答。"""
    return (-1, "401 Unauthorized - session expired")


# ───────────────────────────────────────────────────────
# ファイル末尾: 全 factory を dict で参照可能に（fixture 選択用）
# ───────────────────────────────────────────────────────

ACCINFO_FIXTURES = {
    "normal": accinfo_query_response_normal,
    "zero_pnl": accinfo_query_response_zero_pnl,
    "drawdown": accinfo_query_response_drawdown,
    "nan": accinfo_query_response_nan,
    "auth_401_en": accinfo_query_response_401_english,
    "auth_zh": accinfo_query_response_auth_chinese,
    "auth_ja": accinfo_query_response_auth_japanese,
    "empty": accinfo_query_response_empty,
    "rate_limit": accinfo_query_response_rate_limit,
}

ACC_LIST_FIXTURES = {
    "normal": get_acc_list_response_normal,
    "empty": get_acc_list_response_empty,
    "auth_401": get_acc_list_response_401,
}
