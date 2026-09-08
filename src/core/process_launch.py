"""Frozen-aware subprocess launch + PID/lock identity helpers.

两类关切：
1. **子进程启动命令**：源码环境用 ``python -m src.xxx``；PyInstaller 打包环境
   ``sys.executable`` 是 lamix 二进制，``-m`` 不再可用，改走 CLI 内部子命令
   ``lamix gateway daemon-run`` / ``lamix gateway watchdog-run``。
2. **PID/锁身份校验**：仅靠 PID 存活会被 PID 复用或 lock 里存的是 cli PID
   误判为 daemon 已在运行。所有 pid/lock 文件带 role 字段（daemon/watchdog/cli），
   校验时对 ``ps`` 命令行做特征匹配。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROLE_DAEMON = "daemon"
ROLE_WATCHDOG = "watchdog"
ROLE_CLI = "cli"

# 用于识别进程角色的命令行子串（针对源码 / frozen 两种启动方式都能命中）
_DAEMON_MARKERS = ("src.daemon", "daemon-run")
_WATCHDOG_MARKERS = ("src.watchdog", "watchdog-run")


def is_frozen() -> bool:
    """当前进程是否运行在 PyInstaller 打包环境。"""
    return bool(getattr(sys, "frozen", False))


def daemon_launch_cmd() -> list[str]:
    """返回用于以子进程方式启动 daemon 的 argv。"""
    if is_frozen():
        return [sys.executable, "gateway", "daemon-run"]
    return [sys.executable, "-m", "src.daemon"]


def watchdog_launch_cmd() -> list[str]:
    """返回用于以子进程方式启动 watchdog 的 argv。"""
    if is_frozen():
        return [sys.executable, "gateway", "watchdog-run"]
    return [sys.executable, "-m", "src.watchdog"]


def safe_mode_launch_cmd(safe_mode_script: str | None = None) -> list[str]:
    """返回用于以子进程方式启动 safe_mode 的 argv。

    frozen: ``[lamix, gateway, safe-mode-run]``（走 CLI 内部子命令，
    不能把 src/safe_mode.py 路径当参数传给 lamix 二进制）。
    源码: ``[python, src/safe_mode.py]``（脚本路径由调用方传入）。
    """
    if is_frozen():
        return [sys.executable, "gateway", "safe-mode-run"]
    if safe_mode_script is None:
        # 源码模式下 safe_mode.py 就在 src/ 目录
        from pathlib import Path as _P
        safe_mode_script = str(_P(__file__).resolve().parent.parent / "safe_mode.py")
    return [sys.executable, safe_mode_script]


# ── PID/锁文件的身份校验 ─────────────────────────────────────────────


def _ps_command(pid: int) -> str:
    """返回 ``ps -p PID -o command=`` 的输出（进程不存在返回空串）。"""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "wmic", "process", "where", f"ProcessId={pid}",
                    "get", "CommandLine", "/format:list",
                ],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if line.startswith("CommandLine="):
                    return line[len("CommandLine="):].strip()
        except Exception:
            pass
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def pid_role(pid: int) -> str | None:
    """通过 ps 命令行推断 PID 的角色。

    返回 "daemon" / "watchdog" / "cli" / None（未知或已死）。
    先匹配 daemon/watchdog 的强特征子串，再兜底判断是否是 ``lamix cli`` 形态。
    """
    cmd = _ps_command(pid)
    if not cmd:
        return None
    if any(m in cmd for m in _DAEMON_MARKERS):
        return ROLE_DAEMON
    if any(m in cmd for m in _WATCHDOG_MARKERS):
        return ROLE_WATCHDOG
    # cli：命令行中出现独立的 "cli" 参数（如 "lamix cli"），
    # 但要排除 -m src.cli / src.cli 这类。
    parts = cmd.split()
    for i, part in enumerate(parts):
        if part == "cli":
            prev = parts[i - 1] if i > 0 else ""
            if prev in ("-m",) or prev.endswith("src.cli"):
                continue
            return ROLE_CLI
    return None


def process_exists(pid: int) -> bool:
    """跨平台 PID 存活检测。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            line = result.stdout.strip()
            return bool(line) and str(pid) in line
        except (subprocess.SubprocessError, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def write_pid_record(path: Path, pid: int, role: str) -> None:
    """把 PID + role 以 JSON 写入 pid/lock 文件（原子写入）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": int(pid), "role": role})
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_pid_record(path: Path) -> tuple[int | None, str | None]:
    """读取 pid/lock 文件，返回 (pid, role)。

    兼容旧格式：文件里只有一个整数时，返回 (pid, None)。
    """
    if not path.exists():
        return None, None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        try:
            pid = int(data.get("pid", 0))
        except (TypeError, ValueError):
            return None, None
        role = data.get("role") if isinstance(data.get("role"), str) else None
        return (pid if pid > 0 else None), role
    # 兼容旧的纯 PID 格式
    try:
        return int(raw), None
    except ValueError:
        return None, None


def is_running_as(pid: int | None, expected_role: str) -> bool:
    """PID 存在且身份确实是 expected_role（防 PID 复用与身份误判）。"""
    if not pid or pid <= 0:
        return False
    if not process_exists(pid):
        return False
    actual = pid_role(pid)
    if actual is None:
        # 无法确认身份（例如 ps 权限受限）：保守地按存活处理，
        # 交由上层根据具体场景决定是否放行。
        return False
    return actual == expected_role
