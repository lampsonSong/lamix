"""POSIX (macOS/Linux) 进程管理实现。

macOS 使用 launchd 管理 daemon，Linux 使用直接进程拉起。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from src.core.process_launch import read_pid_record
from src.platforms.process_manager import ProcessManager

logger = logging.getLogger(__name__)

# macOS launchd 服务标签
_DAEMON_LAUNCHCTL_LABEL = "com.lamix.gateway"


class PosixProcessManager(ProcessManager):
    """macOS / Linux 进程管理。"""

    def find_process(self, command_pattern: str) -> int | None:
        """通过 pgrep 查找匹配命令行模式的进程。"""
        # 优先从 pid 文件读取
        from src.core.config import LAMIX_DIR

        pid_file = LAMIX_DIR / "logs" / "daemon.pid"
        pid, _ = read_pid_record(pid_file)
        if pid and self.is_alive(pid):
            return pid

        # 通过 pgrep 查找
        try:
            result = subprocess.run(
                ["pgrep", "-f", command_pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
                for pid in pids:
                    if pid != os.getpid():
                        return pid
        except Exception:
            pass
        return None

    def is_alive(self, pid: int, *, exclude_zombie: bool = True) -> bool:
        """检查进程是否存活。

        Args:
            pid: 目标进程 ID
            exclude_zombie: True 时僵尸进程（state=Z）视为已死。
                            僵尸进程 os.kill(pid,0) 仍成功，但已无实际功能，
                            不应阻止新实例启动。
        """
        try:
            os.kill(pid, 0)
        except OSError:
            return False

        if not exclude_zombie:
            return True

        # 通过 /proc 或 ps 检查进程状态，排除僵尸
        return not self._is_zombie(pid)

    @staticmethod
    def _is_zombie(pid: int) -> bool:
        """检查进程是否为僵尸状态 (state=Z)。

        优先读 /proc/{pid}/stat（Linux），fallback 到 ps 命令（macOS）。
        """
        # Linux: 直接读 /proc
        stat_path = f"/proc/{pid}/stat"
        try:
            with open(stat_path) as f:
                # 格式: pid (comm) state ...
                # comm 可能含空格和括号，从最后一个 ')' 后取 state
                raw = f.read()
                close_paren = raw.rfind(")")
                if close_paren >= 0:
                    state = raw[close_paren + 1:].strip().split()[0]
                    return state in ("Z", "X")
        except (FileNotFoundError, IndexError, PermissionError):
            pass

        # macOS / fallback: 用 ps
        try:
            result = subprocess.run(
                ["ps", "-o", "state=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            )
            state = result.stdout.strip()
            return state == "Z"
        except Exception:
            return False

    def kill_process(self, pid: int, graceful: bool = True) -> bool:
        """通过 SIGTERM/SIGKILL 终止进程。

        僵尸进程（state=Z）无法被信号杀死，直接视为已死并清理 PID 文件。
        """
        from src.core.config import LAMIX_DIR

        pid_file = LAMIX_DIR / "logs" / "daemon.pid"

        def _cleanup_pid_file() -> None:
            """如果 PID 文件记录的是本 pid，则删除。"""
            try:
                stored_pid, _ = read_pid_record(pid_file)
                if stored_pid == pid:
                    pid_file.unlink(missing_ok=True)
            except OSError:
                pass

        # 进程不存在（包括僵尸），直接清理
        if not self.is_alive(pid):
            _cleanup_pid_file()
            return True

        try:
            if graceful:
                os.kill(pid, signal.SIGTERM)
                # 等待最多 5 秒
                for _ in range(50):
                    if not self.is_alive(pid):
                        return True
                    time.sleep(0.1)
                logger.warning("进程 %d 未在 5s 内退出，强杀", pid)

            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
            # SIGKILL 后再检查（僵尸则 is_alive 返回 False）
            dead = not self.is_alive(pid)
            if dead:
                _cleanup_pid_file()
            return dead
        except OSError:
            dead = not self.is_alive(pid)
            if dead:
                _cleanup_pid_file()
            return dead

    def restart_daemon(
        self,
        daemon_command: list[str],
        pid_file: Path,
        log_dir: Path,
        cwd: Path | None = None,
    ) -> bool:
        """重启 daemon：kill 旧进程 + 清理 PID 文件 + Popen 拉起新进程。

        关键：kill 旧进程后必须清理 PID 文件，否则新 daemon 的
        _check_single_instance() 会读到残留 PID 并拒绝启动。
        但 Popen 写入的是 wrapper PID（非实际 daemon PID），
        所以这里只做清理，让 daemon 自己在 _write_daemon_pid() 中写入真实 PID。
        """
        # 先尝试终止旧进程
        old_pid, _ = read_pid_record(pid_file)

        if old_pid is not None:
            self.kill_process(old_pid, graceful=True)

        # 确保 PID 文件被清理（kill_process 内部已清理，这里兜底）
        pid_file.unlink(missing_ok=True)

        return self._restart_via_popen(daemon_command, pid_file, log_dir, cwd)

    def _restart_via_launchctl(self) -> bool:
        """macOS: 通过 launchctl kickstart 重启。"""
        try:
            result = subprocess.run(
                [
                    "launchctl",
                    "kickstart",
                    "-k",
                    f"gui/{os.getuid()}/{_DAEMON_LAUNCHCTL_LABEL}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                logger.info("launchctl kickstart 成功")
                return True
            else:
                logger.error("launchctl kickstart 失败: %s", result.stderr)
                return False
        except Exception as e:
            logger.error("launchctl kickstart 异常: %s", e)
            return False

    def _restart_via_popen(
        self,
        daemon_command: list[str],
        pid_file: Path,
        log_dir: Path,
        cwd: Path | None,
    ) -> bool:
        """通过 Popen 拉起 daemon 进程。

        注意：Popen 返回的是 wrapper（bash）进程的 PID，
        daemon 内部 setproctitle("lamix") 后会通过 _write_daemon_pid()
        写入自己的真实 PID，覆盖这里的 wrapper PID。
        所以这里写入 wrapper PID 只是临时占位，daemon 启动后会自动更新。
        """
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_log = open(log_dir / "daemon.log", "a", encoding="utf-8")
            stderr_log = open(log_dir / "daemon_error.log", "a", encoding="utf-8")

            proc = subprocess.Popen(
                daemon_command,
                cwd=str(cwd) if cwd else None,
                stdout=stdout_log,
                stderr=stderr_log,
                start_new_session=True,  # detach from parent process group
            )

            # 临时写入 wrapper PID，daemon 启动后会覆盖为自己的 PID
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(proc.pid), encoding="utf-8")

            logger.info("daemon 已启动 (wrapper PID=%d，等待 daemon 写入真实 PID)", proc.pid)
            return True
        except Exception as e:
            logger.error("Popen 启动 daemon 失败: %s", e)
            return False
