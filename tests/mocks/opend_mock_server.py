"""tests/mocks/opend_mock_server.py — futu OpenD threading-based mock server

OpenD の代替 Python mock。実際の futu-api SDK を要求せずに
moomoo_provider.py や moomoo_opend_relogin.py の統合テストを実行可能にする。

実装方針:
- B16 規律 (asyncio は common_v3/executor/async_impl.py 外禁止) に従い
  threading + socket.socket で実装する。
- 各ポートに専用スレッドを立て、クライアント接続ごとにさらにスレッドを生成する。

## ポート割り当て
- 11111: futu API protocol v2 (binary: 20 byte header + JSON body)
- 22222: operate port (telnet-like, CRLF ベース banner + command)

## サポートコマンド (11111)
| cmd_id | 名称               |
|--------|--------------------|
|   1001 | InitConnect        |
|   2101 | Trd_GetAccList     |
|   2102 | Trd_GetFunds       |
|   2175 | Trd_PlaceOrder     |
|   3009 | Qot_GetOptionChain |

## Fault injection フラグ (FaultFlags)
| フラグ名         | 動作                                           |
|-----------------|------------------------------------------------|
| auth_fail       | GetAccList / GetFunds → ret_code=-1, 「401 Unauthorized」|
| rate_limit_429  | 最初の N リクエストに rate_limit エラーを返す   |
| hang            | レスポンスを送らずコネクションを保持            |
| bad_json        | body に不正 JSON を返す                         |

## 依存
- 標準ライブラリのみ (threading / socket / struct / json)
- futu-api SDK 不要
- read-only: 既存コード無変更

## 利用例
    server = OpenDMockServer(api_port=21111, operate_port=22223)
    server.start()           # バックグラウンドスレッドで起動
    try:
        ...  # テストコード
    finally:
        server.stop()

    # または context manager:
    with OpenDMockServer(api_port=21111, operate_port=22223) as server:
        ...
"""
from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# futu API v2 プロトコル定数
# Header format (big-endian):
#   magic(4B) + version(4B) + cmd_id(4B) + seq(4B) + body_len(4B) = 20 bytes
# Reference: https://openapi.futunn.com/futu-api-doc/intro/intro.html
# ─────────────────────────────────────────────────────────────────────────────
_FUTU_MAGIC: int = 0x46555455  # b'FUTU'
_PROTO_VERSION: int = 2
_HEADER_FMT: str = ">IIIII"
_HEADER_SIZE: int = struct.calcsize(_HEADER_FMT)  # = 20 bytes

# cmd_id 定数
CMD_INIT_CONNECT: int = 1001
CMD_TRD_GET_ACC_LIST: int = 2101
CMD_TRD_GET_FUNDS: int = 2102
CMD_TRD_PLACE_ORDER: int = 2175
CMD_QOT_GET_OPTION_CHAIN: int = 3009

# ret_code
RET_OK: int = 0
RET_ERROR: int = -1

# ─────────────────────────────────────────────────────────────────────────────
# Fault injection フラグ
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FaultFlags:
    """fault injection の設定コンテナ。

    Attributes:
        auth_fail: Trd_GetAccList / Trd_GetFunds に 401 エラーを返す。
        rate_limit_429: 最初の N リクエストに rate_limit エラーを返す。0=無効。
        hang: レスポンスを送らずコネクションを保持する (timeout テスト用)。
        bad_json: body に不正 JSON を返す (parse error テスト用)。
    """
    auth_fail: bool = False
    rate_limit_429: int = 0
    hang: bool = False
    bad_json: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# プロトコルヘルパー: フレームのパック / アンパック
# ─────────────────────────────────────────────────────────────────────────────

def _pack_frame(cmd_id: int, seq: int, body: bytes) -> bytes:
    """futu v2 binary frame を生成する。"""
    header = struct.pack(
        _HEADER_FMT,
        _FUTU_MAGIC,
        _PROTO_VERSION,
        cmd_id,
        seq,
        len(body),
    )
    return header + body


def _unpack_header(data: bytes) -> tuple[int, int, int, int, int]:
    """20 バイトのヘッダーをアンパックして (magic, version, cmd_id, seq, body_len) を返す。"""
    return struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])


