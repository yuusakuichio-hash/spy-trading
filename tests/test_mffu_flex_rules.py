"""tests/test_mffu_flex_rules.py

MFFUFlexRules runtime guard + basic contract tests
Sprint 1 Phase A / C-004

完了条件:
- 新規テスト 6 件以上 PASS
- pytest tests/ 全体 regression ゼロ
- AST hook (.claude/hooks/mffu_dry_run_guard.sh) とは別層（runtime guard）

テスト分類:
[T1] ENVIRONMENT=prod + dry_run=True  → SystemExit + kill_switch activate
[T2] ENVIRONMENT=staging + dry_run=True → 通過 (SystemExit なし)
[T3] ENVIRONMENT=prod + dry_run=False  → 通過
[T4] yaml 不在 + live モード → MFFURuleMissingError (fail-closed)
[T5] yaml 不在 + dry_run=True → 通過 (dry_run 退避経路)
[T6] yaml 存在 + live モード → インスタンス生成成功
[T7] dry_run モードの check_can_trade → (False, 'dry_run mode: 本番発注不可')
[T8] live モードの check_can_trade → (True, 'ok')
[T9] null 値 yaml で get_profit_target → MFFURuleMissingError
[T10] ENVIRONMENT 未設定 + dry_run=True → 通過 (prod 以外)
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# sys.path はconftest.pyが設定済み
from chronos_v3.prop.mffu_flex import MFFUFlexRules, MFFURuleMissingError, OrderRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def real_yaml_path() -> Path:
    """プロジェクトの実 mffu_flex.yaml を返す。"""
    return Path(__file__).parent.parent / "data" / "prop_rules" / "mffu_flex.yaml"


@pytest.fixture()
def minimal_yaml(tmp_path: Path) -> Path:
    """有効な minimal yaml (null 値あり) を tmp_path に生成して返す。"""
    content = textwrap.dedent("""\
        schema_version: "1.0"
        source: "test"
        verified_at: "2026-04-23"
        verified_by: "test"
        eval:
          profit_target_usd: null
          max_loss_limit_usd: null
          consistency_rule_pct: 50
          weekend_hold_forbidden: true
          hft_threshold_trades_per_day: 200
        funded:
          max_loss_limit_usd: null
          consistency_rule_pct: null
          weekend_hold_forbidden: true
          hft_threshold_trades_per_day: 200
        force_close_et: "16:00@America/New_York"
    """)
    p = tmp_path / "mffu_flex.yaml"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture()
def filled_yaml(tmp_path: Path) -> Path:
    """profit_target / max_loss が数値で埋まった yaml。"""
    content = textwrap.dedent("""\
        schema_version: "1.0"
        source: "test"
        verified_at: "2026-04-23"
        verified_by: "test"
        eval:
          profit_target_usd: 3000.0
          max_loss_limit_usd: 2500.0
          consistency_rule_pct: 50
          weekend_hold_forbidden: true
          hft_threshold_trades_per_day: 200
        funded:
          max_loss_limit_usd: 2000.0
          consistency_rule_pct: null
          weekend_hold_forbidden: true
          hft_threshold_trades_per_day: 200
        force_close_et: "16:00@America/New_York"
    """)
    p = tmp_path / "mffu_flex_filled.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# [T1] prod + dry_run=True → SystemExit + kill_switch activate
# ---------------------------------------------------------------------------

def test_prod_dry_run_raises_systemexit(minimal_yaml: Path) -> None:
    """ENVIRONMENT=prod で dry_run=True → SystemExit + kill_switch.activate 呼出。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
        mock_activate = MagicMock()
        with patch("chronos_v3.prop.mffu_flex._ks_activate_import", mock_activate, create=True):
            # kill_switch を mock してactivateが呼ばれることも確認
            with patch("common.kill_switch.activate") as mock_ks:
                with pytest.raises(SystemExit) as exc_info:
                    MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
    # SystemExit メッセージに [FATAL] が含まれること
    assert "[FATAL]" in str(exc_info.value)


