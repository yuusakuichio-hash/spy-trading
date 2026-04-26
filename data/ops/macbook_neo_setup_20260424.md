# MacBook NEO 初期セットアップ（2026-04-24 策定）

**策定日**: 2026-04-24
**対象**: MacBook NEO（ゆうさくさんが 2026/3 購入・法人設立時に個人→法人譲渡予定）
**用途**: Mac mini（現運用機）のサブ / 外出先作業 / Mac mini 障害時 fallback

---

## 優先順序（所要時間）

### 最優先（15-20 分・これだけで dashboard 閲覧可）

| # | 作業 | 所要 |
|---|---|---|
| 1 | **Tailscale インストール**（pkg ダブルクリック → Apple ID login） | 3 分 |
| 2 | Safari で Mac mini の Tailscale IP にアクセス → ダッシュボード閲覧 | 1 分 |

→ これだけで MacBook NEO から外出先でも dashboard が見られる

### 準優先（30 分・開発環境構築）

| # | 作業 | 所要 |
|---|---|---|
| 3 | **Claude Code インストール**（`brew install claude` or 公式）+ Max Plan login | 5 分 |
| 4 | `git clone` で trading repo 複製 | 3 分 |
| 5 | `~/.claude/projects/-Users-yuusakuichio-trading/memory/` を Mac mini から同期（scp or rsync）| 5 分 |
| 6 | `~/.zshrc` に alias 追加（cj / ct / tls）| 2 分 |
| 7 | Pushover 通知設定の同期（環境変数 or `.env`）| 3 分 |
| 8 | `scripts/sora_status_server.py` の LAN 版設定 | 3 分 |

→ 開発・追加作業が MacBook NEO で完結可能に

### 冗長化 / fallback 用（60 分・Mac mini 故障時用）

| # | 作業 | 所要 |
|---|---|---|
| 9 | moomoo OpenD インストール（必要時のみ・paper 運用開始後）| 10 分 |
| 10 | futu-api pip install | 2 分 |
| 11 | LaunchAgent plist 複製（supervisor / status-server 等）| 15 分 |
| 12 | launchd bootstrap | 5 分 |
| 13 | ~/sora_journal/ 同期 | 5 分 |
| 14 | backup 対象 archive 同期（data/archive/）| 20 分 |

→ Mac mini 死亡時に MacBook NEO へ cut over 可能

---

## やらなくていいこと（MacBook NEO では不要）

- moomoo OpenD 常駐（Mac mini 側のみで OK）
- Bot 本体の常駐（paper 運用中は Mac mini）
- cloudflared 常駐（外出先 URL は Mac mini 側で提供中）
- caffeinate（MacBook は蓋閉じれば sleep 可）

---

## 最小構成（所要 5 分・とにかく外出先で見るだけ）

**1. Tailscale インストール**（`/Users/yuusakuichio/Downloads/sprint2_assets/Tailscale.pkg` を MacBook NEO に airdrop or iCloud 経由でコピー → ダブルクリック）

**2. Safari で Tailscale 経由 Mac mini IP:8765 にアクセス**

この 2 ステップだけで MacBook NEO から dashboard が見られる状態。

---

## 同期するメモリ / 設定（Mac mini → MacBook NEO）

### 同期必須
- `~/.claude/projects/-Users-yuusakuichio-trading/memory/` （memory 本体）
- `~/.zshrc`（alias）
- `~/trading/` （git clone でよい・.gitignore 対象は個別コピー）

### 同期禁止（個別保持）
- `~/.claude/sessions/`（セッション固有）
- `~/sora_journal/jsonl/`（ジャーナル領域隔離規律）
- `.env` 系 secrets（個別で環境変数として再設定推奨）

---

## 同期方法案（ゆうさくさん選択）

| 案 | 手段 | メリット | デメリット |
|---|---|---|---|
| A | Tailscale + rsync | 必要時 pull 可・自動化可 | Tailscale 設定必要 |
| B | iCloud Drive 共有 | GUI 簡単 | memory 量で iCloud 容量逼迫 |
| C | Git 経由（公開不可フォルダは別）| 履歴残る | secrets 誤 commit リスク |
| D | 手動 AirDrop | 完全制御 | 面倒・忘却リスク |

**推奨 A**: Tailscale + rsync。Mac mini ↔ MacBook NEO 間で必要時 pull。

---

## セキュリティ

- LLC 譲渡予定なので**個人 secrets を含む .env や APIkey は譲渡前に環境変数化して残さない**
- MacBook NEO に保存する memory は Sora Lab / project 関連のみ
- ジャーナル領域（身体・家族・音楽個人事情）は引き続き隔離

---

## 関連

- `memory/project_expense_optimization.md`（MacBook NEO 購入経緯）
- `memory/project_llc_establishment_20260419.md`（LLC 設立計画）
- `data/ops/iphone_terminal_setup_20260424.md`（iPhone Termius 設定）
