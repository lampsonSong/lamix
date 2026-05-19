"""反思与知识沉淀模块的单元测试。"""

import time
from unittest.mock import MagicMock, patch

import pytest

from src.core.reflection import (
    _content_already_exists,
    _create_project,
    _create_skill,
    _update_project,
    _update_skill,
    should_reflect,
)


class TestShouldReflect:
    """should_reflect 触发条件测试。"""

    def test_reflect_with_high_tool_count(self):
        """tool_call >= 3 触发。"""
        assert should_reflect(tool_call_count=3) is True

    def test_no_reflect_with_low_tool_count(self):
        """tool_call < 3 不触发。"""
        assert should_reflect(tool_call_count=2) is False


class TestContentAlreadyExists:
    """_content_already_exists 逻辑测试。"""

    def test_empty_existing(self):
        """空现有内容返回 False。"""
        assert _content_already_exists("", "新内容") is False

    def test_exact_match(self):
        """完全相同返回 True。"""
        assert _content_already_exists("相同内容", "相同内容") is True

    def test_similar_content(self):
        """高度相似返回 True。"""
        existing = "这是一个测试内容，包含一些重复的文字和描述"
        new = "这是一个测试内容，包含一些重复的文字和描述"
        assert _content_already_exists(existing, new) is True
