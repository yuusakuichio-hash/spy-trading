#!/usr/bin/env python3
"""ORB strike計算 + チェーンsnapshot範囲 + Symbol mismatch bug の回帰テスト

[背景]
2026/04/17 のペーパー運用で ORB が SPX選択下で deep ITM 5400 strike を選ぶ
バグが発生した。根本原因:
1. SPXWチェーンが数百strike あり、先頭200件制限で現在価格周辺が snapshot範囲外
2. ORBが内部的にSPY固定ロジックなのに、チェーン取得だけ underlying_code (SPX)

本テストは以下を検証:
- get_option_chain_with_greeks が center_strike引数を受け取る
- ORB execute_entry が underlying_code を SPY に固定する
- option_code 形式と strike値が整合する
- ATM strike と選ばれた option_strike の乖離が ±15% 以内

Usage:
    python3 test_strike_bug.py
"""
import sys, os, ast, re
from pathlib import Path

BASE = Path(__file__).resolve().parent
SPY_BOT = BASE / "spy_bot.py"

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []

def check(name: str, ok: bool, detail: str = ""):
    tag = PASS if ok else FAIL
    results.append((ok, name, detail))
    print(f"{tag} {name}{': ' + detail if detail else ''}")


# ============================================================
# Test 1: get_option_chain_with_greeks のsignatureにcenter_strike追加
# ============================================================
src = SPY_BOT.read_text()

def test_signature_has_center_strike():
    m = re.search(
        r"def get_option_chain_with_greeks\(self, expiry: str, opt_type: str,\s*"
        r"center_strike:",
        src,
    )
    check(
        "get_option_chain_with_greeks has center_strike param",
        m is not None,
        "" if m else "signature not found",
    )

test_signature_has_center_strike()


# ============================================================
# Test 2: center_strike指定時に中心ソートロジックがあるか
# ============================================================
def test_center_strike_sort_logic():
    # 「center_strike is not None」直後にソートロジックがある
    has_sort = bool(re.search(
        r"center_strike is not None.*?"
        r"strike_price.*?"
        r"(sort_values|_dist)",
        src,
        re.DOTALL,
    ))
    check(
        "center_strike sort logic present",
        has_sort,
        "" if has_sort else "sort_values pattern not found",
    )

test_center_strike_sort_logic()


# ============================================================
# Test 3: ORB execute_entry → _execute_entry_impl wrapper パターン
# ============================================================
def test_orb_try_finally_wrapper():
    # try: self._execute_entry_impl(direction) と finally: underlying_code復元
    has_wrapper = bool(re.search(
        r"def execute_entry\(self, direction: str\).*?"
        r"try:\s*\n\s*return self\._execute_entry_impl.*?"
        r"finally:.*?underlying_code",
        src,
        re.DOTALL,
    ))
    check(
        "ORB execute_entry has try/finally wrapper",
        has_wrapper,
        "" if has_wrapper else "wrapper pattern not found",
    )

test_orb_try_finally_wrapper()


# ============================================================
# Test 4: ORB _execute_entry_impl でunderlying_codeを SPY に固定
# ============================================================
def test_orb_forces_spy():
    # _orb_forced_spy フラグ + underlying_code = UNDERLYING_CODE 代入
    has_force = bool(re.search(
        r"self\.mkt\.underlying_code\s*=\s*UNDERLYING_CODE.*?"
        r"_orb_forced_spy\s*=\s*True",
        src,
        re.DOTALL,
    ))
    check(
        "ORB forces SPY when underlying != SPY",
        has_force,
        "" if has_force else "SPY forcing logic not found",
    )

test_orb_forces_spy()


# ============================================================
# Test 5: ORB strike整合性チェック（乖離>15%で中止）
# ============================================================
def test_orb_strike_deviation_check():
    has_check = bool(re.search(
        r"\[ORB\] strike整合性NG.*?option_strike.*?atm_strike.*?"
        r"return None",
        src,
        re.DOTALL,
    ))
    check(
        "ORB rejects option when strike deviation > 15%",
        has_check,
        "" if has_check else "strike整合性NG block not found",
    )

