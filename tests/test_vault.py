"""vault 模块测试：加密存储、键名校验、run 注入、非 TTY 拒绝输出。"""

import subprocess
import sys
from pathlib import Path

import pytest

from src import vault
from src.vault import VaultError


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """把 vault 三文件指向 tmp_path。"""
    monkeypatch.setattr(vault, "VAULT_FILE", tmp_path / "vault.enc")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / ".vault_key")
    monkeypatch.setattr(vault, "LOCK_FILE", tmp_path / ".vault.lock")
    return tmp_path


class TestStore:
    def test_set_get_roundtrip(self, tmp_vault):
        vault.set_secret("jump-sudo", "La@123456")
        assert vault.get_secret("jump-sudo") == "La@123456"

    def test_encrypted_on_disk(self, tmp_vault):
        """密文落盘：vault.enc 原始字节里搜不到明文。"""
        secret = "SuperSecretPwd99"
        vault.set_secret("k1", secret)
        blob = (tmp_vault / "vault.enc").read_bytes()
        assert secret.encode() not in blob

    def test_key_file_and_vault_permissions(self, tmp_vault):
        vault.set_secret("k1", "v")
        if sys.platform != "win32":
            assert (tmp_vault / ".vault_key").stat().st_mode & 0o777 == 0o600
            assert (tmp_vault / "vault.enc").stat().st_mode & 0o777 == 0o600

    def test_list_keys(self, tmp_vault):
        vault.set_secret("b-key", "1")
        vault.set_secret("a-key", "2")
        assert vault.list_keys() == ["a-key", "b-key"]

    def test_get_missing_raises(self, tmp_vault):
        with pytest.raises(VaultError, match="键不存在"):
            vault.get_secret("nope")

    def test_delete(self, tmp_vault):
        vault.set_secret("k", "v")
        assert vault.delete_secret("k") is True
        assert vault.delete_secret("k") is False
        assert vault.list_keys() == []

    def test_set_empty_deletes(self, tmp_vault):
        vault.set_secret("k", "v")
        vault.set_secret("k", "")
        assert vault.list_keys() == []

    def test_invalid_key_name(self, tmp_vault):
        for bad in ("", "1 digit-start ok?", "-dash", "a" * 65, "has space",
                    "key/with/slash"):
            with pytest.raises(VaultError):
                vault.set_secret(bad, "v")

    def test_key_persistence_across_instances(self, tmp_vault):
        """两次独立操作共享同一密钥文件（加密可解）。"""
        vault.set_secret("k1", "first")
        vault.set_secret("k2", "second")
        assert vault.get_secret("k1") == "first"
        assert vault.get_secret("k2") == "second"


class TestRunInjection:
    def test_env_var_injected(self, tmp_vault):
        vault.set_secret("run-key", "InjectedValue123")
        code = vault.run_with_secret(
            "run-key",
            [sys.executable, "-c",
             "import os; assert os.environ['LAMIX_SECRET'] == 'InjectedValue123'"])
        assert code == 0

    def test_run_missing_key_raises(self, tmp_vault):
        with pytest.raises(VaultError):
            vault.run_with_secret("nope", ["echo", "hi"])

    def test_run_empty_cmd_raises(self, tmp_vault):
        vault.set_secret("k", "v")
        with pytest.raises(VaultError, match="需要命令"):
            vault.run_with_secret("k", [])

    def test_run_returns_exit_code(self, tmp_vault):
        vault.set_secret("k", "v")
        assert vault.run_with_secret("k", [sys.executable, "-c", "raise SystemExit(3)"]) == 3


class TestInteractiveGuard:
    def test_non_tty_get_refused(self, tmp_vault, capsys):
        """非 TTY 下 get 拒绝输出明文（agent context 防线）。"""
        vault.set_secret("guard", "NeverLeakThis")
        rc = vault.get_secret_interactive("guard")
        out = capsys.readouterr()
        assert rc == 1
        assert "NeverLeakThis" not in out.out
        assert "NeverLeakThis" not in out.err
        assert "非 TTY" in out.err


class TestCli:
    """CLI 冒烟：set --stdin / list / run。"""

    def _run_cli(self, *argv, stdin_text=""):
        from src.cli import _build_parser, run_vault
        parser = _build_parser()
        args = parser.parse_args(("vault",) + argv)
        import io
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            run_vault(args)
        finally:
            sys.stdin = old_stdin

    def test_cli_set_list_run(self, tmp_vault, capsys):
        self._run_cli("set", "cli-key", "--stdin", stdin_text="CliSecret42\n")
        assert vault.get_secret("cli-key") == "CliSecret42"
        self._run_cli("list")
        assert "cli-key" in capsys.readouterr().out
        code = vault.run_with_secret(
            "cli-key",
            [sys.executable, "-c",
             "import os; print('got', os.environ['LAMIX_SECRET'])"])
        assert code == 0

    def test_cli_get_non_tty_exit_code(self, tmp_vault):
        """CLI get 在非 TTY（测试环境）退出码非 0 且不输出明文。"""
        vault.set_secret("cli-g", "Hidden9")
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            self._run_cli("get", "cli-g")
        assert exc_info.value.code == 1
