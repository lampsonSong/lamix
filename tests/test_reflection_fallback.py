"""反思机制 fallback 降级测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import json

import pytest

from src.core import reflection


# ── 辅助 ─────────────────────────────────────────────────────────────────────

def _mock_adapter(responses: list, raise_error: Exception | None = None):
    """创建模拟 adapter，按顺序返回 responses 中的值。"""
    mock = MagicMock()
    if raise_error:
        mock.chat.side_effect = raise_error
        return mock
    
    def chat_side_effect(*args, **kwargs):
        idx = chat_side_effect._idx
        chat_side_effect._idx += 1
        if idx < len(responses):
            content, tool_calls = responses[idx]
            msg = MagicMock()
            msg.content = content
            msg.model_dump.return_value = {
                "content": content,
                "tool_calls": tool_calls or []
            }
            resp = MagicMock()
            resp.choices = [MagicMock(message=msg, finish_reason="stop")]
            return resp
        # 默认返回空响应
        msg = MagicMock()
        msg.content = ""
        msg.model_dump.return_value = {"content": "", "tool_calls": []}
        resp = MagicMock()
        resp.choices = [MagicMock(message=msg, finish_reason="stop")]
        return resp
    chat_side_effect._idx = 0
    mock.chat.side_effect = chat_side_effect
    return mock


def _tool_calls(calls: list[dict]) -> list:
    """生成 tool_calls 列表。"""
    return [
        {
            "id": f"call_{i}",
            "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))},
            "type": "function"
        }
        for i, c in enumerate(calls)
    ]


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state():
    """每个测试前清空全局状态。"""
    reflection._llm_client = None
    reflection._skill_index = None
    reflection._fallback_llms = []
    yield
    reflection._llm_client = None
    reflection._skill_index = None
    reflection._fallback_llms = []


# ── run_reflection_loop 主模型成功 ──────────────────────────────────────────

def test_run_reflection_loop_primary_succeeds_no_tools():
    """主模型成功，无 tool_calls，返回空列表。"""
    adapter = _mock_adapter([("无需沉淀", [])])

    with patch.object(reflection, '_get_skill_full_content', return_value=None):
        result = reflection.run_reflection_loop(
            goal="测试目标",
            execution_summary="测试执行",
            llm_client=MagicMock(model="primary"),
            adapter=adapter,
        )

    assert result == []


def test_run_reflection_loop_primary_succeeds_with_tools():
    """主模型成功，返回 tool_calls，执行并返回沉淀结果。"""
    tc = _tool_calls([
        {"name": "skill_create", "args": {"name": "test-skill", "content": "测试", "reason": "测试"}}
    ])
    adapter = _mock_adapter([("", tc), ("无需沉淀", [])])

    # Mock _TOOL_RUNNERS 直接返回期望的结果
    mock_runners = {
        "skill_create": lambda args: "已创建 skill: test-skill",
        "skill_update": lambda args: "",
        "project_create": lambda args: "",
        "project_update": lambda args: "",
        "info_create": lambda args: "",
        "info_update": lambda args: "",
    }

    with patch.object(reflection, '_get_skill_full_content', return_value=None):
        with patch.object(reflection, '_TOOL_RUNNERS', mock_runners):
            result = reflection.run_reflection_loop(
                goal="测试目标",
                execution_summary="测试执行",
                llm_client=MagicMock(model="primary"),
                adapter=adapter,
            )

    assert len(result) == 1
    assert "test-skill" in result[0]


# ── run_reflection_loop 主模型失败，fallback 成功 ────────────────────────────

def test_run_reflection_loop_primary_fails_fallback_succeeds():
    """主模型失败，fallback 成功。"""
    primary_adapter = _mock_adapter(None, raise_error=Exception("主模型失败"))
    fallback_adapter = _mock_adapter([("无需沉淀", [])])

    result = reflection.run_reflection_loop(
        goal="测试目标",
        execution_summary="测试执行",
        llm_client=MagicMock(model="primary"),
        adapter=primary_adapter,
        fallback_models=[(MagicMock(model="fallback"), fallback_adapter)],
    )

    assert result == []


def test_run_reflection_loop_primary_fails_fallback_with_tools():
    """主模型失败，fallback 成功并返回 tool_calls。"""
    tc = _tool_calls([
        {"name": "info_create", "args": {"name": "test-info", "content": "测试", "reason": "测试"}}
    ])
    
    primary_adapter = _mock_adapter(None, raise_error=Exception("主模型失败"))
    fallback_adapter = _mock_adapter([("", tc), ("无需沉淀", [])])

    mock_runners = {
        "skill_create": lambda args: "",
        "skill_update": lambda args: "",
        "project_create": lambda args: "",
        "project_update": lambda args: "",
        "info_create": lambda args: "已创建 info: test-info",
        "info_update": lambda args: "",
    }

    with patch.object(reflection, '_get_skill_full_content', return_value=None):
        with patch.object(reflection, '_TOOL_RUNNERS', mock_runners):
            result = reflection.run_reflection_loop(
                goal="测试目标",
                execution_summary="测试执行",
                llm_client=MagicMock(model="primary"),
                adapter=primary_adapter,
                fallback_models=[(MagicMock(model="fallback"), fallback_adapter)],
            )

    assert len(result) == 1
    assert "test-info" in result[0]


# ── run_reflection_loop 多级 fallback ───────────────────────────────────────

def test_run_reflection_loop_multiple_fallbacks():
    """主模型失败，fallback1 失败，fallback2 成功。"""
    primary_adapter = _mock_adapter(None, raise_error=Exception("主模型失败"))
    fb1_adapter = _mock_adapter(None, raise_error=Exception("fallback1 也失败"))
    fb2_adapter = _mock_adapter([("无需沉淀", [])])

    result = reflection.run_reflection_loop(
        goal="测试目标",
        execution_summary="测试执行",
        llm_client=MagicMock(model="primary"),
        adapter=primary_adapter,
        fallback_models=[
            (MagicMock(model="fb1"), fb1_adapter),
            (MagicMock(model="fb2"), fb2_adapter)
        ],
    )

    assert result == []


# ── run_reflection_loop 所有模型都失败 ──────────────────────────────────────

def test_run_reflection_loop_all_fail():
    """主模型和所有 fallback 都失败，通知用户。"""
    primary_adapter = _mock_adapter(None, raise_error=Exception("主模型失败"))
    fallback_adapter = _mock_adapter(None, raise_error=Exception("fallback 也失败"))

    notified = []
    def mock_notify(msg):
        notified.append(msg)

    with patch.object(reflection, '_notify_user', mock_notify):
        result = reflection.run_reflection_loop(
            goal="测试目标",
            execution_summary="测试执行",
            llm_client=MagicMock(model="primary"),
            adapter=primary_adapter,
            fallback_models=[(MagicMock(model="fallback"), fallback_adapter)],
        )

    assert result == []
    assert len(notified) == 1
    assert "反思失败" in notified[0]


# ── run_reflection_loop 无 fallback 配置 ─────────────────────────────────────

def test_run_reflection_loop_no_fallback():
    """没有配置 fallback，主模型失败后直接失败。"""
    primary_adapter = _mock_adapter(None, raise_error=Exception("主模型失败"))

    notified = []
    def mock_notify(msg):
        notified.append(msg)

    with patch.object(reflection, '_notify_user', mock_notify):
        result = reflection.run_reflection_loop(
            goal="测试目标",
            execution_summary="测试执行",
            llm_client=MagicMock(model="primary"),
            adapter=primary_adapter,
            fallback_models=None,
        )

    assert result == []
    assert len(notified) == 1
    assert "反思失败" in notified[0]


# ── set_fallback_llms 与 set_llm_client ─────────────────────────────────────

def test_set_fallback_llms_setters():
    """验证 setter 函数正常工作。"""
    mock_client = MagicMock()
    mock_fb1 = MagicMock()
    mock_fb2 = MagicMock()

    reflection.set_llm_client(mock_client)
    reflection.set_fallback_llms([mock_fb1, mock_fb2])

    assert reflection._llm_client is mock_client
    assert reflection._fallback_llms == [mock_fb1, mock_fb2]


def test_set_fallback_llms_clears():
    """set_fallback_llms(None) 清空 fallback 列表。"""
    mock_fb = MagicMock()
    reflection.set_fallback_llms([mock_fb])
    assert reflection._fallback_llms == [mock_fb]

    reflection.set_fallback_llms(None)
    assert reflection._fallback_llms == []


# ── should_reflect 测试 ──────────────────────────────────────────────────────

def test_should_reflect_cooldown():
    """冷却时间内不触发反思。"""
    import time
    reflection._last_reflect_time = time.time()
    assert reflection.should_reflect(tool_call_count=5) is False


def test_should_reflect_tool_call_count():
    """tool_call_count >= 3 触发反思。"""
    import time
    reflection._last_reflect_time = 0
    assert reflection.should_reflect(tool_call_count=0) is False
    assert reflection.should_reflect(tool_call_count=1) is False
    assert reflection.should_reflect(tool_call_count=2) is False
    assert reflection.should_reflect(tool_call_count=3) is True
    assert reflection.should_reflect(tool_call_count=5) is True


def test_mark_reflection_done():
    """mark_reflection_done 重置冷却。"""
    import time
    reflection._last_reflect_time = time.time()
    reflection.mark_reflection_done()
    assert time.time() - reflection._last_reflect_time < 1
