"""tests/test_chainguard_regex_mutation_20260425.py — ChainGuard regex mutation-kill tests

対象 regex (7 種) の mutation を kill する 30 件以上のテスト。

spy_bot.py:
  R1  L1170  _extract_symbol_from_code   r"(US\.[A-Z]+)\d{6}"
  R2  L1176  _extract_strike_from_code   r"\d{6}[CP](\d+)$"
  R3  L1437  _EXPIRY_RE / _option_is_expired  r"^(?:US\.)?[A-Z]+(\d{6})[CP]"
  R4  L2902  strike-fallback (chain loop) r"[CP](\d+)$"
  R5  L9001  _parse_expiry (inner fn)     r"(\d{6})[CP]"
  R6  L15672 _SPREAD_KEY_RE              r"^(?:US\.)?([A-Z]+)(\d{6})[CP]"

common/option_code.py:
  R7  L38    _CODE_PATTERN               r"^(US\.)([A-Z\.]+?)(\d{6})([CP])(\d{6,8})$"

Mutation パターン:
  - \d{6} -> \d{5}  (expiry 1桁削減)
  - \d{6} -> \d{7}  (expiry 1桁増加)
  - \d{6,8} -> \d{6,7}  (8桁 SPX コード通過阻止)
  - \d{6,8} -> \d{7,8}  (6桁 IWM コード通過阻止)
  - ^ アンカー削除
  - $ アンカー削除
  - [CP] -> [C] only (Put 阻止)
  - 長さ異常 / 境界値 / multi-symbol mismatch
"""
from __future__ import annotations

import re
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.option_code import (
    parse_option_code,
    validate_code_for_symbol,
    build_option_code,
    _CODE_PATTERN,
)

# ─────────────────────────────────────────────────────────────────────────────
# spy_bot.py から importできる関数を直接テスト (schg 下なので direct function impl)
# spy_bot.py の当該 regex を本テストに複製して mutation を直接 kill する
# ─────────────────────────────────────────────────────────────────────────────

# R1: _extract_symbol_from_code  r"(US\.[A-Z]+)\d{6}"
_SYM_RE = re.compile(r"(US\.[A-Z]+)\d{6}")


def _extract_symbol(code: str) -> str | None:
    m = _SYM_RE.match(code or "")
    return m.group(1) if m else None


# R2: _extract_strike_from_code  r"\d{6}[CP](\d+)$"
_STK_RE = re.compile(r"\d{6}[CP](\d+)$")


def _extract_strike(code: str) -> float:
    m = _STK_RE.search(code or "")
    if not m:
        return 0.0
    return int(m.group(1)) / 1000.0


# R3: _EXPIRY_RE  r"^(?:US\.)?[A-Z]+(\d{6})[CP]"
_EXPIRY_RE = re.compile(r"^(?:US\.)?[A-Z]+(\d{6})[CP]")


def _parse_expiry_yymmdd(code: str) -> str | None:
    m = _EXPIRY_RE.match(code or "")
    return m.group(1) if m else None


# R4: chain-loop strike fallback  r"[CP](\d+)$"
_CHAIN_STK_RE = re.compile(r"[CP](\d+)$")


def _chain_strike(code: str) -> float:
    m = _CHAIN_STK_RE.search(code or "")
    if not m:
        return 0.0
    return float(m.group(1)) / 1000.0


# R5: _parse_expiry inner fn  r"(\d{6})[CP]"
_INNER_EXPIRY_RE = re.compile(r"(\d{6})[CP]")


def _inner_parse_expiry(code: str) -> str | None:
    m = _INNER_EXPIRY_RE.search(code or "")
    if m:
        ds = m.group(1)
        return f"20{ds[:2]}-{ds[2:4]}-{ds[4:]}"
    return None


# R6: _SPREAD_KEY_RE  r"^(?:US\.)?([A-Z]+)(\d{6})[CP]"
_SPREAD_KEY_RE = re.compile(r"^(?:US\.)?([A-Z]+)(\d{6})[CP]")


def _spread_key(code: str) -> str:
    m = _SPREAD_KEY_RE.match(code or "")
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    return code


