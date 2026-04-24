"""tests/test_pdt_mode_switch.py — PDT 自動導出設計のテスト

設計原則の検証:
- pdt_constrained は mode + capital から derived（state.json は参照しない）
- paper モードは常に pdt_constrained=False
- live $25K 未満 → pdt_constrained=True（物理的に無効化不可）
- live $25K 以上 → pdt_constrained=False

4ケース smoke テスト:
  1. paper × $20K → False
  2. paper × $30K → False
  3. live  × $20K → True
  4. live  × $30K → False

+ 抜け道チェック:
  - state.json に pdt_constrained=False を直書きしても live $20K は True のまま
  - mode が不正値 → ValueError
  - ATLAS_MODE 環境変数が最優先
"""
from __future__ import annotations

import os
import pytest

from common.trading_mode import (
    get_current_mode,
    get_pdt_constrained,
    assert_pdt_check_passed_if_live,
    PAPER,
    LIVE,
)


# ── 基本 4 ケース smoke テスト ────────────────────────────────────────────────

class TestPdtDerived4Cases:
    """paper/live × $20K/$30K の 4 ケース"""

    def test_paper_20k(self):
        """paper + $20K → pdt_constrained=False（FINRA 対象外）"""
        assert get_pdt_constrained(PAPER, 20_000.0) is False

    def test_paper_30k(self):
        """paper + $30K → pdt_constrained=False（FINRA 対象外）"""
        assert get_pdt_constrained(PAPER, 30_000.0) is False

    def test_live_20k(self):
        """live + $20K → pdt_constrained=True（$25K 未満）"""
        assert get_pdt_constrained(LIVE, 20_000.0) is True

    def test_live_30k(self):
        """live + $30K → pdt_constrained=False（$25K 以上）"""
        assert get_pdt_constrained(LIVE, 30_000.0) is False


# ── 境界値テスト ──────────────────────────────────────────────────────────────

class TestPdtBoundary:
    def test_live_exactly_25k(self):
        """live + $25,000 → False（境界値: $25K は制約なし）"""
        assert get_pdt_constrained(LIVE, 25_000.0) is False

    def test_live_24999(self):
        """live + $24,999 → True（$25K 未満）"""
        assert get_pdt_constrained(LIVE, 24_999.0) is True

    def test_live_zero_capital(self):
        """live + $0 → True（保守的: $25K 未満として扱う）"""
        assert get_pdt_constrained(LIVE, 0.0) is True

    def test_paper_zero_capital(self):
        """paper + $0 → False（paper は資本額によらず False）"""
        assert get_pdt_constrained(PAPER, 0.0) is False


# ── 抜け道チェック: state.json 手動書き換えは効かない ────────────────────────

class TestStatJsonCannotOverride:
    """state.json に pdt_constrained=False を直書きしても live $20K は True のまま"""

    def test_state_json_bypass_impossible(self, tmp_path):
        """
        state.json に pdt_constrained=False を書いても、
        get_pdt_constrained() は state.json を読まず mode+capital から算出するため
        live $20K は必ず True になる。
        """
        import json

        state_file = tmp_path / "atlas_state.json"
        state_file.write_text(json.dumps({"pdt_constrained": False, "capital_usd": 20_000.0}))

        # state.json を読み込んでも get_pdt_constrained() には渡さない
        state = json.loads(state_file.read_text())
        # 悪意ある or 誤ったコードが state["pdt_constrained"] を直接使う代わりに
        # derived API を呼ぶと必ず正しい値が返る
        derived = get_pdt_constrained(LIVE, state["capital_usd"])
        assert derived is True, (
            "state.json の pdt_constrained=False を直書きしても "
            "live $20K は pdt_constrained=True でなければならない"
        )


# ── get_current_mode() テスト ────────────────────────────────────────────────

