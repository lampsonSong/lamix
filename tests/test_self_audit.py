"""self_audit 模块测试。"""

import os
import re
import tempfile
import pytest
from pathlib import Path

from src.core.self_audit import (
    AuditFinding,
    AuditReport,
    scan_skills,
    scan_projects,
    scan_skill_scripts,
    run_audit,
    format_report_detail,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_lamix_dir(tmp_path):
    """用临时目录替代 ~/.lamix 用于测试。"""
    skills_dir = tmp_path / "skills"
    projects_dir = tmp_path / "projects"
    skills_dir.mkdir()
    projects_dir.mkdir()

    from src.core.self_audit import scanners, lifecycle
    old_skills = scanners.SKILLS_DIR
    old_projects = scanners.PROJECTS_DIR
    scanners.SKILLS_DIR = skills_dir
    scanners.PROJECTS_DIR = projects_dir
    lifecycle.SKILLS_DIR = skills_dir
    lifecycle.PROJECTS_DIR = projects_dir

    yield tmp_path, skills_dir, projects_dir

    scanners.SKILLS_DIR = old_skills
    scanners.PROJECTS_DIR = old_projects
    lifecycle.SKILLS_DIR = old_skills
    lifecycle.PROJECTS_DIR = old_projects


# ── AuditReport ────────────────────────────────────────────────────────────────

class TestAuditReport:
    def test_summary_text_no_findings(self):
        report = AuditReport(
            timestamp="2025-05-01 04:00",
            duration_seconds=0.5,
            skills_scanned=10,
            projects_scanned=3,
            scripts_scanned=2,
            findings=[],
        )
        text = report.summary_text()
        assert "没有发现问题" in text

    def test_summary_text_with_findings(self):
        report = AuditReport(
            timestamp="2025-05-01 04:00",
            duration_seconds=0.5,
            skills_scanned=10,
            projects_scanned=3,
            scripts_scanned=0,
            findings=[
                AuditFinding("error", "skill", "bad-skill", "YAML解析失败"),
                AuditFinding("warning", "skill", "orphan", "目录无SKILL.md"),
            ],
        )
        text = report.summary_text()
        assert "error=1" in text
        assert "warning=1" in text


# ── scan_skills ───────────────────────────────────────────────────────────────

class TestScanSkills:
    def test_valid_skill_no_findings(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        skill_file = skills_dir / "good-skill.md"
        skill_file.write_text("""---
name: good-skill
description: 一个正常的 skill
triggers:
  - 好用
---
# 正文
1. **第一步**: 执行
2. **第二步**: 验证
""", encoding="utf-8")

        findings = scan_skills()
        error_finding = [f for f in findings if f.severity == "error"]
        assert len(error_finding) == 0

    def test_missing_skills_dir(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        import shutil
        shutil.rmtree(skills_dir)
        findings = scan_skills()
        assert findings == []

    def test_orphan_md_file_no_frontmatter(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        # 平铺格式下，orphan 表现为没有 frontmatter 的 .md 文件
        orphan_file = skills_dir / "orphan-skill.md"
        orphan_file.write_text("some doc without frontmatter", encoding="utf-8")

        findings = scan_skills()
        assert any(f.target == "orphan-skill" and "缺少 frontmatter" in f.message for f in findings)

    def test_missing_frontmatter(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        skill_file = skills_dir / "no-fm.md"
        skill_file.write_text("没有 frontmatter 的 skill 正文", encoding="utf-8")

        findings = scan_skills()
        assert any("缺少 frontmatter" in f.message for f in findings)


    def test_missing_skill_name(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        skill_file = skills_dir / "no-name.md"
        skill_file.write_text("""---
description: 缺少 name 字段
---
正文内容
""", encoding="utf-8")

        findings = scan_skills()
        assert any("name 字段" in f.message and f.severity == "info" for f in findings)

    def test_template_placeholder(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        skill_file = skills_dir / "template-skill.md"
        skill_file.write_text("""---
name: template-skill
description: 模板
triggers:
  - 模板
---
1. 步骤一
2. 步骤二
3. 步骤三
""", encoding="utf-8")

        findings = scan_skills()
        # 应该检测到模板占位内容
        assert any("步骤一" in f.message and "步骤三" in f.message for f in findings)


# ── scan_projects ─────────────────────────────────────────────────────────────

class TestScanProjects:
    def test_valid_project_no_findings(self, temp_lamix_dir):
        _, _, projects_dir = temp_lamix_dir
        (projects_dir / "good-project.md").write_text("""# good-project

## 基本信息
路径: /tmp/test

## 2025-01-01
更新内容
""", encoding="utf-8")

        findings = scan_projects()
        error_findings = [f for f in findings if f.severity == "error"]
        assert len(error_findings) == 0

    def test_missing_first_header(self, temp_lamix_dir):
        _, _, projects_dir = temp_lamix_dir
        (projects_dir / "no-header.md").write_text("没有标题的 project 文件", encoding="utf-8")

        findings = scan_projects()
        assert any("markdown 标题" in f.message for f in findings)

    def test_empty_file(self, temp_lamix_dir):
        _, _, projects_dir = temp_lamix_dir
        (projects_dir / "empty.md").write_text("", encoding="utf-8")

        findings = scan_projects()
        assert any(f.target == "empty" and f.severity == "error" for f in findings)

    def test_unclosed_code_block(self, temp_lamix_dir):
        _, _, projects_dir = temp_lamix_dir
        bt = chr(96)
        triple = bt * 3
        content = f"# broken\n\n{triple}python\ndef foo():\n    pass\n{triple}\n{triple}\n"
        (projects_dir / "broken.md").write_text(content, encoding="utf-8")

        findings = scan_projects()
        assert any("未闭合" in f.message for f in findings)


# ── scan_skill_scripts ────────────────────────────────────────────────────────

class TestScanSkillScripts:
    def test_valid_script_no_findings(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        scripts_dir = skills_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "valid_module.py").write_text('''TOOL_SCHEMA = {
    "function": {
        "name": "learned_valid_module",
        "description": "Does something",
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

def TOOL_RUNNER(params):
    return "ok"
''', encoding="utf-8")

        findings = scan_skill_scripts()
        error_findings = [f for f in findings if f.severity == "error"]
        assert len(error_findings) == 0

    def test_syntax_error(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        scripts_dir = skills_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "bad_syntax.py").write_text("def broken(:", encoding="utf-8")

        findings = scan_skill_scripts()
        assert any(f.target == "scripts/bad_syntax" and f.severity == "error" and "语法错误" in f.message for f in findings)

    def test_blocked_import(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        scripts_dir = skills_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "bad_import.py").write_text('''from src.core import agent
def TOOL_RUNNER(params):
    return "ok"
TOOL_SCHEMA = {"function": {"name": "x", "description": "", "parameters": {"type": "object", "properties": {}, "required": []}}}
''', encoding="utf-8")

        findings = scan_skill_scripts()
        assert any(f.target == "scripts/bad_import" and "危险 import" in f.message for f in findings)

    def test_missing_runner(self, temp_lamix_dir):
        _, skills_dir, _ = temp_lamix_dir
        scripts_dir = skills_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "no_runner.py").write_text('''TOOL_SCHEMA = {"function": {"name": "learned_no_runner", "description": "", "parameters": {"type": "object", "properties": {}, "required": []}}}
''', encoding="utf-8")

        findings = scan_skill_scripts()
        assert any("缺少 TOOL_RUNNER" in f.message for f in findings)


# ── run_audit & format_report_detail ─────────────────────────────────────────

class TestRunAudit:
    def test_full_audit_returns_report(self, temp_lamix_dir):
        _, skills_dir, projects_dir = temp_lamix_dir

        skill_file = skills_dir / "normal.md"
        skill_file.write_text("""---
name: normal
description: normal
triggers:
  - normal
---
1. **步骤**: do it
""", encoding="utf-8")

        (projects_dir / "proj.md").write_text("# proj\n\n内容", encoding="utf-8")

        report = run_audit()
        assert report.skills_scanned == 1
        assert report.projects_scanned == 1
        assert report.scripts_scanned == 0
        assert report.duration_seconds >= 0
        error_finding = [f for f in report.findings if f.severity == "error"]
        assert len(error_finding) == 0


class TestFormatReportDetail:
    def test_empty_report(self):
        report = AuditReport(timestamp="2025-05-01 04:00", duration_seconds=0.1, findings=[])
        text = format_report_detail(report)
        assert "没有发现问题" in text

    def test_grouped_by_severity(self):
        report = AuditReport(
            timestamp="2025-05-01 04:00",
            duration_seconds=0.1,
            skills_scanned=5,
            findings=[
                AuditFinding("error", "skill", "s1", "语法错误"),
                AuditFinding("warning", "skill", "s2", "缺触发词"),
                AuditFinding("info", "skill", "s3", "无编号步骤"),
            ],
        )
        text = format_report_detail(report)
        assert "ERROR" in text
        assert "WARNING" in text
        assert "INFO" in text