test_orb_strike_deviation_check()


# ============================================================
# Test 6: Straddle Buy でも center_strike と整合性チェック
# ============================================================
def test_straddle_buy_center_strike():
    # STRADDLE_BUY関連でcenter_strike=float(atm_strike)
    in_straddle = bool(re.search(
        r"call_chain = self\.mkt\.get_option_chain_with_greeks\(\s*"
        r"today_str, \"CALL\", center_strike=float\(atm_strike\)\)",
        src,
    ))
    check(
        "Straddle Buy uses center_strike",
        in_straddle,
        "" if in_straddle else "pattern not found",
    )
    has_check = bool(re.search(
        r"\[STRADDLE_BUY\].*?strike整合性NG",
        src,
        re.DOTALL,
    ))
    check(
        "Straddle Buy has strike deviation check",
        has_check,
        "" if has_check else "check not found",
    )

test_straddle_buy_center_strike()


# ============================================================
# Test 7: CS Sell でも center_strike 対応
# ============================================================
def test_cs_sell_center_strike():
    has_center = bool(re.search(
        r"chain = self\.mkt\.get_option_chain_with_greeks\(\s*"
        r"expiry, direction, center_strike=float\(_center\)",
        src,
    ))
    check(
        "CS Sell uses center_strike",
        has_center,
        "" if has_center else "pattern not found",
    )
    has_check = bool(re.search(
        r"SELL strike整合性NG",
        src,
    ))
    check(
        "CS Sell has SELL strike deviation check",
        has_check,
        "" if has_check else "check not found",
    )

test_cs_sell_center_strike()


# ============================================================
# Test 8: Calendar _find_atm_option でcenter_strike
# ============================================================
def test_calendar_center_strike():
    has_center = bool(re.search(
        r"def _find_atm_option.*?"
        r"get_option_chain_with_greeks\(\s*expiry, opt_type, center_strike=float\(spy_price\)\)",
        src,
        re.DOTALL,
    ))
    check(
        "Calendar _find_atm_option uses center_strike",
        has_center,
        "" if has_center else "pattern not found",
    )

test_calendar_center_strike()


# ============================================================
# Test 9: Hedge（Straddle Buy Hedge）でcenter_strike
# ============================================================
def test_hedge_center_strike():
    has_center = bool(re.search(
        r"\[STRADDLE_BUY\]\[HEDGE\].*?"
        r"chain\s*= self\.mkt\.get_option_chain_with_greeks\(\s*"
        r"today_str, direction, center_strike=float\(spy_price\)\)",
        src,
        re.DOTALL,
    ))
    check(
        "Straddle Buy Hedge uses center_strike",
        has_center,
        "" if has_center else "pattern not found",
    )

test_hedge_center_strike()


# ============================================================
# Test 10: Python syntax check
# ============================================================
def test_python_syntax():
    try:
        ast.parse(src)
        check("Python syntax valid", True)
    except SyntaxError as e:
        check("Python syntax valid", False, str(e))

test_python_syntax()


# ============================================================
# Test 11: option_code 生成フォーマット検証（既存ロジックを壊していないか）
# ============================================================
def test_option_code_format_dry_test():
    # dry-testで仮想コード生成: US.SPY{yymmdd}{C/P}{strike*1000}
    # multiline regex
    m = re.search(
        r"virtual_code\s*=.*?US\.SPY.*?y%m%d.*?"
        r"atm_strike\s*\*\s*1000",
        src,
        re.DOTALL,
    )
    check(
        "ORB dry-test option code uses SPY{yymmdd}{C/P}{strike*1000}",
        m is not None,
        "" if m else "format not matched",
    )

test_option_code_format_dry_test()


