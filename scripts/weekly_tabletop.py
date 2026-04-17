#!/usr/bin/env python3
"""Weekly Tabletop Exercise (防災訓練) — 日曜 18:00 JST 自動実行

Claude haiku を呼び出し、現在のBot/インフラ状態をベースに
最悪10シナリオを生成。各シナリオに想定対応を記載。

出力: data/tabletop_YYYYMMDD.md
通知: Pushover [SYS/TABLETOP]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from urllib import parse, request

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
LOG_DIR = DATA / "logs"

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
# System context collector
# ---------------------------------------------------------------------------

def collect_system_context() -> dict:
    """現在のBot/インフラ状態を収集してコンテキスト化"""
    ctx: dict = {}

    # condor_pnl.json から最近の戦術・P&Lサマリー
    pnl_file = DATA / "condor_pnl.json"
    if pnl_file.exists():
        with open(pnl_file) as f:
            raw = json.load(f)
        trades = raw.get("trades", [])
        exits = [t for t in trades if t.get("event") == "exit" and t.get("pnl_usd") is not None]
        pnl_values = [t["pnl_usd"] for t in exits[-20:]]  # 直近20件
        ctx["recent_pnl_count"] = len(pnl_values)
        ctx["recent_pnl_sum"] = sum(pnl_values) if pnl_values else 0.0
        ctx["recent_pnl_avg"] = (sum(pnl_values) / len(pnl_values)) if pnl_values else 0.0

        # 戦術バリエーション
        tactics = list({t.get("tactic", "unknown") for t in trades if t.get("event") == "entry"})
        ctx["tactics_used"] = tactics

    # atlas_state.json
    state_file = DATA / "atlas_state.json"
    if state_file.exists():
        with open(state_file) as f:
            state = json.load(f)
        ctx["atlas_state"] = state

    # spx_condor_failures.json
    fail_file = DATA / "spx_condor_failures.json"
    if fail_file.exists():
        with open(fail_file) as f:
            failures = json.load(f)
        ctx["failure_count"] = len(failures) if isinstance(failures, list) else 0

    # 最近のAAR ファイルがあれば取り込む
    aar_files = sorted(DATA.glob("aar_*.md"))
    if aar_files:
        ctx["latest_aar"] = aar_files[-1].name

    return ctx


# ---------------------------------------------------------------------------
# Scenario generation (haiku)
# ---------------------------------------------------------------------------

FALLBACK_SCENARIOS = [
    {
        "id": "S01",
        "title": "moomoo API 認証切れ・全発注不能",
        "probability": "medium",
        "impact": "catastrophic",
        "description": "AppKey/SecretのTTL切れ、またはIPブロックによりOpenD接続が失われる",
        "response": "① /root/spxbot/spx_bot.py の認証情報を更新 ② openD再起動 ③ DRY_RUN=1 で接続確認 ④ Pushover報告",
    },
    {
        "id": "S02",
        "title": "VPS障害・Bot完全停止",
        "probability": "low",
        "impact": "catastrophic",
        "description": "Vultr VPS 198.13.37.17 が無応答になりBot全停止",
        "response": "① Vultrコンソールで電源リセット ② SSH復旧確認 ③ systemctl start apex_bot ④ watchdog.py 確認",
    },
    {
        "id": "S03",
        "title": "VIX急騰50超・全ポジション強制損切り連発",
        "probability": "low",
        "impact": "high",
        "description": "ブラックスワン的VIX急騰でintraday_crisis多発、1日で5%以上のドローダウン",
        "response": "① DRY_RUN=1 に切替 ② スプレッド幅・qty上限を半減 ③ crisis閾値パラメータ見直し ④ 翌日環境確認後に再開",
    },
    {
        "id": "S04",
        "title": "Pushover通知サービス障害・無音障害",
        "probability": "medium",
        "impact": "medium",
        "description": "Pushoverが無応答になり、Botの障害を検知できない状態が続く",
        "response": "① ntfy.sh を代替通知チャンネルとして即時切替 ② GitHub Issue経由で状態確認 ③ Pushover API status確認",
    },
    {
        "id": "S05",
        "title": "Condor P&L JSONの破損・データ消失",
        "probability": "low",
        "impact": "high",
        "description": "condor_pnl.json が並行書き込みで破損し、AAR・分析が全て不正確になる",
        "response": "① .bak ファイルから最新バックアップを復元 ② JSONスキーマ検証スクリプト実行 ③ ファイルロック実装をBuilderに依頼",
    },
    {
        "id": "S06",
        "title": "夏時間→冬時間切替でBot発注時刻ズレ",
        "probability": "medium",
        "impact": "medium",
        "description": "11月第一日曜にET/JSTオフセットが+13→+14に変わり、LaunchAgent時刻がズレる",
        "response": "① plist の Hour 値を+1補正 ② launchctl unload/load で再登録 ③ 翌日の実発注ログでズレ確認",
    },
    {
        "id": "S07",
        "title": "Anthropic API レートリミット・haiku停止",
        "probability": "medium",
        "impact": "low",
        "description": "AAR/Tabletop が haiku 呼び出しでレートリミットに当たり生成失敗",
        "response": "① fallback（ルールベース提案）で継続 ② ANTHROPIC_API_KEY 使用量確認 ③ 指数バックオフでリトライ",
    },
    {
        "id": "S08",
        "title": "moomooデモ→本番誤爆（DRY_RUN未設定）",
        "probability": "low",
        "impact": "catastrophic",
        "description": "環境変数 DRY_RUN が未設定のままスクリプトが本番モードで起動し実注文が入る",
        "response": "① 直ちにBot停止 ② moomooアプリで手動ポジションクローズ ③ 環境変数チェックをBot起動前 assertion に追加",
    },
    {
        "id": "S09",
        "title": "GitHub Actions ワークフロー停止・hub_relay無効化",
        "probability": "low",
        "impact": "medium",
        "description": "GitHubのActionsクォータ超過またはシークレット期限切れでhub_relayが動かない",
        "response": "① SSH直接接続でコマンド実行 ② GitHub Secretsの期限確認・更新 ③ ntfy.sh経由のバックアップ経路を利用",
    },
    {
        "id": "S10",
        "title": "Mac電源断・LaunchAgent全停止",
        "probability": "medium",
        "impact": "high",
        "description": "ローカルMacが停電・スリープ等でLaunchAgentが全停止し、朝のAAR・分析が未実行",
        "response": "① 電源アダプタ常時接続確認 ② スリープ無効設定(caffeinateコマンド) ③ 重要スクリプトをVPSに移行検討",
    },
]


def generate_scenarios_with_haiku(ctx: dict, today: date) -> list[dict]:
    """Claude haiku でシナリオを生成。失敗時はfallback使用"""
    if not ANTHROPIC_API_KEY:
        print("[tabletop] ANTHROPIC_API_KEY未設定 → fallbackシナリオ使用", flush=True)
        return FALLBACK_SCENARIOS

    context_summary = json.dumps(ctx, ensure_ascii=False, indent=2)

    prompt = f"""あなたはSPX 0DTE自動売買ボット(Sora Lab)のリスク管理担当です。
