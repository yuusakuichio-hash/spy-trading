#!/usr/bin/env python3
"""
common/external_health_ping.py — Healthchecks.io 外部死活監視 ping 送信モジュール

背景:
    2026-04-20 Pushover IP ban 事案で全通知経路が死亡した。
    本モジュールは Pushover とは完全独立した経路（別認証・別ベンダー）で
    Healthchecks.io に死活情報を送信する。Tier 2 保険として機能する。

対象コンポーネント:
    - chronos_agent   (Chronos: CME先物Bot エージェント)
    - chronos_watchdog (Chronos: CME先物Bot 最後の砦)
    - atlas_agent     (Atlas: SPXオプションBot エージェント)
    - atlas_watchdog  (Atlas: SPXオプションBot 最後の砦)
    - sora_heartbeat_monitor (内部heartbeat監視デーモン)
    + 今後追加される全Bot

設計原則:
    - Pushover経路と完全独立（別認証・別ネットワーク経路・別ベンダー）
    - ping失敗でもコンポーネント本業は継続（監視は最終保険）
    - UUID未設定コンポーネントは warning のみ・処理継続
    - retry 5回・timeout 10秒・exponential backoff

UUID設定方法 (.env):
    HC_UUID_CHRONOS_AGENT=<healthchecks.io チェック UUID>
    HC_UUID_CHRONOS_WATCHDOG=<healthchecks.io チェック UUID>
    HC_UUID_ATLAS_AGENT=<healthchecks.io チェック UUID>
    HC_UUID_ATLAS_WATCHDOG=<healthchecks.io チェック UUID>
    HC_UUID_SORA_HEARTBEAT_MONITOR=<healthchecks.io チェック UUID>
    HC_UUID_HEALTH_AGGREGATOR=<healthchecks.io チェック UUID>

API:
    from common.external_health_ping import ping_healthchecks

    # 成功報告（メインループ正常完了時）
    ping_healthchecks("chronos_agent")

    # 開始報告（Bot起動時）
    ping_healthchecks("chronos_agent", status="start")

    # 失敗報告（致命エラー時）
    ping_healthchecks("chronos_agent", status="fail")

注意:
    Atlas と Chronos は完全独立したBot。
    Atlas = SPXオプション (spy_bot.py / atlas_agent.py / atlas_watchdog.py)
    Chronos = CME先物 (chronos_bot.py / chronos_agent.py / chronos_watchdog.py)
    コンポーネント名を混同しないこと。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Literal

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _requests = None  # type: ignore[assignment]
    _REQUESTS_OK = False

log = logging.getLogger("external_health_ping")
if not log.handlers:
    import sys
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] ext_health_ping: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# ── 定数 ─────────────────────────────────────────────────────────────────────
_HC_BASE_URL = "https://hc-ping.com"
_TIMEOUT_SEC = 10
_MAX_RETRIES = 5
_RETRY_BASE_SEC = 1.0  # exponential backoff の底

# コンポーネント名 → 環境変数キーのマッピング
# 新しいBotを追加する場合はここに追記するだけでよい
_COMPONENT_ENV_MAP: dict[str, str] = {
    # Chronos: CME先物Bot 系（Atlas と混同禁止）
    "chronos_agent":           "HC_UUID_CHRONOS_AGENT",
    "chronos_watchdog":        "HC_UUID_CHRONOS_WATCHDOG",
    "chronos_bot":             "HC_UUID_CHRONOS_BOT",
    # Atlas: SPXオプションBot 系（Chronos と混同禁止）
    "atlas_agent":             "HC_UUID_ATLAS_AGENT",
    "atlas_watchdog":          "HC_UUID_ATLAS_WATCHDOG",
    "spy_bot":                 "HC_UUID_SPY_BOT",
    # 共通監視インフラ
    "sora_heartbeat_monitor":  "HC_UUID_SORA_HEARTBEAT_MONITOR",
    "health_aggregator":       "HC_UUID_HEALTH_AGGREGATOR",
}


def _load_env_file() -> None:
    """プロジェクトルートの .env を os.environ に読み込む（未設定キーのみ）。"""
    candidates = [
        Path("/root/spxbot/.env"),
        Path(__file__).parent.parent / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
            break


def _get_uuid(component: str) -> str | None:
    """コンポーネント名から Healthchecks.io UUID を取得する。

    未設定の場合は None を返す（warning は呼び出し元が出す）。
    """
    env_key = _COMPONENT_ENV_MAP.get(component)
    if env_key is None:
        # 未知コンポーネントは env_key を動的生成して試みる
        env_key = "HC_UUID_" + component.upper().replace("-", "_")
    return os.environ.get(env_key) or None


def _build_url(uuid: str, status: Literal["success", "fail", "start"]) -> str:
    """Healthchecks.io ping URL を構築する。

    - success: /uuid
    - fail:    /uuid/fail
    - start:   /uuid/start
    """
    base = f"{_HC_BASE_URL}/{uuid}"
    if status == "fail":
        return f"{base}/fail"
    if status == "start":
        return f"{base}/start"
    return base


def ping_healthchecks(
    component: str,
    status: Literal["success", "fail", "start"] = "success",
    payload: str | None = None,
) -> bool:
    """Healthchecks.io に ping を送信する。

    Parameters
    ----------
    component:
        コンポーネント識別子。例: "chronos_agent", "atlas_watchdog"
        コンポーネント名はBotシステムと対応させること:
          - Chronos系: "chronos_agent", "chronos_watchdog", "chronos_bot"
          - Atlas系:   "atlas_agent",   "atlas_watchdog",   "spy_bot"
    status:
        "success" (デフォルト) / "fail" / "start"
    payload:
        ping に添付するテキスト（任意・最大10KB）。エラーメッセージ等に使用。

    Returns
    -------
    bool:
        True = 送信成功 / False = 全リトライ失敗またはUUID未設定
    """
    if not _REQUESTS_OK:
        log.warning("[%s] requests library not available. ping skipped.", component)
        return False

    # .env ロード（初回のみ有効・setdefaultで上書きしない）
    _load_env_file()

    uuid = _get_uuid(component)
    if not uuid:
        env_key = _COMPONENT_ENV_MAP.get(component, f"HC_UUID_{component.upper()}")
        log.warning(
            "[%s] UUID not configured (env: %s). "
            "Set it in .env to enable Healthchecks.io monitoring.",
            component,
            env_key,
        )
        return False

    url = _build_url(uuid, status)

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            kwargs: dict = {"timeout": _TIMEOUT_SEC}
            if payload:
                kwargs["data"] = payload[:10240].encode("utf-8")
                resp = _requests.post(url, **kwargs)  # type: ignore[union-attr]
            else:
                resp = _requests.get(url, **kwargs)  # type: ignore[union-attr]

            if resp.status_code == 200:
                log.debug("[%s] ping OK (status=%s, attempt=%d)", component, status, attempt)
                return True

            log.warning(
                "[%s] ping HTTP %d (status=%s, attempt=%d/%d)",
                component,
                resp.status_code,
                status,
                attempt,
                _MAX_RETRIES,
            )

        except Exception as exc:
            log.warning(
                "[%s] ping exception (status=%s, attempt=%d/%d): %s",
                component,
                status,
                attempt,
                _MAX_RETRIES,
                exc,
            )

        if attempt < _MAX_RETRIES:
            sleep_sec = _RETRY_BASE_SEC * (2 ** (attempt - 1))
            time.sleep(sleep_sec)

    log.error(
        "[%s] ping FAILED after %d retries (status=%s)",
        component,
        _MAX_RETRIES,
        status,
    )
    return False


def list_configured_components() -> dict[str, bool]:
    """全コンポーネントのUUID設定状況を返す。

    Returns
    -------
    dict[component_name, is_configured]
    """
    _load_env_file()
    return {
        comp: bool(_get_uuid(comp))
        for comp in _COMPONENT_ENV_MAP
    }


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Healthchecks.io ping テスト送信")
    parser.add_argument("component", nargs="?", help="コンポーネント名（例: chronos_agent）")
    parser.add_argument(
        "--status",
        choices=["success", "fail", "start"],
        default="success",
        help="ping ステータス (default: success)",
    )
    parser.add_argument("--list", action="store_true", help="設定済みコンポーネント一覧を表示")
    args = parser.parse_args()

    if args.list:
        status_map = list_configured_components()
        print("コンポーネント UUID 設定状況:")
        for comp, configured in status_map.items():
            mark = "OK" if configured else "未設定"
            env_key = _COMPONENT_ENV_MAP.get(comp, "?")
            print(f"  [{mark}] {comp:30s}  env: {env_key}")
        sys.exit(0)

    if not args.component:
        parser.print_help()
        sys.exit(1)

    ok = ping_healthchecks(args.component, status=args.status)
    print(f"ping {'成功' if ok else '失敗'}: component={args.component}, status={args.status}")
    sys.exit(0 if ok else 1)
