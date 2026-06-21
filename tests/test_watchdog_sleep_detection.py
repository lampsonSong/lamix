"""测试 watchdog 睡眠恢复检测逻辑。

核心场景：Mac 合盖睡眠 → 短暂唤醒 → watchdog 不应杀 daemon。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.watchdog import (
    SLEEP_DETECT_MULTIPLIER,
    SLEEP_WAKE_GRACE_PERIOD,
    Watchdog,
)


def _make_watchdog():
    """创建一个 Watchdog 实例（不启动循环）。"""
    wd = Watchdog()
    wd._daemon_pid = None
    return wd


class TestSleepDetection:
    """测试睡眠恢复检测。"""

    def test_normal_interval_no_grace(self):
        """正常运行时（间隔 ~10s），不应触发宽限期。"""
        wd = _make_watchdog()
        wd._last_check_time = time.time() - 10  # 10s 前

        # 模拟 run() 中的检测逻辑
        now_ts = time.time()
        gap = now_ts - wd._last_check_time
        if gap > 10 * SLEEP_DETECT_MULTIPLIER:
            wd._sleep_grace_until = now_ts + SLEEP_WAKE_GRACE_PERIOD

        assert wd._sleep_grace_until == 0  # 没有触发宽限期

    def test_large_interval_triggers_grace(self):
        """watchdog 间隔跳跃（如 930s），应触发宽限期。"""
        wd = _make_watchdog()
        wd._last_check_time = time.time() - 930  # 模拟 930s 前（=系统睡眠 ~15 分钟）

        now_ts = time.time()
        gap = now_ts - wd._last_check_time
        if gap > 10 * SLEEP_DETECT_MULTIPLIER:
            wd._sleep_grace_until = now_ts + SLEEP_WAKE_GRACE_PERIOD

        assert wd._sleep_grace_until > now_ts  # 宽限期已激活

    def test_grace_period_blocks_restart(self):
        """心跳超时 + 宽限期内 → 不应重启。"""
        wd = _make_watchdog()
        wd._sleep_grace_until = time.time() + 20  # 20s 后才过期

        # 模拟心跳超时场景
        assert time.time() < wd._sleep_grace_until  # 宽限期有效

    def test_grace_period_expired_allows_restart(self):
        """心跳超时 + 宽限期已过 → 可以正常重启。"""
        wd = _make_watchdog()
        wd._sleep_grace_until = time.time() - 1  # 1s 前已过期

        assert time.time() >= wd._sleep_grace_until  # 宽限期已过

    def test_first_run_no_false_sleep_detection(self):
        """首次运行（_last_check_time=None）不应误判为睡眠。"""
        wd = _make_watchdog()
        assert wd._last_check_time is None
        assert wd._sleep_grace_until == 0

    def test_threshold_boundary(self):
        """边界值：间隔正好等于 WATCHDOG_INTERVAL * MULTIPLIER 不触发。"""
        wd = _make_watchdog()
        from src.core.constants import WATCHDOG_INTERVAL

        # 间隔 = 阈值 - 1s（不触发）
        wd._last_check_time = time.time() - (WATCHDOG_INTERVAL * SLEEP_DETECT_MULTIPLIER - 1)
        gap = time.time() - wd._last_check_time
        assert gap <= WATCHDOG_INTERVAL * SLEEP_DETECT_MULTIPLIER

    def test_multiple_sleep_cycles_reset_grace(self):
        """多次睡眠循环：每次醒来都应重置宽限期。"""
        wd = _make_watchdog()

        # 第一轮睡眠恢复
        wd._last_check_time = time.time() - 600
        now_ts = time.time()
        gap = now_ts - wd._last_check_time
        if gap > 10 * SLEEP_DETECT_MULTIPLIER:
            wd._sleep_grace_until = now_ts + SLEEP_WAKE_GRACE_PERIOD
        grace1 = wd._sleep_grace_until

        # 第二轮睡眠恢复（假设过了很长时间）
        wd._last_check_time = time.time() - 900
        now_ts2 = time.time()
        gap2 = now_ts2 - wd._last_check_time
        if gap2 > 10 * SLEEP_DETECT_MULTIPLIER:
            wd._sleep_grace_until = now_ts2 + SLEEP_WAKE_GRACE_PERIOD
        grace2 = wd._sleep_grace_until

        assert grace2 > grace1  # 第二次宽限期比第一次更晚


class TestCheckDaemonWithSleep:
    """测试 _check_daemon 在睡眠场景下的行为。"""

    @patch("src.watchdog._restart_daemon")
    @patch("src.watchdog.load_heartbeat")
    @patch.object(Watchdog, "_find_daemon_pid", return_value=12345)
    def test_no_restart_during_grace_period(self, mock_pid, mock_hb, mock_restart):
        """心跳超时 + 宽限期内 → _check_daemon 不应调用 _restart_daemon。"""
        from src.core.heartbeat import HeartbeatRecord

        mock_hb.return_value = HeartbeatRecord(
            pid=12345,
            task_id="daemon",
            user_stopped=False,
            last_heartbeat=(datetime.now() - timedelta(seconds=300)).isoformat(),
        )

        # 确保心跳文件存在
        with patch("pathlib.Path.exists", return_value=True):
            wd = _make_watchdog()
            wd._sleep_grace_until = time.time() + 30  # 宽限期内

            wd._check_daemon()
            mock_restart.assert_not_called()

    @patch("src.watchdog._restart_daemon")
    @patch("src.watchdog.load_heartbeat")
    @patch.object(Watchdog, "_find_daemon_pid", return_value=12345)
    def test_restart_after_grace_expired(self, mock_pid, mock_hb, mock_restart):
        """心跳超时 + 宽限期已过 → _check_daemon 应调用 _restart_daemon。"""
        from src.core.heartbeat import HeartbeatRecord

        mock_hb.return_value = HeartbeatRecord(
            pid=12345,
            task_id="daemon",
            user_stopped=False,
            last_heartbeat=(datetime.now() - timedelta(seconds=300)).isoformat(),
        )

        with patch("pathlib.Path.exists", return_value=True):
            wd = _make_watchdog()
            wd._sleep_grace_until = time.time() - 10  # 宽限期已过

            wd._check_daemon()
            mock_restart.assert_called_once()
