"""TaskScheduler 测试。"""

import time
import threading
from unittest.mock import MagicMock, patch

from src.core.task_scheduler.triggers import TaskConfig, TaskType
from src.core.task_scheduler.scheduler import TaskScheduler, _default_notify


def _make_scheduler() -> TaskScheduler:
    """创建测试用调度器（不持久化，避免 SQLite 文件残留）。"""
    s = TaskScheduler()  # 正常走 __init__，确保 _session 等属性初始化
    return s


class TestDelayedTask:
    def test_delayed_fires_once(self):
        s = _make_scheduler()
        result = []
        config = TaskConfig(
            task_id="test_delayed",
            task_type=TaskType.DELAYED,
            trigger_seconds=1,
            func=lambda: result.append("fired"),
        )
        s.schedule(config)
        s.start()
        time.sleep(2)
        s.shutdown(wait=False)
        assert result == ["fired"]

    def test_delayed_with_args(self):
        s = _make_scheduler()
        result = []
        config = TaskConfig(
            task_id="test_delayed_args",
            task_type=TaskType.DELAYED,
            trigger_seconds=1,
            func=lambda x, y: result.append(x + y),
            func_args={"x": 1, "y": 2},
        )
        s.schedule(config)
        s.start()
        time.sleep(2)
        s.shutdown(wait=False)
        assert result == [3]


class TestIntervalTask:
    def test_interval_fires_multiple(self):
        s = _make_scheduler()
        result = []
        config = TaskConfig(
            task_id="test_interval",
            task_type=TaskType.INTERVAL,
            interval_seconds=1,
            func=lambda: result.append(1),
        )
        s.schedule(config)
        s.start()
        time.sleep(3.5)
        s.shutdown(wait=False)
        assert len(result) >= 3

    def test_interval_cancel(self):
        s = _make_scheduler()
        result = []
        config = TaskConfig(
            task_id="test_cancel",
            task_type=TaskType.INTERVAL,
            interval_seconds=1,
            func=lambda: result.append(1),
        )
        s.schedule(config)
        s.start()
        time.sleep(1.5)
        assert s.cancel("test_cancel") is True
        count_after_cancel = len(result)
        time.sleep(2)
        s.shutdown(wait=False)
        assert len(result) == count_after_cancel


class TestCronTask:
    def test_cron_registers(self):
        s = _make_scheduler()
        config = TaskConfig(
            task_id="test_cron",
            task_type=TaskType.CRON,
            cron_hour=4,
            cron_minute=0,
            func=lambda: None,
        )
        s.schedule(config)
        s.start()
        tasks = s.list_tasks()
        assert any(t["task_id"] == "test_cron" for t in tasks)
        s.shutdown(wait=False)


class TestCallbacks:
    def test_on_done_called(self):
        s = _make_scheduler()
        done = MagicMock()
        config = TaskConfig(
            task_id="test_done",
            task_type=TaskType.DELAYED,
            trigger_seconds=1,
            func=lambda: 42,
            on_done=done,
        )
        s.schedule(config)
        s.start()
        time.sleep(2)
        s.shutdown(wait=False)
        done.assert_called_once_with(42)

    def test_on_error_called(self):
        s = _make_scheduler()
        err = MagicMock()

        def bad_func():
            raise ValueError("boom")

        config = TaskConfig(
            task_id="test_err",
            task_type=TaskType.DELAYED,
            trigger_seconds=1,
            func=bad_func,
            on_error=err,
        )
        s.schedule(config)
        s.start()
        time.sleep(2)
        s.shutdown(wait=False)
        err.assert_called_once()
        assert isinstance(err.call_args[0][0], ValueError)


class TestListTasks:
    def test_list_returns_registered(self):
        s = _make_scheduler()
        s.schedule(TaskConfig(
            task_id="t1",
            task_type=TaskType.INTERVAL,
            interval_seconds=60,
            func=lambda: None,
            description="task one",
        ))
        s.schedule(TaskConfig(
            task_id="t2",
            task_type=TaskType.CRON,
            cron_hour=4,
            func=lambda: None,
            description="task two",
        ))
        s.start()
        tasks = s.list_tasks()
        ids = [t["task_id"] for t in tasks]
        assert "t1" in ids
        assert "t2" in ids
        s.shutdown(wait=False)

    def test_replace_existing(self):
        s = _make_scheduler()
        s.schedule(TaskConfig(
            task_id="dup",
            task_type=TaskType.INTERVAL,
            interval_seconds=60,
            func=lambda: "v1",
        ))
        s.schedule(TaskConfig(
            task_id="dup",
            task_type=TaskType.INTERVAL,
            interval_seconds=120,
            func=lambda: "v2",
        ))
        s.start()
        tasks = s.list_tasks()
        dup_tasks = [t for t in tasks if t["task_id"] == "dup"]
        assert len(dup_tasks) == 1
        s.shutdown(wait=False)


