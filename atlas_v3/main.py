"""atlas_v3/main.py — Atlas v3 独立 entry point (判断 1: bootstrap 配線 Case B)

責務:
- YAML ロード → preflight コンプライアンスチェック → MonitorDaemon 起動の一貫エントリポイント
- spy_bot.py には一切触らない（既存コード書換禁止）
- argparse で --mode paper|live / --config-file / --provider を受け付ける
- 単体実行可能: python3 -m atlas_v3.main --mode paper

設計:
- bootstrap_paper_monitor() を唯一の起動経路として使用（CRIT-R4-1 解消）
- metric_provider は --provider 引数で選択:
    dummy    : DummyMetricProvider（起動・疎通確認用・テスト専用）
    yfinance : YFinanceMetricProvider（実 yfinance データ取得）
    moomoo   : MoomooMetricProvider（moomoo Paper Bot データ取得・将来実装）
  C2 fix: --provider 未指定時のデフォルトは "yfinance"（Dummy 本番流出を防止）
- daemon.start() 後はシグナル待機（SIGINT/SIGTERM で graceful stop）

使用方法:
    # Paper モードで起動（LaunchAgent から呼ばれる）
    python3 -m atlas_v3.main --mode paper --provider yfinance

    # カスタム YAML 設定ファイルで起動
    python3 -m atlas_v3.main --mode paper --provider yfinance --config-file data/configs/atlas_paper_risk.yaml

    # preflight チェックをスキップして起動（テスト用）
    python3 -m atlas_v3.main --mode paper --provider dummy --skip-preflight

    # Live モードで起動（preflight 全項目 CRITICAL チェック）
    python3 -m atlas_v3.main --mode live --provider yfinance --config-file data/configs/atlas_production_small.yaml

launchd 連携:
    ~/Library/LaunchAgents/com.soralab.atlas-paper.plist の ProgramArguments に
    [..., --mode, paper, --provider, yfinance] を設定すること。
    --provider を省略すると yfinance がデフォルトになる（C2 fix）。

C2 fix (DummyMetricProvider 本番流出修正):
    - 旧実装: main.py:125 で常に DummyMetricProvider をインスタンス化 → 監視全盲
    - 新実装: --provider {dummy,yfinance,moomoo} で明示選択。デフォルト yfinance。
    - DummyMetricProvider は --provider dummy を明示した場合のみ起動。
    - launchd plist は --provider yfinance を ProgramArguments で渡す。
"""
from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

# プロジェクトルートを sys.path に追加（モジュールとして呼ばれた場合は不要だが直接実行時用）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DummyMetricProvider — 起動確認用（実 Bot 未接続時のフォールバック）
# ---------------------------------------------------------------------------

class DummyMetricProvider:
    """起動疎通確認用のダミー metric provider。

    実 Bot が接続されるまでの間、monitor が起動できることを確認するために使用。
    本番では実 Bot の metric_provider を差し替える。

    警告: このプロバイダはゼロ値を返すため、監視実質無効。
    起動後に実 Bot の provider を bootstrap_paper_monitor() の引数で渡すこと。
    """

    def __init__(self, warn_on_use: bool = True) -> None:
        self._warn_on_use = warn_on_use

    def get_metrics(self) -> dict:
        if self._warn_on_use:
            log.warning(
                "[DummyMetricProvider] Returning zero metrics. "
                "Replace with real Bot metric_provider for production monitoring."
            )
        return {
            "pnl_day_usd": 0.0,
            "drawdown_pct": 0.0,
            "latency_ms": 0.0,
        }


# ---------------------------------------------------------------------------
# シグナルハンドラ
# ---------------------------------------------------------------------------

_SHUTDOWN_REQUESTED = False


def _handle_signal(signum: int, frame) -> None:
    """SIGINT / SIGTERM で graceful shutdown を要求する。"""
    global _SHUTDOWN_REQUESTED
    log.info("[main] Signal %s received. Requesting shutdown...", signum)
    _SHUTDOWN_REQUESTED = True


# ---------------------------------------------------------------------------
# メイン起動ロジック
# ---------------------------------------------------------------------------

