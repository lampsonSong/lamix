"""skill 族化别名解析测试（2026-10-27 skills 族归并配套）。"""

from pathlib import Path

import pytest

from src.core import skills_tools
from src.core.skills_tools import _resolve_skill_path


@pytest.fixture
def family_env(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(skills_tools, "SKILLS_DIR", skills)
    monkeypatch.setattr(skills_tools, "_alias_cache", None)
    fam = skills / "uat-ops-family.md"
    fam.write_text(
        "---\n"
        "name: uat-ops-family\n"
        "description: UAT 族\n"
        "aliases:\n- uat-deploy\n- uat-deploy-workflow\n"
        "---\n"
        "# uat-ops-family\n\n### uat-deploy\n\n内容 A\n",
        encoding="utf-8",
    )
    solo = skills / "solo-skill.md"
    solo.write_text("---\nname: solo-skill\ndescription: 独立\n---\n# solo\n", encoding="utf-8")
    return skills


class TestAliasResolve:
    def test_alias_routes_to_family(self, family_env):
        p, sub = _resolve_skill_path("uat-deploy")
        assert p is not None and p.name == "uat-ops-family.md"
        assert sub is False

    def test_second_alias(self, family_env):
        p, _ = _resolve_skill_path("uat-deploy-workflow")
        assert p is not None and p.name == "uat-ops-family.md"

    def test_real_name_still_direct(self, family_env):
        p, _ = _resolve_skill_path("solo-skill")
        assert p is not None and p.name == "solo-skill.md"

    def test_family_own_name(self, family_env):
        p, _ = _resolve_skill_path("uat-ops-family")
        assert p is not None and p.name == "uat-ops-family.md"

    def test_unknown_name_still_none(self, family_env):
        p, _ = _resolve_skill_path("no-such-skill")
        assert p is None

    def test_cache_invalidated_on_reset(self, family_env):
        _resolve_skill_path("uat-deploy")
        # 新增别名文件 + 清缓存后可解析
        skills_tools._alias_cache = None
        other = family_env / "x-family.md"
        other.write_text(
            "---\nname: x-family\ndescription: x\naliases:\n- old-x\n---\n# x\n",
            encoding="utf-8",
        )
        p, _ = _resolve_skill_path("old-x")
        assert p is not None and p.name == "x-family.md"
