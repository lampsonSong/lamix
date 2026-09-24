"""归档逻辑测试 — 验证基于用户最后活跃日期的归档判断。

覆盖：
1. touch_last_active_date / _get_last_active_date 文件读写
2. cleanup_stale_knowledge 对 skill/info/project 的归档规则
3. 归档基准日期是最后活跃日期而非 today
4. invocation_count 对归档决策的影响
"""

import os
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════════════════


def _write_frontmatter(path: Path, meta: dict, body: str = "test body") -> None:
    """写一个带 YAML frontmatter 的 .md 文件。"""
    import yaml
    lines = ["---"]
    lines.append(yaml.dump(meta, default_flow_style=False).strip())
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _make_dirs(tmp_path):
    """创建标准目录结构并返回 (skills_dir, projects_dir, info_dir)。"""
    skills_dir = tmp_path / "skills"
    projects_dir = tmp_path / "projects"
    # PROJECTS_DIR.parent / "info" → tmp_path / "info"
    info_dir = tmp_path / "info"
    # 确保存在（代码检查 PROJECTS_DIR.exists()）
    projects_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir, projects_dir, info_dir


# ═══════════════════════════════════════════════════════════════════════════════
# 1. touch_last_active_date / _get_last_active_date
# ═══════════════════════════════════════════════════════════════════════════════


class TestLastActiveDate:
    """验证活跃日期记录和读取。"""

    def test_touch_and_read(self, tmp_path):
        """写入后能正确读回。"""
        from src.core.self_audit import touch_last_active_date, _get_last_active_date

        fake_file = tmp_path / ".last_active_date"
        with patch("src.core.self_audit.lifecycle._LAST_ACTIVE_FILE", fake_file):
            touch_last_active_date()
            result = _get_last_active_date()

        assert result == date.today()

    def test_read_empty_file(self, tmp_path):
        """空文件返回 None。"""
        from src.core.self_audit import _get_last_active_date

        fake_file = tmp_path / ".last_active_date"
        fake_file.write_text("", encoding="utf-8")
        with patch("src.core.self_audit.lifecycle._LAST_ACTIVE_FILE", fake_file), \
             patch("src.core.heartbeat.get_last_activity_time", return_value=None):
            result = _get_last_active_date()
        assert result is None

    def test_read_nonexistent_file_falls_back_to_heartbeat(self, tmp_path):
        """文件不存在时 fallback 到 heartbeat。"""
        from src.core.self_audit import _get_last_active_date

        fake_file = tmp_path / ".last_active_date"
        from datetime import datetime
        mock_dt = datetime(2026, 5, 10, 12, 0, 0)

        with patch("src.core.self_audit.lifecycle._LAST_ACTIVE_FILE", fake_file), \
             patch("src.core.heartbeat.get_last_activity_time", return_value=mock_dt):
            result = _get_last_active_date()

        assert result == date(2026, 5, 10)

    def test_read_nonexistent_no_heartbeat(self, tmp_path):
        """文件不存在且 heartbeat 也无 → None。"""
        from src.core.self_audit import _get_last_active_date

        fake_file = tmp_path / ".last_active_date"
        with patch("src.core.self_audit.lifecycle._LAST_ACTIVE_FILE", fake_file), \
             patch("src.core.heartbeat.get_last_activity_time", return_value=None):
            result = _get_last_active_date()
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 归档基准日期基于最后活跃日期
# ═══════════════════════════════════════════════════════════════════════════════


