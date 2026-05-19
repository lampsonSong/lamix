"""Skills Manager 单元测试（隔离 ~/.lamix 依赖）。"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.skills.manager import (
    ConsolidationAction,
    _parse_skill_md,
    create_skill,
    execute_consolidation,
    get_skills_summary,
    list_skills,
    show_skill,
    load_all_skills,
    Skill,
)


# ─── Skill 实例化 Helper ──────────────────────────────────────────────────────

def _make_skill(tmp_path: Path, skill_file_name: str = None, **meta) -> tuple:
    """在 tmp_path 下创建子目录格式 skill（skill-name/SKILL.md），返回 (Skill实例, Path)。"""
    if skill_file_name is None:
        skill_file_name = meta.get("name", "default-skill")

    # fm_name 优先使用 meta 中的 name，否则用 skill_file_name
    fm_name = meta.get("name", skill_file_name)
    fm = {
        "name": fm_name,
        "description": meta.get("description", ""),
    }

    fm_parts = [f"name: {fm['name']}"]
    if fm['description']:
        fm_parts.append(f"description: {fm['description']}")

    body = meta.get("body", "# Body\nContent here")
    content = "---\n" + "\n".join(fm_parts) + "\n---\n" + body
    
    # 创建子目录格式
    skill_dir = tmp_path / skill_file_name
    skill_dir.mkdir(exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")

    skill = Skill(name=fm["name"], path=skill_file, meta=fm, body=body)
    return skill, skill_file


# ─── Skill 基础测试 ──────────────────────────────────────────────────────────

class TestSkillBasic:

    def test_skill_attributes(self, tmp_path: Path):
        skill, _ = _make_skill(
            tmp_path, "my-skill",
            description="My skill",
            body="# My Skill\n\nBody content",
        )
        assert skill.name == "my-skill"
        assert skill.description == "My skill"
        assert "Body content" in skill.body


class TestParseSkillMd:

    def test_parse_valid_frontmatter(self, tmp_path: Path):
        skill, _ = _make_skill(
            tmp_path, "valid-skill",
            name="test-skill",
            description="A test skill",
            body="# Body\nThis is the body.",
        )
        result = _parse_skill_md(skill.path)
        assert result is not None
        assert result.name == "test-skill"
        assert result.description == "A test skill"
        assert "This is the body." in result.body

    def test_parse_missing_frontmatter(self, tmp_path: Path):
        # 无 frontmatter 时，用文件名（不含 .md）作为 name
        skill_file = tmp_path / "no-frontmatter.md"
        skill_file.write_text("# Just a title\n\nSome content.", encoding="utf-8")

        result = _parse_skill_md(skill_file)
        assert result is not None
        assert result.name == "no-frontmatter"
        assert result.description == ""
        assert "Some content." in result.body

    def test_parse_invalid_yaml(self, tmp_path: Path):
        skill_file = tmp_path / "bad-yaml.md"
        skill_file.write_text(
            "---\nname: [invalid\n  yaml\n---\n# Body\n",
            encoding="utf-8",
        )

        result = _parse_skill_md(skill_file)
        assert result is not None
        assert result.name == "bad-yaml"

    def test_parse_nonexistent_file(self):
        result = _parse_skill_md(Path("/nonexistent/path/SKILL.md"))
        assert result is None

    def test_parse_empty_file(self, tmp_path: Path):
        skill_file = tmp_path / "empty.md"
        skill_file.write_text("", encoding="utf-8")

        result = _parse_skill_md(skill_file)
        assert result is not None
        assert result.name == "empty"


class TestGetSkillsSummary:

    def test_empty_skills(self):
        result = get_skills_summary({})
        assert result == ""

    def test_single_skill(self, tmp_path: Path):
        skill, _ = _make_skill(tmp_path, "test", description="Test skill")
        result = get_skills_summary({"test": skill})
        assert "test" in result
        assert "Test skill" in result

    def test_multiple_skills(self, tmp_path: Path):
        skill1, _ = _make_skill(tmp_path, "s1", name="skill1", description="First")
        skill2, _ = _make_skill(tmp_path, "s2", name="skill2", description="Second")
        result = get_skills_summary({"skill1": skill1, "skill2": skill2})
        assert "skill1" in result
        assert "skill2" in result


class TestListSkills:

    def test_empty(self):
        result = list_skills({})
        assert "暂无" in result

    def test_with_skills(self, tmp_path: Path):
        skill, _ = _make_skill(tmp_path, "test", description="Test skill")
        result = list_skills({"test": skill})
        assert "test" in result
        assert "Test skill" in result


class TestShowSkill:

    def test_show_nonexistent_skill(self):
        result = show_skill("nonexistent", {})
        assert "未找到" in result

    def test_show_existing_skill(self, tmp_path: Path):
        skill, _ = _make_skill(tmp_path, "test", description="Test skill")
        result = show_skill("test", {"test": skill})
        assert "Test skill" in result


class TestCreateSkill:

    def test_create_new_skill(self, tmp_path: Path):
        with patch("src.skills.manager.SKILLS_DIR", tmp_path / "skills"):
            result = create_skill(
                name="new-skill",
                description="A new skill",
            )
            assert "已创建" in result or "new-skill" in result
            # 期望子目录格式：skills/new-skill/new-skill.md
            assert (tmp_path / "skills" / "new-skill" / "SKILL.md").exists()

    def test_create_existing_skill(self, tmp_path: Path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        # 已有子目录格式文件
        existing_dir = skill_dir / "existing"
        existing_dir.mkdir()
        (existing_dir / "SKILL.md").write_text("old content", encoding="utf-8")

        with patch("src.skills.manager.SKILLS_DIR", skill_dir):
            result = create_skill(name="existing", description="Test")

        assert "已存在" in result


class TestConsolidateSkills:

    def test_less_than_two_skills(self, tmp_path: Path):
        from src.skills.manager import consolidate_skills

        skill, _ = _make_skill(tmp_path, "solo", description="Solo")
        mock_llm = MagicMock()

        actions, analysis = consolidate_skills({"solo": skill}, mock_llm)
        assert actions == []
        assert analysis == ""

    def test_consolidate_with_llm_failure(self, tmp_path: Path):
        from src.skills.manager import consolidate_skills

        skill1, _ = _make_skill(tmp_path, "s1", description="S1")
        skill2, _ = _make_skill(tmp_path, "s2", description="S2")

        mock_llm = MagicMock()
        mock_llm.clone_for_inference.side_effect = Exception("LLM error")

        actions, analysis = consolidate_skills({"s1": skill1, "s2": skill2}, mock_llm)
        assert actions == []
        assert "[错误]" in analysis


class TestExecuteConsolidation:

    def test_empty_actions(self):
        result = execute_consolidation([])
        assert "没有需要合并" in result

    def test_nonexistent_skill_path(self, tmp_path: Path):
        with patch("src.skills.manager.SKILLS_DIR", tmp_path):
            action = ConsolidationAction(
                keep="nonexistent",
                delete=[],
                merged_body="Merged body",
                keep_invocation_count=0,
            )

            result = execute_consolidation([action])
            assert "[跳过]" in result


class TestLoadAllSkills:

    def test_load_skills_from_directory(self, tmp_path: Path):
        """测试加载子目录格式的 skills"""
        _make_skill(tmp_path, "skill1", description="Skill 1")
        _make_skill(tmp_path, "skill2", description="Skill 2")

        # 验证子目录结构存在
        assert (tmp_path / "skill1" / "SKILL.md").exists()
        assert (tmp_path / "skill2" / "SKILL.md").exists()

        empty_base = tmp_path / "empty_base"
        empty_base.mkdir()
        with patch("src.skills.manager.SKILLS_DIR", tmp_path), \
             patch("src.skills.manager.BASE_SKILLS_DIR", empty_base):
            skills = load_all_skills()

        assert len(skills) == 2
        assert "skill1" in skills
        assert "skill2" in skills

    def test_load_skills_empty_directory(self, tmp_path: Path):
        empty_base = tmp_path / "empty_base"
        empty_base.mkdir()
        with patch("src.skills.manager.SKILLS_DIR", tmp_path), \
             patch("src.skills.manager.BASE_SKILLS_DIR", empty_base):
            skills = load_all_skills()
        assert skills == {}
