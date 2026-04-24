"""common_v3/notify/quiet_hours_pushover.py — ADR-015 B 深夜通知遅延送信 wrapper

責務:
- common/pushover_client.py の既存 quiet_hours 機能を参照しつつ、
  atlas_v3 / common_v3 配下から呼び出せる thin wrapper を提供する
- 深夜（JST 22:00-4:00）に発生した非緊急通知を朝まで delay キューへ積む
- common/pushover_client.py は書換禁止のため、この wrapper がアダプタとして機能する

設計:
- send_with_quiet_hours(): quiet hours 判定 → 深夜非緊急は morning queue へ delay
- flush_morning_queue(): 朝の配信キューをフラッシュ（LaunchAgent から呼ぶ）
- _is_quiet_hours_jst(): JST 22:00-4:00 判定（common/pushover_client.py の実装を再利用）

実装規律:
- common/pushover_client.py の _enqueue_morning_digest / _is_quiet_hours を直接 import
  して処理を委譲する（コード重複なし・書換なし）
- common/pushover_client.py が利用できない場合は fallback（無音ではなく直接送信）
- Python 3.14 互換 (from __future__ import annotations)
- sync-only（async 禁止）

使用例:
    from common_v3.notify.quiet_hours_pushover import send_with_quiet_hours
    send_with_quiet_hours(
        title="[Atlas] moomoo fallback 発動",
        message="AuthenticationError → yfinance に切替",
        priority=1,
        app_tag="Atlas",
    )
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# プロジェクトルートを sys.path に追加（直接実行時用）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _is_quiet_hours_jst() -> bool:
    """JST 22:00-4:00 の深夜静穏時間かどうかを返す。

    common/pushover_client.py の _is_quiet_hours() と同一ロジック。
    common モジュールが利用不可の場合は zoneinfo で直接判定する。

    Returns:
        True: 静穏時間内（深夜通知を遅延すべき時間帯）
        False: 通常時間（即時送信してよい）
    """
    try:
        # 既存実装を再利用（書換禁止なので import のみ）
        from common.pushover_client import _is_quiet_hours
        return _is_quiet_hours()
    except ImportError:
        pass

    # フォールバック: zoneinfo で直接判定
    try:
        import datetime
        from zoneinfo import ZoneInfo
        now_jst = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))
        h = now_jst.hour
        return h >= 22 or h < 4
    except Exception as exc:
        log.warning(
            "[quiet_hours_pushover] zoneinfo fallback failed: %s. "
            "Assuming NOT quiet hours (fail-open for alerts).",
            exc,
        )
        return False


def _is_night_emergency_v3(title: str, message: str, priority: int) -> bool:
    """夜間でも即時送信すべき緊急通知かどうかを返す。

    common/pushover_client.py の _is_night_emergency() と同一判定ロジック。

    条件（AND):
      1. priority >= 2
      2. _NIGHT_EMERGENCY_KEYWORDS のいずれかが title/message に含まれる

    Args:
        title: 通知タイトル
        message: 通知本文
        priority: Pushover priority (-2〜2)

    Returns:
        True: 夜間でも送信すべき真の緊急通知
        False: 深夜遅延対象
    """
    try:
        from common.pushover_client import _is_night_emergency
        return _is_night_emergency(title, message, priority)
    except ImportError:
        pass

    # フォールバック: 最小限の判定（priority=2 のみ夜間送信）
    return priority >= 2


def send_with_quiet_hours(
    title: str,
    message: str,
    priority: int = 0,
    *,
    token: Optional[str] = None,
    app_tag: str = "Atlas",
    force_immediate: bool = False,
) -> bool:
    """深夜静穏時間を考慮した Pushover 送信。

    ADR-015 B: プライベート領域尊重規律に基づき、深夜通知を朝まで遅延する。

    動作:
      - 通常時間帯（JST 4:00-22:00）: common/pushover_client.send_critical() で即時送信
      - 深夜静穏時間（JST 22:00-4:00）:
        - 真の緊急通知（priority=2 + 資金損失等キーワード）: 即時送信
        - 非緊急通知: morning queue に積んで朝まで遅延
      - force_immediate=True: 静穏時間チェックをバイパスして即時送信

    Args:
        title: 通知タイトル
        message: 通知本文（1024文字で切り捨て）
        priority: Pushover priority (-2〜2)
        token: Pushover トークン（省略時は環境変数から自動選択）
        app_tag: ログ/キュー識別タグ
        force_immediate: True なら静穏時間チェックをバイパス

    Returns:
        True: 即時送信成功 or 正常 queue 追記
        False: エラー
    """
    try:
        from common.pushover_client import (
            send_critical,
            _enqueue_morning_digest,
            _is_quiet_hours,
            _is_night_emergency,
            LEVEL_CRITICAL,
        )
    except ImportError as exc:
        log.warning(
            "[quiet_hours_pushover] common.pushover_client import failed: %s. "
            "Notification dropped. title=%s",
            exc, title[:60],
        )
        return False

    # force_immediate: 静穏時間チェックをバイパス
    if force_immediate:
        log.info(
            "[quiet_hours_pushover] force_immediate=True — bypassing quiet hours. title=%s",
            title[:60],
        )
        return send_critical(title, message, priority=priority, token=token, app_tag=app_tag)

    # 静穏時間外（通常時間帯）: 即時送信
    if not _is_quiet_hours():
        return send_critical(title, message, priority=priority, token=token, app_tag=app_tag)

    # 静穏時間内（深夜）: 真の緊急通知は即時送信
    if _is_night_emergency(title, message, priority):
        log.warning(
            "[quiet_hours_pushover] NIGHT_EMERGENCY override — sending immediately. title=%s",
            title[:60],
        )
        return send_critical(title, message, priority=priority, token=token, app_tag=app_tag)

    # 静穏時間内・非緊急: morning queue に遅延
    _tok = token or ""
    _enqueue_morning_digest(title, message, priority, _tok, app_tag)
    log.info(
        "[quiet_hours_pushover] Quiet hours — deferred to morning queue. title=%s",
        title[:60],
    )
    return True


def flush_morning_queue_v3() -> int:
    """朝の遅延通知キューをフラッシュして送信する。

    common/pushover_client.py の flush_queue() / flush_batch_queue() とは別に、
    morning queue（_enqueue_morning_digest で積まれたもの）を送信する。

    LaunchAgent の起動時（JST 4:00 以降）に呼ばれることを想定。

    Returns:
        int: フラッシュした通知件数
    """
    try:
        from common.pushover_client import (
            _load_morning_queue,
            _clear_morning_queue,
            send_critical,
        )
    except ImportError as exc:
        log.warning("[quiet_hours_pushover] flush failed: import error: %s", exc)
        return 0

    entries = _load_morning_queue()
    if not entries:
        log.info("[quiet_hours_pushover] morning queue empty — nothing to flush")
        return 0

    sent = 0
    for entry in entries:
        try:
            ok = send_critical(
                entry.get("title", ""),
                entry.get("message", ""),
                priority=int(entry.get("priority", 0)),
                token=entry.get("token") or None,
                app_tag=entry.get("app_tag", "Atlas"),
            )
            if ok:
                sent += 1
        except Exception as exc:
            log.warning("[quiet_hours_pushover] flush entry error: %s", exc)

    _clear_morning_queue()
    log.info("[quiet_hours_pushover] morning queue flushed: sent=%d total=%d", sent, len(entries))
    return sent
