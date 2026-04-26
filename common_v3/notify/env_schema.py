"""common_v3/notify/env_schema.py — multi_channel_sender の env 設定スキーマ定義

multi_channel_sender.py が参照する環境変数の仕様を一元定義する。
本番環境では .env ファイルまたは systemd EnvironmentFile= から注入する。

必須 vs オプション:
  Pushover:
    PUSHOVER_OPS_TOKEN  (必須) — Pushover アプリトークン
    PUSHOVER_USER       (必須) — Pushover ユーザーキー
  Slack:
    SLACK_WEBHOOK_URL   (オプション) — Slack Incoming Webhook URL
                          未設定時は Slack チャンネルをスキップする
  Gmail:
    GMAIL_APP_PASSWORD  (オプション) — Gmail アプリパスワード
                          未設定時は Gmail チャンネルをスキップする
    GMAIL_FROM          (オプション・デフォルト: yuusakuichio@gmail.com)
    GMAIL_TO            (オプション・デフォルト: GMAIL_FROM と同値)

fallback 優先順位:
  1. Pushover  (PUSHOVER_OPS_TOKEN + PUSHOVER_USER が両方設定されている場合)
  2. Slack     (SLACK_WEBHOOK_URL が設定されている場合)
  3. Gmail     (GMAIL_APP_PASSWORD が設定されている場合)

Slack Webhook URL の取得方法:
  1. Slack ワークスペース > Apps > Incoming WebHooks を追加
  2. 通知先チャンネルを選択
  3. Webhook URL をコピーして SLACK_WEBHOOK_URL に設定
  URL 形式: https://hooks.slack.com/services/TXXXXXXXX/BXXXXXXXX/XXXXXXXXXXXXXXXXXXXXXXXX

Gmail アプリパスワードの取得方法:
  1. Google アカウント > セキュリティ > 2段階認証を有効化
  2. セキュリティ > アプリパスワード > アプリ選択 > 生成
  3. 生成された 16 文字のパスワードを GMAIL_APP_PASSWORD に設定
  注意: 通常の Gmail パスワードではなくアプリパスワードを使用すること

systemd EnvironmentFile 設定例 (/etc/sora_lab/notify.env):
  PUSHOVER_OPS_TOKEN=your_pushover_app_token
  PUSHOVER_USER=your_pushover_user_key
  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
  GMAIL_FROM=yuusakuichio@gmail.com
  GMAIL_TO=yuusakuichio@gmail.com

.env ファイル設定例 (/root/spxbot/.env):
  PUSHOVER_OPS_TOKEN=your_pushover_app_token
  PUSHOVER_USER=your_pushover_user_key
  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EnvVar:
    """環境変数の仕様定義。"""
    name: str
    required: bool
    default: Optional[str]
    description: str
    example: str


# Pushover
PUSHOVER_OPS_TOKEN = EnvVar(
    name="PUSHOVER_OPS_TOKEN",
    required=True,
    default=None,
    description="Pushover アプリトークン。アカウント登録後アプリ作成で取得する。",
    example="azGDURRuFHOkHNOmwq17BM4QhPfhRu",
)

PUSHOVER_USER = EnvVar(
    name="PUSHOVER_USER",
    required=True,
    default=None,
    description="Pushover ユーザーキー。Pushover ダッシュボードで確認できる。",
    example="uQiRzpo4DXghDmr9QzzfQu",
)

# Slack
SLACK_WEBHOOK_URL = EnvVar(
    name="SLACK_WEBHOOK_URL",
    required=False,
    default=None,
    description=(
        "Slack Incoming Webhook URL。"
        "未設定の場合は Slack チャンネルをスキップして Gmail へ fallback する。"
    ),
    example="https://hooks.slack.com/services/TXXXXXXXX/BXXXXXXXX/XXXXXXXXXXXXXXXXXXXXXXXX",
)

# Gmail
GMAIL_APP_PASSWORD = EnvVar(
    name="GMAIL_APP_PASSWORD",
    required=False,
    default=None,
    description=(
        "Gmail アプリパスワード (16文字)。"
        "通常の Gmail パスワードではなく Google アカウントのアプリパスワードを使用する。"
        "未設定の場合は Gmail チャンネルをスキップする。"
    ),
    example="abcd efgh ijkl mnop",
)

GMAIL_FROM = EnvVar(
    name="GMAIL_FROM",
    required=False,
    default="yuusakuichio@gmail.com",
    description="送信元 Gmail アドレス。省略時は yuusakuichio@gmail.com を使用する。",
    example="yuusakuichio@gmail.com",
)

GMAIL_TO = EnvVar(
    name="GMAIL_TO",
    required=False,
    default=None,
    description="送信先アドレス。省略時は GMAIL_FROM と同値。",
    example="yuusakuichio@gmail.com",
)


ALL_VARS: tuple[EnvVar, ...] = (
    PUSHOVER_OPS_TOKEN,
    PUSHOVER_USER,
    SLACK_WEBHOOK_URL,
    GMAIL_APP_PASSWORD,
    GMAIL_FROM,
    GMAIL_TO,
)


def validate() -> dict[str, str]:
    """必須 env var の設定状態を検証する。

    Returns:
        dict: missing な必須変数の {name: "missing"} マッピング。
              全て設定済みなら空 dict を返す。
    """
    import os
    missing: dict[str, str] = {}
    for var in ALL_VARS:
        if var.required and not os.environ.get(var.name):
            missing[var.name] = "missing"
    return missing


def print_schema() -> None:
    """全 env var の仕様を標準出力に表示する (診断用)。"""
    import os
    print("=== common_v3/notify/multi_channel_sender env schema ===")
    for var in ALL_VARS:
        is_set = bool(os.environ.get(var.name))
        req_str = "REQUIRED" if var.required else "optional"
        set_str = "SET" if is_set else "NOT SET"
        print(f"  {var.name:<25} [{req_str:<8}] [{set_str}]")
        print(f"    {var.description}")
        print(f"    example: {var.example}")
        print()


if __name__ == "__main__":
    print_schema()
    issues = validate()
    if issues:
        print(f"[ERROR] missing required vars: {list(issues.keys())}")
    else:
        print("[OK] all required vars are set")
