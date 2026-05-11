# Sora Incubator Template

トレーディング(spy-trading)プロジェクトの「嘘被害0」governance 機構を、ドメイン非依存な形で抽出した雛形。新規収益化プロジェクトを独立フォルダで立ち上げるための土台。

## 用途

- spy-trading とは別ドメインの新規プロジェクト(SNS / SaaS / コンサル等)を立ち上げる
- 完了虚偽宣言・先延ばし語彙・根拠なし断定・一人称ブレ等の規律違反を物理的に block する hook を継承
- 各プロジェクトは独立 git リポジトリ・独立 hook で完結し、相互非干渉

## トレーディング非干渉の保証

- このテンプレートから生成されるプロジェクトは **trading の `common/` `atlas_v3/` `chronos_v3/` 等を一切参照しない**
- hook 内パスは **`$CLAUDE_PROJECT_DIR` で各プロジェクトに閉じる**
- Pushover token/user 等の secret は **継承せず**、各プロジェクトの `.env` で個別管理
- trading 側 runtime に対して読み書き・実行・購読は **発生しない**

## セットアップ手順(ローカル VPS で実行)

```bash
# 1) spy-trading リポジトリの claude/new-code-separate-folder-TpchB ブランチを checkout 済みとする
cd /Users/yuusakuichio/trading  # (= /home/user/spy-trading in cloud env)
git checkout claude/new-code-separate-folder-TpchB

# 2) 育成所の親フォルダを作成
mkdir -p ~/sora_incubator

# 3) 候補プロジェクトを bootstrap
bash incubator_template/install.sh ~/sora_incubator/candidate_a

# 4) 新プロジェクトに cd して Claude Code を起動
cd ~/sora_incubator/candidate_a
claude
```

## フォルダ昇格(後で再編成)

`install.sh` が生成するプロジェクトは hook パスが全て `$CLAUDE_PROJECT_DIR` 基準なので、`mv` で任意の場所に動かせる:

```bash
# 育成所からメインプロジェクトに昇格
mv ~/sora_incubator/candidate_a ~/sora-monetize

# Claude Code を再起動するだけで OK(設定は自動追従)
cd ~/sora-monetize
claude
```

## 継承した規律

`_template/.claude/hooks/` に以下を配置:

| hook | 種別 | 動作 |
|---|---|---|
| `pronoun_guard.sh` | Stop | 一人称「僕」「俺」検知で block(引用内は許可) |
| `discipline_guard.sh` | PreToolUse + UserPromptSubmit | 先延ばし語・確認癖・桁違い見積を検知、3回目で hard block |
| `confidence_assertion_guard.sh` | Stop | 「X%確実」「全合格」等の根拠なし断定を evidence なしで block |
| `deferral_language_guard.sh` | Stop | 「明日」「後日」等の先延ばし語が応答に含まれたら通知(Pushover 設定時のみ) |
| `false_claim_detector.sh` | Stop | 「完了」宣言時に pytest 証跡なければ警告 |
| `claim_ledger_guard.py` | Stop | URL/価格/仕様の未検証 claim を block(プロジェクト毎の ledger) |

## 継承しなかったもの(trading 固有)

- `legacy_write_block.sh`: trading コード保護専用
- `auth_budget_guard.py`: OpenD 3/24h
- `circuit_breaker_no_auto_recovery_guard.sh`: kill switch
- `chronos_edit_spec_guard.sh`: Chronos 仕様準拠
- `session_start_market_specs_reload.sh`: 米国市場時間
- `andon_multichannel.py`: Pushover チャネル設定込み

## ファイル構成

```
incubator_template/
├── README.md           # このファイル
├── install.sh          # bootstrap スクリプト
└── _template/
    ├── CLAUDE.md       # ドメイン非依存の汎用規律
    ├── .gitignore
    ├── .env.example    # Pushover 等の secret テンプレート
    ├── .claude/
    │   ├── settings.json
    │   └── hooks/      # ジェネリック governance hook 群
    ├── scripts/        # 各プロジェクトで追加するスクリプト用
    ├── memory/         # 汎用規律 md(ドメイン非依存のみ)
    └── data/logs/      # hook 違反ログ出力先
```
