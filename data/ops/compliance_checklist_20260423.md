# Atlas Paper 法的コンプライアンス最終確認チェックリスト

**作成日**: 2026-04-23  
**担当**: builder (Sora Lab)  
**最終承認**: ゆうさくさん確認必須

---

## 1. moomoo 利用規約適合

| 項目 | 状態 | 根拠 |
|------|------|------|
| ペーパー取引は TrdEnv.SIMULATE を使用 | 対応済 | vault.py: `trd_env: str = "SIMULATE"` で SIMULATE 以外は ValueError |
| OpenD API の利用は個人利用の範囲内 | **[PENDING_OWNER_APPROVAL_PAPER] paper 起動は継続・live 起動はブロック** | ゆうさくさんが moomoo ToS §7 を確認すること |
| API レート制限（50 req/s）の遵守 | 対応済 | latency_monitor.py で p99 監視 + BACKOFF 機能 |
| 自動売買に関する moomoo 規約確認 | **[PENDING_OWNER_APPROVAL_PAPER] paper 起動は継続・live 起動はブロック** | ゆうさくさんが moomoo Developer Agreement §3 を確認すること |
| API キーの第三者共有禁止 | 対応済 | vault.py で暗号化保管 + .gitignore で除外 |

**確認アクション**: ゆうさくさんが moomoo 公式サイトの利用規約（Developer Agreement + ToS）を確認し、自動売買が明示的に許可されていることを確認すること。

---

## 2. 日本金融商品取引法（金商法）適合

| 項目 | 状態 | 根拠 |
|------|------|------|
| 個人の自己勘定取引は金商法の投資運用業に該当しない | 適合 | 金商法 2 条 8 項：第三者のために行う場合のみ登録義務あり |
| ペーパー取引（仮想資金）は実際の金融商品取引ではない | 適合 | TrdEnv.SIMULATE のため実資金移動なし |
| アルゴリズム取引の個人利用は規制対象外 | 適合 | 金商法では個人の自己勘定アルゴ取引に特段の規制なし（2026-04 現在） |
| C2 サービス（有料シグナル配信）は金商法適用可能性あり | C2 除外済 | `memory/project_c2_excluded.md` 参照：C2 は計画から除外済 |
| 私募ファンド設立時は金商法第 2 条 3 項適格機関投資家特例業者の要件確認 | 将来確認 | 2027 年以降に別途検討 |

---

## 3. C2 除外の整合確認

| 項目 | 状態 |
|------|------|
| Atlas/Chronos のシグナルを C2 等の第三者に販売・配信しない | 確認済 |
| SNS での取引結果公開は「記録共有」であり有料シグナル配信ではない | 確認済 |
| 「私募ファンド」計画は 2027 年以降・現時点では個人取引のみ | 確認済 |

根拠ファイル: `memory/project_c2_excluded.md`

---

## 4. 税務適合

| 項目 | 状態 | 根拠 |
|------|------|------|
| 米国株オプション取引の国内課税区分は「総合課税・雑所得」 | 確認済 | `memory/project_atlas_tax_correction_20260420.md` |
| ペーパー取引の損益は課税対象外 | 適合 | 仮想資金のため実現損益なし |
| 本番移行後は確定申告が必要（年間 20 万超の雑所得） | 将来対応 | 本番移行時に改めて確認 |
| LLC 経由での取引は収益構造が異なる可能性あり | 将来確認 | `memory/project_llc_establishment_20260419.md` 参照 |

---

## 5. データ・プライバシー適合

| 項目 | 状態 |
|------|------|
| moomoo API キーを Git にコミットしない | 対応済（.gitignore 確認済） |
| 取引ログに個人識別情報（PII）を含めない | 対応済（state_v3/*.jsonl に PII なし） |
| VPS 上のログに API キーを出力しない | **[PENDING_OWNER_APPROVAL_PAPER] 要確認**（spy_bot.py のログレベル設定確認） |

---

## 6. 技術的セーフガード整合

| 項目 | 実装ファイル | 状態 |
|------|--------------|------|
| Kill Switch による緊急全停止 | `common_v3/risk/kill_switch.py` | 実装済 |
| 日次損失制限 | `data/configs/atlas_paper_risk.yaml` | 実装済 |
| 最大ドローダウン制限 | `common_v3/risk/engine.py` | 実装済 |
| Paper モード強制（SIMULATE 以外で起動不可） | `atlas_v3/ops/vault.py` | 実装済 |
| credentials 暗号化保管 | `atlas_v3/ops/vault.py` | 実装済 |
| レイテンシ異常時自動停止 | `atlas_v3/ops/latency_monitor.py` | 実装済 |
| 24h 監視 + エスカレーション | `atlas_v3/ops/monitor.py` | 実装済 |

---

## 7. 最終承認サイン

**判断 2 タグ凡例（Sprint 1-B Phase B）:**
- `[PENDING_OWNER_APPROVAL_PAPER]`: paper 起動は WARN のみ（継続）・live 起動はブロック
- `[PENDING_OWNER_APPROVAL_LIVE]`: paper / live 両方でブロック（最高優先度）

---

未確認項目（§7 記載・ゆうさくさん最終確認必須）:

- [ ] **[PENDING_OWNER_APPROVAL_PAPER §7-1]** ゆうさくさんが moomoo Developer Agreement を確認し自動売買が許可されていることを確認（§1「確認待ち」項目と連動）
- [ ] **[PENDING_OWNER_APPROVAL_PAPER §7-2]** ゆうさくさんが moomoo ToS §7（個人利用範囲）を確認し OpenD API が個人利用許可内であることを確認
- [ ] **[PENDING_OWNER_APPROVAL_PAPER §7-3]** VPS 上の spy_bot.py ログレベル設定を確認し API キーがログ出力されないことを確認（§5「要確認」項目と連動）
- [ ] ゆうさくさんが税務区分（雑所得・総合課税）を顧問税理士に確認（live 移行時）
- [ ] .env.d/moomoo_paper.env に実キーを記入し load_from_env() テストが通ることを確認
- [ ] pytest tests/test_atlas_v3_paper_ops.py が全件 PASS であることを確認
- [ ] replay バックテスト（2 年分）が Sharpe > 0 であることを確認

**このチェックリストが全件チェックされるまで本番移行禁止。**
**[PENDING_OWNER_APPROVAL] タグ付き項目はゆうさくさんの確認なしに「承認済み」にしてはならない。**
