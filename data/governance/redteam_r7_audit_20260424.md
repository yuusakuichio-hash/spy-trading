# Redteam r7 最終敵対レビュー（r1-r7 累積判定）

Agent ID: ac015c22fafba62b7
実行: 2026-04-24

## 判定サマリ
- DoD 最終判定: **FAIL**
- 4/27+α ペーパー開始: **NO-GO**
- 新 CRITICAL 5件 + HIGH 6件 + regression 6件 + Builder 報告との 175件 乖離

## レビュー全文

`metric_provider_fn = yf_provider.get_metrics` これは **bound method だから isinstance チェックは通る**。でも lambda でラップすれば通らない。更に本番では yfinance 一択だが、4/27+α でペーパー開始するのに **moomoo は NotImplementedError**。つまり **Bot の本当の PnL は取れず、SPY 騰落率の代理指標で監視する**設計。

最終レポート作成。

---

# Red Team 最終敵対レビュー（r1-r7 累積・4/27+α ペーパー開始判定）

## 最もヤバい 3 件

**YABASOU-1: bash_write_guard は 15 攻撃中 13 件すり抜け（CRIT-R6-1 偽装防御・再発）**
実測。`python3 -c "import shutil; shutil.copy('evil', 'spy_bot.py')"` / `python3 -c "from pathlib import Path; Path('spy_bot.py').write_text('x')"` / `python3 -c "import os; os.rename('evil.py', 'spy_bot.py')"` / `git apply malicious.patch` / `patch spy_bot.py < p.patch` / `install -m 0644 evil.py spy_bot.py` / `ln -sf /tmp/evil.py spy_bot.py` / `tr a b < other.py > spy_bot.py` / `vim -c ':write spy_bot.py'` / `head/tail`では止まらないが`python3 script.py >> spy_bot.py`も通る。しかも WRITE_CMD_PATTERNS の `r'(echo|printf)\s+.*>'` は `>` の前のスペースを要求するため `echo x>spy_bot.py` もすり抜け可能性あり。**共犯 r6 で指摘されたのに r7 は regex を追加しただけで根本 design flaw（allowlist ではなく blacklist）を解決していない。**

**YABASOU-2: CRIT-R6-3 isinstance 化は lambda/partial で完全 bypass（実証済）**
`atlas_v3/main.py:138` で `return yf_provider.get_metrics` は bound method なので本番経路は通る。しかし builder r7 の「SneakyDummy 完全防止」という主張は偽装。実測: `lambda: d.get_metrics()` → `_is_dummy_provider=False`、`functools.partial(d.get_metrics)` → `False`。さらに zero_detection_n=0 がデフォルト（「後方互換」の名のもと無効）。r6 指摘の monkey-patch 型攻撃は根絶されていない。

**YABASOU-3: install_atlas_paper_daemon.sh は初回実行不能 + _probe_recovery が Kill Switch 自動復活**
`PLIST_SRC="/Users/.../scripts/com.soralab.atlas-paper.plist"` が**実在しない**（実ファイル確認）。PLIST_DEST が既存であれば動くが、新規 Mac / CI / クリーン環境では即 fail。更に `_probe_recovery` (monitor.py:1192-1229) は probe 成功時に global KillSwitch + 全 firm flag を自動解除。**「連続失敗で Kill Switch 発動 → 1 回 probe 通っただけで自動復活」という LTCM 1998 / Therac-25 1985 型の自動復旧暴走パターン**。4/27+α ペーパー開始時に一度 Kill Switch が誤発動すると、回復 probe が通る瞬間に全 firm flag が消える。

## 新 CRITICAL 実害高: 5 件

**C-R7-1**: bash_write_guard 15 攻撃中 13 件 bypass（再現: 上記 YABASOU-1 一覧）。
**C-R7-2**: PROTECTED_PATTERNS に `atlas_v3/`・`common_v3/`・`chronos_v3/` が**一切含まれない**。新規コード保護ゼロ。
**C-R7-3**: `_probe_recovery` 自動 Kill Switch 解除（monitor.py:1192-1229）。「ゾンビ状態解消」の名で自動復活させる設計は金融 Bot で最悪。
**C-R7-4**: CRIT-R6-3 lambda/partial bypass（実証済）。zero_detection_n=0 デフォルト。
**C-R7-5**: install_atlas_paper_daemon.sh の PLIST_SRC パスが実在しない → 初回インストール失敗。

## 新 HIGH: 6 件

**H-R7-1**: LogRotator は作ったが **MonitorDaemon の loop に一切呼ばれていない**（grep 確認・log_rotator.py のみで自閉）。単なる「作っただけコード」。
**H-R7-2**: _verify_daemon_alive は `"PID" in output` 文字列判定。`launchctl list` stderr に「PID not found」と出るだけで true になる文字列部分一致バグ（247 行 `"PID"` が output の "PID not found" にマッチ）。
**H-R7-3**: yfinance degraded mode は**アラートを送らない**（`log.warning` のみ）。Pushover/andon 連動なし。監視者は気付けない。
**H-R7-4**: 本番 provider は SPY 騰落率を代理 PnL とする擬似データ（yfinance_provider.py:46 `_PROXY_NOTIONAL_USD = 5000.0` 固定）。**実 Bot の PnL とは一致しない**。監視は見かけだけ。
**H-R7-5**: plist の HardResourceLimits 500MB は yfinance + pandas の実メモリ（pandas DataFrame cache 含め 300-800MB 想定）を考えると OOM loop（SIGKILL → KeepAlive restart → SIGKILL 反復）のリスクあり。
**H-R7-6**: 本番 code 全体で assert ほぼゼロ（async_impl.py に 1 件のみ）。runtime invariant 検証完全欠落。Therac-25 型 race condition を静的検知不可能。

