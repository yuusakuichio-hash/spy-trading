"""atlas_v3/bots/main.py — AtlasEngine native ランチャー。

設計方針（β-2 配線実装 2026-04-25）
--------------------------------------
- subprocess.Popen(spy_bot.py) 経路を完全廃止。
- AtlasEngine を直接インスタンス化し TacticRegistry 経由で 11 戦術を登録。
- paper / dry-run モードでは stub providers を注入してブローカー接続なしで動作。
- SIGTERM / KeyboardInterrupt で graceful shutdown（engine.stop_event セット → loop 終了）。
- spy_bot.py / common/* / chronos* / atlas_v3/core/engine.py / registry.py は変更禁止。

使用例
------
    python3 -m atlas_v3.bots --mode paper
    python3 -m atlas_v3.bots --mode paper --dry-run
    python3 -m atlas_v3.bots --mode paper --no-orb --no-calendar --no-multi
    python3 -m atlas_v3.bots --mode live

フラグ → TacticRegistry disable_names 変換表
-------------------------------------------
    --no-orb       → "orb_native" を除外
    --no-calendar  → "diagonal_spread" を除外
    --no-multi     → 現在 atlas_v3 では multi-symbol 概念が tactic 単位でないため
                     ログ警告のみ（将来の tactic 追加時に対応）
"""
from __future__ import annotations

import argparse
import datetime
import logging
import signal
import threading
import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from atlas_v3.core.engine import AtlasEngine

log = logging.getLogger("atlas.bots.main")

# ---------------------------------------------------------------------------
# Stub providers（paper / dry-run 時に注入）
# ---------------------------------------------------------------------------

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.core.engine import OrderRequest, OrderResult


# _StubMarketData 固定値定数（マジックナンバー排除）
_STUB_VIX: float = 16.0
_STUB_VRP: float = 0.0
_STUB_GEX: float = 0.0
_STUB_TERM_RATIO: float = 1.0
_STUB_BIAS: str = "neutral"
_STUB_IVR_SPY: float = 40.0


class _StubMarketData:
    """dry-run / paper モード用 MarketDataClient stub。

    get_environment() は固定の中立 MarketEnvironment を返す。
    Phase 2 で MoomooProvider / YFinanceProvider に差し替える。
    """

    def get_environment(self) -> MarketEnvironment:
        return MarketEnvironment(
            vix=_STUB_VIX,
            vrp=_STUB_VRP,
            gex=_STUB_GEX,
            term_ratio=_STUB_TERM_RATIO,
            bias=_STUB_BIAS,
            ivr_by_symbol={"SPY": _STUB_IVR_SPY},
        )


class _StubBroker:
    """dry-run / paper モード用 BrokerClient stub。

    place_order() は発注をスキップして dry_run_skip ステータスを返す。
    Phase 2 で MoomooBroker / PaperBroker に差し替える。
    """

    def place_order(self, request: OrderRequest) -> OrderResult:
        log.info(
            "[_StubBroker] dry-run skip: symbol=%s side=%s qty=%d tactic=%s",
            request.symbol,
            request.side,
            request.quantity,
            request.tactic_name,
        )
        return OrderResult(
            order_id=f"stub-{uuid.uuid4().hex[:8]}",
            symbol=request.symbol,
            status="dry_run_skip",
            tactic_name=request.tactic_name,
            detail="stub broker: order skipped in dry-run/paper mode",
        )


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """ArgumentParser を構築して返す（テストから単体呼び出し可能）。"""
    parser = argparse.ArgumentParser(
        prog="python3 -m atlas_v3.bots",
        description="atlas_v3.bots — AtlasEngine native ランチャー",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live", "dry", "test-connect"],
        required=True,
        help=(
            "実行モード: "
            "paper=ペーパートレード / "
            "live=本番 / "
            "dry=paper+dry-run (接続なし) / "
            "test-connect=接続テストのみ"
        ),
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="接続なし・市場時間外でも全ロジックをテスト（stub providers 使用）",
    )
    parser.add_argument(
        "--no-orb",
        dest="no_orb",
        action="store_true",
        help="ORB 買い戦略を無効化（tactic_name='orb_native' を除外）",
    )
    parser.add_argument(
        "--no-calendar",
        dest="no_calendar",
        action="store_true",
        help="カレンダースプレッド戦略を無効化（tactic_name='diagonal_spread' を除外）",
    )
    parser.add_argument(
        "--no-multi",
        dest="no_multi",
        action="store_true",
        help="マルチ銘柄同時運用を無効化（将来の multi-symbol tactic 除外に対応）",
    )
    return parser


# ---------------------------------------------------------------------------
# disable_names 計算
# ---------------------------------------------------------------------------

# --no-* フラグ → 除外する tactic_name のマッピング
_FLAG_TO_TACTIC_NAMES: dict[str, list[str]] = {
    "no_orb": ["orb_native"],
    "no_calendar": ["diagonal_spread"],
    # no_multi: atlas_v3 v3.0 時点では tactic 単位の multi-symbol 概念なし
    # 将来 multi_symbol_* tactic が追加された際にここに追記する
    "no_multi": [],
}


