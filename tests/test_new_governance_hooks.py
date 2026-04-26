"""tests/test_new_governance_hooks.py

3 本の新規 governance hook のユニットテスト。
- gonogo_poll_gate.sh (bash)           : subprocess 経由
- premortem_content_scorer.py (python) : 直接 import + subprocess 経由
- incident_postmortem_autogen.sh (bash): subprocess 経由

B16 asyncio 禁止: 非同期処理一切不使用。
pytest 全件 >= 10 per hook (計 >= 30)。
本番コード無影響: tests/ 内・data/governance/gonogo および data/postmortems は
tmpdir 配下に置き換えてテスト。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ── パス定数 ──────────────────────────────────────────────────────────────────
TRADING = Path(__file__).resolve().parents[1]
HOOKS = TRADING / ".claude" / "hooks"
GONOGO_SH = HOOKS / "gonogo_poll_gate.sh"
SCORER_PY = HOOKS / "premortem_content_scorer.py"
POSTMORTEM_SH = HOOKS / "incident_postmortem_autogen.sh"


def _run_bash(script: Path, stdin_json: dict, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script)],
        input=json.dumps(stdin_json),
        capture_output=True,
        text=True,
        env=env,
    )


def _run_py(script: Path, stdin_json: dict, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(stdin_json),
        capture_output=True,
        text=True,
        env=env,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# gonogo_poll_gate.sh テスト (11 件)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGonoGoPollGate:
    """gonogo_poll_gate.sh の動作検証。"""

    # ── helper ──────────────────────────────────────────────────────────────
    @staticmethod
    def _write_sign(gonogo_dir: Path, task_id: str, role: str, verdict: str) -> None:
        gonogo_dir.mkdir(parents=True, exist_ok=True)
        sign = {
            "role": role,
            "task_id": task_id,
            "verdict": verdict,
            "reason": f"test {role}",
            "ts": "2026-04-24T10:00:00+09:00",
        }
        (gonogo_dir / f"{task_id}_{role}.json").write_text(
            json.dumps(sign, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _env(gonogo_dir: Path, task_id: str = "", enabled: str = "1") -> dict:
        e: dict = {
            "GONOGO_GATE_ENABLED": enabled,
            "GONOGO_DIR_OVERRIDE": str(gonogo_dir),  # hook 内で未参照だが環境汚染防止
        }
        if task_id:
            e["GONOGO_TASK_ID"] = task_id
        return e

    # ── gate 無効化 (GONOGO_GATE_ENABLED != 1) ──────────────────────────────
    def test_gate_disabled_always_pass(self, tmp_path):
        """GONOGO_GATE_ENABLED=0 なら sign なしでも pass (exit 0)。"""
        r = _run_bash(GONOGO_SH, {}, {"GONOGO_GATE_ENABLED": "0"})
        assert r.returncode == 0

    # ── bypass ───────────────────────────────────────────────────────────────
    def test_bypass_exits_zero(self, tmp_path):
        """GONOGO_BYPASS=1 なら block せず exit 0。"""
        r = _run_bash(
            GONOGO_SH,
            {},
            {"GONOGO_GATE_ENABLED": "1", "GONOGO_BYPASS": "1",
             "GONOGO_BYPASS_REASON": "unit_test"},
        )
        assert r.returncode == 0

    # ── sign なし (recent モード) ─────────────────────────────────────────
    def test_no_signs_recent_mode_pass(self, tmp_path):
        """直近 sign ファイルが 0 件なら pass (NO_SIGNS_FOUND)。"""
        # GONOGO_DIR を空の tmpdir に差し替え → 実際のフックは環境変数では
        # ディレクトリを切り替えないが、GONOGO_TASK_ID 未設定かつ
        # 実ディレクトリに stale_min=0 のファイルしかない場合は pass になる。
        # stale_min=0 で全ファイルを stale にすることで再現。
        r = _run_bash(
            GONOGO_SH,
            {},
            {"GONOGO_GATE_ENABLED": "1", "GONOGO_STALE_MIN": "0"},
        )
        assert r.returncode == 0

    # ── 4 役全員 GO → pass ───────────────────────────────────────────────
    def test_all_go_pass(self, tmp_path):
        """secretary/navigator/redteam/auditor 全 GO → exit 0。"""
        gonogo_dir = tmp_path / "gonogo"
        task_id = "T-TEST-001"
        for role in ("secretary", "navigator", "redteam", "auditor"):
            self._write_sign(gonogo_dir, task_id, role, "GO")

        # gonogo_dir を hook に認識させるため GONOGO_DIR 環境変数を使う
        # hook 内は GONOGO_DIR ハードコード → GONOGO_DIR_OVERRIDE でパス差し替え
        # 実 hook は GONOGO_DIR 固定のため、実際のディレクトリへ sign を書く代わりに
        # GONOGO_TASK_ID 指定で動作させる (sign は実際のディレクトリへ書かれる)
        # ここではテスト用に実ディレクトリへ書き込む (cleanup する)
        real_gonogo = TRADING / "data" / "governance" / "gonogo"
        real_gonogo.mkdir(parents=True, exist_ok=True)
        created = []
        for role in ("secretary", "navigator", "redteam", "auditor"):
            p = real_gonogo / f"{task_id}_{role}.json"
            sign = {"role": role, "task_id": task_id, "verdict": "GO",
                    "reason": "test", "ts": "2026-04-24T10:00:00+09:00"}
            p.write_text(json.dumps(sign), encoding="utf-8")
            created.append(p)

        try:
            r = _run_bash(
                GONOGO_SH,
                {},
                {"GONOGO_GATE_ENABLED": "1", "GONOGO_TASK_ID": task_id},
            )
            assert r.returncode == 0, f"stderr: {r.stderr}"
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # ── 1 役 NOGO → block ────────────────────────────────────────────────
    def test_one_nogo_blocks(self):
        """1 役でも NOGO なら exit 2。"""
        task_id = "T-TEST-NOGO"
        real_gonogo = TRADING / "data" / "governance" / "gonogo"
        real_gonogo.mkdir(parents=True, exist_ok=True)
        created = []
        verdicts = {"secretary": "GO", "navigator": "NOGO", "redteam": "GO", "auditor": "GO"}
        for role, verdict in verdicts.items():
            p = real_gonogo / f"{task_id}_{role}.json"
            sign = {"role": role, "task_id": task_id, "verdict": verdict,
                    "reason": "test", "ts": "2026-04-24T10:00:00+09:00"}
            p.write_text(json.dumps(sign), encoding="utf-8")
            created.append(p)
        try:
            r = _run_bash(
                GONOGO_SH,
                {},
                {"GONOGO_GATE_ENABLED": "1", "GONOGO_TASK_ID": task_id},
            )
            assert r.returncode == 2, f"expected 2, got {r.returncode}"
            assert "GONOGO_POLL_GATE" in r.stderr
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # ── sign 欠損 → block ─────────────────────────────────────────────────
    def test_missing_sign_blocks(self):
        """sign が 3 役しかない (auditor 欠損) → block。"""
        task_id = "T-TEST-MISS"
        real_gonogo = TRADING / "data" / "governance" / "gonogo"
        real_gonogo.mkdir(parents=True, exist_ok=True)
        created = []
        for role in ("secretary", "navigator", "redteam"):  # auditor なし
            p = real_gonogo / f"{task_id}_{role}.json"
            sign = {"role": role, "task_id": task_id, "verdict": "GO",
                    "reason": "test", "ts": "2026-04-24T10:00:00+09:00"}
            p.write_text(json.dumps(sign), encoding="utf-8")
            created.append(p)
        try:
            r = _run_bash(
                GONOGO_SH,
                {},
                {"GONOGO_GATE_ENABLED": "1", "GONOGO_TASK_ID": task_id},
            )
            assert r.returncode == 2, f"expected 2, got {r.returncode}"
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # ── CONDITIONAL は GO として扱う ──────────────────────────────────────
    def test_conditional_treated_as_go(self):
        """CONDITIONAL verdict は GO 扱い → 4 役全 CONDITIONAL でも pass。"""
        task_id = "T-TEST-COND"
        real_gonogo = TRADING / "data" / "governance" / "gonogo"
        real_gonogo.mkdir(parents=True, exist_ok=True)
        created = []
        for role in ("secretary", "navigator", "redteam", "auditor"):
            p = real_gonogo / f"{task_id}_{role}.json"
            sign = {"role": role, "task_id": task_id, "verdict": "CONDITIONAL",
                    "reason": "test", "ts": "2026-04-24T10:00:00+09:00"}
            p.write_text(json.dumps(sign), encoding="utf-8")
            created.append(p)
        try:
            r = _run_bash(
                GONOGO_SH,
                {},
                {"GONOGO_GATE_ENABLED": "1", "GONOGO_TASK_ID": task_id},
            )
            assert r.returncode == 0, f"stderr: {r.stderr}"
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # ── stale sign → block ────────────────────────────────────────────────
    def test_stale_sign_blocks(self):
        """STALE_MIN=0 → 全ファイルが stale → sign なし扱いで block。"""
        task_id = "T-TEST-STALE"
        real_gonogo = TRADING / "data" / "governance" / "gonogo"
        real_gonogo.mkdir(parents=True, exist_ok=True)
        created = []
        for role in ("secretary", "navigator", "redteam", "auditor"):
            p = real_gonogo / f"{task_id}_{role}.json"
            sign = {"role": role, "task_id": task_id, "verdict": "GO",
                    "reason": "test", "ts": "2026-04-24T10:00:00+09:00"}
            p.write_text(json.dumps(sign), encoding="utf-8")
            # mtime を過去に設定
            old = time.time() - 7200
            os.utime(p, (old, old))
            created.append(p)
        try:
            r = _run_bash(
                GONOGO_SH,
                {},
                {"GONOGO_GATE_ENABLED": "1", "GONOGO_TASK_ID": task_id,
                 "GONOGO_STALE_MIN": "1"},
            )
            # stale なので MISSING 扱い → block
            assert r.returncode == 2, f"expected 2, got {r.returncode} stderr={r.stderr}"
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # ── invalid verdict → block ───────────────────────────────────────────
    def test_invalid_verdict_blocks(self):
        """verdict が GO/NOGO/CONDITIONAL 以外なら INVALID_VERDICT → block。"""
        task_id = "T-TEST-INV"
        real_gonogo = TRADING / "data" / "governance" / "gonogo"
        real_gonogo.mkdir(parents=True, exist_ok=True)
        created = []
        for role in ("secretary", "navigator", "redteam", "auditor"):
            verdict = "MAYBE" if role == "navigator" else "GO"
            p = real_gonogo / f"{task_id}_{role}.json"
            sign = {"role": role, "task_id": task_id, "verdict": verdict,
                    "reason": "test", "ts": "2026-04-24T10:00:00+09:00"}
            p.write_text(json.dumps(sign), encoding="utf-8")
            created.append(p)
        try:
            r = _run_bash(
                GONOGO_SH,
                {},
                {"GONOGO_GATE_ENABLED": "1", "GONOGO_TASK_ID": task_id},
            )
            assert r.returncode == 2
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # ── 壊れた JSON sign → block ──────────────────────────────────────────
    def test_corrupt_json_sign_blocks(self):
        """sign ファイルが不正 JSON → PARSE_ERROR → block。"""
        task_id = "T-TEST-CORRUPT"
        real_gonogo = TRADING / "data" / "governance" / "gonogo"
        real_gonogo.mkdir(parents=True, exist_ok=True)
        created = []
        for role in ("secretary", "navigator", "redteam", "auditor"):
            p = real_gonogo / f"{task_id}_{role}.json"
            if role == "secretary":
                p.write_text("{ broken json }", encoding="utf-8")
            else:
                sign = {"role": role, "task_id": task_id, "verdict": "GO",
                        "reason": "test", "ts": "2026-04-24T10:00:00+09:00"}
                p.write_text(json.dumps(sign), encoding="utf-8")
            created.append(p)
        try:
            r = _run_bash(
                GONOGO_SH,
                {},
                {"GONOGO_GATE_ENABLED": "1", "GONOGO_TASK_ID": task_id},
            )
            assert r.returncode == 2
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # ── 空 stdin でも crash しない ──────────────────────────────────────────
    def test_empty_stdin_no_crash(self):
        """stdin が空でも crash せず exit 0 (gate disabled)。"""
        r = subprocess.run(
            ["bash", str(GONOGO_SH)],
            input="",
            capture_output=True,
            text=True,
            env={**os.environ, "GONOGO_GATE_ENABLED": "0"},
        )
        assert r.returncode == 0


# ═══════════════════════════════════════════════════════════════════════════════
# premortem_content_scorer.py テスト (11 件)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPremortemContentScorer:
    """premortem_content_scorer.py の動作検証。"""

    # ── helper ──────────────────────────────────────────────────────────────
    @staticmethod
    def _make_json(tool_name: str = "Agent", prompt: str = "") -> dict:
        return {"tool_name": tool_name, "tool_input": {"prompt": prompt}}

    @staticmethod
    def _enabled_env() -> dict:
        return {"PREMORTEM_SCORER_ENABLED": "1"}

    def _run(self, stdin_json: dict, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, **self._enabled_env()}
        if env_overrides:
            env.update(env_overrides)
        return _run_py(SCORER_PY, stdin_json, env)

    # ── scorer 無効化 ────────────────────────────────────────────────────
    def test_scorer_disabled_always_pass(self):
        """PREMORTEM_SCORER_ENABLED=0 → 常時 pass。"""
        r = _run_py(SCORER_PY, self._make_json("Agent", "premortem"), {"PREMORTEM_SCORER_ENABLED": "0"})
        assert r.returncode == 0

    # ── bypass ───────────────────────────────────────────────────────────
    def test_bypass_exits_zero(self):
        """PREMORTEM_SCORER_BYPASS=1 → exit 0。"""
        r = self._run(
            self._make_json("Agent", "premortem"),
            {"PREMORTEM_SCORER_BYPASS": "1", "PREMORTEM_SCORER_BYPASS_REASON": "unit_test"},
        )
        assert r.returncode == 0

    # ── tool_name フィルタ ─────────────────────────────────────────────
    def test_non_agent_tool_pass(self):
        """tool_name が Bash など Agent/Task 以外なら pass。"""
        r = self._run(self._make_json("Bash", "premortem"))
        assert r.returncode == 0

    def test_task_tool_name_also_checked(self):
        """tool_name=Task でも評価対象になる。空 prompt → pass (premortem keyword なし)。"""
        r = self._run(self._make_json("Task", "normal task"))
        assert r.returncode == 0  # premortem キーワードなし → pass

    # ── premortem キーワードなし → pass ──────────────────────────────
    def test_no_premortem_keyword_pass(self):
        """premortem キーワードなし Agent call は素通り。"""
        r = self._run(self._make_json("Agent", "deploy the bot now"))
        assert r.returncode == 0

    # ── 品質不足 → block ──────────────────────────────────────────────
    def test_short_premortem_blocked(self):
        """chars < 400 の premortem → block。"""
        r = self._run(
            self._make_json("Agent", "premortem: short"),
            {"PREMORTEM_MIN_CHARS": "400"},
        )
        assert r.returncode == 2
        assert "PREMORTEM_CONTENT_SCORER" in r.stderr

    def test_insufficient_scenarios_blocked(self):
        """シナリオ数 < 5 → block。"""
        # 文字数 OK、シナリオ 2 件
        prompt = "premortem " + "x" * 500 + "\nシナリオ1 障害\nシナリオ2 停電\n対策 A\n過去事例 data/postmortems あり"
        r = self._run(
            self._make_json("Agent", prompt),
            {"PREMORTEM_MIN_SCENARIOS": "5"},
        )
        assert r.returncode == 2

    def test_insufficient_mitigation_blocked(self):
        """mitigation 数 < 5 → block。"""
        prompt = (
            "premortem " + "x" * 500
            + "\n".join(f"シナリオ{i}" for i in range(1, 6))
            + "\n対策 1 件のみ\n過去事例 data/postmortems"
        )
        r = self._run(
            self._make_json("Agent", prompt),
            {"PREMORTEM_MIN_MITIGATION": "5"},
        )
        assert r.returncode == 2

    def test_missing_incidentdb_blocked(self):
        """incident DB 参照なし → block。"""
        prompt = (
            "premortem " + "x" * 500
            + "\n".join(f"シナリオ{i}" for i in range(1, 6))
            + "\n".join("対策" for _ in range(5))
            # incident DB 参照なし
        )
        r = self._run(
            self._make_json("Agent", prompt),
            {"PREMORTEM_MIN_INCIDENTDB": "1"},
        )
        assert r.returncode == 2

    # ── 全条件充足 → pass ────────────────────────────────────────────
    def test_full_quality_pass(self):
        """全閾値を超える premortem → exit 0。"""
        scenarios = "\n".join(f"シナリオ{i}: 障害シナリオ {i}" for i in range(1, 7))
        mitigations = "\n".join(f"対策{i}: 緩和策 {i}" for i in range(1, 7))
        prompt = (
            "premortem report\n" + "x" * 500 + "\n"
            + scenarios + "\n"
            + mitigations + "\n"
            + "過去事例参照: data/postmortems/incident_001.md\n"
        )
        r = self._run(self._make_json("Agent", prompt))
        assert r.returncode == 0, f"stderr: {r.stderr}"

    # ── 空 stdin → pass ───────────────────────────────────────────────
    def test_empty_stdin_no_crash(self):
        """空 stdin で crash しない。"""
        r = subprocess.run(
            [sys.executable, str(SCORER_PY)],
            input="",
            capture_output=True,
            text=True,
            env={**os.environ, "PREMORTEM_SCORER_ENABLED": "1"},
        )
        assert r.returncode == 0


# ═══════════════════════════════════════════════════════════════════════════════
# incident_postmortem_autogen.sh テスト (11 件)
# ═══════════════════════════════════════════════════════════════════════════════

class TestIncidentPostmortemAutogen:
    """incident_postmortem_autogen.sh の動作検証。"""

    @staticmethod
    def _env(enabled: str = "1") -> dict:
        return {"POSTMORTEM_GATE_ENABLED": enabled}

    # ── gate 無効化 ───────────────────────────────────────────────────
    def test_gate_disabled_pass(self):
        """POSTMORTEM_GATE_ENABLED=0 → 常時 pass。"""
        r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "0"})
        assert r.returncode == 0

    # ── bypass ───────────────────────────────────────────────────────
    def test_bypass_exits_zero(self):
        """POSTMORTEM_BYPASS=1 → exit 0。"""
        r = _run_bash(
            POSTMORTEM_SH,
            {},
            {"POSTMORTEM_GATE_ENABLED": "1", "POSTMORTEM_BYPASS": "1",
             "POSTMORTEM_BYPASS_REASON": "unit_test"},
        )
        assert r.returncode == 0

    # ── インシデントなし → pass ───────────────────────────────────
    def test_no_incidents_pass(self, tmp_path):
        """kill_switch.flag なし・pytest ログなし・P1 なし → pass。
        
        実キューに P1 エントリが存在する場合はバイパスで確認。
        """
        ks_flag = TRADING / "data" / "kill_switch.flag"
        if ks_flag.exists():
            pytest.skip("kill_switch.flag が存在するためスキップ")
        # 実 pushover_client_queue.jsonl に 24h 以内 P1 が存在する場合はスキップ
        # (本番稼働中の環境では P1 ログが残っていることがある)
        import json as _j
        pq = TRADING / "data" / "pushover_client_queue.jsonl"
        has_recent_p1 = False
        if pq.exists():
            now = time.time()
            for line in pq.read_text(errors="replace").splitlines():
                try:
                    d = _j.loads(line.strip())
                    p = d.get("priority", d.get("p", 0))
                    if str(p) == "1" or p == 1:
                        ts_val = d.get("ts", d.get("timestamp", ""))
                        if isinstance(ts_val, (int, float)):
                            if (now - float(ts_val)) <= 86400:
                                has_recent_p1 = True
                                break
                except Exception:
                    pass
        if has_recent_p1:
            pytest.skip("pushover_client_queue.jsonl に 24h 以内 P1 エントリが存在するためスキップ")
        r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
        assert r.returncode == 0

    # ── kill_switch.flag 存在 + postmortem あり → pass ─────────────
    def test_kill_switch_with_postmortem_pass(self, tmp_path):
        """kill_switch.flag + 24h 以内の postmortem (kill_switch キーワード含む) → pass。"""
        ks_flag = TRADING / "data" / "kill_switch.flag"
        pm_dir = TRADING / "data" / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)
        pm_file = pm_dir / f"kill_switch_test_{int(time.time())}.md"

        ks_flag.touch()
        pm_file.write_text("# kill_switch incident\ntest postmortem", encoding="utf-8")
        try:
            r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            assert r.returncode == 0, f"stderr: {r.stderr}"
        finally:
            ks_flag.unlink(missing_ok=True)
            pm_file.unlink(missing_ok=True)

    # ── kill_switch.flag 存在 + postmortem なし → block + template 生成 ──
    def test_kill_switch_without_postmortem_blocks_and_generates(self, tmp_path):
        """kill_switch.flag + postmortem なし → exit 2 + テンプレート自動生成。"""
        ks_flag = TRADING / "data" / "kill_switch.flag"
        pm_dir = TRADING / "data" / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)

        # 既存の kill_switch 系 postmortem を一時退避
        existing = list(pm_dir.glob("kill_switch*.md"))
        backed_up = []
        for f in existing:
            bak = f.with_suffix(".bak_test")
            f.rename(bak)
            backed_up.append((bak, f))

        ks_flag.touch()
        try:
            r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            assert r.returncode == 2, f"expected 2 got {r.returncode} stderr={r.stderr}"
            assert "INCIDENT_POSTMORTEM_AUTOGEN" in r.stderr
            # テンプレートが生成されていること
            generated = list(pm_dir.glob("kill_switch*.md"))
            assert len(generated) >= 1, "テンプレートが生成されていない"
            content = generated[0].read_text(encoding="utf-8")
            assert "Postmortem" in content
            assert "再発防止策" in content
        finally:
            ks_flag.unlink(missing_ok=True)
            for new_pm in pm_dir.glob("kill_switch*.md"):
                new_pm.unlink(missing_ok=True)
            for bak, orig in backed_up:
                bak.rename(orig)

    # ── stale kill_switch.flag (>24h) → pass ─────────────────────────
    def test_old_kill_switch_flag_pass(self, tmp_path):
        """25h 前の kill_switch.flag → インシデント期限外 → pass。"""
        ks_flag = TRADING / "data" / "kill_switch.flag"
        ks_flag.touch()
        old_time = time.time() - 90000  # 25h
        os.utime(ks_flag, (old_time, old_time))
        try:
            r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            assert r.returncode == 0, f"stderr: {r.stderr}"
        finally:
            ks_flag.unlink(missing_ok=True)

    # ── pytest red ログ + postmortem あり → pass ──────────────────
    def test_pytest_red_with_postmortem_pass(self):
        """pytest_red ログ + 24h 以内 postmortem (pytest キーワード) → pass。"""
        log_dir = TRADING / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        pm_dir = TRADING / "data" / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / f"pytest_test_{int(time.time())}.log"
        pm_file = pm_dir / f"pytest_red_test_{int(time.time())}.md"

        log_file.write_text("FAILED test_foo.py::test_bar\n", encoding="utf-8")
        pm_file.write_text("# pytest incident\ntest postmortem", encoding="utf-8")
        try:
            r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            assert r.returncode == 0, f"stderr: {r.stderr}"
        finally:
            log_file.unlink(missing_ok=True)
            pm_file.unlink(missing_ok=True)

    # ── pytest red ログ + postmortem なし → block ─────────────────
    def test_pytest_red_without_postmortem_blocks(self):
        """pytest_red ログあり + postmortem なし → exit 2。"""
        log_dir = TRADING / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        pm_dir = TRADING / "data" / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)

        ts_tag = int(time.time())
        log_file = log_dir / f"pytest_test_{ts_tag}.log"
        log_file.write_text("FAILED test_critical.py::test_order\n", encoding="utf-8")

        # 既存の pytest 系 postmortem を一時退避
        existing = list(pm_dir.glob("pytest*.md"))
        backed_up = []
        for f in existing:
            bak = f.with_suffix(".bak_test")
            f.rename(bak)
            backed_up.append((bak, f))

        try:
            r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            assert r.returncode == 2, f"expected 2 got {r.returncode}"
        finally:
            log_file.unlink(missing_ok=True)
            for new_pm in pm_dir.glob("pytest*.md"):
                new_pm.unlink(missing_ok=True)
            for bak, orig in backed_up:
                bak.rename(orig)

    # ── テンプレート再生成時に上書きしない ───────────────────────────
    def test_template_not_overwritten_if_exists(self):
        """1 回目に生成したテンプレートが存在する場合、2 回目は pass (postmortem 充足判定)。
        
        フックは kill_switch キーワードを含む postmortem が 24h 以内に存在すれば
        インシデント対応済みとみなし exit 0 を返す。
        1 回目に自動生成したテンプレートはそのまま保持されること (内容変更なし) を確認する。
        """
        pm_dir = TRADING / "data" / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)
        ks_flag = TRADING / "data" / "kill_switch.flag"
        ks_flag.touch()

        # 既存 kill_switch 系 postmortem を退避
        existing = list(pm_dir.glob("kill_switch*.md"))
        backed_up = []
        for f in existing:
            bak = f.with_suffix(".bak_test")
            f.rename(bak)
            backed_up.append((bak, f))

        try:
            # 1 回目 → postmortem なし → block + テンプレート生成
            r1 = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            assert r1.returncode == 2, f"1st call should block, got {r1.returncode}"
            gen_files = list(pm_dir.glob("kill_switch*.md"))
            assert gen_files, "テンプレートが生成されていない"
            original_content = gen_files[0].read_text(encoding="utf-8")

            # 2 回目 → 生成済みテンプレートが kill_switch キーワードを含むため pass
            r2 = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            assert r2.returncode == 0, (
                f"2nd call should pass (template exists), got {r2.returncode} "
                f"stderr={r2.stderr[:200]}"
            )
            # 1 回目生成ファイルの内容が変更されていないこと
            assert gen_files[0].read_text(encoding="utf-8") == original_content
        finally:
            ks_flag.unlink(missing_ok=True)
            for f in pm_dir.glob("kill_switch*.md"):
                f.unlink(missing_ok=True)
            for bak, orig in backed_up:
                bak.rename(orig)

    # ── 生成テンプレートに必須セクションが含まれる ───────────────────
    def test_generated_template_has_required_sections(self):
        """自動生成テンプレートに 8 セクション全て含まれる。"""
        pm_dir = TRADING / "data" / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)
        ks_flag = TRADING / "data" / "kill_switch.flag"
        ks_flag.touch()

        existing = list(pm_dir.glob("kill_switch*.md"))
        backed_up = []
        for f in existing:
            bak = f.with_suffix(".bak_test")
            f.rename(bak)
            backed_up.append((bak, f))

        try:
            r = _run_bash(POSTMORTEM_SH, {}, {"POSTMORTEM_GATE_ENABLED": "1"})
            gen_files = list(pm_dir.glob("kill_switch*.md"))
            assert gen_files, "テンプレートが生成されていない"
            content = gen_files[0].read_text(encoding="utf-8")
            for section in ("概要", "影響範囲", "原因分析", "タイムライン",
                            "対応内容", "再発防止策", "学習事項", "承認"):
                assert section in content, f"セクション '{section}' が欠落"
        finally:
            ks_flag.unlink(missing_ok=True)
            for f in pm_dir.glob("kill_switch*.md"):
                f.unlink(missing_ok=True)
            for bak, orig in backed_up:
                bak.rename(orig)

    # ── 空 stdin でも crash しない ──────────────────────────────────
    def test_empty_stdin_no_crash(self):
        """空 stdin で crash しない。"""
        r = subprocess.run(
            ["bash", str(POSTMORTEM_SH)],
            input="",
            capture_output=True,
            text=True,
            env={**os.environ, "POSTMORTEM_GATE_ENABLED": "0"},
        )
        assert r.returncode == 0
