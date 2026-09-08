"""第四轮修复测试：watchdog 循环重启残留 3 bug。

Bug A: restart_daemon 杀旧进程 — JSON pid 文件兼容
Bug B: watchdog 心跳判定启动宽限期
Bug C: 上线通知冷却
"""

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# setproctitle mock（daemon.py 顶层 import setproctitle）
_mock_setproctitle = __import__("types", fromlist=[""]).ModuleType("setproctitle")
_mock_setproctitle.setproctitle = lambda *a, **k: None
_mock_setproctitle.getproctitle = lambda: ""
sys.modules["setproctitle"] = _mock_setproctitle


# ─── Bug A: restart_daemon JSON pid 文件 ──────────────────────────────────


class TestBugA_RestartDaemonPidParsing:
    """restart_daemon 应正确解析 JSON 格式 pid 文件并杀旧进程。"""

    def test_posix_restart_daemon_json_pid(self, tmp_path):
        """JSON 格式 pid 文件 → restart_daemon 能取出 pid 并调用 kill_process。"""
        from src.platforms.posix_process_manager import PosixProcessManager

        pm = PosixProcessManager()
        pid_file = tmp_path / "daemon.pid"
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        # 写 JSON 格式 pid 文件
        pid_file.write_text(json.dumps({"pid": 12345, "role": "daemon"}), encoding="utf-8")

        with patch.object(pm, "kill_process", return_value=True) as mock_kill, \
             patch.object(pm, "_restart_via_popen", return_value=True) as mock_popen:
            result = pm.restart_daemon(
                daemon_command=["python", "-m", "src.daemon"],
                pid_file=pid_file,
                log_dir=log_dir,
            )

        assert result is True
        # 核心断言：kill_process 收到的是正确的 pid（12345），而不是 None
        mock_kill.assert_called_once_with(12345, graceful=True)

    def test_posix_restart_daemon_plain_int_pid(self, tmp_path):
        """老纯整数格式 → 不回归，仍能正常解析。"""
        from src.platforms.posix_process_manager import PosixProcessManager

        pm = PosixProcessManager()
        pid_file = tmp_path / "daemon.pid"
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        # 纯整数格式
        pid_file.write_text("99999", encoding="utf-8")

        with patch.object(pm, "kill_process", return_value=True) as mock_kill, \
             patch.object(pm, "_restart_via_popen", return_value=True):
            pm.restart_daemon(
                daemon_command=["python", "-m", "src.daemon"],
                pid_file=pid_file,
                log_dir=log_dir,
            )

        mock_kill.assert_called_once_with(99999, graceful=True)

    def test_posix_restart_daemon_no_pid_file(self, tmp_path):
        """pid 文件不存在 → 不调用 kill_process，直接拉新进程。"""
        from src.platforms.posix_process_manager import PosixProcessManager

        pm = PosixProcessManager()
        pid_file = tmp_path / "daemon.pid"  # 不创建
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        with patch.object(pm, "kill_process") as mock_kill, \
             patch.object(pm, "_restart_via_popen", return_value=True):
            pm.restart_daemon(
                daemon_command=["python", "-m", "src.daemon"],
                pid_file=pid_file,
                log_dir=log_dir,
            )

        mock_kill.assert_not_called()

    def test_posix_cleanup_pid_file_json_format(self, tmp_path):
        """_cleanup_pid_file 在 JSON 格式下能正确匹配 pid 并删除文件。"""
        from src.platforms.posix_process_manager import PosixProcessManager

        pm = PosixProcessManager()

        # LAMIX_DIR 在 kill_process 内部 lazy import，mock config 模块
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(exist_ok=True)
        actual_pid_file = logs_dir / "daemon.pid"
        actual_pid_file.write_text(json.dumps({"pid": 42, "role": "daemon"}), encoding="utf-8")

        # 模拟进程已死 → kill_process 直接走 _cleanup_pid_file
        with patch.object(pm, "is_alive", return_value=False), \
             patch("src.core.config.LAMIX_DIR", tmp_path):
            result = pm.kill_process(42, graceful=True)

        assert result is True
        assert not actual_pid_file.exists()

    def test_windows_restart_daemon_json_pid(self, tmp_path):
        """Windows: JSON 格式 pid → 正确解析并杀旧。"""
        from src.platforms.windows.process_manager import WindowsProcessManager

        pm = WindowsProcessManager()
        pid_file = tmp_path / "daemon.pid"
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        pid_file.write_text(json.dumps({"pid": 54321, "role": "daemon"}), encoding="utf-8")

        with patch.object(pm, "kill_process", return_value=True) as mock_kill, \
             patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 99
            mock_popen.return_value = mock_proc
            pm.restart_daemon(
                daemon_command=["python", "-m", "src.daemon"],
                pid_file=pid_file,
                log_dir=log_dir,
            )

        mock_kill.assert_called_once_with(54321, graceful=True)


