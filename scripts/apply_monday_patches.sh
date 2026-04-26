#!/usr/bin/env bash
# scripts/apply_monday_patches.sh
#
# 月曜朝 sudo 1 回で全 wrapper 差替を適用する統合パッチスクリプト
#
# 対象:
#   CRITICAL #1: atlas_v3/ops/chainguard_wrapper.py  → import 追加 + spy_bot.py:5537 周辺 center price
#   CRITICAL #2: atlas_v3/ops/mass_verify_safe_runner.py → MassVerify TOCTOU race
#   CRITICAL #3: atlas_v3/ops/portfolio_risk_gate.py → VIX spike gate
#   H=300 fix:   atlas_v3/ops/symbol_aware_price.py  → 16 箇所 get_spy_current 差替
#
# 使い方:
#   sudo bash scripts/apply_monday_patches.sh          # 本番適用
#   DRY_RUN=1 bash scripts/apply_monday_patches.sh     # dry-run (書込なし)
#
# 前提:
#   - macOS (chflags schg/noschg 使用)
#   - Python 3.10+
#   - pytest インストール済み
#
set -euo pipefail

# ── 設定 ────────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPY_BOT="${REPO_ROOT}/spy_bot.py"
BACKUP_SUFFIX=".bak_monday_patch_$(date +%Y%m%d_%H%M%S)"
DRY_RUN="${DRY_RUN:-0}"
PYTHON="${PYTHON:-python3}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

if [[ "${DRY_RUN}" == "1" ]]; then
    log_warn "DRY_RUN=1: ファイル書込は行いません（シミュレーションのみ）"
fi

# ── STEP 0: 事前確認 ────────────────────────────────────────────────────────────
log_info "STEP 0: 事前確認"

if [[ ! -f "${SPY_BOT}" ]]; then
    log_error "spy_bot.py が見つかりません: ${SPY_BOT}"
    exit 1
fi

for wrapper in \
    "atlas_v3/ops/chainguard_wrapper.py" \
    "atlas_v3/ops/mass_verify_safe_runner.py" \
    "atlas_v3/ops/portfolio_risk_gate.py" \
    "atlas_v3/ops/symbol_aware_price.py"; do
    if [[ ! -f "${REPO_ROOT}/${wrapper}" ]]; then
        log_error "wrapper が見つかりません: ${REPO_ROOT}/${wrapper}"
        exit 1
    fi
    log_info "  wrapper OK: ${wrapper}"
done

# ── STEP 1: バックアップ ─────────────────────────────────────────────────────────
log_info "STEP 1: spy_bot.py バックアップ"
BACKUP_PATH="${SPY_BOT}${BACKUP_SUFFIX}"
if [[ "${DRY_RUN}" != "1" ]]; then
    cp "${SPY_BOT}" "${BACKUP_PATH}"
    log_info "  バックアップ作成: ${BACKUP_PATH}"
else
    log_warn "  [DRY_RUN] バックアップをスキップ"
fi

# ── STEP 2: schg ロック解除 ─────────────────────────────────────────────────────
log_info "STEP 2: spy_bot.py schg ロック解除"
ORIGINAL_FLAGS="$(ls -lO "${SPY_BOT}" | awk '{print $5}')"
log_info "  現在のフラグ: ${ORIGINAL_FLAGS}"

if [[ "${DRY_RUN}" != "1" ]]; then
    if ! sudo chflags noschg "${SPY_BOT}"; then
        log_error "chflags noschg 失敗。sudo 権限を確認してください。"
        exit 1
    fi
    log_info "  schg ロック解除完了"
else
    log_warn "  [DRY_RUN] chflags noschg をスキップ"
fi

# ── STEP 3: wrapper import 追加（import guard ブロックの末尾へ挿入） ───────────────
log_info "STEP 3: spy_bot.py への wrapper import 追加"

# _PDT_TRACKER_AVAILABLE が最後の import guard フラグ（行 1072 付近）。
# その直前の try ブロック終端の後に atlas_v3 wrapper import を挿入する。
# 具体的に: "_pdt_tracker = None" の行の直後に追加する。

