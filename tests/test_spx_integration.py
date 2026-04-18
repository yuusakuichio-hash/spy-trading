"""tests/test_spx_integration.py — SPX/SPXW 銘柄混入防止 統合テスト

4/17事故再現テストを含む30テスト以上。
全テストが PASS することが SPX 対応復活・混入根絶の完了基準。

実行方法:
    python3 -m pytest tests/test_spx_integration.py -v
    または
    python3 tests/test_spx_integration.py
"""

import re
import sys
import os

# common モジュールのパスを通す
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.symbol_meta import (
    SYMBOL_META,
    ALLOWED_SYMBOLS,
    get_meta,
    get_strike_interval,
    get_option_root,
    get_center_strike_tolerance,
    underlying_from_option_root,
    is_allowed,
    is_cash_settled,
    is_section_1256,
)
from common.option_code import (
    parse_option_code,
    validate_code_for_symbol,
    build_option_code,
    round_strike,
)


def _check(name: str, condition: bool):
    if condition:
        print(f"  PASS: {name}")
    else:
        print(f"  FAIL: {name}")
    return condition


def run_all_tests():
    results = []

    print("\n=== [1] symbol_meta: WHITELIST ===")
    results.append(_check("US.SPY は ALLOWED", is_allowed("US.SPY")))
    results.append(_check("US..SPX は ALLOWED (4/17対応で復活)", is_allowed("US..SPX")))
    results.append(_check("US.QQQ は ALLOWED", is_allowed("US.QQQ")))
    results.append(_check("US.NVDA は ALLOWED", is_allowed("US.NVDA")))
    results.append(_check("US.UNKNOWN は NOT ALLOWED", not is_allowed("US.UNKNOWN")))
    results.append(_check("空文字は NOT ALLOWED", not is_allowed("")))

    print("\n=== [2] symbol_meta: strike_interval ===")
    results.append(_check("US.SPY strike_interval=1.0", get_strike_interval("US.SPY") == 1.0))
    results.append(_check("US..SPX strike_interval=5.0 ($5刻み必須)", get_strike_interval("US..SPX") == 5.0))
    results.append(_check("US.IWM strike_interval=0.5", get_strike_interval("US.IWM") == 0.5))
    results.append(_check("US.NVDA strike_interval=2.5", get_strike_interval("US.NVDA") == 2.5))
    results.append(_check("不明銘柄 strike_interval=1.0 (フォールバック)", get_strike_interval("US.UNKNOWN") == 1.0))

    print("\n=== [3] symbol_meta: option_root ===")
    results.append(_check("US.SPY option_root_0dte=SPY", get_option_root("US.SPY") == "SPY"))
    results.append(_check("US..SPX option_root_0dte=SPXW (0DTE/Weekly)", get_option_root("US..SPX") == "SPXW"))
    results.append(_check("US..SPX option_root monthly=SPX", get_option_root("US..SPX", use_0dte=False) == "SPX"))
    results.append(_check("US.QQQ option_root_0dte=QQQ", get_option_root("US.QQQ") == "QQQ"))

    print("\n=== [4] symbol_meta: center_strike_tolerance ===")
    results.append(_check("US.SPY tolerance=0.20", get_center_strike_tolerance("US.SPY") == 0.20))
    results.append(_check("US..SPX tolerance=0.10 (±10%でSPY混入即検知)", get_center_strike_tolerance("US..SPX") == 0.10))
    results.append(_check("US.NVDA tolerance=0.25 (個別株)", get_center_strike_tolerance("US.NVDA") == 0.25))

    print("\n=== [5] symbol_meta: settlement + section1256 ===")
    results.append(_check("US.SPY is NOT cash_settled", not is_cash_settled("US.SPY")))
    results.append(_check("US..SPX is cash_settled (欧州型・行使リスクなし)", is_cash_settled("US..SPX")))
    results.append(_check("US.SPY is NOT section_1256", not is_section_1256("US.SPY")))
    results.append(_check("US..SPX is section_1256 (60/40税制優遇)", is_section_1256("US..SPX")))

    print("\n=== [6] option_code: parse ===")
    spy_parsed = parse_option_code("US.SPY260417C00710000")
    results.append(_check("SPY code parse: root=SPY", spy_parsed is not None and spy_parsed["root"] == "SPY"))
    results.append(_check("SPY code parse: underlying=US.SPY", spy_parsed is not None and spy_parsed["underlying"] == "US.SPY"))
    results.append(_check("SPY code parse: strike=710.0", spy_parsed is not None and spy_parsed["strike"] == 710.0))
    results.append(_check("SPY code parse: side=C", spy_parsed is not None and spy_parsed["side"] == "C"))
    results.append(_check("SPY code parse: expiry=2026-04-17", spy_parsed is not None and spy_parsed["expiry"] == "2026-04-17"))

    spxw_parsed = parse_option_code("US.SPXW260417C05400000")
    results.append(_check("SPXW code parse: root=SPXW", spxw_parsed is not None and spxw_parsed["root"] == "SPXW"))
    results.append(_check("SPXW code parse: underlying=US..SPX", spxw_parsed is not None and spxw_parsed["underlying"] == "US..SPX"))
    results.append(_check("SPXW code parse: strike=5400.0", spxw_parsed is not None and spxw_parsed["strike"] == 5400.0))

    results.append(_check("不正コードはparse=None", parse_option_code("invalid") is None))
    results.append(_check("空文字はparse=None", parse_option_code("") is None))

    print("\n=== [7] option_code: validate_code_for_symbol ===")
    # 正常系
    results.append(_check("SPY code + expected SPY = True",
                         validate_code_for_symbol("US.SPY260417C00710000", "US.SPY")))
    results.append(_check("SPXW code + expected US..SPX = True",
                         validate_code_for_symbol("US.SPXW260417C05400000", "US..SPX")))
    results.append(_check("QQQ code + expected QQQ = True",
                         validate_code_for_symbol("US.QQQ260417C00450000", "US.QQQ")))

    # 4/17事故再現テスト
    # シナリオ: underlying=SPX に切替中に SPY ATM(710) でchainを取得 → SPXW chainを引いた
    # -> validate_code_for_symbol でブロック
    results.append(_check(
        "[4/17事故再現] SPY code (710) + expected US..SPX = False (発注ブロック)",
        not validate_code_for_symbol("US.SPY260417C00710000", "US..SPX")
    ))
    results.append(_check(
        "[4/17事故再現] SPXW code (5400) + expected US.SPY = False (発注ブロック)",
        not validate_code_for_symbol("US.SPXW260417C05400000", "US.SPY")
    ))
    results.append(_check(
        "QQQ code + expected SPY = False",
        not validate_code_for_symbol("US.QQQ260417C00450000", "US.SPY")
    ))
    results.append(_check(
        "NVDA code + expected SPX = False",
        not validate_code_for_symbol("US.NVDA260417C00800000", "US..SPX")
    ))
    results.append(_check(
        "空コード + expected SPY = False",
        not validate_code_for_symbol("", "US.SPY")
    ))
    results.append(_check(
        "有効コード + 空expected = False",
        not validate_code_for_symbol("US.SPY260417C00710000", "")
    ))

    print("\n=== [8] option_code: build_option_code ===")
    spxw_code = build_option_code("US..SPX", "2026-04-18", 5400.0, "CALL")
    results.append(_check(
        "US..SPX build = US.SPXW260418C05400000",
        spxw_code == "US.SPXW260418C05400000"
    ))
    spy_code = build_option_code("US.SPY", "2026-04-18", 710.0, "PUT")
    results.append(_check(
        "US.SPY build = US.SPY260418P00710000",
        spy_code == "US.SPY260418P00710000"
    ))
    # SPX monthly (第3金曜) は SPX prefix
    spx_monthly_code = build_option_code("US..SPX", "2026-04-17", 5400.0, "CALL", use_0dte=False)
    results.append(_check(
        "US..SPX monthly build = US.SPX260417C05400000",
        spx_monthly_code == "US.SPX260417C05400000"
    ))
    # build したコードが validate で通るか確認
    results.append(_check(
        "build済みSPXWコードが validate でTrue",
        validate_code_for_symbol(spxw_code, "US..SPX")
    ))
    results.append(_check(
        "build済みSPYコードが validate でTrue",
        validate_code_for_symbol(spy_code, "US.SPY")
    ))

    print("\n=== [9] option_code: round_strike ===")
    results.append(_check("SPY round 561.3 -> 561.0 ($1刻み)", round_strike("US.SPY", 561.3) == 561.0))
    results.append(_check("SPX round 5412.7 -> 5415.0 ($5刻み)", round_strike("US..SPX", 5412.7) == 5415.0))
    results.append(_check("SPX round 5397.5 -> 5400.0 ($5刻み)", round_strike("US..SPX", 5397.5) == 5400.0))
    results.append(_check("SPX round 5402.4 -> 5400.0 ($5刻み)", round_strike("US..SPX", 5402.4) == 5400.0))
    results.append(_check("IWM round 201.3 -> 201.5 ($0.5刻み)", round_strike("US.IWM", 201.3) == 201.5))
    results.append(_check("NVDA round 882.3 -> 882.5 ($2.5刻み)", round_strike("US.NVDA", 882.3) == 882.5))

    print("\n=== [10] underlying_from_option_root ===")
    results.append(_check("SPXW -> US..SPX", underlying_from_option_root("SPXW") == "US..SPX"))
    results.append(_check("SPX -> US..SPX", underlying_from_option_root("SPX") == "US..SPX"))
    results.append(_check("SPY -> US.SPY", underlying_from_option_root("SPY") == "US.SPY"))
    results.append(_check("QQQ -> US.QQQ", underlying_from_option_root("QQQ") == "US.QQQ"))
    results.append(_check("UNKNOWN -> None", underlying_from_option_root("UNKNOWN") is None))

    print("\n=== [11] 4/17事故シナリオ: SPX-SPY混在チェーンシミュレーション ===")
    # シナリオ: MassVerify で underlying=US..SPX に切替後、
    # SPY の ATM strike ($710) で chain を作成 → validate で全てブロックされること
    # (実際の chain 取得は API が必要なので、生成コードでシミュレーション)
    spy_atm = 710.0
    contaminated_codes = [
        f"US.SPY260418C{int(s * 1000):08d}"
        for s in [700.0, 705.0, 710.0, 715.0, 720.0]
    ]
    expected_symbol = "US..SPX"
    all_blocked = all(
        not validate_code_for_symbol(code, expected_symbol)
        for code in contaminated_codes
    )
    results.append(_check(
        f"SPY codes ({contaminated_codes[:2]}...) が US..SPX として validate されない (全5件ブロック)",
        all_blocked
    ))

    # SPXW chain は全て通る
    spxw_strikes = [5380.0, 5385.0, 5390.0, 5395.0, 5400.0, 5405.0, 5410.0, 5415.0, 5420.0]
    spxw_codes = [
        f"US.SPXW260418C{int(s * 1000):08d}"
        for s in spxw_strikes
    ]
    all_passed = all(
        validate_code_for_symbol(code, "US..SPX")
        for code in spxw_codes
    )
    results.append(_check(
        f"SPXW codes ({spxw_codes[:2]}...) が US..SPX として validate される (全9件通過)",
        all_passed
    ))

    # SPXW chain は US.SPY として validate されない
    all_blocked_2 = all(
        not validate_code_for_symbol(code, "US.SPY")
        for code in spxw_codes
    )
    results.append(_check(
        f"SPXW codes が US.SPY として validate されない (全9件ブロック)",
        all_blocked_2
    ))

    # center_strike tolerance: SPX の ±10% で SPY strike を検知
    spx_center = 5400.0
    spy_strike_711 = 711.0
    _dev = abs(spy_strike_711 - spx_center) / spx_center
    _tol_spx = get_center_strike_tolerance("US..SPX")  # 0.10
    results.append(_check(
        f"SPX center={spx_center} に SPY strike={spy_strike_711} が乖離 {_dev*100:.1f}% > {_tol_spx*100:.0f}% で検知",
        _dev > _tol_spx
    ))

    # SPXW strike $5400 は SPX center $5400 の ±10% 内
    spxw_strike = 5400.0
    _dev2 = abs(spxw_strike - spx_center) / spx_center
    results.append(_check(
        f"SPX center={spx_center} に SPXW strike={spxw_strike} が乖離 {_dev2*100:.1f}% <= {_tol_spx*100:.0f}% で許可",
        _dev2 <= _tol_spx
    ))

    print("\n=== Summary ===")
    passed = sum(results)
    total = len(results)
    print(f"  {passed} / {total} PASS")
    if passed == total:
        print("  ALL TESTS PASSED")
        return True
    else:
        print(f"  {total - passed} FAILED")
        return False


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
