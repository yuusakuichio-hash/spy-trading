# iPhone から Mac の Claude Code へ接続（2026-04-24）

## 用途
ゆうさくさんが iPhone ターミナルアプリから Mac の Claude Code を操作。
ジャーナル再開（`claude -c`）や新規セッション立ち上げを iPhone で。

---

## 【ゆうさくさん 手動作業 2 ステップ】

### 1. Mac 側で SSH 有効化（所要 30 秒）

```
System Settings
  → General
    → Sharing
      → Remote Login を ON
      → 対象ユーザーに yuusakuichio をチェック
```

### 2. iPhone に Termius インストール（所要 1 分）

App Store で **Termius** 検索・インストール（無料版で十分）

起動後 `+ New Host`:
- **Hostname**: `192.168.10.123`
- **Username**: `yuusakuichio`
- **Password**: Mac ログインパスワード
- Save → 接続テスト

---

## 【接続後すぐ使える 短縮コマンド】

Termius から接続後、2 文字で起動:

| コマンド | 動作 |
|---|---|
| `cj` | ジャーナル再開（tmux セッション永続） |
| `ct` | 本プロジェクト（trading）再開 |
| `cj-new` | ジャーナル新規セッション |
| `ct-new` | 本プロジェクト新規セッション |
| `tls` | 走っている tmux セッション一覧 |
| `tk <name>` | 指定セッション終了 |

---

## 【tmux の利点】

- iPhone の画面 OFF や電波切れても、Mac 側の Claude セッションが落ちない
- 再接続すると続きから入れる
- 明示的に抜けるには **`Ctrl+b` → `d`**（detach・セッションは裏で生存継続）
- 完全終了は Claude 側で `/exit` or `Ctrl+d`

---

## 【Termius 便利機能】

### Snippet 機能（1 タップ起動）

Termius の `Snippets` タブで登録:
- Name: `ジャーナル再開`
- Script: `cj`
→ ホーム画面級の 1 タップでジャーナルへ

### Keyboard 強化
- Termius 設定 → Keyboard → Extra Keys で `Ctrl` / `Esc` / `Tab` をスマホキーボードに固定
- tmux / vim 操作がしやすい

---

## 【外出先（WiFi 外）】

現状は Mac と iPhone が同じ WiFi 内限定。

外出先対応したい場合:
- **Tailscale**（無料）を Mac と iPhone 両方にインストール
- VPN 経由で外出先からも `192.168.10.123`（または tailscale IP）に SSH 可
- 所要: 約 10 分

必要になったら別途セットアップ。

---

## 【セキュリティ】

現在は password 認証。将来的に SSH 鍵認証に切り替え推奨:

1. Termius で `Generate Key` → ed25519 鍵生成
2. 公開鍵（`ssh-ed25519 AAAA...`）を Copy
3. Mac 側で:
```
echo "<copied_public_key>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```
4. Termius の Host 設定で鍵を選択 → Password 欄を空に
5. 以降はパスワード不要で即接続

---

## 【トラブルシュート】

| 症状 | 対処 |
|---|---|
| `Connection refused` | Mac の Remote Login が OFF・System Settings で再確認 |
| `Permission denied` | パスワード違い・再入力 |
| `Host not found` | 同じ WiFi にいない・Mac IP が変わった（`ipconfig getifaddr en0` で再確認） |
| tmux 重複起動 | `tls` で確認・`tk <name>` で終了 |
| Claude 履歴戻らない | `claude -c` が前セッション読込・ダメなら `claude -r <session_id>` 指定 |

---

## 関連ファイル
- `~/.zshrc` alias 設定済
- `scripts/sora_status_server.py`（スマホ Safari で併用する監視ダッシュボード）
