#!/usr/bin/env python3
"""
scripts/claude_night_watcher.py — Claude Night Watcher (Haiku LLM-based)

LaunchAgent が 15 分毎に起動し、Bot 状態を Haiku で読解。
異常を検知したら Pushover でエスカレーション。

稼働時間: JST 22:00-05:00 (StartCalendarInterval で制御)
コスト: ~$0.002/回, ~$1.8/月 (15分×8h×30日)

出力: data/ops/night_watcher_verdicts.jsonl
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import traceback
from pathlib import Path

# プロジェクトルートを sys.path に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
    sys.exit(1)

from common.pushover_client import send as pushover_send


def _load_dotenv() -> None:
    """PROJECT_ROOT/.env から環境変数を読み込む（python-dotenv 不要）。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

# ---- 定数 ----
MODEL_HAIKU = "claude-haiku-4-5-20251001"
VERDICT_LOG = PROJECT_ROOT / "data" / "ops" / "night_watcher_verdicts.jsonl"
MAX_LOG_LINES = 100
MAX_CHRONOS_LINES = 50

LOG_PATHS = {
    "spybot": PROJECT_ROOT / "data" / "logs" / "spybot_stdout.log",
    "chronos_watchdog": PROJECT_ROOT / "data" / "logs" / "chronos_watchdog_stdout.log",
    "ground_truth": PROJECT_ROOT / "data" / "logs" / "ground_truth_reconciler.log",
    "dead_man": PROJECT_ROOT / "logs" / "dead_man_switch.log",
}
DEAD_MAN_PING = PROJECT_ROOT / "dead_man_ping.jsonl"

SYSTEM_PROMPT = """あなたはトレーディングBotシステムの監視エージェントです。
Bot のログを読み、異常があれば JSON で報告します。

判定ルール:
- anomaly=false: 通常運転・定型的なログのみ
- severity=low: 警告だが自己回復中 / 軽微なエラー
- severity=high: 複数エラー継続 / 発注失敗 / 認証問題
- severity=critical: Bot停止 / 証拠金危機 / 死活ping途絶

必ず以下の JSON のみ返してください（説明文不要）:
{
  "anomaly": true/false,
  "severity": "low" | "high" | "critical",
  "summary": "50字以内の日本語要約",
  "recommended_action": "30字以内の対応指示"
}"""


def _tail_lines(path: Path, n: int) -> str:
    """ファイルの末尾 n 行を返す。ファイルなければ空文字。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:]) if lines else "(empty)"
    except FileNotFoundError:
        return f"(file not found: {path.name})"
    except Exception as e:
        return f"(read error: {e})"


def _get_dead_man_last_ping() -> str:
    """dead_man_ping.jsonl の最終 ping 時刻を返す。"""
    try:
        lines = DEAD_MAN_PING.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return "(no ping data)"
        last = json.loads(lines[-1])
        return last.get("ts", last.get("timestamp", str(last)))
    except FileNotFoundError:
        return "(ping file not found)"
    except Exception as e:
        return f"(parse error: {e})"


def build_context() -> str:
    """LLM に渡すコンテキスト文字列を構築する。"""
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    sections = [
        f"## 現在時刻 (JST): {now_jst.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"## spybot_stdout.log (直近 {MAX_LOG_LINES} 行)",
        _tail_lines(LOG_PATHS["spybot"], MAX_LOG_LINES),
        "",
        f"## chronos_watchdog_stdout.log (直近 {MAX_CHRONOS_LINES} 行)",
        _tail_lines(LOG_PATHS["chronos_watchdog"], MAX_CHRONOS_LINES),
        "",
        "## ground_truth_reconciler.log (直近 30 行)",
        _tail_lines(LOG_PATHS["ground_truth"], 30),
        "",
        "## dead_man_switch.log (直近 10 行)",
        _tail_lines(LOG_PATHS["dead_man"], 10),
        "",
        f"## dead_man 最終 ping 時刻: {_get_dead_man_last_ping()}",
    ]
    return "\n".join(sections)


def call_haiku(context: str) -> dict:
    """Haiku API を呼び出し、判定 JSON を返す。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が環境変数に設定されていない")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"以下の Bot ログを分析し、JSON のみ返してください:\n\n{context}",
            }
        ],
    )
    raw = message.content[0].text.strip()
    # JSON 部分だけ抽出（前後のマークダウン等を除去）
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def escalate(verdict: dict, context_summary: str) -> None:
    """severity に応じて Pushover エスカレーション。"""
    severity = verdict.get("severity", "low")
    summary = verdict.get("summary", "不明")
    action = verdict.get("recommended_action", "確認してください")

    if severity == "critical":
        priority = 2
        title = "[Atlas][CRITICAL] Night Watcher 緊急アラート"
    elif severity == "high":
        priority = 1
        title = "[Atlas][HIGH] Night Watcher 異常検知"
    else:
        priority = 0
        title = "[Atlas][LOW] Night Watcher 軽微警告"

    msg = f"{summary}\n対応: {action}"
    pushover_send(
        title,
        msg,
        priority=priority,
    )


def write_verdict(verdict: dict, context_len: int, model: str, error: str | None = None) -> None:
    """判定結果を JSONL に追記する。"""
    VERDICT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model": model,
        "verdict": verdict,
        "context_chars": context_len,
        "error": error,
    }
    with VERDICT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    """メインエントリーポイント。0=正常, 1=エラー。"""
    print(f"[NightWatcher] start {datetime.datetime.now().isoformat()}", flush=True)

    try:
        context = build_context()
        context_len = len(context)
        print(f"[NightWatcher] context built: {context_len} chars", flush=True)

        verdict = call_haiku(context)
        print(f"[NightWatcher] verdict: {json.dumps(verdict, ensure_ascii=False)}", flush=True)

        write_verdict(verdict, context_len, MODEL_HAIKU)

        if verdict.get("anomaly", False):
            escalate(verdict, context[:200])
            print(f"[NightWatcher] escalated severity={verdict.get('severity')}", flush=True)
        else:
            print("[NightWatcher] no anomaly detected", flush=True)

        return 0

    except Exception as e:
        err_msg = traceback.format_exc()
        print(f"[NightWatcher] ERROR: {e}\n{err_msg}", file=sys.stderr, flush=True)
        # エラー自体も記録
        write_verdict({}, 0, MODEL_HAIKU, error=str(e))
        # watcher 自身のエラーは high でエスカレーション
        try:
            pushover_send(
                "[Atlas][HIGH] Night Watcher 実行エラー",
                f"エラー: {str(e)[:100]}",
                priority=1,
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
