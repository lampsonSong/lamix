"""测试 reflect_notify_callback 在各种场景下的调用行为"""
import pytest
from unittest.mock import MagicMock, patch
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestReflectNotifyCallback:
    """测试 reflect_notify_callback 的调用行为（模拟 _run_reflection 逻辑）"""

    def _simulate_run_reflection(self, callback, hints, raise_error=None):
        """模拟 _run_reflection 的核心逻辑，用于测试 callback 行为"""
        def inner():
            _notify = callback
            try:
                if raise_error:
                    raise raise_error
                # 模拟 run_reflection_loop 返回 hints
                if hints:
                    if _notify:
                        _notify("📝 反思沉淀完成:\n  • " + "\n  • ".join(hints))
                elif _notify:
                    _notify("📝 反思完成，暂无新内容需要沉淀")
            except Exception as e:
                if _notify:
                    _notify(f"反思失败: {e}")
        
        t = threading.Thread(target=inner, daemon=True)
        t.start()
        t.join(timeout=2)

    def test_notify_called_when_hints_exist(self):
        """有沉淀结果时，notify 回调应该被调用并传入格式化消息"""
        callback_messages = []
        callback = lambda msg: callback_messages.append(msg)

        hints = ["更新了 skill: test-skill", "修复了 bug"]
        self._simulate_run_reflection(callback, hints)

        assert len(callback_messages) == 1
        assert "反思沉淀完成" in callback_messages[0]
        assert "test-skill" in callback_messages[0]

    def test_notify_called_when_no_hints(self):
        """无沉淀结果时，notify 回调也应该被调用，告知暂无新内容"""
        callback_messages = []
        callback = lambda msg: callback_messages.append(msg)

        self._simulate_run_reflection(callback, hints=[])

        assert len(callback_messages) == 1
        assert "暂无新内容需要沉淀" in callback_messages[0]

    def test_notify_called_on_exception(self):
        """反思过程抛出异常时，notify 回调应该被调用并传入错误信息"""
        callback_messages = []
        callback = lambda msg: callback_messages.append(msg)

        self._simulate_run_reflection(callback, hints=None, raise_error=Exception("test error"))

        assert len(callback_messages) == 1
        assert "反思失败" in callback_messages[0]
        assert "test error" in callback_messages[0]

    def test_no_crash_when_callback_is_none(self):
        """reflect_notify_callback 为 None 时不应崩溃"""
        # callback 为 None 时不应该抛出异常
        self._simulate_run_reflection(callback=None, hints=["hint"])
        # 能执行到这里说明没有崩溃

    def test_no_notify_when_callback_none_and_no_hints(self):
        """callback 为 None 且无 hints 时，不应尝试调用"""
        # 不应该抛出 AttributeError
        self._simulate_run_reflection(callback=None, hints=[])

    def test_no_notify_on_exception_when_callback_none(self):
        """callback 为 None 时发生异常，不应崩溃"""
        # 不应该抛出 AttributeError
        self._simulate_run_reflection(callback=None, hints=None, raise_error=Exception("error"))


class TestReflectNotifyCallbackIntegration:
    """集成测试：验证 Agent 正确设置 callback"""

    def test_agent_has_reflect_notify_callback_attr(self):
        """Agent 有 reflect_notify_callback 属性"""
        from src.core.agent import Agent

        mock_llm = MagicMock()
        mock_llm.messages = []
        mock_adapter = MagicMock()
        agent = Agent(llm=mock_llm, adapter=mock_adapter)

        assert hasattr(agent, "reflect_notify_callback")

    def test_set_reply_channel_sets_callback(self):
        """set_reply_channel 被调用后，agent.reflect_notify_callback 不为 None"""
        from src.core.session import Session

        mock_agent = MagicMock()
        mock_agent.reflect_notify_callback = None
        session = Session(agent=mock_agent, config={})

        with patch("src.platforms.manager.PlatformManager.instance") as mock_mgr:
            mock_mgr_instance = MagicMock()
            mock_mgr_instance._adapters = {"feishu": MagicMock()}
            mock_mgr_instance._loop = MagicMock()
            mock_mgr.return_value = mock_mgr_instance

            session.set_reply_channel("feishu", "chat_123")

        assert mock_agent.reflect_notify_callback is not None
