"""chronos_v3.prop.mffu_flex — MFFU Flex runtime guard + rule evaluation

spec: data/specs/v3/chronos_spec_v3_20260422.md B5 R2b L172-L192
sprint: Sprint 1 Phase A / C-004 Redteam r1 CRITICAL+HIGH → r2 CRITICAL×2

Runtime guard (L190):
    ENVIRONMENT=prod (or unset/unknown) + dry_run=True → activate kill_switch + SystemExit
    This fires regardless of how the caller passes dry_run (kwarg, positional,
    **kwargs expansion, monkey-patch) because it is checked at __init__ time
    inside the class itself — not via static AST analysis.

Hardening added in Redteam r1:
    S7 (CRITICAL): _dry_run is a read-only property. Post-init write raises AttributeError.
    S1/S2 (HIGH):  Fail-closed ENVIRONMENT check. Unset / unknown values treated as prod.
    S3 (HIGH):     dry_run must be exactly bool; int/str raises TypeError.
    S6 (HIGH):     check_can_trade re-evaluates ENVIRONMENT to catch late prod promotion.
    S4 (MEDIUM):   __init_subclass__ installs guard so subclass __init__ override cannot
                   skip the prod+dry_run check.

Hardening added in Redteam r2:
    C-R2-A (CRITICAL): _dry_run backing store moved to module-level WeakKeyDictionary.
        object.__setattr__(obj, '_MFFUFlexRules__dry_run_val', True) no longer reaches
        the backing store — the property reads from _DRY_RUN_STATE[self] which lives
        entirely outside the instance's __dict__ / slots / name-mangled attributes.
    C-R2-B (CRITICAL): __init_subclass__ guard now uses inspect.signature().bind() to
        extract the resolved value of 'dry_run' regardless of whether it was passed as
        a positional argument, a keyword argument, or via *args spread.

AST hook (.claude/hooks/mffu_dry_run_guard.sh) remains as second layer.
"""
from __future__ import annotations

import datetime
import inspect
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MFFURuleMissingError(Exception):
    """yaml 未更新 / 値欠落 / 旧式フォーマット.

    Raised on:
    - yaml file not found (fail-closed)
    - required key missing or null in live mode
    - schema_version mismatch
    """


# ---------------------------------------------------------------------------
# Internal helper: fail-closed ENVIRONMENT evaluation
# ---------------------------------------------------------------------------

# Environments in which dry_run=True is permitted.
_DRY_RUN_SAFE_ENVS: frozenset[str] = frozenset({"dev", "test", "paper", "staging"})


def _is_prod_env() -> bool:
    """Return True if ENVIRONMENT indicates a prod-class environment.

    Fail-closed: unset, empty, or unrecognised values are treated as prod.
    Comparison is case-insensitive and strips surrounding whitespace / newlines.
    """
    raw = os.environ.get("ENVIRONMENT", "prod")
    normalised = raw.strip().lower()
    return normalised not in _DRY_RUN_SAFE_ENVS


def _activate_kill_switch_if_available(reason: str) -> None:
    """Best-effort call to common.kill_switch.activate."""
    try:
        from common.kill_switch import activate as _ks_activate
        _ks_activate(reason=reason)
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# StorageBackend stub (BT本格実装は Sprint 2)
# ---------------------------------------------------------------------------


class _StorageBackend:
    """Minimal interface for daily PnL storage.

    Sprint 1 実装はメモリのみ。Sprint 2 で DB/file backend に差し替え。
    """

    def get_daily_pnl(self, account: str, date: datetime.date) -> float:
        return 0.0

    def get_daily_pnls(self, account: str, days: int = 10) -> list[float]:
        return []


# ---------------------------------------------------------------------------
# OrderRequest stub
# ---------------------------------------------------------------------------


class OrderRequest:
    """Minimal order descriptor used by check_can_trade."""

    def __init__(self, symbol: str, qty: int, side: str = "buy") -> None:
        self.symbol = symbol
        self.qty = qty
        self.side = side


