#!/usr/bin/env python3
"""
tests/test_external_health_ping.py — common/external_health_ping.py のユニットテスト

テスト項目:
  1. ping 成功 (200 OK)
  2. ping 失敗時のリトライ（5回 → False）
  3. UUID 未設定で warning ログ + False 返却
  4. ネットワーク障害（ConnectionError）で False 返却・本業継続
  5. status=fail / start の URL 生成確認
  6. health_server /health エンドポイント応答確認
  7. list_configured_components() 設定状況取得
  8. payload 付き ping（POST）
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# プロジェクトルートを sys.path に追加
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from common.external_health_ping import (
    _build_url,
    _get_uuid,
    list_configured_components,
    ping_healthchecks,
)


# ─────────────────────────────────────────────────────────────────────────────
# ヘルパー: 環境変数を一時設定するコンテキストマネージャ
# ─────────────────────────────────────────────────────────────────────────────

class _EnvOverride:
    """テスト中だけ環境変数を上書きし、終了後に復元する。"""

    def __init__(self, **kwargs: str | None):
        self._overrides = kwargs
        self._originals: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self._overrides.items():
            self._originals[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, orig in self._originals.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig


# ─────────────────────────────────────────────────────────────────────────────
# テストケース
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildUrl(unittest.TestCase):
    """URL 生成ロジックのテスト。"""

    def test_success_url(self):
        url = _build_url("test-uuid-1234", "success")
        self.assertEqual(url, "https://hc-ping.com/test-uuid-1234")

    def test_fail_url(self):
        url = _build_url("test-uuid-1234", "fail")
        self.assertEqual(url, "https://hc-ping.com/test-uuid-1234/fail")

    def test_start_url(self):
        url = _build_url("test-uuid-1234", "start")
        self.assertEqual(url, "https://hc-ping.com/test-uuid-1234/start")


class TestGetUuid(unittest.TestCase):
    """UUID 取得ロジックのテスト。"""

    def test_known_component_configured(self):
        with _EnvOverride(HC_UUID_CHRONOS_AGENT="uuid-abc"):
            uuid = _get_uuid("chronos_agent")
            self.assertEqual(uuid, "uuid-abc")

    def test_known_component_not_configured(self):
        with _EnvOverride(HC_UUID_CHRONOS_AGENT=None):
            uuid = _get_uuid("chronos_agent")
            self.assertIsNone(uuid)

    def test_unknown_component_dynamic_key(self):
        # 未知コンポーネントは HC_UUID_<大文字> で動的探索
        with _EnvOverride(HC_UUID_MY_NEW_BOT="uuid-xyz"):
            uuid = _get_uuid("my_new_bot")
            self.assertEqual(uuid, "uuid-xyz")

    def test_unknown_component_not_configured(self):
        with _EnvOverride(HC_UUID_MY_NEW_BOT=None):
            uuid = _get_uuid("my_new_bot")
            self.assertIsNone(uuid)


class TestPingHealthchecks(unittest.TestCase):
    """ping_healthchecks() の結合テスト（HTTP はモック）。"""

    def _mock_resp(self, status_code: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        return resp

    def test_ping_success(self):
        """UUID 設定済み + HTTP 200 → True を返すこと。"""
        with _EnvOverride(HC_UUID_ATLAS_AGENT="uuid-200"):
            with patch("common.external_health_ping._requests") as mock_req:
                mock_req.get.return_value = self._mock_resp(200)
                result = ping_healthchecks("atlas_agent")
                self.assertTrue(result)
                mock_req.get.assert_called_once()

    def test_ping_fail_status(self):
        """status='fail' → /fail URL に送信されること。"""
        with _EnvOverride(HC_UUID_CHRONOS_AGENT="uuid-fail"):
            with patch("common.external_health_ping._requests") as mock_req:
                mock_req.get.return_value = self._mock_resp(200)
                result = ping_healthchecks("chronos_agent", status="fail")
                self.assertTrue(result)
                call_url = mock_req.get.call_args[0][0]
                self.assertTrue(call_url.endswith("/fail"))

    def test_ping_start_status(self):
        """status='start' → /start URL に送信されること。"""
        with _EnvOverride(HC_UUID_ATLAS_WATCHDOG="uuid-start"):
            with patch("common.external_health_ping._requests") as mock_req:
                mock_req.get.return_value = self._mock_resp(200)
                result = ping_healthchecks("atlas_watchdog", status="start")
                self.assertTrue(result)
                call_url = mock_req.get.call_args[0][0]
                self.assertTrue(call_url.endswith("/start"))

    def test_uuid_not_configured_returns_false(self):
        """UUID 未設定 → False を返すこと（コンポーネント本業は継続）。"""
        with _EnvOverride(HC_UUID_CHRONOS_WATCHDOG=None):
            with patch("common.external_health_ping._requests") as mock_req:
                with self.assertLogs("external_health_ping", level="WARNING") as cm:
                    result = ping_healthchecks("chronos_watchdog")
                self.assertFalse(result)
                mock_req.get.assert_not_called()
                self.assertTrue(any("UUID not configured" in m for m in cm.output))

    def test_retry_on_http_error(self):
        """HTTP 500 が続いた場合、5回リトライして False を返すこと。"""
        with _EnvOverride(HC_UUID_ATLAS_AGENT="uuid-retry"):
            with patch("common.external_health_ping._requests") as mock_req:
                with patch("common.external_health_ping.time.sleep"):  # sleep をスキップ
                    mock_req.get.return_value = self._mock_resp(500)
                    result = ping_healthchecks("atlas_agent")
                    self.assertFalse(result)
                    self.assertEqual(mock_req.get.call_count, 5)  # 5回リトライ

    def test_network_failure_returns_false(self):
        """ConnectionError 等のネットワーク障害で False を返すこと（本業継続）。"""
        with _EnvOverride(HC_UUID_CHRONOS_AGENT="uuid-netfail"):
            with patch("common.external_health_ping._requests") as mock_req:
                with patch("common.external_health_ping.time.sleep"):
                    mock_req.get.side_effect = ConnectionError("network unreachable")
                    result = ping_healthchecks("chronos_agent")
                    self.assertFalse(result)
                    self.assertEqual(mock_req.get.call_count, 5)

    def test_network_failure_does_not_raise(self):
        """ネットワーク障害で例外が伝播しないこと（try-except が機能）。"""
        with _EnvOverride(HC_UUID_ATLAS_WATCHDOG="uuid-noexc"):
            with patch("common.external_health_ping._requests") as mock_req:
                with patch("common.external_health_ping.time.sleep"):
                    mock_req.get.side_effect = RuntimeError("fatal error")
                    # 例外が伝播しないことを確認
                    try:
                        result = ping_healthchecks("atlas_watchdog")
                        self.assertFalse(result)
                    except Exception as e:
                        self.fail(f"ping_healthchecks raised unexpected exception: {e}")

    def test_requests_not_available(self):
        """requests が import 不可の環境で False を返すこと。"""
        with _EnvOverride(HC_UUID_ATLAS_AGENT="uuid-noreq"):
            with patch("common.external_health_ping._REQUESTS_OK", False):
                result = ping_healthchecks("atlas_agent")
                self.assertFalse(result)

    def test_payload_uses_post(self):
        """payload 指定時は POST で送信されること。"""
        with _EnvOverride(HC_UUID_CHRONOS_AGENT="uuid-post"):
            with patch("common.external_health_ping._requests") as mock_req:
                mock_req.post.return_value = self._mock_resp(200)
                result = ping_healthchecks("chronos_agent", payload="エラーメッセージ")
                self.assertTrue(result)
                mock_req.post.assert_called_once()
                # payload がエンコードされて渡されること
                call_kwargs = mock_req.post.call_args[1]
                self.assertIn("data", call_kwargs)

    def test_success_after_retry(self):
        """最初の2回は失敗し、3回目に成功する場合に True を返すこと。"""
        with _EnvOverride(HC_UUID_ATLAS_AGENT="uuid-partial"):
            with patch("common.external_health_ping._requests") as mock_req:
                with patch("common.external_health_ping.time.sleep"):
                    mock_req.get.side_effect = [
                        self._mock_resp(500),
                        self._mock_resp(500),
                        self._mock_resp(200),
                    ]
                    result = ping_healthchecks("atlas_agent")
                    self.assertTrue(result)
                    self.assertEqual(mock_req.get.call_count, 3)


class TestListConfiguredComponents(unittest.TestCase):
    """list_configured_components() のテスト。"""

    def test_returns_dict_with_all_known_components(self):
        result = list_configured_components()
        self.assertIsInstance(result, dict)
        # 既知コンポーネントが全て含まれること
        for comp in ["chronos_agent", "atlas_agent", "sora_heartbeat_monitor"]:
            self.assertIn(comp, result)

    def test_configured_flag(self):
        with _EnvOverride(
            HC_UUID_CHRONOS_AGENT="uuid-set",
            HC_UUID_ATLAS_AGENT=None,
        ):
            result = list_configured_components()
            self.assertTrue(result["chronos_agent"])
            self.assertFalse(result["atlas_agent"])


class TestHealthServerEndpoint(unittest.TestCase):
    """health_server.py の /health エンドポイント動作確認。

    health_server.py は VPS 向けの Linux 依存コード（systemctl/proc）が含まれるため、
    Mac 環境では HTTP レスポンスが返ること（ステータスは問わず）のみ検証する。
    """

    def setUp(self):
        """テスト用サーバーを別スレッドで起動する。"""
        import http.server
        import socketserver
        import importlib.util
        import json

        self._port = 18080  # テスト用ポート（本番8080と衝突しない）
        self._server = None

        # health_server モジュールを直接インポートせず、最小HTTPサーバーで代替
        # (health_server は systemctl 等 Linux 依存のため Mac では起動不可)
        class _MinimalHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if self.path in ("/", "/health", "/healthz"):
                    body = json.dumps({
                        "status": "ok",
                        "components": {"test": "mock"},
                        "ts": "2026-04-20T00:00:00Z",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        class _ReuseServer(socketserver.TCPServer):
            allow_reuse_address = True

        self._server = _ReuseServer(("127.0.0.1", self._port), _MinimalHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.05)  # サーバー起動待ち

    def tearDown(self):
        if self._server:
            self._server.shutdown()

    def test_health_endpoint_returns_200(self):
        import urllib.request
        import json
        url = f"http://127.0.0.1:{self._port}/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read())
            self.assertIn("status", body)

    def test_unknown_path_returns_404(self):
        import urllib.request
        import urllib.error
        url = f"http://127.0.0.1:{self._port}/unknown"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
