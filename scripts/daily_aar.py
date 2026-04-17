#!/usr/bin/env python3
"""Daily AAR (After Action Review) — 米軍AAR方式
平日 05:15 JST 自動実行

4問フォーマット:
  a) 想定: strategy_selector が決定した戦術・パラメータ
  b) 実際: condor.log 実績 + P&L
  c) 差分: 想定と実際のギャップ
  d) 次回改善提案: Claude haiku による分析

出力: data/aar_YYYYMMDD.md
通知: Pushover [Atlas/AAR]
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib import parse, request

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
LOG_DIR = DATA / "logs"
AAR_DIR = DATA  # aar_YYYYMMDD.md を data/ 直下に出力

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "u2cevk8nktib3sr148rw2hs78ecvux")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


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
# Data loading helpers
# ---------------------------------------------------------------------------

def load_condor_pnl(target_date: date) -> dict:
    """condor_pnl.json から target_date のトレード情報を抽出"""
    pnl_file = DATA / "condor_pnl.json"
    if not pnl_file.exists():
        return {"entries": [], "exits": [], "env_snapshots": [], "total_pnl": None}

    with open(pnl_file) as f:
        raw = json.load(f)

    trades = raw.get("trades", [])
    date_str = target_date.isoformat()

    entries = [t for t in trades if t.get("event") == "entry" and t.get("date") == date_str]
    exits = [t for t in trades if t.get("event") == "exit" and t.get("date") == date_str]
    env_snaps = [t for t in trades if t.get("event") == "env_snapshot" and t.get("date") == date_str]

    total_pnl = None
    pnl_values = [t.get("pnl_usd") for t in exits if t.get("pnl_usd") is not None]
    if pnl_values:
        total_pnl = sum(pnl_values)

    return {
        "entries": entries,
        "exits": exits,
        "env_snapshots": env_snaps,
        "total_pnl": total_pnl,
    }


def load_condor_csv(target_date: date) -> list[dict]:
    """condor_2026-MM.csv から target_date の行を抽出"""
    month_str = target_date.strftime("%Y-%m")
    csv_file = LOG_DIR / f"condor_{month_str}.csv"
    if not csv_file.exists():
        return []

    results = []
    with open(csv_file) as f:
        header = None
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if header is None:
                header = parts
                continue
            row = dict(zip(header, parts))
            # timestamp 先頭がターゲット日付
            ts = row.get("timestamp", "")
            if ts.startswith(target_date.isoformat()):
                results.append(row)
    return results


def extract_strategy_selector_output(target_date: date) -> dict:
    """condor_pnl.json の env_snapshot から戦術選択内容を復元"""
    pnl_data = load_condor_pnl(target_date)
    snaps = pnl_data.get("env_snapshots", [])
    entries = pnl_data.get("entries", [])

    if not snaps and not entries:
        return {"tactics": [], "vix": None, "env_score": None, "regime": None, "direction": None}

    # 最初の env_snapshot から環境情報取得
    first_snap = snaps[0] if snaps else {}
    tactics = list({e.get("tactic", "unknown") for e in entries})

    return {
        "tactics": tactics,
        "vix": first_snap.get("vix"),
        "env_score": first_snap.get("env_score"),
        "regime": first_snap.get("regime"),
        "direction": first_snap.get("direction"),
        "params": first_snap.get("params", {}),
        "vrp": first_snap.get("vrp"),
        "ivr": first_snap.get("ivr"),
    }


def parse_condor_log_for_date(target_date: date) -> list[str]:
    """condor.log から target_date の行を最大100行抽出"""
    condor_log = LOG_DIR / "condor.log"
    if not condor_log.exists():
        return []

    date_str = target_date.strftime("%Y-%m-%d")
    lines = []
    with open(condor_log) as f:
        for line in f:
            if date_str in line:
                lines.append(line.strip())

    return lines[-100:]  # 直近100行


# ---------------------------------------------------------------------------
# Haiku call for AAR analysis (question d)
# ---------------------------------------------------------------------------

def generate_improvement_suggestion(
    target_date: date,
    planned: dict,
    actual: dict,
    gap: str,
) -> str:
    """Claude haiku を呼び出して改善提案を生成。APIキーなければ fallback"""
    if not ANTHROPIC_API_KEY:
        return generate_improvement_fallback(planned, actual, gap)

    prompt = f"""あなたはSPX 0DTE オプション自動売買ボット(Sora Lab)の戦後分析担当です。
