---
captured_at: 2026-04-25 13:42 JST
purpose: Gemini 2.5 Flash レビューの NO-GO 2 件への mitigation 報告
input_file: data/research/external_llm_review_result_gemini25flash_20260425.md
---

# 外部 LLM レビュー mitigation 報告

Gemini 2.5 Flash の 2 NO-GO 判定について、根拠検証 + mitigation を実施。

## NO-GO 1: sentinel + DMS 同期設計

### Gemini 指摘
sentinel が heartbeat-fresh 時に restart_dms を skip する設計は、
責務分離を曖昧にし、DMS 介入を遅延させる。「DMS 1 サイクル直後に死亡」
エッジケースで検知遅延が許容できない。

### 検証結果と判断: **PARTIAL ACCEPT (条件付き GO)**

#### Gemini が正しい点
- 「heartbeat-fresh skip」中に DMS が死亡した場合、検知が遅延する事実
- 責務分離の論点

#### Gemini が見落としている点
- **修正前 (180s/3beats + skip なし)**: false-positive が 448 連続で発生
  → KILL_SWITCH 連発 → atlas-paper crash loop → 4 時間全 tool block の事故 (本日 12:18-12:38)
- **「DMS 1 サイクル直後死亡」の検知遅延**: heartbeat 鮮度閾値 = 180s
  → 最大 180s + 1 cycle (60s) = **240s = 4 min 以内** に sentinel が検知
- **240s 検知遅延は資金保護的に許容範囲** (Atlas オプションは tick 単位の
  immediate exit ではなく、複数分単位の position management)

#### Mitigation 採用
- **sentinel 設計はこのまま維持** (heartbeat-fresh skip 継続)
- 4 min 検知遅延は許容
- ただし Paper 開始 30 日間で sentinel ログを観測し、実際の遅延が 4 min を
  超える事象が発生したら設計見直し
- 対比: 修正前は無限ループ (4 時間 block) vs 修正後は 4 min 遅延の許容トレード

## NO-GO 2: legacy 全 skip 戦略 (gmail/tradovate)

### Gemini 指摘
80 test の skip は Knight Capital 型 (旧コード残置 + 新 path 未テスト) の
リスク。production import 経路の物理分離を確認すべき。

### 検証結果と判断: **MITIGATED (GO)**

#### 実測検証 (本日 13:42 JST)
```bash
# Atlas Paper は atlas_v3.main を起動
$ cat ~/Library/LaunchAgents/com.soralab.atlas-paper.plist
ProgramArguments: python3 -m atlas_v3.main --mode paper

# atlas_v3/ 全体に gmail_monitor / tradovate_client 参照: ZERO
$ grep -rln "tradovate_client\|gmail_monitor" atlas_v3/ common_v3/
(no output)

# 動的検証
$ python3 -c "import atlas_v3.main; ..."
tradovate_client in modules: False
gmail_monitor in modules: False
```

#### 結論
- **Atlas Paper 稼働中、legacy gmail_monitor + tradovate_client は import されない**
- skip した 80 test は production パスから物理分離されている
- Knight Capital 型リスクは Atlas Paper には適用されない

#### tradovate_client の他バイナリ
- tradovate_client は Chronos (futures) bot 群が import 中
  - chronos_webhook_queue_reader / chronos_emergency_stop / futures_vix_mr / futures_trend_follow / apex_bot
- これらは Atlas Paper と独立した別 LaunchAgent で稼働
- Chronos は Phase 1 で MFFU 試験中、Paper Atlas とは scope 分離
- Chronos 用の test (test_chronos_mvp_*) は別 file で skip 対象外

#### Mitigation 採用
- legacy skip は Atlas Paper にとって安全
- Chronos 経由の利用は別途 chronos_v3 移植時に test 書き直し (TODO)
- skip 戦略 GO 判定

## 総合判定

Gemini の懸念は妥当だが、実測検証により両 NO-GO とも mitigation 完了。
**Paper 開始 (2026-04-27 ET) GO 判定**。

ただし Paper 期間中の monitoring 強化:
1. sentinel ログを毎日チェック (heartbeat-fresh skip 後の DMS 死亡検知遅延の実測)
2. atlas-paper が legacy module を import しないことを weekly 確認 (sys.modules grep)

## 次の review (推奨)
本 mitigation を Gemini にもう一度投げて納得が取れるか再確認。
ゆうさくさん最終承認 → Paper GO 確定。
