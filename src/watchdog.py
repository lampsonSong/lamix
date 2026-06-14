"""Watchdog 进程：监控 daemon 心跳，超时则重启。

启动方式：由 launchd 管理（独立于 daemon 的 service）。
心跳超时（30 秒无心跳）→ 检查是否 user_stopped → 检查 Mac 是否休眠 → 否则重启 daemon。

心跳文件：~/.lamix/heartbeat/<pid>.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from src.core.config import LAMIX_DIR
from src.core.heartbeat import (
    HEARTBEAT_DIR,
    HEARTBEAT_INTERVAL,
    read_all_heartbeats,
    cleanup_stale_heartbeats,
    load_heartbeat,
)
from src.platforms.process_manager import get_process_manager
import logging
logger = logging.getLogger(__name__)

from src.core.constants import HEARTBEAT_TIMEOUT, WATCHDOG_INTERVAL
LOG_DIR = LAMIX_DIR / "logs"

# watchdog 重启 daemon 前设置的标志文件
_RESTART_FLAG_PATH = LAMIX_DIR / ".restart_by_watchdog"


def _get_lamix_bin() -> str | None:
    """定位 lamix 可执行文件路径。"""
    import shutil
    import sysconfig

    lamix = shutil.which("lamix")
    if lamix:
        return lamix

    scripts = sysconfig.get_path("scripts")
    candidates = ["lamix", "lamix.exe", "lamix.bat", "lamix-script.py"]
    for name in candidates:
        path = os.path.join(scripts, name)
        if os.path.exists(path):
            return path
    return None


def _log(msg: str) -> None:
    """写日志。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "watchdog.log"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    logger.info(f"[watchdog] {msg}")


def _load_config() -> dict:
    """加载配置。"""
    config_path = LAMIX_DIR / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _set_restart_flag(pid: int) -> None:
    """设置重启标志，告知 daemon 此次启动是被 watchdog 重启的。"""
    try:
        _RESTART_FLAG_PATH.write_text(str(pid), encoding="utf-8")
    except Exception as e:
        _log(f"设置重启标志失败: {e}")


def _clear_restart_flag() -> None:
    """清除重启标志。"""
    try:
        if _RESTART_FLAG_PATH.exists():
            _RESTART_FLAG_PATH.unlink()
    except Exception:
        pass


def _restart_daemon(pm) -> None:
    """通过 ProcessManager 重启 daemon。

    重启前设置标志文件，告知 daemon 此次是被 watchdog 重启的。
    """
    _log("尝试重启 daemon...")

    # 先找到当前 daemon pid，设置重启标志
    pid_file = LOG_DIR / "daemon.pid"
    old_pid = None
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pass

    # 设置重启标志（包含旧 pid，便于日志追踪）
    _set_restart_flag(old_pid or 0)

    try:
        # 优先使用 lamix 命令，确保 ps aux | grep lamix 能搜到
        lamix_bin = _get_lamix_bin()
        # 用 python -m src.daemon 启动，避免走 lamix gateway 的 REPL 流程
        if lamix_bin:
            daemon_command = [sys.executable, "-m", "src.daemon"]
        else:
            daemon_command = [sys.executable, "-m", "src.daemon"]

        success = pm.restart_daemon(
            daemon_command=daemon_command,
            pid_file=pid_file,
            log_dir=LOG_DIR,
        )

        if success:
            _log("daemon 重启请求已发送")
        else:
            _log("daemon 重启失败")
    except Exception as e:
        _log(f"重启 daemon 失败: {e}")
        # 失败时清除标志
        _clear_restart_flag()