以下のDaily AARデータを分析し、日本語で改善提案を200字以内でまとめてください。

# 対象日: {target_date.isoformat()}

## 想定（戦術選択）
- 戦術: {planned.get('tactics', [])}
- VIX: {planned.get('vix')}
- 環境スコア: {planned.get('env_score')}
- レジーム: {planned.get('regime')}
- 方向: {planned.get('direction')}
- パラメータ: {planned.get('params', {})}

## 実際（結果）
- エントリー数: {actual.get('entry_count', 0)}
- エグジット数: {actual.get('exit_count', 0)}
- 総P&L: ${actual.get('total_pnl', 'N/A')}
- 早期クローズ理由: {actual.get('early_exit_reasons', [])}

## 差分
{gap}

改善提案（200字以内）:"""

    try:
        import urllib.request
        import json as _json

        payload = _json.dumps({
            "model": "claude-haiku-4-5",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = _json.loads(resp.read())
            return result["content"][0]["text"].strip()
    except Exception as e:
        print(f"[haiku error] {e}", file=sys.stderr)
        return generate_improvement_fallback(planned, actual, gap)


def generate_improvement_fallback(planned: dict, actual: dict, gap: str) -> str:
    """APIキー未設定 or 失敗時のルールベース改善提案"""
    suggestions = []

    total_pnl = actual.get("total_pnl")
    early_exits = actual.get("early_exit_reasons", [])
    entry_count = actual.get("entry_count", 0)

    if total_pnl is not None and total_pnl < 0:
        suggestions.append("P&L マイナス: 翌日エントリー条件の env_score 閾値を引き上げ検討")

    if any("crisis" in str(r) for r in early_exits):
        suggestions.append("intraday_crisis 発動: VIX急騰検知の感度パラメータ確認")

    if entry_count == 0:
        suggestions.append("エントリー0件: フィルタ条件が厳しすぎる可能性。env_score 計算ロジック確認")

    if not suggestions:
        suggestions.append("目立った逸脱なし。現行パラメータ維持で継続観察")

    return " / ".join(suggestions)


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------

def compute_gap(planned: dict, actual: dict) -> str:
    lines = []

    # エントリー数チェック
    entry_count = actual.get("entry_count", 0)
    if entry_count == 0 and planned.get("tactics"):
        lines.append(f"- 想定戦術 {planned['tactics']} に対してエントリー0件 → フィルタ・接続エラー確認必要")
    elif entry_count > 0:
        lines.append(f"- エントリー {entry_count}件 実行")

    # P&L
    pnl = actual.get("total_pnl")
    if pnl is not None:
        if pnl > 0:
            lines.append(f"- P&L: +${pnl:.2f} (プラス)")
        elif pnl < 0:
            lines.append(f"- P&L: -${abs(pnl):.2f} (マイナス) → 損失原因を特定すること")
        else:
            lines.append("- P&L: $0.00 (ドライラン/未決済)")

    # 早期クローズ
    early_exits = actual.get("early_exit_reasons", [])
    if early_exits:
        lines.append(f"- 早期クローズ発動: {set(early_exits)}")

    if not lines:
        lines.append("- データ不足のため差分評価不可。ログ確認を推奨")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main AAR generation
# ---------------------------------------------------------------------------

def run_aar(target_date: date | None = None) -> Path:
    if target_date is None:
        # 05:15 JST 実行 → 前営業日（ET当日）を対象
        # 平日ET市場日 = JST前日。土曜朝 → 金曜ET分
        now_jst = datetime.now()
        target_date = now_jst.date() - timedelta(days=1)

    date_str = target_date.strftime("%Y%m%d")
    out_path = AAR_DIR / f"aar_{date_str}.md"

    print(f"[AAR] Generating for {target_date.isoformat()} ...", flush=True)

    # --- a) 想定 ---
    planned = extract_strategy_selector_output(target_date)

    # --- b) 実際 ---
    pnl_data = load_condor_pnl(target_date)
    csv_rows = load_condor_csv(target_date)
    log_lines = parse_condor_log_for_date(target_date)

    exits = pnl_data.get("exits", [])
    early_exit_reasons = [e.get("reason") for e in exits if e.get("reason") not in ("15:50_force_close", "force_close_15:50")]

    actual = {
        "entry_count": len(pnl_data.get("entries", [])),
        "exit_count": len(exits),
        "total_pnl": pnl_data.get("total_pnl"),
        "early_exit_reasons": early_exit_reasons,
        "csv_row_count": len(csv_rows),
        "log_line_count": len(log_lines),
        "tactics_used": list({e.get("tactic", "unknown") for e in pnl_data.get("entries", [])}),
    }

    # --- c) 差分 ---
    gap = compute_gap(planned, actual)

    # --- d) 改善提案 ---
    suggestion = generate_improvement_suggestion(target_date, planned, actual, gap)

    # --- Markdown 出力 ---
    lines_out = [
        f"# Daily AAR — {target_date.isoformat()}",
        f"",
        f"**生成日時**: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} JST",
        f"",
        f"---",
        f"",
        f"## a) 想定（strategy_selector 出力）",
        f"",
        f"| 項目 | 値 |",
        f"|---|---|",
        f"| 戦術 | {planned.get('tactics', 'N/A')} |",
        f"| VIX | {planned.get('vix', 'N/A')} |",
        f"| 環境スコア | {planned.get('env_score', 'N/A')} |",
        f"| レジーム | {planned.get('regime', 'N/A')} |",
        f"| 方向 | {planned.get('direction', 'N/A')} |",
        f"| VRP | {planned.get('vrp', 'N/A')} |",
        f"| IVR | {planned.get('ivr', 'N/A')} |",
        f"| パラメータ | {planned.get('params', {})} |",
        f"",
        f"## b) 実際（condor.log 実績 + P&L）",
        f"",
        f"| 項目 | 値 |",
        f"|---|---|",
        f"| エントリー数 | {actual['entry_count']} |",
        f"| エグジット数 | {actual['exit_count']} |",
        f"| 総P&L | ${actual['total_pnl'] if actual['total_pnl'] is not None else 'N/A'} |",
        f"| 使用戦術 | {actual['tactics_used']} |",
        f"| 早期クローズ理由 | {actual['early_exit_reasons'] if actual['early_exit_reasons'] else 'なし'} |",
        f"| CSV行数 | {actual['csv_row_count']} |",
        f"| ログ行数 (抜粋) | {actual['log_line_count']} |",
        f"",
    ]

    if log_lines:
        lines_out += [
            f"### condor.log 抜粋（直近20行）",
            f"",
            "```",
        ]
        for ll in log_lines[-20:]:
            lines_out.append(ll)
        lines_out += ["```", ""]

    lines_out += [
        f"## c) 差分（想定 vs 実際）",
        f"",
        gap,
        f"",
        f"## d) 次回改善提案",
        f"",
        suggestion,
        f"",
        f"---",
        f"*Generated by scripts/daily_aar.py (Sora Lab)*",
    ]

    out_path.write_text("\n".join(lines_out), encoding="utf-8")
    print(f"[AAR] Written: {out_path}", flush=True)

    # --- Pushover 通知 ---
    pnl_str = f"${actual['total_pnl']:.2f}" if actual["total_pnl"] is not None else "N/A"
    msg = (
        f"対象日: {target_date.isoformat()}\n"
        f"戦術: {planned.get('tactics', 'N/A')} | P&L: {pnl_str}\n"
        f"エントリー: {actual['entry_count']}件\n"
        f"改善提案: {suggestion[:100]}"
    )
    send_pushover("[Atlas/AAR] Daily AAR 完了", msg)

    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Daily AAR generator")
    parser.add_argument("--date", type=str, help="対象日 YYYY-MM-DD (省略時=昨日)", default=None)
    args = parser.parse_args()

    target = None
    if args.date:
        target = date.fromisoformat(args.date)

    out = run_aar(target)
    print(f"[AAR] Done: {out}")
