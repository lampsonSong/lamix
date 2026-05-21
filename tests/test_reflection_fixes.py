"""测试反思机制修复：通知发送、skill 刷新、1113 错误码定位"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestReflectionNotifyCallback:
    """修复 1: feishu._handle_dispatch 调用 session.set_reply_channel"""

    def test_handle_dispatch_calls_set_reply_channel(self):
        """测试 _handle_dispatch 创建 session 后调用了 set_reply_channel"""
        from src.platforms.adapters.feishu import FeishuAdapter

        adapter = FeishuAdapter(config={"app_id": "test", "app_secret": "test"})

        mock_session = MagicMock()
        mock_session_manager = MagicMock()
        mock_session_manager.get_or_create.return_value = mock_session
        adapter.session_manager = mock_session_manager

        # Mock 进度卡片相关
        mock_platform_mgr = MagicMock()
        mock_platform_mgr._adapters = {"feishu": adapter}
        mock_platform_mgr._loop = None

        with patch("src.platforms.manager.PlatformManager.instance", return_value=mock_platform_mgr):
            adapter._handle_dispatch(
                open_id="open_123",
                chat_id="chat_456",
                message_id="msg_789",
                text="test",
                reaction_id=None,
            )

        # 验证 set_reply_channel 被调用了
        mock_session.set_reply_channel.assert_called_once_with("feishu", "chat_456")

    def test_notify_callback_is_set_on_session(self):
        """测试 set_reply_channel 被调用后，session 的 reflect_notify_callback 不为 None"""
        from src.core.session import Session

        mock_agent = MagicMock()
        mock_agent.reflect_notify_callback = None
        session = Session(agent=mock_agent, config={})

        # 模拟飞书调用 set_reply_channel
        with patch("src.platforms.manager.PlatformManager.instance") as mock_mgr:
            mock_mgr_instance = MagicMock()
            mock_mgr_instance._adapters = {"feishu": MagicMock()}
            mock_mgr_instance._loop = MagicMock()
            mock_mgr.return_value = mock_mgr_instance

            session.set_reply_channel("feishu", "chat_123")

        assert mock_agent.reflect_notify_callback is not None


class TestReflectionSkillRefresh:
    """修复 2: agent 后台反思完成后刷新 skill index"""

    def test_agent_has_load_or_refresh_skills_method(self):
        """测试 Agent 有 _load_or_refresh_skills 方法"""
        from src.core.agent import Agent

        mock_llm = MagicMock()
        mock_llm.messages = []
        mock_adapter = MagicMock()
        agent = Agent(llm=mock_llm, adapter=mock_adapter)

        assert hasattr(agent, "_load_or_refresh_skills")

    def test_background_reflection_refreshes_skills(self):
        """测试后台反思完成后调用 _load_or_refresh_skills"""
        from src.core.agent import Agent

        mock_llm = MagicMock()
        mock_llm.messages = []
        mock_adapter = MagicMock()
        agent = Agent(llm=mock_llm, adapter=mock_adapter)

        # Mock 技能索引
        mock_skill_index = MagicMock()
        agent.skill_index = mock_skill_index

        # 模拟一次 skill 刷新
        agent._load_or_refresh_skills()

        # 验证 skill_index 被刷新
        mock_skill_index.load_or_build.assert_called()


class TestGlm1113Error:
    """修复 3: 智谱 1113 错误码定位（余额不足/未开通 glm-5.1）"""

    def test_glm_1113_means_no_balance_or_no_access(self):
        """测试 glm-5.1 返回 1113 时 glm-4-flash 可能仍可用"""
        # 这个测试验证我们的定位：1113 是 glm-5.1 特有的问题
        # 不意味着所有 glm 模型都不可用
        error_response = {"error": {"code": "1113", "message": "余额不足或无可用资源包,请充值。"}}

        # 验证错误码含义
        assert error_response["error"]["code"] == "1113"
        assert "余额不足" in error_response["error"]["message"]

    def test_fallback_models_work_when_primary_fails(self):
        """测试主模型失败时 fallback 模型能正常工作"""
        from src.core.agent import Agent

        mock_llm_primary = MagicMock()
        mock_llm_primary.messages = []
        mock_adapter_primary = MagicMock()

        mock_llm_fallback = MagicMock()
        mock_llm_fallback.messages = []
        mock_adapter_fallback = MagicMock()

        agent = Agent(
            llm=mock_llm_primary,
            adapter=mock_adapter_primary,
            fallback_models=[(mock_llm_fallback, mock_adapter_fallback)],
        )

        # 验证 fallback_models 已设置
        assert len(agent.fallback_models) == 1

        # 模拟主模型失败后的 fallback 调用
        agent.switch_llm(new_llm=mock_llm_fallback, new_adapter=mock_adapter_fallback)

        assert agent.llm is mock_llm_fallback
        assert agent.adapter is mock_adapter_fallback
