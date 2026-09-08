"""测试 src/core/process_launch.py 的 frozen 检测与 PID/lock 身份校验。

覆盖两处 bug：
1. Bug 1: frozen 环境下 daemon/watchdog 子进程启动命令必须走 CLI 内部子命令
   （lamix gateway daemon-run），不能用 `-m src.xxx`。
2. Bug 2: pid/lock 文件必须带 role 字段；读取时按 role 校验进程身份，
   老格式（纯 PID）向后兼容且身份不匹配时视为陈旧。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core import process_launch as pl


# ── Bug 1: frozen-aware launch commands ────────────────────────────────


class TestLaunchCmd:
    def test_daemon_cmd_source_mode(self):
        """源码环境应返回 python -m src.daemon"""
        with mock.patch.object(pl, "is_frozen", return_value=False):
            with mock.patch.object(pl.sys, "executable", "/usr/bin/python"):
                cmd = pl.daemon_launch_cmd()
        assert cmd == ["/usr/bin/python", "-m", "src.daemon"]

    def test_daemon_cmd_frozen_mode(self):
        """frozen 环境不能用 -m，必须走 gateway daemon-run 内部子命令"""
        with mock.patch.object(pl, "is_frozen", return_value=True):
            with mock.patch.object(
                pl.sys, "executable", "/Applications/Lamix.app/Contents/MacOS/lamix"
            ):
                cmd = pl.daemon_launch_cmd()
        assert cmd == [
            "/Applications/Lamix.app/Contents/MacOS/lamix",
            "gateway",
            "daemon-run",
        ]
        # 确保没有 -m 参数（这是原 bug 的根因）
        assert "-m" not in cmd
        assert "src.daemon" not in cmd

    def test_watchdog_cmd_source_mode(self):
        with mock.patch.object(pl, "is_frozen", return_value=False):
            with mock.patch.object(pl.sys, "executable", "/usr/bin/python"):
                cmd = pl.watchdog_launch_cmd()
        assert cmd == ["/usr/bin/python", "-m", "src.watchdog"]

    def test_watchdog_cmd_frozen_mode(self):
        """frozen 环境 watchdog 也必须走内部子命令"""
        with mock.patch.object(pl, "is_frozen", return_value=True):
            with mock.patch.object(
                pl.sys, "executable", "/Applications/Lamix.app/Contents/MacOS/lamix"
            ):
                cmd = pl.watchdog_launch_cmd()
        assert cmd == [
            "/Applications/Lamix.app/Contents/MacOS/lamix",
            "gateway",
            "watchdog-run",
        ]
        assert "-m" not in cmd
        assert "src.watchdog" not in cmd

    def test_is_frozen_reads_sys_frozen(self):
        """is_frozen() 直接反映 sys.frozen 属性"""
        # 默认（无 sys.frozen）返回 False
        # 用 monkeypatch 手动设置
        original = getattr(sys, "frozen", None)
        try:
            sys.frozen = True  # type: ignore[attr-defined]
            assert pl.is_frozen() is True
            del sys.frozen  # type: ignore[attr-defined]
            assert pl.is_frozen() is False
        finally:
            if original is not None:
                sys.frozen = original  # type: ignore[attr-defined]


# ── Bug 2: pid record read/write + identity check ──────────────────────


class TestPidRecord:
    def test_write_and_read_roundtrip(self, tmp_path):
        p = tmp_path / "daemon.pid"
        pl.write_pid_record(p, 12345, pl.ROLE_DAEMON)
        pid, role = pl.read_pid_record(p)
        assert pid == 12345
        assert role == pl.ROLE_DAEMON

    def test_write_pid_is_json(self, tmp_path):
        """新格式必须是可解析的 JSON，含 pid 和 role"""
        p = tmp_path / "watchdog.pid"
        pl.write_pid_record(p, 999, pl.ROLE_WATCHDOG)
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data == {"pid": 999, "role": "watchdog"}

    def test_read_legacy_plain_int(self, tmp_path):
        """老格式（纯 PID）依然能被读出，role 为 None"""
        p = tmp_path / "daemon.pid"
        p.write_text("54321")
        pid, role = pl.read_pid_record(p)
        assert pid == 54321
        assert role is None

    def test_read_missing_file(self, tmp_path):
        p = tmp_path / "does_not_exist.pid"
        pid, role = pl.read_pid_record(p)
        assert pid is None
        assert role is None

    def test_read_empty_file(self, tmp_path):
        p = tmp_path / "empty.pid"
        p.write_text("")
        pid, role = pl.read_pid_record(p)
        assert pid is None
        assert role is None

    def test_read_corrupt_json(self, tmp_path):
        p = tmp_path / "bad.pid"
        p.write_text("{not-json}")
        pid, role = pl.read_pid_record(p)
        assert pid is None
        assert role is None

    def test_read_garbage(self, tmp_path):
        p = tmp_path / "garbage.pid"
        p.write_text("not-a-number")
        pid, role = pl.read_pid_record(p)
        assert pid is None
        assert role is None

    def test_write_is_atomic(self, tmp_path):
        """连续写入不留下 .tmp 中间文件"""
        p = tmp_path / "daemon.pid"
        pl.write_pid_record(p, 1, pl.ROLE_DAEMON)
        pl.write_pid_record(p, 2, pl.ROLE_DAEMON)
        tmps = list(tmp_path.glob("*.tmp"))
        assert tmps == []


class TestPidRole:
    def test_pid_role_daemon_source(self):
        """python -m src.daemon 的 ps 输出应识别为 daemon"""
        with mock.patch.object(
            pl, "_ps_command", return_value="/venv/bin/python -m src.daemon"
        ):
            assert pl.pid_role(1234) == pl.ROLE_DAEMON

    def test_pid_role_daemon_frozen(self):
        """lamix gateway daemon-run 应识别为 daemon"""
        with mock.patch.object(
            pl,
            "_ps_command",
            return_value="/Applications/Lamix.app/Contents/MacOS/lamix gateway daemon-run",
        ):
            assert pl.pid_role(1234) == pl.ROLE_DAEMON

    def test_pid_role_watchdog_source(self):
        with mock.patch.object(
            pl, "_ps_command", return_value="/venv/bin/python -m src.watchdog"
        ):
            assert pl.pid_role(1234) == pl.ROLE_WATCHDOG

    def test_pid_role_watchdog_frozen(self):
        with mock.patch.object(
            pl,
            "_ps_command",
            return_value="/Applications/Lamix.app/Contents/MacOS/lamix gateway watchdog-run",
        ):
            assert pl.pid_role(1234) == pl.ROLE_WATCHDOG

    def test_pid_role_cli(self):
        """lamix cli 应识别为 cli"""
        with mock.patch.object(
            pl,
            "_ps_command",
            return_value="/Applications/Lamix.app/Contents/MacOS/lamix cli",
        ):
            assert pl.pid_role(1234) == pl.ROLE_CLI

    def test_pid_role_cli_with_query(self):
        """lamix cli 后带 query 参数依然识别为 cli"""
        with mock.patch.object(
            pl,
            "_ps_command",
            return_value="/usr/local/bin/lamix cli hello world",
        ):
            assert pl.pid_role(1234) == pl.ROLE_CLI

    def test_pid_role_dead_process(self):
        """ps 无输出（进程死了）→ None"""
        with mock.patch.object(pl, "_ps_command", return_value=""):
            assert pl.pid_role(1234) is None

    def test_pid_role_unrelated_process(self):
        """完全不相关的进程 → None"""
        with mock.patch.object(pl, "_ps_command", return_value="/usr/bin/fontdrvhost.exe"):
            assert pl.pid_role(1234) is None

    def test_pid_role_daemon_takes_precedence_over_cli_substring(self):
        """带 daemon 标识 + 也有 cli 子串时，daemon 优先"""
        with mock.patch.object(
            pl, "_ps_command",
            return_value="/venv/bin/python -m src.daemon cli-mode",
        ):
            # daemon 优先命中
            assert pl.pid_role(1234) == pl.ROLE_DAEMON


class TestIsRunningAs:
    def test_returns_false_for_none_pid(self):
        assert pl.is_running_as(None, pl.ROLE_DAEMON) is False

    def test_returns_false_for_zero_pid(self):
        assert pl.is_running_as(0, pl.ROLE_DAEMON) is False

    def test_returns_false_when_process_dead(self):
        with mock.patch.object(pl, "process_exists", return_value=False):
            assert pl.is_running_as(1234, pl.ROLE_DAEMON) is False

    def test_cli_pid_is_not_running_as_daemon(self):
        """核心 Bug 2 场景：lock 里 cli 的 PID 存活但身份不是 daemon → 不算 daemon 在运行"""
        with mock.patch.object(pl, "process_exists", return_value=True):
            with mock.patch.object(pl, "pid_role", return_value=pl.ROLE_CLI):
                assert pl.is_running_as(1234, pl.ROLE_DAEMON) is False

    def test_daemon_pid_is_running_as_daemon(self):
        with mock.patch.object(pl, "process_exists", return_value=True):
            with mock.patch.object(pl, "pid_role", return_value=pl.ROLE_DAEMON):
                assert pl.is_running_as(1234, pl.ROLE_DAEMON) is True

    def test_unknown_role_treated_conservatively(self):
        """无法辨识身份时，不算 running（避免误判为已在运行）"""
        with mock.patch.object(pl, "process_exists", return_value=True):
            with mock.patch.object(pl, "pid_role", return_value=None):
                assert pl.is_running_as(1234, pl.ROLE_DAEMON) is False


# ── cli.py 的 _acquire_instance_lock role 语义 ───────────────────────


class TestInstanceLockRole:
    """验证 cli 持有的 instance.lock 不会阻挡 daemon 的启动。"""

    def _reset_atexit(self, monkeypatch):
        # 避免测试污染 atexit 回调
        import atexit
        monkeypatch.setattr(atexit, "register", lambda *a, **k: None)

    def test_cli_lock_does_not_block_daemon(self, tmp_path, monkeypatch):
        """cli 持有 instance.lock 后，daemon role 仍可以覆写。"""
        from src import cli

        # 隔离 lock 路径到临时目录
        lock_path = tmp_path / "instance.lock"
        monkeypatch.setattr(cli, "_INSTANCE_LOCK_PATH", lock_path)
        monkeypatch.setattr(cli, "LAMIX_DIR", tmp_path)
        self._reset_atexit(monkeypatch)

        # 模拟一个还活着的 cli 进程占了锁
        pl.write_pid_record(lock_path, 99999, pl.ROLE_CLI)
        # 让 _proc_exists 认定 99999 存活
        monkeypatch.setattr(cli, "_proc_exists", lambda pid: pid == 99999)

        # daemon 尝试拿锁 → 允许，且锁被改写为 daemon
        ok = cli._acquire_instance_lock(role=pl.ROLE_DAEMON)
        assert ok is True
        pid, role = pl.read_pid_record(lock_path)
        assert pid == os.getpid()
        assert role == pl.ROLE_DAEMON

    def test_same_role_alive_holder_blocks(self, tmp_path, monkeypatch):
        """同 role 的持有者仍在跑 → 拒绝（保留原语义）"""
        from src import cli

        lock_path = tmp_path / "instance.lock"
        monkeypatch.setattr(cli, "_INSTANCE_LOCK_PATH", lock_path)
        monkeypatch.setattr(cli, "LAMIX_DIR", tmp_path)
        self._reset_atexit(monkeypatch)

        pl.write_pid_record(lock_path, 99999, pl.ROLE_CLI)
        monkeypatch.setattr(cli, "_proc_exists", lambda pid: pid == 99999)
        # ps 确认身份也是 cli（cli 模块 import 了 pid_role，patch cli 里的名字）
        monkeypatch.setattr(cli, "pid_role", lambda pid: pl.ROLE_CLI)

        ok = cli._acquire_instance_lock(role=pl.ROLE_CLI)
        assert ok is False
        # 原锁不动
        pid, role = pl.read_pid_record(lock_path)
        assert pid == 99999
        assert role == pl.ROLE_CLI

    def test_legacy_lock_stale_pid_gets_overwritten(self, tmp_path, monkeypatch):
        """老格式（纯 PID）且持有者已死 → 覆写成功"""
        from src import cli

        lock_path = tmp_path / "instance.lock"
        monkeypatch.setattr(cli, "_INSTANCE_LOCK_PATH", lock_path)
        monkeypatch.setattr(cli, "LAMIX_DIR", tmp_path)
        self._reset_atexit(monkeypatch)

        # 老格式：纯 PID，没有 role
        lock_path.write_text("12345")
        monkeypatch.setattr(cli, "_proc_exists", lambda pid: False)  # 已死

        ok = cli._acquire_instance_lock(role=pl.ROLE_CLI)
        assert ok is True
        pid, role = pl.read_pid_record(lock_path)
        assert pid == os.getpid()
        assert role == pl.ROLE_CLI

    def test_legacy_lock_alive_pid_identity_mismatch_gets_overwritten(
        self, tmp_path, monkeypatch
    ):
        """老格式 + PID 存活但真实身份≠请求 role → 覆写（防误判）"""
        from src import cli

        lock_path = tmp_path / "instance.lock"
        monkeypatch.setattr(cli, "_INSTANCE_LOCK_PATH", lock_path)
        monkeypatch.setattr(cli, "LAMIX_DIR", tmp_path)
        self._reset_atexit(monkeypatch)

        lock_path.write_text("99999")  # 老格式，无 role
        monkeypatch.setattr(cli, "_proc_exists", lambda pid: pid == 99999)
        # 但实际这个 PID 是个 cli 进程（patch cli 模块里的 pid_role 名字）
        monkeypatch.setattr(cli, "pid_role", lambda pid: pl.ROLE_CLI)

        # 请求 daemon lock → 应允许覆写
        ok = cli._acquire_instance_lock(role=pl.ROLE_DAEMON)
        assert ok is True
        pid, role = pl.read_pid_record(lock_path)
        assert pid == os.getpid()
        assert role == pl.ROLE_DAEMON


# ── cli.py 的 _is_daemon_running 身份校验 ────────────────────────────


class TestIsDaemonRunning:
    def test_cli_pid_in_daemon_pid_file_not_treated_as_daemon(self, tmp_path, monkeypatch):
        """Bug 2 核心：daemon.pid 里是 cli 的 PID 时，_is_daemon_running 必须返回 False"""
        from src import cli

        pid_file = tmp_path / "daemon.pid"
        pl.write_pid_record(pid_file, 88888, pl.ROLE_CLI)  # 故意写错的 role
        monkeypatch.setattr(
            Path, "home", classmethod(lambda cls: tmp_path.parent / tmp_path.name / "home")
        )
        # 直接 patch cli 里读的路径
        real_home = tmp_path

        def fake_pid_path(*a, **k):
            return pid_file

        # 更简单：patch read_pid_record 返回我们准备的数据
        monkeypatch.setattr(cli, "read_pid_record", lambda p: (88888, pl.ROLE_CLI))
        monkeypatch.setattr(cli, "is_running_as", lambda pid, role: False)

        assert cli._is_daemon_running() is False

    def test_alive_daemon_returns_true(self, monkeypatch):
        from src import cli

        monkeypatch.setattr(cli, "read_pid_record", lambda p: (12345, pl.ROLE_DAEMON))
        monkeypatch.setattr(
            cli,
            "is_running_as",
            lambda pid, role: pid == 12345 and role == pl.ROLE_DAEMON,
        )
        assert cli._is_daemon_running() is True

    def test_missing_pid_file_returns_false(self, monkeypatch):
        from src import cli

        monkeypatch.setattr(cli, "read_pid_record", lambda p: (None, None))
        assert cli._is_daemon_running() is False


# ── argparse: gateway 新增内部子命令能被解析 ────────────────────────


class TestGatewayInternalSubcommands:
    def test_daemon_run_subcommand_parses(self):
        from src import cli

        parser = cli._build_parser()
        args = parser.parse_args(["gateway", "daemon-run"])
        assert args.command == "gateway"
        assert args.gateway_action == "daemon-run"

    def test_watchdog_run_subcommand_parses(self):
        from src import cli

        parser = cli._build_parser()
        args = parser.parse_args(["gateway", "watchdog-run"])
        assert args.command == "gateway"
        assert args.gateway_action == "watchdog-run"

    def test_existing_gateway_subcommands_still_work(self):
        """回归：start/stop/restart 未被破坏"""
        from src import cli

        parser = cli._build_parser()
        for action in ("start", "stop", "restart"):
            args = parser.parse_args(["gateway", action])
            assert args.gateway_action == action
