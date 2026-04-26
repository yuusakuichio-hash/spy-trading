"""common_v3/notify/multi_channel_sender.py — 3 段 fallback 通知送信器

概要:
  Pushover の月間 10000 メッセージ上限到達 (HTTP 429 または monthly_exceeded)
  に対応するため、Pushover → Slack webhook → Gmail SMTP の順で自動 fallback する。

設計方針:
  - common/pushover_client.py は schg lock で書換禁止。このモジュールから参照のみ。
  - Pushover が 429 / monthly_exceeded を返したら Slack へ fallback する。
  - Slack が失敗したら Gmail SMTP へ fallback する。
  - 各チャンネルは独立 try/except — 1チャンネル失敗でも次チャンネルへ継続する。
  - graceful degradation: 全チャンネル未設定でも例外を上げず FallbackResult を返す。

env 設定 (common_v3/notify/env_schema.py に定義):
  SLACK_WEBHOOK_URL      — Slack Incoming Webhook URL
  GMAIL_APP_PASSWORD     — Gmail アプリパスワード (SMTP 認証用)
  GMAIL_FROM             — 送信元 Gmail アドレス (デフォルト: yuusakuichio@gmail.com)
  GMAIL_TO               — 送信先アドレス (デフォルト: GMAIL_FROM と同じ)
  PUSHOVER_OPS_TOKEN     — Pushover アプリトークン (common/pushover_client.py 共通)
  PUSHOVER_USER          — Pushover ユーザーキー (common/pushover_client.py 共通)

使用例:
    from common_v3.notify.multi_channel_sender import send_with_fallback

    result = send_with_fallback(
        title="[Atlas] kill switch 発動",
        message="LOSS_3PCT トリガー: 緊急停止",
        priority=2,
    )
    # result.delivered_by -> "pushover" | "slack" | "gmail" | None
    # result.ok           -> bool

公開 API:
    send_with_fallback(title, message, priority, *, token, app_tag) -> FallbackResult
    FallbackResult: dataclass (ok, delivered_by, attempted, errors)
    MonthlyExceededError: Pushover 上限到達を示す例外 (テスト用)

Python バージョン: 3.10+
"""
from __future__ import annotations

import logging
import os
import smtplib
import sys
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] multi_channel_sender: %(message)s")
    )
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# プロジェクトルートを sys.path に追加 (直接実行 / テスト環境)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore

# ── 定数 ─────────────────────────────────────────────────────────────────────
_SLACK_TIMEOUT_SEC: int = 10
_GMAIL_TIMEOUT_SEC: int = 15
_PUSHOVER_TIMEOUT_SEC: int = 10

# Pushover が「月間上限超過」を示す際のレスポンスボディに含まれるキーワード
_MONTHLY_EXCEEDED_KEYWORDS: tuple[str, ...] = (
    "monthly_exceeded",
    "monthly limit exceeded",
    "app limit exceeded",
)


# ── 公開 例外 ─────────────────────────────────────────────────────────────────

class MonthlyExceededError(Exception):
    """Pushover 月間 10000 メッセージ上限到達を示す例外。

    テストおよび外部コードからこの例外を受け取って fallback 経路を判断できる。
    """


# ── 結果 dataclass ────────────────────────────────────────────────────────────

@dataclass
class FallbackResult:
    """send_with_fallback() の戻り値。

    Attributes:
        ok:           いずれかのチャンネルで送信成功した場合 True
        delivered_by: 成功したチャンネル名 ("pushover" | "slack" | "gmail" | None)
        attempted:    試行したチャンネル名のリスト (順序保証)
        errors:       各チャンネルの失敗理由 {"channel": "reason"}
    """
    ok: bool
    delivered_by: Optional[str]
    attempted: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


# ── 内部: Pushover 送信 ───────────────────────────────────────────────────────

