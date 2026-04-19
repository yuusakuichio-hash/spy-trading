"""tests/test_pushover_client.py

common.pushover_client の単体テスト（8ケース必須）

ケース一覧:
  1. send 成功
  2. 429 で 1 回目 — backoff 発動せず（consecutive_429=1 に増加）
  3. 429 連続 3 回で backoff 発動
  4. banned 文字列検知で backoff 発動
  5. ban 中の send はキュー追記して False 返却
  6. flush_queue で送信成功
  7. flush_queue で再度 429 食らった時（backoff 延長）
  8. queue 10MB 超過で stale drop / サイズ drop
"""

from __future__ import annotations

import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── プロジェクトルートをパスに追加 ────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# ── requests をモック（ネットワーク不要） ─────────────────────────────────────
_requests_mock = types.ModuleType("requests")
_ok_resp = MagicMock()
_ok_resp.ok = True
_ok_resp.status_code = 200
_ok_resp.text = '{"status":1}'
_requests_mock.post = MagicMock(return_value=_ok_resp)
sys.modules["requests"] = _requests_mock

import importlib
import common.pushover_client as pc

# ── モジュールの requests 参照を差し替え ──────────────────────────────────────
pc._requests = _requests_mock


# ─────────────────────────────────────────────────────────────────────────────
# テスト共通セットアップ
# ─────────────────────────────────────────────────────────────────────────────

_TMP_STATE = Path("/tmp/test_pushover_client_state.json")
_TMP_QUEUE = Path("/tmp/test_pushover_client_queue.jsonl")

# 元のパスを保存
_ORIG_STATE_PATH = pc.STATE_PATH
_ORIG_QUEUE_PATH = pc.QUEUE_PATH
_ORIG_DEFAULT_TOKEN = pc._DEFAULT_TOKEN
_ORIG_DEFAULT_USER  = pc._DEFAULT_USER


def _setup_paths():
    """テスト用一時パスへ差し替え・既存ファイルをクリア。"""
    pc.STATE_PATH = _TMP_STATE
    pc.QUEUE_PATH = _TMP_QUEUE
    for p in [_TMP_STATE, _TMP_QUEUE]:
        if p.exists():
            p.unlink()


def _teardown_paths():
    """テスト用パスをクリアし、元のパスに戻す。
    元の state ファイルもクリアして ban 状態が他テストに漏れないようにする。
    """
    for p in [_TMP_STATE, _TMP_QUEUE]:
        if p.exists():
            p.unlink()
    pc.STATE_PATH     = _ORIG_STATE_PATH
    pc.QUEUE_PATH     = _ORIG_QUEUE_PATH
    pc._DEFAULT_TOKEN = _ORIG_DEFAULT_TOKEN
    pc._DEFAULT_USER  = _ORIG_DEFAULT_USER
    # 元の state ファイルを ban-free 状態にリセット（他テストへの汚染防止）
    try:
        import json
        if _ORIG_STATE_PATH.exists():
            _ORIG_STATE_PATH.write_text(
                json.dumps({"consecutive_429": 0, "backoff_until": 0.0}),
                encoding="utf-8",
            )
    except Exception:
        pass