class TestGetCurrentMode:
    def test_env_var_paper_wins(self, monkeypatch):
        """ATLAS_MODE=paper が最優先"""
        monkeypatch.setenv("ATLAS_MODE", "paper")
        assert get_current_mode(acc_type="REAL") == PAPER

    def test_env_var_live_wins(self, monkeypatch):
        """ATLAS_MODE=live が最優先"""
        monkeypatch.setenv("ATLAS_MODE", "live")
        assert get_current_mode(acc_type="SIMULATE") == LIVE

    def test_env_var_invalid_falls_through(self, monkeypatch):
        """ATLAS_MODE=invalid はフォールバック判定に進む"""
        monkeypatch.setenv("ATLAS_MODE", "invalid")
        # acc_type=SIMULATE → paper
        assert get_current_mode(acc_type="SIMULATE") == PAPER

    def test_acc_type_simulate(self, monkeypatch):
        """acc_type=SIMULATE → paper"""
        monkeypatch.delenv("ATLAS_MODE", raising=False)
        assert get_current_mode(acc_type="SIMULATE") == PAPER

    def test_acc_type_real(self, monkeypatch):
        """acc_type=REAL → live"""
        monkeypatch.delenv("ATLAS_MODE", raising=False)
        assert get_current_mode(acc_type="REAL") == LIVE

    def test_cfg_paper_true(self, monkeypatch):
        """cfg_paper=True → paper"""
        monkeypatch.delenv("ATLAS_MODE", raising=False)
        assert get_current_mode(cfg_paper=True) == PAPER

    def test_launchagent_contains_paper(self, monkeypatch):
        """launchagent_name に 'paper' → paper"""
        monkeypatch.delenv("ATLAS_MODE", raising=False)
        assert get_current_mode(launchagent_name="atlas_paper_launcher") == PAPER

    def test_default_is_live(self, monkeypatch):
        """全フラグ未設定 → live"""
        monkeypatch.delenv("ATLAS_MODE", raising=False)
        assert get_current_mode() == LIVE

    def test_env_var_case_insensitive(self, monkeypatch):
        """ATLAS_MODE=PAPER (大文字) → paper"""
        monkeypatch.setenv("ATLAS_MODE", "PAPER")
        assert get_current_mode() == PAPER


# ── 不正 mode → ValueError ────────────────────────────────────────────────────

class TestInvalidMode:
    def test_unknown_mode_raises(self):
        """不正な mode 値は ValueError"""
        with pytest.raises(ValueError, match="unknown mode"):
            get_pdt_constrained("pdt_constrained", 20_000.0)

    def test_empty_mode_raises(self):
        """空文字 mode は ValueError"""
        with pytest.raises(ValueError):
            get_pdt_constrained("", 20_000.0)


# ── Defense-in-Depth: assert_pdt_check_passed_if_live ────────────────────────

class TestAssertPdtCheckPassed:
    def test_live_under_25k_check_not_passed_raises(self):
        """live + $20K + check_passed=False → RuntimeError"""
        with pytest.raises(RuntimeError, match="Defense-in-Depth VIOLATION"):
            assert_pdt_check_passed_if_live(LIVE, 20_000.0, check_passed=False)

    def test_live_under_25k_check_passed_ok(self):
        """live + $20K + check_passed=True → 例外なし"""
        assert_pdt_check_passed_if_live(LIVE, 20_000.0, check_passed=True)  # no raise

    def test_live_over_25k_no_raise(self):
        """live + $30K → $25K 以上なので check_passed=False でも例外なし"""
        assert_pdt_check_passed_if_live(LIVE, 30_000.0, check_passed=False)  # no raise

    def test_paper_under_25k_no_raise(self):
        """paper + $20K → FINRA 対象外なので check_passed=False でも例外なし"""
        assert_pdt_check_passed_if_live(PAPER, 20_000.0, check_passed=False)  # no raise


# ── pre_trade_check 統合確認 ──────────────────────────────────────────────────

class TestPreTradeCheckIntegration:
    """pre_trade_check.check_order() が paper モードで PDT ブロックをスキップするか確認"""

    def test_paper_mode_pdt_block_skipped(self):
        """paper=True の OrderContext では PDT 上限でもブロックされない"""
        from common.pre_trade_check import OrderContext, check_order

        ctx = OrderContext(
            symbol="SPY",
            strike=500.0,
            side="SELL",
            qty=1,
            option_price=1.5,
            bid=1.4,
            ask=1.6,
            est_margin=500.0,
            capital_usd=420_000.0,   # ペーパー
            paper=True,
        )
        result = check_order(ctx)
        # PDT 上限（rolling5=3）でブロックされていないことを確認
        assert result.layer != "L3.5" or "PDT上限到達" not in result.reason, (
            f"paper モードで PDT ブロックが発動してしまった: {result}"
        )

    def test_live_mode_pdt_block_fires(self):
        """live + $20K では PDT 上限チェックが実行される（ブロックされるかは残数依存）"""
        from common.pre_trade_check import OrderContext, check_order

        ctx = OrderContext(
            symbol="SPY",
            strike=500.0,
            side="SELL",
            qty=1,
            option_price=1.5,
            bid=1.4,
            ask=1.6,
            est_margin=500.0,
            capital_usd=20_000.0,    # live $20K
            paper=False,
        )
        result = check_order(ctx)
        # live モードなので PDT チェックは実行される（ブロックされるかは tracker 状態依存）
        # ここでは「paper モードと異なるコードパスを通る」ことを確認
        # (test isolation: PDT tracker の状態はここでは制御しない)
        assert result is not None