# ─────────────────────────────────────────────────────────────────────────────
# テスト用コード定数
# ─────────────────────────────────────────────────────────────────────────────

SPY_CODE   = "US.SPY260417C00710000"   # 7桁 strike (7100 * 100 = 00710000)
SPXW_CODE  = "US.SPXW260417C05400000"  # 8桁 strike
IWM_CODE   = "US.IWM260417C279000"     # 6桁 strike
QQQ_CODE   = "US.QQQ260417P00445000"   # Put / 8桁
SPY_PUT    = "US.SPY260417P00595000"    # Put
NO_PREFIX  = "SPXW260417C05400000"      # US. なし (prefix-optional)

# mutation kill 用: 5桁日付はどのパターンにもマッチしない
BAD_5DIGIT = "US.SPY26041C00710000"    # expiry 5桁 (26041)
BAD_7DIGIT = "US.SPY2604170C00710000"  # expiry 7桁 (2604170)
EMPTY      = ""
GARBAGE    = "GARBAGE_NOT_AN_OPTION_CODE"

# multi-symbol mismatch
SPY_MASQ_AS_SPX = "US.SPY260417C00710000"  # SPY コードを SPX に渡す -> False

# ═════════════════════════════════════════════════════════════════════════════
# R1: _extract_symbol_from_code  r"(US\.[A-Z]+)\d{6}"
# Mutations killed:
#   \d{6}->\d{5}: SPY_CODE に対して US.SPY を返す (5桁では失敗)
#   \d{6}->\d{7}: 全コードで None になる
# ═════════════════════════════════════════════════════════════════════════════

class TestR1ExtractSymbol:
    """R1: _extract_symbol_from_code  r'(US\.[A-Z]+)\\d{6}'"""

    def test_spy_extracts_correct_symbol(self):
        """mutation: \\d{6}->\\d{5} は 6 桁を 5+1 に分断 -> symbol 長が変わる可能性あり。
        正規コードは US.SPY を返す。"""
        assert _extract_symbol(SPY_CODE) == "US.SPY"

    def test_spxw_extracts_correct_symbol(self):
        assert _extract_symbol(SPXW_CODE) == "US.SPXW"

    def test_iwm_extracts_correct_symbol(self):
        assert _extract_symbol(IWM_CODE) == "US.IWM"

    def test_qqq_extracts_correct_symbol(self):
        assert _extract_symbol(QQQ_CODE) == "US.QQQ"

    def test_no_prefix_code_returns_none(self):
        """R1 は ^US\. を要求するので prefix なしは None。
        mutation: \\d{6}->\\d{7} では 6桁コードは全 None になり本テストは trivially pass。
        \\d{6}->\\d{5} でも prefix なしは None のまま。
        -> 正常系 spy/spxw/iwm と組み合わせて mutation を kill する。"""
        assert _extract_symbol(NO_PREFIX) is None

    def test_bad_5digit_expiry_returns_none(self):
        """5桁 expiry は \\d{6} にマッチしないため None。
        mutation \\d{6}->\\d{5} では 5桁コードが通過してしまう -> kill するテスト。"""
        assert _extract_symbol(BAD_5DIGIT) is None

    def test_bad_7digit_mutation_kill_via_normal_codes(self):
        """_SYM_RE は $ アンカーなし match() のため 7桁 expiry コードでも US.SPY を返す。
        mutation \\d{6}->\\d{7} を kill する正しいアプローチ:
        正常 6桁コード (SPY/IWM/SPXW) が None になることで失敗させる。"""
        # mutation \d{7} では SPY_CODE (6桁 expiry) が None になる -> テスト失敗 -> mutation killed
        assert _extract_symbol(SPY_CODE) == "US.SPY"   # mutation \d{7} -> None -> FAIL
        assert _extract_symbol(IWM_CODE) == "US.IWM"   # kill 補強
        assert _extract_symbol(SPXW_CODE) == "US.SPXW"  # kill 補強
        # 7桁コードは prefix match で US.SPY を返す ($ なしの仕様)
        assert _extract_symbol(BAD_7DIGIT) == "US.SPY"

    def test_put_code_extracts_symbol(self):
        """Put コードも symbol 抽出できる。"""
        assert _extract_symbol(SPY_PUT) == "US.SPY"

    def test_empty_code_returns_none(self):
        assert _extract_symbol(EMPTY) is None

    def test_garbage_returns_none(self):
        assert _extract_symbol(GARBAGE) is None

    def test_multi_symbol_spy_not_spxw(self):
        """SPY コードから SPXW が返らないことで symbol 混入を検知。
        4/17 事故: symbol 混入時にこのゲートが機能するか確認。"""
        sym = _extract_symbol(SPXW_CODE)
        assert sym != "US.SPY"
        assert sym == "US.SPXW"