def build_disable_names(args: argparse.Namespace) -> list[str]:
    """parser 解析済み args から除外する tactic_name リストを返す。

    Returns
    -------
    list[str]
        TacticRegistry からフィルタアウトする tactic_name の一覧。
    """
    disable: list[str] = []
    for flag, names in _FLAG_TO_TACTIC_NAMES.items():
        if getattr(args, flag, False):
            disable.extend(names)
            if not names:
                log.warning(
                    "[main] --%s 指定: atlas_v3 v3.0 では対応 tactic なし（ログのみ）",
                    flag.replace("_", "-"),
                )
    return disable


# ---------------------------------------------------------------------------
# AtlasEngine ファクトリ
# ---------------------------------------------------------------------------

def build_engine_native(
    disable_names: list[str],
    market_data: object | None = None,
    broker: object | None = None,
) -> "AtlasEngine":
    """TacticRegistry 経由で AtlasEngine を組み立てて返す。

    Parameters
    ----------
    disable_names : list[str]
        除外する tactic_name リスト（--no-orb 等から生成）。
    market_data : object | None
        MarketDataClient 実装。None のとき _StubMarketData を使用。
    broker : object | None
        BrokerClient 実装。None のとき _StubBroker を使用。

    Returns
    -------
    AtlasEngine
        disable_names を除いた戦術が登録済みのエンジン。
    """
    from atlas_v3.bots.engines.registry import TacticRegistry
    from atlas_v3.core.engine import AtlasEngine

    registry = TacticRegistry()

    if disable_names:
        log.info("[main] disable_names=%s を除外して AtlasEngine を組み立て", disable_names)
        tactics = [t for t in registry.all_tactics() if t.tactic_name not in disable_names]
    else:
        tactics = registry.all_tactics()

    log.info(
        "[main] AtlasEngine 組み立て: 戦術数=%d / 全体=%d",
        len(tactics),
        len(registry),
    )

    md = market_data if market_data is not None else _StubMarketData()
    bk = broker if broker is not None else _StubBroker()

    return AtlasEngine(
        market_data=md,
        broker=bk,
        tactics=tactics,
    )


# ---------------------------------------------------------------------------
# run loop
# ---------------------------------------------------------------------------

# tick 間隔（秒）— Phase 2 で動的調整予定
_TICK_INTERVAL_SECS: float = 60.0


