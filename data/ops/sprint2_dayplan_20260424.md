# Sprint 2 Day-by-Day 計画（2026-04-24 策定）

**策定日**: 2026-04-24 07:40 JST
**前提**: Sprint 1-B Phase B 区切り（commit `2e35d14`）・ゆうさくさん判断 C 採択
**目標**: paper 開始条件整備 + r1-r7 負債解消 + 4/28〜04-30 頃 paper 開始

---

## Day 1（2026-04-24 〜 04-25）: 基盤整備

### AM（着手可能・前半 4h）

| task | carryover ID | 担当 agent | 見積 |
|---|---|---|---|
| moomoo OpenD 事前調査結果レビュー | C-017 前段 | ゆうさくさん + secretary | 15 分 |
| allowlist hook 設計ドキュメント作成 | C-018 | builder | 1-2h |
| spy_bot.py / chronos_bot.py stash 扱い決定 | C-026 | secretary + ゆうさくさん判断 | 15 分 |
| common/ 未コミット差分整理 | C-021 | secretary | 30 分 |

### PM（後半 4h）

| task | carryover ID | 担当 agent | 見積 |
|---|---|---|---|
| allowlist hook 物理実装（chmod 0444 + chflags schg）| C-018 | builder + navigator 並走 | 2h |
| hook 実攻撃試行テスト（15+ ベクトル実試行形式）| C-018 検証 | builder | 1-2h |
| Redteam 独立検証（allowlist hook） | - | redteam | 30 分 |

### Day 1 完了条件
- allowlist hook 稼働・15+ 攻撃ベクトル全 block
- spy_bot / chronos_bot stash 処理決定
- common/ 差分 0 or 明示 carryover

---

## Day 2（2026-04-25 〜 04-26）: moomoo 接続実装

### AM

| task | carryover ID | 担当 agent | 見積 |
|---|---|---|---|
| atlas_v3/ops/moomoo_provider.py 新設（account/position/pnl）| C-017 | builder | 2-3h |
| OpenD 常駐化（launchd plist 追加）| C-017 | builder + ops | 30 分 |
| moomoo 認証・paper 口座接続テスト | C-017 | builder | 1h |

### PM

| task | carryover ID | 担当 agent | 見積 |
|---|---|---|---|
| moomoo_provider の単体テスト（mock）| C-017 | builder | 2h |
| moomoo 実 paper 口座接続テスト | C-017 | builder + ゆうさくさん | 1h |
| main.py `--provider moomoo` を production default に昇格 | C-017 | builder | 30 分 |
| Navigator + Redteam 並走検証 | - | navigator + redteam | 1h |

### Day 2 完了条件
- 実 Bot の PnL が取得できる
- paper 口座で発注 → 約定確認ループ
- yfinance は emergency fallback のみ

---

## Day 3（2026-04-26 〜 04-27）: 残件整理 + paper 開始準備

### AM: r1-r7 負債解消

| task | carryover ID | 担当 agent | 見積 |
|---|---|---|---|
| AST inspection テスト 4 箇所を実動作化 | C-019 | builder | 1h |
| silent except 明示化 | C-020 | builder | 30 分 |
| _probe_recovery 自動 KillSwitch 解除見直し（手動必須化） | C-022 | builder + navigator | 1h |
| LogRotator の MonitorDaemon 配線 | C-023 | builder | 30 分 |
| assert 境界条件検査導入 | C-025 | builder | 1h |

### PM: 最終検証

| task | 担当 | 見積 |
|---|---|---|
| pytest 全件走行 0 failed 確認 | builder → navigator | 30 分 |
| Redteam r1 (Sprint 2 初回) 独立検証 | redteam | 1h |
| Navigator 最終監査 | navigator | 30 分 |
| paper 起動 dry-run（launchctl load → 30 分監視）| ゆうさくさん + ops | 1h |

### Day 3 完了条件
- pytest 0 failed（pre-existing 除く）
- Redteam PASS
- paper 30 分 dry-run 成功
- Sprint 2 の DoD 全項目達成

---

## Day 4（2026-04-27 〜 04-28）: paper 開始

| task | 備考 |
|---|---|
| 本番 paper launchd 起動 | 09:30 ET (22:20 JST) |
| 初回監視（1 時間集中）| ゆうさくさん + ops |
| 監視 daemon 動作確認 | ダッシュボード (`192.168.10.123:8765`)|
| Pushover 通知経路確認 | - |
| EOD（end of day）集計 | 05:10 JST+1 |

---

## リスク・buffer

- **moomoo API 接続で詰まる場合**: Day 2 が 1-2 日延伸 → paper 開始 04-29 or 04-30
- **Redteam で再度致命指摘**: Day 3 に Builder r2 追加・1 日延伸
- **ゆうさくさん判断待ち項目**: stash 扱い・provider 選定（数分で可）

## 毎日の規律（r1-r7 教訓反映）

1. **Builder + Navigator 並走必須**（逐次運用禁止）
2. **既存コード改変 0 を毎日 git diff で確認**
3. **AST inspection テスト禁止・実試行形式必須**
4. **Redteam は実攻撃試行まで踏み込む**（関数存在確認は不可）
5. **CIA hook 出力を Builder が確認した証跡を残す**
6. **Normalization of Deviance 警戒**: symptom 対応でなく root design 変更優先

## 関連ファイル

- `data/sprint1_carryovers.md` C-017〜C-026
- `data/governance/definition_of_done.md`
- `memory/project_session_20260424_sprint1b_conclusion.md`
- `memory/feedback_navigator_mandatory_20260422.md`
- `memory/feedback_no_postpone_within_sprint_20260424.md`