def _json_body(obj: Any) -> bytes:
    """dict を UTF-8 JSON bytes に変換する。"""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# レスポンスファクトリ: cmd_id ごとの正常 / 異常レスポンス
# ─────────────────────────────────────────────────────────────────────────────

def _resp_init_connect(seq: int) -> bytes:
    body = _json_body({
        "ret_code": RET_OK,
        "ret_msg": "ok",
        "data": {
            "server_version": "8.0.3208.0",
            "login_user_id": 123456789,
            "conn_id": seq,
            "conn_key": "mock_conn_key_abc123",
        },
    })
    return _pack_frame(CMD_INIT_CONNECT, seq, body)


def _resp_get_acc_list(seq: int, *, auth_fail: bool = False) -> bytes:
    if auth_fail:
        body = _json_body({
            "ret_code": RET_ERROR,
            "ret_msg": "401 Unauthorized - session expired",
            "data": {},
        })
    else:
        body = _json_body({
            "ret_code": RET_OK,
            "ret_msg": "ok",
            "data": {
                "acc_list": [
                    {
                        "trd_side": "LONG",
                        "acc_id": 12345678,
                        "trd_env": "TRD_ENV_SIMULATE",
                        "acc_type": "ACC_TYPE_MARGIN",
                        "card_num": "****1234",
                        "security_firm": "SECURITY_FIRM_FUTUJP",
                        "sim_acc_type": "SIM_ACC_TYPE_STOCK",
                    }
                ]
            },
        })
    return _pack_frame(CMD_TRD_GET_ACC_LIST, seq, body)


def _resp_get_funds(seq: int, *, auth_fail: bool = False) -> bytes:
    if auth_fail:
        body = _json_body({
            "ret_code": RET_ERROR,
            "ret_msg": "401 Unauthorized - session expired",
            "data": {},
        })
    else:
        body = _json_body({
            "ret_code": RET_OK,
            "ret_msg": "ok",
            "data": {
                "funds": {
                    "power": 50000.0,
                    "total_assets": 100000.0,
                    "cash": 50000.0,
                    "market_val": 50000.0,
                    "frozen_power": 0.0,
                    "avl_withdrawal_cash": 48500.0,
                    "realized_pl": 500.0,
                    "unrealized_pl": -200.0,
                    "risk_ratio": 0.0,
                    "init_margin": 0.0,
                    "maintenance_margin": 0.0,
                }
            },
        })
    return _pack_frame(CMD_TRD_GET_FUNDS, seq, body)


def _resp_place_order(seq: int) -> bytes:
    body = _json_body({
        "ret_code": RET_OK,
        "ret_msg": "ok",
        "data": {
            "trd_env": "TRD_ENV_SIMULATE",
            "order_id": 9000000001,
        },
    })
    return _pack_frame(CMD_TRD_PLACE_ORDER, seq, body)


def _resp_get_option_chain(seq: int) -> bytes:
    body = _json_body({
        "ret_code": RET_OK,
        "ret_msg": "ok",
        "data": {
            "option_chain": [
                {
                    "strike_price": 500.0,
                    "option_type": "CALL",
                    "code": "US.SPY240101C500",
                    "bid_price": 2.50,
                    "ask_price": 2.55,
                    "delta": 0.45,
                    "gamma": 0.02,
                    "theta": -0.05,
                    "vega": 0.10,
                    "implied_volatility": 0.18,
                    "open_interest": 12000,
                }
            ]
        },
    })
    return _pack_frame(CMD_QOT_GET_OPTION_CHAIN, seq, body)


def _resp_rate_limit(cmd_id: int, seq: int) -> bytes:
    body = _json_body({
        "ret_code": RET_ERROR,
        "ret_msg": "rate limit exceeded, please retry later",
        "data": {},
    })
    return _pack_frame(cmd_id, seq, body)


def _resp_bad_json(cmd_id: int, seq: int) -> bytes:
    """不正 JSON を body に持つフレーム (parse error テスト用)。"""
    body = b'{"ret_code": 0, "ret_msg": "ok", INVALID_JSON_HERE'
    return _pack_frame(cmd_id, seq, body)


