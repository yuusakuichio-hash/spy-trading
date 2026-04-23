"""
tests/test_deadman_lib.py — common_v3.observability.deadman ライブラリ単体テスト

カバレッジ:
    1. beacon round-trip (write_beacon -> get_last_ping)
    2. check_and_alert dict 形状
    3. get_last_ping None fallback (ファイル不在 / コンポーネント不在)
    4. list_components 完全一致
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ライブラリ import
from common_v3.observability.deadman import (
    COMPONENTS,
    CRIT_SEC,
    WARN_SEC,
    check_and_alert,
    get_last_ping,
    list_components,
    write_beacon,
)


# ────────────────────────────────────────────────────────────────────────────
# fixture: テスト用の一時 PING_FILE を差し替える
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_ping_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PING_FILE / PING_DIR を tmp_path 配下に差し替えるフィクスチャ。"""
    ping_dir = tmp_path / "data" / "ops" / "heartbeat"
    ping_file = ping_dir / "dead_man_ping.jsonl"

    import common_v3.observability.deadman as dm
    monkeypatch.setattr(dm, "PING_DIR", ping_dir)
    monkeypatch.setattr(dm, "PING_FILE", ping_file)
    return ping_file


# ────────────────────────────────────────────────────────────────────────────
# 1. beacon round-trip
# ────────────────────────────────────────────────────────────────────────────

