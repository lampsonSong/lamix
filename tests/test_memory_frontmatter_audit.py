"""memory frontmatter 规范审计测试（2026-10-27 新增能力）。

覆盖：
1. scan_memory_frontmatter：缺 frontmatter / 缺字段 → 自动补齐；已有字段不覆盖
2. info/project 归档判据：只看 30 天未使用（不再用 7 天+0 调用），避免误杀
3. info 层有 frontmatter 时仍报 mtime 陈旧提示（可见性不丢）
4. load_info 读取时写回 last_used_at（info 层使用信号）
5. dup_scan 相似度计算剥离 frontmatter（全库共同词不抬高相似度）
"""

import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _fm(meta: dict, body: str = "正文内容\n") -> str:
    fm = yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip()
    return f"---\n{fm}\n---\n\n{body}"


@pytest.fixture
def mem_dirs(tmp_path):
    """projects / info / skills 三个临时目录 + 屏蔽真实 memory 目录。"""
    from src.core import self_audit as sa
    from src.core.self_audit import lifecycle, scanners

    projects = tmp_path / "projects"
    info = tmp_path / "info"
    skills = tmp_path / "skills"
    for d in (projects, info, skills):
        d.mkdir()

    old = (scanners.SKILLS_DIR, scanners.PROJECTS_DIR, sa.INFO_DIR,
           lifecycle.SKILLS_DIR, lifecycle.PROJECTS_DIR)
    scanners.SKILLS_DIR, scanners.PROJECTS_DIR, sa.INFO_DIR = skills, projects, info
    lifecycle.SKILLS_DIR, lifecycle.PROJECTS_DIR = skills, projects
    yield projects, info, skills
    (scanners.SKILLS_DIR, scanners.PROJECTS_DIR, sa.INFO_DIR,
     lifecycle.SKILLS_DIR, lifecycle.PROJECTS_DIR) = old


# ══ 1. scan_memory_frontmatter ══════════════════════════════════════════════

class TestScanMemoryFrontmatter:
    def test_adds_frontmatter_when_missing(self, mem_dirs):
        from src.core.self_audit.scanners import scan_memory_frontmatter
        projects, info, _ = mem_dirs

        _write(projects / "p1.md", "# p1\n\n> 项目说明：某某\n\n正文\n")
        _write(info / "i1.md", "# i1\n\n> 知识说明：某某\n\n正文\n")

        findings = scan_memory_frontmatter(auto_fix=True)
        assert len(findings) == 2
        assert all(f.fixed for f in findings)

        for path, kind in ((projects / "p1.md", "project"), (info / "i1.md", "info")):
            raw = path.read_text(encoding="utf-8")
            assert raw.startswith("---\n")
            meta = yaml.safe_load(re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL).group(1))
            assert meta["name"] == path.stem
            assert meta["description"] == ("项目说明：某某" if kind == "project" else "知识说明：某某")
            assert meta["created_at"] and meta["last_used_at"]
            # 正文完整保留
            assert "正文" in raw

    def test_does_not_overwrite_existing_fields(self, mem_dirs):
        from src.core.self_audit.scanners import scan_memory_frontmatter
        projects, _, _ = mem_dirs

        _write(projects / "p2.md", _fm({"name": "自定义名", "description": "原描述"}))
        scan_memory_frontmatter(auto_fix=True)

        raw = (projects / "p2.md").read_text(encoding="utf-8")
        meta = yaml.safe_load(re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL).group(1))
        assert meta["name"] == "自定义名"
        assert meta["description"] == "原描述"
        assert "created_at" in meta and "last_used_at" in meta

    def test_complete_frontmatter_produces_no_finding(self, mem_dirs):
        from src.core.self_audit.scanners import scan_memory_frontmatter
        projects, _, _ = mem_dirs
        _write(projects / "p3.md", _fm({
            "name": "p3", "description": "d",
            "created_at": "2026-10-01", "last_used_at": "2026-10-20",
        }))
        assert scan_memory_frontmatter(auto_fix=True) == []

    def test_no_auto_fix_only_reports(self, mem_dirs):
        from src.core.self_audit.scanners import scan_memory_frontmatter
        projects, _, _ = mem_dirs
        _write(projects / "p4.md", "# p4\n\n正文\n")

        findings = scan_memory_frontmatter(auto_fix=False)
        assert len(findings) == 1
        assert findings[0].fixed is False
        assert not (projects / "p4.md").read_text(encoding="utf-8").startswith("---")

    def test_archived_dir_skipped(self, mem_dirs):
        from src.core.self_audit.scanners import scan_memory_frontmatter
        projects, _, _ = mem_dirs
        _write(projects / ".archived" / "old.md", "# old\n")
        assert scan_memory_frontmatter(auto_fix=True) == []


# ══ 2 & 3. 归档判据 ═════════════════════════════════════════════════════════