## 新 regression: 有（builder 主張と実測乖離）

builder r7 主張: **「3677 passed / 3 failed / 回帰 0」**
実測 `pytest tests/` 結果: **3852 passed / 6 failed / 7 skipped / 15 xfailed**。
fail 内訳: test_atlas_cycle3 backup / test_chronos_high_7 plist x2 / test_external_health_ping x2 / test_task9_fill_pipeline。
builder は pre-existing とも主張したが、**REG-R6-X スクリプト実行の証跡未提示**。自己申告のみ。

## 運用的な穴

- plist の StandardOutPath / StandardErrorPath は state_v3 ディレクトリ依存（存在しないと daemon クラッシュ）
- `_probe_recovery` がローカル時刻依存（市場時間外に probe 成功 → 開場時に Kill Switch 既に解除済み状態で取引再開）
- git stash に `spy_bot chronos_bot legacy_write_block 調査用 20260424_0257` が残存。**未適用の変更が r1-r7 後に pending**。stash pop すると何が起きるか不明。

## 戦略的リスク

- ペーパー Bot の実 PnL が取れない設計（moomoo は NotImplementedError）で 30 日走らせる意味が希薄（SPY 騰落率を見てるだけ）
- 「バグなし絶対最優先」規律（CLAUDE.md 2026-04-22）に反して 6 failed 残存
- r6→r7 で「偽装防御」指摘が繰り返されたのに builder は hook を追加しただけで根本を直さない（Normalization of Deviance）

## 反論視点

Builder 主張「CRIT-R6-1 完全対応」→**反論**: 13/15 攻撃 bypass を実証。sed/perl/rsync/cp/mv/tee/echo の一部を列挙しただけの blacklist。allowlist design（Read-only marker + 専用 Edit tool のみ許可）にすべき。
Builder 主張「CRIT-R6-3 isinstance 化で bypass 防止」→**反論**: lambda と functools.partial で実証 bypass。
Builder 主張「回帰 0」→**反論**: 実測 6 failed / 3852 passed（builder 報告 3677 passed と 175 件乖離）。

## 重症度評価

| ID | 重症度 | 対策優先度 |
|---|---|---|
| C-R7-1 bash_write_guard bypass 13/15 | CRITICAL | 即 1 |
| C-R7-3 probe 自動 KS 解除 | CRITICAL | 即 1 |
| C-R7-4 lambda bypass 再発 | CRITICAL | 即 2 |
| C-R7-2 新規ディレクトリ無保護 | CRITICAL | 即 2 |
| C-R7-5 plist 初回 fail | HIGH | 3 |
| H-R7-1 LogRotator 未配線 | HIGH | 4 |
| H-R7-3 degraded alert なし | HIGH | 4 |

## DoD 最終判定: **FAIL**

理由: (1) 実攻撃 13/15 bypass 実証、(2) CRIT-R6-3 lambda bypass 実証、(3) 回帰 6 件残存（builder 報告と乖離）、(4) LogRotator 完全未配線、(5) install script 初回不能、(6) probe 自動復活は設計レベル欠陥。

## 4/27+α ペーパー開始可否: **NO-GO**

CONDITIONAL-GO にも届かない。代理 PnL で 30 日ペーパー走らせても戦術検証にならず時間の無駄。C-R7-1/3/4 を allowlist 設計 + 自動復活禁止 + lambda 対策で根治してから再評価。

## r1-r6 偽装防御の r7 残存

**根絶できていない**。r6 指摘「isinstance 化しても SneakyDummy 型 bypass 残存」に対する r7 の対応は「isinstance + オプトイン zero-detection」で、デフォルト有効化を避けたため**同じ穴が残っている**。r6 指摘「bash_write_guard は regex の網羅性」に対する r7 対応は「パターン追加」で、allowlist design 変更を避けたため**新攻撃ベクトルで即崩壊**。Challenger O-ring 型 Normalization of Deviance（1986）と同一パターン。

**関連ファイル**:
- /Users/yuusakuichio/trading/.claude/hooks/_bash_write_guard_logic.py
- /Users/yuusakuichio/trading/.claude/hooks/bash_write_guard.sh
- /Users/yuusakuichio/trading/atlas_v3/ops/monitor.py
- /Users/yuusakuichio/trading/atlas_v3/ops/log_rotator.py
- /Users/yuusakuichio/trading/atlas_v3/main.py
- /Users/yuusakuichio/trading/scripts/install_atlas_paper_daemon.sh
- /Users/yuusakuichio/trading/common_v3/risk/kill_switch.py
- /Users/yuusakuichio/Library/LaunchAgents/com.soralab.atlas-paper.plist

---