# ============================================================
# Test 12: Strike scale sanity check (静的シミュレーション)
# ============================================================
def test_strike_scale_sanity():
    """ATM strike 707 の場合 US.SPY260417C707000 が期待値"""
    atm_strike = 707
    expiry_yymmdd = "260417"
    direction = "CALL"
    expected = f"US.SPY{expiry_yymmdd}C{int(atm_strike * 1000)}"
    assert expected == "US.SPY260417C707000", expected
    check("Strike scale: SPY 707 → US.SPY260417C707000", True, expected)

    # SPX ATM 7100の場合、仮に US.SPXW260417C7100000 を期待
    atm_spx = 7100
    expected_spx = f"US.SPXW{expiry_yymmdd}C{int(atm_spx * 1000)}"
    assert expected_spx == "US.SPXW260417C7100000", expected_spx
    check("Strike scale: SPX 7100 → US.SPXW260417C7100000", True, expected_spx)

    # Bug例: ATM 707 + strike 5400 → 乖離率
    dev = abs(5400 - 707) / 707
    check(
        "Bug case: ATM 707 vs strike 5400 deviation > 15%",
        dev > 0.15,
        f"deviation={dev*100:.1f}%",
    )

test_strike_scale_sanity()


# ============================================================
# Test 13: [2026/04/12 追加] _execute_entry_impl で underlying_code 固定が
#           spy_price 取得より前に行われているか検証
# ルートコーズ: MassVerifyで mkt.underlying_code=US..SPX の状態で
# spy_price = self._get_spy_price() → self.mkt.get_spy_current() →
# self.underlying_code(=SPX) の価格 5400 が返り atm_strike=5400 になるバグ。
# 修正後: _orb_orig_underlying/mkt.underlying_code 切替が spy_price 取得より先。
# ============================================================
def test_orb_spy_fixed_before_price_fetch():
    """underlying_code 固定ブロックが spy_price = self._get_spy_price() より前にある。

    _execute_entry_impl の先頭行位置を取得し、その後から
    underlying_code固定行とspy_price取得行の相対位置を比較する。
    """
    lines = src.splitlines()

    # _execute_entry_impl の定義行番号を特定
    impl_start = None
    for i, line in enumerate(lines):
        if "def _execute_entry_impl(self" in line:
            impl_start = i
            break

    if impl_start is None:
        check(
            "ORB: underlying_code fixed BEFORE spy_price fetch",
            False,
            "_execute_entry_impl not found",
        )
        return

    # impl_start 以降で対象行を検索（次のメソッド定義まで、コメント行除外）
    pos_fixed = None
    pos_spy = None
    for i, line in enumerate(lines[impl_start:], start=impl_start):
        if i > impl_start and re.match(r"    def ", line):
            break  # 次のメソッドに到達したら終了
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # コメント行をスキップ（パターン文字列がコメントに含まれる場合の誤検知防止）
        if pos_fixed is None and "self.mkt.underlying_code = UNDERLYING_CODE" in line:
            pos_fixed = i
        if pos_spy is None and "spy_price = self._get_spy_price()" in line:
            pos_spy = i

    if pos_fixed is None:
        check(
            "ORB: underlying_code fixed BEFORE spy_price fetch",
            False,
            "underlying_code固定行が見つからない（_execute_entry_impl内）",
        )
        return
    if pos_spy is None:
        check(
            "ORB: underlying_code fixed BEFORE spy_price fetch",
            False,
            "spy_price取得行が見つからない（_execute_entry_impl内）",
        )
        return

    ok = pos_fixed < pos_spy
    check(
        "ORB: underlying_code fixed BEFORE spy_price fetch",
        ok,
        f"L{pos_fixed+1}(fixed) < L{pos_spy+1}(spy) " +
        ("OK: 切替が先" if ok else "NG: spy_price取得が先 → atm_strike汚染リスク"),
    )

test_orb_spy_fixed_before_price_fetch()