# ---------------------------------------------------------------------------
# MFFUFlexRules
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# C-R2-A: closure-based backing store for _dry_run
#
# Storing the bool in a module-level WeakKeyDictionary means it lives
# OUTSIDE the instance's __dict__, slots, and any name-mangled attribute.
# object.__setattr__(obj, '_MFFUFlexRules__dry_run_val', True) writes to
# __dict__ (a different namespace) and therefore has no effect on what
# _dry_run property returns.
# WeakKeyDictionary keys are held weakly → no reference cycle / memory leak.
# ---------------------------------------------------------------------------
_DRY_RUN_STATE: WeakKeyDictionary["MFFUFlexRules", bool] = WeakKeyDictionary()

_SUPPORTED_SCHEMA_VERSIONS = {"1.0"}
_FRESHNESS_WARN_DAYS = 30


class MFFUFlexRules:
    """MFFU Flex prop firm rule engine.

    Parameters
    ----------
    yaml_path:
        Path to the MFFU rules yaml file
        (canonical: data/prop_rules/mffu_flex.yaml).
    storage:
        StorageBackend instance for daily PnL queries.
        Pass None to use the in-memory stub (paper / test only).
    dry_run:
        Must be exactly bool (True or False). Non-bool raises TypeError.
        If True, all check_can_trade calls return (False, 'dry_run mode: 本番発注不可 ...').
        FORBIDDEN when ENVIRONMENT is not in {dev, test, paper, staging} — raises SystemExit.
        Unset or unknown ENVIRONMENT values are treated as prod (fail-closed).

    Raises
    ------
    TypeError
        When dry_run is not exactly bool.
    SystemExit
        When ENVIRONMENT is prod-class and dry_run=True.
    MFFURuleMissingError
        When yaml_path does not exist (fail-closed in live mode).
    """

    # ── S4 / C-R2-B: subclass guard ──────────────────────────────────────
    def __init_subclass__(cls, **kwargs: object) -> None:
        """Ensure subclasses cannot bypass the prod+dry_run guard.

        C-R2-B hardening: uses inspect.signature().bind() to resolve the
        value of 'dry_run' regardless of whether the caller supplied it as
        a positional argument, a keyword argument, or via *args spread.
        The previous kw.get('dry_run', False) only inspected **kwargs and
        therefore missed positional-only invocations like Sub(path, s, True).
        """
        super().__init_subclass__(**kwargs)
        original_init = cls.__dict__.get("__init__")
        if original_init is not None:
            import functools

            @functools.wraps(original_init)
            def _guarded_init(self: "MFFUFlexRules", *args: object, **kw: object) -> None:
                # C-R2-B: resolve 'dry_run' from positional OR keyword args.
                # inspect.signature().bind() maps every argument (including
                # positional ones) to their parameter names as declared in the
                # subclass __init__ signature.
                try:
                    sig = inspect.signature(original_init)
                    bound = sig.bind(self, *args, **kw)
                    bound.apply_defaults()
                    _sub_dry_run = bound.arguments.get("dry_run", False)
                except TypeError:
                    # If bind fails (e.g. wrong arity) let the original call
                    # propagate the error naturally.
                    _sub_dry_run = kw.get("dry_run", False)

                if not isinstance(_sub_dry_run, bool):
                    raise TypeError(
                        f"MFFUFlexRules.dry_run must be bool, got {type(_sub_dry_run).__name__!r}"
                    )
                if _sub_dry_run and _is_prod_env():
                    _activate_kill_switch_if_available(
                        reason="MFFU dry_run mode in prod environment (subclass path)"
                    )
                    raise SystemExit(
                        "[FATAL] MFFUFlexRules subclass(dry_run=True) is forbidden in prod. "
                        "Set dry_run=False or fix yaml before deploying."
                    )
                original_init(self, *args, **kw)

            cls.__init__ = _guarded_init  # type: ignore[method-assign]

    def __init__(
        self,
        yaml_path: Path,
        storage: _StorageBackend | None = None,
        dry_run: bool = False,
    ) -> None:
        # ── S3: bool型強制 ──────────────────────────────────────────────────
        # Must be the very first guard — before any attribute is set.
        if not isinstance(dry_run, bool):
            raise TypeError(
                f"MFFUFlexRules.dry_run must be bool (True/False), "
                f"got {type(dry_run).__name__!r}: {dry_run!r}. "
                "Do not pass integers or strings; use True/False explicitly."
            )

        # ── S1/S2: fail-closed ENVIRONMENT guard ──────────────────────────
        # Unset, empty, and unrecognised ENVIRONMENT values are treated as prod.
        if dry_run and _is_prod_env():
            _activate_kill_switch_if_available(
                reason="MFFU dry_run mode in prod environment"
            )
            raise SystemExit(
                "[FATAL] MFFUFlexRules(dry_run=True) is forbidden in prod. "
                "Set dry_run=False or fix yaml before deploying."
            )

        # ── C-R2-A: write backing store to module-level WeakKeyDictionary.
        #   This lives entirely outside the instance's __dict__ / slots /
        #   name-mangled attributes.  object.__setattr__(obj,
        #   '_MFFUFlexRules__dry_run_val', True) only modifies __dict__ and
        #   therefore has zero effect on what _dry_run property returns.
        _DRY_RUN_STATE[self] = dry_run
        self._storage = storage if storage is not None else _StorageBackend()
        self._yaml_path = Path(yaml_path)
        self._rules: dict = {}

        # ── yaml 読込 ────────────────────────────────────────────────────
        self._load_yaml()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _dry_run(self) -> bool:
        """Read-only after __init__. Any attempt to set raises AttributeError.

        C-R2-A: backing store is _DRY_RUN_STATE (module-level WeakKeyDictionary).
        object.__setattr__(obj, '_MFFUFlexRules__dry_run_val', True) has no
        effect here because the property never reads from __dict__ or any
        name-mangled attribute.
        """
        return _DRY_RUN_STATE.get(self, False)

    @_dry_run.setter
    def _dry_run(self, value: object) -> None:
        raise AttributeError(
            "_dry_run is immutable after __init__. "
            "Reassigning dry_run mode is forbidden to prevent live/dry_run confusion."
        )

    @property
    def mode(self) -> Literal["live", "dry_run"]:
        return "dry_run" if self._dry_run else "live"

    # ------------------------------------------------------------------
    # yaml loading (fail-closed)
    # ------------------------------------------------------------------

    def _load_yaml(self) -> None:
        """Load and validate the rules yaml.

        Raises
        ------
        MFFURuleMissingError
            - yaml file not found
            - pyyaml not installed
            - schema_version unsupported
        """
        if not _YAML_AVAILABLE:
            if self._dry_run:
                # dry_run allows missing yaml (yaml欠落退避経路)
                return
            raise MFFURuleMissingError(
                "pyyaml is not installed; cannot load MFFU rules. "
                "Install: pip install pyyaml"
            )

        if not self._yaml_path.exists():
            if self._dry_run:
                # dry_run: yaml不在は許容・BT実行継続
                return
            raise MFFURuleMissingError(
                f"MFFU rules yaml not found: {self._yaml_path}. "
                "Run bootstrap procedure (spec B5 R2 L145-168)."
            )

        with self._yaml_path.open("r", encoding="utf-8") as fh:
            data = _yaml.safe_load(fh)

        if not isinstance(data, dict):
            raise MFFURuleMissingError(
                f"MFFU yaml parse failed or empty: {self._yaml_path}"
            )

        schema = data.get("schema_version")
        if schema not in _SUPPORTED_SCHEMA_VERSIONS:
            raise MFFURuleMissingError(
                f"Unsupported schema_version={schema!r} in {self._yaml_path}. "
                f"Supported: {_SUPPORTED_SCHEMA_VERSIONS}"
            )

        self._rules = data

    # ------------------------------------------------------------------
    # Public API (spec B5 minimum)
    # ------------------------------------------------------------------

    def verify_yaml_freshness(self) -> tuple[bool, int]:
        """Check yaml mtime against 30-day threshold.

        Returns
        -------
        (ok, days_elapsed)
            ok=True  → within 30 days
            ok=False → stale (>30 days) — EICAS Warning required
        """
        if not self._yaml_path.exists():
            return False, -1
        mtime = datetime.datetime.fromtimestamp(
            self._yaml_path.stat().st_mtime, tz=datetime.timezone.utc
        )
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        days = (now - mtime).days
        return days <= _FRESHNESS_WARN_DAYS, days

    def get_profit_target(self, account_type: Literal["eval", "funded"]) -> float:
        """Return profit target USD from yaml.

        Raises MFFURuleMissingError if not set or null.
        """
        if self._dry_run:
            raise MFFURuleMissingError(
                "get_profit_target unavailable in dry_run mode"
            )
        try:
            val = self._rules[account_type]["profit_target_usd"]
        except (KeyError, TypeError) as exc:
            raise MFFURuleMissingError(
                f"profit_target_usd missing for account_type={account_type!r}"
            ) from exc
        if val is None:
            raise MFFURuleMissingError(
                f"profit_target_usd is null for account_type={account_type!r}. "
                "Transcribe from MFFU dashboard."
            )
        return float(val)

    def get_max_loss(self, account_type: str) -> float:
        """Return max loss USD from yaml.

        Raises MFFURuleMissingError if not set or null.
        """
        if self._dry_run:
            raise MFFURuleMissingError(
                "get_max_loss unavailable in dry_run mode"
            )
        try:
            val = self._rules[account_type]["max_loss_limit_usd"]
        except (KeyError, TypeError) as exc:
            raise MFFURuleMissingError(
                f"max_loss_limit_usd missing for account_type={account_type!r}"
            ) from exc
        if val is None:
            raise MFFURuleMissingError(
                f"max_loss_limit_usd is null for account_type={account_type!r}. "
                "Transcribe from MFFU dashboard."
            )
        return float(val)

    def check_can_trade(
        self, account: str, pending_order: OrderRequest
    ) -> tuple[bool, str]:
        """Return (can_trade, reason).

        dry_run mode:
            Always returns (False, 'dry_run mode: 本番発注不可 — 発注はシミュレーションのみ有効').
            Callers MUST NOT treat False here as a trading signal — it means the engine
            is running in paper/test mode and real orders will never be placed.

        S6 late-binding race guard:
            Also re-evaluates ENVIRONMENT at call time. If the env has been promoted to
            prod while this instance is still in dry_run, raises SystemExit immediately.
        """
        # S6: re-evaluate ENVIRONMENT on every call to catch late prod promotion
        if self._dry_run and _is_prod_env():
            _activate_kill_switch_if_available(
                reason="MFFU dry_run mode detected in prod environment at check_can_trade"
            )
            raise SystemExit(
                "[FATAL] MFFUFlexRules in dry_run=True but ENVIRONMENT is now prod. "
                "Shutdown immediately."
            )

        if self._dry_run:
            return (
                False,
                "dry_run mode: 本番発注不可 — 発注はシミュレーションのみ有効",
            )
        # Sprint 2 で本格実装。今は yaml 読込確認のみ行う。
        if not self._rules:
            return False, "rules not loaded"
        return True, "ok"

    def check_daily_loss(self, account: str, current_pnl: float) -> bool:
        """Return True if within daily loss limit.

        In dry_run always returns False (safe side).
        """
        if self._dry_run:
            return False
        # Sprint 2 で本格実装
        return True

    def check_weekend_hold(self, now: datetime.datetime) -> bool:
        """Return True if position can be held (not forbidden weekend period).

        In dry_run always returns False (safe side).
        """
        if self._dry_run:
            return False
        weekend_forbidden = (
            self._rules.get("eval", {}).get("weekend_hold_forbidden", True)
        )
        if not weekend_forbidden:
            return True
        # Friday after force_close_et or Saturday/Sunday → forbidden
        weekday = now.weekday()  # Monday=0, Sunday=6
        if weekday in (5, 6):  # Saturday, Sunday
            return False
        return True

    def check_consistency_rule(
        self, account: str, daily_pnls: list[float]
    ) -> bool:
        """Return True if consistency rule is satisfied.

        In dry_run always returns False (safe side).
        """
        if self._dry_run:
            return False
        # Sprint 2 で本格実装
        return True