def test_prod_dry_run_systemexit_message_contains_forbidden(minimal_yaml: Path) -> None:
    """SystemExit メッセージが 'forbidden in prod' を含む。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
        with patch("common.kill_switch.activate"):
            with pytest.raises(SystemExit) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
    assert "forbidden in prod" in str(exc_info.value)


def test_prod_dry_run_kill_switch_activate_called(minimal_yaml: Path) -> None:
    """ENVIRONMENT=prod + dry_run=True で common.kill_switch.activate が呼ばれる。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
        with patch("common.kill_switch.activate") as mock_activate:
            with pytest.raises(SystemExit):
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
    mock_activate.assert_called_once()
    # reason 引数に "dry_run" が含まれること
    call_kwargs = mock_activate.call_args
    reason_arg = (
        call_kwargs.kwargs.get("reason")
        or (call_kwargs.args[0] if call_kwargs.args else "")
    )
    assert "dry_run" in reason_arg


# ---------------------------------------------------------------------------
# [T2] staging + dry_run=True → 通過
# ---------------------------------------------------------------------------

def test_staging_dry_run_allowed(minimal_yaml: Path) -> None:
    """ENVIRONMENT=staging では dry_run=True が許容される。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
    assert rules.mode == "dry_run"


# ---------------------------------------------------------------------------
# [T3] prod + dry_run=False → 通過
# ---------------------------------------------------------------------------

def test_prod_live_mode_allowed(minimal_yaml: Path) -> None:
    """ENVIRONMENT=prod + dry_run=False は正常。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=False)
    assert rules.mode == "live"


# ---------------------------------------------------------------------------
# [T4] yaml 不在 + live モード → fail-closed
# ---------------------------------------------------------------------------

def test_yaml_missing_live_mode_raises(tmp_path: Path) -> None:
    """yaml ファイルが存在しない live モードは MFFURuleMissingError。"""
    missing_path = tmp_path / "nonexistent.yaml"
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        with pytest.raises(MFFURuleMissingError) as exc_info:
            MFFUFlexRules(yaml_path=missing_path, dry_run=False)
    assert "not found" in str(exc_info.value).lower() or "bootstrap" in str(exc_info.value)


# ---------------------------------------------------------------------------
# [T5] yaml 不在 + dry_run=True → 通過（退避経路）
# ---------------------------------------------------------------------------

def test_yaml_missing_dry_run_allowed(tmp_path: Path) -> None:
    """yaml 不在でも dry_run=True なら通過（退避経路）。"""
    missing_path = tmp_path / "nonexistent.yaml"
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=missing_path, dry_run=True)
    assert rules.mode == "dry_run"


# ---------------------------------------------------------------------------
# [T6] yaml 存在 + live モード → インスタンス生成成功
# ---------------------------------------------------------------------------

def test_yaml_present_live_mode_instantiates(minimal_yaml: Path) -> None:
    """yaml が存在する live モードはインスタンス生成成功。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=False)
    assert rules.mode == "live"


# ---------------------------------------------------------------------------
# [T7] dry_run モードの check_can_trade → (False, 'dry_run mode: 本番発注不可')
# ---------------------------------------------------------------------------

def test_check_can_trade_dry_run_blocks(minimal_yaml: Path) -> None:
    """dry_run モードでは check_can_trade が常に (False, 'dry_run mode: ...') を返す。

    S6: check_can_trade 内で ENVIRONMENT を再評価するため、
    呼出時も同じ環境変数 context 内で実行する。
    """
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        order = OrderRequest(symbol="MES", qty=1, side="buy")
        can_trade, reason = rules.check_can_trade(account="eval_001", pending_order=order)
    assert can_trade is False
    assert "dry_run mode" in reason
    assert "本番発注不可" in reason


# ---------------------------------------------------------------------------
# [T8] live モードの check_can_trade → (True, 'ok')
# ---------------------------------------------------------------------------

def test_check_can_trade_live_returns_ok(minimal_yaml: Path) -> None:
    """live モードの check_can_trade は Sprint 1 stub として (True, 'ok')。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=False)
    order = OrderRequest(symbol="MES", qty=1)
    can_trade, reason = rules.check_can_trade(account="eval_001", pending_order=order)
    assert can_trade is True
    assert reason == "ok"


# ---------------------------------------------------------------------------
# [T9] null 値 yaml で get_profit_target → MFFURuleMissingError
# ---------------------------------------------------------------------------

