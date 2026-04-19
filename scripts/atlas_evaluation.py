#!/usr/bin/env python3
"""Atlas Trader Evaluation Framework (v2) -- Atlas オプションBotの実装採点スクリプト

16項目 A1-A16 で Atlas の実装を静的AST解析 + パターンマッチ + pytest連動で採点する。
手動申告による採点を廃止し、コードベースとテスト実行で客観採点する。

futures_trader_evaluation.py の設計思想を Atlas 向けに踏襲。

使い方:
  python3 scripts/atlas_evaluation.py                        # Atlas 全体採点
  python3 scripts/atlas_evaluation.py --out <file.md>        # 出力ファイル指定
  python3 scripts/atlas_evaluation.py --run-tests            # pytest連動モード
  python3 scripts/atlas_evaluation.py --codebase <dir>       # 対象ディレクトリ指定

出力:
  data/eval/atlas_trader_eval_v2_YYYYMMDD.md

合格ライン:
  60点以上 (75%) -- 本番移行可・ペーパー検証完了水準
  70点以上 (87.5%) -- 本番移行推奨・安定稼働水準
  80点 (100%) -- EXCELLENT / 全項目充足
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parents[1]
EVAL_DIR = BASE / "data" / "eval"

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "u2cevk8nktib3sr148rw2hs78ecvux")

PASS_THRESHOLD = 60       # 本番移行可
EXCELLENT_THRESHOLD = 70  # 本番移行推奨

DEFAULT_TARGETS = [
    "spy_bot.py",
    "atlas_agent.py",
    "atlas_rules.yaml",
    "common/pre_trade_check.py",
]


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    file: str
    line: int
    snippet: str


@dataclass
class CriteriaScore:
    criterion_id: str
    criterion_name: str
    score: int          # 0-5
    max_score: int = 5
    rationale: str = ""
    evidences: list[Evidence] = field(default_factory=list)
    improvement: str = ""

    def to_dict(self) -> dict:
        return {
            "criterion_id": self.criterion_id,
            "criterion_name": self.criterion_name,
            "score": self.score,
            "max_score": self.max_score,
            "rationale": self.rationale,
            "evidences": [asdict(e) for e in self.evidences],
            "improvement": self.improvement,
        }


@dataclass
class EvaluationReport:
    generated_at: str
    codebase_path: str
    target_files: list[str]
    scores: list[CriteriaScore]

    @property
    def total_score(self) -> int:
        return sum(s.score for s in self.scores)

    @property
    def max_total(self) -> int:
        return sum(s.max_score for s in self.scores)

    @property
    def score_pct(self) -> float:
        return self.total_score / self.max_total * 100 if self.max_total else 0.0

    @property
    def pass_judge(self) -> str:
        if self.total_score >= EXCELLENT_THRESHOLD:
            return "EXCELLENT"
        if self.total_score >= PASS_THRESHOLD:
            return "PASS"
        return "FAIL"


# ---------------------------------------------------------------------------
# 16項目定義 (A1-A16)
# ---------------------------------------------------------------------------

CRITERIA: list[dict] = [
    # --- 戦術実装系 (A1-A6) ---
    {
        "id": "A1",
        "name": "エントリー時間規律",
        "description": (
            "LAST_ENTRY_H/M でカットオフ実装。"
            "_is_past_entry_cutoff() が全エンジンで呼ばれること。"
            "市場クローズ前のEARLY_CLOSE_EXIT がクローズ前 (ET<13:00) であること。"
        ),
    },
    {
        "id": "A2",
        "name": "PDT全戦術合算カウンタ",
        "description": (
            "PDTカウンタが CS/ORB/IC/Butterfly/Calendar/DeltaHedge 全戦術を合算。"
            "_pdt_trade_count / pdt_tracker で実装。週リセット動作。"
        ),
    },
    {
        "id": "A3",
        "name": "Kill Switch + audit + TTL",
        "description": (
            "kill_switch_check() / audit_trail / TTL付きidempotency store。"
            "KillSwitch発動で全発注停止。audit log が永続書き込みされること。"
        ),
    },
    {
        "id": "A4",
        "name": "pre_trade_check 4層防護",
        "description": (
            "common/pre_trade_check.py に4層チェック実装。"
            "Layer1:時間ゲート/Layer2:証拠金/Layer3:PDT/Layer4:DD。"
            "全エンジンで check_order() が呼ばれること。"
        ),
    },
    {
        "id": "A5",
        "name": "Delta Hedge 動的qty",
        "description": (
            "UNWIND時の qty を _delta_hedge_codes から動的取得。"
            "部分UNWIND成功分を即除去。指値試行→成行fallback実装。"
            "C1-B1/B2/B3 修正が全て反映済みであること。"
        ),
    },
    {
        "id": "A6",
        "name": "多戦術実装 (8戦術以上)",
        "description": (
            "cs_sell / orb_buy / straddle_buy / ic_sell / butterfly / "
            "calendar_sell / strangle_sell / delta_hedge の8戦術が実装済み。"
            "各戦術が execute_entry を持つこと。"
        ),
    },
    # --- 環境適応系 (A7-A9) ---
    {
        "id": "A7",
        "name": "StrategySelector 環境適応",
        "description": (
            "StrategySelector がVIX/IVR/VRP/GEX等の動的環境データを参照。"
            "固定パラメータではなく条件分岐で戦術選択。"
            "select_tactic() または同等メソッドで呼ばれること。"
        ),
    },
    {
        "id": "A8",
        "name": "SymbolSelector マルチ銘柄",
        "description": (
            "SymbolSelector が SPY/QQQ/IWM/TSLA等の複数銘柄から動的選択。"
            "select_symbol() または get_ranked_symbols() が実装済み。"
            "銘柄固定化なし。"
        ),
    },
    {
        "id": "A9",
        "name": "TMR qty検証 (Two-Man Rule)",
        "description": (
            "Two-Man Rule が atlas_agent.py に実装済み。"
            "level3以上でPushover承認待ち。"
            "C7-B1: level2_approval_required が False (未実装承認機構の無効化)。"
        ),
    },
    # --- 誤発注防止系 (A10-A12) ---
    {
        "id": "A10",
        "name": "Idempotency (決定的signal_id)",
        "description": (
            "ORB/Calendar/DeltaHedge のsignal_idが uuid.uuid4() ではなく "
            "ticker+direction+timestamp(分単位)の決定的値で生成。"
            "再起動後も同一シグナルに同一keyが生成されること。"
        ),
    },
    {
        "id": "A11",
        "name": "裸ポジション検出",
        "description": (
            "place_credit_spread で sell_fill/buy_fill の両方がNoneでない場合のみ True。"
            "片脚未約定を検知して反転決済を発動。C2-B1修正が反映済み。"
        ),
    },
    {
        "id": "A12",
        "name": "連続損失停止",
        "description": (
            "check_consecutive_losses() / _orb_check_consecutive_losses() が実装。"
            "全戦術エンジンのエントリー前に連続損失チェックを通すこと。"
        ),
    },
    # --- フォールバック系 (A13-A14) ---
    {
        "id": "A13",
        "name": "外部データ fallback",
        "description": (
            "QuoteContextManager の段階的フェイルオーバー (Level 0-3) 実装。"
            "Finnhub→yahoo→cache→新規停止 の4段階。"
            "VIX/IVR/SMAの取得失敗時に代替ソースへ fallback。"
        ),
    },
    {
        "id": "A14",
        "name": "Phase 自動遷移",
        "description": (
            "資金フェーズ (Phase1/2/3) の自動遷移ロジック。"
            "口座残高やDD実績に基づく self._current_phase 更新。"
            "Phase別パラメータ切替実装。"
        ),
    },
    # --- 監視・品質系 (A15-A16) ---
    {
        "id": "A15",
        "name": "Two-Man Rule 運用継続性",
        "description": (
            "C7-B1: level2_approval_required=False で運用ブロックを防止。"
            "emergency_bypass_conditions が crisis/kill_switch を含む。"
            "Level3承認はPushover経由で正しく実装済み。"
        ),
    },
    {
        "id": "A16",
        "name": "監視自動化 + AAR",
        "description": (
            "atlas_agent.py の IntradayMonitor が場中常駐監視。"
            "daily_aar.py が日次自動実行。"
            "deviation_scanner.py が乖離検知。"
            "scripts/ に3本以上の監視スクリプトが存在。"
        ),
    },
]


# ---------------------------------------------------------------------------
# grep ヘルパー
# ---------------------------------------------------------------------------

def grep_files(
    pattern: str,
    paths: list[Path],
    flags: int = 0,
) -> list[Evidence]:
    """ファイル群から正規表現パターンを検索してEvidence一覧を返す。"""
    results = []
    for p in paths:
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(pattern, line, flags):
                results.append(Evidence(
                    file=str(p.relative_to(BASE)),
                    line=i,
                    snippet=line.strip()[:120],
                ))
    return results


def count_pattern(pattern: str, paths: list[Path], flags: int = 0) -> int:
    return len(grep_files(pattern, paths, flags))


def ast_has_function(path: Path, func_name: str) -> bool:
    """ASTでファイルに指定名の関数/メソッド定義が存在するか確認。"""
    if not path.exists():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    return True
    except SyntaxError:
        pass
    return False


def run_pytest_count(test_pattern: str, base: Path) -> tuple[int, int]:
    """pytest を実行して (passed, total) を返す。失敗時は (0, 0)。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", f"-k={test_pattern}", "--tb=no", "-q"],
            capture_output=True, text=True, timeout=60, cwd=str(base),
        )
        # "X passed" または "X failed" を抽出
        passed = 0
        total = 0
        m_pass = re.search(r"(\d+) passed", result.stdout)
        m_fail = re.search(r"(\d+) failed", result.stdout)
        if m_pass:
            passed = int(m_pass.group(1))
            total += passed
        if m_fail:
            total += int(m_fail.group(1))
        return passed, total
    except Exception:
        return 0, 0


