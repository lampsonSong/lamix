"""守卫测试追加：明文密码写入拦截（check_plaintext_secret_write）。"""

import pytest

from src.tools import memory_guard as mg


class TestPlaintextSecretGuard:
    """明文密码写入守卫：拦高置信密码行，放 vault 键引用。"""

    _BLOCK_CASES = [
        '"password": "sk-xxxxxxx"',
        '"passwd":"P@ssw0rd2024"',
        'password = "Ab12@xyz"',
        "- 密码: La@0355061",
        "password: SuperSecret99",
        "PWD：abc123456",
    ]
    _PASS_CASES = [
        "- 密码: vault 键 jump-server-sudo",
        "密码存 vault：jump-server-sudo",
        "password: xxx（示例）",
        "密码: <hidden>",
        "密码: TODO",
        "讨论 password field 的设计",
        "密码：已废弃不再用",
    ]

    def _check(self, line: str) -> bool:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mg, "_memory_dir_of", lambda p: p.parent)
            return mg.check_plaintext_secret_write(
                type("P", (), {"parent": None, "name": "x.md"})() if False
                else _FakePath(), line + "\n") is not None

    def test_block_plaintext(self):
        for case in self._BLOCK_CASES:
            assert self._check(case), f"应拦截: {case}"

    def test_pass_vault_reference(self):
        for case in self._PASS_CASES:
            assert not self._check(case), f"应放行: {case}"


class _FakePath:
    """骗过类型检查的假路径（_memory_dir_of 已被 monkeypatch）。"""

    @property
    def parent(self):
        return self

    @property
    def name(self):
        return "x.md"
