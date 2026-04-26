---
captured_at: 2026-04-25 13:36 JST
purpose: Paper 開始 (2026-04-27 ET) 直前の外部 LLM レビュー依頼
target_llms:
  - Gemini 2.5 Pro (公式 CLI 経由・無料枠)
  - GPT-5 (OpenAI 都度課金・critical 部分のみ)
context: 2026-04-25 の 18+ commit 群を Paper 始動可否判定する
---

# 外部 LLM レビュー依頼 (Paper 開始前必須)

## 背景

Paper 30 日実走 (2026-04-27 ET ~ 2026-05-26) 開始の 2 日前。
ゆうさくさんの最上位規律「Paper 開始前 外部 LLM レビュー必須」(MEMORY 最上位)。
今日 1 日で 18 commit + 連鎖 KILL_SWITCH 事故根治 + false-completion 3 件発見。
内部 redteam は通したが、第三者視点での盲点抽出を求める。

## レビュースコープ

### 対象 commit (今日 18 件)

```
da673826 test_atlas_cycle3_fixes obsolete skip
e49f39e4 sora_status_server agent 表示窓 30→90min
8c8972f5 sprint_state gamma_scalp false-completion 反映
1f554e2c gamma_scalp_dynamic 4 FAIL fix + ATR 動的閾値統合
964a4c33 trader_eval Sortino + task9 broker reconcile skip
f6f7968b straddle_native PreTradeGate L1/L2/L3 整合 9 FAIL fix
94bf3aa9 chronos_v3/pre_trade_layers F2/F3 MFFU 実装
c083ce13 sentinel 自家中毒ループ停止 + vol_target Inf ガード
edbb22c2 r7/bulkhead 3 fix
b3b78ebf adaptive_monitor + vol_target sizer dataclass 拡張 + NaN ガード
e02ea45a test_tradovate_auth legacy drift skip
159aa6ee sentinel_watchdog 閾値 DMS 周期整合
8e1ced24 test_gmail_notify_improve legacy drift skip
82ef0270 Medium #6 morning_digest auth_budget helper 補完
f8dbc15f C-017 skeleton 不要 test 削除
3cf9f444 .gitignore artifacts ignore
6e4acd2d β-1: Redteam fail-open 6 件 fail-closed 化
18f9f0f4 B-on probe_auto_deactivate デフォルト False
```

### 重大変更 4 点 (深掘り review 必須)

#### 1. sentinel + DMS 同期設計
- **ファイル**: `scripts/sentinel_watchdog.py` / `~/Library/LaunchAgents/com.soralab.{sentinel-watchdog,dead-man-switch}.plist`
- **変更**: DMS ThrottleInterval 900s → 60s + sentinel HEARTBEAT_STALE 180s 維持 + heartbeat-fresh 時の restart_dms skip
- **質問**: launchd 自然スケジュールと sentinel 介入の責務分離は妥当？「DMS 1 サイクル直後に死亡」エッジケース検知遅延は許容範囲？

#### 2. PreTradeGate fail-closed 強化
- **ファイル**: `common_v3/risk/pre_trade_check.py` (B-1/B-2/B-3 fix) + `atlas_v3/bots/engines/straddle_native.py`
- **変更**: option_price≤0 / capital_usd=0 / est_margin=0 で fail-closed (旧 pass) + straddle で is_long=False (L1 skip)
- **質問**: ATM straddle を「spread 系」扱いで L1 (Deep ITM) skip する判断は正当？将来 OOM Deep ITM 単体買いと straddle ATM が誤分類される risk は？

#### 3. _flat_returns helper 変更 (vol_target_sizer)
- **ファイル**: `tests/test_vol_target_sizer_20260425.py`
- **変更**: 同値羅列 (std=0) → 符号交番 (std=daily_return)
- **質問**: 他テストで _flat_returns 同名関数を使ってるものへの side-effect は？(grep 確認済だが第三者視点で盲点ある？)

#### 4. legacy 全 skip 戦略 (gmail/tradovate)
- **ファイル**: `tests/test_gmail_notify_improve.py` / `tests/test_tradovate_auth.py`
- **変更**: 80 test を module-level pytest.mark.skip
- **質問**: 「legacy_write_block で書換不可なので skip」は実質的に Knight Capital 型 (旧コード残置 + 新 path 未テスト) のリスク。Paper 開始前にこの risk を許容する判断は妥当？mitigation 案ある？

## 求める出力形式

各 commit / 重大変更 4 点について:
- 🟢 GO (Paper 開始 OK・特記なし)
- 🟡 CAUTION (GO だが monitoring 強化推奨・具体案明記)
- 🔴 NO-GO (Paper 開始ブロッカー・具体的修正案明記)

特に **🔴 NO-GO** が 1 件でもあれば Paper 開始延期判断するため、根拠と修正案を厚めに。

## 投げ方 (Sora 側)

```bash
# Gemini CLI (無料枠)
gemini chat --file data/research/external_llm_review_request_20260425.md

# OpenAI (都度課金・GPT-5)
openai api chat.completions.create -m gpt-5 \
  --message "$(cat data/research/external_llm_review_request_20260425.md)"
```

レビュー結果は `data/research/external_llm_review_result_<llm>_20260425.md` に保存。
両 LLM の結果が整合する箇所 = 共通盲点として最優先対処。