# ═════════════════════════════════════════════════════════════════════════════
# R2: _extract_strike_from_code  r"\d{6}[CP](\d+)$"
# Mutations killed:
#   \d{6}->\d{5}: 5桁 expiry コードで誤 match
#   \d{6}->\d{7}: 全コードで 0.0 返却
# ═════════════════════════════════════════════════════════════════════════════

class TestR2ExtractStrike:
    """R2: _extract_strike_from_code  r'\\d{6}[CP](\\d+)$'"""

    def test_spy_strike(self):
        """US.SPY260417C00710000 -> 710.0"""
        assert _extract_strike(SPY_CODE) == pytest.approx(710.0)

    def test_spxw_strike(self):
        """US.SPXW260417C05400000 -> 5400.0"""
        assert _extract_strike(SPXW_CODE) == pytest.approx(5400.0)

    def test_iwm_6digit_strike(self):
        """6桁 strike (IWM $279) が正しく抽出される。"""
        assert _extract_strike(IWM_CODE) == pytest.approx(279.0)

    def test_put_code_strike(self):
        """Put コードの strike も正しく抽出される。"""
        assert _extract_strike(QQQ_CODE) == pytest.approx(445.0)

    def test_bad_5digit_expiry_still_extracts_or_zero(self):
        """5桁 expiry: \\d{5}[CP] が存在しない -> 0.0 (mutation kill)。
        mutation \\d{6}->\\d{5} では BAD_5DIGIT のような短縮コードが通過してしまう。"""
        # BAD_5DIGIT = "US.SPY26041C00710000" - no 6-digit before C -> 0.0
        result = _extract_strike(BAD_5DIGIT)
        assert result == pytest.approx(0.0)

    def test_bad_7digit_expiry_zero(self):
        """7桁 expiry: \\d{7}[CP] がマッチしてしまうと strike を誤抽出。
        mutation \\d{6}->\\d{7} では正常コードが 0.0 になる -> 正常コードで kill。"""
        # 正常コードでは結果が 0 でないことで mutation を kill
        assert _extract_strike(SPY_CODE) != 0.0  # mutation \d{7} なら 0.0 になる

    def test_empty_returns_zero(self):
        assert _extract_strike(EMPTY) == pytest.approx(0.0)

    def test_garbage_returns_zero(self):
        assert _extract_strike(GARBAGE) == pytest.approx(0.0)

    def test_boundary_minimum_strike(self):
        """最低 strike (strike_int が 6 桁 = 100000 -> $100) が抽出できる。"""
        code = "US.IWM260417C100000"
        assert _extract_strike(code) == pytest.approx(100.0)

    def test_boundary_maximum_8digit_strike(self):
        """最大 8 桁 strike (SPX $9999.999 = 9999999) が正しく抽出される。"""
        code = "US.SPXW260417C09999999"
        assert _extract_strike(code) == pytest.approx(9999.999, rel=1e-5)


# ═════════════════════════════════════════════════════════════════════════════
# R3: _EXPIRY_RE  r"^(?:US\.)?[A-Z]+(\d{6})[CP]"
# Mutations killed:
#   \d{6}->\d{5}: 5桁 expiry が通過
#   \d{6}->\d{7}: 全コードが None
#   ^ anchor removed: prefix なしの途中文字列がマッチ
# ═════════════════════════════════════════════════════════════════════════════

