"""tests/test_atlas_v3_vault_20260425.py — atlas_v3/ops/vault.py coverage tests

対象: atlas_v3/ops/vault.py (217 stmts)
happy path: 8 件 / error path: 6 件
推定 coverage: ~70%
"""
from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path

import pytest

from atlas_v3.ops.vault import (
    PaperCredentials,
    VaultError,
    decrypt_from_disk,
    encrypt_to_disk,
    load_from_env,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_VALID_ENV_CONTENT = textwrap.dedent("""\
    MOOMOO_APP_ID=test_app_id
    MOOMOO_APP_SECRET=test_secret_value
    MOOMOO_HOST=127.0.0.1
    MOOMOO_PORT=11111
    MOOMOO_TRD_ENV=SIMULATE
""")


def _write_secure(path: Path, content: str) -> None:
    """0600 で書く（vault のパーミッション要件を満たす）。"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    # parent も 0700 以下にする（グループ・other の read/exec を落とす）
    try:
        path.parent.chmod(0o700)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PaperCredentials — happy path
# ---------------------------------------------------------------------------

class TestPaperCredentials:
    def test_happy_defaults(self):
        """最小フィールドで生成できる。"""
        c = PaperCredentials(app_id="aid", app_secret="sec")
        assert c.app_id == "aid"
        assert c.trd_env == "SIMULATE"
        assert c.port == 11111

    def test_happy_custom_host_port(self):
        """host / port を指定できる。"""
        c = PaperCredentials(app_id="a", app_secret="s", host="192.168.1.1", port=22222)
        assert c.host == "192.168.1.1"
        assert c.port == 22222

    def test_repr_masks_secret(self):
        """__repr__ は app_secret を **** でマスクする。"""
        c = PaperCredentials(app_id="AID", app_secret="SecretLongValue")
        r = repr(c)
        assert "SecretLongValue" not in r
        assert "****" in r
        assert "AID" in r

    def test_repr_short_secret_masked(self):
        """短い app_secret（4 文字以下）も **** にマスクされる。"""
        c = PaperCredentials(app_id="a", app_secret="abc")
        assert "abc" not in repr(c)
        assert "****" in repr(c)

    # --- error path ---

    def test_empty_app_id_raises(self):
        with pytest.raises(VaultError, match="app_id must not be empty"):
            PaperCredentials(app_id="   ", app_secret="sec")

    def test_empty_app_secret_raises(self):
        with pytest.raises(VaultError, match="app_secret must not be empty"):
            PaperCredentials(app_id="aid", app_secret="  ")

    def test_invalid_port_raises(self):
        with pytest.raises(VaultError, match="port must be 1-65535"):
            PaperCredentials(app_id="a", app_secret="s", port=0)

    def test_trd_env_real_raises(self):
        with pytest.raises(VaultError, match="trd_env must be 'SIMULATE'"):
            PaperCredentials(app_id="a", app_secret="s", trd_env="REAL")


# ---------------------------------------------------------------------------
# load_from_env — happy path
# ---------------------------------------------------------------------------

class TestLoadFromEnv:
    def test_happy_full_fields(self, tmp_path):
        """全フィールド指定で PaperCredentials を返す。"""
        env_file = tmp_path / "moomoo_paper.env"
        _write_secure(env_file, _VALID_ENV_CONTENT)
        creds = load_from_env(env_path=env_file)
        assert creds.app_id == "test_app_id"
        assert creds.app_secret == "test_secret_value"
        assert creds.host == "127.0.0.1"
        assert creds.port == 11111
        assert creds.trd_env == "SIMULATE"

    def test_happy_quoted_values(self, tmp_path):
        """クォートで囲まれた値も正常にパースされる。"""
        content = textwrap.dedent("""\
            MOOMOO_APP_ID="quoted_app_id"
            MOOMOO_APP_SECRET='single_secret'
            MOOMOO_TRD_ENV=SIMULATE
        """)
        env_file = tmp_path / "moomoo_paper.env"
        _write_secure(env_file, content)
        creds = load_from_env(env_path=env_file)
        assert creds.app_id == "quoted_app_id"
        assert creds.app_secret == "single_secret"

    def test_happy_comment_and_blank_lines_skipped(self, tmp_path):
        """コメント行・空行はスキップされる。"""
        content = textwrap.dedent("""\
            # This is a comment
            MOOMOO_APP_ID=cid

            MOOMOO_APP_SECRET=csecret
            MOOMOO_TRD_ENV=SIMULATE
        """)
        env_file = tmp_path / "moomoo_paper.env"
        _write_secure(env_file, content)
        creds = load_from_env(env_path=env_file)
        assert creds.app_id == "cid"

    def test_happy_default_host_port_when_omitted(self, tmp_path):
        """HOST / PORT 省略時はデフォルト値が使われる。"""
        content = textwrap.dedent("""\
            MOOMOO_APP_ID=aid
            MOOMOO_APP_SECRET=asecret
            MOOMOO_TRD_ENV=SIMULATE
        """)
        env_file = tmp_path / "moomoo_paper.env"
        _write_secure(env_file, content)
        creds = load_from_env(env_path=env_file)
        assert creds.host == "127.0.0.1"
        assert creds.port == 11111

    # --- error path ---

    def test_file_not_found_raises(self, tmp_path):
        """env ファイルが存在しない場合 VaultError を raise する。"""
        missing = tmp_path / "no_such.env"
        with pytest.raises(VaultError, match="Env file not found"):
            load_from_env(env_path=missing)

    def test_missing_app_id_raises(self, tmp_path):
        """MOOMOO_APP_ID が欠如していたら VaultError。"""
        content = "MOOMOO_APP_SECRET=sec\nMOOMOO_TRD_ENV=SIMULATE\n"
        env_file = tmp_path / "bad.env"
        _write_secure(env_file, content)
        with pytest.raises(VaultError, match="MOOMOO_APP_ID not found"):
            load_from_env(env_path=env_file)

    def test_missing_app_secret_raises(self, tmp_path):
        """MOOMOO_APP_SECRET が欠如していたら VaultError。"""
        content = "MOOMOO_APP_ID=aid\nMOOMOO_TRD_ENV=SIMULATE\n"
        env_file = tmp_path / "bad.env"
        _write_secure(env_file, content)
        with pytest.raises(VaultError, match="MOOMOO_APP_SECRET not found"):
            load_from_env(env_path=env_file)

    def test_invalid_port_string_raises(self, tmp_path):
        """MOOMOO_PORT が数値でない場合 VaultError。"""
        content = textwrap.dedent("""\
            MOOMOO_APP_ID=aid
            MOOMOO_APP_SECRET=sec
            MOOMOO_PORT=not_a_number
            MOOMOO_TRD_ENV=SIMULATE
        """)
        env_file = tmp_path / "bad.env"
        _write_secure(env_file, content)
        with pytest.raises(VaultError, match="MOOMOO_PORT must be integer"):
            load_from_env(env_path=env_file)

    def test_trd_env_real_raises(self, tmp_path):
        """MOOMOO_TRD_ENV=REAL の場合 VaultError（PaperCredentials の guard）。"""
        content = textwrap.dedent("""\
            MOOMOO_APP_ID=aid
            MOOMOO_APP_SECRET=sec
            MOOMOO_TRD_ENV=REAL
        """)
        env_file = tmp_path / "bad.env"
        _write_secure(env_file, content)
        with pytest.raises(VaultError):
            load_from_env(env_path=env_file)


# ---------------------------------------------------------------------------
# encrypt_to_disk / decrypt_from_disk — happy path + error path
# ---------------------------------------------------------------------------

class TestEncryptDecrypt:
    """Fernet round-trip テスト。VAULT_ALLOW_ENV_FALLBACK=1 + master_key 直渡しで
    keyring / 環境変数への依存を排除する。"""

    _TEST_KEY = "rOhG9GYe7FVkYNTAwsQYYobvhS6nyRQocU4Ml6QOPY8="

    def _creds(self) -> PaperCredentials:
        return PaperCredentials(app_id="enc_id", app_secret="enc_secret")

    def test_happy_encrypt_then_decrypt(self, tmp_path):
        """暗号化 → 復号でオリジナルと一致する。"""
        vault_path = tmp_path / "vault.enc"
        creds = self._creds()
        written = encrypt_to_disk(creds, vault_path=vault_path, master_key=self._TEST_KEY)
        assert written == vault_path
        assert vault_path.exists()
        restored = decrypt_from_disk(vault_path=vault_path, master_key=self._TEST_KEY)
        assert restored.app_id == creds.app_id
        assert restored.app_secret == creds.app_secret
        assert restored.trd_env == "SIMULATE"

    def test_happy_encrypted_file_is_0600(self, tmp_path):
        """encrypt_to_disk は保存後に 0600 を設定する。"""
        vault_path = tmp_path / "vault.enc"
        encrypt_to_disk(self._creds(), vault_path=vault_path, master_key=self._TEST_KEY)
        mode = stat.S_IMODE(vault_path.stat().st_mode)
        assert mode == 0o600

    def test_happy_parent_created_if_missing(self, tmp_path):
        """出力先ディレクトリが存在しなくても自動作成される。"""
        vault_path = tmp_path / "deep" / "nested" / "vault.enc"
        encrypt_to_disk(self._creds(), vault_path=vault_path, master_key=self._TEST_KEY)
        assert vault_path.exists()

    def test_happy_decrypt_custom_host_port(self, tmp_path):
        """カスタム host / port も round-trip で復元される。"""
        vault_path = tmp_path / "vault.enc"
        creds = PaperCredentials(
            app_id="hid", app_secret="hsec", host="10.0.0.1", port=9999
        )
        encrypt_to_disk(creds, vault_path=vault_path, master_key=self._TEST_KEY)
        restored = decrypt_from_disk(vault_path=vault_path, master_key=self._TEST_KEY)
        assert restored.host == "10.0.0.1"
        assert restored.port == 9999

    # --- error path ---

    def test_decrypt_file_not_found_raises(self, tmp_path):
        """vault ファイルが存在しない場合 VaultError。"""
        missing = tmp_path / "no_vault.enc"
        with pytest.raises(VaultError, match="vault file not found"):
            decrypt_from_disk(vault_path=missing, master_key=self._TEST_KEY)

    def test_decrypt_wrong_key_raises(self, tmp_path):
        """異なるキーで復号しようとすると VaultError。"""
        vault_path = tmp_path / "vault.enc"
        encrypt_to_disk(self._creds(), vault_path=vault_path, master_key=self._TEST_KEY)
        # 別の有効な Fernet キーを生成
        from cryptography.fernet import Fernet
        wrong_key = Fernet.generate_key().decode()
        with pytest.raises(VaultError, match="Decryption failed"):
            decrypt_from_disk(vault_path=vault_path, master_key=wrong_key)

    def test_decrypt_corrupted_file_raises(self, tmp_path):
        """vault ファイルが壊れている場合 VaultError。"""
        vault_path = tmp_path / "vault.enc"
        vault_path.write_bytes(b"not_valid_fernet_data")
        with pytest.raises(VaultError):
            decrypt_from_disk(vault_path=vault_path, master_key=self._TEST_KEY)

    def test_encrypt_invalid_key_raises(self, tmp_path):
        """不正な master_key で encrypt しようとすると VaultError。"""
        vault_path = tmp_path / "vault.enc"
        with pytest.raises(VaultError, match="Invalid VAULT_MASTER_KEY"):
            encrypt_to_disk(self._creds(), vault_path=vault_path, master_key="not_valid_key==")


# ---------------------------------------------------------------------------
# _resolve_master_key — env fallback path (monkeypatched)
# ---------------------------------------------------------------------------

class TestResolveMasterKey:
    """VAULT_ALLOW_ENV_FALLBACK=1 環境での環境変数フォールバック確認。"""

    def test_env_fallback_with_opt_in(self, monkeypatch):
        """VAULT_ALLOW_ENV_FALLBACK=1 + VAULT_MASTER_KEY 設定でキーが返る。"""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("VAULT_ALLOW_ENV_FALLBACK", "1")
        monkeypatch.setenv("VAULT_MASTER_KEY", key)
        # keyring を ImportError にして env フォールバックを強制させる
        import unittest.mock as mock
        with mock.patch.dict("sys.modules", {"keyring": None}):
            from atlas_v3.ops.vault import _resolve_master_key
            result = _resolve_master_key()
        assert result == key

    def test_no_opt_in_no_keyring_raises(self, monkeypatch):
        """VAULT_ALLOW_ENV_FALLBACK 未設定 + keyring なしなら VaultError。"""
        monkeypatch.delenv("VAULT_ALLOW_ENV_FALLBACK", raising=False)
        import unittest.mock as mock
        with mock.patch.dict("sys.modules", {"keyring": None}):
            from atlas_v3.ops.vault import _resolve_master_key
            with pytest.raises(VaultError):
                _resolve_master_key()
