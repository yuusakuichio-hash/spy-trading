#!/usr/bin/env python3
"""
premortem.py - Gary Klein premortem analysis runner (Sora Lab)

背景: 2026-04-12 ゆうさくさん指示
  unittestは「想定済み」しかカバーしない。着手前に
  「このタスクが失敗したらどう失敗するか」を体系探索する手順が不在。
  認知科学 Gary Klein の premortem 手法 + HAZOP guide words + ACH
  (Analysis of Competing Hypotheses) を合成して Atlas/Builder に組み込む。

設計方針（CLAUDE.md 規律準拠）:
  - 環境適応型: 閾値・対策はタスク内容とコンテキストから動的生成
  - 低コストモデル (Claude Haiku) を別呼び出し、メインセッション圧迫しない
  - Haiku API key 未設定時は決定論的 fallback で guide words 骨組みのみ出力
  - 出力は data/premortem_reports/<timestamp>_<slug>.md に永続化
  - 結果は stdout にも pathと要約を出し、gate.sh から検知可能

CLI:
  python3 scripts/premortem.py --task "タスク記述" [--files path1,path2] [--out PATH]
  python3 scripts/premortem.py --task "..." --json        # JSON結果をstdoutへ
  python3 scripts/premortem.py --selftest                 # API叩かず動作確認

終了コード:
  0: 成功 (レポート生成)
  1: 引数不正
  2: API呼び出し失敗 (fallback で出力はしている)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = BASE_DIR / "data" / "premortem_reports"
ENV_FILE = BASE_DIR / ".env"

HAIKU_MODEL = "claude-3-5-haiku-20241022"
API_URL = "https://api.anthropic.com/v1/messages"
API_TIMEOUT = 60

# HAZOP guide words (Imperial Chemical Industries / IEC 61882)
HAZOP_GUIDE_WORDS = [
    ("No/None", "その要素が完全に欠落したら？"),
    ("More", "想定より多すぎる/大きすぎる/速すぎる場合は？"),
    ("Less", "想定より少ない/小さい/遅い場合は？"),
    ("As well as", "想定外の追加が混入したら？"),
    ("Part of", "一部だけ成立・残りが欠落したら？"),
    ("Reverse", "逆方向・真逆の動作が起きたら？"),
    ("Other than / Instead of", "全く別のものに置き換わったら？"),
    ("Early", "想定より早く起きたら？"),
    ("Late", "想定より遅く起きたら？"),
    ("Before", "前工程より前に発火したら？"),
    ("After", "後続が先に終わったら？"),
]


def _load_env() -> None:
    """Load .env key=value pairs into os.environ (existing env wins)."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _slug(text: str, max_len: int = 40) -> str:
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    s = re.sub(r"[^\w\-]+", "_", text.strip())[:max_len].strip("_")
    return f"{s or 'task'}_{h}"


def _build_prompt(task: str, files: list[str], context: str) -> str:
    gw_lines = "\n".join(f"- {w}: {hint}" for w, hint in HAZOP_GUIDE_WORDS)
    files_block = "\n".join(f"- {f}" for f in files) if files else "(なし)"
    return f"""あなたは Sora Lab の premortem 分析官です。Gary Klein の premortem 手法と HAZOP guide words、
ACH (Analysis of Competing Hypotheses) を適用し、以下のタスクを「既に致命的に失敗した」前提で原因を列挙してください。

# タスク
{task}

# 対象ファイル
{files_block}

# 追加コンテキスト
{context or "(なし)"}

# 出力要件（厳格・省略禁止）

## A. 致命的失敗シナリオ 10 件（必ず 10 件）
各シナリオは次の JSON フィールドを埋めること:
  - id: F01..F10
  - title: 1行
  - scenario: 何が起きてどう失敗するか 2-4 文
  - probability: low / medium / high
  - impact: low / medium / high / catastrophic
  - detection: どう検知できるか
  - mitigation: 事前対策・再発防止策

## B. HAZOP guide words 適用 (以下すべてに1件以上当てはめる)
{gw_lines}

各 guide word について {{word, risk, mitigation}} を返す。

## C. 競合仮説 (ACH)
「このタスクは本当に意図通り動く」という主仮説に対し、
反証可能な競合仮説を最低 3 件挙げ、
各仮説の {{hypothesis, evidence_for, evidence_against, test}} を返す。

## D. 総合判定
  - overall_risk: low / medium / high / critical
  - go_no_go: GO / CONDITIONAL_GO / NO_GO
  - top3_blockers: 上記から最重要 3 件の id を抜粋
  - required_gates: 実装前に必ずクリアすべき条件の配列

# 出力フォーマット
純粋な JSON オブジェクト 1 個のみ。マークダウンや説明文なし。
ルート: {{ "scenarios": [...], "hazop": [...], "competing_hypotheses": [...], "judgment": {{...}} }}
"""