def _build_metric_provider(provider_name: str) -> "Callable[[], dict]":
    """--provider 引数から metric provider callable を構築する。

    C2 fix: DummyMetricProvider は --provider dummy 明示時のみ返す。
    デフォルトは yfinance。moomoo は将来実装。

    Args:
        provider_name: "dummy" / "yfinance" / "moomoo"

    Returns:
        callable: () -> dict (keys: pnl_day_usd, drawdown_pct, latency_ms)

    Raises:
        ValueError: 未知の provider 名
        ImportError: provider の依存ライブラリが未インストール
    """
    if provider_name == "dummy":
        log.warning(
            "[main] --provider dummy specified. "
            "DummyMetricProvider returns zero metrics — monitoring is effectively disabled. "
            "Use --provider yfinance for production paper trading."
        )
        dummy = DummyMetricProvider(warn_on_use=True)
        return dummy.get_metrics

    if provider_name == "yfinance":
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        yf_provider = YFinanceMetricProvider()
        log.info("[main] Using YFinanceMetricProvider for metric data.")
        return yf_provider.get_metrics

    if provider_name == "moomoo":
        # ADR-014 (Sprint 2 C-017 本実装) により配線。
        # S-1 fix: startup 時に smoke_test → 401/unauth 早期検知
        # ADR-015 B fix: AuthenticationError → yfinance auto fallback
        #   （旧: SystemExit(78) → launchd 再起動ループ / 監視全盲リスク）
        #   （新: AuthenticationError catch → YFinanceMetricProvider に自動切替）
        from atlas_v3.ops.moomoo_provider import (
            AuthenticationError,
            MoomooMetricProvider,
            MoomooProviderNotImplementedError,
        )
        moomoo_provider = MoomooMetricProvider()
        try:
            moomoo_provider.smoke_test()
            log.info("[main] MoomooMetricProvider smoke_test passed.")
        except AuthenticationError as auth_err:
            # ADR-015 B: AuthenticationError → yfinance fallback（SystemExit しない）
            log.warning(
                "[main] Moomoo smoke_test FAILED (AuthenticationError): %s. "
                "ADR-015 B: Auto-falling back to YFinanceMetricProvider. "
                "Monitoring continues with proxy metrics. "
                "Re-login via moomoo app to restore real PnL metrics.",
                auth_err,
            )
            return _build_metric_provider("yfinance")
        except MoomooProviderNotImplementedError as ni_err:
            # futu-api 未インストール → yfinance fallback（開発環境配慮）
            log.warning(
                "[main] Moomoo not implemented / futu-api missing: %s. "
                "ADR-015 B: Auto-falling back to YFinanceMetricProvider.",
                ni_err,
            )
            return _build_metric_provider("yfinance")
        except Exception as other_err:
            log.warning(
                "[main] Moomoo smoke_test non-auth error (OpenD may not be running): %s. "
                "Proceeding with fail-closed provider (get_metrics will raise).",
                other_err,
            )
        log.info("[main] Using MoomooMetricProvider (Sprint 2 C-017) for metric data.")
        return moomoo_provider.get_metrics

    raise ValueError(
        f"Unknown provider: {provider_name!r}. "
        "Must be one of: dummy, yfinance, moomoo"
    )


def run(
    mode: str = "paper",
    config_file: Optional[Path] = None,
    skip_preflight: bool = False,
    daemon_only: bool = False,
    provider: str = "yfinance",
) -> int:
    """Atlas v3 を起動する。

    Args:
        mode:           "paper" / "live"
        config_file:    YAML 設定ファイルパス。None なら atlas_paper_risk.yaml を使用。
        skip_preflight: True なら preflight チェックをスキップ（テスト用）。
        daemon_only:    True なら daemon 起動後すぐに返す（テスト用）。
        provider:       metric provider 名。"dummy" / "yfinance" / "moomoo"
                        C2 fix: デフォルト "yfinance"（旧: Dummy 固定 → 監視全盲）

    Returns:
        終了コード（0: 正常 / 1: エラー）
    """
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = False

    log.info("[main] Starting Atlas v3 in mode=%s provider=%s", mode, provider)

    # シグナルハンドラ登録
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # C2 fix: --provider 引数で metric provider を選択。デフォルト yfinance。
    # 旧: 常に DummyMetricProvider → 監視全盲（C2 修正前）
    try:
        metric_provider_fn = _build_metric_provider(provider)
    except (ValueError, ImportError, NotImplementedError) as e:
        log.critical("[main] Failed to build metric provider: %s", e)
        return 1

    try:
        from atlas_v3.ops.monitor import bootstrap_paper_monitor

        daemon = bootstrap_paper_monitor(
            metric_provider=metric_provider_fn,
            config_path=config_file,
            run_preflight=not skip_preflight,
            preflight_mode=mode,
        )

        log.info(
            "[main] MonitorDaemon started (mode=%s, check_interval=%.1fs). "
            "Waiting for shutdown signal (SIGINT/SIGTERM)...",
            mode,
            daemon.config.check_interval_secs,
        )

        if daemon_only:
            # テスト用: daemon 起動後すぐに返す
            return 0

        # シグナル待機ループ
        while not _SHUTDOWN_REQUESTED:
            time.sleep(1.0)

        log.info("[main] Shutdown requested. Stopping MonitorDaemon...")
        daemon.stop(timeout=10.0)
        log.info("[main] MonitorDaemon stopped. Atlas v3 exit normally.")
        return 0

    except KeyboardInterrupt:
        log.info("[main] KeyboardInterrupt. Stopping.")
        return 0
    except Exception as e:
        log.critical("[main] Fatal error: %s", e, exc_info=True)
        return 1


