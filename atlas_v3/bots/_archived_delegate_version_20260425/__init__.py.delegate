"""atlas_v3.bots — subprocess 境界 spy_bot.py launcher パッケージ。

公開シンボル
-----------
main
    argparse + subprocess.Popen で spy_bot.py を起動する CLI エントリポイント関数。
    python3 -m atlas_v3.bots --mode paper で呼び出される。

build_parser
    ArgumentParser 構築関数（テスト・外部ツールから直接利用可能）。

build_spy_bot_argv
    parser 解析済み Namespace → spy_bot.py argv 変換関数。

launch_spy_bot
    spy_bot.py を subprocess.Popen で起動する低レベル関数。

設計制約（Redteam 対案 2026-04-25 採択）
----------------------------------------
- delegate 禁止: spy_bot.py を import して SPYCreditSpreadBot を呼ぶ実装は却下済。
- subprocess 境界: spy_bot.py は独立プロセスとして起動する。
  logger / env / PID はすべて spy_bot 側で完結する。
- AtlasTrader クラスはこのパッケージから削除。delegate 復活禁止。
"""
from atlas_v3.bots.main import (
    build_parser,
    build_spy_bot_argv,
    launch_spy_bot,
    main,
)

__all__ = [
    "build_parser",
    "build_spy_bot_argv",
    "launch_spy_bot",
    "main",
]
