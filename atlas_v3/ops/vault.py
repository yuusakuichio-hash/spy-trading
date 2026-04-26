"""atlas_v3/ops/vault.py — Paper API キー/シークレット vault 管理

責務:
- .env.d/moomoo_paper.env から Paper API credentials を読み込む
- cryptography.fernet で暗号化した credentials を data/state_v3/vault_paper.enc に保存/復号
- 実キーはこのファイルには記載しない（.env.d/ は .gitignore で除外済み）

公開 API:
    VaultError          — vault 操作エラーの基底例外
    PaperCredentials    — 復号された credentials dataclass (frozen=True)
    load_from_env()     — .env.d/moomoo_paper.env から読み込んで PaperCredentials を返す
    encrypt_to_disk()   — PaperCredentials を暗号化して data/state_v3/vault_paper.enc に保存
    decrypt_from_disk() — data/state_v3/vault_paper.enc を復号して PaperCredentials を返す

セキュリティ設計:
- Fernet キーは環境変数 VAULT_MASTER_KEY から読む（ファイルには保存しない）
- VAULT_MASTER_KEY 未設定時は encrypt_to_disk / decrypt_from_disk は VaultError を raise
- load_from_env() は暗号化不要（プロセス内メモリのみ・ディスクに書かない）
- str repr は credentials をマスクする
"""
from __future__ import annotations

import dataclasses
import logging
import os
import stat
from pathlib import Path
from typing import IO, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# パス定義
# ---------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parents[2]
_ENV_DIR = _BASE / ".env.d"
_ENV_FILE = _ENV_DIR / "moomoo_paper.env"
_STATE_DIR = _BASE / "data" / "state_v3"
_VAULT_FILE = _STATE_DIR / "vault_paper.enc"

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------

class VaultError(Exception):
    """vault 操作に関するエラー。"""


# ---------------------------------------------------------------------------
# PaperCredentials
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class PaperCredentials:
    """moomoo Paper API credentials。

    Fields:
        app_id:       moomoo OpenD AppID
        app_secret:   moomoo OpenD App Secret
        host:         OpenD ホスト（デフォルト: 127.0.0.1）
        port:         OpenD ポート（デフォルト: 11111）
        trd_env:      取引環境（"SIMULATE" 固定 — Paper モード）
    """
    app_id: str
    app_secret: str
    host: str = "127.0.0.1"
    port: int = 11111
    trd_env: str = "SIMULATE"

    def __post_init__(self) -> None:
        if not self.app_id.strip():
            raise VaultError("app_id must not be empty")
        if not self.app_secret.strip():
            raise VaultError("app_secret must not be empty")
        if self.port <= 0 or self.port > 65535:
            raise VaultError(f"port must be 1-65535, got {self.port}")
        if self.trd_env != "SIMULATE":
            raise VaultError(
                f"trd_env must be 'SIMULATE' for paper credentials, got {self.trd_env!r}"
            )

    def __repr__(self) -> str:
        masked_secret = (
            self.app_secret[:4] + "****"
            if len(self.app_secret) > 4
            else "****"
        )
        return (
            f"PaperCredentials(app_id={self.app_id!r}, "
            f"app_secret='{masked_secret}', "
            f"host={self.host!r}, port={self.port}, trd_env={self.trd_env!r})"
        )


# ---------------------------------------------------------------------------
# env ファイルパーサ
# ---------------------------------------------------------------------------