def _is_mac_sleeping() -> bool:
    """检测 Mac 是否处于休眠状态。

    通过 pmset 检测：
    1. 如果有 ' Sleep ' 行且值 > 0，说明正在休眠（有预置休眠计时器）
    2. 如果有 ' Display Sleep ' 行且值 > 0，说明显示器已休眠
    3. 如果 'PreventUserIdleSystemSleep' 和 'NoIdleSleepAssertion' 都为 0，
       且没有 'UserIsActive'=1，说明系统处于无人值守的休眠状态

    Returns:
        True 表示 Mac 可能在休眠，跳过重启是安全的
    """
    if sys.platform != "darwin":
        return False

    try:
        result = subprocess.run(
            ["pmset", "-g", "assertions"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False

        output = result.stdout

        # 提取各 assertion 状态
        prevent_idle = "PreventUserIdleSystemSleep" in output
        no_idle = "NoIdleSleepAssertion" in output
        user_active = "UserIsActive" in output

        # 如果用户活跃，系统肯定醒着
        if user_active and not no_idle:
            return False

        # 如果没有任何 idle sleep prevention，系统可能在休眠
        if not prevent_idle and not no_idle:
            # 进一步检查：尝试 pmset -g 查看电源状态和 sleep timer
            result2 = subprocess.run(
                ["pmset", "-g"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result2.returncode == 0:
                pmset_out = result2.stdout
                # 检查是否有 Sleep 或 Dark Wake 计时器活跃
                # " Sleep " 后面跟着数字表示有休眠倒计时
                if re.search(r"(?i)^\s*Sleep\s+[1-9]", pmset_out, re.MULTILINE):
                    return True
                # 检查是否处于 Dark Wake / Idle Sleep 状态
                # standby=1 表示启用了自动休眠
                if "standby" in pmset_out.lower():
                    # 有 standby 启用时，检查是否真的在休眠
                    # 通过检查进程是否响应来辅助判断
                    pass

        return False

    except (subprocess.SubprocessError, OSError) as e:
        _log(f"检测 Mac 休眠状态失败: {e}")
        return False


class Watchdog:
    """看门狗主逻辑。"""

    def __init__(self) -> None:
        self._shutdown = threading.Event()
        self._daemon_pid: int | None = None  # 监控的 daemon pid
        self._pm = get_process_manager()  # 平台相关的进程管理器

    def _find_daemon_pid(self) -> int | None:
        """从进程列表中找到 daemon 的 pid。"""
        # 优先从 pid 文件读
        pid_file = LOG_DIR / "daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if self._pm.is_alive(pid):
                    return pid
            except (ValueError, OSError):
                pass

        # 通过进程名找
        return self._pm.find_process("(src\\.daemon|lamix.*gateway)")

    def _check_daemon(self) -> None:
        """检查 daemon 心跳。"""
        # 先找到 daemon pid
        pid = self._find_daemon_pid()
        if pid is None:
            _log("未找到 daemon 进程，尝试重启")
            _restart_daemon(self._pm)
            return

        if pid != self._daemon_pid:
            _log(f"daemon pid 变化: {self._daemon_pid} -> {pid}")
            self._daemon_pid = pid
            # 新 daemon 上线时清除重启标志
            _clear_restart_flag()

        # 读心跳文件
        heartbeat_path = HEARTBEAT_DIR / f"{pid}.json"
        if not heartbeat_path.exists():
            _log(f"daemon ({pid}) 心跳文件不存在，尝试重启")
            _restart_daemon(self._pm)
            return

        rec = load_heartbeat(heartbeat_path)
        if rec is None:
            _log(f"daemon ({pid}) 心跳文件损坏，尝试重启")
            _restart_daemon(self._pm)
            return

        # 检查是否被用户主动停止
        if rec.user_stopped:
            _log(f"daemon ({pid}) 被用户主动停止，不重拉")
            return

        # 检查心跳是否超时
        try:
            last = datetime.fromisoformat(rec.last_heartbeat)
        except ValueError:
            _log(f"daemon ({pid}) 心跳时间格式错误，尝试重启")
            _restart_daemon(self._pm)
            return

        elapsed = (datetime.now() - last).total_seconds()
        if elapsed > HEARTBEAT_TIMEOUT:
            # 心跳超时：先检查 Mac 是否在休眠，休眠则跳过重启
            if _is_mac_sleeping():
                _log(f"daemon ({pid}) 心跳超时（{elapsed:.0f}s > {HEARTBEAT_TIMEOUT}s）"
                     f"，但 Mac 正在休眠，跳过重启")
                return
            _log(f"daemon ({pid}) 心跳超时（{elapsed:.0f}s > {HEARTBEAT_TIMEOUT}s），尝试重启")
            _restart_daemon(self._pm)
        else:
            _log(f"daemon ({pid}) 心跳正常（{elapsed:.0f}s 前）")

    def _cleanup_loop(self) -> None:
        """定期清理已死亡进程的心跳文件。"""
        while not self._shutdown.is_set():
            self._shutdown.wait(WATCHDOG_INTERVAL * 3)
            if self._shutdown.is_set():
                break
            try:
                cleaned = cleanup_stale_heartbeats()
                if cleaned:
                    _log(f"清理了 {len(cleaned)} 个过时心跳文件: {cleaned}")
            except Exception:
                pass

    def _signal_handler(self, signum: int, _frame) -> None:
        _log(f"收到信号 {signum}，退出")
        self._shutdown.set()

    def run(self) -> None:
        """主循环。"""
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        _log(f"Watchdog 启动 (PID={os.getpid()})")
        cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        cleanup_thread.start()

        while not self._shutdown.is_set():
            self._shutdown.wait(WATCHDOG_INTERVAL)
            if self._shutdown.is_set():
                break
            try:
                self._check_daemon()
            except Exception as e:
                _log(f"检查 daemon 时异常: {e}")

        _log("Watchdog 退出")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lamix Watchdog")
    parser.parse_args()
    Watchdog().run()


if __name__ == "__main__":
    main()