# ─── Bug B: watchdog 启动宽限期 ──────────────────────────────────────────


class TestBugB_WatchdogStartupGrace:
    """watchdog 在 daemon pid 变化后的宽限期内不应因心跳文件缺失而重启。"""

    def _make_watchdog(self):
        from src.watchdog import Watchdog
        wd = Watchdog()
        wd._pm = MagicMock()
        return wd

    @patch("src.watchdog._restart_daemon")
    @patch("src.watchdog.load_heartbeat")
    def test_no_restart_during_grace_period(self, mock_load_hb, mock_restart, tmp_path):
        """pid 刚变化 + 心跳文件不存在 → 不触发 restart。"""
        wd = self._make_watchdog()

        # 模拟 pid 发现
        wd._pm.is_alive.return_value = True
        wd._daemon_pid = None  # 首次发现

        # 模拟 heartbeat 目录（空，无心跳文件）
        with patch("src.watchdog.HEARTBEAT_DIR", tmp_path), \
             patch("src.watchdog.LOG_DIR", tmp_path), \
             patch("src.watchdog.read_pid_record", return_value=(1234, "daemon")):
            # 第一次 check：pid 从 None → 1234（触发 _pid_changed_at 记录）
            # 心跳文件不存在 → 但在宽限期内 → 不 restart
            wd._check_daemon()

        mock_restart.assert_not_called()

    @patch("src.watchdog._restart_daemon")
    @patch("src.watchdog.load_heartbeat")
    def test_restart_after_grace_period(self, mock_load_hb, mock_restart, tmp_path):
        """pid 变化超过 60s + 心跳文件仍不存在 → 触发 restart。"""
        wd = self._make_watchdog()

        # 模拟 pid 已经 set 且过了宽限期
        wd._daemon_pid = 1234
        wd._pid_changed_at = time.time() - 120  # 120 秒前

        wd._pm.is_alive.return_value = True

        with patch("src.watchdog.HEARTBEAT_DIR", tmp_path), \
             patch("src.watchdog.LOG_DIR", tmp_path), \
             patch("src.watchdog.read_pid_record", return_value=(1234, "daemon")):
            wd._check_daemon()

        mock_restart.assert_called_once()

    @patch("src.watchdog._restart_daemon")
    def test_no_restart_when_heartbeat_fresh(self, mock_restart, tmp_path):
        """心跳文件存在且新鲜 → 不触发 restart（不受宽限期影响）。"""
        wd = self._make_watchdog()
        wd._daemon_pid = 5678
        wd._pid_changed_at = time.time() - 5  # 5 秒前

        wd._pm.is_alive.return_value = True

        # 写一个新鲜的心跳文件
        hb_file = tmp_path / "5678.json"
        hb_data = {
            "pid": 5678,
            "task_id": "daemon",
            "user_stopped": False,
            "last_heartbeat": datetime.now().isoformat(),
        }
        hb_file.write_text(json.dumps(hb_data), encoding="utf-8")

        hb_record = MagicMock()
        hb_record.user_stopped = False
        hb_record.last_heartbeat = datetime.now().isoformat()

        with patch("src.watchdog.HEARTBEAT_DIR", tmp_path), \
             patch("src.watchdog.LOG_DIR", tmp_path), \
             patch("src.watchdog.read_pid_record", return_value=(5678, "daemon")), \
             patch("src.watchdog.load_heartbeat", return_value=hb_record):
            wd._check_daemon()

        mock_restart.assert_not_called()


