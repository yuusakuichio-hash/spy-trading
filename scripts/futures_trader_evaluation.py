#!/usr/bin/env python3
"""Futures Trader Evaluation Framework -- 優秀先物MESトレーダー判定スクリプト

16項目 F1-F16 で Chronos / MFFU先物Bot の実装を静的解析で採点する。
Atlas版 scripts/trader_evaluation.py の設計思想を先物向けに踏襲。

Atlas版との違い:
  - 入力: トレードPnLログ（動的）ではなく、コードベース（静的）
  - 評価: 「優秀MESトレーダーが必ず持つ16要素」の実装充足度
  - スコア: 各項目 0-5 点、合計 80 点満点

使い方:
  python3 scripts/futures_trader_evaluation.py                       # Chronos 全体採点
  python3 scripts/futures_trader_evaluation.py --out <file.md>       # 出力ファイル指定
  python3 scripts/futures_trader_evaluation.py --codebase <dir>      # 対象ディレクトリ指定

出力:
  data/eval/chronos_trader_eval_YYYYMMDD.md

合格ライン:
  60点以上 (75%) — 基本稼働可・公募ファンド運用水準
  70点以上 (87.5%) — MFFU Sim-Funded安定・私募ファンド検討可
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib import parse, request

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parents[1]
EVAL_DIR = BASE / "data" / "eval"

# Pushover
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "u2cevk8nktib3sr148rw2hs78ecvux")

# 合格ライン
PASS_THRESHOLD = 60   # 基本稼働可
EXCELLENT_THRESHOLD = 70  # MFFU Sim-Funded安定

# 対象ファイル
DEFAULT_TARGETS = [
    "chronos_bot.py",
    "chronos_rules.yaml",
    "chronos_mffu_rules.py",
    "chronos_strategy_selector.py",
    "chronos_pre_trade_check.py",
    "chronos_symbol_meta.py",
    "tradovate_client.py",
    "futures_vix_mr.py",
    "futures_level_trading.py",
    "futures_session_strategy.py",
    "futures_time_of_day_bias.py",
    "futures_asia_range_fade.py",
    "futures_gap_fill_advanced.py",
    "futures_trend_follow.py",
    "chronos_cumulative_delta.py",   # F12: Cumulative Delta
    "chronos_liquidity_sweep.py",    # F13: Liquidity Sweep
]


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """grep などで見つけた「実装の証拠」。"""
    file: str
    line: int
    snippet: str


@dataclass
class CriteriaScore:
    """1項目の採点結果。"""
    criterion_id: str
    criterion_name: str
    score: int              # 0-5
    max_score: int = 5
    rationale: str = ""
    evidences: list[Evidence] = field(default_factory=list)
    improvement: str = ""   # 改善提案

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
    """全体採点結果。"""
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
# 16項目定義（research_mes_trader_day_20260419.md Section 5 および 6 より）
# ---------------------------------------------------------------------------

CRITERIA: list[dict] = [
    # --- 戦術実装系 (F1-F4) ---
    {
        "id": "F1",
        "name": "ORBセットアップ規律",
        "description": (
            "Opening Range判定の精度・false break検知・ATR閾値。"
            "Toby Crabel / Linda Raschke の5分ORBをVIX>=20帯のみ適用。"
            "stop=OR反対端・RR動的。"
        ),
    },
    {
        "id": "F2",
        "name": "VIX-MR タイミング",
        "description": (
            "VIX終値Zスコア(20日SMA/SD)による15:40-15:55 ET窓の"
            "overnight Longエントリー。Z>=1.5で発動・5日保有上限。"
        ),
    },
    {
        "id": "F3",
        "name": "Max Loss遵守",
        "description": (
            "MFFU各Phase（Eval $2000・初回Payout後 $100）での損失ガード"
            "(Safety Buffer・Trailing DD)動作。Intraday予防halt含む。"
        ),
    },
    {
        "id": "F4",
        "name": "Consistency管理",
        "description": (
            "Evaluation 50%（予防35%）/ Sim-Funded 無し のフェーズ切替動作。"
            "is_consistency_applicable() のPhase分岐実装。"
        ),
    },
    # --- ガード系 (F5-F8) ---
    {
        "id": "F5",
        "name": "News Window回避",
        "description": (
            "T1指標（FOMC/CPI/NFP/PPI/ISM等）±2分の発注停止。"
            "economic_calendar連携・MFFU公式プロトコル準拠。"
        ),
    },
    {
        "id": "F6",
        "name": "Globex Maintenance Break",
        "description": (
            "CME 17:00-18:00 ET完全停止。is_maintenance_break() 実装・"
            "block_new_orders・pre_break_buffer含む。"
        ),
    },
    {
        "id": "F7",
        "name": "Hedging禁止遵守",
        "description": (
            "同一商品MES/ES両建て検知・リジェクト。"
            "check_hedging_violation()・同一プロダクトペアテーブル。"
        ),
    },
    {
        "id": "F8",
        "name": "連敗制御",
        "description": (
            "2連敗サイズ50%・3連敗サイズ25%・5連敗当日停止。"
            "daily_reset連動・record_trade_result()更新。"
        ),
    },
    # --- 環境適応系 (F9-F13) ---
    {
        "id": "F9",
        "name": "セッション認識",
        "description": (
            "Asia/EU/US/Power Hour/Lunch Lullの切替。"
            "session_strategy.py・時間帯別戦術選択。"
        ),
    },
    {
        "id": "F10",
        "name": "ATR Regime適応",
        "description": (
            "日足ATR(14)のP33/P67でlow/mid/high分類・"
            "high_vol/low_volでサイズ調整・TP/SL比例幅。"
        ),
    },
    {
        "id": "F11",
        "name": "VWAP使用",
        "description": (
            "RTH起点VWAP・Reclaim / Rejection判定。"
            "Anchored VWAP(前日高安/発表時点)含むと満点。"
        ),
    },
    {
        "id": "F12",
        "name": "Cumulative Delta",
        "description": (
            "出来高方向性(買い約定-売り約定)の活用。"
            "Footprint/DOM bid-ask比率でプロキシ可。"
        ),
    },
    {
        "id": "F13",
        "name": "Liquidity Sweep認識",
        "description": (
            "Stop Hunt Reversalの検知。前日高安・前週高安・IB端の"
            "sweep後の逆張りエントリー。"
        ),
    },
    # --- インフラ・規律系 (F14-F16) ---
    {
        "id": "F14",
        "name": "Phase認識",
        "description": (
            "demo/evaluation/sim_funded_pre/sim_funded_after_payoutの切替。"
            "on_payout_received・account_type分岐・survival_mode。"
        ),
    },
    {
        "id": "F15",
        "name": "Rate-limit処理",
        "description": (
            "Tradovate 429応答の指数バックオフ。"
            "p-ticket/p-captcha対応・連続停止ガード。"
        ),
    },
    {
        "id": "F16",
        "name": "Risk-per-trade サイジング",
        "description": (
            "口座サイズの0.2-0.5%に制限。Kelly+Consistency-aware cap・"
            "OR幅比例・point_valueベース。"
        ),
    },
]


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------

def send_pushover(title: str, message: str, priority: int = 0) -> bool:
    data = parse.urlencode({
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message[:1020],
        "priority": priority,
    }).encode()
    try:
        req = request.Request("https://api.pushover.net/1/messages.json", data=data)
        with request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[pushover error] {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Grep / 検索ユーティリティ
# ---------------------------------------------------------------------------

def grep_codebase(pattern: str, files: list[Path], max_results: int = 10) -> list[Evidence]:
    """複数ファイルから regex にマッチする行を Evidence として返す。"""
    results: list[Evidence] = []
    regex = re.compile(pattern)
    for f in files:
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                snippet = line.strip()
                if len(snippet) > 160:
                    snippet = snippet[:160] + "..."
                results.append(Evidence(file=f.name, line=i, snippet=snippet))
                if len(results) >= max_results:
                    return results
    return results


def has_yaml_key(yaml_path: Path, key_path: list[str]) -> Optional[str]:
    """yaml に key_path (ネストキー) が存在するか確認。存在すれば該当行を返す。"""
    if not yaml_path.exists():
        return None
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except Exception:
        return None
    # 段階マッチ: 最上位キー -> サブキー ... の順に文字列で確認
    cur_text = text
    for key in key_path:
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*:", re.MULTILINE)
        m = pattern.search(cur_text)
        if not m:
            return None
        # このキーの直後からインデント内のブロックに限定
        cur_text = cur_text[m.end():]
    # 見つかったブロックの最初の160文字を返す
    return cur_text.split("\n")[0].strip()[:160] if cur_text else ""


# ---------------------------------------------------------------------------
# 個別項目採点ロジック
# ---------------------------------------------------------------------------

def _score_f1_orb(files: dict[str, Path]) -> CriteriaScore:
    """F1: ORBセットアップ規律"""
    evidences = []
    score = 0

    # 1. ORBクラス存在
    orb_class = grep_codebase(
        r"class\s+FuturesORBStrategy|class\s+ORBStrategy",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if orb_class:
        score += 1
        evidences.extend(orb_class[:1])

    # 2. update_or_candle / finalize_or（5分OR形成ロジック）
    or_logic = grep_codebase(
        r"def\s+(update_or_candle|finalize_or|check_breakout)",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if len(or_logic) >= 3:
        score += 1
        evidences.extend(or_logic[:3])

    # 3. VIX帯フィルタ (>=20 / panic 2/3 / mid 50%)
    vix_filter = grep_codebase(
        r"orb_vix_min|vix_band.*orb|ORB.*VIX|vix.*35\.0",
        [files["chronos_rules.yaml"], files["chronos_strategy_selector.py"],
         files["chronos_bot.py"]],
        max_results=5,
    )
    if vix_filter:
        score += 1
        evidences.extend(vix_filter[:2])

    # 4. stop=OR反対端・RR動的
    stop_logic = grep_codebase(
        r"ORB_STOP_ATR_MULT|ORB_TARGET_ATR_MULT|orb_sl_ratio|orb_tp_ratio",
        [files["chronos_bot.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if stop_logic:
        score += 1
        evidences.extend(stop_logic[:2])

    # 5. False break検知（出来高フィルタ or 2-bar confirmation）
    false_break = grep_codebase(
        r"volume_min|false_break|confirmation|retest|1\.3.*volume|volume.*ratio",
        [files["chronos_bot.py"], files["chronos_strategy_selector.py"],
         files["chronos_rules.yaml"]],
        max_results=3,
    )
    if false_break:
        score += 1
        evidences.extend(false_break[:1])

    rationale_parts = []
    if score >= 4:
        rationale_parts.append(
            f"FuturesORBStrategy実装済み・5分OR確定・VIX>=20フィルタ・RR動的"
            f"(STOP=OR×1.0, TP=OR×2.0)。"
        )
    elif score >= 3:
        rationale_parts.append(
            f"基本ORB実装済みだがfalse break検知が弱い(volume閾値null)。"
        )
    else:
        rationale_parts.append("ORB実装不完全。")

    improvement = (
        "volume 1.3倍以上フィルタの有効化 (chronos_rules.yaml: entry.env_filters.volume_min)。"
        "false break検知のため retest confirmation を FuturesORBStrategy.check_breakout に追加。"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F1",
        criterion_name="ORBセットアップ規律",
        score=score,
        rationale=" ".join(rationale_parts),
        evidences=evidences,
        improvement=improvement,
    )


def _score_f2_vix_mr(files: dict[str, Path]) -> CriteriaScore:
    """F2: VIX-MR タイミング"""
    evidences = []
    score = 0

    # 1. VIX-MR クラス or 関数
    mr_class = grep_codebase(
        r"class\s+VIXMR|class\s+.*VIX.*Mean|futures_vix_mr",
        [files.get("futures_vix_mr.py", Path("/nonexistent")),
         files["chronos_bot.py"]],
        max_results=3,
    )
    if mr_class:
        score += 1
        evidences.extend(mr_class[:1])

    # 2. Zスコア計算 (20日SMA/SD)
    z_score = grep_codebase(
        r"calc_vix_z_score|vix_z.*1\.5|zscore_min|VIX.*Z",
        [files["chronos_rules.yaml"], files["chronos_strategy_selector.py"],
         files.get("futures_vix_mr.py", Path("/nonexistent"))],
        max_results=3,
    )
    if z_score:
        score += 1
        evidences.extend(z_score[:1])

    # 3. エントリー窓 15:40-15:55 ET
    window = grep_codebase(
        r"15:40|15:55|overnight_entry_window|vix_mr_window",
        [files["chronos_rules.yaml"], files["chronos_strategy_selector.py"]],
        max_results=3,
    )
    if window:
        score += 1
        evidences.extend(window[:1])

    # 4. 5日保有上限・SL 1.5%・TP 1.0%
    exit_rules = grep_codebase(
        r"vix_mr_max_hold_days|vix_mr_sl_pct|vix_mr_tp_pct",
        [files["chronos_rules.yaml"]],
        max_results=3,
    )
    if len(exit_rules) >= 3:
        score += 1
        evidences.extend(exit_rules[:2])

    # 5. Z>=1.5 で panic size 0.5 / high size 1.0 の帯別調整
    size_adjust = grep_codebase(
        r"base_vmr.*panic|panic.*vix_mr|vix_mr.*size.*band",
        [files["chronos_strategy_selector.py"]],
        max_results=3,
    )
    if size_adjust:
        score += 1
        evidences.extend(size_adjust[:1])

    rationale = (
        "VIX-MR完全実装: Z>=1.5で15:40-15:55 ETにentry, 5日hold, VIX帯別size調整(panic 0.5 / high 1.0 / other 0.7)"
        if score >= 4 else
        "VIX-MR基本実装済みだが帯別調整または窓判定に一部欠損あり"
        if score >= 3 else
        "VIX-MR実装不完全"
    )

    improvement = (
        "VIX6M/VIX3M を取得して Term Structure と統合した Z-score を使う(data/research_mes_trader_day_20260419.md §B-4)"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F2",
        criterion_name="VIX-MR タイミング",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f3_max_loss(files: dict[str, Path]) -> CriteriaScore:
    """F3: Max Loss遵守"""
    evidences = []
    score = 0

    # 1. MFFURuleGuard / MFFURules の存在
    rule_guard = grep_codebase(
        r"class\s+MFFURuleGuard|class\s+MFFURules",
        [files["chronos_bot.py"], files["chronos_mffu_rules.py"]],
        max_results=3,
    )
    if rule_guard:
        score += 1
        evidences.extend(rule_guard[:2])

    # 2. Safety Buffer 計算（EOD Trailing DD）
    safety_buffer = grep_codebase(
        r"calc_safety_buffer|check_mffu_safety_buffer|trailing_drawdown",
        [files["chronos_mffu_rules.py"]],
        max_results=3,
    )
    if safety_buffer:
        score += 1
        evidences.extend(safety_buffer[:1])

    # 3. Intraday予防 halt (hypothetical_eod)
    preventive = grep_codebase(
        r"preventive_halt|hypothetical_eod|INTRADAY_STOP_PCT",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if preventive:
        score += 1
        evidences.extend(preventive[:1])

    # 4. 初回Payout後 $100 MLL の効力
    after_payout = grep_codebase(
        r"sim_max_loss_after_payout|effective_mll|after_payout.*100|payout_count.*1",
        [files["chronos_mffu_rules.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if after_payout:
        score += 1
        evidences.extend(after_payout[:1])

    # 5. Daily soft stop ($300) 明示実装
    # 厳格採点: daily_soft_stop セクション or $300 soft cap が yaml/bot にあるかを確認
    soft_stop = grep_codebase(
        r"daily_soft_stop|soft_stop_threshold|loss_threshold_usd",
        [files["chronos_rules.yaml"], files["chronos_bot.py"]],
        max_results=3,
    )
    if soft_stop:
        score += 1
        evidences.extend(soft_stop[:1])

    # 統合halt経路 (_kill_switch_day 等) は上記の安全網にあたり、5点目とは別軸
    rationale = (
        "MFFURuleGuard + MFFURules 両層実装。EOD Trailing DD + Intraday予防halt + "
        "初回Payout後 $100 MLL対応(survival_mode) + 日次loss_floor統合。"
        "ただし Daily soft stop ($300) の明示実装が未。"
        if score == 4 else
        "Max Loss ガード完全実装 (Daily soft stop含む)"
        if score >= 5 else
        "Max Loss ガード実装済みだが Phase 切替 または 予防 halt の一部に欠落"
        if score >= 3 else
        "Max Loss 遵守層 不完全"
    )

    improvement = (
        "Daily soft stop ($300) を chronos_rules.yaml に追加して "
        "当日エントリーブロック化する (research §C-5)。"
        "現在は Intraday STOP_PCT (90%) で予防halt するが、MFFU合格者ジャーナル標準の"
        "$300固定soft stopは別途必要"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F3",
        criterion_name="Max Loss遵守",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f4_consistency(files: dict[str, Path]) -> CriteriaScore:
    """F4: Consistency管理"""
    evidences = []
    score = 0

    # 1. is_consistency_applicable の Phase分岐
    phase_branch = grep_codebase(
        r"is_consistency_applicable|is_consistency_check_enabled",
        [files["chronos_mffu_rules.py"], files["chronos_bot.py"]],
        max_results=3,
    )
    if phase_branch:
        score += 1
        evidences.extend(phase_branch[:1])

    # 2. Evaluation 50% ルール
    eval_50 = grep_codebase(
        r"eval_consistency_max_pct.*0\.50|consistency_max_pct.*0\.50|50%",
        [files["chronos_mffu_rules.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if eval_50:
        score += 1
        evidences.extend(eval_50[:1])

    # 3. 予防 35% soft cap
    safety_35 = grep_codebase(
        r"CONSISTENCY_SAFETY_PCT|consistency_safety|0\.35",
        [files["chronos_strategy_selector.py"]],
        max_results=3,
    )
    if safety_35:
        score += 1
        evidences.extend(safety_35[:1])

    # 4. Sim-Funded でスキップ（phase_rules.consistency_skip_phases）
    sim_skip = grep_codebase(
        r"consistency_skip_phases|mffu_sim_funded|PHASE_SIM_FUNDED",
        [files["chronos_rules.yaml"], files["chronos_mffu_rules.py"],
         files["chronos_bot.py"]],
        max_results=3,
    )
    if sim_skip:
        score += 1
        evidences.extend(sim_skip[:1])

    # 5. daily_pnl_history での 50% 違反検出 + call_site統合
    # 厳格採点: is_consistency_check_enabled の call_site (実際にメインループで呼ばれるか) を確認
    violation = grep_codebase(
        r"check_mffu_consistency|check_consistency.*phase|max_single_day.*total_profit",
        [files["chronos_mffu_rules.py"]],
        max_results=3,
    )
    if violation:
        score += 0.5
        evidences.extend(violation[:1])

    # call_site 統合（chronos_bot.py メインループで is_consistency_check_enabled or check_mffu_consistency が実際に使われているか）
    call_site = grep_codebase(
        r"is_consistency_check_enabled\(\)|check_mffu_consistency\(|check_mffu_compliance\(",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    # 定義位置は除外（def ではない呼び出し）
    call_site = [c for c in call_site if "def " not in c.snippet]
    if call_site:
        score += 0.5
        evidences.extend(call_site[:1])

    score = int(score)  # 小数を整数化（0.5+0.5=1相当で5点到達可）

    rationale = (
        "Consistency管理完全: is_consistency_applicable() Phase分岐 + Eval 50%ルール + "
        "予防35% soft cap + Sim-Fundedスキップ + daily_pnl違反検知 + call_site統合すべて実装"
        if score >= 5 else
        "Consistency管理 概ね実装済みだが call_site統合が弱い: "
        "is_consistency_check_enabled() が定義のみで、メインループで呼ばれていない可能性"
        if score == 4 else
        "Consistency管理実装済みだが一部欠落"
        if score >= 3 else
        "Consistency管理 不完全"
    )

    improvement = (
        "is_consistency_check_enabled() を chronos_bot.py の run_forever() ループ内で "
        "check_mffu_compliance(rules) と組み合わせて毎サイクル評価する。"
        "現在は定義のみで実動作が確認できない。"
        "Sim-Funded フェーズ移行時に automate で rules.yaml 更新するフックも追加推奨"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F4",
        criterion_name="Consistency管理",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f5_news_window(files: dict[str, Path]) -> CriteriaScore:
    """F5: News Window回避"""
    evidences = []
    score = 0

    # 1. NewsTradingFilter クラス
    news_class = grep_codebase(
        r"class\s+NewsTradingFilter",
        [files["chronos_bot.py"]],
        max_results=2,
    )
    if news_class:
        score += 1
        evidences.extend(news_class[:1])

    # 2. blackout_window_sec = 120 (±2分)
    blackout_sec = grep_codebase(
        r"blackout_window_sec.*120|BLACKOUT_MINUTES.*2|NEWS_EVENT_BLACKOUT_MINUTES",
        [files["chronos_rules.yaml"], files["chronos_bot.py"]],
        max_results=3,
    )
    if blackout_sec:
        score += 1
        evidences.extend(blackout_sec[:1])

    # 3. T1 events リスト (FOMC/CPI/NFP 最低3種)
    t1_events = grep_codebase(
        r"FOMC|CPI|NFP|t1_events|MFFU_HIGH_IMPACT_EVENTS",
        [files["chronos_rules.yaml"], files["chronos_bot.py"]],
        max_results=5,
    )
    t1_keywords = set()
    for e in t1_events:
        for kw in ["FOMC", "CPI", "NFP", "PPI", "ISM", "PCE", "GDP"]:
            if kw in e.snippet.upper():
                t1_keywords.add(kw)
    if len(t1_keywords) >= 3:
        score += 1
        evidences.extend(t1_events[:2])

    # 4. _in_news_window / is_blackout 統合（ORB + Level等の経路）
    integration = grep_codebase(
        r"_in_news_window|NewsGuard|news_filter\.is_blackout",
        [files["chronos_bot.py"]],
        max_results=5,
    )
    if len(integration) >= 3:
        score += 1
        evidences.extend(integration[:2])

    # 5. hold_existing_positions: true (既存ポジhold許可)
    hold_existing = grep_codebase(
        r"hold_existing_positions|既存ポジ.*hold|hold.*既存",
        [files["chronos_rules.yaml"], files["chronos_bot.py"]],
        max_results=3,
    )
    if hold_existing:
        score += 1
        evidences.extend(hold_existing[:1])

    rationale = (
        "NewsTradingFilter + 2分窓 + T1 events + ORB/Level経路でのNewsGuard統合・既存ポジhold許可"
        if score >= 4 else
        "News Window実装済みだが統合経路 or T1リストに不足"
        if score >= 3 else
        "News Window 不完全"
    )

    improvement = (
        "econ_calendar.json の自動更新 cron (Finnhub calendar) を追加"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F5",
        criterion_name="News Window回避",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f6_maintenance_break(files: dict[str, Path]) -> CriteriaScore:
    """F6: Globex Maintenance Break"""
    evidences = []
    score = 0

    # 1. _is_maintenance_break 実装
    mb_func = grep_codebase(
        r"def\s+_is_maintenance_break|is_maintenance_break",
        [files["chronos_bot.py"]],
        max_results=2,
    )
    if mb_func:
        score += 2
        evidences.extend(mb_func[:1])

    # 2. 17:00-18:00 ET 設定
    mb_config = grep_codebase(
        r"maintenance_break|17:00.*18:00|start_et.*17:00",
        [files["chronos_rules.yaml"]],
        max_results=3,
    )
    if mb_config:
        score += 1
        evidences.extend(mb_config[:1])

    # 3. block_new_orders / pre_break_buffer
    block_config = grep_codebase(
        r"block_new_orders|pre_break_buffer_minutes",
        [files["chronos_rules.yaml"]],
        max_results=3,
    )
    if block_config:
        score += 1
        evidences.extend(block_config[:1])

    # 4. run_forever 内でのガード経路
    guard_integration = grep_codebase(
        r"Maintenance Break中|_is_maintenance_break\(now_et\)",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if guard_integration:
        score += 1
        evidences.extend(guard_integration[:1])

    rationale = (
        "is_maintenance_break + 17:00-18:00 ET設定 + block_new_orders + "
        "run_forever経路統合すべて完備"
        if score >= 4 else
        "Maintenance Break実装済みだが経路統合に一部欠落"
        if score >= 3 else
        "Maintenance Break 不完全"
    )

    improvement = (
        "Daily Strong Close の15:45 buffer と整合させる。祝日前 early close に対応"
        " (economic_calendar_2026.json の休場日参照)"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F6",
        criterion_name="Globex Maintenance Break",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f7_hedging(files: dict[str, Path]) -> CriteriaScore:
    """F7: Hedging禁止遵守"""
    evidences = []
    score = 0

    # 1. check_hedging_violation 関数
    hedge_func = grep_codebase(
        r"def\s+check_hedging_violation",
        [files["chronos_pre_trade_check.py"]],
        max_results=2,
    )
    if hedge_func:
        score += 2
        evidences.extend(hedge_func[:1])

    # 2. 同一プロダクトペアテーブル (MES/ES, MNQ/NQ)
    pair_table = grep_codebase(
        r"_HEDGE_SAME_PRODUCT_PAIRS|MES.*ES|MNQ.*NQ|hedging_guard",
        [files["chronos_pre_trade_check.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if pair_table:
        score += 1
        evidences.extend(pair_table[:1])

    # 3. Long/Short 逆方向検知ロジック
    direction = grep_codebase(
        r"pos_is_long.*new_is_long|両建て|BUY.*LONG|SELL.*SHORT",
        [files["chronos_pre_trade_check.py"]],
        max_results=3,
    )
    if direction:
        score += 1
        evidences.extend(direction[:1])

    # 4. place_order 直前の呼び出し経路
    call_site = grep_codebase(
        r"check_hedging_violation\(",
        [files["chronos_bot.py"], files["tradovate_client.py"]],
        max_results=3,
    )
    if call_site:
        score += 1
        evidences.extend(call_site[:1])

    rationale = (
        "check_hedging_violation + MES/ES / MNQ/NQ / MYM/YM / M2K/RTY 4ペアのテーブル + "
        "long/short 逆方向ロジック完備。ただし call_site が pre_trade_check に統合されていない場合は発注経路で呼ばれない"
        if score >= 3 else
        "Hedging ガード実装 不完全 (関数定義のみ・発注経路未統合)"
    )

    improvement = (
        "chronos_bot.py または place_order 直前に check_hedging_violation() を必ず呼ぶ。"
        "現状 pre_trade_check.check_order() は NotImplementedError で機能していない"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F7",
        criterion_name="Hedging禁止遵守",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f8_loss_streak(files: dict[str, Path]) -> CriteriaScore:
    """F8: 連敗制御"""
    evidences = []
    score = 0

    # 1. consecutive_loss_guard 設定
    clg = grep_codebase(
        r"consecutive_loss_guard|halt_streak|streak_2_size_pct|streak_3_size_pct",
        [files["chronos_rules.yaml"]],
        max_results=3,
    )
    if clg:
        score += 1
        evidences.extend(clg[:1])

    # 2. _apply_loss_scaling 実装
    apply_scaling = grep_codebase(
        r"def\s+_apply_loss_scaling|_consecutive_losses",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if apply_scaling:
        score += 1
        evidences.extend(apply_scaling[:1])

    # 3. record_trade_result での更新
    record = grep_codebase(
        r"def\s+record_trade_result|_consecutive_losses\s*\+=",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if record:
        score += 1
        evidences.extend(record[:1])

    # 4. 5連敗 kill_switch_day
    kill_switch = grep_codebase(
        r"_kill_switch_day|halt_streak.*5|5連敗",
        [files["chronos_bot.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if kill_switch:
        score += 1
        evidences.extend(kill_switch[:1])

    # 5. 日次リセット
    daily_reset = grep_codebase(
        r"daily_reset_et|_daily_reset.*consecutive|reset.*connsecutive",
        [files["chronos_bot.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if daily_reset:
        score += 1
        evidences.extend(daily_reset[:1])

    rationale = (
        "consecutive_loss_guard完全実装: 2連敗50% / 3連敗25% / 5連敗停止 + "
        "_apply_loss_scaling + record_trade_result + 日次リセット"
        if score >= 4 else
        "連敗制御実装済みだが一部経路欠落"
        if score >= 3 else
        "連敗制御 不完全"
    )

    improvement = (
        "連敗判定の粒度を戦術別に分ける (ORB vs VIX-MR) と精度向上。"
        "現状は全戦術合算のため VIX-MR 連敗で ORB サイズも縮小される"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F8",
        criterion_name="連敗制御",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f9_session(files: dict[str, Path]) -> CriteriaScore:
    """F9: セッション認識"""
    evidences = []
    score = 0

    # 1. futures_session_strategy.py の存在
    session_file = grep_codebase(
        r"class\s+SessionBasedStrategy|def\s+get_current_session",
        [files.get("futures_session_strategy.py", Path("/nonexistent"))],
        max_results=3,
    )
    if session_file:
        score += 1
        evidences.extend(session_file[:1])

    # 2. Asia/London/US_Open/US_Midday/US_Close 5セッション分類
    sessions = grep_codebase(
        r"asia|london|us_open|us_midday|us_close",
        [files.get("futures_session_strategy.py", Path("/nonexistent"))],
        max_results=10,
    )
    found = set()
    for e in sessions:
        for s in ["asia", "london", "us_open", "us_midday", "us_close"]:
            if s in e.snippet.lower():
                found.add(s)
    if len(found) >= 4:
        score += 1
        evidences.extend(sessions[:2])

    # 3. Power Hour / Lunch Lull (Time-of-Day Bias)
    tod = grep_codebase(
        r"power_hour|lunch_lull|calc_tod_bias|Time-of-Day|time_bias_et",
        [files.get("futures_time_of_day_bias.py", Path("/nonexistent")),
         files["chronos_strategy_selector.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if tod:
        score += 1
        evidences.extend(tod[:1])

    # 4. セッション統合: selector でのsession戦術追加
    integration = grep_codebase(
        r"session_from_env|session_based|_sess_engine|session.*strategy",
        [files["chronos_strategy_selector.py"]],
        max_results=3,
    )
    if integration:
        score += 1
        evidences.extend(integration[:1])

    # 5. Asia Range Fade / London breakout など具体戦術
    specific = grep_codebase(
        r"AsiaRangeFade|asia_range_fade|is_asia_session|London.*breakout",
        [files.get("futures_asia_range_fade.py", Path("/nonexistent")),
         files["chronos_strategy_selector.py"]],
        max_results=3,
    )
    if specific:
        score += 1
        evidences.extend(specific[:1])

    rationale = (
        "5セッション分類 + Time-of-Day Bias + selector統合 + Asia Range Fade / London Breakout まで完備"
        if score >= 4 else
        "セッション認識実装済みだが一部経路欠落"
        if score >= 3 else
        "セッション認識 不完全"
    )

    improvement = (
        "Power Hour (14:30-15:30 ET) / Lunch Lull (11:30-13:00 ET) を "
        "明示的戦術スイッチとして実装 (現状は TOD bias の乗数のみ)"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F9",
        criterion_name="セッション認識",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f10_atr_regime(files: dict[str, Path]) -> CriteriaScore:
    """F10: ATR Regime適応"""
    evidences = []
    score = 0

    # 1. atr_regime 設定 yaml
    atr_yaml = grep_codebase(
        r"atr_regime|lookback_days.*60|low_pct.*33|high_pct.*67",
        [files["chronos_rules.yaml"]],
        max_results=3,
    )
    if atr_yaml:
        score += 1
        evidences.extend(atr_yaml[:1])

    # 2. high/low volatility 分類関数
    classify = grep_codebase(
        r"def\s+get_atr_regime|classify.*atr|atr_low.*atr_high|P33|P67",
        [files["chronos_bot.py"], files["chronos_strategy_selector.py"]],
        max_results=3,
    )
    if classify:
        score += 1
        evidences.extend(classify[:1])

    # 3. ATR 現値取得経路
    atr_fetch = grep_codebase(
        r"self\._atr|env\[.atr.\]|atr_5d|atr_20d|calc_atr",
        [files["chronos_bot.py"], files["chronos_strategy_selector.py"],
         files.get("futures_gap_fill_advanced.py", Path("/nonexistent"))],
        max_results=5,
    )
    if atr_fetch:
        score += 1
        evidences.extend(atr_fetch[:2])

    # 4. size_multiplier の atr regime 依存
    size_mult = grep_codebase(
        r"size_multiplier_low|size_multiplier_high|atr.*size.*mult",
        [files["chronos_rules.yaml"], files["chronos_strategy_selector.py"]],
        max_results=3,
    )
    if size_mult:
        score += 1
        evidences.extend(size_mult[:1])

    # 5. TP/SL 幅のATR比例 (OR幅 = ATRプロキシ)
    tp_sl_atr = grep_codebase(
        r"or_range.*MULT|orb_sl_ratio|orb_tp_ratio|ATR_MULT",
        [files["chronos_bot.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if tp_sl_atr:
        score += 1
        evidences.extend(tp_sl_atr[:1])

    rationale = (
        "ATR Regime 完全実装 (yaml + 分類関数 + fetch + size_mult + TP/SL比例)"
        if score >= 4 else
        "ATR Regime 部分実装: yaml設定 + OR幅ベースのTP/SL はあるが、動的分類関数 (get_atr_regime) が未実装"
        if score >= 2 else
        "ATR Regime 不完全"
    )

    improvement = (
        "`def get_atr_regime(atr_14d: float, atr_history_60d: list) -> str` を新規実装し、"
        "strategy_selector で vix_band と同列に size_pct 乗数として適用する。"
        "現状 atr_20d は env_score のプロキシで暫定実装 (chronos_bot.py:2302)"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F10",
        criterion_name="ATR Regime適応",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f11_vwap(files: dict[str, Path]) -> CriteriaScore:
    """F11: VWAP使用"""
    evidences = []
    score = 0

    # 1. calc_vwap 関数
    vwap_func = grep_codebase(
        r"def\s+calc_vwap|def\s+calc_vwap_from_ohlcv",
        [files.get("futures_level_trading.py", Path("/nonexistent"))],
        max_results=3,
    )
    if vwap_func:
        score += 1
        evidences.extend(vwap_func[:1])

    # 2. update_vwap (リアルタイム更新)
    update = grep_codebase(
        r"def\s+update_vwap|self\.vwap\s*=",
        [files.get("futures_level_trading.py", Path("/nonexistent"))],
        max_results=3,
    )
    if update:
        score += 1
        evidences.extend(update[:1])

    # 3. VWAP Reclaim / Rejection 判定
    reclaim = grep_codebase(
        r"vwap_dist|VWAP_REVERT_SIGMA|vwap.*reclaim|vwap.*rejection|VWAP 平均回帰",
        [files.get("futures_level_trading.py", Path("/nonexistent"))],
        max_results=3,
    )
    if reclaim:
        score += 1
        evidences.extend(reclaim[:1])

    # 4. chronos_bot.py で VWAP を env に含める経路
    integration = grep_codebase(
        r"env\[.vwap.\]|self\.vwap|update_vwap\(",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if integration:
        score += 1
        evidences.extend(integration[:1])

    # 5. Anchored VWAP (前日高・前日安・発表時点など複数アンカー)
    anchored = grep_codebase(
        r"anchored_vwap|AnchoredVWAP|prev_high.*vwap|vwap.*anchor",
        [files.get("futures_level_trading.py", Path("/nonexistent")),
         files["chronos_bot.py"]],
        max_results=3,
    )
    if anchored:
        score += 1
        evidences.extend(anchored[:1])

    rationale = (
        "VWAP 完全実装: calc_vwap + update_vwap + Reclaim/Rejection + bot統合 + Anchored対応"
        if score >= 4 else
        "VWAP 基本実装 (RTH VWAP + Mean Reversion判定) あるが Anchored VWAP未実装"
        if score >= 3 else
        "VWAP 部分実装または未統合"
    )

    improvement = (
        "Anchored VWAP (前日高・前日安・FOMC発表時点) を追加。"
        "Brian Shannon AlphaTrends 流の複数アンカー運用 (research §B-3)"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F11",
        criterion_name="VWAP使用",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f12_cumulative_delta(files: dict[str, Path]) -> CriteriaScore:
    """F12: Cumulative Delta"""
    evidences = []
    score = 0

    # 1. CumulativeDelta クラス実装 (chronos_cumulative_delta.py)
    cd_class = grep_codebase(
        r"class\s+CumulativeDelta|CumulativeDelta",
        list(files.values()),
        max_results=5,
    )
    if cd_class:
        score += 1
        evidences.extend(cd_class[:1])

    # 2. Bid/Ask volume 比率 (代替プロキシ: calc_bid_ask_delta / calc_volume_ratio)
    bid_ask = grep_codebase(
        r"bid_volume|ask_volume|bid_ask_ratio|calc_bid_ask_delta|delta_volume",
        list(files.values()),
        max_results=5,
    )
    if bid_ask:
        score += 1
        evidences.extend(bid_ask[:1])

    # 3. 方向性プロキシ / 乖離検出 (detect_divergence / volume_ratio)
    proxy = grep_codebase(
        r"detect_divergence|volume_ratio|buy_sell_ratio|delta_sign|calc_volume_ratio",
        list(files.values()),
        max_results=5,
    )
    if proxy:
        score += 1
        evidences.extend(proxy[:1])

    # 4. 日次 reset / バケット集計 (daily_reset / bucket_minutes)
    daily_reset_check = grep_codebase(
        r"daily_reset|bucket_minutes|_flush_bucket|BucketDelta",
        list(files.values()),
        max_results=5,
    )
    if daily_reset_check:
        score += 1
        evidences.extend(daily_reset_check[:1])

    # 5. 戦略統合 (chronos_rules.yaml: cumulative_delta セクション + strategy_selector 統合)
    yaml_path = files.get("chronos_rules.yaml", Path("/nonexistent"))
    yaml_section = has_yaml_key(yaml_path, ["cumulative_delta"])
    selector_integration = grep_codebase(
        r"cumulative_delta_bias|_CUMULATIVE_DELTA_AVAILABLE",
        list(files.values()),
        max_results=3,
    )
    if yaml_section is not None or selector_integration:
        score += 1
        if selector_integration:
            evidences.extend(selector_integration[:1])

    rationale = (
        "Cumulative Delta 完全実装: CumulativeDelta クラス / bid_ask delta / 乖離検出 / 日次reset / yaml+selector統合"
        if score >= 5 else
        "Cumulative Delta 高度実装 (4/5 要素)"
        if score >= 4 else
        "Cumulative Delta 実装あり (3/5 要素)"
        if score >= 3 else
        "Cumulative Delta 部分実装 (プロキシのみ)"
        if score > 0 else
        "Cumulative Delta 未実装"
    )

    improvement = (
        ""
        if score >= 5 else
        "Tradovate MD WebSocket の DOM 経由で bid/ask volume をリアルタイム取得して "
        "CumulativeDelta.update() に渡す実装を追加 (dom_proxy_enabled: true)。"
    )

    return CriteriaScore(
        criterion_id="F12",
        criterion_name="Cumulative Delta",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f13_liquidity_sweep(files: dict[str, Path]) -> CriteriaScore:
    """F13: Liquidity Sweep認識"""
    evidences = []
    score = 0

    # 1. LiquiditySweepDetector クラス実装 (chronos_liquidity_sweep.py)
    sweep_class = grep_codebase(
        r"class\s+LiquiditySweepDetector|LiquiditySweepDetector|liquidity_sweep",
        list(files.values()),
        max_results=5,
    )
    if sweep_class:
        score += 2
        evidences.extend(sweep_class[:1])

    # 2. 前日高安 / IB端 sweep検知 (prev_high / prev_low / ib_high / ib_low)
    prev_high_low = grep_codebase(
        r"prev_high|prev_low|ib_high|ib_low|sweep_high|sweep_low",
        list(files.values()),
        max_results=5,
    )
    if prev_high_low:
        score += 1
        evidences.extend(prev_high_low[:1])

    # 3. 出来高フィルタ + ATR 反転確認
    volume_atr = grep_codebase(
        r"volume_multiplier|volume_ratio.*sweep|reversal_atr|atr_breach",
        list(files.values()),
        max_results=3,
    )
    if volume_atr:
        score += 1
        evidences.extend(volume_atr[:1])

    # 4. Reversal エントリー + 戦略統合 (is_reversal_confirmed / get_entry_signal / yaml統合)
    reversal_integration = grep_codebase(
        r"is_reversal_confirmed|get_entry_signal|liquidity_sweep_reversal|_LIQUIDITY_SWEEP_AVAILABLE",
        list(files.values()),
        max_results=3,
    )
    if reversal_integration:
        score += 1
        evidences.extend(reversal_integration[:1])

    rationale = (
        "Liquidity Sweep 完全実装: LiquiditySweepDetector / prev_high/low + IB端 / 出来高+ATRフィルタ / reversal統合"
        if score >= 5 else
        "Liquidity Sweep 高度実装 (4/5 要素)"
        if score >= 4 else
        "Liquidity Sweep 実装あり (sweep クラス + 部分統合)"
        if score >= 3 else
        "Liquidity Sweep 部分実装"
        if score > 0 else
        "Liquidity Sweep 未実装"
    )

    improvement = (
        ""
        if score >= 5 else
        "chronos_liquidity_sweep.py の LiquiditySweepDetector と "
        "chronos_strategy_selector.py の F13 統合を完成させる。"
        "yaml: liquidity_sweep セクションの全パラメータを設定する。"
    )

    return CriteriaScore(
        criterion_id="F13",
        criterion_name="Liquidity Sweep認識",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f14_phase(files: dict[str, Path]) -> CriteriaScore:
    """F14: Phase認識"""
    evidences = []
    score = 0

    # 1. Phase定数 (PHASE_EVALUATION / PHASE_SIM_FUNDED / PHASE_SIM_FUNDED_AFTER_PAYOUT)
    phase_const = grep_codebase(
        r"PHASE_EVALUATION|PHASE_SIM_FUNDED|PHASE_SIM_FUNDED_AFTER_PAYOUT|PHASE_LIVE",
        [files["chronos_mffu_rules.py"], files["chronos_bot.py"]],
        max_results=5,
    )
    phases = set()
    for e in phase_const:
        for kw in ["PHASE_EVALUATION", "PHASE_SIM_FUNDED", "AFTER_PAYOUT", "PHASE_LIVE"]:
            if kw in e.snippet:
                phases.add(kw)
    if len(phases) >= 3:
        score += 1
        evidences.extend(phase_const[:2])

    # 2. account_type 分岐
    account_type = grep_codebase(
        r"account_type|_account_type|mffu_eval|mffu_sim_funded",
        [files["chronos_bot.py"], files["chronos_rules.yaml"]],
        max_results=5,
    )
    if account_type:
        score += 1
        evidences.extend(account_type[:1])

    # 3. on_payout_received ハンドラ
    on_payout = grep_codebase(
        r"on_payout_received|on_first_payout_received",
        [files["chronos_bot.py"], files["chronos_mffu_rules.py"]],
        max_results=3,
    )
    if on_payout:
        score += 1
        evidences.extend(on_payout[:1])

    # 4. survival_mode 切替
    survival = grep_codebase(
        r"_survival_mode_active|survival_mode_after_payout|_apply_survival_mode",
        [files["chronos_bot.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if survival:
        score += 1
        evidences.extend(survival[:1])

    # 5. _get_active_phase_config で設定返却
    active_config = grep_codebase(
        r"_get_active_phase_config|get_survival_mode_config",
        [files["chronos_bot.py"], files["chronos_mffu_rules.py"]],
        max_results=3,
    )
    if active_config:
        score += 1
        evidences.extend(active_config[:1])

    rationale = (
        "Phase認識完全: 4 Phase定数 + account_type分岐 + on_payout_received遷移 + "
        "survival_mode + _get_active_phase_config"
        if score >= 4 else
        "Phase認識実装済みだが一部経路欠落"
        if score >= 3 else
        "Phase認識 不完全"
    )

    improvement = (
        "PHASE_LIVE の実装を進める (現在はEvaluationとSim-Fundedのみ本実装)。"
        "MFFU Live移行時に規律を変えるロジック追加"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F14",
        criterion_name="Phase認識",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f15_rate_limit(files: dict[str, Path]) -> CriteriaScore:
    """F15: Rate-limit処理"""
    evidences = []
    score = 0

    # 1. _request_with_backoff 実装
    backoff = grep_codebase(
        r"def\s+_request_with_backoff|backoff_base_sec|backoff_max_sec",
        [files["tradovate_client.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if backoff:
        score += 2
        evidences.extend(backoff[:1])

    # 2. 429 検知・指数バックオフ
    exp_backoff = grep_codebase(
        r"RATE_LIMIT_STATUS_CODE|status_code.*429|backoff\s*\*\s*2",
        [files["tradovate_client.py"]],
        max_results=3,
    )
    if exp_backoff:
        score += 1
        evidences.extend(exp_backoff[:1])

    # 3. 連続停止ガード (_rate_limit_halted)
    halt = grep_codebase(
        r"_rate_limit_halted|is_rate_limit_halted|consecutive_halt_count",
        [files["tradovate_client.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if halt:
        score += 1
        evidences.extend(halt[:1])

    # 4. p-ticket / p-captcha / p-time 対応
    ticket = grep_codebase(
        r"p-ticket|p-captcha|p-time|CAPTCHA",
        [files["tradovate_client.py"]],
        max_results=3,
    )
    if ticket:
        score += 1
        evidences.extend(ticket[:1])

    rationale = (
        "Rate-limit完全実装: _request_with_backoff + 指数backoff + 連続halt + p-ticket検知"
        if score >= 4 else
        "Rate-limit実装済みだがp-ticket対応が部分的 (検知はするがハンドリングロジック限定)"
        if score >= 3 else
        "Rate-limit 不完全"
    )

    improvement = (
        "p-ticket 受信時の CAPTCHA 解決フローを実装 (現状はログ出力のみ・resolve後にrenew_token経路)。"
        "reset_rate_limit_daily を daily_reset に紐付ける launchd job 追加"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F15",
        criterion_name="Rate-limit処理",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


def _score_f16_risk_sizing(files: dict[str, Path]) -> CriteriaScore:
    """F16: Risk-per-trade サイジング"""
    evidences = []
    score = 0

    # 1. Kelly 基準実装 (_calc_contracts)
    kelly = grep_codebase(
        r"calc_kelly_fraction|_calc_contracts|kelly.*\*\s*account_balance|dollar_risk",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if kelly:
        score += 1
        evidences.extend(kelly[:1])

    # 2. OR幅ベースの risk_per_contract
    or_risk = grep_codebase(
        r"risk_per_contract|or_range\s*\*\s*ORB_STOP_ATR_MULT\s*\*\s*point_value",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if or_risk:
        score += 1
        evidences.extend(or_risk[:1])

    # 3. Consistency-aware cap
    cons_cap = grep_codebase(
        r"consistency_cap|Consistency cap|max_daily_pnl.*0\.35",
        [files["chronos_bot.py"]],
        max_results=3,
    )
    if cons_cap:
        score += 1
        evidences.extend(cons_cap[:1])

    # 4. max_contracts 経路 (Scaling table / MFFURuleGuard)
    max_con = grep_codebase(
        r"get_allowed_contracts|get_max_mini_contracts|max_concurrent_contracts",
        [files["chronos_mffu_rules.py"], files["chronos_bot.py"], files["chronos_rules.yaml"]],
        max_results=3,
    )
    if max_con:
        score += 1
        evidences.extend(max_con[:1])

    # 5. 0.2-0.5% risk 制限 (survival mode の per_trade_stop_usd = $25 on MLL $100 = 25%)
    risk_pct = grep_codebase(
        r"max_loss_per_trade_pct|per_trade_stop_usd|risk.*0\.5%|risk.*0\.25",
        [files["chronos_rules.yaml"], files["chronos_mffu_rules.py"]],
        max_results=3,
    )
    if risk_pct:
        score += 1
        evidences.extend(risk_pct[:1])

    rationale = (
        "Risk-per-trade 完全実装: Kelly + OR幅risk + Consistency-cap + max_contracts + 口座%制限"
        if score >= 4 else
        "サイジング実装あるが max_loss_per_trade_pct が null (chronos_rules.yaml)"
        if score >= 3 else
        "Risk-per-trade サイジング 不完全"
    )

    improvement = (
        "chronos_rules.yaml の risk.max_loss_per_trade_pct: null を 0.5% (=$250 on $50K) に確定。"
        "現在は Kelly + OR幅 + Consistency cap の3層でカバーしているが明示制限がない"
    ) if score < 5 else ""

    return CriteriaScore(
        criterion_id="F16",
        criterion_name="Risk-per-trade サイジング",
        score=score,
        rationale=rationale,
        evidences=evidences,
        improvement=improvement,
    )


# ---------------------------------------------------------------------------
# 採点ディスパッチャ
# ---------------------------------------------------------------------------

SCORING_FUNCS = {
    "F1": _score_f1_orb,
    "F2": _score_f2_vix_mr,
    "F3": _score_f3_max_loss,
    "F4": _score_f4_consistency,
    "F5": _score_f5_news_window,
    "F6": _score_f6_maintenance_break,
    "F7": _score_f7_hedging,
    "F8": _score_f8_loss_streak,
    "F9": _score_f9_session,
    "F10": _score_f10_atr_regime,
    "F11": _score_f11_vwap,
    "F12": _score_f12_cumulative_delta,
    "F13": _score_f13_liquidity_sweep,
    "F14": _score_f14_phase,
    "F15": _score_f15_rate_limit,
    "F16": _score_f16_risk_sizing,
}


class FuturesTraderEvaluator:
    """Chronos コードベースを静的解析で採点する。"""

    def __init__(self, codebase_path: Path):
        self.codebase = Path(codebase_path)
        self.files = self._load_files()

    def _load_files(self) -> dict[str, Path]:
        """対象ファイルを辞書で返す。存在しないファイルはダミーパス。"""
        d: dict[str, Path] = {}
        for fname in DEFAULT_TARGETS:
            p = self.codebase / fname
            d[fname] = p if p.exists() else Path("/nonexistent") / fname
        return d

    def evaluate(self) -> EvaluationReport:
        """16項目すべてを採点してレポートを返す。"""
        scores: list[CriteriaScore] = []
        for c in CRITERIA:
            cid = c["id"]
            func = SCORING_FUNCS[cid]
            cs = func(self.files)
            cs.criterion_name = c["name"]
            scores.append(cs)

        return EvaluationReport(
            generated_at=datetime.now().isoformat(),
            codebase_path=str(self.codebase),
            target_files=[f for f in DEFAULT_TARGETS if (self.codebase / f).exists()],
            scores=scores,
        )


# ---------------------------------------------------------------------------
# Markdown レポート生成
# ---------------------------------------------------------------------------

def build_markdown_report(report: EvaluationReport) -> str:
    total = report.total_score
    max_t = report.max_total
    pct = report.score_pct
    judge = report.pass_judge

    # 改善優先 TOP 3 (スコア低い順)
    sorted_scores = sorted(report.scores, key=lambda s: s.score)
    top3_improve = [s for s in sorted_scores if s.score < 5][:3]

    lines = [
        f"# Chronos 先物トレーダー判定レポート",
        "",
        f"**採点日時**: {report.generated_at}",
        f"**対象コードベース**: `{report.codebase_path}`",
        f"**対象ファイル**: {len(report.target_files)} 本 ({', '.join(report.target_files[:8])}{'...' if len(report.target_files) > 8 else ''})",
        "",
        "---",
        "",
        "## 1. エグゼクティブサマリー",
        "",
        f"### 合計点: **{total} / {max_t} 点 ({pct:.1f}%)**",
        "",
        f"**合格判定: {judge}**",
        "",
        f"- 合格ライン ({PASS_THRESHOLD}点 / 75%): **{'達成' if total >= PASS_THRESHOLD else '未達'}**",
        f"- 優秀ライン ({EXCELLENT_THRESHOLD}点 / 87.5%): **{'達成' if total >= EXCELLENT_THRESHOLD else '未達'}**",
        "",
        "### スコア分布",
        "",
        "| ランク | 対象 |",
        "|---|---|",
    ]
    rank_5 = [s for s in report.scores if s.score == 5]
    rank_4 = [s for s in report.scores if s.score == 4]
    rank_3 = [s for s in report.scores if s.score == 3]
    rank_2 = [s for s in report.scores if s.score == 2]
    rank_1 = [s for s in report.scores if s.score == 1]
    rank_0 = [s for s in report.scores if s.score == 0]
    lines += [
        f"| 5 マスター級 | {', '.join(f'{s.criterion_id}' for s in rank_5) or '(なし)'} |",
        f"| 4 高度実装 | {', '.join(f'{s.criterion_id}' for s in rank_4) or '(なし)'} |",
        f"| 3 基本実装 | {', '.join(f'{s.criterion_id}' for s in rank_3) or '(なし)'} |",
        f"| 2 部分実装 | {', '.join(f'{s.criterion_id}' for s in rank_2) or '(なし)'} |",
        f"| 1 部分実装(バグ) | {', '.join(f'{s.criterion_id}' for s in rank_1) or '(なし)'} |",
        f"| 0 未実装 | {', '.join(f'{s.criterion_id}' for s in rank_0) or '(なし)'} |",
        "",
        "### 改善優先 TOP 3",
        "",
    ]

    for i, s in enumerate(top3_improve, 1):
        lines += [
            f"#### {i}. {s.criterion_id} {s.criterion_name} ({s.score}/5点)",
            f"- **現状**: {s.rationale}",
            f"- **改善案**: {s.improvement}",
            "",
        ]

    lines += [
        "---",
        "",
        "## 2. 16項目 詳細採点",
        "",
        "| ID | 項目 | 点数 | 判定 |",
        "|---|---|---|---|",
    ]
    for s in report.scores:
        status = (
            "EXCELLENT" if s.score == 5 else
            "GOOD" if s.score == 4 else
            "OK" if s.score == 3 else
            "WARN" if s.score == 2 else
            "POOR" if s.score == 1 else
            "FAIL"
        )
        lines.append(f"| {s.criterion_id} | {s.criterion_name} | {s.score}/{s.max_score} | {status} |")
    lines.append("")

    # 個別採点詳細
    for s in report.scores:
        lines += [
            f"### {s.criterion_id}. {s.criterion_name} — **{s.score}/{s.max_score} 点**",
            "",
            f"**評価根拠**: {s.rationale}",
            "",
        ]
        if s.evidences:
            lines.append("**実装エビデンス**:")
            for e in s.evidences[:5]:
                # snippetの安全化
                snippet = e.snippet.replace("|", "\\|")
                lines.append(f"- `{e.file}:{e.line}` — `{snippet}`")
            lines.append("")
        if s.improvement:
            lines += [f"**改善提案**: {s.improvement}", ""]

    # 合格ラインへのギャップ分析
    lines += [
        "---",
        "",
        "## 3. 合格ラインへのギャップ分析",
        "",
    ]
    gap_to_pass = max(0, PASS_THRESHOLD - total)
    gap_to_excellent = max(0, EXCELLENT_THRESHOLD - total)
    lines += [
        f"- 現在点: {total}/{max_t}",
        f"- 合格まで: **{gap_to_pass}点**",
        f"- 優秀まで: **{gap_to_excellent}点**",
        "",
    ]

    if total >= EXCELLENT_THRESHOLD:
        lines.append("**優秀ライン達成。私募ファンド運用水準に到達。**")
    elif total >= PASS_THRESHOLD:
        lines.append(
            f"**合格ライン達成。あと{gap_to_excellent}点で優秀ライン。"
            f"下記の改善TOP3 (合計{sum(5 - s.score for s in top3_improve)}点余地) に集中すれば達成可能。**"
        )
    else:
        lines.append(
            f"**未達。最低 {gap_to_pass} 点の積み上げが必要。"
            f"F12-F13 (Cumulative Delta / Liquidity Sweep) は Phase3戦術のため後回し推奨、"
            f"F10-F11 (ATR Regime / Anchored VWAP) を先に固める。**"
        )

    lines += [
        "",
        "---",
        "",
        "## 4. 次回採点予定",
        "",
        "- **Week 1 (MFFU Eval開始直後)**: ペーパー5日稼働後に再採点。F8 連敗制御・F15 rate-limit の実動作確認 (実トレード駆動)",
        "- **Week 2-3 (Eval通過前)**: Red Team対応を含む改善完了後に再採点。F10 ATR Regime を実装して +2-3点",
        "- **Month 1 (Sim-Funded移行後)**: Phase切替の実動作確認 + F4 Consistency自動遷移の検証",
        "- **Month 3 (ThetaData Pro契約時)**: F12/F13 (Cumulative Delta / Liquidity Sweep) 実装で +6-8点・優秀ライン到達を目標",
        "",
        "---",
        "",
        "## 5. 参考資料",
        "",
        "- `data/research_mes_trader_day_20260419.md` — 起点調査 (16項目ドラフト§5 / Chronos翻訳表§4)",
        "- `data/futures_trader_evaluation_framework.md` — 本FWの設計書",
        "- `scripts/trader_evaluation.py` — Atlas版 (0DTE 15指標) 同型スクリプト",
        "- `data/eval/trader_eval_20260418.md` — Atlas採点前例",
        "",
        f"*Generated by scripts/futures_trader_evaluation.py (Sora Lab / Chronos) — {datetime.now().strftime('%Y-%m-%d %H:%M JST')}*",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="先物MES優秀トレーダー判定FW (Chronos採点)")
    parser.add_argument("--codebase", type=str, default=str(BASE),
                        help="Chronos コードベースのディレクトリ (default: リポジトリルート)")
    parser.add_argument("--out", type=str, default=None,
                        help="出力ファイルパス (default: data/eval/chronos_trader_eval_YYYYMMDD.md)")
    parser.add_argument("--json", action="store_true", help="JSONも出力する")
    parser.add_argument("--no-pushover", action="store_true")
    args = parser.parse_args()

    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    evaluator = FuturesTraderEvaluator(args.codebase)
    report = evaluator.evaluate()

    # デフォルト出力先
    today = datetime.now().strftime("%Y%m%d")
    out_path = Path(args.out) if args.out else (EVAL_DIR / f"chronos_trader_eval_{today}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Markdown
    md_content = build_markdown_report(report)
    out_path.write_text(md_content, encoding="utf-8")
    print(f"[eval] Report: {out_path}", flush=True)

    # JSON
    if args.json:
        json_path = out_path.with_suffix(".json")
        json_data = {
            "generated_at": report.generated_at,
            "codebase_path": report.codebase_path,
            "target_files": report.target_files,
            "total_score": report.total_score,
            "max_total": report.max_total,
            "score_pct": report.score_pct,
            "pass_judge": report.pass_judge,
            "scores": [s.to_dict() for s in report.scores],
        }
        json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"[eval] JSON: {json_path}", flush=True)

    # サマリー表示
    print("")
    print(f"合計: {report.total_score}/{report.max_total} ({report.score_pct:.1f}%) — {report.pass_judge}")
    print("")
    for s in report.scores:
        print(f"  {s.criterion_id:3s} {s.criterion_name:30s}  {s.score}/5")
    print("")

    # Pushover通知
    if not args.no_pushover:
        top3 = sorted(report.scores, key=lambda s: s.score)[:3]
        top3_str = ", ".join(f"{s.criterion_id}({s.score})" for s in top3)
        msg = (
            f"Chronos採点: {report.total_score}/{report.max_total} "
            f"({report.score_pct:.0f}%) {report.pass_judge}\n"
            f"改善TOP3: {top3_str}\n"
            f"出力: {out_path.name}"
        )
        priority = 0 if report.pass_judge in ("PASS", "EXCELLENT") else 1
        send_pushover(
            f"[Chronos] 先物トレーダー判定 {report.total_score}/{report.max_total}",
            msg,
            priority=priority,
        )

    print(f"[eval] Done. Judge={report.pass_judge}")


if __name__ == "__main__":
    main()