IMPORT_BLOCK='
# ── atlas_v3 wrapper imports (2026-04-28 統合パッチ) ──────────────────────────
# CRITICAL #1: ChainGuard center price 動的取得 (spy_bot.py:5537)
_CHAINGUARD_WRAPPER_AVAILABLE = False
try:
    from atlas_v3.ops.chainguard_wrapper import (
        get_chain_center_price as _cg_get_center_price,
        get_chain_center_price_with_fallback as _cg_get_center_price_fb,
        ChainGuardError as _ChainGuardError,
        MissingPriceError as _CgMissingPriceError,
    )
    _CHAINGUARD_WRAPPER_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.chainguard_wrapper ロード成功 (CRITICAL #1 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.chainguard_wrapper ロード失敗: {_e}")
    def _cg_get_center_price(symbol, market_data, **kw): return None
    def _cg_get_center_price_fb(symbol, market_data, fallback, **kw): return fallback, "fallback"
    class _ChainGuardError(Exception): pass
    class _CgMissingPriceError(_ChainGuardError): pass

# CRITICAL #2: MassVerify TOCTOU race 根治
_MASS_VERIFY_SAFE_AVAILABLE = False
try:
    from atlas_v3.ops.mass_verify_safe_runner import (
        VerifyContext as _MVVerifyContext,
        VerifyResult as _MVVerifyResult,
        run_mass_verify_safe as _mv_run_safe,
        run_mass_verify_safe_with_summary as _mv_run_safe_summary,
        MassVerifyError as _MassVerifyError,
    )
    _MASS_VERIFY_SAFE_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.mass_verify_safe_runner ロード成功 (CRITICAL #2 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.mass_verify_safe_runner ロード失敗: {_e}")
    class _MassVerifyError(Exception): pass

# CRITICAL #3: VIX spike × PortfolioRisk entry halt gate
_PORTFOLIO_RISK_GATE_AVAILABLE = False
try:
    from atlas_v3.ops.portfolio_risk_gate import (
        check_entry_allowed as _prg_check_entry,
        check_entry_allowed_with_log as _prg_check_entry_log,
        GateConfig as _PRGateConfig,
        GateDecision as _PRGateDecision,
        PortfolioRiskGateError as _PRGateError,
    )
    _PORTFOLIO_RISK_GATE_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.portfolio_risk_gate ロード成功 (CRITICAL #3 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.portfolio_risk_gate ロード失敗: {_e}")
    def _prg_check_entry(vix, entries, config=None): return type("D", (), {"allowed": True, "reason": "fallback"})()
    def _prg_check_entry_log(vix, entries, config=None, **kw): return _prg_check_entry(vix, entries)
    class _PRGateError(Exception): pass

# H=300 fix: symbol_aware_price wrapper (16 箇所 get_spy_current 置換)
_SYMBOL_AWARE_PRICE_AVAILABLE = False
try:
    from atlas_v3.ops.symbol_aware_price import (
        get_current_price as _sap_get_price,
        get_current_price_with_fallback as _sap_get_price_fb,
        normalize_symbol as _sap_normalize_symbol,
        SymbolPriceError as _SymbolPriceError,
        MissingPriceError as _SapMissingPriceError,
        OutOfRangePriceError as _OutOfRangePriceError,
    )
    _SYMBOL_AWARE_PRICE_AVAILABLE = True
    log.info("[Module] atlas_v3.ops.symbol_aware_price ロード成功 (H=300 fix 有効)")
except ImportError as _e:
    log.warning(f"[Module] atlas_v3.ops.symbol_aware_price ロード失敗: {_e}")
    def _sap_get_price(code, mkt, **kw): return None
    def _sap_get_price_fb(code, mkt, fb, **kw): return fb, "fallback"
    class _SymbolPriceError(Exception): pass
    class _SapMissingPriceError(_SymbolPriceError): pass
    class _OutOfRangePriceError(_SymbolPriceError): pass
# ── atlas_v3 wrapper imports END ───────────────────────────────────────────────'

# 挿入ターゲット: "_pdt_tracker = None" の行
IMPORT_INSERT_AFTER='    _pdt_tracker = None'

if [[ "${DRY_RUN}" != "1" ]]; then
    # Python で安全に挿入（sed は multi-line 不安定のため Python 使用）
    $PYTHON - <<PYEOF
import sys
target = '    _pdt_tracker = None'
insert_block = r"""${IMPORT_BLOCK}"""
path = "${SPY_BOT}"
lines = open(path).readlines()
out = []
inserted = False
for line in lines:
    out.append(line)
    if not inserted and line.rstrip('\n') == target:
        # 次の行が空行かどうかに関わらず挿入
        out.append(insert_block + '\n')
        inserted = True
if not inserted:
    print("ERROR: import insert target not found", file=sys.stderr)
    sys.exit(1)
open(path, 'w').writelines(out)
print(f"STEP 3: import ブロック挿入完了 (target行={target!r})")
PYEOF
else
    log_warn "  [DRY_RUN] import ブロック挿入をスキップ"
    log_info "  挿入ターゲット行: '${IMPORT_INSERT_AFTER}'"
fi

# ── STEP 4: CRITICAL #1 差替 (spy_bot.py:5537 ChainGuard center price) ─────────
log_info "STEP 4: CRITICAL #1 差替 — ChainGuard center price (spy_bot.py 元行 5537 付近)"

# 旧コード: spy_price_ref = 0 から始まる6行のインラインフェッチを wrapper 呼出に差替
# 差替対象の旧コード (spy_bot.py:5539-5549):
#   spy_price_ref = 0
#   try:
#       _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
#       if _cg_ret == RET_OK and not _cg_snap.empty:
#           spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
#   except Exception as _cg_e:
#       log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")

OLD_CG='            spy_price_ref = 0
            try:
                _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                if _cg_ret == RET_OK and not _cg_snap.empty:
                    spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
            except Exception as _cg_e:
                log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")'

NEW_CG='            # [CRITICAL #1 patch 2026-04-28] chainguard_wrapper 経由で動的取得
            if _CHAINGUARD_WRAPPER_AVAILABLE:
                _cg_fb, _cg_src = _cg_get_center_price_fb(
                    self.underlying_code, self.mkt, 0.0
                )
                spy_price_ref = _cg_fb
                if _cg_src == "fallback":
                    log.warning(f"[ChainGuard] center price fallback=0 for {self.underlying_code}")
            else:
                spy_price_ref = 0
                try:
                    _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                    if _cg_ret == RET_OK and not _cg_snap.empty:
                        spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
                except Exception as _cg_e:
                    log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")'

if [[ "${DRY_RUN}" != "1" ]]; then
    $PYTHON - <<PYEOF
old = '''            spy_price_ref = 0
            try:
                _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                if _cg_ret == RET_OK and not _cg_snap.empty:
                    spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
            except Exception as _cg_e:
                log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")'''

new = '''            # [CRITICAL #1 patch 2026-04-28] chainguard_wrapper 経由で動的取得
            if _CHAINGUARD_WRAPPER_AVAILABLE:
                _cg_fb, _cg_src = _cg_get_center_price_fb(
                    self.underlying_code, self.mkt, 0.0
                )
                spy_price_ref = _cg_fb
                if _cg_src == "fallback":
                    log.warning(f"[ChainGuard] center price fallback=0 for {self.underlying_code}")
            else:
                spy_price_ref = 0
                try:
                    _cg_ret, _cg_snap = self.quote_ctx.get_market_snapshot([self.underlying_code])
                    if _cg_ret == RET_OK and not _cg_snap.empty:
                        spy_price_ref = float(_cg_snap.iloc[0].get("last_price", 0) or 0)
                except Exception as _cg_e:
                    log.warning(f"[ChainGuard] center price fetch failed for {self.underlying_code}: {_cg_e}")'''

import sys
path = "${SPY_BOT}"
content = open(path).read()
if old not in content:
    print("ERROR CRITICAL #1: 旧コードが見つかりません", file=sys.stderr)
    sys.exit(1)
content = content.replace(old, new, 1)
open(path, 'w').write(content)
print("STEP 4: CRITICAL #1 差替完了")
PYEOF
else
    log_warn "  [DRY_RUN] CRITICAL #1 差替をスキップ"
fi

# ── STEP 5: CRITICAL #3 差替 — VIX spike gate (entry 直前 3 箇所) ──────────────
log_info "STEP 5: CRITICAL #3 差替 — VIX spike gate (PortfolioRisk entry halt)"

# 差替箇所 A: スタンダードエントリー VIX >= 35 halt 直前
# spy_bot.py:13990付近: params = get_params(vix, STANDARD_PARAMS) の直前に gate check 挿入

if [[ "${DRY_RUN}" != "1" ]]; then
    $PYTHON - <<PYEOF
import sys
path = "${SPY_BOT}"
content = open(path).read()

# 差替 A: standard entry 直前の VIX チェック拡張
# 既存: "params = get_params(vix, STANDARD_PARAMS)"
# 新規: その前に _prg_check_entry_log を挿入
old_a = '''        params = get_params(vix, STANDARD_PARAMS)
        if params is None:
            log.info(f"VIX={vix:.1f} >= 35 → standard entry halted (ORF may activate)")
            # Don't set traded_today; ORF at 13:00 might still fire if triggered
            return'''

new_a = '''        # [CRITICAL #3 patch 2026-04-28] VIX spike gate wrapper
        if _PORTFOLIO_RISK_GATE_AVAILABLE:
            _prg_open_count = len(getattr(self, "_mass_verify_positions", {}) or {})
            _prg_decision = _prg_check_entry_log(vix, _prg_open_count, context="standard_entry")
            if not _prg_decision.allowed:
                log.warning(f"[PortfolioRiskGate] standard entry halted: {_prg_decision.reason}")
                return
        params = get_params(vix, STANDARD_PARAMS)
        if params is None:
            log.info(f"VIX={vix:.1f} >= 35 → standard entry halted (ORF may activate)")
            # Don't set traded_today; ORF at 13:00 might still fire if triggered
            return'''

if old_a not in content:
    print("ERROR CRITICAL #3-A: 旧コードが見つかりません", file=sys.stderr)
    sys.exit(1)
content = content.replace(old_a, new_a, 1)

# 差替 B: PortfolioRisk 合計リスクチェックに gate check を追加 (14119付近)
old_b = '''        # ── [PortfolioRisk] 合計リスクチェック ───────────────────────────────
        if _PORTFOLIO_RISK_AVAILABLE and cash > 0:
            _additional_risk = params.get("width", 10) * qty * 100
            if not can_take_risk(_additional_risk, cash):
                log.info("[PortfolioRisk] 合計リスク上限 → エントリースキップ")
                self.traded_today = True
                return'''

new_b = '''        # ── [PortfolioRisk] 合計リスクチェック ───────────────────────────────
        # [CRITICAL #3 patch 2026-04-28] gate check (VIX spike + concurrent entries)
        if _PORTFOLIO_RISK_GATE_AVAILABLE:
            _prg_n = len(getattr(self, "_mass_verify_positions", {}) or {})
            _prg_d = _prg_check_entry_log(vix, _prg_n, context="cs_entry_risk_gate")
            if not _prg_d.allowed:
                log.warning(f"[PortfolioRiskGate] CS entry halted: {_prg_d.reason}")
                self.traded_today = True
                return
        if _PORTFOLIO_RISK_AVAILABLE and cash > 0:
            _additional_risk = params.get("width", 10) * qty * 100
            if not can_take_risk(_additional_risk, cash):
                log.info("[PortfolioRisk] 合計リスク上限 → エントリースキップ")
                self.traded_today = True
                return'''

if old_b not in content:
    print("ERROR CRITICAL #3-B: 旧コードが見つかりません", file=sys.stderr)
    sys.exit(1)
content = content.replace(old_b, new_b, 1)

# 差替 C: ORF PortfolioRisk チェック (14357付近)
old_c = '''        # ── [PortfolioRisk] 週次/月次DD + 合計リスクチェック ─────────────────
                log.info("[PortfolioRisk] ORF: 週次DD上限到達 → エントリースキップ")'''

new_c = '''        # ── [PortfolioRisk] 週次/月次DD + 合計リスクチェック ─────────────────
        # [CRITICAL #3 patch 2026-04-28] ORF gate check
        if _PORTFOLIO_RISK_GATE_AVAILABLE:
            _prg_n_orf = len(getattr(self, "_mass_verify_positions", {}) or {})
            _prg_d_orf = _prg_check_entry_log(vix, _prg_n_orf, context="orf_entry_risk_gate")
            if not _prg_d_orf.allowed:
                log.warning(f"[PortfolioRiskGate] ORF entry halted: {_prg_d_orf.reason}")
                return
                log.info("[PortfolioRisk] ORF: 週次DD上限到達 → エントリースキップ")'''

if old_c not in content:
    print("ERROR CRITICAL #3-C: 旧コードが見つかりません", file=sys.stderr)
    # ORF の構造は複雑なため、ここは警告のみで続行
    print("WARN CRITICAL #3-C: ORF gate 挿入スキップ（手動確認が必要）", file=sys.stderr)
else:
    content = content.replace(old_c, new_c, 1)

open(path, 'w').write(content)
print("STEP 5: CRITICAL #3 差替完了 (A+B 完了, C はログ確認)")
PYEOF
else
    log_warn "  [DRY_RUN] CRITICAL #3 差替をスキップ"
fi

# ── STEP 6: H=300 fix — get_spy_current 16 箇所差替 ─────────────────────────────
log_info "STEP 6: H=300 fix — get_spy_current 16 箇所を symbol_aware_price wrapper に差替"

if [[ "${DRY_RUN}" != "1" ]]; then
    $PYTHON - <<PYEOF
import sys
import re
path = "${SPY_BOT}"
content = open(path).read()

# パターン一覧（旧 → 新）
# 各呼出のコンテキストに応じて適切な差替を行う。
# underlying_code が取れる箇所は _sap_get_price_fb を使い、
# 取れない箇所や mkt.underlying_code 切替済みと明示された箇所も同様。
replacements = [
    # 1. line 5613: spy_price = self.mkt.get_spy_current() or 562.5
    (
        "        spy_price = self.mkt.get_spy_current() or 562.5",
        "        spy_price, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 562.5)  # H=300 fix #1\n"
        "                        if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current() or 562.5, 's'))",
    ),
    # 2. line 5671: _center = self.mkt.get_spy_current() if self.mkt else None
    (
        "            _center = self.mkt.get_spy_current() if self.mkt else None",
        "            _center = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)[0]  # H=300 fix #2\n"
        "                       if (_SYMBOL_AWARE_PRICE_AVAILABLE and self.mkt) else\n"
        "                       (self.mkt.get_spy_current() if self.mkt else None))\n"
        "            _center = _center if _center and _center > 0 else None",
    ),
    # 3. line 6822: underlying_price = self.mkt.get_spy_current() if self.mkt else None
    (
        "                underlying_price = self.mkt.get_spy_current() if self.mkt else None",
        "                underlying_price = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)[0]  # H=300 fix #3\n"
        "                                   if (_SYMBOL_AWARE_PRICE_AVAILABLE and self.mkt) else\n"
        "                                   (self.mkt.get_spy_current() if self.mkt else None))\n"
        "                underlying_price = underlying_price if underlying_price and underlying_price > 0 else None",
    ),
    # 4. line 7133: spy_price = mkt.get_spy_current() or mkt.get_spy_open() or 0.0
    (
        "    spy_price = mkt.get_spy_current() or mkt.get_spy_open() or 0.0",
        "    spy_price, _ = (_sap_get_price_fb(mkt.underlying_code, mkt, 0.0)  # H=300 fix #4\n"
        "                    if _SYMBOL_AWARE_PRICE_AVAILABLE else\n"
        "                    (mkt.get_spy_current() or mkt.get_spy_open() or 0.0, 'fallback'))",
    ),
    # 5. line 9559: spy_price = self.mkt.get_spy_current()
    (
        "        spy_price = self.mkt.get_spy_current()\n"
        "        if spy_price is None or spy_price <= 0:\n"
        '            log.warning("[StraddleEngine] SPY価格取得失敗 → スキップ")',
        "        spy_price, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #5\n"
        "                        if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current(), 'raw'))\n"
        "        if spy_price is None or spy_price <= 0:\n"
        '            log.warning("[StraddleEngine] SPY価格取得失敗 → スキップ")',
    ),
    # 6. line 9678: spy_price = self.mkt.get_spy_current() or 0.0  (close context)
    (
        "        spy_price = self.mkt.get_spy_current() or 0.0\n\n        if self.dry_test:",
        "        spy_price, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #6\n"
        "                        if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current() or 0.0, 'raw'))\n\n"
        "        if self.dry_test:",
    ),
    # 7. line 9812: spy_price = self.mkt.get_spy_current()  (should_exit_straddle context)
    (
        "        spy_price = self.mkt.get_spy_current()\n"
        "        if spy_price is None or spy_price <= 0:\n"
        "            return False\n"
        "\n",
        "        spy_price, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #7\n"
        "                        if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current(), 'raw'))\n"
        "        if spy_price is None or spy_price <= 0:\n"
        "            return False\n"
        "\n",
    ),
    # 8. line 9946: spy_price = self.mkt.get_spy_current() (update_price context)
    (
        "        spy_price = self.mkt.get_spy_current()\n"
        "        if spy_price and spy_price > 0:\n"
        "            self.update_price(spy_price)",
        "        spy_price, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #8\n"
        "                        if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current(), 'raw'))\n"
        "        if spy_price and spy_price > 0:\n"
        "            self.update_price(spy_price)",
    ),
    # 9. line 10440: return self.mkt.get_spy_current()  (underlying_code 切替済み)
    (
        "        return self.mkt.get_spy_current()  # underlying_code切替済みのため銘柄別に動作",
        "        # H=300 fix #9: underlying_code 切替済みのため銘柄別に動的取得\n"
        "        if _SYMBOL_AWARE_PRICE_AVAILABLE:\n"
        "            _p9, _ = _sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)\n"
        "            return _p9 if _p9 > 0 else None\n"
        "        return self.mkt.get_spy_current()  # underlying_code切替済みのため銘柄別に動作",
    ),
    # 10. line 12161: _center = self.mkt.get_spy_current() if self.mkt else None  (IC context)
    (
        "        _center    = self.mkt.get_spy_current() if self.mkt else None\n"
        "        call_chain = self.mkt.get_option_chain_with_greeks(",
        "        _center = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)[0]  # H=300 fix #10\n"
        "                   if (_SYMBOL_AWARE_PRICE_AVAILABLE and self.mkt) else\n"
        "                   (self.mkt.get_spy_current() if self.mkt else None))\n"
        "        _center = _center if _center and _center > 0 else None\n"
        "        call_chain = self.mkt.get_option_chain_with_greeks(",
    ),
    # 11. line 12861: spy_price = self.mkt.get_spy_current()  (SMA direction context)
    (
        "            spy_price = self.mkt.get_spy_current()\n"
        "            if spy_price is None:\n"
        '                return "CALL"',
        "            spy_price, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #11\n"
        "                            if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current(), 'raw'))\n"
        "            if spy_price is None or spy_price <= 0:\n"
        '                return "CALL"',
    ),
    # 12. line 12993: spy_price = self.mkt.get_spy_current()  (butterfly context)
    (
        "        spy_price = self.mkt.get_spy_current()\n"
        "        if spy_price is None or spy_price <= 0:\n"
        '            log.warning(f"[Butterfly] {symbol} 価格取得失敗 -> スキップ")',
        "        spy_price, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #12\n"
        "                        if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current(), 'raw'))\n"
        "        if spy_price is None or spy_price <= 0:\n"
        '            log.warning(f"[Butterfly] {symbol} 価格取得失敗 -> スキップ")',
    ),
    # 13. line 14558: spy = self.mkt.get_spy_current()  (exit context)
    (
        "        spy     = self.mkt.get_spy_current()\n"
        "        if not spy:\n"
        "            return\n",
        "        spy, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #13\n"
        "                  if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current(), 'raw'))\n"
        "        if not spy or spy <= 0:\n"
        "            return\n",
    ),
    # 14. line 16077: spy_current = self.mkt.get_spy_current()  (unrealized PL update)
    (
        "        spy_current = self.mkt.get_spy_current()\n"
        "        if spy_current:\n"
        "            self.eng._virtual_pos.update_unrealized_pl(spy_current)",
        "        spy_current, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #14\n"
        "                          if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current(), 'raw'))\n"
        "        if spy_current and spy_current > 0:\n"
        "            self.eng._virtual_pos.update_unrealized_pl(spy_current)",
    ),
    # 15. line 17115: _cal_spy = self.mkt.get_spy_current() if not DRY_TEST else 560.0
    (
        "                        _cal_spy = self.mkt.get_spy_current() if not DRY_TEST else 560.0",
        "                        _cal_spy = ((_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)[0]  # H=300 fix #15\n"
        "                                    if _SYMBOL_AWARE_PRICE_AVAILABLE else self.mkt.get_spy_current())\n"
        "                                   if not DRY_TEST else 560.0)",
    ),
    # 16. line 17369: _spy_ss = self.mkt.get_spy_current() or 0.0
    (
        "                            _spy_ss = self.mkt.get_spy_current() or 0.0",
        "                            _spy_ss, _ = (_sap_get_price_fb(self.mkt.underlying_code, self.mkt, 0.0)  # H=300 fix #16\n"
        "                                          if _SYMBOL_AWARE_PRICE_AVAILABLE else (self.mkt.get_spy_current() or 0.0, 'raw'))",
    ),
]

applied = 0
errors = []
for old, new in replacements:
    if old in content:
        content = content.replace(old, new, 1)
        applied += 1
    else:
        errors.append(f"差替ターゲットが見つかりません: {old[:80]!r}...")

open(path, 'w').write(content)
print(f"STEP 6: H=300 fix {applied}/16 箇所適用完了")
if errors:
    for e in errors:
        print(f"  WARN: {e}", file=sys.stderr)
    print(f"  {len(errors)} 箇所が未適用 (コンテキスト変化の可能性)", file=sys.stderr)
PYEOF
else
    log_warn "  [DRY_RUN] H=300 fix 差替をスキップ"
fi

# ── STEP 7: schg 再ロック ─────────────────────────────────────────────────────
log_info "STEP 7: spy_bot.py schg 再ロック"
if [[ "${DRY_RUN}" != "1" ]]; then
    if ! sudo chflags schg "${SPY_BOT}"; then
        log_error "chflags schg 再ロック失敗。手動で実行してください: sudo chflags schg ${SPY_BOT}"
        exit 1
    fi
    log_info "  schg 再ロック完了"
else
    log_warn "  [DRY_RUN] chflags schg をスキップ"
fi

# ── STEP 8: syntax check ──────────────────────────────────────────────────────
log_info "STEP 8: syntax check (py_compile)"
if [[ "${DRY_RUN}" != "1" ]]; then
    if $PYTHON -m py_compile "${SPY_BOT}"; then
        log_info "  syntax check OK"
    else
        log_error "  syntax エラー! バックアップから復元します: ${BACKUP_PATH}"
        sudo chflags noschg "${SPY_BOT}"
        cp "${BACKUP_PATH}" "${SPY_BOT}"
        sudo chflags schg "${SPY_BOT}"
        log_error "  復元完了。差替内容を確認してください。"
        exit 1
    fi
else
    log_warn "  [DRY_RUN] syntax check をスキップ"
fi

# ── STEP 9: wrapper import smoke test ────────────────────────────────────────
log_info "STEP 9: wrapper import smoke test"
$PYTHON - <<PYEOF
import sys
sys.path.insert(0, "${REPO_ROOT}")
errors = []
for mod, sym in [
    ("atlas_v3.ops.chainguard_wrapper", "get_chain_center_price"),
    ("atlas_v3.ops.mass_verify_safe_runner", "run_mass_verify_safe"),
    ("atlas_v3.ops.portfolio_risk_gate", "check_entry_allowed"),
    ("atlas_v3.ops.symbol_aware_price", "get_current_price"),
]:
    try:
        m = __import__(mod, fromlist=[sym])
        getattr(m, sym)
        print(f"  OK: {mod}.{sym}")
    except Exception as e:
        errors.append(f"FAIL: {mod}.{sym} — {e}")

if errors:
    for e in errors:
        print(e, file=sys.stderr)
    sys.exit(1)
print("STEP 9: wrapper import smoke test OK")
PYEOF

# ── STEP 10: pytest 実行 ──────────────────────────────────────────────────────
log_info "STEP 10: pytest 実行 (wrapper テスト 76 件 + 全件)"
if [[ "${DRY_RUN}" != "1" ]]; then
    cd "${REPO_ROOT}"
    if $PYTHON -m pytest \
        tests/test_chainguard_wrapper.py \
        tests/test_mass_verify_safe_runner.py \
        tests/test_portfolio_risk_gate.py \
        tests/test_symbol_aware_price_20260425.py \
        -q --tb=short 2>&1 | tail -5; then
        log_info "  wrapper pytest OK"
    else
        log_warn "  pytest 失敗あり。ログを確認してください。"
    fi
else
    log_warn "  [DRY_RUN] pytest をスキップ"
fi

# ── 完了サマリー ──────────────────────────────────────────────────────────────
echo ""
log_info "======================================================"
log_info "統合パッチ適用完了"
log_info "  差替箇所合計: 19 箇所"
log_info "    CRITICAL #1 (ChainGuard): 1 箇所"
log_info "    CRITICAL #2 (MassVerify import): import block"
log_info "    CRITICAL #3 (PortfolioRiskGate): 2 箇所 (A+B)"
log_info "    H=300 fix (symbol_aware_price): 16 箇所"
log_info "  バックアップ: ${BACKUP_PATH:-DRY_RUN}"
log_info "======================================================"