class TestArchiveAnchorDate:
    """验证归档用 anchor_date 而非 today。"""

    def test_skill_archived_by_anchor_not_today(self, tmp_path):
        """skill 最后使用在 anchor_date 前 7 天内，不应被归档。"""
        from src.core.self_audit import cleanup_stale_knowledge

        skills_dir, projects_dir, _ = _make_dirs(tmp_path)
        skill_md = skills_dir / "test-skill.md"

        # anchor_date = 2026-04-25, stale_7 = 2026-04-18
        # last_used = 2026-04-20 > stale_7，不应归档
        anchor = date(2026, 4, 25)
        _write_frontmatter(skill_md, {
            "name": "test-skill",
            "created_at": "2026-04-01",
            "last_used_at": "2026-04-20",
            "invocation_count": 0,
        })

        with patch("src.core.self_audit.lifecycle.SKILLS_DIR", skills_dir), \
             patch("src.core.self_audit.lifecycle.PROJECTS_DIR", projects_dir), \
             patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            findings = cleanup_stale_knowledge(auto_fix=True)

        skill_findings = [f for f in findings if f.category == "skill"]
        assert len(skill_findings) == 0
        assert skill_md.exists()

    def test_skill_archived_when_old_by_anchor(self, tmp_path):
        """skill 最后使用在 anchor_date 前 30 天以上，应归档。"""
        from src.core.self_audit import cleanup_stale_knowledge

        skills_dir, projects_dir, _ = _make_dirs(tmp_path)
        skill_md = skills_dir / "old-skill.md"

        # anchor_date = 2026-05-10, stale_30 = 2026-04-10
        # last_used = 2026-04-01 < stale_30，应归档
        anchor = date(2026, 5, 10)
        _write_frontmatter(skill_md, {
            "name": "old-skill",
            "created_at": "2026-03-01",
            "last_used_at": "2026-04-01",
            "invocation_count": 5,
        })

        with patch("src.core.self_audit.lifecycle.SKILLS_DIR", skills_dir), \
             patch("src.core.self_audit.lifecycle.PROJECTS_DIR", projects_dir), \
             patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            findings = cleanup_stale_knowledge(auto_fix=True)

        skill_findings = [f for f in findings if f.category == "skill"]
        assert len(skill_findings) == 1
        assert skill_findings[0].fixed is True
        assert not skill_md.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Skill 归档规则
# ═══════════════════════════════════════════════════════════════════════════════


