"""common_v3.auth — 認証情報集約管理 (β-2 配線 skeleton)

Responsibility
--------------
現状散在している API key / 認証情報を 1 箇所に集約:

- moomoo_app_id / moomoo_app_secret (現状 .env / credentials.md)
- yfinance: 不要 (anonymous)
- Tradovate: APP_ID / SECRET / 2FA TOTP (現状 credentials.md)
- Pushover: PUSHOVER_TOKEN / USER_KEY (現状 .env)
- Anthropic / OpenAI / Gemini API key (現状 .env)
- moomoo OpenD config (host / port)

## Why

現状の問題:
1. credentials.md と .env の両方に key が存在し更新時の同期忘れ
2. 各 module が `os.environ.get(...)` を直接呼ぶため漏洩確認が困難
3. paper / live で異なる token を切替える機構が個別実装
4. auth_budget (common/auth_budget.py) との連携が暗黙

これは 2017 Equifax incident (秘匿情報の管理分散による漏洩) と同型の
「秘密が散らばっていて全容把握不能」事故を防ぐ構造的対策。

## Public API (β-2 後段で実装予定)

- ``Credentials`` dataclass (frozen)
  - 全 API key を properties として提供
  - 起動時に 1 回だけ load・以降 immutable
- ``get_credentials() -> Credentials`` (singleton)
- ``rotate_token(name) -> None``  # token rotation 起動
- ``audit_log_access(name, caller) -> None``  # 誰がいつ何を読んだか記録

## How to apply

β-2 後段で:
1. ``os.environ.get("MOOMOO_APP_ID")`` 直接呼出を全面廃止
2. ``get_credentials().moomoo_app_id`` 経由に置換
3. credentials.md と .env の二重管理を .env 単一源化
4. paper/live token は ``Credentials.for_mode(mode)`` で切替

現状は skeleton。auth_budget との連携部分のみ re-export 提供。
"""

# Lazy import で循環 import 回避
__all__ = []


def __getattr__(name):
    if name == "AuthBudget":
        try:
            from common.auth_budget import AuthBudget
            return AuthBudget
        except ImportError:
            raise AttributeError(
                f"AuthBudget は common/auth_budget.py が必要 (β-2 後段で common_v3 移植)"
            )
    raise AttributeError(f"module 'common_v3.auth' has no attribute {name!r}")