def _check_file_permissions(path: Path) -> None:
    """ファイルパーミッションを検査し、world-readable なら VaultError を raise する（CRITICAL 2）。

    RT-R2-006 修正: symlink の場合は実体（realpath）の親ディレクトリも検査する。
    symlink 自体のパーミッションではなく実体ファイル + 実体親ディレクトリのパーミッションを確認。

    要件:
    - ファイル: 0600 のみ許可（owner read/write のみ）
    - ディレクトリ: world-executable/readable でないこと（0700 推奨）

    Args:
        path: 検査対象ファイルパス

    Raises:
        VaultError: パーミッションが 0600 以外（world/group readable）
    """
    if not path.exists():
        return  # 存在しない場合は呼び出し元が検査済み

    file_mode = path.stat().st_mode
    # other (world) の read ビット: S_IROTH = 0o004
    # group の read ビット: S_IRGRP = 0o040
    if file_mode & (stat.S_IRGRP | stat.S_IROTH):
        octal = oct(stat.S_IMODE(file_mode))
        raise VaultError(
            f"Insecure file permissions on {path}: {octal}. "
            "Must be 0600 (owner read/write only). "
            f"Fix with: chmod 0600 {path}"
        )

    # ディレクトリの world-readable/executable チェック（直接親）
    parent = path.parent
    if parent.exists():
        dir_mode = parent.stat().st_mode
        if dir_mode & (stat.S_IROTH | stat.S_IXOTH):
            octal = oct(stat.S_IMODE(dir_mode))
            raise VaultError(
                f"Insecure directory permissions on {parent}: {octal}. "
                "Recommended: chmod 0700 (owner only). "
                f"Fix with: chmod 0700 {parent}"
            )

    # RT-R2-006: symlink の場合は realpath の親ディレクトリも検査
    try:
        real_path = path.resolve()
        # resolve() で別パスになった = symlink だった
        if real_path != path and real_path.exists():
            real_parent = real_path.parent
            if real_parent.exists():
                real_dir_mode = real_parent.stat().st_mode
                if real_dir_mode & (stat.S_IROTH | stat.S_IXOTH):
                    octal = oct(stat.S_IMODE(real_dir_mode))
                    raise VaultError(
                        f"Insecure realpath parent directory permissions on {real_parent}: {octal}. "
                        f"({path} is a symlink pointing to {real_path}). "
                        "Recommended: chmod 0700 (owner only). "
                        f"Fix with: chmod 0700 {real_parent}"
                    )
    except VaultError:
        raise
    except Exception as e:
        log.warning("[Vault] symlink realpath check failed (non-critical): %s", e)


def _check_fd_permissions(fd: int, path: Path) -> None:
    """NEW-H-3: 既に開いている fd に対して os.fstat() でパーミッション再検査する。

    TOCTOU (time-of-check/time-of-use) 競合を排除するため:
    - _check_file_permissions(path) は path.stat() を使う（check 段階）
    - _check_fd_permissions(fd) は os.fstat(fd) を使う（use 段階・アトミック）
    - symlink 差し替え攻撃: open() 後に fd を保持しているため、
      攻撃者が symlink を差し替えても fd は元のファイルを指したまま。
      os.fstat(fd) は fd が指すファイルのパーミッションを返すため、
      symlink 経由の不正アクセスを検出できる。

    Args:
        fd:   open() した後の file descriptor (int)
        path: エラーメッセージ用のパス（表示のみ）

    Raises:
        VaultError: fd が指すファイルのパーミッションが 0600 以外
    """
    try:
        fd_stat = os.fstat(fd)
    except OSError as e:
        raise VaultError(f"fstat() failed on fd for {path}: {e}") from e

    fd_mode = fd_stat.st_mode
    if fd_mode & (stat.S_IRGRP | stat.S_IROTH):
        octal = oct(stat.S_IMODE(fd_mode))
        raise VaultError(
            f"Insecure file permissions (post-open fstat) on {path}: {octal}. "
            "Possible TOCTOU attack (symlink swap after stat). "
            "Must be 0600 (owner read/write only). "
            f"Fix with: chmod 0600 {path}"
        )


def _check_path_not_symlink(path: Path) -> None:
    """H2 fix: path 自体と親ディレクトリが symlink でないことを os.lstat() で確認する。

    O_NOFOLLOW は最終コンポーネントの symlink のみを防ぐ。
    しかし path のいずれかの中間コンポーネント（親ディレクトリ）が symlink の場合は
    O_NOFOLLOW では防ぎ切れない（例: data/ 自体が /attacker/data へのシンボリックリンク）。

    この関数は:
    1. path 自体が symlink でないこと（os.lstat().st_mode）
    2. 全親ディレクトリコンポーネントが symlink でないこと
    を事前に確認する。

    注意: この lstat チェックと O_NOFOLLOW open() の間に TOCTOU 競合が残るが、
    実用的な攻撃難易度は大幅に上がる（チェック段階で既に symlink を拒否）。
    O_NOFOLLOW による最終ガードと組み合わせることで二重防御を実現する。

    Args:
        path: 検査対象ファイルパス

    Raises:
        VaultError: path または親ディレクトリコンポーネントが symlink
    """
    # path 自体のチェック
    try:
        lstat_result = os.lstat(str(path))
    except FileNotFoundError:
        return  # 存在しない場合は呼び出し元が検査済み
    except OSError as e:
        raise VaultError(f"lstat failed on {path}: {e}") from e

    if stat.S_ISLNK(lstat_result.st_mode):
        raise VaultError(
            f"H2 fix: {path} is a symlink. "
            "Vault files must be regular files (not symlinks). "
            "Symlink of parent directory is also rejected to prevent directory-level TOCTOU."
        )

    # 親ディレクトリコンポーネントのチェック（data/ 自体が symlink の場合を防ぐ）
    parts = path.parts
    for i in range(1, len(parts)):
        parent_path = Path(*parts[:i])
        if not parent_path.exists():
            break
        try:
            parent_lstat = os.lstat(str(parent_path))
        except OSError:
            break
        if stat.S_ISLNK(parent_lstat.st_mode):
            raise VaultError(
                f"H2 fix: Parent directory component {parent_path} is a symlink. "
                f"Directory symlinks can redirect vault file access to attacker-controlled paths. "
                f"All path components of vault files must be real directories."
            )