def _resp_unknown_cmd(cmd_id: int, seq: int) -> bytes:
    body = _json_body({
        "ret_code": RET_ERROR,
        "ret_msg": f"unknown cmd_id={cmd_id}",
        "data": {},
    })
    return _pack_frame(cmd_id, seq, body)


# ─────────────────────────────────────────────────────────────────────────────
# Operate ポート (22222) 用定数
# ─────────────────────────────────────────────────────────────────────────────

_OPERATE_BANNER: bytes = (
    b"Welcome to moomoo OpenD mock operate interface v2.0\r\n"
    b"Type \"help\" for available commands.\r\n"
    b"> "
)

_OPERATE_HELP: bytes = (
    b"Available commands:\r\n"
    b"  help                        - show this help\r\n"
    b"  relogin -login_pwd_md5=HEX  - refresh login session\r\n"
    b"> "
)


# ─────────────────────────────────────────────────────────────────────────────
# API ポート (11111) クライアントハンドラ (1 スレッド / 接続)
# ─────────────────────────────────────────────────────────────────────────────

def _handle_api_client(
    conn: socket.socket,
    fault_flags: FaultFlags,
    server: "OpenDMockServer",
) -> None:
    """API ポートの 1 クライアント接続を処理するスレッドエントリーポイント。"""
    buf = b""
    req_count = 0
    conn.settimeout(10.0)
    try:
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf += chunk

            while len(buf) >= _HEADER_SIZE:
                magic, _version, cmd_id, seq, body_len = _unpack_header(buf)
                if magic != _FUTU_MAGIC:
                    log.warning("[API mock] bad magic %s, closing", hex(magic))
                    return
                total = _HEADER_SIZE + body_len
                if len(buf) < total:
                    break
                _body_bytes = buf[_HEADER_SIZE:total]
                buf = buf[total:]
                req_count += 1

                response = _build_api_response(
                    cmd_id=cmd_id,
                    seq=seq,
                    req_count=req_count,
                    fault_flags=fault_flags,
                    server=server,
                )
                if response is not None:
                    try:
                        conn.sendall(response)
                    except OSError:
                        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _build_api_response(
    *,
    cmd_id: int,
    seq: int,
    req_count: int,
    fault_flags: FaultFlags,
    server: "OpenDMockServer",
) -> Optional[bytes]:
    """fault injection を考慮してレスポンスフレームを生成する。

    Returns:
        bytes フレーム、または hang の場合は None (送信しない)。
    """
    # fault: hang
    if fault_flags.hang:
        log.debug("[API mock] hang injected cmd_id=%d seq=%d", cmd_id, seq)
        server._record_request(cmd_id=cmd_id, seq=seq, fault="hang")
        return None

    # fault: rate_limit_429 — 最初の N リクエスト
    if fault_flags.rate_limit_429 > 0 and req_count <= fault_flags.rate_limit_429:
        server._record_request(cmd_id=cmd_id, seq=seq, fault="rate_limit")
        return _resp_rate_limit(cmd_id, seq)

    # fault: bad_json
    if fault_flags.bad_json:
        server._record_request(cmd_id=cmd_id, seq=seq, fault="bad_json")
        return _resp_bad_json(cmd_id, seq)

    # 通常ルーティング
    server._record_request(cmd_id=cmd_id, seq=seq, fault=None)
    return _dispatch_cmd(cmd_id, seq, fault_flags)


def _dispatch_cmd(cmd_id: int, seq: int, fault_flags: FaultFlags) -> bytes:
    """cmd_id に応じてレスポンスフレームを選択する。"""
    if cmd_id == CMD_INIT_CONNECT:
        return _resp_init_connect(seq)
    if cmd_id == CMD_TRD_GET_ACC_LIST:
        return _resp_get_acc_list(seq, auth_fail=fault_flags.auth_fail)
    if cmd_id == CMD_TRD_GET_FUNDS:
        return _resp_get_funds(seq, auth_fail=fault_flags.auth_fail)
    if cmd_id == CMD_TRD_PLACE_ORDER:
        return _resp_place_order(seq)
    if cmd_id == CMD_QOT_GET_OPTION_CHAIN:
        return _resp_get_option_chain(seq)
    return _resp_unknown_cmd(cmd_id, seq)


