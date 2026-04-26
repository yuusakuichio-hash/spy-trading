#!/usr/bin/env python3
"""
check_pending_completions.py
時間帯可変 deadline を過ぎても resolved=false の pending エントリに対して:
1. Pushover priority=1 で通知
2. violation_registry.jsonl にエントリ追加
同一パターンの繰り返し回数が escalate_at 以上になると priority=2 (CRITICAL) で通知。

時間帯別 deadline（JST基準）:
  - 場中 (22:30-05:00 JST): 10分  ← Atlas+Chronos 同時稼働。62.4円/分の機会損失防止
  - 場外 (05:00-22:30 JST): 20分  ← Chronosのみ稼働
  - メンテ (06:00-07:00 JST): 60分 ← デプロイ作業時間帯を考慮
  - 週末 (土06:00-月07:00 JST): 120分 ← Bot非稼働・機会損失なし

LaunchAgent または cron から定期実行（15分ごと推奨）。
"""
import json, os, sys, hashlib, subprocess
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
PENDING_PATH = f"{BASE}/data/pending_completions.jsonl"
REGISTRY_PATH = f"{BASE}/data/violation_registry.jsonl"
PATTERNS_PATH = f"{BASE}/data/violation_patterns.json"
LOG_PATH = f"{BASE}/data/logs/discipline_violations.log"
JST = timezone(timedelta(hours=9))

# Pushover credentials - ~/.claude/agents/ から読み込む
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")

# credentials ファイルから読み込み試行
CRED_PATHS = [
    f"{BASE}/.claude/skills/credentials.md",
    os.path.expanduser("~/.claude/agents/credentials.md"),
]
for cp in CRED_PATHS:
    if os.path.exists(cp):
        try:
            with open(cp) as f:
                ctext = f.read()
            import re
            # PUSHOVER_API_TOKEN or PUSHOVER_TOKEN
            m = re.search(r"PUSHOVER_(?:API_)?TOKEN[:\s]+([a-zA-Z0-9_-]+)", ctext)
            if m and not PUSHOVER_TOKEN:
                PUSHOVER_TOKEN = m.group(1).strip()
            m2 = re.search(r"PUSHOVER_USER(?:_KEY)?[:\s]+([a-zA-Z0-9_-]+)", ctext)
            if m2 and not PUSHOVER_USER:
                PUSHOVER_USER = m2.group(1).strip()
        except Exception:
            pass
    if PUSHOVER_TOKEN and PUSHOVER_USER:
        break


def get_deadline_minutes(pattern_config: dict, now: datetime) -> int:
    """
    時間帯に応じた deadline 分数を返す（市場時間帯ではなく Bot 稼働時間帯で判定）。

    注意: common/market_calendar.py の「市場時間帯」関数とは別実装。
    こちらは Atlas+Chronos の稼働有無による機会損失の重み付けが目的。

    判定ロジック（JST基準）:
      週末（土06:00 〜 月07:00）  → deadline_minutes_weekend  (default: 120)
      メンテ（毎日 06:00-07:00）  → deadline_minutes_maintenance (default: 60)
      場中（22:30-05:00）          → deadline_minutes_market_hours (default: 10)
        analyst 推奨根拠: 場中は Atlas+Chronos 同時稼働。
        85パーセンタイルタスク完了時間が12分・違反検知遅延コストが62.4円/分。
        30分放置 = 1,872円/回の機会損失。10分が均衡点。
        escalate_at も場中は3回に引き上げ（誤発火ペナルティ軽減）。
      場外（05:00-22:30）          → deadline_minutes_daytime  (default: 20)
    """
    weekday = now.weekday()  # 0=月, 5=土, 6=日
    hour = now.hour
    minute = now.minute
    t = (hour, minute)

    # 週末判定（土06:00 〜 月07:00）
    if weekday == 5 and t >= (6, 0):
        return pattern_config.get("deadline_minutes_weekend", 120)
    if weekday == 6:
        return pattern_config.get("deadline_minutes_weekend", 120)
    if weekday == 0 and t < (7, 0):
        return pattern_config.get("deadline_minutes_weekend", 120)

    # メンテ（06:00-07:00 JST）— 週末範囲より後に判定
    if (6, 0) <= t < (7, 0):
        return pattern_config.get("deadline_minutes_maintenance", 60)

    # 場中（22:30-翌05:00 JST）= Atlas+Chronos 同時稼働
    if t >= (22, 30) or t < (5, 0):
        return pattern_config.get("deadline_minutes_market_hours", 10)

    # 場外（05:00-22:30 JST）= Chronos のみ
    return pattern_config.get("deadline_minutes_daytime", 20)


def get_escalate_at(pattern_config: dict, now: datetime) -> int:
    """
    時間帯に応じた escalate_at 閾値を返す。

    場中は誤発火によるノイズを抑えるため escalate_at を3に緩和
    （場中でも繰り返し3回目でCRITICAL昇格する点は同様）。
    場外はデフォルト2のまま。
    """
    hour = now.hour
    minute = now.minute
    t = (hour, minute)
    if t >= (22, 30) or t < (5, 0):
        return pattern_config.get("escalate_at_market_hours", 3)
    return pattern_config.get("escalate_at_default", 2)