def _run_one_tick(
    engine: "AtlasEngine",
    tick_count: int,
    session_id_prefix: str,
) -> tuple[object, bool]:
    """1 tick 分の engine.run_session を実行して (result, kill_switch_triggered) を返す。

    Parameters
    ----------
    engine : AtlasEngine
        実行対象エンジン。
    tick_count : int
        現在の tick 番号（session_id 生成に使用）。
    session_id_prefix : str
        session_id プレフィックス。

    Returns
    -------
    tuple[object, bool]
        (SessionResult | None, Kill Switch が発動したか)
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    session_id = f"{session_id_prefix}tick-{tick_count}-{now_jst.strftime('%Y%m%dT%H%M%S')}"
    log.info("[run_loop] tick=%d session_id=%s", tick_count, session_id)
    try:
        result = engine.run_session(session_id=session_id)
    except Exception as exc:  # noqa: BLE001 — run_session は内部例外を外へ漏らさない設計（ループ継続優先）
        log.error("[run_loop] tick=%d 例外: %s", tick_count, exc, exc_info=True)
        result = None
    kill_switch = bool(result is not None and result.terminated_by_kill_switch)
    return result, kill_switch


def run_loop(
    engine: "AtlasEngine",
    stop_event: threading.Event,
    tick_interval_secs: float = _TICK_INTERVAL_SECS,
    session_id_prefix: str = "",
) -> int:
    """AtlasEngine の tick loop を SIGTERM / stop_event まで回し続ける。

    Parameters
    ----------
    engine : AtlasEngine
        実行対象エンジン。
    stop_event : threading.Event
        セットされたらループ終了。SIGTERM ハンドラがセットする。
    tick_interval_secs : float
        tick 間隔（秒）。デフォルト 60 秒。0 以下は不正。
    session_id_prefix : str
        session_id プレフィックス（テストで識別用）。

    Returns
    -------
    int
        終了コード（0=正常, 1=Kill Switch 等による異常）。
    """
    assert tick_interval_secs > 0, f"tick_interval_secs は正の値が必要: {tick_interval_secs}"
    log.info("[run_loop] 開始: tick_interval=%.1fs", tick_interval_secs)
    tick_count = 0
    terminated_by_ks = False

    while not stop_event.is_set():
        tick_count += 1
        _, kill_switch = _run_one_tick(engine, tick_count, session_id_prefix)
        if kill_switch:
            log.warning("[run_loop] Kill Switch ARMED — ループ終了")
            terminated_by_ks = True
            break
        # 次の tick まで待機（stop_event がセットされれば即抜け）
        stop_event.wait(timeout=tick_interval_secs)

    log.info(
        "[run_loop] 終了: ticks=%d kill_switch=%s",
        tick_count,
        terminated_by_ks,
    )
    return 1 if terminated_by_ks else 0


# ---------------------------------------------------------------------------
# SIGTERM graceful shutdown
# ---------------------------------------------------------------------------

def setup_graceful_shutdown(stop_event: threading.Event) -> None:
    """SIGTERM / SIGINT を受け取ったら stop_event をセットするハンドラを登録する。

    Parameters
    ----------
    stop_event : threading.Event
        ループ停止フラグ。
    """
    def _handle(signum: int, frame: object) -> None:
        log.info("[main] シグナル受信 (signum=%d) → stop_event セット", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    log.info("[main] graceful shutdown ハンドラ登録完了 (SIGTERM / SIGINT)")


# ---------------------------------------------------------------------------
# main サブルーティン
# ---------------------------------------------------------------------------

def _main_test_connect(disable_names: list[str]) -> int:
    """test-connect モード: AtlasEngine 組み立てのみ行い即終了する。

    Returns
    -------
    int
        0=成功, 1=失敗。
    """
    log.info("[main] test-connect モード: engine 組み立てのみで終了")
    try:
        build_engine_native(disable_names=disable_names)
        log.info("[main] test-connect OK: AtlasEngine 組み立て完了")
        return 0
    except Exception as exc:
        log.error("[main] test-connect FAIL: %s", exc, exc_info=True)
        return 1


def _build_market_data(mode: str):
    """mode に応じて MarketDataClient 実装を返す。

    paper / live モード: moomoo OpenD 経由で MoomooMarketDataAdapter (実 VIX 等) を注入。
    moomoo 接続失敗時は _StubMarketData fallback (degraded mode 警告 log 付)。
    dry / test-connect モード: 常に _StubMarketData (テスト用固定値)。

    Returns
    -------
    (market_data, quote_ctx_to_close): MarketDataClient 実装 + cleanup 用 quote_ctx (None 可)
    """
    if mode in ("dry", "test-connect"):
        return None, None  # build_engine_native で _StubMarketData fallback

    try:
        import futu as ft
        from atlas_v3.ops.market_data_adapter import MoomooMarketDataAdapter
        quote_ctx = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
        adapter = MoomooMarketDataAdapter(quote_ctx)
        log.info(
            "[main] MoomooMarketDataAdapter 注入完了 (mode=%s)・実 VIX 取得経路アクティブ",
            mode,
        )
        return adapter, quote_ctx
    except Exception as exc:
        log.warning(
            "[main] MoomooMarketDataAdapter 注入失敗・_StubMarketData fallback: %s",
            exc,
        )
        return None, None


def _main_start_run_loop(disable_names: list[str], mode: str) -> int:
    """paper / dry / live モード: AtlasEngine を組み立てて run_loop を起動する。

    Returns
    -------
    int
        run_loop の終了コード（0=正常, 1=Kill Switch 等による異常）。
    """
    stop_event = threading.Event()
    setup_graceful_shutdown(stop_event)
    market_data, quote_ctx_to_close = _build_market_data(mode)
    try:
        engine = build_engine_native(
            disable_names=disable_names,
            market_data=market_data,
        )
    except Exception as exc:
        log.error("[main] AtlasEngine 組み立て失敗: %s", exc, exc_info=True)
        if quote_ctx_to_close is not None:
            try:
                quote_ctx_to_close.close()
            except Exception:
                pass
        return 1
    log.info("[main] run_loop 開始 (mode=%s)", mode)
    try:
        return run_loop(engine=engine, stop_event=stop_event)
    finally:
        if quote_ctx_to_close is not None:
            try:
                quote_ctx_to_close.close()
                log.info("[main] quote_ctx close 完了")
            except Exception as e:
                log.warning("[main] quote_ctx close 失敗: %s", e)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """CLI エントリポイント。終了コード (0=正常, 非0=異常) を返す。

    Parameters
    ----------
    argv : list[str] | None
        引数リスト（None のとき sys.argv[1:] を使用）。テストから直接渡せる。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)

    is_dry_run = args.dry_run or args.mode in ("dry", "test-connect")
    is_paper = args.mode in ("paper", "dry", "test-connect") or args.dry_run
    log.info(
        "[main] 起動: mode=%s dry_run=%s no_orb=%s no_calendar=%s no_multi=%s "
        "is_dry_run=%s is_paper=%s",
        args.mode, args.dry_run, args.no_orb, args.no_calendar, args.no_multi,
        is_dry_run, is_paper,
    )

    disable_names = build_disable_names(args)
    if disable_names:
        log.info("[main] 除外 tactic_names: %s", disable_names)

    if args.mode == "test-connect":
        return _main_test_connect(disable_names)
    return _main_start_run_loop(disable_names, args.mode)


if __name__ == "__main__":
    import sys
    sys.exit(main())
