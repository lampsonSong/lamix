"""lamix 本地密码 vault：加密存储 + run 注入执行。

威胁模型：明文密码不得进入 LLM context（防上传 LLM server）。

- 密码以 Fernet 加密存储于 ``~/.lamix/vault.enc``（0600）
- 密钥为 ``~/.lamix/.vault_key``（0600，32 字节 urlsafe base64）
- agent 取用唯一姿势：``lamix vault run <key> -- <cmd...>``——密码注入
  子进程环境变量 ``LAMIX_SECRET``，不进命令行参数（ps 不可见）、不回显
  stdout（LLM 看不到值），只存在于目标命令的进程环境内
- ``lamix vault get <key>`` 仅在 TTY 交互下输出明文（agent 的 shell 工具
  无 TTY，拿不到明文；人登录终端可直接取用）

模块级 VAULT_FILE / KEY_FILE 可被测试替换（同 INFO_DIR 模式）。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

VAULT_FILE = Path.home() / ".lamix" / "vault.enc"
KEY_FILE = Path.home() / ".lamix" / ".vault_key"
LOCK_FILE = Path.home() / ".lamix" / ".vault.lock"
ENV_VAR = "LAMIX_SECRET"
KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class VaultError(Exception):
    """vault 操作错误（键不存在/格式非法/解密失败等）。"""


def validate_key_name(key: str) -> None:
    """校验键名：字母数字开头，允许 . _ -，长度 ≤64。"""
    if not KEY_NAME_RE.match(key):
        raise VaultError(f"非法键名 {key!r}：字母数字开头，仅 [A-Za-z0-9._-]，≤64 字符")


def _ensure_fernet() -> Fernet:
    """读取或首次生成密钥，返回 Fernet 实例。"""
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        token = KEY_FILE.read_text(encoding="utf-8").strip()
        if not token:
            raise VaultError(f"密钥文件为空：{KEY_FILE}")
        return Fernet(token.encode())
    token = Fernet.generate_key()
    KEY_FILE.write_text(token.decode(), encoding="utf-8")
    os.chmod(KEY_FILE, 0o600)
    return Fernet(token)


def load_secrets() -> dict[str, str]:
    """读取并解密整个 vault；无文件时返回空 dict。"""
    if not VAULT_FILE.exists():
        return {}
    blob = VAULT_FILE.read_bytes()
    if not blob:
        return {}
    try:
        plain = _ensure_fernet().decrypt(blob)
        data = json.loads(plain)
    except (InvalidToken, json.JSONDecodeError) as exc:
        raise VaultError(f"vault 解密失败（密钥不匹配或文件损坏）：{exc}") from exc
    if not isinstance(data, dict):
        raise VaultError("vault 内容格式异常（非 JSON 对象）")
    return {str(k): str(v) for k, v in data.items()}


def save_secrets(data: dict[str, str]) -> None:
    """加密并原子写回整个 vault（tmp + rename，带文件锁）。"""
    VAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        blob = _ensure_fernet().encrypt(
            json.dumps(data, ensure_ascii=False).encode("utf-8"))
        tmp = VAULT_FILE.with_suffix(".enc.tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(VAULT_FILE)


def set_secret(key: str, value: str) -> None:
    """写入/更新一个键。空值视为删除。"""
    validate_key_name(key)
    data = load_secrets()
    if value == "":
        data.pop(key, None)
    else:
        data[key] = value
    save_secrets(data)


def get_secret(key: str) -> str:
    """取一个键的明文值；不存在时抛 VaultError。"""
    data = load_secrets()
    if key not in data:
        raise VaultError(f"键不存在：{key}（vault 共 {len(data)} 个键）")
    return data[key]


def list_keys() -> list[str]:
    """列出全部键名（仅键名，永不输出值）。"""
    return sorted(load_secrets().keys())


def delete_secret(key: str) -> bool:
    """删除一个键，返回是否存在过。"""
    data = load_secrets()
    if key not in data:
        return False
    del data[key]
    save_secrets(data)
    return True


def run_with_secret(key: str, cmd: list[str]) -> int:
    """注入 LAMIX_SECRET 环境变量执行命令，返回退出码。

    密码只进入子进程环境变量：命令行参数不含密码（ps 不可见），
    stdout/stderr 原样透传（若命令自身回显密码，责任在命令）。
    """
    if not cmd:
        raise VaultError("run 需要命令（-- 之后）")
    value = get_secret(key)
    env = dict(os.environ)
    env[ENV_VAR] = value
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


def get_secret_interactive(key: str, stream=None) -> int:
    """TTY 限制版取值：仅当 stdout 是终端时打印明文，否则拒绝。

    agent 的 shell 工具无 TTY → 无法经此路径把明文带回 LLM context；
    人在登录终端执行则正常输出。返回进程退出码。
    """
    stream = stream or sys.stdout
    if not stream.isatty():
        print(f"[vault] 拒绝输出：stdout 非 TTY（防明文进入 agent context）。"
              f"请用 `lamix vault run {key} -- <cmd>` 注入使用。",
              file=sys.stderr)
        return 1
    print(get_secret(key))
    return 0