class TestPromptTask:
    """测试 prompt 模式的定时任务。"""

    def test_prompt_task_injected_to_session(self):
        s = _make_scheduler()
        mock_session = MagicMock()
        mock_session.handle_input.return_value = "done"
        s.set_session(mock_session)

        config = TaskConfig(
            task_id="test_prompt",
            task_type=TaskType.DELAYED,
            trigger_seconds=1,
            prompt="检查训练状态并发飞书报告",
        )
        s.schedule(config)
        s.start()
        time.sleep(2)
        s.shutdown(wait=False)
        mock_session.handle_input.assert_called_once_with("检查训练状态并发飞书报告")

    def test_prompt_task_without_session_fails_gracefully(self):
        s = _make_scheduler()
        # 不设置 session
        config = TaskConfig(
            task_id="test_no_session",
            task_type=TaskType.DELAYED,
            trigger_seconds=1,
            prompt="应该失败",
        )
        s.schedule(config)
        s.start()
        time.sleep(2)
        s.shutdown(wait=False)
        # 不崩溃就算通过

    def test_func_and_prompt_exclusive(self):
        """func 和 prompt 不能都不填。"""
        import pytest
        with pytest.raises(ValueError, match="必须指定 func 或 prompt"):
            TaskConfig(
                task_id="bad",
                task_type=TaskType.INTERVAL,
                interval_seconds=60,
            )


class TestPromptTaskDefaultNotify:
    """测试 prompt 模式 on_done 未设置时的默认外发（解决监控任务静默不报的 bug）。"""

    def test_prompt_no_on_done_nonempty_reply_triggers_notify(self):
        """prompt + 无 on_done + reply 非空 → 默认外发被触发。"""
        from src.core.session_commands import HandleResult

        s = _make_scheduler()
        mock_session = MagicMock()
        mock_session.handle_input.return_value = HandleResult(reply="训练已到第 100 步")
        s.set_session(mock_session)

        with patch("src.core.task_scheduler.scheduler._default_notify") as mock_notify:
            config = TaskConfig(
                task_id="test_auto_notify",
                task_type=TaskType.DELAYED,
                trigger_seconds=1,
                prompt="检查训练进度",
            )
            s.schedule(config)
            s.start()
            time.sleep(2)
            s.shutdown(wait=False)
            mock_notify.assert_called_once()
            args = mock_notify.call_args[0]
            assert args[0].task_id == "test_auto_notify"
            assert args[1].reply == "训练已到第 100 步"

    def test_prompt_no_on_done_empty_reply_skips_notify(self):
        """prompt + 无 on_done + reply 为空 → 默认外发不触发。"""
        from src.core.session_commands import HandleResult

        s = _make_scheduler()
        mock_session = MagicMock()
        mock_session.handle_input.return_value = HandleResult(reply="")
        s.set_session(mock_session)

        with patch("src.core.task_scheduler.scheduler._default_notify") as mock_notify:
            config = TaskConfig(
                task_id="test_no_notify_empty",
                task_type=TaskType.DELAYED,
                trigger_seconds=1,
                prompt="安静任务",
            )
            s.schedule(config)
            s.start()
            time.sleep(2)
            s.shutdown(wait=False)
            mock_notify.assert_not_called()

    def test_prompt_with_on_done_skips_default_notify(self):
        """prompt + 有 on_done → 默认外发不触发（on_done 优先级最高）。"""
        from src.core.session_commands import HandleResult

        s = _make_scheduler()
        mock_session = MagicMock()
        mock_session.handle_input.return_value = HandleResult(reply="hello")
        s.set_session(mock_session)
        on_done = MagicMock()

        with patch("src.core.task_scheduler.scheduler._default_notify") as mock_notify:
            config = TaskConfig(
                task_id="test_on_done_priority",
                task_type=TaskType.DELAYED,
                trigger_seconds=1,
                prompt="测试 on_done 优先",
                on_done=on_done,
            )
            s.schedule(config)
            s.start()
            time.sleep(2)
            s.shutdown(wait=False)
            on_done.assert_called_once()
            mock_notify.assert_not_called()