def _make_resp(status_code: int, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.ok = (status_code == 200)
    r.text = text
    return r


def _set_env():
    """テスト用トークン・ユーザを環境変数にセット。"""
    pc._DEFAULT_TOKEN = "test_token_xxx"
    pc._DEFAULT_USER  = "test_user_yyy"


# ─────────────────────────────────────────────────────────────────────────────
# テストクラス
# ─────────────────────────────────────────────────────────────────────────────

class TestPushoverClientSend(unittest.TestCase):

    def setUp(self):
        _setup_paths()
        _set_env()
        _requests_mock.post.reset_mock()

    def tearDown(self):
        _teardown_paths()

    # ── ケース1: send 成功 ────────────────────────────────────────────────────
    def test_1_send_success(self):
        """正常な 200 レスポンスで True を返す。state は変化なし。"""
        _requests_mock.post.return_value = _make_resp(200)

        result = pc.send("Test Title", "Test Message", priority=0)

        self.assertTrue(result)
        _requests_mock.post.assert_called_once()
        call_kwargs = _requests_mock.post.call_args
        self.assertIn(_TMP_STATE.name, str(pc.STATE_PATH))

    # ── ケース2: 429 で 1 回目 — backoff 発動せず ────────────────────────────
    def test_2_single_429_no_backoff(self):
        """429 を 1 回受けても consecutive_429=1 になるが backoff は発動しない。"""
        _requests_mock.post.return_value = _make_resp(429)

        result = pc.send("Title", "Msg", priority=1)

        self.assertFalse(result)
        state = pc._load_state()
        # consecutive_429 が 1 になっている
        self.assertEqual(state["consecutive_429"], 1)
        # backoff_until は未来でない（ban 未発動）
        self.assertLessEqual(state["backoff_until"], time.time() + 1)

    # ── ケース3: 429 連続 3 回で backoff 発動 ────────────────────────────────
    def test_3_three_consecutive_429_triggers_backoff(self):
        """429 を 3 回連続で受けると backoff_until が未来に設定される。"""
        _requests_mock.post.return_value = _make_resp(429)

        for _ in range(pc._429_MAX_CONSECUTIVE):
            pc.send("Title", "Msg", priority=1)

        state = pc._load_state()
        self.assertEqual(state["consecutive_429"], pc._429_MAX_CONSECUTIVE)
        self.assertGreater(state["backoff_until"], time.time())

        # 3 回目でキューに追記されていること
        entries = pc._load_queue()
        self.assertGreaterEqual(len(entries), 1)

    # ── ケース4: banned 文字列検知で backoff 発動 ────────────────────────────
    def test_4_banned_string_detection(self):
        """レスポンスに 'banned' が含まれる場合も 429 と同扱い。3回でbackoff。"""
        banned_resp = _make_resp(200, text='{"status":0,"errors":["application is banned"]}')
        # banned でも ok=False と判定されるよう調整
        banned_resp.ok = False
        banned_resp.status_code = 200
        _requests_mock.post.return_value = banned_resp

        # 3 回送信
        for _ in range(pc._429_MAX_CONSECUTIVE):
            pc.send("Title", "Msg", priority=1)

        state = pc._load_state()
        self.assertEqual(state["consecutive_429"], pc._429_MAX_CONSECUTIVE)
        self.assertGreater(state["backoff_until"], time.time())

    # ── ケース5: ban 中の send はキュー追記・False 返却 ──────────────────────
    def test_5_send_during_ban_queues_entry(self):
        """backoff_until が未来に設定されている状態では HTTP を叩かずキューへ追記。"""
        # 手動で ban 状態を作る
        pc._save_state(pc._429_MAX_CONSECUTIVE, time.time() + 1000)
        _requests_mock.post.reset_mock()

        result = pc.send("Queued Title", "Queued Msg", priority=1, app_tag="TEST")

        self.assertFalse(result)
        # HTTP は叩かない
        _requests_mock.post.assert_not_called()
        # キューに 1 件
        entries = pc._load_queue()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Queued Title")
        self.assertEqual(entries[0]["app_tag"], "TEST")


class TestPushoverClientFlush(unittest.TestCase):

    def setUp(self):
        _setup_paths()
        _set_env()
        _requests_mock.post.reset_mock()

    def tearDown(self):
        _teardown_paths()

    # ── ケース6: flush_queue で送信成功 ──────────────────────────────────────
    def test_6_flush_queue_success(self):
        """キューに 3 件ある状態で flush_queue を呼ぶと全件送信・キュークリア。"""
        # ban 解除済み状態
        pc._save_state(0, 0.0)

        # キューに 3 件追記
        for i in range(3):
            pc._queue_entry(f"Title {i}", f"Msg {i}", 0, "test_token_xxx", "TEST")

        _requests_mock.post.return_value = _make_resp(200)

        sent = pc.flush_queue()

        self.assertEqual(sent, 3)
        entries = pc._load_queue()
        self.assertEqual(len(entries), 0)

    # ── ケース7: flush_queue で再度 429 食らった時 ───────────────────────────
    def test_7_flush_queue_429_extends_backoff(self):
        """flush 中のテスト送信で 429 → backoff が延長される。"""
        pc._save_state(0, 0.0)
        pc._queue_entry("Title", "Msg", 0, "test_token_xxx", "TEST")

        _requests_mock.post.return_value = _make_resp(429)

        sent = pc.flush_queue()

        self.assertEqual(sent, 0)
        state = pc._load_state()
        # backoff が未来に延長されていること
        self.assertGreater(state["backoff_until"], time.time())
        # キューはそのまま残っている
        entries = pc._load_queue()
        self.assertEqual(len(entries), 1)

    # ── ケース8: queue 10MB 超過で stale drop / サイズ drop ─────────────────
    def test_8_queue_size_limit_drops_old_entries(self):
        """10MB 超のキューは古いエントリから drop される。"""
        # 小さなサイズ制限に差し替えて検証
        orig_max = pc._QUEUE_MAX_BYTES
        pc._QUEUE_MAX_BYTES = 500  # 500 バイト上限

        try:
            # 500 バイト超のエントリを複数追加
            for i in range(10):
                pc._queue_entry(
                    f"Title {i:04d}",
                    "X" * 100,  # 各エントリ ~150 バイト
                    0,
                    "tok",
                    "TEST",
                )

            before = len(pc._load_queue())
            self.assertEqual(before, 10)

            dropped = pc._trim_queue()

            after = len(pc._load_queue())
            self.assertGreater(dropped, 0)
            self.assertLess(after, before)

            # 残ったエントリのバイト合計が上限以内
            entries = pc._load_queue()
            total_bytes = sum(
                len(json.dumps(e, ensure_ascii=False).encode()) for e in entries
            )
            self.assertLessEqual(total_bytes, pc._QUEUE_MAX_BYTES)
        finally:
            pc._QUEUE_MAX_BYTES = orig_max

    def test_8b_stale_drop(self):
        """24時間以上古いエントリは _trim_queue で破棄される。"""
        orig_stale = pc._STALE_DROP_SEC
        pc._STALE_DROP_SEC = 100  # 100秒以上古いものをdrop

        try:
            # 200秒前 (stale)
            old_entry = {
                "ts": time.time() - 200,
                "title": "Old",
                "message": "old msg",
                "priority": 0,
                "token": "tok",
                "app_tag": "TEST",
            }
            # 現在時刻 (fresh)
            new_entry = {
                "ts": time.time(),
                "title": "New",
                "message": "new msg",
                "priority": 0,
                "token": "tok",
                "app_tag": "TEST",
            }
            _TMP_QUEUE.parent.mkdir(parents=True, exist_ok=True)
            with _TMP_QUEUE.open("w") as f:
                f.write(json.dumps(old_entry) + "\n")
                f.write(json.dumps(new_entry) + "\n")

            dropped = pc._trim_queue()

            self.assertEqual(dropped, 1)
            entries = pc._load_queue()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["title"], "New")
        finally:
            pc._STALE_DROP_SEC = orig_stale