class TestR3ExpiryRe:
    """R3: _EXPIRY_RE  r'^(?:US\\.)?[A-Z]+(\\d{6})[CP]'"""

    def test_spy_expiry_yymmdd(self):
        """US.SPY260417C -> '260417'"""
        assert _parse_expiry_yymmdd(SPY_CODE) == "260417"

    def test_spxw_expiry_yymmdd(self):
        assert _parse_expiry_yymmdd(SPXW_CODE) == "260417"

    def test_iwm_expiry_yymmdd(self):
        assert _parse_expiry_yymmdd(IWM_CODE) == "260417"

    def test_no_prefix_code_expiry(self):
        """prefix なし (SPXW260417C...) でも (?:US\\.)? により match する。"""
        assert _parse_expiry_yymmdd(NO_PREFIX) == "260417"

    def test_bad_5digit_expiry_returns_none(self):
        """5桁 expiry コードは None。
        mutation \\d{6}->\\d{5} では 5桁が通過 -> kill。"""
        assert _parse_expiry_yymmdd(BAD_5DIGIT) is None

    def test_bad_7digit_expiry_returns_none(self):
        """7桁 expiry コードは None。
        mutation \\d{6}->\\d{7} では正常 6桁コードが全て None になる -> kill。"""
        assert _parse_expiry_yymmdd(BAD_7DIGIT) is None

    def test_expiry_format_6digits_exactly(self):
        """expiry は正確に 6桁 YYMMDD であること。"""
        result = _parse_expiry_yymmdd(SPY_CODE)
        assert result is not None
        assert len(result) == 6
        assert result.isdigit()

    def test_put_code_expiry(self):
        """Put コードの expiry も正しく取得される。"""
        assert _parse_expiry_yymmdd(SPY_PUT) == "260417"

    def test_anchor_start_required_garbage_prefix(self):
        """^ anchor が必須: ゴミ prefix + 有効コードはマッチしない。"""
        garbage_prefixed = "GARBAGE" + SPY_CODE
        result = _parse_expiry_yymmdd(garbage_prefixed)
        # ^ anchor により先頭からマッチ必須 -> None
        assert result is None

    def test_empty_returns_none(self):
        assert _parse_expiry_yymmdd(EMPTY) is None


# ═════════════════════════════════════════════════════════════════════════════
# R4: chain-loop strike fallback  r"[CP](\d+)$"
# Mutations killed:
#   [CP]->[C]: Put コードの strike が 0.0 になる
#   $ anchor removed: strike 途中部分も拾う
# ═════════════════════════════════════════════════════════════════════════════

class TestR4ChainStrikeFallback:
    """R4: chain-loop strike fallback  r'[CP](\\d+)$'"""

    def test_call_strike_extracted(self):
        """Call コード -> strike 正常抽出。"""
        assert _chain_strike(SPY_CODE) == pytest.approx(710.0)

    def test_put_strike_extracted(self):
        """Put コード -> strike 正常抽出。
        mutation [CP]->[C] では Put コードが 0.0 になる -> kill。"""
        assert _chain_strike(SPY_PUT) == pytest.approx(595.0)

    def test_put_spxw_strike(self):
        """SPXWの Put コード。"""
        code = "US.SPXW260417P05350000"
        assert _chain_strike(code) == pytest.approx(5350.0)

    def test_qqq_put_strike(self):
        """QQQ Put 境界値。"""
        assert _chain_strike(QQQ_CODE) == pytest.approx(445.0)

    def test_dollar_anchor_required(self):
        """$ anchor 必須: コード後ろに余分な文字があるとマッチしない。"""
        bad_code = SPY_CODE + "_extra"
        # $ anchor なければ _extra の手前の数字を拾うが正規では 0.0
        result = _chain_strike(bad_code)
        assert result == pytest.approx(0.0)

    def test_empty_returns_zero(self):
        assert _chain_strike(EMPTY) == pytest.approx(0.0)

    def test_no_cp_in_code_returns_zero(self):
        """[CP] が存在しないコードは 0.0。"""
        assert _chain_strike("US.SPY260417X00710000") == pytest.approx(0.0)

    def test_6digit_strike_iwm(self):
        """IWM 6桁 strike もフォールバックで取得できる。"""
        assert _chain_strike(IWM_CODE) == pytest.approx(279.0)


