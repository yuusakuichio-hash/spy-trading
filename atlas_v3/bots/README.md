# atlas_v3/bots — subprocess 境界 spy_bot.py launcher

## 設計意図

### delegate 禁止

以前の実装（AtlasTrader）は `spy_bot.py` を `import` して
`SPYCreditSpreadBot` を直接呼び出す **delegate パターン** を採用していた。

Redteam は以下の理由でこれを却下した:

1. **プロセス境界消失**: import すると spy_bot のグローバル変数・ログ・PID が
   atlas_v3 プロセスと混在する。デバッグ・ログ分離が困難になる。
2. **モジュール副作用**: `spy_bot.py` はモジュールレベルで認証・接続を試みる可能性がある。
   import 時点で副作用が走ることを防げない。
3. **schg lock 回避不可能**: spy_bot.py の内部グローバル (`ENABLE_ORB` 等) を
   import 後に書き換える実装は、spy_bot の内部構造依存になり脆い。
4. **テスト汚染**: `sys.modules` に stub を差し込む迂回策が必要になり、
   テスト自体の信頼性が下がる。

### subprocess 境界隔離

本実装は `subprocess.Popen([sys.executable, 'spy_bot.py', ...])` で
spy_bot.py を **独立プロセス** として起動する。

- **logger は spy_bot 側で完結**: atlas_v3.bots は spy_bot の内部ロガーを参照しない。
- **env は spy_bot 側で完結**: 環境変数は `os.environ.copy()` で子プロセスに継承される。
  atlas_v3.bots 側でグローバルを書き換える必要はない。
- **PID は spy_bot 側で完結**: launchd / watchdog は spy_bot の PID を直接監視できる。
- **spy_bot.py 書換ゼロ**: このモジュールは spy_bot.py を一切変更しない。

### SIGTERM forward

launchd が `atlas_v3.bots` プロセスに SIGTERM を送ったとき、
`_setup_sigterm_forward()` が子プロセス (spy_bot) にも SIGTERM を転送する。

これにより:
- `launchctl unload` 時に spy_bot が孤立プロセスとして残留しない。
- `ExitTimeout` 以内に両プロセスが終了する。

## 使用方法

```
python3 -m atlas_v3.bots --mode paper
python3 -m atlas_v3.bots --mode paper --dry-run
python3 -m atlas_v3.bots --mode paper --test-connect
python3 -m atlas_v3.bots --mode live
python3 -m atlas_v3.bots --mode dry
python3 -m atlas_v3.bots --mode paper --no-orb --no-calendar --no-multi
```

## argv 変換表

| atlas_v3.bots 引数 | spy_bot.py 引数 |
|---|---|
| `--mode paper` | `--paper` |
| `--mode live` | (なし、spy_bot デフォルト) |
| `--mode dry` | `--paper --dry-test` |
| `--mode test-connect` | `--paper --test-connect` |
| `--dry-run` | `--dry-test` |
| `--no-orb` | `--no-orb` |
| `--no-calendar` | `--no-calendar` |
| `--no-multi` | `--no-multi` |

## 禁止事項

- `from spy_bot import ...` / `import spy_bot` をこのパッケージ内で行うことは禁止。
- `AtlasTrader` クラスの復活禁止 (delegate パターン回帰禁止)。
- spy_bot.py の内部グローバル (`ENABLE_ORB` 等) を直接書き換えることは禁止。