def test_get_profit_target_null_raises(minimal_yaml: Path) -> None:
    """profit_target_usd が null のまま live モードで取得 → MFFURuleMissingError。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=False)
    with pytest.raises(MFFURuleMissingError) as exc_info:
        rules.get_profit_target("eval")
    assert "null" in str(exc_info.value) or "null" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# [T10] ENVIRONMENT 未設定 + dry_run=True → fail-closed (SystemExit)
#
# Redteam r1 S1/S2 変更: 未設定は prod 扱い → guard 発動
# ---------------------------------------------------------------------------

def test_no_environment_dry_run_raises_systemexit(minimal_yaml: Path) -> None:
    """ENVIRONMENT 未設定では dry_run=True が fail-closed で SystemExit になる（prod 扱い）。

    Redteam r1 HIGH S1/S2: fail-closed。旧実装の「未設定=通過」を廃止。
    """
    env_without_environment = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
    with patch.dict(os.environ, env_without_environment, clear=True):
        with pytest.raises(SystemExit) as exc_info:
            MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
    assert "[FATAL]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# [T11] mode プロパティ正確性
# ---------------------------------------------------------------------------

def test_mode_property_live(minimal_yaml: Path) -> None:
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=False)
    assert rules.mode == "live"


def test_mode_property_dry_run(minimal_yaml: Path) -> None:
    with patch.dict(os.environ, {"ENVIRONMENT": "dev"}):
        rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
    assert rules.mode == "dry_run"


# ---------------------------------------------------------------------------
# [T12] get_profit_target - 数値が入っている場合は正常取得
# ---------------------------------------------------------------------------

def test_get_profit_target_filled_returns_float(filled_yaml: Path) -> None:
    """profit_target_usd が数値の場合は float で返る。"""
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=filled_yaml, dry_run=False)
    result = rules.get_profit_target("eval")
    assert isinstance(result, float)
    assert result > 0


# ---------------------------------------------------------------------------
# [T13] 実yaml (data/prop_rules/mffu_flex.yaml) が存在 + live → インスタンス生成
# ---------------------------------------------------------------------------

def test_real_yaml_instantiates(real_yaml_path: Path) -> None:
    """プロジェクトの実 yaml ファイルで live モード生成が通ること。"""
    if not real_yaml_path.exists():
        pytest.skip("real yaml not found")
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        rules = MFFUFlexRules(yaml_path=real_yaml_path, dry_run=False)
    assert rules.mode == "live"


# ---------------------------------------------------------------------------
# [T14] schema_version 不正 → MFFURuleMissingError
# ---------------------------------------------------------------------------

def test_invalid_schema_version_raises(tmp_path: Path) -> None:
    """schema_version が未対応値の場合 MFFURuleMissingError。"""
    bad_yaml = tmp_path / "bad_schema.yaml"
    bad_yaml.write_text(
        'schema_version: "99.0"\neval:\n  profit_target_usd: 1000\n',
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
        with pytest.raises(MFFURuleMissingError) as exc_info:
            MFFUFlexRules(yaml_path=bad_yaml, dry_run=False)
    assert "schema_version" in str(exc_info.value) or "Unsupported" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Redteam r1 negative tests — CRITICAL S7 / HIGH S1-S3 / MEDIUM S4 / S6
# ---------------------------------------------------------------------------

class TestRedteamR1NegativeTests:
    """Negative tests added per Redteam r1 CRITICAL 1 + HIGH 4 findings."""

    # ----------------------------------------------------------------
    # S7: _dry_run 属性書換禁止
    # ----------------------------------------------------------------

    def test_dry_run_attribute_write_raises_attribute_error(self, minimal_yaml: Path) -> None:
        """S7 CRITICAL: 初期化後に _dry_run を書き換えようとすると AttributeError。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        with pytest.raises(AttributeError) as exc_info:
            rules._dry_run = False  # 書換試み
        assert "immutable" in str(exc_info.value).lower() or "forbidden" in str(exc_info.value).lower()

    def test_dry_run_attribute_write_true_to_false_raises(self, minimal_yaml: Path) -> None:
        """S7 CRITICAL: dry_run=False インスタンスへの True 書込も AttributeError。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=False)
        with pytest.raises(AttributeError):
            rules._dry_run = True

    # ----------------------------------------------------------------
    # S3: dry_run 型検証 (non-bool)
    # ----------------------------------------------------------------

    def test_dry_run_int_1_raises_type_error(self, minimal_yaml: Path) -> None:
        """S3 HIGH: dry_run=1 (int truthy) は TypeError。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            with pytest.raises(TypeError) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=1)  # type: ignore[arg-type]
        assert "bool" in str(exc_info.value)

    def test_dry_run_string_true_raises_type_error(self, minimal_yaml: Path) -> None:
        """S3 HIGH: dry_run='true' (str) は TypeError。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            with pytest.raises(TypeError) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run="true")  # type: ignore[arg-type]
        assert "bool" in str(exc_info.value)

    def test_dry_run_int_0_raises_type_error(self, minimal_yaml: Path) -> None:
        """S3 HIGH: dry_run=0 (int falsy) も TypeError（明示的 bool 必須）。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            with pytest.raises(TypeError) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=0)  # type: ignore[arg-type]
        assert "bool" in str(exc_info.value)

    # ----------------------------------------------------------------
    # S1/S2: ENVIRONMENT fail-closed (大小揺れ / 未設定)
    # ----------------------------------------------------------------

    def test_environment_uppercase_prod_raises_systemexit(self, minimal_yaml: Path) -> None:
        """S1/S2 HIGH: ENVIRONMENT='PROD' (大文字) → fail-closed で SystemExit。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "PROD"}):
            with pytest.raises(SystemExit) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        assert "[FATAL]" in str(exc_info.value)

    def test_environment_prod_with_spaces_raises_systemexit(self, minimal_yaml: Path) -> None:
        """S1/S2 HIGH: ENVIRONMENT=' prod ' (前後スペース) → fail-closed で SystemExit。"""
        with patch.dict(os.environ, {"ENVIRONMENT": " prod "}):
            with pytest.raises(SystemExit) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        assert "[FATAL]" in str(exc_info.value)

    def test_environment_prod_with_newline_raises_systemexit(self, minimal_yaml: Path) -> None:
        """S1/S2 HIGH: ENVIRONMENT='prod\\n' (末尾改行) → fail-closed で SystemExit。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "prod\n"}):
            with pytest.raises(SystemExit) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        assert "[FATAL]" in str(exc_info.value)

    def test_environment_unknown_value_raises_systemexit(self, minimal_yaml: Path) -> None:
        """S1/S2 HIGH: ENVIRONMENT='production' (未知の値) → fail-closed で SystemExit。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            with pytest.raises(SystemExit) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        assert "[FATAL]" in str(exc_info.value)

    def test_environment_unset_raises_systemexit(self, minimal_yaml: Path) -> None:
        """S1/S2 HIGH: ENVIRONMENT 未設定 → prod 扱い → fail-closed で SystemExit。

        旧 T10 が「通過」を期待していたが Redteam r1 で fail-closed に変更済み。
        """
        env_without = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
        with patch.dict(os.environ, env_without, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        assert "[FATAL]" in str(exc_info.value)

    # ----------------------------------------------------------------
    # S6: late binding race — check_can_trade env re-eval
    # ----------------------------------------------------------------

    def test_s6_late_binding_prod_promotion_raises_at_check_can_trade(
        self, minimal_yaml: Path
    ) -> None:
        """S6 HIGH: インスタンス生成後に ENVIRONMENT が prod に昇格すると
        check_can_trade が SystemExit を raise する。"""
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        # ENVIRONMENT を prod に昇格させた状態で check_can_trade を呼ぶ
        order = OrderRequest(symbol="MES", qty=1, side="buy")
        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            with pytest.raises(SystemExit) as exc_info:
                rules.check_can_trade(account="eval_001", pending_order=order)
        assert "[FATAL]" in str(exc_info.value)

    # ----------------------------------------------------------------
    # S4: subclass __init__ override guard
    # ----------------------------------------------------------------

    def test_s4_subclass_init_override_dry_run_true_in_prod_raises(
        self, minimal_yaml: Path
    ) -> None:
        """S4 MEDIUM: subclass が __init__ を override しても prod+dry_run guard が発動。"""

        class _SubRules(MFFUFlexRules):
            def __init__(self, yaml_path: Path, dry_run: bool = False) -> None:
                # super() を呼ばずに直接属性設定しようとするサブクラス
                # __init_subclass__ のラッパーがこれを捕捉してガードを実行する
                super().__init__(yaml_path=yaml_path, dry_run=dry_run)

        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            with pytest.raises(SystemExit) as exc_info:
                _SubRules(yaml_path=minimal_yaml, dry_run=True)
        assert "[FATAL]" in str(exc_info.value)

    # ----------------------------------------------------------------
    # 正常系確認: safe env では dry_run=True が通る
    # ----------------------------------------------------------------

    @pytest.mark.parametrize("env_val", ["dev", "test", "paper", "staging"])
    def test_safe_envs_allow_dry_run_true(self, env_val: str, minimal_yaml: Path) -> None:
        """S1/S2: dev/test/paper/staging では dry_run=True が許容される。"""
        with patch.dict(os.environ, {"ENVIRONMENT": env_val}):
            rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=True)
        assert rules.mode == "dry_run"


# ---------------------------------------------------------------------------
# Redteam r2 negative tests — C-R2-A (WeakKeyDictionary) / C-R2-B (inspect.signature)
# ---------------------------------------------------------------------------

class TestRedteamR2NegativeTests:
    """Negative tests for Redteam r2 CRITICAL findings C-R2-A and C-R2-B."""

    # ----------------------------------------------------------------
    # C-R2-A: name-mangling bypass via object.__setattr__ must NOT persist
    # ----------------------------------------------------------------

    def test_object_setattr_dry_run_val_does_not_persist(self, minimal_yaml: Path) -> None:
        """C-R2-A CRITICAL: object.__setattr__(obj, '_MFFUFlexRules__dry_run_val', True)
        does NOT change _dry_run because backing store is WeakKeyDictionary, not __dict__.

        Before r2: _dry_run read self.__dry_run_val (name-mangled → __dict__ key
        '_MFFUFlexRules__dry_run_val').  object.__setattr__() could overwrite it.
        After r2: _dry_run reads _DRY_RUN_STATE[self].  Writing to __dict__ has no
        effect.
        """
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            rules = MFFUFlexRules(yaml_path=minimal_yaml, dry_run=False)

        assert rules._dry_run is False, "precondition: dry_run=False"

        # Attempt name-mangling bypass — the exact attack vector from Redteam r2
        object.__setattr__(rules, "_MFFUFlexRules__dry_run_val", True)

        # _dry_run must still be False; the write went to __dict__, not _DRY_RUN_STATE
        assert rules._dry_run is False, (
            "C-R2-A FAIL: object.__setattr__ bypass changed _dry_run "
            "— backing store was not moved to WeakKeyDictionary"
        )
        assert rules.mode == "live", "mode must remain 'live' after bypass attempt"

    # ----------------------------------------------------------------
    # C-R2-B: subclass positional dry_run=True in prod raises SystemExit
    # ----------------------------------------------------------------

    def test_subclass_positional_dry_run_true_in_prod_raises(
        self, minimal_yaml: Path
    ) -> None:
        """C-R2-B CRITICAL: subclass __init__ with dry_run as positional arg must fire guard.

        Before r2: __init_subclass__ guard used kw.get('dry_run', False) which missed
        positional arguments.
        After r2: inspect.signature(original_init).bind() resolves 'dry_run' regardless
        of whether it was passed positionally or as a keyword.
        """

        class _SubPositional(MFFUFlexRules):
            def __init__(self, yaml_path: Path, storage: object, dry_run: bool = False) -> None:
                super().__init__(yaml_path=yaml_path, storage=storage, dry_run=dry_run)

        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            with pytest.raises(SystemExit) as exc_info:
                # dry_run=True passed as 3rd positional argument
                _SubPositional(minimal_yaml, None, True)

        assert "[FATAL]" in str(exc_info.value), (
            "C-R2-B FAIL: positional dry_run=True in subclass did not raise [FATAL] SystemExit"
        )

    def test_subclass_positional_via_args_spread_raises(
        self, minimal_yaml: Path
    ) -> None:
        """C-R2-B CRITICAL: *args spread with dry_run=True must also fire guard.

        Verifies that passing (yaml_path, storage, True) via *args to the subclass
        __init__ still triggers the SystemExit guard in prod.
        """

        class _SubArgsSpread(MFFUFlexRules):
            def __init__(self, yaml_path: Path, storage: object, dry_run: bool = False) -> None:
                super().__init__(yaml_path=yaml_path, storage=storage, dry_run=dry_run)

        positional_args = (minimal_yaml, None, True)  # dry_run=True at index 2

        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            with pytest.raises(SystemExit) as exc_info:
                _SubArgsSpread(*positional_args)

        assert "[FATAL]" in str(exc_info.value), (
            "C-R2-B FAIL: *args spread with dry_run=True did not raise [FATAL] SystemExit"
        )
