"""测试飞书卡片（interactive）消息解析 — _extract_card_text / _extract_text"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.platforms.adapters.feishu import (
    _extract_card_text,
    _extract_elements_text,
    FeishuAdapter,
)


# ────────────────────────────────────────────────────────────────────
# 辅助：快速构造 adapter 实例（只用于调 _extract_text，不启动 WS）
# ────────────────────────────────────────────────────────────────────
@pytest.fixture
def adapter():
    return FeishuAdapter({"app_id": "test", "app_secret": "test"})


# ────────────────────────────────────────────────────────────────────
# _extract_text 基本能力（回归）
# ────────────────────────────────────────────────────────────────────
class TestExtractTextBasic:
    def test_plain_text(self, adapter):
        raw = json.dumps({"text": "hello"})
        assert adapter._extract_text(raw) == "hello"

    def test_post_rich_text(self, adapter):
        raw = json.dumps({
            "content": [
                [{"tag": "text", "text": "第一行"}, {"tag": "text", "text": "内容"}],
                [{"tag": "text", "text": "第二行"}],
            ]
        })
        result = adapter._extract_text(raw)
        assert "第一行" in result
        assert "第二行" in result

    def test_non_json_passthrough(self, adapter):
        assert adapter._extract_text("just plain text") == "just plain text"

    def test_empty_string(self, adapter):
        assert adapter._extract_text("") == ""

    def test_none(self, adapter):
        assert adapter._extract_text(None) == ""


# ────────────────────────────────────────────────────────────────────
# schema 2.0 卡片
# ────────────────────────────────────────────────────────────────────
class TestSchema20Card:
    def test_simple_markdown_card(self, adapter):
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "通知标题"}},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "这是正文内容"},
                    {"tag": "markdown", "content": "第二段"},
                ]
            },
        }
        result = adapter._extract_text(json.dumps(card))
        assert "通知标题" in result
        assert "这是正文内容" in result
        assert "第二段" in result

    def test_column_set_nested(self, adapter):
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "报表"}},
            "body": {
                "elements": [
                    {
                        "tag": "column_set",
                        "columns": [
                            {
                                "tag": "column",
                                "elements": [
                                    {"tag": "markdown", "content": "左列内容"},
                                ],
                            },
                            {
                                "tag": "column",
                                "elements": [
                                    {"tag": "markdown", "content": "右列内容"},
                                ],
                            },
                        ],
                    }
                ]
            },
        }
        result = adapter._extract_text(json.dumps(card))
        assert "左列内容" in result
        assert "右列内容" in result
        assert "报表" in result

    def test_button_text(self, adapter):
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "操作"}},
            "body": {
                "elements": [
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "点击确认"},
                            }
                        ],
                    }
                ]
            },
        }
        result = adapter._extract_text(json.dumps(card))
        assert "点击确认" in result

    def test_hr_and_img_skipped(self, adapter):
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "T"}},
            "body": {
                "elements": [
                    {"tag": "hr"},
                    {"tag": "img", "img_key": "xxx"},
                    {"tag": "markdown", "content": "可见"},
                ]
            },
        }
        result = adapter._extract_text(json.dumps(card))
        assert "可见" in result
        assert "xxx" not in result

    def test_note_nested(self, adapter):
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "T"}},
            "body": {
                "elements": [
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": "备注文本"},
                        ],
                    }
                ]
            },
        }
        result = adapter._extract_text(json.dumps(card))
        assert "备注文本" in result


# ────────────────────────────────────────────────────────────────────
# 旧版卡片
# ────────────────────────────────────────────────────────────────────
class TestLegacyCard:
    def test_basic_elements(self, adapter):
        card = {
            "title": "旧版标题",
            "elements": [
                [
                    {"tag": "text", "text": "行一"},
                    {"tag": "a", "text": "链接文本", "href": "https://example.com"},
                    {"tag": "at", "user_name": "张三"},
                    {"tag": "img", "image_key": "xxx"},
                ],
            ],
        }
        result = adapter._extract_text(json.dumps(card))
        assert "旧版标题" in result
        assert "行一" in result
        assert "链接文本" in result
        assert "@张三" in result
        assert "xxx" not in result

    def test_flat_dict_elements(self, adapter):
        card = {
            "title": "平铺",
            "elements": [
                {"tag": "markdown", "content": "md内容"},
                {"tag": "text", "text": "txt内容"},
            ],
        }
        result = adapter._extract_text(json.dumps(card))
        assert "md内容" in result
        assert "txt内容" in result

    def test_title_as_dict(self, adapter):
        card = {
            "title": {"tag": "plain_text", "content": "字典标题"},
            "elements": [{"tag": "markdown", "content": "body"}],
        }
        result = adapter._extract_text(json.dumps(card))
        assert "字典标题" in result


# ────────────────────────────────────────────────────────────────────
# 降级占位卡片
# ────────────────────────────────────────────────────────────────────
class TestPlaceholderCard:
    def test_placeholder_only_returns_empty(self, adapter):
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "请升级至最新版本客户端，以查看内容"}},
            "body": {"elements": []},
        }
        result = adapter._extract_text(json.dumps(card))
        assert result == ""

    def test_placeholder_mixed(self, adapter):
        """占位文本被过滤，但其他有效文本保留。"""
        card = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "真实标题"}},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "请升级至最新版本客户端，以查看内容"},
                    {"tag": "markdown", "content": "有效内容"},
                ]
            },
        }
        result = adapter._extract_text(json.dumps(card))
        assert "有效内容" in result
        assert "请升级" not in result


# ────────────────────────────────────────────────────────────────────
# _extract_card_text 直接测试
# ────────────────────────────────────────────────────────────────────
class TestExtractCardTextDirect:
    def test_non_card_returns_none(self):
        assert _extract_card_text({"foo": "bar"}) is None

    def test_empty_card(self):
        result = _extract_card_text({
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": ""}},
            "body": {"elements": []},
        })
        assert result == ""


# ────────────────────────────────────────────────────────────────────
# _extract_elements_text 直接测试
# ────────────────────────────────────────────────────────────────────
class TestExtractElementsText:
    def test_non_list_returns_empty(self):
        assert _extract_elements_text("not a list") == []
        assert _extract_elements_text(None) == []

    def test_div_with_fields(self):
        elems = [
            {
                "tag": "div",
                "fields": [
                    {"tag": "markdown", "content": "字段一"},
                    {"tag": "markdown", "content": "字段二"},
                ],
            }
        ]
        parts = _extract_elements_text(elems)
        assert "字段一" in parts
        assert "字段二" in parts