class TestSkillArchiveRules:
    """验证 skill 的 7 天 / 30 天归档规则。"""

    def _setup_and_run(self, tmp_path, meta, anchor=None):
        from src.core.self_audit import cleanup_stale_knowledge

        skills_dir, projects_dir, _ = _make_dirs(tmp_path)
        skill_md = skills_dir / "test-skill.md"
        _write_frontmatter(skill_md, meta)
        if anchor is None:
            anchor = date.today()

        with patch("src.core.self_audit.lifecycle.SKILLS_DIR", skills_dir), \
             patch("src.core.self_audit.lifecycle.PROJECTS_DIR", projects_dir), \
             patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            findings = cleanup_stale_knowledge(auto_fix=True)
        return [f for f in findings if f.category == "skill"], skill_md

    def test_recent_skill_kept(self, tmp_path):
        """7 天内有使用，保留。"""
        findings, skill_md = self._setup_and_run(tmp_path, {
            "name": "active-skill",
            "created_at": "2026-01-01",
            "last_used_at": (date.today() - timedelta(days=3)).isoformat(),
            "invocation_count": 5,
        })
        assert len(findings) == 0
        assert skill_md.exists()

    def test_7days_no_invocation_archived(self, tmp_path):
        """7 天没用且 invocation_count=0，归档。"""
        anchor = date.today()
        findings, skill_md = self._setup_and_run(tmp_path, {
            "name": "unused-skill",
            "created_at": (anchor - timedelta(days=10)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
            "invocation_count": 0,
        })
        assert len(findings) == 1
        assert findings[0].fixed is True
        assert not skill_md.exists()

    def test_7days_has_invocation_kept(self, tmp_path):
        """7 天没用但 invocation_count>0，保留。"""
        anchor = date.today()
        findings, skill_md = self._setup_and_run(tmp_path, {
            "name": "used-before-skill",
            "created_at": (anchor - timedelta(days=20)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
            "invocation_count": 3,
        })
        assert len(findings) == 0
        assert skill_md.exists()

    def test_30days_archived_regardless(self, tmp_path):
        """30 天没用，不管 invocation_count，归档。"""
        anchor = date.today()
        findings, skill_md = self._setup_and_run(tmp_path, {
            "name": "very-old-skill",
            "created_at": (anchor - timedelta(days=40)).isoformat(),
            "last_used_at": (anchor - timedelta(days=31)).isoformat(),
            "invocation_count": 10,
        })
        assert len(findings) == 1
        assert findings[0].fixed is True
        assert not skill_md.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Info 归档规则（统一 skill 规则）
# ═══════════════════════════════════════════════════════════════════════════════


class TestInfoArchiveRules:
    """验证 info 的归档规则与 skill 一致。"""

    def _setup_and_run(self, tmp_path, meta, anchor=None):
        from src.core.self_audit import cleanup_stale_knowledge

        skills_dir, projects_dir, info_dir = _make_dirs(tmp_path)
        _write_frontmatter(info_dir / "test-info.md", meta)
        if anchor is None:
            anchor = date.today()

        with patch("src.core.self_audit.lifecycle.SKILLS_DIR", skills_dir), \
             patch("src.core.self_audit.lifecycle.PROJECTS_DIR", projects_dir), \
             patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            findings = cleanup_stale_knowledge(auto_fix=True)
        return [f for f in findings if f.category == "info"], info_dir / "test-info.md"

    def test_7days_no_invocation_archived(self, tmp_path):
        """info 7 天没用且 invocation_count=0，归档。"""
        anchor = date.today()
        findings, info_file = self._setup_and_run(tmp_path, {
            "name": "unused-info",
            "created_at": (anchor - timedelta(days=10)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
            "invocation_count": 0,
        })
        assert len(findings) == 1
        assert findings[0].fixed is True
        assert not info_file.exists()

    def test_7days_has_invocation_kept(self, tmp_path):
        """info 7 天没用但 invocation_count>0，保留。"""
        anchor = date.today()
        findings, info_file = self._setup_and_run(tmp_path, {
            "name": "used-info",
            "created_at": (anchor - timedelta(days=20)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
            "invocation_count": 3,
        })
        assert len(findings) == 0
        assert info_file.exists()

    def test_30days_archived_regardless(self, tmp_path):
        """info 30 天没用，归档。"""
        anchor = date.today()
        findings, info_file = self._setup_and_run(tmp_path, {
            "name": "old-info",
            "created_at": (anchor - timedelta(days=40)).isoformat(),
            "last_used_at": (anchor - timedelta(days=31)).isoformat(),
            "invocation_count": 10,
        })
        assert len(findings) == 1
        assert findings[0].fixed is True


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Project 归档规则（统一 skill 规则）
# ═══════════════════════════════════════════════════════════════════════════════


class TestProjectArchiveRules:
    """验证 project 的归档规则与 skill 一致。"""

    def _setup_and_run(self, tmp_path, meta, anchor=None):
        from src.core.self_audit import cleanup_stale_knowledge

        skills_dir, projects_dir, _ = _make_dirs(tmp_path)
        _write_frontmatter(projects_dir / "test-project.md", meta)
        if anchor is None:
            anchor = date.today()

        with patch("src.core.self_audit.lifecycle.SKILLS_DIR", skills_dir), \
             patch("src.core.self_audit.lifecycle.PROJECTS_DIR", projects_dir), \
             patch("src.core.self_audit.lifecycle._get_last_active_date", return_value=anchor):
            findings = cleanup_stale_knowledge(auto_fix=True)
        return [f for f in findings if f.category == "project"], projects_dir / "test-project.md"

    def test_7days_no_invocation_archived(self, tmp_path):
        """project 7 天没用且 invocation_count=0，归档。"""
        anchor = date.today()
        findings, proj_file = self._setup_and_run(tmp_path, {
            "name": "unused-project",
            "created_at": (anchor - timedelta(days=10)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
            "invocation_count": 0,
        })
        assert len(findings) == 1
        assert findings[0].fixed is True
        assert not proj_file.exists()

    def test_7days_has_invocation_kept(self, tmp_path):
        """project 7 天没用但 invocation_count>0，保留。"""
        anchor = date.today()
        findings, proj_file = self._setup_and_run(tmp_path, {
            "name": "used-project",
            "created_at": (anchor - timedelta(days=20)).isoformat(),
            "last_used_at": (anchor - timedelta(days=8)).isoformat(),
            "invocation_count": 5,
        })
        assert len(findings) == 0
        assert proj_file.exists()

    def test_30days_archived(self, tmp_path):
        """project 30 天没用，归档。"""
        anchor = date.today()
        findings, proj_file = self._setup_and_run(tmp_path, {
            "name": "old-project",
            "created_at": (anchor - timedelta(days=40)).isoformat(),
            "last_used_at": (anchor - timedelta(days=31)).isoformat(),
            "invocation_count": 10,
        })
        assert len(findings) == 1
        assert findings[0].fixed is True