以下のシステム状態を分析し、今後1週間で発生しうる最悪シナリオを10件、
JSON配列形式で返してください。

# システム状態
{context_summary}

# 既知の構成
- Mac上のPythonボット (spx_bot.py / apex_bot)
- VPS 198.13.37.17 (hub_agent, webhook_server, ntfy_listener, cloudflared)
- moomoo OpenD API (AppKey認証)
- Anthropic Claude API (haiku呼び出し)
- LaunchAgent (com.atlas.*)
- データ: condor_pnl.json, condor.log, aar_*.md

# 出力形式 (JSON配列、10件固定)
[
  {{
    "id": "S01",
    "title": "短いタイトル",
    "probability": "low/medium/high",
    "impact": "low/medium/high/catastrophic",
    "description": "具体的な障害内容（1〜2文）",
    "response": "対応手順（① ② ③ の番号付きステップ）"
  }},
  ...
]

JSON配列のみ返してください。説明文不要。"""

    try:
        import urllib.request
        import json as _json

        payload = _json.dumps({
            "model": "claude-haiku-4-5",
            "max_tokens": 2048,
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = _json.loads(resp.read())
            text = result["content"][0]["text"].strip()

        # JSON抽出（```json ... ``` ブロック対応）
        import re
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            scenarios = _json.loads(json_match.group(0))
            if isinstance(scenarios, list) and len(scenarios) > 0:
                print(f"[tabletop] haiku生成: {len(scenarios)}件のシナリオ", flush=True)
                return scenarios

        print("[tabletop] haiku応答のJSONパース失敗 → fallback使用", flush=True)
        return FALLBACK_SCENARIOS

    except Exception as e:
        print(f"[tabletop] haiku呼び出しエラー: {e} → fallback使用", file=sys.stderr)
        return FALLBACK_SCENARIOS


# ---------------------------------------------------------------------------
# Main tabletop generation
# ---------------------------------------------------------------------------

def run_tabletop(target_date: date | None = None) -> Path:
    if target_date is None:
        target_date = datetime.now().date()

    date_str = target_date.strftime("%Y%m%d")
    out_path = DATA / f"tabletop_{date_str}.md"

    print(f"[TABLETOP] Generating for {target_date.isoformat()} ...", flush=True)

    ctx = collect_system_context()
    scenarios = generate_scenarios_with_haiku(ctx, target_date)

    # --- Markdown 出力 ---
    lines_out = [
        f"# Weekly Tabletop Exercise — {target_date.isoformat()}",
        f"",
        f"**生成日時**: {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} JST",
        f"**生成方法**: {'Claude haiku-4-5' if ANTHROPIC_API_KEY else 'Fallback (ルールベース)'}",
        f"",
        f"---",
        f"",
        f"## システム状態サマリー",
        f"",
        f"| 項目 | 値 |",
        f"|---|---|",
        f"| 直近P&L件数 | {ctx.get('recent_pnl_count', 'N/A')} |",
        f"| 直近P&L合計 | ${ctx.get('recent_pnl_sum', 'N/A'):.2f} |",
        f"| 使用戦術 | {ctx.get('tactics_used', 'N/A')} |",
        f"| 障害件数 | {ctx.get('failure_count', 'N/A')} |",
        f"| 最新AAR | {ctx.get('latest_aar', 'なし')} |",
        f"",
        f"---",
        f"",
        f"## 最悪シナリオ 10件",
        f"",
    ]

    for i, s in enumerate(scenarios[:10], 1):
        sid = s.get("id", f"S{i:02d}")
        title = s.get("title", "不明")
        prob = s.get("probability", "?")
        impact = s.get("impact", "?")
        desc = s.get("description", "")
        resp = s.get("response", "対応手順未定義")

        lines_out += [
            f"### {sid}. {title}",
            f"",
            f"- **発生確率**: {prob}",
            f"- **影響度**: {impact}",
            f"",
            f"**シナリオ説明**",
            f"{desc}",
            f"",
            f"**想定対応**",
            f"{resp}",
            f"",
        ]

    lines_out += [
        f"---",
        f"",
        f"## 演習チェックリスト",
        f"",
        f"- [ ] 全10シナリオを読んだ",
        f"- [ ] S01〜S03（重大度高）の対応手順を確認した",
        f"- [ ] 未整備の対応手順を Issues に登録した",
        f"- [ ] 次週のTabletopまでに改善を1件以上実施する",
        f"",
        f"---",
        f"*Generated by scripts/weekly_tabletop.py (Sora Lab)*",
    ]

    out_path.write_text("\n".join(lines_out), encoding="utf-8")
    print(f"[TABLETOP] Written: {out_path}", flush=True)

    # --- Pushover 通知 ---
    s_titles = "\n".join(f"• {s.get('id','?')}: {s.get('title','?')}" for s in scenarios[:5])
    msg = (
        f"日付: {target_date.isoformat()}\n"
        f"シナリオ: {len(scenarios)}件生成\n\n"
        f"Top5:\n{s_titles}\n\n"
        f"詳細: data/tabletop_{date_str}.md"
    )
    send_pushover("[SYS/TABLETOP] Weekly Tabletop 完了", msg)

    return out_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Weekly Tabletop Exercise generator")
    parser.add_argument("--date", type=str, help="対象日 YYYY-MM-DD (省略時=今日)", default=None)
    args = parser.parse_args()

    target = None
    if args.date:
        target = date.fromisoformat(args.date)

    out = run_tabletop(target)
    print(f"[TABLETOP] Done: {out}")
