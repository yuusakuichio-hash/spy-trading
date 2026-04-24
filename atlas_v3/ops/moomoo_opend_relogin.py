"""atlas_v3/ops/moomoo_opend_relogin.py — 案 F 実装 Step 2/5

moomoo OpenD の preemptive relogin daemon。12h 周期で OpenD Operation Command
(telnet 127.0.0.1:22222) に `relogin -login_pwd_md5=<hex>` を送信し、OpenD を
無停止で認証 refresh する。

設計根拠:
- 公式 doc: https://openapi.moomoo.com/moomoo-api-doc/en/opend/opend-operate.html
- 外部 relogin は「同一アカウントのみ・OpenD 再起動不要・既存 session 維持」
- py-futu-api は socket 再接続時に unlock_trade 自動再発行するため、独自 retry 層は有害

前提:
- Keychain に credential 登録済（scripts/setup_moomoo_keychain.sh で初回登録）
- OpenD 本体は launchd で常駐 (com.soralab.opend)
- このプロセス自身は launchd で 12h 周期起動 (com.soralab.moomoo-opend-relogin)

規律:
- credential を ps aux / log / file に一切書かない
- MD5 化後の hex も log に出さない
- auth_budget (max=3/24h) 内で動作
- failure 時は Pushover priority=1 で即エスカレーション
- heartbeat を data/state_v3/opend_relogin_heartbeat.jsonl に書き込む
  (Sentinel が監視して 25h 以上 heartbeat なければ andon 発火)

規律違反チェック:
- 22-06 JST の深夜失敗時は Pushover priority=2 を使い quiet_hours 迂回
- ADR-015 A (Sentinel 拡張) と連携
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("moomoo_opend_relogin")

# ── 設定 ──────────────────────────────────────────────────────────────────────
_KEYCHAIN_SERVICE_PWD = "moomoo_opend"
_KEYCHAIN_SERVICE_ACCOUNT = "moomoo_opend_account"
_KEYCHAIN_ACCOUNT_REF = "sora_lab"

_OPEND_OPERATE_HOST = os.getenv("MOOMOO_OPEND_OPERATE_HOST", "127.0.0.1")
_OPEND_OPERATE_PORT = int(os.getenv("MOOMOO_OPEND_OPERATE_PORT", "22222"))
_OPERATE_SOCKET_TIMEOUT_SECS = 10.0
_OPERATE_READ_TIMEOUT_SECS = 5.0

# heartbeat 書込先（env で test 差替可能）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR_DEFAULT = _PROJECT_ROOT / "data" / "state_v3"
_STATE_DIR = Path(os.getenv("TRADING_STATE_DIR", str(_STATE_DIR_DEFAULT)))
_HEARTBEAT_FILE = _STATE_DIR / "opend_relogin_heartbeat.jsonl"


class RelogingError(Exception):
    """relogin 処理で発生する上位例外。"""


class KeychainAccessError(RelogingError):
    """Keychain からの credential 取得失敗。"""


class OpendOperateConnectionError(RelogingError):
    """OpenD operate port (22222) への接続失敗。"""


class OpendOperateResponseError(RelogingError):
    """relogin response が成功応答でない。"""


# ── Keychain 取得 ──────────────────────────────────────────────────────────────

def _fetch_from_keychain(service: str, account: Optional[str] = None) -> str:
    """macOS security CLI で Keychain entry を取得する。

    password 本体は subprocess 引数として渡さず、stdout でのみ受け取る
    （-w option は password を stdout に出力する設計）。
    """
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd.extend(["-a", account])
    cmd.append("-w")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeychainAccessError(
            f"Keychain lookup timeout (service={service})"
        ) from exc
    except FileNotFoundError as exc:
        raise KeychainAccessError(
            "macOS 'security' CLI not found. This daemon runs on macOS only."
        ) from exc

    if result.returncode != 0:
        # stderr に credential は出ない（macOS 実装で保証）
        raise KeychainAccessError(
            f"Keychain entry not found (service={service}). "
            "Run scripts/setup_moomoo_keychain.sh to register credential."
        )

    value = result.stdout.rstrip("\n")
    if not value:
        raise KeychainAccessError(f"Keychain entry empty (service={service})")
    return value


def _resolve_credential() -> tuple[str, str]:
    """Keychain から (account, password) を取得する。

    - account は `moomoo_opend_account` service + `sora_lab` account から取出
    - password は `moomoo_opend` service + 取得した account から取出
    """
    account = _fetch_from_keychain(_KEYCHAIN_SERVICE_ACCOUNT, _KEYCHAIN_ACCOUNT_REF)
    password = _fetch_from_keychain(_KEYCHAIN_SERVICE_PWD, account)
    return account, password


# ── MD5 化 ────────────────────────────────────────────────────────────────────

def _password_md5(password: str) -> str:
    """plaintext password を 32 桁小文字 hex MD5 に変換する。

    moomoo 公式仕様に準拠:
    https://openapi.moomoo.com/moomoo-api-doc/en/trade/unlock.html
    > md5 of password (all lowercase, 32 char hex)
    """
    return hashlib.md5(password.encode("utf-8")).hexdigest()


# ── telnet 経由の relogin 実行 ─────────────────────────────────────────────────

def _send_operate_command(command: str) -> str:
    """OpenD operate port に command を送信し response を返す。

    raw TCP socket。OpenD は接続時に banner を即送信する仕様のため、
    以下順で処理する:
      1. 接続
      2. banner を drain (短い timeout で読み捨て)
      3. command 送信
      4. command response を受信 (banner 以降の新 bytes のみ)
    """
    try:
        sock = socket.create_connection(
            (_OPEND_OPERATE_HOST, _OPEND_OPERATE_PORT),
            timeout=_OPERATE_SOCKET_TIMEOUT_SECS,
        )
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise OpendOperateConnectionError(
            f"OpenD operate port unreachable: "
            f"{_OPEND_OPERATE_HOST}:{_OPEND_OPERATE_PORT} ({exc})"
        ) from exc

    try:
        # Phase 1: banner drain (OpenD の挨拶文を捨てる)
        sock.settimeout(1.0)
        banner_buf = b""
        banner_deadline = time.monotonic() + 1.5
        while time.monotonic() < banner_deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            banner_buf += chunk
            # prompt または "Type help" パターン到達で banner 完了と判定
            banner_lower = banner_buf.lower()
            if any(p in banner_lower for p in (b"type \"help\"", b"type 'help'", b"> ", b">>>")):
                break

        # Phase 2: command 送信 (OpenD は CRLF 要求・LF のみでは無視される)
        sock.settimeout(_OPERATE_READ_TIMEOUT_SECS)
        payload = (command + "\r\n").encode("utf-8")
        sock.sendall(payload)

        # Phase 3: command response を受信 (banner は既に drain 済なのでこれは真の応答)
        response_buf = b""
        response_deadline = time.monotonic() + _OPERATE_READ_TIMEOUT_SECS
        while time.monotonic() < response_deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                # timeout は「応答終了」の合図 (prompt 待ちでもう来ない)
                break
            if not chunk:
                break
            response_buf += chunk
            # 改行が 1 件以上入ったら response 到達・さらに短い follow-up 読取
            if b"\n" in response_buf and len(response_buf) >= 10:
                # 追加 chunk を短時間待つ (multi-line response 完全取得)
                sock.settimeout(0.3)
                try:
                    while True:
                        more = sock.recv(4096)
                        if not more:
                            break
                        response_buf += more
                except socket.timeout:
                    pass
                break

        return response_buf.decode("utf-8", errors="replace").strip()
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _execute_relogin(password_md5: str) -> str:
    """OpenD に relogin -login_pwd_md5=<hex> を送り response を返す。

    password_md5 は log には出さず、response だけを log する。
    """
    command = f"relogin -login_pwd_md5={password_md5}"
    log.info("[Relogin] sending relogin command to OpenD (md5 hex masked)")
    response = _send_operate_command(command)
    return response


def _validate_relogin_response(response: str) -> None:
    """relogin response の成功判定。

    公式 doc に明示された response format は限定的なため、以下ルールで判定:
    - 空 response → failure
    - 「success」「Success」「OK」「succeed」等の肯定語を含む → 成功
    - 「fail」「error」「invalid」「wrong」等の否定語を含む → failure
    - いずれも含まない場合 → 判定不能として警告扱い（failure 側に倒す）
    """
    if not response:
        raise OpendOperateResponseError("empty response from OpenD operate port")

    lower = response.lower()
    negative_terms = [
        "fail", "error", "invalid", "wrong", "incorrect",
        "expired", "denied", "not found", "timeout",
        # moomoo 多言語
        "失败", "错误", "失败", "失敗", "エラー",
    ]
    positive_terms = ["success", "succeed", "ok", "accepted", "done", "成功", "完了"]

    if any(term in lower for term in negative_terms):
        raise OpendOperateResponseError(
            f"relogin failed: {response[:200]}"
        )
    if any(term in lower for term in positive_terms):
        return
    # 判定不能は warning + failure 扱い（安全側）
    raise OpendOperateResponseError(
        f"relogin response ambiguous (cannot determine success): {response[:200]}"
    )


# ── heartbeat ─────────────────────────────────────────────────────────────────

def _record_heartbeat(status: str, details: Optional[dict] = None) -> None:
    """heartbeat を jsonl に append する。Sentinel が監視。"""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "component": "moomoo_opend_relogin",
            "pid": os.getpid(),
            "status": status,
            "details": details or {},
        }
        with open(_HEARTBEAT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.error("[Relogin] failed to write heartbeat: %s", exc)


# ── Pushover エスカレーション ─────────────────────────────────────────────────

def _escalate_failure(reason: str, response_excerpt: str = "") -> None:
    """relogin 失敗を Pushover priority=1 で通知する。

    深夜 (22-06 JST) は priority=2 で quiet_hours を迂回 (認証失敗は緊急)。
    """
    try:
        import common.pushover_client as _pushover
        title = "[Atlas] moomoo OpenD relogin FAILED"
        message = (
            f"reason: {reason}\n"
            f"response: {response_excerpt[:200] if response_excerpt else '(none)'}\n"
            f"action: moomoo app で手動再ログイン要 or Keychain credential 確認\n"
            f"ts: {datetime.now(timezone.utc).isoformat()}"
        )
        # 深夜は priority=2 で迂回
        hour_jst = (datetime.now(timezone.utc).hour + 9) % 24
        priority = 2 if (22 <= hour_jst or hour_jst < 6) else 1
        _pushover.send(title=title, message=message, priority=priority)
        log.info("[Relogin] escalated failure to Pushover (priority=%d)", priority)
    except Exception as exc:
        log.error("[Relogin] Pushover escalation failed: %s", exc)


# ── 公開 API ──────────────────────────────────────────────────────────────────

def run_once() -> int:
    """1 回だけ relogin を実行する (launchd から呼ばれる entry point)。

    Returns:
        exit code (0=成功, 1=credential エラー, 2=接続エラー, 3=relogin 失敗)
    """
    try:
        account, password = _resolve_credential()
    except KeychainAccessError as exc:
        log.error("[Relogin] Keychain access failed: %s", exc)
        _record_heartbeat("failure", {"stage": "keychain", "error": str(exc)})
        _escalate_failure(f"Keychain access: {exc}")
        return 1

    # password は MD5 化後すぐメモリから消したい
    password_md5 = _password_md5(password)
    password = "*" * len(password)  # overwrite
    del password

    try:
        response = _execute_relogin(password_md5)
    except OpendOperateConnectionError as exc:
        log.error("[Relogin] OpenD operate connection failed: %s", exc)
        _record_heartbeat("failure", {"stage": "connect", "error": str(exc)})
        _escalate_failure(f"OpenD operate port unreachable: {exc}")
        return 2
    finally:
        password_md5 = "*" * len(password_md5)  # overwrite
        del password_md5

    try:
        _validate_relogin_response(response)
    except OpendOperateResponseError as exc:
        log.error("[Relogin] relogin response error: %s", exc)
        _record_heartbeat("failure", {
            "stage": "response",
            "error": str(exc),
            "response_excerpt": response[:200] if response else "",
        })
        _escalate_failure(str(exc), response or "")
        return 3

    log.info("[Relogin] success (account=%s, response=%s)", account, response[:80])
    _record_heartbeat("success", {
        "account": account,
        "response_excerpt": response[:200] if response else "",
    })
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