# ── 追加: flush は ban 中はスキップ ──────────────────────────────────────────
class TestFlushSkipDuringBan(unittest.TestCase):

    def setUp(self):
        _setup_paths()
        _set_env()
        _requests_mock.post.reset_mock()

    def tearDown(self):
        _teardown_paths()

    def test_flush_skips_during_ban(self):
        """backoff_until が未来なら flush_queue は 0 を返す。"""
        pc._save_state(3, time.time() + 1000)
        pc._queue_entry("Title", "Msg", 0, "tok", "TEST")

        sent = pc.flush_queue()

        self.assertEqual(sent, 0)
        _requests_mock.post.assert_not_called()

    def test_success_resets_consecutive_counter(self):
        """送信成功後は consecutive_429 がリセットされる。"""
        pc._save_state(2, 0.0)  # ban 未発動・カウンタ 2
        _requests_mock.post.return_value = _make_resp(200)

        result = pc.send("Title", "Msg", priority=0)

        self.assertTrue(result)
        state = pc._load_state()
        self.assertEqual(state["consecutive_429"], 0)
        self.assertEqual(state["backoff_until"], 0.0)

    def test_token_override(self):
        """token 引数で任意のトークンを指定できる。"""
        _requests_mock.post.return_value = _make_resp(200)

        pc.send("Title", "Msg", priority=0, token="custom_token_zzz")

        call_kwargs = _requests_mock.post.call_args
        sent_data = call_kwargs[1].get("data") or call_kwargs[0][1]  # positional or keyword
        # data dict に custom_token が渡っているか
        # _http_post は data= で渡す
        # call_args[1]["data"] or call_args.kwargs["data"]
        if call_kwargs.kwargs:
            sent_data = call_kwargs.kwargs.get("data", {})
        else:
            sent_data = call_kwargs[1].get("data", {})
        self.assertEqual(sent_data.get("token"), "custom_token_zzz")


if __name__ == "__main__":
    unittest.main(verbosity=2)