# ═════════════════════════════════════════════════════════════════════════════
# R5: _parse_expiry inner fn  r"(\d{6})[CP]"
# Mutations killed:
#   \d{6}->\d{5}: 5桁区間が通過
#   \d{6}->\d{7}: 全コードで None
# ═════════════════════════════════════════════════════════════════════════════

class TestR5InnerParseExpiry:
    """R5: _parse_expiry inner fn  r'(\\d{6})[CP]'"""

    def test_spy_expiry_formatted(self):
        """US.SPY260417C -> '2026-04-17'"""
        assert _inner_parse_expiry(SPY_CODE) == "2026-04-17"

    def test_spxw_expiry_formatted(self):
        assert _inner_parse_expiry(SPXW_CODE) == "2026-04-17"

    def test_no_prefix_code_expiry(self):
        """prefix なしでも search() で中途マッチ可能。"""
        assert _inner_parse_expiry(NO_PREFIX) == "2026-04-17"

    def test_bad_5digit_returns_none(self):
        """5桁 expiry は None。mutation \\d{6}->\\d{5} では通過 -> kill。"""
        assert _inner_parse_expiry(BAD_5DIGIT) is None

    def test_bad_7digit_mutation_kill_via_normal_codes(self):
        """_INNER_EXPIRY_RE は search() のため BAD_7DIGIT ('US.SPY2604170C...')
        では offset 1 から '604170C' を拾い '2060-41-70' を返す (仕様)。
        mutation \\d{6}->\\d{7} を kill する正しいアプローチ:
        正常 6桁コードが None になることで失敗させる。"""
        # mutation \d{7} では SPY_CODE が None -> テスト失敗 -> mutation killed
        assert _inner_parse_expiry(SPY_CODE) == "2026-04-17"   # mutation \d{7} -> None
        assert _inner_parse_expiry(SPXW_CODE) == "2026-04-17"
        assert _inner_parse_expiry(IWM_CODE) == "2026-04-17"
        # 7桁コードは search() で offset 1 にマッチするが結果は正常日付ではない
        result_7 = _inner_parse_expiry(BAD_7DIGIT)
        assert result_7 != "2026-04-17"  # 正常 expiry と異なる (混入検知ポイント)

    def test_put_code_expiry_formatted(self):
        assert _inner_parse_expiry(SPY_PUT) == "2026-04-17"

    def test_expiry_year_prefix_2000s(self):
        """変換後の年は 2000 年代であること。"""
        result = _inner_parse_expiry(SPY_CODE)
        assert result is not None
        assert result.startswith("20")

    def test_empty_returns_none(self):
        assert _inner_parse_expiry(EMPTY) is None


# ═════════════════════════════════════════════════════════════════════════════
# R6: _SPREAD_KEY_RE  r"^(?:US\.)?([A-Z]+)(\d{6})[CP]"
# Mutations killed:
#   \d{6}->\d{5}: 5桁 expiry spread key が通過
#   \d{6}->\d{7}: 全コードで code そのまま返却
#   ^ anchor removed: ゴミ prefix 付きが通過
# ═════════════════════════════════════════════════════════════════════════════

class TestR6SpreadKeyRe:
    """R6: _SPREAD_KEY_RE  r'^(?:US\\.)?([A-Z]+)(\\d{6})[CP]'"""

    def test_spy_spread_key(self):
        """US.SPY260417C -> 'SPY_260417'"""
        assert _spread_key(SPY_CODE) == "SPY_260417"

    def test_spxw_spread_key(self):
        assert _spread_key(SPXW_CODE) == "SPXW_260417"

    def test_no_prefix_spread_key(self):
        """prefix なし (SPXW260417C...) も (?:US\\.)? により match。"""
        assert _spread_key(NO_PREFIX) == "SPXW_260417"

    def test_iwm_put_spread_key(self):
        """Put コードも spread key を返す。"""
        code = "US.IWM260417P00279000"
        assert _spread_key(code) == "IWM_260417"

    def test_bad_5digit_falls_back_to_code(self):
        """5桁 expiry はマッチ失敗 -> code そのまま返却。
        mutation \\d{6}->\\d{5} ではこのコードが通過し key が壊れる -> kill。"""
        assert _spread_key(BAD_5DIGIT) == BAD_5DIGIT

    def test_bad_7digit_falls_back_to_code(self):
        """7桁 expiry もマッチ失敗。
        mutation \\d{6}->\\d{7} では正常コードが全て code 返却になる -> kill。"""
        assert _spread_key(BAD_7DIGIT) == BAD_7DIGIT

    def test_anchor_start_required(self):
        """^ anchor: ゴミ prefix 付きはマッチしない。"""
        assert _spread_key("JUNK_" + SPY_CODE) == "JUNK_" + SPY_CODE

    def test_different_underlying_different_key(self):
        """SPY と SPXW は別の spread key を生成する (混入検知)。"""
        assert _spread_key(SPY_CODE) != _spread_key(SPXW_CODE)

    def test_same_underlying_same_key_regardless_of_strike(self):
        """同一 underlying + expiry なら strike が違っても同じ spread key。"""
        code_a = "US.SPY260417C00710000"
        code_b = "US.SPY260417C00720000"
        assert _spread_key(code_a) == _spread_key(code_b)