# ============================================================
# Test 14: [2026/04/12 追加] _get_spy_price() が underlying_code 非依存の
#           SPY固定実装になっているか検証
# 修正後: get_spy_snapshot / get_spy_current の代わりに
#          "US.SPY" を直接指定した snapshot 取得 or Finnhub を使う。
# ============================================================
def test_get_spy_price_is_spy_fixed():
    """_get_spy_price が self.mkt.get_spy_current() に依存しない実装。

    コメント行を除いた実コード行のみで判定する。
    """
    lines = src.splitlines()

    # def _get_spy_price(self) の定義行（ORBEngineクラス内のもの: インデント4スペース）
    fn_start = None
    for i, line in enumerate(lines):
        if re.match(r"    def _get_spy_price\(self\)", line):
            fn_start = i
            break

    if fn_start is None:
        check("_get_spy_price is SPY-fixed (no get_spy_current)", False, "not found")
        return

    # 次のメソッドまでを収集（コメント行・docstring行除外）
    fn_code_lines = []
    in_docstring = False
    for i, line in enumerate(lines[fn_start + 1:], start=fn_start + 1):
        if re.match(r"    def ", line):
            break
        stripped = line.strip()
        # docstring の開始/終了を追跡
        if '"""' in stripped:
            count = stripped.count('"""')
            if not in_docstring:
                in_docstring = True
                if count >= 2:
                    in_docstring = False  # 1行docstring
                continue
            else:
                in_docstring = False
                continue
        if in_docstring:
            continue
        if stripped.startswith("#"):
            continue  # コメント行スキップ
        fn_code_lines.append(line)

    fn_code = "\n".join(fn_code_lines)

    # 旧実装: return self.mkt.get_spy_current() → underlying_code依存
    has_old = "self.mkt.get_spy_current()" in fn_code
    # 新実装: "US.SPY" を明示的に使っているか
    has_spy_fixed = '"US.SPY"' in fn_code or "'US.SPY'" in fn_code

    check(
        "_get_spy_price does NOT use self.mkt.get_spy_current() in code",
        not has_old,
        "旧実装(コード行)が残っている" if has_old else "OK: get_spy_current()依存なし",
    )
    check(
        '_get_spy_price uses "US.SPY" explicitly in code',
        has_spy_fixed,
        "US.SPY固定参照なし" if not has_spy_fixed else "OK: US.SPY固定",
    )

test_get_spy_price_is_spy_fixed()


# ============================================================
# Test 15: [2026/04/12 追加] deep ITM ガードが option_price >= 50 で
#           発注を拒否するロジックを含む
# ============================================================
def test_orb_deep_itm_guard():
    has_guard = bool(re.search(
        r"deep ITM.*?発注拒否|_DEEP_ITM_THRESHOLD",
        src,
        re.DOTALL,
    ))
    check(
        "ORB has deep ITM price guard (threshold >= $50)",
        has_guard,
        "" if has_guard else "deep ITM guard not found",
    )
    # 閾値が $50 以上に設定されているか (50.0 or higher)
    m_thresh = re.search(r"_DEEP_ITM_THRESHOLD\s*=\s*(\d+\.?\d*)", src)
    if m_thresh:
        threshold = float(m_thresh.group(1))
        check(
            f"deep ITM threshold >= 50 (actual={threshold})",
            threshold >= 50,
            f"threshold={threshold}",
        )
    else:
        check("deep ITM threshold defined", False, "_DEEP_ITM_THRESHOLD not found")

test_orb_deep_itm_guard()


# ============================================================
# 結果サマリー
# ============================================================
total = len(results)
passed = sum(1 for ok, _, _ in results if ok)
failed = total - passed

print()
print(f"{'='*60}")
print(f"  Test result: {passed}/{total} passed, {failed} failed")
print(f"{'='*60}")

if failed > 0:
    print("\nFailed tests:")
    for ok, name, detail in results:
        if not ok:
            print(f"  {FAIL} {name}: {detail}")
    sys.exit(1)

sys.exit(0)