def _parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 形式の .env ファイルをパースして dict を返す。

    NEW-H-3 (TOCTOU アトミック化) + HIGH-R4-1 (O_NOFOLLOW) + H2 fix (親ディレクトリ symlink 無防御修正):
    - H2 fix: _check_path_not_symlink() で path + 全親ディレクトリが symlink でないことを事前確認
    - O_NOFOLLOW フラグ付きで os.open() を呼び出すことで、
      symlink 差し替え攻撃を完全に排除する。
      （symlink の場合は open() 時点で OSError/errno.ELOOP を raise）
    - O_CLOEXEC で fd の子プロセスへの漏洩を防ぐ。
    - open() した fd を保持したまま os.fstat() でパーミッション再検査する（TOCTOU 排除）。

    O_NOFOLLOW の動作:
    - path が symlink の場合: open() が OSError (errno.ELOOP または EMLINK) を raise
    - macOS: ELOOP / Linux: ELOOP（POSIX 準拠）
    - Windows: O_NOFOLLOW 未サポート → 通常の open() にフォールバック（警告ログ）

    - # コメント行はスキップ
    - 空行はスキップ
    - 値の前後クォートを除去（'...' / "..."）
    """
    result: dict[str, str] = {}

    # H2 fix: path + 親ディレクトリが symlink でないことを lstat で事前確認
    _check_path_not_symlink(path)

    # HIGH-R4-1: O_NOFOLLOW + O_CLOEXEC で symlink 差し替え完全排除
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:
        log.warning(
            "[Vault] O_NOFOLLOW not available on this platform (%s). "
            "Symlink protection is weakened. Falling back to fstat-only TOCTOU protection.",
            os.name,
        )
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        raw_fd = os.open(str(path), flags)
    except OSError as e:
        import errno
        eloop = getattr(errno, "ELOOP", None)
        if eloop is not None and e.errno == eloop:
            raise VaultError(
                f"Symlink detected (O_NOFOLLOW): {path} is a symlink. "
                "Vault files must not be symlinks (TOCTOU attack prevention). "
                "Create a regular file with: chmod 0600 <actual_file>"
            ) from e
        raise VaultError(f"Failed to open {path} with O_NOFOLLOW: {e}") from e

    # HIGH-R4-1: fd から IO オブジェクトを生成して fstat で post-open 検査
    try:
        import io
        with io.open(raw_fd, "r", encoding="utf-8", closefd=True) as fh:
            # fd を使って open 後にパーミッション再検査（TOCTOU 排除）
            _check_fd_permissions(fh.fileno(), path)
            content = fh.read()
    except VaultError:
        raise
    except Exception as e:
        raise VaultError(f"Failed to read {path}: {e}") from e

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # クォート除去
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def load_from_env(env_path: Optional[Path] = None) -> PaperCredentials:
    """`.env.d/moomoo_paper.env` から PaperCredentials を読み込む。

    CRITICAL 4 修正:
    - ファイルが存在しない場合は VaultError を raise する（環境変数フォールバック削除）。
    - 環境変数フォールバックを使うと CI/CD 環境に設定された MOOMOO_TRD_ENV=REAL が
      Paper 用途に漏入する危険があるため、ファイル存在を必須とする。
    - trd_env が SIMULATE 以外なら PaperCredentials.__post_init__ で VaultError を raise する。

    Args:
        env_path: テスト用に .env ファイルパスを上書きできる。None なら _ENV_FILE を使用。

    Returns:
        PaperCredentials

    Raises:
        VaultError: ファイル不在・必須キー不足・trd_env が SIMULATE 以外
    """
    target = env_path or _ENV_FILE

    if not target.exists():
        raise VaultError(
            f"Env file not found: {target}. "
            "Create the file with MOOMOO_APP_ID, MOOMOO_APP_SECRET, MOOMOO_TRD_ENV=SIMULATE. "
            "Environment variable fallback is disabled to prevent REAL env leakage."
        )

    # パーミッション検査（CRITICAL 2 + RT-R2-006 symlink 対応）
    _check_file_permissions(target)

    env_vars = _parse_env_file(target)

    app_id = env_vars.get("MOOMOO_APP_ID", "").strip()
    app_secret = env_vars.get("MOOMOO_APP_SECRET", "").strip()
    host = env_vars.get("MOOMOO_HOST", "127.0.0.1").strip()
    port_str = env_vars.get("MOOMOO_PORT", "11111").strip()
    trd_env = env_vars.get("MOOMOO_TRD_ENV", "SIMULATE").strip()

    if not app_id:
        raise VaultError(
            f"MOOMOO_APP_ID not found in {target}. "
            "Add MOOMOO_APP_ID=<your_app_id> to the file."
        )
    if not app_secret:
        raise VaultError(
            f"MOOMOO_APP_SECRET not found in {target}. "
            "Add MOOMOO_APP_SECRET=<your_secret> to the file."
        )

    try:
        port = int(port_str)
    except ValueError:
        raise VaultError(f"MOOMOO_PORT must be integer, got {port_str!r}")

    # PaperCredentials.__post_init__ が trd_env != "SIMULATE" を VaultError で検査する
    return PaperCredentials(
        app_id=app_id,
        app_secret=app_secret,
        host=host,
        port=port,
        trd_env=trd_env,
    )


def _resolve_master_key(explicit_key: Optional[str] = None) -> str:
    """VAULT_MASTER_KEY を優先順位で解決する（H-1 keyring 対応）。

    優先順位:
    1. explicit_key（テスト注入・最優先）
    2. keyring ライブラリ（Mac Keychain / SecretService 等）— env 露出なし
    3. 環境変数 VAULT_MASTER_KEY（明示 opt-in: VAULT_ALLOW_ENV_FALLBACK=1 が必要）
       RT-R2-H1 修正: keyring 失敗を明示 log + VAULT_ALLOW_ENV_FALLBACK=1 未設定なら VaultError raise

    keyring サービス名: "atlas_v3_vault", ユーザー名: "VAULT_MASTER_KEY"

    Returns:
        master key 文字列（空でない保証）

    Raises:
        VaultError: いずれの経路でもキーを取得できない場合
                    または keyring 失敗 + VAULT_ALLOW_ENV_FALLBACK 未設定の場合
    """
    if explicit_key:
        return explicit_key

    # keyring 優先（H-1: env 露出防止）
    keyring_error: Optional[Exception] = None
    try:
        import keyring as _keyring
        kr_key = _keyring.get_password("atlas_v3_vault", "VAULT_MASTER_KEY")
        if kr_key:
            return kr_key
        # kr_key is None = keyring に未登録（エラーではなく未設定）
        log.debug("[Vault] keyring: VAULT_MASTER_KEY not found in keyring.")
    except Exception as e:
        keyring_error = e
        # RT-R2-H1: keyring 失敗を明示 log（silent 退行禁止）
        log.warning(
            "[Vault] keyring lookup failed: %s. "
            "Check that keyring is properly installed and accessible.",
            e,
        )

    # RT-R2-H1: 環境変数フォールバックは VAULT_ALLOW_ENV_FALLBACK=1 の明示 opt-in が必要
    # 本番環境では未設定 → VaultError で強制 secure
    allow_env_fallback = os.environ.get("VAULT_ALLOW_ENV_FALLBACK", "") == "1"

    if not allow_env_fallback:
        if keyring_error is not None:
            raise VaultError(
                f"keyring lookup failed: {keyring_error}. "
                "Environment variable fallback is DISABLED (VAULT_ALLOW_ENV_FALLBACK not set). "
                "To enable fallback for CI/dev: export VAULT_ALLOW_ENV_FALLBACK=1. "
                "For production: store key in keyring with: "
                "python3 -c \"import keyring; keyring.set_password('atlas_v3_vault', "
                "'VAULT_MASTER_KEY', '<key>')\""
            )
        raise VaultError(
            "VAULT_MASTER_KEY not found in keyring. "
            "Environment variable fallback is DISABLED (VAULT_ALLOW_ENV_FALLBACK not set). "
            "Store key in keyring: "
            "python3 -c \"import keyring; keyring.set_password('atlas_v3_vault', "
            "'VAULT_MASTER_KEY', '<key>')\". "
            "For CI/dev: export VAULT_ALLOW_ENV_FALLBACK=1 && export VAULT_MASTER_KEY=<key>"
        )

    # 明示 opt-in: 環境変数フォールバック（launchctl/ps で露出リスクあり）
    env_key = os.environ.get("VAULT_MASTER_KEY", "")
    if env_key:
        log.warning(
            "[Vault] Using VAULT_MASTER_KEY from environment variable. "
            "This is an explicit opt-in (VAULT_ALLOW_ENV_FALLBACK=1). "
            "Prefer keyring for production."
        )
        return env_key

    raise VaultError(
        "VAULT_MASTER_KEY not found in keyring or environment. "
        "Recommended: store in keyring with: "
        "python3 -c \"import keyring; keyring.set_password('atlas_v3_vault', 'VAULT_MASTER_KEY', '<key>')\". "
        "Fallback (CI/dev only): export VAULT_ALLOW_ENV_FALLBACK=1 && export VAULT_MASTER_KEY=<key>. "
        "Generate key with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )


def encrypt_to_disk(
    creds: PaperCredentials,
    vault_path: Optional[Path] = None,
    master_key: Optional[str] = None,
) -> Path:
    """PaperCredentials を Fernet 暗号化して vault_paper.enc に保存する。

    Args:
        creds:       保存する credentials
        vault_path:  テスト用にパスを上書き。None なら _VAULT_FILE を使用。
        master_key:  テスト用にキーを上書き。None なら keyring/VAULT_MASTER_KEY を使用。

    Returns:
        書き込んだファイルのパス

    Raises:
        VaultError: cryptography 未インストール / VAULT_MASTER_KEY 未設定
    """
    try:
        from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
    except ImportError:
        raise VaultError(
            "cryptography package not installed. "
            "Run: pip install cryptography"
        )

    import json
    from cryptography.fernet import Fernet

    # H-1: keyring 優先で master key を解決
    key_str = _resolve_master_key(master_key)

    try:
        fernet = Fernet(key_str.encode())
    except Exception as e:
        raise VaultError(f"Invalid VAULT_MASTER_KEY: {e}")

    payload = json.dumps({
        "app_id": creds.app_id,
        "app_secret": creds.app_secret,
        "host": creds.host,
        "port": creds.port,
        "trd_env": creds.trd_env,
    }, ensure_ascii=False)

    encrypted = fernet.encrypt(payload.encode("utf-8"))

    target = vault_path or _VAULT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encrypted)
    # 所有者のみ読み書き可能
    target.chmod(0o600)
    return target


def decrypt_from_disk(
    vault_path: Optional[Path] = None,
    master_key: Optional[str] = None,
) -> PaperCredentials:
    """vault_paper.enc を Fernet 復号して PaperCredentials を返す。

    Args:
        vault_path:  テスト用にパスを上書き。None なら _VAULT_FILE を使用。
        master_key:  テスト用にキーを上書き。None なら keyring/VAULT_MASTER_KEY を使用。

    Returns:
        PaperCredentials

    Raises:
        VaultError: ファイル不在 / 復号失敗 / スキーマ不正
    """
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        raise VaultError(
            "cryptography package not installed. "
            "Run: pip install cryptography"
        )

    import json
    from cryptography.fernet import Fernet, InvalidToken

    # H-1: keyring 優先で master key を解決
    key_str = _resolve_master_key(master_key)

    target = vault_path or _VAULT_FILE
    if not target.exists():
        raise VaultError(f"vault file not found: {target}")

    try:
        fernet = Fernet(key_str.encode())
    except Exception as e:
        raise VaultError(f"Invalid VAULT_MASTER_KEY: {e}")

    try:
        decrypted = fernet.decrypt(target.read_bytes())
    except InvalidToken:
        raise VaultError(
            "Decryption failed: invalid key or corrupted vault file."
        )

    try:
        data = json.loads(decrypted.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise VaultError(f"Vault payload JSON parse error: {e}")

    required = {"app_id", "app_secret"}
    missing = required - data.keys()
    if missing:
        raise VaultError(f"Vault payload missing required fields: {missing}")

    return PaperCredentials(
        app_id=data["app_id"],
        app_secret=data["app_secret"],
        host=data.get("host", "127.0.0.1"),
        port=int(data.get("port", 11111)),
        trd_env=data.get("trd_env", "SIMULATE"),
    )
