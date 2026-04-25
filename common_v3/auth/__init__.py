"""common_v3.auth — 認証情報集約管理 (本実装)

Public API:
- get_credential(name, default=None): 統一窓口で API key 取得
- Credentials: 全 API key を保持する frozen dataclass
- get_credentials(): singleton accessor

統一管理対象:
- moomoo_app_id / moomoo_app_secret
- pushover_token / pushover_user_key / pushover_alert_token / pushover_ops_token / pushover_report_token
- finnhub_api_key
- gemini_api_key / openai_api_key / anthropic_api_key
- x_api_key / x_api_secret / x_access_token / x_access_token_secret
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Credentials:
    """全 API key を保持する frozen dataclass (起動時 1 回 load).

    全 field は str (環境変数経由・未設定は空文字)。
    """
    # moomoo
    moomoo_app_id: str = ""
    moomoo_app_secret: str = ""

    # Pushover
    pushover_token: str = ""
    pushover_user_key: str = ""
    pushover_alert_token: str = ""
    pushover_ops_token: str = ""
    pushover_report_token: str = ""

    # Market data
    finnhub_api_key: str = ""

    # LLM
    gemini_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # X (Twitter)
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""


_cached: Optional[Credentials] = None


def _load_from_env() -> Credentials:
    """環境変数から Credentials を構築."""
    return Credentials(
        moomoo_app_id=os.environ.get("MOOMOO_APP_ID", ""),
        moomoo_app_secret=os.environ.get("MOOMOO_APP_SECRET", ""),
        pushover_token=os.environ.get("PUSHOVER_TOKEN", ""),
        pushover_user_key=os.environ.get("PUSHOVER_USER_KEY", ""),
        pushover_alert_token=os.environ.get("PUSHOVER_ALERT_TOKEN", ""),
        pushover_ops_token=os.environ.get("PUSHOVER_OPS_TOKEN", ""),
        pushover_report_token=os.environ.get("PUSHOVER_REPORT_TOKEN", ""),
        finnhub_api_key=os.environ.get("FINNHUB_API_KEY", ""),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        x_api_key=os.environ.get("X_API_KEY", ""),
        x_api_secret=os.environ.get("X_API_SECRET", ""),
        x_access_token=os.environ.get("X_ACCESS_TOKEN", ""),
        x_access_token_secret=os.environ.get("X_ACCESS_TOKEN_SECRET", ""),
    )


def get_credentials() -> Credentials:
    """singleton accessor (テストでは reset_credentials_cache() でリセット可)."""
    global _cached
    if _cached is None:
        _cached = _load_from_env()
    return _cached


def reset_credentials_cache() -> None:
    """テスト用: cache をリセットして次回 get_credentials() で再 load させる."""
    global _cached
    _cached = None


def get_credential(name: str, default: str = "") -> str:
    """名前で credential を取得 (例: "moomoo_app_id")."""
    creds = get_credentials()
    return getattr(creds, name, default)


__all__ = [
    "Credentials",
    "get_credentials",
    "reset_credentials_cache",
    "get_credential",
]
