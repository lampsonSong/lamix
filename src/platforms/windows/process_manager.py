"""Windows 进程管理实现。

使用 wmic/tasklist 进行进程查找和存活检测。
通过 stop.flag 文件实现优雅终止（Windows 下 SIGTERM 不可靠）。
使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 创建独立 daemon 进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from src.platforms.process_manager import ProcessManager
from src.core.config import LAMIX_DIR

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
# 无窗口控制台：daemon 及其子进程（如心跳 multiprocessing 子进程）都继承这个不可见控制台，
# 否则 console-less 的 daemon 再 spawn python.exe 子进程时会弹出可见的黑框。
CREATE_NO_WINDOW = 0x08000000


class WindowsProcessManager(ProcessManager):
    """Windows 进程管理器。"""

    def find_process(self, command_pattern: str) -> int | None:
        """通过 wmic 查找匹配命令行的 python 进程。"""
        try:
            result = subprocess.run(
                [
                    "wmic", "process", "where",
                    f"Name='python.exe' and CommandLine like '%{command_pattern}%'",
                    "get", "ProcessId",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                for line in lines[1:]:  # 跳过标题行
                    line = line.strip()
                    if line.isdigit():
                        pid = int(line)
                        if pid != os.getpid():
                            return pid
        except Exception:
            pass
        return None

    def is_alive(self, pid: int) -> bool:
        """通过 tasklist 检查进程是否存活。"""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        except Exception:
            return False

    def kill_process(self, pid: int, graceful: bool = True) -> bool:
        """终止进程。

        graceful=True: 写 stop.flag，等待进程自行退出（最多 10s），超时则强杀。
        graceful=False: taskkill /F 强制终止。
        """
        if not self.is_alive(pid):
            return True

        try:
            if graceful:
                stop_flag = LAMIX_DIR / "stop.flag"
                stop_flag.parent.mkdir(parents=True, exist_ok=True)
                stop_flag.write_text(str(pid), encoding="utf-8")

                for _ in range(100):  # 最多等 10s
                    time.sleep(0.1)
                    if not self.is_alive(pid):
                        try:
                            stop_flag.unlink()
                        except OSError:
                            pass
                        return True

                # 超时，强杀
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=5,
                )
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=5,
                )
            return True
        except Exception:
            return False

    def restart_daemon(
        self,
        daemon_command: list[str],
        pid_file: Path,
        log_dir: Path,
        cwd: Path | None = None,
    ) -> bool:
        """通过 Popen 启动 daemon 进程。

        使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 使 daemon
        独立于 watchdog 进程组，watchdog 退出不会连带杀 daemon。
        """
        # 终止旧进程
        if pid_file.exists():
            try:
                old_pid = int(pid_file.read_text().strip())
                self.kill_process(old_pid, graceful=True)
            except (ValueError, OSError):
                pass

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_log = log_dir / "daemon_stdout.log"
            stderr_log = log_dir / "daemon_stderr.log"

            work_dir = str(cwd) if cwd else str(Path.cwd())

            with (
                open(stdout_log, "a", encoding="utf-8") as out_f,
                open(stderr_log, "a", encoding="utf-8") as err_f,
            ):
                proc = subprocess.Popen(
                    daemon_command,
                    stdout=out_f,
                    stderr=err_f,
                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                    cwd=work_dir,
                )

            # 写 pid 文件
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(proc.pid), encoding="utf-8")
            return True
        except Exception:
            return False