def load_violation_patterns() -> dict:
    """violation_patterns.json を読み込む。失敗時はデフォルト値で継続。"""
    try:
        with open(PATTERNS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def log_event(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    with open(LOG_PATH, "a") as f:
        f.write(f"[CHECK_PENDING] {ts} {msg}\n")


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def save_jsonl(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def normalize_title(title):
    """類似度判定用の正規化"""
    import re
    # 日付・ハッシュ部分を除去
    title = re.sub(r"_?20\d{6}.*", "", title)
    title = re.sub(r"_?\d{8}.*", "", title)
    return title.lower()


def find_similar_in_registry(title, registry):
    """既存 registry から類似エントリを検索"""
    normalized = normalize_title(title)
    for e in registry:
        if normalize_title(e.get("title", "")) == normalized:
            return e
    return None


def send_pushover(title, message, priority=1):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log_event(f"PUSHOVER_SKIP: credentials not found. title={title}")
        return False
    try:
        cmd = [
            "curl", "-s", "-X", "POST",
            "https://api.pushover.net/1/messages.json",
            "-d", f"token={PUSHOVER_TOKEN}",
            "-d", f"user={PUSHOVER_USER}",
            "-d", f"title={title}",
            "-d", f"message={message}",
            "-d", f"priority={priority}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            log_event(f"PUSHOVER_SENT: {title}")
            return True
    except Exception as e:
        log_event(f"PUSHOVER_ERROR: {e}")
    return False


def main():
    now = datetime.now(JST)
    patterns = load_violation_patterns()
    mac_pattern = patterns.get("memory_as_completion", {})

    pending = load_jsonl(PENDING_PATH)
    registry = load_jsonl(REGISTRY_PATH)

    # 現在時刻に対応する escalate_at を取得（memory_as_completion パターン用）
    escalate_at = get_escalate_at(mac_pattern, now)

    updated_pending = []
    violations_found = 0

    for e in pending:
        if e.get("resolved", False):
            updated_pending.append(e)
            continue

        deadline_str = e.get("deadline_ts", "")
        try:
            # Python 3.7+ fromisoformat
            deadline = datetime.fromisoformat(deadline_str)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=JST)
        except Exception:
            updated_pending.append(e)
            continue

        if now <= deadline:
            # まだ期限内
            updated_pending.append(e)
            continue

        # 期限切れ・未解決
        violations_found += 1
        title = e.get("title", e.get("memory_path", "?"))

        similar = find_similar_in_registry(title, registry)
        if similar:
            similar["occurrence_count"] = similar.get("occurrence_count", 1) + 1
            similar["last_ts"] = now.isoformat()
            count = similar["occurrence_count"]
        else:
            new_entry = {
                "ts": now.isoformat(),
                "last_ts": now.isoformat(),
                "type": "memory_as_completion",
                "title": title,
                "memory_path": e.get("memory_path", ""),
                "fingerprint": e.get("fingerprint", ""),
                "occurrence_count": 1,
            }
            registry.append(new_entry)
            count = 1

        if count >= escalate_at:
            priority = 2
            push_title = f"[CRITICAL] REPEATED VIOLATION 第{count}回"
            push_msg = f"同一パターンが {count} 回繰り返されています。\n対象: {title}\nメモリ保存=完了ではない。コード実装commitが必要。"
        else:
            priority = 1
            push_title = "[ALERT] Memory-as-completion violation"
            # deadline がいつ設定されたか（登録時刻から計算）でメッセージを変える
            registered_ts = e.get("ts", "")
            try:
                reg_dt = datetime.fromisoformat(registered_ts)
                if reg_dt.tzinfo is None:
                    reg_dt = reg_dt.replace(tzinfo=JST)
                elapsed_min = int((now - reg_dt).total_seconds() / 60)
            except Exception:
                elapsed_min = 0
            # 時間帯別 deadline をメッセージに含める
            deadline_min = get_deadline_minutes(mac_pattern, now)
            push_msg = (
                f"{elapsed_min}分経過・コードcommitなし（時間帯deadline: {deadline_min}分）。\n"
                f"対象: {title}\n"
                f"メモリ保存だけでは対策完了ではない。実装してcommitせよ。"
            )

        send_pushover(push_title, push_msg, priority)
        log_event(f"VIOLATION_ESCALATED: {title} count={count} priority={priority} escalate_at={escalate_at}")

        # pending エントリは resolved=false のまま保持（Stop hook で検査継続）
        updated_pending.append(e)

    if violations_found > 0:
        save_jsonl(PENDING_PATH, updated_pending)
        save_jsonl(REGISTRY_PATH, registry)
        log_event(f"CHECK_COMPLETE: {violations_found} violations escalated")
    else:
        log_event("CHECK_COMPLETE: no expired pending entries")


if __name__ == "__main__":
    main()
