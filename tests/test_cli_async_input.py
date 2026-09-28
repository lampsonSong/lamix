"""AsyncInputRunner 测试：worker 线程执行、打断、标志传递、异常兜底。"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.cli_ui.async_runner import AsyncInputRunner


def _fake_session(handle_impl=None):
    """构造最小 session：handle_input / agent.request_interrupt / confirm_and_execute。"""
    if handle_impl is None:
        def handle_impl(user_input):
            return SimpleNamespace(
                reply=f"echo:{user_input}", is_command=False,
                is_exit=False, is_new=False, compaction_msg="",
            )
    agent = SimpleNamespace(
        request_interrupt=Mock(),
        confirm_execute_result=None,
    )
    agent.confirm_and_execute = Mock(return_value="计划执行完成")
    session = SimpleNamespace(
        handle_input=Mock(side_effect=handle_impl),
        agent=agent,
    )
    return session


class TestSubmit:
    def test_runs_in_worker_thread(self):
        """handle_input 必须在 worker 线程执行，不在主线程。"""
        seen_threads = []
        started = threading.Event()

        def handle(user_input):
            seen_threads.append(threading.current_thread().name)
            started.set()
            return SimpleNamespace(reply="r", is_command=False,
                                   is_exit=False, is_new=False, compaction_msg="")

        session = _fake_session(handle)
        runner = AsyncInputRunner()
        assert runner.submit(session, "hello", None) is True
        assert started.wait(timeout=5)
        runner.wait_done(timeout=5)
        assert "cli-input-worker" in seen_threads

    def test_last_result_and_render_callback(self):
        session = _fake_session()
        rendered = []
        runner = AsyncInputRunner()
        runner.submit(session, "hi", rendered.append)
        runner.wait_done(timeout=5)
        assert runner.last_result is not None
        assert runner.last_result.reply == "echo:hi"
        assert rendered and rendered[0].reply == "echo:hi"

    def test_submit_while_busy_returns_false(self):
        block = threading.Event()

        def handle(user_input):
            block.wait(timeout=10)
            return SimpleNamespace(reply="slow", is_command=False,
                                   is_exit=False, is_new=False, compaction_msg="")

        session = _fake_session(handle)
        runner = AsyncInputRunner()
        assert runner.submit(session, "first", None) is True
        # 忙期间再提交 → False
        assert runner.busy() is True
        assert runner.submit(session, "second", None) is False
        block.set()
        runner.wait_done(timeout=5)
        session.handle_input.assert_called_once_with("first")

    def test_worker_exception_captured_not_crash(self):
        def handle(user_input):
            raise RuntimeError("boom")

        session = _fake_session(handle)
        runner = AsyncInputRunner()
        runner.submit(session, "x", None)
        assert runner.wait_done(timeout=5) is True
        assert isinstance(runner.last_error, RuntimeError)
        # 线程正常结束，runner 恢复可用
        assert runner.busy() is False


class TestInterrupt:
    def test_request_interrupt_calls_agent_when_busy(self):
        block = threading.Event()

        def handle(user_input):
            block.wait(timeout=10)
            return SimpleNamespace(reply="", is_command=False,
                                   is_exit=False, is_new=False, compaction_msg="")

        session = _fake_session(handle)
        runner = AsyncInputRunner()
        runner.submit(session, "task", None)
        assert runner.busy() is True
        runner.request_interrupt(session)
        assert session.agent.request_interrupt.called
        block.set()
        runner.wait_done(timeout=5)

    def test_request_interrupt_noop_when_idle(self):
        session = _fake_session()
        runner = AsyncInputRunner()
        runner.request_interrupt(session)  # 不应抛错
        assert not session.agent.request_interrupt.called

    def test_wait_done_true_after_finish(self):
        session = _fake_session()
        runner = AsyncInputRunner()
        runner.submit(session, "x", None)
        deadline = time.time() + 5
        while runner.busy() and time.time() < deadline:
            time.sleep(0.01)
        assert runner.wait_done(timeout=1) is True


class TestFlags:
    def _result(self, **kw):
        base = dict(reply="", is_command=False, is_exit=False,
                    is_new=False, compaction_msg="")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_exit_flag(self):
        session = _fake_session(lambda u: self._result(is_exit=True))
        runner = AsyncInputRunner()
        runner.submit(session, "/exit", None)
        runner.wait_done(timeout=5)
        assert runner.exit_requested is True

    def test_reset_flag(self):
        session = _fake_session(lambda u: self._result(is_new=True))
        runner = AsyncInputRunner()
        runner.submit(session, "/new", None)
        runner.wait_done(timeout=5)
        assert runner.needs_reset is True

    def test_pending_confirm_flag(self):
        session = _fake_session(lambda u: self._result(reply="请确认是否执行此计划"))
        runner = AsyncInputRunner()
        runner.submit(session, "plan", None)
        runner.wait_done(timeout=5)
        assert runner.pending_confirm is True

    def test_normal_reply_no_flags(self):
        session = _fake_session()
        runner = AsyncInputRunner()
        runner.submit(session, "hi", None)
        runner.wait_done(timeout=5)
        assert runner.exit_requested is False
        assert runner.needs_reset is False
        assert runner.pending_confirm is False


class TestConfirm:
    def test_submit_confirm_renders_exec_result(self):
        session = _fake_session()
        rendered = []
        runner = AsyncInputRunner()
        after_called = []
        assert runner.submit_confirm(
            session, rendered.append, after=lambda: after_called.append(1)
        ) is True
        runner.wait_done(timeout=5)
        assert session.agent.confirm_and_execute.called
        assert rendered and rendered[0].reply == "计划执行完成"
        assert after_called == [1]

    def test_submit_confirm_while_busy_rejected(self):
        block = threading.Event()

        def handle(user_input):
            block.wait(timeout=10)
            return SimpleNamespace(reply="", is_command=False,
                                   is_exit=False, is_new=False, compaction_msg="")

        session = _fake_session(handle)
        runner = AsyncInputRunner()
        runner.submit(session, "t", None)
        assert runner.submit_confirm(session, None) is False
        block.set()
        runner.wait_done(timeout=5)