def _call_haiku(prompt: str, api_key: str) -> dict[str, Any]:
    payload = json.dumps({
        "model": HAIKU_MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
        resp = json.loads(r.read())
    text = "".join(
        part.get("text", "")
        for part in resp.get("content", [])
        if part.get("type") == "text"
    ).strip()
    # Haiku sometimes wraps JSON in ```json fences
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"no JSON object found in Haiku response: {text[:200]}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------------------
# Category-specific failure mode templates (keyword-based dynamic fallback)
# キーワード分類で fallback テンプレートをタスク固有に切替。LLM call なし。
# 新カテゴリ追加: CATEGORY_TEMPLATES に dict を追記するだけ。
# ---------------------------------------------------------------------------
CATEGORY_TEMPLATES: dict[str, dict] = {
    "web_api": {
        "keywords": ["webhook", "api", "endpoint", "http", "request", "rest",
                     "post", "curl", "bearer", "token", "oauth"],
        "scenarios": [
            ("Bearer トークン期限切れで全リクエスト401", "high", "catastrophic"),
            ("エンドポイントURL変更で silent 404", "medium", "high"),
            ("レート制限429で発注ループが詰まる", "medium", "high"),
            ("タイムアウト未設定でスレッドが永久ブロック", "medium", "high"),
            ("リプレイ攻撃でべき等でない操作が二重実行", "low", "catastrophic"),
            ("TLS証明書期限切れで接続拒否", "low", "high"),
            ("JSONレスポンスのフィールド名変更で KeyError", "medium", "high"),
            ("ネットワーク瞬断でリトライなし→発注欠落", "medium", "high"),
        ],
        "top3": ["F01", "F04", "F05"],
        "gates": ["Auth smoke test (401確認)", "Retry/timeout 設定確認",
                  "冪等性・べき等ガード実装"],
    },
    "test_pytest": {
        "keywords": ["test", "pytest", "unittest", "mock", "fixture",
                     "assert", "coverage", "flaky", "ci", "redteam"],
        "scenarios": [
            ("flaky test がCI環境のみ失敗し本番バグを隠蔽", "medium", "high"),
            ("mock が実APIと乖離し false positive 量産", "high", "high"),
            ("env 依存変数が未設定でテスト全スキップ", "medium", "high"),
            ("テストが自分のコードをimportせず常に合格", "low", "catastrophic"),
            ("coverage 100%でも統合パスが未カバー", "medium", "high"),
            ("並列テストで共有ファイルに race condition", "low", "high"),
            ("fixture のティアダウンが漏れてDB/ファイル汚染", "medium", "medium"),
        ],
        "top3": ["F01", "F02", "F04"],
        "gates": ["実モジュールimport確認", "mock/実API乖離チェック",
                  "env vars CI設定確認"],
    },
    "hook_precommit": {
        "keywords": ["hook", "pre-commit", "precommit", "pre_commit",
                     "git hook", "shell", "bash", "exit code", "guard"],
        "scenarios": [
            ("exit code 非ゼロでコミット全ブロック (誤爆)", "high", "high"),
            ("hookが stdoutに大量出力しタイムアウト", "medium", "medium"),
            ("hookが CI環境の PATH 相違で not found", "medium", "high"),
            ("--no-verify でバイパスされ規律が機能しない", "low", "catastrophic"),
            ("hook内で python -c が構文エラーで常時ブロック", "medium", "high"),
            ("hook の実行権限 (chmod +x) 未設定", "low", "high"),
            ("hook が別hookを再帰呼び出しして無限ループ", "low", "high"),
        ],
        "top3": ["F01", "F04", "F05"],
        "gates": ["dry-run で誤爆確認", "CI環境PATH検証",
                  "--no-verify バイパス監視設計"],
    },
    "memory_migration": {
        "keywords": ["memory", "migration", "migrate", "backup", "schema",
                     "database", "data", "json", "state", "restore"],
        "scenarios": [
            ("移行スクリプトで元データを上書き・バックアップなし", "medium", "catastrophic"),
            ("スキーマバージョン不整合で読み込み silent fail", "high", "high"),
            ("部分移行後にプロセスがクラッシュし中途半端な状態", "medium", "high"),
            ("ロールバック手順未定義で旧バージョンに戻せない", "low", "catastrophic"),
            ("文字コード (UTF-8/Shift-JIS) 変換で文字化け", "low", "medium"),
            ("大容量データで移行タイムアウト→中断", "low", "high"),
            ("並行アクセス中の移行でデータ破損", "medium", "catastrophic"),
            ("移行後の整合性チェックなしで破損データが本番流入", "medium", "high"),
        ],
        "top3": ["F01", "F04", "F07"],
        "gates": ["バックアップ取得確認", "ロールバック手順文書化",
                  "移行後整合性チェック自動化"],
    },
    "strategy_trading": {
        "keywords": ["strategy", "trading", "trade", "order", "position",
                     "option", "spy", "spx", "vix", "delta", "hedge",
                     "pdt", "margin", "drawdown", "bot", "atlas", "chronos"],
        "scenarios": [
            ("PDT違反で口座が90日ロック", "medium", "catastrophic"),
            ("資金管理ロジックのバグで証拠金超過発注", "low", "catastrophic"),
            ("DD上限超過後もエントリー継続で連鎖損失", "low", "catastrophic"),
            ("市場クローズ直前に発注→約定できずポジション持ち越し", "medium", "high"),
            ("VIX急騰時に戦術切替が遅延し最悪タイミングで発注", "medium", "high"),
            ("二重発注防止ロジックのバグで同一シグナルを複数回発注", "low", "catastrophic"),
            ("fill 確認なしで損切注文が未執行のまま放置", "medium", "high"),
            ("タイムゾーン誤りで場外に発注ループが走る", "low", "high"),
        ],
        "top3": ["F01", "F02", "F06"],
        "gates": ["PDT制約チェック実装確認", "証拠金超過ガード確認",
                  "DD上限 kill switch 動作確認"],
    },
    "deploy_vps": {
        "keywords": ["deploy", "vps", "ssh", "scp", "service", "systemd",
                     "launchd", "plist", "daemon", "restart", "infra"],
        "scenarios": [
            ("デプロイ先に旧バージョンが残存し新旧混在で動作", "medium", "high"),
            ("systemd service が ExecStart パス誤りで即死", "high", "high"),
            ("SSH key 権限が 644 で認証失敗", "medium", "high"),
            ("デプロイ後の動作確認なしで本番バグ放置", "high", "catastrophic"),
            ("旧プロセスが残留して二重起動", "medium", "high"),
            ("ログディレクトリ未作成でサービス起動失敗", "medium", "medium"),
            ("環境変数 .env 未転送で本番 API key なし", "medium", "catastrophic"),
        ],
        "top3": ["F01", "F04", "F07"],
        "gates": ["デプロイ後 status 確認", "旧プロセス停止確認",
                  ".env 転送確認"],
    },
}

# Default fallback (original behavior)
_DEFAULT_TEMPLATES = [
    ("依存サービス未起動でフェイル", "high", "catastrophic"),
    ("API rate limit / auth 失敗", "medium", "high"),
    ("データ型/スキーマ不整合で silent fail", "medium", "high"),
    ("タイムゾーン混在 (JST/ET/UTC)", "medium", "high"),
    ("並行実行で race condition", "low", "high"),
    ("ディスク/メモリ枯渇", "low", "high"),
    ("冬時間/夏時間境界で off-by-one", "low", "medium"),
    ("既存ファイル破壊・未バックアップ", "medium", "catastrophic"),
    ("テスト不足で本番初回に発覚", "high", "high"),
    ("roll-back 手順未定義で復旧不能", "low", "catastrophic"),
]
_DEFAULT_TOP3 = ["F01", "F08", "F10"]
_DEFAULT_GATES = ["事前バックアップ", "smoke test 実施", "roll-back 手順文書化"]


def _classify_task(task: str) -> str | None:
    """タスク文字列のキーワードでカテゴリを判定。最初にマッチしたカテゴリを返す。"""
    task_lower = task.lower()
    for category, cfg in CATEGORY_TEMPLATES.items():
        if any(kw in task_lower for kw in cfg["keywords"]):
            return category
    return None


def _fallback_report(task: str, files: list[str], reason: str) -> dict[str, Any]:
    """API未設定・失敗時の決定論的骨組み。タスクキーワードで固有テンプレートを選択する。"""
    category = _classify_task(task)
    if category and category in CATEGORY_TEMPLATES:
        cfg = CATEGORY_TEMPLATES[category]
        raw_templates = cfg["scenarios"]
        top3 = cfg["top3"]
        gates = cfg["gates"]
        category_label = category
    else:
        raw_templates = _DEFAULT_TEMPLATES  # type: ignore[assignment]
        top3 = _DEFAULT_TOP3
        gates = _DEFAULT_GATES
        category_label = "default"

    scenarios = []
    for i, item in enumerate(raw_templates, start=1):
        title, p, impact = item
        scenarios.append({
            "id": f"F{i:02d}",
            "title": title,
            "scenario": f"{task[:60]}... に対し『{title}』が発生し、復旧不能または無音で不正動作する。",
            "probability": p,
            "impact": impact,
            "detection": "ログ監視 / smoke test / assert",
            "mitigation": "事前バックアップ・DRY_RUN・pre-check・段階リリース",
        })

    hazop = [{"word": w, "risk": f"{w}適用時の逸脱", "mitigation": hint}
             for w, hint in HAZOP_GUIDE_WORDS]
    return {
        "scenarios": scenarios,
        "hazop": hazop,
        "competing_hypotheses": [
            {"hypothesis": "実装は意図通り動く",
             "evidence_for": "仕様書通り", "evidence_against": "本番未検証",
             "test": "smoke test"},
            {"hypothesis": "隠れた副作用がある",
             "evidence_for": "既存コードとの結合部", "evidence_against": "なし",
             "test": "既存回帰テスト"},
            {"hypothesis": "前提条件が既に壊れている",
             "evidence_for": "依存サービスの稼働状況不明",
             "evidence_against": "直近稼働ログあり", "test": "依存 healthcheck"},
        ],
        "judgment": {
            "overall_risk": "medium",
            "go_no_go": "CONDITIONAL_GO",
            "top3_blockers": top3,
            "required_gates": gates,
        },
        "_fallback_reason": reason,
        "_category": category_label,
    }


def _render_markdown(task: str, files: list[str], data: dict[str, Any],
                     *, source: str, generated_at: str) -> str:
    out: list[str] = []
    out.append(f"# Premortem Report")
    out.append("")
    out.append(f"- **Generated**: {generated_at}")
    out.append(f"- **Source**: {source}")
    out.append(f"- **Task**: {task}")
    if files:
        out.append(f"- **Files**: " + ", ".join(files))
    judgment = data.get("judgment", {})
    out.append(f"- **Overall Risk**: {judgment.get('overall_risk', 'n/a')}")
    out.append(f"- **GO/NO-GO**: {judgment.get('go_no_go', 'n/a')}")
    top3 = judgment.get("top3_blockers", [])
    if top3:
        out.append(f"- **Top3 Blockers**: " + ", ".join(top3))
    gates = judgment.get("required_gates", [])
    if gates:
        out.append(f"- **Required Gates**:")
        for g in gates:
            out.append(f"  - {g}")
    out.append("")

    out.append("## A. 致命的失敗シナリオ")
    out.append("")
    out.append("| id | title | prob | impact | detection | mitigation |")
    out.append("|---|---|---|---|---|---|")
    for s in data.get("scenarios", []):
        out.append("| {id} | {title} | {probability} | {impact} | {detection} | {mitigation} |".format(
            id=s.get("id", "?"),
            title=str(s.get("title", "")).replace("|", "/"),
            probability=s.get("probability", ""),
            impact=s.get("impact", ""),
            detection=str(s.get("detection", "")).replace("|", "/"),
            mitigation=str(s.get("mitigation", "")).replace("|", "/"),
        ))
    out.append("")
    out.append("### 詳細")
    for s in data.get("scenarios", []):
        out.append(f"- **{s.get('id','?')} {s.get('title','')}**: {s.get('scenario','')}")
    out.append("")

    out.append("## B. HAZOP Guide Words")
    out.append("")
    for h in data.get("hazop", []):
        out.append(f"- **{h.get('word','?')}**: risk={h.get('risk','')} / mitigation={h.get('mitigation','')}")
    out.append("")

    out.append("## C. Competing Hypotheses (ACH)")
    out.append("")
    for i, h in enumerate(data.get("competing_hypotheses", []), start=1):
        out.append(f"### H{i}. {h.get('hypothesis','')}")
        out.append(f"- evidence_for: {h.get('evidence_for','')}")
        out.append(f"- evidence_against: {h.get('evidence_against','')}")
        out.append(f"- test: {h.get('test','')}")
    out.append("")

    if "_fallback_reason" in data:
        out.append("## ⚠ Fallback Notice")
        out.append("")
        out.append(f"Haiku API 未使用。理由: `{data['_fallback_reason']}`")
        out.append("骨組みテンプレートのみ。API key を設定して再実行推奨。")
        out.append("")

    out.append("---")
    out.append("_Gary Klein premortem + HAZOP + ACH, Sora Lab_")
    return "\n".join(out) + "\n"


def run(task: str, files: list[str], context: str,
        *, out_path: Path | None = None,
        force_fallback: bool = False) -> tuple[Path, dict[str, Any], str]:
    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_KEY", "")
    source = "haiku"
    data: dict[str, Any]
    if force_fallback or not api_key:
        data = _fallback_report(task, files,
                                reason="no_api_key" if not api_key else "forced")
        source = "fallback"
    else:
        prompt = _build_prompt(task, files, context)
        try:
            data = _call_haiku(prompt, api_key)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as e:
            data = _fallback_report(task, files, reason=f"api_error:{type(e).__name__}")
            source = "fallback"

    now = datetime.datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    generated_at = now.isoformat(timespec="seconds")
    if out_path is None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORT_DIR / f"{stamp}_{_slug(task)}.md"
    md = _render_markdown(task, files, data, source=source, generated_at=generated_at)
    out_path.write_text(md, encoding="utf-8")

    # sidecar JSON for programmatic consumption (gate.sh etc)
    json_path = out_path.with_suffix(".json")
    json_path.write_text(json.dumps({
        "task": task,
        "files": files,
        "generated_at": generated_at,
        "source": source,
        "data": data,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, data, source


def main() -> int:
    ap = argparse.ArgumentParser(description="Gary Klein premortem analyser")
    ap.add_argument("--task", help="task description")
    ap.add_argument("--files", default="", help="comma-separated target files")
    ap.add_argument("--context", default="", help="optional extra context")
    ap.add_argument("--out", default="", help="explicit output .md path")
    ap.add_argument("--json", action="store_true", help="print JSON result to stdout")
    ap.add_argument("--selftest", action="store_true",
                    help="run without calling API (fallback only)")
    args = ap.parse_args()

    if args.selftest:
        task = args.task or "selftest: add new tactic to builder"
        files = [f for f in args.files.split(",") if f]
        p, data, source = run(task, files, args.context, force_fallback=True)
        print(f"OK selftest source={source} report={p}")
        return 0

    if not args.task:
        print("ERR --task required", file=sys.stderr)
        return 1

    files = [f for f in args.files.split(",") if f]
    out_path = Path(args.out) if args.out else None
    p, data, source = run(args.task, files, args.context, out_path=out_path)

    if args.json:
        print(json.dumps({"report": str(p), "source": source, "data": data},
                         ensure_ascii=False))
    else:
        judgment = data.get("judgment", {})
        print(f"premortem: report={p}")
        print(f"  source={source}")
        print(f"  risk={judgment.get('overall_risk')}  decision={judgment.get('go_no_go')}")
        print(f"  scenarios={len(data.get('scenarios', []))}")
        print(f"  top3={judgment.get('top3_blockers')}")
    return 0 if source == "haiku" else 2


if __name__ == "__main__":
    sys.exit(main())