def _pushover_send(
    title: str,
    message: str,
    priority: int,
    token: Optional[str],
) -> tuple[bool, bool]:
    """Pushover へ直接 HTTP POST する。

    Returns:
        (success: bool, is_rate_limited_or_monthly_exceeded: bool)

    common/pushover_client.py の _http_post() と同等だが、
    monthly_exceeded の検出を追加している。
    """
    _token = token or os.environ.get("PUSHOVER_OPS_TOKEN") or os.environ.get("PUSHOVER_TOKEN") or ""
    _user = os.environ.get("PUSHOVER_USER", "")

    if not _token or not _user:
        log.warning("[pushover] token or user not set — skip")
        return False, False

    if _requests is None:
        log.warning("[pushover] requests library not available")
        return False, False

    try:
        data: dict = {
            "token":    _token,
            "user":     _user,
            "title":    title,
            "message":  message[:1024],
            "priority": priority,
        }
        if priority >= 2:
            data["retry"]  = 30
            data["expire"] = 3600

        resp = _requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=_PUSHOVER_TIMEOUT_SEC,
        )

        body = resp.text or ""
        body_lower = body.lower()

        # 月間上限超過の検出 (429 に加えて本文チェック)
        if resp.status_code == 429 or any(kw in body_lower for kw in _MONTHLY_EXCEEDED_KEYWORDS):
            log.warning("[pushover] rate_limited/monthly_exceeded status=%s body=%s",
                        resp.status_code, body[:200])
            return False, True

        if "banned" in body_lower:
            log.warning("[pushover] banned detected: %s", body[:200])
            return False, True

        if resp.ok:
            log.info("[pushover] sent ok: %s", title[:60])
            return True, False

        log.warning("[pushover] error status=%s body=%s", resp.status_code, body[:200])
        return False, False

    except Exception as exc:
        log.warning("[pushover] exception: %s", exc)
        return False, False


# ── 内部: Slack 送信 ──────────────────────────────────────────────────────────

def _slack_send(title: str, message: str, priority: int) -> bool:
    """Slack Incoming Webhook へ送信する。

    SLACK_WEBHOOK_URL 未設定なら graceful skip (False を返す)。

    Args:
        title:    通知タイトル
        message:  通知本文
        priority: Pushover priority 準拠 (-2〜2)。2 なら :rotating_light: prefix を付ける。

    Returns:
        True: 送信成功 / False: スキップまたは失敗
    """
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        log.debug("[slack] SLACK_WEBHOOK_URL not set — skip")
        return False

    if _requests is None:
        log.warning("[slack] requests library not available — skip")
        return False

    try:
        prefix = ":rotating_light: " if priority >= 2 else ""
        text = f"{prefix}*{title}*\n{message[:2000]}"
        payload = {"text": text}
        resp = _requests.post(
            webhook_url,
            json=payload,
            timeout=_SLACK_TIMEOUT_SEC,
        )
        # Slack Incoming Webhook は成功時 200 + "ok" を返す
        if resp.ok:
            log.info("[slack] sent ok: %s", title[:60])
            return True
        log.warning("[slack] failed status=%s body=%s", resp.status_code, resp.text[:200])
        return False

    except Exception as exc:
        log.warning("[slack] exception: %s", exc)
        return False


# ── 内部: Gmail SMTP 送信 ─────────────────────────────────────────────────────

def _gmail_send(title: str, message: str) -> bool:
    """Gmail SMTP 経由でメール送信する。

    GMAIL_APP_PASSWORD 未設定なら graceful skip (False を返す)。

    Args:
        title:   通知タイトル (Subject に使用)
        message: 通知本文

    Returns:
        True: 送信成功 / False: スキップまたは失敗
    """
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not app_password:
        log.debug("[gmail] GMAIL_APP_PASSWORD not set — skip")
        return False

    gmail_from = os.environ.get("GMAIL_FROM", "yuusakuichio@gmail.com")
    gmail_to = os.environ.get("GMAIL_TO", gmail_from)

    try:
        msg = MIMEText(message[:4096], "plain", "utf-8")
        msg["Subject"] = f"[Sora Lab Fallback] {title}"
        msg["From"] = gmail_from
        msg["To"] = gmail_to

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=_GMAIL_TIMEOUT_SEC) as smtp:
            smtp.login(gmail_from, app_password)
            smtp.sendmail(gmail_from, [gmail_to], msg.as_string())

        log.info("[gmail] sent ok: %s", title[:60])
        return True

    except Exception as exc:
        log.warning("[gmail] exception: %s", exc)
        return False


# ── 公開 API ──────────────────────────────────────────────────────────────────

