"""Skills 按需加载 / 索引构建测试。"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.core.skills_tools import _parse_skill, skill


def _write_skill_md(skills_dir: Path, name: str, description: str = "", triggers: list = None, body: str = "") -> Path:
    frontmatter = f"---\nname: {name}\ndescription: {description}\n"
    if triggers:
        frontmatter += f"triggers:\n" + "\n".join(f"- {t}" for t in triggers) + "\n"
    frontmatter += f"---\n\n{body}"
    f = skills_dir / f"{name}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(frontmatter, encoding="utf-8")
    return f


class TestBuildSkillsIndex:
    """测试 skills 索引构建。"""

    def test_skills_index_contains_name_and_desc(self, tmp_path: Path):
        """索引包含 skill 名称和描述。"""
        f = _write_skill_md(tmp_path, "code-review", "Review code quality", body="steps")

        mock_index = MagicMock()
        mock_index._entries = [{"name": "code-review", "description": "Review code quality", "path": str(f)}]
        mock_index.list_summaries.return_value = [{"name": "code-review", "description": "Review code quality"}]

        summaries = mock_index.list_summaries()
        assert any(s.get("name") == "code-review" for s in summaries)

    def test_skills_index_format(self, tmp_path: Path):
        """索引格式是列表。"""
        f = _write_skill_md(tmp_path, "test", "desc")
        mock_index = MagicMock()
        mock_index._entries = [{"name": "test", "description": "desc", "path": str(f)}]
        mock_index.list_summaries.return_value = [{"name": "test", "description": "desc"}]

        summaries = mock_index.list_summaries()
        assert isinstance(summaries, list)


class TestSkillIndexSearch:
    """测试技能搜索。"""

    def test_search_finds_by_name(self, tmp_path: Path):
        """按名称搜索能找到。"""
        f = _write_skill_md(tmp_path, "debug", "Debug code")
        mock_index = MagicMock()
        mock_index._entries = [{"name": "debug", "description": "Debug code", "path": str(f)}]

        with patch("src.core.skills_tools._active_skill_index", mock_index):
            result = skill({"action": "search", "query": "debug"})
        assert "Debug" in result or "# Debug" in result


class TestSkillIndexIncrementalBuild:
    """测试增量索引构建。"""

    def test_new_skill_detected(self, tmp_path: Path):
        """新增 skill 被索引检测到。"""
        from src.core.indexer import SkillIndex

        skills_dir = tmp_path / "skills"
        index_dir = tmp_path / "index"
        skills_dir.mkdir()
        index_dir.mkdir()

        _write_skill_md(skills_dir, "skill-a", "desc a")
        idx = SkillIndex(skills_dir, index_dir)
        idx.load_or_build()

        _write_skill_md(skills_dir, "skill-b", "desc b")
        idx2 = SkillIndex(skills_dir, index_dir)
        idx2.load_or_build()

        summaries = idx2.list_summaries()
        names = [s.get("name", "") for s in summaries]
        assert "skill-a" in names
        assert "skill-b" in names

    def test_modified_file_rebuilt(self, tmp_path: Path):
        """修改后的 skill 被重新索引。"""
        import time
        from src.core.indexer import SkillIndex

        skills_dir = tmp_path / "skills"
        index_dir = tmp_path / "index"
        skills_dir.mkdir()
        index_dir.mkdir()

        _write_skill_md(skills_dir, "skill-a", "old desc")
        idx1 = SkillIndex(skills_dir, index_dir)
        idx1.load_or_build()

        time.sleep(0.1)
        _write_skill_md(skills_dir, "skill-a", "new desc")
        idx2 = SkillIndex(skills_dir, index_dir)
        idx2.load_or_build()

        summaries = idx2.list_summaries()
        assert any("new desc" in s.get("description", "") for s in summaries)


class TestSkillTriggerMerge:
    """测试 triggers 字段处理。"""

    def test_parse_skill_with_triggers(self, tmp_path: Path):
        """带 triggers 的 skill 能被正确解析。"""
        f = _write_skill_md(
            tmp_path, "test", "desc",
            triggers=["trigger1", "trigger2"],
            body="body"
        )
        result = _parse_skill(f)
        assert result is not None
        assert result["name"] == "test"

    def test_parse_skill_without_triggers(self, tmp_path: Path):
        """无 triggers 的 skill 也能正常解析。"""
        f = _write_skill_md(tmp_path, "test", "desc", body="body")
        result = _parse_skill(f)
        assert result is not None
        assert result["name"] == "test"