class TestBeaconRoundTrip:
    def test_write_creates_file(self, tmp_ping_file: Path) -> None:
        write_beacon("spy_bot")
        assert tmp_ping_file.exists(), "PING_FILE が作成されていない"

    def test_written_record_is_valid_json(self, tmp_ping_file: Path) -> None:
        write_beacon("atlas_agent")
        lines = [l for l in tmp_ping_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["component"] == "atlas_agent"
        assert "ts" in rec
        assert "hash" in rec

    def test_get_last_ping_returns_recent_timestamp(self, tmp_ping_file: Path) -> None:
        before = time.time()
        write_beacon("chronos_bot")
        after = time.time()
        ts = get_last_ping("chronos_bot")
        assert ts is not None
        assert before <= ts <= after + 1.0, f"ts={ts} が before={before}..after={after} 範囲外"

    def test_multiple_writes_returns_latest(self, tmp_ping_file: Path) -> None:
        write_beacon("spy_bot")
        time.sleep(0.05)
        write_beacon("spy_bot")
        ts1 = get_last_ping("spy_bot")

        # さらに 1 件追記して最新が変わることを確認
        time.sleep(0.05)
        write_beacon("spy_bot")
        ts2 = get_last_ping("spy_bot")

        assert ts2 is not None
        assert ts1 is not None
        assert ts2 > ts1, "後から書いた beacon が返されていない"

    def test_different_components_independent(self, tmp_ping_file: Path) -> None:
        write_beacon("spy_bot")
        time.sleep(0.05)
        write_beacon("atlas_agent")

        ts_spy = get_last_ping("spy_bot")
        ts_atlas = get_last_ping("atlas_agent")
        assert ts_spy is not None
        assert ts_atlas is not None
        # atlas は後で書いたので ts_atlas >= ts_spy
        assert ts_atlas >= ts_spy


# ────────────────────────────────────────────────────────────────────────────
# 2. check_and_alert dict 形状
# ────────────────────────────────────────────────────────────────────────────

class TestCheckAndAlertDictShape:
    def test_returns_dict_with_required_keys(self, tmp_ping_file: Path) -> None:
        with patch("common_v3.observability.deadman._send_alert"):
            result = check_and_alert()
        assert isinstance(result, dict)
        for key in ("ok", "warn", "crit", "checked_at"):
            assert key in result, f"キー '{key}' が dict に存在しない"

    def test_ok_true_when_all_fresh(self, tmp_ping_file: Path) -> None:
        """全コンポーネントのビーコンが新鮮なら ok=True。"""
        for comp in COMPONENTS:
            write_beacon(comp)
        with patch("common_v3.observability.deadman._send_alert") as mock_alert:
            result = check_and_alert()
        assert result["ok"] is True
        assert result["warn"] == []
        assert result["crit"] == []
        mock_alert.assert_not_called()

    def test_warn_when_beacon_stale_30min(self, tmp_ping_file: Path) -> None:
        """ビーコンが WARN_SEC 以上古い場合、warn リストに含まれる。"""
        for comp in COMPONENTS:
            write_beacon(comp)

        stale_time = time.time() - WARN_SEC - 10  # 30分+10秒前

        import common_v3.observability.deadman as dm
        with patch.object(dm, "get_last_ping") as mock_glp:
            def side_effect(comp: str) -> float | None:
                if comp == "spy_bot":
                    return stale_time
                return time.time()  # 他は新鮮
            mock_glp.side_effect = side_effect
            with patch.object(dm, "_send_alert"):
                result = dm.check_and_alert()

        assert "spy_bot" in result["warn"] or "spy_bot" in result["crit"]
        assert result["ok"] is False

    def test_crit_when_beacon_stale_60min(self, tmp_ping_file: Path) -> None:
        """ビーコンが CRIT_SEC 以上古い場合、crit リストに含まれる。"""
        stale_time = time.time() - CRIT_SEC - 10  # 60分+10秒前

        import common_v3.observability.deadman as dm
        with patch.object(dm, "get_last_ping") as mock_glp:
            def side_effect(comp: str) -> float | None:
                if comp == "atlas_agent":
                    return stale_time
                return time.time()
            mock_glp.side_effect = side_effect
            with patch.object(dm, "_send_alert"):
                result = dm.check_and_alert()

        assert "atlas_agent" in result["crit"]
        assert result["ok"] is False

    def test_checked_at_is_recent_epoch(self, tmp_ping_file: Path) -> None:
        before = time.time()
        with patch("common_v3.observability.deadman._send_alert"):
            result = check_and_alert()
        after = time.time()
        assert before <= result["checked_at"] <= after + 1.0

    def test_warn_and_crit_are_lists(self, tmp_ping_file: Path) -> None:
        with patch("common_v3.observability.deadman._send_alert"):
            result = check_and_alert()
        assert isinstance(result["warn"], list)
        assert isinstance(result["crit"], list)


# ────────────────────────────────────────────────────────────────────────────
# 3. get_last_ping None fallback
# ────────────────────────────────────────────────────────────────────────────

class TestGetLastPingNoneFallback:
    def test_returns_none_when_file_absent(self, tmp_ping_file: Path) -> None:
        # ファイルが作成される前
        assert not tmp_ping_file.exists()
        result = get_last_ping("spy_bot")
        assert result is None

    def test_returns_none_when_component_not_in_file(self, tmp_ping_file: Path) -> None:
        write_beacon("atlas_agent")
        result = get_last_ping("nonexistent_component_xyz")
        assert result is None

    def test_returns_none_for_empty_file(self, tmp_ping_file: Path) -> None:
        tmp_ping_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_ping_file.write_text("", encoding="utf-8")
        result = get_last_ping("spy_bot")
        assert result is None

    def test_skips_corrupt_lines(self, tmp_ping_file: Path) -> None:
        """壊れた行を含むファイルでも正常行を返す。"""
        import json
        import time as _time
        from datetime import datetime, timezone

        tmp_ping_file.parent.mkdir(parents=True, exist_ok=True)
        good_ts = datetime.now(timezone.utc).isoformat()
        with tmp_ping_file.open("w", encoding="utf-8") as f:
            f.write("not json garbage\n")
            f.write(json.dumps({"ts": good_ts, "component": "spy_bot", "hash": "abc"}) + "\n")

        result = get_last_ping("spy_bot")
        assert result is not None
        assert abs(result - datetime.fromisoformat(good_ts).timestamp()) < 1.0


# ────────────────────────────────────────────────────────────────────────────
# 4. list_components 完全一致
# ────────────────────────────────────────────────────────────────────────────

class TestListComponents:
    def test_matches_components_constant(self) -> None:
        assert list_components() == COMPONENTS

    def test_returns_copy_not_same_object(self) -> None:
        result = list_components()
        result.append("injected_component")
        # 元の COMPONENTS は変わっていないこと
        assert "injected_component" not in COMPONENTS

    def test_contains_all_expected_components(self) -> None:
        expected = [
            "spy_bot",
            "atlas_agent",
            "chronos_webhook_server",
            "chronos_traderspost_forwarder",
            "chronos_agent",
            "chronos_bot",
            "chronos_webhook_queue_reader",
        ]
        result = list_components()
        assert result == expected, f"期待={expected}, 実際={result}"