# ─── Bug C: 上线通知冷却 ─────────────────────────────────────────────────


class TestBugC_NotifyCooldown:
    """上线通知应有 600s 冷却期，避免 watchdog 循环重启时轰炸用户。"""

    def test_first_send_succeeds_and_records(self, tmp_path):
        """首次发送成功并写入时间戳。"""
        from src.daemon import (
            _check_notify_cooldown,
            _record_notify_sent,
            _LAST_NOTIFY_PATH,
        )

        notify_path = tmp_path / "last_online_notify.json"

        with patch("src.daemon._LAST_NOTIFY_PATH", notify_path):
            # 文件不存在 → 不冷却
            assert _check_notify_cooldown() is False

            # 记录发送
            _record_notify_sent()
            assert notify_path.exists()
            data = json.loads(notify_path.read_text(encoding="utf-8"))
            assert "last_sent" in data

    def test_cooldown_within_600s(self, tmp_path):
        """600s 内第二次调用 → 冷却中。"""
        from src.daemon import _check_notify_cooldown, _record_notify_sent

        notify_path = tmp_path / "last_online_notify.json"

        with patch("src.daemon._LAST_NOTIFY_PATH", notify_path):
            # 写入"刚发过"的时间戳
            notify_path.write_text(
                json.dumps({"last_sent": datetime.now().isoformat()}),
                encoding="utf-8",
            )
            assert _check_notify_cooldown() is True

    def test_no_cooldown_after_600s(self, tmp_path):
        """超过 600s → 可以再次发送。"""
        from src.daemon import _check_notify_cooldown

        notify_path = tmp_path / "last_online_notify.json"
        old_time = (datetime.now() - timedelta(seconds=700)).isoformat()

        with patch("src.daemon._LAST_NOTIFY_PATH", notify_path):
            notify_path.write_text(
                json.dumps({"last_sent": old_time}),
                encoding="utf-8",
            )
            assert _check_notify_cooldown() is False

    def test_corrupted_file_allows_send(self, tmp_path):
        """文件损坏 → 视为可发送。"""
        from src.daemon import _check_notify_cooldown

        notify_path = tmp_path / "last_online_notify.json"
        notify_path.write_text("not json {{{{", encoding="utf-8")

        with patch("src.daemon._LAST_NOTIFY_PATH", notify_path):
            assert _check_notify_cooldown() is False

    def test_send_boot_notification_skips_during_cooldown(self, tmp_path):
        """_send_boot_notification 在冷却期内不发送。"""
        from src.daemon import _send_boot_notification

        notify_path = tmp_path / "last_online_notify.json"
        notify_path.write_text(
            json.dumps({"last_sent": datetime.now().isoformat()}),
            encoding="utf-8",
        )

        config = {
            "feishu": {
                "app_id": "test_id",
                "app_secret": "test_secret",
                "owner_chat_id": "test_chat",
                "user_open_id": "test_open",
            }
        }

        with patch("src.daemon._LAST_NOTIFY_PATH", notify_path), \
             patch("src.daemon.FeishuClient", create=True) as mock_client_cls:
            _send_boot_notification(config, pid=999, is_recovery=False)
            # FeishuClient 不应被实例化（冷却跳过了整个发送逻辑）
            mock_client_cls.assert_not_called()
