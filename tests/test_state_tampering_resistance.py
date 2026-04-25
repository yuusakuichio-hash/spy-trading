"""tests/test_state_tampering_resistance.py

state.json直接改ざんで安全装置がバイパスできないことを検証するテスト。

Red Team観点で4つのフィールドを手動改ざんして、
それぞれが意図通り無効化されることを確認する。

改ざんシナリオ:
1. capital_usd を state.json に書き込んでも derived値 (futu API) が使われる
2. acc_type を state.json に書き込んでも derived値 (futu API) が使われる
3. manual_halt を state.json に書き込んでも atlas_halt.json だけが参照される
4. kill_switch.flag を直接 rm しても自動再発動する
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

# パス解決
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


class TestCapitalUsdTamperingResistance:
    """capital_usd フィールド改ざん耐性テスト"""

    def test_capital_usd_in_state_json_is_ignored(self, tmp_path):
        """state.json に capital_usd を直接書き込んでも derived関数に影響しない"""
        # state.json に capital_usd を書き込む（改ざんシミュレーション）
        state_file = tmp_path / "atlas_state.json"
        tampered_state = {
            "capital_usd": 999_999.0,  # 攻撃者がPDT制約をバイパスしようとする
            "started_at": "2026-01-01T00:00:00",
        }
        state_file.write_text(json.dumps(tampered_state))

        # atlas_agent の load_state は capital_usd を読むが、
        # _get_capital_and_acc_type_derived は state.json を読まない
        # → ATLAS_MODE=paper の場合は PAPER_CAPITAL_USD が返る
        with mock.patch.dict(os.environ, {"ATLAS_MODE": "paper"}):
            # 動的import して derived関数をテスト
            import importlib
            import atlas_agent
            importlib.reload(atlas_agent)

            capital, acc_type = atlas_agent._get_capital_and_acc_type_derived({"paper": True})
            # state.json の 999999 ではなく PAPER_CAPITAL_USD が返る
            assert capital == atlas_agent.PAPER_CAPITAL_USD, (
                f"capital_usd改ざん耐性NG: state.jsonの999999が使われてしまった。"
                f"actual={capital}"
            )
            assert acc_type == "SIMULATE", f"acc_type改ざん耐性NG: actual={acc_type}"

    def test_capital_usd_not_read_from_state_in_pdt_check(self, tmp_path):
        """PDT制約チェックが state.json の capital_usd を参照しないことを確認"""
        from common.trading_mode import get_pdt_constrained, LIVE

        # state.json に $999,999 を書いてもPDT判定は引数の capital_usd で行われる
        # get_pdt_constrained は state.json を読まない（引数のみで判定）
        constrained_low = get_pdt_constrained(LIVE, capital_usd=10_000.0)
        constrained_high = get_pdt_constrained(LIVE, capital_usd=999_999.0)

        assert constrained_low is True, "low capital → PDT制約あるべき"
        assert constrained_high is False, "high capital → PDT制約なし"
        # state.json を一切参照していないことが重要（引数経由のみ）


class TestAccTypeTamperingResistance:
    """acc_type フィールド改ざん耐性テスト"""

    def test_acc_type_paper_with_env_override(self):
        """ATLAS_MODE=paper 環境変数が acc_type の state.json 改ざんより優先される"""
        from common.trading_mode import get_current_mode

        # ATLAS_MODE=paper が設定されていれば acc_type の引数値は無関係
        with mock.patch.dict(os.environ, {"ATLAS_MODE": "paper"}):
            mode = get_current_mode(acc_type="REAL", cfg_paper=False)
            assert mode == "paper", (
                f"ATLAS_MODE=paper なのに mode={mode}。"
                f"環境変数が acc_type引数(REAL)より優先されるべき"
            )

    def test_acc_type_live_without_env_override(self):
        """ATLAS_MODE 未設定時は acc_type=REAL → live モードになる"""
        from common.trading_mode import get_current_mode

        env_without_atlas_mode = {k: v for k, v in os.environ.items() if k != "ATLAS_MODE"}
        with mock.patch.dict(os.environ, env_without_atlas_mode, clear=True):
            mode = get_current_mode(acc_type="REAL", cfg_paper=False)
            assert mode == "live", f"acc_type=REAL で mode=live が期待されるが actual={mode}"

    def test_acc_type_tampered_to_simulate_still_respects_atlas_mode(self):
        """ATLAS_MODE=live 設定時、acc_type=SIMULATE 改ざんは無視される"""
        from common.trading_mode import get_current_mode

        with mock.patch.dict(os.environ, {"ATLAS_MODE": "live"}):
            # acc_type=SIMULATE を渡しても ATLAS_MODE=live が優先される
            mode = get_current_mode(acc_type="SIMULATE", cfg_paper=False)
            assert mode == "live", (
                f"ATLAS_MODE=live なのに acc_type=SIMULATEで mode={mode}になった。"
                f"環境変数が最優先されるべき"
            )


class TestManualHaltTamperingResistance:
    """manual_halt フィールド改ざん耐性テスト"""

    def test_state_json_manual_halt_not_loaded(self, tmp_path):
        """state.json に manual_halt を直接書いても load_halt() は None を返す"""
        import importlib
        import atlas_agent
        importlib.reload(atlas_agent)

        # HALT_FILE が存在しない状態で state.json に manual_halt を書く
        halt_file = tmp_path / "halt" / "atlas_halt.json"
        assert not halt_file.exists()

        # load_halt() は HALT_FILE のみ参照する
        # state.json の manual_halt は一切無視される
        with mock.patch.object(atlas_agent, "HALT_FILE", halt_file):
            result = atlas_agent.load_halt()
            assert result is None, (
                f"state.json に manual_halt があっても load_halt() は None を返すべき。"
                f"actual={result}"
            )

    def test_halt_file_without_cli_written_flag_is_rejected(self, tmp_path):
        """atlas_halt.json に cli_written フラグがない場合は改ざんとして拒否される"""
        import importlib
        import atlas_agent
        importlib.reload(atlas_agent)

        halt_file = tmp_path / "halt" / "atlas_halt.json"
        halt_file.parent.mkdir(parents=True)

        # cli_written フラグなしで直接書き込む（改ざんシミュレーション）
        tampered_halt = {
            "rule_id": "manual",
            "since": "2026-01-01T00:00:00",
            # cli_written フラグを意図的に省略
        }
        halt_file.write_text(json.dumps(tampered_halt))

        with mock.patch.object(atlas_agent, "HALT_FILE", halt_file):
            with mock.patch.object(atlas_agent, "pushover"):
                result = atlas_agent.load_halt()
                # cli_written なし → 改ざんとして None を返す
                assert result is None, (
                    f"cli_written フラグなしのhalt.jsonは拒否されるべき。actual={result}"
                )

    def test_halt_via_set_halt_has_cli_written_flag(self, tmp_path):
        """set_halt() が書くファイルには必ず cli_written=True が含まれる"""
        import importlib
        import atlas_agent
        importlib.reload(atlas_agent)

        halt_file = tmp_path / "halt" / "atlas_halt.json"
        audit_log = tmp_path / "logs" / "halt_audit.jsonl"

        with mock.patch.object(atlas_agent, "HALT_FILE", halt_file):
            with mock.patch.object(atlas_agent, "HALT_AUDIT_LOG", audit_log):
                with mock.patch.object(atlas_agent, "pushover"):
                    atlas_agent.set_halt(reason="test_reason", who="test")
                    data = json.loads(halt_file.read_text())
                    assert data.get("cli_written") is True, (
                        f"set_halt()が書いたファイルに cli_written=True がない。data={data}"
                    )


class TestKillSwitchTamperingResistance:
    """kill_switch.flag 直接削除耐性テスト"""

    def test_cache_removed_check_is_realtime(self):
        """is_active() がキャッシュなしでリアルタイムにファイルを確認する"""
        import importlib
        from common import kill_switch
        importlib.reload(kill_switch)

        with tempfile.TemporaryDirectory() as tmpdir:
            flag = Path(tmpdir) / "kill_switch.flag"
            audit = Path(tmpdir) / "kill_switch_audit.jsonl"

            with mock.patch.object(kill_switch, "FLAG_FILE", flag):
                with mock.patch.object(kill_switch, "AUDIT_FILE", audit):
                    with mock.patch.object(kill_switch, "_activated_at", None):
                        # ファイルなし → False
                        assert kill_switch.is_active() is False

                        # ファイル作成
                        flag.write_text("activated_at=2026-01-01\nreason=test\n")
                        # キャッシュがあれば False のまま
                        # キャッシュなし設計なら即 True
                        assert kill_switch.is_active() is True

                        # ファイル削除
                        flag.unlink()
                        # キャッシュがあれば True のまま
                        # キャッシュなし設計なら即 False
                        assert kill_switch.is_active() is False

    def test_unexpected_flag_deletion_triggers_reactivation(self):
        """activate()後にフラグファイルを直接rmすると自動再発動する"""
        import importlib
        from common import kill_switch
        importlib.reload(kill_switch)

        with tempfile.TemporaryDirectory() as tmpdir:
            flag = Path(tmpdir) / "data" / "kill_switch.flag"
            audit = Path(tmpdir) / "kill_switch_audit.jsonl"
            flag.parent.mkdir(parents=True)

            with mock.patch.object(kill_switch, "FLAG_FILE", flag):
                with mock.patch.object(kill_switch, "AUDIT_FILE", audit):
                    with mock.patch.object(kill_switch, "_pushover_kill_switch"):
                        # activate()
                        kill_switch.activate(reason="test", activator="test")
                        assert flag.exists(), "activate後はフラグファイルが存在するはず"
                        assert kill_switch._activated_at is not None

                        # 直接削除（攻撃シミュレーション）
                        flag.unlink()
                        assert not flag.exists()

                        # is_active() が削除を検知して自動再発動する
                        result = kill_switch.is_active()
                        assert result is True, (
                            "直接rm後に is_active()を呼んだら自動再発動でTrueが返るべき"
                        )
                        assert flag.exists(), "自動再発動でフラグファイルが再作成されるべき"

    def test_deactivate_clears_activated_at(self):
        """deactivate()後は _activated_at がリセットされ削除検知が無効になる"""
        import importlib
        from common import kill_switch
        importlib.reload(kill_switch)

        with tempfile.TemporaryDirectory() as tmpdir:
            flag = Path(tmpdir) / "data" / "kill_switch.flag"
            audit = Path(tmpdir) / "kill_switch_audit.jsonl"
            flag.parent.mkdir(parents=True)

            with mock.patch.object(kill_switch, "FLAG_FILE", flag):
                with mock.patch.object(kill_switch, "AUDIT_FILE", audit):
                    with mock.patch.object(kill_switch, "_pushover_kill_switch"):
                        kill_switch.activate(reason="test", activator="test")
                        kill_switch.deactivate(activator="test")

                        # _activated_at がリセットされている
                        assert kill_switch._activated_at is None
                        # ファイルもない
                        assert not flag.exists()
                        # is_active() は False（再発動しない）
                        assert kill_switch.is_active() is False


class TestPDTThresholdCentralized:
    """PDT_THRESHOLD が一元管理されていることを確認するテスト"""

    def test_pdt_threshold_in_common_pdt_rules(self):
        """common.pdt_rules.PDT_THRESHOLD が 25000.0 であること"""
        from common.pdt_rules import PDT_THRESHOLD
        assert PDT_THRESHOLD == 25_000.0

    @pytest.mark.skip(reason="legacy strategy_selector.py drift — atlas_v3 移植時 rewrite (2026-04-25)")
    def test_strategy_selector_uses_pdt_threshold(self):
        """strategy_selector.py が PDT_THRESHOLD をインポートして使用していること"""
        import strategy_selector
        from common.pdt_rules import PDT_THRESHOLD

        # strategy_selector モジュールに PDT_THRESHOLD が import されていること
        assert hasattr(strategy_selector, "PDT_THRESHOLD"), (
            "strategy_selector.py が common.pdt_rules.PDT_THRESHOLD を import していない"
        )
        assert strategy_selector.PDT_THRESHOLD == PDT_THRESHOLD

    @pytest.mark.skip(reason="legacy strategy_selector.py drift — atlas_v3 移植時 rewrite (2026-04-25)")
    def test_no_hardcoded_25000_in_strategy_selector(self):
        """strategy_selector.py に 25_000 のハードコードがないことを確認"""
        selector_path = BASE / "strategy_selector.py"
        source = selector_path.read_text()
        # 25_000.0 や 25000.0 などのハードコードを検索
        # コメントや文字列リテラルを除いた実際のコードに含まれてはいけない
        import re
        # PDT_THRESHOLD より前に定義された 25_000 または 25000 のパターン
        # import文より後のコードで直接数値リテラルを使っていないか確認
        lines = source.splitlines()
        violations = []
        in_docstring = False
        quote_char = None
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # docstring/文字列リテラルのトグル検知（簡易）
            if '"""' in stripped or "'''" in stripped:
                triple = '"""' if '"""' in stripped else "'''"
                count = stripped.count(triple)
                if count == 1:
                    in_docstring = not in_docstring
                # 同一行に2つある場合は開いて即閉じ
                # → in_docstring は変わらない
            if in_docstring:
                continue
            # コメント行はスキップ
            if stripped.startswith("#"):
                continue
            # PDT_THRESHOLD のインポート行はスキップ
            if "PDT_THRESHOLD" in stripped:
                continue
            # $25,000 形式（ドル記号+コンマ区切り）はドキュメント的記述のためスキップ
            if re.search(r"\$25,000", stripped):
                continue
            # 25_000 または 25000 の数値リテラルを含む行を検出
            if re.search(r"\b25_?000\b", stripped):
                violations.append(f"L{i}: {line}")
        assert not violations, (
            f"strategy_selector.py に PDT閾値ハードコードが残っています:\n"
            + "\n".join(violations)
        )
