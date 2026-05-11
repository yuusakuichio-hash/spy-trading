# {{PROJECT_NAME}} — Sora Lab 収益化候補プロジェクト

このプロジェクトは `sora_incubator` から bootstrap された汎用 governance scaffold 上で動く。トレーディング(spy-trading)とは独立。

## 最上位規律(spy-trading から継承・ドメイン非依存版)

1. **バグなし絶対最優先** — 速度・並列化・完了宣言はこの下
2. **完了宣言は証跡 4 点セット必須** — grep / AST / test stdout / mutation
3. **既存コード書換禁止の代わりに**: ドメイン未確定段階では「動くもの優先・捨てる前提」で書く。ただし削除前に確認
4. **数値引用物理規律** — 金額・%・件数の直書き禁止、根拠ファイル参照
5. **時間感覚規律** — `date` で実時刻確認、「すぐ」発言前に過去実測補正

## このプロジェクトのドメイン

(未確定 / 候補:                                  )

ドメイン確定後、以下を更新:
- 目標(金額・期日)
- ターゲットユーザー
- 競合・差別化
- 撤退基準

## 絶対守るべき禁句 TOP5(継承)

1. 「月曜から」「週末に」「後日」「クローズ後」等の先延ばし語彙禁止
2. 「進めていい？」「どれからやる？」等の不要な確認禁止(明確に判断必要な箇所のみ)
3. 「メモリに保存した」で対策完了扱い禁止(hook / linter / 物理化まで)
4. 一般論禁止 — 具体ファイル名・行番号・出典付きで語る
5. 「全テスト pass」「完全稼働」等の根拠なし断定禁止 — evidence path を必ず添える

## セッション開始時必須手順

1. このファイル(CLAUDE.md)を読む
2. `memory/INDEX.md` でドメイン関連 md を引く
3. `data/logs/pending_proposal_violations.md` があれば未解決違反を確認

## 鉄則

1. 実装前に公式ドキュメント確認(`memory/feedback_decision_criteria.md`)
2. 公式仕様確認済なら即実行・無駄な確認禁止
3. 完了宣言時は `pytest` 等の全件 + 証跡 4 点セット必須
4. プライベート時間に干渉しない(ゆうさくさんの休息・家族時間を尊重)

## 絶対禁止事項

- `/compact` 禁止(会話圧縮・要約・重要議論削除禁止)
- spy-trading 側のファイル(`/Users/yuusakuichio/trading/` 配下)書換禁止 — 参照のみ
- secret(Pushover token/user, API key 等)を git にコミットしない — `.env` で管理

## hook 物理ガード(`.claude/settings.json` で wiring 済)

- `pronoun_guard.sh` 一人称規律
- `discipline_guard.sh` 先延ばし・確認癖・桁違い見積
- `confidence_assertion_guard.sh` 根拠なし断定
- `deferral_language_guard.sh` 先延ばし語(応答)
- `false_claim_detector.sh` 虚偽完了
- `claim_ledger_guard.py` 未検証 URL/価格/仕様

詳細は `incubator_template/README.md`。

## 通知設定(任意)

Pushover を使う場合のみ `.env` に `PUSHOVER_TOKEN` `PUSHOVER_USER` を設定。未設定時は hook は通知をスキップ(violation 検知は機能継続)。
