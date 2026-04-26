"""tests/fixtures/opend_mock_fixture.py — OpenDMockServer の pytest fixture 提供

使い方:
    from tests.fixtures.opend_mock_fixture import opend_mock, opend_mock_auth_fail

    def test_something(opend_mock):
        # opend_mock.api_port / operate_port は OS 割り当て空きポート
        ...

ポートは pytest 実行環境での衝突を避けるため ephemeral (OS 割り当て) とする。
固定ポート (11111 / 22222) を使いたい場合は OpenDMockServer を直接インスタンス化する。
"""
from __future__ import annotations

from typing import Generator

import pytest

from tests.mocks.opend_mock_server import FaultFlags, OpenDMockServer


def _find_free_port() -> int:
    """OS に空きポートを割り当ててもらい、ポート番号を返す。"""
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def opend_mock() -> Generator[OpenDMockServer, None, None]:
    """正常動作の OpenDMockServer fixture。

    Yields:
        起動済みの OpenDMockServer インスタンス。
        テスト終了後に自動停止する。
    """
    api_port = _find_free_port()
    operate_port = _find_free_port()
    server = OpenDMockServer(
        api_port=api_port,
        operate_port=operate_port,
        fault_flags=FaultFlags(),
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def opend_mock_auth_fail() -> Generator[OpenDMockServer, None, None]:
    """auth_fail=True の OpenDMockServer fixture。

    GetAccList / GetFunds を呼ぶと 401 エラーを返す。
    """
    api_port = _find_free_port()
    operate_port = _find_free_port()
    server = OpenDMockServer(
        api_port=api_port,
        operate_port=operate_port,
        fault_flags=FaultFlags(auth_fail=True),
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def opend_mock_rate_limit() -> Generator[OpenDMockServer, None, None]:
    """rate_limit_429=3 の OpenDMockServer fixture。

    最初の 3 リクエストに rate_limit エラーを返す。
    """
    api_port = _find_free_port()
    operate_port = _find_free_port()
    server = OpenDMockServer(
        api_port=api_port,
        operate_port=operate_port,
        fault_flags=FaultFlags(rate_limit_429=3),
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def opend_mock_hang() -> Generator[OpenDMockServer, None, None]:
    """hang=True の OpenDMockServer fixture。

    リクエストに対してレスポンスを送らない (timeout テスト用)。
    """
    api_port = _find_free_port()
    operate_port = _find_free_port()
    server = OpenDMockServer(
        api_port=api_port,
        operate_port=operate_port,
        fault_flags=FaultFlags(hang=True),
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def opend_mock_bad_json() -> Generator[OpenDMockServer, None, None]:
    """bad_json=True の OpenDMockServer fixture。

    レスポンス body に不正 JSON を返す (parse error テスト用)。
    """
    api_port = _find_free_port()
    operate_port = _find_free_port()
    server = OpenDMockServer(
        api_port=api_port,
        operate_port=operate_port,
        fault_flags=FaultFlags(bad_json=True),
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()
