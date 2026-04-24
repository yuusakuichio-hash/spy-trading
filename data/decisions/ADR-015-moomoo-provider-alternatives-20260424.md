# ADR-015: ADR-014 代替案・ゆうさくさん承認要請（2026-04-24）

**Status**: Proposed（ゆうさくさん判断待ち）
**Supersedes**: ADR-014 Decision 1, 2 を部分的に再検討
**Date**: 2026-04-24
**Trigger**: Strategist 第三者検証（agent a943891a86064487e）+ Redteam r8 T-1 指摘

---

## 背景

ADR-014 で Secretary が自動採用した 3 判断のうち、Strategist 独立検証で以下が指摘された:

1. **Decision 1, 2 は独立でなく Decision 1 が SPOF**（Mac mini 死亡で 2/3 判断崩壊）
2. **Decision 2 はプライベート領域**（ゆうさくさん 24-48h 深夜再ログイン負担）を本人承認なしで確定
3. **Secretary 自動判断規律は bug 率判断のみに適用妥当**・Decision 1, 2 はライフスタイル判断で対象外

---

## 代替案 A: Mac mini + 物理強制 3 点セット（Decision 1 補強）

### 内容
1. **Mac mini は保持**（現状継続・auth_budget 抵触回避）
2. **caffeinate 物理強制**で夜間 sleep 禁止（`scripts/mac_caffeinate_daemon.sh`）
3. **Sentinel プロセス死活監視**で OpenD 死亡検知（認証失敗検知だけでなくプロセス死も）

### 実装
- `scripts/mac_caffeinate_daemon.sh`（作成済・2026-04-24）
- launchd plist 化検討（`com.soralab.caffeinate.plist`）
- OpenD 死活監視は Sentinel（非 LLM Python daemon・`scripts/dead_man_switch.py`）拡張

### bug 発生率
- 夜間 sleep 起因の daemon 落ち: 極低（caffeinate で物理保証）
- OpenD 死活見逃し: 低（Sentinel で追加監視）

---

## 代替案 B: yfinance auto-fallback default 化（Decision 2 補強）

### 内容
ADR-014 Decision 2 の「fail-closed」を「yfinance auto-fallback を default 動作に昇格」に変更:

1. moomoo AuthenticationError 発生 → **yfinance に auto fallback**（監視継続・代理 PnL で時間稼ぎ）
2. Pushover 通知は **quiet_hours 外で遅延送信**（深夜叩き起こし回避）
3. ゆうさくさん戻り次第 手動再ログイン

### 利点
- 深夜 3am の Pushover 鳴動回避（プライベート領域尊重）
- 監視ゼロ状態の回避（fallback で代理 PnL 継続）
- 24-48h 期限切れに対してゆうさくさんの都合で対処

### 実装
- `atlas_v3/main.py` で moomoo AuthenticationError catch → yfinance provider に差し替え
- Pushover client の quiet_hours ロジック確認（既存機能）

### bug 発生率
- 深夜叩き起こし: 極低
- 代理 PnL 監視の精度低下: 中（一時的・手動再ログインまで）

---

## 代替案 C: ADR-014 のまま Secretary 自動採用を尊重

### 内容
Strategist 指摘を受けた上で、ゆうさくさんが「Secretary 判断で OK」と明示すれば ADR-014 そのままで進行。

### リスク
- 深夜 Pushover による生活サイクル影響
- Mac mini SPOF 対策なし
- プライベート領域抵触の疑い残存

---

## 判断要請

**ゆうさくさんの承認必要**: 以下どれか明示してください:

| 選択 | 採用 | 影響 |
|---|---|---|
| **A + B 両方採択（推奨）** | Mac mini + caffeinate + Sentinel / yfinance auto-fallback + 遅延 Pushover | Decision 1/2 の弱点解消 |
| A のみ採択 | Mac mini 強化のみ | Decision 2 は ADR-014 のまま |
| B のみ採択 | yfinance fallback のみ | Decision 1 は ADR-014 のまま |
| C 継続 | ADR-014 そのまま | Secretary 判断全尊重 |
| D 再設計 | 別方針（例: VPS 移行前倒し等） | ゆうさくさん指示 |

---

## 関連

- `data/decisions/ADR-014-moomoo-provider-scope-20260424.md`（対象）
- Strategist 報告（agent a943891a86064487e）
- `scripts/mac_caffeinate_daemon.sh`（代替案 A の構成要素・作成済）
- `scripts/sprint2_precondition_check.sh`（T-1 前提崩壊検知）
- `memory/feedback_bug_rate_auto_decide_no_question_20260423.md`（Secretary 自動採用規律）

## 影響予定ファイル（採択時）

- 代替案 A: `scripts/dead_man_switch.py` 拡張・`~/Library/LaunchAgents/com.soralab.caffeinate.plist` 作成
- 代替案 B: `atlas_v3/main.py` fallback logic / `common/pushover_client.py` quiet_hours 確認
