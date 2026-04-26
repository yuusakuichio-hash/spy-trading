"""atlas_v3.bots — AtlasEngine native ランチャーパッケージ。

公開シンボル
-----------
main
    argparse + AtlasEngine で native run loop を起動する CLI エントリポイント関数。
    python3 -m atlas_v3.bots --mode paper で呼び出される。

build_parser
    ArgumentParser 構築関数（テスト・外部ツールから直接利用可能）。

build_disable_names
    parser 解析済み Namespace → 除外 tactic_name リスト変換関数。

build_engine_native
    TacticRegistry 経由で AtlasEngine を組み立てるファクトリ関数。

run_loop
    AtlasEngine の tick loop（stop_event まで継続）。

setup_graceful_shutdown
    SIGTERM / SIGINT ハンドラ登録関数。

設計制約（β-2 配線実装 2026-04-25）
--------------------------------------
- subprocess.Popen(spy_bot.py) 経路を廃止。
- AtlasEngine を直接インスタンス化し TacticRegistry 経由で 11 戦術を登録。
- spy_bot.py / common/* / chronos* / atlas_v3/core/engine.py / registry.py は変更禁止。
"""
from atlas_v3.bots.main import (
    build_disable_names,
    build_engine_native,
    build_parser,
    main,
    run_loop,
    setup_graceful_shutdown,
)

__all__ = [
    "build_disable_names",
    "build_engine_native",
    "build_parser",
    "main",
    "run_loop",
    "setup_graceful_shutdown",
]