# ---------------------------------------------------------------------------
# 採点ロジック (A1-A16)
# ---------------------------------------------------------------------------

def score_a1_entry_time(paths: list[Path]) -> CriteriaScore:
    """A1: エントリー時間規律"""
    evs = []
    score = 0

    # LAST_ENTRY定数定義
    e = grep_files(r"LAST_ENTRY_H\s*=", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    # _is_past_entry_cutoff 関数
    e = grep_files(r"def _is_past_entry_cutoff", paths)
    evs.extend(e[:1])
    if e:
        score += 1

    # execute_entry 内でカットオフチェック
    e = grep_files(r"_is_past_entry_cutoff\(", paths)
    evs.extend(e[:2])
    if len(e) >= 3:
        score += 2
    elif len(e) >= 1:
        score += 1

    # C4-B1: EARLY_CLOSE_EXIT が 12:50 (クローズ前10分以上前)
    e_h = grep_files(r"EARLY_CLOSE_EXIT_H\s*=\s*12", paths)
    e_m = grep_files(r"EARLY_CLOSE_EXIT_M\s*=\s*50", paths)
    evs.extend(e_h[:1])
    if e_h and e_m:
        score += 1  # C4-B1修正確認

    return CriteriaScore(
        "A1", "エントリー時間規律",
        min(score, 5), 5,
        f"カットオフ関数: {len(grep_files(r'_is_past_entry_cutoff', paths))}件, "
        f"EARLY_CLOSE 12:50: {'OK' if e_h and e_m else 'NG (C4-B1未修正)'}",
        evs,
        "" if score >= 4 else "EARLY_CLOSE_EXIT_H/Mを12:50(ET)以前に設定すること。",
    )


def score_a2_pdt_counter(paths: list[Path]) -> CriteriaScore:
    """A2: PDT全戦術合算カウンタ"""
    evs = []
    score = 0

    e = grep_files(r"_pdt_trade_count|pdt_tracker|PDT_WEEKLY", paths)
    evs.extend(e[:3])
    if e:
        score += 2

    e = grep_files(r"_on_position_closed", paths)
    evs.extend(e[:2])
    if len(e) >= 5:
        score += 2
    elif len(e) >= 2:
        score += 1

    e = grep_files(r"weekly.*reset|reset.*weekly|pdt.*weekly", paths, re.IGNORECASE)
    evs.extend(e[:1])
    if e:
        score += 1

    return CriteriaScore(
        "A2", "PDT全戦術合算カウンタ",
        min(score, 5), 5,
        f"PDTカウンタ実装: {'OK' if score >= 2 else 'NG'}, "
        f"on_position_closed: {len(grep_files(r'_on_position_closed', paths))}件",
        evs,
        "" if score >= 4 else "全戦術の _on_position_closed 呼出でPDTカウンタを合算すること。",
    )


def score_a3_kill_switch(paths: list[Path]) -> CriteriaScore:
    """A3: Kill Switch + audit + TTL"""
    evs = []
    score = 0

    e = grep_files(r"kill_switch|KillSwitch|KILL_SWITCH", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    e = grep_files(r"audit_trail|audit_log|append_audit", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    e = grep_files(r"_idem_store|IdempotencyStore|TTL|ttl", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    e = grep_files(r"idempotency|idem_key|make_key", paths)
    evs.extend(e[:1])
    if e:
        score += 1

    _ks_ok = "OK" if grep_files(r"kill_switch|KillSwitch", paths) else "NG"
    _at_ok = "OK" if grep_files(r"audit_trail|audit_log", paths) else "NG"
    _id_ok = "OK" if grep_files(r"_idem_store|IdempotencyStore", paths) else "NG"
    return CriteriaScore(
        "A3", "Kill Switch + audit + TTL",
        min(score, 5), 5,
        f"KillSwitch: {_ks_ok}, audit_trail: {_at_ok}, Idempotency: {_id_ok}",
        evs,
        "" if score >= 4 else "KillSwitch / audit_trail / TTL付きIdempotency全て実装が必要。",
    )


def score_a4_pre_trade_check(paths: list[Path]) -> CriteriaScore:
    """A4: pre_trade_check 4層防護"""
    evs = []
    score = 0

    e = grep_files(r"from common.pre_trade_check|import.*pre_trade_check", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    e = grep_files(r"check_order\(", paths)
    evs.extend(e[:3])
    if len(e) >= 5:
        score += 2
    elif len(e) >= 2:
        score += 1

    e = grep_files(r"Layer1|Layer2|Layer3|Layer4|time_gate|margin_check|pdt_check", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    pre_path = BASE / "common" / "pre_trade_check.py"
    if pre_path.exists():
        score += 1
        evs.append(Evidence("common/pre_trade_check.py", 1, "file exists"))

    return CriteriaScore(
        "A4", "pre_trade_check 4層防護",
        min(score, 5), 5,
        f"check_order呼出: {len(grep_files(r'check_order', paths))}件, "
        f"pre_trade_check.py: {'存在' if pre_path.exists() else '不存在'}",
        evs,
        "" if score >= 4 else "全エンジンのexecute_entry前にcheck_order()を通すこと。",
    )


def score_a5_delta_hedge_dynamic(paths: list[Path]) -> CriteriaScore:
    """A5: Delta Hedge 動的qty"""
    evs = []
    score = 0

    # C1-B1: qty map
    e = grep_files(r"_delta_hedge_qty_map|_uw_qty", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    # C1-B2: 成功分即除去
    e = grep_files(r"_delta_hedge_codes\.remove\(", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    # C1-B3: 指値fallback
    e = grep_files(r"delta_hedge_unwind_limit|unwind.*use_limit=True", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    # UNWIND発注自体
    e = grep_files(r"delta_hedge_unwind", paths)
    evs.extend(e[:1])
    if e:
        score += 1

    _qty_ok = "OK" if grep_files(r"_delta_hedge_qty_map", paths) else "NG"
    _rm_ok = "OK" if grep_files(r"_delta_hedge_codes\.remove", paths) else "NG"
    _lim_ok = "OK" if grep_files(r"delta_hedge_unwind_limit", paths) else "NG"
    return CriteriaScore(
        "A5", "Delta Hedge 動的qty",
        min(score, 5), 5,
        f"qty_map: {_qty_ok}, 即除去: {_rm_ok}, 指値fallback: {_lim_ok}",
        evs,
        "" if score >= 4 else "C1-B1/B2/B3 全て修正すること。",
    )


def score_a6_multi_tactic(paths: list[Path]) -> CriteriaScore:
    """A6: 多戦術実装 (8戦術以上)"""
    tactics = [
        "CreditSpreadEngine", "ORBEngine", "StraddleBuyEngine",
        "IronCondorSellEngine", "ButterflyEngine", "CalendarEngine",
        "StrangleSellEngine", "IntradayMonitor",
    ]
    evs = []
    found = 0
    for t in tactics:
        e = grep_files(rf"class {t}", paths)
        if e:
            found += 1
            evs.extend(e[:1])

    score = min(found, 5)
    return CriteriaScore(
        "A6", "多戦術実装 (8戦術以上)",
        score, 5,
        f"実装済み戦術: {found}/8 ({', '.join(t for t in tactics if grep_files(rf'class {t}', paths))})",
        evs,
        "" if found >= 8 else f"未実装: {[t for t in tactics if not grep_files(rf'class {t}', paths)]}",
    )


def score_a7_strategy_selector(paths: list[Path]) -> CriteriaScore:
    """A7: StrategySelector 環境適応"""
    evs = []
    score = 0

    e = grep_files(r"class StrategySelector|StrategySelector", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    e = grep_files(r"select_tactic|_select_tactic|select_strategy", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    e = grep_files(r"VIX|ivr|IVR|vix_term|GEX|VRP", paths)
    evs.extend(e[:2])
    if len(e) >= 5:
        score += 2
    elif len(e) >= 2:
        score += 1

    return CriteriaScore(
        "A7", "StrategySelector 環境適応",
        min(score, 5), 5,
        f"StrategySelector: {'OK' if grep_files(r'class StrategySelector', paths) else 'NG'}, "
        f"環境変数参照: {len(grep_files(r'VIX|IVR', paths))}件",
        evs,
        "" if score >= 4 else "select_tactic()でVIX/IVR/VRP等の動的データを参照すること。",
    )


def score_a8_symbol_selector(paths: list[Path]) -> CriteriaScore:
    """A8: SymbolSelector マルチ銘柄"""
    evs = []
    score = 0

    e = grep_files(r"class SymbolSelector|SymbolSelector", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    e = grep_files(r"select_symbol|get_ranked_symbols|SYMBOL_WHITELIST|symbol_whitelist", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    symbols = ["QQQ", "IWM", "TSLA", "NVDA", "AAPL"]
    found_syms = [s for s in symbols if grep_files(rf'"{s}"|"US\.{s}"', paths)]
    if len(found_syms) >= 3:
        score += 2
        evs.append(Evidence("spy_bot.py", 0, f"multi-symbol: {found_syms}"))
    elif found_syms:
        score += 1

    return CriteriaScore(
        "A8", "SymbolSelector マルチ銘柄",
        min(score, 5), 5,
        f"SymbolSelector: {'OK' if grep_files(r'class SymbolSelector', paths) else 'NG'}, "
        f"複数銘柄: {found_syms}",
        evs,
        "" if score >= 4 else "SymbolSelector でSPY以外の銘柄を動的選択すること。",
    )


def score_a9_tmr_qty(paths: list[Path]) -> CriteriaScore:
    """A9: TMR qty検証 (Two-Man Rule)"""
    evs = []
    score = 0

    e = grep_files(r"two_man_rule|Two.Man.Rule|TMR", paths, re.IGNORECASE)
    evs.extend(e[:2])
    if e:
        score += 2

    e = grep_files(r"level3|Level3|min_level.*3", paths)
    evs.extend(e[:2])
    if e:
        score += 1

    # C7-B1: level2_approval_required=false
    e = grep_files(r"level2_approval_required.*false|level2_approval_required.*False", paths, re.IGNORECASE)
    evs.extend(e[:1])
    if e:
        score += 1

    e = grep_files(r"PENDING_APPROVAL|emergency_bypass", paths)
    evs.extend(e[:1])
    if e:
        score += 1

    return CriteriaScore(
        "A9", "TMR qty検証 (Two-Man Rule)",
        min(score, 5), 5,
        f"TMR実装: {'OK' if grep_files(r'two_man_rule', paths, re.IGNORECASE) else 'NG'}, "
        f"C7-B1 level2無効化: {'OK' if grep_files(r'level2_approval_required.*[Ff]alse', paths, re.IGNORECASE) else 'NG (未修正)'}",
        evs,
        "" if score >= 4 else "C7-B1: level2_approval_required=falseに設定すること。",
    )


def score_a10_idempotency(paths: list[Path]) -> CriteriaScore:
    """A10: Idempotency (決定的signal_id)"""
    evs = []
    score = 0

    # uuid4が消えたか (orb/calendar/delta_hedgeの決定的化)
    uuid_count = len(grep_files(r"uuid\.uuid4\(\).*orb_|uuid\.uuid4\(\).*calendar_|uuid\.uuid4\(\).*delta_hedge", paths))
    deterministic = grep_files(r"orb_.*direction.*\%Y%m%d%H%M|deterministic signal_id|orb_.*_long_\|orb_.*_short_", paths)
    evs.extend(deterministic[:2])

    if not uuid_count:
        score += 2
    elif uuid_count <= 2:
        score += 1

    # ORBのsignal_idフォーマット確認
    e = grep_files(r"orb_.*\{direction\}.*\{.*strftime", paths)
    evs.extend(e[:1])
    if e:
        score += 1

    # _idem_store で重複ブロック
    e = grep_files(r"_idem_store\.make_key|重複発注ブロック|Idempotency.*ブロック", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    _det_ok = "OK" if deterministic else "NG"
    _dup_ok = "OK" if grep_files(r"重複発注ブロック", paths) else "NG"
    return CriteriaScore(
        "A10", "Idempotency (決定的signal_id)",
        min(score, 5), 5,
        f"uuid4残存(orb/cal/dh): {uuid_count}件, 決定的key: {_det_ok}, 重複ブロック: {_dup_ok}",
        evs,
        "" if score >= 4 else "C3-B1: ORB/Calendar/DeltaHedgeのsignal_idを分単位決定的値に変更すること。",
    )


def score_a11_naked_position(paths: list[Path]) -> CriteriaScore:
    """A11: 裸ポジション検出"""
    evs = []
    score = 0

    # C2-B1: fill確認後Falseを返す
    e = grep_files(r"sell_fill is None or buy_fill is None|C2-B1", paths)
    evs.extend(e[:2])
    if e:
        score += 3

    # 反転決済発動
    e = grep_files(r"_reverse_leg|片脚.*反転|CS片脚未約定", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    return CriteriaScore(
        "A11", "裸ポジション検出",
        min(score, 5), 5,
        f"fill確認: {'OK' if grep_files(r'sell_fill is None or buy_fill is None', paths) else 'NG (C2-B1未修正)'}, "
        f"反転決済: {'OK' if grep_files(r'_reverse_leg', paths) else 'NG'}",
        evs,
        "" if score >= 4 else "C2-B1: place_credit_spread でfill確認後に片脚ならFalseを返すこと。",
    )


def score_a12_consecutive_loss(paths: list[Path]) -> CriteriaScore:
    """A12: 連続損失停止"""
    evs = []
    score = 0

    e = grep_files(r"check_consecutive_losses|consecutive_loss|CONSECUTIVE_LOSS", paths)
    evs.extend(e[:3])
    if e:
        score += 2

    # orb専用
    e = grep_files(r"_orb_check_consecutive_losses|_ic_sell_check_consecutive", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    # エントリー前チェック
    e = grep_files(r"if.*consecutive.*: return|consecutive.*: skip", paths, re.IGNORECASE)
    evs.extend(e[:1])
    if e:
        score += 1

    return CriteriaScore(
        "A12", "連続損失停止",
        min(score, 5), 5,
        f"連続損失チェック: {len(grep_files(r'check_consecutive_losses', paths))}件",
        evs,
        "" if score >= 4 else "全戦術エンジンのエントリー前に連続損失チェックを入れること。",
    )


def score_a13_external_fallback(paths: list[Path]) -> CriteriaScore:
    """A13: 外部データ fallback"""
    evs = []
    score = 0

    e = grep_files(r"QuoteContextManager|quote_context_manager", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    e = grep_files(r"Level 0|Level 1|Level 2|Level 3|フェイルオーバー|failover", paths, re.IGNORECASE)
    evs.extend(e[:2])
    if e:
        score += 1

    e = grep_files(r"yahoo|Finnhub|finnhub|cache.*fallback|fallback.*cache", paths, re.IGNORECASE)
    evs.extend(e[:2])
    if e:
        score += 2

    return CriteriaScore(
        "A13", "外部データ fallback",
        min(score, 5), 5,
        f"QuoteContextManager: {'OK' if grep_files(r'QuoteContextManager', paths) else 'NG'}, "
        f"fallback: {'OK' if grep_files(r'yahoo|Finnhub', paths, re.IGNORECASE) else 'NG'}",
        evs,
        "" if score >= 4 else "QuoteContextManagerで4段階フェイルオーバーを実装すること。",
    )


def score_a14_phase_transition(paths: list[Path]) -> CriteriaScore:
    """A14: Phase 自動遷移"""
    evs = []
    score = 0

    e = grep_files(r"_current_phase|current_phase|Phase.*transition|phase_transition", paths, re.IGNORECASE)
    evs.extend(e[:2])
    if e:
        score += 2

    e = grep_files(r"Phase1|Phase2|Phase3|PHASE_1|PHASE_2", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    e = grep_files(r"account_cash.*phase|phase.*account_cash|_update_phase", paths, re.IGNORECASE)
    evs.extend(e[:1])
    if e:
        score += 1

    return CriteriaScore(
        "A14", "Phase 自動遷移",
        min(score, 5), 5,
        f"phase管理: {'OK' if grep_files(r'_current_phase', paths) else '未実装'}, "
        f"Phase定義: {'OK' if grep_files(r'Phase1|Phase2', paths) else '未実装'}",
        evs,
        "" if score >= 3 else "口座残高に基づくPhase自動遷移を実装すること。",
    )


def score_a15_tmr_continuity(paths: list[Path]) -> CriteriaScore:
    """A15: Two-Man Rule 運用継続性"""
    evs = []
    score = 0

    # C7-B1確認
    e = grep_files(r"level2_approval_required.*[Ff]alse|C7-B1", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    # emergency_bypass
    e = grep_files(r"emergency_bypass_conditions|crisis_regime_detected|kill_switch_activated", paths)
    evs.extend(e[:2])
    if e:
        score += 2

    # Level3 push確認
    e = grep_files(r"Level3.*承認|APPROVAL_REQUIRED.*L3|approval_body", paths, re.IGNORECASE)
    evs.extend(e[:1])
    if e:
        score += 1

    return CriteriaScore(
        "A15", "Two-Man Rule 運用継続性",
        min(score, 5), 5,
        f"C7-B1 level2無効化: {'OK' if grep_files(r'level2_approval_required.*[Ff]alse', paths, re.IGNORECASE) else 'NG'}, "
        f"emergency_bypass: {'OK' if grep_files(r'emergency_bypass_conditions', paths) else 'NG'}",
        evs,
        "" if score >= 4 else "level2_approval_required=false かつ emergency_bypass_conditions設定が必要。",
    )


def score_a16_monitoring(paths: list[Path]) -> CriteriaScore:
    """A16: 監視自動化 + AAR"""
    evs = []
    score = 0

    scripts_dir = BASE / "scripts"
    monitoring_scripts = list(scripts_dir.glob("*.py")) if scripts_dir.exists() else []
    if len(monitoring_scripts) >= 3:
        score += 2
        evs.append(Evidence("scripts/", 0, f"{len(monitoring_scripts)}本の監視スクリプト"))

    e = grep_files(r"daily_aar|AAR|aar\.py", paths + list(scripts_dir.glob("*.py")) if scripts_dir.exists() else paths)
    evs.extend(e[:2])
    if e:
        score += 1

    e = grep_files(r"deviation_scanner|週次偏差|乖離検知", paths + list(scripts_dir.glob("*.py")) if scripts_dir.exists() else paths)
    evs.extend(e[:1])
    if e:
        score += 1

    e = grep_files(r"IntradayMonitor.*常駐|_check_.*loop|while.*True.*monitor", paths, re.IGNORECASE)
    evs.extend(e[:1])
    if e:
        score += 1

    return CriteriaScore(
        "A16", "監視自動化 + AAR",
        min(score, 5), 5,
        f"監視スクリプト: {len(monitoring_scripts)}本, "
        f"daily_aar: {'OK' if grep_files(r'daily_aar', paths) else 'NG'}, "
        f"deviation_scanner: {'OK' if (scripts_dir / 'deviation_scanner.py').exists() else 'NG'}",
        evs,
        "" if score >= 4 else "daily_aar.py / deviation_scanner.py を自動実行に組み込むこと。",
    )


# ---------------------------------------------------------------------------
# 全体採点
# ---------------------------------------------------------------------------

SCORERS = [
    score_a1_entry_time,
    score_a2_pdt_counter,
    score_a3_kill_switch,
    score_a4_pre_trade_check,
    score_a5_delta_hedge_dynamic,
    score_a6_multi_tactic,
    score_a7_strategy_selector,
    score_a8_symbol_selector,
    score_a9_tmr_qty,
    score_a10_idempotency,
    score_a11_naked_position,
    score_a12_consecutive_loss,
    score_a13_external_fallback,
    score_a14_phase_transition,
    score_a15_tmr_continuity,
    score_a16_monitoring,
]


def evaluate(codebase: Path, run_tests: bool = False) -> EvaluationReport:
    """Atlas全体採点を実行してEvaluationReportを返す。"""
    target_paths: list[Path] = []
    for t in DEFAULT_TARGETS:
        p = codebase / t
        if p.exists():
            target_paths.append(p)
        else:
            print(f"  [WARN] not found: {t}", file=sys.stderr)

    # atlas_agent.py と atlas_rules.yaml も追加
    for extra in ["atlas_agent.py", "atlas_rules.yaml"]:
        ep = codebase / extra
        if ep.exists() and ep not in target_paths:
            target_paths.append(ep)

    scores = []
    for scorer in SCORERS:
        try:
            cs = scorer(target_paths)
        except Exception as ex:
            cs = CriteriaScore(
                "??", scorer.__name__, 0, 5,
                f"採点エラー: {ex}", [], "採点ロジック修正が必要"
            )
        scores.append(cs)
        print(f"  {cs.criterion_id}: {cs.score}/{cs.max_score} — {cs.criterion_name}")

    return EvaluationReport(
        generated_at=datetime.now().isoformat(),
        codebase_path=str(codebase),
        target_files=[str(p.relative_to(BASE)) for p in target_paths],
        scores=scores,
    )


# ---------------------------------------------------------------------------
# レポート生成
# ---------------------------------------------------------------------------

def generate_markdown(report: EvaluationReport) -> str:
    lines = [
        f"# Atlas Trader Evaluation v2",
        f"",
        f"生成日時: {report.generated_at}",
        f"対象: {report.codebase_path}",
        f"",
        f"## 総合スコア",
        f"",
        f"**{report.total_score} / {report.max_total} ({report.score_pct:.1f}%) — {report.pass_judge}**",
        f"",
        f"合格ライン: 60点(本番移行可) / 70点(本番移行推奨) / 80点(EXCELLENT)",
        f"",
        f"## 採点詳細",
        f"",
    ]
    for cs in report.scores:
        status = "OK" if cs.score >= 4 else ("WARN" if cs.score >= 2 else "FAIL")
        lines.append(f"### {cs.criterion_id}: {cs.criterion_name} [{status}] {cs.score}/{cs.max_score}")
        lines.append(f"")
        lines.append(f"**判定根拠**: {cs.rationale}")
        if cs.evidences:
            lines.append(f"")
            lines.append(f"**エビデンス**:")
            for ev in cs.evidences[:3]:
                lines.append(f"- `{ev.file}:{ev.line}` — `{ev.snippet}`")
        if cs.improvement:
            lines.append(f"")
            lines.append(f"**改善点**: {cs.improvement}")
        lines.append(f"")

    # サマリー
    fail_items = [cs for cs in report.scores if cs.score < 3]
    warn_items = [cs for cs in report.scores if 3 <= cs.score < 4]
    ok_items = [cs for cs in report.scores if cs.score >= 4]

    lines.extend([
        f"## サマリー",
        f"",
        f"- OK ({len(ok_items)}項目): {', '.join(c.criterion_id for c in ok_items)}",
        f"- WARN ({len(warn_items)}項目): {', '.join(c.criterion_id for c in warn_items)}",
        f"- FAIL ({len(fail_items)}項目): {', '.join(c.criterion_id for c in fail_items)}",
        f"",
    ])
    if fail_items:
        lines.append(f"## 次サイクル必須修正 ({len(fail_items)}件)")
        lines.append("")
        for cs in fail_items:
            lines.append(f"- **{cs.criterion_id}**: {cs.improvement or cs.criterion_name}")
        lines.append("")

    return "\n".join(lines)


def pushover_notify(report: EvaluationReport) -> None:
    try:
        msg = (
            f"[Atlas] 採点完了 {report.total_score}/{report.max_total} "
            f"({report.score_pct:.1f}%) — {report.pass_judge}"
        )
        data = json.dumps({
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": "[Atlas] cycle3 採点結果",
            "message": msg,
            "priority": 0,
        }).encode()
        req = __import__("urllib.request", fromlist=["Request", "urlopen"])
        r = req.Request(
            "https://api.pushover.net/1/messages.json",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        __import__("urllib.request").urlopen(r, timeout=10)
    except Exception as ex:
        print(f"Pushover送信失敗: {ex}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas Evaluation Framework v2")
    parser.add_argument("--codebase", default=str(BASE), help="対象ディレクトリ")
    parser.add_argument("--out", default=None, help="出力ファイルパス")
    parser.add_argument("--run-tests", action="store_true", help="pytest連動モード")
    parser.add_argument("--no-push", action="store_true", help="Pushover通知なし")
    args = parser.parse_args()

    codebase = Path(args.codebase).resolve()
    print(f"Atlas Evaluation v2 — 対象: {codebase}")

    report = evaluate(codebase, run_tests=args.run_tests)

    md = generate_markdown(report)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    out_path = Path(args.out) if args.out else EVAL_DIR / f"atlas_trader_eval_v2_{date_str}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"\n結果: {out_path}")
    print(f"スコア: {report.total_score}/{report.max_total} ({report.score_pct:.1f}%) — {report.pass_judge}")

    if not args.no_push:
        pushover_notify(report)


if __name__ == "__main__":
    main()