class TestMemoryArchiveJudgement:
    def _run(self, projects, info, anchor):
        from src.core.self_audit import cleanup_stale_knowledge
        with patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            return cleanup_stale_knowledge(auto_fix=True)

    def test_info_not_archived_by_7day_rule(self, mem_dirs):
        """8 天未读但从未被调用（invocation_count 缺失）→ 不应归档。

        2026-10-27 回归：若沿用 skills 的「7 天+0 调用」规则，补 frontmatter 后
        从未被读取过的知识条目 7 天即被误杀。
        """
        projects, info, _ = mem_dirs
        anchor = date.today()
        _write(info / "cold.md", _fm({
            "name": "cold",
            "created_at": (anchor - timedelta(days=20)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
        }))

        self._run(projects, info, anchor)
        assert (info / "cold.md").exists(), "8 天未读的知识条目被误杀"

    def test_info_archived_after_30days(self, mem_dirs):
        projects, info, _ = mem_dirs
        anchor = date.today()
        _write(info / "dead.md", _fm({
            "name": "dead",
            "created_at": (anchor - timedelta(days=60)).isoformat(),
            "last_used_at": (anchor - timedelta(days=31)).isoformat(),
        }))

        self._run(projects, info, anchor)
        assert not (info / "dead.md").exists()
        assert (info / ".archived" / "dead.md").exists()

    def test_project_not_archived_by_7day_rule(self, mem_dirs):
        projects, info, _ = mem_dirs
        anchor = date.today()
        _write(projects / "cold_proj.md", _fm({
            "name": "cold_proj",
            "created_at": (anchor - timedelta(days=20)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
        }))

        self._run(projects, info, anchor)
        assert (projects / "cold_proj.md").exists()

    def test_info_with_frontmatter_still_reports_stale(self, mem_dirs):
        """有 frontmatter 但内容陈旧 → 仍报 mtime 提示（可见性不丢）。"""
        projects, info, _ = mem_dirs
        anchor = date.today()
        p = info / "stale.md"
        _write(p, _fm({
            "name": "stale",
            "created_at": (anchor - timedelta(days=60)).isoformat(),
            "last_used_at": (anchor - timedelta(days=1)).isoformat(),
        }))
        import os, time
        old = time.time() - 60 * 86400
        os.utime(p, (old, old))

        findings = self._run(projects, info, anchor)
        stale = [f for f in findings if f.target == "stale" and "未更新" in f.message]
        assert stale, "陈旧提示丢失"
        assert p.exists(), "刚被读过的条目不应被归档"

    def test_skill_7day_rule_preserved(self, mem_dirs):
        """skills 层的 7 天+0 调用规则保持不变（回归保护）。"""
        projects, info, skills = mem_dirs
        anchor = date.today()
        _write(skills / "unused-skill.md", _fm({
            "name": "unused-skill",
            "created_at": (anchor - timedelta(days=10)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
            "invocation_count": 0,
        }))

        self._run(projects, info, anchor)
        assert not (skills / "unused-skill.md").exists(), "skills 7 天规则被误改"


# ══ 4. load_info 写回 last_used_at ══════════════════════════════════════════

class TestLoadInfoTouchesLastUsed:
    def test_read_updates_last_used_at(self, tmp_path):
        from src.core import prompt_builder
        info_dir = tmp_path / "info"
        info_dir.mkdir()
        target = info_dir / "readme.md"
        _write(target, _fm({"name": "readme", "description": "d"}, "# readme\n\n正文\n"))

        with patch.object(prompt_builder, "INFO_DIR", info_dir):
            out = prompt_builder.load_info("readme")
        assert "正文" in out
        assert "---" not in out, "frontmatter 不应出现在返回内容里"

        raw = target.read_text(encoding="utf-8")
        meta = yaml.safe_load(re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL).group(1))
        assert meta.get("last_used_at") == date.today().isoformat()
        assert meta.get("name") == "readme"


# ══ 5. dup_scan 剥离 frontmatter ════════════════════════════════════════════

class TestDupScanStripsFrontmatter:
    def test_common_frontmatter_not_counted_as_duplicate(self, tmp_path):
        from src.core.self_audit.dup_scan import scan_duplicate_knowledge
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        common = {"name": "x", "description": "同样的描述", "created_at": "2026-10-01",
                  "last_used_at": "2026-10-20"}
        _write(a, _fm(common, "内容甲" * 60))
        _write(b, _fm(common, "内容乙" * 60))

        findings = scan_duplicate_knowledge(info_dir=tmp_path)
        dup = [f for f in findings if "内容疑似重复" in f.message]
        assert not dup, f"共同 frontmatter 抬高了相似度：{[f.message for f in dup]}"


class TestDupScanCrossLayerName:
    def test_cross_layer_same_name_downgraded_to_info(self, tmp_path):
        """跨层同名（project + skill）降为 info，不再是 warning 噪声。"""
        from src.core.self_audit.dup_scan import scan_duplicate_knowledge
        projects = tmp_path / "projects"
        skills = tmp_path / "skills"
        projects.mkdir()
        skills.mkdir()
        _write(projects / "baidaoai-uat-deploy.md", "# 状态快照\n\n项目状态\n" * 20)
        _write(skills / "baidaoai-uat-deploy.md", "# 工作流\n\n部署步骤\n" * 20)

        findings = scan_duplicate_knowledge(projects_dir=projects, skills_dir=skills)
        hit = [f for f in findings if f.target == "baidaoai-uat-deploy" and "同名" in f.message]
        assert len(hit) == 1
        assert hit[0].severity == "info", "跨层同名应降为 info"

    def test_same_layer_name_still_warning(self, tmp_path):
        from src.core.self_audit.dup_scan import scan_duplicate_knowledge
        projects = tmp_path / "projects"
        projects.mkdir()
        _write(projects / "foo.md", "# A\n\n内容甲" * 30)
        _write(projects / "foos.md", "# B\n\n内容乙" * 30)
        findings = scan_duplicate_knowledge(projects_dir=projects)
        hit = [f for f in findings if f.target == "foo" and "归一化" in f.message]
        assert len(hit) == 1
        assert hit[0].severity == "warning", "同层归一化同名仍应 warning"