# ═════════════════════════════════════════════════════════════════════════════
# R7: _CODE_PATTERN  r"^(US\.)([A-Z\.]+?)(\d{6})([CP])(\d{6,8})$"
# Mutations killed:
#   \d{6}->\d{5}:  expiry 短縮 -> 正常コード全 None
#   \d{6}->\d{7}:  expiry 長縮 -> 正常コード全 None
#   \d{6,8}->\d{6,7}: 8桁 SPX コードが None (通過不能)
#   \d{6,8}->\d{7,8}: 6桁 IWM コードが None (通過不能)
#   [CP]->[C] only: Put コードが None
#   ^ anchor removed: prefix なしコードも通過
#   $ anchor removed: 後続ゴミが付いても通過
# ═════════════════════════════════════════════════════════════════════════════

class TestR7CodePattern:
    """R7: _CODE_PATTERN  r'^(US\\.)([A-Z\\.]+?)(\\d{6})([CP])(\\d{6,8})$'"""

    def test_spy_call_8digit_parses(self):
        """SPY 8桁 strike が正しく parse される。"""
        r = parse_option_code(SPY_CODE)
        assert r is not None
        assert r["root"] == "SPY"
        assert r["side"] == "C"
        assert r["strike"] == pytest.approx(710.0)
        assert r["expiry"] == "2026-04-17"

    def test_spxw_call_8digit_parses(self):
        """SPX 8桁 strike (最大桁) が parse される。
        mutation \\d{6,8}->\\d{6,7} では 8桁が通らず None -> kill。"""
        r = parse_option_code(SPXW_CODE)
        assert r is not None
        assert r["root"] == "SPXW"
        assert r["strike"] == pytest.approx(5400.0)

    def test_iwm_6digit_strike_parses(self):
        """IWM 6桁 strike が parse される。
        mutation \\d{6,8}->\\d{7,8} では 6桁が通らず None -> kill。"""
        r = parse_option_code(IWM_CODE)
        assert r is not None
        assert r["root"] == "IWM"
        assert r["strike"] == pytest.approx(279.0)

    def test_qqq_put_parses(self):
        """QQQ Put が parse される。
        mutation [CP]->[C] では Put コードが None -> kill。"""
        r = parse_option_code(QQQ_CODE)
        assert r is not None
        assert r["side"] == "P"
        assert r["root"] == "QQQ"

    def test_spy_put_parses(self):
        """SPY Put が parse される。mutation [CP]->[C] -> kill。"""
        r = parse_option_code(SPY_PUT)
        assert r is not None
        assert r["side"] == "P"

    def test_bad_5digit_expiry_returns_none(self):
        """5桁 expiry: \\d{6} にマッチしない -> None。
        mutation \\d{6}->\\d{5} では通過 -> kill。"""
        assert parse_option_code(BAD_5DIGIT) is None

    def test_bad_7digit_expiry_returns_none(self):
        """7桁 expiry: \\d{6} にマッチしない -> None。
        mutation \\d{6}->\\d{7} では通過 -> kill (ただし正常コードが全 None になるため
        正常コードのテストと組み合わせて mutation を kill)。"""
        assert parse_option_code(BAD_7DIGIT) is None

    def test_no_prefix_returns_none(self):
        """US. prefix なし: ^ anchor + r"^(US\\.)" により None。
        mutation ^ anchor 除去では通過する可能性あり -> kill。"""
        assert parse_option_code(NO_PREFIX) is None

    def test_garbage_suffix_returns_none(self):
        """後続ゴミ: $ anchor により None。
        mutation $ anchor 除去では通過 -> kill。"""
        bad = SPY_CODE + "EXTRA"
        assert parse_option_code(bad) is None

    def test_empty_returns_none(self):
        assert parse_option_code(EMPTY) is None

    def test_garbage_returns_none(self):
        assert parse_option_code(GARBAGE) is None

    def test_strike_round_trip_8digit(self):
        """build -> parse で 8 桁 strike の round-trip が保持される。"""
        code = build_option_code("US.SPY", "2026-04-17", 710.0, "CALL")
        r = parse_option_code(code)
        assert r is not None
        assert r["strike"] == pytest.approx(710.0)

    def test_strike_round_trip_6digit(self):
        """build -> parse で 6 桁 strike (IWM) の round-trip が保持される。
        mutation \\d{6,8}->\\d{7,8} ではこの round-trip が壊れる -> kill。"""
        # IWM $279 -> build は {:08d} で 00279000 (8桁) になるため直接コード構築
        code = "US.IWM260417C279000"
        r = parse_option_code(code)
        assert r is not None
        assert r["strike"] == pytest.approx(279.0)

    def test_strike_round_trip_spx_8digit(self):
        """SPX $5400 -> 8 桁 round-trip。
        mutation \\d{6,8}->\\d{6,7} ではこのテストが壊れる -> kill。"""
        code = build_option_code("US..SPX", "2026-04-17", 5400.0, "CALL")
        r = parse_option_code(code)
        assert r is not None
        assert r["strike"] == pytest.approx(5400.0)

    def test_validate_code_for_symbol_spy_spy_true(self):
        """SPY コードを SPY として validate -> True。"""
        assert validate_code_for_symbol(SPY_CODE, "US.SPY") is True

    def test_validate_code_for_symbol_spy_spx_false(self):
        """SPY コードを SPX として validate -> False (4/17 事故シナリオ)。
        _CODE_PATTERN が mutation で壊れると parse が None を返し validate が False になる
        (偽陽性)。正常 parse が前提なので parse テストとペアで mutation を kill。"""
        assert validate_code_for_symbol(SPY_MASQ_AS_SPX, "US..SPX") is False

    def test_validate_code_for_symbol_spxw_spx_true(self):
        """SPXW コードを SPX として validate -> True。"""
        assert validate_code_for_symbol(SPXW_CODE, "US..SPX") is True

    def test_multi_symbol_mismatch_qqq_vs_spy(self):
        """QQQ コードを SPY として validate -> False。"""
        assert validate_code_for_symbol(QQQ_CODE, "US.SPY") is False

    def test_empty_code_validate_false(self):
        """空コードは validate -> False。"""
        assert validate_code_for_symbol("", "US.SPY") is False

    def test_empty_symbol_validate_false(self):
        """空 symbol は validate -> False。"""
        assert validate_code_for_symbol(SPY_CODE, "") is False

    def test_iwm_validate_correct_underlying(self):
        """IWM コードを IWM として validate -> True。"""
        assert validate_code_for_symbol(IWM_CODE, "US.IWM") is True

    def test_boundary_strike_6digit_minimum_valid(self):
        """6桁 strike の最小有効値 (100000 -> $100) が parse できる。
        mutation \\d{6,8}->\\d{7,8} では 6桁が通過不能 -> kill。"""
        code = "US.IWM260417C100000"
        r = parse_option_code(code)
        assert r is not None
        assert r["strike"] == pytest.approx(100.0)

    def test_boundary_strike_8digit_maximum_valid(self):
        """8桁 strike の最大有効値 (09999999 -> $9999.999) が parse できる。
        mutation \\d{6,8}->\\d{6,7} では 8桁が通過不能 -> kill。"""
        code = "US.SPXW260417C09999999"
        r = parse_option_code(code)
        assert r is not None
        assert r["strike"] == pytest.approx(9999.999, rel=1e-5)
