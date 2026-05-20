"""Skills 工具测试：_parse_skill, skill view/search, info。"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.core.skills_tools import _parse_skill, skill, info


def _write_skill_md(skills_dir: Path, name: str, description: str = "", body: str = "") -> Path:
    """创建一个双层 skill 目录 skills/<name>/SKILL.md。"""
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    skill_dir = skills_dir / name
    skill_dir.mkdir(exist_ok=True)
    f = skill_dir / "SKILL.md"
    f.write_text(content, encoding="utf-8")
    return f


class TestParseSkill:
    """测试 _parse_skill 解析。"""

    def test_parse_with_frontmatter(self, tmp_path: Path):
        """解析带 frontmatter 的 SKILL.md。"""
        f = _write_skill_md(tmp_path, "my-skill", "A skill", "body content")
        result = _parse_skill(f)
        assert result is not None
        assert result["name"] == "my-skill"
        assert result["description"] == "A skill"
        assert "body content" in result["body"]

    def test_parse_without_frontmatter(self, tmp_path: Path):
        """解析无 frontmatter 的文件，name 取文件名（path.stem）。"""
        f = tmp_path / "no-meta.md"
        f.write_text("Just some content", encoding="utf-8")
        result = _parse_skill(f)
        assert result is not None
        assert result["name"] == "no-meta"
        assert result["description"] == ""

    def test_parse_nonexistent_file(self, tmp_path: Path):
        """不存在的文件返回 None。"""
        result = _parse_skill(tmp_path / "nonexistent.md")
        assert result is None


class TestSkillTool:
    """测试 skill 工具入口。"""

    def test_skill_view(self, tmp_path: Path):
        """view 能加载技能全文。"""
        f = _write_skill_md(tmp_path, "test-skill", "desc", "body text")

        # mock _resolve_skill_path 直接返回测试文件路径
        with patch("src.core.skills_tools._resolve_skill_path", return_value=(f, False)):
            with patch("src.core.skills_tools._increment_invocation", return_value=1):
                result = skill({"action": "view", "name": "test-skill"})
        assert "test-skill" in result
        assert "body text" in result

    def test_skill_view_not_found(self):
        """view 不存在的技能报错。"""
        mock_index = MagicMock()
        mock_index._entries = []
        with patch("src.core.skills_tools._active_skill_index", mock_index):
            result = skill({"action": "view", "name": "nonexistent"})
        assert "[提示]" in result or "没有找到" in result or "错误" in result

    def test_skill_search(self, tmp_path: Path):
        """search 能搜索技能。"""
        f = _write_skill_md(tmp_path, "code-review", "Review code", "steps")

        mock_index = MagicMock()
        mock_index._entries = [{"name": "code-review", "description": "Review code", "path": str(f)}]
        # search 直接使用 _active_skill_index，不需要 mock _resolve_skill_path
        with patch("src.core.skills_tools._active_skill_index", mock_index):
            result = skill({"action": "search", "query": "review"})
        assert "Code Review" in result or "# Code Review" in result

    def test_skill_search_no_match(self):
        """search 无匹配。"""
        mock_index = MagicMock()
        mock_index._entries = []
        with patch("src.core.skills_tools._active_skill_index", mock_index):
            result = skill({"action": "search", "query": "nonexistent"})
        assert "[提示]" in result or "没有找到" in result

    def test_skill_invalid_action(self):
        """无效 action 报错。"""
        result = skill({"action": "invalid"})
        assert "错误" in result
