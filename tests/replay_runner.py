"""tests/replay_runner.py — APIコール記録の再生・修正版コード検証

Record-Replay検証の流れ:
  1. 本番/ペーパーでAPIRecorder.start_record()を有効化してBot稼働
  2. 記録ファイル(tests/recorded/session_*.jsonl)が生成される
  3. このスクリプトで記録を再生し、修正版コードが正しく動くか検証
  4. SHADOW比較モードでは本番結果と修正版結果をdiff出力

使い方:
    # 記録ファイルを指定して再生
    python3 tests/replay_runner.py --session tests/recorded/session_20260418_*.jsonl

    # Shadowモード: 本番と修正版を比較
    python3 tests/replay_runner.py --session <file> --shadow-compare

    # smoke test（記録→再生で同じ結果が出るか）
    python3 tests/replay_runner.py --smoke

    # 日次記録を一括再生
    python3 tests/replay_runner.py --date 20260418
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import textwrap
from pathlib import Path

# プロジェクトルートをsys.pathに追加
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from common.api_recorder import (
    APIRecorder,
    RecorderMode,
    ReplayExhaustedError,
    ReplayMethodMismatchError,
    get_recorder,
    set_recorder,
)

RECORDED_DIR = BASE / "tests" / "recorded"


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── Session情報表示 ────────────────────────────────────────────────

def inspect_session(path: Path) -> dict:
    """記録ファイルの内容を解析してサマリーを返す"""
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not entries:
        return {"path": str(path), "count": 0, "methods": {}}

    methods: dict[str, int] = {}
    exceptions = []
    first_ts = entries[0].get("ts", "")
    last_ts = entries[-1].get("ts", "")

    for e in entries:
        m = e.get("method", "unknown")
        methods[m] = methods.get(m, 0) + 1
        if "exception" in e:
            exceptions.append({"method": m, "exception": e["exception"]})

    return {
        "path": str(path),
        "count": len(entries),
        "methods": methods,
        "exceptions": exceptions,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_note": f"{first_ts[:16]} ~ {last_ts[:16]}" if first_ts and last_ts else "N/A",
    }


def cmd_inspect(args) -> None:
    paths = _resolve_paths(args)
    for p in paths:
        info = inspect_session(p)
        _log(f"=== {p.name} ===")
        _log(f"  エントリー数: {info['count']}")
        _log(f"  期間: {info['duration_note']}")
        _log(f"  メソッド別件数:")
        for m, cnt in sorted(info["methods"].items(), key=lambda x: -x[1]):
            _log(f"    {m}: {cnt}件")
        if info["exceptions"]:
            _log(f"  例外記録: {len(info['exceptions'])}件")
            for exc in info["exceptions"][:5]:
                _log(f"    {exc['method']}: {exc['exception']}")
        print()


# ── Smoke Test ─────────────────────────────────────────────────────

def cmd_smoke(args) -> bool:
    """smoke test: 記録→再生で同じ結果が出るか確認

    1. インメモリRecorderでダミーコールを記録
    2. 同じファイルで再生
    3. 戻り値が一致するか照合
    """
    _log("=== Smoke Test 開始 ===")
    recorder = APIRecorder(record_dir=RECORDED_DIR)

    # --- 記録フェーズ ---
    session_path = recorder.start_record("smoke_test")
    _log(f"記録先: {session_path}")

    test_cases = [
        ("get_market_snapshot", lambda codes: (0, [{"code": codes[0], "last_price": 560.12}]), (["US.SPY"],), {}),
        ("get_option_chain",    lambda code, expiry: (0, [{"code": f"{code}_{expiry}_C_560"}]),
                                ("US.SPY", "20260418"), {}),
        ("get_vix",             lambda: (0, [{"code": "US.VIX", "last_price": 18.5}]), (), {}),
    ]

    recorded_results = []
    for method, fn, fn_args, fn_kwargs in test_cases:
        result = recorder.call(method, fn, *fn_args, **fn_kwargs)
        recorded_results.append(result)
        _log(f"  記録: {method} → {type(result).__name__}")

    stats = recorder.stop()
    _log(f"記録完了: {stats['call_count']}コール")

    # --- 再生フェーズ ---
    recorder2 = APIRecorder(record_dir=RECORDED_DIR)
    count = recorder2.start_replay(session_path)
    _log(f"再生開始: {count}エントリー")

    replayed_results = []
    for method, fn, fn_args, fn_kwargs in test_cases:
        # real_fnは使われない（REPLAYモードなので記録値を返す）
        result = recorder2.call(method, fn, *fn_args, **fn_kwargs)
        replayed_results.append(result)
        _log(f"  再生: {method} → {type(result).__name__}")

    recorder2.stop()

    # --- 照合 ---
    passed = True
    for i, (method, _, _, _) in enumerate(test_cases):
        r = recorded_results[i]
        p = replayed_results[i]
        match = _results_match(r, p)
        icon = "PASS" if match else "FAIL"
        _log(f"  [{icon}] {method}: recorded={r!r} replayed={p!r}")
        if not match:
            passed = False

    # クリーンアップ
    try:
        session_path.unlink()
    except Exception:
        pass

    _log(f"=== Smoke Test {'PASSED' if passed else 'FAILED'} ===")
    return passed


def _results_match(a, b) -> bool:
    """記録と再生の結果が一致するか判定（型・値の比較）"""
    # tupleとlistは同値扱い（_make_serializableがlist変換するため）
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_results_match(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_results_match(a[k], b[k]) for k in a)
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 1e-9
    return a == b


# ── Shadow Compare ─────────────────────────────────────────────────

class ShadowResult:
    """Shadowモードの1コールの実行結果"""
    def __init__(self, method: str, baseline, modified, match: bool):
        self.method = method
        self.baseline = baseline
        self.modified = modified
        self.match = match


def run_shadow_compare(
    record_path: Path,
    modified_fn_map: dict[str, callable],
) -> list[ShadowResult]:
    """記録を使って本番コードと修正版コードを比較する。

    Args:
        record_path: 再生するJSONLファイルのパス
        modified_fn_map: {method_name: modified_function} の辞書
                         記録値（baseline）と修正版fn実行結果を比較する

    Returns:
        ShadowResult のリスト
    """
    recorder = APIRecorder(record_dir=RECORDED_DIR)
    count = recorder.start_replay(record_path)
    _log(f"Shadow比較: {record_path.name} ({count} エントリー)")

    results = []
    entries_by_method: dict[str, list] = {}

    # 記録ファイルを直接読み込んで baseline を取得
    with open(record_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                m = e.get("method", "")
                entries_by_method.setdefault(m, []).append(e)
            except json.JSONDecodeError:
                pass

    for method, entries in entries_by_method.items():
        if method not in modified_fn_map:
            continue
        modified_fn = modified_fn_map[method]
        for entry in entries:
            baseline = entry.get("ret")
            args_repr = entry.get("args_repr", "")
            try:
                # 修正版関数を引数なしで呼ぶ（コールバック形式）
                modified_result = modified_fn(baseline, args_repr)
                match = _results_match(baseline, modified_result)
            except Exception as e:
                modified_result = f"EXCEPTION: {e}"
                match = False

            results.append(ShadowResult(method, baseline, modified_result, match))

    recorder.stop()
    return results


def cmd_shadow(args) -> None:
    """Shadow比較コマンド（デモ用: 修正版=そのまま返す同値関数で比較）"""
    paths = _resolve_paths(args)
    if not paths:
        _log("エラー: 記録ファイルが見つかりません")
        return

    for path in paths:
        # デモ用: 修正版関数はbaselineをそのまま返す（差分ゼロを期待）
        info = inspect_session(path)
        methods = list(info["methods"].keys())
        modified_fn_map = {m: (lambda baseline, args_repr: baseline) for m in methods}

        results = run_shadow_compare(path, modified_fn_map)
        passed = sum(1 for r in results if r.match)
        total = len(results)
        _log(f"{path.name}: {passed}/{total} 一致")

        mismatches = [r for r in results if not r.match]
        if mismatches:
            _log("  差分 (最初の5件):")
            for r in mismatches[:5]:
                _log(f"    [{r.method}] baseline={r.baseline!r} modified={r.modified!r}")


# ── 日次一括再生 ───────────────────────────────────────────────────

def cmd_replay_date(args) -> None:
    """指定日付のすべての記録ファイルを再生する"""
    date_str = args.date  # "20260418"
    paths = sorted(RECORDED_DIR.glob(f"session_{date_str}*.jsonl"))
    if not paths:
        _log(f"記録ファイルなし: {RECORDED_DIR}/session_{date_str}*.jsonl")
        return

    _log(f"日付 {date_str}: {len(paths)} ファイル")
    for path in paths:
        info = inspect_session(path)
        _log(f"  {path.name}: {info['count']} エントリー ({info['duration_note']})")
        # 再生して例外が出ないか確認
        recorder = APIRecorder(record_dir=RECORDED_DIR)
        recorder.start_replay(path)
        ok_count = 0
        err_count = 0
        while recorder.status()["replay_index"] < recorder.status()["replay_total"]:
            idx = recorder.status()["replay_index"]
            method = recorder._replay_queue[idx].get("method", "unknown")
            try:
                recorder._replay_call(method)
                ok_count += 1
            except Exception as e:
                err_count += 1
                if "EXCEPTION" not in str(e):
                    _log(f"    再生エラー: {method}: {e}")
        recorder.stop()
        _log(f"    再生: OK={ok_count} ERR={err_count}")


# ── ヘルパー ───────────────────────────────────────────────────────

def _resolve_paths(args) -> list[Path]:
    """コマンドライン引数からファイルパスのリストを解決する"""
    if hasattr(args, "session") and args.session:
        paths = []
        for s in (args.session if isinstance(args.session, list) else [args.session]):
            p = Path(s)
            if p.is_file():
                paths.append(p)
            else:
                # glob展開を試みる
                expanded = sorted(RECORDED_DIR.glob(p.name))
                paths.extend(expanded)
        return paths
    # デフォルト: 最新ファイル
    all_files = sorted(RECORDED_DIR.glob("session_*.jsonl"))
    return all_files[-1:] if all_files else []


# ── CLI ───────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Record-Replay Runner — FutuOpenD APIコール記録・再生ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        使い方:
          python3 tests/replay_runner.py --smoke
          python3 tests/replay_runner.py --inspect --session tests/recorded/session_*.jsonl
          python3 tests/replay_runner.py --shadow --session tests/recorded/session_20260418_*.jsonl
          python3 tests/replay_runner.py --date 20260418
        """),
    )
    ap.add_argument("--smoke", action="store_true", help="Smoke test: 記録→再生で同じ結果が出るか")
    ap.add_argument("--inspect", action="store_true", help="記録ファイルの内容を表示")
    ap.add_argument("--shadow", action="store_true", help="Shadow比較: baseline vs 修正版")
    ap.add_argument("--date", type=str, help="日付 (例: 20260418) で一括再生")
    ap.add_argument("--session", nargs="+", help="対象記録ファイル (複数可)")

    args = ap.parse_args()

    if args.smoke:
        ok = cmd_smoke(args)
        sys.exit(0 if ok else 1)
    elif args.inspect:
        cmd_inspect(args)
    elif args.shadow:
        cmd_shadow(args)
    elif args.date:
        cmd_replay_date(args)
    else:
        ap.print_help()
        _log(f"\n記録ファイル一覧: {RECORDED_DIR}")
        files = sorted(RECORDED_DIR.glob("session_*.jsonl"))
        if files:
            for f in files[-10:]:
                size = f.stat().st_size
                _log(f"  {f.name} ({size:,} bytes)")
        else:
            _log("  (記録ファイルなし)")


if __name__ == "__main__":
    main()
