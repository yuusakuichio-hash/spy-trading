"""tests/test_opend_mock_server_20260425.py — OpenDMockServer 単体テスト

opend_mock_server.py 自体の動作を検証する。futu SDK 不要・標準ライブラリのみ。

テスト設計方針:
- 各テストは独立した ephemeral ポートを使用し他テストと干渉しない
- 全テスト後にサーバーを確実に stop() する (context manager 使用)
- fault injection フラグを組み合わせた境界ケースを網羅する
"""
from __future__ import annotations

import json
import socket
import struct
import time

import pytest

from tests.mocks.opend_mock_server import (
    CMD_INIT_CONNECT,
    CMD_QOT_GET_OPTION_CHAIN,
    CMD_TRD_GET_ACC_LIST,
    CMD_TRD_GET_FUNDS,
    CMD_TRD_PLACE_ORDER,
    FaultFlags,
    OpenDMockServer,
    RET_ERROR,
    RET_OK,
    _FUTU_MAGIC,
    _HEADER_FMT,
    _HEADER_SIZE,
    _pack_frame,
    _unpack_header,
)

# ─────────────────────────────────────────────────────────────────────────────
# テストヘルパー
# ─────────────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """OS に空きポートを割り当ててポート番号を返す。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_server(**kwargs) -> OpenDMockServer:
    """ephemeral ポートの OpenDMockServer を生成する (起動はしない)。"""
    return OpenDMockServer(
        api_port=_free_port(),
        operate_port=_free_port(),
        **kwargs,
    )


def _send_api_request(host: str, port: int, cmd_id: int, seq: int = 1, body: bytes = b"{}") -> dict:
    """API ポートにリクエストフレームを送り、レスポンス JSON dict を返す。"""
    frame = _pack_frame(cmd_id, seq, body)
    with socket.create_connection((host, port), timeout=3.0) as sock:
        sock.sendall(frame)
        # レスポンスを受信
        raw = b""
        sock.settimeout(3.0)
        while len(raw) < _HEADER_SIZE:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        if len(raw) < _HEADER_SIZE:
            raise IOError("incomplete header received")
        _magic, _ver, _cmd, _seq, body_len = _unpack_header(raw)
        total = _HEADER_SIZE + body_len
        while len(raw) < total:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        body_bytes = raw[_HEADER_SIZE:total]
        return json.loads(body_bytes.decode("utf-8"))


def _send_operate_command(host: str, port: int, cmd: str, timeout: float = 3.0) -> str:
    """Operate ポートに CRLF コマンドを送り、banner 以降のレスポンスを返す。"""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        # バナーを読み捨て
        banner = b""
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            banner += chunk
            if b"> " in banner:
                break
        # コマンド送信
        sock.sendall((cmd + "\r\n").encode("utf-8"))
        # レスポンス受信
        resp = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            resp += chunk
            if b"\n" in resp:
                break
        return resp.decode("utf-8", errors="replace").strip()


# ─────────────────────────────────────────────────────────────────────────────
# T-01: サーバーの起動・停止
# ─────────────────────────────────────────────────────────────────────────────

class TestServerLifecycle:
    """サーバーライフサイクル: start / stop / context manager。"""

    def test_start_and_stop(self):
        """start() / stop() が例外なく完了し、ポートが LISTEN 状態になる。"""
        server = _make_server()
        server.start()
        try:
            # API ポートに接続できる
            conn = socket.create_connection(("127.0.0.1", server.api_port), timeout=1.0)
            conn.close()
        finally:
            server.stop()

    def test_context_manager(self):
        """with 文での起動・停止が正常動作する。"""
        server = _make_server()
        with server:
            conn = socket.create_connection(("127.0.0.1", server.api_port), timeout=1.0)
            conn.close()

    def test_operate_port_is_listening(self):
        """Operate ポートも LISTEN 状態になる。"""
        server = _make_server()
        with server:
            conn = socket.create_connection(("127.0.0.1", server.operate_port), timeout=1.0)
            conn.close()

    def test_double_start_raises(self):
        """二重 start() は RuntimeError を raise する。"""
        server = _make_server()
        server.start()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                server.start()
        finally:
            server.stop()


# ─────────────────────────────────────────────────────────────────────────────
# T-02: プロトコルヘルパーの単体検証
# ─────────────────────────────────────────────────────────────────────────────