def send_with_fallback(
    title: str,
    message: str,
    priority: int = 0,
    *,
    token: Optional[str] = None,
    app_tag: str = "SYS",
) -> FallbackResult:
    """Pushover → Slack → Gmail の 3 段 fallback で通知を送信する。

    fallback トリガー条件:
      - Pushover が 429 (rate limit) を返した場合
      - Pushover レスポンス本文に monthly_exceeded / app limit exceeded が含まれる場合
      - Pushover トークン/ユーザーキーが未設定の場合
      - Pushover 送信で例外が発生した場合

    各チャンネルは独立 try/except — 上位チャンネルが失敗しても必ず次を試みる。
    全チャンネル失敗時は ok=False / delivered_by=None の FallbackResult を返す
    (例外は上げない)。

    Args:
        title:    通知タイトル
        message:  通知本文
        priority: Pushover priority 準拠 (-2〜2)
        token:    Pushover トークン (省略時は PUSHOVER_OPS_TOKEN 環境変数)
        app_tag:  ログ識別タグ (例: "Atlas", "Chronos", "SYS")

    Returns:
        FallbackResult:
            ok:           いずれかのチャンネルで成功した場合 True
            delivered_by: 成功したチャンネル名 or None
            attempted:    試行チャンネルリスト
            errors:       失敗チャンネルの理由 dict
    """
    result = FallbackResult(ok=False, delivered_by=None)

    # ── Step 1: Pushover ─────────────────────────────────────────────────────
    try:
        result.attempted.append("pushover")
        success, is_rate_limited = _pushover_send(title, message, priority, token)

        if success:
            log.info("[fallback] app_tag=%s delivered_by=pushover title=%s",
                     app_tag, title[:60])
            result.ok = True
            result.delivered_by = "pushover"
            return result

        if is_rate_limited:
            reason = "429_or_monthly_exceeded"
        else:
            reason = "send_failed"
        result.errors["pushover"] = reason
        log.info("[fallback] pushover failed reason=%s — trying slack. app_tag=%s",
                 reason, app_tag)

    except Exception as exc:
        result.errors["pushover"] = f"exception:{exc}"
        log.warning("[fallback] pushover exception: %s", exc)

    # ── Step 2: Slack ────────────────────────────────────────────────────────
    try:
        result.attempted.append("slack")
        slack_ok = _slack_send(title, message, priority)

        if slack_ok:
            log.info("[fallback] app_tag=%s delivered_by=slack title=%s",
                     app_tag, title[:60])
            result.ok = True
            result.delivered_by = "slack"
            return result

        result.errors["slack"] = "send_failed_or_not_configured"
        log.info("[fallback] slack failed — trying gmail. app_tag=%s", app_tag)

    except Exception as exc:
        result.errors["slack"] = f"exception:{exc}"
        log.warning("[fallback] slack exception: %s", exc)

    # ── Step 3: Gmail ────────────────────────────────────────────────────────
    try:
        result.attempted.append("gmail")
        gmail_ok = _gmail_send(title, message)

        if gmail_ok:
            log.info("[fallback] app_tag=%s delivered_by=gmail title=%s",
                     app_tag, title[:60])
            result.ok = True
            result.delivered_by = "gmail"
            return result

        result.errors["gmail"] = "send_failed_or_not_configured"
        log.warning("[fallback] all channels failed. app_tag=%s title=%s",
                    app_tag, title[:60])

    except Exception as exc:
        result.errors["gmail"] = f"exception:{exc}"
        log.warning("[fallback] gmail exception: %s", exc)

    return result


def channel_status() -> dict[str, bool]:
    """各チャンネルの設定状態を返す (デバッグ・診断用)。

    Returns:
        dict: {"pushover": bool, "slack": bool, "gmail": bool}
              True = 必要な env var が設定されている
    """
    pushover_ok = bool(
        (os.environ.get("PUSHOVER_OPS_TOKEN") or os.environ.get("PUSHOVER_TOKEN"))
        and os.environ.get("PUSHOVER_USER")
    )
    slack_ok = bool(os.environ.get("SLACK_WEBHOOK_URL"))
    gmail_ok = bool(os.environ.get("GMAIL_APP_PASSWORD"))
    return {
        "pushover": pushover_ok,
        "slack": slack_ok,
        "gmail": gmail_ok,
    }
