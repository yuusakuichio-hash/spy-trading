# Redteam 監査結果: data/prop_rules/mffu_flex.yaml bootstrap

**監査日**: 2026-04-23
**対象**: `data/prop_rules/mffu_flex.yaml`（P0 #5 成果物・26 行）
**仕様書**: `data/specs/v3/chronos_spec_v3_20260422.md` B5 R2b L144-L192
**Redteam 判定**: CONDITIONAL-PASS（CRITICAL 3・HIGH 17・MEDIUM 12・LOW 3）

---

## Sprint 0.5 Day 1 対応済（R1+R2）

| # | 項目 | 対応 |
|---|---|---|
| V-21 | force_close_et の TZ 読み違え（JST 解釈で MFFU 即失格）| yaml 値を `"16:00@America/New_York"` IANA 明示に変更 |
| 片輪デプロイ | yaml 単独で dry_run_guard hook 不在 | `.claude/hooks/mffu_dry_run_guard.sh` 実装（P0 #8・settings 登録済） |

---

## Phase 2 着手前に必須対応（持ち越し）

### CRITICAL

#### S-1: 転記忘れ silent live（Knight Capital 型）
- **発火条件**: Phase 2 Builder が「null → ハードコード $3,000 fallback」親切設計を入れる
- **影響**: MFFU 途中プラン改定見逃し・初回違反で account termination
- **Phase 2 実装要求**:
  - `chronos_v3/prop/mffu_flex.py` に `MFFURuleMissingError` class + yaml null 値検知
  - null 値で起動時 `MFFURuleMissingError` raise（silent default 拒否）
  - 契約テスト `chronos_v3/tests/test_mffu_yaml_bootstrap.py`

#### V-3: YAML tag injection（サプライチェーン攻撃）
- **発火条件**: `yaml.load()` 誤用 + 第三者 PR で `!!python/object` tag 混入
- **影響**: 任意コード実行
- **Phase 2 実装要求**:
  - pre-commit hook で yaml 内 `!!python` 検出
  - 全 parser で `yaml.safe_load` 強制の AST check

### HIGH（Phase 2 Sprint 1 着手前）

- **S-2** DST 遷移日境界: 2026-11-01 Fall back の金曜 force_close で 1 時間ズレ
- **S-3** yaml 改竄検知: hash 記録 + git blame 照合 CI check
- **S-5** 50 (int) vs 0.50 (ratio) 単位混乱: pydantic schema + 単位明示
- **St-1** null bootstrap 思想自体: `MFFURuleMissingError` で「キー不在 = 仕様違反」を強制
- **St-4** ダブル真実源: `legacy_import_block.sh` で `chronos_rules_plugin/mffu_flex.py` の chronos_v3 経路 import 禁止
- **B-4** source URL / fetched_hash 欠落: `source_urls` + `source_fetched_at` + `source_content_sha256` yaml に追加
- **O-1** Phase 2 実装真空: yaml と同日に `MFFURuleMissingError` skeleton 実装すべきだった（今回は hook #8 で代替）
- **O-3** 転記単一障害点: スクリーンショット保存 + 2 人目確認 or API 照合
- **V-6** key typo: `pydantic.BaseModel.model_config = ConfigDict(extra="forbid")`
- **V-9** tzdata 古さ: 起動時 `zoneinfo.ZoneInfo("US/Eastern")` オフセット既知値照合
- **V-10** atomic write: yaml 書換は `tempfile + os.replace()` パターンのみ
- **V-11** 同名 yaml 複数パス重複: 読込時 glob 重複検知 → 例外
- **V-17** test 環境の本番 yaml 汚染: pytest fixture は tmp_path コピー必須
- **V-19** 仕様追加耐性: `unknown_rules: dict` extensible schema + spec_drift watcher
- **V-20** 既存 silent default: `legacy_import_block.sh`（St-4 と同じ）
- **V-22** watcher 間隔: 24h scan + If-Modified-Since header poll
- **V-23** 逆転記攻撃: non-null → null への変更を git pre-commit hook でブロック

### MEDIUM / LOW

省略（元レポート参照・`data/governance/redteam_audit_mffu_flex_20260423_full.md` 相当の原文は task_notification ログに保存済み）

---

## 戦略的指摘（Phase 2 着手時に Builder / Navigator / Redteam 必読）

1. **「Navigator PASS は仕様書と文字列一致 PASS・安全 PASS ではない」**
   - 今後の Navigator 基準改定要件（yaml 内容妥当性・TZ/単位/schema 型まで検査）

2. **「yaml 単独先行 = Knight Capital 型片輪デプロイ」の警告**
   - 今回は #8 (mffu_dry_run_guard.sh) を同日実装で部分解消
   - ただし `MFFURuleMissingError` skeleton と `legacy_import_block.sh` は Phase 2 持ち越し

3. **「null bootstrap 思想」自体がアンチパターン**
   - 契約後に埋める値なら yaml にキー自体を存在させない設計も検討（キー不在検知）

---

## 関連ファイル

- `data/prop_rules/mffu_flex.yaml`（本件対象・V-21 修正済）
- `.claude/hooks/mffu_dry_run_guard.sh`（片輪解消・P0 #8）
- `data/specs/v3/chronos_spec_v3_20260422.md` B5 R2b
- `chronos_rules_plugin/mffu_flex.py`（既存・ダブル真実源側・Phase 2 で legacy_import_block 対象）
- Phase 2 Builder 着手時に `memory/project_session_20260423_*.md` と共に必読