class TestDefaultNotifyDirect:
    """直接测试 _default_notify 函数行为（不通过 scheduler 触发路径）。"""

    def _make_config(self, task_id: str = "my_task") -> TaskConfig:
        return TaskConfig(
            task_id=task_id,
            task_type=TaskType.DELAYED,
            trigger_seconds=10,
            prompt="test",
        )

    def test_default_notify_calls_notify_user_with_formatted_text(self):
        """reply 非空时调用 daemon_boot._notify_user，文本格式：[定时任务 X]\\n\\n<reply>。"""
        from src.core.session_commands import HandleResult

        config = self._make_config("my_task")
        result = HandleResult(reply="训练已到第 100 步")

        with patch("src.daemon_boot._notify_user") as mock_notify, \
             patch("src.core.config.load_config") as mock_load_config:
            mock_load_config.return_value = {"feishu": {"app_id": "x", "app_secret": "y"}}
            _default_notify(config, result)

            mock_notify.assert_called_once()
            text = mock_notify.call_args[0][0]
            cfg = mock_notify.call_args.kwargs.get("config")
            assert text == "[定时任务 my_task]\n\n训练已到第 100 步", f"text 不对: {text!r}"
            assert cfg == {"feishu": {"app_id": "x", "app_secret": "y"}}, f"config 不对: {cfg!r}"

    def test_default_notify_skips_when_reply_empty(self):
        """reply 为空时（HandleResult 或 str 都没 reply 属性）不调用 _notify_user。"""
        from src.core.session_commands import HandleResult

        config = self._make_config("silent_task")

        with patch("src.daemon_boot._notify_user") as mock_notify:
            # HandleResult(reply="")
            _default_notify(config, HandleResult(reply=""))
            # 普通字符串（没有 reply 属性）
            _default_notify(config, "plain string")
            # None
            _default_notify(config, None)
            mock_notify.assert_not_called()

    def test_default_notify_swallows_notify_user_exceptions(self):
        """_notify_user 抛异常时 _default_notify 只 warn 不崩溃（防御性兜底）。"""
        from src.core.session_commands import HandleResult

        config = self._make_config("will_fail")
        result = HandleResult(reply="hello")

        # _notify_user 抛任意异常都不应传播
        with patch("src.daemon_boot._notify_user", side_effect=RuntimeError("network down")):
            # 不抛异常就算通过
            _default_notify(config, result)

    def test_default_notify_uses_real_load_config_when_not_patched(self):
        """不 patch load_config 时，_default_notify 真的调 load_config()（链路完整性）。"""
        from src.core.session_commands import HandleResult

        config = self._make_config("real_config_test")
        result = HandleResult(reply="check")

        with patch("src.daemon_boot._notify_user") as mock_notify:
            # load_config 真的跑（无 patch）—— 验证它存在且返回 dict
            _default_notify(config, result)
            mock_notify.assert_called_once()
            # 第二个位置/关键字参数应该是 config dict
            cfg = mock_notify.call_args.kwargs.get("config")
            assert isinstance(cfg, dict), f"config 应该是 dict，实际: {type(cfg)}"


class TestFullChainIntegration:
    """完整链路集成测试：真实 scheduler + 真实 _default_notify + mock daemon_boot._notify_user。"""

    def test_scheduler_prompt_triggers_default_notify_to_daemon_boot(self):
        """完整链路：prompt 任务跑完 → _default_notify 真跑 → daemon_boot._notify_user 真收到。"""
        from src.core.session_commands import HandleResult

        s = _make_scheduler()
        mock_session = MagicMock()
        mock_session.handle_input.return_value = HandleResult(reply="训练进度正常")
        s.set_session(mock_session)

        # 不 patch _default_notify —— 让它真的跑
        # 只 patch 最末端的 daemon_boot._notify_user 防止真发飞书
        with patch("src.daemon_boot._notify_user") as mock_notify:
            config = TaskConfig(
                task_id="integration_test",
                task_type=TaskType.DELAYED,
                trigger_seconds=1,
                prompt="监控训练进度",
            )
            s.schedule(config)
            s.start()
            time.sleep(2.5)
            s.shutdown(wait=False)

            mock_notify.assert_called_once()
            text = mock_notify.call_args[0][0]
            assert "integration_test" in text
            assert "训练进度正常" in text
            assert text.startswith("[定时任务 ")