# ─────────────────────────────────────────────────────────────────────────────
# Operate ポート (22222) クライアントハンドラ
# ─────────────────────────────────────────────────────────────────────────────

def _handle_operate_client(
    conn: socket.socket,
    fault_flags: FaultFlags,
    server: "OpenDMockServer",
) -> None:
    """Operate ポートの 1 クライアント接続を処理するスレッドエントリーポイント。"""
    conn.settimeout(10.0)
    try:
        conn.sendall(_OPERATE_BANNER)
        buf = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd_str = line.rstrip(b"\r").decode("utf-8", errors="replace").strip()
                if cmd_str:
                    _dispatch_operate_cmd(
                        cmd=cmd_str,
                        conn=conn,
                        fault_flags=fault_flags,
                        server=server,
                    )
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _dispatch_operate_cmd(
    *,
    cmd: str,
    conn: socket.socket,
    fault_flags: FaultFlags,
    server: "OpenDMockServer",
) -> None:
    """operate コマンド文字列を解析してレスポンスを送信する。"""
    server._operate_commands.append(cmd)
    lower = cmd.lower()

    def _send(data: bytes) -> None:
        try:
            conn.sendall(data)
        except OSError:
            pass

    if lower == "help":
        _send(_OPERATE_HELP)
        return

    if lower.startswith("relogin"):
        # fault: hang
        if fault_flags.hang:
            log.debug("[Operate mock] hang injected for relogin cmd")
            return
        # fault: auth_fail
        if fault_flags.auth_fail:
            _send(b"error: relogin failed - invalid credentials\r\n> ")
            return
        _send(b"relogin success\r\n> ")
        return

    # 未知コマンド
    _send(f"unknown command: {cmd}\r\n> ".encode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# アクセプトループ: 各ポートごとの accept スレッド
# ─────────────────────────────────────────────────────────────────────────────

def _accept_loop(
    server_sock: socket.socket,
    handler: Any,
    fault_flags_ref: "list[FaultFlags]",  # mutable container で参照渡し
    server: "OpenDMockServer",
    stop_event: threading.Event,
) -> None:
    """server_sock で accept して handler スレッドを生成するループ。

    stop_event がセットされると終了する。fault_flags_ref[0] を参照するため
    実行中の fault_flags 差し替えに対応している。
    """
    server_sock.settimeout(0.2)
    while not stop_event.is_set():
        try:
            conn, addr = server_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        log.debug("[mock] accepted connection from %s", addr)
        t = threading.Thread(
            target=handler,
            args=(conn, fault_flags_ref[0], server),
            daemon=True,
        )
        t.start()


# ─────────────────────────────────────────────────────────────────────────────
# OpenDMockServer — 両ポートを管理するメインクラス
# ─────────────────────────────────────────────────────────────────────────────

class OpenDMockServer:
    """futu OpenD の threading + socket ベース mock サーバー。

    2 つのリスニングスレッド (API / Operate) を起動し、接続ごとに
    ハンドラースレッドを生成する。stop() でグレースフルに停止する。

    Args:
        api_host: API ポートのバインドアドレス (default: 127.0.0.1)
        api_port: API ポート番号 (default: 11111)
        operate_host: Operate ポートのバインドアドレス (default: 127.0.0.1)
        operate_port: Operate ポート番号 (default: 22222)
        fault_flags: fault injection 設定 (default: FaultFlags() = 全無効)

    Example::

        server = OpenDMockServer(api_port=21111, operate_port=22223)
        server.start()
        try:
            # ... テストコード ...
        finally:
            server.stop()
    """

    def __init__(
        self,
        *,
        api_host: str = "127.0.0.1",
        api_port: int = 11111,
        operate_host: str = "127.0.0.1",
        operate_port: int = 22222,
        fault_flags: Optional[FaultFlags] = None,
    ) -> None:
        self.api_host = api_host
        self.api_port = api_port
        self.operate_host = operate_host
        self.operate_port = operate_port

        # mutable container で fault_flags を accept ループに渡す (set_fault_flags 対応)
        self._fault_ref: list[FaultFlags] = [fault_flags or FaultFlags()]

        self._stop_event = threading.Event()
        self._api_sock: Optional[socket.socket] = None
        self._operate_sock: Optional[socket.socket] = None
        self._api_thread: Optional[threading.Thread] = None
        self._operate_thread: Optional[threading.Thread] = None

        # テスト補助: リクエストログ・operate コマンドログ
        self._request_log: list[dict] = []
        self._operate_commands: list[str] = []
        self._log_lock = threading.Lock()

    # ── ライフサイクル ────────────────────────────────────────────────────────

    def start(self, startup_timeout: float = 5.0) -> None:
        """バックグラウンドスレッドでサーバーを起動する。

        startup_timeout 秒以内に両ポートが LISTEN 状態になるまでブロックする。

        Raises:
            RuntimeError: サーバーが既に起動済みの場合
            OSError: ポートバインド失敗
        """
        if self._api_thread is not None and self._api_thread.is_alive():
            raise RuntimeError("OpenDMockServer is already running")

        self._stop_event.clear()

        self._api_sock = self._make_server_socket(self.api_host, self.api_port)
        self._operate_sock = self._make_server_socket(self.operate_host, self.operate_port)

        self._api_thread = threading.Thread(
            target=_accept_loop,
            args=(self._api_sock, _handle_api_client, self._fault_ref, self, self._stop_event),
            name="opend_mock_api_accept",
            daemon=True,
        )
        self._operate_thread = threading.Thread(
            target=_accept_loop,
            args=(self._operate_sock, _handle_operate_client, self._fault_ref, self, self._stop_event),
            name="opend_mock_operate_accept",
            daemon=True,
        )
        self._api_thread.start()
        self._operate_thread.start()

        # LISTEN 確認: ポートに接続できるまで待機
        deadline = time.monotonic() + startup_timeout
        for port in (self.api_port, self.operate_port):
            while time.monotonic() < deadline:
                try:
                    probe = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                    probe.close()
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise TimeoutError(
                    f"OpenDMockServer port {port} did not become ready "
                    f"within {startup_timeout}s"
                )

        log.info(
            "[OpenDMockServer] ready api=%s:%d operate=%s:%d",
            self.api_host, self.api_port,
            self.operate_host, self.operate_port,
        )

    def stop(self, shutdown_timeout: float = 3.0) -> None:
        """サーバーを停止してスレッドを join する。"""
        self._stop_event.set()
        for sock in (self._api_sock, self._operate_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for t in (self._api_thread, self._operate_thread):
            if t is not None:
                t.join(timeout=shutdown_timeout)
        self._api_sock = None
        self._operate_sock = None
        self._api_thread = None
        self._operate_thread = None

    @staticmethod
    def _make_server_socket(host: str, port: int) -> socket.socket:
        """再利用可能なリスニングソケットを生成する。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(16)
        return sock

    # ── fault injection ───────────────────────────────────────────────────────

    @property
    def fault_flags(self) -> FaultFlags:
        return self._fault_ref[0]

    def set_fault_flags(self, flags: FaultFlags) -> None:
        """実行中に fault injection フラグを差し替える。

        Note:
            進行中のハンドラースレッドには適用されない (新規接続から有効)。
        """
        self._fault_ref[0] = flags

    # ── テスト補助 ────────────────────────────────────────────────────────────

    def _record_request(self, *, cmd_id: int, seq: int, fault: Optional[str]) -> None:
        with self._log_lock:
            self._request_log.append({
                "ts": time.monotonic(),
                "cmd_id": cmd_id,
                "seq": seq,
                "fault": fault,
            })

    def reset_logs(self) -> None:
        """リクエストログと operate コマンドログをクリアする。"""
        with self._log_lock:
            self._request_log.clear()
            self._operate_commands.clear()

    def get_request_log(self) -> list[dict]:
        """記録されたリクエストのリストを返す (スナップショットコピー)。"""
        with self._log_lock:
            return list(self._request_log)

    def get_operate_commands(self) -> list[str]:
        """operate ポートで受信したコマンドのリスト (スナップショットコピー)。"""
        with self._log_lock:
            return list(self._operate_commands)

    # ── context manager 対応 ─────────────────────────────────────────────────

    def __enter__(self) -> "OpenDMockServer":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()
