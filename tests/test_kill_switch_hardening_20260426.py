"""tests/test_kill_switch_hardening_20260426.py

C-011 (Sprint 1 carryover) KillSwitch HIGH/MEDIUM 検証:
- H-1: _write_flag tmp 名 uuid suffix（pid 衝突 race 解消）
- H-3: deactivate audit 先書き → unlink 順序（操作 trace 保証）
- M-3: unlink 中の FileNotFoundError catch（並行 deactivate race 許容）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# H-1: tmp 名衝突回避
# ---------------------------------------------------------------------------

class TestWriteFlagTmpName:
    def test_tmp_name_includes_random_suffix(self, tmp_path, monkeypatch):
        """_write_flag が tmp 名に random suffix を含む（同一 pid 衝突回避）。"""
        import common_v3.risk.kill_switch as ks_mod

        target = tmp_path / "flag.json"
        # tmp 名生成挙動を spy
        original_replace = ks_mod.os.replace
        captured_tmp_paths = []

        def spy_replace(src, dst):
            captured_tmp_paths.append(str(src))
            return original_replace(src, dst)

        monkeypatch.setattr(ks_mod.os, "replace", spy_replace)
        ks_mod._write_flag(target, {"reason": "test1"})
        ks_mod._write_flag(target, {"reason": "test2"})

        # 2 回の tmp 名は異なる（uuid suffix で衝突回避）
        assert len(captured_tmp_paths) == 2
        assert captured_tmp_paths[0] != captured_tmp_paths[1], (
            f"H-1 regression: tmp 名が同一 → pid のみで衝突 race。"
            f"got: {captured_tmp_paths}"
        )
        # tmp 名に十分な random エントロピー (16 hex 以上) が含まれる
        # ".flag.json.tmp.{pid}.{16hex}" 形式
        for p in captured_tmp_paths:
            tail = p.rsplit(".", 1)[-1]
            assert len(tail) >= 12, f"random suffix が短すぎる: {tail}"


# ---------------------------------------------------------------------------
# H-3: audit 先書き → unlink 順序
# ---------------------------------------------------------------------------

class TestDeactivateAuditOrder:
    def test_audit_written_before_unlink(self, tmp_path, monkeypatch):
        """_deactivate_raw が audit を unlink より先に書く（操作 trace 保証）。"""
        import common_v3.risk.kill_switch as ks_mod

        monkeypatch.setattr(ks_mod, "_STATE_DIR", tmp_path)
        flag_path = tmp_path / "kill_switch.flag"
        audit_path = tmp_path / "kill_switch_audit.jsonl"
        monkeypatch.setattr(ks_mod, "FLAG_FILE", flag_path)
        monkeypatch.setattr(ks_mod, "AUDIT_FILE", audit_path)
        flag_path.write_text('{"reason":"test","activated_at":"2026-04-26"}')

        # unlink 直前で audit が既に書かれていることを確認するため、
        # unlink を spy + 呼出時の audit 内容を check
        events_at_unlink: list[bool] = []
        original_unlink = ks_mod.Path.unlink

        def spy_unlink(self, *a, **kw):
            # unlink 時点で audit が既に書かれているか
            events_at_unlink.append(audit_path.exists())
            return original_unlink(self, *a, **kw)

        monkeypatch.setattr(ks_mod.Path, "unlink", spy_unlink)

        result = ks_mod._deactivate_raw(activator="t1", reason="test_dr")
        assert result is True
        assert len(events_at_unlink) == 1
        assert events_at_unlink[0] is True, (
            "H-3 regression: unlink 時点で audit が未書き = 順序逆転"
        )


# ---------------------------------------------------------------------------
# M-3: unlink race 許容
# ---------------------------------------------------------------------------

class TestDeactivateUnlinkRace:
    def test_filenotfound_during_unlink_swallowed(self, tmp_path, monkeypatch):
        """unlink 中に並行 deactivate で flag が消えていても OSError なし。"""
        import common_v3.risk.kill_switch as ks_mod

        monkeypatch.setattr(ks_mod, "_STATE_DIR", tmp_path)
        flag_path = tmp_path / "kill_switch.flag"
        audit_path = tmp_path / "kill_switch_audit.jsonl"
        monkeypatch.setattr(ks_mod, "FLAG_FILE", flag_path)
        monkeypatch.setattr(ks_mod, "AUDIT_FILE", audit_path)
        flag_path.write_text('{"reason":"test"}')

        # unlink 直前で別プロセスが消したシナリオ shimulate
        original_unlink = ks_mod.Path.unlink

        def fake_unlink(self, *a, **kw):
            # 1 回目だけ FileNotFoundError を raise (race simulate)
            raise FileNotFoundError(2, "race")

        monkeypatch.setattr(ks_mod.Path, "unlink", fake_unlink)

        # M-3: race を例外で外に漏らさず正常終了
        result = ks_mod._deactivate_raw(activator="t2", reason="race_test")
        assert result is True, "M-3 regression: race 中の deactivate が False / exception を漏らした"