class TestProtocolHelpers:
    """_pack_frame / _unpack_header の対称性検証。"""

    def test_pack_unpack_roundtrip(self):
        """pack して unpack すると元の値に戻る。"""
        body = b'{"test": 1}'
        frame = _pack_frame(CMD_INIT_CONNECT, seq=42, body=body)
        magic, ver, cmd_id, seq, body_len = _unpack_header(frame)
        assert magic == _FUTU_MAGIC
        assert ver == 2
        assert cmd_id == CMD_INIT_CONNECT
        assert seq == 42
        assert body_len == len(body)
        assert frame[_HEADER_SIZE:] == body

    def test_header_size_is_20(self):
        """ヘッダーサイズは 20 バイト固定。"""
        assert _HEADER_SIZE == 20

    def test_magic_bytes(self):
        """マジックバイトは b'FUTU' (0x46555455)。"""
        assert _FUTU_MAGIC == 0x46555455


# ─────────────────────────────────────────────────────────────────────────────
# T-03: InitConnect 応答
# ─────────────────────────────────────────────────────────────────────────────

class TestInitConnect:
    """CMD_INIT_CONNECT (cmd_id=1001) の応答検証。"""

    def test_init_connect_returns_ret_ok(self):
        """InitConnect に RET_OK=0 を返す。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_INIT_CONNECT)
            assert resp["ret_code"] == RET_OK

    def test_init_connect_has_conn_id(self):
        """InitConnect レスポンスに conn_id フィールドが含まれる。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_INIT_CONNECT, seq=99)
            assert "conn_id" in resp["data"]


# ─────────────────────────────────────────────────────────────────────────────
# T-04: Trd_GetAccList 応答
# ─────────────────────────────────────────────────────────────────────────────

class TestGetAccList:
    """CMD_TRD_GET_ACC_LIST (cmd_id=2101) の正常 / auth_fail 応答検証。"""

    def test_get_acc_list_returns_ret_ok(self):
        """正常: RET_OK=0 と acc_list を返す。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_ACC_LIST)
            assert resp["ret_code"] == RET_OK
            assert "acc_list" in resp["data"]

    def test_get_acc_list_has_simulate_env(self):
        """正常: acc_list 内に TRD_ENV_SIMULATE エントリが存在する。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_ACC_LIST)
            acc_list = resp["data"]["acc_list"]
            assert len(acc_list) >= 1
            assert any(a["trd_env"] == "TRD_ENV_SIMULATE" for a in acc_list)

    def test_get_acc_list_auth_fail_returns_401(self):
        """auth_fail=True: ret_code=-1 と 401 メッセージを返す。"""
        server = _make_server(fault_flags=FaultFlags(auth_fail=True))
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_ACC_LIST)
            assert resp["ret_code"] == RET_ERROR
            assert "401" in resp["ret_msg"] or "Unauthorized" in resp["ret_msg"]


# ─────────────────────────────────────────────────────────────────────────────
# T-05: Trd_GetFunds 応答
# ─────────────────────────────────────────────────────────────────────────────

class TestGetFunds:
    """CMD_TRD_GET_FUNDS (cmd_id=2102) の正常 / auth_fail 応答検証。"""

    def test_get_funds_returns_ret_ok(self):
        """正常: RET_OK=0 と funds フィールドを返す。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_FUNDS)
            assert resp["ret_code"] == RET_OK
            assert "funds" in resp["data"]

    def test_get_funds_has_total_assets(self):
        """正常: funds に total_assets が含まれる。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_FUNDS)
            assert "total_assets" in resp["data"]["funds"]

    def test_get_funds_auth_fail(self):
        """auth_fail=True: ret_code=-1 を返す。"""
        server = _make_server(fault_flags=FaultFlags(auth_fail=True))
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_FUNDS)
            assert resp["ret_code"] == RET_ERROR


# ─────────────────────────────────────────────────────────────────────────────
# T-06: Trd_PlaceOrder 応答
# ─────────────────────────────────────────────────────────────────────────────