def _verify_daemon_alive(label: str = "com.soralab.atlas-paper") -> int:
    """CRIT-R6-4: launchctl list で daemon が実際に起動しているか確認する。

    install_atlas_paper_daemon.sh が launchctl bootstrap 後に呼び出すことを想定。
    'PID' が launchctl list 出力に含まれていれば起動中と判断。

    Returns:
        0: daemon 起動確認成功
        1: daemon 未起動または確認失敗
    """
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # 出力に "PID" が含まれていれば実際に動作中
            output = result.stdout + result.stderr
            if '"PID"' in output or "PID" in output:
                log.info("[verify_daemon_alive] %s is RUNNING (CRIT-R6-4 check passed).", label)
                return 0
            else:
                log.warning(
                    "[verify_daemon_alive] %s listed but no PID found (may be stopped). "
                    "Output: %s",
                    label, output[:200],
                )
                return 1
        else:
            log.error(
                "[verify_daemon_alive] launchctl list %s failed (exit=%d). "
                "Daemon may not be loaded. stderr: %s",
                label, result.returncode, result.stderr[:200],
            )
            return 1
    except FileNotFoundError:
        log.error("[verify_daemon_alive] launchctl not found (not on macOS?).")
        return 1
    except subprocess.TimeoutExpired:
        log.error("[verify_daemon_alive] launchctl list timed out.")
        return 1
    except Exception as e:
        log.error("[verify_daemon_alive] Unexpected error: %s", e)
        return 1


def main() -> None:
    """argparse エントリポイント。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Atlas v3 独立 entry point (判断 1: bootstrap 配線)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python3 -m atlas_v3.main --mode paper
  python3 -m atlas_v3.main --mode paper --config-file data/configs/atlas_paper_risk.yaml
  python3 -m atlas_v3.main --mode live --config-file data/configs/atlas_production_small.yaml
  python3 -m atlas_v3.main --mode paper --skip-preflight  # テスト用
""",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="起動モード。paper: PENDING_OWNER_APPROVAL_PAPER は WARN のみ。live: 全 CRITICAL チェック。",
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        default=None,
        dest="config_file",
        help="YAML 設定ファイルパス（デフォルト: data/configs/atlas_paper_risk.yaml）",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        default=False,
        dest="skip_preflight",
        help="preflight チェックをスキップする（テスト用・本番では使用禁止）",
    )
    parser.add_argument(
        "--provider",
        choices=["dummy", "yfinance", "moomoo"],
        default="yfinance",
        dest="provider",
        help=(
            "metric provider。"
            "yfinance: yfinance ライブラリ経由（デフォルト・本番推奨）。"
            "dummy: ゼロ値ダミー（テスト専用・本番使用禁止）。"
            "moomoo: moomoo Paper Bot データ（将来実装）。"
            "C2 fix: デフォルトを yfinance に変更（旧: dummy 固定 → 監視全盲）"
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        dest="log_level",
        help="ログレベル（デフォルト: INFO）",
    )
    parser.add_argument(
        "--verify-daemon-alive",
        action="store_true",
        default=False,
        dest="verify_daemon_alive",
        help=(
            "CRIT-R6-4: launchctl list com.soralab.atlas-paper を実行して "
            "daemon が実際に起動しているか確認する。"
            "起動確認に失敗した場合は exit code 1 で終了する。"
            "install_atlas_paper_daemon.sh から呼ばれることを想定。"
        ),
    )

    args = parser.parse_args()

    # ログレベル設定
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # CRIT-R6-4: --verify-daemon-alive は daemon 起動確認のみ（run() は呼ばない）
    if args.verify_daemon_alive:
        exit_code = _verify_daemon_alive()
        sys.exit(exit_code)

    exit_code = run(
        mode=args.mode,
        config_file=args.config_file,
        skip_preflight=args.skip_preflight,
        daemon_only=False,
        provider=args.provider,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