class TestPlaceOrder:
    """CMD_TRD_PLACE_ORDER (cmd_id=2175) の応答検証。"""

    def test_place_order_returns_order_id(self):
        """PlaceOrder: RET_OK=0 と order_id を返す。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_PLACE_ORDER)
            assert resp["ret_code"] == RET_OK
            assert "order_id" in resp["data"]


# ─────────────────────────────────────────────────────────────────────────────
# T-07: Qot_GetOptionChain 応答
# ─────────────────────────────────────────────────────────────────────────────

class TestGetOptionChain:
    """CMD_QOT_GET_OPTION_CHAIN (cmd_id=3009) の応答検証。"""

    def test_option_chain_returns_data(self):
        """OptionChain: RET_OK=0 と option_chain リストを返す。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_QOT_GET_OPTION_CHAIN)
            assert resp["ret_code"] == RET_OK
            assert "option_chain" in resp["data"]
            chain = resp["data"]["option_chain"]
            assert isinstance(chain, list)
            assert len(chain) >= 1

    def test_option_chain_has_greeks(self):
        """OptionChain エントリに Greeks (delta/gamma/theta/vega) が含まれる。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, CMD_QOT_GET_OPTION_CHAIN)
            entry = resp["data"]["option_chain"][0]
            for greek in ("delta", "gamma", "theta", "vega"):
                assert greek in entry, f"missing greek: {greek}"


# ─────────────────────────────────────────────────────────────────────────────
# T-08: rate_limit_429 fault injection
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimitFault:
    """rate_limit_429 フラグの動作検証。"""

    def test_rate_limit_returns_error_for_first_n_requests(self):
        """最初の N リクエストに rate_limit エラーを返す。"""
        server = _make_server(fault_flags=FaultFlags(rate_limit_429=2))
        with server:
            # 1 回目: rate_limit
            resp1 = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_ACC_LIST, seq=1)
            assert resp1["ret_code"] == RET_ERROR
            assert "rate limit" in resp1["ret_msg"].lower()

    def test_request_log_records_rate_limit_fault(self):
        """rate_limit fault が request_log に記録される。"""
        server = _make_server(fault_flags=FaultFlags(rate_limit_429=1))
        with server:
            _send_api_request("127.0.0.1", server.api_port, CMD_INIT_CONNECT, seq=1)
            log = server.get_request_log()
            assert len(log) >= 1
            assert any(entry["fault"] == "rate_limit" for entry in log)


# ─────────────────────────────────────────────────────────────────────────────
# T-09: bad_json fault injection
# ─────────────────────────────────────────────────────────────────────────────

class TestBadJsonFault:
    """bad_json フラグの動作検証。"""

    def test_bad_json_body_is_not_valid_json(self):
        """bad_json=True: レスポンス body が JSON として parse できない。"""
        server = _make_server(fault_flags=FaultFlags(bad_json=True))
        frame = _pack_frame(CMD_INIT_CONNECT, seq=1, body=b"{}")
        with server:
            with socket.create_connection(("127.0.0.1", server.api_port), timeout=3.0) as sock:
                sock.sendall(frame)
                raw = b""
                sock.settimeout(3.0)
                while len(raw) < _HEADER_SIZE:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                _magic, _ver, _cmd, _seq, body_len = _unpack_header(raw)
                total = _HEADER_SIZE + body_len
                while len(raw) < total:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                body_bytes = raw[_HEADER_SIZE:total]
                with pytest.raises(json.JSONDecodeError):
                    json.loads(body_bytes)


# ─────────────────────────────────────────────────────────────────────────────
# T-10: hang fault injection
# ─────────────────────────────────────────────────────────────────────────────

class TestHangFault:
    """hang フラグの動作検証: タイムアウトを引き起こすこと。"""

    def test_hang_causes_socket_timeout(self):
        """hang=True: connect 後にレスポンスが来ず socket.timeout が発生する。"""
        server = _make_server(fault_flags=FaultFlags(hang=True))
        frame = _pack_frame(CMD_INIT_CONNECT, seq=1, body=b"{}")
        with server:
            with socket.create_connection(("127.0.0.1", server.api_port), timeout=2.0) as sock:
                sock.sendall(frame)
                sock.settimeout(0.5)
                with pytest.raises(socket.timeout):
                    sock.recv(4096)


# ─────────────────────────────────────────────────────────────────────────────
# T-11: Operate ポート (22222) の banner / help / relogin コマンド
# ─────────────────────────────────────────────────────────────────────────────

class TestOperatePort:
    """22222 ポートの banner + コマンド応答検証。"""

    def test_banner_is_sent_on_connect(self):
        """接続直後に banner が送られる。"""
        server = _make_server()
        with server:
            with socket.create_connection(("127.0.0.1", server.operate_port), timeout=2.0) as sock:
                sock.settimeout(2.0)
                data = b""
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    data += chunk
                    if b"> " in data:
                        break
                assert b"Welcome" in data or b"moomoo" in data or b"> " in data

    def test_relogin_success_response(self):
        """relogin コマンドに 'success' 応答が返る (fault なし)。"""
        server = _make_server()
        with server:
            resp = _send_operate_command(
                "127.0.0.1", server.operate_port,
                "relogin -login_pwd_md5=abc123def456abc123def456abc12345"
            )
            assert "success" in resp.lower()

    def test_relogin_auth_fail_response(self):
        """auth_fail=True: relogin コマンドに 'error' 応答が返る。"""
        server = _make_server(fault_flags=FaultFlags(auth_fail=True))
        with server:
            resp = _send_operate_command(
                "127.0.0.1", server.operate_port,
                "relogin -login_pwd_md5=abc123def456abc123def456abc12345"
            )
            assert "error" in resp.lower() or "fail" in resp.lower() or "invalid" in resp.lower()

    def test_help_command(self):
        """help コマンドに利用可能コマンド一覧が返る。"""
        server = _make_server()
        with server:
            resp = _send_operate_command("127.0.0.1", server.operate_port, "help")
            assert "relogin" in resp.lower()

    def test_operate_commands_log(self):
        """operate ポートで受信したコマンドが get_operate_commands() に記録される。"""
        server = _make_server()
        with server:
            _send_operate_command("127.0.0.1", server.operate_port, "help")
            cmds = server.get_operate_commands()
            assert "help" in cmds


# ─────────────────────────────────────────────────────────────────────────────
# T-12: リクエストログ / reset_logs
# ─────────────────────────────────────────────────────────────────────────────

class TestRequestLog:
    """get_request_log / reset_logs の動作検証。"""

    def test_request_is_logged(self):
        """API リクエストが request_log に記録される。"""
        server = _make_server()
        with server:
            _send_api_request("127.0.0.1", server.api_port, CMD_INIT_CONNECT)
            log_entries = server.get_request_log()
            assert len(log_entries) >= 1
            assert log_entries[0]["cmd_id"] == CMD_INIT_CONNECT

    def test_reset_logs_clears_log(self):
        """reset_logs() 後に request_log が空になる。"""
        server = _make_server()
        with server:
            _send_api_request("127.0.0.1", server.api_port, CMD_INIT_CONNECT)
            assert len(server.get_request_log()) >= 1
            server.reset_logs()
            assert server.get_request_log() == []

    def test_normal_request_has_no_fault(self):
        """正常リクエストの fault フィールドは None。"""
        server = _make_server()
        with server:
            _send_api_request("127.0.0.1", server.api_port, CMD_INIT_CONNECT)
            entry = server.get_request_log()[0]
            assert entry["fault"] is None


# ─────────────────────────────────────────────────────────────────────────────
# T-13: set_fault_flags の動的差し替え
# ─────────────────────────────────────────────────────────────────────────────

class TestSetFaultFlags:
    """実行中に set_fault_flags() でフラグを差し替えられること。"""

    def test_set_fault_flags_changes_behavior(self):
        """set_fault_flags(auth_fail=True) 後の新規接続は 401 を返す。"""
        server = _make_server()
        with server:
            # 最初は正常
            resp1 = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_ACC_LIST)
            assert resp1["ret_code"] == RET_OK

            # フラグ差し替え
            server.set_fault_flags(FaultFlags(auth_fail=True))

            # 新規接続から auth_fail が適用される
            resp2 = _send_api_request("127.0.0.1", server.api_port, CMD_TRD_GET_ACC_LIST)
            assert resp2["ret_code"] == RET_ERROR


# ─────────────────────────────────────────────────────────────────────────────
# T-14: 不明 cmd_id の処理
# ─────────────────────────────────────────────────────────────────────────────

class TestUnknownCmdId:
    """未定義の cmd_id に対して RET_ERROR を返すこと。"""

    def test_unknown_cmd_id_returns_error(self):
        """cmd_id=9999 (未定義) に ret_code=-1 が返る。"""
        server = _make_server()
        with server:
            resp = _send_api_request("127.0.0.1", server.api_port, cmd_id=9999)
            assert resp["ret_code"] == RET_ERROR
            assert "9999" in resp["ret_msg"]


# ─────────────────────────────────────────────────────────────────────────────
# T-15: FaultFlags デフォルト値
# ─────────────────────────────────────────────────────────────────────────────

class TestFaultFlagsDefaults:
    """FaultFlags のデフォルト値が全て無効 (fault なし) であること。"""

    def test_default_fault_flags_all_disabled(self):
        """デフォルト FaultFlags は全フラグが無効。"""
        ff = FaultFlags()
        assert ff.auth_fail is False
        assert ff.rate_limit_429 == 0
        assert ff.hang is False
        assert ff.bad_json is False
